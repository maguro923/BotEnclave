"""BotEnclave web application.

Assembles the auth package and the supervision core into the FastAPI app:
dashboard, bot detail (log view + interactive console), user management.
Every bot-addressed route goes through the single `bot_access` dependency,
which is the one place ownership is checked (PLAN sections 3.2 and 2.6);
a startup scan refuses to boot if a bot route lacks it.

Blocking work (helper subprocess calls, file tails) always runs in the
thread pool, never on the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import hmac
import json
import secrets as stdlib_secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from botmgr.audit import AuditLog, AuditMirror
from botmgr.auth.deps import AuthBundle, build_auth, init_auth_db
from botmgr.auth.models import User
from botmgr.auth.routes import build_auth_router, setup_token_path
from botmgr.auth.service import AuthService
from botmgr.console import CaptureController
from botmgr.deploy import DeployError, DeployService
from botmgr.envstore import SecretsError, SecretsService
from botmgr.helper import (
    HelperClient,
    HelperError,
    Transport,
    direct_transport,
    sudo_transport,
)
from botmgr.helper_backend import HelperBackend
from botmgr.helperd import BOT_NAME_RE
from botmgr.launcher import LaunchConfig
from botmgr.logtail import LogFollower, read_tail
from botmgr.masking import mask_lines
from botmgr.reconcile import reconcile
from botmgr.secrets import decrypt_text, encrypt_text, load_or_create_fernet
from botmgr.store import StateStore, StoredBot
from botmgr.supervisor import BusyError, SecretPendingError, Supervisor
from botmgr.watchdog import Watchdog
from botmgr.webhooks import WebhookSecrets

CSRF_COOKIE = "botenclave_csrf"
CSRF_HEADER = "x-csrf-token"
AUTH_COOKIE = "botenclave_auth"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_WEBHOOK_BYTES = 1024 * 1024

BASE_DIR = Path(__file__).parent
WATCHDOG_INTERVAL_SECONDS = 5.0


@dataclass
class AppConfig:
    root: Path
    secret: str
    cookie_secure: bool = True
    transport: str = "direct"
    helper_entry: str = "/usr/local/lib/botenclave/helper"
    package_dir: Path | None = None
    transport_override: Transport | None = field(default=None, repr=False)
    # Guest quotas (PLAN 3.2); admins are exempt.
    guest_max_bots: int = 5
    guest_max_disk_bytes: int = 1 << 30
    audit_retention_days: int = 180
    bot_runtime: str = "helper"


def authorize(user: User, stored: StoredBot | None) -> StoredBot:
    """The single ownership check. Guests touching another user's bot get
    a 404, so the response never confirms the bot exists."""
    if stored is None:
        raise HTTPException(status_code=404, detail="no such bot")
    if user.is_superuser or stored.owner_id == user.id:
        return stored
    raise HTTPException(status_code=404, detail="no such bot")


def visible_bots(user: User, bots: list[StoredBot]) -> list[StoredBot]:
    if user.is_superuser:
        return bots
    return [b for b in bots if b.owner_id == user.id]


def assert_bot_routes_guarded(
    app: FastAPI, guard: object, exempt_paths: frozenset[str] = frozenset()
) -> None:
    """Startup check: every route addressing a bot must depend on the
    guard, so a future route cannot forget the ownership check. Exempt
    paths (the HMAC-signed git webhook) carry their own authentication
    and are listed explicitly by the caller."""

    def walk(dependant: Any) -> list[object]:
        found: list[object] = []
        for dep in dependant.dependencies:
            found.append(dep.call)
            found.extend(walk(dep))
        return found

    for route in app.routes:
        path = getattr(route, "path", "")
        if "{bot_id}" in path and path not in exempt_paths:
            dependant = getattr(route, "dependant", None)
            calls = walk(dependant) if dependant is not None else []
            if guard not in calls:
                raise RuntimeError(
                    f"bot route without ownership guard: {path}"
                )


def create_app(config: AppConfig) -> FastAPI:
    root = config.root
    root.mkdir(parents=True, exist_ok=True)
    # Bot users must be able to traverse the deployment root to reach
    # their own folders (the same reason bots/ itself is 0711, PLAN 2.4);
    # a private umask would otherwise make every start fail.
    root.chmod(0o711)
    bundle: AuthBundle = build_auth(root, config.secret, config.cookie_secure)
    service = AuthService(bundle)
    store = StateStore(root / "botmgr.db")
    if config.transport_override is not None:
        transport = config.transport_override
    elif config.transport == "sudo":
        transport = sudo_transport(config.helper_entry, root, config.package_dir)
    else:
        transport = direct_transport(root, config.package_dir)
    client = HelperClient(transport)
    secrets_service = SecretsService(root, store)
    supervisor = Supervisor(
        HelperBackend(client, via_unit=config.bot_runtime == "systemd"),
        store=store,
        env_provider=secrets_service.resolve,
    )
    capture = CaptureController(client)
    usage: dict[str, int] = {}
    watchdog = Watchdog(
        supervisor,
        client,
        capture,
        root,
        usage_sink=lambda bot, n: usage.__setitem__(bot, n),
    )
    audit = AuditLog(root / "audit.log", mirror=AuditMirror(root / "botmgr.db"))
    deploy_service = DeployService(root, store, supervisor, client)
    webhook_secrets = WebhookSecrets(root)
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    current_user = bundle.current_user
    current_user_optional = bundle.fastapi_users.current_user(
        optional=True, active=True
    )

    async def bot_access(
        bot_id: str, user: User = Depends(current_user)  # type: ignore[arg-type]
    ) -> StoredBot:
        stored = await run_in_threadpool(store.get_bot, bot_id)
        return authorize(user, stored)

    def guest_quota_error(user: User, *, adding_bot: bool = False) -> str | None:
        """Guest quotas (PLAN 3.2): bot count and total disk usage.
        Admins are exempt; memory/RSS stay advisory by design."""
        if user.is_superuser:
            return None
        owned = [b for b in store.load_all() if b.owner_id == user.id]
        if adding_bot and len(owned) >= config.guest_max_bots:
            return f"bot quota reached ({config.guest_max_bots} bots)"
        total = sum(usage.get(b.bot_id, 0) for b in owned)
        if total >= config.guest_max_disk_bytes:
            return "disk quota exceeded"
        return None

    def mask_for(bot: str) -> list[str]:
        try:
            resolution = secrets_service.resolve(bot)
        except SecretsError:
            return []
        if not resolution.env:
            return []
        return list(resolution.env.values())

    def visible_audit(user: User) -> list[dict[str, object]]:
        entries = audit.read_all()
        if user.is_superuser:
            return entries
        own = {b.bot_id for b in store.load_all() if b.owner_id == user.id}
        return [e for e in entries if e.get("target") in own]

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        lock_fd = open(root / "manager.lock", "a+")  # noqa: SIM115, ASYNC230
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError(
                "another BotEnclave manager is already running for this root"
            ) from None
        await init_auth_db(bundle)
        if config.audit_retention_days > 0:
            dropped = await run_in_threadpool(
                audit.purge,
                time.time() - config.audit_retention_days * 86400,
            )
            if dropped:
                audit.record("system", "audit.purge", "", f"dropped={dropped}")
        report = await run_in_threadpool(reconcile, supervisor, store, client, capture)
        for bot in report.adopted_running:
            audit.record("system", "reconcile.adopted", bot)
        for bot in report.found_dead:
            audit.record("system", "reconcile.found_dead", bot)
        if await service.count_users() == 0:
            token_path = setup_token_path(root)
            if not token_path.exists():
                token_path.write_text(
                    stdlib_secrets.token_urlsafe(24), encoding="ascii"
                )
                token_path.chmod(0o600)
            print(
                "[botenclave] first-run setup: open /setup and use the token "
                f"stored in {token_path}",
                flush=True,
            )
        task = asyncio.create_task(_watchdog_loop(watchdog, audit))
        app.state.lock_fd = lock_fd
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            store.close()
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    app = FastAPI(title="BotEnclave", lifespan=lifespan)

    @app.middleware("http")
    async def csrf_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        # Double-submit cookie on every mutating API call (PLAN 2.5).
        # Login is excluded (no session exists yet; SameSite=Lax plus the
        # JSON content type cover it), as are signed webhooks.
        path = request.url.path
        mutating = request.method in ("POST", "PUT", "PATCH", "DELETE")
        protected = path.startswith(("/api/", "/admin/", "/auth/")) and path != (
            "/auth/login"
        )
        if mutating and protected:
            cookie = request.cookies.get(CSRF_COOKIE)
            header = request.headers.get(CSRF_HEADER)
            if (
                not cookie
                or not header
                or not hmac.compare_digest(cookie, header)
            ):
                return JSONResponse(
                    {"detail": "CSRF token missing or invalid"}, status_code=403
                )
        response = await call_next(request)
        set_cookie = response.headers.get("set-cookie", "")
        if (AUTH_COOKIE in request.cookies or AUTH_COOKIE in set_cookie) and (
            CSRF_COOKIE not in request.cookies
        ):
            response.set_cookie(
                CSRF_COOKIE,
                stdlib_secrets.token_urlsafe(24),
                httponly=False,
                samesite="lax",
                secure=config.cookie_secure,
                path="/",
            )
        return response

    async def check_origin(websocket: WebSocket) -> None:
        # CSWSH guard (PLAN 2.5): a present Origin must match the Host.
        origin = websocket.headers.get("origin")
        if origin and urlparse(origin).netloc != websocket.headers.get("host", ""):
            raise HTTPException(status_code=403, detail="origin mismatch")

    app.include_router(build_auth_router(bundle, service, audit_sink=audit.record))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        # Minimal unauthenticated liveness signal (PLAN 5.2).
        return {"status": "ok"}
    app.mount(
        "/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static"
    )

    # -- pages ---------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html", {})

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "setup.html", {})

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(
        request: Request,
        user: User | None = Depends(current_user_optional),
    ) -> Response:
        if user is None:
            if await service.count_users() == 0:
                return RedirectResponse("/setup", status_code=303)
            return RedirectResponse("/login", status_code=303)
        bots = visible_bots(user, store.load_all())
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "user": user,
                "bots": [
                    {
                        "id": b.bot_id,
                        "state": supervisor.state(b.bot_id).value,
                        "secret_backend": b.secret_backend,
                        "dotenv_mode": b.dotenv_mode,
                    }
                    for b in bots
                ],
            },
        )

    @app.get("/bots/{bot_id}", response_class=HTMLResponse)
    async def bot_detail(
        request: Request,
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "bot_detail.html",
            {
                "user": user,
                "bot": stored.bot_id,
                "state": supervisor.state(stored.bot_id).value,
            },
        )

    @app.get("/users", response_class=HTMLResponse)
    async def users_page(
        request: Request, admin: User = Depends(bundle.current_admin)  # type: ignore[arg-type]
    ) -> HTMLResponse:
        users = await service.list_users()
        return templates.TemplateResponse(
            request, "users.html", {"user": admin, "users": users}
        )

    @app.get("/account", response_class=HTMLResponse)
    async def account_page(
        request: Request, user: User = Depends(current_user)  # type: ignore[arg-type]
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "account.html", {"user": user}
        )

    # -- bot API ---------------------------------------------------------------
    @app.get("/api/bots")
    async def list_bots(user: User = Depends(current_user)) -> list[dict[str, object]]:  # type: ignore[arg-type]
        bots = visible_bots(user, store.load_all())
        return [
            {
                "id": b.bot_id,
                "state": supervisor.state(b.bot_id).value,
                "secret_backend": b.secret_backend,
                "dotenv_mode": b.dotenv_mode,
                "disk_bytes": usage.get(b.bot_id),
            }
            for b in bots
        ]

    @app.post("/api/bots")
    async def create_bot(
        body: dict[str, Any], user: User = Depends(current_user)  # type: ignore[arg-type]
    ) -> dict[str, str]:
        name = body.get("name")
        command = body.get("command")
        if not isinstance(name, str) or not BOT_NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="invalid bot name")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) for part in command)
        ):
            raise HTTPException(status_code=400, detail="command must be a non-empty list")
        existing = store.get_bot(name)
        if existing is not None:
            if user.is_superuser or existing.owner_id == user.id:
                raise HTTPException(status_code=409, detail="bot already exists")
            # Another user's bot: answer exactly like a validation
            # failure so the name's existence is not confirmed.
            raise HTTPException(status_code=400, detail="invalid bot name")
        quota = guest_quota_error(user, adding_bot=True)
        if quota is not None:
            raise HTTPException(status_code=403, detail=quota)

        def do_create() -> None:
            client.create_user(name)
            supervisor.register(
                name,
                LaunchConfig(
                    command=tuple(command),
                    cwd=str(root / "bots" / name / "data"),
                    env={},
                ),
                owner_id=user.id,
            )

        await run_in_threadpool(do_create)
        audit.record(user.email, "bot.create", name)
        return {"id": name, "state": "stopped"}

    @app.delete("/api/bots/{bot_id}")
    async def delete_bot(
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, str]:
        bot = stored.bot_id

        def do_delete() -> None:
            if supervisor.state(bot).value not in ("stopped", "attention"):
                with contextlib.suppress(Exception):
                    supervisor.stop(bot)
            store.remove_bot(bot)
            secrets_service.delete(bot)
            webhook_secrets.delete(bot)
            client.delete_user(bot)

        await run_in_threadpool(do_delete)
        audit.record(user.email, "bot.delete", bot)
        return {"status": "ok"}

    @app.post("/api/bots/{bot_id}/start")
    async def start_bot(
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, str]:
        quota = guest_quota_error(user)
        if quota is not None:
            raise HTTPException(status_code=403, detail=quota)
        try:
            await run_in_threadpool(supervisor.start, stored.bot_id)
        except BusyError:
            raise HTTPException(status_code=409, detail="operation in progress") from None
        except SecretPendingError:
            audit.record(user.email, "bot.start.secret_pending", stored.bot_id)
            return {"state": "secret-pending"}
        audit.record(user.email, "bot.start", stored.bot_id)
        return {"state": supervisor.state(stored.bot_id).value}

    @app.post("/api/bots/{bot_id}/stop")
    async def stop_bot(
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, str]:
        try:
            await run_in_threadpool(supervisor.stop, stored.bot_id)
        except BusyError:
            raise HTTPException(status_code=409, detail="operation in progress") from None
        audit.record(user.email, "bot.stop", stored.bot_id)
        return {"state": supervisor.state(stored.bot_id).value}

    @app.get("/api/bots/{bot_id}/log")
    async def bot_log(
        lines: int = 200,
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, object]:
        path = root / "bots" / stored.bot_id / "logs" / "console.log"
        tail = await run_in_threadpool(read_tail, path, max(1, min(lines, 2000)))
        return {"lines": mask_lines(tail, mask_for(stored.bot_id))}

    # -- environment / secrets (values are never re-displayed, PLAN 3.3) -------
    @app.get("/api/bots/{bot_id}/env")
    async def get_env(
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, object]:
        return {
            "backend": stored.secret_backend,
            "keys": secrets_service.keys(stored.bot_id),
            "dotenv_mode": stored.dotenv_mode,
        }

    @app.put("/api/bots/{bot_id}/env")
    async def put_env(
        body: dict[str, Any],
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, object]:
        bot = stored.bot_id
        env = body.get("env")
        backend = body.get("backend", stored.secret_backend)
        if (
            not isinstance(env, dict)
            or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in env.items()
            )
            or not isinstance(backend, str)
        ):
            raise HTTPException(status_code=400, detail="env must be an object of strings")
        warnings: list[str] = []
        token = env.get("DISCORD_TOKEN")
        bot_identity: dict[str, str] | None = None
        if token:
            digest = hashlib.sha256(token.encode()).hexdigest()
            other = store.token_hash_owner(digest)
            if other is not None and other != bot:
                warnings.append(
                    f"the same DISCORD_TOKEN is already registered to bot '{other}'"
                )
            bot_identity = await _verify_discord_token(token)
            if bot_identity is None:
                warnings.append("DISCORD_TOKEN could not be verified with Discord")
        try:
            await run_in_threadpool(secrets_service.set_env, bot, env, backend)
        except SecretsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        audit.record(
            user.email,
            "env.update",
            bot,
            f"backend={backend} keys={len(env)}",
        )
        return {"status": "ok", "warnings": warnings, "bot_identity": bot_identity}

    @app.post("/api/bots/{bot_id}/dotenv-mode")
    async def set_dotenv_mode(
        body: dict[str, Any],
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, object]:
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled must be a boolean")
        store.set_dotenv_mode(stored.bot_id, enabled)
        audit.record(
            user.email, "env.dotenv_mode", stored.bot_id, f"enabled={enabled}"
        )
        return {"dotenv_mode": enabled}

    # -- deploy / releases -------------------------------------------------------
    @app.post("/api/bots/{bot_id}/deploy/git")
    async def deploy_git(
        body: dict[str, Any],
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, object]:
        quota = guest_quota_error(user)
        if quota is not None:
            raise HTTPException(status_code=403, detail=quota)
        bot = stored.bot_id
        url = body.get("url")
        branch = body.get("branch")
        if not isinstance(url, str) or not (
            branch is None or isinstance(branch, str)
        ):
            raise HTTPException(status_code=400, detail="url and branch required")
        fernet = load_or_create_fernet(root)
        url_enc = encrypt_text(fernet, url)
        try:
            result = await run_in_threadpool(
                deploy_service.deploy_git,
                bot,
                url,
                branch,
                url_enc=url_enc,
            )
        except DeployError as exc:
            audit.record(user.email, "deploy.git.failed", bot, _safe_detail(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from None
        audit.record(user.email, "deploy.git", bot, f"release={result.release_id}")
        return {
            "release_id": result.release_id,
            "entry": result.entry,
            "state": result.state,
        }

    @app.post("/api/bots/{bot_id}/deploy/upload")
    async def deploy_upload(
        file: UploadFile = File(...),
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, object]:
        quota = guest_quota_error(user)
        if quota is not None:
            raise HTTPException(status_code=403, detail=quota)
        bot = stored.bot_id
        filename = Path(file.filename or "upload.zip").name
        tmp_dir = root / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / f"upload-{stdlib_secrets.token_hex(6)}.zip"
        data = bytearray()
        while chunk := await file.read(65536):
            data += chunk
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="upload too large")
        await run_in_threadpool(tmp_path.write_bytes, bytes(data))
        try:
            result = await run_in_threadpool(
                deploy_service.deploy_zip, bot, tmp_path, filename
            )
        except DeployError as exc:
            audit.record(user.email, "deploy.upload.failed", bot, _safe_detail(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from None
        finally:
            tmp_path.unlink(missing_ok=True)
        audit.record(user.email, "deploy.upload", bot, f"release={result.release_id}")
        return {
            "release_id": result.release_id,
            "entry": result.entry,
            "state": result.state,
        }

    @app.get("/api/bots/{bot_id}/releases")
    async def list_releases(
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> list[dict[str, object]]:
        return [
            {
                "release_id": r.release_id,
                "source_ref": r.source_ref,
                "created_at": r.created_at,
                "active": r.active,
            }
            for r in deploy_service.releases(stored.bot_id)
        ]

    @app.get("/api/bots/{bot_id}/releases/{release_id}/log")
    async def release_log(
        release_id: str,
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, str]:
        known = {r.release_id for r in deploy_service.releases(stored.bot_id)}
        if release_id not in known:
            raise HTTPException(status_code=404, detail="no such release")
        log = await run_in_threadpool(
            deploy_service.deploy_log, stored.bot_id, release_id
        )
        masked = mask_lines(log.splitlines(), mask_for(stored.bot_id))
        return {"log": "\n".join(masked)}

    @app.post("/api/bots/{bot_id}/rollback")
    async def rollback(
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, object]:
        quota = guest_quota_error(user)
        if quota is not None:
            raise HTTPException(status_code=403, detail=quota)
        bot = stored.bot_id
        try:
            result = await run_in_threadpool(deploy_service.rollback, bot)
        except DeployError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        audit.record(user.email, "deploy.rollback", bot, f"release={result.release_id}")
        return {
            "release_id": result.release_id,
            "entry": result.entry,
            "state": result.state,
        }

    # -- webhook secret ------------------------------------------------------------
    @app.get("/api/bots/{bot_id}/webhook")
    async def webhook_status(
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, object]:
        return {"configured": webhook_secrets.has_secret(stored.bot_id)}

    @app.post("/api/bots/{bot_id}/webhook-secret")
    async def webhook_rotate(
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, str]:
        secret = webhook_secrets.rotate(stored.bot_id)
        audit.record(user.email, "webhook.rotate", stored.bot_id)
        return {"secret": secret}

    # -- file management (PLAN 2.8) --------------------------------------------------
    def _file_error(exc: HelperError) -> HTTPException:
        status = {
            "bad_path": 400,
            "bad_zone": 400,
            "read_only": 400,
            "symlink": 400,
            "escape": 400,
            "not_dir": 404,
            "not_file": 404,
            "missing": 404,
            "too_large": 413,
            "exists": 409,
            "is_dir": 400,
        }.get(exc.code, 400)
        return HTTPException(status_code=status, detail=exc.message)

    @app.get("/api/bots/{bot_id}/files")
    async def files_list(
        zone: str = "data",
        path: str = "",
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, object]:
        try:
            entries = await run_in_threadpool(
                client.file_list, stored.bot_id, zone, path
            )
        except HelperError as exc:
            raise _file_error(exc) from None
        return {"entries": entries}

    @app.get("/api/bots/{bot_id}/files/content")
    async def files_read(
        zone: str = "data",
        path: str = "",
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, object]:
        try:
            return await run_in_threadpool(
                client.file_read, stored.bot_id, zone, path
            )
        except HelperError as exc:
            raise _file_error(exc) from None

    @app.get("/api/bots/{bot_id}/files/download")
    async def files_download(
        zone: str = "data",
        path: str = "",
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> Response:
        import base64
        from urllib.parse import quote

        try:
            result = await run_in_threadpool(
                client.file_read, stored.bot_id, zone, path
            )
        except HelperError as exc:
            raise _file_error(exc) from None
        if result.get("kind") == "text":
            data = str(result["content"]).encode()
        else:
            data = base64.b64decode(str(result["content_b64"]))
        name = Path(path).name or "download"
        # RFC 5987: a raw non-ASCII name cannot be encoded into the
        # header (Starlette raises) — send an ASCII fallback plus the
        # percent-encoded UTF-8 form.
        fallback = (
            name.encode("ascii", "ignore").decode().replace('"', "_").replace("\\", "_")
            or "download"
        )
        disposition = (
            f'attachment; filename="{fallback}"; '
            f"filename*=UTF-8''{quote(name)}"
        )
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": disposition},
        )

    @app.put("/api/bots/{bot_id}/files/content")
    async def files_write(
        body: dict[str, Any],
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, str]:
        zone = body.get("zone", "data")
        path = body.get("path")
        content = body.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            raise HTTPException(status_code=400, detail="path and content required")
        try:
            await run_in_threadpool(
                client.file_write, stored.bot_id, zone, path, content
            )
        except HelperError as exc:
            raise _file_error(exc) from None
        audit.record(user.email, "file.write", stored.bot_id, f"{zone}:{path}")
        return {"status": "ok"}

    @app.post("/api/bots/{bot_id}/files/mkdir")
    async def files_mkdir(
        body: dict[str, Any],
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, str]:
        zone = body.get("zone", "data")
        try:
            await run_in_threadpool(
                client.file_mkdir,
                stored.bot_id,
                zone,
                body.get("path", ""),
            )
        except HelperError as exc:
            raise _file_error(exc) from None
        audit.record(
            user.email, "file.mkdir", stored.bot_id, f"{zone}:{body.get('path', '')}"
        )
        return {"status": "ok"}

    @app.delete("/api/bots/{bot_id}/files")
    async def files_delete(
        zone: str = "data",
        path: str = "",
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, str]:
        try:
            await run_in_threadpool(
                client.file_delete, stored.bot_id, zone, path
            )
        except HelperError as exc:
            raise _file_error(exc) from None
        audit.record(user.email, "file.delete", stored.bot_id, f"{zone}:{path}")
        return {"status": "ok"}

    @app.post("/api/bots/{bot_id}/files/rename")
    async def files_rename(
        body: dict[str, Any],
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, str]:
        try:
            await run_in_threadpool(
                client.file_rename,
                stored.bot_id,
                body.get("zone", "data"),
                body.get("path", ""),
                body.get("to", ""),
            )
        except HelperError as exc:
            raise _file_error(exc) from None
        audit.record(
            user.email,
            "file.rename",
            stored.bot_id,
            f"{body.get('path', '')} -> {body.get('to', '')}",
        )
        return {"status": "ok"}

    # -- git webhook (signed, per-bot secret; PLAN 2.5) --------------------------------
    @app.post("/webhooks/git/{bot_id}")
    async def git_webhook(bot_id: str, request: Request) -> Response:
        body = await request.body()
        if len(body) > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="payload too large")
        verified = webhook_secrets.verify(
            bot_id,
            request.headers.get("x-botenclave-timestamp"),
            request.headers.get("x-botenclave-signature"),
            body,
        )
        stored = store.get_bot(bot_id)
        if not verified or stored is None:
            raise HTTPException(status_code=401, detail="signature verification failed")
        source = store.get_source(bot_id)
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        repo = payload.get("repo") if isinstance(payload, dict) else None
        branch = payload.get("branch") if isinstance(payload, dict) else None
        url: str | None = None
        if source is not None and source[0] == "git" and source[1]:
            try:
                url = decrypt_text(load_or_create_fernet(root), source[1])
            except Exception:  # noqa: BLE001
                url = None
        branch_ok = source is not None and (
            source[2] is None or branch is None or branch == source[2]
        )
        if url is None or repo != url or not branch_ok:
            audit.record("webhook", "webhook.rejected", bot_id, "repo/branch mismatch")
            raise HTTPException(status_code=400, detail="repo/branch mismatch")
        audit.record("webhook", "webhook.accepted", bot_id)

        def run_deploy() -> None:
            try:
                result = deploy_service.deploy_git(
                    bot_id,
                    url,
                    source[2] if source else None,
                    url_enc=source[1] if source else None,
                )
                audit.record(
                    "webhook", "deploy.git", bot_id, f"release={result.release_id}"
                )
            except Exception as exc:  # noqa: BLE001 - background: record, never raise
                audit.record(
                    "webhook", "deploy.git.failed", bot_id, _safe_detail(exc)
                )

        threading.Thread(target=run_deploy, daemon=True).start()
        return JSONResponse({"status": "accepted"}, status_code=202)

    # -- audit viewing (admin: all; guests: their own bots only, PLAN 3.6) -------
    @app.get("/api/audit")
    async def audit_list(
        limit: int = 200, user: User = Depends(current_user)  # type: ignore[arg-type]
    ) -> list[dict[str, object]]:
        entries = visible_audit(user)
        return entries[-max(1, min(limit, 1000)) :][::-1]

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_page(
        request: Request, user: User = Depends(current_user)  # type: ignore[arg-type]
    ) -> HTMLResponse:
        entries = visible_audit(user)[-200:][::-1]
        return templates.TemplateResponse(
            request, "audit.html", {"user": user, "entries": entries}
        )

    # -- admin: user deletion with bot transfer --------------------------------
    @app.delete("/admin/users/{user_id}")
    async def delete_user(
        user_id: int,
        mode: str = "transfer",
        admin: User = Depends(bundle.current_admin),  # type: ignore[arg-type]
    ) -> dict[str, str]:
        target = await service.get_user(user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="no such user")
        if target.is_superuser and await service.admin_count() <= 1:
            raise HTTPException(status_code=400, detail="cannot delete the last admin")
        owned = [b for b in store.load_all() if b.owner_id == user_id]
        if mode == "transfer":
            for bot in owned:
                store.set_owner(bot.bot_id, admin.id)
        elif mode == "delete":
            for bot in owned:

                def do_delete(bot_id: str = bot.bot_id) -> None:
                    if supervisor.state(bot_id).value not in ("stopped", "attention"):
                        with contextlib.suppress(Exception):
                            supervisor.stop(bot_id)
                    store.remove_bot(bot_id)
                    secrets_service.delete(bot_id)
                    webhook_secrets.delete(bot_id)
                    client.delete_user(bot_id)

                await run_in_threadpool(do_delete)
        else:
            raise HTTPException(status_code=400, detail="mode must be transfer or delete")
        await service.delete_user(user_id)
        audit.record(admin.email, "user.delete", target.email, f"mode={mode}")
        return {"status": "ok"}

    # -- websockets ---------------------------------------------------------------
    @app.websocket("/ws/overview")
    async def overview_ws(
        websocket: WebSocket,
        user: User = Depends(current_user),  # type: ignore[arg-type]
        _origin: None = Depends(check_origin),
    ) -> None:
        await websocket.accept()
        try:
            while True:
                fresh = await service.get_user(user.id)
                if fresh is None or not fresh.is_active:
                    await websocket.close(code=4401)
                    return
                bots = visible_bots(user, store.load_all())
                payload = [
                    {"id": b.bot_id, "state": supervisor.state(b.bot_id).value}
                    for b in bots
                ]
                await websocket.send_text(json.dumps(payload))
                await asyncio.sleep(2)
        except WebSocketDisconnect:
            return

    @app.websocket("/ws/bots/{bot_id}/log")
    async def log_ws(
        websocket: WebSocket,
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
        _origin: None = Depends(check_origin),
    ) -> None:
        await websocket.accept()
        path = root / "bots" / stored.bot_id / "logs" / "console.log"
        follower = LogFollower(path)
        values = mask_for(stored.bot_id)
        try:
            initial = await run_in_threadpool(read_tail, path, 200)
            await websocket.send_text(
                json.dumps({"lines": mask_lines(initial, values)})
            )
            while True:
                lines = follower.poll()
                if lines:
                    await asyncio.wait_for(
                        websocket.send_text(
                            json.dumps({"lines": mask_lines(lines, values)})
                        ),
                        timeout=10,
                    )
                await asyncio.sleep(0.4)
        except (WebSocketDisconnect, TimeoutError):
            return
        finally:
            follower.close()

    @app.websocket("/ws/bots/{bot_id}/console")
    async def console_ws(
        websocket: WebSocket,
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
        _origin: None = Depends(check_origin),
    ) -> None:
        bot = stored.bot_id
        await websocket.accept()
        audit.record(user.email, "console.connect", bot)
        await run_in_threadpool(capture.acquire_private, bot)
        try:
            snapshot = await run_in_threadpool(client.console_snapshot, bot)
            await websocket.send_text(json.dumps({"type": "screen", "data": snapshot}))
            sender = asyncio.create_task(_console_sender(websocket, client, bot))
            try:
                while True:
                    message = await websocket.receive_text()
                    await _handle_console_message(message, client, bot)
            except WebSocketDisconnect:
                pass
            finally:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sender
        finally:
            # Cleanup must survive client-disconnect cancellation: shield
            # each step so one cancelled await cannot skip the next step.
            for step in (
                lambda: capture.release_private(bot),
                lambda: client.console_clear_history(bot),
            ):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(run_in_threadpool(step))
            audit.record(user.email, "console.disconnect", bot)

    assert_bot_routes_guarded(
        app, bot_access, exempt_paths=frozenset({"/webhooks/git/{bot_id}"})
    )
    app.state.audit = audit
    app.state.supervisor = supervisor
    app.state.store = store
    app.state.secrets = secrets_service
    app.state.deploy = deploy_service
    app.state.webhook_secrets = webhook_secrets
    return app


def _safe_detail(exc: Exception) -> str:
    """Error text for audit details: no credential-bearing URLs, capped."""
    import re

    text = re.sub(r"(https?://)[^/@\s]+@", r"\1", str(exc))
    return text[:200]


async def _verify_discord_token(token: str) -> dict[str, str] | None:
    """Best-effort identity check against the Discord API (PLAN 3.3).
    Any failure (network, bad token) returns None and becomes a warning,
    never a hard error: the panel must not depend on Discord being up."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": f"Bot {token}"},
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return {"username": str(data.get("username", "")), "id": str(data.get("id", ""))}
    except Exception:  # noqa: BLE001
        return None


