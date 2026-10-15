#!/usr/bin/env bash
# Phase 2 acceptance tests for one user's browser container (plan §63): container hardening, chooser,
# Chrome and Firefox start/exit and sandboxes, profile persistence across a restart, egress filter.
#
# Drives the real session through xdotool inside the container; no user is logged in during the
# test (it does not need Nginx or Authelia).
#
# Environment:
#   PROJECT          Compose project name (default: remote-browser)
#   BROWSER_USER     the user whose browser is tested (service browser-<user>)
#   SERVICE          Compose service instead (default: browser-$BROWSER_USER)
#   EGRESS_TARGETS   extra "ip:port" targets that must be blocked, e.g. "192.168.1.10:8006 10.0.0.5:636"
#                    (Proxmox, FreeIPA, ...). The host's addresses and private ranges are always tested.
#   SKIP_RESTART=1   skip the container restart (persistence) part
#
# The restart closes any browser the user has open in that container.
set -uo pipefail

PROJECT="${PROJECT:-remote-browser}"
[[ -n "${SERVICE:-}${BROWSER_USER:-}" ]] || { echo "set BROWSER_USER (or SERVICE)" >&2; exit 2; }
SERVICE="${SERVICE:-browser-$BROWSER_USER}"

pass=0 fail=0
ok()  { echo "PASS  $1"; pass=$((pass + 1)); }
bad() { echo "FAIL  $1"; [[ -n "${2:-}" ]] && echo "      $2"; fail=$((fail + 1)); }
check() { local name="$1"; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }

cid="$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=$SERVICE")"
[[ -n "$cid" && "$(wc -l <<<"$cid")" == 1 ]] || { echo "no running $SERVICE container in project $PROJECT" >&2; exit 1; }

inspect() { docker inspect --format "$1" "$cid"; }
env_of() { inspect '{{range .Config.Env}}{{println .}}{{end}}' | sed -n "s/^$1=//p"; }
BROWSER_USER="${BROWSER_USER:-$(env_of REMOTE_BROWSER_USER)}"
PUID="$(env_of PUID)"
PGID="$(env_of PGID)"

# Commands as the session user, with the session's display and home.
as_user() { docker exec -u "$PUID:$PGID" -e DISPLAY=:1 -e HOME=/config "$cid" "$@"; }
as_root() { docker exec "$cid" "$@"; }
chooser_visible() { as_user xdotool search --onlyvisible --name '^Choose your browser$' >/dev/null 2>&1; }
user_procs() { as_root pgrep -u "$PUID" -x "$1" >/dev/null; }
# Wait up to $1 seconds for "$@" to succeed.
wait_for() { local t="$1"; shift; for ((i = 0; i < t * 2; i++)); do "$@" && return 0; sleep 0.5; done; return 1; }
not() { ! "$@"; }

echo "== container ($BROWSER_USER, uid $PUID)"
check "hostname is browser-$BROWSER_USER" test "$(inspect '{{.Config.Hostname}}')" == "browser-$BROWSER_USER"
check "not privileged" test "$(inspect '{{.HostConfig.Privileged}}')" == false
check "no published ports" test -z "$(inspect '{{range $p, $b := .NetworkSettings.Ports}}{{if $b}}{{$p}} {{end}}{{end}}')"
check "no Docker socket mounted" not grep -q docker.sock <<<"$(inspect '{{range .Mounts}}{{.Source}} {{end}}')"
check "custom seccomp profile (not unconfined)" grep -q 'seccomp={' <<<"$(inspect '{{json .HostConfig.SecurityOpt}}')"
networks="$(inspect '{{range $n, $_ := .NetworkSettings.Networks}}{{$n}} {{end}}')"
check "attached to exactly one network" test "$(wc -w <<<"$networks")" == 1
net="${networks%% *}"
members="$(docker network inspect --format '{{range .Containers}}{{.Name}} {{end}}' "$net")"
check "its network holds only this browser and nginx" test "$(wc -w <<<"$members")" == 2 -a -n "$(grep -o "${PROJECT}-nginx-1" <<<"$members")"
# Nginx reaches Authelia over its default route; through a browser network it would be egress-filtered.
nginx_cid="$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=nginx")"
front_gw="$(docker network inspect --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}' "${PROJECT}_frontend")"
nginx_gw="$(docker exec "$nginx_cid" ip route 2>&1 | sed -n 's/^default via \([0-9.]*\).*/\1/p')"
check "nginx's default route is the frontend network (via ${nginx_gw:-none})" test -n "$front_gw" -a "$nginx_gw" == "$front_gw"

echo "== user and profile"
check "PUID is not root" test "$PUID" != 0
check "/config owned by PUID, mode 0700" test "$(as_root stat -c '%u %a' /config)" == "$PUID 700"
check "launcher runs as the session user" test -n "$(as_root pgrep -u "$PUID" -f /usr/local/bin/browser-launcher)"
check "no browser-launcher runs as root" not as_root pgrep -u 0 -f /usr/local/bin/browser-launcher
check "no sudo in the image" not as_root sh -c 'command -v sudo'
groups="$(as_user id -Gn 2>/dev/null)"
check "session user not in sudo/docker groups" not grep -qwE 'sudo|docker' <<<"$groups"

echo "== chooser"
check "chooser visible at session start" wait_for 30 chooser_visible

