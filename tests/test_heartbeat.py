#!/usr/bin/python3
"""Tests for ODL-216, re-cut by ODL-467: the signed heartbeat and the poll.

Hermetic: a scratch SQLite file, a generated Ed25519 pair, a fake SSH server
that speaks the unencrypted half of the key exchange, a fake SMTP server and a
fake Matrix homeserver.  No tenant machine, no live API, no sleeps.

The suite is organised around the card's five lines:

* a fixture heartbeat stop at the ten-minute threshold alerts `down`;
* a sealed reboot between two beats raises no state event and no unlock email,
  and the Webapp URL (the hostname) is unchanged;
* a stopped heartbeat is `down` whatever port 22 answers; port 22 is a symptom;
* a missed 07:00 brief is folded into `down`, or is its own alert while live;
* a filled disk, a spend spike and a stale backup each alert, and every alert
  names the tenant, the slug, the symptom and the first command.

    /usr/bin/python3 -m unittest tests.test_heartbeat -v
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odilectl import cli, health, state  # noqa: E402


def load_heartbeat_script():
    """Load `scripts/heartbeat.py` as a module; it is a script, not a package."""
    path = ROOT / "scripts" / "heartbeat.py"
    spec = importlib.util.spec_from_file_location("odile_heartbeat", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


heartbeat = load_heartbeat_script()

# 08:00 UTC on 8 November 2026.  A fixed clock keeps every assertion
# explainable and makes the brief deadline visible instead of accidental.
NOW = int(datetime(2026, 11, 8, 8, 0, tzinfo=timezone.utc).timestamp())
DAY = 24 * 60 * 60
STOP = state.HEARTBEAT_STOP_SECONDS


class Clock:
    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# A real ed25519 *host* key blob shaped as SSH would send it.  The private
# half never existed; a host key blob is public, and the probe only hashes it.
TEST_HOST_BLOB = health._string(b"ssh-ed25519") + health._string(bytes(range(32)))
TEST_HOST_B64 = base64.b64encode(TEST_HOST_BLOB).decode("ascii")
TEST_HOST_FP = health.ssh_fingerprint(TEST_HOST_BLOB)
OTHER_HOST_BLOB = health._string(b"ssh-ed25519") + health._string(bytes(range(1, 33)))
OTHER_HOST_B64 = base64.b64encode(OTHER_HOST_BLOB).decode("ascii")
OTHER_HOST_FP = health.ssh_fingerprint(OTHER_HOST_BLOB)


def observation(fingerprint: str = TEST_HOST_FP, *, reachable: bool = True) -> health.HostKeyObservation:
    if not reachable:
        return health.HostKeyObservation(False, (), "connection refused")
    return health.HostKeyObservation(
        True, (health.HostKey("ssh-ed25519", TEST_HOST_B64, fingerprint),)
    )


def facts(
    *,
    disk: float | None = None,
    spent: float | None = None,
    cap: float = 100.0,
    backup_at: int | None = None,
    brief_at: int | None = None,
    uptime: int = 3600,
    cause: str | None = None,
    boot: dict | None = None,
) -> dict:
    payload: dict = {"versions": {"odile": "0.1.0", "hermes": "0.9"}, "uptime_seconds": uptime}
    if disk is not None:
        payload["disk"] = {"total_bytes": 100_000_000_000, "used_bytes": int(disk * 1_000_000_000), "percent": disk}
    if spent is not None:
        payload["spend"] = {"spent_usd": spent, "cap_usd": cap}
    if backup_at is not None:
        payload["last_backup_at"] = backup_at
    if brief_at is not None:
        payload["last_brief_at"] = brief_at
    if cause is not None:
        payload["cause"] = cause
    if boot is not None:
        payload["boot"] = boot
    return payload


def boot_fact(release: str = "2026.11.1", measurement: str = "11" * 48) -> dict:
    return {"release": release, "measurement": measurement}


class TempStoreMixin(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="odile-heartbeat-")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.db = Path(self.directory) / "waitlist.db"
        self.clock = Clock()
        self.store = state.StateStore.open(self.db, clock=self.clock)
        self.addCleanup(self.store.close)
        self.beats = health.HeartbeatStore.open(
            self.db, clock=self.clock, verifier=heartbeat.verify_ed25519
        )
        self.addCleanup(self.beats.close)
        self.keydir = Path(self.directory) / "keys"
        self.private, self.public = heartbeat.keygen(self.keydir)
        self.public_pem = self.public.read_text(encoding="utf-8")

    def active_tenant(
        self,
        slug: str = "odl-7f3a2c",
        *,
        ipv4: str = "203.0.113.120",
        initramfs: str | None = TEST_HOST_FP,
        owner: str = "ada@example.com",
        at: int = NOW,
        release: str | None = None,
    ) -> state.Tenant:
        self.clock.now = at
        self.store.invite(slug, detail={"owner_email": owner})
        self.store.claim(slug)
        self.store.provision(
            slug,
            server_id=f"srv-{slug[-6:]}",
            hostname=slug,
            ipv4=ipv4,
            plan_id=113,
            release=release,
            expires_at=at + 30 * DAY,
            initramfs_host_key=initramfs,
        )
        self.store.await_boot(slug)
        self.store.start_first_visit(slug)
        return self.store.ready(slug)

    def first_visit_tenant(
        self,
        slug: str = "odl-7f3a2c",
        *,
        ipv4: str = "203.0.113.120",
        owner: str = "ada@example.com",
        at: int = NOW,
    ) -> state.Tenant:
        """Walk one tenant to `first_visit` — provisioned, and never ready."""
        self.clock.now = at
        self.store.invite(slug, detail={"owner_email": owner})
        self.store.claim(slug)
        self.store.provision(
            slug,
            server_id=f"srv-{slug[-6:]}",
            hostname=slug,
            ipv4=ipv4,
            plan_id=113,
            release=None,
            expires_at=at + 30 * DAY,
            initramfs_host_key=TEST_HOST_FP,
        )
        self.store.await_boot(slug)
        return self.store.start_first_visit(slug)

    def beat(
        self,
        slug: str = "odl-7f3a2c",
        *,
        at: int = NOW,
        payload: dict | None = None,
        private: Path | None = None,
        public_pem: str | None = None,
        key: str = "first",
    ) -> bytes:
        body_dict = {
            "slug": slug,
            "at": at,
            "facts": payload if payload is not None else facts(),
        }
        if key == "first":
            body_dict["public_key"] = public_pem or self.public_pem
        body = json.dumps(body_dict, sort_keys=True).encode("utf-8")
        signature = heartbeat.sign_ed25519(body, private or self.private)
        self.beats.record(body, signature, now=at)
        return body


# ---------------------------------------------------------------------------
# A fake SSH server: the unencrypted half of the key exchange, nothing more
# ---------------------------------------------------------------------------


def _negotiate(client_kexinit: dict[str, list[str]], server_algorithms: Sequence[str]) -> str:
    for name in client_kexinit.get("kex", []):
        if name in server_algorithms:
            return name
    raise AssertionError("client and fake server share no key exchange algorithm")


class FakeSSHServer:
    """Answers one host key; parses the client's KEXINIT and KEXDH_INIT."""

    def __init__(
        self,
        host_key_blob: bytes = TEST_HOST_BLOB,
        *,
        host_key_algorithms: Sequence[str] = ("ssh-ed25519",),
        kex_algorithms: Sequence[str] = (health.KEX_GROUP14,),
    ) -> None:
        self.host_key_blob = host_key_blob
        self.host_key_algorithms = tuple(host_key_algorithms)
        self.kex_algorithms = tuple(kex_algorithms)
        self.port = 0
        self.errors: list[str] = []
        self.client_kexinit: dict[str, list[str]] | None = None
        self.client_kex_algorithm: str | None = None
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = False

    def __enter__(self) -> "FakeSSHServer":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.2)
        self.port = listener.getsockname()[1]
        self._listener = listener
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        if self._listener is not None:
            self._listener.close()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._listener.accept()  # type: ignore[union-attr]
            except (TimeoutError, OSError):
                continue
            try:
                self._handle(conn)
            except Exception as error:  # recorded and asserted by the test
                self.errors.append(f"{type(error).__name__}: {error}")
            finally:
                conn.close()

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(5)
        conn.sendall(b"SSH-2.0-FakeSSH_1.0\r\n")
        client_banner = health._read_line(conn)
        if not client_banner.startswith(b"SSH-2.0-"):
            raise AssertionError(f"client banner {client_banner!r} is wrong")
        reader = health._Reader(health._socket_source(conn))
        payload = health._read_packet(reader)
        self.client_kexinit, _ = health._parse_kexinit(payload)
        health._write_packet(
            conn,
            health._build_kexinit(self.kex_algorithms, self.host_key_algorithms),
        )
        self.client_kex_algorithm = _negotiate(
            self.client_kexinit or {}, self.kex_algorithms
        )
        payload = health._read_packet(reader)
        if payload[0] != health.SSH_MSG_KEXDH_INIT:
            raise AssertionError(f"expected KEXDH_INIT, got {payload[0]}")
        client = health._Reader(health._bytes_source(payload[1:]))
        signature = health._string(b"ssh-ed25519") + health._string(b"\x00" * 64)
        if self.client_kex_algorithm == health.KEX_GROUP14:
            exponent = client.mpint()
            if not 1 < exponent < health.GROUP14_PRIME - 1:
                raise AssertionError("client DH value is out of range")
            f = pow(health.GROUP14_GENERATOR, 424242, health.GROUP14_PRIME)
            reply = (
                bytes([health.SSH_MSG_KEXDH_REPLY])
                + health._string(self.host_key_blob)
                + health._mpint(f)
                + health._string(signature)
            )
        else:
            public = client.string()
            if len(public) != 32:
                raise AssertionError("client X25519 public key is not 32 bytes")
            _, server_public = health._x25519_public_key()
            reply = (
                bytes([health.SSH_MSG_KEXDH_REPLY])
                + health._string(self.host_key_blob)
                + health._string(server_public)
                + health._string(signature)
            )
        health._write_packet(conn, reply)


