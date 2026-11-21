"""Tests for permissions.yaml — structural checks for the AgentDB permissions file.

The deployment uses the "all bots registered by default" model: a top-level
``default_agent_scope`` grants memory scopes to any bot identity not listed under
``agents:``. A specific bot can be disabled with ``agents: <id>: { disabled: true }``.
"""
import yaml
import pytest
from pathlib import Path

PERMISSIONS_PATH = Path(__file__).parent.parent / "permissions.yaml"

_REQUIRED_SCOPE_KEYS = {"can_write", "cannot_write", "can_read"}


@pytest.fixture(scope="module")
def doc():
    return yaml.safe_load(PERMISSIONS_PATH.read_text())


def test_file_exists():
    assert PERMISSIONS_PATH.exists(), "permissions.yaml must exist at the repo root"


def test_default_policy_is_deny(doc):
    # Anything outside an agent's granted scope is still denied.
    assert doc.get("default_policy") == "deny"


def test_has_default_agent_scope(doc):
    # All bots are registered by default via default_agent_scope.
    scope = doc.get("default_agent_scope")
    assert isinstance(scope, dict) and scope, "default_agent_scope must be a non-empty mapping"
    assert _REQUIRED_SCOPE_KEYS <= set(scope.keys()), (
        f"default_agent_scope is missing keys: {_REQUIRED_SCOPE_KEYS - set(scope.keys())}"
    )


def test_default_scope_can_write_core_memory(doc):
    scope = doc["default_agent_scope"]
    for prefix in ("memory/working/", "memory/episodic/", "memory/compaction/"):
        assert prefix in scope["can_write"], (
            f"default_agent_scope.can_write must include {prefix}"
        )


def test_explicit_agents_are_structurally_valid(doc):
    # agents: may be empty (default model); any explicit entry must also be valid:
    # either a full scope (can_write/cannot_write/can_read) or {disabled: true}.
    for agent_id, scope in (doc.get("agents") or {}).items():
        if scope.get("disabled") is True:
            continue
        missing = _REQUIRED_SCOPE_KEYS - set(scope.keys())
        assert not missing, f"agents.{agent_id} is missing keys: {missing}"


def test_human_zone_prefixes_defined(doc):
    prefixes = doc.get("human_zone_prefixes", [])
    assert len(prefixes) > 0, "human_zone_prefixes should list at least one protected path"


def test_no_internal_bot_names_present(doc):
    """Ensure internal bot names were not carried over into enabled entries."""
    internal_names = {"surfer", "komper", "maven"}
    agent_ids = set((doc.get("agents") or {}).keys())
    for name in internal_names:
        contaminated = {a for a in agent_ids if name in a}
        assert not contaminated, (
            f"Internal bot name '{name}' found in agents: {contaminated}. "
            "This file is public — replace with generic examples."
        )
