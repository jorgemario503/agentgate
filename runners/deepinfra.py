"""Standalone DeepInfra runner using the OpenAI-compatible chat API. Requires openai."""
from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from openai import AsyncOpenAI

import core.approvals as approvals
import core.config as _cfg
from core.config import (
    ADDITIONAL_DIRS,
    BLOCKED_PATTERNS,
    CLAUDE_SETTINGS,
    SDK_ALLOWED_TOOLS,
)
from core.agentdb import AgentDBSurface, prune_session_directories
from core.context.cache_key import cache_key_kwargs, retry_without_cache_key
from core.history import JsonFileHistoryStore
from core.prompts import (
    apply_system_prompt,
    build_summary_prompt,
    build_system_prompt,
)
from core.sessions import classify_session
from core.providers import settings_for
from core.reasoning import (
    build_request_kwargs,
    classify_outcome,
    extract_reasoning_content,
    resolve as resolve_reasoning,
)
from core.tools.registry import Tool, build_tools
from core.skills import (
    build_skills_context, SkillSession,
    is_skill_read_tool, record_activation_from_result,
    recommend, build_recommendation_nudge,
    restoration_message, resolve_invocation,
)
from runners.base import (
    PlatformConfig, TaskInterrupted, check_blocked,
    _describe_ask_user_question, _resolve_ask_user_answer, _describe_tool_call,
)

def _cfg_repo_map_enabled() -> bool:
    import core.config as _c
    return bool(getattr(_c, "REPO_MAP_ENABLED", False))


def _cfg_tool_compression_enabled() -> bool:
    """Whether native tool-result compression is enabled (read lazily)."""
    import core.config as _c
    return bool(_c.TOOL_COMPRESSION_ENABLED)


def _skills_context():
    """Build the shared skills context from config (OFF by default)."""
    import core.config as _c
    return build_skills_context(
        enabled=bool(getattr(_c, "AGENTGATE_SKILLS_ENABLED", False)),
        skills_dir=getattr(_c, "AGENTGATE_SKILLS_DIR", "skills"),
        manifest_tokens=int(getattr(_c, "AGENTGATE_SKILLS_MANIFEST_TOKENS", 600)),
        selection_mode=getattr(_c, "AGENTGATE_SKILLS_SELECTION_MODE", "model"),
    )


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelSpec:
    name: str
    model_id: str
    context_window: int | None


# All provider configuration comes from core.providers (single source of truth).
_SETTINGS = settings_for("deepinfra")

TEMPERATURE = _SETTINGS.temperature
MAX_TOKENS = _SETTINGS.max_tokens
DEFAULT_CONTEXT_WINDOW = _SETTINGS.context_window


import re as _re


def _extract_files(text: str) -> list[str]:
    return _re.findall(r"(?:[\w./-]+/[\w.-]+\.\w{1,6}|/[\w./-]+\.\w{1,6})", text)

def _extract_dirs(text: str, workspace_path: str = "") -> list[str]:
    """Directories the turn mentions that actually exist (see core.context)."""
    from core.context.dir_evidence import extract_existing_dirs
    return extract_existing_dirs(text, workspace_path=workspace_path)

def resolve_model(value: str | None) -> ModelSpec:
    raw = (value or settings_for("deepinfra").model or "").strip()
    if not raw:
        raise ValueError("Set DEEPINFRA_MODEL or provide --model")
    return ModelSpec(raw, raw, DEFAULT_CONTEXT_WINDOW)


# ---------------------------------------------------------------------------
# History trimming / compaction
# ---------------------------------------------------------------------------

TOKEN_CHAR_RATIO = 3.5
HISTORY_RESERVE_TOKENS = 2000
SUMMARY_MODEL_ID = _SETTINGS.summary_model or ""
SUMMARY_MAX_TOKENS = _SETTINGS.summary_max_tokens
SUMMARY_TEMPERATURE = _SETTINGS.summary_temperature
SUMMARY_CHUNK_CHARS = _SETTINGS.summary_chunk_chars
COMPACT_TRIGGER_RATIO = 0.7
COMPACT_TARGET_RATIO = 0.5

def _estimate_tokens(obj: Any) -> int:
    return max(1, int(len(json.dumps(obj)) / TOKEN_CHAR_RATIO))

