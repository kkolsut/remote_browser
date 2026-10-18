#!/usr/bin/env python3
"""Install the remote browser's fixed units from .env, idempotently (the sync adds the per-user ones):

  QUADLET_DIR/rb-frontend.network    Nginx's network (route metric 50, see scripts/rb_units.py)
  QUADLET_DIR/rb-nginx.container     Nginx, with the restrictions of plan §31
  GENERATED_DIR/nginx.env            Nginx's environment (an env file: values are taken literally)
  SYSTEMD_DIR/remote-browser-*       egress filter, sync, backup and image-update units (systemd/)

Validates first (Podman's Quadlet generator, then `nginx -t` in a throwaway container) and changes
nothing if either fails. Then: daemon-reload; enables the egress filter and the timers; starts Nginx,
or restarts it when its unit or environment changed (open streams end) or --restart-nginx is given
(after a change to nginx/templates/, which are rendered at start; map and snippet changes only need
`podman exec rb-nginx nginx -s reload`).

Usage: sudo ./scripts/install-units.py [--env-file FILE] [--check] [--restart-nginx] [--no-start]
  --check          validate only, change nothing (scripts/validate-config.sh)
  --no-start       install and enable, start nothing
Exit status: 0 ok (prints "changed" when anything was written), 1 error.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rb_units  # noqa: E402
from freeipa_common import REPO_DIR, read_env_file  # noqa: E402
from rb_units import EGRESS_UNIT, NGINX, NginxConfig, UnitError  # noqa: E402

SYSTEMD_DIR = Path("/etc/systemd/system")
TIMERS = ("remote-browser-sync.timer", "remote-browser-backup.timer", "remote-browser-image-update.timer")
REQUIRED = ("PUBLIC_HOST", "AUTHELIA_HOST", "AUTHELIA_AUTHZ_URL", "TRUSTED_PROXY")
# Only these variables are substituted into nginx/templates/*.template.
ENVSUBST_FILTER = ("^(PUBLIC_HOST|AUTHELIA_HOST|PUBLIC_PORT|AUTHELIA_AUTHZ_URL|AUTHELIA_CA_FILE|TRUSTED_PROXY"
                   "|NGINX_LOCAL_RESOLVERS)$")


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def repo_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_DIR / path


def nginx_config(env: dict[str, str], env_file: Path) -> NginxConfig:
    def get(key: str, default: str = "") -> str:
        return env.get(key, "").strip() or default

    missing = [k for k in REQUIRED if not get(k)]
    if missing:
        fail(f"set {', '.join(missing)} in the env file")
    return NginxConfig(
        repo_dir=REPO_DIR,
        env_file=env_file,
        bind_address=get("BIND_ADDRESS", "0.0.0.0"),
        https_port=get("HTTPS_PORT", "8443"),
        nginx_generated_dir=repo_path(get("NGINX_GENERATED_DIR", "./nginx/generated")),
        tls_dir=repo_path(get("TLS_DIR", "./local/tls")),
        authelia_ca_dir=repo_path(get("AUTHELIA_CA_DIR", "./local/authelia-ca")),
        frontend_subnet=get("FRONTEND_SUBNET"),
        environment={
            "PUBLIC_HOST": get("PUBLIC_HOST"),
            "AUTHELIA_HOST": get("AUTHELIA_HOST"),
            "PUBLIC_PORT": get("PUBLIC_PORT", "443"),
            "AUTHELIA_AUTHZ_URL": get("AUTHELIA_AUTHZ_URL"),
            "AUTHELIA_CA_FILE": get("AUTHELIA_CA_FILE", "/etc/ssl/certs/ca-certificates.crt"),
            "TRUSTED_PROXY": get("TRUSTED_PROXY"),
            "TZ": get("TZ", "UTC"),
            # NGINX_LOCAL_RESOLVERS from /etc/resolv.conf: aardvark-dns on the frontend network's gateway,
            # which also answers for the user networks Nginx joins later.
            "NGINX_ENTRYPOINT_LOCAL_RESOLVERS": "1",
            "NGINX_ENVSUBST_FILTER": ENVSUBST_FILTER,
        },
    )


def host_units() -> dict[str, str]:
    return {p.name: p.read_text().replace("@REPO_DIR@", str(REPO_DIR))
            for p in sorted((REPO_DIR / "systemd").iterdir()) if p.suffix in (".service", ".timer")}


def systemctl(*args: str) -> None:
    proc = subprocess.run(["systemctl", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        fail(f"systemctl {' '.join(args)}: {(proc.stderr or proc.stdout).strip()}")


def nginx_test(cfg: NginxConfig, env_content: str) -> None:
    for d in (cfg.nginx_generated_dir, cfg.tls_dir, cfg.authelia_ca_dir):
        if not d.is_dir():
            fail(f"{d} is missing (scripts/init-secrets.sh creates it)")
    with tempfile.NamedTemporaryFile("w", prefix="rb-nginx-", suffix=".env") as f:
        f.write(env_content)
        f.flush()
        test_cfg = NginxConfig(**{**cfg.__dict__, "env_file": Path(f.name)})
        proc = subprocess.run(rb_units.nginx_test_command(test_cfg), capture_output=True, text=True)
    out = (proc.stderr or proc.stdout).strip()
    if proc.returncode != 0:
        fail(f"nginx -t failed:\n{out}")
    print("\n".join(line for line in out.splitlines() if "nginx: " in line))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", type=Path, default=REPO_DIR / ".env")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--restart-nginx", action="store_true")
    ap.add_argument("--no-start", action="store_true")
    args = ap.parse_args()

    env_file = args.env_file.resolve()
    if not env_file.is_file():
        fail(f"missing {env_file} (copy .env.example)")
    env = read_env_file(env_file)
    quadlet_dir = Path(env.get("QUADLET_DIR", "").strip() or rb_units.QUADLET_DIR)
    generated_dir = repo_path(env.get("GENERATED_DIR", "").strip() or "./generated")
    cfg = nginx_config(env, generated_dir / "nginx.env")

    try:
        env_content = rb_units.nginx_env(cfg)
        quadlets = {
            f"{rb_units.FRONTEND}.network": rb_units.frontend_network(rb_units.safe("FRONTEND_SUBNET",
                                                                                    cfg.frontend_subnet)),
            f"{NGINX}.container": rb_units.nginx_container(cfg, REPO_DIR / "scripts" / "sync-freeipa-users.py"),
        }
        print("== quadlet generator")
        rb_units.validate(quadlets)
        print("ok")
    except UnitError as exc:
        fail(str(exc))
    print("== nginx -t")
    nginx_test(cfg, env_content)
    if args.check:
        print("OK")
        return 0

    files = {quadlet_dir / n: c for n, c in quadlets.items()}
    files[cfg.env_file] = env_content
    files.update({SYSTEMD_DIR / n: c for n, c in host_units().items()})
    changed = [p for p, c in files.items() if not p.exists() or p.read_text() != c]
    for p in changed:
        p.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        p.write_text(files[p])
        p.chmod(0o600 if p == cfg.env_file else 0o644)
        print(f"wrote {p}")
    nginx_changed = any(p.parent == quadlet_dir or p == cfg.env_file for p in changed)

    if changed:
        systemctl("daemon-reload")
    systemctl("enable", EGRESS_UNIT, *TIMERS)
    if not args.no_start:
        systemctl("start", EGRESS_UNIT, *TIMERS)
        if nginx_changed or args.restart_nginx:
            systemctl("restart", f"{NGINX}.service")
            print(f"{NGINX} (re)started")
        else:
            systemctl("start", f"{NGINX}.service")
    if changed:
        print("changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
