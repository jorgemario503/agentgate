"""Milestone 01 / ORCH-CONTRACT-001..006 — orchestration JSON contract validators.

Pure-function tests: no I/O beyond the schemas directory, no runner, no network.
Each acceptance criterion has at least one negative case, because a validator that
only accepts valid input proves nothing.
"""
import json
from pathlib import Path

import pytest

from core.orchestration import (
    ValidationError,
    check_path_contained,
    require_valid,
    validate_decision_digest,
    validate_gate_decision,
    validate_gate_record,
    validate_handoff,
    validate_review_findings,
)

SCHEMAS = Path(__file__).resolve().parents[1] / "core" / "orchestration" / "schemas"

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA256 = "c" * 64


# --------------------------------------------------------------------------
# Fixtures: one minimal-valid payload per contract. Tests mutate a copy.
# --------------------------------------------------------------------------

def _handoff(**over):
    p = {
        "schema_version": "1.0",
        "workflow_id": "WF-001",
        "milestone_id": "02",
        "attempt_id": "001",
        "handoff_id": "HO-001",
        "idempotency_key": "WF-001:02:001:implementer_to_reviewer",
        "source_role": "implementer",
        "target_role": "reviewer",
        "requested_action": "review_milestone",
        "spec_version": "1.0",
        "base_commit": SHA_A,
        "candidate_commit": SHA_B,
        "artifact_references": [
            {"path": "docs/spec/02-x/attempts/001/implementation-report.md",
             "sha256": SHA256}
        ],
        "created_at": "2026-11-22T00:00:00Z",
        "requested_by": "agent:implementer",
        "authorization_status": "pending_human_approval",
    }
    p.update(over)
    return p


def _review(**over):
    p = {
        "schema_version": "1.0",
        "handoff_id": "HO-001",
        "attempt_id": "001",
        "reviewed_commit": SHA_B,
        "recommendation": "APPROVED",
        "acceptance_criteria": [
            {"requirement_id": "ORCH-SKILL-001", "status": "pass",
             "evidence": "test_role_skills_are_discovered"}
        ],
        "findings": [],
        "open_questions": [],
        "tests_performed": ["pytest -q"],
        "tests_not_performed": [],
        "residual_risks": [],
        "recommended_next_action": "accept",
    }
    p.update(over)
    return p


def _gate(**over):
    p = {
        "schema_version": "1.0",
        "milestone_id": "02",
        "attempt_id": "001",
        "spec_version": "1.0",
        "candidate_commit": SHA_B,
        "gate_type": "handoff_approval",
        "decision_authority": "human",
        "decision_status": "pending",
        "next_action": "await-human-decision",
    }
    p.update(over)
    return p


def _digest(**over):
    p = {
        "digest_schema_version": "1.0",
        "digest_id": "DG-001",
        "milestone_id": "02",
        "attempt_id": "001",
        "source_record_ids": ["gate-001", "review-001"],
        "generated_at": "2026-11-22T00:00:00Z",
        "decisions": [
            {"decision_id": "D-001", "outcome": "CHANGES_REQUESTED",
             "decided_by": "U123", "decision_note": "Please fix F-001."}
        ],
    }
    p.update(over)
    return p


def _gate_record(**over):
    p = {
        "schema_version": "1.0",
        "gate_id": "gate-001",
        "workflow_id": "WF-001",
        "milestone_id": "02",
        "attempt_id": "001",
        "transition": "implementer_to_reviewer",
        "candidate_commit": SHA_B,
        "status": "pending",
        "requires_human_decision": True,
        "approvers": {"platform": "slack", "user_ids": ["U123"]},
        "requested_at": "2026-11-22T00:00:00Z",
        "escalation_count": 0,
    }
    p.update(over)
    return p


# --------------------------------------------------------------------------
# ORCH-CONTRACT-001: schemas exist and are versioned, valid JSON
# --------------------------------------------------------------------------

SCHEMA_FILES = {
    "handoff.schema.json": "1.0",
    "review-findings.schema.json": "1.0",
    "gate-decision.schema.json": "1.0",
    "decision-digest.schema.json": "1.0",
    "gate-record.schema.json": "1.0",
}


@pytest.mark.parametrize("filename,version", sorted(SCHEMA_FILES.items()))
def test_schema_files_exist_and_parse(filename, version):
    path = SCHEMAS / filename
    assert path.is_file(), f"missing schema {filename}"
    doc = json.loads(path.read_text())
    assert doc["title"]
    # Each schema pins its version via a const on the version field.
    props = doc["properties"]
    version_key = next(k for k in props if k.endswith("schema_version"))
    assert props[version_key]["const"] == version


