"""Orchestration concern package.

Holds the machine-readable contracts shared between AgentGate and the sibling
orchestrator application: versioned JSON schemas (``core/orchestration/schemas/``)
and pure-Python validators (``core.orchestration.validate``).

This package deliberately contains **no** runtime orchestration behaviour yet:
no container supervision, no gate store, no FSM. Those land in later milestones
and in the sibling application. Keeping the contracts here means the schemas and
their validators evolve together and neither imports from ``runners/`` or ``bots/``.

Why the contracts live in AgentGate rather than the orchestrator
----------------------------------------------------------------
AgentGate has two in-process consumers that exist whether or not orchestration is
enabled:

* ``milestone_submit`` (``core/tools/registry.py``) must validate a handoff payload
  before it can request a transition.
* The reviewer agent runs on an open-model runner here and must be able to produce a
  valid ``review-findings.json``.

If the validators lived only in the sibling app, AgentGate would have to depend on
the orchestrator, inverting the intended dependency (the coordinator drives AgentGate,
not the reverse). So the orchestrator **imports** this package rather than redefining
the wire format. Precedent: ``core/skills/catalog.py`` implements an external standard
in-repo for the same reason.

Of the five contracts, ``handoff`` and ``review-findings`` are genuinely shared;
``gate-decision`` and ``decision-digest`` lean orchestrator; ``gate-record`` describes
the durable GateStore and is the most likely one to relocate once the sibling app's
boundaries are concrete. They are versioned together here so one drift guard covers
all of them. (Decision log DEC-108 / DEC-109.)

Layout follows ``core/skills/`` and ``core/tools/`` (group by concern, not by file
type), so callers import from the package rather than a module path:

    from core.orchestration import validate_handoff, ValidationError
"""
from __future__ import annotations

from core.orchestration.registry import (
    RepoRegistry,
    RegistryError,
    SnapshotRecord,
    WorktreeRecord,
    STATUS_ACTIVE,
    STATUS_CLEANED,
    STATUS_CLEANUP_FAILED,
    STATUS_CLEANUP_PENDING,
    STATUS_PROVISIONING,
)
from core.orchestration.validate import (
    ACCEPTANCE_STATUSES,
    GATE_STATUSES,
    RECOMMENDATIONS,
    ROLES,
    SEVERITIES,
    TRANSITIONS,
    ValidationError,
    check_path_contained,
    require_valid,
    validate_decision_digest,
    validate_gate_decision,
    validate_gate_record,
    validate_handoff,
    validate_review_findings,
)

__all__ = [
    "RepoRegistry",
    "RegistryError",
    "SnapshotRecord",
    "WorktreeRecord",
    "STATUS_ACTIVE",
    "STATUS_CLEANED",
    "STATUS_CLEANUP_FAILED",
    "STATUS_CLEANUP_PENDING",
    "STATUS_PROVISIONING",
    "ACCEPTANCE_STATUSES",
    "GATE_STATUSES",
    "RECOMMENDATIONS",
    "ROLES",
    "SEVERITIES",
    "TRANSITIONS",
    "ValidationError",
    "check_path_contained",
    "require_valid",
    "validate_decision_digest",
    "validate_gate_decision",
    "validate_gate_record",
    "validate_handoff",
    "validate_review_findings",
]
