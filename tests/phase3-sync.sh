#!/usr/bin/env bash
# Phase 3 acceptance tests for provisioning (plan §56, §58, §64), against a running stack on this host.
#
#   1. consistency: every user in GENERATED_DIR/state.json has a running browser-<uid> with the right
#      uid, hostname and profile, alone with Nginx on net-<uid>; the Nginx map lists exactly them;
#      no leftover browser containers or user networks;
#   2. isolation: no browser reaches another user's browser (by IP or name);
#   3. idempotence: a second sync changes no file, recreates no container, does not touch Nginx;
#   4. with AUTH_BACKEND=file (the demo) also: an unreadable source and an empty group change nothing;
#      removing a user from the group removes the container and network, keeps the profile; adding
#      the user back restores the browser with the same subnet and profile.
#      With FreeIPA, do 4 by hand (README, "Provisioning").
#
# Runs scripts/sync-freeipa-users.py, so it needs the same rights (root in production).
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
PROJECT="$(env_get COMPOSE_PROJECT_NAME remote-browser)"
GENERATED_DIR="$(env_get GENERATED_DIR ./generated path)"
MAP_FILE="$(env_get NGINX_GENERATED_DIR ./nginx/generated path)/users.map"
USER_DATA_ROOT="$(env_get USER_DATA_ROOT ./local/users path)"
BACKEND="$(env_get AUTH_BACKEND freeipa)"
STATE="$GENERATED_DIR/state.json"

pass=0 fail=0
ok()  { echo "PASS  $1"; pass=$((pass + 1)); }
bad() { echo "FAIL  $1"; [[ -n "${2:-}" ]] && echo "      $2"; fail=$((fail + 1)); }
check() { local name="$1"; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }
not() { ! "$@"; }

sync_out="$(mktemp)"
trap 'rm -f "$sync_out" "${users_tmp:-}"' EXIT
# Exit status 0 or 2 (some users skipped on purpose) is a successful run.
run_sync() {
    "$repo_dir/scripts/sync-freeipa-users.py" --env-file "$ENV_FILE" "$@" >"$sync_out" 2>&1
    local rc=$?
    [[ $rc == 0 || $rc == 2 ]]
}

state_users() { python3 -c 'import json,sys; [print(u) for u in sorted(json.load(open(sys.argv[1]))["users"])]' "$STATE"; }
state_of() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["users"][sys.argv[2]][sys.argv[3]])' "$STATE" "$1" "$2"; }
cid_of() {
    docker ps -q --no-trunc --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=$1"
}
net_of() {
    docker network ls -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.network=net-$1"
}
fingerprint() {
    # Everything a no-op sync must leave alone.
    {
        sha256sum "$STATE" "$MAP_FILE" "$GENERATED_DIR/compose.users.yaml" 2>/dev/null
        docker ps -a --no-trunc --filter "label=com.docker.compose.project=$PROJECT" --format '{{.ID}} {{.Names}}' | sort
        docker inspect --format '{{.State.StartedAt}}' "$(cid_of nginx)"
    }
}

[[ -f "$STATE" ]] || { echo "no $STATE: run the sync first" >&2; exit 1; }
nginx="$(cid_of nginx)"
[[ -n "$nginx" ]] || { echo "nginx of project $PROJECT is not running" >&2; exit 1; }
mapfile -t users < <(state_users)

