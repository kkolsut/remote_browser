# Security

What protects what, where it is enforced, and which test proves it. Plan references: §13, §30-33,
§44, §61, §66.

## Trust boundaries

```text
Internet ──▶ Apache (TLS, existing) ──▶ Nginx (this VM) ──▶ browser-<uid> (one per user)
                                         │                    │
                                         └─ authz ─▶ Authelia  └─▶ public Internet only
```

- **Identity** comes only from Authelia's answer to Nginx's `auth_request`. Client-supplied identity
  headers are overwritten, never trusted. A route exists only for users the sync provisioned, and
  everyone else gets the "no browser" page, never another user's container.
- **A browser container is untrusted.** Treat it as an arbitrary program run by the user. It must
  not reach other users, the LAN or the host. If it escapes the browser sandbox, it must still be an
  unprivileged process in a restricted container.

## Controls

| Control | Where | Tested by |
|---|---|---|
| Login against FreeIPA, group required, regulation below the FreeIPA lockout | Authelia (`docs/apache-authelia.md`) | `phase1-auth.sh` |
| Forged `Remote-User` etc. ignored; cookies and `Authorization` never reach a browser | `nginx/snippets/` | `phase1-auth.sh`, `routing.sh` |
| Nginx answers only the reverse proxy | `TRUSTED_PROXY` | `routing.sh` (`BACKEND_ADDR`) |
| Only provisioned users are routed, each to their own container | generated `users.map` | `routing.sh` (access log `upstream=`) |
| Provisioning matches Authelia's eligibility exactly; LDAP errors never mean "no users" | `sync-freeipa-users.py` | `test_sync.py`, `phase3-sync.sh` |
| Uids become names only after `^[a-z][a-z0-9-]{0,30}$` | sync | `test_sync.py` |
| One network per user (only that browser and Nginx) | generated Compose + sync | `phase2-browser.sh`, `phase3-sync.sh` |
| No browser→LAN/host/private ranges | `egress-firewall.sh` (iptables) | `phase2-browser.sh` |
| Per-user host uid, profile `0700`, never re-chowned automatically | sync | `phase3-sync.sh`, `test_sync.py` |
| Selkies stream only from the public origin | `SELKIES_ALLOWED_ORIGINS` | `routing.sh` |
| Chrome and Firefox sandboxes on (never `--no-sandbox`) | `seccomp-chrome.json` + `SYS_CHROOT` | `phase2-browser.sh` |
| `no-new-privileges`; all capabilities dropped except CHOWN, DAC_OVERRIDE, FOWNER, SETGID, SETUID, KILL, SYS_CHROOT | generated Compose | `phase2-browser.sh` |
| No sudo, no Docker socket, not privileged, no published ports, pids limit | image + generated Compose | `phase2-browser.sh` |
| Nginx: read-only root filesystem, 4 capabilities, `no-new-privileges` | `compose.yaml` | `validate-config.sh`, demo |
| Open streams end when access ends: within one sync interval (5 min), or now | sync, `kill-session.sh` | `phase3-sync.sh` |
| Browser security fixes within a week, tested before rollout | `update-browser-image.sh` + timer | smoke test in the script |
| Secrets outside Git, `0600`; backups `0700` | `init-secrets.sh`, `backup.sh` | `backup-restore.sh` |
| Pinned images (Nginx tag, Selkies base by digest, dated browser image) | `compose.yaml`, `Dockerfile` | — |

### Why these capabilities

The image's s6 init runs as root inside the container until it starts the session as the user's uid:
- CHOWN, DAC_OVERRIDE and FOWNER: preparing `/config`;
- SETUID and SETGID: dropping to the user;
- KILL: supervising the session's processes.

SYS_CHROOT is needed because both sandboxes chroot their child processes, and Docker's seccomp
profile allows `chroot(2)` only with this capability. Without it, both sandboxes silently turned
off; `phase2-browser.sh` caught this. The sandboxes use unprivileged user namespaces, not Chrome's
setuid helper, so `no-new-privileges` costs nothing.

Dropped from Docker's defaults: NET_RAW (no raw sockets or spoofing), MKNOD, NET_BIND_SERVICE, FSETID,
SETFCAP, SETPCAP, AUDIT_WRITE.

## Accepted risks (tell users)

- **Chrome passwords** are stored with `--password-store=basic`: there is no keyring in the container.
  They are protected only by the profile directory's permissions and by backup handling. Prefer
  Firefox with a primary password, or a password manager.
- **Logout does not end an open stream.** Authelia checks the session only when the stream starts.
  Disabling an account ends the stream at the next sync (≤ 5 min). For "now", use `kill-session.sh`.
- **Two devices, one desktop.** A user has one container and one display. A second device joins the
  same desktop; the first connection is not cut off (measured: both WebSockets stay open). Whether
  input from both is accepted at once is up to Selkies.
- **Clipboard is on** (copy/paste between the device and the remote browser). File transfer is off
  by default (`BROWSER_FILE_TRANSFERS`).
- **The browser reaches the whole public Internet.** That is the product. The egress filter covers
  only private, LAN and host addresses.
- **The VM's Docker host is the boundary.** The sync and the timers run as root, and the `docker`
  group is root-equivalent. Keep the checkout root-owned, and limit SSH to administrators (WireGuard
  or LAN only).
- **Backups hold everyone's profiles and the secrets.** Store them like secrets.

## Recommended on the VM

- `unattended-upgrades` for Debian security updates. Reboot when the kernel changes: the browser
  sandboxes depend on the kernel.
- Firewall: `HTTPS_PORT` only from Apache; SSH only from the admin network.
- Authelia `two_factor` for this service: TOTP or WebAuthn, which needs SMTP for enrolment
  (`docs/apache-authelia.md` §1.2).
- Audit membership changes to `remote-browser-users` in FreeIPA (who added whom). The sync's log in
  the journal shows when a change took effect.
- Run `scripts/healthcheck.sh` from monitoring. It reports a failed sync, a stale backup, a missing
  egress filter, an expiring certificate and unhealthy browsers.
