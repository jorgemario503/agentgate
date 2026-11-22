"""Durable gate store (SQLite).

Where gates live and why here
-----------------------------
A gate can legitimately wait hours for a human. ``core/approvals._pending`` is module-level
in-memory state: excellent for a per-turn tool confirmation, wrong for an authorization that
must survive a restart. A restarted process that forgot a pending gate would silently drop an
approval — a correctness failure, not a UX wrinkle.

**What belongs here vs. the orchestrator.** This store holds gate *records* and enforces
*storage invariants* (atomic resolution, immutability, restart safety). Whether a human gate
is required at all is *policy*, which belongs to the orchestrator and arrives as the
``requires_human_approval`` input. Nothing in this module derives policy or classifies risk
(ORCH-POLICY-004); it records what it is told.

Resolution is compare-and-set
-----------------------------
``resolve()`` transitions ``pending -> approved|denied|cancelled`` in a single
``UPDATE ... WHERE status='pending'``. The row-count tells us whether we won. That makes a
double-click, a retried message, or two processes racing on the same gate safe: exactly one
resolution lands, and the loser is told it was already resolved rather than silently
overwriting the first decision.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from core.orchestration.validate import validate_gate_record

SCHEMA_VERSION = 1

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"

_RESOLVED = (STATUS_APPROVED, STATUS_DENIED, STATUS_CANCELLED, STATUS_EXPIRED)

#: Gate types whose human requirement can never be waived (ORCH-POLICY-005 / DEC-309).
NON_WAIVABLE_GATE_TYPES = frozenset({
    "milestone_acceptance",
    "product_spec_change",
    "spec_finalization",
})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gates (
    gate_id                  TEXT PRIMARY KEY,
    workflow_id              TEXT NOT NULL,
    milestone_id             TEXT NOT NULL,
    attempt_id               TEXT NOT NULL,
    handoff_id               TEXT,
    transition               TEXT NOT NULL,
    gate_type                TEXT NOT NULL,
    candidate_commit         TEXT NOT NULL,
    status                   TEXT NOT NULL,
    requires_human_approval  INTEGER NOT NULL,
    risk_class               TEXT,
    approver_platform        TEXT NOT NULL,
    approvers_json           TEXT NOT NULL,
    requested_at             REAL NOT NULL,
    resolved_at              REAL,
    resolved_platform        TEXT,
    resolved_user_id         TEXT,
    causation_event_id       TEXT,
    escalation_count         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_gates_status ON gates(status);
CREATE INDEX IF NOT EXISTS idx_gates_workflow ON gates(workflow_id);
"""


class GateStoreError(Exception):
    """Base error for the gate store."""


class AlreadyResolved(GateStoreError):
    """Raised when a gate is resolved twice; carries the winning outcome."""

    def __init__(self, gate_id: str, status: str):
        self.gate_id = gate_id
        self.status = status
        super().__init__(f"gate {gate_id!r} is already {status}")


class PolicyError(GateStoreError):
    """Raised when a record would waive a non-waivable human requirement."""


