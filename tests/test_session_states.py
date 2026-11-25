"""Session state classification — spec §4, tests S1-S7.

A session state answers one question: does this turn stand at a session
boundary, or inside one? The decision input is the registry entry plus whether
this process still holds a live client for the thread; `session_id` is
deliberately NOT used, because the non-Claude runners always set it to
`thread_id` and therefore cannot distinguish a resume from a fresh start.

Three states, not four. The idle reaper used to contribute a fourth
(`RESUME_AFTER_TIMEOUT`) through an `expired` flag. That was redundant: the
reaper's lifecycle effect is closing the client, which is already visible as
"no client". Whether the user was *told* is presentation and is carried by
`idle_notified_at`, which no state or injection decision reads.
"""

import asyncio

import time

import pytest

from core.sessions import (
    IDLE_NOTIFIED_KEY, SessionState, SessionStore, classify_session,
    idle_notified_at,
)
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


# -- S1-S3: the three states ------------------------------------------------


def test_start_when_no_registry_entry():
    """S1: no registry entry at all."""
    assert classify_session(None, has_client=False) is SessionState.SESSION_START


def test_warm_when_client_alive():
    """S2: registry entry and this process still holds the client."""
    assert classify_session(meta(), has_client=True) is SessionState.SESSION_WARM


def test_resume_when_client_gone():
    """S3: row survives, client does not — a restart or a failed turn."""
    assert classify_session(meta(), has_client=False) is SessionState.SESSION_RESUME


def test_stopped_reads_as_start(store):
    """S5: /stop deletes the registry row, so the thread reads as a new session."""
    store.register_session("thread-1", "C1", "/tmp/ws")
    assert classify_session(store.get_session_meta("thread-1"), has_client=True) is SessionState.SESSION_WARM
    store.unregister_session("thread-1")
    assert classify_session(store.get_session_meta("thread-1"), has_client=False) is SessionState.SESSION_START


# -- the idle marker is presentation, not lifecycle -------------------------


def test_idle_marker_does_not_change_state():
    """The reaper's notification must not invent a fourth state.

    Retiring RESUME_AFTER_TIMEOUT: a row flagged idle classifies exactly like
    any other row with no client, because the lifecycle effect (client closed)
    is already what `has_client=False` says.
    """
    flagged = meta(**{IDLE_NOTIFIED_KEY: time.time()})
    assert classify_session(flagged, has_client=False) is SessionState.SESSION_RESUME
    assert classify_session(flagged, has_client=False) is classify_session(meta(), has_client=False)


def test_idle_marker_never_outranks_a_live_client():
    """A stale notification cannot make a connected session look resumable."""
    flagged = meta(**{IDLE_NOTIFIED_KEY: time.time()})
    assert classify_session(flagged, has_client=True) is SessionState.SESSION_WARM


def test_legacy_expired_row_is_read_as_idle_notified():
    """Rows written before the rename still mean "the user was told"."""
    legacy = meta(expired=True)
    assert idle_notified_at(legacy) == legacy["last_activity"]
    assert classify_session(legacy, has_client=False) is SessionState.SESSION_RESUME
    assert idle_notified_at(meta()) is None
    assert idle_notified_at(None) is None


def test_mark_idle_notified_migrates_the_legacy_key(store):
    """Writing the new key drops the legacy one, so rows age out naturally."""
    store.register_session("t1", "C1", "/tmp/ws")
    registry = store.load_registry()
    registry["t1"]["expired"] = True          # simulate an old process's row
    store.save_registry(registry)

    when = 1234567890.0
    store.mark_idle_notified("t1", when=when)

    row = store.get_session_meta("t1")
    assert row[IDLE_NOTIFIED_KEY] == when
    assert "expired" not in row
    assert store.idle_notified_at("t1") == when


def test_mark_idle_notified_ignores_unknown_threads(store):
    """The reaper may race /stop; marking a removed row must be a no-op."""
    store.mark_idle_notified("never-registered")
    assert store.get_session_meta("never-registered") is None


# -- helpers ----------------------------------------------------------------


def test_boundary_is_start_and_resume():
    """Per-session context is injected at exactly the two boundary states."""
    boundary = {s for s in SessionState if s.is_boundary}
    assert boundary == {SessionState.SESSION_START, SessionState.SESSION_RESUME}
    assert not SessionState.SESSION_WARM.is_boundary


def test_states_report_their_opener():
    """S7: the opener is derived from the state, never from session_id.

    A non-Claude runner keeps session_id=None-or-thread_id forever, so the old
    `prior_session_id` signal reported "Starting" for a genuine resume.
    """
    assert "Resuming" in SessionState.SESSION_RESUME.opener()
    assert "Starting" in SessionState.SESSION_START.opener()
    assert SessionState.SESSION_WARM.opener() == ""


def test_idle_opener_is_distinct():
    """Idle expiry still reads differently — as presentation, not as a state."""
    base = SessionState.SESSION_RESUME.opener()
    idle = SessionState.SESSION_RESUME.opener(idle_notified=True)
    assert idle != base
    assert "idle" in idle.lower()
    assert "Resuming" in base
    # A start is never announced as a resumption, idle or not.
    assert SessionState.SESSION_START.opener(idle_notified=True) == SessionState.SESSION_START.opener()


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
    # A row with no client is a resume even though session_id may be None.
    resumed = classify_session(meta(session_id=None), has_client=False)
    assert "Resuming" in resumed.opener()

    # No row at all is genuinely new.
    assert "Starting" in classify_session(None, has_client=False).opener()


def test_bot_opener_matches_the_state_function():
    """The bots must derive the opener from classify_session."""
    import inspect

    for module_path in ("bots/slack.py", "bots/telegram.py", "bots/whatsapp.py"):
        source = inspect.getsource(__import__(module_path.replace("/", ".")[:-3], fromlist=["x"]))
        assert "classify_session" in source, f"{module_path} does not use classify_session"
        assert 'prior_session_id' not in source, (
            f"{module_path} still keys the opener off session_id"
        )
