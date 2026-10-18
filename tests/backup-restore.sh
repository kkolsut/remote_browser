#!/usr/bin/env bash
# Backup and restore test (plan §46-47) against a running stack:
#   1. scripts/backup.sh: configuration + every profile, no browser caches, browsers resumed after;
#   2. a profile changed afterwards comes back exactly by scripts/restore.sh --users, the changed one
#      is kept aside, the browser runs again with its Chrome and Firefox profiles;
#   3. scripts/restore.sh --config recreates .env, secrets and generated state in a fresh checkout.
# Run as root, like the scripts.
#
# Environment:
#   ENV_FILE       env file of the stack (default: .env)
#   BROWSER_USER   user whose profile is restored (default: the first user in state.json)
set -uo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$(realpath "${ENV_FILE:-$repo_dir/.env}")"
env_get() {
    local v; v="$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2-)"; v="${v:-$2}"
    [[ "${3:-}" == path && "$v" != /* ]] && v="$repo_dir/${v#./}"
    echo "$v"
}
USER_DATA_ROOT="$(env_get USER_DATA_ROOT ./local/users path)"
BACKUP_DIR="$(env_get BACKUP_DIR ./local/backups path)"
STATE="$(env_get GENERATED_DIR ./generated path)/state.json"
BROWSER_USER="${BROWSER_USER:-$(python3 -c 'import json,sys; print(sorted(json.load(open(sys.argv[1]))["users"])[0])' "$STATE")}"

pass=0 fail=0
ok()  { echo "PASS  $1"; pass=$((pass + 1)); }
bad() { echo "FAIL  $1"; [[ -n "${2:-}" ]] && echo "      $2"; fail=$((fail + 1)); }
check() { local name="$1"; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }
not() { ! "$@"; }

cid="browser-$BROWSER_USER"
state() { podman container inspect --format '{{.State.Status}}' "$cid" 2>/dev/null; }
[[ "$(state)" == running ]] || { echo "$cid is not running" >&2; exit 1; }
puid="$(podman container inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$cid" | sed -n 's/^PUID=//p')"
pgid="$(podman container inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$cid" | sed -n 's/^PGID=//p')"
as_user() { podman exec -u "$puid:$pgid" "$1" sh -c "$2"; }
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "== backup"
as_user "$cid" 'rm -f /config/rb-after-backup; echo rb-backup-test > /config/rb-backup-marker; mkdir -p /config/.cache/rb-test && head -c 100000 /dev/urandom > /config/.cache/rb-test/blob'
before="$(find "$BACKUP_DIR" -maxdepth 1 -name 'remote-browser-*' 2>/dev/null | sort | tail -n1)"
if ENV_FILE="$ENV_FILE" "$repo_dir/scripts/backup.sh" > "$work/backup.log" 2>&1; then ok "backup.sh runs"
else bad "backup.sh runs" "$(tail -n 5 "$work/backup.log")"; fi
backup="$(find "$BACKUP_DIR" -maxdepth 1 -name 'remote-browser-*' | sort | tail -n1)"
if [[ -z "$backup" || "$backup" == "$before" ]]; then
    bad "a new backup directory"
    echo; echo "$pass passed, $fail failed (stopped: no backup)"; exit 1
fi
ok "a new backup directory"
check "backup readable only by its owner" test "$(stat -c %a "$backup")" == 700
check "checksums verify" sh -c "cd '$backup' && sha256sum --quiet -c SHA256SUMS"
config_list="$(tar -tzf "$backup/config.tar.gz")"
check "configuration has the env file" grep -qx "${ENV_FILE#"$repo_dir"/}" <<<"$config_list"
check "configuration has the secrets" grep -q "$(basename "$(env_get SECRETS_DIR ./local/secrets)")/" <<<"$config_list"
check "configuration has the generated state" grep -q 'state.json$' <<<"$config_list"
users_list="$(tar -tzf "$backup/users/$BROWSER_USER.tar.gz")"
check "profile archive has the marker" grep -qx "$BROWSER_USER/config/rb-backup-marker" <<<"$users_list"
check "profile archive has the Chrome profile" grep -q "^$BROWSER_USER/config/.config/google-chrome/" <<<"$users_list"
check "profile archive has no caches" not grep -q '/\.cache/\|/cache2/\|/Cache/' <<<"$users_list"
check "browser running again (not left paused)" test "$(state)" == running

echo "== restore a profile"
as_user "$cid" 'rm -f /config/rb-backup-marker; echo junk > /config/rb-after-backup'
if ENV_FILE="$ENV_FILE" "$repo_dir/scripts/restore.sh" "$backup" --users "$BROWSER_USER" > "$work/restore.log" 2>&1; then
    ok "restore.sh --users runs"
else bad "restore.sh --users runs" "$(tail -n 5 "$work/restore.log")"; fi
aside="$(find "$USER_DATA_ROOT" -maxdepth 1 -name "$BROWSER_USER.before-restore-*" | sort | tail -n1)"
check "the replaced profile is kept aside" test -f "$aside/config/rb-after-backup"
check "restored profile owned by the user, mode 0700" test "$(stat -c '%u %a' "$USER_DATA_ROOT/$BROWSER_USER")" == "$puid 700"
for _ in $(seq 30); do [[ "$(state)" == running ]] && break; sleep 1; done
check "browser running after the restore" test "$(state)" == running
check "marker back" as_user "$cid" 'grep -q rb-backup-test /config/rb-backup-marker'
check "later file gone" as_user "$cid" 'test ! -e /config/rb-after-backup'
check "Chrome and Firefox profiles back" as_user "$cid" \
    'test -d /config/.config/google-chrome/Default && ls -d /config/.mozilla/firefox/*/prefs.js /config/.config/mozilla/firefox/*/prefs.js 2>/dev/null | grep -q .'
chooser() { as_user "$cid" "DISPLAY=:1 xdotool search --onlyvisible --name '^Choose your browser\$'" >/dev/null 2>&1; }
for _ in $(seq 60); do chooser && break; sleep 1; done
check "session starts (chooser visible)" chooser
as_user "$cid" 'rm -f /config/rb-backup-marker /config/rb-after-backup; rm -rf /config/.cache/rb-test'
rm -rf "$aside"

echo "== restore the configuration into a fresh checkout"
fresh="$work/checkout"
mkdir -p "$fresh"
# The application files without any local state, as Ansible (or git clone) would put them on a new VM.
tar -C "$repo_dir" --exclude=./local --exclude=./.env --exclude='./generated/*' --exclude='./nginx/generated/*' \
    --exclude=__pycache__ -cf - . | tar -C "$fresh" -xf -
env_rel="${ENV_FILE#"$repo_dir"/}"
rm -f "$fresh/$env_rel"   # it must come from the backup
if ENV_FILE="$fresh/$env_rel" "$fresh/scripts/restore.sh" "$backup" --config > "$work/restore-config.log" 2>&1; then
    ok "restore.sh --config runs"
else bad "restore.sh --config runs" "$(tail -n 5 "$work/restore-config.log")"; fi
for path in "$env_rel" "$(tar -tzf "$backup/config.tar.gz" | grep -m1 'secrets/.\+')" \
            "$(tar -tzf "$backup/config.tar.gz" | grep -m1 'state.json$')"; do
    check "restored identically: $path" cmp -s "$repo_dir/$path" "$fresh/$path"
done
check "refuses to overwrite a configuration without --force" \
    not env ENV_FILE="$fresh/$env_rel" "$fresh/scripts/restore.sh" "$backup" --config

echo
echo "$pass passed, $fail failed"
(( fail == 0 ))
