"""Milestone 02 / ORCH-CLEAN-001..005 — two-phase cleanup, protections, recovery.

The protection tests are the important ones: it is cheap to ask a human and expensive to
destroy uncommitted work, so every rule here fails *closed* (refuse to delete).
"""
import subprocess
from pathlib import Path

import pytest

from core.orchestration.cleanup import (
    ProtectionError,
    check_protections,
    execute_cleanup,
    is_dirty,
    is_referenced_elsewhere,
    reconcile_orphans,
    sweep,
)
from core.orchestration.registry import (
    STATUS_CLEANED,
    STATUS_CLEANUP_FAILED,
    STATUS_CLEANUP_PENDING,
    RepoRegistry,
)
from core.orchestration.worktree import provision_worktree


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "T"], root)
    (root / "README.md").write_text("hello\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "init"], root)
    return root


@pytest.fixture
def head(repo):
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


@pytest.fixture
def registry(tmp_path):
    return RepoRegistry(tmp_path / "reg.db")


@pytest.fixture
def managed(tmp_path):
    return tmp_path / "workspaces"


def _provision(registry, repo, managed, head, **over):
    kw = dict(registry=registry, repo_root=repo, managed_root=managed,
              workflow_id="WF-001", spec_id="SPEC-001", base_commit=head)
    kw.update(over)
    return provision_worktree(**kw)


# --------------------------------------------------------------------------
# ORCH-CLEAN-001: two-phase, observable, idempotent
# --------------------------------------------------------------------------

def test_cleanup_removes_worktree_and_marks_cleaned(registry, repo, managed, head):
    rec = _provision(registry, repo, managed, head)
    path = Path(rec.worktree_path)
    assert path.exists()

    out = execute_cleanup(registry, rec.resource_id)
    assert out.cleaned is True
    assert not path.exists()
    assert registry.get(rec.resource_id).status == STATUS_CLEANED


def test_cleanup_is_idempotent(registry, repo, managed, head):
    rec = _provision(registry, repo, managed, head)
    execute_cleanup(registry, rec.resource_id)
    again = execute_cleanup(registry, rec.resource_id)
    assert again.cleaned is True


def test_begin_cleanup_marks_pending_and_counts(registry, repo, managed, head):
    rec = _provision(registry, repo, managed, head)
    registry.begin_cleanup(rec.resource_id)
    row = registry.get(rec.resource_id)
    assert row.status == STATUS_CLEANUP_PENDING
    assert row.cleanup_attempts == 1


# --------------------------------------------------------------------------
# ORCH-CLEAN-002: dirty worktrees are never silently destroyed
# --------------------------------------------------------------------------

def test_dirty_worktree_blocks_cleanup(registry, repo, managed, head):
    rec = _provision(registry, repo, managed, head)
    (Path(rec.worktree_path) / "wip.txt").write_text("unfinished work")

    out = execute_cleanup(registry, rec.resource_id)
    assert out.cleaned is False
    assert "uncommitted" in out.reason
    assert Path(rec.worktree_path).exists(), "dirty worktree must be preserved"
    assert registry.get(rec.resource_id).status == STATUS_CLEANUP_FAILED


def test_dirty_worktree_allowed_when_explicitly_authorised(registry, repo, managed, head):
    rec = _provision(registry, repo, managed, head)
    (Path(rec.worktree_path) / "wip.txt").write_text("x")
    out = execute_cleanup(registry, rec.resource_id, allow_dirty=True)
    assert out.cleaned is True


def test_is_dirty_detects_untracked_and_modified(registry, repo, managed, head):
    rec = _provision(registry, repo, managed, head)
    assert is_dirty(rec.worktree_path) is False
    (Path(rec.worktree_path) / "new.txt").write_text("x")
    assert is_dirty(rec.worktree_path) is True


# --------------------------------------------------------------------------
# ORCH-CLEAN-003: referenced worktrees are never deleted
# --------------------------------------------------------------------------

def test_referenced_worktree_blocks_cleanup(registry, repo, managed, head):
    a = _provision(registry, repo, managed, head, workflow_id="WF-001", spec_id="SPEC-001")
    assert is_referenced_elsewhere(registry, a) is False

    # Simulate a second workflow pointing at the same path (a misconfiguration we must catch).
    registry.register_provisioning(
        resource_id="R-DUP", repository_id="repo-001", repo_root=str(repo),
        workflow_id="WF-002", spec_id="SPEC-002", target_branch="main",
        base_commit=head, workflow_branch="agentgate/WF-002/SPEC-002",
        worktree_path=a.worktree_path, max_active=2,
    )
    assert is_referenced_elsewhere(registry, a) is True
    with pytest.raises(ProtectionError, match="still referenced"):
        check_protections(registry, a)


# --------------------------------------------------------------------------
# ORCH-CLEAN-004: crash recovery
# --------------------------------------------------------------------------

def test_orphan_is_detectable_after_crash(registry, repo, managed, head):
    """A row left 'provisioning' with a dead lease is a recoverable orphan."""
    registry.register_provisioning(
        resource_id="R-ORPHAN", repository_id="repo-001", repo_root=str(repo),
        workflow_id="WF-009", spec_id="SPEC-009", target_branch="main",
        base_commit=head, workflow_branch="agentgate/WF-009/SPEC-009",
        worktree_path=str(managed / "gone"), pid=999_999,   # dead pid
    )
    orphans = reconcile_orphans(registry, stale_after_seconds=0)
    assert [o.resource_id for o in orphans] == ["R-ORPHAN"]


def test_live_provisioning_is_not_treated_as_orphan(registry, repo, managed, head):
    _provision(registry, repo, managed, head)          # active, live lease
    assert reconcile_orphans(registry, stale_after_seconds=60) == []


# --------------------------------------------------------------------------
# ORCH-CLEAN-005: honest failure reporting + sweep continues
# --------------------------------------------------------------------------

def test_sweep_reports_every_outcome_and_continues(registry, repo, managed, head):
    clean = _provision(registry, repo, managed, head, workflow_id="WF-001", spec_id="S1")
    dirty = _provision(registry, repo, managed, head, workflow_id="WF-002",
                       spec_id="S2", max_active=2)
    (Path(dirty.worktree_path) / "wip.txt").write_text("x")

    outcomes = {o.resource_id: o for o in sweep(registry)}
    assert outcomes[clean.resource_id].cleaned is True
    assert outcomes[dirty.resource_id].cleaned is False
    assert registry.get(dirty.resource_id).status == STATUS_CLEANUP_FAILED


def test_failure_never_reports_complete(registry, repo, managed, head):
    rec = _provision(registry, repo, managed, head)
    (Path(rec.worktree_path) / "wip.txt").write_text("x")
    execute_cleanup(registry, rec.resource_id)
    assert registry.get(rec.resource_id).status != STATUS_CLEANED


def test_unknown_resource_returns_failure_not_exception(registry):
    out = execute_cleanup(registry, "does-not-exist")
    assert out.cleaned is False
    assert "unknown" in out.reason


# --------------------------------------------------------------------------
# Known limitation, asserted so it cannot drift silently.
# --------------------------------------------------------------------------

def test_cleanup_retains_branch_by_design(registry, repo, managed, head):
    """Cleanup removes the worktree but KEEPS the branch.

    This is deliberate (spec §10.2: never prune arbitrary branches, preserve unintegrated
    work). The consequence -- and it is a real one -- is that workflow branches accumulate
    until something reclaims them. Reclaiming a workflow's *own* reserved branch after the
    spec is finalized is not implemented in this milestone; this test documents the gap so
    it is visible rather than surprising.
    """
    rec = _provision(registry, repo, managed, head)
    branch = rec.workflow_branch
    execute_cleanup(registry, rec.resource_id)

    branches = _git(["branch", "--list", branch], repo).stdout
    assert branch in branches, "branch retention is intentional; reclamation is a later task"


# --------------------------------------------------------------------------
# ORCH-SNAP-004: snapshots are disposable and must not accumulate.
#
# Regression: snapshots were originally created but never registered, so `sweep()`
# could not see them and they survived cleanup forever. That is exactly the unbounded
# resource leak D11 makes release-blocking.
# --------------------------------------------------------------------------

def test_snapshot_is_registered_before_creation(registry, repo, managed, head):
    from core.orchestration.worktree import snapshot_worktree
    snap_path = snapshot_worktree(
        registry=registry, repo_root=repo, managed_root=managed,
        candidate_commit=head, workflow_id="WF-001", spec_id="SPEC-001",
    )
    snaps = registry.list_snapshots()
    assert len(snaps) == 1
    assert snaps[0].snapshot_path == str(snap_path)
    assert snaps[0].status == "active"


def test_snapshot_cleanup_removes_directory(registry, repo, managed, head):
    from core.orchestration.worktree import snapshot_worktree
    from core.orchestration.cleanup import execute_snapshot_cleanup

    path = snapshot_worktree(
        registry=registry, repo_root=repo, managed_root=managed,
        candidate_commit=head, workflow_id="WF-001", spec_id="SPEC-001",
    )
    rid = registry.list_snapshots()[0].resource_id
    out = execute_snapshot_cleanup(registry, rid)
    assert out.cleaned is True
    assert not path.exists()
    assert registry.get_snapshot(rid).status == STATUS_CLEANED


def test_snapshot_cleanup_is_idempotent(registry, repo, managed, head):
    from core.orchestration.worktree import snapshot_worktree
    from core.orchestration.cleanup import execute_snapshot_cleanup

    snapshot_worktree(registry=registry, repo_root=repo, managed_root=managed,
                      candidate_commit=head, workflow_id="WF-001", spec_id="SPEC-001")
    rid = registry.list_snapshots()[0].resource_id
    execute_snapshot_cleanup(registry, rid)
    assert execute_snapshot_cleanup(registry, rid).cleaned is True


def test_sweep_cleans_snapshots_too(registry, repo, managed, head):
    """REGRESSION: a snapshot must not survive a sweep."""
    from core.orchestration.worktree import snapshot_worktree

    wt = _provision(registry, repo, managed, head)
    snap = snapshot_worktree(registry=registry, repo_root=repo, managed_root=managed,
                             candidate_commit=head, workflow_id="WF-001", spec_id="SPEC-001")

    sweep(registry)

    assert not Path(wt.worktree_path).exists()
    assert not snap.exists(), "snapshot leaked: registered but not swept"
    assert registry.list_snapshots(status="active") == []


def test_duplicate_snapshot_for_same_attempt_rejected(registry, repo, managed, head):
    from core.orchestration.worktree import snapshot_worktree
    snapshot_worktree(registry=registry, repo_root=repo, managed_root=managed,
                      candidate_commit=head, workflow_id="WF-001", spec_id="SPEC-001")
    with pytest.raises(Exception):
        snapshot_worktree(registry=registry, repo_root=repo, managed_root=managed,
                          candidate_commit=head, workflow_id="WF-001", spec_id="SPEC-001")
