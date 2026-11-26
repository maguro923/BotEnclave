"""Confined file operations for one bot's tree (PLAN sections 2.8, 3.4).

Used by the privileged helper's executor (and by tests through the same
functions). The rules that keep a bot — or a caller tricking the manager —
from escaping the bot's own tree:

- Zones: ``data`` (read/write), ``logs`` and ``releases`` (read-only).
  ``venvs`` and anything else is not even listed.
- Relative paths only; absolute paths, NUL bytes and ``..`` components
  are rejected at the door.
- Symlinks are never followed: any existing component of the path that is
  a symlink makes the operation fail. Links may appear in listings,
  flagged as such, but cannot be opened, edited, moved or deleted.
- Files are opened with O_NOFOLLOW and the opened fd's real path is
  re-checked against the zone root (narrows the validate/use window).
- Writes are atomic (temp file in the same directory + rename) and, for
  edits, limited to 1 MiB of UTF-8 text. Reads are capped at 8 MiB.
"""

from __future__ import annotations

import base64
import os
import secrets as stdlib_secrets
from dataclasses import dataclass
from pathlib import Path

ZONES_READABLE = ("data", "logs", "releases")
ZONES_WRITABLE = ("data",)
MAX_EDIT_BYTES = 1_048_576
MAX_READ_BYTES = 8_388_608
MAX_PATH_CHARS = 512


class FileOpError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FileEntry:
    name: str
    kind: str  # file | dir | symlink | other
    size: int | None

    def to_json(self) -> dict[str, object]:
        return {"name": self.name, "kind": self.kind, "size": self.size}


def _zone_base(bot_root: Path, zone: str) -> Path:
    if zone not in ZONES_READABLE:
        raise FileOpError("bad_zone", f"zone is not accessible: {zone!r}")
    return bot_root / zone


def _check_writable(zone: str) -> None:
    if zone not in ZONES_WRITABLE:
        raise FileOpError("read_only", f"zone {zone} is read-only")


def _split(rel: str) -> list[str]:
    if not isinstance(rel, str) or "\x00" in rel or len(rel) > MAX_PATH_CHARS:
        raise FileOpError("bad_path", "invalid path")
    if rel.startswith("/"):
        raise FileOpError("bad_path", "absolute paths are not allowed")
    parts: list[str] = []
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise FileOpError("bad_path", "parent references are not allowed")
        parts.append(part)
    return parts


def _resolve(bot_root: Path, zone: str, rel: str) -> Path:
    """Resolve rel inside the zone, refusing symlinks on the way."""
    base = _zone_base(bot_root, zone)
    parts = _split(rel)
    current = base
    for part in parts:
        current = current / part
        if os.path.lexists(current) and current.is_symlink():
            raise FileOpError("symlink", f"refusing to follow symlink: {rel}")
    base_real = Path(os.path.realpath(base))
    real = Path(os.path.realpath(current))
    if real != base_real and base_real not in real.parents:
        raise FileOpError("escape", f"path escapes its zone: {rel}")
    return current


def _open_checked(path: Path, zone_base_real: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW)
    real = Path(os.path.realpath(f"/proc/self/fd/{fd}"))
    if real != zone_base_real and zone_base_real not in real.parents:
        os.close(fd)
        raise FileOpError("escape", "opened file is outside its zone")
    return fd


def list_dir(bot_root: Path, zone: str, rel: str) -> list[FileEntry]:
    target = _resolve(bot_root, zone, rel)
    if not target.is_dir():
        raise FileOpError("not_dir", f"not a directory: {rel}")
    entries: list[FileEntry] = []
    for child in sorted(target.iterdir(), key=lambda p: p.name):
        if child.is_symlink():
            kind = "symlink"
        elif child.is_dir():
            kind = "dir"
        elif child.is_file():
            kind = "file"
        else:
            kind = "other"
        size = None
        if kind == "file":
            size = child.stat().st_size
        entries.append(FileEntry(name=child.name, kind=kind, size=size))
    return entries


def read_file(bot_root: Path, zone: str, rel: str) -> dict[str, object]:
    if not _split(rel):
        raise FileOpError("bad_path", "a file path is required")
    target = _resolve(bot_root, zone, rel)
    if not target.is_file():
        raise FileOpError("not_file", f"not a file: {rel}")
    base_real = Path(os.path.realpath(_zone_base(bot_root, zone)))
    fd = _open_checked(target, base_real, os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size
        if size > MAX_READ_BYTES:
            raise FileOpError("too_large", "file exceeds the read limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_READ_BYTES:
                # The file grew after the size check (a running bot
                # appending to it); the cap applies to what is read,
                # not only to what fstat reported.
                raise FileOpError("too_large", "file exceeds the read limit")
            chunks.append(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    try:
        return {"kind": "text", "content": data.decode("utf-8"), "size": len(data)}
    except UnicodeDecodeError:
        return {
            "kind": "binary",
            "content_b64": base64.b64encode(data).decode(),
            "size": len(data),
        }


def write_file(bot_root: Path, zone: str, rel: str, content: str) -> None:
    _check_writable(zone)
    if not _split(rel):
        raise FileOpError("bad_path", "a file path is required")
    data = content.encode("utf-8")
    if len(data) > MAX_EDIT_BYTES:
        raise FileOpError("too_large", "file exceeds the edit limit (1 MiB)")
    target = _resolve(bot_root, zone, rel)
    if target.is_dir():
        raise FileOpError("is_dir", f"is a directory: {rel}")
    parent = target.parent
    if not parent.is_dir():
        raise FileOpError("not_dir", "parent directory does not exist")
    tmp = parent / f".{target.name}.tmp-{stdlib_secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)


def make_dir(bot_root: Path, zone: str, rel: str) -> None:
    _check_writable(zone)
    if not _split(rel):
        raise FileOpError("bad_path", "a directory path is required")
    target = _resolve(bot_root, zone, rel)
    if os.path.lexists(target):
        raise FileOpError("exists", f"already exists: {rel}")
    if not target.parent.is_dir():
        raise FileOpError("not_dir", "parent directory does not exist")
    target.mkdir()


def delete_path(bot_root: Path, zone: str, rel: str) -> None:
    _check_writable(zone)
    if not _split(rel):
        raise FileOpError("bad_path", "a path is required")
    target = _resolve(bot_root, zone, rel)
    if not os.path.lexists(target):
        raise FileOpError("missing", f"no such path: {rel}")
    if target.is_dir():
        target.rmdir()  # refuses non-empty directories
    else:
        target.unlink()


def rename_path(bot_root: Path, zone: str, from_rel: str, to_rel: str) -> None:
    _check_writable(zone)
    if not _split(from_rel) or not _split(to_rel):
        raise FileOpError("bad_path", "both paths are required")
    source = _resolve(bot_root, zone, from_rel)
    target = _resolve(bot_root, zone, to_rel)
    if not os.path.lexists(source):
        raise FileOpError("missing", f"no such path: {from_rel}")
    if os.path.lexists(target):
        raise FileOpError("exists", f"already exists: {to_rel}")
    if not target.parent.is_dir():
        raise FileOpError("not_dir", "target parent does not exist")
    os.rename(source, target)
