"""User-editable prompt files — spec §6.7.

Operators can override the built-in prompt text without editing source. Files
live under `prompts/` and are read at startup; a missing or unreadable file
falls back to the built-in text with a printed warning, never to an empty
prompt (an empty system prompt would silently drop the hard rules).

Because providers cache a prompt by matching from the start, the shipped files
carry a header explaining the ordering rule: stable text first, volatile text
never in the prefix.
"""

import pytest
from pathlib import Path

from core import prompts


@pytest.fixture
def prompt_dir(tmp_path, monkeypatch):
    d = tmp_path / "prompts"
    d.mkdir()
    monkeypatch.setattr(prompts, "PROMPT_DIR", d, raising=True)
    monkeypatch.delenv(prompts.PROMPT_ENV, raising=False)
    return d


# -- overrides -------------------------------------------------------------


def test_base_prompt_override_replaces_builtin(prompt_dir):
    (prompt_dir / "base.md").write_text("CUSTOM BASE INSTRUCTIONS")
    assert "CUSTOM BASE INSTRUCTIONS" in prompts.build_base_prompt()


def test_hard_rules_override_replaces_builtin(prompt_dir):
    (prompt_dir / "hard_rules.md").write_text("CUSTOM HARD RULES")
    prompt = prompts.build_base_prompt()
    assert "CUSTOM HARD RULES" in prompt


def test_resume_block_override(prompt_dir):
    (prompt_dir / "resume.md").write_text("CUSTOM RESUME HEADER")
    block = prompts.build_resume_block("/tmp/h.json", "/ws")
    assert "CUSTOM RESUME HEADER" in block


def test_env_var_points_at_another_directory(prompt_dir, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "base.md").write_text("FROM ENV DIR")
    monkeypatch.setenv(prompts.PROMPT_ENV, str(other))
    assert "FROM ENV DIR" in prompts.build_base_prompt()


# -- safe fallback ---------------------------------------------------------


def test_missing_directory_falls_back_to_builtin(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "PROMPT_DIR", tmp_path / "nope", raising=True)
    monkeypatch.delenv(prompts.PROMPT_ENV, raising=False)
    assert "never violate these under any circumstances" in prompts.build_base_prompt()


def test_missing_file_falls_back_to_builtin(prompt_dir):
    prompt = prompts.build_base_prompt()
    assert "coding assistant" in prompt


def test_empty_file_falls_back_to_builtin(prompt_dir):
    """An empty override must not silently erase the hard rules."""
    (prompt_dir / "hard_rules.md").write_text("   \n\n")
    prompt = prompts.build_base_prompt()
    assert "never violate these under any circumstances" in prompt


def test_unreadable_file_falls_back(prompt_dir):
    target = prompt_dir / "hard_rules.md"
    target.write_text("x")
    target.chmod(0o000)
    try:
        prompt = prompts.build_base_prompt()
    finally:
        target.chmod(0o644)
    assert "never violate these under any circumstances" in prompt


def test_override_does_not_break_the_resume_composition(prompt_dir, tmp_path):
    """Overrides must not disturb the stable-to-volatile ordering."""
    import json

    (prompt_dir / "hard_rules.md").write_text("CUSTOM RULES")
    path = tmp_path / "t1.json"
    path.write_text(json.dumps([{"role": "user", "content": "earlier"}]))

    from core.sessions import SessionState

    prompt = prompts.build_system_prompt(
        state=SessionState.COLD, history_path=str(path), workspace_path="/ws"
    )
    assert prompt.index("CUSTOM RULES") < prompt.index(str(path))


def test_file_header_comments_do_not_reach_the_model(prompt_dir):
    """Operator notes at the top of a prompt file stay out of the prompt."""
    (prompt_dir / "base.md").write_text(
        "# PREFIX-CACHE RULE: keep this out of the prompt\n"
        "# second note\n"
        "\n"
        "ACTUAL BASE TEXT\n"
    )
    prompt = prompts.build_base_prompt()
    assert "ACTUAL BASE TEXT" in prompt
    assert "PREFIX-CACHE RULE" not in prompt


