"""Tests for core/sessions.py."""
import json
import os
import pytest
from core.sessions import SessionStore


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


def test_expire_session_preserves_session_id(store):
    store.register_session("t1", "ch1", "/ws/t1", session_id="saved-id")
    store._expire_session("t1")
    meta = store.get_session_meta("t1")
    assert meta["expired"] is True
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
