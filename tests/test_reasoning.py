"""Tests for the serverless reasoning support (core/reasoning)."""

import pytest

from core.reasoning import (
    NOT_VERIFIED_WARNING,
    build_request_kwargs,
    classify_outcome,
    extract_reasoning_content,
    lookup,
    resolve,
)
from core.reasoning.registry import ReasoningRuntimeState


# --------------------------------------------------------------------------
# Registry / resolution
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "provider,model,expected_style",
    [
        ("deepinfra", "deepseek-ai/DeepSeek-R1", "reasoning_effort"),
        ("deepinfra", "openai/gpt-oss-120b", "reasoning_effort"),
        ("deepinfra", "some/-thinking", "model_variant"),
        ("fireworks", "accounts/fireworks/models/deepseek-r1", "reasoning_effort"),
        ("fireworks", "kimi-k2-thinking", "reasoning_effort"),
        ("local", "qwen3:14b", "reasoning_effort"),
    ],
)
def test_registry_resolves_known_models(provider, model, expected_style):
    resolution = resolve(provider, model)
    assert resolution.in_registry is True
    assert resolution.style == expected_style


def test_unknown_model_is_reported_as_unknown_not_supported():
    resolution = resolve("deepinfra", "brand/new-model")
    assert resolution.in_registry is False
    assert resolution.style == "unknown"
    # Unknown is still *tryable* — the user may opt in.
    assert resolution.supported is True


def test_lookup_returns_none_for_unregistered_provider():
    assert lookup("some-other-provider", "any-model") is None


# --------------------------------------------------------------------------
# Request kwargs
# --------------------------------------------------------------------------

def test_reasoning_off_sends_nothing():
    resolution = resolve("deepinfra", "deepseek-ai/DeepSeek-R1")
    assert build_request_kwargs(resolution, "off", "x") == {}


def test_reasoning_effort_style_sends_effort():
    resolution = resolve("deepinfra", "deepseek-ai/DeepSeek-R1")
    assert build_request_kwargs(resolution, "high", "x") == {"reasoning_effort": "high"}


def test_model_variant_sends_no_parameter():
    resolution = resolve("deepinfra", "some-model-thinking")
    assert resolution.style == "model_variant"
    assert build_request_kwargs(resolution, "high", "x") == {}


def test_unknown_style_sends_no_parameter():
    resolution = resolve("deepinfra", "brand/new-model")
    assert build_request_kwargs(resolution, "high", "x") == {}


# --------------------------------------------------------------------------
# Response parsing / classification
# --------------------------------------------------------------------------

def test_extract_reasoning_content_from_string_field():
    assert extract_reasoning_content({"reasoning_content": "  trace  "}) == "trace"


def test_extract_reasoning_content_from_alternate_fields():
    assert extract_reasoning_content({"reasoning": "r"}) == "r"
    assert extract_reasoning_content({"thinking": "t"}) == "t"
    assert extract_reasoning_content({"reasoning": {"content": "nested"}}) == "nested"


def test_extract_reasoning_content_empty_when_absent():
    assert extract_reasoning_content({"content": "just the answer"}) == ""
    assert extract_reasoning_content({}) == ""


def test_outcome_verified_when_reasoning_returned():
    state, detail = classify_outcome(
        request_failed=False, error_message="", reasoning_text="thinking..."
    )
    assert state == "verified"
    assert "verified" in detail.lower()


def test_outcome_unverified_when_accepted_but_no_reasoning():
    state, detail = classify_outcome(
        request_failed=False, error_message="", reasoning_text=""
    )
    assert state == "unverified"
    assert detail.startswith(NOT_VERIFIED_WARNING)


def test_outcome_rejected_when_provider_rejects_parameter():
    state, detail = classify_outcome(
        request_failed=True,
        error_message="400 invalid_request_error: unknown field reasoning_effort",
        reasoning_text="",
    )
    assert state == "rejected"
    assert "rejected" in detail.lower()


def test_outcome_failed_for_generic_failure_not_rejected():
    """A 5xx/timeout/429 is a *failed* request, not a reasoning rejection.

    Collapsing these to "rejected" made the runners disable reasoning on any
    transient error. Only a real parameter rejection may turn reasoning off.
    """
    state, detail = classify_outcome(
        request_failed=True, error_message="500 boom", reasoning_text=""
    )
    assert state == "failed"
    assert "failed" in detail.lower()


@pytest.mark.parametrize(
    "msg",
    [
        "429 Too Many Requests",
        "ReadTimeout: the request timed out",
        "500 Internal Server Error",
        "502 Bad Gateway",
        "Connection reset by peer",
    ],
)
def test_transient_failures_are_not_classified_as_rejection(msg):
    state, _ = classify_outcome(request_failed=True, error_message=msg, reasoning_text="")
    assert state == "failed", f"{msg!r} must not disable reasoning"


# --------------------------------------------------------------------------
# Runtime state
# --------------------------------------------------------------------------

def test_runtime_state_resets_when_model_changes():
    state = ReasoningRuntimeState(level="high")
    state.state = "verified"
    state.last_reasoning = "old"
    state.model_id = "model-a"

    state.reset_for_model("model-a")
    assert state.state == "verified"  # same model -> untouched

    state.reset_for_model("model-b")
    assert state.state == "unknown"
    assert state.last_reasoning == ""
    assert state.model_id == "model-b"


# --------------------------------------------------------------------------
# Runner model accessor — the id /reasoning resolves against
# --------------------------------------------------------------------------
#
# Bots used to derive the reasoning model with `getattr(runner, "model", "")`.
# LocalRunner happens to expose `.model`, but DeepInfra and Fireworks keep the
# id on `._model.model_id`, so that getattr returned "" -- and /reasoning then
# reported "model not in the registry" for a model it could never match.
# `current_model_id` gives every runner one accessor that returns the real id.

