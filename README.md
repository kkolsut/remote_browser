# Remote Browser

Self-hosted remote browser service: one isolated Selkies container per authorized FreeIPA user, with
Google Chrome and Firefox and persistent per-user profiles. It runs on rootful **Podman** under
systemd (Quadlet units), behind the site's **Apache** reverse proxy and **Authelia** (FreeIPA login).

```text
client ──HTTPS──▶ Apache ──HTTPS──▶ rb-nginx (this host) ──▶ browser-<uid> (own network per user)
                                      └── authz ──▶ Authelia (FreeIPA)
```

Design and rules: [`docs/plan.md`](docs/plan.md). It was written for Docker Compose; the controls are
the same, the implementation is Podman Quadlet units ([`scripts/rb_units.py`](scripts/rb_units.py)).
Apache and Authelia integration: [`docs/apache-authelia.md`](docs/apache-authelia.md).

In this homelab it is deployed to podman01 by `playbooks/deploy-remote-browser.yml`, which does all of
the steps below. They are written out here for understanding and for doing them by hand.

## Status

| Phase | Scope | State |
|-------|-------|-------|
| 1 | Authentication: Nginx `auth_request` against Authelia (FreeIPA), identity echo `/whoami` | implemented; ported to Podman Quadlet, awaiting deployment test |
| 2 | Dual-browser Selkies image, chooser, seccomp, per-user network, egress firewall | implemented; ported, awaiting deployment test |
| 3 | `sync-freeipa-users.py` provisioning, timer, `kill-session.sh` | implemented, unit tests pass (faked LDAP, Podman and systemd; real Quadlet generator); awaiting test against FreeIPA |
| 4 | Per-user routing for all users | implemented (generated whitelist map) |
| 5 | Hardening, weekly image update, backup/restore, health check, purge | implemented; ported, awaiting deployment test |

The Docker Compose version and its local demo were verified end to end; this Podman port has been
verified by the unit tests, Podman's Quadlet generator and `nginx -t` so far. The acceptance tests in
`tests/` are the deployment test.

## Layout

```text
scripts/rb_units.py              every Quadlet unit: Nginx, its network, per-user browser + network
scripts/install-units.py         installs Nginx's units and the host timers from .env, validates first
scripts/sync-freeipa-users.py    provisioning: FreeIPA group -> browser units, networks, map, profiles
scripts/freeipa_common.py        shared LDAP logic (same eligibility rules as Authelia)
scripts/validate-freeipa.py      FreeIPA connectivity, permissions and eligibility check
scripts/init-secrets.sh          host directories, bind password file, internal TLS certificate
scripts/validate-config.sh       Quadlet generator over all units + nginx -t, changes nothing
scripts/kill-session.sh          end a user's open stream now (restarts their browser)
scripts/build-browser-image.sh   build + version-tag the browser image
scripts/update-browser-image.sh  weekly: build, smoke-test, roll out the Chrome/Firefox image
scripts/backup.sh, restore.sh    configuration, secrets and profiles; restore one profile or everything
scripts/purge-user.sh            delete a former user's profile (the only way profiles are deleted)
scripts/healthcheck.sh           OK/WARN/CRIT for monitoring
scripts/egress-firewall.sh       block browser containers from LAN, host and each other (nftables)
nginx/nginx.conf                 http-level settings, user -> browser whitelist map
nginx/templates/*.template       server blocks (envsubst at container start)
nginx/snippets/                  Authelia auth_request integration, proxy, TLS, websocket
nginx/html/                      error pages (403, no browser yet, browser starting)
nginx/generated/                 users.map: user -> browser whitelist, written by the sync
generated/                       state.json (by the sync), nginx.env (by install-units.py)
browser-image/                   Selkies + Chrome + Firefox image, chooser
apache/remote-browser.conf       a reference vhost for the reverse proxy
systemd/                         egress filter, sync, backup and image-update units and timers
tests/                           acceptance tests (run on the host) and the sync's unit tests
docs/                            plan, Apache/Authelia, FreeIPA, security, backup, troubleshooting
```

On the host, the units live in `/etc/containers/systemd/remote-browser/` (`QUADLET_DIR`):

| File | Unit | Written by |
|------|------|------------|
| `rb-frontend.network` | `rb-frontend-network.service` | `install-units.py` |
| `rb-nginx.container` | `rb-nginx.service` | `install-units.py` |
| `rb-net-<uid>.network` | `rb-net-<uid>-network.service` | the sync |
| `browser-<uid>.container` | `browser-<uid>.service` | the sync |

Podman's generator turns them into services at every `systemctl daemon-reload`; they start at boot
through their `[Install]` section. Never edit them: both scripts overwrite them.

## Tests without a host

```bash
python3 tests/test_sync.py      # sync unit tests: LDAP, Podman and systemd faked
```

