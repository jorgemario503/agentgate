"""Tests for reasoning wiring in the local (OpenAI-compatible) runner."""

import pytest

import core.commands as commands
from core.reasoning.registry import ReasoningRuntimeState
from runners.local import LocalAgentClient


def _client(thread_id: str, model: str = "qwen3:14b") -> LocalAgentClient:
    return LocalAgentClient(
        name="ollama",
        base_url="http://localhost:11434/v1",
        model=model,
        session_id=thread_id,
    )


def test_reasoning_payload_empty_when_off():
    commands._reasoning_states.pop("t-off", None)
    client = _client("t-off")
    state = commands.reasoning_state_for("t-off")
    state.level = "off"
    assert client._reasoning_payload() == {}


def test_reasoning_payload_includes_effort_for_known_model():
    commands._reasoning_states.pop("t-on", None)
    client = _client("t-on")
    state = commands.reasoning_state_for("t-on")
    state.level = "high"
    assert client._reasoning_payload() == {"reasoning_effort": "high"}


def test_reasoning_payload_empty_for_unknown_model():
    commands._reasoning_states.pop("t-unk", None)
    client = _client("t-unk", model="totally/unknown-model")
    state = commands.reasoning_state_for("t-unk")
    state.level = "high"
    # Unknown models send no parameter; the user is warned via state instead.
    assert client._reasoning_payload() == {}


def test_record_reasoning_marks_verified():
    commands._reasoning_states.pop("t-ver", None)
    client = _client("t-ver")
    state = commands.reasoning_state_for("t-ver")
    state.level = "medium"
    client._record_reasoning(
        {"choices": [{"message": {"content": "answer", "reasoning_content": "trace"}}]}
    )
    assert state.state == "verified"
    assert state.last_reasoning == "trace"


def test_record_reasoning_marks_unverified_when_absent():
    commands._reasoning_states.pop("t-unv", None)
    client = _client("t-unv")
    state = commands.reasoning_state_for("t-unv")
    state.level = "medium"
    client._record_reasoning({"choices": [{"message": {"content": "answer"}}]})
    assert state.state == "unverified"
    assert "not verified" in state.detail.lower()


def test_record_reasoning_ignored_when_off():
    commands._reasoning_states.pop("t-noop", None)
    client = _client("t-noop")
    state = commands.reasoning_state_for("t-noop")
    state.level = "off"
    client._record_reasoning({"choices": [{"message": {"reasoning_content": "x"}}]})
    assert state.state == "unknown"
