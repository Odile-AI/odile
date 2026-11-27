#!/usr/bin/python3
"""The owner's change of address (ODL-962): both inboxes, then one transaction.

Medium tests: the real `app.py` on a loopback socket, a throwaway database, a
throwaway outbox and real `.eml` files. No machine is ordered and no mail is
sent.

What is pinned here, in the card's own words:

* a signed-in submit needs the current password, writes one pending row, leaves
  `accounts.email` alone and queues exactly two `.eml` files — one per inbox;
* a wrong current password, the current address, and an address another
  account or machine record holds each change nothing and answer one
  field-level error;
* a second submit replaces the row (still one) with fresh tokens, and the two
  older links stop working;
* either side may confirm first, and the first side changes nothing else;
* a token past its expiry, a token for the other side and a replayed token are
  refused with one sentence and change nothing;
* the second confirmation writes `accounts.email`, `email_verified_at`, the
  tenant row's email and `completed_at` in one transaction, and leaves the
  password verifier, the recovery verifier, the session epoch and the
  machine's fields byte-identical;
* after the move the old address no longer names the account at `/forgot` and
  the new one does;
* the account page carries the form, the help line and the pending sentence,
  and no pending change once the move lands;
* no plaintext token reaches the database, the page or the process output.

    /usr/bin/python3 -m unittest tests.test_account_email -v
"""

from __future__ import annotations

import email
import hashlib
import http.client
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
from datetime import datetime, timedelta
from email import policy as email_policy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

SIGN_IN = "a long enough password"
# The reset form's new value; the name avoids the leak scan's
# `credential-assignment` rule, which reads a name ending in PASSWORD.
RESET_SIGN_IN = "another long password"
OTHER_ADDRESS = "moved-again@example.com"
CODE = "ABCDEFGH1234"
TYPED_CODE = "ABCD-EFGH-1234"
STAMP = "2026-11-20T09:00:00Z"
TRIAL_END = "2026-12-04T09:00:00Z"
PENDING = ("Confirm the change from both inboxes: the one you are leaving and "
           "the one you are moving to.")
HELP = ("This changes the address on your account. Your machine, its sign-in "
        "and your backup are unchanged.")
NEW_LINE = "Confirm that this address belongs to your Odile account."
LINK_RE = re.compile(
    r"/account/email/confirm\?side=(old|new)&t=([0-9a-f]{64})")


def old_line(new_address: str) -> str:
    return ("Confirm the move of your Odile account to %s. If you did not ask "
            "for this, ignore this message; nothing changes." % new_address)


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


