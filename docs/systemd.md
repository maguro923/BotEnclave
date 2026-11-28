# systemd deployment

BotEnclave's standard layout on a systemd host:

- a dedicated **manager user** (e.g. `botenclave`) runs the web app as a
  normal, unprivileged service;
- the **privileged helper** is a root-owned entry point the manager may
  invoke only through a narrowly-scoped sudoers rule;
- each bot runs as its own OS user inside its own systemd unit, so the
  bot's processes live in a cgroup with `MemoryMax` / `CPUQuota`, and —
  with the standard secret backend — PID 1 decrypts the bot's
  credential directly into the unit's private credentials directory
  (`LoadCredentialEncrypted`). Neither the manager nor the helper ever
  sees the plaintext in this mode.

## Install

Run as root, from a checkout of this repository installed at a
system-wide location readable by the bot users (the examples use
`/usr/local/lib/botenclave/botmgr` as the package directory and
`/srv/botenclave` as the data root):

```sh
# 1. the manager user and the data root
useradd --system --home /srv/botenclave --shell /usr/sbin/nologin botenclave
mkdir -p /srv/botenclave && chown botenclave /srv/botenclave

# 2. helper entry, sudoers rule, manager unit
botmgr --root /srv/botenclave systemd install --manager-user botenclave

# 3. start the manager; finish the first-run setup in the browser
systemctl enable --now botenclave
journalctl -u botenclave | grep setup   # where the setup token file lives
```

`botmgr systemd render-manager` / `render-bot` print the same files for
review without installing them.

## Per-bot units

Bots are created from the panel as usual (the helper creates the OS
user). To move a bot under systemd:

```sh
botmgr --root /srv/botenclave systemd install-bot <bot>
```

and run the manager with `--bot-runtime systemd` (the installed
manager unit takes its flags from `render-manager`; regenerate it or
edit the `ExecStart` line). In this mode:

- starting a bot writes a secret-free `launch.json` into the bot's
  folder and starts `botenclave-bot-<bot>.service`;
- the unit's main process (`botmgr.botunit`) starts the bot's tmux
  server exactly like the helper path does, so the interactive console,
  log capture and private mode work unchanged;
- stopping stays graceful: the manager signals the pane's process tree
  through the helper first, then stops the unit.

**Secret backend requirement:** through a unit, secrets can only be
delivered by `LoadCredentialEncrypted`, so the bot must use the
**systemd-creds** backend. Starting a bot whose environment is stored
with the Fernet backend is refused with an explanation —
writing those values into `launch.json` would put plaintext secrets on
disk. Switch the backend in the bot's Environment section first.

## Notes

- The manager unit sets `Restart=on-failure`, `MemoryMax=1G` and
  `OOMScoreAdjust=-500` so the kernel prefers killing a bot over the
  manager. Bot units default to `MemoryMax=512M`, `CPUQuota=100%`,
  `OOMScoreAdjust=500`; adjust with `systemd install-bot --memory-max
  --cpu-quota`, or a drop-in.
- Manager logs go to the journal (`journalctl -u botenclave`); the
  audit log is a file with its own retention (see backup.md).
- Optional extra hardening for bot units (drop-ins): restrict outbound
  traffic with `IPAddressAllow`/`IPAddressDeny` to the APIs the bot
  needs, and hide other bots' folders with `InaccessiblePaths`. These
  are deliberately not defaults: the right values depend on the bot.