async def _watchdog_loop(watchdog: Watchdog, audit: AuditLog) -> None:
    while True:
        try:
            events = await run_in_threadpool(watchdog.step)
        except Exception:  # noqa: BLE001 - supervision must never die silently
            events = ["watchdog step failed"]
        for event in events:
            audit.record("system", "watchdog", "", event)
        await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)


async def _console_sender(websocket: WebSocket, client: HelperClient, bot: str) -> None:
    last: str | None = None
    while True:
        snapshot = await run_in_threadpool(client.console_snapshot, bot)
        if snapshot != last:
            last = snapshot
            await websocket.send_text(json.dumps({"type": "screen", "data": snapshot}))
        await asyncio.sleep(0.25)


async def _handle_console_message(
    message: str, client: HelperClient, bot: str
) -> None:
    try:
        payload = json.loads(message)
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    kind = payload.get("type")
    if kind == "input":
        data = payload.get("data")
        if isinstance(data, str) and data:
            if data in ("\r", "\n"):
                await run_in_threadpool(client.console_send, bot, "", True)
            else:
                await run_in_threadpool(client.console_send, bot, data, False)
    elif kind == "resize":
        cols, rows = payload.get("cols"), payload.get("rows")
        if isinstance(cols, int) and isinstance(rows, int):
            await run_in_threadpool(client.console_resize, bot, cols, rows)
