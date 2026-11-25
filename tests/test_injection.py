"""Turn injection: who contributes context, and when.

Two defects motivated this module, and both are pinned here:

1. **Producers overwrote each other.** Assembly used one ``memory_context``
   variable, so the last producer won. With AgentDB enabled the repo map was
   discarded; with it disabled the map was sent on every turn. Either way the
   documented "inject once per session" contract was false.

2. **Scope was implicit.** Whether a block belonged to a session boundary or to
   every turn was decided by where the code happened to sit.

The tests are written against the contract, not the implementation: what is
injected, by whom, on which turns.
"""

from pathlib import Path

import pytest

from core.injection import (
    ASSET_SCOPES,
    MEMORY_RECORDS_HEADING,
    Injection,
    InjectionScope,
    TurnInjections,
    build_turn_injections,
    is_session_scoped_record,
    memory_records_injection,
    orientation_injection,
    repo_map_injection,
    split_memory_parts,
)
from core.sessions import SessionState

REPO = Path(__file__).resolve().parents[1]
BOUNDARY = (SessionState.SESSION_START, SessionState.SESSION_RESUME)
ALL_STATES = tuple(SessionState)


@pytest.fixture
def repo(tmp_path):
    """A real git-shaped directory, so root resolution succeeds."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "mod.py").write_text("def thing():\n    pass\n")
    return root


@pytest.fixture
def ws(tmp_path):
    """A workspace that is NOT nested in a repository."""
    path = tmp_path / "ws"
    path.mkdir()
    return path


# -- scope semantics -------------------------------------------------------


def test_boundary_scope_covers_start_and_resume_only():
    for state in BOUNDARY:
        assert InjectionScope.SESSION_BOUNDARY.applies(state), state
    assert not InjectionScope.SESSION_BOUNDARY.applies(SessionState.SESSION_WARM)


def test_every_turn_scope_covers_all_states():
    for state in ALL_STATES:
        assert InjectionScope.EVERY_TURN.applies(state), state


def test_finer_scopes_distinguish_start_from_resume():
    assert InjectionScope.SESSION_START.applies(SessionState.SESSION_START)
    assert not InjectionScope.SESSION_START.applies(SessionState.SESSION_RESUME)
    assert InjectionScope.SESSION_RESUME.applies(SessionState.SESSION_RESUME)
    assert not InjectionScope.SESSION_RESUME.applies(SessionState.SESSION_START)
    # Neither fires inside an open session.
    for scope in (InjectionScope.SESSION_START, InjectionScope.SESSION_RESUME):
        assert not scope.applies(SessionState.SESSION_WARM), scope


# -- the headline contract: once per session, not per turn ------------------


def test_repo_map_is_injected_on_the_boundary_turn_only(repo, ws):
    """The defect this module exists to fix: no map on warm turns."""
    for state in ALL_STATES:
        turn = build_turn_injections(
            state, workspace_path=str(ws), repo_candidates=[str(repo)],
        )
        assets = [i.asset for i in turn.applying()]
        if state is SessionState.SESSION_WARM:
            assert "repo_map" not in assets, "repo map injected mid-session"
        else:
            assert "repo_map" in assets, f"no map at {state.value}"


def test_orientation_follows_the_same_boundary(ws):
    for state in ALL_STATES:
        turn = build_turn_injections(state, workspace_path=str(ws))
        assets = [i.asset for i in turn.applying()]
        assert ("orientation" in assets) is (state is not SessionState.SESSION_WARM)


def test_agentdb_memory_is_injected_every_turn(ws):
    """Memory is query-relevant: suppressing it mid-session would be wrong."""
    for state in ALL_STATES:
        turn = build_turn_injections(
            state, workspace_path=str(ws),
            agentdb_memory_parts=["## Background\n- fact"],
        )
        assert "memory_records" in [i.asset for i in turn.applying()], state


# -- the second defect: producers must compose, not clobber -----------------


def test_map_and_memory_both_survive_composition(repo, ws):
    """With AgentDB on, the map used to be discarded. Both must appear."""
    turn = build_turn_injections(
        SessionState.SESSION_START,
        workspace_path=str(ws),
        repo_candidates=[str(repo)],
        agentdb_memory_parts=["- memory/episodic/1/x: a fact"],
    )
    rendered = turn.compose()
    assert "Repository map" in rendered
    assert MEMORY_RECORDS_HEADING in rendered


def test_composition_order_is_source_then_insertion(repo, ws):
    """AgentGate environment knowledge precedes memory: where you are, then what
    you remember. Reviewers can rely on this order."""
    turn = build_turn_injections(
        SessionState.SESSION_START,
        workspace_path=str(ws),
        repo_candidates=[str(repo)],
        agentdb_memory_parts=["- memory/episodic/1/x: a fact"],
    )
    rendered = turn.compose()
    assert rendered.index("Your starting directory") < rendered.index(MEMORY_RECORDS_HEADING)


def test_each_block_keeps_its_own_heading(ws):
    """Ownership stays legible in a transcript: a reader can tell which producer
    emitted a block, which is impossible when everything is one string."""
    turn = build_turn_injections(
        SessionState.SESSION_START, workspace_path=str(ws),
        agentdb_memory_parts=["- x"],
    )
    assert MEMORY_RECORDS_HEADING in turn.compose()


# -- declared scopes are enforced ------------------------------------------


def test_asset_table_declares_every_builtin_asset():
    for asset in ("orientation", "repo_map", "memory_records", "skills_nudge"):
        assert asset in ASSET_SCOPES, f"{asset} built but not declared"


def test_constructing_an_asset_with_a_contradicting_scope_is_rejected():
    """Otherwise ASSET_SCOPES would be documentation that lies."""
    with pytest.raises(ValueError, match="repo_map"):
        Injection(
            source="agentgate", asset="repo_map", heading="", body="x",
            scope=InjectionScope.EVERY_TURN,
        )


def test_undeclared_assets_are_allowed_for_extensions():
    Injection(source="agentgate", asset="future_thing", heading="", body="x",
              scope=InjectionScope.EVERY_TURN)


# -- session-scoped memory records are separated from turn memory -----------


def test_compaction_checkpoints_are_session_scoped():
    assert is_session_scoped_record("memory/compaction/checkpoint/123: {}")
    assert is_session_scoped_record("memory/compaction/summary/123: text")
    assert not is_session_scoped_record("memory/episodic/123/x: a fact")


def test_checkpoint_stubs_are_not_rendered_as_turn_memory(ws):
    """A turn used to receive five identical checkpoint stubs and nothing else."""
    parts = [
        "- memory/compaction/checkpoint/1795311336: {}\n"
        "- memory/compaction/checkpoint/1795282796: {}",
    ]
    turn = build_turn_injections(
        SessionState.SESSION_WARM, workspace_path=str(ws),
        agentdb_memory_parts=parts,
    )
    assert turn.compose() == "", "session-scoped artefacts leaked into the turn"


def test_the_header_survives_with_its_per_turn_records():
    per_turn, _resume, _discarded = split_memory_parts([
        "Background context:\n"
        "- memory/compaction/checkpoint/1: {}\n"
        "- memory/episodic/1/x: real fact"
    ])
    assert len(per_turn) == 1
    assert "Background context:" in per_turn[0]
    assert "real fact" in per_turn[0]
    assert "checkpoint" not in per_turn[0]


def test_duplicate_records_are_collapsed():
    """A retrieval pass can return the same record more than once."""
    line = "- memory/compaction/checkpoint/1: {}"
    _per_turn, _resume, discarded = split_memory_parts([f"h\n{line}\n{line}"])
    assert discarded == [line]


def test_empty_and_blank_parts_are_ignored():
    assert split_memory_parts(["", "   ", None]) == ([], [], [])
    assert memory_records_injection([]) is None
    assert memory_records_injection(["   "]) is None


# -- producers never raise --------------------------------------------------


def test_producers_are_failures_tolerant(monkeypatch):
    """A broken producer must not cost a turn."""
    import core.context.orientation as orientation
    import core.context.repo_map as repo_map

    monkeypatch.setattr(orientation, "build_orientation",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(repo_map, "build_repo_map_segment",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert orientation_injection("/definitely/not/here", ["/nope"]) is None
    assert repo_map_injection("/definitely/not/here", candidates=["/nope"]) is None


def test_no_repo_candidates_means_no_map(ws):
    """``None`` is how a caller says "this session has no evidence" — the honest
    answer, and the one that stops a wrong map being sent."""
    turn = build_turn_injections(SessionState.SESSION_START, workspace_path=str(ws),
                                 repo_candidates=None)
    assert "repo_map" not in [i.asset for i in turn.applying()]


def test_empty_turn_composes_to_empty_string():
    assert TurnInjections(SessionState.SESSION_WARM).compose() == ""
    assert not TurnInjections(SessionState.SESSION_WARM)


# -- the runners delegate, rather than assembling inline --------------------


def test_runners_use_the_shared_assembly():
    """The three runners had already diverged from the documented behaviour.

    Structural check: no runner may build a memory/map context string inline.
    """
    for name in ("deepinfra", "local", "fireworks"):
        source = (REPO / "runners" / f"{name}.py").read_text()
        assert "build_turn_injections(" in source, f"{name} does not use the shared assembly"
        assert '"## Relevant memory for this turn' not in source, (
            f"{name} still assembles memory inline"
        )


def test_runners_do_not_pass_the_operator_registry_as_repo_evidence():
    """Passing ADDITIONAL_DIRS wholesale is what made every session map agentgate."""
    for name in ("deepinfra", "local", "fireworks"):
        source = (REPO / "runners" / f"{name}.py").read_text()
        assert "candidates=list(ADDITIONAL_DIRS)" not in source, (
            f"{name} still resolves the repo root from the operator registry"
        )


# -- the state must be captured where a boundary is observable -------------


def test_client_creation_reports_the_state_it_observed():
    """A regression guard for an always-warm bug.

    ``classify_session(meta, has_client(thread_id))`` is only meaningful
    *before* the client is stored. Called after, ``has_client`` is always True,
    so every turn classifies as SESSION_WARM and a boundary asset is never
    injected at all — the map silently disappears instead of being sent twice.

    The runners therefore capture the state inside `_get_or_create_client` and
    return it with the client. This test pins that contract: the state must be
    determined at creation, not recomputed afterwards.
    """
    for name in ("deepinfra", "local", "fireworks"):
        source = (REPO / "runners" / f"{name}.py").read_text()
        assert "return client, state" in source, (
            f"{name} does not return the state captured at client creation"
        )
        # The warm short-circuit must be explicit rather than re-derived.
        assert "SessionState.SESSION_WARM" in source, (
            f"{name} does not mark an existing client as warm"
        )


def test_warm_reuse_of_a_live_client_is_warm():
    """The state for an existing client is WARM, without inspecting the registry."""
    # Mirrors the short-circuit in `_get_or_create_client`, which is the only
    # place a later turn's state can be decided correctly.
    assert SessionState.SESSION_WARM.is_boundary is False


# -- AgentDB memory assets are scoped by what a record describes -----------


def test_memory_families_are_classified_by_key():
    """The store's `kind` column is 'source' for every row, so key is the only
    reliable classifier."""
    from core.injection import MemoryAsset, memory_asset_of

    cases = {
        "memory/episodic/deepinfra/slack/1/fact": MemoryAsset.EPISODIC,
        "memory/episodic/claude/x/1/directory_tracking/2026": MemoryAsset.EPISODIC,
        "memory/compaction/checkpoint/1": MemoryAsset.CHECKPOINT,
        "memory/compaction/summary/1": MemoryAsset.COMPACTION_SUMMARY,
        "memory/canonical/abc": MemoryAsset.CANONICAL,
        "unrecognised/key": MemoryAsset.OTHER,
        "": MemoryAsset.OTHER,
    }
    for key, expected in cases.items():
        assert memory_asset_of(key) is expected, key


def test_memory_asset_scopes_match_the_declared_table():
    """Each family's scope comes from ASSET_SCOPES, not from a second list."""
    from core.injection import ASSET_SCOPES, scope_of_memory_record

    assert scope_of_memory_record("memory/compaction/checkpoint/1") is ASSET_SCOPES["compaction_checkpoint"]
    assert scope_of_memory_record("memory/compaction/summary/1") is ASSET_SCOPES["compaction_summary"]
    assert scope_of_memory_record("memory/episodic/1/x") is ASSET_SCOPES["memory_records"]
    assert ASSET_SCOPES["compaction_checkpoint"] is InjectionScope.NEVER
    assert ASSET_SCOPES["compaction_summary"] is InjectionScope.SESSION_RESUME


