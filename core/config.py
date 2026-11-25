"""
Central configuration: every env var is read here.
Required vars fail loudly at startup; no os.environ.get() scattered elsewhere.
"""
from dotenv import load_dotenv
load_dotenv()

import os

# -------------------------
# PATHS & TIMEOUTS
# -------------------------
BASE = "agent-sessions"

IDLE_TIMEOUT_SECONDS = int(os.environ.get("IDLE_TIMEOUT_SECONDS", 8 * 60 * 60))     # 8h
APPROVAL_TIMEOUT_SECONDS = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", 4 * 60 * 60))  # 4h
REAPER_INTERVAL_SECONDS = 300
SLOW_RESPONSE_SECONDS = int(os.environ.get("SLOW_RESPONSE_SECONDS", 60))

# Extra directories the agent may read/write freely. Comma-separated, and each
# entry must be an ABSOLUTE path ("/Users/me/code/thing"): a relative path would
# resolve against whatever the process cwd happens to be, which is not the
# session workspace, so it would silently point somewhere unintended.
#
# Entries are normalised AND validated here, once, rather than at each use site,
# because the consumers differ in how forgiving they are:
#   * runners/claude.py passes them straight to the SDK's add_dirs, where a
#     malformed entry is an opaque failure, and
#   * the directory report and orientation block resolve them against the
#     filesystem, where a bad value simply means "does not exist" and the entry
#     disappears without a word.
# Silent disappearance is the failure this guards against: the operator believes
# a directory is available and the agent quietly cannot see it. Every rejected
# value carries a reason, which the bots print at startup.
def _parse_additional_dirs(raw: str) -> list[str]:
    """Split on commas and trim, preserving empty entries for validation.

    An empty entry is kept (as ``""``) so a stray comma is reported by the
    validator rather than dropped here, where it would leave no trace.
    """
    return [(entry or "").strip() for entry in (raw or "").split(",")]


def _validate_additional_dirs(entries: list[str]) -> list:
    """Run the shared validator. Never raises; a broken import is not fatal."""
    try:
        from core.context.dir_validation import check_additional_dirs

        return check_additional_dirs(entries)
    except Exception:
        # Fail open on the *feature*, not on the process: expose the entries
        # unvalidated rather than starting with none.
        from core.context.dir_validation import DirCheck

        return [DirCheck(e, os.path.expanduser(e), "accepted") for e in entries if e]


# Validated once at import so every consumer sees the same decision, and so the
# reason a value was refused is available to the startup banner.
ADDITIONAL_DIR_CHECKS = _validate_additional_dirs(
    _parse_additional_dirs(os.environ.get("ADDITIONAL_DIRS", ""))
)

#: The entries actually handed to runners. Refused values are excluded here
#: rather than at each use site, so the Claude allow-list, the AgentDB workspace
#: snapshot and the orientation block cannot disagree about what is configured.
ADDITIONAL_DIRS = [c.path for c in ADDITIONAL_DIR_CHECKS if c.usable]


def additional_dirs_report() -> list[str]:
    """Human-readable lines describing rejected or dangling entries.

    Empty when every configured entry is fine. Bots print these at startup so a
    typo is visible instead of leaving a directory mysteriously unavailable.
    """
    from core.context.dir_validation import describe_problems

    return describe_problems(ADDITIONAL_DIR_CHECKS)

# -------------------------
# SLACK
# -------------------------
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")

ALLOWED_SLACK_USER_IDS = set(
    filter(None, os.environ.get("ALLOWED_SLACK_USER_IDS", "").split(","))
)
ADMIN_SLACK_USER_IDS = set(
    filter(None, os.environ.get("ADMIN_SLACK_USER_IDS", "").split(","))
)
SLOW_RESPONSE_EMOJI = os.environ.get("SLOW_RESPONSE_EMOJI", "turtle")

# -------------------------
# TELEGRAM
# -------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

ALLOWED_TELEGRAM_USER_IDS = set(
    filter(None, os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "").split(","))
)
ADMIN_TELEGRAM_USER_IDS = set(
    filter(None, os.environ.get("ADMIN_TELEGRAM_USER_IDS", "").split(","))
)

