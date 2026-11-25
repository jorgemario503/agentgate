# Changelog

All notable changes to AgentGate are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/); versions follow the
project's release practice.

## [Unreleased]

### Fixed
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

### Added
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
