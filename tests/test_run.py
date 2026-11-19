"""Tests for run.py — the master runner / entry point."""
import sys
import pytest


def test_run_importable():
    import run
    assert run is not None


def test_parse_argv_slack():
    from run import parse_argv, BotSpec
    specs, list_bots, _ = parse_argv(["--slack"])
    assert not list_bots
    assert len(specs) == 1
    assert specs[0].channel == "slack"


def test_parse_argv_telegram_defaults():
    from run import parse_argv, BotSpec
    specs, _, _ = parse_argv(["--telegram"])
    assert specs[0].channel == "telegram"
    assert specs[0].runner == "claude"
    assert specs[0].bot_id is None
    assert specs[0].model is None


def test_parse_argv_telegram_full():
    from run import parse_argv
    specs, _, _ = parse_argv(["--telegram", "fireworks", "personal", "kimi-k2"])
    s = specs[0]
    assert s.runner == "fireworks"
    assert s.bot_id == "personal"
    assert s.model == "kimi-k2"


def test_parse_argv_list_bots():
    from run import parse_argv
    specs, list_bots, _ = parse_argv(["--list-bots"])
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
    specs, _, _ = parse_argv(["--slack", "--telegram", "claude", "work", "--whatsapp"])
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


# ---------------------------------------------------------------------------
# --set KEY=VALUE overrides
# ---------------------------------------------------------------------------

def test_parse_argv_set_collects_overrides():
    from run import parse_argv
    specs, _, overrides = parse_argv(
        ["--slack", "--set", "MAX_TOOL_ITERATIONS=60", "--set", "TEMPERATURE=0.9"]
    )
    assert len(specs) == 1
    assert overrides == {"MAX_TOOL_ITERATIONS": "60", "TEMPERATURE": "0.9"}


def test_parse_argv_set_equals_form():
    from run import parse_argv
    _, _, overrides = parse_argv(["--slack", "--set=DEEPINFRA_MODEL=foo/bar"])
    assert overrides == {"DEEPINFRA_MODEL": "foo/bar"}


def test_parse_argv_set_does_not_break_telegram_positionals():
    from run import parse_argv
    specs, _, overrides = parse_argv(
        ["--telegram", "ollama", "surfer", "qwen3:14b", "--set", "MAX_TOKENS=2048"]
    )
    assert specs[0].runner == "ollama"
    assert specs[0].bot_id == "surfer"
    assert specs[0].model == "qwen3:14b"
    assert overrides == {"MAX_TOKENS": "2048"}


def test_parse_argv_set_without_value_raises():
    from run import parse_argv
    with pytest.raises(SystemExit, match="KEY=VALUE"):
        parse_argv(["--slack", "--set"])


def test_parse_argv_set_malformed_raises():
    from run import parse_argv
    with pytest.raises(SystemExit, match="KEY=VALUE"):
        parse_argv(["--slack", "--set", "novalue"])


# ---------------------------------------------------------------------------
# --slack / --whatsapp runner + model (previously silently dropped)
# ---------------------------------------------------------------------------

def test_parse_argv_slack_runner():
    from run import parse_argv
    specs, _, _ = parse_argv(["--slack", "deepinfra"])
    assert len(specs) == 1
    assert specs[0].channel == "slack"
    assert specs[0].runner == "deepinfra"
    assert specs[0].model is None


def test_parse_argv_slack_runner_and_model():
    from run import parse_argv
    specs, _, _ = parse_argv(["--slack", "deepinfra", "deepinfra/Qwen/Qwen3-235B-A22B"])
    s = specs[0]
    assert s.runner == "deepinfra"
    assert s.model == "deepinfra/Qwen/Qwen3-235B-A22B"


def test_parse_argv_slack_defaults_to_claude():
    from run import parse_argv
    specs, _, _ = parse_argv(["--slack"])
    assert specs[0].runner == "claude"
    assert specs[0].model is None


def test_parse_argv_whatsapp_runner_and_model():
    from run import parse_argv
    specs, _, _ = parse_argv(["--whatsapp", "ollama", "qwen3:14b"])
    s = specs[0]
    assert s.channel == "whatsapp"
    assert s.runner == "ollama"
    assert s.model == "qwen3:14b"


def test_parse_argv_slack_invalid_runner_raises():
    from run import parse_argv
    with pytest.raises(SystemExit, match="invalid runner"):
        parse_argv(["--slack", "notarunner"])


def test_parse_argv_whatsapp_invalid_runner_raises():
    from run import parse_argv
    with pytest.raises(SystemExit, match="invalid runner"):
        parse_argv(["--whatsapp", "notarunner"])


def test_parse_argv_slack_rejects_third_value():
    from run import parse_argv
    with pytest.raises(SystemExit, match="at most 2 values"):
        parse_argv(["--slack", "deepinfra", "model-x", "extra"])


def test_parse_argv_slack_does_not_swallow_following_flag():
    from run import parse_argv
    specs, _, overrides = parse_argv(["--slack", "deepinfra", "--set", "TEMPERATURE=0.5"])
    assert len(specs) == 1
    assert specs[0].runner == "deepinfra"
    assert overrides == {"TEMPERATURE": "0.5"}


def test_botspec_script_argv_slack_emits_runner_and_model():
    from run import BotSpec
    argv = BotSpec(channel="slack", runner="deepinfra", model="m-1").script_argv()
    assert argv[:3] == ["-m", "bots.slack", "--deepinfra"]
    assert "--model" in argv and "m-1" in argv
    assert "--bot" not in argv


def test_botspec_script_argv_whatsapp_emits_runner_and_model():
    from run import BotSpec
    argv = BotSpec(channel="whatsapp", runner="ollama", model="qwen3:14b").script_argv()
    assert argv[:3] == ["-m", "bots.whatsapp", "--ollama"]
    assert "--model" in argv and "qwen3:14b" in argv


def test_botspec_script_argv_slack_default_runner_omits_flag():
    from run import BotSpec
    argv = BotSpec(channel="slack", runner="claude").script_argv()
    assert argv == ["-m", "bots.slack"]
