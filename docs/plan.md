# Remote Browser on Proxmox — FreeIPA Authentication, Chrome + Firefox, Persistent Per-User Profiles

> **Status:** implementation plan  
> **Supersedes:** the previous Google OAuth / per-browser-container design  
> **Primary goal:** a lightweight self-hosted remote browser service authenticated against FreeIPA, with a persistent isolated browser environment for every authorized user.

### Revision 3 (2026-10-13) — deployment behind the existing Apache and Authelia

The service runs on its own VM behind the site's **existing Apache reverse proxy** and **existing
Authelia**. Where this plan describes a public Nginx with its own TLS/ACME and a bundled Authelia
(§3, §5, §7, §10-11, §36-37, §53, §62), the implementation differs:

- Apache terminates public TLS and forwards `browser.example.com` over HTTPS to the VM's Nginx, pinned to
  the VM's certificate (`apache/remote-browser.conf`). No ACME or port 80 on the VM.
- Nginx stays the gatekeeper on the VM: `auth_request` against the existing Authelia's authz endpoint
  `remote-browser` (CookieSession only) over the LAN, identity only from Authelia, per-user routing.
  It answers only the reverse proxy (`TRUSTED_PROXY`) and takes the client address from its
  `X-Forwarded-For`.
- The Authelia configuration (FreeIPA backend, access rule, regulation, cookie domain) lives in the
  existing instance; requirements in `docs/apache-authelia.md`. A bundled Authelia remains only in the
  local demo.
- A site-wide SSO cookie would reach browser containers: Nginx strips cookies and credentials before
  proxying to a browser.
