#!/usr/bin/env bash
# Local demo of the stack without FreeIPA, in the production topology: an Apache in front (running
# apache/remote-browser.conf), the browser VM's Nginx, a demo Authelia with dummy users from
# demo/users.yml (password "demo"). https://browser.localhost:8443/, self-signed TLS, data in
# local/demo/. scripts/sync-freeipa-users.py provisions the browsers, reading demo/users.yml instead
# of FreeIPA. The browser image is built on first use. No root needed (except the optional egress filter).
#
# Usage: ./scripts/demo.sh up        create secrets + certificate, build the image if missing, start, sync
#        ./scripts/demo.sh sync      provision browsers after editing demo/users.yml (sync options pass through)
#        ./scripts/demo.sh test      run all tests against it (phases 1-3, backup/restore)
#        ./scripts/demo.sh build     rebuild the browser image (local/remote-dual-browser:demo)
#        ./scripts/demo.sh down      stop it (rm -rf local/demo to start from scratch)
#        ./scripts/demo.sh <args>    any other docker compose command (logs -f, ps, ...)
#        DEMO_ENGINE=podman ./scripts/demo.sh ...   the same on rootful Podman instead of Docker
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
env_file="$repo_dir/demo/demo.env"
env_get() { grep -E "^$1=" "$env_file" | tail -n1 | cut -d= -f2-; }
engine="${DEMO_ENGINE:-docker}"

# DEMO_ENGINE=podman: the same demo on rootful Podman (scripts/podman-setup.sh). The docker CLI talks
# to Podman's socket; the env file is demo/demo.env plus compose.podman.yaml in COMPOSE_FILE.
# Don't run both engines' demos at once: same ports, same subnets.
if [[ "$engine" == podman ]]; then
    export DOCKER_HOST="unix:///run/podman/podman.sock"
    mkdir -p "$repo_dir/local/demo"
    podman_env="$repo_dir/local/demo/demo-podman.env"
    { sed 's|^COMPOSE_FILE=compose.yaml:|COMPOSE_FILE=compose.yaml:compose.podman.yaml:|' "$env_file"
      echo "CONTAINER_ENGINE=podman"; } > "$podman_env"
    env_file="$podman_env"
elif [[ "$engine" != docker ]]; then
    echo "DEMO_ENGINE must be docker or podman" >&2; exit 2
fi
project="$(env_get COMPOSE_PROJECT_NAME)"

# Every demo browser runs as you, so everything under local/demo stays yours.
export DEMO_UID="${DEMO_UID:-$(id -u)}" DEMO_GID="${DEMO_GID:-$(id -g)}"

# Project name and files (COMPOSE_FILE, including the generated browsers) come from demo/demo.env.
dc() {
    [[ -f "$repo_dir/$(env_get GENERATED_DIR)/compose.users.yaml" ]] || { echo "run ./scripts/demo.sh up first" >&2; exit 1; }
    (cd "$repo_dir" && unset COMPOSE_FILE COMPOSE_PROJECT_NAME && docker compose --env-file "$env_file" "$@")
}
# Exit status 2 = some users skipped on purpose (eve.smith); only 1 is a failure.
provision() { "$repo_dir/scripts/sync-freeipa-users.py" --env-file "$env_file" "$@" || (( $? == 2 )); }

build_image() {
    echo "Building $(env_get BROWSER_IMAGE) (Chrome + Firefox, several minutes the first time)..."
    docker build -t "$(env_get BROWSER_IMAGE)" "$repo_dir/browser-image"
}

case "${1:-}" in
    up)
        shift
        ENV_FILE="$env_file" "$repo_dir/scripts/init-secrets.sh" --self-signed-tls >/dev/null
        if [[ "$engine" == podman ]]; then
            ENV_FILE="$env_file" "$repo_dir/scripts/podman-setup.sh"
            # Reuse an image Docker already built instead of building ~5 GB again.
            if ! docker image inspect "$(env_get BROWSER_IMAGE)" >/dev/null 2>&1 \
                    && DOCKER_HOST=unix:///var/run/docker.sock docker image inspect "$(env_get BROWSER_IMAGE)" >/dev/null 2>&1; then
                echo "Copying $(env_get BROWSER_IMAGE) from Docker to Podman..."
                DOCKER_HOST=unix:///var/run/docker.sock docker save "$(env_get BROWSER_IMAGE)" | docker load
            fi
        fi
        docker image inspect "$(env_get BROWSER_IMAGE)" >/dev/null 2>&1 || build_image
        # The generated Compose file first (docker compose needs it), then the infrastructure, then
        # the browsers: the sync creates the profile directories and attaches Nginx to their networks.
        provision --generate-only >/dev/null
        dc up -d --remove-orphans "$@" nginx apache authelia
        # Restart, not reload: templates are rendered when the container starts.
        dc restart nginx >/dev/null
        provision
        port="$(env_get PUBLIC_PORT)"
        cat <<MSG

Open https://$(env_get PUBLIC_HOST):$port/ (accept the self-signed certificate for both
browser.localhost and auth.browser.localhost). Password for every user: demo
  alice, dave  in the access group -> their own browser session: choose Chrome or Firefox
  bob          not in the group    -> 403
  carol        disabled            -> login fails
  eve.smith    in the group, but not a safe uid -> no browser provisioned ("No browser yet")
Identity seen by backends: https://$(env_get PUBLIC_HOST):$port/whoami
Logs: ./scripts/demo.sh logs -f    Stop: ./scripts/demo.sh down
Change users: edit demo/users.yml, ./scripts/demo.sh restart authelia, ./scripts/demo.sh sync

Optional, needs root: block the browsers from your LAN and this host (plan §30a):
  sudo ENV_FILE=demo/demo.env ./scripts/egress-firewall.sh apply
  (undo: ... egress-firewall.sh remove)
MSG
        ;;
    build) build_image ;;
    sync) shift; provision "$@" ;;
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
        BROWSER_USER=alice BROWSER_PASS=demo OTHER_USER=dave OTHER_PASS=demo PROJECT="$project" \
        UNPROVISIONED_USER=eve.smith UNPROVISIONED_PASS=demo \
            "$repo_dir/tests/routing.sh" || rc=1
        echo
        PROJECT="$project" SERVICE=browser-alice "$repo_dir/tests/phase2-browser.sh" || rc=1
        echo
        ENV_FILE="$env_file" "$repo_dir/tests/phase3-sync.sh" || rc=1
        echo
        ENV_FILE="$env_file" BROWSER_USER=alice "$repo_dir/tests/backup-restore.sh" || rc=1
        exit $rc
        ;;
    ""|-h|--help) sed -n '2,15p' "$0" ;;
    *) dc "$@" ;;
esac
