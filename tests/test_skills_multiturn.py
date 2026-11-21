"""Multi-turn / compaction / resume validation for skill activation (spec §7).

Deterministic, offline. Exercises SkillSession + the client's outgoing-message
builder without network calls.
"""
import tempfile
import unittest
from pathlib import Path

import core.skills as skills
from runners.local import LocalAgentClient


def _skill(root, name, body="BODY"):
    d = Path(root) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n\n{body}")


def _client(tmp):
    ctx = skills.build_skills_context(
        enabled=True, skills_dir=tmp, manifest_tokens=400, selection_mode="model")
    sess = skills.SkillSession(ctx.catalog)
    return LocalAgentClient(name="t", base_url="http://localhost:1/v1", model="m",
                            session_id="s", skills_context=ctx, skill_session=sess)


class TestMultiTurn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _skill(self.tmp, "s", body="BODY-TEXT-UNIQUE")
        self.c = _client(self.tmp)
        self.cat = self.c._skills_context.catalog

    def _out_text(self):
        return "\n".join(str(m.get("content", "")) for m in self.c._build_outgoing_messages())

    def test_1_activation_then_tool_loop_no_duplicate(self):
        # Body present from a skill_read tool result; never duplicated.
        self.c._skill_turn = 1
        self.c._skill_session.record_activation("s", self.cat.get("s").fingerprint, 1)
        self.c.messages = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "go"},
            {"role": "tool", "content": "BODY-TEXT-UNIQUE"},
        ]
        out = self._out_text()
        self.assertEqual(out.count("BODY-TEXT-UNIQUE"), 1)
        self.assertNotIn(skills.RESTORE_LABEL, out)

    def test_2_same_task_continuation_restores_once(self):
        self.c._skill_turn = 2  # still same task (activated turn 2)
        self.c._skill_session.record_activation("s", self.cat.get("s").fingerprint, 2)
        # Body was trimmed away.
        self.c.messages = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "continue same task"},
        ]
        out_msgs = self.c._build_outgoing_messages()
        out = "\n".join(str(m.get("content", "")) for m in out_msgs)
        self.assertIn("BODY-TEXT-UNIQUE", out)
        restores = [m for m in out_msgs
                    if skills.RESTORE_LABEL in str(m.get("content", ""))]
        self.assertTrue(restores)
        self.assertTrue(all(m["role"] == "user" for m in restores))

    def test_3_compaction_during_task_restores(self):
        # Same as 2: trimming (compaction) during the active task turn restores.
        self.c._skill_turn = 1
        self.c._skill_session.record_activation("s", self.cat.get("s").fingerprint, 1)
        self.c.messages = [{"role": "system", "content": "SYS"},
                           {"role": "user", "content": "x"}]
        self.assertIn("BODY-TEXT-UNIQUE", self._out_text())

    def test_4_compaction_before_later_continuation_no_auto_restore(self):
        # Activated in turn 1; the next user turn (2) is treated as a new task.
        self.c._skill_turn = 2
        self.c._skill_session.record_activation("s", self.cat.get("s").fingerprint, 1)
        self.c.messages = [{"role": "system", "content": "SYS"},
                           {"role": "user", "content": "new task"}]
        self.assertNotIn("BODY-TEXT-UNIQUE", self._out_text())

    def test_5_explicit_task_switch_no_restore(self):
        self.c._skill_turn = 3
        self.c._skill_session.record_activation("s", self.cat.get("s").fingerprint, 1)
        self.c.messages = [{"role": "system", "content": "SYS"},
                           {"role": "user", "content": "something else"}]
        self.assertNotIn("BODY-TEXT-UNIQUE", self._out_text())

    def test_6_cold_resume_no_invented_state(self):
        # A fresh session (fresh SkillSession) has no active skills → no restore.
        fresh = _client(self.tmp)
        fresh.messages = [{"role": "system", "content": "SYS"},
                          {"role": "user", "content": "resume"}]
        out = "\n".join(str(m.get("content", ""))
                        for m in fresh._build_outgoing_messages())
        self.assertNotIn("BODY-TEXT-UNIQUE", out)

    def test_7_updated_contents_not_substituted(self):
        self.c._skill_turn = 1
        self.c._skill_session.record_activation("s", self.cat.get("s").fingerprint, 1)
        # Change the file on disk AFTER activation.
        (Path(self.tmp) / "s" / "SKILL.md").write_text(
            "---\nname: s\ndescription: d\n---\n\nCHANGED-BODY")
        self.c.messages = [{"role": "system", "content": "SYS"},
                           {"role": "user", "content": "x"}]
        out = self._out_text()
        # Fingerprint mismatch → do NOT inject the changed body.
        self.assertNotIn("CHANGED-BODY", out)
        self.assertNotIn("BODY-TEXT-UNIQUE", out)

    def test_8_unrelated_request_after_activation(self):
        self.c._skill_turn = 2
        self.c._skill_session.record_activation("s", self.cat.get("s").fingerprint, 1)
        self.c.messages = [{"role": "system", "content": "SYS"},
                           {"role": "user", "content": "unrelated"}]
        self.assertNotIn("BODY-TEXT-UNIQUE", self._out_text())


if __name__ == "__main__":
    unittest.main()
