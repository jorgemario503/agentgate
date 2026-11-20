"""Tests for core/agentdb.py — the shared AgentDB delegation surface."""

import pytest

from core.agentdb import AgentDBSurface


class FakeMiddleware:
    def __init__(self):
        self.calls = []

    def pending_conflicts(self):
        self.calls.append("pending_conflicts")
        return ["c1", "c2"]


def test_unbound_surface_raises():
    with pytest.raises(RuntimeError, match="not initialized"):
        AgentDBSurface().list_pending_conflicts()


def test_conflicts_are_forwarded():
    fake = FakeMiddleware()
    surface = AgentDBSurface()
    surface.bind(fake)
    assert surface.list_pending_conflicts() == ["c1", "c2"]
    assert fake.calls == ["pending_conflicts"]


def test_surface_holds_no_capability_or_provider_knowledge():
    """Guardrail: the helper itself must not decide capabilities or governance.

    Checked against the class source only (the module docstring explains the
    boundary and will legitimately mention these words).
    """
    import inspect

    source = inspect.getsource(AgentDBSurface)
    for forbidden in ("CAPABILITIES", "permissions_path", "memory.yaml"):
        assert forbidden not in source, f"{forbidden} leaked into AgentDBSurface"
