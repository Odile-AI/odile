#!/usr/bin/python3
"""End-to-end checks for the seed service.

Starts the real service against a throwaway database and a throwaway Ed25519
key, then speaks HTTP to it and verifies signatures with the same OpenSSL the
installer will use. Standard library only, like the service.

ODL-455 adds the provision side: a run of the real `odilectl provision`
command, against a recorded provider transport shaped like the live create
response in `docs/runbooks/provision.md` and the real seed service, proves the
token is bound to the create response's instance id before the first poll — the
machine is still deploying. The same class then
makes the refusal runs (another instance, a second use) and the claim fallback.

    /usr/bin/python3 -m unittest discover -s tests -v

The evidence transcript is:

    /usr/bin/python3 tests/test_seed.py --transcript
"""
import base64
import email
from email import policy as email_policy
import hashlib
import http.client
import importlib.util
import io
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
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED = os.path.join(ROOT, "seed", "seed.py")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from odilectl import claim as claim_module  # noqa: E402
from odilectl import cli as odilectl_cli  # noqa: E402
from odilectl.config import Config  # noqa: E402
from odilectl.providers.base import Machine, Provider  # noqa: E402
from odilectl.providers.servury import ServuryProvider  # noqa: E402
from tests import box_fixture  # noqa: E402

EXPECTED_FIELDS = {
    "slug", "instance_id", "near_api_key", "model", "support_public_key",
    "backup_repo_url", "mail_relay", "hermes_version",
    # ODL-514: the seed signs with its own key and carries the certificate
    # that chains that key to the release key.
    "signing_key_sha256", "signing_key", "signing_key_cert",
}

# A real ed25519 public key line; the measured `bb.key=` is its base64.
BOOT_KEY_TEXT = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICFh7hQkST5vRDolO3yqwCARRugZahMdc+WgR69JsoXG "
    "odile-test throwaway boot key"
)
BOOT_KEY_B64 = base64.b64encode(BOOT_KEY_TEXT.encode()).decode()


def read_ready_body(path: str | Path) -> str:
    """The decoded text/plain body of a queued ready message.

    The raw .eml is quoted-printable, so a long URL is soft-wrapped on disk;
    the reader decodes what a mail client shows.
    """
    message = email.message_from_bytes(
        Path(path).read_bytes(), policy=email_policy.default
    )
    part = message.get_body(preferencelist=("plain",))
    if part is None:
        raise AssertionError("the ready message has no text/plain body")
    return part.get_content()

# A PEM is what the relay's self-signed certificate is; seed.py only checks
# the markers, so a short synthetic body keeps the fixture readable.
RELAY_CA = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIBhTCCASugAwIBAgIUTESTRELAYCA0000000000000000\n"
    "-----END CERTIFICATE-----\n"
)
# One name for the fixture's relay SASL credential: `secret="…"` inline is a
# credential-shaped assignment the tree scan flags, so the fixture names it here.
RELAY_SASL = "relay-credential-0001"

CONFIG = {
    "near_api_key": "near-test-key-0001",
    "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
    "support_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITEST support@odile.fyi",
    "backup_repo_url": "sftp:odile-backup@backup.odile.fyi:/tenants/{slug}",
    "hermes_version": "2026.11.1",
    "mail_relay": {
        "host": "mail.odile.fyi",
        "port": 587,
        "username": "{slug}@mail.odile.fyi",
        "secret": "relay-credential-0001",
        # ODL-469: the relay's certificate is self-signed, so the signed seed
        # carries the PEM the tenant pins until the relay has a public chain.
        "tls_ca": RELAY_CA,
    },
}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def seed_certificate(directory: str, seed_key: str) -> tuple[str, str, str]:
    """A release key pair and its certificate over `seed_key`'s public half.

    ODL-514: the service signs seeds with the seed key; the release key (a
    different key, held offline in production) signs the certificate that the
    installer checks. Returns (release key, release public key, certificate).
    """
    release_key = os.path.join(directory, "release-signing.pem")
    release_pub = release_key + ".pub"
    certificate = os.path.join(directory, "seed-key.crt")
    subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", release_key],
                   check=True, capture_output=True)
    os.chmod(release_key, 0o600)
    subprocess.run(["openssl", "pkey", "-in", release_key, "-pubout", "-out", release_pub],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "dgst", "-sign", release_key, "-out", certificate,
                    seed_key + ".pub"], check=True, capture_output=True)
    os.chmod(certificate, 0o644)
    return release_key, release_pub, certificate


def iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def now() -> datetime:
    return datetime.now(timezone.utc)


def wait_health(port: int, proc, seconds: float = 10.0) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=1).status == 200:
                return
        except OSError:
            time.sleep(0.05)
    out = b""
    if proc.poll() is not None:
        out = proc.communicate(timeout=5)[0] or b""
    raise RuntimeError("seed service did not start:\n" + out.decode("utf-8", "replace"))


class TokenVerbSurface(unittest.TestCase):
    """The `token` verb set, pinned because a record named a verb that is not one.

    SPIKE-servury-reinstall §5 said `token issue`/`rotate`/`bind` mint and print
    a token; corrected 2026-11-21, `bind` is a module function
    (`seed/seed.py:862`) and these three are the parser's whole verb set. A
    `token bind` added later fails here, and the commit that adds it retires
    §5's correction (`docs/product/SPIKE-servury-reinstall.md`) in the same
    change — the record is dated, so it keeps the sentence that was wrong.
    """

    @staticmethod
    def run_seed(*args):
        return subprocess.run(
            [sys.executable, SEED, *args], capture_output=True, text=True, timeout=30
        )

    def test_bind_is_not_a_token_verb(self):
        result = self.run_seed("token", "bind", "--tenant", "odl-alpha")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("invalid choice", result.stderr)
        self.assertIn("'bind'", result.stderr)
        # argparse prints the choices it would have accepted: the whole set.
        self.assertIn("{issue,rotate,list}", result.stderr)

    def test_the_three_token_verbs_parse(self):
        for verb in ("issue", "rotate", "list"):
            with self.subTest(verb=verb):
                result = self.run_seed("token", verb, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)


