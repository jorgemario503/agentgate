"""
Session registry (persisted to disk), per-thread workspace setup,
Claude settings writer, and the idle-session reaper.

Session identity — the one thing to understand before reading further
--------------------------------------------------------------------
There are three different identifiers in play and they are not interchangeable.

**Address** (``thread_id``) — where the conversation lives. It is produced by
the bot, not by the agent, and it is *not* a session:

============================  =========================================
Slack                          ``thread_ts or ts`` — new thread, new address
Telegram                       ``str(chat_id)`` — one address per chat, forever
WhatsApp                       ``phone`` — one address per phone, forever
============================  =========================================

The address is the durable key: history files, workspaces, AgentDB records and
the provider cache key are all named after it.

**Session** — one continuous conversation *in a process*. For the open-model
runners the session id **is** the address (they pass ``thread_id`` straight
through), so the only thing that distinguishes a new session from a later turn
is whether this process still holds a client for that address. For Claude the
SDK mints a real UUID and ``resume=`` carries it back.

**Provider cache key** — ``core.context.cache_key`` sends the address as
DeepInfra's ``prompt_cache_key`` / Fireworks' ``x-session-affinity`` on *every*
message. Because the address is permanent for Telegram and WhatsApp, so is the
cache lineage there: a new session mutates the prefix of an existing lineage
rather than starting a fresh one. That is why per-session text must travel in
the ephemeral context and never in the cached system prefix.

Session states
--------------
A session state answers exactly one question: **does this turn stand at a
session boundary, or inside one?** It is derived from the registry row plus
whether this process holds a live client. It is deliberately *not* influenced by
the idle reaper — going idle closes a client, which is a fact about this process
and is already visible as "no client". Whether the user was *told* about it is
presentation, not lifecycle, and is carried separately by
``SessionStore.idle_notified_at``.
"""
import asyncio
import json
import os
import time
from enum import Enum
from typing import Optional

from core.config import (
    CLAUDE_SETTINGS,
    IDLE_TIMEOUT_SECONDS,
    REAPER_INTERVAL_SECONDS,
)


# Threads that opted out of the idle reaper via /idle stop.
# In-memory only: resets on restart, like auto-approval.
_keep_alive: set[str] = set()

#: Registry key recording *when* the reaper last told the user a session went
#: idle. Presentation only: no session state or injection decision reads it.
#: Rows written before this rename said ``"expired": true``; both are accepted.
IDLE_NOTIFIED_KEY = "idle_notified_at"
_LEGACY_EXPIRED_KEY = "expired"


class SessionState(Enum):
    """Where a turn sits relative to the session it belongs to.

    Exactly three states, because there are exactly two lifecycle facts that
    change what a turn needs: whether a session is starting, and whether this
    process still has it open.

    ``SESSION_START`` covers a first-ever message *and* the first message after
    ``/stop``: the registry row is removed by ``/stop``, so the two are the same
    fact from the registry's point of view. They are the same state on purpose —
    both need a full context injection and the same opener.

    ``SESSION_RESUME`` covers a row that still exists while this process holds no
    client. That happens on a process restart, and after a turn failed and the
    runner closed its client. Both mean the same thing to a turn: prior context
    exists and must be re-supplied.
    """

    SESSION_START = "session_start"
    SESSION_RESUME = "session_resume"
    SESSION_WARM = "session_warm"

    @property
    def is_boundary(self) -> bool:
        """True at a session start or resume — the only time to inject
        per-session context (repo map, orientation, resume block).

        ``SESSION_WARM`` is a later turn inside an open session; per-session
        context was already delivered on the turn that opened it.
        """
        return self is not SessionState.SESSION_WARM

    def opener(self, *, idle_notified: bool = False) -> str:
        """User-facing status line; empty for a warm turn (already connected).

        ``idle_notified`` distinguishes "we closed your idle session and told
        you so" from other resumptions. It is a parameter rather than part of
        the state so that a notification can never change what gets injected.
        """
        if self is SessionState.SESSION_WARM:
            return ""
        if self is SessionState.SESSION_START:
            return "🚀 Starting agent session"
        return "⏰ Session went idle — resuming" if idle_notified else "🔄 Resuming session"


