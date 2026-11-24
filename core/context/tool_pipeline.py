"""Shared adapter: turn a raw tool result into the model-facing tool message.

This is the single integration point the runners call, so compression/dedup
logic is not duplicated three times (see AGENTS.md "don't copy blocks across
runners" and docs/ARCHITECTURE.md "introduce an adapter").

Design:

- Always returns a **string** for the message `content`, safe to append as a
  ``role: "tool"`` message. Never raises; on any error returns the input text
  unchanged.
- Holds an :class:`~core.artifacts.ArtifactStore`. The store's directory is
  decided from the session base so originals persist across restarts and are
  scoped per-platform.
- Compression is gated on ``AGENTGATE_TOOL_COMPRESSION_ENABLED``. With it off,
  this is a pure passthrough (acceptance criterion 1: byte-identical outbound).
- The model-facing text always discloses omissions + a retrieval handle, and
  the original is retrievable through the ``tool_result_read`` tool.

This module performs no network or model calls.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from core import config as _cfg
from core.context.artifacts import ArtifactStore
from core.context.tool_compression import CompressConfig, maybe_compress


class ToolResultPipeline:
    """Per-runner helper that compresses/annotates tool results (flag-gated)."""

    def __init__(
        self,
        base_dir: str | Path,
        *,
        session_id: str,
        workspace_id: str,
        enabled: Optional[bool] = None,
        min_chars: Optional[int] = None,
        compress_config: Optional[CompressConfig] = None,
        dedup_enabled: Optional[bool] = None,
    ):
        self.session_id = session_id
        self.workspace_id = workspace_id
        self.enabled = _cfg.TOOL_COMPRESSION_ENABLED if enabled is None else enabled
        self._dedup_override = dedup_enabled
        self._min_chars = (
            _cfg.TOOL_COMPRESSION_MIN_CHARS if min_chars is None else min_chars
        )
        self._config = compress_config or CompressConfig(min_chars=self._min_chars)

        artifact_root = _cfg.TOOL_ARTIFACT_DIR.strip()
        root = Path(artifact_root) if artifact_root else Path(base_dir) / "_artifacts"
        self.store = ArtifactStore(root)

        # C2 dedup (optional). Uses the same store so the canonical copy is a
        # real artifact retrievable via tool_result_read.
        self.dedup_enabled = (
            _cfg.CROSS_TURN_DEDUP_ENABLED
            if self._dedup_override is None else self._dedup_override
        )
        self._dedup = None
        if self.dedup_enabled:
            try:
                from core.context.dedup import DedupIndex
                self._dedup = DedupIndex(
                    self.store, session_id=session_id, workspace_id=workspace_id,
                )
            except Exception:
                self._dedup = None

    def process(self, text: str, *, tool_name: str = "") -> str:
        """Return model-facing text for a tool result. Never raises."""
        try:
            if self._dedup is not None and isinstance(text, str):
                from core.context.dedup import dedup_tool_result
                deduped = dedup_tool_result(
                    text, self._dedup, tool_name=tool_name,
                    min_chars=self._min_chars,
                )
                if deduped is not text:
                    # Duplicate: a short reference. Do not also compress it.
                    return deduped
            if not self.enabled:
                return text
            if not isinstance(text, str):
                return text  # type: ignore[return-value]
            outcome = maybe_compress(
                text,
                tool_name=tool_name,
                enabled=True,
                config=self._config,
                store=self.store,
                session_id=self.session_id,
                workspace_id=self.workspace_id,
            )
            return outcome.text
        except Exception:
            return text

    # --- retrieval (for the tool_result_read tool) ---------------------
    def read_original(
        self,
        artifact_id: str,
        offset: int = 0,
        limit: int = 20_000,
    ) -> str:
        """Read a slice of an original by id, authorized to this scope.

        Raises PermissionError when unknown / out of scope (the two are
        indistinguishable by design), ValueError for bad args.
        """
        data = self.store.read(
            self.session_id, self.workspace_id, artifact_id, offset, limit
        )
        return data.decode("utf-8", "replace")


#: Default page size when the model asks for more of an original.
RETRIEVE_DEFAULT_LIMIT = 20_000
RETRIEVE_MAX_LIMIT = 100_000


def tool_result_read_handler(pipeline: "ToolResultPipeline"):
    """Return a handler for the ``tool_result_read`` tool bound to `pipeline`.

    The handler is sync (the runners run tool handlers in a thread) and always
    returns a string. Authorization failure yields a clear error string rather
    than raising into the tool loop (fail-closed: no data is returned).
    """

    def handler(args: dict) -> str:
        artifact_id = str(args.get("artifact_id", "") or "").strip()
        try:
            offset = int(args.get("offset", 0) or 0)
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = int(args.get("limit", RETRIEVE_DEFAULT_LIMIT) or RETRIEVE_DEFAULT_LIMIT)
        except (TypeError, ValueError):
            limit = RETRIEVE_DEFAULT_LIMIT
        offset = max(0, offset)
        limit = max(1, min(limit, RETRIEVE_MAX_LIMIT))
        if not artifact_id:
            return "ERROR: artifact_id is required"
        try:
            chunk = pipeline.read_original(artifact_id, offset=offset, limit=limit)
        except PermissionError:
            # Unknown or out-of-scope: do not distinguish, do not leak.
            return "ERROR: artifact not accessible for this session."
        except ValueError as e:
            return f"ERROR: bad retrieval arguments: {e}"
        except Exception as e:  # pragma: no cover - defensive
            return f"ERROR: retrieval failed: {e}"
        more = ""
        if len(chunk) >= limit:
            more = f"\n…[more available; call again with offset={offset + len(chunk)}]"
        return chunk + more

    return handler
