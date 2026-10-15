#!/usr/bin/env python3
"""Provision one browser container per eligible FreeIPA user (plan §14-17, §32-35).

Reads the direct members of FREEIPA_BROWSER_GROUP that pass Authelia's FreeIPA account filter
(freeipa_common.py) and makes the Docker host match:

  GENERATED_DIR/compose.users.yaml   service browser-<uid> + network net-<uid> per user
  GENERATED_DIR/state.json           uidNumber/gidNumber/subnet per user (subnets stay put)
  NGINX_GENERATED_DIR/users.map      "<uid>" "browser-<uid>:3000"; whitelist for Nginx
  USER_DATA_ROOT/<uid>/config        created owned by uidNumber:gidNumber, mode 0700; never deleted

then, unless --generate-only / --dry-run:
  1. stops and removes browser containers of users who are no longer eligible (ends their streams);
  2. reloads Nginx after `nginx -t` (the previous map is restored if the test fails);
  3. `docker compose up -d` for every user's browser (starts new ones, applies BROWSER_IMAGE changes);
  4. attaches Nginx to every user network, live: putting the networks into Nginx's Compose definition
     would make Compose recreate Nginx, and drop every open stream, whenever a user is added;
  5. detaches Nginx from and removes networks no longer in the project.

Fail-safe (plan §32): any LDAP or configuration error aborts before anything changes; an empty group or
more than SYNC_MAX_REMOVALS (default 2) removals is refused without --force; generated files are written
atomically after `docker compose config` accepted them, previous versions kept as *.prev; runs never
overlap (flock). Running it again without FreeIPA changes changes nothing.

Users whose uid is not a safe name (^[a-z][a-z0-9-]{0,30}$), whose uidNumber/gidNumber is missing or 0,
or whose profile directory is owned by someone else are not provisioned and are reported; the others
still are (exit status 2).

Run as root (it chowns profile directories to the users' uidNumbers) from a root-owned checkout, after
the stack is up. After `docker compose up -d` recreated Nginx, run it again: a recreated Nginx is on no
user network until then.

Usage:
  sync-freeipa-users.py [--env-file FILE] [--dry-run | --generate-only] [--force]

Exit status: 0 ok, 1 aborted (nothing changed) or apply error, 2 applied but some users skipped.

AUTH_BACKEND=file (the local demo): users come from Authelia's users file (AUTHELIA_USERS_FILE), all
with DEMO_UID/DEMO_GID, eligible when in the group and not disabled, as the demo Authelia decides.
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
from freeipa_common import (  # noqa: E402
    REPO_DIR,
    UID_RE,
    FreeIPAError,
    connect,
    load_config,
    read_env_file,
    resolve_members,
)

SUBNET_PREFIX = 28  # nginx + one browser per network
STATE_VERSION = 1
BROWSER_PREFIX = "browser-"
NETWORK_PREFIX = "net-"
# Compose's own separator for COMPOSE_FILE on Linux.
COMPOSE_PATH_SEPARATOR = ":"
PHASE2_FILE = "compose.browser-test.yaml"
PHASE2_NETWORK = "browser-test"

# Keys read from the env file; the process environment overrides them, as with docker compose.
SETTING_KEYS = (
    "AUTH_BACKEND", "FREEIPA_BROWSER_GROUP", "COMPOSE_PROJECT_NAME", "COMPOSE_FILE", "GENERATED_DIR",
    "NGINX_GENERATED_DIR", "USER_DATA_ROOT", "BROWSER_NET_POOL", "SYNC_MAX_REMOVALS",
    "AUTHELIA_USERS_FILE", "DEMO_UID", "DEMO_GID",
)


class SyncError(Exception):
    """Abort: nothing has been changed."""


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --- settings ---------------------------------------------------------------------------------

def repo_path(value: str) -> Path:
    """Relative paths are relative to the repository, as for Compose bind mounts."""
    path = Path(value)
    return path if path.is_absolute() else REPO_DIR / path


@dataclass(frozen=True)
class Settings:
    env_file: Path
    backend: str
    group: str
    project: str
    compose_files: tuple[Path, ...]
    generated_dir: Path
    nginx_generated_dir: Path
    user_data_root: Path
    pool: ipaddress.IPv4Network
    max_removals: int
    users_file: Path | None
    demo_ids: tuple[int, int] | None

    @property
    def compose_out(self) -> Path:
        return self.generated_dir / "compose.users.yaml"

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

    generated_dir = repo_path(get("GENERATED_DIR", "./generated"))
    compose_out = generated_dir / "compose.users.yaml"
    files_value = get("COMPOSE_FILE", f"compose.yaml{COMPOSE_PATH_SEPARATOR}{compose_out}")
    files = tuple(repo_path(f) for f in files_value.split(COMPOSE_PATH_SEPARATOR) if f)
    resolved = [f.resolve() for f in files]
    if compose_out.resolve() not in resolved:
        raise SyncError(f"COMPOSE_FILE ({files_value}) must include {compose_out}, or docker compose would "
                        "not see the generated browser services")
    if any(f.name == PHASE2_FILE for f in files):
        raise SyncError(f"COMPOSE_FILE still includes {PHASE2_FILE} (Phase 2). Replace it with {compose_out} "
                        "(README, 'Provisioning'); the sync then removes the test container.")

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
        users_file = repo_path(get("AUTHELIA_USERS_FILE", "./demo/users.yml"))
        try:
            demo_ids = (int(get("DEMO_UID")), int(get("DEMO_GID")))
        except ValueError as exc:
            raise SyncError("AUTH_BACKEND=file needs DEMO_UID and DEMO_GID (scripts/demo.sh sets them)") from exc

    return Settings(
        env_file=env_file,
        backend=backend,
        group=get("FREEIPA_BROWSER_GROUP", "remote-browser-users"),
        project=get("COMPOSE_PROJECT_NAME", "remote-browser"),
        compose_files=files,
        generated_dir=generated_dir,
        nginx_generated_dir=repo_path(get("NGINX_GENERATED_DIR", "./nginx/generated")),
        user_data_root=repo_path(get("USER_DATA_ROOT", "./local/users")),
        pool=pool,
        max_removals=max_removals,
        users_file=users_file,
        demo_ids=demo_ids,
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
    """Demo: Authelia file backend users in the group and not disabled."""
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

    shared_ids_ok: the demo runs every browser with the same uid on purpose."""
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

    in_use: subnets of Docker networks other than these users' own networks."""
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

