"""Tests for skill activation: triggers (Mode B), //skill (Mode C), trust and
task boundaries (spec: skill-activation)."""
import tempfile
import unittest
from pathlib import Path

import core.skills as skills
from runners.local import LocalAgentClient


def _skill(root, name, description="A skill.", body="BODY", triggers=None):
    d = Path(root) / name
    d.mkdir(parents=True, exist_ok=True)
    meta = ""
    if triggers:
        lines = "".join(f"      - {t}\n" for t in triggers)
        meta = f"metadata:\n  agentgate:\n    triggers:\n{lines}"
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{meta}---\n\n{body}"
    )


def _cat(*specs):
    root = tempfile.mkdtemp()
    for spec in specs:
        _skill(root, *spec)
    return root, skills.discover(root)


def _client(tmp, *, mode="model", session_id="t"):
    ctx = skills.build_skills_context(
        enabled=True, skills_dir=tmp, manifest_tokens=400, selection_mode=mode
    )
    sess = skills.SkillSession(ctx.catalog)
    return LocalAgentClient(
        name="test", base_url="http://localhost:1/v1", model="m",
        session_id=session_id, skills_context=ctx, skill_session=sess,
    )


# --- Mode B: triggers -------------------------------------------------------

class TestTriggers(unittest.TestCase):
    def test_parsed_into_entry(self):
        _root, cat = _cat(("create-ticket", "Tickets.", "B", ["create a ticket", "open an issue"]))
        self.assertEqual(cat.get("create-ticket").triggers, ("create a ticket", "open an issue"))

    def test_malformed_triggers_dropped(self):
        root = tempfile.mkdtemp()
        d = Path(root) / "x"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: x\ndescription: d\nmetadata:\n  agentgate:\n"
            "    triggers: [\"a\", \"ok phrase\", 5]\n---\nb"
        )
        cat = skills.discover(root)
        self.assertEqual(cat.get("x").triggers, ("ok phrase",))

    def test_match_only_configured_phrases_not_description(self):
        _root, cat = _cat(("t", "create tickets in the usual format", "B", ["usual format"]))
        # Description token 'tickets' does NOT match; only the trigger does.
        self.assertEqual(skills.recommend(cat, user_request="make tickets"), [])
        self.assertTrue(skills.recommend(cat, user_request="use the usual format"))

    def test_word_boundary_no_substring(self):
        _root, cat = _cat(("t", "d", "B", ["ticket"]))
        self.assertEqual(skills.recommend(cat, user_request="tickets please"), [])

    def test_at_most_two_specificity_ordered(self):
        _root, cat = _cat(
            ("a-skill", "d", "B", ["review"]),
            ("b-skill", "d", "B", ["review pr"]),
            ("c-skill", "d", "B", ["review pr for security"]),
        )
        recs = skills.recommend(cat, user_request="review pr for security please")
        self.assertEqual(len(recs), 2)
        # The most specific (longest) trigger ranks first.
        self.assertEqual(recs[0].name, "c-skill")

    def test_abstains_when_no_trigger(self):
        _root, cat = _cat(("t", "d", "B", ["create a ticket"]))
        self.assertEqual(skills.recommend(cat, user_request="deploy the cluster"), [])

    def test_nudge_never_includes_body(self):
        _root, cat = _cat(("t", "d", "SECRET-BODY", ["do it"]))
        recs = skills.recommend(cat, user_request="do it now")
        nudge = skills.build_recommendation_nudge(recs)
        self.assertIn("t", nudge)
        self.assertNotIn("SECRET-BODY", nudge)


# --- Mode C: //skill invocation --------------------------------------------

