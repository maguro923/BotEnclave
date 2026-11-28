"""Python interpreters a bot's venv can be built with.

A bot may pin a Python version (``3.11`` .. ``3.14``); deploys then
build its venv with that interpreter instead of the manager's own.
Installed interpreters are detected on the host (PATH, the usual
system locations, and the manager's own uv-managed directory under
the data root). A supported version that is not installed yet can be
installed on demand with uv into that managed directory, so a stored
pin always resolves to a real, detected executable — never to an
arbitrary path derived from the version string.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

_VERSION_RE = re.compile(r"^3\.(\d{1,2})$")
_CANDIDATE_RE = re.compile(r"^python3\.(\d{1,2})$")

# Versions the panel offers. Anything uv can fetch a standalone
# CPython build for; the manager itself needs 3.11+, but a bot's
# venv may legitimately target an older interpreter.
SUPPORTED_VERSIONS = ("3.9", "3.10", "3.11", "3.12", "3.13", "3.14")

_INSTALL_TIMEOUT_SECONDS = 600
_install_lock = threading.Lock()


class InstallError(Exception):
    """A requested interpreter could not be installed."""


@dataclass(frozen=True)
class Interpreter:
    version: str
    path: str


def default_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def is_supported(version: object) -> bool:
    return isinstance(version, str) and version in SUPPORTED_VERSIONS


def managed_dir(root: Path) -> Path:
    """Where uv-installed interpreters live (under the data root)."""
    return root / "pythons"


def _search_dirs() -> list[Path]:
    dirs: list[Path] = []
    for part in os.environ.get("PATH", "").split(os.pathsep):
        if part and Path(part) not in dirs:
            dirs.append(Path(part))
    for extra in ("/usr/local/bin", "/usr/bin", "/bin"):
        if Path(extra) not in dirs:
            dirs.append(Path(extra))
    return dirs


def _scan(directory: Path, found: dict[str, str]) -> None:
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        match = _CANDIDATE_RE.match(entry.name)
        if match is None:
            continue
        version = f"3.{match.group(1)}"
        if version in found:
            continue
        try:
            if entry.is_file() and os.access(entry, os.X_OK):
                found[version] = str(entry)
        except OSError:
            continue


def detect_interpreters(managed: Path | None = None) -> list[Interpreter]:
    """All usable python3.x interpreters, newest first.

    The manager's own interpreter always represents its version: a
    bot that asks for the default version must get the exact binary
    the manager itself runs on. uv-managed installs (one directory
    per build, ``bin/python3.x`` inside) are scanned when the caller
    passes the managed directory.
    """
    found: dict[str, str] = {default_version(): sys.executable}
    for directory in _search_dirs():
        _scan(directory, found)
    if managed is not None:
        try:
            builds = [p for p in managed.iterdir() if p.is_dir()]
        except OSError:
            builds = []
        for build in builds:
            _scan(build / "bin", found)
    ordered = sorted(found, key=lambda v: int(v.split(".")[1]), reverse=True)
    return [Interpreter(version=v, path=found[v]) for v in ordered]


def resolve_interpreter(version: str, managed: Path | None = None) -> str:
    """The executable for a pinned version, or ValueError."""
    if not _VERSION_RE.match(version):
        raise ValueError(f"invalid Python version: {version!r}")
    for interpreter in detect_interpreters(managed):
        if interpreter.version == version:
            return interpreter.path
    raise ValueError(f"Python {version} is not installed on this host")


def _uv_command() -> list[str] | None:
    """How to invoke uv, as an argv prefix.

    The manager is not always run by its own venv's interpreter:
    the systemd unit starts the system python with the venv's
    site-packages on PYTHONPATH, so the uv binary bundled in that
    venv must also be found via the package's own location, and as
    a last resort uv can run as a module of the current interpreter
    whenever the uv package is importable.
    """
    sibling = Path(sys.executable).with_name("uv")
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return [str(sibling)]
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyvenv.cfg").is_file():
            candidate = parent / "bin" / "uv"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return [str(candidate)]
            break
    found = shutil.which("uv")
    if found:
        return [found]
    if importlib.util.find_spec("uv") is not None:
        return [sys.executable, "-m", "uv"]
    return None


def install_python(version: str, root: Path) -> str:
    """Install a supported interpreter with uv; return its path.

    Serialized: two panel actions asking for interpreters at once
    share one uv run at a time. The child resets the umask the serve
    process tightened for its databases — installed interpreters must
    stay readable and executable by every bot user, whose venvs read
    the base interpreter's standard library at runtime.
    """
    if not is_supported(version):
        raise InstallError(f"unsupported Python version: {version!r}")
    target = managed_dir(root)
    try:
        return resolve_interpreter(version, target)
    except ValueError:
        pass
    uv = _uv_command()
    if uv is None:
        raise InstallError("uv is not available on this host")
    target.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(root),
        "UV_CACHE_DIR": str(root / ".cache" / "uv"),
        # Hermetic: uv otherwise discovers a uv.toml from the
        # working directory upward, which both surprises and can
        # fail outright when that tree is not readable by the
        # manager user.
        "UV_NO_CONFIG": "1",
    }
    with _install_lock:
        try:
            return resolve_interpreter(version, target)
        except ValueError:
            pass
        proc = subprocess.run(
            [*uv, "python", "install", version, "--install-dir", str(target)],
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT_SECONDS,
            env=env,
            cwd=str(root),
            preexec_fn=lambda: os.umask(0o022),
            check=False,
        )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise InstallError(
            f"installing Python {version} failed: {' '.join(tail)}"
        )
    try:
        return resolve_interpreter(version, target)
    except ValueError:
        raise InstallError(
            f"Python {version} installed but not detected under {target}"
        ) from None
