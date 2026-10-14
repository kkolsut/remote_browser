#!/usr/bin/env bash
# Build the dual-browser image and tag it with the build date and the installed browser versions:
#   local/remote-dual-browser:<YYYYMMDD>-chrome<version>-firefox<version>
# Always builds without cache, so Chrome and Firefox are the current releases (plan §54).
# Prints the tag as the last line; roll it out by setting BROWSER_IMAGE in .env.
#
# Usage: ./scripts/build-browser-image.sh [--tag NAME:TAG]   (extra tag, e.g. local/remote-dual-browser:demo)
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
repo="local/remote-dual-browser"
extra_tags=()

while (( $# )); do
    case "$1" in
        --tag) extra_tags+=("$2"); shift 2 ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

build_tag="$repo:build-$(date +%Y%m%d%H%M%S)"
docker build --pull --no-cache -t "$build_tag" "$repo_dir/browser-image" >&2

versions="$(docker run --rm --entrypoint cat "$build_tag" /etc/remote-browser-versions)"
chrome="$(sed -n 's/^chrome=//p' <<<"$versions")"
firefox="$(sed -n 's/^firefox=//p' <<<"$versions")"
[[ -n "$chrome" && -n "$firefox" ]] || { echo "cannot read browser versions from the image" >&2; exit 1; }

# Docker tags allow [A-Za-z0-9_.-]; Debian versions may contain ~ and +.
tag="$repo:$(date +%Y%m%d)-chrome${chrome%%-*}-firefox${firefox%%[~+-]*}"
tag="${tag//[^A-Za-z0-9_.:\/-]/_}"
docker tag "$build_tag" "$tag"
for t in "${extra_tags[@]}"; do docker tag "$build_tag" "$t"; done
docker rmi "$build_tag" >/dev/null

echo "Chrome $chrome, Firefox $firefox" >&2
echo "$tag"
