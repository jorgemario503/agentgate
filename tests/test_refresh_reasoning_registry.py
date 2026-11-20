"""Tests for scripts/refresh_reasoning_registry.py — the pure logic only.

The network fetch is not exercised here; the catalog shape is fixed by the
provider. What matters is the mapping (tags -> style) and the diff, which
decide what the generated fragment gains or loses.
"""

import importlib.util
from pathlib import Path


def _load_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "refresh_reasoning_registry.py"
    spec = importlib.util.spec_from_file_location("refresh_reasoning_registry", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


script = _load_script()


# -- tag -> style -----------------------------------------------------------

def test_classify_reasoning_effort_wins_over_plain_reasoning():
    assert script.classify_tags(["chat", "reasoning_effort", "reasoning"]) == "reasoning_effort"


def test_classify_plain_reasoning_only_is_model_variant():
    assert script.classify_tags(["chat", "reasoning"]) == "model_variant"


def test_classify_no_reasoning_tag_is_none():
    assert script.classify_tags(["chat", "prompt_cache"]) is None


def test_classify_empty_and_missing_are_none():
    assert script.classify_tags([]) is None
    assert script.classify_tags(None) is None  # type: ignore[arg-type]


def test_classify_is_case_and_space_insensitive():
    assert script.classify_tags([" Reasoning_Effort "]) == "reasoning_effort"


# -- build_entries ----------------------------------------------------------

def test_build_entries_maps_and_sorts():
    models = [
        {"id": "b/effort", "metadata": {"tags": ["reasoning_effort"]}},
        {"id": "a/variant", "metadata": {"tags": ["reasoning"]}},
        {"id": "c/plain", "metadata": {"tags": ["chat"]}},     # skipped
        {"id": "d/nometa"},                                     # skipped (no metadata)
        {"metadata": {"tags": ["reasoning"]}},                  # skipped (no id)
    ]
    entries = script.build_entries(models)
    assert entries == [("a/variant", "model_variant"), ("b/effort", "reasoning_effort")]


def test_build_entries_empty_catalog():
    assert script.build_entries([]) == []


# -- diff -------------------------------------------------------------------

def test_diff_detects_added_removed_changed():
    old = [("keep", "reasoning_effort"), ("gone", "model_variant"), ("flip", "model_variant")]
    new = [("keep", "reasoning_effort"), ("new", "reasoning_effort"), ("flip", "reasoning_effort")]
    added, removed, changed = script.diff(old, new)
    assert added == [("new", "reasoning_effort")]
    assert removed == [("gone", "model_variant")]
    assert changed == [("flip", "model_variant", "reasoning_effort")]


def test_diff_is_empty_when_identical():
    entries = [("a", "reasoning_effort"), ("b", "model_variant")]
    assert script.diff(entries, list(entries)) == ([], [], [])


# -- render ----------------------------------------------------------------

def test_render_fragment_is_importable_shape_and_marks_generated():
    src = script.render_fragment(
        "deepinfra", [("x/y", "model_variant")], "https://api.deepinfra.com/v1/openai/models"
    )
    assert "GENERATED" in src
    assert "DEEPINFRA_GENERATED" in src
    assert '("x/y", ModelReasoning("model_variant"))' in src
    # It must be valid, compilable Python.
    compile(src, "<fragment>", "exec")
