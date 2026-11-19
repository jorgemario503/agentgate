"""Session state classification — spec §4, tests S1-S7.

A session's state decides whether a resume prompt is injected. The decision
input is the registry entry plus whether this process still holds a live
client for the thread; `session_id` is deliberately NOT used, because the
non-Claude runners always set it to `thread_id` and therefore cannot
distinguish a resume from a fresh start.
"""

import asyncio
import json
import time

import pytest

from core.sessions import SessionState, SessionStore, classify_session
from runners.local import JsonFileSessionStore, LocalAgentClient


@pytest.fixture
def store(tmp_path):
    return SessionStore(base=str(tmp_path / "sessions"))


def meta(**overrides):
    base = {
        "channel": "C1",
        "workspace_path": "/tmp/ws",
        "session_id": "thread-1",
        "started_at": time.time(),
        "last_activity": time.time(),
    }
    base.update(overrides)
    return base


# -- S1-S5: the five states -------------------------------------------------


def test_new_when_no_registry_entry():
    """S1: no registry entry at all."""
    assert classify_session(None, has_client=False) is SessionState.NEW


def test_warm_when_client_alive():
    """S2: registry entry and this process still holds the client."""
    assert classify_session(meta(), has_client=True) is SessionState.WARM


def test_cold_when_client_gone_but_not_expired():
    """S3: bot restarted inside the idle window - client gone, not expired."""
    assert classify_session(meta(), has_client=False) is SessionState.COLD


def test_resume_after_timeout_when_expired():
    """S4: the reaper marked it expired; the row (and history) survive."""
    assert classify_session(meta(expired=True), has_client=False) is SessionState.RESUME_AFTER_TIMEOUT


def test_expired_beats_live_client():
    """S4: an expired row is a resume even if a client somehow lingers."""
    assert classify_session(meta(expired=True), has_client=True) is SessionState.RESUME_AFTER_TIMEOUT


def test_stopped_reads_as_new(store):
    """S5: /stop deletes the registry row, so the thread reads as a new session."""
    store.register_session("thread-1", "C1", "/tmp/ws")
    assert classify_session(store.get_session_meta("thread-1"), has_client=True) is SessionState.WARM
    store.unregister_session("thread-1")
    assert classify_session(store.get_session_meta("thread-1"), has_client=False) is SessionState.NEW


# -- helpers ----------------------------------------------------------------


def test_only_cold_and_timeout_resume():
    """The resume prompt applies to exactly two states."""
    resuming = {s for s in SessionState if s.resumes}
    assert resuming == {SessionState.COLD, SessionState.RESUME_AFTER_TIMEOUT}


def test_states_report_their_opener():
    """S7: the opener is derived from the state, never from session_id.

    A non-Claude runner keeps session_id=None-or-thread_id forever, so the old
    `prior_session_id` signal reported "Starting" for a genuine resume.
    """
    assert "Resuming" in SessionState.COLD.opener
    assert "Resuming" in SessionState.RESUME_AFTER_TIMEOUT.opener
    assert "Starting" in SessionState.NEW.opener
    assert SessionState.WARM.opener == ""


# -- S6: last-2-turn selection ---------------------------------------------


def _write_history(directory, thread_id, turns):
    """Persist a conversation in the runner's on-disk shape."""
    messages = [{"role": "system", "content": "SYSTEM"}]
    for i, (user, assistant) in enumerate(turns, start=1):
        messages.append({"role": "user", "content": user})
        messages.append({"role": "assistant", "content": assistant})
    JsonFileSessionStore(directory=str(directory)).save(thread_id, messages)
    return messages


def test_history_reloads_for_existing_thread(tmp_path):
    """Baseline: a cold client reloads the whole conversation."""
    history_dir = tmp_path / "local_history"
    _write_history(history_dir, "t1", [(f"q{i}", f"a{i}") for i in range(1, 6)])

    client = LocalAgentClient(
        name="local",
        base_url="http://x",
        model="m",
        system_prompt="FRESH",
        session_id="t1",
        session_store=JsonFileSessionStore(directory=str(history_dir)),
    )
    asyncio.run(client.connect())
    contents = [m.get("content") for m in client.messages]
    assert "q5" in contents and "a5" in contents


# -- opener shown to the user ----------------------------------------------


def test_opener_uses_registry_state_not_session_id():
    """S7: a resumed non-Claude thread must say "Resuming".

    The bots used `prior_session_id` to choose the opener. The stateless
    runners always set session_id to thread_id (or leave it None), so a
    genuine resume was announced as a fresh start.
    """
    # An expired row is a resume even though session_id may be None.
    expired = classify_session(meta(session_id=None, expired=True), has_client=False)
    assert "Resuming" in expired.opener

    # No row at all is genuinely new.
    assert "Starting" in classify_session(None, has_client=False).opener


def test_bot_opener_matches_the_state_function():
    """The bots must derive the opener from classify_session."""
    import inspect

    for module_path in ("bots/slack.py", "bots/telegram.py", "bots/whatsapp.py"):
        source = inspect.getsource(__import__(module_path.replace("/", ".")[:-3], fromlist=["x"]))
        assert "classify_session" in source, f"{module_path} does not use classify_session"
        assert 'prior_session_id' not in source, (
            f"{module_path} still keys the opener off session_id"
        )
