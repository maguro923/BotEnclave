#!/bin/sh
# BotEnclave production setup: prerequisites, package, manager user,
# systemd unit and first start. Idempotent — safe to re-run.
#
# Usage (from the repository root):
#   sudo sh scripts/install.sh
#
# Overrides:
#   BOTENCLAVE_ROOT=/srv/botenclave        data root
#   BOTENCLAVE_MANAGER_USER=botenclave     manager OS user
#   BOTENCLAVE_VENV=/usr/local/lib/botenclave/venv
set -eu

ROOT=${BOTENCLAVE_ROOT:-/srv/botenclave}
MANAGER_USER=${BOTENCLAVE_MANAGER_USER:-botenclave}
VENV=${BOTENCLAVE_VENV:-/usr/local/lib/botenclave/venv}
BOTMGR="$VENV/bin/botmgr"

say() { printf '%s\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root: sudo sh scripts/install.sh"
[ -f pyproject.toml ] && [ -d botmgr ] \
    || die "run from the BotEnclave repository root"
command -v systemctl >/dev/null 2>&1 \
    || die "systemd is required for the supported layout"

# 1. Prerequisites (Debian/Ubuntu install them; elsewhere they must
#    already be present).
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        python3 python3-venv tmux git
fi
for cmd in python3 tmux git; do
    command -v "$cmd" >/dev/null 2>&1 || die "missing prerequisite: $cmd"
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python 3.11 or later is required"

# 2. Package into a venv the per-bot OS users can read and execute.
if [ ! -x "$VENV/bin/python" ]; then
    mkdir -p "$(dirname "$VENV")"
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet .
chmod -R a+rX "$VENV"

# 3. Manager user and data root.
if ! id "$MANAGER_USER" >/dev/null 2>&1; then
    useradd --system --home "$ROOT" --shell /usr/sbin/nologin "$MANAGER_USER"
fi
mkdir -p "$ROOT"
chown "$MANAGER_USER" "$ROOT"

# Existing data is never overwritten; say so when there is any.
EXISTING=""
for f in access.toml tls-cert.pem tls-key.pem secret.key web-secret \
    botmgr.db auth.db setup-token; do
    [ -e "$ROOT/$f" ] && EXISTING="$EXISTING $f"
done
[ -d "$ROOT/bots" ] && EXISTING="$EXISTING bots/"
if [ -n "$EXISTING" ]; then
    say "Existing BotEnclave data found in $ROOT:"
    say " $EXISTING"
    say "Settings and data are kept as-is; nothing is overwritten."
fi

# 4. Helper entry point, sudoers rule and the manager unit.
"$BOTMGR" --root "$ROOT" systemd install --manager-user "$MANAGER_USER"
PORT=$(sed -n 's/.*--port \([0-9][0-9]*\).*/\1/p' \
    /etc/systemd/system/botenclave.service | head -n 1)
[ -n "$PORT" ] || PORT=8000

# A still-unused setup token is replaced with a fresh one on re-install
# (the manager mints it at startup; the new value is printed below).
if [ -f "$ROOT/setup-token" ]; then
    rm -f "$ROOT/setup-token"
    say "Replaced the setup token with a fresh one (shown below)."
fi

# 5. Start (or restart, when re-running over a live install).
systemctl enable botenclave >/dev/null 2>&1 || true
if systemctl is-active --quiet botenclave; then
    systemctl restart botenclave
else
    systemctl start botenclave
fi

# 6. Wait for the health endpoint.
i=0
while [ $i -lt 30 ]; do
    if "$VENV/bin/python" -c '
import ssl, sys, urllib.request
try:
    ctx = ssl.create_default_context(cafile=sys.argv[1])
    url = "https://127.0.0.1:" + sys.argv[2] + "/healthz"
    urllib.request.urlopen(url, timeout=2, context=ctx)
except Exception:
    sys.exit(1)
' "$ROOT/tls-cert.pem" "$PORT" 2>/dev/null; then
        break
    fi
    i=$((i + 1))
    sleep 1
done
[ $i -lt 30 ] || die "manager did not become healthy; see: journalctl -u botenclave"

say ""
say "BotEnclave is running."
say "Open https://127.0.0.1:$PORT/ on this host to finish the first-run setup."
say "The panel serves HTTPS with a self-signed certificate, so the browser"
say "warns on the first visit. From another machine, tunnel in:"
say "  ssh -L $PORT:127.0.0.1:$PORT <user>@<this-host>"
say "or whitelist that machine in $ROOT/access.toml and restart"
say "(docs/setup.md, section 6)."
if [ -f "$ROOT/setup-token" ]; then
    say "Setup token: $(cat "$ROOT/setup-token")"
else
    say "Setup already completed (no setup token present)."
    say "For a new token: $BOTMGR --root $ROOT setup-token"
fi
say "Then enable TOTP for the administrator and take a first backup"
say "(docs/backup.md)."