# Press a key in the chooser window. Real (XTEST) key events: GTK ignores synthetic ones (--window).
press() { as_user sh -c "w=\$(xdotool search --onlyvisible --name '^Choose your browser\$' | head -n1) && xdotool windowactivate --sync \$w && xdotool key $1"; }

# Sandbox checks on the processes of the browser the chooser started (plan §44). chrome://sandbox
# cannot be rendered headless, so the kernel's view is checked instead.
# Chrome: every renderer sits in a nested PID namespace (NSpid lists more than one pid) under seccomp.
chrome_sandboxed() {
    as_root sh -c '
        n=0
        for p in $(pgrep -u "$1" -f -- "--type=renderer"); do
            n=$((n + 1))
            [ "$(grep -c "^Seccomp:[[:space:]]*2" /proc/$p/status)" = 1 ] || exit 1
            [ "$(awk "/^NSpid:/ { print NF - 1 }" /proc/$p/status)" -ge 2 ] || exit 1
        done
        [ $n -gt 0 ]' sh "$PUID"
}
# Firefox: every web content process has its own user and network namespace (no network access).
firefox_sandboxed() {
    as_user sh -c '
        n=0; own_user=$(readlink /proc/self/ns/user); own_net=$(readlink /proc/self/ns/net)
        for p in $(pgrep -u "$(id -u)" -x "Web Content|Isolated Web Co"); do
            n=$((n + 1))
            u=$(readlink /proc/$p/ns/user) && t=$(readlink /proc/$p/ns/net) || exit 1
            [ "$u" != "$own_user" ] && [ "$t" != "$own_net" ] || exit 1
        done
        [ $n -gt 0 ]'
}

run_browser_cycle() {
    local name="$1" key="$2" proc="$3" sandbox_check="${4:-}"
    press "$key"
    check "$name: starts from the chooser" wait_for 40 user_procs "$proc"
    check "$name: chooser hidden while running" wait_for 10 not chooser_visible
    sleep 8   # let it open its first page and write the profile
    [[ -n "$sandbox_check" ]] && check "$name: sandbox active" "$sandbox_check"
    as_root pkill -TERM -u "$PUID" -x "$proc"
    check "$name: chooser returns after the browser exits" wait_for 30 chooser_visible
}

run_browser_cycle Chrome c chrome chrome_sandboxed
run_browser_cycle Firefox f firefox-bin firefox_sandboxed

check "Chrome profile exists in /config" as_user test -d /config/.config/google-chrome/Default
check "Firefox profile exists in /config" as_user sh -c 'ls -d /config/.mozilla/firefox/*/prefs.js /config/.config/mozilla/firefox/*/prefs.js 2>/dev/null | grep -q .'

if [[ "${SKIP_RESTART:-0}" != 1 ]]; then
    echo "== persistence across a container restart"
    # Leave a marker in each profile, start Chrome, then restart under it (stale SingletonLock).
    as_user sh -c 'echo rb-test > /config/.config/google-chrome/rb-persistence-marker'
    as_user sh -c 'for d in /config/.mozilla/firefox/*/ /config/.config/mozilla/firefox/*/; do [ -f "$d/prefs.js" ] && echo rb-test > "$d/rb-persistence-marker"; done; true'
    press c
    wait_for 40 user_procs chrome
    sleep 5
    docker restart "$cid" >/dev/null
    check "chooser returns after container restart" wait_for 90 chooser_visible
    check "Chrome marker survived" as_user grep -q rb-test /config/.config/google-chrome/rb-persistence-marker
    check "Firefox marker survived" as_user sh -c 'cat /config/.mozilla/firefox/*/rb-persistence-marker /config/.config/mozilla/firefox/*/rb-persistence-marker 2>/dev/null | grep -q rb-test'
    run_browser_cycle "Chrome after restart (no 'profile in use')" c chrome
    run_browser_cycle "Firefox after restart" f firefox-bin
    as_user sh -c 'rm -f /config/.config/google-chrome/rb-persistence-marker /config/.mozilla/firefox/*/rb-persistence-marker /config/.config/mozilla/firefox/*/rb-persistence-marker'
fi

echo "== egress"
code="$(as_user curl -sS -m 15 -o /dev/null -w '%{http_code}' https://www.google.com/generate_204 2>&1)"
check "public Internet reachable (got $code)" test "$code" == 204

# A blocked target must fail at once with "No route to host" (REJECT icmp-admin-prohibited).
# "Connection refused" or a successful connect means the target was reached.
blocked() {
    local target="$1" out
    out="$(as_user timeout 8 bash -c "exec 3<>/dev/tcp/${target%:*}/${target##*:}" 2>&1)"
    if grep -q 'No route to host' <<<"$out"; then ok "blocked: $target"
    else bad "blocked: $target" "${out:-connected}"; fi
}
gateway="$(docker network inspect --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}' "$net")"
host_ip="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
for target in "$gateway:22" "${host_ip:-}:22" 10.0.0.1:80 172.16.0.1:80 192.168.0.1:80 100.64.0.1:80 169.254.169.254:80 ${EGRESS_TARGETS:-}; do
    [[ "$target" == :* ]] && continue
    blocked "$target"
done
out="$(as_user curl -sS -m 5 http://authelia:9091/ 2>&1)"
check "Authelia not resolvable/reachable from the browser" grep -qiE 'resolve|No route' <<<"$out"

echo
echo "$pass passed, $fail failed"
(( fail == 0 ))
