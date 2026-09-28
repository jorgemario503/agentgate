"""Tests for permissions.yaml — structural checks for the AgentDB permissions file."""
import yaml
import pytest
from pathlib import Path

PERMISSIONS_PATH = Path(__file__).parent.parent / "permissions.yaml"


@pytest.fixture(scope="module")
def doc():
    return yaml.safe_load(PERMISSIONS_PATH.read_text())


def test_file_exists():
    assert PERMISSIONS_PATH.exists(), "permissions.yaml must exist at the repo root"


def test_default_policy_is_deny(doc):
    assert doc.get("default_policy") == "deny"


def test_all_platform_agents_present(doc):
    agents = doc.get("agents", {})
    for agent_id in ("slack_assistant", "telegram_assistant", "whatsapp_assistant"):
        assert agent_id in agents, f"agents.{agent_id} is missing from permissions.yaml"


def test_all_agents_have_required_keys(doc):
    required = {"can_write", "cannot_write", "can_read"}
    for agent_id, scope in doc.get("agents", {}).items():
        missing = required - set(scope.keys())
        assert not missing, f"agents.{agent_id} is missing keys: {missing}"


def test_telegram_and_slack_have_same_scopes(doc):
    slack = doc["agents"]["slack_assistant"]
    telegram = doc["agents"]["telegram_assistant"]
    assert slack["can_write"] == telegram["can_write"]
    assert slack["cannot_write"] == telegram["cannot_write"]
    assert slack["can_read"] == telegram["can_read"]


def test_all_agents_can_write_proposals(doc):
    for agent_id, scope in doc["agents"].items():
        assert "proposal/" in scope["can_write"], (
            f"agents.{agent_id} needs proposal/ write access for AgentDB skill proposals"
        )


def test_human_zone_prefixes_defined(doc):
    prefixes = doc.get("human_zone_prefixes", [])
    assert len(prefixes) > 0, "human_zone_prefixes should list at least one protected path"


def test_no_internal_bot_names_present(doc):
    """Ensure the internal NewtonX bot names were not carried over."""
    internal_names = {"surfer", "komper", "maven"}
    agent_ids = set(doc.get("agents", {}).keys())
    for name in internal_names:
        contaminated = {a for a in agent_ids if name in a}
        assert not contaminated, (
            f"Internal bot name '{name}' found in agents: {contaminated}. "
            "This file is public — replace with generic examples."
        )
