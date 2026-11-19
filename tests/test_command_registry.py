"""Tests for the unified command registry (core/commands.py)."""

import asyncio

import pytest

import core.commands as commands
from runners.base import PlatformConfig


class FakeStore:
    base = "/tmp/agentgate-test"

    def append_audit_log(self, *args, **kwargs):
        pass


class FakeRunner:
    async def cancel(self, thread_id):
        return True

    def list_pending_conflicts(self):
        return ["conflict-1"]

    def list_pending_proposals(self, status="pending"):
        return []


@pytest.fixture(autouse=True)
def _runner():
    commands.use_runner(FakeRunner())
    yield


def make_ctx(user_id="u1", admins=frozenset({"admin1"})):
    platform = PlatformConfig(
        session_store=FakeStore(),
        agent_id="agent",
        admin_user_ids=admins,
        allowed_user_ids=frozenset({"u1", "admin1"}),
    )
    return commands.CommandContext(platform=platform, thread_id="t1", user_id=user_id)


# -- registry shape ---------------------------------------------------------

def test_registry_has_unique_canonical_names():
    names = [spec.name for spec in commands.COMMANDS]
    assert len(names) == len(set(names))


def test_aliases_do_not_collide_with_canonical_names():
    names = {spec.name for spec in commands.COMMANDS}
    for spec in commands.COMMANDS:
        for alias in spec.aliases:
            assert alias not in names or alias == spec.name


def test_expected_commands_are_registered():
    names = {spec.name for spec in commands.COMMANDS}
    assert {"stop", "status", "cancel", "conflicts", "proposals",
            "propose", "approve", "reject", "autoapprove", "reasoning",
            "help"} <= names


# -- parsing ----------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("stop", "stop"),
    ("stop session", "stop"),
    ("session status", "status"),
    ("reasoning", "reasoning"),
    ("reasoning high", "reasoning"),
    ("  REASONING   HIGH  ", "reasoning"),
])
def test_parse_matches_and_aliases(text, expected):
    match = commands.parse_command(text)
    assert match is not None
    assert match[0].name == expected


def test_parse_extracts_args():
    spec, args = commands.parse_command("reasoning high")
    assert args == "high"
    spec, args = commands.parse_command("reasoning")
    assert args == ""


@pytest.mark.parametrize("text", ["hello there", "", "   ", "notacommand"])
def test_parse_returns_none_for_non_commands(text):
    assert commands.parse_command(text) is None


def test_longest_alias_wins():
    # "stop session" must not be parsed as "stop" + args "session"
    spec, args = commands.parse_command("stop session")
    assert spec.name == "stop"
    assert args == ""


# -- dispatch ---------------------------------------------------------------

def test_dispatch_runs_command():
    reply = asyncio.run(commands.dispatch("cancel", make_ctx()))
    assert reply is not None
    assert "Interrupting" in reply


def test_dispatch_returns_none_for_plain_text():
    assert asyncio.run(commands.dispatch("write me a function", make_ctx())) is None


def test_admin_only_command_blocks_non_admin():
    reply = asyncio.run(commands.dispatch("approve p1", make_ctx(user_id="u1")))
    assert "Only admins" in reply


def test_admin_only_command_allows_admin():
    reply = asyncio.run(commands.dispatch("approve p1", make_ctx(user_id="admin1")))
    assert "Only admins" not in reply


def test_missing_required_args_shows_usage():
    reply = asyncio.run(commands.dispatch("propose", make_ctx(user_id="admin1")))
    assert reply.startswith("Usage: propose")


def test_help_lists_every_command():
    reply = asyncio.run(commands.dispatch("help", make_ctx()))
    for spec in commands.COMMANDS:
        assert spec.name in reply
