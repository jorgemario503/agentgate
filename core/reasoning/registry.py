"""Reasoning-mode support for OpenAI-compatible serverless providers.

Design notes
------------
There is no provider endpoint that tells us *which parameter shape* a model
accepts for reasoning. The authoritative source is documentation, so we keep a
small static registry and treat anything absent from it as **unknown**.

Three observable states, all decided from real traffic (never guessed):

  verified    the model returned reasoning content / thinking text
  unverified  the request succeeded but no reasoning content came back
              (accepted-but-maybe-ignored — the user is warned explicitly)
  rejected    the provider rejected the parameter (HTTP 400)

We never persist a probe result as authoritative: a 200 only proves the
parameter parsed, not that the model honours it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ReasoningLevel = Literal["off", "low", "medium", "high"]
ReasoningState = Literal["unknown", "verified", "unverified", "rejected", "failed"]

REASONING_LEVELS: tuple[str, ...] = ("off", "low", "medium", "high")

# How a provider expects the reasoning control to be expressed.
ReasoningStyle = Literal[
    "reasoning_effort",   # top-level `reasoning_effort`: "low"|"medium"|"high"
    "extra_body",         # provider-specific object nested under extra_body
    "model_variant",      # the model id encodes it; no parameter is sent
    "unsupported",        # known not to support reasoning controls
]


@dataclass(frozen=True)
class ModelReasoning:
    """Registry entry: how one provider/model accepts reasoning."""

    style: ReasoningStyle
    levels: tuple[str, ...] = ("low", "medium", "high")
    # Content field the provider returns when reasoning actually happened.
    response_field: str = "reasoning_content"
    notes: str = ""


# ---------------------------------------------------------------------------
# Registry: provider -> model pattern -> entry
# Patterns are matched with fnmatch against the model id, first match wins,
# so put more specific patterns first.
# ---------------------------------------------------------------------------

REGISTRY: dict[str, list[tuple[str, ModelReasoning]]] = {
    "deepinfra": [
        # DeepSeek R1 family exposes reasoning_effort.
        ("deepseek-r1*", ModelReasoning("reasoning_effort", notes="DeepSeek R1 reasoning model")),
        ("deepseek-ai/DeepSeek-R1*", ModelReasoning("reasoning_effort")),
        ("*r1*", ModelReasoning("reasoning_effort")),
        # DeepSeek V3/V4 are covered by the generated fragment (registry_generated.py),
        # which records the catalog's own tags: V3.1 carries reasoning_effort, V4.x
        # carries reasoning only. Kept out of the hand list so the catalog stays the
        # single source of truth for these (docs/reasoning-registry-source-of-truth.md).
        # gpt-oss supports reasoning_effort low|medium|high.
        ("*gpt-oss*", ModelReasoning("reasoning_effort")),
        ("*kimi-k2*thinking*", ModelReasoning("reasoning_effort")),
        # -thinking variants encode it in the model id.
        ("*-thinking", ModelReasoning("model_variant", notes="thinking is a model variant")),
        ("*thinking*", ModelReasoning("model_variant")),
    ],
    "fireworks": [
        ("*deepseek-r1*", ModelReasoning("reasoning_effort")),
        ("*r1*", ModelReasoning("reasoning_effort")),
        ("*gpt-oss*", ModelReasoning("reasoning_effort")),
        ("*kimi-k2*thinking*", ModelReasoning("reasoning_effort")),
        ("*thinking*", ModelReasoning("reasoning_effort")),
    ],
    "local": [
        # Ollama / vLLM / llama.cpp expose reasoning_effort when supported.
        ("*gpt-oss*", ModelReasoning("reasoning_effort")),
        ("*deepseek-r1*", ModelReasoning("reasoning_effort")),
        ("*qwen3*", ModelReasoning("reasoning_effort")),
        ("*thinking*", ModelReasoning("reasoning_effort")),
    ],
}


def _with_generated(provider: str, entries: list[tuple[str, ModelReasoning]]) -> list[tuple[str, ModelReasoning]]:
    """Append the machine-generated patterns for `provider` after the hand-written ones.

    Generated entries are exact model ids (see scripts/refresh_reasoning_registry.py),
    so they are the most specific possible match and never shadow a hand-written
    wildcard that a human deliberately placed first. A missing generated module
    is not an error: the hand-written patterns still work.
    """
    try:
        from core.reasoning import registry_generated as _gen
    except Exception:
        return entries
    generated = getattr(_gen, f"{provider.upper()}_GENERATED", [])
    return entries + list(generated)


REGISTRY["deepinfra"] = _with_generated("deepinfra", REGISTRY["deepinfra"])


def lookup(provider: str, model_id: str) -> ModelReasoning | None:
    """Return the registry entry for a provider/model, or None when unknown."""
    import fnmatch

    entries = REGISTRY.get(provider.lower())
    if not entries:
        return None
    model = (model_id or "").strip()
    for pattern, entry in entries:
        if fnmatch.fnmatch(model.lower(), pattern.lower()):
            return entry
    return None


@dataclass
class ReasoningRuntimeState:
    """Per-thread runtime state: the requested level plus what we observed.

    Never persisted. Reset when the session ends or the model changes.
    """

    level: ReasoningLevel = "off"
    state: ReasoningState = "unknown"
    last_reasoning: str = ""
    model_id: str = ""
    detail: str = ""

    def reset_for_model(self, model_id: str) -> None:
        if model_id != self.model_id:
            self.model_id = model_id
            self.state = "unknown"
            self.last_reasoning = ""
            self.detail = ""
