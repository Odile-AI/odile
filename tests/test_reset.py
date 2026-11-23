#!/usr/bin/python3
"""The account reset with the recovery code (ODL-578).

Medium tests: the real `app.py` on a loopback socket, a throwaway database,
outbox, session key and heartbeat keys. No machine is ordered, nothing is
mailed and no live store is touched; the transcripts in
`docs/evidence/a5-account-reset.md` are the run record.

What is pinned here, in the card's own words:

* `accounts.recovery_hash` is additive and the migration is idempotent on a
  database that predates it;
* `POST /tenant/recovery` verifies the machine's signature with `openssl`
  against `heartbeat_keys`, writes the account's hash, answers 200 with
  `changed:true` / `changed:false`, 409 for an unregistered key and 400 for a
  value that is not an installer-format scrypt string, and echoes neither the
  hash nor the body;
* `GET /forgot` is one form, `POST /forgot` verifies the code against the
  hash, answers one error string for a wrong email-or-code, is rate-limited
  5 per address and 20 per IP per hour, writes the new password, bumps
  `session_epoch`, signs this browser in and queues no mail;
* every signed-in change ends the account's other sessions;
* the guard rail: no `accounts` column holds the plaintext, a bearer, or a
  value the guest's own gate accepts as-is.

    /usr/bin/python3 -m unittest tests.test_reset -v
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from odilectl import health  # noqa: E402

SIGN_IN = "a long enough password"
NEW_SIGN_IN = "a different long password"
TRIAL_START = "2026-11-20T09:00:00Z"
TRIAL_END = "2026-12-04T09:00:00Z"
# 20 characters from the machine's alphabet: the shape the guest mints
# (installer/first_visit.py:CODE_ALPHABET). The plaintext exists only here.
RECOVERY_CODE = "M3XK9PVT2QWR7HCL5BSD"

D13 = ("Enter your email and your recovery code, then choose a new account "
       "password. This changes your account password only; your machine's "
       "sign-in is reset on the machine with the same code.")
D16 = ("Lost your code? There is no other way back: ask us to delete your "
       "account and machine, then register again. Your backup is the only way "
       "to bring the files.")
D12 = ("This changes your account password. Your machine's sign-in is "
       "unchanged; change it on the machine.")
MISMATCH = "That email and recovery code do not match."
RATE = "Too many attempts. Try again in an hour."
HELP = "Your code is shown once, at your first sign-in."
DONE_CHANGED = "Your account password is changed."
DONE_MACHINE = ("Your machine's sign-in did not change; reset it on the machine "
                "with the same code.")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port: int, method: str, path: str, body: str | None = None,
            *, cookie: str | None = None, origin: str | None = None,
            headers: dict | None = None, raw: bytes | None = None,
            ctype: str = "application/x-www-form-urlencoded"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    sent = {"Content-Type": ctype, "Connection": "close"}
    if cookie:
        sent["Cookie"] = cookie
    if origin:
        sent["Origin"] = origin
    for name, value in (headers or {}).items():
        sent[name] = value
    payload = raw if raw is not None else (body.encode("utf-8") if body is not None else None)
    connection.request(method, path, body=payload, headers=sent)
    response = connection.getresponse()
    data = response.read()
    result = (response.status, dict(response.getheaders()), data)
    connection.close()
    return result


def form(**fields) -> str:
    return urllib.parse.urlencode(fields)


def flat(text: str) -> str:
    """A rendered page's text with runs of whitespace collapsed."""
    return re.sub(r"\s+", " ", text)


def css_declarations(text: str, selector: str) -> set[str]:
    """The declarations of one CSS rule in a rendered page, order-free."""
    match = re.search(re.escape(selector) + r"\{([^}]*)\}", text)
    if match is None:
        return set()
    return {part.strip() for part in match.group(1).split(";") if part.strip()}


def wait_health(port: int, proc: subprocess.Popen, seconds: float = 30.0) -> None:
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


def tree_digest(directory: Path) -> str:
    """sha256 of every path, mode and byte under `directory`."""
    digest = hashlib.sha256()
    if not directory.exists():
        return digest.hexdigest()
    for path in sorted(directory.rglob("*")):
        digest.update(str(path.relative_to(directory)).encode())
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def openssl() -> str:
    found = shutil.which("openssl")
    if not found:
        raise unittest.SkipTest("openssl is not installed")
    return found


class KeyPair:
    """An Ed25519 pair written by openssl, the way the guest makes one."""

    def __init__(self, directory: Path, name: str = "heartbeat") -> None:
        self.private = directory / (name + ".key")
        self.public = directory / (name + ".pub")
        subprocess.run([openssl(), "genpkey", "-algorithm", "ED25519",
                        "-out", str(self.private)], check=True, capture_output=True)
        self.private.chmod(0o600)
        subprocess.run([openssl(), "pkey", "-in", str(self.private), "-pubout",
                        "-out", str(self.public)], check=True, capture_output=True)

    def pem(self) -> str:
        return self.public.read_text(encoding="utf-8")

    def sign(self, body: bytes) -> str:
        done = subprocess.run([openssl(), "dgst", "-sign", str(self.private),
                               "-out", "/dev/stdout"], input=body, capture_output=True)
        if done.returncode != 0 or len(done.stdout) != 64:
            raise RuntimeError("openssl did not sign: %s" % done.stderr.decode())
        return base64.b64encode(done.stdout).decode("ascii")


