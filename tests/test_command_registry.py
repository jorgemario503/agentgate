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
    assert {"stop", "status", "cancel", "conflicts", "autoapprove",
            "reasoning", "help"} <= names


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
    reply = asyncio.run(commands.dispatch("autoapprove on", make_ctx(user_id="u1")))
    assert "Only admins" in reply


def test_admin_only_command_allows_admin():
    reply = asyncio.run(commands.dispatch("autoapprove on", make_ctx(user_id="admin1")))
    assert "Only admins" not in reply


def test_missing_required_args_shows_usage():
    reply = asyncio.run(commands.dispatch("autoapprove", make_ctx(user_id="admin1")))
    assert reply.startswith("Usage: autoapprove")


def test_help_lists_every_command():
    reply = asyncio.run(commands.dispatch("help", make_ctx()))
    for spec in commands.COMMANDS:
        assert spec.name in reply


# ---------------------------------------------------------------------------
# Telegram slash form
#
# bots/telegram.py dispatches RAW message text, which keeps the leading slash.
# The registry tests above all use slashless input, which is why the slash form
# silently stopped matching after 5cd8e18 (10 per-command handlers -> one
# dispatch that reads update.message.text). These pin the form Telegram sends.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("/stop", "stop"),
    ("/status", "status"),
    ("/cancel", "cancel"),
    ("/help", "help"),
    ("/autoapprove on", "autoapprove"),
    ("/autoapprove off", "autoapprove"),
    ("/idle start", "idle"),
    ("/reasoning high", "reasoning"),
    ("/stop session", "stop"),
    ("/session status", "status"),
])
def test_parse_matches_telegram_slash_form(text, expected):
    match = commands.parse_command(text)
    assert match is not None, f"{text!r} did not match any command"
    assert match[0].name == expected


def test_slash_form_extracts_args():
    spec, args = commands.parse_command("/autoapprove on")
    assert spec.name == "autoapprove"
    assert args == "on"


@pytest.mark.parametrize("text,expected", [
    ("/AUTOAPPROVE ON", "autoapprove"),
    ("/autoapprove   on", "autoapprove"),
    ("  /autoapprove on  ", "autoapprove"),
    ("/AutoApprove On", "autoapprove"),
])
def test_slash_form_is_case_and_space_insensitive(text, expected):
    match = commands.parse_command(text)
    assert match is not None
    assert match[0].name == expected


@pytest.mark.parametrize("text", ["/", "//", "//autoapprove on", "/ ", "/ help"])
def test_slash_edge_cases_do_not_match(text):
    assert commands.parse_command(text) is None


@pytest.mark.parametrize("text", [
    "help", "stop session", "reasoning high", "  REASONING   HIGH  ",
])
def test_slashless_input_unchanged_by_normalise(text):
    """Slack dispatches pre-cleaned text; _normalise must be a no-op there."""
    assert commands._normalise(text) == " ".join(text.strip().lower().split())


@pytest.mark.parametrize("text", [
    "hello there", "write me a function", "notacommand", "", "   ",
])
def test_slash_fix_does_not_turn_prose_into_commands(text):
    assert commands.parse_command(text) is None


def test_autoapprove_slash_form_toggles_the_flag():
    """End-to-end: the command Telegram actually sends must set the flag."""
    import core.approvals as approvals
    tid = "slash-form-chat"
    approvals.disable_auto_approve(tid)
    try:
        ctx = make_ctx(user_id="admin1")
        ctx.thread_id = tid
        reply = asyncio.run(commands.dispatch("/autoapprove on", ctx))
        assert "enabled" in reply.lower()
        assert approvals.is_auto_approved(tid)
        reply = asyncio.run(commands.dispatch("/autoapprove off", ctx))
        assert "disabled" in reply.lower()
        assert not approvals.is_auto_approved(tid)
    finally:
        approvals.disable_auto_approve(tid)


# ---------------------------------------------------------------------------
# Platform command forms — pin the actual contract
#
#   Telegram: slash commands, NO bot mention.   "/autoapprove on"
#   Slack:    bot mention, NO slash command.    "@bot autoapprove on"
#             (bots/slack.py strips "<@U123>" before dispatch, so what
#              reaches the registry is plain "autoapprove on")
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected,args", [
    ("/autoapprove on", "autoapprove", "on"),
    ("/idle start", "idle", "start"),
    ("/reasoning high", "reasoning", "high"),
    ("/stop", "stop", ""),
])
def test_telegram_form_is_slash_without_mention(text, expected, args):
    spec, got_args = commands.parse_command(text)
    assert spec.name == expected
    assert got_args == args


@pytest.mark.parametrize("text,expected,args", [
    ("autoapprove on", "autoapprove", "on"),
    ("idle start", "idle", "start"),
    ("reasoning high", "reasoning", "high"),
    ("stop", "stop", ""),
])
def test_slack_form_after_mention_strip_is_plain_text(text, expected, args):
    spec, got_args = commands.parse_command(text)
    assert spec.name == expected
    assert got_args == args


def test_slack_mention_is_stripped_before_dispatch():
    """The "<@U123>" mention removal that Slack performs before dispatch."""
    import re
    for raw, clean in [
        ("<@U12345> autoapprove on", "autoapprove on"),
        ("<@U999>  idle start ", "idle start"),
    ]:
        stripped = re.sub(r"<@[A-Z0-9]+>", "", raw).strip()
        assert stripped == clean
        assert commands.parse_command(stripped) is not None


@pytest.mark.parametrize("text", [
    "@mybot autoapprove on",
    "@mybot /autoapprove on",
])
def test_mention_prefixed_text_is_not_a_command(text):
    """Telegram does not take a mention; such text is an agent prompt, not a command.

    It must fall through to the agent turn rather than being parsed here.
    """
    assert commands.parse_command(text) is None
