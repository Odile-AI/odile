"""Tests for the bounce parser.

A bounce is only useful if it names the address that failed and the SMTP
status that failed it. These fixtures are shaped like the reports Postfix,
Gmail and Outlook actually send, plus the two things that arrive in the same
mailbox and must not be mistaken for bounces: a DMARC aggregate report and an
ARF complaint.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("mail_dsn", ROOT / "scripts" / "mail_dsn.py")
assert SPEC and SPEC.loader
mail_dsn = importlib.util.module_from_spec(SPEC)
sys.modules["mail_dsn"] = mail_dsn
SPEC.loader.exec_module(mail_dsn)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


POSTFIX_APPLY = load_module("deploy_postfix_apply", ROOT / "deploy" / "postfix" / "apply.py")
OPENDKIM_APPLY = load_module("deploy_opendkim_apply", ROOT / "deploy" / "opendkim" / "apply.py")

REAL_MAILDIR = Path("/var/mail/dsn/Maildir")


def hard_bounce() -> bytes:
    return (
        b"Return-Path: <>\r\n"
        b"Delivered-To: dsn@mail.odile.fyi\r\n"
        b"From: Mail Delivery System <MAILER-DAEMON@mail.odile.fyi>\r\n"
        b"To: b-20261106-5dbc28f67be4@mail.odile.fyi\r\n"
        b"Subject: Undelivered Mail Returned to Sender\r\n"
        b"Date: Fri, 06 Nov 2026 13:51:26 +0000 (UTC)\r\n"
        b"Message-Id: <20261106135126.579B91AA1E9@mail.odile.fyi>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/report; report-type=delivery-status;\r\n"
        b'\tboundary="8B0311AA1E3.1793973086/mail.odile.fyi"\r\n'
        b"\r\n"
        b"This is a MIME-encapsulated message.\r\n"
        b"\r\n"
        b"--8B0311AA1E3.1793973086/mail.odile.fyi\r\n"
        b"Content-Description: Notification\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"<someone@gmail.com>: host gmail-smtp-in.l.google.com said:\r\n"
        b"    550-5.7.26 Your email has been blocked because the sender is\r\n"
        b"    unauthenticated.\r\n"
        b"\r\n"
        b"--8B0311AA1E3.1793973086/mail.odile.fyi\r\n"
        b"Content-Description: Delivery report\r\n"
        b"Content-Type: message/delivery-status\r\n"
        b"\r\n"
        b"Reporting-MTA: dns; mail.odile.fyi\r\n"
        b"X-Postfix-Queue-ID: 8B0311AA1E3\r\n"
        b"X-Postfix-Sender: rfc822; b-20261106-5dbc28f67be4@mail.odile.fyi\r\n"
        b"Arrival-Date: Fri, 06 Nov 2026 13:51:25 +0000 (UTC)\r\n"
        b"\r\n"
        b"Final-Recipient: rfc822; someone@gmail.com\r\n"
        b"Original-Recipient: rfc822;someone@gmail.com\r\n"
        b"Action: failed\r\n"
        b"Status: 5.7.26\r\n"
        b"Remote-MTA: dns; gmail-smtp-in.l.google.com\r\n"
        b"Diagnostic-Code: smtp; 550-5.7.26 Your email has been blocked because\r\n"
        b"    the sender is unauthenticated.\r\n"
        b"\r\n"
        b"--8B0311AA1E3.1793973086/mail.odile.fyi\r\n"
        b"Content-Description: Undelivered Message\r\n"
        b"Content-Type: message/rfc822\r\n"
        b"\r\n"
        b"From: Odile <invite@mail.odile.fyi>\r\n"
        b"To: someone@gmail.com\r\n"
        b"Subject: Odile test invite (ODL-102)\r\n"
        b"\r\n"
        b"body\r\n"
        b"--8B0311AA1E3.1793973086/mail.odile.fyi--\r\n"
    )


def soft_bounce() -> bytes:
    return (
        b"From: Mail Delivery System <MAILER-DAEMON@mail.odile.fyi>\r\n"
        b"To: b-20261106-5dbc28f67be4@mail.odile.fyi\r\n"
        b"Subject: Delivery Status Notification (Delay)\r\n"
        b"Message-Id: <20261106140000.ABCDE1AA1E9@mail.odile.fyi>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/report; report-type=delivery-status; boundary="b2"\r\n'
        b"\r\n"
        b"--b2\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"This is a delivery status notification.\r\n"
        b"--b2\r\n"
        b"Content-Type: message/delivery-status\r\n"
        b"\r\n"
        b"Reporting-MTA: dns; mail.odile.fyi\r\n"
        b"X-Postfix-Queue-ID: 4A1B2C3D4E5\r\n"
        b"X-Postfix-Sender: rfc822; b-20261106-5dbc28f67be4@mail.odile.fyi\r\n"
        b"\r\n"
        b"Final-Recipient: rfc822; slow@example.com\r\n"
        b"Action: delayed\r\n"
        b"Status: 4.4.1\r\n"
        b"Remote-MTA: dns; mx.example.com\r\n"
        b"Diagnostic-Code: smtp; 451 4.4.1 Connection timed out\r\n"
        b"\r\n"
        b"--b2--\r\n"
    )


def feedback_report() -> bytes:
    return (
        b"From: abuse@example.com\r\n"
        b"To: b-20261106-5dbc28f67be4@mail.odile.fyi\r\n"
        b"Subject: Complaint about message\r\n"
        b"Message-Id: <arf-1@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/report; report-type=feedback-report; boundary="a1"\r\n'
        b"\r\n"
        b"--a1\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"This is an abuse report.\r\n"
        b"--a1\r\n"
        b"Content-Type: message/feedback-report\r\n"
        b"\r\n"
        b"Feedback-Type: abuse\r\n"
        b"User-Agent: ExampleGenerator/1.0\r\n"
        b"Version: 1\r\n"
        b"Original-Mail-From: b-20261106-5dbc28f67be4@mail.odile.fyi\r\n"
        b"Original-Rcpt-To: someone@example.com\r\n"
        b"Arrival-Date: Fri, 06 Nov 2026 13:51:25 +0000\r\n"
        b"Source-IP: 203.0.113.9\r\n"
        b"Reported-Domain: example.com\r\n"
        b"Authentication-Results: mx.example.com; spf=pass smtp.mailfrom=mail.odile.fyi\r\n"
        b"\r\n"
        b"--a1\r\n"
        b"Content-Type: message/rfc822\r\n"
        b"\r\n"
        b"From: Odile <invite@mail.odile.fyi>\r\n"
        b"Subject: Odile test invite (ODL-102)\r\n"
        b"\r\n"
        b"body\r\n"
        b"--a1--\r\n"
    )


def dmarc_aggregate() -> bytes:
    return (
        b"From: noreply-dmarc-support@google.com\r\n"
        b"To: dmarc@mail.odile.fyi\r\n"
        b"Subject: Report domain: mail.odile.fyi\r\n"
        b"Message-Id: <dmarc-1@google.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/report; report-type=dmarc; boundary="d1"\r\n'
        b"\r\n"
        b"--d1\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"This is a DMARC aggregate report.\r\n"
        b"--d1\r\n"
        b"Content-Type: application/zip\r\n"
        b"Content-Disposition: attachment; filename=report.zip\r\n"
        b"\r\n"
        b"PK\x03\x04not-a-real-zip\r\n"
        b"--d1--\r\n"
    )


def two_recipients() -> bytes:
    report = hard_bounce().replace(
        b"Final-Recipient: rfc822; someone@gmail.com\r\n"
        b"Original-Recipient: rfc822;someone@gmail.com\r\n"
        b"Action: failed\r\n"
        b"Status: 5.7.26\r\n"
        b"Remote-MTA: dns; gmail-smtp-in.l.google.com\r\n"
        b"Diagnostic-Code: smtp; 550-5.7.26 Your email has been blocked because\r\n"
        b"    the sender is unauthenticated.\r\n",
        b"Final-Recipient: rfc822; someone@gmail.com\r\n"
        b"Action: failed\r\n"
        b"Status: 5.2.1\r\n"
        b"\r\n"
        b"Final-Recipient: rfc822; other@example.com\r\n"
        b"Action: delayed\r\n"
        b"Status: 4.4.1\r\n",
    )
    return report


class ParseTests(unittest.TestCase):
    def test_hard_bounce_names_the_recipient_and_the_smtp_status(self):
        record = mail_dsn.parse_report(hard_bounce())
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["kind"], "dsn")
        self.assertEqual(record["bounce"], "hard")
        self.assertEqual(record["recipients"][0]["recipient"], "someone@gmail.com")
        self.assertEqual(record["recipients"][0]["status"], "5.7.26")
        self.assertEqual(record["recipient"], "someone@gmail.com")
        self.assertEqual(record["status"], "5.7.26")

    def test_hard_bounce_keeps_the_envelope_sender_and_queue_id(self):
        record = mail_dsn.parse_report(hard_bounce())
        assert record is not None
        self.assertEqual(record["original_sender"], "b-20261106-5dbc28f67be4@mail.odile.fyi")
        self.assertEqual(record["queue_id"], "8B0311AA1E3")
        self.assertEqual(record["reporting_mta"], "mail.odile.fyi")
        self.assertIn("unauthenticated", record["recipients"][0]["diagnostic"])

    def test_soft_bounce_is_soft_not_hard(self):
        record = mail_dsn.parse_report(soft_bounce())
        assert record is not None
        self.assertEqual(record["bounce"], "soft")
        self.assertEqual(record["recipients"][0]["status"], "4.4.1")
        self.assertEqual(record["recipients"][0]["action"], "delayed")

    def test_every_recipient_in_a_multi_recipient_report_is_kept(self):
        record = mail_dsn.parse_report(two_recipients())
        assert record is not None
        self.assertEqual(len(record["recipients"]), 2)
        statuses = {item["status"] for item in record["recipients"]}
        self.assertEqual(statuses, {"5.2.1", "4.4.1"})
        self.assertEqual(record["bounce"], "hard")

    def test_feedback_report_is_a_complaint_not_a_bounce(self):
        record = mail_dsn.parse_report(feedback_report())
        assert record is not None
        self.assertEqual(record["kind"], "complaint")
        self.assertEqual(record["feedback_type"], "abuse")
        self.assertEqual(record["original_recipient"], "someone@example.com")
        self.assertEqual(record["original_sender"], "b-20261106-5dbc28f67be4@mail.odile.fyi")
        self.assertNotIn("bounce", record)

    def test_dmarc_aggregate_report_is_ignored(self):
        self.assertIsNone(mail_dsn.parse_report(dmarc_aggregate()))

    def test_ordinary_mail_is_ignored(self):
        raw = b"From: a@b.c\r\nTo: dsn@mail.odile.fyi\r\nSubject: hello\r\n\r\nhi\r\n"
        self.assertIsNone(mail_dsn.parse_report(raw))

    def test_a_report_without_a_delivery_status_part_is_not_a_bounce(self):
        raw = hard_bounce().replace(b"message/delivery-status", b"text/plain")
        self.assertIsNone(mail_dsn.parse_report(raw))


class ProcessTests(unittest.TestCase):
    def _maildir(self, tmp: Path) -> Path:
        base = tmp / "Maildir"
        for sub in ("new", "cur", "tmp"):
            (base / sub).mkdir(parents=True)
        return base

    def test_process_appends_one_record_per_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._maildir(Path(tmp))
            (base / "new" / "1.host").write_bytes(hard_bounce())
            (base / "new" / "2.host").write_bytes(feedback_report())
            (base / "new" / "3.host").write_bytes(dmarc_aggregate())
            out = Path(tmp) / "dsn.jsonl"
            counts = mail_dsn.process(base, out)
            self.assertEqual(counts["dsn"], 1)
            self.assertEqual(counts["complaint"], 1)
            self.assertEqual(counts["ignored"], 1)
            records = [json.loads(line) for line in out.read_text().splitlines()]
            self.assertEqual(len(records), 2)
            self.assertTrue(all("source" in record and "parsed_at" in record for record in records))

    def test_process_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._maildir(Path(tmp))
            (base / "new" / "1.host").write_bytes(hard_bounce())
            out = Path(tmp) / "dsn.jsonl"
            mail_dsn.process(base, out)
            first = out.read_text()
            counts = mail_dsn.process(base, out)
            self.assertEqual(counts["appended"], 0)
            self.assertEqual(counts["duplicate"], 1)
            self.assertEqual(out.read_text(), first)

    def test_process_reports_an_empty_maildir_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._maildir(Path(tmp))
            out = Path(tmp) / "dsn.jsonl"
            counts = mail_dsn.process(base, out)
            self.assertEqual(counts["scanned"], 0)
            self.assertFalse(out.exists())

    def test_dry_run_shows_records_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._maildir(Path(tmp))
            (base / "new" / "1.host").write_bytes(hard_bounce())
            out = Path(tmp) / "dsn.jsonl"
            counts = mail_dsn.process(base, out, dry_run=True)
            self.assertEqual(counts["dsn"], 1)
            self.assertFalse(out.exists())

    def test_cur_is_read_as_well_as_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._maildir(Path(tmp))
            (base / "cur" / "1.host:2,S").write_bytes(soft_bounce())
            out = Path(tmp) / "dsn.jsonl"
            counts = mail_dsn.process(base, out)
            self.assertEqual(counts["dsn"], 1)


class RealReportTests(unittest.TestCase):
    """The card asks for the real rejected message from HQ, when it is present."""

    def test_the_real_dsn_on_hq_parses(self):
        if not REAL_MAILDIR.is_dir():
            self.skipTest("not on HQ: /var/mail/dsn/Maildir is absent")
        records = []
        for sub in ("new", "cur"):
            for path in sorted((REAL_MAILDIR / sub).glob("*")):
                record = mail_dsn.parse_file(path)
                if record:
                    records.append(record)
        if not records:
            self.skipTest("the bounce mailbox has no delivery report right now")
        hard = [record for record in records if record["kind"] == "dsn" and record["bounce"] == "hard"]
        self.assertTrue(hard, "expected the 550 5.7.26 rejection to parse as a hard bounce")
        statuses = {item["status"] for record in hard for item in record["recipients"]}
        self.assertIn("5.7.26", statuses)
        self.assertTrue(any("gmail.com" in (record["recipient"] or "") for record in hard))


class OutboundAddressFamilyTests(unittest.TestCase):
    """Which of our two addresses the relay hands a message to decides Gmail's verdict.

    Measured 2026-11-22 (`docs/evidence/t_7378e496/`): the same brief, from the
    same sender to the same recipient, delivered over the A was filed to Spam
    4 of 4 times, and over the AAAA to the INBOX 4 of 4, with SPF, DKIM and
    DMARC passing on every copy. (The two addresses are the relay's A and AAAA,
    scrubbed to 203.0.113.139 and 2001:0db8:0:1c9::1 here as in every committed
    page.) Postfix picks between them at random by default
    (`smtp_address_preference = any`), so a tenant's first brief was a coin
    flip.
    """

    def test_the_committed_config_prefers_ipv6(self):
        settings = POSTFIX_APPLY.main_cf_settings()
        self.assertEqual(settings.get("smtp_address_preference"), "ipv6")

    def test_the_live_config_carries_the_committed_setting(self):
        live = Path("/etc/postfix/main.cf")
        if not live.is_file():
            self.skipTest("not on the mail host: /etc/postfix/main.cf is absent")
        settings = POSTFIX_APPLY.main_cf_settings(live)
        if settings.get("myhostname") != "mail.odile.fyi":
            self.skipTest("not Odile's relay: this host's main.cf has another myhostname")
        # A hand edit to /etc/postfix is drift the applier exists to remove, so
        # the live file must already say what the committed file says -- after
        # `deploy/postfix/apply.py --apply`, never `postconf -e` alone.
        self.assertEqual(settings.get("smtp_address_preference"), "ipv6")


class DeployCaptureTests(unittest.TestCase):
    """The deploy trees must fail on a fresh host's missing pieces, not say clean.

    The verifier's round-1 finding was exactly this: `--check` printed "clean"
    on a host with no bounce mailbox and no alias database. These tests run the
    appliers' own offline checks, so `make check` covers the fresh-host path and
    not only the machine the card was built on. `--self-test` on each script is
    the same code, for a run where the repository is not checked out.
    """

    def test_postfix_fresh_host_checks(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(POSTFIX_APPLY.self_test(), 0, output.getvalue())

    def test_opendkim_rotation_checks(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(OPENDKIM_APPLY.self_test(), 0, output.getvalue())


if __name__ == "__main__":
    unittest.main()
