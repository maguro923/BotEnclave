"""Console log rotation.

Rotation is copytruncate by design (PLAN section 1.5): the tmux pipe-pane
writer holds the file open by inode, so the file must be emptied in place,
never renamed away. The copy keeps the previous generations.
"""

from __future__ import annotations

import shutil
from pathlib import Path

MAX_LOG_BYTES = 5_000_000
GENERATIONS = 5


def rotate_copytruncate(
    path: Path, max_bytes: int = MAX_LOG_BYTES, generations: int = GENERATIONS
) -> bool:
    """Rotate ``path`` if it has reached ``max_bytes``. Returns True if a
    rotation happened."""
    try:
        if path.stat().st_size < max_bytes:
            return False
    except OSError:
        return False
    for index in range(generations - 1, 0, -1):
        src = path.with_name(f"{path.name}.{index}")
        dst = path.with_name(f"{path.name}.{index + 1}")
        if src.exists():
            src.replace(dst)
    shutil.copyfile(path, path.with_name(f"{path.name}.1"))
    # Truncate in place: same inode, so the running writer keeps appending
    # to the (now empty) current file.
    with open(path, "r+b") as fh:
        fh.truncate(0)
    return True
