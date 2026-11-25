"""Tests for core.context.orientation — starting directory + available paths."""
import unittest

from core.context.orientation import (
    ADDITIONAL_HEADING,
    WORKSPACE_HEADING,
    build_additional_dirs_block,
    build_orientation,
    build_workspace_line,
)

WS = "/Users/me/agent-sessions/slack/123/workspace"
REPO_A = "/Users/me/dev/komper"
REPO_B = "/Users/me/dev/api"


class WorkspaceLineTests(unittest.TestCase):
    def test_names_the_path_and_says_it_is_a_scratch_dir(self):
        line = build_workspace_line(WS)
        self.assertIn(WORKSPACE_HEADING, line)
        self.assertIn(WS, line)
        self.assertIn("scratch", line)
        # The core correction: do not assume the project lives here.
        self.assertIn("NOT the project", line)

    def test_empty_workspace_renders_nothing(self):
        self.assertEqual(build_workspace_line(""), "")
        self.assertEqual(build_workspace_line("   "), "")

    def test_no_volatile_values(self):
        # Prefix-cache safety: nothing that changes between turns.
        line = build_workspace_line(WS)
        for banned in ("turn", "timestamp", "20:", "Token"):
            self.assertNotIn(banned, line)


class AdditionalDirsBlockTests(unittest.TestCase):
    def test_empty_renders_nothing(self):
        self.assertEqual(build_additional_dirs_block([]), "")
        self.assertEqual(build_additional_dirs_block(None), "")

    def test_lists_each_dir(self):
        block = build_additional_dirs_block([REPO_A, REPO_B])
        self.assertIn(ADDITIONAL_HEADING, block)
        self.assertIn(REPO_A, block)
        self.assertIn(REPO_B, block)

    def test_distinguishes_from_starting_point(self):
        block = build_additional_dirs_block([REPO_A])
        self.assertIn("separate from your starting directory", block)

    def test_does_not_override_the_task(self):
        # Guards the failure mode where the agent "helpfully" switches repo.
        block = build_additional_dirs_block([REPO_A])
        self.assertIn("does not mean you should switch to it", block)
        self.assertIn("the user's instruction for this task decides", block)

    def test_relative_entries_dropped(self):
        block = build_additional_dirs_block(["dev/komper", REPO_A])
        self.assertIn(REPO_A, block)
        # Rendered as a bullet, not merely mentioned inside another path.
        self.assertNotIn("- dev/komper", block)

    def test_duplicates_collapsed(self):
        block = build_additional_dirs_block([REPO_A, REPO_A, "/Users/me/dev/../dev/komper"])
        self.assertEqual(block.count(f"- {REPO_A}"), 1)

    def test_tilde_expanded(self):
        block = build_additional_dirs_block(["~/dev/komper"])
        self.assertNotIn("~/dev/komper", block)


class OrientationTests(unittest.TestCase):
    def test_workspace_comes_before_additional_dirs(self):
        out = build_orientation(WS, [REPO_A])
        self.assertLess(out.index(WORKSPACE_HEADING), out.index(ADDITIONAL_HEADING))

    def test_workspace_only(self):
        out = build_orientation(WS, [])
        self.assertIn(WORKSPACE_HEADING, out)
        self.assertNotIn(ADDITIONAL_HEADING, out)

    def test_additional_only_when_no_workspace(self):
        out = build_orientation("", [REPO_A])
        self.assertNotIn(WORKSPACE_HEADING, out)
        self.assertIn(ADDITIONAL_HEADING, out)

    def test_neither_renders_empty(self):
        self.assertEqual(build_orientation("", []), "")

    def test_both_present_is_one_block(self):
        out = build_orientation(WS, [REPO_A, REPO_B])
        self.assertIn(WS, out)
        self.assertIn(REPO_A, out)
        self.assertIn(REPO_B, out)

    def test_never_raises(self):
        self.assertEqual(build_orientation(None, None), "")
        self.assertEqual(build_orientation(WS, ["", None, 5]), build_workspace_line(WS))


if __name__ == "__main__":
    unittest.main()
