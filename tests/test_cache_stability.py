"""Tests for core.cache_stability — H13 prefix-stability measurement (offline)."""
import unittest

from core.cache_stability import (
    measure_prefix_stability,
    record_provider_usage,
    run_h13,
)


class TestPrefixStability(unittest.TestCase):
    def test_prefix_is_byte_stable_across_turns(self):
        # Appending turns must never change earlier serialized messages.
        script = ["alpha\n" * 400, "beta\n" * 400, "gamma\n" * 400]
        rep = measure_prefix_stability(script, enabled=True, dedup_enabled=False)
        # every turn after the first reuses a byte-identical leading prefix
        self.assertTrue(all(f == 1.0 for f in rep.prefix_reuse_fraction))

    def test_baseline_also_stable(self):
        script = ["x\n" * 400] * 3
        rep = measure_prefix_stability(script, enabled=False, dedup_enabled=False)
        self.assertTrue(all(f == 1.0 for f in rep.prefix_reuse_fraction))

    def test_dedup_does_not_rewrite_earlier_messages(self):
        # repeated identical reads: dedup shortens the *new* message only.
        text = "same bytes\n" * 400
        script = [text, text, text, text]
        rep = measure_prefix_stability(script, enabled=False, dedup_enabled=True)
        # Even though later duplicates become pointers, earlier messages never
        # change -> prefix fraction stays 1.0.
        self.assertTrue(all(f == 1.0 for f in rep.prefix_reuse_fraction))

    def test_record_provider_usage_attaches_live_data(self):
        rep = measure_prefix_stability(["z\n" * 400, "z\n" * 400], enabled=False, dedup_enabled=False)
        self.assertIsNone(rep.provider_cached_tokens)
        record_provider_usage(rep, [0, 500])
        self.assertEqual(rep.provider_cached_tokens, [0, 500])


class TestRunH13(unittest.TestCase):
    def test_all_variants_stable(self):
        r = run_h13()
        self.assertTrue(r["all_prefixes_stable"])
        for v in ("A", "B", "C"):
            self.assertIn(v, r["prefix_reuse_fraction"])
            for f in r["prefix_reuse_fraction"][v]:
                self.assertEqual(f, 1.0)

    def test_report_is_honest_about_scope(self):
        r = run_h13()
        self.assertIn("no provider claims", r["measured"])
        self.assertIn("reports", r)


if __name__ == "__main__":
    unittest.main()
