# Open-Model Agent Skills — Decisions

Status: implementation landed, feature **OFF by default**, pending the
DeepInfra evaluation in `EVALUATION.md` / `RESULTS.md` before any default change.

Source commit at implementation start: `8410073`
(`test(agentdb): real-dependency memory integration for 0.5 release`), merge-base
with `main` = `2001f33`. Branch: `feat/open-model-skills`.

> Note: the task prompt referenced `docs/spec/skills/`; the authoritative spec
> (`docs/spec/open-model-spec.md` §12) specifies `docs/skills/`, so the
> deliverables live here. This is a path discrepancy only — content is per spec.

## 1. Locked boundaries

| Decision | Choice | Rationale |
|---|---|---|
| Scope | open-model runners only: `local`, `fireworks`, `deepinfra` | Spec §1. Claude has native context management; AgentDB is governed memory, not skills. |
| Claude | **untouched** | Guard test asserts no skill refs in `runners/claude.py`. |
| AgentDB | **untouched** (no skill handling/migration/governance) | Guard test asserts `core/agentdb.py` has no skill surface. |
| Selection, default | **A: model-selected** | Model decides whether to call `skill_read`; "no skill" is legitimate. No forced full-body auto-injection. |
| Selection, experimental | **B: harness-assisted recommendations** | Deterministic heuristic nudge, behind its own flag; A/B comparison decides if it ships. |
| Catalog | **one operator-configured shared catalog** | `AGENTGATE_SKILLS_DIR`; per-session state isolated; never depends on AgentDB. |
| Default | **OFF** (`AGENTGATE_SKILLS_ENABLED=false`) | Disabled path is byte-for-byte unchanged (S01). |
| Format | Agent Skills standard `SKILL.md` | See §3. |
| Dependency | reuse existing **PyYAML** (`safe_load`), bounded input | No new runtime dependency. |
| `skills-ref` | **not adopted** | Upstream README marks it demonstration-only; its `strictyaml` dep and CLI are not worth the maintenance cost for one parser. We mirror its validation rules instead. |

## 2. Non-goals (excluded)

Skills authoring, approval, auditing, staleness metrics, auto-evolution, version
registry, marketplace, embeddings/vector DB, an extra planning LLM, automatic
shell execution from skills, and any AgentDB skill integration.

## 3. Format and validation

A skill is `<AGENTGATE_SKILLS_DIR>/<skill-name>/SKILL.md` with YAML frontmatter:

- `name` (required): lowercase, digits/letters/hyphens, ≤64 chars, no
  leading/trailing/consecutive hyphens, **must match the directory name**.
- `description` (required): non-empty, ≤1024 chars.
- `license`, `compatibility` (≤500), `metadata` (mapping), `allowed-tools`
  (informational only) — optional.
- Unknown frontmatter fields are rejected (excluded + diagnostic).

Limits mirror `skills-ref`'s validator (`MAX_*` constants in `core/skills.py`).
Parsing is bounded: a `SKILL.md` over 64 KiB is excluded, not read unbounded.

## 4. Dependency audit (Phase 0)

- `core/prompts.py`: `build_system_prompt(state, history_path, workspace_path,
  agentdb_middleware, session_id)` — sections emitted stable→volatile. Added
  optional `skills_manifest` placed right after base/hard-rules (stable prefix).
- `core/tools/registry.py`: `build_tools(workspace_path, store, *, output_cap,
  bash_timeout, pipeline=None)` — one shared registry for all three runners.
  Added optional `skills_catalog` → conditionally adds `skill_read`/`skill_list`.
- `core/sessions.py`: `SessionState` (NEW/WARM/COLD/RESUME_AFTER_TIMEOUT) drives
  whether a resume block is injected. Untouched.
- `core/history.py`: `JsonFileHistoryStore`, `<thread_id>.json`. Untouched.
- Runners: `_get_or_create_client` builds `system_prompt` + `build_tools` +
  client per thread; `receive_response` runs the tool loop; `_build_outgoing_messages`
  injects `_ephemeral_context` before the latest user turn. These were the only
  insertion seams.
- Compaction/compression: `_trim_history`, `_maybe_compact_history`,
  `_summarize_tool_result`, `_tool_pipeline.process` — skill bodies bypass the
  last two (exact return).

No older line numbers applied; the seams above are from this checkout at `8410073`.

## 5. Module decision

A single pure module, `core/skills.py` (discovery/manifest/read/recommend +
`SkillsContext` + `SkillSession`). One home for catalog logic, no runner
duplication of business rules, minimal module count. No new package, no new
dependency.

## 6. Session lifecycle decisions

- Activation is recorded **only** when a `skill_read` tool result is successfully
  appended (not on recommendation, not on error).
- Presence is derived by **scanning the actual outgoing messages** for the verbatim
  body — never from activation state alone (S11/S12).
- If a body survives trimming/compaction, it is **never repeated**.
- If an active body was dropped, it is re-injected **once** as volatile context
  before the latest user turn (never into the stable system prefix), then normal
  history policy resumes.
- If the on-disk body changed since activation (fingerprint mismatch), re-injection
  is refused (graceful give-up; the model can `skill_read` again).
- Cold resume: presence is judged from the reloaded messages; a stale "active"
  claim without a body present triggers the same one-time recovery path.

## 7. Cache / prefix decisions

- Manifest is deterministic (sorted) and byte-identical across warm turns; it
  lives inside the cached stable prefix.
- B recommendations and reactivation are injected as **volatile** system messages
  before the latest user turn, so they never disturb the cached prefix.
- Skill bodies arrive as tool responses (naturally after the prefix).

## 8. Security decisions

- `SKILL.md` is untrusted lower-priority data: it cannot edit hard rules, tool
  approval, filesystem access or network authorization.
- `allowed-tools` never widens permissions.
- Discovery resolves the canonical path and refuses anything escaping the skills
  root (symlink/traversal). `skill_read` validates the name against the catalog
  (never a raw path) and re-checks containment at read time.
- No code execution or remote fetch during discovery or read.
- Diagnostics name the directory/reason, never file contents.

## 9. Config surface (new names kept to four)

| Var | Default | Meaning |
|---|---|---|
| `AGENTGATE_SKILLS_ENABLED` | `false` | Master switch. |
| `AGENTGATE_SKILLS_DIR` | `skills` | Operator-configured shared catalog. |
| `AGENTGATE_SKILLS_MANIFEST_TOKENS` | `600` | Manifest budget. |
| `AGENTGATE_SKILLS_SELECTION_MODE` | `model` | `model` (A) or `recommend` (B). |
