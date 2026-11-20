"""Tests for reasoning wiring in the local (OpenAI-compatible) runner."""

import core.commands as commands
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


# --------------------------------------------------------------------------
# Error handling: only a genuine reasoning rejection may disable reasoning.
# A transient failure (429/5xx/timeout) must leave the level ON.
# --------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            req = httpx.Request("POST", "http://localhost/v1/chat/completions")
            resp = httpx.Response(self.status_code, text=self.text, request=req)
            raise httpx.HTTPStatusError("boom", request=req, response=resp)


class _Http:
    def __init__(self, resp):
        self._resp = resp

    async def post(self, *a, **k):
        return self._resp


def _drive_call(thread_id, model, resp):
    """Run one _call_local against a fake HTTP response; return (state, raised)."""
    commands._reasoning_states.pop(thread_id, None)
    client = _client(thread_id, model=model)
    client._http = _Http(resp)
    state = commands.reasoning_state_for(thread_id)
    state.level = "high"
    import asyncio

    raised = None
    try:
        asyncio.run(client._call_local())
    except Exception as e:  # the turn fails, which is expected
        raised = e
    return state, raised


def test_transient_500_does_not_disable_reasoning():
    state, raised = _drive_call("t-500", "qwen3:14b", _Resp(500, "Internal Server Error"))
    assert raised is not None  # the turn still fails
    assert state.level == "high", "a 500 must not turn reasoning off"
    assert state.state == "failed"


def test_transient_429_does_not_disable_reasoning():
    state, _ = _drive_call("t-429", "qwen3:14b", _Resp(429, "Too Many Requests"))
    assert state.level == "high", "a 429 must not turn reasoning off"
    assert state.state == "failed"


def test_genuine_parameter_rejection_disables_reasoning():
    body = "400 invalid_request_error: unknown field reasoning_effort"
    state, _ = _drive_call("t-400", "qwen3:14b", _Resp(400, body))
    assert state.level == "off", "a real rejection should turn reasoning off"
    assert state.state == "rejected"


# --------------------------------------------------------------------------
# extra_body style must reach the wire as a top-level field on raw httpx
# (the SDK robots unwrap it; the local transport must do so itself).
# --------------------------------------------------------------------------

def test_unwrap_extra_body_flattens_nested_field():
    from runners.local import _unwrap_extra_body

    assert _unwrap_extra_body({"extra_body": {"reasoning": {"effort": "high"}}}) == {
        "reasoning": {"effort": "high"}
    }


def test_unwrap_extra_body_passes_reasoning_effort_through():
    from runners.local import _unwrap_extra_body

    assert _unwrap_extra_body({"reasoning_effort": "high"}) == {"reasoning_effort": "high"}


def test_unwrap_extra_body_noop_when_absent():
    from runners.local import _unwrap_extra_body

    assert _unwrap_extra_body({}) == {}


def test_local_payload_never_contains_literal_extra_body(monkeypatch):
    """Even if a model resolved to extra_body, the wire payload must be flat."""
    import runners.local as local_mod

    commands._reasoning_states.pop("t-eb", None)
    client = _client("t-eb")
    state = commands.reasoning_state_for("t-eb")
    state.level = "high"
    monkeypatch.setattr(
        local_mod, "build_request_kwargs",
        lambda resolution, level, model: {"extra_body": {"reasoning": {"effort": level}}},
    )
    payload = client._reasoning_payload()
    assert "extra_body" not in payload
    assert payload == {"reasoning": {"effort": "high"}}