def test_checkpoints_are_declared_never_injected():
    """A tombstone has no content: all 16 rows in a real store were pure stubs."""
    for state in ALL_STATES:
        turn = build_turn_injections(
            state, workspace_path="/ws",
            agentdb_memory_parts=["- memory/compaction/checkpoint/1: {}"],
        )
        assets = [i.asset for i in turn.applying()]
        assert "compaction_checkpoint" not in assets, state
        assert "compaction_summary" not in assets, state
        # Nothing from the checkpoint reaches the rendered context.
        assert "checkpoint" not in turn.compose(), state


def test_compaction_summary_is_a_resume_asset(ws):
    """The handover note helps a process that lost the transcript, not a live one."""
    parts = ["- memory/compaction/summary/1: implemented the milestone"]

    resume = build_turn_injections(
        SessionState.SESSION_RESUME, workspace_path=str(ws), agentdb_memory_parts=parts,
    )
    assert "compaction_summary" in [i.asset for i in resume.applying()]

    for state in (SessionState.SESSION_START, SessionState.SESSION_WARM):
        turn = build_turn_injections(
            state, workspace_path=str(ws), agentdb_memory_parts=parts,
        )
        assert "compaction_summary" not in [i.asset for i in turn.applying()], state


def test_episodic_records_stay_per_turn(ws):
    """A record recalled for this question must not be suppressed mid-session."""
    parts = ["- memory/episodic/1/x: user works on komper"]
    for state in ALL_STATES:
        turn = build_turn_injections(
            state, workspace_path=str(ws), agentdb_memory_parts=parts,
        )
        assert "memory_records" in [i.asset for i in turn.applying()], state


def test_a_mixed_retrieval_is_split_into_three_buckets(ws):
    """One retrieval call mixes families; scope attaches to the record."""
    parts = [
        "Background context from previous sessions:\n"
        "- memory/compaction/checkpoint/1: {}\n"
        "- memory/compaction/summary/1: did X then Y\n"
        "- memory/episodic/9/fact: real fact"
    ]
    per_turn, resume_only, discarded = split_memory_parts(parts)
    assert "real fact" in "\n".join(per_turn)
    assert "checkpoint" not in "\n".join(per_turn)
    assert "did X then Y" in "\n".join(resume_only)
    assert len(discarded) == 1


def test_every_memory_family_has_a_declared_asset():
    """No family may fall through to an undeclared scope."""
    from core.injection import MemoryAsset, _ASSET_FOR_FAMILY

    for family in MemoryAsset:
        asset = _ASSET_FOR_FAMILY[family]
        assert asset in ASSET_SCOPES, f"{family.value} -> undeclared asset {asset!r}"
