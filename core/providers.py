"""Declarative provider specs + layered configuration resolution.

Single source of truth for every setting the open-model (OpenAI-compatible)
runners read, and the only place that knows about precedence.

Resolution order (lowest to highest priority)
---------------------------------------------
1. built-in default from the ProviderSpec
2. global override            ``MAX_TOOL_ITERATIONS``
3. per-provider override      ``DEEPINFRA_MAX_TOOL_ITERATIONS``
4. CLI override               ``--set MAX_TOOL_ITERATIONS=60`` (process only)

Every resolved value records where it came from, so a future CLI/TUI can show
"Temperature: 0.9 (from CLI)" without re-reading any configuration itself.

Providers that are inherently provider-specific (connection + model identity)
do NOT participate in the global layer: ``API_KEY``, ``BASE_URL``, ``MODEL``.

Internal algorithm constants (token estimation, compaction ratios) are not
exposed: they are correctness knobs, not user settings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable

# Suffixes that may also be set globally.
GLOBAL_OVERRIDABLE: tuple[str, ...] = (
    "TEMPERATURE",
    "MAX_TOKENS",
    "CONTEXT_WINDOW",
    "MAX_TOOL_ITERATIONS",
    "TOOL_OUTPUT_CAP",
    "BASH_TIMEOUT_SECONDS",
    "SUMMARY_MODEL",
    "SUMMARY_MAX_TOKENS",
    "SUMMARY_TEMPERATURE",
    "SUMMARY_CHUNK_CHARS",
    "CONSOLIDATION_MODEL",
    "CONSOLIDATION_MAX_TOKENS",
    "CONSOLIDATION_TEMPERATURE",
)

# Provider-identity suffixes: no global layer.
PROVIDER_ONLY: tuple[str, ...] = ("API_KEY", "BASE_URL", "MODEL")

SOURCE_DEFAULT = "default"
SOURCE_GLOBAL = "global"
SOURCE_PROVIDER = "provider"
SOURCE_CLI = "cli"


@dataclass(frozen=True)
class Resolved:
    """A resolved setting plus provenance, suitable for display in a CLI/TUI."""

    key: str
    value: Any
    source: str

    @property
    def is_default(self) -> bool:
        return self.source == SOURCE_DEFAULT

    def describe(self) -> str:
        if self.source == SOURCE_DEFAULT:
            return f"{self.key}={self.value} (default)"
        return f"{self.key}={self.value} (from {self.source})"


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
    consolidation_model_default: str | None = None
    consolidation_max_tokens_default: int | None = None
    consolidation_temperature_default: float | None = None
    max_tool_iterations_default: int = 25
    tool_output_cap_default: int = 8_000
    bash_timeout_seconds_default: int = 120
    supports_extra_body: bool = False

    def env(self, suffix: str) -> str:
        return f"{self.env_prefix}_{suffix}"

    def defaults(self) -> dict[str, Any]:
        """Built-in defaults keyed by suffix."""
        return {
            "BASE_URL": self.base_url_default,
            "API_KEY": None,
            "MODEL": self.model_default,
            "CONTEXT_WINDOW": self.context_window_default,
            "TEMPERATURE": self.temperature_default,
            "MAX_TOKENS": self.max_tokens_default,
            "MAX_TOOL_ITERATIONS": self.max_tool_iterations_default,
            "TOOL_OUTPUT_CAP": self.tool_output_cap_default,
            "BASH_TIMEOUT_SECONDS": self.bash_timeout_seconds_default,
            "SUMMARY_MODEL": self.summary_model_default,
            "SUMMARY_MAX_TOKENS": self.summary_max_tokens_default,
            "SUMMARY_TEMPERATURE": self.summary_temperature_default,
            "SUMMARY_CHUNK_CHARS": self.summary_chunk_chars_default,
            "CONSOLIDATION_MODEL": self.consolidation_model_default,
            "CONSOLIDATION_MAX_TOKENS": self.consolidation_max_tokens_default,
            "CONSOLIDATION_TEMPERATURE": self.consolidation_temperature_default,
        }


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
        consolidation_model_default="accounts/fireworks/models/llama-v3p1-8b-instruct",
        consolidation_max_tokens_default=3072,
        consolidation_temperature_default=0.2,
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


_CASTERS: dict[str, Callable[[str], Any]] = {
    "CONTEXT_WINDOW": int,
    "MAX_TOKENS": int,
    "MAX_TOOL_ITERATIONS": int,
    "TOOL_OUTPUT_CAP": int,
    "BASH_TIMEOUT_SECONDS": int,
    "SUMMARY_MAX_TOKENS": int,
    "SUMMARY_CHUNK_CHARS": int,
    "TEMPERATURE": float,
    "SUMMARY_TEMPERATURE": float,
    "CONSOLIDATION_MAX_TOKENS": int,
    "CONSOLIDATION_TEMPERATURE": float,
}


def _cast(suffix: str, raw: str) -> Any:
    caster = _CASTERS.get(suffix, str)
    value = raw.strip()
    return caster(value) if value else None


def _resolve_suffix(
    spec: ProviderSpec,
    suffix: str,
    env: dict[str, str],
    cli_overrides: dict[str, str],
) -> Resolved:
    """Apply the 4-layer cascade for one setting of one provider."""
    key = spec.env(suffix)

    # 4. CLI override (highest) - provider-prefixed name, or the bare name for
    #    suffixes that are also globally overridable.
    if key in cli_overrides:
        return Resolved(key, _cast(suffix, cli_overrides[key]), SOURCE_CLI)
    if suffix in GLOBAL_OVERRIDABLE and suffix in cli_overrides:
        return Resolved(key, _cast(suffix, cli_overrides[suffix]), SOURCE_CLI)

    # 3. Per-provider variable.
    raw = (env.get(key) or "").strip()
    if raw:
        return Resolved(key, _cast(suffix, raw), SOURCE_PROVIDER)

    # 2. Global override (only for suffixes that participate).
    if suffix in GLOBAL_OVERRIDABLE:
        global_raw = (env.get(suffix) or "").strip()
        if global_raw:
            return Resolved(key, _cast(suffix, global_raw), SOURCE_GLOBAL)

    # 1. Built-in default.
    return Resolved(key, spec.defaults().get(suffix), SOURCE_DEFAULT)


@dataclass(frozen=True)
class ProviderSettings:
    """Resolved values for one provider, with provenance for each setting."""

    name: str
    resolved: dict[str, Resolved] = field(default_factory=dict)

    # --- convenience accessors -------------------------------------------------
    def get(self, suffix: str) -> Any:
        entry = self.resolved.get(suffix)
        return entry.value if entry else None

    def source_of(self, suffix: str) -> str:
        entry = self.resolved.get(suffix)
        return entry.source if entry else SOURCE_DEFAULT

    @property
    def base_url(self) -> str | None:
        return self.get("BASE_URL")

    @property
    def api_key(self) -> str:
        return self.get("API_KEY") or ""

    @property
    def model(self) -> str | None:
        return self.get("MODEL")

    @property
    def context_window(self) -> int | None:
        return self.get("CONTEXT_WINDOW")

    @property
    def temperature(self) -> float | None:
        return self.get("TEMPERATURE")

    @property
    def max_tokens(self) -> int | None:
        return self.get("MAX_TOKENS")

    @property
    def max_tool_iterations(self) -> int:
        return self.get("MAX_TOOL_ITERATIONS") or 25

    @property
    def tool_output_cap(self) -> int:
        return self.get("TOOL_OUTPUT_CAP") or 8_000

    @property
    def bash_timeout_seconds(self) -> int:
        return self.get("BASH_TIMEOUT_SECONDS") or 120

    @property
    def summary_model(self) -> str | None:
        return self.get("SUMMARY_MODEL")

    @property
    def summary_max_tokens(self) -> int | None:
        return self.get("SUMMARY_MAX_TOKENS")

    @property
    def summary_temperature(self) -> float | None:
        return self.get("SUMMARY_TEMPERATURE")

    @property
    def summary_chunk_chars(self) -> int:
        return self.get("SUMMARY_CHUNK_CHARS") or 20_000

    @property
    def consolidation_model(self) -> str | None:
        return self.get("CONSOLIDATION_MODEL")

    @property
    def consolidation_max_tokens(self) -> int | None:
        return self.get("CONSOLIDATION_MAX_TOKENS")

    @property
    def consolidation_temperature(self) -> float | None:
        return self.get("CONSOLIDATION_TEMPERATURE")

    # --- validation ------------------------------------------------------------
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

    def describe(self) -> list[str]:
        """Human-readable "key=value (source)" lines, for logs or a future TUI."""
        return [self.resolved[k].describe() for k in sorted(self.resolved)]


CLI_OVERRIDES_VAR = "AGENTGATE_SET"


def parse_cli_overrides(pairs: list[str] | None) -> dict[str, str]:
    """Parse ["KEY=VALUE", ...] into a dict. Raises ValueError on bad input."""
    out: dict[str, str] = {}
    for item in pairs or []:
        if "=" not in item:
            raise ValueError(f"--set expects KEY=VALUE, got {item!r}")
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"--set expects KEY=VALUE, got {item!r}")
        out[key] = value.strip()
    return out


def cli_overrides_from_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Read overrides handed to a bot subprocess via the CLI_OVERRIDES_VAR."""
    env = dict(os.environ if environ is None else environ)
    raw = env.get(CLI_OVERRIDES_VAR, "").strip()
    if not raw:
        return {}
    return parse_cli_overrides(raw.split(","))


