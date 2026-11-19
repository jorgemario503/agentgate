"""System-message replacement on resume — spec §5.4, tests P1-P4.

`connect()` reloads a saved message list that always starts with the system
message from the *previous* process. If the client only inserted its new
`system_prompt` when none was present, the freshly assembled prompt -- and with
it the resume block and AgentDB memory -- was silently discarded on every cold
resume.

The system prompt is authoritative: slot 0 is replaced, never skipped. P1-P4
are parametrised over the three stateless runners because each carries its own
copy of this class.
"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from runners import deepinfra, fireworks, local

RUNNERS = [
    pytest.param(local, "local", id="local"),
    pytest.param(fireworks, "fireworks", id="fireworks"),
    pytest.param(deepinfra, "deepinfra", id="deepinfra"),
]


@pytest.fixture
def history(tmp_path):
    """A saved conversation whose system message is from a previous process."""
    directory = tmp_path / "history"
    directory.mkdir()
    messages = [
        {"role": "system", "content": "STALE SYSTEM FROM PREVIOUS PROCESS"},
        {"role": "user", "content": "what were we doing?"},
        {"role": "assistant", "content": "reviewing the spec"},
    ]
    (directory / "t1.json").write_text(json.dumps(messages))
    return directory, messages


def make_client(module, name, history_dir, system_prompt, monkeypatch=None):
    """Build a client for `module`.

    `fireworks-ai` is an optional dependency and the fireworks client refuses
    to construct without it, so its SDK symbol is patched here. Every existing
    runner test follows the same convention rather than requiring the package.
    """
    store = module.JsonFileSessionStore(directory=str(history_dir))
    common = dict(
        name=name,
        system_prompt=system_prompt,
        session_id="t1",
        session_store=store,
    )
    if module is local:
        return local.LocalAgentClient(base_url="http://x", model="m", **common)
    if module is fireworks:
        if monkeypatch is not None:
            monkeypatch.setattr(fireworks, "_FIREWORKS_AVAILABLE", True)
            monkeypatch.setattr(fireworks, "AsyncFireworks", MagicMock(), raising=False)
        common.pop("name", None)
        return fireworks.FireworksAgentClient(model="m", api_key="k", **common)
    # DeepInfraAgentClient takes no `name` argument, unlike the other two.
    common.pop("name", None)
    return deepinfra.DeepInfraAgentClient(model="m", api_key="k", **common)


@pytest.mark.parametrize("module,name", RUNNERS)
def test_p1_reload_replaces_system_prompt(module, name, history, monkeypatch):
    """P1: the freshly built prompt wins over the stale saved one."""
    history_dir, saved = history
    client = make_client(module, name, history_dir, "FRESH PROMPT", monkeypatch)
    asyncio.run(client.connect())

    assert client.messages[0]["content"] == "FRESH PROMPT"
    assert "STALE" not in client.messages[0]["content"]
    # History is still replayed: only the system slot changes.
    assert len(client.messages) == len(saved)


@pytest.mark.parametrize("module,name", RUNNERS)
def test_p2_exactly_one_system_message(module, name, history, monkeypatch):
    """P2: no duplicated system message after replacement."""
    history_dir, _ = history
    client = make_client(module, name, history_dir, "FRESH PROMPT", monkeypatch)
    asyncio.run(client.connect())

    roles = [m.get("role") for m in client.messages]
    assert roles.count("system") == 1
    assert roles[0] == "system"


@pytest.mark.parametrize("module,name", RUNNERS)
def test_p3_empty_prompt_preserves_existing(module, name, history, monkeypatch):
    """P3: an empty prompt must not wipe the saved system message."""
    history_dir, _ = history
    client = make_client(module, name, history_dir, "", monkeypatch)
    asyncio.run(client.connect())

    assert client.messages[0]["content"] == "STALE SYSTEM FROM PREVIOUS PROCESS"


def test_resume_block_survives_connect(tmp_path):
    """End to end: the resume block reaches the messages sent to the model."""
    from core.prompts import build_system_prompt
    from core.sessions import SessionState

    directory = tmp_path / "history"
    directory.mkdir()
    path = directory / "t1.json"
    path.write_text(
        json.dumps(
            [
                {"role": "system", "content": "STALE"},
                {"role": "user", "content": "earlier question"},
                {"role": "assistant", "content": "earlier answer"},
            ]
        )
    )

    prompt = build_system_prompt(
        state=SessionState.COLD, history_path=str(path), workspace_path="/ws"
    )
    client = local.LocalAgentClient(
        name="local",
        base_url="http://x",
        model="m",
        system_prompt=prompt,
        session_id="t1",
        session_store=local.JsonFileSessionStore(directory=str(directory)),
    )
    asyncio.run(client.connect())

    system = client.messages[0]["content"]
    assert str(path) in system
    assert "earlier answer" in system
    assert "never violate these under any circumstances" in system
