"""Bounded, session/workspace-scoped store for original tool-result bytes.

When tool-result compression elides content from the model-facing message, the
*original* must survive so it can be retrieved exactly. This store is the
canonical copy: compression never overwrites it, and every read is authorized
against the caller's session **and** workspace.

Design invariants (see docs: ARCHITECTURE.md "Safety invariants"):

- **Authorization on every read**, independent of whether the caller knows the
  id. An id alone never grants access: the (session_id, workspace_id) pair is
  re-checked and must match the scope the artifact was stored under.
- **Opaque, deterministic ids.** The id is a namespaced hash of
  (session, workspace, content). It leaks neither content nor absolute paths.
  Knowing the id is not sufficient to read it (scope check still applies).
- **Restart-persistent.** Artifacts live on disk under `base/<sess>/<ws>/`; a
  fresh process over the same base reads them back.
- **Path-confined.** Ids are validated against a strict pattern and the
  resolved path is re-checked to stay under the scope directory; symlinks are
  never followed out of the tree.
- **Restrictive perms, atomic writes.** 0o700 dirs, 0o600 files, write-to-temp
  then rename so a partial write is never observable.
- **Bounded reads.** `read(offset, limit)` returns at most `limit` bytes with
  deterministic clamping, so retrieval tool output is bounded by construction.

This module makes no network or model calls and never raises out of `put` for
mundane reasons beyond genuine I/O failure; readers get a typed exception.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

#: Strict artifact-id shape: 64 lowercase hex chars (a full sha256 hex digest).
_ID_RE = re.compile(r"^[0-9a-f]{64}$")

#: Max bytes a single `read` will ever return, regardless of caller `limit`.
MAX_READ_BYTES = 1_000_000


@dataclass(frozen=True)
class ArtifactRef:
    """Handle to a stored original. Safe to log; contains no content/secrets."""

    artifact_id: str
    session_id: str
    workspace_id: str
    sha256: str
    byte_length: int


def _hash_parts(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", "surrogatepass"))
        h.update(b"\x00")  # unambiguous separator
    return h.hexdigest()


class ArtifactStore:
    """On-disk original-artifact store, scoped by session and workspace."""

    def __init__(self, base: str | Path):
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.base, 0o700)
        except OSError:
            pass  # best-effort on platforms without POSIX perms

    # --- internal helpers ----------------------------------------------
    def _scope_dir(self, session_id: str, workspace_id: str) -> Path:
        # Scope components are hashed so a hostile session/workspace string
        # (e.g. "../x") cannot traverse the filesystem.
        s = _hash_parts("sess", session_id)[:32]
        w = _hash_parts("ws", workspace_id)[:32]
        d = self.base / s / w
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.base / s, 0o700)
            os.chmod(d, 0o700)
        except OSError:
            pass
        return d

    def _artifact_id(self, session_id: str, workspace_id: str, content: bytes) -> str:
        # Namespaced content hash: deterministic per scope, opaque, no path/content leak.
        return _hash_parts(
            "art",
            session_id,
            workspace_id,
            hashlib.sha256(content).hexdigest(),
        )

    def _path_for(self, scope_dir: Path, artifact_id: str) -> Path:
        if not isinstance(artifact_id, str) or not _ID_RE.match(artifact_id):
            raise ValueError(f"malformed artifact id: {artifact_id!r}")
        return scope_dir / artifact_id

    def _confine(self, scope_dir: Path, path: Path) -> Path:
        """Resolve `path` and ensure it stays under `scope_dir`, no symlink follow."""
        # Reject if the path component is a symlink before resolving.
        if path.is_symlink():
            raise PermissionError("artifact path is a symlink")
        resolved = path.resolve(strict=False)
        scope_resolved = scope_dir.resolve(strict=True)
        try:
            resolved.relative_to(scope_resolved)
        except ValueError:
            raise PermissionError("artifact path escapes scope directory")
        if resolved.is_symlink():
            raise PermissionError("artifact path is a symlink")
        return resolved

    # --- public API -----------------------------------------------------
    def put(self, session_id: str, workspace_id: str, content: bytes) -> ArtifactRef:
        """Store `content` under the given scope; return an opaque ref."""
        if not isinstance(content, (bytes, bytearray)):
            content = str(content).encode("utf-8", "surrogatepass")
        content = bytes(content)
        artifact_id = self._artifact_id(session_id, workspace_id, content)
        scope_dir = self._scope_dir(session_id, workspace_id)
        path = self._path_for(scope_dir, artifact_id)

        if not path.exists():
            # Atomic write: temp file in the same dir, then rename.
            tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with open(tmp, "wb") as f:
                    f.write(content)
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                os.replace(tmp, path)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass

        return ArtifactRef(
            artifact_id=artifact_id,
            session_id=session_id,
            workspace_id=workspace_id,
            sha256=hashlib.sha256(content).hexdigest(),
            byte_length=len(content),
        )

    def read(
        self,
        session_id: str,
        workspace_id: str,
        artifact_id: str,
        offset: int = 0,
        limit: int = MAX_READ_BYTES,
    ) -> bytes:
        """Return bytes of an original, authorized for this session+workspace.

        Raises PermissionError when the artifact is unknown *or* belongs to a
        different scope (the two are deliberately indistinguishable to callers),
        ValueError for malformed arguments.
        """
        if offset < 0:
            raise ValueError("offset must be >= 0")
        if limit < 0:
            raise ValueError("limit must be >= 0")
        if not isinstance(artifact_id, str) or not _ID_RE.match(artifact_id):
            # Unknown or crafted id: treat as denied, do not echo it back.
            raise PermissionError("artifact not accessible")

        scope_dir = self._scope_dir(session_id, workspace_id)
        try:
            path = self._path_for(scope_dir, artifact_id)
            path = self._confine(scope_dir, path)
        except (ValueError, PermissionError):
            raise PermissionError("artifact not accessible")

        if not path.exists() or not path.is_file():
            raise PermissionError("artifact not accessible")

        cap = min(limit, MAX_READ_BYTES)
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(cap)
