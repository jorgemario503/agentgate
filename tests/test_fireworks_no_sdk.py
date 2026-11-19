"""The fireworks runner must not require the `fireworks-ai` SDK.

Fireworks exposes an OpenAI-compatible chat-completions endpoint, exactly like
DeepInfra, so the runner should ride on the `openai` package that
requirements.txt already installs. Before this, `runners/fireworks.py` imported
its own SDK at module scope and raised RuntimeError in `__init__` when it was
missing, which made `--fireworks` the only backend that failed on a fresh
install.

These tests fail if the SDK import or the availability guard comes back.
"""

import asyncio
import inspect
import sys

import pytest

import runners.fireworks as fw


def test_module_does_not_import_the_fireworks_sdk():
    source = inspect.getsource(fw)
    assert "from fireworks import" not in source
    assert "import fireworks" not in source
    assert "_FIREWORKS_AVAILABLE" not in source


def test_fireworks_sdk_not_required_at_import():
    """Importing the runner must not pull fireworks-ai into sys.modules."""
    assert "fireworks" not in sys.modules


def test_client_builds_without_the_sdk():
    """No SDK, no guard: a client constructs with only the openai package."""
    client = fw.FireworksAgentClient(
        model="gpt-oss-20b",
        api_key="test-key",
        session_store=fw.JsonFileSessionStore(directory="/tmp/agentgate-fw-test"),
    )
    assert client._client is not None
    asyncio.run(client.disconnect())


def test_client_points_at_the_fireworks_endpoint():
    from core.providers import settings_for

    client = fw.FireworksAgentClient(
        model="gpt-oss-20b",
        api_key="test-key",
        session_store=fw.JsonFileSessionStore(directory="/tmp/agentgate-fw-test"),
    )
    base_url = str(client._client.base_url).rstrip("/")
    expected = settings_for("fireworks").base_url or "https://api.fireworks.ai/inference/v1"
    assert base_url == expected.rstrip("/")
    asyncio.run(client.disconnect())


def test_set_model_needs_no_network():
    """Model resolution is offline, matching the deepinfra runner."""
    runner = fw.FireworksRunner(model="gpt-oss-20b")
    spec = asyncio.run(runner.set_model("gpt-oss-20b"))
    assert spec.name == "gpt-oss-20b"
    assert spec.model_id == "accounts/fireworks/models/gpt-oss-20b"


def test_unknown_model_does_not_raise():
    """An unverifiable model falls back to the default context window."""
    runner = fw.FireworksRunner(model="gpt-oss-20b")
    spec = asyncio.run(runner.set_model("definitely-not-a-real-model"))
    assert spec.context_window == fw.DEFAULT_CONTEXT_WINDOW
