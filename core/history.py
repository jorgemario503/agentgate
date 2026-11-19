"""Runner conversation history on disk, one naming rule for every backend.

Each stateless runner (local, fireworks, deepinfra) replays its conversation
from a JSON file keyed by thread id. The directory used to be
`{name}_history` for the local runner but hardcoded `fw_history` /
`di_history` for the other two, which forced the resume block to special-case
the path.

The rule is now `{name}_history` everywhere, so local keeps `ollama_history`
and `mlx_history` separate while each hosted provider gets its own directory.

Migrating existing installs is a manual step: move the old directory's files
into the new one. Nothing here reads or writes the old names.
"""
from __future__ import annotations

import json
from pathlib import Path


class JsonFileHistoryStore:
    """Message history for one runner, persisted as `<thread_id>.json`."""

    def __init__(
        self,
        directory: str | Path | None = None,
        directory_name: str | None = None,
        base: str | Path | None = None,
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

    @classmethod
    def for_runner(cls, base: str | Path, runner_name: str) -> "JsonFileHistoryStore":
        """Build the store for `runner_name` under a platform's session base."""
        return cls(directory=Path(base) / f"{runner_name}_history")

    def _path(self, session_id: str) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        return self.directory / f"{session_id}.json"

    def load(self, session_id: str) -> list[dict]:
        """Read a session's messages; [] when there is nothing stored."""
        path = self.directory / f"{session_id}.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def save(self, session_id: str, messages: list[dict]) -> None:
        """Write the session's messages."""
        self._path(session_id).write_text(json.dumps(messages, indent=2))