def test_schemas_carry_no_bom_and_are_utf8():
    for filename in SCHEMA_FILES:
        raw = (SCHEMAS / filename).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{filename} has a BOM"
        raw.decode("utf-8")


# --------------------------------------------------------------------------
# ORCH-CONTRACT-002: malformed payloads are rejected
# --------------------------------------------------------------------------

def test_valid_payloads_pass():
    assert validate_handoff(_handoff()) == []
    assert validate_review_findings(_review()) == []
    assert validate_gate_decision(_gate()) == []
    assert validate_decision_digest(_digest()) == []
    assert validate_gate_record(_gate_record()) == []


@pytest.mark.parametrize("missing", list(_handoff().keys()))
def test_handoff_missing_required_field_rejected(missing):
    p = _handoff()
    del p[missing]
    errors = validate_handoff(p)
    assert errors, f"missing {missing!r} was accepted"
    assert any(missing in e for e in errors)


def test_handoff_rejects_unknown_field():
    errors = validate_handoff(_handoff(unexpected="x"))
    assert any("unknown field" in e for e in errors)


def test_handoff_rejects_wrong_schema_version():
    errors = validate_handoff(_handoff(schema_version="2.0"))
    assert any("schema_version" in e for e in errors)


def test_handoff_rejects_same_role():
    errors = validate_handoff(_handoff(source_role="reviewer", target_role="reviewer"))
    assert any("must differ" in e for e in errors)


def test_review_rejects_bad_recommendation():
    errors = validate_review_findings(_review(recommendation="LGTM"))
    assert any("recommendation" in e for e in errors)


def test_review_requires_nonempty_acceptance_matrix():
    errors = validate_review_findings(_review(acceptance_criteria=[]))
    assert any("acceptance_criteria" in e for e in errors)


def test_review_rejects_bad_acceptance_status():
    bad = _review(acceptance_criteria=[
        {"requirement_id": "X", "status": "probably", "evidence": "vibes"}
    ])
    assert any("status" in e for e in validate_review_findings(bad))


def test_review_rejects_duplicate_finding_ids():
    f = {"id": "F-001", "severity": "major", "summary": "s",
         "impact": "i", "remediation": "r"}
    errors = validate_review_findings(_review(findings=[f, dict(f)]))
    assert any("duplicate" in e for e in errors)


def test_review_rejects_malformed_finding_id():
    f = {"id": "finding-1", "severity": "major", "summary": "s",
         "impact": "i", "remediation": "r"}
    assert any("F-<number>" in e for e in validate_review_findings(_review(findings=[f])))


def test_gate_rejects_recorded_without_decider():
    errors = validate_gate_decision(_gate(decision_status="recorded"))
    assert any("decided_by" in e for e in errors)


def test_gate_rejects_pending_with_decider():
    errors = validate_gate_decision(_gate(decided_by="U123"))
    assert any("must not carry" in e for e in errors)


def test_gate_record_rejects_pending_with_resolver():
    errors = validate_gate_record(_gate_record(
        resolved_by={"platform": "slack", "user_id": "U1"}))
    assert any("must not carry" in e for e in errors)


def test_gate_record_requires_resolver_when_approved():
    errors = validate_gate_record(_gate_record(status="approved"))
    assert any("resolved_by" in e for e in errors)


def test_gate_record_allows_empty_approvers_but_contract_is_visible():
    # Empty approver set is structurally valid (it fails closed at the policy
    # layer, not the schema layer). Documented here so the boundary is explicit.
    assert validate_gate_record(_gate_record(
        approvers={"platform": "slack", "user_ids": []})) == []


# --------------------------------------------------------------------------
# ORCH-CONTRACT-003: path containment
# --------------------------------------------------------------------------

@pytest.fixture
def repo_root(tmp_path):
    (tmp_path / "docs" / "spec").mkdir(parents=True)
    (tmp_path / "docs" / "spec" / "report.md").write_text("x")
    return tmp_path


def test_contained_path_accepted(repo_root):
    assert check_path_contained("docs/spec/report.md", repo_root) is None


@pytest.mark.parametrize("bad", [
    "../escape.md",
    "docs/../../escape.md",
    "/etc/passwd",
])
def test_unsafe_paths_rejected(repo_root, bad):
    assert check_path_contained(bad, repo_root) is not None


