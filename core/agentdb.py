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
    """Resolve AgentGate config and hand it to agentdb's ``build_llm_fn``.

    AgentGate owns only its own configuration here: the on/off flag and the
    consolidation model/params (resolved from ``core.providers``). All the
    client construction and ``llm_fn`` wrapping lives in the agentdb package
    (``agentdb.providers.consolidation.build_llm_fn``) — AgentGate does not
    import ``openai`` or wrap any callable itself.

    Returns ``None`` (consolidation stays copy-only / inert) unless ALL of:
    * ``AGENTDB_CONSOLIDATION_ENABLED`` is on,
    * a consolidation model is configured for the provider
      (``{PREFIX}_CONSOLIDATION_MODEL`` or a global ``CONSOLIDATION_MODEL``), and
    * the installed agentdb exposes ``build_llm_fn`` (import succeeds).

    Until a released ``agentdatabase`` ships ``build_llm_fn``, this returns
    ``None`` (feature dormant); the runner then passes ``run_consolidation=False``
    and behavior is unchanged. Never raises at build time; the package handles
    model-call errors and falls back to the top source value.
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

        # The high-level helper (client construction + wrapping) lives in agentdb.
        from agentdb.providers.consolidation import build_llm_fn

        return build_llm_fn(
            model=model,
            base_url=settings.base_url,
            api_key=settings.api_key or None,
            max_tokens=settings.consolidation_max_tokens,
            temperature=settings.consolidation_temperature,
        )
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
