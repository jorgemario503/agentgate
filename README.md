# AgentGate

Gateway between messaging platforms (Slack, Telegram, WhatsApp) and local AI coding agents.

Each bot runs as a private-chat conversation with a long-lived agent session. The agent has full tool access (Bash, file read/write, git, etc.) and asks for your approval before running anything potentially destructive. Every approval or denial is logged locally.

---

## Supported platforms

| Platform | Transport | Bot file |
|---|---|---|
| Telegram | Long polling | `bots/telegram.py` |
| Slack | Socket Mode | `bots/slack.py` |
| WhatsApp | wppconnect-server webhook | `bots/whatsapp.py` |

---

## Supported backends

| Backend | Flag | Requires |
|---|---|---|
| Claude (default) | `--claude` | `claude` CLI logged in |
| Fireworks | `--fireworks` | `pip install fireworks-ai` + `FIREWORKS_API_KEY` |
| DeepInfra | `--deepinfra` | `DEEPINFRA_API_KEY` + `DEEPINFRA_MODEL` (`openai` is in requirements) |
| Ollama | `--ollama` | Ollama server running, `OLLAMA_MODEL` |
| MLX | `--mlx` | mlx-lm server running, `MLX_MODEL` |

---

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/jorgemario503/agentgate
cd agentgate
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

For the Fireworks backend, also run:
```bash
pip install "fireworks-ai>=1.2.0"
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — add your bot token(s) and any other vars you need
```

Required vars by platform:

**Telegram:**
```
TELEGRAM_BOT_TOKEN=<your bot token from @BotFather>
```

**Slack:**
```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

The app itself can be created from [`docs/slack-manifest.yaml`](docs/slack-manifest.yaml)
(*Create New App → From an app manifest*). Its scopes are grouped into tiers —
core, read-context, outbound, and broader/riskier — so you can delete the tiers
you don't want before creating the app. Worth knowing:

- **Scopes grant capability, not silent action.** Outbound pings still pass
  through the approval gate, so the agent asks before messaging someone.

The bot does not implement Slack-specific tools for search or file upload: with
the scopes in place, the agent writes scripts using its existing Bash access,
which keeps agentgate from duplicating Slack's API surface.

`org_deploy_enabled` defaults to `false`. Enabling it without setting
`ALLOWED_SLACK_USER_IDS` lets anyone in the workspace drive the agent.

**WhatsApp:**
```
WPPCONNECT_SERVER_URL=http://localhost:8082
WPPCONNECT_WEBHOOK_URL=https://your-ngrok-url/webhook
```

See `.env.example` for the full list including optional vars.

### 3. Run

```bash
# Single bot
python run.py --telegram
python run.py --slack
python run.py --whatsapp

# Multiple bots at once
python run.py --slack --telegram

# With a non-default backend
python run.py --telegram fireworks
python run.py --telegram ollama
python run.py --telegram ollama surfer qwen3:14b
python run.py --slack deepinfra
python run.py --slack deepinfra deepinfra/Qwen/Qwen3-235B-A22B
python run.py --whatsapp ollama qwen3:14b

