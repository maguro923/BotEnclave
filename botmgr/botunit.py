"""Per-bot systemd unit entry point (PLAN sections 3.3, 5.2).

Runs as the bot user, as the unit's main process. It starts the bot's
tmux server with one pane running the launcher shim in --files mode
(non-secret launch.json + the credentials-directory env file that PID 1
decrypted from the LoadCredentialEncrypted credential), attaches log
capture exactly like the helper path, and then waits: when the pane
dies or the session disappears, this process exits so systemd records
the bot's exit. Stopping is normally driven by the manager through the
helper (graceful signal sequence against the pane's process tree); the
unit's ExecStop kills the tmux server as the backstop.

Usage: python -m botmgr.botunit --root <root> --bot <bot>
           [--package-dir <dir>] [--stop]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from botmgr.helperd import socket_name_for, username_for

POLL_SECONDS = 1.0


def _tmux(package_dir: Path, bot: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "tmux",
            "-u",  # force UTF-8 regardless of locale (see helperd._tmux)
            "-L",
            socket_name_for(bot),
            "-f",
            str(package_dir / "tmux.conf"),
            *args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def _pane_state(package_dir: Path, bot: str) -> str:
    """'alive', 'dead' (pane exited, session kept by remain-on-exit), or
    'gone' (no server/session)."""
    proc = _tmux(
        package_dir, bot, "display-message", "-p", "-t", "main", "#{pane_dead}"
    )
    if proc.returncode != 0:
        return "gone"
    return "dead" if proc.stdout.strip() == "1" else "alive"


def start_and_wait(root: Path, bot: str, package_dir: Path) -> int:
    bot_dir = root / "bots" / bot
    launch_json = bot_dir / "launch.json"
    if not launch_json.exists():
        print(f"botunit: no launch.json for {bot}", file=sys.stderr)
        return 1
    credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    env_file: Path | None = None
    if credentials_dir:
        candidate = Path(credentials_dir) / f"botmgr-{bot}"
        if candidate.exists():
            env_file = candidate
    # dotenv compatibility mode (PLAN 2.8): botunit is the process that
    # can see the merged env in unit mode, so it writes data/.env here.
    launch_data = json.loads(launch_json.read_text(encoding="utf-8"))
    if launch_data.get("dotenv_mode"):
        from botmgr.launcher import config_from_json, format_env_text, parse_env_text

        config = config_from_json(launch_json.read_bytes())
        merged = dict(config.env)
        if env_file is not None:
            merged.update(parse_env_text(env_file.read_text(encoding="utf-8")))
        dotenv = bot_dir / "data" / ".env"
        tmp = bot_dir / "data" / f".env.tmp-{os.urandom(4).hex()}"
        tmp.write_text(format_env_text(merged), encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, dotenv)
    package_parent = str(package_dir.parent)
    shim = (
        f"PYTHONPATH={package_parent} exec /usr/bin/python3"
        f" -m botmgr.launcher --files {launch_json}"
    )
    if env_file is not None:
        shim += f" {env_file}"
    proc = _tmux(
        package_dir,
        bot,
        "new-session",
        "-d",
        "-s",
        "main",
        "-x",
        "200",
        "-y",
        "50",
        shim,
    )
    if proc.returncode != 0:
        print(f"botunit: tmux new-session failed: {proc.stderr.strip()}", file=sys.stderr)
        return 1
    # Attach log capture before output starts, mirroring the helper path.
    target = bot_dir / "logs" / "console.log"
    _tmux(package_dir, bot, "pipe-pane", "-t", "main", f"cat >> {target}")
    while True:
        state = _pane_state(package_dir, bot)
        if state != "alive":
            return 0
        time.sleep(POLL_SECONDS)


def stop(root: Path, bot: str, package_dir: Path) -> int:
    _tmux(package_dir, bot, "kill-server")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="botunit")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--bot", required=True)
    parser.add_argument("--package-dir", type=Path, default=None)
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args(argv)
    package_dir = args.package_dir or Path(__file__).resolve().parent
    # Sanity: this process is expected to run as the bot's own user.
    expected = username_for(args.bot)
    import pwd

    if pwd.getpwuid(os.getuid()).pw_name != expected and os.getuid() != 0:
        print(f"botunit: must run as {expected}", file=sys.stderr)
        return 1
    if args.stop:
        return stop(args.root, args.bot, package_dir)
    # Keep a JSON parse check early so a corrupt launch.json fails fast.
    json.loads((args.root / "bots" / args.bot / "launch.json").read_text())
    return start_and_wait(args.root, args.bot, package_dir)


if __name__ == "__main__":
    raise SystemExit(main())
