"""Controlled DeepInfra evaluation of open-model skills (OFF / A / B / C).

Reproducible evaluation harness (not production telemetry). Drives the real
DeepInfraAgentClient with the same system prompt + shared tool registry the
runner uses, runs scripted multi-turn scenarios with *isolated* state per trial,
and scores objective validators (no LLM judge).

Modes:
    off  - skills unavailable (baseline)
    a    - catalog visible, model-selected `skill_read`
    b    - + deterministic trigger recommendations (Mode B)
    c    - explicit `//skill-name` user invocation (diagnostic)

Usage:
    python -m scripts.eval_skills --dry-run
    python -m scripts.eval_skills --modes off,a,b,c --reps 1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import core.skills as skills
import core.prompts as prompts
import core.tools.registry as reg
from core.sessions import SessionState
from core.providers import settings_for


# name -> (description, triggers, body). Fictional team conventions live in BODY.
SKILLS: dict[str, tuple[str, list[str], str]] = {
    "create-ticket": (
        "Create internal tickets using the team's required format.",
        ["create a ticket", "usual ticket format", "open an issue"],
        "TICKET FORMAT (must follow exactly):\n"
        "- Title MUST start with prefix '[PAY]' for payments, '[OPS]' for ops.\n"
        "- Body sections in this order: Summary, Impact, Steps to Reproduce, Owner.\n"
        "- Priority mapping: 'production down'->P0, 'degraded'->P1, else P2.\n"
        "- Labels use the taxonomy: area:payments, area:ops, sev:p0|p1|p2.\n"
        "- If owner is unknown, write 'Owner: UNASSIGNED' (do NOT invent a name).\n",
    ),
    "review-pr": (
        "Review pull requests using the project's checklist.",
        ["review pr", "review pull request", "review this pr"],
        "PR REVIEW CHECKLIST (include each item verbatim as a bullet):\n"
        "- [ ] Correctness\n- [ ] Tests added\n- [ ] No secrets committed\n"
        "- [ ] Backwards compatibility\nVerdict line exactly 'VERDICT: APPROVE' "
        "or 'VERDICT: CHANGES REQUESTED'.\n",
    ),
    "release-notes": (
        "Write release notes in the prescribed internal structure.",
        ["release notes", "changelog entry"],
        "RELEASE NOTES STRUCTURE:\n"
        "Header: '## Release <version>'\nThen sections in order: Added, Changed, Fixed.\n"
        "Each item prefixed with '- '. Omit empty sections.\n",
    ),
    "csv-clean": (
        "Clean and normalize CSV files.",
        ["clean csv", "tidy csv"],
        "1. Trim cells.\n2. Drop empty rows.\n3. Dedupe rows.\n4. Enforce header.\n",
    ),
    "sum-digits": (
        "Sum individual digit characters in text.",
        ["sum digits", "digit sum"],
        "Sum every DIGIT CHARACTER (12 -> 1+2=3), not numeric tokens. Integer only.\n",
    ),
    "incident-report": (
        "Write an incident report with the org's severity mapping.",
        ["incident report", "postmortem", "outage report"],
        "INCIDENT REPORT FORMAT:\n"
        "Header: '# Incident: <title>'\n"
        "Then: Severity: SEV1|SEV2|SEV3 (SEV1 = full outage, SEV2 = partial, SEV3 = minor)\n"
        "Sections in order: Timeline, Impact, Root Cause, Action Items.\n"
        "Action Items must each start with '- [ ]'.\n",
    ),
    "deploy-prep": (
        "Prepare a deployment checklist with org approval requirements.",
        ["prepare deployment", "deployment checklist", "prep deploy"],
        "DEPLOY CHECKLIST (verbatim bullets required):\n"
        "- [ ] Change ticket linked\n- [ ] Two approvals recorded\n"
        "- [ ] Rollback plan written\n- [ ] Monitoring dashboard checked\n"
        "Finish with 'STATUS: READY' or 'STATUS: BLOCKED'.\n",
    ),
}


def _build_catalog(root: Path) -> "skills.Catalog":
    for name, (desc, triggers, body) in SKILLS.items():
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        tl = "".join(f"      - {t}\n" for t in triggers)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n"
            f"metadata:\n  agentgate:\n    triggers:\n{tl}---\n\n{body}"
        )
    return skills.discover(root)


@dataclass
class Scenario:
    id: str
    turns: list[str]
    setup: dict[str, str]
    relevant: list[str]
    deadline_turn: int
    validate: Callable[[Path], tuple[bool, str]]
    explicit_skill: "str | None" = None
    explicit_task: "str | None" = None
    fabrication: "Callable[[Path], bool] | None" = None


def _v(acc, pred, label):
    def _check(ws: Path):
        p = ws / acc
        if not p.exists():
            return False, f"{label}: {acc} missing"
        try:
            txt = p.read_text()
        except Exception as e:
            return False, f"{label}: unreadable {e}"
        return (pred(txt), f"{label}: {'ok' if pred(txt) else 'failed'}")
    return _check


SCENARIOS: list = []


def _add(sc):
    SCENARIOS.append(sc)


def _ticket_ok(t: str) -> bool:
    return ("[PAY]" in t or "[OPS]" in t) and all(
        s in t for s in ("Summary", "Impact", "Steps to Reproduce", "Owner")
    ) and ("P0" in t or "P1" in t or "P2" in t) and ("area:payments" in t or "area:ops" in t)


def _fabricates_owner(ws: Path) -> bool:
    p = ws / "ticket.md"
    if not p.exists():
        return False
    t = p.read_text()
    if "UNASSIGNED" in t:
        return False
    return re.search(r"Owner:\s*[A-Z][a-z]+", t) is not None


_add(Scenario(
    id="ticket-payments",
    turns=["Create a ticket for Alex using the usual ticket format. The payment "
           "retry issue is happening again and production is down. Save it to ticket.md."],
    setup={}, relevant=["create-ticket"], deadline_turn=1,
    validate=_v("ticket.md", _ticket_ok, "ticket-format"),
    fabrication=_fabricates_owner,
))

_add(Scenario(
    id="ticket-ops-degraded",
    turns=["Open an issue for the nightly batch job that is degraded. Use our usual "
           "ticket format. Owner unknown. Save it to ticket.md."],
    setup={}, relevant=["create-ticket"], deadline_turn=1,
    validate=_v("ticket.md", lambda t: "[OPS]" in t and "P1" in t and "UNASSIGNED" in t,
                "ops-p1-unassigned"),
))


def _pr_ok(t):
    return all(x in t for x in ["Correctness", "Tests added", "No secrets committed",
                                "Backwards compatibility", "VERDICT:"])


_add(Scenario(
    id="pr-review-checklist",
    turns=["Review PR 124 for security and performance. Write the review to review.md."],
    setup={"pr124.diff": "+ password = 'hardcoded'\n+ os.system(cmd)\n"},
    relevant=["review-pr"], deadline_turn=1,
    validate=_v("review.md", _pr_ok, "pr-checklist"),
))

_add(Scenario(
    id="release-notes",
    turns=["Write release notes for version 2.4.0: added SSO, changed login flow, "
           "fixed the retry bug. Save to notes.md."],
    setup={}, relevant=["release-notes"], deadline_turn=1,
    validate=_v("notes.md", lambda t: "## Release 2.4.0" in t and "Added" in t
                and "Changed" in t and "Fixed" in t, "release-structure"),
))

_add(Scenario(
    id="overlap-review",
    turns=["Please review pr 88 quickly. Save to out.md."],
    setup={"pr88.diff": "+ x = 1\n"}, relevant=["review-pr"], deadline_turn=1,
    validate=_v("out.md", lambda t: "VERDICT:" in t, "review-verdict"),
))

_add(Scenario(
    id="no-skill-math",
    turns=["Create out.txt containing the sum of 17 and 25."],
    setup={}, relevant=[], deadline_turn=1,
    validate=_v("out.txt", lambda t: "42" in t, "sum"),
))

_add(Scenario(
    id="needs-clarification",
    turns=["Create a ticket. Save it to ticket.md."],
    setup={}, relevant=["create-ticket"], deadline_turn=1,
    validate=_v("ticket.md", lambda t: "UNASSIGNED" in t or "Summary" in t, "ticket-partial"),
    fabrication=_fabricates_owner,
))

_add(Scenario(
    id="digit-chars",
    turns=["In refs.txt sum every individual digit character (12 means 1+2). "
           "Write only the integer to dc.txt."],
    setup={"refs.txt": "v12 and 34 then 5"}, relevant=["sum-digits"], deadline_turn=1,
    validate=_v("dc.txt", lambda t: t.strip() == "15", "digit-sum"),
))

_add(Scenario(
    id="explicit-ticket",
    turns=[],
    setup={}, relevant=["create-ticket"], deadline_turn=1,
    explicit_skill="create-ticket",
    explicit_task="Create a ticket for Alex about failed payment retries (production down). Save it to ticket.md.",
    validate=_v("ticket.md", _ticket_ok, "explicit-ticket-format"),
))

_add(Scenario(
    id="multiturn-ticket",
    turns=[
        "Create a ticket for the payment retry issue, production down. Save it to ticket.md.",
        "Actually use our usual ticket format and save it to ticket.md.",
    ],
    setup={}, relevant=["create-ticket"], deadline_turn=2,
    validate=_v("ticket.md", _ticket_ok, "multiturn-ticket"),
))



# --- Additional scenarios to reach >=24 and widen coverage ---------------

_add(Scenario(
    id="ticket-paraphrase-1",
    turns=["I need to file a ticket about payment retries, using the official layout. "
           "Production is down. Save ticket.md."],
    setup={}, relevant=["create-ticket"], deadline_turn=1,
    validate=_v("ticket.md", _ticket_ok, "paraphrase-ticket"),
    fabrication=_fabricates_owner,
))

_add(Scenario(
    id="ticket-explicit-format-wins",
    turns=["Use the usual ticket format to open an issue: checkout is degraded "
           "(not fully down). Save to ticket.md."],
    setup={}, relevant=["create-ticket"], deadline_turn=1,
    validate=_v("ticket.md", lambda t: "[PAY]" in t and "P1" in t, "degraded-p1"),
))

_add(Scenario(
    id="pr-review-second",
    turns=["review pull request 9 and write the checklist review to r9.md."],
    setup={"p9.diff": "- old\n+ new\n"}, relevant=["review-pr"], deadline_turn=1,
    validate=_v("r9.md", _pr_ok, "pr-second"),
))

_add(Scenario(
    id="release-notes-second",
    turns=["Give me a changelog entry for 3.1.0: fixed crash on startup. Save to c.md."],
    setup={}, relevant=["release-notes"], deadline_turn=1,
    validate=_v("c.md", lambda t: "## Release 3.1.0" in t and "Fixed" in t, "changelog"),
))

_add(Scenario(
    id="incident-report",
    turns=["Write an incident report for a full outage caused by a bad deploy. "
           "Save to incident.md."],
    setup={}, relevant=["incident-report"], deadline_turn=1,
    validate=_v("incident.md", lambda t: "# Incident" in t and "SEV1" in t
                and "Timeline" in t and "Root Cause" in t and "- [ ]" in t, "incident"),
))

_add(Scenario(
    id="incident-partial",
    turns=["postmortem for the partial web degradation. Write incident.md."],
    setup={}, relevant=["incident-report"], deadline_turn=1,
    validate=_v("incident.md", lambda t: "SEV2" in t and "Action Items" in t, "severity-map"),
))

_add(Scenario(
    id="deploy-prep",
    turns=["Prepare deployment checklist for service payments v2. Save deploy.md."],
    setup={}, relevant=["deploy-prep"], deadline_turn=1,
    validate=_v("deploy.md", lambda t: all(x in t for x in [
        "Change ticket linked", "Two approvals recorded", "Rollback plan written",
        "Monitoring dashboard checked", "STATUS:"]) , "deploy-checklist"),
))

_add(Scenario(
    id="no-skill-copy",
    turns=["Copy the text in 'hello' to hi.txt."],
    setup={"in.txt": "hello"}, relevant=[], deadline_turn=1,
    validate=_v("hi.txt", lambda t: "hello" in t, "copy"),
))

_add(Scenario(
    id="no-skill-arithmetic-2",
    turns=["Write the product of 6 and 7 to prod.txt."],
    setup={}, relevant=[], deadline_turn=1,
    validate=_v("prod.txt", lambda t: "42" in t, "product"),
))

_add(Scenario(
    id="digit-chars-2",
    turns=["Sum the digit sum of nums.txt (each digit separately). digits.txt."],
    setup={"nums.txt": "99 1"}, relevant=["sum-digits"], deadline_turn=1,
    validate=_v("digits.txt", lambda t: t.strip() == "19", "digit-sum-2"),  # 9+9+1
))

_add(Scenario(
    id="explicit-review",
    turns=[],
    setup={"pr.diff": "+ y = 2\n"},
    relevant=["review-pr"], deadline_turn=1,
    explicit_skill="review-pr",
    explicit_task="Review PR 55 and write the review to rev.md.",
    validate=_v("rev.md", _pr_ok, "explicit-review"),
))

_add(Scenario(
    id="explicit-release",
    turns=[],
    setup={}, relevant=["release-notes"], deadline_turn=1,
    explicit_skill="release-notes",
    explicit_task="Write release notes for 5.0.0 adding dark mode. Save rel.md.",
    validate=_v("rel.md", lambda t: "## Release 5.0.0" in t and "Added" in t, "explicit-release"),
))

_add(Scenario(
    id="multiturn-deploy",
    turns=[
        "I want to deploy payments v3 but first do the prep checklist.",
        "Save the deployment checklist to deploy.md now.",
    ],
    setup={}, relevant=["deploy-prep"], deadline_turn=2,
    validate=_v("deploy.md", lambda t: "STATUS:" in t and "Two approvals recorded" in t,
                "multiturn-deploy"),
))

_add(Scenario(
    id="misleading-distractor",
    turns=["Write release notes is NOT what I want; just save the word 'done' to done.txt."],
    setup={}, relevant=[], deadline_turn=1,
    # Mentions 'release notes' (a trigger) but the task is unrelated; the model
    # should not adopt release-note structure for a trivial copy.
    validate=_v("done.txt", lambda t: t.strip() == "done", "distractor-ignored"),
))


def _apply_workspace(ws: Path, setup: dict[str, str]):
    for name, content in setup.items():
        p = ws / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


class _StubStore:
    def lookup_directory(self, name):
        return None

    def remember_directory(self, name, path):
        pass


async def _perm_allow(tool_name, input_data):
    from runners.deepinfra import PermissionResultAllow
    return PermissionResultAllow()


def _score_reads(read_names, relevant):
    reads, rel = set(read_names), set(relevant)
    tp = len(reads & rel); fp = len(reads - rel); fn = len(rel - reads)
    precision = tp / (tp + fp) if (tp + fp) else (1.0 if not rel else 0.0)
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(precision, 3),
            "recall": round(recall, 3), "false_positive": fp > 0}


async def _run_scenario(scenario, mode, model):
    from runners.deepinfra import DeepInfraAgentClient, AssistantMessage, ToolUseBlock

    ws = Path(tempfile.mkdtemp(prefix=f"evalskills-{scenario.id}-"))
    _apply_workspace(ws, scenario.setup)
    catalog_root = Path(tempfile.mkdtemp(prefix="skills-cat-"))
    _build_catalog(catalog_root)

    enabled = mode in ("a", "b", "c")
    ctx = skills.build_skills_context(
        enabled=enabled, skills_dir=str(catalog_root), manifest_tokens=600,
        selection_mode="recommend" if mode == "b" else "model",
    )
    sess = skills.SkillSession(ctx.catalog) if ctx.enabled else None

    history_path = str(ws / "_history.json")
    system_prompt = prompts.build_system_prompt(
        state=SessionState.NEW, history_path=history_path, workspace_path=str(ws),
        skills_manifest=ctx.manifest if mode in ("a", "b") else "",
    )
    tools = reg.build_tools(
        str(ws), _StubStore(), output_cap=8000, bash_timeout=60,
        skills_catalog=ctx.catalog if enabled else None,
    )
    settings = settings_for("deepinfra")
    client = DeepInfraAgentClient(
        model=model, api_key=settings.api_key, context_window=settings.context_window,
        tools=tools, system_prompt=system_prompt, can_use_tool=_perm_allow,
        session_id=f"{scenario.id}-{mode}",
        skills_context=ctx if enabled else None, skill_session=sess,
    )
    await client.connect()

    usage = {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0,
             "cost": 0.0, "calls": 0, "latency": 0.0}
    _orig_call = client._call_deepinfra
    async def _wrapped():
        t0 = time.time()
        data = await _orig_call()
        usage["latency"] += time.time() - t0
        u = (data.get("usage") or {}) if isinstance(data, dict) else {}
        usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        usage["completion_tokens"] += int(u.get("completion_tokens") or 0)
        usage["cached_tokens"] += int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        usage["cost"] += float(u.get("estimated_cost") or 0.0)
        usage["calls"] += 1
        return data
    client._call_deepinfra = _wrapped

    skill_reads = []

    async def _run_one(prompt, turn_idx, explicit_msg=None):
        mem = None
        if mode == "b":
            nudge = client.skills_recommendation(prompt)
            if nudge:
                mem = nudge
        await client.query(prompt, ephemeral_context=mem, explicit_skill_message=explicit_msg)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, ToolUseBlock) and b.name == "skill_read":
                        skill_reads.append((turn_idx, str(b.input.get("name"))))

    try:
        if mode == "c":
            prompt = f"//{scenario.explicit_skill} {scenario.explicit_task}"
            inv = skills.resolve_invocation(prompt, client._skills_context,
                                            client._skill_session, client._skill_turn + 1)
            await _run_one(inv.task, 1, inv.explicit_message)
        else:
            for i, p in enumerate(scenario.turns, start=1):
                await _run_one(p, i)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    ok, detail = scenario.validate(ws)
    fabricated = bool(scenario.fabrication(ws)) if scenario.fabrication else False
    first_read = skill_reads[0][0] if skill_reads else None
    res = {
        "id": scenario.id, "mode": mode, "ok": ok, "detail": detail,
        "skill_reads": skill_reads, "first_read_turn": first_read,
        "deadline_turn": scenario.deadline_turn,
        "just_in_time": (first_read is not None and first_read <= scenario.deadline_turn),
        "relevant": scenario.relevant, "fabricated": fabricated,
        "explicit_skill": scenario.explicit_skill,
        **_score_reads([n for _, n in skill_reads], scenario.relevant),
        "usage": usage,
    }
    shutil.rmtree(ws, ignore_errors=True)
    shutil.rmtree(catalog_root, ignore_errors=True)
    return res


def _summarize(rows):
    from collections import defaultdict
    agg = defaultdict(lambda: {"n": 0, "ok": 0, "reads": 0, "fp": 0, "fab": 0,
                               "pt": 0, "cached": 0, "cost": 0.0, "calls": 0,
                               "latency": 0.0})
    for r in rows:
        a = agg[r["mode"]]
        a["n"] += 1; a["ok"] += int(r["ok"]); a["reads"] += len(r["skill_reads"])
        a["fp"] += int(r["false_positive"]); a["fab"] += int(r["fabricated"])
        a["pt"] += r["usage"]["prompt_tokens"]; a["cached"] += r["usage"]["cached_tokens"]
        a["cost"] += r["usage"]["cost"]; a["calls"] += r["usage"]["calls"]
        a["latency"] += r["usage"].get("latency", 0.0)
    print("\n=== aggregate ===")
    for m in ("off", "a", "b", "c"):
        if m not in agg:
            continue
        a = agg[m]
        print(f"{m:3} ok={a['ok']}/{a['n']} reads={a['reads']} fp={a['fp']} "
              f"fab={a['fab']} pt={a['pt']} cached={a['cached']} "
              f"calls={a['calls']} cost=${a['cost']:.5f} lat={a['latency']:.1f}s")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default="off,a,b,c")
    ap.add_argument("--scenarios", type=int, default=len(SCENARIOS))
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    chosen = SCENARIOS[: args.scenarios]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    if args.dry_run:
        print(f"{len(chosen)} scenarios x {len(modes)} modes x {args.reps} reps")
        for sc in chosen:
            print(f"  {sc.id}: relevant={sc.relevant} deadline={sc.deadline_turn} "
                  f"explicit={sc.explicit_skill}")
        return

    model = settings_for("deepinfra").model
    print(f"model={model} modes={modes} reps={args.reps}")
    rows = []
    for rep in range(args.reps):
        for sc in chosen:
            for mode in modes:
                # Mode C (explicit //skill) only applies to scenarios that
                # define an explicit skill; skip otherwise.
                if mode == "c" and not sc.explicit_skill:
                    continue
                r = await _run_scenario(sc, mode, model)
                r["rep"] = rep
                rows.append(r)
                print(f"  [{mode}] {sc.id}: ok={r['ok']} reads={r['skill_reads']} "
                      f"jit={r['just_in_time']} fab={r['fabricated']} "
                      f"calls={r['usage']['calls']}")
    out = Path("agent-sessions/_eval") / "skills_eval_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))
    _summarize(rows)
    print(f"\nwrote {out}  ({len(rows)} rows)")


if __name__ == "__main__":
    asyncio.run(main())
