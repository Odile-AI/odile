#!/usr/bin/python3
"""The account page (ODL-583): four states, two actions, one column.

Medium tests: the real `app.py` on a loopback socket, a throwaway database and
outbox, and the rows the page reads. No machine is ordered and no mail is sent;
the live renders are docs/evidence/a2-account-page.md.

What is pinned here, in the card's own words:

* a signed-out request to /account redirects to /login;
* a signed-in request for another account's machine is 404;
* the four states render from the account's and the tenant's rows, with the
  trial line under each state in the brand's date form;
* every state carries "Your Odile password opens your machine too.";
* the ready state carries the machine's address, the report link and
  "Open your machine", plus the one resend action;
* the resend queues a tokenless ready email, keeps the machine's URL, and a
  second press leaves the same bytes and the same state;
* the failed state carries the retry copy and no machine id;
* no state carries JavaScript or a third-party request.

    /usr/bin/python3 -m unittest tests.test_account -v
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

SIGN_IN = "a long enough password"
TRIAL_START = "2026-11-20T09:00:00Z"
TRIAL_END = "2026-12-04T09:00:00Z"
BRAND_TRIAL = "4 December 2026"
D17 = "Your Odile password opens your machine too."
WAIT = "You can close this page. We email you when the machine is ready."


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def request(port: int, method: str, path: str, body: str | None = None,
            cookie: str | None = None, origin: str | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Connection": "close"}
    if cookie:
        headers["Cookie"] = cookie
    if origin:
        headers["Origin"] = origin
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    data = response.read()
    result = (response.status, dict(response.getheaders()), data)
    connection.close()
    return result


def form(**fields) -> str:
    return urllib.parse.urlencode(fields)


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


class AccountBase(unittest.TestCase):
    """One real service per class, a throwaway database, outbox and key."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="odile-account-")
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

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def email(self) -> str:
        type(self)._counter += 1
        return "account%d@example.com" % type(self)._counter

    @staticmethod
    def slug_for(email: str) -> str:
        return "odl-" + hashlib.sha256(email.encode()).hexdigest()[:6]

    def seed_account(self, email: str, state: str, *, verified: bool = True,
                     ipv4: str = "203.0.113.41", tenant_ipv4: str | None = None,
                     server_id: str | None = None, reason: str = "",
                     trial_end: str = TRIAL_END) -> sqlite3.Row:
        """One account and its tenant row, in exactly the state the page reads."""
        slug = self.slug_for(email)
        machine = server_id or hashlib.sha256(
            ("server:" + email).encode()).hexdigest()[:8]
        # `confirm` is the page's name for an unverified account; the schema's
        # state for it is `unverified`.
        db_state = "unverified" if state == "confirm" else state
        stamp = TRIAL_START
        connection = self.connect()
        try:
            connection.execute(
                "INSERT INTO tenants (slug, email, ssh_public_key, state, plan,"
                " ipv4, server_id, release, created_at)"
                " VALUES (?, ?, ?, 'invited', 'alpha', ?, ?, '2026.11.1', ?)",
                (slug, email, app.throwaway_boot_key(), tenant_ipv4, machine, stamp),
            )
            connection.execute(
                "INSERT INTO accounts (slug, email, password_hash,"
                " confirm_token_hash, confirm_expires_at, email_verified_at,"
                " trial_started_at, trial_ends_at, provision_state,"
                " provision_reason, server_id, ipv4, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (slug, email, app.hash_password(SIGN_IN),
                 hashlib.sha256(("confirm:" + email).encode()).hexdigest(),
                 "2026-11-21T09:00:00Z",
                 stamp if verified else None, stamp, trial_end, db_state,
                 reason or None, machine if state == "ready" else None,
                 ipv4 if state == "ready" else None, stamp),
            )
            connection.commit()
            return connection.execute(
                "SELECT * FROM accounts WHERE email = ?", (email,)).fetchone()
        finally:
            connection.close()

    def tenant(self, slug: str) -> sqlite3.Row:
        connection = self.connect()
        try:
            return connection.execute(
                "SELECT * FROM tenants WHERE slug = ?", (slug,)).fetchone()
        finally:
            connection.close()

    def sign_in(self, email: str) -> str:
        status, headers, body = request(
            self.port, "POST", "/signin",
            form(email=email, password=SIGN_IN))
        self.assertEqual(status, 303, body[:300])
        return headers["Set-Cookie"].split(";")[0]

    def page(self, cookie: str, path: str = "/account"):
        status, headers, body = request(self.port, "GET", path, cookie=cookie)
        self.assertEqual(status, 200, body[:300])
        return headers, body.decode("utf-8")

    def resend(self, cookie: str, *, origin: str | None = None):
        return request(self.port, "POST", "/account/resend", body="",
                       cookie=cookie, origin=origin)

    def queued_ready(self, slug: str) -> Path:
        return self.outbox / "ready" / ("%s.eml" % slug)


