#!/usr/bin/python3
"""Tests for ODL-220: a claim becomes a machine and a ready email.

Hermetic: a fixture provider (the same interface ODL-211 defines), a recorded
Cloudflare transport (the seam ODL-213 already tests through), and a throwaway
SQLite file. No socket except the loopback HTTP server the web-claim test
starts, no live database, no provider or DNS credential.

The tests are organised around the card's three lines:

* a claim creates exactly one VM record and one queued ready email;
* a failed provision leaves no billed resource and a clear state;
* retrying the claim does not create a second VM.

The evidence transcript is:

    /usr/bin/python3 tests/test_claim.py --transcript

The suite is:

    /usr/bin/python3 -m unittest tests.test_claim -v
"""

from __future__ import annotations

import email
from email import policy as email_policy
import http.client
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from dataclasses import replace
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from odilectl import claim, dns  # noqa: E402
from odilectl import cli as odilectl_cli  # noqa: E402
from odilectl.config import Config  # noqa: E402
from odilectl.errors import ConfigError, ProviderError, UsageError  # noqa: E402
from odilectl.providers.base import Machine, Provider  # noqa: E402

# A real ed25519 public key; the private half was generated for the ODL-211
# fixture and thrown away. A public key is not a secret.
KEY_TEXT = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICFh7hQkST5vRDolO3yqwCARRugZahMdc+WgR69JsoXG "
    "odile-fixture@example.com"
)
KEY_TEXT_2 = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIL7Tzl2nYSnGVbP/Vfp9wJwD7q2Wt2mJKT5p8g4oWn7Z "
    "second-fixture@example.com"
)
PRIVATE_KEY = """-----BEGIN OPENSSH PRIVATE KEY-----
[REDACTED PRIVATE KEY]
-----END OPENSSH PRIVATE KEY-----
"""
MALFORMED_KEY = "ssh-ed25519 not-base64 odile-fixture@example.com"

SLUG = "odl-7f3a2c"
IPV4 = "203.0.113.120"
ZONE = "odile.fyi"
ZONE_ID = "fixture-zone-id"
# The account site the invitation email names; the kit prints `<host>/forgot`
# when the operator records it in the config.
ACCOUNT_HOST = "https://test.odile.fyi"
NOW = int(datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc).timestamp())
MACHINE_EXPIRES = int(datetime(2026, 12, 11, 12, 0, tzinfo=timezone.utc).timestamp())
RELEASE = "2026.11.1"
# The re-ordered machine's pin (ODL-947): a rebuild under a newer release, so
# the web row written `COALESCE` at the first order has to be moved, not kept.
NEXT_RELEASE = "2026.12.0"
# What the provider claims its machine booted. ODL-398's rule: an observation,
# never an instruction — the pin's `provision_release` is the only source.
PROVIDER_RELEASE = "2026.09.9"
MEASUREMENT = "ab" * 48


def at(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc)


# --------------------------------------------------------------------- fixtures


class FixtureProvider(Provider):
    """The ODL-211 interface, in memory: one create, one poll, one list."""

    name = "fixture"

    def __init__(self, *, fail_poll: bool = False, fail_create: bool = False,
                 ipv4: str = IPV4, expires_at: int = MACHINE_EXPIRES,
                 measured_release: str | None = PROVIDER_RELEASE) -> None:
        self.machines: dict[str, Machine] = {}
        self.calls: list[tuple] = []
        self.created = 0
        self.fail_poll = fail_poll
        self.fail_create = fail_create
        self.ipv4 = ipv4
        self.expires_at = expires_at
        self.measured_release = measured_release

    def create(self, spec) -> Machine:
        self.calls.append(("create", spec.hostname, spec.ssh_key))
        if self.fail_create:
            raise ProviderError("fixture: no stock")
        self.created += 1
        server_id = f"srv-fixture{self.created:04d}"
        detail = (
            {"measured_release": self.measured_release}
            if self.measured_release is not None else {}
        )
        machine = Machine(
            server_id=server_id,
            hostname=spec.hostname,
            state="pending",
            plan_id=spec.plan_id,
            expires_at=self.expires_at,
            raw_status="deploying",
            detail=detail,
        )
        self.machines[server_id] = machine
        return machine

    def poll(self, server_id: str, *, timeout: float = 900.0,
             interval: float = 10.0) -> Machine:
        self.calls.append(("poll", server_id))
        machine = self.machines[server_id]
        if self.fail_poll:
            self.machines[server_id] = replace(
                machine, state="failed", raw_status="failed"
            )
            raise ProviderError("fixture: the build failed")
        machine = replace(
            machine, state="active", ipv4=self.ipv4, raw_status="active"
        )
        self.machines[server_id] = machine
        return machine

    def power(self, server_id: str, action: str) -> Machine:
        raise NotImplementedError("the claim pipeline never powers a machine")

    def terminate(self, server_id: str) -> bool:
        self.calls.append(("terminate", server_id))
        machine = self.machines.get(server_id)
        if machine is None:
            return False
        self.machines[server_id] = replace(
            machine, state="terminated", ipv4=None, raw_status="terminated"
        )
        return True

    def list(self) -> list[Machine]:
        return list(self.machines.values())

    def live(self) -> list[Machine]:
        return [m for m in self.machines.values() if m.state not in ("terminated", "failed")]


class CloudflareFixture:
    """A tiny stateful Cloudflare: enough of the API for A records, no socket."""

    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_create = False
        self._next = 0

    def _ok(self, result):
        return 200, json.dumps({"success": True, "result": result, "errors": []}).encode()

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None):
        parts = urllib.parse.urlsplit(url)
        path = parts.path.replace("/client/v4", "")
        query = urllib.parse.parse_qs(parts.query)
        self.calls.append((method, path))
        if method == "GET" and path == "/zones":
            return self._ok([{"id": ZONE_ID, "name": ZONE}])
        if method == "GET" and path == f"/zones/{ZONE_ID}/dns_records":
            name = query.get("name", [""])[0]
            rows = [
                row for row in self.records.values()
                if row["type"] == "A" and row["name"].lower() == name.lower()
            ]
            return self._ok(rows)
        if method == "POST" and path == f"/zones/{ZONE_ID}/dns_records":
            assert body is not None
            if self.fail_create:
                return 400, json.dumps({
                    "success": False, "result": None,
                    "errors": [{"message": "fixture: DNS write refused"}],
                }).encode()
            self._next += 1
            row = json.loads(body)
            row["id"] = f"rec-{self._next}"
            self.records[row["id"]] = row
            return self._ok(row)
        if method == "PATCH" and path.startswith(f"/zones/{ZONE_ID}/dns_records/"):
            assert body is not None
            record_id = path.rsplit("/", 1)[1]
            row = json.loads(body)
            row["id"] = record_id
            self.records[record_id] = row
            return self._ok(row)
        if method == "DELETE" and path.startswith(f"/zones/{ZONE_ID}/dns_records/"):
            record_id = path.rsplit("/", 1)[1]
            self.records.pop(record_id, None)
            return self._ok({"id": record_id})
        return 404, json.dumps({
            "success": False, "result": None,
            "errors": [{"message": f"no fixture route for {method} {path}"}],
        }).encode()


# --------------------------------------------------------------------- base