# See all configured Telegram identities
python run.py --list-bots
```

Positional grammar differs per channel, because only Telegram has bot identities:

| Command | Shape |
|---|---|
| `--slack` | `[runner] [model]` |
| `--whatsapp` | `[runner] [model]` |
| `--telegram` | `[runner] [bot_id] [model]` |

`runner` is one of `claude` (default), `deepinfra`, `fireworks`, `ollama`, `mlx`.

Or run a bot directly:
```bash
python -m bots.telegram --claude
python -m bots.telegram --ollama --model qwen3:14b --bot personal
python -m bots.slack --fireworks
```

---

## Multiple Telegram bots

Each bot identity needs its own token and its own session namespace.

In `.env`:
```
TELEGRAM_BOT_TOKEN_PERSONAL=<token from BotFather>
TELEGRAM_BOT_TOKEN_WORK=<different token>
ALLOWED_TELEGRAM_USER_IDS_PERSONAL=123456789
ALLOWED_TELEGRAM_USER_IDS_WORK=987654321
```

Launch:
```bash
python run.py --telegram claude personal --telegram claude work
```

Each identity stores sessions under `agent-sessions/telegram/<name>/`, ensuring
conversation history is fully isolated.

> **Important:** Two bots sharing the same token share the same Telegram identity
> (same chat IDs, same polling session). Always use distinct tokens for distinct
> bot identities.

---

## Bot commands

### Telegram

| Command | Description |
|---|---|
| `/stop` | End the current session |
| `/status` | Show session status |
| `/cancel` | Interrupt the in-flight agent turn |
| `/conflicts` | List pending AgentDB memory conflicts |
| `/proposals` | List pending skill proposals |
| `/propose <name> <content>` | Draft a new skill proposal |
| `/approve <proposal_id>` | Approve a proposal (admins only) |
| `/reject <proposal_id> [reason]` | Reject a proposal (admins only) |
| `/autoapprove on\|off` | Toggle auto-approval for this chat (admins only) |
| `/idle start\|stop` | Disable/enable the idle timeout for this chat |
| `/help` | List every command |
| `/reasoning off\|low\|medium\|high` | Set reasoning effort for this chat |
| `/reasoning show` | Show the last captured reasoning trace |
| `/reasoning` | Show current level, backend, model and verification state |

### Slack

@mention the bot with the same keywords:
- `@bot stop session` · `@bot session status` · `@bot cancel`
- `@bot conflicts` · `@bot proposals` · `@bot propose <name> <content>`
- `@bot approve <id>` · `@bot reject <id>` · `@bot autoapprove on|off`
- `@bot reasoning off|low|medium|high` · `@bot reasoning show` · `@bot reasoning`

> **The two platforms use different command forms — this is intentional.**
> Telegram uses **slash commands with no mention** (`/autoapprove on`); Slack
> uses **a bot mention with no slash** (`@bot autoapprove on`). Slack strips the
> mention before dispatching, so both reach the same registry. WhatsApp does not
> dispatch commands at all — it relays every message to the agent.

---

## Session lifecycle

1. **First message** — a session starts. A workspace directory is created under `agent-sessions/<platform>/<id>/`.
2. **Subsequent messages** — relayed to the same agent process.
3. **Idle timeout** — sessions expire after `IDLE_TIMEOUT_SECONDS` (default: 8 hours). The session ID is preserved so the agent can resume from its last Claude transcript.
4. **`/stop`** — tears down the agent client immediately; next message starts fresh.

---

## Tool approvals

When the agent wants to run a command or write a file, it asks for approval:

```
Run command:
`git push origin main`
```

Reply `yes` (allow once) or `no` (deny). Set `/autoapprove on` to skip approvals for the current session — blocked patterns and deny rules in `settings.json` still apply.

**Auto-approval is reset on bot restart.** It is never persisted to disk.

---

## Configuration and overrides

Runner settings resolve through four layers (lowest to highest):

1. built-in default
2. **global** override, e.g. `MAX_TOOL_ITERATIONS=60`
3. **per-provider** override, e.g. `DEEPINFRA_MAX_TOOL_ITERATIONS=80`
4. **CLI** override, e.g. `--set MAX_TOOL_ITERATIONS=99` (this run only)

Globally overridable: `MAX_TOOL_ITERATIONS`, `TOOL_OUTPUT_CAP`, `BASH_TIMEOUT_SECONDS`, `TEMPERATURE`, `MAX_TOKENS`, `CONTEXT_WINDOW`, `SUMMARY_MODEL`, `SUMMARY_MAX_TOKENS`, `SUMMARY_TEMPERATURE`, `SUMMARY_CHUNK_CHARS`.

Always per-provider (never global): `API_KEY`, `BASE_URL`, `MODEL`.

```bash
# Global default, one line in .env
MAX_TOOL_ITERATIONS=40

# This provider only
DEEPINFRA_MAX_TOOL_ITERATIONS=60

