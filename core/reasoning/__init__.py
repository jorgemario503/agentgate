"""Reasoning-mode support, shared by serverless (OpenAI-compatible) providers.

Claude keeps its own implementation in `runners/claude_reasoning.py` because the
SDK exposes a different mechanism (adaptive thinking + effort).
"""
from core.reasoning.registry import (
    ModelReasoning,
    ReasoningLevel,
    ReasoningRuntimeState,
    ReasoningState,
    ReasoningStyle,
    REASONING_LEVELS,
    lookup,
)
from core.reasoning.resolve import (
    NOT_VERIFIED_WARNING,
    Resolution,
    build_request_kwargs,
    classify_outcome,
    extract_reasoning_content,
    looks_like_rejection,
    resolve,
)

__all__ = [
    "ModelReasoning",
    "ReasoningLevel",
    "ReasoningRuntimeState",
    "ReasoningState",
    "ReasoningStyle",
    "REASONING_LEVELS",
    "lookup",
    "NOT_VERIFIED_WARNING",
    "Resolution",
    "build_request_kwargs",
    "classify_outcome",
    "extract_reasoning_content",
    "looks_like_rejection",
    "resolve",
]