class RedirectAndOwnershipTests(AccountBase):
    def test_a_signed_out_request_to_account_redirects_to_login(self):
        status, headers, body = request(self.port, "GET", "/account")
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/login")
        status, _headers, body = request(self.port, "GET", "/login")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn('action="/login"', text)
        self.assertIn("Sign in", text)

    def test_a_signed_in_request_for_another_accounts_machine_is_404(self):
        mine = self.email()
        other = self.email()
        self.seed_account(mine, "ready")
        self.seed_account(other, "ready")
        cookie = self.sign_in(mine)
        status, _headers, body = request(
            self.port, "GET", "/account/%s" % self.slug_for(other), cookie=cookie)
        self.assertEqual(status, 404, body[:200])
        status, _headers, body = request(
            self.port, "GET", "/account/%s" % self.slug_for(mine), cookie=cookie)
        self.assertEqual(status, 200, body[:200])
        text = body.decode("utf-8")
        self.assertIn("%s.test.odile.fyi" % self.slug_for(mine), text)
        self.assertNotIn("%s.test.odile.fyi" % self.slug_for(other), text)

    def test_the_login_post_signs_in_and_signin_stays_a_working_seam(self):
        email = self.email()
        self.seed_account(email, "building")
        status, headers, _body = request(
            self.port, "POST", "/login", form(email=email, password=SIGN_IN))
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"].split(";")[0]
        self.page(cookie)
        status, _headers, body = request(self.port, "GET", "/signin")
        self.assertEqual(status, 200)
        self.assertIn('action="/signin"', body.decode("utf-8"))


class FourStateTests(AccountBase):
    STATE_HEADINGS = {
        "confirm": "Check your email.",
        "building": "Building.",
        "ready": "Your machine is ready.",
        "failed": "The machine could not be built.",
    }

    def state_account(self, state: str):
        email = self.email()
        row = self.seed_account(
            email, state,
            verified=(state != "confirm"),
            tenant_ipv4="203.0.113.42" if state == "ready" else None,
            reason="create: HTTP 404: Plan not found." if state == "failed" else "")
        return email, row, self.sign_in(email)

    def test_each_of_the_four_states_renders_its_copy_and_the_two_lines(self):
        for state in ("confirm", "building", "ready", "failed"):
            with self.subTest(state=state):
                _email, row, cookie = self.state_account(state)
                _headers, text = self.page(cookie)
                self.assertIn("<h1>%s</h1>" % self.STATE_HEADINGS[state], text)
                self.assertIn("Your trial ends on %s." % BRAND_TRIAL, text)
                self.assertIn(D17, text)
                self.assertNotIn("<script", text.lower())
                self.assertIsNone(
                    re.search(r'<(?:link|img)\b[^>]*\b(?:href|src)="https?://', text),
                    "a third-party asset request")
                self.assertEqual(text.count("<main"), 1)
                self.assertIn("main{max-width:720px", text)
                self.assertNotIn("grid-template-columns", text)
                self.assertNotIn("countdown", text.lower())

    def test_the_building_state_carries_the_wait_copy_and_no_action(self):
        _email, _row, cookie = self.state_account("building")
        _headers, text = self.page(cookie)
        self.assertIn("We have your address. The machine is being set up.", text)
        self.assertIn("Its first boot prepares the encrypted disk, which is the slow part.", text)
        self.assertIn(WAIT, text)
        self.assertIn("appears when ready", text)
        self.assertNotIn("Send the ready email again", text)
        self.assertNotIn("Open your machine", text)

    def test_the_ready_state_carries_the_address_report_and_open_action(self):
        email, row, cookie = self.state_account("ready")
        _headers, text = self.page(cookie)
        host = "%s.test.odile.fyi" % row["slug"]
        self.assertIn(host, text)
        self.assertIn("https://%s/verify" % host, text)
        self.assertIn("https://%s/login" % host, text)
        self.assertIn("Open your machine", text)
        # The tenant row's IPv4 is the machine's address when the two disagree.
        self.assertIn("203.0.113.42", text)
        self.assertNotIn("203.0.113.41", text)
        self.assertEqual(text.count('action="/account/resend"'), 1)

    def test_the_failed_state_shows_the_retry_copy_and_no_machine_id(self):
        email, row, cookie = self.state_account("failed")
        _headers, text = self.page(cookie)
        self.assertIn("The build failed. We are retrying. You do not need to do anything.", text)
        self.assertIn("help@odile.fyi", text)
        self.assertNotIn("Your machine is", text)
        self.assertNotIn(row["slug"], text)
        self.assertNotIn(self.tenant(row["slug"])["server_id"], text)
        self.assertNotIn("203.0.113.", text)
        self.assertNotIn("Open your machine", text)
        self.assertNotIn("Send the ready email again", text)

    def test_the_trial_line_matches_each_accounts_own_row(self):
        first = self.email()
        second = self.email()
        self.seed_account(first, "building", trial_end="2026-12-04T09:00:00Z")
        self.seed_account(second, "building", trial_end="2026-12-19T09:00:00Z")
        _headers, text = self.page(self.sign_in(first))
        self.assertIn("Your trial ends on 4 December 2026.", text)
        self.assertNotIn("19 December 2026", text)
        _headers, text = self.page(self.sign_in(second))
        self.assertIn("Your trial ends on 19 December 2026.", text)