def render_compose(users: list[User], subnets: dict[str, str], group: str) -> str:
    """Values are validated (uid regex, integers, subnets), so plain formatting is safe."""
    out = [
        f"# Generated by scripts/sync-freeipa-users.py from the FreeIPA group {group}. Do not edit:",
        "# the next sync overwrites it. Nginx is attached to the networks by the sync, not here.",
    ]
    if not users:
        out += ["services: {}", ""]
        return "\n".join(out)
    out.append("services:")
    for u in users:
        out += [
            f"  {BROWSER_PREFIX}{u.uid}:",
            "    image: ${BROWSER_IMAGE:?set BROWSER_IMAGE in .env}",
            "    # Stable hostname: Chrome and Firefox profile locks embed it (plan §24).",
            f"    hostname: {BROWSER_PREFIX}{u.uid}",
            "    restart: unless-stopped",
            "    environment:",
            f'      PUID: "{u.uid_number}"',
            f'      PGID: "{u.gid_number}"',
            "      TZ: ${TZ:-UTC}",
            f"      REMOTE_BROWSER_USER: {u.uid}",
            "      SELKIES_FILE_TRANSFERS: ${BROWSER_FILE_TRANSFERS:-}",
            "      SELKIES_ALLOWED_ORIGINS: https://${PUBLIC_HOST},https://${PUBLIC_HOST}:${PUBLIC_PORT:-443}",
            "    volumes:",
            f"      - ${{USER_DATA_ROOT:-./local/users}}/{u.uid}/config:/config",
            "    expose:",
            '      - "3000"',
            "    shm_size: 1gb",
            "    networks:",
            f"      - {NETWORK_PREFIX}{u.uid}",
            "    security_opt:",
            "      - seccomp=./browser-image/seccomp-chrome.json",
        ]
    out.append("networks:")
    for u in users:
        out += [
            f"  {NETWORK_PREFIX}{u.uid}:",
            "    enable_ipv6: false",
            "    ipam:",
            "      config:",
            f"        - subnet: {subnets[u.uid]}",
        ]
    out.append("")
    return "\n".join(out)


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


# --- docker -----------------------------------------------------------------------------------

class DockerError(Exception):
    pass


