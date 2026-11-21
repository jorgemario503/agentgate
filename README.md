# AgentGate

Gateway between messaging platforms (Slack, Telegram, WhatsApp) and local AI
coding agents. Send a message, get a coding agent with real tool access — and
approve anything destructive before it runs.

- **Architecture** — how the layers fit together: [ARCHITECTURE.md](ARCHITECTURE.md)
- **Contributing** — conventions and known traps: [AGENTS.md](AGENTS.md)

---

## How it works

You message a bot. The bot turns that into a prompt, a runner executes it
against a model backend, and every potentially destructive tool call is gated on
your approval. Approvals and denials are audited to disk.

Sessions are isolated per platform and per thread, stored under
`agent-sessions/` (gitignored).

---

## Supported platforms

| Platform | Transport | Bot file |
|---|---|---|
| Telegram | Long polling | `bots/telegram.py` |
| Slack | Socket Mode | `bots/slack.py` |
| WhatsApp | wppconnect-server webhook | `bots/whatsapp.py` |

## Supported backends

| Backend | Flag | Requires |
|---|---|---|
| Claude (default) | `--claude` | `claude` CLI logged in |
| Fireworks | `--fireworks` | `FIREWORKS_API_KEY` + `FIREWORKS_MODEL` |
| DeepInfra | `--deepinfra` | `DEEPINFRA_API_KEY` + `DEEPINFRA_MODEL` |
| Ollama | `--ollama` | Ollama server running, `OLLAMA_MODEL` |
| MLX | `--mlx` | mlx-lm server running, `MLX_MODEL` |

---

## Quick start

```bash
git clone https://github.com/jorgemario503/agentgate
cd agentgate
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in the token(s) for your platform
```

Minimum required vars by platform:

| Platform | Variables |
|---|---|
| Telegram | `TELEGRAM_BOT_TOKEN` |
| Slack | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` |
| WhatsApp | `WPPCONNECT_SERVER_URL`, `WPPCONNECT_WEBHOOK_URL` |

See [`.env.example`](.env.example) for the full list including optional vars.

### Run

```bash
python run.py --telegram      # one bot
python run.py --slack
python run.py --whatsapp

python run.py --slack --telegram                        # several at once
python run.py --slack deepinfra                         # non-default backend
python run.py --telegram ollama surfer qwen3:14b        # runner, bot_id, model
python run.py --list-bots                               # configured identities
```

`run.py` spawns each bot as its own subprocess. It is a launcher, not a
supervisor — no restart-on-crash.

Positional arguments differ per channel, because only Telegram has bot identities:

| Channel | Shape |
|---|---|
| `--slack` | `[runner] [model]` |
| `--whatsapp` | `[runner] [model]` |
| `--telegram` | `[runner] [bot_id] [model]` |

`runner` is one of `claude` (default), `deepinfra`, `fireworks`, `ollama`, `mlx`.

Bots can also be started directly:

```bash
python -m bots.telegram --claude
python -m bots.telegram --ollama --model qwen3:14b --bot personal
python -m bots.slack --deepinfra
```

---

## Platform setup

### Slack

Create the app from [`docs/slack-manifest.yaml`](docs/slack-manifest.yaml)
(*Create New App → From an app manifest*). Scopes are grouped into tiers — core,
read-context, outbound, broader/riskier — so you can delete the tiers you don't
want before creating the app.

Worth knowing:

- **Scopes grant capability, not silent action.** Outbound pings still pass
  through the approval gate, so the agent asks before messaging someone.
- The bot ships no Slack-specific search or upload tools. With the scopes in
  place the agent writes scripts using its Bash access, which keeps AgentGate
  from duplicating Slack's API surface.
- `org_deploy_enabled` defaults to `false`. Enabling it without
  `ALLOWED_SLACK_USER_IDS` lets anyone in the workspace drive the agent.

Slack commands take the form `@bot <command>` — see [Commands](#commands).

### Telegram

Create a bot with [@BotFather](https://t.me/BotFather) and set
`TELEGRAM_BOT_TOKEN`. Telegram uses **slash commands with no mention** — see
[Commands](#commands).

**Multiple identities.** Each bot identity needs its own token and its own
session namespace:

```
TELEGRAM_BOT_TOKEN_PERSONAL=<token>
TELEGRAM_BOT_TOKEN_WORK=<different token>
ALLOWED_TELEGRAM_USER_IDS_PERSONAL=123456789
ALLOWED_TELEGRAM_USER_IDS_WORK=987654321
```

```bash
python run.py --telegram claude personal --telegram claude work
```

Sessions land under `agent-sessions/telegram/<name>/`.

> **Important:** Two bots sharing a token share the same Telegram identity (same
> chat IDs, same polling session). Always use distinct tokens.

### WhatsApp

1. Start [wppconnect-server](https://github.com/wppconnect-team/wppconnect-server):
   ```bash
   git clone https://github.com/wppconnect-team/wppconnect-server
   cd wppconnect-server && npm install && npm start
   ```
2. Scan the QR code to link your device.
3. Expose the webhook: `ngrok http 8081`
4. Set `WPPCONNECT_WEBHOOK_URL=https://<ngrok-url>/webhook`.

