"""Prefix-cache stability harness (H13, stage 1: offline).

The provider prefix cache matches the *serialized request* from the start and
discards the cached prefix on the first differing byte. This harness measures,
for a multi-turn session under each variant, how many **leading messages stay
byte-identical** turn-over-turn — i.e. the reusable prefix — and the token
fraction that represents.

It does NOT claim provider cached-token numbers. It produces the offline,
deterministic part (prefix byte stability + reusable-prefix token estimate) and
exposes a seam (`record_provider_usage`) so a live run can attach real
`cached_tokens` without changing the measurement code.
"""
import json
from dataclasses import dataclass, field
from typing import Optional

from core.tool_pipeline import ToolResultPipeline
from core.tool_compression import CompressConfig


def _est_tokens(obj) -> int:
    return max(1, int(len(_serialize(obj)) / 3.5))


def _serialize(messages: list[dict]) -> str:
    """Deterministic serialization approximating the wire body's message list."""
    return json.dumps(messages, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass
class TurnSnapshot:
    turn: int
    messages: list[dict]
    serialized: str
    prefix_tokens: int          # est tokens of the byte-stable leading prefix


@dataclass
class CacheReport:
    variant: str
    turns: list[TurnSnapshot] = field(default_factory=list)
    # per turn (from turn 1 on): 0..1 fraction of the prior request that is a
    # byte-identical reusable prefix
    prefix_reuse_fraction: list[float] = field(default_factory=list)
    notes: str = ""
    provider_cached_tokens: Optional[list[int]] = None  # filled by live run only


def _leading_identical_count(prev: list[dict], cur: list[dict]) -> int:
    """Number of leading messages (by exact serialized bytes) shared byte-for-byte."""
    n = 0
    for a, b in zip(prev, cur):
        if _serialize(a) == _serialize(b):
            n += 1
        else:
            break
    return n


def _simulate_session(pipe, tool_script: list[str], *, user_turns: int) -> list[list[dict]]:
    """Build the outgoing message list at each turn for a tiny agent loop.

    Mirrors the real runners: on each user turn we append a user message, then
    for each scripted tool result we append an assistant tool_call + the tool
    message obtained from `pipe.process(...)`. Returns the message list snapshot
    *after each user turn's tool exchange* (i.e. what would be sent next).
    """
    messages: list[dict] = []
    snapshots: list[list[dict]] = []
    ti = 0
    for t in range(user_turns):
        messages.append({"role": "user", "content": f"turn {t}"})
        if ti < len(tool_script):
            raw = tool_script[ti]
            ti += 1
            call_id = f"call{t}"
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [{"id": call_id, "type": "function",
                                "function": {"name": "Bash", "arguments": "{}"}}],
            })
            processed = pipe.process(raw, tool_name="Bash")
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": processed})
        snapshots.append([dict(m) for m in messages])
    return snapshots


def measure_prefix_stability(
    tool_script: list[str],
    *,
    enabled: bool,
    dedup_enabled: bool,
    user_turns: Optional[int] = None,
    variant: str = "",
    min_chars: int = 1000,
) -> CacheReport:
    """Measure leading-prefix byte stability across turns for one variant."""
    import tempfile
    user_turns = user_turns or max(1, len(tool_script))
    rep = CacheReport(variant=variant)
    with tempfile.TemporaryDirectory() as d:
        pipe = ToolResultPipeline(
            d, session_id="h13", workspace_id="/ws",
            enabled=enabled, dedup_enabled=dedup_enabled, min_chars=min_chars,
            compress_config=CompressConfig(min_chars=min_chars),
        )
        snaps = _simulate_session(pipe, tool_script, user_turns=user_turns)
        for i, msgs in enumerate(snaps):
            rep.turns.append(TurnSnapshot(
                turn=i, messages=msgs, serialized=_serialize(msgs),
                prefix_tokens=_est_tokens(msgs),
            ))
        for i in range(1, len(snaps)):
            prev, cur = snaps[i - 1], snaps[i]
            k = _leading_identical_count(prev, cur)
            prior_tokens = _est_tokens(prev) or 1
            prefix_tokens = _est_tokens(prev[:k]) if k else 0
            rep.prefix_reuse_fraction.append(round(prefix_tokens / prior_tokens, 3))
    return rep


def record_provider_usage(rep: CacheReport, cached_tokens: list[int]) -> CacheReport:
    """Attach real provider `cached_tokens` from a live run (H13, stage 2).

    Stage 1 (offline) never calls this. A live harness passes the per-turn
    cached-token counts the provider reports, so the same report object carries
    measured data alongside the offline estimates.
    """
    rep.provider_cached_tokens = list(cached_tokens)
    return rep


def run_h13(tool_script: Optional[list[str]] = None) -> dict:
    """Compare prefix reuse for baseline vs native variants on one script."""
    if tool_script is None:
        # repeated identical reads: the case where dedup could tempt a
        # retroactive rewrite (it must not) and compression tests prefix stability
        base = "file line contents\n" * 400
        tool_script = [base, base, base, "different output\n" * 400]

    A = measure_prefix_stability(tool_script, enabled=False, dedup_enabled=False, variant="A baseline")
    B = measure_prefix_stability(tool_script, enabled=True, dedup_enabled=False, variant="B C1")
    C = measure_prefix_stability(tool_script, enabled=True, dedup_enabled=True, variant="C C1+C2")

    return {
        "script_len": len(tool_script),
        "prefix_reuse_fraction": {
            "A": A.prefix_reuse_fraction,
            "B": B.prefix_reuse_fraction,
            "C": C.prefix_reuse_fraction,
        },
        "all_prefixes_stable": all(
            f == 1.0 for rep in (A, B, C) for f in rep.prefix_reuse_fraction
        ),
        "measured": "offline: leading-message byte stability; no provider claims",
        "reports": {"A": A, "B": B, "C": C},
    }
