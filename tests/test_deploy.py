"""Deploy pipeline tests (PLAN 2.6): zip end-to-end, GC, rollback,
hostile archives, URL/branch validation, and a git clone over a local
git-daemon (the API-layer validator rejects local paths, so the worker's
clone is exercised through the git:// transport instead).
"""

from __future__ import annotations

import io
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from botmgr.deploy import (
    DeployError,
    DeployService,
    validate_branch,
    validate_git_url,
)
from botmgr.helper import HelperClient
from botmgr.launcher import LaunchConfig
from botmgr.procident import current_boot_id
from botmgr.store import StateStore
from botmgr.supervisor import Supervisor

BOT_PY = "import time\nprint('hello from bot')\n"


def make_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


class FakeTransport:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._pid = 20000

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request["op"]
        bot = request.get("bot")
        if op == "ensure_layout":
            for sub in ("releases", "venvs"):
                (self.root / "bots" / str(bot) / sub).mkdir(
                    parents=True, exist_ok=True
                )
            return {"ok": True, "result": {"ready": True}}
        if op == "start_bot":
            self._pid += 1
            return {
                "ok": True,
                "result": {
                    "identity": {
                        "pid": self._pid,
                        "start_time": 99,
                        "boot_id": current_boot_id(),
                    }
                },
            }
        if op == "stop_bot":
            return {"ok": True, "result": {"exit_code": 0}}
        if op == "bot_status":
            return {"ok": True, "result": {"alive": True, "exit_code": None}}
        if op == "capture_state":
            return {"ok": True, "result": {"enabled": True}}
        if op == "set_capture":
            return {"ok": True, "result": {}}
        return {"ok": False, "error": "bad_op", "message": str(op)}


@pytest.fixture()
def service(tmp_path: Path) -> tuple[DeployService, StateStore, Supervisor, Path]:
    from botmgr.helper_backend import HelperBackend

    store = StateStore(tmp_path / "botmgr.db")
    client = HelperClient(FakeTransport(tmp_path))
    supervisor = Supervisor(HelperBackend(client), store=store)
    (tmp_path / "bots" / "alpha" / "data").mkdir(parents=True)
    supervisor.register(
        "alpha",
        LaunchConfig(("python3", "bot.py"), str(tmp_path / "bots" / "alpha" / "data"), {}),
    )
    return DeployService(tmp_path, store, supervisor, client), store, supervisor, tmp_path


def test_zip_deploy_pipeline_and_rollback(
    service: tuple[DeployService, StateStore, Supervisor, Path],
) -> None:
    deploy, store, _supervisor, root = service
    zip1 = make_zip(root / "v1.zip", {"bot.py": BOT_PY})
    result = deploy.deploy_zip("alpha", zip1, "v1.zip")
    bot_dir = root / "bots" / "alpha"
    assert (bot_dir / "current").resolve() == (
        bot_dir / "releases" / result.release_id
    ).resolve()
    active = store.active_release("alpha")
    assert active is not None and active.release_id == result.release_id
    config = store.get_bot("alpha").config  # type: ignore[union-attr]
    assert config.command[0].endswith(f"venvs/{result.release_id}/bin/python")
    assert config.command[1].endswith("current/bot.py")
    assert (bot_dir / "venvs" / result.release_id / "bin" / "python").exists()

    zip2 = make_zip(root / "v2.zip", {"bot.py": BOT_PY + "# v2\n"})
    second = deploy.deploy_zip("alpha", zip2, "v2.zip")
    assert second.release_id != result.release_id
    rolled = deploy.rollback("alpha")
    assert rolled.release_id == result.release_id
    assert store.active_release("alpha").release_id == result.release_id  # type: ignore[union-attr]


def test_gc_keeps_active_and_previous(
    service: tuple[DeployService, StateStore, Supervisor, Path],
) -> None:
    deploy, store, _supervisor, root = service
    ids = []
    for i in range(3):
        zip_path = make_zip(root / f"v{i}.zip", {"bot.py": BOT_PY})
        ids.append(deploy.deploy_zip("alpha", zip_path, f"v{i}.zip").release_id)
    records = store.list_releases("alpha")
    assert [r.release_id for r in records] == [ids[2], ids[1]]
    bot_dir = root / "bots" / "alpha"
    assert not (bot_dir / "releases" / ids[0]).exists()
    assert not (bot_dir / "venvs" / ids[0]).exists()
    assert (bot_dir / "releases" / ids[1]).exists()