def test_shipped_prompt_files_load_cleanly():
    """The files we ship must parse without leaking their headers."""
    prompt = prompts.build_base_prompt()
    assert "coding assistant" in prompt
    assert "PREFIX-CACHE RULE" not in prompt
    assert "never violate these under any circumstances" in prompt


def test_shipped_hard_rules_match_the_builtin_structure():
    """The shipped hard_rules.md is an active override, so it must be sound.

    Shipping it is deliberate: the rules live in core/config.py in plain sight
    anyway, and hiding the file only made them harder to find. What matters is
    that it still states the rule categories the built-in text covers, so the
    override cannot quietly drop one.
    """
    shipped = Path("prompts") / prompts.HARD_RULES_FILE
    assert shipped.exists()
    text = shipped.read_text()

    from tests.test_prompt_files import _strip_header

    body = _strip_header(text).lower()
    for category in (
        "database tables",
        "secrets",
        "git",
        "where clause",
        "delete",
        "directory",
    ):
        assert category in body, f"shipped hard rules lost the {category!r} category"


def test_builtin_rules_remain_authoritative_without_override(tmp_path, monkeypatch):
    """With no override present, the enforced rule text is unchanged."""
    monkeypatch.setattr(prompts, "PROMPT_DIR", tmp_path, raising=True)
    monkeypatch.delenv(prompts.PROMPT_ENV, raising=False)
    prompt = prompts.build_base_prompt()
    assert "Never run DELETE FROM without a WHERE clause" in prompt
    assert "Never expose secrets, credentials, or tokens in output" in prompt


# -- section delimiters ----------------------------------------------------


def test_sections_use_markdown_headings(tmp_path, monkeypatch):
    """Each injected section is delimited by a fixed markdown heading."""
    import json

    monkeypatch.setattr(prompts, "PROMPT_DIR", tmp_path / "none", raising=True)
    monkeypatch.delenv(prompts.PROMPT_ENV, raising=False)
    path = tmp_path / "t1.json"
    path.write_text(json.dumps([{"role": "user", "content": "earlier"}]))

    from core.sessions import SessionState

    built = prompts.build_system_prompt(
        state=SessionState.COLD, history_path=str(path), workspace_path="/ws"
    )
    # `memory` only appears when AgentDB yields content, so it is checked
    # separately; these three are always present.
    for key in ("base", "hard_rules", "resume"):
        assert prompts.SECTION_HEADINGS[key] in built, f"missing heading: {key}"
    order = [
        built.index(prompts.SECTION_HEADINGS[k]) for k in ("base", "hard_rules", "resume")
    ]
    assert order == sorted(order), "headings are not in stable-to-volatile order"


def test_headings_are_not_overridable(prompt_dir):
    """Operators can edit content; the section headings are structural."""
    (prompt_dir / "base.md").write_text("CUSTOM TEXT")
    prompt = prompts.build_base_prompt()
    assert prompts.SECTION_HEADINGS["base"] in prompt
    assert "CUSTOM TEXT" in prompt


# -- summary prompt lives with the others ----------------------------------


def test_summary_prompt_is_centralised():
    from runners import deepinfra, fireworks, local

    for module in (local, deepinfra, fireworks):
        assert not hasattr(module, "_SUMMARY_SYSTEM_PROMPT"), (
            f"{module.__name__} still defines its own summary prompt"
        )
    assert prompts.build_summary_prompt()


def test_summary_prompt_is_overridable(prompt_dir):
    (prompt_dir / "summary.md").write_text("CUSTOM SUMMARY INSTRUCTIONS")
    assert prompts.build_summary_prompt() == "CUSTOM SUMMARY INSTRUCTIONS"


def test_summary_prompt_falls_back_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "PROMPT_DIR", tmp_path / "nope", raising=True)
    monkeypatch.delenv(prompts.PROMPT_ENV, raising=False)
    assert "condensing" in prompts.build_summary_prompt()


def _strip_header(text: str) -> str:
    """Mirror of the loader's comment stripping, for asserting on file bodies."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (lines[i].lstrip().startswith("#") or not lines[i].strip()):
        i += 1
    return "\n".join(lines[i:]).strip()
