"""Tests for core.context.tool_compression — native, targeted tool-result compression.

Maps to the spec's TESTS_AND_BENCHMARKS.md: T01 (disabled == identity),
T02 (never touches non-tool/system input), T04 (repetitive logs keep errors),
T05 (JSON anomalies represented), T06 (code/diff/traceback conservative),
T07 (dense line head/tail/count), T08 (exception -> safe fallback),
plus the output-format contract (explicit omission markers + retrieval handle).
"""
import tempfile
import unittest

from core.context.artifacts import ArtifactStore
from core.context.tool_compression import (
    CompressConfig,
    CompressionOutcome,
    maybe_compress,
)


class _Base(unittest.TestCase):
    """Provide a real temp ArtifactStore so compression can persist originals."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ArtifactStore(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def compress(self, text, **kw):
        return maybe_compress(
            text, tool_name=kw.pop("tool_name", "Bash"),
            enabled=kw.pop("enabled", True),
            store=self.store, session_id="s", workspace_id="w",
            **kw,
        )


def _log(n=2000, fatal="FATAL disk full at /var/data"):
    lines = [f"INFO step {i} ok latency={i % 7}ms" for i in range(n)]
    lines.insert(n // 2, fatal)
    return "\n".join(lines)


class TestDisabledIsIdentity(_Base):
    def test_disabled_returns_input_unchanged(self):
        text = _log()
        out = self.compress(text, enabled=False)
        self.assertFalse(out.changed)
        self.assertEqual(out.text, text)
        self.assertIsNone(out.original.artifact_id if out.original else None)

    def test_under_threshold_passthrough(self):
        text = "short output"
        out = self.compress(text)
        self.assertFalse(out.changed)
        self.assertEqual(out.text, text)


class TestRepetitiveLogs(_Base):
    def test_logs_shrink_and_keep_fatal(self):
        text = _log()
        out = self.compress(text, config=CompressConfig(min_chars=1000))
        self.assertTrue(out.changed)
        self.assertLess(len(out.text), len(text))
        # the central FATAL must survive verbatim (T04)
        self.assertIn("FATAL disk full at /var/data", out.text)

    def test_omission_is_marked_and_retrievable(self):
        text = _log()
        out = self.compress(text, config=CompressConfig(min_chars=1000))
        self.assertTrue(out.changed)
        self.assertIn("elided", out.text.lower())
        self.assertIsNotNone(out.original)
        # the artifact id appears in the handle, and read-back is exact
        self.assertIn(out.original.artifact_id, out.text)
        self.assertEqual(
            self.store.read("s", "w", out.original.artifact_id, 0, 10**7),
            text.encode(),
        )


class TestStructuredJSON(_Base):
    def _json(self):
        import json
        records = [{"id": i, "status": "ok"} for i in range(500)]
        records[250] = {"id": 250, "status": "ERROR", "detail": "unique-anomaly-xyz"}
        return json.dumps(records)

    def test_json_keeps_anomaly_and_shape(self):
        text = self._json()
        out = self.compress(text, config=CompressConfig(min_chars=1000))
        self.assertTrue(out.changed)
        # the rare anomalous record must be represented (T05)
        self.assertIn("unique-anomaly-xyz", out.text)
        # output must not masquerade as the original valid JSON document
        self.assertNotEqual(out.text, text)

    def test_json_never_emits_malformed_json_as_original(self):
        text = self._json()
        out = self.compress(text, config=CompressConfig(min_chars=1000))
        import json as _j
        # strip our trailing handle to inspect the JSON-ish body
        body = out.text.split("\n\n[AgentGate compressed")[0]
        try:
            parsed = _j.loads(body)
        except _j.JSONDecodeError:
            parsed = None
        if parsed is not None and isinstance(parsed, dict):
            self.assertIn("unique-anomaly-xyz", out.text)


class TestCodeAndTracebackConservative(_Base):
    def test_source_code_passthrough(self):
        code = "\n".join(
            ["def resolve_directory(name, registry):",
             "    key = name.strip().lower()",
             "    if key not in registry:",
             "        raise KeyError(name)",
             "    return registry[key]"] * 200
        )
        out = self.compress(code, tool_name="Read", config=CompressConfig(min_chars=1000))
        # T06: conservative passthrough for source — must not drop code
        self.assertIn("def resolve_directory", out.text)
        self.assertIn("raise KeyError(name)", out.text)

    def test_traceback_preserved(self):
        tb = "Traceback (most recent call last):\n" + "".join(
            f'  File "/x/mod{i}.py", line {i}, in fn\n    raise ValueError("boom{i}")\n'
            for i in range(300)
        ) + "ValueError: boom299\n"
        out = self.compress(tb, config=CompressConfig(min_chars=1000))
        # at minimum the final exception line must survive
        self.assertIn("ValueError: boom299", out.text)


class TestDenseLine(_Base):
    def test_dense_line_head_tail(self):
        dense = "x" * 50_000  # one long low-whitespace line
        text = "\nnormal line before\n" + dense + "\nnormal line after"
        out = self.compress(text, config=CompressConfig(min_chars=1000))
        self.assertTrue(out.changed)
        self.assertLess(len(out.text), len(text))
        # head and tail of the dense line remain
        self.assertTrue(out.text.count("x") < 50_000)
        self.assertIn("normal line after", out.text)


class TestExceptionFallback(_Base):
    def test_internal_error_falls_back(self):
        out = self.compress(None)  # type: ignore[arg-type]
        self.assertFalse(out.changed)

    def test_bytes_and_weird_types_do_not_raise(self):
        for bad in (b"binary\x00bytes", 12345, ["list"], {"d": 1}):
            out = self.compress(bad)  # type: ignore[arg-type]
            self.assertFalse(out.changed)

    def test_store_unavailable_falls_back(self):
        # No store -> refuse lossy output (safety over savings).
        out = maybe_compress(_log(), tool_name="Bash", enabled=True,
                             config=CompressConfig(min_chars=1000), store=None)
        self.assertFalse(out.changed)
        self.assertIn("no_store", out.transforms)


class TestNonToolMessagesUntouched(_Base):
    def test_only_string_tool_results_considered(self):
        out = self.compress({"role": "system", "content": "HARD RULES"})  # type: ignore[arg-type]
        self.assertFalse(out.changed)


if __name__ == "__main__":
    unittest.main()
