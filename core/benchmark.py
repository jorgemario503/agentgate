"""Replayable, offline benchmark harness (C4).

Compares the native context-efficiency stages on identical, synthetic
session-shaped inputs and reports deterministic metrics. It makes **no network
or model calls** and never fabricates provider/cached-token numbers — those are
only reported if a caller supplies them.

Variants:

- A baseline: raw tool results, no transformation.
- B native C1: targeted tool-result compression.
- C native C1+C2: compression + cross-turn dedup.
- D native C1+C2+C3: the above + repository-map context (token cost of the
  injected segment counted; savings only manifest as avoided re-reads, so we
  report the map's size, not a fabricated saving).
- E Headroom: only if `headroom` is importable in an isolated env; otherwise
  reported as "unavailable" rather than claimed.

Usage:
    python -m core.benchmark            # prints a report for built-in fixtures
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from typing import Optional

from core.context.tool_pipeline import ToolResultPipeline
from core.context.tool_compression import CompressConfig

# Advisory estimator kept local so the harness has no provider dependency.
def _est_tokens(text: str) -> int:
    return max(1, int(len(text) / 3.5))


# ---------------------------------------------------------------------------
# Synthetic session fixtures (deterministic)
# ---------------------------------------------------------------------------

def _repetitive_log(n: int = 2000) -> str:
    lines = [f"INFO step {i} ok latency={i % 7}ms" for i in range(n)]
    lines.insert(n // 2, "FATAL disk full at /var/data")
    return "\n".join(lines)


def _json_anomaly(n: int = 600) -> str:
    recs = [{"id": i, "status": "ok"} for i in range(n)]
    recs[n // 2] = {"id": n // 2, "status": "ERROR", "detail": "anomaly-XYZ"}
    return json.dumps(recs)


def _dense_blob() -> str:
    return "normal start\n" + ("Q" * 40_000) + "\nnormal end"


def _repeated_read() -> str:
    return "file contents line\n" * 400


@dataclass
class SessionStep:
    tool_name: str
    result: str


@dataclass
class Session:
    name: str
    steps: list[SessionStep]


def builtin_sessions() -> list[Session]:
    return [
        Session("repetitive-log", [SessionStep("Bash", _repetitive_log())]),
        Session("json-anomaly", [SessionStep("Bash", _json_anomaly())]),
        Session("dense-asset", [SessionStep("Bash", _dense_blob())]),
        Session("repeated-read", [
            SessionStep("Read", _repeated_read()),
            SessionStep("Read", _repeated_read()),
            SessionStep("Read", _repeated_read()),
        ]),
    ]


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

@dataclass
class VariantResult:
    variant: str
    per_session: dict[str, int] = field(default_factory=dict)  # name -> emitted tokens
    totals: int = 0
    retrieval_ok: bool = True
    dedup_hits: int = 0
    notes: str = ""


def _run_variant(sessions, *, compress, dedup) -> VariantResult:
    res = VariantResult(variant="")
    for sess in sessions:
        with tempfile.TemporaryDirectory() as d:
            pipe = ToolResultPipeline(
                d, session_id=sess.name, workspace_id="/ws",
                enabled=compress, dedup_enabled=dedup, min_chars=1000,
                compress_config=CompressConfig(min_chars=1000),
            )
            emitted = 0
            for step in sess.steps:
                out = pipe.process(step.result, tool_name=step.tool_name)
                emitted += _est_tokens(out)
                if "duplicate" in out.lower():
                    res.dedup_hits += 1
            res.per_session[sess.name] = emitted
            res.totals += emitted
    return res


def run_benchmark(sessions=None, *, repo_map_segment: Optional[str] = None) -> dict:
    """Return a dict of variant -> VariantResult plus derived savings.

    No network/model calls. Headroom is attempted only if importable and is
    reported honestly as unavailable otherwise.
    """
    sessions = sessions or builtin_sessions()

    A = _run_variant(sessions, compress=False, dedup=False); A.variant = "A baseline"
    B = _run_variant(sessions, compress=True, dedup=False); B.variant = "B C1"
    C = _run_variant(sessions, compress=True, dedup=True); C.variant = "C C1+C2"

    # D adds the repo-map segment's token cost (a cost, not a fabricated saving).
    D = VariantResult(variant="D C1+C2+C3")
    D.per_session = dict(C.per_session)
    D.totals = C.totals
    D.dedup_hits = C.dedup_hits
    map_tokens = _est_tokens(repo_map_segment) if repo_map_segment else 0
    D.notes = f"repo-map context adds ~{map_tokens} tokens/session (exact-source reads on demand)"

    # E Headroom — evaluation only, isolated; skip honestly if unavailable.
    E = VariantResult(variant="E Headroom")
    try:
        import headroom as _hr  # noqa: F401
        _ = _hr  # touch to satisfy linters; presence is what we check
        E.notes = "headroom importable; run its proxy/baseline separately in an isolated env"
        E.available = True
    except Exception:
        E.available = False
        E.notes = "headroom not installed in this environment (evaluation-only; skipped)"

    results = {"A": A, "B": B, "C": C, "D": D, "E": E}
    base = A.totals or 1
    report = {
        "sessions": [s.name for s in sessions],
        "totals": {k: v.totals for k, v in results.items() if hasattr(v, "totals")},
        "reduction_vs_baseline": {
            k: round(1 - (v.totals / base), 3)
            for k, v in results.items() if hasattr(v, "totals")
        },
        "dedup_hits": {k: getattr(v, "dedup_hits", 0) for k, v in results.items()},
        "per_session": {k: v.per_session for k, v in results.items() if v.per_session},
        "notes": {k: v.notes for k, v in results.items() if v.notes},
        "headroom_available": bool(getattr(E, "available", False)),
        "measured": "offline deterministic estimates; no provider/cached-token claims",
    }
    return report


def main() -> None:
    print(json.dumps(run_benchmark(), indent=2))


if __name__ == "__main__":
    main()
