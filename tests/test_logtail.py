from pathlib import Path

from botmgr.logfiles import rotate_copytruncate
from botmgr.logtail import (
    MAX_LINE_BYTES,
    LogFollower,
    decode_line,
    read_segment,
    read_tail,
)


def test_follower_reads_new_lines(tmp_path: Path) -> None:
    log = tmp_path / "console.log"
    log.write_text("first\n")
    follower = LogFollower(log)
    assert follower.poll() == ["first"]
    assert follower.poll() == []
    with open(log, "a") as fh:
        fh.write("second\nthird\n")
    assert follower.poll() == ["second", "third"]
    follower.close()


def test_follower_waits_for_complete_lines(tmp_path: Path) -> None:
    log = tmp_path / "console.log"
    log.write_text("partial")
    follower = LogFollower(log)
    assert follower.poll() == []
    with open(log, "a") as fh:
        fh.write(" done\n")
    assert follower.poll() == ["partial done"]
    follower.close()


def test_follower_survives_copytruncate(tmp_path: Path) -> None:
    log = tmp_path / "console.log"
    log.write_text("before\n")
    follower = LogFollower(log)
    assert follower.poll() == ["before"]
    with open(log, "r+b") as fh:
        fh.truncate(0)
    with open(log, "a") as fh:
        fh.write("after\n")
    assert follower.poll() == ["after"]
    follower.close()


def test_follower_caps_overlong_line_and_recovers(tmp_path: Path) -> None:
    log = tmp_path / "console.log"
    log.write_bytes(b"x" * (MAX_LINE_BYTES + 5000) + b"\nnext\n")
    follower = LogFollower(log)
    lines = follower.poll()
    assert len(lines) == 2
    assert lines[0].endswith("[truncated]")
    assert lines[1] == "next"
    follower.close()


def test_follower_no_newline_flood_does_not_buffer_forever(
    tmp_path: Path,
) -> None:
    log = tmp_path / "console.log"
    log.write_bytes(b"y" * (MAX_LINE_BYTES * 3))
    follower = LogFollower(log)
    lines = follower.poll()
    assert len(lines) == 1
    assert lines[0].endswith("[truncated]")
    assert follower.poll() == []
    follower.close()


def test_decode_line_replaces_invalid_utf8() -> None:
    assert decode_line(b"\xff\xfe") == "��"


def test_read_tail(tmp_path: Path) -> None:
    log = tmp_path / "console.log"
    log.write_text("a\nb\nc\nd\n")
    assert read_tail(log, 2) == ["c", "d"]
    assert read_tail(log, 99) == ["a", "b", "c", "d"]
    assert read_tail(tmp_path / "missing.log", 5) == []


def test_rotate_copytruncate(tmp_path: Path) -> None:
    log = tmp_path / "console.log"
    log.write_text("x" * 100)
    inode_before = log.stat().st_ino
    assert rotate_copytruncate(log, max_bytes=50, generations=3) is True
    assert log.read_text() == ""
    assert log.stat().st_ino == inode_before
    assert (tmp_path / "console.log.1").read_text() == "x" * 100


def test_rotate_skips_small_files(tmp_path: Path) -> None:
    log = tmp_path / "console.log"
    log.write_text("small")
    assert rotate_copytruncate(log, max_bytes=1000) is False
    assert log.read_text() == "small"


def test_rotate_shifts_generations(tmp_path: Path) -> None:
    log = tmp_path / "console.log"
    (tmp_path / "console.log.1").write_text("gen1")
    (tmp_path / "console.log.2").write_text("gen2")
    log.write_text("current")
    assert rotate_copytruncate(log, max_bytes=1, generations=3) is True
    assert (tmp_path / "console.log.1").read_text() == "current"
    assert (tmp_path / "console.log.2").read_text() == "gen1"
    assert (tmp_path / "console.log.3").read_text() == "gen2"


def test_truncated_line_does_not_end_in_replacement_char() -> None:
    raw = "←".encode() * (MAX_LINE_BYTES // 3 + 10)
    decoded = decode_line(raw)
    assert "\ufffd" not in decoded
    assert decoded.endswith(" [truncated]")


def test_read_tail_window_drops_the_partial_first_line(tmp_path: Path) -> None:
    log = tmp_path / "big.log"
    filler = ("x" * 100 + "\n") * 12000  # > 1 MiB window
    log.write_text(filler + "last-line\n", encoding="utf-8")
    lines = read_tail(log, 3)
    assert lines[-1] == "last-line"
    assert all(set(line) == {"x"} for line in lines[:-1])


def test_read_tail_newline_free_file_over_window(tmp_path: Path) -> None:
    """Regression (CI failure 2026-11-27): a log flooded with one
    newline-free stream larger than the read window returned no lines
    at all, because the window's only fragment was dropped as a
    partial first line."""
    log = tmp_path / "flood.log"
    log.write_bytes(b"x" * (2 * 1024 * 1024))
    lines = read_tail(log, 3)
    assert len(lines) == 1
    assert lines[0].endswith(" [truncated]")
    assert set(lines[0][:100]) == {"x"}


def test_read_segment_returns_only_lines_from_the_offset(
    tmp_path: Path,
) -> None:
    log = tmp_path / "console.log"
    old = b"old one\nold two\n"
    log.write_bytes(old + b"new one\nnew two\n")
    lines, end = read_segment(log, len(old), 200)
    assert lines == ["new one", "new two"]
    assert end == log.stat().st_size


def test_read_segment_mid_line_offset_skips_the_partial_line(
    tmp_path: Path,
) -> None:
    log = tmp_path / "console.log"
    log.write_bytes(b"partial-line\nwhole\n")
    lines, _end = read_segment(log, 4, 200)
    assert lines == ["whole"]


def test_read_segment_offset_past_end_reads_the_whole_file(
    tmp_path: Path,
) -> None:
    # The offset was recorded before a copytruncate rotation emptied
    # the file: everything in the fresh file is the current run.
    log = tmp_path / "console.log"
    log.write_bytes(b"fresh\n")
    lines, _end = read_segment(log, 5000, 200)
    assert lines == ["fresh"]


def test_read_segment_caps_at_max_lines(tmp_path: Path) -> None:
    log = tmp_path / "console.log"
    log.write_bytes(b"a\nb\nc\nd\n")
    lines, _end = read_segment(log, 0, 2)
    assert lines == ["c", "d"]


def test_follower_with_start_offset_skips_existing_content(
    tmp_path: Path,
) -> None:
    log = tmp_path / "console.log"
    existing = b"first\nsecond\n"
    log.write_bytes(existing)
    follower = LogFollower(log, start_offset=len(existing))
    assert follower.poll() == []
    with open(log, "a") as fh:
        fh.write("third\n")
    assert follower.poll() == ["third"]
    follower.close()
