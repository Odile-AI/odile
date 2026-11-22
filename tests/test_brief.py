#!/usr/bin/python3
"""Tests for ODL-235: the brief's email sender.

The sender is the delivery half of the morning brief: the job writes
`~/briefs/<date>.md`, this script composes the plain-text email in the owner's
zone, sends it through Odile's relay as `brief-<slug>@mail.odile.fyi` with a
VERP envelope, and records the outcome where the signed heartbeat can see it.

Hermetic: a scratch home, a scratch seed, a fixed clock and a fake SMTP client.
No tenant machine, no network, no sleeps.

    /usr/bin/python3 -m unittest tests.test_brief -v
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import smtplib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "send_brief.py"
TEMPLATE = ROOT / "templates" / "brief.txt"
HEARTBEAT = ROOT / "scripts" / "heartbeat.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


send_brief = load_module("odile_send_brief_test", SCRIPT)

SLUG = "odl-7f3a2c"
TO = "mei@example.com"
RELAY_TEXT = "sentinel-relay-value-not-in-output"
# 2026-11-09T23:00:00Z is 07:00 on 10 November in Hong Kong.
NOW = int(datetime(2026, 11, 9, 23, 0, tzinfo=timezone.utc).timestamp())
DISPATCHED = "2026-11-09T23:00:41+00:00"

BRIEF = """# Morning brief — Tuesday 10 November 2026

Hong Kong, HKT (UTC+8) · 07:00

Weather: Hong Kong — cloudy, 17°C.

**Confidential computing.** The summit schedule is published.
https://example.com/summit

