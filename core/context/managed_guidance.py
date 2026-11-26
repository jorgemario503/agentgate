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
    read_record: Any,
    count_live_by_key: Any = None,
) -> Optional[ManagedGuidance]:
    """Resolve at most one instruction for this turn, or ``None``.

    ``read_record(key, agent_id=...)`` is expected to be ``AgentDB.read`` — it
    raises when the caller may not read the record, which is treated as "no
    guidance" rather than an error the turn must survive.

    Every failure mode in the spec lands on the same result, deliberately:
    unresolved scope, no mapping, an ambiguous mapping, a missing record, a scope
    mismatch, a denied read, or a withdrawal. A turn without applicable guidance
    is normal, so none of these is exceptional.
    """
    selection = select_for(selections, scope)
    if selection is None:
        return None

    try:
        record = read_record(selection.record_key, agent_id=scope.agent_id)
    except Exception:
        # Denied read, or any other read failure: no guidance, no turn failure.
        return None

    # A repeat write leaves sibling records under one key, and ``read()`` returns
    # whichever it encounters first. If more than one live record shares the key,
    # the selection is ambiguous: the maintainer's intent cannot be established, so
    # nothing is sent rather than an arbitrary one of them.
    if count_live_by_key is not None:
        try:
            if count_live_by_key(selection.record_key, agent_id=scope.agent_id) != 1:
                return None
        except Exception:
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

    A malformed entry is skipped with its reason rather than aborting startup —
    guidance is optional, and a typo in one mapping should not stop the bot. The
    caller reports the skipped entries, the way ``ADDITIONAL_DIRS`` does.
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
