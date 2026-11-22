"""Durable, cross-process workspace registry (SQLite).

Why a registry at all
---------------------
Several orchestrator processes may work on different specs of the same repository at the
same time (D8). They must agree about who owns which worktree and branch, and that agreement
must survive crashes. A module-level dict cannot do that; a timestamp file cannot do it
safely. SQLite gives us transactional writes and, in WAL mode, concurrent readers with a
single writer.

What this module guarantees
--------------------------
- **Register-before-create.** A row is written in ``provisioning`` state *before* the
  worktree is created, in the same logical step. A crash therefore leaves a detectable
  orphan rather than an untracked worktree.
- **Leases, not timestamps.** A lease records PID, process start time and a heartbeat. A PID
  alone can be reused, and a bare timestamp cannot distinguish "slow" from "dead" — the spec
  (§8) requires recoverable leases for exactly this reason.
- **Single active worktree per workflow.** Enforced as a counted constraint on write
  (DEC-206), not merely documented.
- **No secrets, no file contents.** Only identifiers, paths, SHAs, timestamps and status.

Thread/process safety: every mutation runs inside an explicit transaction with
``BEGIN IMMEDIATE`` so two processes cannot interleave a read-then-write.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA_VERSION = 1

#: Lifecycle states for a registered resource.
STATUS_PROVISIONING = "provisioning"
STATUS_ACTIVE = "active"
STATUS_CLEANUP_PENDING = "cleanup_pending"
STATUS_CLEANUP_FAILED = "cleanup_failed"
STATUS_CLEANED = "cleaned"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worktrees (
    resource_id      TEXT PRIMARY KEY,
    repository_id    TEXT NOT NULL,
    repo_root        TEXT NOT NULL,
    workflow_id      TEXT NOT NULL,
    spec_id          TEXT NOT NULL,
    target_branch    TEXT NOT NULL,
    base_commit      TEXT NOT NULL,
    workflow_branch  TEXT NOT NULL,
    worktree_path    TEXT NOT NULL,
    status           TEXT NOT NULL,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    cleanup_attempts INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    UNIQUE (workflow_branch)
);

CREATE TABLE IF NOT EXISTS leases (
    resource_id   TEXT PRIMARY KEY REFERENCES worktrees(resource_id) ON DELETE CASCADE,
    pid           INTEGER NOT NULL,
    process_start REAL NOT NULL,
    heartbeat_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_worktrees_workflow ON worktrees(workflow_id);
CREATE INDEX IF NOT EXISTS idx_worktrees_status   ON worktrees(status);
"""


class RegistryError(Exception):
    """Raised for a registry-level conflict or invariant violation."""


@dataclass(frozen=True)
class WorktreeRecord:
    resource_id: str
    repository_id: str
    repo_root: str
    workflow_id: str
    spec_id: str
    target_branch: str
    base_commit: str
    workflow_branch: str
    worktree_path: str
    status: str
    created_at: float
    updated_at: float
    cleanup_attempts: int
    last_error: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WorktreeRecord":
        return cls(**{k: row[k] for k in row.keys()})


def process_start_time(pid: Optional[int] = None) -> float:
    """Best-effort start time for this process, used to make leases recoverable.

    Returns 0.0 when unavailable rather than guessing: a wrong start time is worse
    than an absent one, because it would make a live lease look stale.
    """
    pid = pid or os.getpid()
    try:
        import psutil  # optional
        return psutil.Process(pid).create_time()
    except Exception:
        pass
    # Portable-enough fallback: process age is unknowable here, so report 0.0.
    return 0.0


