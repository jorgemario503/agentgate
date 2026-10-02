"""
Slack entrypoint — @mention-only bot that remote-controls a local agent
session per thread, with runtime tool approvals relayed to Slack.

Run from a shell with `claude` on PATH (or with --fireworks/--ollama/--mlx):
    python -m bots.slack
    python -m bots.slack --ollama --model qwen3:14b
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

from slack_bolt.app.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

import core.approvals as approvals
import core.commands as commands
import core.config as config
from core.sessions import SessionStore
from runners.base import PlatformConfig, TaskInterrupted, check_blocked

CHUNK = 3800  # Slack hard limit is 4000; leave headroom
THINKING_EMOJI = "hourglass_flowing_sand"

# App created at module level — token resolved in make_platform()
app: AsyncApp = None


def make_platform() -> tuple[PlatformConfig, SessionStore]:
    """Build PlatformConfig and SessionStore from env vars. Call once at startup."""
    allowed_ids = frozenset(filter(None, os.environ.get("ALLOWED_SLACK_USER_IDS", "").split(",")))
    admin_ids = frozenset(filter(None, os.environ.get("ADMIN_SLACK_USER_IDS", "").split(",")))
    store = SessionStore(base=os.environ.get("SLACK_SESSION_BASE", "agent-sessions/slack"))
    platform = PlatformConfig(
        session_store=store,
        reply_hint="`@bot yes` to allow or `@bot no` to deny",
        agent_id="slack_assistant",
        allowed_user_ids=allowed_ids,
        admin_user_ids=admin_ids,
    )
    return platform, store


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
# Slack helpers
# ---------------------------------------------------------------------------

async def _post(channel, thread_id, text):
    await app.client.chat_postMessage(channel=channel, thread_ts=thread_id, text=text)


async def _add_reaction(channel, message_ts, name):
    try:
        await app.client.reactions_add(channel=channel, timestamp=message_ts, name=name)
    except Exception:
        pass


async def _remove_reaction(channel, message_ts, name):
    try:
        await app.client.reactions_remove(channel=channel, timestamp=message_ts, name=name)
    except Exception:
        pass


async def _slow_marker(channel, message_ts):
    try:
        await asyncio.sleep(config.SLOW_RESPONSE_SECONDS)
        await _add_reaction(channel, message_ts, config.SLOW_RESPONSE_EMOJI)
    except asyncio.CancelledError:
        raise


async def _deliver(channel, thread_id, text):
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or ["✅ Done."]
    for chunk in chunks:
        await _post(channel, thread_id, chunk)


# ---------------------------------------------------------------------------
# Core flow
# ---------------------------------------------------------------------------

async def _run_and_reply(runner, platform, thread_id, channel, prompt, workspace_path, resume_session_id, message_ts):
    await _add_reaction(channel, message_ts, THINKING_EMOJI)
    watchdog = asyncio.create_task(_slow_marker(channel, message_ts))

    async def thread_post(text):
        await _post(channel, thread_id, text)

    async def cleanup():
        watchdog.cancel()
        await _remove_reaction(channel, message_ts, THINKING_EMOJI)
        await _remove_reaction(channel, message_ts, config.SLOW_RESPONSE_EMOJI)

    try:
        response, session_id = await runner.run_prompt(
            thread_id=thread_id,
            workspace_path=workspace_path,
            prompt=prompt,
            resume_session_id=resume_session_id,
            post_fn=thread_post,
            message_id=message_ts,
            platform=platform,
        )
    except TaskInterrupted:
        await cleanup()
        await _post(channel, thread_id, "🛑 Cancelled.")
        return
    except RuntimeError as e:
        await cleanup()
        await _post(channel, thread_id, f"❌ Error: {str(e)[:1500]}")
        return

    await cleanup()
    platform.session_store.update_session_id(thread_id, session_id)
    await _deliver(channel, thread_id, response or "")


# ---------------------------------------------------------------------------
# @mention handler
# ---------------------------------------------------------------------------

def _register_handlers(platform: PlatformConfig):
    store = platform.session_store

    @app.event("app_mention")
    async def handle_mention(event, say):
        text = event["text"]
        thread_id = event.get("thread_ts") or event["ts"]
        channel = event["channel"]
        message_ts = event["ts"]
        clean_text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
        user_id = event.get("user")

        if not commands.is_allowed(platform, user_id):
            await _post(channel, thread_id, "🚫 You are not authorized to use this bot.")
            return

        if approvals.has_pending(thread_id):
            if approvals.try_resolve(thread_id, clean_text):
                await _add_reaction(channel, message_ts, "white_check_mark")
                return
            reminder = (
                "⚠️ A question is pending. Reply with your answer first."
                if approvals.pending_kind(thread_id) == "answer"
                else "⚠️ A permission request is pending. Reply `@bot yes` or `@bot no` first."
            )
            await _post(channel, thread_id, reminder)
            return

        runner = commands._require_runner()

        if clean_text.lower() == "cancel":
            await _post(channel, thread_id, await commands.cancel_reply(thread_id))
            return

        if clean_text.lower() == "stop session":
            reply = await commands.stop_session_reply(thread_id, store)
            await _post(channel, thread_id, reply)
            return

        if clean_text.lower() == "session status":
            reply = await commands.session_status_reply(thread_id, store)
            await _post(channel, thread_id, reply)
            return

        if clean_text.lower() == "conflicts":
            await _post(channel, thread_id, await commands.conflicts_reply())
            return

        if clean_text.lower() == "proposals":
            await _post(channel, thread_id, await commands.proposals_reply())
            return

        if clean_text.lower().startswith("propose "):
            reply = await commands.propose_reply(clean_text[len("propose "):])
            await _post(channel, thread_id, reply)
            return

        if clean_text.lower().startswith("approve "):
            is_admin = commands.is_admin(platform, user_id)
            reply = await commands.approve_reply(clean_text[len("approve "):], is_admin=is_admin)
            await _post(channel, thread_id, reply)
            return

        if clean_text.lower().startswith("reject "):
            is_admin = commands.is_admin(platform, user_id)
            reply = await commands.reject_reply(clean_text[len("reject "):], is_admin=is_admin)
            await _post(channel, thread_id, reply)
            return

        if clean_text.lower().startswith("autoapprove "):
            is_admin = commands.is_admin(platform, user_id)
            reply = await commands.autoapprove_reply(
                thread_id, clean_text[len("autoapprove "):], is_admin=is_admin,
                user_id=user_id, session_store=store,
            )
            await _post(channel, thread_id, reply)
            return

        blocked = check_blocked(clean_text, config.BLOCKED_PATTERNS)
        if blocked:
            await _post(channel, thread_id, f"🚫 *Blocked.* Your message matches a forbidden pattern: `{blocked}`")
            return

        meta = store.get_session_meta(thread_id)
        if meta and not meta.get("expired"):
            store.touch_session(thread_id)
            await _run_and_reply(
                runner, platform, thread_id, channel, clean_text,
                workspace_path=meta["workspace_path"],
                resume_session_id=meta.get("session_id"),
                message_ts=message_ts,
            )
            return

        if clean_text.lower() in {"stop session", "session status"}:
            await _post(channel, thread_id, "⚠️ No active session. Mention me with a task to start one.")
            return

        prior_session_id = meta.get("session_id") if meta else None
        start_msg = "🔄 Resuming session" if prior_session_id else "🚀 Starting agent session"
        await _post(
            channel, thread_id,
            f"{start_msg}...\n"
            f"When the agent needs permission (e.g. git commit/push), you'll be asked "
            f"`@bot yes` / `@bot no` right here.",
        )

        workspace_path = store.ensure_workspace(thread_id)
        store.register_session(thread_id, channel, workspace_path)
        meta = store.get_session_meta(thread_id)
        await _run_and_reply(
            runner, platform, thread_id, channel, clean_text,
            workspace_path=workspace_path,
            resume_session_id=meta.get("session_id"),
            message_ts=message_ts,
        )

        await _post(
            channel, thread_id,
            "✅ Session active. Tag me in this thread to continue.\n"
            "Commands: `@bot stop session` · `@bot session status`",
        )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

async def main(backend: str = "claude", model: str | None = None):
    global app

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        raise SystemExit("❌ SLACK_BOT_TOKEN is not set — check .env")
    app_token = os.environ.get("SLACK_APP_TOKEN", "")
    if not app_token:
        raise SystemExit("❌ SLACK_APP_TOKEN is not set — check .env")

    app = AsyncApp(token=bot_token)
    platform, store = make_platform()
    runner = await select_runner(backend, model)
    commands.use_runner(runner)
    _register_handlers(platform)

    print(f"[startup] Backend: {backend}")
    os.makedirs(store.base, exist_ok=True)

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
                path=config.AGENTDB_PATH,
                memory_dir=config.AGENTDB_MEMORY_DIR,
                permissions_path=agentdb_permissions,
                tracking_directories=["agent-sessions"],
                tracking_root=".",
            )
            runner.init_agentdb(db, platform=platform)
            print(f"[startup] AgentDB initialized at {config.AGENTDB_PATH} (agent_id=slack_assistant)")
        except ImportError:
            print("[startup] AgentDB not installed — skipping (AGENTDB_ENABLED=true but package missing)")

    print(f"[startup] Blocked patterns: {len(config.BLOCKED_PATTERNS)} rules active")
    print(f"[startup] User allowlist: {platform.allowed_user_ids or 'OPEN TO ALL (set ALLOWED_SLACK_USER_IDS)'}")
    print(f"[startup] Admin allowlist: {platform.admin_user_ids or 'NONE (set ADMIN_SLACK_USER_IDS)'}")
    print(f"[startup] Additional dirs: {config.ADDITIONAL_DIRS or 'none'}")
    print(f"[startup] Approval timeout: {config.APPROVAL_TIMEOUT_SECONDS}s")

    async def notify(channel, thread_id, text):
        await _post(channel, thread_id, text)

    reaper_task = asyncio.create_task(store.reaper(runner.close_client, notify))
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

    handler = AsyncSocketModeHandler(app, app_token)
    await handler.connect_async()
    print("[startup] Bot running via Socket Mode. Ctrl+C to stop.")

    await stop.wait()

    reaper_task.cancel()
    print("[shutdown] Closing agent clients...")
    await runner.close_all()
    print("[shutdown] Closing Slack connection...")
    await handler.close_async()
    print("[shutdown] Done.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Slack bot that remote-controls a local coding agent."
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
    parser.set_defaults(backend="claude")
    args = parser.parse_args()
    asyncio.run(main(args.backend, args.model))
