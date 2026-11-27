"""Unit-file rendering (pure text) — the live systemd path is covered
by the privileged integration test."""

from __future__ import annotations

from pathlib import Path

from botmgr import systemd_units

ROOT = Path("/srv/botenclave")
PKG = Path("/usr/local/lib/botenclave/botmgr")


def test_bot_unit_with_credential() -> None:
    unit = systemd_units.render_bot_unit(
        "alpha", ROOT, PKG, use_credential=True
    )
    assert "User=bm-alpha" in unit
    assert (
        "LoadCredentialEncrypted=botmgr-alpha:/srv/botenclave/secrets/alpha.cred"
        in unit
    )
    assert "botmgr.botunit" in unit
    assert "MemoryMax=512M" in unit
    assert systemd_units.bot_unit_name("alpha") == "botenclave-bot-alpha.service"


def test_bot_unit_without_credential() -> None:
    unit = systemd_units.render_bot_unit("alpha", ROOT, PKG)
    assert "LoadCredentialEncrypted" not in unit


def test_manager_unit_and_sudoers() -> None:
    unit = systemd_units.render_manager_unit(ROOT, "botenclave", PKG, port=9000)
    assert "User=botenclave" in unit
    assert "Restart=always" in unit
    # A stop must be bounded: the panel holds WebSockets open, and an
    # unbounded graceful shutdown made `systemctl stop` appear to hang.
    assert "TimeoutStopSec=20" in unit
    assert "OOMScoreAdjust=-500" in unit
    assert "--transport sudo" in unit
    assert "--port 9000" in unit
    sudoers = systemd_units.render_sudoers("botenclave")
    assert "botenclave ALL=(root) NOPASSWD: /usr/local/lib/botenclave/helper" in sudoers
    entry = systemd_units.render_helper_entry(ROOT, PKG)
    assert entry.startswith("#!/bin/sh")
    assert "BOTENCLAVE_ROOT=/srv/botenclave" in entry
    assert "botmgr.helperd" in entry


def test_helper_entry_exports_manager_group_when_given() -> None:
    entry = systemd_units.render_helper_entry(
        ROOT, PKG, manager_group="botenclave"
    )
    assert "export BOTENCLAVE_MANAGER_GROUP=botenclave\n" in entry


def test_helper_entry_omits_manager_group_by_default() -> None:
    entry = systemd_units.render_helper_entry(ROOT, PKG)
    assert "BOTENCLAVE_MANAGER_GROUP" not in entry


def test_parse_manager_bind_roundtrip() -> None:
    unit = systemd_units.render_manager_unit(
        ROOT, "botenclave", PKG, host="0.0.0.0", port=8443
    )
    assert systemd_units.parse_manager_bind(unit) == ("0.0.0.0", 8443)


def test_parse_manager_bind_defaults() -> None:
    unit = systemd_units.render_manager_unit(ROOT, "botenclave", PKG)
    assert systemd_units.parse_manager_bind(unit) == ("127.0.0.1", 8000)


def test_parse_manager_bind_absent() -> None:
    assert systemd_units.parse_manager_bind("[Unit]\nDescription=x\n") is None
    bot = systemd_units.render_bot_unit("alpha", ROOT, PKG)
    assert systemd_units.parse_manager_bind(bot) is None
