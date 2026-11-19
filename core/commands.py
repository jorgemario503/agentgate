"""
Platform-agnostic command layer, shared by every bot entrypoint.

Each function takes plain values and returns a reply string — no Telegram
Update/context, no Slack event/say, nothing platform-specific. The bot's
only job is: recognize the command, extract args and user_id in whatever
way that platform requires, call the matching function here, and send the
returned string back through its own transport.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

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


# ---------------------------------------------------------------------------
# Idle timeout
# ---------------------------------------------------------------------------

IDLE_USAGE = "Usage: idle start|stop"


async def idle_reply(
    thread_id: str,
    raw_args: str,
    *,
    user_id: Optional[str] = None,
    session_store=None,
) -> str:
    """Handle `/idle start|stop` — opt a chat out of the idle reaper.

    Mirrors auto-approval: the flag lives in memory only and therefore resets
    when the bot restarts. Conversation history is unaffected (serverless
    runners reload it from disk by thread id).
    """
    from core import sessions as _sessions

    arg = raw_args.strip().lower()
    if arg in ("stop", "off"):
        _sessions.enable_keep_alive(thread_id)
        print(f"[idle] {thread_id}: keep-alive enabled by user_id={user_id}")
        if session_store is not None:
            session_store.append_audit_log(
                thread_id, "idle_keepalive_toggle", {"state": "start", "user_id": user_id}
            )
        return (
            "♾️ Idle timeout disabled for this chat until `/idle stop` or the bot restarts. "
            "The session will not be expired for inactivity."
        )
    if arg in ("start", "on"):
        _sessions.disable_keep_alive(thread_id)
        print(f"[idle] {thread_id}: keep-alive disabled by user_id={user_id}")
        if session_store is not None:
            session_store.append_audit_log(
                thread_id, "idle_keepalive_toggle", {"state": "stop", "user_id": user_id}
            )
        return "⏰ Idle timeout restored for this session."
    state = "start" if _sessions.is_keep_alive(thread_id) else "stop"
    return f"{IDLE_USAGE} (currently {state})"


# ---------------------------------------------------------------------------
# Reasoning mode
# ---------------------------------------------------------------------------

REASONING_USAGE = "Usage: reasoning off|low|medium|high|show"

# Per-thread reasoning state (level + observed verification). Never persisted.
_reasoning_states: dict[str, "object"] = {}


def reasoning_state_for(thread_id: str):
    """Return (creating if needed) the runtime reasoning state for a thread."""
    from core.reasoning.registry import ReasoningRuntimeState

    state = _reasoning_states.get(thread_id)
    if state is None:
        state = ReasoningRuntimeState()
        _reasoning_states[thread_id] = state
    return state


def reset_reasoning_state(thread_id: str) -> None:
    _reasoning_states.pop(thread_id, None)


def reasoning_reply(
    thread_id: str,
    raw_args: str,
    *,
    backend: str,
    model_id: str,
    state,
    session_store=None,
) -> str:
    """Handle `/reasoning <level|show>`.

    `backend` is the runner name ("claude", "deepinfra", "fireworks", "ollama",
    "mlx"). `state` is the per-thread ReasoningRuntimeState owned by the runner.
    """
    from core.reasoning import REASONING_LEVELS, resolve

    arg = raw_args.strip().lower()

    if not arg or arg == "status":
        return _reasoning_status(backend, model_id, state)

    if arg == "show":
        if state.last_reasoning:
            return f"🧠 Last reasoning trace:\n\n{state.last_reasoning}"
        if state.level == "off":
            return "Reasoning is off — nothing to show."
        return f"No reasoning content captured yet. {state.detail or ''}".strip()

    if arg not in REASONING_LEVELS:
        return REASONING_USAGE

    if backend == "claude":
        state.level = arg
        state.state = "unknown" if arg != "off" else "unknown"
        state.detail = ""
        if session_store is not None:
            session_store.append_audit_log(
                thread_id, "reasoning_level", {"level": arg, "backend": backend, "model": model_id}
            )
        if arg == "off":
            return "🔒 Reasoning disabled."
        return (
            f"🧠 Reasoning set to `{arg}` (Claude adaptive thinking).\n"
            "Verification appears after the next reply."
        )

    resolution = resolve(backend, model_id)
    state.level = arg
    state.detail = ""

    if arg == "off":
        state.state = "unknown"
        if session_store is not None:
            session_store.append_audit_log(
                thread_id, "reasoning_level", {"level": "off", "backend": backend, "model": model_id}
            )
        return "🔒 Reasoning disabled."

    if resolution.style == "unsupported":
        state.state = "rejected"
        return f"❌ Model `{model_id}` does not support reasoning controls."

    if not resolution.in_registry:
        # Unknown model: honour the user's explicit choice, but be honest that
        # we cannot verify support up front.
        state.state = "unverified"
        state.detail = (
            "⚠️ Reasoning mode not verified — model not in the registry. "
            "Requested anyway; if the provider rejects or ignores it you will be told."
        )
        if session_store is not None:
            session_store.append_audit_log(
                thread_id, "reasoning_level",
                {"level": arg, "backend": backend, "model": model_id, "registry": "miss"},
            )
        return (
            f"{state.detail}\n"
            f"Requested level: `{arg}` for `{model_id}`."
        )

    state.state = "unknown"
    if session_store is not None:
        session_store.append_audit_log(
            thread_id, "reasoning_level", {"level": arg, "backend": backend, "model": model_id}
        )
    return (
        f"🧠 Reasoning set to `{arg}` for `{model_id}` "
        f"({resolution.style}). Verification appears after the next reply."
    )


def _reasoning_status(backend: str, model_id: str, state) -> str:
    from core.reasoning import resolve

    if backend == "claude":
        support = "adaptive thinking (effort)"
        in_registry = "n/a"
    else:
        resolution = resolve(backend, model_id)
        support = resolution.style
        in_registry = "yes" if resolution.in_registry else "no"

    lines = [
        f"🧠 Reasoning: `{state.level}`",
        f"Backend: {backend} · model: `{model_id}`",
        f"Support: {support} · in registry: {in_registry}",
        f"State: {state.state}",
    ]
    if state.detail:
        lines.append(state.detail)
    if state.level == "off":
        lines.append("Enable with: reasoning low|medium|high")
    lines.append("Show the last trace with: reasoning show")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command registry — single dispatch table shared by every bot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandSpec:
    """One user-facing command.

    ``name`` is the canonical form. ``aliases`` are alternative spellings that
    also map here (e.g. Slack historically used "stop session" and
    "session status"). ``admin_only`` commands are refuse-closed for
    non-admins. ``takes_args`` documents whether trailing text is meaningful.
    """

    name: str
    handler: Callable[["CommandContext"], Awaitable[str]]
    aliases: tuple[str, ...] = ()
    admin_only: bool = False
    takes_args: bool = False
    usage: str = ""
    description: str = ""


@dataclass
class CommandContext:
    """Everything a command handler needs, independent of the platform."""

    platform: PlatformConfig
    thread_id: str
    user_id: Optional[str]
    args: str = ""
    # Optional extras some commands need; None when the bot did not supply them.
    backend: str = "claude"
    model_id: str = ""
    session_store: object = None

    @property
    def is_admin(self) -> bool:
        return is_admin(self.platform, self.user_id)


# ---------------------------------------------------------------------------
# Handlers — adapt the existing *_reply functions to the uniform signature
# ---------------------------------------------------------------------------

async def _cmd_cancel(ctx: CommandContext) -> str:
    return await cancel_reply(ctx.thread_id)


async def _cmd_stop(ctx: CommandContext) -> str:
    return await stop_session_reply(ctx.thread_id, _store(ctx))


async def _cmd_status(ctx: CommandContext) -> str:
    return await session_status_reply(ctx.thread_id, _store(ctx))


async def _cmd_conflicts(ctx: CommandContext) -> str:
    return await conflicts_reply()


async def _cmd_proposals(ctx: CommandContext) -> str:
    return await proposals_reply()


async def _cmd_propose(ctx: CommandContext) -> str:
    return await propose_reply(ctx.args)


async def _cmd_approve(ctx: CommandContext) -> str:
    return await approve_reply(ctx.args, is_admin=ctx.is_admin)


async def _cmd_reject(ctx: CommandContext) -> str:
    return await reject_reply(ctx.args, is_admin=ctx.is_admin)


async def _cmd_autoapprove(ctx: CommandContext) -> str:
    return await autoapprove_reply(
        ctx.thread_id,
        ctx.args,
        is_admin=ctx.is_admin,
        user_id=ctx.user_id,
        session_store=_store(ctx),
    )


async def _cmd_idle(ctx: CommandContext) -> str:
    return await idle_reply(
        ctx.thread_id, ctx.args, user_id=ctx.user_id, session_store=_store(ctx)
    )


async def _cmd_help(ctx: CommandContext) -> str:
    return command_help()


async def _cmd_reasoning(ctx: CommandContext) -> str:
    return reasoning_reply(
        ctx.thread_id,
        ctx.args,
        backend=ctx.backend,
        model_id=ctx.model_id,
        state=reasoning_state_for(ctx.thread_id),
        session_store=_store(ctx),
    )


def _store(ctx: CommandContext):
    """Session store from the context, falling back to the platform's."""
    return ctx.session_store or ctx.platform.session_store


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("stop", _cmd_stop, aliases=("stop session",), description="End the current session"),
    CommandSpec("status", _cmd_status, aliases=("session status",), description="Show session status"),
    CommandSpec("cancel", _cmd_cancel, description="Interrupt the in-flight turn"),
    CommandSpec("conflicts", _cmd_conflicts, description="List pending memory conflicts"),
    CommandSpec("proposals", _cmd_proposals, description="List pending skill proposals"),
    CommandSpec("propose", _cmd_propose, takes_args=True, usage="propose <name> <content>",
                description="Draft a skill proposal"),
    CommandSpec("approve", _cmd_approve, takes_args=True, admin_only=True, usage="approve <id>",
                description="Approve a skill proposal"),
    CommandSpec("reject", _cmd_reject, takes_args=True, admin_only=True, usage="reject <id> [reason]",
                description="Reject a skill proposal"),
    CommandSpec("autoapprove", _cmd_autoapprove, takes_args=True, admin_only=True,
                usage="autoapprove on|off", description="Toggle auto-approval"),
    CommandSpec("idle", _cmd_idle, takes_args=True, usage="idle start|stop",
                description="Disable/enable the idle timeout"),
    CommandSpec("reasoning", _cmd_reasoning, takes_args=True, usage="reasoning off|low|medium|high|show",
                description="Set reasoning effort"),
    CommandSpec("help", _cmd_help, description="List available commands"),
)


