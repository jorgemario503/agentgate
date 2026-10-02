"""Tests for run.py — the master runner / entry point."""
import sys
import pytest


def test_run_importable():
    import run
    assert run is not None


def test_parse_argv_slack():
    from run import parse_argv, BotSpec
    specs, list_bots = parse_argv(["--slack"])
    assert not list_bots
    assert len(specs) == 1
    assert specs[0].channel == "slack"


def test_parse_argv_telegram_defaults():
    from run import parse_argv, BotSpec
    specs, _ = parse_argv(["--telegram"])
    assert specs[0].channel == "telegram"
    assert specs[0].runner == "claude"
    assert specs[0].bot_id is None
    assert specs[0].model is None


def test_parse_argv_telegram_full():
    from run import parse_argv
    specs, _ = parse_argv(["--telegram", "fireworks", "personal", "kimi-k2"])
    s = specs[0]
    assert s.runner == "fireworks"
    assert s.bot_id == "personal"
    assert s.model == "kimi-k2"


def test_parse_argv_list_bots():
    from run import parse_argv
    specs, list_bots = parse_argv(["--list-bots"])
    assert list_bots
    assert specs == []


def test_parse_argv_invalid_runner_raises():
    from run import parse_argv
    with pytest.raises(SystemExit, match="invalid runner"):
        parse_argv(["--telegram", "notarunner"])


def test_parse_argv_unknown_flag_raises():
    from run import parse_argv
    with pytest.raises(SystemExit, match="unknown flag"):
        parse_argv(["--badbot"])


def test_parse_argv_multiple_bots():
    from run import parse_argv
    specs, _ = parse_argv(["--slack", "--telegram", "claude", "work", "--whatsapp"])
    channels = [s.channel for s in specs]
    assert "slack" in channels
    assert "telegram" in channels
    assert "whatsapp" in channels


def test_botspec_label_with_id():
    from run import BotSpec
    s = BotSpec(channel="telegram", bot_id="personal")
    assert s.label() == "telegram/personal"


def test_botspec_label_without_id():
    from run import BotSpec
    s = BotSpec(channel="slack")
    assert s.label() == "slack"


def test_botspec_script_argv_telegram(monkeypatch):
    from run import BotSpec
    s = BotSpec(channel="telegram", runner="fireworks", bot_id="personal", model="gpt-4o")
    argv = s.script_argv()
    assert "-m" in argv
    assert "bots.telegram" in argv
    assert "--fireworks" in argv
    assert "--bot" in argv
    assert "personal" in argv
    assert "--model" in argv
    assert "gpt-4o" in argv


def test_botspec_script_argv_slack():
    from run import BotSpec
    s = BotSpec(channel="slack")
    argv = s.script_argv()
    assert "bots.slack" in argv


def test_botspec_script_argv_whatsapp():
    from run import BotSpec
    s = BotSpec(channel="whatsapp")
    argv = s.script_argv()
    assert "bots.whatsapp" in argv
