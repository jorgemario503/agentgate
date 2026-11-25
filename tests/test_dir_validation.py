"""Tests for core.context.dir_validation — entry checks and rejection reasons.

The behaviour under test is that a bad ``ADDITIONAL_DIRS`` value is *refused
with a reason*, not silently skipped. Existence checks use an injected ``exists``
switch (or a real temp dir) so the tests do not depend on the host layout.
"""
import os
import tempfile
import unittest

from core.context.dir_validation import (
    ACCEPTED,
    DUPLICATE,
    EMPTY,
    FILESYSTEM_ROOT,
    HOME_TOO_BROAD,
    MISSING,
    RELATIVE,
    SHELL_FRAGMENT,
    check_additional_dirs,
    describe_problems,
    usable_dirs,
)

HOME = "/Users/me"


def _statuses(entries, **kw):
    kw.setdefault("home", HOME)
    kw.setdefault("exists", False)
    return [(c.status, c.raw) for c in check_additional_dirs(entries, **kw)]


class FormatTests(unittest.TestCase):
    def test_absolute_path_accepted(self):
        self.assertEqual(_statuses(["/Users/me/dev/komper"]),
                         [(ACCEPTED, "/Users/me/dev/komper")])

    def test_relative_refused(self):
        self.assertEqual(_statuses(["dev/komper"]), [(RELATIVE, "dev/komper")])

    def test_filesystem_root_refused(self):
        self.assertEqual(_statuses(["/"]), [(FILESYSTEM_ROOT, "/")])

    def test_home_refused(self):
        self.assertEqual(_statuses([HOME]), [(HOME_TOO_BROAD, HOME)])

    def test_home_refused_with_trailing_slash(self):
        self.assertEqual(_statuses([HOME + "/"]), [(HOME_TOO_BROAD, HOME + "/")])

    def test_home_is_not_confused_with_a_subdirectory(self):
        status, _ = _statuses([HOME + "/dev/komper"])[0]
        self.assertEqual(status, ACCEPTED)

    def test_subdirectory_of_home_allowed(self):
        self.assertEqual(_statuses(["/Users/me/dev"]), [(ACCEPTED, "/Users/me/dev")])

    def test_shell_fragment_refused(self):
        for bad in ("/Users/me/dev/*", "/Users/me/$X", "/Users/me/a|b"):
            status, _ = _statuses([bad])[0]
            self.assertEqual(status, SHELL_FRAGMENT, bad)

    def test_empty_entry_refused(self):
        self.assertEqual(_statuses([""])[0][0], EMPTY)

    def test_duplicate_refused(self):
        got = _statuses(["/Users/me/dev/a", "/Users/me/dev/a"])
        self.assertEqual([s for s, _ in got], [ACCEPTED, DUPLICATE])

    def test_equivalent_paths_are_duplicates(self):
        got = _statuses(["/Users/me/dev/a", "/Users/me/dev/./a"])
        self.assertEqual([s for s, _ in got], [ACCEPTED, DUPLICATE])

    def test_order_preserved(self):
        got = _statuses(["/b/x", "/a/y"])
        self.assertEqual([raw for _, raw in got], ["/b/x", "/a/y"])

    def test_none_is_survivable(self):
        self.assertEqual(check_additional_dirs(None, home=HOME, exists=False), [])


class ExistenceTests(unittest.TestCase):
    def test_missing_directory_is_usable_but_reported(self):
        with tempfile.TemporaryDirectory() as root:
            gone = os.path.join(root, "nope")
            checks = check_additional_dirs([gone], home=HOME)
            self.assertEqual(checks[0].status, MISSING)
            # Usable: it may be created later.
            self.assertTrue(checks[0].usable)
            self.assertEqual(usable_dirs(checks), [gone])

    def test_existing_directory_accepted(self):
        with tempfile.TemporaryDirectory() as root:
            real = os.path.join(root, "repo")
            os.mkdir(real)
            checks = check_additional_dirs([real], home=HOME)
            self.assertEqual(checks[0].status, ACCEPTED)
            self.assertEqual(describe_problems(checks), [])

    def test_refused_entries_are_not_usable(self):
        checks = check_additional_dirs(["relative", "/"], home=HOME, exists=False)
        self.assertEqual(usable_dirs(checks), [])


class ReportTests(unittest.TestCase):
    def test_every_problem_has_a_reason_quoting_the_value(self):
        checks = check_additional_dirs(
            ["dev/x", "/", HOME, "/Users/me/dev/*", ""], home=HOME, exists=False
        )
        lines = describe_problems(checks)
        self.assertEqual(len(lines), 5)
        for line in lines:
            self.assertIn("ADDITIONAL_DIRS", line)
            self.assertTrue(line.split(":", 1)[-1].strip(), line)

    def test_missing_is_a_warning_not_a_rejection(self):
        with tempfile.TemporaryDirectory() as root:
            line = describe_problems(
                check_additional_dirs([os.path.join(root, "gone")], home=HOME)
            )[0]
        self.assertIn("[warning]", line)

    def test_refused_is_labelled_ignored(self):
        line = describe_problems(
            check_additional_dirs(["relative"], home=HOME, exists=False)
        )[0]
        self.assertIn("[ignored]", line)

    def test_no_problems_no_lines(self):
        self.assertEqual(
            describe_problems(check_additional_dirs(["/tmp"], home=HOME, exists=False)),
            [],
        )


if __name__ == "__main__":
    unittest.main()
