# Skills — operator & contributor guide

Agent Skills let the open-model runners (`local`, `fireworks`, `deepinfra`) load
task-specific procedures on demand from portable `SKILL.md` files. The agent sees
a compact manifest of the available skills and reads a skill's full instructions
only when it decides one is relevant (or when a user forces one).

This guide covers the model, authoring, configuration, extension points and
testing. For a copy-paste quick start, see the "Skills" section of the
[README](../README.md#skills-open-model-opt-in--off-by-default).

> Claude and AgentDB are not involved — skills are a separate, opt-in capability
> for the open-model runners. With `AGENTGATE_SKILLS_ENABLED=false` (the default)
> nothing here is consulted and outbound requests are byte-for-byte unchanged.

> **Source of truth:** [`injection-ground-truth.md`](injection-ground-truth.md) —
> the asset table and the per-backend split. This page goes deeper on skills.

## How it works

1. **Catalog discovery (read-only, offline).** At client build time each runner
   scans `AGENTGATE_SKILLS_DIR` (default `skills/`) for a directory per skill
   containing a `SKILL.md` with YAML frontmatter. Bodies are not parsed here —
   only frontmatter — so discovery is cheap and never executes project code.
2. **Manifest in the stable prefix.** A compact, deterministic manifest (bounded
   by `AGENTGATE_SKILLS_MANIFEST_TOKENS`) is injected into the cacheable system
   prefix. Because it is stable across turns, the provider prompt cache holds.
3. **On-demand read.** The model calls the `skill_read` tool (exact body, never
   lossily compressed); `skill_list` enumerates what is available.
4. **Activation modes.** See below. Explicit `//skill-name` invocation works
   whenever skills are enabled, regardless of mode.

Everything skills-related lives in one module, `core/skills/catalog.py`; it is
pure (no runner/provider/AgentDB knowledge) and the three runners call it from
their existing client-build seam. Nothing is duplicated per runner.

## Authoring a skill

One folder per skill under the catalog, each with a `SKILL.md`:

```
skills/
  create-ticket/
    SKILL.md
```

```markdown
---
name: create-ticket
description: Create internal tickets using our required format.
metadata:
  agentgate:
    triggers:            # optional — used only by Mode B (recommend)
      - create a ticket
      - usual ticket format
---

# Ticket format
- Title prefix: [PAY] for payments, [OPS] for ops.
- Sections: Summary, Impact, Steps to Reproduce, Owner.
- If the owner is unknown, write "Owner: UNASSIGNED" — do not invent a name.
```

Rules:

- `name` must be lowercase, hyphen-separated, and match the folder name.
- `name` and `description` are required. `license`, `compatibility`, `metadata`
  and `allowed-tools` are optional.
- Frontmatter is parsed with PyYAML `safe_load` under bounded input — no new
  runtime dependency, and malformed frontmatter is rejected, not guessed.

## Configuration

```env
AGENTGATE_SKILLS_ENABLED=false                # master switch (default off)
AGENTGATE_SKILLS_DIR=skills                   # catalog directory
AGENTGATE_SKILLS_MANIFEST_TOKENS=600          # cap on the injected manifest
AGENTGATE_SKILLS_SELECTION_MODE=model         # model | recommend (Mode B)
```

### Where the catalog lives

`AGENTGATE_SKILLS_DIR` accepts any path, not just one inside this repository —
a shared team folder is a supported setup:

```env
AGENTGATE_SKILLS_DIR=/opt/team/skills         # absolute: the clearest form
AGENTGATE_SKILLS_DIR=~/agent-skills           # `~` is expanded to your home
AGENTGATE_SKILLS_DIR=skills                   # relative: resolved from the CWD
```

A relative value is resolved against the **process working directory**, not the
repository root, so it means different things depending on where the bot was
launched. That is fine for `python run.py` from the checkout, and fragile under
a service manager that sets its own `WorkingDirectory`. The startup line always
names the directory that was actually used, so this is visible rather than
inferred:

```
[startup] Skills: 3 available (milestone-implement, ...), mode 'model', dir /opt/team/skills
```

An unrecognised `AGENTGATE_SKILLS_SELECTION_MODE` falls back to `model` and is
reported on its own line, as is any skill excluded during discovery.

### Activation modes

| Mode | Setting | Behavior |
|---|---|---|
| **A — model-selected** (recommended) | `model` (default) | The model decides when a skill is relevant and calls `skill_read`. "No skill" is a valid choice. |
| **C — explicit** | automatic when enabled | A user prefixes `//<skill-name>` to force a skill for one message. Unknown names error; no silent substitution. |
| **B — recommend** (experimental) | `recommend` | Deterministic keyword triggers nudge the model toward a skill. Lower-priority hints only; never forces activation or includes the body. |

Evaluation showed no material benefit from Mode B over Mode A, and it adds a
false-positive surface — keep the default `model`.

## Security model

- Skills are **operator-controlled files** you place in the catalog.
- Skill instructions are delivered as clearly labeled **user-role** content —
  never system instructions — so they cannot override the hard rules or grant
  tool permissions (`allowed-tools` in a skill is informational only).
- Loads are **path-contained**: `SKILL.md` must resolve inside the catalog (no
  symlink/traversal escapes), and a body whose fingerprint changed since read is
  not silently restored.
- These reduce risk but **do not establish immunity to prompt injection** — only
  add skills you trust as you would any code.

## Multi-turn behavior & caching

- Skill instructions are not repeated while they remain in context; if a body is
  trimmed/compacted **within the active turn**, it is restored once.
- A **subsequent user turn does not auto-restore** an earlier body; the model can
  `skill_read` again if still relevant — this keeps context lean.
- The manifest sits in the stable prefix and is cached by the provider across
  turns. That prefix cache is **not** agent memory: skill state is session-local
  and reset per session.

## Testing & evaluation

Deterministic offline tests (no network):

```bash
python -m pytest tests/test_skills.py tests/test_skills_activation.py \
                 tests/test_skills_guards.py tests/test_skills_multiturn.py \
                 tests/test_skills_runner_integration.py -q
```

The isolated, cohort-separated OFF/A/B/C evaluation harness is
`scripts/eval_skills.py`. It is deliberately separate from the shared catalog so
cohorts cannot contaminate each other.

## Where it lives

| File | Role |
|---|---|
| `core/skills/catalog.py` | Discovery, manifest, on-demand read, recommendations (pure) |
| `core/config.py` | `AGENTGATE_SKILLS_*` flags |
| `runners/{local,fireworks,deepinfra}.py` | Call the catalog at client build |
| `scripts/eval_skills.py` | Isolated OFF/A/B/C evaluation harness |
| `skills/{milestone-implement,milestone-review,product-spec-driven}/` | The shipped role skills |
