"""Scoped tool approval — a policy object injected per agent.

Why this exists
---------------
AgentGate's approval ladder has three rungs: deny rules, allow rules, then ``ask`` — which
resolves either by thread-wide auto-approve or by blocking on a human. That fits a solo
developer, and it is unchanged.

It does not fit a batch agent. An orchestrator running an agent for twenty minutes cannot block
on "may I run pytest?" five times, and auto-approving *everything* for the thread is too coarse:
a reviewer should read-only, an implementer should write only inside its worktree.

So the orchestrator supplies a **scope** instead of a conversation. The check is deterministic,
auditable, and needs no round trip.

Where it sits in the ladder
---------------------------
**After** ``permission_decision()`` has returned ``ask``, never before it. That ordering is the
whole safety property: an injected policy can never override an operator's hard deny rule.
Encoded once, in :func:`decide`, so the three runners cannot drift apart.

An agent approving its own tool calls by chatting would be slow, unauditable, and an agent
authorising itself — which the constraints forbid. Pre-authorised scope avoids all three.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class ToolPolicy(Protocol):
    """Decides whether a tool call is pre-authorised for one agent."""

    def allows(self, tool_name: str, input_data: dict) -> bool:
        """True when this call falls inside the agent's granted scope."""
        ...


class NullPolicy:
    """A policy that authorises nothing. Useful as an explicit default in tests."""

    def allows(self, tool_name: str, input_data: dict) -> bool:
        return False


def policy_allows(
    policy: Optional[ToolPolicy], tool_name: str, input_data: dict
) -> bool:
    """Consult ``policy``, failing closed.

    A policy that raises is treated as *deny*: a broken scope check must not silently widen
    permissions. ``None`` means no policy is configured, which is simply "not pre-authorised"
    and falls through to the existing ladder.
    """
    if policy is None:
        return False
    try:
        return bool(policy.allows(tool_name, input_data))
    except Exception:                       # noqa: BLE001 - fail closed, never widen
        return False


def decide(
    *,
    rule_decision: str,
    policy: Optional[ToolPolicy],
    tool_name: str,
    input_data: dict,
    auto_approved: bool,
) -> str:
    """Resolve a tool call to ``'allow' | 'deny' | 'ask'``.

    The full ladder, in one place, in this order:

    1. ``rule_decision == 'deny'``  -> deny   (an operator's hard block always wins)
    2. ``rule_decision == 'allow'`` -> allow  (an operator's allow rule)
    3. policy allows               -> allow  (the orchestrator's per-agent scope)
    4. ``auto_approved``           -> allow  (thread-wide flag, existing behaviour)
    5. otherwise                   -> ask    (block on a human, existing behaviour)

    Steps 3 and 4 are deliberately ordered: a scoped policy is more specific than a
    thread-wide flag, so it is consulted first. Step 1 coming first is the safety property —
    a policy cannot re-enable something the operator denied.
    """
    if rule_decision == "deny":
        return "deny"
    if rule_decision == "allow":
        return "allow"
    if policy_allows(policy, tool_name, input_data):
        return "allow"
    if auto_approved:
        return "allow"
    return "ask"
