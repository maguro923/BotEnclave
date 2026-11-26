"""Display-time log masking rules (PLAN 1.3)."""

from __future__ import annotations

from botmgr.masking import mask_lines

TOKEN = "supersecrettoken123"


def test_masks_known_values() -> None:
    lines = [f"connecting with {TOKEN} ok", "nothing here"]
    out = mask_lines(lines, [TOKEN])
    assert TOKEN not in out[0]
    assert "•••" in out[0]
    assert out[1] == "nothing here"


def test_short_values_ignored() -> None:
    out = mask_lines(["abc def abc"], ["abc"])
    assert out == ["abc def abc"]


def test_only_first_4kb_scanned() -> None:
    line = "x" * 5000 + TOKEN
    out = mask_lines([line], [TOKEN])
    # Beyond the scan prefix the value survives: the documented CPU bound.
    assert out[0].endswith(TOKEN)
    line2 = TOKEN + "y" * 5000
    out2 = mask_lines([line2], [TOKEN])
    assert TOKEN not in out2[0][:4096]
