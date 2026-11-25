"""Resume prompt assembly — spec §5.2/§5.3, tests R1-R8 and D1-D8.

The resume block tells a non-Claude model that it is continuing an existing
conversation: where the history lives, what shape it is in, and what the last
two turns were. It fires regardless of AgentDB state.

Every optional section degrades independently. A missing history file, an
unavailable AgentDB, or an unset summary model must each remove only their own
section — never the whole prompt.
"""

import json

import pytest

from core.prompts import (
    build_agentdb_block,
    build_base_prompt,
    build_resume_block,
    build_system_prompt,
)
from core.sessions import SessionState


HARD_RULE_MARKER = "never violate these under any circumstances"


def write_history(tmp_path, turns, thread_id="t1", system="OLD SYSTEM"):
    """Persist a conversation in the runner's on-disk shape.

    `turns` is a list of (user, assistant) pairs; assistant may be None to
    model a turn that ended on a tool result.
    """
    messages = [{"role": "system", "content": system}]
    for user, assistant in turns:
        messages.append({"role": "user", "content": user})
        if assistant is not None:
            messages.append({"role": "assistant", "content": assistant})
    path = tmp_path / f"{thread_id}.json"
    path.write_text(json.dumps(messages, indent=2))
    return path


# -- base prompt ------------------------------------------------------------


def test_base_prompt_keeps_hard_rules():
    prompt = build_base_prompt()
    assert HARD_RULE_MARKER in prompt
    assert "coding assistant" in prompt


# -- R1-R8: resume block content -------------------------------------------


def test_r1_history_path_present(tmp_path):
    path = write_history(tmp_path, [("q1", "a1")])
    block = build_resume_block(history_path=str(path), workspace_path="/ws/x")
    assert str(path) in block


def test_r2_workspace_path_present(tmp_path):
    path = write_history(tmp_path, [("q1", "a1")])
    block = build_resume_block(history_path=str(path), workspace_path="/ws/project")
    assert "/ws/project" in block


def test_r3_last_two_turns_rendered(tmp_path):
    path = write_history(
        tmp_path,
        [("turn one q", "turn one a"), ("turn two q", "turn two a"), ("turn three q", "turn three a")],
    )
    block = build_resume_block(history_path=str(path), workspace_path="/ws")
    assert "turn three q" in block and "turn three a" in block
    assert "turn two q" in block and "turn two a" in block
    assert "turn one q" not in block


def test_r7_tool_role_documented(tmp_path):
    """The block must say the history contains tool entries."""
    path = write_history(tmp_path, [("q1", "a1")])
    block = build_resume_block(history_path=str(path), workspace_path="/ws")
    assert "tool" in block
    assert "role" in block


def test_r8_tool_output_not_inlined(tmp_path):
    """Tool bodies are not replayed; only where to find them is stated."""
    tool_output = "HUGE-TOOL-OUTPUT-" + "x" * 5000
    messages = [
        {"role": "system", "content": "OLD"},
        {"role": "user", "content": "run it"},
        {"role": "tool", "content": tool_output},
        {"role": "assistant", "content": "done"},
    ]
    path = tmp_path / "t1.json"
    path.write_text(json.dumps(messages))
    block = build_resume_block(history_path=str(path), workspace_path="/ws")
    assert "HUGE-TOOL-OUTPUT" not in block


def test_r6_truncates_long_turns(tmp_path):
    path = write_history(tmp_path, [("q" * 50000, "a" * 50000)])
    block = build_resume_block(history_path=str(path), workspace_path="/ws", max_chars=2000)
    assert len(block) <= 4000  # bounded, with room for boilerplate


# -- R4/R5: composition -----------------------------------------------------


def test_r4_resume_appends_and_keeps_hard_rules(tmp_path):
    path = write_history(tmp_path, [("q1", "a1")])
    prompt = build_system_prompt(
        state=SessionState.SESSION_RESUME, history_path=str(path), workspace_path="/ws"
    )
    assert HARD_RULE_MARKER in prompt
    assert "coding assistant" in prompt
    assert str(path) in prompt


