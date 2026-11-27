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
        self.published: list[tuple[str, str]] = []

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request["op"]
        bot = request.get("bot")
        if op == "ensure_layout":
            for sub in ("releases", "venvs"):
                (self.root / "bots" / str(bot) / sub).mkdir(
                    parents=True, exist_ok=True
                )
            return {"ok": True, "result": {"ready": True}}
        if op == "publish_release":
            self.published.append((str(bot), str(request["release_id"])))
            return {"ok": True, "result": {"published": request["release_id"]}}
        if op == "point_current":
            import os

            bot_dir = self.root / "bots" / str(bot)
            tmp_link = bot_dir / ".current.tmp-test"
            os.symlink(f"releases/{request['release_id']}", tmp_link)
            os.replace(tmp_link, bot_dir / "current")
            return {"ok": True, "result": {"current": request["release_id"]}}
        if op == "edit_discard":
            import shutil

            shutil.rmtree(self.root / "bots" / str(bot) / "edit", ignore_errors=True)
            return {"ok": True, "result": {"discarded": bot}}
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


def _patch_helperd_gid(monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    from types import SimpleNamespace

    import botmgr.helperd as helperd_mod

    monkeypatch.setattr(
        helperd_mod.pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_gid=os.getgid()),
    )


def _release_tree(root: Path, bot: str, release_id: str) -> tuple[Path, Path]:
    release = root / "bots" / bot / "releases" / release_id
    venv = root / "bots" / bot / "venvs" / release_id
    (release / "bin").mkdir(parents=True)
    (venv / "bin").mkdir(parents=True)
    return release, venv


def test_publish_release_group_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from botmgr.helperd import SystemExecutor

    release_id = "20261127T000000-ab12"
    release, venv = _release_tree(tmp_path, "alpha", release_id)
    script = release / "bin" / "python"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o700)
    plain = release / "bot.py"
    plain.write_text("x", encoding="utf-8")
    plain.chmod(0o600)
    venv_lib = venv / "lib.py"
    venv_lib.write_text("x", encoding="utf-8")
    venv_lib.chmod(0o644)
    _patch_helperd_gid(monkeypatch)
    SystemExecutor(tmp_path).publish_release("alpha", release_id)
    assert release.stat().st_mode & 0o777 == 0o750
    assert script.stat().st_mode & 0o777 == 0o750  # executables stay executable
    assert plain.stat().st_mode & 0o777 == 0o640
    assert venv_lib.stat().st_mode & 0o777 == 0o640


def test_publish_release_never_follows_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from botmgr.helperd import SystemExecutor

    release_id = "20261127T000000-ab12"
    release, _venv = _release_tree(tmp_path, "alpha", release_id)
    outside = tmp_path / "interpreter"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    outside.chmod(0o644)
    (release / "bin" / "python").symlink_to(outside)  # like a venv's python
    _patch_helperd_gid(monkeypatch)
    SystemExecutor(tmp_path).publish_release("alpha", release_id)
    assert outside.stat().st_mode & 0o777 == 0o644


def test_deploy_publishes_via_helper(
    service: tuple[DeployService, StateStore, Supervisor, Path],
) -> None:
    deploy, _store, _supervisor, root = service
    zip_path = make_zip(root / "pub.zip", {"bot.py": BOT_PY})
    result = deploy.deploy_zip("alpha", zip_path, "pub.zip")
    transport = deploy._client._transport  # type: ignore[attr-defined]
    assert transport.published == [("alpha", result.release_id)]


def test_fetch_git_rewrites_credentialed_remote(tmp_path: Path) -> None:
    import io

    import botmgr.deploy as deploy_mod

    calls: list[list[str]] = []

    def fake_run_logged(argv: list[str], log: object, timeout: int, **kw: object) -> None:
        calls.append(argv)

    original = deploy_mod._run_logged
    deploy_mod._run_logged = fake_run_logged  # type: ignore[assignment]
    try:
        deploy_mod.fetch_git(
            "https://user:SECRET@github.com/owner/repo.git",
            None,
            tmp_path / "dest",
            io.StringIO(),
        )
    finally:
        deploy_mod._run_logged = original  # type: ignore[assignment]
    set_url = [c for c in calls if "set-url" in c]
    assert len(set_url) == 1
    assert set_url[0][-1] == "https://github.com/owner/repo.git"
    assert all("SECRET" not in part for c in set_url for part in c)


