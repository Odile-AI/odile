#!/usr/bin/python3
"""ODL-242 — one NEAR AI inference key per instance, and the record of it.

What this file proves, in the order the card asks:

* two instances provision with two *different* keys, each seed carries only its
  own, and the machine's ``/etc/odile/key.env`` (written by the installer's own
  ``ensure_key_env``) matches its seed;
* HQ's record names the label and the first characters, and no raw ``sk-…`` is
  stored at HQ after the exchange — not in the database, not in the handoff;
* destroy resolves the provider's key id from the *list* call (label + prefix)
  and deletes it, and a second destroy converges without another delete;
* the allowance's two refusal classes are read from the provider's own numbers
  (``api_key_limit_exceeded`` at the key's ``spendLimit``, ``no_limit_configured``
  at zero org credit) and named in one plain sentence;
* a 401 rotates the account credential and persists the rotated pair at 0600
  before the retried call;
* a seed with no key anywhere refuses ``503 no_inference_key`` *without*
  spending the token, and a key bound to another token is not handed over.

Hermetic: a recorded transport stands in for the provider's management API and
the console's refresh route; the seed service is the real one, started on
localhost with a throwaway database, signing key and release certificate.

    /usr/bin/python3 -m unittest tests.test_keys -v
"""

from __future__ import annotations

import contextlib
import http.client
import hashlib
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
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "seed" / "seed.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from odilectl import cli, keys, state  # noqa: E402
from odilectl.config import Config  # noqa: E402
from odilectl.providers import servury  # noqa: E402
from tests.test_odilectl import (  # noqa: E402
    KEY_TEXT,
    TEST_KEY,
    RecordedTransport,
    create_response,
    empty_servers_response,
    server_row,
)

WORKSPACE = "220b244c-1f72-479e-afa5-6ad8ebe735c4"
ORG = "dedd2da9-44d3-46ba-b9ed-5ab34c5e3031"
NANO = 1_000_000_000
SPEND_LIMIT = 10 * NANO
CREDENTIAL = {
    "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) HeadlessChrome/145 Safari/537.36",
    "access_token": "access-1",
    "refresh_token": "***",
}


def seed_module():
    """Load ``seed/seed.py`` by path, the way the CLI and the service do."""
    spec = importlib.util.spec_from_file_location("odile_seed_service_test", SEED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


#: One id sequence for every fake surface, so two mints in one test (or in two
#: fixtures) never share an id — the store refuses a reused id outright.
_KEY_SERIAL = [0]


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shared_key(slug: str) -> str:
    """The seed config's shared fallback for one slug — the value a seed must not carry.

    Built in two pieces so a secret scanner reading this file never mistakes the
    fixture for a minted key; `SeedFixture.shared_fallback` writes the same one.
    """
    return "sk-shared-" + slug


def wait_health(port: int, proc, seconds: float = 10.0) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=1).status == 200:
                return
        except OSError:
            time.sleep(0.05)
    output = b""
    if proc.poll() is not None:
        output = proc.communicate(timeout=5)[0] or b""
    raise RuntimeError("the seed service did not start:\n" + output.decode("utf-8", "replace"))


# --------------------------------------------------------------- the provider


class FakeNear:
    """A recorded NEAR AI management surface: mint, list, delete, balance, refresh.

    Shaped from the live captures in `docs/product/SPIKE-near-key-api.md` §3:
    the mint returns the raw key once and the masked `key_prefix`, the list
    returns `key: null`, and the balance omits `remaining` while the org has no
    credit. It also enforces the bearer, so a call that skipped a refresh after
    a 401 fails here rather than passing quietly.
    """

    def __init__(self, *, access_token: str = "access-1", key_limit: int | None = SPEND_LIMIT,
                 usage: int = 0, remaining: int | None = 10 * NANO,
                 refresh_status: int = 200, mint_status: int = 201):
        self.access_token = access_token
        self.key_limit = key_limit
        self.usage = usage
        self.remaining = remaining
        self.refresh_status = refresh_status
        self.mint_status = mint_status
        self.calls: list[dict] = []
        self.keys: dict[str, dict] = {}
        self.raw_keys: list[str] = []
        self.refresh_token = "***"
        self.minted = 0
        self.expire_access_once = False

    # -- the transport seam ------------------------------------------------

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None):
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        path = parts.path
        payload = json.loads(body.decode("utf-8")) if body else None
        self.calls.append({"method": method, "path": path, "headers": dict(headers),
                           "body": payload})
        if path == "/api/auth/refresh":
            return self._refresh(headers)
        authorization = headers.get("authorization") or ""
        if self.expire_access_once:
            self.expire_access_once = False
            return 401, b'{"message":"token expired"}'
        if authorization != "Bearer " + self.access_token:
            return 401, b'{"message":"Invalid or expired access token"}'
        if method == "POST" and path.endswith("/api-keys"):
            return self._mint(payload)
        if method == "GET" and path.endswith("/api-keys"):
            listed = [
                {**key, "key": None}
                for key in self.keys.values()
            ]
            return 200, json.dumps({"api_keys": listed, "total": len(listed),
                                    "limit": 100, "offset": 0}).encode()
        if method == "DELETE" and "/api-keys/" in path:
            key_id = path.rsplit("/", 1)[-1]
            if self.keys.pop(key_id, None) is None:
                return 404, b'{"message":"not found"}'
            return 204, b""
        if method == "GET" and path.endswith("/usage/balance"):
            answer = {"organization_id": ORG, "total_spent": self.usage,
                      "total_spent_display": "$0.00", "credit_limits": []}
            if self.remaining is not None:
                answer["remaining"] = self.remaining
                answer["spend_limit"] = SPEND_LIMIT
            return 200, json.dumps(answer).encode()
        return 404, b'{"message":"no such route"}'

    # -- the shapes --------------------------------------------------------

    def _refresh(self, headers: dict):
        if self.refresh_status != 200:
            return self.refresh_status, b'{"error":"Failed to refresh token"}'
        if "cookie" not in headers or "HeadlessChrome" not in (headers.get("user-agent") or ""):
            return 401, b'{"error":"Failed to refresh token"}'
        self.access_token = "access-%d" % (len(self.calls) + 1)
        self.refresh_token = "rt-%d" % (len(self.calls) + 1)
        return 200, json.dumps({
            "__Host-cloud-access_token": self.access_token,
            "__Host-cloud-refresh_token": self.refresh_token,
        }).encode()

    def _mint(self, payload):
        if self.mint_status not in (200, 201):
            return self.mint_status, b'{"message":"the mint failed"}'
        if not isinstance(payload, dict) or not payload.get("name"):
            return 400, b'{"message":"name is required"}'
        limit = payload.get("spendLimit")
        self.minted += 1
        _KEY_SERIAL[0] += 1
        key_id = "key-%04d" % _KEY_SERIAL[0]
        raw = "sk-" + ("%04d" % _KEY_SERIAL[0]) + "a" * 40
        record = {
            "id": key_id,
            "name": payload["name"],
            "key_prefix": raw[:6] + "****",
            "workspace_id": WORKSPACE,
            "created_at": iso_now(),
            "last_used_at": None,
            "is_active": True,
            "spend_limit": ({
                "amount": int(limit["amount"]), "scale": 9, "currency": "USD"
            } if isinstance(limit, dict) else None),
            "usage": {"amount": self.usage, "scale": 9, "currency": "USD"},
        }
        self.keys[key_id] = record
        self.raw_keys.append(raw)
        return 201, json.dumps({**record, "key": raw}).encode()

    # -- assertions helpers ------------------------------------------------

    def actions(self) -> list[str]:
        return [f"{call['method']} {call['path']}" for call in self.calls]

    def deletes(self) -> list[str]:
        return [call["path"].rsplit("/", 1)[-1] for call in self.calls
                if call["method"] == "DELETE"]

    def mints(self) -> list[dict]:
        return [call for call in self.calls
                if call["method"] == "POST" and call["path"].endswith("/api-keys")]