def _build_index() -> dict[str, CommandSpec]:
    index: dict[str, CommandSpec] = {}
    for spec in COMMANDS:
        index[spec.name.lower()] = spec
        for alias in spec.aliases:
            index[alias.lower()] = spec
    return index


_INDEX = _build_index()


# Telegram sends commands as "/cmd args" — PTB strips the slash to decide which
# handler fires, but leaves update.message.text intact, so the slash is still
# there when a raw-text dispatcher sees it. Slack never sends a slash: its bot
# mention is removed by bots/slack.py before dispatch. Strip the leading slash
# here so both forms reach the registry; slashless input is unaffected.
_TELEGRAM_SLASH_RE = re.compile(r"^/(?P<cmd>\S+)")


def _normalise(text: str) -> str:
    """Lowercase, collapse whitespace, and drop a leading Telegram slash.

    Slashless input is returned unchanged, so Slack's pre-cleaned text (which
    never carries a slash) parses exactly as before.
    """
    text = text.strip()
    m = _TELEGRAM_SLASH_RE.match(text)
    if m:
        text = m.group("cmd") + text[m.end():]
    return " ".join(text.lower().split())


def parse_command(text: str) -> tuple[CommandSpec, str] | None:
    """Match user text against the registry.

    Longest match first so "stop session" beats a hypothetical bare "stop".
    Returns (spec, args) or None when the text is not a command.
    """
    if not text:
        return None
    normalised = _normalise(text)
    if not normalised:
        return None

    for candidate in sorted(_INDEX, key=len, reverse=True):
        if normalised == candidate:
            return _INDEX[candidate], ""
        if normalised.startswith(candidate + " "):
            return _INDEX[candidate], normalised[len(candidate) + 1:].strip()
    return None


async def dispatch(text: str, ctx: CommandContext) -> str | None:
    """Run the matching command, or return None when the text is not one."""
    match = parse_command(text)
    if match is None:
        return None
    spec, args = match
    if spec.admin_only and not ctx.is_admin:
        return f"🚫 Only admins can use `{spec.name}`."
    ctx.args = args
    if spec.takes_args and not args and spec.usage:
        # Commands that need input show usage instead of an opaque error.
        if spec.name in ("propose", "approve", "reject", "autoapprove"):
            return f"Usage: {spec.usage}"
    return await spec.handler(ctx)


def command_help() -> str:
    """Markdown help generated from the registry."""
    lines = ["Available commands:"]
    for spec in COMMANDS:
        suffix = " (admins)" if spec.admin_only else ""
        lines.append(f"- `{spec.usage or spec.name}` — {spec.description}{suffix}")
    return "\n".join(lines)