class ResetBase(unittest.TestCase):
    """One real service per class: a throwaway database, outbox and keys."""

    # A class may lower this to reach a 429 in two presses; the default leaves
    # the other suites' per-IP budgets effectively unspent.
    RATE_LIMIT = "1000"

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="odile-reset-")
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
        # The receiver's own schema, in the same file app.py owns: on a live
        # box `scripts/heartbeat.py serve` writes it, so the fixture writes the
        # same bytes rather than a table of its own invention.
        connection = sqlite3.connect(cls.db)
        connection.executescript(health.HEARTBEAT_SCHEMA)
        connection.commit()
        connection.close()
        cls.keys_dir = cls.dir / "keys"
        cls.keys_dir.mkdir()
        cls.registered = KeyPair(cls.keys_dir, "registered")
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

    # -- fixtures ----------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def email(self) -> str:
        type(self)._counter += 1
        return "reset%d@example.com" % type(self)._counter

    @staticmethod
    def slug_for(email: str) -> str:
        return "odl-" + hashlib.sha256(email.encode()).hexdigest()[:6]

    def seed_account(self, email: str, *, verified: bool = True,
                     recovery: str | None = RECOVERY_CODE,
                     password: str = SIGN_IN, machine: bool = True,
                     tenant_server_id: str | None = None) -> sqlite3.Row:
        """One account row and its tenant row, with the machine's push applied."""
        slug = self.slug_for(email)
        recovery_hash = app.hash_password(recovery) if recovery else None
        server_id = tenant_server_id or hashlib.sha256(
            ("server:" + email).encode()).hexdigest()[:12]
        connection = self.connect()
        try:
            connection.execute(
                "INSERT INTO tenants (slug, email, ssh_public_key, state, plan,"
                " ipv4, server_id, release, created_at)"
                " VALUES (?, ?, ?, 'invited', 'alpha', ?, ?, '2026.11.1', ?)",
                (slug, email, app.throwaway_boot_key(),
                 "203.0.113.41" if machine else None,
                 server_id if machine else None, TRIAL_START),
            )
            connection.execute(
                "INSERT INTO accounts (slug, email, password_hash,"
                " recovery_hash, confirm_token_hash, confirm_expires_at,"
                " email_verified_at, trial_started_at, trial_ends_at,"
                " provision_state, server_id, ipv4, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?)",
                (slug, email, app.hash_password(password), recovery_hash,
                 hashlib.sha256(("confirm:" + email).encode()).hexdigest(),
                 "2026-11-21T09:00:00Z", TRIAL_START if verified else None,
                 TRIAL_START, TRIAL_END,
                 server_id if machine else None,
                 "203.0.113.41" if machine else None, TRIAL_START),
            )
            connection.commit()
            return connection.execute(
                "SELECT * FROM accounts WHERE email = ?", (email,)).fetchone()
        finally:
            connection.close()

    def account(self, email: str) -> sqlite3.Row:
        connection = self.connect()
        try:
            return connection.execute(
                "SELECT * FROM accounts WHERE email = ?", (email,)).fetchone()
        finally:
            connection.close()

    def sign_in(self, email: str, password: str = SIGN_IN):
        status, headers, body = request(
            self.port, "POST", "/signin", form(email=email, password=password),
            headers={"X-Forwarded-For": "198.51.100.%d" % (self._next_ip())})
        self.assertEqual(status, 303, body[:300])
        return headers["Set-Cookie"].split(";")[0]

    def _next_ip(self) -> int:
        type(self)._counter += 1
        return 1 + (type(self)._counter % 250)

    @staticmethod
    def input_tag(text: str, name: str) -> str:
        """The rendered <input> for one field, so its ARIA can be read."""
        match = re.search(r'<input[^>]*\bname="%s"[^>]*>' % re.escape(name), text)
        assert match is not None, "no input named %s" % name
        return match.group(0)

    def register_key(self, slug: str, pem: str) -> None:
        connection = self.connect()
        try:
            connection.execute(
                "INSERT OR REPLACE INTO heartbeat_keys (slug, public_key,"
                " registered_at) VALUES (?, ?, ?)",
                (slug, pem, TRIAL_START))
            connection.commit()
        finally:
            connection.close()

    def forgot(self, *, email: str, code: str = RECOVERY_CODE,
               password: str = NEW_SIGN_IN, repeat: str | None = None,
               ip: str | None = None, cookie: str | None = None,
               origin: str | None = None):
        fields = dict(email=email, code=code, password=password,
                      password2=password if repeat is None else repeat)
        return request(self.port, "POST", "/forgot", form(**fields),
                       cookie=cookie, origin=origin,
                       headers={"X-Forwarded-For": ip or "198.51.100.7"})

    def signed_push(self, key: KeyPair, payload: dict, *, body: bytes | None = None,
                    signed_body: bytes | None = None, when: str | None = None):
        raw = body if body is not None else json.dumps(payload).encode("utf-8")
        signature = key.sign(raw if signed_body is None else signed_body)
        return request(self.port, "POST", "/tenant/recovery", raw=raw,
                       ctype="application/json",
                       headers={"X-Odile-Signature": signature})


# ---------------------------------------------------------------------------
# the column and its migration


