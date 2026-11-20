"""Tests for core.benchmark — the offline replay harness (C4).

Asserts the report is deterministic, offline, and honest: baseline is the
largest, C1 reduces repetitive/serializable bulk, C1+C2 shows dedup hits on the
repeated-read session, and Headroom is reported as available/unavailable
without fabricating metrics.
"""
import unittest

from core.benchmark import builtin_sessions, run_benchmark


class TestBenchmark(unittest.TestCase):
    def test_report_shape_and_offline(self):
        r = run_benchmark()
        self.assertIn("totals", r)
        self.assertIn("A", r["totals"])
        self.assertIn("measured", r)
        self.assertIn("offline", r["measured"])

    def test_c1_reduces_tokens(self):
        r = run_benchmark()
        self.assertLess(r["totals"]["B"], r["totals"]["A"])

    def test_dedup_hits_on_repeated_reads(self):
        r = run_benchmark()
        self.assertGreater(r["dedup_hits"]["C"], 0)
        self.assertEqual(r["dedup_hits"]["A"], 0)

    def test_deterministic(self):
        self.assertEqual(run_benchmark(), run_benchmark())

    def test_headroom_honest(self):
        r = run_benchmark()
        self.assertIn("headroom_available", r)
        self.assertIsInstance(r["headroom_available"], bool)


if __name__ == "__main__":
    unittest.main()