- Browser network pool `10.213.0.0/16` (outside Docker's default address pools) instead of
  `172.30.0.0/16`. Firefox's current profile location is `~/.config/mozilla/firefox` (XDG); both
  locations live in `/config`.

### Revision 2 (2026-10-13) — review changes

- Browser containers no longer share one network: **one network per user**, nginx attached to all (§30). A shared network let `browser-alice` reach `browser-bob:3000` directly, bypassing Nginx/Authelia.
- **Egress filtering** for browser networks: block RFC1918/link-local/host, allow public Internet (§30a).
- Active streams survive logout/removal; sync now **restarts/stops containers** of users who lost access, plus an admin kill command (§17).
- Chrome sandbox requires a **custom seccomp profile**; `no-new-privileges` must be tested against it (§31, §44).
- Sync uses the **same eligibility rules as Authelia**: direct group membership + not locked + password/principal not expired (§32, §35).
- Sync **fails safe**: LDAP errors abort; mass removal requires `--force` (§32).
- Bind account is an LDAP **system account** (no password expiry) (§8).
- Authelia `regulation` + 2FA readiness moved into Phase 1; SMTP is effectively required for 2FA (§11, §36).
- Per-user host UID/GID from FreeIPA `uidNumber`/`gidNumber`; per-user dirs `0700` (§19, §31).
- Weekly browser-image rebuild for Chrome/Firefox security fixes (§54).
- Stricter UID regex; stable container hostname; `--password-store=basic`; VM sizing; Firefox source; doc inconsistencies fixed.

---

## 1. Goal

Build a self-hosted remote browser service on Proxmox with these properties:

- users authenticate with their **FreeIPA username/password**;
- only members of a dedicated FreeIPA group may use the service;
- every authorized FreeIPA user gets an **isolated persistent browser environment**;
- the user can choose **Google Chrome** or **Mozilla Firefox** after entering the remote session;
- Chrome and Firefox both retain their own cookies, sessions, history, bookmarks, extensions, saved preferences, and account login state;
- one user must never see another user's browser profile;
- the VM itself has no full desktop environment;
- only the HTTPS reverse proxy is exposed publicly;
- browser/Selkies ports remain private;
- FreeIPA is the authoritative source of users and access membership;
- adding/removing access is performed primarily by changing membership in a FreeIPA group.

The design should remain simple enough to audit and maintain.

---

## 2. Main Architectural Decision

Use **one Selkies container per FreeIPA user**, not one container per browser.

Each user's container contains:

```text
Google Chrome
Mozilla Firefox
lightweight browser launcher
Selkies streaming stack
```

The user's entire home directory is persisted at `/config`.

Therefore one persistent volume naturally contains separate browser profiles:

```text
/config/.config/google-chrome/
/config/.mozilla/firefox/
```

Conceptually:

```text
FreeIPA user: alice

/srv/remote-browser/users/alice/
└── config/
    ├── .config/
    │   └── google-chrome/
    ├── .mozilla/
    │   └── firefox/
    ├── Downloads/
    └── ...
```

This is preferable to two always-running containers per user because it reduces idle resource consumption.

The user chooses which browser to launch from a small launcher presented inside the streamed session.

---

## 3. High-Level Architecture

```text
                         Internet
                            |
                            | HTTPS :443
                            v
                  +--------------------+
                  |       Nginx        |
                  | TLS termination    |
                  | auth_request       |
                  +---------+----------+
                            |
                            v
                  +--------------------+
                  |      Authelia      |
                  | FreeIPA via LDAP   |
                  +---------+----------+
                            |
                            | LDAPS
                            v
                  +--------------------+
                  |      FreeIPA       |
                  | users + groups     |
                  +--------------------+

After successful authentication:

                    Remote-User: alice
                            |
                            v
                  +--------------------+
                  |       Nginx        |
                  | user -> backend    |
                  +---------+----------+
                            |
                            v
                  +--------------------+
                  | browser-alice      |
                  | Selkies            |
                  |                    |
                  | [ Chrome ]         |
                  | [ Firefox ]        |
                  +---------+----------+
                            |
                            v
             /srv/remote-browser/users/alice/config
```

FreeIPA identity is the key used throughout the system.

Use the FreeIPA `uid` as the canonical identifier.

---

## 4. Authentication and Authorization

Replace `oauth2-proxy` with **Authelia**.

Authelia supports FreeIPA as an LDAP backend.

Authentication flow:

```text
browser.example.com
        |
        v
      Nginx
        |
        v
     Authelia
        |
        | LDAPS
        v
     FreeIPA
```

Authorization is based on a FreeIPA group.

Create:

```text
remote-browser-users
```

Only members of this group may access the remote browser service.

Example:

```text
remote-browser-users
├── alice
├── bob
└── sisio
```

Do not maintain a separate application-level allow-list.

The FreeIPA group is the authoritative access list.

---

## 5. Recommended DNS Layout

Use a dedicated subdomain namespace.

Example:

```text
browser.example.com
auth.browser.example.com
```

DNS:

```text
browser.example.com       -> PUBLIC_IP
auth.browser.example.com  -> PUBLIC_IP
```

Authelia's session cookie should be scoped to:

```text
browser.example.com
```

rather than the entire parent domain where possible.

This keeps the authentication cookie limited to the remote-browser service namespace.

---

## 6. Proxmox VM

Create a small Debian VM.

Recommended starting configuration:

```text
OS:       Debian 13 minimal
vCPU:     4 (2 is enough for Phase 1 only)
RAM:      8 GB
Balloon:  disabled (or minimum >= 75% of RAM)
Disk 1:   24 GB  (OS, Docker images)
Disk 2:   separate virtual disk mounted at /srv/remote-browser (profiles)
NIC:      VirtIO
Desktop:  none
```

There is no GPU, so Selkies encodes video in software: budget roughly **one vCPU per concurrently active stream** at 1080p, plus browser CPU.

Browser tabs, video-heavy pages, Google Maps and large web applications will dominate memory usage; budget 1.5–2 GB per active user.

A low balloon minimum makes browsers swap or get OOM-killed under pressure; do not let the host reclaim memory aggressively.

A separate data disk simplifies growth, backup and restore of profiles.

Scale based on real use.

---

## 7. Network Exposure

Only expose:

```text
TCP/443 -> Nginx
```

Optionally expose TCP/80 only for ACME HTTP validation.

Do **not** publicly expose:

```text
Authelia :9091
Selkies  :3000
Selkies  :3001
Docker API
LDAP
LDAPS
```

Administration should preferably remain reachable through LAN/WireGuard.

---

## 8. FreeIPA Preparation

Create a dedicated FreeIPA group:

```text
remote-browser-users
```

Add users who should be able to access the service.

Example:

```bash
ipa group-add remote-browser-users \
  --desc="Users allowed to access remote browser"

ipa group-add-member remote-browser-users \
  --users=alice,bob
```

Use FreeIPA as the only source of access membership.

### Service Account

Create a dedicated LDAP bind/service account for Authelia and the provisioning sync script.

Example logical name:

```text
svc_remote_browser
```

Requirements:

- read users;
- read groups;
- read membership information;
- no administrative rights;
- no password reset rights unless explicitly needed;
- long random password.

Use the minimum required FreeIPA permissions.

Do not use the FreeIPA `admin` account.

Create it as an LDAP **system account** under `cn=sysaccounts,cn=etc,<base>`, not as a regular FreeIPA user. Regular users are subject to the password policy (default 90-day expiry), which would silently break authentication and provisioning.

```text
uid=svc_remote_browser,cn=sysaccounts,cn=etc,dc=example,dc=internal
```

Use `ipa sysaccount-add` where available, otherwise `ldapmodify` as Directory Manager (see `docs/freeipa.md`).

The account must be able to read, for users: `uid`, `mail`, `displayName`, `memberOf`, `nsAccountLock`, `krbPasswordExpiration`, `krbPrincipalExpiration`, `uidNumber`, `gidNumber`; for groups: `cn`, `member`. `scripts/validate-freeipa.py` verifies this, because unreadable attributes make LDAP filters silently wrong (e.g. an unreadable `nsAccountLock` lets disabled users through; an unreadable `krbPasswordExpiration` locks everyone out).

---

## 9. LDAPS

Authelia and the provisioning script should connect to FreeIPA over:

```text
ldaps://ipa.example.internal
```

or LDAP + StartTLS if that is the site's established FreeIPA configuration.

Prefer certificate validation.

Mount the FreeIPA CA certificate into the Authelia container if the FreeIPA TLS chain is not already trusted.

Do not disable TLS verification in production.

---

## 10. Authelia FreeIPA Configuration

Conceptual configuration:

```yaml
authentication_backend:
  ldap:
    implementation: freeipa
    address: ldaps://ipa.example.internal
    base_dn: DC=example,DC=internal
    user: UID=svc_remote_browser,CN=users,CN=accounts,DC=example,DC=internal
    password: ${FREEIPA_BIND_PASSWORD}
```

Use the actual DN returned by FreeIPA rather than assuming the example DN above.

The current Authelia FreeIPA implementation provides appropriate FreeIPA attribute defaults, including:

```text
username -> uid
mail     -> mail
groups   -> cn
memberOf -> memberOf
```

It also provides FreeIPA-aware default LDAP filters intended to exclude disabled, expired-password and expired accounts.

Do not replace these filters unless there is a specific requirement.

---

## 11. Authelia Access Policy

Default policy:

```yaml
access_control:
  default_policy: deny
```

Permit only the FreeIPA group:

```yaml
access_control:
  default_policy: deny

  rules:
    - domain:
        - browser.example.com
      policy: one_factor
      subject:
        - group:remote-browser-users
```

Initially use:

```text
one_factor
```

where the first factor is the FreeIPA password.

### Brute Force and Account Lockout

Failed logins through Authelia count against the FreeIPA lockout policy. An Internet attacker can therefore lock out FreeIPA accounts domain-wide, not only for this service.

Configure Authelia `regulation` in Phase 1 so Authelia bans an attacker before FreeIPA's lockout threshold is reached:

```yaml
regulation:
  max_retries: 3
  find_time: 2m
  ban_time: 15m
```

Keep `max_retries` below FreeIPA's `krbPwdMaxFailure` (default 6).

Restrict the authz endpoint to `CookieSession` so credentials in `Authorization`/`Proxy-Authorization` headers cannot bypass the portal (and later 2FA).

### Two-Factor

Because the service is Internet-accessible, `two_factor` (TOTP or WebAuthn) is strongly recommended and the policy is a single setting (`AUTH_POLICY`) from Phase 1 onwards.

Device enrolment requires identity verification by e-mail, so SMTP is effectively required for 2FA. Without SMTP the filesystem notifier can be used, with an administrator reading verification links from the Authelia data directory — acceptable only for a handful of users.

FreeIPA remains the primary identity/password authority.

---

## 12. Authelia Headers

Nginx should obtain identity metadata only from Authelia's internal authorization response.

Required identity:

```text
Remote-User
```

Useful metadata:

```text
Remote-Groups
Remote-Email
Remote-Name
```

The canonical container/profile key is `Remote-User`, which should correspond to the FreeIPA `uid`.

---

## 13. Critical Header Security

Never trust authentication headers supplied directly by the Internet client.

Nginx must discard/overwrite incoming values for:

```text
Remote-User
Remote-Groups
Remote-Email
Remote-Name

X-Remote-User
X-Remote-Groups
X-Remote-Email
X-Remote-Name
```

Only values returned from the internal Authelia `auth_request` may be used.

Implementation notes:

- `proxy_set_header Remote-User $auth_user;` replaces any client-supplied header of the same name (also when the value is empty, the client value is not forwarded).
- Explicitly clear the `X-Remote-*` variants with `proxy_set_header X-Remote-User "";`.
- Keep `underscores_in_headers off` (default) so `Remote_User` variants are dropped.
- Routing uses the `$auth_user` variable, never a request header.

The browser containers must only be reachable from Nginx, never from each other (see §30).

---

## 14. User Provisioning Model

Authentication happens live against FreeIPA.

Container provisioning is synchronized from membership of:

```text
remote-browser-users
```

Create:

```text
scripts/sync-freeipa-users.py
```

The script queries FreeIPA through LDAP and obtains the **eligible** users: direct members of the group that also pass the same account filter Authelia uses (not locked, password not expired, principal not expired). Provisioning and authentication must agree on who is allowed.

For each user it ensures:

```text
browser-<uid> container definition
per-user Docker network
persistent data directory (owned by the user's uidNumber/gidNumber, mode 0700)
Nginx user -> backend mapping
```

Example:

```text
FreeIPA group
    |
    v
sync-freeipa-users.py
    |
    +--> generated/compose.users.yaml
    |
    +--> nginx/generated/users.map
    |
    +--> /srv/remote-browser/users/<uid>/config
```

This removes the need for a manually maintained `users.yaml`.

---

## 15. Why Provisioning Is Separate from Authentication

Do not make the public web application itself create Docker containers dynamically.

Avoid giving an Internet-facing browser broker access to:

```text
/var/run/docker.sock
```

Instead:

1. FreeIPA controls access.
2. A local provisioning script synchronizes authorized users.
3. Docker Compose creates the containers.
4. Nginx routes the authenticated user to their pre-provisioned container.

This is simpler and has a much smaller security boundary.

---

## 16. Provisioning Frequency

Support both:

```bash
./scripts/sync-freeipa-users.py
```

and a systemd timer.

Suggested timer:

```text
every 5 minutes
```

The script must be idempotent.

A manual sync should always be available for immediate changes.

---

## 17. Removing Access

When a user is removed from `remote-browser-users`:

### Authentication

Authelia should deny future authorization once group membership/session state is refreshed. Set `authentication_backend.refresh_interval` explicitly (e.g. `1m`).

### Active Sessions

`auth_request` is evaluated when the Selkies WebSocket is opened, not continuously. An already-open stream keeps working after logout, after the Authelia session expires, and after removal from the group. The only reliable way to end it is to stop or restart the user's container.

Therefore:

- the sync stops containers of users who are no longer eligible (removed, disabled, expired) — worst-case exposure is one sync interval;
- provide `scripts/kill-session.sh <uid>` to restart a user's container immediately (incident response);
- document that Authelia logout alone does not terminate an open stream.

### Provisioning

The next sync should:

- stop that user's browser container;
- remove the container definition from generated Compose;
- remove the user's network;
- remove the Nginx backend mapping;
- **retain the user's persistent profile directory**.

Do not automatically delete:

```text
/srv/remote-browser/users/<uid>/
```

User-data deletion must be a separate explicit administrative operation.

---

## 18. Repository Layout

Recommended repository:

```text
remote-browser/
├── README.md
├── compose.yaml
├── .env.example
├── .gitignore
│
├── browser-image/
│   ├── Dockerfile
│   ├── seccomp-chrome.json
│   ├── launcher/
│   │   └── browser-launcher.py
│   └── root/
│       └── defaults/
│           ├── autostart
│           └── autostart_wayland
│
├── authelia/
│   └── configuration.yml
│
├── nginx/
│   ├── nginx.conf
│   ├── templates/
│   │   └── remote-browser.conf.template
│   ├── snippets/
│   │   ├── authelia-location.conf
│   │   ├── authelia-authrequest.conf
│   │   ├── proxy.conf
│   │   ├── strip-identity-headers.conf
│   │   ├── tls.conf
│   │   └── websocket.conf
│   ├── html/
│   │   └── error pages
│   └── generated/
│       └── users.map
│
├── generated/
│   └── compose.users.yaml
│
├── scripts/
│   ├── init-secrets.sh
│   ├── freeipa_common.py
│   ├── sync-freeipa-users.py
│   ├── validate-freeipa.py
│   ├── validate-config.sh
│   ├── kill-session.sh
│   ├── egress-firewall.sh
│   ├── purge-user.py
│   └── backup.sh
│
├── tests/
│   └── phase1-auth.sh
│
├── systemd/
│   ├── remote-browser-sync.service
│   ├── remote-browser-sync.timer
│   ├── remote-browser-image-rebuild.service
│   └── remote-browser-image-rebuild.timer
│
└── docs/
    ├── freeipa.md
    ├── deployment.md
    ├── security.md
    ├── backup.md
    └── troubleshooting.md
```

Generated files must not be manually edited.

---

## 19. Persistent Storage Layout

Use:

```text
/srv/remote-browser/
├── users/
│   ├── alice/
│   │   └── config/
│   ├── bob/
│   │   └── config/
│   └── sisio/
│       └── config/
└── backups/
```

Each user container receives:

```text
/srv/remote-browser/users/<uid>/config:/config
```

The user must not be able to mount or browse another user's host directory.

Ownership:

```text
/srv/remote-browser/users/        root:root  0755
/srv/remote-browser/users/<uid>/  <uidNumber>:<gidNumber>  0700
```

Each container runs with `PUID`/`PGID` = the user's FreeIPA `uidNumber`/`gidNumber`, so different users' profiles are owned by different host UIDs. A compromise of one container (or a host process running as one user's UID) cannot read another user's profile through plain file permissions.

---

## 20. Dual-Browser Selkies Image

Build one custom image containing both:

```text
Google Chrome Stable
Mozilla Firefox
```

Use the current LinuxServer Selkies base image.

Concept:

```dockerfile
FROM ghcr.io/linuxserver/baseimage-selkies:debiantrixie
```

Install:

- official Google Chrome Stable;
- Firefox;
- a minimal launcher dependency if needed.

Do not install GNOME/KDE/XFCE.

The Selkies base already provides the streaming/display stack.

---

## 21. Chrome Installation

Install **official Google Chrome Stable** from Google's Debian repository.

Do not substitute Chromium in the final implementation.

Chrome profile should live naturally under the persistent home:

```text
/config/.config/google-chrome
```

Do not manually share the same Chrome profile between users.

---

## 22. Firefox Installation

Firefox is also required.

Install Firefox from Mozilla's official APT repository (`packages.mozilla.org`, package `firefox`), pinning the repository with APT preferences so Debian's `firefox-esr` is never pulled in instead.

LinuxServer's current Firefox image may be used as a reference for build steps.

Firefox state should live under the persistent home directory, for example:

```text
/config/.mozilla/firefox
```

Chrome and Firefox therefore remain independent while sharing the same per-user persistent volume.

---

## 23. Browser Launcher

When the Selkies session starts, do **not** automatically launch Chrome.

Launch a minimal selector.

Target appearance:

```text
+----------------------------------+
|        Choose your browser       |
|                                  |
|       [ Google Chrome ]          |
|                                  |
|       [ Mozilla Firefox ]        |
|                                  |
+----------------------------------+
```

Preferred implementation:

- tiny Python GUI or a very small launcher dependency;
- no embedded web server;
- no Docker access;
- two large buttons;
- launches browser as the same unprivileged user;
- waits for browser exit;
- returns to launcher after the browser closes.

Do not add an entire desktop environment merely to implement the chooser.

---

## 24. Launcher Behavior

Pseudo-code:

```python
while True:
    choice = show_launcher()

    if choice == "chrome":
        clear_stale_lock("~/.config/google-chrome/Singleton*")
        run(["google-chrome-stable", "--start-maximized", "--password-store=basic"])

    elif choice == "firefox":
        clear_stale_lock("~/.mozilla/firefox/*/lock", "*/.parentlock")
        run(["firefox"])
```

There is no "logout" button inside the session: a persistent container has nothing to log out of. Logout is a link in the Nginx-served page (`/logout` → Authelia logout), and it does **not** stop an open stream (§17).

Profile locks: Chrome (`SingletonLock`) and Firefox (`lock`) embed the hostname. Give every container a stable `hostname: browser-<uid>`; the launcher removes stale locks only when no browser process of that kind is running.

Password storage: the container has no keyring, so Chrome is started with `--password-store=basic`. Saved passwords are then protected only by filesystem permissions on the host. Document this for users.

Important:

- browser process should not run as root;
- if the browser crashes, return to the launcher;
- closing Chrome should not destroy Firefox settings;
- closing Firefox should not destroy Chrome settings;
- browser settings are persisted only through `/config`.

---

## 25. Browser Profile Separation

For the same FreeIPA user:

```text
alice
├── Chrome profile
└── Firefox profile
```

They are allowed to coexist in the same `/config` volume because each browser uses separate directories.

Across different FreeIPA users:

```text
alice/config
bob/config
```

must be entirely separate host directories and mounts.

Never use a shared profile volume for multiple users.

---

## 26. Browser Login Is Independent of FreeIPA

FreeIPA authentication grants access to the remote browser service.

It does **not** automatically log the user into:

```text
Google Chrome account
Google websites
Firefox Sync
websites opened in either browser
```

Users may sign into those services manually. Their login state persists in their individual browser profile.

Do not attempt to pass FreeIPA credentials or Authelia session credentials into Chrome or Firefox.

---

## 27. Nginx User-to-Container Routing

Generate a whitelist-style Nginx map.

Example:

```nginx
map $auth_user $browser_backend {
    default "";

    "alice" "browser-alice:3000";
    "bob"   "browser-bob:3000";
    "sisio" "browser-sisio:3000";
}
```

Do **not** simply concatenate arbitrary values from request headers into Docker DNS names.

The generated map is safer because only provisioned FreeIPA users have a valid backend.

If the backend map is empty, return HTTP 403 or 503 with a clear message rather than routing to a fallback browser.

Because `proxy_pass` uses a variable, Nginx resolves the backend at request time and needs Docker's resolver:

```nginx
resolver 127.0.0.11 valid=10s ipv6=off;
proxy_pass http://$browser_backend;
```

A mapped but stopped container yields 502; serve a clear "your browser is starting / unavailable" page for 502/504.

The map key is compared case-sensitively; usernames are restricted to lowercase (§33).

---

## 28. Nginx / Authelia Auth Request

Use Authelia's current Nginx `auth_request` integration.

Conceptual flow:

```text
request
   |
   v
Nginx
   |
   +--> /internal/authelia/authz
           |
           v
        Authelia
           |
           +--> 200 + Remote-User
           |
           +--> 401 / 403
```

Capture:

```nginx
auth_request_set $auth_user   $upstream_http_remote_user;
auth_request_set $auth_groups $upstream_http_remote_groups;
auth_request_set $auth_email  $upstream_http_remote_email;
auth_request_set $auth_name   $upstream_http_remote_name;
```

Use upstream examples from current Authelia documentation rather than legacy `/api/verify` configurations.

---

## 29. Selkies Reverse Proxy

Internally use the Selkies HTTP endpoint:

```text
:3000
```

behind Nginx.

Publicly expose only HTTPS.

Nginx must support:

- WebSocket upgrade;
- long read timeouts;
- streaming responses;
- required forwarded headers.

Prefer:

```yaml
expose:
  - "3000"
```

instead of host-published browser ports.

---

## 30. Docker Networks

Selkies on port 3000 has no authentication of its own. **Any container that can reach `browser-bob:3000` gets Bob's live desktop.** A single shared `browser_backend` network would let Alice type `http://browser-bob:3000` into her remote browser and bypass Nginx and Authelia entirely.

Therefore use **one network per user**:

```text
frontend          nginx (published ports)
auth              nginx, authelia
net-alice         nginx, browser-alice
net-bob           nginx, browser-bob
```

The sync script generates the per-user networks together with the services, and attaches Nginx to every one of them. Nginx gets one interface per user; that is fine at this scale.

Browser containers must not have access to Docker management APIs.

### 30a. Egress Filtering

Browser containers need public Internet access, and by default they can also reach everything the VM can: Proxmox, FreeIPA, the router, other LAN services, and the VM's own published ports. A remote browser is effectively a foothold inside the network.

Add rules to the `DOCKER-USER` iptables chain (`scripts/egress-firewall.sh`, applied by a systemd unit at boot) for the subnet range used by the browser networks:

```text
allow   established/related
allow   DNS to the Docker embedded resolver
drop    10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10
drop    169.254.0.0/16, 127.0.0.0/8, fc00::/7, fe80::/10
drop    the VM's own addresses
allow   everything else (public Internet)
```

Allocate the browser networks from one dedicated pool (e.g. `172.30.0.0/16`, `/28` per user) so the rules can target the pool.

Add to tests: from inside Alice's browser, `browser-bob:3000`, Proxmox, FreeIPA and the VM IP must all be unreachable.

---

## 31. Generated Per-User Compose Service

Example:

```yaml
services:
  browser-alice:
    image: local/remote-dual-browser:<PINNED_VERSION>
    container_name: browser-alice
    hostname: browser-alice

    environment:
      PUID: "1234500001"   # FreeIPA uidNumber
      PGID: "1234500001"   # FreeIPA gidNumber
      TZ: "Europe/Warsaw"
      REMOTE_BROWSER_USER: "alice"

    volumes:
      - /srv/remote-browser/users/alice/config:/config

    expose:
      - "3000"

    shm_size: "1gb"

    networks:
      - net-alice

    restart: unless-stopped

    security_opt:
      - seccomp=./browser-image/seccomp-chrome.json
      # - no-new-privileges:true   # enable only after verifying the Chrome sandbox still works

networks:
  net-alice:
    ipam:
      config:
        - subnet: 172.30.0.16/28
```

Chrome's sandbox needs either unprivileged user namespaces or the setuid `chrome-sandbox` helper. Docker's default seccomp profile blocks namespace creation without `CAP_SYS_ADMIN`, and `no-new-privileges` disables the setuid helper. Use a seccomp profile derived from Docker's default that additionally permits `clone`/`unshare` with namespace flags (Google's `chrome.json` is the reference), and verify `chrome://sandbox` reports a working sandbox. Do not fall back to `seccomp=unconfined` or `--no-sandbox` without a documented decision.

The VM kernel must permit unprivileged user namespaces (Debian default).

Validate compatibility of all hardening options with current Selkies/Chrome before enforcing them.

Do not use `privileged: true` unless a specific tested feature requires it.

Do not mount `/var/run/docker.sock` into browser containers.

---

## 32. FreeIPA Synchronization Script

`scripts/sync-freeipa-users.py` is a core component.

Responsibilities:

1. connect to FreeIPA over LDAPS;
2. bind using the read-only service account;
3. read the direct `member` values of `remote-browser-users`;
4. keep only users passing Authelia's FreeIPA account filter (not locked, password not expired, principal not expired);
5. extract and validate `uid`, `uidNumber`, `gidNumber`;
6. sort the user list deterministically;
7. generate per-user Compose services and networks;
8. generate the Nginx user map;
9. create missing profile directories with correct ownership/mode;
10. never delete user data;
11. validate generated configuration;
12. apply changed Compose configuration (`up -d --remove-orphans`);
13. reload Nginx only after `nginx -t` succeeds;
14. stop containers belonging to users no longer eligible.

The generated output must be deterministic.

Running the script twice with no FreeIPA changes should produce no changes.

### Fail-Safe Behaviour

- Any LDAP error (unreachable, bind failure, missing group, size limit, timeout) aborts the run **without changing anything**. An error must never be interpreted as "zero members".
- If a run would remove more than `SYNC_MAX_REMOVALS` users (default 2) or all users, abort unless `--force` is given.
- Write generated files atomically (temp file + rename) and keep the previous version for rollback.
- Use a lock (`flock`) so runs never overlap.

---

## 33. UID Validation

Never put an arbitrary LDAP value directly into path names, Docker names, or Nginx upstream values.

Validate FreeIPA `uid`.

Recommended accepted pattern:

```regex
^[a-z][a-z0-9-]{0,30}$
```

Lowercase only, no dots or underscores: the UID becomes part of Compose service names, Docker DNS names, network names and a case-sensitive Nginx map key. FreeIPA lowercases uids by default, so this rarely excludes anyone.

If a FreeIPA UID does not satisfy the deployment's safe-name rules:

- do not provision the user;
- log a clear error;
- do not silently sanitize it into a different identity.

---

## 34. FreeIPA Query

Prefer LDAP querying with a Python LDAP library such as `ldap3`.

The script should query the group by a specific DN or exact `cn`.

Example conceptual group DN:

```text
cn=remote-browser-users,cn=groups,cn=accounts,dc=example,dc=internal
```

Use the actual FreeIPA directory structure.

The script should support configuration like:

```dotenv
FREEIPA_LDAP_URL=ldaps://ipa.example.internal
FREEIPA_BASE_DN=dc=example,dc=internal
FREEIPA_BIND_DN=uid=svc_remote_browser,cn=sysaccounts,cn=etc,dc=example,dc=internal
FREEIPA_BROWSER_GROUP=remote-browser-users
```

The bind password should come from a secret file or protected runtime environment.

---

## 35. Nested FreeIPA Groups

For v1:

```text
support direct membership only
```

This matches Authelia's FreeIPA default groups filter, `(&(member={dn})(objectClass=groupOfNames))`, which only sees direct members. (FreeIPA's `memberOf` on user entries *does* include indirect groups, so the sync script must **not** use a `memberOf=` user search — that would provision nested members whom Authelia then denies.)

