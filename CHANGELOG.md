# Changelog

All notable changes to AgentGate are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/); versions follow the
project's release practice.

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
