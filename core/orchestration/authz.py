"""Gate authorization — who may resolve *this* gate.

Two checks, deliberately separate
--------------------------------
``core.commands.is_allowed`` answers *"may this user use the bot at all?"* and it
**fails open** when the allowlist is empty. Gate authorization is a different question
with the opposite default:

    is_gate_approver -> fails CLOSED (no approvers configured = nobody may approve)

That asymmetry is the whole point. A gate is an authorization to start agent work; an
empty or misconfigured approver set must stop the workflow, never open it to everyone.
Reusing ``is_allowed`` here would silently grant every bot user the right to approve.

Scoping
-------
Approvers carry a ``platform`` as well as user ids. A Slack user id must not authorize an
approval arriving on another platform, even if the strings collide.

Admin is not a fallback
-----------------------
``is_admin`` means "may widen permissions"; gate approval means "may authorize this
transition". Overloading them would let any admin pass any gate, defeating per-gate
approval (spec §7.1).
"""
from __future__ import annotations

from typing import AbstractSet, Any, Iterable, Mapping, Optional


class NotAuthorized(Exception):
    """Raised when an actor may not resolve a gate."""


def is_gate_approver(
    gate: Mapping[str, Any],
    *,
    platform: Optional[str],
    user_id: Optional[str],
    allow_admins: Iterable[str] = (),
) -> bool:
    """True only when ``user_id`` on ``platform`` is a configured approver for ``gate``.

    Fails closed: a missing/empty approver list, a missing platform, or a missing user id
    all return False. ``allow_admins`` is intentionally a *separate* list — passing admin
    ids here is a caller decision and is not the default.
    """
    if not isinstance(gate, Mapping):
        return False
    if not platform or not user_id:
        return False                      # fail closed on incomplete identity

    approvers = gate.get("approvers")
    if not isinstance(approvers, Mapping):
        return False                      # fail closed: no approver structure

    if approvers.get("platform") != platform:
        return False                      # scoped: wrong platform never matches

    user_ids = approvers.get("user_ids")
    if not isinstance(user_ids, (list, tuple, set, frozenset)):
        return False
    if not user_ids:
        return False                      # fail closed: empty set authorizes nobody

    if user_id in user_ids:
        return True

    # Explicit, opt-in admin fallback. Never enabled by default (spec §7.1).
    admins = {a for a in allow_admins if isinstance(a, str)}
    return bool(admins) and user_id in admins


def require_gate_approver(
    gate: Mapping[str, Any],
    *,
    platform: Optional[str],
    user_id: Optional[str],
    allow_admins: Iterable[str] = (),
) -> None:
    """Raise :class:`NotAuthorized` unless the actor may resolve ``gate``."""
    if not is_gate_approver(gate, platform=platform, user_id=user_id,
                            allow_admins=allow_admins):
        raise NotAuthorized(
            f"{user_id!r}@{platform!r} is not an approver for gate "
            f"{gate.get('gate_id')!r}"
        )
