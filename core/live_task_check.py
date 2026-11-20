"""Live task-correctness probe against DeepInfra (opt-in, budget-guarded).

Purpose (spec C4, "task correctness"): check that when a bulky tool result is
compressed, the model can still answer a question whose answer depends on
content that was elided - by using visible retained lines or by retrieving the
original through the same artifact id.

This hits a real provider, so it is OPT-IN and COST-GUARDED:

- Skipped unless AGENTGATE_LIVE_TESTS=1 (the test wrapper enforces this).
- Uses the SAME env vars and model id the runner already uses:
  DEEPINFRA_API_KEY / DEEPINFRA_BASE_URL / DEEPINFRA_MODEL via
  core.providers.settings_for("deepinfra") - nothing hardcoded.
- A hard cumulative token budget (AGENTGATE_LIVE_TOKEN_BUDGET, default 6000)
  across the whole run. Each request is pre-checked against the remaining
  budget, and the real usage each response reports is recorded. When the budget
  is short the run stops cleanly and reports what it could not complete.

No secrets are logged or printed.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from core.providers import settings_for
from core.tool_pipeline import ToolResultPipeline
from core.tool_compression import CompressConfig


LIVE_FLAG = "AGENTGATE_LIVE_TESTS"
BUDGET_ENV = "AGENTGATE_LIVE_TOKEN_BUDGET"
DEFAULT_BUDGET = 6000
MAX_COMPLETION_TOKENS = 200


class BudgetExceeded(RuntimeError):
    pass


class skip_reason_error(RuntimeError):
    """Raised to signal the live run cannot proceed (test wrapper skips)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class TokenBudget:
    limit: int = DEFAULT_BUDGET
    used: int = 0
    calls: int = 0
    log: list = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def precheck(self, est_prompt_tokens: int, max_completion: int) -> None:
        worst = est_prompt_tokens + max_completion
        # Guard every call, including the first: a budget smaller than a single
        # worst-case call means we must not start it at all.
        if worst > self.remaining:
            raise BudgetExceeded(
                "refusing call: worst-case %d > remaining %d" % (worst, self.remaining)
            )

    def record(self, usage: Optional[dict]) -> None:
        self.calls += 1
        if not usage:
            self.used += MAX_COMPLETION_TOKENS
            self.log.append({"usage": None})
            return
        total = int(usage.get("total_tokens") or 0)
        if not total:
            total = int(usage.get("prompt_tokens") or 0) + int(
                usage.get("completion_tokens") or 0)
        self.used += total
        self.log.append(usage)


def _est_tokens(text: str) -> int:
    return max(1, int(len(text) / 3.5))


def live_enabled() -> bool:
    return os.environ.get(LIVE_FLAG, "").strip() in ("1", "true", "yes", "on")


def _client():
    from openai import AsyncOpenAI
    s = settings_for("deepinfra")
    api_key = s.api_key
    if not api_key:
        raise skip_reason_error("DEEPINFRA_API_KEY not set")
    base_url = s.base_url or "https://api.deepinfra.com/v1/openai"
    return AsyncOpenAI(api_key=api_key, base_url=base_url)


def _make_needle_log():
    lines = ["INFO task progress %d ok" % i for i in range(400)]
    needle = "FATAL: migration aborted, missing table 'invoices_2024'"
    lines.insert(200, needle)
    return "\n".join(lines), needle


def run_task_correctness(budget: Optional[int] = None) -> dict:
    if not live_enabled():
        raise skip_reason_error(LIVE_FLAG + " not set")
    import asyncio
    s = settings_for("deepinfra")
    model = s.model
    if not model:
        raise skip_reason_error("DEEPINFRA_MODEL not set")
    if budget is None:
        budget = int(os.environ.get(BUDGET_ENV, DEFAULT_BUDGET))
    return asyncio.run(_run_async(model, budget))


