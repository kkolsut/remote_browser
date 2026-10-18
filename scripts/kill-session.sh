#!/usr/bin/env bash
# End a user's open browser stream now (plan §17): restarts the user's browser (browser-<uid>.service).
#
# Authelia only checks the session when the stream's WebSocket opens. Logging out, an expired session
# or removal from the group does not end a stream that is already open. The sync removes the
# container of a user who lost access at its next run (timer: every 5 minutes); this is for "now".
# A restart lets the user reconnect if Authelia still lets them in: to lock someone out, disable the
# account in FreeIPA (or remove it from the group) first, then run this.
#
# Usage: sudo ./scripts/kill-session.sh <uid>
set -euo pipefail

uid="${1:-}"
[[ "$uid" =~ ^[a-z][a-z0-9-]{0,30}$ ]] || { sed -n '2,10p' "$0" >&2; exit 2; }

systemctl is-active --quiet "browser-$uid.service" || { echo "no running browser for $uid (browser-$uid.service)" >&2; exit 1; }
systemctl restart "browser-$uid.service"
echo "restarted browser-$uid: its open streams have ended"
