"""Tests for the Claude-specific reasoning mapping (runners/claude_reasoning.py)."""

import pytest

from core.reasoning.registry import ReasoningRuntimeState
from runners.claude_reasoning import (
    build_claude_options,
    extract_thinking_text,
    record_claude_outcome,
)


def test_off_disables_thinking():
    config = build_claude_options("off")
    assert config.thinking == {"type": "disabled"}
    assert config.effort is None


@pytest.mark.parametrize("level,effort", [("low", "low"), ("medium", "medium"), ("high", "high")])
def test_adaptive_maps_level_to_effort(level, effort):
    config = build_claude_options(level, adaptive=True)
    assert config.thinking == {"type": "adaptive", "display": "summarized"}
    assert config.effort == effort


def test_fixed_budget_for_older_models():
    config = build_claude_options("medium", adaptive=False)
    assert config.thinking["type"] == "enabled"
    assert config.thinking["budget_tokens"] > 0
    assert config.effort is None


def test_extract_thinking_text_ignores_empty_blocks():
    class Block:
        def __init__(self, thinking):
            self.thinking = thinking

    blocks = [Block("  first  "), Block(""), Block(None), Block("second")]
    assert extract_thinking_text(blocks) == "first\n\nsecond"


def test_extract_thinking_text_handles_no_blocks():
    assert extract_thinking_text([]) == ""


def test_outcome_verified_when_thinking_returned():
    state = ReasoningRuntimeState(level="high")
    detail = record_claude_outcome(state, "let me think about it")
    assert state.state == "verified"
    assert state.last_reasoning == "let me think about it"
    assert "verified" in detail.lower()


def test_outcome_unverified_when_no_thinking_text():
    """Absence of thinking text is NOT proof reasoning is off (display=omitted)."""
    state = ReasoningRuntimeState(level="high")
    detail = record_claude_outcome(state, "")
    assert state.state == "unverified"
    assert "not verified" in detail.lower()


def test_record_outcome_is_noop_when_off():
    state = ReasoningRuntimeState(level="off")
    assert record_claude_outcome(state, "ignored") == ""
    assert state.state == "unknown"
