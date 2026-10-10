"""Tests for runners/claude.py — ClaudeRunner (SDK-independent paths)."""
import pytest
from unittest.mock import MagicMock, patch
from runners.base import PlatformConfig


def make_store():
    store = MagicMock()
    store.base = "/tmp/test"
    store.lookup_directory.return_value = None
    return store


def make_platform():
    return PlatformConfig(session_store=make_store(), agent_id="test_agent")


def test_claude_runner_importable():
    from runners.claude import ClaudeRunner
    assert ClaudeRunner is not None


def test_claude_runner_has_required_methods():
    from runners.claude import ClaudeRunner
    for method in ("run_prompt", "cancel", "close_client", "close_all",
                   "has_client", "init_agentdb", "list_pending_conflicts",
                   "list_pending_proposals", "propose_skill",
                   "approve_skill_proposal", "reject_skill_proposal"):
        assert hasattr(ClaudeRunner, method), f"ClaudeRunner missing {method}"


def test_has_client_false_before_any_session():
    from runners.claude import ClaudeRunner
    runner = ClaudeRunner()
    assert runner.has_client("no-such-thread") is False


def test_init_agentdb_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("AGENTDB_ENABLED", "false")
    import importlib
    import core.config as cfg
    importlib.reload(cfg)
    from runners.claude import ClaudeRunner
    runner = ClaudeRunner()
    # should not raise even though agentdb may not be installed
    runner.init_agentdb(db=None, platform=make_platform())
    assert runner._agentdb_middleware is None


def test_list_pending_conflicts_raises_when_not_initialized():
    from runners.claude import ClaudeRunner
    runner = ClaudeRunner()
    with pytest.raises(RuntimeError, match="AgentDB"):
        runner.list_pending_conflicts()


def test_cancel_returns_false_when_no_client():
    import asyncio
    from runners.claude import ClaudeRunner
    runner = ClaudeRunner()
    result = asyncio.run(runner.cancel("no-such-thread"))
    assert result is False
