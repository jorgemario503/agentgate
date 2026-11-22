"""Strict validators for the orchestration JSON contracts.

Why hand-written instead of a schema library
--------------------------------------------
The project declares no JSON-schema dependency (see requirements.txt) and the spec
requires that Phase 1 add no heavyweight dependency. Rather than pull one in for five
contracts, this module encodes the same checks directly and reads the sibling
``schemas/*.json`` files for documentation and drift testing.

Design invariants
-----------------
- **Pure.** No I/O, no network, no runner imports. Validation never mutates input.
- **Fail closed.** Unknown fields are rejected; required fields are enforced; a
  missing or wrong ``schema_version`` is an error, never a default.
- **Path containment is enforced here, not downstream.** Every artifact reference is
  checked for traversal, absolute paths, and symlink escape, mirroring
  ``core.artifacts.ArtifactStore._confine``.
- **Full SHAs only.** Abbreviated or fabricated commit ids are rejected, because the
  entire review model rests on an immutable, unambiguous candidate.
- **Errors are collected, not raised on first failure**, so a caller (and a test) can
  see every problem at once.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

#: Roles that may appear as source/target of a handoff.
ROLES = ("implementer", "reviewer")

#: Transitions the coordinator may schedule.
TRANSITIONS = ("implementer_to_reviewer", "reviewer_to_implementer_fix")

#: Reviewer recommendations. Mirrors spec 4.2 exactly.
RECOMMENDATIONS = (
    "APPROVED",
    "APPROVED_WITH_NOTES",
    "CHANGES_REQUESTED",
    "HUMAN_DECISION_REQUIRED",
    "BLOCKED",
)

SEVERITIES = ("critical", "major", "minor", "suggestion")

ACCEPTANCE_STATUSES = ("pass", "fail", "not-verified", "not-applicable")

GATE_STATUSES = ("pending", "approved", "denied", "expired", "cancelled")

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ISO_UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$")
_FINDING_ID = re.compile(r"^F-[0-9]+$")
_QUESTION_ID = re.compile(r"^Q-[0-9]+$")


class ValidationError(Exception):
    """Raised by the ``require_*`` helpers. Carries every collected error."""

    def __init__(self, errors: Iterable[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors) or "validation failed")


# ---------------------------------------------------------------------------
# Small typed helpers. Each returns an error string or None.
# ---------------------------------------------------------------------------

def _require_fields(obj: Any, fields: Iterable[str], where: str) -> list[str]:
    if not isinstance(obj, dict):
        return [f"{where}: expected an object, got {type(obj).__name__}"]
    return [f"{where}: missing required field {f!r}" for f in fields if f not in obj]


def _reject_unknown(obj: Any, allowed: Iterable[str], where: str) -> list[str]:
    if not isinstance(obj, dict):
        return []
    allowed = set(allowed)
    return [f"{where}: unknown field {k!r}" for k in obj if k not in allowed]


def _check_str(value: Any, name: str, where: str, *, max_len: int = 512,
               min_len: int = 1) -> list[str]:
    if not isinstance(value, str):
        return [f"{where}.{name}: expected string, got {type(value).__name__}"]
    if len(value) < min_len:
        return [f"{where}.{name}: must be at least {min_len} character(s)"]
    if len(value) > max_len:
        return [f"{where}.{name}: exceeds {max_len} chars"]
    return []


def _check_enum(value: Any, name: str, where: str, allowed: Iterable[str]) -> list[str]:
    allowed = tuple(allowed)
    if value not in allowed:
        return [f"{where}.{name}: {value!r} not in {list(allowed)}"]
    return []


def _check_full_sha(value: Any, name: str, where: str) -> list[str]:
    if not isinstance(value, str) or not _FULL_SHA.match(value):
        return [f"{where}.{name}: must be a full 40-char lowercase hex SHA"]
    return []


def _check_iso_utc(value: Any, name: str, where: str) -> list[str]:
    if not isinstance(value, str) or not _ISO_UTC.match(value):
        return [f"{where}.{name}: must be an ISO-8601 UTC timestamp ending in Z"]
    return []


# ---------------------------------------------------------------------------
# Path containment — mirrors core.artifacts.ArtifactStore._confine
# ---------------------------------------------------------------------------

def check_path_contained(raw_path: str, root: Path) -> str | None:
    """Return an error string when ``raw_path`` is unsafe, else None.

    Rejects absolute paths, traversal that escapes ``root``, and symlink escapes.
    Uses the same resolve-then-relative_to strategy as the artifact store so the
    two subsystems cannot disagree about what "contained" means.
    """
    if not isinstance(raw_path, str) or not raw_path:
        return "path must be a non-empty string"
    p = Path(raw_path)
    if p.is_absolute():
        return f"path {raw_path!r} must be repository-relative, not absolute"
    try:
        root_resolved = root.resolve(strict=True)
    except OSError:
        # A missing root is a caller/programming error, not a payload error.
        return None
    candidate = (root_resolved / p)
    if candidate.is_symlink():
        return f"path {raw_path!r} is a symlink"
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return f"path {raw_path!r} could not be resolved"
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return f"path {raw_path!r} escapes the repository root"
    if resolved.is_symlink():
        return f"path {raw_path!r} is a symlink"
    return None


# ---------------------------------------------------------------------------
# 4.1 Handoff request
# ---------------------------------------------------------------------------

_HANDOFF_ALLOWED = {
    "schema_version", "workflow_id", "spec_id", "milestone_id", "attempt_id",
    "handoff_id", "idempotency_key", "source_role", "target_role",
    "requested_action", "spec_version", "base_commit", "candidate_commit",
    "artifact_references", "created_at", "requested_by", "authorization_status",
}

_HANDOFF_REQUIRED = (
    "schema_version", "workflow_id", "milestone_id", "attempt_id", "handoff_id",
    "idempotency_key", "source_role", "target_role", "requested_action",
    "spec_version", "base_commit", "candidate_commit", "artifact_references",
    "created_at", "requested_by", "authorization_status",
)


def validate_handoff(payload: Any, *, repo_root: Path | None = None) -> list[str]:
    """Validate a handoff request (spec 4.1). Returns a list of errors."""
    where = "handoff"
    errors = _require_fields(payload, _HANDOFF_REQUIRED, where)
    if errors:
        return errors
    errors += _reject_unknown(payload, _HANDOFF_ALLOWED, where)

    if payload["schema_version"] != "1.0":
        errors.append(f"{where}.schema_version: expected '1.0'")

    for f in ("workflow_id", "milestone_id", "attempt_id", "handoff_id",
              "idempotency_key", "spec_version", "requested_by"):
        errors += _check_str(payload[f], f, where, max_len=256)

    if "spec_id" in payload:
        errors += _check_str(payload["spec_id"], "spec_id", where, max_len=128)

    errors += _check_enum(payload["source_role"], "source_role", where, ROLES)
    errors += _check_enum(payload["target_role"], "target_role", where, ROLES)
    errors += _check_enum(payload["requested_action"], "requested_action", where,
                          ("review_milestone", "apply_fixes"))
    errors += _check_enum(payload["authorization_status"], "authorization_status", where,
                          ("pending_human_approval", "approved", "denied",
                           "cancelled", "expired"))
    errors += _check_full_sha(payload["base_commit"], "base_commit", where)
    errors += _check_full_sha(payload["candidate_commit"], "candidate_commit", where)
    errors += _check_iso_utc(payload["created_at"], "created_at", where)

    # A handoff to yourself is meaningless and usually a bug.
    if payload["source_role"] == payload["target_role"]:
        errors.append(f"{where}: source_role and target_role must differ")

    refs = payload["artifact_references"]
    if not isinstance(refs, list) or not refs:
        errors.append(f"{where}.artifact_references: must be a non-empty array")
    elif len(refs) > 100:
        errors.append(f"{where}.artifact_references: at most 100 entries")
    else:
        for i, ref in enumerate(refs):
            sub = f"{where}.artifact_references[{i}]"
            errs = _require_fields(ref, ("path", "sha256"), sub)
            errors += errs
            if errs:
                continue
            errors += _reject_unknown(ref, ("path", "sha256"), sub)
            errors += _check_str(ref["path"], "path", sub, max_len=512)
            if not isinstance(ref["sha256"], str) or not _SHA256.match(ref["sha256"]):
                errors.append(f"{sub}.sha256: must be a 64-char lowercase hex digest")
            if repo_root is not None and isinstance(ref["path"], str):
                problem = check_path_contained(ref["path"], repo_root)
                if problem:
                    errors.append(f"{sub}.path: {problem}")
    return errors


# ---------------------------------------------------------------------------
# 4.2 Reviewer result
# ---------------------------------------------------------------------------

_REVIEW_ALLOWED = {
    "schema_version", "handoff_id", "attempt_id", "reviewed_commit",
    "recommendation", "acceptance_criteria", "findings", "open_questions",
    "tests_performed", "tests_not_performed", "residual_risks",
    "recommended_next_action",
}

_REVIEW_REQUIRED = tuple(sorted(_REVIEW_ALLOWED))


def validate_review_findings(payload: Any) -> list[str]:
    """Validate a reviewer result (spec 4.2). Returns a list of errors."""
    where = "review"
    errors = _require_fields(payload, _REVIEW_REQUIRED, where)
    if errors:
        return errors
    errors += _reject_unknown(payload, _REVIEW_ALLOWED, where)

    if payload["schema_version"] != "1.0":
        errors.append(f"{where}.schema_version: expected '1.0'")
    errors += _check_str(payload["handoff_id"], "handoff_id", where, max_len=128)
    errors += _check_str(payload["attempt_id"], "attempt_id", where, max_len=64)
    errors += _check_full_sha(payload["reviewed_commit"], "reviewed_commit", where)
    errors += _check_enum(payload["recommendation"], "recommendation", where,
                          RECOMMENDATIONS)
    errors += _check_str(payload["recommended_next_action"],
                         "recommended_next_action", where, max_len=1000)

    matrix = payload["acceptance_criteria"]
    if not isinstance(matrix, list) or not matrix:
        errors.append(f"{where}.acceptance_criteria: must be a non-empty array")
    else:
        for i, row in enumerate(matrix):
            sub = f"{where}.acceptance_criteria[{i}]"
            errs = _require_fields(row, ("requirement_id", "status", "evidence"), sub)
            errors += errs
            if errs:
                continue
            errors += _check_str(row["requirement_id"], "requirement_id", sub, max_len=128)
            errors += _check_enum(row["status"], "status", sub, ACCEPTANCE_STATUSES)
            errors += _check_str(row["evidence"], "evidence", sub, max_len=4000)

    findings = payload["findings"]
    if not isinstance(findings, list):
        errors.append(f"{where}.findings: must be an array")
    else:
        seen_ids: set[str] = set()
        for i, f in enumerate(findings):
            sub = f"{where}.findings[{i}]"
            errs = _require_fields(f, ("id", "severity", "summary", "impact", "remediation"), sub)
            errors += errs
            if errs:
                continue
            if not isinstance(f["id"], str) or not _FINDING_ID.match(f["id"]):
                errors.append(f"{sub}.id: must match F-<number>")
            elif f["id"] in seen_ids:
                errors.append(f"{sub}.id: duplicate finding id {f['id']!r}")
            else:
                seen_ids.add(f["id"])
            errors += _check_enum(f["severity"], "severity", sub, SEVERITIES)

    questions = payload["open_questions"]
    if not isinstance(questions, list):
        errors.append(f"{where}.open_questions: must be an array")
    else:
        for i, q in enumerate(questions):
            sub = f"{where}.open_questions[{i}]"
            errs = _require_fields(q, ("id", "question"), sub)
            errors += errs
            if errs:
                continue
            if not isinstance(q["id"], str) or not _QUESTION_ID.match(q["id"]):
                errors.append(f"{sub}.id: must match Q-<number>")

    for list_field in ("tests_performed", "tests_not_performed", "residual_risks"):
        if not isinstance(payload[list_field], list):
            errors.append(f"{where}.{list_field}: must be an array")
    return errors


# ---------------------------------------------------------------------------
# 4.3 Gate decision
# ---------------------------------------------------------------------------

_GATE_ALLOWED = {
    "schema_version", "milestone_id", "attempt_id", "spec_version",
    "candidate_commit", "gate_type", "configured_policy", "decision_authority",
    "decision_status", "outcome", "blocking_finding_ids", "conditions",
    "next_action", "decided_by", "decided_at", "causation_event_id",
    "approval_event_id",
}

_GATE_REQUIRED = (
    "schema_version", "milestone_id", "attempt_id", "spec_version",
    "candidate_commit", "gate_type", "decision_authority", "decision_status",
    "next_action",
)


def validate_gate_decision(payload: Any) -> list[str]:
    """Validate a gate decision (spec 4.3). Returns a list of errors."""
    where = "gate"
    errors = _require_fields(payload, _GATE_REQUIRED, where)
    if errors:
        return errors
    errors += _reject_unknown(payload, _GATE_ALLOWED, where)

    if payload["schema_version"] != "1.0":
        errors.append(f"{where}.schema_version: expected '1.0'")
    errors += _check_full_sha(payload["candidate_commit"], "candidate_commit", where)
    errors += _check_enum(payload["gate_type"], "gate_type", where,
                          ("handoff_approval", "milestone_acceptance",
                           "product_spec_change", "spec_finalization"))
    errors += _check_enum(payload["decision_authority"], "decision_authority", where,
                          ("human", "policy"))
    errors += _check_enum(payload["decision_status"], "decision_status", where,
                          ("pending", "recorded"))

    status = payload["decision_status"]
    if status == "recorded":
        for f in ("outcome", "decided_by", "decided_at"):
            if f not in payload:
                errors.append(f"{where}: decision_status 'recorded' requires {f!r}")
        if "outcome" in payload:
            errors += _check_enum(payload["outcome"], "outcome", where,
                                  RECOMMENDATIONS + ("DENIED", "CANCELLED", "EXPIRED"))
        if "decided_by" in payload:
            errors += _check_str(payload["decided_by"], "decided_by", where, max_len=128)
        if "decided_at" in payload:
            errors += _check_iso_utc(payload["decided_at"], "decided_at", where)
    else:
        if "decided_by" in payload:
            errors.append(f"{where}: pending gate must not carry 'decided_by'")

    if "blocking_finding_ids" in payload:
        ids = payload["blocking_finding_ids"]
        if not isinstance(ids, list):
            errors.append(f"{where}.blocking_finding_ids: must be an array")
        else:
            for i, fid in enumerate(ids):
                if not isinstance(fid, str) or not _FINDING_ID.match(fid):
                    errors.append(f"{where}.blocking_finding_ids[{i}]: must match F-<number>")
    return errors


# ---------------------------------------------------------------------------
# 4.4 Decision digest
# ---------------------------------------------------------------------------

_DIGEST_ALLOWED = {
    "digest_schema_version", "digest_id", "workflow_id", "milestone_id",
    "attempt_id", "source_record_ids", "generated_at", "decisions",
    "findings_to_address", "approved_spec_versions", "unanswered_question_ids",
    "non_authoritative_prose",
}

_DIGEST_REQUIRED = (
    "digest_schema_version", "digest_id", "milestone_id", "attempt_id",
    "source_record_ids", "generated_at",
)

#: Field names that would indicate raw conversation leaked into the digest.
#: The digest is built from structured records only (spec 4.4), so these must
#: never appear. Enforced structurally, not by trusting the producer.
_FORBIDDEN_DIGEST_KEYS = frozenset({
    "transcript", "raw_chat", "chat", "messages", "conversation",
    "history", "reasoning", "agent_notes", "summary",
})


def validate_decision_digest(payload: Any) -> list[str]:
    """Validate a decision digest (spec 4.4). Returns a list of errors."""
    where = "digest"
    errors = _require_fields(payload, _DIGEST_REQUIRED, where)
    if errors:
        return errors
    errors += _reject_unknown(payload, _DIGEST_ALLOWED, where)

    if payload["digest_schema_version"] != "1.0":
        errors.append(f"{where}.digest_schema_version: expected '1.0'")
    errors += _check_str(payload["digest_id"], "digest_id", where, max_len=128)
    errors += _check_str(payload["milestone_id"], "milestone_id", where, max_len=64)
    errors += _check_str(payload["attempt_id"], "attempt_id", where, max_len=64)
    errors += _check_iso_utc(payload["generated_at"], "generated_at", where)

    src = payload["source_record_ids"]
    if not isinstance(src, list) or not src:
        errors.append(f"{where}.source_record_ids: must be a non-empty array (traceability)")
    else:
        for i, rid in enumerate(src):
            errors += _check_str(rid, f"source_record_ids[{i}]", where, max_len=128)

    # Structural containment check: no raw-conversation field may exist anywhere.
    errors += _scan_forbidden_keys(payload, where)

    for i, d in enumerate(payload.get("decisions") or []):
        sub = f"{where}.decisions[{i}]"
        errs = _require_fields(d, ("decision_id", "outcome", "decided_by"), sub)
        errors += errs
        if errs:
            continue
        errors += _check_str(d["decision_id"], "decision_id", sub, max_len=128)
        errors += _check_str(d["decided_by"], "decided_by", sub, max_len=128)
        if "decision_note" in d:
            errors += _check_str(d["decision_note"], "decision_note", sub, max_len=2000)
    return errors


def _scan_forbidden_keys(obj: Any, where: str, depth: int = 0) -> list[str]:
    """Recursively reject keys that would smuggle raw conversation into a digest.

    Walks both objects and arrays: a forbidden field nested inside a list (for
    example inside ``decisions[]``) must be caught just as surely as a top-level
    one. Returning early on non-dicts previously made the list branch dead code.
    """
    if depth > 8:
        return []
    errors: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_DIGEST_KEYS:
                errors.append(
                    f"{where}: forbidden field {key!r} (digest must contain no raw chat)"
                )
            errors += _scan_forbidden_keys(value, f"{where}.{key}", depth + 1)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            errors += _scan_forbidden_keys(item, f"{where}[{i}]", depth + 1)
    return errors


# ---------------------------------------------------------------------------
# Durable gate record
# ---------------------------------------------------------------------------

_GATE_RECORD_ALLOWED = {
    "schema_version", "gate_id", "workflow_id", "milestone_id", "attempt_id",
    "handoff_id", "transition", "candidate_commit", "status",
    "requires_human_decision", "approvers", "requested_at", "resolved_at",
    "resolved_by", "channel_ref", "escalation_count", "last_escalated_at",
    "escalation_policy_seconds",
}

_GATE_RECORD_REQUIRED = (
    "schema_version", "gate_id", "workflow_id", "milestone_id", "attempt_id",
    "transition", "candidate_commit", "status", "requires_human_decision",
    "approvers", "requested_at", "escalation_count",
)


def validate_gate_record(payload: Any) -> list[str]:
    """Validate a durable gate record (spec 5/6/10). Returns a list of errors."""
    where = "gate_record"
    errors = _require_fields(payload, _GATE_RECORD_REQUIRED, where)
    if errors:
        return errors
    errors += _reject_unknown(payload, _GATE_RECORD_ALLOWED, where)

    if payload["schema_version"] != "1.0":
        errors.append(f"{where}.schema_version: expected '1.0'")
    errors += _check_full_sha(payload["candidate_commit"], "candidate_commit", where)
    errors += _check_enum(payload["transition"], "transition", where,
                          TRANSITIONS + ("reviewer_to_security_reviewer",))
    errors += _check_enum(payload["status"], "status", where, GATE_STATUSES)
    errors += _check_iso_utc(payload["requested_at"], "requested_at", where)

    if not isinstance(payload["requires_human_decision"], bool):
        errors.append(f"{where}.requires_human_decision: must be a boolean")

    count = payload["escalation_count"]
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        errors.append(f"{where}.escalation_count: must be a non-negative integer")

    approvers = payload["approvers"]
    errs = _require_fields(approvers, ("platform", "user_ids"), f"{where}.approvers")
    errors += errs
    if not errs:
        errors += _check_str(approvers["platform"], "platform", f"{where}.approvers", max_len=64)
        if not isinstance(approvers["user_ids"], list):
            errors.append(f"{where}.approvers.user_ids: must be an array (may be empty)")

    status = payload["status"]
    if status == "pending" and "resolved_by" in payload:
        errors.append(f"{where}: pending gate must not carry 'resolved_by'")
    if status in ("approved", "denied"):
        for f in ("resolved_at", "resolved_by"):
            if f not in payload:
                errors.append(f"{where}: status {status!r} requires {f!r}")
    if "resolved_by" in payload:
        rb = payload["resolved_by"]
        errs = _require_fields(rb, ("platform", "user_id"), f"{where}.resolved_by")
        errors += errs
        if not errs:
            errors += _check_str(rb["platform"], "platform", f"{where}.resolved_by", max_len=64)
            errors += _check_str(rb["user_id"], "user_id", f"{where}.resolved_by", max_len=128)
    return errors


# ---------------------------------------------------------------------------
# Convenience: raise on error (for callers that want fail-fast)
# ---------------------------------------------------------------------------

_VALIDATORS = {
    "handoff": validate_handoff,
    "review": validate_review_findings,
    "gate_decision": validate_gate_decision,
    "digest": validate_decision_digest,
    "gate_record": validate_gate_record,
}


def require_valid(kind: str, payload: Any, **kwargs: Any) -> None:
    """Validate ``payload`` as ``kind`` and raise :class:`ValidationError` on failure."""
    try:
        fn = _VALIDATORS[kind]
    except KeyError:
        raise ValueError(f"unknown contract kind: {kind!r}") from None
    errors = fn(payload, **kwargs)
    if errors:
        raise ValidationError(errors)
