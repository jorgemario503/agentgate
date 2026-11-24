"""Scoped tool approval — the ladder ordering is the safety property.

`decide()` is the single place the approval order is encoded, so these tests are about order,
not just outcomes. The critical one: a policy must never override an operator's deny rule.
"""
import pytest

from core.tool_policy import NullPolicy, ToolPolicy, decide, policy_allows


class AllowAll:
    def allows(self, tool_name, input_data):
        return True


class AllowBash:
    def allows(self, tool_name, input_data):
        return tool_name == "Bash"


class Boom:
    def allows(self, tool_name, input_data):
        raise RuntimeError("policy exploded")


def _decide(rule="ask", policy=None, auto=False, tool="Bash", args=None):
    return decide(rule_decision=rule, policy=policy, tool_name=tool,
                  input_data=args or {"command": "pytest"}, auto_approved=auto)


# --------------------------------------------------------------------------
# Ordering: deny always wins
# --------------------------------------------------------------------------

def test_deny_rule_beats_an_allow_all_policy():
    """The whole safety property. A policy must not re-enable what an operator denied."""
    assert _decide(rule="deny", policy=AllowAll()) == "deny"


def test_deny_rule_beats_auto_approve():
    assert _decide(rule="deny", auto=True) == "deny"


def test_allow_rule_beats_a_denying_policy():
    """An operator's explicit allow does not need the policy's consent."""
    assert _decide(rule="allow", policy=NullPolicy()) == "allow"


# --------------------------------------------------------------------------
# The policy rung sits between rules and the thread-wide flag
# --------------------------------------------------------------------------

def test_policy_allows_a_call_that_would_otherwise_ask():
    assert _decide(rule="ask", policy=AllowBash(), tool="Bash") == "allow"


def test_policy_does_not_allow_out_of_scope():
    assert _decide(rule="ask", policy=AllowBash(), tool="Write") == "ask"


def test_policy_is_consulted_before_the_thread_wide_flag():
    """Scoped approval is more specific than a blanket flag, so it is reported first."""
    assert _decide(rule="ask", policy=AllowBash(), auto=False) == "allow"


def test_no_policy_falls_back_to_existing_behaviour():
    """Without a policy the ladder is unchanged: auto-approve, else ask."""
    assert _decide(rule="ask", policy=None, auto=True) == "allow"
    assert _decide(rule="ask", policy=None, auto=False) == "ask"


def test_null_policy_authorises_nothing():
    assert _decide(rule="ask", policy=NullPolicy(), auto=False) == "ask"


# --------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------

def test_raising_policy_denies():
    """A broken scope check must not widen permissions."""
    assert policy_allows(Boom(), "Bash", {}) is False
    assert _decide(rule="ask", policy=Boom(), auto=False) == "ask"


def test_policy_returning_truthy_non_bool_is_coerced():
    class Truthy:
        def allows(self, tool_name, input_data):
            return "yes"
    assert policy_allows(Truthy(), "Bash", {}) is True


def test_none_policy_is_not_an_error():
    assert policy_allows(None, "Bash", {}) is False


# --------------------------------------------------------------------------
# The protocol itself
# --------------------------------------------------------------------------

def test_builtin_policies_satisfy_the_protocol():
    assert isinstance(NullPolicy(), ToolPolicy)
    assert isinstance(AllowBash(), ToolPolicy)


def test_protocol_rejects_a_non_conforming_object():
    assert not isinstance(object(), ToolPolicy)
