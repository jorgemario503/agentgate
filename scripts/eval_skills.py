"""Controlled DeepInfra evaluation of open-model skills (OFF vs A vs B).

This is a *reproducible evaluation harness*, not production telemetry. It drives
the real DeepInfraAgentClient directly (same system prompt + tool registry the
runner uses), runs scripted multi-turn scenarios, and scores objective validators.

Usage:
    python -m scripts.eval_skills --dry-run          # offline: print the plan
    python -m scripts.eval_skills --scenarios 6      # run first N scenarios
    python -m scripts.eval_skills --modes off,a,b

Modes:
    off  - no skills (baseline)
    a    - catalog + model-selected skill_read
    b    - catalog + harness recommendations + model-selected skill_read

Each scenario is graded by a deterministic validator over the produced files
(no LLM judge). skill_read calls are NOT counted as success.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import core.skills as skills
import core.prompts as prompts
import core.tools.registry as reg
from core.sessions import SessionState
from core.providers import settings_for


# ---------------------------------------------------------------------------
# Fixed skill catalog (distinct + overlapping descriptions)
# ---------------------------------------------------------------------------

SKILLS = {
    "csv-clean": (
        "Clean and normalize CSV files: trim whitespace, drop empty rows, "
        "dedupe rows, and enforce a header.",
        "1. Read the CSV.\n2. Trim whitespace on all cells.\n3. Drop rows that "
        "are entirely empty.\n4. Dedupe identical rows.\n5. Ensure the header row "
        "is present and unique.\n",
    ),
    "json-validate": (
        "Validate JSON files against basic shape rules and report the first error.",
        "1. Parse the JSON.\n2. If parse fails, report the line/column.\n3. "
        "Check the top-level is an object.\n",
    ),
    "sum-digits": (
        "Sum all numeric digit characters appearing in a text file.",
        "1. Read the file.\n2. Extract every digit character (0-9).\n3. Sum them "
        "as integers.\n4. Print the sum.\n",
    ),
}


def _build_catalog(root: Path) -> "skills.Catalog":
    for name, (desc, body) in SKILLS.items():
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}")
    return skills.discover(root)


# ---------------------------------------------------------------------------
# Scenario model
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    id: str
    turns: list[str]           # user messages, in order
    setup: dict[str, str]      # files to create in the workspace before turn 1
    relevant: list[str]        # ground-truth relevant skills
    deadline_turn: int         # earliest useful turn (1-based)
    validate: Callable[[Path], tuple[bool, str]]  # (ok, detail)


def _v(path_acc: str, pred: Callable[[str], bool], label: str) -> Callable[[Path], tuple[bool, str]]:
    def _check(ws: Path):
        p = ws / path_acc
        if not p.exists():
            return False, f"{label}: {path_acc} missing"
        try:
            txt = p.read_text()
        except Exception as e:
            return False, f"{label}: unreadable {e}"
        return (pred(txt), f"{label}: {'ok' if pred(txt) else 'failed'}")
    return _check


SCENARIOS: list[Scenario] = []


def _add(sc: Scenario):
    SCENARIOS.append(sc)


# S-obvious: need csv-clean early.
_add(Scenario(
    id="csv-tidy",
    turns=["Clean the file data.csv so it has a header and no empty/duplicate rows. "
           "Write the cleaned result to clean.csv."],
    setup={"data.csv": "a,b\n 1 , 2 \n\n1,2\n,,\n3,4\n1,2\n"},
    relevant=["csv-clean"], deadline_turn=1,
    validate=_v("clean.csv", lambda t: t.strip().splitlines()[0] == "a,b"
                and "1,2" in t and t.count("1,2") == 1, "csv-cleaned"),
))

# S-failure: relevant skill only after a tool failure (model must recover).
_add(Scenario(
    id="json-recover",
    turns=["Validate config.json and tell me if it is valid JSON. Write app_valid.txt "
           "containing exactly 'valid' or 'invalid'."],
    setup={"config.json": '{ "a": 1, }'},  # invalid trailing comma
    relevant=["json-validate"], deadline_turn=1,
    validate=_v("app_valid.txt", lambda t: t.strip() == "invalid", "json-invalid-detected"),
))

# S-distractor: irrelevant skill; no skill needed.
_add(Scenario(
    id="no-skill-sum",
    turns=["Create out.txt containing the sum of the two numbers 17 and 25."],
    setup={},
    relevant=[], deadline_turn=1,
    validate=_v("out.txt", lambda t: "42" in t, "sum-correct"),
))

# S-digit: sum-digits skill relevant, procedural constraint required.
_add(Scenario(
    id="digit-sum",
    turns=["Sum every digit character that appears in notes.txt and write just the "
           "number to digits.txt."],
    setup={"notes.txt": "order 1x2 then 3, ref 4-5 and 6"},
    relevant=["sum-digits"], deadline_turn=1,
    validate=_v("digits.txt", lambda t: t.strip() == "21", "digits-summed"),  # 1+2+3+4+5+6
))



# S-hard: digit *characters* summed, not numeric tokens (a model that sums
# numbers instead of digit chars gets a different value). The skill spells out
# the procedure, so it should help a model that would otherwise sum numbers.
_add(Scenario(
    id="digit-chars",
    turns=["In refs.txt, sum every individual digit character (treat 12 as 1+2). "
           "Write only the integer to dc.txt."],
    setup={"refs.txt": "v12 and 34 then 5"},
    relevant=["sum-digits"], deadline_turn=1,
    validate=_v("dc.txt", lambda t: t.strip() == "15", "digit-chars-summed"),  # 1+2+3+4+5=15
))

# S-format: exact required output format a model may not guess.
_add(Scenario(
    id="csv-header-required",
    turns=["Normalize rows.csv to have exactly the header 'x,y' and no blank or "
           "duplicate rows; write the result to norm.csv with a header even if "
           "the input lacked values."],
    setup={"rows.csv": "9,8\n9,8\n\n"},
    relevant=["csv-clean"], deadline_turn=1,
    validate=_v("norm.csv", lambda t: t.strip().splitlines()[0] == "x,y"
                and t.count("9,8") == 1, "csv-header"),
))

def _apply_workspace(ws: Path, setup: dict[str, str]):
    for name, content in setup.items():
        p = ws / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


# ---------------------------------------------------------------------------
# Runner driver
# ---------------------------------------------------------------------------

async def _perm_allow(tool_name, input_data):
    """Evaluation-only: auto-allow everything (no human in the loop)."""
    from runners.deepinfra import PermissionResultAllow
    return PermissionResultAllow()


async def _run_scenario(scenario: Scenario, mode: str, model: str) -> dict:
    from runners.deepinfra import DeepInfraAgentClient, AssistantMessage, TextBlock, ToolUseBlock

    ws = Path(tempfile.mkdtemp(prefix=f"evalskills-{scenario.id}-"))
    _apply_workspace(ws, scenario.setup)
    catalog_root = Path(tempfile.mkdtemp(prefix="skills-cat-"))
    _build_catalog(catalog_root)

    enabled = mode in ("a", "b")
    ctx = skills.build_skills_context(
        enabled=enabled, skills_dir=str(catalog_root), manifest_tokens=600,
        selection_mode="recommend" if mode == "b" else "model",
    )
    sess = skills.SkillSession(ctx.catalog) if ctx.enabled else None

    history_path = str(ws / "_history.json")
    system_prompt = prompts.build_system_prompt(
        state=SessionState.NEW, history_path=history_path, workspace_path=str(ws),
        skills_manifest=ctx.manifest,
    )
    tools = reg.build_tools(
        str(ws), _StubStore(), output_cap=8000, bash_timeout=60,
        skills_catalog=ctx.catalog if ctx.enabled else None,
    )
    settings = settings_for("deepinfra")
    client = DeepInfraAgentClient(
        model=model, api_key=settings.api_key, context_window=settings.context_window,
        tools=tools, system_prompt=system_prompt, can_use_tool=_perm_allow,
        session_id=scenario.id,
        skills_context=ctx if ctx.enabled else None, skill_session=sess,
    )
    await client.connect()

    # Capture usage/cost/latency per API call by wrapping the raw call.
    usage_totals = {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0, "cost": 0.0, "calls": 0}
    _orig_call = client._call_deepinfra
    async def _wrapped_call():
        data = await _orig_call()
        u = (data.get("usage") or {}) if isinstance(data, dict) else {}
        usage_totals["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        usage_totals["completion_tokens"] += int(u.get("completion_tokens") or 0)
        usage_totals["cached_tokens"] += int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        usage_totals["cost"] += float(u.get("estimated_cost") or 0.0)
        usage_totals["calls"] += 1
        return data
    client._call_deepinfra = _wrapped_call

    skill_reads: list[tuple[int, str]] = []
    try:
        for turn_idx, prompt in enumerate(scenario.turns, start=1):
            # Mode B nudge.
            mem = None
            nudge = client.skills_recommendation(prompt)
            if nudge:
                mem = nudge
            await client.query(prompt, ephemeral_context=mem)
            text_parts: list[str] = []
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            text_parts.append(b.text)
                        elif isinstance(b, ToolUseBlock) and b.name == "skill_read":
                            skill_reads.append((turn_idx, str(b.input.get("name"))))
                # capture usage from last call
            # best-effort perf capture is done inside client if available
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    ok, detail = scenario.validate(ws)
    first_read_turn = skill_reads[0][0] if skill_reads else None
    read_names = [n for _, n in skill_reads]
    precision_recall = _score_reads(read_names, scenario.relevant)
    result = {
        "id": scenario.id,
        "mode": mode,
        "ok": ok,
        "detail": detail,
        "skill_reads": skill_reads,
        "first_read_turn": first_read_turn,
        "deadline_turn": scenario.deadline_turn,
        "just_in_time": (first_read_turn is not None and first_read_turn <= scenario.deadline_turn),
        "relevant": scenario.relevant,
        **precision_recall,
        "usage": usage_totals,
    }
    shutil.rmtree(ws, ignore_errors=True)
    shutil.rmtree(catalog_root, ignore_errors=True)
    return result


class _StubStore:
    def lookup_directory(self, name):
        return None

    def remember_directory(self, name, path):
        pass


def _score_reads(read_names: list[str], relevant: list[str]) -> dict:
    reads = set(read_names)
    rel = set(relevant)
    tp = len(reads & rel)
    fp = len(reads - rel)
    fn = len(rel - reads)
    precision = tp / (tp + fp) if (tp + fp) else (1.0 if not rel else 0.0)
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(precision, 3),
            "recall": round(recall, 3), "false_positive": fp > 0}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default="off,a,b")
    ap.add_argument("--scenarios", type=int, default=len(SCENARIOS))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    chosen = SCENARIOS[: args.scenarios]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    if args.dry_run:
        print(f"{len(chosen)} scenarios x {len(modes)} modes")
        for sc in chosen:
            print(f"  {sc.id}: relevant={sc.relevant} deadline={sc.deadline_turn}")
        return

    settings = settings_for("deepinfra")
    model = settings.model
    print(f"model={model}")
    rows = []
    for sc in chosen:
        for mode in modes:
            r = await _run_scenario(sc, mode, model)
            rows.append(r)
            print(f"  [{mode}] {sc.id}: ok={r['ok']} reads={r['skill_reads']} "
                  f"jit={r['just_in_time']} P={r['precision']} R={r['recall']}")
    out = Path("agent-sessions/_eval") / "skills_eval_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))
    print(f"\nwrote {out}  ({len(rows)} rows)")


if __name__ == "__main__":
    asyncio.run(main())
