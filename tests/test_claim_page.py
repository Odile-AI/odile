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
from tests.test_web_pages import REGION, focus_order, main_id  # noqa: E402

EMAIL = "ada@example.com"
MEASUREMENT = "cd" * 48
# The decided v3.9 rows (docs/product/CLAIMS-RECUT-STRICT.md §2–§3 on
# odl/ODL-461), verbatim, with v3.11's re-cuts (ODL-577): the operator row is
# D-3, the HQ data row is D-8, the first-visit sign-in row is D-1 and the
# after-PASS clause is D-7.
# ODL-466 §0.2 holds D-3's "only sign-in" tail until ODL-925: at this head the
# first visit still sets the sign-in, and ODL-521/522/523 cleared the image's
# public bench password that §0.2 named. The page keeps the no-standing-key
# sentence, which is the row's proven half.
ROW_NO_LOGIN = ("No one at Odile holds a key to your machine: no standing key, "
                "no root password, no disk passphrase.")
# "Not the disk key" is held until the first-boot re-key (t_28dda3b7).
ROW_HQ = ("At HQ we hold your email, the record of your machine, a boot key "
          "with no private half, a hash of your password and a hash of your "
          "recovery code.")
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
# returned it after that drill. The old "a reboot closes it" wording is false
# and must not ship.
ROW_SUPPORT = "A support grant is a time-boxed door into the running machine"
# D-1's first two sentences, which the ceremony page and the claim page carry;
# the third ("the only app-layer gate") is held in §1a until ODL-925.
ROW_SIGN_IN = ("Your Odile password opens your machine and your account page. "
               "You chose it when you registered.")