def test_activate_failure_restores_previous_release(
    service: tuple[DeployService, StateStore, Supervisor, Path],
    tmp_path: Path,
) -> None:
    deploy, store, supervisor, root = service
    zip1 = make_zip(tmp_path / "v1.zip", {"bot.py": BOT_PY})
    first = deploy.deploy_zip("alpha", zip1, "v1.zip")
    assert store.active_release("alpha").release_id == first.release_id  # type: ignore[union-attr]
    supervisor.start("alpha")  # the bot is running when v2 arrives

    def boom(bot: str) -> None:
        raise RuntimeError("spawn exploded")

    supervisor.start = boom  # type: ignore[method-assign]
    zip2 = make_zip(tmp_path / "v2.zip", {"bot.py": BOT_PY + "# v2\n"})
    with pytest.raises(DeployError, match="previous release restored"):
        deploy.deploy_zip("alpha", zip2, "v2.zip")
    active = store.active_release("alpha")
    assert active is not None and active.release_id == first.release_id
    current = root / "bots" / "alpha" / "current"
    assert current.resolve().name == first.release_id


def test_worker_timeout_kills_tree_and_cleans_up(
    service: tuple[DeployService, StateStore, Supervisor, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json as json_mod
    import subprocess as sp

    import botmgr.deploy as deploy_mod

    deploy, _store, _supervisor, root = service
    sleeper = sp.Popen(["sleep", "60"], start_new_session=True)

    class FakePopen:
        pid = sleeper.pid

        def __init__(self, argv: list[str], **kw: object) -> None:
            self._job = json_mod.loads(Path(argv[-1]).read_text(encoding="utf-8"))

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            bot_dir = (
                Path(str(self._job["root"])) / "bots" / str(self._job["bot"])
            )
            rid = str(self._job["release_id"])
            (bot_dir / "releases" / rid).mkdir(parents=True, exist_ok=True)
            (bot_dir / "venvs" / rid).mkdir(parents=True, exist_ok=True)
            raise sp.TimeoutExpired(cmd="worker", timeout=timeout or 0)

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(deploy_mod.subprocess, "Popen", FakePopen)
    zip_path = make_zip(tmp_path / "slow.zip", {"bot.py": BOT_PY})
    try:
        with pytest.raises(DeployError, match="timed out"):
            deploy.deploy_zip("alpha", zip_path, "slow.zip")
        assert sleeper.poll() is not None  # the whole tree was killed
        bot_dir = root / "bots" / "alpha"
        assert list((bot_dir / "releases").iterdir()) == []
        assert list((bot_dir / "venvs").iterdir()) == []
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


def test_stale_job_files_are_swept_on_deploy(
    service: tuple[DeployService, StateStore, Supervisor, Path],
    tmp_path: Path,
) -> None:
    import os

    deploy, _store, _supervisor, root = service
    tmp_dir = root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    stale = tmp_dir / "stale.json"
    stale.write_text("{}", encoding="utf-8")
    old = time.time() - 100_000
    os.utime(stale, (old, old))
    fresh = tmp_dir / "fresh.json"
    fresh.write_text("{}", encoding="utf-8")
    zip_path = make_zip(tmp_path / "ok.zip", {"bot.py": BOT_PY})
    deploy.deploy_zip("alpha", zip_path, "ok.zip")
    assert not stale.exists()  # a dead manager's job file (with credentials)
    assert fresh.exists()


def test_deploy_edit_commit_creates_new_release_and_keeps_rollback(
    service: tuple[DeployService, StateStore, Supervisor, Path],
) -> None:
    import shutil

    deploy, store, _supervisor, root = service
    zip1 = make_zip(root / "v1.zip", {"bot.py": BOT_PY})
    first = deploy.deploy_zip("alpha", zip1, "v1.zip")
    bot_dir = root / "bots" / "alpha"
    # Stage an edit of the active release, as the helper's edit_begin
    # would, changing two files before the commit.
    edit_dir = bot_dir / "edit"
    shutil.copytree(bot_dir / "releases" / first.release_id, edit_dir)
    (edit_dir / "bot.py").write_text(BOT_PY + "# edited\n", encoding="utf-8")
    (edit_dir / "config.yml").write_text("key: value\n", encoding="utf-8")
    store.start_edit_session("alpha", first.release_id)
    result = deploy.deploy_edit("alpha")
    assert result.release_id != first.release_id
    new_tree = bot_dir / "releases" / result.release_id
    assert (new_tree / "config.yml").read_text(encoding="utf-8") == "key: value\n"
    assert "# edited" in (new_tree / "bot.py").read_text(encoding="utf-8")
    active = store.active_release("alpha")
    assert active is not None and active.release_id == result.release_id
    assert store.edit_session_base("alpha") is None
    assert not edit_dir.exists()
    # The pre-edit release survived untouched: rollback returns to it.
    back = deploy.rollback("alpha")
    assert back.release_id == first.release_id


def test_deploy_edit_refuses_a_stale_base(
    service: tuple[DeployService, StateStore, Supervisor, Path],
) -> None:
    import shutil

    deploy, store, _supervisor, root = service
    zip1 = make_zip(root / "v1.zip", {"bot.py": BOT_PY})
    first = deploy.deploy_zip("alpha", zip1, "v1.zip")
    bot_dir = root / "bots" / "alpha"
    edit_dir = bot_dir / "edit"
    shutil.copytree(bot_dir / "releases" / first.release_id, edit_dir)
    # The session claims a base that is not the active release.
    store.start_edit_session("alpha", "20260101T000000-dead")
    with pytest.raises(DeployError):
        deploy.deploy_edit("alpha")
    # A refused commit keeps the session and its files for a retry.
    assert store.edit_session_base("alpha") == "20260101T000000-dead"
    assert edit_dir.exists()


def test_run_logged_children_do_not_inherit_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deploy worker's children (venv creation, pip installs of
    requirements.txt) must not inherit the manager's PYTHONPATH, or
    pip skips requirements the manager's own environment satisfies."""
    import io

    import botmgr.deploy as deploy_mod

    seen: dict[str, object] = {}

    class _Proc:
        returncode = 0

    def fake_run(argv: list[str], **kwargs: object) -> _Proc:
        seen.update(kwargs)
        return _Proc()

    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)
    monkeypatch.setenv("PYTHONPATH", "/manager/site-packages")
    deploy_mod._run_logged(["true"], io.StringIO(), timeout=10)
    env = seen.get("env")
    assert isinstance(env, dict)
    assert "PYTHONPATH" not in env


