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


def test_outcome_rejected_for_generic_failure():
    state, _ = classify_outcome(
        request_failed=True, error_message="500 boom", reasoning_text=""
    )
    assert state == "rejected"


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
