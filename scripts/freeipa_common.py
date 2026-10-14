"""Shared FreeIPA access for validate-freeipa.py and (Phase 3) sync-freeipa-users.py.

Eligibility rules MUST match Authelia's `freeipa` LDAP implementation defaults, otherwise users
get provisioned whom Authelia denies (or the reverse):

  users filter:  (objectClass=person)(!(nsAccountLock=TRUE))
                 (krbPasswordExpiration>={now})
                 (|(!(krbPrincipalExpiration=*))(krbPrincipalExpiration>={now}))
  groups filter: (&(member={dn})(objectClass=groupOfNames))   -> DIRECT membership only

Any LDAP problem raises FreeIPAError. Callers must treat that as "abort, change nothing" and never
as an empty group.
"""

from __future__ import annotations

import os
import re
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import ldap3
    from ldap3.core.exceptions import LDAPException, LDAPNoSuchObjectResult
    from ldap3.utils.dn import parse_dn
except ImportError:  # pragma: no cover - environment problem, reported clearly
    raise SystemExit("python3-ldap3 is required: apt install python3-ldap3")

REPO_DIR = Path(__file__).resolve().parent.parent

# Lowercase only: the uid becomes part of Compose service, Docker DNS and network names and a
# case-sensitive Nginx map key (plan §33).
UID_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")

USER_ATTRS = [
    "uid",
    "uidNumber",
    "gidNumber",
    "objectClass",
    "nsAccountLock",
    "krbPasswordExpiration",
    "krbPrincipalExpiration",
]


class FreeIPAError(Exception):
    """LDAP/configuration failure. Never interpret as 'no users'."""


@dataclass(frozen=True)
class Config:
    ldap_url: str
    base_dn: str
    bind_dn: str
    bind_password: str
    group: str
    ca_dir: Path | None
    timeout: int = 10

    @property
    def users_dn(self) -> str:
        return f"cn=users,cn=accounts,{self.base_dn}"

    @property
    def groups_dn(self) -> str:
        return f"cn=groups,cn=accounts,{self.base_dn}"

    @property
    def group_dn(self) -> str:
        return f"cn={escape_dn_value(self.group)},{self.groups_dn}"


@dataclass
class Member:
    dn: str
    uid: str | None = None
    uid_number: int | None = None
    gid_number: int | None = None
    locked: bool = False
    password_expiration: str | None = None
    principal_expiration: str | None = None
    is_person: bool = False
    eligible_by_filter: bool = False  # server-side evaluation of Authelia's filter
    problems: list[str] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return self.eligible_by_filter and not self.problems


def escape_dn_value(value: str) -> str:
    return re.sub(r'([,+"\\<>;=#])', r"\\\1", value)


def norm_dn(dn: str) -> str:
    """Case- and whitespace-insensitive DN form for suffix comparisons."""
    dn = re.sub(r"(?<!\\),\s+", ",", dn.strip())
    try:
        return ",".join(f"{a.strip()}={v.strip()}" for a, v, _ in parse_dn(dn)).lower()
    except LDAPException as exc:
        raise FreeIPAError(f"invalid DN {dn!r}: {exc}") from exc


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE reader for .env (no shell expansion)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _repo_path(value: str) -> Path:
    """Relative .env paths are relative to the repository, as in compose.yaml bind mounts."""
    path = Path(value)
    return path if path.is_absolute() else REPO_DIR / path


def load_config(env_file: Path | None = None, password_file: Path | None = None) -> Config:
    env = read_env_file(env_file or REPO_DIR / ".env")
    env.update({k: v for k, v in os.environ.items() if k.startswith("FREEIPA_") or k == "SECRETS_DIR"})

    def need(key: str) -> str:
        value = env.get(key, "").strip()
        if not value:
            raise FreeIPAError(f"{key} is not set (.env or environment)")
        return value

    secrets_dir = _repo_path(env.get("SECRETS_DIR") or "./local/secrets")
    pw_path = password_file or secrets_dir / "freeipa_bind_password"
    try:
        password = pw_path.read_text().rstrip("\n")
    except OSError as exc:
        raise FreeIPAError(f"cannot read bind password from {pw_path}: {exc.strerror}") from exc
    if not password:
        raise FreeIPAError(f"bind password file {pw_path} is empty")

    group = env.get("FREEIPA_BROWSER_GROUP") or "remote-browser-users"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", group):
        raise FreeIPAError(f"FREEIPA_BROWSER_GROUP {group!r} contains unsupported characters")

    ca_dir = _repo_path(env.get("FREEIPA_CA_DIR") or "./local/freeipa-ca")
    url = need("FREEIPA_LDAP_URL")
    if not url.lower().startswith("ldaps://"):
        raise FreeIPAError("FREEIPA_LDAP_URL must use ldaps://")

    return Config(
        ldap_url=url,
        base_dn=need("FREEIPA_BASE_DN"),
        bind_dn=need("FREEIPA_BIND_DN"),
        bind_password=password,
        group=group,
        ca_dir=ca_dir if ca_dir.is_dir() else None,
    )


