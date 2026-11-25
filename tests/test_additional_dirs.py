"""Tests for ADDITIONAL_DIRS parsing in core.config.

`ADDITIONAL_DIRS` is read once at import from the environment, so these tests
exercise the parser directly with a patched environment and a reloaded module.
"""
import importlib
import os
import unittest
from unittest import mock


def _parse(raw: str) -> list[str]:
    """Reload core.config with ADDITIONAL_DIRS set to `raw`."""
    import core.config as cfg

    with mock.patch.dict(os.environ, {"ADDITIONAL_DIRS": raw}):
        return importlib.reload(cfg).ADDITIONAL_DIRS


class AdditionalDirsParsingTests(unittest.TestCase):
    def tearDown(self):
        # Leave the module in its normal, environment-derived state.
        import core.config as cfg

        importlib.reload(cfg)

    def test_empty_is_empty_list(self):
        self.assertEqual(_parse(""), [])
        self.assertEqual(_parse("   "), [])

    def test_single_absolute_path(self):
        self.assertEqual(_parse("/Users/me/code/thing"), ["/Users/me/code/thing"])

    def test_comma_space_form_is_trimmed(self):
        # The natural way to write a list: "a, b". Previously the second entry
        # kept its leading space, was not absolute, did not exist, and vanished
        # silently.
        self.assertEqual(
            _parse("/Users/me/a, /Users/me/b"),
            ["/Users/me/a", "/Users/me/b"],
        )

    def test_padded_single_path_is_trimmed(self):
        self.assertEqual(_parse("  /Users/me/a  "), ["/Users/me/a"])

    def test_tilde_is_expanded(self):
        self.assertEqual(
            _parse("~/code/thing"), [os.path.expanduser("~/code/thing")]
        )

    def test_empty_entries_ignored(self):
        self.assertEqual(_parse("/a,,/b,"), ["/a", "/b"])

    def test_relative_paths_are_refused_not_resolved(self):
        # Refused rather than resolved against an unrelated process cwd; the
        # report is what tells the operator, so silence is not an option.
        self.assertEqual(_parse("relative/dir"), [])

    def test_refused_entry_is_reported(self):
        import core.config as cfg

        with mock.patch.dict(os.environ, {"ADDITIONAL_DIRS": "relative/dir"}):
            cfg = importlib.reload(cfg)
            report = cfg.additional_dirs_report()
        self.assertEqual(cfg.ADDITIONAL_DIRS, [])
        self.assertEqual(len(report), 1)
        self.assertIn("relative/dir", report[0])
        self.assertIn("not an absolute path", report[0])

    def test_clean_entry_reports_nothing(self):
        import core.config as cfg

        with mock.patch.dict(os.environ, {"ADDITIONAL_DIRS": "/tmp"}):
            cfg = importlib.reload(cfg)
            self.assertEqual(cfg.ADDITIONAL_DIRS, ["/tmp"])
            self.assertEqual(cfg.additional_dirs_report(), [])


if __name__ == "__main__":
    unittest.main()
