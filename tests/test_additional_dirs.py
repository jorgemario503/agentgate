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

    def test_relative_paths_are_not_silently_absolutised(self):
        # Kept verbatim: the operator gets to see their mistake rather than have
        # the path resolved against an unrelated process cwd.
        self.assertEqual(_parse("relative/dir"), ["relative/dir"])


if __name__ == "__main__":
    unittest.main()