Document this.

---

## 36. Service Secrets

Do not store secrets in Git.

Secrets include:

```text
FREEIPA_BIND_PASSWORD
AUTHELIA_SESSION_SECRET
AUTHELIA_STORAGE_ENCRYPTION_KEY
AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET
SMTP credentials (required in practice once 2FA is enabled)
TLS private keys
```

Authelia reads each secret from a file via the `<OPTION>_FILE` environment variable (e.g. `AUTHELIA_SESSION_SECRET_FILE`). `scripts/init-secrets.sh` generates them.

Preferred storage:

1. Docker secrets;
2. root-owned files under `/etc/remote-browser/secrets/`;
3. `.env` only as a development fallback.

---

## 37. Authelia Storage

Use SQLite for a small single-instance deployment unless there is a reason to use PostgreSQL.

Persist Authelia database, configuration, and required secrets.

FreeIPA remains the source for:

```text
username
password
group membership
display name
email
```

---

## 38. Session Behavior

Expected flow:

```text
1. User opens https://browser.example.com
2. Nginx requests authorization from Authelia.
3. User logs in with FreeIPA credentials.
4. Authelia verifies the user and group membership.
5. Nginx receives Remote-User=alice.
6. Nginx maps alice -> browser-alice.
7. Selkies session opens.
8. Browser launcher appears.
9. Alice selects Chrome or Firefox.
10. Browser starts using Alice's persistent /config.
11. Closing browser returns to launcher.
```