TELEGRAM_THINKING_REACTION = os.environ.get("TELEGRAM_THINKING_REACTION", "🤔")
TELEGRAM_SLOW_RESPONSE_REACTION = os.environ.get("TELEGRAM_SLOW_RESPONSE_REACTION", "🐳")

# -------------------------
# WHATSAPP (wppconnect linked-device)
# -------------------------
WPPCONNECT_SERVER_URL = os.environ.get("WPPCONNECT_SERVER_URL", "http://localhost:8082")
ALLOWED_WHATSAPP_USER_IDS = set(
    filter(None, os.environ.get("ALLOWED_WHATSAPP_USER_IDS", "").split(","))
)
ADMIN_WHATSAPP_USER_IDS = set(
    filter(None, os.environ.get("ADMIN_WHATSAPP_USER_IDS", "").split(","))
)

# -------------------------
# RUNNERS (open-model providers)
# -------------------------
# Provider configuration lives in core/providers.py (single source of truth).
# Use `from core.providers import settings_for` and call settings_for("<provider>").
# Claude is different by design: no credential, no base URL and no sampling knobs.
# Its settings (CLAUDE_SETTINGS / SDK_ALLOWED_TOOLS / SYSTEM_PROMPT) stay below
# and in runners/claude_reasoning.py, deliberately separate from provider specs.

# -------------------------
# AGENTDB (governed long-term memory — enabled by default)
# -------------------------
AGENTDB_ENABLED = os.environ.get("AGENTDB_ENABLED", "true").lower() in ("true", "1", "yes")
# AGENTDB_MEMORY_DIR is the workspace root. The SQLite DB lives at
#   <AGENTDB_MEMORY_DIR>/database/agentdb.sqlite
# and the config file at:
#   <AGENTDB_MEMORY_DIR>/config/memory.yaml
# To enable semantic retrieval, add to memory.yaml:
#   embeddings:
#     enabled: true
#   (requires: pip install 'agentdatabase[local-embed]')
AGENTDB_MEMORY_DIR = os.environ.get("AGENTDB_MEMORY_DIR", "agentdb-workspace")
AGENTDB_PERMISSIONS_PATH = os.environ.get("AGENTDB_PERMISSIONS_PATH", "permissions.yaml")
# Consolidation synthesis (llm_fn): OFF by default. When enabled AND a
# consolidation model is configured ({PREFIX}_CONSOLIDATION_MODEL or a global
# CONSOLIDATION_MODEL), AgentGate passes an llm_fn to end_session so consolidation
# can synthesize a canonical instead of copying. Inert until the upstream
# auto-consolidation dispatch is fixed (agentdb package spec §1); safe on/off.
AGENTDB_CONSOLIDATION_ENABLED = os.environ.get(
    "AGENTDB_CONSOLIDATION_ENABLED", "false"
).lower() in ("true", "1", "yes")


# -------------------------
# CONTEXT EFFICIENCY (optional; every flag OFF by default)
#
# Native token-efficiency features (see docs/context-efficiency.md). Compression is LOSSY in the
# model-facing message, but the original is always kept and retrievable, and it
# is NOT redaction: it never scrubs secrets. With every flag off, outbound
# requests are byte-for-byte unchanged.
# -------------------------
def _env_bool(name, default="false"):
    return os.environ.get(name, default).strip().lower() in ("true", "1", "yes", "on")


# C1: native targeted compression of bulky tool results.
TOOL_COMPRESSION_ENABLED = _env_bool("AGENTGATE_TOOL_COMPRESSION_ENABLED")
# Only tool results at least this many chars are considered (advisory).
TOOL_COMPRESSION_MIN_CHARS = int(os.environ.get("AGENTGATE_TOOL_COMPRESSION_MIN_CHARS", 4000))

# Where originals are kept. "" -> <session-base>/_artifacts (per platform base).
TOOL_ARTIFACT_DIR = os.environ.get("AGENTGATE_TOOL_ARTIFACT_DIR", "")

# C2: deterministic cross-turn deduplication of repeated tool results.
CROSS_TURN_DEDUP_ENABLED = _env_bool("AGENTGATE_CROSS_TURN_DEDUP_ENABLED")

# C3: Python-first incremental repository map (context injection).
REPO_MAP_ENABLED = _env_bool("AGENTGATE_REPO_MAP_ENABLED")
REPO_MAP_MAX_TOKENS = int(os.environ.get("AGENTGATE_REPO_MAP_MAX_TOKENS", 1500))