def settings_for(
    provider: str,
    *,
    environ: dict[str, str] | None = None,
    model_override: str | None = None,
    api_key_override: str | None = None,
    cli_overrides: dict[str, str] | None = None,
) -> ProviderSettings:
    """Resolve a provider's settings through the 4-layer cascade.

    ``environ`` and ``cli_overrides`` are injectable for tests; production
    passes the process environment plus the ``--set`` values from the CLI.
    """
    spec = PROVIDERS.get(provider.lower())
    if spec is None:
        raise ValueError(
            f"Unknown provider {provider!r}. Known: {', '.join(sorted(PROVIDERS))}"
        )
    env = dict(os.environ if environ is None else environ)
    overrides = cli_overrides_from_env(env)
    overrides.update(cli_overrides or {})

    if model_override:
        overrides[spec.env("MODEL")] = model_override
    if api_key_override:
        overrides[spec.env("API_KEY")] = api_key_override

    resolved = {
        suffix: _resolve_suffix(spec, suffix, env, overrides)
        for suffix in spec.defaults()
    }
    return ProviderSettings(name=spec.name, resolved=resolved)


def iter_provider_vars() -> list[tuple[str, str | None]]:
    """(variable_name, built-in default) for docs generation."""
    out: list[tuple[str, str | None]] = []
    for spec in PROVIDERS.values():
        for suffix, value in spec.defaults().items():
            out.append((spec.env(suffix), None if value is None else str(value)))
    return out


def iter_global_vars() -> list[tuple[str, str | None]]:
    """Global override names and the default they fall back to, for docs."""
    out: list[tuple[str, str | None]] = []
    for suffix in GLOBAL_OVERRIDABLE:
        default = None
        for spec in PROVIDERS.values():
            default = spec.defaults().get(suffix)
            if default is not None:
                break
        out.append((suffix, None if default is None else str(default)))
    return out