class FakeMachine:
    """The provider's machine surface, shaped like the recorded fixtures."""

    def __init__(self, slug: str, server_id: str):
        self.slug = slug
        self.server_id = server_id
        self.transport = RecordedTransport({
            ("GET", "servers"): [(200, empty_servers_response())],
            ("POST", "create"): [(200, create_response(hostname=slug, server_id=server_id))],
            ("GET", "server"): [
                (200, server_row(status="deploying", ipv4_address=None,
                                 hostname=slug, id=server_id)),
                (200, server_row(hostname=slug, id=server_id)),
            ],
        })

    def provider(self, _config):
        return servury.ServuryProvider(TEST_KEY, transport=self.transport)


# ------------------------------------------------------------- the seed service


class SeedFixture(unittest.TestCase):
    """The real seed service, on localhost, over a throwaway database.

    One database for the class, because that is the shipped shape: the setup
    token ledger, the tenant rows and the state spine live in one SQLite file
    (`seed_db` and `state_db` are the same path in the live config).
    """

    SLUGS = (
        ("odl-keya", "srv-keya"),
        ("odl-keyb", "srv-keyb"),
        ("odl-keyc", "srv-keyc"),
        ("odl-keyd", "srv-keyd"),
        ("odl-keye", "srv-keye"),
        ("odl-keyf", "srv-keyf"),
        ("odl-keyg", "srv-keyg"),
        ("odl-keyh", "srv-keyh"),
        ("odl-keyi", "srv-keyi"),
        ("odl-keyj", "srv-keyj"),
        ("odl-keyk", "srv-keyk"),
    )

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="odile-keys-")
        directory = cls.tmp.name
        cls.db = os.path.join(directory, "waitlist.db")
        cls.signing_key = os.path.join(directory, "seed-signing.pem")
        cls.config = os.path.join(directory, "seed-config.json")
        cls.seed_keys = os.path.join(directory, "seed-keys.json")
        cls.rebuilds = os.path.join(directory, "seed-rebuilds.json")
        cls.credential = os.path.join(directory, "near-account.json")
        os.makedirs(os.path.join(directory, "etc"), exist_ok=True)

        with open(cls.credential, "w", encoding="utf-8") as handle:
            json.dump(dict(CREDENTIAL), handle)
        os.chmod(cls.credential, 0o600)

        # The seed config deliberately carries *no* near_api_key: every seed in
        # these tests must come from the per-instance handoff. ODL-469 made
        # mail_relay.tls_ca required, so the fixture carries a shape-valid PEM
        # (the relay's certificate is what a tenant pins).
        config = {
            "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
            "support_public_key": KEY_TEXT,
            "backup_repo_url": "sftp:odile-backup@backup.example:/tenants/{slug}",
            "hermes_version": "2026.11.1",
            "mail_relay": {"host": "smtp.example", "port": 587,
                           "username": "{slug}@mail.example", "secret": "relay-test",
                           "tls_ca": ("-----BEGIN CERTIFICATE-----\n"
                                      "MIIBhTCCASugAwIBAgIUTESTRELAYCA0000000000000000\n"
                                      "-----END CERTIFICATE-----\n")},
        }
        with open(cls.config, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        os.chmod(cls.config, 0o600)

        cls.release_key = os.path.join(directory, "release-signing.pem")
        cls.release_pub = cls.release_key + ".pub"
        cls.cert = os.path.join(directory, "seed-key.crt")
        subprocess.run([sys.executable, str(SEED), "keygen"],
                       env=cls.env(), capture_output=True, check=True, timeout=60)
        os.chmod(cls.signing_key, 0o600)
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519",
                        "-out", cls.release_key], check=True, capture_output=True)
        os.chmod(cls.release_key, 0o600)
        subprocess.run(["openssl", "pkey", "-in", cls.release_key, "-pubout",
                        "-out", cls.release_pub], check=True, capture_output=True)
        subprocess.run(["openssl", "dgst", "-sign", cls.release_key,
                        "-out", cls.cert, cls.signing_key + ".pub"],
                       check=True, capture_output=True)
        os.chmod(cls.cert, 0o644)
        subprocess.run([sys.executable, str(SEED), "init"],
                       env=cls.env(), capture_output=True, check=True, timeout=60)

        con = sqlite3.connect(cls.db)
        try:
            app.migrate(con)
            stamp = iso_now()
            for slug, server in cls.SLUGS:
                con.execute(
                    "INSERT INTO tenants (slug, email, server_id, created_at)"
                    " VALUES (?, ?, ?, ?)", (slug, slug + "@example.com", server, stamp))
            con.commit()
        finally:
            con.close()

        cls.port = free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, str(SEED), "serve"],
            env=dict(cls.env(), ODILE_SEED_PORT=str(cls.port), ODILE_SEED_BIND="127.0.0.1"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        wait_health(cls.port, cls.proc)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.communicate()
        cls.tmp.cleanup()

    @classmethod
    def env(cls) -> dict:
        environment = dict(os.environ)
        environment.update({
            "ODILE_DB": cls.db,
            "ODILE_SEED_CONFIG": cls.config,
            "ODILE_SEED_KEYS": cls.seed_keys,
            "ODILE_SEED_REBUILDS": cls.rebuilds,
            "ODILE_SEED_SIGNING_KEY": cls.signing_key,
            "ODILE_SEED_CERT": cls.cert,
            "ODILE_SEED_RELEASE_PUBKEY": cls.release_pub,
        })
        return environment

    # -- helpers -----------------------------------------------------------

    def make_config(self, **overrides) -> Path:
        """An odilectl config on the class database, with the NEAR account."""
        directory = tempfile.mkdtemp(prefix="odile-keys-config-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = Path(directory) / "config.yaml"
        values = {
            "provider": "servury",
            "reseller_key": TEST_KEY,
            "default_plan_id": "113",
            "default_days": "31",
            "state_db": self.db,
            "seed_db": self.db,
            "seed_keys": self.seed_keys,
            "backup_repo": "sftp://backup@example.invalid/odile",
            "dns_token": "fixture-token",
            "dns_zone": "odile.fyi",
            "dns_zone_id": "fixture-zone",
            "near_account": self.credential,
            "near_workspace_id": WORKSPACE,
            "near_org_id": ORG,
            "near_spend_limit_usd": "10",
        }
        values.update(overrides)
        path.write_text(
            "".join(f"{key}: {value}\n" for key, value in values.items() if value is not None),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def key_file(self) -> Path:
        directory = tempfile.mkdtemp(prefix="odile-keys-pub-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = Path(directory) / "tenant.pub"
        path.write_text(KEY_TEXT + "\n", encoding="utf-8")
        return path

    def pin_file(self) -> Path:
        directory = tempfile.mkdtemp(prefix="odile-keys-pin-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = Path(directory) / "odile-verify.pin"
        path.write_text(json.dumps({
            "schema": 2, "measurement": "ab" * 48, "provision_release": "2026.11.1",
            "releases": {"2026.11.1": {}},
        }), encoding="utf-8")
        return path

    def provision(self, slug: str, server_id: str, config: Path, *,
                  provider=None, extra=()):
        """Run the real provision command; remember the token it printed."""
        if not hasattr(self, "_tokens"):
            self._tokens = {}
        machine = FakeMachine(slug, server_id)
        out = io.StringIO()
        err = io.StringIO()
        code = cli.main(
            ["provision", slug, "--key", str(self.key_file()),
             "--pin", str(self.pin_file()), "--interval", "0", "--apply", "--json",
             "--config", str(config), *extra],
            provider_factory=provider or machine.provider,
            stdout=out, stderr=err,
        )
        match = re.search(r'"token": "([0-9a-f]{64})"', out.getvalue())
        if code == 0:
            self.assertIsNotNone(match, out.getvalue())
            self._tokens[slug] = match.group(1)
        self._out = out.getvalue()
        return code, err.getvalue(), machine

    def active_provider(self, slug: str, server_id: str):
        """A provider that lists this machine as active, for the reuse path."""
        class Reader:
            def __init__(self):
                self.transport = RecordedTransport({
                    ("GET", "servers"): [(200, {"success": True, "data": {
                        "servers": [{
                            "id": server_id, "hostname": slug, "plan_id": 113,
                            "ipv4_address": "203.0.113.120", "status": "active",
                            "powered_on": True, "snp_enabled": True,
                            "fde_enabled": True, "measured_boot": True,
                            "expiration_date": 1792000000, "expired": False,
                        }], "total": 1}})],
                    ("GET", "server"): [(200, server_row(hostname=slug, id=server_id))],
                })

            def provider(self, _config):
                return servury.ServuryProvider(TEST_KEY, transport=self.transport)

        return Reader()

    @contextlib.contextmanager
    def shared_fallback(self, value: str = "sk-shared-{slug}"):
        """Put the seed config's shared key in place for one test, then remove it.

        The fixture's config deliberately carries none, so the 503 path can be
        tested; a re-issued token must still never reach this value.
        """
        with open(self.config, encoding="utf-8") as handle:
            document = json.load(handle)
        document["near_api_key"] = value
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        os.chmod(self.config, 0o600)
        try:
            yield value
        finally:
            with open(self.config, encoding="utf-8") as handle:
                document = json.load(handle)
            document.pop("near_api_key", None)
            with open(self.config, "w", encoding="utf-8") as handle:
                json.dump(document, handle)
            os.chmod(self.config, 0o600)

    def redeem(self, token: str, instance_id: str):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        connection.request("POST", "/seed",
                           body=json.dumps({"token": token, "instance_id": instance_id}),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def handoff(self) -> dict:
        try:
            with open(self.seed_keys, encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return {}

    def run_seed(self, *args):
        """Run the real seed CLI against this fixture's database and handoff."""
        return subprocess.run(
            [sys.executable, str(SEED), *args], env=self.env(),
            capture_output=True, text=True, timeout=30,
        )

    def mint_raw_token_row(self, slug: str, instance_id: str, token: str) -> None:
        """Write a token row behind the mint commands' back, as a stale ledger would.

        `token issue`/`rotate`/`provision` all bind the tenant's key to the token
        they mint (ODL-242). A row inserted here is the shape a path that did not
        (an older release, a hand edit) leaves behind; the exchange must still
        never hand that machine the config's shared key.
        """
        stamp = iso_now()
        expires = datetime.fromtimestamp(
            time.time() + 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        con = sqlite3.connect(self.db)
        try:
            tenant_id = con.execute(
                "SELECT id FROM tenants WHERE slug = ?", (slug,)).fetchone()[0]
            con.execute(
                "INSERT INTO seed_tokens (tenant_id, token_hash, instance_id,"
                " created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (tenant_id, hashlib.sha256(token.encode("ascii")).hexdigest(),
                 instance_id, stamp, expires),
            )
            con.commit()
        finally:
            con.close()

    def token_rows(self, slug: str) -> list[tuple]:
        """(used_at, state) for a tenant's tokens, oldest first, from the ledger."""
        con = sqlite3.connect(self.db)
        try:
            return con.execute(
                "SELECT used_at, CASE WHEN used_at IS NULL THEN 'live' ELSE 'ended' END"
                " FROM seed_tokens JOIN tenants ON tenants.id = seed_tokens.tenant_id"
                " WHERE tenants.slug = ? ORDER BY seed_tokens.id", (slug,)).fetchall()
        finally:
            con.close()

    def db_bytes(self) -> bytes:
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("PRAGMA wal_checkpoint(FULL)")
        finally:
            connection.close()
        return Path(self.db).read_bytes()


def run_cli(argv, config_path, provider_factory=None):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv) + ["--config", str(config_path)],
                    provider_factory=provider_factory, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


# ------------------------------------------------------- issue at provision


class ProvisionIssueTest(SeedFixture):
    """Two provisions, two keys, each seed carrying only its own (DoD 1-2)."""

    def test_two_instances_get_two_keys_and_each_seed_its_own(self):
        config = self.make_config()
        near = FakeNear()
        with mock.patch.object(keys, "TRANSPORT", near):
            first_code, first_err, _ = self.provision("odl-keya", "srv-keya", config)
            second_code, second_err, _ = self.provision("odl-keyb", "srv-keyb", config)
        self.assertEqual(first_code, 0, first_err)
        self.assertEqual(second_code, 0, second_err)

        # Two mints, two different raw keys, each with the envelope's cap.
        minted = near.mints()
        self.assertEqual(len(minted), 2, near.actions())
        self.assertEqual(
            {call["body"]["spendLimit"]["amount"] for call in minted}, {SPEND_LIMIT}
        )
        self.assertEqual(
            [call["body"]["name"] for call in minted], ["odile-odl-keya", "odile-odl-keyb"]
        )

        waiting = self.handoff()
        token_a = self._tokens["odl-keya"]
        token_b = self._tokens["odl-keyb"]
        key_a = waiting["odl-keya"]["key"]
        key_b = waiting["odl-keyb"]["key"]
        self.assertNotEqual(key_a, key_b)
        self.assertTrue(key_a.startswith("sk-"), key_a)

        status_a, body_a = self.redeem(token_a, "srv-keya")
        status_b, body_b = self.redeem(token_b, "srv-keyb")
        self.assertEqual(status_a, 200, body_a)
        self.assertEqual(status_b, 200, body_b)
        seed_a = json.loads(body_a)
        seed_b = json.loads(body_b)
        self.assertEqual(seed_a["near_api_key"], key_a)
        self.assertEqual(seed_b["near_api_key"], key_b)
        self.assertNotIn(key_b, body_a.decode("utf-8"))
        self.assertNotIn(key_a, body_b.decode("utf-8"))

        # The exchange spent the handoff: HQ keeps no raw key afterwards.
        waiting_now = self.handoff()
        self.assertNotIn("odl-keya", waiting_now)
        self.assertNotIn("odl-keyb", waiting_now)

        # The record: label and first characters, never the raw key.
        store = state.StateStore.open(self.db)
        try:
            record_a = store.key_for("odl-keya")
            record_b = store.key_for("odl-keyb")
        finally:
            store.close()
        self.assertEqual(record_a.label, "odile-odl-keya")
        self.assertEqual(record_a.key_prefix, key_a[:6] + "****")
        self.assertEqual(record_a.state, "issued")
        self.assertEqual(record_a.instance_id, "srv-keya")
        self.assertEqual(record_a.spend_limit_nano, SPEND_LIMIT)
        self.assertNotEqual(record_a.key_id, record_b.key_id)
        # No raw key anywhere in the file: the recorded prefix is `sk-…****`,
        # so the test looks for the minted values themselves.
        data = self.db_bytes()
        for raw in near.raw_keys:
            self.assertNotIn(raw.encode("utf-8"), data, "a raw key reached the database")

    def test_the_machines_key_env_matches_its_seed(self):
        """The installer's own writer, on a scratch root, holds this seed's key."""
        config = self.make_config()
        near = FakeNear()
        with mock.patch.object(keys, "TRANSPORT", near):
            code, err, _ = self.provision("odl-keyc", "srv-keyc", config)
        self.assertEqual(code, 0, err)
        status, body = self.redeem(self._tokens["odl-keyc"], "srv-keyc")
        self.assertEqual(status, 200, body)
        seed = json.loads(body)

        installer = load_installer()
        scratch = Path(tempfile.mkdtemp(prefix="odile-keys-root-"))
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)

        class Layout:
            mode = "bench"

        layout = Layout()
        layout.etc = scratch / "etc"
        layout.etc.mkdir(parents=True)
        layout.key_env = layout.etc / "key.env"
        changed = installer.ensure_key_env(layout, seed, False)
        self.assertTrue(changed)
        self.assertEqual(
            layout.key_env.read_text(encoding="utf-8"),
            f"{installer.NEAR_KEY_ENV}={seed['near_api_key']}\n",
        )
        self.assertEqual(layout.key_env.stat().st_mode & 0o777, 0o600)
        # A second run is the same file: the installer converges.
        self.assertFalse(installer.ensure_key_env(layout, seed, False))

    def test_a_live_machine_keeps_its_key_and_a_second_run_mints_nothing(self):
        """A second run with the raw key still waiting re-binds it; it does not mint."""
        config = self.make_config()
        near = FakeNear()
        with mock.patch.object(keys, "TRANSPORT", near):
            code, err, machine = self.provision("odl-keyd", "srv-keyd", config)
            self.assertEqual(code, 0, err)
            before = near.minted
            # The provider now lists the machine as active: the second run is
            # the reuse path, and it must not revoke the key the machine spends.
            again = FakeMachine("odl-keyd", "srv-keyd")
            again.transport = RecordedTransport({
                ("GET", "servers"): [(200, {"success": True, "data": {
                    "servers": [{
                        "id": "srv-keyd", "hostname": "odl-keyd", "plan_id": 113,
                        "ipv4_address": "203.0.113.120", "status": "active",
                        "powered_on": True, "snp_enabled": True,
                        "fde_enabled": True, "measured_boot": True,
                        "expiration_date": 1792000000, "expired": False,
                    }], "total": 1}})],
                ("GET", "server"): [(200, server_row(hostname="odl-keyd",
                                                      id="srv-keyd"))],
            })
            code, err, _ = self.provision("odl-keyd", "srv-keyd", config,
                                          provider=again.provider)
        self.assertEqual(code, 0, err)
        self.assertEqual(near.minted, before, "the reuse path minted a second key")
        self.assertEqual(near.deletes(), [], "the reuse path revoked the live key")


def load_installer():
    """The installer module, loaded by path (it is a program, not a package)."""
    if "odile_install" in sys.modules:
        return sys.modules["odile_install"]
    path = ROOT / "installer" / "odile_install.py"
    spec = importlib.util.spec_from_file_location("odile_install", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["odile_install"] = module
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------- a re-issued token


class ReissueTest(SeedFixture):
    """A second run on a live machine: the re-issued token still seeds its own key.

    The verifier's round-4 must (ODL-242): `provision` mints a new setup token
    on a live machine, so the key in the handoff must be re-bound to that token
    — and when the raw key has already left HQ, a new key must be minted rather
    than falling back to the config's shared key. Both shapes are redeemed here,
    with the shared fallback present, so a fallback would be invisible no longer.
    """

    def test_a_reissued_token_rebinds_the_waiting_key_and_mints_nothing(self):
        """Shape B: the raw key still waits, so it moves to the new token."""
        config = self.make_config()
        near = FakeNear()
        with self.shared_fallback():
            with mock.patch.object(keys, "TRANSPORT", near):
                code, err, _ = self.provision("odl-keyg", "srv-keyg", config)
                self.assertEqual(code, 0, err)
                first_token = self._tokens["odl-keyg"]
                waiting = self.handoff()["odl-keyg"]
                minted = near.minted

                code, err, _ = self.provision(
                    "odl-keyg", "srv-keyg", config,
                    provider=self.active_provider("odl-keyg", "srv-keyg").provider,
                )
                self.assertEqual(code, 0, err)
                second_token = self._tokens["odl-keyg"]
                self.assertNotEqual(second_token, first_token)
                self.assertEqual(near.minted, minted, "the reuse path minted a second key")
                self.assertEqual(near.deletes(), [], "the reuse path revoked the live key")

                rebound = self.handoff()["odl-keyg"]
                self.assertEqual(rebound["key"], waiting["key"])
                self.assertEqual(rebound["key_id"], waiting["key_id"])
                self.assertEqual(
                    rebound["token_hash"],
                    hashlib.sha256(second_token.encode("utf-8")).hexdigest(),
                )
                self.assertEqual(rebound["instance_id"], "srv-keyg")

                status, body = self.redeem(second_token, "srv-keyg")
                self.assertEqual(status, 200, body)
                seed = json.loads(body)
                self.assertEqual(seed["near_api_key"], waiting["key"])
                self.assertNotEqual(seed["near_api_key"], shared_key("odl-keyg"))
                self.assertNotIn("odl-keyg", self.handoff(), "the exchange left a raw key at HQ")

                # The replaced token ended with the reissue (`bind` ends it).
                status_one, body_one = self.redeem(first_token, "srv-keyg")
                self.assertEqual(status_one, 410, body_one)

    def test_a_reissued_token_after_the_key_left_hq_mints_a_new_one(self):
        """Shape A: no raw key waits, so the reissue mints, revokes, and says so."""
        config = self.make_config()
        near = FakeNear()
        with self.shared_fallback():
            with mock.patch.object(keys, "TRANSPORT", near):
                code, err, _ = self.provision("odl-keyh", "srv-keyh", config)
                self.assertEqual(code, 0, err)
                first_token = self._tokens["odl-keyh"]
                status, body = self.redeem(first_token, "srv-keyh")
                self.assertEqual(status, 200, body)
                first_key = json.loads(body)["near_api_key"]
                self.assertEqual(self.handoff(), {}, "the first exchange left a raw key at HQ")
                store = state.StateStore.open(self.db)
                try:
                    first_record = store.key_for("odl-keyh")
                finally:
                    store.close()
                self.assertIsNotNone(first_record)

                code, err, _ = self.provision(
                    "odl-keyh", "srv-keyh", config,
                    provider=self.active_provider("odl-keyh", "srv-keyh").provider,
                )
                self.assertEqual(code, 0, err)
                second_token = self._tokens["odl-keyh"]
                self.assertNotEqual(second_token, first_token)
                self.assertEqual(len(near.mints()), 2, near.actions())
                # The key the running machine spends with was revoked at the
                # provider before the new one was minted: one live key per tenant.
                self.assertEqual(near.deletes(), [first_record.key_id], near.actions())
                self.assertIn("installed again", self._out)

                second_key = self.handoff()["odl-keyh"]["key"]
                self.assertNotEqual(second_key, first_key)
                status, body = self.redeem(second_token, "srv-keyh")
                self.assertEqual(status, 200, body)
                self.assertEqual(json.loads(body)["near_api_key"], second_key)
                self.assertNotEqual(json.loads(body)["near_api_key"], shared_key("odl-keyh"))
                self.assertNotIn("odl-keyh", self.handoff())

        store = state.StateStore.open(self.db)
        try:
            history = store.keys("odl-keyh")
        finally:
            store.close()
        self.assertEqual([record.state for record in history], ["revoked", "issued"])
        self.assertEqual(history[0].detail.get("reason"), "superseded")
        self.assertEqual(history[1].key_prefix, second_key[:6] + "****")
        self.assertNotIn(first_key, self.db_bytes().decode("utf-8", "replace"))


# ------------------------------------------------- a rotated / issued token


class RotateTest(SeedFixture):
    """`token rotate`/`token issue` on a tenant that owns an inference key.

    The verifier's round-5 must (ODL-242): the runbook prescribes rotation as a
    re-issue path next to `odilectl provision`, so the mint commands must carry
    the tenant's own key. When the raw key still waits, they re-bind it to the
    token they mint; when it has already left HQ, they refuse (`409
    key_left_hq`) instead of handing out a token whose seed would carry the
    config's shared key. Both shapes run with the shared fallback present, so a
    fallback cannot hide.
    """

    def test_rotate_on_a_keyed_tenant_rebinds_the_waiting_key(self):
        """The raw key still waits: rotate moves it to the new token, minting nothing."""
        config = self.make_config()
        near = FakeNear()
        with self.shared_fallback():
            with mock.patch.object(keys, "TRANSPORT", near):
                code, err, _ = self.provision("odl-keyi", "srv-keyi", config)
                self.assertEqual(code, 0, err)
                first_token = self._tokens["odl-keyi"]
                waiting = self.handoff()["odl-keyi"]
                minted = near.minted

            rotated = self.run_seed("token", "rotate", "--tenant", "odl-keyi",
                                    "--instance", "srv-keyi")
            self.assertEqual(rotated.returncode, 0, rotated.stderr)
            match = re.search(r"^token:\s+([0-9a-f]{64})$", rotated.stdout, re.M)
            self.assertIsNotNone(match, rotated.stdout)
            second_token = match.group(1)
            self.assertNotEqual(second_token, first_token)

            # The same raw key, bound to the token this run minted.
            rebound = self.handoff()["odl-keyi"]
            self.assertEqual(rebound["key"], waiting["key"])
            self.assertEqual(rebound["key_id"], waiting["key_id"])
            self.assertEqual(
                rebound["token_hash"],
                hashlib.sha256(second_token.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(rebound["instance_id"], "srv-keyi")
            self.assertEqual(near.minted, minted, "the seed service cannot mint a key")

            status, body = self.redeem(second_token, "srv-keyi")
            self.assertEqual(status, 200, body)
            seed = json.loads(body)
            self.assertEqual(seed["near_api_key"], waiting["key"])
            self.assertNotEqual(seed["near_api_key"], shared_key("odl-keyi"))
            self.assertNotIn("odl-keyi", self.handoff(), "the exchange left a raw key at HQ")

            # The token rotate ended no longer seeds anything.
            status_one, body_one = self.redeem(first_token, "srv-keyi")
            self.assertEqual(status_one, 410, body_one)

    def test_rotate_after_the_key_left_hq_refuses_before_minting_a_token(self):
        """No raw key waits: rotate and issue refuse, and the live token survives."""
        config = self.make_config()
        near = FakeNear()
        with self.shared_fallback(), mock.patch.object(keys, "TRANSPORT", near):
            code, err, _ = self.provision("odl-keyj", "srv-keyj", config)
            self.assertEqual(code, 0, err)
            first_token = self._tokens["odl-keyj"]
            status, body = self.redeem(first_token, "srv-keyj")
            self.assertEqual(status, 200, body)
            own = json.loads(body)["near_api_key"]
            self.assertEqual(self.handoff(), {}, "the first exchange left a raw key at HQ")

            # A live token to prove the refusal does not end it, then both verbs.
            self.mint_raw_token_row("odl-keyj", "srv-keyj", "ef" * 32)
            for verb in ("rotate", "issue"):
                refused = self.run_seed("token", verb, "--tenant", "odl-keyj",
                                        "--instance", "srv-keyj")
                self.assertNotEqual(refused.returncode, 0, refused.stdout)
                self.assertIn("key_left_hq", refused.stderr)
                self.assertIn("odilectl provision", refused.stderr)
                self.assertNotIn("token:", refused.stdout, refused.stdout)

            # Nothing was minted, and rotate did not end the token it found.
            rows = self.token_rows("odl-keyj")
            self.assertEqual(len(rows), 2, rows)
            self.assertEqual(rows[-1][1], "live", rows)
            self.assertEqual(self.handoff(), {})

            # `provision` is the remedy the refusal names: a new token that
            # carries a new key, and the running machine's old key is revoked.
            code, err, _ = self.provision(
                "odl-keyj", "srv-keyj", config,
                provider=self.active_provider("odl-keyj", "srv-keyj").provider,
            )
            self.assertEqual(code, 0, err + self._out)
            replacement = self._tokens["odl-keyj"]
            status, body = self.redeem(replacement, "srv-keyj")
            self.assertEqual(status, 200, body)
            self.assertNotEqual(json.loads(body)["near_api_key"], own)
            self.assertNotEqual(json.loads(body)["near_api_key"], shared_key("odl-keyj"))
            self.assertEqual(len(near.mints()), 2, near.actions())

    def test_a_token_minted_behind_the_service_never_carries_the_shared_key(self):
        """The exchange is the last guard: keyed tenant, no waiting key -> 503, unspent."""
        config = self.make_config()
        near = FakeNear()
        token = "cd" * 32
        with self.shared_fallback():
            with mock.patch.object(keys, "TRANSPORT", near):
                code, err, _ = self.provision("odl-keyk", "srv-keyk", config)
                self.assertEqual(code, 0, err)
                status, body = self.redeem(self._tokens["odl-keyk"], "srv-keyk")
                self.assertEqual(status, 200, body)
                self.assertEqual(self.handoff(), {})
            self.mint_raw_token_row("odl-keyk", "srv-keyk", token)

            status, body = self.redeem(token, "srv-keyk")
            self.assertEqual(status, 503, body)
            payload = json.loads(body)
            self.assertEqual(payload["reason"], "no_inference_key")
            self.assertIn("odilectl provision", payload["error"])
            self.assertNotIn(shared_key("odl-keyk"), body.decode("utf-8", "replace"))
            # A refusal never burns the token.
            self.assertEqual(self.token_rows("odl-keyk")[-1][0], None)


# -------------------------------------------------------------------- revoke


class RevokeTest(SeedFixture):
    """Destroy resolves the id from the list call and the old key cannot spend."""

    def _destroy(self, config: Path, near: FakeNear, slug: str, *extra):
        machine = FakeMachine(slug, "srv-" + slug.rsplit("-", 1)[-1])
        machine.transport = RecordedTransport({
            ("GET", "servers"): [(200, empty_servers_response())],
        })
        with mock.patch.object(keys, "TRANSPORT", near), \
                mock.patch("odilectl.offboard.DNS_TRANSPORT", FakeDNS()), \
                mock.patch("odilectl.offboard.VERIFY_FETCH", lambda _url: GRANT_NONE), \
                mock.patch("odilectl.offboard.SLEEP", lambda _seconds: None):
            return run_cli(["destroy", slug, "--skip-export", "--apply", "--json", *extra],
                           config, provider_factory=machine.provider)

    def test_destroy_revokes_the_key_the_list_call_names(self):
        config = self.make_config()
        near = FakeNear()
        with mock.patch.object(keys, "TRANSPORT", near):
            code, err, _ = self.provision("odl-keye", "srv-keye", config)
        self.assertEqual(code, 0, err)
        store = state.StateStore.open(self.db)
        try:
            record = store.key_for("odl-keye")
        finally:
            store.close()
        self.assertEqual(record.state, "issued")
        self.assertEqual(list(near.keys), [record.key_id])
        self.assertTrue(Path(self.seed_keys).is_file())
        self.assertIn("odl-keye", self.handoff())

        # The record's id is not the one the provider now lists: only the list
        # call can resolve the id, which is what the card requires.
        original = near.keys.pop(record.key_id)
        near.keys = {"key-listed": dict(original, id="key-listed")}

        code, out, err = self._destroy(config, near, "odl-keye")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["key"]["resolved"], "list")
        self.assertEqual(payload["key"]["key_id"], "key-listed")
        self.assertEqual(payload["key"]["status"], 204)
        self.assertEqual(near.deletes(), ["key-listed"])
        self.assertEqual(near.keys, {}, "the key is still active at the provider")
        # The label and prefix HQ held are what resolved it; the id came from
        # the list call, not from the record.
        self.assertEqual(payload["key"]["label"], record.label)
        self.assertEqual(payload["key"]["key_prefix"], record.key_prefix)
        self.assertNotEqual(payload["key"]["key_id"], record.key_id)
        listing = [call for call in near.calls
                   if call["method"] == "GET" and call["path"].endswith("/api-keys")]
        self.assertTrue(listing, near.actions())
        self.assertFalse(self.handoff(), "destroy left the raw key at HQ")

    def test_a_second_destroy_converges_without_another_delete(self):
        config = self.make_config()
        near = FakeNear()
        with mock.patch.object(keys, "TRANSPORT", near):
            self.provision("odl-keyf", "srv-keyf", config)
        code, _out, err = self._destroy(config, near, "odl-keyf")
        self.assertEqual(code, 0, err)
        deletes = list(near.deletes())
        code, out, err = self._destroy(config, near, "odl-keyf")
        self.assertEqual(code, 0, err)
        self.assertEqual(near.deletes(), deletes, "the second destroy deleted again")
        payload = json.loads(out)
        self.assertEqual(payload["key"]["resolved"], "record")
        self.assertEqual(payload["key"]["status"], 404)
        store = state.StateStore.open(self.db)
        try:
            record = store.key_for("odl-keyf")
        finally:
            store.close()
        self.assertEqual(record.state, "revoked")
        self.assertEqual(record.revoked_at is not None, True)


class FakeDNS:
    """Cloudflare with no A records: absent find, absent delete."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append(method)
        return 200, json.dumps({"success": True, "result": []}).encode()


GRANT_NONE = (
    b"  operator key   PASS  none (authorized_keys: 1 line(s), the measured throwaway)\n"
)


# ----------------------------------------------------------------- allowance


class AllowanceTest(SeedFixture):
    """The two 402 classes, read from the provider's own numbers."""

    def _account(self):
        return keys.Account(credential_path=self.credential, workspace_id=WORKSPACE,
                            org_id=ORG, spend_limit_nano=SPEND_LIMIT)

    def test_at_the_keys_limit_the_sentence_names_the_spend_and_the_limit(self):
        near = FakeNear(usage=SPEND_LIMIT + 2_180, key_limit=SPEND_LIMIT)
        with mock.patch.object(keys, "TRANSPORT", near):
            reading = keys.allowance(self._account(), {
                "usage": {"amount": SPEND_LIMIT + 2_180},
                "spend_limit": {"amount": SPEND_LIMIT},
            })
        self.assertEqual(reading["state"], "at_limit")
        self.assertIn("$10", reading["sentence"])
        self.assertIn("402 api_key_limit_exceeded", reading["sentence"])
        self.assertEqual(reading["spent_nano"], SPEND_LIMIT + 2_180)

    def test_at_zero_credit_the_sentence_names_the_other_refusal(self):
        near = FakeNear(remaining=None)
        with mock.patch.object(keys, "TRANSPORT", near):
            reading = keys.allowance(self._account(), {
                "usage": {"amount": 0}, "spend_limit": {"amount": SPEND_LIMIT},
            })
        self.assertEqual(reading["state"], "no_credit")
        self.assertIn("402 no_limit_configured", reading["sentence"])
        self.assertIn("top up", reading["sentence"])

    def test_the_command_reports_the_record_and_the_allowance(self):
        config = self.make_config()
        near = FakeNear()
        with mock.patch.object(keys, "TRANSPORT", near):
            self.provision("odl-keya", "srv-keya", config)
            code, out, err = run_cli(["keys", "odl-keya", "--check", "--json"], config)
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["record"]["label"], "odile-odl-keya")
        self.assertTrue(payload["record"]["key_prefix"].endswith("****"))
        for raw in near.raw_keys:
            self.assertNotIn(raw, out, "the command printed a raw key")
        self.assertEqual(payload["provider"]["allowance"]["state"], "ok")
        self.assertTrue(payload["provider"]["key"]["is_active"])


# ------------------------------------------------------------ the credential


class CredentialTest(SeedFixture):
    """A 401 rotates the pair and persists it before the retried call."""

    def test_a_401_refreshes_once_and_the_rotated_pair_is_persisted(self):
        near = FakeNear()
        near.expire_access_once = True
        account = keys.Account(credential_path=self.credential, workspace_id=WORKSPACE,
                               org_id=ORG, spend_limit_nano=SPEND_LIMIT)
        with mock.patch.object(keys, "TRANSPORT", near):
            issued = keys.issue(account, "odl-cred")
        self.assertTrue(issued.raw_key.startswith("sk-"))
        self.assertEqual(near.mints()[0]["headers"]["authorization"],
                         "Bearer " + near.access_token)
        refreshed = json.loads(Path(self.credential).read_text(encoding="utf-8"))
        self.assertNotEqual(refreshed["access_token"], CREDENTIAL["access_token"])
        self.assertNotEqual(refreshed["refresh_token"], CREDENTIAL["refresh_token"])
        self.assertEqual(refreshed["user_agent"], CREDENTIAL["user_agent"])
        self.assertEqual(Path(self.credential).stat().st_mode & 0o777, 0o600)
        # A second call uses the persisted token, not the stale one.
        with mock.patch.object(keys, "TRANSPORT", near):
            keys.list_keys(account)
        self.assertEqual(near.calls[-1]["headers"]["authorization"],
                         "Bearer " + near.access_token)

    def test_a_world_readable_credential_is_refused(self):
        path = Path(tempfile.mkdtemp(prefix="odile-keys-cred-")) / "near-account.json"
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        path.write_text(json.dumps(dict(CREDENTIAL)), encoding="utf-8")
        path.chmod(0o644)
        with self.assertRaisesRegex(Exception, "chmod 600"):
            keys.load_credential(str(path))

    def test_the_config_switch_rules_the_mint(self):
        config = self.make_config()
        loaded = keys.account_from_config(Config.load(config))
        self.assertEqual(loaded.workspace_id, WORKSPACE)
        self.assertEqual(loaded.spend_limit_nano, SPEND_LIMIT)
        off = self.make_config(near_account=None)
        self.assertIsNone(keys.account_from_config(Config.load(off)))


# ------------------------------------------------------------- the seed edge


class SeedEdgeTest(SeedFixture):
    """A seed with no key refuses without spending the token."""

    def test_no_key_anywhere_is_503_and_the_token_still_works(self):
        config = self.make_config(near_account=None)
        code, err, _ = self.provision("odl-keyb", "srv-keyb", config)
        self.assertEqual(code, 0, err)
        token = self._tokens["odl-keyb"]
        status, body = self.redeem(token, "srv-keyb")
        self.assertEqual(status, 503, body)
        self.assertEqual(json.loads(body)["reason"], "no_inference_key")

        # Put a key in place for *this* token, the way provision's bind step
        # does after the order: the same token still redeems, and the seed
        # carries the key minted for it.
        seed = seed_module()
        seed.set_issued_key(self.seed_keys, "odl-keyb", {
            "key": "sk-late-key-0001", "label": "odile-odl-keyb",
            "key_id": "key-late", "prefix": "sk-lat****",
            "workspace_id": WORKSPACE,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "instance_id": "srv-keyb", "created_at": iso_now(),
        })
        status, body = self.redeem(token, "srv-keyb")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["near_api_key"], "sk-late-key-0001")

    def test_a_key_bound_to_another_token_is_not_handed_over(self):
        seed = seed_module()
        path = os.path.join(tempfile.mkdtemp(prefix="odile-keys-handoff-"),
                            "seed-keys.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), ignore_errors=True)
        seed.set_issued_key(path, "odl-keyc", {
            "key": "sk-bound-elsewhere", "label": "odile-odl-keyc",
            "key_id": "key-x", "prefix": "sk-bou****", "workspace_id": WORKSPACE,
            "token_hash": "a" * 64, "instance_id": "srv-keyc",
            "created_at": iso_now(),
        })
        self.assertIsNone(seed.issued_key_for(seed.load_keys(path), "odl-keyc", "b" * 64))
        self.assertIsNotNone(seed.issued_key_for(seed.load_keys(path), "odl-keyc", "a" * 64))
        # The exchange for another token leaves the entry alone.
        self.assertIsNone(seed.take_issued_key(path, "odl-keyc", "b" * 64))
        self.assertIn("odl-keyc", seed.load_keys(path))

    def test_the_shared_config_key_is_still_the_bench_fallback(self):
        seed = seed_module()
        config = {"near_api_key": "sk-shared-{slug}"}
        self.assertEqual(seed.near_api_key(config, None, "odl-keyd"), "sk-shared-odl-keyd")
        with self.assertRaises(seed.SeedError) as caught:
            seed.near_api_key({}, None, "odl-keyd")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.reason, "no_inference_key")


# ------------------------------------------------------------- the store


class StoreTest(unittest.TestCase):
    """The key record's own invariants, without a provider or a service."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="odile-keys-store-")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.store = state.StateStore.open(os.path.join(self.directory, "state.db"))
        self.addCleanup(self.store.close)

    def record(self, slug: str, key_id: str) -> state.TenantKey:
        return self.store.record_key(
            slug, label=f"odile-{slug}", key_id=key_id, key_prefix=key_id[:6] + "****",
            workspace_id=WORKSPACE, spend_limit_nano=SPEND_LIMIT,
        )

    def test_one_live_key_per_tenant_and_a_rebuild_supersedes(self):
        first = self.record("odl-store", "key-0001")
        self.assertEqual(first.state, "issued")
        second = self.record("odl-store", "key-0002")
        self.assertEqual(second.state, "issued")
        rows = self.store.keys("odl-store")
        self.assertEqual([row.key_id for row in rows], ["key-0001", "key-0002"])
        self.assertEqual(rows[0].state, "revoked")
        self.assertIn("superseded", rows[0].detail["reason"])
        self.assertEqual(rows[1].state, "issued")

    def test_the_same_key_id_twice_does_not_move_its_issue_time(self):
        first = self.record("odl-store", "key-0003")
        again = self.record("odl-store", "key-0003")
        self.assertEqual(first.issued_at, again.issued_at)
        self.assertEqual(len(self.store.keys("odl-store")), 1)

    def test_revoking_twice_changes_nothing_and_keeps_the_first_stamp(self):
        record = self.record("odl-store", "key-0004")
        first = self.store.revoke_key("key-0004", reason="destroy")
        second = self.store.revoke_key("key-0004", reason="destroy")
        self.assertEqual(first.state, "revoked")
        self.assertEqual(first.revoked_at, second.revoked_at)
        self.assertEqual(record.issued_at, second.issued_at)

    def test_the_record_is_written_once(self):
        self.record("odl-store", "key-0005")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE tenant_keys SET key_prefix = 'sk-moved' WHERE key_id = 'key-0005'"
            )
            self.store.connection.commit()
        self.store.connection.rollback()


if __name__ == "__main__":
    unittest.main()
