"""Unit tests for the per-bot extra-packages module."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from botmgr import packages


@pytest.mark.parametrize(
    "spec",
    [
        "requests",
        "discord.py",
        "discord.py>=2.3",
        "requests==2.32.3",
        "uvicorn[standard]",
        "pydantic~=2.9",
        "package-name!=1.0",
        "a",
    ],
)
def test_validate_spec_accepts(spec: str) -> None:
    assert packages.validate_spec(spec) == spec.strip()


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "   ",
        "-e git+https://example.com/repo",
        "--index-url https://evil.example",
        "requests flask",
        "requests; rm -rf /",
        "requests && curl evil",
        "https://example.com/pkg.tar.gz",
        "/local/path/pkg",
        "requests>=",
        "x" * 200,
    ],
)
def test_validate_spec_rejects(spec: str) -> None:
    with pytest.raises(packages.PackageError):
        packages.validate_spec(spec)


def test_package_name() -> None:
    assert packages.package_name("discord.py>=2.3") == "discord.py"
    assert packages.package_name("uvicorn[standard]") == "uvicorn"
    with pytest.raises(packages.PackageError):
        packages.package_name("--bad")


def test_install_routes_to_the_venv_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").touch()
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(packages.subprocess, "run", fake_run)
    ok, output = packages.install(venv, "requests>=2")
    assert ok and output == "ok"
    assert calls[0][0] == str(venv / "bin" / "python")
    assert calls[0][3:] == ["install", "requests>=2", "--disable-pip-version-check"]
    ok, _ = packages.uninstall(venv, "requests>=2")
    assert ok
    assert calls[1][3:] == ["uninstall", "-y", "requests", "--disable-pip-version-check"]


def test_install_without_venv_reports_clearly(tmp_path: Path) -> None:
    ok, output = packages.install(tmp_path / "nope", "requests")
    assert not ok
    assert "no virtual environment" in output
    assert packages.list_installed(tmp_path / "nope") == []


def test_list_installed_reads_pip_list(tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        check=True,
        capture_output=True,
        timeout=300,
    )
    installed = packages.list_installed(venv)
    names = {entry["name"].lower() for entry in installed}
    assert "pip" in names
    assert all(entry["version"] for entry in installed)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("requests", ["requests"]),
        ("requests, discord.py>=2.3", ["requests", "discord.py>=2.3"]),
        ("requests discord.py", ["requests", "discord.py"]),
        ("requests、numpy， scipy", ["requests", "numpy", "scipy"]),
        ("  requests ,, flask  ", ["requests", "flask"]),
        ("", []),
    ],
)
def test_split_specs(text: str, expected: list[str]) -> None:
    assert packages.split_specs(text) == expected
    for part in expected:
        assert packages.validate_spec(part) == part


def test_pip_children_never_inherit_the_manager_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the manager runs with PYTHONPATH=<its own
    site-packages>. A pip child inheriting it sees the manager's
    packages as installed in the bot's venv — `pip list` reports them
    and `pip install` skips requirements they satisfy (discord.py's
    idna was skipped exactly this way), leaving the venv incomplete.
    """
    import subprocess as sp

    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").touch()
    seen: list[dict[str, str] | None] = []

    def fake_run(argv: list[str], **kwargs: object) -> sp.CompletedProcess[str]:
        env = kwargs.get("env")
        seen.append(env if isinstance(env, dict) else None)
        out = "[]" if "list" in argv else ""
        return sp.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(packages.subprocess, "run", fake_run)
    monkeypatch.setenv("PYTHONPATH", "/manager/site-packages")
    monkeypatch.setenv("PYTHONHOME", "/manager")
    assert packages.install(venv, "requests")[0] is True
    assert packages.list_installed(venv) == []
    assert len(seen) == 2
    for env in seen:
        assert env is not None
        assert "PYTHONPATH" not in env
        assert "PYTHONHOME" not in env


def test_run_pip_child_resets_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pip runs from the manager process, which serves with a
    private umask; without a reset in the child, packages installed
    into a bot's live venv would be created 0600 and the bot could
    not read them."""
    import os

    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("", encoding="ascii")
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=args[0] if args else [], returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(packages.subprocess, "run", fake_run)
    ok, _output = packages.install(venv, "requests")
    assert ok
    preexec = captured["preexec_fn"]
    assert callable(preexec)
    previous = os.umask(0o077)
    try:
        preexec()  # type: ignore[operator]
        current = os.umask(0o022)
        os.umask(current)
    finally:
        os.umask(previous)
    assert current == 0o022
