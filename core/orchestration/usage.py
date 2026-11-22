"""Normalized provider usage records.

Why this exists
---------------
The three open-model runners previously discarded the provider's ``usage`` block:
``deepinfra`` and ``fireworks`` build a plain dict from the completion object and
``local`` parses the raw JSON response — all of which contain ``usage`` — but only
``_record_reasoning`` was ever read from it. Without a normalized record there is no
way to attribute token consumption to a role or attempt, and no way to test the D1
hypothesis (do persistent sessions actually preserve cache hits?).

Design invariants
-----------------
- **Missing is ``None``, never ``0``.** Zero is a *measured* value; absence is not. A
  provider that omits ``usage`` must be distinguishable from one that reports zero,
  otherwise every rollup silently under-reports.
- **Never raise into a turn.** :func:`safe_emit` swallows and logs sink failures. A
  telemetry problem must not cost a user their answer.
- **Dependency-free.** This module imports nothing from ``runners/`` or ``bots/``; the
  package-level guard test enforces that (DEC-108).
- **No prompt text, ever.** Only counts, identifiers and timing. Telemetry must never
  become a second copy of the conversation.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

#: Provider usage field -> UsageRecord field. The first name is the key that actually
#: appears in an OpenAI-compatible ``usage`` block; the second is our attribute name.
_USAGE_KEYS = (
    ("prompt_tokens", "prompt_tokens"),
    ("completion_tokens", "completion_tokens"),
    ("total_tokens", "total_tokens"),
)

#: Cached-prompt counters live under ``prompt_tokens_details``.
_DETAIL_KEYS = (
    ("cached_tokens", "cached_tokens"),
    ("cache_write_tokens", "cache_write_tokens"),
)


@dataclass(frozen=True)
class UsageRecord:
    """One provider response's consumption, attributed to a caller context.

    Every counting field is ``Optional[int]``: ``None`` means *the provider did not
    report it*, which is materially different from ``0`` (reported as zero).
    """

    runner: str
    model_id: str
    session_key: str
    outcome: str = "ok"
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    ts_start: Optional[float] = None
    ts_end: Optional[float] = None
    error: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @property
    def has_provider_usage(self) -> bool:
        """True when the provider reported *any* token count."""
        return any(
            getattr(self, f) is not None
            for f in ("prompt_tokens", "completion_tokens", "total_tokens",
                      "cached_tokens", "cache_write_tokens")
        )


def _as_int(value: Any) -> Optional[int]:
    """Coerce to a non-negative int, or None. Never invents a zero."""
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer():
        return int(value) if value >= 0 else None
    return None


def normalize_usage(
    data: Any,
    *,
    runner: str,
    model_id: str,
    session_key: str,
    outcome: str = "ok",
    error: Optional[str] = None,
    ts_start: Optional[float] = None,
    ts_end: Optional[float] = None,
) -> UsageRecord:
    """Project a provider response dict into a :class:`UsageRecord`.

    Accepts the OpenAI-compatible shape (``{"usage": {...}}``) as produced by
    ``model_dump()`` / ``to_dict()`` / ``response.json()``. A response with no usable
    usage block yields a record whose counts are all ``None`` — that is the correct,
    honest result, not an error.
    """
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        usage = {}

    values: dict[str, Optional[int]] = {}
    for field, key in _USAGE_KEYS:
        values[field] = _as_int(usage.get(key))

    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = {}
    for field, key in _DETAIL_KEYS:
        values[field] = _as_int(details.get(key))

    return UsageRecord(
        runner=runner,
        model_id=model_id,
        session_key=session_key,
        outcome=outcome,
        ts_start=ts_start,
        ts_end=ts_end,
        error=error,
        **values,
    )


#: A sink receives one record per provider response.
UsageSink = Callable[[UsageRecord], None]


def safe_emit(sink: Optional[UsageSink], record: UsageRecord) -> None:
    """Deliver ``record`` to ``sink``, never raising into the caller's turn.

    Telemetry is best-effort by design: a broken sink (disk full, closed file, user
    callback bug) must not surface as a failed model turn.
    """
    if sink is None:
        return
    try:
        sink(record)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, telemetry only
        log.warning("usage sink failed (%s): %s", type(exc).__name__, exc)
