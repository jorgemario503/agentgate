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
| Ollama | `--ollama` | Ollama server running, `OLLAMA_MODEL` |
| MLX | `--mlx` | mlx-lm server running, `MLX_MODEL` |

---

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/your-org/agentgate
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
python run.py --telegram ollama -- --model qwen3:14b

# See all configured Telegram identities
python run.py --list-bots
```

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

### Slack

@mention the bot with the same keywords:
- `@bot stop session` · `@bot session status` · `@bot cancel`
- `@bot conflicts` · `@bot proposals` · `@bot propose <name> <content>`
- `@bot approve <id>` · `@bot reject <id>` · `@bot autoapprove on|off`

---

## Session lifecycle

1. **First message** — a session starts. A workspace directory is created under `agent-sessions/<platform>/<id>/`.
2. **Subsequent messages** — relayed to the same agent process.
3. **Idle timeout** — sessions expire after `IDLE_TIMEOUT_SECONDS` (default: 2 hours). The session ID is preserved so the agent can resume from its last Claude transcript.
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

## Allowlists

By default all users can interact with the bot. To restrict access:

```
# .env
ALLOWED_TELEGRAM_USER_IDS=123456789,987654321
ADMIN_TELEGRAM_USER_IDS=123456789

ALLOWED_SLACK_USER_IDS=U001,U002
ADMIN_SLACK_USER_IDS=U001
```

Admins can approve/reject AgentDB skill proposals and toggle auto-approval.

---

## AgentDB (optional governed memory)

AgentDB is disabled by default (`AGENTDB_ENABLED=false`). When enabled, the agent has persistent cross-session memory governed by `permissions.yaml`.

To enable:
```
# .env
AGENTDB_ENABLED=true
AGENTDB_PATH=./agentdb.db
AGENTDB_MEMORY_DIR=./agentdb-workspace
AGENTDB_PERMISSIONS_PATH=./permissions.yaml
```

Edit `permissions.yaml` to control which memory paths each agent identity can read and write. Any agent_id not listed there gets every write denied by the `default_policy: deny` rule.

When adding a named Telegram identity, add a matching entry:
```yaml
agents:
  telegram_assistant_personal: *bot_scope
```

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
├── bots/
│   ├── slack.py          # Slack @mention bot
│   ├── telegram.py       # Telegram private-chat bot
│   └── whatsapp.py       # WhatsApp linked-device bot (wppconnect-server)
│
├── runners/
│   ├── base.py           # Runner protocol + shared types
│   ├── claude.py         # Claude Agent SDK runner
│   ├── fireworks.py      # Fireworks (Kimi K2, etc.) runner
│   └── local.py          # OpenAI-compatible local server runner (Ollama, MLX, llama.cpp)
│
├── core/
│   ├── config.py         # all env var loading + validation
│   ├── sessions.py       # session store + idle reaper
│   ├── approvals.py      # async approval/answer gate
│   └── commands.py       # platform-agnostic command layer
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

## License

MIT
