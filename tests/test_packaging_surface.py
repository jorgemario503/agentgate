"""The shipped package must contain what the contract promises.

Why this exists
---------------
`docs/runner-surface-contract.md` tells agentorchestrator what it may import. Packaging decides
whether those imports actually resolve in a clean install -- and a `packages = [...]` list is easy
to break by omission: forget one entry and the orchestrator fails at import with
`ModuleNotFoundError` long after the change.

These tests check the declaration, not the built wheel (building is slow and needs network for
deps). The end-to-end wheel check is `scripts/` territory; this is the fast guard.
"""
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text())


def _packages() -> set[str]:
    return set(_pyproject()["tool"]["setuptools"]["packages"])


#: Subpackages the runners import. Shipping runners/ without these is a broken wheel.
REQUIRED_WITH_RUNNERS = {
    "core.context",
    "core.reasoning",
    "core.skills",
    "core.tools",
}


def test_runners_are_shipped():
    """DEC-1102: the orchestrator imports the runners rather than copying them."""
    assert "runners" in _packages()


def test_core_subpackages_used_by_runners_are_shipped():
    packages = _packages()
    missing = REQUIRED_WITH_RUNNERS - packages
    assert not missing, (
        f"runners import these but the wheel omits them: {sorted(missing)}"
    )


def test_handoff_contract_still_shipped():
    """The original published surface must not regress while adding runners."""
    assert "core.handoff" in _packages()


def test_bots_are_not_shipped():
    """AgentGate's bot stack must not become a dependency of a second product."""
    assert not any(p.startswith("bots") for p in _packages())


def test_openai_is_a_base_dependency():
    """deepinfra and fireworks import it at module level, so it is hard-required."""
    deps = _pyproject()["project"]["dependencies"]
    assert any(d.startswith("openai") for d in deps), deps


def test_claude_sdk_is_an_extra_not_a_base_dependency():
    """runners/claude.py fails at import without it, but the orchestrator never imports it."""
    project = _pyproject()["project"]
    assert not any("claude" in d.lower() for d in project["dependencies"]), (
        "claude-agent-sdk must stay optional; it is only imported by runners/claude.py"
    )
    full = project["optional-dependencies"]["full"]
    assert any("claude" in d.lower() for d in full)


def test_agentdatabase_is_an_extra():
    """Imported lazily inside core/agentdb; the runners work without it."""
    project = _pyproject()["project"]
    assert not any("agentdatabase" in d.lower() for d in project["dependencies"])
    full = project["optional-dependencies"]["full"]
    assert any("agentdatabase" in d.lower() for d in full)
