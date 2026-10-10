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

# Extra directories the agent may read/write freely (comma-separated absolute paths).
ADDITIONAL_DIRS = list(filter(None, os.environ.get("ADDITIONAL_DIRS", "").split(",")))

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
# RUNNER — Fireworks
# -------------------------
FIREWORKS_API_KEY = os.environ.get("FIREWORKS_API_KEY", "")
FIREWORKS_MODEL = os.environ.get("FIREWORKS_MODEL", "")
FIREWORKS_TEMPERATURE = float(os.environ.get("FIREWORKS_TEMPERATURE", "0.2"))
FIREWORKS_MAX_TOKENS = int(os.environ.get("FIREWORKS_MAX_TOKENS", "4096"))

# -------------------------
# RUNNER — Ollama
# -------------------------
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")
OLLAMA_CONTEXT_WINDOW = int(os.environ.get("OLLAMA_CONTEXT_WINDOW", "32768"))
OLLAMA_TEMPERATURE = float(os.environ.get("OLLAMA_TEMPERATURE", "0.2"))
OLLAMA_MAX_TOKENS = int(os.environ.get("OLLAMA_MAX_TOKENS", "4096"))
OLLAMA_SUMMARY_MODEL = os.environ.get("OLLAMA_SUMMARY_MODEL", "")

# -------------------------
# RUNNER — MLX / second local server
# -------------------------
MLX_BASE_URL = os.environ.get("MLX_BASE_URL", "http://localhost:8081/v1")
MLX_MODEL = os.environ.get("MLX_MODEL", "")
MLX_API_KEY = os.environ.get("MLX_API_KEY", "")
MLX_CONTEXT_WINDOW = int(os.environ.get("MLX_CONTEXT_WINDOW", "32768"))
MLX_TEMPERATURE = float(os.environ.get("MLX_TEMPERATURE", "0.2"))
MLX_MAX_TOKENS = int(os.environ.get("MLX_MAX_TOKENS", "4096"))
MLX_SUMMARY_MODEL = os.environ.get("MLX_SUMMARY_MODEL", "")

# -------------------------
# AGENTDB (optional — disabled by default)
# -------------------------
AGENTDB_ENABLED = os.environ.get("AGENTDB_ENABLED", "false").lower() in ("true", "1", "yes")
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
# SECURITY LAYER 4 — system prompt appended to every session
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