echo "== consistency (${#users[@]} users: ${users[*]})"
check "at least one user provisioned" test "${#users[@]}" -gt 0
map_users="$(grep -oE '^"[a-z][a-z0-9-]*" "browser-[a-z][a-z0-9-]*:3000";$' "$MAP_FILE" | cut -d'"' -f2 | sort | tr '\n' ' ')"
check "Nginx map lists exactly the provisioned users" test "$map_users" == "$(printf '%s ' "${users[@]}")"
check "Nginx map has no other entries" test "$(grep -cv '^#' "$MAP_FILE")" == "${#users[@]}"
for u in "${users[@]}"; do
    cid="$(cid_of "browser-$u")"
    if [[ -z "$cid" ]]; then bad "$u: browser-$u running"; continue; fi
    ok "$u: browser-$u running"
    env="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$cid")"
    check "$u: PUID/PGID are the uidNumber/gidNumber" \
        grep -qx "PUID=$(state_of "$u" uid_number)" <<<"$env"
    check "$u: REMOTE_BROWSER_USER=$u" grep -qx "REMOTE_BROWSER_USER=$u" <<<"$env"
    check "$u: hostname browser-$u" test "$(docker inspect --format '{{.Config.Hostname}}' "$cid")" == "browser-$u"
    check "$u: /config is USER_DATA_ROOT/$u/config" test \
        "$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Source}}{{end}}{{end}}' "$cid")" \
        == "$(realpath -m "$USER_DATA_ROOT/$u/config")"
    check "$u: profile directory owned by the uidNumber, mode 0700" \
        test "$(stat -c '%u %a' "$USER_DATA_ROOT/$u")" == "$(state_of "$u" uid_number) 700"
    net="$(net_of "$u")"
    if [[ -z "$net" ]]; then bad "$u: network net-$u exists"; continue; fi
    check "$u: net-$u has the subnet from state.json" \
        test "$(docker network inspect --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' "$net")" == "$(state_of "$u" subnet)"
    members="$(docker network inspect --format '{{range $id, $_ := .Containers}}{{$id}} {{end}}' "$net")"
    check "$u: net-$u holds exactly browser-$u and nginx" \
        test "$(tr ' ' '\n' <<<"$members" | grep -c .)" == 2 -a -n "$(grep -o "$cid" <<<"$members")" -a -n "$(grep -o "$nginx" <<<"$members")"
    check "$u: browser-$u is on no other network" \
        test "$(docker inspect --format '{{len .NetworkSettings.Networks}}' "$cid")" == 1
done
extra="$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Label "com.docker.compose.service"}}' \
    | grep '^browser-' | sort | grep -vxF -f <(printf 'browser-%s\n' "${users[@]}"))"
check "no browser containers of unprovisioned users" test -z "$extra"
extra="$(docker network ls --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Label "com.docker.compose.network"}}' \
    | grep -E '^(net-|browser-test$)' | grep -vxF -f <(printf 'net-%s\n' "${users[@]}"))"
check "no leftover user networks" test -z "$extra"

