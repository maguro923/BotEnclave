# BotEnclave

A security-first, Docker-free web panel for managing multiple Discord bots.

BotEnclave lets you register, deploy, start, stop, and watch Discord bots from
one web UI. Each bot runs as its own OS process under its own dedicated OS
user, with its files, logs, and secrets isolated from every other bot.

## Status

Under active design and early development. Not yet usable: the design and
implementation plan were written first, and the code is being built milestone
by milestone with test-driven development.

## Highlights

- **Python (discord.py) native** — built first for the Python bot ecosystem;
  other runtimes are planned through a runtime-adapter layer.
- **No Docker required** — runs on a small VPS, a Raspberry Pi, or in
  restricted containers where Docker is unavailable.
- **Git-native deploys** — register a bot from a git repository or an uploaded
  file set, redeploy on push via signed webhooks, and roll back to the
  previous release.
- **Isolation by default** — one OS user per bot, per-bot tmux sessions, a
  small privileged helper for the few operations that need root, and secrets
  the panel itself never sees in plaintext on systemd systems
  (systemd-creds / LoadCredentialEncrypted).
- **Security as a feature** — password + TOTP login, invite-only users with
  admin/guest roles, a hash-chained audit log, and a published threat model.

## Requirements

- Linux (or WSL2)
- Python 3.11+

## License

Apache License 2.0 — see [LICENSE](LICENSE).
