"""Git worktree provisioning, immutable snapshots and ownership preflight.

Every workflow gets its own worktree and its own branch (D8/D10). The user's original
checkout is never touched — that is the property most worth protecting, because a bug here
would corrupt a developer's working tree rather than a disposable artifact.

Design choices that carry the guarantees
---------------------------------------
- **Register-before-create.** The registry row is written *first* (``provisioning``), then the
  worktree is created, then the row is marked ``active``. A crash leaves a detectable orphan
  instead of an untracked worktree (ORCH-REG-003).
- **Host-generated paths only.** The worktree path and branch are derived from the managed
  root and the workflow/spec ids. A caller may not supply a path outside the managed root,
  because a model-supplied path is exactly how an agent would escape containment.
- **Detached snapshots.** ``git worktree add --detach <path> <sha>`` cannot follow a branch,
  which is what makes "review an immutable candidate" literally true (ORCH-SNAP-001/003).
- **Ownership is checked against the registry, not the filesystem.** Two workflows could
  otherwise be pointed at the same directory and quietly share a writable tree (ORCH-ISO).
"""
from __future__ import annotations

import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from core.orchestration.registry import RepoRegistry, RegistryError, WorktreeRecord

#: Reserved branch namespace (spec §8 / DEC-203).
BRANCH_NAMESPACE = "agentgate"

#: Ids are restricted so they cannot inject path separators or git options.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class WorktreeError(Exception):
    """Raised when provisioning, snapshotting or ownership checks fail."""


def _check_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.match(value):
        raise WorktreeError(
            f"{name} must be 1-64 chars of letters, digits, '.', '_' or '-' "
            f"(got {value!r})"
        )
    return value


def _run_git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[:500]}"
        )
    return proc


def branch_name(workflow_id: str, spec_id: str) -> str:
    """The reserved branch for a workflow (``agentgate/<workflow>/<spec>``)."""
    return f"{BRANCH_NAMESPACE}/{_check_id(workflow_id, 'workflow_id')}/{_check_id(spec_id, 'spec_id')}"


def worktree_path_for(managed_root: Path, workflow_id: str, spec_id: str) -> Path:
    """Host-generated worktree path. Never derived from model input beyond validated ids."""
    return Path(managed_root).resolve() / f"{workflow_id}__{spec_id}"


def _assert_inside(path: Path, root: Path) -> None:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        raise WorktreeError(f"path {path} escapes managed root {root}") from None


def provision_worktree(
    *,
    registry: RepoRegistry,
    repo_root: str | os.PathLike,
    managed_root: str | os.PathLike,
    workflow_id: str,
    spec_id: str,
    base_commit: str,
    repository_id: str = "repo-001",
    target_branch: str = "main",
    max_active: int = 1,
) -> WorktreeRecord:
    """Create (or return) the workflow's worktree on its own branch.

    Idempotent: calling twice for the same workflow/spec returns the existing ACTIVE row
    (ORCH-WT-005) rather than creating a second worktree.
    """
    _check_id(workflow_id, "workflow_id")
    _check_id(spec_id, "spec_id")
    repo_root = Path(repo_root).resolve()
    managed_root = Path(managed_root).resolve()
    managed_root.mkdir(parents=True, exist_ok=True)

    branch = branch_name(workflow_id, spec_id)

    existing = registry.find_by_branch(branch)
    if existing and existing.status == "active":
        return existing                          # idempotent

    path = worktree_path_for(managed_root, workflow_id, spec_id)
    _assert_inside(path, managed_root)

    if not _full_sha(base_commit):
        raise WorktreeError("base_commit must be a full 40-char lowercase hex SHA")
    _run_git(["rev-parse", "--verify", f"{base_commit}^{{commit}}"], repo_root)

    resource_id = f"WT-{uuid.uuid4().hex[:12]}"

    # 1. reserve the row BEFORE touching the filesystem
    registry.register_provisioning(
        resource_id=resource_id,
        repository_id=repository_id,
        repo_root=str(repo_root),
        workflow_id=workflow_id,
        spec_id=spec_id,
        target_branch=target_branch,
        base_commit=base_commit,
        workflow_branch=branch,
        worktree_path=str(path),
        max_active=max_active,
    )

    try:
        # 2. create the branch (if absent) and the worktree on it
        if _run_git(["rev-parse", "--verify", branch], repo_root, check=False).returncode != 0:
            _run_git(["branch", branch, base_commit], repo_root)
        _run_git(["worktree", "add", str(path), branch], repo_root)
        registry.mark_active(resource_id)
    except Exception as exc:
        registry.set_error(resource_id, str(exc))
        raise

    return registry.get(resource_id)  # type: ignore[return-value]


