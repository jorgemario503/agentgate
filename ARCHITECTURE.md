# Architecture

How AgentGate is put together, and why. For setup and usage see
[README.md](README.md). For conventions when changing the code see
[AGENTS.md](AGENTS.md).

---

## The idea in one paragraph

AgentGate is a **transport adapter**, not an agent framework. A messaging
platform delivers a message; a bot converts it into a prompt; a runner executes
that prompt against a model backend with real tool access; every potentially
destructive tool call is gated on human approval. Everything platform-specific
lives in `bots/`, everything model-specific lives in `runners/`, and the pieces
that must not drift between them live in `core/`.

```
  Slack ──┐
Telegram ─┼─→  bots/*.py  ─→  core/  ─→  runners/*.py  ─→  model backend
 WhatsApp ─┘   (transport)   (shared)     (execution)        (Claude / local)
                                  │
                                  └─→  agent-sessions/<platform>/<thread>/   (disk)
```

---

## Layers

### `bots/` — transport

One module per platform. Each owns its transport (Slack Socket Mode, Telegram
long polling, WhatsApp webhook), its argument parsing, and its runner
selection. A bot has four jobs:

1. Authenticate the sender against an allowlist.
2. Convert the platform event into a `thread_id` plus text.
3. Try the command registry; if unmatched, relay to the runner.
4. Deliver the reply, chunked to the platform size limit.

Bots must not contain command logic or model logic. Anything a second platform
would also need belongs in `core/`.

**Command forms differ by platform, deliberately:**

| Platform | Form | Mechanism |
|---|---|---|
| Telegram | `/autoapprove on` | PTB matches on the slash-stripped text but leaves `message.text` intact, so the registry strips the slash |
| Slack | `@bot autoapprove on` | `bots/slack.py` strips the `<@U…>` mention before dispatch |
| WhatsApp | *(none)* | Relays every message to the agent; does not dispatch commands |

### `core/` — shared, platform-agnostic

| Module | Responsibility |
|---|---|
| `config.py` | Loads dotenv first (every entrypoint imports it before anything else) and exposes module-level constants. |
| `providers.py` | Declarative provider specs plus the four-layer precedence resolution used by open-model runners. |
| `sessions.py` | Per-platform session registry on disk, workspace creation, directory tool, audit log, idle reaper, and `SessionState`/`classify_session`. |
| `prompts.py` | Every prompt surface: base instructions, hard rules, resume block, AgentDB memory, and the compaction summariser. Owns section ordering and file loading. |
| `history.py` | `JsonFileHistoryStore` — one `<name>_history/<thread_id>.json` rule for all open-model runners. |
| `cache_key.py` | Provider allowlist for session-scoped prompt cache keys. |
| `approvals.py` | The async approval/answer gate and the in-memory auto-approve set. |
| `commands.py` | `CommandSpec` registry, `parse_command`, `dispatch`, and every command implementation. |
| `agentdb.py` | Optional governed-memory integration surface. |
| `reasoning/` | Static registry of per-model reasoning parameter shapes plus resolution. |

Nothing in `core/` may import from `bots/`. Runners are reached through the
`Runner` protocol, and `core/commands.py` holds exactly one runner reference per
process, bound at startup via `use_runner()`.

### `runners/` — execution

`runners/base.py` defines the structural `Runner` protocol
(`run_prompt`, `cancel`, `close_client`, `close_all`, `has_client`, plus the
AgentDB methods). Every backend implements it:

| Runner | Backend | Permission gate |
|---|---|---|
| `claude.py` | Claude Agent SDK (spawns the local `claude` CLI) | SDK `can_use_tool` callback |
| `deepinfra.py` | DeepInfra (`AsyncOpenAI` + `base_url`) | tool-call loop, before execution |
| `fireworks.py` | Fireworks (`AsyncOpenAI` + `base_url`) | tool-call loop, before execution |
| `local.py` | Ollama / MLX / llama.cpp (raw `httpx`) | tool-call loop, before execution |

