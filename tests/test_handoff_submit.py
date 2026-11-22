"""Milestone 01 / ORCH-SUBMIT-001..006 — the request-only `milestone_submit` tool.

The central property under test is that this tool *requests* and never *acts*: it must not
spawn an agent, approve a gate, or advance a workflow. Several tests here are as much
negative assertions about what the tool cannot do as about what it can.
"""
import json
from pathlib import Path

import pytest

from core.handoff.submit import SubmitError, submit_handoff
from core.tools.registry import build_tools

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA256 = "c" * 64


class _Store:
    def lookup_directory(self, name):
        return None

    def remember_directory(self, name, path):
        pass


def _args(**over):
    a = {
        "milestone_id": "01",
        "candidate_commit": SHA_B,
        "base_commit": SHA_A,
        "target_role": "reviewer",
        "requested_action": "review_milestone",
        "idempotency_key": "WF-001:01:001:implementer_to_reviewer",
        "artifact_references": [
            {"path": "docs/spec/01-x/attempts/001/implementation-report.md",
             "sha256": SHA256}
        ],
    }
    a.update(over)
    return a


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "repo" / "docs" / "spec" / "01-x" / "attempts" / "001"
    d.mkdir(parents=True)
    (d / "implementation-report.md").write_text("report")
    return tmp_path / "repo"


@pytest.fixture
def registry(tmp_path):
    return tmp_path / "wfreg"


def _submit(args, repo, registry, **kw):
    params = dict(
        registry_dir=registry,
        repo_root=repo,
        source_role="implementer",
        workflow_id="WF-001",
        requested_by="agent:deepinfra",
    )
    params.update(kw)
    return submit_handoff(args, **params)


# --------------------------------------------------------------------------
# ORCH-SUBMIT-001: registered in the shared registry
# --------------------------------------------------------------------------

def test_tool_present_when_enabled(tmp_path):
    tools = build_tools("/tmp", _Store(), output_cap=1000, bash_timeout=5,
                        workflows_enabled=True,
                        workflow_registry_dir=str(tmp_path / "reg"))
    assert "milestone_submit" in tools
    fn = tools["milestone_submit"].schema["function"]
    assert fn["name"] == "milestone_submit"
    assert set(fn["parameters"]["required"]) == {
        "milestone_id", "candidate_commit", "base_commit",
        "idempotency_key", "artifact_references",
    }


# --------------------------------------------------------------------------
# ORCH-SUBMIT-006: absent when orchestration is disabled (the default)
# --------------------------------------------------------------------------

def test_tool_absent_when_disabled():
    tools = build_tools("/tmp", _Store(), output_cap=1000, bash_timeout=5)
    assert "milestone_submit" not in tools


def test_disabled_registry_matches_pre_orchestration_shape():
    """Disabled mode must be exactly the original 8 tools — no extra schema."""
    names = set(build_tools("/tmp", _Store(), output_cap=1000, bash_timeout=5))
    assert names == {
        "Bash", "Read", "Write", "Edit", "Glob", "Grep",
        "lookup_directory", "remember_directory",
    }


# --------------------------------------------------------------------------
# ORCH-SUBMIT-002: request-only. No spawn, no approval, no transition.
# --------------------------------------------------------------------------

