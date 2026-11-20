"""Tests for core.artifacts — bounded, session/workspace-scoped original store.

These correspond to the spec's T11 (restart), T12 (cross-session isolation),
T13 (path security) and T18 (bounded reads). The store must:
  - persist across process restarts (re-open the same directory),
  - refuse reads from a different session/workspace,
  - reject traversal / symlink / malformed IDs,
  - support deterministic bounded offset/limit reads.
"""
import os
import tempfile
import unittest
from pathlib import Path

from core.artifacts import ArtifactStore


class TestArtifactStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = ArtifactStore(self.base)

    def tearDown(self):
        self._tmp.cleanup()

    # --- round trip -----------------------------------------------------
    def test_put_then_read_exact(self):
        data = b"hello\nworld\nFATAL boom\n"
        ref = self.store.put("sess1", "ws1", data)
        out = self.store.read("sess1", "ws1", ref.artifact_id, offset=0, limit=10_000)
        self.assertEqual(out, data)

    def test_ref_fields(self):
        data = b"abcdef"
        ref = self.store.put("s", "w", data)
        self.assertEqual(ref.byte_length, 6)
        self.assertEqual(len(ref.sha256), 64)
        self.assertEqual((ref.session_id, ref.workspace_id), ("s", "w"))
        # opaque id must not embed the raw content or an absolute path
        self.assertNotIn("abcdef", ref.artifact_id)
        self.assertNotIn(str(self.base), ref.artifact_id)

    # --- bounded reads (T18) -------------------------------------------
    def test_bounded_offset_limit(self):
        data = b"0123456789"
        ref = self.store.put("s", "w", data)
        self.assertEqual(self.store.read("s", "w", ref.artifact_id, 2, 3), b"234")
        # limit beyond end clamps
        self.assertEqual(self.store.read("s", "w", ref.artifact_id, 8, 100), b"89")
        # offset beyond end -> empty
        self.assertEqual(self.store.read("s", "w", ref.artifact_id, 100, 10), b"")

    def test_negative_offset_rejected(self):
        ref = self.store.put("s", "w", b"xyz")
        with self.assertRaises(ValueError):
            self.store.read("s", "w", ref.artifact_id, -1, 5)

    # --- restart persistence (T11) -------------------------------------
    def test_restart_persists(self):
        data = b"persist me across restart\n"
        ref = self.store.put("s", "w", data)
        # simulate a new process: brand-new store over the same base dir
        store2 = ArtifactStore(self.base)
        self.assertEqual(store2.read("s", "w", ref.artifact_id, 0, 10_000), data)

    # --- cross-session / workspace isolation (T12) ---------------------
    def test_foreign_session_denied(self):
        ref = self.store.put("sessA", "wsA", b"secretA")
        with self.assertRaises(PermissionError):
            self.store.read("sessB", "wsA", ref.artifact_id, 0, 100)

    def test_foreign_workspace_denied(self):
        ref = self.store.put("sessA", "wsA", b"data")
        with self.assertRaises(PermissionError):
            self.store.read("sessA", "wsB", ref.artifact_id, 0, 100)

    def test_unknown_id_denied(self):
        with self.assertRaises(PermissionError):
            self.store.read("s", "w", "deadbeef" * 8, 0, 100)

    # --- path security (T13) -------------------------------------------
    def test_traversal_id_rejected(self):
        for bad in ("../etc/passwd", "a/b", "..", ".", "x/../../y", ""):
            with self.assertRaises((PermissionError, ValueError)):
                self.store.read("s", "w", bad, 0, 100)

    def test_no_symlink_escape(self):
        ref = self.store.put("s", "w", b"real")
        # The real scope dir is opaque (hashed); create a symlink at an
        # artifact-shaped name inside it and point it outside the tree.
        scope_dir = self.store._scope_dir("s", "w")
        target = self.base / "outside.txt"
        target.write_bytes(b"OUTSIDE")
        link = scope_dir / ("f" * 64)
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(PermissionError):
            self.store.read("s", "w", "f" * 64, 0, 100)

    def test_ids_are_deterministic_stable(self):
        # same content + scope -> same id (content-addressed, deterministic)
        a = self.store.put("s", "w", b"same")
        b = self.store.put("s", "w", b"same")
        self.assertEqual(a.artifact_id, b.artifact_id)


if __name__ == "__main__":
    unittest.main()
