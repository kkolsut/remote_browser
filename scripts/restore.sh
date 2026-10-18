#!/usr/bin/env bash
# Restore from a backup of scripts/backup.sh (plan §47). Verifies the checksums first.
#
#   --config        restore .env, secrets, TLS, CA certificates and generated state into this checkout
#                   (on a clean VM: after git clone, before anything else). Refuses to overwrite an
#                   existing .env unless --force.
#   --users [uid..] restore profiles (all in the backup, or the ones named). The user's browser is
#                   stopped meanwhile; an existing profile directory is moved aside to
#                   <uid>.before-restore-<time>, never deleted. Ownership comes from the backup.
#
# Run as root. Afterwards run the sync: it recreates the browsers of eligible users.
#
# Usage: sudo ./scripts/restore.sh <backup dir> --config [--force]
#        sudo ./scripts/restore.sh <backup dir> --users [uid ...]
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
env_file="$(realpath -m "${ENV_FILE:-$repo_dir/.env}")"
usage() { sed -n '2,14p' "$0" >&2; exit 2; }

backup="${1:-}"; shift || true
[[ -n "$backup" && -f "$backup/SHA256SUMS" ]] || { echo "not a backup directory: ${backup:-?}" >&2; usage; }
mode="" force=0 uids=()
while (( $# )); do
    case "$1" in
        --config) mode=config ;;
        --users) mode=users ;;
        --force) force=1 ;;
        -*) usage ;;
        *) [[ "$mode" == users ]] || usage; uids+=("$1") ;;
    esac
    shift
done
[[ -n "$mode" ]] || usage

echo "== verifying $backup"
(cd "$backup" && sha256sum --quiet -c SHA256SUMS) || { echo "checksum mismatch: backup damaged" >&2; exit 1; }
sed 's/^/  /' "$backup/MANIFEST"

if [[ "$mode" == config ]]; then
    if [[ -e "$env_file" && $force == 0 ]]; then
        echo "$env_file exists; --force to overwrite the configuration" >&2; exit 1
    fi
    tar -C "$repo_dir" -xzf "$backup/config.tar.gz" --numeric-owner --same-permissions
    [[ -f "$backup/config-abs.tar.gz" ]] && tar -C / -xzf "$backup/config-abs.tar.gz" --numeric-owner --same-permissions
    echo "configuration restored into $repo_dir. Next: ./scripts/install-units.py, then the profiles"
    exit 0
fi

env_get() {
    local v; v="$(grep -E "^$1=" "$env_file" | tail -n1 | cut -d= -f2- || true)"; v="${v:-$2}"
    [[ "${3:-}" == path && "$v" != /* ]] && v="$repo_dir/${v#./}"
    echo "$v"
}
[[ -f "$env_file" ]] || { echo "missing $env_file: restore --config first" >&2; exit 1; }
user_root="$(env_get USER_DATA_ROOT ./local/users path)"
mkdir -p "$user_root"

if (( ${#uids[@]} == 0 )); then
    for f in "$backup"/users/*.tar.gz; do [[ -e "$f" ]] && uids+=("$(basename "$f" .tar.gz)"); done
fi
stamp="$(date +%Y%m%d-%H%M%S)"
for uid in "${uids[@]}"; do
    [[ "$uid" =~ ^[a-z][a-z0-9-]{0,30}$ ]] || { echo "invalid uid: $uid" >&2; exit 2; }
    archive="$backup/users/$uid.tar.gz"
    [[ -f "$archive" ]] || { echo "no profile of $uid in this backup" >&2; exit 1; }
    was_running=0
    if systemctl is-active --quiet "browser-$uid.service"; then
        systemctl stop "browser-$uid.service" && was_running=1 && echo "  stopped browser-$uid"
    fi
    if [[ -e "$user_root/$uid" ]]; then
        mv "$user_root/$uid" "$user_root/$uid.before-restore-$stamp"
        echo "  $uid: current profile moved to $uid.before-restore-$stamp"
    fi
    tar -C "$user_root" -xzf "$archive" --numeric-owner --same-permissions
    echo "  $uid: restored"
    (( was_running )) && systemctl start "browser-$uid.service" && echo "  started browser-$uid"
done
echo "done. Run the sync to (re)create browsers of eligible users."
