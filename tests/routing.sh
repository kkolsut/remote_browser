#!/usr/bin/env bash
# User -> browser routing through Nginx + Authelia (plan §27, §58, §65), against a running stack.
#
# Required environment:
#   BROWSER_USER / BROWSER_PASS           user WITH a provisioned browser container
#   OTHER_USER / OTHER_PASS               a second user with a browser (optional): each user reaches only
#                                         their own container
#   UNPROVISIONED_USER / UNPROVISIONED_PASS   group member WITHOUT a browser (optional, skipped if unset)
#   ACCESS_LOG=1                          on the browser VM as root: the Nginx access log (podman logs
#                                         rb-nginx) proves which container served each user
#   PUBLIC_HOST, AUTHELIA_HOST, PUBLIC_PORT, TARGET_IP, CACERT, INSECURE   as for tests/phase1-auth.sh
#   BACKEND_ADDR                          ip:port of the browser VM's Nginx (HTTPS_PORT), optional:
#                                         a connection that bypasses the reverse proxy must get no answer
#
# Uses no wrong passwords, so it does not count toward Authelia regulation.
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
: "${PUBLIC_HOST:?}" "${AUTHELIA_HOST:?}" "${BROWSER_USER:?}" "${BROWSER_PASS:?}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

curl_opts=(-sS --max-time 15 -o "$work/body" -D "$work/headers" -w '%{http_code}')
[[ -n "${TARGET_IP:-}" ]] && curl_opts+=(--resolve "$PUBLIC_HOST:$PUBLIC_PORT:$TARGET_IP" --resolve "$AUTHELIA_HOST:$PUBLIC_PORT:$TARGET_IP")
[[ -n "${CACERT:-}" ]] && curl_opts+=(--cacert "$CACERT")
[[ "${INSECURE:-0}" == 1 ]] && curl_opts+=(-k)

pass=0 fail=0
ok()  { echo "PASS  $1"; pass=$((pass + 1)); }
bad() { echo "FAIL  $1"; [[ -n "${2:-}" ]] && echo "      $2"; fail=$((fail + 1)); }

login() {
    U="$2" P="$3" T="$PUBLIC_URL/" python3 -c \
        'import json,os; print(json.dumps({"username":os.environ["U"],"password":os.environ["P"],"keepMeLoggedIn":False,"targetURL":os.environ["T"]}))' \
        | curl "${curl_opts[@]}" -c "$1" -b "$1" -H 'Content-Type: application/json' --data @- \
            "$AUTHELIA_URL/api/firstfactor"
}

# WebSocket handshake to the Selkies stream; $1 jar, $2 Origin. Prints the status code.
ws_handshake() {
    curl "${curl_opts[@]}" --http1.1 -b "$1" --max-time 5 \
        -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
        -H "Sec-WebSocket-Key: $(head -c16 /dev/urandom | base64)" -H "Origin: $2" \
        "$PUBLIC_URL/api/websockets" 2>/dev/null
}

