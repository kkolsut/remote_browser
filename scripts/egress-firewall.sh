#!/usr/bin/env bash
# Egress filter for the browser containers (plan §30a). Browser networks are allocated from
# BROWSER_NET_POOL; traffic from that pool may reach the public Internet only:
#
#   to the host itself (any port, any of its addresses)        REJECT   (INPUT, chain RB-INPUT)
#   to RFC1918 / CGNAT / link-local / loopback / multicast     REJECT   (DOCKER-USER, chain RB-EGRESS)
#   to the host's own addresses (rootless Docker: via slirp)   REJECT   (DOCKER-USER)
#   replies and Nginx <-> browser on the user's own network    allowed
#   everything else (public Internet)                          allowed
#
# REJECT (icmp-admin-prohibited) instead of DROP: blocked connections fail at once with
# "No route to host" instead of hanging, which is also what tests/phase2-browser.sh checks for.
# Name resolution keeps working: Docker's embedded DNS answers inside the container's namespace.
#
# Usage: sudo ./scripts/egress-firewall.sh apply|remove|status
#        ENV_FILE=demo/demo.env sudo -E ./scripts/egress-firewall.sh apply   (another env file)
#
# Rootful Docker: run as root, at boot after docker.service (systemd/remote-browser-egress.service).
# Rootless Docker: run as the Docker user; the rules go into rootlesskit's network namespace.
# IPv4 only: the browser networks are created without IPv6.
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
env_file="${ENV_FILE:-$repo_dir/.env}"
env_get() { grep -E "^$1=" "$env_file" 2>/dev/null | tail -n1 | cut -d= -f2- || true; }

pool="${BROWSER_NET_POOL:-$(env_get BROWSER_NET_POOL)}"
pool="${pool:-10.213.0.0/16}"
[[ "$pool" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || { echo "invalid BROWSER_NET_POOL: $pool" >&2; exit 2; }

blocked_nets=(
    10.0.0.0/8 172.16.0.0/12 192.168.0.0/16   # RFC1918: LAN, Proxmox, FreeIPA, other containers
    100.64.0.0/10                             # CGNAT / Tailscale
    169.254.0.0/16                            # link-local, cloud metadata
    127.0.0.0/8 0.0.0.0/8                     # loopback, "this network"
    224.0.0.0/4 240.0.0.0/4                   # multicast, reserved
)

ns=()
if [[ "$(docker info --format '{{.SecurityOptions}}' 2>/dev/null)" == *rootless* ]]; then
    child_pid_file="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/dockerd-rootless/child_pid"
    [[ -r "$child_pid_file" ]] || { echo "rootless Docker: cannot read $child_pid_file" >&2; exit 1; }
    ns=(nsenter -U --preserve-credentials -n -t "$(cat "$child_pid_file")")
elif (( EUID != 0 )); then
    echo "run as root (rootful Docker)" >&2
    exit 1
fi
ipt() { "${ns[@]}" iptables -w "$@"; }

# All IPv4 addresses of the host (outside rootlesskit's namespace).
host_addrs() { ip -4 -o addr show | awk '{ split($4, a, "/"); print a[1] }' | sort -u; }

remove() {
    while ipt -C DOCKER-USER -s "$pool" -j RB-EGRESS 2>/dev/null; do ipt -D DOCKER-USER -s "$pool" -j RB-EGRESS; done
    while ipt -C INPUT -s "$pool" -j RB-INPUT 2>/dev/null; do ipt -D INPUT -s "$pool" -j RB-INPUT; done
    for chain in RB-EGRESS RB-INPUT; do
        if ipt -L "$chain" -n >/dev/null 2>&1; then ipt -F "$chain"; ipt -X "$chain"; fi
    done
}

apply() {
    ipt -L DOCKER-USER -n >/dev/null 2>&1 || { echo "chain DOCKER-USER missing: start Docker first" >&2; exit 1; }
    local reject=(-j REJECT --reject-with icmp-admin-prohibited) addr net

    # Build the chains, then hook them in, so there is never a window with a half-built chain.
    for chain in RB-EGRESS RB-INPUT; do ipt -N "$chain" 2>/dev/null || ipt -F "$chain"; done

    ipt -A RB-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    # Nginx <-> browser inside one user's network. Separate user networks are separate bridges,
    # which Docker isolates from each other; tests/phase2-browser.sh checks it.
    ipt -A RB-EGRESS -d "$pool" -j RETURN
    while read -r addr; do ipt -A RB-EGRESS -d "$addr" "${reject[@]}"; done < <(host_addrs)
    for net in "${blocked_nets[@]}"; do ipt -A RB-EGRESS -d "$net" "${reject[@]}"; done
    ipt -A RB-EGRESS -j RETURN

    ipt -A RB-INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    ipt -A RB-INPUT "${reject[@]}"

    ipt -C DOCKER-USER -s "$pool" -j RB-EGRESS 2>/dev/null || ipt -I DOCKER-USER 1 -s "$pool" -j RB-EGRESS
    ipt -C INPUT -s "$pool" -j RB-INPUT 2>/dev/null || ipt -I INPUT 1 -s "$pool" -j RB-INPUT
    echo "egress filter active for $pool"
}

case "${1:-}" in
    apply) apply ;;
    remove) remove; echo "egress filter removed for $pool" ;;
    status)
        ipt -S RB-EGRESS 2>/dev/null && ipt -S RB-INPUT 2>/dev/null \
            && ipt -C DOCKER-USER -s "$pool" -j RB-EGRESS 2>/dev/null && ipt -C INPUT -s "$pool" -j RB-INPUT 2>/dev/null \
            && echo "egress filter active for $pool" || { echo "egress filter NOT active for $pool"; exit 1; } ;;
    *) sed -n '2,20p' "$0"; exit 2 ;;
esac
