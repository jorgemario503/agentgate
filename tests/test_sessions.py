"""Tests for core/sessions.py."""
import json
import os
import pytest
from core.sessions import SessionStore, wants_claude_settings


@pytest.fixture
def store(tmp_path):
    return SessionStore(base=str(tmp_path / "sessions"))


def test_register_and_get(store):
    store.register_session("t1", "ch1", "/ws/t1")
    meta = store.get_session_meta("t1")
    assert meta is not None
    assert meta["channel"] == "ch1"
    assert meta["workspace_path"] == "/ws/t1"
    assert meta.get("session_id") is None


def test_update_session_id(store):
    store.register_session("t1", "ch1", "/ws/t1")
    store.update_session_id("t1", "abc-123")
    assert store.get_session_meta("t1")["session_id"] == "abc-123"


def test_mark_idle_notified_preserves_session_id(store):
    """Recording that the user was told preserves the saved session id.

    The marker replaced the old `expired` boolean; either way the row survives
    so the next turn can resume rather than start over.
    """
    store.register_session("t1", "ch1", "/ws/t1", session_id="saved-id")
    store.mark_idle_notified("t1")
    meta = store.get_session_meta("t1")
    assert "expired" not in meta
    assert store.idle_notified_at("t1") is not None
    assert meta["session_id"] == "saved-id"


def test_unregister_removes_entry(store):
    store.register_session("t1", "ch1", "/ws/t1")
    store.unregister_session("t1")
    assert store.get_session_meta("t1") is None


def test_ensure_workspace_creates_dir(store):
    path = store.ensure_workspace("t1")
    assert os.path.isdir(path)
    assert path.endswith("/workspace")


def test_lookup_and_remember_directory(store):
    assert store.lookup_directory("myrepo") is None
    store.remember_directory("myrepo", "/home/user/repos/myrepo")
    assert store.lookup_directory("myrepo") == "/home/user/repos/myrepo"


def test_append_audit_log(store, tmp_path):
    store.append_audit_log("t1", "tool_call", {"tool": "Bash", "decision": "allow"})
    log_path = os.path.join(store.base, "t1", "audit.log")
    assert os.path.exists(log_path)
    entry = json.loads(open(log_path).read().strip())
    assert entry["event"] == "tool_call"
    assert entry["tool"] == "Bash"


# -- workspace setup is runner-aware --------------------------------------


def test_ensure_workspace_writes_claude_settings_by_default(store):
    """Claude needs .claude/settings.json in its cwd, so this stays the default."""
    path = store.ensure_workspace("t1")
    assert os.path.isfile(os.path.join(path, ".claude", "settings.json"))


def test_ensure_workspace_can_skip_claude_settings(store):
    """Non-Claude runners have no use for a Claude settings file."""
    path = store.ensure_workspace("t1", claude_settings=False)
    assert os.path.isdir(path)
    assert not os.path.exists(os.path.join(path, ".claude"))


def test_bots_only_write_claude_settings_for_claude():
    """The bots pass the runner kind through, so a non-Claude run writes none."""
    import inspect

    for module_path, needle in (
        ("bots/slack.py", "wants_claude_settings"),
        ("bots/telegram.py", "wants_claude_settings"),
        ("bots/whatsapp.py", "wants_claude_settings"),
    ):
        module = __import__(module_path.replace("/", ".")[:-3], fromlist=["x"])
        assert needle in inspect.getsource(module), f"{module_path} ignores runner kind"


def test_wants_claude_settings_is_true_only_for_claude():
    """Behavioural check on the helper itself.

    The bots' call sites are covered textually elsewhere; this asserts the
    decision, so a helper that always returned True could not pass.
    """
    from runners.base import PlatformConfig
    from runners.claude import ClaudeRunner
    from runners.local import LocalRunner

    local_runner = LocalRunner(
        name="ollama", base_url="http://localhost:11434/v1", model="test-model"
    )
    assert wants_claude_settings(ClaudeRunner()) is True
    assert wants_claude_settings(local_runner) is False
    assert wants_claude_settings(object()) is False