# Did the last request of user $1 reach the container browser-$1? (Nginx logs upstream=<ip:port>.)
served_by_own_browser() {
    local user="$1" ips addr
    ips=" $(podman container inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "browser-$user" 2>/dev/null)"
    [[ "$ips" != " " ]] || return 1
    addr="$(podman logs --since 2m rb-nginx 2>/dev/null | grep -F " - $user [" | grep -o 'upstream=[0-9.:]*' | tail -n1)"
    addr="${addr#upstream=}"
    [[ -n "$addr" && "$ips" == *" ${addr%:*} "* ]]
}
route_check() {
    [[ "${ACCESS_LOG:-0}" == 1 ]] || return 0
    if served_by_own_browser "$1"; then ok "Nginx routed $1 to browser-$1"
    else bad "Nginx routed $1 to browser-$1" "upstream in the access log is not that container"; fi
}

echo "== provisioned user ($BROWSER_USER)"
jar="$work/jar"
code="$(login "$jar" "$BROWSER_USER" "$BROWSER_PASS")"
[[ "$code" == 200 ]] && ok "login" || bad "login" "got $code"

code="$(curl "${curl_opts[@]}" -b "$jar" "$PUBLIC_URL/")"
if [[ "$code" == 200 ]] && grep -qi 'selkies' "$work/body"; then ok "/ serves the user's Selkies session"
else bad "/ serves the user's Selkies session" "got $code: $(head -c 200 "$work/body")"; fi

route_check "$BROWSER_USER"

code="$(ws_handshake "$jar" "$PUBLIC_URL")"
[[ "$code" == 101 ]] && ok "stream WebSocket upgrades through Nginx (101)" || bad "stream WebSocket upgrades through Nginx" "got $code"

code="$(ws_handshake "$jar" "https://evil.example")"
[[ "$code" != 101 ]] && ok "cross-site Origin cannot open the stream (got $code)" || bad "cross-site Origin cannot open the stream" "got 101"

code="$(ws_handshake "$work/no-jar" "$PUBLIC_URL")"
[[ "$code" != 101 ]] && ok "stream refuses unauthenticated clients (got $code)" || bad "stream refuses unauthenticated clients" "got 101"

# Forged identity headers must not change the route: the response must still be this user's
# session (the backend sees the real identity, which /whoami shows).
code="$(curl "${curl_opts[@]}" -b "$jar" -H "Remote-User: someone-else" -H "X-Remote-User: someone-else" "$PUBLIC_URL/whoami")"
if [[ "$code" == 200 ]] && grep -qx "remote_user=$BROWSER_USER" "$work/body"; then ok "forged Remote-User does not change identity"
else bad "forged Remote-User does not change identity" "got $code: $(head -c 200 "$work/body")"; fi

if [[ -n "${OTHER_USER:-}" ]]; then
    echo "== second user ($OTHER_USER)"
    jar3="$work/jar3"
    code="$(login "$jar3" "$OTHER_USER" "$OTHER_PASS")"
    [[ "$code" == 200 ]] && ok "login" || bad "login" "got $code"
    code="$(curl "${curl_opts[@]}" -b "$jar3" -H "Remote-User: $BROWSER_USER" "$PUBLIC_URL/")"
    if [[ "$code" == 200 ]] && grep -qi 'selkies' "$work/body"; then ok "/ serves a Selkies session"
    else bad "/ serves a Selkies session" "got $code: $(head -c 200 "$work/body")"; fi
    route_check "$OTHER_USER"
    # And back: the first user still reaches their own container, not the one used last.
    curl "${curl_opts[@]}" -b "$jar" "$PUBLIC_URL/" >/dev/null
    route_check "$BROWSER_USER"
fi

if [[ -n "${UNPROVISIONED_USER:-}" ]]; then
    echo "== group member without a browser ($UNPROVISIONED_USER)"
    jar2="$work/jar2"
    code="$(login "$jar2" "$UNPROVISIONED_USER" "$UNPROVISIONED_PASS")"
    [[ "$code" == 200 ]] && ok "login" || bad "login" "got $code"
    code="$(curl "${curl_opts[@]}" -b "$jar2" "$PUBLIC_URL/")"
    if [[ "$code" == 503 ]] && grep -q 'No browser yet' "$work/body" && ! grep -qi selkies "$work/body"; then
        ok "gets the 'no browser yet' page (503), no fallback browser"
    else
        bad "gets the 'no browser yet' page, no fallback browser" "got $code: $(head -c 200 "$work/body")"
    fi
    code="$(ws_handshake "$jar2" "$PUBLIC_URL")"
    [[ "$code" != 101 ]] && ok "cannot open any stream (got $code)" || bad "cannot open any stream" "got 101"
    code="$(curl "${curl_opts[@]}" -b "$jar2" -H "Remote-User: $BROWSER_USER" "$PUBLIC_URL/")"
    [[ "$code" == 503 ]] && ok "forged Remote-User: $BROWSER_USER does not reach that browser" || bad "forged Remote-User does not reach another browser" "got $code"
fi

if [[ -n "${BACKEND_ADDR:-}" ]]; then
    echo "== bypassing the reverse proxy ($BACKEND_ADDR)"
    # Nginx closes connections from anyone but TRUSTED_PROXY without a response (444).
    code="$(curl -sS -k --max-time 10 -o /dev/null -w '%{http_code}' -b "$jar" \
        --resolve "$PUBLIC_HOST:${BACKEND_ADDR##*:}:${BACKEND_ADDR%:*}" "https://$PUBLIC_HOST:${BACKEND_ADDR##*:}/" 2>/dev/null)"
    [[ "$code" == 000 ]] && ok "direct connection to Nginx gets no response" || bad "direct connection to Nginx gets no response" "got $code"
fi

echo
echo "$pass passed, $fail failed"
(( fail == 0 ))
