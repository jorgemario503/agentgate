"""Deterministic offline tests for the open-model Agent Skills feature.

These tests exercise real behavior (catalog discovery, tool handlers, session
state, outgoing messages), and assert that the disabled path is unchanged.
"""
import os
import tempfile
import unittest
from pathlib import Path

import core.prompts as prompts
import core.sessions as sessions
import core.skills as skills
import core.tools.registry as reg


def _write_skill(root, name, description="A skill.", body="BODY", extra=""):
    d = Path(root) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n{body}"
    )
    return d


class _Store:
    def lookup_directory(self, name):
        return None

    def remember_directory(self, name, path):
        pass


class TestDisabledEquivalence(unittest.TestCase):
    def test_manifest_empty_when_disabled(self):
        ctx = skills.build_skills_context(
            enabled=False, skills_dir="/nope", manifest_tokens=100, selection_mode="model"
        )
        self.assertFalse(ctx.enabled)
        self.assertEqual(ctx.manifest, "")

    def test_empty_catalog_disables_feature(self):
        tmp = tempfile.mkdtemp()
        ctx = skills.build_skills_context(
            enabled=True, skills_dir=tmp, manifest_tokens=100, selection_mode="model"
        )
        self.assertFalse(ctx.enabled)
        self.assertEqual(ctx.manifest, "")

    def test_registry_unchanged_when_catalog_none(self):
        tmp = tempfile.mkdtemp()
        base = reg.build_tools(tmp, _Store(), output_cap=8000, bash_timeout=120)
        self.assertNotIn("skill_read", base)
        self.assertNotIn("skill_list", base)
        self.assertEqual(set(base), {
            "Bash", "Read", "Write", "Edit", "Glob", "Grep",
            "lookup_directory", "remember_directory",
        })

    def test_prompt_unchanged_when_manifest_empty(self):
        state = sessions.SessionState.SESSION_START
        plain = prompts.build_system_prompt(state=state)
        with_empty = prompts.build_system_prompt(state=state, skills_manifest="")
        self.assertEqual(plain, with_empty)