When Podman is installed, the generated units also go through its real Quadlet generator.

## Deploy

### 1. Host

A VM with rootful **Podman 5** and systemd (Rocky Linux 10 / RHEL 10 or Debian 13), SELinux enforcing
or not. 4 vCPU and 8 GB RAM to start (video is encoded in software, roughly one vCPU per active
stream; 1.5–2 GB per active user), 24 GB or more for images (the browser image is ~5 GB and the
update keeps three), and a separate disk for profiles and backups.

```bash
sudo dnf install podman python3-ldap3 nftables openssl curl    # Debian: apt install podman python3-ldap3 ...
```

Rootful, because each browser container runs as the user's FreeIPA `uidNumber`/`gidNumber`, so
profiles are owned by different host UIDs (plan §19), far outside the subordinate ID range a rootless
user gets.

### 2. FreeIPA

Follow [`docs/freeipa.md`](docs/freeipa.md): access group, test accounts, system bind account (used by
the provisioning scripts), read permissions, CA certificate.

### 3. Authelia and Apache

Follow [`docs/apache-authelia.md`](docs/apache-authelia.md):
- Authelia: an authz endpoint `remote-browser` (session cookie only), the access rule for the group,
  and the session cookie domain. An Authelia container on this host can join `rb-frontend` and
  be reached as `http://authelia:9091`.
- Apache: the `browser.example.com` vhost (`apache/remote-browser.conf`), pinned to the certificate
  from step 4.

### 4. Configuration and secrets

The checkout is root-owned: the sync, the egress filter and Nginx's `ExecStartPost` run as root from
it.

```bash
sudo install -d /opt/remote-browser && sudo cp -a . /opt/remote-browser && cd /opt/remote-browser
sudo cp .env.example .env && sudoedit .env
sudo ./scripts/init-secrets.sh --self-signed-tls     # asks for the FreeIPA bind password
sudo install -m 0644 ipa-ca.crt local/freeipa-ca/
sudo ./scripts/validate-freeipa.py --expect-eligible remote-browser-test --expect-denied remote-browser-disabled
```

- `--self-signed-tls` creates `local/tls/fullchain.pem`, the certificate Nginx presents to Apache (valid
  10 years). Copy it to Apache (`docs/apache-authelia.md` §2).
- Secrets live in `./local/secrets` (root, `0600`), never in `.env`.
- Allow `HTTPS_PORT` only from Apache in the host firewall. Nginx also refuses everyone but
  `TRUSTED_PROXY`.

### 5. Units, then test authentication

```bash
sudo ./scripts/install-units.py      # validates, installs, enables the egress filter + timers, starts Nginx
systemctl status rb-nginx

TEST_USER=remote-browser-test TEST_PASS=… \
OUTSIDER_USER=remote-browser-outsider OUTSIDER_PASS=… \
DISABLED_USER=remote-browser-disabled DISABLED_PASS=… \
./tests/phase1-auth.sh
```

`install-units.py` changes nothing unless the Quadlet generator accepts the units and `nginx -t`
passes in a throwaway container. Run it again after any change to `.env`. It restarts Nginx when
Nginx's unit or environment changed; `--restart-nginx` also after a change to `nginx/templates/`.

`phase1-auth.sh` goes through Apache like a real client. It covers:
- redirect when anonymous, forged identity headers, basic-auth bypass;
- the authz endpoint blocked on the public portal;
- bad password, group member allowed, header overwrite after login;
- non-member denied (403), disabled user, logout.

Check session expiry and the regulation ban manually. Each run uses one wrong password for `TEST_USER`.

`https://browser.example.com/whoami` shows the identity Nginx forwards to backends. `/` shows "No browser
yet" until the user has a browser.

### 6. Browsers: image, provisioning

**Image.** `sudo ./scripts/build-browser-image.sh` prints a tag like
`localhost/remote-dual-browser:20261013-chrome155.0.8059.39-firefox158.0`. Set `BROWSER_IMAGE` in `.env`
to it. The base image is pinned by tag and digest in `browser-image/Dockerfile`. Chrome and Firefox are
installed fresh on every build (`--no-cache`) from Google's and Mozilla's APT repositories, and the
build verifies their signing-key fingerprints.

**Egress filter.** Browser containers must not reach the LAN, Apache, Authelia, Proxmox, FreeIPA or the
host (plan §30a). `remote-browser-egress.service` (installed and started by `install-units.py`) applies
`scripts/egress-firewall.sh`: it rejects everything from `BROWSER_NET_POOL` to private, link-local and
loopback ranges, to other users' networks and to the host itself, and allows the public Internet.
Every browser unit `Requires=` it, so no browser starts without it. Check with
`sudo ./scripts/egress-firewall.sh status`. The host's addresses are read when the rules are applied:
restart the unit if they change, and after anything that flushes the nftables ruleset.

