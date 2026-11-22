"""The receipt tool must say SPAM when Gmail filed the message to Spam.

Measured 2026-11-22 (docs/evidence/t_7378e496): a message in `[Gmail]/Spam`
answers `X-GM-LABELS ()`. It carries no `\\Spam` label -- the folder is the
only thing that says where it is -- so a placement read from labels alone
reported OTHER for a message Gmail had quarantined.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("send_test", ROOT / "scripts" / "send_test.py")
assert SPEC and SPEC.loader
send_test = importlib.util.module_from_spec(SPEC)
sys.modules["send_test"] = send_test
SPEC.loader.exec_module(send_test)

RAW = (b"From: Odile <brief-x@mail.odile.fyi>\r\n"
       b"To: someone@gmail.com\r\n"
       b"Subject: Sunday, 22 November - your brief\r\n"
       b"Authentication-Results: mx.google.com; dkim=pass header.i=@mail.odile.fyi;\r\n"
       b"\tspf=pass smtp.mailfrom=b-1@mail.odile.fyi; dmarc=pass\r\n"
       b"\r\nbody\r\n")


class FakeIMAP:
    """Enough of imaplib for gmail_fetch: select, search, fetch, login, logout."""

    def __init__(self, copies):
        # {folder: [(uid, label_payload)]}
        self.copies = copies
        self.selected = None
        self.searched = []

    def login(self, user, password):
        self.user, self.password = user, password
        return "OK", [b"logged in"]

    def logout(self):
        return "BYE", [b""]

    def select(self, folder, readonly=False):
        self.selected = folder
        return "OK", [b"1"]

    def search(self, *args):
        self.searched.append((self.selected, args))
        uids = [uid for uid, _ in self.copies.get(self.selected, [])]
        return "OK", [b" ".join(uid.encode() for uid in uids)]

    def fetch(self, uid, spec):
        key = self.selected
        for row_uid, labels in self.copies.get(key, []):
            if row_uid == uid:
                if "X-GM-LABELS" in spec:
                    return "OK", [f"{uid} (X-GM-LABELS ({labels}))".encode()]
                return "OK", [(b"1 (RFC822 {5}", RAW)]
        return "NO", [b""]


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="odile-send-test-")
        secret = Path(self.tmp.name) / "gmail.pass"
        secret.write_text("fifteencharpass\n", encoding="utf-8")
        secret.chmod(0o600)
        self.saved = (send_test.GMAIL_PASSWORD_FILE, send_test.imaplib.IMAP4_SSL)
        send_test.GMAIL_PASSWORD_FILE = str(secret)

    def tearDown(self):
        send_test.GMAIL_PASSWORD_FILE, send_test.imaplib.IMAP4_SSL = self.saved
        self.tmp.cleanup()

    def headers(self, copies):
        send_test.imaplib.IMAP4_SSL = lambda host, port: FakeIMAP(copies)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = send_test.cmd_gmail_headers(
                type("Args", (), {"gmail_headers": "your brief",
                                  "user": "someone@gmail.com"})())
        return code, output.getvalue()

    def test_a_message_in_the_spam_folder_is_reported_as_spam(self):
        # The measured shape: All Mail answers nothing, Spam answers the uid,
        # and that copy's label list is empty.
        code, output = self.headers({send_test.GMAIL_SPAM: [("4", "")]})
        self.assertIn("placement: SPAM", output, output)
        self.assertIn("FAIL  inbox  SPAM", output, output)
        self.assertEqual(code, 1)

    def test_a_message_in_the_inbox_is_still_the_inbox(self):
        code, output = self.headers({send_test.GMAIL_ALL_MAIL: [("59", '"\\\\Inbox"')]})
        self.assertIn("placement: INBOX", output, output)
        self.assertIn("PASS  inbox  INBOX", output, output)
        self.assertEqual(code, 0)

    def test_an_unlabelled_message_elsewhere_is_still_other(self):
        code, output = self.headers({send_test.GMAIL_ALL_MAIL: [("12", "")]})
        self.assertIn("placement: OTHER", output, output)
        self.assertEqual(code, 1)

    def test_no_message_at_all_is_an_error_not_a_verdict(self):
        send_test.imaplib.IMAP4_SSL = lambda host, port: FakeIMAP({})
        with self.assertRaises(send_test.Fail):
            send_test.gmail_fetch("your brief", "someone@gmail.com")


if __name__ == "__main__":
    unittest.main()