def closed_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------------------
# The signing key and the heartbeat store
# ---------------------------------------------------------------------------


class KeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="odile-key-")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def test_keygen_writes_a_private_key_0600_and_a_public_key(self) -> None:
        private, public = heartbeat.keygen(self.directory)
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)
        self.assertEqual(public.stat().st_mode & 0o777, 0o644)
        self.assertIn("BEGIN PRIVATE KEY", private.read_text(encoding="utf-8"))
        self.assertTrue(
            public.read_text(encoding="utf-8").startswith("-----BEGIN PUBLIC KEY-----")
        )

    def test_keygen_is_idempotent_and_keeps_the_same_key(self) -> None:
        private, public = heartbeat.keygen(self.directory)
        first = private.read_bytes()
        again_private, again_public = heartbeat.keygen(self.directory)
        self.assertEqual(again_private, private)
        self.assertEqual(again_public, public)
        self.assertEqual(private.read_bytes(), first)

    def test_a_signature_covers_the_exact_bytes(self) -> None:
        private, _ = heartbeat.keygen(self.directory)
        public_pem = (Path(self.directory) / "heartbeat.pub").read_text(encoding="utf-8")
        body = b'{"slug":"odl-7f3a2c","at":1}'
        signature = heartbeat.sign_ed25519(body, private)
        self.assertEqual(len(signature), 64)
        self.assertTrue(heartbeat.verify_ed25519(body, signature, public_pem))
        self.assertFalse(heartbeat.verify_ed25519(body + b" ", signature, public_pem))
        other = signature[:-1] + bytes([signature[-1] ^ 0x01])
        self.assertNotEqual(other, signature)
        self.assertFalse(heartbeat.verify_ed25519(body, other, public_pem))

    def test_a_group_readable_private_key_is_refused(self) -> None:
        private, _ = heartbeat.keygen(self.directory)
        private.chmod(0o644)
        with self.assertRaises(SystemExit):
            heartbeat.sign_ed25519(b"x", private)