def _render_turns_for_summary(turns: list[list[dict]]) -> str:
    lines: list[str] = []
    for turn in turns:
        for m in turn:
            role = m.get("role")
            if role == "user":
                lines.append(f"User: {m.get('content', '')}")
            elif role == "assistant":
                if m.get("tool_calls"):
                    names = ", ".join(tc["function"]["name"] for tc in m["tool_calls"])
                    lines.append(f"Assistant: [called tool(s): {names}]")
                    if m.get("content"):
                        lines.append(f"Assistant: {m['content']}")
                else:
                    lines.append(f"Assistant: {m.get('content', '')}")
            elif role == "tool":
                lines.append(f"Tool result: {(m.get('content') or '')[:500]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

class SessionStore(ABC):
    @abstractmethod
    def load(self, session_id: str) -> list[dict]: ...
    @abstractmethod
    def save(self, session_id: str, messages: list[dict]) -> None: ...


class JsonFileSessionStore(JsonFileHistoryStore, SessionStore):
    """Persisted message history for one runner (see core.history)."""

    def __init__(self, directory: str | Path = "./di_sessions"):
        JsonFileHistoryStore.__init__(self, directory=directory)


# ---------------------------------------------------------------------------
# Message types (mimic claude_agent_sdk shapes for isinstance checks)
# ---------------------------------------------------------------------------

@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class AssistantMessage:
    content: list


@dataclass
class ResultMessage:
    session_id: str
    is_error: bool = False
    result: Optional[str] = None


@dataclass
class PermissionResultAllow:
    pass


@dataclass
class PermissionResultDeny:
    message: str = ""


# ---------------------------------------------------------------------------
# Tool registry — the 8 tools live in core/tools/registry.py (shared by all
# open-model runners). Only the provider caps are runner-specific:
# ---------------------------------------------------------------------------
_OUTPUT_CAP = settings_for("deepinfra").tool_output_cap
_BASH_TIMEOUT_SECONDS = settings_for("deepinfra").bash_timeout_seconds


_ALWAYS_ALLOW_TOOLS = {"lookup_directory", "remember_directory"}


# ---------------------------------------------------------------------------
# Permission engine — reimplements settings.json fast-path
# ---------------------------------------------------------------------------

_PATH_FIELD = {
    "Read": "file_path", "Write": "file_path", "Edit": "file_path",
    "MultiEdit": "file_path", "Glob": "path", "Grep": "path",
}

_RULE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\((.*)\)$")


def _rule_target(tool_name: str, input_data: dict) -> str:
    if tool_name == "Bash":
        return input_data.get("command", "") or ""
    field = _PATH_FIELD.get(tool_name)
    return (input_data.get(field, "") or "") if field else ""


def _match_rule(rule: str, tool_name: str, input_data: dict) -> bool:
    m = _RULE_RE.match(rule.strip())
    if not m:
        return rule.strip() == tool_name
    rule_tool, spec = m.group(1), m.group(2)
    if rule_tool != tool_name:
        return False
    return fnmatch.fnmatch(_rule_target(tool_name, input_data).strip(), spec.strip())


def permission_decision(tool_name: str, input_data: dict) -> str:
    """Return 'allow' | 'deny' | 'ask'. Deny wins over allow."""
    perms = CLAUDE_SETTINGS.get("permissions", {})
    if any(_match_rule(r, tool_name, input_data) for r in perms.get("deny", [])):
        return "deny"
    if tool_name in _ALWAYS_ALLOW_TOOLS:
        return "allow"
    if tool_name in SDK_ALLOWED_TOOLS:
        return "allow"
    if any(_match_rule(r, tool_name, input_data) for r in perms.get("allow", [])):
        return "allow"
    return "ask"


# ---------------------------------------------------------------------------
# DeepInfraAgentClient
# ---------------------------------------------------------------------------

CanUseTool = Callable[[str, dict], Awaitable[Any]]

def _safe_json_args(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


class DeepInfraAgentClient:
    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        tools: Optional[dict[str, Tool]] = None,
        system_prompt: Optional[str] = None,
        can_use_tool: Optional[CanUseTool] = None,
        session_id: Optional[str] = None,
        session_store: Optional[SessionStore] = None,
        max_tool_iterations: int = settings_for("deepinfra").max_tool_iterations,
        context_window: int | None = DEFAULT_CONTEXT_WINDOW,
        on_compact: Optional[Callable[[str], None]] = None,
        tool_pipeline=None,
        skills_context=None,
        skill_session=None,
        usage_sink=None,
    ):
        self.model = model
        self.context_window = context_window
        self._usage_sink = usage_sink
        self.temperature = TEMPERATURE
        self.max_tokens = MAX_TOKENS
        settings = settings_for("deepinfra")
        api_key = api_key or settings.api_key
        if not api_key:
            raise ValueError("Set DEEPINFRA_API_KEY or pass api_key=")
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=settings.base_url or "https://api.deepinfra.com/v1/openai",
        )
        self.tools = tools if tools is not None else {}
        self.system_prompt = system_prompt
        self.can_use_tool = can_use_tool
        # Reasoning: level is per-thread and set via the /reasoning command.
        self._thread_id = session_id or ""
        self._reasoning = None
        self.session_id = session_id or str(uuid.uuid4())
        self.session_store: SessionStore = session_store or JsonFileSessionStore()
        self.max_tool_iterations = max_tool_iterations
        self.messages: list[dict] = []
        self._ephemeral_context: Optional[str] = None
        self.on_compact = on_compact
        self._tool_pipeline = tool_pipeline
        # Open-model skills (OFF by default); see runners/local.py.
        self._skills_context = skills_context
        self._skill_session = skill_session
        self._skill_turn = 0
        self._explicit_skill_message = None

    async def connect(self) -> None:
        self.messages = apply_system_prompt(
            self.session_store.load(self.session_id), self.system_prompt
        )

    async def query(self, prompt: str, ephemeral_context: Optional[str] = None,
                    explicit_skill_message: Optional[dict] = None) -> None:
        self.messages.append({"role": "user", "content": prompt})
        self._ephemeral_context = ephemeral_context
        # Explicit //skill body is a lower-trust user message (spec §5), added
        # after the task so normal lifecycle rules apply — never a system role.
        self._explicit_skill_message = explicit_skill_message
        if explicit_skill_message is not None:
            self.messages.append(explicit_skill_message)
        self._skill_turn += 1

    def _trim_history(self) -> None:
        if self.context_window is None:
            return
        system_msg = None
        rest = self.messages
        if rest and rest[0].get("role") == "system":
            system_msg, rest = rest[0], rest[1:]
        turns: list[list[dict]] = []
        for m in rest:
            if m.get("role") == "user" or not turns:
                turns.append([m])
            else:
                turns[-1].append(m)
        if not turns:
            return
        ephemeral_tokens = _estimate_tokens({"content": self._ephemeral_context}) if self._ephemeral_context else 0
        if self.context_window is None:
            return 100_000
        budget = max(self.context_window - (self.max_tokens or 0) - HISTORY_RESERVE_TOKENS - ephemeral_tokens, (self.max_tokens or 0))
        kept = [turns[-1]]
        used = (_estimate_tokens(system_msg) if system_msg else 0) + _estimate_tokens(turns[-1])
        dropped = 0
        for turn in reversed(turns[:-1]):
            cost = _estimate_tokens(turn)
            if used + cost > budget:
                dropped += 1
                continue
            kept.insert(0, turn)
            used += cost
        if dropped:
            print(f"[deepinfra] trimmed {dropped} oldest turn(s) from session {self.session_id}")
        self.messages = ([system_msg] if system_msg else []) + [m for turn in kept for m in turn]

    def skills_recommendation(self, user_request: str) -> str:
        """Mode B: short volatile nudge to read a likely skill (never a body)."""
        if self._skills_context is None or not self._skills_context.enabled:
            return ""
        if self._skills_context.mode != "recommend":
            return ""
        recs = recommend(self._skills_context.catalog, user_request=user_request)
        return build_recommendation_nudge(recs)

    def _skill_reactivation_messages(self) -> list[dict]:
        """At most one lower-trust restore message for a trimmed active skill.

        Only within the same task the skill was activated in. Delivered as a
        USER-role labeled message (spec §5), never as system.
        """
        if self._skill_session is None or self._skills_context is None:
            return []
        blocks = self._skill_session.reactivation_block(
            self.messages, self._skills_context.catalog, self._skill_turn
        )
        if not blocks:
            return []
        name = self._skill_session.last_reactivated_name or "skill"
        return [restoration_message(name, blocks)]

    def _build_outgoing_messages(self) -> list[dict]:
        messages = list(self.messages)
        skill_msgs = self._skill_reactivation_messages()
        if skill_msgs or self._ephemeral_context:
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    for m in reversed(skill_msgs):
                        messages.insert(i, m)
                    if self._ephemeral_context:
                        messages.insert(i, {"role": "system", "content": self._ephemeral_context})
                    break
        return messages

    async def _maybe_compact_history(self) -> None:
        if self.context_window is None or not SUMMARY_MODEL_ID:
            return
        system_msg = None
        rest = self.messages
        if rest and rest[0].get("role") == "system":
            system_msg, rest = rest[0], rest[1:]
        turns: list[list[dict]] = []
        for m in rest:
            if m.get("role") == "user" or not turns:
                turns.append([m])
            else:
                turns[-1].append(m)
        if len(turns) < 2:
            return
        ephemeral_tokens = _estimate_tokens({"content": self._ephemeral_context}) if self._ephemeral_context else 0
        budget = max(self.context_window - (self.max_tokens or 0) - HISTORY_RESERVE_TOKENS - ephemeral_tokens, (self.max_tokens or 0))
        used = (_estimate_tokens(system_msg) if system_msg else 0) + sum(
            _estimate_tokens(t) for t in turns
        )
        if used <= budget * COMPACT_TRIGGER_RATIO:
            return
        target = budget * COMPACT_TARGET_RATIO
        to_compact: list[list[dict]] = []
        remaining = used
        for turn in turns[:-1]:
            if remaining <= target:
                break
            to_compact.append(turn)
            remaining -= _estimate_tokens(turn)
        if not to_compact:
            return
        transcript = _render_turns_for_summary(to_compact)
        try:
            completion = await self._client.chat.completions.create(
                model=SUMMARY_MODEL_ID,
                **({"max_tokens": SUMMARY_MAX_TOKENS} if SUMMARY_MAX_TOKENS is not None else {}),
                **({"temperature": SUMMARY_TEMPERATURE} if SUMMARY_TEMPERATURE is not None else {}),
                messages=[
                    {"role": "system", "content": build_summary_prompt()},
                    {"role": "user", "content": transcript},
                ],
            )
            summary = completion.model_dump(exclude_none=True)["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"[deepinfra] compaction failed for session {self.session_id}: {e}")
            return
        if not summary:
            return
        n_compacted = len(to_compact)
        summary_turn = [{
            "role": "user",
            "content": f"[Summary of {n_compacted} earlier turn(s), compacted to save context]\n{summary}",
        }]
        kept_turns = turns[n_compacted:]
        self.messages = (
            ([system_msg] if system_msg else [])
            + summary_turn
            + [m for t in kept_turns for m in t]
        )
        self.session_store.save(self.session_id, self.messages)
        print(f"[deepinfra] compacted {n_compacted} oldest turn(s) in session {self.session_id}")
        if self.on_compact is not None:
            try:
                self.on_compact(summary)
            except Exception as e:
                print(f"[agentdb] on_compact callback error: {e}")

    def _reasoning_state(self):
        """Per-thread reasoning state owned by core.commands (level + verification)."""
        if self._reasoning is None:
            import core.commands as _commands
            self._reasoning = _commands.reasoning_state_for(self._thread_id or "default")
        return self._reasoning

    def _reasoning_kwargs(self) -> dict:
        state = self._reasoning_state()
        if state.level == "off":
            return {}
        resolution = resolve_reasoning("deepinfra", self.model)
        return build_request_kwargs(resolution, state.level, self.model)

    async def _call_deepinfra(self) -> dict:
        await self._maybe_compact_history()
        self._trim_history()
        outgoing = self._build_outgoing_messages()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": outgoing,
        }
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.tools:
            kwargs["tools"] = [t.schema for t in self.tools.values()]
        kwargs.update(self._reasoning_kwargs())
        cache_kwargs = cache_key_kwargs("deepinfra", self.session_id)
        try:
            try:
                completion = await self._client.chat.completions.create(
                    **kwargs, **cache_kwargs
                )
            except Exception as exc:
                # A server that rejects the cache key must not cost the turn.
                if not cache_kwargs or not retry_without_cache_key(exc):
                    raise
                completion = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            state = self._reasoning_state()
            if state.level != "off":
                # Classify once; a rejection disables reasoning for this thread.
                state.state, state.detail = classify_outcome(
                    request_failed=True, error_message=str(exc), reasoning_text="",
                )
                if state.state == "rejected":
                    state.level = "off"
            raise
        data = completion.model_dump(exclude_none=True)
        self._record_reasoning(data)
        self._emit_usage(data)
        return data

    def _record_reasoning(self, data: dict) -> None:
        state = self._reasoning_state()
        if state.level == "off":
            return
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError):
            return
        text = extract_reasoning_content(message)
        state.state, state.detail = classify_outcome(
            request_failed=False, error_message="", reasoning_text=text,
        )
        if text:
            state.last_reasoning = text

    def _emit_usage(self, data: dict) -> None:
        """Report this response's token usage to the optional sink (best-effort).

        No-ops when no sink is configured, so default behaviour is unchanged.
        ``safe_emit`` swallows sink errors; telemetry never costs a turn.
        """
        if self._usage_sink is None:
            return
        from core.handoff.usage import normalize_usage, safe_emit
        record = normalize_usage(
            data,
            runner="deepinfra",
            model_id=self.model,
            session_key=self.session_id,
        )
        safe_emit(self._usage_sink, record)

    @property

    def _max_tool_result_chars(self) -> int:
        if self.context_window is None:
            return 100_000
        budget = max(self.context_window - (self.max_tokens or 0) - HISTORY_RESERVE_TOKENS, (self.max_tokens or 0))
        return max(8_000, min(100_000, int(budget * TOKEN_CHAR_RATIO * 0.25)))

    async def _run_handler(self, tool: Tool, args: dict) -> str:
        return await asyncio.to_thread(tool.handler, args)

    async def _summarize_tool_result(self, result: str, tool_name: str) -> str:
        cap = self._max_tool_result_chars
        if not SUMMARY_MODEL_ID:
            return result[:cap] + f"\n...[truncated from {len(result):,} chars]"
        chunk_size = SUMMARY_CHUNK_CHARS
        chunks = [result[i:i + chunk_size] for i in range(0, len(result), chunk_size)]
        n = len(chunks)
        per_chunk_tokens = max(50, min(SUMMARY_MAX_TOKENS or int(cap / (n * TOKEN_CHAR_RATIO)), int(cap / (n * TOKEN_CHAR_RATIO))))
        async def _do_summarize(text: str, label: str, max_tok: int) -> str:
            try:
                completion = await self._client.chat.completions.create(
                    model=SUMMARY_MODEL_ID,
                    **({"max_tokens": max_tok} if SUMMARY_MAX_TOKENS is not None else {}),
                    **({"temperature": SUMMARY_TEMPERATURE} if SUMMARY_TEMPERATURE is not None else {}),
                    messages=[
                        {"role": "system", "content": build_summary_prompt()},
                        {"role": "user", "content": (
                            f"Summarize this '{tool_name}' tool output ({label}), "
                            f"preserving all technical details:\n\n{text}"
                        )},
                    ],
                )
                return completion.model_dump(exclude_none=True)["choices"][0]["message"]["content"].strip()
            except Exception as e:
                print(f"[deepinfra] tool result summarization failed ({label}): {e}")
                return ""
        labels = [f"part {i + 1}/{n}" if n > 1 else "full content" for i in range(n)]
        summaries = list(await asyncio.gather(*[
            _do_summarize(c, l, per_chunk_tokens) for c, l in zip(chunks, labels)
        ]))
        valid = [s for s in summaries if s]
        if not valid:
            return (result[:cap]
                    + f"\n...[{len(result):,} chars — summarization failed, truncated at {cap:,}]")
        combined = "\n\n".join(valid)
        if len(combined) > cap:
            final = await _do_summarize(combined[:chunk_size], "final merge", SUMMARY_MAX_TOKENS)
            if final:
                return final
            return combined[:cap] + f"\n...[truncated after summarization, {len(combined):,} chars]"
        return combined

    async def receive_response(self):
        for _ in range(self.max_tool_iterations):
            try:
                data = await self._call_deepinfra()
            except Exception as e:
                yield ResultMessage(
                    session_id=self.session_id, is_error=True,
                    result=f"DeepInfra error: {e}",
                )
                return
            choice = data["choices"][0]
            msg = choice["message"]
            tool_calls = msg.get("tool_calls") or []
            content_blocks: list = []
            if msg.get("content"):
                content_blocks.append(TextBlock(text=msg["content"]))
            for tc in tool_calls:
                fn = tc["function"]
                content_blocks.append(ToolUseBlock(
                    id=tc["id"], name=fn["name"],
                    input=_safe_json_args(fn.get("arguments")),
                ))
            if content_blocks:
                yield AssistantMessage(content=content_blocks)
            if not tool_calls:
                self.messages.append({"role": "assistant", "content": msg.get("content", "")})
                self.session_store.save(self.session_id, self.messages)
                yield ResultMessage(session_id=self.session_id, is_error=False)
                return
            parsed_args: dict[str, dict] = {}
            sanitized_tool_calls = []
            for tc in tool_calls:
                args = _safe_json_args(tc["function"].get("arguments"))
                parsed_args[tc["id"]] = args
                sanitized_tool_calls.append({
                    **tc,
                    "function": {**tc["function"], "arguments": json.dumps(args)},
                })
            self.messages.append({
                "role": "assistant",
                "content": msg.get("content"),
                "tool_calls": sanitized_tool_calls,
            })
            for tc in tool_calls:
                fn = tc["function"]
                name = fn["name"]
                args = parsed_args[tc["id"]]
                tool = self.tools.get(name)
                if tool is None:
                    result_text = f"ERROR: unknown tool '{name}'"
                elif self.can_use_tool is not None:
                    decision = await self.can_use_tool(name, args)
                    if isinstance(decision, PermissionResultDeny):
                        result_text = f"DENIED: {decision.message}"
                    else:
                        result_text = await self._run_handler(tool, args)
                else:
                    result_text = await self._run_handler(tool, args)
                # Skill instructions must be returned exactly: bypass lossy
                # compression and cap-based summarisation for skill_read.
                _is_skill_read = self._skill_session is not None and is_skill_read_tool(name)
                if self._tool_pipeline is not None and not _is_skill_read:
                    result_text = self._tool_pipeline.process(
                        result_text, tool_name=name)
                cap = self._max_tool_result_chars
                if not _is_skill_read and len(result_text) > cap:
                    print(f"[deepinfra] tool '{name}' returned {len(result_text):,} chars "
                          f"(cap {cap:,}) — chunk-summarizing")
                    result_text = await self._summarize_tool_result(result_text, name)
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_text,
                })
                if _is_skill_read:
                    record_activation_from_result(
                        self._skill_session, self._skills_context.catalog,
                        name, args, result_text, self._skill_turn,
                    )
        yield ResultMessage(
            session_id=self.session_id, is_error=True,
            result=f"Hit max_tool_iterations={self.max_tool_iterations} without a final answer",
        )

    async def disconnect(self) -> None:
        self.session_store.save(self.session_id, self.messages)
        await self._client.close()


