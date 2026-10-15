#!/usr/bin/env bash
# File-level backup (plan §46): what is needed to rebuild this VM from Git plus this backup.
#
#   BACKUP_DIR/remote-browser-<YYYYmmdd-HHMMSS>/
#     config.tar.gz      .env, secrets, TLS, CA certificates, generated state (keeps each user's subnet)
#     users/<uid>.tar.gz one per profile directory in USER_DATA_ROOT (also users without a browser
#                        now), without regenerable browser caches
#     SHA256SUMS, MANIFEST
#
# Each user's browser is paused (docker pause) while their profile is archived, so the browsers'
# databases are consistent: an open stream freezes for that time, it does not end. --no-pause skips
# that (e.g. when Proxmox snapshots are the primary backup). The backup holds secrets and every
# profile: it is written root-only (0700/0600). Copy it off the VM; PBS snapshots of the VM come first.
# Keeps the newest BACKUP_KEEP (default 7) backups. Run as root (remote-browser-backup.timer).
#
# Usage: ./scripts/backup.sh [--no-pause]
#        ENV_FILE=demo/demo.env ./scripts/backup.sh
# Restore: scripts/restore.sh <backup directory>
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
env_file="$(realpath "${ENV_FILE:-$repo_dir/.env}")"
pause=1
for arg in "$@"; do
    case "$arg" in
        --no-pause) pause=0 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done
[[ -f "$env_file" ]] || { echo "missing $env_file" >&2; exit 1; }

env_get() {
    local v; v="$(grep -E "^$1=" "$env_file" | tail -n1 | cut -d= -f2- || true)"; v="${v:-$2}"
    [[ "${3:-}" == path && "$v" != /* ]] && v="$repo_dir/${v#./}"
    echo "$v"
}
project="$(env_get COMPOSE_PROJECT_NAME remote-browser)"
user_root="$(env_get USER_DATA_ROOT ./local/users path)"
backup_root="$(env_get BACKUP_DIR ./local/backups path)"
keep="$(env_get BACKUP_KEEP 7)"
[[ "$keep" =~ ^[1-9][0-9]*$ ]] || { echo "BACKUP_KEEP must be a number >= 1" >&2; exit 1; }

# Paths of the configuration archive, relative to the repository when they are inside it.
config_paths=("$env_file")
for key in SECRETS_DIR:./local/secrets TLS_DIR:./local/tls FREEIPA_CA_DIR:./local/freeipa-ca \
           AUTHELIA_CA_DIR:./local/authelia-ca GENERATED_DIR:./generated NGINX_GENERATED_DIR:./nginx/generated \
           AUTHELIA_DATA_DIR:; do
    value="$(env_get "${key%%:*}" "${key#*:}")"
    [[ -z "$value" ]] && continue
    [[ "$value" == /* ]] || value="$repo_dir/${value#./}"
    [[ -e "$value" ]] || continue
    # Only the demo's bundled Authelia data belongs to a container's root; without root, skip it loudly.
    if [[ -n "$(find "$value" ! -readable -print -quit 2>/dev/null)" ]]; then
        echo "WARNING: $value is not fully readable (run as root); not in this backup" >&2
        continue
    fi
    config_paths+=("$value")
done

# Regenerable caches (plan §46).
excludes=(
    --exclude='config/.cache'
    --exclude='config/.config/google-chrome/*/Cache'
    --exclude='config/.config/google-chrome/*/Code Cache'
    --exclude='config/.config/google-chrome/*/GPUCache'
    --exclude='config/.config/google-chrome/*/Service Worker/CacheStorage'
    --exclude='config/.config/google-chrome/*/Service Worker/ScriptCache'
    --exclude='config/.config/google-chrome/Singleton*'
    --exclude='config/.mozilla/firefox/*/cache2'
    --exclude='config/.config/mozilla/firefox/*/cache2'
    --exclude='config/.mozilla/firefox/*/lock'
    --exclude='config/.config/mozilla/firefox/*/lock'
)

umask 077
name="remote-browser-$(date +%Y%m%d-%H%M%S)"
partial="$backup_root/.$name.partial"
mkdir -p "$backup_root" "$partial/users"
chmod 0700 "$backup_root"
trap 'rm -rf "$partial"' EXIT

echo "== configuration"
# Absolute paths stored without the leading /, restored relative to /. Paths inside the repository
# are stored relative to it, so a backup restores into a checkout anywhere.
rel=() abs=()
for p in "${config_paths[@]}"; do
    if [[ "$p" == "$repo_dir"/* ]]; then rel+=("${p#"$repo_dir"/}"); else abs+=("$p"); fi
done
tar -C "$repo_dir" -czf "$partial/config.tar.gz" --numeric-owner --exclude='*.prev' --exclude='.sync.lock' "${rel[@]}"
(( ${#abs[@]} )) && tar -C / -czf "$partial/config-abs.tar.gz" --numeric-owner "${abs[@]#/}"
printf '  %s\n' "${rel[@]}" "${abs[@]}"

echo "== profiles ($user_root)"
paused=""
resume() { [[ -n "$paused" ]] && docker unpause "$paused" >/dev/null 2>&1; paused=""; }
trap 'resume; rm -rf "$partial"' EXIT
failed=0
for dir in "$user_root"/*/; do
    [[ -d "$dir" ]] || continue
    uid="$(basename "$dir")"
    [[ "$uid" =~ ^[a-z][a-z0-9-]{0,30}$ ]] || { echo "  skip $dir (not a uid)"; continue; }
    cid=""
    if (( pause )); then
        cid="$(docker ps -q --filter "label=com.docker.compose.project=$project" \
            --filter "label=com.docker.compose.service=browser-$uid" --filter status=running)"
        [[ -n "$cid" ]] && docker pause "$cid" >/dev/null && paused="$cid"
    fi
    start=$SECONDS
    if tar -C "$user_root" -czf "$partial/users/$uid.tar.gz" --numeric-owner "${excludes[@]}" "$uid"; then
        echo "  $uid: $(du -h "$partial/users/$uid.tar.gz" | cut -f1)${cid:+, paused $((SECONDS - start))s}"
    else
        echo "  $uid: FAILED" >&2; failed=1
    fi
    resume
done

{
    echo "created=$(date -Iseconds)"
    echo "host=$(uname -n)"
    echo "repo=$repo_dir"
    echo "commit=$(git -C "$repo_dir" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    echo "browser_image=$(env_get BROWSER_IMAGE)"
    echo "user_data_root=$user_root"
} > "$partial/MANIFEST"
(cd "$partial" && find . -type f ! -name SHA256SUMS -printf '%P\n' | sort | xargs -d '\n' sha256sum > SHA256SUMS)
(( failed )) && { echo "backup incomplete, not kept" >&2; exit 1; }

mv "$partial" "$backup_root/$name"
trap - EXIT
echo "== written $backup_root/$name ($(du -sh "$backup_root/$name" | cut -f1))"

find "$backup_root" -maxdepth 1 -type d -name 'remote-browser-*' -printf '%f\n' | sort -r | tail -n +$((keep + 1)) \
    | while read -r old; do rm -rf "${backup_root:?}/$old" && echo "removed old backup $old"; done