echo "== isolation between users"
if (( ${#users[@]} < 2 )); then
    echo "SKIP  needs two provisioned users"
else
    for a in "${users[@]}"; do
        # Control: the probe itself works, so the failures below are the network's doing.
        ip_a="$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$(cid_of "browser-$a")")"
        check "$a reaches its own Selkies ($ip_a:3000; control)" \
            docker exec "$(cid_of "browser-$a")" timeout 5 bash -c "exec 3<>/dev/tcp/$ip_a/3000"
        for b in "${users[@]}"; do
            [[ "$a" == "$b" ]] && continue
            ip_b="$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$(cid_of "browser-$b")")"
            out="$(docker exec "$(cid_of "browser-$a")" timeout 5 bash -c "exec 3<>/dev/tcp/$ip_b/3000" 2>&1)" \
                && bad "$a cannot reach browser-$b ($ip_b:3000)" "connected" \
                || ok "$a cannot reach browser-$b ($ip_b:3000)"
            check "$a cannot resolve browser-$b" not docker exec "$(cid_of "browser-$a")" getent hosts "browser-$b"
        done
    done
fi

echo "== idempotence"
before="$(fingerprint)"
if run_sync; then ok "sync runs"; else bad "sync runs" "$(cat "$sync_out")"; fi
check "second sync reports no file changes" grep -q 'generated files unchanged' "$sync_out"
check "second sync does not reload nginx" not grep -q 'nginx reloaded' "$sync_out"
check "nothing recreated, nginx untouched" test "$(fingerprint)" == "$before"

if [[ "$BACKEND" != file ]]; then
    echo
    echo "AUTH_BACKEND=$BACKEND: test removal and re-adding by hand (README, 'Provisioning')."
else
    users_file="$(env_get AUTHELIA_USERS_FILE ./demo/users.yml path)"
    group="$(env_get FREEIPA_BROWSER_GROUP remote-browser-users)"
    users_tmp="$(mktemp --suffix=.yml)"
    # Every change below goes through a copy of the users file; the demo's own file is never touched.
    with_users() { AUTHELIA_USERS_FILE="$users_tmp" run_sync "$@"; }

    echo "== fail-safe (nothing may change)"
    before="$(fingerprint)"
    AUTHELIA_USERS_FILE=/nonexistent/users.yml run_sync
    check "unreadable user source aborts" grep -q '^ABORT' "$sync_out"
    check "... and changes nothing" test "$(fingerprint)" == "$before"

    python3 - "$users_file" "$users_tmp" "$group" <<'PY'
import sys, yaml
data = yaml.safe_load(open(sys.argv[1]))
for entry in data["users"].values():
    entry["groups"] = [g for g in entry.get("groups") or [] if g != sys.argv[3]]
yaml.safe_dump(data, open(sys.argv[2], "w"))
PY
    with_users
    check "empty group is refused without --force" grep -q 'refusing without --force' "$sync_out"
    check "... and changes nothing" test "$(fingerprint)" == "$before"

    victim="${users[-1]}"
    keep=("${users[@]:0:${#users[@]}-1}")
    if (( ${#keep[@]} == 0 )); then
        echo "SKIP  removal test needs two provisioned users"
    else
        echo "== removing $victim from the group"
        victim_cid="$(cid_of "browser-$victim")"
        subnet="$(state_of "$victim" subnet)"
        docker exec -u "$(state_of "$victim" uid_number):$(state_of "$victim" gid_number)" "$victim_cid" \
            sh -c 'echo rb-sync-test > /config/rb-sync-marker'
        keep_ids="$(for u in "${keep[@]}"; do cid_of "browser-$u"; done)"
        nginx_started="$(docker inspect --format '{{.State.StartedAt}}' "$nginx")"
        python3 - "$users_file" "$users_tmp" "$group" "$victim" <<'PY'
import sys, yaml
data = yaml.safe_load(open(sys.argv[1]))
entry = data["users"][sys.argv[4]]
entry["groups"] = [g for g in entry.get("groups") or [] if g != sys.argv[3]]
yaml.safe_dump(data, open(sys.argv[2], "w"))
PY
        if with_users; then ok "sync runs"; else bad "sync runs" "$(cat "$sync_out")"; fi
        check "browser-$victim stopped and removed" test -z "$(docker ps -aq --filter "id=$victim_cid")"
        check "net-$victim removed" test -z "$(net_of "$victim")"
        check "$victim removed from the Nginx map" not grep -q "\"$victim\"" "$MAP_FILE"
        check "profile of $victim retained" test -f "$USER_DATA_ROOT/$victim/config/rb-sync-marker"
        check "other browsers untouched" test "$(for u in "${keep[@]}"; do cid_of "browser-$u"; done)" == "$keep_ids"
        check "nginx not restarted" test "$(docker inspect --format '{{.State.StartedAt}}' "$nginx")" == "$nginx_started"

        echo "== adding $victim back"
        if run_sync; then ok "sync runs"; else bad "sync runs" "$(cat "$sync_out")"; fi
        cid="$(cid_of "browser-$victim")"
        check "browser-$victim running again" test -n "$cid"
        check "same subnet as before ($subnet)" test "$(state_of "$victim" subnet)" == "$subnet"
        check "nginx attached to net-$victim" grep -q "$nginx" <<<"$(docker network inspect --format '{{range $id, $_ := .Containers}}{{$id}} {{end}}' "$(net_of "$victim")")"
        check "$victim back in the Nginx map" grep -q "^\"$victim\" \"browser-$victim:3000\";$" "$MAP_FILE"
        check "profile content still there" docker exec "$cid" grep -q rb-sync-test /config/rb-sync-marker
        docker exec "$cid" rm -f /config/rb-sync-marker
        check "nginx not restarted" test "$(docker inspect --format '{{.State.StartedAt}}' "$nginx")" == "$nginx_started"
    fi
fi

echo
echo "$pass passed, $fail failed"
(( fail == 0 ))