---

## 39. Concurrent Sessions

Initial assumption:

```text
one active Selkies session per FreeIPA user
```

Do not attempt to support two independent simultaneous desktops for the same FreeIPA user in v1.

Test and document the actual Selkies behavior if the same user connects from two devices.

---

## 40. Resource Model

One idle Selkies container per authorized user is acceptable for the initial small-user deployment.

Only the selected browser needs to consume significant application memory.

Suggested:

```text
shm_size: 1 GB
```

Do not initially impose very tight RAM limits.

Monitor with:

```bash
docker stats
free -h
vmstat
```

If the number of authorized users increases materially, add on-demand container start/stop in a later phase.

---

## 41. Optional Idle Shutdown — Future Phase

Possible future model:

```text
idle user
   |
   v
stop browser container
   |
   | /config remains
   v
start container on next access
```

This requires a trusted host-side controller/session broker.

Do **not** implement this in v1.

---

## 42. Downloads

Each user's downloads should persist inside their own config tree:

```text
/srv/remote-browser/users/<uid>/config/Downloads
```

Do not share a common Downloads directory by default.

---

## 43. Clipboard and File Transfer

Selkies may provide clipboard/file-transfer capabilities.

Treat these as intentional data-transfer features.

For v1:

- enable only if required;
- document their behavior;
- verify users cannot access host files outside `/config`;
- do not mount sensitive host directories.

