"""Orchestration concern package.

Holds the machine-readable contracts shared between AgentGate and the sibling
orchestrator application: versioned JSON schemas (``core/orchestration/schemas/``)
and pure-Python validators (``core.orchestration.validate``).

This package deliberately contains **no** runtime orchestration behaviour yet:
no container supervision, no gate store, no FSM. Those land in later milestones.
Keeping the contracts here means the schemas and their validators evolve together
and neither imports from ``runners/`` or ``bots/``.

Layout follows ``core/skills/`` and ``core/tools/`` (group by concern, not by file
type), so callers import from the package rather than a module path:

    from core.orchestration import validate_handoff, ValidationError
"""
from __future__ import annotations

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
