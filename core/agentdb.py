"""Thin delegation for the AgentDB surfaces shared by every runner.

This module deliberately knows NOTHING about providers, capabilities or
governance policy:

* which capability profile a runtime uses is chosen in the runner
  (``SERVERLESS_CAPABILITIES`` / ``LOCAL_CAPABILITIES`` / ``CLAUDE_CAPABILITIES``,
  all defined by agentdb), and
* governance defaults and write scopes live in agentdb's ``memory.yaml`` and in
  ``permissions.yaml``.

The only thing here is the mechanical "raise if AgentDB was never initialized,
otherwise forward the call" boilerplate that was previously copy-pasted into
each runner. Runners own the middleware; this just forwards.
"""
from __future__ import annotations

from typing import Any


class AgentDBSurface:
    """Forwards the shared AgentDB query/proposal calls to a middleware instance."""

    __slots__ = ("_middleware", "_agent_id")

    def __init__(self, middleware: Any = None, agent_id: str = "agent") -> None:
        self._middleware = middleware
        self._agent_id = agent_id

    # -- wiring ---------------------------------------------------------
    def bind(self, middleware: Any, agent_id: str) -> None:
        self._middleware = middleware
        self._agent_id = agent_id

    @property
    def middleware(self) -> Any:
        return self._middleware

    def _require(self) -> Any:
        if self._middleware is None:
            raise RuntimeError("AgentDB not initialized — call init_agentdb first")
        return self._middleware

    # -- shared surfaces ------------------------------------------------
    def list_pending_conflicts(self) -> list:
        return self._require().pending_conflicts()

    def list_pending_proposals(self, status: str = "pending") -> list[dict]:
        return self._require().list_proposals(status=status)

    def propose_skill(self, name: str, content: str) -> str:
        return self._require().propose_skill(name=name, content=content, scope=[self._agent_id])

    def approve_proposal(self, proposal_id: str) -> None:
        self._require().approve_proposal(proposal_id)

    def reject_proposal(self, proposal_id: str, reason: str | None = None) -> None:
        self._require().reject_proposal(proposal_id, reason=reason)
