"""Milestone 03 / ORCH-GATE-001..008, ORCH-POLICY-*, ORCH-STATUS-* — durable gate store."""
import ast
import sqlite3
from pathlib import Path

import pytest

from core.orchestration.authz import NotAuthorized
from core.orchestration.gate_store import (
    STATUS_APPROVED,
    STATUS_DENIED,
    STATUS_PENDING,
    AlreadyResolved,
    GateStore,
    GateStoreError,
    PolicyError,
)
from core.orchestration.validate import validate_gate_record

SHA = "b" * 40
MODULE = Path(__file__).resolve().parents[1] / "core" / "orchestration" / "gate_store.py"


def _store(tmp_path, name="gates.db"):
    return GateStore(tmp_path / name)


def _create(store, **over):
    kw = dict(
        gate_id="G-001",
        workflow_id="WF-001",
        milestone_id="03",
        attempt_id="001",
        transition="implementer_to_reviewer",
        gate_type="handoff_approval",
        candidate_commit=SHA,
        requires_human_approval=True,
        approvers={"platform": "slack", "user_ids": ["U-APPROVER"]},
    )
    kw.update(over)
    return store.create(**kw)


# --------------------------------------------------------------------------
# ORCH-GATE-001/002: persistence + contract conformance
# --------------------------------------------------------------------------

def test_gate_persists_across_instances(tmp_path):
    _create(_store(tmp_path))
    fresh = _store(tmp_path)
    g = fresh.get("G-001")
    assert g is not None and g.status == STATUS_PENDING


def test_gate_db_is_separate_from_workspace_registry(tmp_path):
    store = _store(tmp_path, "gates.db")
    _create(store)
    assert store.db_path.name == "gates.db"
    assert (tmp_path / "gates.db").exists()
    assert not (tmp_path / "orchestration.db").exists()


def test_persisted_gate_satisfies_shipped_contract(tmp_path):
    _create(_store(tmp_path))
    g = _store(tmp_path).get("G-001")
    assert validate_gate_record(g.as_gate_record()) == []


def test_resolved_gate_satisfies_contract(tmp_path):
    store = _store(tmp_path)
    _create(store)
    store.resolve("G-001", outcome=STATUS_APPROVED, platform="slack",
                  user_id="U-APPROVER")
    g = _store(tmp_path).get("G-001")
    assert validate_gate_record(g.as_gate_record()) == []


# --------------------------------------------------------------------------
# ORCH-GATE-003: atomic compare-and-set
# --------------------------------------------------------------------------

def test_second_resolution_is_rejected(tmp_path):
    store = _store(tmp_path)
    _create(store)
    store.resolve("G-001", outcome=STATUS_APPROVED, platform="slack", user_id="U-APPROVER")
    with pytest.raises(AlreadyResolved) as exc:
        store.resolve("G-001", outcome=STATUS_DENIED, platform="slack",
                      user_id="U-APPROVER")
    assert exc.value.status == STATUS_APPROVED


# --------------------------------------------------------------------------
# ORCH-GATE-004: restart safety
# --------------------------------------------------------------------------

def test_pending_gate_survives_restart_and_remains_resolvable(tmp_path):
    _create(_store(tmp_path))
    fresh = _store(tmp_path)
    assert fresh.get("G-001").status == STATUS_PENDING
    fresh.resolve("G-001", outcome=STATUS_APPROVED, platform="slack", user_id="U-APPROVER")
    assert _store(tmp_path).get("G-001").status == STATUS_APPROVED


# --------------------------------------------------------------------------
# ORCH-GATE-005/006: attribution and immutability
# --------------------------------------------------------------------------

def test_resolution_records_actor_and_timestamp(tmp_path):
    store = _store(tmp_path)
    _create(store)
    g = store.resolve("G-001", outcome=STATUS_APPROVED, platform="slack",
                      user_id="U-APPROVER")
    assert g.resolved_platform == "slack"
    assert g.resolved_user_id == "U-APPROVER"
    assert g.resolved_at is not None


def test_resolved_gate_is_immutable(tmp_path):
    store = _store(tmp_path)
    _create(store)
    first = store.resolve("G-001", outcome=STATUS_APPROVED, platform="slack",
                          user_id="U-APPROVER")
    with pytest.raises(AlreadyResolved):
        store.resolve("G-001", outcome=STATUS_APPROVED, platform="slack",
                      user_id="U-APPROVER")
    after = store.get("G-001")
    assert after.resolved_at == first.resolved_at
    assert after.status == first.status


# --------------------------------------------------------------------------
# ORCH-GATE-007/008: WAL, no sensitive content
# --------------------------------------------------------------------------

def test_wal_mode(tmp_path):
    store = _store(tmp_path)
    with sqlite3.connect(store.db_path) as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_no_conversation_content_columns(tmp_path):
    store = _store(tmp_path)
    with sqlite3.connect(store.db_path) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(gates)")}
    for forbidden in ("content", "body", "message", "transcript", "chat", "secret", "token"):
        assert forbidden not in cols


# --------------------------------------------------------------------------
# ORCH-AUTHZ at the store boundary (005/006)
# --------------------------------------------------------------------------

def test_non_approver_cannot_resolve(tmp_path):
    store = _store(tmp_path)
    _create(store)
    with pytest.raises(NotAuthorized):
        store.resolve("G-001", outcome=STATUS_APPROVED, platform="slack",
                      user_id="U-NOBODY")
    assert store.get("G-001").status == STATUS_PENDING, "state must be unchanged"


