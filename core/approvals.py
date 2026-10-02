"""
Approval routing — bridges a paused tool call and the user's reply.

Two flavors:
  - "approval": yes/no gate on a tool call (e.g. Bash). Only yes/no resolves it.
  - "answer": free-text answer to AskUserQuestion. Any non-empty reply resolves it.

Concurrent requests on one thread queue rather than auto-deny: only the item
at index 0 has had its prompt posted and is running its timeout clock.
"""
import asyncio
import time
from typing import TYPE_CHECKING

from core.config import APPROVAL_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from core.sessions import SessionStore

APPROVE_WORDS = {"yes", "approve", "y"}
DENY_WORDS = {"no", "deny", "n"}

_pending: dict[str, list[dict]] = {}
_auto_approve: set[str] = set()


def enable_auto_approve(thread_id: str) -> None:
    _auto_approve.add(thread_id)


def disable_auto_approve(thread_id: str) -> None:
    _auto_approve.discard(thread_id)


def is_auto_approved(thread_id: str) -> bool:
    return thread_id in _auto_approve


def has_pending(thread_id: str) -> bool:
    return bool(_pending.get(thread_id))


def pending_description(thread_id: str) -> str | None:
    queue = _pending.get(thread_id)
    return queue[0]["description"] if queue else None


def pending_kind(thread_id: str) -> str | None:
    queue = _pending.get(thread_id)
    return queue[0]["kind"] if queue else None


def _advance_queue(thread_id: str, item: dict) -> None:
    queue = _pending.get(thread_id)
    if not queue:
        return
    try:
        queue.remove(item)
    except ValueError:
        pass
    if queue:
        queue[0]["activated"].set()
    else:
        _pending.pop(thread_id, None)


async def _await_reply(thread_id: str, description: str, post_fn, *, kind: str,
                        prompt: str, empty_result, timeout_message: str,
                        session_store: "SessionStore"):
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    activated = asyncio.Event()
    item = {
        "future": future,
        "description": description,
        "created_at": None,
        "kind": kind,
        "activated": activated,
    }

    queue = _pending.setdefault(thread_id, [])
    queue.append(item)
    if len(queue) == 1:
        activated.set()
    else:
        session_store.append_audit_log(thread_id, "approval", {
            "description": description[:500], "decision": "queued", "kind": kind,
        })

    await activated.wait()
    item["created_at"] = time.time()

    print(f"[approvals] Pending {kind} created for thread {thread_id}")
    session_store.append_audit_log(thread_id, "approval", {
        "description": description[:500], "decision": "requested", "kind": kind,
    })

    try:
        await post_fn(prompt)
    except Exception as e:
        print(f"[approvals] FAILED to post {kind} prompt for {thread_id}: {e!r}")
        session_store.append_audit_log(thread_id, "approval", {
            "description": description[:500], "decision": "deny",
            "reason": "post_failed", "error": str(e),
        })
        _advance_queue(thread_id, item)
        return empty_result

    try:
        result = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_SECONDS)
        print(f"[approvals] Thread {thread_id} resolved ({kind}): {result!r}")
        session_store.append_audit_log(thread_id, "approval", {
            "description": description[:500], "decision": "resolved",
            "kind": kind, "reason": "user_reply",
        })
        return result
    except asyncio.TimeoutError:
        print(f"[approvals] Thread {thread_id} timed out ({kind})")
        session_store.append_audit_log(thread_id, "approval", {
            "description": description[:500], "decision": "deny",
            "kind": kind, "reason": "timeout",
        })
        try:
            await post_fn(timeout_message)
        except Exception as e:
            print(f"[approvals] Failed to post timeout notice: {e!r}")
        return empty_result
    finally:
        _advance_queue(thread_id, item)


async def request(thread_id: str, description: str, post_fn,
                   reply_hint: str = "`yes` to allow or `no` to deny",
                   session_store: "SessionStore | None" = None) -> bool:
    """Post an approval question and block until the user answers or the timeout."""
    if session_store is None:
        raise RuntimeError("session_store is required")
    minutes = APPROVAL_TIMEOUT_SECONDS // 60
    prompt = (
        f"🔐 *Permission required*\n{description}\n"
        f"Reply {reply_hint} (auto-deny in {minutes} min)."
    )
    result = await _await_reply(
        thread_id, description, post_fn, kind="approval", prompt=prompt,
        empty_result=False, timeout_message="⏰ No reply — permission denied by timeout.",
        session_store=session_store,
    )
    return bool(result)


async def request_answer(thread_id: str, description: str, post_fn,
                          session_store: "SessionStore | None" = None) -> str | None:
    """Post a question (AskUserQuestion) and block until the user replies."""
    if session_store is None:
        raise RuntimeError("session_store is required")
    minutes = APPROVAL_TIMEOUT_SECONDS // 60
    prompt = (
        f"❓ *Claude has a question*\n{description}\n"
        f"Reply in this thread with your answer (auto-skip in {minutes} min)."
    )
    return await _await_reply(
        thread_id, description, post_fn, kind="answer", prompt=prompt,
        empty_result=None, timeout_message="⏰ No reply — skipping the question.",
        session_store=session_store,
    )


def try_resolve(thread_id: str, text: str) -> bool:
    """Called by the bot handler for every incoming message.
    Returns True if the message resolved a pending request (consumed).
    """
    queue = _pending.get(thread_id)
    if not queue:
        return False
    item = queue[0]

    if item["kind"] == "answer":
        stripped = text.strip()
        if not stripped:
            return False
        if not item["future"].done():
            item["future"].set_result(stripped)
        return True

    word = text.strip().lower()
    if word in APPROVE_WORDS:
        if not item["future"].done():
            item["future"].set_result(True)
        return True
    if word in DENY_WORDS:
        if not item["future"].done():
            item["future"].set_result(False)
        return True

    return False