class HeartbeatStoreTest(TempStoreMixin):
    def test_the_first_heartbeat_registers_the_public_half(self) -> None:
        self.beat()
        self.assertEqual(self.beats.public_key("odl-7f3a2c"), self.public_pem.strip())
        self.assertEqual(self.beats.heartbeat_count("odl-7f3a2c"), 1)
        latest = self.beats.latest("odl-7f3a2c")
        assert latest is not None
        self.assertEqual(latest.at, NOW)
        self.assertEqual(latest.facts["uptime_seconds"], 3600)

    def test_a_later_heartbeat_verifies_against_the_registered_key(self) -> None:
        self.beat()
        self.beat(at=NOW + 300, key="later")
        self.assertEqual(self.beats.heartbeat_count(), 2)

    def test_a_different_key_is_a_conflict_not_a_reregistration(self) -> None:
        self.beat()
        other_dir = Path(self.directory) / "other"
        other_private, other_public = heartbeat.keygen(other_dir)
        with self.assertRaises(health.HeartbeatRejected) as caught:
            self.beat(at=NOW + 300, private=other_private, public_pem=other_public.read_text())
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.beats.heartbeat_count(), 1)
        self.assertEqual(self.beats.public_key("odl-7f3a2c"), self.public_pem.strip())

    def test_a_bad_signature_is_rejected(self) -> None:
        body = json.dumps(
            {"slug": "odl-7f3a2c", "at": NOW, "facts": {}, "public_key": self.public_pem},
            sort_keys=True,
        ).encode()
        with self.assertRaises(health.HeartbeatRejected) as caught:
            self.beats.record(body, b"\x00" * 64, now=NOW)
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(self.beats.heartbeat_count(), 0)

    def test_a_replayed_or_older_heartbeat_is_rejected(self) -> None:
        self.beat()
        with self.assertRaises(health.HeartbeatRejected) as caught:
            self.beat(at=NOW, key="later")
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(health.HeartbeatRejected):
            self.beat(at=NOW - 10, key="later")
        self.assertEqual(self.beats.heartbeat_count(), 1)

    def test_a_heartbeat_from_the_future_is_rejected(self) -> None:
        body = json.dumps(
            {"slug": "odl-7f3a2c", "at": NOW + 301, "facts": {}, "public_key": self.public_pem},
            sort_keys=True,
        ).encode()
        signature = heartbeat.sign_ed25519(body, self.private)
        with self.assertRaises(health.HeartbeatRejected) as caught:
            self.beats.record(body, signature, now=NOW)
        self.assertEqual(caught.exception.status, 400)

    def test_the_first_heartbeat_must_carry_the_public_half(self) -> None:
        body = json.dumps({"slug": "odl-7f3a2c", "at": NOW, "facts": {}}, sort_keys=True).encode()
        signature = heartbeat.sign_ed25519(body, self.private)
        with self.assertRaises(health.HeartbeatRejected) as caught:
            self.beats.record(body, signature, now=NOW)
        self.assertEqual(caught.exception.status, 400)

    def test_a_malformed_fact_is_rejected_at_the_door(self) -> None:
        body = json.dumps(
            {
                "slug": "odl-7f3a2c",
                "at": NOW,
                "facts": {"disk": {"percent": 140}},
                "public_key": self.public_pem,
            },
            sort_keys=True,
        ).encode()
        signature = heartbeat.sign_ed25519(body, self.private)
        with self.assertRaises(health.HeartbeatRejected) as caught:
            self.beats.record(body, signature, now=NOW)
        self.assertEqual(caught.exception.status, 400)

    def test_the_newest_beat_is_the_latest(self) -> None:
        self.beat()
        self.beat(at=NOW + 60, key="later")
        latest = self.beats.latest("odl-7f3a2c")
        assert latest is not None
        self.assertEqual(latest.at, NOW + 60)
        self.assertEqual(self.beats.latest("odl-nobody"), None)

    def test_a_boot_fact_is_stored_and_latest_exposes_it(self) -> None:
        self.beat(payload=facts(boot=boot_fact()))
        latest = self.beats.latest("odl-7f3a2c")
        assert latest is not None
        self.assertEqual(latest.boot, boot_fact())

    def test_a_malformed_boot_object_is_rejected_400(self) -> None:
        malformed = (
            "2026.11.1",
            {},
            {"release": "2026.11.1"},
            {"release": 2026.11, "measurement": "11" * 48},
            {"release": "2026.11.1", "measurement": "not-hex"},
            {"release": "2026.11.1", "measurement": "11" * 47},
        )
        for boot in malformed:
            with self.subTest(boot=boot):
                body = json.dumps(
                    {
                        "slug": "odl-7f3a2c",
                        "at": NOW,
                        "facts": {"boot": boot},
                        "public_key": self.public_pem,
                    },
                    sort_keys=True,
                ).encode()
                signature = heartbeat.sign_ed25519(body, self.private)
                with self.assertRaises(health.HeartbeatRejected) as caught:
                    self.beats.record(body, signature, now=NOW)
                self.assertEqual(caught.exception.status, 400)
                self.assertIn("boot", str(caught.exception))


# ---------------------------------------------------------------------------
# The receiver and the guest's post, end to end over a real socket
# ---------------------------------------------------------------------------