# One-off experiment, no file edits (highest precedence)
python run.py --telegram ollama surfer qwen3:14b --set MAX_TOOL_ITERATIONS=99 --set TEMPERATURE=0.9
```

`--set KEY=VALUE` may be repeated and applies to every bot in that run. Each
resolved setting also records where it came from (`default` / `global` /
`provider` / `cli`), which the CLI can display.

---

## Idle timeout

Sessions expire after `IDLE_TIMEOUT_SECONDS` (default 8h) of inactivity. To keep
one alive, opt the chat out:

```
/idle start   # the reaper will skip this chat
/idle stop    # restore normal expiry
/idle         # show current state
```

Like `/autoapprove`, the flag is **in memory only** and resets when the bot
restarts — but the conversation itself survives: serverless runners reload
history from disk keyed by thread id, and the Claude SDK resumes from its own
session id.

## Commands

Every command is declared once in `core/commands.py` (`COMMANDS`) and both bots
dispatch through the same entry point, so the command set, admin rules and
usage text cannot drift between platforms:

```python
CommandSpec("reasoning", _cmd_reasoning, takes_args=True,
            usage="reasoning off|low|medium|high|show",
            description="Set reasoning effort")
```

Adding a command is one entry — no per-bot wiring. Telegram and Slack accept the
same names; `stop session` and `session status` remain as aliases for the older
Slack spelling, and `/help` is generated from the registry.

## Reasoning mode

`/reasoning low|medium|high` turns on extended reasoning for the current chat;
`/reasoning` shows the current state and `/reasoning show` prints the last trace.
Applies to every backend, with a different mechanism per provider:

| Backend | Mechanism |
|---|---|
| DeepInfra / Fireworks / local | `reasoning_effort` (`extra_body` for providers that need it) |
| Claude | `thinking: adaptive` (`display: summarized`) + `effort` |

**Verification is honest, not assumed.** Each chat tracks one of three states:

| State | Meaning |
|---|---|
| `verified` | The model actually returned reasoning content / thinking text. |
| `unverified` | The request succeeded but no reasoning came back — shown as **"⚠️ Reasoning mode not verified"**. The model may be ignoring the parameter. |
| `rejected` | The provider rejected the parameter (HTTP 400). Reasoning is turned off for that chat. |

A small built-in registry (`core/reasoning/registry.py`) maps provider/model to the
parameter shape. **If your model is not in the registry** the bot says so up front
and still lets you try it — the result is then labelled `unverified` until the
model proves otherwise by returning reasoning content.

> Claude note: Opus 4.7+ returns thinking as signature-only by default, so we ask
> for `display: "summarized"` when verifying. Missing thinking text on Claude is
> reported as `unverified`, never as a rejection.

---

## Allowlists

By default all users can interact with the bot. To restrict access:

```
# .env
ALLOWED_TELEGRAM_USER_IDS=123456789,987654321
ADMIN_TELEGRAM_USER_IDS=123456789

ALLOWED_SLACK_USER_IDS=U001,U002
ADMIN_SLACK_USER_IDS=U001

ALLOWED_WHATSAPP_USER_IDS=5511999999999
ADMIN_WHATSAPP_USER_IDS=5511999999999
```

Telegram identities can also use per-bot suffixes (`ALLOWED_TELEGRAM_USER_IDS_WORK`),
which fall back to the unsuffixed vars when absent — see
[Multiple Telegram bots](#multiple-telegram-bots).

Admins can approve/reject AgentDB skill proposals and toggle auto-approval.

---

## AgentDB (optional governed memory)

AgentDB is disabled by default. When enabled, the agent has persistent cross-session memory governed by a `permissions.yaml` file.

**1. Install the package**

```bash
pip install agentdatabase
# optional: semantic search and conflict detection
pip install 'agentdatabase[local-embed]'
```

**2. Create `permissions.yaml`**

This file governs what the agent can read and write in memory. A minimal starting point:

```yaml
agent_id: slack_assistant   # must match the bot's agent_id (see bots/*.py)
allowed_read_scopes: ["*"]
allowed_write_scopes: ["*"]
```

See the [agentdatabase docs](https://github.com/newtonx/agentdb) for the full permissions schema and scope configuration.

**3. Set env vars**

```bash
# .env
AGENTDB_ENABLED=true
AGENTDB_MEMORY_DIR=agentdb-workspace      # DB and config live here
AGENTDB_PERMISSIONS_PATH=permissions.yaml
```

On startup the bot prints `AgentDB initialized at agentdb-workspace`. If the package is missing or `permissions.yaml` doesn't exist, it logs a warning and continues without memory.

**Embeddings (optional)**

To enable semantic retrieval, edit `<AGENTDB_MEMORY_DIR>/config/memory.yaml` (created on first run):

```yaml
embeddings:
  enabled: true
