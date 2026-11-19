"""Runner conversation history on disk, one naming rule for every backend.

Each stateless runner (local, fireworks, deepinfra) replays its conversation
from a JSON file keyed by thread id. The directory used to be
`{name}_history` for the local runner but hardcoded `fw_history` /
`di_history` for the other two, which forced the resume block to special-case
the path.

The rule is now `{name}_history` everywhere. Because renaming a directory
would orphan conversations written before the change, a store may be given a
`legacy_directory`: when the new directory holds no file for a session, reads
fall back to the legacy one. Writes always go to the new directory, so a
session migrates on its next save. Nothing is ever moved or deleted — the
legacy file is left exactly as it was.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


class JsonFileHistoryStore:
    """Message history for one runner, persisted as `<thread_id>.json`."""

    def __init__(
        self,
        directory: str | Path | None = None,
        directory_name: str | None = None,
        base: str | Path | None = None,
        legacy_directory: str | Path | None = None,
    ) -> None:
        """Resolve the directory from either an explicit path or a base + name.

        `directory` wins when given; otherwise `base / directory_name` is used,
        which is how the runners build `agent-sessions/<platform>/<name>_history`.
        """
        if directory is None:
            if base is None or directory_name is None:
                raise ValueError(
                    "pass directory=, or both base= and directory_name="
                )
            directory = Path(base) / directory_name
        self.directory = Path(directory)
        self.legacy_directory: Optional[Path] = (
            Path(legacy_directory) if legacy_directory else None
        )

    @classmethod
    def for_runner(
        cls,
        base: str | Path,
        runner_name: str,
        legacy_name: str | None = None,
    ) -> "JsonFileHistoryStore":
        """Build the store for `runner_name` under a platform's session base."""
        return cls(
            directory=Path(base) / f"{runner_name}_history",
            legacy_directory=(Path(base) / legacy_name) if legacy_name else None,
        )

    def _path(self, session_id: str) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        return self.directory / f"{session_id}.json"

    def _legacy_path(self, session_id: str) -> Optional[Path]:
        if self.legacy_directory is None:
            return None
        path = self.legacy_directory / f"{session_id}.json"
        return path if path.exists() else None

    def load(self, session_id: str) -> list[dict]:
        """Read a session's messages, falling back to the legacy directory."""
        path = self.directory / f"{session_id}.json"
        if not path.exists():
            legacy = self._legacy_path(session_id)
            if legacy is None:
                return []
            path = legacy
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def save(self, session_id: str, messages: list[dict]) -> None:
        """Write to the current directory only; the legacy file is untouched."""
        self._path(session_id).write_text(json.dumps(messages, indent=2))
