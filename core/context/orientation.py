"""Where the agent is, and where it may go.

A session starts in a scratch directory — ``agent-sessions/<platform>/<thread>/workspace``
— that is empty and belongs to the session. The repositories a user actually
cares about are elsewhere, named by ``ADDITIONAL_DIRS``. Nothing in the base
prompt says any of that, so an agent's first act is often to guess, search, or
work in the wrong tree.

This module builds the two short blocks that state it plainly:

* **workspace** — the starting directory: "you are here, it is empty, it is
  yours, do not assume the user's project is inside it".
* **additional dirs** — separately labelled, because these are *not* the
  starting point. They are paths the operator has made available, and mutating
  them affects a real project.

The second block is deliberately explicit that a registered path does not
override the user's instruction for *this* task. Without that line an agent
tends to "helpfully" switch to a listed project, which is the same failure mode
as guessing a directory, just better informed.

Prefix-cache note: these blocks contain per-session values (the workspace path)
and belong in the ephemeral per-turn context, **not** the cached system prefix.
That is why they are built here and injected by the runner rather than added to
``build_system_prompt``. Only paths and flags are rendered — never timestamps,
turn numbers or counts that would change between turns for no reason.
"""
from __future__ import annotations

import os
from typing import Iterable, Optional

#: Fixed headings. Structural, like ``core.prompts.SECTION_HEADINGS``: code and
#: tests rely on them, so operators override text, never the heading.
WORKSPACE_HEADING = "## Your starting directory"
ADDITIONAL_HEADING = "## Additional directories available to you"


def _clean_dirs(dirs: Iterable[str]) -> list[str]:
    """Absolute paths only, order preserved, duplicates removed.

    A relative entry cannot be rendered as a location (it means nothing without
    a base), so it is dropped rather than shown and then silently ignored.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in dirs or ():
        try:
            path = os.path.expanduser(str(raw).strip())
            if not path or not os.path.isabs(path):
                continue
            key = os.path.normpath(path)
            if key in seen:
                continue
        except Exception:
            continue
        seen.add(key)
        out.append(path)
    return out


def build_workspace_line(workspace_path: str) -> str:
    """The starting-point block. Empty string when there is no workspace."""
    path = (workspace_path or "").strip()
    if not path:
        return ""
    return "\n".join([
        WORKSPACE_HEADING,
        f"- Path: {path}",
        "- This is where you start, and where relative paths and `Bash` land.",
        "- It is a scratch directory for this session. It is normally empty, and "
        "it is normally NOT the project the user is asking about.",
        "- Do not assume the user's code is here. If the task names a location, "
        "work there instead; if you cannot tell where the work belongs, ask.",
    ])


def build_additional_dirs_block(additional_dirs: Iterable[str]) -> str:
    """The available-paths block. Empty string when there are none.

    Rendered only when at least one usable (absolute) entry exists, so the
    common single-directory setup does not pay for an empty section.
    """
    dirs = _clean_dirs(additional_dirs)
    if not dirs:
        return ""
    lines = [
        ADDITIONAL_HEADING,
        "These are separate from your starting directory. They exist because the "
        "user has made them available — they are real projects, not scratch space.",
    ]
    lines.extend(f"- {d}" for d in dirs)
    lines.append(
        "Use one only when the task points at it. A path being listed here does "
        "not mean you should switch to it: the user's instruction for this task "
        "decides where you work. Changes here affect a real project."
    )
    return "\n".join(lines)


def build_orientation(
    workspace_path: str,
    additional_dirs: Optional[Iterable[str]] = None,
) -> str:
    """Both blocks, starting directory first. Empty string if neither applies.

    Order matters: the agent reads where it *is* before what it *may reach*, so
    a listed project is never mistaken for the starting point.
    """
    parts = [build_workspace_line(workspace_path),
             build_additional_dirs_block(additional_dirs or [])]
    return "\n\n".join(p for p in parts if p)
