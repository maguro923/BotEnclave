"""Shared fixtures for the privileged integration tests.

The `helper` fixture builds a Helper with the real SystemExecutor against a
pytest temp root, with a world-readable staging copy of the package (the
bot users must be able to read the tmux config and the launcher shim).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

import botmgr
from botmgr.helperd import Helper, SystemExecutor

TEST_BOTS = ("itest1", "itest2", "ctest1")


@pytest.fixture()
def helper(tmp_path: Path) -> Helper:
    # pytest's temp tree is owner-only; the bot users need traverse (x)
    # permission on every ancestor of the test root to reach their folders.
    for parent in (tmp_path, *tmp_path.parents):
        if parent == Path("/tmp"):
            break
        parent.chmod(0o711)
    staging = tmp_path / "staging"
    package_src = Path(botmgr.__file__).parent
    shutil.copytree(
        package_src, staging / "botmgr", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(Path(__file__).parent / "fixtures", staging / "fixtures")
    for path in staging.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    staging.chmod(0o755)
    executor = SystemExecutor(tmp_path, package_dir=staging / "botmgr")
    h = Helper(root=tmp_path, executor=executor)
    h.fixtures_dir = staging / "fixtures"  # type: ignore[attr-defined]
    h.package_dir = staging / "botmgr"  # type: ignore[attr-defined]
    yield h
    for bot in TEST_BOTS:
        subprocess.run(
            ["su", "-s", "/bin/sh", f"bm-{bot}", "-c", f"tmux -L be-{bot} kill-server"],
            capture_output=True,
            check=False,
        )
        h.handle({"op": "delete_user", "bot": bot})