class TestConformance(unittest.TestCase):
    def test_valid_skill_discovered(self):
        tmp = tempfile.mkdtemp()
        _write_skill(tmp, "pdf-reader", description="Read PDF files.", body="step 1")
        cat = skills.discover(tmp)
        self.assertEqual(cat.names, ("pdf-reader",))

    def test_directory_name_must_match(self):
        tmp = tempfile.mkdtemp()
        d = Path(tmp) / "wrong-dir"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: other\ndescription: x\n---\nb")
        cat = skills.discover(tmp)
        self.assertEqual(cat.names, ())
        self.assertIn("wrong-dir", " ".join(cat.diagnostics))

    def test_missing_description_excluded(self):
        tmp = tempfile.mkdtemp()
        d = Path(tmp) / "no-desc"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: no-desc\n---\nbody")
        cat = skills.discover(tmp)
        self.assertEqual(cat.names, ())

    def test_broken_yaml_excluded_not_raising(self):
        tmp = tempfile.mkdtemp()
        d = Path(tmp) / "broken"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: [unclosed\n---\nb")
        cat = skills.discover(tmp)
        self.assertNotIn("broken", cat.names)


class TestDuplicates(unittest.TestCase):
    def test_distinct_dirs_distinct_names(self):
        tmp = tempfile.mkdtemp()
        _write_skill(tmp, "dup-one")
        _write_skill(tmp, "z-dup")
        cat = skills.discover(tmp)
        self.assertEqual(cat.names, ("dup-one", "z-dup"))

    def test_name_is_unique(self):
        tmp = tempfile.mkdtemp()
        _write_skill(tmp, "dup", description="first")
        cat = skills.discover(tmp)
        self.assertEqual(cat.names.count("dup"), 1)


class TestNoEager(unittest.TestCase):
    def test_manifest_contains_name_only_not_body(self):
        tmp = tempfile.mkdtemp()
        _write_skill(tmp, "a-skill", description="Handle A.", body="HIDDEN-BODY-TEXT")
        cat = skills.discover(tmp)
        man = skills.build_manifest(cat, max_tokens=400)
        self.assertIn("a-skill", man)
        self.assertNotIn("HIDDEN-BODY-TEXT", man)


class TestManifest(unittest.TestCase):
    def test_deterministic(self):
        tmp = tempfile.mkdtemp()
        _write_skill(tmp, "beta", description="B.")
        _write_skill(tmp, "alpha", description="A.")
        cat = skills.discover(tmp)
        self.assertEqual(
            skills.build_manifest(cat, max_tokens=400),
            skills.build_manifest(cat, max_tokens=400),
        )

    def test_budget_reports_omissions_and_skill_list(self):
        tmp = tempfile.mkdtemp()
        _write_skill(tmp, "one", description="one " * 50)
        _write_skill(tmp, "two", description="two " * 50)
        cat = skills.discover(tmp)
        man = skills.build_manifest(cat, max_tokens=40)
        self.assertIn("omitted", man)
        self.assertIn("skill_list", man)


class TestBody(unittest.TestCase):
    def test_exact_body_returned(self):
        tmp = tempfile.mkdtemp()
        _write_skill(tmp, "exact", body="line1\nline2\nline3")
        cat = skills.discover(tmp)
        self.assertEqual(skills.read_skill_body(cat, "exact"), "line1\nline2\nline3")

    def test_body_not_truncated_by_output_cap(self):
        tmp = tempfile.mkdtemp()
        big = "x" * 20000
        _write_skill(tmp, "big", body=big)
        cat = skills.discover(tmp)
        tools = reg.build_tools(tmp, _Store(), output_cap=100, bash_timeout=120,
                                skills_catalog=cat)
        out = tools["skill_read"].handler({"name": "big"})
        self.assertEqual(len(out), len(big))


class TestPathSafety(unittest.TestCase):
    def test_traversal_name_fails(self):
        tmp = tempfile.mkdtemp()
        _write_skill(tmp, "safe")
        cat = skills.discover(tmp)
        with self.assertRaises(skills.SkillReadError):
            skills.read_skill_body(cat, "../../anywhere")

    def test_symlink_escape_excluded(self):
        tmp = tempfile.mkdtemp()
        outside = tempfile.mkdtemp()
        _write_skill(outside, "evil")
        link = Path(tmp) / "evil"
        try:
            link.symlink_to(Path(outside) / "evil", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported")
        cat = skills.discover(tmp)
        self.assertNotIn("evil", cat.names)


class TestAllowedTools(unittest.TestCase):
    def test_allowed_tools_does_not_widen_permissions(self):
        tmp = tempfile.mkdtemp()
        _write_skill(tmp, "s", extra="allowed-tools: Bash\n")
        cat = skills.discover(tmp)
        tools = reg.build_tools(tmp, _Store(), output_cap=8000, bash_timeout=120,
                                skills_catalog=cat)
        self.assertIn("skill_read", tools)
        self.assertEqual(
            set(tools) - {"skill_read", "skill_list"},
            {"Bash", "Read", "Write", "Edit", "Glob", "Grep",
             "lookup_directory", "remember_directory"},
        )


class TestSharedRegistry(unittest.TestCase):
    def test_one_builder_for_all_runners(self):
        tmp = tempfile.mkdtemp()
        _write_skill(tmp, "s", description="D.", body="b")
        cat = skills.discover(tmp)
        a = reg.build_tools(tmp, _Store(), output_cap=100, bash_timeout=10, skills_catalog=cat)
        b = reg.build_tools(tmp, _Store(), output_cap=200, bash_timeout=20, skills_catalog=cat)
        self.assertEqual(
            set(a) - {"skill_read", "skill_list"},
            set(b) - {"skill_read", "skill_list"},
        )


class TestPrefixStable(unittest.TestCase):
    def test_manifest_deterministic_across_calls(self):
        tmp = tempfile.mkdtemp()
        _write_skill(tmp, "s", description="D.")
        cat = skills.discover(tmp)
        first = skills.build_manifest(cat, max_tokens=400)
        for _ in range(5):
            self.assertEqual(skills.build_manifest(cat, max_tokens=400), first)


class TestSessionLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _write_skill(self.tmp, "pdf-reader", body="USE-PDFTOTEXT")
        self.cat = skills.discover(self.tmp)
        self.body = skills.read_skill_body(self.cat, "pdf-reader")
        self.sess = skills.SkillSession(self.cat)

    def test_activation_recorded_only_after_body_appended(self):
        skills.record_activation_from_result(
            self.sess, self.cat, "skill_read", {"name": "pdf-reader"}, "ERROR: x", 1
        )
        self.assertEqual(self.sess.active_names, ())
        skills.record_activation_from_result(
            self.sess, self.cat, "skill_read", {"name": "pdf-reader"}, self.body, 1
        )
        self.assertEqual(self.sess.active_names, ("pdf-reader",))

    def test_s11_not_repeated_while_in_messages(self):
        self.sess.record_activation("pdf-reader", self.cat.get("pdf-reader").fingerprint, 1)
        msgs = [{"role": "tool", "content": self.body}]
        self.assertEqual(self.sess.reactivation_block(msgs, self.cat, 2), "")

    def test_s12_reactivation_after_trim_once(self):
        # Same task turn: activation at turn 2, still turn 2 when trimmed -> restore once.
        self.sess.record_activation("pdf-reader", self.cat.get("pdf-reader").fingerprint, 2)
        trimmed = [{"role": "user", "content": "continue"}]
        block = self.sess.reactivation_block(trimmed, self.cat, 2)
        self.assertIn("USE-PDFTOTEXT", block)
        # Once only, even on repeat calls within the same turn.
        self.assertEqual(self.sess.reactivation_block(trimmed, self.cat, 2), "")

    def test_s12b_no_restore_after_task_boundary(self):
        # Activated in task turn 1; a later turn (2) must NOT auto-restore it.
        self.sess.record_activation("pdf-reader", self.cat.get("pdf-reader").fingerprint, 1)
        trimmed = [{"role": "user", "content": "new unrelated task"}]
        self.assertEqual(self.sess.reactivation_block(trimmed, self.cat, 2), "")

    def test_s13_changed_fingerprint_not_silently_reserved(self):
        self.sess.record_activation("pdf-reader", "stale-fingerprint", 1)
        (Path(self.tmp) / "pdf-reader" / "SKILL.md").write_text(
            "---\nname: pdf-reader\ndescription: Changed.\n---\n\nDIFFERENT"
        )
        trimmed = [{"role": "user", "content": "continue"}]
        self.assertEqual(self.sess.reactivation_block(trimmed, self.cat, 2), "")


if __name__ == "__main__":
    unittest.main()


# -- startup reporting ------------------------------------------------------


def test_startup_report_lists_catalog_and_mode():
    """An operator must be able to see what skills resolved."""
    from core.skills import build_skills_context, skills_startup_report

    ctx = build_skills_context(enabled=True, skills_dir="skills",
                               manifest_tokens=600, selection_mode="model")
    lines = skills_startup_report(ctx)
    assert lines
    assert "mode 'model'" in lines[0]
    for entry in ctx.catalog.entries:
        assert entry.name in lines[0]


def test_startup_report_warns_on_an_unrecognised_mode():
    """A typo used to fall back to Mode A invisibly."""
    from core.skills import build_skills_context, skills_startup_report

    ctx = build_skills_context(enabled=True, skills_dir="skills",
                               manifest_tokens=600, selection_mode="recomend")
    lines = skills_startup_report(ctx)
    joined = "\n".join(lines)
    assert "recomend" in joined, "the bad value is not reported"
    assert "falling back to 'model'" in joined
    # The resolved mode is still reported on its own line.
    assert any("mode 'model'" in line for line in lines)


def test_startup_report_is_silent_when_skills_are_disabled():
    """Disabled consults nothing, so there is nothing to report."""
    from core.skills import build_skills_context, skills_startup_report

    ctx = build_skills_context(enabled=False, skills_dir="skills",
                               manifest_tokens=600, selection_mode="model")
    assert skills_startup_report(ctx) == []


def test_startup_report_surfaces_discovery_diagnostics(tmp_path):
    """An excluded skill is otherwise invisible: it simply never appears."""
    from core.skills import build_skills_context, skills_startup_report

    root = tmp_path / "skills"
    bad = root / "broken-skill"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("---\nname: broken-skill\n---\nno description\n")

    ctx = build_skills_context(enabled=True, skills_dir=str(root),
                               manifest_tokens=600, selection_mode="model")
    joined = "\n".join(skills_startup_report(ctx))
    assert "description" in joined.lower(), f"diagnostic not surfaced: {joined!r}"


def test_bots_report_skills_at_startup():
    """All three bots print the report, like they do for ADDITIONAL_DIRS."""
    import inspect

    for module_path in ("bots/slack.py", "bots/telegram.py", "bots/whatsapp.py"):
        source = inspect.getsource(
            __import__(module_path.replace("/", ".")[:-3], fromlist=["x"])
        )
        assert "skills_startup_report(" in source, f"{module_path} does not report skills"


# -- skills directory resolution -------------------------------------------


def test_home_relative_skills_dir_is_expanded():
    """`~/agent-skills` is how people point at a shared folder outside the repo.

    Path does not expand `~`, so without this the literal directory
    `~/agent-skills` is looked for and not found — an empty catalog that reads
    as "no skills configured".
    """
    from core.skills.catalog import resolve_skills_dir

    expanded = resolve_skills_dir("~/agent-skills")
    assert expanded == os.path.join(os.path.expanduser("~"), "agent-skills")
    assert "~" not in expanded


def test_absolute_skills_dir_is_unchanged():
    from core.skills.catalog import resolve_skills_dir

    assert resolve_skills_dir("/opt/team/skills") == "/opt/team/skills"


def test_relative_skills_dir_stays_relative():
    """Relative stays relative: the startup report says what it resolved to."""
    from core.skills.catalog import resolve_skills_dir

    assert resolve_skills_dir("skills") == "skills"


def test_empty_skills_dir_is_passed_through():
    from core.skills.catalog import resolve_skills_dir

    assert resolve_skills_dir(None) is None
    assert resolve_skills_dir("") == ""


def test_home_relative_dir_finds_a_real_catalog(tmp_path, monkeypatch):
    """End to end: an expanded `~` path actually discovers skills."""
    from core.skills import build_skills_context

    home = tmp_path / "home"
    skill = home / "agent-skills" / "my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: a shared skill\n---\nbody\n"
    )
    monkeypatch.setenv("HOME", str(home))

    ctx = build_skills_context(enabled=True, skills_dir="~/agent-skills",
                               manifest_tokens=600, selection_mode="model")
    assert [e.name for e in ctx.catalog.entries] == ["my-skill"]


def test_startup_report_names_the_resolved_directory():
    """A relative setting must not be ambiguous at startup."""
    from core.skills import build_skills_context, skills_startup_report

    ctx = build_skills_context(enabled=True, skills_dir="skills",
                               manifest_tokens=600, selection_mode="model")
    joined = "\n".join(skills_startup_report(ctx))
    assert "dir" in joined
    assert os.path.abspath("skills") in joined


def test_missing_directory_diagnostic_names_the_full_path():
    """'not found' is only actionable if the operator sees where it looked."""
    from core.skills import build_skills_context, skills_startup_report

    ctx = build_skills_context(enabled=True, skills_dir="definitely-not-here-xyz",
                               manifest_tokens=600, selection_mode="model")
    joined = "\n".join(skills_startup_report(ctx))
    assert "definitely-not-here-xyz" in joined
    assert os.path.abspath("definitely-not-here-xyz") in joined
