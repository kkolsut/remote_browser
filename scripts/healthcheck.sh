#!/usr/bin/env bash
# Health of the service (plan §48-49), one line per check: OK / WARN / CRIT.
# Exit status: 0 all OK, 1 a warning, 2 something critical: usable from cron (mail on output with
# --quiet), a systemd timer, or a monitoring push, e.g. Uptime Kuma:
#   ./scripts/healthcheck.sh --quiet && curl -fsS "https://kuma.example/api/push/<token>?status=up"
#
# Checks: Nginx and every provisioned browser running/healthy and not restarting in a loop; Authelia's
# health endpoint; FreeIPA LDAPS bind (as the sync would); TLS certificate expiry; disk space for
# profiles; memory; time since the last successful sync; age of the newest backup; egress filter
# (root only). Read-only; run as root for the egress and FreeIPA checks.
#
# Usage: ./scripts/healthcheck.sh [--quiet]          ENV_FILE=demo/demo.env ./scripts/healthcheck.sh
set -uo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
env_file="$(realpath "${ENV_FILE:-$repo_dir/.env}")"
quiet=0; [[ "${1:-}" == --quiet ]] && quiet=1
env_get() {
    local v; v="$(grep -E "^$1=" "$env_file" 2>/dev/null | tail -n1 | cut -d= -f2- || true)"; v="${v:-$2}"
    [[ "${3:-}" == path && "$v" != /* ]] && v="$repo_dir/${v#./}"
    echo "$v"
}

worst=0
report() {  # level name detail
    local level="$1" code=0
    case "$level" in WARN) code=1 ;; CRIT) code=2 ;; esac
    (( code > worst )) && worst=$code
    (( quiet && code == 0 )) || printf '%-4s  %s: %s\n' "$level" "$2" "$3"
}

project="$(env_get COMPOSE_PROJECT_NAME remote-browser)"
generated="$(env_get GENERATED_DIR ./generated path)"
user_root="$(env_get USER_DATA_ROOT ./local/users path)"
backend="$(env_get AUTH_BACKEND freeipa)"

container_health() {  # service -> "status health restarts"
    local cid; cid="$(docker ps -aq --filter "label=com.docker.compose.project=$project" --filter "label=com.docker.compose.service=$1" | head -n1)"
    [[ -n "$cid" ]] || { echo "missing - 0"; return; }
    docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}} {{.RestartCount}}' "$cid"
}
check_container() {  # service, level when down
    read -r status health restarts <<<"$(container_health "$1")"
    if [[ "$status" != running ]]; then report "$2" "$1" "$status"
    elif [[ "$health" == unhealthy ]]; then report "$2" "$1" "unhealthy"
    elif (( restarts > 3 )); then report WARN "$1" "running, but restarted $restarts times"
    else report OK "$1" "running${health:+ ($health)}"; fi
}

# --- containers ---
check_container nginx CRIT
users=()
if [[ -f "$generated/state.json" ]]; then
    mapfile -t users < <(python3 -c 'import json,sys; [print(u) for u in sorted(json.load(open(sys.argv[1]))["users"])]' "$generated/state.json")
fi
down=()
for u in "${users[@]}"; do
    read -r status health restarts <<<"$(container_health "browser-$u")"
    if [[ "$status" != running || "$health" == unhealthy ]]; then down+=("$u:$status/$health")
    elif (( restarts > 3 )); then report WARN "browser-$u" "restarted $restarts times"; fi
done
if (( ${#down[@]} )); then report CRIT browsers "${#down[@]} of ${#users[@]} not running/healthy: ${down[*]}"
else report OK browsers "${#users[@]} running"; fi

# --- Authelia ---
authz="$(env_get AUTHELIA_AUTHZ_URL '')"
if [[ -n "$authz" ]]; then
    health_url="${authz%%/api/*}/api/health"
    ca_file="$(env_get AUTHELIA_CA_FILE '')"
    curl_ca=()
    if [[ "$ca_file" == /etc/nginx/authelia-ca/* ]]; then
        curl_ca=(--cacert "$(env_get AUTHELIA_CA_DIR ./local/authelia-ca path)/${ca_file#/etc/nginx/authelia-ca/}")
    fi
    if [[ "$authz" == http://authelia:* ]]; then  # the demo's Authelia is only on the Docker network
        out="$(docker exec "$(docker ps -q --filter "label=com.docker.compose.project=$project" --filter label=com.docker.compose.service=nginx)" \
            wget -q -O- "$health_url" 2>&1)"
    else
        out="$(curl -fsS --max-time 10 "${curl_ca[@]}" "$health_url" 2>&1)"
    fi
    if grep -q '"OK"' <<<"$out"; then report OK authelia "$health_url"
    else report CRIT authelia "$health_url: ${out:0:120}"; fi
fi

# --- FreeIPA ---
if [[ "$backend" == freeipa ]]; then
    out="$(python3 - "$repo_dir" "$env_file" 2>&1 <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, f"{sys.argv[1]}/scripts")
from freeipa_common import FreeIPAError, connect, load_config
try:
    connect(load_config(Path(sys.argv[2]))).unbind()
except FreeIPAError as exc:
    sys.exit(str(exc))
PY
)"
    if [[ $? == 0 ]]; then report OK freeipa "LDAPS bind works"; else report CRIT freeipa "${out:0:160}"; fi
fi

# --- TLS certificate presented to the reverse proxy ---
cert="$(env_get TLS_DIR ./local/tls path)/fullchain.pem"
if [[ -f "$cert" ]]; then
    end="$(openssl x509 -in "$cert" -noout -enddate | cut -d= -f2)"
    if ! openssl x509 -in "$cert" -noout -checkend $((14 * 86400)) >/dev/null; then report CRIT tls "expires $end"
    elif ! openssl x509 -in "$cert" -noout -checkend $((30 * 86400)) >/dev/null; then report WARN tls "expires $end"
    else report OK tls "valid until $end"; fi
else
    report CRIT tls "$cert missing"
fi

# --- disk and memory ---
if [[ -d "$user_root" ]]; then
    used="$(df --output=pcent "$user_root" | tail -n1 | tr -dc 0-9)"
    largest="$(du -s "$user_root"/*/ 2>/dev/null | sort -rn | head -n3 | awk '{printf "%s %dM, ", $2, $1/1024}' | sed "s|$user_root/||g; s|/ | |g; s|, $||")"
    if (( used >= 90 )); then report CRIT disk "${used}% used for profiles; largest: $largest"
    elif (( used >= 80 )); then report WARN disk "${used}% used for profiles; largest: $largest"
    else report OK disk "${used}% used; largest profiles: ${largest:-none}"; fi
fi
read -r total avail <<<"$(free -m | awk '/^Mem:/ {print $2, $7}')"
if (( avail * 100 / total < 10 )); then report WARN memory "${avail} MiB of ${total} MiB available"
else report OK memory "${avail} MiB of ${total} MiB available"; fi

# --- sync ---
if [[ -f "$generated/state.json" ]]; then
    if systemctl cat remote-browser-sync.service >/dev/null 2>&1; then
        result="$(systemctl show remote-browser-sync.service -p Result --value)"
        status="$(systemctl show remote-browser-sync.service -p ExecMainStatus --value)"
        since="$(systemctl show remote-browser-sync.service -p ExecMainExitTimestamp --value)"
        if [[ "$status" == 0 ]]; then report OK sync "last run ok ($since)"
        elif [[ "$status" == 2 ]]; then report WARN sync "last run skipped members ($since): journalctl -u remote-browser-sync"
        else report CRIT sync "last run failed: $result, exit $status ($since): journalctl -u remote-browser-sync"; fi
        systemctl is-active --quiet remote-browser-sync.timer || report CRIT sync-timer "remote-browser-sync.timer not active"
    else
        report WARN sync "remote-browser-sync timer not installed"
    fi
else
    report CRIT sync "never ran ($generated/state.json missing)"
fi

# --- backup ---
backup_root="$(env_get BACKUP_DIR ./local/backups path)"
newest="$(find "$backup_root" -maxdepth 1 -type d -name 'remote-browser-*' -printf '%T@ %f\n' 2>/dev/null | sort -rn | head -n1)"
if [[ -z "$newest" ]]; then report WARN backup "no backup in $backup_root"
else
    age_h=$(( ($(date +%s) - ${newest%%.*}) / 3600 ))
    if (( age_h > 48 )); then report WARN backup "newest is ${age_h}h old: ${newest#* }"
    else report OK backup "${newest#* } (${age_h}h old)"; fi
fi

# --- egress filter ---
if (( EUID == 0 )); then
    if out="$(ENV_FILE="$env_file" "$repo_dir/scripts/egress-firewall.sh" status 2>&1)"; then report OK egress "$out"
    else report CRIT egress "$out"; fi
fi

exit $worst