The two hosted runners both ride on the `openai` package with only a `base_url`
override, so neither needs a vendor SDK. `local.py` deliberately keeps raw
`httpx`: it targets arbitrary OpenAI-compatible servers of varying strictness,
and the reasoning payload already passes `extra_body` as a plain body key,
which would collide with the SDK's own `extra_body` parameter.

### Prompt assembly

Every prompt surface lives in `core/prompts.py`; the runners only pass state in.
Sections are emitted **most-stable first**, because providers cache a prompt by
matching from the first character and discard the cached prefix on the first
difference:

```
## Base instructions      stable across every turn of every session
## Hard rules             stable; the security instructions
## Resuming an existing session   stable for one session (cold resume only)
## Memory from previous sessions  may vary per session
```

Section headings are fixed constants (`SECTION_HEADINGS`) injected by the
builder, never read from a prompt file, so editing a file cannot break or
duplicate the structure. Per-turn memory is *not* part of this prefix: the
runners inject it as an ephemeral system message immediately before the latest
user turn, so it never disturbs the cached prefix.

The text itself comes from `prompts/*.md` (`base.md`, `hard_rules.md`,
`resume.md`, `summary.md`), relocatable via `AGENTGATE_PROMPTS_DIR`. Leading
`#` comment lines are stripped and never reach the model. A missing, empty, or
unreadable file falls back to a named `FALLBACK_*` constant — never to an empty
prompt, which would silently drop the hard rules. **All four runners read these
files**, Claude included: `hard_rules.md` is appended to Claude's preset prompt
as well as folded into the open-model base prompt, so one edit reaches every
backend.

### Session state and resume

`core/sessions.py::classify_session(meta, has_client)` decides whether a turn
stands at a session boundary or inside one. The inputs are the registry entry and
whether *this process* still holds a live client — never the session id, because
the open-model runners always pass `thread_id` as the session id and so cannot
distinguish a resume from a fresh start.

| State | Condition | Prompt |
|---|---|---|
| `SESSION_START` | no registry entry (first ever, or after `/stop`) | base + hard rules |
| `SESSION_WARM` | entry, client alive | none — history is in RAM |
| `SESSION_RESUME` | entry, client gone (process restart, or a failed turn) | base + hard rules + resume |

`SESSION_START` and `SESSION_RESUME` are the two boundary states
(`state.is_boundary`); per-session context such as the orientation and repo-map
blocks is injected there and nowhere else.

There is deliberately **no idle-expiry state**. The reaper's lifecycle effect is
closing the client, which `has_client=False` already reports; a row it flagged
classifies as `SESSION_RESUME` like any other. Whether the user was *told* about
the idleness is presentation, recorded separately in `idle_notified_at`, and it
only selects the opener text (`⏰ Session went idle — resuming`). No state or
injection decision reads it.

The resume block names the full history file, its format and its `role` values
(including `tool`), then replays only the last two turns — tool output stays on
disk rather than in the prompt.

On resume the system prompt is **authoritative**: `apply_system_prompt()`
replaces slot 0 of the reloaded history rather than skipping it. The saved
history always begins with the previous process's system message, so inserting
only when absent silently discarded every freshly built prompt.

### Turn context injection

> **Source of truth:** [`docs/injection-ground-truth.md`](docs/injection-ground-truth.md) holds
> the complete asset table, the ownership rule (agentdb generates, AgentGate decides
> prevalence), the open-model/Claude and Slack/Telegram/WhatsApp splits, and the compression
> matrix. This section expands it; where they differ, the ground truth wins.

`core/injection.py` decides what extra context a turn carries. Producers emit
`Injection` values declaring a `source` (ownership), an `asset`, and a scope;
`TurnInjections` filters by the turn's session state and composes each block
under its own heading. Nothing is merged into one string, so no producer can
silently replace another.

| Source | Asset | Scope |
|---|---|---|
| agentgate | `orientation` | session boundary |
| agentgate | `repo_map` | session boundary |
| agentgate | `skills_nudge` | every turn |
| agentdb | `memory_records` | every turn |
| agentdb | `compaction_summary` | session resume |
| agentdb | `compaction_checkpoint` | never |
| agentdb | `workspace` | session boundary |
| agentdb | `work_context` | every turn |

