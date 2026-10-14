#!/usr/bin/env python3
"""Generate browser-image/seccomp-chrome.json from Docker's default seccomp profile (plan §31).

Docker's default profile allows clone/unshare with namespace flags only to containers holding
CAP_SYS_ADMIN, so Chrome and Firefox cannot build their user-namespace sandboxes. This adds exactly
one rule on top of the default: clone and unshare are allowed unconditionally. Everything else
(mount, setns, pivot_root, bpf, keyctl, ...) stays as in Docker's default. clone3 keeps returning
ENOSYS, so glibc falls back to clone.

The reference is Google's chrome.json, which does the same on an older default profile.

Usage: scripts/gen-seccomp-chrome.py [SOURCE.json]
       (default: download the pinned moby/profiles default.json)
"""

import json
import sys
import urllib.request
from pathlib import Path

# Bump deliberately, then regenerate and test chrome://sandbox (plan §44).
MOBY_PROFILES_COMMIT = "6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef"
SOURCE_URL = f"https://raw.githubusercontent.com/moby/profiles/{MOBY_PROFILES_COMMIT}/seccomp/default.json"
OUTPUT = Path(__file__).resolve().parent.parent / "browser-image" / "seccomp-chrome.json"

SANDBOX_RULE = {
    "names": ["clone", "unshare"],
    "action": "SCMP_ACT_ALLOW",
    "comment": "remote-browser: Chrome/Firefox user-namespace sandbox (scripts/gen-seccomp-chrome.py)",
}


def main() -> int:
    if len(sys.argv) > 1:
        profile = json.loads(Path(sys.argv[1]).read_text())
    else:
        with urllib.request.urlopen(SOURCE_URL, timeout=30) as resp:
            profile = json.load(resp)

    if profile.get("defaultAction") != "SCMP_ACT_ERRNO":
        print("unexpected source profile: defaultAction is not SCMP_ACT_ERRNO", file=sys.stderr)
        return 1
    if any(r.get("comment") == SANDBOX_RULE["comment"] for r in profile["syscalls"]):
        print("source profile already contains the sandbox rule", file=sys.stderr)
        return 1

    profile["syscalls"].append(SANDBOX_RULE)
    OUTPUT.write_text(json.dumps(profile, indent=2) + "\n")
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
