"""Runner-side handoff contract.

Renamed from ``core.orchestration`` (DEC-404): "orchestration" now names the *other*
repo, and keeping it here invited re-merging workflow logic into the runner. The word
also matches the vocabulary already used throughout — ``handoff.json``,
``submit_handoff``, ``validate_handoff``.

What lives here, and why
------------------------
AgentGate is the **runner**; agentorchestrator owns the workflow. This package holds
only what the runner legitimately needs:

* **``validate`` + ``schemas/``** — the wire contracts. They stay with the runner because
  AgentGate must validate a ``milestone_submit`` payload without importing the
  orchestrator; moving them would invert the dependency (DEC-108 as revised).
* **``submit``** — the ``milestone_submit`` tool. In-process, request-only, no decisions.
* **``usage``** — provider telemetry normalisation, read from a response the runner
  already holds.

The gate contracts (``validate_gate_decision``, ``validate_gate_record``,
``GATE_STATUSES``) also live here, although the gate *store* and *authorization* moved to
agentorchestrator. Contracts stay with the runner; state and policy do not.

Do **not** add orchestration logic here. The FSM, gate policy, risk classification,
Slack interaction, escalation and container supervision all belong in agentorchestrator.

Dependency rule
---------------
This package imports nothing from ``runners/``, ``bots/`` or ``agentorchestrator``. The
dependency direction is agentorchestrator -> agentgate, never the reverse. Enforced by
tests in ``tests/test_handoff_contracts.py``. Only the standard library is used, so the
wheel installs into the orchestrator without the bot stack.

    from core.handoff import validate_handoff, ValidationError
"""
from __future__ import annotations

#: Distribution version, mirrored from pyproject.toml. Kept here so a consumer can
#: assert which contract it is running against without reading package metadata.
#: A test asserts this equals pyproject.toml's version.
__version__ = "0.3.1"

from core.handoff.submit import SubmitError, submit_handoff
from core.handoff.usage import (
    UsageRecord,
    UsageSink,
    normalize_usage,
    safe_emit,
)
from core.handoff.validate import (
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
