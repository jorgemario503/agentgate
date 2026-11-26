"""What gets injected into a turn, who owns it, and when.

A turn's context is assembled from more than one producer. AgentGate contributes
things it knows about the environment (where the session starts, what repository
it is in). AgentDB contributes governed memory (recalled records, workspace
snapshots, compaction artefacts). Before this module they were assembled inline
in each of the three open-model runners, with two consequences:

1. **They overwrote each other.** The assembly used one ``memory_context``
   variable, so whichever producer ran last won. With AgentDB enabled the repo map
   was silently discarded; with it disabled the map was sent on every turn. Both
   wrong, neither visible.

2. **Scope was implicit.** Whether a block belonged to a session boundary or to
   every turn was decided by where the code happened to sit, not by anything a
   reviewer could check.

This module makes both explicit. Producers emit :class:`Injection` values, each
declaring its ``source`` (ownership), ``asset`` (what it is) and ``scope`` (when
it applies). :class:`TurnInjections` collects them, filters by the turn's
:class:`~core.sessions.SessionState`, and composes them under their own headings.
Nothing is merged, reordered or dropped silently.

Scope vocabulary
----------------
``SESSION_START`` / ``SESSION_RESUME`` / ``SESSION_BOUNDARY`` / ``EVERY_TURN``

A *boundary* turn is the first turn of a session — either starting one or
resuming one. It is the only turn where per-session context is worth sending:
the text does not change while the session stays open, and resending it is both
wasted tokens and a re-scan of the repository on every message.

Prefix-cache note
-----------------
Composition happens into the *ephemeral* context, which the open-model runners
insert immediately before the last user message. It never touches
``messages[0]``. That matters because the provider cache key is derived from the
conversation address, which is permanent for Telegram and WhatsApp — per-session
text in the system prompt would mutate a long-lived cache lineage on every
session start. See ``core.context.cache_key`` and ``core.sessions``.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Literal, Optional

from core.sessions import SessionState


class InjectionScope(Enum):
    """When an injection applies.

    ``SESSION_BOUNDARY`` is the convenient default for per-session context; the
    two finer scopes exist so a producer can say "only on resume" without
    inventing a new predicate downstream.
    """

    SESSION_START = "session_start"
    SESSION_RESUME = "session_resume"
    SESSION_BOUNDARY = "session_boundary"
    EVERY_TURN = "every_turn"
    #: Declared but never injected. For records that exist in the store and can
    #: be retrieved, but carry no content worth sending — saying so in the table
    #: is clearer than dropping them without a declaration.
    NEVER = "never"

    def applies(self, state: SessionState) -> bool:
        """Whether this scope covers a turn in ``state``."""
        if self is InjectionScope.NEVER:
            return False
        if self is InjectionScope.EVERY_TURN:
            return True
        if state is SessionState.SESSION_WARM:
            # Inside an open session: only per-turn injections apply.
            return False
        if self is InjectionScope.SESSION_BOUNDARY:
            return True
        if self is InjectionScope.SESSION_START:
            return state is SessionState.SESSION_START
        return state is SessionState.SESSION_RESUME


#: Ownership, so a block's origin is never ambiguous in a transcript or a test.
Source = Literal["agentgate", "agentdb"]

#: What each known asset is, and when it should be injected.
#:
#: This table is the reviewable answer to "what gets injected, and when?". A
#: producer that is not declared here still works (its :class:`Injection` carries
#: its own scope) — the table documents the agreed configuration so drift is a
#: diff, not a surprise.
#:
#: AgentDB's assets are split by what each record *describes*, not by which call
#: returned it. A single retrieval pass mixes all four families, so scope has to
#: be attached to the record, never to the call.
ASSET_SCOPES: dict[str, InjectionScope] = {
    # AgentGate: environment knowledge and per-turn hints.
    "orientation": InjectionScope.SESSION_BOUNDARY,
    "repo_map": InjectionScope.SESSION_BOUNDARY,
    "skills_nudge": InjectionScope.EVERY_TURN,
    # AgentDB. `memory_records` is what makes a turn's retrieval worth doing: a
    # record recalled *for this question*. The other three describe the session
    # or the workspace, so they belong to the boundary.
    "memory_records": InjectionScope.EVERY_TURN,
    # Maintainer-managed guidance: one configured record per repository+task.
    # Resolved on every request, never cached, so a withdrawal or scope switch
    # takes effect on the next turn.
    "managed_guidance": InjectionScope.EVERY_TURN,
    "compaction_summary": InjectionScope.SESSION_RESUME,
    "compaction_checkpoint": InjectionScope.NEVER,
    "workspace": InjectionScope.SESSION_BOUNDARY,
    "work_context": InjectionScope.EVERY_TURN,
}


class MemoryAsset(Enum):
    """The families of record an AgentDB retrieval can return.

    Derived from the record *key*, which is the only stable classifier: the
    store's ``kind`` column says ``source`` for every row, so it cannot tell a
    compaction checkpoint from an episodic fact.

    The store writes four families (verified against a real database):
    ``memory/episodic/<runtime>/<agent>/<sid>/...``,
    ``memory/compaction/checkpoint/<sid>``,
    ``memory/compaction/summary/<sid>``, and
    ``memory/canonical/...``.
    """

    #: Records written about *this* session's act of working: tool traces,
    #: directory snapshots, per-turn observations. Session-local by
    #: construction — AgentDB already refuses cross-session episodic records.
    EPISODIC = "episodic"
    #: A tombstone marking that a session compacted. Carries no content.
    CHECKPOINT = "checkpoint"
    #: The handover text written at compaction. Content, and it describes the
    #: session's past, so it is a resume asset — not a per-turn one.
    COMPACTION_SUMMARY = "compaction_summary"
    #: Derived, validated memory (a canonical consolidates sources).
    CANONICAL = "canonical"
    #: Anything unrecognised. Treated as per-turn: it came from a query.
    OTHER = "other"


#: Substring -> family. Ordered: the specific prefixes before the general ones,
#: because ``memory/compaction/checkpoint/`` is also matched by ``compaction``.
_MEMORY_ASSET_MARKERS: tuple[tuple[str, MemoryAsset], ...] = (
    ("memory/compaction/checkpoint/", MemoryAsset.CHECKPOINT),
    ("memory/compaction/summary/", MemoryAsset.COMPACTION_SUMMARY),
    ("memory/compaction/", MemoryAsset.COMPACTION_SUMMARY),
    ("memory/canonical/", MemoryAsset.CANONICAL),
    ("memory/episodic/", MemoryAsset.EPISODIC),
    # Directory snapshots are episodic records, and AgentDB already filters them
    # from injection. Kept explicit so the classification is total.
    ("/directory_tracking/", MemoryAsset.EPISODIC),
)

#: Which asset each family is declared under in :data:`ASSET_SCOPES`.
_ASSET_FOR_FAMILY: dict[MemoryAsset, str] = {
    MemoryAsset.EPISODIC: "memory_records",
    MemoryAsset.CANONICAL: "memory_records",
    MemoryAsset.OTHER: "memory_records",
    MemoryAsset.COMPACTION_SUMMARY: "compaction_summary",
    MemoryAsset.CHECKPOINT: "compaction_checkpoint",
}


def memory_asset_of(key: str) -> MemoryAsset:
    """Classify a memory record by its key. Unrecognised keys are ``OTHER``."""
    text = key or ""
    for marker, family in _MEMORY_ASSET_MARKERS:
        if marker in text:
            return family
    return MemoryAsset.OTHER


def is_checkpoint_record(key: str) -> bool:
    """A compaction tombstone: no content, so never worth injecting.

    ``checkpoint`` records store only ``{"session_id": ..., "type": "checkpoint"}``
    — verified against a real store, where all 16 checkpoint rows were pure
    stubs. Injecting them costs a line each and tells the model nothing.
    """
    return memory_asset_of(key) is MemoryAsset.CHECKPOINT


def scope_of_memory_record(key: str) -> InjectionScope:
    """The scope a single memory record belongs to."""
    return ASSET_SCOPES[_ASSET_FOR_FAMILY[memory_asset_of(key)]]


def is_session_scoped_record(key: str) -> bool:
    """True for memory records that describe a session, not a question.

    Checkpoints and compaction summaries both do: one is a tombstone, the other
    is the session's handover note. Neither answers *this* turn's question.
    """
    return memory_asset_of(key) in (MemoryAsset.CHECKPOINT, MemoryAsset.COMPACTION_SUMMARY)



@dataclass(frozen=True)
class Injection:
    """One producer's contribution to a turn's context."""

    source: Source
    asset: str
    heading: str
    body: str
    scope: InjectionScope

    def __post_init__(self) -> None:
        # A declared scope that contradicts the table would make ASSET_SCOPES a
        # lie, so it is rejected at construction rather than silently obeyed.
        declared = ASSET_SCOPES.get(self.asset)
        if declared is not None and declared is not self.scope:
            raise ValueError(
                f"asset {self.asset!r} is declared {declared.value} in ASSET_SCOPES "
                f"but was constructed with {self.scope.value}"
            )

    @property
    def is_empty(self) -> bool:
        return not (self.body or "").strip()

    def render(self) -> str:
        """Heading + body. The heading is always present, so a reader (or a test)
        can tell which producer emitted a block."""
        return f"{self.heading}\n{self.body}".strip()


class TurnInjections:
    """The injections for one turn, filtered and composed.

    Use per turn — it is cheap — and add producers in any order: composition
    follows ``source`` then insertion, never the order of overlapping edits.
    """

    def __init__(self, state: SessionState):
        self.state = state
        self._items: list[Injection] = []

    def add(self, injection: Optional[Injection]) -> "TurnInjections":
        """Add one injection, ignoring ``None`` and empty bodies."""
        if injection is not None and not injection.is_empty:
            self._items.append(injection)
        return self

    def extend(self, injections: Iterable[Optional[Injection]]) -> "TurnInjections":
        for injection in injections:
            self.add(injection)
        return self

    def applying(self) -> list[Injection]:
        """The injections that apply to this turn's state, in insertion order."""
        return [i for i in self._items if i.scope.applies(self.state)]

    def skipped(self) -> list[Injection]:
        """The ones this turn's state excludes — useful in tests and logs."""
        return [i for i in self._items if not i.scope.applies(self.state)]

    def compose(self) -> str:
        """Render every applicable injection, each under its own heading.

        Returns "" when nothing applies, so a caller can pass the result
        straight to ``ephemeral_context`` without a truthiness dance.
        """
        return "\n\n".join(i.render() for i in self.applying())

    def __bool__(self) -> bool:
        return bool(self.applying())

    def __len__(self) -> int:
        return len(self.applying())


