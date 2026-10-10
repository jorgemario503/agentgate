"""Tests for runners/fireworks.py — FireworksRunner (SDK-independent paths)."""
import pytest
from unittest.mock import MagicMock
from runners.base import PlatformConfig


def make_store():
    store = MagicMock()
    store.base = "/tmp/test"
    store.lookup_directory.return_value = None
    return store


def make_platform():
    return PlatformConfig(session_store=make_store(), agent_id="test_agent")


def test_fireworks_runner_importable():
    from runners.fireworks import FireworksRunner
    assert FireworksRunner is not None


def test_fireworks_runner_has_required_methods():
    from runners.fireworks import FireworksRunner
    for method in ("run_prompt", "cancel", "close_client", "close_all",
                   "has_client", "init_agentdb", "list_pending_conflicts",
                   "list_pending_proposals", "propose_skill",
                   "approve_skill_proposal", "reject_skill_proposal"):
        assert hasattr(FireworksRunner, method), f"FireworksRunner missing {method}"


def test_has_client_false_before_any_session():
    from runners.fireworks import FireworksRunner
    runner = FireworksRunner()
    assert runner.has_client("no-such-thread") is False


def test_init_agentdb_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("AGENTDB_ENABLED", "false")
    import importlib
    import core.config as cfg
    importlib.reload(cfg)
    from runners.fireworks import FireworksRunner
    runner = FireworksRunner()
    runner.init_agentdb(db=None, platform=make_platform())
    assert runner._agentdb_middleware is None


def test_list_pending_conflicts_raises_when_not_initialized():
    from runners.fireworks import FireworksRunner
    runner = FireworksRunner()
    with pytest.raises(RuntimeError, match="AgentDB"):
        runner.list_pending_conflicts()


def test_cancel_returns_false_when_no_client():
    import asyncio
    from runners.fireworks import FireworksRunner
    runner = FireworksRunner()
    result = asyncio.run(runner.cancel("no-such-thread"))
    assert result is False


def test_permission_decision_deny_beats_allow():
    from runners.fireworks import permission_decision
    # An explicit deny rule must beat an allow even if the same tool is auto-allowed
    import core.config as cfg
    original = cfg.CLAUDE_SETTINGS
    cfg.CLAUDE_SETTINGS = {
        "permissions": {
            "allow": ["Read"],
            "deny": ["Read(**/.env)"],
        }
    }
    try:
        # .env file should be denied even though Read is allowed
        result = permission_decision("Read", {"file_path": "/home/user/.env"})
        assert result == "deny"
        # normal file should be allowed
        result2 = permission_decision("Read", {"file_path": "/home/user/code.py"})
        assert result2 == "allow"
    finally:
        cfg.CLAUDE_SETTINGS = original


def test_describe_tool_call_bash():
    from runners.base import _describe_tool_call
    desc = _describe_tool_call("Bash", {"command": "ls -la"})
    assert "ls -la" in desc


def test_resolve_model_bare_name():
    from runners.fireworks import resolve_model
    spec = resolve_model("minimax-m3")
    assert spec.name == "minimax-m3"
    assert "minimax-m3" in spec.model_id
    assert spec.context_window > 0


def test_resolve_model_full_id():
    from runners.fireworks import resolve_model
    spec = resolve_model("accounts/acme/models/my-model")
    assert spec.name == "my-model"
    assert spec.model_id == "accounts/acme/models/my-model"