def test_authz_rechecked_at_resolution_time(tmp_path):
    """Approvers are read from storage at resolve time, not from a stale display copy."""
    store = _store(tmp_path)
    _create(store, approvers={"platform": "slack", "user_ids": ["U-A"]})
    # Simulate the approver list changing after display: the stored row is what counts.
    with sqlite3.connect(store.db_path) as c:
        c.execute("UPDATE gates SET approvers_json=? WHERE gate_id='G-001'",
                  ('["U-B"]',))
        c.commit()
    with pytest.raises(NotAuthorized):
        store.resolve("G-001", outcome=STATUS_APPROVED, platform="slack", user_id="U-A")
    assert store.resolve("G-001", outcome=STATUS_APPROVED, platform="slack",
                         user_id="U-B").status == STATUS_APPROVED


# --------------------------------------------------------------------------
# ORCH-POLICY-001..005: policy and risk class are INPUTS
# --------------------------------------------------------------------------

def test_requires_human_approval_recorded_verbatim(tmp_path):
    store = _store(tmp_path)
    g = _create(store, requires_human_approval=False)
    assert g.requires_human_approval is False
    assert _store(tmp_path).get("G-001").requires_human_approval is False


def test_false_policy_still_recordable_and_resolvable(tmp_path):
    """The store records policy; it does not refuse a no-human-approval gate."""
    store = _store(tmp_path)
    _create(store, requires_human_approval=False)
    assert store.resolve("G-001", outcome=STATUS_APPROVED, platform="slack",
                         user_id="U-APPROVER").status == STATUS_APPROVED


@pytest.mark.parametrize("risk", ["low", "standard", "high", None])
def test_risk_class_stored_verbatim(tmp_path, risk):
    store = _store(tmp_path)
    _create(store, risk_class=risk)
    assert _store(tmp_path).get("G-001").risk_class == risk


@pytest.mark.parametrize("gate_type", ["milestone_acceptance", "product_spec_change",
                                       "spec_finalization"])
def test_non_waivable_gates_reject_no_human(tmp_path, gate_type):
    store = _store(tmp_path)
    with pytest.raises(PolicyError, match="cannot set requires_human_approval=False"):
        _create(store, gate_type=gate_type, requires_human_approval=False)
    assert store.list_pending() == []


def test_handoff_gate_may_waive_human_approval(tmp_path):
    store = _store(tmp_path)
    g = _create(store, gate_type="handoff_approval", requires_human_approval=False)
    assert g.requires_human_approval is False


def test_store_contains_no_classification_or_policy_derivation():
    """ORCH-POLICY-004: AgentGate must never *decide* policy.

    Asserted structurally: the module must not call anything that looks like a risk
    classifier, and must not compute requires_human_approval from other fields.
    """
    tree = ast.parse(MODULE.read_text())
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    forbidden = {"classify", "classify_risk", "assess_risk", "infer_risk",
                 "derive_policy", "should_require_human"}
    assert not (called & forbidden), f"policy derivation found: {called & forbidden}"


# --------------------------------------------------------------------------
# ORCH-STATUS-001..003
# --------------------------------------------------------------------------

def test_list_pending_returns_expected_fields_and_order(tmp_path):
    store = _store(tmp_path)
    _create(store, gate_id="G-001")
    _create(store, gate_id="G-002")
    pending = store.list_pending()
    assert [g.gate_id for g in pending] == ["G-001", "G-002"]  # deterministic
    g = pending[0]
    assert g.milestone_id and g.attempt_id and g.gate_type and g.status


def test_list_pending_excludes_resolved(tmp_path):
    store = _store(tmp_path)
    _create(store)
    store.resolve("G-001", outcome=STATUS_APPROVED, platform="slack", user_id="U-APPROVER")
    assert store.list_pending() == []


def test_no_resolution_path_bypasses_authz():
    """ORCH-STATUS-003: no public method resolves without an approver check."""
    tree = ast.parse(MODULE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and "resolve" in node.name:
            body = ast.dump(node)
            assert "require_gate_approver" in body, (
                f"{node.name} resolves without an authorization check"
            )


def test_duplicate_gate_id_rejected(tmp_path):
    store = _store(tmp_path)
    _create(store)
    with pytest.raises(GateStoreError, match="already exists"):
        _create(store)


# --------------------------------------------------------------------------
# ORCH-GATE-003 (strong form): real cross-process race for the same gate.
#
# The sequential test above is necessary but NOT sufficient: it is satisfied by the
# early `status != pending` check, so it passes even if the UPDATE loses its
# compare-and-set guard. This test races four real processes; exactly one may win.
# --------------------------------------------------------------------------

import multiprocessing as mp  # noqa: E402


def _race_worker(db_path: str, q) -> None:
    try:
        s = GateStore(db_path)
        s.resolve("G-RACE", outcome=STATUS_APPROVED, platform="slack",
                  user_id="U-APPROVER")
        q.put("won")
    except AlreadyResolved as exc:
        q.put(f"lost:{exc.status}")
    except Exception as exc:                       # noqa: BLE001
        q.put(f"error:{type(exc).__name__}:{exc}")


def test_concurrent_resolution_has_exactly_one_winner(tmp_path):
    db = tmp_path / "gates.db"
    store = GateStore(db)
    _create(store, gate_id="G-RACE")

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_race_worker, args=(str(db), q)) for _ in range(4)]
    for p in procs:
        p.start()
    results = [q.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=60)

    winners = [r for r in results if r == "won"]
    losers = [r for r in results if r.startswith("lost:")]
    errors = [r for r in results if r.startswith("error:")]

    assert not errors, errors
    assert len(winners) == 1, f"expected exactly one winner, got {results}"
    assert len(losers) == 3, results
    assert GateStore(db).get("G-RACE").status == STATUS_APPROVED
