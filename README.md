# Remote Browser

Self-hosted remote browser service: one isolated Selkies container per authorized FreeIPA user, with
Google Chrome and Firefox and persistent per-user profiles. It runs on its own VM behind the site's
existing **Apache** reverse proxy and **Authelia** (FreeIPA login).

```text
client ──HTTPS──▶ Apache (existing) ──HTTPS──▶ Nginx (this VM) ──▶ browser-<uid> (own network per user)
                                                 └── authz ──▶ Authelia (existing, FreeIPA)
```

Design and rules: [`../remote-browser-freeipa-chrome-firefox-plan.md`](../remote-browser-freeipa-chrome-firefox-plan.md).
Apache and Authelia integration: [`docs/apache-authelia.md`](docs/apache-authelia.md).

## Status

| Phase | Scope | State |
|-------|-------|-------|
| 1 | Authentication: Nginx `auth_request` against Authelia (FreeIPA), identity echo `/whoami` | **implemented**, verified in the local demo; awaiting deployment test |
| 2 | Dual-browser Selkies image, chooser, seccomp, per-user network, egress firewall | **implemented**, verified in the local demo; awaiting deployment test |
| 3 | `sync-freeipa-users.py` provisioning, timer, `kill-session.sh` | **implemented**, verified in the local demo (file-backed source) and unit tests (faked LDAP); awaiting test against FreeIPA |
| 4 | Per-user routing for all users | **implemented** (generated whitelist map), verified in the local demo with two users |
| 5 | Hardening, backup, rebuild timer | not started |

## Layout

```text
compose.yaml                     nginx (pinned): the browser VM's entry point
generated/                       compose.users.yaml (browser + network per user), state.json; by the sync
compose.demo.yaml                local demo: adds an Apache (stand-in reverse proxy) and an Authelia
apache/remote-browser.conf       the vhost for your Apache (also run by the demo)
nginx/nginx.conf                 http-level settings, resolver, user -> browser whitelist map
nginx/templates/*.template       server blocks (envsubst, see compose.yaml)
nginx/snippets/                  Authelia auth_request integration, proxy, TLS, websocket
nginx/html/                      error pages (403, no browser yet, browser starting)
nginx/generated/                 users.map: user -> browser whitelist, written by the sync
browser-image/                   Selkies + Chrome + Firefox image, chooser, seccomp-chrome.json
demo/                            demo env, dummy users, demo Authelia and Apache configuration
scripts/init-secrets.sh          host directories, secrets, internal TLS certificate (idempotent, no root)
scripts/demo.sh                  local demo without FreeIPA
scripts/validate-freeipa.py      FreeIPA connectivity, permissions and eligibility check
scripts/freeipa_common.py        shared LDAP logic (same eligibility rules as Authelia)
scripts/sync-freeipa-users.py    provisioning: FreeIPA group -> browser containers, networks, map, profiles
scripts/kill-session.sh          end a user's open stream now (restarts their browser)
scripts/validate-config.sh       compose config + seccomp profile + nginx -t
scripts/build-browser-image.sh   build + version-tag the browser image
scripts/gen-seccomp-chrome.py    regenerate browser-image/seccomp-chrome.json from Docker's default
scripts/egress-firewall.sh       block browser containers from LAN/host (iptables DOCKER-USER/INPUT)
systemd/remote-browser-egress.service   applies the egress filter at boot
systemd/remote-browser-sync.{service,timer}   runs the sync every 5 minutes
tests/phase1-auth.sh             authentication/authorization through the public URL
tests/routing.sh                 user -> own browser, WebSocket, no fallback, forged headers, no bypass
tests/phase2-browser.sh          container hardening, chooser, sandboxes, persistence, egress
tests/phase3-sync.sh             provisioning on a live stack: consistency, isolation, idempotence
tests/test_sync.py               sync unit tests: fail-safe, eligibility, generated files (no LDAP needed)
docs/apache-authelia.md          what to add to your Apache and Authelia
docs/freeipa.md                  FreeIPA preparation
```

## Local demo (no FreeIPA)

