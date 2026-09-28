"""
Session registry (persisted to disk), per-thread workspace setup,
Claude settings writer, and the idle-session reaper.
"""
import asyncio
import json
import os
import time

from core.config import (
    CLAUDE_SETTINGS,
    IDLE_TIMEOUT_SECONDS,
    REAPER_INTERVAL_SECONDS,
)


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
    def ensure_workspace(self, thread_id: str) -> str:
        workspace_path = f"{self.base}/{thread_id}/workspace"
        os.makedirs(workspace_path, exist_ok=True)
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
