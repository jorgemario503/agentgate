"""Tests for bots/telegram.py, bots/slack.py, bots/whatsapp.py."""
import os
import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from runners.base import PlatformConfig


def make_store():
    store = MagicMock()
    store.base = "/tmp/test-bots"
    store.get_session_meta.return_value = None
    return store


# ---------------------------------------------------------------------------
# bots/telegram.py
# ---------------------------------------------------------------------------

def test_telegram_bot_importable():
    from bots import telegram
    assert telegram is not None


def test_telegram_bot_has_main():
    from bots import telegram
    assert callable(getattr(telegram, "main", None))


def test_telegram_configure_bot_reads_env(monkeypatch):
    """configure_bot(None) reads TELEGRAM_BOT_TOKEN from env."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-123")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "111,222")
    monkeypatch.setenv("ADMIN_TELEGRAM_USER_IDS", "111")
    from bots import telegram as tbot
    token = tbot.configure_bot(None)
    assert token == "test-token-123"
    assert "111" in tbot.TELEGRAM_PLATFORM.allowed_user_ids
    assert "111" in tbot.TELEGRAM_PLATFORM.admin_user_ids


def test_telegram_configure_bot_named(monkeypatch):
    """configure_bot('work') reads TELEGRAM_BOT_TOKEN_WORK."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_WORK", "work-token-456")
    from bots import telegram as tbot
    token = tbot.configure_bot("work")
    assert token == "work-token-456"
    assert tbot.BOT_AGENT_ID == "telegram_assistant_work"


def test_telegram_configure_bot_missing_token_raises(monkeypatch):
    """configure_bot raises SystemExit when token is missing."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_MISSING", raising=False)
    from bots import telegram as tbot
    with pytest.raises(SystemExit):
        tbot.configure_bot("missing")


def test_telegram_agent_id_derivation():
    from bots.telegram import _agent_id_for
    assert _agent_id_for(None) == "telegram_assistant"
    assert _agent_id_for("personal") == "telegram_assistant_personal"


# ---------------------------------------------------------------------------
# bots/slack.py
# ---------------------------------------------------------------------------

def test_slack_bot_importable():
    from bots import slack
    assert slack is not None


def test_slack_bot_has_main():
    from bots import slack
    assert callable(getattr(slack, "main", None))


def test_slack_make_platform_reads_env(monkeypatch):
    """make_platform() reads SLACK_BOT_TOKEN, ALLOWED_SLACK_USER_IDS."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("ALLOWED_SLACK_USER_IDS", "U001,U002")
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U001")
    from bots import slack as sbot
    platform, store = sbot.make_platform()
    assert "U001" in platform.allowed_user_ids
    assert platform.agent_id == "slack_assistant"


# ---------------------------------------------------------------------------
# bots/whatsapp.py
# ---------------------------------------------------------------------------

def test_whatsapp_bot_importable():
    from bots import whatsapp
    assert whatsapp is not None


def test_whatsapp_bot_has_app():
    """FastAPI app should be defined at module level."""
    from bots import whatsapp
    assert whatsapp.app is not None


def test_whatsapp_handle_message_blocks_unauthorized(monkeypatch):
    """handle_message should reject users not in the allowlist."""
    monkeypatch.setenv("WPPCONNECT_SERVER_URL", "http://localhost:8082")
    monkeypatch.setenv("ALLOWED_WHATSAPP_USER_IDS", "5511999")
    from bots import whatsapp as wbot
    import importlib
    importlib.reload(wbot)

    sent = []

    async def fake_send(phone, text):
        sent.append((phone, text))

    async def run():
        wbot.send_whatsapp = fake_send
        await wbot.handle_message("9999999", "hello")

    asyncio.run(run())
    assert any("not authorized" in m[1].lower() for m in sent)
