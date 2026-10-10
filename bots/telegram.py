"""
Telegram entrypoint — private-chat-only bot that remote-controls a local
agent session per chat, with runtime tool approvals relayed to Telegram.

Run from a shell with `claude` on PATH (or with --fireworks/--ollama/--mlx):
    python -m bots.telegram
    python -m bots.telegram --ollama --model qwen3:14b
    python -m bots.telegram --bot personal   # reads TELEGRAM_BOT_TOKEN_PERSONAL
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
from pathlib import Path

try:
    from claude_agent_sdk._internal.transport.subprocess_cli import _kill_active_children as _sdk_kill_children
except Exception as e:
    print(f"[startup] Warning: could not import _kill_active_children: {e}")
    _sdk_kill_children = None

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

import core.approvals as approvals
import core.commands as commands
import core.config as config
from core.sessions import SessionStore
from runners.base import PlatformConfig, TaskInterrupted, check_blocked

# ---------------------------------------------------------------------------
# Module-level globals set by configure_bot() — read-only after that.
# ---------------------------------------------------------------------------
telegram_store: SessionStore = None
TELEGRAM_PLATFORM: PlatformConfig = None
BOT_AGENT_ID: str = "telegram_assistant"

_BOT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

CHUNK = 4000  # Telegram hard limit is 4096; leave headroom


def _agent_id_for(bot_name: str | None) -> str:
    return f"telegram_assistant_{bot_name}" if bot_name else "telegram_assistant"


def configure_bot(bot_name: str | None) -> str:
    """Resolve Telegram bot identity. Returns the bot token.

    bot_name=None → reads TELEGRAM_BOT_TOKEN, stores under agent-sessions/telegram/.
    bot_name='work' → reads TELEGRAM_BOT_TOKEN_WORK (required, no fallback),
        ALLOWED/ADMIN_TELEGRAM_USER_IDS_WORK (fall back to unsuffixed vars),
        stores under agent-sessions/telegram/work/.
    """
    global telegram_store, TELEGRAM_PLATFORM, BOT_AGENT_ID

    if bot_name is not None and not _BOT_NAME_RE.match(bot_name):
        raise SystemExit(f"❌ --bot name must match [A-Za-z0-9_-]+, got: {bot_name!r}")

    suffix = bot_name.upper() if bot_name else None

    def env(base: str, fallback: bool = True) -> str:
        if suffix:
            v = os.environ.get(f"{base}_{suffix}")
            if v is not None:
                return v
            if not fallback:
                return ""
        return os.environ.get(base, "")

    token = env("TELEGRAM_BOT_TOKEN", fallback=False)
    if not token:
        var = f"TELEGRAM_BOT_TOKEN_{suffix}" if suffix else "TELEGRAM_BOT_TOKEN"
        raise SystemExit(f"❌ {var} is not set — check .env")

    allowed_ids = frozenset(filter(None, env("ALLOWED_TELEGRAM_USER_IDS").split(",")))
    admin_ids = frozenset(filter(None, env("ADMIN_TELEGRAM_USER_IDS").split(",")))
    base_dir = f"agent-sessions/telegram/{bot_name}" if bot_name else "agent-sessions/telegram"
    BOT_AGENT_ID = _agent_id_for(bot_name)

    telegram_store = SessionStore(base=base_dir)
    TELEGRAM_PLATFORM = PlatformConfig(
        session_store=telegram_store,
        reply_hint="`yes` to allow or `no` to deny",
        agent_id=BOT_AGENT_ID,
        allowed_user_ids=allowed_ids,
        admin_user_ids=admin_ids,
    )
    return token


# ---------------------------------------------------------------------------
# Runner selection — returns a class-based runner instance
# ---------------------------------------------------------------------------

async def select_runner(backend: str, model: str | None = None):
    if backend == "fireworks":
        try:
            from runners.fireworks import FireworksRunner
        except ImportError as e:
            raise SystemExit(
                "❌ --fireworks selected but the Fireworks SDK isn't installed.\n"
                "   Run:  pip install \"fireworks-ai>=1.2.0\"\n"
                f"   (import error: {e})"
            )
        runner = FireworksRunner()
        active = await runner.set_model(model)
        print(f"[fireworks] active model: {active}")
        return runner

    if backend in ("ollama", "mlx", "local"):
        from runners.local import LocalRunner
        runner = LocalRunner.from_env(backend)
        active = runner.set_model(model)
        print(f"[{backend}] active model: {active}")
        return runner

    from runners.claude import ClaudeRunner
    return ClaudeRunner()


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

async def _post(context, chat_id, text):
    await context.bot.send_message(chat_id=chat_id, text=text)


async def _set_reaction(context, chat_id, message_id, emoji):
    try:
        await context.bot.set_message_reaction(
            chat_id=chat_id, message_id=message_id,
            reaction=[emoji] if emoji else [],
        )
    except Exception:
        pass


async def _slow_marker(context, chat_id, message_id):
    try:
        await asyncio.sleep(config.SLOW_RESPONSE_SECONDS)
        await _set_reaction(context, chat_id, message_id, config.TELEGRAM_SLOW_RESPONSE_REACTION)
    except asyncio.CancelledError:
        raise


async def _deliver(context, chat_id, text):
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or ["✅ Done."]
    for chunk in chunks:
        await _post(context, chat_id, chunk)


# ---------------------------------------------------------------------------
# Core flow
# ---------------------------------------------------------------------------

async def _run_and_reply(runner, chat_id, context, prompt, workspace_path, resume_session_id, message_id):
    user_message_id = int(message_id)
    await _set_reaction(context, chat_id, user_message_id, config.TELEGRAM_THINKING_REACTION)
    watchdog = asyncio.create_task(_slow_marker(context, chat_id, user_message_id))

    async def thread_post(text):
        await _post(context, chat_id, text)

    async def cleanup():
        watchdog.cancel()
        await _set_reaction(context, chat_id, user_message_id, None)

    try:
        response, session_id = await runner.run_prompt(
            thread_id=str(chat_id),
            workspace_path=workspace_path,
            prompt=prompt,
            resume_session_id=resume_session_id,
            post_fn=thread_post,
            message_id=message_id,
            platform=TELEGRAM_PLATFORM,
        )
    except TaskInterrupted:
        await cleanup()
        await _post(context, chat_id, "🛑 Cancelled.")
        return
    except RuntimeError as e:
        await cleanup()
        await _post(context, chat_id, f"❌ Error: {str(e)[:1500]}")
        return

    await cleanup()
    telegram_store.update_session_id(str(chat_id), session_id)
    await _deliver(context, chat_id, response or "")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    text = (message.text or "").strip()
    if not text:
        return

    chat_id = update.effective_chat.id
    thread_id = str(chat_id)
    user_id = str(update.effective_user.id) if update.effective_user else None
    message_id = str(message.message_id)

    if not commands.is_allowed(TELEGRAM_PLATFORM, user_id):
        await _post(context, chat_id, "🚫 You are not authorized to use this bot.")
        return

    if approvals.has_pending(thread_id):
        if approvals.try_resolve(thread_id, text):
            return
        reminder = (
            "⚠️ A question is pending. Reply with your answer first."
            if approvals.pending_kind(thread_id) == "answer"
            else "⚠️ A permission request is pending. Reply `yes` or `no` first."
        )
        await _post(context, chat_id, reminder)
        return

    blocked = check_blocked(text, config.BLOCKED_PATTERNS)
    if blocked:
        await _post(context, chat_id, f"🚫 Blocked. Your message matches a forbidden pattern: `{blocked}`")
        return

    # Retrieve runner from commands module (bound via use_runner at startup)
    runner = commands._require_runner()

    meta = telegram_store.get_session_meta(thread_id)
    if meta and not meta.get("expired"):
        telegram_store.touch_session(thread_id)
        await _run_and_reply(
            runner, chat_id, context, text,
            workspace_path=meta["workspace_path"],
            resume_session_id=meta.get("session_id"),
            message_id=message_id,
        )
        return

    prior_session_id = meta.get("session_id") if meta else None
    start_msg = "🔄 Resuming session" if prior_session_id else "🚀 Starting agent session"
    await _post(
        context, chat_id,
        f"{start_msg}...\n"
        f"When the agent needs permission (e.g. git commit/push), you'll be asked "
        f"`yes` / `no` right here.",
    )

    workspace_path = telegram_store.ensure_workspace(thread_id)
    telegram_store.register_session(thread_id, str(chat_id), workspace_path)
    meta = telegram_store.get_session_meta(thread_id)
    await _run_and_reply(
        runner, chat_id, context, text, workspace_path=workspace_path,
        resume_session_id=meta.get("session_id"), message_id=message_id,
    )

    await _post(
        context, chat_id,
        "✅ Session active. Message me in this chat to continue.\n"
        "Commands: `/stop` · `/status`",
    )


async def _command_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = update.effective_chat.id
    thread_id = str(chat_id)
    user_id = str(update.effective_user.id) if update.effective_user else None

    if not commands.is_allowed(TELEGRAM_PLATFORM, user_id):
        await _post(context, chat_id, "🚫 You are not authorized to use this bot.")
        return False

    if approvals.has_pending(thread_id):
        reminder = (
            "⚠️ A question is pending. Reply with your answer first."
            if approvals.pending_kind(thread_id) == "answer"
            else "⚠️ A permission request is pending. Reply `yes` or `no` first."
        )
        await _post(context, chat_id, reminder)
        return False

    return True


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _command_allowed(update, context):
        return
    thread_id = str(update.effective_chat.id)
    reply = await commands.stop_session_reply(thread_id, telegram_store)
    await _post(context, update.effective_chat.id, reply)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _command_allowed(update, context):
        return
    thread_id = str(update.effective_chat.id)
    reply = await commands.session_status_reply(thread_id, telegram_store)
    await _post(context, update.effective_chat.id, reply)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _command_allowed(update, context):
        return
    thread_id = str(update.effective_chat.id)
    reply = await commands.cancel_reply(thread_id)
    await _post(context, update.effective_chat.id, reply)


async def cmd_conflicts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _command_allowed(update, context):
        return
    reply = await commands.conflicts_reply()
    await _post(context, update.effective_chat.id, reply)


async def cmd_proposals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _command_allowed(update, context):
        return
    reply = await commands.proposals_reply()
    await _post(context, update.effective_chat.id, reply)


async def cmd_propose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _command_allowed(update, context):
        return
    reply = await commands.propose_reply(" ".join(context.args))
    await _post(context, update.effective_chat.id, reply)


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _command_allowed(update, context):
        return
    user_id = str(update.effective_user.id) if update.effective_user else None
    is_admin = commands.is_admin(TELEGRAM_PLATFORM, user_id)
    reply = await commands.approve_reply(" ".join(context.args), is_admin=is_admin)
    await _post(context, update.effective_chat.id, reply)


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _command_allowed(update, context):
        return
    user_id = str(update.effective_user.id) if update.effective_user else None
    is_admin = commands.is_admin(TELEGRAM_PLATFORM, user_id)
    reply = await commands.reject_reply(" ".join(context.args), is_admin=is_admin)
    await _post(context, update.effective_chat.id, reply)


async def cmd_autoapprove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _command_allowed(update, context):
        return
    thread_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id) if update.effective_user else None
    is_admin = commands.is_admin(TELEGRAM_PLATFORM, user_id)
    reply = await commands.autoapprove_reply(
        thread_id, " ".join(context.args), is_admin=is_admin,
        user_id=user_id, session_store=TELEGRAM_PLATFORM.session_store,
    )
    await _post(context, update.effective_chat.id, reply)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

async def main(backend: str = "claude", model: str | None = None, bot_name: str | None = None):
    token = configure_bot(bot_name)
    runner = await select_runner(backend, model)
    commands.use_runner(runner)

    print(f"[startup] Backend: {backend}")
    if bot_name:
        print(f"[startup] Bot identity: {bot_name}  (agent_id={BOT_AGENT_ID})")

    os.makedirs(telegram_store.base, exist_ok=True)

    if config.AGENTDB_ENABLED:
        try:
            from agentdb import AgentDB
            agentdb_permissions = Path(config.AGENTDB_PERMISSIONS_PATH)
            if not agentdb_permissions.exists():
                raise FileNotFoundError(
                    f"AgentDB permissions file not found: {agentdb_permissions}. "
                    f"Set AGENTDB_PERMISSIONS_PATH or create the file."
                )
            db = AgentDB(
                memory_dir=config.AGENTDB_MEMORY_DIR,
                permissions_path=agentdb_permissions,
                tracking_directories=["agent-sessions"],
                tracking_root=".",
            )
            runner.init_agentdb(db, platform=TELEGRAM_PLATFORM)
            db.lifecycle.run_sweep()
            print(f"[startup] AgentDB initialized at {config.AGENTDB_MEMORY_DIR} (agent_id={BOT_AGENT_ID})")
        except ImportError:
            print("[startup] AgentDB not installed — skipping (AGENTDB_ENABLED=true but package missing)")

    print(f"[startup] Blocked patterns: {len(config.BLOCKED_PATTERNS)} rules active")
    suffix_hint = bot_name.upper() if bot_name else None
    allowed_hint = f"ALLOWED_TELEGRAM_USER_IDS_{suffix_hint}" if suffix_hint else "ALLOWED_TELEGRAM_USER_IDS"
    admin_hint = f"ADMIN_TELEGRAM_USER_IDS_{suffix_hint}" if suffix_hint else "ADMIN_TELEGRAM_USER_IDS"
    print(f"[startup] User allowlist: {TELEGRAM_PLATFORM.allowed_user_ids or f'OPEN TO ALL (set {allowed_hint})'}")
    print(f"[startup] Admin allowlist: {TELEGRAM_PLATFORM.admin_user_ids or f'NONE (set {admin_hint})'}")
    print(f"[startup] Additional dirs: {config.ADDITIONAL_DIRS or 'none'}")
    print(f"[startup] Approval timeout: {config.APPROVAL_TIMEOUT_SECONDS}s")

    application = Application.builder().token(token).concurrent_updates(True).build()
    application.add_handler(CommandHandler("stop", cmd_stop, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("status", cmd_status, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("cancel", cmd_cancel, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("conflicts", cmd_conflicts, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("proposals", cmd_proposals, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("propose", cmd_propose, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("approve", cmd_approve, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("reject", cmd_reject, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("autoapprove", cmd_autoapprove, filters=filters.ChatType.PRIVATE))
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_message)
    )

    async def notify(channel, thread_id, text):
        await application.bot.send_message(chat_id=int(thread_id), text=text)

    reaper_task = asyncio.create_task(telegram_store.reaper(runner.close_client, notify))
    print(f"[startup] Reaper started (idle timeout: {config.IDLE_TIMEOUT_SECONDS / 3600:.1f}h)")

    stop = asyncio.Event()
    sig_count = {"n": 0}

    def _on_signal():
        sig_count["n"] += 1
        if sig_count["n"] >= 2:
            print("\n[shutdown] Force exit.")
            if _sdk_kill_children is not None:
                try:
                    _sdk_kill_children()
                except Exception as e:
                    print(f"[shutdown] Warning: could not kill subprocesses: {e}")
            os._exit(1)
        print("\n[shutdown] Stopping... (press Ctrl+C again to force)")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal)

    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    print("[startup] Bot running via long polling. Ctrl+C to stop.")

    await stop.wait()

    reaper_task.cancel()
    print("[shutdown] Closing agent clients...")
    await runner.close_all()
    print("[shutdown] Stopping Telegram polling...")
    await application.updater.stop()
    await application.stop()
    await application.shutdown()
    print("[shutdown] Done.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Telegram bot that remote-controls a local coding agent."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--claude", dest="backend", action="store_const", const="claude",
                       help="Use the Claude Agent SDK backend (default).")
    group.add_argument("--fireworks", dest="backend", action="store_const", const="fireworks",
                       help="Use the Fireworks backend.")
    group.add_argument("--ollama", dest="backend", action="store_const", const="ollama",
                       help="Use a local Ollama server.")
    group.add_argument("--mlx", dest="backend", action="store_const", const="mlx",
                       help="Use a local MLX server.")
    parser.add_argument("--model", dest="model", default=None,
                        help="Model name override (for --fireworks/--ollama/--mlx).")
    parser.add_argument("--bot", dest="bot_name", default=None,
                        help="Named bot identity (e.g. 'personal'). "
                             "Reads TELEGRAM_BOT_TOKEN_<NAME> and scopes session storage.")
    parser.set_defaults(backend="claude")
    args = parser.parse_args()
    asyncio.run(main(args.backend, args.model, args.bot_name))