---

## 44. Browser Sandbox

Prefer running both browsers with their normal sandbox enabled.

Do not automatically add:

```text
--no-sandbox
```

to Chrome.

If the container environment requires disabling the sandbox, document why and record the security impact.

The expected route to a working sandbox is the custom seccomp profile described in §31. Verify with `chrome://sandbox` (Chrome) and `about:support` → Sandbox (Firefox) as part of Phase 2 acceptance.

---

## 45. Browser Account Sync

### Chrome

User may manually sign into a Google account.

State persists in:

```text
/config/.config/google-chrome
```

### Firefox

User may manually sign into Firefox Sync.

State persists in:

```text
/config/.mozilla/firefox
```

FreeIPA credentials must never be forwarded into either account system.

---

## 46. Backup

Back up:

```text
/srv/remote-browser/users/
Authelia database
Authelia configuration
Nginx configuration
FreeIPA integration configuration
deployment secrets
```

Recommended strategy:

```text
Proxmox Backup Server
+
optional file-level profile backup
```

Prefer snapshots or stop containers before a file-level browser-profile backup.

Exclude regenerable caches from file-level backups to keep them small:

```text
config/.cache/
config/.config/google-chrome/*/Cache/
config/.config/google-chrome/*/Code Cache/
config/.config/google-chrome/*/GPUCache/
config/.config/google-chrome/*/Service Worker/CacheStorage/
config/.mozilla/firefox/*/cache2/
```