def test_deploy_edit_from_scratch_creates_the_first_release(
    service: tuple[DeployService, StateStore, Supervisor, Path],
) -> None:
    deploy, store, _supervisor, root = service
    bot_dir = root / "bots" / "alpha"
    edit_dir = bot_dir / "edit"
    edit_dir.mkdir(parents=True)
    (edit_dir / "bot.py").write_text(BOT_PY, encoding="utf-8")
    store.start_edit_session("alpha", "")
    result = deploy.deploy_edit("alpha")
    active = store.active_release("alpha")
    assert active is not None and active.release_id == result.release_id
    assert (bot_dir / "releases" / result.release_id / "bot.py").exists()
    assert store.edit_session_base("alpha") is None
    assert not edit_dir.exists()


def test_deploy_edit_scratch_refuses_when_a_release_appeared(
    service: tuple[DeployService, StateStore, Supervisor, Path],
) -> None:
    deploy, store, _supervisor, root = service
    bot_dir = root / "bots" / "alpha"
    edit_dir = bot_dir / "edit"
    edit_dir.mkdir(parents=True)
    (edit_dir / "bot.py").write_text(BOT_PY, encoding="utf-8")
    store.start_edit_session("alpha", "")
    zip1 = make_zip(root / "v1.zip", {"bot.py": BOT_PY})
    deploy.deploy_zip("alpha", zip1, "v1.zip")
    with pytest.raises(DeployError, match="since editing began"):
        deploy.deploy_edit("alpha")
    # The refused commit keeps the session and its files.
    assert store.edit_session_base("alpha") == ""
    assert (edit_dir / "bot.py").exists()