class MigrationTests(unittest.TestCase):
    """`recovery_hash` is additive and applied once on an existing database."""

    OLD_ACCOUNTS = """
    CREATE TABLE accounts (
        id                    INTEGER PRIMARY KEY,
        slug                  TEXT    NOT NULL UNIQUE,
        email                 TEXT    NOT NULL UNIQUE,
        password_hash         TEXT    NOT NULL,
        confirm_token_hash    TEXT    NOT NULL UNIQUE,
        confirm_expires_at    TEXT    NOT NULL,
        email_verified_at     TEXT,
        session_epoch         INTEGER NOT NULL DEFAULT 1,
        trial_started_at      TEXT    NOT NULL,
        trial_ends_at         TEXT    NOT NULL,
        provision_state       TEXT    NOT NULL DEFAULT 'unverified',
        provision_queued_at   TEXT,
        provision_started_at  TEXT,
        provision_finished_at TEXT,
        provision_reason      TEXT,
        server_id             TEXT,
        ipv4                  TEXT,
        key_label             TEXT,
        key_prefix            TEXT,
        created_at            TEXT    NOT NULL
    );
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="odile-reset-migrate-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "old.db"
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.executescript(self.OLD_ACCOUNTS)
        connection.execute(
            "INSERT INTO accounts (slug, email, password_hash,"
            " confirm_token_hash, confirm_expires_at, trial_started_at,"
            " trial_ends_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("odl-old123", "old@example.com", app.hash_password(SIGN_IN),
             hashlib.sha256(b"token").hexdigest(), "2026-11-21T09:00:00Z",
             TRIAL_START, TRIAL_END, TRIAL_START))
        connection.commit()
        self.connection = connection
        self.addCleanup(connection.close)

    def columns(self) -> dict:
        return {row[1]: row for row in self.connection.execute(
            "PRAGMA table_info(accounts)")}

    def test_the_first_migration_adds_the_column_and_keeps_the_row(self):
        self.assertNotIn("recovery_hash", self.columns())
        report = app.migrate(self.connection)
        self.assertIn("accounts.recovery_hash", report["added"])
        self.assertIn("recovery_hash", self.columns())
        row = self.connection.execute(
            "SELECT email, recovery_hash FROM accounts").fetchone()
        self.assertEqual(row["email"], "old@example.com")
        self.assertIsNone(row["recovery_hash"])

    def test_the_second_migration_changes_nothing(self):
        app.migrate(self.connection)
        before = self.connection.execute(
            "SELECT * FROM accounts").fetchall()
        report = app.migrate(self.connection)
        self.assertEqual(report["added"], [])
        after = self.connection.execute("SELECT * FROM accounts").fetchall()
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])

    def test_a_fresh_database_carries_the_column_and_no_value(self):
        with tempfile.TemporaryDirectory(prefix="odile-reset-fresh-") as directory:
            connection = sqlite3.connect(Path(directory) / "fresh.db")
            self.addCleanup(connection.close)
            app.migrate(connection)
            names = {row[1] for row in connection.execute("PRAGMA table_info(accounts)")}
            self.assertIn("recovery_hash", names)


# ---------------------------------------------------------------------------
# POST /tenant/recovery


class RecoveryEndpointTests(ResetBase):
    def setUp(self) -> None:
        self.email_address = self.email()
        self.slug = self.slug_for(self.email_address)
        self.row = self.seed_account(self.email_address, recovery=None)
        self.register_key(self.slug, self.registered.pem())

    def payload(self, **overrides) -> dict:
        body = {
            "slug": self.slug,
            "instance_id": self.row["server_id"],
            "recovery_hash": app.hash_password(RECOVERY_CODE),
        }
        body.update(overrides)
        return body

    def test_a_signed_push_writes_the_hash_and_answers_changed_true(self):
        pushed = self.payload()
        status, headers, body = self.signed_push(self.registered, pushed)
        self.assertEqual(status, 200, body[:300])
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(json.loads(body), {"changed": True, "ok": True})
        stored = self.account(self.email_address)["recovery_hash"]
        self.assertEqual(stored, pushed["recovery_hash"])
        # The hash opens nothing on its own: it is a verifier, not a bearer.
        self.assertTrue(app.verify_password(RECOVERY_CODE, stored))
        self.assertFalse(app.verify_password(stored, stored))
        # The response never echoes the value it stored.
        self.assertNotIn(pushed["recovery_hash"].encode(), body)

    def test_the_same_value_again_is_idempotent_and_says_changed_false(self):
        pushed = self.payload()
        self.assertEqual(self.signed_push(self.registered, pushed)[0], 200)
        status, _headers, body = self.signed_push(self.registered, pushed)
        self.assertEqual(status, 200, body[:300])
        self.assertEqual(json.loads(body), {"changed": False, "ok": True})
        self.assertEqual(self.account(self.email_address)["recovery_hash"],
                         pushed["recovery_hash"])

    def test_a_new_value_replaces_the_old_one_and_the_old_code_stops(self):
        old = app.hash_password(RECOVERY_CODE)
        self.assertEqual(self.signed_push(
            self.registered, self.payload(recovery_hash=old))[0], 200)
        replacement = "ABCDEFGHJKLMNPQRSTUV"
        status, _headers, body = self.signed_push(
            self.registered, self.payload(recovery_hash=app.hash_password(replacement)))
        self.assertEqual(status, 200, body[:300])
        self.assertEqual(json.loads(body)["changed"], True)
        stored = self.account(self.email_address)["recovery_hash"]
        self.assertTrue(app.verify_password(replacement, stored))
        self.assertFalse(app.verify_password(RECOVERY_CODE, stored))
        # And the account's own reset now accepts the replacement only.
        self.assertEqual(self.forgot(email=self.email_address,
                                     code=RECOVERY_CODE, ip="203.0.113.201")[0], 401)
        self.assertEqual(self.forgot(email=self.email_address, code=replacement,
                                     ip="203.0.113.202")[0], 303)

    def test_an_unregistered_key_is_409(self):
        # A slug no heartbeat has ever registered: the endpoint must refuse
        # before it looks at the signature or the value.
        stranger = KeyPair(self.keys_dir, "stranger")
        status, _headers, body = self.signed_push(stranger, {
            "slug": "odl-unregistered",
            "instance_id": self.row["server_id"],
            "recovery_hash": app.hash_password(RECOVERY_CODE)})
        self.assertEqual(status, 409, body[:300])
        self.assertIsNone(self.account(self.email_address)["recovery_hash"])

    def test_a_registered_key_signing_a_different_key_is_401(self):
        stranger = KeyPair(self.keys_dir, "wrong")
        status, _headers, body = self.signed_push(stranger, self.payload())
        self.assertEqual(status, 401, body[:300])
        self.assertIsNone(self.account(self.email_address)["recovery_hash"])

    def test_a_tampered_body_does_not_verify(self):
        raw = json.dumps(self.payload()).encode()
        other = json.dumps(self.payload(recovery_hash=app.hash_password("x" * 20))).encode()
        status, _headers, body = self.signed_push(
            self.registered, {}, body=other, signed_body=raw)
        self.assertEqual(status, 401, body[:300])
        self.assertIsNone(self.account(self.email_address)["recovery_hash"])

    def test_a_value_that_is_not_an_installer_hash_is_400(self):
        for bad in ("hunter2", RECOVERY_CODE, "", "bcrypt$16384$8$1$AAAA$BBBB",
                    "scrypt$16384$8$1$AAAA$BBBB",
                    "scrypt$1234$8$1$" + base64.b64encode(b"x" * 16).decode()
                    + "$" + base64.b64encode(b"y" * 32).decode(),
                    "scrypt$16384$8$1$!!!!$" + base64.b64encode(b"y" * 32).decode(),
                    app.hash_password(SIGN_IN) + "\n"):
            with self.subTest(value=bad[:24]):
                status, _headers, body = self.signed_push(
                    self.registered, self.payload(recovery_hash=bad))
                self.assertEqual(status, 400, body[:300])
                self.assertIsNone(self.account(self.email_address)["recovery_hash"])

    def test_the_accepted_shapes_are_the_two_the_tree_writes(self):
        # The account's own verifier and the guest's first-visit hash use one
        # format; both must pass the endpoint's check.
        self.assertTrue(app.is_scrypt_verifier(app.hash_password(SIGN_IN)))
        self.assertTrue(app.is_scrypt_verifier(self.payload()["recovery_hash"]))

    def test_a_body_that_is_not_json_is_400_and_not_echoed(self):
        raw = b"slug=odl-123&secret=hunter2"
        signature = self.registered.sign(raw)
        status, _headers, body = request(
            self.port, "POST", "/tenant/recovery", raw=raw,
            ctype="application/json",
            headers={"X-Odile-Signature": signature})
        self.assertEqual(status, 400, body[:300])
        self.assertNotIn(b"hunter2", body)

    def test_a_missing_field_is_400(self):
        for missing in ("slug", "instance_id", "recovery_hash"):
            with self.subTest(missing=missing):
                payload = self.payload()
                payload.pop(missing)
                status, _headers, body = self.signed_push(self.registered, payload)
                self.assertEqual(status, 400, body[:300])

    def test_an_instance_id_that_is_not_this_machines_is_400(self):
        status, _headers, body = self.signed_push(
            self.registered, self.payload(instance_id="srv-not-this-one"))
        self.assertEqual(status, 400, body[:300])
        self.assertIsNone(self.account(self.email_address)["recovery_hash"])

    def test_a_slug_with_no_account_is_404(self):
        self.register_key("odl-nobody", self.registered.pem())
        status, _headers, body = self.signed_push(self.registered, {
            "slug": "odl-nobody", "instance_id": "srv-x",
            "recovery_hash": app.hash_password(RECOVERY_CODE)})
        self.assertEqual(status, 404, body[:300])

    def test_a_signed_push_with_no_heartbeat_store_at_all_is_409(self):
        # The heartbeat schema may not have been created on this database yet;
        # that is an unregistered key, not a 500.
        stranger_slug = "odl-nokeys"
        connection = self.connect()
        try:
            connection.execute("DELETE FROM heartbeat_keys WHERE slug = ?",
                               (stranger_slug,))
            connection.commit()
        finally:
            connection.close()
        status, _headers, body = self.signed_push(self.registered, {
            "slug": stranger_slug, "instance_id": "srv-x",
            "recovery_hash": app.hash_password(RECOVERY_CODE)})
        self.assertEqual(status, 409, body[:300])


class NoLoggingTests(unittest.TestCase):
    """Neither the body nor the hash reaches the service's output."""

    def test_neither_the_push_nor_the_reset_writes_a_value(self):
        tmp = tempfile.TemporaryDirectory(prefix="odile-reset-log-")
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        db = directory / "waitlist.db"
        port = free_port()
        env = dict(
            os.environ, ODILE_BIND="127.0.0.1", ODILE_PORT=str(port),
            ODILE_RATE_LIMIT="1000", ODILE_DB=str(db),
            ODILE_OUTBOX=str(directory / "outbox"),
            ODILE_SESSION_KEY_FILE=str(directory / "session.key"),
            ODILE_ATTEST=str(directory / "attestation.json"),
            ODILE_ROADMAP=str(directory / "roadmap.html"),
            ODILE_TENANT_DOMAIN="test.odile.fyi",
        )
        proc = subprocess.Popen([sys.executable, str(ROOT / "app.py")], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: (proc.terminate(), proc.communicate()))
        wait_health(port, proc)
        email = "log@example.com"
        slug = "odl-" + hashlib.sha256(email.encode()).hexdigest()[:6]
        key = KeyPair(directory, "key")
        connection = sqlite3.connect(db)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(health.HEARTBEAT_SCHEMA)
        connection.execute(
            "INSERT INTO heartbeat_keys (slug, public_key, registered_at)"
            " VALUES (?, ?, ?)", (slug, key.pem(), TRIAL_START))
        connection.execute(
            "INSERT INTO accounts (slug, email, password_hash,"
            " confirm_token_hash, confirm_expires_at, trial_started_at,"
            " trial_ends_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (slug, email, app.hash_password(SIGN_IN),
             hashlib.sha256(b"t").hexdigest(), "2026-11-21T09:00:00Z",
             TRIAL_START, TRIAL_END, TRIAL_START))
        connection.commit()
        connection.close()
        pushed = app.hash_password(RECOVERY_CODE)
        raw = json.dumps({"slug": slug, "instance_id": "srv-log",
                          "recovery_hash": pushed}).encode()
        status, _headers, _body = request(
            port, "POST", "/tenant/recovery", raw=raw,
            ctype="application/json",
            headers={"X-Odile-Signature": key.sign(raw)})
        self.assertEqual(status, 200)
        status, _headers, _body = request(
            port, "POST", "/forgot",
            form(email=email, code=RECOVERY_CODE, password=NEW_SIGN_IN,
                 password2=NEW_SIGN_IN))
        self.assertEqual(status, 303)
        proc.terminate()
        output = proc.communicate(timeout=15)[0].decode("utf-8", "replace")
        for value in (RECOVERY_CODE, NEW_SIGN_IN, SIGN_IN, pushed,
                      "recovery_hash", "code="):
            self.assertNotIn(value, output, "the service logged %r" % value[:20])