---

## 47. Restore

A restore test must demonstrate:

1. restore service VM or clean Debian VM;
2. restore deployment repository;
3. restore secrets;
4. restore `/srv/remote-browser/users`;
5. start infrastructure;
6. sync FreeIPA group;
7. log in as test user;
8. open Chrome and verify state;
9. open Firefox and verify state.

---

## 48. Logging

Log:

- Nginx request status;
- Authelia authentication events;
- denied access;
- FreeIPA synchronization changes;
- failed provisioning;
- container restarts;
- profile disk usage.

Do not log credentials, auth cookies, or browser profile contents.

---

## 49. Monitoring

Monitor at minimum:

```text
VM CPU
VM RAM
filesystem usage
container restart count
Nginx health
Authelia health
FreeIPA LDAP reachability
TLS certificate expiry
```

Prometheus/Grafana/Uptime Kuma can be added later.

---

## 50. Systemd FreeIPA Sync Timer

Provide:

```text
systemd/remote-browser-sync.service
systemd/remote-browser-sync.timer
```

Suggested behavior:

```text
OnBootSec=2min
OnUnitActiveSec=5min
```

Prevent overlapping sync runs.

---

## 51. Deployment Workflow

Target process:

```bash
git clone <repo>
cd remote-browser

cp .env.example .env
$EDITOR .env

sudo ./scripts/init-secrets.sh

docker build -t local/remote-dual-browser:<VERSION> browser-image/

python3 scripts/validate-freeipa.py
python3 scripts/sync-freeipa-users.py --generate-only

docker compose \
  -f compose.yaml \
  -f generated/compose.users.yaml \
  config

docker compose \
  -f compose.yaml \
  -f generated/compose.users.yaml \
  up -d

docker compose exec nginx nginx -t
```