A **boundary** turn is a session start or resume. Scope is consulted *before* a
producer runs, not after, because building the repo map walks the repository —
running it and discarding the result would keep the per-message scan the scope
exists to prevent.

The output is composed into the *ephemeral* context, which the open-model runners
insert immediately before the last user message. It never touches
`messages[0]`, so the cached system prefix stays byte-identical across a session.
That matters most for Telegram and WhatsApp: their address — and therefore the
provider cache key — is permanent, so per-session text in the system prompt would
mutate a long-lived cache lineage at every session start.

AgentDB's assets are split by **what a record describes**, not by which call
returned it: one retrieval pass mixes all four families. The record key is the
only reliable classifier — the store's `kind` column is `source` for every row,
so it cannot tell a compaction checkpoint from an episodic fact.

| Key prefix | Family | Why that scope |
|---|---|---|
| `memory/episodic/` | episodic | written about this session's own work; answers a question |
| `memory/canonical/` | canonical | derived memory, query-relevant by construction |
| `memory/compaction/summary/<sid>` | handover note | describes the session's past; useful only to a process that lost the transcript |
| `memory/compaction/checkpoint/<sid>` | tombstone | carries no content — verified against a real store, where all 16 rows were pure `{"type": "checkpoint"}` stubs |

Checkpoints are declared `NEVER` rather than silently dropped, so the table says
what happens to them. Summaries are `SESSION_RESUME`: repeating a handover note
on every turn of a live session is the same noise as the stubs.

### Session-scoped cache keys

`core/cache_key.py` pins each conversation to its own provider cache lineage,
keyed by **provider** rather than runner class so Claude and any future SDK
backend are excluded by construction:

| Provider | Mechanism |
|---|---|
| deepinfra | `extra_body={"prompt_cache_key": thread_id}` |
| fireworks | `extra_headers={"x-session-affinity": thread_id}` |
| ollama / mlx | opt-in via `AGENTGATE_PROMPT_CACHE_KEY` |

Both are **request-level**: the key is a sibling of `messages` in the body, or
an HTTP header. It is never placed inside message content — that would spend
tokens, put volatile data in the cache prefix, and still land after the system
prompt. The model never sees a session id; continuity comes from the replayed
`messages` array. If a server rejects the key the request is retried without it,
so a cache optimisation never costs a turn.

### Conversation history on disk

All open-model runners use one rule: `<session_base>/<name>_history/<thread_id>.json`,
via `core/history.py`. Only one runner class serves `local`, so its name is the
provider (`ollama_history`, `mlx_history`), keeping two local providers from
sharing a conversation. Migrating from the old `fw_history` / `di_history`
directories is a manual step; there is no fallback, so an unmigrated directory
reads as empty rather than silently mixing two locations.

---

## The approval gate

This is the core security property: **the agent never silently does something
destructive.**

```
agent wants a tool call
        |
        +- blocked pattern? -------------> refuse, no prompt
        |
        +- /autoapprove on? -------------> allow, write audit log
        |
        +- post prompt to chat
               |
               +- "yes" / "no" ----> allow / deny, write audit log
               +- no reply within APPROVAL_TIMEOUT_SECONDS (4h) -> deny
```

Implementation notes that matter:

- **State is in memory only.** `_auto_approve` is a `set[str]` and `_pending` is
  a `dict[str, list[dict]]` in `core/approvals.py`. Both reset on bot restart,
  by design — a restart is a natural "revoke everything".
- **One queue per thread.** Concurrent tool calls for the same thread are
  serialized through `_advance_queue`, so the user is never asked two questions
  at once and answers cannot be applied to the wrong request.
- **Blocked patterns still apply under auto-approval.** `/autoapprove` skips the
  question, not the policy.
- **Everything is audited** to `<session_dir>/audit_log.jsonl`.

---

## Session model

State is **isolated per platform and per thread**, on disk:

