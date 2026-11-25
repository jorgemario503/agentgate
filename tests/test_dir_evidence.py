"""Tests for core.context.dir_evidence — directory evidence from a turn's text.

Regression origin: the previous extractor was a bare regex over free text, so
any slash-shaped fragment became a "session directory" and was re-injected into
the model's context every turn. A real session reported all of, verbatim::

    /agent-sessions/  /Users/jm/.../  /context/  /.../  (and similar)

Tests use an injected ``exists`` predicate so they never depend on the host
filesystem.
"""
import os
import tempfile
import unittest
from pathlib import Path

from core.context.dir_evidence import (
    MAX_DIRS,
    extract_existing_dirs,
    prune_existing_dirs,
)


def _exists_among(*paths: str):
    """Predicate accepting exactly ``paths`` (compared path-normalised)."""
    allowed = {os.path.normpath(p) for p in paths}
    return lambda p: os.path.normpath(p) in allowed


class PhantomDirectoryRegressionTests(unittest.TestCase):
    """The exact strings a real session produced must not survive."""

    NOISE = (
        "Session directories:\n"
        "  /agent-sessions/ (this session)\n"
        "  /agentgate/agent-sessions/slack/1795.../ (this session)\n"
        "  /Users/jm/.../ (this session)\n"
        "  /context/ (this session)\n"
        "  /.../ (this session)\n"
    )

    def test_no_invented_directory_survives(self):
        # The guarantee is that nothing *non-existent* is reported. That is
        # enforced by the existence check, so drive it with a predicate that
        # rejects everything, as a fresh machine would for those paths.
        self.assertEqual(extract_existing_dirs(self.NOISE, exists=lambda p: False), [])

    def test_leading_slash_fragment_is_not_promoted(self):
        # "/agent-sessions/" is a relative path quoted with a leading slash.
        # It must not become a real directory at the filesystem root.
        got = extract_existing_dirs("/agent-sessions/", exists=lambda p: True)
        self.assertNotIn("/agent-sessions", got)

    def test_ellipsis_marker_rejected(self):
        got = extract_existing_dirs(
            "see /Users/jm/.../ and /.../", exists=lambda p: True
        )
        self.assertEqual(got, [])

    def test_traversal_segments_rejected(self):
        got = extract_existing_dirs("/a/../b/", exists=lambda p: True)
        self.assertEqual(got, [])

    def test_mention_without_existing_directory_dropped(self):
        got = extract_existing_dirs("/Users/jm/ghost/", exists=lambda p: False)
        self.assertEqual(got, [])

    def test_real_absolute_directory_kept(self):
        got = extract_existing_dirs(
            "I am working in /Users/jm/Documents/github/komper now.",
            exists=_exists_among("/Users/jm/Documents/github/komper"),
        )
        self.assertEqual(got, ["/Users/jm/Documents/github/komper"])

    def test_relative_path_resolved_against_workspace(self):
        # "core/context/" written as "/context/" is resolved under the
        # workspace, not at the filesystem root.
        with tempfile.TemporaryDirectory() as ws:
            want = os.path.join(ws, "context")
            os.mkdir(want)
            got = extract_existing_dirs(
                "see /context/ for details", workspace_path=ws
            )
            self.assertEqual(got, [want])


class BoundingTests(unittest.TestCase):
    def test_duplicates_collapsed(self):
        got = extract_existing_dirs(
            "/tmp/a/ /tmp/a/ /tmp/a/", exists=lambda p: True
        )
        self.assertEqual(got, ["/tmp/a"])

    def test_output_is_capped(self):
        text = " ".join(f"/tmp/d{i}/" for i in range(50))
        got = extract_existing_dirs(text, exists=lambda p: True)
        self.assertEqual(len(got), MAX_DIRS)

    def test_never_raises_on_hostile_input(self):
        self.assertEqual(extract_existing_dirs(None, exists=lambda p: True), [])
        self.assertEqual(extract_existing_dirs("", exists=lambda p: True), [])

    def test_existence_check_errors_are_skipped(self):
        def boom(_p):
            raise OSError("permission denied")

        self.assertEqual(extract_existing_dirs("/tmp/a/", exists=boom), [])


class RealFilesystemTests(unittest.TestCase):
    """One end-to-end pass against a real temp tree (no mocks)."""

    def test_only_existing_dirs_returned(self):
        with tempfile.TemporaryDirectory() as root:
            real = Path(root) / "komper"
            real.mkdir()
            ghost = Path(root) / "deleted-scratch"
            text = f"Working in {real}/ and earlier {ghost}/ — also /.../ here."
            got = extract_existing_dirs(text, workspace_path=root)
            self.assertEqual(got, [str(real)])


class PruneTests(unittest.TestCase):
    def test_drops_missing_and_relative(self):
        with tempfile.TemporaryDirectory() as root:
            real = Path(root) / "keep"
            real.mkdir()
            kept = prune_existing_dirs(
                [str(real), str(Path(root) / "gone"), "relative/path", ""]
            )
            self.assertEqual(kept, [str(real)])

    def test_keeps_everything_when_all_exist(self):
        with tempfile.TemporaryDirectory() as root:
            a = Path(root) / "a"
            b = Path(root) / "b"
            a.mkdir()
            b.mkdir()
            self.assertEqual(len(prune_existing_dirs([str(a), str(b)])), 2)

    def test_never_raises(self):
        self.assertEqual(prune_existing_dirs(None), [])


if __name__ == "__main__":
    unittest.main()
