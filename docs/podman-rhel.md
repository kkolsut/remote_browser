# Deploying on RHEL / Alma / Rocky with Podman

Step by step, on a RHEL-family VM (9 or 10) with **rootful Podman 5+**, SELinux enforcing and
firewalld. Apache and Authelia already run elsewhere; FreeIPA is prepared per
[`freeipa.md`](freeipa.md). Run every command from the checkout, `/opt/remote-browser`.

How it works on Podman. The project keeps using the `docker` CLI and `docker compose`, which talk to
rootful Podman through its Docker-compatible API socket. The differences from Docker are handled by:

| Difference | Handled by |
|---|---|
| Podman ignores `gw_priority`, so Nginx's default route could go through a user network | `frontend` created with route metric 50 (`podman-setup.sh`, `compose.podman.yaml`) |
| Podman's API cannot take Compose's custom seccomp profile | Podman's default profile, which allows the browser sandboxes (`CONTAINER_ENGINE=podman`) |
| Podman does not isolate networks from each other by default | the egress filter rejects traffic between user networks |
| No Docker iptables chain for the egress filter | the filter is its own nftables table |
| DNS at the network gateway, not 127.0.0.11 | Nginx reads its resolver from `/etc/resolv.conf` |
| SELinux | volumes labelled `:z` (Nginx config) and `:Z` (each profile gets a private label) |
| Restart policies only work at boot with `podman-restart.service` | enabled by `podman-setup.sh` |

**Verified** with rootful Podman on a test machine (Podman 6.1, no SELinux):
- provisioning, routing, the container restrictions and both browser sandboxes;
- persistence;
- isolation between users;
- backup and restore.

**Not verified yet** (that machine had neither SELinux nor a Podman-only host):
- SELinux enforcing, including the sandboxes' user namespaces under `container_t`;
- firewalld together with Podman's networking;
- the full image build under Podman;
- the egress filter's blocking;
- a reboot.

Steps 12 and 14 test exactly these. Don't go live before they pass.

---

## 1. VM

4 vCPU, 8 GB RAM for a handful of users (about 150 MB idle per user, 0.5-1.5 GB with a browser
open), 40 GB+ disk for images and profiles. A static IP.

## 2. Packages

```bash
sudo dnf install -y git python3 openssl curl tar nftables podman container-selinux policycoreutils-python-utils

# python3-ldap3 is in EPEL.
#   Alma / Rocky:
sudo dnf install -y epel-release
#   RHEL: sudo subscription-manager repos --enable codeready-builder-for-rhel-$(rpm -E %rhel)-$(arch)-rpms
#         sudo dnf install -y https://dl.fedoraproject.org/pub/epel/epel-release-latest-$(rpm -E %rhel).noarch.rpm
sudo dnf install -y python3-ldap3
#   (if EPEL lacks it: sudo pip3 install ldap3)

# The docker CLI and compose plugin only, NOT docker-ce (the engine). Podman's own "docker"
# emulation must not be installed.
sudo dnf remove -y podman-docker
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
sudo dnf install -y docker-ce-cli docker-compose-plugin
```

## 3. Rootful Podman

```bash
sudo systemctl enable --now podman.socket
sysctl user.max_user_namespaces         # must be > 0: the browser sandboxes use user namespaces
```

Everything below runs with `sudo`. Rootful matters: each browser runs as its user's FreeIPA uidNumber,
far outside rootless Podman's ID ranges.

## 4. Checkout and configuration

```bash
sudo git clone <repo> /opt/remote-browser && cd /opt/remote-browser
sudo cp .env.example .env && sudoedit .env
```

In `.env`, set at least:

```dotenv
CONTAINER_ENGINE=podman
COMPOSE_FILE=compose.yaml:compose.podman.yaml:generated/compose.users.yaml

PUBLIC_HOST=browser.example.com          # public name, served by your Apache
AUTHELIA_HOST=auth.example.com           # your Authelia portal (logout link)
BIND_ADDRESS=<VM IP>                     # where Nginx listens
HTTPS_PORT=8443
TRUSTED_PROXY=<Apache IP>/32             # the only address Nginx answers
AUTHELIA_AUTHZ_URL=https://<authelia host>:9091/api/authz/remote-browser

FREEIPA_LDAP_URL=ldaps://<ipa host>
FREEIPA_BASE_DN=dc=...,dc=...            # ipa env basedn
FREEIPA_BIND_DN=uid=svc_remote_browser,cn=sysaccounts,cn=etc,dc=...,dc=...
FREEIPA_BROWSER_GROUP=remote-browser-users

TZ=Europe/Warsaw
```