# ---------------------------------------------------------------------------
# AgentGate producers
# ---------------------------------------------------------------------------

#: Headings are structural: tests and readers rely on them. Operators override
#: text via prompt files, never these.
ORIENTATION_HEADING = ""      # build_orientation emits its own two headings
MEMORY_RECORDS_HEADING = "## Relevant memory for this turn"
MANAGED_GUIDANCE_HEADING = "## Repository guidance for this task"
COMPACTION_HEADING = "## Summary of earlier work in this session"


def orientation_injection(
    workspace_path: str,
    additional_dirs: Optional[Iterable[str]] = None,
) -> Optional[Injection]:
    """Where the session starts, and what it may reach. Never raises."""
    try:
        from core.context.orientation import build_orientation
        body = build_orientation(workspace_path, list(additional_dirs or []))
    except Exception:
        return None
    if not (body or "").strip():
        return None
    return Injection(
        source="agentgate",
        asset="orientation",
        # The builder emits "## Your starting directory" and "## Additional
        # directories available to you" itself, so no extra heading is added.
        heading=ORIENTATION_HEADING,
        body=body,
        scope=ASSET_SCOPES["orientation"],
    )


def repo_map_injection(
    workspace_path: str,
    *,
    candidates: Optional[list[str]] = None,
) -> Optional[Injection]:
    """An outline of the repository this session is working in.

    ``candidates`` are paths the session has evidence for. Passing the whole
    ``ADDITIONAL_DIRS`` list makes the first entry win, which is how every
    session ended up with a map of AgentGate regardless of what it was working
    on — see :func:`core.context.repo_map.find_repo_root`. Callers should pass
    evidence for *this* session, not the operator's registry.
    """
    try:
        from core.context.repo_map import build_repo_map_segment
        body = build_repo_map_segment(workspace_path, candidates=candidates)
    except Exception:
        return None
    if not (body or "").strip():
        return None
    # The segment already opens with its own heading.
    return Injection(
        source="agentgate",
        asset="repo_map",
        heading="",
        body=body,
        scope=ASSET_SCOPES["repo_map"],
    )


