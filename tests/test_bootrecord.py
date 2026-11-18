#!/usr/bin/python3
"""Tests for the measured boot record (ODL-398 follow-up).

The card's question is narrow: after a strict first boot, does HQ's row carry
the key from the measured line, the pinned release and the state the machine is
in — and does a machine that never first-boots leave the old row alone?

Hermetic: a scratch SQLite file built by `app.migrate` + `state.SCHEMA`, and a
fixture pin whose `cmdline` carries a real SSH public key in `bb.key=`. No
socket, no live API, no tenant.

    /usr/bin/python3 -m unittest tests.test_bootrecord -v
"""

from __future__ import annotations

import base64
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from odilectl import bootrecord, cli, state  # noqa: E402
from tests.test_odilectl import TempConfigMixin, run_cli  # noqa: E402

SLUG = "odl-7f3a2c"
INSTANCE = "srv-7f3a2c"
HOST_KEY = "SHA256:IRqReY120zzs/+HwhEQIw8EfYl0ys5pdX4/fSHopHWQ"
KEY_TEXT = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICFh7hQkST5vRDolO3yqwCARRugZahMdc+WgR69JsoXG "
    "odile-test throwaway boot key"
)
TOKEN = "ab" * 32


def encoded_key(line: str = KEY_TEXT) -> str:
    return base64.b64encode(line.encode()).decode()


def measured_cmdline(*, instance: str = INSTANCE, hostname: str = SLUG,
                     key: str | None = None, extra: str = "") -> str:
    return (
        "root=/dev/mapper/root_crypt ro console=ttyS0 "
        f"bb.host={hostname} bb.iid={instance} bb.key={encoded_key(key or KEY_TEXT)} "
        f"odile.token={TOKEN} odile.seed_url=https://odile.fyi/seed{extra}"
    )


def record(**overrides) -> dict:
    value = {
        "cmdline": measured_cmdline(),
        "release": "2026.11.1",
        "first_visit": "done",
        "host_key": HOST_KEY,
        "serving": "200",
        "at": "2026-11-12T14:57:46Z",
    }
    value.update(overrides)
    return value


class BootRecordFixture(unittest.TestCase):
    """One scratch HQ database with a tenant at the edge under test."""

    def make_db(self, *, spine_state: str = "first_boot",
                web_state: str = "provisioned", release: str | None = None,
                initramfs: str | None = "SHA256:old-initramfs") -> sqlite3.Connection:
        directory = tempfile.mkdtemp(prefix="odile-bootrecord-")
        self.addCleanup(__import__("shutil").rmtree, directory, ignore_errors=True)
        self.db_path = Path(directory) / "waitlist.db"
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        app.migrate(con)
        con.executescript(state.SCHEMA)
        con.execute(
            "INSERT INTO tenants (slug, email, state, server_id, ssh_public_key,"
            " release, created_at, claimed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (SLUG, "bench@odile.test", web_state, INSTANCE, "ssh-ed25519 OLD", release,
             "2026-11-09T16:03:46Z", "2026-11-09T16:03:46Z"),
        )
        con.execute(
            "INSERT INTO tenant_state (slug, state, server_id, release,"
            " initramfs_host_key, detail, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (SLUG, spine_state, INSTANCE, release, initramfs,
             json.dumps({"measured_release": "2026.11.1"}), "2026-11-09T16:03:46Z",
             "2026-11-10T04:46:50Z"),
        )
        con.commit()
        return con

    def rows(self, con: sqlite3.Connection) -> tuple[sqlite3.Row, sqlite3.Row]:
        tenant = con.execute("SELECT * FROM tenants WHERE slug = ?", (SLUG,)).fetchone()
        spine = con.execute("SELECT * FROM tenant_state WHERE slug = ?", (SLUG,)).fetchone()
        return tenant, spine

    def events(self, con: sqlite3.Connection) -> list[dict]:
        return [dict(row) for row in con.execute(
            "SELECT kind, from_state, to_state FROM tenant_state_events ORDER BY id")]


