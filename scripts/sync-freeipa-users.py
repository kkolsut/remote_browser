#!/usr/bin/env python3
"""Provision one browser container per eligible FreeIPA user (plan §14-17, §32-35).

Reads the direct members of FREEIPA_BROWSER_GROUP that pass Authelia's FreeIPA account filter
(freeipa_common.py) and makes the Podman host match (scripts/rb_units.py):

  QUADLET_DIR/browser-<uid>.container  the user's browser (browser-<uid>.service)
  QUADLET_DIR/rb-net-<uid>.network     the user's own network, a /28 of BROWSER_NET_POOL
  GENERATED_DIR/state.json             uidNumber/gidNumber/subnet per user (subnets stay put)
  NGINX_GENERATED_DIR/users.map        "<uid>" "browser-<uid>:3000"; whitelist for Nginx
  USER_DATA_ROOT/<uid>/config          created owned by uidNumber:gidNumber, mode 0700; never deleted

then, unless --generate-only / --dry-run:
  1. stops the browsers of users who are no longer eligible (ends their streams), removes their units;
  2. installs the new units, `systemctl daemon-reload`;
  3. reloads Nginx after `nginx -t` (the previous map is restored if the test fails);
  4. restarts browsers whose unit changed (e.g. BROWSER_IMAGE), starts every user's browser;
  5. attaches Nginx to every user network, live: listing the networks in rb-nginx.container would
     restart Nginx, and drop every open stream, whenever a user is added;
  6. detaches Nginx from and removes user networks of users who lost access.

--attach-only (rb-nginx.service's ExecStartPost): only step 5, for the users in state.json. A new Nginx
container is on no user network.

Fail-safe (plan §32): any LDAP or configuration error aborts before anything changes; an empty group or
more than SYNC_MAX_REMOVALS (default 2) removals is refused without --force; units are installed only
after Podman's Quadlet generator accepted them, the other generated files atomically with the previous
versions kept as *.prev; runs never overlap (flock). Running it again without FreeIPA changes changes
nothing.

Users whose uid is not a safe name (^[a-z][a-z0-9-]{0,30}$), whose uidNumber/gidNumber is missing or 0,
or whose profile directory is owned by someone else are not provisioned and are reported; the others
still are (exit status 2).

Run as root (it chowns profile directories to the users' uidNumbers and drives rootful Podman and
systemd) from a root-owned checkout.

Usage:
  sync-freeipa-users.py [--env-file FILE] [--dry-run | --generate-only | --attach-only] [--force]

Exit status: 0 ok, 1 aborted (nothing changed) or apply error, 2 applied but some users skipped.

AUTH_BACKEND=file (unit tests only): users come from an Authelia-style users file (AUTHELIA_USERS_FILE),
all with DEMO_UID/DEMO_GID, eligible when in the group and not disabled.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rb_units  # noqa: E402
from freeipa_common import (  # noqa: E402
    REPO_DIR,
    UID_RE,
    FreeIPAError,
    connect,
    load_config,
    read_env_file,
    resolve_members,
)
from rb_units import BROWSER_PREFIX, NETWORK_PREFIX, NGINX, USER_LABEL, BrowserConfig, UnitError  # noqa: E402

SUBNET_PREFIX = 28  # nginx + one browser per network
STATE_VERSION = 1

# Keys read from the env file; the process environment overrides them.
SETTING_KEYS = (
    "AUTH_BACKEND", "FREEIPA_BROWSER_GROUP", "QUADLET_DIR", "GENERATED_DIR", "NGINX_GENERATED_DIR",
    "USER_DATA_ROOT", "BROWSER_NET_POOL", "SYNC_MAX_REMOVALS", "AUTHELIA_USERS_FILE", "DEMO_UID", "DEMO_GID",
    "BROWSER_IMAGE", "TZ", "PUBLIC_HOST", "PUBLIC_PORT", "BROWSER_FILE_TRANSFERS",
)


class SyncError(Exception):
    """Abort: nothing has been changed."""


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --- settings ---------------------------------------------------------------------------------

def repo_path(value: str) -> Path:
    """Relative paths in .env are relative to the repository."""
    path = Path(value)
    return path if path.is_absolute() else REPO_DIR / path


@dataclass(frozen=True)
class Settings:
    env_file: Path
    backend: str
    group: str
    quadlet_dir: Path
    generated_dir: Path
    nginx_generated_dir: Path
    user_data_root: Path
    pool: ipaddress.IPv4Network
    max_removals: int
    users_file: Path | None
    demo_ids: tuple[int, int] | None
    # Rendered into every browser unit; empty image only for --dry-run / --attach-only.
    browser: BrowserConfig

    @property
    def state_file(self) -> Path:
        return self.generated_dir / "state.json"

    @property
    def map_file(self) -> Path:
        return self.nginx_generated_dir / "users.map"

    @property
    def map_loaded_file(self) -> Path:
        """Hash of the map Nginx last loaded: reload only when it differs. Every reload leaves the old
        workers running until their WebSockets close, so reloading on every run would pile them up."""
        return self.generated_dir / ".map-loaded"


def load_settings(env_file: Path) -> Settings:
    if not env_file.is_file():
        raise SyncError(f"missing {env_file} (copy .env.example)")
    env = read_env_file(env_file)
    env.update({k: os.environ[k] for k in SETTING_KEYS if k in os.environ})

    def get(key: str, default: str = "") -> str:
        return env.get(key, "").strip() or default

    backend = get("AUTH_BACKEND", "freeipa")
    if backend not in ("freeipa", "file"):
        raise SyncError(f"AUTH_BACKEND must be freeipa or file, not {backend!r}")

    try:
        pool = ipaddress.ip_network(get("BROWSER_NET_POOL", "10.213.0.0/16"))
    except ValueError as exc:
        raise SyncError(f"invalid BROWSER_NET_POOL: {exc}") from exc
    if pool.version != 4 or pool.prefixlen > SUBNET_PREFIX:
        raise SyncError(f"BROWSER_NET_POOL must be an IPv4 network of /{SUBNET_PREFIX} or larger")

    try:
        max_removals = int(get("SYNC_MAX_REMOVALS", "2"))
    except ValueError as exc:
        raise SyncError("SYNC_MAX_REMOVALS must be a number") from exc

    users_file = demo_ids = None
    if backend == "file":
        users_file = repo_path(get("AUTHELIA_USERS_FILE", "./users.yml"))
        try:
            demo_ids = (int(get("DEMO_UID")), int(get("DEMO_GID")))
        except ValueError as exc:
            raise SyncError("AUTH_BACKEND=file needs DEMO_UID and DEMO_GID") from exc

    user_data_root = repo_path(get("USER_DATA_ROOT", "./local/users"))
    try:
        browser = BrowserConfig(
            image=rb_units.safe("BROWSER_IMAGE", get("BROWSER_IMAGE")),
            tz=rb_units.safe("TZ", get("TZ", "UTC")),
            public_host=rb_units.safe("PUBLIC_HOST", get("PUBLIC_HOST")),
            public_port=rb_units.safe("PUBLIC_PORT", get("PUBLIC_PORT", "443")),
            file_transfers=rb_units.safe("BROWSER_FILE_TRANSFERS", get("BROWSER_FILE_TRANSFERS")),
            user_data_root=Path(rb_units.safe("USER_DATA_ROOT", str(user_data_root))),
        )
    except UnitError as exc:
        raise SyncError(str(exc)) from exc

    return Settings(
        env_file=env_file,
        backend=backend,
        group=get("FREEIPA_BROWSER_GROUP", "remote-browser-users"),
        quadlet_dir=Path(get("QUADLET_DIR", rb_units.QUADLET_DIR)),
        generated_dir=repo_path(get("GENERATED_DIR", "./generated")),
        nginx_generated_dir=repo_path(get("NGINX_GENERATED_DIR", "./nginx/generated")),
        user_data_root=user_data_root,
        pool=pool,
        max_removals=max_removals,
        users_file=users_file,
        demo_ids=demo_ids,
        browser=browser,
    )


# --- users ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class User:
    uid: str
    uid_number: int
    gid_number: int


@dataclass
class Candidate:
    """A direct group member that passed the account filter, before name/number checks."""
    uid: str | None
    uid_number: int | None
    gid_number: int | None
    source: str  # DN or file entry, for messages
    problems: list[str] = field(default_factory=list)


def ldap_candidates(settings: Settings) -> list[Candidate]:
    """Eligible direct members. Raises FreeIPAError on any LDAP problem: never 'zero members'."""
    cfg = load_config(settings.env_file)
    conn = connect(cfg)
    try:
        members, nested = resolve_members(conn, cfg)
    finally:
        conn.unbind()
    for dn in nested:
        log(f"note: nested group {dn} ignored (direct membership only, as in Authelia)")
    result = []
    for m in members:
        if not m.eligible_by_filter:
            # Locked, expired, or not a person: Authelia denies the login, so no browser either.
            why = "; ".join(m.problems) or "locked, expired or not a person"
            log(f"skip: {m.uid or m.dn} may not log in ({why})")
            continue
        # resolve_members' problems (uid format, numbers) are re-checked by select_users.
        result.append(Candidate(m.uid, m.uid_number, m.gid_number, m.dn))
    return result


def file_candidates(settings: Settings) -> list[Candidate]:
    """Tests: Authelia file backend users in the group and not disabled."""
    try:
        import yaml
    except ImportError as exc:
        raise SyncError("AUTH_BACKEND=file needs PyYAML (python3-yaml)") from exc
    try:
        data = yaml.safe_load(settings.users_file.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise SyncError(f"cannot read {settings.users_file}: {exc}") from exc
    users = (data or {}).get("users")
    if not isinstance(users, dict):
        raise SyncError(f"{settings.users_file} has no 'users' mapping")
    uid_number, gid_number = settings.demo_ids
    result = []
    for name, entry in users.items():
        entry = entry or {}
        if settings.group not in (entry.get("groups") or []):
            continue
        if entry.get("disabled"):
            log(f"skip: {name} is disabled")
            continue
        result.append(Candidate(str(name), uid_number, gid_number, f"{settings.users_file.name}:{name}"))
    return result


def select_users(candidates: list[Candidate], shared_ids_ok: bool = False) -> tuple[list[User], list[str]]:
    """Validate names and numbers; refuse ambiguity instead of guessing. Returns (users, errors).

    shared_ids_ok: the file backend (tests) runs every browser with the same uid on purpose."""
    errors: list[str] = []
    by_source: dict[str, Candidate] = {}
    for c in candidates:
        by_source.setdefault(c.source.lower(), c)  # the same entry returned twice
    by_uid: dict[str, list[Candidate]] = {}
    for c in by_source.values():
        if not c.uid or not UID_RE.fullmatch(c.uid):
            errors.append(f"{c.uid or c.source}: uid is not a safe name ({UID_RE.pattern}); not provisioned")
        elif not c.uid_number or not c.gid_number:
            # 0 is root; None is missing/unreadable.
            errors.append(f"{c.uid}: uidNumber/gidNumber missing, unreadable or 0; not provisioned")
        else:
            by_uid.setdefault(c.uid, []).append(c)

    users: list[User] = []
    for uid, cs in by_uid.items():
        if len(cs) > 1:
            errors.append(f"{uid}: several directory entries ({', '.join(c.source for c in cs)}); not provisioned")
        else:
            users.append(User(uid, cs[0].uid_number, cs[0].gid_number))
    owners: dict[int, list[str]] = {}
    for u in users:
        owners.setdefault(u.uid_number, []).append(u.uid)
    shared = {} if shared_ids_ok else {n: uids for n, uids in owners.items() if len(uids) > 1}
    for n, uids in shared.items():
        errors.append(f"{', '.join(uids)}: share uidNumber {n}, their profiles would not be separated; "
                      "not provisioned")
    users = [u for u in users if u.uid_number not in shared]
    users.sort(key=lambda u: u.uid)
    return users, errors


# --- profile directories ----------------------------------------------------------------------

def user_dirs(root: Path, user: User) -> tuple[Path, Path]:
    return root / user.uid, root / user.uid / "config"


def check_user_dir(root: Path, user: User) -> str | None:
    """Problem with an existing USER_DATA_ROOT/<uid>[/config], or None (missing ones are created later).

    Never changes the owner of an existing directory: a mismatch (e.g. a changed uidNumber) needs an
    administrator."""
    for path in user_dirs(root, user):
        try:
            st = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(st.st_mode):
            return f"{path} is not a directory"
        if st.st_uid != user.uid_number:
            return (f"{path} is owned by uid {st.st_uid}, not the user's uidNumber {user.uid_number}; "
                    f"fix it by hand (chown -R {user.uid_number}:{user.gid_number} {root / user.uid})")
    return None


def create_user_dir(root: Path, user: User) -> None:
    """Create missing directories owned by the user, mode 0700; tighten a too-open <uid> directory."""
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    for path in user_dirs(root, user):
        if path.exists():
            continue
        try:
            path.mkdir(mode=0o700)
            os.chmod(path, 0o700)
            os.chown(path, user.uid_number, user.gid_number)
        except PermissionError as exc:
            if path.exists():
                path.rmdir()
            raise SyncError(f"cannot create {path} owned by {user.uid_number}:{user.gid_number} "
                            "(run the sync as root)") from exc
        log(f"created {path}")
    home = root / user.uid
    if home.stat().st_mode & 0o077:
        os.chmod(home, 0o700)
        log(f"chmod 0700 {home}")


# --- subnets ----------------------------------------------------------------------------------

def allocate_subnets(users: list[User], previous: dict[str, str], pool: ipaddress.IPv4Network,
                     in_use: list[ipaddress.IPv4Network]) -> dict[str, str]:
    """Keep each user's subnet; give new users the lowest free /28 of the pool.

    in_use: subnets of Podman networks other than these users' own networks."""
    def free(net: ipaddress.IPv4Network, taken: list[ipaddress.IPv4Network]) -> bool:
        return not any(net.overlaps(t) for t in taken)

    taken = list(in_use)
    result: dict[str, str] = {}
    for u in users:
        old = previous.get(u.uid)
        if old:
            net = ipaddress.ip_network(old)
            if net.subnet_of(pool) and free(net, taken):
                result[u.uid] = old
                taken.append(net)
    for u in users:
        if u.uid in result:
            continue
        for net in pool.subnets(new_prefix=SUBNET_PREFIX):
            if free(net, taken):
                result[u.uid] = str(net)
                taken.append(net)
                break
        else:
            raise SyncError(f"BROWSER_NET_POOL {pool} has no free /{SUBNET_PREFIX} left")
    return result


