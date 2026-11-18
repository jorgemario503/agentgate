"""Tests for core/agentdb.py — the shared AgentDB delegation surface."""

import pytest

from core.agentdb import AgentDBSurface


class FakeMiddleware:
    def __init__(self):
        self.calls = []
        self.agent_id = None

    def pending_conflicts(self):
        self.calls.append("pending_conflicts")
        return ["c1", "c2"]

    def list_proposals(self, status="pending"):
        self.calls.append(("list_proposals", status))
        return [{"id": "p1"}]

    def propose_skill(self, name, content, scope):
        self.calls.append(("propose_skill", name, content, scope))
        return "proposal-1"

    def approve_proposal(self, proposal_id):
        self.calls.append(("approve", proposal_id))

    def reject_proposal(self, proposal_id, reason=None):
        self.calls.append(("reject", proposal_id, reason))


def test_unbound_surface_raises():
    with pytest.raises(RuntimeError, match="not initialized"):
        AgentDBSurface().list_pending_conflicts()


def test_conflicts_are_forwarded():
    fake = FakeMiddleware()
    surface = AgentDBSurface()
    surface.bind(fake, "agent-x")
    assert surface.list_pending_conflicts() == ["c1", "c2"]
    assert fake.calls == ["pending_conflicts"]


def test_proposals_forward_status():
    fake = FakeMiddleware()
    AgentDBSurface(fake, "agent-x")
    surface = AgentDBSurface()
    surface.bind(fake, "agent-x")
    assert surface.list_pending_proposals("approved") == [{"id": "p1"}]
    assert fake.calls[-1] == ("list_proposals", "approved")


def test_propose_skill_uses_bound_agent_id():
    fake = FakeMiddleware()
    surface = AgentDBSurface()
    surface.bind(fake, "slack_assistant")
    assert surface.propose_skill("n", "body") == "proposal-1"
    assert fake.calls[-1] == ("propose_skill", "n", "body", ["slack_assistant"])


def test_approve_and_reject_are_forwarded():
    fake = FakeMiddleware()
    surface = AgentDBSurface()
    surface.bind(fake, "a")
    surface.approve_proposal("p1")
    surface.reject_proposal("p2", reason="dupe")
    assert ("approve", "p1") in fake.calls
    assert ("reject", "p2", "dupe") in fake.calls


def test_surface_holds_no_capability_or_provider_knowledge():
    """Guardrail: the helper itself must not decide capabilities or governance.

    Checked against the class source only (the module docstring explains the
    boundary and will legitimately mention these words).
    """
    import inspect

    source = inspect.getsource(AgentDBSurface)
    for forbidden in ("CAPABILITIES", "permissions_path", "memory.yaml"):
        assert forbidden not in source, f"{forbidden} leaked into AgentDBSurface"
