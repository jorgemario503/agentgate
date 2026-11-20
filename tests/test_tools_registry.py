"""Tests for core.tools.registry — the shared open-model tool registry.

Pins the contract the three runners rely on: the 8 core tools, parameterized
output cap / bash timeout, and the optional tool_result_read addition.
"""
import tempfile
import unittest
from pathlib import Path

from core.tools.registry import Tool, build_tools


class _FakeStore:
    def __init__(self):
        self.remembered = {}

    def lookup_directory(self, name):
        return self.remembered.get(name)

    def remember_directory(self, name, path):
        self.remembered[name] = path


EXPECTED = {"Bash", "Read", "Write", "Edit", "Glob", "Grep",
            "lookup_directory", "remember_directory"}


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = _FakeStore()

    def _tools(self, **kw):
        return build_tools(self.dir, self.store, output_cap=8000, bash_timeout=120, **kw)

    def test_has_all_core_tools(self):
        tools = self._tools()
        self.assertEqual(set(tools), EXPECTED)
        for name, tool in tools.items():
            self.assertIsInstance(tool, Tool)
            self.assertEqual(tool.schema["type"], "function")
            self.assertEqual(tool.schema["function"]["name"], name)
            self.assertTrue(callable(tool.handler))

    def test_no_result_read_without_pipeline(self):
        self.assertNotIn("tool_result_read", self._tools())

    def test_result_read_added_with_pipeline(self):
        from core.tool_pipeline import ToolResultPipeline
        real = ToolResultPipeline(self.dir, session_id="s", workspace_id="w", enabled=True)
        tools = self._tools(pipeline=real)
        self.assertIn("tool_result_read", tools)

    def test_bash_runs_and_truncates(self):
        tools = build_tools(self.dir, self.store, output_cap=50, bash_timeout=30)
        out = tools["Bash"].handler({"command": "printf 'x%.0s' {1..200}"})
        self.assertLessEqual(len(out), 60)  # truncated to ~cap (+ "(no output)" guard)

    def test_bash_timeout_uses_param(self):
        tools = build_tools(self.dir, self.store, output_cap=8000, bash_timeout=1)
        # a command that sleeps longer than the timeout returns the timeout error
        out = tools["Bash"].handler({"command": "sleep 3"})
        self.assertIn("timed out after 1s", out)

    def test_write_read_edit_roundtrip(self):
        tools = self._tools()
        tools["Write"].handler({"file_path": "a.txt", "content": "hello world"})
        self.assertEqual(tools["Read"].handler({"file_path": "a.txt"}), "hello world")
        tools["Edit"].handler({"file_path": "a.txt", "old_string": "world", "new_string": "there"})
        self.assertEqual(tools["Read"].handler({"file_path": "a.txt"}), "hello there")
        # ambiguous edit is rejected
        (Path(self.dir) / "b.txt").write_text("x x")
        err = tools["Edit"].handler({"file_path": "b.txt", "old_string": "x", "new_string": "y"})
        self.assertIn("ambiguous", err)

    def test_lookup_and_remember_directory(self):
        tools = self._tools()
        tools["remember_directory"].handler({"name": "proj", "path": "/tmp/proj"})
        self.assertEqual(tools["lookup_directory"].handler({"name": "proj"}), "/tmp/proj")
        self.assertIn("No directory", tools["lookup_directory"].handler({"name": "missing"}))

    def test_glob_and_grep(self):
        (Path(self.dir) / "mod.py").write_text("def foo():\n    pass\ndef bar():\n    pass\n")
        tools = self._tools()
        self.assertIn("mod.py", tools["Glob"].handler({"pattern": "*.py"}))
        hits = tools["Grep"].handler({"pattern": "def foo", "path": self.dir})
        self.assertIn("def foo", hits)


if __name__ == "__main__":
    unittest.main()
