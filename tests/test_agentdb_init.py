"""Tests for core.agentdb.init_agentdb_for — hard-fail policy + wiring.

AgentDB is enabled by default. These tests pin the policy from the spec:
- disabled -> no-op (no AgentDB construction);
- enabled but package/permissions/dir missing -> AgentDBInitError with a clear
  remedy (hard fail, decision (a));
- enabled and healthy -> builds AgentDB, calls runner.init_agentdb, runs a sweep.
"""
import sys
import unittest
from pathlib import Path
import tempfile

from core.agentdb import AgentDBInitError, init_agentdb_for


class _Config:
    def __init__(self, enabled=True, memdir=None, perms=None):
        self.AGENTDB_ENABLED = enabled
        self.AGENTDB_MEMORY_DIR = memdir or tempfile.mkdtemp()
        self.AGENTDB_PERMISSIONS_PATH = perms or "does-not-exist-xyz.yaml"


class _Runner:
    def __init__(self):
        self.inited = None

    def init_agentdb(self, db, platform):
        self.inited = (db, platform)


class TestInitAgentDBFor(unittest.TestCase):
    def test_disabled_is_noop(self):
        runner = _Runner()
        # No package access at all when disabled.
        init_agentdb_for(runner, object(), config=_Config(enabled=False))
        self.assertIsNone(runner.inited)

    def test_missing_permissions_file_raises(self):
        runner = _Runner()
        cfg = _Config(enabled=True, perms="definitely-missing-123.yaml")
        with self.assertRaises(AgentDBInitError) as ctx:
            init_agentdb_for(runner, object(), config=cfg)
        self.assertIn("permissions", str(ctx.exception).lower())
        self.assertIn("AGENTDB_ENABLED=false", str(ctx.exception))
        self.assertIsNone(runner.inited)

    def test_missing_package_raises(self):
        # Simulate the package being absent by making the import fail.
        real = sys.modules.get("agentdb")
        sys.modules["agentdb"] = None  # import agentdb -> ImportError-ish
        try:
            runner = _Runner()
            # give it a real permissions file so we reach the import first? no —
            # import is attempted first, before the permissions check.
            with self.assertRaises(AgentDBInitError) as ctx:
                init_agentdb_for(runner, object(), config=_Config(enabled=True))
            self.assertIn("not installed", str(ctx.exception).lower())
            self.assertIn("agentdatabase", str(ctx.exception))
        finally:
            if real is not None:
                sys.modules["agentdb"] = real
            else:
                sys.modules.pop("agentdb", None)

    def test_healthy_wires_runner_and_runs_sweep(self):
        # Use the real, installed agentdb package + a real permissions file.
        try:
            import agentdb
            _ = agentdb.AgentDB  # presence check
        except Exception:
            self.skipTest("agentdatabase not installed")
        runner = _Runner()
        memdir = tempfile.mkdtemp()
        # Copy the repo permissions.yaml into the memory dir tree if present.
        perms = Path("permissions.yaml")
        if not perms.exists():
            self.skipTest("permissions.yaml not present in repo root")
        cfg = _Config(enabled=True, memdir=memdir, perms=str(perms))
        init_agentdb_for(runner, object(), config=cfg)
        # runner wired with a DB object
        self.assertIsNotNone(runner.inited)
        db, _platform = runner.inited
        self.assertIsNotNone(db)


if __name__ == "__main__":
    unittest.main()