@dataclass(frozen=True)
class Gate:
    gate_id: str
    workflow_id: str
    milestone_id: str
    attempt_id: str
    handoff_id: Optional[str]
    transition: str
    gate_type: str
    candidate_commit: str
    status: str
    requires_human_approval: bool
    risk_class: Optional[str]
    approver_platform: str
    approver_user_ids: tuple[str, ...]
    requested_at: float
    resolved_at: Optional[float]
    resolved_platform: Optional[str]
    resolved_user_id: Optional[str]
    causation_event_id: Optional[str]
    escalation_count: int

    def as_gate_record(self) -> dict[str, Any]:
        """Project into the shipped ``gate-record`` contract for validation."""
        rec: dict[str, Any] = {
            "schema_version": "1.0",
            "gate_id": self.gate_id,
            "workflow_id": self.workflow_id,
            "milestone_id": self.milestone_id,
            "attempt_id": self.attempt_id,
            "transition": self.transition,
            "candidate_commit": self.candidate_commit,
            "status": self.status,
            "requires_human_decision": self.requires_human_approval,
            "approvers": {"platform": self.approver_platform,
                          "user_ids": list(self.approver_user_ids)},
            "requested_at": _iso(self.requested_at),
            "escalation_count": self.escalation_count,
        }
        if self.handoff_id:
            rec["handoff_id"] = self.handoff_id
        if self.resolved_at is not None:
            rec["resolved_at"] = _iso(self.resolved_at)
        if self.resolved_platform and self.resolved_user_id:
            rec["resolved_by"] = {"platform": self.resolved_platform,
                                  "user_id": self.resolved_user_id}
        return rec

    def as_approver_gate(self) -> dict[str, Any]:
        """Shape expected by :func:`core.orchestration.authz.is_gate_approver`."""
        return {
            "gate_id": self.gate_id,
            "approvers": {"platform": self.approver_platform,
                          "user_ids": list(self.approver_user_ids)},
        }


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class GateStore:
    """SQLite-backed durable gate records."""

    def __init__(self, db_path: str | os.PathLike):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # -- plumbing ---------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)",
                             (str(SCHEMA_VERSION),))
            elif int(row["value"]) != SCHEMA_VERSION:
                raise GateStoreError(
                    f"gate schema_version {row['value']} != {SCHEMA_VERSION}")

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
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

    # -- reads ------------------------------------------------------------
    def get(self, gate_id: str) -> Optional[Gate]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM gates WHERE gate_id=?", (gate_id,)).fetchone()
        return _row_to_gate(row) if row else None

    def list_pending(self) -> list[Gate]:
        """Pending gates, oldest first. Deterministic; usable from terminal or orchestrator."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM gates WHERE status=? ORDER BY requested_at, gate_id",
                (STATUS_PENDING,),
            ).fetchall()
        return [_row_to_gate(r) for r in rows]

    def list_for_workflow(self, workflow_id: str) -> list[Gate]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM gates WHERE workflow_id=? ORDER BY requested_at, gate_id",
                (workflow_id,),
            ).fetchall()
        return [_row_to_gate(r) for r in rows]

    # -- writes -----------------------------------------------------------
    def create(
        self,
        *,
        gate_id: str,
        workflow_id: str,
        milestone_id: str,
        attempt_id: str,
        transition: str,
        gate_type: str,
        candidate_commit: str,
        requires_human_approval: bool,
        approvers: dict[str, Any],
        handoff_id: Optional[str] = None,
        risk_class: Optional[str] = None,
        causation_event_id: Optional[str] = None,
    ) -> Gate:
        """Record a new pending gate.

        Policy inputs are stored verbatim. The only thing refused here is a record that
        would waive a non-waivable human requirement (ORCH-POLICY-005).
        """
        if gate_type in NON_WAIVABLE_GATE_TYPES and not requires_human_approval:
            raise PolicyError(
                f"gate_type {gate_type!r} cannot set requires_human_approval=False"
            )
        if not isinstance(approvers, dict):
            raise GateStoreError("approvers must be a mapping with platform and user_ids")
        platform = approvers.get("platform")
        user_ids = approvers.get("user_ids")
        if not isinstance(platform, str) or not isinstance(user_ids, (list, tuple, set)):
            raise GateStoreError("approvers requires a string platform and a user_ids list")

        now = time.time()
        row = {
            "gate_id": gate_id,
            "workflow_id": workflow_id,
            "milestone_id": milestone_id,
            "attempt_id": attempt_id,
            "handoff_id": handoff_id,
            "transition": transition,
            "gate_type": gate_type,
            "candidate_commit": candidate_commit,
            "status": STATUS_PENDING,
            "requires_human_approval": 1 if requires_human_approval else 0,
            "risk_class": risk_class,
            "approver_platform": platform,
            "approvers_json": _dumps(list(user_ids)),
            "requested_at": now,
            "escalation_count": 0,
            "causation_event_id": causation_event_id,
        }
        gate = Gate(
            gate_id=gate_id, workflow_id=workflow_id, milestone_id=milestone_id,
            attempt_id=attempt_id, handoff_id=handoff_id, transition=transition,
            gate_type=gate_type, candidate_commit=candidate_commit,
            status=STATUS_PENDING, requires_human_approval=bool(requires_human_approval),
            risk_class=risk_class, approver_platform=platform,
            approver_user_ids=tuple(user_ids), requested_at=now,
            resolved_at=None, resolved_platform=None, resolved_user_id=None,
            causation_event_id=causation_event_id, escalation_count=0,
        )
        errors = validate_gate_record(gate.as_gate_record())
        if errors:
            raise GateStoreError("gate does not satisfy the contract: " + "; ".join(errors))

        with self._txn() as conn:
            try:
                conn.execute(
                    """INSERT INTO gates
                       (gate_id, workflow_id, milestone_id, attempt_id, handoff_id,
                        transition, gate_type, candidate_commit, status,
                        requires_human_approval, risk_class, approver_platform,
                        approvers_json, requested_at, escalation_count,
                        causation_event_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (gate_id, workflow_id, milestone_id, attempt_id, handoff_id,
                     transition, gate_type, candidate_commit, STATUS_PENDING,
                     row["requires_human_approval"], risk_class, platform,
                     row["approvers_json"], now, 0, causation_event_id),
                )
            except sqlite3.IntegrityError as exc:
                raise GateStoreError(f"gate {gate_id!r} already exists") from exc
        return self.get(gate_id)  # type: ignore[return-value]

    def resolve(
        self,
        gate_id: str,
        *,
        outcome: str,
        platform: Optional[str],
        user_id: Optional[str],
        allow_admins: tuple[str, ...] = (),
    ) -> Gate:
        """Resolve a pending gate with an authenticated actor.

        Atomic compare-and-set: the UPDATE only matches a still-pending row, so concurrent
        resolvers cannot both win. Authorization is checked **here**, at resolution time,
        against the stored approvers — not at display time (ORCH-AUTHZ-006).
        """
        if outcome not in (STATUS_APPROVED, STATUS_DENIED, STATUS_CANCELLED):
            raise GateStoreError(f"invalid resolution outcome {outcome!r}")

        current = self.get(gate_id)
        if current is None:
            raise GateStoreError(f"unknown gate {gate_id!r}")
        if current.status != STATUS_PENDING:
            raise AlreadyResolved(gate_id, current.status)

        from core.orchestration.authz import require_gate_approver
        require_gate_approver(current.as_approver_gate(), platform=platform,
                              user_id=user_id, allow_admins=allow_admins)

        now = time.time()
        with self._txn() as conn:
            cur = conn.execute(
                "UPDATE gates SET status=?, resolved_at=?, resolved_platform=?, "
                "resolved_user_id=? WHERE gate_id=? AND status=?",
                (outcome, now, platform, user_id, gate_id, STATUS_PENDING),
            )
            if cur.rowcount != 1:
                # Lost the race: report the winner, never overwrite it.
                row = conn.execute("SELECT status FROM gates WHERE gate_id=?",
                                   (gate_id,)).fetchone()
                raise AlreadyResolved(gate_id, row["status"] if row else "unknown")
        return self.get(gate_id)  # type: ignore[return-value]


# -- helpers ---------------------------------------------------------------

def _dumps(obj: Any) -> str:
    import json
    return json.dumps(obj)


def _loads(raw: str) -> list[str]:
    import json
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _row_to_gate(row: sqlite3.Row) -> Gate:
    return Gate(
        gate_id=row["gate_id"], workflow_id=row["workflow_id"],
        milestone_id=row["milestone_id"], attempt_id=row["attempt_id"],
        handoff_id=row["handoff_id"], transition=row["transition"],
        gate_type=row["gate_type"], candidate_commit=row["candidate_commit"],
        status=row["status"],
        requires_human_approval=bool(row["requires_human_approval"]),
        risk_class=row["risk_class"], approver_platform=row["approver_platform"],
        approver_user_ids=tuple(_loads(row["approvers_json"])),
        requested_at=row["requested_at"], resolved_at=row["resolved_at"],
        resolved_platform=row["resolved_platform"],
        resolved_user_id=row["resolved_user_id"],
        causation_event_id=row["causation_event_id"],
        escalation_count=row["escalation_count"],
    )
