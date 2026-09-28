"""
Master runner — launch any combination of bots as subprocesses from a
single command, for local dev convenience. Not a process supervisor:
no restart-on-crash, no daemonization.

Usage:
    python run.py --slack
    python run.py --telegram
    python run.py --telegram fireworks personal gpt-oss-20b
    python run.py --slack --telegram claude work
    python run.py --whatsapp
    python run.py --list-bots

Each bot runs as its own subprocess (python -m bots.<channel>).
stdout/stderr from each child is streamed with a [label] prefix.
SIGINT/SIGTERM is forwarded to all children; they do their own teardown.
"""
import core.config  # loads .env first — keep this import at the top

import asyncio
import os
import re
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

CHANNELS_NO_ARGS = ("slack", "whatsapp")
VALID_RUNNERS = ("claude", "fireworks", "ollama", "mlx")


@dataclass(frozen=True)
class BotSpec:
    channel: str
    runner: str | None = None
    bot_id: str | None = None
    model: str | None = None

    def label(self) -> str:
        return f"{self.channel}/{self.bot_id}" if self.bot_id else self.channel

    def script_argv(self) -> list[str]:
        """Arguments after `sys.executable -u` for this bot."""
        if self.channel == "telegram":
            argv = ["-m", "bots.telegram"]
            if self.runner and self.runner != "claude":
                argv.append(f"--{self.runner}")
            if self.model:
                argv += ["--model", self.model]
            if self.bot_id:
                argv += ["--bot", self.bot_id]
            return argv
        if self.channel == "slack":
            return ["-m", "bots.slack"]
        if self.channel == "whatsapp":
            return ["-m", "bots.whatsapp"]
        return ["-m", f"bots.{self.channel}"]


def parse_argv(argv: list[str]) -> tuple[list[BotSpec], bool]:
    """Parse run.py's CLI args. Returns (specs, list_bots_requested).
    Raises SystemExit on malformed input."""
    specs: list[BotSpec] = []
    list_bots = False
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--list-bots":
            list_bots = True
            i += 1
            continue
        if tok.startswith("--") and tok[2:] in CHANNELS_NO_ARGS:
            specs.append(BotSpec(channel=tok[2:]))
            i += 1
            continue
        if tok == "--telegram":
            i += 1
            positional: list[str] = []
            while i < len(argv) and not argv[i].startswith("--") and len(positional) < 3:
                positional.append(argv[i])
                i += 1
            if i < len(argv) and not argv[i].startswith("--"):
                raise SystemExit(
                    f"--telegram accepts at most 3 values (runner, bot_id, model), "
                    f"got unexpected extra value: {argv[i]!r}"
                )
            runner = positional[0] if len(positional) >= 1 else "claude"
            bot_id = positional[1] if len(positional) >= 2 else None
            model = positional[2] if len(positional) >= 3 else None
            if runner not in VALID_RUNNERS:
                raise SystemExit(
                    f"--telegram: invalid runner {runner!r} (expected one of {VALID_RUNNERS}); "
                    f"note bot_id can't be set without an explicit runner first, "
                    f"e.g. '--telegram claude {runner}'"
                )
            specs.append(BotSpec(channel="telegram", runner=runner, bot_id=bot_id, model=model))
            continue
        raise SystemExit(f"unknown flag: {tok!r}")
    return specs, list_bots


# ---------------------------------------------------------------------------
# --list-bots
# ---------------------------------------------------------------------------

_TELEGRAM_TOKEN_SUFFIX_RE = re.compile(r"^TELEGRAM_BOT_TOKEN_(.+)$")


def _known_telegram_identities() -> list[str | None]:
    ids: list[str | None] = []
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        ids.append(None)
    for key in os.environ:
        m = _TELEGRAM_TOKEN_SUFFIX_RE.match(key)
        if m:
            ids.append(m.group(1).lower())
    return ids


def _isolated_agent_ids(memory_yaml_path: Path) -> set[str]:
    if not memory_yaml_path.exists():
        return set()
    data = yaml.safe_load(memory_yaml_path.read_text()) or {}
    agents = (data.get("profile") or {}).get("agents") or {}
    return set(agents.keys())


