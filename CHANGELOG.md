# Changelog

## Unreleased

- Fixed: deploy activation failed with `Permission denied`
  (Errno 13) after a successful build. Switching the `current`
  symlink happens inside the bot-owned folder, which the manager
  may only traverse; the swap now goes through the privileged
  helper (`point_current`), atomically as before.
- Fixed: opening the console of a bot that is not running raised
  an exception (tmux has no server to capture) and logged a
  traceback. A missing tmux server now reads as an empty pane.
- Fixed: Git/ZIP deploys failed with `PermissionError: [Errno 1]`
  when the panel ran with per-bot OS users. The deploy worker
  (running as the manager user) tried to hand the built release to
  the bot's group itself, but a non-owner `chown` is not permitted
  and the manager is not a member of the bot's group. Publishing a
  release now goes through the privileged helper like every other
  root operation; the worker only builds.
- Fixed: the live console could not connect on fresh installs
  (`No supported WebSocket library detected`, console requests
  answered 404). The `websockets` package is now a dependency.
- Changed: the manager's operational logging is unified on Python
  logging to standard error — under systemd, the journal
  (`journalctl -u botenclave`) — covering the server's own messages
  and uvicorn's in one format. The audit trail (`audit.log`) and
  per-bot logs are unchanged.

- Changed: the self-signed TLS certificate is now short-lived —
  31-day validity, renewed roughly monthly. When 7 days or less
  remain (checked at startup and daily while running) a fresh key
  pair replaces both files atomically, with the address list
  recomputed; a failed renewal keeps the old pair serving and is
  retried, never taking the panel down. Under systemd the manager
  restarts itself to apply a renewal (the manager unit is now
  `Restart=always`); elsewhere the new pair applies at the next
  restart. Certificates issued before this change (multi-year
  validity) are replaced once at startup, so existing installs
  converge on the monthly cycle.
- Added: usernames. The first-run setup registers a username next
  to the email address, it can be changed under Account, and it is
  shown in the header and the user list. Email remains the login
  identifier.
- Added: panel languages — English and Japanese. The language is
  chosen at first-run setup and can be changed at any time by an
  administrator under the new Settings page; it applies to the
  whole panel.
- Added: password confirmation on the setup form (a mismatch is an
  error), show/hide buttons on the password fields of the login,
  setup and account forms, a light/dark theme toggle (light by
  default, remembered per browser), and the service name in the
  header of every page, including login and setup.

- Fixed: a malformed `access.toml` (for example an unquoted IP
  address) made the manager exit and systemd restart it in a loop,
  taking the panel down entirely — including on loopback. The
  manager now falls back to the loopback-only policy and logs the
  parse error prominently; the fallback can only be more
  restrictive than intended, never more open. `botmgr access`
  remains strict and is the documented way to validate the file
  before restarting.
- Fixed: re-running the installer silently reused everything. It now
  lists the existing settings and data found in the data root and
  states that nothing is overwritten, replaces a still-unused setup
  token with a fresh one (previously the first token stayed valid
  until setup completed, across any number of reinstalls), and keeps
  a manager bind (`--host`/`--port`) previously set with
  `botmgr systemd install` instead of resetting it to the defaults;
  the health check and the printed URL now use that port.

- Added: panel access control via `<root>/access.toml` (mode 600,
  terminal-edited, applied at restart, fail-closed on malformed
  input). Loopback is always allowed; the `allow` list admits
  further clients by IP address or CIDR range, and admitting
  anything beyond loopback widens the bind to all interfaces while
  the manager enforces the list itself with 403 responses.
  `allow_all = true` disables the restriction. `botmgr access`
  prints the effective policy.
