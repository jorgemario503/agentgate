"""Runner-level integration tests for open-model Agent Skills.

These inspect the *actual outgoing message arrays* the local/open clients
build (S10/S11/S15 etc.) and verify the disabled path leaves them unchanged.
They use LocalAgentClient (representative of all three runners, which share the
same _build_outgoing_messages + tool-loop shape) without any network calls.
"""
import tempfile
import unittest
from pathlib import Path

import core.skills as skills
from runners.local import LocalAgentClient


def _write_skill(root, name, description="A skill.", body="BODY", triggers=None):
    d = Path(root) / name
    d.mkdir(parents=True, exist_ok=True)
    meta = ""
    if triggers:
        lines = "".join(f"      - {t}\n" for t in triggers)
        meta = f"metadata:\n  agentgate:\n    triggers:\n{lines}"
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{meta}---\n\n{body}"
    )


def _client(tmp, *, enabled, mode="model", session_id="t"):
    ctx = skills.build_skills_context(
        enabled=enabled, skills_dir=tmp, manifest_tokens=400, selection_mode=mode
    )
    sess = skills.SkillSession(ctx.catalog) if ctx.enabled else None
    return LocalAgentClient(
        name="test",
        base_url="http://localhost:1/v1",
        model="m",
        session_id=session_id,
        skills_context=ctx if ctx.enabled else None,
        skill_session=sess,
    )


class TestDisabledOutgoing(unittest.TestCase):
    def test_no_skills_means_no_manifest_and_no_volatile(self):
        tmp = tempfile.mkdtemp()
        c = _client(tmp, enabled=False)
        self.assertIsNone(c._skills_context)
        self.assertIsNone(c._skill_session)
        self.assertEqual(c.skills_recommendation("do a thing"), "")
        self.assertEqual(c._skill_reactivation_messages(), [])


class TestModeBAbstention(unittest.TestCase):
    def test_s14_unrelated_task_abstains(self):
        tmp = tempfile.mkdtemp()
        _write_skill(tmp, "pdf-reader", description="Extract text from PDF documents.",
                     triggers=["read pdf", "extract pdf text"])
        c = _client(tmp, enabled=True, mode="recommend")
        # Unrelated request -> no recommendation -> no nudge (trigger-based).
        self.assertEqual(c.skills_recommendation("deploy the kubernetes cluster"), "")

    def test_related_request_recommends_by_name_only(self):
        tmp = tempfile.mkdtemp()
        _write_skill(tmp, "pdf-reader", description="Extract text from PDF documents.",
                     body="SECRET-INSTRUCTION", triggers=["extract text"])
        c = _client(tmp, enabled=True, mode="recommend")
        nudge = c.skills_recommendation("please extract text from this pdf")
        self.assertIn("pdf-reader", nudge)
        # S15: never full skill body in the nudge.
        self.assertNotIn("SECRET-INSTRUCTION", nudge)

    def test_mode_model_never_recommends(self):
        tmp = tempfile.mkdtemp()
        _write_skill(tmp, "pdf-reader", description="Extract text from PDF documents.",
                     triggers=["extract text"])
        c = _client(tmp, enabled=True, mode="model")
        self.assertEqual(c.skills_recommendation("extract text from pdf"), "")


class TestOutgoingMessageInjection(unittest.TestCase):
    def test_s11_body_present_no_reactivation_in_outgoing(self):
        tmp = tempfile.mkdtemp()
        _write_skill(tmp, "pdf-reader", body="USE-PDFTOTEXT")
        c = _client(tmp, enabled=True)
        c.messages = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "read the pdf"},
            {"role": "tool", "content": "USE-PDFTOTEXT"},
        ]
        c._skill_session.record_activation(
            "pdf-reader", c._skills_context.catalog.get("pdf-reader").fingerprint, 1
        )
        out = c._build_outgoing_messages()
        joined = "\n".join(m.get("content", "") for m in out)
        # Body appears exactly once; no restore message prepended.
        self.assertEqual(joined.count("USE-PDFTOTEXT"), 1)
        self.assertNotIn(skills.RESTORE_LABEL, joined)

    def test_s12_trimmed_body_restored_same_turn_lower_trust(self):
        tmp = tempfile.mkdtemp()
        _write_skill(tmp, "pdf-reader", body="USE-PDFTOTEXT")
        c = _client(tmp, enabled=True)
        c._skill_turn = 1  # current task turn
        c.messages = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "read the pdf"},
            {"role": "user", "content": "continue"},
        ]
        c._skill_session.record_activation(
            "pdf-reader", c._skills_context.catalog.get("pdf-reader").fingerprint, 1
        )
        out = c._build_outgoing_messages()
        # Stable prefix (system slot 0) untouched.
        self.assertEqual(out[0], {"role": "system", "content": "SYS"})
        joined = "\n".join(str(m.get("content", "")) for m in out)
        self.assertIn("USE-PDFTOTEXT", joined)
        # Restored as a USER-role lower-trust message, never a system message.
        restore = [m for m in out if skills.RESTORE_LABEL in str(m.get("content", ""))]
        self.assertTrue(restore)
        self.assertTrue(all(m["role"] == "user" for m in restore))

    def test_s12b_previous_task_not_auto_restored(self):
        tmp = tempfile.mkdtemp()
        _write_skill(tmp, "pdf-reader", body="USE-PDFTOTEXT")
        c = _client(tmp, enabled=True)
        c._skill_turn = 2  # a later task turn
        c.messages = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "unrelated new task"},
        ]
        c._skill_session.record_activation(
            "pdf-reader", c._skills_context.catalog.get("pdf-reader").fingerprint, 1
        )
        out = c._build_outgoing_messages()
        joined = "\n".join(str(m.get("content", "")) for m in out)
        # Not restored on a later, unrelated task.
        self.assertNotIn("USE-PDFTOTEXT", joined)


class TestRegistryIntegration(unittest.TestCase):
    def test_skill_read_bypasses_compression_in_loop(self):
        # The local tool loop skips the pipeline for skill_read; assert the
        # helper predicate the loop uses.
        self.assertTrue(skills.is_skill_read_tool("skill_read"))
        self.assertFalse(skills.is_skill_read_tool("Read"))


if __name__ == "__main__":
    unittest.main()