def _full_sha(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{40}", value or ""))


def snapshot_worktree(
    *,
    registry: RepoRegistry,
    repo_root: str | os.PathLike,
    managed_root: str | os.PathLike,
    candidate_commit: str,
    workflow_id: str,
    spec_id: str,
    attempt_id: str = "001",
) -> Path:
    """Create a **detached**, read-only snapshot of an exact candidate commit (ORCH-SNAP-*.).

    Detached so it cannot follow a moving branch; read-only so a reviewer cannot modify the
    evidence it is reviewing.
    """
    _check_id(workflow_id, "workflow_id")
    _check_id(spec_id, "spec_id")
    _check_id(attempt_id, "attempt_id")
    if not _full_sha(candidate_commit):
        raise WorktreeError("candidate_commit must be a full 40-char lowercase hex SHA")

    repo_root = Path(repo_root).resolve()
    managed_root = Path(managed_root).resolve()
    snap_root = managed_root / "_snapshots"
    snap_root.mkdir(parents=True, exist_ok=True)
    path = snap_root / f"{workflow_id}__{spec_id}__{attempt_id}"
    _assert_inside(path, managed_root)

    _run_git(["rev-parse", "--verify", f"{candidate_commit}^{{commit}}"], repo_root)
    if path.exists():
        raise WorktreeError(f"snapshot already exists at {path}")

    # Register BEFORE creating, exactly as worktrees do: a crash mid-create then leaves a
    # detectable row instead of an untracked directory that cleanup can never find.
    resource_id = f"SN-{uuid.uuid4().hex[:12]}"
    existing = registry.list_snapshots()
    for snap in existing:
        if (snap.workflow_id, snap.spec_id, snap.attempt_id) == (
                workflow_id, spec_id, attempt_id) and snap.status != "cleaned":
            raise WorktreeError(
                f"a snapshot for attempt {attempt_id!r} already exists ({snap.resource_id})"
            )
    registry.register_snapshot(
        resource_id=resource_id,
        repository_id="repo-001",
        repo_root=str(repo_root),
        workflow_id=workflow_id,
        spec_id=spec_id,
        attempt_id=attempt_id,
        candidate_commit=candidate_commit,
        snapshot_path=str(path),
    )

    try:
        _run_git(["worktree", "add", "--detach", str(path), candidate_commit], repo_root)
        _make_read_only(path)
        registry.mark_snapshot_active(resource_id)
    except Exception:
        # Leave the row in provisioning so reconciliation can find the partial artifact.
        raise
    return path


def _make_read_only(path: Path) -> None:
    """Strip write bits recursively. Best-effort: mode is not a mount boundary."""
    for root, dirs, files in os.walk(path):
        for name in files:
            try:
                os.chmod(os.path.join(root, name), 0o444)
            except OSError:
                pass
        for name in dirs:
            try:
                os.chmod(os.path.join(root, name), 0o555)
            except OSError:
                pass


def assert_workflow_owns_path(
    registry: RepoRegistry, workflow_id: str, path: str | os.PathLike
) -> WorktreeRecord:
    """Refuse to act on a path owned by a different workflow (ORCH-ISO-001)."""
    target = str(Path(path).resolve())
    for rec in registry.list_for_workflow(workflow_id):
        if str(Path(rec.worktree_path).resolve()) == target:
            return rec
    # Either unregistered or owned by someone else — both are refusals.
    raise WorktreeError(
        f"workflow {workflow_id!r} does not own {target}"
    )


def assert_branch_allowed(workflow_id: str, spec_id: str, branch: str) -> None:
    """Reject a branch switch outside the reserved namespace (ORCH-ISO-002)."""
    expected = branch_name(workflow_id, spec_id)
    if branch != expected:
        raise WorktreeError(
            f"branch {branch!r} is outside the reserved namespace for this workflow "
            f"(expected {expected!r})"
        )