# ---------------------------------------------------------------------------
# GET/POST /forgot


class ForgotPageTests(ResetBase):
    def test_the_form_is_one_form_with_the_four_fields_and_the_copy(self):
        status, headers, body = request(self.port, "GET", "/forgot")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertEqual(text.count("<form"), 1)
        for name in ("email", "code", "password", "password2"):
            self.assertIn('name="%s"' % name, text)
        page = flat(text)
        self.assertIn(D13, page)
        self.assertIn(HELP, page)
        self.assertIn(D16, page)
        self.assertIn("Reset your account password", text)
        self.assertNotIn("<script", text.lower())
        self.assertIsNone(re.search(r'<(?:link|img)\b[^>]*\b(?:href|src)="https?://', text))
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        # A field holds a value, not a label: the sheet tracks capitals only,
        # so no input rule carries letter-spacing (Brand gate round 2 follow-up,
        # t_7f1fb414's lint).
        for selector in ("input[type=email]", "input[type=password]",
                         "input[type=text]"):
            rule = css_declarations(text, selector)
            self.assertFalse([d for d in rule if d.startswith("letter-spacing")],
                             "%s carries letter-spacing" % selector)
        # Selecting the filled button's label paints the sheet's inverted pair
        # (Brand gate round 2, should 5).
        self.assertIn("background:var(--paper)",
                      css_declarations(text, "button::selection"))
        self.assertIn("color:var(--ink)",
                      css_declarations(text, "button::selection"))

    def test_the_sign_in_page_links_to_the_reset(self):
        status, _headers, body = request(self.port, "GET", "/signin")
        self.assertEqual(status, 200)
        self.assertIn('href="/forgot"', body.decode("utf-8"))


