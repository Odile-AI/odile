#!/usr/bin/python3
"""The registration → tenant → machine path (ODL-341).

Medium tests: the real `app.py` on a loopback socket and a throwaway database,
plus the root worker driven directly with a recorded provider. No machine is
ordered and no DNS is written; the live run is docs/evidence/a2-registration.md.

What is pinned here, in the card's own words:

* one POST makes one account row (unverified) and one confirmation email, and
  orders nothing; a second identical POST makes no second row;
* the trial is written once, at registration, and nothing later moves it;
* the confirmation POST spends the token once, sets `email_verified_at`, and
  only then queues one provision; a third click is harmless;
* the queue becomes a machine through the same `provision.run` the operator
  CLI calls, with the tenant row's one public key; the row records the machine
  id, the IPv4 and the key's label/first characters;
* a failed provision leaves no billed machine and a retryable reason;
* the seed the worker's token buys carries `sign_in_hash`, byte-equal to
  `accounts.password_hash`, and no forbidden/password key;
* the session cookie carries the row's epoch, and a bump refuses a second
  browser on its next request;
* the registration page carries one approved sentence, four fields, one button,
  no JavaScript and no third-party request;
* the privacy inventory names every `accounts.*` column.

    /usr/bin/python3 -m unittest tests.test_register -v
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import http.client
import importlib.util
import json
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
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import seed.seed as seed  # noqa: E402
from odilectl import keys, provision  # noqa: E402
from odilectl.config import Config  # noqa: E402
from odilectl.errors import ProviderError  # noqa: E402
from odilectl.providers.base import Machine  # noqa: E402

INSTALLER = ROOT / "installer" / "odile_install.py"
SIGN_IN = "a long enough password"
EMAIL_RE = re.compile(r"/confirm\?t=([0-9a-f]{64})")
SENTENCE = ("One password opens your machine and your account page. Your first "
            "sign-in shows you a recovery code \u2014 keep it somewhere off the machine.")
BOOT_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICFh7hQkST5vRDolO3yqwCARRugZahMdc+WgR69JsoXG "
    "tenant@odile"
)


# --------------------------------------------------------------------- helpers


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_health(port: int, proc: subprocess.Popen, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("app.py exited: %s" % proc.stdout.read().decode())
        try:
            status, _headers, _body = request(port, "GET", "/health")
        except OSError:
            time.sleep(0.1)
            continue
        if status == 200:
            return
        time.sleep(0.1)
    raise RuntimeError("app.py did not answer /health")


def request(port: int, method: str, path: str, body: str | None = None,
            cookie: str | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Connection": "close"}
    if cookie:
        headers["Cookie"] = cookie
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    data = response.read()
    result = (response.status, dict(response.getheaders()), data)
    connection.close()
    return result


def form(**fields) -> str:
    return urllib.parse.urlencode(fields)


class FlowBase(unittest.TestCase):
    """One real service per class, one throwaway database and outbox."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="odile-register-")
        cls.dir = Path(cls.tmp.name)
        cls.db = cls.dir / "waitlist.db"
        cls.outbox = cls.dir / "outbox"
        cls.port = free_port()
        env = dict(
            os.environ,
            ODILE_BIND="127.0.0.1",
            ODILE_PORT=str(cls.port),
            ODILE_RATE_LIMIT="1000",
            ODILE_DB=str(cls.db),
            ODILE_OUTBOX=str(cls.outbox),
            ODILE_SESSION_KEY_FILE=str(cls.dir / "session.key"),
            ODILE_ATTEST=str(cls.dir / "attestation.json"),
            ODILE_ROADMAP=str(cls.dir / "roadmap.html"),
            ODILE_ACCOUNT_HOST="https://test.odile.fyi",
            ODILE_TENANT_DOMAIN="test.odile.fyi",
        )
        cls.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "app.py")], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        wait_health(cls.port, cls.proc)
        cls._counter = 0

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proc.terminate()
        try:
            cls.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.communicate()
        cls.tmp.cleanup()

    def email(self) -> str:
        type(self)._counter += 1
        return "tester%d@example.com" % type(self)._counter

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def account(self, email: str) -> sqlite3.Row | None:
        connection = self.connect()
        try:
            return connection.execute("SELECT * FROM accounts WHERE email = ?",
                                      (email,)).fetchone()
        finally:
            connection.close()

    def tenant(self, slug: str) -> sqlite3.Row | None:
        connection = self.connect()
        try:
            return connection.execute("SELECT * FROM tenants WHERE slug = ?",
                                      (slug,)).fetchone()
        finally:
            connection.close()

    def register(self, email: str, password: str = SIGN_IN, repeat: str | None = None,
                 consent: str = "yes", hp: str = ""):
        return request(self.port, "POST", "/register", form(
            email=email, password=password,
            password2=password if repeat is None else repeat,
            consent=consent, hp=hp))

    def confirmation_token(self, account_id: int) -> str:
        path = self.outbox / "confirm" / ("%d.eml" % account_id)
        message = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
        body = message.get_body(preferencelist=("plain",)).get_content()
        match = EMAIL_RE.search(body)
        self.assertIsNotNone(match, body)
        return match.group(1)

    def confirmation_emails(self, account_id: int) -> list[Path]:
        directory = self.outbox / "confirm"
        if not directory.is_dir():
            return []
        return sorted(directory.glob("%d.eml" % account_id))


