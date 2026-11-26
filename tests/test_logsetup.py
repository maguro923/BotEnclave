from __future__ import annotations

import logging
import sys

import botmgr.logsetup as logsetup
from botmgr.logsetup import configure_logging, uvicorn_log_config


def test_configure_logging_routes_root_to_stderr() -> None:
    logsetup._configured = False
    try:
        configure_logging()
        root = logging.getLogger()
        assert root.level == logging.INFO
        assert len(root.handlers) == 1
        handler = root.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream is sys.stderr
        # uvicorn loggers propagate into the same handler
        assert logging.getLogger("uvicorn.access").propagate is True
        # idempotent: a second call must not stack handlers
        configure_logging()
        assert len(logging.getLogger().handlers) == 1
    finally:
        logsetup._configured = False


def test_uvicorn_log_config_shares_the_format() -> None:
    config = uvicorn_log_config()
    assert config["formatters"]["botmgr"]["format"] == logsetup._FORMAT
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert config["loggers"][name]["handlers"] == ["botmgr"]
