#!/usr/bin/env bash
# Egress filter for the browser containers (plan §30a), for Docker and Podman alike: its own nftables
# table, independent of the engine's rules. Browser networks are /28s from BROWSER_NET_POOL; traffic
# from that pool may reach the public Internet only:
#
#   to the host itself (any port, any address), except DNS to its network's gateway   REJECT (input)
#   to another user's network (Podman does not isolate networks by default)          REJECT (forward)
#   to RFC1918 / CGNAT / link-local / loopback / multicast                            REJECT (forward)
#   replies; Nginx <-> browser within the user's own /28                              allowed
#   everything else (public Internet)                                                 allowed
#
# A reject in any nftables base chain is final, whatever Docker's or netavark's chains accept.
# REJECT (icmp admin-prohibited) instead of DROP: blocked connections fail at once with "No route to
# host" instead of hanging, which is what tests/phase2-browser.sh checks for.
# DNS: Docker's embedded resolver answers inside the container; Podman's aardvark-dns listens on each
# network's gateway, i.e. on the host, hence the one input exception.
#
# Usage: sudo ./scripts/egress-firewall.sh apply|remove|status
#        sudo ENV_FILE=demo/demo.env ./scripts/egress-firewall.sh apply   (another env file)
#
# Root, rootful Docker or Podman. At boot before the containers start: systemd/remote-browser-egress.service.
# If something flushes the whole ruleset (e.g. restarting Debian's nftables.service, whose
# /etc/nftables.conf starts with "flush ruleset"), apply again. IPv4 only: the browser networks have
# no IPv6.
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
env_file="${ENV_FILE:-$repo_dir/.env}"
env_get() { grep -E "^$1=" "$env_file" 2>/dev/null | tail -n1 | cut -d= -f2- || true; }

pool="${BROWSER_NET_POOL:-$(env_get BROWSER_NET_POOL)}"
pool="${pool:-10.213.0.0/16}"
[[ "$pool" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || { echo "invalid BROWSER_NET_POOL: $pool" >&2; exit 2; }
table="inet rb_egress"

blocked_nets=(
    10.0.0.0/8 172.16.0.0/12 192.168.0.0/16   # RFC1918: LAN, Proxmox, FreeIPA, other containers
    100.64.0.0/10                             # CGNAT / Tailscale
    169.254.0.0/16                            # link-local, cloud metadata
    127.0.0.0/8 0.0.0.0/8                     # loopback, "this network"
    224.0.0.0/4 240.0.0.0/4                   # multicast, reserved
)

(( EUID == 0 )) || { echo "run as root" >&2; exit 1; }
command -v nft >/dev/null || { echo "nft (nftables) is required" >&2; exit 1; }

# Every /28 of the pool paired with itself: "same user network". nftables cannot compare a packet's
# source and destination subnet, so the pairs are listed (4096 for a /16, generated once).
same_net_elements() {
    python3 - "$pool" <<'PY'
import ipaddress, sys
pool = ipaddress.ip_network(sys.argv[1])
if pool.prefixlen > 28:
    sys.exit("BROWSER_NET_POOL must be /28 or larger")
print(", ".join(f"{n} . {n}" for n in pool.subnets(new_prefix=28)))
PY
}

# The iptables rules of earlier versions of this script (Docker's DOCKER-USER chain).
remove_legacy() {
    command -v iptables >/dev/null || return 0
    while iptables -w -C DOCKER-USER -s "$pool" -j RB-EGRESS 2>/dev/null; do iptables -w -D DOCKER-USER -s "$pool" -j RB-EGRESS; done
    while iptables -w -C INPUT -s "$pool" -j RB-INPUT 2>/dev/null; do iptables -w -D INPUT -s "$pool" -j RB-INPUT; done
    for chain in RB-EGRESS RB-INPUT; do
        if iptables -w -L "$chain" -n >/dev/null 2>&1; then iptables -w -F "$chain"; iptables -w -X "$chain"; fi
    done
}

apply() {
    local blocked; blocked="$(IFS=,; echo "${blocked_nets[*]}")"
    # One transaction: create (if missing), delete, recreate. Never a moment without the filter once
    # it existed, never a half-built table.
    nft -f - <<EOF
table $table {}
delete table $table
table $table {
    set same_net {
        type ipv4_addr . ipv4_addr
        flags interval
        elements = { $(same_net_elements) }
    }
    set blocked {
        type ipv4_addr
        flags interval
        elements = { $blocked }
    }

    chain forward {
        type filter hook forward priority filter - 10; policy accept;
        ip saddr != $pool return
        ct state established,related accept
        # Nginx <-> browser on the user's own network (bridged traffic passes here with br_netfilter).
        ip saddr . ip daddr @same_net accept
        ip daddr @blocked reject with icmp type admin-prohibited
    }

    chain input {
        type filter hook input priority filter - 10; policy accept;
        ip saddr != $pool return
        ct state established,related accept
        # Podman's aardvark-dns on the network's own gateway.
        ip daddr $pool meta l4proto { tcp, udp } th dport 53 accept
        reject with icmp type admin-prohibited
    }
}
EOF
    remove_legacy
    echo "egress filter active for $pool"
}

remove() {
    nft delete table $table 2>/dev/null || true
    remove_legacy
}

case "${1:-}" in
    apply) apply ;;
    remove) remove; echo "egress filter removed for $pool" ;;
    status)
        if nft list chain $table forward 2>/dev/null | grep -qF "ip saddr != $pool return" \
                && nft list chain $table input >/dev/null 2>&1; then
            echo "egress filter active for $pool"
        else
            echo "egress filter NOT active for $pool"; exit 1
        fi ;;
    *) sed -n '2,24p' "$0"; exit 2 ;;
esac
