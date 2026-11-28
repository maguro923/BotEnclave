# Architecture

This document distills BotEnclave's design: the principles, the process
model, and the security design. It is the public summary of the
project's internal design and implementation plans.

## The one principle

**No bot — hung, flooding, or hostile — may ever stop the panel, the
supervisor, or another bot.** Every design decision is downstream of
that:

- One bot = one OS process (its own interpreter; the GIL is never
  shared with the manager).
- The manager never blocks on a bot: helper calls, deploys, and file
  operations run off the event loop, with timeouts everywhere.
- Bot output flows PTY → tmux pipe-pane → a plain log file. The panel
  reads files; it never sits in a bot's output path.

## Process model

```
browser ──► manager (FastAPI, unprivileged service user)
              │  fixed JSON protocol over sudo (production) 
              ▼
            privileged helper (root, small, validates everything)
              │  su to the bot's user
              ▼
            per-bot tmux server ──► launcher shim ──► bot process
```

- The **manager** owns state: users (a separate auth database),
  bot registry and states (SQLite), releases, secrets metadata, and a
  hash-chained audit log mirrored into SQLite.
- The **helper** is the only root component. Its operation set is a
  fixed enumeration (create/delete OS user, start/stop, console,
  capture, file operations, disk usage); every argument is validated
  there, paths are derived from the bot name by the helper itself, and
  external commands run with fixed argv and no shell.
- The **launcher shim** receives the launch configuration exactly once
  (a one-shot FIFO in the helper path; files + the credentials
  directory in the systemd path), applies rlimits, sanitizes the
  environment (interpreter-control variables such as `PYTHONPATH` and
  `LD_*` are stripped), and `exec`s into the bot.
- **tmux** is used for one reason: a persistent PTY for the
  interactive console. Every bot takes the same path whether or not
  anyone ever opens its console.
- Process identity is (pid, start-time, boot-id) from `/proc`, so a
  recycled PID is never mistaken for the bot. Stopping re-verifies the
  identity, then signals the whole process tree: SIGTERM, a grace
  period, SIGKILL, and a descendant sweep.

Under systemd, each bot additionally gets its own unit: the unit's
main process starts the same tmux/shim chain, and the bot lives in the
unit's cgroup (`MemoryMax`, `CPUQuota`). See systemd.md.

## Bot lifecycle

A per-bot state machine (`stopped → starting → running → stopping`,
plus `crashed`, `backoff`, `degraded`, `attention`, and
`secret-pending`) drives supervision:

- Restarting after an unexpected exit is an operator policy per bot:
  `none` (never), `once` (one retry after 5 seconds — the default), or
  `three` (retries after 5s, 15s and 60s). When the budget is spent
  the bot parks in `attention` for a human. A run that survives 60
  seconds counts as a success and resets the budget.
- A run that dies within the **startup phase (10 seconds of launch)**
  is never restarted, whatever the policy: startup failures (import
  errors, bad configuration) are deterministic, and retrying them
  would only churn the log. It parks in `attention` immediately.
- A bot can opt into **autostart** (default off): when the manager
  itself starts, such bots are started automatically.
- On manager restart, **reconcile** rebuilds reality from the store
  plus the helper's live view: running bots are adopted; a bot found
  dead is counted as a startup-phase failure (its death time is
  unknown, so there is no blind retry — autostart is the explicit way
  to bring it back); log capture that fell off is re-attached.
- `secret-pending` is not a failure: it means the bot's secrets are
  unavailable to this manager process (the stored artifact is missing,
  or the bot still uses a backend this build no longer supports) and
  the bot waits for its owner to re-enter them.

## Data layout and permissions

Everything for one bot lives under `bots/<bot>/`:

| Path | Owner | Mode | Notes |
|---|---|---|---|
| `bots/` | manager | 0711 | traverse-only: bots cannot list each other |
| `bots/<bot>/` | bot user | 0710 | manager group may traverse |
| `releases/`, `venvs/` | manager | 0750 (bot group r-x) | bots can run their code, never rewrite a release |
| `data/` | bot user | 0700 (strict) | the bot's cwd and only writable area besides logs |
| `logs/` | bot user | 0750 (manager group r) | `console.log`, copy-truncate rotation, 5 MiB × 5 |