```

Requires `pip install 'agentdatabase[local-embed]'`. See the agentdatabase package for model configuration and further options.

---

## Security

- **Blocked patterns** — `BLOCKED_PATTERNS` in `core/config.py` (and settable via `.env`) are applied to every incoming message and every Bash command the agent generates. Matching commands are refused without asking.
- **settings.json** — the Claude CLI's own `allow`/`deny` rules are enforced by the Claude runner's permission callback.
- **Audit log** — every tool approval/denial is written to `<session_dir>/audit_log.jsonl`.
- **PII** — never log, commit, or hard-code secrets or user PII. Session data lives only in `agent-sessions/` (gitignored).

---

## Directory structure

```
agentgate/
├── .env.example          # all env vars documented
├── .gitignore
├── README.md
├── requirements.txt
├── run.py                # launch one or more bots as subprocesses
├── permissions.yaml      # AgentDB write permissions (generic examples)
│
├── docs/
│   └── slack-manifest.yaml   # Slack app manifest — tiered, commented scopes
│
├── bots/
│   ├── slack.py          # Slack @mention bot
│   ├── telegram.py       # Telegram private-chat bot
│   └── whatsapp.py       # WhatsApp linked-device bot (wppconnect-server)
│
├── runners/
│   ├── base.py           # Runner protocol + shared types
│   ├── claude.py         # Claude Agent SDK runner
│   ├── claude_reasoning.py  # Claude thinking/effort mapping
│   ├── deepinfra.py      # DeepInfra runner
│   ├── fireworks.py      # Fireworks (Kimi K2, etc.) runner
│   └── local.py          # OpenAI-compatible local server runner (Ollama, MLX, llama.cpp)
│
├── core/
│   ├── config.py         # all env var loading + validation
│   ├── providers.py      # declarative provider specs + config precedence
│   ├── sessions.py       # session store + idle reaper
│   ├── approvals.py      # async approval/answer gate
│   ├── agentdb.py        # AgentDB integration surface
│   ├── commands.py       # platform-agnostic command layer
│   └── reasoning/        # reasoning-parameter registry + resolution
│       ├── registry.py
│       └── resolve.py
│
└── tests/
```

---

## WhatsApp prerequisites

1. Clone and start [wppconnect-server](https://github.com/wppconnect-team/wppconnect-server):
   ```bash
   git clone https://github.com/wppconnect-team/wppconnect-server
   cd wppconnect-server && npm install && npm start
   ```
2. Scan the QR code to link your device.
3. Expose the webhook with ngrok: `ngrok http 8081`
4. Set `WPPCONNECT_WEBHOOK_URL=https://<ngrok-url>/webhook` in `.env`.

---

## MLX server as a launchd service (macOS)

To run `mlx_lm.server` automatically at login, save the following to
`~/Library/LaunchAgents/com.agentgate.mlx-server.plist`, then:

```bash
launchctl load ~/Library/LaunchAgents/com.agentgate.mlx-server.plist
```

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.agentgate.mlx-server</string>
    <key>ProgramArguments</key>
    <array>
        <!-- absolute path to python in the venv where mlx-lm is installed -->
        <string>/path/to/venv/bin/python</string>
        <string>-m</string>
        <string>mlx_lm.server</string>
        <!-- same machine: 127.0.0.1 | cross-machine: 0.0.0.0 -->
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8082</string>
        <string>--model</string>
        <string>mlx-community/Qwen3-14B-4bit</string>
        <!-- omit --api-key for local-only use; required for cross-machine -->
        <string>--api-key</string>
        <string>YOUR_SECRET_KEY</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/mlx-server.log</string>
    <key>StandardErrorPath</key><string>/tmp/mlx-server.log</string>
</dict>
</plist>
```

Generate a secret key: `openssl rand -hex 32`

For cross-machine access, change `--host 127.0.0.1` → `--host 0.0.0.0` and
use [Tailscale](https://tailscale.com/) so only your own devices can reach
the port. See `.env.example` for the matching `MLX_BASE_URL` / `MLX_API_KEY`
values.

---

## License

AGPL-3.0. Any modification — including running this as a hosted network
service — must be released under the same terms. See [LICENSE](LICENSE).