**Provisioning.** `scripts/sync-freeipa-users.py` gives every eligible member of
`FREEIPA_BROWSER_GROUP` a browser. "Eligible" means the same as for Authelia: a direct member (nested
groups are ignored), not locked, password and principal not expired. For each such user it writes
`browser-<uid>.container` and `rb-net-<uid>.network` (a /28 from `BROWSER_NET_POOL` that the user keeps)
into `QUADLET_DIR`, and the route into `nginx/generated/users.map`. It creates
`USER_DATA_ROOT/<uid>/config` owned by the user's uidNumber, mode 0700. Then it applies the changes: it
stops the browsers of users who lost access, reloads systemd, reloads Nginx after `nginx -t`,
restarts browsers whose unit changed, starts new ones, and attaches Nginx to each user network.

```bash
sudo ./scripts/sync-freeipa-users.py --dry-run        # who would get a browser; changes nothing
sudo ./scripts/sync-freeipa-users.py                  # provision
# every 5 minutes from then on: remote-browser-sync.timer (enabled by install-units.py)
```

Rules worth knowing (plan §17, §32-33):
- **Fail-safe.** Any LDAP or configuration error aborts the run before anything changes. An error is
  never read as "no members". The sync refuses an empty result, or more than `SYNC_MAX_REMOVALS`
  (default 2) removals at once, unless you pass `--force`. Units are installed only after Podman's
  Quadlet generator accepts them; the previous map and state are kept as `*.prev`.
- **Skipped members.** A uid must match `^[a-z][a-z0-9-]{0,30}$`, because it becomes container,
  unit, network and DNS names. Members that don't match, members without uidNumber/gidNumber, members
  sharing a uidNumber, and members whose profile directory belongs to another uid are not provisioned.
  The sync logs them and exits with status 2, which shows as a failed unit. Fix the member, or take it
  out of the group.
- **Removal.** A user who is removed from the group, disabled or expired loses the browser at the
  next run, so an open stream lasts at most one timer interval. The profile directory is never
  deleted. `scripts/kill-session.sh <uid>` ends a stream immediately.
- **Nginx's networks.** Nginx joins the user networks live: listing them in `rb-nginx.container` would
  restart Nginx, and cut all open streams, for every new user. Whenever `rb-nginx.service` starts,
  its `ExecStartPost` (`sync-freeipa-users.py --attach-only`) puts the new container back on every
  user network.
- **SELinux.** Each profile is mounted with `:Z`, a label private to that user's container; Nginx's
  read-only mounts with `:z`.
- **Never edit** the generated units, `generated/` or `nginx/generated/users.map`. The next run
  overwrites them.

**Test:**

```bash
sudo ./scripts/validate-config.sh
BROWSER_USER=remote-browser-test BROWSER_PASS=… OTHER_USER=<second member> OTHER_PASS=… ACCESS_LOG=1 \
    BACKEND_ADDR=<host-ip>:8443 sudo -E ./tests/routing.sh
BROWSER_USER=remote-browser-test \
    EGRESS_TARGETS="<apache-ip>:443 <freeipa-ip>:636 <proxmox-ip>:8006" sudo -E ./tests/phase2-browser.sh
sudo ./tests/phase3-sync.sh
```

`routing.sh` uses Nginx's access log (`upstream=`) to check that each user reaches their own container.

`phase3-sync.sh` checks provisioning on the running host:
- every provisioned user has their own container, uid, profile, network and unit (shared only with
  Nginx);
- no browser can reach another user's browser;
- a second sync changes nothing and leaves Nginx alone;
- a restarted Nginx is back on every user network (it restarts Nginx: open streams end).

Group changes are tested by hand against FreeIPA (plan §64). Run the sync with
`sudo systemctl start remote-browser-sync` and check the result each time:
1. Add a test user to the group. They get a browser.
2. Disable the user while they have a stream open (`ipa user-disable`). The stream ends, the
   container, unit and network are gone, and `USER_DATA_ROOT/<uid>` is still there.
3. Enable the user again. The browser is back with the old bookmarks.
4. Remove the user from the group. Same result as step 2.
5. Make FreeIPA unreachable: `sudo FREEIPA_LDAP_URL=ldaps://192.0.2.1 ./scripts/sync-freeipa-users.py`.
   It must say `ABORT`, and nothing may change.

`phase2-browser.sh` drives the session with xdotool. It checks:
- the chooser, and Chrome and Firefox starting and exiting;
- the browser sandboxes: Chrome renderers in nested PID namespaces under seccomp, Firefox content
  processes in their own user and network namespaces;
- persistence across a container restart (stale profile locks), Nginx's default route, and egress.

It closes browsers in the container, so don't run it while the test user is working.

Manual acceptance (plan §63):
1. Log in as the test user.
2. In Chrome, open `chrome://sandbox`. It should say "You are adequately sandboxed".
3. In Firefox, open `about:support` → *Sandbox*. User namespaces should be true and the content level
   4 or higher.
