#!/bin/sh
# BotEnclave removal: stop and remove the services, the helper entry
# point, the sudoers rule and the installed package.
#
# By default the data root (settings, databases, bot files, secrets)
# and the OS users are KEPT, so reinstalling restores the panel as it
# was. Pass --purge to remove those too; purging is irreversible.
#
# Usage:
#   sudo sh scripts/uninstall.sh [--purge]
#
# Overrides (same names as scripts/install.sh):
#   BOTENCLAVE_ROOT=/srv/botenclave        data root
#   BOTENCLAVE_MANAGER_USER=botenclave     manager OS user
#   BOTENCLAVE_VENV=/usr/local/lib/botenclave/venv
set -eu

ROOT=${BOTENCLAVE_ROOT:-/srv/botenclave}
MANAGER_USER=${BOTENCLAVE_MANAGER_USER:-botenclave}
VENV=${BOTENCLAVE_VENV:-/usr/local/lib/botenclave/venv}
INSTALL_DIR=$(dirname "$VENV")
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

say() { printf '%s\n' "$*"; }
die() { printf 'uninstall: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root: sudo sh scripts/uninstall.sh"
command -v systemctl >/dev/null 2>&1 \
    || die "systemd is required for the supported layout"

if [ "$PURGE" = 1 ] && [ -t 0 ]; then
    say "This will permanently delete $ROOT (all bots, secrets and"
    say "settings) and remove the manager and per-bot OS users."
    printf 'Type YES to continue: '
    read -r answer
    [ "$answer" = "YES" ] || die "aborted"
fi

# Bot names come from the installed bot units and, while it lasts,
# the data root; both sources are needed for a complete purge.
bot_names() {
    for unit in /etc/systemd/system/botenclave-bot-*.service; do
        [ -e "$unit" ] || continue
        name=$(basename "$unit" .service)
        printf '%s\n' "${name#botenclave-bot-}"
    done
    if [ -d "$ROOT/bots" ]; then
        for dir in "$ROOT/bots"/*; do
            [ -d "$dir" ] || continue
            basename "$dir"
        done
    fi
}
BOTS=$(bot_names | sort -u)

# 1. Stop and disable the bot units and the manager.
for bot in $BOTS; do
    systemctl disable --now "botenclave-bot-$bot" >/dev/null 2>&1 || true
done
systemctl disable --now botenclave >/dev/null 2>&1 || true

# 2. Remove the unit files, the sudoers rule and the helper entry.
for bot in $BOTS; do
    rm -f "/etc/systemd/system/botenclave-bot-$bot.service"
done
rm -f /etc/systemd/system/botenclave.service
rm -f /etc/sudoers.d/botenclave
systemctl daemon-reload
systemctl reset-failed >/dev/null 2>&1 || true
say "removed systemd units and sudoers rule"

# 3. Remove the installed package (venv + helper entry point). Only
#    when the directory really looks like a BotEnclave install.
if [ -x "$VENV/bin/botmgr" ] || [ -e "$INSTALL_DIR/helper" ]; then
    rm -rf "$INSTALL_DIR"
    say "removed $INSTALL_DIR"
else
    say "no BotEnclave install found at $INSTALL_DIR; skipped"
fi

if [ "$PURGE" = 0 ]; then
    say ""
    say "Kept as-is:"
    [ -d "$ROOT" ] && say "  data root: $ROOT (settings, bots, secrets)"
    say "  OS users: $MANAGER_USER and any bm-<bot> users"
    say "Reinstall with scripts/install.sh to bring the panel back."
    say "Remove everything with: sudo sh scripts/uninstall.sh --purge"
    exit 0
fi

# 4. Purge: leftover tmux sockets, the data root and the OS users.
for bot in $BOTS; do
    uid=$(id -u "bm-$bot" 2>/dev/null || true)
    [ -n "$uid" ] && rm -rf "/tmp/tmux-$uid"
    userdel "bm-$bot" 2>/dev/null || true
done
if [ -d "$ROOT" ]; then
    rm -rf "$ROOT"
    say "removed data root $ROOT"
fi
userdel "$MANAGER_USER" 2>/dev/null || true
say "removed OS users ($MANAGER_USER, bm-<bot>)"
say "BotEnclave is fully removed."
