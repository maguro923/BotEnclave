# Upgrading

Bots are independent processes: upgrading the manager never requires
stopping them. The manager restarts, reconciles against the helper's
view of the world, and adopts the bots that are still running.

## Procedure

1. **Take a backup first** (this is the rollback path):
   ```sh
   botmgr --root /srv/botenclave backup /var/backups/botenclave
   ```
2. Install the new package version over the old one (same location).
   If you run per-bot systemd units or the helper entry, re-run the
   installers so the rendered files match the new version:
   ```sh
   botmgr --root /srv/botenclave systemd install --manager-user botenclave
   botmgr --root /srv/botenclave systemd install-bot <bot>   # per bot
   ```
3. Restart only the manager:
   ```sh
   systemctl restart botenclave
   ```
   Database migrations (both the main store and the auth package's)
   run automatically at startup and are additive.
4. Check the dashboard: running bots should show as adopted/running;
   the audit log records what reconcile found.

## If something goes wrong

Restore the backup into place and reinstall the previous package
version:

```sh
systemctl stop botenclave
botmgr --root /srv/botenclave restore <archive> --force
# reinstall the previous package version
systemctl start botenclave
```

The pre-restore tree is kept at `<root>.pre-restore-<timestamp>` for
inspection; delete it manually once the rollback is confirmed good.
