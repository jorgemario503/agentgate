"""Runner-side handoff contract (formerly ``core/orchestration``).

Scheduled for rename to ``core/handoff`` once the orchestrator-owned modules move to
the **agentorchestrator** repository (DEC-404). This package is AgentGate's published
surface for that repo, so changes here are API changes.

What lives here, and why
------------------------
AgentGate is the **runner**; agentorchestrator owns the workflow. This package holds
only what the runner legitimately needs:

* **``validate`` + ``schemas/``** — the wire contracts. They stay in the runner because
  AgentGate must validate a ``milestone_submit`` payload without importing the
  orchestrator; moving them would invert the dependency (DEC-108 as revised).
* **``submit``** — the ``milestone_submit`` tool. In-process, request-only, no decisions.
* **``usage``** — provider telemetry normalisation, read from a response the runner
  already holds.

Everything else in this package at the time of writing (``registry``, ``worktree``,
``cleanup``, ``gate_store``, ``authz``) is **orchestrator state scheduled to move out**
per the boundary decision record. Do not add orchestration logic here: it belongs in
agentorchestrator.

Dependency rule
---------------
This package imports nothing from ``runners/``, ``bots/`` or ``agentorchestrator``. The
dependency direction is agentorchestrator -> agentgate, never the reverse. Enforced by
tests in ``tests/test_orchestration_contracts.py``. Of the three staying modules, only
the standard library is used, so the wheel is installable into the orchestrator without
the bot stack.

    from core.orchestration import validate_handoff, ValidationError
"""
from __future__ import annotations

#: Distribution version, mirrored from pyproject.toml. Kept here so a consumer can
#: assert which contract it is running against without reading package metadata.
#: A test asserts this equals pyproject.toml's version.
__version__ = "0.2.0"

from core.orchestration.submit import SubmitError, submit_handoff
from core.orchestration.usage import (
    UsageRecord,
    UsageSink,
    normalize_usage,
    safe_emit,
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

#: Stable published surface for agentorchestrator (DEC-407 check 2).
#: These names are what the orchestrator may rely on. Removing or renaming one is a
#: breaking API change and must fail CI until the orchestrator's pin is bumped.
PUBLISHED_API = (
    # wire contracts
    "ROLES", "TRANSITIONS", "RECOMMENDATIONS", "SEVERITIES",
    "ACCEPTANCE_STATUSES", "GATE_STATUSES",
    "ValidationError", "require_valid", "check_path_contained",
    "validate_handoff", "validate_review_findings",
    "validate_gate_decision", "validate_decision_digest", "validate_gate_record",
    # runner-side handoff request
    "submit_handoff", "SubmitError",
    # provider telemetry
    "UsageRecord", "UsageSink", "normalize_usage", "safe_emit",
)

#: Transitional modules: **none remain**. Workspace lifecycle and the gate store both
#: moved to agentorchestrator (migration steps 1 and 2). Kept as an empty tuple so the
#: surface test can still assert that nothing transitional leaked back in.
TRANSITIONAL = ()

__all__ = list(PUBLISHED_API) + list(TRANSITIONAL)
