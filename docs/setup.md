# Setup guide

This guide takes a fresh Linux host from nothing to a running
BotEnclave instance with its first bot online. It consolidates the
install paths that the README only sketches; for the design behind
them see [architecture.md](architecture.md), and for day-2 operations
see [backup.md](backup.md) and [upgrading.md](upgrading.md).

There are two supported layouts:

| Layout | When to use it |
|---|---|
| **Quickstart** (single-host) | Trying BotEnclave out, a personal host you fully control. The manager runs as root and drives the helper in-process. |
| **Production** (systemd) | Anything long-lived or shared. The manager runs as its own unprivileged user, the helper is reached through a narrow sudoers rule, and each bot runs in its own systemd unit. |

Both layouts give every bot its own OS user, tmux server and process
tree; they differ in how the manager itself is run.

## 1. Host prerequisites

- Linux (or WSL2 with systemd enabled). Windows and macOS are not
  supported.
- Python 3.11 or later.
- `tmux` and `git` on `PATH`.
- For the production layout: systemd and sudo.
- About 200 MB of disk for the manager and its virtualenv, plus
  whatever your bots need (each bot gets its own virtualenv per
  release).

On Debian/Ubuntu:

```sh
sudo apt update
sudo apt install -y python3 python3-venv tmux git
```

## 2. Install the package

Install into a location that the per-bot OS users can read and
execute — they load the launcher shim and the tmux configuration from
the package directory, so a virtualenv under a private home directory
will not work.

```sh
git clone https://github.com/maguro923/BotEnclave.git
cd BotEnclave
sudo python3 -m venv /usr/local/lib/botenclave/venv
sudo /usr/local/lib/botenclave/venv/bin/pip install .
```

From here on, `botmgr` means
`/usr/local/lib/botenclave/venv/bin/botmgr`.

Choose a **data root** — the directory that will hold the databases,
secrets, audit log and every bot's files. The examples use
`/srv/botenclave`. Keep it off shared/network filesystems: the state
store is SQLite and expects local file locking.

## 3a. Quickstart layout

Run the manager as root:

```sh
sudo botmgr --root /srv/botenclave serve --port 8000
```

