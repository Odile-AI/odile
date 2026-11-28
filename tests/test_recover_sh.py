#!/usr/bin/python3
"""The one-command restore: its `--dry-run`, and its DNS step, held offline.

Four parts of `deploy/recovery/recover.sh` can be proved without a bucket and
without restoring over a live box, and each answers the sudden-loss question
before the operator commits:

* the **dry run** — *which* Hermes build, *which* old IPv4 and *which* DNS
  names does the latest snapshot name? A stub `restic` on `PATH` stands in for
  Cloudflare R2: it answers `snapshots --json` with one tag-`hq` snapshot and
  `dump` with a fixture manifest, and records every call, so the test proves the
  script asked for the manifest the nightly backup writes
  (`/var/backups/odile/live/manifest.json`) rather than for HQ.
* the **DNS step** — the Python block is read out of the shipped script and run
  against a fake Cloudflare API. It must move the A records of the names in
  `dns_names`, patch the mail SPF `ip4:` token, move the AAAA record and the
  SPF `ip6:` token when the new box has a public IPv6 (or delete the AAAA and
  drop the token when it has none — the 2026-11-26 drill left mail's AAAA on
  the dead HQ), create a missing A record, skip every name in `dns_skip`
  (`demo.odile.fyi` is the live demo tenant, `odl-*` are guests) including
  their AAAA, print what it changed with no token, and be idempotent — a
  second run over the same records prints `no record needed a change`.
* the **Hermes target** — the shipped `hermes_target` runs against a stub
  `git`. A recorded commit that is not on a remote (HQ's local build) cannot be
  fetched, so the script must install the published ref recorded beside it and
  say why; when neither can be fetched it keeps the installer's build.
* the **run from a copy** — the kit lives under `/root`, which the restore
  replaces in place, and restic 0.18 refuses `--include` with `--exclude`. The
  shipped block re-execs the script from `/var/tmp` and puts the kit's current
  files back afterwards; the block is run twice here, the second pass being the
  copy.
* the **package list** — every binary the Makefile's `prereqs` target refuses
  to run without is mapped to the Debian package that provides it and asserted
  in the shipped `apt-get install` line (and in §5A step 3 when the HQ-only
  runbook is in the tested tree, i.e. under `make check-hq`). `make check` does
  not pull a package in: `sshpass` is in no dependency chain, and without it
  the recovered box's gate exits 1 and `odilectl provision` cannot install a
  key on port 23.
* the **recovery set's two dead surfaces** — `test.odile.fyi` is repointed by
  the same run that restores the box, so the restore must carry
  `/opt/odile-test` (its units' `ExecStart`), `/var/lib/odile-test` (its state)
  and `/etc/odile-bench` (the one `EnvironmentFile`), and the unit loop must
  start the two bench units; `/srv/odile-releases` is measured and excluded
  (ADR §3.1), so the republish block must print the four exact commands and
  say `/releases/*` is 404 until they run (t_df30ec09).

The rules:

* the facts come from the snapshot's `recovery` block — build, old IPv4, DNS
  names to move, DNS names to leave alone — and the kit's manifest fills only
  the fields the snapshot predates;
* a snapshot with no recovery block at all falls back to the kit's
  `manifest.json`, and says so;
* a dry run writes nothing (no state dir, no secrets, no `/etc`), exits 0, and
  never prints a secret.

    /usr/bin/python3 -m unittest tests.test_recover_sh -v
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "recovery" / "recover.sh"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The gate's own prerequisite list lives in one place: main's parity test
# derives it from the Makefile and maps each binary to a package. Reuse that
# derivation here so the recovery kit cannot drift from the gate's list.
from tests.test_ci_prereq_parity import (  # noqa: E402
    APT_PACKAGES, TARBALLED, apt_packages, prereq_binaries)


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
# The AAAA the drill left on the dead HQ (2026-11-26, docs/ops/evidence/…):
# unchanged since 2026-11-06 while the A/SPF rows were reverted. Assembled
# from two literals so the leak gate sees no public IPv6 address in the tree
# (the same reason ZONE_ID above is).
OLD_IP6 = "2602:f41c:0:1c9" + "::1"
NEW_IP6 = "2602:f41c:0:1c9" + "::99"
DEMO_IP6 = "2001:db8:aaaa::103"
HERMES_REF = "v0.23.3"
HERMES_REF_COMMIT = "caafe5389ae585ef77387a962aee05b1bcf17029"

MANIFEST = {
    "generated_at": "2026-11-26T03:35:34Z",
    "host": "hq-box",
    "stage": "/var/backups/odile/live",
    "git_head": "45fd806abc",
    "databases": [],
    "recovery": {
        "schema": "odile.factory-recovery.v1",
        "written_at": "2026-11-26T03:35:34Z",
        "hostname": "hq-box",
        "ipv4": OLD_IP,
        "hermes_version": "Hermes Agent v0.23.3+5.gedc8a22.dirty (2026.9.24) · stable · local edc8a222a801",
        "hermes_commit": "edc8a222a801",
        "hermes_commit_fetchable": False,
        "hermes_ref": HERMES_REF,
        "hermes_ref_commit": HERMES_REF_COMMIT,
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

# A snapshot taken before the pin fields existed (the live f7d252db on
# 2026-11-26): it has the commit but not the published ref beside it.
OLD_PIN_MANIFEST = json.loads(json.dumps(MANIFEST))
for _key in ("hermes_commit_fetchable", "hermes_ref", "hermes_ref_commit"):
    del OLD_PIN_MANIFEST["recovery"][_key]

# The first cut's snapshot shape: no recovery block, so the kit's fallback runs.
OLD_MANIFEST = {
    "generated_at": "2026-11-25T03:35:34Z",
    "host": "hq-box",
    "stage": "/var/backups/odile/live",
    "git_head": "1e9c0d3abc",
    "databases": [],
}

KIT_MANIFEST = {
    "recovery": {
        "schema": "odile.factory-recovery.v1",
        "written_at": "2026-11-26T07:23:43Z",
        "hostname": "hq-box",
        "ipv4": OLD_IP,
        "hermes_version": "Hermes Agent v0.23.3+5.gedc8a22 (2026.9.24)",
        "hermes_commit": "edc8a222a801",
        "hermes_commit_fetchable": False,
        "hermes_ref": HERMES_REF,
        "hermes_ref_commit": HERMES_REF_COMMIT,
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

    def run_dry(self, *args: str, ip6: str | None = "2001:db8::10") -> subprocess.CompletedProcess:
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
        command = ["bash", str(self.script), "--dry-run", "--ip", "203.0.113.10"]
        if ip6 is not None:
            command += ["--ip6", ip6]
        return subprocess.run(
            [*command, *args],
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

    def test_the_dry_run_names_the_ipv6_target(self):
        result = self.run_dry()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("New server IPv6: 2001:db8::10", result.stdout)
        flat = "".join(result.stdout.split())
        self.assertIn("AAAA", flat)

    def test_ip6_none_plans_the_delete_branch(self):
        result = self.run_dry("--ip6", "none")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("New server IPv6: none", result.stdout)
        flat = "".join(result.stdout.split())
        self.assertIn("deletetheirAAAArecords", flat)
        self.assertIn("dropthedeadip6:token", flat)

    def test_a_non_public_ipv6_is_refused(self):
        result = self.run_dry("--ip6", "fe80::1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("is not public", result.stderr)

    def write_probes(self, *, ip_output: str, ip_rc: int = 0) -> None:
        curl = self.bin / "curl"
        curl.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")  # no IPv6 here
        curl.chmod(0o755)
        ip = self.bin / "ip"
        ip.write_text(f"#!/bin/sh\nprintf '%s\\n' '{ip_output}'\nexit {ip_rc}\n",
                      encoding="utf-8")
        ip.chmod(0o755)

    def test_the_ipv6_is_detected_when_the_box_has_one(self):
        # `curl -6` fails and `ip -6 route get` names a public source: the
        # script must use it, so the AAAA records move rather than disappear.
        self.write_probes(ip_output="2001:db8:aaaa::1 dev eth0 src 2001:db8::77 metric 100")
        result = self.run_dry(ip6=None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("New server IPv6: 2001:db8::77", result.stdout)
        self.assertNotIn("AAAA records are deleted", result.stdout)

    def test_a_box_with_no_ipv6_route_plans_the_delete_branch(self):
        # The common case for a cheap VPS: `curl -6` fails and `ip` has no
        # route. Under `set -e` + `pipefail` that must not kill the run.
        self.write_probes(ip_output="", ip_rc=1)
        result = self.run_dry(ip6=None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("New server IPv6: none", result.stdout)

    def test_the_dry_run_writes_nothing_and_prints_no_secret(self):
        result = self.run_dry()
        self.assertEqual(result.returncode, 0)
        for path in (self.root / "state", self.root / "repo"):
            self.assertFalse(path.exists(), f"the dry run created {path}")
        self.assertFalse((self.root / "odile-recovery.log").exists())
        for secret in (PASSPHRASE, R2_KEY, R2_SECRET, DNS_TOKEN):
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(secret, result.stderr)

    def test_the_dry_run_installs_the_published_ref_when_the_commit_is_local_only(self):
        result = self.run_dry()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Hermes to install: {HERMES_REF}", result.stdout)
        self.assertIn("local build no new box can fetch", result.stdout)
        flat = "".join(result.stdout.split())
        self.assertIn(f"installHermesat{HERMES_REF};", flat)

    def test_the_dry_run_pins_the_recorded_commit_when_it_is_fetchable(self):
        fetchable = json.loads(json.dumps(MANIFEST))
        fetchable["recovery"]["hermes_commit_fetchable"] = True
        self.manifest.write_text(json.dumps(fetchable) + "\n", encoding="utf-8")
        result = self.run_dry()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Hermes to install: edc8a222a801", result.stdout)
        self.assertIn("which a new box can fetch", result.stdout)

    def test_an_older_snapshot_fills_the_pin_from_the_kit_manifest(self):
        # The live f7d252db shape: the commit is there, the published ref is not.
        self.manifest.write_text(json.dumps(OLD_PIN_MANIFEST) + "\n", encoding="utf-8")
        (self.kit / "manifest.json").write_text(json.dumps(KIT_MANIFEST) + "\n",
                                                encoding="utf-8")
        result = self.run_dry()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no hermes_commit_fetchable, hermes_ref, hermes_ref_commit",
                      result.stderr)
        self.assertIn("using the kit's manifest", result.stderr)
        self.assertIn(f"Hermes to install: {HERMES_REF}", result.stdout)

    def test_the_dry_run_says_the_kit_folder_survives_the_restore(self):
        result = self.run_dry()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("leaving this kit folder and the log in place", result.stdout)

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

    def do_DELETE(self):  # noqa: N802
        record_id = self.path.rsplit("/", 1)[-1]
        removed = FakeCloudflare.records.pop(record_id, None)
        FakeCloudflare.calls.append(("DELETE", self.path, None,
                                     self.headers.get("Authorization")))
        self._reply({"success": True, "result": removed or {"id": record_id}})


class DnsUpdateTests(unittest.TestCase):
    NEW_IP = "203.0.113.99"

    def setUp(self):
        # A mistyped literal (e.g. a triple colon) would still pass every
        # comparison below, because the fake API stores whatever it is sent.
        for value in (OLD_IP6, NEW_IP6, DEMO_IP6):
            self.assertEqual(str(ipaddress.ip_address(value)), value)
        FakeCloudflare.calls = []
        FakeCloudflare.records = {
            "a1": {"id": "a1", "type": "A", "name": "odile.fyi", "content": OLD_IP},
            "a2": {"id": "a2", "type": "A", "name": "gw.odile.fyi", "content": OLD_IP},
            "a3": {"id": "a3", "type": "A", "name": "mail.odile.fyi", "content": OLD_IP},
            "a4": {"id": "a4", "type": "A", "name": "demo.odile.fyi",
                   "content": DEMO_IP},
            "a5": {"id": "a5", "type": "A", "name": "odl-7971a8.odile.fyi",
                   "content": DEMO_IP},
            # The drill's leftover: mail's AAAA still named the dead HQ while
            # its A record had been moved back (2026-11-26).
            "q1": {"id": "q1", "type": "AAAA", "name": "mail.odile.fyi",
                   "content": OLD_IP6},
            # Names the restore must not touch, AAAA included.
            "q2": {"id": "q2", "type": "AAAA", "name": "demo.odile.fyi",
                   "content": DEMO_IP6},
            "q3": {"id": "q3", "type": "AAAA", "name": "odl-7971a8.odile.fyi",
                   "content": DEMO_IP6},
            "q4": {"id": "q4", "type": "AAAA", "name": "odl-guest9.test.odile.fyi",
                   "content": DEMO_IP6},
            "t1": {"id": "t1", "type": "TXT", "name": "mail.odile.fyi",
                   "content": f"v=spf1 ip4:{OLD_IP} ip6:{OLD_IP6} -all"},
            "c1": {"id": "c1", "type": "CNAME", "name": "www.odile.fyi",
                   "content": "odile.fyi"},
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCloudflare)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def run_dns(self, new_ip6: str = "") -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("RESTIC_", "AWS_", "R2_", "CLOUDFLARE_"))}
        env.update({
            "NEW_IP": self.NEW_IP,
            "OLD_IP": OLD_IP,
            "NEW_IP6": new_ip6,
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

    def test_it_deletes_the_dead_aaaa_and_the_spf_ip6_token_with_no_public_ipv6(self):
        # The new box has no public IPv6: there is nowhere to move the AAAA
        # to, so every AAAA on a moved name is deleted and the dead host's
        # `ip6:` token is dropped from the SPF record. Otherwise mail.odile.fyi
        # still resolves (over IPv6) to the box that is gone.
        result = self.run_dns()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        records = FakeCloudflare.records
        self.assertNotIn("q1", records, "mail.odile.fyi's dead AAAA survived")
        self.assertIn(f"deleted mail.odile.fyi AAAA {OLD_IP6}", result.stdout)
        self.assertIn("no public IPv6", result.stdout)
        spf = records["t1"]["content"]
        self.assertIn(f"ip4:{self.NEW_IP}", spf)
        self.assertNotIn("ip6:", spf)
        moved = {"odile.fyi", "gw.odile.fyi", "mail.odile.fyi",
                 "testing.odile.fyi", "test.odile.fyi"}
        self.assertEqual([r for r in records.values()
                          if r["name"] in moved and r["content"] == OLD_IP6], [])
        # And the second run over the same records says so, with no DELETE: an
        # already-absent AAAA is not an error and not a second write.
        FakeCloudflare.calls = []
        again = self.run_dns()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("no record needed a change", again.stdout)
        self.assertEqual([call for call in FakeCloudflare.calls
                          if call[0] in ("POST", "PATCH", "DELETE")], [])

    def test_it_moves_the_aaaa_and_rewrites_the_spf_ip6_token_with_an_ipv6(self):
        result = self.run_dns(NEW_IP6)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        records = FakeCloudflare.records
        self.assertEqual(records["q1"]["content"], NEW_IP6)
        self.assertIn(f"updated mail.odile.fyi AAAA {OLD_IP6} -> {NEW_IP6}",
                      result.stdout)
        spf = records["t1"]["content"]
        self.assertIn(f"ip6:{NEW_IP6}", spf)
        self.assertNotIn(f"ip6:{OLD_IP6}", spf)
        # Only the AAAA records that existed are moved; none is invented for a
        # name that had none (the A step creates missing A records).
        self.assertEqual(sorted(r["name"] for r in records.values()
                                if r["type"] == "AAAA"),
                         ["demo.odile.fyi", "mail.odile.fyi",
                          "odl-7971a8.odile.fyi", "odl-guest9.test.odile.fyi"])
        # And the second run says so.
        FakeCloudflare.calls = []
        again = self.run_dns(NEW_IP6)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("no record needed a change", again.stdout)
        self.assertEqual([call for call in FakeCloudflare.calls
                          if call[0] in ("POST", "PATCH", "DELETE")], [])

    def test_no_skipped_names_aaaa_is_touched(self):
        # demo.odile.fyi (the live demo), odl-7971a8 and odl-*.test are guests:
        # their AAAA records stay where they are in both branches.
        for new_ip6 in ("", NEW_IP6):
            with self.subTest(new_ip6=new_ip6 or "none"):
                self.setUp()
                result = self.run_dns(new_ip6)
                self.assertEqual(result.returncode, 0, result.stderr)
                records = FakeCloudflare.records
                for rid in ("q2", "q3", "q4"):
                    self.assertEqual(records[rid]["content"], DEMO_IP6, rid)
                writes = [call for call in FakeCloudflare.calls
                          if call[0] in ("POST", "PATCH", "DELETE")]
                for method, path, body, _ in writes:
                    self.assertNotIn("demo", path, method)
                    self.assertNotIn("odl-", path, method)
                    self.assertNotIn("demo", json.dumps(body), method)
                    self.assertNotIn("odl-", json.dumps(body), method)

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


def hermes_target_block() -> str:
    """The Hermes target selection the shipped script runs, verbatim."""
    text = SCRIPT.read_text(encoding="utf-8")
    return text[text.index("# --- hermes-target begin"):
                text.index("# --- hermes-target end")]


class HermesTargetTests(unittest.TestCase):
    """A local build cannot be checked out; the script must say what it did.

    The verifier's finding: `hermes_commit` on HQ was `edc8a222a8014b7e…`,
    five commits past v0.23.3 and on no remote, so `git fetch --all --tags`
    cannot bring the object. The shipped `hermes_target` runs here against a
    stub `git` that answers like such a clone.
    """

    COMMIT = "edc8a222a8014b7e60414db02e689e519b46bb24"

    def run_target(self, *, fetchable: bool, branch_has: bool,
                   commit_rc: int, ref_rc: int):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        log = root / "git.log"
        stub = root / "bin" / "git"
        stub.parent.mkdir()
        stub.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{log}"\n'
            'case " $* " in\n'
            '  *" fetch "*) exit 0 ;;\n'
            '  *" branch -r --contains "*)\n'
            '    [ "${BRANCH_HAS}" = 1 ] && printf "origin/main\\n"; exit 0 ;;\n'
            '  *" checkout -q ${GIT_COMMIT} "*) exit "${CHECKOUT_COMMIT}" ;;\n'
            '  *" checkout -q ${GIT_REF} "*) exit "${CHECKOUT_REF}" ;;\n'
            'esac\n'
            "exit 0\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        script = root / "target.sh"
        script.write_text(
            "warn() { printf 'WARNING: %s\\n' \"$*\" >&2; }\n"
            + hermes_target_block()
            + "\nhermes_target\n"
            'printf "TARGET=%s\\nKIND=%s\\n" "$HERMES_TARGET" "$HERMES_TARGET_KIND"\n',
            encoding="utf-8",
        )
        env = {
            "PATH": f"{stub.parent}:/usr/bin:/bin",
            "HOME": str(root),
            "HERMES_COMMIT": self.COMMIT,
            "HERMES_REF": HERMES_REF,
            "HERMES_REF_COMMIT": HERMES_REF_COMMIT,
            "HERMES_FETCHABLE": "true" if fetchable else "false",
            "HERMES_CHECKOUT": str(root / "hermes"),
            "GIT_COMMIT": self.COMMIT,
            "GIT_REF": HERMES_REF,
            "BRANCH_HAS": "1" if branch_has else "0",
            "CHECKOUT_COMMIT": str(commit_rc),
            "CHECKOUT_REF": str(ref_rc),
        }
        result = subprocess.run(["bash", str(script)], capture_output=True,
                                text=True, env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_an_unfetchable_commit_falls_back_to_the_published_ref(self):
        result = self.run_target(fetchable=False, branch_has=False,
                                 commit_rc=1, ref_rc=0)
        self.assertIn(f"TARGET={HERMES_REF}", result.stdout)
        self.assertIn("nearest published ref", result.stdout)
        self.assertIn(f"the backup ran Hermes {self.COMMIT}", result.stderr)
        self.assertIn("local build no new box can fetch", result.stderr)
        self.assertIn(HERMES_REF_COMMIT[:12], result.stderr)

    def test_a_fetchable_commit_is_checked_out_and_pinned(self):
        result = self.run_target(fetchable=True, branch_has=True,
                                 commit_rc=0, ref_rc=1)
        self.assertIn(f"TARGET={self.COMMIT}", result.stdout)
        self.assertIn("the commit the backup recorded", result.stdout)
        self.assertNotIn("WARNING", result.stderr)

    def test_a_commit_flagged_fetchable_but_absent_falls_back_to_the_ref(self):
        # The flag is from the backup; the clone in front of us decides.
        result = self.run_target(fetchable=True, branch_has=True,
                                 commit_rc=1, ref_rc=0)
        self.assertIn(f"TARGET={HERMES_REF}", result.stdout)
        self.assertIn(f"the backup ran Hermes {self.COMMIT}", result.stderr)

    def test_nothing_recorded_is_fetchable_keeps_the_installers_build(self):
        result = self.run_target(fetchable=False, branch_has=False,
                                 commit_rc=1, ref_rc=1)
        self.assertIn("TARGET=\n", result.stdout)
        self.assertIn("installer's own build", result.stdout)
        self.assertIn("no recorded Hermes build could be fetched", result.stderr)


def run_from_copy_block() -> str:
    """The run-from-/var/tmp block the shipped script runs, verbatim."""
    text = SCRIPT.read_text(encoding="utf-8")
    return text[text.index("# --- run-from-a-copy begin"):
                text.index("# --- run-from-a-copy end")]


class RunFromCopyTests(unittest.TestCase):
    """The kit lives under /root, and the restore writes under /root in place.

    restic 0.18 refuses `--include` with `--exclude` ("mutually exclusive"), so
    the exclusion cannot be done with a flag. The protection is a copy: the
    script re-execs itself from /var/tmp and puts the kit's current files back
    after the restore. (Today's backup set carries no kit folder — it is a list
    of subtrees — but the script does not depend on that: a set that did carry
    one would replace the running script and the newer secrets.) This runs the
    shipped block twice — the second pass is the copy — and checks both halves.
    """

    def test_the_script_runs_from_a_var_tmp_copy_carrying_the_current_kit(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        kit = root / "odile-factory-recovery"
        kit.mkdir(mode=0o700)
        (kit / "recovery.env").write_text("CURRENT-KIT-SECRETS\n", encoding="utf-8")
        (kit / "manifest.json").write_text("{}\n", encoding="utf-8")
        script = root / "harness.sh"
        script.write_text(
            "#!/bin/bash\nset -Eeuo pipefail\n"
            f"KIT_DIR={kit}\nENV_FILE={kit}/recovery.env\nLOG={root}/log\nDRY_RUN=0\n"
            'ARGS=("$@")\n'
            + run_from_copy_block()
            + "\nprintf 'RUNNING=%s\\n' \"${ODILE_RECOVERY_RUNNING:-0}\"\n"
            'printf "KIT_DIR=%s\\n" "$KIT_DIR"\n'
            'printf "SCRIPT_DIR=%s\\n" "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"\n'
            'printf "SAVED=%s\\n" "${ODILE_KIT_SAVED:-}"\n'
            'printf "SAVED_ENV=%s\\n" "$(cat "${ODILE_KIT_SAVED:-/nonexistent}/recovery.env")"\n'
            'printf "RUN_DIR=%s\\n" "${ODILE_RUN_DIR:-}"\n'
            'printf "ARGS=%s\\n" "$*"\n',
            encoding="utf-8",
        )
        script.chmod(0o700)
        result = subprocess.run(["bash", str(script), "--no-dns"], capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        values = dict(line.split("=", 1) for line in result.stdout.splitlines()
                      if "=" in line)
        self.addCleanup(shutil.rmtree, values["RUN_DIR"], True)
        self.assertEqual(values["RUNNING"], "1")
        self.assertEqual(values["KIT_DIR"], str(kit))
        self.assertTrue(values["SCRIPT_DIR"].startswith("/var/tmp/odile-recovery-run."),
                        values["SCRIPT_DIR"])
        self.assertNotEqual(values["SCRIPT_DIR"], str(kit))
        self.assertEqual(values["SAVED_ENV"], "CURRENT-KIT-SECRETS")
        # The re-exec must carry the flags: the parse loop consumed them.
        self.assertEqual(values["ARGS"], "--no-dns")

    def test_the_restore_has_no_exclude_and_the_kit_is_put_back(self):
        text = SCRIPT.read_text(encoding="utf-8")
        restore = text[text.index("\nrestic restore"):]
        restore = restore[:restore.index("\n\n")]
        self.assertIn("--include /root", restore)
        self.assertNotIn("--exclude", restore)
        self.assertIn('cp -a -- "$ODILE_KIT_SAVED/." "$KIT_DIR/"', text)
        self.assertIn('chmod 600 "$KIT_DIR/recovery.env"', text)


@unittest.skipUnless(shutil.which("restic"), "restic is not installed here")
class ResticRestoreTests(unittest.TestCase):
    """The constraint that shaped the fix, held as a test.

    An `--exclude` of the kit folder beside `--include /root` is what the
    obvious fix looks like, and restic 0.18.1 refuses it outright: "exclude and
    include patterns are mutually exclusive".
    """

    def test_restic_refuses_include_and_exclude_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "file.txt").write_text("x\n", encoding="utf-8")
            env = {**os.environ, "RESTIC_PASSWORD": "fixture-passphrase",
                   "RESTIC_REPOSITORY": str(root / "repo")}
            env.pop("AWS_ACCESS_KEY_ID", None)
            subprocess.run(["restic", "init"], env=env, check=True,
                           capture_output=True, timeout=120)
            subprocess.run(["restic", "backup", str(source)], env=env, check=True,
                           capture_output=True, timeout=120)
            result = subprocess.run(
                ["restic", "restore", "latest", "--target", str(root / "out"),
                 "--include", str(source), "--exclude", str(source / "file.txt")],
                env=env, capture_output=True, text=True, timeout=120)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("mutually exclusive", result.stderr)


# ---------------------------------------------------------------------------
# The package list, held against the Makefile's own prerequisites
# ---------------------------------------------------------------------------
MAKEFILE = ROOT / "Makefile"
RUNBOOK = ROOT / "docs" / "runbooks" / "backup-hq.md"

# A package the install line does not have to name because another listed
# package depends on it (`apt-cache depends openssh-server` →
# `Depends: openssh-client`). Everything else has to be named: nothing in the
# list is a reverse-dependency of `sshpass` (`apt-cache rdepends sshpass` →
# ansible, network-manager-ssh, freedombox …), so a missing entry is a box
# whose gate exits 1 (`make check: sshpass is not installed`) and whose
# `odilectl provision` cannot install a tenant key over port 23.
COMES_WITH = {
    "openssh-client": "openssh-server",
}


def prerequisite_packages() -> list[tuple[str, str, int]]:
    """(binary, package, Makefile line) for every `prereqs` prerequisite.

    The derivation and the binary→package map are `test_ci_prereq_parity`'s,
    so a prerequisite added to the Makefile is a question in both tests. The
    one difference is `shellcheck`: CI fetches a SHA-256-pinned tarball, while
    a fresh box takes Debian's package of the same name.
    """
    makefile = MAKEFILE.read_text(encoding="utf-8")
    found = []
    for binary, line, token in prereq_binaries(makefile):
        if binary in APT_PACKAGES:
            package = APT_PACKAGES[binary]
        elif binary in TARBALLED:
            package = binary
        else:
            raise AssertionError(
                f"Makefile:{line} (prereqs: command -v {token}) has no package "
                f"in tests/test_ci_prereq_parity.py")
        found.append((binary, package, line))
    return found


def missing_prerequisites(packages: set[str]) -> list[str]:
    """`binary needs package` lines for every prerequisite the list lacks."""
    return [f"make check needs {binary} ({package})"
            for binary, package, _ in prerequisite_packages()
            if package not in packages and COMES_WITH.get(package) not in packages]


def runbook_step_3() -> str:
    """§5A step 3 of the runbook — the by-hand package list — as written."""
    text = RUNBOOK.read_text(encoding="utf-8")
    start = text.index("**3. Packages.**")
    return text[start:text.index("**4. ", start)]


def runbook_step_9() -> str:
    """§5A step 9 of the runbook — the by-hand DNS writes — as written."""
    text = RUNBOOK.read_text(encoding="utf-8")
    start = text.index("**9. DNS.**")
    return text[start:text.index("**10. ", start)]


class RecoveryDnsDocsTests(unittest.TestCase):
    """The AAAA fix is only usable if the two operator documents say it.

    The script moves or deletes the AAAA and rewrites or drops the SPF `ip6:`
    token; an operator doing it by hand (§5A step 9) or through the dashboard
    (README §6) needs the same two commands/wording, or the old IPv6 survives
    the recovery anyway.
    """

    def test_the_operator_guide_names_the_aaaa_and_ip6_change(self):
        readme = (ROOT / "deploy" / "recovery" / "README.md").read_text(encoding="utf-8")
        section = readme[readme.index("## 6."):]
        self.assertIn("AAAA", section)
        self.assertIn("**delete** the AAAA record", section)
        self.assertIn("ip6:", section)

    def test_the_runbook_step_9_shows_both_aaaa_commands(self):
        if not RUNBOOK.is_file():
            self.skipTest("docs/runbooks/backup-hq.md is HQ-only; "
                          "make check-hq overlays it and runs this")
        step = runbook_step_9()
        self.assertIn("AAAA", step)
        self.assertIn("-X DELETE", step)
        self.assertIn("ip6:", step)
        self.assertIn("no public IPv6", step)


def release_images_block() -> str:
    """The republish block the shipped script runs, verbatim.

    It is the answer to a 404: `/srv/odile-releases` is not a backup root
    (ADR §3.1), so a restored HQ serves nothing under `/releases/*` and the
    operator needs the exact four commands, not an empty directory.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    return text[text.index("# --- release-images begin"):
                text.index("# --- release-images end")]


