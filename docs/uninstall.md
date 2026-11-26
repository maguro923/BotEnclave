# Uninstalling BotEnclave

`scripts/uninstall.sh` removes BotEnclave in two strengths. Run it
as root; it does not need to run from the repository checkout.

```bash
sudo sh scripts/uninstall.sh           # remove the program, keep data
sudo sh scripts/uninstall.sh --purge   # remove everything
```

Both forms are idempotent: pieces that are already gone are skipped.

## Default: remove the program, keep the data

The default run:

1. Stops and disables every bot unit
   (`botenclave-bot-<bot>.service`) and the manager
   (`botenclave.service`).
2. Deletes those unit files and `/etc/sudoers.d/botenclave`, then
   reloads systemd.
3. Deletes the install directory (the venv and the helper entry
   point, default `/usr/local/lib/botenclave`).

It deliberately **keeps**:

- the data root (default `/srv/botenclave`) — settings, databases,
  the audit log, TLS material, encrypted secrets and every bot's
  files;
- the OS users — the manager user and every `bm-<bot>` bot user.

Reinstalling with `scripts/install.sh` on top of a kept data root
brings the panel back exactly as it was (see
[upgrading.md](upgrading.md)); no bot needs to be re-registered.

## --purge: remove everything

`--purge` additionally:

4. Deletes the bot users' tmux sockets and the `bm-<bot>` users.
5. Deletes the data root, including all bot files and secrets.
6. Deletes the manager user.

Purging is **irreversible**: bot code that exists only inside the
data root, environment values and encrypted credentials are gone.
Take a backup first if any of it matters ([backup.md](backup.md)).
When run in a terminal, `--purge` asks you to type `YES` before it
deletes anything.

## Overrides

The script honours the same environment overrides as the installer:
`BOTENCLAVE_ROOT`, `BOTENCLAVE_MANAGER_USER` and `BOTENCLAVE_VENV`.
Use them if the install used non-default locations.

## Verifying the removal

```bash
systemctl status botenclave            # Unit ... could not be found
ls /etc/sudoers.d/botenclave           # No such file or directory
ls /usr/local/lib/botenclave           # No such file or directory (or kept out)
getent passwd botenclave               # only after --purge: no output
ls /srv/botenclave                     # only after --purge: No such file
```
