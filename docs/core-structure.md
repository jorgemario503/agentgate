# Core structure — recommendation

`core/` is the cross-platform layer (bots depend on it; it never imports from
`bots/` or concrete runners — see AGENTS.md). It currently holds ~20 flat
modules. This note records the recommended target structure and the
incremental path we are taking toward it.

## Principle

Group modules by **concern**, not by file type. A flat `core/` is fine while it
is small, but once it mixes a dozen unrelated responsibilities, a shallow
package-per-concern layout makes ownership and imports obvious:

- imports read as intent (`from core.sessions.store import ...`)
- a whole subsystem can be moved or tested in isolation
- new code lands in the right place by default, so the layout does not drift

Two hard rules stay unchanged: `core/` never imports `bots/`; runners are
reached through the `Runner` protocol, never imported from a bot hot path.

## Recommended target layout

```
core/
  config/        # settings + provider specs        config.py, providers.py
  prompts/       # prompt assembly                   prompts.py
  sessions/      # session registry + history        sessions.py, history.py
  tools/         # shared tool registry  (DONE)      registry.py
  agentdb/       # governed memory + skills surface  agentdb.py
  context/       # token efficiency                  artifacts, dedup, tool_compression,
                                                     tool_pipeline, repo_map, compression
  commands/      # command registry                  commands.py
  runtime/       # approvals, cache_key, reasoning/  approvals.py, cache_key.py
  skills/        # file-based skills (future)        see docs/spec/open-model-skills.md
```

Each is a package with an `__init__.py`; submodules keep their current names so
the move is mechanical (`sessions.py` -> `sessions/store.py`, etc.).

## Incremental path (what we do now)

A full reshuffle would touch ~50 import sites and bury real changes in churn.
So we convert **one concern at a time**, starting with the one we are already
touching:

1. **`core/tools/` — DONE.** The 8-tool registry that was triplicated across
   `local` / `fireworks` / `deepinfra` now lives in `core/tools/registry.py`.
   Runners import `Tool, build_tools` from it and pass their provider caps
   (`output_cap`, `bash_timeout`) as parameters. Claude is unaffected (it uses
   the SDK tools + its own MCP server).
2. Next candidates, when each is touched for another reason (no churn for its
   own sake): `core/context/` (the compression/repo-map cluster added recently),
   then `core/sessions/`, then `core/config/`.
3. Leave `contracts`/`runtime` last — they are the most widely imported.

### Migration mechanics for a concern

- `git mv core/<file>.py core/<concern>/<name>.py` and add
  `core/<concern>/__init__.py`.
- Update imports; keep a thin re-export in the old location only if a long tail
  of callers makes it worthwhile (then delete it once callers move).
- Run the full suite: `python -m pytest -q` (green = behaviour preserved).
- One concern per commit so review stays readable.

## Anti-patterns to avoid

- A new "misc"/"utils" bucket — that just moves the flat problem into a folder.
- Circular deps: `context/` must not import `agentdb/`; shared primitives go in
  a lower layer both can import.
- Re-exporting everything from `core/__init__.py`; prefer explicit module paths.