Your Downloads folder: 14 files sorted, 3 duplicates removed.
"""


class FakeSMTP:
    """The smtplib.SMTP surface send_brief uses, recording the order."""

    instances: list["FakeSMTP"] = []
    fail_login = False
    fail_send = False

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[str] = []
        self.message = None
        self.envelope = None
        self.user = None
        self.password = None
        self.context = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.calls.append("close")
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")
        self.context = context

    def login(self, user, password):
        self.calls.append("login")
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        self.user, self.password = user, password

    def send_message(self, message, from_addr=None, to_addrs=None):
        self.calls.append("send")
        if FakeSMTP.fail_send:
            raise smtplib.SMTPServerDisconnected("connection lost")
        self.message = message
        self.envelope = (from_addr, list(to_addrs or []))


class BriefTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeSMTP.instances = []
        FakeSMTP.fail_login = False
        FakeSMTP.fail_send = False
        self.directory = tempfile.mkdtemp(prefix="odile-brief-")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.home = Path(self.directory) / "home"
        (self.home / ".hermes" / "cron").mkdir(parents=True)
        (self.home / "briefs").mkdir()
        (self.home / ".hermes" / "brief.txt").write_text(
            TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (self.home / "briefs" / "2026-11-10.md").write_text(BRIEF, encoding="utf-8")
        self.seed = Path(self.directory) / "seed.json"
        self.write_seed()
        self.address = self.home / ".hermes" / "brief.json"
        self.address.write_text(json.dumps({"to": TO}), encoding="utf-8")
        self.jobs = self.home / ".hermes" / "cron" / "jobs.json"
        self.write_jobs()
        self.status = Path(self.directory) / "var" / "lib" / "odile" / "brief.json"

    def write_seed(self, **mail) -> None:
        relay = {
            "host": "mail.odile.fyi",
            "port": 587,
            "username": f"{SLUG}@mail.odile.fyi",
            "secret": RELAY_TEXT,
        }
        relay.update(mail)
        self.seed.write_text(
            json.dumps({"slug": SLUG, "mail_relay": relay}), encoding="utf-8"
        )

    def write_jobs(self, *, kind: str | None = "on_time", lateness: float = 0.4,
                   dispatched_at: str = DISPATCHED, name: str = "Morning brief",
                   expr: str = "0 7 * * *", timezone_name: str = "Asia/Hong_Kong") -> None:
        dispatch = None
        if kind is not None:
            dispatch = {
                "scheduled_at": "2026-11-09T23:00:00+00:00",
                "dispatched_at": dispatched_at,
                "lateness_seconds": lateness,
                "kind": kind,
            }
        job = {
            "id": "45cb990513c1",
            "name": name,
            "prompt": "",
            "schedule": {"kind": "cron", "expr": expr, "display": expr},
            "timezone": timezone_name,
            "deliver": "local",
            "enabled": True,
            "last_dispatch": dispatch,
        }
        self.jobs.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")

    def run_sender(self, *extra: str) -> tuple[int, str, str]:
        argv = [
            "--home", str(self.home),
            "--seed", str(self.seed),
            "--jobs", str(self.jobs),
            "--address", str(self.address),
            "--status", str(self.status),
            "--now", str(NOW),
            *extra,
        ]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(send_brief.smtplib, "SMTP", FakeSMTP):
            with redirect_stdout(out), redirect_stderr(err):
                code = send_brief.main(argv)
        return code, out.getvalue(), err.getvalue()

    def message(self) -> FakeSMTP:
        self.assertEqual(len(FakeSMTP.instances), 1)
        sent = FakeSMTP.instances[0]
        self.assertIsNotNone(sent.message)
        return sent

    # -- composition -------------------------------------------------------

    def test_subject_and_date_are_the_owners_zone(self) -> None:
        code, out, err = self.run_sender("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("Subject:   Tuesday, 10 November — your brief", out)
        self.assertIn("10 November 2026", out)
        self.assertIn("2026-11-10 · 07:00 Asia/Hong_Kong · odl-7f3a2c", out)
        self.assertIn("https://odl-7f3a2c.odile.fyi", out)

    def test_markdown_is_reduced_to_readable_plain_text(self) -> None:
        _code, out, _err = self.run_sender("--dry-run")
        self.assertIn("Morning brief — Tuesday 10 November 2026", out)
        self.assertIn("Confidential computing. The summit schedule is published.", out)
        self.assertNotIn("**", out)
        self.assertNotIn("](#", out)
        self.assertIn("https://example.com/summit", out)

    def test_success_uses_the_documented_headers_and_plain_text(self) -> None:
        code, out, err = self.run_sender()
        self.assertEqual(code, 0, err)
        sent = self.message()
        message = sent.message
        self.assertEqual(message["From"], "Odile <brief-odl-7f3a2c@mail.odile.fyi>")
        self.assertEqual(message["To"], TO)
        self.assertEqual(message["Reply-To"], "help@odile.fyi")
        self.assertEqual(message["Subject"], "Tuesday, 10 November — your brief")
        self.assertTrue(str(message["Message-ID"]).endswith("@mail.odile.fyi>"))
        self.assertEqual(message.get_content_type(), "text/plain")
        self.assertIsNone(message.get_body(("html",)))
        body = message.get_content()
        self.assertIn("Good morning.", body)
        self.assertIn("it carries the message, never your disk.", body)
        self.assertIn(f"brief: sent to {TO} at 07:00 Asia/Hong_Kong", out)

    def test_the_envelope_is_a_verp_and_the_from_header_is_the_tenant(self) -> None:
        self.run_sender()
        sent = self.message()
        envelope_from, recipients = sent.envelope
        self.assertTrue(envelope_from.startswith("b-20261110-"))
        self.assertTrue(envelope_from.endswith("@mail.odile.fyi"))
        self.assertEqual(recipients, [TO])
        self.assertNotEqual(envelope_from, sent.message["From"])

    def test_two_sends_use_different_verp_envelopes(self) -> None:
        """A VERP is unique per send, not only per tenant and date."""
        self.run_sender()
        first_send = self.message().envelope
        assert first_send is not None
        first = first_send[0]
        FakeSMTP.instances = []
        self.run_sender("--now", str(NOW + 60))
        second_send = self.message().envelope
        assert second_send is not None
        second = second_send[0]
        self.assertNotEqual(first, second)
        for envelope in (first, second):
            self.assertTrue(envelope.startswith("b-20261110-"))
            self.assertTrue(envelope.endswith("@mail.odile.fyi"))

    def test_tls_starttls_and_login_happen_before_the_send(self) -> None:
        self.run_sender()
        sent = self.message()
        self.assertEqual(sent.calls[:4], ["ehlo", "starttls", "ehlo", "login"])
        self.assertIn("send", sent.calls)
        self.assertEqual(sent.user, f"{SLUG}@mail.odile.fyi")
        self.assertEqual(sent.password, RELAY_TEXT)
        self.assertEqual((sent.host, sent.port), ("mail.odile.fyi", 587))

    def test_a_seed_ca_is_pinned_for_starttls(self) -> None:
        ca = Path(self.directory) / "mail-ca.pem"
        ca.write_text("-----BEGIN CERTIFICATE-----\nnot-a-real-cert\n-----END CERTIFICATE-----\n")
        self.write_seed(tls_ca=str(ca))
        loaded = []

        class Context:
            def load_verify_locations(self, **kwargs):
                loaded.append(kwargs)

        with mock.patch.object(send_brief.ssl, "create_default_context", lambda: Context()):
            code, _out, err = self.run_sender()
        self.assertEqual(code, 0, err)
        self.assertEqual(loaded, [{"cafile": str(ca)}])

    def test_a_pem_body_in_the_seed_is_pinned_in_memory(self) -> None:
        pem = "-----BEGIN CERTIFICATE-----\nnot-a-real-cert\n-----END CERTIFICATE-----\n"
        self.write_seed(tls_ca=pem)
        loaded = []

        class Context:
            def load_verify_locations(self, **kwargs):
                loaded.append(kwargs)

        with mock.patch.object(send_brief.ssl, "create_default_context", lambda: Context()):
            code, _out, err = self.run_sender()
        self.assertEqual(code, 0, err)
        self.assertEqual(loaded, [{"cadata": pem.strip()}])

    # -- lateness ----------------------------------------------------------

    def test_an_on_time_dispatch_has_no_late_sentence(self) -> None:
        self.run_sender()
        body = self.message().message.get_content()
        self.assertNotIn("after its 07:00 slot", body)

    def test_a_late_dispatch_says_when_and_by_how_much(self) -> None:
        self.write_jobs(kind="late", lateness=840, dispatched_at="2026-11-09T23:14:00+00:00")
        late_now = int(datetime(2026, 11, 9, 23, 14, 30, tzinfo=timezone.utc).timestamp())
        code, _out, err = self.run_sender("--now", str(late_now))
        self.assertEqual(code, 0, err)
        body = self.message().message.get_content()
        self.assertIn("This one ran at 07:14 — 14 minutes after its 07:00 slot.", body)

    def test_a_catch_up_dispatch_reads_in_hours_past_ninety_minutes(self) -> None:
        self.write_jobs(kind="catch_up", lateness=10920, dispatched_at="2026-11-10T01:02:00+00:00")
        now = int(datetime(2026, 11, 10, 1, 2, 30, tzinfo=timezone.utc).timestamp())
        code, _out, err = self.run_sender("--now", str(now))
        self.assertEqual(code, 0, err)
        body = self.message().message.get_content()
        self.assertIn("This one ran at 09:02 — 3 hours 2 minutes after its 07:00 slot.", body)

    def test_a_dispatch_older_than_the_window_is_not_this_run(self) -> None:
        self.write_jobs(kind="late", lateness=840, dispatched_at="2026-11-08T23:14:00+00:00")
        self.run_sender()
        body = self.message().message.get_content()
        self.assertNotIn("after its 07:00 slot", body)

    def test_a_job_with_no_dispatch_record_is_not_marked_late(self) -> None:
        self.write_jobs(kind=None)
        self.run_sender()
        body = self.message().message.get_content()
        self.assertNotIn("after its 07:00 slot", body)

    # -- status ------------------------------------------------------------

    def test_success_records_last_sent_at_for_the_heartbeat(self) -> None:
        self.run_sender()
        record = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertTrue(record["ok"])
        self.assertEqual(record["last_sent_at"], NOW)
        self.assertEqual(record["last_attempt_at"], NOW)
        self.assertEqual(record["to"], TO)
        self.assertEqual(record["slug"], SLUG)
        self.assertEqual(record["date"], "2026-11-10")
        self.assertEqual(record["late_seconds"], 0)
        self.assertIn("@mail.odile.fyi", record["message_id"])

    def test_a_failed_send_records_the_error_and_keeps_the_last_sent_at(self) -> None:
        self.status.parent.mkdir(parents=True)
        self.status.write_text(json.dumps({"last_sent_at": NOW - 86400}), encoding="utf-8")
        FakeSMTP.fail_login = True
        code, out, err = self.run_sender()
        self.assertEqual(code, 1)
        self.assertIn("brief: send failed", err)
        self.assertEqual(out.strip(), "")
        record = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertFalse(record["ok"])
        self.assertEqual(record["last_sent_at"], NOW - 86400)
        self.assertEqual(record["last_attempt_at"], NOW)
        self.assertIn("bad credentials", record["error"])

    def test_a_missing_brief_exits_three_and_records_the_failure(self) -> None:
        (self.home / "briefs" / "2026-11-10.md").unlink()
        code, _out, err = self.run_sender()
        self.assertEqual(code, 3)
        self.assertIn("no brief for 2026-11-10", err)
        record = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertFalse(record["ok"])
        self.assertIn("no brief for 2026-11-10", record["error"])
        self.assertEqual(FakeSMTP.instances, [])

    def test_the_failure_record_leaves_the_heartbeat_with_no_new_brief(self) -> None:
        """The HQ chain: a failed send does not advance last_brief_at."""
        self.status.parent.mkdir(parents=True)
        self.status.write_text(json.dumps({"last_sent_at": NOW - 86400}), encoding="utf-8")
        FakeSMTP.fail_send = True
        self.run_sender()
        heartbeat = load_module("odile_heartbeat_test", HEARTBEAT)
        args = heartbeat.build_parser().parse_args(
            ["collect", "--slug", SLUG, "--brief-status", str(self.status), "--at", str(NOW)]
        )
        payload = heartbeat.collect(args)
        self.assertEqual(payload["facts"]["last_brief_at"], NOW - 86400)
        self.assertLess(payload["facts"]["last_brief_at"], NOW)

    def test_a_stale_status_file_is_replaced_not_appended(self) -> None:
        self.status.parent.mkdir(parents=True)
        self.status.write_text("{not json", encoding="utf-8")
        code, _out, err = self.run_sender()
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(self.status.read_text(encoding="utf-8"))["ok"])

    # -- refusals ----------------------------------------------------------

    def test_a_missing_address_exits_two_and_writes_nothing(self) -> None:
        self.address.unlink()
        code, _out, err = self.run_sender()
        self.assertEqual(code, 2)
        self.assertIn("brief address file", err)
        self.assertFalse(self.status.exists())

    def test_a_seed_without_a_relay_exits_four(self) -> None:
        self.seed.write_text(json.dumps({"slug": SLUG}), encoding="utf-8")
        code, _out, err = self.run_sender()
        self.assertEqual(code, 4)
        self.assertIn("mail_relay", err)

    def test_an_unknown_zone_exits_two(self) -> None:
        self.write_jobs(timezone_name="Mars/Olympus")
        code, _out, err = self.run_sender()
        self.assertEqual(code, 2)
        self.assertIn("unknown timezone", err)

    def test_the_secret_never_reaches_stdout_or_stderr(self) -> None:
        outputs = []
        for extra, fail in ((["--dry-run"], False), ([], False), ([], True)):
            if self.status.exists():
                self.status.unlink()
            FakeSMTP.instances = []
            FakeSMTP.fail_send = fail
            _code, out, err = self.run_sender(*extra)
            outputs.extend([out, err])
        for text in outputs:
            self.assertNotIn(RELAY_TEXT, text)

    def test_dry_run_sends_nothing_and_writes_no_status(self) -> None:
        code, out, err = self.run_sender("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(FakeSMTP.instances, [])
        self.assertFalse(self.status.exists())
        self.assertIn("From:      Odile <brief-odl-7f3a2c@mail.odile.fyi>", out)

    def test_dry_run_with_a_missing_brief_writes_no_status(self) -> None:
        """`--dry-run` promises no write on every path, not only the happy one."""
        (self.home / "briefs" / "2026-11-10.md").unlink()
        code, out, err = self.run_sender("--dry-run")
        self.assertEqual(code, 3)
        self.assertIn("no brief for 2026-11-10", err)
        self.assertEqual(out, "")
        self.assertFalse(self.status.exists())
        self.assertEqual(FakeSMTP.instances, [])

    def test_running_twice_keeps_one_status_record(self) -> None:
        self.run_sender()
        FakeSMTP.instances = []
        self.run_sender("--now", str(NOW + 60))
        record = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertEqual(record["last_attempt_at"], NOW + 60)
        self.assertEqual(record["last_sent_at"], NOW + 60)
        self.assertEqual(len(FakeSMTP.instances), 1)


if __name__ == "__main__":
    unittest.main()
