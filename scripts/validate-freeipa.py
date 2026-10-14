#!/usr/bin/env python3
"""Validate the FreeIPA integration before (and after) deploying.

Checks:
  * LDAPS connection with certificate verification, bind as the service account;
  * the access group exists and is readable;
  * every direct member: uid format, uidNumber/gidNumber, lock/expiry state, and whether
    Authelia's FreeIPA user filter matches it (evaluated by the LDAP server, as Authelia does);
  * attribute readability problems that would make the filters silently wrong.

Usage:
  ./scripts/validate-freeipa.py [--expect-eligible UID ...] [--expect-denied UID ...]

--expect-* turn known test accounts into hard assertions, e.g. a disabled test user:
  --expect-eligible remote-browser-test --expect-denied remote-browser-disabled

Exit status: 0 ok, 1 LDAP/config error, 2 assertion failed or members need attention.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from freeipa_common import FreeIPAError, connect, load_config, resolve_members  # noqa: E402


def status_of(m) -> str:
    if m.eligible:
        return "ELIGIBLE"
    reasons = []
    if not m.is_person:
        reasons.append("not-a-person")
    if m.locked:
        reasons.append("locked")
    if m.password_expiration is None:
        reasons.append("no-krbPasswordExpiration")
    if not m.eligible_by_filter and not reasons:
        reasons.append("expired")
    reasons += m.problems
    return "denied: " + ", ".join(reasons)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", type=Path)
    ap.add_argument("--password-file", type=Path)
    ap.add_argument("--expect-eligible", nargs="*", default=[], metavar="UID")
    ap.add_argument("--expect-denied", nargs="*", default=[], metavar="UID")
    args = ap.parse_args()

    try:
        cfg = load_config(args.env_file, args.password_file)
        print(f"LDAP:   {cfg.ldap_url}  (CA dir: {cfg.ca_dir or 'system trust store'})")
        print(f"Bind:   {cfg.bind_dn}")
        if ",cn=sysaccounts," not in cfg.bind_dn.lower().replace(" ", ""):
            print("WARN:   bind DN is not a system account; its password may expire (docs/freeipa.md)")
        conn = connect(cfg)
        print("OK:     TLS verified, bind succeeded")
        print(f"Group:  {cfg.group_dn}")
        members, nested = resolve_members(conn, cfg)
        conn.unbind()
    except FreeIPAError as exc:
        print(f"ERROR:  {exc}", file=sys.stderr)
        return 1

    print(f"OK:     group readable, {len(members)} direct user member(s)\n")
    width = max([len(m.uid or "?") for m in members] + [3])
    for m in members:
        print(f"  {(m.uid or '?'):<{width}}  uidNumber={m.uid_number}  gidNumber={m.gid_number}  {status_of(m)}")
        if m.uid is None:
            print(f"  {'':<{width}}  dn={m.dn}")

    attention = 0
    if nested:
        print("\nNOTE:   nested groups are ignored (direct membership only, same as Authelia):")
        for dn in nested:
            print(f"          {dn}")

    if members and all(m.password_expiration is None for m in members):
        attention += 1
        print("\nWARN:   krbPasswordExpiration is not visible for ANY member. Either the bind account cannot read it"
              "\n        (then Authelia will deny everyone) or no member has a password set. See docs/freeipa.md.")
    if members and not any(m.locked for m in members) and not args.expect_denied:
        print("\nHINT:   no member is locked, so nsAccountLock readability cannot be verified. Add a disabled test"
              "\n        user to the group and pass --expect-denied <uid>.")

    by_uid = {m.uid: m for m in members if m.uid}
    for uid in args.expect_eligible:
        m = by_uid.get(uid)
        if not (m and m.eligible):
            attention += 1
            print(f"FAIL:   expected {uid} to be eligible: {status_of(m) if m else 'not a direct member'}")
    for uid in args.expect_denied:
        m = by_uid.get(uid)
        if m is None:
            print(f"FAIL:   expected-denied user {uid} must be a direct member for this check to mean anything")
            attention += 1
        elif m.eligible:
            attention += 1
            print(f"FAIL:   expected {uid} to be denied but it is eligible "
                  "(is nsAccountLock / krbPrincipalExpiration readable by the bind account?)")
        else:
            print(f"OK:     {uid} denied ({status_of(m)})")

    eligible = sum(1 for m in members if m.eligible)
    print(f"\n{eligible} eligible user(s).")
    return 2 if attention else 0


if __name__ == "__main__":
    sys.exit(main())
