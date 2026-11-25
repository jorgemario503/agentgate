# Changelog

All notable changes to AgentGate are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/); versions follow the
project's release practice.

## [Unreleased]

### Removed
- **`skills/example-summarize/`** — a sample template that nobody invoked. It had
  no `metadata.agentgate.triggers`, so Mode B could never surface it, and it
  listed as an available skill in every session's manifest. Kept in
  `_archive/skills-legacy/` rather than deleted. The three role skills remain.

### Added
- **`core/injection.py` — turn context has a declared owner and a declared
  scope.** Three copies of the same assembly lived in the open-model runners and
  had drifted from the documented behaviour. They now emit `Injection` values
  carrying a `source` (`agentgate` or `agentdb`), an `asset`, and a scope from
  `SESSION_START` / `SESSION_RESUME` / `SESSION_BOUNDARY` / `EVERY_TURN`, and one
  `build_turn_injections()` assembles them. `ASSET_SCOPES` is the reviewable
  table of what is injected when, and constructing an asset with a scope that
  contradicts it raises rather than letting the table become a lie.

### Fixed
- **A misconfigured skills setup is now visible at startup.** Skills fail quietly
  by design — an invalid `SKILL.md` is excluded, an unrecognised
  `AGENTGATE_SKILLS_SELECTION_MODE` falls back to `model`, a missing directory
  yields an empty catalog — which is right for a request path but leaves the
  operator unable to see that their configuration did not take effect. All three
  bots now print the resolved catalog and mode once at startup, plus a warning
  when the selection mode was not recognised and any per-skill discovery
  diagnostic, in the same shape as `ADDITIONAL_DIRS` reporting. A typo such as
  `AGENTGATE_SKILLS_SELECTION_MODE=recomend` previously ran Mode A with no
  indication at all.
- **The repo map is injected once per session, as documented — not every turn.**
  The cache added earlier avoided rebuilding the map but not re-*sending* it:
  the text was written into a turn-local variable on every turn. Scope is now
  consulted *before* the producer runs, so a warm turn neither builds nor sends
  the map, the orientation blocks, or the repo-history read that feeds root
  resolution.
- **Retrieved memory no longer erases the repo map, and vice versa.** Assembly
  used one `memory_context` variable, so with AgentDB enabled the map was
  silently discarded and with it disabled the map was sent on every turn. Both
  wrong, neither visible. Producers now compose under their own headings.
- **The map describes the session's repository, not the operator's first
  `ADDITIONAL_DIRS` entry.** Candidates are now paths the session's own turns
  named that exist on disk (plus the workspace when it is itself a repo).
  Passing `ADDITIONAL_DIRS` wholesale made the first entry win, so a session
  working in one project received a ~1,500-token map of another — delivered with
  full confidence, and impossible for a model to recognise as wrong. When the
  session has no evidence, no map is injected.
- **Compaction checkpoint stubs are no longer injected as turn memory.** A
  retrieval pass returns `memory/compaction/checkpoint/<sid>` records alongside
  query-relevant ones, so a turn could receive the same stub several times and
  nothing else. Session-scoped records are separated from per-turn memory, and
  duplicate records are collapsed.

### Changed
- **Session states are now exactly three, and the idle reaper no longer invents
  one.** `NEW`/`WARM`/`COLD`/`RESUME_AFTER_TIMEOUT` are now
  `SESSION_START`/`SESSION_WARM`/`SESSION_RESUME`; `RESUME_AFTER_TIMEOUT` is
  gone. The reaper's only lifecycle effect is closing the client, which
  `has_client=False` already reports, so a row it flagged classifies as an
  ordinary `SESSION_RESUME`. Whether the user was *told* about the idleness is
  presentation, stored separately as `idle_notified_at` (read-compatible with
  rows written by an older process, which used `"expired": true`), and it now
  selects only the opener text: `⏰ Session went idle — resuming`. The overloaded
  `state.resumes` property is replaced by `state.is_boundary`, which means what
  the injection layer needs ("start or resume"), and `opener` takes
  `idle_notified=` rather than being a property. No behaviour change to what is
  injected, and the Claude runner is untouched.

### Fixed
- **A session that had gone idle could lose its saved session id.** The bots
  gated the resume path on `meta and not meta.get("expired")`, so once the
  reaper had notified the user, the next message fell through to the *start*
  path, re-registered the row and discarded `resume_session_id` — the SDK UUID
  on Claude. A row that exists is resumable; the notification flag is
  presentation and no longer gates the branch.
- **The repo map no longer describes the wrong repository.** `find_repo_root`
  walked up from the session workspace to the nearest `.git`, and session
  workspaces live at `agent-sessions/<platform>/<thread>/workspace` — inside
  AgentGate's own checkout. Every session that was not working on AgentGate
  therefore received a ~1,500-token map of AgentGate, presented with full
  confidence. Root resolution is now evidence-based: a validated
  `ADDITIONAL_DIRS` entry inside a repository, else the workspace when it is not
  nested inside one, else **no map at all**. A wrong map is worse than no map,
  because a model cannot tell one from the other.
