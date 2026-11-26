# Changelog

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