# ------------------------------------------------------------------ registration


class RegistrationTests(FlowBase):
    def test_one_post_makes_one_unverified_row_and_one_email_and_orders_nothing(self):
        email = self.email()
        status, _headers, body = self.register(email)
        self.assertEqual(status, 200, body[:400])
        self.assertIn(b"Check your email", body)
        account = self.account(email)
        self.assertIsNotNone(account)
        self.assertIsNone(account["email_verified_at"])
        self.assertEqual(account["provision_state"], "unverified")
        self.assertIsNone(account["server_id"])
        self.assertIsNone(account["ipv4"])
        self.assertEqual(len(self.confirmation_emails(account["id"])), 1)
        tenant = self.tenant(account["slug"])
        self.assertIsNotNone(tenant)
        self.assertIsNone(tenant["server_id"])
        self.assertEqual(tenant["state"], "invited")
        self.assertEqual(tenant["email"], email)

    def test_a_second_identical_post_makes_no_second_row_and_moves_no_trial(self):
        email = self.email()
        self.assertEqual(self.register(email)[0], 200)
        first = self.account(email)
        second = self.register(email)[0]
        self.assertEqual(second, 200)
        again = self.account(email)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["trial_started_at"], first["trial_started_at"])
        self.assertEqual(again["trial_ends_at"], first["trial_ends_at"])
        connection = self.connect()
        try:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM accounts WHERE email = ?", (email,)).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM tenants WHERE email = ?", (email,)).fetchone()[0], 1)
        finally:
            connection.close()
        self.assertEqual(len(self.confirmation_emails(first["id"])), 1)
        # The resend mints a new link; the old one is dead, one message waits.
        token = self.confirmation_token(first["id"])
        self.assertNotEqual(token, hashlib.sha256(b"").hexdigest())

    def test_the_trial_is_fourteen_days_written_with_the_row(self):
        email = self.email()
        self.register(email)
        account = self.account(email)
        from datetime import datetime, timedelta, timezone
        start = datetime.strptime(account["trial_started_at"], "%Y-%m-%dT%H:%M:%SZ")
        end = datetime.strptime(account["trial_ends_at"], "%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(end - start, timedelta(days=14))
        self.assertEqual(account["created_at"], account["trial_started_at"])

    def test_the_four_fields_own_their_errors(self):
        email = self.email()
        cases = [
            ("email", dict(email="not an address")),
            ("password", dict(password="short")),
            ("password2", dict(repeat="something else entirely")),
            ("consent", dict(consent="")),
        ]
        for field, kwargs in cases:
            status, _headers, body = self.register(**{"email": email, **kwargs})
            self.assertEqual(status, 422, (field, body[:200]))
            text = body.decode("utf-8")
            self.assertIn('id="%s"' % field, text)
            self.assertIn('aria-describedby="%s-err"' % field, text)
            self.assertIn('id="%s-err"' % field, text)
            # Only the field named by the message carries the ARIA pair.
            others = [name for name in ("email", "password", "password2", "consent")
                      if name != field]
            for name in others:
                self.assertNotIn('aria-describedby="%s-err"' % name, text)
            # A field's message owns its own slot; the page-level slot stays
            # empty, so one submit never shows the same sentence twice.
            self.assertNotIn('id="register-err"', text)
        self.assertIsNone(self.account(email))

    def test_the_consent_tick_survives_a_rejected_form(self):
        """One mistyped password must not cost the reader their consent tick."""
        email = self.email()
        status, _headers, body = self.register(email, repeat="something else entirely")
        self.assertEqual(status, 422)
        text = body.decode("utf-8")
        self.assertRegex(text, r'name="consent"[^>]* checked')
        self.assertIn('value="%s"' % email, text)

    def test_the_page_carries_one_approved_sentence_four_fields_and_no_script(self):
        status, headers, body = request(self.port, "GET", "/register")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn(SENTENCE, text)
        for name in ('name="email"', 'name="password"', 'name="password2"',
                     'name="consent"'):
            self.assertIn(name, text)
        self.assertEqual(text.count("<button type=\"submit\">"), 1)
        self.assertNotIn("<script", text.lower())
        self.assertNotIn("https://", text.replace("https://test.odile.fyi", ""))
        self.assertNotIn("Set-Cookie", headers)

    def test_the_honeypot_creates_nothing(self):
        email = self.email()
        status, _headers, _body = self.register(email, hp="bot")
        self.assertEqual(status, 200)
        self.assertIsNone(self.account(email))

    def test_a_database_error_still_answers_the_page(self):
        """A sqlite3 failure is a 500 screen, not a closed connection.

        Round 2 measured POST /register with the database swapped under the
        live instance: RemoteDisconnected, no body — the button appeared to do
        nothing. The retry loop's own 500 covered only exhaustion; the open and
        the reads now land in the same page-level slot.
        """
        email = self.email()
        moved = self.db.with_name(self.db.name + ".moved")
        if moved.exists():
            moved.unlink()
        os.replace(self.db, moved)
        self.db.mkdir()
        try:
            status, _headers, body = self.register(email)
        finally:
            self.db.rmdir()
            os.replace(moved, self.db)
        self.assertEqual(status, 500, body[:300])
        text = body.decode("utf-8")
        self.assertIn('id="register-err"', text)
        self.assertIn('role="alert"', text)
        self.assertIn("Something broke. Try again?", text)
        self.assertEqual(text.count('role="alert"'), 1)
        self.assertIn('value="%s"' % email, text)
        self.assertIn("Create account", text)
        # The service is healthy again: the same POST now makes the row.
        self.assertEqual(self.register(email)[0], 200)
        self.assertIsNotNone(self.account(email))


class ConfirmTests(FlowBase):
    def test_confirm_spends_once_sets_verified_and_queues_one_provision(self):
        email = self.email()
        self.register(email)
        account = self.account(email)
        token = self.confirmation_token(account["id"])
        status, _headers, body = request(self.port, "GET", "/confirm?t=" + token)
        self.assertEqual(status, 200)
        self.assertIn(b"Confirm my address", body)
        # The sentence that tells the reader to click renders only with the
        # button (Brand gate round 2, should 2).
        self.assertIn(b"One click. After you confirm", body)
        status, headers, body = request(self.port, "POST", "/confirm",
                                        form(token=token))
        self.assertEqual(status, 303, body[:300])
        self.assertEqual(headers.get("Location"), "/account")
        confirmed = self.account(email)
        self.assertIsNotNone(confirmed["email_verified_at"])
        self.assertEqual(confirmed["provision_state"], "queued")
        self.assertIsNotNone(confirmed["provision_queued_at"])
        self.assertIsNone(confirmed["server_id"])
        self.assertTrue(headers.get("Set-Cookie", "").startswith("odile_account="))

    def test_a_third_confirmation_is_harmless(self):
        email = self.email()
        self.register(email)
        account = self.account(email)
        token = self.confirmation_token(account["id"])
        self.assertEqual(request(self.port, "POST", "/confirm", form(token=token))[0], 303)
        first = self.account(email)
        for _ in range(2):
            status, _headers, body = request(self.port, "POST", "/confirm",
                                             form(token=token))
            self.assertEqual(status, 200, body[:200])
            self.assertIn(b"Check your email", body)
        again = self.account(email)
        self.assertEqual(again["provision_queued_at"], first["provision_queued_at"])
        self.assertEqual(again["provision_state"], "queued")

    def test_the_link_expires_after_a_day(self):
        email = self.email()
        self.register(email)
        account = self.account(email)
        token = self.confirmation_token(account["id"])
        connection = self.connect()
        try:
            connection.execute(
                "UPDATE accounts SET confirm_expires_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00Z", account["id"]))
            connection.commit()
        finally:
            connection.close()
        status, _headers, body = request(self.port, "POST", "/confirm", form(token=token))
        self.assertEqual(status, 410, body[:200])
        self.assertIsNone(self.account(email)["email_verified_at"])

    def test_an_unknown_token_is_not_valid(self):
        status, _headers, _body = request(self.port, "POST", "/confirm",
                                          form(token="f" * 64))
        self.assertEqual(status, 404)

    def test_an_unknown_link_shows_the_way_back_to_registration(self):
        """No button that cannot work: the dead link offers /register instead."""
        status, _headers, body = request(self.port, "GET", "/confirm?t=" + "f" * 64)
        self.assertEqual(status, 404)
        text = body.decode("utf-8")
        self.assertIn("That confirmation link is not valid.", text)
        self.assertNotIn("<form", text)
        self.assertNotIn("Confirm my address", text)
        # The sentence about the button goes with the button.
        self.assertNotIn("One click", text)
        self.assertIn('class="note next"', text)
        self.assertIn('href="/register"', text)
        self.assertIn("Register again", text)

    def test_an_expired_link_shows_the_way_back_to_registration(self):
        email = self.email()
        self.register(email)
        account = self.account(email)
        token = self.confirmation_token(account["id"])
        connection = self.connect()
        try:
            connection.execute(
                "UPDATE accounts SET confirm_expires_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00Z", account["id"]))
            connection.commit()
        finally:
            connection.close()
        status, _headers, body = request(self.port, "GET", "/confirm?t=" + token)
        self.assertEqual(status, 410)
        text = body.decode("utf-8")
        self.assertIn("That confirmation link has expired.", text)
        self.assertNotIn("<form", text)
        self.assertNotIn("Confirm my address", text)
        self.assertNotIn("One click", text)
        self.assertIn('class="note next"', text)
        self.assertIn('href="/register"', text)
        self.assertIn("Register again", text)
        # The POST of the same spent-by-time link says the same thing.
        status, _headers, body = request(self.port, "POST", "/confirm",
                                         form(token=token))
        self.assertEqual(status, 410)
        text = body.decode("utf-8")
        self.assertNotIn("<form", text)
        self.assertNotIn("One click", text)
        self.assertIn('href="/register"', text)

    def test_an_already_confirmed_link_points_at_sign_in(self):
        email = self.email()
        self.register(email)
        account = self.account(email)
        token = self.confirmation_token(account["id"])
        self.assertEqual(request(self.port, "POST", "/confirm", form(token=token))[0], 303)
        status, _headers, body = request(self.port, "GET", "/confirm?t=" + token)
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("This address is already confirmed.", text)
        self.assertNotIn("<form", text)
        self.assertNotIn("Confirm my address", text)
        self.assertNotIn("One click", text)
        # The question would repeat the sentence above it; the link is the
        # whole sentence (Brand gate round 2, nit 1).
        self.assertNotIn("Already confirmed?", text)
        self.assertIn('class="note next"', text)
        self.assertIn('href="/signin"', text)
        self.assertIn("Sign in to your account", text)

    def test_the_way_back_is_a_full_size_target(self):
        """The way back is the whole content of its paragraph, so 44 px applies.

        BRAND.md §Accessibility tokens excepts only inline prose links; round 2
        measured these at 118×19 and 57×19. The rendered box is measured in
        the evidence probe; here the rule that produces it is pinned, so
        deleting the rule or the class fails this test.
        """
        status, _headers, body = request(self.port, "GET", "/confirm?t=" + "f" * 64)
        self.assertEqual(status, 404)
        text = body.decode("utf-8")
        self.assertIn('<p class="note next"><a href="/register">Register again</a></p>',
                      text)
        self.assertIn(
            "p.note a{display:inline-flex;align-items:center;min-height:44px}", text)
        self.assertIn("p.next{margin:8px 0 12px}", text)

    def test_a_machine_that_already_exists_is_not_queued_again(self):
        """A replayed confirmation on an existing machine lands `ready`."""
        email = self.email()
        # A tenant the claim path already provisioned, with the address of the
        # registration: the account adopts it instead of ordering a second one.
        slug = "odl-%06x" % (abs(hash(email)) % 0xFFFFFF)
        connection = self.connect()
        try:
            connection.execute(
                "INSERT INTO tenants (slug, email, ssh_public_key, state, plan,"
                " server_id, ipv4, created_at) VALUES (?, ?, ?, 'installed', 'alpha',"
                " 'srv-existing', '203.0.113.7', '2026-01-01T00:00:00Z')",
                (slug, email, BOOT_KEY))
            connection.commit()
        finally:
            connection.close()
        self.register(email)
        account = self.account(email)
        self.assertEqual(account["slug"], slug)
        token = self.confirmation_token(account["id"])
        self.assertEqual(request(self.port, "POST", "/confirm", form(token=token))[0], 303)
        confirmed = self.account(email)
        self.assertEqual(confirmed["provision_state"], "ready")
        self.assertIsNone(confirmed["provision_queued_at"])
        self.assertEqual(confirmed["server_id"], "srv-existing")
        self.assertEqual(confirmed["ipv4"], "203.0.113.7")


