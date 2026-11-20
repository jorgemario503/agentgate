"""Deterministic cross-turn deduplication of repeated tool-result content (C2).

Bash coding agents re-display the same bytes across turns (``cat foo.py`` then
``git diff`` then ``cat foo.py`` again). The per-result compressors are blind to
this: the redundancy is *across* turns. This module replaces a repeated tool
result with a compact, in-context pointer to the original (stored in the
:class:`~core.artifacts.ArtifactStore`), keeping the earliest copy canonical.

Hard invariants (see docs/ARCHITECTURE.md):

1. **Exact byte matching only** as the v1 baseline. No near-match folding until
   separately proven (T10 guards against false dedup).
2. **Prefix monotonicity.** Dedup is applied to the *new* result only, at
   ingestion. A result already appended is never rewritten, so appending a turn
   never mutates an earlier serialized message.
3. **Session/workspace isolation.** The index is scoped by the same hashed
   (session, workspace) namespace as the artifact store; content seen in one
   session is never treated as a duplicate in another.
4. **No dangling references.** The canonical artifact is the stored original;
   if it cannot be stored, the result is left untouched.

This module makes no network or model calls and never raises out of
:func:`dedup_tool_result`.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Optional

from core.artifacts import ArtifactStore


@dataclass(frozen=True)
class DedupOutcome:
    is_duplicate: bool
    canonical_id: Optional[str] = None
    count: int = 1


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


class DedupIndex:
    """Per-scope map from content hash -> (canonical artifact id, count).

    Persisted as JSON next to the artifacts so a restart keeps deduping against
    the same canonical copies (T11). Scoped by session+workspace via the store's
    namespace, so cross-session isolation holds (T12).
    """

    def __init__(self, store: ArtifactStore, *, session_id: str, workspace_id: str):
        self.store = store
        self.session_id = session_id
        self.workspace_id = workspace_id
        self.last_canonical_id: Optional[str] = None
        # Put the index in the same private scope dir the store uses.
        self._dir = store._scope_dir(session_id, workspace_id)
        self._path = self._dir / "_dedup_index.json"
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict:
        try:
            raw = self._path.read_text()
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        tmp = self._path.with_name(f".{self._path.name}.tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(self._data, f)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self._path)
        except OSError:
            # Index persistence is best-effort; in-memory state still works.
            pass

    def observe(self, text: str, *, tool_name: str = "") -> DedupOutcome:
        """Record `text`; report whether it duplicates an earlier result."""
        h = _content_hash(text)
        entry = self._data.get(h)
        if entry is not None:
            entry["count"] = int(entry.get("count", 1)) + 1
            self._data[h] = entry
            self._save()
            self.last_canonical_id = entry["artifact_id"]
            return DedupOutcome(True, entry["artifact_id"], entry["count"])

        # First occurrence: store the original (canonical) and record it.
        try:
            ref = self.store.put(self.session_id, self.workspace_id,
                                 text.encode("utf-8", "surrogatepass"))
        except Exception:
            # Cannot persist -> do not claim dedup; let caller keep full text.
            self.last_canonical_id = None
            return DedupOutcome(False, None, 1)
        self._data[h] = {"artifact_id": ref.artifact_id, "count": 1}
        self._save()
        self.last_canonical_id = ref.artifact_id
        return DedupOutcome(False, ref.artifact_id, 1)


def dedup_tool_result(
    text: str,
    index: DedupIndex,
    *,
    tool_name: str = "",
    min_chars: int = 200,
) -> str:
    """Return `text` unchanged on first sight; a short reference on a duplicate.

    Never raises. Below ``min_chars`` (or non-str) the text is returned as-is.
    """
    try:
        if not isinstance(text, str) or len(text) < min_chars:
            return text
        outcome = index.observe(text, tool_name=tool_name)
        if not outcome.is_duplicate or not outcome.canonical_id:
            return text
        aid = outcome.canonical_id
        return (
            f"[AgentGate duplicate tool result | identical content already sent "
            f"earlier in this session | occurrence #{outcome.count} | "
            f"original={aid} | retrieve=tool_result_read('{aid}', start, limit)]"
        )
    except Exception:
        return text
