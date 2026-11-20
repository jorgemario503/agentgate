# Open-Model Agent Skills — Implementation

Feature is **OFF by default**. With `AGENTGATE_SKILLS_ENABLED=false` (default),
no manifest, no tools, and no session state are added — outbound requests are
unchanged (verified by S01).

## Module map

| File | Change |
|---|---|
| `core/skills.py` | **New.** Pure helpers: discovery, `SkillEntry`/`Catalog`, frontmatter parse+validate, `build_manifest`, `list_line`, `read_skill_body`, `SkillsContext`/`build_skills_context`, `SkillSession`, `recommend`/`build_recommendation_nudge`, `record_activation_from_result`, `is_skill_read_tool`. |
| `core/config.py` | Four new env vars (`AGENTGATE_SKILLS_*`), all OFF/default-safe. |
| `core/prompts.py` | `build_system_prompt(..., skills_manifest="")`; manifest appended after base/hard-rules (stable prefix). Empty default = unchanged. |
| `core/tools/registry.py` | `build_tools(..., skills_catalog=None)`; adds `skill_read` + `skill_list` only when a non-empty catalog is passed. |
| `runners/local.py`, `runners/fireworks.py`, `runners/deepinfra.py` | `_skills_context()` helper; client gains `skills_context`/`skill_session` + `_skill_turn`; `query()` bumps the turn; tool loop bypasses compression/summarisation for `skill_read` and records activation; `_build_outgoing_messages` injects the one-time reactivation block as volatile context; `skills_recommendation()` powers Mode B; runner build wires manifest/tools/state. |

Claude runner and AgentDB are not touched.

## Data flow

1. **Build (per thread, in `_get_or_create_client`)**
   - `_skills_context()` → `build_skills_context(enabled, dir, tokens, mode)`.
   - If enabled and catalog non-empty: render `manifest` once; create a
     `SkillSession(catalog)`; pass the catalog to `build_tools` and the manifest
     to `build_system_prompt`; hand `skills_context` + `skill_session` to the client.
   - If disabled/empty: pass `None` everywhere → no change.

2. **Turn (`run_prompt` → `client.query`)**
   - Mode B: `client.skills_recommendation(prompt)` returns a short volatile nudge
     (names only) appended to `ephemeral_context`.
   - `query()` appends the user message and increments `_skill_turn`.

3. **Send (`_build_outgoing_messages`)**
   - Computes `_skill_reactivation_context()` from the *actual trimmed* messages.
   - Injects `[ephemeral, reactivation]` as system messages immediately before the
     latest user turn (volatile; stable prefix untouched).

4. **Tool loop (`receive_response`)**
   - `skill_read` results skip `_tool_pipeline.process` and `_summarize_tool_result`
     (exact body, no lossy compression / cap truncation).
   - On a successful `skill_read` append, `record_activation_from_result` stores
     `name + fingerprint + turn`.

## Catalog config

```
AGENTGATE_SKILLS_ENABLED=false        # master switch
AGENTGATE_SKILLS_DIR=skills           # <dir>/<name>/SKILL.md
AGENTGATE_SKILLS_MANIFEST_TOKENS=600
AGENTGATE_SKILLS_SELECTION_MODE=model # model | recommend
```

Example catalog:

```
skills/
  pdf-reader/
    SKILL.md   # --- name: pdf-reader / description: ... --- instructions
  csv-clean/
    SKILL.md
```

## Security assumptions

- The skills root is operator-controlled. Discovery refuses entries whose
  resolved path escapes the root (symlinks/traversal) and excludes invalid
  frontmatter with a redacted diagnostic.
- `skill_read` validates the requested name against the catalog (never a raw
  path) and re-checks containment at read time.
- `allowed-tools` is informational; it never changes tool approval/permissions.
- No code execution or remote fetch at discovery or read.
- Bodies are returned verbatim; failed discovery is non-fatal; unsafe path
  resolution fails closed.

## Session lifecycle summary

See DECISIONS.md §6. In short: activation only on successful append; presence
checked from the real messages; no repeat while present; one reactivation after
a drop; fingerprint change ⇒ graceful give-up; cold resume safe.

## Tests

- `tests/test_skills.py` — unit (S01–S13).
- `tests/test_skills_runner_integration.py` — outgoing-message inspection (S11/S12/S14/S15).
- `tests/test_skills_guards.py` — no Claude/AgentDB surface (S17), disabled parity (S18).