# --------------------------------------------------------------------- sessions


class SessionTests(FlowBase):
    def sign_in(self, email: str, password: str = SIGN_IN):
        return request(self.port, "POST", "/signin",
                       form(email=email, password=password))

    def confirmed(self):
        email = self.email()
        self.register(email)
        account = self.account(email)
        token = self.confirmation_token(account["id"])
        request(self.port, "POST", "/confirm", form(token=token))
        return email, self.account(email)

    def test_a_bumped_epoch_refuses_a_signed_in_second_browser(self):
        email, account = self.confirmed()
        first = self.sign_in(email)
        second = self.sign_in(email)
        self.assertEqual(first[0], 303)
        self.assertEqual(second[0], 303)
        cookie_one = first[1]["Set-Cookie"].split(";")[0]
        cookie_two = second[1]["Set-Cookie"].split(";")[0]
        self.assertEqual(request(self.port, "GET", "/account", cookie=cookie_one)[0], 200)
        self.assertEqual(request(self.port, "GET", "/account", cookie=cookie_two)[0], 200)
        connection = self.connect()
        try:
            connection.execute("UPDATE accounts SET session_epoch = session_epoch + 1"
                               " WHERE id = ?", (account["id"],))
            connection.commit()
        finally:
            connection.close()
        for cookie in (cookie_one, cookie_two):
            status, headers, _body = request(self.port, "GET", "/account", cookie=cookie)
            self.assertEqual(status, 303)
            self.assertEqual(headers.get("Location"), "/signin")

    def test_a_forged_cookie_is_refused(self):
        email, account = self.confirmed()
        cookie = "odile_account=v1.%d.1.%s" % (account["id"], "0" * 64)
        status, headers, _body = request(self.port, "GET", "/account", cookie=cookie)
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/signin")

    def test_the_sign_in_seam_way_back_is_a_full_size_target(self):
        """The same 44 px rule reaches the seam's standalone "Register" link.

        Nit 2 of the brand round: the link is the whole content of its
        paragraph, so the inline-prose exception does not apply (ODL-583 owns
        the finished page; this is the rule in passing).
        """
        status, _headers, body = request(self.port, "GET", "/signin")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn('<p class="note"><a href="/register">Register</a></p>', text)
        self.assertIn(
            "p.note a{display:inline-flex;align-items:center;min-height:44px}", text)

    def test_a_wrong_password_is_401(self):
        email, _account = self.confirmed()
        status, _headers, body = self.sign_in(email, "not the password at all")
        self.assertEqual(status, 401)
        self.assertIn(b"do not match", body)

    def test_the_retry_action_accepts_an_empty_body(self):
        """The failed state's one action is a button, so the POST has no fields."""
        email, account = self.confirmed()
        connection = self.connect()
        try:
            connection.execute(
                "UPDATE accounts SET provision_state = 'failed',"
                " provision_reason = 'create: HTTP 404: Plan not found.' WHERE id = ?",
                (account["id"],))
            connection.commit()
        finally:
            connection.close()
        cookie = self.sign_in(email)[1]["Set-Cookie"].split(";")[0]
        status, headers, body = request(self.port, "POST", "/account/retry",
                                        cookie=cookie)
        self.assertEqual(status, 303, body[:200])
        self.assertEqual(headers.get("Location"), "/account")
        again = self.account(email)
        self.assertEqual(again["provision_state"], "queued")
        self.assertIsNone(again["provision_reason"])

    def test_a_cross_site_post_is_refused_and_its_own_origin_is_not(self):
        import http.client
        email, _account = self.confirmed()
        good = {"Origin": "http://127.0.0.1:%d" % self.port,
                "Content-Type": "application/x-www-form-urlencoded",
                "Connection": "close"}
        for path, origin, expected in (
                ("/signin", "https://evil.example", 403),
                ("/signin", "http://127.0.0.1:%d" % self.port, 303),
                ("/account/retry", "https://evil.example", 403)):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            headers = dict(good, Origin=origin)
            connection.request("POST", path, body=form(email=email, password=SIGN_IN),
                               headers=headers)
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, expected, (path, origin))