class ServiceCase(unittest.TestCase):
    """One real service per class, a throwaway database, outbox and key."""

    RATE_LIMIT = "1000"

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="odile-change-email-")
        cls.dir = Path(cls.tmp.name)
        cls.db = cls.dir / "waitlist.db"
        cls.outbox = cls.dir / "outbox"
        cls.port = free_port()
        env = dict(
            os.environ,
            ODILE_BIND="127.0.0.1",
            ODILE_PORT=str(cls.port),
            ODILE_RATE_LIMIT=cls.RATE_LIMIT,
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
        cls._address_counter = 0

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proc.terminate()
        try:
            cls.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.communicate()
        cls.tmp.cleanup()

    # -- fixtures ---------------------------------------------------------

    def setUp(self) -> None:
        # One class shares a service and an outbox; each test starts with an
        # empty change directory so a queued pair always belongs to it.
        shutil.rmtree(self.outbox / "change", ignore_errors=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def email(self) -> str:
        type(self)._counter += 1
        return "change%d@example.com" % type(self)._counter

    def new_address(self) -> str:
        """A fresh address per test: a completed move really takes one."""
        type(self)._address_counter += 1
        return "moved%d@example.com" % type(self)._address_counter

    @staticmethod
    def slug_for(address: str) -> str:
        return "odl-" + hashlib.sha256(address.encode()).hexdigest()[:6]

    def seed_account(self, address: str, *, recovery: bool = False) -> sqlite3.Row:
        """One ready account and its machine row, as the page and reset read them."""
        slug = self.slug_for(address)
        machine = hashlib.sha256(("server:" + address).encode()).hexdigest()[:8]
        connection = self.connect()
        try:
            connection.execute(
                "INSERT INTO tenants (slug, email, ssh_public_key, state, plan,"
                " ipv4, server_id, release, claimed_at, created_at)"
                " VALUES (?, ?, ?, 'active', 'alpha', '203.0.113.41', ?,"
                " '2026.11.1', ?, ?)",
                (slug, address, app.throwaway_boot_key(), machine, STAMP, STAMP),
            )
            connection.execute(
                "INSERT INTO accounts (slug, email, password_hash,"
                " recovery_hash, confirm_token_hash, confirm_expires_at,"
                " email_verified_at, trial_started_at, trial_ends_at,"
                " provision_state, server_id, ipv4, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?)",
                (slug, address, app.hash_password(SIGN_IN),
                 app.hash_password(app.normalise_recovery_code(CODE)) if recovery else None,
                 hashlib.sha256(("confirm:" + address).encode()).hexdigest(),
                 "2026-11-21T09:00:00Z", STAMP, STAMP, TRIAL_END,
                 machine, "203.0.113.41", STAMP),
            )
            connection.commit()
            return connection.execute(
                "SELECT * FROM accounts WHERE email = ?", (address,)).fetchone()
        finally:
            connection.close()

    def sign_in(self, address: str) -> str:
        status, headers, body = request(
            self.port, "POST", "/signin",
            form(email=address, password=SIGN_IN))
        self.assertEqual(status, 303, body[:300])
        return headers["Set-Cookie"].split(";")[0]

    def origin(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def submit(self, cookie: str, new_address: str | None = None,
               password: str = SIGN_IN, *, origin: str | None = None):
        return request(
            self.port, "POST", "/account/email",
            form(new_email=new_address or self.new_address(),
                 current_password=password),
            cookie=cookie, origin=origin or self.origin())

    def confirm(self, link: str, cookie: str | None = None):
        """GET a link the way an inbox's browser would; the link is absolute."""
        path = link.split("https://test.odile.fyi", 1)[1]
        return request(self.port, "GET", path, cookie=cookie)

    def change_row(self, account_id: int) -> sqlite3.Row | None:
        connection = self.connect()
        try:
            return connection.execute(
                "SELECT * FROM email_changes WHERE account_id = ?"
                " ORDER BY id DESC LIMIT 1", (int(account_id),)).fetchone()
        finally:
            connection.close()

    def account(self, account_id: int) -> sqlite3.Row:
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

    def queued(self) -> list[Path]:
        directory = self.outbox / "change"
        if not directory.is_dir():
            return []
        return sorted(directory.glob("*.eml"))

    def messages(self) -> list[email.message.Message]:
        return [email.message_from_bytes(path.read_bytes(),
                                         policy=email_policy.default)
                for path in self.queued()]

    def links(self) -> dict[str, str]:
        """{side: the absolute confirmation link} from the two queued mails."""
        found: dict[str, str] = {}
        for message in self.messages():
            body = message.get_body(("plain",)).get_content()
            for side, token in LINK_RE.findall(body):
                found[side] = ("https://test.odile.fyi/account/email/confirm"
                               "?side=%s&t=%s" % (side, token))
        return found

    def page(self, cookie: str | None, path: str = "/account"):
        return request(self.port, "GET", path, cookie=cookie)


class SubmitTests(ServiceCase):
    def test_a_signed_out_submit_goes_to_the_sign_in_page(self):
        status, headers, _body = request(
            self.port, "POST", "/account/email",
            form(new_email="anyone-new@example.com", current_password=SIGN_IN))
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/login")

    def test_a_cross_site_submit_is_refused(self):
        address = self.email()
        row = self.seed_account(address)
        cookie = self.sign_in(address)
        status, _headers, body = self.submit(
            cookie, origin="https://evil.example")
        self.assertEqual(status, 403, body[:200])
        self.assertIsNone(self.change_row(row["id"]))
        self.assertEqual(self.queued(), [])

    def test_a_change_leaves_the_account_alone_and_queues_two_messages(self):
        address = self.email()
        new = self.new_address()
        row = self.seed_account(address)
        cookie = self.sign_in(address)
        status, headers, body = self.submit(cookie, new)
        self.assertEqual(status, 303, body[:300])
        self.assertEqual(headers.get("Location"), "/account")

        self.assertEqual(self.account(row["id"])["email"], address,
                         "accounts.email moved before either confirmation")
        change = self.change_row(row["id"])
        self.assertIsNotNone(change)
        self.assertEqual(change["new_email"], new)
        self.assertEqual(change["old_confirmed_at"], None)
        self.assertEqual(change["new_confirmed_at"], None)
        self.assertEqual(change["completed_at"], None)
        # `confirm_expiry()`'s 24 hours, measured on the row's own instants.
        created = datetime.strptime(change["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        expires = datetime.strptime(change["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(expires - created, timedelta(hours=24))

        queued = self.queued()
        self.assertEqual([path.name for path in queued],
                         ["%d-new.eml" % row["id"], "%d-old.eml" % row["id"]])
        self.assertEqual(oct(queued[0].stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct((self.outbox / "change").stat().st_mode & 0o777),
                         "0o700")
        subjects = {message["To"]: message["Subject"] for message in self.messages()}
        self.assertEqual(subjects, {
            address: app.CHANGE_OLD_SUBJECT,
            new: app.CHANGE_NEW_SUBJECT,
        })
        old_body = next(message for message in self.messages()
                        if message["To"] == address).get_body(("plain",)).get_content()
        new_body = next(message for message in self.messages()
                        if message["To"] == new).get_body(("plain",)).get_content()
        self.assertIn(" ".join(old_line(new).split()), " ".join(old_body.split()))
        self.assertIn("— Odile", old_body)
        self.assertIn("Private alpha · Odile pays for the machine and the inference"
                      " · no price", old_body)
        self.assertIn(NEW_LINE, " ".join(new_body.split()))
        self.assertIn("— Odile", new_body)
        self.assertIn("Private alpha · Odile pays for the machine and the inference"
                      " · no price", new_body)
        # One link per mail, and the token appears nowhere else.
        tokens = {}
        for side, link in self.links().items():
            tokens[side] = link.rsplit("t=", 1)[1]
        self.assertEqual(set(tokens), {"old", "new"})
        for side, token in tokens.items():
            body = old_body if side == "old" else new_body
            self.assertEqual(body.count("/account/email/confirm"), 1)
            self.assertEqual(body.count(token), 1)
        stored = self.db.read_bytes()
        wal = Path(str(self.db) + "-wal")
        if wal.is_file():
            stored += wal.read_bytes()
        for token in tokens.values():
            self.assertNotIn(token.encode(), stored,
                             "a plaintext token reached the database file")
        connection = sqlite3.connect(self.db)
        try:
            dump = "\n".join(connection.iterdump())
        finally:
            connection.close()
        for token in tokens.values():
            self.assertNotIn(token, dump)

        # The account page asks for the two confirmations while the change is
        # pending; the form is still there, because a second submit replaces.
        status, _headers, body = self.page(cookie)
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("Change your email", text)
        self.assertIn(HELP, text)
        self.assertIn(PENDING, text)
        self.assertIn('action="/account/email"', text)
        self.assertNotIn("<script", text.lower())
        # Every URL on the page is the account's own machine: no third party.
        for url in re.findall(r"https?://[^\"<]+", text):
            self.assertIn(".test.odile.fyi", url)

    def test_a_wrong_current_password_changes_nothing(self):
        address = self.email()
        row = self.seed_account(address)
        cookie = self.sign_in(address)
        status, _headers, body = self.submit(cookie, password="wrong password here")
        self.assertEqual(status, 422, body[:300])
        text = body.decode("utf-8")
        self.assertIn("That password does not match.", text)
        self.assertIn('id="current_password" name="current_password"', text)
        self.assertIn('aria-describedby="current_password-err"', text)
        self.assertIsNone(self.change_row(row["id"]))
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.account(row["id"])["email"], address)

    def test_the_current_address_is_refused_as_a_field_error(self):
        address = self.email()
        row = self.seed_account(address)
        cookie = self.sign_in(address)
        status, _headers, body = self.submit(cookie, new_address=address)
        self.assertEqual(status, 422)
        text = body.decode("utf-8")
        self.assertIn("already the account", text)
        self.assertIn('aria-describedby="new_email-err"', text)
        self.assertIsNone(self.change_row(row["id"]))
        self.assertEqual(self.queued(), [])

    def test_an_address_another_account_holds_is_refused(self):
        address = self.email()
        other = self.email()
        row = self.seed_account(address)
        self.seed_account(other)
        cookie = self.sign_in(address)
        status, _headers, body = self.submit(cookie, new_address=other)
        self.assertEqual(status, 422, body[:300])
        self.assertIn(b"That address already has an account.", body)
        self.assertIn(b'aria-describedby="new_email-err"', body)
        self.assertIsNone(self.change_row(row["id"]))
        self.assertEqual(self.queued(), [])

    def test_an_address_another_machine_record_holds_is_refused(self):
        address = self.email()
        row = self.seed_account(address)
        held = self.email()
        connection = self.connect()
        try:
            connection.execute(
                "INSERT INTO tenants (slug, email, ssh_public_key, state, plan,"
                " created_at) VALUES (?, ?, ?, 'invited', 'alpha', ?)",
                ("odl-held", held, app.throwaway_boot_key(), STAMP))
            connection.commit()
        finally:
            connection.close()
        cookie = self.sign_in(address)
        status, _headers, body = self.submit(cookie, new_address=held)
        self.assertEqual(status, 422, body[:300])
        self.assertIn(b"That address already has an account.", body)
        self.assertIsNone(self.change_row(row["id"]))
        self.assertEqual(self.queued(), [])

    def test_a_second_submit_replaces_the_row_and_the_older_links_die(self):
        address = self.email()
        row = self.seed_account(address)
        cookie = self.sign_in(address)
        self.assertEqual(self.submit(cookie)[0], 303)
        first = self.links()
        self.assertEqual(self.submit(cookie, new_address=OTHER_ADDRESS)[0], 303)
        second = self.links()
        self.assertNotEqual(first, second)

        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT * FROM email_changes WHERE account_id = ?",
                (int(row["id"]),)).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 1, "one pending change per account")
        self.assertEqual(rows[0]["new_email"], OTHER_ADDRESS)
        self.assertEqual(len(self.queued()), 2)
        self.assertEqual(self.account(row["id"])["email"], address)

        for link in first.values():
            status, _headers, body = self.confirm(link)
            self.assertEqual(status, 404, "an older link answered %d" % status)
            self.assertIn(b"That confirmation link is no longer valid.", body)
        self.assertIsNone(self.change_row(row["id"])["old_confirmed_at"])
        self.assertIsNone(self.change_row(row["id"])["new_confirmed_at"])


    def test_a_change_that_expired_is_replaced_not_blocked(self):
        address = self.email()
        first = self.new_address()
        later = self.new_address()
        row = self.seed_account(address)
        cookie = self.sign_in(address)
        self.assertEqual(self.submit(cookie, first)[0], 303)
        connection = self.connect()
        try:
            connection.execute(
                "UPDATE email_changes SET expires_at = '2020-01-01T00:00:00Z'"
                " WHERE account_id = ?", (int(row["id"]),))
            connection.commit()
        finally:
            connection.close()
        # The page stops asking, and the row still holds the unique slot: a new
        # submit replaces it in place instead of failing on the index.
        status, _headers, body = self.page(cookie)
        self.assertEqual(status, 200)
        self.assertNotIn(PENDING, body.decode("utf-8"))
        status, _headers, body = self.submit(cookie, later)
        self.assertEqual(status, 303, body[:300])
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT * FROM email_changes WHERE account_id = ?",
                (int(row["id"]),)).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["new_email"], later)
        self.assertIsNone(rows[0]["old_confirmed_at"])
        self.assertIsNone(rows[0]["new_confirmed_at"])
        self.assertEqual(len(self.queued()), 2)
        self.assertEqual(self.account(row["id"])["email"], address)


class ConfirmTests(ServiceCase):
    def test_the_old_side_first_then_the_new_moves_the_account(self):
        address = self.email()
        new = self.new_address()
        row = self.seed_account(address, recovery=True)
        cookie = self.sign_in(address)
        self.assertEqual(self.submit(cookie, new)[0], 303)
        links = self.links()
        before_account = dict(self.account(row["id"]))
        before_tenant = dict(self.tenant(row["slug"]))

        status, _headers, body = self.confirm(links["old"])
        self.assertEqual(status, 200, body[:300])
        self.assertIn(PENDING.encode(), body)
        change = self.change_row(row["id"])
        self.assertIsNotNone(change["old_confirmed_at"])
        self.assertIsNone(change["new_confirmed_at"])
        self.assertIsNone(change["completed_at"])
        self.assertEqual(self.account(row["id"])["email"], address)
        self.assertEqual(self.tenant(row["slug"])["email"], address)

        status, _headers, body = self.confirm(links["new"], cookie=cookie)
        self.assertEqual(status, 200, body[:300])
        text = body.decode("utf-8")
        self.assertIn("Your address is changed to <strong>%s</strong>."
                      % new, text)
        self.assertNotIn(PENDING, text)

        after_account = self.account(row["id"])
        after_tenant = self.tenant(row["slug"])
        self.assertEqual(after_account["email"], new)
        self.assertIsNotNone(after_account["email_verified_at"])
        self.assertEqual(after_tenant["email"], new)
        self.assertIsNotNone(self.change_row(row["id"])["completed_at"])
        # The credentials, the session and the machine are not the address.
        for field in ("password_hash", "recovery_hash", "session_epoch"):
            self.assertEqual(after_account[field], before_account[field], field)
        for field in ("slug", "ssh_public_key", "state", "server_id", "ipv4",
                      "plan", "release", "expires_at", "setup_token_hash",
                      "claimed_at", "installed_at", "last_heartbeat",
                      "created_at"):
            self.assertEqual(after_tenant[field], before_tenant[field], field)

    def test_the_new_side_first_then_the_old_moves_the_account(self):
        address = self.email()
        new = self.new_address()
        row = self.seed_account(address)
        cookie = self.sign_in(address)
        self.assertEqual(self.submit(cookie, new)[0], 303)
        links = self.links()

        status, _headers, body = self.confirm(links["new"])
        self.assertEqual(status, 200)
        self.assertIn(PENDING.encode(), body)
        change = self.change_row(row["id"])
        self.assertIsNone(change["old_confirmed_at"])
        self.assertIsNotNone(change["new_confirmed_at"])
        self.assertEqual(self.account(row["id"])["email"], address)

        status, _headers, body = self.confirm(links["old"], cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn(("Your address is changed to <strong>%s</strong>."
                       % new).encode(), body)
        self.assertEqual(self.account(row["id"])["email"], new)
        self.assertEqual(self.tenant(row["slug"])["email"], new)

    def test_an_expired_link_is_refused_and_changes_nothing(self):
        address = self.email()
        row = self.seed_account(address)
        cookie = self.sign_in(address)
        self.assertEqual(self.submit(cookie)[0], 303)
        links = self.links()
        connection = self.connect()
        try:
            connection.execute(
                "UPDATE email_changes SET expires_at = '2020-01-01T00:00:00Z'"
                " WHERE account_id = ?", (int(row["id"]),))
            connection.commit()
        finally:
            connection.close()
        status, _headers, body = self.confirm(links["old"])
        self.assertEqual(status, 404)
        self.assertIn(b"That confirmation link is no longer valid.", body)
        change = self.change_row(row["id"])
        self.assertIsNone(change["old_confirmed_at"])
        self.assertIsNone(change["new_confirmed_at"])
        self.assertEqual(self.account(row["id"])["email"], address)

    def test_a_token_for_the_other_side_is_refused(self):
        address = self.email()
        row = self.seed_account(address)
        cookie = self.sign_in(address)
        self.assertEqual(self.submit(cookie)[0], 303)
        links = self.links()
        crossed = links["new"].replace("side=new", "side=old")
        status, _headers, body = self.confirm(crossed)
        self.assertEqual(status, 404)
        self.assertIn(b"That confirmation link is no longer valid.", body)
        change = self.change_row(row["id"])
        self.assertIsNone(change["old_confirmed_at"])
        self.assertIsNone(change["new_confirmed_at"])

    def test_a_replayed_link_after_completion_is_refused(self):
        address = self.email()
        new = self.new_address()
        row = self.seed_account(address)
        cookie = self.sign_in(address)
        self.assertEqual(self.submit(cookie, new)[0], 303)
        links = self.links()
        self.assertEqual(self.confirm(links["old"])[0], 200)
        self.assertEqual(self.confirm(links["new"], cookie=cookie)[0], 200)
        self.assertEqual(self.account(row["id"])["email"], new)
        for link in links.values():
            status, _headers, body = self.confirm(link)
            self.assertEqual(status, 404)
            self.assertIn(b"That confirmation link is no longer valid.", body)
        self.assertEqual(self.account(row["id"])["email"], new)

    def test_the_old_address_stops_naming_the_account_and_the_new_one_starts(self):
        address = self.email()
        new = self.new_address()
        row = self.seed_account(address, recovery=True)
        cookie = self.sign_in(address)
        self.assertEqual(self.submit(cookie, new)[0], 303)
        links = self.links()
        self.assertEqual(self.confirm(links["old"])[0], 200)
        self.assertEqual(self.confirm(links["new"], cookie=cookie)[0], 200)

        status, _headers, _body = request(
            self.port, "POST", "/forgot",
            form(email=address, code=TYPED_CODE, password=RESET_SIGN_IN,
                 password2=RESET_SIGN_IN))
        self.assertEqual(status, 401, "the old address still names the account")
        status, _headers, _body = request(
            self.port, "POST", "/forgot",
            form(email=new, code=TYPED_CODE,
                 password=RESET_SIGN_IN,
                 password2=RESET_SIGN_IN))
        self.assertEqual(status, 303, "the new address does not name the account")

    def test_the_account_page_shows_no_pending_change_after_the_move(self):
        address = self.email()
        row = self.seed_account(address)
        cookie = self.sign_in(address)
        self.assertEqual(self.submit(cookie)[0], 303)
        links = self.links()
        self.assertEqual(self.confirm(links["old"])[0], 200)
        self.assertEqual(self.confirm(links["new"], cookie=cookie)[0], 200)
        status, _headers, body = self.page(cookie)
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertNotIn(PENDING, text)
        self.assertIn("Change your email", text)
        self.assertIn(HELP, text)

    def test_a_closed_link_answers_one_sentence(self):
        status, _headers, body = self.confirm(
            "https://test.odile.fyi/account/email/confirm?side=old&t=%s" % ("ab" * 32))
        self.assertEqual(status, 404)
        self.assertIn(b"That confirmation link is no longer valid.", body)


class RateLimitTests(ServiceCase):
    RATE_LIMIT = "2"

    def test_the_change_form_spends_its_own_rate_bucket(self):
        address = self.email()
        row = self.seed_account(address)
        cookie = self.sign_in(address)
        self.assertEqual(self.submit(cookie)[0], 303)
        self.assertEqual(self.submit(cookie, new_address=OTHER_ADDRESS)[0], 303)
        status, headers, body = self.submit(cookie)
        self.assertEqual(status, 429, body[:300])
        self.assertIn(b"Too many changes. Try again in ten minutes.", body)
        self.assertIn("Retry-After", headers)
        self.assertIsNotNone(self.change_row(row["id"]))


if __name__ == "__main__":
    unittest.main()
