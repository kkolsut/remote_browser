#!/usr/bin/env python3
"""Unit tests for scripts/sync-freeipa-users.py (plan §56): fail-safe behaviour, eligibility, generated
files and units, removal guard, subnet allocation, apply order. LDAP, Podman and systemd are faked; the
generated units are also checked by Podman's real Quadlet generator when it is installed.

    python3 tests/test_sync.py      (or: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("sync", REPO / "scripts" / "sync-freeipa-users.py")
sync = importlib.util.module_from_spec(spec)
sys.modules["sync"] = sync
spec.loader.exec_module(sync)
from freeipa_common import FreeIPAError, Member  # noqa: E402  (path set up by the sync module)

if not os.environ.get("VERBOSE"):
    sync.log = sync.warn = lambda msg: None

ME = os.getuid()
MY_GID = os.getgid()
MAP_LINE = re.compile(r'^"([a-z][a-z0-9-]{0,30})" "browser-\1:3000";$')


def args(**kw) -> argparse.Namespace:
    return argparse.Namespace(**{"dry_run": False, "generate_only": False, "attach_only": False, "force": False,
                                 **kw})


def cand(uid, uid_number=ME, gid_number=MY_GID, source=None):
    return sync.Candidate(uid, uid_number, gid_number, source or f"uid={uid},cn=users,cn=accounts,dc=x")


def parse_unit(text: str) -> dict[str, dict[str, list[str]]]:
    """Quadlet file -> {section: {key: [values]}} (keys may repeat)."""
    result: dict[str, dict[str, list[str]]] = {}
    section = None
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = result.setdefault(line.strip("[]"), {})
        else:
            key, value = line.split("=", 1)
            section.setdefault(key, []).append(value)
    return result


class FakePodman:
    """Records actions; models networks and containers from the units the sync installs."""

    def __init__(self, settings, other_subnets=()):
        self.s = settings
        self.actions: list[tuple] = []
        self.nets: dict[str, dict] = {}  # name -> network
        self.running: set[str] = set()  # running units
        self.nginx = True
        self.nginx_ok = True
        self.reject = None
        for i, subnet in enumerate(other_subnets):
            self.nets[f"other{i}"] = {"name": f"other{i}", "user": None, "subnets": [subnet], "containers": []}

    def validate(self, units):
        if self.reject:
            raise sync.EngineError(self.reject)

    def networks(self):
        return [dict(n, containers=list(n["containers"])) for n in self.nets.values()]

    def nginx_running(self):
        return self.nginx

    def nginx_test(self):
        self.actions.append(("nginx -t",))
        return self.nginx_ok, "" if self.nginx_ok else "bad config"

    def nginx_reload(self):
        self.actions.append(("reload",))

    def daemon_reload(self):
        self.actions.append(("daemon-reload",))

    def stop(self, units):
        self.actions.append(("stop", tuple(units)))
        for unit in units:
            self.running.discard(unit)
            if unit.startswith("browser-"):
                for n in self.nets.values():
                    if unit[:-8] in n["containers"]:
                        n["containers"].remove(unit[:-8])

    def try_restart(self, units):
        self.actions.append(("try-restart", tuple(units)))

    def start(self, units):
        self.actions.append(("start", tuple(units)))
        for unit in units:
            uid = unit.removeprefix("browser-").removeprefix("rb-net-").removesuffix(".service").removesuffix("-network")
            unit_file = self.s.quadlet_dir / f"rb-net-{uid}.network"
            name = f"rb-net-{uid}"
            if name not in self.nets and unit_file.exists():
                subnet = parse_unit(unit_file.read_text())["Network"]["Subnet"][0]
                self.nets[name] = {"name": name, "user": uid, "subnets": [subnet], "containers": []}
            if unit.startswith("browser-") and name in self.nets and f"browser-{uid}" not in self.nets[name]["containers"]:
                self.nets[name]["containers"].append(f"browser-{uid}")
            self.running.add(unit)

    def connect(self, network):
        self.actions.append(("connect", network))
        self.nets[network]["containers"].append("rb-nginx")

    def disconnect(self, network):
        self.actions.append(("disconnect", network))
        self.nets[network]["containers"].remove("rb-nginx")

    def remove_network(self, network):
        self.actions.append(("rm network", network))
        del self.nets[network]


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rb-sync-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = self.tmp / "test.env"
        self.write_env()
        self.settings = sync.load_settings(self.env)
        self.engine = FakePodman(self.settings)

    def write_env(self, **extra):
        values = {
            # Every test user has this process's uid (so no root is needed to create their profile
            # directories); only the file backend (tests) allows shared uidNumbers. The FreeIPA
            # path is tested through ldap_candidates and select_users.
            "AUTH_BACKEND": "file",
            "DEMO_UID": str(ME),
            "DEMO_GID": str(MY_GID),
            "QUADLET_DIR": str(self.tmp / "quadlet"),
            "GENERATED_DIR": str(self.tmp / "generated"),
            "NGINX_GENERATED_DIR": str(self.tmp / "nginx"),
            "USER_DATA_ROOT": str(self.tmp / "users"),
            "BROWSER_NET_POOL": "10.213.0.0/16",
            "BROWSER_IMAGE": "local/remote-dual-browser:test",
            "PUBLIC_HOST": "browser.example.com",
            "AUTHELIA_HOST": "auth.example.com",
            "AUTHELIA_AUTHZ_URL": "https://auth.example.com/api/authz/remote-browser",
            "TRUSTED_PROXY": "192.0.2.1/32",
            **extra,
        }
        self.env.write_text("".join(f"{k}={v}\n" for k, v in values.items()))

    def sync_run(self, candidates, **kw) -> int:
        return sync.run(self.settings, args(**kw), engine=self.engine, candidates=candidates)

    def snapshot(self) -> dict:
        return {p.relative_to(self.tmp): p.read_bytes() for p in sorted(self.tmp.rglob("*"))
                if p.is_file() and p.name != ".sync.lock"}

    def state(self) -> dict:
        return json.loads(self.settings.state_file.read_text())["users"]

    def map_users(self) -> list[str]:
        lines = [line for line in self.settings.map_file.read_text().splitlines() if not line.startswith("#")]
        for line in lines:
            self.assertRegex(line, MAP_LINE)
        return [MAP_LINE.match(line).group(1) for line in lines]

    # --- fail-safe -------------------------------------------------------------------------------

    def test_ldap_errors_abort_without_changes(self):
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        before = self.snapshot()
        actions = len(self.engine.actions)
        for message in ("LDAP connect/bind failed: unreachable", "LDAP connect/bind failed: invalidCredentials",
                        "group not found or not readable", "LDAP search failed: sizeLimitExceeded"):
            def broken(_settings, message=message):
                raise FreeIPAError(message)
            orig = sync.ldap_candidates
            sync.ldap_candidates = broken
            try:
                with self.assertRaises(sync.SyncError):
                    sync.run(dataclasses.replace(self.settings, backend="freeipa"), args(), engine=self.engine)
            finally:
                sync.ldap_candidates = orig
            self.assertEqual(self.snapshot(), before, message)
            self.assertEqual(len(self.engine.actions), actions, message)

    def test_empty_group_refused_without_force(self):
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        before = self.snapshot()
        with self.assertRaisesRegex(sync.SyncError, "--force"):
            self.sync_run([])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.sync_run([], force=True), 0)
        self.assertEqual(self.map_users(), [])
        self.assertIn(("stop", ("browser-alice.service", "rb-net-alice-network.service")), self.engine.actions)
        self.assertIn(("rm network", "rb-net-alice"), self.engine.actions)

    def test_empty_group_on_first_run_refused(self):
        with self.assertRaises(sync.SyncError):
            self.sync_run([])
        self.assertFalse(self.settings.map_file.exists())

    def test_mass_removal_refused_without_force(self):
        self.assertEqual(self.sync_run([cand(u) for u in ("a1", "a2", "a3", "a4")]), 0)
        before = self.snapshot()
        with self.assertRaisesRegex(sync.SyncError, "SYNC_MAX_REMOVALS"):
            self.sync_run([cand("a1")])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.sync_run([cand("a1"), cand("a2")]), 0)  # 2 removals: allowed
        self.assertEqual(self.sync_run([cand("a1")], force=False), 0)  # 1 more
        self.write_env(SYNC_MAX_REMOVALS="0")
        self.settings = sync.load_settings(self.env)
        with self.assertRaises(sync.SyncError):
            self.sync_run([cand("a5")])

    def test_unreadable_state_aborts(self):
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        self.settings.state_file.write_text("{broken")
        with self.assertRaisesRegex(sync.SyncError, "state.json"):
            self.sync_run([cand("alice")])

    def test_rejected_units_are_never_installed(self):
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        before = self.snapshot()
        self.engine.reject = "converting \"browser-bob.container\": unsupported key"
        with self.assertRaisesRegex(sync.SyncError, "rejected"):
            self.sync_run([cand("alice"), cand("bob")])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.tmp / "users" / "bob").exists())

    def test_failed_nginx_test_restores_previous_map(self):
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        old_map = self.settings.map_file.read_text()
        self.engine.nginx_ok = False
        self.assertEqual(self.sync_run([cand("alice"), cand("bob")]), 1)
        self.assertEqual(self.settings.map_file.read_text(), old_map)
        self.assertNotIn(("reload",), self.engine.actions[-5:])

    # --- eligibility -----------------------------------------------------------------------------

    def fake_ldap(self, members, nested=()):
        saved = (sync.load_config, sync.connect, sync.resolve_members)

        class Conn:
            def unbind(self):
                pass
        sync.load_config = lambda env_file: object()
        sync.connect = lambda cfg: Conn()
        sync.resolve_members = lambda conn, cfg: (members, list(nested))

        def restore():
            sync.load_config, sync.connect, sync.resolve_members = saved
        self.addCleanup(restore)

    def member(self, uid, eligible=True, **kw):
        return Member(dn=f"uid={uid},cn=users,cn=accounts,dc=x", uid=uid, uid_number=kw.get("uid_number", ME),
                      gid_number=MY_GID, eligible_by_filter=eligible)

    def test_disabled_and_expired_members_not_provisioned(self):
        self.fake_ldap([self.member("alice"), self.member("locked", eligible=False),
                        self.member("expired", eligible=False)])
        users, _ = sync.select_users(sync.ldap_candidates(self.settings))
        self.assertEqual([u.uid for u in users], ["alice"])

    def test_nested_group_members_not_provisioned(self):
        # resolve_members reports nested groups separately and never expands them.
        self.fake_ldap([self.member("alice")], nested=["cn=sub,cn=groups,cn=accounts,dc=x"])
        self.assertEqual([c.uid for c in sync.ldap_candidates(self.settings)], ["alice"])

    def test_one_and_multiple_users(self):
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        self.assertEqual(self.map_users(), ["alice"])
        self.assertEqual(self.sync_run([cand("carol"), cand("alice"), cand("bob")]), 0)
        self.assertEqual(self.map_users(), ["alice", "bob", "carol"])  # sorted
        self.assertEqual(sorted(self.state()), ["alice", "bob", "carol"])

    def test_duplicate_ldap_result(self):
        users, errors = sync.select_users([cand("alice"), cand("alice")])
        self.assertEqual([u.uid for u in users], ["alice"])
        self.assertEqual(errors, [])

    def test_same_uid_from_two_entries_is_refused(self):
        users, errors = sync.select_users([cand("alice", source="dn1"), cand("alice", source="dn2"), cand("bob")])
        self.assertEqual([u.uid for u in users], ["bob"])
        self.assertTrue(any("several" in e for e in errors))

    def test_invalid_uids_not_provisioned(self):
        bad = ["Alice", "john.smith", "a_b", "1abc", "-x", "a" * 32, "", None, "x/../y", 'x"y', "x y"]
        users, errors = sync.select_users([cand(u, source=f"s{i}") for i, u in enumerate(bad)] + [cand("ok-1")])
        self.assertEqual([u.uid for u in users], ["ok-1"])
        self.assertEqual(len(errors), len(bad))
        self.assertEqual(self.sync_run([cand(u, source=f"s{i}") for i, u in enumerate(bad)] + [cand("ok-1")]), 2)
        self.assertEqual(self.map_users(), ["ok-1"])

    def test_missing_or_root_ids_not_provisioned(self):
        users, errors = sync.select_users([cand("a", uid_number=None), cand("b", gid_number=0),
                                           cand("c", uid_number=0), cand("d")])
        self.assertEqual([u.uid for u in users], ["d"])
        self.assertEqual(len(errors), 3)

    def test_shared_uid_number_refused_except_file_backend(self):
        users, errors = sync.select_users([cand("a", uid_number=5000), cand("b", uid_number=5000), cand("c")])
        self.assertEqual([u.uid for u in users], ["c"])
        users, _ = sync.select_users([cand("a"), cand("b")], shared_ids_ok=True)
        self.assertEqual(len(users), 2)

    # --- profile directories ---------------------------------------------------------------------

    def test_profile_directories_created_0700_and_kept_on_removal(self):
        self.assertEqual(self.sync_run([cand("alice"), cand("bob")]), 0)
        for u in ("alice", "bob"):
            st = (self.tmp / "users" / u).stat()
            self.assertEqual((st.st_uid, st.st_mode & 0o777), (ME, 0o700))
            self.assertTrue((self.tmp / "users" / u / "config").is_dir())
        (self.tmp / "users" / "bob" / "config" / "Bookmarks").write_text("keep me")
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        self.assertEqual((self.tmp / "users" / "bob" / "config" / "Bookmarks").read_text(), "keep me")

    def test_directory_owned_by_someone_else_is_not_provisioned(self):
        (self.tmp / "users" / "alice").mkdir(parents=True)  # owned by ME
        self.assertEqual(self.sync_run([cand("alice", uid_number=ME + 1), cand("bob")]), 2)
        self.assertEqual(self.map_users(), ["bob"])

    def test_symlinked_profile_is_not_provisioned(self):
        (self.tmp / "users").mkdir()
        (self.tmp / "users" / "alice").symlink_to(self.tmp)
        self.assertEqual(self.sync_run([cand("alice"), cand("bob")]), 2)
        self.assertEqual(self.map_users(), ["bob"])

    @unittest.skipIf(ME == 0, "root may create directories for any uid")
    def test_cannot_create_foreign_directory_aborts_before_install(self):
        with self.assertRaisesRegex(sync.SyncError, "run the sync as root"):
            self.sync_run([cand("alice", uid_number=ME + 1)])
        self.assertFalse(self.settings.map_file.exists())

    def test_generate_only_and_dry_run_touch_no_directories(self):
        self.assertEqual(self.sync_run([cand("alice")], dry_run=True), 0)
        self.assertFalse(self.settings.map_file.exists())
        self.assertEqual(self.sync_run([cand("alice")], generate_only=True), 0)
        self.assertTrue(self.settings.map_file.exists())
        self.assertFalse((self.tmp / "users" / "alice").exists())
        self.assertEqual(self.engine.actions, [])

    # --- changes, idempotence, apply order ---------------------------------------------------------

    def test_add_remove_and_no_change(self):
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        self.assertEqual(self.sync_run([cand("alice"), cand("bob")]), 0)
        self.assertIn(("connect", "rb-net-bob"), self.engine.actions)
        self.assertTrue((self.tmp / "quadlet" / "browser-bob.container").exists())
        before, n = self.snapshot(), len(self.engine.actions)
        self.assertEqual(self.sync_run([cand("bob"), cand("alice")]), 0)
        self.assertEqual(self.snapshot(), before)
        # A no-op run only re-asserts the browsers (start is idempotent): no daemon-reload, no restart,
        # no Nginx reload, no network change.
        self.assertEqual(self.engine.actions[n:], [("start", ("browser-alice.service", "browser-bob.service"))])

        self.engine.actions.clear()
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        kinds = [a[0] for a in self.engine.actions]
        # Streams end first, before the unit files go.
        self.assertEqual(self.engine.actions[0], ("stop", ("browser-bob.service", "rb-net-bob-network.service")))
        self.assertLess(kinds.index("daemon-reload"), kinds.index("start"))
        self.assertLess(kinds.index("reload"), kinds.index("start"))
        self.assertIn(("disconnect", "rb-net-bob"), self.engine.actions)
        self.assertIn(("rm network", "rb-net-bob"), self.engine.actions)
        self.assertNotIn("try-restart", kinds)
        self.assertEqual(self.map_users(), ["alice"])
        self.assertFalse((self.tmp / "quadlet" / "browser-bob.container").exists())
        self.assertFalse((self.tmp / "quadlet" / "rb-net-bob.network").exists())
        self.assertTrue((self.tmp / "users" / "bob" / "config").is_dir())  # profile kept
        self.assertTrue((self.settings.map_file.parent / "users.map.prev").exists())

    def test_new_image_restarts_existing_browsers_only(self):
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        self.write_env(BROWSER_IMAGE="localhost/remote-dual-browser:new")
        self.settings = sync.load_settings(self.env)
        self.engine.s = self.settings
        self.engine.actions.clear()
        self.assertEqual(self.sync_run([cand("alice"), cand("bob")]), 0)
        self.assertIn(("try-restart", ("browser-alice.service",)), self.engine.actions)  # not bob: new
        self.assertIn("Image=localhost/remote-dual-browser:new",
                      (self.tmp / "quadlet" / "browser-bob.container").read_text())

    def test_unsafe_values_refused(self):
        for key, value in (("PUBLIC_HOST", "x$(id)"), ("TZ", "Europe/Warsaw %n"), ("BROWSER_IMAGE", 'a"b'),
                           ("USER_DATA_ROOT", "/srv/my profiles")):
            self.write_env(**{key: value})
            with self.assertRaisesRegex(sync.SyncError, key):
                sync.load_settings(self.env)

    def test_missing_image_refused(self):
        self.write_env(BROWSER_IMAGE="")
        self.settings = sync.load_settings(self.env)
        with self.assertRaisesRegex(sync.SyncError, "BROWSER_IMAGE"):
            self.sync_run([cand("alice")])

    def test_attach_only_joins_known_networks(self):
        self.assertEqual(self.sync_run([cand("alice"), cand("bob")]), 0)
        for n in self.engine.nets.values():  # a new Nginx container: on no user network
            if "rb-nginx" in n["containers"]:
                n["containers"].remove("rb-nginx")
        self.engine.actions.clear()
        self.assertEqual(self.sync_run(None, attach_only=True), 0)
        self.assertEqual(self.engine.actions[0], ("start", ("rb-net-alice-network.service", "rb-net-bob-network.service")))
        self.assertEqual(sorted(a for a in self.engine.actions if a[0] == "connect"),
                         [("connect", "rb-net-alice"), ("connect", "rb-net-bob")])
        self.engine.actions.clear()
        self.assertEqual(self.sync_run(None, attach_only=True), 0)  # already attached: nothing
        self.assertNotIn("connect", [a[0] for a in self.engine.actions])

    def test_nginx_not_running(self):
        self.engine.nginx = False
        self.assertEqual(self.sync_run([cand("alice")]), 1)
        self.assertIn(("start", ("browser-alice.service",)), self.engine.actions)

    def test_reload_after_generate_only(self):
        self.assertEqual(self.sync_run([cand("alice")], generate_only=True), 0)
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        self.assertIn(("reload",), self.engine.actions)

    def test_output_is_deterministic(self):
        self.assertEqual(self.sync_run([cand("b"), cand("a"), cand("c")]), 0)
        first = self.snapshot()
        shutil.rmtree(self.tmp / "generated")
        shutil.rmtree(self.tmp / "nginx")
        shutil.rmtree(self.tmp / "quadlet")
        self.engine = FakePodman(self.settings)
        self.assertEqual(self.sync_run([cand("c"), cand("a"), cand("b")]), 0)
        second = self.snapshot()
        for path in first:
            if path.name.endswith(".prev"):
                continue
            self.assertEqual(first[path], second[path], path)

    # --- subnets ---------------------------------------------------------------------------------

    def test_subnets_stable_and_skip_used(self):
        self.engine = FakePodman(self.settings, other_subnets=["10.213.0.0/24"])
        self.assertEqual(self.sync_run([cand("bob")]), 0)
        self.assertEqual(self.state()["bob"]["subnet"], "10.213.1.0/28")
        self.assertEqual(self.sync_run([cand("alice"), cand("bob")]), 0)
        self.assertEqual(self.state()["bob"]["subnet"], "10.213.1.0/28")  # kept although alice sorts first
        self.assertEqual(self.state()["alice"]["subnet"], "10.213.1.16/28")
        nets = [ipaddress.ip_network(v["subnet"]) for v in self.state().values()]
        self.assertFalse(nets[0].overlaps(nets[1]))

    def test_pool_exhausted(self):
        pool = ipaddress.ip_network("10.213.0.0/27")
        users = [sync.User(u, ME, MY_GID) for u in ("a", "b", "c")]
        with self.assertRaisesRegex(sync.SyncError, "no free"):
            sync.allocate_subnets(users, {}, pool, [])

    # --- generated files -------------------------------------------------------------------------

    def test_generated_units_and_map(self):
        self.assertEqual(self.sync_run([cand("alice"), cand("bob-2")]), 0)
        qdir = self.tmp / "quadlet"
        self.assertEqual(sorted(p.name for p in qdir.iterdir()),
                         ["browser-alice.container", "browser-bob-2.container",
                          "rb-net-alice.network", "rb-net-bob-2.network"])
        unit = parse_unit((qdir / "browser-alice.container").read_text())
        c = unit["Container"]
        self.assertEqual(c["ContainerName"], ["browser-alice"])
        self.assertEqual(c["HostName"], ["browser-alice"])
        self.assertEqual(c["Network"], ["rb-net-alice.network"])
        self.assertIn(f"PUID={ME}", c["Environment"])
        self.assertIn("SELKIES_ALLOWED_ORIGINS=https://browser.example.com,https://browser.example.com:443",
                      c["Environment"])
        self.assertEqual(c["Volume"], [f"{self.tmp}/users/alice/config:/config:Z"])
        self.assertNotIn("PublishPort", c)
        self.assertNotIn("PodmanArgs", c)
        self.assertNotIn("SeccompProfile", c)  # Podman's default
        self.assertEqual(c["NoNewPrivileges"], ["true"])
        self.assertEqual(c["DropCapability"], ["ALL"])
        caps = c["AddCapability"][0].split()
        self.assertIn("SYS_CHROOT", caps)  # both browser sandboxes chroot
        self.assertFalse({"SYS_ADMIN", "NET_RAW", "MKNOD", "NET_ADMIN"} & set(caps))
        self.assertGreater(int(c["PidsLimit"][0]), 0)
        self.assertEqual(unit["Unit"]["Requires"], ["remote-browser-egress.service"])
        net = parse_unit((qdir / "rb-net-bob-2.network").read_text())["Network"]
        self.assertEqual(net["NetworkName"], ["rb-net-bob-2"])
        self.assertEqual(net["Subnet"], ["10.213.0.16/28"])
        self.assertEqual(self.map_users(), ["alice", "bob-2"])

    def test_foreign_files_in_quadlet_dir_left_alone(self):
        qdir = self.tmp / "quadlet"
        qdir.mkdir()
        for name in ("rb-nginx.container", "rb-frontend.network", "browser-Bad.container", "notes.txt"):
            (qdir / name).write_text("keep")
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        self.assertEqual(self.sync_run([cand("bob")]), 0)
        for name in ("rb-nginx.container", "rb-frontend.network", "browser-Bad.container", "notes.txt"):
            self.assertEqual((qdir / name).read_text(), "keep")

    @unittest.skipUnless(sync.rb_units.generator(), "Podman's Quadlet generator not installed")
    def test_generated_units_accepted_by_quadlet(self):
        self.assertEqual(self.sync_run([cand("alice"), cand("bob")]), 0)
        units = sync.generated_unit_files(self.tmp / "quadlet")
        out = sync.rb_units.validate(units)
        self.assertIn("---browser-alice.service---", out)
        self.assertIn("Requires=rb-net-alice-network.service", out)
        self.assertIn("--cap-drop all", out)
        self.assertIn(f"-v {self.tmp}/users/alice/config:/config:Z", out)
        with self.assertRaises(sync.UnitError):
            sync.rb_units.validate({"browser-x.container": units["browser-alice.container"] + "Bogus=1\n"})


if __name__ == "__main__":
    unittest.main()
