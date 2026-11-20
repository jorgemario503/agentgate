"""Live task-correctness probe: budget-guard unit tests + opt-in live test.

The budget-guard tests always run (no network). The live test is skipped unless
AGENTGATE_LIVE_TESTS=1, so normal `pytest` runs never spend tokens or hit the
network. When enabled it uses the same DEEPINFRA_* env vars / model the runner
uses and a small token budget.
"""
import os
import unittest

from core.live_task_check import (
    BudgetExceeded,
    TokenBudget,
    live_enabled,
    run_live_cache_reuse,
    run_multi_case,
    run_task_correctness,
    select_models,
    skip_reason_error,
)


class TestTokenBudgetGuard(unittest.TestCase):
    def test_first_call_guarded_when_budget_too_small(self):
        b = TokenBudget(limit=100)
        # worst-case prompt+completion far exceeds the 100-token budget
        with self.assertRaises(BudgetExceeded):
            b.precheck(est_prompt_tokens=500, max_completion=200)

    def test_call_allowed_within_budget(self):
        b = TokenBudget(limit=1000)
        b.precheck(est_prompt_tokens=200, max_completion=200)  # 400 <= 1000
        self.assertEqual(b.remaining, 1000)

    def test_record_reduces_remaining(self):
        b = TokenBudget(limit=1000)
        b.record({"total_tokens": 300})
        self.assertEqual(b.used, 300)
        self.assertEqual(b.remaining, 700)
        self.assertEqual(b.calls, 1)

    def test_record_prompt_plus_completion_when_no_total(self):
        b = TokenBudget(limit=1000)
        b.record({"prompt_tokens": 120, "completion_tokens": 30})
        self.assertEqual(b.used, 150)

    def test_record_without_usage_charges_cap(self):
        b = TokenBudget(limit=1000)
        b.record(None)
        self.assertGreater(b.used, 0)

    def test_subsequent_call_blocked_when_remaining_low(self):
        b = TokenBudget(limit=500)
        b.record({"total_tokens": 400})       # remaining 100
        with self.assertRaises(BudgetExceeded):
            b.precheck(est_prompt_tokens=200, max_completion=200)


class TestLiveGate(unittest.TestCase):
    def test_disabled_by_default(self):
        # With the flag unset, the run refuses (the live test below turns it on).
        if os.environ.get("AGENTGATE_LIVE_TESTS"):
            self.skipTest("live flag set in env; gate test not applicable")
        self.assertFalse(live_enabled())
        with self.assertRaises(skip_reason_error):
            run_task_correctness()


@unittest.skipUnless(
    os.environ.get("AGENTGATE_LIVE_TESTS", "").strip() in ("1", "true", "yes", "on"),
    "set AGENTGATE_LIVE_TESTS=1 to run the live DeepInfra task-correctness probe",
)
class TestLiveTaskCorrectness(unittest.TestCase):
    """Real provider call — opt-in, budget-guarded, small token footprint."""

    def test_compressed_answer_and_exact_retrieval(self):
        budget = int(os.environ.get("AGENTGATE_LIVE_TOKEN_BUDGET", "6000"))
        report = run_task_correctness(budget=budget)

        # Budget must never be exceeded.
        self.assertLessEqual(report["used_tokens"], report["budget_limit"])

        by_name = {c["name"]: c for c in report["cases"]}
        # Retrieval path must be exact (offline-deterministic, no tokens).
        self.assertIn("retrieval_exact", by_name)
        self.assertTrue(by_name["retrieval_exact"]["exact_match"])

        # If we actually spent a call on the model, the compressed message must
        # have carried the FATAL fact well enough to answer it correctly.
        if "answer_from_compressed" in by_name:
            self.assertTrue(by_name["answer_from_compressed"]["artifact_id_present"])
            self.assertTrue(
                by_name["answer_from_compressed"]["correct"],
                msg=f"model answer: {by_name['answer_from_compressed']['answer']!r}",
            )
        else:
            self.skipTest(f"case skipped under budget: {report.get('skipped')}")


@unittest.skipUnless(
    os.environ.get("AGENTGATE_LIVE_TESTS", "").strip() in ("1", "true", "yes", "on"),
    "set AGENTGATE_LIVE_TESTS=1 to run the live DeepInfra cache-reuse probe",
)
class TestLiveCacheReuse(unittest.TestCase):
    def test_repeated_prefix_reports_cached_tokens(self):
        budget = int(os.environ.get("AGENTGATE_LIVE_TOKEN_BUDGET", "6000"))
        report = run_live_cache_reuse(budget=budget)
        self.assertLessEqual(report["used_tokens"], report["budget_limit"])
        # We do not *require* caching (an implementation detail of the provider);
        # we only require the measurement to be well-formed and honest.
        self.assertIn("cache_reuse_observed", report)
        if len(report["turns"]) == 2:
            self.assertIn("cached_tokens", report["turns"][1])


class TestSelectionAndCases(unittest.TestCase):
    def test_select_models_registry_validated(self):
        from core.reasoning.registry import lookup
        models = select_models(limit=3)
        self.assertTrue(models)
        for m in models:
            self.assertIsNotNone(lookup("deepinfra", m))

    def test_select_models_respects_limit(self):
        self.assertLessEqual(len(select_models(limit=2)), 2)

    def test_cases_have_needle_and_question(self):
        from core.live_task_check import _cases
        cases = _cases()
        self.assertTrue(cases)
        for name, full, needle, question in cases:
            self.assertTrue(needle and question)
            # every needle must be recoverable from the ORIGINAL text
            self.assertIn(needle, full)


@unittest.skipUnless(
    os.environ.get("AGENTGATE_LIVE_TESTS", "").strip() in ("1", "true", "yes", "on"),
    "set AGENTGATE_LIVE_TESTS=1 to run the live multi-case DeepInfra suite",
)
class TestLiveMultiCase(unittest.TestCase):
    def test_multi_case_across_models(self):
        budget = int(os.environ.get("AGENTGATE_LIVE_TOKEN_BUDGET", "6000"))
        report = run_multi_case(budget=budget, max_models=3)
        # budget never exceeded
        self.assertLessEqual(report["used_tokens"], report["budget_limit"])
        self.assertTrue(report["results"] or report["skipped"])
        # every completed case must be correct (no destroyed content)
        for r in report["results"]:
            if "error" in r:
                continue
            self.assertTrue(
                r["correct"],
                msg="%s/%s wrong: %r" % (r["model"], r["case"], r["answer"]),
            )


if __name__ == "__main__":
    unittest.main()