# -------------------------
# OPEN-MODEL SKILLS (Agent Skills; OFF by default)
#
# On-demand, model-selected skill instructions for the open-model runners
# (local / fireworks / deepinfra). Claude and AgentDB are excluded by design.
# With AGENTGATE_SKILLS_ENABLED=false (the default) nothing here is consulted,
# and outbound requests are byte-for-byte unchanged.
# See the "Skills" section of README.md.
# -------------------------
AGENTGATE_SKILLS_ENABLED = _env_bool("AGENTGATE_SKILLS_ENABLED")
# One operator-configured catalog shared by all open-model runners.
AGENTGATE_SKILLS_DIR = os.environ.get("AGENTGATE_SKILLS_DIR", "skills")
# Token budget for the compact skills manifest in the stable prompt section.
AGENTGATE_SKILLS_MANIFEST_TOKENS = int(
    os.environ.get("AGENTGATE_SKILLS_MANIFEST_TOKENS", 600)
)
# "model" (A: model-selected) or "recommend" (B: harness-assisted nudges).
AGENTGATE_SKILLS_SELECTION_MODE = os.environ.get(
    "AGENTGATE_SKILLS_SELECTION_MODE", "model"
).strip().lower()

# -------------------------
# WORKFLOW ORCHESTRATION (OFF by default)
#
# When disabled (default), the `milestone_submit` tool is NOT built and is
# invisible to models: the registry is byte-for-byte what it was before
# orchestration existed. When enabled, an implementer/reviewer agent may *request*
# a transition; it never spawns an agent, approves a gate, or advances a workflow.
# Approval remains a human/host operation.
# See docs: handoff-orchestration-spec-reconciled.md.
# -------------------------
AGENTGATE_WORKFLOWS_ENABLED = _env_bool("AGENTGATE_WORKFLOWS_ENABLED")
# Host-private directory where handoff requests are written. Deliberately NOT
# under docs/spec/: that is project deliverable space, this is runtime state.
AGENTGATE_WORKFLOW_REGISTRY_DIR = os.environ.get(
    "AGENTGATE_WORKFLOW_REGISTRY_DIR", "agent-sessions/workflows"
)
# Managed root for per-workflow Git worktrees (DEC-201). Host-private and already
# covered by the agent-sessions/ ignore rule, so managed trees never touch repo status.
AGENTGATE_WORKSPACE_ROOT = os.environ.get(
    "AGENTGATE_WORKSPACE_ROOT", "agent-sessions/workspaces"
)
# Durable cross-process registry (DEC-202). Same volume as the worktrees so
# registration and rename operations stay atomic.
AGENTGATE_ORCHESTRATION_DB = os.environ.get(
    "AGENTGATE_ORCHESTRATION_DB", "agent-sessions/orchestration.db"
)
# Durable gate store (DEC-301): separate DB from the workspace registry -- gates and
# worktrees have different lifecycles, and mixing them would make the registry a
# general-purpose store. Gate POLICY (who approves, whether a human is required) is
# orchestrator configuration and is deliberately NOT read here.
AGENTGATE_GATE_DB = os.environ.get(
    "AGENTGATE_GATE_DB", "agent-sessions/gates.db"
)

# -------------------------
# SECURITY LAYER 1 — hard blocks on both user messages and generated Bash commands
# -------------------------
BLOCKED_PATTERNS = [
    r"rm\s+(-\w+\s+)*(/\s|~/|~\s|\.\s|\.\.|/\*|\*)",
    r"rm\s+-[a-z]*r[a-z]*f\s+(/|~|\.|\.\.|\*)",
    r"rm\s+-[a-z]*f[a-z]*r\s+(/|~|\.|\.\.|\*)",
    r"rm\s+.*\.(env|pem|key|p12|pfx)",
    r"rm\s+.*(credentials|secrets|token)",
    r">+\s*\.env\b",
    r"tee\s+(-a\s+)?\.env\b",
    r"DROP\s+TABLE",
    r"DROP\s+SCHEMA",
    r"DROP\s+DATABASE",
    r"TRUNCATE\s+TABLE",
    r"DELETE\s+FROM\s+\w+\s*;",
    r"git\s+push\s+--force",
    r"git\s+reset\s+--hard",
    r"git\s+clean\s+-fd",
    r"curl\s+.*\|\s*(ba)?sh",
    r"wget\s+.*\|\s*(ba)?sh",
    r"chmod\s+777",
    r"sudo\s+rm",
    r"sudo\s+dd",
]

