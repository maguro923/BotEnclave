"""Interpreter detection and version resolution (botmgr.pythons)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from botmgr.pythons import (
    default_version,
    detect_interpreters,
    resolve_interpreter,
)


def _fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, executable in (
        ("python3.97", True),
        ("python3.98", True),
        ("python3.96", False),  # present but not executable: unusable
        ("python3", True),  # unversioned: not a pinnable candidate
        ("python2.7", True),
        ("python3.x", True),
    ):
        path = bin_dir / name
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        if executable:
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def test_detect_includes_manager_and_fake_interpreters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_bin(tmp_path, monkeypatch)
    found = {i.version: i.path for i in detect_interpreters()}
    assert found[default_version()]  # the manager's own interpreter
    assert found["3.97"].endswith("python3.97")
    assert found["3.98"].endswith("python3.98")
    assert "3.96" not in found  # not executable
    assert "2.7" not in found
    versions = [i.version for i in detect_interpreters()]
    assert versions == sorted(
        versions, key=lambda v: int(v.split(".")[1]), reverse=True
    )


def test_resolve_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_bin(tmp_path, monkeypatch)
    assert resolve_interpreter("3.97").endswith("python3.97")
    assert resolve_interpreter(default_version())
    with pytest.raises(ValueError, match="not installed"):
        resolve_interpreter("3.99")
    for bad in ("3", "4.0", "3.13.1", "../bin/sh", "3.13; rm -rf /", ""):
        with pytest.raises(ValueError, match="invalid"):
            resolve_interpreter(bad)


def test_managed_dir_interpreters_are_detected(tmp_path: Path) -> None:
    from botmgr.pythons import managed_dir

    root = tmp_path / "root"
    build = managed_dir(root) / "cpython-3.97.1-linux-x86_64-gnu" / "bin"
    build.mkdir(parents=True)
    exe = build / "python3.97"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)

    found = {i.version: i.path for i in detect_interpreters(managed_dir(root))}
    assert found["3.97"] == str(exe)
    assert resolve_interpreter("3.97", managed_dir(root)) == str(exe)
    with pytest.raises(ValueError, match="not installed"):
        resolve_interpreter("3.97")  # invisible without the managed dir


def _fake_uv(tmp_path: Path, fail: bool = False) -> Path:
    script = tmp_path / "uv"
    if fail:
        body = "#!/bin/sh\necho 'network is down' >&2\nexit 1\n"
    else:
        body = (
            "#!/bin/sh\n"
            'ver="$3"\n'
            'dir="$5"\n'
            'mkdir -p "$dir/cpython-$ver.0/bin"\n'
            'printf \'#!/bin/sh\\n\' > "$dir/cpython-$ver.0/bin/python$ver"\n'
            'chmod +x "$dir/cpython-$ver.0/bin/python$ver"\n'
        )
    script.write_text(body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_install_python_uses_uv_into_the_managed_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from botmgr import pythons

    uv = _fake_uv(tmp_path)
    monkeypatch.setattr(pythons, "_uv_command", lambda: [str(uv)])
    root = tmp_path / "root"
    # 3.10 is supported and not installed on the test host, so the
    # install path is exercised end to end against the fake uv.
    path = pythons.install_python("3.10", root)
    assert path.endswith("python3.10")
    assert Path(path).is_file()
    # Already installed: a second call resolves without uv.
    monkeypatch.setattr(
        pythons, "_uv_command", lambda: (_ for _ in ()).throw(AssertionError)
    )
    assert pythons.install_python("3.10", root) == path


def test_install_python_runs_uv_hermetically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess as subprocess_mod

    from botmgr import pythons

    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess_mod.CompletedProcess[str]:
        seen["argv"] = argv
        seen.update(kwargs)
        target = Path(argv[argv.index("--install-dir") + 1])
        exe = target / "cpython-3.10.0" / "bin" / "python3.10"
        exe.parent.mkdir(parents=True)
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        return subprocess_mod.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pythons, "_uv_command", lambda: ["/fake/uv"])
    monkeypatch.setattr(pythons.subprocess, "run", fake_run)
    root = tmp_path / "root"
    assert pythons.install_python("3.10", root).endswith("python3.10")
    assert seen["cwd"] == str(root)
    env = seen["env"]
    assert isinstance(env, dict) and env.get("UV_NO_CONFIG") == "1"
    cache = env.get("UV_CACHE_DIR")
    assert isinstance(cache, str) and cache.endswith(f"uv-{os.geteuid()}")


def test_install_python_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from botmgr import pythons

    with pytest.raises(pythons.InstallError, match="unsupported"):
        pythons.install_python("3.99", tmp_path)
    with pytest.raises(pythons.InstallError, match="uv is not available"):
        monkeypatch.setattr(pythons, "_uv_command", lambda: None)
        pythons.install_python("3.10", tmp_path)
    uv = _fake_uv(tmp_path, fail=True)
    monkeypatch.setattr(pythons, "_uv_command", lambda: [str(uv)])
    with pytest.raises(pythons.InstallError, match="network is down"):
        pythons.install_python("3.10", tmp_path)


def test_uv_command_finds_the_venv_the_package_lives_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from botmgr import pythons

    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("", encoding="utf-8")
    uv = venv / "bin" / "uv"
    uv.write_text("#!/bin/sh\n", encoding="utf-8")
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    pkg = venv / "lib" / "python3.99" / "site-packages" / "botmgr"
    pkg.mkdir(parents=True)
    fake_file = pkg / "pythons.py"
    fake_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(pythons, "__file__", str(fake_file))
    # The systemd unit's shape: the system interpreter runs the
    # manager, so there is no uv next to sys.executable.
    monkeypatch.setattr(pythons.sys, "executable", "/usr/bin/python3")
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    assert pythons._uv_command() == [str(uv)]


def test_uv_command_module_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util

    from botmgr import pythons

    monkeypatch.setattr(pythons, "__file__", str(tmp_path / "pythons.py"))
    monkeypatch.setattr(pythons.sys, "executable", "/usr/bin/python3")
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: object() if name == "uv" else None
    )
    assert pythons._uv_command() == ["/usr/bin/python3", "-m", "uv"]
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert pythons._uv_command() is None


def test_manager_interpreter_wins_for_its_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    own = bin_dir / f"python{default_version()}"
    own.write_text("#!/bin/sh\n", encoding="utf-8")
    own.chmod(own.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))
    found = {i.version: i.path for i in detect_interpreters()}
    assert found[default_version()] == sys.executable
    assert os.path.isabs(found[default_version()])