```
agent-sessions/
├── slack/<thread_ts>/workspace/
├── telegram/<chat_id>/workspace/
├── telegram/<bot_name>/<chat_id>/workspace/
└── whatsapp-linked/<phone>/workspace/
```

Each bot instantiates its own `SessionStore`, so two platforms can never collide
on the same thread id. Only the Claude runner writes `settings.json` into a
workspace (`write_claude_settings`), which is where the CLI's own allow/deny
rules live; the bots pass `claude_settings=wants_claude_settings(runner)` so an
open-model session does not leave a Claude settings file behind.

`thread_id` is the platform's native identifier rendered as a string — a Slack
thread timestamp, a Telegram chat id, or a WhatsApp phone number. The runner,
the approval gate, and the audit log all key off that single string.

**Lifecycle:** the first message creates the session and workspace; subsequent
messages reuse it; the idle reaper expires it after `IDLE_TIMEOUT_SECONDS`
(default 8h). A `/stop` (or `stop session`) tears down the client and removes
the registry row but keeps the history file, so a reopened thread continues its
transcript — history is keyed by `thread_id`, so nothing leaks between threads.

---

## Configuration precedence

Open-model runners resolve settings through four layers, lowest to highest:

1. Built-in default from the `ProviderSpec`.
2. **Global** — `MAX_TOOL_ITERATIONS`
3. **Per-provider** — `DEEPINFRA_MAX_TOOL_ITERATIONS`
4. **CLI** — `--set MAX_TOOL_ITERATIONS=99`, for this process only

Every resolved value records its origin (`default` / `global` / `provider` /
`cli`), so a value can always explain itself. `API_KEY`, `BASE_URL` and `MODEL`
are always per-provider and never participate in the global layer — a global
default for an API key would be meaningless.

`run.py --set` forwards overrides to child processes through the
`AGENTGATE_SET` environment variable, which is how a single CLI flag reaches a
bot running in its own subprocess.

---

## Process model

`run.py` is a **launcher, not a supervisor**. It parses the CLI into `BotSpec`
objects and spawns one child per bot (`python -u -m bots.<channel>`), prefixing
each child's output with `[label]` and forwarding `SIGINT`/`SIGTERM`. It does
**not** restart crashed children or daemonize. The `-u` flag is deliberate:
unbuffered child stdout so live lines appear immediately.

Each child is a fully independent process with its own runner instance, its own
`SessionStore`, and its own in-memory approval state.

---

## Reasoning mode

There is no endpoint that reports which reasoning parameter shape a model
accepts, so `core/reasoning/registry.py` carries a small static table and treats
anything absent as **unknown**. The state machine is honest rather than
optimistic:

| State | Meaning |
|---|---|
| `verified` | The model actually returned reasoning content. |
| `unverified` | The request succeeded but no reasoning came back. |
| `rejected` | The provider rejected the parameter (HTTP 400). |

Claude uses `thinking: adaptive` plus `effort`; open-model providers use
`reasoning_effort` (via `extra_body` when needed).

---

## Extension points

| Goal | How |
|---|---|
| Add a command | One `CommandSpec` entry in `core/commands.py`. Both dispatching bots pick it up automatically; `/help` is generated from the registry. |
| Add a platform | New module in `bots/`, own `SessionStore` base dir, call `use_runner()` at startup. |
| Add a model backend | Implement the `Runner` protocol in `runners/`, add a `ProviderSpec` if it is OpenAI-compatible. |
| Add a setting | Declare it in `core/providers.py` so it inherits the precedence cascade and origin tracking. |

---

## Security boundaries

- **Allowlists** (`ALLOWED_*`) fail **open** when empty — the bot is usable by
  anyone who can reach it. Set them for any non-private deployment.
- **Admin lists** (`ADMIN_*`) fail **closed** — no admins means admin commands
  are refused for everyone.
- **Blocked patterns** (`BLOCKED_PATTERNS`) apply to inbound messages and to
  every Bash command the agent generates, before the approval prompt.
- **Session data is gitignored** and lives only under `agent-sessions/`.
