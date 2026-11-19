"""
Session registry (persisted to disk), per-thread workspace setup,
Claude settings writer, and the idle-session reaper.
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


class SessionState(Enum):
    """How a thread relates to the session it may resume.

    The classification answers one question: should this turn inject a resume
    prompt? It is derived from the registry entry plus whether *this process*
    still holds a live client — never from the session id, because the
    non-Claude runners always pass `thread_id` as the session id and so cannot
    distinguish a resume from a fresh start.

    `opener` is the one-line status shown to the user when a session starts;
    it is derived from the state so the two cannot drift.
    """

    NEW = "new"
    WARM = "warm"
    COLD = "cold"
    RESUME_AFTER_TIMEOUT = "resume_after_timeout"

    @property
    def resumes(self) -> bool:
        """True when the model has prior context this process must re-supply."""
        return self in (SessionState.COLD, SessionState.RESUME_AFTER_TIMEOUT)

    @property
    def opener(self) -> str:
        """User-facing status line; empty for WARM (already connected)."""
        if self is SessionState.WARM:
            return ""
        if self.resumes:
            return "🔄 Resuming session"
        return "🚀 Starting agent session"


def classify_session(meta: Optional[dict], has_client: bool) -> SessionState:
    """Classify a thread into a `SessionState`.

    `meta` is the registry entry (None when the thread was never registered or
    was removed by `/stop`); `has_client` is `runner.has_client(thread_id)`.

    An expired row outranks a live client: the reaper decided this session was
    over, and its history must be re-injected even if a stale client lingers.
    """
    if not meta:
        return SessionState.NEW
    if meta.get("expired"):
        return SessionState.RESUME_AFTER_TIMEOUT
    if has_client:
        return SessionState.WARM
    return SessionState.COLD



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

    def _expire_session(self, thread_id):
        """Mark a session as expired but retain session_id for future resumption."""
        registry = self.load_registry()
        if thread_id in registry:
            registry[thread_id]["expired"] = True
            registry[thread_id]["last_activity"] = time.time()
            self.save_registry(registry)

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
                    try:
                        await close_client_fn(thread_id)
                    except Exception as e:
                        print(f"[reaper] Error closing client {thread_id}: {e}")
                    self._expire_session(thread_id)
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