def classify_session(meta: Optional[dict], has_client: bool) -> SessionState:
    """Classify a thread into a `SessionState`.

    `meta` is the registry entry (None when the thread was never registered or
    was removed by `/stop`); `has_client` is `runner.has_client(thread_id)`.

    Note what is *absent* from this function: the idle marker. A row the reaper
    has flagged classifies exactly like any other row without a client, because
    the reaper's only lifecycle effect is already captured by ``has_client``.
    """
    if not meta:
        return SessionState.SESSION_START
    if has_client:
        return SessionState.SESSION_WARM
    return SessionState.SESSION_RESUME


def idle_notified_at(meta: Optional[dict]) -> Optional[float]:
    """When the reaper last notified the user this session went idle, if ever.

    Reads the current key and the legacy ``expired`` boolean, so rows written by
    an older process keep their meaning. A legacy ``expired`` row records no
    timestamp, so its ``last_activity`` stands in for one.
    """
    if not meta:
        return None
    if IDLE_NOTIFIED_KEY in meta:
        return meta.get(IDLE_NOTIFIED_KEY)
    if meta.get(_LEGACY_EXPIRED_KEY):
        return meta.get("last_activity")
    return None



def enable_keep_alive(thread_id: str) -> None:
    """Stop the idle reaper from expiring this thread."""
    _keep_alive.add(thread_id)


def disable_keep_alive(thread_id: str) -> None:
    _keep_alive.discard(thread_id)


def is_keep_alive(thread_id: str) -> bool:
    return thread_id in _keep_alive


