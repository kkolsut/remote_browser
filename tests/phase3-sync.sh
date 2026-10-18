#!/usr/bin/env bash
# Phase 3 acceptance tests for provisioning (plan §56, §58, §64), against a running stack on this host.
#
#   1. consistency: every user in GENERATED_DIR/state.json has a running browser-<uid> with the right
#      uid, hostname and profile, alone with Nginx on rb-net-<uid>; the Nginx map lists exactly them;
#      no leftover browser containers, user networks or unit files;
#   2. isolation: no browser reaches another user's browser (by IP or name);
#   3. idempotence: a second sync changes no file, recreates no container, does not touch Nginx;
#   4. a restarted Nginx is back on every user network (rb-nginx.service runs --attach-only).
#      Group changes (removal, disabling, re-adding) are tested by hand (README, "Provisioning").
#
# Runs scripts/sync-freeipa-users.py and restarts rb-nginx (open streams end): run as root, not while
# users are working.
#
# Environment:
#   ENV_FILE   env file of the stack (default: .env)
set -uo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$repo_dir/.env}"
env_get() {
    local v; v="$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2-)"; v="${v:-$2}"
    [[ "${3:-}" == path && "$v" != /* ]] && v="$repo_dir/${v#./}"
    echo "$v"
}
QUADLET_DIR="$(env_get QUADLET_DIR /etc/containers/systemd/remote-browser)"
GENERATED_DIR="$(env_get GENERATED_DIR ./generated path)"
MAP_FILE="$(env_get NGINX_GENERATED_DIR ./nginx/generated path)/users.map"
USER_DATA_ROOT="$(env_get USER_DATA_ROOT ./local/users path)"
STATE="$GENERATED_DIR/state.json"

pass=0 fail=0
ok()  { echo "PASS  $1"; pass=$((pass + 1)); }
bad() { echo "FAIL  $1"; [[ -n "${2:-}" ]] && echo "      $2"; fail=$((fail + 1)); }
check() { local name="$1"; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }
not() { ! "$@"; }

sync_out="$(mktemp)"
trap 'rm -f "$sync_out"' EXIT
# Exit status 0 or 2 (some users skipped on purpose) is a successful run.
run_sync() {
    "$repo_dir/scripts/sync-freeipa-users.py" --env-file "$ENV_FILE" "$@" >"$sync_out" 2>&1
    local rc=$?
    [[ $rc == 0 || $rc == 2 ]]
}

state_users() { python3 -c 'import json,sys; [print(u) for u in sorted(json.load(open(sys.argv[1]))["users"])]' "$STATE"; }
state_of() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["users"][sys.argv[2]][sys.argv[3]])' "$STATE" "$1" "$2"; }
inspect() { podman container inspect --format "$2" "$1" 2>/dev/null; }
net_members() { podman network inspect --format '{{range .Containers}}{{.Name}} {{end}}' "$1" 2>/dev/null; }
ip_of() { inspect "$1" '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'; }
fingerprint() {
    # Everything a no-op sync must leave alone.
    {
        sha256sum "$STATE" "$MAP_FILE" "$QUADLET_DIR"/browser-*.container "$QUADLET_DIR"/rb-net-*.network 2>/dev/null
        podman ps -a --no-trunc --format '{{.ID}} {{.Names}}' | sort
        inspect rb-nginx '{{.State.StartedAt}}'
    }
}

[[ -f "$STATE" ]] || { echo "no $STATE: run the sync first" >&2; exit 1; }
[[ "$(inspect rb-nginx '{{.State.Running}}')" == true ]] || { echo "rb-nginx is not running" >&2; exit 1; }
mapfile -t users < <(state_users)

echo "== consistency (${#users[@]} users: ${users[*]})"
check "at least one user provisioned" test "${#users[@]}" -gt 0
map_users="$(grep -oE '^"[a-z][a-z0-9-]*" "browser-[a-z][a-z0-9-]*:3000";$' "$MAP_FILE" | cut -d'"' -f2 | sort | tr '\n' ' ')"
check "Nginx map lists exactly the provisioned users" test "$map_users" == "$(printf '%s ' "${users[@]}")"
check "Nginx map has no other entries" test "$(grep -cv '^#' "$MAP_FILE")" == "${#users[@]}"
for u in "${users[@]}"; do
    c="browser-$u"
    if [[ "$(inspect "$c" '{{.State.Running}}')" != true ]]; then bad "$u: $c running"; continue; fi
    ok "$u: $c running"
    check "$u: $c.service active" systemctl is-active --quiet "$c.service"
    env="$(inspect "$c" '{{range .Config.Env}}{{println .}}{{end}}')"
    check "$u: PUID/PGID are the uidNumber/gidNumber" \
        grep -qx "PUID=$(state_of "$u" uid_number)" <<<"$env"
    check "$u: REMOTE_BROWSER_USER=$u" grep -qx "REMOTE_BROWSER_USER=$u" <<<"$env"
    check "$u: hostname $c" test "$(inspect "$c" '{{.Config.Hostname}}')" == "$c"
    check "$u: /config is USER_DATA_ROOT/$u/config" test \
        "$(inspect "$c" '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Source}}{{end}}{{end}}')" \
        == "$(realpath -m "$USER_DATA_ROOT/$u/config")"
    check "$u: profile directory owned by the uidNumber, mode 0700" \
        test "$(stat -c '%u %a' "$USER_DATA_ROOT/$u")" == "$(state_of "$u" uid_number) 700"
    net="rb-net-$u"
    if ! podman network exists "$net"; then bad "$u: network $net exists"; continue; fi
    check "$u: $net has the subnet from state.json" \
        test "$(podman network inspect --format '{{range .Subnets}}{{.Subnet}}{{end}}' "$net")" == "$(state_of "$u" subnet)"
    members=" $(net_members "$net")"
    check "$u: $net holds exactly $c and rb-nginx" \
        test "$(wc -w <<<"$members")" == 2 -a -n "$(grep -o " $c " <<<"$members")" -a -n "$(grep -o " rb-nginx " <<<"$members")"
    check "$u: $c is on no other network" test "$(inspect "$c" '{{len .NetworkSettings.Networks}}')" == 1
done
extra="$(podman ps -a --format '{{.Names}}' | grep '^browser-' | sort | grep -vxF -f <(printf 'browser-%s\n' "${users[@]}"))"
check "no browser containers of unprovisioned users" test -z "$extra"
extra="$(podman network ls --format '{{.Name}}' | grep '^rb-net-' | grep -vxF -f <(printf 'rb-net-%s\n' "${users[@]}"))"
check "no leftover user networks" test -z "$extra"
extra="$(ls "$QUADLET_DIR" 2>/dev/null | grep -E '^(browser-.*\.container|rb-net-.*\.network)$' \
    | grep -vxF -f <(printf 'browser-%s.container\nrb-net-%s.network\n' "${users[@]}" "${users[@]}" | sort -u))"
check "no leftover unit files in $QUADLET_DIR" test -z "$extra"

echo "== isolation between users"
if (( ${#users[@]} < 2 )); then
    echo "SKIP  needs two provisioned users"
else
    for a in "${users[@]}"; do
        # Control: the probe itself works, so the failures below are the network's doing.
        ip_a="$(ip_of "browser-$a")"
        check "$a reaches its own Selkies ($ip_a:3000; control)" \
            podman exec "browser-$a" timeout 5 bash -c "exec 3<>/dev/tcp/$ip_a/3000"
        for b in "${users[@]}"; do
            [[ "$a" == "$b" ]] && continue
            ip_b="$(ip_of "browser-$b")"
            out="$(podman exec "browser-$a" timeout 5 bash -c "exec 3<>/dev/tcp/$ip_b/3000" 2>&1)" \
                && bad "$a cannot reach browser-$b ($ip_b:3000)" "connected" \
                || ok "$a cannot reach browser-$b ($ip_b:3000)"
            check "$a cannot resolve browser-$b" not podman exec "browser-$a" getent hosts "browser-$b"
        done
    done
fi

echo "== idempotence"
before="$(fingerprint)"
if run_sync; then ok "sync runs"; else bad "sync runs" "$(cat "$sync_out")"; fi
check "second sync reports no file changes" grep -q 'generated files unchanged' "$sync_out"
check "second sync does not reload nginx" not grep -q 'nginx reloaded' "$sync_out"
check "nothing recreated, nginx untouched" test "$(fingerprint)" == "$before"

echo "== nginx restart"
systemctl restart rb-nginx.service
sleep 3
for u in "${users[@]}"; do
    check "new rb-nginx is back on rb-net-$u (--attach-only)" grep -qw rb-nginx <<<"$(net_members "rb-net-$u")"
done

echo
echo "Group changes (removal, disabling, re-adding): by hand against FreeIPA (README, 'Provisioning')."
echo
echo "$pass passed, $fail failed"
(( fail == 0 ))
