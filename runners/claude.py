"""
Claude Agent SDK runner.

One ClaudeSDKClient per thread, kept in memory while the session is active.
Conversation memory lives in the client; the session_id stored in the registry
lets us resume after a bot restart.

Auth: the SDK spawns the local `claude` binary and uses its login — no
ANTHROPIC_API_KEY involved. Launch from a shell where `claude` is on PATH.
"""
import asyncio
import json
import os
import time
import uuid

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    tool,
    create_sdk_mcp_server,
)

import core.approvals as approvals
from core.config import (
    ADDITIONAL_DIRS,
    AGENTDB_ENABLED,
    BLOCKED_PATTERNS,
    SDK_ALLOWED_TOOLS,
    SYSTEM_PROMPT,
)
from runners.base import (
    PlatformConfig, TaskInterrupted, check_blocked,
    _describe_ask_user_question, _resolve_ask_user_answer, _describe_tool_call,
)

try:
    if AGENTDB_ENABLED:
        from agentdb.adapters.claude_agent_sdk import AgentDBClaudeMiddleware
        _AGENTDB_AVAILABLE = True
    else:
        _AGENTDB_AVAILABLE = False
except ImportError:
    _AGENTDB_AVAILABLE = False

_MAX_ENRICHED_CHARS = 50_000
_SUMMARY_MODEL = "claude-haiku-4-5-20251001"
_SUMMARY_CHUNK_CHARS = 150_000

DIRECTORY_TOOL_NAMES = [
    "mcp__directory_tools__lookup_directory",
    "mcp__directory_tools__remember_directory",
]


async def _haiku_summarize(prompt: str) -> str:
    try:
        options = ClaudeAgentOptions(
            model=_SUMMARY_MODEL,
            allowed_tools=[],
            permission_mode="auto",
            max_turns=1,
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        await client.query(prompt)
        parts: list[str] = []
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
        await client.disconnect()
        return "\n".join(parts).strip()
    except Exception as e:
        print(f"[claude_runner] haiku summarization failed: {e}")
        return ""


async def _haiku_summarize_chunked(instruction: str, content: str) -> str:
    chunks = [content[i:i + _SUMMARY_CHUNK_CHARS]
              for i in range(0, len(content), _SUMMARY_CHUNK_CHARS)]
    n = len(chunks)
    summaries: list[str] = []
    for i, chunk in enumerate(chunks):
        part = f" (part {i + 1}/{n})" if n > 1 else ""
        result = await _haiku_summarize(f"{instruction}{part}:\n\n{chunk}")
        if result:
            summaries.append(result)
    if not summaries:
        return ""
    combined = "\n\n".join(summaries)
    if n > 1:
        merged = await _haiku_summarize(
            f"Merge these {len(summaries)} partial summaries into one coherent summary:\n\n{combined}"
        )
        return merged or combined
    return combined


def _make_directory_tools_server(store):
    @tool("lookup_directory", "Resolve a known directory name to its absolute path", {"name": str})
    async def lookup_directory(args):
        path = store.lookup_directory(args["name"])
        text = path if path else f"No directory registered under '{args['name']}'."
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "remember_directory",
        "Persist a name -> absolute path mapping for future lookups",
        {"name": str, "path": str},
    )
    async def remember_directory(args):
        store.remember_directory(args["name"], args["path"])
        return {"content": [{"type": "text", "text": f"Remembered '{args['name']}' -> {args['path']}"}]}

    return create_sdk_mcp_server(
        name="directory_tools",
        tools=[lookup_directory, remember_directory],
    )