Leave it in the foreground for a first look, or run it under tmux /
`nohup` for a trial. Then continue with [first-run setup](#4-first-run-setup).

Notes:

- `--transport` defaults to `direct`, which is only safe because the
  manager itself is root here. Never expose this mode's port beyond a
  trusted network without TLS in front (see [§6](#6-tls-and-access)).
- The manager is single-process by design and refuses to start twice
  against the same root (it takes an exclusive lock). If a previous
  process was killed uncleanly, make sure no `botmgr serve` is alive
  before restarting.

## 3b. Production layout (systemd)

```sh
# 1. the manager user and the data root
sudo useradd --system --home /srv/botenclave --shell /usr/sbin/nologin botenclave
sudo mkdir -p /srv/botenclave && sudo chown botenclave /srv/botenclave

# 2. helper entry point, sudoers rule and the manager unit
sudo botmgr --root /srv/botenclave systemd install --manager-user botenclave

# 3. start the manager
sudo systemctl enable --now botenclave
```

`botmgr systemd render-manager` / `render-bot` print the unit files
for review without installing anything. Full details, including
per-bot units and their resource limits, are in
[systemd.md](systemd.md).

In this layout the manager talks to the helper through sudo
(`--transport sudo`, already set in the installed unit) and bots can
later be moved under individual systemd units with
`botmgr systemd install-bot <bot>` plus `--bot-runtime systemd`.

## 4. First-run setup

1. Open `http://<host>:8000/`. With no administrator existing yet, you
   are redirected to the setup page.
2. Enter the **setup token**. It is printed in the manager's log at
   first start and stored in `<root>/setup-token` (mode 600):

   ```sh
   sudo cat /srv/botenclave/setup-token        # quickstart
   sudo journalctl -u botenclave | grep setup  # production
   ```

   The token file is deleted once setup completes; if you lose it
   before finishing, restart the manager to mint a new one, or run
   `botmgr --root /srv/botenclave setup-token`.
3. Create the administrator account (password: at least 10
   characters). You are signed in immediately afterwards.

Right after setup, open **Account** and enable **TOTP two-factor
authentication** — the dashboard shows a warning banner until the
administrator has done this. Save the backup codes somewhere safe;
each is single-use.

## 5. Your first bot

1. On the dashboard, **Add a bot**: give it a name (lowercase letters,
   digits and `-`/`_`; it becomes the OS user `bm-<name>`) and,
   optionally, a start command. The command can be left empty if you
   deploy code in the next step — the deploy fills it in.
2. Open the bot's page and use **Deploy**:
   - **Git**: paste an `https://` or `ssh` repository URL and a
     branch. Credentials embedded in the URL are stored encrypted
     and redacted from deploy logs.
   - **Zip**: upload an archive of the bot's code. Paths escaping the
     archive root, absolute paths and symlinks are rejected.
   
   The deploy detects the entry file (`main.py`, `bot.py`, `app.py`,
   `run.py`, or a single top-level `.py`), syntax-checks it, builds a
   per-release virtualenv from `requirements.txt` if present, and
   switches the bot to the new release atomically.
3. In **Environment variables**, set what the bot needs — typically
   `DISCORD_TOKEN`. Pick the secret backend (see the README's
   comparison table; `systemd-creds` is the standard choice on
   systemd hosts). Values are never displayed again after saving.
4. Press **Start**. The bot should reach `running`; open **Logs** or
   the **Console** to watch it. While a console is connected, log
   capture pauses (private mode) so anything typed at the bot — for
   example during an interactive login — never lands in log files.

Guests: an administrator can invite additional users from **Users**.
Guests only ever see their own bots; accounts are invite-only and
there is no self-registration. Only invite people you trust to run
code on this host — a guest's bot is still arbitrary code execution
as that bot's OS user.

## 6. TLS and access

Session cookies are `Secure` by default, so over plain HTTP on a
non-localhost address the browser will not send them and login will
appear to loop. For anything beyond a trusted LAN, put BotEnclave
behind a TLS-terminating reverse proxy (or a private tunnel) and
keep the manager's own port unexposed.

`--no-cookie-secure` disables the `Secure` flag for local development
only. Do not use it on a reachable host.

## 7. Post-setup checklist

- [ ] Administrator has TOTP enabled and backup codes stored.
- [ ] First backup taken: `botmgr --root /srv/botenclave backup
      --output /path/to/backup.tar.gz` (see [backup.md](backup.md)).
      Note that systemd-creds ciphertext is bound to the host key and
      cannot be decrypted after a host migration — plan to re-enter
      environment values on a new host.
- [ ] Audit log retention fits your needs (default 180 days;
      `botmgr audit purge --days N`, `botmgr audit verify` checks
      the hash chain).
- [ ] Manager restarts cleanly (`systemctl restart botenclave`, or
      re-run the quickstart command) and running bots are reconciled
      — adopted if still alive, marked crashed if gone.

## 8. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Manager exits with “already running” | Another `botmgr serve` holds the lock on this root, or a previous one is still alive. Check `pgrep -af botmgr` before assuming a stale lock. |
| Bot fails to start with permission errors | The package directory or a release/venv is not readable/executable by the bot's OS user. Reinstall into a world-readable location (see [§2](#2-install-the-package)); do not `chmod` a private home directory open as a workaround. |
| Bot sits in `secret-pending` | Its environment values are not resolvable: the memory-only backend lost them on manager restart (re-enter them), or the bot runs under a systemd unit with a non-systemd-creds backend (switch the backend in the Environment section). |
| Login page loops back after signing in | Cookie not being sent: you are on plain HTTP with `Secure` cookies. Use HTTPS / a proxy (§6), or `--no-cookie-secure` for local development only. |
| Deploy rejects a zip | It contains absolute paths, `..` entries or symlinks, or exceeds the size limit. Repack with plain relative file paths. |
| Deploy cannot use a git URL | Only `https`, `ssh` and `git@` URLs are accepted, and local file transport is disabled by design. Mirror the repository to a reachable remote. |
| Console shows nothing / bot seems mute | A console connection pauses log capture but not the bot. Disconnect the console and check **Logs**; also confirm the bot writes to stdout/stderr — output redirected to its own files is not captured. |

## 9. Uninstall

Stop the manager, stop all bots, then remove the units and files:

```sh
sudo systemctl disable --now botenclave                 # production
sudo botmgr --root /srv/botenclave bot-list             # stop/remove bots first
sudo rm -f /etc/systemd/system/botenclave*.service \
           /etc/sudoers.d/botenclave                    # production artifacts
sudo systemctl daemon-reload
sudo rm -rf /usr/local/lib/botenclave /srv/botenclave
# per-bot OS users remain; remove with: sudo userdel bm-<name>
```

Take a backup (§7) before uninstalling if there is any chance you
will want the bots' `data/` directories back.
