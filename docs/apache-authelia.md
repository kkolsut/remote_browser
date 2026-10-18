# Running behind the existing Apache and Authelia

```text
client ──HTTPS──▶ Apache (existing)  ──HTTPS, pinned cert──▶ Nginx (browser VM :HTTPS_PORT) ──▶ browser-<uid>
                  browser.example.com                          │  per-user Podman networks
                  auth.example.com ──▶ Authelia (existing)     │
                                          ▲                    │
                                          └──── authz (LAN) ───┘
```

- **Apache** terminates public TLS for `browser.example.com` and forwards everything to the VM. It does no
  authentication itself.
- **Nginx on the VM** asks Authelia about every request (`auth_request`), takes the identity from
  Authelia's answer only, and routes the user to their own container. It answers only Apache
  (`TRUSTED_PROXY`).
- **Authelia** (yours) checks the FreeIPA password and the group, and serves the login portal as today.

The per-user routing has to happen on the VM: the browser containers are reachable only on per-user
Podman networks there, and Selkies has no authentication of its own.

## 1. Authelia

Your Authelia must be 4.38 or newer. Add the following to its configuration and validate it
(`authelia validate-config`) before restarting.

### 1.1 Authz endpoint for this service

```yaml
server:
  endpoints:
    authz:
      remote-browser:
        implementation: 'AuthRequest'
        # Session cookie only: credentials in Authorization headers would bypass the portal,
        # regulation and 2FA.
        authn_strategies:
          - name: 'CookieSession'
```

> **Warning.** As soon as `server.endpoints.authz` contains any endpoint, Authelia no longer creates its
> built-in ones (`auth-request`, `forward-auth`, `ext-authz`). Verified with 4.39.28: they return 404. If
> other applications use them, declare them as well:
>
> ```yaml
>       auth-request:
>         implementation: 'AuthRequest'
>       forward-auth:
>         implementation: 'ForwardAuth'
>       ext-authz:
>         implementation: 'ExtAuthz'
> ```

Nginx calls it at `AUTHELIA_AUTHZ_URL`, e.g. `https://authelia.example.internal:9091/api/authz/remote-browser`.
Use a direct LAN route to Authelia, not the public portal vhost. Prefer HTTPS: whoever answers
this request decides who the user is (`Remote-User`), so on plain HTTP anyone who can tamper with LAN
traffic between the VM and Authelia could log in as anyone. With a private CA, put its certificate in
`AUTHELIA_CA_DIR` and set `AUTHELIA_CA_FILE=/etc/nginx/authelia-ca/<file>`.

Block `/api/authz/` on the public Authelia vhost, where clients have no business calling it:

```apache
<Location /api/authz/>
    Require all denied
</Location>
ProxyPass /api/authz/ !
```

(before the vhost's `ProxyPass /`). `tests/phase1-auth.sh` checks this.

### 1.2 Access rule

Only the FreeIPA group may use the service. Put these rules **before** any wildcard rule that would also
match `browser.example.com` (e.g. `*.example.com`). Authelia uses the first matching rule.

```yaml
access_control:
  rules:
    - domain: 'browser.example.com'
      policy: 'one_factor'          # or two_factor
      subject:
        - 'group:remote-browser-users'
    - domain: 'browser.example.com'
      policy: 'deny'
```

### 1.3 Session cookie

`browser.example.com` must be covered by one of your `session.cookies` domains (e.g. `example.com`
with `authelia_url: https://auth.example.com`). Then the existing SSO session also works for the
remote browser.

The plan (§5) prefers a cookie scoped to the service. With a shared Authelia that means a separate cookie
domain `browser.example.com`, whose portal must be inside it: an extra Apache vhost
`auth.browser.example.com` → the same Authelia, and

```yaml
session:
  cookies:
    - domain: 'browser.example.com'
      authelia_url: 'https://auth.browser.example.com'
```

Users then log in once more for this service. Either way, Nginx strips all cookies before a request
reaches a browser container, so the SSO cookie never gets into a user's remote session.

### 1.4 Backend and session settings this service relies on

- **FreeIPA backend** with `implementation: 'freeipa'` and its default users/groups filters. They reject
  locked, password-expired and principal-expired accounts and see only direct group members. The
  provisioning sync (Phase 3) applies the same rules; with other filters, login and provisioning would
  disagree about who is allowed.
- **`authentication_backend.refresh_interval`** of a few minutes at most (e.g. `1m`), so removal from
  the group or disabling an account ends access to new requests quickly.
- **`regulation`** below the FreeIPA lockout threshold (`docs/freeipa.md` §7), e.g. 3 retries in 2 minutes.
- Authelia sees the real client address: Apache appends it to `X-Forwarded-For`, and Nginx forwards it
  in the authz request.

## 2. Apache

Needs Apache 2.4.47 or newer (`upgrade=websocket`): `a2enmod ssl proxy proxy_http headers`.

1. Copy the VM's certificate, which Apache pins (`local/tls/fullchain.pem`, made by
   `./scripts/init-secrets.sh --self-signed-tls`, valid 10 years), and the vhost file:

   ```bash
   sudo install -d /etc/apache2/remote-browser
   sudo install -m 0644 fullchain.pem /etc/apache2/remote-browser/backend.pem
   sudo install -m 0644 apache/remote-browser.conf /etc/apache2/remote-browser/remote-browser.conf
   ```

