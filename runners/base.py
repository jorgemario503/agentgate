"""
Shared types and utilities for all AgentGate runner backends.

PlatformConfig — everything a runner needs to know about which platform
    (Telegram, Slack, WhatsApp, ...) is calling it: session store, reply
    hint text, AgentDB agent_id, and allowlists.

TaskInterrupted — raised when a turn was cancelled by the user rather than
    failing normally.

check_blocked — pattern-matching gate applied to both user messages and
    every Bash command Claude generates (Layer 1 security).

Runner Protocol — structural interface every runner backend must implement.
    Bots receive a runner instance; run.py selects and instantiates one at
    startup. No more `import runner_xyz as runner` module-level globals.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from core.sessions import SessionStore


class TaskInterrupted(RuntimeError):
    """Raised when a turn was cancelled via cancel() rather than failing."""


@dataclass(frozen=True)
class PlatformConfig:
    """
    Everything a runner needs to know about which platform a session belongs
    to: which SessionStore backs its audit log, what reply_hint text to show
    in approval prompts, what AgentDB agent_id to register under, and the
    platform's own user allowlists.

    One instance per platform, built by run.py and passed through to the bot
    and then to every runner call.
    """
    session_store: "SessionStore"
    reply_hint: str = "`yes` to allow or `no` to deny"
    agent_id: str = "agent"
    allowed_user_ids: frozenset[str] = field(default_factory=frozenset)
    admin_user_ids: frozenset[str] = field(default_factory=frozenset)


def check_blocked(text: str, patterns: list[str]) -> str | None:
    """Return the first matching forbidden pattern, or None if safe.

    Applied to both incoming user messages and every Bash command Claude
    generates (Layer 1 security — no override possible).
    """
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return pattern
    return None


# ---------------------------------------------------------------------------
# Approval-flow formatters — shared by every runner's permission callback.
# ---------------------------------------------------------------------------

def _describe_ask_user_question(input_data: dict) -> str:
    """Render an AskUserQuestion call as platform-neutral text."""
    questions = input_data.get("questions", [])
    blocks = []
    for q in questions:
        header = q.get("header", "").strip()
        lines = [f"*{header}*" if header else "*Question*", q.get("question", "").strip()]
        for i, opt in enumerate(q.get("options", []), start=1):
            label = opt.get("label", "")
            desc = opt.get("description", "")
            lines.append(f"{i}. *{label}* — {desc}" if desc else f"{i}. *{label}*")
        blocks.append("\n".join(lines))
    text = "\n\n".join(blocks)
    if len(questions) > 1:
        text += (
            "\n\n_Reply with one number per question, in order, separated by "
            'commas (e.g. "1, 2") to pick by number — or answer in plain text._'
        )
    return text


def _resolve_ask_user_answer(input_data: dict, answer: str) -> str:
    """Map a numeric reply back to its option label(s); pass through free text."""
    questions = input_data.get("questions", [])
    stripped = answer.strip()
    if not questions:
        return stripped
    tokens = [t for t in re.split(r"[,;\s]+", stripped) if t]
    if len(tokens) != len(questions) or not all(t.isdigit() for t in tokens):
        return stripped
    labels = []
    for q, token in zip(questions, tokens):
        options = q.get("options", [])
        idx = int(token) - 1
        if not (0 <= idx < len(options)):
            return stripped
        labels.append(options[idx].get("label", token))
    if len(questions) == 1:
        return labels[0]
    return "; ".join(
        f"{(q.get('header') or q.get('question') or f'Q{i + 1}').strip()}: {label}"
        for i, (q, label) in enumerate(zip(questions, labels))
    )


def _describe_tool_call(tool_name: str, input_data: dict) -> str:
    """Human-readable description of what the agent wants to do."""
    if tool_name == "Bash":
        cmd = input_data.get("command", "")
        return f"Run command:\n```{cmd[:800]}```"
    if tool_name == "AskUserQuestion":
        return _describe_ask_user_question(input_data)
    import json
    pretty = json.dumps(input_data, indent=2, default=str)
    return f"Use tool `{tool_name}`:\n```{pretty[:800]}```"


@runtime_checkable
class Runner(Protocol):
    """
    Structural interface every runner backend implements. Bots receive an
    instance; run.py selects and instantiates one at startup.

    The 15 names in the original drop-in contract are preserved here as
    instance methods rather than module-level functions, enabling multiple
    runner instances (e.g. two Telegram bots with different backends) to
    coexist in the same process without module-level state collisions.
    """

    async def run_prompt(
        self,
        thread_id: str,
        workspace_path: str,
        prompt: str,
        resume_session_id: str | None,
        post_fn,
        message_id: str | None = None,
        platform: PlatformConfig | None = None,
    ) -> tuple[str, str]:
        """Send prompt to the backend, return (response_text, session_id)."""
        ...

    async def cancel(self, thread_id: str) -> bool:
        """Interrupt the in-flight turn. Returns True if one was running."""
        ...

    async def close_client(self, thread_id: str) -> None:
        """Disconnect and forget this thread's client."""
        ...

    async def close_all(self) -> None:
        """Disconnect every active client (bot shutdown)."""
        ...

    def has_client(self, thread_id: str) -> bool:
        """Return True if a live client exists for this thread."""
        ...

    def init_agentdb(self, db, platform: PlatformConfig) -> None:
        """Initialize AgentDB governed memory. No-op when AGENTDB_ENABLED=false."""
        ...

    def list_pending_conflicts(self) -> list:
        """Read-only view into AgentDB's conflict queue."""
        ...

    def list_pending_proposals(self, status: str = "pending") -> list[dict]:
        """List AgentDB skill proposals by status."""
        ...

    def propose_skill(self, name: str, content: str) -> str:
        """Draft an AgentDB skill proposal. Returns the proposal_id."""
        ...

    def approve_skill_proposal(self, proposal_id: str) -> None:
        """Approve a pending AgentDB skill proposal."""
        ...

    def reject_skill_proposal(self, proposal_id: str, reason: str | None = None) -> None:
        """Reject a pending AgentDB skill proposal."""
        ...