def memory_records_injection(parts: Iterable[str]) -> Optional[Injection]:
    """AgentDB's recalled memory for this turn.

    Per-turn by nature: it answers *this* question. Session-scoped artefacts are
    the caller's business — see :func:`split_memory_parts`.
    """
    body = "\n\n".join(p for p in parts if (p or "").strip())
    if not body.strip():
        return None
    return Injection(
        source="agentdb",
        asset="memory_records",
        heading=MEMORY_RECORDS_HEADING,
        body=body,
        scope=ASSET_SCOPES["memory_records"],
    )


def compaction_summary_injection(parts: Iterable[str]) -> Optional[Injection]:
    """AgentDB's compaction handover note, shown when a session resumes.

    The summary is written at compaction and describes what the session had done
    before the transcript was trimmed. A resuming process needs exactly that; a
    live one already has the transcript.
    """
    body = "\n\n".join(p for p in parts if (p or "").strip())
    if not body.strip():
        return None
    return Injection(
        source="agentdb",
        asset="compaction_summary",
        heading=COMPACTION_HEADING,
        body=body,
        scope=ASSET_SCOPES["compaction_summary"],
    )


def managed_guidance_injection(text: str, *, record_key: str = "") -> Optional[Injection]:
    """One maintainer-managed instruction for this turn.

    Emitted only when the host resolved a matching selection *and* AgentDB allowed
    the read *and* the record's own scope matched. This function adds no decision
    of its own: it renders what was resolved, so a caller cannot accidentally
    broaden applicability here.
    """
    body = (text or "").strip()
    if not body:
        return None
    return Injection(
        source="agentdb",
        asset="managed_guidance",
        heading=MANAGED_GUIDANCE_HEADING,
        body=body,
        scope=ASSET_SCOPES["managed_guidance"],
    )