class ClaudeRunner:
    """
    Class-based wrapper around ClaudeSDKClient. One instance per bot process.
    Each platform (Slack, Telegram, …) instantiates its own ClaudeRunner so
    their per-thread _clients and _locks dicts never collide.
    """

    def __init__(self):
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._agentdb_middleware = None
        self._agent_id: str = "agent"
        self._interrupt_requested: set[str] = set()

    # ------------------------------------------------------------------
    # Runner protocol
    # ------------------------------------------------------------------

    def has_client(self, thread_id: str) -> bool:
        return thread_id in self._clients

    def init_agentdb(self, db, platform: PlatformConfig) -> None:
        import core.config as _cfg
        if not _cfg.AGENTDB_ENABLED:
            return
        try:
            from agentdb.adapters.middleware import AgentDBMiddleware
            from agentdb.adapters.capabilities import CLAUDE_CAPABILITIES
        except ImportError:
            return
        self._agent_id = platform.agent_id
        self._agentdb_middleware = AgentDBMiddleware(
            db=db, agent_id=platform.agent_id, capabilities=CLAUDE_CAPABILITIES
        )

    def list_pending_conflicts(self) -> list:
        if self._agentdb_middleware is None:
            raise RuntimeError("AgentDB not initialized — call init_agentdb first")
        return self._agentdb_middleware.pending_conflicts()

    def list_pending_proposals(self, status: str = "pending") -> list[dict]:
        if self._agentdb_middleware is None:
            raise RuntimeError("AgentDB not initialized — call init_agentdb first")
        return self._agentdb_middleware.list_proposals(status=status)

    def propose_skill(self, name: str, content: str) -> str:
        if self._agentdb_middleware is None:
            raise RuntimeError("AgentDB not initialized — call init_agentdb first")
        return self._agentdb_middleware.propose_skill(
            name=name, content=content, scope=[self._agent_id]
        )

    def approve_skill_proposal(self, proposal_id: str) -> None:
        if self._agentdb_middleware is None:
            raise RuntimeError("AgentDB not initialized — call init_agentdb first")
        self._agentdb_middleware.approve_proposal(proposal_id)

    def reject_skill_proposal(self, proposal_id: str, reason=None) -> None:
        if self._agentdb_middleware is None:
            raise RuntimeError("AgentDB not initialized — call init_agentdb first")
        self._agentdb_middleware.reject_proposal(proposal_id, reason=reason)

    async def cancel(self, thread_id: str) -> bool:
        lock = self._locks.get(thread_id)
        client = self._clients.get(thread_id)
        if lock is None or not lock.locked() or client is None:
            return False
        self._interrupt_requested.add(thread_id)
        try:
            await client.interrupt()
        except Exception:
            self._interrupt_requested.discard(thread_id)
            raise
        return True

    async def close_client(self, thread_id: str) -> None:
        client = self._clients.pop(thread_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        self._locks.pop(thread_id, None)

    async def close_all(self) -> None:
        for thread_id in list(self._clients.keys()):
            await self.close_client(thread_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _lock_for(self, thread_id: str) -> asyncio.Lock:
        if thread_id not in self._locks:
            self._locks[thread_id] = asyncio.Lock()
        return self._locks[thread_id]

    def _log_usage(self, thread_id: str, msg: ResultMessage, usage_log: str) -> None:
        record = {
            "ts": time.time(),
            "thread_id": thread_id,
            "session_id": msg.session_id,
            "duration_ms": msg.duration_ms,
            "duration_api_ms": msg.duration_api_ms,
            "num_turns": msg.num_turns,
            "total_cost_usd": msg.total_cost_usd,
            "usage": msg.usage,
            "model_usage": msg.model_usage,
        }
        try:
            with open(usage_log, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            print(f"[claude_runner] usage log error: {e}")

    def _make_permission_callback(self, thread_id: str, post_fn, platform: PlatformConfig):
        store = platform.session_store

        async def callback(tool_name: str, input_data: dict, context):
            try:
                target = (
                    input_data.get("command", "") if tool_name == "Bash"
                    else json.dumps(input_data, default=str)
                )
                print(f"[permissions] {thread_id}: {tool_name} -> {target[:200]}")

                blocked = check_blocked(target, BLOCKED_PATTERNS)
                if blocked:
                    print(f"[permissions] {thread_id}: BLOCKED by pattern {blocked}")
                    store.append_audit_log(thread_id, "tool_call", {
                        "tool": tool_name, "target": target[:500],
                        "decision": "blocked", "pattern": blocked,
                    })
                    await post_fn(
                        f"🚫 Blocked a forbidden operation (pattern `{blocked}`). "
                        f"Claude was told to find another way."
                    )
                    return PermissionResultDeny(
                        message=f"Operation blocked by security policy (matched: {blocked}). "
                                f"Do not retry it; choose a safer approach."
                    )

                if tool_name == "AskUserQuestion":
                    description = _describe_ask_user_question(input_data)
                    raw_answer = await approvals.request_answer(
                        thread_id, description, post_fn, session_store=store,
                    )
                    store.append_audit_log(thread_id, "tool_call", {
                        "tool": tool_name, "target": target[:500],
                        "decision": "answered" if raw_answer is not None else "no_answer",
                    })
                    if raw_answer is None:
                        return PermissionResultDeny(
                            message="The user did not answer in time. Proceed using "
                                    "your best judgment, or ask again as plain text."
                        )
                    answer = _resolve_ask_user_answer(input_data, raw_answer)
                    return PermissionResultDeny(message=f"The user answered: {answer}")

                if approvals.is_auto_approved(thread_id):
                    store.append_audit_log(thread_id, "tool_call", {
                        "tool": tool_name, "target": target[:500], "decision": "auto_allow",
                    })
                    return PermissionResultAllow()

                description = _describe_tool_call(tool_name, input_data)
                allowed = await approvals.request(
                    thread_id, description, post_fn, platform.reply_hint, session_store=store,
                )
                store.append_audit_log(thread_id, "tool_call", {
                    "tool": tool_name, "target": target[:500],
                    "decision": "allow" if allowed else "deny",
                })
                if allowed:
                    return PermissionResultAllow()
                return PermissionResultDeny(
                    message="The user denied this action. Do not retry it; "
                            "ask the user how to proceed instead."
                )

            except Exception as e:
                print(f"[permissions] {thread_id}: CALLBACK ERROR: {e!r}")
                store.append_audit_log(thread_id, "tool_call", {
                    "tool": tool_name, "decision": "error", "error": str(e),
                })
                try:
                    await post_fn(
                        f"⚠️ Permission system error while evaluating `{tool_name}`: "
                        f"`{str(e)[:300]}` — denied as a precaution. Check the bot terminal."
                    )
                except Exception as post_err:
                    print(f"[permissions] {thread_id}: also failed to notify: {post_err!r}")
                return PermissionResultDeny(
                    message=f"Permission system error ({e}). The action was denied; "
                            f"tell the user to check the bot logs."
                )

        return callback

    async def _get_or_create_client(
        self, thread_id: str, workspace_path: str,
        resume_session_id, post_fn, platform: PlatformConfig
    ) -> ClaudeSDKClient:
        if thread_id in self._clients:
            return self._clients[thread_id]

        resume = resume_session_id
        if resume is not None:
            try:
                uuid.UUID(str(resume))
            except (ValueError, TypeError):
                print(f"[claude_runner] Ignoring non-UUID resume id {resume!r} "
                      f"(likely from another backend) — starting a fresh session.")
                resume = None

        options = ClaudeAgentOptions(
            cwd=workspace_path,
            allowed_tools=SDK_ALLOWED_TOOLS + DIRECTORY_TOOL_NAMES,
            mcp_servers={"directory_tools": _make_directory_tools_server(platform.session_store)},
            permission_mode="default",
            can_use_tool=self._make_permission_callback(thread_id, post_fn, platform),
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": SYSTEM_PROMPT,
            },
            setting_sources=["project"],
            add_dirs=ADDITIONAL_DIRS,
            resume=resume,
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        self._clients[thread_id] = client
        return client

    async def run_prompt(
        self, thread_id: str, workspace_path: str, prompt: str,
        resume_session_id, post_fn, message_id=None, platform: PlatformConfig = None
    ) -> tuple[str, str]:
        if platform is None:
            raise ValueError("platform is required")

        usage_log = os.path.join(platform.session_store.base, "usage.jsonl")

        async def _record_outcome(response_text: str, outcome_type: str, outcome_value: float) -> None:
            if self._agentdb_middleware is None:
                return
            try:
                await self._agentdb_middleware.finalize_turn_async(
                    session_id=thread_id,
                    model_response=response_text,
                    message_id=message_id or thread_id,
                    outcome_type=outcome_type,
                    outcome_value=outcome_value,
                )
            except Exception as e:
                print(f"[agentdb] finalize_turn_async error: {e}")

        async with self._lock_for(thread_id):
            try:
                try:
                    client = await self._get_or_create_client(
                        thread_id, workspace_path, resume_session_id, post_fn, platform
                    )

                    enriched_prompt = prompt
                    if self._agentdb_middleware is not None:
                        messages, _ = await self._agentdb_middleware.prepare_turn_async(
                            user_query=prompt,
                            session_id=thread_id,
                            working_dirs=[workspace_path] + list(ADDITIONAL_DIRS),
                        )
                        system_blocks = [m["content"] for m in messages if m.get("role") == "system"]
                        user_blocks = [m["content"] for m in messages if m.get("role") == "user"]
                        prompt_parts = system_blocks + user_blocks
                        if len(prompt_parts) > 1:
                            candidate = "\n\n".join(prompt_parts)
                            if len(candidate) > _MAX_ENRICHED_CHARS:
                                context_text = "\n\n".join(system_blocks)
                                print(f"[claude_runner] agentdb enrichment too large "
                                      f"({len(context_text):,} chars) — chunk-summarizing with {_SUMMARY_MODEL}")
                                summary = await _haiku_summarize_chunked(
                                    f"You are condensing retrieved context for a coding agent. "
                                    f"The user's request is: {prompt}\n\n"
                                    f"Condense the following context to the key information "
                                    f"relevant to that request",
                                    context_text,
                                )
                                if summary:
                                    enriched_prompt = f"{summary}\n\n{prompt}"
                            else:
                                enriched_prompt = candidate

                    await client.query(enriched_prompt)

                    text_parts: list[str] = []
                    session_id = resume_session_id
                    _prompt_too_long = False

                    async for message in client.receive_response():
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    text_parts.append(block.text)
                        elif isinstance(message, ResultMessage):
                            session_id = message.session_id
                            self._log_usage(thread_id, message, usage_log)
                            if message.is_error:
                                if thread_id in self._interrupt_requested:
                                    raise TaskInterrupted("Task cancelled by user.")
                                error_text = message.result or "Claude returned an error"
                                if "prompt is too long" in error_text.lower():
                                    _prompt_too_long = True
                                    break
                                raise RuntimeError(error_text)

                    if _prompt_too_long:
                        print(f"[claude_runner] prompt-too-long — recovering with {_SUMMARY_MODEL}")
                        recovery = await _haiku_summarize(
                            f"The user asked a coding assistant: {prompt}\n\n"
                            f"The assistant attempted to retrieve reference material but "
                            f"it exceeded the context window. Please answer the user's "
                            f"request concisely from your training knowledge."
                        )
                        if recovery:
                            text_parts = [recovery]
                        else:
                            raise RuntimeError("Prompt is too long and recovery summarization failed")

                    response_text = "\n".join(text_parts).strip()
                    await _record_outcome(response_text, "task_complete", 1.0)
                    return response_text, session_id

                finally:
                    self._interrupt_requested.discard(thread_id)

            except TaskInterrupted:
                await _record_outcome("", "task_cancelled", 0.0)
                raise
            except RuntimeError:
                await _record_outcome("", "task_failed", 0.0)
                raise
            except Exception as e:
                await _record_outcome("", "task_failed", 0.0)
                await self.close_client(thread_id)
                raise RuntimeError(f"Claude SDK error: {e}") from e