def test_symlink_escape_rejected(repo_root):
    outside = repo_root.parent / "outside.md"
    outside.write_text("secret")
    link = repo_root / "docs" / "link.md"
    link.symlink_to(outside)
    assert check_path_contained("docs/link.md", repo_root) is not None


def test_handoff_validates_artifact_paths_when_root_given(repo_root):
    p = _handoff(artifact_references=[
        {"path": "../escape.md", "sha256": SHA256}
    ])
    errors = validate_handoff(p, repo_root=repo_root)
    assert any("escapes" in e or "absolute" in e for e in errors)


# --------------------------------------------------------------------------
# ORCH-CONTRACT-004: full SHA and digest enforcement
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad_sha", ["abc123", "a" * 39, "A" * 40, "z" * 40])
def test_abbreviated_or_invalid_sha_rejected(bad_sha):
    assert validate_handoff(_handoff(candidate_commit=bad_sha))


def test_sha256_must_be_64_hex():
    p = _handoff(artifact_references=[{"path": "docs/x.md", "sha256": "abc"}])
    assert any("sha256" in e for e in validate_handoff(p))


# --------------------------------------------------------------------------
# ORCH-CONTRACT-005: pure, importable, side-effect free
# --------------------------------------------------------------------------

def test_validators_do_not_mutate_input():
    p = _handoff()
    snapshot = json.dumps(p, sort_keys=True)
    validate_handoff(p)
    assert json.dumps(p, sort_keys=True) == snapshot


def test_validation_error_carries_all_errors():
    with pytest.raises(ValidationError) as exc:
        require_valid("handoff", {"schema_version": "1.0"})
    assert len(exc.value.errors) > 1


def test_unknown_kind_raises_valueerror():
    with pytest.raises(ValueError):
        require_valid("nope", {})


# --------------------------------------------------------------------------
# ORCH-CONTRACT-006: role allowlist and transition allowlist
# --------------------------------------------------------------------------

def test_unknown_role_rejected():
    assert validate_handoff(_handoff(target_role="architect"))


def test_gate_record_rejects_unknown_transition():
    assert validate_gate_record(_gate_record(transition="anything_goes"))


# --------------------------------------------------------------------------
# Digest: no raw conversation may cross (D2 containment, spec 4.4)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("forbidden", [
    "transcript", "raw_chat", "chat", "messages", "conversation",
    "history", "reasoning", "agent_notes", "summary",
])
def test_digest_rejects_raw_conversation_fields(forbidden):
    p = _digest(**{forbidden: "reviewer said: still broken"})
    assert validate_decision_digest(p), f"{forbidden!r} was accepted"


def test_digest_rejects_nested_transcript():
    p = _digest(decisions=[{
        "decision_id": "D-1", "outcome": "APPROVED", "decided_by": "U1",
        "transcript": "full slack thread",
    }])
    assert any("forbidden field" in e for e in validate_decision_digest(p))


def test_digest_requires_source_record_ids():
    p = _digest(source_record_ids=[])
    assert validate_decision_digest(p)


def test_digest_allows_attributed_bounded_decision_note():
    assert validate_decision_digest(_digest()) == []


# --------------------------------------------------------------------------
# Drift guard: the JSON schema files and the code constants must agree.
# Without this, the documented contract and the enforced contract can diverge
# silently — which is worse than having only one of them.
# --------------------------------------------------------------------------

from core.orchestration import validate as _v  # noqa: E402

_SCHEMA_CODE_PAIRS = [
    ("handoff.schema.json", _v._HANDOFF_REQUIRED, _v._HANDOFF_ALLOWED),
    ("review-findings.schema.json", _v._REVIEW_REQUIRED, _v._REVIEW_ALLOWED),
    ("gate-decision.schema.json", _v._GATE_REQUIRED, _v._GATE_ALLOWED),
    ("gate-record.schema.json", _v._GATE_RECORD_REQUIRED, _v._GATE_RECORD_ALLOWED),
    ("decision-digest.schema.json", _v._DIGEST_REQUIRED, _v._DIGEST_ALLOWED),
]


@pytest.mark.parametrize("filename,required,allowed", _SCHEMA_CODE_PAIRS,
                         ids=[p[0] for p in _SCHEMA_CODE_PAIRS])
def test_schema_matches_validator_contract(filename, required, allowed):
    """The documented schema and the enforced validator must describe the same shape."""
    doc = json.loads((SCHEMAS / filename).read_text())
    assert set(doc["required"]) == set(required), (
        f"{filename} required fields drifted from the validator"
    )
    assert set(doc["properties"]) == set(allowed), (
        f"{filename} properties drifted from the validator"
    )
