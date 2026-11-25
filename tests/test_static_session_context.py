"""Static session context: built once per session, injected at start/resume.

Regression origin: the repo map was rebuilt and re-injected on *every turn*.
Beyond the token cost of resending identical text, it re-scanned the repository
on every message. The map and the orientation block describe things that do not
change mid-session, so they are built at the session boundary (client creation)
and cached until the session ends.
"""
import unittest
from unittest import mock

import core.context.repo_map as repo_map
import runners.deepinfra as deepinfra
import runners.fireworks as fireworks
import runners.local as local

RUNNERS = (
    ("deepinfra", deepinfra),
    ("local", local),
    ("fireworks", fireworks),
)

WS = "/tmp/whatever/workspace"


def _runner(module):
    """A runner instance with only the state the method needs."""
    r = module.DeepInfraRunner.__new__(module.DeepInfraRunner) \
        if hasattr(module, "DeepInfraRunner") else None
    if r is None:
        cls = next(
            v for k, v in vars(module).items()
            if isinstance(v, type) and k.endswith("Runner")
        )
        r = cls.__new__(cls)
    r._session_static_context = {}
    return r


class StaticContextBuildTests(unittest.TestCase):
    def test_map_built_once_across_turns(self):
        for name, module in RUNNERS:
            with self.subTest(runner=name):
                r = _runner(module)
                with mock.patch.object(module, "ADDITIONAL_DIRS", ["/tmp/repo"]), \
                     mock.patch.object(module, "_cfg_repo_map_enabled", return_value=True), \
                     mock.patch.object(
                         repo_map, "build_repo_map_segment",
                         return_value="## Repository map (overview)\n# Repository map: repo",
                     ) as spy:
                    first = r._static_session_context("t1", WS)
                    second = r._static_session_context("t1", WS)
                    r._static_session_context("t1", WS)
                self.assertEqual(spy.call_count, 1, "map rebuilt between turns")
                self.assertEqual(first, second)

    def test_candidates_come_from_additional_dirs(self):
        for name, module in RUNNERS:
            with self.subTest(runner=name):
                r = _runner(module)
                with mock.patch.object(module, "ADDITIONAL_DIRS", ["/a/repo", "/b/repo"]), \
                     mock.patch.object(module, "_cfg_repo_map_enabled", return_value=True), \
                     mock.patch.object(repo_map, "build_repo_map_segment", return_value="") as spy:
                    r._static_session_context("t1", WS)
                self.assertEqual(
                    spy.call_args.kwargs.get("candidates"), ["/a/repo", "/b/repo"]
                )

    def test_orientation_present_without_repo_map(self):
        for name, module in RUNNERS:
            with self.subTest(runner=name):
                r = _runner(module)
                with mock.patch.object(module, "ADDITIONAL_DIRS", []), \
                     mock.patch.object(module, "_cfg_repo_map_enabled", return_value=False):
                    out = r._static_session_context("t1", WS)
                self.assertIn("Your starting directory", out)
                self.assertNotIn("Repository map", out)

    def test_orientation_survives_when_map_disabled(self):
        # Orientation must not be gated behind the repo-map flag.
        r = _runner(deepinfra)
        with mock.patch.object(deepinfra, "ADDITIONAL_DIRS", []), \
             mock.patch.object(deepinfra, "_cfg_repo_map_enabled", return_value=False):
            self.assertTrue(r._static_session_context("t1", WS).strip())

    def test_map_failure_yields_orientation_only(self):
        # A failing map build must not take the orientation block with it.
        for name, module in RUNNERS:
            with self.subTest(runner=name):
                r = _runner(module)
                with mock.patch.object(module, "ADDITIONAL_DIRS", []), \
                     mock.patch.object(module, "_cfg_repo_map_enabled", return_value=True), \
                     mock.patch.object(
                         repo_map, "build_repo_map_segment",
                         side_effect=RuntimeError("boom"),
                     ):
                    out = r._static_session_context("t1", WS)
                self.assertIn("Your starting directory", out)
                self.assertNotIn("Repository map", out)

    def test_flag_failure_is_contained(self):
        # The flag read is inside the guarded region too, so a broken config
        # lookup cannot break a turn.
        for name, module in RUNNERS:
            with self.subTest(runner=name):
                r = _runner(module)
                with mock.patch.object(module, "ADDITIONAL_DIRS", []), \
                     mock.patch.object(
                         module, "_cfg_repo_map_enabled",
                         side_effect=RuntimeError("boom"),
                     ):
                    out = r._static_session_context("t1", WS)
                self.assertIn("Your starting directory", out)

    def test_per_session_isolation(self):
        r = _runner(deepinfra)
        with mock.patch.object(deepinfra, "ADDITIONAL_DIRS", ["/tmp/repo"]), \
             mock.patch.object(deepinfra, "_cfg_repo_map_enabled", return_value=True), \
             mock.patch.object(
                 repo_map, "build_repo_map_segment", return_value="# Repository map: x"
             ) as spy:
            r._static_session_context("s1", WS)
            r._static_session_context("s2", WS)
        self.assertEqual(spy.call_count, 2)


if __name__ == "__main__":
    unittest.main()
