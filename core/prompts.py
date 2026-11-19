"""System prompt assembly for the non-Claude runners, in one place.

Before this module the non-Claude runners each concatenated their own base
prompt with `core.config.SYSTEM_PROMPT` inline (triplicated), and there was no
prompt surface that told a model it was *resuming* a conversation.

Sections are emitted most-stable first, because providers cache a prompt by
matching from the start and invalidate it on the first differing character:

    1. base rules      - stable across every turn of every session
    2. resume block    - stable for the life of one session (cold resume only)
    3. AgentDB memory  - may vary per session

Per-turn memory is *not* handled here; the runners inject it as an ephemeral
system message immediately before the latest user turn, so it never disturbs
the cached prefix.

Every optional section degrades independently. A missing or unreadable history
file, an uninitialized AgentDB, or a failure inside AgentDB removes only that
section -- never the whole prompt.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from core.config import SYSTEM_PROMPT as _CONFIG_SYSTEM_PROMPT
from core.sessions import SessionState

#: Where the prompt files live, and the variable that can relocate them.
#: Read at call time so an operator can edit a file without touching source,
#: and so tests can point it elsewhere.
PROMPT_DIR = Path("prompts")
PROMPT_ENV = "AGENTGATE_PROMPTS_DIR"

#: Override file names, one per surface.
BASE_FILE = "base.md"
HARD_RULES_FILE = "hard_rules.md"
RESUME_FILE = "resume.md"
SUMMARY_FILE = "summary.md"

#: Fixed section headings. These are structural, not content: they delimit
#: each injected section so the model can tell them apart, and code (and the
#: tests) rely on them. Operators override the *text* of a section, never its
#: heading.
SECTION_HEADINGS = {
    "base": "## Base instructions",
    "hard_rules": "## Hard rules",
    "resume": "## Resuming an existing session",
    "memory": "## Memory from previous sessions",
    "summary": "## Summarisation instructions",
}

#: Minimal fallbacks, used only when the corresponding file is absent, empty
#: or unreadable. The shipped files under `prompts/` are the real definitions;
#: these exist so a deleted file degrades to working defaults rather than to
#: an empty prompt. Keep them short and in sync with the files' intent, not
#: with their exact wording.
FALLBACK_BASE = (
    "You are a coding assistant operating in a local workspace via tools. "
    "Available tools: Bash (shell), Read, Write, Edit, Glob, Grep, and "
    "lookup_directory / remember_directory. Prefer Read/Glob/Grep for inspection "
    "and Edit for surgical changes; use Bash for everything else. When you refer to "
    "a directory by name, call lookup_directory first before searching."
)

#: Fallback hard rules. The authoritative copy is prompts/hard_rules.md; this
#: is `core.config.SYSTEM_PROMPT`, kept only so a deleted file still yields
#: working rules. Claude and the stateless runners both read the file, so they
#: cannot drift apart.
FALLBACK_HARD_RULES = _CONFIG_SYSTEM_PROMPT

#: Fallback opening lines for the resume block.
FALLBACK_RESUME = (
    "You are continuing a conversation that started earlier. Do not greet the\n"
    "user or re-introduce yourself."
)

FALLBACK_SUMMARY = (
    "You are condensing an earlier portion of an ongoing coding-agent "
    "conversation so it can be removed from the live context. Preserve user "
    "goals, decisions and their reasoning, concrete technical facts, code "
    "changes, remaining tasks and open questions. Omit chatter and repeated "
    "information. Write plain prose with no headings or preamble."
)

#: How many trailing turns the resume block replays inline.
RESUME_TAIL_TURNS = 2

#: Cap on the rendered turn digest, in characters, so a huge turn cannot
#: blow the context budget.
RESUME_MAX_CHARS = 4000

#: Rendered when the history file cannot be read. Naming the path still helps:
#: the model can try to read it itself.
_HISTORY_UNAVAILABLE = "(could not be read; read it directly if you need detail)"


def _strip_comments(text: str) -> str:
    """Drop a leading `#` comment block so file headers stay out of the prompt.

    Prompt files carry operator notes at the top (including the prefix-cache
    rule). Sending those to the model would waste tokens and add noise, so the
    leading comment lines are removed, along with surrounding blank lines.
    """
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (lines[i].lstrip().startswith("#") or not lines[i].strip()):
        i += 1
    return "\n".join(lines[i:]).strip()


def _prompt_dir() -> Path:
    override = os.environ.get(PROMPT_ENV)
    return Path(override) if override else PROMPT_DIR


def _override(filename: str) -> str:
    """Read a prompt override, or "" when absent, empty, or unreadable.

    Returning "" makes the caller fall back to the built-in text. An empty
    override must never win: that would silently erase the hard rules.
    """
    try:
        text = _strip_comments((_prompt_dir() / filename).read_text())
    except OSError:
        return ""
    except Exception as e:  # unexpected read failure must not take startup down
        print(f"[prompts] could not read {filename}: {e}")
        return ""
    return text


def _section(filename: str, builtin: str) -> str:
    """The override for `filename`, falling back to `builtin`."""
    return _override(filename) or builtin


def _heading(key: str) -> str:
    return SECTION_HEADINGS[key]


def build_base_prompt() -> str:
    """The base instructions and hard rules, each under a fixed heading.

    The heading is added by this function, not stored in the override file, so
    a customised file cannot break the section structure. The *content* of each
    section can be overridden via `PROMPT_DIR`; a missing, empty, or unreadable
    file falls back to the built-in text.
    """
    base = _section(BASE_FILE, FALLBACK_BASE)
    rules = build_hard_rules()
    return f"{_heading('base')}\n{base}\n\n{_heading('hard_rules')}\n{rules}"


def build_hard_rules() -> str:
    """The hard rules, from prompts/hard_rules.md.

    Shared by every runner: the Claude runner appends these to its preset
    prompt, and the stateless runners include them in the base prompt. One
    file means one edit reaches all of them.
    """
    return _section(HARD_RULES_FILE, FALLBACK_HARD_RULES)


def build_summary_prompt() -> str:
    """Instructions for the compaction summariser.

    Centralised here with the other prompt surfaces; the three runners used to
    keep drifting copies. Overridable like the rest.
    """
    return _section(SUMMARY_FILE, FALLBACK_SUMMARY)


def _load_messages(history_path: str) -> list[dict]:
    """Read the runner's history file, returning [] on any problem."""
    try:
        raw = Path(history_path).read_text()
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [m for m in data if isinstance(m, dict)]


