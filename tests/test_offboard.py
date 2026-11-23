#!/usr/bin/python3
"""ODL-218: the offboard path — export, grant check, DNS, machine, row, backup.

Hermetic: the provider is the same recorded-transport fixture `test_odilectl`
uses, Cloudflare is a recorded transport behind `offboard.DNS_TRANSPORT`, and
the tenant's `/verify` is a string behind `offboard.VERIFY_FETCH`. The clock is
fixed so the 30-day retention date is exact.

    /usr/bin/python3 -m unittest tests.test_offboard -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odilectl import cli, offboard, state  # noqa: E402
from odilectl.providers import servury  # noqa: E402

SLUG = "odl-7f3a2c"
SERVER_ID = "abc12345"
TEST_KEY = "test-reseller-key"
ZONE = "odile.fyi"
NOW = 1794538000.0  # 2026-11-13T03:06:40Z
RECORD = {
    "id": "rec-1",
    "name": f"{SLUG}.{ZONE}",
    "type": "A",
    "content": "203.0.113.120",
    "ttl": 60,
    "proxied": False,
}
VERIFY_NONE = (
    b"read from your machine\n\n  usable keys    PASS  0 usable user keys\n"
    b"  operator key   PASS  none (authorized_keys: 1 line(s), the measured throwaway)\n"
)
VERIFY_LIVE = (
    b"read from your machine\n\n"
    b"  operator key   FAIL  authorized_keys carries ssh-ed25519 AAAA... odile-support-grant\n"
)


# --------------------------------------------------------------- recorded fixtures


class RecordedTransport:
    """Replays recorded provider payloads in order; unrecorded calls raise."""

    def __init__(self, routes):
        self.routes = {key: list(value) for key, value in routes.items()}
        self.calls = []

    def __call__(self, method, url, headers, body):
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        action = query.get("action", [None])[0]
        self.calls.append({"method": method, "action": action, "id": query.get("id", [None])[0]})
        queue = self.routes.get((method, action))
        if not queue:
            raise AssertionError(f"no recorded response for {method} action={action!r}")
        status, payload = queue.pop(0)
        return status, json.dumps(payload).encode("utf-8")


def machine_row(**overrides):
    row = {
        "id": SERVER_ID,
        "hostname": SLUG,
        "plan_id": 113,
        "plan": "Blackbox Plus",
        "os": "debian13",
        "ipv4_address": "203.0.113.120",
        "status": "active",
        "powered_on": True,
        "expiration_date": 1792000000,
        "expired": False,
    }
    row.update(overrides)
    return row


def servers_response(*rows):
    rows = rows or (machine_row(),)
    return {"success": True, "data": {"servers": list(rows), "total": len(rows)}}


def empty_servers():
    return {"success": True, "data": {"servers": [], "total": 0}}


class FakeDNS:
    """The zone-scoped Cloudflare calls the client makes, without the network."""

    def __init__(self, record=None):
        self.record = dict(record) if record else None
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append(method)
        if method == "GET":
            rows = [self.record] if self.record else []
            return 200, json.dumps({"success": True, "result": rows}).encode()
        if method == "DELETE":
            if self.record is None:
                return 404, json.dumps(
                    {"success": False, "errors": [{"message": "not found"}]}
                ).encode()
            self.record = None
            return 200, json.dumps({"success": True, "result": {"id": "rec-1"}}).encode()
        raise AssertionError(f"unexpected DNS {method} {url}")


class Seams(contextlib.AbstractContextManager):
    """Patch the module's three test seams and restore them on exit."""

    def __init__(self, *, dns=None, verify=None, now=NOW):
        self.dns = dns if dns is not None else FakeDNS()
        self.verify = verify if verify is not None else (lambda url: VERIFY_NONE)
        self.now = now

    def __enter__(self):
        self.saved = (offboard.DNS_TRANSPORT, offboard.VERIFY_FETCH, offboard.CLOCK, offboard.SLEEP)
        offboard.DNS_TRANSPORT = self.dns
        offboard.VERIFY_FETCH = self.verify
        offboard.CLOCK = lambda: self.now
        offboard.SLEEP = lambda _seconds: None
        return self

    def __exit__(self, *exc):
        offboard.DNS_TRANSPORT, offboard.VERIFY_FETCH, offboard.CLOCK, offboard.SLEEP = self.saved
        return False


# ------------------------------------------------------------------ the fixture


class OffboardTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="odile-offboard-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.db = self.directory / "state.db"
        self.bundle = self.make_bundle(self.directory / "odile-export.tar.gz")

    def make_config(self, **overrides) -> Path:
        path = self.directory / "config.yaml"
        values = {
            "provider": "servury",
            "reseller_key": TEST_KEY,
            "dns_token": "fixture-token",
            "dns_zone": ZONE,
            "dns_zone_id": "zone-1",
            "state_db": str(self.db),
            "default_plan_id": "113",
            "default_days": "31",
        }
        values.update(overrides)
        path.write_text(
            "".join(f"{key}: {value}\n" for key, value in values.items() if value is not None),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def make_bundle(self, path: Path, *, drop: str | None = None) -> Path:
        """A bundle with the four members the card names, minus `drop`."""
        members = {
            "AGENTS.md": b"# the tenant's agent\n",
            ".hermes/memories/memories.md": b"remembered\n",
            ".hermes/skills/odile-help/SKILL.md": b"# help\n",
            ".hermes/state.db": b"SQLite format 3\x00",
            "workspace/memo.md": b"a memo\n",
        }
        with tarfile.open(path, "w:gz") as archive:
            for name, body in members.items():
                if drop and name.startswith(drop):
                    continue
                info = tarfile.TarInfo(name)
                info.size = len(body)
                info.mode = 0o644
                import io as _io

                archive.addfile(info, _io.BytesIO(body))
        return path

    def activate_row(self, slug: str = SLUG) -> None:
        """A tenant row at `invited`, which may move straight to destroyed."""
        with state.StateStore.open(self.db) as store:
            store.invite(slug)

    def run_cli(self, argv, *, routes, dns=None, verify=None, factory=None, now=NOW, **config):
        transport = RecordedTransport(routes)
        provider = factory or (
            lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport)
        )
        out, err = io.StringIO(), io.StringIO()
        with Seams(dns=dns, verify=verify, now=now):
            code = cli.main(
                [*argv, "--config", str(self.make_config(**config))],
                provider_factory=provider,
                stdout=out,
                stderr=err,
            )
        return code, out.getvalue(), err.getvalue(), transport

    def rows(self):
        with state.StateStore.open(self.db) as store:
            return {tenant.slug: tenant for tenant in store.tenants()}, store_events(store)


def store_events(store):
    return store.events()


def explode(_config):
    raise AssertionError("this path must not build a provider")


# ---------------------------------------------------------------------- the plan


