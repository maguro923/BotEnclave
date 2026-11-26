# BotEnclave

A security-first, Docker-free web panel for managing multiple Discord bots.

日本語版: [README.ja.md](README.ja.md)

BotEnclave lets a small, trusted group run several Discord bots on one
Linux host and manage them from one web UI: deploy from git or a zip,
start/stop, watch logs, use an interactive console, roll back releases —
while every bot stays isolated in its own OS user, its own files, and
its own secrets.

## Who this is for — and who it is not for

BotEnclave is built for **a small group of people who know each other**
(an admin plus invited guests) running their own bots on their own
host. Guests can deploy and run arbitrary code inside their bots' OS
users, so **a guest must be someone you would trust with code
execution**. This is not a public hosting service and must not be
offered to untrusted strangers.

## Features

- **Python (discord.py) native, no Docker** — bots run as plain
  processes; works on a small VPS, a Raspberry Pi, or restricted
  containers where Docker is unavailable.
- **Git-native deploys** — deploy from a git URL or an uploaded zip;
  each deploy builds a per-release virtualenv, verifies the entry
  point, and atomically switches `current`. Roll back to the previous
  release in one click. Signed per-bot webhooks redeploy on push.
- **Isolation by default** — one dedicated OS user per bot, strict
  directory permissions, per-bot tmux servers for the console, and a
  small privileged helper that performs the few root operations through
  a fixed, validated operation set. The web app itself never runs as
  root.
- **Secrets that stay secret** — per-bot environment variables with
  three storage backends. On systemd hosts (the standard), values are
  encrypted with systemd-creds and decrypted by PID 1 into the bot's
  unit (`LoadCredentialEncrypted`); the panel never sees the plaintext.
  Saved values are never shown again, and known values are masked in
  log views.
- **Accounts with real guardrails** — invite-only users, admin/guest
  roles with per-bot ownership enforced on every route (IDOR-tested),
  Argon2id passwords, TOTP two-factor with single-use backup codes,
  login lockout, guest quotas, CSRF protection, and a hash-chained,
  mirrored audit log.

## Requirements

- Linux (or WSL2)
- Python 3.11+
- `tmux` and `git` on the host
- systemd and sudo (the supported layout runs the manager under
  systemd and reaches the helper through sudo)

## Install

```sh
git clone https://github.com/maguro923/BotEnclave.git
cd BotEnclave
sudo sh scripts/install.sh
```

The installer sets up the production layout: prerequisites
(Debian/Ubuntu), the package in `/usr/local/lib/botenclave/venv` (a
location the bot users can read — they load the launcher shim and the
tmux configuration from it), the `botenclave` manager user, the data
root `/srv/botenclave`, the systemd unit, and the first start. It is
idempotent; the manual steps are in [docs/setup.md](docs/setup.md).

## First steps

1. The panel listens on `127.0.0.1:8000` only, over HTTPS. Open
   `https://127.0.0.1:8000/` on the host, forward it over SSH
   (`ssh -L 8000:127.0.0.1:8000 <user>@<host>`) and open the same
   address locally, or whitelist your machine's address in
   `access.toml` — you are redirected to the first-run setup. The
   certificate is self-signed and generated on the host, so the
   browser warns once. The
   setup token is printed at the end of the install run and stored
   in `/srv/botenclave/setup-token` (mode 600). Section 6 of
   [docs/setup.md](docs/setup.md) explains the access model.
2. Create the administrator account, then sign in.
3. **Add a bot** on the dashboard (name + command), open its page, and
   deploy code from the **Deploy** section (git URL or zip upload).
   A deploy detects the entry file (`main.py`, `bot.py`, `app.py`,
   `run.py`), builds a virtualenv from `requirements.txt`, and points
   the bot's command at it.
4. Set the bot's environment (e.g. `DISCORD_TOKEN`) in the
   **Environment variables** section, then press **Start**.
5. Watch the log, or open the interactive console. While a console is
   connected, log capture pauses (private mode) so credentials typed
   there never land in log files.

Serve over HTTPS (or behind a TLS-terminating proxy) outside a trusted
LAN: session cookies are `Secure` by default, and `--no-cookie-secure`
exists only for local development.

## Layout (systemd)

The layout the security design assumes — manager as its own
unprivileged user, helper via a narrow sudoers rule, one systemd unit
per bot — is described in [docs/systemd.md](docs/systemd.md).

## Secret backends

Chosen per bot in the Environment section:

| Backend | Storage | Notes |
|---|---|---|
| systemd-creds (standard) | `.env` encrypted with the host key | Decrypted by the helper at start, or by PID 1 under systemd units. Bound to the host: cannot be decrypted after a host migration. |
| Fernet | `.env` encrypted with the manager's key file | Fallback for non-systemd hosts. Weaker: whoever takes both the key file and the store can decrypt. |
| Memory only | Nothing on disk | Values are lost when the manager restarts; the bot waits in `secret-pending` until they are re-entered. |

A dotenv compatibility mode (writing the values to `data/.env` at
start) exists for bots that cannot read environment variables; it is
off by default and visibly flagged, because it leaves plaintext on
disk.

## Documentation

- [docs/setup.md](docs/setup.md) — step-by-step setup guide, from
  host prerequisites to the first running bot
- [docs/architecture.md](docs/architecture.md) — design principles,
  process model, and the security design in one place
- [docs/systemd.md](docs/systemd.md) — production install with systemd
- [docs/backup.md](docs/backup.md) — backup, restore, audit retention
- [docs/upgrading.md](docs/upgrading.md) — upgrading without stopping bots
- [SECURITY.md](SECURITY.md) — reporting vulnerabilities, threat model summary

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest          # full suite (privileged tests need root)
.venv/bin/ruff check botmgr tests
.venv/bin/mypy botmgr
```

Development is test-driven; the security requirements ship with
negative tests (cross-user access is refused everywhere, hostile bots
and archives cannot escape their sandbox). CI runs the same checks on
standard GitHub-hosted runners.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