def test_r5_fires_with_agentdb_disabled(tmp_path):
    path = write_history(tmp_path, [("q1", "a1")])
    prompt = build_system_prompt(
        state=SessionState.SESSION_RESUME,
        history_path=str(path),
        workspace_path="/ws",
        agentdb_middleware=None,
    )
    assert str(path) in prompt


def test_new_session_has_no_resume_block(tmp_path):
    path = write_history(tmp_path, [("q1", "a1")])
    prompt = build_system_prompt(
        state=SessionState.SESSION_START, history_path=str(path), workspace_path="/ws"
    )
    assert str(path) not in prompt
    assert HARD_RULE_MARKER in prompt


def test_warm_session_has_no_resume_block(tmp_path):
    path = write_history(tmp_path, [("q1", "a1")])
    prompt = build_system_prompt(
        state=SessionState.SESSION_WARM, history_path=str(path), workspace_path="/ws"
    )
    assert str(path) not in prompt


# -- D1-D8: independent degradation ----------------------------------------


def test_d1_missing_history_file():
    block = build_resume_block(history_path="/nonexistent/t1.json", workspace_path="/ws")
    assert "/ws" in block
    assert "/nonexistent/t1.json" in block  # path still named so the model can look


def test_missing_history_reports_no_prior_history_not_a_read_error():
    """A brand-new session has no file yet; that is not a read failure.

    Before this was distinguished, a missing/empty history rendered "could not
    be read; read it directly", which sent the model chasing a file with
    nothing in it. The first turn of every session hits this path.
    """
    block = build_resume_block(history_path="/nonexistent/t1.json", workspace_path="/ws")
    assert "no earlier messages" in block.lower()
    assert "could not be read" not in block.lower()


def test_corrupt_history_reports_read_error(tmp_path):
    """A file that exists but cannot be parsed is a genuine read error."""
    path = tmp_path / "t1.json"
    path.write_text("{not json")
    block = build_resume_block(history_path=str(path), workspace_path="/ws")
    assert "could not be read" in block.lower()
    assert "no earlier messages" not in block.lower()


def test_empty_history_file_reports_no_prior_history(tmp_path):
    """An empty file is an unwritten history, not a corrupt one."""
    path = tmp_path / "t1.json"
    path.write_text("[]")
    block = build_resume_block(history_path=str(path), workspace_path="/ws")
    assert "no earlier messages" in block.lower()


def test_whitespace_only_history_reports_no_prior_history(tmp_path):
    """A zero-length file from a created-but-unsaved session is not an error."""
    path = tmp_path / "t1.json"
    path.write_text("   \n  ")
    block = build_resume_block(history_path=str(path), workspace_path="/ws")
    assert "no earlier messages" in block.lower()
    assert "could not be read" not in block.lower()


def test_system_only_history_reports_no_prior_history(tmp_path):
    """A file holding only the system message has no turns to resume."""
    path = tmp_path / "t1.json"
    path.write_text(json.dumps([{"role": "system", "content": "hi"}]))
    block = build_resume_block(history_path=str(path), workspace_path="/ws")
    assert "no earlier messages" in block.lower()


def test_d2_corrupt_history_file(tmp_path):
    path = tmp_path / "t1.json"
    path.write_text("{not json")
    block = build_resume_block(history_path=str(path), workspace_path="/ws")
    assert "/ws" in block


def test_d3_empty_history_file(tmp_path):
    path = tmp_path / "t1.json"
    path.write_text("[]")
    block = build_resume_block(history_path=str(path), workspace_path="/ws")
    assert "/ws" in block


def test_d4_agentdb_middleware_none(tmp_path):
    block = build_agentdb_block(None, session_id="t1", workspace_path="/ws")
    assert block == ""


def test_d5_agentdb_prepare_turn_raises():
    class Boom:
        def prepare_turn(self, **kwargs):
            raise RuntimeError("agentdb exploded")

    assert build_agentdb_block(Boom(), session_id="t1", workspace_path="/ws") == ""