class ClaimBase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="odile-claim-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.db_path = self.directory / "waitlist.db"
        self.outbox_path = self.directory / "outbox"
        self.config_path = self.directory / "config.yaml"
        # The release pin is a fixture file, not the repo's: a test that a
        # provider's measured_release is ignored must control both sources.
        self.pin_path = self.directory / "odile-verify.pin"
        self.write_pin()
        self.write_config()
        self.config = Config.load(self.config_path)
        self.con = claim.open_db(self.db_path)
        self.addCleanup(self.con.close)
        self.cloud = CloudflareFixture()
        self.dns_client = dns.CloudflareDNS(
            "fixture-dns-token", ZONE, zone_id=ZONE_ID, transport=self.cloud
        )

    def write_pin(self, release: str | None = RELEASE) -> None:
        """A fixture pin; `release=None` leaves `provision_release` out."""
        document: dict = {"schema": 2, "measurement": MEASUREMENT, "releases": {}}
        if release is not None:
            document["provision_release"] = release
            document["releases"][release] = {}
        self.pin_path.write_text(json.dumps(document), encoding="utf-8")

    def write_config(self, **overrides) -> None:
        values = {
            "provider": "servury",
            "reseller_key": "fixture-reseller-key",
            "dns_token": "fixture-dns-token",
            "dns_zone": ZONE,
            "dns_zone_id": ZONE_ID,
            "default_plan_id": "113",
            "default_days": "31",
            "default_plan_name": "Blackbox Plus",
            "state_db": str(self.db_path),
            "outbox": str(self.outbox_path),
            "alert_email": "ops@example.com",
            "backup_repo": "sftp://backup@example.invalid/odile/{slug}",
            "release_pin": str(self.pin_path),
            # ODL-398: only the pin is a source. A stale `release:` key left
            # in the file must not win over the pin's provision_release.
            "release": PROVIDER_RELEASE,
            "expected_measurement": MEASUREMENT,
        }
        values.update(overrides)
        self.config_path.write_text(
            "".join(f"{key}: {value}\n" for key, value in values.items() if value is not None),
            encoding="utf-8",
        )
        self.config_path.chmod(0o600)

    def paid_claim(self) -> claim.IssuedClaim:
        issued = self.issue()
        self.accept(issued)
        return issued

    def issue(self, *, plan: str | None = "Blackbox Plus", now: int = NOW) -> claim.IssuedClaim:
        return claim.issue_claim(
            self.con, "ada@example.com", plan=plan, slug=SLUG, now=at(now)
        )

    def accept(self, issued: claim.IssuedClaim, *, key: str = KEY_TEXT,
               now: int = NOW) -> claim.ClaimResult:
        return claim.accept(self.con, issued.token, key, now=at(now))

    def provision(self, provider: FixtureProvider, *, now: int = NOW,
                  slug: str = SLUG) -> claim.ProvisionResult:
        return claim.provision_tenant(
            self.con,
            slug,
            provider=provider,
            config=self.config,
            dns_client=self.dns_client,
            outbox=claim.ReadyOutbox(self.outbox_path),
            now=now,
            interval=0.0,
        )

    def ready_files(self) -> list[Path]:
        directory = self.outbox_path / "ready"
        return sorted(directory.iterdir()) if directory.exists() else []

    def alerts(self) -> list[dict]:
        path = self.outbox_path / "alerts.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def events(self) -> list[sqlite3.Row]:
        return self.con.execute("SELECT * FROM events ORDER BY id").fetchall()

    def tenant(self) -> sqlite3.Row:
        return self.con.execute("SELECT * FROM tenants WHERE slug = ?", (SLUG,)).fetchone()

    def spine_release(self) -> str | None:
        return self.con.execute(
            "SELECT release FROM tenant_state WHERE slug = ?", (SLUG,)
        ).fetchone()["release"]

    def state(self) -> str:
        return self.con.execute(
            "SELECT state FROM tenant_state WHERE slug = ?", (SLUG,)
        ).fetchone()["state"]


# --------------------------------------------------------------------- schema