def test_hostile_zips_rejected(
    service: tuple[DeployService, StateStore, Supervisor, Path],
) -> None:
    deploy, store, _supervisor, root = service
    slip = make_zip(root / "slip.zip", {"../evil.py": "x", "bot.py": BOT_PY})
    with pytest.raises(DeployError):
        deploy.deploy_zip("alpha", slip, "slip.zip")
    assert store.list_releases("alpha") == []
    assert not (root / "bots" / "alpha" / "evil.py").exists()
    assert not (root / "evil.py").exists()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("bot.py", BOT_PY)
        info = zipfile.ZipInfo("link.py")
        info.external_attr = 0o120777 << 16
        zf.writestr(info, "/etc/passwd")
    link_zip = root / "link.zip"
    link_zip.write_bytes(buffer.getvalue())
    with pytest.raises(DeployError):
        deploy.deploy_zip("alpha", link_zip, "link.zip")
    assert store.list_releases("alpha") == []


def test_validate_git_url_and_branch() -> None:
    assert validate_git_url("https://github.com/x/y.git")
    assert validate_git_url("https://user:tok@github.com/x/y.git")
    assert validate_git_url("ssh://git@github.com/x/y.git")
    assert validate_git_url("git@github.com:x/y.git")
    for bad in (
        "file:///etc/passwd",
        "/tmp/repo",
        "http://github.com/x/y.git",
        "-oProxyCommand=id",
        "",
        "https://exa mple.com/x.git",
    ):
        with pytest.raises(DeployError):
            validate_git_url(bad)
    assert validate_branch("main") == "main"
    assert validate_branch("feature/x-1.2") == "feature/x-1.2"
    assert validate_branch(None) is None
    for bad in ("-x", "a..b", "/abs", "trail/", "x.lock"):
        with pytest.raises(DeployError):
            validate_branch(bad)


def test_git_deploy_via_daemon(
    service: tuple[DeployService, StateStore, Supervisor, Path],
    tmp_path: Path,
) -> None:
    deploy, store, _supervisor, root = service
    src = tmp_path / "src"
    src.mkdir()
    (src / "bot.py").write_text(BOT_PY, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(src),
            "-c", "user.email=t@example.com", "-c", "user.name=t",
            "commit", "-qm", "init",
        ],
        check=True,
    )
    (src / ".git" / "git-daemon-export-ok").touch()
    daemon = subprocess.Popen(
        [
            "git", "daemon", "--export-all", f"--base-path={tmp_path}",
            "--port=19418", "--reuseaddr",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.7)
        # deploy_git itself would reject git:// at the API validator
        # (https/ssh only, PLAN 3.4); drive the worker path directly to
        # exercise clone + build + switch end to end.
        result = deploy._deploy(
            "alpha",
            {"kind": "git", "url": "git://127.0.0.1:19418/src", "branch": None},
            source_ref="git:HEAD",
            restart=True,
        )
    finally:
        daemon.terminate()
    active = store.active_release("alpha")
    assert active is not None and active.release_id == result.release_id
    deployed = (
        root / "bots" / "alpha" / "releases" / result.release_id / "bot.py"
    )
    assert deployed.read_text(encoding="utf-8") == BOT_PY


def test_publish_permissions_group_access(tmp_path: Path) -> None:
    import os
    import pwd
    from types import SimpleNamespace

    from botmgr.deploy import _publish_permissions

    base = tmp_path / "release"
    (base / "bin").mkdir(parents=True)
    script = base / "bin" / "python"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o700)
    plain = base / "bot.py"
    plain.write_text("x", encoding="utf-8")
    plain.chmod(0o600)
    real = pwd.getpwnam
    import botmgr.deploy as deploy_mod

    deploy_mod.pwd = SimpleNamespace(  # type: ignore[attr-defined]
        getpwnam=lambda name: SimpleNamespace(pw_gid=os.getgid())
    )
    try:
        _publish_permissions([base], "alpha")
    finally:
        deploy_mod.pwd = real  # type: ignore[attr-defined]
    assert base.stat().st_mode & 0o777 == 0o750
    assert script.stat().st_mode & 0o777 == 0o750  # executables stay executable
    assert plain.stat().st_mode & 0o777 == 0o640