class ParseRecordTests(unittest.TestCase):
    """The key is the measured line's, and nothing else is accepted."""

    def test_the_key_and_release_come_from_the_measured_line(self) -> None:
        parsed = bootrecord.parse_record(record(), instance_id=INSTANCE, slug=SLUG)
        self.assertEqual(parsed["boot_key"], KEY_TEXT)
        self.assertTrue(parsed["boot_key_fingerprint"].startswith("SHA256:"))
        self.assertEqual(parsed["release"], "2026.11.1")
        self.assertEqual(parsed["first_visit"], "done")
        self.assertEqual(parsed["instance_id"], INSTANCE)
        self.assertEqual(parsed["cmdline_sha256"],
                         __import__("hashlib").sha256(
                             measured_cmdline().encode()).hexdigest())

    def test_two_bb_key_tokens_are_refused(self) -> None:
        line = measured_cmdline() + " bb.key=" + encoded_key()
        with self.assertRaises(bootrecord.BootRecordError) as caught:
            bootrecord.parse_record(record(cmdline=line))
        self.assertIn("bb.key", str(caught.exception))

    def test_another_instance_or_tenant_is_refused(self) -> None:
        with self.assertRaises(bootrecord.BootRecordError):
            bootrecord.parse_record(record(cmdline=measured_cmdline(instance="srv-other")),
                                    instance_id=INSTANCE)
        with self.assertRaises(bootrecord.BootRecordError):
            bootrecord.parse_record(record(cmdline=measured_cmdline(hostname="odl-other")),
                                    slug=SLUG)

    def test_a_malformed_key_or_release_is_refused(self) -> None:
        with self.assertRaises(bootrecord.BootRecordError):
            bootrecord.parse_record(record(cmdline=measured_cmdline(key="ssh-ed25519 !!!")))
        with self.assertRaises(bootrecord.BootRecordError):
            bootrecord.parse_record(record(release="not-a-release"))
        with self.assertRaises(bootrecord.BootRecordError):
            bootrecord.parse_record(record(first_visit="maybe"))
        with self.assertRaises(bootrecord.BootRecordError):
            bootrecord.parse_record({**record(), "password": "hunter2"})