- Added: the panel now serves HTTPS with a self-signed certificate
  generated locally at first start (`tls-cert.pem` / `tls-key.pem`
  in the data root, five-year validity, covering localhost, the
  hostname and the host's current addresses). Plain HTTP is no
  longer served at all — the port refuses non-TLS requests instead
  of redirecting them. This also makes the rebinding option in the
  setup guide fully usable, since `Secure` session cookies now hold
  on any address.

- Added `scripts/install.sh`: one-shot production setup
  (prerequisites, package, manager user, systemd unit, first start).
  Idempotent.
- Removed the quickstart (single-host) layout from the documentation.
  The systemd layout is the one supported setup, and the installer
  script performs it.
- Documentation: the setup guide and both READMEs now state the
  loopback-only bind up front instead of sending the reader to
  `http://<host>:8000/`, present the SSH tunnel as the recommended
  way in, and spell out why rebinding alone is not a working setup
  (login page reachable, but credentials unencrypted and `Secure`
  cookies dropped over plain HTTP at a non-localhost address).
- Fixed a pre-UI-change bug-hunt pass (37 new regression tests):
  supervision now survives one bot's helper failure instead of stopping
  for every bot, and a failed stop stays retryable; the manager group
  is wired into bot directory ownership so the panel can read bot logs
  in the systemd layout; private console mode can no longer be
  overridden by the watchdog's capture repair; process sweeps verify
  start times before SIGKILL; first-run setup is atomic under
  concurrency; short passwords return 400 instead of 500; lockouts
  consume the failure count and second-factor failures count toward
  them; a torn audit line no longer breaks viewing or startup, and a
  full retention purge keeps the hash chain valid across restarts;
  downloads support non-ASCII filenames; bot creation no longer
  confirms other users' bot names; deploy permission publishing never
  follows symlinks; git credentials are stripped from the cloned
  release's `.git/config`; a failed activation restores the previous
  release; the deploy worker timeout exceeds its stage budgets and a
  timeout kills the whole worker tree; panel-written files belong to
  the bot user; secret backend migration cannot strand a bot with an
  empty environment; backup restore sanitizes manifest paths; the
  Fernet key is created atomically; `bot-remove` (CLI) deletes the
  bot's secrets like the web path does; deleting a bot unregisters it
  from the supervisor, so the watchdog stops polling it and its name
  can be reused without restarting the manager; the delete flow now
  offers the file-handling choice PLAN 2.4 calls for (files kept by
  default, `?purge_files=true` / `--purge-files` removes the bot's
  whole directory tree through the helper).

## 0.1.0 — 2026-11-26

First release.

- Web panel: dashboard, bot detail with live log view and an
  interactive console (xterm.js, vendored), user management, audit
  viewer. First-run setup with a one-time setup token.
- Supervision core: per-bot state machine with crash backoff and an
  attention state, process identity verification via `/proc`, graceful
  stop with descendant sweep, reconcile on manager restart, watchdog
  with log-capture self-repair and RSS/disk reporting.
- Isolation: one OS user per bot, a small privileged helper with a
  fixed validated operation set, per-bot tmux servers, strict
  directory permissions.
- Deploys: git clone or zip upload in a worker process, entry-point
  detection and syntax verification, per-release virtualenvs, atomic
  `current` switching, two-generation GC, one-click rollback, and
  per-bot HMAC-signed git webhooks (timestamp replay window).
- Secrets: systemd-creds (standard; helper-decrypted at start, or
  PID 1 via `LoadCredentialEncrypted` under systemd units), Fernet,
  and memory-only backends with a `secret-pending` state; optional
  dotenv compatibility mode; Discord token verification and duplicate
  warnings; log masking at display time.
- Accounts: invite-only admin/guest roles, per-bot ownership enforced
  on every route (IDOR test suite), Argon2id, TOTP with backup codes,
  login lockout and rate limits, temporary passwords, guest quotas,
  CSRF and WebSocket origin checks.
- Operations: hash-chained mirrored audit log with retention purges,
  `botmgr` CLI (serve, unlock, audit verify/purge, backup/restore,
  systemd installers), systemd units for the manager and per bot,
  backup/restore with a hash manifest, and an upgrade procedure that
  keeps bots running.
