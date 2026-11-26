# Setup guide

This guide takes a fresh Linux host from nothing to a running
BotEnclave instance with its first bot online. For the design behind
the layout see [architecture.md](architecture.md), and for day-2
operations see [backup.md](backup.md) and [upgrading.md](upgrading.md).

There is one supported layout, the **production layout**: the manager
runs as its own unprivileged user under systemd, the helper is reached
through a narrow sudoers rule, and each bot runs as its own OS user in
its own tmux server (optionally in its own systemd unit). The
installer script in §2 performs the whole setup; §3 lists the same
steps manually.

## 1. Host prerequisites

- Linux (or WSL2 with systemd enabled). Windows and macOS are not
  supported.
- systemd and sudo.
- Python 3.11 or later, `tmux` and `git` on `PATH`.
- About 200 MB of disk for the manager and its virtualenv, plus
  whatever your bots need (each bot gets its own virtualenv per
  release).

On Debian/Ubuntu the installer script installs the packages for you:

```sh
sudo apt update
sudo apt install -y python3 python3-venv tmux git
```

## 2. Automated setup (recommended)

Clone the repository and run the installer as root:

```sh
git clone https://github.com/maguro923/BotEnclave.git
cd BotEnclave
sudo sh scripts/install.sh
```

The script is idempotent (safe to re-run) and performs:

1. the prerequisite check/install (§1),
2. a virtualenv at `/usr/local/lib/botenclave/venv` with the package
   installed into it — a location the per-bot OS users can read and
   execute, because they load the launcher shim and the tmux
   configuration from the package directory,
3. the manager user `botenclave` and the data root `/srv/botenclave`
   (the directory that holds the databases, secrets, audit log and
   every bot's files — keep it off shared/network filesystems, the
   state store is SQLite and expects local file locking),
4. the helper entry point, the sudoers rule and the manager unit
   (`botmgr systemd install`),
5. enabling and starting the service, waiting for its health
   endpoint, and printing the first-run setup token.

Paths can be overridden through environment variables:
`BOTENCLAVE_ROOT`, `BOTENCLAVE_MANAGER_USER`, `BOTENCLAVE_VENV`.

