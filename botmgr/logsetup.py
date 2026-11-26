"""Operational logging for the manager server.

One destination for everything the server emits: standard error,
which under systemd is the journal (``journalctl -u botenclave``).
journald supplies the timestamp, unit and pid, so the line format
stays short. This is the conventional place for a systemd service's
operational log and is readable only by root and the journal group.

The audit trail is not logging: security-relevant operations are
recorded separately in the hash-chained ``audit.log`` in the data
root. Bot output is separate too (each bot's ``logs/`` directory).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

_FORMAT = "%(levelname)s %(name)s: %(message)s"

_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    """Route the root logger (and uvicorn's loggers) to stderr."""
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    _configured = True


def uvicorn_log_config() -> dict[str, Any]:
    """dictConfig for ``uvicorn.run`` matching :func:`configure_logging`,
    so access and error lines share the one format and destination."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"botmgr": {"format": _FORMAT}},
        "handlers": {
            "botmgr": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "formatter": "botmgr",
            }
        },
        "loggers": {
            "uvicorn": {"handlers": ["botmgr"], "propagate": False},
            "uvicorn.error": {"handlers": ["botmgr"], "propagate": False},
            "uvicorn.access": {"handlers": ["botmgr"], "propagate": False},
        },
    }