Only reload Nginx after validation succeeds.

---

## 52. `.env.example`

Example:

```dotenv
PUBLIC_HOST=browser.example.com
AUTHELIA_HOST=auth.browser.example.com

TZ=Europe/Warsaw
PUID=1000
PGID=1000

FREEIPA_LDAP_URL=ldaps://ipa.example.internal
FREEIPA_BASE_DN=dc=example,dc=internal
FREEIPA_BIND_DN=uid=svc_remote_browser,cn=sysaccounts,cn=etc,dc=example,dc=internal
FREEIPA_BROWSER_GROUP=remote-browser-users

USER_DATA_ROOT=/srv/remote-browser/users
SECRETS_DIR=/etc/remote-browser/secrets
AUTH_POLICY=one_factor
```

The complete, current list lives in the repository's `.env.example`.

Do not put the real bind password in `.env.example`.

---

## 53. Docker Compose Base Services

Base `compose.yaml` contains only shared infrastructure:

```text
nginx
authelia
```

Per-user browser containers live in:

```text
generated/compose.users.yaml
```

---

## 54. Image Versioning

Do not use floating `latest` tags in production.

Pin tested versions of:

```text
Authelia
Nginx
Selkies base
browser image
```

Upgrade procedure:

```text
backup
build/test
upgrade one test account
verify Chrome
verify Firefox
roll out
```

### Browser Security Updates

Pinning conflicts with browser patching: Chrome inside an image never auto-updates, and Chrome/Firefox ship security fixes roughly weekly. Pinning the *base* images is right; the *browser layer* must still be refreshed on a schedule.

- `systemd/remote-browser-image-rebuild.timer` rebuilds the browser image weekly (`--pull --no-cache` for the browser install layers), tags it with the date and the installed Chrome/Firefox versions;
- the rebuilt image is smoke-tested with the test account, then rolled out by updating the pinned tag;
- out-of-band rebuilds for actively exploited browser CVEs.

---

## 55. Test Account

Create a dedicated FreeIPA test account such as:

```text
remote-browser-test
```

Add it to `remote-browser-users`.

Use it for deployment validation.

Do not reuse the FreeIPA administrator account.

---

## 56. Automated Tests — Provisioning

Test:

- LDAP unavailable → abort, no changes;
- bad bind password → abort, no changes;
- missing group → abort, no changes;
- group with zero users → refused without `--force`;
- removal above `SYNC_MAX_REMOVALS` → refused without `--force`;
- disabled / expired user in group → not provisioned;
- nested-group member → not provisioned;
- one user;
- multiple users;
- duplicate LDAP result;
- invalid UID;
- user added;
- user removed;
- no-change sync;
- generated YAML validity;
- generated Nginx map validity.

---

## 57. Automated Tests — Authorization

Expected:

```text
not authenticated
    -> redirected to Authelia

valid FreeIPA user outside group
    -> denied

valid FreeIPA user in group
    -> allowed

disabled FreeIPA account
    -> denied

expired account/password according to backend rules
    -> denied
```

---

## 58. Automated Tests — User Isolation

Create `alice` and `bob`.

Verify:

```text
alice -> browser-alice
bob   -> browser-bob
```

Alice must not access Bob's session using modified URLs, forged identity headers, service-name guessing, query parameters, or her own browser cookies.

From **inside** Alice's remote browser (and via `docker exec browser-alice curl …`), these must be unreachable:

```text
http://browser-bob:3000
http://authelia:9091
the VM's LAN address
Proxmox web UI
FreeIPA (LDAP/HTTPS)
any RFC1918 address
```

Bob's profile directory must not be readable with Alice's UID.

---

## 59. Automated Tests — Persistence

Chrome:

```text
1. login as test user
2. choose Chrome
3. create bookmark/change setting
4. close Chrome
5. restart container
6. choose Chrome
7. verify state
```

Firefox:

```text
1. choose Firefox
2. create bookmark/change setting
3. close Firefox
4. restart container
5. choose Firefox
6. verify state
```

Also verify Chrome and Firefox settings remain independent.

---

## 60. Automated Tests — Browser Selector

Verify:

```text
session opens -> selector visible
Chrome button -> Chrome launches
close Chrome -> selector returns
Firefox button -> Firefox launches
close Firefox -> selector returns
container restart -> selector returns
```

The launcher must not execute arbitrary user-supplied commands.

---

## 61. Security Requirements

Minimum:

- HTTPS only;
- FreeIPA authentication through Authelia;
- group-based authorization;
- LDAPS with certificate validation;
- read-only FreeIPA service account;
- one isolated `/config` per user;
- one isolated container per user;
- one Docker network per user (no browser-to-browser reachability);
- browser egress to LAN/private/host addresses blocked;
- per-user host UID/GID and `0700` profile directories;
- Authelia regulation below the FreeIPA lockout threshold;
- no browser host ports;
- no Authelia public port;
- no Docker socket in browser containers;
- no privileged browser containers by default;
- client auth headers stripped/overwritten;
- Nginx routes only generated/provisioned users;
- secrets outside Git;
- pinned images;
- backups;
- no automatic deletion of user data.

Recommended:

- Authelia 2FA;
- SSH only through WireGuard/LAN;
- host firewall;
- automatic Debian security updates;
- audit FreeIPA group membership changes.

---

## 62. Phase 1 — FreeIPA Authentication MVP

Implement:

```text
Nginx
   |
Authelia
   |
FreeIPA
```

Use a static protected page initially.

Acceptance criteria:

- FreeIPA login works;
- invalid password fails;
- non-group user is denied;
- group member is allowed;
- disabled FreeIPA user is denied;
- authenticated UID is available to Nginx;
- client header spoofing does not work;
- `Authorization`/`Proxy-Authorization` basic credentials are not accepted by the authz endpoint;
- repeated bad passwords trigger Authelia regulation before FreeIPA lockout;
- `validate-freeipa.py` confirms the bind account can read all attributes the filters depend on;
- switching `AUTH_POLICY=two_factor` works (with SMTP or the filesystem notifier).

