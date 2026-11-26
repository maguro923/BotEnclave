"""systemd unit and install-file rendering (PLAN sections 3.3, 5.2).

Everything here is pure text generation so it can be unit-tested and
reviewed before anything is installed. The `botmgr systemd` CLI writes
these files (as root) and reloads systemd.

Layout produced by an install:
- /usr/local/lib/botenclave/helper — root-owned helper entry (sudo target)
- /etc/sudoers.d/botenclave — the manager user may run only that entry
- /etc/systemd/system/botenclave.service — the manager itself
- /etc/systemd/system/botenclave-bot-<bot>.service — one unit per bot
  whose tmux server runs inside the unit's cgroup; with a systemd-creds
  secret backend, PID 1 decrypts the credential into the unit's private
  credentials directory (LoadCredentialEncrypted) and neither the
  manager nor the helper ever sees the plaintext.
"""

from __future__ import annotations

import re
from pathlib import Path

HELPER_ENTRY_PATH = "/usr/local/lib/botenclave/helper"
MANAGER_UNIT = "botenclave.service"


def bot_unit_name(bot: str) -> str:
    return f"botenclave-bot-{bot}.service"


def parse_manager_bind(unit_text: str) -> tuple[str, int] | None:
    """The --host/--port a rendered manager unit serves on, if present."""
    for line in unit_text.splitlines():
        if not line.startswith("ExecStart=") or " serve " not in line:
            continue
        host = re.search(r"--host\s+(\S+)", line)
        port = re.search(r"--port\s+(\d+)", line)
        if host is not None and port is not None:
            return host.group(1), int(port.group(1))
    return None


def render_helper_entry(
    root: Path, package_dir: Path, manager_group: str | None = None
) -> str:
    group_line = (
        f"export BOTENCLAVE_MANAGER_GROUP={manager_group}\n"
        if manager_group
        else ""
    )
    return (
        "#!/bin/sh\n"
        "# BotEnclave privileged helper entry point (root-owned).\n"
        "# sudo strips the caller's environment, so everything the helper\n"
        "# needs is fixed here, at install time.\n"
        f"export BOTENCLAVE_ROOT={root}\n"
        f"export BOTENCLAVE_PACKAGE_DIR={package_dir}\n"
        f"{group_line}"
        f"export PYTHONPATH={package_dir.parent}\n"
        "exec /usr/bin/python3 -m botmgr.helperd\n"
    )


def render_sudoers(manager_user: str) -> str:
    return (
        "# BotEnclave: the manager service user may run only the\n"
        "# root-owned helper entry point, with no password prompt.\n"
        "# The helper validates every request itself; sudoers cannot\n"
        "# restrict the JSON protocol further, and does not need to.\n"
        f"{manager_user} ALL=(root) NOPASSWD: {HELPER_ENTRY_PATH}\n"
    )


def render_manager_unit(
    root: Path,
    manager_user: str,
    package_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> str:
    return f"""[Unit]
Description=BotEnclave manager
After=network.target
Wants=network.target

[Service]
Type=simple
User={manager_user}
WorkingDirectory={root}
Environment=PYTHONPATH={package_dir.parent}
ExecStart=/usr/bin/python3 -m botmgr.cli --root {root} serve --host {host} --port {port} --transport sudo
Restart=on-failure
RestartSec=5
MemoryMax=1G
OOMScoreAdjust=-500

[Install]
WantedBy=multi-user.target
"""


def render_bot_unit(
    bot: str,
    root: Path,
    package_dir: Path,
    *,
    memory_max: str = "512M",
    cpu_quota: str = "100%",
    use_credential: bool = False,
) -> str:
    credential_line = ""
    if use_credential:
        # The credential ID must equal the name embedded at encryption
        # time (botmgr-<bot>), or PID 1 refuses to hand it over.
        credential_line = (
            f"LoadCredentialEncrypted=botmgr-{bot}:{root}/secrets/{bot}.cred\n"
        )
    return f"""[Unit]
Description=BotEnclave bot {bot}
After=network.target

[Service]
Type=simple
User=bm-{bot}
Group=bm-{bot}
WorkingDirectory={root}/bots/{bot}/data
Environment=PYTHONPATH={package_dir.parent}
{credential_line}ExecStart=/usr/bin/python3 -m botmgr.botunit --root {root} --bot {bot} --package-dir {package_dir}
ExecStop=/usr/bin/python3 -m botmgr.botunit --root {root} --bot {bot} --package-dir {package_dir} --stop
MemoryMax={memory_max}
CPUQuota={cpu_quota}
OOMScoreAdjust=500
NoNewPrivileges=true
KillMode=control-group

[Install]
WantedBy=multi-user.target
"""