Runs the production topology on your machine: an Apache container in front (running
`apache/remote-browser.conf`), the VM's Nginx, a demo Authelia with dummy users from `demo/users.yml`,
and a Chrome/Firefox container per user, provisioned by the same `sync-freeipa-users.py` as in
production. In the demo, the sync reads `demo/users.yml` instead of FreeIPA (`AUTH_BACKEND=file`).
Settings are in `demo/demo.env`. Needs only Docker (Engine 28+) with `docker compose` and PyYAML. The
first `up` builds the browser image (~5 GB, several minutes).

```bash
./scripts/demo.sh up        # https://browser.localhost:8443/ (self-signed; accept it for both names)
./scripts/demo.sh test      # phase1-auth.sh, routing.sh, phase2-browser.sh, phase3-sync.sh
./scripts/demo.sh sync      # provision after editing demo/users.yml (after: demo.sh restart authelia)
./scripts/demo.sh build     # rebuild the browser image after changing browser-image/
./scripts/demo.sh logs -f
./scripts/demo.sh down      # rm -rf local/demo to start from scratch
python3 tests/test_sync.py  # sync unit tests, no stack needed
```

Users (password `demo`):
- `alice` and `dave` are in the access group and each get their own browser (choose Chrome or Firefox).
- `bob` is not in the group (403).
- `carol` is disabled: no login, no browser.
- `eve.smith` is in the group, but a dot is not allowed in a uid that becomes container and network
  names. The sync refuses it, so the login works but shows "No browser yet".

`/whoami` shows the identity Nginx forwards to backends.

All demo browsers run as **your** uid/gid, so `local/demo` stays deletable without root (in production
each runs as the user's own FreeIPA uidNumber). The egress filter is the one part that needs root;
without it the egress checks of `demo.sh test` fail:

```bash
sudo ENV_FILE=demo/demo.env ./scripts/egress-firewall.sh apply    # remove: ... remove
```

## Deploy

### 1. VM and Docker

Debian 13 VM (plan §6). **Docker CE from Docker's repository, Engine 28 or newer, rootful** (Debian's
`docker.io` is too old for `gw_priority`):

```bash
sudo apt install git python3-ldap3 curl openssl iptables
# Docker CE: https://docs.docker.com/engine/install/debian/
sudo usermod -aG docker $USER     # note: the docker group is root-equivalent
```

Rootful, because each browser container runs as the user's FreeIPA `uidNumber`/`gidNumber`, so profiles
are owned by different host UIDs (plan §19). FreeIPA ID ranges (e.g. 916200000+) lie far outside the
65536 subordinate IDs rootless Docker gets by default.

### 2. FreeIPA

Follow [`docs/freeipa.md`](docs/freeipa.md): access group, test accounts, system bind account (used by
the provisioning scripts), read permissions, CA certificate.

### 3. Authelia and Apache

Follow [`docs/apache-authelia.md`](docs/apache-authelia.md):
- Authelia: an authz endpoint `remote-browser` (session cookie only), the access rule for the group,
  and the session cookie domain.
- Apache: the `browser.example.com` vhost (`apache/remote-browser.conf`), pinned to the VM's
  certificate from step 4.

### 4. Configuration and secrets

The checkout is root-owned: the provisioning sync and the egress filter run as root from it (the sync
creates each user's profile directory owned by that user's uidNumber). Everything below runs in it.

```bash
sudo git clone <repo> /opt/remote-browser && cd /opt/remote-browser
sudo cp .env.example .env && sudoedit .env
sudo ./scripts/init-secrets.sh --self-signed-tls     # prompts for the FreeIPA bind password
sudo install -m 0644 ipa-ca.crt local/freeipa-ca/
sudo ./scripts/validate-freeipa.py --expect-eligible remote-browser-test --expect-denied remote-browser-disabled
```

- `--self-signed-tls` creates `local/tls/fullchain.pem`, the certificate Nginx presents to Apache (valid
  10 years). Copy it to Apache (`docs/apache-authelia.md` §2). A certificate from your internal CA works too.
- Secrets live in `./local/secrets` (root, `0600`), never in Git or `.env`.
- Allow `HTTPS_PORT` only from Apache in the VM firewall. Nginx also refuses everyone but `TRUSTED_PROXY`.

### 5. Start and test authentication

```bash
./scripts/validate-config.sh
docker compose up -d nginx       # browsers come in step 6

TEST_USER=remote-browser-test TEST_PASS=… \
OUTSIDER_USER=remote-browser-outsider OUTSIDER_PASS=… \
DISABLED_USER=remote-browser-disabled DISABLED_PASS=… \
./tests/phase1-auth.sh
```