Deploys build a new release directory plus a per-release virtualenv,
verify the entry point's syntax in a separate process, then switch
the `current` symlink. Two generations are kept (active + previous),
which is what rollback uses.

Panel editing works on a staged copy of the active release under
`edit/`; committing the session publishes it as a new release, and
the commit is refused if the active release moved on in the
meantime. A bot with no release yet gets an empty staging area
instead, so its first release can be written by hand in the panel;
a release deployed in the meantime likewise refuses the commit.

## Security design

**Threat model.** Assets: bot secrets (e.g. Discord tokens), panel
control, other bots' code and data, the host itself. Attackers: an
internet third party (including content that flows through a bot's
logs into an admin's browser), a *guest user* probing other people's
bots (IDOR is the headline attack), a malicious or compromised bot,
and a malicious repository or zip. The panel serves a small trusted
group; it is not a public multi-tenant hoster.

**Authorization.** One dependency (`bot_access`) resolves every
bot-addressed route and applies one rule — owner or admin, otherwise
404 (existence is hidden). A startup scan refuses to boot if any
bot-addressed route lacks the guard. WebSocket streams are filtered
per subscriber. Mutating API calls require a double-submit CSRF
token; WebSockets check `Origin`.

**Authentication** lives in a separate package (`botmgr/auth`) built
on FastAPI Users with database-backed opaque session tokens (so an
admin can revoke sessions), Argon2id hashing, invite-only account
creation, admin-issued temporary passwords with forced change and an
issue rate limit (never for admin accounts — a panel admin must not
take over a peer admin; admin recovery is the terminal's
`botmgr reset-password`), login lockout (5 failures → 15 minutes,
released by an admin or `botmgr unlock`), and TOTP with replay
protection plus single-use hashed backup codes.

**Secrets.** Per bot, one of three backends (see the README table);
the default for new bots is `systemd-creds`. Writing a systemd-creds
credential is delegated to the privileged helper: encrypting with the
host key requires interactive authentication from an unprivileged
caller, which the manager process does not have. (Decryption never
touches the manager at all — PID 1 does it via
`LoadCredentialEncrypted`.) Invariants: values are injected per bot, only at start, through the
one-shot channel; never via `tmux -e` or session environment (those
leak through `/proc` and `show-environment`); never into the bot
registry database, the audit log, or deploy logs (credential-bearing
URLs are redacted); never re-displayed after saving. The Discord
token, when present, is verified against the Discord API at save time
and a token registry warns when the same token is registered to two
bots. The registry stores only an iterated HMAC-SHA-256 fingerprint of
the token, keyed with a server-side secret kept in
`secrets/token-hash.key` (mode 600) — separate from the database — so
a database copy alone cannot be used to test candidate tokens against
it.

**Inputs and files.** Zip extraction rejects traversal, absolute
paths, symlinks, and size bombs, in a worker process. Git clones are
limited to https/ssh schemes with option-injection guards and the
file protocol disabled. The web file manager is confined by a zone
model (data/ editable; logs/ and releases/ read-only; venvs/ hidden),
never follows symlinks, re-checks the opened fd's real path, and
writes atomically; the same validation runs independently in the
manager and in the helper.

**Audit.** Logins, bot lifecycle operations, deploys and rollbacks,
environment changes (never values), console connect/disconnect
(private mode), user administration, and webhook activity are
recorded in an append-only, hash-chained file, mirrored into SQLite,
retained for 180 days by default with chain-preserving purges.
Admins see everything; guests see the entries for their own bots.

**Known limits, stated plainly.** OS-user separation protects bots
from each other and from the panel, not the host from a bot: a bot
runs arbitrary code as its own user on your machine, so deploy
BotEnclave itself on a dedicated host or VM for anything you do not
fully trust. Root on the same host can tamper with the audit chain
(the mirror makes it detectable, not impossible). The Fernet backend
is only as strong as the key file's protection. The systemd-creds
backend cannot migrate across hosts by design.
