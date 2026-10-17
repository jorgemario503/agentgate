"""
Fireworks runner — OpenAI-compatible chat-completions endpoint.

No `claude` binary required. Bring your own FIREWORKS_API_KEY.

Requires: pip install "fireworks-ai>=1.2.0"
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import subprocess
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

try:
    import fireworks
    from fireworks import AsyncFireworks
    _FIREWORKS_AVAILABLE = True
except ImportError:
    _FIREWORKS_AVAILABLE = False
    fireworks = None  # type: ignore
    AsyncFireworks = None  # type: ignore

import core.approvals as approvals
from core.config import (
    ADDITIONAL_DIRS,
    AGENTDB_ENABLED,
    BLOCKED_PATTERNS,
    CLAUDE_SETTINGS,
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


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelSpec:
    name: str
    model_id: str
    context_window: int


DEFAULT_MODEL_NAME = "minimax-m3"
DEFAULT_CONTEXT_WINDOW = 512_000

TEMPERATURE = float(os.environ.get("FIREWORKS_TEMPERATURE", "0.2"))
MAX_TOKENS = int(os.environ.get("FIREWORKS_MAX_TOKENS", "4096"))


def _parse_full_model_id(model_id: str) -> tuple[str, str] | None:
    parts = model_id.split("/")
    if len(parts) == 4 and parts[0] == "accounts" and parts[2] == "models":
        return parts[1], parts[3]
    return None


import re as _re


def _extract_files(text: str) -> list[str]:
    return _re.findall(r'(?:[\w./\-]+/[\w.\-]+\.\w{1,6}|/[\w./\-]+\.\w{1,6})', text)


def _extract_dirs(text: str) -> list[str]:
    return _re.findall(r'/(?:[\w./\-]+/)+', text)


def resolve_model(value: str | None) -> ModelSpec:
    raw = value or DEFAULT_MODEL_NAME
    parsed = _parse_full_model_id(raw)
    if parsed:
        _account_id, name = parsed
        model_id = raw
    else:
        name = raw
        model_id = f"accounts/fireworks/models/{raw}"
    return ModelSpec(name, model_id, DEFAULT_CONTEXT_WINDOW)


# ---------------------------------------------------------------------------
# History trimming / compaction
# ---------------------------------------------------------------------------

TOKEN_CHAR_RATIO = 3.5
HISTORY_RESERVE_TOKENS = 2000

SUMMARY_MODEL_ID = os.environ.get(
    "FIREWORKS_SUMMARY_MODEL", "accounts/fireworks/models/llama-v3p1-8b-instruct"
)
SUMMARY_MAX_TOKENS = int(os.environ.get("FIREWORKS_SUMMARY_MAX_TOKENS", "3072"))
SUMMARY_TEMPERATURE = 0.2
SUMMARY_CHUNK_CHARS = int(os.environ.get("FIREWORKS_SUMMARY_CHUNK_CHARS", str(20_000)))
COMPACT_TRIGGER_RATIO = 0.7
COMPACT_TARGET_RATIO = 0.5

_SUMMARY_SYSTEM_PROMPT = (
    "You are condensing an earlier portion of an ongoing coding-agent "
    "conversation so it can be removed from the live context. Preserve "
    "all information needed to continue the conversation correctly, "
    "including: user goals and constraints; decisions made and the "
    "reasoning behind them when it affects future work; concrete facts "
    "(file paths, function names, class names, APIs, identifiers, URLs, "
    "numbers, versions, commands, errors, and configuration values); "
    "code changes already made; remaining tasks, open questions, and "
    "known issues. Omit routine tool chatter, acknowledgements, repeated "
    "information, and other conversational filler. Be concise, but do "
    "not sacrifice important technical detail for brevity. Write plain "
    "prose with no headings or preamble."
)


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


class JsonFileSessionStore(SessionStore):
    def __init__(self, directory: str | Path = "./fw_sessions"):
        self.directory = Path(directory)

    def _path(self, session_id: str) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        return self.directory / f"{session_id}.json"

    def load(self, session_id: str) -> list[dict]:
        path = self._path(session_id)
        if path.exists():
            return json.loads(path.read_text())
        return []

    def save(self, session_id: str, messages: list[dict]) -> None:
        self._path(session_id).write_text(json.dumps(messages, indent=2))


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
# Tool registry
# ---------------------------------------------------------------------------

@dataclass
class Tool:
    schema: dict
    handler: Callable[[dict], str]


def _tool_schema(name: str, description: str, params: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": params, "required": required},
        },
    }


_OUTPUT_CAP = 8000


def _resolve(cwd: str, path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (Path(cwd) / p)


def _make_bash(cwd: str) -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        cmd = args.get("command", "")
        if not cmd:
            return "ERROR: no command provided"
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            return "ERROR: command timed out after 120s"
        except Exception as e:
            return f"ERROR: {e}"
        out = proc.stdout or ""
        if proc.stderr:
            out += f"\n[stderr]\n{proc.stderr}"
        if proc.returncode != 0:
            out += f"\n[exit {proc.returncode}]"
        return out[:_OUTPUT_CAP] or "(no output)"
    return handler


def _make_read(cwd: str) -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        try:
            return _resolve(cwd, args.get("file_path", "")).read_text()[:_OUTPUT_CAP]
        except Exception as e:
            return f"ERROR reading {args.get('file_path')}: {e}"
    return handler


def _make_write(cwd: str) -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        try:
            target = _resolve(cwd, args.get("file_path", ""))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(args.get("content", ""))
            return f"Wrote {target}"
        except Exception as e:
            return f"ERROR writing {args.get('file_path')}: {e}"
    return handler


def _make_edit(cwd: str) -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        try:
            target = _resolve(cwd, args.get("file_path", ""))
            text = target.read_text()
            old, new = args.get("old_string", ""), args.get("new_string", "")
            count = text.count(old)
            if not old or count == 0:
                return f"ERROR: old_string not found in {target}"
            if count > 1:
                return f"ERROR: old_string is ambiguous ({count} matches) in {target}"
            target.write_text(text.replace(old, new, 1))
            return f"Edited {target}"
        except Exception as e:
            return f"ERROR editing {args.get('file_path')}: {e}"
    return handler


def _make_glob(cwd: str) -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        pattern = args.get("pattern", "")
        base = _resolve(cwd, args.get("path", ".")) if args.get("path") else Path(cwd)
        try:
            matches = sorted(str(p) for p in base.glob(pattern))
        except Exception as e:
            return f"ERROR in glob: {e}"
        return "\n".join(matches[:200]) or "(no matches)"
    return handler


def _make_grep(cwd: str) -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        pattern = args.get("pattern", "")
        base = _resolve(cwd, args.get("path", ".")) if args.get("path") else Path(cwd)
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"ERROR: bad regex: {e}"
        hits: list[str] = []
        files = [base] if base.is_file() else base.rglob("*")
        for f in files:
            if not f.is_file():
                continue
            try:
                for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{f}:{i}:{line[:200]}")
                        if len(hits) >= 200:
                            return "\n".join(hits) + "\n(truncated at 200)"
            except Exception:
                continue
        return "\n".join(hits) or "(no matches)"
    return handler


def _make_lookup_directory(store) -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        path = store.lookup_directory(args.get("name", ""))
        return path or f"No directory registered under '{args.get('name')}'."
    return handler


def _make_remember_directory(store) -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        store.remember_directory(args.get("name", ""), args.get("path", ""))
        return f"Remembered '{args.get('name')}' -> {args.get('path')}"
    return handler


_ALWAYS_ALLOW_TOOLS = {"lookup_directory", "remember_directory"}


def build_tools(workspace_path: str, store) -> dict[str, Tool]:
    def t(name, desc, params, required, handler):
        return Tool(schema=_tool_schema(name, desc, params, required), handler=handler)

    return {
        "Bash": t(
            "Bash", "Run a shell command in the session workspace.",
            {"command": {"type": "string"}}, ["command"], _make_bash(workspace_path),
        ),
        "Read": t(
            "Read", "Read a text file from the local filesystem.",
            {"file_path": {"type": "string"}}, ["file_path"], _make_read(workspace_path),
        ),
        "Write": t(
            "Write", "Write (overwrite) a text file on the local filesystem.",
            {"file_path": {"type": "string"}, "content": {"type": "string"}},
            ["file_path", "content"], _make_write(workspace_path),
        ),
        "Edit": t(
            "Edit", "Replace one exact occurrence of old_string with new_string in a file.",
            {"file_path": {"type": "string"}, "old_string": {"type": "string"},
             "new_string": {"type": "string"}},
            ["file_path", "old_string", "new_string"], _make_edit(workspace_path),
        ),
        "Glob": t(
            "Glob", "List files matching a glob pattern.",
            {"pattern": {"type": "string"}, "path": {"type": "string"}},
            ["pattern"], _make_glob(workspace_path),
        ),
        "Grep": t(
            "Grep", "Search file contents for a regular expression.",
            {"pattern": {"type": "string"}, "path": {"type": "string"}},
            ["pattern"], _make_grep(workspace_path),
        ),
        "lookup_directory": t(
            "lookup_directory", "Resolve a known directory name to its absolute path.",
            {"name": {"type": "string"}}, ["name"], _make_lookup_directory(store),
        ),
        "remember_directory": t(
            "remember_directory", "Persist a name -> absolute path mapping for future lookups.",
            {"name": {"type": "string"}, "path": {"type": "string"}},
            ["name", "path"], _make_remember_directory(store),
        ),
    }


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
# FireworksAgentClient
# ---------------------------------------------------------------------------

CanUseTool = Callable[[str, dict], Awaitable[Any]]

_BASE_SYSTEM_PROMPT = (
    "You are a coding assistant operating in a local workspace via tools. "
    "Available tools: Bash (shell), Read, Write, Edit, Glob, Grep, and "
    "lookup_directory / remember_directory. Prefer Read/Glob/Grep for inspection "
    "and Edit for surgical changes; use Bash for everything else. When you refer to "
    "a directory by name, call lookup_directory first before searching."
)


def _safe_json_args(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


class FireworksAgentClient:
    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        tools: Optional[dict[str, Tool]] = None,
        system_prompt: Optional[str] = None,
        can_use_tool: Optional[CanUseTool] = None,
        session_id: Optional[str] = None,
        session_store: Optional[SessionStore] = None,
        max_tool_iterations: int = 25,
        context_window: int = DEFAULT_CONTEXT_WINDOW,
        on_compact: Optional[Callable[[str], None]] = None,
    ):
        self.model = model
        self.context_window = context_window
        api_key = api_key or os.environ.get("FIREWORKS_API_KEY")
        if not api_key:
            raise ValueError("Set FIREWORKS_API_KEY or pass api_key=")
        if not _FIREWORKS_AVAILABLE:
            raise RuntimeError(
                "fireworks-ai is not installed — pip install 'fireworks-ai>=1.2.0'"
            )
        self._client = AsyncFireworks(api_key=api_key)
        self.tools = tools if tools is not None else {}
        self.system_prompt = system_prompt
        self.can_use_tool = can_use_tool
        self.session_id = session_id or str(uuid.uuid4())
        self.session_store: SessionStore = session_store or JsonFileSessionStore()
        self.max_tool_iterations = max_tool_iterations
        self.messages: list[dict] = []
        self._ephemeral_context: Optional[str] = None
        self.on_compact = on_compact

    async def connect(self) -> None:
        self.messages = self.session_store.load(self.session_id)
        if self.system_prompt and not any(m.get("role") == "system" for m in self.messages):
            self.messages.insert(0, {"role": "system", "content": self.system_prompt})

    async def query(self, prompt: str, ephemeral_context: Optional[str] = None) -> None:
        self.messages.append({"role": "user", "content": prompt})
        self._ephemeral_context = ephemeral_context

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
        budget = max(self.context_window - MAX_TOKENS - HISTORY_RESERVE_TOKENS - ephemeral_tokens, MAX_TOKENS)

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
            print(f"[fireworks] trimmed {dropped} oldest turn(s) from session {self.session_id}")

        self.messages = ([system_msg] if system_msg else []) + [m for turn in kept for m in turn]

    def _build_outgoing_messages(self) -> list[dict]:
        if not self._ephemeral_context:
            return self.messages
        messages = list(self.messages)
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                messages.insert(i, {"role": "system", "content": self._ephemeral_context})
                break
        return messages

    async def _maybe_compact_history(self) -> None:
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
        budget = max(self.context_window - MAX_TOKENS - HISTORY_RESERVE_TOKENS - ephemeral_tokens, MAX_TOKENS)
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
                max_tokens=SUMMARY_MAX_TOKENS,
                temperature=SUMMARY_TEMPERATURE,
                messages=[
                    {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": transcript},
                ],
            )
            summary = completion.to_dict()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"[fireworks] compaction failed for session {self.session_id}: {e}")
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
        print(f"[fireworks] compacted {n_compacted} oldest turn(s) in session {self.session_id}")
        if self.on_compact is not None:
            try:
                self.on_compact(summary)
            except Exception as e:
                print(f"[agentdb] on_compact callback error: {e}")

    async def _call_fireworks(self) -> dict:
        await self._maybe_compact_history()
        self._trim_history()
        outgoing = self._build_outgoing_messages()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "temperature": TEMPERATURE,
            "messages": outgoing,
        }
        if self.tools:
            kwargs["tools"] = [t.schema for t in self.tools.values()]
        completion = await self._client.chat.completions.create(**kwargs)
        return completion.to_dict()

    @property
    def _max_tool_result_chars(self) -> int:
        budget = max(self.context_window - MAX_TOKENS - HISTORY_RESERVE_TOKENS, MAX_TOKENS)
        return max(8_000, min(100_000, int(budget * TOKEN_CHAR_RATIO * 0.25)))

    async def _run_handler(self, tool: Tool, args: dict) -> str:
        return await asyncio.to_thread(tool.handler, args)

    async def _summarize_tool_result(self, result: str, tool_name: str) -> str:
        cap = self._max_tool_result_chars
        chunk_size = SUMMARY_CHUNK_CHARS
        chunks = [result[i:i + chunk_size] for i in range(0, len(result), chunk_size)]
        n = len(chunks)
        per_chunk_tokens = max(50, min(SUMMARY_MAX_TOKENS, int(cap / (n * TOKEN_CHAR_RATIO))))

        async def _do_summarize(text: str, label: str, max_tok: int) -> str:
            try:
                completion = await self._client.chat.completions.create(
                    model=SUMMARY_MODEL_ID,
                    max_tokens=max_tok,
                    temperature=SUMMARY_TEMPERATURE,
                    messages=[
                        {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                        {"role": "user", "content": (
                            f"Summarize this '{tool_name}' tool output ({label}), "
                            f"preserving all technical details:\n\n{text}"
                        )},
                    ],
                )
                return completion.to_dict()["choices"][0]["message"]["content"].strip()
            except Exception as e:
                print(f"[fireworks] tool result summarization failed ({label}): {e}")
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
                data = await self._call_fireworks()
            except Exception as e:
                if _FIREWORKS_AVAILABLE and fireworks is not None:
                    for exc_type in (
                        fireworks.RateLimitError,
                        fireworks.AuthenticationError,
                        fireworks.APIStatusError,
                        fireworks.APIConnectionError,
                    ):
                        if isinstance(e, exc_type):
                            yield ResultMessage(
                                session_id=self.session_id, is_error=True,
                                result=f"Fireworks API error: {e}",
                            )
                            return
                yield ResultMessage(
                    session_id=self.session_id, is_error=True,
                    result=f"Fireworks error: {e}",
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

                cap = self._max_tool_result_chars
                if len(result_text) > cap:
                    print(f"[fireworks] tool '{name}' returned {len(result_text):,} chars "
                          f"(cap {cap:,}) — chunk-summarizing")
                    result_text = await self._summarize_tool_result(result_text, name)

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_text,
                })

        yield ResultMessage(
            session_id=self.session_id, is_error=True,
            result=f"Hit max_tool_iterations={self.max_tool_iterations} without a final answer",
        )

    async def disconnect(self) -> None:
        self.session_store.save(self.session_id, self.messages)
        await self._client.close()


# ---------------------------------------------------------------------------
# FireworksRunner — class-based API (implements runners.base.Runner protocol)
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


class FireworksRunner:
    """Class-based Fireworks runner. One instance per bot process."""

    def __init__(self, model: str | None = None):
        self._model: ModelSpec = resolve_model(
            model or os.environ.get("FIREWORKS_MODEL")
        )
        self._clients: dict[str, FireworksAgentClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._current_tasks: dict[str, asyncio.Task] = {}
        self._interrupt_requested: set[str] = set()
        self._agentdb_middleware = None
        self._agentdb = None
        self._agent_id: str = "agent"
        self._turn_counters: dict[str, int] = {}
        self._session_workspace_paths: dict[str, str] = {}

    async def set_model(self, value: str | None) -> ModelSpec:
        """Resolve model and optionally look up its context window from Fireworks."""
        spec = resolve_model(value)
        api_key = os.environ.get("FIREWORKS_API_KEY")
        if api_key and _FIREWORKS_AVAILABLE:
            try:
                client = AsyncFireworks(api_key=api_key)
                parsed = _parse_full_model_id(spec.model_id)
                account_id, name = parsed if parsed else ("fireworks", spec.model_id)
                try:
                    model = await client.models.get(name, account_id=account_id)
                except fireworks.NotFoundError:
                    model = None
                await client.close()
                context_length = getattr(model, "context_length", None) if model else None
                if context_length:
                    spec = ModelSpec(spec.name, spec.model_id, context_length)
                elif model is None:
                    print(f"[fireworks] warning: '{spec.model_id}' not found in catalog")
            except Exception as e:
                print(f"[fireworks] warning: could not confirm context window for "
                      f"'{spec.model_id}' ({e}); using fallback {spec.context_window}")
        self._model = spec
        return self._model

    def has_client(self, thread_id: str) -> bool:
        return thread_id in self._clients

    def init_agentdb(self, db, platform: PlatformConfig) -> None:
        import core.config as _cfg
        if not _cfg.AGENTDB_ENABLED:
            return
        try:
            from agentdb.adapters.middleware import AgentDBMiddleware
            from agentdb.adapters.capabilities import FIREWORKS_CAPABILITIES
        except ImportError:
            return
        self._agent_id = platform.agent_id
        self._agentdb = db
        self._agentdb_middleware = AgentDBMiddleware(
            db=db, agent_id=platform.agent_id, capabilities=FIREWORKS_CAPABILITIES
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
                    import asyncio as _asyncio
                    _asyncio.create_task(self._agentdb_middleware.end_session(thread_id, jsonl_path))
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
    ) -> FireworksAgentClient:
        if thread_id in self._clients:
            return self._clients[thread_id]

        store = platform.session_store
        history_store = JsonFileSessionStore(directory=f"{store.base}/fw_history")
        system_prompt = f"{_BASE_SYSTEM_PROMPT}\n\n{SYSTEM_PROMPT}"

        # Local models can't call agentdb tools mid-turn, so pre-load a memory
        # snapshot into the system prompt at session start.
        if self._agentdb_middleware is not None:
            try:
                init_messages, _ = await self._agentdb_middleware.prepare_turn_async(
                    user_query="",
                    session_id=thread_id,
                    working_dirs=[workspace_path] + list(ADDITIONAL_DIRS),
                )
                init_parts = [
                    m["content"] for m in init_messages
                    if m.get("role") in ("system", "user") and m.get("content", "").strip()
                ]
                if init_parts:
                    system_prompt = (
                        f"{system_prompt}\n\n"
                        f"## Memory from previous sessions\n"
                        + "\n\n".join(init_parts)
                    )
            except Exception as e:
                print(f"[agentdb] session-start memory load error: {e}")

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

        client = FireworksAgentClient(
            model=self._model.model_id,
            context_window=self._model.context_window,
            tools=build_tools(workspace_path, store),
            system_prompt=system_prompt,
            can_use_tool=_make_permission_callback(
                thread_id, post_fn, platform, self._interrupt_requested,
            ),
            session_id=thread_id,
            session_store=history_store,
            on_compact=on_compact,
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
                    thread_id, workspace_path, resume_session_id, post_fn, platform,
                )

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

                await client.query(prompt, ephemeral_context=memory_context)

                text_parts: list[str] = []
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                text_parts.append(block.text)
                    elif isinstance(message, ResultMessage):
                        if message.is_error:
                            raise RuntimeError(message.result or "Fireworks agent error")

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
                # Return resume_session_id unchanged — Fireworks uses its own
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
                raise RuntimeError(f"Fireworks error: {e}") from e
            finally:
                self._current_tasks.pop(thread_id, None)
                self._interrupt_requested.discard(thread_id)