class RecordWriterTests(BootRecordFixture):
    """The row after the first boot, and the row after a tenant that never booted."""

    def test_a_boot_that_finished_its_first_visit_moves_to_ready(self) -> None:
        con = self.make_db()
        parsed = bootrecord.parse_record(record(), instance_id=INSTANCE, slug=SLUG)
        written = bootrecord.record_boot(con, slug=SLUG, record=parsed, source="first_boot")
        self.assertEqual(written["state"], "ready")
        self.assertEqual(written["changed"],
                         ["first_boot -> first_visit", "first_visit -> ready"])
        tenant, spine = self.rows(con)
        self.assertEqual(tenant["ssh_public_key"], KEY_TEXT)
        self.assertEqual(tenant["release"], "2026.11.1")
        self.assertEqual(tenant["state"], "active")
        self.assertEqual(tenant["installed_at"], "2026-11-12T14:57:46Z")
        self.assertEqual(spine["state"], "ready")
        self.assertEqual(spine["release"], "2026.11.1")
        self.assertIsNone(spine["initramfs_host_key"])
        detail = json.loads(spine["detail"])
        self.assertEqual(detail["boot_key"], KEY_TEXT)
        self.assertEqual(detail["boot_key_fingerprint"], parsed["boot_key_fingerprint"])
        self.assertEqual(detail["boot_release"], "2026.11.1")
        self.assertEqual(detail["first_visit"], "done")
        self.assertEqual(detail["host_key"], HOST_KEY)
        self.assertEqual(detail["cmdline_sha256"], parsed["cmdline_sha256"])
        self.assertEqual(detail["measured_release"], "2026.11.1")
        self.assertEqual(self.events(con), [
            {"kind": "first_visit", "from_state": "first_boot", "to_state": "first_visit"},
            {"kind": "ready", "from_state": "first_visit", "to_state": "ready"},
        ])

    def test_a_boot_before_the_first_visit_stops_at_first_visit(self) -> None:
        con = self.make_db()
        parsed = bootrecord.parse_record(record(first_visit="pending"),
                                         instance_id=INSTANCE, slug=SLUG)
        written = bootrecord.record_boot(con, slug=SLUG, record=parsed)
        self.assertEqual(written["state"], "first_visit")
        self.assertEqual(written["changed"], ["first_boot -> first_visit"])
        tenant, spine = self.rows(con)
        self.assertEqual(tenant["state"], "installed")
        self.assertEqual(spine["state"], "first_visit")
        self.assertEqual(json.loads(spine["detail"])["first_visit"], "pending")
        self.assertEqual(self.events(con), [
            {"kind": "first_visit", "from_state": "first_boot", "to_state": "first_visit"},
        ])

    def test_a_second_record_writes_nothing_and_adds_no_event(self) -> None:
        con = self.make_db()
        parsed = bootrecord.parse_record(record(), instance_id=INSTANCE, slug=SLUG)
        bootrecord.record_boot(con, slug=SLUG, record=parsed)
        before = self.rows(con)
        again = bootrecord.record_boot(con, slug=SLUG, record=parsed)
        after = self.rows(con)
        self.assertEqual(again["changed"], [])
        self.assertEqual(before[0]["ssh_public_key"], after[0]["ssh_public_key"])
        self.assertEqual(before[1]["detail"], after[1]["detail"])
        self.assertEqual(before[1]["updated_at"], after[1]["updated_at"])
        self.assertEqual(len(self.events(con)), 2)

    def test_a_replay_without_a_stamp_keeps_the_first_recorded_at(self) -> None:
        """A boot report carries no `at`; the row keeps the first write's stamp.

        The clock is pinned and moves between the calls, so a per-call key in
        `detail` (the old `recorded_at`) would make the two rows differ.
        """
        con = self.make_db()
        parsed = bootrecord.parse_record(record(at=None), instance_id=INSTANCE, slug=SLUG)
        first = datetime(2026, 11, 12, 14, 57, 46, tzinfo=timezone.utc)
        later = datetime(2026, 11, 12, 16, 0, 0, tzinfo=timezone.utc)
        bootrecord.record_boot(con, slug=SLUG, record=parsed, now=first)
        before = self.rows(con)
        again = bootrecord.record_boot(con, slug=SLUG, record=parsed, now=later)
        after = self.rows(con)
        self.assertEqual(again["changed"], [])
        self.assertEqual(json.loads(after[1]["detail"])["recorded_at"],
                         "2026-11-12T14:57:46Z")
        self.assertEqual(before[1]["detail"], after[1]["detail"])
        self.assertEqual(before[1]["updated_at"], after[1]["updated_at"])
        self.assertEqual(len(self.events(con)), 2)

    def test_a_ready_row_takes_the_note_without_a_transition(self) -> None:
        con = self.make_db(spine_state="ready", web_state="active")
        parsed = bootrecord.parse_record(record(), instance_id=INSTANCE, slug=SLUG)
        written = bootrecord.record_boot(con, slug=SLUG, record=parsed)
        self.assertEqual(written["state"], "ready")
        self.assertEqual(written["changed"], [])
        _, spine = self.rows(con)
        self.assertEqual(json.loads(spine["detail"])["boot_key"], KEY_TEXT)
        self.assertEqual(self.events(con), [])

    def test_the_order_release_is_not_rewritten_by_the_observation(self) -> None:
        con = self.make_db(release="2026.10.1")
        parsed = bootrecord.parse_record(record(), instance_id=INSTANCE, slug=SLUG)
        written = bootrecord.record_boot(con, slug=SLUG, record=parsed)
        tenant, spine = self.rows(con)
        self.assertEqual(written["release"], "2026.11.1")
        self.assertEqual(tenant["release"], "2026.10.1")
        self.assertEqual(spine["release"], "2026.10.1")

    def test_a_row_that_never_reached_first_boot_is_refused(self) -> None:
        con = self.make_db(spine_state="provisioning")
        parsed = bootrecord.parse_record(record(), instance_id=INSTANCE, slug=SLUG)
        with self.assertRaises(bootrecord.BootRecordError) as caught:
            bootrecord.record_boot(con, slug=SLUG, record=parsed)
        self.assertIn("first_boot", str(caught.exception))
        tenant, spine = self.rows(con)
        self.assertEqual(tenant["ssh_public_key"], "ssh-ed25519 OLD")
        self.assertEqual(spine["state"], "provisioning")

    def test_a_destroyed_tenant_is_refused(self) -> None:
        con = self.make_db(spine_state="destroyed")
        parsed = bootrecord.parse_record(record(), instance_id=INSTANCE, slug=SLUG)
        with self.assertRaises(bootrecord.BootRecordError):
            bootrecord.record_boot(con, slug=SLUG, record=parsed)

    def test_another_instances_measured_line_is_refused(self) -> None:
        con = self.make_db()
        parsed = bootrecord.parse_record(record(cmdline=measured_cmdline(instance="srv-other")),
                                         instance_id="srv-other", slug=SLUG)
        with self.assertRaises(bootrecord.BootRecordError) as caught:
            bootrecord.record_boot(con, slug=SLUG, record=parsed)
        self.assertIn("ordered for instance", str(caught.exception))


