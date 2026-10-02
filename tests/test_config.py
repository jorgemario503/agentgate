"""Tests for core/config.py — centralized env var loading."""
import os
import pytest


def test_blocked_patterns_not_empty():
    from core.config import BLOCKED_PATTERNS
    assert len(BLOCKED_PATTERNS) > 0


def test_blocked_patterns_are_strings():
    from core.config import BLOCKED_PATTERNS
    for p in BLOCKED_PATTERNS:
        assert isinstance(p, str)


def test_sdk_allowed_tools_contains_read():
    from core.config import SDK_ALLOWED_TOOLS
    assert "Read" in SDK_ALLOWED_TOOLS
    assert "Bash" not in SDK_ALLOWED_TOOLS


def test_agentdb_enabled_defaults_false(monkeypatch):
    monkeypatch.delenv("AGENTDB_ENABLED", raising=False)
    import importlib
    import core.config as cfg
    importlib.reload(cfg)
    assert cfg.AGENTDB_ENABLED is False


def test_agentdb_enabled_true_when_set(monkeypatch):
    monkeypatch.setenv("AGENTDB_ENABLED", "true")
    import importlib
    import core.config as cfg
    importlib.reload(cfg)
    assert cfg.AGENTDB_ENABLED is True


def test_claude_settings_has_permissions():
    from core.config import CLAUDE_SETTINGS
    assert "permissions" in CLAUDE_SETTINGS
    assert "allow" in CLAUDE_SETTINGS["permissions"]
    assert "deny" in CLAUDE_SETTINGS["permissions"]


def test_system_prompt_is_nonempty_string():
    from core.config import SYSTEM_PROMPT
    assert isinstance(SYSTEM_PROMPT, str)
    assert len(SYSTEM_PROMPT) > 50
