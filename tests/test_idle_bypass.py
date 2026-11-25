"""Tests for /idle start|stop — opting a chat out of the idle reaper."""

import asyncio
import time

import pytest

import core.commands as commands
from core import sessions
from core.config import IDLE_TIMEOUT_SECONDS
from runners.base import PlatformConfig


class FakeStore:
    base = "/tmp/agentgate-idle-test"

    def __init__(self):
        self.audit = []

    def append_audit_log(self, *args, **kwargs):
        self.audit.append((args, kwargs))


class FakeRunner:
    async def cancel(self, thread_id):
        return True

    def list_pending_conflicts(self):
        return []


@pytest.fixture(autouse=True)
def _reset():
    commands.use_runner(FakeRunner())
    sessions._keep_alive.clear()
    yield
    sessions._keep_alive.clear()


def ctx(store, thread_id="t1", user_id="u1"):
    platform = PlatformConfig(
        session_store=store,
        agent_id="a",
        admin_user_ids=frozenset({"u1"}),
        allowed_user_ids=frozenset({"u1"}),
    )
    return commands.CommandContext(
        platform=platform, thread_id=thread_id, user_id=user_id, session_store=store
    )


def run(cmd, context):
    return asyncio.run(commands.dispatch(cmd, context))


# -- command behaviour ------------------------------------------------------

def test_idle_stop_enables_flag():
    store = FakeStore()
    reply = run("idle stop", ctx(store))
    assert sessions.is_keep_alive("t1")
    assert "disabled" in reply.lower()


def test_idle_start_disables_flag():
    store = FakeStore()
    run("idle stop", ctx(store))
    reply = run("idle start", ctx(store))
    assert not sessions.is_keep_alive("t1")
    assert "restored" in reply.lower()


def test_idle_without_args_reports_state():
    store = FakeStore()
    assert "currently start" in run("idle", ctx(store))
    run("idle stop", ctx(store))
    assert "currently stop" in run("idle", ctx(store))


def test_idle_rejects_unknown_arg():
    reply = run("idle maybe", ctx(FakeStore()))
    assert reply.startswith("Usage: idle")


def test_idle_is_scoped_per_thread():
    store = FakeStore()
    run("idle stop", ctx(store, thread_id="t1"))
    assert sessions.is_keep_alive("t1")
    assert not sessions.is_keep_alive("t2")


def test_idle_toggle_is_audited():
    store = FakeStore()
    run("idle stop", ctx(store))
    run("idle start", ctx(store))
    states = [a[0][2]["state"] for a in store.audit]
    assert states == ["on", "off"]


# -- reaper integration -----------------------------------------------------

def _expire_due_registry(store, monkeypatch, thread_ids):
    """Drive one reaper iteration against a registry older than the timeout."""
    stale = time.time() - IDLE_TIMEOUT_SECONDS - 60
    monkeypatch.setattr(
        type(store), "load_registry",
        lambda self: {tid: {"last_activity": stale, "channel": "test"} for tid in thread_ids},
        raising=False,
    )
    monkeypatch.setattr(sessions, "REAPER_INTERVAL_SECONDS", 0)
    closed = []

    async def close_client(tid):
        closed.append(tid)

    async def notify(*args, **kwargs):
        pass

    async def drive():
        task = asyncio.create_task(store.reaper(close_client, notify))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    notified = []
    monkeypatch.setattr(type(store), "mark_idle_notified", lambda self, tid: notified.append(tid), raising=False)
    asyncio.run(drive())
    return closed, notified


def _drive_reaper(monkeypatch, thread_ids, tmp_path):
    """Run one real reaper iteration with a registry older than the timeout."""
    from core.sessions import SessionStore

    store = SessionStore(base=str(tmp_path))
    stale = time.time() - IDLE_TIMEOUT_SECONDS - 60
    monkeypatch.setattr(
        store, "load_registry",
        lambda: {tid: {"last_activity": stale, "channel": "test"} for tid in thread_ids},
    )
    notified = []
    monkeypatch.setattr(store, "mark_idle_notified", lambda tid: notified.append(tid))
    monkeypatch.setattr(sessions, "REAPER_INTERVAL_SECONDS", 0)
    closed = []

    async def close_client(tid):
        closed.append(tid)

    async def notify(*args, **kwargs):
        pass

    async def drive():
        task = asyncio.create_task(store.reaper(close_client, notify))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    return closed, notified


def test_reaper_expires_idle_thread_without_flag(monkeypatch, tmp_path):
    closed, notified = _drive_reaper(monkeypatch, ["t1"], tmp_path)
    assert set(closed) == {"t1"}
    assert set(notified) == {"t1"}


def test_reaper_skips_thread_with_keepalive_on(monkeypatch, tmp_path):
    sessions.enable_keep_alive("t1")
    closed, notified = _drive_reaper(monkeypatch, ["t1"], tmp_path)
    assert closed == []
    assert notified == []


def test_reaper_expires_again_after_keepalive_off(monkeypatch, tmp_path):
    sessions.enable_keep_alive("t1")
    sessions.disable_keep_alive("t1")
    closed, notified = _drive_reaper(monkeypatch, ["t1"], tmp_path)
    assert set(closed) == {"t1"}
