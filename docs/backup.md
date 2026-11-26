# Backup and restore

## What a backup contains

`botmgr backup` produces a single `.tar.gz` with a hash manifest:

- both databases (`botmgr.db`, `auth.db`), snapshotted through SQLite's
  backup API so they are consistent even while the manager is running;
- the key files (`secret.key`, `web-secret`) — keys and databases are
  always backed up **together**; a database without its key cannot
  decrypt the Fernet stores or TOTP secrets;
- the whole `secrets/` directory (per-bot systemd-creds ciphertexts,
  Fernet-encrypted environments, webhook secrets);
- the audit log;
- every bot's `data/` and `logs/`.

`releases/` and `venvs/` are excluded by default (git-sourced releases
can be re-fetched and re-deployed); pass `--include-releases` to take
them too.

```sh
botmgr --root /srv/botenclave backup /var/backups/botenclave
botmgr --root /srv/botenclave backup /var/backups/botenclave --include-releases
```

## Restore

```sh
botmgr --root /srv/botenclave restore /var/backups/botenclave/botenclave-backup-YYYYMMDDTHHMMSS.tar.gz
```

Restore verifies every file against the manifest before moving
anything into place, refuses to overwrite an existing installation
unless `--force` is given (the old tree is then moved aside to
`<root>.pre-restore-<timestamp>`, never deleted), and restores
owner-only permissions on the secret material.

After a restore, restart the manager. Bots that were running are
reconciled on startup as usual.

## The systemd-creds host-binding caveat

systemd-creds ciphertexts are bound to the **host key**
(`/var/lib/systemd/credential.secret`). A backup restored onto a
different host cannot decrypt them: bots using the systemd-creds
backend will need their environment re-entered after a host migration
(the panel shows their keys; values must be supplied again). Fernet
material and webhook secrets restore on any host — which is exactly
why the backup archive itself must be stored as carefully as the
secrets it carries. The host key is intentionally *not* part of the
backup: exporting it would defeat the binding.

## Retention

Backups are operator-managed (no built-in rotation). The audit log has
its own retention: entries older than 180 days are purged at manager
startup (configurable via `AppConfig.audit_retention_days`), keeping
the hash chain verifiable from the purge anchor. Verify a chain at any
time with:

```sh
botmgr --root /srv/botenclave audit verify
botmgr --root /srv/botenclave audit purge --days 180
```