`phase1-auth.sh` goes through Apache like a real client. It covers:
- redirect when anonymous, forged identity headers, basic-auth bypass;
- the authz endpoint blocked on the public portal;
- bad password, group member allowed, header overwrite after login;
- non-member denied (403), disabled user, logout.

Check session expiry and the regulation ban manually. Each run uses one wrong password for `TEST_USER`.

`https://browser.example.com/whoami` shows the identity Nginx forwards to backends. `/` shows "No browser
yet" until the user has a browser container.

### 6. Browsers: image, egress filter, provisioning

**Image.** Run `./scripts/build-browser-image.sh`, which prints a tag like
`local/remote-dual-browser:20261013-chrome155.0.8059.39-firefox158.0`, and set `BROWSER_IMAGE` in `.env`
to it. The base image is pinned by tag and digest in `browser-image/Dockerfile`. Chrome and Firefox are
installed fresh on every build (`--no-cache`) from Google's and Mozilla's APT repositories, and the build
verifies their signing-key fingerprints.

**Egress filter.** Browser containers must not reach the LAN, Apache, Authelia, Proxmox, FreeIPA or the
host (plan §30a). `scripts/egress-firewall.sh` rejects everything from `BROWSER_NET_POOL` to
private/link-local/loopback ranges and to the host itself, and allows the public Internet. Install it
as a boot-time unit (before the first browser starts):

```bash
sed "s|@REPO_DIR@|$PWD|" systemd/remote-browser-egress.service | sudo tee /etc/systemd/system/remote-browser-egress.service
sudo systemctl daemon-reload && sudo systemctl enable --now remote-browser-egress.service
sudo ./scripts/egress-firewall.sh status
```

The host's addresses are read when the rules are applied. Restart the unit if they change, and after
a `firewall-cmd --reload` (which can drop the rules).

**Provisioning.** `scripts/sync-freeipa-users.py` gives every eligible member of
`FREEIPA_BROWSER_GROUP` a browser. "Eligible" means the same as for Authelia: a direct member (nested
groups are ignored), not locked, password and principal not expired. For each such user it writes
the Compose service `browser-<uid>` and network `net-<uid>` (a /28 from `BROWSER_NET_POOL` that the
user keeps) into `generated/`, and the route into `nginx/generated/users.map`. It creates
`USER_DATA_ROOT/<uid>/config` owned by the user's uidNumber, mode 0700. Then it applies the changes:
it stops the browsers of users who lost access, reloads Nginx after `nginx -t`, starts new browsers,
and attaches Nginx to each user network.

```bash
sudo ./scripts/sync-freeipa-users.py --dry-run        # who would get a browser; changes nothing
sudo ./scripts/sync-freeipa-users.py                  # provision
# every 5 minutes from now on:
for u in remote-browser-sync.service remote-browser-sync.timer; do
    sed "s|@REPO_DIR@|$PWD|" systemd/$u | sudo tee /etc/systemd/system/$u >/dev/null
done
sudo systemctl daemon-reload && sudo systemctl enable --now remote-browser-sync.timer
```

Rules worth knowing (plan §17, §32-33):
- **Fail-safe.** Any LDAP or configuration error aborts the run before anything changes. An error is
  never read as "no members". The sync refuses an empty result, or more than `SYNC_MAX_REMOVALS`
  (default 2) removals at once, unless you pass `--force`. Generated files are installed only after
  `docker compose config` accepts them; the previous versions are kept as `*.prev`.
- **Skipped members.** A uid must match `^[a-z][a-z0-9-]{0,30}$`, because it becomes container,
  network and DNS names. Members that don't match, members without uidNumber/gidNumber, members
  sharing a uidNumber, and members whose profile directory belongs to another uid are not provisioned.
  The sync logs them and exits with status 2, which shows as a failed unit. Fix the member, or take it
  out of the group.
- **Removal.** A user who is removed from the group, disabled or expired loses the browser at the
  next run, so an open stream lasts at most one timer interval. The profile directory is never
  deleted. `scripts/kill-session.sh <uid>` ends a stream immediately.
- **Nginx's networks.** The sync attaches Nginx to the user networks itself. Declaring them in
  Compose would make every new user recreate Nginx and cut all open streams. A recreated Nginx (e.g.
  `docker compose up -d` after a change to Nginx) is on no user network until the next sync: run it
  right after.
