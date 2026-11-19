"""One naming rule for runner history: `{name}_history`.

History lived in `{name}_history/` for the local runner but in hardcoded
`fw_history/` and `di_history/` for the other two, which made the resume
block's history path a three-way special case. The rule is now `{name}_history`
for every runner.

Migrating an existing install is a manual step: move the files from the old
directory into the new one. There is deliberately no fallback reading the old
names, so a directory that was not migrated reads as empty rather than
silently mixing two locations.
"""

import asyncio
import json

import pytest

from runners import deepinfra, fireworks, local

RUNNERS = [
    pytest.param(local, "ollama", id="local-ollama"),
    pytest.param(local, "mlx", id="local-mlx"),
    pytest.param(fireworks, "fireworks", id="fireworks"),
    pytest.param(deepinfra, "deepinfra", id="deepinfra"),
]


def history_dir(runner_name):
    """The single naming rule every runner must follow."""
    return f"{runner_name}_history"


# -- the naming rule --------------------------------------------------------


@pytest.mark.parametrize("module,name", RUNNERS)
def test_store_uses_name_history(module, name, tmp_path):
    store = module.JsonFileSessionStore(directory=str(tmp_path / history_dir(name)))
    assert store.directory.name == history_dir(name)


def test_for_runner_builds_the_name_history_path(tmp_path):
    store = deepinfra.JsonFileSessionStore.for_runner(tmp_path, "deepinfra")
    assert store.directory == tmp_path / "deepinfra_history"


def test_local_keeps_ollama_and_mlx_separate(tmp_path):
    """The reason local's name is dynamic: two providers must not share history."""
    ollama = local.JsonFileSessionStore.for_runner(tmp_path, "ollama")
    mlx = local.JsonFileSessionStore.for_runner(tmp_path, "mlx")
    assert ollama.directory != mlx.directory
    assert ollama.directory.name == "ollama_history"
    assert mlx.directory.name == "mlx_history"


def test_old_directories_are_not_read(tmp_path):
    """No legacy fallback: an unmigrated directory reads as empty."""
    old_dir = tmp_path / "di_history"
    old_dir.mkdir()
    (old_dir / "t1.json").write_text(json.dumps([{"role": "user", "content": "old"}]))

    store = deepinfra.JsonFileSessionStore.for_runner(tmp_path, "deepinfra")
    assert store.load("t1") == []


def test_roundtrip(tmp_path):
    store = deepinfra.JsonFileSessionStore.for_runner(tmp_path, "deepinfra")
    store.save("t1", [{"role": "user", "content": "hello"}])
    assert store.load("t1") == [{"role": "user", "content": "hello"}]


def test_corrupt_file_reads_as_empty(tmp_path):
    store = deepinfra.JsonFileSessionStore.for_runner(tmp_path, "deepinfra")
    store.directory.mkdir(parents=True, exist_ok=True)
    (store.directory / "t1.json").write_text("{not json")
    assert store.load("t1") == []


# -- the runners actually build their history store ------------------------


@pytest.mark.parametrize("module,name", RUNNERS)
def test_runner_history_store_uses_name_history(module, name, tmp_path, monkeypatch):
    """`_get_or_create_client` must resolve `{name}_history` and not crash."""
    from runners.base import PlatformConfig

    class Store:
        base = str(tmp_path)

    monkeypatch.setenv("DEEPINFRA_API_KEY", "k")
    monkeypatch.setenv("FIREWORKS_API_KEY", "k")
    if module is fireworks:
        runner = module.FireworksRunner(model="gpt-oss-20b")
    elif module is deepinfra:
        import core.config as cfg
        monkeypatch.setattr(cfg, "AGENTDB_ENABLED", False)
        runner = module.DeepInfraRunner(model="test-model")
    else:
        # from_env reads .env, which the suite disables; supply the minimum.
        runner = module.LocalRunner(
            name=name, base_url="http://localhost:11434/v1", model="test-model"
        )

    platform = PlatformConfig(session_store=Store(), agent_id="a")
    if module is local:
        client = asyncio.run(runner._get_or_create_client("t1", "/ws", None, platform))
    else:
        client = asyncio.run(runner._get_or_create_client("t1", "/ws", "t1", None, platform))
    assert client.session_store.directory.name == history_dir(name)
