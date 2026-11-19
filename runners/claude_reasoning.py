"""Claude-specific reasoning mapping.

Kept separate from the serverless (OpenAI-compatible) reasoning support because
the Claude Agent SDK exposes a different mechanism:

  * `thinking={"type": "adaptive", "display": "summarized"}` with `effort=`
    (Opus 4.6+ / 4.7+), or `{"type": "enabled", "budget_tokens": N}` on older
    models that need a fixed budget.
  * Thinking arrives as `ThinkingBlock(thinking, signature)` content blocks.

Important verification caveat: on Opus 4.7+ the thinking text defaults to
`display="omitted"` (signature-only). That means **absence of thinking text does
not prove reasoning is off** — so for Claude we ask for `display="summarized"`
when we want to verify, and treat "no text" as `unverified`, never `rejected`.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.reasoning.registry import ReasoningLevel, ReasoningRuntimeState

# Claude `effort` levels map 1:1 onto our levels. `xhigh`/`max` exist upstream
# but are model-specific, so we stay within low/medium/high.
CLAUDE_EFFORT: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
}

# Fixed budgets for older models that do not support adaptive thinking.
CLAUDE_BUDGET_TOKENS: dict[str, int] = {
    "low": 2048,
    "medium": 8192,
    "high": 16384,
}


@dataclass(frozen=True)
class ClaudeReasoningConfig:
    """Ready-to-merge ClaudeAgentOptions fields."""

    thinking: dict | None
    effort: str | None


def build_claude_options(level: ReasoningLevel, *, adaptive: bool = True) -> ClaudeReasoningConfig:
    """Translate a reasoning level into ClaudeAgentOptions values.

    `adaptive=True` prefers the modern adaptive+effort path; otherwise a fixed
    token budget is used (older models).
    """
    if level == "off":
        return ClaudeReasoningConfig(thinking={"type": "disabled"}, effort=None)

    if adaptive:
        # "summarized" so we can verify reasoning actually happened.
        return ClaudeReasoningConfig(
            thinking={"type": "adaptive", "display": "summarized"},
            effort=CLAUDE_EFFORT.get(level, "medium"),
        )

    return ClaudeReasoningConfig(
        thinking={"type": "enabled", "budget_tokens": CLAUDE_BUDGET_TOKENS.get(level, 8192)},
        effort=None,
    )


def extract_thinking_text(content_blocks: list) -> str:
    """Concatenate non-empty ThinkingBlock text from a message's content list."""
    parts: list[str] = []
    for block in content_blocks or []:
        # Duck-typed: the SDK exposes ThinkingBlock(thinking, signature).
        text = getattr(block, "thinking", None)
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


def record_claude_outcome(state: ReasoningRuntimeState, thinking_text: str) -> str:
    """Update runtime state after a Claude turn and return the human detail.

    No reasoning text -> `unverified` (never `rejected`): with `display`
    omitted upstream it may still be working.
    """
    if state.level == "off":
        state.state = "unknown"
        state.detail = ""
        return ""
    if thinking_text:
        state.state = "verified"
        state.last_reasoning = thinking_text
        state.detail = "✓ Reasoning verified — Claude returned thinking content."
        return state.detail
    state.state = "unverified"
    state.detail = (
        "⚠️ Reasoning mode not verified — Claude did not return thinking text. "
        "It may be running with display=omitted."
    )
    return state.detail