# --- rendering --------------------------------------------------------------------------------

def render_units(settings: Settings, users: list[User], subnets: dict[str, str]) -> dict[str, str]:
    """Every generated unit file in QUADLET_DIR, by file name."""
    units = {}
    for u in users:
        units[f"{NETWORK_PREFIX}{u.uid}.network"] = rb_units.user_network(u.uid, subnets[u.uid])
        units[f"{BROWSER_PREFIX}{u.uid}.container"] = rb_units.browser_container(
            u.uid, u.uid_number, u.gid_number, settings.browser)
    return units


def generated_unit_files(quadlet_dir: Path) -> dict[str, str]:
    """The sync's own files in QUADLET_DIR (rb-frontend.network and rb-nginx.container are not)."""
    if not quadlet_dir.is_dir():
        return {}
    result = {}
    for pattern in (f"{BROWSER_PREFIX}*.container", f"{NETWORK_PREFIX}*.network"):
        for p in quadlet_dir.glob(pattern):
            uid = p.name.removeprefix(BROWSER_PREFIX).removeprefix(NETWORK_PREFIX).rsplit(".", 1)[0]
            if UID_RE.fullmatch(uid):
                result[p.name] = p.read_text()
    return result


def unit_uid(name: str) -> str:
    return name.removeprefix(BROWSER_PREFIX).removeprefix(NETWORK_PREFIX).rsplit(".", 1)[0]