class BootRecordCliTests(TempConfigMixin):
    """`odilectl boot-record` is the repair path; it is a dry run until --apply."""

    def setUp(self) -> None:
        self.config = self.make_config()
        values = {}
        for line in self.config.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(":")
            values[key.strip()] = value.strip()
        self.db = Path(values["state_db"])
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        app.migrate(con)
        con.executescript(state.SCHEMA)
        con.execute(
            "INSERT INTO tenants (slug, email, state, server_id, ssh_public_key,"
            " created_at, claimed_at) VALUES (?, ?, 'provisioned', ?, 'ssh-ed25519 OLD',"
            " '2026-11-09T16:03:46Z', '2026-11-09T16:03:46Z')",
            (SLUG, "bench@odile.test", INSTANCE),
        )
        con.execute(
            "INSERT INTO tenant_state (slug, state, server_id, initramfs_host_key,"
            " detail, created_at, updated_at) VALUES (?, 'first_boot', ?, ?, '{}', ?, ?)",
            (SLUG, INSTANCE, "SHA256:old", "2026-11-09T16:03:46Z",
             "2026-11-10T04:46:50Z"),
        )
        con.commit()
        con.close()
        directory = tempfile.mkdtemp(prefix="odile-boot-pin-")
        self.addCleanup(__import__("shutil").rmtree, directory, ignore_errors=True)
        self.pin = Path(directory) / "tenant-image.pin"
        self.pin.write_text(json.dumps({
            "release": "2026.11.1", "cmdline": measured_cmdline(),
        }), encoding="utf-8")

    def test_the_default_is_a_dry_run_and_apply_writes_the_row(self) -> None:
        argv = ["boot-record", SLUG, "--pin", str(self.pin), "--first-visit", "done"]
        code, out, err = run_cli(argv, self.config)
        self.assertEqual(code, 0, err)
        self.assertIn("dry run", out)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                con.execute("SELECT ssh_public_key FROM tenants").fetchone()[0],
                "ssh-ed25519 OLD")
        finally:
            con.close()

        code, out, err = run_cli(argv + ["--apply"], self.config)
        self.assertEqual(code, 0, err)
        self.assertIn("first_boot -> ready", out)
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute("SELECT state, ssh_public_key, release FROM tenants").fetchone()
            spine = con.execute("SELECT state, release FROM tenant_state").fetchone()
        finally:
            con.close()
        self.assertEqual(row["ssh_public_key"], KEY_TEXT)
        self.assertEqual(row["release"], "2026.11.1")
        self.assertEqual(row["state"], "active")
        self.assertEqual(spine["state"], "ready")
        self.assertEqual(spine["release"], "2026.11.1")


if __name__ == "__main__":
    unittest.main()