4. Set a bookmark in each browser, run `./scripts/kill-session.sh <uid>`, and check that both
   bookmarks are still there.

### 7. Updates, backup, monitoring

`install-units.py` has enabled both timers:

- **Browser updates** (Sunday night): `update-browser-image.sh` builds an image with the current
  Chrome and Firefox. If either changed, it smoke-tests the image: the browser unit exactly as the sync
  renders it, without network, as a transient Quadlet (chooser, both sandboxes, persistence). Then it
  sets `BROWSER_IMAGE` and runs the sync, which restarts every browser. A failure changes nothing. The
  newest three images are kept for rollback.
- **Backup** (nightly): configuration, secrets and profiles into `BACKUP_DIR`. Copy it off the host.
  Before go-live, run `sudo ./tests/backup-restore.sh`, then the clean-host procedure in
  [`docs/backup.md`](docs/backup.md).
- **Monitoring:** `sudo ./scripts/healthcheck.sh` prints OK/WARN/CRIT and exits 0/1/2. Hook it into
  what you have, e.g. cron (`--quiet`, mails only problems) or an Uptime Kuma push monitor.

### What the browser container is and is not allowed

- Runs as the user's FreeIPA uid. There is no `sudo` in the image, and the user is not in the `sudo`,
  `wheel` or `docker` groups. No engine socket, not privileged, no published ports. Only Nginx shares
  its network.
- Seccomp: Podman's default profile, which allows the unprivileged user namespaces both browser
  sandboxes use. Never `--no-sandbox`.
- `no-new-privileges`, all capabilities dropped except the few the init needs plus `SYS_CHROOT` (which
  both sandboxes need), 4096 processes at most, logs to journald. All of this is tested against the
  sandboxes; the reasons are in [`docs/security.md`](docs/security.md).
- Its profile carries an SELinux label private to its container (`:Z`).
- It never sees the client's cookies or `Authorization` headers: Nginx strips them, so a site-wide
  Authelia SSO cookie cannot leak into a remote session.
- Selkies:
  - clipboard on;
  - file transfer off (`BROWSER_FILE_TRANSFERS`);
  - webcam, gamepads, microphone, printing, the command channel and the apps panel off;
  - the stream accepts WebSockets only from the public origin.

  Selkies has no authentication of its own: never publish port 3000.
- Chrome saves passwords with `--password-store=basic` (no keyring in the container). They are
  protected only by the profile directory's permissions on the host. Tell users.
- Closing a browser returns to the chooser; there is no logout inside the session.

## Operations

- **Users:** add them to or remove them from `FREEIPA_BROWSER_GROUP` in FreeIPA. The sync timer
  applies the change within 5 minutes; `sudo systemctl start remote-browser-sync` applies it now. Log:
  `journalctl -u remote-browser-sync`.
- **Logout:** `https://browser.example.com/logout` → Authelia logout. It does **not** end an
  already-open browser stream. Only stopping or restarting the user's browser does (plan §17):
  `./scripts/kill-session.sh <uid>`. To lock someone out, disable them in FreeIPA first.
- **One user's browser:** `systemctl status|restart browser-<uid>`, `journalctl -u browser-<uid>`.
- **Browser updates:** `sudo systemctl start remote-browser-image-update` builds, tests and rolls out
  now (for an actively exploited browser bug). It restarts every browser, which ends open streams.
- **Backup / restore:** [`docs/backup.md`](docs/backup.md). **Deleting a former user's profile:**
  `sudo ./scripts/purge-user.sh <uid>`.
- **Health:** `sudo ./scripts/healthcheck.sh`. **Problems:** [`docs/troubleshooting.md`](docs/troubleshooting.md).
- **Login policy, 2FA, brute-force regulation:** in Authelia (`docs/apache-authelia.md` §1).
- **Exposure:** the host publishes only `HTTPS_PORT`, and only `TRUSTED_PROXY` gets an answer there.
- **Nginx config changes:** `nginx/templates/` → `sudo ./scripts/install-units.py --restart-nginx`
  (templates are rendered at start). Map and snippet changes only need
  `sudo podman exec rb-nginx nginx -s reload`.

## Pinned versions

| Component | Version |
|-----------|---------|
| nginx | `docker.io/library/nginx:1.30.5-alpine` (`scripts/rb_units.py`) |
| Selkies base | `ghcr.io/linuxserver/baseimage-selkies:debiantrixie-3900c0f0-ls140` (+ digest, `browser-image/Dockerfile`) |
| Browser image | `BROWSER_IMAGE` in `.env` (date + Chrome + Firefox version tag) |

Upgrade: backup → bump the tag → `sudo ./scripts/install-units.py` (validates, restarts Nginx) → tests.