def render_map(users: list[User], group: str) -> str:
    out = [f"# Generated by scripts/sync-freeipa-users.py from the FreeIPA group {group}. Do not edit."]
    out += [f'"{u.uid}" "{BROWSER_PREFIX}{u.uid}:3000";' for u in users]
    return "\n".join(out) + "\n"


def render_state(users: list[User], subnets: dict[str, str]) -> str:
    state = {
        "version": STATE_VERSION,
        "users": {u.uid: {"uid_number": u.uid_number, "gid_number": u.gid_number, "subnet": subnets[u.uid]}
                  for u in users},
    }
    return json.dumps(state, indent=2, sort_keys=True) + "\n"


def read_state(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text())
        users = state["users"]
        assert state.get("version") == STATE_VERSION and isinstance(users, dict)
    except (OSError, ValueError, KeyError, AssertionError) as exc:
        raise SyncError(f"{path} is unreadable ({exc}); restore {path.name}.prev or fix it") from exc
    return users


# --- files ------------------------------------------------------------------------------------

def map_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest() + "\n"


def read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def install(path: Path, content: str, mode: int = 0o644) -> None:
    """Atomic replace; the previous version is kept as <name>.prev for rollback."""
    old = read_text(path)
    if old is not None:
        atomic_write(path.with_name(path.name + ".prev"), old, mode)
    atomic_write(path, content, mode)


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- podman and systemd ------------------------------------------------------------------------

