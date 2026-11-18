"""Tests for runners/deepinfra.py.

The runner is a standalone class (not a subclass of LocalRunner) exposing
`from_env()`, `resolve_model()` and per-thread warming configuration.
Generation settings (temperature / max_tokens) are module-level values sourced
from the environment at import time, so we assert on `runners.deepinfra`'s
constants via reload rather than on instance attributes.
"""

import importlib

import pytest

from runners.deepinfra import DeepInfraRunner, resolve_model


@pytest.fixture
def deepinfra_env(monkeypatch):
    monkeypatch.setenv("DEEPINFRA_API_KEY", "test-api-key")
    monkeypatch.setenv("DEEPINFRA_MODEL", "test/main-model")

    for key in (
        "DEEPINFRA_TEMPERATURE",
        "DEEPINFRA_MAX_TOKENS",
        "DEEPINFRA_SUMMARY_MODEL",
        "DEEPINFRA_BASE_URL",
        "DEEPINFRA_CONTEXT_WINDOW",
    ):
        monkeypatch.delenv(key, raising=False)


def test_deepinfra_runner_importable():
    assert DeepInfraRunner is not None


def test_deepinfra_from_env_uses_env_model(deepinfra_env):
    runner = DeepInfraRunner.from_env()

    assert runner._model.model_id == "test/main-model"
    # Clients are created per-thread on demand, so nothing is connected yet.
    assert runner._clients == {}


def test_deepinfra_uses_provider_defaults(deepinfra_env, monkeypatch):
    """With no env overrides the module-level settings stay None (provider default)."""
    monkeypatch.delenv("DEEPINFRA_TEMPERATURE", raising=False)
    monkeypatch.delenv("DEEPINFRA_MAX_TOKENS", raising=False)

    import runners.deepinfra as di

    reloaded = importlib.reload(di)
    assert reloaded.TEMPERATURE is None
    assert reloaded.MAX_TOKENS is None


def test_deepinfra_explicit_generation_settings(deepinfra_env, monkeypatch):
    monkeypatch.setenv("DEEPINFRA_TEMPERATURE", "0.7")
    monkeypatch.setenv("DEEPINFRA_MAX_TOKENS", "8192")

    import runners.deepinfra as di

    reloaded = importlib.reload(di)
    assert reloaded.TEMPERATURE == pytest.approx(0.7)
    assert reloaded.MAX_TOKENS == 8192


def test_deepinfra_model_override(deepinfra_env):
    runner = DeepInfraRunner.from_env(model="test/cli-model")

    assert runner._model.model_id == "test/cli-model"


def test_deepinfra_summary_model_optional(deepinfra_env):
    import runners.deepinfra as di

    reloaded = importlib.reload(di)
    assert reloaded.SUMMARY_MODEL_ID == ""


def test_deepinfra_summary_model_configured(deepinfra_env, monkeypatch):
    monkeypatch.setenv("DEEPINFRA_SUMMARY_MODEL", "test/summary-model")

    import runners.deepinfra as di

    reloaded = importlib.reload(di)
    assert reloaded.SUMMARY_MODEL_ID == "test/summary-model"


def test_deepinfra_requires_api_key(deepinfra_env, monkeypatch):
    monkeypatch.delenv("DEEPINFRA_API_KEY")

    with pytest.raises(ValueError, match="DEEPINFRA_API_KEY"):
        DeepInfraRunner.from_env()


def test_deepinfra_requires_model(deepinfra_env, monkeypatch):
    monkeypatch.delenv("DEEPINFRA_MODEL")

    with pytest.raises(ValueError, match="model|MODEL"):
        resolve_model(None)
