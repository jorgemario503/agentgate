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
| `sessions.py` | Per-platform session registry on disk, workspace creation, directory tool, audit log, idle reaper. |
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
| `deepinfra.py` | DeepInfra (OpenAI-compatible) | tool-call loop, before execution |
| `fireworks.py` | Fireworks | tool-call loop, before execution |
| `local.py` | Ollama / MLX / llama.cpp | tool-call loop, before execution |

The Claude runner resumes from its own SDK session id; the three open-model
runners are **serverless** in the sense that they reload conversation history
from disk keyed by `thread_id` rather than holding it in a live client.

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
on the same thread id. The Claude runner also writes `settings.json` into each
workspace (`write_claude_settings`), which is where the CLI's own allow/deny
rules live.

`thread_id` is the platform's native identifier rendered as a string — a Slack
thread timestamp, a Telegram chat id, or a WhatsApp phone number. The runner,
the approval gate, and the audit log all key off that single string.

**Lifecycle:** the first message creates the session and workspace; subsequent
messages reuse it; the idle reaper expires it after `IDLE_TIMEOUT_SECONDS`
(default 8h). A `/stop` (or `stop session`) tears down the client but keeps the
session id so the agent can resume its transcript.

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