class RateLimitTests(unittest.TestCase):
    """A burst on the public POSTs answers 429 (ADR-006 §7)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="odile-register-rate-")
        directory = Path(cls.tmp.name)
        cls.port = free_port()
        env = dict(
            os.environ,
            ODILE_BIND="127.0.0.1", ODILE_PORT=str(cls.port), ODILE_RATE_LIMIT="2",
            ODILE_DB=str(directory / "waitlist.db"),
            ODILE_OUTBOX=str(directory / "outbox"),
            ODILE_SESSION_KEY_FILE=str(directory / "session.key"),
            ODILE_ATTEST=str(directory / "a.json"),
            ODILE_ROADMAP=str(directory / "r.html"),
            ODILE_ACCOUNT_HOST="https://test.odile.fyi",
        )
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "app.py")], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        wait_health(cls.port, cls.proc)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proc.terminate()
        try:
            cls.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.communicate()
        cls.tmp.cleanup()

    def test_a_registration_burst_answers_429(self):
        statuses = []
        bodies = []
        for index in range(4):
            status, _headers, body = request(self.port, "POST", "/register", form(
                email="burst%d@example.com" % index, password=SIGN_IN,
                password2=SIGN_IN, consent="yes"))
            statuses.append(status)
            bodies.append(body.decode("utf-8"))
        self.assertEqual(statuses[:2], [200, 200], statuses)
        self.assertEqual(statuses[2:], [429, 429], statuses)
        # The rate limit names no field, so its sentence lands in the form's
        # own page-level slot — never a screen that answers 429 silently
        # (Brand gate round 1, must 1).
        for index, text in enumerate(bodies[2:], start=2):
            self.assertIn('id="register-err"', text)
            self.assertIn('role="alert"', text)
            self.assertIn("Too many registrations from here.", text)
            self.assertEqual(text.count('role="alert"'), 1, index)
            self.assertIn('value="burst%d@example.com"' % index, text)
            self.assertIn("Create account", text)

    def test_a_confirmation_burst_answers_429(self):
        statuses = [request(self.port, "POST", "/confirm",
                            form(token="%064x" % index))[0] for index in range(4)]
        self.assertEqual(statuses[2:], [429, 429], statuses)


# ----------------------------------------------------------------------- worker


class FakeProvider:
    """The provider seam: no socket, no order, one recorded machine."""

    name = "fake"

    def __init__(self, *, fail_poll: bool = False, machines: list | None = None):
        self.fail_poll = fail_poll
        self.machines = list(machines or [])
        self.created = []
        self.specs = []
        self.terminated = []

    def create(self, spec):
        self.specs.append(spec)
        machine = Machine(server_id="srv-%d" % (len(self.created) + 1),
                          hostname=spec.hostname, state="pending", ipv4=None,
                          raw_status="deploying")
        self.created.append(machine)
        return machine

    def poll(self, server_id, *, timeout=900.0, interval=10.0):
        if self.fail_poll:
            raise ProviderError("the provider says the build failed")
        return Machine(server_id=server_id, hostname=self.created[0].hostname,
                       state="active", ipv4="203.0.113.41", raw_status="active")

    def power(self, server_id, action):
        return self.poll(server_id)

    def terminate(self, server_id):
        self.terminated.append(server_id)
        return True

    def list(self):
        return self.machines


class WorkerTests(unittest.TestCase):
    """The root half: the queue becomes a machine, once."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="odile-worker-")
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "waitlist.db"
        connection = sqlite3.connect(self.db)
        app.migrate(connection)
        connection.close()
        self.slug = "odl-abc123"
        self.email = "worker@example.com"
        self.account_id = self.add_account()
        self.config_path = self.write_config()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add_account(self, *, verified: bool = True, state: str | None = None) -> int:
        stamp = "2026-11-22T10:00:00Z"
        verifier = app.hash_password(SIGN_IN)
        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        try:
            if connection.execute("SELECT 1 FROM tenants WHERE slug = ?",
                                  (self.slug,)).fetchone() is None:
                connection.execute(
                    "INSERT INTO tenants (slug, email, ssh_public_key, state, plan,"
                    " created_at) VALUES (?, ?, ?, 'invited', 'alpha', ?)",
                    (self.slug, self.email, BOOT_KEY, stamp))
            connection.execute(
                "INSERT INTO accounts (slug, email, password_hash, confirm_token_hash,"
                " confirm_expires_at, email_verified_at, trial_started_at,"
                " trial_ends_at, provision_state, provision_queued_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.slug, self.email, verifier, hashlib.sha256(b"t").hexdigest(),
                 "2026-11-23T10:00:00Z", stamp if verified else None, stamp,
                 "2026-12-06T10:00:00Z", state or ("queued" if verified else "unverified"),
                 stamp if verified else None, stamp))
            connection.commit()
            return connection.execute(
                "SELECT id FROM accounts WHERE email = ?", (self.email,)).fetchone()[0]
        finally:
            connection.close()

    def write_config(self, **overrides) -> Path:
        path = self.dir / "config.yaml"
        values = {
            "provider": "servury",
            "state_db": str(self.db),
            "seed_db": str(self.db),
            "seed_keys": str(self.dir / "seed-keys.json"),
            "outbox": str(self.dir / "outbox"),
            "default_plan_id": "108",
            "default_days": "7",
            "near_account": str(self.dir / "near-account.json"),
            "near_workspace_id": "ws-test",
            **overrides,
        }
        path.write_text("\n".join("%s: %s" % item for item in values.items()) + "\n",
                        encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def load_config(self, path: Path | None = None):
        return Config.load(path or self.config_path)

    def issued(self):
        return keys.Issued(label="odile-" + self.slug, key_id="key-1",
                           key_prefix="sk-abc****", workspace_id="ws-test",
                           instance_id=None, spend_limit_nano=10_000_000_000,
                           raw_key="sk-abcdefghijklmnop")

    def test_the_queue_becomes_one_machine_and_records_id_ip_and_key(self):
        provider = FakeProvider()
        with mock.patch.object(provision.keys, "issue", return_value=self.issued()), \
                mock.patch.object(provision, "_ensure_dns", return_value="created"):
            results = provision.provision_queue(
                self.load_config(), apply=True, provider_factory=lambda _c: provider)
        self.assertEqual(len(results), 1, results)
        self.assertTrue(results[0]["ok"], results)
        self.assertEqual(len(provider.created), 1)
        self.assertEqual(provider.specs[0].ssh_key.strip(), BOOT_KEY.strip())
        self.assertEqual(provider.specs[0].hostname, self.slug)
        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        try:
            account = connection.execute("SELECT * FROM accounts WHERE id = ?",
                                         (self.account_id,)).fetchone()
            tenant = connection.execute("SELECT * FROM tenants WHERE slug = ?",
                                        (self.slug,)).fetchone()
            raw = connection.execute("SELECT * FROM tenant_keys").fetchone()
        finally:
            connection.close()
        self.assertEqual(account["provision_state"], "ready")
        self.assertEqual(account["server_id"], "srv-1")
        self.assertEqual(account["ipv4"], "203.0.113.41")
        self.assertEqual(account["key_label"], "odile-" + self.slug)
        self.assertEqual(account["key_prefix"], "sk-abc****")
        self.assertEqual(tenant["server_id"], "srv-1")
        self.assertEqual(tenant["ipv4"], "203.0.113.41")
        self.assertEqual(tenant["release"], provision.pinned_release())
        self.assertIsNotNone(raw)
        self.assertEqual(raw["key_prefix"], "sk-abc****")

    def test_a_second_run_orders_nothing(self):
        provider = FakeProvider()
        with mock.patch.object(provision.keys, "issue", return_value=self.issued()), \
                mock.patch.object(provision, "_ensure_dns", return_value="same"):
            provision.provision_queue(self.load_config(), apply=True,
                                      provider_factory=lambda _c: provider)
            second = provision.provision_queue(self.load_config(), apply=True,
                                               provider_factory=lambda _c: provider)
        self.assertEqual(second, [])
        self.assertEqual(len(provider.created), 1)

    def test_a_failed_provision_leaves_no_billed_machine(self):
        provider = FakeProvider(fail_poll=True)
        with mock.patch.object(provision.keys, "issue", return_value=self.issued()), \
                mock.patch.object(provision, "_ensure_dns", return_value="same"):
            results = provision.provision_queue(
                self.load_config(), apply=True, provider_factory=lambda _c: provider)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertIn("build failed", results[0]["reason"])
        self.assertEqual(provider.terminated, ["srv-1"])
        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        try:
            account = connection.execute("SELECT * FROM accounts WHERE id = ?",
                                         (self.account_id,)).fetchone()
            tenant = connection.execute("SELECT * FROM tenants WHERE slug = ?",
                                        (self.slug,)).fetchone()
        finally:
            connection.close()
        self.assertEqual(account["provision_state"], "failed")
        self.assertIn("build failed", account["provision_reason"])
        self.assertIsNone(account["server_id"])
        self.assertIsNone(tenant["server_id"])

    def test_a_retry_after_a_failure_orders_one_machine(self):
        failing = FakeProvider(fail_poll=True)
        with mock.patch.object(provision.keys, "issue", return_value=self.issued()), \
                mock.patch.object(provision, "_ensure_dns", return_value="same"):
            provision.provision_queue(self.load_config(), apply=True,
                                      provider_factory=lambda _c: failing)
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("UPDATE accounts SET provision_state = 'queued'"
                               " WHERE id = ?", (self.account_id,))
            connection.commit()
        finally:
            connection.close()
        provider = FakeProvider()
        with mock.patch.object(provision.keys, "issue", return_value=self.issued()), \
                mock.patch.object(provision, "_ensure_dns", return_value="created"):
            results = provision.provision_queue(
                self.load_config(), apply=True, provider_factory=lambda _c: provider)
        self.assertTrue(results[0]["ok"], results)
        self.assertEqual(len(provider.created), 1)
        self.assertEqual(provider.terminated, [])

    def test_only_verified_and_queued_rows_are_picked(self):
        connection = sqlite3.connect(self.db)
        try:
            connection.execute(
                "INSERT INTO accounts (slug, email, password_hash, confirm_token_hash,"
                " confirm_expires_at, email_verified_at, trial_started_at,"
                " trial_ends_at, provision_state, created_at)"
                " VALUES ('odl-def456', 'unverified@example.com', 'scrypt$x', 'h2',"
                " '2026-11-23T00:00:00Z', NULL, '2026-11-22T00:00:00Z',"
                " '2026-12-06T00:00:00Z', 'queued', '2026-11-22T00:00:00Z')")
            connection.commit()
        finally:
            connection.close()
        provider = FakeProvider()
        with mock.patch.object(provision.keys, "issue", return_value=self.issued()), \
                mock.patch.object(provision, "_ensure_dns", return_value="same"):
            provision.provision_queue(self.load_config(), apply=True,
                                      provider_factory=lambda _c: provider)
        self.assertEqual([m.hostname for m in provider.created], [self.slug])


# ------------------------------------------------------------------------- seed


class SeedTests(unittest.TestCase):
    """The seed the worker's token buys carries the account's verifier."""

    #: The relay's PEM. main's ODL-469 made `mail_relay.tls_ca` required in a
    #: seed config, so this fixture carries the shape main's own seed fixture
    #: carries (tests/test_seed.RELAY_CA); the branch was cut before that rule.
    RELAY_CA = (
        "-----BEGIN CERTIFICATE-----\n"
        "MIIBhTCCASugAwIBAgIUTESTRELAYCA0000000000000000\n"
        "-----END CERTIFICATE-----\n"
    )

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("odile_install_341", INSTALLER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls.installer = module

    CONFIG = {
        "near_api_key": "sk-shared-test",
        "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
        "support_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITEST support@odile.fyi",
        "backup_repo_url": "sftp:odile-backup@backup.odile.fyi:/tenants/{slug}",
        "hermes_version": "2026.11.1",
        "mail_relay": {"host": "smtp.mail.odile.fyi", "port": 587,
                       "username": "{slug}@mail.odile.fyi", "secret": "relay-0001",
                       "tls_ca": RELAY_CA},
    }

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="odile-seed-341-")
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "waitlist.db"
        connection = sqlite3.connect(self.db)
        app.migrate(connection)
        seed.ensure_schema(connection)
        self.slug = "odl-341aaa"
        self.instance = "srv-341"
        self.token = "ab" * 32
        stamp = "2026-11-22T10:00:00Z"
        connection.execute(
            "INSERT INTO tenants (slug, email, ssh_public_key, state, plan, server_id,"
            " created_at) VALUES (?, ?, ?, 'provisioned', 'alpha', ?, ?)",
            (self.slug, "seed@example.com", BOOT_KEY, self.instance, stamp))
        self.verifier = app.hash_password(SIGN_IN)
        self.account_id = connection.execute(
            "INSERT INTO accounts (slug, email, password_hash, confirm_token_hash,"
            " confirm_expires_at, email_verified_at, trial_started_at, trial_ends_at,"
            " provision_state, created_at) VALUES (?, ?, ?, 'h', '2026-11-23T10:00:00Z',"
            " ?, ?, ?, 'ready', ?)",
            (self.slug, "seed@example.com", self.verifier, stamp, stamp,
             "2026-12-06T10:00:00Z", stamp)).lastrowid
        tenant_id = connection.execute("SELECT id FROM tenants WHERE slug = ?",
                                       (self.slug,)).fetchone()[0]
        connection.execute(
            "INSERT INTO seed_tokens (tenant_id, token_hash, instance_id, created_at,"
            " expires_at) VALUES (?, ?, ?, ?, ?)",
            (tenant_id, seed.hash_token(self.token), self.instance, stamp,
             "2026-11-23T10:00:00Z"))
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def redeem(self, *, with_account: bool = True) -> dict:
        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        if not with_account:
            connection.execute("DELETE FROM accounts")
            connection.commit()
        try:
            return seed.redeem(
                connection, self.token, self.instance, self.CONFIG,
                identity=(b"seed-public-key", b"certificate"),
                keys={}, keys_file=str(self.dir / "seed-keys.json"),
                now=seed.datetime(2026, 11, 22, 10, 30, tzinfo=seed.timezone.utc),
            )
        finally:
            connection.close()

    def test_the_seed_carries_the_account_verifier_byte_for_byte(self):
        body = self.redeem()
        self.assertEqual(body["sign_in_hash"], self.verifier)
        self.assertTrue(body["sign_in_hash"].startswith("scrypt$"))
        self.assertTrue(self.installer.verify_password(SIGN_IN, body["sign_in_hash"]))
        self.assertTrue(app.verify_password(SIGN_IN, body["sign_in_hash"]))
        self.assertFalse(app.verify_password(SIGN_IN + "x", body["sign_in_hash"]))
        forbidden = set(self.installer.SEED_FORBIDDEN)
        self.assertEqual(set(body) & forbidden, set())
        # The installer contract as it stands (ODL-925 will add the field to
        # SEED_KEYS): an unknown key is not a refusal, and no forbidden key
        # matches it — so a seed carrying only this addition still installs.
        self.installer.validate_seed(body, self.instance, self.slug)
        self.assertNotIn(SIGN_IN, json.dumps(body))
        # The stored row is the verifier, never the password.
        self.assertNotIn(SIGN_IN, self.verifier)

    def test_a_seed_without_an_account_omits_the_field(self):
        body = self.redeem(with_account=False)
        self.assertNotIn("sign_in_hash", body)
        self.assertEqual(set(body) & set(self.installer.SEED_FORBIDDEN), set())


# ---------------------------------------------------------------------- privacy


class PrivacyTests(unittest.TestCase):
    def test_every_account_column_is_named_in_the_privacy_inventory(self):
        text = (ROOT / "web" / "privacy.html").read_text(encoding="utf-8")
        connection = sqlite3.connect(":memory:")
        connection.executescript(app.SCHEMA)
        try:
            columns = {"accounts.%s" % row[1] for row in
                       connection.execute("PRAGMA table_info(accounts)")}
        finally:
            connection.close()
        self.assertTrue(columns)
        for column in sorted(columns):
            self.assertIn("<code>%s</code>" % column, text)


class PasswordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("odile_install_341b", INSTALLER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls.installer = module

    def test_the_account_verifier_is_the_installers_format(self):
        encoded = app.hash_password(SIGN_IN)
        self.assertTrue(encoded.startswith("scrypt$16384$8$1$"))
        self.assertTrue(self.installer.verify_password(SIGN_IN, encoded))
        self.assertTrue(app.verify_password(
            SIGN_IN, self.installer.hash_password(SIGN_IN)))
        self.assertNotIn(SIGN_IN, encoded)


if __name__ == "__main__":
    unittest.main()