class ForgotFlowTests(ResetBase):
    def setUp(self) -> None:
        self.email_address = self.email()
        self.row = self.seed_account(self.email_address)

    def test_a_correct_code_writes_the_new_password_and_signs_this_browser_in(self):
        before = self.account(self.email_address)
        status, headers, body = self.forgot(email=self.email_address,
                                            ip="203.0.113.31")
        self.assertEqual(status, 303, body[:300])
        self.assertEqual(headers.get("Location"), "/forgot/done")
        cookie = headers["Set-Cookie"].split(";")[0]
        after = self.account(self.email_address)
        self.assertNotEqual(after["password_hash"], before["password_hash"])
        self.assertTrue(app.verify_password(NEW_SIGN_IN, after["password_hash"]))
        self.assertFalse(app.verify_password(SIGN_IN, after["password_hash"]))
        self.assertEqual(int(after["session_epoch"]), int(before["session_epoch"]) + 1)
        # The browser that reset is signed in; the old password is not.
        status, _headers, body = request(self.port, "GET", "/account", cookie=cookie)
        self.assertEqual(status, 200, body[:300])
        self.assertIn("Your machine is ready.", body.decode("utf-8"))
        status, _headers, _body = request(
            self.port, "POST", "/signin",
            form(email=self.email_address, password=SIGN_IN))
        self.assertEqual(status, 401)
        self.sign_in(self.email_address, NEW_SIGN_IN)

    def test_a_pasted_code_with_dashes_and_lower_case_is_the_same_code(self):
        """The page prints the code in groups; the account reads it that way.

        ODL-926: the machine hashes the letters and digits only, so a code
        pasted with its dashes (or in lower case) must verify here too, or the
        one code would work only when typed without them.
        """
        pasted = " " + "-".join(
            RECOVERY_CODE.lower()[index:index + 4] for index in range(0, 20, 4)) + " "
        status, headers, body = self.forgot(email=self.email_address, code=pasted,
                                            ip="203.0.113.33")
        self.assertEqual(status, 303, body[:300])
        self.assertEqual(headers.get("Location"), "/forgot/done")
        self.assertTrue(app.verify_password(NEW_SIGN_IN,
                                            self.account(self.email_address)["password_hash"]))

    def test_the_success_page_says_what_changed_and_links_the_machines_reset(self):
        status, headers, _body = self.forgot(email=self.email_address,
                                             ip="203.0.113.32")
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"].split(";")[0]
        status, _headers, body = request(self.port, "GET", "/forgot/done", cookie=cookie)
        self.assertEqual(status, 200, body[:300])
        page = flat(body.decode("utf-8"))
        self.assertIn(DONE_CHANGED, page)
        self.assertIn(DONE_MACHINE, page)
        self.assertIn("https://%s.test.odile.fyi/first-visit/reset"
                      % self.row["slug"], page)
        self.assertNotIn("<script", page.lower())

    def test_the_success_page_needs_the_browser_that_reset(self):
        status, headers, _body = request(self.port, "GET", "/forgot/done")
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/login")

    def test_a_wrong_code_and_an_unknown_address_answer_the_same_thing(self):
        known = self.forgot(email=self.email_address, code="WRONGWRONGWRONGWRONG",
                            ip="203.0.113.41")
        unknown = self.forgot(email="nobody@example.com", code=RECOVERY_CODE,
                              ip="203.0.113.42")
        self.assertEqual(known[0], 401)
        self.assertEqual(unknown[0], 401)
        self.assertIn(MISMATCH, flat(known[2].decode()))
        self.assertIn(MISMATCH, flat(unknown[2].decode()))
        self.assertEqual(known[2], unknown[2].replace(b"nobody@example.com",
                                                      self.email_address.encode()))
        # Nothing was written and no session was minted.
        self.assertTrue(app.verify_password(SIGN_IN,
                                            self.account(self.email_address)["password_hash"]))
        self.assertNotIn("Set-Cookie", known[1])

    def test_an_account_with_no_code_pushed_yet_answers_the_same_string(self):
        fresh = self.email()
        self.seed_account(fresh, recovery=None)
        status, _headers, body = self.forgot(email=fresh, ip="203.0.113.43")
        self.assertEqual(status, 401)
        self.assertIn(MISMATCH, flat(body.decode()))

    def test_a_short_password_and_a_mismatch_are_field_errors(self):
        short = self.forgot(email=self.email_address, password="short",
                            ip="203.0.113.44")
        self.assertEqual(short[0], 422)
        self.assertIn("Use at least 16 characters.", short[2].decode())
        self.assertIn('id="password-err"', short[2].decode())
        mismatch = self.forgot(email=self.email_address, password=NEW_SIGN_IN,
                               repeat=NEW_SIGN_IN + "x", ip="203.0.113.45")
        self.assertEqual(mismatch[0], 422)
        self.assertIn("The two passwords do not match.", mismatch[2].decode())
        self.assertIn('id="password2-err"', mismatch[2].decode())
        self.assertTrue(app.verify_password(SIGN_IN,
                                            self.account(self.email_address)["password_hash"]))

    def test_the_wrong_code_sits_under_the_code_field_and_both_inputs_point_at_it(self):
        status, _headers, body = self.forgot(
            email=self.email_address, code="WRONGWRONGWRONGWRONG",
            ip="203.0.113.48")
        self.assertEqual(status, 401)
        text = body.decode("utf-8")
        # In place, under the field the reader has to correct — not a summary
        # over four fields (Brand gate round 2, should 3).
        message_at = text.index('<p class="err" id="forgot-err"')
        self.assertGreater(message_at, text.index('id="code"'))
        self.assertLess(message_at, text.index('id="password"'))
        for name in ("email", "code"):
            tag = self.input_tag(text, name)
            self.assertIn('aria-invalid="true"', tag, name)
            self.assertIn('aria-describedby="forgot-err"', tag, name)
        # The shape errors keep their own slot and their own field's ARIA pair.
        short = self.forgot(email=self.email_address, password="short",
                            ip="203.0.113.49")[2].decode("utf-8")
        self.assertIn('id="password-err"', short)
        self.assertIn('aria-describedby="password-err"',
                      self.input_tag(short, "password"))
        self.assertNotIn('aria-describedby="forgot-err"', short)

    def test_a_cross_site_post_is_refused(self):
        status, _headers, _body = self.forgot(
            email=self.email_address, origin="https://evil.example",
            ip="203.0.113.46")
        self.assertEqual(status, 403)

    def test_the_reset_queues_no_mail(self):
        confirm_dir = self.outbox / "confirm"
        ready_dir = self.outbox / "ready"
        confirm_dir.mkdir(parents=True, exist_ok=True)
        ready_dir.mkdir(parents=True, exist_ok=True)
        (confirm_dir / "1.eml").write_bytes(b"confirm bytes\n")
        (ready_dir / "odl-x.eml").write_bytes(b"ready bytes\n")
        before = tree_digest(self.outbox)
        status, _headers, _body = self.forgot(email=self.email_address,
                                              ip="203.0.113.47")
        self.assertEqual(status, 303)
        self.assertEqual(tree_digest(self.outbox), before,
                         "the reset wrote into the outbox")

    def test_five_attempts_for_one_address_and_the_sixth_is_429(self):
        address = self.email()
        self.seed_account(address)
        statuses = []
        for attempt in range(6):
            status, headers, body = self.forgot(
                email=address, code="WRONGWRONGWRONGWRONG" if attempt else RECOVERY_CODE,
                ip="203.0.113.%d" % (60 + attempt))
            statuses.append(status)
        self.assertEqual(statuses[:5], [303] + [401] * 4, statuses)
        self.assertEqual(statuses[5], 429, statuses)
        self.assertIn(RATE, flat(body.decode()))
        self.assertEqual(headers.get("Retry-After"), "3600")

    def test_the_address_bucket_is_per_address(self):
        first = self.email()
        second = self.email()
        self.seed_account(first)
        self.seed_account(second)
        for attempt in range(5):
            self.forgot(email=first, code="WRONGWRONGWRONGWRONG",
                        ip="203.0.113.%d" % (80 + attempt))
        status, _headers, _body = self.forgot(email=second, ip="203.0.113.90")
        self.assertEqual(status, 303)

    def test_twenty_attempts_from_one_ip_and_the_twenty_first_is_429(self):
        statuses = []
        for attempt in range(21):
            address = self.email()
            self.seed_account(address)
            status, _headers, _body = self.forgot(
                email=address, code="WRONGWRONGWRONGWRONG", ip="198.18.0.9")
            statuses.append(status)
        self.assertEqual(statuses[:20], [401] * 20, statuses)
        self.assertEqual(statuses[20], 429, statuses)


