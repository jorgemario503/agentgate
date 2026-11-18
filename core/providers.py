"""Declarative provider specs for the open-model (OpenAI-compatible) runners.

Single source of truth for every configuration variable those runners read.

Rules
-----
* Per-provider variable names, prefixed by the provider (DeepInfra, Fireworks,
  Ollama, MLX).
* Runners receive a ``ProviderSettings`` and must not read the process
  environment directly.
* A ``None`` default means "leave it unset" - used for values the provider
  itself should decide (e.g. DeepInfra temperature), so we omit them from the
  request instead of inventing a value.
* Summary/compaction knobs are uniform across providers, defaulting to the
  historical per-provider values so behaviour does not change.

Claude is deliberately NOT part of this module. It has no credential, no base
URL and no sampling knobs, so forcing it into this shape would produce a spec
full of meaningless nulls.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    """Static description of one open-model provider's configuration surface."""

    name: str
    env_prefix: str
    base_url_default: str | None
    api_key_required: bool
    model_default: str | None
    context_window_default: int | None
    temperature_default: float | None
    max_tokens_default: int | None
    summary_model_default: str | None
    summary_max_tokens_default: int | None
    summary_temperature_default: float | None
    summary_chunk_chars_default: int
    supports_extra_body: bool = False

    def env(self, suffix: str) -> str:
        return f"{self.env_prefix}_{suffix}"


PROVIDERS: dict[str, ProviderSpec] = {
    "deepinfra": ProviderSpec(
        name="deepinfra",
        env_prefix="DEEPINFRA",
        base_url_default="https://api.deepinfra.com/v1/openai",
        api_key_required=True,
        model_default=None,
        context_window_default=None,
        temperature_default=None,
        max_tokens_default=None,
        summary_model_default=None,
        summary_max_tokens_default=None,
        summary_temperature_default=None,
        summary_chunk_chars_default=20_000,
    ),
    "fireworks": ProviderSpec(
        name="fireworks",
        env_prefix="FIREWORKS",
        base_url_default=None,
        api_key_required=True,
        model_default="minimax-m3",
        context_window_default=512_000,
        temperature_default=0.2,
        max_tokens_default=4096,
        summary_model_default="accounts/fireworks/models/llama-v3p1-8b-instruct",
        summary_max_tokens_default=3072,
        summary_temperature_default=0.2,
        summary_chunk_chars_default=20_000,
    ),
    "ollama": ProviderSpec(
        name="ollama",
        env_prefix="OLLAMA",
        base_url_default="http://localhost:11434/v1",
        api_key_required=False,
        model_default=None,
        context_window_default=32_768,
        temperature_default=0.2,
        max_tokens_default=4096,
        summary_model_default=None,
        summary_max_tokens_default=2048,
        summary_temperature_default=0.2,
        summary_chunk_chars_default=20_000,
    ),
    "mlx": ProviderSpec(
        name="mlx",
        env_prefix="MLX",
        base_url_default="http://localhost:8081/v1",
        api_key_required=False,
        model_default=None,
        context_window_default=32_768,
        temperature_default=0.2,
        max_tokens_default=4096,
        summary_model_default=None,
        summary_max_tokens_default=2048,
        summary_temperature_default=0.2,
        summary_chunk_chars_default=20_000,
    ),
}


@dataclass(frozen=True)
class ProviderSettings:
    """Resolved values for one provider, read from the process environment."""

    name: str
    base_url: str | None
    api_key: str
    model: str | None
    context_window: int | None
    temperature: float | None
    max_tokens: int | None
    summary_model: str | None
    summary_max_tokens: int | None
    summary_temperature: float | None
    summary_chunk_chars: int
    supports_extra_body: bool

    def require_model(self) -> str:
        if not self.model:
            raise ValueError(
                f"Set {PROVIDERS[self.name].env('MODEL')} or provide --model"
            )
        return self.model

    def require_api_key(self) -> str:
        if PROVIDERS[self.name].api_key_required and not self.api_key:
            raise ValueError(f"Set {PROVIDERS[self.name].env('API_KEY')}")
        return self.api_key


def _read_str(env: dict[str, str], key: str, default: str | None) -> str | None:
    raw = (env.get(key) or "").strip()
    return raw or default


def _read_int(env: dict[str, str], key: str, default: int | None) -> int | None:
    raw = (env.get(key) or "").strip()
    return int(raw) if raw else default


def _read_float(env: dict[str, str], key: str, default: float | None) -> float | None:
    raw = (env.get(key) or "").strip()
    return float(raw) if raw else default


def settings_for(
    provider: str,
    *,
    environ: dict[str, str] | None = None,
    model_override: str | None = None,
    api_key_override: str | None = None,
) -> ProviderSettings:
    """Resolve a provider's settings from the environment.

    ``environ`` is injectable for tests; production passes the process env.
    """
    spec = PROVIDERS.get(provider.lower())
    if spec is None:
        raise ValueError(
            f"Unknown provider {provider!r}. Known: {', '.join(sorted(PROVIDERS))}"
        )
    env = dict(os.environ if environ is None else environ)

    return ProviderSettings(
        name=spec.name,
        base_url=_read_str(env, spec.env("BASE_URL"), spec.base_url_default),
        api_key=(api_key_override or _read_str(env, spec.env("API_KEY"), "") or ""),
        model=(model_override or _read_str(env, spec.env("MODEL"), spec.model_default)),
        context_window=_read_int(env, spec.env("CONTEXT_WINDOW"), spec.context_window_default),
        temperature=_read_float(env, spec.env("TEMPERATURE"), spec.temperature_default),
        max_tokens=_read_int(env, spec.env("MAX_TOKENS"), spec.max_tokens_default),
        summary_model=_read_str(env, spec.env("SUMMARY_MODEL"), spec.summary_model_default),
        summary_max_tokens=_read_int(
            env, spec.env("SUMMARY_MAX_TOKENS"), spec.summary_max_tokens_default
        ),
        summary_temperature=_read_float(
            env, spec.env("SUMMARY_TEMPERATURE"), spec.summary_temperature_default
        ),
        summary_chunk_chars=_read_int(
            env, spec.env("SUMMARY_CHUNK_CHARS"), spec.summary_chunk_chars_default
        ) or spec.summary_chunk_chars_default,
        supports_extra_body=spec.supports_extra_body,
    )


def iter_provider_vars() -> list[tuple[str, str | None]]:
    """(variable_name, default_as_text) for every provider variable - used for docs."""
    getters = [
        ("BASE_URL", lambda s: s.base_url_default),
        ("API_KEY", lambda s: None),
        ("MODEL", lambda s: s.model_default),
        ("CONTEXT_WINDOW", lambda s: s.context_window_default),
        ("TEMPERATURE", lambda s: s.temperature_default),
        ("MAX_TOKENS", lambda s: s.max_tokens_default),
        ("SUMMARY_MODEL", lambda s: s.summary_model_default),
        ("SUMMARY_MAX_TOKENS", lambda s: s.summary_max_tokens_default),
        ("SUMMARY_TEMPERATURE", lambda s: s.summary_temperature_default),
        ("SUMMARY_CHUNK_CHARS", lambda s: s.summary_chunk_chars_default),
    ]
    out: list[tuple[str, str | None]] = []
    for spec in PROVIDERS.values():
        for suffix, getter in getters:
            value = getter(spec)
            out.append((spec.env(suffix), None if value is None else str(value)))
    return out