def split_memory_parts(
    parts: Iterable[str],
) -> tuple[list[str], list[str], list[str]]:
    """Sort retrieved memory lines into the three scopes they belong to.

    Returns ``(per_turn, resume_only, discarded)``.

    - **per_turn** — episodic and canonical records: they answer this question.
    - **resume_only** — compaction summaries: the session's handover note. Worth
      showing a process that has lost the transcript, not worth repeating on
      every turn of a live session.
    - **discarded** — compaction checkpoints. They carry no content (all 16 rows
      in a real store were pure ``{"type": "checkpoint"}`` stubs), so they are
      dropped rather than re-scoped.

    AgentDB renders each record as one line in a block, so the split is per
    *line*. A header line that carries no record stays with whichever bucket has
    content, so surviving records keep their heading. Duplicates are collapsed:
    a retrieval pass can return the same record more than once.
    """
    per_turn: list[str] = []
    resume_only: list[str] = []
    discarded: list[str] = []
    seen: set[str] = set()

    for part in parts:
        if not (part or "").strip():
            continue
        keep_turn: list[str] = []
        keep_resume: list[str] = []
        header: list[str] = []
        for line in part.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if is_checkpoint_record(stripped):
                if stripped not in seen:
                    seen.add(stripped)
                    discarded.append(stripped)
                continue
            if is_session_scoped_record(stripped):
                if stripped not in seen:
                    seen.add(stripped)
                    keep_resume.append(line)
                continue
            if not _looks_like_record_line(stripped):
                # A block header ("Background context from previous sessions:")
                # belongs with whichever records survive.
                header.append(line)
                continue
            if stripped not in seen:
                seen.add(stripped)
                keep_turn.append(line)

        if keep_turn:
            per_turn.append("\n".join(header + keep_turn))
            header = []
        if keep_resume:
            resume_only.append("\n".join(header + keep_resume))

    return per_turn, resume_only, discarded


