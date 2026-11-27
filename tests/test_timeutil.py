"""Timezone resolution and timestamp formatting."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from botmgr.timeutil import (
    configured_tz,
    configured_tz_name,
    format_ts,
    system_timezone_name,
)


def test_format_ts_uses_the_zone_and_full_format() -> None:
    assert format_ts(0, ZoneInfo("UTC")) == "1970-01-01 00:00:00"
    assert format_ts(0, ZoneInfo("Asia/Tokyo")) == "1970-01-01 09:00:00"


def test_configured_tz_prefers_the_setting() -> None:
    tz = configured_tz("Asia/Tokyo")
    assert format_ts(0, tz) == "1970-01-01 09:00:00"
    assert configured_tz_name("Asia/Tokyo") == "Asia/Tokyo"


def test_configured_tz_falls_back_without_raising() -> None:
    tz = configured_tz("Bogus/Zone")
    # Whatever the fallback is, formatting must work.
    assert format_ts(0, tz).endswith(":00:00")
    assert configured_tz_name("Bogus/Zone")


def test_system_timezone_name_is_none_or_loadable() -> None:
    name = system_timezone_name()
    if name is not None:
        ZoneInfo(name)  # must not raise
