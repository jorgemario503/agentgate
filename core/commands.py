"""
Platform-agnostic command layer, shared by every bot entrypoint.

Each function takes plain values and returns a reply string — no Telegram
Update/context, no Slack event/say, nothing platform-specific. The bot's
only job is: recognize the command, extract args and user_id in whatever
way that platform requires, call the matching function here, and send the
returned string back through its own transport.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

import core.approvals as approvals
from runners.base import PlatformConfig

if TYPE_CHECKING:
    from runners.base import Runner

# Active runner backend — set at startup via use_runner().
# Each bot process binds this once; it's never shared across processes.
_runner: "Runner | None" = None


def use_runner(runner: "Runner") -> None:
    """Point the command layer at the runner instance for this process."""
    global _runner
    _runner = runner


def _require_runner() -> "Runner":
    if _runner is None:
        raise RuntimeError("use_runner() must be called before any command")
    return _runner


def is_allowed(platform: PlatformConfig, user_id: Optional[str]) -> bool:
    """Fail-open when the platform's allowlist is empty (open to all)."""
    return not platform.allowed_user_ids or user_id in platform.allowed_user_ids


def is_admin(platform: PlatformConfig, user_id: Optional[str]) -> bool:
    """Fail-closed when the platform's admin list is empty (nobody is admin)."""
    return bool(user_id) and user_id in platform.admin_user_ids


async def cancel_reply(thread_id: str) -> str:
    runner = _require_runner()
    try:
        interrupted = await runner.cancel(thread_id)
    except Exception as e:
        return f"⚠️ Failed to interrupt: {str(e)[:300]}"
    if interrupted:
        return "🛑 Interrupting current task…"
    return "Nothing to cancel — no task is currently running."


async def stop_session_reply(thread_id: str, session_store) -> str:
    runner = _require_runner()
    await runner.close_client(thread_id)
    session_store.unregister_session(thread_id)
    approvals.disable_auto_approve(thread_id)
    return "🛑 Session ended."


async def session_status_reply(thread_id: str, session_store) -> str:
    runner = _require_runner()
    meta = session_store.get_session_meta(thread_id)
    if not meta:
        return "🔴 No active session."
    idle = time.time() - meta["last_activity"]
    live = (
        "🟢 client connected"
        if runner.has_client(thread_id)
        else "🟡 will resume from saved session on next message"
    )
    return f"Session active — idle {idle / 60:.0f}m — {live}"


async def conflicts_reply() -> str:
    runner = _require_runner()
    try:
        conflicts = runner.list_pending_conflicts()
    except Exception as e:
        return f"⚠️ Failed to list conflicts: {str(e)[:300]}"
    if not conflicts:
        return "✅ No pending conflicts."
    lines = [f"- {c}" for c in conflicts]
    return "⚠️ Pending conflicts:\n" + "\n".join(lines)


async def proposals_reply() -> str:
    runner = _require_runner()
    try:
        proposals = runner.list_pending_proposals()
    except Exception as e:
        return f"⚠️ Failed to list proposals: {str(e)[:300]}"
    if not proposals:
        return "No pending skill proposals."
    lines = [f"- `{p['proposal_id']}` {p['name']}" for p in proposals]
    return "📋 Pending proposals:\n" + "\n".join(lines)


async def propose_reply(raw_args: str) -> str:
    runner = _require_runner()
    parts = raw_args.strip().split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        return "Usage: propose <name> <content...>"
    name, content = parts[0], parts[1].strip()
    try:
        proposal_id = runner.propose_skill(name=name, content=content)
    except Exception as e:
        return f"⚠️ Failed to create proposal: {str(e)[:300]}"
    return f"📝 Proposal created: `{proposal_id}` — pending admin review."


async def approve_reply(raw_args: str, is_admin: bool) -> str:
    runner = _require_runner()
    if not is_admin:
        return "🚫 Only admins can approve proposals."
    proposal_id = raw_args.strip()
    if not proposal_id:
        return "Usage: approve <proposal_id>"
    try:
        runner.approve_skill_proposal(proposal_id)
    except Exception as e:
        return f"⚠️ Failed to approve: {str(e)[:300]}"
    return f"✅ Approved `{proposal_id}` — skill created."


async def reject_reply(raw_args: str, is_admin: bool) -> str:
    runner = _require_runner()
    if not is_admin:
        return "🚫 Only admins can reject proposals."
    parts = raw_args.strip().split(None, 1)
    if not parts or not parts[0]:
        return "Usage: reject <proposal_id> [reason...]"
    proposal_id = parts[0]
    reason = parts[1].strip() if len(parts) > 1 else None
    try:
        runner.reject_skill_proposal(proposal_id, reason=reason)
    except Exception as e:
        return f"⚠️ Failed to reject: {str(e)[:300]}"
    return f"🚫 Rejected `{proposal_id}`."


async def autoapprove_reply(
    thread_id: str,
    raw_args: str,
    is_admin: bool,
    *,
    user_id: Optional[str] = None,
    session_store=None,
) -> str:
    if not is_admin:
        return "🚫 Only admins can toggle auto-approval."
    arg = raw_args.strip().lower()
    if arg == "on":
        approvals.enable_auto_approve(thread_id)
        print(f"[autoapprove] {thread_id}: enabled by user_id={user_id}")
        if session_store is not None:
            session_store.append_audit_log(
                thread_id, "auto_approve_toggle", {"state": "on", "user_id": user_id}
            )
        return ("✅ Auto-approval enabled for this chat until you turn it off or "
                "the bot restarts. Blocked-pattern and settings.json deny rules "
                "still apply.")
    if arg == "off":
        approvals.disable_auto_approve(thread_id)
        print(f"[autoapprove] {thread_id}: disabled by user_id={user_id}")
        if session_store is not None:
            session_store.append_audit_log(
                thread_id, "auto_approve_toggle", {"state": "off", "user_id": user_id}
            )
        return "🔒 Auto-approval disabled for this session."
    state = "on" if approvals.is_auto_approved(thread_id) else "off"
    return f"Usage: autoapprove on|off (currently {state})"