`BROWSER_NET_POOL` (default `10.213.0.0/16`) must not overlap your LAN.

## 5. Podman host setup

```bash
sudo ./scripts/podman-setup.sh
```

The script:
- checks the packages, user namespaces and SELinux;
- points `/run/docker.sock` at Podman's socket (so `sudo docker ...` and the systemd units reach
  Podman);
- enables `podman-restart.service`;
- creates the `frontend` network with route metric 50.

It must end with `OK`. Check: `sudo docker version` shows `Podman Engine`.

## 6. Secrets, certificates, FreeIPA

```bash
sudo ./scripts/init-secrets.sh --self-signed-tls      # asks for the FreeIPA bind password
sudo install -m 0644 ipa-ca.crt local/freeipa-ca/     # from /etc/ipa/ca.crt on an enrolled host
sudo ./scripts/validate-freeipa.py --expect-eligible remote-browser-test --expect-denied remote-browser-disabled
```

`validate-freeipa.py` must exit 0 (`freeipa.md` §6). With a private CA for Authelia: put it in
`local/authelia-ca/` and set `AUTHELIA_CA_FILE=/etc/nginx/authelia-ca/<file>` in `.env`.

## 7. Authelia and Apache

Follow [`apache-authelia.md`](apache-authelia.md):
- Authelia: the `remote-browser` authz endpoint, the access rule for the group, the FreeIPA backend
  with its default filters, `refresh_interval: 1m`, and `regulation`.
- Apache: the vhost from `apache/remote-browser.conf`, forwarding to `https://<VM IP>:8443`, pinned to
  `local/tls/fullchain.pem` from step 6 (copy that file to Apache).

## 8. Firewall

```bash
# HTTPS_PORT from Apache only; SSH from your admin network only.
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="<Apache IP>/32" port port="8443" protocol="tcp" accept'
sudo firewall-cmd --reload
```

Podman forwards published ports with DNAT before firewalld's input rules see the packets, so don't
rely on firewalld alone for this port. `BIND_ADDRESS` and Nginx's `TRUSTED_PROXY` check (it closes
every other connection without an answer) are the enforced controls. Step 12 tests them from
another host.

Egress filter (browsers may reach the Internet, but not your LAN, this host or each other):

```bash
sed "s|@REPO_DIR@|$PWD|" systemd/remote-browser-egress.service | sudo tee /etc/systemd/system/remote-browser-egress.service >/dev/null
sudo systemctl daemon-reload && sudo systemctl enable --now remote-browser-egress.service
sudo ./scripts/egress-firewall.sh status
```

It is its own nftables table. `firewall-cmd --reload` leaves it alone.

## 9. Nginx and the login

```bash
sudo ./scripts/validate-config.sh
sudo docker compose up -d nginx
sudo docker compose ps                      # nginx: Up (healthy)

TEST_USER=remote-browser-test TEST_PASS=… OUTSIDER_USER=remote-browser-outsider OUTSIDER_PASS=… \
DISABLED_USER=remote-browser-disabled DISABLED_PASS=… ./tests/phase1-auth.sh
```

Run `phase1-auth.sh` from any machine that reaches the public URL (it needs only `curl` and
`python3`).

## 10. Browser image

```bash
sudo ./scripts/build-browser-image.sh       # ~5 GB, several minutes; prints the tag as the last line
sudoedit .env                               # BROWSER_IMAGE=<that tag>
```

## 11. Provisioning

