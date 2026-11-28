import io
import time

from botmgr.logwriter import TimestampingWriter

NOW = 1_700_000_000.0
PREFIX = (
    "[" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW)) + "] "
).encode()


def make_writer() -> tuple[TimestampingWriter, io.BytesIO]:
    out = io.BytesIO()
    return TimestampingWriter(out, now_fn=lambda: NOW), out


def test_prefixes_each_line() -> None:
    writer, out = make_writer()
    writer.write(b"first\nsecond\n")
    assert out.getvalue() == PREFIX + b"first\n" + PREFIX + b"second\n"


def test_line_split_across_writes_is_stamped_once() -> None:
    writer, out = make_writer()
    writer.write(b"par")
    # The prefix and the partial line are already on disk: a writer
    # killed mid-line loses nothing it received.
    assert out.getvalue() == PREFIX + b"par"
    writer.write(b"tial\nnext")
    assert out.getvalue() == PREFIX + b"partial\n" + PREFIX + b"next"


def test_blank_line_is_stamped() -> None:
    writer, out = make_writer()
    writer.write(b"\n")
    assert out.getvalue() == PREFIX + b"\n"


def test_binary_output_passes_through() -> None:
    writer, out = make_writer()
    writer.write(b"\x1b[31mred\x1b[0m\n")
    assert out.getvalue() == PREFIX + b"\x1b[31mred\x1b[0m\n"
