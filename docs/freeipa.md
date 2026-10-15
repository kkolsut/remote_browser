# FreeIPA preparation

Everything below uses `dc=example,dc=internal`; substitute your base DN. Run the `ipa` commands as an
IPA administrator (`kinit admin`).

## 1. Access group

```bash
ipa group-add remote-browser-users --desc="Users allowed to access remote browser"
ipa group-add-member remote-browser-users --users=alice,bob
```

**Direct membership only.** Authelia's FreeIPA groups filter is `(&(member={dn})(objectClass=groupOfNames))`,
which sees only direct members; the provisioning sync applies the same rule. Members reached through a nested
group are ignored, not partially enabled.

Usernames must match `^[a-z][a-z0-9-]{0,30}$` (no dots or underscores), because they become Docker service,
network and DNS names. Non-matching members are reported by `validate-freeipa.py` and never provisioned.

## 2. Test accounts

```bash
ipa user-add remote-browser-test     --first=Remote --last=Test     --password
ipa user-add remote-browser-outsider --first=Remote --last=Outsider --password
ipa user-add remote-browser-disabled --first=Remote --last=Disabled --password
ipa group-add-member remote-browser-users --users=remote-browser-test,remote-browser-disabled
ipa user-disable remote-browser-disabled
```

New FreeIPA passwords are expired on first use: log in once (e.g. `kinit remote-browser-test`) and change
them, otherwise Authelia's filter (`krbPasswordExpiration>=now`) rejects the accounts.

`remote-browser-disabled` stays **in** the group on purpose: it proves the bind account can read
`nsAccountLock`.

## 3. Service (bind) account — a system account

Use an LDAP system account under `cn=sysaccounts,cn=etc`. Do not use a regular user: the password policy
would expire its password (default after 90 days) and silently break logins and provisioning.

Recent FreeIPA releases provide `ipa sysaccount-add` (check `ipa help sysaccount`). Otherwise create it with
LDIF as Directory Manager:

```bash
PW="$(openssl rand -base64 36)"; echo "$PW"   # store it; it goes into the secrets dir later
ldapmodify -x -H ldaps://ipa.example.internal -D "cn=Directory Manager" -W <<EOF
dn: uid=svc_remote_browser,cn=sysaccounts,cn=etc,dc=example,dc=internal
changetype: add
objectclass: account
objectclass: simplesecurityobject
uid: svc_remote_browser
userPassword: $PW
passwordExpirationTime: 20380119031407Z
nsIdleTimeout: 0
EOF
```

## 4. Read permissions

The bind account must be able to read, **including in search filters**:

| Entry | Attributes |
|-------|------------|
| users | `uid`, `mail`, `displayName`, `memberOf`, `objectClass`, `nsAccountLock`, `krbPasswordExpiration`, `krbPrincipalExpiration`, `uidNumber`, `gidNumber` |
| groups | `cn`, `member`, `objectClass` |

Some of these (notably the Kerberos expiration attributes and `nsAccountLock`) are not readable by every
authenticated identity in default FreeIPA installs. **Unreadable attributes do not cause errors, they make
filters silently wrong**:

- unreadable `nsAccountLock` → disabled users can log in;
- unreadable `krbPasswordExpiration` → nobody can log in.

Grant an explicit, read-only permission:

```bash
ipa permission-add "Remote Browser - Read user account state" \
  --type=user --right=read --right=search --right=compare \
  --attrs={uid,mail,displayname,memberof,objectclass,nsaccountlock,krbpasswordexpiration,krbprincipalexpiration,uidnumber,gidnumber}

ipa permission-add "Remote Browser - Read group membership" \
  --type=group --right=read --right=search --right=compare \
  --attrs={cn,member,objectclass}

ipa privilege-add "Remote Browser Reader" --desc="Read-only access for the remote browser service"
ipa privilege-add-permission "Remote Browser Reader" \
  --permissions="Remote Browser - Read user account state" \
  --permissions="Remote Browser - Read group membership"

ipa role-add "Remote Browser Service" --desc="Remote browser LDAP bind account"
ipa role-add-privilege "Remote Browser Service" --privileges="Remote Browser Reader"
```

Add the system account to the role. With sysaccount support in `ipa`, use
`ipa role-add-member "Remote Browser Service" --sysaccounts=svc_remote_browser`; otherwise:

```bash
ldapmodify -x -H ldaps://ipa.example.internal -D "cn=Directory Manager" -W <<EOF
dn: cn=Remote Browser Service,cn=roles,cn=accounts,dc=example,dc=internal
changetype: modify
add: member
member: uid=svc_remote_browser,cn=sysaccounts,cn=etc,dc=example,dc=internal
EOF
```

Grant nothing else: no write rights, no password-reset rights.

## 5. CA certificate

Copy the IPA CA to the remote-browser VM (from any enrolled host, or `https://ipa.example.internal/ipa/config/ca.crt`):

```bash
install -m 0644 ca.crt local/freeipa-ca/ipa-ca.crt     # FREEIPA_CA_DIR
```

The Python scripts (`validate-freeipa.py`, `sync-freeipa-users.py`) trust certificates in that
directory. Your Authelia needs the same CA for its LDAPS connection. TLS verification is never disabled.

## 6. Verify

```bash
sudo apt install python3-ldap3
./scripts/validate-freeipa.py \
  --expect-eligible remote-browser-test \
  --expect-denied remote-browser-disabled
```

Exit status 0 means the connection, bind, group, attribute visibility and eligibility rules are all as
expected. Fix any `WARN`/`FAIL` before going further.

## 7. Lockout policy

Your Authelia's `regulation` must ban before the FreeIPA lockout threshold (e.g. 3 failures in 2 minutes), so
Internet attackers are stopped by Authelia before they lock the account domain-wide:

```bash
ipa pwpolicy-show --all | grep -i -E 'failure|lockout'
```

`Max failures` (`krbPwdMaxFailure`) should be greater than `regulation.max_retries`.