class SeedService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        cls.db = os.path.join(d, "waitlist.db")
        cls.key = os.path.join(d, "seed-signing.pem")
        cls.pub = cls.key + ".pub"
        # ODL-514: the release key signs the certificate over the seed key; it
        # is a different key from the one the service signs seeds with.
        cls.release_key = os.path.join(d, "release-signing.pem")
        cls.release_pub = cls.release_key + ".pub"
        cls.cert = os.path.join(d, "seed-key.crt")
        cls.config = os.path.join(d, "seed-config.json")
        cls.rebuilds = os.path.join(d, "seed-rebuilds.json")
        cls.env = dict(
            os.environ,
            ODILE_DB=cls.db,
            ODILE_SEED_CONFIG=cls.config,
            ODILE_SEED_REBUILDS=cls.rebuilds,
            ODILE_SEED_SIGNING_KEY=cls.key,
            ODILE_SEED_CERT=cls.cert,
            ODILE_SEED_RELEASE_PUBKEY=cls.release_pub,
        )

        # keygen through the real CLI, then lock the file down
        keygen = cls.run_seed("keygen")
        if keygen.returncode != 0:
            raise RuntimeError(keygen.stderr)
        os.chmod(cls.key, 0o600)

        # the release key, and its certificate over the seed public key
        cls.release_key, cls.release_pub, cls.cert = seed_certificate(d, cls.key)

        with open(cls.config, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f)
        os.chmod(cls.config, 0o600)

        init = cls.run_seed("init")
        if init.returncode != 0:
            raise RuntimeError(init.stderr)

        con = sqlite3.connect(cls.db)
        stamp = iso(now())
        for slug, server in (("odl-alpha", "srv-alpha"), ("odl-beta", "srv-beta"),
                             ("odl-rot", "srv-rot"), ("odl-boot", "srv-boot")):
            con.execute(
                "INSERT INTO tenants (slug, email, server_id, created_at) VALUES (?, ?, ?, ?)",
                (slug, slug + "@example.com", server, stamp))
        con.execute(
            "INSERT INTO tenants (slug, email, created_at) VALUES ('odl-noinst', ?, ?)",
            ("noinst@example.com", stamp))
        # The measured boot record writes the tenant spine; the fixture gives
        # the service the same tables and one order at first_boot.
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from odilectl import state as state_module

        con.executescript(state_module.SCHEMA)
        con.execute(
            "INSERT INTO tenant_state (slug, state, server_id, initramfs_host_key,"
            " detail, created_at, updated_at) VALUES (?, 'first_boot', ?, ?, '{}', ?, ?)",
            ("odl-boot", "srv-boot", "SHA256:seed-fixture", stamp, stamp))
        con.commit()
        con.close()

        cls.port = free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, SEED, "serve"],
            env=dict(cls.env, ODILE_SEED_PORT=str(cls.port), ODILE_SEED_BIND="127.0.0.1"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        wait_health(cls.port, cls.proc)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.communicate()
        cls.tmp.cleanup()

    # -- helpers ----------------------------------------------------------

    @classmethod
    def run_seed(cls, *args):
        return subprocess.run(
            [sys.executable, SEED, *args], env=cls.env,
            capture_output=True, text=True, timeout=30,
        )

    def issue(self, tenant="odl-alpha", *extra):
        result = self.run_seed("token", "issue", "--tenant", tenant, *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        match = re.search(r"^token:\s+([0-9a-f]{64})$", result.stdout, re.M)
        self.assertIsNotNone(match, "no token line in:\n" + result.stdout)
        return match.group(1), result

    def post(self, payload=None, raw=None, path="/seed", method="POST"):
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers={"Content-Type": "application/json"})
        res = conn.getresponse()
        data = res.read()
        headers = {key.lower(): value for key, value in res.getheaders()}
        conn.close()
        return res.status, headers, data

    def exchange(self, token, instance):
        return self.post({"token": token, "instance_id": instance})

    def openssl_verify(self, body, signature_b64, pubkey=None):
        with tempfile.TemporaryDirectory() as d:
            msg = os.path.join(d, "msg")
            sig = os.path.join(d, "sig")
            with open(msg, "wb") as f:
                f.write(body)
            with open(sig, "wb") as f:
                f.write(base64.b64decode(signature_b64))
            proc = subprocess.run(
                ["openssl", "dgst", "-verify", pubkey or self.pub, "-signature", sig, msg],
                capture_output=True, text=True,
            )
            return proc.returncode == 0, proc.stdout + proc.stderr

    def signature_of(self, headers):
        value = headers.get("x-odile-signature", "")
        self.assertTrue(value.startswith("ed25519="), value)
        return value.split("=", 1)[1]

    def db_row(self, token):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(
                "SELECT token_hash, instance_id, created_at, expires_at, used_at"
                " FROM seed_tokens WHERE token_hash = ?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
        finally:
            con.close()

    # -- the happy path ---------------------------------------------------

    def test_a_valid_token_returns_the_signed_seed_once(self):
        token, issued = self.issue()
        status, headers, body = self.exchange(token, "srv-alpha")
        self.assertEqual(status, 200, body)
        self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
        self.assertEqual(headers.get("cache-control"), "no-store")
        seed = json.loads(body)
        self.assertEqual(seed["slug"], "odl-alpha")
        self.assertEqual(seed["instance_id"], "srv-alpha")
        self.assertIn("expires:  ", issued.stdout)
        self.assertFalse(seed["slug"] == token, "the token must not be echoed")
        self.assertNotIn(token, body.decode())

        ok, output = self.openssl_verify(body, self.signature_of(headers))
        self.assertTrue(ok, output)
        self.assertIn("Verified OK", output)

        # replay: the same bytes, a different answer
        status, _, body = self.exchange(token, "srv-alpha")
        self.assertEqual(status, 410)
        self.assertEqual(json.loads(body)["reason"], "used")
        self.assertNotIn("slug", json.loads(body))

    def test_the_seed_carries_the_contract_and_no_user_secret(self):
        token, _ = self.issue()
        status, _, body = self.exchange(token, "srv-alpha")
        self.assertEqual(status, 200, body)
        seed = json.loads(body)
        self.assertEqual(set(seed), EXPECTED_FIELDS)
        self.assertEqual(seed["near_api_key"], CONFIG["near_api_key"])
        self.assertEqual(seed["model"], CONFIG["model"])
        self.assertEqual(seed["support_public_key"], CONFIG["support_public_key"])
        self.assertEqual(seed["backup_repo_url"],
                         "sftp:odile-backup@backup.odile.fyi:/tenants/odl-alpha")
        self.assertEqual(seed["hermes_version"], CONFIG["hermes_version"])
        self.assertEqual(seed["mail_relay"], {
            "host": "mail.odile.fyi",
            "port": 587,
            "username": "odl-alpha@mail.odile.fyi",
            "secret": "relay-credential-0001",
            "tls_ca": RELAY_CA,
        })

        raw = body.decode("utf-8").lower()
        for banned in ("password", "passphrase", "private key", "private_key",
                       "luks", "ssh_public_key", "@example.com"):
            self.assertNotIn(banned, raw, "the seed contains %r" % banned)

        def keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)

        for key in keys(seed):
            self.assertNotRegex(key, r"(?i)pass(word|phrase)|private|luks")

    def test_the_seed_carries_the_relay_ca_the_tenant_pins(self):
        """ODL-469: a self-signed relay without its CA in the seed is a silent failure."""
        token, _ = self.issue()
        status, _, body = self.exchange(token, "srv-alpha")
        self.assertEqual(status, 200, body)
        seed = json.loads(body)
        self.assertEqual(seed["mail_relay"]["tls_ca"], RELAY_CA)
        self.assertIn("BEGIN CERTIFICATE", seed["mail_relay"]["tls_ca"])
        # The one per-tenant substitution still reaches the relay block.
        self.assertEqual(seed["mail_relay"]["username"], "odl-alpha@mail.odile.fyi")

    def config_with(self, **relay):
        """A 0600 copy of CONFIG with the relay block replaced."""
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        config = dict(CONFIG, mail_relay=relay)
        path = os.path.join(directory, "seed-config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        os.chmod(path, 0o600)
        return path

    def assert_refused(self, config, needle):
        proc = subprocess.run(
            [sys.executable, SEED, "serve"],
            env=dict(self.env, ODILE_DB=os.path.join(os.path.dirname(config), "w.db"),
                     ODILE_SEED_CONFIG=config, ODILE_SEED_PORT=str(free_port())),
            capture_output=True, text=True, timeout=20,
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(needle, proc.stderr)

    def test_a_config_without_the_relay_ca_refuses_to_start(self):
        self.assert_refused(self.config_with(
            host="mail.odile.fyi", port=587,
            username="{slug}@mail.odile.fyi", secret=RELAY_SASL,
        ), "mail_relay.tls_ca")

    def test_a_config_whose_relay_ca_is_not_a_pem_refuses_to_start(self):
        self.assert_refused(self.config_with(
            host="mail.odile.fyi", port=587,
            username="{slug}@mail.odile.fyi", secret=RELAY_SASL,
            tls_ca="not a certificate",
        ), "mail_relay.tls_ca")

    # -- refusals ---------------------------------------------------------

    def test_an_expired_token_returns_410(self):
        token, _ = self.issue()
        past = iso(now() - timedelta(hours=25))
        created = iso(now() - timedelta(hours=49))
        con = sqlite3.connect(self.db)
        try:
            con.execute(
                "UPDATE seed_tokens SET created_at = ?, expires_at = ? WHERE token_hash = ?",
                (created, past, hashlib.sha256(token.encode()).hexdigest()))
            con.commit()
        finally:
            con.close()
        status, _, body = self.exchange(token, "srv-alpha")
        self.assertEqual(status, 410)
        self.assertEqual(json.loads(body)["reason"], "expired")

    def test_a_token_for_another_instance_returns_403_and_is_not_consumed(self):
        token, _ = self.issue()
        status, _, body = self.exchange(token, "srv-beta")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["reason"], "wrong_instance")
        # the refusal did not burn the token: the right machine still gets it
        status, _, body = self.exchange(token, "srv-alpha")
        self.assertEqual(status, 200, body)

    def test_an_unknown_token_returns_404(self):
        status, _, body = self.exchange("ab" * 32, "srv-alpha")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["reason"], "unknown_token")

    def test_malformed_requests_return_400(self):
        cases = [
            b"not json",
            b"{}",
            json.dumps({"token": "NOT-HEX", "instance_id": "srv-alpha"}).encode(),
            json.dumps({"token": "ab" * 32, "instance_id": "bad instance!"}).encode(),
        ]
        for raw in cases:
            status, _, body = self.post(raw=raw)
            self.assertEqual(status, 400, (raw, body))
            self.assertIn("reason", json.loads(body))
        status, _, body = self.post(raw=b"")
        self.assertEqual(status, 400)

    def test_get_and_unknown_paths_are_refused(self):
        status, _, _ = self.post(path="/seed", method="GET")
        self.assertEqual(status, 405)
        status, _, _ = self.post(path="/nope", method="GET")
        self.assertEqual(status, 404)
        status, _, _ = self.post(payload={}, path="/nope")
        self.assertEqual(status, 404)
        status, _, body = self.post(raw=b"x" * 5000)
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["reason"], "too_large")

    # -- the token ledger -------------------------------------------------

    def test_tokens_are_stored_as_hashes_only(self):
        token, _ = self.issue()
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.assertEqual(len(digest), 64)
        row = self.db_row(token)
        self.assertIsNotNone(row)
        self.assertEqual(row[0], digest)
        con = sqlite3.connect(self.db)
        try:
            plaintext = con.execute(
                "SELECT count(*) FROM seed_tokens WHERE token_hash = ?", (token,)).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(plaintext, 0, "the token itself must not be stored")
        self.assertNotEqual(row[0], token)

    def test_a_token_expires_after_24_hours(self):
        token, _ = self.issue()
        row = self.db_row(token)
        created = datetime.strptime(row[2], "%Y-%m-%dT%H:%M:%SZ")
        expires = datetime.strptime(row[3], "%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(expires - created, timedelta(hours=24))

    def test_two_tokens_are_32_random_bytes_and_differ(self):
        first, _ = self.issue()
        second, _ = self.issue()
        self.assertEqual(len(first), 64)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, second)
        self.assertNotEqual(self.db_row(first)[0], self.db_row(second)[0])

    def test_issue_requires_a_bound_instance(self):
        result = self.run_seed("token", "issue", "--tenant", "odl-noinst")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("server_id", result.stderr)
        result = self.run_seed("token", "issue", "--tenant", "odl-noinst",
                               "--instance", "srv-manual")
        self.assertEqual(result.returncode, 0, result.stderr)
        token = re.search(r"^token:\s+([0-9a-f]{64})$", result.stdout, re.M).group(1)
        self.assertEqual(self.db_row(token)[1], "srv-manual")

    def test_rotation_revokes_the_previous_token_and_lists_no_plaintext(self):
        first, _ = self.issue("odl-rot")
        dry = self.run_seed("token", "rotate", "--tenant", "odl-rot", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("would revoke: 1", dry.stdout)
        self.assertIsNone(self.db_row(first)[4], "a dry run must change nothing")
        listing = self.run_seed("token", "list", "--tenant", "odl-rot")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn("live", listing.stdout)
        self.assertIsNone(re.search(r"[0-9a-f]{64}", listing.stdout),
                          "token list must not print a token")

        rotated = self.run_seed("token", "rotate", "--tenant", "odl-rot")
        self.assertEqual(rotated.returncode, 0, rotated.stderr)
        self.assertIn("revoked:  1", rotated.stdout)
        second = re.search(r"^token:\s+([0-9a-f]{64})$", rotated.stdout, re.M).group(1)

        # The ledger shows the old token ended and the new one live — and
        # still never prints either token.
        listing = self.run_seed("token", "list", "--tenant", "odl-rot")
        self.assertEqual(listing.stdout.count("ended"), 1, listing.stdout)
        self.assertEqual(listing.stdout.count("live"), 1, listing.stdout)
        self.assertIsNone(re.search(r"[0-9a-f]{64}", listing.stdout))

        status, _, body = self.exchange(first, "srv-rot")
        self.assertEqual(status, 410, body)
        status, _, body = self.exchange(second, "srv-rot")
        self.assertEqual(status, 200, body)

    def test_concurrent_redemption_yields_exactly_one_seed(self):
        token, _ = self.issue()
        results = []
        lock = threading.Lock()

        def hit():
            status, _, _ = self.exchange(token, "srv-alpha")
            with lock:
                results.append(status)

        threads = [threading.Thread(target=hit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(sorted(results), [200] + [410] * 7, results)

    # -- signing and boot -------------------------------------------------

    def test_the_installer_can_verify_and_tampering_is_caught(self):
        token, _ = self.issue()
        status, headers, body = self.exchange(token, "srv-alpha")
        self.assertEqual(status, 200)
        signature = self.signature_of(headers)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "seed.json")
            with open(path, "wb") as f:
                f.write(body)
            good = self.run_seed("verify", "--pubkey", self.release_pub, "--sig", signature,
                                 "--body", path)
            self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
            self.assertIn("signature: OK", good.stdout)
            self.assertIn("certified by", good.stdout)

            with open(path, "wb") as f:
                f.write(body.replace(b'"slug":"odl-alpha"', b'"slug":"odl-evil"'))
            bad = self.run_seed("verify", "--pubkey", self.release_pub, "--sig", signature,
                                "--body", path)
            self.assertEqual(bad.returncode, 1)
            self.assertIn("signature: BAD", bad.stderr)

            wrong_key = os.path.join(d, "other.pem")
            subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", wrong_key],
                           check=True, capture_output=True)
            wrong_pub = os.path.join(d, "other.pub")
            subprocess.run(["openssl", "pkey", "-in", wrong_key, "-pubout", "-out", wrong_pub],
                           check=True, capture_output=True)
            ok, _ = self.openssl_verify(body, signature, pubkey=wrong_pub)
            self.assertFalse(ok)

    def test_the_seed_carries_the_seed_key_and_its_certificate(self):
        """ODL-514: the body names the seed key and carries the release key's certificate."""
        token, _ = self.issue()
        status, headers, body = self.exchange(token, "srv-alpha")
        self.assertEqual(status, 200)
        seed = json.loads(body)
        with open(self.pub, "rb") as handle:
            public = handle.read()
        self.assertEqual(seed["signing_key_sha256"], hashlib.sha256(public).hexdigest())
        self.assertEqual(seed["signing_key"].encode("ascii"), public)
        certificate = base64.b64decode(seed["signing_key_cert"], validate=True)
        self.assertEqual(len(certificate), 64)
        signature = base64.b64decode(self.signature_of(headers), validate=True)
        with tempfile.TemporaryDirectory() as d:
            cert_file = os.path.join(d, "cert")
            sig_file = os.path.join(d, "sig")
            body_file = os.path.join(d, "body.json")
            with open(cert_file, "wb") as handle:
                handle.write(certificate)
            with open(sig_file, "wb") as handle:
                handle.write(signature)
            with open(body_file, "wb") as handle:
                handle.write(body)
            certified = subprocess.run(
                ["openssl", "dgst", "-verify", self.release_pub, "-signature", cert_file, self.pub],
                capture_output=True, text=True)
            self.assertIn("Verified OK", certified.stdout,
                          "the certificate is the release key's signature over the seed key")
            # The seed body is signed by the *seed* key, not by the release key.
            direct = subprocess.run(
                ["openssl", "dgst", "-verify", self.release_pub, "-signature", sig_file, body_file],
                capture_output=True, text=True)
            self.assertNotIn("Verified OK", direct.stdout,
                             "the release key no longer signs seed bodies directly")
            seed_key_file = os.path.join(d, "seed.pub")
            shutil.copy(self.pub, seed_key_file)
            by_seed = subprocess.run(
                ["openssl", "dgst", "-verify", seed_key_file, "-signature", sig_file, body_file],
                capture_output=True, text=True)
            self.assertIn("Verified OK", by_seed.stdout)

    def test_verify_refuses_a_certificate_from_a_third_key(self):
        """A certificate nobody in the chain signed is refused, on one line."""
        token, _ = self.issue()
        status, headers, body = self.exchange(token, "srv-alpha")
        self.assertEqual(status, 200)
        signature = self.signature_of(headers)
        with tempfile.TemporaryDirectory() as d:
            third_key = os.path.join(d, "third.pem")
            subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", third_key],
                           check=True, capture_output=True)
            os.chmod(third_key, 0o600)
            forged_cert = os.path.join(d, "third.crt")
            subprocess.run(["openssl", "dgst", "-sign", third_key, "-out", forged_cert, self.pub],
                           check=True, capture_output=True)
            document = json.loads(body)
            with open(forged_cert, "rb") as handle:
                document["signing_key_cert"] = base64.b64encode(handle.read()).decode("ascii")
            forged = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
            path = os.path.join(d, "forged.json")
            with open(path, "wb") as handle:
                handle.write(forged)
            result = self.run_seed("verify", "--pubkey", self.release_pub, "--sig", signature,
                                   "--body", path)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            lines = [line for line in result.stderr.splitlines() if line.strip()]
            self.assertEqual(len(lines), 1, result.stderr)
            self.assertIn("was not signed by", lines[0])

    def test_a_seed_signed_by_the_release_key_still_verifies(self):
        """A seed issued before ODL-514 has no certificate and still verifies."""
        token, _ = self.issue()
        status, headers, body = self.exchange(token, "srv-alpha")
        self.assertEqual(status, 200)
        document = json.loads(body)
        for field in ("signing_key_sha256", "signing_key", "signing_key_cert"):
            document.pop(field, None)
        legacy = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "legacy.json")
            with open(path, "wb") as handle:
                handle.write(legacy)
            signature_path = os.path.join(d, "legacy.sig")
            subprocess.run(["openssl", "dgst", "-sign", self.release_key, "-out", signature_path,
                            path], check=True, capture_output=True)
            with open(signature_path, "rb") as handle:
                encoded = base64.b64encode(handle.read()).decode("ascii")
            result = self.run_seed("verify", "--pubkey", self.release_pub, "--sig", encoded,
                                   "--body", path)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("signature: OK", result.stdout)
            self.assertNotIn("certified by", result.stdout,
                             "the legacy path verifies against the release key itself")
            # and a legacy body signed by the seed key is not accepted there
            wrong = os.path.join(d, "wrong.sig")
            subprocess.run(["openssl", "dgst", "-sign", self.key, "-out", wrong, path],
                           check=True, capture_output=True)
            with open(wrong, "rb") as handle:
                encoded_wrong = base64.b64encode(handle.read()).decode("ascii")
            refused = self.run_seed("verify", "--pubkey", self.release_pub, "--sig", encoded_wrong,
                                    "--body", path)
            self.assertEqual(refused.returncode, 1)

    def test_a_service_whose_certificate_certifies_another_key_refuses_to_start(self):
        """ODL-514: the service refuses to sign seeds nobody can verify."""
        with tempfile.TemporaryDirectory() as d:
            key = os.path.join(d, "seed-signing.pem")
            gen = subprocess.run([sys.executable, SEED, "keygen", "--out", key, "--pub", key + ".pub"],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(gen.returncode, 0, gen.stderr)
            os.chmod(key, 0o600)
            other = os.path.join(d, "other.pem")
            subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", other],
                           check=True, capture_output=True)
            os.chmod(other, 0o600)
            cert = os.path.join(d, "seed-key.crt")
            subprocess.run(["openssl", "dgst", "-sign", other, "-out", cert, key + ".pub"],
                           check=True, capture_output=True)
            os.chmod(cert, 0o644)
            config = os.path.join(d, "config.json")
            with open(config, "w", encoding="utf-8") as handle:
                json.dump(CONFIG, handle)
            os.chmod(config, 0o600)
            proc = subprocess.run(
                [sys.executable, SEED, "serve"],
                env=dict(os.environ, ODILE_DB=os.path.join(d, "w.db"), ODILE_SEED_CONFIG=config,
                         ODILE_SEED_SIGNING_KEY=key, ODILE_SEED_CERT=cert,
                         ODILE_SEED_RELEASE_PUBKEY=key + ".pub",
                         ODILE_SEED_PORT=str(free_port())),
                capture_output=True, text=True, timeout=20,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("make seed-cert", proc.stderr)

    def test_a_fresh_database_is_created_on_boot(self):
        with tempfile.TemporaryDirectory() as d:
            port = free_port()
            proc = subprocess.Popen(
                [sys.executable, SEED, "serve"],
                env=dict(self.env, ODILE_DB=os.path.join(d, "fresh.db"),
                         ODILE_SEED_PORT=str(port), ODILE_SEED_BIND="127.0.0.1"),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            try:
                wait_health(port, proc)
                con = sqlite3.connect(os.path.join(d, "fresh.db"))
                try:
                    tables = {row[0] for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'")}
                    count = con.execute("SELECT count(*) FROM seed_tokens").fetchone()[0]
                finally:
                    con.close()
            finally:
                proc.terminate()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
        self.assertIn("seed_tokens", tables)
        self.assertIn("tenants", tables)
        self.assertEqual(count, 0)

    def test_a_world_readable_config_or_key_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as d:
            config = os.path.join(d, "config.json")
            shutil.copy(self.config, config)
            os.chmod(config, 0o644)
            proc = subprocess.run(
                [sys.executable, SEED, "serve"],
                env=dict(self.env, ODILE_SEED_CONFIG=config,
                         ODILE_SEED_PORT=str(free_port())),
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("chmod 600", proc.stderr)

            key = os.path.join(d, "key.pem")
            shutil.copy(self.key, key)
            os.chmod(key, 0o644)
            proc = subprocess.run(
                [sys.executable, SEED, "serve"],
                env=dict(self.env, ODILE_SEED_SIGNING_KEY=key,
                         ODILE_SEED_PORT=str(free_port())),
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("chmod 600", proc.stderr)

    def test_health_is_plain_and_uncached_enough_to_probe(self):
        status, headers, body = self.post(path="/health", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok\n")
        self.assertEqual(headers.get("content-type"), "text/plain; charset=utf-8")


    # -- the measured boot record -----------------------------------------

    def measured_line(self, instance="srv-boot", host="odl-boot", key=None) -> str:
        return (
            "root=/dev/mapper/root_crypt ro console=ttyS0 "
            f"bb.host={host} bb.iid={instance} bb.key={key or BOOT_KEY_B64}"
        )

    def boot_record(self, token, instance="srv-boot", **overrides):
        value = {
            "cmdline": self.measured_line(instance=instance),
            "release": "2026.11.1",
            "first_visit": "done",
            "serving": "200",
        }
        value.update(overrides)
        return self.post({"token": token, "instance_id": instance, "record": value})

    def boot_rows(self):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            tenant = con.execute(
                "SELECT state, ssh_public_key, release FROM tenants WHERE slug = 'odl-boot'"
            ).fetchone()
            spine = con.execute(
                "SELECT state, release, initramfs_host_key, detail FROM tenant_state"
                " WHERE slug = 'odl-boot'"
            ).fetchone()
            events = con.execute(
                "SELECT count(*) FROM tenant_state_events WHERE slug = 'odl-boot'"
            ).fetchone()[0]
        finally:
            con.close()
        return tenant, spine, events

    def boot_row(self, slug: str) -> sqlite3.Row:
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(
                "SELECT detail, updated_at FROM tenant_state WHERE slug = ?", (slug,)
            ).fetchone()
        finally:
            con.close()

    def pin_tenant(self, slug: str, instance: str) -> None:
        """A tenant at `first_boot` with its own spine row, for a pinned replay."""
        stamp = iso(now())
        con = sqlite3.connect(self.db)
        try:
            con.execute(
                "INSERT INTO tenants (slug, email, server_id, created_at)"
                " VALUES (?, ?, ?, ?)",
                (slug, slug + "@example.com", instance, stamp))
            con.execute(
                "INSERT INTO tenant_state (slug, state, server_id, detail,"
                " created_at, updated_at) VALUES (?, 'first_boot', ?, '{}', ?, ?)",
                (slug, instance, stamp, stamp))
            con.commit()
        finally:
            con.close()

    def test_a_record_before_the_seed_is_refused(self):
        token, _ = self.issue("odl-boot")
        status, _, body = self.boot_record(token)
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "not_redeemed")

    def test_a_rebuild_secret_travels_signed_and_only_with_its_own_token(self):
        seed_module = load_seed_module()
        token, _ = self.issue("odl-alpha")
        entry = seed_module.seal_backup_password(REBUILD_PASSWORD, token)
        entry["token_hash"] = hashlib.sha256(token.encode()).hexdigest()
        entry["created_at"] = iso(now())
        seed_module.set_rebuild(self.rebuilds, "odl-alpha", entry)
        self.addCleanup(seed_module.set_rebuild, self.rebuilds, "odl-alpha", None)
        self.assertNotIn(REBUILD_PASSWORD, Path(self.rebuilds).read_text(encoding="utf-8"))

        status, headers, body = self.exchange(token, "srv-alpha")
        self.assertEqual(status, 200, body)
        parsed = json.loads(body)
        self.assertIn("rebuild", parsed)
        self.assertEqual(load_firstboot_module().unseal_rebuild(parsed["rebuild"], token),
                         REBUILD_PASSWORD)
        signature = headers["x-odile-signature"].split("=", 1)[1]
        self.assertTrue(self.openssl_verify(body, signature))

        other, _ = self.issue("odl-beta")
        status, _, body = self.exchange(other, "srv-beta")
        self.assertEqual(status, 200, body)
        self.assertNotIn("rebuild", json.loads(body))

    def test_a_rebuild_secret_keeps_the_rest_of_the_seed_contract(self):
        seed_module = load_seed_module()
        token, _ = self.issue("odl-alpha")
        entry = seed_module.seal_backup_password(REBUILD_PASSWORD, token)
        entry["token_hash"] = hashlib.sha256(token.encode()).hexdigest()
        seed_module.set_rebuild(self.rebuilds, "odl-alpha", entry)
        self.addCleanup(seed_module.set_rebuild, self.rebuilds, "odl-alpha", None)
        status, _, body = self.exchange(token, "srv-alpha")
        self.assertEqual(status, 200, body)
        parsed = json.loads(body)
        self.assertEqual(EXPECTED_FIELDS | {"rebuild"}, set(parsed))
        self.assertEqual(set(parsed["rebuild"]), {"kdf", "sealed", "tag"})

    def test_the_record_after_the_seed_writes_the_measured_row_once(self):
        token, _ = self.issue("odl-boot")
        status, _, body = self.exchange(token, "srv-boot")
        self.assertEqual(status, 200, body)
        status, _, body = self.boot_record(token)
        self.assertEqual(status, 200, body)
        answer = json.loads(body)
        self.assertTrue(answer["recorded"])
        self.assertEqual(answer["slug"], "odl-boot")
        self.assertEqual(answer["state"], "ready")
        self.assertEqual(answer["release"], "2026.11.1")
        self.assertTrue(answer["boot_key_fingerprint"].startswith("SHA256:"))
        tenant, spine, events = self.boot_rows()
        self.assertEqual(tenant["ssh_public_key"], BOOT_KEY_TEXT)
        self.assertEqual(tenant["release"], "2026.11.1")
        self.assertEqual(tenant["state"], "active")
        self.assertEqual(spine["state"], "ready")
        self.assertEqual(spine["release"], "2026.11.1")
        self.assertIsNone(spine["initramfs_host_key"])
        detail = json.loads(spine["detail"])
        self.assertEqual(detail["boot_key"], BOOT_KEY_TEXT)
        self.assertEqual(detail["first_visit"], "done")
        self.assertIn("cmdline_sha256", detail)
        # A reboot sends the same record again; it writes no second event and
        # no second row: every detail key, `recorded_at` included, is unchanged.
        status, _, body = self.boot_record(token)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["changed"], [])
        again = self.boot_rows()
        self.assertEqual(again[2], events)
        before = json.loads(spine["detail"])
        self.assertRegex(before["recorded_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(json.loads(again[1]["detail"]), before)

    def test_a_replayed_record_keeps_the_first_recorded_at(self):
        """The row's stamp is the first record's, with the writer's clock pinned.

        Both forms: a record that carries its own `at` (the operator's repair
        path) and one that does not (the guest's boot report). The clock moves
        between the two calls, so a per-call key would show up as a changed
        `detail` — the replay must write nothing at all.
        """
        seed_module = load_seed_module()
        first = now().replace(microsecond=0)
        later = first + timedelta(minutes=5)
        # slug, instance, the record's own `at`, and the clock of the first call
        for slug, instance, own_at, first_now in (
            ("odl-pin", "srv-pin", None, first),
            ("odl-pinat", "srv-pinat", iso(first), later),
        ):
            self.pin_tenant(slug, instance)
            token, _ = self.issue(slug)
            status, _, body = self.exchange(token, instance)
            self.assertEqual(status, 200, body)
            record = {
                "cmdline": self.measured_line(instance=instance, host=slug),
                "release": "2026.11.1",
                "first_visit": "done",
                "serving": "200",
            }
            if own_at:
                record["at"] = own_at
            con = sqlite3.connect(self.db)
            con.row_factory = sqlite3.Row
            try:
                seed_module.record_for_token(con, token, instance, record, now=first_now)
                before = self.boot_row(slug)
                replay = dict(record)
                if own_at:
                    replay["at"] = iso(later)
                seed_module.record_for_token(con, token, instance, replay,
                                             now=later + timedelta(minutes=5))
                after = self.boot_row(slug)
            finally:
                con.close()
            self.assertEqual(json.loads(after["detail"])["recorded_at"],
                             iso(first), slug)
            self.assertEqual(after["detail"], before["detail"], slug)
            self.assertEqual(after["updated_at"], before["updated_at"], slug)

    def test_a_record_for_another_instance_is_refused(self):
        token, _ = self.issue("odl-boot")
        self.exchange(token, "srv-boot")
        status, _, body = self.boot_record(token, instance="srv-other")
        self.assertEqual(status, 403, body)
        self.assertEqual(json.loads(body)["reason"], "wrong_instance")

    def test_an_unknown_or_expired_token_is_refused(self):
        status, _, body = self.post({
            "token": "ab" * 32, "instance_id": "srv-boot",
            "record": {"cmdline": self.measured_line(), "release": "2026.11.1",
                       "first_visit": "done"},
        })
        self.assertEqual(status, 404, body)
        token, _ = self.issue("odl-boot")
        self.exchange(token, "srv-boot")
        con = sqlite3.connect(self.db)
        try:
            con.execute(
                "UPDATE seed_tokens SET created_at = ?, expires_at = ? WHERE token_hash = ?",
                (iso(now() - timedelta(hours=25)), iso(now() - timedelta(hours=1)),
                 hashlib.sha256(token.encode()).hexdigest()))
            con.commit()
        finally:
            con.close()
        status, _, body = self.boot_record(token)
        self.assertEqual(status, 410, body)
        self.assertEqual(json.loads(body)["reason"], "expired")

    def test_a_malformed_record_is_refused_and_writes_nothing(self):
        token, _ = self.issue("odl-boot")
        self.exchange(token, "srv-boot")
        before = self.boot_rows()
        for record in (
            {"cmdline": "no key here", "release": "2026.11.1", "first_visit": "done"},
            {"cmdline": self.measured_line(), "release": "nope", "first_visit": "done"},
            {"cmdline": self.measured_line(), "release": "2026.11.1", "first_visit": "maybe"},
            {"cmdline": self.measured_line(host="odl-other"), "release": "2026.11.1",
             "first_visit": "done"},
            {"cmdline": self.measured_line(), "release": "2026.11.1",
             "first_visit": "done", "password": "hunter2"},
        ):
            status, _, body = self.post({
                "token": token, "instance_id": "srv-boot", "record": record})
            self.assertEqual(status, 400, (record, body))
            self.assertEqual(json.loads(body)["reason"], "bad_record")
        self.assertEqual(self.boot_rows(), before)


class DefaultSigningKey(unittest.TestCase):
    """The default matters when neither the unit nor a hand-run sets a path."""

    def test_with_no_environment_or_credential_the_default_is_the_seed_key(self):
        env = {key: value for key, value in os.environ.items()
               if key not in ("ODILE_SEED_SIGNING_KEY", "CREDENTIALS_DIRECTORY")}
        probe = (
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location('seed_under_test', %r)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "print(module.signing_key_path())\n" % SEED
        )
        result = subprocess.run([sys.executable, "-c", probe], env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "/root/.odile/seed-signing.pem")

    def test_the_seed_key_is_not_the_release_key(self):
        """ODL-514: the two keys are different files, and the installer's key is not the seed's."""
        module = load_seed_module()
        self.assertEqual(module.DEFAULT_SIGNING_KEY, "/root/.odile/seed-signing.pem")
        self.assertNotEqual(module.DEFAULT_SIGNING_KEY, "/root/.odile/installer-signing.pem")
        self.assertEqual(module.DEFAULT_RELEASE_PUBKEY,
                         os.path.join(ROOT, "installer", "odile-signing-key.pub"))
        unit = Path(os.path.join(ROOT, "deploy", "seed", "odile-seed.service")).read_text(
            encoding="utf-8")
        self.assertIn("LoadCredential=seed-key:/root/.odile/seed-signing.pem", unit)
        self.assertNotIn("installer-signing", unit)


# ----------------------------------------------- provision binds the token (ODL-455)

PROVISION_RELEASE = "2026.11.1"
MACHINE_EXPIRES = 1796918490
TENANT_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICFh7hQkST5vRDolO3yqwCARRugZahMdc+WgR69JsoXG "
    "odile-fixture@example.com"
)


def load_seed_module():
    """Load seed/seed.py as a module so a test can call `bind` directly."""
    spec = importlib.util.spec_from_file_location("seed_under_test_binding", SEED)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load %s" % SEED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def exchange(port: int, token: str, instance: str) -> tuple[int, dict]:
    """POST one token/instance pair to the real seed service; return status+body."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(
        "POST", "/seed",
        body=json.dumps({"token": token, "instance_id": instance}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res.status, json.loads(data)


FIRSTBOOT = os.path.join(ROOT, "deploy", "image", "odile-firstboot.py")


def load_firstboot_module():
    """The guest's half of the rebuild seal, loaded by path as the image runs it."""
    spec = importlib.util.spec_from_file_location("firstboot_under_test_seal", FIRSTBOOT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load %s" % FIRSTBOOT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REBUILD_PASSWORD = "test-rebuild-password-2026-0001"
SEAL_TOKEN = "ab" * 32


class RebuildSeal(unittest.TestCase):
    """ODL-215 follow-up: the old repository password travels sealed under the token.

    The writer is `seed.seal_backup_password` on HQ; the reader is
    `odile-firstboot.unseal_rebuild` in the image. They are two files in two
    trees, so this class runs one against the other: a drift in either is a
    failure here, not a rebuilt machine that meets its old repository with a
    new key.
    """

    def setUp(self) -> None:
        self.seed = load_seed_module()
        self.firstboot = load_firstboot_module()

    def test_the_guest_opens_what_the_service_sealed(self) -> None:
        entry = self.seed.seal_backup_password(REBUILD_PASSWORD, SEAL_TOKEN)
        self.assertEqual(set(entry), {"kdf", "sealed", "tag"})
        self.assertEqual(entry["kdf"], "hmac-sha256-ctr-v1")
        self.assertNotIn(REBUILD_PASSWORD, json.dumps(entry))
        self.assertEqual(self.firstboot.unseal_rebuild(entry, SEAL_TOKEN), REBUILD_PASSWORD)
        self.assertEqual(self.seed.unseal_backup_password(entry, SEAL_TOKEN), REBUILD_PASSWORD)

    def test_another_token_does_not_open_it(self) -> None:
        entry = self.seed.seal_backup_password(REBUILD_PASSWORD, SEAL_TOKEN)
        with self.assertRaisesRegex(self.firstboot.Refusal, "does not open"):
            self.firstboot.unseal_rebuild(entry, "b2" * 32)
        with self.assertRaisesRegex(ValueError, "does not open"):
            self.seed.unseal_backup_password(entry, "b2" * 32)

    def test_one_flipped_byte_fails_the_tag(self) -> None:
        entry = self.seed.seal_backup_password(REBUILD_PASSWORD, SEAL_TOKEN)
        first = "0" if entry["sealed"][0] != "0" else "1"
        tampered = dict(entry, sealed=first + entry["sealed"][1:])
        with self.assertRaisesRegex(self.firstboot.Refusal, "does not open"):
            self.firstboot.unseal_rebuild(tampered, SEAL_TOKEN)
        with self.assertRaisesRegex(ValueError, "does not open"):
            self.seed.unseal_backup_password(tampered, SEAL_TOKEN)

    def test_a_malformed_entry_is_refused_before_any_crypto(self) -> None:
        entry = self.seed.seal_backup_password(REBUILD_PASSWORD, SEAL_TOKEN)
        for broken, reason in (
            (dict(entry, kdf="aes-256-gcm"), "kdf"),
            (dict(entry, sealed=entry["sealed"][:-1]), "sealed"),
            (dict(entry, sealed=entry["sealed"] + "zz"), "hex|sealed"),
            (dict(entry, tag=entry["tag"][:16]), "tag"),
        ):
            with self.assertRaisesRegex(self.firstboot.Refusal, reason):
                self.firstboot.unseal_rebuild(broken, SEAL_TOKEN)

    def test_the_rebuilds_file_refuses_a_plaintext_password_by_name(self) -> None:
        entry = self.seed.seal_backup_password(REBUILD_PASSWORD, SEAL_TOKEN)
        entry["token_hash"] = hashlib.sha256(SEAL_TOKEN.encode()).hexdigest()
        entry["backup_password"] = REBUILD_PASSWORD
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "seed-rebuilds.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"odl-bench": entry}, handle)
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(self.seed.ConfigError, "plaintext backup_password"):
                self.seed.load_rebuilds(path)
            with self.assertRaisesRegex(self.seed.ConfigError, "plaintext backup_password"):
                self.seed.set_rebuild(path, "odl-bench", entry)

    def test_a_stale_entry_does_not_travel_with_a_new_token(self) -> None:
        entry = self.seed.seal_backup_password(REBUILD_PASSWORD, SEAL_TOKEN)
        entry["token_hash"] = hashlib.sha256(SEAL_TOKEN.encode()).hexdigest()
        rebuilds = {"odl-bench": entry}
        self.assertIsNone(self.seed.rebuild_for(rebuilds, "odl-bench", "c3" * 32))
        self.assertIsNone(self.seed.rebuild_for({}, "odl-bench",
                                               hashlib.sha256(SEAL_TOKEN.encode()).hexdigest()))
        served = self.seed.rebuild_for(
            rebuilds, "odl-bench", hashlib.sha256(SEAL_TOKEN.encode()).hexdigest())
        self.assertEqual(set(served), {"kdf", "sealed", "tag"})
        self.assertEqual(self.firstboot.unseal_rebuild(served, SEAL_TOKEN), REBUILD_PASSWORD)

    def test_set_rebuild_writes_ciphertext_and_removes_it(self) -> None:
        entry = self.seed.seal_backup_password(REBUILD_PASSWORD, SEAL_TOKEN)
        entry["token_hash"] = hashlib.sha256(SEAL_TOKEN.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "seed-rebuilds.json")
            self.seed.set_rebuild(path, "odl-bench", entry)
            text = Path(path).read_text(encoding="utf-8")
            self.assertNotIn(REBUILD_PASSWORD, text)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(self.seed.load_rebuilds(path), {"odl-bench": entry})
            # A second run is byte-identical: the write is idempotent.
            self.seed.set_rebuild(path, "odl-bench", entry)
            self.assertEqual(Path(path).read_text(encoding="utf-8"), text)
            self.seed.set_rebuild(path, "odl-bench", None)
            self.assertEqual(self.seed.load_rebuilds(path), {})
            self.assertEqual(Path(path).read_text(encoding="utf-8").strip(), "{}")


class RecordedProvision:
    """A recorded provider transport that checks the ledger at the first poll.

    `?action=create` answers exactly as the live capture in
    `docs/runbooks/provision.md` did: the order returns at once with
    `server_id`, and the machine is still `deploying`. The first
    `?action=server` — the poll, while the machine is being built — reads the
    token ledger before it answers. If the token is not bound by then, the test
    fails at the precise moment ODL-455 cares about.
    """

    def __init__(self, db_path: str, slug: str, server_id: str,
                 ipv4: str = "203.0.113.120", *, listed: bool = False):
        self.db_path = db_path
        self.slug = slug
        self.server_id = server_id
        self.ipv4 = ipv4
        self.calls: list[str] = []
        self.poll_ledger: list[tuple] = []
        self.create_body: dict = {}
        # `?action=servers` lists the machine the account holds. Before the
        # order there is none; once `create` answered (or a previous run did),
        # the real list carries it — the live capture in
        # docs/runbooks/provision.md §1 read `total 2` — so a second provision
        # reuses it instead of ordering again.
        self.listed = listed

    def _machine(self) -> dict:
        return {
            "id": self.server_id,
            "hostname": self.slug,
            "plan_id": 113,
            "plan": "Blackbox Plus",
            "status": "active",
            "ipv4_address": self.ipv4,
            "ipv6_address": "2001:0db8:0:2a::1",
            "powered_on": True,
            "snp_enabled": True,
            "fde_requested": True,
            "measured_boot": True,
            "expiration_date": MACHINE_EXPIRES,
            "expired": False,
        }

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        action = query.get("action", [None])[0]
        self.calls.append(action or "")
        if action == "servers":
            servers = [self._machine()] if self.listed else []
            return 200, json.dumps(
                {"success": True, "data": {"servers": servers, "total": len(servers)}}
            ).encode()
        if action == "create":
            self.create_body = json.loads(body or b"{}")
            self.listed = True
            return 200, json.dumps({"success": True, "data": {
                "message": "Order accepted. The machine is built in the background ...",
                "server_id": self.server_id,
                "hostname": self.slug,
                "status": "deploying",
                "expiration_date": MACHINE_EXPIRES,
                "invoice_id": "fixture-invoice",
                "amount_charged": 18.99,
            }}).encode()
        if action == "server":
            con = sqlite3.connect(self.db_path)
            try:
                self.poll_ledger.append(tuple(con.execute(
                    "SELECT token_hash, instance_id, used_at FROM seed_tokens"
                    " WHERE instance_id = ?", (self.server_id,)).fetchone() or ()))
            finally:
                con.close()
            return 200, json.dumps({"success": True, "data": {"server": self._machine()}}).encode()
        raise AssertionError("no recorded response for %s action=%r" % (method, action))


class FixtureProvider(Provider):
    """In-memory provider for the claim fallback: create, poll, list."""

    name = "fixture"

    def __init__(self, ipv4: str = "203.0.113.120") -> None:
        self.machines: dict[str, Machine] = {}
        self.created = 0
        self.ipv4 = ipv4

    def create(self, spec) -> Machine:
        self.created += 1
        machine = Machine(
            server_id="srv-fixture%04d" % self.created,
            hostname=spec.hostname,
            state="pending",
            plan_id=spec.plan_id,
            expires_at=MACHINE_EXPIRES,
            raw_status="deploying",
        )
        self.machines[machine.server_id] = machine
        return machine

    def poll(self, server_id: str, *, timeout: float = 900.0,
             interval: float = 10.0) -> Machine:
        machine = replace(self.machines[server_id], state="active",
                          ipv4=self.ipv4, raw_status="active")
        self.machines[server_id] = machine
        return machine

    def power(self, server_id: str, action: str) -> Machine:
        raise NotImplementedError("the claim pipeline never powers a machine")

    def terminate(self, server_id: str) -> bool:
        machine = self.machines.get(server_id)
        if machine is None:
            return False
        self.machines[server_id] = replace(machine, state="terminated", ipv4=None)
        return True

    def list(self) -> list[Machine]:
        return list(self.machines.values())


class FakeDNS:
    """The claim worker's DNS calls, in memory; this card is not about DNS."""

    zone = "odile.fyi"

    def __init__(self) -> None:
        self.records: dict[str, str] = {}

    def ensure(self, name: str, ipv4: str, ttl: int, apply: bool = False):
        self.records[name] = ipv4
        return SimpleNamespace(action="created", record=SimpleNamespace(id="rec-fixture"))

    def remove(self, name: str, apply: bool = False):
        self.records.pop(name, None)
        return SimpleNamespace(action="deleted", record=None)


def run_odilectl(argv, config_path, provider_factory):
    out, err = io.StringIO(), io.StringIO()
    code = odilectl_cli.main(
        [*argv, "--config", str(config_path)],
        provider_factory=provider_factory,
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


def _tenant_files(directory: str, slug: str) -> tuple[str, str]:
    """A tenant public key and a release pin (the config is written per test)."""
    key_file = os.path.join(directory, slug + ".pub")
    with open(key_file, "w", encoding="utf-8") as f:
        f.write(TENANT_KEY + "\n")
    pin = os.path.join(directory, slug + "-pin.json")
    with open(pin, "w", encoding="utf-8") as f:
        json.dump({"schema": 2, "measurement": "ab" * 48,
                   "provision_release": PROVISION_RELEASE}, f)
    return key_file, pin


def _odilectl_config(directory: str, slug: str, db: str,
                     seed_rebuilds: str | None = None) -> str:
    """A 0600 odilectl config whose state and seed ledgers are one file."""
    path = os.path.join(directory, slug + "-odilectl.yaml")
    lines = [
        "provider: servury",
        "reseller_key: fixture-reseller-key",
        "default_plan_id: 113",
        "default_days: 31",
        "state_db: " + db,
        "seed_db: " + db,
        # ODL-924: the pairing's four paths, under this test's directory.
        "hetzner_box_id: 709870",
        "hetzner_dir: " + os.path.join(directory, "hetzner"),
        "hetzner_keys: " + os.path.join(directory, "keys"),
        "seed_backup_pairs: " + os.path.join(directory, "seed-backup-pairs.json"),
    ]
    if seed_rebuilds is not None:
        lines.append("seed_rebuilds: " + seed_rebuilds)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    return path


class BackupPairs(unittest.TestCase):
    """ODL-924: one shape for the per-tenant pair the seed service reads.

    The file at `/var/lib/odile/seed-backup-pairs.json` carries both private
    halves, so every rule here is one the guest's first boot depends on. The
    validator is the service's own, run on the way in and on the way out.
    """

    def setUp(self) -> None:
        self.seed = load_seed_module()
        self.slug = "odl-7f3a2c"
        self.entry = {
            "box_id": 709870,
            "box_server": "u728180.your-storagebox.de",
            "subaccount_id": 392550,
            "subaccount_username": "u728180-sub1",
            "subaccount_home": "tenants/%s" % self.slug,
            "readonly_subaccount_id": 392556,
            "readonly_subaccount_username": "u728180-sub2",
            "readonly_subaccount_home": "tenants/%s/data" % self.slug,
            "repo_url": "sftp:u728180-sub1@u728180-sub1.your-storagebox.de:data/restic",
            "readonly_repo_url": "sftp:u728180-sub2@u728180-sub2.your-storagebox.de:restic",
            "ssh_port": 22,
            "known_hosts": ("u728180-sub1.your-storagebox.de ssh-ed25519 "
                            "AAAAC3NzaC1lZDI1NTE5AAAAIFixtureHostKey\n"
                            "u728180-sub2.your-storagebox.de ssh-ed25519 "
                            "AAAAC3NzaC1lZDI1NTE5AAAAIFixtureHostKey"),
            "ssh_private_key": box_fixture.PRIVATE_KEY,
            "readonly_ssh_private_key": box_fixture.PRIVATE_KEY,
            "previous_instance": "",
            "provisioned_at": "2026-11-25T00:00:00Z",
        }

    def test_the_entry_round_trips_at_0600_and_a_rerun_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "seed-backup-pairs.json")
            self.seed.set_backup_pair(path, self.slug, self.entry)
            text = Path(path).read_text(encoding="utf-8")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(self.seed.load_backup_pairs(path), {self.slug: self.entry})
            self.seed.set_backup_pair(path, self.slug, self.entry)
            self.assertEqual(Path(path).read_text(encoding="utf-8"), text)
            self.seed.set_backup_pair(path, self.slug, None)
            self.assertEqual(self.seed.load_backup_pairs(path), {})

    def test_a_plaintext_subaccount_password_is_refused_by_name(self) -> None:
        for plaintext in ("password", "passphrase"):
            with self.assertRaisesRegex(self.seed.ConfigError, "plaintext %s" % plaintext):
                self.seed.validate_backup_pair(dict(self.entry, **{plaintext: "hunter2"}),
                                               "fixture")

    def test_the_two_homes_must_nest_and_never_be_the_same_home(self) -> None:
        with self.assertRaisesRegex(self.seed.ConfigError, "one home"):
            self.seed.validate_backup_pair(
                dict(self.entry, readonly_subaccount_home=self.entry["subaccount_home"]),
                "fixture")
        with self.assertRaisesRegex(self.seed.ConfigError, "not a subdirectory"):
            self.seed.validate_backup_pair(
                dict(self.entry, readonly_subaccount_home="tenants/another/data"), "fixture")

    def test_the_url_has_no_port_and_the_login_is_the_providers(self) -> None:
        with self.assertRaisesRegex(self.seed.ConfigError, "port in the URL"):
            self.seed.validate_backup_pair(
                dict(self.entry, repo_url="sftp:u728180-sub1@u728180-sub1"
                                          ".your-storagebox.de:22/data/restic"), "fixture")
        with self.assertRaisesRegex(self.seed.ConfigError, "provider login"):
            self.seed.validate_backup_pair(
                dict(self.entry, subaccount_username=self.slug), "fixture")

    def test_the_keys_and_the_pinned_host_lines_are_checked(self) -> None:
        rw_host = "u728180-sub1.your-storagebox.de"
        ro_host = "u728180-sub2.your-storagebox.de"
        rw_line = self.entry["known_hosts"].splitlines()[0]
        ro_line = self.entry["known_hosts"].splitlines()[1]
        for broken, reason in (
            (dict(self.entry, ssh_private_key="not-a-key"), "not a private key"),
            (dict(self.entry, readonly_ssh_private_key=""), "not a private key"),
            (dict(self.entry, known_hosts=rw_line + "\nnot a host key line"),
             "not a pinned host key"),
            # The read-only URL's hostname, unpinned: the guest's strict check
            # fails on the restore path (review 1, must 3).
            (dict(self.entry, known_hosts=rw_line),
             "no pinned line for %s" % ro_host),
            (dict(self.entry, ssh_port=2222), "ssh_port must be 22"),
            (dict(self.entry, subaccount_id=self.entry["readonly_subaccount_id"]),
             "one sub-account twice"),
        ):
            with self.assertRaisesRegex(self.seed.ConfigError, reason):
                self.seed.validate_backup_pair(broken, "fixture")
        # One line per hostname is the shape; the second line is not an error,
        # and the order does not matter.
        self.seed.validate_backup_pair(
            dict(self.entry, known_hosts=ro_line + "\n" + rw_line), "fixture")

    def test_build_seed_carries_the_pair_and_overrides_the_config_template(self) -> None:
        seed = self.seed.build_seed({"slug": self.slug}, "srv-fixture", CONFIG,
                                    backup_pair=self.entry)
        self.assertEqual(seed["backup_repo_url"], self.entry["repo_url"])
        self.assertEqual(seed["backup"]["repo_url"], self.entry["repo_url"])
        self.assertEqual(seed["backup"]["subaccount_id"], 392550)
        self.assertEqual(seed["backup"]["subaccount_username"], "u728180-sub1")
        self.assertEqual(seed["backup"]["ssh_port"], 22)
        self.assertIn("PRIVATE KEY", seed["backup"]["ssh_private_key"])
        self.assertEqual(seed["backup"]["previous_instance"], "")
        # A row's own marker wins over the pair's (a rebuild).
        rebuilt = self.seed.build_seed({"slug": self.slug}, "srv-fixture", CONFIG,
                                       backup_pair=self.entry,
                                       previous_instance="srv-old-0001")
        self.assertEqual(rebuilt["backup"]["previous_instance"], "srv-old-0001")
        # Without a pair the config's template is what the bench and a manual
        # seed still get, and no `backup` block appears.
        plain = self.seed.build_seed({"slug": self.slug}, "srv-fixture", CONFIG)
        self.assertNotIn("backup", plain)
        self.assertEqual(plain["backup_repo_url"],
                         CONFIG["backup_repo_url"].replace("{slug}", self.slug))


class ProvisionBinding(unittest.TestCase):
    """ODL-455: the provision run binds the seed token before the machine boots."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        cls.db = os.path.join(d, "waitlist.db")
        cls.key = os.path.join(d, "seed-signing.pem")
        cls.pub = cls.key + ".pub"
        cls.seed_config = os.path.join(d, "seed-config.json")
        cls.seed_rebuilds = os.path.join(d, "seed-rebuilds.json")
        cls.backup_pairs = os.path.join(d, "seed-backup-pairs.json")
        cls.env = dict(
            os.environ,
            ODILE_DB=cls.db,
            ODILE_SEED_CONFIG=cls.seed_config,
            ODILE_SEED_REBUILDS=cls.seed_rebuilds,
            ODILE_SEED_BACKUP_PAIRS=cls.backup_pairs,
            ODILE_SEED_SIGNING_KEY=cls.key,
            ODILE_SEED_CERT=os.path.join(d, "seed-key.crt"),
            ODILE_SEED_RELEASE_PUBKEY=os.path.join(d, "release-signing.pem.pub"),
        )
        keygen = subprocess.run([sys.executable, SEED, "keygen"], env=cls.env,
                                capture_output=True, text=True, timeout=30)
        if keygen.returncode != 0:
            raise RuntimeError(keygen.stderr)
        os.chmod(cls.key, 0o600)
        cls.release_key, cls.release_pub, _cert = seed_certificate(d, cls.key)
        with open(cls.seed_config, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f)
        os.chmod(cls.seed_config, 0o600)
        init = subprocess.run([sys.executable, SEED, "init"], env=cls.env,
                              capture_output=True, text=True, timeout=30)
        if init.returncode != 0:
            raise RuntimeError(init.stderr)
        cls.port = free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, SEED, "serve"],
            env=dict(cls.env, ODILE_SEED_PORT=str(cls.port), ODILE_SEED_BIND="127.0.0.1"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        wait_health(cls.port, cls.proc)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.communicate()
        cls.tmp.cleanup()

    _counter = 0

    def setUp(self):
        type(self)._counter += 1
        self.slug = "odl-%06x" % type(self)._counter
        self.server_id = "%08x" % (0x31A60000 + type(self)._counter)
        self.key_file, self.pin = _tenant_files(self.tmp.name, self.slug)
        self.config = _odilectl_config(self.tmp.name, self.slug, self.db,
                                       self.seed_rebuilds)
        # ODL-924: the box API, ssh-keygen, the host-key scan and the key
        # install are seams; this class proves the wiring end to end without a
        # socket or a command.
        box_fixture.patchers(self)

    def box(self) -> box_fixture.FakeBox:
        """The fake box this test's store-of-record directory owns."""
        return box_fixture.box_for(Path(self.tmp.name) / "hetzner")

    def add_tenant(self) -> None:
        con = sqlite3.connect(self.db)
        try:
            con.execute(
                "INSERT INTO tenants (slug, email, created_at) VALUES (?, ?, ?)",
                (self.slug, self.slug + "@example.com", iso(now())),
            )
            con.commit()
        finally:
            con.close()

    def provision(self, *, listed: bool = False) -> tuple[int, str, str, RecordedProvision]:
        transport = RecordedProvision(self.db, self.slug, self.server_id, listed=listed)
        code, out, err = run_odilectl(
            ["provision", self.slug, "--key", self.key_file, "--pin", self.pin,
             "--interval", "0", "--apply", "--json"],
            self.config,
            lambda _cfg: ServuryProvider("fixture-reseller-key", transport=transport),
        )
        return code, out, err, transport

    def test_a_rebuild_seals_the_old_password_under_the_bound_token(self):
        self.add_tenant()
        recovery = os.path.join(self.tmp.name, self.slug + "-recovery.txt")
        with open(recovery, "w", encoding="utf-8") as handle:
            handle.write("Odile recovery file\n"
                         "===================\n"
                         "Tenant:              %s\n\n"
                         "Backup password:     %s\n" % (self.slug, REBUILD_PASSWORD))
        os.chmod(recovery, 0o600)
        transport = RecordedProvision(self.db, self.slug, self.server_id)
        code, out, err = run_odilectl(
            ["provision", self.slug, "--key", self.key_file, "--pin", self.pin,
             "--interval", "0", "--apply", "--json",
             "--backup-password-file", recovery],
            self.config,
            lambda _cfg: ServuryProvider("fixture-reseller-key", transport=transport),
        )
        self.assertEqual(code, 0, err)
        binding = json.loads(out)["seed"]
        self.assertEqual(binding["rebuild"], "sealed")
        text = Path(self.seed_rebuilds).read_text(encoding="utf-8")
        self.assertNotIn(REBUILD_PASSWORD, text)
        entry = json.loads(text)[self.slug]
        self.assertEqual(os.stat(self.seed_rebuilds).st_mode & 0o777, 0o600)
        self.assertEqual(entry["token_hash"], binding["token_hash"])
        self.assertEqual(entry["token_hash"],
                         hashlib.sha256(binding["token"].encode()).hexdigest())

        # The real service hands the secret to the machine the token is bound
        # to, and the guest's own code opens it.
        status, seed = exchange(self.port, binding["token"], self.server_id)
        self.assertEqual(status, 200, seed)
        self.assertEqual(
            load_firstboot_module().unseal_rebuild(seed["rebuild"], binding["token"]),
            REBUILD_PASSWORD)

        # A later provision without the file clears the entry: a token never
        # carries the previous attempt's secret. The provider lists the machine
        # the first run ordered, so this run records it rather than ordering a
        # second one (a live row takes no new machine; see provision's guard).
        code, out, err, _transport = self.provision(listed=True)
        self.assertEqual(code, 0, err)
        document = json.loads(Path(self.seed_rebuilds).read_text(encoding="utf-8"))
        self.assertNotIn(self.slug, document)

    def test_a_recovery_file_without_the_line_orders_nothing(self):
        self.add_tenant()
        bad = os.path.join(self.tmp.name, self.slug + "-not-a-recovery.txt")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write("this file is not the user's recovery file\n")
        os.chmod(bad, 0o600)
        transport = RecordedProvision(self.db, self.slug, self.server_id)
        code, out, err = run_odilectl(
            ["provision", self.slug, "--key", self.key_file, "--pin", self.pin,
             "--apply", "--backup-password-file", bad],
            self.config,
            lambda _cfg: ServuryProvider("fixture-reseller-key", transport=transport),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("Backup password:", err)
        self.assertEqual(transport.calls, [])

    def ledger(self) -> list[tuple]:
        con = sqlite3.connect(self.db)
        try:
            return con.execute(
                "SELECT token_hash, instance_id, used_at FROM seed_tokens"
                " WHERE instance_id = ?", (self.server_id,)).fetchall()
        finally:
            con.close()

    def tenant_hash(self):
        con = sqlite3.connect(self.db)
        try:
            row = con.execute("SELECT setup_token_hash FROM tenants WHERE slug = ?",
                              (self.slug,)).fetchone()
            return row[0] if row else None
        finally:
            con.close()

    # -- the binding ------------------------------------------------------

    def test_a_provision_run_binds_the_token_before_the_machine_is_active(self):
        self.add_tenant()
        code, out, err, transport = self.provision()
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["ok"], payload)
        binding = payload["seed"]
        self.assertTrue(binding["bound"], binding)
        self.assertEqual(binding["instance_id"], self.server_id)
        token = binding["token"]
        self.assertRegex(token, r"^[0-9a-f]{64}$")
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.assertEqual(binding["token_hash"], digest)

        # create returned the id; the poll answered while the machine was still
        # being built, and the ledger already held the token bound to that id.
        self.assertEqual(transport.calls, ["servers", "create", "server"])
        self.assertEqual(transport.poll_ledger, [(digest, self.server_id, None)])

        # the order carries the tenant's public key, and no HQ credential
        self.assertEqual(transport.create_body["ssh_key"], TENANT_KEY)
        for banned in ("password", "passphrase", "private_key", "operator_key"):
            self.assertNotIn(banned, transport.create_body)

        self.assertEqual(self.ledger(), [(digest, self.server_id, None)])
        self.assertEqual(self.tenant_hash(), digest)
        con = sqlite3.connect(self.db)
        try:
            plaintext = con.execute(
                "SELECT count(*) FROM seed_tokens WHERE token_hash = ?", (token,)
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(plaintext, 0, "the token itself must not be stored")

    def test_the_human_transcript_prints_the_instance_and_the_token_hash(self):
        self.add_tenant()
        transport = RecordedProvision(self.db, self.slug, self.server_id)
        code, out, err = run_odilectl(
            ["provision", self.slug, "--key", self.key_file, "--pin", self.pin,
             "--interval", "0", "--apply"],
            self.config,
            lambda _cfg: ServuryProvider("fixture-reseller-key", transport=transport),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("  server_id: %s" % self.server_id, out)
        match = re.search(r"^  setup token: ([0-9a-f]{64})$", out, re.M)
        self.assertIsNotNone(match, out)
        token = match.group(1)
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.assertIn("  token hash:  " + digest, out)
        self.assertEqual(out.count(token), 1, "the token must be printed once")

    def test_the_bound_token_seeds_only_its_instance_and_only_once(self):
        self.add_tenant()
        code, out, err, _transport = self.provision()
        self.assertEqual(code, 0, err)
        token = json.loads(out)["seed"]["token"]

        status, body = exchange(self.port, token, "srv-another")
        self.assertEqual(status, 403, body)
        self.assertEqual(body["reason"], "wrong_instance")
        # the refusal did not burn the token
        status, body = exchange(self.port, token, self.server_id)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["slug"], self.slug)
        self.assertEqual(body["instance_id"], self.server_id)
        status, body = exchange(self.port, token, self.server_id)
        self.assertEqual(status, 410, body)
        self.assertEqual(body["reason"], "used")

    # -- the fallback -----------------------------------------------------

    def test_a_provision_without_a_tenant_row_records_the_claim_fallback(self):
        code, out, err, transport = self.provision()
        self.assertEqual(code, 0, err)
        binding = json.loads(out)["seed"]
        self.assertFalse(binding["bound"], binding)
        self.assertIn("no tenants row", binding["reason"])
        self.assertIn("ODL-220", binding["reason"])
        self.assertEqual(transport.calls, ["servers", "create", "server"])
        self.assertEqual(self.ledger(), [])

    def test_the_claim_path_mints_after_activation_and_the_mail_never_shows_it(self):
        """The fallback: no create-time binding, so the claim worker mints.

        B-once (ODL-925): the token is the machine's seed badge and the
        rebuild-seal key; the ready email and the kit name the tokenless page
        and never print it. The minted token still opens the seed exchange.
        """
        con = claim_module.open_db(self.db)
        captured: dict[str, str] = {}
        original_rotate = claim_module._SEED.rotate
        try:
            issued = claim_module.issue_claim(
                con, self.slug + "@example.com", plan="Blackbox Plus",
                slug=self.slug, now=now(),
            )
            claim_module.accept(con, issued.token, TENANT_KEY, now=now())
            outbox = os.path.join(self.tmp.name, self.slug + "-outbox")
            config = Config(Path(self.config), {
                "state_db": self.db,
                "seed_db": self.db,
                "outbox": outbox,
                "release": PROVISION_RELEASE,
                "default_plan_id": "113",
                "default_days": "31",
                # ODL-924 follow-up: the claim path mints the pair before the
                # order, and this Config replaces the file's values wholesale —
                # without these four the recorder would use the live host's
                # store of record and seed handoff.
                "hetzner_box_id": "709870",
                "hetzner_dir": os.path.join(self.tmp.name, "hetzner"),
                "hetzner_keys": os.path.join(self.tmp.name, "keys"),
                "seed_backup_pairs": os.path.join(self.tmp.name,
                                                  "seed-backup-pairs.json"),
            })

            def recording_rotate(*args, **kwargs):
                minted = original_rotate(*args, **kwargs)
                captured["token"] = minted["token"]
                return minted

            claim_module._SEED.rotate = recording_rotate
            result = claim_module.provision_tenant(
                con, self.slug, provider=FixtureProvider(), config=config,
                dns_client=FakeDNS(), outbox=claim_module.ReadyOutbox(outbox),
                now=int(time.time()), interval=0.0,
                # ODL-947: the mail is queued when the machine answers at its
                # own name; this test is about the token, so the machine
                # answers on the first run.
                guest_probe=lambda domain, slug: SimpleNamespace(
                    measurement="c0" * 48, release=PROVISION_RELEASE,
                    verify_bytes=50000, checked_at="2026-11-24T08:49:07Z"),
            )
        finally:
            claim_module._SEED.rotate = original_rotate
            con.close()
        self.assertTrue(result.ok, result.reason)
        con = claim_module.open_db(self.db)
        try:
            row = con.execute(
                "SELECT token_hash, instance_id, used_at FROM seed_tokens"
                " WHERE tenant_id = (SELECT id FROM tenants WHERE slug = ?)",
                (self.slug,),
            ).fetchone()
            tenant = con.execute(
                "SELECT setup_token_hash FROM tenants WHERE slug = ?", (self.slug,)
            ).fetchone()
        finally:
            con.close()
        self.assertIsNotNone(row)
        self.assertIsNotNone(result.server_id)
        self.assertEqual(row["instance_id"], result.server_id)
        self.assertEqual(row["token_hash"], tenant["setup_token_hash"])
        self.assertIsNotNone(result.email_path)
        assert result.email_path is not None
        token = captured["token"]
        self.assertEqual(hashlib.sha256(token.encode()).hexdigest(), row["token_hash"])
        message = read_ready_body(result.email_path)
        self.assertNotIn(token, message, "the ready email never prints the setup token")
        self.assertNotIn("first-visit?t=", message)

        status, body = exchange(self.port, token, "srv-another")
        self.assertEqual(status, 403, body)
        status, body = exchange(self.port, token, result.server_id)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["slug"], self.slug)
        status, body = exchange(self.port, token, result.server_id)
        self.assertEqual(status, 410, body)

    # -- the seed service's bind itself -----------------------------------

    def test_bind_writes_the_ledger_and_the_tenant_hash_in_one_step(self):
        self.add_tenant()
        seed = load_seed_module()
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            first = seed.bind(con, self.slug, instance_id=self.server_id)
            row = con.execute(
                "SELECT token_hash, instance_id, used_at FROM seed_tokens"
                " WHERE token_hash = ?", (first["token_hash"],),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["instance_id"], self.server_id)
            self.assertIsNone(row["used_at"])
            self.assertEqual(
                con.execute("SELECT setup_token_hash FROM tenants WHERE slug = ?",
                            (self.slug,)).fetchone()[0],
                first["token_hash"],
            )

            second = seed.bind(con, self.slug, instance_id=self.server_id)
            ended = con.execute(
                "SELECT used_at FROM seed_tokens WHERE token_hash = ?",
                (first["token_hash"],),
            ).fetchone()[0]
            self.assertIsNotNone(ended, "a second bind ends the previous token")
            self.assertEqual(
                con.execute("SELECT setup_token_hash FROM tenants WHERE slug = ?",
                            (self.slug,)).fetchone()[0],
                second["token_hash"],
            )
        finally:
            con.close()

    # ------------------------------------------------- the pair (ODL-924)

    def store_path(self) -> Path:
        return Path(self.tmp.name) / "hetzner" / ("odile-%s.env" % self.slug)

    def pairs_path(self) -> Path:
        return Path(self.tmp.name) / "seed-backup-pairs.json"

    def test_a_provision_mints_the_pair_in_the_measured_order(self):
        """Create, update settings, key install, readback, then readonly: true."""
        self.add_tenant()
        code, out, err, _transport = self.provision()
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        backup = payload["backup"]
        self.assertEqual(backup["state"], "created")
        # Two resets per account: one to make the password live for the key
        # install, one to discard it.
        self.assertEqual(backup["password_resets"], 4)
        box = self.box()
        # This class shares one fake box, so a sibling test's pair is also in
        # the list: assert this slug's pair, in creation order.
        pair = [account for account in box.accounts
                if re.fullmatch(r"odile-%s-(rw|ro)" % re.escape(self.slug),
                                account["name"])]
        self.assertEqual([account["name"] for account in pair],
                         ["odile-%s-rw" % self.slug, "odile-%s-ro" % self.slug])
        rw, ro = pair
        # The nested homes: never the same home (shared homes share keys).
        self.assertEqual(rw["home_directory"], "tenants/%s" % self.slug)
        self.assertEqual(ro["home_directory"], "tenants/%s/data" % self.slug)
        self.assertFalse(rw["access_settings"]["readonly"])
        self.assertTrue(ro["access_settings"]["readonly"])
        # The lock came after both key installs, and a readback preceded it.
        lock = next(index for index, (what, detail) in enumerate(box_fixture.EVENTS)
                    if what.endswith("update_access_settings") and detail == "readonly=True")
        installs = [index for index, (what, _detail) in enumerate(box_fixture.EVENTS)
                    if what == "install"]
        self.assertEqual(len(installs), 2)
        self.assertLess(max(installs), lock)
        readbacks = [index for index, (what, _detail) in enumerate(box_fixture.EVENTS)
                     if re.match(r"^GET /storage_boxes/\d+/subaccounts/\d+$", what)]
        self.assertTrue(readbacks and min(readbacks) < lock)
        # The logins are the provider's, recorded from the API.
        self.assertEqual(backup["subaccount_username"], rw["username"])
        self.assertRegex(rw["username"], r"^u728180-sub\d+$")
        self.assertEqual(backup["readonly_subaccount_username"], ro["username"])
        # The store of record: 0600, both private halves, no password.
        env = self.store_path()
        self.assertEqual(os.stat(env).st_mode & 0o777, 0o600)
        text = env.read_text(encoding="utf-8")
        self.assertIn("BACKUP_RW_PRIVATE_KEY=", text)
        self.assertIn("BACKUP_RO_PRIVATE_KEY=", text)
        self.assertIn("BACKUP_REPOSITORY=sftp:%s@%s:data/restic"
                      % (rw["username"], rw["server"]), text)
        for password in box.passwords:
            self.assertNotIn(password, text)
        # The seed handoff: 0600, one entry, the no-port URL, both keys.
        pairs = json.loads(self.pairs_path().read_text(encoding="utf-8"))
        entry = pairs[self.slug]
        self.assertEqual(os.stat(self.pairs_path()).st_mode & 0o777, 0o600)
        self.assertEqual(entry["subaccount_id"], rw["id"])
        self.assertEqual(entry["readonly_subaccount_id"], ro["id"])
        self.assertEqual(entry["repo_url"],
                         "sftp:%s@%s:data/restic" % (rw["username"], rw["server"]))
        self.assertNotIn(":22", entry["repo_url"])
        self.assertIn("ssh_private_key", entry)
        # The seed's pin opens every URL it carries: one line per hostname
        # (review 1, must 3 — one line failed the read-only URL under strict
        # checking).
        pinned = entry["known_hosts"].splitlines()
        self.assertEqual([line.split(" ")[0] for line in pinned],
                         [rw["server"], ro["server"]])
        self.assertEqual(entry["known_hosts"].count("\n"), 1)
        # The row's cache names both ids and both logins.
        con = sqlite3.connect(self.db)
        try:
            detail = json.loads(con.execute(
                "SELECT detail FROM tenant_state WHERE slug = ?", (self.slug,)
            ).fetchone()[0])
        finally:
            con.close()
        for key in ("backup_box_id", "backup_subaccount_id",
                    "backup_readonly_subaccount_id", "backup_subaccount_username",
                    "backup_readonly_subaccount_username", "previous_instance"):
            self.assertIn(key, detail)
        self.assertEqual(detail["backup_subaccount_id"], rw["id"])
        self.assertEqual(detail["backup_readonly_subaccount_username"], ro["username"])
        # The seed the guest gets carries the whole pair, and no password.
        status, seed = exchange(self.port, payload["seed"]["token"], self.server_id)
        self.assertEqual(status, 200, seed)
        self.assertEqual(seed["backup_repo_url"], entry["repo_url"])
        self.assertEqual(seed["backup"]["repo_url"], entry["repo_url"])
        self.assertEqual(seed["backup"]["subaccount_username"], rw["username"])
        self.assertEqual(seed["backup"]["known_hosts"], entry["known_hosts"])
        self.assertTrue(seed["backup"]["ssh_private_key"].startswith("-----BEGIN"))
        self.assertEqual(seed["backup"]["previous_instance"], "")
        self.assertNotIn("password", json.dumps(seed).lower())

    def test_the_existing_pair_is_adopted_and_a_second_run_prints_same(self):
        self.add_tenant()
        box = self.box()
        rw, ro = box.add_pair(self.slug)
        keys = Path(self.tmp.name) / "keys" / self.slug
        keys.mkdir(parents=True, exist_ok=True)
        (keys / ("odile-%s-rw" % self.slug)).write_text(box_fixture.PRIVATE_KEY,
                                                         encoding="utf-8")
        (keys / ("odile-%s-ro" % self.slug)).write_text(box_fixture.PRIVATE_KEY,
                                                         encoding="utf-8")
        (keys / "known_hosts").write_text(box_fixture.HOST_LINE + "\n", encoding="utf-8")
        before = len(box.calls)

        code, out, err, _transport = self.provision()
        self.assertEqual(code, 0, err)
        backup = json.loads(out)["backup"]
        self.assertEqual(backup["state"], "adopted")
        self.assertEqual(backup["subaccount_id"], rw["id"])
        self.assertEqual(backup["readonly_subaccount_id"], ro["id"])
        # An adopted pair pins both hostnames too (the drill's own file was
        # kept; the seed must open both URLs it carries).
        entry = json.loads(self.pairs_path().read_text(encoding="utf-8"))[self.slug]
        self.assertEqual([line.split(" ")[0] for line in entry["known_hosts"].splitlines()],
                         [rw["server"], ro["server"]])
        self.assertEqual([call for call in box.calls[before:] if call[0] == "POST"], [],
                         "an adopted pair is never re-created")

        # The idempotent second run reads the pair back and prints `same`.
        before = len(box.calls)
        transport = RecordedProvision(self.db, self.slug, self.server_id, listed=True)
        code, out, err = run_odilectl(
            ["provision", self.slug, "--key", self.key_file, "--pin", self.pin,
             "--interval", "0", "--apply"],
            self.config,
            lambda _cfg: ServuryProvider("fixture-reseller-key", transport=transport),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("same", out)
        self.assertEqual([call for call in box.calls[before:] if call[0] == "POST"], [])

    def test_a_reopen_records_the_dead_machine_in_the_row_the_seed_and_the_store(self):
        """Review 1, must 1: a rebuilt machine's seed carries the dead id.

        The slug is adopted, then the row goes `destroyed` (the machine is
        gone). The second run takes the read-back path — the store exists and
        the box's pair is intact — and must record the dead machine's id as
        `previous_instance` in the row's `detail`, the seed entry and the store
        of record: that field is what makes ODL-937's first visit a restore.
        """
        self.add_tenant()
        box = self.box()
        box.add_pair(self.slug)
        keys = Path(self.tmp.name) / "keys" / self.slug
        keys.mkdir(parents=True, exist_ok=True)
        (keys / ("odile-%s-rw" % self.slug)).write_text(box_fixture.PRIVATE_KEY,
                                                         encoding="utf-8")
        (keys / ("odile-%s-ro" % self.slug)).write_text(box_fixture.PRIVATE_KEY,
                                                         encoding="utf-8")
        (keys / "known_hosts").write_text(box_fixture.HOST_LINE + "\n", encoding="utf-8")
        code, out, err, _transport = self.provision()
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["backup"]["state"], "adopted")

        # The machine is gone. The slug reopens; the dead id is the marker.
        con = sqlite3.connect(self.db)
        try:
            con.execute("UPDATE tenant_state SET state = 'destroyed', server_id = ?"
                        " WHERE slug = ?", ("srv-old-0001", self.slug))
            con.commit()
        finally:
            con.close()
        code, out, err, _transport = self.provision()
        self.assertEqual(code, 0, out + err)
        backup = json.loads(out)["backup"]
        self.assertEqual(backup["state"], "same")
        self.assertEqual(backup["previous_instance"], "srv-old-0001")

        con = sqlite3.connect(self.db)
        try:
            detail = json.loads(con.execute(
                "SELECT detail FROM tenant_state WHERE slug = ?", (self.slug,)
            ).fetchone()[0])
        finally:
            con.close()
        self.assertEqual(detail["previous_instance"], "srv-old-0001")
        entry = json.loads(self.pairs_path().read_text(encoding="utf-8"))[self.slug]
        self.assertEqual(entry["previous_instance"], "srv-old-0001")
        self.assertIn("BACKUP_PREVIOUS_INSTANCE=srv-old-0001",
                      self.store_path().read_text(encoding="utf-8"))

        # A third run with no dead machine keeps the recorded marker: the
        # read-back is idempotent, not a reset. The provider lists the machine
        # the reopen ordered, so this run records it rather than ordering again.
        code, out, err, _transport = self.provision(listed=True)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(out)["backup"]["previous_instance"], "srv-old-0001")

    def test_at_the_ceiling_a_recorded_slug_still_reads_back_as_same(self):
        """Review 1, must 2: 50 pairs recorded, one of them this slug.

        The 51st refusal counts pairs; a slug the store already records is not
        an additional pair, so re-running it at the ceiling must print `same`
        rather than be refused as number 51. Its own store directory, so the
        fifty fillers never leak into another test's pairing.
        """
        directory = tempfile.mkdtemp(prefix="odl924-ceiling-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        self.add_tenant()
        box = box_fixture.box_for(Path(directory) / "hetzner")
        box.add_pair(self.slug)
        keys = Path(directory) / "keys" / self.slug
        keys.mkdir(parents=True, exist_ok=True)
        (keys / ("odile-%s-rw" % self.slug)).write_text(box_fixture.PRIVATE_KEY,
                                                         encoding="utf-8")
        (keys / ("odile-%s-ro" % self.slug)).write_text(box_fixture.PRIVATE_KEY,
                                                         encoding="utf-8")
        (keys / "known_hosts").write_text(box_fixture.HOST_LINE + "\n", encoding="utf-8")
        config = _odilectl_config(directory, self.slug, self.db, self.seed_rebuilds)

        def run(slug: str, *, listed: bool) -> tuple[int, str, str]:
            transport = RecordedProvision(self.db, slug, self.server_id, listed=listed)
            return run_odilectl(
                ["provision", slug, "--key", self.key_file, "--pin", self.pin,
                 "--interval", "0", "--apply"],
                config,
                lambda _cfg: ServuryProvider("fixture-reseller-key", transport=transport),
            )

        code, out, err = run(self.slug, listed=False)
        self.assertEqual(code, 0, err)
        self.assertIn("adopted", out)

        store = Path(directory) / "hetzner"
        for index in range(49):
            (store / ("odile-odl-fill%04d.env" % index)).write_text(
                "BACKUP_SLUG=filler\n", encoding="utf-8")
        self.assertEqual(len(list(store.glob("odile-*.env"))), 50)

        before = len(box.calls)
        code, out, err = run(self.slug, listed=True)
        self.assertEqual(code, 0, out + err)
        self.assertIn("same", out)
        self.assertEqual([call for call in box.calls[before:] if call[0] == "POST"], [],
                         "the recorded slug re-reads, it never re-creates")

        # A slug that is *not* one of the 50 is still refused, by name.
        other = "odl-fresh51"
        code, out, err = run(other, listed=False)
        self.assertEqual(code, 4, out + err)
        self.assertIn("51st", err)
        self.assertIn(other, err)

    def test_the_51st_pair_is_refused_by_name_before_any_provider_call(self):
        directory = tempfile.mkdtemp(prefix="odl924-51st-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        store = Path(directory) / "hetzner"
        store.mkdir(mode=0o700, parents=True, exist_ok=True)
        for index in range(50):
            (store / ("odile-odl-%06x.env" % index)).write_text(
                "BACKUP_SLUG=fixture\n", encoding="utf-8")
        slug = "odl-51st01"
        config = _odilectl_config(directory, slug, self.db, self.seed_rebuilds)
        transport = RecordedProvision(self.db, slug, self.server_id)
        code, out, err = run_odilectl(
            ["provision", slug, "--key", self.key_file, "--pin", self.pin,
             "--interval", "0", "--apply"],
            config,
            lambda _cfg: ServuryProvider("fixture-reseller-key", transport=transport),
        )
        self.assertEqual(code, 4, out + err)
        self.assertIn("51st", err)
        self.assertIn(slug, err)
        self.assertIn("100 sub-accounts", err)
        box = box_fixture.box_for(Path(directory) / "hetzner")
        self.assertEqual(box.calls, [], "the count is the store's; no API call was made")
        self.assertEqual(transport.calls, [], "no machine was ordered")

    def test_the_ro_home_must_nest_and_the_ro_key_must_be_locked(self):
        self.add_tenant()
        box = self.box()
        rw, ro = box.add_pair(self.slug)
        ro["home_directory"] = "tenants/%s" % self.slug
        keys = Path(self.tmp.name) / "keys" / self.slug
        keys.mkdir(parents=True, exist_ok=True)
        (keys / ("odile-%s-rw" % self.slug)).write_text(box_fixture.PRIVATE_KEY,
                                                         encoding="utf-8")
        (keys / ("odile-%s-ro" % self.slug)).write_text(box_fixture.PRIVATE_KEY,
                                                         encoding="utf-8")
        (keys / "known_hosts").write_text(box_fixture.HOST_LINE + "\n", encoding="utf-8")
        code, out, err, _transport = self.provision()
        self.assertEqual(code, 4, out + err)
        self.assertIn("data", out + err)
        self.assertIn("never the same home", out + err)
        self.assertFalse(self.store_path().exists(),
                         "a refused pair writes no store of record")


def transcript() -> None:
    """Print the ODL-455 evidence from a disposable database and service.

    Every number here comes from a real run of the real code: the recorded
    provider transport (shaped from the API docs), the `odilectl provision`
    command, the seed service over loopback HTTP, and SQLite. Nothing live is
    touched and no money is spent. The exact command is:

        /usr/bin/python3 tests/test_seed.py --transcript
    """
    directory = tempfile.mkdtemp(prefix="odile-455-")
    proc = None
    try:
        db = os.path.join(directory, "waitlist.db")
        key = os.path.join(directory, "seed-signing.pem")
        seed_config = os.path.join(directory, "seed-config.json")
        env = dict(os.environ, ODILE_DB=db, ODILE_SEED_CONFIG=seed_config,
                   ODILE_SEED_SIGNING_KEY=key)
        for command in ("keygen", "init"):
            result = subprocess.run([sys.executable, SEED, command], env=env,
                                    capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                raise RuntimeError(result.stderr)
        os.chmod(key, 0o600)
        with open(seed_config, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f)
        os.chmod(seed_config, 0o600)
        port = free_port()
        proc = subprocess.Popen(
            [sys.executable, SEED, "serve"],
            env=dict(env, ODILE_SEED_PORT=str(port), ODILE_SEED_BIND="127.0.0.1"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        wait_health(port, proc)

        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row

        print("== 1. a provision run binds the token to the create id, before the poll ==")
        slug = "odl-7f3a2c"
        server_id = "0badc0de"
        con.execute(
            "INSERT INTO tenants (slug, email, created_at) VALUES (?, ?, ?)",
            (slug, "ada@example.com", iso(now())),
        )
        con.commit()
        key_file, pin = _tenant_files(directory, slug)
        config = _odilectl_config(directory, slug, db)
        transport = RecordedProvision(db, slug, server_id)
        argv = ["provision", slug, "--key", key_file, "--pin", pin,
                "--interval", "0", "--apply"]
        print("$ odilectl " + " ".join(argv))
        code, out, err = run_odilectl(
            argv, config,
            lambda _cfg: ServuryProvider("fixture-reseller-key", transport=transport),
        )
        print(out.rstrip())
        if err.strip():
            print("stderr:", err.strip())
        print("exit:", code)
        print("provider calls:", transport.calls)
        print("ledger read at the first poll (machine still deploying):",
              transport.poll_ledger)
        print("create body ssh_key == the tenant public key:",
              transport.create_body.get("ssh_key") == TENANT_KEY,
              "· password/passphrase fields:",
              [k for k in ("password", "passphrase", "private_key", "operator_key")
               if k in transport.create_body])
        match = re.search(r"^  setup token: ([0-9a-f]{64})$", out, re.M)
        if match is None:
            raise RuntimeError("the provision run printed no setup token:\n" + out)
        token = match.group(1)
        row = con.execute(
            "SELECT token_hash, instance_id, expires_at, used_at FROM seed_tokens"
            " WHERE instance_id = ?", (server_id,)).fetchone()
        print("seed_tokens row: token_hash=%s instance_id=%s expires_at=%s used_at=%s"
              % (row["token_hash"], row["instance_id"], row["expires_at"], row["used_at"]))
        print("tenants.setup_token_hash:", con.execute(
            "SELECT setup_token_hash FROM tenants WHERE slug = ?", (slug,)).fetchone()[0])
        print("sha256(printed token) == stored hash:",
              hashlib.sha256(token.encode()).hexdigest() == row["token_hash"])
        print("rows where token_hash == the plaintext token:",
              con.execute("SELECT count(*) FROM seed_tokens WHERE token_hash = ?",
                          (token,)).fetchone()[0])

        print()
        print("== 2. the seed service refuses another instance and a second use ==")
        for instance in ("srv-another", server_id, server_id):
            status, body = exchange(port, token, instance)
            shown = {k: body[k] for k in ("reason", "slug", "instance_id") if k in body}
            print("POST /seed {token, instance_id=%s} -> %d %s"
                  % (instance, status, json.dumps(shown, sort_keys=True)))

        print()
        print("== 3. no tenants row: the report records the claim fallback ==")
        fallback_slug = "odl-6e5d4c"
        fallback_server = "41b72ccc"
        fallback_key, fallback_pin = _tenant_files(directory, fallback_slug)
        fallback_config = _odilectl_config(directory, fallback_slug, db)
        fallback_transport = RecordedProvision(db, fallback_slug, fallback_server)
        argv = ["provision", fallback_slug, "--key", fallback_key,
                "--pin", fallback_pin, "--interval", "0", "--apply"]
        print("$ odilectl " + " ".join(argv))
        code, out, err = run_odilectl(
            argv, fallback_config,
            lambda _cfg: ServuryProvider("fixture-reseller-key",
                                         transport=fallback_transport),
        )
        print(out.rstrip())
        if err.strip():
            print("stderr:", err.strip())
        print("exit:", code)
        print("seed_tokens rows for %s: %r" % (fallback_server, con.execute(
            "SELECT token_hash FROM seed_tokens WHERE instance_id = ?",
            (fallback_server,)).fetchall()))

        print()
        print("== 4. the claim fallback mints at activation; the mail never prints it ==")
        claim_slug = "odl-5a4b3c"
        issued = claim_module.issue_claim(
            con, "grace@example.com", plan="Blackbox Plus", slug=claim_slug, now=now())
        claim_module.accept(con, issued.token, TENANT_KEY, now=now())
        outbox = os.path.join(directory, "outbox")
        claim_config = Config(Path(config), {
            "state_db": db, "seed_db": db, "outbox": outbox,
            "release": PROVISION_RELEASE, "default_plan_id": "113",
            "default_days": "31",
        })
        captured = {}
        original_rotate = claim_module._SEED.rotate

        def recording_rotate(*args, **kwargs):
            minted = original_rotate(*args, **kwargs)
            captured["token"] = minted["token"]
            return minted

        claim_module._SEED.rotate = recording_rotate
        try:
            result = claim_module.provision_tenant(
                con, claim_slug, provider=FixtureProvider(), config=claim_config,
                dns_client=FakeDNS(), outbox=claim_module.ReadyOutbox(outbox),
                now=int(time.time()), interval=0.0,
            )
        finally:
            claim_module._SEED.rotate = original_rotate
        if not result.ok or not result.server_id or not result.email_path:
            raise RuntimeError("the claim fallback did not provision: %s" % result.reason)
        print("claim provision: ok=%s machine=%s email=%s"
              % (result.ok, result.server_id, result.email_path))
        message = read_ready_body(result.email_path)
        claim_token = captured["token"]
        claim_row = con.execute(
            "SELECT token_hash, instance_id FROM seed_tokens WHERE instance_id = ?",
            (result.server_id,)).fetchone()
        print("ready email prints the setup token:",
              claim_token in message,
              "· tokenless page named:", "first-visit?t=" not in message,
              "· stored hash:", claim_row["token_hash"],
              "· instance:", claim_row["instance_id"])
        for instance in ("srv-another", result.server_id, result.server_id):
            status, body = exchange(port, claim_token, instance)
            shown = {k: body[k] for k in ("reason", "slug", "instance_id") if k in body}
            print("POST /seed {token, instance_id=%s} -> %d %s"
                  % (instance, status, json.dumps(shown, sort_keys=True)))

        print()
        print("== 5. the gate ==")
        print("$ /usr/bin/python3 -m unittest tests.test_seed -v   # output in the handoff")
        print("$ make check                                       # output in the handoff")
        con.close()
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
        shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--transcript"]:
        transcript()
    else:
        unittest.main(verbosity=2)
