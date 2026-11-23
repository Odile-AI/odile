#!/usr/bin/python3
"""The console's first action: re-point an account's address (ODL-582).

Medium tests: the real `app.py` on a loopback socket, a throwaway database,
outbox, session key and admin-email file. No machine is ordered, nothing is
delivered and no live store is touched; the transcripts and renders in
docs/evidence/a6-repoint.md are the run record.

What is pinned here, in the card's own words:

* only the admin account reaches the page and the action; a signed-in
  non-admin gets 403 and the database does not move;
* the action rewrites the account's address, leaves `email_verified_at` null
  until the new address confirms, writes the actor, the previous address and
  the time to the admin log, and leaves `accounts.password_hash` and
  `accounts.recovery_hash` untouched;
* the account page says "confirm your new address" until the confirmation is
  spent, and the confirmation does not queue a second provision;
* `/forgot` still works for the account under the old address, because it
  needs the email as the name and the code as the credential;
* a notice is queued to the old address and the confirmation to the new one;
  losing the old-address message (a bounce) does not block the change;
* no `accounts` column becomes a value the guest's own gate accepts.

    /usr/bin/python3 -m unittest tests.test_admin -v
"""

from __future__ import annotations

import base64
import email
import hashlib
import html
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

SIGN_IN = "a long enough password"
NEW_SIGN_IN = "a different long password"
ADMIN = "admin@example.com"
TRIAL_START = "2026-11-20T09:00:00Z"
TRIAL_END = "2026-12-04T09:00:00Z"
RECOVERY_CODE = "M3XK9PVT2QWR7HCL5BSD"

CONFIRM_HEAD = "Confirm your new address."
CONFIRM_LINE = "Your account stays as it is until you open it"
CHANGED = "The address is changed."
FORBIDDEN_HEAD = "Not here."


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port: int, method: str, path: str, body: str | None = None,
            *, cookie: str | None = None, origin: str | None = None,
            headers: dict | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    sent = {"Content-Type": "application/x-www-form-urlencoded",
            "Connection": "close"}
    if cookie:
        sent["Cookie"] = cookie
    if origin:
        sent["Origin"] = origin
    for name, value in (headers or {}).items():
        sent[name] = value
    payload = body.encode("utf-8") if body is not None else None
    connection.request(method, path, body=payload, headers=sent)
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


