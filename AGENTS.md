# AGENTS.md

Conventions for working on AgentGate. Written for both human contributors and
AI coding agents. For how the system fits together, read
[ARCHITECTURE.md](ARCHITECTURE.md); for setup and usage, see
[README.md](README.md).

---

## Before you change anything

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q          # expect 366 passing
```

Never work without a green baseline. If tests fail before you start, fix or
report that first — otherwise you cannot tell your breakage from pre-existing
breakage.

A dotenv file is required for bots to start but **not** for the test suite. If
you need one, copy `.env.example`. That file stays out of version control.

---

## Where code belongs

| Adding | Goes in | Must NOT go in |
|---|---|---|
| A command | `core/commands.py` (one `CommandSpec`) | a bot module |
| Transport and auth | `bots/<platform>.py` | `core/` |
| A model backend | `runners/<backend>.py` | a bot module |
| A shared setting | `core/providers.py` | hardcoded in a runner |
| Cross-platform logic | `core/` | duplicated per bot |
| Prompt text | `prompts/*.md` | a string constant in a runner |
| Prompt assembly | `core/prompts.py` | each runner concatenating its own |

Two dependency rules are non-negotiable:

- **`core/` never imports from `bots/`.** Bots depend on core, not the reverse.
- **Runners are reached through the `Runner` protocol**, not by importing a
  concrete runner from a bot's hot path.

If you find yourself copying a block from one bot to another, that block
belongs in `core/`.

---

## The command registry

A command is **one declaration**, and both dispatching bots (Slack, Telegram)
pick it up automatically. `/help` and usage text are generated from it, so
they cannot drift.

```python
CommandSpec(
    "autoapprove",
    _cmd_autoapprove,
    takes_args=True,
    admin_only=True,
    usage="autoapprove on|off",
    description="Toggle auto-approval",
)
```

Rules:

- Handlers live in `core/commands.py` and take a `CommandContext`, returning a
  reply **string**. No Telegram `Update`, no Slack `event`.
- Mark `admin_only=True` for anything that widens permissions.
- Provide `usage` whenever `takes_args=True` — bare invocation then prints it
  instead of failing opaquely.

### Command forms differ per platform — keep them different

| Platform | Correct form | Handled by |
|---|---|---|
| Telegram | `/autoapprove on` | `_normalise` strips the leading slash |
| Slack | `@bot autoapprove on` | `bots/slack.py` strips `<@Uxxx>` before dispatch |
| WhatsApp | none | does not dispatch commands |

Telegram needs **no bot mention**; Slack needs **no slash**. Do not "unify"
these by adding mention-stripping to the shared parser. That is how the
`5cd8e18` regression happened (see below).

---

## Testing

Tests live in `tests/`, are plain `pytest`, and use **fakes over mocks**:

```python
class FakeStore:
    base = "/tmp/agentgate-test"
    def append_audit_log(self, *args, **kwargs):
        pass
```

Guidance:

- **Test the input format the real caller sends.** The slash-command outage
  shipped because every registry test used slashless strings while Telegram
  sends `/cmd`. If a bot feeds a function, test that bot's actual format.
- **Prove a test bites.** Revert your fix, confirm the test fails, restore.
  A test that passes before and after proves nothing.
- Prefer async tests via `asyncio.run(...)` inside a sync test, matching
  existing style.
- Run the **full** suite, not just the file you touched, because shared state in
  `core/approvals.py` is module-level.

```bash
python -m pytest -q                                  # everything
python -m pytest tests/test_command_registry.py -q   # one file
```

---

## Things that have broken before

Real regressions from this repository's history. Check your change against these.

1. **Raw text vs. parsed text.** `5cd8e18` replaced per-command handlers with a
   unified dispatcher that read `message.text`, which still contains the slash.
   Every Telegram command silently stopped working. When you change how text
   reaches a parser, verify the format end to end.
2. **Dead branches justified by invented requirements.** A `@BotName` handler
   was added for a contract that did not exist. If a branch exists for a reason,
   that reason must be reproducible in a test.
3. **Same field, two names.** A `--port` flag set uvicorn's port while the
   webhook URL was derived from a different variable at import time, so inbound
   messages silently went nowhere. If a value is derived at import, mutate the
   input before the import, not the local copy.
4. **In-memory state assumed persistent.** Auto-approval and idle-bypass are
   deliberately in-memory. Do not "fix" them by writing to disk without
   discussing the security tradeoff.
5. **Insert-if-absent losing to a stale copy.** `connect()` inserted the system
   prompt only when the reloaded history had none — but the saved history always
   starts with the previous process's system message, so every freshly built
   prompt was discarded on resume. When a default is "fill it in if missing",
   ask what happens when a stale value is already there.
6. **A dependency the runner does not need.** `fireworks.py` imported its own
   SDK and raised in `__init__` without it, making `--fireworks` the only
   backend that failed on a fresh install. Both hosted runners ride on `openai`
   with a `base_url` override; `local` keeps raw `httpx` on purpose.
7. **Calls with no import, in paths tests never run.** `build_summary_prompt()`
   was called in all three open-model runners without being imported: it only
   runs during compaction, so a green suite hid a `NameError`. `pyflakes` found
   it, along with a WhatsApp handler using `runner` where the module-level
   `_runner` was meant. Run `python -m pyflakes core/ bots/ runners/` and do not
   assume coverage reaches long-tail paths.
8. **Tests that assert on source text instead of behaviour.** A check that a
   helper's *name* appeared in a bot passed even when the helper was sabotaged
   to always return `True`. Assert the decision, not the identifier.

---

## Prompts

Prompt text lives in `prompts/*.md`; assembly lives in `core/prompts.py`.
Never put prompt strings back in a runner.

| Surface | File | Fallback constant |
|---|---|---|
| base instructions | `prompts/base.md` | `FALLBACK_BASE` |
| hard rules | `prompts/hard_rules.md` | `FALLBACK_HARD_RULES` |
| resume block opening | `prompts/resume.md` | `FALLBACK_RESUME` |
| compaction summariser | `prompts/summary.md` | `FALLBACK_SUMMARY` |

Rules:

- **All four runners read these files**, Claude included. `hard_rules.md` is
  appended to Claude's preset prompt and folded into the open-model base
  prompt, so one edit reaches every backend.
- **Section headings are structural.** They live in `SECTION_HEADINGS` and are
  injected by the builder; a prompt file must not repeat one. Changing a
  heading is a contract change, not a wording change.
- **Keep the prefix stable.** Providers cache from the first character and
  discard the cached prefix on the first difference. Never put timestamps,
  turn counters, thread ids or paths in the prefix.
- **Falling back must never yield an empty prompt.** A missing or unreadable
  file returns its `FALLBACK_*` constant; an empty override loses to the
  fallback for the same reason.

---

## Security rules

- **Never log secrets, API keys, or user PII.** No printing dotenv values, and
  no echoing them in tests.
- **Never submit** code that disables an approval gate, blocked pattern, or
  allowlist check.
- **Blocked patterns apply to inbound text and generated Bash.** If you add a
  path where agent-generated commands bypass `check_blocked`, that is a security
  regression, not a convenience.
- **Allowlists fail open, admin lists fail closed.** Keep that asymmetry; it is
  intentional.
- **New outbound capability** (messaging others, writing outside the workspace)
  must go through `core/approvals.py`.

---

## Commit style

Conventional-ish, lowercase scope, imperative subject:

```
fix(whatsapp): make --port update the advertised webhook URL
feat(idle): /idle start|stop to bypass the idle reaper
refactor(commands): single command registry with unified dispatch
docs(readme): sync with current code
```

Common scopes: `run`, `commands`, `config`, `runners`, `agentdb`, `slack`,
`telegram`, `whatsapp`, `docs`.

Rules:

- One logical change per commit.
- The body explains why and what was wrong, not a restatement of the diff.
- If a commit fixes a bug, name the commit that introduced it when you know it.
- Run the full suite before committing.

---

## Environment gotchas

- **Dotenv loads at import time.** Import `core.config` before reading
  `os.environ` for anything it owns, or you will read unset variables.
- **`run.py --set` crosses a process boundary.** Overrides travel to child
  processes via `AGENTGATE_SET`; a child never sees the parent's CLI flags
  directly.
- **Each bot is its own process** with its own `SessionStore` and its own
  in-memory approval state. Two bots in one `run.py` invocation share nothing.
- **Session data is gitignored** and lives under `agent-sessions/`. Removing it
  loses conversation history, not code.

---

## Checklist before opening a PR

- [ ] Full suite green (`python -m pytest -q`)
- [ ] New behavior has a test that fails without the change
- [ ] No secrets, API keys, or PII in the diff
- [ ] No approval gate, allowlist, or blocked-pattern check weakened
- [ ] Docs updated if usage, config, or architecture changed
- [ ] Commit message explains why, not just what
