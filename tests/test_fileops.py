"""Path-confinement suite for the file manager (PLAN 2.8 / 3.4).

The fixture tree includes escape attempts a hostile bot could plant:
symlinks inside data/ pointing outside, and a symlink in place of a file.
Every one must be refused, while normal operations keep working.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from botmgr import fileops
from botmgr.fileops import FileOpError


@pytest.fixture()
def bot_root(tmp_path: Path) -> Path:
    root = tmp_path / "bots" / "alpha"
    (root / "data" / "sub").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "releases" / "r1").mkdir(parents=True)
    (root / "venvs" / "r1").mkdir(parents=True)
    (root / "data" / "config.json").write_text('{"a": 1}', encoding="utf-8")
    (root / "data" / "sub" / "note.txt").write_text("hello", encoding="utf-8")
    (root / "logs" / "console.log").write_text("log line", encoding="utf-8")
    (root / "releases" / "r1" / "bot.py").write_text("print(1)", encoding="utf-8")
    # Escape attempts planted by the "bot".
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret", encoding="utf-8")
    (root / "data" / "escape_dir").symlink_to(outside, target_is_directory=True)
    (root / "data" / "escape_file").symlink_to(outside / "secret.txt")
    (root / "data" / "sub" / "escape_up").symlink_to(
        tmp_path / "bots", target_is_directory=True
    )
    return root


def test_list_and_read_happy_path(bot_root: Path) -> None:
    entries = fileops.list_dir(bot_root, "data", "")
    names = {e.name: e for e in entries}
    assert names["config.json"].kind == "file"
    assert names["escape_dir"].kind == "symlink"
    result = fileops.read_file(bot_root, "data", "sub/note.txt")
    assert result == {"kind": "text", "content": "hello", "size": 5}
    result = fileops.read_file(bot_root, "logs", "console.log")
    assert result["content"] == "log line"


def test_traversal_rejected(bot_root: Path) -> None:
    for bad in ("../outside/secret.txt", "sub/../../outside", "/etc/passwd", ".."):
        with pytest.raises(FileOpError):
            fileops.read_file(bot_root, "data", bad)
        with pytest.raises(FileOpError):
            fileops.list_dir(bot_root, "data", bad)


def test_symlinks_never_followed(bot_root: Path) -> None:
    with pytest.raises(FileOpError) as exc:
        fileops.read_file(bot_root, "data", "escape_file")
    assert exc.value.code == "symlink"
    with pytest.raises(FileOpError):
        fileops.read_file(bot_root, "data", "escape_dir/secret.txt")
    with pytest.raises(FileOpError):
        fileops.list_dir(bot_root, "data", "escape_dir")
    with pytest.raises(FileOpError):
        fileops.read_file(bot_root, "data", "sub/escape_up/alpha/data/config.json")
    # Writing through a symlinked directory is refused too.
    with pytest.raises(FileOpError):
        fileops.write_file(bot_root, "data", "escape_dir/planted.txt", "x")
    with pytest.raises(FileOpError):
        fileops.delete_path(bot_root, "data", "escape_file")


def test_zone_rules(bot_root: Path) -> None:
    # venvs is not a zone at all.
    with pytest.raises(FileOpError):
        fileops.list_dir(bot_root, "venvs", "")
    # logs and releases are read-only.
    assert fileops.read_file(bot_root, "releases", "r1/bot.py")["kind"] == "text"
    with pytest.raises(FileOpError) as exc:
        fileops.write_file(bot_root, "releases", "r1/bot.py", "hacked")
    assert exc.value.code == "read_only"
    with pytest.raises(FileOpError):
        fileops.write_file(bot_root, "logs", "console.log", "x")
    with pytest.raises(FileOpError):
        fileops.delete_path(bot_root, "logs", "console.log")
    with pytest.raises(FileOpError):
        fileops.rename_path(bot_root, "releases", "r1/bot.py", "r1/x.py")
    # The edit staging zone is readable and writable like data.
    (bot_root / "edit").mkdir(exist_ok=True)
    fileops.write_file(bot_root, "edit", "bot.py", "edited")
    assert fileops.read_file(bot_root, "edit", "bot.py")["content"] == "edited"


def test_write_is_atomic_and_bounded(bot_root: Path) -> None:
    fileops.write_file(bot_root, "data", "new.txt", "content")
    assert (bot_root / "data" / "new.txt").read_text(encoding="utf-8") == "content"
    # No temp files are left behind.
    leftovers = [p.name for p in (bot_root / "data").iterdir() if ".tmp-" in p.name]
    assert leftovers == []
    fileops.write_file(bot_root, "data", "config.json", "{}")
    assert (bot_root / "data" / "config.json").read_text(encoding="utf-8") == "{}"
    with pytest.raises(FileOpError) as exc:
        fileops.write_file(bot_root, "data", "big.txt", "x" * (fileops.MAX_EDIT_BYTES + 1))
    assert exc.value.code == "too_large"
    # Writing into a missing directory fails instead of creating a path.
    with pytest.raises(FileOpError):
        fileops.write_file(bot_root, "data", "nope/x.txt", "x")


def test_mkdir_delete_rename(bot_root: Path) -> None:
    fileops.make_dir(bot_root, "data", "made")
    assert (bot_root / "data" / "made").is_dir()
    with pytest.raises(FileOpError):
        fileops.make_dir(bot_root, "data", "made")
    fileops.rename_path(bot_root, "data", "sub/note.txt", "sub/renamed.txt")
    assert (bot_root / "data" / "sub" / "renamed.txt").exists()
    with pytest.raises(FileOpError):
        fileops.rename_path(bot_root, "data", "sub/renamed.txt", "config.json")
    fileops.delete_path(bot_root, "data", "sub/renamed.txt")
    assert not (bot_root / "data" / "sub" / "renamed.txt").exists()
    # Non-empty directories cannot be deleted.
    with pytest.raises(OSError):
        fileops.delete_path(bot_root, "data", "sub")


def test_read_cap_applies_to_a_file_growing_past_fstat(
    bot_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A running bot appending to the file must not defeat the cap: the
    # fstat size passes, but the bytes actually read are counted too.
    target = bot_root / "data" / "growing.log"
    target.write_bytes(b"x" * 64)
    real_fstat = os.fstat

    def lying_fstat(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        return os.stat_result((result.st_mode, result.st_ino, result.st_dev,
                               result.st_nlink, result.st_uid, result.st_gid,
                               4, result.st_atime, result.st_mtime, result.st_ctime))

    monkeypatch.setattr(fileops, "MAX_READ_BYTES", 8)
    monkeypatch.setattr(os, "fstat", lying_fstat)
    with pytest.raises(FileOpError) as excinfo:
        fileops.read_file(bot_root, "data", "growing.log")
    assert excinfo.value.code == "too_large"