def _looks_like_record_line(line: str) -> bool:
    """Heuristic: a rendered memory record starts with a bullet.

    AgentDB's ``record_line_format`` is ``- {key}: {value}``, so every record is
    a bullet and every heading is not. Used only to decide where a block header
    travels, never to classify a record's family.
    """
    return line.lstrip().startswith("- ")




# ---------------------------------------------------------------------------
# Runner-facing assembly
# ---------------------------------------------------------------------------

def build_turn_injections(
    state: SessionState,
    *,
    workspace_path: str,
    additional_dirs: Optional[Iterable[str]] = None,
    repo_candidates: Optional[list[str]] = None,
    agentdb_memory_parts: Optional[Iterable[str]] = None,
    managed_guidance_text: str = "",
    nudge: str = "",
) -> TurnInjections:
    """Assemble every injection for one turn.

    Shared by the open-model runners so their assembly cannot drift apart — the
    three copies that prompted this module had already diverged from the
    documented behaviour. Producers are added independently; nothing is merged
    into a single string until :meth:`TurnInjections.compose`.

    **Boundary producers are never run on a warm turn.** Building the repo map
    walks the repository, so running it and then filtering the result would keep
    the per-message scan this module exists to remove. The scope is consulted
    before the producer is called, not after.

    ``repo_candidates`` is passed straight to :func:`repo_map_injection` and
    should be evidence for *this session*, never the operator's whole
    ``ADDITIONAL_DIRS`` registry. ``None`` means "no repo map" — the honest
    answer when the session has not established where it is working.
    """
    injections = TurnInjections(state)
    at_boundary = InjectionScope.SESSION_BOUNDARY.applies(state)

    if at_boundary:
        injections.add(orientation_injection(workspace_path, additional_dirs))
        if repo_candidates is not None:
            injections.add(repo_map_injection(workspace_path, candidates=repo_candidates))

    if (managed_guidance_text or "").strip():
        # Rendered first among AgentDB assets: a maintainer's instruction is the
        # most specific thing a turn can carry, so it precedes recalled memory.
        injections.add(managed_guidance_injection(managed_guidance_text))

    if agentdb_memory_parts:
        per_turn, resume_only, _discarded = split_memory_parts(agentdb_memory_parts)
        injections.add(memory_records_injection(per_turn))
        # A compaction summary is the session's handover note: it belongs to a
        # resume, where the transcript may be gone. Repeating it on every turn of
        # a live session would be the same noise as the checkpoint stubs.
        if resume_only and InjectionScope.SESSION_RESUME.applies(state):
            injections.add(compaction_summary_injection(resume_only))

    if (nudge or "").strip():
        injections.add(Injection(
            source="agentgate",
            asset="skills_nudge",
            heading="",
            body=nudge,
            scope=ASSET_SCOPES["skills_nudge"],
        ))

    return injections
