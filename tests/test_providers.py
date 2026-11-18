"""Tests for the declarative provider specs (core/providers.py)."""

import pytest

from core.providers import PROVIDERS, iter_provider_vars, settings_for


def test_all_open_model_providers_are_registered():
    assert set(PROVIDERS) == {"deepinfra", "fireworks", "ollama", "mlx"}


def test_claude_is_not_a_provider_spec():
    # Claude is configured separately; it has no API key/base URL/sampling knobs.
    assert "claude" not in PROVIDERS


def test_unknown_provider_raises_with_helpful_message():
    with pytest.raises(ValueError, match="Unknown provider"):
        settings_for("nope", environ={})


def test_deepinfra_omits_unset_sampling_values():
    settings = settings_for("deepinfra", environ={"DEEPINFRA_MODEL": "m"})
    # None means "let the provider decide" -> omitted from the request.
    assert settings.temperature is None
    assert settings.max_tokens is None
    assert settings.context_window is None
    assert settings.base_url == "https://api.deepinfra.com/v1/openai"


def test_fireworks_has_its_historical_defaults():
    settings = settings_for("fireworks", environ={})
    assert settings.model == "minimax-m3"
    assert settings.context_window == 512_000
    assert settings.temperature == pytest.approx(0.2)
    assert settings.max_tokens == 4096
    assert settings.summary_model == "accounts/fireworks/models/llama-v3p1-8b-instruct"


def test_ollama_reads_prefixed_variables():
    settings = settings_for(
        "ollama",
        environ={"OLLAMA_MODEL": "qwen3:14b", "OLLAMA_TEMPERATURE": "0.7", "OLLAMA_BASE_URL": "http://x/v1"},
    )
    assert settings.model == "qwen3:14b"
    assert settings.temperature == pytest.approx(0.7)
    assert settings.base_url == "http://x/v1"


def test_mlx_defaults_and_override():
    defaults = settings_for("mlx", environ={})
    assert defaults.base_url == "http://localhost:8081/v1"
    assert defaults.context_window == 32_768
    override = settings_for("mlx", environ={"MLX_MODEL": "mlx-community/foo"})
    assert override.model == "mlx-community/foo"


def test_model_override_beats_environment():
    settings = settings_for(
        "ollama", environ={"OLLAMA_MODEL": "from-env"}, model_override="from-cli"
    )
    assert settings.model == "from-cli"


def test_require_model_raises_when_missing():
    with pytest.raises(ValueError, match="MODEL"):
        settings_for("deepinfra", environ={}).require_model()


def test_require_api_key_raises_only_when_required():
    with pytest.raises(ValueError, match="API_KEY"):
        settings_for("fireworks", environ={}).require_api_key()
    # local providers do not need a credential
    assert settings_for("ollama", environ={}).require_api_key() == ""


def test_summary_knobs_are_uniform_across_providers():
    for name in PROVIDERS:
        settings = settings_for(name, environ={})
        assert settings.summary_chunk_chars > 0
        assert settings.summary_max_tokens is not None or name == "deepinfra"


def test_iter_provider_vars_covers_every_spec():
    variables = iter_provider_vars()
    names = {name for name, _ in variables}
    assert len(variables) == len(PROVIDERS) * 10
    assert "DEEPINFRA_MODEL" in names
    assert "FIREWORKS_MODEL" in names
    assert "OLLAMA_MODEL" in names
    assert "MLX_MODEL" in names