class TestInvocation(unittest.TestCase):
    def test_valid_resolves_and_splits_task(self):
        tmp, _cat_ = _cat(("review-pr", "d", "BODY-X"))
        c = _client(tmp)
        r = skills.parse_skill_invocation("//review-pr Review PR 124", c._skills_context.catalog)
        self.assertIsInstance(r, skills.SkillInvocation)
        self.assertEqual(r.name, "review-pr")
        self.assertEqual(r.task, "Review PR 124")

    def test_leading_whitespace_ok(self):
        tmp, _ = _cat(("s", "d", "B"))
        c = _client(tmp)
        self.assertIsInstance(
            skills.parse_skill_invocation("   //s do", c._skills_context.catalog),
            skills.SkillInvocation,
        )

    def test_unknown_is_error_with_suggestion(self):
        tmp, _ = _cat(("create-ticket", "d", "B"))
        c = _client(tmp)
        r = skills.parse_skill_invocation("//create-tickt do", c._skills_context.catalog)
        self.assertIsInstance(r, skills.InvocationError)
        self.assertEqual(r.reason, "unknown skill")

    def test_invalid_name_is_error(self):
        tmp, _ = _cat(("s", "d", "B"))
        c = _client(tmp)
        r = skills.parse_skill_invocation("//Bad Name", c._skills_context.catalog)
        self.assertIsInstance(r, skills.InvocationError)

    def test_single_slash_not_invocation(self):
        tmp, _ = _cat(("s", "d", "B"))
        c = _client(tmp)
        self.assertIsNone(skills.parse_skill_invocation("/help", c._skills_context.catalog))

    def test_mid_text_double_slash_not_invocation(self):
        tmp, _ = _cat(("s", "d", "B"))
        c = _client(tmp)
        self.assertIsNone(skills.parse_skill_invocation("run //s now", c._skills_context.catalog))

    def test_resolve_invocation_disabled_is_identity(self):
        # Disabled context: never parse //, return prompt unchanged.
        ctx = skills.build_skills_context(enabled=False, skills_dir="/nope",
                                          manifest_tokens=100, selection_mode="model")
        r = skills.resolve_invocation("//s do", ctx, None, 1)
        self.assertEqual(r.task, "//s do")
        self.assertIsNone(r.explicit_message)
        self.assertIsNone(r.error_reply)


class TestExplicitActivationMessage(unittest.TestCase):
    def test_explicit_message_is_user_role_lower_trust(self):
        tmp, _ = _cat(("s", "d", "BODY-Z"))
        c = _client(tmp)
        r = skills.resolve_invocation("//s do the thing", c._skills_context, c._skill_session, 1)
        self.assertEqual(r.task, "do the thing")
        self.assertEqual(r.skill_name, "s")
        self.assertEqual(r.explicit_message["role"], "user")
        self.assertIn("BODY-Z", r.explicit_message["content"])
        self.assertIn(skills.EXPLICIT_LABEL, r.explicit_message["content"])
        # Activation recorded for lifecycle rules.
        self.assertIn("s", c._skill_session.active_names)


# --- Trust / task boundary --------------------------------------------------

class TestTrustBoundary(unittest.TestCase):
    def test_restore_message_is_user_role(self):
        tmp, _ = _cat(("s", "d", "BODY-Q"))
        c = _client(tmp)
        c._skill_turn = 1
        c.messages = [{"role": "system", "content": "SYS"},
                      {"role": "user", "content": "go"}]
        c._skill_session.record_activation(
            "s", c._skills_context.catalog.get("s").fingerprint, 1)
        out = c._build_outgoing_messages()
        # system prefix unchanged; restore is a user message
        self.assertEqual(out[0], {"role": "system", "content": "SYS"})
        restores = [m for m in out if skills.RESTORE_LABEL in str(m.get("content", ""))]
        self.assertTrue(restores)
        self.assertTrue(all(m["role"] == "user" for m in restores))

    def test_no_auto_restore_across_task(self):
        tmp, _ = _cat(("s", "d", "BODY-Q"))
        c = _client(tmp)
        c._skill_turn = 2
        c.messages = [{"role": "system", "content": "SYS"},
                      {"role": "user", "content": "new task"}]
        c._skill_session.record_activation(
            "s", c._skills_context.catalog.get("s").fingerprint, 1)
        out = c._build_outgoing_messages()
        joined = "\n".join(str(m.get("content", "")) for m in out)
        self.assertNotIn("BODY-Q", joined)


class TestTaskBoundarySession(unittest.TestCase):
    def test_same_task_restores_previous_does_not(self):
        tmp, _ = _cat(("s", "d", "BODY-T"))
        cat = skills.discover(tmp)
        sess = skills.SkillSession(cat)
        sess.record_activation("s", cat.get("s").fingerprint, 3)
        msgs = [{"role": "user", "content": "x"}]
        self.assertIn("BODY-T", sess.reactivation_block(msgs, cat, 3))
        sess2 = skills.SkillSession(cat)
        sess2.record_activation("s", cat.get("s").fingerprint, 3)
        self.assertEqual(sess2.reactivation_block(msgs, cat, 4), "")


if __name__ == "__main__":
    unittest.main()
