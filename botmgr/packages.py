"""Per-bot extra packages: panel-installed pip packages layered on top
of a release's requirements.txt.

Isolation model: a bot only ever runs from its own per-release venv
(``venvs/<release_id>``), and pip is only ever pointed at that one
venv's interpreter. An install for one bot therefore cannot affect
another bot, nor another release of the same bot (a rollback keeps the
older release's venv exactly as it was built).

Specs are deliberately conservative: a distribution name, optional
extras, and at most one version constraint. Anything that could be
read as a pip option (leading dashes, URLs, paths, multiple tokens)
is rejected, and pip is always invoked without a shell.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

_SPEC_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?P<extras>\[[A-Za-z0-9][A-Za-z0-9,._-]*\])?"
    r"(?P<constraint>\s*(===?|>=?|<=?|~=|!=)\s*[A-Za-z0-9][A-Za-z0-9._*!+-]*)?$"
)

MAX_SPEC_LENGTH = 128
PIP_TIMEOUT_SECONDS = 600


class PackageError(Exception):
    """Raised when a package spec is not acceptable."""


def validate_spec(spec: str) -> str:
    cleaned = spec.strip()
    if len(cleaned) > MAX_SPEC_LENGTH or _SPEC_RE.match(cleaned) is None:
        raise PackageError(f"invalid package spec: {spec!r}")
    return cleaned


def split_specs(text: str) -> list[str]:
    """Split a batch entry into individual specs.

    Separators are whitespace and commas (ASCII or fullwidth), so
    "requests, discord.py>=2 numpy" is three specs. A single spec
    never contains those characters (validate_spec rejects them), so
    splitting first and validating each part is unambiguous.
    """
    parts = re.split(r"[\s,、，]+", text.strip())
    return [part for part in parts if part]


def package_name(spec: str) -> str:
    match = _SPEC_RE.match(spec.strip())
    if match is None:
        raise PackageError(f"invalid package spec: {spec!r}")
    return match.group("name")


def _run_pip(
    venv_dir: Path, args: list[str], timeout: int = PIP_TIMEOUT_SECONDS
) -> tuple[bool, str]:
    python = venv_dir / "bin" / "python"
    if not python.exists():
        return False, "this bot has no virtual environment yet; deploy a release first"
    try:
        proc = subprocess.run(
            [str(python), "-m", "pip", *args, "--disable-pip-version-check"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"pip timed out after {timeout}s"
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output[-4000:]


def install(venv_dir: Path, spec: str) -> tuple[bool, str]:
    return _run_pip(venv_dir, ["install", validate_spec(spec)])


def uninstall(venv_dir: Path, spec: str) -> tuple[bool, str]:
    return _run_pip(venv_dir, ["uninstall", "-y", package_name(spec)])


def list_installed(venv_dir: Path) -> list[dict[str, str]]:
    python = venv_dir / "bin" / "python"
    if not python.exists():
        return []
    try:
        proc = subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "list",
                "--format=json",
                "--disable-pip-version-check",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return []
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [
        {"name": str(item.get("name", "")), "version": str(item.get("version", ""))}
        for item in data
        if isinstance(item, dict)
    ]
