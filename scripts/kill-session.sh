#!/usr/bin/env bash
# End a user's open browser stream now (plan §17): restarts the user's browser container.
#
# Authelia only checks the session when the stream's WebSocket opens. Logging out, an expired session
# or removal from the group does not end a stream that is already open. The sync removes the
# container of a user who lost access at its next run (timer: every 5 minutes); this is for "now".
# A restart lets the user reconnect if Authelia still lets them in: to lock someone out, disable the
# account in FreeIPA (or remove it from the group) first, then run this.
#
# Usage: ./scripts/kill-session.sh <uid>
#        ENV_FILE=demo/demo.env ./scripts/kill-session.sh alice
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
env_file="${ENV_FILE:-$repo_dir/.env}"
uid="${1:-}"

[[ "$uid" =~ ^[a-z][a-z0-9-]{0,30}$ ]] || { sed -n '2,11p' "$0" >&2; exit 2; }
project="$(grep -E '^COMPOSE_PROJECT_NAME=' "$env_file" 2>/dev/null | tail -n1 | cut -d= -f2-)"
project="${project:-remote-browser}"

cid="$(docker ps -q --filter "label=com.docker.compose.project=$project" \
    --filter "label=com.docker.compose.service=browser-$uid")"
[[ -n "$cid" ]] || { echo "no running browser for $uid in project $project" >&2; exit 1; }
docker restart -t 5 "$cid" >/dev/null
echo "restarted browser-$uid: its open streams have ended"
