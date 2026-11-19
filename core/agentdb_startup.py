"""Serialise AgentDB startup across processes that share one workspace.

Every bot builds its own ``AgentDB``, and that constructor opens several SQLite
connections (memory store, audit log, system stats, skills). Each connection
runs ``PRAGMA journal_mode=WAL``, which needs an exclusive lock. ``run.py``
starts bots in parallel, so two bots pointed at the same ``AGENTDB_MEMORY_DIR``
can collide and one dies with ``sqlite3.OperationalError: database is locked``.

Two complementary measures live here:

* :func:`agentdb_startup_lock` — a file lock that lets only one process initialise
  AgentDB at a time. Startup is short, so a short serialisation is imperceptible.
* :func:`open_agentdb` — retries the constructor when the lock still fails,
  covering bot runs that bypass ``run.py`` (``python -m bots.telegram``).

The lock file lives beside the database, not in the repository root, so separate
workspaces never block each other.
"""

from __future__ import annotations

import contextlib
import fcntl
import time
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

T = TypeVar("T")

#: How long to wait for another process to finish initialising AgentDB.
LOCK_TIMEOUT_SECONDS = 30.0

#: Retry schedule for the constructor itself, in seconds.
RETRY_DELAYS = (0.1, 0.2, 0.4, 0.8, 1.6, 3.2)


def _lock_path(memory_dir: str | Path) -> Path:
    """The lock lives next to the database, so separate workspaces stay independent."""
    return Path(memory_dir) / "database" / "agentdb.startup.lock"


@contextlib.contextmanager
def agentdb_startup_lock(memory_dir: str | Path) -> Iterator[None]:
    """Hold an exclusive cross-process lock for the duration of AgentDB setup.

    Falls back to running without the lock if the file system does not support
    ``fcntl`` (non-POSIX), so startup never becomes more fragile than before.
    """
    lock_file = _lock_path(memory_dir)
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        handle = lock_file.open("w")
    except OSError:
        yield
        return

    try:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    # Give up waiting and proceed: a slow peer must not stop startup.
                    break
                time.sleep(0.05)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _is_lock_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def open_agentdb(factory: Callable[[], T], memory_dir: str | Path) -> T:
    """Build an ``AgentDB`` without racing other processes for the same file.

    ``factory`` is a zero-argument callable so this module stays free of an
    agentdb import; it also keeps the retry logic testable.
    """
    with agentdb_startup_lock(memory_dir):
        last_error: BaseException | None = None
        for delay in (0.0, *RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                return factory()
            except Exception as exc:  # noqa: BLE001 - re-raised below when not a lock
                if not _is_lock_error(exc):
                    raise
                last_error = exc
        assert last_error is not None
        raise last_error