# ---------------------------------------------------------------------------
# the session epoch on every signed-in change


class SessionEpochTests(ResetBase):
    def test_a_reset_ends_the_accounts_other_browsers(self):
        email = self.email()
        self.seed_account(email)
        first = self.sign_in(email)
        second = self.sign_in(email)
        self.assertEqual(request(self.port, "GET", "/account", cookie=first)[0], 200)
        self.assertEqual(request(self.port, "GET", "/account", cookie=second)[0], 200)
        status, headers, _body = self.forgot(email=email, ip="203.0.113.111")
        self.assertEqual(status, 303)
        fresh = headers["Set-Cookie"].split(";")[0]
        for stale in (first, second):
            status, headers, _body = request(self.port, "GET", "/account", cookie=stale)
            self.assertEqual(status, 303)
            self.assertEqual(headers.get("Location"), "/login")
        self.assertEqual(request(self.port, "GET", "/account", cookie=fresh)[0], 200)

    def test_a_change_with_the_current_password_ends_the_others(self):
        email = self.email()
        self.seed_account(email)
        first = self.sign_in(email)
        second = self.sign_in(email)
        before = self.account(email)
        status, headers, body = request(
            self.port, "POST", "/account/password",
            form(current=SIGN_IN, password=NEW_SIGN_IN, password2=NEW_SIGN_IN),
            cookie=first, headers={"X-Forwarded-For": "203.0.113.112"})
        self.assertEqual(status, 303, body[:300])
        self.assertEqual(headers.get("Location"), "/account")
        fresh = headers["Set-Cookie"].split(";")[0]
        after = self.account(email)
        self.assertTrue(app.verify_password(NEW_SIGN_IN, after["password_hash"]))
        self.assertEqual(int(after["session_epoch"]), int(before["session_epoch"]) + 1)
        self.assertEqual(request(self.port, "GET", "/account", cookie=second)[0], 303)
        self.assertEqual(request(self.port, "GET", "/account", cookie=fresh)[0], 200)
        self.sign_in(email, NEW_SIGN_IN)

    def test_a_wrong_current_password_changes_nothing(self):
        email = self.email()
        self.seed_account(email)
        cookie = self.sign_in(email)
        before = self.account(email)
        status, _headers, body = request(
            self.port, "POST", "/account/password",
            form(current="not the password", password=NEW_SIGN_IN,
                 password2=NEW_SIGN_IN),
            cookie=cookie, headers={"X-Forwarded-For": "203.0.113.113"})
        self.assertIn(status, (401, 422), body[:300])
        self.assertIn("That password does not match.", body.decode())
        after = self.account(email)
        self.assertEqual(after["password_hash"], before["password_hash"])
        self.assertEqual(after["session_epoch"], before["session_epoch"])
        self.assertEqual(request(self.port, "GET", "/account", cookie=cookie)[0], 200)


