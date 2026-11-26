"""botmgr command-line tool.

Drives the same core as the web app (helper client, supervisor, store,
reconcile, watchdog) without any web layer. Useful for installation,
recovery (including the `unlock` flow's home in M2), and headless testing.

Every invocation reconciles first: bots started by an earlier invocation
are adopted, bots that died meanwhile are counted as crashed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
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
    from botmgr.envstore import SecretsService

    store = StateStore(root / "botmgr.db")
    client = HelperClient(direct_transport(root, package_dir))
    supervisor = Supervisor(
        HelperBackend(client),
        store=store,
        env_provider=SecretsService(root, store).resolve,
    )
    capture = CaptureController(client)
    report = reconcile(supervisor, store, client, capture)
    watchdog = Watchdog(supervisor, client, capture, root)
    return Runtime(store, client, supervisor, capture, watchdog, report)


def _web_secret(root: Path) -> str:
    env = os.environ.get("BOTENCLAVE_SECRET")
    if env:
        return env
    path = root / "web-secret"
    if path.exists():
        return path.read_text(encoding="ascii").strip()
    root.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    path.write_text(value, encoding="ascii")
    path.chmod(0o600)
    return value


async def _unlock(root: Path, email: str) -> bool:
    from botmgr.auth.deps import build_auth, init_auth_db
    from botmgr.auth.service import AuthService

    bundle = build_auth(root, _web_secret(root), cookie_secure=True)
    await init_auth_db(bundle)
    return await AuthService(bundle).unlock(email)


async def _setup_token(root: Path, regenerate: bool) -> str:
    from botmgr.auth.deps import build_auth, init_auth_db
    from botmgr.auth.routes import setup_token_path
    from botmgr.auth.service import AuthService

    path = setup_token_path(root)
    if path.exists() and not regenerate:
        return path.read_text(encoding="ascii").strip()
    bundle = build_auth(root, _web_secret(root), cookie_secure=True)
    await init_auth_db(bundle)
    if await AuthService(bundle).count_users() > 0:
        raise RuntimeError("setup is already complete; no token exists")
    token = secrets.token_urlsafe(24)
    path.write_text(token, encoding="ascii")
    path.chmod(0o600)
    return token


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

    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--transport", choices=("direct", "sudo"), default="direct")
    p.add_argument("--no-cookie-secure", action="store_true")
    p = sub.add_parser("unlock")
    p.add_argument("email")
    p = sub.add_parser("setup-token")
    p.add_argument("--regenerate", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "serve":
        import uvicorn

        from botmgr.app import AppConfig, create_app

        app = create_app(
            AppConfig(
                root=args.root,
                secret=_web_secret(args.root),
                cookie_secure=not args.no_cookie_secure,
                transport=args.transport,
                package_dir=args.package_dir,
            )
        )
        uvicorn.run(app, host=args.host, port=args.port)
        return 0
    if args.command == "unlock":
        if asyncio.run(_unlock(args.root, args.email)):
            print(f"unlocked {args.email}")
            return 0
        print(f"no such user: {args.email}", file=sys.stderr)
        return 1
    if args.command == "setup-token":
        try:
            print(asyncio.run(_setup_token(args.root, args.regenerate)))
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
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