class EngineError(Exception):
    pass


class Podman:
    """The few Podman and systemd operations the sync needs. Replaced by a fake in tests/test_sync.py."""

    def __init__(self, settings: Settings):
        self.s = settings

    def _run(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(args, capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise EngineError(f"{' '.join(args[:4])} ...: {(proc.stderr or proc.stdout).strip()}")
        return proc.stdout

    def validate(self, units: dict[str, str]) -> None:
        """Podman's Quadlet generator over the new units; raises EngineError if it rejects them."""
        try:
            rb_units.validate(units)
        except UnitError as exc:
            raise EngineError(str(exc)) from exc

    def networks(self) -> list[dict]:
        """All Podman networks: name, remote-browser user label, IPv4 subnets, container names."""
        names = self._run("podman", "network", "ls", "--format", "{{.Name}}").split()
        if not names:
            return []
        result = []
        for n in json.loads(self._run("podman", "network", "inspect", *names)):
            result.append({
                "name": n["name"],
                "user": (n.get("labels") or {}).get(USER_LABEL),
                "subnets": [s["subnet"] for s in n.get("subnets") or []],
                "containers": sorted(c.get("name", "") for c in (n.get("containers") or {}).values()),
            })
        return result

    def nginx_running(self) -> bool:
        out = self._run("podman", "container", "inspect", "--format", "{{.State.Running}}", NGINX, check=False)
        return out.strip() == "true"

    def nginx_test(self) -> tuple[bool, str]:
        proc = subprocess.run(["podman", "exec", NGINX, "nginx", "-t"], capture_output=True, text=True)
        return proc.returncode == 0, (proc.stderr or proc.stdout).strip()

    def nginx_reload(self) -> None:
        self._run("podman", "exec", NGINX, "nginx", "-s", "reload")

    def daemon_reload(self) -> None:
        self._run("systemctl", "daemon-reload")

    def stop(self, units: list[str]) -> None:
        self._run("systemctl", "stop", *units)

    def try_restart(self, units: list[str]) -> None:
        self._run("systemctl", "try-restart", *units)

    def start(self, units: list[str]) -> None:
        self._run("systemctl", "start", *units)

    def connect(self, network: str) -> None:
        self._run("podman", "network", "connect", network, NGINX)

    def disconnect(self, network: str) -> None:
        self._run("podman", "network", "disconnect", network, NGINX)

    def remove_network(self, network: str) -> None:
        # Never -f: that would remove the containers on it, i.e. Nginx.
        self._run("podman", "network", "rm", network)

# --- the sync ---------------------------------------------------------------------------------

@dataclass
class Plan:
    users: list[User]
    errors: list[str]
    added: list[str]
    removed: list[str]
    files: dict[Path, str]  # map and state: path -> new content
    changed: list[Path]
    units: dict[str, str]  # every generated unit: file name -> content
    units_changed: list[str]  # new or different
    units_stale: list[str]  # installed, no longer wanted


def make_plan(settings: Settings, candidates: list[Candidate], engine: Podman, force: bool) -> Plan:
    """Decide everything without changing anything."""
    users, errors = select_users(candidates, shared_ids_ok=settings.backend == "file")

    ok_users = []
    for u in users:
        problem = check_user_dir(settings.user_data_root, u)
        if problem:
            errors.append(f"{u.uid}: {problem}; not provisioned")
        else:
            ok_users.append(u)
    users = ok_users
    for e in errors:
        warn(f"ERROR: {e}")

    previous = read_state(settings.state_file)
    names = {u.uid for u in users}
    added = sorted(names - previous.keys())
    removed = sorted(previous.keys() - names)
    if not users and not force:
        raise SyncError("no user would have a browser (empty group, or every member skipped); "
                        "refusing without --force")
    if len(removed) > settings.max_removals and not force:
        raise SyncError(f"would remove {len(removed)} users ({', '.join(removed)}), more than "
                        f"SYNC_MAX_REMOVALS={settings.max_removals}; refusing without --force")
    if users and not settings.browser.image:
        raise SyncError("BROWSER_IMAGE is not set (scripts/build-browser-image.sh prints the tag)")
    if users and not settings.browser.public_host:
        raise SyncError("PUBLIC_HOST is not set")

    own = {rb_units.network_name(u.uid) for u in users}
    in_use = []
    for n in engine.networks():
        if n["name"] in own:
            continue
        in_use += [ipaddress.ip_network(s) for s in n["subnets"] if ipaddress.ip_network(s).version == 4]
    subnets = allocate_subnets(users, {uid: v.get("subnet") for uid, v in previous.items()}, settings.pool,
                               in_use)

    files = {
        settings.map_file: render_map(users, settings.group),
        settings.state_file: render_state(users, subnets),
    }
    changed = [p for p, content in files.items() if read_text(p) != content]
    units = render_units(settings, users, subnets)
    installed = generated_unit_files(settings.quadlet_dir)
    units_changed = sorted(n for n, c in units.items() if installed.get(n) != c)
    units_stale = sorted(installed.keys() - units.keys())
    return Plan(users, errors, added, removed, files, changed, units, units_changed, units_stale)


def validate_units(plan: Plan, engine: Podman) -> None:
    try:
        engine.validate(plan.units)
    except EngineError as exc:
        raise SyncError(f"generated units rejected by Podman's Quadlet generator, nothing applied: {exc}") from exc


def apply(settings: Settings, plan: Plan, engine: Podman) -> list[str]:
    """Make Podman and systemd match the plan. Returns errors (empty: everything applied)."""
    errors: list[str] = []
    wanted = {u.uid for u in plan.users}
    qdir = settings.quadlet_dir

    # 1. Browsers of users who lost access: stop them first, this ends their open streams (plan §17).
    stale_uids = sorted({unit_uid(n) for n in plan.units_stale} - wanted)
    if stale_uids:
        for uid in stale_uids:
            log(f"stop {BROWSER_PREFIX}{uid}")
        engine.stop([u for uid in stale_uids for u in (rb_units.browser_unit(uid), rb_units.network_unit(uid))])

    # 2. Units. Removing a unit file never deletes the network itself (step 6) or the profile.
    if plan.units_changed or plan.units_stale:
        qdir.mkdir(mode=0o755, parents=True, exist_ok=True)
        for name in plan.units_changed:
            atomic_write(qdir / name, plan.units[name])
            log(f"wrote {qdir / name}")
        for name in plan.units_stale:
            (qdir / name).unlink(missing_ok=True)
            log(f"removed {qdir / name}")
        engine.daemon_reload()

    # 3. Routes. The map is already installed; test it before reloading.
    nginx = engine.nginx_running()
    if not nginx:
        errors.append(f"{NGINX} is not running: systemctl start {NGINX}, then run the sync again")
    elif read_text(settings.map_loaded_file) != map_hash(settings.map_file):
        ok, out = engine.nginx_test()
        if ok:
            engine.nginx_reload()
            atomic_write(settings.map_loaded_file, map_hash(settings.map_file))
            log("nginx reloaded")
        else:
            prev = settings.map_file.with_name(settings.map_file.name + ".prev")
            if prev.exists():
                atomic_write(settings.map_file, prev.read_text())
            else:
                settings.map_file.unlink(missing_ok=True)
            errors.append(f"nginx -t failed, previous map restored, routes unchanged: {out}")

    # 4. Browsers: restart those whose unit changed (a new BROWSER_IMAGE ends their streams), then
    #    make sure every one runs (starts new ones and their networks; a no-op for running ones).
    restart = sorted(rb_units.browser_unit(unit_uid(n)) for n in plan.units_changed
                     if n.endswith(".container") and unit_uid(n) not in plan.added)
    if restart:
        engine.try_restart(restart)
        log(f"restarted {' '.join(restart)}")
    if wanted:
        engine.start(sorted(rb_units.browser_unit(uid) for uid in wanted))

    # 5./6. Nginx on exactly the user networks. Only networks with the sync's label are ever removed.
    if nginx:
        errors += attach(engine, wanted)
    for n in engine.networks():
        if n["user"] is None or n["user"] in wanted or not n["name"].startswith(NETWORK_PREFIX):
            continue
        if NGINX in n["containers"]:
            engine.disconnect(n["name"])
        if [c for c in n["containers"] if c != NGINX]:
            errors.append(f"network {n['name']} is no longer used but still has containers; not removed")
            continue
        engine.remove_network(n["name"])
        log(f"removed network {n['name']}")
    return errors


def attach(engine: Podman, uids: set[str]) -> list[str]:
    """Nginx onto each of these users' networks it is not on yet."""
    errors = []
    present = {n["name"]: n for n in engine.networks()}
    for uid in sorted(uids):
        name = rb_units.network_name(uid)
        net = present.get(name)
        if net is None:
            errors.append(f"network {name} does not exist (systemctl status {rb_units.network_unit(uid)})")
        elif NGINX not in net["containers"]:
            try:
                engine.connect(name)
                log(f"nginx attached to {name}")
            except EngineError as exc:
                errors.append(f"cannot attach nginx to {name}: {exc}")
    return errors


def attach_only(settings: Settings, engine: Podman) -> int:
    """After Nginx (re)started: join the networks of the users in state.json. No lock: connecting is
    idempotent, and waiting for a running sync would hold up Nginx's start."""
    uids = set(read_state(settings.state_file))
    if not uids:
        return 0
    try:
        engine.start(sorted(rb_units.network_unit(uid) for uid in uids))
    except EngineError as exc:
        warn(f"ERROR: {exc}")
    errors = attach(engine, uids)
    for e in errors:
        warn(f"ERROR: {e}")
    return 1 if errors else 0


def run(settings: Settings, args: argparse.Namespace, engine: Podman | None = None,
        candidates: list[Candidate] | None = None) -> int:
    engine = engine or Podman(settings)
    if getattr(args, "attach_only", False):
        return attach_only(settings, engine)
    settings.generated_dir.mkdir(parents=True, exist_ok=True)
    settings.nginx_generated_dir.mkdir(parents=True, exist_ok=True)

    with open(settings.generated_dir / ".sync.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)

        if candidates is None:
            try:
                candidates = ldap_candidates(settings) if settings.backend == "freeipa" else file_candidates(settings)
            except FreeIPAError as exc:
                raise SyncError(f"{exc}") from exc

        try:
            plan = make_plan(settings, candidates, engine, force=args.force)
        except EngineError as exc:
            raise SyncError(f"podman: {exc}") from exc
        log(f"{len(plan.users)} user(s): {' '.join(u.uid for u in plan.users) or '-'}")
        if plan.added:
            log(f"added:   {' '.join(plan.added)}")
        if plan.removed:
            log(f"removed: {' '.join(plan.removed)} (profile directories are kept)")

        if args.dry_run:
            for p in plan.changed:
                log(f"would write {p}")
            for n in plan.units_changed:
                log(f"would write {settings.quadlet_dir / n}")
            for n in plan.units_stale:
                log(f"would remove {settings.quadlet_dir / n}")
            log("dry run: nothing changed")
            return 2 if plan.errors else 0

        validate_units(plan, engine)
        if not args.generate_only:
            for u in plan.users:
                create_user_dir(settings.user_data_root, u)
        for path in plan.changed:
            install(path, plan.files[path])
            log(f"wrote {path}")
        if not plan.changed and not plan.units_changed and not plan.units_stale:
            log("generated files unchanged")
        if args.generate_only:
            return 2 if plan.errors else 0

        try:
            apply_errors = apply(settings, plan, engine)
        except EngineError as exc:
            warn(f"ERROR: applying failed: {exc}")
            return 1
        for e in apply_errors:
            warn(f"ERROR: {e}")
        if apply_errors:
            return 1
        return 2 if plan.errors else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", type=Path, default=REPO_DIR / ".env")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    mode.add_argument("--generate-only", action="store_true",
                      help="write map and state only; touch neither units, Podman nor profile directories")
    mode.add_argument("--attach-only", action="store_true",
                      help="attach Nginx to the networks of the users in state.json (rb-nginx.service)")
    ap.add_argument("--force", action="store_true",
                    help="allow an empty result or more than SYNC_MAX_REMOVALS removals")
    args = ap.parse_args(argv)
    try:
        settings = load_settings(args.env_file.resolve())
        return run(settings, args)
    except SyncError as exc:
        warn(f"ABORT: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
