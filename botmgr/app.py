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
import json
import secrets as stdlib_secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from botmgr.audit import AuditLog
from botmgr.auth.deps import AuthBundle, build_auth, init_auth_db
from botmgr.auth.models import User
from botmgr.auth.routes import build_auth_router, setup_token_path
from botmgr.auth.service import AuthService
from botmgr.console import CaptureController
from botmgr.helper import HelperClient, Transport, direct_transport, sudo_transport
from botmgr.helper_backend import HelperBackend
from botmgr.helperd import BOT_NAME_RE
from botmgr.launcher import LaunchConfig
from botmgr.logtail import LogFollower, read_tail
from botmgr.reconcile import reconcile
from botmgr.store import StateStore, StoredBot
from botmgr.supervisor import BusyError, Supervisor
from botmgr.watchdog import Watchdog

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


def assert_bot_routes_guarded(app: FastAPI, guard: object) -> None:
    """Startup check: every route addressing a bot must depend on the
    guard, so a future route cannot forget the ownership check."""

    def walk(dependant: Any) -> list[object]:
        found: list[object] = []
        for dep in dependant.dependencies:
            found.append(dep.call)
            found.extend(walk(dep))
        return found

    for route in app.routes:
        path = getattr(route, "path", "")
        if "{bot_id}" in path:
            dependant = getattr(route, "dependant", None)
            calls = walk(dependant) if dependant is not None else []
            if guard not in calls:
                raise RuntimeError(
                    f"bot route without ownership guard: {path}"
                )


def create_app(config: AppConfig) -> FastAPI:
    root = config.root
    root.mkdir(parents=True, exist_ok=True)
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
    supervisor = Supervisor(HelperBackend(client), store=store)
    capture = CaptureController(client)
    watchdog = Watchdog(supervisor, client, capture, root)
    audit = AuditLog(root / "audit.log")
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

    app = FastAPI(title="BotEnclave", lifespan=lifespan)
    app.include_router(build_auth_router(bundle, service))
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
                    {"id": b.bot_id, "state": supervisor.state(b.bot_id).value}
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
    async def list_bots(user: User = Depends(current_user)) -> list[dict[str, str]]:  # type: ignore[arg-type]
        bots = visible_bots(user, store.load_all())
        return [
            {"id": b.bot_id, "state": supervisor.state(b.bot_id).value}
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
        if store.get_bot(name) is not None:
            raise HTTPException(status_code=409, detail="bot already exists")

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
            client.delete_user(bot)

        await run_in_threadpool(do_delete)
        audit.record(user.email, "bot.delete", bot)
        return {"status": "ok"}

    @app.post("/api/bots/{bot_id}/start")
    async def start_bot(
        user: User = Depends(current_user),  # type: ignore[arg-type]
        stored: StoredBot = Depends(bot_access),
    ) -> dict[str, str]:
        try:
            await run_in_threadpool(supervisor.start, stored.bot_id)
        except BusyError:
            raise HTTPException(status_code=409, detail="operation in progress") from None
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
        return {"lines": tail}

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
        websocket: WebSocket, user: User = Depends(current_user)  # type: ignore[arg-type]
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
    ) -> None:
        await websocket.accept()
        path = root / "bots" / stored.bot_id / "logs" / "console.log"
        follower = LogFollower(path)
        try:
            initial = await run_in_threadpool(read_tail, path, 200)
            await websocket.send_text(json.dumps({"lines": initial}))
            while True:
                lines = follower.poll()
                if lines:
                    await asyncio.wait_for(
                        websocket.send_text(json.dumps({"lines": lines})),
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

    assert_bot_routes_guarded(app, bot_access)
    app.state.audit = audit
    app.state.supervisor = supervisor
    app.state.store = store
    return app


async def _watchdog_loop(watchdog: Watchdog, audit: AuditLog) -> None:
    while True:
        events = await run_in_threadpool(watchdog.step)
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
