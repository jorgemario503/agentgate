# Runner Surface Contract

**Status:** proposed. Defines what `agentgaterunner` publishes for `agentorchestrator`.
**Decided in:** `agentorchestrator/docs/spec/06-runner-reuse/decision-log.md` (DEC-1102)
**Applies to:** the three open-model runners (`deepinfra`, `fireworks`, `local`). Claude is out
of scope — the orchestrator is open-model only.

---

## 1. Why this document exists

Once `agentgaterunner` ships `runners/`, a runner signature change breaks a **second repository**.
Without a stated surface, "we changed a runner" and "we broke the orchestrator" are the same
event, discovered late.

This document names the surface deliberately: **what the orchestrator may depend on**, and what is
internal and free to churn.

---

## 2. The supported surface

Four names. Everything else in `runners/` is internal.

### 2.1 `Runner` — the protocol

`runners.base.Runner`. Seven members, all of which the orchestrator may call:

| Member | Purpose |
|---|---|
| `run_prompt(thread_id, workspace_path, prompt, resume_session_id, post_fn, message_id=None, platform=None) -> (text, session_id)` | Execute one turn |
| `cancel(thread_id) -> bool` | Interrupt an in-flight turn |
| `close_client(thread_id)` | Release one session |
| `close_all()` | Release every session |
| `has_client(thread_id) -> bool` | Is a live client present |
| `current_model_id -> str` | Which model is in use |
| `init_agentdb(db, platform)` | Bind governed memory |

### 2.2 `PlatformConfig`

`runners.base.PlatformConfig`. The orchestrator's handle on **identity and storage**:

| Field | Orchestrator's use |
|---|---|
| `session_store` | Must be supplied with an **absolute** path (§4) |
| `agent_id` | Identity for AgentDB binding — one per agent |
| `allowed_user_ids` / `admin_user_ids` | Unused by the orchestrator; empty |
| `reply_hint` | Unused; auto-approval means no prompts |

### 2.3 `check_blocked`

`runners.base.check_blocked(text, patterns) -> str | None`. Pattern gate applied to user messages
and generated shell commands. The orchestrator passes its own pattern list; it does not depend on
agentgate's defaults.

### 2.4 `ToolPolicy` — **new, not yet implemented**

The scoped approval hook (see §5). Until it exists, the orchestrator uses `enable_auto_approve`.

---

## 3. Explicitly internal

These change freely and the orchestrator must not import them:

- The per-backend client classes (`DeepInfraAgentClient`, `FireworksAgentClient`, `LocalAgentClient`)
- `permission_decision`, `_make_permission_callback`, `_describe_tool_call`, `_rule_target`
- Reasoning helpers, cache-key helpers, tool-compression wiring
- Every `core.*` module except `core.handoff` and the ones below

### 3.1 Core modules the runners pull in

Shipping `runners/` requires their `core/` dependencies. Measured from the source (not assumed):

`config`, `providers`, `sessions`, `history`, `agentdb`, `prompts`, `approvals`, `commands`,
`tools`, `skills`, `reasoning`, `context`

Twelve modules, plus `core.handoff` — which is a **separate** published contract (DEC-108) and may
be imported directly.

Note `context` covers what used to be four flat modules; the M1 grouping moved `artifacts`,
`cache_key`, `tool_pipeline` and `tool_compression` inside it. The orchestrator does not name
either shape — it gets them transitively.

---

## 4. Constraints the orchestrator must honour

| # | Constraint | Why |
|---|---|---|
| 1 | `PlatformConfig.session_store` uses an **absolute** path | Defaults are relative to cwd; N containers means N cwds, so sessions collide or vanish silently |
| 2 | `AGENTDB_MEMORY_DIR` is set **per agent** | SQLite is single-writer; a shared file serializes parallel agents |
| 3 | `AGENTDB_ENABLED` is set **explicitly** | It defaults to `true` |
| 4 | `AGENTGATE_TOOL_ARTIFACT_DIR` is absolute | Same reason as (1) |
| 5 | One bot per Slack thread | AgentGate's gate bot and the orchestrator's bot must never share one |

Constraints 1–4 are configuration. Constraint 5 is a rule nothing currently enforces
(recorded as an accepted limitation in the orchestrator's master spec).

---

## 5. The one gap: `set_model`

`set_model(value)` exists on **all three open runners** and is called by every bot — but is
**not in the `Runner` protocol**. Bots rely on duck typing.

Two options, and this document does not pick one:

- **Add it to the protocol**, making it a supported surface the orchestrator can use to switch
  models per agent.
- **Leave it out**, and the orchestrator sets the model at construction only.

**Recommendation: add it.** An orchestrator running heterogeneous agents (a cheap planner, an
expensive reviewer) will want it, and a name that every implementation provides but the protocol
omits is a surface that exists without being declared — the defect shape this project keeps
finding.

**Also missing from the protocol and worth the same treatment:** nothing else. `set_model` is the
only member all three open runners implement that `Runner` does not declare.

---

## 6. Changes to this contract

Adding a name to §2 is a **feature**. Removing or changing one in §2 is a **breaking change** and
requires a version bump plus an update to the orchestrator's pin (`DEC-402`).

A change to anything in §3 is neither, by definition.

---

## 7. What this contract does not cover

- **Where** the package ships from (PyPI vs path) — that is the packaging decision.
- **Version pinning** — `agentorchestrator` pins `agentgaterunner>=0.3,<0.4`; this document does
  not change it.
- **Claude** — out of scope entirely.
