"""Tests for runners/deepinfra.py."""

import pytest

from runners.deepinfra import DeepInfraRunner


@pytest.fixture
def deepinfra_env(monkeypatch):
    monkeypatch.setenv("DEEPINFRA_API_KEY", "test-api-key")
    monkeypatch.setenv("DEEPINFRA_MODEL", "test/main-model")

    for key in (
        "DEEPINFRA_TEMPERATURE",
        "DEEPINFRA_MAX_TOKENS",
        "DEEPINFRA_SUMMARY_MODEL",
        "DEEPINFRA_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_deepinfra_runner_importable():
    assert DeepInfraRunner is not None


def test_deepinfra_inherits_local_runner():
    from runners.local import LocalRunner

    assert issubclass(DeepInfraRunner, LocalRunner)


def test_deepinfra_from_env(deepinfra_env):
    runner = DeepInfraRunner.from_env()

    assert runner.name == "deepinfra"
    assert runner.model == "test/main-model"
    assert runner.api_key == "test-api-key"
    assert runner.base_url == (
        "https://api.deepinfra.com/v1/openai/"
    )


def test_deepinfra_uses_provider_defaults(deepinfra_env):
    runner = DeepInfraRunner.from_env()

    assert runner.temperature is None
    assert runner.max_tokens is None


def test_deepinfra_explicit_generation_settings(
    deepinfra_env, monkeypatch
):
    monkeypatch.setenv("DEEPINFRA_TEMPERATURE", "0.7")
    monkeypatch.setenv("DEEPINFRA_MAX_TOKENS", "8192")

    runner = DeepInfraRunner.from_env()

    assert runner.temperature == pytest.approx(0.7)
    assert runner.max_tokens == 8192


def test_deepinfra_model_override(deepinfra_env):
    runner = DeepInfraRunner.from_env(
        model="test/cli-model"
    )

    assert runner.model == "test/cli-model"


def test_deepinfra_summary_model_optional(deepinfra_env):
    runner = DeepInfraRunner.from_env()

    assert runner.summary_model == ""


def test_deepinfra_summary_model_configured(
    deepinfra_env, monkeypatch
):
    monkeypatch.setenv(
        "DEEPINFRA_SUMMARY_MODEL",
        "test/summary-model",
    )

    runner = DeepInfraRunner.from_env()

    assert runner.summary_model == "test/summary-model"


def test_deepinfra_requires_api_key(
    deepinfra_env, monkeypatch
):
    monkeypatch.delenv("DEEPINFRA_API_KEY")

    with pytest.raises(ValueError, match="DEEPINFRA_API_KEY"):
        DeepInfraRunner.from_env()


def test_deepinfra_requires_model(
    deepinfra_env, monkeypatch
):
    monkeypatch.delenv("DEEPINFRA_MODEL")

    with pytest.raises(ValueError, match="model|MODEL"):
        DeepInfraRunner.from_env()