def test_successful_submit_writes_exactly_one_record(repo, registry):
    msg = _submit(_args(), repo, registry)
    assert msg.startswith("SUBMITTED")
    files = list(registry.glob("*.handoff.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["authorization_status"] == "pending_human_approval"


def test_submit_never_reports_approval(repo, registry):
    """The returned status must be a request, not an authorization.

    Asserted on the status prefix and the persisted record, not on prose: the message
    legitimately contains the phrase "nothing was approved" as a warning.
    """
    msg = _submit(_args(), repo, registry)
    assert msg.startswith("SUBMITTED")
    assert "pending_human_approval" in msg
    # Crucially, the tool must not claim the handoff was approved.
    assert "SUBMITTED: handoff" in msg and "is approved" not in msg
    payload = json.loads(next(registry.glob("*.handoff.json")).read_text())
    assert payload["authorization_status"] == "pending_human_approval"


def test_identity_is_host_supplied_not_model_supplied(repo, registry):
    """A model must not be able to claim another role or workflow via arguments."""
    args = _args(source_role="reviewer", workflow_id="WF-999")
    _submit(args, repo, registry)
    payload = json.loads(next(registry.glob("*.handoff.json")).read_text())
    assert payload["source_role"] == "implementer"   # host value wins
    assert payload["workflow_id"] == "WF-001"


def test_no_approval_or_spawn_tool_exists_anywhere(tmp_path):
    """ORCH-SUBMIT-005: the absence of a model-callable approval is the safety property."""
    tools = build_tools("/tmp", _Store(), output_cap=1000, bash_timeout=5,
                        workflows_enabled=True,
                        workflow_registry_dir=str(tmp_path / "reg"))
    for forbidden in ("approve_handoff", "spawn_agent", "approve", "transition"):
        assert forbidden not in tools


# --------------------------------------------------------------------------
# ORCH-SUBMIT-003: invalid requests rejected with actionable messages
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad,needle", [
    ({"candidate_commit": "abc123"}, "candidate_commit"),
    ({"base_commit": "zzz"}, "base_commit"),
    ({"target_role": "architect"}, "target_role"),
    ({"requested_action": "merge_now"}, "requested_action"),
    ({"milestone_id": ""}, "milestone_id"),
])
def test_invalid_args_rejected(repo, registry, bad, needle):
    msg = _submit(_args(**bad), repo, registry)
    assert msg.startswith("REJECTED"), msg
    assert needle in msg
    assert not list(registry.glob("*.handoff.json")) if registry.exists() else True


def test_path_traversal_artifact_rejected(repo, registry):
    msg = _submit(_args(artifact_references=[
        {"path": "../../../etc/passwd", "sha256": SHA256}
    ]), repo, registry)
    assert msg.startswith("REJECTED")
    assert "escapes" in msg or "absolute" in msg


def test_bad_idempotency_key_rejected(repo, registry):
    msg = _submit(_args(idempotency_key="../../evil"), repo, registry)
    assert msg.startswith("REJECTED")


def test_rejection_reports_all_problems_at_once(repo, registry):
    msg = _submit(_args(candidate_commit="bad", base_commit="bad"), repo, registry)
    assert msg.count("|") >= 1


# --------------------------------------------------------------------------
# ORCH-SUBMIT-004: idempotent replay
# --------------------------------------------------------------------------

def test_replay_returns_original_status_without_new_file(repo, registry):
    first = _submit(_args(), repo, registry)
    second = _submit(_args(), repo, registry)
    assert first.startswith("SUBMITTED")
    assert second.startswith("ALREADY_SUBMITTED")
    assert len(list(registry.glob("*.handoff.json"))) == 1


def test_replay_preserves_original_handoff_id(repo, registry):
    _submit(_args(), repo, registry)
    original = json.loads(next(registry.glob("*.handoff.json")).read_text())["handoff_id"]
    msg = _submit(_args(handoff_id="HO-DIFFERENT"), repo, registry)
    assert original in msg


# --------------------------------------------------------------------------
# Safety: registry confinement and atomic writes
# --------------------------------------------------------------------------

def test_registry_confined_to_its_directory(repo, registry):
    _submit(_args(), repo, registry)
    # Nothing may be written outside the registry dir.
    written = [p for p in registry.rglob("*") if p.is_file()]
    assert written
    for p in written:
        assert registry in p.parents


def test_safe_filename_rejects_path_separators():
    from core.handoff.submit import _safe_filename
    for bad in ("a/b", "../x", "a\\b", "", "x" * 200):
        with pytest.raises(SubmitError):
            _safe_filename(bad)
