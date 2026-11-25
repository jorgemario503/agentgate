"""Validate ``ADDITIONAL_DIRS`` entries and explain what was rejected.

``ADDITIONAL_DIRS`` is free-form operator input read from the environment. Every
value flows into at least one of: the Claude runner's SDK allow-list, the
AgentDB workspace snapshot, and the per-turn orientation block. A malformed
entry does not raise -- it is simply skipped by whichever consumer reaches it
first -- so a typo (" /Users/me/dev/komper" with a stray space, "~/dev/komper",
a path that no longer exists) produces an agent that quietly cannot see a
directory the operator believes it can see.

Silent skipping is the defect. This module makes the decision explicit and
carries a human-readable reason with it, so startup can print exactly what was
dropped and why. Validation is deterministic and offline: no network, no writes,
and it never raises.

Checks, in order:

* **format** -- non-empty, well-formed, and not a shell/bare-word fragment;
* **absolute** -- a relative path means nothing to a bot whose cwd is not the
  session workspace, so it is refused rather than guessed at;
* **depth** -- a filesystem root or a home directory is refused (see below);
* **existence** -- a missing directory is a warning, not a refusal.

A home directory is refused because it is almost always a shortcut that defeats
the point: it makes every project and personal file reachable, and it makes the
AgentDB workspace snapshot enumerate the whole tree two levels deep on every
turn. Listing the individual repositories is what the feature is for.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

#: Outcome kinds. ``accepted`` and ``missing`` keep the entry; the rest drop it.
ACCEPTED = "accepted"
MISSING = "missing"
RELATIVE = "relative"
FILESYSTEM_ROOT = "filesystem-root"
HOME_TOO_BROAD = "home-too-broad"
SHELL_FRAGMENT = "shell-fragment"
DUPLICATE = "duplicate"
EMPTY = "empty"

#: Characters that mean the value was not written as a plain path.
_SHELL_CHARS = frozenset("*?$`|&;<>\n\r\t")


@dataclass(frozen=True)
class DirCheck:
    """One entry and what validation decided about it."""

    raw: str
    path: str
    status: str
    reason: str = ""

    @property
    def usable(self) -> bool:
        """Whether this entry may be used by the consumers.

        ``missing`` counts as usable: a directory may be created later, and
        failing on it at startup would be wrong. It is still reported so a typo
        is visible.
        """
        return self.status in (ACCEPTED, MISSING)


def check_additional_dirs(
    entries: Iterable[str],
    *,
    home: str = "",
    exists: bool = True,
) -> list[DirCheck]:
    """Validate each entry, preserving input order.

    ``home`` defaults to the current user's home directory. ``exists=False``
    skips the filesystem check (used by tests, and by callers that only care
    about format).

    Never raises: an entry that cannot even be inspected is reported as
    ``empty`` rather than aborting validation.
    """
    home_path = os.path.normpath(os.path.expanduser(home or "~"))
    results: list[DirCheck] = []
    seen: set[str] = set()

    for raw in entries or ():
        try:
            text = "" if raw is None else str(raw).strip()
        except Exception:
            text = ""

        if not text:
            results.append(
                DirCheck(str(raw), "", EMPTY, "empty value (check for a stray comma)")
            )
            continue

        path = os.path.expanduser(text)

        if any(ch in path for ch in _SHELL_CHARS):
            results.append(
                DirCheck(
                    text, path, SHELL_FRAGMENT,
                    "contains shell or glob characters; write a plain path",
                )
            )
            continue

        if not os.path.isabs(path):
            results.append(
                DirCheck(
                    text, path, RELATIVE,
                    "not an absolute path; it would resolve against the process "
                    "working directory, not the session workspace",
                )
            )
            continue

        path = os.path.normpath(path)

        if path == os.sep:
            results.append(
                DirCheck(
                    text, path, FILESYSTEM_ROOT,
                    "the filesystem root would expose every file on the machine",
                )
            )
            continue

        if path == home_path:
            results.append(
                DirCheck(
                    text, path, HOME_TOO_BROAD,
                    "your home directory exposes every project and personal file; "
                    "list individual repositories instead",
                )
            )
            continue

        if path in seen:
            results.append(
                DirCheck(text, path, DUPLICATE, "already listed earlier")
            )
            continue
        seen.add(path)

        if exists and not os.path.isdir(path):
            results.append(
                DirCheck(
                    text, path, MISSING,
                    "directory does not exist right now (it will be used if it "
                    "appears; check the spelling and that the path is absolute)",
                )
            )
            continue

        results.append(DirCheck(text, path, ACCEPTED, ""))

    return results


def usable_dirs(checks: Iterable[DirCheck]) -> list[str]:
    """The accepted/dangling paths, in input order, ready for injection."""
    return [c.path for c in checks if c.usable]


def describe_problems(checks: Iterable[DirCheck]) -> list[str]:
    """One log line per entry that was dropped or is dangling.

    Returns an empty list when every entry is fine, so callers can print nothing
    in the common case. Lines quote the value, not just the rule, because the
    operator needs to find the offending character in their configuration.
    """
    lines: list[str] = []
    for c in checks:
        if c.status == ACCEPTED:
            continue
        label = c.path if c.status == MISSING else c.raw
        prefix = "ignored" if not c.usable else "warning"
        lines.append(f"ADDITIONAL_DIRS [{prefix}] {label!r}: {c.reason}")
    return lines
