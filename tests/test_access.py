"""Panel client allowlist: access.toml parsing, policy, enforcement."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from botmgr.access import (
    AccessConfig,
    AccessConfigError,
    ArpTable,
    load_access_config,
    parse_access_config,
)
from botmgr.app import AppConfig, create_app


def NO_MAC(ip: str) -> None:
    return None


def test_default_file_created_loopback_only(tmp_path: Path) -> None:
    config = load_access_config(tmp_path)
    path = tmp_path / "access.toml"
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert config.allow_all is False
    assert config.networks == ()
    assert config.opens_network() is False
    assert config.allows("127.0.0.1", NO_MAC)
    assert config.allows("::1", NO_MAC)
    assert not config.allows("192.168.0.10", NO_MAC)
    assert not config.allows(None, NO_MAC)
    assert not config.allows("testclient", NO_MAC)


def test_parse_entries_and_opens_network() -> None:
    config = parse_access_config(
        {"allow": ["192.168.0.10", "10.0.0.0/8", "AA-BB-CC-DD-EE-FF"]}, "t"
    )
    assert config.opens_network() is True
    assert config.allows("192.168.0.10", NO_MAC)
    assert config.allows("10.1.2.3", NO_MAC)
    assert not config.allows("192.168.0.11", NO_MAC)
    # IPv4-mapped IPv6 peers match IPv4 entries.
    assert config.allows("::ffff:192.168.0.10", NO_MAC)
    # MAC entries match through the resolver, normalized.
    assert config.allows("192.168.0.77", lambda ip: "aa:bb:cc:dd:ee:ff")
    assert not config.allows("192.168.0.77", lambda ip: "00:11:22:33:44:55")


def test_loopback_only_list_does_not_open_network() -> None:
    config = parse_access_config({"allow": ["127.0.0.2"]}, "t")
    assert config.opens_network() is False


def test_allow_all() -> None:
    config = parse_access_config({"allow_all": True}, "t")
    assert config.opens_network() is True
    assert config.allows("203.0.113.9", NO_MAC)


@pytest.mark.parametrize(
    "data",
    [
        {"allow": ["not-an-address"]},
        {"allow": ["192.168.0.0/33"]},
        {"allow": [42]},
        {"allow": "192.168.0.10"},
        {"allow_all": "yes"},
        {"allows": []},
    ],
)
def test_malformed_configs_fail_closed(data: dict) -> None:
    with pytest.raises(AccessConfigError):
        parse_access_config(data, "t")


def test_malformed_file_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "access.toml").write_text("allow = [", encoding="utf-8")
    with pytest.raises(AccessConfigError):
        load_access_config(tmp_path)


def test_arp_table_parsing(tmp_path: Path) -> None:
    arp = tmp_path / "arp"
    arp.write_text(
        "IP address       HW type     Flags       HW address            Mask     Device\n"
        "192.168.0.10     0x1         0x2         AA:BB:CC:DD:EE:FF     *        eth0\n"
        "192.168.0.11     0x1         0x0         00:00:00:00:00:00     *        eth0\n",
        encoding="utf-8",
    )
    table = ArpTable(path=str(arp))
    assert table.mac_for("192.168.0.10") == "aa:bb:cc:dd:ee:ff"
    # Incomplete entries (flags without ATF_COM) do not count.
    assert table.mac_for("192.168.0.11") is None
    assert table.mac_for("192.168.0.99") is None


def _client(tmp_path: Path, peer: str, config: AccessConfig) -> TestClient:
    app = create_app(
        AppConfig(
            root=tmp_path,
            secret="test-secret",
            transport="direct",
            access=config,
        )
    )
    return TestClient(app, client=(peer, 50000))


def test_middleware_denies_unlisted_client(tmp_path: Path) -> None:
    config = parse_access_config({"allow": ["192.168.0.10"]}, "t")
    denied = _client(tmp_path / "a", "192.168.0.11", config)
    resp = denied.get("/healthz")
    assert resp.status_code == 403
    allowed = _client(tmp_path / "b", "192.168.0.10", config)
    assert allowed.get("/healthz").status_code == 200
    local = _client(tmp_path / "c", "127.0.0.1", config)
    assert local.get("/healthz").status_code == 200


def test_middleware_websocket_scope() -> None:
    import asyncio

    from botmgr.access import AccessMiddleware

    config = parse_access_config({"allow": []}, "t")
    seen: list[str] = []

    async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
        seen.append(scope["type"])
        await send({"type": "websocket.accept"})

    middleware = AccessMiddleware(inner, config)

    async def run(peer: str) -> list[dict]:
        sent: list[dict] = []
        scope = {"type": "websocket", "client": (peer, 50000), "path": "/ws/x"}

        async def receive() -> dict:
            return {"type": "websocket.connect"}

        async def send(message: dict) -> None:
            sent.append(message)

        await middleware(scope, receive, send)
        return sent

    denied = asyncio.run(run("192.168.0.11"))
    assert denied == [{"type": "websocket.close", "code": 4403}]
    assert seen == []
    allowed = asyncio.run(run("127.0.0.1"))
    assert allowed == [{"type": "websocket.accept"}]
    assert seen == ["websocket"]