class Docker:
    """The few Docker operations the sync needs. Replaced by a fake in tests/test_sync.py."""

    def __init__(self, settings: Settings):
        self.s = settings

    def _run(self, *args: str, check: bool = True) -> str:
        # cwd: Compose resolves the seccomp profile path against the working directory.
        proc = subprocess.run(args, capture_output=True, text=True, cwd=REPO_DIR)
        if check and proc.returncode != 0:
            raise DockerError(f"{' '.join(args[:4])} ...: {(proc.stderr or proc.stdout).strip()}")
        return proc.stdout

    def _compose(self, files: tuple[Path, ...], *args: str) -> str:
        cmd = ["docker", "compose", "--project-directory", str(REPO_DIR), "--env-file", str(self.s.env_file),
               "-p", self.s.project]
        for f in files:
            cmd += ["-f", str(f)]
        return self._run(*cmd, *args)

    def model(self, files: tuple[Path, ...]) -> dict:
        """`docker compose config` of these files: validates them and returns the project model."""
        return json.loads(self._compose(files, "config", "--format", "json"))

    def networks(self) -> list[dict]:
        """All Docker networks: name, compose project/network labels, IPv4 subnets."""
        ids = self._run("docker", "network", "ls", "-q").split()
        if not ids:
            return []
        result = []
        for n in json.loads(self._run("docker", "network", "inspect", *ids)):
            labels = n.get("Labels") or {}
            result.append({
                "name": n["Name"],
                "project": labels.get("com.docker.compose.project"),
                "key": labels.get("com.docker.compose.network"),
                "subnets": [c["Subnet"] for c in (n.get("IPAM") or {}).get("Config") or [] if "Subnet" in c],
                "containers": sorted((n.get("Containers") or {}).keys()),
            })
        return result

    def project_containers(self) -> list[tuple[str, str]]:
        """(id, service) of every container of the project, running or not."""
        out = self._run("docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={self.s.project}",
                        "--format", '{{.ID}}\t{{.Label "com.docker.compose.service"}}')
        return [tuple(line.split("\t", 1)) for line in out.splitlines() if "\t" in line]

    def running_nginx(self) -> str | None:
        out = self._run("docker", "ps", "-q", "--no-trunc",
                        "--filter", f"label=com.docker.compose.project={self.s.project}",
                        "--filter", "label=com.docker.compose.service=nginx")
        ids = out.split()
        return ids[0] if len(ids) == 1 else None

    def remove_container(self, cid: str) -> None:
        self._run("docker", "stop", "-t", "10", cid)
        self._run("docker", "rm", cid)

    def nginx_test(self, cid: str) -> tuple[bool, str]:
        proc = subprocess.run(["docker", "exec", cid, "nginx", "-t"], capture_output=True, text=True)
        return proc.returncode == 0, (proc.stderr or proc.stdout).strip()

    def nginx_reload(self, cid: str) -> None:
        self._run("docker", "exec", cid, "nginx", "-s", "reload")

    def up(self, services: list[str]) -> None:
        self._compose(self.s.compose_files, "up", "-d", "--no-deps", "--quiet-pull", *services)

    def connect(self, network: str, cid: str) -> None:
        self._run("docker", "network", "connect", network, cid)

    def disconnect(self, network: str, cid: str) -> None:
        self._run("docker", "network", "disconnect", network, cid)

    def remove_network(self, network: str) -> None:
        self._run("docker", "network", "rm", network)


# --- the sync ---------------------------------------------------------------------------------

@dataclass
class Plan:
    users: list[User]
    errors: list[str]
    added: list[str]
    removed: list[str]
    files: dict[Path, str]  # path -> new content
    changed: list[Path]


def make_plan(settings: Settings, candidates: list[Candidate], docker: Docker, force: bool) -> Plan:
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

    own = {f"{NETWORK_PREFIX}{u.uid}" for u in users}
    in_use = []
    for n in docker.networks():
        if n["project"] == settings.project and n["key"] in own:
            continue
        in_use += [ipaddress.ip_network(s) for s in n["subnets"] if ipaddress.ip_network(s).version == 4]
    subnets = allocate_subnets(users, {uid: v.get("subnet") for uid, v in previous.items()}, settings.pool,
                               in_use)

    files = {
        settings.compose_out: render_compose(users, subnets, settings.group),
        settings.map_file: render_map(users, settings.group),
        settings.state_file: render_state(users, subnets),
    }
    changed = [p for p, content in files.items() if read_text(p) != content]
    return Plan(users, errors, added, removed, files, changed)