class PlanTest(OffboardTest):
    def test_the_plan_lists_every_step_and_writes_nothing(self):
        self.activate_row()
        dns = FakeDNS(RECORD)
        code, out, err, transport = self.run_cli(
            ["destroy", SLUG, "--export", str(self.bundle), "--dry-run", "--json"],
            routes={("GET", "servers"): [(200, servers_response())]},
            dns=dns,
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(
            [action["action"] for action in payload["actions"]],
            [
                "export.verify",
                "support.check",
                "dns.delete",
                "provider.terminate",
                "provider.absent",
                # ODL-242: the inference key is revoked after the machine is
                # gone and before the row is marked destroyed.
                "key.revoke",
                "state.destroy",
                "backup.retain",
            ],
        )
        self.assertEqual([call["action"] for call in transport.calls], ["servers"])
        self.assertEqual(dns.calls, ["GET"])
        self.assertEqual(dns.record, RECORD, "the dry run deleted the record")
        tenants, events = self.rows()
        self.assertEqual(tenants[SLUG].state, "invited")
        self.assertEqual([event.kind for event in events], ["invited"])

    def test_the_plan_names_the_bundle_the_grant_and_the_retention(self):
        self.activate_row()
        code, out, err, _ = self.run_cli(
            ["destroy", SLUG, "--export", str(self.bundle), "--json"],
            routes={("GET", "servers"): [(200, servers_response())]},
            dns=FakeDNS(RECORD),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["export"]["sha256"], offboard.inspect_bundle(self.bundle).sha256)
        self.assertEqual(payload["grant"]["status"], "none")
        expected = datetime.fromtimestamp(
            int(NOW) + 30 * 86400, timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(payload["backup"]["keep_until"], expected)
        self.assertFalse(payload["backup"]["due"])

    def test_the_dns_name_follows_tenant_domain_on_the_bench(self):
        """The same rule as `odilectl dns`: the name, not the zone, moves."""
        self.activate_row()
        dns = FakeDNS(RECORD)
        code, out, err, _ = self.run_cli(
            ["destroy", SLUG, "--json"],
            routes={("GET", "servers"): [(200, servers_response())]},
            dns=dns,
            tenant_domain="test.odile.fyi",
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["dns"]["name"], f"{SLUG}.test.odile.fyi")
        self.assertEqual(payload["dns"]["zone"], ZONE)
        self.assertEqual(payload["actions"][2]["name"], f"{SLUG}.test.odile.fyi")
        self.assertEqual(
            payload["grant"]["source"], f"https://{SLUG}.test.odile.fyi/verify"
        )

    def test_a_plan_without_a_bundle_says_so_and_still_lists_the_steps(self):
        code, out, err, transport = self.run_cli(
            ["destroy", SLUG, "--json"],
            routes={("GET", "servers"): [(200, servers_response())]},
            dns=FakeDNS(RECORD),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["actions"][0]["action"], "export.verify")
        self.assertIn("no bundle given", payload["actions"][0]["result"])
        self.assertTrue(any("--export" in note for note in payload["notes"]))


# --------------------------------------------------------------------- the apply


class ApplyTest(OffboardTest):
    def test_apply_verifies_the_bundle_deletes_dns_terminates_and_marks_the_row(self):
        self.activate_row()
        dns = FakeDNS(RECORD)
        code, out, err, transport = self.run_cli(
            ["destroy", SLUG, "--export", str(self.bundle), "--apply", "--json"],
            routes={
                ("GET", "servers"): [(200, servers_response()), (200, empty_servers())],
                ("POST", "terminate"): [(200, {"success": True, "data": {"message": "Server terminated."}})],
            },
            dns=dns,
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["destroyed"])
        self.assertEqual(payload["server_id"], SERVER_ID)
        self.assertEqual(payload["dns"]["action"], "deleted")
        self.assertIsNone(dns.record, "the A record survived the destroy")
        self.assertEqual(
            [(call["method"], call["action"]) for call in transport.calls],
            [("GET", "servers"), ("POST", "terminate"), ("GET", "servers")],
        )
        tenants, events = self.rows()
        tenant = tenants[SLUG]
        self.assertEqual(tenant.state, "destroyed")
        detail = tenant.detail
        self.assertEqual(detail["export"]["sha256"], offboard.inspect_bundle(self.bundle).sha256)
        self.assertEqual(detail["grant"]["status"], "none")
        self.assertEqual(detail["dns"]["action"], "deleted")
        self.assertEqual(detail["machine"]["server_id"], SERVER_ID)
        self.assertEqual(detail["backup"]["days"], 30)
        self.assertEqual([event.kind for event in events], ["invited", "destroyed"])
        destroyed_event = events[-1]
        self.assertEqual(destroyed_event.detail["export"]["entries"], 5)

    def test_a_second_apply_converges(self):
        self.activate_row()
        first = {
            ("GET", "servers"): [(200, servers_response()), (200, empty_servers())],
            ("POST", "terminate"): [(200, {"success": True, "data": {"message": "Server terminated."}})],
        }
        code, out, err, _ = self.run_cli(
            ["destroy", SLUG, "--export", str(self.bundle), "--apply", "--json"],
            routes={key: list(value) for key, value in first.items()},
            dns=FakeDNS(RECORD),
        )
        self.assertEqual(code, 0, err)
        second = {("GET", "servers"): [(200, empty_servers())]}
        code, out, err, transport = self.run_cli(
            ["destroy", SLUG, "--export", str(self.bundle), "--apply", "--json"],
            routes=second,
            dns=FakeDNS(),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertFalse(payload["destroyed"])
        self.assertTrue(payload["already_gone"])
        self.assertEqual(payload["dns"]["action"], "absent")
        self.assertEqual([call["action"] for call in transport.calls], ["servers"])
        tenants, events = self.rows()
        self.assertEqual(tenants[SLUG].state, "destroyed")
        self.assertEqual([event.kind for event in events], ["invited", "destroyed"])

    def test_a_second_run_keeps_the_recorded_deletion_date(self):
        """The 30 days run from the destroy, not from the last look."""
        self.activate_row()
        first = {
            ("GET", "servers"): [(200, servers_response()), (200, empty_servers())],
            ("POST", "terminate"): [(200, {"success": True, "data": {"message": "Server terminated."}})],
        }
        code, out, err, _ = self.run_cli(
            ["destroy", SLUG, "--export", str(self.bundle), "--apply", "--json"],
            routes={key: list(value) for key, value in first.items()},
            dns=FakeDNS(RECORD),
        )
        self.assertEqual(code, 0, err)
        recorded = json.loads(out)["backup"]["keep_until"]
        code, out, err, _ = self.run_cli(
            ["destroy", SLUG, "--export", str(self.bundle), "--apply", "--json"],
            routes={("GET", "servers"): [(200, empty_servers())]},
            dns=FakeDNS(),
            now=NOW + 86400,
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["backup"]["keep_until"], recorded)

    def test_a_404_from_the_terminate_call_is_already_gone(self):
        self.activate_row()
        code, out, err, _ = self.run_cli(
            ["destroy", SLUG, "--export", str(self.bundle), "--apply", "--json"],
            routes={
                ("GET", "servers"): [(200, servers_response()), (200, empty_servers())],
                ("POST", "terminate"): [(404, {"success": False, "error": "Server not found."})],
            },
            dns=FakeDNS(RECORD),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertFalse(payload["destroyed"])
        self.assertTrue(payload["already_gone"])
        tenants, _ = self.rows()
        self.assertEqual(tenants[SLUG].state, "destroyed")

    def test_a_machine_that_stays_listed_is_a_provider_error_and_the_row_stays_live(self):
        self.activate_row()
        code, out, err, _ = self.run_cli(
            ["destroy", SLUG, "--export", str(self.bundle), "--apply", "--wait", "0"],
            routes={
                ("GET", "servers"): [
                    (200, servers_response()),
                    (200, servers_response()),
                ],
                ("POST", "terminate"): [(200, {"success": True, "data": {"message": "Server terminated."}})],
            },
            dns=FakeDNS(RECORD),
        )
        self.assertEqual(code, 4, err)
        self.assertIn("still listed", err)
        tenants, events = self.rows()
        self.assertEqual(tenants[SLUG].state, "invited", "the row moved before the 404 was proved")
        self.assertEqual([event.kind for event in events], ["invited"])


# -------------------------------------------------------------------- refusals


class RefusalTest(OffboardTest):
    def test_apply_without_a_bundle_is_a_usage_error_before_any_call(self):
        code, out, err, _ = self.run_cli(
            ["destroy", SLUG, "--apply"], routes={}, factory=explode
        )
        self.assertEqual(code, 2)
        self.assertIn("--export", err)
        self.assertIn("--skip-export", err)

    def test_an_incomplete_bundle_is_refused_and_names_the_member(self):
        partial = self.make_bundle(self.directory / "partial.tar.gz", drop=".hermes/state.db")
        code, out, err, _ = self.run_cli(
            ["destroy", SLUG, "--export", str(partial), "--apply"],
            routes={},
            factory=explode,
        )
        self.assertEqual(code, 2)
        self.assertIn(".hermes/state.db", err)

    def test_a_live_grant_refuses_the_apply(self):
        self.activate_row()
        code, out, err, transport = self.run_cli(
            ["destroy", SLUG, "--export", str(self.bundle), "--apply", "--json"],
            routes={("GET", "servers"): [(200, servers_response())]},
            dns=FakeDNS(RECORD),
            verify=lambda url: VERIFY_LIVE,
        )
        self.assertEqual(code, 2, err)
        payload = json.loads(out)
        self.assertIn("live support grant", payload["error"])
        self.assertIn("odile-support revoke", payload["error"])
        self.assertEqual([call["action"] for call in transport.calls], ["servers"])
        tenants, _ = self.rows()
        self.assertEqual(tenants[SLUG].state, "invited")

    def test_force_destroys_under_a_live_grant(self):
        self.activate_row()
        code, out, err, _ = self.run_cli(
            ["destroy", SLUG, "--export", str(self.bundle), "--apply", "--force", "--json"],
            routes={
                ("GET", "servers"): [(200, servers_response()), (200, empty_servers())],
                ("POST", "terminate"): [(200, {"success": True, "data": {"message": "Server terminated."}})],
            },
            dns=FakeDNS(RECORD),
            verify=lambda url: VERIFY_LIVE,
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["grant"]["status"], "live")
        tenants, _ = self.rows()
        self.assertEqual(tenants[SLUG].state, "destroyed")

    def test_a_tenant_that_does_not_answer_verify_is_unknown_not_a_refusal(self):
        self.activate_row()

        def broken(url):
            raise OSError("no route to host")

        code, out, err, _ = self.run_cli(
            ["destroy", SLUG, "--export", str(self.bundle), "--apply", "--json"],
            routes={
                ("GET", "servers"): [(200, servers_response()), (200, empty_servers())],
                ("POST", "terminate"): [(200, {"success": True, "data": {"message": "Server terminated."}})],
            },
            dns=FakeDNS(RECORD),
            verify=broken,
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["grant"]["status"], "unknown")
        self.assertIn("no route to host", payload["grant"]["error"])


# ------------------------------------------------------------- absent machine, row


class AbsentTest(OffboardTest):
    def test_an_absent_machine_still_deletes_the_dns_record(self):
        dns = FakeDNS(RECORD)
        code, out, err, transport = self.run_cli(
            ["destroy", SLUG, "--export", str(self.bundle), "--apply", "--json"],
            routes={("GET", "servers"): [(200, empty_servers())]},
            dns=dns,
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["already_gone"])
        self.assertEqual(payload["dns"]["action"], "deleted")
        self.assertIsNone(dns.record)
        self.assertEqual([call["action"] for call in transport.calls], ["servers"])
        tenants, events = self.rows()
        self.assertEqual(tenants, {})
        self.assertEqual(events, ())

    def test_a_hostname_that_is_not_a_slug_has_no_record_and_still_dies(self):
        """A machine named outside the tenant grammar has no record; no crash."""
        code, out, err, transport = self.run_cli(
            ["destroy", "scratch-box", "--skip-export", "--apply", "--json"],
            routes={
                ("GET", "servers"): [
                    (200, {"success": True, "data": {"servers": [machine_row(hostname="scratch-box")], "total": 1}}),
                    (200, empty_servers()),
                ],
                ("POST", "terminate"): [(200, {"success": True, "data": {"message": "Server terminated."}})],
            },
            dns=FakeDNS(RECORD),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["dns"]["action"], "not a tenant slug")
        self.assertIsNone(payload["dns"]["name"])
        self.assertEqual(
            payload["grant"]["source"], "https://scratch-box.odile.fyi/verify"
        )
        self.assertTrue(payload["destroyed"])

    def test_skip_export_destroys_a_machine_that_never_held_data(self):
        code, out, err, _ = self.run_cli(
            ["destroy", SLUG, "--skip-export", "--apply", "--json"],
            routes={
                ("GET", "servers"): [(200, servers_response()), (200, empty_servers())],
                ("POST", "terminate"): [(200, {"success": True, "data": {"message": "Server terminated."}})],
            },
            dns=FakeDNS(RECORD),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertNotIn("export", payload)
        self.assertTrue(payload["destroyed"])


# ------------------------------------------------------------------- the export


class ExportTest(OffboardTest):
    def a_home(self) -> Path:
        """A small home with the members the program exports."""
        home = self.directory / "home"
        (home / ".hermes" / "memories").mkdir(parents=True)
        (home / ".hermes" / "skills" / "odile-help").mkdir(parents=True)
        (home / "AGENTS.md").write_text("# the tenant's agent\n", encoding="utf-8")
        (home / ".hermes" / "memories" / "memories.md").write_text("remembered\n", encoding="utf-8")
        (home / ".hermes" / "skills" / "odile-help" / "SKILL.md").write_text("# help\n", encoding="utf-8")
        return home

    def run_export_program(self, home: Path, *, path_prefix: Path | None = None):
        """Run EXPORT_PROGRAM itself against a fixture home, as the guest does."""
        program = self.directory / "odile-export"
        program.write_text(offboard.EXPORT_PROGRAM, encoding="utf-8")
        program.chmod(0o755)
        env = dict(os.environ)
        env["HOME"] = str(home)
        if path_prefix is not None:
            env["PATH"] = f"{path_prefix}:{env.get('PATH', '/usr/bin:/bin')}"
        return subprocess.run(
            ["/bin/sh", str(program), "odl-test"],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )

    def test_the_script_is_the_guest_program_byte_for_byte(self):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            ["export", SLUG, "--script", "--config", str(self.make_config())],
            provider_factory=explode,
            stdout=out,
            stderr=err,
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(out.getvalue(), offboard.EXPORT_PROGRAM)
        for member in ("AGENTS.md", ".hermes", "workspace", "state.db", "sha256"):
            self.assertIn(member, offboard.EXPORT_PROGRAM)

    def test_the_script_is_in_the_json_too(self):
        out = io.StringIO()
        with Seams():
            code = cli.main(
                ["export", SLUG, "--script", "--json", "--config", str(self.make_config())],
                provider_factory=explode,
                stdout=out,
                stderr=io.StringIO(),
            )
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["script"], offboard.EXPORT_PROGRAM)
        self.assertFalse(payload["dry_run"] is True and payload["command"] != "export")

    def test_verify_prints_the_listing_and_the_hash(self):
        code, out, err, _ = self.run_cli(
            ["export", SLUG, "--verify", str(self.bundle)], routes={}, factory=explode
        )
        self.assertEqual(code, 0, err)
        digest = offboard.inspect_bundle(self.bundle).sha256
        self.assertIn(digest, out)
        self.assertIn("AGENTS.md", out)
        self.assertIn("verdict:   complete", out)
        self.assertIn("listing:", out)

    def test_verify_refuses_an_incomplete_bundle(self):
        partial = self.make_bundle(self.directory / "partial2.tar.gz", drop=".hermes/skills")
        code, out, err, _ = self.run_cli(
            ["export", SLUG, "--verify", str(partial)], routes={}, factory=explode
        )
        self.assertEqual(code, 2)
        self.assertIn(".hermes/skills", err)

    def test_export_apply_without_verify_is_a_usage_error(self):
        code, out, err, _ = self.run_cli(
            ["export", SLUG, "--apply"], routes={}, factory=explode
        )
        self.assertEqual(code, 2)
        self.assertIn("--verify", err)

    def test_a_file_that_is_not_a_tar_is_a_usage_error(self):
        junk = self.directory / "not-a.tar.gz"
        junk.write_text("hello\n", encoding="utf-8")
        code, out, err, _ = self.run_cli(
            ["export", SLUG, "--verify", str(junk)], routes={}, factory=explode
        )
        self.assertEqual(code, 2)
        self.assertIn("not a gzipped tar", err)

    def test_a_tar_that_exits_non_zero_does_not_print_wrote(self):
        """`tar | tar` reports the extractor, so a tar read error still printed
        'wrote'. The program stages the archive as a file and checks its status."""
        home = self.a_home()
        stub = self.directory / "stub"
        stub.mkdir()
        tar = stub / "tar"
        tar.write_text(
            "#!/bin/sh\n"
            "# A tar that fails to read the home (exit 2) but still creates the\n"
            "# final archive, the way GNU tar reports a file that changed.\n"
            "archive=\"\"\n"
            "previous=\"\"\n"
            "create=0\n"
            "for arg in \"$@\"; do\n"
            "  if [ \"$arg\" = \"-cf\" ]; then exit 2; fi\n"
            "  if [ \"$arg\" = \"--create\" ]; then create=1; fi\n"
            "  if [ \"$previous\" = \"--file\" ]; then archive=\"$arg\"; fi\n"
            "  previous=\"$arg\"\n"
            "done\n"
            "if [ \"$create\" = \"1\" ] && [ -n \"$archive\" ]; then : > \"$archive\"; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        tar.chmod(0o755)
        result = self.run_export_program(home, path_prefix=stub)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("wrote ", result.stdout)

    def test_the_bundle_carries_a_consistent_kanban_db(self):
        """The task board is copied through SQLite's backup API, not raw beside
        its WAL: the bundled file opens alone and carries the uncheckpointed row."""
        home = self.a_home()
        kanban = home / ".hermes" / "kanban.db"
        writer = sqlite3.connect(kanban)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE task (id INTEGER PRIMARY KEY, title TEXT)")
        writer.execute("INSERT INTO task (title) VALUES ('only in the WAL at copy time')")
        writer.commit()
        self.assertTrue(
            Path(str(kanban) + "-wal").exists(), "the fixture's row must still be in the WAL"
        )

        result = self.run_export_program(home)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        wrote = next(
            line.split(" ", 1)[1].strip()
            for line in result.stdout.splitlines()
            if line.startswith("wrote ")
        )
        with tarfile.open(wrote, "r:gz") as archive:
            names = [name.removeprefix("./") for name in archive.getnames()]
            self.assertNotIn(".hermes/kanban.db-wal", names)
            self.assertNotIn(".hermes/kanban.db-shm", names)
            member = archive.extractfile("./.hermes/kanban.db")
            assert member is not None
            extracted = self.directory / "kanban-copy.db"
            extracted.write_bytes(member.read())
        copy = sqlite3.connect(extracted)
        try:
            count = copy.execute("SELECT count(*) FROM task").fetchone()[0]
        finally:
            copy.close()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
