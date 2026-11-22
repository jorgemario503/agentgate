"""Two-phase workspace cleanup and crash reconciliation (release-blocking, D11).

The rule that matters: **never destroy work that a human might still need.** A worktree that
is dirty, still referenced by another workflow, or merely *unverified* is preserved and
reported, not deleted. It is far cheaper to ask a human than to lose uncommitted changes.

Two phases, observable and idempotent (ORCH-CLEAN-001)
-----------------------------------------------------
1. ``begin_cleanup``   — mark the row ``cleanup_pending`` and increment the attempt counter.
2. ``execute_cleanup`` — re-verify every protection, remove narrowly, then ``mark_cleaned``.
                         A failed step leaves ``cleanup_failed`` with a reason.

Repeating either phase is safe: an already-clean resource is a no-op, not an error.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.orchestration.registry import (
    STATUS_ACTIVE,
    STATUS_CLEANED,
    STATUS_CLEANUP_FAILED,
    STATUS_CLEANUP_PENDING,
    STATUS_PROVISIONING,
    RepoRegistry,
    WorktreeRecord,
)


@dataclass(frozen=True)
class CleanupOutcome:
    resource_id: str
    cleaned: bool
    reason: Optional[str] = None


class ProtectionError(Exception):
    """Raised when a protection rule forbids deletion."""


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def is_dirty(worktree_path: str | Path) -> bool:
    """True when the worktree has uncommitted changes (including untracked files)."""
    p = Path(worktree_path)
    if not p.exists():
        return False
    proc = _git(["status", "--porcelain"], p)
    return bool(proc.stdout.strip())


def is_referenced_elsewhere(registry: RepoRegistry, record: WorktreeRecord) -> bool:
    """True when another workflow's active row points at the same path or branch."""
    for other in registry.list_for_workflow_any():
        if other.resource_id == record.resource_id:
            continue
        if other.status in (STATUS_CLEANED, STATUS_CLEANUP_FAILED):
            continue
        if (str(Path(other.worktree_path).resolve()) == str(Path(record.worktree_path).resolve())
                or other.workflow_branch == record.workflow_branch):
            return True
    return False


def check_protections(
    registry: RepoRegistry, record: WorktreeRecord, *, allow_dirty: bool = False
) -> None:
    """Raise :class:`ProtectionError` if the resource must NOT be removed."""
    if not allow_dirty and is_dirty(record.worktree_path):
        raise ProtectionError(
            f"worktree {record.worktree_path} has uncommitted changes; refusing to delete"
        )
    if is_referenced_elsewhere(registry, record):
        raise ProtectionError(
            f"worktree {record.resource_id} is still referenced by another active workflow"
        )


def execute_cleanup(
    registry: RepoRegistry,
    resource_id: str,
    *,
    allow_dirty: bool = False,
) -> CleanupOutcome:
    """Remove one managed worktree, honouring every protection rule.

    Never raises for an ordinary protection failure: it records ``cleanup_failed`` and
    returns a :class:`CleanupOutcome` so a sweep can continue past one bad resource.
    """
    record = registry.get(resource_id)
    if record is None:
        return CleanupOutcome(resource_id, False, "unknown resource")
    if record.status == STATUS_CLEANED:
        return CleanupOutcome(resource_id, True)          # idempotent
    if record.status == STATUS_ACTIVE or record.status == STATUS_PROVISIONING:
        registry.begin_cleanup(resource_id)

    try:
        check_protections(registry, record, allow_dirty=allow_dirty)
        _remove_worktree(registry, record)
        registry.mark_cleaned(resource_id)
        return CleanupOutcome(resource_id, True)
    except (ProtectionError, OSError) as exc:
        registry.mark_cleanup_failed(resource_id, str(exc))
        return CleanupOutcome(resource_id, False, str(exc))


def _remove_worktree(registry: RepoRegistry, record: WorktreeRecord) -> None:
    repo_root = Path(record.repo_root)
    path = Path(record.worktree_path)

    if path.exists():
        # Prefer git's own removal (keeps .git metadata consistent).
        proc = _git(["worktree", "remove", "--force", str(path)], repo_root)
        if proc.returncode != 0 and path.exists():
            shutil.rmtree(path, ignore_errors=False)
        _git(["worktree", "prune"], repo_root)
    # The branch is retained unless it is clearly this workflow's own reserved branch and
    # the caller asked for it; branch deletion is deliberately NOT automatic (spec §10.2:
    # never prune arbitrary branches, preserve unintegrated work).


def sweep(registry: RepoRegistry, *, allow_dirty: bool = False) -> list[CleanupOutcome]:
    """Attempt cleanup for everything in a pending/failed/active state.

    Reports every outcome; never stops at the first problem.
    """
    outcomes: list[CleanupOutcome] = []
    for status in (STATUS_CLEANUP_PENDING, STATUS_CLEANUP_FAILED, STATUS_ACTIVE):
        for rec in registry.list_by_status(status):
            outcomes.append(execute_cleanup(registry, rec.resource_id,
                                            allow_dirty=allow_dirty))
    return outcomes


def reconcile_orphans(
    registry: RepoRegistry, *, stale_after_seconds: float = 300.0
) -> list[WorktreeRecord]:
    """Find ``provisioning`` rows whose owner looks dead (crash recovery, ORCH-CLEAN-004).

    Returns them for a human/next step to dispose of; deliberately does not delete.
    """
    return [
        rec for rec in registry.find_orphans()
        if registry.is_lease_stale(rec.resource_id, max_silence_seconds=stale_after_seconds)
    ]