WhatsApp does not dispatch commands — every message is relayed to the agent.

---

## Commands

Declared once in `core/commands.py`; `/help` is generated from the registry.

| Command | Description |
|---|---|
| `/stop` | End the current session |
| `/status` | Show session status |
| `/cancel` | Interrupt the in-flight agent turn |
| `/conflicts` | List pending AgentDB memory conflicts |
| `/autoapprove on\|off` | Toggle auto-approval for this chat (admins only) |
| `/idle start\|stop` | `stop` skips the idle reaper; `start` restores normal expiry |
| `/reasoning off\|low\|medium\|high` | Set reasoning effort for this chat |
| `/reasoning show` | Show the last captured reasoning trace |
| `/reasoning` | Show current level, backend, model and verification state |
| `/help` | List every command |

**The two platforms use different forms — this is intentional:**

| Platform | Form |
|---|---|
| Telegram | `/autoapprove on` — slash, **no mention** |
| Slack | `@bot autoapprove on` — mention, **no slash** |
| WhatsApp | *(no commands)* |

Slack strips the mention before dispatch, so both reach the same registry.

---

## Approvals

When the agent wants to run a command or write a file, it asks first:

```
Run command:
`git push origin main`
```

Reply `yes` to allow once or `no` to deny. Unanswered prompts auto-deny after
`APPROVAL_TIMEOUT_SECONDS` (default 4h).

`/autoapprove on` skips the question for that chat — blocked patterns and
`settings.json` deny rules still apply. The flag is **in memory only** and resets
on bot restart.

---

## Configuration

Runner settings resolve through four layers, lowest to highest:

1. built-in default
2. **global** — `MAX_TOOL_ITERATIONS=60`
3. **per-provider** — `DEEPINFRA_MAX_TOOL_ITERATIONS=80`
4. **CLI** — `--set MAX_TOOL_ITERATIONS=99` (this run only)

Globally overridable: `MAX_TOOL_ITERATIONS`, `TOOL_OUTPUT_CAP`,
`BASH_TIMEOUT_SECONDS`, `TEMPERATURE`, `MAX_TOKENS`, `CONTEXT_WINDOW`,
`SUMMARY_MODEL`, `SUMMARY_MAX_TOKENS`, `SUMMARY_TEMPERATURE`,
`SUMMARY_CHUNK_CHARS`.

Always per-provider: `API_KEY`, `BASE_URL`, `MODEL`.

```bash
python run.py --telegram ollama surfer qwen3:14b \
  --set MAX_TOOL_ITERATIONS=99 --set TEMPERATURE=0.9
```

