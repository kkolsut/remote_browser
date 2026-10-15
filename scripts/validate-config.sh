#!/usr/bin/env bash
# Validate the rendered Docker Compose and Nginx configuration (including the generated user map) in
# throwaway containers.
# (Authelia is the site's existing instance; validate its configuration there.)
# Exits non-zero on the first failure. Safe to run while the stack is up.
#
# Usage: ./scripts/validate-config.sh        ENV_FILE=demo/demo.env ./scripts/validate-config.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# The files docker compose uses: COMPOSE_FILE from .env (compose.yaml + the generated browsers),
# otherwise compose.yaml alone.
env_file="${ENV_FILE:-.env}"
files="$(grep -E '^COMPOSE_FILE=' "$env_file" 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
compose_files=()
IFS=: read -ra listed <<<"${files:-compose.yaml}"
for f in "${listed[@]}"; do
    if [[ -f "$f" ]]; then
        compose_files+=(-f "$f")
    elif [[ "$(basename "$f")" == compose.users.yaml ]]; then
        echo "note: $f not generated yet (first sync), validating without browsers"
    else
        echo "$f (COMPOSE_FILE) is missing" >&2; exit 1
    fi
done
dc() { docker compose --env-file "$env_file" "${compose_files[@]}" "$@"; }

echo "== docker compose config (${compose_files[*]})"
dc config --quiet

echo "== seccomp profile"
python3 -c 'import json, sys; p = json.load(open(sys.argv[1])); assert p["defaultAction"] == "SCMP_ACT_ERRNO"' \
    browser-image/seccomp-chrome.json

echo "== nginx -t"
# The image entrypoint renders templates first because the command starts with "nginx".
# Upstreams are referenced through variables, so this needs neither Authelia nor the browsers.
if ! out="$(dc run --rm --no-deps -T nginx nginx -t 2>&1)"; then
    echo "$out" >&2
    exit 1
fi
echo "$out" | grep -E 'nginx: ' || true

echo "OK"