ROW_PASS_FAIL = ("Check the machine before you trust it: the verify page shows "
                 "PASS or FAIL.")
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
        # D-3's "no disk passphrase" is a negative and stays; the v3.4
        # ceremony's "your passphrase" is what must not return.
        self.assertNotIn("your passphrase", clean.lower())
        self.assertNotIn("passphrase to type", clean.lower())
        self.assertNotIn("installer", clean.lower())
        self.assertNotIn("<script", page.lower())
        self.assertNotIn("{{", page)
        self.assertNotIn('href="http', page)
        self.assertNotIn('src="http', page)
        # The trust moment — key custody and the reboot — is above the form;
        # the fold rule lives in BRAND.md §Spacing. A10 returned with ODL-474
        # after ODL-217's grant drill.
        self.assertLess(page.index("Who holds what"), page.index("<form"))
        for row in (ROW_NO_LOGIN, ROW_SUPPORT, ROW_HQ, ROW_REBOOT):
            self.assertIn(row, page)
        # The lede is the decided row's first two sentences, verbatim.
        self.assertIn(ROW_FIRST_RUN, page)
        self.assertNotIn(ROW_FIRST_RUN_TAIL, page)

    def test_the_form_page_states_no_price(self) -> None:
        """alpha.0 has no price (`docs/MVP-V0.1.md` §0).

        The invite page is the last page a tester reads before the consent tick,
        so the v5 price line ("$49/month. Month-to-month. Cancel anytime.") must
        not survive there. The replacement is the certified invitation and email
        footer wording — "Nothing to pay" and "Odile pays for the machine and
        the inference" — never a figure and never a v5 billing term.
        """
        status, page = request(self.port, "GET", "/claim?t=" + self.issued.token)
        self.assertEqual(status, 200, page)
        clean = visible(page)
        self.assertNotIn("$", clean)
        self.assertNotIn("month-to-month", clean.lower())
        self.assertNotIn("cancel anytime", clean.lower())
        self.assertIn("Nothing to pay.", clean)
        self.assertIn("Odile pays for the machine and the inference.", clean)

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

    def test_every_rendered_claim_state_opens_with_the_skip_link_to_its_main(self) -> None:
        """The three states the claim template can render, not the template.

        The template keeps all three blocks, so its shape is pinned in
        test_web_pages; here each block is pruned by render_claim and the
        document a browser receives is walked in order. The closed link is a
        page with one landmark (main), and still opens with the link.
        """
        status, form = request(self.port, "GET", "/claim?t=" + self.issued.token)
        self.assertEqual(status, 200, form)
        status, claimed = request(self.port, "POST", "/claim", {
            "step": "claim",
            "token": self.issued.token,
            "email": self.email,
            "consent": "yes",
        })
        self.assertEqual(status, 200, claimed)
        status, closed = request(self.port, "GET", "/claim?t=" + "f" * 48)
        self.assertEqual(status, 404, closed)
        states = {
            "form": (form, ["who", "next"]),
            "claimed": (claimed, ["next"]),
            "closed": (closed, []),
        }
        for name, (body, regions) in states.items():
            with self.subTest(state=name):
                target = main_id(body)
                self.assertIsNotNone(target, "the state's <main> carries no id")
                stops = focus_order(body)
                self.assertTrue(stops, "the state has no focusable element")
                self.assertEqual(stops[0]["tag"], "a",
                                 "the first tab stop is <%s>, not the skip link"
                                 % stops[0]["tag"])
                self.assertEqual(stops[0]["attributes"].get("href"), "#" + str(target),
                                 "the skip link points at %r, not the state's main"
                                 % stops[0]["attributes"].get("href"))
                self.assertEqual(len(re.findall(r"<h1\b", body)), 1,
                                 "the state's one heading moved")
                self.assertEqual(len(re.findall(r"<main\b", body)), 1)
                self.assertEqual(REGION.findall(body), regions,
                                 "a landmark was added or removed")

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
        self.assertIn("Sign in", page)
        self.assertIn("Compare the measurement to the one in your ready email.",
                      page)
        # The claims are the decided v3.11 rows, verbatim; the old
        # key/passphrase/ceremony sentences are gone.
        for row in (ROW_PASS_FAIL, ROW_SIGN_IN, ROW_BOOT_KEY,
                    ROW_NO_PASSPHRASE, ROW_REBOOT, ROW_NO_LOGIN):
            self.assertIn(row, page)
        self.assertNotIn("Set your sign-in", page)
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
        body = claim.ready_body(
            url=f"https://{self.slug}.odile.fyi", measurement=MEASUREMENT,
        )
        self.assertIn(f"URL https://{self.slug}.odile.fyi", body)
        self.assertIn(f"Measurement {MEASUREMENT}", body)
        # B-once (ODL-925): the action names the tokenless page and the
        # password chosen at registration; no setup token travels in the mail.
        self.assertIn(
            f"Sign in: https://{self.slug}.odile.fyi/first-visit — "
            "the password you chose when you registered.", body)
        self.assertIn(
            "Check the report before you sign in; it says PASS or FAIL.", body)
        # D-7 said once, followed by D-10's line (brand round).
        self.assertEqual(body.count("it says PASS or FAIL"), 1)
        self.assertIn("That password opens", body)
        self.assertNotIn("Check the report first", body)
        self.assertIn(
            f"The first-visit guide is at {claim.READY_GUIDE_URL}", body
        )
        # The subject is the message's own line, not its first sentence; the
        # body does not repeat it, and the URL is the first thing the reader sees.
        self.assertTrue(body.startswith(f"URL https://{self.slug}.odile.fyi"), body)
        self.assertNotIn(claim.READY_SUBJECT, body)
        # One imperative URL — the first-visit page. The guide is a named
        # address in a sentence, so no line opens with an imperative for it.
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
        self.assertEqual(body.count(claim.READY_GUIDE_URL), 1)
        # The reset line survives: §3.2's three surfaces keep the URL findable.
        self.assertIn(f"https://{self.slug}.odile.fyi/first-visit/reset", body)
        self.assertEqual(body.count("first-visit?t="), 0)
        self.assertEqual(body.count("?t="), 0)
        self.assertNotIn("Set your sign-in", body)
        self.assertNotIn("only after", body)
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
        self.assertIn("{{GUIDE}}", template)
        self.assertNotIn("{{FIRST_VISIT}}", template)
        self.assertEqual(template.count("{{URL}}/first-visit"), 2,
                         "the page and its reset line, both tokenless")
        for old in OLD_COMMANDS:
            self.assertNotIn(old, template.lower())
        self.assertEqual(
            claim.ready_body(url="https://a.odile.fyi", measurement="ab" * 48),
            template.replace("{{URL}}", "https://a.odile.fyi")
                    .replace("{{MEASUREMENT}}", "ab" * 48)
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
