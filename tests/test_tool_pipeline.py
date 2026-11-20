"""Tests for core.tool_pipeline — the shared runner adapter (T01, T08, T12, T18)."""
import tempfile
import unittest

from core.tool_pipeline import ToolResultPipeline


def _log(n=2000):
    lines = [f"INFO step {i} ok latency={i % 7}ms" for i in range(n)]
    lines.insert(n // 2, "FATAL disk full at /var/data")
    return "\n".join(lines)


class TestPipelineDisabled(unittest.TestCase):
    def test_disabled_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as d:
            p = ToolResultPipeline(d, session_id="s", workspace_id="w", enabled=False,
                                   min_chars=1000)
            text = _log()
            self.assertEqual(p.process(text, tool_name="Bash"), text)

    def test_disabled_no_artifact_written(self):
        with tempfile.TemporaryDirectory() as d:
            p = ToolResultPipeline(d, session_id="s", workspace_id="w", enabled=False,
                                   min_chars=1000)
            p.process(_log(), tool_name="Bash")
            # no artifacts directory populated
            arts = list((__import__("pathlib").Path(d) / "_artifacts").glob("**/*"))
            self.assertEqual([x for x in arts if x.is_file()], [])


class TestPipelineEnabled(unittest.TestCase):
    def test_enabled_compresses_and_is_retrievable(self):
        with tempfile.TemporaryDirectory() as d:
            p = ToolResultPipeline(d, session_id="s", workspace_id="w", enabled=True,
                                   min_chars=1000)
            text = _log()
            out = p.process(text, tool_name="Bash")
            self.assertNotEqual(out, text)
            self.assertLess(len(out), len(text))
            self.assertIn("FATAL disk full at /var/data", out)
            # extract the id from the handle and read the original back
            import re
            m = re.search(r"original=([0-9a-f]{64})", out)
            self.assertIsNotNone(m)
            back = p.read_original(m.group(1), 0, 10**7)
            self.assertEqual(back, text)

    def test_small_result_untouched_even_when_enabled(self):
        with tempfile.TemporaryDirectory() as d:
            p = ToolResultPipeline(d, session_id="s", workspace_id="w", enabled=True,
                                   min_chars=1000)
            small = "just a short result"
            self.assertEqual(p.process(small, tool_name="Bash"), small)

    def test_never_raises_on_bad_input(self):
        with tempfile.TemporaryDirectory() as d:
            p = ToolResultPipeline(d, session_id="s", workspace_id="w", enabled=True,
                                   min_chars=1000)
            self.assertIsNone(p.process(None, tool_name="Bash"))


class TestPipelineScopeIsolation(unittest.TestCase):
    def test_other_session_cannot_read(self):
        with tempfile.TemporaryDirectory() as d:
            p = ToolResultPipeline(d, session_id="A", workspace_id="w", enabled=True,
                                   min_chars=1000)
            import re
            out = p.process(_log(), tool_name="Bash")
            aid = re.search(r"original=([0-9a-f]{64})", out).group(1)
            q = ToolResultPipeline(d, session_id="B", workspace_id="w", enabled=True,
                                   min_chars=1000)
            with self.assertRaises(PermissionError):
                q.read_original(aid)


if __name__ == "__main__":
    unittest.main()
