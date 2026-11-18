"""Resolve reasoning support and probe hints for OpenAI-compatible providers.

The registry is authoritative when it knows the model. When it does not, the
model is reported as *unknown* and the user may explicitly opt in to trying it
— the outcome is then labelled `unverified` unless the provider actually
returns reasoning content (`verified`) or rejects the parameter (`rejected`).
"""
from __future__ import annotations

from dataclasses import dataclass

from core.reasoning.registry import (
    ModelReasoning,
    ReasoningLevel,
    REASONING_LEVELS,
    lookup,
)

NOT_VERIFIED_WARNING = "⚠️ Reasoning mode not verified"

_REJECTION_HINTS = (
    "reasoning_effort",
    "reasoning",
    "unknown field",
    "unexpected keyword",
    "invalid_request_error",
    "unsupported parameter",
    "extra fields",
)


@dataclass(frozen=True)
class Resolution:
    """What we know about reasoning for a (provider, model) before any call."""

    provider: str
    model_id: str
    style: str
    levels: tuple[str, ...]
    in_registry: bool
    notes: str = ""

    @property
    def supported(self) -> bool:
        return self.style not in ("unsupported",)

    @property
    def known(self) -> bool:
        return self.in_registry


def resolve(provider: str, model_id: str) -> Resolution:
    entry: ModelReasoning | None = lookup(provider, model_id)
    if entry is None:
        return Resolution(
            provider=provider,
            model_id=model_id,
            style="unknown",
            levels=REASONING_LEVELS[1:],  # low/medium/high
            in_registry=False,
            notes="Model not in the reasoning registry.",
        )
    return Resolution(
        provider=provider,
        model_id=model_id,
        style=entry.style,
        levels=entry.levels,
        in_registry=True,
        notes=entry.notes,
    )


def build_request_kwargs(resolution: Resolution, level: ReasoningLevel, model_id: str) -> dict:
    """Return the kwargs to merge into the chat-completions request.

    Returns an empty dict when reasoning is off or the style needs no parameter.
    """
    if level == "off":
        return {}
    style = resolution.style
    if style == "reasoning_effort":
        return {"reasoning_effort": level}
    if style == "extra_body":
        return {"extra_body": {"reasoning": {"effort": level}}}
    # model_variant / unknown / unsupported: send nothing; the caller decides
    # whether to warn the user (unknown) or refuse (unsupported).
    return {}


def looks_like_rejection(message: str) -> bool:
    """Heuristic: does this error message complain about the reasoning parameter?"""
    low = (message or "").lower()
    return any(hint in low for hint in _REJECTION_HINTS)


def extract_reasoning_content(message: dict) -> str:
    """Pull reasoning text out of a chat-completions message.

    Providers differ in the field name; we check the common ones. An empty
    result means "no reasoning returned", which is what drives `unverified`.
    """
    if not isinstance(message, dict):
        return ""
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            inner = value.get("content") or value.get("text")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return ""


def classify_outcome(
    *,
    request_failed: bool,
    error_message: str,
    reasoning_text: str,
) -> tuple[str, str]:
    """Map one turn's outcome to (state, human detail)."""
    if request_failed:
        if looks_like_rejection(error_message):
            return "rejected", f"❌ Reasoning rejected — {error_message[:200]}"
        return "rejected", f"❌ Reasoning request failed — {error_message[:200]}"
    if reasoning_text:
        return "verified", "✓ Reasoning verified — the model returned reasoning content."
    return (
        "unverified",
        f"{NOT_VERIFIED_WARNING} — the model accepted the parameter but returned "
        "no reasoning content. It may be ignoring it.",
    )