# ---------------------------------------------------------------------------
# DeepInfraRunner — class-based API (implements runners.base.Runner protocol)
# ---------------------------------------------------------------------------

def _make_permission_callback(
    thread_id: str, post_fn, platform: PlatformConfig,
    interrupt_requested: set[str],
):
    store = platform.session_store
    async def callback(tool_name: str, input_data: dict):
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
                    f"Agent was told to find another way."
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
            decision = permission_decision(tool_name, input_data)
            if decision == "deny":
                store.append_audit_log(thread_id, "tool_call", {
                    "tool": tool_name, "target": target[:500], "decision": "deny_rule",
                })
                return PermissionResultDeny(
                    message="Denied by security policy rule. Do not retry it; "
                            "ask the user how to proceed instead."
                )
            if decision == "allow":
                return PermissionResultAllow()
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
                    f"`{str(e)[:300]}` — denied as a precaution."
                )
            except Exception as post_err:
                print(f"[permissions] {thread_id}: also failed to notify: {post_err!r}")
            return PermissionResultDeny(
                message=f"Permission system error ({e}). The action was denied; "
                        f"tell the user to check the bot logs."
            )
    return callback


class DeepInfraRunner:
    """Class-based DeepInfra runner. One instance per bot process."""

    @classmethod
    def from_env(cls, model: str | None = None) -> "DeepInfraRunner":
        settings_for("deepinfra").require_api_key()
        return cls(model=model)

    #: Name used for the history directory and log prefixes.
    name = "deepinfra"

    def __init__(self, model: str | None = None):
        self._model: ModelSpec = resolve_model(model)
        self._clients: dict[str, DeepInfraAgentClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._current_tasks: dict[str, asyncio.Task] = {}
        self._interrupt_requested: set[str] = set()
        self._agentdb_middleware = None
        self._agentdb = None
        self._agentdb_api = AgentDBSurface()
        self._agent_id: str = "agent"
        self._turn_counters: dict[str, int] = {}
        self._session_workspace_paths: dict[str, str] = {}
        # Static per-session context (orientation + repo map), built once at
        # start/resume. Held here rather than rebuilt per turn: it changes
        # only when the session does.
        self._session_static_context: dict[str, str] = {}
        # Optional telemetry sink; None keeps default behaviour byte-identical.
        self._usage_sink = None

    def set_usage_sink(self, sink) -> None:
        """Install (or clear) a callable receiving one UsageRecord per response.

        Passing ``None`` restores default behaviour. Existing clients keep the sink
        they were built with; new clients pick up the current value.
        """
        self._usage_sink = sink

    async def set_model(self, value: str | None) -> ModelSpec:
        self._model = resolve_model(value)
        return self._model

    @property
    def current_model_id(self) -> str:
        """The model id in use. Single accessor so bots never guess an attr name."""
        return self._model.model_id

    def has_client(self, thread_id: str) -> bool:
        return thread_id in self._clients

    def init_agentdb(self, db, platform: PlatformConfig) -> None:
        import core.config as _cfg
        if not _cfg.AGENTDB_ENABLED:
            return
        try:
            from agentdb.adapters.middleware import AgentDBMiddleware
            # Provider-hosted OpenAI-compatible runtime: use the shared serverless
            # capability tier rather than borrowing another vendor's profile.
            from agentdb.adapters.capabilities import SERVERLESS_CAPABILITIES as CAPABILITIES
        except ImportError as e:
            # The package was present (core.agentdb.init_agentdb_for imports
            # it before calling here), so a failed adapter import is a real
            # installation problem — surface it instead of silently no-op'ing.
            raise RuntimeError(
                f"AgentDB is enabled but its adapters could not be imported: {e}. "
                "Reinstall 'agentdatabase' or set AGENTDB_ENABLED=false."
            ) from e
        self._agent_id = platform.agent_id
        self._agentdb = db
        self._agentdb_middleware = AgentDBMiddleware(
            db=db, agent_id=platform.agent_id, capabilities=CAPABILITIES
        )
        self._agentdb_api.bind(self._agentdb_middleware)

    # --- AgentDB shared surfaces (see core/agentdb.py) -------------------
    def list_pending_conflicts(self) -> list:
        return self._agentdb_api.list_pending_conflicts()

    async def cancel(self, thread_id: str) -> bool:
        task = self._current_tasks.get(thread_id)
        if task is None or task.done():
            return False
        self._interrupt_requested.add(thread_id)
        task.cancel()
        return True

    async def close_client(self, thread_id: str) -> None:
        client = self._clients.pop(thread_id, None)
        # The next client for this thread is a *new* session and must rebuild
        # its static context (and re-resolve the repo root, which may differ).
        self._session_static_context.pop(thread_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        if self._agentdb_middleware is not None:
            ws_path = self._session_workspace_paths.pop(thread_id, None)
            if ws_path:
                jsonl_path = str(Path(ws_path) / "session_summary.jsonl")
                try:
                    from core.agentdb import build_consolidation_llm_fn
                    llm_fn = build_consolidation_llm_fn("deepinfra")
                    await self._agentdb_middleware.end_session(
                        thread_id, jsonl_path,
                        run_consolidation=llm_fn is not None, llm_fn=llm_fn,
                    )
                except Exception as e:
                    print(f"[agentdb] end_session error: {e}")
        self._turn_counters.pop(thread_id, None)
        self._locks.pop(thread_id, None)

    async def close_all(self) -> None:
        for thread_id in list(self._clients.keys()):
            await self.close_client(thread_id)

    def _lock_for(self, thread_id: str) -> asyncio.Lock:
        if thread_id not in self._locks:
            self._locks[thread_id] = asyncio.Lock()
        return self._locks[thread_id]

    async def _get_or_create_client(
        self, thread_id: str, workspace_path: str,
        resume_session_id, post_fn, platform: PlatformConfig,
    ) -> DeepInfraAgentClient:
        if thread_id in self._clients:
            return self._clients[thread_id]
        store = platform.session_store
        history_store = JsonFileSessionStore.for_runner(store.base, self.name)
        state = classify_session(
            platform.session_store.get_session_meta(thread_id),
            self.has_client(thread_id),
        )
        skills_ctx = _skills_context()
        system_prompt = build_system_prompt(
            state=state,
            history_path=str(history_store.directory / f"{thread_id}.json"),
            workspace_path=workspace_path,
            agentdb_middleware=self._agentdb_middleware,
            session_id=thread_id,
            skills_manifest=skills_ctx.manifest,
        )
        skill_session = SkillSession(skills_ctx.catalog) if skills_ctx.enabled else None
        on_compact = None
        if self._agentdb is not None:
            db = self._agentdb
            agent_id = self._agent_id
            def on_compact(summary: str) -> None:
                try:
                    db.checkpoint(agent_id=agent_id, session_id=thread_id)
                    db.write_compaction_summary(
                        agent_id=agent_id, session_id=thread_id, summary=summary
                    )
                except Exception as e:
                    print(f"[agentdb] compaction persist error: {e}")
        _tool_pipeline = None
        if _cfg_tool_compression_enabled():
            from core.context.tool_pipeline import ToolResultPipeline
            _tool_pipeline = ToolResultPipeline(
                store.base, session_id=thread_id, workspace_id=workspace_path,
            )
        client = DeepInfraAgentClient(
            model=self._model.model_id,
            context_window=self._model.context_window,
            tools=build_tools(
                workspace_path, store,
                output_cap=_OUTPUT_CAP, bash_timeout=_BASH_TIMEOUT_SECONDS,
                pipeline=_tool_pipeline,
                skills_catalog=skills_ctx.catalog if skills_ctx.enabled else None,
                workflows_enabled=_cfg.AGENTGATE_WORKFLOWS_ENABLED,
                workflow_registry_dir=_cfg.AGENTGATE_WORKFLOW_REGISTRY_DIR,
                source_role="implementer",
                requested_by=f"agent:{self.name}",
            ),
            system_prompt=system_prompt,
            can_use_tool=_make_permission_callback(
                thread_id, post_fn, platform, self._interrupt_requested,
            ),
            session_id=thread_id,
            session_store=history_store,
            on_compact=on_compact,
            tool_pipeline=_tool_pipeline,
            skills_context=skills_ctx if skills_ctx.enabled else None,
            skill_session=skill_session,
            usage_sink=self._usage_sink,
        )
        await client.connect()
        self._clients[thread_id] = client
        self._session_workspace_paths[thread_id] = workspace_path
        return client

    def _static_session_context(self, thread_id: str, workspace_path: str) -> str:
        """Orientation + repo map, built once per session and cached.

        Both describe things that do not change between turns: where the session
        starts, which paths the operator exposed, and the shape of the repository
        being worked in. Re-deriving them every turn would re-scan the repository
        on every message and re-send identical text, so they are built at the
        session boundary instead — client creation, which happens on a new
        session and on a cold resume.

        Never raises: a failure leaves the session with no static context rather
        than breaking the turn.
        """
        cached = self._session_static_context.get(thread_id)
        if cached is not None:
            return cached

        parts: list[str] = []
        try:
            from core.context.orientation import build_orientation
            _orient = build_orientation(workspace_path, ADDITIONAL_DIRS)
            if _orient:
                parts.append(_orient)
        except Exception:
            pass

        try:
            if _cfg_repo_map_enabled():
                from core.context.repo_map import build_repo_map_segment
                # Candidates are the places the session has evidence for: the
                # validated ADDITIONAL_DIRS entries. The workspace alone is a
                # scratch dir that may sit inside an unrelated checkout, so it
                # is not sufficient evidence on its own (see find_repo_root).
                _seg = build_repo_map_segment(
                    workspace_path, candidates=list(ADDITIONAL_DIRS),
                )
                if _seg:
                    parts.append(_seg)
        except Exception:
            pass

        rendered = "\n\n".join(parts)
        self._session_static_context[thread_id] = rendered
        return rendered

    async def run_prompt(
        self, thread_id: str, workspace_path: str, prompt: str,
        resume_session_id, post_fn, message_id=None, platform: PlatformConfig = None,
    ) -> tuple[str, str]:
        if platform is None:
            raise ValueError("platform is required")
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
            self._current_tasks[thread_id] = asyncio.current_task()
            try:
                client = await self._get_or_create_client(
                    thread_id, workspace_path, resume_session_id, post_fn, platform,
                )
                # Mode C: //skill-name invocation (shared parsing/activation).
                invocation = resolve_invocation(
                    prompt, client._skills_context, client._skill_session,
                    client._skill_turn + 1,
                )
                if invocation.error_reply is not None:
                    return invocation.error_reply, client.session_id
                prompt = invocation.task
                memory_context = None
                # Static context (orientation + repo map) is injected at the
                # session boundary only -- start and resume -- not every turn.
                # It describes things that do not change mid-session, and
                # rebuilding the map here would re-scan the repository on every
                # message. It is still per-session, so it travels in the
                # ephemeral context and never in the cached system prefix.
                _static = self._static_session_context(thread_id, workspace_path)
                if _static:
                    memory_context = _static
                if self._agentdb_middleware is not None:
                    # Entries whose directory is gone would otherwise be
                    # re-injected as "session directories" every turn.
                    prune_session_directories(self._agentdb_middleware, thread_id)
                    messages, _ = await self._agentdb_middleware.prepare_turn_async(
                        user_query=prompt,
                        session_id=thread_id,
                        working_dirs=[workspace_path] + list(ADDITIONAL_DIRS),
                    )
                    parts = [m["content"] for m in messages if m.get("role") in ("system", "user")]
                    memory_parts = [p for p in parts if p != prompt]
                    if memory_parts:
                        memory_context = (
                            "## Relevant memory for this turn\n"
                            + "\n\n".join(memory_parts)
                        )
                _skill_nudge = client.skills_recommendation(prompt)
                if _skill_nudge:
                    memory_context = ((memory_context + "\n\n") if memory_context else "") + _skill_nudge
                await client.query(
                    prompt, ephemeral_context=memory_context,
                    explicit_skill_message=invocation.explicit_message,
                )
                text_parts: list[str] = []
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                text_parts.append(block.text)
                    elif isinstance(message, ResultMessage):
                        if message.is_error:
                            raise RuntimeError(message.result or "DeepInfra agent error")
                response_text = "\n".join(text_parts).strip()
                if self._agentdb_middleware is not None:
                    dirs = _extract_dirs(response_text, workspace_path)
                    files = _extract_files(response_text)
                    self._agentdb_middleware.report_directories(thread_id, dirs)
                    turn_num = self._turn_counters.get(thread_id, 0)
                    self._agentdb_middleware.record_turn_context(
                        session_id=thread_id,
                        turn_num=turn_num,
                        files_read=files,
                        files_written=[],
                        key_fact=(response_text[:120].split(".")[0] if response_text else ""),
                    )
                    self._turn_counters[thread_id] = turn_num + 1
                await _record_outcome(response_text, "task_complete", 1.0)
                # Return resume_session_id unchanged — DeepInfra uses its own
                # history store; the registry's session_id belongs to the Claude backend.
                return response_text, resume_session_id
            except asyncio.CancelledError:
                if thread_id in self._interrupt_requested:
                    await _record_outcome("", "task_cancelled", 0.0)
                    raise TaskInterrupted("Task cancelled by user.")
                raise
            except TaskInterrupted:
                await _record_outcome("", "task_cancelled", 0.0)
                raise
            except RuntimeError:
                await _record_outcome("", "task_failed", 0.0)
                raise
            except Exception as e:
                await _record_outcome("", "task_failed", 0.0)
                await self.close_client(thread_id)
                raise RuntimeError(f"DeepInfra error: {e}") from e
            finally:
                self._current_tasks.pop(thread_id, None)
                self._interrupt_requested.discard(thread_id)
