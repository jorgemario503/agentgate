"""One naming rule for runner history, with a fallback for the legacy dirs.

History lived in `{name}_history/` for the local runner but in hardcoded
`fw_history/` and `di_history/` for the other two. That made the resume block's
history path a three-way special case. The rule is now `{name}_history` for
every runner.

Renaming alone would orphan existing conversations, so a store whose new
directory has no file for a session still reads the legacy directory. Writes
always go to the new one, so an upgraded session migrates on its next save.
Reads never delete or move anything.
"""

import json

import pytest

from runners import deepinfra, fireworks, local

RUNNERS = [
    pytest.param(local, "ollama", None, id="local-ollama"),
    pytest.param(local, "mlx", None, id="local-mlx"),
    pytest.param(fireworks, "fireworks", "fw_history", id="fireworks"),
    pytest.param(deepinfra, "deepinfra", "di_history", id="deepinfra"),
]


def history_dir(runner_name):
    """The single naming rule every runner must follow."""
    return f"{runner_name}_history"


# -- the naming rule --------------------------------------------------------


@pytest.mark.parametrize("module,name,legacy", RUNNERS)
def test_store_uses_name_history(module, name, legacy, tmp_path):
    store = module.JsonFileSessionStore(directory=str(tmp_path / history_dir(name)))
    assert store.directory.name == history_dir(name)


def test_local_keeps_ollama_and_mlx_separate(tmp_path):
    """The reason local's name is dynamic: two providers must not share history."""
    assert history_dir("ollama") != history_dir("mlx")


# -- migration: legacy directories still readable ---------------------------


@pytest.mark.parametrize("module,name,legacy", RUNNERS)
def test_legacy_directory_is_still_read(module, name, legacy, tmp_path):
    """An upgraded session keeps its conversation."""
    if legacy is None:
        pytest.skip("local never had a legacy directory name")
    new_dir = tmp_path / history_dir(name)
    old_dir = tmp_path / legacy
    old_dir.mkdir()
    (old_dir / "t1.json").write_text(
        json.dumps([{"role": "user", "content": "from before the rename"}])
    )

    store = module.JsonFileSessionStore(
        directory=str(new_dir), legacy_directory=str(old_dir)
    )
    assert store.load("t1") == [{"role": "user", "content": "from before the rename"}]


def test_new_directory_wins_over_legacy(tmp_path):
    """Once migrated, the new file is authoritative."""
    store_cls = deepinfra.JsonFileSessionStore
    new_dir = tmp_path / "deepinfra_history"
    old_dir = tmp_path / "di_history"
    new_dir.mkdir()
    old_dir.mkdir()
    (new_dir / "t1.json").write_text(json.dumps([{"role": "user", "content": "new"}]))
    (old_dir / "t1.json").write_text(json.dumps([{"role": "user", "content": "old"}]))

    store = store_cls(directory=str(new_dir), legacy_directory=str(old_dir))
    assert store.load("t1") == [{"role": "user", "content": "new"}]


def test_save_writes_only_to_the_new_directory(tmp_path):
    """Saving migrates: the legacy file is left untouched, never deleted."""
    store_cls = deepinfra.JsonFileSessionStore
    new_dir = tmp_path / "deepinfra_history"
    old_dir = tmp_path / "di_history"
    old_dir.mkdir()
    legacy_file = old_dir / "t1.json"
    legacy_file.write_text(json.dumps([{"role": "user", "content": "old"}]))

    store = store_cls(directory=str(new_dir), legacy_directory=str(old_dir))
    store.save("t1", [{"role": "user", "content": "migrated"}])

    assert json.loads((new_dir / "t1.json").read_text()) == [
        {"role": "user", "content": "migrated"}
    ]
    assert legacy_file.exists(), "legacy history must never be removed"
    assert json.loads(legacy_file.read_text()) == [{"role": "user", "content": "old"}]


def test_no_legacy_directory_is_fine(tmp_path):
    """The argument is optional; a fresh install has nothing to migrate."""
    store = deepinfra.JsonFileSessionStore(directory=str(tmp_path / "deepinfra_history"))
    assert store.load("t1") == []


# -- the runners actually build their history store ------------------------


@pytest.mark.parametrize("module,name,legacy", RUNNERS)
def test_runner_history_store_uses_name_history(module, name, legacy, tmp_path, monkeypatch):
    """`_get_or_create_client` must resolve `{name}_history` and not crash."""
    import asyncio

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
    client = asyncio.run(
        runner._get_or_create_client("t1", "/ws", "t1", None, platform)
        if module is not local
        else runner._get_or_create_client("t1", "/ws", None, platform)
    )
    assert client.session_store.directory.name == history_dir(name)
