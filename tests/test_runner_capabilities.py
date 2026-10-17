"""Tests that each runner's init_agentdb wires the correct RunnerCapabilities."""
import pytest
from unittest.mock import MagicMock
from agentdb.adapters.capabilities import (
    LOCAL_CAPABILITIES, FIREWORKS_CAPABILITIES, CLAUDE_CAPABILITIES
)
from agentdb.adapters.middleware import AgentDBMiddleware


def _make_mock_db():
    db = MagicMock()
    db.config_for.return_value = {
        "name": "budget_optimized",
        "retrieval": {"retrieve_limit": 10},
        "injection": {"context_token_limit": 4096, "override_prefix": "⚠",
                      "memory_header": "## Memory", "memory_budget_chars": 4096,
                      "record_line_format": "{key}: {value}"},
        "capture": {"auto_persist_response": False, "conversation_jsonl": False},
    }
    db.skills = MagicMock()
    db.skills.search.return_value = []
    db.skills.get_for_agent.return_value = []
    db.retrieve.return_value = MagicMock(records=[], override_instructions=[])
    db.gateway = MagicMock()
    db.gateway.subscribe = MagicMock()
    return db


def make_store():
    store = MagicMock()
    store.base = "/tmp/test"
    store.lookup_directory.return_value = None
    return store


def make_platform():
    from runners.base import PlatformConfig
    return PlatformConfig(session_store=make_store(), agent_id="surfer")


def test_local_runner_init_agentdb_uses_local_capabilities(monkeypatch):
    monkeypatch.setattr("core.config.AGENTDB_ENABLED", True)
    from runners.local import LocalRunner
    runner = LocalRunner(name="test", base_url="http://localhost/v1", model="test")
    runner.init_agentdb(db=_make_mock_db(), platform=make_platform())
    assert isinstance(runner._agentdb_middleware, AgentDBMiddleware)
    assert runner._agentdb_middleware._capabilities is LOCAL_CAPABILITIES


def test_fireworks_runner_init_agentdb_uses_fireworks_capabilities(monkeypatch):
    monkeypatch.setattr("core.config.AGENTDB_ENABLED", True)
    from runners.fireworks import FireworksRunner
    runner = FireworksRunner()
    runner.init_agentdb(db=_make_mock_db(), platform=make_platform())
    assert isinstance(runner._agentdb_middleware, AgentDBMiddleware)
    assert runner._agentdb_middleware._capabilities is FIREWORKS_CAPABILITIES


def test_claude_runner_init_agentdb_uses_claude_capabilities(monkeypatch):
    monkeypatch.setattr("core.config.AGENTDB_ENABLED", True)
    from runners.claude import ClaudeRunner
    runner = ClaudeRunner()
    runner.init_agentdb(db=_make_mock_db(), platform=make_platform())
    assert isinstance(runner._agentdb_middleware, AgentDBMiddleware)
    assert runner._agentdb_middleware._capabilities is CLAUDE_CAPABILITIES
