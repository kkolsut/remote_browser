# Troubleshooting

Start with `sudo ./scripts/healthcheck.sh`. It points at most of the problems below.

## A user sees "No browser yet"

Authelia let them in, but they have no route. In order:

1. `journalctl -u remote-browser-sync -n 50`: is the user listed as skipped? Possible reasons:
   - the uid is not a safe name (uppercase, a dot or an underscore);
   - no uidNumber/gidNumber;
   - a shared uidNumber;
   - their profile directory belongs to another uid.
2. `sudo ./scripts/validate-freeipa.py`: are they a **direct** member? Nested groups don't count, for
   Authelia either.
3. The sync runs every 5 minutes. `sudo systemctl start remote-browser-sync` runs it now.

## "Your browser is starting" (502) for longer than a minute

- `systemctl status browser-<uid>`: is it running? `sudo podman healthcheck run browser-<uid>`: is it
  healthy? If not, see `journalctl -u browser-<uid>`.
- Is Nginx on the user's network? `sudo podman network inspect rb-net-<uid>` lists `rb-nginx` among its
  containers. A new Nginx container rejoins the networks in its `ExecStartPost`; if that failed
  (`journalctl -u rb-nginx`), run `sudo ./scripts/sync-freeipa-users.py --attach-only`.
- SELinux denials (Rocky/RHEL): `sudo ausearch -m avc -ts recent`.

## The sync aborts

The sync's output says why. Nothing was changed.

- `ABORT: LDAP connect/bind ... failed`: FreeIPA unreachable, the CA certificate in `FREEIPA_CA_DIR`,
  or the bind password in `SECRETS_DIR/freeipa_bind_password`. Check with
  `sudo ./scripts/validate-freeipa.py`.
- `refusing without --force`: the group looks empty, or too many users would lose their browser
  (`SYNC_MAX_REMOVALS`). Check the group in FreeIPA. If the removal is intended, run the sync once with
  `--force`.
- `generated units rejected by Podman's Quadlet generator`: the generator's message follows. Run
  `sudo ./scripts/validate-config.sh`.
- `BROWSER_IMAGE is not set`, or a value refused as unsafe (`only letters, digits and ...`): `.env`.
- `cannot create ... (run the sync as root)`: it was started without `sudo`.

## The browser shows a "profile in use" or "sandbox" problem

- Profile locks after a hard stop are handled by the launcher. If one persists, check that the
  container's hostname is `browser-<uid>` (`HostName=` in its unit).
- Sandbox: `chrome://sandbox` and `about:support` → Sandbox. If it fails after a change to the
  container options, run `tests/phase2-browser.sh`. The `SYS_CHROOT` capability and the seccomp
  profile are both required (`docs/security.md`).

## The weekly image update failed

`journalctl -u remote-browser-image-update`. A failed build or smoke test changes nothing; the
previous image keeps running. To retry: `sudo systemctl start remote-browser-image-update`. To roll
back after a bad rollout: set `BROWSER_IMAGE` to the previous tag (`sudo podman images
localhost/remote-dual-browser`), then run the sync.

## Disk filling up

- `sudo ./scripts/healthcheck.sh` lists the largest profiles.
- Browser caches live in the profiles and are not backed up. Users can clear them in the browser.
- Old images: `sudo podman image prune`. The update script keeps the newest three.
- Backups: `BACKUP_KEEP`.
- Profiles of users who left are kept on purpose. Delete one with `sudo ./scripts/purge-user.sh <uid>`.

## Logs

| What | Where |
|---|---|
| Requests (user, status, which container served it: `upstream=`) | `journalctl -u rb-nginx` (or `sudo podman logs rb-nginx`) |
| Logins and denials | the existing Authelia's log |
| Provisioning changes | `journalctl -u remote-browser-sync` |
| Image updates, backups | `journalctl -u remote-browser-image-update`, `-u remote-browser-backup` |
| A user's session | `journalctl -u browser-<uid>` |

Container logs rotate: 3 × 10 MB per browser, 5 × 20 MB for Nginx. No log contains credentials or
cookies (the Nginx log format leaves them out).
