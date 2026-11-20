"""Fireworks reasoning wiring: request shape and outcome recording.

Mocks the completion call (no network) to prove:
  - `_call_fireworks` sends a top-level `reasoning_effort` kwarg, the exact
    parameter Fireworks documents (https://fireworks.ai/docs/guides/reasoning),
  - a `reasoning_content` reply marks the thread verified,
  - a 400 "non-reasoning model" reply is classified rejected and disables
    reasoning for the thread.
"""
import asyncio

import pytest

import core.commands as commands


def _client(monkeypatch, thread_id: str, model: str = "accounts/fireworks/models/gpt-oss-120b"):
    monkeypatch.setenv("FIREWORKS_API_KEY", "k")
    from runners.fireworks import FireworksAgentClient

    return FireworksAgentClient(model=model, api_key="k", session_id=thread_id)


class _Completion:
    """Stand-in for an SDK ChatCompletion with a given message dict."""

    def __init__(self, message: dict):
        self._message = message

    def to_dict(self):
        return {"choices": [{"message": self._message}]}


def _install_fake_create(client, captured, *, message=None, raises=None):
    async def create(**kwargs):
        captured.update(kwargs)
        if raises is not None:
            raise raises
        return _Completion(message or {"role": "assistant", "content": "ok"})

    client._client.chat.completions.create = create


def test_fireworks_sends_top_level_reasoning_effort(monkeypatch):
    tid = "fw-effort"
    commands._reasoning_states.pop(tid, None)
    client = _client(monkeypatch, tid)
    client.messages = [{"role": "user", "content": "hi"}]
    state = commands.reasoning_state_for(tid)
    state.level = "high"

    captured = {}
    _install_fake_create(
        client, captured, message={"role": "assistant", "content": "ok", "reasoning_content": "t"}
    )
    asyncio.run(client._call_fireworks())

    assert captured.get("reasoning_effort") == "high"
    # Fireworks expects it at the top level, not nested under extra_body.
    assert "extra_body" not in captured or "reasoning_effort" not in (captured.get("extra_body") or {})


def test_fireworks_reasoning_content_marks_verified(monkeypatch):
    tid = "fw-verified"
    commands._reasoning_states.pop(tid, None)
    client = _client(monkeypatch, tid)
    client.messages = [{"role": "user", "content": "hi"}]
    state = commands.reasoning_state_for(tid)
    state.level = "medium"

    captured = {}
    _install_fake_create(
        client, captured,
        message={"role": "assistant", "content": "ok", "reasoning_content": "my trace"},
    )
    asyncio.run(client._call_fireworks())

    assert state.state == "verified"
    assert state.last_reasoning == "my trace"


def test_fireworks_no_reasoning_content_marks_unverified(monkeypatch):
    tid = "fw-unverified"
    commands._reasoning_states.pop(tid, None)
    client = _client(monkeypatch, tid)
    client.messages = [{"role": "user", "content": "hi"}]
    state = commands.reasoning_state_for(tid)
    state.level = "medium"

    _install_fake_create(client, {}, message={"role": "assistant", "content": "ok"})
    asyncio.run(client._call_fireworks())
    assert state.state == "unverified"


def test_fireworks_non_reasoning_rejection_disables(monkeypatch):
    """The exact 400 Fireworks returns for a non-reasoning model."""
    tid = "fw-reject"
    commands._reasoning_states.pop(tid, None)
    client = _client(monkeypatch, tid, model="accounts/fireworks/models/qwen3-embedding-8b")
    client.messages = [{"role": "user", "content": "hi"}]
    state = commands.reasoning_state_for(tid)
    state.level = "high"

    class Boom(Exception):
        def __str__(self):
            return "non-reasoning model does not support reasoning_effort"

    _install_fake_create(client, {}, raises=Boom())
    with pytest.raises(Boom):
        asyncio.run(client._call_fireworks())
    assert state.state == "rejected"
    assert state.level == "off"


def test_fireworks_transient_failure_keeps_reasoning_on(monkeypatch):
    tid = "fw-500"
    commands._reasoning_states.pop(tid, None)
    client = _client(monkeypatch, tid)
    client.messages = [{"role": "user", "content": "hi"}]
    state = commands.reasoning_state_for(tid)
    state.level = "high"

    class Boom(Exception):
        def __str__(self):
            return "500 Internal Server Error"

    _install_fake_create(client, {}, raises=Boom())
    with pytest.raises(Boom):
        asyncio.run(client._call_fireworks())
    assert state.state == "failed"
    assert state.level == "high", "a 500 must not disable reasoning"