def _pid_alive(pid: int) -> bool:
    """True when a process with ``pid`` currently exists (POSIX; best effort)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, just not ours
    except OSError:
        return False
    return True


class RepoRegistry:
    """SQLite-backed registry of workflow worktrees, branches and leases."""

    def __init__(self, db_path: str | os.PathLike):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # -- plumbing ---------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
            row = cur.fetchone()
            if row is None:
                conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)",
                             (str(SCHEMA_VERSION),))
            elif int(row["value"]) != SCHEMA_VERSION:
                raise RegistryError(
                    f"registry schema_version {row['value']} != expected {SCHEMA_VERSION}"
                )

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        """Immediate transaction: two writers cannot interleave read-then-write."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    # -- queries ----------------------------------------------------------
    def get(self, resource_id: str) -> Optional[WorktreeRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM worktrees WHERE resource_id=?", (resource_id,)
            ).fetchone()
        return WorktreeRecord.from_row(row) if row else None

    def find_by_branch(self, workflow_branch: str) -> Optional[WorktreeRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM worktrees WHERE workflow_branch=?", (workflow_branch,)
            ).fetchone()
        return WorktreeRecord.from_row(row) if row else None

    def list_for_workflow(self, workflow_id: str) -> list[WorktreeRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM worktrees WHERE workflow_id=? ORDER BY created_at",
                (workflow_id,),
            ).fetchall()
        return [WorktreeRecord.from_row(r) for r in rows]

    def list_by_status(self, status: str) -> list[WorktreeRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM worktrees WHERE status=? ORDER BY created_at", (status,)
            ).fetchall()
        return [WorktreeRecord.from_row(r) for r in rows]

    def list_for_workflow_any(self) -> list[WorktreeRecord]:
        """Every registered worktree row, across all workflows."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM worktrees ORDER BY created_at").fetchall()
        return [WorktreeRecord.from_row(r) for r in rows]

    def active_worktree_count(self, workflow_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM worktrees WHERE workflow_id=? "
                "AND status IN (?,?)",
                (workflow_id, STATUS_PROVISIONING, STATUS_ACTIVE),
            ).fetchone()
        return int(row["n"])

    # -- mutations --------------------------------------------------------
    def register_provisioning(
        self,
        *,
        resource_id: str,
        repository_id: str,
        repo_root: str,
        workflow_id: str,
        spec_id: str,
        target_branch: str,
        base_commit: str,
        workflow_branch: str,
        worktree_path: str,
        max_active: int = 1,
        pid: Optional[int] = None,
    ) -> WorktreeRecord:
        """Reserve a worktree row *before* the worktree exists (DEC-206 enforced here)."""
        now = time.time()
        with self._txn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM worktrees WHERE workflow_id=? AND status IN (?,?)",
                (workflow_id, STATUS_PROVISIONING, STATUS_ACTIVE),
            ).fetchone()["n"]
            if count >= max_active:
                raise RegistryError(
                    f"workflow {workflow_id!r} already has {count} active worktree(s) "
                    f"(max {max_active})"
                )
            try:
                conn.execute(
                    """INSERT INTO worktrees
                       (resource_id, repository_id, repo_root, workflow_id, spec_id,
                        target_branch, base_commit, workflow_branch, worktree_path,
                        status, created_at, updated_at, cleanup_attempts)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                    (resource_id, repository_id, repo_root, workflow_id, spec_id,
                     target_branch, base_commit, workflow_branch, worktree_path,
                     STATUS_PROVISIONING, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise RegistryError(
                    f"branch {workflow_branch!r} is already registered"
                ) from exc
            self._write_lease(conn, resource_id, pid)
        return self.get(resource_id)  # type: ignore[return-value]

    def _write_lease(self, conn: sqlite3.Connection, resource_id: str,
                     pid: Optional[int] = None) -> None:
        pid = pid or os.getpid()
        conn.execute(
            "INSERT OR REPLACE INTO leases(resource_id,pid,process_start,heartbeat_at) "
            "VALUES (?,?,?,?)",
            (resource_id, pid, process_start_time(pid), time.time()),
        )

    def mark_active(self, resource_id: str) -> None:
        with self._txn() as conn:
            self._require(conn, resource_id)
            conn.execute(
                "UPDATE worktrees SET status=?, updated_at=? WHERE resource_id=?",
                (STATUS_ACTIVE, time.time(), resource_id),
            )

    def heartbeat(self, resource_id: str) -> None:
        with self._txn() as conn:
            self._require(conn, resource_id)
            conn.execute("UPDATE leases SET heartbeat_at=? WHERE resource_id=?",
                         (time.time(), resource_id))
            conn.execute("UPDATE worktrees SET updated_at=? WHERE resource_id=?",
                         (time.time(), resource_id))

    def set_error(self, resource_id: str, message: str) -> None:
        with self._txn() as conn:
            self._require(conn, resource_id)
            conn.execute(
                "UPDATE worktrees SET last_error=?, updated_at=? WHERE resource_id=?",
                (message[:2000], time.time(), resource_id),
            )

    def begin_cleanup(self, resource_id: str) -> None:
        with self._txn() as conn:
            self._require(conn, resource_id)
            conn.execute(
                "UPDATE worktrees SET status=?, cleanup_attempts=cleanup_attempts+1, "
                "updated_at=? WHERE resource_id=?",
                (STATUS_CLEANUP_PENDING, time.time(), resource_id),
            )

    def mark_cleaned(self, resource_id: str) -> None:
        with self._txn() as conn:
            self._require(conn, resource_id)
            conn.execute(
                "UPDATE worktrees SET status=?, updated_at=? WHERE resource_id=?",
                (STATUS_CLEANED, time.time(), resource_id),
            )

    def mark_cleanup_failed(self, resource_id: str, reason: str) -> None:
        with self._txn() as conn:
            self._require(conn, resource_id)
            conn.execute(
                "UPDATE worktrees SET status=?, last_error=?, updated_at=? "
                "WHERE resource_id=?",
                (STATUS_CLEANUP_FAILED, reason[:2000], time.time(), resource_id),
            )

    def _require(self, conn: sqlite3.Connection, resource_id: str) -> None:
        row = conn.execute("SELECT 1 FROM worktrees WHERE resource_id=?",
                           (resource_id,)).fetchone()
        if row is None:
            raise RegistryError(f"unknown resource {resource_id!r}")

    # -- recovery ---------------------------------------------------------
    def find_orphans(self, *, older_than_seconds: float = 0.0) -> list[WorktreeRecord]:
        """Rows still ``provisioning`` — i.e. a crash happened mid-create.

        Conservative by design: it reports, it does not delete. Reconciliation decides.
        """
        cutoff = time.time() - older_than_seconds
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM worktrees WHERE status=? AND updated_at<=?",
                (STATUS_PROVISIONING, cutoff),
            ).fetchall()
        return [WorktreeRecord.from_row(r) for r in rows]

    def is_lease_stale(self, resource_id: str, *, max_silence_seconds: float) -> bool:
        """True when the owning process looks dead (or has stopped heart-beating)."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM leases WHERE resource_id=?",
                               (resource_id,)).fetchone()
        if row is None:
            return True
        if time.time() - row["heartbeat_at"] > max_silence_seconds:
            return True
        return not _pid_alive(row["pid"])
