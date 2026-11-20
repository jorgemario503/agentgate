"""Tests for core.repo_map — Python-first repository map (C3).

Maps to the spec's tests: T14 (ignore/secret/binary/out-of-root excluded),
T15 (incremental add/edit/delete), T16 (syntax errors reported, scan continues),
plus safety: root confinement, bounded output, no project execution.
"""
import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from core.repo_map import RepoMap, build_repo_map, build_repo_map_segment, find_repo_root


def _write(root: Path, rel: str, content: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content))


class TestRepoMapBuild(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_extracts_python_symbols(self):
        _write(self.root, "pkg/mod.py", """
            import os
            class Greeter:
                def hello(self, name):
                    return f"hi {name}"
            def top_level(x, y=1):
                pass
        """)
        m = build_repo_map(self.root)
        names = m.symbol_names()
        self.assertIn("Greeter", names)
        self.assertIn("top_level", names)
        # signature captured for top_level
        self.assertTrue(any("top_level" in s for s in m.render(10000).splitlines()))

    def test_ignored_and_secret_files_excluded(self):
        _write(self.root, "keep.py", "def keep():\n    pass\n")
        _write(self.root, ".env", "SECRET=1\n")
        _write(self.root, "secrets/key.txt", "hidden\n")
        (self.root / ".gitignore").write_text(".env\nsecrets/\n")
        m = build_repo_map(self.root)
        files = m.files()
        self.assertTrue(any(f.endswith("keep.py") for f in files))
        self.assertFalse(any(f.endswith(".env") for f in files))
        self.assertFalse(any("secrets/" in f for f in files))

    def test_binary_and_oversize_excluded(self):
        (self.root / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
        _write(self.root, "big.py", "def f():\n    pass\n" + "# pad\n" * 10000)
        m = build_repo_map(self.root, max_file_bytes=1000)
        files = m.files()
        self.assertFalse(any(f.endswith("blob.bin") for f in files))
        # over-size python file skipped
        self.assertFalse(any(f.endswith("big.py") for f in files))

    def test_syntax_error_reported_scan_continues(self):
        _write(self.root, "broken.py", "def broken(:\n    pass\n")
        _write(self.root, "good.py", "def good():\n    pass\n")
        m = build_repo_map(self.root)
        self.assertIn("good", m.symbol_names())
        self.assertTrue(m.parse_failures())  # broken.py reported, not fatal

    def test_root_confinement(self):
        _write(self.root, "a.py", "def a():\n    pass\n")
        outside = self.root.parent / "outside_zzz.py"
        outside.write_text("def zzz():\n    pass\n")
        try:
            m = build_repo_map(self.root)
            self.assertNotIn("zzz", m.symbol_names())
        finally:
            outside.unlink()

    def test_render_is_budgeted(self):
        for i in range(200):
            _write(self.root, f"m{i}.py", f"def fn_{i}(a, b, c):\n    return a\n")
        m = build_repo_map(self.root)
        out = m.render(token_budget=200)  # tiny budget
        self.assertLessEqual(len(out), 4000)  # bounded
        out_big = m.render(token_budget=100000)
        self.assertGreater(len(out_big), len(out))


class TestIncremental(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_edit_add_delete_incremental(self):
        _write(self.root, "a.py", "def old_name():\n    pass\n")
        m = build_repo_map(self.root)
        self.assertIn("old_name", m.symbol_names())

        # edit: rename
        _write(self.root, "a.py", "def new_name():\n    pass\n")
        m = build_repo_map(self.root, previous=m)
        self.assertIn("new_name", m.symbol_names())
        self.assertNotIn("old_name", m.symbol_names())

        # add
        _write(self.root, "b.py", "def added():\n    pass\n")
        m = build_repo_map(self.root, previous=m)
        self.assertIn("added", m.symbol_names())

        # delete
        (self.root / "b.py").unlink()
        m = build_repo_map(self.root, previous=m)
        self.assertNotIn("added", m.symbol_names())


class TestManifest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_manifest_roundtrip(self):
        _write(self.root, "x.py", "def xfn(a):\n    pass\n")
        m = build_repo_map(self.root)
        manifest = self.root / ".agentgate" / "map.json"
        m.save_manifest(manifest)
        loaded = RepoMap.load_manifest(manifest)
        self.assertIsNotNone(loaded)
        self.assertIn("xfn", loaded.symbol_names())

    def test_load_missing_returns_none(self):
        self.assertIsNone(RepoMap.load_manifest(self.root / "nope.json"))

    def test_reuse_previous_entries(self):
        _write(self.root, "y.py", "def yfn():\n    pass\n")
        m1 = build_repo_map(self.root)
        # second build with previous should reuse unchanged file (same symbol set)
        m2 = build_repo_map(self.root, previous=m1)
        self.assertIn("yfn", m2.symbol_names())
        self.assertEqual(set(m1.files()), set(m2.files()))


class TestSegment(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_find_repo_root_uses_git_marker(self):
        (self.root / ".git").mkdir()
        sub = self.root / "a" / "b"
        sub.mkdir(parents=True)
        self.assertEqual(find_repo_root(sub).resolve(), self.root.resolve())

    def test_segment_rendered_and_persisted(self):
        (self.root / ".git").mkdir()
        _write(self.root, "z.py", "def zfn():\n    pass\n")
        manifest = self.root / ".agentgate" / "map.json"
        seg = build_repo_map_segment(self.root, manifest_path=manifest)
        self.assertIsNotNone(seg)
        self.assertIn("Repository map", seg)
        self.assertIn("zfn", seg)
        self.assertTrue(manifest.is_file())

    def test_segment_none_for_missing_dir(self):
        self.assertIsNone(build_repo_map_segment(self.root / "does_not_exist"))


if __name__ == "__main__":
    unittest.main()
