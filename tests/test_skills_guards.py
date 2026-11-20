"""Guard tests: skills must not touch Claude or AgentDB (S17), and disabled
mode must not alter tool compression or approval behavior (S18)."""
import inspect
import unittest

import runners.claude as claude_mod
import core.agentdb as agentdb_mod


class TestNoClaudeOrAgentDBSurface(unittest.TestCase):
    def test_s17_claude_runner_has_no_skill_refs(self):
        src = inspect.getsource(claude_mod)
        self.assertNotIn("skills_context", src)
        self.assertNotIn("skill_read", src)

    def test_s17_agentdb_surface_untouched(self):
        src = inspect.getsource(agentdb_mod)
        self.assertNotIn("skill", src.lower())


class TestDisabledRegistryParity(unittest.TestCase):
    def test_s18_registry_identical_without_catalog(self):
        import tempfile
        from core.tools.registry import build_tools

        class S:
            def lookup_directory(self, n): return None
            def remember_directory(self, n, p): pass

        tmp = tempfile.mkdtemp()
        a = build_tools(tmp, S(), output_cap=8000, bash_timeout=120)
        b = build_tools(tmp, S(), output_cap=8000, bash_timeout=120, skills_catalog=None)
        self.assertEqual(set(a), set(b))
        # Same schema shape for every core tool.
        for name in a:
            self.assertEqual(a[name].schema, b[name].schema)


if __name__ == "__main__":
    unittest.main()
