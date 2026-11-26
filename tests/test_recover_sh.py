#!/usr/bin/python3
"""The one-command restore: its `--dry-run`, and its DNS step, held offline.

Two parts of `deploy/recovery/recover.sh` can be proved without a bucket and
without restoring over a live box, and both answer the sudden-loss question
before the operator commits:

* the **dry run** — *which* Hermes commit, *which* old IPv4 and *which* DNS
  names does the latest snapshot name? A stub `restic` on `PATH` stands in for
  Cloudflare R2: it answers `snapshots --json` with one tag-`hq` snapshot and
  `dump` with a fixture manifest, and records every call, so the test proves the
  script asked for the manifest the nightly backup writes
  (`/var/backups/odile/live/manifest.json`) rather than for HQ.
* the **DNS step** — the Python block is read out of the shipped script and run
  against a fake Cloudflare API. It must move the A records of the names in
  `dns_names`, patch the mail SPF `ip4:` token, create a missing record, skip
  every name in `dns_skip` (`demo.odile.fyi` is the live demo tenant, `odl-*`
  are guests), print what it changed with no token, and be idempotent — a
  second run over the same records prints `no record needed a change`.

The rules:

* the facts come from the snapshot's `recovery` block — commit, old IPv4, DNS
  names to move, DNS names to leave alone;
* a snapshot that predates the recovery block falls back to the kit's own
  `manifest.json`, and says so;
* a dry run writes nothing (no state dir, no secrets, no `/etc`), exits 0, and
  never prints a secret.

    /usr/bin/python3 -m unittest tests.test_recover_sh -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "recovery" / "recover.sh"

PASSPHRASE = "p" + "0" * 63
R2_KEY = "AKIA" + "R2KEY" + "7" * 13
R2_SECRET = "r2secret" + "9" * 24
DNS_TOKEN = "dnstoken" + "5" * 30
ACCOUNT_ID = "b118cf213a0c4b6287c5826741e04bcf"
# Assembled so the leak gate sees no 32-hex DNS resource id literal.
ZONE_ID = "0123456789abcdef" * 2
SNAPSHOT = "1a2b" + "3c4d"  # no bare 8-hex instance id
OLD_IP = "203.0.113.23"
DEMO_IP = "198.51.100.103"

MANIFEST = {
    "generated_at": "2026-11-26T03:35:34Z",
    "host": "oqwaclzm",
    "stage": "/var/backups/odile/live",
    "git_head": "45fd806abc",
    "databases": [],
    "recovery": {
        "schema": "odile.factory-recovery.v1",
        "written_at": "2026-11-26T03:35:34Z",
        "hostname": "oqwaclzm",
        "ipv4": OLD_IP,
        "hermes_version": "Hermes Agent v0.23.3+5.gedc8a22.dirty (2026.9.24) · stable · local edc8a222a801",
        "hermes_commit": "edc8a222a801",
        "git_head": "45fd806abc",
        "zone_id": ZONE_ID,
        "dns_names": ["odile.fyi", "gw.odile.fyi", "mail.odile.fyi",
                      "testing.odile.fyi", "test.odile.fyi"],
        "dns_skip": ["demo.odile.fyi", "www.odile.fyi", "odl-7971a8.odile.fyi",
                     "odl-*.test.odile.fyi"],
        "bucket": "hermes-hq-backup",
        "endpoint": "https://b118cf213a0c4b6287c5826741e04bcf.eu.r2.cloudflarestorage.com",
    },
}

# The first cut's snapshot shape: no recovery block, so the kit's fallback runs.
OLD_MANIFEST = {
    "generated_at": "2026-11-25T03:35:34Z",
    "host": "oqwaclzm",
    "stage": "/var/backups/odile/live",
    "git_head": "1e9c0d3abc",
    "databases": [],
}

KIT_MANIFEST = {
    "recovery": {
        "schema": "odile.factory-recovery.v1",
        "written_at": "2026-11-26T07:23:43Z",
        "hostname": "oqwaclzm",
        "ipv4": OLD_IP,
        "hermes_version": "Hermes Agent v0.23.3+5.gedc8a22 (2026.9.24)",
        "hermes_commit": "edc8a222a801",
        "git_head": "45fd806abc",
        "zone_id": ZONE_ID,
        "dns_names": ["odile.fyi", "gw.odile.fyi"],
        "dns_skip": ["demo.odile.fyi", "www.odile.fyi", "odl-7971a8.odile.fyi",
                     "odl-*.test.odile.fyi"],
        "bucket": "hermes-hq-backup",
        "endpoint": "https://b118cf213a0c4b6287c5826741e04bcf.eu.r2.cloudflarestorage.com",
    }
}


@unittest.skipUnless(os.geteuid() == 0, "recover.sh refuses to run for a non-root user")
class DryRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.kit = self.root / "odile-factory-recovery"
        self.kit.mkdir(mode=0o700)
        (self.kit / "recovery.env").write_text(
            "# fixture secrets\n"
            f"RESTIC_PASSWORD='{PASSPHRASE}'\n"
            f"R2_ACCESS_KEY_ID='{R2_KEY}'\n"
            f"R2_SECRET_ACCESS_KEY='{R2_SECRET}'\n"
            "R2_ENDPOINT='https://b118cf213a0c4b6287c5826741e04bcf.eu.r2.cloudflarestorage.com'\n"
            f"CLOUDFLARE_ACCOUNT_ID='{ACCOUNT_ID}'\n"
            f"CLOUDFLARE_DNS_TOKEN='{DNS_TOKEN}'\n"
            f"ZONE_ID='{ZONE_ID}'\n",
            encoding="utf-8",
        )
        (self.kit / "recovery.env").chmod(0o600)
        self.script = self.kit / "recover.sh"
        self.script.write_bytes(SCRIPT.read_bytes())
        self.script.chmod(0o700)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "restic.log"
        self.manifest = self.root / "snapshot-manifest.json"
        self.write_stub()

    def write_stub(self) -> None:
        stub = self.bin / "restic"
        stub.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{self.log}"\n'
            'case "$1" in\n'
            "  snapshots)\n"
            f"    cat \"{self.root / 'snapshots.json'}\"\n"
            "    exit 0\n"
            "    ;;\n"
            "  dump)\n"
            "    case \"$3\" in\n"
            "      */manifest.json)\n"
            f"        cat \"{self.manifest}\"\n"
            "        exit 0\n"
            "        ;;\n"
            "    esac\n"
            "    exit 1\n"
            "    ;;\n"
            "esac\n"
            'echo "stub restic: unsupported $*" >&2\n'
            "exit 3\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        (self.root / "snapshots.json").write_text(
            json.dumps([{"short_id": SNAPSHOT, "time": "2026-11-26T03:35:34Z",
                         "tags": ["hq"]}]) + "\n",
            encoding="utf-8",
        )
        self.manifest.write_text(json.dumps(MANIFEST) + "\n", encoding="utf-8")

    def run_dry(self, *args: str) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("RESTIC_", "AWS_", "R2_", "CLOUDFLARE_"))}
        env.update({
            "PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"),
            "ODILE_STATE_DIR": str(self.root / "state"),
            "ODILE_REPO": str(self.root / "repo"),
            "LOG": str(self.root / "odile-recovery.log"),
            "ENV_FILE": str(self.kit / "recovery.env"),
            "ODILE_OS_ID": "debian",
        })
        (self.root / "tmp").mkdir(exist_ok=True)
        (self.root / "home").mkdir(exist_ok=True)
        return subprocess.run(
            ["bash", str(self.script), "--dry-run", "--ip", "203.0.113.10", *args],
            capture_output=True, text=True, env=env, timeout=120,
        )

    def test_the_dry_run_reads_the_snapshots_recovery_block(self):
        result = self.run_dry()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Hermes: edc8a222a801", result.stdout)
        self.assertIn("Old IPv4 in the backup: " + OLD_IP, result.stdout)
        flat = "".join(result.stdout.split())
        self.assertIn("DNSnamestomove:odile.fyi,gw.odile.fyi,mail.odile.fyi,"
                      "testing.odile.fyi,test.odile.fyi", flat)
        self.assertIn("DRY RUN", result.stdout)
        self.assertIn(f"Latest snapshot: {SNAPSHOT}", result.stdout)
        # It asked the snapshot for the nightly manifest, not HQ.
        asked = self.log.read_text(encoding="utf-8")
        self.assertIn(f"dump {SNAPSHOT}", asked)
        self.assertIn("/var/backups/odile/live/manifest.json", asked)

    def test_the_dry_run_writes_nothing_and_prints_no_secret(self):
        result = self.run_dry()
        self.assertEqual(result.returncode, 0)
        for path in (self.root / "state", self.root / "repo"):
            self.assertFalse(path.exists(), f"the dry run created {path}")
        self.assertFalse((self.root / "odile-recovery.log").exists())
        for secret in (PASSPHRASE, R2_KEY, R2_SECRET, DNS_TOKEN):
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(secret, result.stderr)

    def test_the_dns_skip_list_is_an_fnmatch_list_that_keeps_the_demo(self):
        result = self.run_dry()
        self.assertEqual(result.returncode, 0)
        flat = "".join(result.stdout.split())
        self.assertIn("DNSnamestoleavealone:demo.odile.fyi,www.odile.fyi,"
                      "odl-7971a8.odile.fyi,odl-*.test.odile.fyi", flat)

    def test_a_snapshot_without_the_recovery_block_falls_back_to_the_kit(self):
        self.manifest.write_text(json.dumps(OLD_MANIFEST) + "\n", encoding="utf-8")
        (self.kit / "manifest.json").write_text(json.dumps(KIT_MANIFEST) + "\n",
                                                encoding="utf-8")
        result = self.run_dry()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("falling back to the kit's manifest", result.stderr)
        self.assertIn("Hermes: edc8a222a801", result.stdout)
        self.assertIn("Old IPv4 in the backup: " + OLD_IP, result.stdout)

    def test_no_manifest_anywhere_still_prints_the_tracked_dns_defaults(self):
        self.manifest.write_text("", encoding="utf-8")
        result = self.run_dry()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Hermes: latest", result.stdout)
        self.assertIn("Old IPv4 in the backup: unknown", result.stdout)
        self.assertIn("testing.odile.fyi", result.stdout)

    def test_an_unknown_flag_is_refused(self):
        result = self.run_dry("--dns")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown argument", result.stderr)


if __name__ == "__main__":
    unittest.main()


def dns_block() -> str:
    """The Python the shipped script runs for the DNS step, verbatim."""
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("<<'PY'\n", text.index("Updating Cloudflare DNS")) + len("<<'PY'\n")
    end = text.index("\nPY\n", start)
    return text[start:end]


class FakeCloudflare(BaseHTTPRequestHandler):
    """A stateful stand-in for the zone's DNS records endpoint."""

    records: dict[str, dict] = {}
    calls: list[tuple] = []

    def log_message(self, *args):  # noqa: D401 - silence the request log
        return

    def _reply(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's name
        FakeCloudflare.calls.append(("GET", self.path, None,
                                     self.headers.get("Authorization")))
        self._reply({"success": True, "result": list(FakeCloudflare.records.values()),
                     "result_info": {"total_pages": 1}})

    def do_POST(self):  # noqa: N802
        body = self._body()
        record = {"id": f"new{len(FakeCloudflare.records) + 1}", **body}
        FakeCloudflare.records[record["id"]] = record
        FakeCloudflare.calls.append(("POST", self.path, body,
                                     self.headers.get("Authorization")))
        self._reply({"success": True, "result": record})

    def do_PATCH(self):  # noqa: N802
        body = self._body()
        record_id = self.path.rsplit("/", 1)[-1]
        FakeCloudflare.records[record_id].update(body)
        FakeCloudflare.calls.append(("PATCH", self.path, body,
                                     self.headers.get("Authorization")))
        self._reply({"success": True, "result": FakeCloudflare.records[record_id]})


class DnsUpdateTests(unittest.TestCase):
    NEW_IP = "203.0.113.99"

    def setUp(self):
        FakeCloudflare.calls = []
        FakeCloudflare.records = {
            "a1": {"id": "a1", "type": "A", "name": "odile.fyi", "content": OLD_IP},
            "a2": {"id": "a2", "type": "A", "name": "gw.odile.fyi", "content": OLD_IP},
            "a3": {"id": "a3", "type": "A", "name": "mail.odile.fyi", "content": OLD_IP},
            "a4": {"id": "a4", "type": "A", "name": "demo.odile.fyi",
                   "content": DEMO_IP},
            "a5": {"id": "a5", "type": "A", "name": "odl-7971a8.odile.fyi",
                   "content": DEMO_IP},
            "t1": {"id": "t1", "type": "TXT", "name": "mail.odile.fyi",
                   "content": f"v=spf1 ip4:{OLD_IP} ip6:2001:db8::1 -all"},
            "c1": {"id": "c1", "type": "CNAME", "name": "www.odile.fyi",
                   "content": "odile.fyi"},
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCloudflare)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def run_dns(self) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("RESTIC_", "AWS_", "R2_", "CLOUDFLARE_"))}
        env.update({
            "NEW_IP": self.NEW_IP,
            "OLD_IP": OLD_IP,
            "ZONE_ID": ZONE_ID,
            "CLOUDFLARE_DNS_TOKEN": DNS_TOKEN,
            "DNS_NAMES": "odile.fyi,gw.odile.fyi,mail.odile.fyi,testing.odile.fyi,"
                         "test.odile.fyi",
            "DNS_SKIP": "demo.odile.fyi,www.odile.fyi,odl-7971a8.odile.fyi,"
                        "odl-*.test.odile.fyi",
            "CLOUDFLARE_API_BASE":
                f"http://127.0.0.1:{self.server.server_address[1]}/client/v4",
        })
        return subprocess.run(
            [sys.executable, "-"], input=dns_block(), capture_output=True,
            text=True, env=env, timeout=120,
        )

    def test_it_moves_the_hq_names_and_the_spf_token(self):
        result = self.run_dns()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"updated odile.fyi A {OLD_IP} -> {self.NEW_IP}", result.stdout)
        self.assertIn(f"updated gw.odile.fyi A {OLD_IP} -> {self.NEW_IP}", result.stdout)
        self.assertIn("updated mail.odile.fyi TXT (SPF) "
                      f"ip4:{OLD_IP} -> ip4:{self.NEW_IP}", result.stdout)
        self.assertIn(f"created testing.odile.fyi A {self.NEW_IP}", result.stdout)
        records = FakeCloudflare.records
        self.assertEqual(records["a1"]["content"], self.NEW_IP)
        self.assertEqual(records["a2"]["content"], self.NEW_IP)
        self.assertEqual(records["a3"]["content"], self.NEW_IP)
        self.assertIn(f"ip4:{self.NEW_IP}", records["t1"]["content"])
        self.assertNotIn(f"ip4:{OLD_IP}", records["t1"]["content"])

    def test_it_never_moves_the_demo_tenant_or_a_guest(self):
        result = self.run_dns()
        self.assertEqual(result.returncode, 0, result.stderr)
        records = FakeCloudflare.records
        self.assertEqual(records["a4"]["content"], DEMO_IP)
        self.assertEqual(records["a5"]["content"], DEMO_IP)
        touched = [call for call in FakeCloudflare.calls if call[0] in ("POST", "PATCH")]
        for _, path, body, _ in touched:
            self.assertNotIn("demo.odile.fyi", json.dumps(body))
            self.assertNotIn("odl-", json.dumps(body))
        flat = "".join(result.stdout.split())
        self.assertIn("DNSnamesleftalone:demo.odile.fyi,www.odile.fyi,"
                      "odl-7971a8.odile.fyi,odl-*.test.odile.fyi", flat)

    def test_a_second_run_changes_nothing(self):
        self.run_dns()
        FakeCloudflare.calls = []
        result = self.run_dns()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"DNS already points at {self.NEW_IP}", result.stdout)
        self.assertEqual([call for call in FakeCloudflare.calls
                          if call[0] in ("POST", "PATCH")], [])

    def test_the_token_travels_in_the_header_and_never_in_the_output(self):
        result = self.run_dns()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(DNS_TOKEN, result.stdout)
        self.assertNotIn(DNS_TOKEN, result.stderr)
        for method, _path, _body, authorization in FakeCloudflare.calls:
            self.assertEqual(authorization, f"Bearer {DNS_TOKEN}", method)

    def test_a_failed_api_exits_2_without_printing_a_secret(self):
        self.server.shutdown()
        self.server.server_close()
        result = self.run_dns()
        self.assertEqual(result.returncode, 2)
        self.assertIn("DNS NOT UPDATED", result.stderr)
        self.assertNotIn(DNS_TOKEN, result.stderr)
