#!/usr/bin/env python3
"""Unit tests for scripts/sync-freeipa-users.py (plan §56): fail-safe behaviour, eligibility, generated
files, removal guard, subnet allocation, apply order. LDAP and Docker are faked; the generated Compose
file is also checked by the real `docker compose config` when Docker is available.

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
import subprocess
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
    return argparse.Namespace(**{"dry_run": False, "generate_only": False, "force": False, **kw})


def cand(uid, uid_number=ME, gid_number=MY_GID, source=None):
    return sync.Candidate(uid, uid_number, gid_number, source or f"uid={uid},cn=users,cn=accounts,dc=x")


class FakeDocker:
    """Records actions; models the project from the files the sync generates."""

    def __init__(self, settings, other_subnets=()):
        self.s = settings
        self.actions: list[tuple] = []
        self.nets: dict[str, dict] = {}  # docker name -> network
        self.containers: dict[str, str] = {}  # id -> service
        self.nginx = "nginx-id"
        self.nginx_ok = True
        for i, subnet in enumerate(other_subnets):
            self.nets[f"other{i}"] = {"name": f"other{i}", "project": None, "key": None,
                                      "subnets": [subnet], "containers": []}

    def model(self, files):
        import yaml
        generated = yaml.safe_load(Path(files[-1]).read_text())
        return {"services": {"nginx": {}, **(generated.get("services") or {})},
                "networks": {"frontend": {}, **(generated.get("networks") or {})}}

    def networks(self):
        return [dict(n) for n in self.nets.values()]

    def project_containers(self):
        return list(self.containers.items())

    def running_nginx(self):
        return self.nginx

    def remove_container(self, cid):
        self.actions.append(("remove", self.containers.pop(cid)))
        for n in self.nets.values():
            if cid in n["containers"]:
                n["containers"].remove(cid)

    def nginx_test(self, cid):
        self.actions.append(("nginx -t",))
        return self.nginx_ok, "" if self.nginx_ok else "bad config"

    def nginx_reload(self, cid):
        self.actions.append(("reload",))

    def up(self, services):
        self.actions.append(("up", tuple(services)))
        model = self.model(self.s.compose_files)
        for svc in services:
            self.containers.setdefault(f"id-{svc}", svc)
        for key, net in model["networks"].items():
            name = f"{self.s.project}_{key}"
            if key.startswith("net-") and name not in self.nets:
                subnet = net["ipam"]["config"][0]["subnet"]
                self.nets[name] = {"name": name, "project": self.s.project, "key": key, "subnets": [subnet],
                                   "containers": [f"id-browser-{key[4:]}"]}

    def connect(self, network, cid):
        self.actions.append(("connect", network))
        self.nets[network]["containers"].append(cid)

    def disconnect(self, network, cid):
        self.actions.append(("disconnect", network))
        self.nets[network]["containers"].remove(cid)

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
        self.docker = FakeDocker(self.settings)

    def write_env(self, **extra):
        values = {
            # Every test user has this process's uid (so no root is needed to create their profile
            # directories); only the file backend (the demo) allows shared uidNumbers. The FreeIPA
            # path is tested through ldap_candidates and select_users.
            "AUTH_BACKEND": "file",
            "DEMO_UID": str(ME),
            "DEMO_GID": str(MY_GID),
            "COMPOSE_PROJECT_NAME": "rb-unit",
            "GENERATED_DIR": str(self.tmp / "generated"),
            "NGINX_GENERATED_DIR": str(self.tmp / "nginx"),
            "USER_DATA_ROOT": str(self.tmp / "users"),
            "COMPOSE_FILE": f"compose.yaml:{self.tmp / 'generated' / 'compose.users.yaml'}",
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
        return sync.run(self.settings, args(**kw), docker=self.docker, candidates=candidates)

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
        actions = len(self.docker.actions)
        for message in ("LDAP connect/bind failed: unreachable", "LDAP connect/bind failed: invalidCredentials",
                        "group not found or not readable", "LDAP search failed: sizeLimitExceeded"):
            def broken(_settings, message=message):
                raise FreeIPAError(message)
            orig = sync.ldap_candidates
            sync.ldap_candidates = broken
            try:
                with self.assertRaises(sync.SyncError):
                    sync.run(dataclasses.replace(self.settings, backend="freeipa"), args(), docker=self.docker)
            finally:
                sync.ldap_candidates = orig
            self.assertEqual(self.snapshot(), before, message)
            self.assertEqual(len(self.docker.actions), actions, message)

    def test_empty_group_refused_without_force(self):
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        before = self.snapshot()
        with self.assertRaisesRegex(sync.SyncError, "--force"):
            self.sync_run([])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.sync_run([], force=True), 0)
        self.assertEqual(self.map_users(), [])
        self.assertIn(("remove", "browser-alice"), self.docker.actions)

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

    def test_invalid_compose_is_never_installed(self):
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        before = self.snapshot()

        def reject(files):
            raise sync.DockerError("services.browser-bob: bad")
        self.docker.model = reject
        with self.assertRaisesRegex(sync.SyncError, "rejected"):
            self.sync_run([cand("alice"), cand("bob")])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.tmp / "users" / "bob").exists())

    def test_failed_nginx_test_restores_previous_map(self):
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        old_map = self.settings.map_file.read_text()
        self.docker.nginx_ok = False
        self.assertEqual(self.sync_run([cand("alice"), cand("bob")]), 1)
        self.assertEqual(self.settings.map_file.read_text(), old_map)
        self.assertNotIn(("reload",), self.docker.actions[-5:])

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

    def test_shared_uid_number_refused_except_demo(self):
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
        self.assertEqual(self.docker.actions, [])

    # --- changes, idempotence, apply order ---------------------------------------------------------

    def test_add_remove_and_no_change(self):
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        self.assertEqual(self.sync_run([cand("alice"), cand("bob")]), 0)
        self.assertIn(("connect", "rb-unit_net-bob"), self.docker.actions)
        before, n = self.snapshot(), len(self.docker.actions)
        self.assertEqual(self.sync_run([cand("bob"), cand("alice")]), 0)
        self.assertEqual(self.snapshot(), before)
        # A no-op run only re-asserts the browsers (compose up is idempotent): no reload, no network change.
        self.assertEqual(self.docker.actions[n:], [("up", ("browser-alice", "browser-bob"))])

        self.docker.actions.clear()
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        kinds = [a[0] for a in self.docker.actions]
        self.assertEqual(self.docker.actions[0], ("remove", "browser-bob"))  # streams end first
        self.assertLess(kinds.index("reload"), kinds.index("up"))
        self.assertIn(("disconnect", "rb-unit_net-bob"), self.docker.actions)
        self.assertIn(("rm network", "rb-unit_net-bob"), self.docker.actions)
        self.assertEqual(self.map_users(), ["alice"])
        self.assertTrue((self.settings.map_file.parent / "users.map.prev").exists())

    def test_reload_after_generate_only(self):
        self.assertEqual(self.sync_run([cand("alice")], generate_only=True), 0)
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        self.assertIn(("reload",), self.docker.actions)

    def test_output_is_deterministic(self):
        self.assertEqual(self.sync_run([cand("b"), cand("a"), cand("c")]), 0)
        first = self.snapshot()
        shutil.rmtree(self.tmp / "generated")
        shutil.rmtree(self.tmp / "nginx")
        self.docker = FakeDocker(self.settings)
        self.assertEqual(self.sync_run([cand("c"), cand("a"), cand("b")]), 0)
        second = self.snapshot()
        for path in first:
            if path.name.endswith(".prev"):
                continue
            self.assertEqual(first[path], second[path], path)

    def test_phase2_leftovers_are_removed(self):
        self.docker.containers["old"] = "browser-test"
        self.docker.nets["rb-unit_browser-test"] = {"name": "rb-unit_browser-test", "project": "rb-unit",
                                                    "key": "browser-test", "subnets": ["10.213.0.16/28"],
                                                    "containers": ["nginx-id"]}
        self.assertEqual(self.sync_run([cand("alice")]), 0)
        self.assertIn(("remove", "browser-test"), self.docker.actions)
        self.assertIn(("rm network", "rb-unit_browser-test"), self.docker.actions)
        self.assertNotEqual(self.state()["alice"]["subnet"], "10.213.0.16/28")  # was still in use

    def test_phase2_compose_file_refused(self):
        self.write_env(COMPOSE_FILE=f"compose.yaml:compose.browser-test.yaml:{self.tmp}/generated/compose.users.yaml")
        with self.assertRaisesRegex(sync.SyncError, "browser-test"):
            sync.load_settings(self.env)
        self.write_env(COMPOSE_FILE="compose.yaml")
        with self.assertRaisesRegex(sync.SyncError, "must include"):
            sync.load_settings(self.env)

    # --- subnets ---------------------------------------------------------------------------------

    def test_subnets_stable_and_skip_used(self):
        self.docker = FakeDocker(self.settings, other_subnets=["10.213.0.0/24"])
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

    def test_generated_yaml_and_map(self):
        import yaml
        self.assertEqual(self.sync_run([cand("alice"), cand("bob-2")]), 0)
        data = yaml.safe_load(self.settings.compose_out.read_text())
        self.assertEqual(sorted(data["services"]), ["browser-alice", "browser-bob-2"])
        svc = data["services"]["browser-alice"]
        self.assertEqual(svc["hostname"], "browser-alice")
        self.assertEqual(svc["networks"], ["net-alice"])
        self.assertEqual(svc["environment"]["PUID"], str(ME))
        self.assertEqual(svc["expose"], ["3000"])
        self.assertNotIn("ports", svc)
        self.assertNotIn("privileged", svc)
        self.assertNotIn("docker.sock", self.settings.compose_out.read_text())
        self.assertEqual(svc["security_opt"], ["seccomp=./browser-image/seccomp-chrome.json"])
        self.assertEqual(self.map_users(), ["alice", "bob-2"])

    @unittest.skipUnless(shutil.which("docker") and subprocess.run(["docker", "info"], capture_output=True).returncode == 0,
                         "docker not available")
    def test_generated_compose_accepted_by_docker_compose(self):
        self.docker = sync.Docker(self.settings)
        self.docker.networks = lambda: []  # do not depend on this host's networks
        self.assertEqual(sync.run(self.settings, args(generate_only=True), docker=self.docker,
                                  candidates=[cand("alice"), cand("bob")]), 0)
        model = self.docker.model(self.settings.compose_files)
        self.assertEqual(model["services"]["browser-alice"]["volumes"][0]["source"],
                         str(self.tmp / "users" / "alice" / "config"))
        self.assertEqual(set(model["services"]["nginx"]["networks"]), {"frontend"})
        self.assertEqual(model["networks"]["net-bob"]["ipam"]["config"][0]["subnet"], "10.213.0.16/28")
        # Empty result (--force) still renders a valid file.
        self.assertEqual(sync.run(self.settings, args(generate_only=True, force=True), docker=self.docker,
                                  candidates=[]), 0)
        self.docker.model(self.settings.compose_files)


if __name__ == "__main__":
    unittest.main()
