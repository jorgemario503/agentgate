"""Context management concern package.

Everything about **keeping the model's context small and stable**:

* **compression** — bounded tool-result summaries (:mod:`~core.context.tool_compression`)
* **pipeline** — compression + retrieval of originals (:mod:`~core.context.tool_pipeline`)
* **artifacts** — the session/workspace-scoped store for originals
  (:mod:`~core.context.artifacts`)
* **dedup** — cross-turn repeated-result elision (:mod:`~core.context.dedup`)
* **repo_map** — a bounded repository outline (:mod:`~core.context.repo_map`)
* **cache_key** — provider prompt-cache keys (:mod:`~core.context.cache_key`)

These modules form a genuine cluster: ``tool_pipeline`` composes ``tool_compression`` and
``artifacts``, ``dedup`` uses ``artifacts``, and the evaluation harnesses use both. Grouping them
makes the dependency direction visible — this package may not import from ``core.tools`` or
``runners``, or the cluster would depend on its own consumers.

Re-exports follow the ``core/skills/`` and ``core/tools/`` convention so callers can import from
the package rather than a module path:

    from core.context import ArtifactStore, ToolResultPipeline
"""
from __future__ import annotations

from core.context.artifacts import ArtifactRef, ArtifactStore
from core.context.cache_key import cache_key_kwargs, retry_without_cache_key
from core.context.dedup import DedupIndex, DedupOutcome, dedup_tool_result
from core.context.repo_map import (
    FileEntry,
    RepoMap,
    Symbol,
    build_repo_map,
    build_repo_map_segment,
    find_repo_root,
)
from core.context.tool_compression import (
    CompressConfig,
    CompressionOutcome,
    maybe_compress,
)
from core.context.tool_pipeline import ToolResultPipeline, tool_result_read_handler

__all__ = [
    # artifacts
    "ArtifactRef",
    "ArtifactStore",
    # cache keys
    "cache_key_kwargs",
    "retry_without_cache_key",
    # dedup
    "DedupIndex",
    "DedupOutcome",
    "dedup_tool_result",
    # repo map
    "FileEntry",
    "RepoMap",
    "Symbol",
    "build_repo_map",
    "build_repo_map_segment",
    "find_repo_root",
    # compression
    "CompressConfig",
    "CompressionOutcome",
    "maybe_compress",
    # pipeline
    "ToolResultPipeline",
    "tool_result_read_handler",
]
