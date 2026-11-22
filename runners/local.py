"""
Local LLM runner — OpenAI-compatible chat-completions endpoint via httpx.

Works with any local server that speaks the OpenAI chat-completions protocol:
Ollama, mlx-lm/mlx-omni-server, llama.cpp, LM Studio, etc.

Usage:
    runner = LocalRunner.from_env("ollama")   # reads OLLAMA_* env vars
    runner = LocalRunner.from_env("mlx")      # reads MLX_* env vars
    runner = LocalRunner(name="my-llm", base_url="http://...", model="qwen3:14b")

Requires: httpx (already a project dependency — no new packages needed).
"""
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

import httpx

import core.approvals as approvals
from core.config import (
    ADDITIONAL_DIRS,
    BLOCKED_PATTERNS,
    CLAUDE_SETTINGS,
    SDK_ALLOWED_TOOLS,
)
from core.agentdb import AgentDBSurface
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
# History management constants
# ---------------------------------------------------------------------------

TOKEN_CHAR_RATIO = 3.5
HISTORY_RESERVE_TOKENS = 2000
SUMMARY_TEMPERATURE = 0.2
COMPACT_TRIGGER_RATIO = 0.7
COMPACT_TARGET_RATIO = 0.5

import re as _re


def _extract_files(text: str) -> list[str]:
    return _re.findall(r'(?:[\w./\-]+/[\w.\-]+\.\w{1,6}|/[\w./\-]+\.\w{1,6})', text)


def _extract_dirs(text: str) -> list[str]:
    return _re.findall(r'/(?:[\w./\-]+/)+', text)


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

    def __init__(self, directory: str | Path = "./local_sessions"):
        JsonFileHistoryStore.__init__(self, directory=directory)


# ---------------------------------------------------------------------------
# Message shapes
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
_OUTPUT_CAP = 8_000  # fixed cap for local servers (ollama/mlx); not provider-tuned
_BASH_TIMEOUT_SECONDS = 120


_ALWAYS_ALLOW_TOOLS = {"lookup_directory", "remember_directory"}


# ---------------------------------------------------------------------------
# Permission engine — same rule grammar as runners/fireworks.py
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
# LocalAgentClient — httpx-based OpenAI-compatible client
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


def _unwrap_extra_body(kwargs: dict) -> dict:
    """Flatten a reasoning `extra_body` entry into the top-level request body.

    The OpenAI SDK exposes `extra_body={...}` as a kwarg it merges into the JSON
    body automatically. The local runner talks raw httpx, so passing the same
    dict through would send a literal ``"extra_body"`` field, which no
    OpenAI-compatible server expects. Unwrap it here so all three transports
    produce the same wire body. Non-extra_body keys pass through unchanged.
    """
    extra = kwargs.get("extra_body")
    if not isinstance(extra, dict):
        return kwargs
    rest = {k: v for k, v in kwargs.items() if k != "extra_body"}
    merged = dict(extra)
    merged.update(rest)  # an explicit top-level key wins over the nested one
    return merged


