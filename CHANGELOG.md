# Changelog

## Unreleased

- Added `scripts/install.sh`: one-shot production setup
  (prerequisites, package, manager user, systemd unit, first start).
  Idempotent.
- Removed the quickstart (single-host) layout from the documentation.
  The systemd layout is the one supported setup, and the installer
  script performs it.
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