- **`ADDITIONAL_DIRS` validation was POSIX-only.** It used `os.path.isabs` and
  `os.sep`, so on macOS/Linux every Windows path (`C:\Users\me\dev\komper`) was
  refused as "not an absolute path", `C:\` was not recognised as a filesystem
  root, and Windows duplicates were not collapsed. The validator now picks a
  path dialect from how the value is *written*, so Windows paths are judged by
  Windows rules on any host and vice versa. Case handling follows the dialect:
  case-insensitive for Windows (so `C:\Dev` and `c:\dev` are duplicates),
  case-sensitive for POSIX. A backslash is treated as a separator, not a shell
  escape.
- **"Session directories" no longer contains invented paths.** The per-turn
  directory report was a bare regex over the model's free text, so any
  slash-shaped fragment became a directory entry and was re-injected into
  context every turn (observed: `/agent-sessions/`, `/context/`, `/.../`,
  `/Users/jm/.../`). Extraction now lives in `core/context/dir_evidence.py` and
  requires three things the old code lacked: structural validity (no `...`,
  `..`, or doubled separators), a real resolution base (a relative path is
  never promoted to a root-level path), and **existence** (`os.path.isdir`).
  The trailing-slash regex also dropped the final segment, which is
  usually the directory actually being discussed
  (`/Users/jm/Documents/github/komper` reported as its parent).
- Entries whose directory no longer exists are pruned from the session's list
  before the block is built (`prune_session_directories`), so long sessions do
  not accumulate dead scratch paths. Elevated (deliberately granted) entries
  are never pruned.
- **`ADDITIONAL_DIRS` entries are normalised.** `"a, b"` (comma + space) left a
  leading space on every entry after the first, which made it non-absolute and
  non-existent, so the entry disappeared silently. Entries are now stripped and
  `~` is expanded. Relative entries are still kept verbatim rather than
  resolved against an unrelated process cwd.

### Changed
- **Orientation and the repo map are injected once per session** (start and
  resume) instead of every turn. Both describe things that do not change
  mid-session; re-sending identical text each turn cost tokens, and re-building
  the map re-scanned the repository on every message. They are cached per
  session and rebuilt when a new session begins, which also re-resolves the
  repository root. Per-turn context still carries what varies: retrieved memory
  and the skills nudge.

### Added
- **`ADDITIONAL_DIRS` entries are validated, and refusals are explained**
  (`core/context/dir_validation.py`). Previously a malformed entry was skipped
  by whichever consumer reached it first, so the operator believed a directory
  was available while the agent quietly could not see it. Entries are now
  checked once at import, in this order: format (non-empty, no shell/glob
  characters), absolute, depth, existence (a warning, not a refusal). Refused:
  relative paths, `/`, the home directory, shell fragments, duplicates. The
  accept-list is computed in one place, so the Claude allow-list, the AgentDB
  workspace snapshot and the orientation block cannot disagree. Both bots print
  the reason for every refused or dangling entry at startup.
- **Orientation block on every turn** (`core/context/orientation.py`). The
  open-model runners now tell the agent where it is: a `## Your starting
  directory` block naming the session workspace and stating that it is a
  scratch directory, normally empty and normally not the project under
  discussion. When `ADDITIONAL_DIRS` is set, a second, separately-titled
  `## Additional directories available to you` block lists those paths and says
  explicitly that a listed path does not override the user's instruction for
  the current task. Keeping the two blocks apart prevents the failure where an
  agent starts work in a listed repository instead of the one the user named.
  Built independently of AgentDB, so it survives `AGENTDB_ENABLED=false`.

### Documentation
- README: new **"Repo map and `ADDITIONAL_DIRS`"** section — what the map is and
  is not, why the session workspace is not your repository, why
  `ADDITIONAL_DIRS` should point at narrow per-project absolute paths (not a
  parent folder such as `$HOME`), the path-format rules, and how to preview the
  map before enabling it.

## [0.1.0] — 2026-11-20

### Added
- **Open-model Agent Skills (opt-in, OFF by default).** On-demand, standards-
  compatible `SKILL.md` skills for the `local`, `fireworks` and `deepinfra`
  runners:
  - shared operator-configured catalog (`AGENTGATE_SKILLS_DIR`, default `skills/`);
  - compact deterministic manifest in the stable prompt prefix;
  - `skill_read` / `skill_list` tools (exact body, no lossy compression);
  - **Mode A** (recommended when enabled): model-selected activation
    (`AGENTGATE_SKILLS_SELECTION_MODE=model`);
  - **Mode C**: explicit `//skill-name` user invocation (automatic when enabled);
  - **Mode B** (experimental): deterministic keyword trigger recommendations
    (`AGENTGATE_SKILLS_SELECTION_MODE=recommend`).
  Skill instructions are lower-trust (labeled user-role), path-contained, and
  fingerprint-checked; they never become system instructions or grant extra tool
  permissions. Claude and AgentDB are unchanged.
- `scripts/eval_skills.py`: isolated, cohort-separated OFF/A/B/C evaluation harness.
- `skills/example-summarize/`: sample skill template.

### Notes
- Disabled by default (`AGENTGATE_SKILLS_ENABLED=false`); with skills off, outbound
  requests are unchanged. Controlled evaluation showed a large task-success gain on
  specialized workflows at lower cost per successful task (single model).