class LocalAgentClient:
    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        api_key: str = "local",
        summary_model: str = "",
        tools: Optional[dict[str, Tool]] = None,
        system_prompt: Optional[str] = None,
        can_use_tool: Optional[CanUseTool] = None,
        session_id: Optional[str] = None,
        session_store: Optional[SessionStore] = None,
        max_tool_iterations: int = 25,  # overridden per-provider in from_env()
        context_window: int = 32_768,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        summary_chunk_chars: int = 20_000,
        summary_max_tokens: int = 2048,
        on_compact: Optional[Callable[[str], None]] = None,
        tool_pipeline=None,
        skills_context=None,
        skill_session=None,
        usage_sink=None,
    ):
        self.name = name
        self._usage_sink = usage_sink
        self.model = model
        self.summary_model = summary_model
        self.context_window = context_window
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.summary_chunk_chars = summary_chunk_chars
        self.summary_max_tokens = summary_max_tokens
        # Reasoning: level is per-thread, set via the /reasoning command.
        self._thread_id = session_id or ""
        self._reasoning = None

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(600.0),
        )
        self.tools = tools or {}
        self.system_prompt = system_prompt
        self.can_use_tool = can_use_tool
        self.session_id = session_id or str(uuid.uuid4())
        self.session_store: SessionStore = session_store or JsonFileSessionStore()
        self.max_tool_iterations = max_tool_iterations
        self.messages: list[dict] = []
        self._ephemeral_context: Optional[str] = None
        self.on_compact = on_compact
        self._tool_pipeline = tool_pipeline
        # Open-model skills (OFF by default). When skills_context is None or
        # disabled, no skill state is consulted and outbound requests are
        # byte-for-byte unchanged.
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
        # Explicit //skill activation: append the lower-trust skill body right
        # after the task so normal lifecycle rules (no-repeat) apply, WITHOUT
        # using a system role (spec §5 — preserve lower-trust origin).
        self._explicit_skill_message = explicit_skill_message
        if explicit_skill_message is not None:
            self.messages.append(explicit_skill_message)
        self._skill_turn += 1

    def _trim_history(self) -> None:
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
        budget = max(self.context_window - self.max_tokens - HISTORY_RESERVE_TOKENS - ephemeral_tokens, self.max_tokens)
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
            print(f"[{self.name}] trimmed {dropped} oldest turn(s) from session {self.session_id}")
        self.messages = ([system_msg] if system_msg else []) + [m for turn in kept for m in turn]

    def skills_recommendation(self, user_request: str) -> str:
        """Mode B: a short, volatile nudge to read a likely skill (never a body)."""
        if self._skills_context is None or not self._skills_context.enabled:
            return ""
        if self._skills_context.mode != "recommend":
            return ""
        recs = recommend(self._skills_context.catalog, user_request=user_request)
        return build_recommendation_nudge(recs)

    def _skill_reactivation_messages(self) -> list[dict]:
        """At most one lower-trust restore message for a trimmed active skill.

        Only produced within the same task the skill was activated in. The body
        is delivered as a USER-role, labeled message (spec §5), never as system.
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
        # Lower-trust skill restore messages (USER role), inserted just before
        # the latest user turn so the stable prefix is untouched.
        skill_msgs = self._skill_reactivation_messages()
        if skill_msgs or self._ephemeral_context:
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    # Insert restore messages (user) then ephemeral (system) so
                    # ephemeral stays the nearest preceding context as before.
                    for m in reversed(skill_msgs):
                        messages.insert(i, m)
                    if self._ephemeral_context:
                        messages.insert(i, {"role": "system", "content": self._ephemeral_context})
                    break
        return messages

    async def _summarize(self, summarizer_messages: list[dict], max_tok: int) -> str:
        if not self.summary_model:
            return ""
        try:
            response = await self._http.post("/chat/completions", json={
                "model": self.summary_model,
                "max_tokens": max_tok,
                "temperature": SUMMARY_TEMPERATURE,
                "messages": summarizer_messages,
            })
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"[{self.name}] summarization failed: {e}")
            return ""

    async def _maybe_compact_history(self) -> None:
        if not self.summary_model:
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
        budget = max(self.context_window - self.max_tokens - HISTORY_RESERVE_TOKENS - ephemeral_tokens, self.max_tokens)
        used = (_estimate_tokens(system_msg) if system_msg else 0) + sum(_estimate_tokens(t) for t in turns)
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
        summary = await self._summarize([
            {"role": "system", "content": build_summary_prompt()},
            {"role": "user", "content": transcript},
        ], self.summary_max_tokens)
        if not summary:
            return

        n = len(to_compact)
        summary_turn = [{"role": "user", "content": f"[Summary of {n} earlier turn(s)]\n{summary}"}]
        kept_turns = turns[n:]
        self.messages = (
            ([system_msg] if system_msg else [])
            + summary_turn
            + [m for t in kept_turns for m in t]
        )
        self.session_store.save(self.session_id, self.messages)
        print(f"[{self.name}] compacted {n} oldest turn(s) in session {self.session_id}")
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

    def _reasoning_payload(self) -> dict:
        state = self._reasoning_state()
        if state.level == "off":
            return {}
        resolution = resolve_reasoning("local", self.model)
        return _unwrap_extra_body(build_request_kwargs(resolution, state.level, self.model))

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
        """
        if self._usage_sink is None:
            return
        from core.orchestration.usage import normalize_usage, safe_emit
        record = normalize_usage(
            data,
            runner="local",
            model_id=self.model,
            session_key=self.session_id,
        )
        safe_emit(self._usage_sink, record)

    async def _call_local(self) -> dict:
        await self._maybe_compact_history()
        self._trim_history()
        outgoing = self._build_outgoing_messages()
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": outgoing,
        }
        if self.tools:
            payload["tools"] = [t.schema for t in self.tools.values()]
        payload.update(self._reasoning_payload())
        try:
            response = await self._http.post("/chat/completions", json=payload)
            response.raise_for_status()
        except Exception as exc:
            state = self._reasoning_state()
            if state.level != "off":
                body = ""
                resp = getattr(exc, "response", None)
                if resp is not None:
                    try:
                        body = resp.text[:400]
                    except Exception:
                        body = ""
                state.state, state.detail = classify_outcome(
                    request_failed=True,
                    error_message=f"{exc} {body}".strip(),
                    reasoning_text="",
                )
                if state.state == "rejected":
                    state.level = "off"
            raise
        data = response.json()
        self._record_reasoning(data)
        self._emit_usage(data)
        return data

    @property
    def _max_tool_result_chars(self) -> int:
        budget = max(self.context_window - self.max_tokens - HISTORY_RESERVE_TOKENS, self.max_tokens)
        return max(8_000, min(100_000, int(budget * TOKEN_CHAR_RATIO * 0.25)))

    async def _run_handler(self, tool: Tool, args: dict) -> str:
        return await asyncio.to_thread(tool.handler, args)

    async def _summarize_tool_result(self, result: str, tool_name: str) -> str:
        cap = self._max_tool_result_chars
        if not self.summary_model:
            return result[:cap] + f"\n...[truncated at {cap:,} chars]"

        chunk_size = self.summary_chunk_chars
        chunks = [result[i:i + chunk_size] for i in range(0, len(result), chunk_size)]
        n = len(chunks)
        per_chunk_tokens = max(50, min(self.summary_max_tokens, int(cap / (n * TOKEN_CHAR_RATIO))))

        async def _do(text: str, label: str) -> str:
            return await self._summarize([
                {"role": "system", "content": build_summary_prompt()},
                {"role": "user", "content": f"Summarize this '{tool_name}' output ({label}), preserving all technical details:\n\n{text}"},
            ], per_chunk_tokens)

        summaries = list(await asyncio.gather(*[_do(c, f"part {i+1}/{n}") for i, c in enumerate(chunks)]))
        valid = [s for s in summaries if s]
        if not valid:
            return result[:cap] + f"\n...[{len(result):,} chars — summarization failed, truncated]"
        combined = "\n\n".join(valid)
        if len(combined) > cap:
            final = await self._summarize([
                {"role": "system", "content": build_summary_prompt()},
                {"role": "user", "content": combined[:chunk_size]},
            ], self.summary_max_tokens)
            return final or combined[:cap] + "\n...[truncated]"
        return combined

    async def receive_response(self):
        for _ in range(self.max_tool_iterations):
            try:
                data = await self._call_local()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                body = e.response.text[:300]
                msg = ("Rate limited" if status == 429 else
                       "Auth error" if status == 401 else
                       f"HTTP {status}")
                yield ResultMessage(session_id=self.session_id, is_error=True,
                                    result=f"{msg} from {self.name} server: {body}")
                return
            except httpx.ConnectError as e:
                yield ResultMessage(session_id=self.session_id, is_error=True,
                                    result=f"Could not reach {self.name} server (connection refused): {e}")
                return
            except httpx.TimeoutException as e:
                yield ResultMessage(session_id=self.session_id, is_error=True,
                                    result=f"Timed out waiting for {self.name} server ({type(e).__name__}): {e}")
                return
            except Exception as e:
                yield ResultMessage(session_id=self.session_id, is_error=True,
                                    result=f"{self.name} client error: {e}")
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
                name = tc["function"]["name"]
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
                    print(f"[{self.name}] tool '{name}' returned {len(result_text):,} chars "
                          f"(cap {cap:,}) — summarizing")
                    result_text = await self._summarize_tool_result(result_text, name)

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_text,
                })
                if _is_skill_read:
                    # Activation is recorded only after the body was appended.
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
        await self._http.aclose()


