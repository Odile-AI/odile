#!/usr/bin/python3
"""Tests for ODL-459: the claim page, the ready email and the first visit.

The strict bar removes the boot key and the terminal from the user's path, so
these tests assert the surfaces a browser sees and the one message the mailer
queues:

* the claim form asks for the invited email and one consent tick, and the POST
  stores a throwaway public key with no private half;
* the ready email carries the URL, the expected measurement and one link, and
  none of the old ceremony commands;
* the ceremony page explains the first visit, not a terminal ritual.

Hermetic: a throwaway SQLite file, the real app on loopback, and no provider,
DNS, mail or network credential.

    /usr/bin/python3 -m unittest tests.test_claim_page -v
"""

from __future__ import annotations

import hashlib
import http.client
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from odilectl import claim  # noqa: E402
from odilectl.config import Config  # noqa: E402
from odilectl.errors import ConfigError  # noqa: E402
from odilectl.providers.base import validate_ssh_public_key  # noqa: E402

EMAIL = "ada@example.com"
MEASUREMENT = "cd" * 48
# The decided v3.9 rows (docs/product/CLAIMS-RECUT-STRICT.md §2–§3 on
# odl/ODL-461), verbatim. The deck lands in ODL-462; until then the pages quote
# these exact sentences, and this test is what keeps a paraphrase from slipping
# back in.
# ODL-466 §0.2 holds "The only sign-in is yours." until t_adcdec31: before the
# first visit the image's public bench password opens the workspace. The page
# keeps the tenant-scoped operator sentence (ODL-459's accepted form).
ROW_NO_LOGIN = "No one at Odile can log in to your machine."
# "Not the disk key" is held until the first-boot re-key (t_28dda3b7).
ROW_HQ = ("At HQ we hold your email, the record of your machine, and a boot key "
          "with no private half.")
# The lede is the row's first two sentences: the tail puts "set your sign-in"
# before "check the report", which the claimed page and /ceremony contradict.
# The row reorder is ODL-461's; until then the page carries the subset.
# "3 minutes" was never clocked (ODL-466 §2); the row dropped the number.
ROW_FIRST_RUN = "Claiming is a form and a confirmation. Then the machine builds itself."
ROW_FIRST_RUN_TAIL = "you set your sign-in, check the report, and start"
ROW_BOOT_KEY = ("The boot key in the command line is a public key with no private "
                "half. It is plumbing, not a credential.")
# A3 is held from the public pages until the first-boot re-key (t_28dda3b7):
# the published image's master key still opens the tenant disk.
ROW_SEAL = ("The disk unlocks only on the boot you verify. Change the image, and "
            "it stays locked.")
# The no-passphrase half is proven; the custody half is held (t_28dda3b7).
ROW_NO_PASSPHRASE = "There is no passphrase to type."
ROW_REBOOT = "After a reboot, it starts itself. The seal is the key."
# A10 is held from the public pages until ODL-217 proves the grant; ODL-474
# returns it. The old "a reboot closes it" wording is false and must not ship.
ROW_SUPPORT = "A support grant is a time-boxed door into the running machine"
ROW_SIGN_IN = ("Your Odile sign-in is set on your machine at first visit and "
               "hashed there. We never see it.")
ROW_PASS_FAIL = ("Check the machine before you trust it: the verify page shows "
                 "PASS or FAIL, and you set your sign-in only after PASS.")
OLD_COMMANDS = ("ssh", "scp", "ssh-keygen", "passphrase", "encrypt",
                "unlock", "installer", "shred", "setup token", "terminal")
BASE64_RE = re.compile(r"base64,[A-Za-z0-9+/=]+")
WORD_RE = re.compile(r"(?<![\w-])(ssh|scp|shred|installer)(?![\w-])")


def visible(page: str) -> str:
    """The page text without the embedded font's base64, for word checks."""
    return BASE64_RE.sub("base64,", page)


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


def request(port: int, method: str, path: str,
            body: dict | None = None) -> tuple[int, str]:
    payload = urllib.parse.urlencode(body) if body is not None else None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    text = response.read().decode("utf-8")
    conn.close()
    return response.status, text


class ClaimPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        directory = Path(cls.tmp.name)
        cls.db_path = directory / "waitlist.db"
        cls.attest = directory / "attestation.json"
        con = claim.open_db(cls.db_path)
        con.close()
        cls.proc, cls.port = start_app(cls.db_path, cls.attest)

    @classmethod
    def tearDownClass(cls) -> None:
        stop_app(cls.proc)
        cls.tmp.cleanup()

    def setUp(self) -> None:
        # One fresh invitation per test: the tests may claim, refuse or expire
        # it without an order dependency.
        tag = self.id().rsplit(".", 1)[-1]
        digest = hashlib.sha256(tag.encode()).hexdigest()
        self.slug = "odl-" + digest[:6]
        self.email = digest[:10] + "@example.com"
        con = claim.open_db(self.db_path)
        try:
            self.issued = claim.issue_claim(
                con, self.email, plan="Blackbox Plus", slug=self.slug,
                now=datetime.now(timezone.utc),
            )
        finally:
            con.close()

    def row(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        con = claim.connect(self.db_path)
        try:
            return con.execute(sql, args).fetchone()
        finally:
            con.close()

    def rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        con = claim.connect(self.db_path)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    # -- the claim page ---------------------------------------------------

    def test_the_form_asks_for_email_and_consent_only(self) -> None:
        status, page = request(self.port, "GET", "/claim?t=" + self.issued.token)
        self.assertEqual(status, 200, page)
        # The invited address is the readonly field's value; the old
        # "Invitation for" line above the form is gone.
        self.assertIn(f'value="{self.email}"', page)
        self.assertIn("readonly", page)
        self.assertNotIn("Invitation for", page)
        self.assertIn('name="email"', page)
        self.assertIn('name="consent"', page)
        self.assertIn('type="checkbox"', page)
        self.assertIn(f'name="token" value="{self.issued.token}"', page)
        self.assertNotIn('name="key"', page)
        self.assertNotIn("<textarea", page)
        clean = visible(page)
        self.assertNotIn("ssh", clean.lower())
        self.assertNotIn("<code", clean.lower())
        self.assertNotIn("passphrase", clean.lower())
        self.assertNotIn("installer", clean.lower())
        self.assertNotIn("<script", page.lower())
        self.assertNotIn("{{", page)
        self.assertNotIn('href="http', page)
        self.assertNotIn('src="http', page)
        # The trust moment — key custody and the reboot — is above the form;
        # the fold rule lives in BRAND.md §Spacing. A10 is held until ODL-217
        # proves the grant, so it must not appear yet.
        self.assertLess(page.index("Who holds what"), page.index("<form"))
        for row in (ROW_NO_LOGIN, ROW_HQ, ROW_REBOOT):
            self.assertIn(row, page)
        self.assertNotIn(ROW_SUPPORT, page)
        # The lede is the decided row's first two sentences, verbatim.
        self.assertIn(ROW_FIRST_RUN, page)
        self.assertNotIn(ROW_FIRST_RUN_TAIL, page)

    def test_the_branded_error_is_the_shipped_state(self) -> None:
        """The form is novalidate, so the server's in-place message ships.

        A native `required` bubble would pre-empt the branded consent error and
        is unstyled by the sheet; the server answers 422 with the message
        inside the checkbox row instead.
        """
        status, page = request(self.port, "GET", "/claim?t=" + self.issued.token)
        self.assertEqual(status, 200, page)
        form = re.search(r"<form[^>]*>", page)
        assert form is not None
        self.assertIn("novalidate", form.group(0))

    def test_a_claim_without_consent_changes_nothing(self) -> None:
        status, page = request(self.port, "POST", "/claim", {
            "step": "claim",
            "token": self.issued.token,
            "email": self.email,
        })
        self.assertEqual(status, 422, page)
        self.assertIn("I agree to the terms", page)
        # The mistake is the checkbox: the message sits inside the .check row,
        # the checkbox carries the ARIA pair, the email field is untouched.
        check = re.search(r'<div class="check">(.*?)</div>', page, re.S)
        assert check is not None
        self.assertIn('id="consent-err"', check.group(1))
        self.assertIn('role="alert"', check.group(1))
        consent = re.search(r'<input[^>]*id="consent"[^>]*>', page)
        assert consent is not None
        self.assertIn('aria-invalid="true"', consent.group(0))
        self.assertIn('aria-describedby="consent-err"', consent.group(0))
        email = re.search(r'<input[^>]*id="email"[^>]*>', page)
        assert email is not None
        self.assertNotIn("aria-invalid", email.group(0))
        self.assertNotIn('id="email-err"', page)
        self.assertNotIn("{{", page)
        tenant = self.row("SELECT * FROM tenants WHERE slug = ?", (self.slug,))
        assert tenant is not None
        self.assertEqual(tenant["state"], "invited")
        self.assertIsNone(tenant["ssh_public_key"])
        self.assertIsNone(tenant["claimed_at"])
        ledger = self.row(
            "SELECT used_at FROM claims WHERE token_hash = ?",
            (claim.hash_token(self.issued.token),),
        )
        assert ledger is not None
        self.assertIsNone(ledger["used_at"])

    def test_a_claim_with_consent_stores_a_throwaway_key(self) -> None:
        status, page = request(self.port, "POST", "/claim", {
            "step": "claim",
            "token": self.issued.token,
            "email": self.email,
            "consent": "yes",
        })
        self.assertEqual(status, 200, page)
        self.assertIn("Your machine is claimed.", page)
        self.assertIn("We will email you when the machine is ready.", page)
        self.assertNotIn('name="consent"', page)
        self.assertNotIn("{{", page)

        tenant = self.row("SELECT * FROM tenants WHERE slug = ?", (self.slug,))
        assert tenant is not None
        self.assertEqual(tenant["state"], "claimed")
        self.assertIsNotNone(tenant["claimed_at"])
        self.assertEqual(validate_ssh_public_key(tenant["ssh_public_key"]),
                         tenant["ssh_public_key"])
        self.assertTrue(tenant["ssh_public_key"].startswith("ssh-ed25519 "))
        self.assertTrue(tenant["ssh_public_key"].endswith("odile-throwaway"))
        self.assertTrue(claim.ssh_fingerprint(tenant["ssh_public_key"]).startswith("SHA256:"))

        ledger = self.row(
            "SELECT used_at FROM claims WHERE token_hash = ?",
            (claim.hash_token(self.issued.token),),
        )
        assert ledger is not None
        self.assertIsNotNone(ledger["used_at"])
        events = self.rows(
            "SELECT * FROM events WHERE tenant_id = ? AND kind = 'claimed'",
            (tenant["id"],),
        )
        self.assertEqual(len(events), 1)

    def test_a_replay_shows_the_claimed_page(self) -> None:
        status, page = request(self.port, "POST", "/claim", {
            "step": "claim",
            "token": self.issued.token,
            "email": self.email,
            "consent": "yes",
        })
        self.assertEqual(status, 200, page)
        status, page = request(self.port, "GET", "/claim?t=" + self.issued.token)
        self.assertEqual(status, 200, page)
        self.assertIn("Your machine is claimed.", page)
        self.assertNotIn('name="consent"', page)

    def test_a_different_email_is_refused(self) -> None:
        status, page = request(self.port, "POST", "/claim", {
            "step": "claim",
            "token": self.issued.token,
            "email": "someone-else@example.com",
            "consent": "yes",
        })
        self.assertEqual(status, 422, page)
        self.assertIn("does not match the invitation", page)
        # The email is the field at fault; the consent row stays clean.
        self.assertIn('id="email-err"', page)
        email = re.search(r'<input[^>]*id="email"[^>]*>', page)
        assert email is not None
        self.assertIn('aria-invalid="true"', email.group(0))
        self.assertIn('aria-describedby="email-err"', email.group(0))
        consent = re.search(r'<input[^>]*id="consent"[^>]*>', page)
        assert consent is not None
        self.assertNotIn("aria-invalid", consent.group(0))
        self.assertNotIn('id="consent-err"', page)
        tenant = self.row("SELECT * FROM tenants WHERE slug = ?", (self.slug,))
        assert tenant is not None
        self.assertEqual(tenant["state"], "invited")
        self.assertIsNone(tenant["ssh_public_key"])

    def test_an_unknown_token_is_a_closed_page(self) -> None:
        status, page = request(self.port, "GET", "/claim?t=" + "f" * 48)
        self.assertEqual(status, 404, page)
        self.assertIn("This claim link is closed.", page)
        self.assertIn("not valid", page)
        # The closed page has no field: the message is page-level, and the
        # channel for a new invite is named in plain text (no mailto).
        self.assertIn('id="claim-err"', page)
        self.assertNotIn('id="email-err"', page)
        self.assertIn(
            "Ask us at help@odile.fyi for a new invite and this page opens again.",
            page,
        )
        self.assertNotIn("mailto:", page)
        self.assertNotIn("{{", page)

    def test_an_expired_token_is_a_closed_page(self) -> None:
        con = claim.open_db(self.db_path)
        try:
            expired = claim.issue_claim(
                con, "expired@example.com", plan="Blackbox Plus", slug="odl-expired",
                now=datetime.now(timezone.utc) - timedelta(days=30),
            )
        finally:
            con.close()
        status, page = request(self.port, "GET", "/claim?t=" + expired.token)
        self.assertEqual(status, 410, page)
        self.assertIn("This claim link is closed.", page)
        self.assertIn("expired", page)

    def test_get_claim_without_a_token_keeps_the_method_note(self) -> None:
        status, text = request(self.port, "GET", "/claim")
        self.assertEqual(status, 405)
        self.assertIn("POST a claim", text)

    def test_the_throwaway_key_is_unique_and_has_no_private_half(self) -> None:
        one = app.throwaway_boot_key()
        two = app.throwaway_boot_key()
        self.assertNotEqual(one, two)
        self.assertEqual(validate_ssh_public_key(one), one)
        self.assertTrue(claim.ssh_fingerprint(one).startswith("SHA256:"))
        self.assertNotIn("PRIVATE", one)

    # -- the ceremony page ------------------------------------------------

    def test_the_ceremony_page_explains_the_first_visit(self) -> None:
        status, page = request(self.port, "GET", "/ceremony")
        self.assertEqual(status, 200, page)
        self.assertIn("The first visit", page)
        self.assertIn("Check the report", page)
        self.assertIn("Set your sign-in", page)
        self.assertIn("Compare the measurement to the one in your ready email.",
                      page)
        # The claims are the decided v3.9 rows, verbatim; the old
        # key/passphrase/ceremony sentences are gone.
        for row in (ROW_PASS_FAIL, ROW_SIGN_IN, ROW_BOOT_KEY,
                    ROW_NO_PASSPHRASE, ROW_REBOOT, ROW_NO_LOGIN):
            self.assertIn(row, page)
        self.assertNotIn("app-layer gate", page)
        self.assertNotIn("tenant machine", page)
        clean = visible(page)
        self.assertNotIn("<script", clean.lower())
        self.assertNotIn("<code", clean.lower())
        self.assertIsNone(WORD_RE.search(clean.lower()))
        self.assertNotIn("setup token", clean.lower())
        self.assertNotIn("{{", page)

    # -- the ready email --------------------------------------------------

    def test_the_ready_email_has_url_measurement_and_one_link(self) -> None:
        token = "ab" * 32
        first_visit = f"https://{self.slug}.odile.fyi/first-visit?t={token}"
        body = claim.ready_body(
            url=f"https://{self.slug}.odile.fyi", measurement=MEASUREMENT,
            first_visit_url=first_visit,
        )
        self.assertIn(f"URL https://{self.slug}.odile.fyi", body)
        self.assertIn(f"Measurement {MEASUREMENT}", body)
        self.assertIn(f"Set your sign-in: {first_visit}", body)
        self.assertIn(f"Open the first-visit guide: {claim.READY_GUIDE_URL}", body)
        # The subject is the message's first sentence; the body does not
        # repeat it, and the URL is the first thing the reader sees.
        self.assertTrue(body.startswith(f"URL https://{self.slug}.odile.fyi"), body)
        self.assertNotIn(claim.READY_SUBJECT, body)
        self.assertEqual(
            len([line for line in body.splitlines() if line.startswith("Open ")]), 1
        )
        self.assertEqual(body.count(claim.READY_GUIDE_URL), 1)
        # The single-use setup token travels only inside the action URL, and
        # the action URL appears once.
        self.assertEqual(body.count("first-visit?t="), 1)
        self.assertEqual(body.count(token), 1)
        for old in OLD_COMMANDS:
            self.assertNotIn(old, body.lower())
        self.assertNotIn("IPv4", body)
        self.assertNotIn("Host ", body)
        self.assertNotIn("Release ", body)
        self.assertNotIn("Setup token", body)
        self.assertNotIn("{{", body)

    def test_the_template_is_the_one_source_of_the_body(self) -> None:
        template = (ROOT / "templates" / "emails" / "ready.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("{{URL}}", template)
        self.assertIn("{{MEASUREMENT}}", template)
        self.assertIn("{{FIRST_VISIT}}", template)
        self.assertIn("{{GUIDE}}", template)
        for old in OLD_COMMANDS:
            self.assertNotIn(old, template.lower())
        self.assertEqual(
            claim.ready_body(url="https://a.odile.fyi", measurement="ab" * 48,
                             first_visit_url="https://a.odile.fyi/first-visit?t=" + "cd" * 32),
            template.replace("{{URL}}", "https://a.odile.fyi")
                    .replace("{{MEASUREMENT}}", "ab" * 48)
                    .replace("{{FIRST_VISIT}}", "https://a.odile.fyi/first-visit?t=" + "cd" * 32)
                    .replace("{{GUIDE}}", claim.READY_GUIDE_URL),
        )

    def test_the_expected_measurement_comes_from_config_or_the_pin(self) -> None:
        config = Config(Path("fixture"), {"expected_measurement": MEASUREMENT})
        self.assertEqual(claim.expected_measurement(config), MEASUREMENT)
        self.assertRegex(claim.expected_measurement(Config(Path("fixture"), {})),
                         r"^[0-9a-f]{96}$")
        with self.assertRaises(ConfigError):
            claim.expected_measurement(
                Config(Path("fixture"), {"expected_measurement": "not-hex"})
            )

    def test_a_missing_measurement_is_a_config_error(self) -> None:
        with mock.patch.object(claim, "MEASUREMENT_PIN",
                               Path("fixture") / "absent.pin"):
            with self.assertRaises(ConfigError):
                claim.expected_measurement(Config(Path("fixture"), {}))


if __name__ == "__main__":
    unittest.main()
