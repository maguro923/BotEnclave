"""botmgr command-line tool.

Drives the same core as the web app (helper client, supervisor, store,
reconcile, watchdog) without any web layer. Useful for installation,
recovery (including the `unlock` flow's home in M2), and headless testing.

Every invocation reconciles first: bots started by an earlier invocation
are adopted, bots that died meanwhile are counted as crashed.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from botmgr.console import CaptureController
from botmgr.helper import HelperClient, HelperError, direct_transport
from botmgr.helper_backend import HelperBackend
from botmgr.launcher import LaunchConfig
from botmgr.reconcile import ReconcileReport, reconcile
from botmgr.store import StateStore
from botmgr.supervisor import Supervisor
from botmgr.watchdog import Watchdog


@dataclass
class Runtime:
    store: StateStore
    client: HelperClient
    supervisor: Supervisor
    capture: CaptureController
    watchdog: Watchdog
    report: ReconcileReport


def build_runtime(root: Path, package_dir: Path | None = None) -> Runtime:
    store = StateStore(root / "botmgr.db")
    client = HelperClient(direct_transport(root, package_dir))
    supervisor = Supervisor(HelperBackend(client), store=store)
    capture = CaptureController(client)
    report = reconcile(supervisor, store, client, capture)
    watchdog = Watchdog(supervisor, client, capture, root)
    return Runtime(store, client, supervisor, capture, watchdog, report)


def _print_report(report: ReconcileReport) -> None:
    for bot in report.adopted_running:
        print(f"reconcile: {bot} still running, adopted")
    for bot in report.found_dead:
        print(f"reconcile: {bot} had exited, counted as crashed")
    for bot in report.capture_reattached:
        print(f"reconcile: {bot} log capture re-attached")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="botmgr")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("BOTENCLAVE_ROOT", "/srv/botenclave")),
    )
    parser.add_argument("--package-dir", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("user-create")
    p.add_argument("bot")
    p = sub.add_parser("user-delete")
    p.add_argument("bot")

    p = sub.add_parser("bot-add")
    p.add_argument("bot")
    p.add_argument("--cwd", default=None)
    p.add_argument("--env", action="append", default=[])
    p.add_argument("bot_command", nargs=argparse.REMAINDER)

    for name in ("bot-start", "bot-stop", "bot-status", "bot-remove"):
        p = sub.add_parser(name)
        p.add_argument("bot")
    sub.add_parser("bot-list")
    p = sub.add_parser("watch")
    p.add_argument("--interval", type=float, default=5.0)

    args = parser.parse_args(argv)
    runtime = build_runtime(args.root, args.package_dir)
    _print_report(runtime.report)
    supervisor = runtime.supervisor
    try:
        if args.command == "user-create":
            print(runtime.client.create_user(args.bot))
        elif args.command == "user-delete":
            print(runtime.client.delete_user(args.bot))
        elif args.command == "bot-add":
            command = list(args.bot_command)
            if command and command[0] == "--":
                command = command[1:]
            if not command:
                parser.error("bot-add needs a command after --")
            env: dict[str, str] = {}
            for pair in args.env:
                key, sep, value = pair.partition("=")
                if not sep:
                    parser.error(f"--env expects KEY=VALUE, got {pair!r}")
                env[key] = value
            cwd = args.cwd or str(args.root / "bots" / args.bot / "data")
            supervisor.register(
                args.bot, LaunchConfig(tuple(command), cwd, env)
            )
            print(f"registered {args.bot}")
        elif args.command == "bot-start":
            supervisor.start(args.bot)
            print(f"{args.bot}: {supervisor.state(args.bot).value}")
        elif args.command == "bot-stop":
            supervisor.stop(args.bot)
            print(f"{args.bot}: {supervisor.state(args.bot).value}")
        elif args.command == "bot-status":
            supervisor.tick()
            print(f"{args.bot}: {supervisor.state(args.bot).value}")
        elif args.command == "bot-remove":
            if supervisor.state(args.bot).value not in ("stopped", "attention"):
                supervisor.stop(args.bot)
            runtime.store.remove_bot(args.bot)
            print(f"removed {args.bot}")
        elif args.command == "bot-list":
            supervisor.tick()
            for bot in supervisor.bot_ids():
                print(f"{bot}: {supervisor.state(bot).value}")
        elif args.command == "watch":
            try:
                while True:
                    for event in runtime.watchdog.step():
                        print(event, flush=True)
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                return 0
    except HelperError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        runtime.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
