"""prompt_cache_key / session-affinity — spec §6.8.

Providers cache a prompt prefix and, on their side, cannot tell two
conversations apart unless the request says so. DeepInfra and Fireworks both
accept a session-scoped key:

  - DeepInfra: `prompt_cache_key` in the request body
  - Fireworks: `x-session-affinity` header (also accepts prompt_cache_key)

Pinning each AgentGate thread to its own key keeps one conversation's cache
lineage separate from another's on the same model and API key.

The mechanism is an allowlist keyed by provider, never by runner class, so
Claude and any future SDK backend are excluded by construction. The local
runner POSTs a raw JSON body to arbitrary servers, so it is opt-in: a strict
server may reject an unknown field, and in that case the request is retried
without it rather than failing the turn.
"""

import pytest

from core.cache_key import (
    CACHE_KEY_HEADER,
    CACHE_KEY_FIELD,
    cache_key_kwargs,
    retry_without_cache_key,
)


# -- the allowlist ---------------------------------------------------------


def test_deepinfra_uses_the_body_field():
    kwargs = cache_key_kwargs("deepinfra", "thread-1")
    assert kwargs == {"extra_body": {CACHE_KEY_FIELD: "thread-1"}}


def test_fireworks_uses_the_affinity_header():
    kwargs = cache_key_kwargs("fireworks", "thread-1")
    assert kwargs == {"extra_headers": {CACHE_KEY_HEADER: "thread-1"}}


def test_local_is_opt_in(tmp_path, monkeypatch):
    """The local runner must not send the field unless the operator opts in."""
    monkeypatch.delenv("AGENTGATE_PROMPT_CACHE_KEY", raising=False)
    assert cache_key_kwargs("ollama", "thread-1") == {}
    assert cache_key_kwargs("mlx", "thread-1") == {}

    monkeypatch.setenv("AGENTGATE_PROMPT_CACHE_KEY", "1")
    assert cache_key_kwargs("ollama", "thread-1") == {
        CACHE_KEY_FIELD: "thread-1"
    }


def test_unknown_provider_gets_nothing():
    """Unknown and Claude-backed providers are excluded by construction."""
    assert cache_key_kwargs("claude", "thread-1") == {}
    assert cache_key_kwargs("something-else", "thread-1") == {}


def test_no_thread_id_no_kwargs():
    """Without a thread id there is nothing to pin to."""
    assert cache_key_kwargs("deepinfra", "") == {}
    assert cache_key_kwargs("fireworks", None) == {}


def test_keys_differ_per_thread():
    a = cache_key_kwargs("deepinfra", "t1")["extra_body"][CACHE_KEY_FIELD]
    b = cache_key_kwargs("deepinfra", "t2")["extra_body"][CACHE_KEY_FIELD]
    assert a != b


# -- degrade on rejection --------------------------------------------------


def test_retry_helper_detects_a_key_rejection():
    class BadRequest(Exception):
        status_code = 400

        def __str__(self):
            return "unknown field 'prompt_cache_key'"

    assert retry_without_cache_key(BadRequest()) is True


def test_retry_helper_ignores_unrelated_errors():
    class ServerError(Exception):
        status_code = 500

        def __str__(self):
            return "internal server error"

    assert retry_without_cache_key(ServerError()) is False


def test_plain_value_error_is_not_a_key_rejection():
    assert retry_without_cache_key(ValueError("bad json")) is False


# -- the runners actually send it ------------------------------------------


@pytest.mark.parametrize(
    "runner_module,provider,expect_kwarg",
    [
        ("deepinfra", "deepinfra", "extra_body"),
        ("fireworks", "fireworks", "extra_headers"),
    ],
)
def test_runner_passes_the_key_to_the_api(runner_module, provider, expect_kwarg, monkeypatch):
    """The request built by _call_* must carry the key for a real thread."""
    import asyncio

    mod = __import__(f"runners.{runner_module}", fromlist=["x"])
    monkeypatch.setenv("DEEPINFRA_API_KEY", "k")
    monkeypatch.setenv("FIREWORKS_API_KEY", "k")

    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)

            class Msg:
                content = "ok"

            class Choice:
                message = Msg()

                def model_dump(self, **kw):
                    return {"choices": [{"message": {"content": "ok"}}]}

                def to_dict(self):
                    return {"choices": [{"message": {"content": "ok"}}]}

            class Completion:
                choices = [Choice()]

                def model_dump(self, **kw):
                    return {"choices": [{"message": {"content": "ok"}}]}

                def to_dict(self):
                    return {"choices": [{"message": {"content": "ok"}}]}

            return Completion()

    if runner_module == "deepinfra":
        client = mod.DeepInfraAgentClient(model="m", api_key="k", session_id="thread-7")
    else:
        client = mod.FireworksAgentClient(model="m", api_key="k", session_id="thread-7")

    class FakeClient:
        chat = type("C", (), {"completions": FakeCompletions()})()

    client._client = FakeClient()
    asyncio.run(client._call_deepinfra() if hasattr(client, "_call_deepinfra") else client._call_fireworks())

    assert expect_kwarg in captured, f"{provider} did not send the key: {captured}"
    blob = str(captured[expect_kwarg])
    assert "thread-7" in blob


def test_start_rejection_retries_without_the_key(monkeypatch):
    """A strict local server rejecting the field must not cost the turn."""
    import asyncio

    mod = __import__("runners.deepinfra", fromlist=["x"])
    monkeypatch.setenv("DEEPINFRA_API_KEY", "k")

    calls = []

    class Boom(Exception):
        status_code = 400

        def __str__(self):
            return "unknown field 'prompt_cache_key'"

    class FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise Boom()

            class Choice:
                def model_dump(self, **kw):
                    return {"choices": [{"message": {"content": "ok"}}]}

            class Completion:
                choices = [Choice()]

                def model_dump(self, **kw):
                    return {"choices": [{"message": {"content": "ok"}}]}

                def to_dict(self):
                    return {"choices": [{"message": {"content": "ok"}}]}

            return Completion()

    client = mod.DeepInfraAgentClient(model="m", api_key="k", session_id="thread-7")
    client._client = type("C", (), {"chat": type("CC", (), {"completions": FakeCompletions()})()})()

    data = asyncio.run(client._call_deepinfra())
    assert data["choices"][0]["message"]["content"] == "ok"
    assert len(calls) == 2, "expected one retry"
    assert "extra_body" in calls[0]
    assert "extra_body" not in calls[1], "retry must drop the key"
