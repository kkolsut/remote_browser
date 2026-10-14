#!/usr/bin/env bash
# Validate the rendered Docker Compose and Nginx configuration in throwaway containers.
# (Authelia is the site's existing instance; validate its configuration there.)
# Exits non-zero on the first failure. Safe to run while the stack is up.
set -euo pipefail

cd "$(dirname "$0")/.."

compose_files=(-f compose.yaml)
# Phase 2: the single test user, when configured in .env.
grep -qE '^BROWSER_TEST_USER=.+' .env 2>/dev/null && compose_files+=(-f compose.browser-test.yaml)
[[ -s generated/compose.users.yaml ]] && compose_files+=(-f generated/compose.users.yaml)
dc() { docker compose "${compose_files[@]}" "$@"; }

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
