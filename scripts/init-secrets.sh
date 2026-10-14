#!/usr/bin/env bash
# Create host directories and generate deployment secrets. Idempotent: existing secrets are never
# overwritten. Runs as an ordinary user; directories and secrets are owned by whoever runs it.
# Authelia secrets are created only for a bundled Authelia (AUTHELIA_DATA_DIR set: the local demo);
# production uses the site's existing Authelia.
# Relative paths in .env are resolved against the repository directory (as Docker Compose does).
#
# Usage: ./scripts/init-secrets.sh [--self-signed-tls]
#        ENV_FILE=demo/demo.env ./scripts/init-secrets.sh ...   (another env file)
#
#   --self-signed-tls   also create a self-signed certificate for PUBLIC_HOST (+ AUTHELIA_HOST) in
#                       TLS_DIR, valid 10 years: the certificate Nginx presents to the reverse proxy,
#                       which pins it (docs/apache-authelia.md). Or put a certificate from your CA there.
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
env_file="${ENV_FILE:-$repo_dir/.env}"

self_signed=0
for arg in "$@"; do
    case "$arg" in
        --self-signed-tls) self_signed=1 ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

[[ -f "$env_file" ]] || { echo "missing $env_file (copy .env.example first)" >&2; exit 1; }

# Read KEY from .env without sourcing it (values may contain spaces).
env_get() {
    local value
    value="$(grep -E "^$1=" "$env_file" | tail -n1 | cut -d= -f2- || true)"
    value="${value%\"}"; value="${value#\"}"
    echo "${value:-$2}"
}

# Absolute path; relative values are taken relative to the repository, like compose.yaml bind mounts.
env_path() {
    local value
    value="$(env_get "$1" "$2")"
    [[ "$value" == /* ]] || value="$repo_dir/${value#./}"
    echo "$value"
}

secrets_dir="$(env_path SECRETS_DIR ./local/secrets)"
tls_dir="$(env_path TLS_DIR ./local/tls)"
ca_dir="$(env_path FREEIPA_CA_DIR ./local/freeipa-ca)"
authelia_ca_dir="$(env_path AUTHELIA_CA_DIR ./local/authelia-ca)"
bundled_authelia="$(env_get AUTHELIA_DATA_DIR '')"
user_data="$(env_path USER_DATA_ROOT ./local/users)"
public_host="$(env_get PUBLIC_HOST '')"
authelia_host="$(env_get AUTHELIA_HOST '')"

install -d -m 0700 "$secrets_dir" || { echo "cannot create directories; use paths you can write to in .env (e.g. ./local/...)" >&2; exit 1; }
install -d -m 0755 "$tls_dir" "$ca_dir" "$authelia_ca_dir" "$user_data"
[[ -n "$bundled_authelia" ]] && install -d -m 0700 "$(env_path AUTHELIA_DATA_DIR '')"

gen_secret() {
    local file="$secrets_dir/$1"
    if [[ -s "$file" ]]; then
        echo "keep     $file"
    else
        (umask 077; openssl rand -hex 64 | tr -d '\n' > "$file")
        echo "created  $file"
    fi
}

if [[ -n "$bundled_authelia" ]]; then
    gen_secret authelia_session_secret
    gen_secret authelia_storage_encryption_key
    gen_secret authelia_reset_password_jwt_secret
fi

bind_pw_file="$secrets_dir/freeipa_bind_password"
if [[ "$(env_get AUTH_BACKEND freeipa)" == file ]]; then
    echo "skip     $bind_pw_file (AUTH_BACKEND=file)"
elif [[ -s "$bind_pw_file" ]]; then
    echo "keep     $bind_pw_file"
else
    read -r -s -p "FreeIPA bind password for $(env_get FREEIPA_BIND_DN '?'): " bind_pw; echo
    [[ -n "$bind_pw" ]] || { echo "empty password" >&2; exit 1; }
    (umask 077; printf '%s' "$bind_pw" > "$bind_pw_file")
    unset bind_pw
    echo "created  $bind_pw_file"
fi

compgen -G "$secrets_dir/*" >/dev/null && chmod 0600 "$secrets_dir"/*

if (( self_signed )); then
    [[ -n "$public_host" ]] || { echo "PUBLIC_HOST not set" >&2; exit 1; }
    if [[ -s "$tls_dir/fullchain.pem" ]]; then
        echo "keep     $tls_dir/fullchain.pem (delete it to regenerate)"
    else
        san="DNS:$public_host"; [[ -n "$authelia_host" ]] && san+=",DNS:$authelia_host"
        (umask 077; openssl req -x509 -newkey rsa:3072 -nodes -days 3650 \
            -subj "/CN=$public_host" \
            -addext "subjectAltName=$san" \
            -keyout "$tls_dir/privkey.pem" -out "$tls_dir/fullchain.pem" 2>/dev/null)
        chmod 0644 "$tls_dir/fullchain.pem"
        echo "created  self-signed certificate in $tls_dir (copy fullchain.pem to the reverse proxy)"
    fi
fi

if ! compgen -G "$ca_dir/*.crt" >/dev/null && ! compgen -G "$ca_dir/*.pem" >/dev/null; then
    echo
    echo "NOTE: no FreeIPA CA certificate in $ca_dir."
    echo "      Copy /etc/ipa/ca.crt from an IPA-enrolled host there, unless the IPA CA is already publicly trusted."
    echo "      (Used by the FreeIPA scripts: validate-freeipa.py, sync-freeipa-users.py.)"
fi