# -------------------------
# SECURITY LAYER 2 — tools auto-allowed at the SDK level (Bash is NOT here)
# -------------------------
SDK_ALLOWED_TOOLS = ["Read", "Write", "Edit", "MultiEdit", "Glob", "Grep"]

# -------------------------
# SECURITY LAYER 3 — settings.json written into each session workspace
# -------------------------
CLAUDE_SETTINGS = {
    "permissions": {
        "allow": [
            "Bash(dbt *)",
            "Bash(python *)",
            "Bash(python3 *)",
            "Bash(pytest *)",
            "Bash(pip *)",
            "Bash(pip3 *)",
            "Bash(uv *)",
            "Bash(bq *)",
            "Bash(cat *)", "Bash(ls *)", "Bash(find *)", "Bash(grep *)",
            "Bash(echo *)", "Bash(which *)", "Bash(wc *)", "Bash(head *)",
            "Bash(tail *)", "Bash(sort *)", "Bash(uniq *)", "Bash(cut *)",
            "Bash(awk *)", "Bash(sed *)", "Bash(jq *)",
            "Bash(mkdir *)", "Bash(cp *)", "Bash(mv *)", "Bash(touch *)",
            "Bash(git status *)", "Bash(git log *)", "Bash(git diff *)",
            "Bash(git show *)", "Bash(git branch *)", "Bash(git checkout *)",
            "Bash(git fetch *)", "Bash(git pull *)", "Bash(git ls-remote *)",
            "Bash(git add *)",
        ],
        "deny": [
            "Bash(sudo *)",
            "Bash(curl * | *)",
            "Bash(wget * | *)",
            "Bash(git push --force*)",
            "Bash(git reset --hard*)",
            "Bash(git clean -fd*)",
            "Bash(chmod 777 *)",
            "Read(**/.env)",
            "Read(**/.env.*)",
            "Write(**/.env)",
            "Write(**/.env.*)",
            "Edit(**/.env)",
            "Edit(**/.env.*)",
            "MultiEdit(**/.env)",
            "MultiEdit(**/.env.*)",
        ],
    },
}

# -------------------------
# SECURITY LAYER 4 — hard rules appended to every session
#
# FALLBACK ONLY. Every runner — Claude included — reads the rules from
# prompts/hard_rules.md via core.prompts.build_hard_rules(); this string is
# used only when that file is missing, empty, or unreadable.
#
# Edit prompts/hard_rules.md to change the rules. Keep this copy roughly in
# step so a deleted file still degrades to sane text, but it is not the source
# of truth and nothing reads it during normal operation.
# -------------------------
SYSTEM_PROMPT = """
You are a coding assistant working inside a local directory.

HARD RULES — never violate these under any circumstances:
- Never delete, drop, or truncate database tables, schemas, or records
- Never delete key files: .env, .env.*, **/secrets/*, **/*.pem, **/*.key, **/credentials.*
- Never run: chmod 777, curl|bash, wget|bash, sudo rm, sudo dd
- Never run destructive git commands: push --force, reset --hard, clean -fd
- Never run DELETE FROM without a WHERE clause
- Never expose secrets, credentials, or tokens in output

FILE DELETION RULES:
- You MAY delete specific named files that are stale or no longer needed
- You MUST NOT use broad patterns: rm -rf ., rm -rf *, rm -rf /, rm -rf ~
- Always tell the user which files you are about to delete before deleting them
- Prefer moving files to a _archive/ folder over hard deletion when impact is unclear

BASH RULES:
- Prefer dry-run or --check flags when available
- For Python, run in a virtualenv if one exists

DIRECTORY LOOKUP:
- Before searching the filesystem for a directory referred to by name,
  call lookup_directory first.
- If lookup_directory finds it, use that path directly.
- If not found, locate it yourself, then call remember_directory.

GIT RULES:
- When your work is complete, run git commit with a concise message,
  and git push when appropriate. These commands require the user's approval,
  which is handled automatically — just run them when ready.
""".strip()
