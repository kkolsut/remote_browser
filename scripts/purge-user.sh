#!/usr/bin/env bash
# Delete a user's profile for good (plan §17): the explicit administrative step the sync never takes.
# Only for users who no longer have a browser (removed from the group, and the sync has run): refuses
# while the user is provisioned or a container of theirs exists. Asks to type the uid back unless --yes.
# Backups made earlier still contain the profile (scripts/backup.sh); delete those separately if the
# data must be gone everywhere.
#
# Usage: sudo ./scripts/purge-user.sh <uid> [--yes]
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
env_file="${ENV_FILE:-$repo_dir/.env}"
uid="${1:-}" yes="${2:-}"
[[ "$uid" =~ ^[a-z][a-z0-9-]{0,30}$ && ( -z "$yes" || "$yes" == --yes ) ]] || { sed -n '2,8p' "$0" >&2; exit 2; }

env_get() {
    local v; v="$(grep -E "^$1=" "$env_file" 2>/dev/null | tail -n1 | cut -d= -f2- || true)"; v="${v:-$2}"
    [[ "${3:-}" == path && "$v" != /* ]] && v="$repo_dir/${v#./}"
    echo "$v"
}
user_root="$(env_get USER_DATA_ROOT ./local/users path)"
state="$(env_get GENERATED_DIR ./generated path)/state.json"
dir="$user_root/$uid"

[[ -d "$dir" && ! -L "$dir" ]] || { echo "no profile directory $dir" >&2; exit 1; }
if [[ -f "$state" ]] && python3 -c 'import json,sys; sys.exit(sys.argv[2] not in json.load(open(sys.argv[1]))["users"])' "$state" "$uid"; then
    echo "$uid is still provisioned: remove them from the group (or disable them) and run the sync first" >&2
    exit 1
fi
if podman container exists "browser-$uid" || systemctl is-active --quiet "browser-$uid.service"; then
    echo "a container browser-$uid still exists: run the sync first" >&2
    exit 1
fi

echo "Deleting $dir ($(du -sh "$dir" | cut -f1)): Chrome and Firefox profiles, saved passwords, downloads."
if [[ "$yes" != --yes ]]; then
    read -r -p "Type the uid to confirm: " answer
    [[ "$answer" == "$uid" ]] || { echo "not confirmed, nothing deleted"; exit 1; }
fi
rm -rf --one-file-system "$dir"
echo "deleted $dir"
