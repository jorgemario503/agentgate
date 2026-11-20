"""Shared tool registry for the open-model runners.

The three OpenAI-compatible runners (local / fireworks / deepinfra) expose the
same eight tools with identical handlers. This module is the single source of
truth so the registry is not triplicated in each runner.

Per-provider differences are parameters, not module globals:

- ``output_cap``      truncation limit for a single tool result (provider setting)
- ``bash_timeout``    shell-command timeout in seconds (provider setting)
- ``pipeline``        optional ToolResultPipeline -> adds ``tool_result_read``

The Claude runner does NOT use this module: it relies on the SDK's built-in
tools plus its own MCP directory-tools server.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class Tool:
    """A tool exposed to a model: OpenAI function schema + a sync handler."""

    schema: dict
    handler: Callable[[dict], str]


def _tool_schema(name: str, description: str, params: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": params, "required": required},
        },
    }


def _resolve(cwd: str, path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (Path(cwd) / p)


def _make_bash(cwd: str, output_cap: int, bash_timeout: int) -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        cmd = args.get("command", "")
        if not cmd:
            return "ERROR: no command provided"
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=bash_timeout,
            )
        except subprocess.TimeoutExpired:
            return f"ERROR: command timed out after {bash_timeout}s"
        except Exception as e:
            return f"ERROR: {e}"
        out = proc.stdout or ""
        if proc.stderr:
            out += f"\n[stderr]\n{proc.stderr}"
        if proc.returncode != 0:
            out += f"\n[exit {proc.returncode}]"
        return out[:output_cap] or "(no output)"
    return handler


def _make_read(cwd: str, output_cap: int) -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        try:
            return _resolve(cwd, args.get("file_path", "")).read_text()[:output_cap]
        except Exception as e:
            return f"ERROR reading {args.get('file_path')}: {e}"
    return handler


def _make_write(cwd: str) -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        try:
            target = _resolve(cwd, args.get("file_path", ""))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(args.get("content", ""))
            return f"Wrote {target}"
        except Exception as e:
            return f"ERROR writing {args.get('file_path')}: {e}"
    return handler


def _make_edit(cwd: str) -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        try:
            target = _resolve(cwd, args.get("file_path", ""))
            text = target.read_text()
            old, new = args.get("old_string", ""), args.get("new_string", "")
            count = text.count(old)
            if not old or count == 0:
                return f"ERROR: old_string not found in {target}"
            if count > 1:
                return f"ERROR: old_string is ambiguous ({count} matches) in {target}"
            target.write_text(text.replace(old, new, 1))
            return f"Edited {target}"
        except Exception as e:
            return f"ERROR editing {args.get('file_path')}: {e}"
    return handler


def _make_glob(cwd: str) -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        pattern = args.get("pattern", "")
        base = _resolve(cwd, args.get("path", ".")) if args.get("path") else Path(cwd)
        try:
            matches = sorted(str(p) for p in base.glob(pattern))
        except Exception as e:
            return f"ERROR in glob: {e}"
        return "\n".join(matches[:200]) or "(no matches)"
    return handler


def _make_grep(cwd: str) -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        pattern = args.get("pattern", "")
        base = _resolve(cwd, args.get("path", ".")) if args.get("path") else Path(cwd)
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"ERROR: bad regex: {e}"
        hits: list[str] = []
        files = [base] if base.is_file() else base.rglob("*")
        for f in files:
            if not f.is_file():
                continue
            try:
                for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{f}:{i}:{line[:200]}")
                        if len(hits) >= 200:
                            return "\n".join(hits) + "\n(truncated at 200)"
            except Exception:
                continue
        return "\n".join(hits) or "(no matches)"
    return handler


def _make_lookup_directory(store) -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        path = store.lookup_directory(args.get("name", ""))
        return path or f"No directory registered under '{args.get('name')}'."
    return handler


def _make_remember_directory(store) -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        store.remember_directory(args.get("name", ""), args.get("path", ""))
        return f"Remembered '{args.get('name')}' -> {args.get('path')}"
    return handler


def build_tools(
    workspace_path: str,
    store,
    *,
    output_cap: int,
    bash_timeout: int,
    pipeline=None,
    skills_catalog=None,
) -> dict[str, Tool]:
    """Build the shared 8-tool registry (plus conditionals).

    ``output_cap`` and ``bash_timeout`` come from the provider settings, so each
    runner passes its own values; the registry itself holds no module globals.

    ``skills_catalog`` (a ``core.skills.Catalog``) adds the ``skill_read`` and
    ``skill_list`` tools when the skills feature is enabled and the catalog is
    non-empty. When ``None`` (the default) the registry is byte-for-byte the
    same as before skills existed — no extra schema, so disabled mode is
    unchanged.
    """
    def t(name, desc, params, required, handler):
        return Tool(schema=_tool_schema(name, desc, params, required), handler=handler)

    tools = {
        "Bash": t(
            "Bash", "Run a shell command in the session workspace.",
            {"command": {"type": "string"}}, ["command"],
            _make_bash(workspace_path, output_cap, bash_timeout),
        ),
        "Read": t(
            "Read", "Read a text file from the local filesystem.",
            {"file_path": {"type": "string"}}, ["file_path"],
            _make_read(workspace_path, output_cap),
        ),
        "Write": t(
            "Write", "Write (overwrite) a text file on the local filesystem.",
            {"file_path": {"type": "string"}, "content": {"type": "string"}},
            ["file_path", "content"], _make_write(workspace_path),
        ),
        "Edit": t(
            "Edit", "Replace one exact occurrence of old_string with new_string in a file.",
            {"file_path": {"type": "string"}, "old_string": {"type": "string"},
             "new_string": {"type": "string"}},
            ["file_path", "old_string", "new_string"], _make_edit(workspace_path),
        ),
        "Glob": t(
            "Glob", "List files matching a glob pattern.",
            {"pattern": {"type": "string"}, "path": {"type": "string"}},
            ["pattern"], _make_glob(workspace_path),
        ),
        "Grep": t(
            "Grep", "Search file contents for a regular expression.",
            {"pattern": {"type": "string"}, "path": {"type": "string"}},
            ["pattern"], _make_grep(workspace_path),
        ),
        "lookup_directory": t(
            "lookup_directory", "Resolve a known directory name to its absolute path.",
            {"name": {"type": "string"}}, ["name"], _make_lookup_directory(store),
        ),
        "remember_directory": t(
            "remember_directory", "Persist a name -> absolute path mapping for future lookups.",
            {"name": {"type": "string"}, "path": {"type": "string"}},
            ["name", "path"], _make_remember_directory(store),
        ),
    }
    if pipeline is not None:
        from core.tool_pipeline import tool_result_read_handler
        tools["tool_result_read"] = t(
            "tool_result_read",
            "Retrieve the exact original text of a compressed tool result by its "
            "artifact id (bounded by offset/limit).",
            {"artifact_id": {"type": "string"},
             "offset": {"type": "integer"},
             "limit": {"type": "integer"}},
            ["artifact_id"], tool_result_read_handler(pipeline),
        )
    if skills_catalog is not None and skills_catalog.entries:
        from core.skills import read_skill_body, list_line, SkillReadError

        def _skill_read(args: dict) -> str:
            name = args.get("name", "")
            try:
                return read_skill_body(skills_catalog, name)
            except SkillReadError as e:
                return f"ERROR: {e}"

        def _skill_list(args: dict) -> str:
            return list_line(skills_catalog)

        tools["skill_read"] = t(
            "skill_read",
            "Load the full instructions of an available skill by name. Call this "
            "only when a skill is relevant to the current task.",
            {"name": {"type": "string"}}, ["name"], _skill_read,
        )
        tools["skill_list"] = t(
            "skill_list",
            "List every available skill (name + description), including any not "
            "shown in the prompt.",
            {}, [], _skill_list,
        )
    return tools
