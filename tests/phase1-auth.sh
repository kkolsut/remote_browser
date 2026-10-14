#!/usr/bin/env bash
# Phase 1 end-to-end authorization tests against a running stack, through the public entry point
# (reverse proxy -> Nginx, Authelia with FreeIPA).
#
# Required environment:
#   PUBLIC_HOST, AUTHELIA_HOST      (default: read from ../.env)
#   TEST_USER / TEST_PASS           member of the access group
#   OUTSIDER_USER / OUTSIDER_PASS   valid FreeIPA user NOT in the group
# Optional:
#   DISABLED_USER / DISABLED_PASS   disabled FreeIPA user (skipped if unset)
#   TARGET_IP                       connect here instead of DNS (e.g. 127.0.0.1 on the VM)
#   PUBLIC_PORT                     public HTTPS port of the reverse proxy (default: ../.env, else 443)
#   CACERT                          CA bundle for the TLS certificate; INSECURE=1 to skip verification
#
# Each run uses one failed login for TEST_USER (bad-password test), which counts toward Authelia
# regulation (3 per 2 minutes). Do not run it more than once a minute.
set -uo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
env_get() { grep -E "^$1=" "$repo_dir/.env" 2>/dev/null | tail -n1 | cut -d= -f2-; }
PUBLIC_HOST="${PUBLIC_HOST:-$(env_get PUBLIC_HOST)}"
AUTHELIA_HOST="${AUTHELIA_HOST:-$(env_get AUTHELIA_HOST)}"
PUBLIC_PORT="${PUBLIC_PORT:-$(env_get PUBLIC_PORT)}"
PUBLIC_PORT="${PUBLIC_PORT:-443}"
port_suffix=""; [[ "$PUBLIC_PORT" == 443 ]] || port_suffix=":$PUBLIC_PORT"
PUBLIC_URL="https://$PUBLIC_HOST$port_suffix"
AUTHELIA_URL="https://$AUTHELIA_HOST$port_suffix"
: "${PUBLIC_HOST:?}" "${AUTHELIA_HOST:?}" "${TEST_USER:?}" "${TEST_PASS:?}" "${OUTSIDER_USER:?}" "${OUTSIDER_PASS:?}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

curl_opts=(-sS --max-time 15 -o "$work/body" -D "$work/headers" -w '%{http_code}')
[[ -n "${TARGET_IP:-}" ]] && curl_opts+=(--resolve "$PUBLIC_HOST:$PUBLIC_PORT:$TARGET_IP" --resolve "$AUTHELIA_HOST:$PUBLIC_PORT:$TARGET_IP")
[[ -n "${CACERT:-}" ]] && curl_opts+=(--cacert "$CACERT")
[[ "${INSECURE:-0}" == 1 ]] && curl_opts+=(-k)

pass=0 fail=0
ok()  { echo "PASS  $1"; pass=$((pass + 1)); }
bad() { echo "FAIL  $1"; [[ -n "${2:-}" ]] && echo "      $2"; fail=$((fail + 1)); }
location() { tr -d '\r' < "$work/headers" | sed -n 's/^[Ll]ocation: //p' | head -n1; }

# GET the protected page; extra args are passed to curl.
get_public() { curl "${curl_opts[@]}" "$@" "$PUBLIC_URL/"; }
# GET the identity echo, which shows the headers a backend receives.
get_whoami() { curl "${curl_opts[@]}" "$@" "$PUBLIC_URL/whoami"; }

# First-factor login through Authelia's API. $1 jar, $2 user, $3 password (via env, not argv).
login() {
    U="$2" P="$3" T="$PUBLIC_URL/" python3 -c \
        'import json,os; print(json.dumps({"username":os.environ["U"],"password":os.environ["P"],"keepMeLoggedIn":False,"targetURL":os.environ["T"]}))' \
        | curl "${curl_opts[@]}" -c "$1" -b "$1" -H 'Content-Type: application/json' --data @- \
            "$AUTHELIA_URL/api/firstfactor"
}

