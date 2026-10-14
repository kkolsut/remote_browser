#!/usr/bin/env bash
# Local demo of the stack without FreeIPA, in the production topology: an Apache in front (running
# apache/remote-browser.conf), the browser VM's Nginx, a demo Authelia with dummy users from
# demo/users.yml (password "demo"). https://browser.localhost:8443/, self-signed TLS, data in
# local/demo/. alice gets a Chrome/Firefox browser container (Phase 2), built on first use.
# No root needed (except the optional egress filter).
#
# Usage: ./scripts/demo.sh up        create secrets + certificate, build the image if missing, start
#        ./scripts/demo.sh test      run tests/phase1-auth.sh, routing.sh and phase2-browser.sh against it
#        ./scripts/demo.sh build     rebuild the browser image (local/remote-dual-browser:demo)
#        ./scripts/demo.sh down      stop it (rm -rf local/demo to start from scratch)
#        ./scripts/demo.sh <args>    any other docker compose command (logs -f, ps, ...)
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
env_file="$repo_dir/demo/demo.env"
project=remote-browser-demo
env_get() { grep -E "^$1=" "$env_file" | tail -n1 | cut -d= -f2-; }

# alice's browser runs as you, so everything under local/demo stays yours.
export BROWSER_TEST_UID="${BROWSER_TEST_UID:-$(id -u)}" BROWSER_TEST_GID="${BROWSER_TEST_GID:-$(id -g)}"

dc() {
    docker compose --project-directory "$repo_dir" --env-file "$env_file" -p "$project" \
        -f "$repo_dir/compose.yaml" -f "$repo_dir/compose.browser-test.yaml" -f "$repo_dir/compose.demo.yaml" "$@"
}

build_image() {
    echo "Building $(env_get BROWSER_IMAGE) (Chrome + Firefox, several minutes the first time)..."
    docker build -t "$(env_get BROWSER_IMAGE)" "$repo_dir/browser-image"
}

case "${1:-}" in
    up)
        shift
        ENV_FILE="$env_file" "$repo_dir/scripts/init-secrets.sh" --self-signed-tls >/dev/null
        user="$(env_get BROWSER_TEST_USER)"
        user_root="$repo_dir/$(env_get USER_DATA_ROOT)"
        map_dir="$repo_dir/$(env_get NGINX_GENERATED_DIR)"
        install -d -m 0700 "$user_root/$user" "$user_root/$user/config"
        install -d -m 0755 "$map_dir"
        # Phase 2: one hand-written map entry. Phase 3 generates this file from FreeIPA.
        printf '"%s" "browser-%s:3000";\n' "$user" "$user" > "$map_dir/users.map"
        docker image inspect "$(env_get BROWSER_IMAGE)" >/dev/null 2>&1 || build_image
        dc up -d --remove-orphans "$@"
        # Restart, not reload: templates are rendered when the container starts.
        dc restart nginx >/dev/null
        port="$(env_get PUBLIC_PORT)"
        cat <<MSG

Open https://$(env_get PUBLIC_HOST):$port/ (accept the self-signed certificate for both
browser.localhost and auth.browser.localhost). Password for every user: demo
  alice  in the access group   -> her own browser session: choose Chrome or Firefox
  bob    not in the group      -> 403
  carol  disabled              -> login fails
  dave   in the group, no browser provisioned -> "No browser yet" page
Identity seen by backends: https://$(env_get PUBLIC_HOST):$port/whoami
Logs: ./scripts/demo.sh logs -f    Stop: ./scripts/demo.sh down

Optional, needs root: block alice's browser from your LAN and this host (plan §30a):
  sudo ENV_FILE=demo/demo.env ./scripts/egress-firewall.sh apply
  (undo: ... egress-firewall.sh remove)
MSG
        ;;
    build) build_image ;;
    test)
        rc=0
        PUBLIC_HOST="$(env_get PUBLIC_HOST)" AUTHELIA_HOST="$(env_get AUTHELIA_HOST)" \
        PUBLIC_PORT="$(env_get PUBLIC_PORT)" TARGET_IP=127.0.0.1 INSECURE=1 \
        TEST_USER=alice TEST_PASS=demo OUTSIDER_USER=bob OUTSIDER_PASS=demo \
        DISABLED_USER=carol DISABLED_PASS=demo \
            "$repo_dir/tests/phase1-auth.sh" || rc=1
        echo
        PUBLIC_HOST="$(env_get PUBLIC_HOST)" AUTHELIA_HOST="$(env_get AUTHELIA_HOST)" \
        PUBLIC_PORT="$(env_get PUBLIC_PORT)" TARGET_IP=127.0.0.1 INSECURE=1 \
        BACKEND_ADDR="127.0.0.1:$(env_get HTTPS_PORT)" \
        BROWSER_USER=alice BROWSER_PASS=demo UNPROVISIONED_USER=dave UNPROVISIONED_PASS=demo \
            "$repo_dir/tests/routing.sh" || rc=1
        echo
        PROJECT="$project" "$repo_dir/tests/phase2-browser.sh" || rc=1
        exit $rc
        ;;
    ""|-h|--help) sed -n '2,13p' "$0" ;;
    *) dc "$@" ;;
esac