def _turn_groups(messages: list[dict]) -> list[list[dict]]:
    """Group into turns, each starting at a user message.

    Tool results and assistant messages belong to the turn whose user message
    preceded them, matching the runners' own trimming logic.
    """
    turns: list[list[dict]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            continue
        if role == "user" or not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    return turns


def _render_turn(turn: list[dict]) -> str:
    """Render one turn as text, skipping tool output.

    Tool results are deliberately not replayed: they dominate the token count
    and the model can read the file when it actually needs them.
    """
    parts: list[str] = []
    for message in turn:
        role = message.get("role")
        if role == "tool":
            continue
        content = message.get("content")
        if isinstance(content, list):  # Anthropic-style content blocks
            content = " ".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict)
            )
        if not content:
            continue
        text = str(content).strip()
        if not text:
            continue
        label = "User" if role == "user" else "Assistant"
        parts.append(f"{label}: {text}")
    return "\n".join(parts)


def build_resume_block(
    history_path: str,
    workspace_path: str,
    last_turns: int = RESUME_TAIL_TURNS,
    max_chars: int = RESUME_MAX_CHARS,
) -> str:
    """Describe the resumed session and replay its final turns.

    Never raises. If the history cannot be read, the path is still named so the
    model can fetch it.
    """
    intro_body = _section(RESUME_FILE, FALLBACK_RESUME)
    header = [
        f"{_heading('resume')}\n{intro_body}",
        "",
        f"- Workspace: {workspace_path}",
        f"- Full conversation history: {history_path}",
        "  Format: a JSON array of message objects, each "
        '`{"role": ..., "content": ...}`.',
        '  `role` is one of "system", "user", "assistant", or "tool".',
        '  Entries with `role: "tool"` hold tool output from earlier turns.',
        "  Read that file if you need detail beyond the summary below.",
        "",
    ]

    messages = _load_messages(history_path)
    turns = _turn_groups(messages)
    if not turns:
        header.append(f"(The history file {_HISTORY_UNAVAILABLE})")
        return "\n".join(header)

    digest = [_render_turn(t) for t in turns[-last_turns:]]
    rendered = "\n\n".join(d for d in digest if d)
    if not rendered:
        header.append(f"(The history file {_HISTORY_UNAVAILABLE})")
        return "\n".join(header)

    if len(rendered) > max_chars:
        rendered = rendered[:max_chars].rstrip() + "\n[...truncated]"

    header.append("### Most recent turns")
    header.append(rendered)
    return "\n".join(header)


def build_agentdb_block(
    middleware: Any,
    session_id: str,
    workspace_path: str,
) -> str:
    """Memory records recalled by AgentDB, as a prompt section.

    Returns "" when AgentDB is unavailable, returns nothing, or raises. A
    failure here must never affect the rest of the prompt.
    """
    if middleware is None:
        return ""
    try:
        result = middleware.prepare_turn(
            user_query="",
            session_id=session_id,
            working_dirs=[workspace_path],
        )
    except Exception:
        return ""

    try:
        messages = result[0] if isinstance(result, tuple) else result
    except (IndexError, TypeError):
        return ""
    if not isinstance(messages, list):
        return ""

    parts = [
        str(m.get("content", "")).strip()
        for m in messages
        if isinstance(m, dict) and m.get("role") in ("system", "user")
    ]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    return f"{_heading('memory')}\n" + "\n\n".join(parts)


def apply_system_prompt(messages: list[dict], system_prompt: Optional[str]) -> list[dict]:
    """Place `system_prompt` at the head of a reloaded message list.

    The saved history always begins with the system message from the process
    that wrote it, so inserting only when absent meant a freshly assembled
    prompt -- including the resume block and AgentDB memory -- was silently
    discarded on every resume. The system prompt is authoritative: slot 0 is
    replaced.

    An empty or absent prompt leaves the saved messages untouched, so a client
    built without a prompt cannot destroy the one already on disk.
    """
    if not system_prompt:
        return list(messages)
    if messages and messages[0].get("role") == "system":
        return [{"role": "system", "content": system_prompt}] + list(messages[1:])
    return [{"role": "system", "content": system_prompt}] + list(messages)


def build_system_prompt(
    state: SessionState,
    history_path: Optional[str] = None,
    workspace_path: str = "",
    agentdb_middleware: Any = None,
    session_id: Optional[str] = None,
) -> str:
    """Assemble the prompt in stable-to-volatile order.

    The resume block is included only for states that resume; AgentDB memory is
    included only when it yields something. Each section is dropped on failure
    without affecting the others.
    """
    sections = [build_base_prompt()]

    if state.resumes and history_path:
        try:
            resume = build_resume_block(history_path, workspace_path)
        except Exception:
            resume = ""
        if resume:
            sections.append(resume)

    if agentdb_middleware is not None:
        block = build_agentdb_block(
            agentdb_middleware,
            session_id=session_id or "",
            workspace_path=workspace_path,
        )
        if block:
            sections.append(block)

    return "\n\n".join(s for s in sections if s.strip())
