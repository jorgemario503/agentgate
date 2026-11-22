"""Milestone 02 / ORCH-REG-001..005 — durable cross-process workspace registry."""
import multiprocessing as mp
import os
import sqlite3
import time
from pathlib import Path

import pytest

from core.orchestration.registry import (
    STATUS_ACTIVE,
    STATUS_CLEANED,
    STATUS_CLEANUP_PENDING,
    STATUS_PROVISIONING,
    RegistryError,
    RepoRegistry,
)


def _reg(tmp_path):
    return RepoRegistry(tmp_path / "orchestration.db")


def _register(reg, **over):
    kw = dict(
        resource_id="R-001",
        repository_id="repo-001",
        repo_root="/tmp/repo",
        workflow_id="WF-001",
        spec_id="SPEC-001",
        target_branch="main",
        base_commit="a" * 40,
        workflow_branch="agentgate/WF-001/SPEC-001",
        worktree_path="/tmp/ws/WF-001",
    )
    kw.update(over)
    return reg.register_provisioning(**kw)


# --------------------------------------------------------------------------
# ORCH-REG-001: persistence across process restart
# --------------------------------------------------------------------------

def test_record_persists_across_instances(tmp_path):
    db = tmp_path / "orchestration.db"
    r1 = RepoRegistry(db)
    _register(r1)

    r2 = RepoRegistry(db)          # simulates a fresh process
    rec = r2.get("R-001")
    assert rec is not None
    assert rec.workflow_branch == "agentgate/WF-001/SPEC-001"
    assert rec.status == STATUS_PROVISIONING


def test_schema_version_recorded(tmp_path):
    reg = _reg(tmp_path)
    with sqlite3.connect(reg.db_path) as c:
        v = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert v == "1"


def test_status_transitions_persist(tmp_path):
    reg = _reg(tmp_path)
    _register(reg)
    reg.mark_active("R-001")
    assert _reg(tmp_path).get("R-001").status == STATUS_ACTIVE


# --------------------------------------------------------------------------
# ORCH-REG-004: WAL + concurrency
# --------------------------------------------------------------------------

def test_wal_mode_enabled(tmp_path):
    reg = _reg(tmp_path)
    with sqlite3.connect(reg.db_path) as c:
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def _worker(db_path, i, q):
    try:
        reg = RepoRegistry(db_path)
        _register(
            reg,
            resource_id=f"R-{i}",
            workflow_id=f"WF-{i}",
            workflow_branch=f"agentgate/WF-{i}/SPEC-{i}",
            worktree_path=f"/tmp/ws/WF-{i}",
        )
        q.put(("ok", i))
    except Exception as exc:                     # noqa: BLE001
        q.put(("err", f"{type(exc).__name__}: {exc}"))


def test_concurrent_registration_from_multiple_processes(tmp_path):
    """ORCH-REG-002: independent processes both register without corrupting the DB."""
    db = tmp_path / "orchestration.db"
    RepoRegistry(db)                              # create schema first
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(str(db), i, q)) for i in (1, 2)]
    for p in procs:
        p.start()
    results = [q.get(timeout=30) for _ in procs]
    for p in procs:
        p.join(timeout=30)

    assert all(r[0] == "ok" for r in results), results
    reg = RepoRegistry(db)
    assert len(reg.list_for_workflow("WF-1")) == 1
    assert len(reg.list_for_workflow("WF-2")) == 1


def test_duplicate_branch_is_rejected_not_duplicated(tmp_path):
    reg = _reg(tmp_path)
    _register(reg)
    with pytest.raises(RegistryError):
        _register(reg, resource_id="R-002")       # same branch
    assert len(reg.list_for_workflow("WF-001")) == 1


# --------------------------------------------------------------------------
# ORCH-REG-003 / DEC-206: register-before-create + one active worktree
# --------------------------------------------------------------------------

def test_register_before_create_leaves_detectable_orphan(tmp_path):
    """A crash after register but before create is *detectable* (status=provisioning)."""
    reg = _reg(tmp_path)
    _register(reg)                                # registered, never marked active
    orphans = reg.find_orphans()
    assert [o.resource_id for o in orphans] == ["R-001"]
    assert orphans[0].status == STATUS_PROVISIONING