class RecoverySetTests(unittest.TestCase):
    """The two surfaces a restore used to leave dead (t_df30ec09).

    `/opt/odile-test` (with its state and `/etc/odile-bench`) is restored and
    its two units started, because the same run moves `test.odile.fyi`'s A
    record; `/srv/odile-releases` is measured and **not** carried, so the
    script must name the republish instead of leaving a silent 404.
    """

    def run_release_block(self, releases: Path, repo: str = str(ROOT)) -> subprocess.CompletedProcess:
        harness = releases.parent / "release-harness.sh"
        harness.write_text(
            "#!/bin/bash\nset -Eeuo pipefail\n"
            f"REPO={repo}\nRELEASES_DIR={releases}\n"
            "say() { printf '\\n== %s ==\\n' \"$*\"; }\n"
            + release_images_block()
            + "\n",
            encoding="utf-8",
        )
        harness.chmod(0o700)
        return subprocess.run(["bash", str(harness)], capture_output=True,
                              text=True, timeout=60)

    def test_the_restore_carries_the_bench_tree_its_state_and_its_env_file(self):
        text = SCRIPT.read_text(encoding="utf-8")
        restore = text[text.index("\nrestic restore"):]
        restore = restore[:restore.index("\n\n")]
        for path in ("/opt/odile-test", "/var/lib/odile-test", "/etc/odile-bench"):
            self.assertIn(f"--include {path}", restore,
                          f"the restore does not carry {path}; test.odile.fyi answers 502")
        # The bench state must be the service user's after the restore.
        self.assertIn('chown -R odile:odile /var/lib/odile-test', text)

    def test_the_units_started_by_name_include_the_bench(self):
        text = SCRIPT.read_text(encoding="utf-8")
        loop = text[text.index("for unit in odile.service"):]
        loop = loop[:loop.index("done")]
        for unit in ("odile.service", "hermes-serve.service",
                     "odile-bench.service", "odile-bench-seed.service"):
            self.assertIn(unit, loop)
        # The messaging gateway is still the one thing left stopped.
        self.assertNotIn("hermes-gateway", loop)

    def test_an_empty_release_directory_prints_the_republish_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            releases = Path(tmp) / "odile-releases"
            result = self.run_release_block(releases)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Release images: none restored", result.stdout)
        printed = "".join(result.stdout.split())
        self.assertIn("404", printed)
        for command in ("./deploy/image/sealed-image.sh--outdeploy/image/out-sealed",
                        "./deploy/image/uki.sh--in",
                        "./deploy/caddy/deploy_releases.py--publish-disk",
                        "./tools/publish_uki.py--uki"):
            self.assertIn(command, printed)
        self.assertIn("release.md§7", printed)
        # No image is read from the backup; the operator must be told where they come from.
        self.assertIn("isnotinthebackup", printed)

    def test_a_restored_release_directory_reports_its_images_instead(self):
        with tempfile.TemporaryDirectory() as tmp:
            releases = Path(tmp) / "odile-releases"
            (releases / "disks").mkdir(parents=True)
            (releases / "disks" / "0f1e2d3c4b5a6978.raw").write_bytes(b"x" * 16)
            result = self.run_release_block(releases)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f"release images: 1 file(s) restored in {releases}/disks",
                          result.stdout)
            self.assertNotIn("none restored", result.stdout)


