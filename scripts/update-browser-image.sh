#!/usr/bin/env bash
# Browser security updates (plan §54). Chrome and Firefox never update themselves inside the image:
#   1. build a fresh image (scripts/build-browser-image.sh: current Chrome and Firefox);
#   2. if both versions equal those of the running BROWSER_IMAGE, drop it and stop: nobody's session
#      is restarted for nothing;
#   3. smoke-test it: a throwaway container without network, with the production restrictions
#      (rendered by the sync), runs tests/phase2-browser.sh (chooser, both sandboxes, persistence);
#   4. roll out: BROWSER_IMAGE in the env file, then the sync recreates every browser (open streams end);
#   5. remove old images, keeping the newest --keep (default 3) and the running one for rollback.
# A failed build or smoke test leaves everything as it was. Run as root (remote-browser-image-update
# timer, weekly at night).
#
# Usage: ./scripts/update-browser-image.sh [--image TAG] [--no-rollout] [--keep N]
#   --image TAG    test (and roll out) an existing image instead of building one
#   --no-rollout   build and test only; prints the tag to roll out by hand
#   ENV_FILE=demo/demo.env ./scripts/update-browser-image.sh ...   (another env file)
#
# Rollback: set BROWSER_IMAGE to the previous tag (docker images local/remote-dual-browser), run the sync.
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
env_file="$(realpath "${ENV_FILE:-$repo_dir/.env}")"
repo="local/remote-dual-browser"
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

env_get() { grep -E "^$1=" "$env_file" | tail -n1 | cut -d= -f2- || true; }
versions_of() { docker run --rm --network none --entrypoint cat "$1" /etc/remote-browser-versions 2>/dev/null | tr '\n' ' '; }
current="$(env_get BROWSER_IMAGE)"

if [[ -z "$image" ]]; then
    echo "== building"
    image="$("$repo_dir/scripts/build-browser-image.sh" | tail -n1)"
    built=1
fi
docker image inspect "$image" >/dev/null || { echo "no image $image" >&2; exit 1; }
echo "candidate: $image ($(versions_of "$image"))"

if (( built )) && [[ -n "$current" ]] && docker image inspect "$current" >/dev/null 2>&1 \
        && [[ "$(versions_of "$image")" == "$(versions_of "$current")" ]]; then
    echo "Chrome and Firefox unchanged since $current: nothing to roll out"
    [[ "$image" == "$current" ]] || docker rmi "$image" >/dev/null
    exit 0
fi

echo "== smoke test"
project="remote-browser-smoke"
work="$(mktemp -d)"
smoke_compose() {
    BROWSER_IMAGE="$image" USER_DATA_ROOT="$work/users" PUBLIC_HOST=smoke.invalid TZ=UTC \
        docker compose --project-directory "$repo_dir" --env-file /dev/null -p "$project" -f "$work/compose.yaml" "$@"
}
cleanup() {
    smoke_compose down --volumes --timeout 5 >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

# A uid nobody else uses (root), or your own (the demo, without root).
if (( EUID == 0 )); then uid=65532 gid=65532; else uid="$(id -u)" gid="$(id -g)"; fi
install -d -m 0700 "$work/users/smoke" "$work/users/smoke/config"
(( EUID == 0 )) && chown -R "$uid:$gid" "$work/users/smoke"

# The service exactly as the sync renders it, but on no network at all.
engine="$(env_get CONTAINER_ENGINE)"
python3 - "$repo_dir" "$uid" "$gid" "${engine:-docker}" > "$work/compose.yaml" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("sync", f"{sys.argv[1]}/scripts/sync-freeipa-users.py")
sync = importlib.util.module_from_spec(spec)
sys.modules["sync"] = sync
spec.loader.exec_module(sync)
user = sync.User("smoke", int(sys.argv[2]), int(sys.argv[3]))
text = sync.render_compose([user], {"smoke": "192.0.2.0/28"}, "smoke-test", sys.argv[4])
text = text.split("\nnetworks:\n")[0] + "\n"           # drop the network definitions
text = text.replace("    networks:\n      - net-smoke\n", "    network_mode: none\n")
assert "net-smoke" not in text and "network_mode: none" in text
print(text, end="")
PY

smoke_compose up -d --quiet-pull
if ! PROJECT="$project" SERVICE=browser-smoke SMOKE=1 "$repo_dir/tests/phase2-browser.sh"; then
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
old_images="$(docker images "$repo" --format '{{.Repository}}:{{.Tag}}' \
    | { grep -E "^$repo:[0-9]{8}-chrome" || true; } | sort -r | tail -n +$((keep + 1)))"
[[ -z "$old_images" ]] || while read -r old; do
        [[ "$old" == "$image" || "$old" == "$current" ]] && continue
        docker rmi "$old" >/dev/null 2>&1 && echo "removed $old" || echo "kept $old (in use)"
    done <<<"$old_images"
echo "done: $image"