2. `/etc/apache2/sites-available/remote-browser.conf`:

   ```apache
   Define RB_PUBLIC_HOST   browser.example.com
   Define RB_BACKEND       192.168.10.50:8443
   Define RB_BACKEND_CERT  /etc/apache2/remote-browser/backend.pem
   Define RB_CERT          /etc/letsencrypt/live/browser.example.com/fullchain.pem
   Define RB_KEY           /etc/letsencrypt/live/browser.example.com/privkey.pem
   Include /etc/apache2/remote-browser/remote-browser.conf
   ```

3. `sudo a2ensite remote-browser && sudo apachectl configtest && sudo systemctl reload apache2`

What the vhost does (`apache/remote-browser.conf`):
- `ProxyPreserveHost On`: Nginx sees `browser.example.com`. Apache also uses that name for SNI and checks
  the VM's certificate against it.
- `SSLProxyVerify require` against the pinned certificate.
- WebSockets through to the VM (`upgrade=websocket`, 1 h timeout): the Selkies stream.
- Sets `X-Forwarded-Proto`; drops client-supplied `Remote-*` headers (Nginx overwrites them anyway).

In this homelab, web01's vhost is rendered by Ansible from the same rules
(`roles/web-server/templates/vhosts/browser.kolsut.net.conf.j2`).

## 3. The VM

`.env` (see `.env.example`):

```dotenv
PUBLIC_HOST=browser.example.com
AUTHELIA_HOST=auth.example.com
HTTPS_PORT=8443
TRUSTED_PROXY=<Apache's address>/32
AUTHELIA_AUTHZ_URL=https://authelia.example.internal:9091/api/authz/remote-browser
```

Nginx closes connections from any other address than `TRUSTED_PROXY` without an answer. Restrict
`HTTPS_PORT` to Apache in the VM firewall as well. If Apache reaches the VM through NAT, `TRUSTED_PROXY`
is the address the VM sees.

Nginx's default route must be its `rb-frontend` network (route metric 50, `rb-frontend.network`). Through a
browser network, its requests to Authelia would be rejected by the egress filter.
`tests/phase2-browser.sh` checks this.

## 4. Test through the public URL

```bash
TEST_USER=remote-browser-test TEST_PASS=… OUTSIDER_USER=remote-browser-outsider OUTSIDER_PASS=… \
DISABLED_USER=remote-browser-disabled DISABLED_PASS=… ./tests/phase1-auth.sh

BROWSER_USER=remote-browser-test BROWSER_PASS=… BACKEND_ADDR=<vm-ip>:8443 ./tests/routing.sh
```

Run them from a machine that reaches the public name (they use real DNS unless `TARGET_IP` is set).
`BACKEND_ADDR` checks that a connection that bypasses Apache gets no answer.