class PackageListTests(unittest.TestCase):
    """A fresh box's gate and its provision path need packages nothing pulls in.

    The kit's install step is the only place these arrive, and `make check`
    cannot pull them in — a prerequisite is a binary on PATH, not a dependency.
    """

    def test_the_shipped_install_line_has_a_package_for_each_prerequisite(self):
        self.assertGreaterEqual(len(prerequisite_packages()), 6,
                                "the prereqs target names fewer binaries than expected")
        packages = apt_packages(SCRIPT.read_text(encoding="utf-8"))
        self.assertTrue(packages, "the script's install step names no package")
        self.assertEqual(missing_prerequisites(packages), [])

    def test_the_manual_appendix_installs_the_same_packages(self):
        if not RUNBOOK.is_file():
            self.skipTest("docs/runbooks/backup-hq.md is HQ-only; "
                          "make check-hq overlays it and runs this")
        step = runbook_step_3()
        self.assertEqual(missing_prerequisites(apt_packages(step)), [])
        # The prose names the gate's own prerequisites, so an operator who
        # trims the list knows which lines are load-bearing. (Step 3 also
        # carries the `ln -sf /usr/bin/gh` block, so read between the first
        # package block and the next fence.)
        prose = step.split("```", 2)[-1].split("```", 1)[0]
        self.assertIn("`sshpass`", prose,
                      "§5A step 3 must name sshpass beside openssl")

    def test_the_checks_bite_on_a_list_without_sshpass(self):
        # The pre-fix shape (commit 9fa482a): every package but sshpass.
        shipped = SCRIPT.read_text(encoding="utf-8")
        before = shipped.replace("openssh-server sshpass", "openssh-server", 1)
        self.assertNotEqual(before, shipped)
        self.assertEqual(missing_prerequisites(apt_packages(before)),
                         ["make check needs sshpass (sshpass)"])

