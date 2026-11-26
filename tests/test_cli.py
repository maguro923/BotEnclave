"""CLI entry points with a security contract (botmgr.cli).

The library behaviour is covered elsewhere; these tests pin the
CLI-side guarantees: the web secret comes from the environment when
set and is otherwise a stable 0600 file, and a setup token can never
be minted once the first administrator exists.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from botmgr.cli import _setup_token, _web_secret, main


def test_web_secret_prefers_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOTENCLAVE_SECRET", "from-env")
    assert _web_secret(tmp_path) == "from-env"
    assert not (tmp_path / "web-secret").exists()


def test_web_secret_file_is_0600_and_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BOTENCLAVE_SECRET", raising=False)
    first = _web_secret(tmp_path)
    assert first
    assert (tmp_path / "web-secret").stat().st_mode & 0o777 == 0o600
    assert _web_secret(tmp_path) == first


def test_setup_token_lifecycle(tmp_path: Path) -> None:
    token = asyncio.run(_setup_token(tmp_path, regenerate=False))
    assert token
    token_file = tmp_path / "setup-token"
    assert token_file.stat().st_mode & 0o777 == 0o600
    # Without --regenerate the existing token is returned as-is.
    assert asyncio.run(_setup_token(tmp_path, regenerate=False)) == token
    # --regenerate replaces it while setup is still open.
    fresh = asyncio.run(_setup_token(tmp_path, regenerate=True))
    assert fresh != token


def test_setup_token_refused_once_setup_completed(tmp_path: Path) -> None:
    async def make_admin() -> None:
        from botmgr.auth.deps import build_auth, init_auth_db
        from botmgr.auth.service import AuthService

        bundle = build_auth(tmp_path, "test-secret", cookie_secure=True)
        await init_auth_db(bundle)
        await AuthService(bundle).create_first_admin(
            "admin@example.com", "a-strong-password", None
        )

    asyncio.run(make_admin())
    with pytest.raises(RuntimeError, match="setup is already complete"):
        asyncio.run(_setup_token(tmp_path, regenerate=True))
    with pytest.raises(RuntimeError, match="setup is already complete"):
        asyncio.run(_setup_token(tmp_path, regenerate=False))


def test_main_access_prints_effective_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(tmp_path), "access"]) == 0
    out = capsys.readouterr().out
    assert "loopback" in out.lower()


def test_main_setup_token_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(tmp_path), "setup-token"]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed == (tmp_path / "setup-token").read_text(encoding="ascii").strip()
