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
  second press leaves the same bytes and the same state — measured on the
  installed, kit-bearing outbox, where the recovery kit makes the message
  multipart and an unpinned boundary would move on every serialization;
* the failed state carries the retry copy and no machine id;
* no state carries JavaScript or a third-party request.

    /usr/bin/python3 -m unittest tests.test_account -v
"""

from __future__ import annotations

import email
import hashlib
import http.client
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
from email import policy as email_policy
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from odilectl import claim  # noqa: E402

SIGN_IN = "a long enough password"
TRIAL_START = "2026-11-20T09:00:00Z"
TRIAL_END = "2026-12-04T09:00:00Z"
BRAND_TRIAL = "4 December 2026"
D17 = "Your Odile password opens your machine too."
WAIT = "You can close this page. We email you when the machine is ready."
#: The value a provisioner writes after ODL-947: the machine's own boot-report
#: measurement, distinct from the release pin on purpose (the two differ).
MACHINE_MEASUREMENT = "c0" * 48
MEASUREMENT_RE = re.compile(r"\b[0-9a-f]{96}\b")


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

    def queued_confirm(self, account_id: int) -> Path:
        return self.outbox / "confirm" / ("%d.eml" % int(account_id))

    def seed_provisioned_ready(self, row: sqlite3.Row, *,
                               at: int = 1795379886,
                               measurement: str | None = None) -> tuple[str, str]:
        """Write the outbox the provisioner writes: kit, facts, then the `.eml`.

        This is the installed shape the verifier reproduced: the ready email
        carries the recovery kit as an attachment, and neither the mail nor the
        kit prints the setup token (ODL-925: the first visit is a tokenless
        login with the password chosen at registration; the token is the
        machine-side seed badge and the rebuild-seal key). It is also the shape
        that made every serialization pick a fresh random MIME boundary.

        `measurement` defaults to the machine's own value, which is what the
        provisioner writes after ODL-947; pass `app.pinned_measurement()` to
        seed the pre-card shape whose mail printed the release pin.

        Returns the token and the kit text, so a test can assert where each one
        went — and that neither prints it.
        """
        slug = row["slug"]
        host = "%s.test.odile.fyi" % slug
        url = "https://" + host
        token = "ab" * 32
        first_visit_url = url + "/first-visit"
        if measurement is None:
            measurement = MACHINE_MEASUREMENT
        kit_text = claim.render_recovery_kit(
            slug=slug,
            ipv4="203.0.113.42",
            instance_id=row["server_id"],
            token=token,
            measurement=measurement,
            first_visit_url=first_visit_url,
            expires_at="2026-11-21T09:00:00Z",
            release="2026.11.1",
            vcpus=2,
            backup_repo_url=None,
            written_at="2026-11-20T09:00:00Z",
        )
        claim.ReadyOutbox(self.outbox).queue(
            slug=slug,
            recipient=row["email"],
            subject=claim.READY_SUBJECT,
            body=claim.ready_body(url=url, measurement=measurement),
            kit_text=kit_text,
            facts={
                "kind": "ready",
                "slug": slug,
                "to": row["email"],
                "subject": claim.READY_SUBJECT,
                "host": host,
                "url": url,
                "server_id": row["server_id"],
                "ipv4": "203.0.113.42",
                "measurement": measurement,
                "guide_url": claim.READY_GUIDE_URL,
                "release": "2026.11.1",
                "instance_id": row["server_id"],
                "token_expires": "2026-11-21T09:00:00Z",
                "kit_filename": "odile-%s-recovery-kit.txt" % slug,
            },
            at=at,
        )
        return token, kit_text

    def confirm_token(self, raw: bytes) -> str:
        message = email.message_from_bytes(raw, policy=email_policy.default)
        part = message.get_body(("plain",))
        self.assertIsNotNone(part, "the confirmation mail has no plain part")
        assert part is not None
        body = part.get_content()
        match = re.search(r"/confirm\?t=([0-9a-f]{64})", body)
        self.assertIsNotNone(match, "the confirmation mail carries no token link")
        assert match is not None
        return match.group(1)


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

    def test_the_signed_in_404_is_a_branded_page_with_one_way_back(self):
        mine = self.email()
        other = self.email()
        self.seed_account(mine, "ready")
        self.seed_account(other, "ready")
        cookie = self.sign_in(mine)
        for path in ("/account/%s" % self.slug_for(other), "/account/"):
            with self.subTest(path=path):
                status, headers, body = request(
                    self.port, "GET", path, cookie=cookie)
                self.assertEqual(status, 404, body[:200])
                self.assertTrue(headers.get("Content-Type", "").startswith("text/html"),
                                headers.get("Content-Type"))
                text = body.decode("utf-8")
                self.assertIn("<h1>That page is not here.</h1>", text)
                self.assertIn('href="/account"', text)
                self.assertNotEqual(text, "not found\n")
                # The page is the person's; it names neither machine.
                self.assertNotIn(self.slug_for(other), text)
                self.assertNotIn(self.slug_for(mine), text)

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
        _email, row, cookie = self.state_account("building")
        _headers, text = self.page(cookie)
        self.assertIn("We have your address. The machine is being set up.", text)
        # ODL-947: the wait is named once — the machine's own report answers.
        self.assertIn("Its first boot prepares the encrypted disk, which is the "
                      "slow part. We wait for the machine's own report to answer.",
                      text)
        self.assertIn(WAIT, text)
        self.assertIn("Your machine is <strong>%s.test.odile.fyi</strong>. "
                      "It answers when it is ready." % row["slug"], text)
        self.assertNotIn("appears when ready", text)
        self.assertNotIn('action="/account/resend"', text)
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
        # One label line at 320/390/1440 (the sheet's button text row): the
        # 22 November product re-cut of PR/FAQ §4 (t_188e2dd9) shortened this
        # label, closing the 320 px Should the Designer recorded in round 3.
        self.assertIn("Send the email again", text)
        self.assertNotIn("Send the ready email again", text)

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

    def test_the_confirm_state_offers_one_press_to_send_the_email_again(self):
        _email, _row, cookie = self.state_account("confirm")
        _headers, text = self.page(cookie)
        self.assertIn('action="/account/resend"', text)
        # One label line: the sheet's button text row says "one line", and the
        # round-2 label needed 334 px in a 290-304 px control (ODL-583 round 3).
        self.assertIn("Send the email again", text)
        self.assertNotIn("Send the confirmation email again", text)
        self.assertNotIn("Send the ready email again", text)
        self.assertNotIn("Open your machine", text)

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
    def test_a_press_repairs_the_facts_release_after_a_recycle(self):
        """ODL-947: the ready mail's facts name the release the row now holds.

        The sidecar is written once at provisioning. A recycle or re-install can
        move `tenants.release` afterwards, and a press rebuilds the mail from the
        sidecar — so the sidecar must follow the row, while the message bytes
        (which do not print the release) stay identical.
        """
        address = self.email()
        row = self.seed_account(address, "ready", tenant_ipv4="203.0.113.42")
        cookie = self.sign_in(address)
        slug = row["slug"]
        self.seed_provisioned_ready(row)
        facts_path = self.outbox / "ready" / ("%s.json" % slug)
        message_path = self.queued_ready(slug)
        self.assertEqual(json.loads(facts_path.read_text(encoding="utf-8"))["release"],
                         "2026.11.1")
        # The first press is the tokenless re-cut; that mail is the baseline.
        status, _headers, _body = self.resend(
            cookie, origin="http://127.0.0.1:%d" % self.port)
        self.assertEqual(status, 303)
        before_message = hashlib.sha256(message_path.read_bytes()).hexdigest()

        connection = self.connect()
        try:
            connection.execute("UPDATE tenants SET release = ? WHERE slug = ?",
                               ("2026.11.23-5", slug))
            connection.commit()
        finally:
            connection.close()

        status, _headers, _body = self.resend(
            cookie, origin="http://127.0.0.1:%d" % self.port)
        self.assertEqual(status, 303)
        after_facts = json.loads(facts_path.read_text(encoding="utf-8"))
        self.assertEqual(after_facts["release"], "2026.11.23-5")
        self.assertEqual(before_message,
                         hashlib.sha256(message_path.read_bytes()).hexdigest(),
                         "the press changed the mail's bytes")

        # A second press is the same sidecar and the same mail.
        settled = facts_path.read_bytes()
        status, _headers, _body = self.resend(
            cookie, origin="http://127.0.0.1:%d" % self.port)
        self.assertEqual(status, 303)
        self.assertEqual(settled, facts_path.read_bytes())
        self.assertEqual(before_message,
                         hashlib.sha256(message_path.read_bytes()).hexdigest())

    def test_a_press_recuts_a_pre_card_sidecar_that_holds_the_release_pin(self):
        """ODL-947 review round 2: the one pre-card ready tenant's sidecar.

        `odl-12bceb.json` was written by the old press, so its measurement is
        the release pin (`app.pinned_measurement()`) while the machine's own
        `/attestation` prints a different value. `queue_ready` — the function
        `/account/resend` calls for a ready row — must re-cut the sidecar and
        the mail from the machine's own number; and once re-cut, a second press
        must leave the same bytes. The app runs as a subprocess in this suite,
        so the press is driven here directly, as the resend test above does.
        """
        row = self.seed_account(self.email(), "ready", tenant_ipv4="203.0.113.42")
        slug = row["slug"]
        pin = app.pinned_measurement()
        self.seed_provisioned_ready(row, measurement=pin)
        facts_path = self.outbox / "ready" / ("%s.json" % slug)
        message_path = self.queued_ready(slug)
        self.assertEqual(json.loads(facts_path.read_text(encoding="utf-8"))["measurement"],
                         pin, "the fixture is not the pre-card shape")
        seeded = message_path.read_bytes()
        seeded_part = email.message_from_bytes(
            seeded, policy=email_policy.default).get_body(("plain",))
        self.assertIsNotNone(seeded_part)
        assert seeded_part is not None
        self.assertEqual(MEASUREMENT_RE.findall(seeded_part.get_content()), [pin],
                         "the seeded pre-card mail does not print the release pin")

        original_outbox = app.OUTBOX
        app.OUTBOX = str(self.outbox)
        try:
            tenant = self.tenant(slug)
            # A machine that cannot be read yet: the press fails rather than
            # reprinting the release pin as if it were this machine's number.
            with mock.patch.object(claim, "machine_measurement", return_value=None):
                with self.assertRaises(RuntimeError):
                    app.queue_ready(row, tenant)
            self.assertEqual(seeded, message_path.read_bytes(),
                             "the press rewrote the pre-card mail without the machine")
            self.assertEqual(
                json.loads(facts_path.read_text(encoding="utf-8"))["measurement"], pin)

            # The machine answers at its own name: the press writes its value.
            with mock.patch.object(claim, "machine_measurement",
                                   return_value=MACHINE_MEASUREMENT):
                app.queue_ready(row, tenant)
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
            self.assertEqual(facts["measurement"], MACHINE_MEASUREMENT,
                             "the press kept the release pin in the sidecar")
            message = email.message_from_bytes(message_path.read_bytes(),
                                              policy=email_policy.default)
            body = message.get_body(("plain",))
            self.assertIsNotNone(body)
            assert body is not None
            text = body.get_content()
            self.assertEqual(MEASUREMENT_RE.findall(text), [MACHINE_MEASUREMENT],
                             "the ready mail does not print exactly the machine's number")
            self.assertNotIn(pin, text, "the ready mail still prints the release pin")
            settled_mail = message_path.read_bytes()
            settled_facts = facts_path.read_bytes()

            # Re-cut once, then settled: a second press is the same bytes.
            with mock.patch.object(claim, "machine_measurement",
                                   return_value=MACHINE_MEASUREMENT):
                app.queue_ready(row, tenant)
            self.assertEqual(settled_mail, message_path.read_bytes(),
                             "a second press changed the re-cut mail's bytes")
            self.assertEqual(settled_facts, facts_path.read_bytes(),
                             "a second press changed the re-cut facts")
        finally:
            app.OUTBOX = original_outbox

    def test_the_resend_queues_a_tokenless_ready_email_and_repeats_byte_for_byte(self):
        address = self.email()
        row = self.seed_account(address, "ready", tenant_ipv4="203.0.113.42")
        cookie = self.sign_in(address)
        slug = row["slug"]
        before_account = self.connect().execute(
            "SELECT slug, ipv4, provision_state, trial_ends_at FROM accounts WHERE id = ?",
            (row["id"],)).fetchone()
        before_tenant = self.tenant(slug)
        path = self.queued_ready(slug)

        # The installed shape: the provisioner's kit, facts and .eml are on
        # disk before the first press, so the message is multipart from the
        # start. An empty outbox would hide the boundary bug (round 1).
        token, kit_text = self.seed_provisioned_ready(row)
        seeded = path.read_bytes()
        seeded_part = email.message_from_bytes(
            seeded, policy=email_policy.default).get_body(("plain",))
        self.assertIsNotNone(seeded_part, "the provisioner's mail has no plain part")
        assert seeded_part is not None
        # ODL-925: the provisioner's own ready mail is tokenless too.
        seeded_body = seeded_part.get_content()
        self.assertIn("/first-visit", seeded_body)
        self.assertNotIn("first-visit?t=", seeded_body)
        self.assertIsNone(re.search(r"\b[0-9a-f]{64}\b", seeded_body),
                          "the provisioner's own mail carries a setup token")

        status, headers, _body = self.resend(
            cookie, origin="http://127.0.0.1:%d" % self.port)
        self.assertEqual(status, 303, _body[:200])
        self.assertEqual(headers.get("Location"), "/account")
        first = path.read_bytes()
        self.assertNotEqual(first, seeded,
                            "the first press is the tokenless re-cut, not the "
                            "provisioner's own .eml")

        message = email.message_from_bytes(first, policy=email_policy.default)
        self.assertEqual(message["Subject"], claim.READY_SUBJECT)
        part = message.get_body(("plain",))
        self.assertIsNotNone(part, "the ready mail has no plain part")
        assert part is not None
        body = part.get_content()
        self.assertIn("https://%s.test.odile.fyi" % slug, body)
        self.assertIn("https://%s.test.odile.fyi/verify" % slug, body)
        self.assertNotIn("first-visit?t=", body,
                         "the ready email's own link carries a setup token")
        self.assertIsNone(re.search(r"\b[0-9a-f]{64}\b", body),
                          "the resend minted a setup token in the body")

        # The kit rides along byte-for-byte and names the tokenless page; the
        # setup token is the seed badge and the rebuild-seal key (ODL-925).
        attachments = list(message.iter_attachments())
        self.assertEqual(len(attachments), 1, "the kit is not attached")
        attachment = attachments[0]
        self.assertEqual(attachment.get_filename(),
                         "odile-%s-recovery-kit.txt" % slug)
        self.assertEqual(attachment.get_payload(decode=True),
                         kit_text.encode("utf-8"))
        self.assertIn("https://%s.test.odile.fyi/first-visit" % slug, kit_text)
        self.assertNotIn(token, kit_text,
                         "the kit prints the setup token")

        # Two independent serializations of the same facts agree. The old
        # assertion here hashed the same file read twice, so it could not fail.
        original_outbox = app.OUTBOX
        app.OUTBOX = str(self.outbox)
        try:
            rebuilt = Path(app.queue_ready(row, self.tenant(slug))).read_bytes()
        finally:
            app.OUTBOX = original_outbox
        self.assertEqual(hashlib.sha256(first).hexdigest(),
                         hashlib.sha256(rebuilt).hexdigest(),
                         "a rebuild from the facts sidecar is a different mail")

        # Three presses, one sha256, with the kit attached.
        digests = [hashlib.sha256(first).hexdigest()]
        for _press in (2, 3):
            status, headers, _body = self.resend(
                cookie, origin="http://127.0.0.1:%d" % self.port)
            self.assertEqual(status, 303)
            self.assertEqual(headers.get("Location"), "/account")
            digests.append(hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(len(set(digests)), 1,
                         "the presses wrote different bytes: %s" % digests)

        # A press after the mailer removed the .eml restores the same bytes.
        path.unlink()
        status, _headers, _body = self.resend(
            cookie, origin="http://127.0.0.1:%d" % self.port)
        self.assertEqual(status, 303)
        self.assertEqual(digests[0],
                         hashlib.sha256(path.read_bytes()).hexdigest(),
                         "the restored mail is not the queued mail")

        _headers, text = self.page(cookie)
        self.assertIn("Your machine is ready.", text)
        self.assertIn(D17, text)

        after_account = self.connect().execute(
            "SELECT slug, ipv4, provision_state, trial_ends_at FROM accounts WHERE id = ?",
            (row["id"],)).fetchone()
        self.assertEqual(dict(before_account), dict(after_account))
        self.assertEqual(dict(before_tenant), dict(self.tenant(slug)))

    def test_the_resend_does_nothing_in_building_or_failed(self):
        for state in ("building", "failed"):
            with self.subTest(state=state):
                email = self.email()
                row = self.seed_account(
                    email, state,
                    reason="create: HTTP 404: Plan not found." if state == "failed" else "")
                cookie = self.sign_in(email)
                status, headers, _body = self.resend(
                    cookie, origin="http://127.0.0.1:%d" % self.port)
                self.assertEqual(status, 303)
                self.assertEqual(headers.get("Location"), "/account")
                self.assertFalse(self.queued_ready(row["slug"]).exists())
                self.assertFalse(self.queued_confirm(row["id"]).exists())

    def test_the_confirm_resend_sends_a_fresh_token_and_spends_the_old_link(self):
        email = self.email()
        row = self.seed_account(email, "confirm", verified=False)
        cookie = self.sign_in(email)
        status, headers, _body = self.resend(
            cookie, origin="http://127.0.0.1:%d" % self.port)
        self.assertEqual(status, 303, _body[:200])
        self.assertEqual(headers.get("Location"), "/account")
        path = self.queued_confirm(row["id"])
        self.assertTrue(path.is_file())
        first = self.confirm_token(path.read_bytes())
        with self.connect() as connection:
            fresh = connection.execute(
                "SELECT email_verified_at, confirm_token_hash, confirm_expires_at"
                " FROM accounts WHERE id = ?", (row["id"],)).fetchone()
        self.assertIsNone(fresh["email_verified_at"], "the press verified the address")
        self.assertEqual(fresh["confirm_token_hash"], app.hash_token(first))
        self.assertGreater(fresh["confirm_expires_at"], "2026-11-21T09:00:00Z")
        # The mailed link is a live confirmation page, not a dead one.
        status, _headers, body = request(self.port, "GET", "/confirm?t=" + first)
        self.assertEqual(status, 200, body[:200])
        self.assertIn('name="token"', body.decode("utf-8"))
        # A second press mints a new token; the first link stops working.
        status, _headers, _body = self.resend(
            cookie, origin="http://127.0.0.1:%d" % self.port)
        self.assertEqual(status, 303)
        second = self.confirm_token(path.read_bytes())
        self.assertNotEqual(first, second)
        status, _headers, _body = request(self.port, "GET", "/confirm?t=" + first)
        self.assertEqual(status, 404, "the spent confirmation link still works")
        status, _headers, _body = request(self.port, "GET", "/confirm?t=" + second)
        self.assertEqual(status, 200)

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


class BrandSheetTests(AccountBase):
    """The pairs BRAND.md measures in every brand review, on the served page."""

    def test_the_served_page_carries_the_sheet_selection_pair_and_control_states(self):
        email = self.email()
        self.seed_account(email, "building")
        _headers, text = self.page(self.sign_in(email))
        self.assertIn(":root{", text)
        # The pair comes after :root, as every other surface in web/ has it.
        self.assertLess(text.index(":root{"),
                        text.index("::selection{background:#1c1813;color:#eadfc4}"))
        # The ink-filled primary needs the inverted pair: the page pair paints
        # nothing on it (the label's own background is --ink).
        self.assertIn(".primary::selection{background:var(--paper);color:var(--ink)}",
                      text)
        self.assertIn(".primary:hover{background:var(--ink-hover);"
                      "border-color:var(--ink-hover)}", text)
        self.assertIn("button.secondary:hover{background:rgba(28,24,19,.05);"
                      "border-color:var(--ink)}", text)
        # The trial line is its own aside, not bound to the password line, and
        # it takes the sheet's 17 px line-height (1.6, not 1.5).
        self.assertIn(".trial{margin:24px 0 16px;font-size:17px;line-height:1.6;",
                      text)


if __name__ == "__main__":
    unittest.main()