def validate_compose(settings: Settings, content: str, docker: Docker) -> dict:
    """`docker compose config` with the new file in place of the generated one; returns the model."""
    fd, tmp = tempfile.mkstemp(dir=settings.generated_dir, prefix=".compose.users.", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        target = settings.compose_out.resolve()
        files = tuple(Path(tmp) if f.resolve() == target else f for f in settings.compose_files)
        try:
            return docker.model(files)
        except DockerError as exc:
            raise SyncError(f"generated Compose configuration rejected, nothing applied: {exc}") from exc
    finally:
        Path(tmp).unlink(missing_ok=True)


def apply(settings: Settings, plan: Plan, model: dict, docker: Docker) -> list[str]:
    """Make Docker match the installed files. Returns errors (empty: everything applied)."""
    errors: list[str] = []
    wanted_services = {f"{BROWSER_PREFIX}{u.uid}" for u in plan.users}
    model_services = set(model.get("services", {}))
    model_networks = set(model.get("networks", {}))

    # 1. Browsers of users who lost access (and any other browser service no longer in the project,
    #    e.g. Phase 2's browser-test): stop them first, this ends their open streams (plan §17).
    for cid, service in docker.project_containers():
        if service.startswith(BROWSER_PREFIX) and service not in model_services:
            log(f"stop and remove {service} ({cid[:12]})")
            docker.remove_container(cid)

    # 2. Routes. The map is already installed; test it before reloading.
    nginx = docker.running_nginx()
    if nginx is None:
        errors.append("nginx is not running: start the stack (docker compose up -d), then run the sync again")
    elif read_text(settings.map_loaded_file) != map_hash(settings.map_file):
        ok, out = docker.nginx_test(nginx)
        if ok:
            docker.nginx_reload(nginx)
            atomic_write(settings.map_loaded_file, map_hash(settings.map_file))
            log("nginx reloaded")
        else:
            prev = settings.map_file.with_name(settings.map_file.name + ".prev")
            if prev.exists():
                atomic_write(settings.map_file, prev.read_text())
            else:
                settings.map_file.unlink(missing_ok=True)
            errors.append(f"nginx -t failed, previous map restored, routes unchanged: {out}")

    # 3. Browsers: creates networks and containers, applies changed settings (BROWSER_IMAGE).
    if wanted_services:
        docker.up(sorted(wanted_services))

    # 4./5. Nginx on exactly the user networks of the project. Only networks this script (or Phase 2)
    # created are ever removed.
    networks = [n for n in docker.networks() if n["project"] == settings.project]
    for n in networks:
        key = n["key"] or ""
        if not (key.startswith(NETWORK_PREFIX) or key == PHASE2_NETWORK):
            continue
        if key in model_networks:
            if nginx and nginx not in n["containers"]:
                docker.connect(n["name"], nginx)
                log(f"nginx attached to {n['name']}")
        else:
            if nginx and nginx in n["containers"]:
                docker.disconnect(n["name"], nginx)
            others = [c for c in n["containers"] if c != nginx]
            if others:
                errors.append(f"network {n['name']} is no longer used but still has containers; not removed")
                continue
            docker.remove_network(n["name"])
            log(f"removed network {n['name']}")
    return errors


def run(settings: Settings, args: argparse.Namespace, docker: Docker | None = None,
        candidates: list[Candidate] | None = None) -> int:
    docker = docker or Docker(settings)
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
            plan = make_plan(settings, candidates, docker, force=args.force)
        except DockerError as exc:
            raise SyncError(f"docker: {exc}") from exc
        log(f"{len(plan.users)} user(s): {' '.join(u.uid for u in plan.users) or '-'}")
        if plan.added:
            log(f"added:   {' '.join(plan.added)}")
        if plan.removed:
            log(f"removed: {' '.join(plan.removed)} (profile directories are kept)")

        if args.dry_run:
            for p in plan.changed:
                log(f"would write {p}")
            log("dry run: nothing changed")
            return 2 if plan.errors else 0

        model = validate_compose(settings, plan.files[settings.compose_out], docker)
        if not args.generate_only:
            for u in plan.users:
                create_user_dir(settings.user_data_root, u)
        for path in plan.changed:
            install(path, plan.files[path])
            log(f"wrote {path}")
        if not plan.changed:
            log("generated files unchanged")
        if args.generate_only:
            return 2 if plan.errors else 0

        try:
            apply_errors = apply(settings, plan, model, docker)
        except DockerError as exc:
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
                      help="write the generated files, touch neither Docker nor profile directories")
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
