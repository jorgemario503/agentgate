"""
WhatsApp entrypoint — wppconnect-server linked device transport.

Connects to a locally-running wppconnect-server instance
(https://github.com/wppconnect-team/wppconnect-server) and receives
inbound messages via webhook. Processes them through the agent runner
and replies via wppconnect-server's send-message endpoint.

Prerequisites:
    1. Clone and start wppconnect-server locally (default port 8082):
           git clone https://github.com/wppconnect-team/wppconnect-server
           cd wppconnect-server && npm install && npm start
    2. Scan the QR code shown in the console to link your WhatsApp device.
    3. Set WPPCONNECT_SERVER_URL in .env (default: http://localhost:8082).
    4. Start this bot:
           python -m bots.whatsapp
    5. Expose the webhook with ngrok so wppconnect-server can reach it:
           ngrok http 8081
       Set WPPCONNECT_WEBHOOK_URL to the resulting HTTPS URL + /webhook.

wppconnect-server API endpoints used by this file:
    POST /api/{session}/start-session    — start or attach to a WhatsApp session
    POST /api/{session}/send-message     — send a text message
    Inbound webhook event (POST /webhook):
        { "event": "onmessage", "session": "...",
          "data": { "from": "5511999999999@c.us", "body": "...", "isGroupMsg": false } }

Run from a shell with `claude` on PATH (or with --fireworks/--ollama/--mlx):
    python -m bots.whatsapp
    python -m bots.whatsapp --ollama --model qwen3:14b
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
import uvicorn

import core.approvals as approvals
import core.commands as commands
import core.config as config
from core.sessions import SessionStore
from runners.base import PlatformConfig, TaskInterrupted, check_blocked

PORT = int(os.environ.get("WHATSAPP_LINKED_PORT", 8081))
CHUNK = 4000

WPPCONNECT_SESSION = os.environ.get("WPPCONNECT_SESSION", "default")
WPPCONNECT_WEBHOOK_URL = os.environ.get("WPPCONNECT_WEBHOOK_URL", f"http://localhost:{PORT}/webhook")

# Built once at module level; runner is set via lifespan.
_whatsapp_store = SessionStore(base="agent-sessions/whatsapp-linked")
WHATSAPP_PLATFORM = PlatformConfig(
    session_store=_whatsapp_store,
    reply_hint="`yes` to allow or `no` to deny",
    agent_id="whatsapp_assistant",
    allowed_user_ids=frozenset(filter(None, os.environ.get("ALLOWED_WHATSAPP_USER_IDS", "").split(","))),
    admin_user_ids=frozenset(filter(None, os.environ.get("ADMIN_WHATSAPP_USER_IDS", "").split(","))),
)

_http: httpx.AsyncClient = None
_runner = None  # set in lifespan


# ---------------------------------------------------------------------------
# Runner selection
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
# wppconnect-server helpers
# ---------------------------------------------------------------------------

def _wpp_url(path: str) -> str:
    return f"{config.WPPCONNECT_SERVER_URL.rstrip('/')}/{path.lstrip('/')}"


async def send_whatsapp(phone: str, text: str):
    await _http.post(
        _wpp_url(f"/api/{WPPCONNECT_SESSION}/send-message"),
        json={"phone": phone, "message": text},
        timeout=15,
    )


async def _deliver(phone: str, text: str):
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or ["✅ Done."]
    for chunk in chunks:
        await send_whatsapp(phone, chunk)


async def _start_wppconnect_session():
    try:
        resp = await _http.post(
            _wpp_url(f"/api/{WPPCONNECT_SESSION}/start-session"),
            json={"webhook": WPPCONNECT_WEBHOOK_URL, "waitQrCode": False},
            timeout=30,
        )
        resp.raise_for_status()
        print(f"[startup] wppconnect-server session '{WPPCONNECT_SESSION}' started/attached")
    except Exception as e:
        print(
            f"[startup] Warning: could not start wppconnect-server session — "
            f"is the server running at {config.WPPCONNECT_SERVER_URL}? ({e})"
        )


# ---------------------------------------------------------------------------
# Core flow
# ---------------------------------------------------------------------------

async def _run_and_reply(phone: str, prompt: str, workspace_path: str, resume_session_id: str | None):
    thread_id = phone

    async def post_fn(text):
        await send_whatsapp(phone, text)

    try:
        response, session_id = await _runner.run_prompt(
            thread_id=thread_id,
            workspace_path=workspace_path,
            prompt=prompt,
            resume_session_id=resume_session_id,
            post_fn=post_fn,
            message_id=None,
            platform=WHATSAPP_PLATFORM,
        )
    except TaskInterrupted:
        await send_whatsapp(phone, "🛑 Cancelled.")
        return
    except RuntimeError as e:
        await send_whatsapp(phone, f"❌ Error: {str(e)[:1500]}")
        return

    _whatsapp_store.update_session_id(thread_id, session_id)
    await _deliver(phone, response or "")


async def handle_message(phone: str, text: str):
    thread_id = phone

    if not commands.is_allowed(WHATSAPP_PLATFORM, phone):
        await send_whatsapp(phone, "🚫 You are not authorized to use this bot.")
        return

    if approvals.has_pending(thread_id):
        if approvals.try_resolve(thread_id, text):
            return
        reminder = (
            "⚠️ A question is pending. Reply with your answer first."
            if approvals.pending_kind(thread_id) == "answer"
            else "⚠️ A permission request is pending. Reply `yes` or `no` first."
        )
        await send_whatsapp(phone, reminder)
        return

    blocked = check_blocked(text, config.BLOCKED_PATTERNS)
    if blocked:
        await send_whatsapp(phone, f"🚫 Blocked. Your message matches a forbidden pattern: `{blocked}`")
        return

    meta = _whatsapp_store.get_session_meta(thread_id)
    if meta and not meta.get("expired"):
        _whatsapp_store.touch_session(thread_id)
        await _run_and_reply(phone, text, meta["workspace_path"], meta.get("session_id"))
        return

    prior_session_id = meta.get("session_id") if meta else None
    start_msg = "🔄 Resuming session" if prior_session_id else "🚀 Starting agent session"
    await send_whatsapp(
        phone,
        f"{start_msg}...\nWhen the agent needs permission (e.g. git commit), reply `yes` or `no`.",
    )

    workspace_path = _whatsapp_store.ensure_workspace(thread_id)
    _whatsapp_store.register_session(thread_id, phone, workspace_path)
    meta = _whatsapp_store.get_session_meta(thread_id)
    await _run_and_reply(phone, text, workspace_path, meta.get("session_id"))

    await send_whatsapp(phone, "✅ Session active. Message me to continue.")


# ---------------------------------------------------------------------------
# FastAPI webhook
# ---------------------------------------------------------------------------

async def _process_webhook(body: dict):
    try:
        if body.get("event") != "onmessage":
            return
        data = body.get("data", {})
        if data.get("isGroupMsg"):
            return
        raw_from = data.get("from", "")
        phone = raw_from.split("@")[0]
        text = (data.get("body") or "").strip()
        if phone and text:
            await handle_message(phone, text)
    except Exception as e:
        print(f"[webhook] Error processing payload: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http, _runner
    if not config.WPPCONNECT_SERVER_URL:
        raise SystemExit("❌ WPPCONNECT_SERVER_URL is not set — check .env")

    backend = os.environ.get("WHATSAPP_BACKEND", "claude")
    model = os.environ.get("WHATSAPP_MODEL") or None
    _runner = await select_runner(backend, model)
    commands.use_runner(_runner)

    if config.AGENTDB_ENABLED:
        try:
            from agentdb import AgentDB
            agentdb_permissions = Path(config.AGENTDB_PERMISSIONS_PATH)
            if agentdb_permissions.exists():
                db = AgentDB(
                    memory_dir=config.AGENTDB_MEMORY_DIR,
                    permissions_path=agentdb_permissions,
                    tracking_directories=["agent-sessions"],
                    tracking_root=".",
                )
                _runner.init_agentdb(db, platform=WHATSAPP_PLATFORM)
                db.lifecycle.run_sweep()
                print(f"[startup] AgentDB initialized at {config.AGENTDB_MEMORY_DIR} (agent_id=whatsapp_assistant)")
            else:
                print(f"[startup] AgentDB permissions file not found at {config.AGENTDB_PERMISSIONS_PATH} — skipping")
        except ImportError:
            print("[startup] AgentDB not installed — skipping")

    _http = httpx.AsyncClient()
    os.makedirs(_whatsapp_store.base, exist_ok=True)
    await _start_wppconnect_session()

    reaper_task = asyncio.create_task(
        _whatsapp_store.reaper(_runner.close_client, _notify)
    )
    print(f"[startup] WhatsApp linked-device bot running on port {PORT}")
    print(f"[startup] wppconnect-server: {config.WPPCONNECT_SERVER_URL}  session: {WPPCONNECT_SESSION}")
    print(f"[startup] Webhook URL: {WPPCONNECT_WEBHOOK_URL}")
    print(f"[startup] Backend: {backend}")
    print(f"[startup] Blocked patterns: {len(config.BLOCKED_PATTERNS)} rules active")
    print(f"[startup] User allowlist: {WHATSAPP_PLATFORM.allowed_user_ids or 'OPEN TO ALL (set ALLOWED_WHATSAPP_USER_IDS)'}")

    yield

    reaper_task.cancel()
    await _runner.close_all()
    await _http.aclose()
    print("[shutdown] Done.")


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def webhook_receive(request: Request):
    body = await request.json()
    asyncio.create_task(_process_webhook(body))
    return Response(status_code=200)


async def _notify(channel, thread_id, text):
    await send_whatsapp(thread_id, text)


# ---------------------------------------------------------------------------
# CLI entry point (for direct process launch via run.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="WhatsApp linked-device bot that remote-controls a local coding agent."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--claude", dest="backend", action="store_const", const="claude")
    group.add_argument("--fireworks", dest="backend", action="store_const", const="fireworks")
    group.add_argument("--ollama", dest="backend", action="store_const", const="ollama")
    group.add_argument("--mlx", dest="backend", action="store_const", const="mlx")
    parser.add_argument("--model", dest="model", default=None)
    parser.add_argument("--port", dest="port", type=int, default=PORT)
    parser.set_defaults(backend="claude")
    args = parser.parse_args()

    if args.backend != "claude":
        os.environ["WHATSAPP_BACKEND"] = args.backend
    if args.model:
        os.environ["WHATSAPP_MODEL"] = args.model

    uvicorn.run(
        "bots.whatsapp:app",
        host="0.0.0.0",
        port=args.port,
        log_level="info",
    )