expect_redirect_to_portal() {
    local name="$1" code; shift
    code="$(get_public "$@")"
    if [[ "$code" == 302 && "$(location)" == "$AUTHELIA_URL/"* ]]; then ok "$name"
    else bad "$name" "got $code, Location: $(location)"; fi
}

echo "== unauthenticated"
expect_redirect_to_portal "anonymous request is redirected to Authelia"
expect_redirect_to_portal "forged Remote-User/X-Remote-User headers do not authenticate" \
    -H "Remote-User: $TEST_USER" -H "X-Remote-User: $TEST_USER" -H "Remote-Groups: remote-browser-users"
expect_redirect_to_portal "Basic credentials in Authorization header are not accepted" \
    -u "$TEST_USER:$TEST_PASS"
expect_redirect_to_portal "Basic credentials in Proxy-Authorization header are not accepted" \
    -H "Proxy-Authorization: Basic $(printf '%s:%s' "$TEST_USER" "$TEST_PASS" | base64 -w0)"

# The authz endpoints are for Nginx only; the portal's reverse proxy should refuse them.
code="$(curl "${curl_opts[@]}" "$AUTHELIA_URL/api/authz/remote-browser")"
[[ "$code" == 403 || "$code" == 404 ]] && ok "authz endpoint is not reachable from outside ($code)" \
    || bad "authz endpoint exposed by the Authelia vhost" "got $code (block /api/authz/, docs/apache-authelia.md)"

echo "== bad password"
code="$(login "$work/jar-bad" "$TEST_USER" "wrong-${RANDOM}-password")"
[[ "$code" == 401 ]] && ok "bad password is rejected" || bad "bad password is rejected" "got $code"

echo "== group member"
jar="$work/jar-member"
code="$(login "$jar" "$TEST_USER" "$TEST_PASS")"
if [[ "$code" == 200 ]]; then ok "group member can log in"; else bad "group member can log in" "got $code: $(cat "$work/body")"; fi

code="$(get_whoami -b "$jar")"
if [[ "$code" == 200 ]] && grep -qx "remote_user=$TEST_USER" "$work/body"; then ok "group member reaches backend as $TEST_USER"
else bad "group member reaches backend" "got $code: $(head -c 300 "$work/body")"; fi

code="$(get_whoami -b "$jar" -H "Remote-User: admin" -H "X-Remote-User: admin" -H "Remote-Groups: admins")"
if [[ "$code" == 200 ]] && grep -qx "remote_user=$TEST_USER" "$work/body" && grep -qx "x_remote_user=" "$work/body" \
    && ! grep -q "admins" "$work/body"; then
    ok "client identity headers are overwritten for authenticated users"
else
    bad "client identity headers are overwritten" "got $code: $(head -c 300 "$work/body")"
fi

echo "== user outside the group"
jar_out="$work/jar-outsider"
code="$(login "$jar_out" "$OUTSIDER_USER" "$OUTSIDER_PASS")"
if [[ "$code" == 200 ]]; then
    code="$(get_public -b "$jar_out")"
    [[ "$code" == 403 ]] && ok "non-group user is denied (403)" || bad "non-group user is denied" "got $code"
else
    bad "outsider first-factor login (needed for the 403 test)" "got $code"
fi

if [[ -n "${DISABLED_USER:-}" ]]; then
    echo "== disabled user"
    code="$(login "$work/jar-disabled" "$DISABLED_USER" "${DISABLED_PASS:?}")"
    [[ "$code" == 401 ]] && ok "disabled user cannot log in" || bad "disabled user cannot log in" "got $code"
else
    echo "SKIP  disabled user (DISABLED_USER not set)"
fi

echo "== logout"
code="$(curl "${curl_opts[@]}" -b "$jar" -c "$jar" -X POST "$AUTHELIA_URL/api/logout")"
[[ "$code" == 200 ]] && ok "logout request accepted" || bad "logout request accepted" "got $code"
expect_redirect_to_portal "session is invalid after logout" -b "$jar"

echo
echo "$pass passed, $fail failed"
echo "Manual: session expiry (inactivity 1h / expiration 12h), regulation ban after 3 bad passwords."
[[ $fail -eq 0 ]]
