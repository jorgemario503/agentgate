"""Tests for runners/local.py — LocalRunner."""
import asyncio
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


def test_local_runner_importable():
    from runners.local import LocalRunner
    assert LocalRunner is not None


def test_local_runner_has_required_methods():
    from runners.local import LocalRunner
    for method in ("run_prompt", "cancel", "close_client", "close_all",
                   "has_client", "init_agentdb", "list_pending_conflicts",
                   "list_pending_proposals", "propose_skill",
                   "approve_skill_proposal", "reject_skill_proposal"):
        assert hasattr(LocalRunner, method), f"LocalRunner missing {method}"


def test_local_runner_from_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:14b")
    monkeypatch.setenv("OLLAMA_CONTEXT_WINDOW", "65536")
    from runners.local import LocalRunner
    runner = LocalRunner.from_env("ollama")
    assert runner.name == "ollama"
    assert runner.base_url == "http://localhost:11434/v1"
    assert runner.model == "qwen3:14b"
    assert runner.context_window == 65536


def test_has_client_false_before_any_session():
    from runners.local import LocalRunner
    runner = LocalRunner(name="test", base_url="http://localhost:11434/v1", model="test")
    assert runner.has_client("no-such-thread") is False


def test_init_agentdb_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("AGENTDB_ENABLED", "false")
    import importlib
    import core.config as cfg
    importlib.reload(cfg)
    from runners.local import LocalRunner
    runner = LocalRunner(name="test", base_url="http://localhost/v1", model="test-model")
    runner.init_agentdb(db=None, platform=make_platform())
    assert runner._agentdb_middleware is None


def test_list_pending_conflicts_raises_when_not_initialized():
    from runners.local import LocalRunner
    runner = LocalRunner(name="test", base_url="http://localhost/v1", model="test-model")
    with pytest.raises(RuntimeError, match="AgentDB"):
        runner.list_pending_conflicts()


def test_cancel_returns_false_when_no_client():
    from runners.local import LocalRunner
    runner = LocalRunner(name="test", base_url="http://localhost/v1", model="test-model")
    result = asyncio.run(runner.cancel("no-such-thread"))
    assert result is False


def test_set_model_updates_runner():
    from runners.local import LocalRunner
    runner = LocalRunner(name="test", base_url="http://localhost/v1", model="original")
    runner.set_model("updated-model")
    assert runner.model == "updated-model"


def test_multiple_runners_isolated():
    """Two LocalRunner instances must not share state."""
    from runners.local import LocalRunner
    r1 = LocalRunner(name="ollama", base_url="http://localhost:11434/v1", model="a")
    r2 = LocalRunner(name="mlx", base_url="http://localhost:8081/v1", model="b")
    assert r1.base_url != r2.base_url
    assert r1._clients is not r2._clients
