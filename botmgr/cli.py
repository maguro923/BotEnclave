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

    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o711)  # bot users traverse the root to reach their folders
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


def _systemd_command(args: argparse.Namespace) -> int:
    import subprocess

    from botmgr import systemd_units

    root: Path = args.root
    package_dir = (args.package_dir or Path(__file__).resolve().parent).resolve()
    command = args.systemd_command
    if command == "render-manager":
        print(
            systemd_units.render_manager_unit(
                root,
                args.manager_user,
                package_dir,
                host=args.host,
                port=args.port,
            )
        )
        return 0
    if command == "render-bot":
        use_credential = (root / "secrets" / f"{args.bot}.cred").exists()
        print(
            systemd_units.render_bot_unit(
                args.bot, root, package_dir, use_credential=use_credential
            )
        )
        return 0
    if os.geteuid() != 0:
        print("systemd install commands require root", file=sys.stderr)
        return 1
    if command == "install":
        entry = Path(systemd_units.HELPER_ENTRY_PATH)
        entry.parent.mkdir(parents=True, exist_ok=True)
        manager_group: str | None = None
        try:
            import grp
            import pwd

            manager_group = grp.getgrgid(
                pwd.getpwnam(args.manager_user).pw_gid
            ).gr_name
        except KeyError:
            manager_group = None
        entry.write_text(
            systemd_units.render_helper_entry(
                root, package_dir, manager_group=manager_group
            ),
            encoding="utf-8",
        )
        entry.chmod(0o755)
        print(f"wrote {entry}")
        sudoers = Path("/etc/sudoers.d/botenclave")
        sudoers.write_text(
            systemd_units.render_sudoers(args.manager_user), encoding="utf-8"
        )
        sudoers.chmod(0o440)
        print(f"wrote {sudoers}")
        unit = Path("/etc/systemd/system") / systemd_units.MANAGER_UNIT
        unit.write_text(
            systemd_units.render_manager_unit(
                root,
                args.manager_user,
                package_dir,
                host=args.host,
                port=args.port,
            ),
            encoding="utf-8",
        )
        print(f"wrote {unit}")
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        print(
            "next: create the manager user if needed, then"
            " `systemctl enable --now botenclave`"
        )
        return 0
    if command == "install-bot":
        use_credential = (root / "secrets" / f"{args.bot}.cred").exists()
        unit = Path("/etc/systemd/system") / systemd_units.bot_unit_name(args.bot)
        unit.write_text(
            systemd_units.render_bot_unit(
                args.bot,
                root,
                package_dir,
                memory_max=args.memory_max,
                cpu_quota=args.cpu_quota,
                use_credential=use_credential,
            ),
            encoding="utf-8",
        )
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        print(
            f"wrote {unit} (credential: {'yes' if use_credential else 'no'});"
            " start the bot from the panel or CLI as usual"
        )
        return 0
    return 2


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

    for name in ("bot-start", "bot-stop", "bot-status"):
        p = sub.add_parser(name)
        p.add_argument("bot")
    p = sub.add_parser("bot-remove")
    p.add_argument("bot")
    p.add_argument("--purge-files", action="store_true")
    sub.add_parser("bot-list")
    p = sub.add_parser("watch")
    p.add_argument("--interval", type=float, default=5.0)

    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--transport", choices=("direct", "sudo"), default="direct")
    p.add_argument("--no-cookie-secure", action="store_true")
    p.add_argument(
        "--bot-runtime", choices=("helper", "systemd"), default="helper"
    )
    p = sub.add_parser("unlock")
    p.add_argument("email")
    p = sub.add_parser("setup-token")
    p.add_argument("--regenerate", action="store_true")

    p = sub.add_parser("audit")
    audit_sub = p.add_subparsers(dest="audit_command", required=True)
    audit_sub.add_parser("verify")
    p_purge = audit_sub.add_parser("purge")
    p_purge.add_argument("--days", type=int, default=180)

    p = sub.add_parser("systemd")
    systemd_sub = p.add_subparsers(dest="systemd_command", required=True)
    p = systemd_sub.add_parser("render-manager")
    p.add_argument("--manager-user", default="botenclave")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p = systemd_sub.add_parser("render-bot")
    p.add_argument("bot")
    p = systemd_sub.add_parser("install")
    p.add_argument("--manager-user", default="botenclave")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p = systemd_sub.add_parser("install-bot")
    p.add_argument("bot")
    p.add_argument("--memory-max", default="512M")
    p.add_argument("--cpu-quota", default="100%")

    p = sub.add_parser("backup")
    p.add_argument("dest_dir", type=Path)
    p.add_argument("--include-releases", action="store_true")
    p = sub.add_parser("restore")
    p.add_argument("archive", type=Path)
    p.add_argument("--force", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "backup":
        from botmgr.backup import BackupError, create_backup

        try:
            archive = create_backup(
                args.root, args.dest_dir, include_releases=args.include_releases
            )
        except BackupError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"backup written: {archive}")
        return 0
    if args.command == "restore":
        from botmgr.backup import BackupError, restore_backup

        try:
            restored = restore_backup(args.archive, args.root, force=args.force)
        except BackupError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"restored {len(restored)} files into {args.root}")
        return 0
    if args.command == "systemd":
        return _systemd_command(args)
    if args.command == "audit":
        from botmgr.audit import AuditLog

        log = AuditLog(args.root / "audit.log")
        if args.audit_command == "verify":
            ok = log.verify_chain()
            count = len(log.read_all())
            print(f"audit chain {'OK' if ok else 'BROKEN'} ({count} entries)")
            return 0 if ok else 1
        dropped = log.purge(time.time() - args.days * 86400)
        print(f"purged {dropped} entries older than {args.days} days")
        return 0
    if args.command == "serve":
        import uvicorn

        from botmgr.app import AppConfig, create_app
        from botmgr.tls import cert_fingerprint, ensure_tls_material

        if args.no_cookie_secure:
            print(
                "warning: session cookies will not require HTTPS; do not"
                " expose this instance directly (PLAN 3.2)",
                file=sys.stderr,
            )
        # HTTPS only: a self-signed certificate is generated on first
        # start. There is no HTTP listener — plain HTTP to the port is
        # refused by the TLS handshake, not redirected.
        cert_path, key_path = ensure_tls_material(args.root)
        print(
            f"tls: serving HTTPS with {cert_path} "
            f"(SHA-256 {cert_fingerprint(cert_path)})",
            file=sys.stderr,
        )
        app = create_app(
            AppConfig(
                root=args.root,
                secret=_web_secret(args.root),
                cookie_secure=not args.no_cookie_secure,
                transport=args.transport,
                package_dir=args.package_dir,
                bot_runtime=args.bot_runtime,
            )
        )
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            ssl_certfile=str(cert_path),
            ssl_keyfile=str(key_path),
        )
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
            supervisor.unregister(args.bot)
            runtime.store.remove_bot(args.bot)
            # Match the web delete path: the bot's secret artifacts and
            # webhook secret must not survive, or a later bot reusing
            # the name silently inherits them.
            from botmgr.envstore import SecretsService
            from botmgr.webhooks import WebhookSecrets

            SecretsService(args.root, runtime.store).delete(args.bot)
            WebhookSecrets(args.root).delete(args.bot)
            if args.purge_files:
                runtime.client.delete_bot_files(args.bot)
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
