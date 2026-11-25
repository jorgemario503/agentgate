"""Static per-session context: built at the session boundary, not every turn.

Regression origin: the repo map was rebuilt and re-injected on *every turn*.

An earlier fix added a per-thread cache in each runner so the map was rebuilt
only once per session. That helped the re-scan but not the re-*send*: the
assembled string was still placed into a turn-local variable on every turn, so
identical text went to the model on each message — and was silently discarded
whenever AgentDB memory was present.

Scope now lives in :mod:`core.injection`, where it is declared per asset rather
than inferred from where the code sits. These tests keep the original
guarantees (one build per boundary, orientation survives a failing map) and
assert them at the seam that actually decides injection.
"""
import pytest

from core.injection import InjectionScope, TurnInjections, build_turn_injections
from core.sessions import SessionState

BOUNDARY_STATES = (SessionState.SESSION_START, SessionState.SESSION_RESUME)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "mod.py").write_text("def thing():\n    pass\n")
    return root


@pytest.fixture
def ws(tmp_path):
    # Not inside a repository, so it cannot be mistaken for one.
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def test_map_built_once_across_turns(repo, ws, monkeypatch):
    """One build serves the whole session, however many warm turns follow."""
    import core.context.repo_map as repo_map

    calls = []
    real = repo_map.build_repo_map_segment

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(repo_map, "build_repo_map_segment", counting)

    # A start turn builds and injects.
    first = build_turn_injections(
        SessionState.SESSION_START, workspace_path=str(ws),
        repo_candidates=[str(repo)],
    )
    assert "repo_map" in [i.asset for i in first.applying()]
    builds_after_start = len(calls)

    # Every subsequent warm turn injects nothing, so it builds nothing.
    for _ in range(5):
        warm = build_turn_injections(
            SessionState.SESSION_WARM, workspace_path=str(ws),
            repo_candidates=[str(repo)],
        )
        assert "repo_map" not in [i.asset for i in warm.applying()]

    assert len(calls) == builds_after_start, "map rebuilt on a warm turn"


def test_warm_turn_injects_no_static_context(repo, ws):
    """The contract: per-session context belongs to the turn that opens it."""
    warm = build_turn_injections(
        SessionState.SESSION_WARM, workspace_path=str(ws),
        repo_candidates=[str(repo)], additional_dirs=[str(repo)],
    )
    assert warm.compose() == ""
    assert len(warm) == 0


def test_both_boundary_states_get_static_context(repo, ws):
    for state in BOUNDARY_STATES:
        turn = build_turn_injections(
            state, workspace_path=str(ws), repo_candidates=[str(repo)],
        )
        assets = [i.asset for i in turn.applying()]
        assert "orientation" in assets, state
        assert "repo_map" in assets, state


def test_orientation_survives_a_failing_map(ws, monkeypatch):
    """A failing map must not take the orientation block with it."""
    import core.context.repo_map as repo_map

    monkeypatch.setattr(
        repo_map, "build_repo_map_segment",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = build_turn_injections(
        SessionState.SESSION_START, workspace_path=str(ws),
        repo_candidates=["/does/not/exist"],
    ).compose()
    assert "Your starting directory" in out
    assert "Repository map" not in out


def test_orientation_present_without_repo_map(ws):
    out = build_turn_injections(
        SessionState.SESSION_START, workspace_path=str(ws), repo_candidates=None,
    ).compose()
    assert "Your starting directory" in out
    assert "Repository map" not in out


def test_orientation_is_not_gated_behind_the_repo_map(ws):
    """Orientation is environment knowledge, not a repo-map feature."""
    turn = build_turn_injections(
        SessionState.SESSION_START, workspace_path=str(ws), repo_candidates=None,
    )
    assert "orientation" in [i.asset for i in turn.applying()]


def test_additional_dirs_render_as_their_own_block(repo, ws):
    """The two orientation blocks are separate: where you are, where you may go."""
    out = build_turn_injections(
        SessionState.SESSION_START, workspace_path=str(ws),
        additional_dirs=[str(repo)], repo_candidates=None,
    ).compose()
    assert "## Your starting directory" in out
    assert "## Additional directories available to you" in out
    assert out.index("Your starting directory") < out.index("Additional directories")


def test_skipped_injections_are_reported_not_silently_dropped(ws):
    """A filtered block is inspectable, so 'why is my map missing' is answerable."""
    from core.injection import Injection

    turn = TurnInjections(SessionState.SESSION_WARM)
    turn.add(Injection(
        source="agentgate", asset="orientation", heading="## x", body="y",
        scope=InjectionScope.SESSION_BOUNDARY,
    ))
    assert turn.applying() == []
    assert [i.asset for i in turn.skipped()] == ["orientation"]
