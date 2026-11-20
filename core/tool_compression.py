"""Native, targeted compression of bulky tool-result text (spec C1).

Scope — deliberately narrow (see docs: SPEC.md §3):

- Only **tool-result text** is ever passed here. System, developer and user
  messages must never be routed through this module; the callers (runners)
  invoke it at the single point where a ``role: "tool"`` message is built.
- Compression is **lossy in the immediate model context** but the original is
  preserved by an ``ArtifactStore`` and referenced by a stable id, so every
  omitted byte is retrievable exactly.
- Compression is **not redaction**: secrets are not scrubbed here (and must not
  be claimed to be). This module only removes *repetition* and *bulk*, never
  hides sensitive content behind a false privacy promise.

Safety contract for output:

- Fail open: any internal error returns the input unchanged (``changed=False``
  … ``compact_fallback``). Never raises for pathological input.
- Never drop critical diagnostics: lines matching error/fatal/traceback/exit
  patterns are always retained **with adjacent context**; only clearly
  repetitive filler is elided, and every elision is marked with a count.
- Code, diffs and stack traces are treated conservatively: passthrough or
  reference-and-summary, never silent lossy rewording.
- Output is an annotated summary, never *fabricated* content that pretends to
  be the original file/JSON.

The public entry point is :func:`maybe_compress`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, Optional

# Optional import to avoid a hard dependency cycle; the store type is duck-typed.
try:  # pragma: no cover - typing only
    from core.artifacts import ArtifactRef, ArtifactStore
except Exception:  # pragma: no cover
    ArtifactRef = object  # type: ignore
    ArtifactStore = object  # type: ignore


# ---------------------------------------------------------------------------
# Configuration and outcome types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompressConfig:
    """Bounds and knobs. Advisories, not correctness settings."""

    # Only compress results at least this many characters (advisory threshold).
    min_chars: int = 4_000
    # Lines this long AND with very few spaces are "dense" (minified/base64).
    dense_line_min_chars: int = 300
    dense_line_max_space_ratio: float = 0.06
    # How many head/tail lines of a dense line to keep.
    dense_head_chars: int = 200
    dense_tail_chars: int = 200
    # Lines of context to keep around a matched error/fatal line.
    error_context: int = 3
    # Log collapsing: collapse runs of identical lines longer than this.
    collapse_run_threshold: int = 3
    # Hard ceiling on how many lines we will scan (bounded CPU).
    max_scan_lines: int = 100_000
    # Max representative records to keep for a JSON array summary.
    json_sample_records: int = 5


@dataclass(frozen=True)
class CompressionOutcome:
    """Result of a compression attempt. Never contains the original bytes."""

    text: str
    changed: bool
    transforms: tuple[str, ...] = ()
    original: Optional[object] = None  # ArtifactRef | None
    original_tokens_est: int = 0
    transmitted_tokens_est: int = 0


def _est_tokens(text: str) -> int:
    # Advisory only; the runners use ~3.5 chars/token. Kept independent so this
    # module has no import-time dependency on provider config.
    return max(1, int(len(text) / 3.5))


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

_ERROR_RE = re.compile(
    r"(?i)\b("
    r"error|fatal|critical|traceback|exception|panic|failed|failure|"
    r"segfault|denied|refused|timeout|timed out|abort|assert(?:ion)?(?:error)?"
    r")\b"
)
_EXIT_RE = re.compile(r"(?i)\[exit\s+-?\d+\]|\bexit(?:ed)?\s+(?:code|status)\s+\d+")
_TRACEBACK_START_RE = re.compile(r"^Traceback \(most recent call last\):")
# Unified-diff hunk header or file marker.
_DIFF_RE = re.compile(r"^(diff --git |--- |\+\+\+ |@@ )")


def _looks_like_traceback(text: str) -> bool:
    return bool(_TRACEBACK_START_RE.search(text)) or "\n  File \"" in text


def _looks_like_diff(text: str) -> bool:
    count = 0
    for line in text.splitlines()[:200]:
        if _DIFF_RE.match(line):
            count += 1
            if count >= 2:
                return True
    return False


def _looks_like_source(text: str) -> bool:
    # Heuristic: a high fraction of lines start with common code indentation or
    # keywords. Conservative — used only to *avoid* compressing source.
    code_markers = (
        "def ", "class ", "import ", "from ", "return ", "if ", "for ", "while ",
        "elif ", "else:", "try:", "except", "with ", "async def ", "await ",
        "function ", "const ", "let ", "var ", "public ", "private ", "fn ",
        "#include", "package ", "func ", "=>",
    )
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 20:
        return False
    hits = sum(
        1 for ln in lines[:500]
        if ln[:1] in (" ", "\t") or ln.lstrip().startswith(code_markers)
    )
    return hits / min(len(lines), 500) > 0.5


def _is_dense_line(line: str, cfg: CompressConfig) -> bool:
    if len(line) < cfg.dense_line_min_chars:
        return False
    spaces = line.count(" ")
    return (spaces / len(line)) <= cfg.dense_line_max_space_ratio


# ---------------------------------------------------------------------------
# Individual transforms (each returns (text, applied_name) or (None, None))
# ---------------------------------------------------------------------------

def _transform_dense_lines(text: str, cfg: CompressConfig) -> tuple[str, str] | tuple[None, None]:
    lines = text.split("\n")
    changed = False
    out: list[str] = []
    for line in lines:
        if _is_dense_line(line, cfg):
            changed = True
            n = len(line)
            head = line[: cfg.dense_head_chars]
            tail = line[-cfg.dense_tail_chars :] if n > cfg.dense_tail_chars else ""
            out.append(
                f"{head}…[{n - len(head) - len(tail)} chars elided]…{tail}"
            )
        else:
            out.append(line)
    if not changed:
        return None, None
    return "\n".join(out), "dense_line_elide"


def _transform_logs(text: str, cfg: CompressConfig) -> tuple[str, str] | tuple[None, None]:
    """Collapse repetitive filler while keeping all diagnostic lines + context."""
    lines = text.split("\n")
    if len(lines) > cfg.max_scan_lines:
        lines = lines[: cfg.max_scan_lines]
    n = len(lines)

    # Mark lines that must be preserved: diagnostics or adjacent to diagnostics.
    keep = [False] * n
    for i, line in enumerate(lines):
        if _ERROR_RE.search(line) or _EXIT_RE.search(line):
            lo = max(0, i - cfg.error_context)
            hi = min(n, i + cfg.error_context + 1)
            for j in range(lo, hi):
                keep[j] = True

    def _shape(line: str) -> str:
        # Normalize the repetitive *variable* regions so "INFO step 7 ok
        # latency=3ms" and "INFO step 8 ok latency=4ms" are the same shape.
        # No \b anchors: digits embedded in tokens like "=5ms" must normalize.
        s = re.sub(r"[0-9]+", "#", line)            # numbers (incl. 5ms, v1.2)
        s = re.sub(r"[0-9a-fA-F]{8,}", "#", s)      # hex ids / hashes
        return s

    out: list[str] = []
    i = 0
    elided = 0
    while i < n:
        if keep[i]:
            out.append(lines[i])
            i += 1
            continue
        # Group a run of same-shaped (kept=False) lines starting at i.
        shape = _shape(lines[i])
        j = i + 1
        while j < n and not keep[j] and _shape(lines[j]) == shape:
            j += 1
        run = j - i
        if run > cfg.collapse_run_threshold:
            # Keep the first and last of the run (last often has the latest
            # state/counters) and elide the middle with a count.
            out.append(lines[i])
            middle = run - 2
            if middle > 0:
                out.append(f"…[{middle} similar lines elided: {shape.strip()[:80]}]…")
                elided += middle
            out.append(lines[j - 1])
        else:
            out.extend(lines[i:j])
        i = j

    if elided == 0:
        return None, None
    return "\n".join(out), "log_collapse"


def _transform_json(text: str, cfg: CompressConfig) -> tuple[str, str, int] | tuple[None, None, int]:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None, None, 0
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None, None, 0

    if isinstance(data, list) and len(data) > cfg.json_sample_records * 2:
        # Keep a few representative records + any records that look like errors
        # or are otherwise unique/anomalous, plus a count.
        sample = data[: cfg.json_sample_records]
        anomalies = [
            r for r in data[cfg.json_sample_records :]
            if isinstance(r, dict)
            and any(
                _ERROR_RE.search(str(v))
                for v in r.values()
            )
        ]
        chosen = sample + anomalies[: cfg.json_sample_records]
        summary = {
            "_agentgate_summary": True,
            "_note": "JSON array compressed; original retrievable by id",
            "_original_item_count": len(data),
            "_retained_items": len(chosen),
            "items": chosen,
        }
        return json.dumps(summary, indent=2), "json_array_summary", len(data)

    if isinstance(data, dict) and len(stripped) > cfg.min_chars * 2:
        keys = list(data.keys())
        if len(keys) > 50:
            summary = {
                "_agentgate_summary": True,
                "_note": "large JSON object compressed; original retrievable by id",
                "_original_key_count": len(keys),
                "keys_sample": keys[:50],
            }
            return json.dumps(summary, indent=2), "json_object_summary", len(keys)

    return None, None, 0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def maybe_compress(
    text,
    *,
    tool_name: str = "",
    enabled: bool = False,
    config: Optional[CompressConfig] = None,
    store=None,  # ArtifactStore-like: put(session, workspace, bytes) -> ref
    session_id: str = "",
    workspace_id: str = "",
    retrieve_hint: Optional[Callable[[str], str]] = None,
) -> CompressionOutcome:
    """Compress a single tool-result string if enabled and eligible.

    Returns a :class:`CompressionOutcome`. On any error or ineligibility the
    original text is returned unchanged (``changed=False``). Never raises.
    """
    cfg = config or CompressConfig()

    # Only genuine text tool results are eligible. Anything else (None, bytes,
    # dict/list objects, numbers) is passed through untouched.
    if not isinstance(text, str):
        return CompressionOutcome(text=str(text) if text is not None else "", changed=False)
    if not enabled:
        return CompressionOutcome(text=text, changed=False)
    if len(text) < cfg.min_chars:
        return CompressionOutcome(text=text, changed=False)

    try:
        return _compress_impl(
            text, tool_name=tool_name, cfg=cfg, store=store,
            session_id=session_id, workspace_id=workspace_id,
            retrieve_hint=retrieve_hint,
        )
    except Exception:
        # Fail open: never let compression break a tool turn.
        return CompressionOutcome(text=text, changed=False, transforms=("compact_fallback",))


def _compress_impl(
    text: str, *, tool_name: str, cfg: CompressConfig, store,
    session_id: str, workspace_id: str, retrieve_hint,
) -> CompressionOutcome:
    transforms: list[str] = []
    working = text

    # 1. Structured JSON summary (only when it actually parses as JSON).
    jtext, jname, _jcount = _transform_json(working, cfg)
    if jname:
        working = jtext
        transforms.append(jname)

    # 2. Dense-line elision (minified/base64) — safe, structural.
    if jname is None:
        dtext, dname = _transform_dense_lines(working, cfg)
        if dname:
            working = dtext
            transforms.append(dname)

    # 3. Code / diff / traceback: conservative. Do NOT chew source; for these,
    #    only dense-line elision (above) is allowed, never log/json mangling.
    is_codey = _looks_like_source(text) or _looks_like_diff(text) or _looks_like_traceback(text)

    if not is_codey and jname is None:
        ltext, lname = _transform_logs(working, cfg)
        if lname:
            working = ltext
            transforms.append(lname)
    elif _looks_like_traceback(text) and len(working) > cfg.min_chars:
        # Tracebacks: keep the tail (final exception) and head, elide the middle
        # of the repeated frames, but never drop the terminal error line.
        head, tail = _traceback_head_tail(working)
        if tail is not None:
            working = head + "\n…[traceback frames elided]…\n" + tail
            transforms.append("traceback_frames")

    if not transforms or working == text:
        return CompressionOutcome(
            text=text, changed=False,
            original_tokens_est=_est_tokens(text),
            transmitted_tokens_est=_est_tokens(text),
        )

    # Preserve the ORIGINAL via the store; annotate the summary with the handle.
    original_ref = None
    handle = ""
    if store is not None:
        try:
            ref = store.put(session_id, workspace_id, text.encode("utf-8", "surrogatepass"))
            original_ref = ref
            hint = (retrieve_hint(ref.artifact_id) if retrieve_hint
                    else f"tool_result_read('{ref.artifact_id}', start, limit)")
            handle = (
                f"\n\n[AgentGate compressed tool result | original={ref.artifact_id} | "
                f"original_chars={len(text):,} | emitted_chars={len(working):,} | "
                f"transforms={'+'.join(transforms)} | retrieve={hint}]"
            )
        except Exception:
            # If we cannot persist the original, do NOT ship lossy output under
            # a false retrieval promise. Fall back to the original.
            return CompressionOutcome(
                text=text, changed=False, transforms=("store_unavailable",),
                original_tokens_est=_est_tokens(text),
                transmitted_tokens_est=_est_tokens(text),
            )
    else:
        # No store: we may only emit lossy output if there is no omission, but
        # all our transforms elide — so refuse to change. (Safety over savings.)
        return CompressionOutcome(
            text=text, changed=False, transforms=("no_store",),
            original_tokens_est=_est_tokens(text),
            transmitted_tokens_est=_est_tokens(text),
        )
    final = working + handle
    return CompressionOutcome(
        text=final,
        changed=True,
        transforms=tuple(transforms),
        original=original_ref,
        original_tokens_est=_est_tokens(text),
        transmitted_tokens_est=_est_tokens(final),
    )


def _traceback_head_tail(text: str) -> tuple[str, Optional[str]]:
    """Keep the header + terminal exception line; elide repeated frames."""
    lines = text.split("\n")
    # Terminal exception is the last non-empty line that isn't a frame.
    tail_line = ""
    for line in reversed(lines):
        if line.strip():
            tail_line = line
            break
    head = "\n".join(lines[:3])
    return head, tail_line or None