Then continue with [first-run setup](#4-first-run-setup).

## 3. Manual setup

The same steps by hand:

```sh
git clone https://github.com/maguro923/BotEnclave.git
cd BotEnclave
sudo python3 -m venv /usr/local/lib/botenclave/venv
sudo /usr/local/lib/botenclave/venv/bin/pip install .

# the manager user and the data root
sudo useradd --system --home /srv/botenclave --shell /usr/sbin/nologin botenclave
sudo mkdir -p /srv/botenclave && sudo chown botenclave /srv/botenclave

# helper entry point, sudoers rule and the manager unit
sudo /usr/local/lib/botenclave/venv/bin/botmgr --root /srv/botenclave \
    systemd install --manager-user botenclave

# start the manager
sudo systemctl enable --now botenclave
```

From here on, `botmgr` means
`/usr/local/lib/botenclave/venv/bin/botmgr`. In this layout the
manager talks to the helper through sudo (`--transport sudo`, already
set in the installed unit) and bots can later be moved under
individual systemd units with `botmgr systemd install-bot <bot>` plus
`--bot-runtime systemd`. `botmgr systemd render-manager` /
`render-bot` print the unit files for review without installing
anything. Full details, including per-bot units and their resource
limits, are in [systemd.md](systemd.md).

## 4. First-run setup

The manager listens on `127.0.0.1:8000` only — it is not reachable
from other machines — and serves HTTPS with a self-signed
certificate generated at first start (there is no HTTP listener).
On the host itself, open `https://127.0.0.1:8000/`. From your own
machine, use an SSH tunnel (also the recommended way to reach the
panel day to day; section 6 covers the details):

```sh
ssh -L 8000:127.0.0.1:8000 <user>@<host>
# then open https://127.0.0.1:8000/ in your local browser
```

Either way the browser warns once about the self-signed
certificate. That is expected: the certificate is generated on your
host and never leaves it, and its SHA-256 fingerprint is printed in
the manager's log at startup if you want to compare
(`journalctl -u botenclave | grep tls:`).

On a headless server the tunnel may not be the most convenient
route: you can instead whitelist your own machine's address in
`access.toml` and open `https://<server-address>:8000/` directly —
see section 6.

1. Open the panel as above. With no administrator existing yet, you
   are redirected to the setup page.
2. Enter the **setup token**. The installer prints it at the end of
   the run; it is also printed in the manager's log at first start and
   stored in `<root>/setup-token` (mode 600):

   ```sh
   sudo cat /srv/botenclave/setup-token
   sudo journalctl -u botenclave | grep setup
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

The panel serves HTTPS only, with a self-signed certificate the
manager generates into its data root at first start
(`tls-cert.pem` / `tls-key.pem`, key mode 600, five-year validity,
covering `localhost`, the hostname and the host's addresses as seen
at generation time). Plain HTTP is not served at all: an `http://`
request to the port is refused by the TLS handshake, not
redirected.

### Who may reach the panel: access.toml

The panel always answers on loopback. Which *other* machines may
reach it is controlled by `<root>/access.toml` — created on first
start with mode 600, owned by the manager user, editable only from
a terminal (there is deliberately no web-UI path), applied at
restart. A malformed file makes startup fail rather than fall back
to anything more open. `botmgr --root /srv/botenclave access`
prints the effective policy.

By default the list is empty and the panel does not listen on
network interfaces at all. To let your own PC in — the usual setup
for a headless server — add its address:

```toml
allow = ["192.168.0.10"]
```

then `sudo systemctl restart botenclave` and open
`https://<server-address>:8000/`. Once the list admits anything
beyond loopback, the panel listens on all interfaces and enforces
the list itself: unlisted clients receive 403 before any login
logic runs. Entries may be single IP addresses, CIDR ranges
(`192.168.0.0/24`), or MAC addresses (`aa:bb:cc:dd:ee:ff`). A MAC
entry is matched through the kernel ARP table, so it only applies
to IPv4 clients on the same subnet, and a MAC is trivially
spoofable on a LAN — treat it as a convenience identifier for a
home network, never as authentication.

`allow_all = true` disables the restriction entirely: any machine
that can route to this host — including, behind a router port
mapping, the whole internet — can open the login page, and HTTPS,
the password and TOTP are the only protection. Prefer listing
addresses.

Two interactions to know: enforcement uses the client address as
the manager sees it, so behind a reverse proxy every visitor
appears as the proxy itself (`X-Forwarded-For` is not trusted);
and if the host's address has changed since the certificate was
generated, delete `tls-cert.pem` and `tls-key.pem` and restart to
regenerate it with the current addresses. Session cookies are
`Secure`, which HTTPS satisfies on any address, so logins hold on
LAN addresses too.

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
- [ ] Manager restarts cleanly (`systemctl restart botenclave`) and
      running bots are reconciled — adopted if still alive, marked
      crashed if gone.

## 8. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Manager exits with “already running” | Another `botmgr serve` holds the lock on this root, or a previous one is still alive. Check `pgrep -af botmgr` before assuming a stale lock. |
| Bot fails to start with permission errors | The package directory or a release/venv is not readable/executable by the bot's OS user. Reinstall into a world-readable location (see [§2](#2-automated-setup-recommended)); do not `chmod` a private home directory open as a workaround. |
| Bot sits in `secret-pending` | Its environment values are not resolvable: the memory-only backend lost them on manager restart (re-enter them), or the bot runs under a systemd unit with a non-systemd-creds backend (switch the backend in the Environment section). |
| Login page loops back after signing in | Cookie not being sent: you are on plain HTTP with `Secure` cookies. Use HTTPS / a proxy (§6), or `--no-cookie-secure` for local development only. |
| Deploy rejects a zip | It contains absolute paths, `..` entries or symlinks, or exceeds the size limit. Repack with plain relative file paths. |
| Deploy cannot use a git URL | Only `https`, `ssh` and `git@` URLs are accepted, and local file transport is disabled by design. Mirror the repository to a reachable remote. |
| Console shows nothing / bot seems mute | A console connection pauses log capture but not the bot. Disconnect the console and check **Logs**; also confirm the bot writes to stdout/stderr — output redirected to its own files is not captured. |

## 9. Uninstall

Stop the manager, stop all bots, then remove the units and files:

```sh
sudo systemctl disable --now botenclave
sudo botmgr --root /srv/botenclave bot-list             # stop/remove bots first
sudo rm -f /etc/systemd/system/botenclave*.service \
           /etc/sudoers.d/botenclave
sudo systemctl daemon-reload
sudo rm -rf /usr/local/lib/botenclave /srv/botenclave
# per-bot OS users remain; remove with: sudo userdel bm-<name>
```

Take a backup (§7) before uninstalling if there is any chance you
will want the bots' `data/` directories back.
