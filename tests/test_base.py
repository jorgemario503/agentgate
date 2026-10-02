"""Tests for runners/base.py — shared types and utilities."""
import pytest
from runners.base import PlatformConfig, TaskInterrupted, check_blocked


def test_platform_config_defaults():
    store = object()
    pc = PlatformConfig(session_store=store)
    assert pc.session_store is store
    assert pc.reply_hint == "`yes` to allow or `no` to deny"
    assert pc.agent_id == "agent"
    assert pc.allowed_user_ids == frozenset()
    assert pc.admin_user_ids == frozenset()


def test_platform_config_frozen():
    store = object()
    pc = PlatformConfig(session_store=store)
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        pc.agent_id = "other"


def test_task_interrupted_is_runtime_error():
    exc = TaskInterrupted("task was cancelled")
    assert isinstance(exc, RuntimeError)
    assert str(exc) == "task was cancelled"


_BLOCKED_PATTERNS = [
    r"rm\s+(-\w+\s+)*(/\s|~/|~\s|\.\s|\.\.|/\*|\*)",
    r"rm\s+-[a-z]*r[a-z]*f\s+(/|~|\.|\.\.|\*)",
    r"DROP\s+TABLE",
    r"git\s+push\s+--force",
]


def test_check_blocked_rm_rf_root():
    assert check_blocked("rm -rf /", _BLOCKED_PATTERNS) is not None


def test_check_blocked_drop_table():
    assert check_blocked("DROP TABLE users", _BLOCKED_PATTERNS) is not None


def test_check_blocked_git_force_push():
    assert check_blocked("git push --force origin main", _BLOCKED_PATTERNS) is not None


def test_check_blocked_safe_commands():
    assert check_blocked("ls -la", _BLOCKED_PATTERNS) is None
    assert check_blocked("git status", _BLOCKED_PATTERNS) is None
    assert check_blocked("python run.py --telegram", _BLOCKED_PATTERNS) is None


def test_check_blocked_returns_pattern_string():
    result = check_blocked("DROP TABLE users", _BLOCKED_PATTERNS)
    assert isinstance(result, str)