# ---------------------------------------------------------------------------
# the guard rail


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
    """The Hermes checkout whose dashboard basic-auth the guest's gate runs."""
    candidates = [os.environ.get("ODILE_HERMES_AGENT_DIR", "")]
    candidates += ["/root/.hermes/hermes-agent", "/home/odile/.hermes/hermes-agent"]
    for raw in candidates:
        if not raw:
            continue
        root = Path(raw)
        if (root / "plugins" / "dashboard_auth" / "basic" / "__init__.py").is_file():
            return root
    return None


class GuestGate:
    """The workspace gate: HTTP Basic over the vendor's own scrypt check.

    It is the guest's gate in the only form a HQ test can run: the verifier is
    `plugins.dashboard_auth.basic._verify_password`, the code the dashboard
    loads, executed in a subprocess so no vendor module reaches this process.
    """

    def __init__(self, root: Path, encoded: str, username: str = "odile") -> None:
        self.root = root
        self.encoded = encoded
        self.username = username
        gate = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):  # noqa: N802 - stdlib spelling
                header = self.headers.get("Authorization", "")
                ok = False
                if header.startswith("Basic "):
                    try:
                        decoded = base64.b64decode(header[6:].strip(),
                                                   validate=True).decode("utf-8")
                    except (ValueError, UnicodeDecodeError):
                        decoded = ""
                    user, separator, supplied = decoded.partition(":")
                    ok = bool(separator) and user == gate.username \
                        and gate.verifies(supplied)
                body = b"welcome\n" if ok else b"password required\n"
                self.send_response(200 if ok else 401)
                if not ok:
                    self.send_header("WWW-Authenticate",
                                     'Basic realm="Odile", charset="UTF-8"')
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def verifies(self, supplied: str) -> bool:
        done = subprocess.run(
            [sys.executable, "-c", HERMES_PROGRAM, str(self.root), supplied,
             self.encoded],
            capture_output=True, text=True, timeout=120)
        return done.returncode == 0 and done.stdout.strip() == "yes"

    def post(self, password: str):
        token = base64.b64encode(("%s:%s" % (self.username, password)).encode()).decode()
        return request(self.port, "POST", "/", body="",
                       headers={"Authorization": "Basic " + token})

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class GuardRailTests(ResetBase):
    def test_no_accounts_column_holds_the_plaintext_a_bearer_or_a_gate_password(self):
        email = self.email()
        connection = self.connect()
        try:
            token = "f" * 64
            connection.execute(
                "INSERT INTO tenants (slug, email, ssh_public_key, state, plan,"
                " created_at) VALUES (?, ?, ?, 'invited', 'alpha', ?)",
                (self.slug_for(email), email, app.throwaway_boot_key(), TRIAL_START))
            connection.execute(
                "INSERT INTO accounts (slug, email, password_hash, recovery_hash,"
                " confirm_token_hash, confirm_expires_at, email_verified_at,"
                " trial_started_at, trial_ends_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.slug_for(email), email, app.hash_password(SIGN_IN),
                 app.hash_password(RECOVERY_CODE), app.hash_token(token),
                 "2026-11-21T09:00:00Z", TRIAL_START, TRIAL_START, TRIAL_END,
                 TRIAL_START))
            connection.commit()
        finally:
            connection.close()
        row = self.account(email)
        cells = [str(value) for value in tuple(row)]
        for secret in (SIGN_IN, RECOVERY_CODE, token):
            self.assertNotIn(secret, cells,
                             "an accounts column holds a raw value")
        self.assertNotIn(SIGN_IN, json.dumps(dict(row)))
        # Every stored verifier is the installer's scrypt string; a token is a
        # sha256 and an epoch is an integer.
        self.assertTrue(app.is_scrypt_verifier(row["password_hash"]))
        self.assertTrue(app.is_scrypt_verifier(row["recovery_hash"]))
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", row["confirm_token_hash"]))

    def test_the_stored_verifier_is_not_the_guest_gates_password(self):
        root = hermes_agent_root()
        if root is None:
            self.skipTest("the Hermes checkout is not on this machine")
        email = self.email()
        row = self.seed_account(email)
        gate = GuestGate(root, row["password_hash"])
        self.addCleanup(gate.close)
        # The control: the gate is live and accepts the password it verifies.
        status, headers, _body = gate.post(SIGN_IN)
        self.assertEqual(status, 200, headers)
        # The rail: the stored verifier POSTed as the password is a 401.
        status, headers, body = gate.post(row["password_hash"])
        self.assertEqual(status, 401, body[:200])
        self.assertTrue(headers.get("WWW-Authenticate", "").startswith("Basic "))
        # And the guest's own scrypt check agrees, without the HTTP layer.
        self.assertFalse(app.verify_password(row["password_hash"], row["password_hash"]))


# ---------------------------------------------------------------------------
# the account page carries the reset


