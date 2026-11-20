"""Python-first incremental repository map (spec C3).

Builds a compact, deterministic overview of a code repository — language
breakdown, directory tree, and Python public symbols with file-relative paths
and line spans — that a coding session can inject cheaply, while exact source
is retrieved through the existing read/search tools.

Safety (see docs/ARCHITECTURE.md "Repository-map design"):

- **Root-confined.** Only files under ``repo_root`` are read; nothing outside.
- **Ignore-aware.** Honours ``.gitignore`` patterns plus a built-in deny list
  (VCS dirs, secrets like ``.env`` / key / credential files, vendored deps,
  build outputs, caches, binaries). Secrets and binaries are never indexed.
- **Bounded.** Per-file size cap, total file cap, max rendered tokens. Oversize
  and binary files are skipped, not guessed at.
- **No execution.** Files are read as text and parsed with ``ast``; project code
  is never imported or run.
- **Deterministic.** Output ordering is stable so the rendered map is a stable
  prompt prefix; parse failures are reported, never fatal.

This module makes no network or model calls.
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

#: Directories never descended into.
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", ".venv", "venv", "env", ".tox",
    "dist", "build", ".eggs", ".idea", ".vscode", "target", "vendor",
    "site-packages", ".next", ".cache", "coverage", ".coverage",
}

#: Path/name patterns excluded by default (secrets + binaries + junk).
_SKIP_GLOBS = (
    "*.env", ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
    "*.crt", "*.der", "*credentials*", "*secrets*",
    "*.pyc", "*.pyo", "*.so", "*.dylib", "*.dll", "*.o", "*.a",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.pdf", "*.zip",
    "*.gz", "*.tar", "*.whl", "*.jar", "*.bin", "*.exe",
    "*.lock", "package-lock.json", "yarn.lock", "poetry.lock",
)

_DEFAULT_MAX_FILE_BYTES = 200_000
_DEFAULT_MAX_FILES = 5_000
_DEFAULT_MAX_TOKENS = 1_500


@dataclass
class Symbol:
    kind: str          # "class" | "function" | "method"
    name: str
    line: int
    signature: str = ""
    parent: Optional[str] = None


@dataclass
class FileEntry:
    relpath: str
    sha256: str
    byte_length: int
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


class RepoMap:
    """An in-memory repository map. Build via :func:`build_repo_map`."""

    def __init__(self, root: Path, entries: dict[str, FileEntry],
                 failures: dict[str, str], languages: dict[str, int]):
        self.root = root
        self._entries = entries       # relpath -> FileEntry
        self._failures = failures     # relpath -> error
        self._languages = languages   # extension -> count

    # --- views ----------------------------------------------------------
    def files(self) -> list[str]:
        return sorted(self._entries)

    def symbol_names(self) -> set[str]:
        names: set[str] = set()
        for e in self._entries.values():
            for s in e.symbols:
                names.add(s.name)
        return names

    def parse_failures(self) -> dict[str, str]:
        return dict(self._failures)

    def entry(self, relpath: str) -> Optional[FileEntry]:
        return self._entries.get(relpath)

    # --- persistence (manifest reuse across restarts) -------------------
    def save_manifest(self, path: str | Path) -> None:
        """Persist per-file hashes + symbols so a restart can reuse them."""
        import json
        data = {
            "root": str(self.root),
            "languages": self._languages,
            "failures": self._failures,
            "files": {
                rel: {
                    "sha256": e.sha256,
                    "byte_length": e.byte_length,
                    "imports": e.imports,
                    "symbols": [
                        {"kind": s.kind, "name": s.name, "line": s.line,
                         "signature": s.signature, "parent": s.parent}
                        for s in e.symbols
                    ],
                }
                for rel, e in self._entries.items()
            },
        }
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name("." + p.name + ".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, p)

    @classmethod
    def load_manifest(cls, path: str | Path) -> Optional["RepoMap"]:
        """Load a previously saved map, or None if absent/unreadable."""
        import json
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        try:
            root = Path(data["root"])
            entries: dict[str, FileEntry] = {}
            for rel, e in data.get("files", {}).items():
                syms = [
                    Symbol(s["kind"], s["name"], s["line"],
                           s.get("signature", ""), s.get("parent"))
                    for s in e.get("symbols", [])
                ]
                entries[rel] = FileEntry(rel, e["sha256"], e["byte_length"],
                                         syms, e.get("imports", []))
            return cls(root, entries, data.get("failures", {}),
                       {k: int(v) for k, v in data.get("languages", {}).items()})
        except (KeyError, TypeError, ValueError):
            return None

    # --- render ---------------------------------------------------------
    def render(self, token_budget: int = _DEFAULT_MAX_TOKENS) -> str:
        """Deterministic, budgeted map text (~4 chars/token budget)."""
        char_budget = max(200, token_budget * 4)
        lines: list[str] = []

        lines.append(f"# Repository map: {self.root.name}")
        lines.append("")
        # language breakdown
        langs = sorted(self._languages.items(), key=lambda kv: (-kv[1], kv[0]))
        lang_str = ", ".join(f"{ext or '(none)'}:{n}" for ext, n in langs[:10])
        lines.append(f"Languages: {lang_str}")
        lines.append("")

        # top-level dirs
        tops: dict[str, int] = {}
        for rel in self._entries:
            top = rel.split(os.sep, 1)[0] if os.sep in rel else "(root files)"
            tops[top] = tops.get(top, 0) + 1
        lines.append("Top-level:")
        for top in sorted(tops):
            lines.append(f"  {top}/  ({tops[top]} files)")
        lines.append("")

        # symbols, prioritised: classes/functions, task-agnostic stable order
        lines.append("Key symbols (file:line kind name):")
        rendered = []
        for rel in sorted(self._entries):
            e = self._entries[rel]
            for s in e.symbols:
                parent = f"{s.parent}." if s.parent else ""
                sig = s.signature or ""
                rendered.append(f"  {rel}:{s.line} {s.kind} {parent}{s.name}{sig}")
        # budget: add until we'd exceed
        for r in rendered:
            if sum(len(x) + 1 for x in lines) + len(r) + 1 > char_budget:
                lines.append("  …[map truncated to budget]")
                break
            lines.append(r)

        return "\n".join(lines)


def _load_gitignore(root: Path) -> list[str]:
    """Read simple .gitignore patterns (best-effort; no full git semantics)."""
    patterns: list[str] = []
    gi = root / ".gitignore"
    try:
        if gi.is_file():
            for raw in gi.read_text(errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                patterns.append(line)
    except OSError:
        pass
    return patterns


def _is_ignored(relpath: str, name: str, gitignore: list[str]) -> bool:
    for pat in gitignore:
        neg = pat.startswith("!")
        p = pat[1:] if neg else pat
        p = p.rstrip("/")
        # match against the name or the relative path (suffix/dir prefix)
        if fnmatch.fnmatch(name, p) or fnmatch.fnmatch(relpath, p):
            return not neg
        if "/" in p and (relpath == p or relpath.startswith(p + "/")):
            return not neg
        if not "/" in p and any(part == p for part in relpath.split("/")):
            return not neg
    return False


def _is_denied(relpath: str, name: str) -> bool:
    low = name.lower()
    for g in _SKIP_GLOBS:
        if fnmatch.fnmatch(low, g.lower()) or fnmatch.fnmatch(relpath.lower(), g.lower()):
            return True
    return False


def _extract_symbols(tree: ast.AST) -> tuple[list[Symbol], list[str]]:
    symbols: list[Symbol] = []
    imports: list[str] = []

    def _sig(node: ast.AST) -> str:
        try:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in node.args.posonlyargs + node.args.args]
                if node.args.vararg:
                    args.append("*" + node.args.vararg.arg)
                if node.args.kwarg:
                    args.append("**" + node.args.kwarg.arg)
                return f"({', '.join(args)})"
        except Exception:
            pass
        return ""

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.cls_stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef):
            symbols.append(Symbol("class", node.name, node.lineno))
            self.cls_stack.append(node.name)
            self.generic_visit(node)
            self.cls_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef):
            parent = self.cls_stack[-1] if self.cls_stack else None
            kind = "method" if parent else "function"
            symbols.append(Symbol(kind, node.name, node.lineno, _sig(node), parent))
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
            self.visit_FunctionDef(node)  # same shape

        def visit_Import(self, node: ast.Import):
            for a in node.names:
                imports.append(a.name)
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom):
            if node.module:
                imports.append(node.module)
            self.generic_visit(node)

    Visitor().visit(tree)
    return symbols, imports


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _looks_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(4096)
        return b"\x00" in chunk
    except OSError:
        return True


def build_repo_map(
    repo_root: str | Path,
    *,
    previous: Optional[RepoMap] = None,
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
    max_files: int = _DEFAULT_MAX_FILES,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> RepoMap:
    """Scan `repo_root` and build a :class:`RepoMap`. Never raises."""
    root = Path(repo_root).resolve()
    entries: dict[str, FileEntry] = {}
    failures: dict[str, str] = {}
    languages: dict[str, int] = {}
    gitignore = _load_gitignore(root)

    if not root.is_dir():
        return RepoMap(root, entries, failures, languages)

    prev_entries = previous._entries if previous is not None else {}

    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # prune skipped / ignored dirs in place
        rel_dir = os.path.relpath(dirpath, root)
        keep = []
        for d in dirnames:
            rel = d if rel_dir == "." else os.path.join(rel_dir, d)
            if d in _SKIP_DIRS or _is_ignored(rel, d, gitignore) or _is_denied(rel, d):
                continue
            keep.append(d)
        dirnames[:] = sorted(keep)

        for fn in sorted(filenames):
            if scanned >= max_files:
                break
            rel = fn if rel_dir == "." else os.path.join(rel_dir, fn)
            if _is_ignored(rel, fn, gitignore) or _is_denied(rel, fn):
                continue
            path = Path(dirpath) / fn
            # confinement: ensure resolved path stays under root
            try:
                path.resolve().relative_to(root)
            except ValueError:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            ext = path.suffix.lower()
            if ext not in languages:
                languages[ext] = 0
            languages[ext] += 1

            if size > max_file_bytes:
                continue
            if _looks_binary(path):
                continue
            scanned += 1

            # incremental reuse: same content hash -> reuse parsed entry
            prev = prev_entries.get(rel)
            try:
                digest = _file_sha256(path)
            except OSError:
                continue
            if prev is not None and prev.sha256 == digest:
                entries[rel] = prev
                continue

            entry = FileEntry(relpath=rel, sha256=digest, byte_length=size)
            if ext == ".py":
                try:
                    source = path.read_text(errors="strict")
                    tree = ast.parse(source, filename=rel)
                    entry.symbols, entry.imports = _extract_symbols(tree)
                except (SyntaxError, ValueError, OSError) as e:
                    failures[rel] = f"{type(e).__name__}: {e}"
                    entry.symbols, entry.imports = [], []
            entries[rel] = entry

        if scanned >= max_files:
            break

    # detect deletions: previous entries not present now are simply dropped
    return RepoMap(root, entries, failures, languages)


def find_repo_root(workspace_path: str | Path) -> Optional[Path]:
    """Return a repo root for `workspace_path`: a git root or the workspace.

    Uses only filesystem markers (a ``.git`` entry); never executes git. Returns
    None when the workspace is not a directory.
    """
    p = Path(workspace_path)
    if not p.is_dir():
        return None
    cur = p.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".git").exists():
            return candidate
    return cur


def build_repo_map_segment(
    workspace_path: str | Path,
    *,
    manifest_path: Optional[str | Path] = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> Optional[str]:
    """Render a repo-map segment for session context, or None if unavailable.

    Reuses a persisted manifest when present (incremental), then re-saves it.
    Never raises; any problem yields None so the caller keeps its normal
    context unchanged.
    """
    try:
        root = find_repo_root(workspace_path)
        if root is None:
            return None
        prev = RepoMap.load_manifest(manifest_path) if manifest_path else None
        m = build_repo_map(root, previous=prev, max_tokens=max_tokens)
        if manifest_path:
            try:
                m.save_manifest(manifest_path)
            except OSError:
                pass
        rendered = m.render(token_budget=max_tokens)
        return "## Repository map (overview; read exact source with Read/Grep)\n" + rendered
    except Exception:
        return None
