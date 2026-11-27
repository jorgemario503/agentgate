"""Maintainer-managed guidance: one configured record, delivered when it applies.

A maintainer publishes a short instruction as an ordinary AgentDB record and maps
an **exact repository + task type** to that record's key. When a turn's scope
matches, the instruction rides the existing ephemeral context for that turn and
nothing else. This is deliberately not retrieval: there is no query, no ranking,
and no discovery — the mapping is configuration, and the record is named.

Why a configured key rather than a search
-----------------------------------------
The spec for the first slice is explicit: reuse current AgentDB records and APIs
for a known-record workflow, and defer query discovery. A lookup that cannot be
engineered makes a much stronger guarantee than one that can — the maintainer knows
which instruction applies, and the host asserts it rather than hoping a ranker
finds it.

Applicability is decided here, in the host
------------------------------------------
AgentGate resolves repository, task type and caller from trusted runtime state and
requires the record's own scope to match exactly. AgentDB enforces its read policy.
Both must agree before anything is delivered; anything else yields no guidance.

Withdrawal is data, not deletion-policy
---------------------------------------
A maintainer withdraws guidance by removing the mapping (or repointing it), which
happens on the next request because the selection is resolved every turn. An
archived or unreadable record also yields nothing. Guidance is never remembered
between turns: the resolved instruction is not written back into conversation
history, so a later scope switch or resume cannot replay a stale copy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

#: Heading used when a managed instruction is injected. Short and imperative, so
#: the model treats it as a task instruction rather than reference material.
MANAGED_GUIDANCE_HEADING = "## Repository guidance for this task"


@dataclass(frozen=True)
class ManagedGuidanceSelection:
    """One configured mapping entry: exact scope -> one record key."""

    repository_id: str
    task_type: str
    record_key: str

    def __post_init__(self) -> None:
        for name in ("repository_id", "task_type", "record_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"managed guidance {name} must be a non-empty string; got {value!r}"
                )


@dataclass(frozen=True)
class ResolvedScope:
    """The turn's applicability, as the host resolved it.

    Both fields are required for guidance to be considered at all. An unresolved
    scope is not "match anything" — it is "no guidance", because a maintainer's
    instruction is only meaningful for the repository and task it was written for.
    """

    repository_id: Optional[str]
    task_type: Optional[str]
    agent_id: Optional[str]

    @property
    def resolved(self) -> bool:
        return bool(self.repository_id and self.task_type)


def select_for(
    selections: tuple[ManagedGuidanceSelection, ...],
    scope: ResolvedScope,
) -> Optional[ManagedGuidanceSelection]:
    """The single selection whose scope matches exactly, or ``None``.

    Exact match on both fields: a missing or different repository or task type is
    not a match. More than one matching entry is ambiguous, and ambiguity yields
    nothing rather than an arbitrary choice — the maintainer configured a mapping
    that is supposed to be unambiguous.
    """
    if not scope.resolved:
        return None
    matches = [
        s for s in selections
        if s.repository_id == scope.repository_id and s.task_type == scope.task_type
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def record_applies(record: Any, scope: ResolvedScope) -> bool:
    """True when the *record's own* scope matches the turn's, exactly.

    The mapping naming a key is not sufficient on its own: a record can be
    re-scoped after the mapping was written, and the instruction then belongs to a
    different repository or task. Both the mapping and the record must agree.
    """
    if record is None or getattr(record, "archived", False):
        return False
    if getattr(record, "repository_id", None) != scope.repository_id:
        return False
    if getattr(record, "task_type", None) != scope.task_type:
        return False
    return True


def instruction_text(record: Any) -> str:
    """The instruction as text, or ``""`` when there is nothing to send."""
    value = getattr(record, "value", None)
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text.strip()


@dataclass(frozen=True)
class ManagedGuidance:
    """The outcome of resolving guidance for one turn."""

    record_key: str
    text: str


def resolve_managed_guidance(
    *,
    selections: tuple[ManagedGuidanceSelection, ...],
    scope: ResolvedScope,
    select_unique: Any,
) -> Optional[ManagedGuidance]:
    """Resolve at most one instruction for this turn, or ``None``.

    ``select_unique(key, agent_id=...)`` is expected to be ``AgentDB.select_unique``
    — the sole live, readable record for the key, or ``None``. Passing a lookup that
    resolves uniqueness and the record together is what makes "the maintainer's
    record" well defined: a plain ``read()`` returns an arbitrary sibling when a
    repeat write has left several.

    Every failure mode in the spec lands on the same result, deliberately:
    unresolved scope, no mapping, an ambiguous mapping, a missing record, a scope
    mismatch, a denied read, or a withdrawal. A turn without applicable guidance
    is normal, so none of these is exceptional.
    """
    selection = select_for(selections, scope)
    if selection is None:
        return None

    try:
        # One authorized, committed lookup: the sole live record for the key that
        # this caller may read. ``None`` covers absent, ambiguous and denied, which
        # are the same answer to "is there exactly one record I may use here?".
        record = select_unique(selection.record_key, agent_id=scope.agent_id)
    except Exception:
        # A missing or older API, or any read failure: no guidance, no turn failure.
        return None

    if not record_applies(record, scope):
        return None

    text = instruction_text(record)
    if not text:
        return None
    return ManagedGuidance(record_key=selection.record_key, text=text)


def parse_selections(raw: str) -> tuple[ManagedGuidanceSelection, ...]:
    """Parse ``repository:task_type:record_key`` entries, comma separated.

    Example: ``komper:bug_fix:memory/guidance/komper/bug-fix``

    A malformed entry is skipped rather than aborting startup — guidance is
    optional, and a typo in one mapping should not stop the bot. The caller
    reports the skipped entries, the way ``ADDITIONAL_DIRS`` does.
    """
    selections: list[ManagedGuidanceSelection] = []
    for chunk in (raw or "").split(","):
        entry = chunk.strip()
        if not entry:
            continue
        parts = entry.split(":", 2)
        if len(parts) != 3 or not all(p.strip() for p in parts):
            continue
        repository_id, task_type, record_key = (p.strip() for p in parts)
        selections.append(ManagedGuidanceSelection(
            repository_id=repository_id, task_type=task_type, record_key=record_key,
        ))
    return tuple(selections)


def strip_guidance_lines(
    parts: "list[str]", record_key: str,
) -> "list[str]":
    """Remove a managed record's line from recalled memory.

    Generic recall renders each record as ``- <key>: <value>``. A managed
    instruction must reach the model through exactly one path — the configured
    selection — so any occurrence of it in recalled memory is removed here. Without
    this the same instruction can arrive twice, or arrive through recall on a turn
    where its scope does not apply.

    Matching is on the record key, not the value: the value is the instruction text,
    which may legitimately appear in other records.
    """
    if not record_key:
        return parts
    marker = f"- {record_key}:"
    kept: list[str] = []
    for part in parts:
        if not isinstance(part, str) or marker not in part:
            kept.append(part)
            continue
        lines = [ln for ln in part.splitlines() if marker not in ln]
        remaining = "\n".join(lines).strip()
        if remaining:
            kept.append(remaining)
    return kept
