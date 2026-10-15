#!/usr/bin/env bash
# One-time (idempotent) host setup for running on rootful Podman instead of Docker. The project keeps
# using the docker CLI and docker compose, talking to Podman's Docker-compatible API socket.
#
#   - checks: rootful podman.socket, docker CLI + compose plugin, nft;
#   - points the docker CLI at Podman: /run/docker.sock -> /run/podman/podman.sock (tmpfiles.d), unless
#     a real Docker is installed;
#   - creates the "frontend" network with route metric 50, so Nginx's default route stays there when
#     it joins the user networks (compose.podman.yaml explains why); FRONTEND_SUBNET pins its subnet;
#   - enables podman-restart.service: Podman starts "restart: unless-stopped" containers at boot only
#     through it.
# Seccomp needs nothing: Podman's default profile already allows the browser sandboxes.
#
# Usage: sudo ./scripts/podman-setup.sh             ENV_FILE=demo/demo.env ./scripts/podman-setup.sh
# Without root it only creates the network (needs access to the socket) and reports the rest.
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
env_file="${ENV_FILE:-$repo_dir/.env}"
env_get() { grep -E "^$1=" "$env_file" 2>/dev/null | tail -n1 | cut -d= -f2- || true; }
sock=/run/podman/podman.sock
podman_remote() { podman --remote --url "unix://$sock" "$@"; }
problems=0
note() { echo "  $1"; }
fail() { echo "  PROBLEM: $1"; problems=1; }

echo "== checks"
command -v podman >/dev/null || { echo "podman is not installed" >&2; exit 1; }
version="$(podman --version | awk '{print $3}')"
[[ "${version%%.*}" -ge 5 ]] && note "podman $version" || fail "podman $version: 5.0 or newer needed (network route metric)"
if [[ -S "$sock" ]]; then note "rootful API socket $sock"
else fail "no $sock: sudo systemctl enable --now podman.socket"; fi
command -v docker >/dev/null && note "docker CLI: $(docker --version)" \
    || fail "docker CLI missing: install docker-ce-cli and docker-compose-plugin (not docker-ce) from Docker's repository"
docker compose version >/dev/null 2>&1 && note "$(docker compose version)" || fail "docker compose plugin missing"
command -v nft >/dev/null && note "nftables $(nft --version | awk '{print $2}')" || fail "nft missing (scripts/egress-firewall.sh)"
command -v python3 >/dev/null || fail "python3 missing"

echo "== docker CLI -> Podman"
if (( EUID == 0 )); then
    if [[ -e /run/docker.sock && ! -L /run/docker.sock ]] || systemctl is-active --quiet docker.service 2>/dev/null; then
        note "a real Docker runs here too: not redirecting /run/docker.sock (use DOCKER_HOST=unix://$sock)"
    else
        echo "L+ /run/docker.sock - - - - $sock" > /etc/tmpfiles.d/remote-browser-podman.conf
        systemd-tmpfiles --create /etc/tmpfiles.d/remote-browser-podman.conf
        note "/run/docker.sock -> $sock (/etc/tmpfiles.d/remote-browser-podman.conf)"
    fi
    systemctl enable --now podman-restart.service >/dev/null 2>&1 && note "podman-restart.service enabled" \
        || fail "cannot enable podman-restart.service"
else
    note "not root: skipping /run/docker.sock and podman-restart.service (use DOCKER_HOST=unix://$sock)"
fi
server="$(docker version --format '{{.Server.Version}} {{range .Server.Components}}{{.Name}} {{end}}' 2>/dev/null || true)"
[[ "$server" == *Podman* ]] && note "docker CLI reaches Podman ($server)" \
    || fail "the docker CLI does not reach Podman (got: ${server:-nothing}); check DOCKER_HOST / /run/docker.sock"

echo "== frontend network"
project="$(env_get COMPOSE_PROJECT_NAME)"; project="${project:-remote-browser}"
net="${project}_frontend"
subnet="$(env_get FRONTEND_SUBNET)"
if podman_remote network exists "$net"; then
    metric="$(podman_remote network inspect "$net" --format '{{index .Options "metric"}}')"
    have="$(podman_remote network inspect "$net" --format '{{range .Subnets}}{{.Subnet}}{{end}}')"
    if [[ "$metric" != 50 ]]; then fail "$net exists without metric 50: remove it (stop Nginx first) and run this again"
    elif [[ -n "$subnet" && "$have" != "$subnet" ]]; then fail "$net has subnet $have, FRONTEND_SUBNET is $subnet"
    else note "$net exists (metric 50${have:+, $have})"; fi
else
    podman_remote network create --opt metric=50 ${subnet:+--subnet "$subnet"} \
        --label "com.docker.compose.project=$project" --label com.docker.compose.network=frontend "$net" >/dev/null
    note "created $net (metric 50${subnet:+, $subnet})"
fi

[[ "$(env_get CONTAINER_ENGINE)" == podman ]] && note "CONTAINER_ENGINE=podman" \
    || fail "set CONTAINER_ENGINE=podman in $env_file"
files="$(env_get COMPOSE_FILE)"
[[ ":$files:" == *":compose.podman.yaml:"* ]] && note "COMPOSE_FILE includes compose.podman.yaml" \
    || fail "add compose.podman.yaml to COMPOSE_FILE in $env_file (after compose.yaml)"

(( problems == 0 )) && echo "OK" || { echo "fix the problems above"; exit 1; }