# ---------------------------------------------------------------------------
# Permission callback
# ---------------------------------------------------------------------------

def _make_permission_callback(
    thread_id: str, post_fn, platform: PlatformConfig, runner_name: str,
    interrupt_requested: set[str],
):
    store = platform.session_store

    async def callback(tool_name: str, input_data: dict):
        try:
            target = (
                input_data.get("command", "") if tool_name == "Bash"
                else json.dumps(input_data, default=str)
            )
            print(f"[{runner_name}][permissions] {thread_id}: {tool_name} -> {target[:200]}")

            blocked = check_blocked(target, BLOCKED_PATTERNS)
            if blocked:
                store.append_audit_log(thread_id, "tool_call", {
                    "tool": tool_name, "target": target[:500], "decision": "blocked", "pattern": blocked,
                })
                await post_fn(
                    f"🚫 Blocked a forbidden operation (pattern `{blocked}`). "
                    f"Agent was told to find another way."
                )
                return PermissionResultDeny(
                    message=f"Operation blocked by security policy (matched: {blocked}). Do not retry it."
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
                    return PermissionResultDeny(message="The user did not answer in time. Proceed using best judgment.")
                answer = _resolve_ask_user_answer(input_data, raw_answer)
                return PermissionResultDeny(message=f"The user answered: {answer}")

            decision = permission_decision(tool_name, input_data)
            if decision == "deny":
                store.append_audit_log(thread_id, "tool_call", {
                    "tool": tool_name, "target": target[:500], "decision": "deny_rule",
                })
                return PermissionResultDeny(message="Denied by security policy rule. Do not retry it.")
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
            return PermissionResultAllow() if allowed else PermissionResultDeny(
                message="The user denied this action. Do not retry it; ask how to proceed."
            )

        except Exception as e:
            print(f"[{runner_name}][permissions] {thread_id}: CALLBACK ERROR: {e!r}")
            store.append_audit_log(thread_id, "tool_call", {
                "tool": tool_name, "decision": "error", "error": str(e),
            })
            try:
                await post_fn(f"⚠️ Permission system error for `{tool_name}`: `{str(e)[:300]}` — denied.")
            except Exception:
                pass
            return PermissionResultDeny(message=f"Permission system error ({e}). Action denied.")

    return callback


# ---------------------------------------------------------------------------
# LocalRunner — class-based (implements runners.base.Runner protocol)
# ---------------------------------------------------------------------------

class LocalRunner:
    """
    OpenAI-compatible runner for local LLM servers.

    Instantiate once per bot process. Use `from_env(name)` to build from
    environment variables (e.g., `LocalRunner.from_env("ollama")` reads
    OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_CONTEXT_WINDOW, …).
    """

    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        api_key: str = "local",
        summary_model: str = "",
        context_window: int = 32_768,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        summary_chunk_chars: int = 20_000,
        summary_max_tokens: int = 2048,
    ):
        self.name = name
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.summary_model = summary_model
        self.context_window = context_window
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.summary_chunk_chars = summary_chunk_chars
        self.summary_max_tokens = summary_max_tokens

        self._clients: dict[str, LocalAgentClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._current_tasks: dict[str, asyncio.Task] = {}
        self._interrupt_requested: set[str] = set()
        self._agentdb_middleware = None
        self._agentdb = None
        self._agentdb_api = AgentDBSurface()
        self._agent_id: str = "agent"
        self._turn_counters: dict[str, int] = {}
        self._session_workspace_paths: dict[str, str] = {}
        # Optional telemetry sink; None keeps default behaviour byte-identical.
        self._usage_sink = None

    @classmethod
    def from_env(cls, name: str) -> "LocalRunner":
        """Build a LocalRunner from the provider spec (e.g. 'ollama' -> OLLAMA_*)."""
        settings = settings_for(name.lower())
        return cls(
            name=settings.name,
            base_url=settings.base_url or "",
            model=settings.model or "",
            api_key=settings.api_key or "local",
            summary_model=settings.summary_model or "",
            context_window=settings.context_window or 32_768,
            temperature=settings.temperature if settings.temperature is not None else 0.2,
            max_tokens=settings.max_tokens or 4096,
            summary_chunk_chars=settings.summary_chunk_chars,
            summary_max_tokens=settings.summary_max_tokens or 2048,
        )

    def set_usage_sink(self, sink) -> None:
        """Install (or clear) a UsageRecord sink. None restores default behaviour."""
        self._usage_sink = sink

    def set_model(self, value: str | None) -> str:
        if value:
            self.model = value
        return self.model or f"(set {self.name.upper()}_MODEL env var)"

    @property
    def current_model_id(self) -> str:
        """The model id in use. Single accessor so bots never guess an attr name."""
        return self.model or ""

    def has_client(self, thread_id: str) -> bool:
        return thread_id in self._clients

    def init_agentdb(self, db, platform: PlatformConfig) -> None:
        import core.config as _cfg
        if not _cfg.AGENTDB_ENABLED:
            return
        try:
            from agentdb.adapters.middleware import AgentDBMiddleware
            from agentdb.adapters.capabilities import LOCAL_CAPABILITIES
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
            db=db, agent_id=platform.agent_id, capabilities=LOCAL_CAPABILITIES
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
                    llm_fn = build_consolidation_llm_fn(self.name)
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
        post_fn, platform: PlatformConfig,
    ) -> LocalAgentClient:
        if thread_id in self._clients:
            return self._clients[thread_id]

        if not self.base_url:
            raise RuntimeError(
                f"No server URL configured for the {self.name} runner. "
                f"Set {self.name.upper()}_BASE_URL in .env."
            )
        if not self.model:
            raise RuntimeError(
                f"No model configured for the {self.name} runner. "
                f"Set {self.name.upper()}_MODEL in .env."
            )

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
            from core.tool_pipeline import ToolResultPipeline
            _tool_pipeline = ToolResultPipeline(
                store.base, session_id=thread_id, workspace_id=workspace_path,
            )
        client = LocalAgentClient(
            name=self.name,
            base_url=self.base_url,
            model=self.model,
            api_key=self.api_key,
            summary_model=self.summary_model,
            context_window=self.context_window,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            summary_chunk_chars=self.summary_chunk_chars,
            summary_max_tokens=self.summary_max_tokens,
            tools=build_tools(
                workspace_path, store,
                output_cap=_OUTPUT_CAP, bash_timeout=_BASH_TIMEOUT_SECONDS,
                pipeline=_tool_pipeline,
                skills_catalog=skills_ctx.catalog if skills_ctx.enabled else None,
            ),
            system_prompt=system_prompt,
            can_use_tool=_make_permission_callback(
                thread_id, post_fn, platform, self.name, self._interrupt_requested,
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
                    thread_id, workspace_path, post_fn, platform,
                )

                # Mode C: //skill-name invocation (shared parsing/activation).
                # Resolved before memory/nudge so the task text and explicit
                # skill message are final for this turn.
                invocation = resolve_invocation(
                    prompt, client._skills_context, client._skill_session,
                    client._skill_turn + 1,
                )
                if invocation.error_reply is not None:
                    return invocation.error_reply, client.session_id
                prompt = invocation.task

                memory_context = None
                if self._agentdb_middleware is not None:
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

                if _cfg_repo_map_enabled():
                    try:
                        from core.repo_map import build_repo_map_segment
                        _seg = build_repo_map_segment(workspace_path)
                        if _seg:
                            memory_context = ((memory_context + "\n\n") if memory_context else "") + _seg
                    except Exception:
                        pass

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
                            raise RuntimeError(message.result or f"{self.name} agent error")

                response_text = "\n".join(text_parts).strip()
                if self._agentdb_middleware is not None:
                    dirs = _extract_dirs(response_text)
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
                raise RuntimeError(f"{self.name} error: {e}") from e
            finally:
                self._current_tasks.pop(thread_id, None)
                self._interrupt_requested.discard(thread_id)