def _tls(cfg: Config) -> ldap3.Tls:
    ca_data = None
    if cfg.ca_dir:
        pems = sorted(p for p in cfg.ca_dir.iterdir() if p.suffix in (".crt", ".pem"))
        if pems:
            ca_data = "".join(p.read_text() for p in pems)
    # Certificate and hostname verification are always on. With no CA files, the system trust store
    # is used.
    return ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_data=ca_data)


def connect(cfg: Config) -> ldap3.Connection:
    try:
        server = ldap3.Server(cfg.ldap_url, use_ssl=True, tls=_tls(cfg),
                              get_info=ldap3.NONE, connect_timeout=cfg.timeout)
        conn = ldap3.Connection(server, user=cfg.bind_dn, password=cfg.bind_password,
                                receive_timeout=cfg.timeout, raise_exceptions=True,
                                read_only=True, auto_bind=ldap3.AUTO_BIND_NO_TLS)
    except LDAPException as exc:
        raise FreeIPAError(f"LDAP connect/bind to {cfg.ldap_url} as {cfg.bind_dn} failed: {exc}") from exc
    return conn


def _raw(entry: dict, attr: str) -> list[str]:
    for key, values in entry.get("raw_attributes", {}).items():
        if key.lower() == attr.lower():
            return [v.decode("utf-8", "replace") for v in values]
    return []


def _search(conn: ldap3.Connection, base: str, flt: str, attrs: list[str], scope=ldap3.BASE) -> list[dict]:
    try:
        conn.search(base, flt, search_scope=scope, attributes=attrs)
    except LDAPNoSuchObjectResult:
        return []
    except LDAPException as exc:
        raise FreeIPAError(f"LDAP search failed (base={base}): {exc}") from exc
    return [e for e in conn.response if e.get("type") == "searchResEntry"]


def generalized_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%SZ")


def authelia_user_conditions(now: str) -> str:
    return (
        "(objectClass=person)(!(nsAccountLock=TRUE))"
        f"(krbPasswordExpiration>={now})"
        f"(|(!(krbPrincipalExpiration=*))(krbPrincipalExpiration>={now}))"
    )


def direct_member_dns(conn: ldap3.Connection, cfg: Config) -> tuple[list[str], list[str]]:
    """Return (user DNs, nested group DNs) directly listed in the group's `member` attribute."""
    entries = _search(conn, cfg.group_dn, "(objectClass=groupOfNames)", ["member"])
    if not entries:
        raise FreeIPAError(f"group not found or not readable: {cfg.group_dn}")
    users, groups = [], []
    users_suffix = norm_dn(cfg.users_dn)
    groups_suffix = norm_dn(cfg.groups_dn)
    for dn in _raw(entries[0], "member"):
        norm = norm_dn(dn)
        if norm.endswith("," + users_suffix):
            users.append(dn)
        elif norm.endswith("," + groups_suffix):
            groups.append(dn)
        else:
            users.append(dn)  # unexpected location; will be reported as not a person / invalid
    return users, groups


def resolve_members(conn: ldap3.Connection, cfg: Config) -> tuple[list[Member], list[str]]:
    """Read every direct member and evaluate Authelia's user filter for it server-side."""
    user_dns, nested_groups = direct_member_dns(conn, cfg)
    now = generalized_now()
    flt = f"(&{authelia_user_conditions(now)})"
    members: list[Member] = []
    for dn in user_dns:
        m = Member(dn=dn)
        entries = _search(conn, dn, "(objectClass=*)", USER_ATTRS)
        if not entries:
            m.problems.append("entry not found or not readable")
            members.append(m)
            continue
        e = entries[0]
        uid = _raw(e, "uid")
        m.uid = uid[0] if uid else None
        m.is_person = any(oc.lower() == "person" for oc in _raw(e, "objectClass"))
        m.locked = any(v.upper() == "TRUE" for v in _raw(e, "nsAccountLock"))
        pe = _raw(e, "krbPasswordExpiration")
        m.password_expiration = pe[0] if pe else None
        ke = _raw(e, "krbPrincipalExpiration")
        m.principal_expiration = ke[0] if ke else None
        for attr, setter in (("uidNumber", "uid_number"), ("gidNumber", "gid_number")):
            val = _raw(e, attr)
            if val and val[0].isdigit():
                setattr(m, setter, int(val[0]))

        m.eligible_by_filter = bool(_search(conn, dn, flt, ["uid"]))

        if not m.uid:
            m.problems.append("no readable uid")
        elif not UID_RE.fullmatch(m.uid):
            m.problems.append(f"uid {m.uid!r} does not match {UID_RE.pattern}")
        if m.uid_number is None or m.gid_number is None:
            m.problems.append("uidNumber/gidNumber missing or not readable")
        members.append(m)

    members.sort(key=lambda m: (m.uid or "", m.dn))
    return members, sorted(nested_groups)


def eligible_users(conn: ldap3.Connection, cfg: Config) -> list[Member]:
    """Users who may log in AND be provisioned. Raises FreeIPAError on any LDAP problem."""
    members, _ = resolve_members(conn, cfg)
    seen: set[str] = set()
    result = []
    for m in members:
        if m.eligible and m.uid not in seen:
            seen.add(m.uid)
            result.append(m)
    return result