def test_deepinfra_reports_active_model_id():
    from runners.deepinfra import DeepInfraRunner

    runner = DeepInfraRunner(model="deepseek-ai/DeepSeek-V4.1-Flash-1017")
    assert runner.current_model_id == "deepseek-ai/DeepSeek-V4.1-Flash-1017"


def test_deepinfra_accessor_tracks_set_model():
    import asyncio

    from runners.deepinfra import DeepInfraRunner

    runner = DeepInfraRunner(model="deepseek-ai/DeepSeek-V4.1-Flash-1017")
    asyncio.run(runner.set_model("openai/gpt-oss-120b"))
    assert runner.current_model_id == "openai/gpt-oss-120b"


def test_fireworks_reports_active_model_id():
    from runners.fireworks import FireworksRunner

    runner = FireworksRunner(model="gpt-oss-20b")
    # resolve_model expands a bare name to the accounts/... suffix; whatever it
    # resolves to, the accessor and the resolution agree.
    assert runner.current_model_id == runner._model.model_id
    assert runner.current_model_id  # non-empty


def test_local_reports_active_model_id():
    from runners.local import LocalRunner

    runner = LocalRunner(name="ollama", base_url="http://x", model="qwen3:14b")
    assert runner.current_model_id == "qwen3:14b"


def test_claude_reports_empty_model_id():
    from runners.claude import ClaudeRunner

    assert ClaudeRunner().current_model_id == ""


def test_accessor_beats_getattr_for_deepinfra():
    """The bug: getattr(runner, 'model', '') was '' for DeepInfra."""
    from runners.deepinfra import DeepInfraRunner

    runner = DeepInfraRunner(model="deepseek-ai/DeepSeek-V4.1-Flash-1017")
    assert getattr(runner, "model", "") == ""  # the OLD derivation -> empty
    assert runner.current_model_id  # the NEW accessor -> the real id


# --------------------------------------------------------------------------
# DeepSeek V3/V4 in the registry (previously fell through to "unknown")
# --------------------------------------------------------------------------
#
# The generated fragment records the catalog's own tags, and the two DeepSeek
# families differ: V3.1 carries `reasoning_effort` (depth is tunable), while
# V4.x carries only `reasoning` (capable, no parameter). Pinned here so a
# refresh cannot silently collapse the distinction.

@pytest.mark.parametrize(
    "model,expected",
    [
        ("deepseek-ai/DeepSeek-V4.1-Flash-1017", "model_variant"),
        ("deepseek-ai/DeepSeek-V4-Flash-0731", "model_variant"),
        ("deepseek-ai/DeepSeek-V4-Pro", "model_variant"),
        ("deepseek-ai/DeepSeek-V3.1", "reasoning_effort"),
    ],
)
def test_deepseek_v3_v4_registered_from_catalog(model, expected):
    resolution = resolve("deepinfra", model)
    assert resolution.in_registry is True
    assert resolution.style == expected
    if expected == "reasoning_effort":
        assert build_request_kwargs(resolution, "high", model) == {"reasoning_effort": "high"}
    else:
        assert build_request_kwargs(resolution, "high", model) == {}


def test_r1_and_gptoss_still_use_reasoning_effort_after_v4_entry():
    """Guard: the DeepSeek entries must not shadow the earlier effort-style patterns."""
    assert resolve("deepinfra", "deepseek-ai/DeepSeek-R1-0528").style == "reasoning_effort"
    assert resolve("deepinfra", "openai/gpt-oss-120b").style == "reasoning_effort"


# --------------------------------------------------------------------------
# Fireworks: chat models accept top-level reasoning_effort
# --------------------------------------------------------------------------
#
# Fireworks has no reasoning tag in /models, so coverage is by family name.
# Every Fireworks *chat* model was probe-confirmed to accept reasoning_effort
# and return reasoning_content (see docs/reasoning-registry-source-of-truth.md).
# Ids are "accounts/fireworks/models/<name>".

@pytest.mark.parametrize(
    "name",
    [
        "minimax-m3", "gpt-oss-120b", "nemotron-3-ultra-nvfp4", "kimi-k3",
        "qwen3p8-max", "glm-5p3", "glm-5p3-flash", "deepseek-v4p1-flash",
        "ember-1", "nemotron-lightning-3p5-30b-a3b", "inkling",
        "deepseek-v4p1-flash-1017", "minimax-m3p1-flash", "qwen3p8-2p4t-a95b",
    ],
)
def test_fireworks_chat_models_resolve_to_reasoning_effort(name):
    model = f"accounts/fireworks/models/{name}"
    resolution = resolve("fireworks", model)
    assert resolution.in_registry is True
    assert resolution.style == "reasoning_effort"
    # The request carries the top-level parameter Fireworks documents.
    assert build_request_kwargs(resolution, "high", model) == {"reasoning_effort": "high"}


@pytest.mark.parametrize(
    "name",
    ["qwen3-embedding-8b", "qwen3-reranker-8b"],
)
def test_fireworks_embedding_and_reranker_not_registered(name):
    """Non-chat models are left out, so we never send them reasoning_effort."""
    model = f"accounts/fireworks/models/{name}"
    assert lookup("fireworks", model) is None


def test_fireworks_routers_resolve_like_their_model():
    """Spot/router ids share the family name and resolve the same way."""
    assert resolve("fireworks", "accounts/fireworks/routers/kimi-k3-fast").style == "reasoning_effort"
    assert resolve("fireworks", "accounts/fireworks/routers/glm-5p3-spot").style == "reasoning_effort"