async def _run_async(model: str, budget_limit: int) -> dict:
    import re
    import tempfile
    client = _client()
    budget = TokenBudget(limit=budget_limit)
    report = {"model": model, "budget_limit": budget_limit,
              "cases": [], "skipped": [], "used_tokens": 0, "calls": 0}
    full, needle = _make_needle_log()
    try:
        with tempfile.TemporaryDirectory() as d:
            pipe = ToolResultPipeline(
                d, session_id="live", workspace_id="/ws",
                enabled=True, dedup_enabled=False, min_chars=1000,
                compress_config=CompressConfig(min_chars=1000),
            )
            compressed = pipe.process(full, tool_name="Bash")
            m = re.search(r"original=([0-9a-f]{64})", compressed)
            artifact_id = m.group(1) if m else None

            messages = [
                {"role": "system",
                 "content": "You are a terse assistant. Answer briefly."},
                {"role": "user", "content": "Read the tool output."},
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "Bash", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": compressed},
                {"role": "user",
                 "content": "Exactly what was the FATAL error about? Quote the key phrase."},
            ]

            try:
                budget.precheck(_est_tokens(json.dumps(messages)), MAX_COMPLETION_TOKENS)
            except BudgetExceeded as e:
                report["skipped"].append("case1: " + str(e))
            else:
                resp = await client.chat.completions.create(
                    model=model, messages=messages,
                    max_tokens=MAX_COMPLETION_TOKENS, temperature=0)
                data = resp.model_dump(exclude_none=True)
                budget.record(data.get("usage"))
                text = (data["choices"][0]["message"].get("content") or "").strip()
                ok = ("invoices_2024" in text) or ("migration aborted" in text.lower())
                report["cases"].append({
                    "name": "answer_from_compressed",
                    "artifact_id_present": bool(artifact_id),
                    "answer": text[:300],
                    "correct": ok,
                })

            if artifact_id:
                got = pipe.read_original(artifact_id, offset=0, limit=10**7)
                report["cases"].append({
                    "name": "retrieval_exact",
                    "exact_match": got == full,
                })
    finally:
        try:
            await client.close()
        except Exception:
            pass
        report["used_tokens"] = budget.used
        report["calls"] = budget.calls
        report["budget_log"] = budget.log
    return report


def run_live_cache_reuse(budget=None) -> dict:
    """Measure real provider cached_tokens on a repeated-prefix second call.

    Sends the same long, stable prefix twice (only the trailing user question
    differs) and reports the second call's ``cached_tokens``. This is the live
    half of H13; it uses the same budget guard as the task-correctness probe.
    """
    if not live_enabled():
        raise skip_reason_error(LIVE_FLAG + " not set")
    import asyncio

    s = settings_for("deepinfra")
    model = s.model
    if not model:
        raise skip_reason_error("DEEPINFRA_MODEL not set")
    if budget is None:
        budget = int(os.environ.get(BUDGET_ENV, DEFAULT_BUDGET))
    return asyncio.run(_run_cache_async(model, budget))


async def _run_cache_async(model: str, budget_limit: int) -> dict:
    client = _client()
    budget = TokenBudget(limit=budget_limit)
    # A long stable prefix + two different trailing questions.
    prefix = [{"role": "system", "content": "You are terse. " + ("filler sentence. " * 400)}]
    q1 = prefix + [{"role": "user", "content": "Say OK."}]
    q2 = prefix + [{"role": "user", "content": "Say DONE."}]
    report = {"model": model, "budget_limit": budget_limit, "turns": [], "skipped": []}
    try:
        for label, msgs in (("turn1", q1), ("turn2", q2)):
            try:
                budget.precheck(_est_tokens(json.dumps(msgs)), 20)
            except BudgetExceeded as e:
                report["skipped"].append(label + ": " + str(e))
                continue
            resp = await client.chat.completions.create(
                model=model, messages=msgs, max_tokens=20, temperature=0)
            data = resp.model_dump(exclude_none=True)
            usage = data.get("usage") or {}
            budget.record(usage)
            details = usage.get("prompt_tokens_details") or {}
            report["turns"].append({
                "label": label,
                "prompt_tokens": usage.get("prompt_tokens"),
                "cached_tokens": details.get("cached_tokens"),
                "total_tokens": usage.get("total_tokens"),
            })
    finally:
        try:
            await client.close()
        except Exception:
            pass
        report["used_tokens"] = budget.used
        report["calls"] = budget.calls
    # honest verdict: reuse only if the 2nd turn reports cached>0
    t2 = next((t for t in report["turns"] if t["label"] == "turn2"), None)
    report["cache_reuse_observed"] = bool(t2 and (t2.get("cached_tokens") or 0) > 0)
    return report


if __name__ == "__main__":
    print(json.dumps(run_task_correctness(), indent=2))