class AccountPageTests(ResetBase):
    def page(self, state: str):
        email = self.email()
        row = self.seed_account(email, verified=(state != "confirm"))
        if state == "failed":
            connection = self.connect()
            try:
                connection.execute(
                    "UPDATE accounts SET provision_state = 'failed',"
                    " provision_reason = 'create: HTTP 404.' WHERE id = ?",
                    (int(row["id"]),))
                connection.commit()
            finally:
                connection.close()
            row = self.account(email)
        elif state == "building":
            connection = self.connect()
            try:
                connection.execute(
                    "UPDATE accounts SET provision_state = 'building' WHERE id = ?",
                    (int(row["id"]),))
                connection.commit()
            finally:
                connection.close()
        cookie = self.sign_in(email)
        status, _headers, body = request(self.port, "GET", "/account", cookie=cookie)
        self.assertEqual(status, 200, body[:300])
        return row, body.decode("utf-8")

    def test_every_signed_in_state_carries_the_change_form_and_the_lost_code_block(self):
        headings = {
            "confirm": "Check your email.",
            "building": "Building.",
            "ready": "Your machine is ready.",
        }
        for state in ("confirm", "building", "ready"):
            with self.subTest(state=state):
                row, text = self.page(state)
                page = flat(text)
                # The machine's state is the page's own state block.
                self.assertIn("<h1>%s</h1>" % headings[state], text)
                self.assertIn("Change your password", text)
                self.assertIn(D12, page)
                self.assertIn(D16, page)
                self.assertIn('action="/account/password"', text)
                self.assertIn('href="/forgot"', text)
                self.assertIn("https://%s.test.odile.fyi/first-visit/reset"
                              % row["slug"], text)
                self.assertNotIn("<script", text.lower())
                self.assertIsNone(
                    re.search(r'<(?:link|img)\b[^>]*\b(?:href|src)="https?://', text))

    def test_the_failed_state_names_no_machine_and_keeps_the_account_actions(self):
        _row, text = self.page("failed")
        self.assertIn(D12, flat(text))
        self.assertIn(D16, flat(text))
        self.assertIn('href="/forgot"', text)
        self.assertNotIn("first-visit/reset", text)

    def test_the_missing_page_carries_no_account_actions(self):
        email = self.email()
        self.seed_account(email)
        cookie = self.sign_in(email)
        status, _headers, body = request(self.port, "GET", "/account/", cookie=cookie)
        self.assertEqual(status, 404)
        text = body.decode("utf-8")
        self.assertIn("<h1>That page is not here.</h1>", text)
        self.assertNotIn('action="/account/password"', text)
        self.assertNotIn(D16, flat(text))

    def test_the_sections_standalone_links_are_targets_and_its_rhythm_is_forgots(self):
        _row, text = self.page("ready")
        # Each reset link is the whole content of its paragraph, so the sheet's
        # 44 x 44 floor applies and the section carries the same rule /forgot
        # uses for its standalone links (Brand gate round 2, must 2).
        self.assertRegex(
            text,
            r'<p class="note next"><a href="/forgot">Forgot it\? Reset it with '
            r'your recovery code</a></p>')
        self.assertRegex(
            text,
            r'<p class="note"><a href="https://[^"]+/first-visit/reset">'
            r'Reset the sign-in on your machine</a></p>')
        link_rule = css_declarations(text, ".account p.note a")
        self.assertIn("display:inline-flex", link_rule)
        self.assertIn("align-items:center", link_rule)
        self.assertIn("min-height:44px", link_rule)
        # The label-to-field step is the sheet's 8 px here, as on /forgot:
        # gap 16 px minus the label's -8 px margin (Brand gate round 2,
        # should 4).
        self.assertIn("margin-bottom:-8px", css_declarations(text, ".account label"))
        # No field tracking: the sheet tracks capitals, and a field is a value.
        rule = css_declarations(text, ".account input[type=password]")
        self.assertFalse([d for d in rule if d.startswith("letter-spacing")],
                         "the account field carries letter-spacing")


class AccountRefusalPageTests(ResetBase):
    """The account page's own 429 is the finished page, not a bare body.

    The bucket is spent before the current password is verified, so a person
    reaches this by mistyping it a few times — the account-vs-machine
    confusion D-12 exists for. With the per-IP budget lowered to one press,
    the second POST is the rate limit; no timing loop and two requests
    (Brand gate round 2, must 1).
    """

    RATE_LIMIT = "1"

    def test_the_rate_limited_change_answers_the_account_page(self):
        email = self.email()
        self.seed_account(email)
        cookie = self.sign_in(email)
        ip = "203.0.113.201"
        fields = form(current="not the password", password=NEW_SIGN_IN,
                      password2=NEW_SIGN_IN)
        first = request(self.port, "POST", "/account/password", fields,
                        cookie=cookie, headers={"X-Forwarded-For": ip})
        self.assertEqual(first[0], 422, first[2][:300])
        status, headers, body = request(
            self.port, "POST", "/account/password", fields,
            cookie=cookie, headers={"X-Forwarded-For": ip})
        self.assertEqual(status, 429, body[:300])
        self.assertEqual(headers.get("Retry-After"), str(app.RATE_WINDOW))
        self.assertTrue(headers.get("Content-Type", "").startswith("text/html"))
        text = body.decode("utf-8")
        self.assertIn("<title>", text)
        self.assertIn("<h1>Your machine is ready.</h1>", text)
        self.assertIn('id="account-password-err"', text)
        self.assertIn('role="alert"', text)
        self.assertIn("Too many changes. Try again in ten minutes.", text)
        # The page is a way forward, not a dead end: the form and both resets.
        self.assertIn('action="/account/password"', text)
        self.assertIn('href="/forgot"', text)
        self.assertNotIn("{{", text)

    def test_the_rate_limited_resend_answers_the_account_page(self):
        email = self.email()
        row = self.seed_account(email)
        connection = self.connect()
        try:
            connection.execute(
                "UPDATE accounts SET provision_state = 'building' WHERE id = ?",
                (int(row["id"]),))
            connection.commit()
        finally:
            connection.close()
        cookie = self.sign_in(email)
        ip = "203.0.113.202"
        first = request(self.port, "POST", "/account/resend", body="",
                        cookie=cookie, headers={"X-Forwarded-For": ip})
        self.assertEqual(first[0], 303, first[2][:300])
        status, headers, body = request(
            self.port, "POST", "/account/resend", body="",
            cookie=cookie, headers={"X-Forwarded-For": ip})
        self.assertEqual(status, 429, body[:300])
        self.assertEqual(headers.get("Retry-After"), str(app.RATE_WINDOW))
        self.assertTrue(headers.get("Content-Type", "").startswith("text/html"))
        text = body.decode("utf-8")
        self.assertIn("<h1>Building.</h1>", text)
        self.assertIn('id="account-password-err"', text)
        self.assertIn("Too many resends. Try again in ten minutes.", text)
        self.assertNotIn("{{", text)


if __name__ == "__main__":
    unittest.main()