class SchemaTests(ClaimBase):
    def test_open_db_creates_the_additive_tables_and_is_idempotent(self) -> None:
        tables = {
            row[0] for row in self.con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertLessEqual(
            {"waitlist", "tenants", "claims", "events", "grants",
             "seed_tokens", "tenant_state", "tenant_state_events"},
            tables,
        )
        claim.open_db(self.db_path).close()
        again = {
            row[0] for row in self.con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertEqual(tables, again)

    def test_the_service_boot_creates_the_same_tables(self) -> None:
        path = self.directory / "boot.db"
        original = app.DB_PATH
        app.DB_PATH = str(path)
        try:
            app.init_db()
        finally:
            app.DB_PATH = original
        con = sqlite3.connect(path)
        try:
            tables = {
                row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            con.close()
        self.assertLessEqual({"tenants", "claims", "seed_tokens", "tenant_state"}, tables)

    def test_claim_status_reads_a_tenant_row_that_predates_the_release_column(self) -> None:
        """`claim status` is read-only; it must not require the v3 migration."""
        issued = self.issue()
        self.accept(issued)
        self.con.execute("ALTER TABLE tenants DROP COLUMN release")
        self.con.commit()
        status = claim.tenant_status(
            self.con, SLUG, outbox=claim.ReadyOutbox(self.outbox_path)
        )
        self.assertIsNone(status["release"])

    def test_no_private_key_or_password_column(self) -> None:
        banned = re.compile(r"pass(word|phrase)?|private|secret", re.I)
        columns = [row[1] for row in self.con.execute("PRAGMA table_info(tenants)")]
        for column in columns:
            self.assertIsNone(banned.search(column), f"tenants.{column} stores a secret")
        self.assertIn("ssh_public_key", columns)
        self.assertIn("setup_token_hash", columns)


# --------------------------------------------------------------------- invite


class InviteTests(ClaimBase):
    def test_an_invite_creates_one_tenant_and_one_claim_token(self) -> None:
        issued = self.issue()
        tenant = self.tenant()
        self.assertEqual(tenant["state"], "invited")
        self.assertEqual(tenant["email"], "ada@example.com")
        self.assertEqual(tenant["plan"], "Blackbox Plus")
        self.assertEqual(len(self.con.execute("SELECT * FROM claims").fetchall()), 1)
        stored = self.con.execute("SELECT token_hash FROM claims").fetchone()[0]
        self.assertEqual(stored, claim.hash_token(issued.token))
        self.assertNotIn(issued.token, stored)
        self.assertEqual(
            self.con.execute("SELECT state FROM tenant_state").fetchone()[0], "invited"
        )
        self.assertEqual([row["kind"] for row in self.events()], ["invited"])

    def test_a_reinvite_reuses_the_tenant_and_revokes_the_old_token(self) -> None:
        first = self.issue()
        second = claim.issue_claim(
            self.con, "Ada@Example.com", plan="Blackbox Plus", now=at(NOW + 60)
        )
        self.assertTrue(second.reissued)
        self.assertEqual(second.slug, first.slug)
        self.assertEqual(self.con.execute("SELECT count(*) FROM tenants").fetchone()[0], 1)
        self.assertIsNotNone(
            self.con.execute(
                "SELECT used_at FROM claims WHERE token_hash = ?",
                (claim.hash_token(first.token),),
            ).fetchone()[0]
        )
        self.assertIsNone(
            self.con.execute(
                "SELECT used_at FROM claims WHERE token_hash = ?",
                (claim.hash_token(second.token),),
            ).fetchone()[0]
        )
        self.accept(second, now=NOW + 120)
        with self.assertRaises(claim.ClaimRefused) as refused:
            self.accept(first, now=NOW + 180)
        self.assertEqual(refused.exception.status, 409)

    def test_a_bad_email_is_refused(self) -> None:
        with self.assertRaises(claim.ClaimRefused) as refused:
            claim.issue_claim(self.con, "not-an-address", now=at(NOW))
        self.assertEqual((refused.exception.status, refused.exception.reason),
                         (422, "bad_email"))
        self.assertEqual(self.con.execute("SELECT count(*) FROM tenants").fetchone()[0], 0)

    def test_an_invite_refuses_a_settled_tenant(self) -> None:
        self.issue()
        self.con.execute("UPDATE tenants SET state = 'active' WHERE slug = ?", (SLUG,))
        self.con.commit()
        with self.assertRaises(claim.ClaimRefused) as refused:
            self.issue()
        self.assertEqual(refused.exception.reason, "already_settled")


# --------------------------------------------------------------------- accept


class AcceptTests(ClaimBase):
    def test_a_valid_key_claims_the_tenant_and_spends_the_token(self) -> None:
        issued = self.issue()
        result = self.accept(issued)
        tenant = self.tenant()
        self.assertFalse(result.replay)
        self.assertEqual(tenant["state"], "claimed")
        self.assertEqual(tenant["ssh_public_key"], KEY_TEXT)
        self.assertEqual(tenant["claimed_at"], "2026-11-09T12:00:00Z")
        self.assertIsNotNone(
            self.con.execute("SELECT used_at FROM claims").fetchone()[0]
        )
        self.assertEqual(
            self.con.execute("SELECT state FROM tenant_state").fetchone()[0], "claimed"
        )
        kinds = [row["kind"] for row in self.events()]
        self.assertEqual(kinds, ["invited", "claimed"])
        self.assertEqual(
            json.loads(self.events()[-1]["detail"])["key_fingerprint"],
            result.key_fingerprint,
        )
        self.assertTrue(result.key_fingerprint.startswith("SHA256:"))

    def test_the_fingerprint_matches_ssh_keygen(self) -> None:
        if shutil.which("ssh-keygen") is None:
            self.skipTest("ssh-keygen is not installed")
        path = self.directory / "key.pub"
        path.write_text(KEY_TEXT + "\n", encoding="utf-8")
        out = subprocess.run(
            ["ssh-keygen", "-lf", str(path)], capture_output=True, text=True, check=True
        ).stdout
        self.assertIn(claim.ssh_fingerprint(KEY_TEXT), out)

    def test_a_private_key_is_refused_before_the_token_is_spent(self) -> None:
        # The fixture is the redaction marker between real OpenSSH markers: no
        # key body in the tree, and `ssh-keygen -lf` still rejects the block.
        self.assertIn("[REDACTED PRIVATE KEY]", PRIVATE_KEY)
        issued = self.issue()
        with self.assertRaises(claim.ClaimRefused) as refused:
            self.accept(issued, key=PRIVATE_KEY)
        self.assertEqual((refused.exception.status, refused.exception.reason),
                         (422, "bad_key"))
        self.assertIsNone(self.con.execute("SELECT used_at FROM claims").fetchone()[0])
        self.assertEqual(self.tenant()["state"], "invited")

    def test_a_malformed_key_is_refused(self) -> None:
        issued = self.issue()
        with self.assertRaises(claim.ClaimRefused) as refused:
            self.accept(issued, key=MALFORMED_KEY)
        self.assertEqual(refused.exception.status, 422)
        self.assertIsNone(self.con.execute("SELECT used_at FROM claims").fetchone()[0])

    def test_an_unknown_token_is_refused(self) -> None:
        with self.assertRaises(claim.ClaimRefused) as refused:
            claim.accept(self.con, "0" * 48, KEY_TEXT, now=at(NOW))
        self.assertEqual((refused.exception.status, refused.exception.reason),
                         (404, "unknown_claim"))

    def test_an_expired_token_is_refused(self) -> None:
        issued = self.issue()
        with self.assertRaises(claim.ClaimRefused) as refused:
            claim.accept(self.con, issued.token, KEY_TEXT,
                         now=at(NOW + 8 * 24 * 3600))
        self.assertEqual((refused.exception.status, refused.exception.reason),
                         (410, "expired"))
        self.assertIsNone(self.con.execute("SELECT used_at FROM claims").fetchone()[0])

    def test_the_same_key_replays_and_a_different_key_is_refused(self) -> None:
        issued = self.issue()
        first = self.accept(issued)
        second = self.accept(issued, now=NOW + 60)
        self.assertTrue(second.replay)
        self.assertEqual(second.slug, first.slug)
        self.assertEqual(len([r for r in self.events() if r["kind"] == "claimed"]), 1)
        with self.assertRaises(claim.ClaimRefused) as refused:
            self.accept(issued, key=KEY_TEXT_2, now=NOW + 120)
        self.assertEqual((refused.exception.status, refused.exception.reason),
                         (409, "already_used"))
        self.assertEqual(self.tenant()["ssh_public_key"], KEY_TEXT)


# --------------------------------------------------------------------- provision


class ProvisionTests(ClaimBase):
    def test_one_claim_creates_one_machine_and_one_queued_email(self) -> None:
        issued = self.paid_claim()
        provider = FixtureProvider()
        result = self.provision(provider)

        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.created)
        self.assertEqual([call for call in provider.calls if call[0] == "create"], [
            ("create", SLUG, KEY_TEXT)
        ])
        self.assertEqual(len(provider.machines), 1)
        machine = provider.machines["srv-fixture0001"]
        self.assertEqual(machine.state, "active")
        self.assertEqual(len(provider.live()), 1)

        tenant = self.tenant()
        self.assertEqual(tenant["state"], "provisioned")
        self.assertEqual(tenant["server_id"], "srv-fixture0001")
        self.assertEqual(tenant["ipv4"], IPV4)
        self.assertEqual(tenant["expires_at"], "2026-12-11T12:00:00Z")

        # DNS: the A record exists, DNS-only, with the provisioning TTL.
        self.assertEqual(len(self.cloud.records), 1)
        record = next(iter(self.cloud.records.values()))
        self.assertEqual(record["name"], f"{SLUG}.{ZONE}")
        self.assertEqual(record["content"], IPV4)
        self.assertEqual(record["ttl"], dns.PROVISION_TTL)
        self.assertFalse(record["proxied"])

        # One live setup token, bound to the machine, only its hash at rest.
        tokens = self.con.execute("SELECT * FROM seed_tokens").fetchall()
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0]["instance_id"], "srv-fixture0001")
        self.assertIsNone(tokens[0]["used_at"])
        self.assertEqual(tenant["setup_token_hash"], tokens[0]["token_hash"])

        # One queued ready email with the kit attached.
        files = self.ready_files()
        self.assertEqual(len([p for p in files if p.suffix == ".eml"]), 1)
        self.assertIsNotNone(result.email_path)
        queued = email.message_from_bytes(
            Path(result.email_path).read_bytes(), policy=email_policy.default
        )
        self.assertEqual(queued["To"], "ada@example.com")
        self.assertEqual(queued["Subject"], claim.READY_SUBJECT)
        self.assertEqual(queued["From"], claim.READY_SENDER)
        body = queued.get_body(preferencelist=("plain",)).get_content()
        self.assertIn(f"URL https://{SLUG}.{ZONE}", body)
        self.assertIn(f"Measurement {MEASUREMENT}", body)
        self.assertIn(f"The first-visit guide is at {claim.READY_GUIDE_URL}", body)
        self.assertEqual(
            len(
                [
                    line
                    for line in body.splitlines()
                    if line.startswith("Sign in:")
                ]
            ),
            1,
        )
        self.assertEqual(
            [line for line in body.splitlines() if line.startswith("Open ")], []
        )
        for old in ("IPv4", "Host ", "Release ", "Setup token", "ssh",
                    "passphrase", "encrypt", "unlock", "installer", "scp", "shred"):
            self.assertNotIn(old, body)
        kit_text = Path(result.kit_path).read_text(encoding="utf-8")
        # B-once (ODL-925): the email names the tokenless page and the password
        # chosen at registration; no first-visit link carries the setup token.
        self.assertIn(
            f"Sign in: https://{SLUG}.{ZONE}/first-visit — the password you chose "
            "when you registered.", body)
        # D-7 once, then D-10's line (brand round: the duplicate was the
        # earlier "Check the report first" sentence, now gone).
        self.assertEqual(body.count("it says PASS or FAIL"), 1)
        self.assertIn("Check the report before you sign in; it says PASS or FAIL. "
                      "That password opens\n"
                      "your machine too.", body)
        self.assertIn("That password opens", body)
        self.assertNotIn("Check the report first", body)
        self.assertEqual(body.count("first-visit?t="), 0)
        self.assertEqual(body.count("?t="), 0)
        self.assertNotIn("Set your sign-in", body)
        self.assertEqual(tokens[0]["token_hash"], tenant["setup_token_hash"])
        attachments = list(queued.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(),
                         f"odile-{SLUG}-recovery-kit.txt")
        attached = attachments[0].get_payload(decode=True).decode("utf-8")
        self.assertEqual(kit_text, attached)
        # The attached kit names the same tokenless page and never prints the
        # setup token: its two jobs are the seed exchange and the rebuild seal.
        self.assertIn(f"Open https://{SLUG}.{ZONE}/first-visit.",
                      " ".join(kit_text.split()))
        self.assertNotIn("?t=", kit_text)
        self.assertNotIn("Set your sign-in", kit_text)
        self.assertNotIn("Setup token:", kit_text)
        self.assertNotIn("Token expires", kit_text)
        for old in ("ssh", "scp", "ssh-keygen", "passphrase", "installer",
                    "shred", "~/.ssh"):
            self.assertNotIn(old, kit_text.lower())
        # The kit reads check -> sign in (the decided order) and the masthead
        # is sentence case.
        self.assertLess(kit_text.index("Before you sign in"),
                        kit_text.index("\nThe first sign-in\n"))
        self.assertTrue(kit_text.startswith(f"Odile recovery kit — {SLUG}\n"))
        self.assertNotIn("ODILE RECOVERY KIT", kit_text)
        self.assertRegex(kit_text, rf"(?m)^Measurement:\s+{MEASUREMENT}$")
        self.assertRegex(kit_text, r"(?m)^Machine ID:\s+srv-fixture0001$")
        self.assertIn(f"Machine:                {SLUG}", kit_text)
        self.assertIn(f"Address:                {IPV4}", kit_text)
        self.assertEqual(os.stat(result.kit_path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(result.email_path).st_mode & 0o777, 0o600)

        # The state spine says the machine now waits for its first boot.
        self.assertEqual(
            self.con.execute("SELECT state FROM tenant_state").fetchone()[0],
            "first_boot",
        )
        steps = [json.loads(row["detail"]).get("step") for row in self.events()]
        self.assertEqual(steps, [None, None, "vm", "ready"])

    def test_the_claim_records_the_pin_release_not_the_providers(self) -> None:
        """ODL-398: the pin's provision_release is recorded with the tenant.

        The provider's `measured_release` is visible in the order's detail and
        must never become the tenant's release: only the pin (and later the
        guest's heartbeat) is a source.
        """
        self.paid_claim()
        provider = FixtureProvider()  # reports measured_release 2026.09.9
        result = self.provision(provider)
        self.assertTrue(result.ok, result.reason)

        self.assertEqual(result.machine["detail"]["measured_release"], PROVIDER_RELEASE)
        # The web row ODL-232 reads for the ready email's pinned release id.
        self.assertEqual(self.tenant()["release"], RELEASE)
        # The state row `odilectl status` reads for the intended release.
        state = self.con.execute(
            "SELECT release FROM tenant_state WHERE slug = ?", (SLUG,)
        ).fetchone()
        self.assertEqual(state["release"], RELEASE)
        self.assertNotEqual(self.tenant()["release"], PROVIDER_RELEASE)

        kit_text = Path(result.kit_path).read_text(encoding="utf-8")
        self.assertRegex(kit_text, rf"(?m)^Release:\s+{re.escape(RELEASE)}$")
        facts = json.loads(
            Path(result.email_path).with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(facts["release"], RELEASE)

        # A second run with a different provider report is the same success
        # and never rewrites the release the machine was ordered under.
        again = FixtureProvider(measured_release="2026.12.0")
        replay = self.provision(again, now=NOW + 60)
        self.assertTrue(replay.skipped)
        self.assertEqual(self.tenant()["release"], RELEASE)
        self.assertEqual(
            self.con.execute(
                "SELECT release FROM tenant_state WHERE slug = ?", (SLUG,)
            ).fetchone()["release"],
            RELEASE,
        )

    def test_a_rebuild_orders_the_new_release_on_both_rows(self) -> None:
        """ODL-947: a rebuilt machine's pin moves the web row too.

        The shape is a rebuild: the first machine is destroyed, the spine row
        is reopened (its machine columns and detail go, so no boot record
        survives), and the worker orders the replacement under a newer pin. The
        web row's release was written `COALESCE` at the first order and nothing
        after it touches the column, so the state advance is the write that has
        to bring the two rows together. Until this card it did not: the gate
        skipped a spine row that already had a release, which `store.provision`
        had just set to the new pin, and the account page, the ready mail and
        the daily brief kept reading the dead machine's release.
        """
        from odilectl.state import StateStore

        self.paid_claim()
        first = self.provision(FixtureProvider())
        self.assertTrue(first.ok, first.reason)
        self.assertEqual(self.tenant()["release"], RELEASE)
        self.assertEqual(self.spine_release(), RELEASE)

        store = StateStore(self.con)
        store.destroy(SLUG, reason="the fixture machine died")
        store.reopen(SLUG, reason="replacement machine")
        self.assertEqual(self.state(), "claimed")
        # The operator marks the dead machine on the web row: it is no longer a
        # ready tenant, so the worker orders rather than skips.
        self.con.execute("UPDATE tenants SET state = 'churned' WHERE slug = ?", (SLUG,))
        self.con.commit()
        self.write_pin(release=NEXT_RELEASE)

        second = self.provision(FixtureProvider(ipv4="203.0.113.121"), now=NOW + 600)
        self.assertTrue(second.ok, second.reason)
        self.assertFalse(second.skipped)

        self.assertEqual(self.tenant()["release"], NEXT_RELEASE)
        self.assertEqual(self.spine_release(), NEXT_RELEASE)
        assert second.email_path is not None
        facts = json.loads(
            Path(second.email_path).with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(facts["release"], NEXT_RELEASE)

    def test_a_repeat_provision_agrees_the_rows_on_the_spine(self) -> None:
        """ODL-947: the skip path never replays the web row's stale release.

        A rebuild can leave the web row on the dead machine's release (the
        `COALESCE` order write) while the spine names the new order. A re-run
        of provision skips — the machine is ordered and the ready mail queued —
        and the skip path re-advances the state. The spine's release is the
        newer record, so it is the value the web row converges to; passing the
        web row's value as the order's release would walk the spine backwards.
        """
        self.paid_claim()
        first = self.provision(FixtureProvider())
        self.assertTrue(first.ok, first.reason)
        # The shape to repair: the spine has the new order, the web row the
        # dead machine's release.
        self.con.execute("UPDATE tenants SET release = ? WHERE slug = ?", (RELEASE, SLUG))
        self.con.execute(
            "UPDATE tenant_state SET release = ? WHERE slug = ?", (NEXT_RELEASE, SLUG)
        )
        self.con.commit()

        replay = self.provision(FixtureProvider(), now=NOW + 60)
        self.assertTrue(replay.skipped)

        self.assertEqual(self.spine_release(), NEXT_RELEASE)
        self.assertEqual(self.tenant()["release"], NEXT_RELEASE)
        # A second skip is the convergence check: nothing moves.
        third = self.provision(FixtureProvider(), now=NOW + 120)
        self.assertTrue(third.skipped)
        self.assertEqual(self.spine_release(), NEXT_RELEASE)
        self.assertEqual(self.tenant()["release"], NEXT_RELEASE)

    def test_a_pin_without_provision_release_orders_nothing(self) -> None:
        """An unpinned boot release fails closed before the provider is called."""
        self.paid_claim()
        self.write_pin(release=None)
        provider = FixtureProvider()
        with self.assertRaises(UsageError) as refused:
            self.provision(provider)
        self.assertIn("provision_release", str(refused.exception))
        self.assertIn("unpinned", str(refused.exception))
        self.assertEqual(provider.calls, [])
        self.assertEqual(provider.created, 0)
        self.assertEqual(self.tenant()["state"], "claimed")
        self.assertIsNone(self.tenant()["release"])
        self.assertEqual(
            self.con.execute(
                "SELECT count(*) FROM tenant_state WHERE server_id IS NOT NULL"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(self.ready_files(), [])
        self.assertEqual(self.cloud.records, {})

    def test_a_pin_release_that_is_not_an_id_orders_nothing(self) -> None:
        self.paid_claim()
        self.write_pin(release="latest")
        provider = FixtureProvider()
        with self.assertRaises(UsageError) as refused:
            self.provision(provider)
        self.assertIn("not a release id", str(refused.exception))
        self.assertEqual(provider.calls, [])
        self.assertIsNone(self.tenant()["release"])

    def test_odilectl_status_reports_the_claim_recorded_release(self) -> None:
        """The operator's `odilectl status` reads the same pin the worker wrote."""
        self.paid_claim()
        provider = FixtureProvider()
        result = self.provision(provider)
        self.assertTrue(result.ok, result.reason)

        out, err = io.StringIO(), io.StringIO()
        code = odilectl_cli.main(
            [
                "--config", str(self.config_path),
                "status", SLUG,
                "--db", str(self.db_path),
                "--json",
            ],
            provider_factory=lambda _config: provider,
            stdout=out,
            stderr=err,
        )
        self.assertEqual(code, 0, err.getvalue())
        payload = json.loads(out.getvalue())
        release = payload["machines"][0]["release"]
        self.assertEqual(release["intended"], RELEASE)
        # No heartbeat has arrived yet, so the guest's side is silent: unknown,
        # not drift. A real heartbeat is ODL-398's other half.
        self.assertEqual(release["status"], "unknown")

    def test_an_invalid_measurement_spends_nothing(self) -> None:
        self.paid_claim()
        self.write_config(expected_measurement="not-hex")
        self.config = Config.load(self.config_path)
        provider = FixtureProvider()
        with self.assertRaises(ConfigError):
            self.provision(provider)
        self.assertEqual(provider.calls, [])
        self.assertEqual(provider.created, 0)
        self.assertEqual(self.tenant()["state"], "claimed")

    def test_a_second_run_does_not_create_a_second_machine_or_email(self) -> None:
        self.paid_claim()
        first = FixtureProvider()
        self.provision(first)
        second = FixtureProvider()
        result = self.provision(second)
        self.assertTrue(result.ok)
        self.assertTrue(result.skipped)
        self.assertEqual([call for call in second.calls if call[0] == "create"], [])
        self.assertEqual(len(self.ready_files()), 3)
        self.assertEqual(len([p for p in self.ready_files() if p.suffix == ".eml"]), 1)

    def test_a_sent_email_does_not_make_the_worker_provision_again(self) -> None:
        self.paid_claim()
        provider = FixtureProvider()
        first = self.provision(provider)
        token_hash = self.tenant()["setup_token_hash"]
        # The mailer sends the message and removes the queue item; the ready
        # event is the durable marker, so the worker must not re-provision or
        # rotate the token the user already received.
        for path in self.ready_files():
            path.unlink()
        again = FixtureProvider()
        result = self.provision(again, now=NOW + 600)
        self.assertTrue(result.ok)
        self.assertTrue(result.skipped)
        self.assertEqual(again.created, 0)
        self.assertEqual(self.tenant()["setup_token_hash"], token_hash)
        self.assertEqual(claim.eligible(
            self.con, claim.ReadyOutbox(self.outbox_path)), [])
        self.assertEqual(first.server_id, result.server_id)

    def test_the_queue_scan_skips_a_ready_tenant(self) -> None:
        self.paid_claim()
        provider = FixtureProvider()
        claim.run_pending(
            self.con, provider_factory=lambda _config: provider, config=self.config,
            dns_client=self.dns_client, outbox=claim.ReadyOutbox(self.outbox_path),
            now=NOW, interval=0.0,
        )
        self.assertEqual(provider.created, 1)
        again = FixtureProvider()
        results = claim.run_pending(
            self.con, provider_factory=lambda _config: again, config=self.config,
            dns_client=self.dns_client, outbox=claim.ReadyOutbox(self.outbox_path),
            now=NOW + 60, interval=0.0,
        )
        self.assertEqual(results, [])
        self.assertEqual(again.created, 0)
        self.assertEqual(len(claim.eligible(
            self.con, claim.ReadyOutbox(self.outbox_path))), 0)

    def test_an_unpaid_claim_does_not_provision_until_the_flag_is_set(self) -> None:
        issued = self.issue(plan=None)
        self.accept(issued)
        provider = FixtureProvider()
        results = claim.run_pending(
            self.con, provider_factory=lambda _config: provider, config=self.config,
            dns_client=self.dns_client, outbox=claim.ReadyOutbox(self.outbox_path),
            now=NOW, interval=0.0,
        )
        self.assertEqual(results, [])
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.tenant()["state"], "claimed")
        self.assertEqual(self.ready_files(), [])

        tenant = claim.mark_paid(self.con, SLUG, "Blackbox Plus", now=at(NOW + 60))
        self.assertEqual(tenant["plan"], "Blackbox Plus")
        result = self.provision(FixtureProvider(), now=NOW + 120)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.tenant()["state"], "provisioned")
        self.assertEqual(len([p for p in self.ready_files() if p.suffix == ".eml"]), 1)

    def test_an_adopted_machine_is_not_created_again(self) -> None:
        self.paid_claim()
        provider = FixtureProvider()
        self.provision(provider)
        # Lose the email and the tenant's pointer: the worker must adopt the
        # machine the provider still lists rather than order a second one.
        for path in self.ready_files():
            path.unlink()
        self.con.execute(
            "UPDATE tenants SET state = 'claimed', server_id = NULL, ipv4 = NULL,"
            " expires_at = NULL, setup_token_hash = NULL WHERE slug = ?", (SLUG,)
        )
        self.con.commit()
        resumed = self.provision(provider)
        self.assertFalse(resumed.created)
        self.assertTrue(resumed.ok, resumed.reason)
        self.assertEqual(resumed.server_id, "srv-fixture0001")
        self.assertEqual(len(provider.machines), 1)
        # The re-adoption keeps the release the machine was first ordered under.
        self.assertEqual(self.tenant()["release"], RELEASE)

    def test_the_kit_prints_the_account_site_the_config_names(self) -> None:
        """`account_host` in the operator's config reaches the attached kit."""
        self.write_config(account_host=ACCOUNT_HOST)
        self.config = Config.load(self.config_path)
        self.paid_claim()
        result = self.provision(FixtureProvider())
        self.assertTrue(result.ok, result.reason)
        self.assertIsNotNone(result.kit_path)
        kit_text = Path(result.kit_path).read_text(encoding="utf-8")
        self.assertIn(f"{ACCOUNT_HOST}/forgot", kit_text)
        self.assertNotIn("the site in your invitation email", kit_text)

    def test_the_kit_keeps_the_invitation_sentence_without_the_config_key(self) -> None:
        """Unset in the config: the kit reads exactly as it did before the slot."""
        self.paid_claim()
        result = self.provision(FixtureProvider())
        self.assertTrue(result.ok, result.reason)
        kit_text = Path(result.kit_path).read_text(encoding="utf-8")
        self.assertIn(
            "It resets at your account\nsite — the site in your invitation email. "
            "Open that site and add /forgot.", kit_text)
        self.assertNotIn("{{", kit_text)


# --------------------------------------------------------------------- failure


class FailureTests(ClaimBase):
    def test_a_failed_build_terminates_the_machine_and_records_a_clear_state(self) -> None:
        issued = self.paid_claim()
        provider = FixtureProvider(fail_poll=True)
        result = self.provision(provider)

        self.assertFalse(result.ok)
        self.assertEqual(
            [call[0] for call in provider.calls],
            ["create", "poll", "terminate"],
        )
        self.assertEqual(provider.live(), [], "a failed provision left a billed machine")
        self.assertTrue(result.terminated)

        tenant = self.tenant()
        self.assertEqual(tenant["state"], "claimed")
        self.assertIsNone(tenant["server_id"])
        self.assertIsNone(tenant["ipv4"])
        self.assertIsNone(tenant["setup_token_hash"])
        self.assertEqual(self.ready_files(), [])
        self.assertEqual(self.cloud.records, {})

        last = json.loads(self.events()[-1]["detail"])
        self.assertEqual(last["step"], "provision_failed")
        self.assertEqual(last["server_id"], "srv-fixture0001")
        self.assertTrue(last["terminated"])

        alerts = self.alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "provision_failed")
        self.assertEqual(alerts[0]["slug"], SLUG)
        self.assertIn("build failed", alerts[0]["body"])
        self.assertTrue(Path(result.alert_path).is_file())

        status = claim.tenant_status(
            self.con, SLUG, outbox=claim.ReadyOutbox(self.outbox_path)
        )
        self.assertEqual(status["provision"], "failed")
        self.assertEqual(status["tenant_state"], "claimed")
        self.assertIsNone(status["ready_email"])

        # The claim stays usable: the same token replays, and the next run
        # creates exactly one new machine (the terminated one is not a resource).
        replay = self.accept(issued, now=NOW + 60)
        self.assertTrue(replay.replay)
        healthy = FixtureProvider()
        retry = self.provision(healthy, now=NOW + 120)
        self.assertTrue(retry.ok, retry.reason)
        self.assertEqual([c for c in healthy.calls if c[0] == "create"],
                         [("create", SLUG, KEY_TEXT)])
        self.assertEqual(len(healthy.live()), 1)
        self.assertEqual(len([p for p in self.ready_files() if p.suffix == ".eml"]), 1)

    def test_a_create_that_never_returns_leaves_no_machine(self) -> None:
        self.paid_claim()
        provider = FixtureProvider(fail_create=True)
        result = self.provision(provider)
        self.assertFalse(result.ok)
        self.assertFalse(result.terminated)
        self.assertEqual(provider.machines, {})
        self.assertEqual(self.tenant()["state"], "claimed")
        self.assertEqual(len(self.alerts()), 1)

    def test_a_failure_after_the_record_is_written_rolls_everything_back(self) -> None:
        self.paid_claim()
        provider = FixtureProvider()
        with mock.patch.object(
            claim, "render_recovery_kit", side_effect=claim.ClaimError("kit failed")
        ):
            result = self.provision(provider)
        self.assertFalse(result.ok)
        self.assertTrue(result.terminated)
        self.assertEqual(provider.live(), [])
        self.assertEqual(self.cloud.records, {})
        self.assertEqual(self.tenant()["state"], "claimed")
        self.assertIsNone(self.tenant()["server_id"])
        self.assertIsNone(self.tenant()["setup_token_hash"])
        self.assertEqual(self.ready_files(), [])
        self.assertEqual(self.alerts()[0]["kind"], "provision_failed")
        self.assertIn("kit failed", result.reason)

    def test_a_dns_failure_after_the_machine_exists_rolls_both_back(self) -> None:
        self.paid_claim()
        self.cloud.fail_create = True
        provider = FixtureProvider()
        result = self.provision(provider)
        self.assertFalse(result.ok)
        self.assertTrue(result.terminated)
        self.assertEqual(provider.live(), [])
        self.assertEqual(self.cloud.records, {})
        self.assertEqual(self.tenant()["state"], "claimed")
        self.assertEqual(self.ready_files(), [])


# --------------------------------------------------------------------- the web claim


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def start_app(db_path: Path, attest: Path) -> tuple[subprocess.Popen, int]:
    port = free_port()
    env = dict(
        os.environ,
        ODILE_DB=str(db_path),
        ODILE_ATTEST=str(attest),
        ODILE_BIND="127.0.0.1",
        ODILE_PORT=str(port),
        ODILE_RATE_LIMIT="1000",
    )
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "app.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=1
            ).status == 200:
                return proc, port
        except OSError:
            time.sleep(0.05)
    output = proc.communicate(timeout=5)[0].decode("utf-8", "replace")
    raise RuntimeError("the app did not start:\n" + output)


def stop_app(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()


def post_claim(port: int, fields: dict) -> tuple[int, str]:
    body = urllib.parse.urlencode(fields)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(
        "POST", "/claim", body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    response = conn.getresponse()
    text = response.read().decode("utf-8")
    conn.close()
    return response.status, text


class WebClaimTests(ClaimBase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        directory = Path(cls.tmp.name)
        cls.db_path = directory / "waitlist.db"
        cls.attest = directory / "attestation.json"
        config_path = directory / "config.yaml"
        config_path.write_text(
            "provider: servury\nreseller_key: fixture\n"
            "dns_token: fixture\ndns_zone: odile.fyi\n"
            f"state_db: {cls.db_path}\n"
            f"outbox: {directory / 'outbox'}\n"
            "default_plan_id: 113\n"
            f"expected_measurement: {MEASUREMENT}\n",
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        con = claim.open_db(cls.db_path)
        try:
            # The web server accepts with the real clock; issue the token from
            # the same clock so this suite does not expire on a later date.
            cls.issued = claim.issue_claim(
                con, "ada@example.com", plan="Blackbox Plus",
                slug=SLUG, now=datetime.now(timezone.utc),
            )
        finally:
            con.close()
        cls.proc, cls.port = start_app(cls.db_path, cls.attest)

    def setUp(self) -> None:
        # The server and its database are class-level; a per-test database
        # would make every assertion read a file the server never touched.
        self.db_path = type(self).db_path
        self.outbox_path = self.db_path.parent / "outbox"

    @classmethod
    def tearDownClass(cls) -> None:
        stop_app(cls.proc)
        cls.tmp.cleanup()

    def tenant(self) -> sqlite3.Row:
        con = claim.connect(self.db_path)
        try:
            return con.execute("SELECT * FROM tenants WHERE slug = ?", (SLUG,)).fetchone()
        finally:
            con.close()

    def events(self) -> list[sqlite3.Row]:
        con = claim.connect(self.db_path)
        try:
            return con.execute(
                "SELECT * FROM events WHERE tenant_id IS NOT NULL ORDER BY id"
            ).fetchall()
        finally:
            con.close()

    def test_the_page_records_the_claim_and_answers_with_the_promise(self) -> None:
        status, text = post_claim(self.port, {"token": self.issued.token, "key": KEY_TEXT})
        self.assertEqual(status, 200, text)
        self.assertIn("We will email you when the machine is ready.", text)
        tenant = self.tenant()
        self.assertEqual(tenant["state"], "claimed")
        self.assertEqual(tenant["ssh_public_key"], KEY_TEXT)
        claimed = [row for row in self.events() if row["kind"] == "claimed"]
        self.assertEqual(len(claimed), 1)

    def test_a_replay_is_the_same_success_and_a_new_key_is_refused(self) -> None:
        status, text = post_claim(self.port, {"token": self.issued.token, "key": KEY_TEXT})
        self.assertEqual(status, 200, text)
        status, text = post_claim(self.port, {"token": self.issued.token, "key": KEY_TEXT_2})
        self.assertEqual(status, 409, text)
        self.assertIn("already been used", text)
        self.assertEqual(len([row for row in self.events() if row["kind"] == "claimed"]), 1)

    def test_an_unknown_token_is_a_404_and_a_bad_key_is_a_422(self) -> None:
        status, text = post_claim(self.port, {"token": "f" * 48, "key": KEY_TEXT})
        self.assertEqual(status, 404, text)
        status, text = post_claim(self.port, {"token": "f" * 48, "key": PRIVATE_KEY})
        self.assertEqual(status, 422, text)
        status, text = post_claim(self.port, {"token": "f" * 48, "key": MALFORMED_KEY})
        self.assertEqual(status, 422, text)

    def test_get_claim_names_the_method(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/claim")
        response = conn.getresponse()
        text = response.read().decode("utf-8")
        conn.close()
        self.assertEqual(response.status, 405)
        self.assertIn("POST a claim", text)

    def test_an_oversized_claim_body_is_refused(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "POST", "/claim", body=b"x" * 9000,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = conn.getresponse()
        text = response.read().decode("utf-8")
        conn.close()
        self.assertEqual(response.status, 413)
        self.assertIn("too large", text)

    def test_the_waitlist_still_works(self) -> None:
        # The claim route is POST-only for /claim; the waitlist keeps its own
        # route, so use it explicitly.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(
            "POST", "/waitlist", body=urllib.parse.urlencode({"email": "someone@example.com"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 303)
        con = claim.connect(self.db_path)
        try:
            self.assertEqual(
                con.execute("SELECT count(*) FROM waitlist").fetchone()[0], 1
            )
        finally:
            con.close()


# --------------------------------------------------------------------- CLI


class CliTests(ClaimBase):
    def run_cli(self, args: list[str], provider: FixtureProvider | None = None,
                cloud: CloudflareFixture | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = claim.main(
            ["--config", str(self.config_path)] + args,
            provider_factory=(lambda _config: provider) if provider else None,
            dns_transport=cloud,
            stdout=out,
            stderr=err,
        )
        return code, out.getvalue(), err.getvalue()

    def test_the_cli_invite_accept_and_status_round_trip(self) -> None:
        code, out, err = self.run_cli([
            "invite", "ada@example.com", "--plan", "Blackbox Plus",
            "--slug", SLUG, "--apply", "--json",
        ])
        self.assertEqual(code, 0, err)
        token = json.loads(out)["claim_token"]
        key_file = self.directory / "key.pub"
        key_file.write_text(KEY_TEXT + "\n", encoding="utf-8")
        provider = FixtureProvider()
        code, out, err = self.run_cli(
            ["accept", token, "--key", str(key_file), "--interval", "0",
             "--apply", "--json"],
            provider=provider,
            cloud=self.cloud,
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["provision"]["server_id"], "srv-fixture0001")
        code, out, err = self.run_cli(["status", SLUG, "--json"])
        self.assertEqual(code, 0, err)
        status = json.loads(out)
        self.assertEqual(status["provision"], "ready")
        self.assertEqual(status["tenant_state"], "provisioned")
        # A second run has nothing to do and creates nothing.
        again = FixtureProvider()
        code, out, err = self.run_cli(["run", "--apply", "--json"], provider=again,
                                      cloud=self.cloud)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["count"], 0)
        self.assertEqual(again.created, 0)

    def test_a_dry_run_writes_nothing(self) -> None:
        code, out, err = self.run_cli([
            "invite", "ada@example.com", "--plan", "Blackbox Plus", "--slug", SLUG,
            "--json",
        ])
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["dry_run"])
        self.assertEqual(self.con.execute("SELECT count(*) FROM tenants").fetchone()[0], 0)

    def test_the_cli_exits_one_when_the_provision_fails(self) -> None:
        self.paid_claim()
        provider = FixtureProvider(fail_poll=True)
        code, out, err = self.run_cli(["run", "--interval", "0", "--apply", "--json"],
                                      provider=provider, cloud=self.cloud)
        self.assertEqual(code, 1, err)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["results"][0]["ok"])
        self.assertEqual(provider.live(), [])


# --------------------------------------------------------------------- evidence


def transcript() -> None:
    """Print the card's end-to-end evidence; safe to rerun on a fresh box."""
    directory = Path(tempfile.mkdtemp(prefix="odile-claim-transcript-"))
    try:
        db_path = directory / "waitlist.db"
        outbox = directory / "outbox"
        config_path = directory / "config.yaml"
        pin_path = directory / "odile-verify.pin"
        pin_path.write_text(json.dumps({
            "schema": 2,
            "provision_release": RELEASE,
            "measurement": MEASUREMENT,
            "releases": {RELEASE: {}},
        }), encoding="utf-8")
        config_path.write_text(
            "\n".join([
                "provider: servury",
                "reseller_key: fixture-reseller-key",
                "dns_token: fixture-dns-token",
                "dns_zone: odile.fyi",
                "dns_zone_id: fixture-zone-id",
                "default_plan_id: 113",
                "default_days: 31",
                f"state_db: {db_path}",
                f"outbox: {outbox}",
                "alert_email: ops@example.com",
                "backup_repo: sftp://backup@example.invalid/odile/{slug}",
                # The pin is the source; a stale `release:` must not win.
                f"release_pin: {pin_path}",
                f"release: {PROVIDER_RELEASE}",
            ]) + "\n",
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        config = Config.load(config_path)
        con = claim.open_db(db_path)
        cloud = CloudflareFixture()
        dns_client = dns.CloudflareDNS(
            "fixture-dns-token", ZONE, zone_id=ZONE_ID, transport=cloud
        )

        print("== invite ==")
        issued = claim.issue_claim(
            con, "ada@example.com", plan="Blackbox Plus", slug=SLUG, now=at(NOW)
        )
        print(f"tenant {issued.slug} · claim token {issued.token[:8]}… · "
              f"expires {issued.expires_at}")

        print("== accept (the web handler calls the same function) ==")
        accepted = claim.accept(con, issued.token, KEY_TEXT, now=at(NOW))
        print(f"{accepted.slug}: state={accepted.state} "
              f"key={accepted.key_fingerprint} paid={accepted.paid}")

        print("== provision (fixture provider + recorded Cloudflare) ==")
        provider = FixtureProvider()
        result = claim.provision_tenant(
            con, SLUG, provider=provider, config=config, dns_client=dns_client,
            outbox=claim.ReadyOutbox(outbox), now=NOW, interval=0.0,
        )
        print("fixture calls:", provider.calls)
        print("machine:", json.dumps(result.machine, sort_keys=True))
        print("pin:", json.dumps(json.loads(pin_path.read_text(encoding="utf-8")),
                                 sort_keys=True))
        print("tenants row:", json.dumps(dict(con.execute(
            "SELECT slug, state, server_id, ipv4, plan, release, expires_at,"
            " setup_token_hash FROM tenants WHERE slug = ?", (SLUG,)).fetchone()),
            sort_keys=True))
        print("tenant_state row:", json.dumps(dict(con.execute(
            "SELECT state, server_id, release FROM tenant_state WHERE slug = ?",
            (SLUG,)).fetchone()), sort_keys=True))
        print("claim status:", json.dumps(claim.tenant_status(
            con, SLUG, outbox=claim.ReadyOutbox(outbox)), sort_keys=True))
        print("dns:", json.dumps(
            next(iter(cloud.records.values())), sort_keys=True
        ))
        kit = Path(result.kit_path)
        kit_text = kit.read_text(encoding="utf-8")
        kit_visit = [line for line in kit_text.splitlines()
                     if line.startswith("Open ") and "/first-visit." in line][0]
        stored = con.execute(
            "SELECT token_hash, instance_id, expires_at FROM seed_tokens"
        ).fetchone()
        print("kit's first-visit line:", kit_visit)
        print("setup token: stored hash:", stored["token_hash"],
              "instance:", stored["instance_id"], "expires:", stored["expires_at"])
        message = email.message_from_bytes(
            Path(result.email_path).read_bytes(), policy=email_policy.default
        )
        print("queued email:", result.email_path)
        print("  To:", message["To"])
        print("  Subject:", message["Subject"])
        print("  body:")
        body = message.get_body(preferencelist=("plain",)).get_content()
        for line in body.splitlines():
            print("   |", line)
        print("setup token printed in the mail or the kit:",
              "?t=" in body or "?t=" in kit_text)
        print("  attachment:", [a.get_filename() for a in message.iter_attachments()])
        print(f"kit: {kit} ({len(kit_text.splitlines())} lines, "
              f"mode {oct(kit.stat().st_mode & 0o777)})")
        print("files queued:", sorted(p.name for p in (outbox / 'ready').iterdir()))

        print("== retry ==")
        again = FixtureProvider()
        retry = claim.provision_tenant(
            con, SLUG, provider=again, config=config, dns_client=dns_client,
            outbox=claim.ReadyOutbox(outbox), now=NOW + 60, interval=0.0,
        )
        print(f"created={retry.created} skipped={retry.skipped} "
              f"create calls={again.created} emails="
              f"{len([p for p in (outbox / 'ready').iterdir() if p.suffix == '.eml'])}")

        print("== odilectl status on the claim-provisioned tenant ==")
        status_out, status_err = io.StringIO(), io.StringIO()
        code = odilectl_cli.main(
            ["--config", str(config_path), "status", SLUG, "--db", str(db_path)],
            provider_factory=lambda _config: provider,
            stdout=status_out,
            stderr=status_err,
        )
        print(status_out.getvalue().rstrip())
        if status_err.getvalue().strip():
            print("stderr:", status_err.getvalue().strip())
        print("exit:", code)

        print("== the pin has no provision_release: nothing is ordered ==")
        unpinned_db = directory / "unpinned.db"
        bare_pin = directory / "odile-verify.bare.pin"
        bare_pin.write_text(json.dumps({"schema": 2, "releases": {}}), encoding="utf-8")
        unpinned_config_path = directory / "unpinned-config.yaml"
        unpinned_config_path.write_text("\n".join([
            "provider: servury",
            "reseller_key: fixture-reseller-key",
            "default_plan_id: 113",
            f"state_db: {unpinned_db}",
            f"outbox: {directory / 'unpinned-outbox'}",
            f"release_pin: {bare_pin}",
            f"expected_measurement: {MEASUREMENT}",
        ]) + "\n", encoding="utf-8")
        unpinned_config_path.chmod(0o600)
        unpinned_con = claim.open_db(unpinned_db)
        try:
            unpinned = claim.issue_claim(
                unpinned_con, "bare@example.com", plan="Blackbox Plus",
                slug="odl-b0b0b0", now=at(NOW),
            )
            claim.accept(unpinned_con, unpinned.token, KEY_TEXT, now=at(NOW))
            empty = FixtureProvider()
            try:
                claim.provision_tenant(
                    unpinned_con, unpinned.slug, provider=empty,
                    config=Config.load(unpinned_config_path),
                    outbox=claim.ReadyOutbox(directory / "unpinned-outbox"),
                    now=NOW, interval=0.0,
                )
                print("refused: NO — provision_tenant returned")
            except UsageError as error:
                print("refusal:", error)
            print("provider calls:", empty.calls)
            print("tenant:", json.dumps(dict(unpinned_con.execute(
                "SELECT state, server_id, ipv4, release FROM tenants WHERE slug = ?",
                (unpinned.slug,)).fetchone()), sort_keys=True))
            print("tenant_state rows with a server:",
                  unpinned_con.execute(
                      "SELECT count(*) FROM tenant_state WHERE server_id IS NOT NULL"
                  ).fetchone()[0])
        finally:
            unpinned_con.close()

        print("== failed provision ==")
        db2 = directory / "fail.db"
        con2 = claim.open_db(db2)
        issued2 = claim.issue_claim(
            con2, "fail@example.com", plan="Blackbox Plus", slug="odl-a1b2c3",
            now=at(NOW),
        )
        claim.accept(con2, issued2.token, KEY_TEXT, now=at(NOW))
        failing = FixtureProvider(fail_poll=True)
        failed = claim.provision_tenant(
            con2, issued2.slug, provider=failing, config=config, dns_client=dns_client,
            outbox=claim.ReadyOutbox(directory / "fail-outbox"), now=NOW, interval=0.0,
        )
        print("fixture calls:", failing.calls)
        print("machine states:", [m.state for m in failing.machines.values()])
        print("tenant:", json.dumps(dict(con2.execute(
            "SELECT state, server_id, ipv4 FROM tenants WHERE slug = ?",
            (issued2.slug,)).fetchone()), sort_keys=True))
        print("alert:", failed.alert_path)
        print("status:", json.dumps(claim.tenant_status(
            con2, issued2.slug, outbox=claim.ReadyOutbox(directory / "fail-outbox")
        ), sort_keys=True))
        failing.fail_poll = False
        retried = claim.provision_tenant(
            con2, issued2.slug, provider=failing, config=config, dns_client=dns_client,
            outbox=claim.ReadyOutbox(directory / "fail-outbox"), now=NOW + 120,
            interval=0.0,
        )
        print("retry after failure:", retried.ok, retried.server_id,
              "live machines:", len(failing.live()), "/", len(failing.machines),
              "states:", [m.state for m in failing.machines.values()])

        print("== web claim ==")
        web_db = directory / "web.db"
        con3 = claim.open_db(web_db)
        web_now = datetime.now(timezone.utc)
        web_issue = claim.issue_claim(
            con3, "web@example.com", plan="Blackbox Plus", slug="odl-0a1b2c",
            now=web_now,
        )
        con3.close()
        proc, port = start_app(web_db, directory / "attest.json")
        try:
            status, text = post_claim(port, {"token": web_issue.token, "key": KEY_TEXT})
            print("POST /claim ->", status, text.strip())
            status2, text2 = post_claim(port, {"token": web_issue.token, "key": KEY_TEXT})
            print("POST /claim again ->", status2, text2.strip())
        finally:
            stop_app(proc)
        web_con = claim.open_db(web_db)
        web_provider = FixtureProvider()
        web_result = claim.provision_tenant(
            web_con, "odl-0a1b2c", provider=web_provider, config=config,
            dns_client=dns_client, outbox=claim.ReadyOutbox(outbox),
            now=int(web_now.timestamp()) + 300, interval=0.0,
        )
        print("worker after the web claim:",
              web_result.ok, web_result.server_id, web_result.email_path)
        web_con.close()
        con.close()
        con2.close()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--transcript"]:
        transcript()
    else:
        unittest.main(verbosity=2)