class ResendTests(AccountBase):
    def test_the_resend_queues_a_tokenless_ready_email_and_repeats_byte_for_byte(self):
        email = self.email()
        row = self.seed_account(email, "ready", tenant_ipv4="203.0.113.42")
        cookie = self.sign_in(email)
        slug = row["slug"]
        before_account = self.connect().execute(
            "SELECT slug, ipv4, provision_state, trial_ends_at FROM accounts WHERE id = ?",
            (row["id"],)).fetchone()
        before_tenant = self.tenant(slug)

        status, headers, _body = self.resend(
            cookie, origin="http://127.0.0.1:%d" % self.port)
        self.assertEqual(status, 303, _body[:200])
        self.assertEqual(headers.get("Location"), "/account")
        path = self.queued_ready(slug)
        self.assertTrue(path.is_file())
        first = path.read_bytes()
        self.assertEqual(hashlib.sha256(first).hexdigest(),
                         hashlib.sha256(path.read_bytes()).hexdigest())

        message = first.decode("utf-8", "replace")
        self.assertIn("Subject: Your machine is waiting.", message)
        self.assertIn("https://%s.test.odile.fyi" % slug, message)
        self.assertIn("https://%s.test.odile.fyi/verify" % slug, message)
        self.assertNotIn("first-visit?t=", message)
        self.assertIsNone(re.search(r"\b[0-9a-f]{64}\b", message),
                          "the resend minted a setup token")

        status, headers, _body = self.resend(
            cookie, origin="http://127.0.0.1:%d" % self.port)
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/account")
        self.assertEqual(first, path.read_bytes(), "the second press changed the bytes")
        _headers, text = self.page(cookie)
        self.assertIn("Your machine is ready.", text)
        self.assertIn(D17, text)

        after_account = self.connect().execute(
            "SELECT slug, ipv4, provision_state, trial_ends_at FROM accounts WHERE id = ?",
            (row["id"],)).fetchone()
        self.assertEqual(dict(before_account), dict(after_account))
        self.assertEqual(dict(before_tenant), dict(self.tenant(slug)))

    def test_the_resend_does_nothing_before_the_machine_is_ready(self):
        for state in ("confirm", "building", "failed"):
            with self.subTest(state=state):
                email = self.email()
                row = self.seed_account(
                    email, state, verified=(state != "confirm"),
                    reason="create: HTTP 404: Plan not found." if state == "failed" else "")
                cookie = self.sign_in(email)
                status, headers, _body = self.resend(
                    cookie, origin="http://127.0.0.1:%d" % self.port)
                self.assertEqual(status, 303)
                self.assertEqual(headers.get("Location"), "/account")
                self.assertFalse(self.queued_ready(row["slug"]).exists())

    def test_a_cross_site_resend_is_refused(self):
        email = self.email()
        self.seed_account(email, "ready")
        cookie = self.sign_in(email)
        status, _headers, body = self.resend(cookie, origin="https://evil.example")
        self.assertEqual(status, 403, body[:200])

    def test_a_signed_out_resend_redirects_to_login(self):
        status, headers, _body = request(self.port, "POST", "/account/resend",
                                         body="")
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/login")


if __name__ == "__main__":
    unittest.main()