class ReceiverTest(TempStoreMixin):
    def setUp(self) -> None:
        super().setUp()
        self.server = heartbeat.HeartbeatServer(("127.0.0.1", 0), self.beats)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _post(self, body: bytes, signature: str) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            "/heartbeat",
            body=body,
            headers={"Content-Type": "application/json", "X-Odile-Signature": signature},
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def test_a_signed_post_is_accepted_and_health_reports_it(self) -> None:
        body = json.dumps(
            {"slug": "odl-7f3a2c", "at": NOW, "facts": facts(), "public_key": self.public_pem},
            sort_keys=True,
        ).encode()
        signature = base64.b64encode(heartbeat.sign_ed25519(body, self.private)).decode()
        status, payload = self._post(body, signature)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["slug"], "odl-7f3a2c")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", "/health")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["heartbeats"], 1)
        connection.close()

    def test_a_tampered_body_is_401(self) -> None:
        body = json.dumps(
            {"slug": "odl-7f3a2c", "at": NOW, "facts": {}, "public_key": self.public_pem},
            sort_keys=True,
        ).encode()
        signature = base64.b64encode(heartbeat.sign_ed25519(body, self.private)).decode()
        status, payload = self._post(body + b" ", signature)
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])

    def test_a_plain_heartbeat_answer_keeps_its_four_keys(self) -> None:
        # ODL-950: the upgrade booking adds exactly one key to the answer; a
        # beat with no `facts.upgrade` keeps the four it had.
        body = json.dumps(
            {"slug": "odl-7f3a2c", "at": NOW, "facts": facts(), "public_key": self.public_pem},
            sort_keys=True,
        ).encode()
        signature = base64.b64encode(heartbeat.sign_ed25519(body, self.private)).decode()
        status, payload = self._post(body, signature)
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"ok", "slug", "at", "received_at"})

    def test_a_malformed_boot_object_is_a_400_on_the_wire(self) -> None:
        body = json.dumps(
            {
                "slug": "odl-7f3a2c",
                "at": NOW,
                "facts": {"boot": {"release": "2026.11.1", "measurement": "short"}},
                "public_key": self.public_pem,
            },
            sort_keys=True,
        ).encode()
        signature = base64.b64encode(heartbeat.sign_ed25519(body, self.private)).decode()
        status, payload = self._post(body, signature)
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("facts.boot.measurement", payload["error"])
        self.assertEqual(self.beats.heartbeat_count(), 0)

    def test_the_guest_post_verb_lands_a_heartbeat(self) -> None:
        body_path = Path(self.directory) / "body.json"
        body_path.write_text(
            json.dumps(
                {"slug": "odl-7f3a2c", "at": NOW, "facts": facts(), "public_key": self.public_pem},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            code = heartbeat.main(
                [
                    "post",
                    "--url",
                    f"http://127.0.0.1:{self.port}/heartbeat",
                    "--body",
                    str(body_path),
                    "--key",
                    str(self.private),
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(self.beats.heartbeat_count(), 1)

    def test_a_wrong_path_is_404(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("POST", "/nope", body=b"{}")
        response = connection.getresponse()
        self.assertEqual(response.status, 404)
        connection.close()


class CollectTest(unittest.TestCase):
    def test_collect_reads_versions_disk_uptime_backup_and_brief(self) -> None:
        directory = tempfile.mkdtemp(prefix="odile-collect-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        root = Path(directory)
        version = root / "version.json"
        version.write_text(json.dumps({"odile": "0.1.0", "hermes": "0.9"}), encoding="utf-8")
        backup = root / "backup.json"
        backup.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "status": "ok",
                    "last_success_at": NOW - 3600,
                    "repo": "sftp:odile-backup@backup.odile.fyi:/tenants/odl-7f3a2c",
                    "snapshot": "1aa3734b7902881d1947031a5875fd2ce125bed994332c7324173899a0de5617",
                    "repo_bytes": 1017660723,
                    # Not a fact HQ may hold; the tuple must drop it.
                    "password": "not-a-real-password",
                }
            ),
            encoding="utf-8",
        )
        brief = root / "brief.json"
        brief.write_text(json.dumps({"last_sent_at": NOW - 60}), encoding="utf-8")
        public = root / "heartbeat.pub"
        public.write_text("-----BEGIN PUBLIC KEY-----\nfixture\n-----END PUBLIC KEY-----\n")
        args = Namespace(
            slug="odl-7f3a2c",
            at=NOW,
            disk=directory,
            backup_status=str(backup),
            brief_status=str(brief),
            version_file=str(version),
            odile_version=None,
            hermes_version=None,
            cause="kernel update",
            public_key=str(public),
        )
        payload = heartbeat.collect(args)
        self.assertEqual(payload["slug"], "odl-7f3a2c")
        self.assertEqual(payload["at"], NOW)
        self.assertEqual(payload["facts"]["versions"]["odile"], "0.1.0")
        self.assertEqual(payload["facts"]["last_backup_at"], NOW - 3600)
        self.assertEqual(
            payload["facts"]["backup"]["snapshot"],
            "1aa3734b7902881d1947031a5875fd2ce125bed994332c7324173899a0de5617",
        )
        self.assertEqual(payload["facts"]["backup"]["repo_bytes"], 1017660723)
        self.assertEqual(
            payload["facts"]["backup"]["repo"],
            "sftp:odile-backup@backup.odile.fyi:/tenants/odl-7f3a2c",
        )
        # The allowed-fields tuple is the whole filter: a stray key never ships.
        self.assertNotIn("password", payload["facts"]["backup"])
        self.assertNotIn("schema", payload["facts"]["backup"])
        self.assertEqual(payload["facts"]["last_brief_at"], NOW - 60)
        self.assertEqual(payload["facts"]["cause"], "kernel update")
        # The fact is a measurement of the filesystem `--disk` names, so its
        # value belongs to the machine the guest runs on, not to this fixture.
        # `percent > 0` failed on any filesystem under 0.05 % full (HQ's /tmp
        # is a 7.6 GB tmpfs holding ~3 MB: 0.0388 %, rounded to 0.0).  Pin the
        # shape here; the exact arithmetic is the mocked case below.
        disk = payload["facts"]["disk"]
        self.assertGreater(disk["total_bytes"], 0)
        self.assertGreaterEqual(disk["used_bytes"], 0)
        self.assertGreaterEqual(disk["percent"], 0.0)
        self.assertLessEqual(disk["percent"], 100.0)
        self.assertIn("BEGIN PUBLIC KEY", payload["public_key"])

    def test_disk_fact_measures_the_named_path_and_rounds_to_one_decimal(self) -> None:
        directory = tempfile.mkdtemp(prefix="odile-collect-disk-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        root = Path(directory)
        cases = (
            # The /tmp shape measured on HQ, 2026-11-25: nearly empty rounds
            # to 0.0, and that is a fact, not a failure.
            ("a nearly-empty tmpfs", 8148393984, 3162112, 0.0),
            ("a third full", 1000, 333, 33.3),
            ("a zero total", 0, 0, 0.0),
        )
        for name, total, used, percent in cases:
            with self.subTest(name):
                usage = SimpleNamespace(total=total, used=used, free=total - used)
                with mock.patch.object(
                    heartbeat.shutil, "disk_usage", return_value=usage
                ) as measured:
                    payload = heartbeat.collect(self._collect_args(root))
                # The fact follows `args.disk`; it is not a hardcoded path.
                measured.assert_called_once_with(str(root))
                self.assertEqual(
                    payload["facts"]["disk"],
                    {"total_bytes": total, "used_bytes": used, "percent": percent},
                )

    def _report(self, root: Path, *, release: str = "2026.11.1") -> Path:
        raw = bytearray(0x90 + 48)
        raw[0:4] = (5).to_bytes(4, "little")
        raw[0x90 : 0x90 + 48] = b"\x42" * 48
        path = root / "attest.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "release": release,
                    "status": "ok",
                    "hostkey": "ssh-ed25519 AAAA",
                    "fingerprint": "SHA256:" + "A" * 43,
                    "report_b64": base64.b64encode(bytes(raw)).decode("ascii"),
                    "cmdline": "bb.release=%s" % release,
                }
            ),
            encoding="utf-8",
        )
        return path

    def _collect_args(self, root: Path, **overrides) -> Namespace:
        args = Namespace(
            slug="odl-7f3a2c",
            at=NOW,
            disk=str(root),
            backup_status=None,
            brief_status=None,
            version_file=None,
            odile_version=None,
            hermes_version=None,
            cause=None,
            public_key=None,
            attest=None,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_collect_adds_boot_release_and_measurement_from_the_report(self) -> None:
        directory = tempfile.mkdtemp(prefix="odile-collect-boot-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        root = Path(directory)
        report = self._report(root)

        payload = heartbeat.collect(self._collect_args(root, attest=str(report)))

        self.assertEqual(
            payload["facts"]["boot"],
            {"release": "2026.11.1", "measurement": "42" * 48},
        )

    def test_collect_leaves_boot_out_when_the_report_is_absent(self) -> None:
        directory = tempfile.mkdtemp(prefix="odile-collect-noboot-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        root = Path(directory)

        payload = heartbeat.collect(self._collect_args(root, attest=str(root / "absent.json")))

        self.assertNotIn("boot", payload["facts"])


# ---------------------------------------------------------------------------
# The host-key probe, against a fake SSH server and a closed port
# ---------------------------------------------------------------------------


class HostKeyProbeTest(unittest.TestCase):
    def test_reads_the_fixed_ed25519_key_and_its_fingerprint(self) -> None:
        with FakeSSHServer() as server:
            found = health.probe_host_keys(
                "127.0.0.1", server.port, timeout=3, key_algorithms=("ssh-ed25519",)
            )
        self.assertEqual(server.errors, [])
        self.assertTrue(found.reachable)
        self.assertEqual(len(found.keys), 1)
        self.assertEqual(found.keys[0].key_type, "ssh-ed25519")
        self.assertEqual(found.keys[0].blob, TEST_HOST_B64)
        self.assertEqual(found.keys[0].fingerprint, TEST_HOST_FP)
        self.assertNotIn("=", found.keys[0].fingerprint)

    def test_the_x25519_fallback_returns_the_same_key(self) -> None:
        with FakeSSHServer(kex_algorithms=(health.KEX_CURVE25519,)) as server:
            found = health.probe_host_keys(
                "127.0.0.1", server.port, timeout=3, key_algorithms=("ssh-ed25519",)
            )
        self.assertEqual(server.errors, [])
        self.assertEqual(found.keys[0].fingerprint, TEST_HOST_FP)

    def test_a_closed_port_is_unreachable_not_an_exception(self) -> None:
        found = health.probe_host_keys(
            "127.0.0.1", closed_port(), timeout=0.5, key_algorithms=("ssh-ed25519",)
        )
        self.assertFalse(found.reachable)
        self.assertEqual(found.keys, ())
        self.assertIn("connect failed", found.error)

    def test_a_recorded_probe_answers_without_a_socket(self) -> None:
        directory = tempfile.mkdtemp(prefix="odile-recorded-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = Path(directory) / "keys.txt"
        path.write_text(
            f"203.0.113.10 ssh-ed25519 {TEST_HOST_B64}\n203.0.113.11 unreachable\n",
            encoding="utf-8",
        )
        probe = health.recorded_host_key_probe(path)
        self.assertEqual(probe("203.0.113.10").keys[0].fingerprint, TEST_HOST_FP)
        self.assertFalse(probe("203.0.113.11").reachable)
        self.assertFalse(probe("203.0.113.12").reachable)


# ---------------------------------------------------------------------------
# Deliveries: outbox, SMTP and Matrix
# ---------------------------------------------------------------------------


class FakeSMTPServer:
    """Just enough SMTP for `smtplib.send_message`, with no TLS."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop = True
        self._listener.close()
        self._thread.join(timeout=3)

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._listener.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(5)
        stream = conn.makefile("rb")
        conn.sendall(b"220 fake ESMTP\r\n")
        message: dict = {"from": "", "to": [], "data": []}
        in_data = False
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                text = line.rstrip(b"\r\n")
                upper = text.upper()
                if in_data:
                    if text == b".":
                        in_data = False
                        self.messages.append(message)
                        conn.sendall(b"250 queued\r\n")
                    else:
                        message["data"].append(text)
                    continue
                if upper.startswith(b"EHLO") or upper.startswith(b"HELO"):
                    conn.sendall(b"250-fake\r\n250 HELP\r\n")
                elif upper.startswith(b"MAIL FROM:"):
                    message = {"from": text[10:].strip(b"<>"), "to": [], "data": []}
                    conn.sendall(b"250 ok\r\n")
                elif upper.startswith(b"RCPT TO:"):
                    message["to"].append(text[8:].strip(b"<>"))
                    conn.sendall(b"250 ok\r\n")
                elif upper == b"DATA":
                    in_data = True
                    conn.sendall(b"354 go\r\n")
                elif upper == b"QUIT":
                    conn.sendall(b"221 bye\r\n")
                    break
                else:
                    conn.sendall(b"250 ok\r\n")
        finally:
            stream.close()
            conn.close()


class DeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="odile-delivery-")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def test_the_outbox_writes_a_json_line_and_an_eml(self) -> None:
        outbox = health.Outbox(self.directory)
        alert = health.Alert(
            slug="odl-7f3a2c",
            kind="disk",
            tenant="ada@example.com",
            symptom="disk 91.0% used",
            first_command="ssh -i ~/.ssh/odile root@203.0.113.10 'df -h /'",
            at=NOW,
        )
        delivery = outbox.record(
            kind=alert.kind,
            slug=alert.slug,
            subject=alert.subject(),
            body=alert.text(),
            channel="outbox",
            recipient="ops@example.com",
            at=alert.at,
            email=True,
        )
        self.assertTrue(delivery.ok)
        lines = (Path(self.directory) / "alerts.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["at_iso"], "2026-11-08T08:00:00Z")
        emails = list((Path(self.directory) / "emails").glob("*.eml"))
        self.assertEqual(len(emails), 1)
        text = emails[0].read_text(encoding="utf-8")
        self.assertIn("Subject: [Odile] disk filling", text)
        self.assertIn("symptom:  disk 91.0% used", text)

    def test_smtp_delivers_one_message_to_one_address(self) -> None:
        server = FakeSMTPServer()
        self.addCleanup(server.close)
        sender = health.SmtpSender(
            host="127.0.0.1",
            port=server.port,
            sender="alerts@odile.fyi",
            starttls=False,
        )
        delivery = sender.send("ops@example.com", "subject", "body", at=NOW)
        self.assertTrue(delivery.ok, delivery.error)
        deadline = time.time() + 5
        while not server.messages and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(server.messages), 1)
        self.assertEqual(server.messages[0]["to"], [b"ops@example.com"])
        self.assertIn(b"body", b"\n".join(server.messages[0]["data"]))
        self.assertIn(b"Subject: subject", b"\n".join(server.messages[0]["data"]))

    def test_matrix_puts_one_message_in_the_room(self) -> None:
        calls: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_PUT(self) -> None:  # noqa: N802 - stdlib spelling
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                calls.append(
                    {
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "body": json.loads(body),
                    }
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"event_id":"$fixture"}')

            def log_message(self, *args) -> None:  # noqa: D401 - silence
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sender = health.MatrixSender(
            homeserver=f"http://127.0.0.1:{server.server_address[1]}",
            room_id="!room:matrix.org",
            token="fixture-token",
        )
        delivery = sender.send("alert text", at=NOW, slug="odl-7f3a2c", kind="down")
        self.assertTrue(delivery.ok, delivery.error)
        self.assertEqual(len(calls), 1)
        self.assertIn("/rooms/%21room%3Amatrix.org/send/m.room.message/", calls[0]["path"])
        self.assertEqual(calls[0]["auth"], "Bearer fixture-token")
        self.assertEqual(calls[0]["body"]["body"], "alert text")


# ---------------------------------------------------------------------------
# The poll: classification, the state transition, and the alerts
# ---------------------------------------------------------------------------


class HealthCheckTest(TempStoreMixin):
    def courier(self) -> health.Courier:
        return health.Courier(outbox=health.Outbox(Path(self.directory) / "outbox"))

    def run_check(self, now: int, *, apply: bool = True, probe=None, **kwargs) -> health.HealthResult:
        self.clock.now = now
        return health.check(
            self.store,
            self.beats,
            now=now,
            host_key_probe=probe or (lambda host: observation()),
            courier=self.courier(),
            apply=apply,
            alert_email="ops@example.com",
            **kwargs,
        )

    def test_a_sealed_reboot_raises_no_event_and_no_unlock_email(self) -> None:
        """The machine comes back by itself: a 90 s gap is not a state.

        The guest reboots between two beats; the poll runs inside the gap and
        again after the returning beat.  Nothing is parked, no state event is
        written, no unlock email exists, and the hostname — the Webapp URL — is
        the same before and after.
        """
        tenant = self.active_tenant()
        before = tenant.hostname
        self.beat()
        during = self.run_check(NOW + 90)
        self.assertEqual(during.tenants[0].classification, "ok")
        self.assertEqual(during.planned, ())
        self.assertEqual(self.store.get("odl-7f3a2c").state, "ready")
        self.beat(at=NOW + 300, key="later")
        after = self.run_check(NOW + 300)
        self.assertEqual(after.tenants[0].classification, "ok")
        self.assertEqual(after.planned, ())
        current = self.store.get("odl-7f3a2c")
        self.assertEqual(current.state, "ready")
        self.assertEqual(current.hostname, before)
        events = self.store.events("odl-7f3a2c")
        self.assertEqual(
            [event for event in events if event.to_state == "waiting_for_owner"], []
        )
        self.assertEqual(
            [event for event in events if event.kind == "waiting_for_owner"], []
        )
        self.assertFalse((Path(self.directory) / "outbox").exists())

    def test_a_stopped_heartbeat_is_down_whatever_port_22_answers(self) -> None:
        """The recorded key is a symptom now, not a park: every answer is down."""
        self.active_tenant("odl-keya", ipv4="203.0.113.20")
        self.beat("odl-keya", at=NOW)
        self.active_tenant("odl-keyb", ipv4="203.0.113.21")
        self.beat("odl-keyb", at=NOW)
        answers = {
            "203.0.113.20": observation(),                 # the recorded key
            "203.0.113.21": observation(OTHER_HOST_FP),    # a different key
        }
        result = self.run_check(
            NOW + STOP, probe=lambda host: answers.get(host, observation(reachable=False))
        )
        by_slug = {tenant.slug: tenant for tenant in result.tenants}
        for slug in ("odl-keya", "odl-keyb"):
            self.assertEqual(by_slug[slug].classification, "down")
            self.assertEqual(self.store.get(slug).state, "ready")
            kinds = [alert.kind for alert in result.planned if alert.slug == slug]
            self.assertEqual(kinds, ["down"])
        symptoms = {alert.slug: alert.symptom for alert in result.planned}
        self.assertIn("port 22 answers host key SHA256:", symptoms["odl-keya"])
        self.assertIn("port 22 answers host key", symptoms["odl-keyb"])
        self.assertNotIn("waiting_for_owner", [alert.kind for alert in result.planned])
        emails = sorted((Path(self.directory) / "outbox" / "emails").glob("*.eml"))
        self.assertEqual(len(emails), 2, "one operator alert per tenant")
        for email in emails:
            self.assertIn("Subject: [Odile] down", email.read_text(encoding="utf-8"))
            self.assertNotIn("unlock", email.read_text(encoding="utf-8"))
        lines = (Path(self.directory) / "outbox" / "alerts.jsonl").read_text().splitlines()
        self.assertEqual(
            [json.loads(line)["kind"] for line in lines], ["down", "down"]
        )

    def test_an_unreachable_port_is_down(self) -> None:
        self.active_tenant()
        self.beat()
        result = self.run_check(NOW + 700, probe=lambda host: observation(reachable=False))
        self.assertEqual(result.tenants[0].classification, "down")
        self.assertEqual(self.store.get("odl-7f3a2c").state, "ready")
        self.assertIn(
            "port 22 did not answer", [a.symptom for a in result.planned if a.kind == "down"][0]
        )

    def test_a_first_visit_machine_that_answers_nothing_is_down_without_a_heartbeat(
        self,
    ) -> None:
        """A machine that dies between provision and first visit is not silent.

        It has never sent a signed heartbeat, so there is no age to compare;
        the two public surfaces answer nothing (no HTTP answer on `/health`,
        no host key on port 22).  That is `down`: one ledger row, kept open
        across polls, resolved when a surface answers again — no signed
        heartbeat required for the resolution either.
        """
        self.first_visit_tenant(at=NOW - 40 * 3600)
        silent_http = lambda slug: health.HttpObservation(None, "connection refused")  # noqa: E731
        silent_port = lambda host: observation(reachable=False)  # noqa: E731

        first = self.run_check(NOW + 600, probe=silent_port, http_probe=silent_http)
        self.assertEqual(len(first.tenants), 1, "first_visit is polled")
        tenant = first.tenants[0]
        self.assertEqual(tenant.state, "first_visit")
        self.assertIsNone(tenant.heartbeat_age_seconds)
        self.assertEqual(tenant.classification, "down")
        self.assertEqual(
            [a.kind for a in first.planned], ["down"],
            "one condition, one alert: no guessed backup for a never-beating machine",
        )
        down = [a for a in first.alerts if a.slug == "odl-7f3a2c" and a.kind == "down"]
        self.assertEqual(len(down), 1)
        self.assertIn("no signed heartbeat has ever arrived", down[0].symptom)
        # The Morning brief cron is created by the onboarding skill after the
        # sign-in, so a first-visit machine has no brief to miss yet.
        self.assertIn("brief missed: no", down[0].symptom)

        second = self.run_check(NOW + 900, probe=silent_port, http_probe=silent_http)
        rows = [a for a in self.beats.alerts("odl-7f3a2c") if a.kind == "down"]
        self.assertEqual(len(rows), 1, "one row per (slug, kind), not one per poll")
        self.assertIsNone(rows[0].resolved_at)
        self.assertEqual(len([a for a in second.alerts if a.slug == "odl-7f3a2c"]), 1)

        answered = self.run_check(
            NOW + 1200,
            probe=silent_port,
            http_probe=lambda slug: health.HttpObservation(200, '{"ok": true}'),
        )
        self.assertEqual(answered.tenants[0].classification, "ok")
        self.assertIn(("odl-7f3a2c", "down"), answered.resolved)
        self.assertEqual(answered.planned, (), "resolution raises no second alert")
        rows = [a for a in self.beats.alerts("odl-7f3a2c") if a.kind == "down"]
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0].resolved_at)
        self.assertEqual(self.store.get("odl-7f3a2c").state, "first_visit")

    def test_a_first_visit_machine_answering_health_is_not_down(self) -> None:
        """No heartbeat yet, but the machine serves `/health`: it is alive."""
        self.first_visit_tenant()
        result = self.run_check(
            NOW + 600,
            http_probe=lambda slug: health.HttpObservation(200, '{"ok": true}'),
        )
        self.assertEqual(result.tenants[0].classification, "ok")
        self.assertEqual(result.planned, ())

    def test_a_first_visit_machine_answering_health_after_the_brief_hour_plans_nothing(
        self,
    ) -> None:
        """The 07:00 brief job does not exist before the first visit.

        `installer/odile_install.py`'s onboarding skill creates it in the
        tenant's chat after the sign-in moves the row `first_visit -> ready`.
        A first-visit machine that has been up across a 07:00 has therefore
        missed no brief: planning `brief_missing` here would page the owner
        about a cron job nobody created yet.
        """
        self.first_visit_tenant(at=NOW - 40 * 3600)
        result = self.run_check(
            NOW + 600,
            http_probe=lambda slug: health.HttpObservation(200, '{"ok": true}'),
        )
        tenant = result.tenants[0]
        self.assertEqual(tenant.state, "first_visit")
        self.assertFalse(tenant.brief_missed, "a first-visit tenant has no brief yet")
        self.assertEqual(result.planned, ())
        self.assertEqual(self.beats.alerts("odl-7f3a2c"), ())

    def test_a_first_visit_machine_answering_only_port_22_after_the_brief_hour_plans_nothing(
        self,
    ) -> None:
        """`--no-http` leaves port 22 as the one answer; the brief is still absent."""
        self.first_visit_tenant(at=NOW - 40 * 3600)
        result = self.run_check(NOW + 600, probe=lambda host: observation())
        self.assertEqual(result.tenants[0].classification, "ok")
        self.assertFalse(result.tenants[0].brief_missed)
        self.assertEqual(result.planned, ())
        self.assertEqual(self.beats.alerts("odl-7f3a2c"), ())

    def test_a_first_visit_machine_with_port_22_answering_is_not_down(self) -> None:
        """An unanswered `/health` is confirmed against port 22 before `down`."""
        self.first_visit_tenant()
        result = self.run_check(
            NOW + 600,
            probe=lambda host: observation(),
            http_probe=lambda slug: health.HttpObservation(None),
        )
        self.assertEqual(result.tenants[0].classification, "ok")
        self.assertEqual(result.planned, ())

    def test_a_machine_still_booting_is_not_polled(self) -> None:
        """`first_boot` may answer nothing while it boots; the poll starts after it."""
        self.clock.now = NOW
        self.store.invite("odl-7f3a2c")
        self.store.claim("odl-7f3a2c")
        self.store.provision(
            "odl-7f3a2c", server_id="srv-7f3a2c", hostname="odl-7f3a2c",
            ipv4="203.0.113.120", plan_id=113, expires_at=NOW + 30 * DAY,
        )
        self.store.await_boot("odl-7f3a2c")
        self.assertEqual(self.store.get("odl-7f3a2c").state, "first_boot")
        result = self.run_check(
            NOW + 600,
            probe=lambda host: observation(reachable=False),
            http_probe=lambda slug: health.HttpObservation(None),
        )
        self.assertEqual(result.tenants, ())

    def test_without_the_health_probe_port_22_is_the_only_surface(self) -> None:
        """`--no-http` leaves the SSH port as the one answer consulted."""
        self.first_visit_tenant()
        silent = self.run_check(
            NOW + 600, probe=lambda host: observation(reachable=False)
        )
        self.assertEqual(silent.tenants[0].classification, "down")
        live = self.run_check(NOW + 900, probe=lambda host: observation())
        self.assertEqual(live.tenants[0].classification, "ok")
        self.assertIn(("odl-7f3a2c", "down"), live.resolved)

    def test_a_fresh_heartbeat_raises_nothing(self) -> None:
        self.active_tenant()
        self.beat()
        result = self.run_check(NOW + 60)
        self.assertEqual(result.tenants[0].classification, "ok")
        self.assertEqual(result.planned, ())
        self.assertEqual(self.store.get("odl-7f3a2c").state, "ready")

    def test_release_drift_is_reported_when_the_boot_differs(self) -> None:
        """2026.11.1 ordered, 2026.12.0 booted: visible, not silent, not fixed."""
        self.active_tenant(release="2026.11.1")
        self.beat(payload=facts(boot=boot_fact(release="2026.12.0")))
        result = self.run_check(NOW + 60)
        tenant = result.tenants[0]
        self.assertEqual(tenant.release["intended"], "2026.11.1")
        self.assertEqual(tenant.release["booted"], "2026.12.0")
        self.assertEqual(tenant.release["status"], "release_drift")
        self.assertEqual(self.store.get("odl-7f3a2c").release, "2026.11.1")
        self.assertEqual(self.store.get("odl-7f3a2c").state, "ready")

    def test_a_matching_release_is_same(self) -> None:
        self.active_tenant(release="2026.11.1")
        self.beat(payload=facts(boot=boot_fact(release="2026.11.1")))
        result = self.run_check(NOW + 60)
        self.assertEqual(result.tenants[0].release["status"], "same")

    def test_a_missing_boot_object_is_unknown_not_drift(self) -> None:
        self.active_tenant(release="2026.11.1")
        self.beat(payload=facts())
        result = self.run_check(NOW + 60)
        self.assertEqual(result.tenants[0].release["status"], "unknown")

    def test_an_unrecorded_release_is_unknown_not_drift(self) -> None:
        self.active_tenant()
        self.beat(payload=facts(boot=boot_fact(release="2026.11.1")))
        result = self.run_check(NOW + 60)
        self.assertEqual(result.tenants[0].release["status"], "unknown")

    def test_a_missed_brief_on_a_stopped_machine_is_part_of_down(self) -> None:
        # 06:50: the first 07:00 brief is due in ten minutes; the last beat is
        # ten minutes old at 07:00:01, and no brief arrived.
        early = int(datetime(2026, 11, 8, 6, 50, tzinfo=timezone.utc).timestamp())
        self.active_tenant(at=early)
        self.beat(at=early)
        result = self.run_check(early + 601)
        self.assertEqual(result.tenants[0].classification, "down")
        kinds = [alert.kind for alert in result.planned]
        self.assertEqual(kinds, ["down"])
        self.assertIn("brief missed: yes", result.planned[0].symptom)
        self.assertNotIn("brief_missing", kinds)

    def test_a_missed_brief_with_live_heartbeats_is_its_own_alert(self) -> None:
        early = int(datetime(2026, 11, 8, 6, 50, tzinfo=timezone.utc).timestamp())
        self.active_tenant(at=early)
        self.beat(at=early, payload=facts(brief_at=early - DAY))
        self.beat(at=early + 600, key="later", payload=facts(brief_at=early - DAY))
        result = self.run_check(early + 601, probe=lambda host: observation())
        self.assertEqual(result.tenants[0].classification, "ok")
        kinds = [alert.kind for alert in result.planned]
        self.assertEqual(kinds, ["brief_missing"])
        self.assertIn("heartbeats are live", result.planned[0].symptom)

    def test_disk_spend_and_backup_each_alert_at_their_threshold(self) -> None:
        self.active_tenant("odl-disk")
        self.beat("odl-disk", payload=facts(disk=91.0, spent=1.0))
        self.active_tenant("odl-spend")
        self.beat("odl-spend", payload=facts(disk=10.0, spent=85.0, cap=100.0))
        self.active_tenant("odl-backup")
        self.beat("odl-backup", payload=facts(backup_at=NOW - 37 * 3600, disk=10.0, spent=1.0))
        result = self.run_check(NOW + 60)
        by_slug = {alert.slug: alert.kind for alert in result.planned}
        self.assertEqual(by_slug["odl-disk"], "disk")
        self.assertEqual(by_slug["odl-spend"], "spend")
        self.assertEqual(by_slug["odl-backup"], "backup")
        disk_alert = [alert for alert in result.planned if alert.kind == "disk"][0]
        self.assertIn("disk 91.0% used", disk_alert.symptom)

    def test_exactly_at_the_threshold_is_not_an_alert(self) -> None:
        self.active_tenant("odl-edge")
        self.beat(
            "odl-edge",
            payload=facts(disk=80.0, spent=80.0, cap=100.0, backup_at=NOW - 36 * 3600),
        )
        result = self.run_check(NOW)
        self.assertEqual(result.planned, ())

    def test_the_poll_is_idempotent(self) -> None:
        self.active_tenant()
        self.beat()
        first = self.run_check(NOW + STOP)
        second = self.run_check(NOW + STOP)
        self.assertEqual(len([a for a in first.alerts if a.kind == "down"]), 1)
        self.assertEqual(len([a for a in second.alerts if a.kind == "down"]), 1)
        second_down = [a for a in second.alerts if a.kind == "down"][0]
        self.assertEqual(second_down.tenant, "ada@example.com")
        emails = list((Path(self.directory) / "outbox" / "emails").glob("*.eml"))
        self.assertEqual(len(emails), 1, "one condition, one delivery")
        lines = (Path(self.directory) / "outbox" / "alerts.jsonl").read_text().splitlines()
        self.assertEqual(len([l for l in lines if json.loads(l)["kind"] == "down"]), 1)
        self.assertEqual(len([l for l in lines if json.loads(l)["kind"] == "unlock_email"]), 0)

    def test_a_returning_heartbeat_resolves_down(self) -> None:
        self.active_tenant()
        self.beat()
        self.run_check(NOW + STOP)
        self.assertEqual(self.store.get("odl-7f3a2c").state, "ready")
        down = [a for a in self.beats.alerts("odl-7f3a2c") if a.kind == "down"]
        self.assertTrue(down)
        self.assertIsNone(down[-1].resolved_at)
        self.beat(at=NOW + 700, key="later")
        dry = self.run_check(NOW + 700, apply=False)
        self.assertEqual(dry.tenants[0].classification, "ok")
        result = self.run_check(NOW + 700)
        self.assertEqual(self.store.get("odl-7f3a2c").state, "ready")
        self.assertEqual(result.tenants[0].classification, "ok")
        current = [a for a in self.beats.alerts("odl-7f3a2c") if a.kind == "down"]
        self.assertIsNotNone(current[-1].resolved_at)
        self.assertIn(("odl-7f3a2c", "down"), result.resolved)

    def test_a_legacy_parked_tenant_resumes_ready_without_a_waiting_alert(self) -> None:
        """A legacy waiting_for_owner row keeps its value; the beat ends it."""
        self.active_tenant()
        self.beat()
        self.store.heartbeat("odl-7f3a2c", at=NOW)
        self.store.park_for_owner(
            "odl-7f3a2c", observed_host_key=TEST_HOST_FP, now=NOW + STOP
        )
        self.assertEqual(self.store.get("odl-7f3a2c").state, "waiting_for_owner")
        self.beat(at=NOW + 700, key="later")
        result = self.run_check(NOW + 700)
        self.assertEqual(self.store.get("odl-7f3a2c").state, "ready")
        self.assertEqual(result.tenants[0].classification, "ok")
        self.assertEqual(result.planned, ())
        self.assertEqual(
            [a for a in result.alerts if a.kind == "waiting_for_owner"], []
        )

    def test_a_dry_run_changes_nothing_and_sends_nothing(self) -> None:
        self.active_tenant()
        self.beat()
        result = self.run_check(NOW + STOP, apply=False)
        self.assertEqual(self.store.get("odl-7f3a2c").state, "ready")
        self.assertEqual(self.beats.alerts(), ())
        self.assertEqual(result.planned[0].kind, "down")
        self.assertFalse((Path(self.directory) / "outbox").exists())

    def test_a_down_alert_needs_no_owner_address(self) -> None:
        """There is no owner email any more; the operator alert still lands."""
        self.active_tenant(owner="")
        self.beat()
        result = self.run_check(NOW + STOP)
        alert = [a for a in result.alerts if a.kind == "down"][0]
        self.assertTrue(alert.deliveries)
        self.assertTrue(all(d["ok"] for d in alert.deliveries))
        self.assertFalse(
            any(d.get("error", "").startswith("no owner email") for d in alert.deliveries)
        )
        emails = list((Path(self.directory) / "outbox" / "emails").glob("*.eml"))
        self.assertEqual(len(emails), 1)
        self.assertIn("Subject: [Odile] down", emails[0].read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The command surface: one fixture replay with four-plus triggered alerts
# ---------------------------------------------------------------------------


class HealthCommandTest(TempStoreMixin):
    def write_config(self) -> Path:
        config = Path(self.directory) / "config.yaml"
        config.write_text(
            "\n".join(
                [
                    f"state_db: {self.db}",
                    f"heartbeat_db: {self.db}",
                    f"alert_outbox: {Path(self.directory) / 'outbox'}",
                    "alert_email: ops@example.com",
                    "tenant_domain: odile.fyi",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        config.chmod(0o600)
        return config

    def test_the_cli_triggers_the_four_alerts_and_no_unlock_email(self) -> None:
        # Two stopped machines are both `down`; the other three are live, so
        # their resource alerts are the only reason to page for them.
        self.active_tenant("odl-wait", ipv4="203.0.113.10", at=NOW)
        self.beat("odl-wait", at=NOW)
        self.active_tenant("odl-down", ipv4="203.0.113.11", at=NOW)
        self.beat("odl-down", at=NOW, payload=facts(disk=10.0, spent=1.0))
        self.active_tenant("odl-disk", ipv4="203.0.113.12", at=NOW + STOP)
        self.beat("odl-disk", at=NOW + STOP, payload=facts(disk=91.0, spent=1.0, backup_at=NOW + STOP))
        self.active_tenant("odl-spend", ipv4="203.0.113.13", at=NOW + STOP)
        self.beat("odl-spend", at=NOW + STOP, payload=facts(disk=10.0, spent=95.0, backup_at=NOW + STOP))
        self.active_tenant("odl-backup", ipv4="203.0.113.14", at=NOW + STOP)
        self.beat(
            "odl-backup",
            at=NOW + STOP,
            payload=facts(disk=10.0, spent=1.0, backup_at=NOW + STOP - 40 * 3600),
        )
        keys = Path(self.directory) / "recorded-keys.txt"
        keys.write_text(
            f"203.0.113.10 ssh-ed25519 {TEST_HOST_B64}\n"
            f"203.0.113.11 ssh-ed25519 {OTHER_HOST_B64}\n"
            "203.0.113.12 unreachable\n"
            "203.0.113.13 unreachable\n"
            "203.0.113.14 unreachable\n",
            encoding="utf-8",
        )
        config = self.write_config()
        out = io.StringIO()
        code = cli.main(
            [
                "health",
                "--config",
                str(config),
                "--db",
                str(self.db),
                "--heartbeat-db",
                str(self.db),
                "--outbox",
                str(Path(self.directory) / "outbox"),
                "--recorded-host-keys",
                str(keys),
                "--now",
                str(NOW + STOP),
                "--no-http",
                "--apply",
                "--json",
            ],
            stdout=out,
        )
        self.assertEqual(code, 0, out.getvalue())
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["dry_run"])
        kinds = {alert["kind"] for alert in payload["alerts"] if alert["open"]}
        self.assertTrue(
            {"down", "disk", "spend", "backup"} <= kinds,
            kinds,
        )
        self.assertNotIn("waiting_for_owner", kinds)
        self.assertNotIn("unlock_email", kinds)
        # Every open alert is a timestamped row naming who, where, what, and
        # the first command to run.
        for alert in payload["alerts"]:
            if not alert["open"]:
                continue
            self.assertRegex(alert["first_seen_at_iso"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertTrue(alert["tenant"])
            self.assertTrue(alert["slug"].startswith("odl-"))
            self.assertTrue(alert["symptom"])
            self.assertTrue(alert["first_command"])
        lines = (Path(self.directory) / "outbox" / "alerts.jsonl").read_text().splitlines()
        self.assertGreaterEqual(len(lines), 5)
        rendered = "\n".join(json.loads(line)["body"] for line in lines)
        self.assertIn("tenant:   ada@example.com", rendered)
        self.assertIn("first command: ssh -i ~/.ssh/odile", rendered)
        emails = list((Path(self.directory) / "outbox" / "emails").glob("*.eml"))
        self.assertTrue(emails)
        self.assertEqual([path for path in emails if "unlock" in path.name], [])
        for email in emails:
            self.assertNotIn("unlock", email.read_text(encoding="utf-8"))
            self.assertIn("Subject: [Odile]", email.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