def test_d6_agentdb_empty_memory():
    class Empty:
        def prepare_turn(self, **kwargs):
            return ([], None)

    assert build_agentdb_block(Empty(), session_id="t1", workspace_path="/ws") == ""


def test_d8_everything_missing_still_yields_base_plus_resume():
    prompt = build_system_prompt(
        state=SessionState.SESSION_RESUME,
        history_path="/nonexistent/t1.json",
        workspace_path="/ws",
        agentdb_middleware=None,
    )
    assert HARD_RULE_MARKER in prompt
    assert "/nonexistent/t1.json" in prompt
    assert prompt.strip()


def test_agentdb_block_present_when_available():
    class Ok:
        def prepare_turn(self, **kwargs):
            return ([{"role": "system", "content": "remembered: user prefers tabs"}], None)

    block = build_agentdb_block(Ok(), session_id="t1", workspace_path="/ws")
    assert "user prefers tabs" in block


def test_sections_ordered_stable_to_volatile(tmp_path):
    """Prefix-cache rule: base rules precede the resume block."""
    path = write_history(tmp_path, [("q1", "a1")])
    prompt = build_system_prompt(
        state=SessionState.SESSION_RESUME, history_path=str(path), workspace_path="/ws"
    )
    assert prompt.index(HARD_RULE_MARKER) < prompt.index(str(path))


# -- wiring: the runners build the prompt themselves -----------------------


@pytest.mark.parametrize(
    "runner_module,runner_kind",
    [
        pytest.param("local", "local", id="local"),
        pytest.param("fireworks", "fireworks", id="fireworks"),
        pytest.param("deepinfra", "deepinfra", id="deepinfra"),
    ],
)
def test_cold_resume_injects_resume_block(runner_module, runner_kind, tmp_path, monkeypatch):
    """A runner whose client is gone must build a prompt naming the history."""
    import asyncio

    from runners.base import PlatformConfig

    mod = __import__(f"runners.{runner_module}", fromlist=["x"])
    thread = "t9"

    class Store:
        base = str(tmp_path)

    store = Store()
    # A registry row plus no live client is exactly SESSION_RESUME.
    meta = {"session_id": thread, "workspace_path": "/ws"}

    monkeypatch.setenv("DEEPINFRA_API_KEY", "k")
    monkeypatch.setenv("FIREWORKS_API_KEY", "k")
    if runner_kind == "fireworks":
        runner = mod.FireworksRunner(model="gpt-oss-20b")
    elif runner_kind == "deepinfra":
        import core.config as cfg
        monkeypatch.setattr(cfg, "AGENTDB_ENABLED", False)
        runner = mod.DeepInfraRunner(model="test-model")
    else:
        runner = mod.LocalRunner(
            name="ollama", base_url="http://localhost:11434/v1", model="test-model"
        )

    # Seed history in the runner's own directory.
    history_dir = tmp_path / f"{runner.name}_history"
    history_dir.mkdir(parents=True, exist_ok=True)
    (history_dir / f"{thread}.json").write_text(
        json.dumps(
            [
                {"role": "system", "content": "STALE"},
                {"role": "user", "content": "the earlier question"},
                {"role": "assistant", "content": "the earlier answer"},
            ]
        )
    )

    platform = PlatformConfig(session_store=store, agent_id="a")
    monkeypatch.setattr(
        type(platform.session_store), "get_session_meta", lambda self, tid: meta, raising=False
    )

    if runner_kind == "local":
        client, _state = asyncio.run(
            runner._get_or_create_client(thread, "/ws", None, platform)
        )
    else:
        client, _state = asyncio.run(
            runner._get_or_create_client(thread, "/ws", thread, None, platform)
        )

    system = client.messages[0]["content"]
    assert "Resuming an existing session" in system
    assert str(history_dir / f"{thread}.json") in system
    assert "the earlier answer" in system
    assert HARD_RULE_MARKER in system