```bash
sudo ./scripts/sync-freeipa-users.py --dry-run     # who gets a browser
sudo ./scripts/sync-freeipa-users.py
sudo docker compose ps                             # browser-<uid>: Up (healthy) after ~1 minute

for u in remote-browser-sync remote-browser-image-update remote-browser-backup; do
    for t in service timer; do
        sed "s|@REPO_DIR@|$PWD|" systemd/$u.$t | sudo tee /etc/systemd/system/$u.$t >/dev/null
    done
done
sudo systemctl daemon-reload
sudo systemctl enable --now remote-browser-sync.timer remote-browser-image-update.timer remote-browser-backup.timer
```

## 12. Acceptance tests

```bash
# Routing, from the VM. BACKEND_ADDR: the bypass test, which should also pass from another host.
sudo env BROWSER_USER=remote-browser-test BROWSER_PASS=… OTHER_USER=<second member> OTHER_PASS=… \
    PROJECT=remote-browser BACKEND_ADDR=<VM IP>:8443 ./tests/routing.sh
# Container restrictions, chooser, both sandboxes, persistence, egress:
sudo env BROWSER_USER=remote-browser-test \
    EGRESS_TARGETS="<Apache IP>:443 <Authelia IP>:9091 <Proxmox IP>:8006 <FreeIPA IP>:636" ./tests/phase2-browser.sh
# Provisioning: consistency, isolation, idempotence:
sudo ./tests/phase3-sync.sh
# Backup and restore:
sudo ./tests/backup-restore.sh
# SELinux denials during all of the above (expect none):
sudo ausearch -m avc -ts today | grep -E 'browser|nginx' || echo "no denials"
```

Then by hand (README §6):
- Log in.
  - Chrome: `chrome://sandbox` should say "adequately sandboxed".
  - Firefox: `about:support` → Sandbox should show user namespaces true.
- Disable a test user in FreeIPA, then run `sudo systemctl start remote-browser-sync`: their open
  stream ends and the profile stays.
- With FreeIPA unreachable, the sync aborts and nothing changes.

## 13. Health

```bash
sudo ./scripts/healthcheck.sh               # every line OK
```

Hook it into monitoring, e.g. root's crontab:
`*/5 * * * * /opt/remote-browser/scripts/healthcheck.sh --quiet`. Cron mails any output.

## 14. Reboot test

```bash
sudo reboot
# after boot:
sudo ./scripts/egress-firewall.sh status    # active (before any container started)
sudo docker compose ps                      # nginx and all browsers Up again (podman-restart.service)
sudo ./scripts/healthcheck.sh
```

---

## Podman-specific operations

- **After `docker compose up -d` recreated Nginx** (e.g. an Nginx update), run the sync. A new Nginx is
  on no user network until then. The same applies on Docker.
- **`docker run` by hand: always give `--pids-limit`.** Through Podman's API, a container started
  without one gets a limit of 1 process ("can't fork"). Compose services set their own limits.
- **Recreating `frontend`:** stop Nginx, `sudo podman network rm remote-browser_frontend`, then
  `sudo ./scripts/podman-setup.sh` and `sudo docker compose up -d nginx`, then the sync.

## Troubleshooting on RHEL

| Symptom | Check |
|---|---|
| Nginx won't start: permission denied on `/etc/nginx/...` | SELinux: `sudo ausearch -m avc -ts recent`. The volumes need their `:z` labels (compose.yaml); `sudo restorecon -R /opt/remote-browser` and recreate. |
| Browser starts, but sandbox checks fail, or Chrome crashes at once | `sudo ausearch -m avc -ts recent \| grep user_namespace`. If SELinux denies `user_namespace create` for `container_t`, tell me what it shows. Never use `--no-sandbox`. |
| "public Internet reachable" fails | Does any container have Internet? `sudo docker run --rm --pids-limit 64 docker.io/library/alpine:3.22 wget -qO- http://1.1.1.1 >/dev/null && echo ok`. If not, it's the host: `sudo firewall-cmd --get-active-zones`, `sudo nft list ruleset \| grep -i forward`, `sysctl net.ipv4.ip_forward`. |
| Users get "Your browser is starting" | `sudo docker compose ps`; after an Nginx recreate, run the sync. |
| `docker: Cannot connect to the Docker daemon` | `systemctl status podman.socket`; `ls -l /run/docker.sock` must point at `/run/podman/podman.sock` (`podman-setup.sh`). |

Everything else: [`troubleshooting.md`](troubleshooting.md).
