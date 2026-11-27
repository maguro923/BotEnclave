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
