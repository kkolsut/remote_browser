#!/usr/bin/env bash
# Browser security updates (plan §54). Chrome and Firefox never update themselves inside the image:
#   1. build a fresh image (scripts/build-browser-image.sh: current Chrome and Firefox);
#   2. if both versions equal those of the running BROWSER_IMAGE, drop it and stop: nobody's session
#      is restarted for nothing;
#   3. smoke-test it: the browser unit exactly as the sync renders it, but without network, as a
#      transient Quadlet (/run/containers/systemd/rb-smoke.container), runs tests/phase2-browser.sh
#      (chooser, both sandboxes, persistence);
#   4. roll out: BROWSER_IMAGE in the env file, then the sync restarts every browser (open streams end);
#   5. remove old images, keeping the newest --keep (default 3) and the running one for rollback.
# A failed build or smoke test leaves everything as it was. Run as root (remote-browser-image-update
# timer, weekly at night): rootful Podman and systemd.
#
# Usage: ./scripts/update-browser-image.sh [--image TAG] [--no-rollout] [--keep N]
#   --image TAG    test (and roll out) an existing image instead of building one
#   --no-rollout   build and test only; prints the tag to roll out by hand
#
# Rollback: set BROWSER_IMAGE to the previous tag (podman images localhost/remote-dual-browser), run the sync.
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
env_file="$(realpath "${ENV_FILE:-$repo_dir/.env}")"
repo="localhost/remote-dual-browser"
image="" rollout=1 keep=3 built=0

while (( $# )); do
    case "$1" in
        --image) image="$2"; shift 2 ;;
        --no-rollout) rollout=0; shift ;;
        --keep) keep="$2"; shift 2 ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[[ "$keep" =~ ^[1-9][0-9]*$ ]] || { echo "--keep needs a number >= 1" >&2; exit 2; }
[[ -f "$env_file" ]] || { echo "missing $env_file" >&2; exit 1; }
(( EUID == 0 )) || { echo "run as root (rootful Podman, systemd)" >&2; exit 1; }

env_get() { grep -E "^$1=" "$env_file" | tail -n1 | cut -d= -f2- || true; }
versions_of() { podman run --rm --network none --entrypoint cat "$1" /etc/remote-browser-versions 2>/dev/null | tr '\n' ' '; }
current="$(env_get BROWSER_IMAGE)"

if [[ -z "$image" ]]; then
    echo "== building"
    image="$("$repo_dir/scripts/build-browser-image.sh" | tail -n1)"
    built=1
fi
podman image exists "$image" || { echo "no image $image" >&2; exit 1; }
echo "candidate: $image ($(versions_of "$image"))"

if (( built )) && [[ -n "$current" ]] && podman image exists "$current" \
        && [[ "$(versions_of "$image")" == "$(versions_of "$current")" ]]; then
    echo "Chrome and Firefox unchanged since $current: nothing to roll out"
    [[ "$image" == "$current" ]] || podman rmi "$image" >/dev/null
    exit 0
fi

echo "== smoke test"
# A uid that is never a provisioned user (the sync refuses "_"), so the container name cannot clash.
smoke_uid="smoke_test" uid=65532 gid=65532
smoke_unit=/run/containers/systemd/rb-smoke.container
work="$(mktemp -d /var/tmp/rb-smoke.XXXXXX)"
cleanup() {
    systemctl stop rb-smoke.service >/dev/null 2>&1 || true
    rm -f "$smoke_unit"
    systemctl daemon-reload || true
    rm -rf "$work"
}
trap cleanup EXIT

install -d -m 0700 "$work/users/$smoke_uid" "$work/users/$smoke_uid/config"
chown -R "$uid:$gid" "$work/users/$smoke_uid"

# The unit exactly as the sync renders it, but on no network at all.
mkdir -p "$(dirname "$smoke_unit")"
python3 - "$repo_dir" "$image" "$work/users" "$smoke_uid" "$uid" "$gid" > "$smoke_unit" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, f"{sys.argv[1]}/scripts")
import rb_units
cfg = rb_units.BrowserConfig(image=sys.argv[2], tz="UTC", public_host="smoke.invalid", public_port="443",
                             file_transfers="", user_data_root=Path(sys.argv[3]))
print(rb_units.browser_container(sys.argv[4], int(sys.argv[5]), int(sys.argv[6]), cfg, network="none"), end="")
PY
systemctl daemon-reload
systemctl start rb-smoke.service
if ! CONTAINER="browser-$smoke_uid" UNIT=rb-smoke.service SMOKE=1 "$repo_dir/tests/phase2-browser.sh"; then
    echo "SMOKE TEST FAILED: $image is not rolled out" >&2
    exit 1
fi

if (( ! rollout )); then
    echo "smoke test passed; to roll out: BROWSER_IMAGE=$image in $env_file, then run the sync"
    exit 0
fi

echo "== rollout"
if grep -qE '^BROWSER_IMAGE=' "$env_file"; then
    sed -i "s|^BROWSER_IMAGE=.*|BROWSER_IMAGE=$image|" "$env_file"
else
    echo "BROWSER_IMAGE=$image" >> "$env_file"
fi
echo "BROWSER_IMAGE=$image (was: ${current:-unset})"
rc=0
"$repo_dir/scripts/sync-freeipa-users.py" --env-file "$env_file" || rc=$?
(( rc == 0 || rc == 2 )) || { echo "sync failed (exit $rc); BROWSER_IMAGE is set, fix and run the sync" >&2; exit 1; }

echo "== old images"
# Tags start with the build date, so they sort by age.
old_images="$(podman images "$repo" --format '{{.Repository}}:{{.Tag}}' \
    | { grep -E "^$repo:[0-9]{8}-chrome" || true; } | sort -r | tail -n +$((keep + 1)))"
[[ -z "$old_images" ]] || while read -r old; do
        [[ "$old" == "$image" || "$old" == "$current" ]] && continue
        podman rmi "$old" >/dev/null 2>&1 && echo "removed $old" || echo "kept $old (in use)"
    done <<<"$old_images"
echo "done: $image"
