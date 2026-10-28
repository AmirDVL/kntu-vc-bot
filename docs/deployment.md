# Deploy on Debian with systemd

The deployment uses a dedicated `kntu-vc-bot` system user. Source releases and the virtual environment live under `/opt/kntu-vc-bot`. Each release's `.data` directory is a symlink to `/var/lib/kntu-vc-bot`, so verification receipts survive code updates. The service uses a private temporary directory for staged documents.

## Layout

```text
/opt/kntu-vc-bot/
  current -> releases/<git-commit>
  releases/<git-commit>/
    .data -> /var/lib/kntu-vc-bot
  venv/
/etc/kntu-vc-bot.env          # root-owned, mode 0600
/var/lib/kntu-vc-bot/        # service-owned, mode 0700
/etc/systemd/system/kntu-vc-bot.service
```

The environment file uses the canonical keys from `.env.example`: `KNTU_USERNAME`, `KNTU_PASSWORD`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_USER_ID`. It is read by systemd, not placed in the repository or source release. SSH credentials and the private key stay on the operator's machine and must never be committed.

## Service operations

```sh
sudo systemctl status kntu-vc-bot --no-pager
sudo journalctl -u kntu-vc-bot -n 30 --no-pager
sudo systemctl restart kntu-vc-bot
sudo systemctl stop kntu-vc-bot
```

The service is enabled at boot and restarts after process failures. Its systemd unit is in [`deploy/kntu-vc-bot.service`](../deploy/kntu-vc-bot.service). File permissions, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, and a dedicated writable state directory limit the process's filesystem access.

Only one process may poll the same Telegram token. Stop a desktop/test instance before starting this service. Do not run live polling from CI.

## Updating

1. Run the tests and offline demo on the intended commit.
2. Put the tracked source files into a new root-owned release directory; do not copy local `.env`, `.data`, screenshots, logs, or the SSH key.
3. Install that release's requirements in the server virtual environment. Check the supported Python runtime and test dependencies before changing a working deployment.
4. Add the `.data` symlink to `/var/lib/kntu-vc-bot`.
5. Allow in-flight submissions to finish, switch `current` to the new release, and restart the service. Reconnect university sessions afterward: they are deliberately not persisted.
6. Verify `systemctl is-active`, initialization logs, and the absence of competing pollers. A live read-only university check should verify network access separately from Telegram startup.

To roll back code, point `current` to the previous release and restart. The virtual environment is shared: dependency changes also require restoring compatible dependencies. Preserve the receipt database; an uncertain attempt must not be erased to make a retry appear safe.

## Operational limits

This is a single-process deployment with automatic process restart, not high availability. Monitor disk space for receipts and temporary uploads, and restrict access to backups containing receipt metadata. Back up SQLite using its backup API or while the service is stopped. Never publish environment contents, request logs, private homework or receipt database files.

A deployment health check does not replace the disposable-assignment submission test described in [verification.md](verification.md).