def test_find_orphans_respects_age(tmp_path):
    reg = _reg(tmp_path)
    _register(reg)
    assert reg.find_orphans(older_than_seconds=60) == []
    time.sleep(0.01)
    assert len(reg.find_orphans(older_than_seconds=0)) == 1


def test_second_active_worktree_for_same_workflow_rejected(tmp_path):
    reg = _reg(tmp_path)
    _register(reg)
    reg.mark_active("R-001")
    with pytest.raises(RegistryError, match="already has"):
        _register(reg, resource_id="R-002", workflow_branch="agentgate/WF-001/SPEC-002")
    assert reg.active_worktree_count("WF-001") == 1


def test_max_active_is_configurable(tmp_path):
    reg = _reg(tmp_path)
    _register(reg)
    reg.mark_active("R-001")
    rec = _register(reg, resource_id="R-002",
                    workflow_branch="agentgate/WF-001/SPEC-002", max_active=2)
    assert rec.resource_id == "R-002"


# --------------------------------------------------------------------------
# Leases (DEC-204): recoverable, not timestamp-only
# --------------------------------------------------------------------------

def test_lease_written_with_pid_and_heartbeat(tmp_path):
    reg = _reg(tmp_path)
    _register(reg)
    with sqlite3.connect(reg.db_path) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM leases WHERE resource_id='R-001'").fetchone()
    assert row["pid"] == os.getpid()
    assert row["heartbeat_at"] > 0


def test_lease_not_stale_for_live_process(tmp_path):
    reg = _reg(tmp_path)
    _register(reg)
    assert reg.is_lease_stale("R-001", max_silence_seconds=60) is False


def test_lease_stale_when_heartbeat_old(tmp_path):
    reg = _reg(tmp_path)
    _register(reg)
    with sqlite3.connect(reg.db_path) as c:
        c.execute("UPDATE leases SET heartbeat_at=? WHERE resource_id='R-001'",
                  (time.time() - 10_000,))
        c.commit()
    assert reg.is_lease_stale("R-001", max_silence_seconds=60) is True


def test_lease_stale_when_pid_dead(tmp_path):
    reg = _reg(tmp_path)
    _register(reg, pid=999_999)                   # no such process
    assert reg.is_lease_stale("R-001", max_silence_seconds=10_000) is True


# --------------------------------------------------------------------------
# Cleanup states
# --------------------------------------------------------------------------

def test_cleanup_lifecycle(tmp_path):
    reg = _reg(tmp_path)
    _register(reg)
    reg.mark_active("R-001")
    reg.begin_cleanup("R-001")
    rec = reg.get("R-001")
    assert rec.status == STATUS_CLEANUP_PENDING
    assert rec.cleanup_attempts == 1
    reg.mark_cleaned("R-001")
    assert reg.get("R-001").status == STATUS_CLEANED


def test_cleanup_failure_is_recorded_honestly(tmp_path):
    reg = _reg(tmp_path)
    _register(reg)
    reg.begin_cleanup("R-001")
    reg.mark_cleanup_failed("R-001", "worktree is dirty")
    rec = reg.get("R-001")
    assert rec.status == "cleanup_failed"
    assert "dirty" in rec.last_error


def test_unknown_resource_raises(tmp_path):
    reg = _reg(tmp_path)
    with pytest.raises(RegistryError, match="unknown resource"):
        reg.mark_active("nope")


# --------------------------------------------------------------------------
# ORCH-REG-005: no secrets, no file contents
# --------------------------------------------------------------------------

def test_schema_stores_no_content_or_secrets(tmp_path):
    reg = _reg(tmp_path)
    with sqlite3.connect(reg.db_path) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(worktrees)")]
    forbidden = {"content", "body", "secret", "token", "password", "api_key"}
    assert not (forbidden & set(cols)), f"unexpected sensitive columns: {forbidden & set(cols)}"
