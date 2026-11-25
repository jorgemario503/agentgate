"""Directories a session has *evidence* for, from a turn's text.

AgentDB keeps a per-session directory list and re-injects it into the model's
context every turn. That list is only useful if every entry is a directory that
really exists and that the session plausibly touched — otherwise it is noise
the model has to reason past before it can start working.

The original implementation was a bare regex over the turn's free text::

    re.findall(r'/(?:[\\w./-]+/)+', text)

which turns any slash-shaped fragment of prose or of an injected prompt into a
"session directory". A real turn reproduced all of these::

    /agent-sessions/          a relative path, made root-absolute
    /Users/jm/.../            a `...` from the instructions' "**/secrets/*"
    /context/                 a fragment of `core/context/`
    /secrets/                 a fragment of the same rule
    /.../                     pure punctuation

None exist. The model then reported them as noise and stopped trusting the
block — which also cost it the one entry that was real.

This module keeps the text scan (it is the only evidence available for a
stateless runner, which sees tool *results*, not tool *arguments*) but adds the
three checks the original lacked:

* **Structural.** Reject fragments — `...`, trailing `..`, and paths with a
  repeated empty or dotted segment.
* **Absolute.** A relative path is resolved against the workspace, never
  against `/`.
* **Existential.** ``os.path.isdir`` must pass. A path the model merely
  mentioned is not a directory the session worked in.

Existence-checking makes this a filesystem call per candidate, so candidates
are bounded and cached per session by the caller.

This module makes no network or model calls and never raises.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Iterable, Optional

#: Slash-shaped runs in text. The original regex required a *trailing* slash,
#: which silently dropped the last segment: "/Users/jm/Documents/github/komper"
#: matched only as "/Users/jm/Documents/github/". Since the final segment is
#: usually the directory the model actually named, that guaranteed the block
#: held a parent directory instead of the one being discussed. This keeps the
#: trailing slash optional and checks strictly below.
_PATH_RE = re.compile(r"/[\w.-]+(?:/[\w.-]+)*/?")

#: Cap on candidates examined per turn, so a pathological turn cannot turn the
#: existence check into a filesystem sweep.
MAX_CANDIDATES = 40

#: Cap on distinct directories kept per turn.
MAX_DIRS = 10

#: Well-known top-level directories. A candidate whose first segment is one of
#: these is read as an absolute path first; anything else is read relative to
#: the workspace first. Only an ordering hint — the existence check decides.
_KNOWN_ROOTS = frozenset({
    "Users", "home", "root", "tmp", "var", "opt", "etc", "srv", "mnt",
    "media", "usr", "Volumes", "private", "Applications", "System",
    "Library", "workspace", "app", "data", "work",
})


def _looks_structural(candidate: str) -> bool:
    """Reject path fragments that cannot be real directory paths.

    Catches the observed failure modes: ``/Users/jm/.../`` (ellipsis marker),
    ``/.../`` (punctuation only), ``/a/../b/`` (traversal written as text) and
    doubled separators from a broken format string.
    """
    if "..." in candidate:
        return False
    if candidate.startswith("//"):
        return False
    parts = candidate.split("/")
    for part in parts:
        if part in ("..", "."):
            return False
    return True


def _has_plausible_root_segment(candidate: str) -> bool:
    """Could the first segment plausibly be a real top-level directory?

    This decides the *order* in which readings are attempted, never whether a
    path is accepted — :func:`extract_existing_dirs` accepts only directories
    the existence check confirms. ``os.path.isabs`` cannot separate
    ``/Users/jm/komper/`` from ``/agent-sessions/`` (both start with ``/``), so
    without an ordering hint every leading-slash fragment would be tried at the
    filesystem root first.

    Known roots and plain words are tried absolutely first; a hyphen- or
    dot-joined compound (``agent-sessions``, ``secrets``) reads as a relative
    path that was quoted with a slash, so the workspace-relative reading is
    tried first. Both readings are attempted either way.
    """
    first = candidate.lstrip("/").split("/", 1)[0]
    if not first:
        return False
    if first in _KNOWN_ROOTS:
        return True
    return not any(ch in first for ch in "-_.")


def extract_existing_dirs(
    text: str,
    *,
    workspace_path: str = "",
    exists: Optional[Callable[[str], bool]] = None,
) -> list[str]:
    """Return absolute paths to directories that the text mentions *and* exist.

    ``workspace_path`` resolves relative candidates; without it a relative
    candidate is dropped rather than promoted to a root-level path (which is
    how ``/agent-sessions/`` was invented). ``exists`` is injectable so tests
    can run hermetically.

    Deterministic: results keep first-mention order, deduplicated by realpath.
    Never raises.
    """
    check = exists if exists is not None else os.path.isdir
    out: list[str] = []
    seen: set[str] = set()

    try:
        matches = _PATH_RE.findall(text or "")
    except Exception:
        return out

    for raw in matches[:MAX_CANDIDATES]:
        if len(out) >= MAX_DIRS:
            break
        if not _looks_structural(raw):
            continue
        try:
            candidates: list[str] = []
            if os.path.isabs(raw):
                # Read it as a genuine absolute path first, then — because a
                # leading-slash fragment in prose is usually a *relative* path
                # quoted with its slash ("core/context/" written "/context/") —
                # as a path under the workspace. Existence decides below; the
                # heuristic only orders the attempts.
                if _has_plausible_root_segment(raw):
                    candidates.append(raw)
                if workspace_path:
                    candidates.append(os.path.join(workspace_path, raw.lstrip("/")))
            elif workspace_path:
                candidates.append(os.path.join(workspace_path, raw.lstrip("/")))
            if not candidates:
                # No base to resolve against: dropping is strictly better than
                # inventing a root-level directory.
                continue

            path = None
            for cand in candidates:
                norm = os.path.normpath(cand)
                if norm in ("/", "") or not os.path.isabs(norm):
                    continue
                if check(norm):
                    path = norm
                    break
            if path is None:
                continue
            real = os.path.realpath(path)
            if real in seen:
                continue
        except Exception:
            continue
        seen.add(real)
        out.append(path)

    return out


def prune_existing_dirs(
    dirs: Iterable[str],
    *,
    exists: Optional[Callable[[str], bool]] = None,
) -> list[str]:
    """Drop entries that no longer exist or are not absolute/real directories.

    The cap on the recorded list is size-based (AgentDB keeps up to 200 per
    session), so a long session can carry entries for scratch directories that
    were deleted. Re-checking on the way out keeps the injected block honest.
    """
    check = exists if exists is not None else os.path.isdir
    out: list[str] = []
    for d in dirs or ():
        try:
            if not d or not os.path.isabs(d):
                continue
            if not check(d):
                continue
        except Exception:
            continue
        out.append(d)
    return out