Do not add Selkies until authentication is proven.

---

## 63. Phase 2 — Single Test User / Dual Browser

Build the custom Selkies image.

Implement:

```text
Chrome
Firefox
launcher
persistent /config
seccomp profile for the Chrome sandbox
per-user network
egress firewall
```

Acceptance criteria:

- launcher appears;
- Chrome starts;
- Firefox starts;
- closing browser returns to launcher;
- Chrome settings persist;
- Firefox settings persist;
- container recreation does not lose either profile (no "profile in use" error);
- `chrome://sandbox` and Firefox's sandbox status report the sandbox active;
- egress test: public Internet reachable, LAN/RFC1918/VM addresses unreachable.

---

## 64. Phase 3 — FreeIPA Provisioning

Implement:

```text
sync-freeipa-users.py
```

Generate:

```text
generated/compose.users.yaml
nginx/generated/users.map
```

Acceptance criteria:

- adding FreeIPA group member creates environment;
- removing group member stops environment (and ends any open stream);
- disabling a FreeIPA account stops environment;
- profile data is retained;
- repeated sync is idempotent;
- LDAP failure changes nothing;
- invalid config is never applied.

---

## 65. Phase 4 — Per-User Routing

Use `Remote-User` from Authelia to route to the correct generated browser service.

Acceptance criteria:

```text
alice -> browser-alice
bob   -> browser-bob
```

Forged client headers must not influence routing.

Unknown/unprovisioned users must not fall back to another container.

---

## 66. Phase 5 — Hardening

Add:

- remaining container restrictions (`no-new-privileges`, capability drops — only where compatible with the sandbox);
- LDAPS CA validation;
- weekly browser image rebuild timer;
- secure secret files;
- firewall;
- log review;
- backup;
- 2FA if desired;
- automated tests;
- version pinning;
- security-update procedure.

---

## 67. Definition of Done

The project is complete when:

- `https://browser.example.com` is the only public application endpoint;
- users authenticate using FreeIPA credentials;
- only `remote-browser-users` members may access it;
- FreeIPA `uid` is the canonical user identifier;
- authenticated identity cannot be spoofed using request headers;
- every authorized user receives a dedicated Selkies container;
- every user receives a dedicated persistent `/config`;
- the session presents a browser chooser;
- Google Chrome is available;
- Mozilla Firefox is available;
- Chrome settings survive browser/container restart;
- Firefox settings survive browser/container restart;
- Chrome and Firefox settings remain independent;
- one user's profile is not visible to another user;
- no browser/Selkies port is exposed publicly;
- no Docker socket is mounted into browser containers;
- FreeIPA group membership drives provisioning;
- adding/removing a FreeIPA group member is synchronized automatically;
- removing access does not erase profile data;
- profile deletion requires an explicit administrative command;
- configuration generation is deterministic;
- Nginx config is validated before reload;
- browser images and infrastructure are reproducible from Git;
- persistent data and secrets are backed up;
- restore has been tested.

---

## 68. Codex Implementation Rules

1. **FreeIPA is the identity source.**
2. **The FreeIPA `remote-browser-users` group is the authorization source.**
3. Do not add a second hand-maintained application allow-list.
4. Use Authelia's FreeIPA LDAP implementation.
5. Use LDAPS with certificate verification.
6. Use `Remote-User` returned by Authelia as the canonical UID.
7. Never trust `Remote-User` supplied directly by a client.
8. Use one Selkies container per authorized FreeIPA user.
9. Install both Chrome and Firefox in that container.
10. Start a lightweight browser launcher rather than a full desktop.
11. Persist `/config` separately for every user.
12. Never share browser profile directories between users.
13. Do not dynamically expose or mount the Docker socket into the web/browser stack.
14. Provision containers using a local FreeIPA synchronization script.
15. Do not automatically delete data when access is removed.
16. Use generated whitelist-style Nginx user maps.
17. Validate FreeIPA UIDs before using them in paths or container names.
18. Fail closed: unknown users must never receive a fallback browser.
19. Keep browser ports private to the Docker network.
20. Pin tested production image versions.
21. Do not disable browser sandboxing without a documented reason.
22. Add tests for FreeIPA authorization, routing, isolation and profile persistence.
23. Implement Phase 1 before Phase 2.
24. Keep the README sufficient to deploy from a clean Debian VM.
25. Do not implement idle dynamic container creation in v1.
26. One Docker network per user; browser containers must never share a network with each other.
27. Block browser egress to private/LAN/host addresses.
28. Provisioning eligibility must match Authelia's FreeIPA filters exactly.
29. An LDAP error must never be treated as an empty group.

---

## 69. Suggested First Codex Task

Start with only:

```text
Phase 1: Nginx + Authelia + FreeIPA
```

Deliver:

```text
compose.yaml
authelia/configuration.yml
nginx/nginx.conf
nginx/templates/remote-browser.conf.template
nginx/snippets/*
nginx/html/*
scripts/init-secrets.sh
scripts/validate-freeipa.py
scripts/validate-config.sh
tests/phase1-auth.sh
docs/freeipa.md
.env.example
README.md
```

Use a protected static page as the backend.

Test:

```text
authorized group user
non-group user
bad password
disabled user
header spoof attempt
logout
session expiry
```

Only after this works should Codex implement the dual-browser Selkies image.

---

## 70. Reference Documentation

Check current upstream documentation while implementing.

Authelia FreeIPA integration:

```text
https://www.authelia.com/integration/ldap/freeipa/
```

Authelia LDAP configuration:

```text
https://www.authelia.com/configuration/first-factor/ldap/
```

Authelia Nginx integration:

```text
https://www.authelia.com/integration/proxies/nginx/
```

Authelia authorization endpoints:

```text
https://www.authelia.com/configuration/miscellaneous/server-endpoints-authz/
```

LinuxServer Selkies:

```text
https://docs.linuxserver.io/selkies/
```

LinuxServer Selkies base image:

```text
https://docs.linuxserver.io/images/docker-baseimage-selkies/
```

LinuxServer Firefox image:

```text
https://docs.linuxserver.io/images/docker-firefox/
```

If examples in this plan differ from current upstream syntax, current upstream documentation is authoritative.
