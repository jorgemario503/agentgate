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

from pathlib import Path
from typing import Any


class AgentDBInitError(RuntimeError):
    """Raised when AgentDB is enabled but cannot be initialized.

    AgentDB is enabled by default; this is a hard, actionable startup failure
    (decision: fail loudly, not silently run without memory).
    """


def init_agentdb_for(runner: Any, platform: Any, *, config: Any) -> None:
    """Build AgentDB, wire it into `runner`, and run one lifecycle sweep.

    Shared by every bot so the construction and hard-fail policy live in one
    place instead of being triplicated. Raises :class:`AgentDBInitError` with a
    clear remedy when the package, the permissions file, or the memory dir is
    missing. A no-op only when `AGENTDB_ENABLED` is explicitly false.
    """
    if not getattr(config, "AGENTDB_ENABLED", False):
        return

    try:
        from agentdb import AgentDB
    except ImportError as e:  # package not installed
        raise AgentDBInitError(
            "AgentDB is enabled but the 'agentdb' package is not installed. "
            "Install it with: pip install 'agentdatabase>=0.5.0' "
            "or set AGENTDB_ENABLED=false to run without governed memory."
        ) from e

    perms = Path(config.AGENTDB_PERMISSIONS_PATH)
    if not perms.exists():
        raise AgentDBInitError(
            f"AgentDB permissions file not found: {perms}. "
            "Create it (see permissions.yaml) or set AGENTDB_PERMISSIONS_PATH. "
            "Set AGENTDB_ENABLED=false to run without governed memory."
        )

    try:
        db = AgentDB(
            memory_dir=config.AGENTDB_MEMORY_DIR,
            permissions_path=perms,
        )
    except Exception as e:  # bad config, unwritable dir, corrupt store, ...
        raise AgentDBInitError(
            f"AgentDB failed to initialize at {config.AGENTDB_MEMORY_DIR!r}: {e}. "
            "Check AGENTDB_MEMORY_DIR permissions, or set AGENTDB_ENABLED=false."
        ) from e

    runner.init_agentdb(db, platform=platform)
    db.lifecycle.run_sweep()


def build_consolidation_llm_fn(provider: str) -> Any:
    """Build a sync ``llm_fn`` for AgentDB consolidation synthesis, or None.

    Returns None (consolidation stays copy-only / inert) unless:
    * ``AGENTDB_CONSOLIDATION_ENABLED`` is on, AND
    * a consolidation model is configured for the provider
      (``{PREFIX}_CONSOLIDATION_MODEL`` or a global ``CONSOLIDATION_MODEL``).

    The returned callable takes one prompt string and returns the synthesized
    text, using a dedicated (sync) OpenAI-compatible client on the configured
    consolidation model — the same "cheaper model" pattern as SUMMARY_MODEL.
    Never raises at build time; only the actual model call can error, and that
    is handled by the package (it falls back to the top source value).
    """
    try:
        import core.config as _cfg

        if not getattr(_cfg, "AGENTDB_CONSOLIDATION_ENABLED", False):
            return None

        from core.providers import settings_for

        settings = settings_for(provider)
        model = settings.consolidation_model
        if not model:
            return None

        from openai import OpenAI  # sync client — llm_fn is a plain callable

        client = OpenAI(
            api_key=settings.api_key or "not-needed",
            base_url=settings.base_url or "https://api.deepinfra.com/v1/openai",
        )

        def _llm_fn(prompt: str) -> str:
            kwargs: dict[str, Any] = {"model": model, "messages": [
                {"role": "user", "content": prompt},
            ]}
            if settings.consolidation_max_tokens is not None:
                kwargs["max_tokens"] = settings.consolidation_max_tokens
            if settings.consolidation_temperature is not None:
                kwargs["temperature"] = settings.consolidation_temperature
            resp = client.chat.completions.create(**kwargs)
            return (resp.choices[0].message.content or "").strip()

        return _llm_fn
    except Exception:
        return None


class AgentDBSurface:
    """Forwards the shared AgentDB query calls to a middleware instance."""

    __slots__ = ("_middleware",)

    def __init__(self, middleware: Any = None) -> None:
        self._middleware = middleware

    # -- wiring ---------------------------------------------------------
    def bind(self, middleware: Any) -> None:
        self._middleware = middleware

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