class AdminBase(unittest.TestCase):
    """One real service per class, a throwaway database, outbox and admin file."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="odile-admin-")
        cls.dir = Path(cls.tmp.name)
        cls.db = cls.dir / "waitlist.db"
        cls.outbox = cls.dir / "outbox"
        cls.admin_file = cls.dir / "admin-email"
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
            # The three settings this card adds: the admin is a configured
            # address, never a column, and the log is the schema's own table.
            ODILE_ADMIN_EMAIL=ADMIN,
        )
        cls.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "app.py")], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        wait_health(cls.port, cls.proc)
        cls._counter = 0
        cls.seed_admin_static()
        cls.admin_cookie = cls.sign_in_static(ADMIN)

    @classmethod
    def seed_admin_static(cls) -> None:
        """The admin's own row, seeded before the class's first sign-in."""
        slug = cls.slug_for(ADMIN)
        connection = sqlite3.connect(cls.db, timeout=10)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "INSERT OR IGNORE INTO tenants (slug, email, ssh_public_key,"
                " state, plan, ipv4, server_id, release, created_at)"
                " VALUES (?, ?, ?, 'invited', 'alpha', '203.0.113.41', ?,"
                " '2026.11.1', ?)",
                (slug, ADMIN, app.throwaway_boot_key(),
                 hashlib.sha256(("server:" + ADMIN).encode()).hexdigest()[:12],
                 TRIAL_START),
            )
            connection.execute(
                "INSERT OR IGNORE INTO accounts (slug, email, password_hash,"
                " recovery_hash, confirm_token_hash, confirm_expires_at,"
                " email_verified_at, trial_started_at, trial_ends_at,"
                " provision_state, server_id, ipv4, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, '203.0.113.41', ?)",
                (slug, ADMIN, app.hash_password(SIGN_IN),
                 app.hash_password(RECOVERY_CODE),
                 hashlib.sha256(("confirm:" + ADMIN).encode()).hexdigest(),
                 "2026-11-21T09:00:00Z", TRIAL_START, TRIAL_START, TRIAL_END,
                 hashlib.sha256(("server:" + ADMIN).encode()).hexdigest()[:12],
                 TRIAL_START),
            )
            connection.commit()
        finally:
            connection.close()

    @classmethod
    def sign_in_static(cls, email_address: str) -> str:
        status, headers, body = request(
            cls.port, "POST", "/signin",
            form(email=email_address, password=SIGN_IN))
        assert status == 303, (status, body[:300])
        return headers["Set-Cookie"].split(";")[0]

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
        return "admin-target%d@example.com" % type(self)._counter

    @staticmethod
    def slug_for(email_address: str) -> str:
        return "odl-" + hashlib.sha256(email_address.encode()).hexdigest()[:6]

    def seed_account(self, email_address: str, *, verified: bool = True,
                     recovery: str | None = RECOVERY_CODE,
                     password: str = SIGN_IN, machine: bool = True,
                     provision_state: str = "ready") -> sqlite3.Row:
        """One account row and its tenant row, in the state the console reads."""
        slug = self.slug_for(email_address)
        recovery_hash = app.hash_password(recovery) if recovery else None
        server_id = hashlib.sha256(("server:" + email_address).encode()).hexdigest()[:12]
        connection = self.connect()
        try:
            connection.execute(
                "INSERT OR IGNORE INTO tenants (slug, email, ssh_public_key, state,"
                " plan, ipv4, server_id, release, created_at)"
                " VALUES (?, ?, ?, 'invited', 'alpha', ?, ?, '2026.11.1', ?)",
                (slug, email_address, app.throwaway_boot_key(),
                 "203.0.113.41" if machine else None,
                 server_id if machine else None, TRIAL_START),
            )
            connection.execute(
                "INSERT OR IGNORE INTO accounts (slug, email, password_hash,"
                " recovery_hash, confirm_token_hash, confirm_expires_at,"
                " email_verified_at, trial_started_at, trial_ends_at,"
                " provision_state, provision_queued_at, server_id, ipv4, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (slug, email_address, app.hash_password(password), recovery_hash,
                 hashlib.sha256(("confirm:" + email_address).encode()).hexdigest(),
                 "2026-11-21T09:00:00Z", TRIAL_START if verified else None,
                 TRIAL_START, TRIAL_END, provision_state,
                 TRIAL_START if provision_state == "queued" else None,
                 server_id if machine else None,
                 "203.0.113.41" if machine else None, TRIAL_START),
            )
            connection.commit()
            return connection.execute(
                "SELECT * FROM accounts WHERE email = ?", (email_address,)).fetchone()
        finally:
            connection.close()

    def account(self, email_address: str) -> sqlite3.Row | None:
        connection = self.connect()
        try:
            return connection.execute(
                "SELECT * FROM accounts WHERE email = ?", (email_address,)).fetchone()
        finally:
            connection.close()

    def account_by_id(self, account_id: int) -> sqlite3.Row:
        connection = self.connect()
        try:
            return connection.execute(
                "SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
        finally:
            connection.close()

    def tenant(self, slug: str) -> sqlite3.Row:
        connection = self.connect()
        try:
            return connection.execute(
                "SELECT * FROM tenants WHERE slug = ?", (slug,)).fetchone()
        finally:
            connection.close()

    def log_rows(self, account_id: int | None = None) -> list[sqlite3.Row]:
        connection = self.connect()
        try:
            if account_id is None:
                return connection.execute(
                    "SELECT * FROM admin_log ORDER BY id").fetchall()
            return connection.execute(
                "SELECT * FROM admin_log WHERE account_id = ? ORDER BY id",
                (int(account_id),)).fetchall()
        finally:
            connection.close()

    def sign_in(self, email_address: str, password: str = SIGN_IN) -> str:
        status, headers, body = request(
            self.port, "POST", "/signin",
            form(email=email_address, password=password))
        self.assertEqual(status, 303, body[:300])
        return headers["Set-Cookie"].split(";")[0]

    def repoint(self, current: str, new: str, *, cookie: str | None = None,
                origin: str | None = None):
        return request(
            self.port, "POST", "/admin/repoint",
            form(current_email=current, new_email=new),
            cookie=self.admin_cookie if cookie is None else cookie,
            origin=origin)

    def queued_confirm(self, account_id: int) -> Path:
        return self.outbox / "confirm" / ("%d.eml" % int(account_id))

    def queued_notices(self, account_id: int) -> list[Path]:
        return sorted((self.outbox / "repoint").glob("%d-*.eml" % int(account_id)))

    @staticmethod
    def message(path: Path) -> email.message.Message:
        with open(path, "rb") as handle:
            return email.message_from_bytes(handle.read(), policy=email_policy.default)

    def confirm_token(self, path: Path) -> str:
        message = self.message(path)
        part = message.get_body(("plain",))
        self.assertIsNotNone(part, "the confirmation mail has no plain part")
        assert part is not None
        match = re.search(r"/confirm\?t=([0-9a-f]{64})", part.get_content())
        self.assertIsNotNone(match, "the confirmation mail carries no token link")
        assert match is not None
        return match.group(1)


# ---------------------------------------------------------------------------
# the admin gate


class AdminGateTests(AdminBase):
    def test_a_signed_out_request_goes_to_login(self):
        status, headers, _body = request(self.port, "GET", "/admin")
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/login")

    def test_a_non_admin_gets_403_on_the_page_and_the_action(self):
        target = self.email()
        before = self.seed_account(target)
        cookie = self.sign_in(target)
        status, _headers, body = request(self.port, "GET", "/admin", cookie=cookie)
        self.assertEqual(status, 403, body[:300])
        text = body.decode("utf-8")
        self.assertIn("<h1>%s</h1>" % FORBIDDEN_HEAD, text)
        self.assertNotIn('action="/admin/repoint"', text)
        new = "new-%s" % target
        status, _headers, body = self.repoint(target, new, cookie=cookie)
        self.assertEqual(status, 403, body[:300])
        self.assertIn(FORBIDDEN_HEAD, body.decode("utf-8"))
        after = self.account(target)
        self.assertEqual(after["email"], target)
        self.assertEqual(before["password_hash"], after["password_hash"])
        self.assertEqual(self.log_rows(int(before["id"])), [])

    def test_a_cross_site_post_is_refused_even_for_the_admin(self):
        target = self.email()
        self.seed_account(target)
        status, _headers, body = self.repoint(
            target, "new-%s" % target,
            origin="https://evil.example")
        self.assertEqual(status, 403, body[:300])
        self.assertEqual(self.account(target)["email"], target)

    def test_the_admin_page_lists_the_account_and_the_action(self):
        target = self.email()
        self.seed_account(target)
        status, _headers, body = request(
            self.port, "GET", "/admin", cookie=self.admin_cookie)
        self.assertEqual(status, 200, body[:300])
        text = body.decode("utf-8")
        self.assertIn("<h1>The console.</h1>", text)
        self.assertIn(target, text)
        self.assertIn('action="/admin/repoint"', text)
        self.assertIn("Re-point an account's address", text)
        self.assertNotIn("<script", text.lower())
        self.assertIsNone(re.search(
            r'<(?:link|img)\b[^>]*\b(?:href|src)="https?://', text))

    def test_the_admin_file_is_the_fallback_when_the_env_is_absent(self):
        # The service reads the env first; the file is the host's path when no
        # service unit change is wanted. Unit-level here: the same normalisation
        # the running service used for its env value.
        self.admin_file.write_text(ADMIN + "\n", encoding="utf-8")
        previous = os.environ.pop("ODILE_ADMIN_EMAIL", None)
        previous_file = os.environ.get("ODILE_ADMIN_EMAIL_FILE")
        os.environ["ODILE_ADMIN_EMAIL_FILE"] = str(self.admin_file)
        try:
            self.assertEqual(app.admin_email(), ADMIN)
        finally:
            if previous is None:
                os.environ.pop("ODILE_ADMIN_EMAIL", None)
            else:
                os.environ["ODILE_ADMIN_EMAIL"] = previous
            if previous_file is None:
                os.environ.pop("ODILE_ADMIN_EMAIL_FILE", None)
            else:
                os.environ["ODILE_ADMIN_EMAIL_FILE"] = previous_file


# ---------------------------------------------------------------------------
# the action and its row


class RepointTests(AdminBase):
    def test_the_action_rewrites_the_address_and_leaves_the_two_hashes(self):
        target = self.email()
        before = self.seed_account(target)
        tenant_before = self.tenant(before["slug"])
        new = "new-%s" % target
        status, _headers, body = self.repoint(target, new)
        self.assertEqual(status, 200, body[:400])
        text = body.decode("utf-8")
        self.assertIn(CHANGED, text)
        self.assertIn(new, text)
        self.assertIn(target, text)

        after = self.account(new)
        self.assertIsNotNone(after, "the account was not rewritten")
        self.assertIsNone(after["email_verified_at"], "the address was verified early")
        self.assertEqual(after["password_hash"], before["password_hash"])
        self.assertEqual(after["recovery_hash"], before["recovery_hash"])
        self.assertEqual(after["session_epoch"], before["session_epoch"])
        self.assertEqual(after["provision_state"], before["provision_state"])
        self.assertEqual(after["trial_started_at"], before["trial_started_at"])
        self.assertEqual(after["trial_ends_at"], before["trial_ends_at"])
        self.assertIsNone(self.account(target), "the old address still names the account")

        tenant_after = self.tenant(before["slug"])
        # The machine's record follows the person it belongs to; the machine's
        # own facts do not move.
        self.assertEqual(tenant_after["email"], new)
        for column in ("slug", "ssh_public_key", "state", "server_id", "ipv4",
                       "plan", "release", "claimed_at", "installed_at",
                       "last_heartbeat", "created_at"):
            self.assertEqual(tenant_after[column], tenant_before[column], column)

        rows = self.log_rows(int(before["id"]))
        self.assertEqual(len(rows), 1)
        line = dict(rows[0])
        self.assertEqual(line["account_id"], int(before["id"]))
        self.assertEqual(line["actor"], ADMIN)
        self.assertEqual(line["action"], "repoint")
        self.assertEqual(line["previous_email"], target)
        self.assertEqual(line["new_email"], new)
        self.assertRegex(line["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        # No secret rides in the log.
        dumped = json.dumps(line)
        self.assertNotIn(before["password_hash"], dumped)
        self.assertNotIn(before["recovery_hash"], dumped)

    def test_a_literal_slot_in_an_address_is_data_not_a_template(self):
        target = self.email()
        self.seed_account(target)
        weird = "{{new_email}}@example.com"
        status, _headers, body = self.repoint(target, weird)
        self.assertEqual(status, 200, body[:300])
        text = body.decode("utf-8")
        self.assertIn(weird, text, "the typed address was read back as a slot")
        self.assertEqual(self.account(weird)["email"], weird)

    def test_the_admin_log_is_append_only(self):
        target = self.email()
        before = self.seed_account(target)
        status, _headers, body = self.repoint(target, "new-%s" % target)
        self.assertEqual(status, 200, body[:300])
        connection = self.connect()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE admin_log SET actor = 'someone'")
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM admin_log")
            connection.rollback()
        finally:
            connection.close()
        self.assertEqual(len(self.log_rows(int(before["id"]))), 1)

    def test_bad_addresses_are_refused_and_change_nothing(self):
        target = self.email()
        before = self.seed_account(target)
        other = self.email()
        self.seed_account(other)

        status, _headers, body = self.repoint("nobody@example.com", "x@example.com")
        self.assertEqual(status, 404, body[:300])

        status, _headers, body = self.repoint(target, "not an address")
        self.assertEqual(status, 422, body[:300])
        self.assertIn("That doesn't look like an email address.",
                      html.unescape(body.decode("utf-8")))

        status, _headers, body = self.repoint(target, target)
        self.assertEqual(status, 422, body[:300])
        self.assertIn("That is already the account's address.",
                      html.unescape(body.decode("utf-8")))

        status, _headers, body = self.repoint(target, other)
        self.assertEqual(status, 422, body[:300])
        self.assertIn("That address already has an account.",
                      html.unescape(body.decode("utf-8")))

        after = self.account(target)
        self.assertEqual(after["email"], target)
        self.assertEqual(after["password_hash"], before["password_hash"])
        self.assertEqual(self.log_rows(int(before["id"])), [])


# ---------------------------------------------------------------------------
# confirmation before use, and the two notices


class ConfirmationTests(AdminBase):
    def test_the_page_says_confirm_your_new_address_until_the_link_is_spent(self):
        target = self.email()
        before = self.seed_account(target)
        cookie = self.sign_in(target)
        new = "new-%s" % target
        status, _headers, body = self.repoint(target, new)
        self.assertEqual(status, 200, body[:300])

        # The browser that was signed in before the change keeps its session
        # (the epoch did not move), and the page carries the new copy.
        status, _headers, body = request(
            self.port, "GET", "/account", cookie=cookie)
        self.assertEqual(status, 200, body[:300])
        text = body.decode("utf-8")
        self.assertIn("<h1>%s</h1>" % CONFIRM_HEAD, text)
        self.assertIn(CONFIRM_LINE, text)
        self.assertIn(new, text)
        self.assertNotIn("<h1>Your machine is ready.</h1>", text)
        after = self.account(new)
        self.assertIsNone(after["email_verified_at"])
        self.assertEqual(after["provision_state"], before["provision_state"])
        self.assertEqual(after["provision_queued_at"], before["provision_queued_at"])

    def test_both_addresses_get_a_queued_message_and_the_new_one_confirms(self):
        target = self.email()
        before = self.seed_account(target)
        new = "new-%s" % target
        status, _headers, body = self.repoint(target, new)
        self.assertEqual(status, 200, body[:300])

        confirmation = self.queued_confirm(int(before["id"]))
        self.assertTrue(confirmation.is_file(), "no confirmation to the new address")
        message = self.message(confirmation)
        self.assertIn(new, message["To"])
        self.assertIn("Confirm your new address", message["Subject"])
        token = self.confirm_token(confirmation)

        notices = self.queued_notices(int(before["id"]))
        self.assertEqual(len(notices), 1, "no notice to the old address")
        notice = self.message(notices[0])
        self.assertIn(target, notice["To"])
        body_text = notice.get_body(("plain",)).get_content()
        self.assertIn("The address on your Odile account was changed", body_text)
        self.assertNotIn(new, body_text, "an email body carries an address")
        self.assertNotIn(target, body_text, "an email body carries an address")

        status, _headers, page = request(
            self.port, "GET", "/confirm?t=%s" % token)
        self.assertEqual(status, 200, page[:300])
        status, _headers, body = request(
            self.port, "POST", "/confirm", form(token=token))
        self.assertEqual(status, 303, body[:300])

        confirmed = self.account(new)
        self.assertIsNotNone(confirmed["email_verified_at"])
        self.assertEqual(confirmed["provision_state"], before["provision_state"],
                         "the confirmation queued a second provision")
        self.assertEqual(confirmed["provision_queued_at"], before["provision_queued_at"])

        # The second click is harmless and queues nothing.
        status, _headers, _body = request(
            self.port, "POST", "/confirm", form(token=token))
        self.assertEqual(status, 200)

        # The page is back to the machine's state, under the new address.
        cookie = self.sign_in(new)
        status, _headers, body = request(
            self.port, "GET", "/account", cookie=cookie)
        self.assertEqual(status, 200, body[:300])
        text = body.decode("utf-8")
        self.assertIn("<h1>Your machine is ready.</h1>", text)
        self.assertNotIn(CONFIRM_HEAD, text)
        # The ready state names the machine, not the address; the row is the
        # thing that carries the new name.
        self.assertNotIn(target, text)
        self.assertEqual(self.account(new)["email"], new)

    def test_a_repointed_unverified_account_still_queues_its_first_machine(self):
        target = self.email()
        before = self.seed_account(target, verified=False, machine=False,
                                   provision_state="unverified")
        new = "new-%s" % target
        status, _headers, body = self.repoint(target, new)
        self.assertEqual(status, 200, body[:300])
        token = self.confirm_token(self.queued_confirm(int(before["id"])))
        status, _headers, _body = request(
            self.port, "POST", "/confirm", form(token=token))
        self.assertEqual(status, 303)
        after = self.account(new)
        self.assertIsNotNone(after["email_verified_at"])
        self.assertEqual(after["provision_state"], "queued",
                         "the first machine was never queued")
        self.assertIsNotNone(after["provision_queued_at"])

    def test_a_bounce_on_the_old_address_does_not_block_the_change(self):
        target = self.email()
        before = self.seed_account(target)
        new = "new-%s" % target
        status, _headers, body = self.repoint(target, new)
        self.assertEqual(status, 200, body[:300])
        notices = self.queued_notices(int(before["id"]))
        self.assertEqual(len(notices), 1)

        # The mailer tries the old address and it bounces: the message leaves
        # the queue and nothing else moves.
        notices[0].unlink()
        after = self.account(new)
        self.assertIsNone(after["email_verified_at"])
        self.assertEqual(after["password_hash"], before["password_hash"])
        self.assertEqual(after["recovery_hash"], before["recovery_hash"])
        self.assertEqual(len(self.log_rows(int(before["id"]))), 1)
        # The account is still reachable through the address it was re-pointed
        # to, and the confirmation is still queued for it.
        self.assertTrue(self.queued_confirm(int(before["id"])).is_file())
        status, _headers, body = request(
            self.port, "GET", "/account", cookie=self.sign_in(new))
        self.assertEqual(status, 200, body[:300])


# ---------------------------------------------------------------------------
# the old address is still the account's name to /forgot


class ForgotByOldAddressTests(AdminBase):
    def test_forgot_takes_the_old_address_as_the_name(self):
        target = self.email()
        before = self.seed_account(target)
        new = "new-%s" % target
        status, _headers, body = self.repoint(target, new)
        self.assertEqual(status, 200, body[:300])

        status, headers, body = request(
            self.port, "POST", "/forgot",
            form(email=target, code=RECOVERY_CODE, password=NEW_SIGN_IN,
                 password2=NEW_SIGN_IN),
            headers={"X-Forwarded-For": "198.51.100.201"})
        self.assertEqual(status, 303, body[:300])
        self.assertEqual(headers.get("Location"), "/forgot/done")
        after = self.account(new)
        self.assertTrue(app.verify_password(NEW_SIGN_IN, after["password_hash"]))
        self.assertEqual(int(after["session_epoch"]), int(before["session_epoch"]) + 1)
        self.assertEqual(after["recovery_hash"], before["recovery_hash"])
        # The credential is still the code: the old address with a wrong code
        # answers the one mismatch string and changes nothing.
        status, _headers, body = request(
            self.port, "POST", "/forgot",
            form(email=target, code="WRONGWRONGWRONGWRONG",
                 password=NEW_SIGN_IN, password2=NEW_SIGN_IN),
            headers={"X-Forwarded-For": "198.51.100.202"})
        self.assertEqual(status, 401, body[:300])
        self.assertIn("That email and recovery code do not match.", body.decode("utf-8"))

    def test_forgot_takes_the_new_address_after_the_change(self):
        target = self.email()
        self.seed_account(target)
        new = "new-%s" % target
        status, _headers, body = self.repoint(target, new)
        self.assertEqual(status, 200, body[:300])
        status, _headers, body = request(
            self.port, "POST", "/forgot",
            form(email=new, code=RECOVERY_CODE, password=NEW_SIGN_IN,
                 password2=NEW_SIGN_IN),
            headers={"X-Forwarded-For": "198.51.100.203"})
        self.assertEqual(status, 303, body[:300])
        self.assertTrue(app.verify_password(NEW_SIGN_IN,
                                            self.account(new)["password_hash"]))


# ---------------------------------------------------------------------------
# the guard rail and the migration


HERMES_PROGRAM = r'''
import sys, types
sys.path.insert(0, sys.argv[1])
class _Any(types.ModuleType):
    def __getattr__(self, name):
        return type(name, (), {})
sys.modules.setdefault("httpx", _Any("httpx"))
from plugins.dashboard_auth.basic import _verify_password
print("yes" if _verify_password(sys.argv[2], sys.argv[3]) else "no")
'''


def hermes_agent_root() -> Path | None:
    candidates = [os.environ.get("ODILE_HERMES_AGENT_DIR", "")]
    candidates += ["/root/.hermes/hermes-agent", "/home/odile/.hermes/hermes-agent"]
    for raw in candidates:
        if not raw:
            continue
        root = Path(raw)
        if (root / "plugins" / "dashboard_auth" / "basic" / "__init__.py").is_file():
            return root
    return None


class GuardRailTests(AdminBase):
    def test_no_accounts_column_opens_the_guest_gate(self):
        target = self.email()
        before = self.seed_account(target)
        status, _headers, body = self.repoint(target, "new-%s" % target)
        self.assertEqual(status, 200, body[:300])
        row = self.account_by_id(int(before["id"]))
        cells = {column: row[column] for column in row.keys()
                 if row[column] is not None}
        # The schema did not grow a bearer: the action adds a table, not a
        # column, and the only password-shaped value is still a scrypt string.
        self.assertTrue(app.is_scrypt_verifier(row["password_hash"]))
        self.assertTrue(app.is_scrypt_verifier(row["recovery_hash"]))
        root = hermes_agent_root()
        if root is None:
            self.skipTest("the Hermes checkout is not on this machine")
        control = subprocess.run(
            [sys.executable, "-c", HERMES_PROGRAM, str(root), SIGN_IN,
             row["password_hash"]],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(control.returncode, 0, control.stderr[-300:])
        self.assertEqual(control.stdout.strip(), "yes")
        for column, value in cells.items():
            done = subprocess.run(
                [sys.executable, "-c", HERMES_PROGRAM, str(root), str(value),
                 row["password_hash"]],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(done.stdout.strip(), "no",
                             "accounts.%s is the guest gate's password" % column)


class ConsoleSheetTests(unittest.TestCase):
    """The console's own sheet paints every state the page can render.

    Brand gate round 1 read the rendering, not the file: the heading, label
    and error rules were scoped to `.account` (the account page's block, which
    is not on this page), `--line-soft` was used in two hairline declarations
    and defined nowhere, and the submit button matched no rule at all, so it
    fell back to the browser's native control. A test that greps the source
    for class names stayed green through every one of those, so these read the
    page's rules against the page's own states: each `var(--…)` resolves in
    `:root`, and each class a rule scopes appears on some state the page can
    render. The reserved classes are rendered by the states no fixture holds
    open (`err`, `ok`), which is why the union is the four render_admin calls
    and not one served page.
    """

    def states(self) -> list[str]:
        row = {"email": "tester@example.com", "slug": "odl-tester",
               "email_verified_at": None}
        return [
            app.render_admin(allowed=True, actor=ADMIN, accounts=[row]),
            app.render_admin(allowed=True, actor=ADMIN, accounts=[row],
                             error="A message.", field="new_email"),
            app.render_admin(allowed=True, actor=ADMIN, accounts=[row],
                             notice="A notice."),
            app.render_admin(allowed=False),
        ]

    def sheet(self) -> str:
        found = re.search(r"<style>(.*?)</style>", self.states()[0], re.S)
        assert found is not None, "the console page carries no <style>"
        # A comment is not a rule: `BRAND.md` in a comment is not a class.
        return re.sub(r"/\*.*?\*/", "", found.group(1), flags=re.S)

    def test_every_custom_property_the_sheet_uses_is_defined(self):
        css = self.sheet()
        used = set(re.findall(r"var\((--[A-Za-z0-9-]+)\)", css))
        defined = set(re.findall(r"(--[A-Za-z0-9-]+)\s*:", css))
        self.assertGreater(len(defined), 5, "the sheet defines no tokens")
        self.assertEqual(
            sorted(used - defined), [],
            "used but undefined in :root: %s" % sorted(used - defined))

    def test_every_class_the_sheet_scopes_is_on_a_rendered_state(self):
        css = self.sheet()
        present = set()
        for value in re.findall(r'class="([^"]*)"',
                                "".join(self.states())):
            present.update(value.split())
        scoped = set()
        for prelude in re.findall(r"[^{}]+(?=\{)", css):
            if prelude.lstrip().startswith("@"):
                continue
            scoped.update(re.findall(r"\.([A-Za-z][A-Za-z0-9_-]*)", prelude))
        self.assertTrue(scoped, "the sheet scopes no class")
        self.assertEqual(
            sorted(scoped - present), [],
            "styled but never rendered: %s" % sorted(scoped - present))


class RereadFailureTests(unittest.TestCase):
    """The re-read after the commit: the change stands, the page is the console.

    Brand gate round 1, should 4: this branch answered `text/plain` with no
    paper, title or way back — the only branch of the action that was not the
    console. The branch sits between the commit and the two queued messages,
    so a real socket cannot reach it without a corrupt row; this drives the
    handler itself with a connection whose post-commit re-read returns
    nothing, then reads the transition the action did make.
    """

    def test_the_reread_failure_answers_the_console_and_the_change_stands(self):
        old, new = "old@example.com", "new@example.com"
        sent = []

        class NullReread:
            def __init__(self, con):
                self.con = con

            def execute(self, sql, params=()):
                if sql.startswith("SELECT * FROM accounts WHERE id = ?"):
                    # The re-read finds nothing; every other statement is real.
                    return self
                return self.con.execute(sql, params)

            def fetchone(self):
                return None

            def commit(self):
                return self.con.commit()

            def rollback(self):
                return self.con.rollback()

            def close(self):
                return self.con.close()

        with tempfile.TemporaryDirectory(prefix="odile-reread-") as tmp:
            saved = app.DB_PATH
            app.DB_PATH = str(Path(tmp) / "waitlist.db")
            real_db = app.db
            try:
                app.init_db()
                con = sqlite3.connect(app.DB_PATH)
                try:
                    con.execute(
                        "INSERT INTO accounts (slug, email, password_hash,"
                        " recovery_hash, confirm_token_hash,"
                        " confirm_expires_at, email_verified_at,"
                        " trial_started_at, trial_ends_at, provision_state,"
                        " server_id, ipv4, created_at) VALUES ('odl-old', ?,"
                        " ?, ?, 'token-hash', ?, ?, ?, ?, 'ready', 'server',"
                        " '203.0.113.7', ?)",
                        (old, app.hash_password("a long enough password"),
                         app.hash_password(RECOVERY_CODE), TRIAL_END,
                         TRIAL_START, TRIAL_START, TRIAL_END, TRIAL_START))
                    con.commit()
                finally:
                    con.close()
                app.db = lambda: NullReread(real_db())
                handler = app.Handler.__new__(app.Handler)
                setattr(handler, "_same_origin", lambda: True)
                setattr(handler, "_admin_account",
                        lambda: ({"email": ADMIN}, "allowed"))
                setattr(handler, "_admin_rows", lambda: [])

                def capture(status, body="",
                            content_type="text/html; charset=utf-8", **_kw):
                    sent.append((status, body, content_type))
                setattr(handler, "_send", capture)
                handler._admin_repoint({"current_email": [old],
                                        "new_email": [new]})
                check = sqlite3.connect(app.DB_PATH)
                check.row_factory = sqlite3.Row
                try:
                    row = check.execute(
                        "SELECT * FROM accounts WHERE email = ?", (new,)
                    ).fetchone()
                    log = check.execute(
                        "SELECT * FROM admin_log ORDER BY id").fetchall()
                finally:
                    check.close()
            finally:
                app.db = real_db
                app.DB_PATH = saved

        self.assertEqual(len(sent), 1)
        status, body, content_type = sent[0]
        self.assertEqual(status, 500)
        self.assertTrue(content_type.startswith("text/html"),
                        "the re-read failure is not a page: %r" % content_type)
        self.assertIn("The address is changed", body)
        self.assertIn("Reload the console.", body)
        self.assertIn('href="/account"', body, "the page has no way back")
        self.assertIn("The console", body)
        # The sentence is true: the commit landed before the re-read failed.
        self.assertIsNotNone(row, "the address did not change")
        self.assertIsNone(row["email_verified_at"])
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["actor"], ADMIN)
        self.assertEqual(log[0]["action"], "repoint")
        self.assertEqual(log[0]["previous_email"], old)
        self.assertEqual(log[0]["new_email"], new)


class MigrationTests(unittest.TestCase):
    def test_the_admin_log_is_created_once_and_stays_append_only(self):
        con = sqlite3.connect(":memory:")
        try:
            first = app.migrate(con)
            self.assertTrue(first["tables"]["admin_log"]["created"])
            self.assertIn("admin_log", app.SCHEMA_TABLES)
            again = app.migrate(con)
            self.assertFalse(again["tables"]["admin_log"]["created"])
            self.assertEqual(again["added"], [])
            con.execute(
                "INSERT INTO admin_log (account_id, actor, action, previous_email,"
                " new_email, created_at) VALUES (1, ?, 'repoint', ?, ?, ?)",
                (ADMIN, "old@example.com", "new@example.com", TRIAL_START))
            con.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE admin_log SET actor = 'someone'")
            con.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("DELETE FROM admin_log")
            con.rollback()
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
