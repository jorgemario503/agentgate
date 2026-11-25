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

import ntpath
import os
import posixpath
import re
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
#: Backslash is deliberately NOT here: it is the separator on Windows.
_SHELL_CHARS = frozenset("*?$`|&;<>\n\r\t")


def _dialect_of(text: str):
    """Pick the path module that matches how the value is *written*.

    The validator must not assume the host OS agrees with the value. A Windows
    path can appear in configuration read on macOS or Linux (shared .env, a
    container, a teammate's snippet), and judging it with ``os.path`` there
    reports it as "not absolute" -- which is both wrong and unhelpful, since the
    path is perfectly absolute on the machine the operator is describing.

    Detection is deliberately conservative: a drive letter, a UNC prefix, or a
    backslash anywhere means Windows semantics; otherwise POSIX.
    """
    if re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\") or "\\" in text:
        return ntpath
    return posixpath


def _is_root(path: str) -> bool:
    """True for "/" on POSIX and for a bare drive/UNC root on Windows.

    ``os.sep`` cannot be used: on Windows it is a backslash, so comparing a
    POSIX-style "/" would miss, and on POSIX a drive root would never be seen.
    A drive root is "C:\\" (nothing after the drive); "C:\\Users" is not a root.
    A UNC root is "\\\\server\\share" with no path after the share.
    """
    mod = _dialect_of(path)
    norm = mod.normpath(path)
    if mod is ntpath:
        drive, rest = mod.splitdrive(norm)
        if norm.startswith("\\\\"):
            # UNC: root only when the share has no trailing component.
            parts = [p for p in norm.split("\\") if p]
            return len(parts) <= 2
        return bool(drive) and rest in ("", "\\", "/")
    return norm == "/"


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
    homes = _home_variants(home)
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
        mod = _dialect_of(path)

        if any(ch in path for ch in _SHELL_CHARS):
            results.append(
                DirCheck(
                    text, path, SHELL_FRAGMENT,
                    "contains shell or glob characters; write a plain path",
                )
            )
            continue

        if not mod.isabs(path):
            results.append(
                DirCheck(
                    text, path, RELATIVE,
                    "not an absolute path; it would resolve against the process "
                    "working directory, not the session workspace",
                )
            )
            continue

        path = mod.normpath(path)

        if _is_root(path):
            results.append(
                DirCheck(
                    text, path, FILESYSTEM_ROOT,
                    "the filesystem root would expose every file on the machine",
                )
            )
            continue

        if _normalise_for_home(path, homes):
            results.append(
                DirCheck(
                    text, path, HOME_TOO_BROAD,
                    "your home directory exposes every project and personal file; "
                    "list individual repositories instead",
                )
            )
            continue

        key = _dedupe_key(path)
        if key in seen:
            results.append(
                DirCheck(text, path, DUPLICATE, "already listed earlier")
            )
            continue
        seen.add(key)

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


def _home_variants(home: str) -> list[str]:
    """Home directory under both dialects, normalised.

    The host's ``~`` is the primary answer, but a Windows home written into the
    same configuration file must also be recognised when validated on POSIX.
    """
    out: list[str] = []
    try:
        primary = os.path.expanduser(home or "~")
    except Exception:
        primary = ""
    candidates = [primary]
    if home:
        candidates.append(home)
    for c in candidates:
        if not c:
            continue
        mod = _dialect_of(c)
        out.append(mod.normcase(mod.normpath(c)))
    # The Windows home is not discoverable from POSIX, so an explicit
    # C:\Users\<name> is matched structurally below rather than by string.
    return out


def _normalise_for_home(path: str, homes: list[str]) -> bool:
    """Whether ``path`` is one of the home directories (or a Windows home shape)."""
    mod = _dialect_of(path)
    norm = mod.normcase(mod.normpath(path))
    if norm in homes:
        return True
    # A drive-rooted "X:\Users\<name>" with exactly one segment after Users is
    # the Windows home shape; accept it as "too broad" even on POSIX, where the
    # real home is unknowable.
    if mod is ntpath:
        drive, rest = mod.splitdrive(norm)
        parts = [p for p in rest.replace("/", "\\").split("\\") if p]
        if len(parts) == 2 and parts[0].lower() == "users":
            return True
    return False


def _dedupe_key(path: str) -> str:
    """Comparison key: case-insensitive on Windows, exact on POSIX.

    Windows paths are case-insensitive, so ``C:\\Dev`` and ``c:\\dev`` are the
    same directory; POSIX paths are not, so lowercasing there would wrongly
    collapse distinct directories.
    """
    mod = _dialect_of(path)
    return mod.normcase(mod.normpath(path))


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