`--set KEY=VALUE` may be repeated and applies to every bot in that run. See
[ARCHITECTURE.md](ARCHITECTURE.md#configuration-precedence) for how precedence
and origin tracking work.

### Custom system prompts

Prompt text can be overridden without editing source. Drop a file in
`prompts/` — or point `AGENTGATE_PROMPTS_DIR` at your own directory:

| File | Replaces |
|---|---|
| `base.md` | the workflow prompt (tool list, inspection preferences) |
| `hard_rules.md` | the security instructions |
| `resume.md` | the opening lines of the resume block |
| `summary.md` | the compaction summariser's instructions |

All four files ship and are active, so the usual route is to edit them in
place. Use `AGENTGATE_PROMPTS_DIR` only to keep your copies outside the repo:

```bash
export AGENTGATE_PROMPTS_DIR=/path/to/my-prompts
```

A missing, empty, or unreadable file falls back to the built-in text — an
override can never blank the prompt. All four files ship and are active.

**Every runner reads these files**, including Claude: `hard_rules.md` is
appended to Claude's preset prompt as well as to the non-Claude base prompt,
so one edit reaches all backends.

Each file documents its own conventions at the top. The short version: leading
`#` comments are stripped and never reach the model; the section heading is
added by AgentGate, so do not repeat it in the file; and anything from the
first non-comment line onward *is* sent.

### Section headings

Sections are delimited by fixed markdown headings, defined once in
`core/prompts.py` (`SECTION_HEADINGS`) and injected in stable-to-volatile
order:

| Section | Heading |
|---|---|
| base instructions | `## Base instructions` |
| hard rules | `## Hard rules` |
| resume block | `## Resuming an existing session` |
| AgentDB memory | `## Memory from previous sessions` |

The headings are structural, not content: a customised prompt file replaces a
section's body, never its heading, so the structure cannot be broken by an
edit. Injection lives in one module — the runners only pass their state in.

### A note on `hard_rules.md`

Shipping it changes the rule text the model sees, so edit it deliberately. It
is a *prompt*, not an enforcement layer: the real controls are
`core/approvals.py`, `permissions.yaml` and the blocked-pattern list, which
stop calls regardless of what the prompt says. The file states this itself so
nobody mistakes editing it for configuring the guardrails.

### Prompt cache keys

The deepinfra and fireworks runners pin each conversation to its own provider
cache lineage, so concurrent sessions on the same model and API key do not
share a cache entry:

| Runner | Mechanism |
|---|---|
| deepinfra | `prompt_cache_key` in the request body |
| fireworks | `x-session-affinity` header |
| ollama / mlx | **opt-in** via `AGENTGATE_PROMPT_CACHE_KEY=1` |

The key is the thread id. Claude is excluded: its SDK manages session state
itself. The local runners are opt-in because they POST to arbitrary
OpenAI-compatible servers and a strict one may reject an unknown field; if a
request is rejected for that reason it is retried without the key, so a cache
optimisation never costs you a turn.

**Keep the prompt prefix stable.** Providers cache a prompt by matching from
the first character and discard the cached prefix on the first difference, so
keep the most stable text at the top and never put timestamps, turn counters,
thread ids or workspace paths in the prefix. The shipped files carry a header
explaining this; leading `#` comment lines are stripped and never reach the
model.

---

## Allowlists

By default anyone who can reach the bot can use it. To restrict access:

```
# .env
ALLOWED_TELEGRAM_USER_IDS=123456789,987654321
ADMIN_TELEGRAM_USER_IDS=123456789

ALLOWED_SLACK_USER_IDS=U001,U002
ADMIN_SLACK_USER_IDS=U001

ALLOWED_WHATSAPP_USER_IDS=5511999999999
ADMIN_WHATSAPP_USER_IDS=5511999999999
```

Telegram identities can also use per-bot suffixes
(`ALLOWED_TELEGRAM_USER_IDS_WORK`), which fall back to the unsuffixed vars when
absent.

Admins can toggle auto-approval.
Allowlists fail **open** when empty; admin lists fail **closed**.

---

## Idle timeout

Sessions expire after `IDLE_TIMEOUT_SECONDS` (default 8h) of inactivity.

```
/idle start   # restore normal expiry
/idle stop    # the reaper will skip this chat
/idle         # show current state
```

Like `/autoapprove`, this is in-memory only. The conversation itself survives:
the Claude SDK resumes from its own session id, and open-model runners reload
history from disk by thread id.

---

## AgentDB (governed memory — enabled by default)

AgentDB ships with the app (see `requirements.txt`) and is **on by default**:
the agent gains persistent cross-session memory, governed by
[`permissions.yaml`](permissions.yaml).

Configuration via `.env` (defaults shown):

```
AGENTDB_ENABLED=true
AGENTDB_MEMORY_DIR=./agentdb-workspace
AGENTDB_PERMISSIONS_PATH=./permissions.yaml
```

Startup **fails loudly** with an actionable message if the package, the
permissions file, or the memory dir is missing. To run deliberately without
governed memory, set `AGENTDB_ENABLED=false` (governed memory and the
`/conflicts` command are then unavailable).

For semantic retrieval, set `embeddings: {enabled: true}` in
`<AGENTDB_MEMORY_DIR>/config/memory.yaml` and install
`pip install "agentdatabase[local-embed]"`.

---

## Skills (open-model, opt-in — OFF by default)

Open-model runners (`local`, `fireworks`, `deepinfra`) can load **Agent Skills**:
portable `SKILL.md` files that give the agent task-specific procedures on demand.
The agent sees a compact manifest of available skills and reads a skill's full
instructions only when relevant. **Claude and AgentDB are not involved** — this is
a separate, opt-in capability.

Skills are **off by default**. When disabled, requests are byte-for-byte
unchanged.

### Quick start

1. Enable the feature in `.env`:

   ```env
   AGENTGATE_SKILLS_ENABLED=true
   ```

2. Put one folder per skill under the configured catalog (`AGENTGATE_SKILLS_DIR`,
   default `skills/`), each with a `SKILL.md`:

   ```
   skills/
     create-ticket/
       SKILL.md
   ```

   ```markdown
   ---
   name: create-ticket
   description: Create internal tickets using our required format.
   ---

   # Ticket format
   - Title prefix: [PAY] for payments, [OPS] for ops.
   - Sections: Summary, Impact, Steps to Reproduce, Owner.
   - If the owner is unknown, write "Owner: UNASSIGNED" — do not invent a name.
   ```

   The `name` must be lowercase, hyphen-separated, and match the folder name.

### Recommended configuration (Mode A — model-selected)

When enabling skills, use the default model-selected mode:

```env
AGENTGATE_SKILLS_ENABLED=true
AGENTGATE_SKILLS_DIR=skills
AGENTGATE_SKILLS_MANIFEST_TOKENS=600
AGENTGATE_SKILLS_SELECTION_MODE=model
```

The model decides when a skill is relevant and calls `skill_read`; "no skill" is a
valid choice. In controlled evaluation this roughly doubled task success on
specialized workflows while cutting cost per successful task. (Results are
workload- and model-specific.)

### Explicit invocation (Mode C — `//skill-name`)

Users can force a skill for one message by prefixing it with `//<skill-name>`
(works automatically whenever skills are enabled — no extra flag):

```text
//create-ticket Create a ticket for the payment retry issue.
//review-pr Review the authentication changes in PR 124.
```

The skill's instructions are loaded before the task runs. Explicit invocation
**does not bypass tool approvals** — normal permission checks still apply.
Unknown skill names produce a clear error (no silent substitution).

### Experimental: keyword recommendations (Mode B)

An optional mode can nudge the model toward a skill when the request matches
skill-declared trigger phrases:

```env
AGENTGATE_SKILLS_SELECTION_MODE=recommend
```

Triggers live in the `SKILL.md` frontmatter:

```yaml
metadata:
  agentgate:
    triggers:
      - create a ticket
      - usual ticket format
```

Mode B is **experimental and not recommended by default**: evaluation showed no
material benefit over Mode A, and it adds a false-positive surface (it may suggest
a skill that isn't needed). Recommendations are lower-priority hints only — they
never force activation or include the skill body.

### Security model

- Skills are **operator-controlled files** you place in the catalog.
- Skill instructions are **lower-trust reference content**, delivered as clearly
  labeled user-role messages — never system instructions. They cannot override the
  hard rules or grant additional tool permissions (`allowed-tools` in a skill is
  informational only).
- Loads are **path-contained**: `SKILL.md` must resolve inside the catalog (no
  symlink/traversal escapes), and a skill body whose fingerprint changed since it
  was read is not silently restored.
- These safeguards reduce risk but **do not establish immunity to all prompt
  injection** — only add skills you trust as you would any code.

### Multi-turn behavior

- Skill instructions are not repeated while they remain in context.
- If a skill's body is trimmed/compacted **within the active task turn**, it is
  restored once.
- A **subsequent user turn does not automatically restore** an earlier skill body;
  the model can `skill_read` again if still relevant. This keeps context lean.

### Caching

The skills manifest sits in the **stable prompt prefix**, which providers cache
across turns — the same mechanism as the rest of the system prompt. That provider
prefix cache is **not** agent memory: it does not make the model "remember" a
skill across sessions. Skill state is session-local and reset per session.

Configuration (defaults shown):

```
AGENTGATE_SKILLS_ENABLED=false
AGENTGATE_SKILLS_DIR=skills
AGENTGATE_SKILLS_MANIFEST_TOKENS=600
AGENTGATE_SKILLS_SELECTION_MODE=model
```

Authoring, activation modes, security model, multi-turn behavior, testing and
the evaluation harness: [docs/skills.md](docs/skills.md).

---

## Context efficiency (tool-result compression — opt-in, OFF by default)

Open-model runners (`local`, `fireworks`, `deepinfra`) re-send the full
conversation each turn, so bulky tool output (long logs, large JSON, minified
assets) costs tokens every turn. AgentGate can **compress a tool result before
the model sees it**, keep the exact original on disk, and let the model retrieve
it on demand through the `tool_result_read` tool.

Compression is **targeted and conservative**: repetitive log lines are collapsed
(keeping every error/fatal/traceback/exit line and its context), large JSON
becomes an annotated summary, and dense minified/base64 lines keep head+tail.
Source code, diffs and tracebacks are treated conservatively, and **system,
user and assistant messages are never touched**. It is **not redaction** —
secrets are not scrubbed.

```env
AGENTGATE_TOOL_COMPRESSION_ENABLED=true    # master switch (default false)
AGENTGATE_TOOL_COMPRESSION_MIN_CHARS=4000  # only results >= this are considered
AGENTGATE_TOOL_ARTIFACT_DIR=               # blank -> <session-base>/_artifacts
```

Two related opt-in features sit alongside it: **cross-turn deduplication**
(`AGENTGATE_CROSS_TURN_DEDUP_ENABLED=true` — replace a verbatim repeat of an
earlier tool result with a pointer) and a **repo map**
(`AGENTGATE_REPO_MAP_ENABLED=true` — inject a small deterministic overview of the
workspace at session start).

With all flags off (the default), outbound requests are byte-for-byte unchanged.

Safety properties: fail-open (a compression error returns the original), the
original is always retrievable, retrieval is authorized per session **and**
workspace, and a tool message is transformed once so the provider cache prefix
stays stable.

Full guide — transforms, worked example, retrieval, dedup, repo map, tests and
measurement: [docs/context-efficiency.md](docs/context-efficiency.md). For the
opt-in live model probes and how to run them safely, see
[docs/live-tests.md](docs/live-tests.md).

---

## Security

- **Blocked patterns** — `BLOCKED_PATTERNS` in `core/config.py` (settable via
  `.env`) are applied to every incoming message and every Bash command the agent
  generates. Matches are refused without asking.
- **settings.json** — the Claude CLI's own allow/deny rules are enforced by the
  Claude runner's permission callback.
- **Audit log** — every approval and denial is written to
  `<session_dir>/audit_log.jsonl`.
- **PII** — never log, commit, or hard-code secrets or user PII. Session data
  lives only under `agent-sessions/`.

---

## Directory structure

```
agentgate/
├── run.py                    # launch one or more bots as subprocesses
├── .env.example              # all env vars documented
├── permissions.yaml          # AgentDB write permissions
├── docs/slack-manifest.yaml  # Slack app manifest (tiered scopes)
│
├── prompts/                  # editable prompt text: base.md, hard_rules.md,
│                             #   resume.md, summary.md (all runners read these)
├── bots/                     # transport: slack.py, telegram.py, whatsapp.py
├── runners/                  # execution: base.py, claude.py, deepinfra.py,
│                             #            fireworks.py, local.py
├── core/                     # shared: config, providers, sessions, prompts,
│                             #   history, cache_key, approvals, commands,
│                             #   agentdb, skills/, reasoning/
├── skills/                   # open-model Agent Skills catalog (opt-in; examples)
└── tests/
```

Full breakdown: [ARCHITECTURE.md](ARCHITECTURE.md#layers).

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

Generate a key with `openssl rand -hex 32`. For cross-machine access, change
`--host 127.0.0.1` to `--host 0.0.0.0` and use [Tailscale](https://tailscale.com/)
so only your devices can reach the port.

---

## License

AGPL-3.0. Any modification — including running this as a hosted network
service — must be released under the same terms. See [LICENSE](LICENSE).