def _permitted_agent_ids(permissions_path: Path) -> set[str]:
    if not permissions_path.exists():
        return set()
    data = yaml.safe_load(permissions_path.read_text()) or {}
    return set((data.get("agents") or {}).keys())


def _allowlist_status_for(bot_id: str | None) -> str:
    if bot_id is None:
        return "n/a"
    suffix = bot_id.upper()
    if (f"ALLOWED_TELEGRAM_USER_IDS_{suffix}" in os.environ
            or f"ADMIN_TELEGRAM_USER_IDS_{suffix}" in os.environ):
        return "suffixed"
    return "inherited"


def print_known_telegram_bots() -> None:
    from bots.telegram import _agent_id_for
    import core.config as cfg

    if cfg.AGENTDB_ENABLED:
        memory_yaml_path = Path(cfg.AGENTDB_MEMORY_DIR) / "config" / "memory.yaml"
        isolated = _isolated_agent_ids(memory_yaml_path)
        permitted = _permitted_agent_ids(Path(cfg.AGENTDB_PERMISSIONS_PATH))
    else:
        isolated = set()
        permitted = set()

    print("Known Telegram identities (from .env):")
    identities = _known_telegram_identities()
    if not identities:
        print("  (none found — set TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN_<NAME> in .env)")
        return
    for bot_id in identities:
        agent_id = _agent_id_for(bot_id)
        label = bot_id or "(default)"
        allowlist = _allowlist_status_for(bot_id)
        if cfg.AGENTDB_ENABLED:
            isolation = (
                "isolated (listed in profile.agents)"
                if agent_id in isolated
                else "not listed in profile.agents"
            )
            permissions = "OK" if agent_id in permitted else "MISSING (writes will be denied)"
            print(
                f"  {label:<12} agent_id={agent_id:<28} allowlist: {allowlist:<9} "
                f"isolation: {isolation:<38} permissions: {permissions}"
            )
        else:
            print(f"  {label:<12} agent_id={agent_id:<28} allowlist: {allowlist}")


# ---------------------------------------------------------------------------
# Subprocess orchestration
# ---------------------------------------------------------------------------

async def _stream_output(label: str, proc: asyncio.subprocess.Process) -> None:
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        print(f"[{label}] {line.decode(errors='replace').rstrip()}", flush=True)


async def _wait_and_report(label: str, proc: asyncio.subprocess.Process) -> int:
    code = await proc.wait()
    print(f"[{label}] exited with code {code}", flush=True)
    return code


async def run_all(bots: list[tuple[str, list[str]]]) -> int:
    """Spawn every (label, argv) pair as a subprocess, stream their output
    prefixed by label, forward SIGINT/SIGTERM to all children, and return
    0 iff every child exited 0. A child exiting doesn't stop the others."""
    procs: list[tuple[str, asyncio.subprocess.Process]] = []
    for label, argv in bots:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        procs.append((label, proc))

    reader_tasks = [asyncio.create_task(_stream_output(label, proc)) for label, proc in procs]

    def _forward_signal(sig: int) -> None:
        for _, proc in procs:
            if proc.returncode is None:
                try:
                    proc.send_signal(sig)
                except ProcessLookupError:
                    pass

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _forward_signal, sig)

    exit_codes = await asyncio.gather(*(_wait_and_report(label, proc) for label, proc in procs))
    await asyncio.gather(*reader_tasks)
    return 0 if all(code == 0 for code in exit_codes) else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    specs, list_bots = parse_argv(sys.argv[1:])

    if list_bots:
        print_known_telegram_bots()
        return 0

    if not specs:
        print(
            "usage: run.py --slack | --telegram [runner bot_id model] | --whatsapp | --list-bots\n"
            "       runner: claude (default) | fireworks | ollama | mlx\n"
            "\n"
            "examples:\n"
            "  python run.py --slack\n"
            "  python run.py --telegram\n"
            "  python run.py --telegram fireworks personal\n"
            "  python run.py --slack --telegram claude work\n"
            "  python run.py --list-bots",
            file=sys.stderr,
        )
        return 1

    # -u: unbuffered stdout/stderr in the child so live lines appear immediately.
    bots = [
        (spec.label(), [sys.executable, "-u"] + spec.script_argv())
        for spec in specs
    ]
    return asyncio.run(run_all(bots))


if __name__ == "__main__":
    sys.exit(main())