- **Never edit** `generated/` or `nginx/generated/users.map`. The next run overwrites them.

**Coming from Phase 2** (`compose.browser-test.yaml`, now removed): in `.env`, set
`COMPOSE_FILE=compose.yaml:generated/compose.users.yaml` and delete the `BROWSER_TEST_*` lines. Then run
the sync. It removes the old `browser-test` container and network and gives the test user
`browser-<uid>`, using the same profile directory.

**Test:**

```bash
./scripts/validate-config.sh
BROWSER_USER=remote-browser-test BROWSER_PASS=… OTHER_USER=<second member> OTHER_PASS=… PROJECT=remote-browser \
    BACKEND_ADDR=<vm-ip>:8443 ./tests/routing.sh
BROWSER_USER=remote-browser-test \
    EGRESS_TARGETS="<apache-ip>:443 <authelia-ip>:9091 <proxmox-ip>:8006 <freeipa-ip>:636" ./tests/phase2-browser.sh
sudo ./tests/phase3-sync.sh
```

`routing.sh` uses Nginx's access log (`upstream=`) to check that each user reaches their own container.

`phase3-sync.sh` checks provisioning on the running stack:
- every provisioned user has their own container, uid, profile and network (shared only with Nginx);
- no browser can reach another user's browser;
- a second sync changes nothing and leaves Nginx alone.

On the demo it also changes group membership itself. Against FreeIPA, do that part by hand (plan §64).
Run the sync with `sudo systemctl start remote-browser-sync` and check the result each time:
1. Add a test user to the group. They get a browser.
2. Disable the user while they have a stream open (`ipa user-disable`). The stream ends, the
   container and network are gone, and `USER_DATA_ROOT/<uid>` is still there.
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

### What the browser container is and is not allowed

- Runs as the user's FreeIPA uid. There is no `sudo` in the image, and the user is not in the `sudo` or
  `docker` groups. No Docker socket, not privileged, no published ports. Only Nginx shares its network.
- Seccomp: Docker's default profile plus `clone`/`unshare` for the browser sandboxes
  (`scripts/gen-seccomp-chrome.py`). Never `--no-sandbox`. `no-new-privileges` and capability drops are
  Phase 5, after testing them against the sandboxes.
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
  already-open browser stream. Only stopping or restarting the user's container does (plan §17):
  `./scripts/kill-session.sh <uid>`. To lock someone out, disable them in FreeIPA first.
- **Deleting a profile:** the sync never deletes one. When a user will not come back and the sync has
  removed their browser, delete `USER_DATA_ROOT/<uid>` by hand.
- **Browser updates:** run `./scripts/build-browser-image.sh` and smoke-test with the test user. Then set
  the new tag in `BROWSER_IMAGE` and run the sync (or wait for the timer). It recreates every browser,
  which ends open streams. Chrome and Firefox never update themselves inside the image, so rebuild at
  least weekly (the timer comes in Phase 5).
- **After `docker compose up -d`:** run the sync. If Compose recreated Nginx, the sync puts it back on
  the user networks.
- **Login policy, 2FA, brute-force regulation:** in your Authelia (`docs/apache-authelia.md` §1).
- **Exposure:** the VM publishes only `HTTPS_PORT`, and only `TRUSTED_PROXY` gets an answer there.
- **Nginx config changes:** a change to `nginx/templates/` needs `docker compose restart nginx`
  (templates are rendered at start). Map and snippet changes only need `nginx -s reload`.

## Pinned versions

| Component | Version |
|-----------|---------|
| nginx | `1.30.5-alpine` |
| Selkies base | `ghcr.io/linuxserver/baseimage-selkies:debiantrixie-3900c0f0-ls140` (+ digest, `browser-image/Dockerfile`) |
| Browser image | `BROWSER_IMAGE` in `.env` (date + Chrome + Firefox version tag) |
| Seccomp source | moby/profiles `6fe7deb1` (`scripts/gen-seccomp-chrome.py`) |
| Demo only | Authelia `4.39.28`, Apache `httpd:2.4.69-alpine` (+ digest) |

Upgrade: backup → bump tag → `./scripts/validate-config.sh` → `docker compose up -d` → sync → tests.