class SessionStore:
    """
    One instance per platform (Slack, Telegram, ...). Each instance owns
    its own registry file, directory-lookup file, and workspace tree under
    `base`, so two platforms never collide on the same thread/chat ID.
    """

    def __init__(self, base, registry_filename="sessions.json",
                 directories_filename="directories.json"):
        self.base = base
        self.registry_file = f"{base}/{registry_filename}"
        self.directories_file = f"{base}/{directories_filename}"

    # -------------------------
    # REGISTRY
    # -------------------------
    def load_registry(self) -> dict:
        os.makedirs(self.base, exist_ok=True)
        try:
            with open(self.registry_file) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save_registry(self, registry: dict) -> None:
        with open(self.registry_file, "w") as f:
            json.dump(registry, f, indent=2)

    def register_session(self, thread_id, channel, workspace_path, session_id=None):
        registry = self.load_registry()
        existing_session_id = registry.get(thread_id, {}).get("session_id")
        registry[thread_id] = {
            "channel": channel,
            "workspace_path": workspace_path,
            "session_id": session_id or existing_session_id,
            "started_at": time.time(),
            "last_activity": time.time(),
        }
        self.save_registry(registry)

    def update_session_id(self, thread_id, session_id):
        registry = self.load_registry()
        if thread_id in registry:
            registry[thread_id]["session_id"] = session_id
            registry[thread_id]["last_activity"] = time.time()
            self.save_registry(registry)

    def touch_session(self, thread_id):
        registry = self.load_registry()
        if thread_id in registry:
            registry[thread_id]["last_activity"] = time.time()
            self.save_registry(registry)

    def unregister_session(self, thread_id):
        registry = self.load_registry()
        registry.pop(thread_id, None)
        self.save_registry(registry)

    def mark_idle_notified(self, thread_id, when: Optional[float] = None):
        """Record that the user was told this session went idle.

        Purely informational: it drives the opener text and `/session status`.
        It does not change the session state — the reaper closed the client,
        and "no client" already says everything the lifecycle needs.

        Rewrites the row in the current shape, so a legacy ``expired`` flag is
        migrated away the first time this runs.
        """
        registry = self.load_registry()
        meta = registry.get(thread_id)
        if meta is None:
            return
        meta.pop(_LEGACY_EXPIRED_KEY, None)
        meta[IDLE_NOTIFIED_KEY] = when if when is not None else time.time()
        meta["last_activity"] = time.time()
        registry[thread_id] = meta
        self.save_registry(registry)

    def idle_notified_at(self, thread_id) -> Optional[float]:
        """When the reaper last notified this thread, or None."""
        return idle_notified_at(self.get_session_meta(thread_id))

    # Retained as a thin alias: the reaper's method name is part of its call
    # shape in existing tests and subclasses.
    _expire_session = mark_idle_notified

    def get_session_meta(self, thread_id) -> dict | None:
        return self.load_registry().get(thread_id)

    # -------------------------
    # DIRECTORY LOOKUP REGISTRY
    # -------------------------
    def load_directories(self) -> dict:
        os.makedirs(self.base, exist_ok=True)
        try:
            with open(self.directories_file) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save_directories(self, directories: dict) -> None:
        os.makedirs(self.base, exist_ok=True)
        with open(self.directories_file, "w") as f:
            json.dump(directories, f, indent=2)

    def lookup_directory(self, name: str) -> str | None:
        return self.load_directories().get(name)

    def remember_directory(self, name: str, path: str) -> None:
        directories = self.load_directories()
        directories[name] = path
        self.save_directories(directories)

    # -------------------------
    # CLAUDE SETTINGS WRITER
    # -------------------------
    def write_claude_settings(self, workspace_path: str) -> None:
        settings_dir = os.path.join(workspace_path, ".claude")
        os.makedirs(settings_dir, exist_ok=True)
        with open(os.path.join(settings_dir, "settings.json"), "w") as f:
            json.dump(CLAUDE_SETTINGS, f, indent=2)

    # -------------------------
    # WORKSPACE SETUP
    # -------------------------
    def ensure_workspace(self, thread_id: str, claude_settings: bool = True) -> str:
        """Create the thread's workspace.

        `claude_settings` writes `.claude/settings.json`, which the Claude
        runner picks up as its project settings in the cwd. The other runners
        never read it, so the bots pass False for them. Defaults to True so an
        existing caller keeps the old behaviour.
        """
        workspace_path = f"{self.base}/{thread_id}/workspace"
        os.makedirs(workspace_path, exist_ok=True)
        if claude_settings:
            self.write_claude_settings(workspace_path)
        return workspace_path

    # -------------------------
    # AUDIT LOG
    # -------------------------
    def append_audit_log(self, thread_id: str, event: str, payload: dict) -> None:
        thread_dir = f"{self.base}/{thread_id}"
        os.makedirs(thread_dir, exist_ok=True)
        entry = {"ts": time.time(), "event": event, **payload}
        with open(os.path.join(thread_dir, "audit.log"), "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    # -------------------------
    # IDLE SESSION REAPER
    # -------------------------
    async def reaper(self, close_client_fn, notify_fn):
        while True:
            await asyncio.sleep(REAPER_INTERVAL_SECONDS)
            registry = self.load_registry()
            now = time.time()

            for thread_id, meta in list(registry.items()):
                if is_keep_alive(thread_id):
                    continue
                idle = now - meta["last_activity"]
                if idle > IDLE_TIMEOUT_SECONDS:
                    print(f"[reaper] Expiring session {thread_id} (idle {idle:.0f}s)")
                    # The lifecycle effect: the client goes away, which is what
                    # makes the next turn a SESSION_RESUME.
                    try:
                        await close_client_fn(thread_id)
                    except Exception as e:
                        print(f"[reaper] Error closing client {thread_id}: {e}")
                    # The presentation effect: remember that the user was told.
                    self.mark_idle_notified(thread_id)
                    try:
                        await notify_fn(
                            meta["channel"], thread_id,
                            f"⏰ Session went idle after "
                            f"{idle / 3600:.1f}h. Message me again to resume."
                        )
                    except Exception as e:
                        print(f"[reaper] Failed to notify {thread_id}: {e}")


def wants_claude_settings(runner) -> bool:
    """True when the runner will read `.claude/settings.json` from its cwd.

    Only the Claude runner does. Kept here so the bots share one check rather
    than importing ClaudeRunner each time.
    """
    from runners.claude import ClaudeRunner

    return isinstance(runner, ClaudeRunner)
