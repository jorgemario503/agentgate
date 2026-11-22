"""Milestone 02 / ORCH-WT-001..005, ORCH-SNAP-001..003, ORCH-ISO-001..003.

These tests use a **real temporary git repository**, not mocks: Git semantics (worktrees,
detached HEAD, branch refs) are precisely what is under test, and a mock would prove nothing
about them.
"""
import subprocess
from pathlib import Path

import pytest

from core.orchestration.registry import RepoRegistry
from core.orchestration.worktree import (
    WorktreeError,
    assert_branch_allowed,
    assert_workflow_owns_path,
    branch_name,
    provision_worktree,
    snapshot_worktree,
    worktree_path_for,
)

SHA_A = "a" * 40


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
    kw = dict(
        registry=registry, repo_root=repo, managed_root=managed,
        workflow_id="WF-001", spec_id="SPEC-001", base_commit=head,
    )
    kw.update(over)
    return provision_worktree(**kw)


# --------------------------------------------------------------------------
# ORCH-WT-001: distinct from the user's checkout; user repo untouched
# --------------------------------------------------------------------------

def test_worktree_is_separate_from_user_checkout(registry, repo, managed, head):
    rec = _provision(registry, repo, managed, head)
    assert Path(rec.worktree_path).resolve() != repo.resolve()
    assert Path(rec.worktree_path).is_dir()
    assert (Path(rec.worktree_path) / "README.md").exists()


def test_user_checkout_status_unchanged(registry, repo, managed, head):
    before = _git(["status", "--porcelain"], repo).stdout
    _provision(registry, repo, managed, head)
    after = _git(["status", "--porcelain"], repo).stdout
    assert before == after == ""


def test_worktree_listed_by_git(registry, repo, managed, head):
    rec = _provision(registry, repo, managed, head)
    out = _git(["worktree", "list"], repo).stdout
    assert "agentgate/WF-001/SPEC-001" in out


# --------------------------------------------------------------------------
# ORCH-WT-002/003: reserved namespace, host-generated paths
# --------------------------------------------------------------------------

def test_branch_uses_reserved_namespace(registry, repo, managed, head):
    rec = _provision(registry, repo, managed, head)
    assert rec.workflow_branch == "agentgate/WF-001/SPEC-001"
    assert branch_name("WF-002", "SPEC-009") == "agentgate/WF-002/SPEC-009"


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", "x" * 80, "-rf"])
def test_unsafe_ids_rejected(registry, repo, managed, head, bad):
    with pytest.raises(WorktreeError):
        _provision(registry, repo, managed, head, workflow_id=bad)


def test_worktree_path_is_within_managed_root(registry, repo, managed, head):
    rec = _provision(registry, repo, managed, head)
    Path(rec.worktree_path).resolve().relative_to(managed.resolve())


# --------------------------------------------------------------------------
# ORCH-WT-004: pinned to base commit
# --------------------------------------------------------------------------

def test_worktree_head_matches_base(registry, repo, managed, head):
    rec = _provision(registry, repo, managed, head)
    assert _git(["rev-parse", "HEAD"], rec.worktree_path).stdout.strip() == head


def test_invalid_sha_rejected(registry, repo, managed):
    with pytest.raises(WorktreeError, match="full 40-char"):
        _provision(registry, repo, managed, "abc123")


def test_unknown_sha_rejected(registry, repo, managed):
    with pytest.raises(WorktreeError):
        _provision(registry, repo, managed, SHA_A)


# --------------------------------------------------------------------------
# ORCH-WT-005: idempotent
# --------------------------------------------------------------------------

def test_provisioning_twice_returns_same_worktree(registry, repo, managed, head):
    first = _provision(registry, repo, managed, head)
    second = _provision(registry, repo, managed, head)
    assert first.resource_id == second.resource_id
    assert len(registry.list_for_workflow("WF-001")) == 1


# --------------------------------------------------------------------------
# ORCH-SNAP-001..003: immutable, detached, unaffected by later commits
# --------------------------------------------------------------------------

def test_snapshot_is_detached_at_candidate(registry, repo, managed, head):
    snap = snapshot_worktree(
        registry=registry, repo_root=repo, managed_root=managed,
        candidate_commit=head, workflow_id="WF-001", spec_id="SPEC-001",
    )
    assert _git(["rev-parse", "HEAD"], snap).stdout.strip() == head
    # detached: symbolic-ref must fail
    assert _git(["symbolic-ref", "-q", "HEAD"], snap).returncode != 0


def test_snapshot_is_read_only(registry, repo, managed, head):
    snap = snapshot_worktree(
        registry=registry, repo_root=repo, managed_root=managed,
        candidate_commit=head, workflow_id="WF-001", spec_id="SPEC-001",
    )
    target = snap / "README.md"
    assert (target.stat().st_mode & 0o222) == 0


def test_snapshot_unaffected_by_later_commits(registry, repo, managed, head):
    snap = snapshot_worktree(
        registry=registry, repo_root=repo, managed_root=managed,
        candidate_commit=head, workflow_id="WF-001", spec_id="SPEC-001",
    )
    (repo / "README.md").write_text("changed later\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "later"], repo)
    # Re-reading the snapshot still shows the original content.
    assert (snap / "README.md").read_text() == "hello\n"


def test_snapshot_rejects_bad_sha(registry, repo, managed):
    with pytest.raises(WorktreeError, match="full 40-char"):
        snapshot_worktree(
            registry=registry, repo_root=repo, managed_root=managed,
            candidate_commit="deadbeef", workflow_id="WF-001", spec_id="SPEC-001",
        )


# --------------------------------------------------------------------------
# ORCH-ISO-001/002/003: ownership and namespace isolation
# --------------------------------------------------------------------------

def test_workflow_cannot_act_on_another_worktree(registry, repo, managed, head):
    a = _provision(registry, repo, managed, head, workflow_id="WF-001")
    _provision(registry, repo, managed, head, workflow_id="WF-002",
               spec_id="SPEC-002", max_active=2)
    with pytest.raises(WorktreeError, match="does not own"):
        assert_workflow_owns_path(registry, "WF-002", a.worktree_path)


def test_owner_check_passes_for_own_worktree(registry, repo, managed, head):
    a = _provision(registry, repo, managed, head)
    rec = assert_workflow_owns_path(registry, "WF-001", a.worktree_path)
    assert rec.resource_id == a.resource_id


def test_branch_switch_outside_namespace_rejected():
    with pytest.raises(WorktreeError, match="outside the reserved namespace"):
        assert_branch_allowed("WF-001", "SPEC-001", "main")
    assert_branch_allowed("WF-001", "SPEC-001", "agentgate/WF-001/SPEC-001")


def test_two_workflows_get_distinct_trees_and_branches(registry, repo, managed, head):
    a = _provision(registry, repo, managed, head, workflow_id="WF-001", spec_id="SPEC-001")
    b = _provision(registry, repo, managed, head, workflow_id="WF-002",
                   spec_id="SPEC-002", max_active=2)
    assert a.worktree_path != b.worktree_path
    assert a.workflow_branch != b.workflow_branch
    # concurrent-ish writes do not interfere
    (Path(a.worktree_path) / "a.txt").write_text("a")
    assert not (Path(b.worktree_path) / "a.txt").exists()
