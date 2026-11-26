"""Tests for odile-verify.

The verifier's job is to fail closed. These tests run the real command against
a saved report and the synthetic chain the fixture names (a test-only trust
root; the shipped pin keeps AMD's ARK), with no network: a good report passes, a
wrong pin fails, and a report that was changed after the CPU signed it fails the
signature check. One loopback server proves the URL mode fetches `/attestation`
and nothing else when a local chain binds the report, and ignores a served
`trust` block; a second, with a loopback KDS, proves the command the page prints
fetches two URLs — the machine and the VCEK AMD publishes for its chip.

The release tests use a document derived from the fixture — a JSON summary may
grow a `release` and a `cmdline` without touching the signed bytes. They prove
the selection rule (a release absent from the pin fails, never falls back), the
tenant rule (`--expect` is required), the boot record and its comparison, and
that `--print-pin` merges instead of replacing the release map. No test writes
the repository pin: `--print-pin` goes to stdout and `--record` to a temp file.
"""

from __future__ import annotations

import base64
import hashlib
import html
import http.client
import importlib.util
import json
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERIFY = ROOT / "odile-verify"
ACCESS = ROOT / "scripts" / "verify_and_unlock.sh"
ATTEST = ROOT / "attest.py"
FIXTURE = ROOT / "tests" / "fixtures" / "verify" / "synthetic-report.json"
# The same saved report with the two unsigned summary fields a v2 boot report
# carries: the release it booted and the command line the CPU measured. The
# signed bytes are identical (test_the_v2_fixture_only_adds_unsigned_fields).
FIXTURE_V2 = ROOT / "tests" / "fixtures" / "verify" / "synthetic-report-v2.json"
TENANT_FIXTURE = ROOT / "tests" / "fixtures" / "verify" / "synthetic-tenant.json"
DEMO_FIXTURE = ROOT / "tests" / "fixtures" / "verify" / "synthetic-demo.json"
TENANT_VCEK = ROOT / "tests" / "fixtures" / "verify" / "synthetic-vcek.der"
RELEASE_2026_11_2 = ROOT / "tests" / "fixtures" / "verify" / "servury-RELEASE-2026.11.2.json"
PIN = ROOT / "odile-verify.pin"
FIXTURE_BYTES = FIXTURE.read_bytes()
FIXTURE_V2_BYTES = FIXTURE_V2.read_bytes()
DEMO_BYTES = DEMO_FIXTURE.read_bytes()
DEMO_DOC = json.loads(DEMO_BYTES)
TENANT_BYTES = TENANT_FIXTURE.read_bytes()
TENANT_VCEK_BYTES = TENANT_VCEK.read_bytes()
TENANT_DOC = json.loads(TENANT_BYTES)
TENANT_MEASUREMENT = TENANT_DOC["measurement"]
TENANT_REPORT = base64.b64decode(TENANT_DOC["report_b64"])
TENANT_CHIP = TENANT_REPORT[0x1A0:0x1E0].hex()
TENANT_TCB = TENANT_REPORT[0x180:0x188]
# The synthetic fixtures name their own test-only chain. A URL run must pass
# it explicitly, because a served document may not choose its trust root.
SYNTHETIC_TRUST = TENANT_DOC["trust"]
SYNTHETIC_AMD_DIR = TENANT_FIXTURE.parent / SYNTHETIC_TRUST["amd_dir"]
SYNTHETIC_ARK = SYNTHETIC_TRUST["ark_sha256"]

# The /verify page (ODL-236): the program, its template and the machine's
# reported sources. The release pair is the real signed manifest this tree
# ships, so the page's signature check is tested against real bytes.
PAGE = ROOT / "verify" / "verify.py"
TEMPLATE = ROOT / "templates" / "verify.html"
AUDIT = ROOT / "scripts" / "audit.py"
SUPPORT = ROOT / "support" / "odile_support.py"
RELEASE_MANIFEST = ROOT / "release" / "manifest.json"
RELEASE_SIG = ROOT / "release" / "manifest.json.sig"
RELEASE_KEY = ROOT / "release" / "odile-signing-key.pub"
TENANT_KDS_PATH = (
    "/vcek/v1/Milan/%s?blSPL=%d&teeSPL=%d&snpSPL=%d&ucodeSPL=%d"
    % (TENANT_CHIP, TENANT_TCB[0], TENANT_TCB[1], TENANT_TCB[6], TENANT_TCB[7])
)
PIN_BYTES = PIN.read_bytes()
VERIFY_MD = ROOT / "docs" / "runbooks" / "verify.md"
RECUT_MD = ROOT / "docs" / "product" / "CLAIMS-RECUT-STRICT.md"
VERDICT_MD = ROOT / "docs" / "security" / "CLAIMS-VERDICT-strict.md"
# The deck itself. Its §4.4 block is the one a judge copies into a terminal,
# so it is bound to the bundle like the three homes above: a pin change that
# moves the tested three must move the deck's hash line in the same commit.
CLAIMS_MD = ROOT / "docs" / "CLAIMS.md"

NOT_LIVENESS = "the boot report, not a liveness proof"
RELEASE = "2026.11.1"
CMDLINE = (
    "console=ttyS0 consoleblank=0 root=/dev/mapper/root_crypt ro "
    "ds=nocloud;s=/run/blackbox/seed/ bb.release=2026.11.1 bb.iid=test "
    "bb.key=c3NoLWVkMjU1MTkgdGVzdA== end initrd=initrd"
)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFY), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
    )


def tampered_document(offset: int | None = None, json_measurement: str | None = None) -> str:
    """A copy of the fixture with one signed byte and/or the summary changed."""
    data = json.loads(FIXTURE_BYTES)
    if offset is not None:
        raw = bytearray(base64.b64decode(data["report_b64"]))
        raw[offset] ^= 0x01
        data["report_b64"] = base64.b64encode(bytes(raw)).decode("ascii")
    if json_measurement is not None:
        data["measurement"] = json_measurement
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8", dir=FIXTURE.parent)
    with handle:
        json.dump(data, handle)
    return handle.name


def document_with(**fields: object) -> str:
    """The fixture summary plus extra, unsigned fields (release, cmdline)."""
    data = json.loads(FIXTURE_BYTES)
    data.update(fields)
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8", dir=FIXTURE.parent)
    with handle:
        json.dump(data, handle)
    return handle.name


def measurement() -> str:
    return json.loads(FIXTURE_BYTES)["measurement"]


def free_port() -> int:
    """A loopback port nobody is listening on (the kernel picks it)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def load_page():
    """verify/verify.py as a module: the served page's cache is one object."""
    spec = importlib.util.spec_from_file_location("odile_verify_page_odl947", PAGE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class VerifyTests(unittest.TestCase):
    def test_pass_against_the_demo_fixture(self) -> None:
        result = run("--report", str(DEMO_FIXTURE), "--expect", DEMO_DOC["measurement"],
                     "--pin", str(PIN))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for word in ("chain", "signature", "measurement", "fetched_at"):
            self.assertIn(word, result.stdout)
        self.assertIn(NOT_LIVENESS, result.stdout)
        self.assertIn("PASS", result.stdout)
        # The measurement in the output is the one signed into the report.
        self.assertIn(DEMO_DOC["measurement"], result.stdout)

    def test_wrong_pin_fails(self) -> None:
        result = run("--report", str(FIXTURE), "--expect", "00" * 48)
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL", result.stdout)
        self.assertIn("expected", result.stdout)
        self.assertIn(NOT_LIVENESS, result.stdout)

    def test_tampered_measurement_fails_the_signature(self) -> None:
        path = tampered_document(offset=0x90)
        try:
            result = run("--report", path)
        finally:
            Path(path).unlink()
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stdout, r"signature\s+FAIL")
        self.assertIn(NOT_LIVENESS, result.stdout)

    def test_a_summary_that_lies_about_the_measurement_fails(self) -> None:
        path = tampered_document(json_measurement="11" * 48)
        try:
            result = run("--report", path, "--expect", "11" * 48)
        finally:
            Path(path).unlink()
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stdout, r"served\s+FAIL")
        # The signed bytes are untouched, so the signature itself still verifies.
        self.assertRegex(result.stdout, r"signature\s+PASS")

    def test_url_mode_fetches_exactly_one_path(self) -> None:
        seen: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib name
                seen.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(DEMO_BYTES)))
                self.end_headers()
                self.wfile.write(DEMO_BYTES)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d" % server.server_address[1]
            # The served document carries a `trust` block; it must be ignored
            # (explicit --amd-dir/--ark-sha256 below), so the only fetch is
            # /attestation.
            result = run(url, "--expect", DEMO_DOC["measurement"],
                         "--amd-dir", str(SYNTHETIC_AMD_DIR),
                         "--ark-sha256", SYNTHETIC_ARK)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(seen, ["/attestation"])

    def test_a_served_trust_block_is_not_honoured(self) -> None:
        """A remote document must not choose the key it is verified against.

        DEMO_BYTES carries a `trust` block naming the synthetic chain. In URL
        mode the verifier ignores it; with --no-kds the missing vendored VCEK
        fails closed instead of accepting the served root.
        """
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib name
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(DEMO_BYTES)))
                self.end_headers()
                self.wfile.write(DEMO_BYTES)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d" % server.server_address[1]
            result = run(url, "--expect", DEMO_DOC["measurement"], "--no-kds")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"vcek\s+FAIL")
        self.assertNotRegex(result.stdout, r"trust\s+PASS")

    def test_a_fixture_trust_block_cannot_escape_its_directory(self) -> None:
        data = json.loads(FIXTURE_BYTES)
        data["trust"] = dict(data["trust"], amd_dir="../../..")
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8", dir=FIXTURE.parent)
        with handle:
            json.dump(data, handle)
        try:
            result = run("--report", handle.name)
        finally:
            Path(handle.name).unlink()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("unsafe amd_dir", result.stdout)

    def test_a_fixture_naming_a_missing_chain_fails_closed(self) -> None:
        data = json.loads(FIXTURE_BYTES)
        data["trust"] = dict(data["trust"], amd_dir="no-such-chain")
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8", dir=FIXTURE.parent)
        with handle:
            json.dump(data, handle)
        try:
            result = run("--report", handle.name)
        finally:
            Path(handle.name).unlink()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("trust chain is missing", result.stdout)

    def test_a_redirect_is_not_followed(self) -> None:
        seen: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib name
                seen.append(self.path)
                if self.path == "/attestation":
                    self.send_response(302)
                    self.send_header("Location", "/elsewhere")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(FIXTURE_BYTES)))
                self.end_headers()
                self.wfile.write(FIXTURE_BYTES)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d" % server.server_address[1]
            result = run(url)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(seen, ["/attestation"])
        self.assertIn("302", result.stdout)

    def test_a_bad_url_is_a_usage_error(self) -> None:
        result = run("ftp://example.com")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not an http(s) URL", result.stderr)

    def test_a_short_report_fails_closed(self) -> None:
        data = json.loads(FIXTURE_BYTES)
        data["report_b64"] = base64.b64encode(b"\x05" + b"\x00" * 31).decode("ascii")
        data["report_size"] = 32
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8", dir=FIXTURE.parent)
        with handle:
            json.dump(data, handle)
        try:
            result = run("--report", handle.name)
        finally:
            Path(handle.name).unlink()
        self.assertEqual(result.returncode, 1)
        self.assertIn("report is 32 bytes", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_print_pin_keeps_stdout_machine_readable(self) -> None:
        result = run("--print-pin", "--report", str(FIXTURE))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        pin = json.loads(result.stdout)
        self.assertEqual(pin["measurement"], json.loads(FIXTURE_BYTES)["measurement"])
        self.assertEqual(len(pin["measurement"]), 96)
        self.assertIn("odile-verify", result.stderr)

    def test_the_pin_records_its_source_and_commit(self) -> None:
        pin = json.loads(PIN.read_text(encoding="utf-8"))
        self.assertRegex(pin["measurement"], r"^[0-9a-f]{96}$")
        self.assertRegex(pin["fetched_at"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertRegex(pin["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(pin["report_version"], 5)

    def test_the_pin_is_a_schema_2_release_recipe(self) -> None:
        """The demo record stays v1; the release entry is the recipe.

        The hashes below were copied from the live release directory
        (sha256sum -c SHA256SUMS, 4/4 OK) and from RELEASE.json itself, not
        from prose. The §Evidence transcript in docs/runbooks/verify.md shows
        the commands.
        """
        pin = json.loads(PIN.read_text(encoding="utf-8"))
        self.assertEqual(pin["schema"], 2)
        self.assertEqual(pin["provision_release"], RELEASE)
        # The v1 top-level fields are the demo machine's record, byte for byte.
        self.assertEqual(
            pin["measurement"],
            "e99728efbbf23ae517bde68b7667448aa98ed8905e48cddae037ff1742cd10f3"
            "116f98b3ece36aad2e4c8b9e72643fbc",
        )
        self.assertEqual(pin["report_version"], 5)
        self.assertEqual(pin["policy"], "0x30000")
        self.assertEqual(pin["fetched_at"], "2026-11-13T01:55:04Z")
        self.assertEqual(pin["pinned_at"], "2026-11-13")
        self.assertEqual(pin["source"], "https://odile.fyi/attestation")
        self.assertEqual(pin["commit"], "9678eb3ae28d736a975d24ac685efa29a1cce6ae")
        entry = pin["releases"][RELEASE]
        self.assertEqual(
            entry["release_url"],
            "https://servury.com/downloads/blackbox/2026.11.1/",
        )
        self.assertEqual(
            entry["release_json_sha256"],
            "aa37e4ade903aa933eb702f9d9d2191fc4861a1c723ae6e7e0e986e1d4eb9a65",
        )
        self.assertEqual(entry["artifacts"], {
            "OVMF.amdsev.fd":
                "8731846cdbcbb6aa5c624238b839544b1553f4c5af874450f043321b3ef8958b",
            "vmlinuz":
                "96758c40859c94163e5cf81399a107d679e984e251cb4e13aba4e7d92f5d14f2",
            "initrd.img":
                "8c2aff57573c4af11289553fa185c8f13613acca688a52c2e579d4ea9c2beaf7",
        })
        self.assertEqual(entry["measurement_parameters"], {
            "vcpu_type": "EPYC-v4",
            "guest_features": "0x1",
            "policy": "0x30000",
            "vmm": "QEMU",
        })
        self.assertEqual(entry["vcpu_counts"], [4])
        self.assertEqual(entry["inputs"]["kernel_version"], "6.12.115+deb13.1-amd64")
        self.assertEqual(entry["inputs"]["edk2"], "edk2 edk2-stable202505 unknown")

    def test_the_v2_fixture_only_adds_unsigned_fields(self) -> None:
        v1 = json.loads(FIXTURE_BYTES)
        v2 = json.loads(FIXTURE_V2_BYTES)
        self.assertEqual(v1["report_b64"], v2["report_b64"])
        for key in ("measurement", "report_version", "report_size", "fetched_at"):
            self.assertEqual(v1[key], v2[key])
        self.assertEqual(v2["release"], RELEASE)
        self.assertEqual(v2["vcpus"], 4)
        self.assertTrue(v2["cmdline"].endswith(" initrd=initrd"))

    def test_a_plan_the_entry_was_not_measured_on_fails(self) -> None:
        path = document_with(**json.loads(FIXTURE_V2_BYTES))
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["vcpus"] = 2
        Path(path).write_text(json.dumps(data), encoding="utf-8")
        try:
            result = run("--report", path, "--expect", measurement())
        finally:
            Path(path).unlink()
        self.assertEqual(result.returncode, 1)
        self.assertIn("measured at vcpu_counts [4]", result.stdout)
        self.assertRegex(result.stdout, r"release\s+FAIL")

    def test_release_selected_from_the_document_prints_the_record(self) -> None:
        result = run("--report", str(FIXTURE_V2), "--expect", measurement())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"release\s+PASS")
        self.assertRegex(result.stdout, r"expected\s+PASS")
        self.assertIn("odile.boot-record.v1", result.stdout)
        self.assertIn('"release": "2026.11.1"', result.stdout)
        self.assertIn(NOT_LIVENESS, result.stdout)

    def test_a_release_absent_from_the_pin_fails_and_does_not_fall_back(self) -> None:
        path = document_with(release="2026.12.9", cmdline=CMDLINE)
        try:
            result = run("--report", path, "--expect", measurement())
        finally:
            Path(path).unlink()
        self.assertEqual(result.returncode, 1)
        self.assertIn("release not pinned", result.stdout)
        self.assertRegex(result.stdout, r"release\s+FAIL")
        self.assertNotRegex(result.stdout, r"release\s+PASS")

    def test_a_tenant_run_without_expect_is_a_usage_error(self) -> None:
        result = run("--report", str(FIXTURE_V2))
        self.assertEqual(result.returncode, 2)
        self.assertIn("--expect", result.stderr)

    def test_release_flag_must_match_the_served_document(self) -> None:
        result = run("--report", str(FIXTURE_V2), "--release", "2026.12.9",
                     "--expect", measurement())
        self.assertEqual(result.returncode, 1)
        self.assertIn("the served document says 2026.11.1", result.stdout)
        self.assertRegex(result.stdout, r"release\s+FAIL")

    def test_record_writes_the_section_43_record(self) -> None:
        pin = json.loads(PIN.read_text(encoding="utf-8"))
        entry = pin["releases"][RELEASE]
        cmdline = json.loads(FIXTURE_V2_BYTES)["cmdline"]
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.close()
        try:
            result = run("--report", str(FIXTURE_V2), "--expect", measurement(),
                         "--record", handle.name)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            record = json.loads(Path(handle.name).read_text(encoding="utf-8"))
        finally:
            Path(handle.name).unlink()
        self.assertEqual(record["schema"], "odile.boot-record.v1")
        self.assertEqual(record["release"], RELEASE)
        self.assertEqual(record["artifacts"], entry["artifacts"])
        self.assertEqual(record["measurement_parameters"],
                         entry["measurement_parameters"])
        self.assertEqual(record["vcpus"], 4)
        stripped = cmdline[:-len(" initrd=initrd")]
        self.assertEqual(record["cmdline_sha256"],
                         hashlib.sha256(stripped.encode("utf-8")).hexdigest())
        self.assertEqual(len(stripped.encode("utf-8")), 1144)
        self.assertEqual(record["measurement"], measurement())
        self.assertRegex(record["checked_at"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertIn("odile-verify", record["verifier"])
        # No test writes state: the repository pin is untouched.
        self.assertEqual(PIN.read_bytes(), PIN_BYTES)

    def test_record_without_a_pinned_release_is_a_usage_error(self) -> None:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.close()
        try:
            result = run("--report", str(FIXTURE), "--record", handle.name)
        finally:
            Path(handle.name).unlink()
        self.assertEqual(result.returncode, 2)
        self.assertIn("pinned release", result.stderr)
        self.assertFalse(Path(handle.name).exists())

    def test_previous_says_same_or_names_the_field_that_moved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "record.json"
            first = run("--report", str(FIXTURE_V2), "--expect", measurement(),
                        "--record", str(record))
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            same = run("--report", str(FIXTURE_V2), "--expect", measurement(),
                       "--previous", str(record))
            self.assertEqual(same.returncode, 0, same.stdout + same.stderr)
            self.assertRegex(same.stdout, r"previous\s+same")
            for field, moved_value in (
                ("measurement", "11" * 48),
                ("release", "2026.12.9"),
                ("cmdline_sha256", "22" * 32),
            ):
                previous = json.loads(record.read_text(encoding="utf-8"))
                previous[field] = moved_value
                moved = Path(tmp) / ("previous-%s.json" % field)
                moved.write_text(json.dumps(previous), encoding="utf-8")
                result = run("--report", str(FIXTURE_V2), "--expect", measurement(),
                             "--previous", str(moved))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("%s moved" % field, result.stdout)

    def test_print_pin_merges_instead_of_replacing(self) -> None:
        text = PIN.read_text(encoding="utf-8")
        pin = json.loads(text)
        first = run("--print-pin", "--report", str(FIXTURE))
        second = run("--print-pin", "--report", str(FIXTURE))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        merged = json.loads(first.stdout)
        again = json.loads(second.stdout)
        self.assertEqual(merged["schema"], pin["schema"])
        self.assertEqual(merged["provision_release"], pin["provision_release"])
        self.assertEqual(merged["releases"], pin["releases"])
        # The map is byte-identical, not merely equal after parsing: the
        # runbook redirects --print-pin stdout into the file.
        self.assertEqual(first.stdout[first.stdout.index('"releases"'):],
                         text[text.index('"releases"'):])
        # A second refresh moves nothing outside the demo record's three fields.
        for key in set(merged) | set(again):
            if key in ("fetched_at", "pinned_at", "commit"):
                continue
            self.assertEqual(merged.get(key), again.get(key), key)

    def test_the_demo_source_keeps_the_v1_record(self) -> None:
        body = DEMO_BYTES

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib name
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        pin = json.loads(PIN_BYTES)
        url = "http://127.0.0.1:%d" % server.server_address[1]
        pin["source"] = url + "/attestation"
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8")
        with handle:
            json.dump(pin, handle, ensure_ascii=False)
        try:
            result = run(url, "--pin", handle.name,
                         "--amd-dir", str(SYNTHETIC_AMD_DIR),
                         "--ark-sha256", SYNTHETIC_ARK)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            Path(handle.name).unlink()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"release\s+PASS")
        self.assertRegex(result.stdout, r"pin\s+PASS")
        self.assertIn("odile.boot-record.v1", result.stdout)
        self.assertNotIn("--expect", result.stdout)


class AttestMetadataTests(unittest.TestCase):
    """attest.py puts the measured command line's release into its document."""

    def load(self):
        spec = importlib.util.spec_from_file_location("odile_attest", ATTEST)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_release_and_cmdline_are_read_from_the_command_line(self) -> None:
        module = self.load()
        self.assertEqual(module.cmdline_value(CMDLINE, "bb.release"), RELEASE)
        self.assertIsNone(module.cmdline_value("console=ttyS0", "bb.release"))
        handle = tempfile.NamedTemporaryFile("w", suffix=".cmdline", delete=False)
        with handle:
            handle.write(CMDLINE + "\n")
        try:
            self.assertEqual(module.read_cmdline(handle.name), CMDLINE)
        finally:
            Path(handle.name).unlink()

    def test_main_writes_release_and_cmdline_even_when_the_device_fails(self) -> None:
        import contextlib
        import io
        from unittest import mock

        module = self.load()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "attestation.json"
            argv = sys.argv
            sys.argv = ["attest.py", str(out)]
            try:
                with mock.patch.object(module, "read_cmdline", return_value=CMDLINE), \
                        mock.patch.object(module, "fetch", side_effect=OSError("no device")), \
                        mock.patch.object(module.os, "cpu_count", return_value=4), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(module.main(), 0)
            finally:
                sys.argv = argv
            data = json.loads(out.read_text(encoding="utf-8"))
        self.assertFalse(data["available"])
        self.assertEqual(data["release"], RELEASE)
        self.assertEqual(data["cmdline"], CMDLINE)
        self.assertEqual(data["vcpus"], 4)

_APP = None


def load_app():
    """app.py as a module, loaded once: the page renderer the tests exercise."""
    global _APP
    if _APP is None:
        spec = importlib.util.spec_from_file_location("odile_app_odl473", ROOT / "app.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _APP = module
    return _APP


_SNP = None


def load_installer():
    """The signed installer as a module: its parser is the installed one."""
    spec = importlib.util.spec_from_file_location(
        "odile_install_odl474b", ROOT / "installer" / "odile_install.py")
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load installer/odile_install.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_snp_report():
    """tools/snp_report.py as a module: the KDS and TCB helpers under test."""
    global _SNP
    if _SNP is None:
        spec = importlib.util.spec_from_file_location(
            "odile_snp_report_tenant", ROOT / "tools" / "snp_report.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _SNP = module
    return _SNP


class AccessReportTests(unittest.TestCase):
    """The strict-bar report `verify_and_unlock.sh --mode access` prints.

    The bench is a guest root in a temp directory plus the saved report: no
    machine, no ssh, no network. The card's five lines are the evidence the
    claims re-cut cites, so each one gets a PASS case and a FAIL case.
    """

    # A real ed25519 public key whose private half was generated once and
    # shredded: exactly the throwaway the strict bar provisions. The fixture
    # reuses it so the report (and the page that quotes it) is deterministic.
    THROWAWAY = (
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINoumjNprJdWOjWlvt/gn/bdQ+BuqHZ4UeE4aM2p87hE"
        " bench throwaway boot key"
    )
    FINGERPRINT = "SHA256:LLwDfYyE3VMc9T9o+XvydfNzrXxyzaQ2lysSxCSSEWA"
    SEAL_ID = "strict-bench-v1"
    SCRYPT = "scrypt$16384$8$1$c2FsdC1mb3ItdGhlLWJlbmNo$aGFzaC1mb3ItdGhlLWJlbmNo"

    def command_line(self, *, key: str | None = None, seal_id: str | None = "seal") -> str:
        base = json.loads(FIXTURE_V2_BYTES)["cmdline"]
        tokens = [
            token for token in base.split()
            if not token.startswith("bb.key=") and token != "initrd=initrd"
        ]
        tokens.append("bb.key=" + base64.b64encode((key or self.THROWAWAY).encode()).decode())
        if seal_id is not None:
            tokens.append("seal_id=" + self.SEAL_ID)
        tokens.append("initrd=initrd")
        return " ".join(tokens)

    def bench(
        self,
        tmp: Path,
        *,
        cmdline: str | None = None,
        running: str | None = None,
        keys: tuple[str, ...] = (),
        policy: int | None = None,
        hash_line: str | None = "scrypt",
        extra_env: tuple[str, ...] = (),
        attestation: bool = True,
        private_key: tuple[str, str] | None = None,
        extra_files: dict[str, str] | None = None,
        write_authorized_keys: bool = True,
    ) -> Path:
        root = tmp / "guest"
        for directory in ("proc", "root/.ssh", "home/odile/.hermes", "var/lib/odile"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        measured = cmdline if cmdline is not None else self.command_line()
        (root / "proc/cmdline").write_text((running if running is not None else measured) + "\n")
        if write_authorized_keys:
            (root / "root/.ssh/authorized_keys").write_text(
                "\n".join(keys or (self.THROWAWAY,)) + "\n")
        env = ["HERMES_DASHBOARD_BASIC_AUTH_USERNAME=odile"]
        if hash_line is not None:
            env.append("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH="
                       + (self.SCRYPT if hash_line == "scrypt" else hash_line))
        env.append("HERMES_DASHBOARD_BASIC_AUTH_SECRET=bench")
        env.extend(extra_env)
        (root / "home/odile/.hermes/.env").write_text("\n".join(env) + "\n")
        if attestation:
            document = json.loads(FIXTURE_V2_BYTES)
            document["cmdline"] = measured
            if policy is not None:
                raw = bytearray(base64.b64decode(document["report_b64"]))
                struct.pack_into("<Q", raw, 8, policy)
                document["report_b64"] = base64.b64encode(bytes(raw)).decode("ascii")
            (root / "var/lib/odile/attestation.json").write_text(json.dumps(document))
        if private_key is not None:
            path, body = private_key
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
            target.chmod(0o600)
        for path, body in (extra_files or {}).items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            target.chmod(0o600)
        return root

    def report(self, root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ACCESS), "--mode", "access", "--root", str(root), "--user", "odile", *extra],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=ROOT,
        )

    def test_the_bench_report_passes_and_names_every_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(Path(tmp))
            result = self.report(root, "--expect", measurement())
            # A second run reads the same bench and prints the same report: no state.
            again = self.report(root, "--expect", measurement())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for label in ("usable keys", "boot key", "seal", "sign-in hash", "operator key"):
            self.assertRegex(result.stdout, r"%s\s+PASS" % label)
        self.assertIn("0 usable user keys", result.stdout)
        self.assertIn(self.FINGERPRINT, result.stdout)
        self.assertIn("every authorized_keys line is the measured boot key", result.stdout)
        self.assertIn("no private half of it on the machine", result.stdout)
        self.assertIn("policy 0x30000", result.stdout)
        self.assertIn("seal_id " + self.SEAL_ID, result.stdout)
        self.assertIn(measurement(), result.stdout)
        self.assertIn("scrypt hash present", result.stdout)
        self.assertIn("none", result.stdout)
        self.assertIn("PASS", result.stdout.splitlines()[-1])
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual(again.stdout, result.stdout)

    def test_a_private_half_makes_a_key_usable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "bench-user",
                 "-f", str(tmp_path / "user")],
                check=True,
            )
            public = (tmp_path / "user.pub").read_text(encoding="utf-8").strip()
            private = (tmp_path / "user").read_text(encoding="utf-8")
            root = self.bench(
                tmp_path,
                keys=(self.THROWAWAY, public),
                private_key=("home/odile/.ssh/id_ed25519", private),
            )
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"usable keys\s+FAIL")
        self.assertIn("1 private half on the machine", result.stdout)
        self.assertRegex(result.stdout, r"boot key\s+PASS")
        self.assertRegex(result.stdout, r"operator key\s+PASS")

    def test_the_throwaway_private_half_fails_both_key_lines(self) -> None:
        """A private half of the measured boot key is the strict bar's one sin."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "bench-throwaway",
                 "-f", str(tmp_path / "boot")],
                check=True,
            )
            public = (tmp_path / "boot.pub").read_text(encoding="utf-8").strip()
            private = (tmp_path / "boot").read_text(encoding="utf-8")
            line = self.command_line(key=public)
            root = self.bench(
                tmp_path,
                cmdline=line,
                running=line,
                keys=(public,),
                private_key=("root/.ssh/id_ed25519", private),
            )
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"usable keys\s+FAIL")
        self.assertRegex(result.stdout, r"boot key\s+FAIL")
        self.assertIn("private half on the machine", result.stdout)

    def test_a_grant_shaped_line_fails_usable_keys_and_operator_key(self) -> None:
        """A support grant is an options-prefixed key line, not a plain key."""
        grant = (
            'restrict,command="/usr/local/bin/odile-support" '
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMdWyPF1Bcwd6sFiL4KiUtmYTq2FLe95TmfBQS0fEnu2"
            " support@hq"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(Path(tmp), keys=(self.THROWAWAY, grant))
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"usable keys\s+FAIL")
        self.assertRegex(result.stdout, r"operator key\s+FAIL")
        self.assertIn("authorized_keys carries", result.stdout)
        self.assertIn("odile-support", result.stdout)

    def test_a_user_key_in_a_home_authorized_keys_fails_usable_keys(self) -> None:
        """`odile` runs a shell with NOPASSWD sudo: its own key is a login.

        The report used to read only root's authorized_keys, so a user key in
        the tenant user's own file left "0 usable user keys" PASS.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "bench-user",
                 "-f", str(tmp_path / "user")],
                check=True,
            )
            public = (tmp_path / "user.pub").read_text(encoding="utf-8").strip()
            private = (tmp_path / "user").read_text(encoding="utf-8")
            root = self.bench(
                tmp_path,
                extra_files={
                    "home/odile/.ssh/authorized_keys": public + "\n",
                    "home/odile/.ssh/id_ed25519": private,
                },
            )
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"usable keys\s+FAIL")
        self.assertIn("other than the measured throwaway", result.stdout)
        self.assertIn("/home/odile/.ssh/authorized_keys", result.stdout)
        self.assertRegex(result.stdout, r"boot key\s+PASS")

    def test_a_grant_in_a_home_authorized_keys_fails_operator_key(self) -> None:
        """A support grant in the tenant user's file is still an operator key."""
        grant = (
            'restrict,command="/usr/local/bin/odile-support" '
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMdWyPF1Bcwd6sFiL4KiUtmYTq2FLe95TmfBQS0fEnu2"
            " support@hq"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(
                Path(tmp),
                extra_files={"home/odile/.ssh/authorized_keys": grant + "\n"},
            )
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"usable keys\s+FAIL")
        self.assertRegex(result.stdout, r"operator key\s+FAIL")
        self.assertIn("odile-support", result.stdout)
        self.assertIn("/home/odile/.ssh/authorized_keys", result.stdout)

    # -- a live support grant is named, not a bare FAIL (t_e23f3288) -----
    #
    # A grant is a key in authorized_keys by design, so `usable keys` and
    # `operator key` fail while it is open. What the page lacked was a row
    # that says *why*: the grant id, its expiry and the key fingerprint. The
    # state file is the witness, not the line's shape — a hand-written
    # grant-shaped line with no live state must still fail (the two tests
    # above pin that).
    GRANT_ID = "7bc06cc48244"
    GRANT_EXPIRES = "2099-01-01T00:00:00Z"
    GRANT_KEY = ("ssh-ed25519 "
                 "AAAAC3NzaC1lZDI1NTE5AAAAIMdWyPF1Bcwd6sFiL4KiUtmYTq2FLe95TmfBQS0fEnu2"
                 " support@hq")
    OTHER_KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIBW0mNRc2zHqyCxjwkzfdIt+lubwOEBY/Repnbm1lqCn"

    def key_fingerprint(self, blob: str) -> str:
        return "SHA256:" + base64.b64encode(
            hashlib.sha256(base64.b64decode(blob)).digest()).decode().rstrip("=")

    def grant_line(self, *, blob: str | None = None, grant_id: str | None = None,
                   command: str = "/usr/local/sbin/odile-support session") -> str:
        """The one line `odile-support grant` appends to root's key file."""
        blob = blob or self.GRANT_KEY.split()[1]
        return ('command="%s",no-agent-forwarding,no-port-forwarding,'
                "no-X11-forwarding,no-user-rc ssh-ed25519 %s odile-support-grant-%s"
                % (command, blob, grant_id or self.GRANT_ID))

    def grant_state(self, **fields: object) -> dict:
        """A live grant's state file: public material only, 0644 on a tenant."""
        blob = self.GRANT_KEY.split()[1]
        state = {
            "version": 1,
            "grant_id": self.GRANT_ID,
            "slug": "odl-bench",
            "reason": "ODL-217 drill: grant, use, expire",
            "created_at": "2026-11-12T21:23:43Z",
            "expires_at": self.GRANT_EXPIRES,
            "hours": 24.0,
            "key_type": "ssh-ed25519",
            "key_blob": blob,
            "key_fingerprint": self.key_fingerprint(blob),
            "key_comment": "support@hq",
        }
        state.update(fields)
        return state

    def test_a_live_grant_is_its_own_open_row(self) -> None:
        """The two FAIL rows stay honest; a sixth row names the grant."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(
                Path(tmp),
                keys=(self.THROWAWAY, self.grant_line()),
                extra_files={"var/lib/odile/support-grant.json":
                             json.dumps(self.grant_state())},
            )
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"usable keys\s+FAIL")
        self.assertRegex(result.stdout, r"operator key\s+FAIL")
        self.assertRegex(result.stdout, r"support grant\s+OPEN")
        self.assertIn(self.GRANT_ID, result.stdout)
        self.assertIn(self.GRANT_EXPIRES, result.stdout)
        self.assertIn(self.key_fingerprint(self.GRANT_KEY.split()[1]), result.stdout)
        self.assertIn("line present", result.stdout)
        # The verdict is the grant, not the bare FAIL that read as a break.
        self.assertRegex(result.stdout.splitlines()[-1], r"^  OPEN \u2014 ")
        self.assertIn("the two key rows fail because that granted key is in", result.stdout)
        self.assertNotIn("FAIL \u2014 the access report did not pass", result.stdout)

    def test_a_live_grant_and_a_failed_seal_names_both(self) -> None:
        """A grant explains the key rows, not the seal; the verdict says both.

        The verdict used to be chosen from `grant` before `fails`, so a
        machine that also failed attestation read as "only the grant is open",
        and the causal sentence blamed two rows while three had failed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(
                Path(tmp),
                keys=(self.THROWAWAY, self.grant_line()),
                policy=0x30001,
                extra_files={"var/lib/odile/support-grant.json":
                             json.dumps(self.grant_state())},
            )
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"seal\s+FAIL")
        self.assertRegex(result.stdout, r"support grant\s+OPEN")
        verdict = result.stdout.splitlines()[-1]
        self.assertRegex(verdict, r"^  OPEN \u2014 a support grant is live until ")
        self.assertIn(self.GRANT_ID, verdict)
        self.assertIn("1 other check FAIL (seal)", verdict)
        self.assertIn("the report did not pass", verdict)
        self.assertNotIn("the two key rows fail", verdict)

    def test_a_live_grant_and_a_planted_key_name_the_other_failure(self) -> None:
        """`usable keys` failing for more than the grant is still reported.

        The row said two foreign keys and a private half; the verdict said the
        two key rows fail because of the grant, which the row contradicts.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "bench-user",
                 "-f", str(tmp_path / "user")],
                check=True,
            )
            public = (tmp_path / "user.pub").read_text(encoding="utf-8").strip()
            private = (tmp_path / "user").read_text(encoding="utf-8")
            root = self.bench(
                tmp_path,
                keys=(self.THROWAWAY, self.grant_line(), public),
                private_key=("home/odile/.ssh/id_ed25519", private),
                extra_files={"var/lib/odile/support-grant.json":
                             json.dumps(self.grant_state())},
            )
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("2 authorized key(s) other than the measured throwaway", result.stdout)
        self.assertIn("1 private half on the machine", result.stdout)
        self.assertRegex(result.stdout, r"support grant\s+OPEN")
        verdict = result.stdout.splitlines()[-1]
        self.assertRegex(verdict, r"^  OPEN \u2014 a support grant is live until ")
        self.assertIn(self.GRANT_ID, verdict)
        self.assertIn("1 other check FAIL (usable keys)", verdict)
        self.assertIn("the report did not pass", verdict)
        self.assertNotIn("the two key rows fail", verdict)

    def test_a_live_grant_after_the_boot_rewrite_is_open_and_passes_five(self) -> None:
        """The boot rewrite deletes the line; the state keeps the grant live.

        `odile-release-setup.service` rewrites authorized_keys at every boot and
        the grant's timer restores its line within 30 s, so the report the boot
        oneshot writes often sees a live grant with no line. Five rows PASS and
        the grant row says the line is missing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(
                Path(tmp),
                extra_files={"var/lib/odile/support-grant.json":
                             json.dumps(self.grant_state())},
            )
            result = self.report(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for tag in ("usable keys", "boot key", "seal", "sign-in hash", "operator key"):
            self.assertRegex(result.stdout, r"%s\s+PASS" % tag)
        self.assertRegex(result.stdout, r"support grant\s+OPEN")
        self.assertIn("line missing", result.stdout)
        self.assertRegex(result.stdout.splitlines()[-1], r"^  OPEN \u2014 ")

    def test_an_expired_or_revoked_grant_state_is_not_live(self) -> None:
        """Only a grant still in its window is named; a tombstone is not."""
        cases = {
            "expired": {"expires_at": "2020-01-01T00:00:00Z"},
            "expired-tombstone": {"expires_at": "2020-01-01T00:00:00Z",
                                  "expired_at": "2026-11-13T00:07:48Z"},
            "revoked": {"revoked_at": "2026-11-13T00:08:08Z"},
        }
        for name, fields in cases.items():
            with self.subTest(state=name), tempfile.TemporaryDirectory() as tmp:
                root = self.bench(
                    Path(tmp),
                    keys=(self.THROWAWAY, self.grant_line()),
                    extra_files={"var/lib/odile/support-grant.json":
                                 json.dumps(self.grant_state(**fields))},
                )
                result = self.report(root)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertNotIn("support grant", result.stdout)
            self.assertRegex(result.stdout.splitlines()[-1],
                             r"^  FAIL \u2014 the access report did not pass")

    def test_a_grant_state_does_not_excuse_a_different_key(self) -> None:
        """The state names one fingerprint; a line carrying another still fails.

        A root shell can write the state file, so the row may only excuse the
        exact line `grant` would have written for that state.
        """
        line = self.grant_line(blob=self.OTHER_KEY)
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(
                Path(tmp),
                keys=(self.THROWAWAY, line),
                extra_files={"var/lib/odile/support-grant.json":
                             json.dumps(self.grant_state())},
            )
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"usable keys\s+FAIL")
        self.assertIn(self.key_fingerprint(self.OTHER_KEY), result.stdout)
        self.assertRegex(result.stdout, r"support grant\s+OPEN")
        self.assertIn("line missing", result.stdout)

    def test_a_matching_key_with_the_wrong_forced_command_is_not_the_grant(self) -> None:
        """Same key, same marker, but a command that is not the session wrapper."""
        line = self.grant_line(command="/bin/sh -c id")
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(
                Path(tmp),
                keys=(self.THROWAWAY, line),
                extra_files={"var/lib/odile/support-grant.json":
                             json.dumps(self.grant_state())},
            )
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"operator key\s+FAIL")
        self.assertIn("line missing", result.stdout)

    def test_the_boot_private_half_outside_a_dot_ssh_dir_fails(self) -> None:
        """A private key is not less dangerous because it is not under .ssh.

        The scan used to read only */.ssh, so `/root/boot-key` left "no
        private half of it on the machine" PASS.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "bench-throwaway",
                 "-f", str(tmp_path / "boot")],
                check=True,
            )
            public = (tmp_path / "boot.pub").read_text(encoding="utf-8").strip()
            private = (tmp_path / "boot").read_text(encoding="utf-8")
            line = self.command_line(key=public)
            root = self.bench(
                tmp_path,
                cmdline=line,
                running=line,
                keys=(public,),
                extra_files={"root/boot-key": private},
            )
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"boot key\s+FAIL")
        self.assertRegex(result.stdout, r"usable keys\s+FAIL")
        self.assertIn("/root/boot-key", result.stdout)

    def test_an_encrypted_boot_private_half_with_no_pub_sibling_fails(self) -> None:
        """`ssh-keygen -y -P ""` refuses a passphrase; `-l` still names the key.

        With no .pub sibling the old fallback found no public line and left
        the encrypted boot key's private half PASS.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "hunter2",
                 "-C", "bench-throwaway", "-f", str(tmp_path / "boot")],
                check=True,
            )
            public = (tmp_path / "boot.pub").read_text(encoding="utf-8").strip()
            private = (tmp_path / "boot").read_text(encoding="utf-8")
            line = self.command_line(key=public)
            root = self.bench(
                tmp_path,
                cmdline=line,
                running=line,
                keys=(public,),
                extra_files={"root/.ssh/id_ed25519": private},
            )
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"boot key\s+FAIL")
        self.assertIn("/root/.ssh/id_ed25519", result.stdout)

    def test_a_missing_authorized_keys_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(Path(tmp), write_authorized_keys=False)
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"usable keys\s+FAIL")
        self.assertRegex(result.stdout, r"operator key\s+FAIL")
        self.assertIn("no /root/.ssh/authorized_keys", result.stdout)

    def test_an_operator_key_is_named_and_fails(self) -> None:
        operator = (
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMdWyPF1Bcwd6sFiL4KiUtmYTq2FLe95TmfBQS0fEnu2"
            " odile-support@hq"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(Path(tmp), keys=(self.THROWAWAY, operator))
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"usable keys\s+FAIL")
        self.assertRegex(result.stdout, r"operator key\s+FAIL")
        self.assertIn("odile-support@hq", result.stdout)

    def test_a_policy_other_than_0x30000_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(Path(tmp), policy=0x30001)
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"seal\s+FAIL")
        self.assertIn("policy 0x30001", result.stdout)

    def test_a_missing_seal_id_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            line = self.command_line(seal_id=None)
            root = self.bench(Path(tmp), cmdline=line, running=line)
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"seal\s+FAIL")
        self.assertIn("seal_id missing", result.stdout)

    def test_a_measurement_that_does_not_match_expect_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(Path(tmp))
            result = self.report(root, "--expect", "00" * 48)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"seal\s+FAIL")

    def test_a_missing_signin_hash_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(Path(tmp), hash_line=None)
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"sign-in hash\s+FAIL")
        self.assertIn("no scrypt hash", result.stdout)

    def test_a_plaintext_password_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(
                Path(tmp),
                extra_env=("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=hunter2",),
            )
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"sign-in hash\s+FAIL")
        self.assertIn("plaintext password", result.stdout)

    def test_a_missing_attestation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(Path(tmp), attestation=False)
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"boot key\s+FAIL")
        self.assertRegex(result.stdout, r"seal\s+FAIL")
        self.assertIn("no report", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_the_running_command_line_must_carry_the_measured_key(self) -> None:
        other = (
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMdWyPF1Bcwd6sFiL4KiUtmYTq2FLe95TmfBQS0fEnu2"
            " someone-else"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(Path(tmp), running=self.command_line(key=other))
            result = self.report(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"boot key\s+FAIL")
        self.assertIn("running command line", result.stdout)

    def test_access_mode_needs_no_host_and_no_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(Path(tmp))
            result = self.report(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("--host", result.stderr)
        self.assertNotIn("--release", result.stderr)

    def test_the_page_quotes_the_bench_report_verbatim(self) -> None:
        """The demo's fallback block is the bench report, not a paraphrase.

        With no report file the served page carries the 10 November block;
        compared as one block, in order: a substring check would also pass a
        reordered or duplicated quote.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = self.bench(Path(tmp))
            result = self.report(root, "--expect", measurement())
            missing = Path(tmp) / "no-report.txt"
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        app = load_app()
        saved = app.ACCESS_REPORT_PATH
        app.ACCESS_REPORT_PATH = str(missing)
        try:
            page = app.render_verify("demo")
        finally:
            app.ACCESS_REPORT_PATH = saved
        blocks = re.findall(r'<pre class="report-pre"><code>(.*?)</code></pre>', page, re.S)
        self.assertEqual(len(blocks), 1, "the served demo page carries one report block")
        quoted = html.unescape(blocks[0])
        report = result.stdout.rstrip("\n")
        self.assertEqual(quoted.splitlines(), report.splitlines(),
                         "the page does not quote the report line for line")
        self.assertEqual(quoted, report,
                         "the page does not quote the report byte for byte")

    def test_the_page_scopes_the_report_to_a_test_machine(self) -> None:
        """The block is a test machine's report, not the demo machine's state.

        The page's lede describes the demo machine ("Odile holds this key"),
        so a bare "read from the machine" tag would make a reader take the
        five access lines as that machine's. The heading names the machine;
        the caption keeps the method and the date.
        """
        for name in ("verify.html", "verify-public.html"):
            with self.subTest(page=name):
                page = (ROOT / "web" / name).read_text(encoding="utf-8")
                self.assertIn('<h2 id="access">The access report, from a test machine</h2>', page)
                self.assertIn('<p class="caption">Read from the machine, 10 November 2026</p>',
                              page)
                self.assertNotIn("Bench report", page)
                self.assertNotIn("odl458", page,
                                 "the page still carries the internal card id")

    def test_the_page_uses_the_sheet_shell_scale(self) -> None:
        """The shell's two roles, not the /faq scale this page inherited.

        The wordmark floor is 32 px and the section title is the sheet's h2
        row; the 4 px pass moved the blocks around them. These are the exact
        declarations the brand review measured on the rendered page.
        """
        for name in ("verify.html", "verify-public.html"):
            with self.subTest(page=name):
                page = (ROOT / "web" / name).read_text(encoding="utf-8")
                style = page.split("<style>", 1)[1].split("</style>", 1)[0]
                self.assertIn("font-size:2rem;font-weight:500;letter-spacing:-.03em", style)
                self.assertIn(
                    "h2{margin:0 0 16px;font-size:clamp(1.9rem,3vw,2.6rem);"
                    "line-height:1.04;letter-spacing:-.02em;font-weight:500}", style)
                self.assertIn("section{margin:48px 0 0;padding-top:24px", style)
                self.assertIn("section{margin-top:32px;padding-top:24px}", style)
                self.assertIn(".gaps li{display:grid;grid-template-columns:2.2rem minmax(0,1fr);"
                              "gap:0 12px;margin:0 0 24px", style)

    def test_the_terminal_selection_is_a_visible_pair(self) -> None:
        """Selecting the fingerprint in the terminal must show a highlight.

        The page's global selection is paper on ink; on the terminal's near-
        black panel that paints ink on ink (1.05:1) and the highlight vanishes
        while a reader copies the fingerprint or the measurement.
        """
        for name in ("verify.html", "verify-public.html"):
            with self.subTest(page=name):
                page = (ROOT / "web" / name).read_text(encoding="utf-8")
                style = page.split("<style>", 1)[1].split("</style>", 1)[0]
                rule = re.search(
                    r"pre code::selection\{background:var\(--([a-z-]+)\);color:var\(--([a-z-]+)\)\}",
                    style)
                self.assertIsNotNone(rule, "no terminal ::selection pair in the page")
                if rule is None:  # pragma: no cover - the assertion above failed
                    continue

                def colour(token: str) -> str:
                    found = re.search(r"--%s:(#[0-9a-f]{6})" % re.escape(token), style)
                    self.assertIsNotNone(found, "no --%s token" % token)
                    return found.group(1)

                def luminance(value: str) -> float:
                    channels = []
                    for index in (1, 3, 5):
                        channel = int(value[index:index + 2], 16) / 255
                        channels.append(channel / 12.92 if channel <= 0.04045
                                        else ((channel + 0.055) / 1.055) ** 2.4)
                    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

                background, foreground = rule.groups()
                first, second = luminance(colour(background)), luminance(colour(foreground))
                ratio = (max(first, second) + 0.05) / (min(first, second) + 0.05)
                self.assertGreaterEqual(ratio, 7.0,
                                        "the terminal selection pair is only %.2f:1" % ratio)

    def test_both_pages_read_in_the_text_face_and_keep_the_display_roles(self) -> None:
        """The tenant page and the public page carry the same type roles.

        ODL-503 re-cut the prose onto the platform text serif, but the tenant
        /verify ships byte-embedded in the signed installer, so it was left on
        Cormorant at 18 px / 1.55 with a 14.72 px note. Both pages now name
        the display face twice only (the @font-face and the --serif token) and
        set body prose at 18 px / 1.6 and notes at 16 px / 1.6, so the page a
        tenant reads is the page the brand review measured.
        """
        for name in ("verify.html", "verify-public.html"):
            with self.subTest(page=name):
                text = (ROOT / "web" / name).read_text(encoding="utf-8")
                self.assertIn(':root{--serif:"Cormorant Garamond","Iowan Old Style",'
                              '"Palatino Linotype",Palatino,Georgia,serif;'
                              '--text:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,'
                              '"DejaVu Serif",serif;', text)
                self.assertIn("font:400 18px/1.6 var(--text);", text)
                self.assertIn(".lede{font-size:18px;line-height:1.6;color:var(--soft)}", text)
                self.assertIn(".note{margin:0 0 12px;font-size:16px;line-height:1.6;", text)
                self.assertIn("h1,h2,h3,.brand,.mark,.wordmark,.focus{font-family:var(--serif)}",
                              text)
                self.assertNotIn("font:400 18px/1.55", text,
                                 "the prose is back on the display face at display leading")
                self.assertNotIn("font-size:.92rem;line-height:1.5", text,
                                 "the note is back at the pre-re-cut size")
                # Cormorant is named by the @font-face and the --serif token,
                # and by no prose selector.
                self.assertEqual(text.count('"Cormorant Garamond"'), 2,
                                 "the display face is set on a prose selector")
                self.assertEqual(text.count("font:600 10px/1 var(--text);"), 1,
                                 "the small caps label is not on the text face")
                self.assertIn("max-width:66ch", text, "the reading measure moved")


class OwnReportTests(unittest.TestCase):
    """ODL-473: /verify renders the machine's own report file.

    The report the boot oneshot writes is the only thing the page may show as
    this machine's state. The 10 November bench block stays as the demo's
    fallback when no file exists; a tenant with no file says so instead.
    """

    AT = "2026-11-11T10:00:00Z"
    PASS_ROWS = (
        "  usable keys    PASS  0 usable user keys; every authorized_keys line is the measured boot key",
        "  boot key       PASS  ssh-ed25519 SHA256:LLwDfYyE3VMc9T9o+XvydfNzrXxyzaQ2lysSxCSSEWA; public key only",
        "  seal           PASS  policy 0x30000; seal_id strict-bench-v1; measurement " + "a" * 96,
        "  sign-in hash   PASS  scrypt hash present in /home/odile/.hermes/.env; no plaintext password",
        "  operator key   PASS  none (authorized_keys: 1 line(s), the measured throwaway)",
    )

    PASS_SUMMARY = ("PASS \u2014 five checks: zero usable user keys, the throwaway "
                    "boot key, the seal, the sign-in hash, no operator key.")
    FAIL_SUMMARY = "FAIL \u2014 the access report did not pass"

    def write_report(self, tmp: Path, *, rows=PASS_ROWS, summary: str = PASS_SUMMARY,
                     at: str | None = AT) -> Path:
        """Write exactly what `--mode access --out` writes: provenance, report, summary."""
        path = tmp / "access-report.txt"
        head = ("read from your machine, %s\n" % at) if at else ""
        path.write_text(
            head + "verify_and_unlock \u2014 access report\n\n"
            + "\n".join(rows) + "\n\n  %s\n" % summary
        )
        return path

    def app_page(self, role: str, report: Path | None) -> str:
        app = load_app()
        saved = app.ACCESS_REPORT_PATH
        app.ACCESS_REPORT_PATH = str(report) if report is not None else "/nonexistent/odl473"
        try:
            return app.render_verify(role)
        finally:
            app.ACCESS_REPORT_PATH = saved

    def test_the_script_writes_the_report_file_0644_with_the_tag(self) -> None:
        case = AccessReportTests("test_the_bench_report_passes_and_names_every_line")
        with tempfile.TemporaryDirectory() as tmp:
            root = case.bench(Path(tmp))
            out = Path(tmp) / "access-report.txt"
            result = subprocess.run(
                [str(ACCESS), "--mode", "access", "--root", str(root), "--user", "odile",
                 "--expect", measurement(), "--out", str(out)],
                capture_output=True, text=True, timeout=120, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o644)
            text = out.read_text(encoding="utf-8")
            # The file the script writes is what /verify renders as the
            # machine's own state: same block, same lines, its timestamp.
            page = self.app_page("tenant", out)
        self.assertTrue(text.startswith("read from your machine, "), text[:80])
        self.assertRegex(text.splitlines()[0], r"^read from your machine, \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        # stdout stays the report itself: the file minus its provenance line.
        self.assertEqual(text.split("\n", 1)[1], result.stdout)
        for tag in ("usable keys", "boot key", "seal", "sign-in hash", "operator key"):
            self.assertRegex(text, r"%s +PASS" % tag)
        block = re.search(r'<pre class="report-pre"><code>(.*?)</code></pre>', page, re.S)
        self.assertIsNotNone(block)
        self.assertEqual(html.unescape(block.group(1)).splitlines(),
                         text.splitlines()[1:],
                         "the page does not quote the file the script wrote")
        self.assertIn('<time datetime="%s">' % text.splitlines()[0].rsplit(", ", 1)[1], page)

    def test_a_failing_run_still_writes_the_fail_lines(self) -> None:
        case = AccessReportTests("test_the_bench_report_passes_and_names_every_line")
        with tempfile.TemporaryDirectory() as tmp:
            root = case.bench(Path(tmp), hash_line=None)
            out = Path(tmp) / "access-report.txt"
            result = subprocess.run(
                [str(ACCESS), "--mode", "access", "--root", str(root), "--user", "odile",
                 "--out", str(out)],
                capture_output=True, text=True, timeout=120, cwd=ROOT)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            text = out.read_text(encoding="utf-8")
        self.assertRegex(text, r"sign-in hash +FAIL")
        self.assertIn("FAIL \u2014 the access report did not pass", text)

    def test_the_page_renders_the_report_file_with_the_tag_and_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_report(Path(tmp))
            page = self.app_page("tenant", path)
        self.assertIn("read from your machine", page)
        self.assertIn(self.AT, page)
        self.assertEqual(page.count('<pre class="report-pre">'), 1)
        block = re.search(r'<pre class="report-pre"><code>(.*?)</code></pre>', page, re.S)
        self.assertIsNotNone(block)
        quoted = html.unescape(block.group(1))
        self.assertEqual(quoted.splitlines()[2].strip(), self.PASS_ROWS[0].strip())
        self.assertIn("operator key   PASS  none", quoted)
        self.assertNotIn("from a test machine", page, "the bench block is not this machine")
        self.assertNotIn("{{", page)

    def test_a_fail_report_reaches_the_page(self) -> None:
        rows = list(self.PASS_ROWS)
        rows[0] = "  usable keys    FAIL  1 authorized key(s) other than the measured throwaway"
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_report(Path(tmp), rows=tuple(rows),
                                     summary=self.FAIL_SUMMARY)
            page = self.app_page("tenant", path)
        self.assertIn("usable keys    FAIL", page)
        self.assertIn("FAIL \u2014 the access report did not pass", page)
        self.assertIn("read from your machine", page)

    # -- a live grant on the page (t_e23f3288) ---------------------------
    GRANT_ROW = ("  " + "%-14s" % "support grant" + " OPEN  "
                 "grant 7bc06cc48244; expires 2099-01-01T00:00:00Z; "
                 "key SHA256:clN4wFkPkmjrpKbjkg6Z7N7zSB3L7w/AcHV5ysbfWAQ; line present")
    OPEN_SUMMARY = ("OPEN \u2014 a support grant is live until 2099-01-01T00:00:00Z "
                    "(grant 7bc06cc48244): the two key rows fail because that "
                    "granted key is in /root/.ssh/authorized_keys.")

    def grant_rows(self) -> tuple[str, ...]:
        rows = list(self.PASS_ROWS)
        rows[0] = ("  usable keys    FAIL  1 authorized key(s) other than the "
                   "measured throwaway in /root/.ssh/authorized_keys: "
                   "SHA256:clN4wFkPkmjrpKbjkg6Z7N7zSB3L7w/AcHV5ysbfWAQ "
                   "\u2014 the support grant 7bc06cc48244 (expires "
                   "2099-01-01T00:00:00Z)")
        rows[4] = ("  operator key   FAIL  authorized_keys carries "
                   "command=\"/usr/local/sbin/odile-support session\" "
                   "odile-support-grant-7bc06cc48244 \u2014 the support grant "
                   "7bc06cc48244, expires 2099-01-01T00:00:00Z")
        return tuple(rows) + (self.GRANT_ROW,)

    def test_a_live_grant_report_reaches_the_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_report(Path(tmp), rows=self.grant_rows(),
                                     summary=self.OPEN_SUMMARY)
            page = self.app_page("tenant", path)
        block = re.search(r'<pre class="report-pre"><code>(.*?)</code></pre>', page, re.S)
        self.assertIsNotNone(block, "the grant report is not shown as this machine's")
        quoted = html.unescape(block.group(1))
        self.assertRegex(quoted, r"support grant\s+OPEN")
        self.assertIn("7bc06cc48244", quoted)
        self.assertIn("2099-01-01T00:00:00Z", quoted)
        self.assertIn("OPEN \u2014 a support grant is live", quoted)
        self.assertNotIn("not in the shape the reporter writes", page)

    def test_a_grant_row_without_an_open_verdict_is_not_the_reporter_shape(self) -> None:
        """The row and the verdict move together; half of one is a bad file."""
        for name, rows, summary in (
            ("row-with-fail", self.grant_rows(), self.FAIL_SUMMARY),
            ("open-without-row", self.PASS_ROWS, self.OPEN_SUMMARY),
        ):
            with self.subTest(shape=name), tempfile.TemporaryDirectory() as tmp:
                path = self.write_report(Path(tmp), rows=tuple(rows), summary=summary)
                page = self.app_page("tenant", path)
            self.assertNotIn("support grant", page)
            self.assertIn("not in the shape the reporter writes", page)

    def test_the_three_parsers_agree_on_a_grant_report(self) -> None:
        """app.py, first_visit.py and the installer's embedded copy read alike.

        The installer ships `first_visit.py` as an embedded string, so the copy
        under test is the one a tenant gets, not a checkout file beside it.
        """
        sys.path.insert(0, str(ROOT / "installer"))
        import first_visit  # noqa: E402
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_report(Path(tmp), rows=self.grant_rows(),
                                     summary=self.OPEN_SUMMARY)
            shipped = Path(tmp) / "shipped_first_visit.py"
            shipped.write_text(installer.FIRST_VISIT_SOURCE, encoding="utf-8")
            spec = importlib.util.spec_from_file_location("first_visit_shipped", shipped)
            embedded = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = embedded
            spec.loader.exec_module(embedded)
            app = load_app()
            saved = app.ACCESS_REPORT_PATH
            app.ACCESS_REPORT_PATH = str(path)
            try:
                from_app = app.access_report()
            finally:
                app.ACCESS_REPORT_PATH = saved
            answers = [from_app, first_visit.access_report(path),
                       embedded.access_report(path)]
        for answer in answers[1:]:
            self.assertEqual(answer, answers[0],
                             "a parser disagrees with app.py on the grant report")
        self.assertEqual(answers[0]["state"], "ok")
        self.assertRegex(answers[0]["text"], r"support grant\s+OPEN")

    def test_without_a_file_the_demo_keeps_the_bench_and_the_tenant_says_so(self) -> None:
        demo = self.app_page("demo", None)
        tenant = self.app_page("tenant", None)
        self.assertIn("The access report, from a test machine", demo)
        self.assertNotIn("from your machine</h2>", demo)
        self.assertNotIn("from a test machine", tenant)
        self.assertIn("Report unavailable.", tenant)
        self.assertIn("has not written its access report", tenant)

    def test_a_malformed_report_is_not_shown_as_the_bench(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "access-report.txt"
            path.write_text("read from your machine, not-a-time\ngarbage\n")
            page = self.app_page("demo", path)
        self.assertNotIn("from a test machine", page)
        self.assertIn("not in the shape the reporter writes", page)

    def test_a_hostile_report_file_stays_escaped(self) -> None:
        rows = list(self.PASS_ROWS)
        rows[3] = ("  sign-in hash   PASS  <script>alert(1)</script> in "
                   "/home/odile/.hermes/.env")
        with tempfile.TemporaryDirectory() as tmp:
            page = self.app_page("tenant", self.write_report(Path(tmp), rows=tuple(rows)))
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_the_first_visit_service_no_longer_serves_verify(self) -> None:
        """One page, not two: /verify belongs to odile-verify (t_4fcd465f)."""
        sys.path.insert(0, str(ROOT / "installer"))
        import first_visit  # noqa: E402
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state = tmp_path / "state.json"
            state.write_text(json.dumps({
                "version": 1, "token_sha256": "c" * 64, "created_at": self.AT,
                "used_at": None, "signin_set_at": None, "recovery_hash": None,
                "recovery_issued_at": None, "recovery_used_at": None}), encoding="utf-8")
            env = tmp_path / ".env"
            env.write_text("HERMES_DASHBOARD_BASIC_AUTH_USERNAME=odile\n", encoding="utf-8")
            service = first_visit.FirstVisit(
                state=state, env=env, page=ROOT / "web" / "first-visit.html",
                recovery=tmp_path / "recovery.txt", reload_command=[],
                bind="127.0.0.1", port=0, caddy_stores=())
            server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         first_visit.build_handler(service))
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = "http://127.0.0.1:%d" % port
            try:
                with urllib.request.urlopen(base + "/health", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(base + "/verify", timeout=5)
                self.assertEqual(caught.exception.code, 404,
                                 "the first-visit service still answers /verify")
            finally:
                server.shutdown()
                server.server_close()


    def test_the_image_creates_the_report_directory_before_it_starts_the_units(self) -> None:
        """ReadWritePaths on a missing directory is systemd 226/NAMESPACE.

        Measured on the first booted image: `/var/lib/odile` did not exist
        until the first boot wrote its state, and both report units failed with
        `Failed to set up mount namespacing: /var/lib/odile: No such file or
        directory`. The image must create the directory before it starts them.
        """
        text = (ROOT / "deploy" / "image" / "install-stack.sh").read_text(encoding="utf-8")
        create = text.index("install -d -m 0755 /var/lib/odile")
        start = text.index("systemctl start odile-bench-attest.service")
        self.assertLess(create, start,
                        "the report directory is created after the units start")

    def test_the_first_visit_asks_for_a_restart_not_a_start(self) -> None:
        """The unit is RemainAfterExit=yes; a `start` after boot is a no-op.

        The sign-in line would keep naming the placeholder .env forever, so the
        POST's command must be a restart, and it must actually run.
        """
        sys.path.insert(0, str(ROOT / "installer"))
        import first_visit  # noqa: E402
        self.assertEqual(first_visit.DEFAULT_REPORT_COMMAND.split()[:2],
                         ["systemctl", "restart"],
                         "a start would not re-run the oneshot")
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "ran"
            service = first_visit.FirstVisit(
                state=Path(tmp) / "state.json", env=Path(tmp) / ".env",
                page=ROOT / "web" / "first-visit.html",
                recovery=Path(tmp) / "recovery.txt", reload_command=[],
                bind="127.0.0.1", port=0,
                report_command=["/bin/sh", "-c", "printf ran > '%s'" % marker])
            service.refresh_report()
            self.assertTrue(marker.is_file(), "the refresh command did not run")
            self.assertEqual(marker.read_text(), "ran")
        unit = (ROOT / "deploy" / "image" / "odile-first-visit.service").read_text(encoding="utf-8")
        self.assertIn("--report-command 'systemctl restart --no-block "
                      "odile-access-report.service'", unit,
                      "the image's unit does not ask for a restart")


class RefreshRunbookTests(unittest.TestCase):
    """The refresh recipe hands AMD's KDS the chip id, not a digest of it.

    ``docs/runbooks/verify.md`` is the documented way to read the KDS path
    segment for a report, and its step 1 once printed ``sha256(chip_id)`` as the
    ``hwid`` a KDS URL takes, which answers ``400 Bad Request``. Nothing else
    exercises that snippet — the verifier reads the fixture, not the prose — so
    this test runs the snippet itself against the fixture report.
    """

    def step_one(self) -> str:
        text = VERIFY_MD.read_text(encoding="utf-8")
        _, _, tail = text.partition("## Refreshing the AMD certificates")
        self.assertTrue(tail, "the AMD refresh section is gone from the runbook")
        shell = re.search(r"```sh\n(.*?)```", tail, re.S)
        if shell is None:
            self.fail("the refresh section carries no shell block")
        parts = shell.group(1).split("<<'PY'\n", 1)
        self.assertEqual(len(parts), 2, "step 1 has no python heredoc")
        code = parts[1].split("\nPY\n", 1)[0]
        self.assertIn('"/tmp/attestation.json"', code)
        return code

    def test_the_refresh_recipe_prints_the_chip_id_the_kds_takes(self) -> None:
        code = self.step_one()
        report = base64.b64decode(json.loads(FIXTURE_BYTES)["report_b64"])
        chip_id = report[0x1A0:0x1E0].hex()
        digest = hashlib.sha256(report[0x1A0:0x1E0]).hexdigest()
        self.assertEqual(len(chip_id), 128)
        with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as handle:
            handle.write(FIXTURE_BYTES)
            path = handle.name
        try:
            source = code.replace('"/tmp/attestation.json"', repr(path))
            result = subprocess.run([sys.executable, "-"], input=source,
                                    capture_output=True, text=True)
        finally:
            Path(path).unlink()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(chip_id, result.stdout)
        self.assertNotIn(digest, result.stdout,
                         "the KDS takes the chip id, not a digest of it")


class KDSFixture:
    """A loopback AMD KDS: one object, one status, every request path kept."""

    def __init__(self, payload: bytes = TENANT_VCEK_BYTES, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.seen: list[str] = []
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "KDSFixture":
        seen, payload, status = self.seen, self.payload, self.status

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib name
                seen.append(self.path)
                self.send_response(status)
                if status == 200:
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if status == 200:
                    self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        assert self.server is not None and self.thread is not None
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def base(self) -> str:
        assert self.server is not None
        return "http://127.0.0.1:%d" % self.server.server_address[1]


class TenantVcekTests(unittest.TestCase):
    """ODL-319 follow-up: a tenant's report is checked with the tenant's VCEK.

    The chain in ``--amd-dir`` is the synthetic fixture root. These tests serve
    the tenant's own VCEK from a loopback KDS and prove the verifier fetches it
    by the report's raw chip id and reported TCB, checks it against the pinned
    ARK, and fails closed when the KDS or the report is wrong. No test reaches
    AMD.
    """

    def run_against_kds(self, *args: str, document: Path = TENANT_FIXTURE,
                        payload: bytes = TENANT_VCEK_BYTES, status: int = 200,
                        expect: str | None = TENANT_MEASUREMENT
                        ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with KDSFixture(payload, status) as kds:
            argv = ["--report", str(document)]
            if expect is not None:
                argv += ["--expect", expect]
            argv += ["--kds-base", kds.base, *args]
            result = run(*argv)
        return result, kds.seen

    def test_a_tenant_report_fetches_its_own_vcek_and_passes(self) -> None:
        result, seen = self.run_against_kds()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"vcek\s+PASS")
        for label in ("chain", "cpu binding", "signature", "policy", "tcb",
                      "report", "served", "release", "expected"):
            self.assertRegex(result.stdout, r"%s\s+PASS" % re.escape(label))
        self.assertIn(TENANT_MEASUREMENT, result.stdout)
        self.assertIn("policy 0x30000", result.stdout)
        self.assertIn("2026.11.2 is pinned", result.stdout)
        self.assertIn("PASS — the CPU signed this report", result.stdout)
        self.assertEqual(seen, [TENANT_KDS_PATH])

    def test_the_two_vcpu_plan_is_measured_by_the_pin(self) -> None:
        """The demo is a 2-vCPU Blackbox Core; the pin must carry that count.

        `vcpu_counts` means "plan sizes this release was measured at", not
        "plan sizes we sell": the demo's UKI (`47be81ca…`) measures
        `83bd3fce…` at 2 vCPU, so the release row has to accept 2. The
        document is the tenant fixture with only the unsigned `vcpus` field
        changed, which is the field the row reads.
        """
        data = dict(TENANT_DOC)
        data["vcpus"] = 2
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8", dir=TENANT_FIXTURE.parent)
        with handle:
            json.dump(data, handle)
        try:
            result, _ = self.run_against_kds(document=Path(handle.name))
        finally:
            Path(handle.name).unlink()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"release\s+PASS")
        self.assertIn("2026.11.2 is pinned", result.stdout)

    def test_the_kds_path_is_the_raw_chip_id_and_reported_tcb(self) -> None:
        _, seen = self.run_against_kds()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], TENANT_KDS_PATH)
        chip = seen[0].split("/vcek/v1/Milan/", 1)[1].split("?", 1)[0]
        self.assertEqual(len(chip), 128)
        digest = hashlib.sha256(bytes.fromhex(chip)).hexdigest()
        self.assertEqual(len(digest), 64)
        self.assertNotIn(digest, seen[0],
                         "the KDS hwid segment is the chip id, not a digest")
        self.assertIn("blSPL=%d" % TENANT_TCB[0], seen[0])
        self.assertIn("snpSPL=%d" % TENANT_TCB[6], seen[0])

    def test_a_tampered_tenant_report_fails_closed(self) -> None:
        raw = bytearray(TENANT_REPORT)
        raw[0x90] ^= 0x01
        data = dict(TENANT_DOC)
        data["report_b64"] = base64.b64encode(bytes(raw)).decode("ascii")
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8", dir=TENANT_FIXTURE.parent)
        with handle:
            json.dump(data, handle)
        try:
            result, seen = self.run_against_kds(document=Path(handle.name))
        finally:
            Path(handle.name).unlink()
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stdout, r"vcek\s+PASS")
        self.assertRegex(result.stdout, r"signature\s+FAIL")
        self.assertIn("FAIL — the report did not verify", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual(seen, [TENANT_KDS_PATH])

    def test_a_kds_404_fails_closed(self) -> None:
        result, seen = self.run_against_kds(status=404)
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stdout, r"vcek\s+FAIL")
        self.assertIn("HTTP 404", result.stdout)
        self.assertRegex(result.stdout, r"cpu binding\s+FAIL")
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual(seen, [TENANT_KDS_PATH])

    def test_a_non_certificate_kds_body_fails_closed(self) -> None:
        result, _ = self.run_against_kds(payload=b"not a certificate")
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stdout, r"vcek\s+FAIL")
        self.assertIn("did not return a certificate", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_the_demo_fixture_never_touches_the_kds(self) -> None:
        with KDSFixture() as kds:
            result = run("--report", str(DEMO_FIXTURE), "--expect", DEMO_DOC["measurement"],
                         "--pin", str(PIN), "--kds-base", kds.base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("kds", result.stdout)
        self.assertEqual(kds.seen, [])

    def test_the_url_mode_page_command_fetches_two_urls(self) -> None:
        """The command `web/verify.html` prints fetches two URLs, not one.

        URL mode with the shipped defaults: no `--report`, no `--no-kds`. The
        only overrides are the loopback KDS and the synthetic trust root the
        fixtures are signed with (a served document never names its own). The
        chain directory carries the synthetic ASK and ARK and no VCEK, so the
        report's own VCEK can only come from the KDS. The count is the sentence
        the page now states.
        """
        seen: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib name
                seen.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(TENANT_BYTES)))
                self.end_headers()
                self.wfile.write(TENANT_BYTES)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d" % server.server_address[1]
            with tempfile.TemporaryDirectory(prefix="odile-amd-") as amd_dir:
                for name in ("ark.pem", "ask.pem"):
                    (Path(amd_dir) / name).write_bytes(
                        (SYNTHETIC_AMD_DIR / name).read_bytes())
                with KDSFixture() as kds:
                    result = run(url, "--expect", TENANT_MEASUREMENT,
                                 "--amd-dir", amd_dir,
                                 "--ark-sha256", SYNTHETIC_ARK,
                                 "--kds-base", kds.base)
                    kds_seen = list(kds.seen)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"vcek\s+PASS")
        self.assertEqual(seen, ["/attestation"])
        self.assertEqual(kds_seen, [TENANT_KDS_PATH])

    def test_no_kds_refuses_the_fetch(self) -> None:
        result, seen = self.run_against_kds("--no-kds")
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stdout, r"vcek\s+FAIL")
        self.assertIn("--no-kds", result.stdout)
        self.assertRegex(result.stdout, r"signature\s+FAIL")
        self.assertEqual(seen, [])

    def test_a_bad_kds_base_is_a_usage_error(self) -> None:
        result = run("--report", str(TENANT_FIXTURE), "--expect", TENANT_MEASUREMENT,
                     "--kds-base", "ftp://example.com")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--kds-base", result.stderr)

    def test_the_tenant_2026_11_2_entry_matches_the_release_file(self) -> None:
        pin = json.loads(PIN.read_text(encoding="utf-8"))
        entry = pin["releases"]["2026.11.2"]
        release_bytes = RELEASE_2026_11_2.read_bytes()
        release = json.loads(release_bytes)
        self.assertEqual(hashlib.sha256(release_bytes).hexdigest(),
                         entry["release_json_sha256"])
        self.assertEqual(entry["artifacts"], release["artifacts"])
        self.assertEqual(entry["measurement_parameters"],
                         release["measurement_parameters"])
        self.assertEqual(entry["release_url"],
                         "https://servury.com/downloads/blackbox/2026.11.2/")
        self.assertIn(4, entry["vcpu_counts"])
        self.assertIn(6, entry["vcpu_counts"])
        # The demo moved to a 2-vCPU Blackbox Core; its UKI (`47be81ca…`)
        # measures `83bd3fce…` at 2 vCPU, so the entry names that plan too.
        self.assertIn(2, entry["vcpu_counts"])
        # Rotation is additive: the old entry a running tenant boots stays.
        self.assertIn("2026.11.1", pin["releases"])

    def test_the_tcb_floor_is_on_committed_tcb(self) -> None:
        module = load_snp_report()
        raw = bytearray(TENANT_REPORT)
        # The hypervisor reports snp 23; the committed value is 28. The floor
        # must pass on the committed value, or a lowered reported TCB hides it.
        module.check_tcb(module.Report(bytes(raw)))
        raw[0x1E0 + 6] = 26
        with self.assertRaises(module.ReportError) as raised:
            module.check_tcb(module.Report(bytes(raw)))
        self.assertIn("below the minimum", str(raised.exception))

    def test_the_record_carries_the_tenant_release_and_measurement(self) -> None:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.close()
        try:
            result, _ = self.run_against_kds("--record", handle.name)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            record = json.loads(Path(handle.name).read_text(encoding="utf-8"))
        finally:
            Path(handle.name).unlink()
        self.assertEqual(record["release"], "2026.11.2")
        self.assertEqual(record["measurement"], TENANT_MEASUREMENT)
        cmdline = TENANT_DOC["cmdline"].strip()
        suffix = " initrd=initrd"
        if cmdline.endswith(suffix):
            cmdline = cmdline[:-len(suffix)]
        self.assertEqual(record["cmdline_sha256"],
                         hashlib.sha256(cmdline.encode("utf-8")).hexdigest())


class SyntheticFixtureTests(unittest.TestCase):
    """The generator is the source of the committed fixtures; it must verify."""

    def test_the_generator_builds_a_verifiable_fixture(self) -> None:
        with tempfile.TemporaryDirectory(prefix="odl-synthetic-") as tmp:
            result = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "make_synthetic_report.py"),
                 "--out-dir", tmp],
                capture_output=True, text=True, timeout=120, cwd=ROOT,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            document = json.loads(
                (Path(tmp) / "synthetic-report.json").read_text(encoding="utf-8"))
            module = load_snp_report()
            report = module.Report(base64.b64decode(document["report_b64"]))
            trust = document["trust"]
            checks = module.run_checks(
                report, str(Path(tmp) / trust["amd_dir"]), trust["ark_sha256"])
            self.assertTrue(all(check.ok for check in checks),
                            [check.detail for check in checks if not check.ok])
            self.assertTrue(report.chip_id.startswith(b"ODILE-SYNTHETIC-"))
            self.assertEqual(report.measurement_hex, document["measurement"])
            self.assertEqual(report.version, 5)
            self.assertEqual(len(report.raw), 1184)


def sha256sum_fence(path: Path) -> str:
    """The first ```sh fence in `path` that runs `sha256sum -c`, fences included.

    The deck's §4.4 and the decided draft's §4.4 each carry one such block —
    the four commands a judge copies. Reading the block rather than the whole
    file is what makes a stale §4.4 line fail even though the deck names the
    current digest in its change-log row as well (measured on `t_650c8f4c`).
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for opening, line in enumerate(lines):
        if line.strip() != "```sh":
            continue
        for closing in range(opening + 1, len(lines)):
            if lines[closing].strip() != "```":
                continue
            block = "".join(lines[opening:closing + 1])
            if "sha256sum -c" in block:
                return block
            break
    raise AssertionError("%s carries no ```sh fence with sha256sum -c" % path)


class BundleTests(unittest.TestCase):
    """The published tarball is reproducible and its hash is the one we print."""

    def test_the_bundle_is_reproducible_across_the_clock(self) -> None:
        app = load_app()
        app._bundle = None
        with mock.patch("time.time", return_value=1_000_000_000.0):
            first = app.verifier_bundle()
        app._bundle = None
        with mock.patch("time.time", return_value=2_000_000_000.0):
            second = app.verifier_bundle()
        self.assertEqual(first, second)
        self.assertEqual(first[4:8], b"\x00\x00\x00\x00",
                         "the gzip header must not carry a build clock")

    def test_the_published_hash_is_the_bundle_in_this_tree(self) -> None:
        """Every home of the hash names the bundle this tree builds, block included.

        `docs/CLAIMS.md` §4.4 is the block a judge copies: while it was
        outside this set, a pin change (the 2-vCPU plan on `t_a37c001d`) moved
        this test's three files and left the deck naming the old tarball
        (`t_b0cc9015` fixed that by hand). Two shapes, and both are needed:
        the presence loop reads the pin, so it is the only one red on a tree
        whose pin differs from what the deck names; the fence comparison binds
        the §4.4 block itself, so a stale §4.4 line cannot hide behind the
        digest the deck's own change-log row carries (`t_650c8f4c` left that
        gap: the row made the presence loop green with §4.4 reverted).
        """
        app = load_app()
        app._bundle = None
        digest = hashlib.sha256(app.verifier_bundle()).hexdigest()
        for path in (VERIFY_MD, RECUT_MD, VERDICT_MD, CLAIMS_MD):
            self.assertIn(digest, path.read_text(encoding="utf-8"),
                          "%s does not name the bundle this tree builds" % path)
        # The block, not the file: `docs/CLAIMS.md` carries this digest twice
        # (§4.4 and the v3.10n row), so the loop above cannot see a stale
        # §4.4 line. §4.4 is the block a judge copies, so it must equal the
        # decided draft's byte for byte. This comparison reads no pin, which
        # is why it does not replace the loop.
        self.assertEqual(
            sha256sum_fence(CLAIMS_MD), sha256sum_fence(RECUT_MD),
            "docs/CLAIMS.md §4.4's sha256sum fence differs from "
            "docs/product/CLAIMS-RECUT-STRICT.md §4.4's",
        )


class VerifyPageNoteTests(unittest.TestCase):
    """The public /verify note's fetch count, fenced.

    The note is the page's one sentence about the verifier's network path. It
    said "one URL" while the shipped verifier also fetched the report's VCEK
    from AMD's KDS; after `t_3cd62ef9` it says two. Pin the narrow phrase and
    forbid the wide one, so a later edit cannot quietly widen it back.
    """

    def test_both_pages_state_two_urls_and_not_one(self) -> None:
        for name in ("verify.html", "verify-public.html"):
            with self.subTest(page=name):
                page = (ROOT / "web" / name).read_text(encoding="utf-8")
                self.assertIn(
                    'It fetches two URLs: <a href="/attestation">/attestation</a>, '
                    "and that report's own VCEK from AMD's KDS.", page)
                self.assertNotIn("fetches one URL", page)
                self.assertNotIn("exactly one URL", page)


class VerifyReadmeNoteTests(unittest.TestCase):
    """`verify/README.md` is a published surface; its network sentences are fenced.

    main added the file in `t_5ce34861` after this branch's base, so no gate had
    read it. It said the verifier fetches one URL and that the demo binds the
    vendored VCEK; `t_3cd62ef9` deletes the vendored VCEK, so every default run
    — the demo's included — fetches the report's own VCEK from AMD's KDS. Pin
    the true sentences and forbid the wide ones, so a later edit cannot widen
    them back.
    """

    README = ROOT / "verify" / "README.md"

    def text(self) -> str:
        # Whitespace-normalised: a re-wrap must not be able to break the fence.
        return " ".join(self.README.read_text(encoding="utf-8").split())

    def test_the_readme_states_the_default_runs_two_fetches(self) -> None:
        text = self.text()
        self.assertIn("it fetches two URLs on the default run", text)
        for stale in ("fetches one URL", "exactly one URL",
                      "demo binds the vendored VCEK", "vendored chain only",
                      "vendored AMD chain"):
            self.assertNotIn(stale, text)

    def test_the_readme_states_no_kds_fails_closed_and_the_tests_use_the_synthetic_chain(self) -> None:
        text = self.text()
        self.assertIn(
            "needs `--amd-dir` holding a chain that binds the report, and fails "
            "closed otherwise", text)
        self.assertIn("synthetic test-only chain with no network", text)


class FocusOrder(HTMLParser):
    """The document order of what a browser would tab to, and nothing else.

    ``<a href>``, ``<button>``, ``<input>`` that is not hidden, ``<select>``,
    ``<textarea>`` and any element with a non-negative ``tabindex``. A class
    name proves nothing about order, so the tests read the rendered page: the
    skip link's whole contract is that it comes first.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stops: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str,
                        attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        tabindex = (attributes.get("tabindex") or "").strip()
        kind = (attributes.get("type") or "text").strip().lower()
        focusable = (
            (tag == "a" and attributes.get("href") is not None)
            or tag in ("button", "select", "textarea")
            or (tag == "input" and kind != "hidden")
            or (tabindex.isdigit() and int(tabindex) >= 0)
        )
        if focusable:
            self.stops.append({
                "tag": tag,
                "class": attributes.get("class") or "",
                "id": attributes.get("id"),
                "href": attributes.get("href"),
            })


def focus_order(body: str) -> list[dict[str, str | None]]:
    """What the page's own markup makes tabbable, in document order."""
    parser = FocusOrder()
    parser.feed(body)
    parser.close()
    return parser.stops


def main_id(body: str) -> str | None:
    """The ``id`` on the page's ``<main>``, or None when it carries none."""
    element = re.search(r"<main\b([^>]*)>", body)
    if element is None:
        return None
    attribute = re.search(r'\bid="([^"]+)"', element.group(1))
    return attribute.group(1) if attribute else None


class VerifySkipLinkTests(unittest.TestCase):
    """BRAND.md's Keyboard row on HQ's /verify page.

    The page exposes four landmarks in the demo role and three on a tenant, so
    the rule applies: a skip link is the first focusable element and it names
    the page's own ``main``. HQ serves ``web/verify-public.html``; the review
    reads ``web/verify.html``; the machine's page is ``templates/verify.html``,
    rendered by ``odile-verify`` (asserted in ``VerifyPageTests``). One shell,
    three carriers, so all three must carry the same rule.
    """

    AT = "2026-11-11T10:00:00Z"
    REPORT = (
        "read from your machine, 2026-11-11T10:00:00Z\n"
        "verify_and_unlock \u2014 access report\n\n"
        "  usable keys    PASS  0 usable user keys; every authorized_keys line is the measured boot key\n"
        "  boot key       PASS  ssh-ed25519 SHA256:LLwDfYyE3VMc9T9o+XvydfNzrXxyzaQ2lysSxCSSEWA; public key only\n"
        "  seal           PASS  policy 0x30000; seal_id strict-bench-v1; measurement " + "a" * 96 + "\n"
        "  sign-in hash   PASS  scrypt hash present in /home/odile/.hermes/.env; no plaintext password\n"
        "  operator key   PASS  none (authorized_keys: 1 line(s), the measured throwaway)\n\n"
        "  PASS \u2014 five checks: zero usable user keys, the throwaway boot key, the seal, "
        "the sign-in hash, no operator key.\n"
    )

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="odile-verify-skip-")
        self.base = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def page(self, role: str, report: str | None) -> str:
        """app.py's render of the HQ page, with or without a report file."""
        app = load_app()
        saved = app.ACCESS_REPORT_PATH
        if report is None:
            app.ACCESS_REPORT_PATH = str(self.base / "no-report.txt")
        else:
            path = self.base / "access-report.txt"
            path.write_text(report, encoding="utf-8")
            app.ACCESS_REPORT_PATH = str(path)
        try:
            return app.render_verify(role)
        finally:
            app.ACCESS_REPORT_PATH = saved

    def states(self) -> dict[str, str]:
        """The demo's bench fallback and report, and a tenant with each."""
        return {
            "demo-bench": self.page("demo", None),
            "demo-machine": self.page("demo", self.REPORT),
            "tenant-machine": self.page("tenant", self.REPORT),
            "tenant-noaccess": self.page("tenant", None),
        }

    def test_the_first_tab_stop_in_every_state_is_a_skip_link_to_that_pages_main(self):
        for name, body in self.states().items():
            with self.subTest(state=name):
                stops = focus_order(body)
                self.assertTrue(stops, "the %s page has no focusable element" % name)
                first = stops[0]
                self.assertEqual(first["tag"], "a",
                                 "the first tab stop is <%s>, not the skip link"
                                 % first["tag"])
                self.assertIn("skip", (first["class"] or "").split(),
                              "the first tab stop is not the skip link: %r" % first)
                target = main_id(body)
                self.assertIsNotNone(target, "the page's <main> carries no id")
                self.assertEqual(first["href"], "#" + str(target),
                                 "the skip link points at %r, not the page's main"
                                 % first["href"])
                self.assertIn('<a class="skip" href="#%s">Skip to content</a>'
                              % target, body)

    def test_every_state_keeps_its_landmarks_and_its_one_heading(self):
        """The skip link is an anchor before ``main``, so nothing else moves.

        The demo role keeps the laptop section, the report, the access report
        and the three gaps; a tenant keeps the report, its own access section
        and the gaps. The header sits inside ``main``, so it is not a banner
        landmark of its own, and one ``<h1>`` stays one ``<h1>``.
        """
        expected = {
            "demo-bench": ["check", "report", "access", "gaps"],
            "demo-machine": ["check", "report", "access", "gaps"],
            "tenant-machine": ["report", "access", "gaps"],
            "tenant-noaccess": ["report", "access", "gaps"],
        }
        for name, body in self.states().items():
            with self.subTest(state=name):
                self.assertEqual(
                    re.findall(r'<section\b[^>]*aria-labelledby="([^"]+)"', body),
                    expected[name], "a landmark was added or removed")
                self.assertEqual(len(re.findall(r"<h1\b", body)), 1)
                self.assertEqual(len(re.findall(r"<main\b", body)), 1)
                self.assertEqual(len(re.findall(r"<header\b", body)), 1)

    def test_the_shipped_carriers_read_the_skip_link_first_and_paint_the_ring(self):
        """The review copy, the served copy and the machine's template agree."""
        carriers = (ROOT / "web" / "verify.html",
                    ROOT / "web" / "verify-public.html",
                    TEMPLATE)
        for path in carriers:
            with self.subTest(page=path.name):
                text = path.read_text(encoding="utf-8")
                stops = focus_order(text)
                self.assertTrue(stops, "the page has no focusable element")
                self.assertIn("skip", (stops[0]["class"] or "").split(),
                              "the first tab stop in the markup is %r" % stops[0])
                self.assertEqual(stops[0]["href"], "#" + str(main_id(text)))
                self.assertIn(".skip{position:absolute;left:-9999px;top:0}", text)
                rule = re.search(r"\.skip:focus-visible\{([^}]*)\}", text)
                self.assertIsNotNone(rule, "no :focus-visible rule reveals the skip link")
                assert rule is not None  # the assertion above names the failure
                declarations = rule.group(1)
                self.assertIn("left:24px", declarations)
                self.assertIn("min-height:44px", declarations)
                self.assertIn("background:var(--ink)", declarations)
                self.assertIn("color:var(--paper)", declarations)
                self.assertIn("outline:2px solid var(--ink)", declarations,
                              "the focus ring is not the sheet's 2 px --ink")
                self.assertIn("outline-offset:3px", declarations,
                              "the focus ring is not at the sheet's 3 px offset")
                self.assertNotIn(".skip:focus{", text,
                                 "the link shows on a mouse focus too")
                self.assertIn(".skip:focus-visible{left:18px}", text,
                              "the link does not follow main's 18 px inset at 390")


class VerifyPageTests(unittest.TestCase):
    """The /verify page: every row, its provenance, and a page that fails honest.

    The page is built from the machine's own records (the tenant boot report,
    the installer's boot record, the handoff audit, the grant program, the
    signed manifest) and rendered from `templates/verify.html`. These tests run
    the real program against a throwaway machine root: no network, no /proc of
    the host, one subprocess per page.
    """

    NOW = "2026-11-13T12:00:00Z"
    BOOT_LINE = base64.b64decode(
        re.search(r"(?:^|\s)bb\.key=(\S+)", TENANT_DOC["cmdline"]).group(1)
    ).decode("utf-8").strip()
    BOOT_FP = "SHA256:" + base64.b64encode(hashlib.sha256(
        base64.b64decode(BOOT_LINE.split()[1])).digest()).decode().rstrip("=")
    HOST_KEY = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILm3AdilA0maawhVo8zrQtNDK"
                "L6VL9rOd/No8qi2jpEK bench host key")
    HOST_FP = "SHA256:" + base64.b64encode(hashlib.sha256(
        base64.b64decode(HOST_KEY.split()[1])).digest()).decode().rstrip("=")
    GRANT_KEY = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOHgESGVIJvEIywSkS0UrbZP"
                 "UH7HpCyXFql99gxHCWDL odile-support-grant-9947606342d1")
    LUKS_ONE = """\
LUKS header information
Version:       \t2
Epoch:         \t3
Metadata area: \t16384 [bytes]
Keyslots area: \t16744448 [bytes]
UUID:          \t01d8852a-20f5-4942-a4f3-7841efa3df5a
Label:         \t(no label)
Subsystem:     \t(no subsystem)
Flags:       \t(no flags)

Data segments:
  0: crypt
\toffset: 16777216 [bytes]
\tlength: (whole device)
\tcipher: aes-xts-plain64
\tsector: 4096 [bytes]

Keyslots:
  0: luks2
\tKey:        512 bits
\tPriority:   normal
\tCipher:     aes-xts-plain64
\tCipher key: 512 bits
\tPBKDF:      pbkdf2
\tHash:       sha256
\tIterations: 1000
\tAF stripes: 4000
\tAF hash:    sha256
\tArea offset:32768 [bytes]
\tArea length:258048 [bytes]
\tDigest ID:  0

Tokens:
Digests:
  0: pbkdf2
\tHash:       sha256
\tIterations: 1000
\tSalt:       db 2b 16 7f 42 4d eb 71 4e 86 e3 48 f1 06 51 00
\tDigest:     8e 61 0a c3 3e 4f 7c 4a 0d b9 2e 8f 91 5d 6a 7b
"""
    LUKS_TWO = LUKS_ONE.replace(
        "Tokens:", "  1: luks2\n\tKey:        512 bits\n\tPriority:   normal\n"
                   "\tPBKDF:      pbkdf2\n\tArea offset:290816 [bytes]\n"
                   "\tArea length:258048 [bytes]\n\tDigest ID:  0\n\nTokens:")

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="odile-verify-page-")
        self.base = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    # -- fixtures -----------------------------------------------------------

    def build(self, *, audit: bool = True, support: bool = True,
              grant: dict | None = None, luks: str | None = None) -> tuple[Path, Path]:
        root = self.base / "root"

        def write(rel: str, text: str, mode: int = 0o644) -> Path:
            path = root / rel.lstrip("/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            path.chmod(mode)
            return path

        write("/proc/cmdline", TENANT_DOC["cmdline"].strip() + "\n")
        write("/proc/mounts", "/dev/mapper/root_crypt / ext4 rw,relatime 0 0\n")
        write("/var/lib/odile/attestation.json", TENANT_BYTES.decode("utf-8"))
        record = {
            "schema": "odile.boot-record.v1",
            "release": "2026.11.2",
            "measurement": TENANT_MEASUREMENT,
            "cmdline_sha256": hashlib.sha256(
                TENANT_DOC["cmdline"].strip().encode("utf-8")).hexdigest(),
            "vcpus": 4,
            "checked_at": "2026-11-13T01:13:51Z",
            "expected": TENANT_MEASUREMENT,
            "installer_sha256": hashlib.sha256(
                (ROOT / "release" / "odile_install.py").read_bytes()).hexdigest(),
            "source": "boot-report",
            "artifacts": None,
        }
        write("/etc/odile/boot-record.json", json.dumps(record, indent=2) + "\n")
        write("/etc/odile/manifest.json", RELEASE_MANIFEST.read_text(encoding="utf-8"))
        write("/etc/odile/manifest.json.sig", RELEASE_SIG.read_text(encoding="utf-8"))
        write("/etc/odile/odile-signing-key.pub", RELEASE_KEY.read_text(encoding="utf-8"))
        write("/etc/ssh/ssh_host_ed25519_key.pub", self.HOST_KEY + "\n")
        write("/etc/ssh/sshd_config",
              "Include /etc/ssh/sshd_config.d/*.conf\nPasswordAuthentication yes\n")
        write("/etc/ssh/sshd_config.d/99-odile.conf",
              "PasswordAuthentication no\nKbdInteractiveAuthentication no\n")
        write("/etc/passwd",
              "root:x:0:0:root:/root:/bin/bash\n"
              "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n"
              "odile:x:1000:1000:Odile agent:/home/odile:/bin/bash\n")
        write("/etc/shadow",
              "root:*:20000:0:99999:7:::\n"
              "odile:!:20000:0:99999:7:::\n", 0o600)
        write("/etc/sudoers.d/odile", "odile ALL=(ALL) NOPASSWD:ALL\n", 0o440)
        write("/root/.ssh/authorized_keys", self.BOOT_LINE + "\n", 0o600)
        write("/home/odile/.hermes/.env",
              "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH="
              "scrypt$16384$8$1$c2FsdA$aGFzaA\n",
              0o600)
        write("/run/bb/sealed-unlock.state", "opened\n")
        write("/var/log/auth.log",
              "".join("Accepted publickey for odile from 10.0.0.%d port 51%d ssh2: "
                      "ssh-ed25519 %s\n" % (n, n, self.BOOT_FP) for n in (2, 3)))
        if audit:
            target = write("/usr/local/bin/odile-audit",
                           AUDIT.read_text(encoding="utf-8"), 0o755)
            target.chmod(0o755)
        if support:
            target = write("/usr/local/sbin/odile-support",
                           SUPPORT.read_text(encoding="utf-8"), 0o755)
            target.chmod(0o755)
        if grant:
            write("/var/lib/odile/support-grant.json",
                  json.dumps(grant, indent=2) + "\n")
            with (root / "root/.ssh/authorized_keys").open("a", encoding="utf-8") as handle:
                handle.write('command="/usr/local/sbin/odile-support session",'
                             'no-port-forwarding %s\n' % self.GRANT_KEY)
        dump = self.base / "luks.txt"
        dump.write_text(luks if luks is not None else self.LUKS_ONE, encoding="utf-8")
        return root, dump

    def grant_document(self) -> dict:
        return {
            "grant_id": "9947606342d1",
            "slug": "odl-fake01",
            "reason": "drill",
            "created_at": "2026-11-13T09:00:00Z",
            "expires_at": "2026-11-14T09:00:00Z",
            "hours": 24,
            "key_fingerprint": self.GRANT_FP,
            "key_comment": "odile-support-grant-9947606342d1",
        }

    GRANT_FP = "SHA256:" + base64.b64encode(hashlib.sha256(
        base64.b64decode(GRANT_KEY.split()[1])).digest()).decode().rstrip("=")

    def run_page(self, root: Path, dump: Path, *args: str) -> subprocess.CompletedProcess:
        command = [sys.executable, str(PAGE), "--root", str(root),
                   "--luks-dump", str(dump), "--now", self.NOW, *args]
        return subprocess.run(command, text=True, capture_output=True)

    def page(self, **kwargs) -> str:
        root, dump = self.build(**kwargs)
        result = self.run_page(root, dump)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def page_json(self, **kwargs) -> dict:
        root, dump = self.build(**kwargs)
        result = self.run_page(root, dump, "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    # -- the report ---------------------------------------------------------

    def test_the_page_shows_the_live_report_its_fingerprint_and_the_measurement(self) -> None:
        body = self.page()
        self.assertIn(TENANT_MEASUREMENT, body, "the full measurement must be on the page")
        report = base64.b64decode(TENANT_DOC["report_b64"])
        self.assertIn(hashlib.sha256(report).hexdigest(), body,
                      "the fingerprint of the signed report bytes must be on the page")
        self.assertIn("2026.11.2", body)
        self.assertIn("4 vCPU", body)
        self.assertIn("2026-11-13T01:13:51Z", body, "the report's fetched_at")

    def test_a_report_without_fetched_at_keeps_the_row_intact(self) -> None:
        # The fetched timestamp is a mono mark; an empty mark must not rewrite
        # the row's HTML character by character.
        root, dump = self.build()
        path = root / "var/lib/odile/attestation.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document.pop("fetched_at", None)
        path.write_text(json.dumps(document), encoding="utf-8")
        result = self.run_page(root, dump)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('<span class="mono"></span>', result.stdout)
        self.assertIn(TENANT_MEASUREMENT, result.stdout)
        self.assertRegex(result.stdout, r"report.*?the SHA-256 of the signed", re.S)

    def test_every_row_carries_exactly_one_provenance_tag(self) -> None:
        body = self.page()
        rows = re.findall(r"<tr class=\"check\">(.*?)</tr>", body, re.S)
        self.assertGreaterEqual(len(rows), 15, "the page lost rows")
        laptop = [row for row in rows if "checked from your laptop" in row]
        machine = [row for row in rows if "read from the machine" in row]
        self.assertEqual(len(rows), len(laptop) + len(machine),
                         "a row carries no provenance tag")
        self.assertEqual(len(laptop), 3, "the three deeper checks are the laptop rows")
        for row in rows:
            self.assertEqual(
                row.count("checked from your laptop") + row.count("read from the machine"),
                1, "a row carries two provenance tags: %s" % row[:120])

    def test_the_page_shows_root_lock_password_auth_off_panel_reset_and_accounts(self) -> None:
        body = self.page()
        self.assertRegex(body, r"root password.*?locked", re.S)
        self.assertRegex(body, r"password auth.*?PasswordAuthentication no", re.S)
        self.assertRegex(body, r"panel root reset.*?no guest agent", re.S)
        self.assertRegex(body, r"accounts.*?root and odile", re.S)

    def test_the_boot_key_and_the_host_key_are_distinct_fingerprints(self) -> None:
        body = self.page()
        self.assertEqual(self.BOOT_FP, "SHA256:I2VaMwa+RRbbA6/f4AJhQUJv3WhAnjQvNxqmWZAVPCM")
        self.assertIn(self.BOOT_FP, body)
        self.assertIn(self.HOST_FP, body)
        self.assertNotEqual(self.BOOT_FP, self.HOST_FP)
        text = self.plain(body)
        self.assertRegex(text, r"host key.*?distinct from the boot key", re.S)
        self.assertRegex(text, r"boot key.*?one bb\.key in the command line", re.S)
        self.assertRegex(text, r"authorized keys.*?every one the measured boot key", re.S)

    def test_the_page_shows_one_keyslot_and_the_sealed_unlock_state(self) -> None:
        body = self.plain(self.page())
        self.assertRegex(body, r"LUKS.*?one keyslot \(slot 0\)", re.S)
        self.assertRegex(body, r"boot and unlock.*?opened", re.S)

    def test_the_page_shows_the_seal_policy_and_the_signin_hash(self) -> None:
        """CLAIMS §4.4 names both rows; the page must render them (t_4fcd465f)."""
        body = self.page()
        cell = self.row(body, "seal policy")
        text = self.plain(cell)
        self.assertIn("verdict pass", cell)
        self.assertIn("policy 0x30000", text)
        self.assertIn('<span class="mono">0x30000</span>', cell)
        seal_id = re.search(r"(?:^|\s)seal_id=(\S+)", TENANT_DOC["cmdline"]).group(1)
        self.assertIn("seal_id %s" % seal_id, text)
        self.assertIn('<span class="mono">%s</span>' % TENANT_MEASUREMENT, cell)
        signin = self.row(body, "sign-in hash")
        self.assertIn("verdict pass", signin)
        self.assertIn("scrypt hash present in /home/odile/.hermes/.env; "
                      "no plaintext password", self.plain(signin))
        # The hash itself is a secret-shaped value: presence, never the value.
        self.assertNotIn("$scrypt$", body)

    def test_a_missing_seal_id_and_a_plaintext_password_fail_their_rows(self) -> None:
        root, dump = self.build()
        cmdline_path = root / "proc/cmdline"
        cmdline_path.write_text(
            re.sub(r"\s*seal_id=\S+", "", cmdline_path.read_text(encoding="utf-8")),
            encoding="utf-8")
        env = root / "home/odile/.hermes/.env"
        env.write_text(env.read_text(encoding="utf-8")
                       + "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=plaintext\n",
                       encoding="utf-8")
        result = self.run_page(root, dump)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        seal = self.row(result.stdout, "seal policy")
        self.assertIn("verdict fail", seal)
        self.assertIn("no measured seal_id", seal)
        signin = self.row(result.stdout, "sign-in hash")
        self.assertIn("verdict fail", signin)
        self.assertIn("plaintext password", signin)

    # -- the manifest -------------------------------------------------------

    def test_the_signed_manifest_is_verified_on_the_machine(self) -> None:
        body = self.page()
        self.assertRegex(body, r"manifest.*?signature verified", re.S)
        self.assertIn("SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8", body)

    def test_a_tampered_manifest_is_reported_bad(self) -> None:
        root, dump = self.build()
        manifest = root / "etc/odile/manifest.json"
        manifest.write_text(manifest.read_text(encoding="utf-8").replace(
            '"audit_sha256"', '"audit_sha256" ', 1), encoding="utf-8")
        result = self.run_page(root, dump)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"manifest.*?signature BAD", re.S)

    # -- the support grant --------------------------------------------------

    def test_no_grant_is_the_baseline(self) -> None:
        body = self.page()
        self.assertRegex(body, r"support access.*?no grant", re.S)

    def test_a_live_grant_shows_two_keys_and_two_keyslots(self) -> None:
        body = self.page(grant=self.grant_document(), luks=self.LUKS_TWO)
        self.assertRegex(body, r"support access.*?grant live", re.S)
        self.assertIn("9947606342d1", body)
        self.assertIn("2026-11-14T09:00:00Z", body)
        self.assertRegex(body, r"grant live.*?2 keys", re.S)
        self.assertRegex(body, r"grant live.*?2 keyslots", re.S)
        self.assertIn(self.GRANT_FP, body)
        self.assertRegex(body, r"LUKS.*?second is the support grant", re.S)

    # -- the authorized keys row is the door, not the key -------------------

    def row(self, body: str, name: str) -> str:
        match = re.search(
            r'<tr class="check"><th scope="row">%s</th><td>(.*?)</td><td class="prov">'
            % re.escape(name), body, re.S)
        self.assertIsNotNone(match, "no row named %r" % name)
        return match.group(1)

    def plain(self, body: str) -> str:
        """The page's text, tags removed, for assertions about sentences."""
        return html.unescape(re.sub(r"<[^>]+>", "", body))

    def test_the_authorized_keys_row_is_the_one_measured_boot_key(self) -> None:
        cell = self.row(self.page(), "authorized keys")
        self.assertIn("verdict pass", cell)
        self.assertIn("1 line(s) over 1 file(s), every one the measured boot key", cell)

    def test_a_live_grant_fails_the_authorized_keys_row_and_names_the_grant_key(self) -> None:
        # The ODL-217 drill on the live tenant: during a grant a second key is in
        # authorized_keys, so this row must fail and name it, not say "one key".
        body = self.page(grant=self.grant_document(), luks=self.LUKS_TWO)
        cell = self.row(body, "authorized keys")
        self.assertIn("verdict fail", cell)
        self.assertIn("1 line(s) that are not the measured boot key", cell)
        self.assertIn('<span class="mono">%s</span>' % html.escape(self.GRANT_FP), cell)
        self.assertRegex(self.row(body, "support access"), r"2 keys in authorized_keys")
        self.assertNotIn("one bb.key in the command line, public key only",
                         cell, "the row still claims there is one key")

    # -- the commands, the gaps, the status link ---------------------------

    def test_the_commands_from_the_claims_deck_are_on_the_page(self) -> None:
        body = self.page()
        for command in ("passwd -S root",
                        "sshd -T | grep -i '^passwordauthentication'",
                        "ssh-keygen -lf /root/.ssh/authorized_keys",
                        "DEV=$(cryptsetup status root_crypt",
                        'cryptsetup luksDump "$DEV"',
                        "odile-support status",
                        "curl -s https://&lt;slug&gt;.odile.fyi/attestation",
                        "sha256sum /usr/local/bin/odile-install.sh"):
            self.assertIn(command, body, "the deck's command is missing: %s" % command)

    def test_the_luks_row_prints_the_container_resolution_not_the_mapper(self) -> None:
        """The mapper is plaintext: luksDump on it is refused (support.md)."""
        cell = html.unescape(self.row(self.page(), "LUKS"))
        self.assertIn('cryptsetup luksDump "$(cryptsetup status root_crypt', cell)
        self.assertNotIn("/dev/mapper/root_crypt", cell)

    def test_the_two_command_panels_carry_the_page_wrap_rule(self) -> None:
        """At 390 the three long lines were clipped by a 320 px code box."""
        body = self.page()
        self.assertEqual(body.count('<pre class="report-pre">'), 2,
                         "both command panels must wrap, not scroll sideways")
        self.assertRegex(body, r"\.report-pre code\{[^}]*white-space:pre-wrap[^}]*\}")

    def test_the_live_luks_row_resolves_the_device_from_the_mapper_name(self) -> None:
        """On `/` the program asks cryptsetup status which device to dump."""
        spec = importlib.util.spec_from_file_location(
            "odile_verify_luks_device", PAGE)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["odile_verify_luks_device"] = module
        spec.loader.exec_module(module)
        stamp = module.parse_iso(self.NOW)
        self.assertIsNotNone(stamp)
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "sda3"
            device.write_text("", encoding="utf-8")
            calls = []

            def fake_run(argv, **_kwargs):
                calls.append(argv)
                if argv[:2] == ["cryptsetup", "status"]:
                    return subprocess.CompletedProcess(
                        argv, 0, "  type:    LUKS2\n  device:  %s\n" % device, "")
                if argv[:2] == ["cryptsetup", "luksDump"]:
                    return subprocess.CompletedProcess(
                        argv, 0, self.LUKS_ONE, "")
                return subprocess.CompletedProcess(argv, 127, "", "missing")

            with mock.patch.object(module, "run", fake_run):
                machine = module.Machine(root=Path("/"), now=stamp)
                keyslots, tokens, command = machine.luks()
        self.assertEqual(keyslots, ["0"])
        self.assertEqual(tokens, [])
        self.assertIn(["cryptsetup", "luksDump", str(device)], calls)
        self.assertEqual(command, "cryptsetup luksDump %s" % device)

    def test_the_three_powers_paragraph_names_the_sign_in_not_a_setup_step(self) -> None:
        # B-once (ODL-929, D-7): the registration password is the machine's
        # sign-in, so the check is "before you sign in", not a first-visit
        # setup step. The retired phrase rode inside the signed installer.
        body = self.page()
        self.assertIn("compare it to the one in your ready email before you sign in", body)
        self.assertNotIn("set your sign-in", body)

    def test_the_three_gaps_are_stated_in_order_with_the_manifest_note(self) -> None:
        body = self.page()
        gaps = ("The app layer is not hardware-measured yet.",
                "The console passes through the host.",
                "Odile keeps three powers.")
        for gap in gaps:
            self.assertIn(gap, body)
        positions = [body.index(gap) for gap in gaps]
        self.assertEqual(positions, sorted(positions), "the gaps are out of order")
        self.assertIn("The manifest records the machine as provisioned", body)

    def test_the_status_page_is_linked_with_its_approved_sentence(self) -> None:
        body = self.page()
        self.assertIn('href="https://odile.fyi/status"', body)
        self.assertIn("The status page counts waiting-for-owner separately "
                      "and names no tenant.", body)

    # -- no JavaScript, no third-party request, the claims lint ------------

    def test_no_javascript_and_no_third_party_request(self) -> None:
        body = self.page()
        self.assertNotIn("<script", body.lower())
        self.assertIsNone(
            re.search(r"<(?:script|link|img)\b[^>]*\b(?:src|href)=[\"']https?://", body, re.I),
            "the page makes an external request")
        self.assertNotIn("url(http", body.replace(" ", "").lower())
        # Links may name Odile's own pages, and the commands may name this
        # machine's own `<slug>.odile.fyi` URL. Nothing else.
        allowed = re.compile(r"^https://(?:odile\.fyi|<slug>\.odile\.fyi)(?:/[^\s\"'<>]*)?$")
        for url in re.findall(r"https?://[^\s\"'<>]+", html.unescape(body)):
            self.assertRegex(url, allowed, "the page names an external URL: %s" % url)

    def test_the_claims_lint_covers_the_page_and_the_template(self) -> None:
        rendered = self.base / "rendered.html"
        rendered.write_text(self.page(), encoding="utf-8")
        for target in (rendered, TEMPLATE):
            result = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "claims_lint.py"),
                 "--root", str(ROOT), str(target)],
                text=True, capture_output=True)
            self.assertEqual(result.returncode, 0,
                             "%s: %s%s" % (target, result.stdout, result.stderr))

    # -- CSS off, and the phone ---------------------------------------------

    def test_the_page_reads_in_order_with_css_disabled(self) -> None:
        body = self.page()
        text = re.sub(r"<style>.*?</style>", "", body, flags=re.S)
        text = re.sub(r"<[^>]+>", "\n", text)
        text = html.unescape(text)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        order = ["This machine, checked.", "From your laptop", "From the machine",
                 "Check it yourself", "The three gaps we state before anyone asks"]
        positions = []
        for heading in order:
            self.assertIn(heading, lines, "a heading disappeared with CSS off")
            positions.append(lines.index(heading))
        self.assertEqual(positions, sorted(positions), "CSS-off order is wrong")
        # Nothing in the body may be hidden by an attribute or an inline style;
        # the only `display:none` is the phone table head, which CSS-off shows.
        self.assertNotRegex(body, r"<[^>]*\shidden(?:\s|=|>)")
        self.assertNotIn(' style="display:none"', body)

    def test_the_page_has_the_brand_face_and_the_phone_viewport(self) -> None:
        body = self.page()
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', body)
        self.assertIn("Cormorant Garamond", body)
        self.assertIn("data:font/woff2", body)
        self.assertIn("@media (max-width:560px)", body)

    def test_prose_reads_in_the_text_face_and_headings_keep_the_display_face(self) -> None:
        """BRAND.md §Typography: the display face never sets a reading sentence.

        The surface table's row for /verify reads "Text serif for prose;
        display for headings; mono for credentials and machine values", and the
        page this card replaces (`web/verify.html`, t_ae833f3f) already reads
        in the text face. Measured on the painted face: Cormorant at 18/1.55
        for the body, 15.2 px notes and a 15.2 px foot regress the type roles
        on the same URL.
        """
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn('--text:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,'
                      '"DejaVu Serif",serif}', text)
        self.assertIn("font:400 18px/1.6 var(--text);", text)
        self.assertIn(".lede{font-size:18px;line-height:1.6;", text)
        self.assertIn(".note{margin:0 0 16px;font-size:16px;line-height:1.6;", text)
        self.assertIn(".foot{margin:32px 0 0;padding-top:16px;border-top:1px solid "
                      "var(--line);font-size:16px;line-height:1.6;", text)
        self.assertIn("h1,h2,.brand{font-family:var(--serif)}", text)
        self.assertNotIn("font:400 18px/1.55", text,
                         "the prose is back on the display face at display leading")
        self.assertNotIn(".note{margin:0 0 16px;font-size:.95rem", text,
                         "the note is back below the 16 px text role")
        # Cormorant is named by the @font-face and the --serif token only;
        # a third occurrence is a prose selector setting the display face.
        self.assertEqual(text.count('"Cormorant Garamond"'), 2,
                         "the display face is set on a prose selector")

    # -- brand round 1: the machine value role ------------------------------

    def test_the_machine_values_are_set_in_the_mono_role(self) -> None:
        # BRAND.md §Typography: a machine value is set in the mono role, so the
        # page can be compared with the stranger's own command output by eye.
        body = self.page(grant=self.grant_document(), luks=self.LUKS_TWO)
        report = base64.b64decode(TENANT_DOC["report_b64"])
        manifest = json.loads(RELEASE_MANIFEST.read_text(encoding="utf-8"))
        for name, value in (
                ("report", "2026-11-13T01:13:51Z"),
                ("report", hashlib.sha256(report).hexdigest()),
                ("measurement", TENANT_MEASUREMENT),
                ("release", "2026.11.2"),
                ("release", "4 vCPU"),
                ("boot key", self.BOOT_FP),
                ("host key", self.HOST_FP),
                ("host key", "/etc/ssh/ssh_host_ed25519_key.pub"),
                ("boot and unlock", "2026-11-13T01:13:51Z"),
                ("support access", "9947606342d1"),
                ("support access", "2026-11-14T09:00:00Z"),
                ("support access", self.GRANT_FP),
                ("manifest", manifest["installer_sha256"][:12] + "\u2026"),
                ("manifest", "SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8")):
            self.assertIn('<span class="mono">%s</span>' % html.escape(value),
                          self.row(body, name),
                          "%s is not set in the mono role: %r" % (name, value))
        # And no hash, fingerprint or timestamp is left bare in any value cell.
        for cell in re.findall(r"<td>(.*?)</td>", body, re.S):
            bare = re.sub(r'<span class="mono">.*?</span>', "", cell, flags=re.S)
            bare = re.sub(r'<code class="cmd">.*?</code>', "", bare, flags=re.S)
            bare = html.unescape(re.sub(r"<[^>]+>", "", bare))
            for pattern in (r"[0-9a-f]{96}", r"[0-9a-f]{64}",
                            r"SHA256:[A-Za-z0-9+/=]{16,}",
                            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"):
                self.assertIsNone(re.search(pattern, bare),
                                  "%r is bare outside the mono role: %r" % (pattern, bare[:160]))

    def test_the_phone_header_keeps_the_32px_marks_16px_clear_space(self) -> None:
        """BRAND.md §Name and mark: a 32 px mark keeps 16 px of clear space.

        This page's header carries the wordmark alone, so its own bottom
        margin is the whole gap to the h1. The 24 px phone margin measured
        12.0 px of box gap at 390 and 320 — 4 px under the floor — and the
        sheet's next on-scale value, 32 px, measures 20.0 px there and at
        1440. t_fc412bd9's render probe reads those figures; this test fails
        first if the rule moves again, before the page ships inside the
        signed installer.
        """
        body = self.page()
        css = re.search(r"<style>(.*?)</style>", body, re.S).group(1)
        wide = re.search(r"\.top\{([^}]*)\}", css)
        self.assertIsNotNone(wide, "the page lost its header rule")
        assert wide is not None  # the assertion above names the failure
        self.assertIn("margin:0 0 32px", wide.group(1))
        phone = re.search(r"@media \(max-width:560px\)\{(.*?)\n\}", css, re.S)
        self.assertIsNotNone(phone, "the page lost its phone block")
        assert phone is not None
        rule = re.search(r"\.top\{([^}]*)\}", phone.group(1))
        self.assertIsNotNone(rule, "the phone block has no .top margin")
        assert rule is not None
        self.assertIn("margin-bottom:32px", rule.group(1),
                      "the 32 px mark is under its 16 px clear-space floor at ≤560")
        self.assertNotIn("28px", phone.group(1),
                         "28 px is off the sheet's 4 px scale")
        brand = re.search(r"\.brand\{([^}]*)\}", css)
        self.assertIsNotNone(brand, "the page lost its wordmark rule")
        assert brand is not None
        self.assertIn("padding:12px 0", brand.group(1))

    def test_the_header_carries_the_wordmark_alone(self) -> None:
        # BRAND.md change log 2026-11-11: one link, not two to the same place.
        header = re.search(r'<header class="top">(.*?)</header>',
                           self.page(), re.S).group(1)
        self.assertEqual(len(re.findall(r"<a\b", header)), 1, header)
        self.assertIn('<a class="brand" href="/">Odile</a>', header)

    def test_lowercase_provenance_labels_are_not_tracked(self) -> None:
        css = re.search(r"<style>(.*?)</style>", self.page(), re.S).group(1)
        for selector in (r"\.provenance\{", r"\.checks \.prov\{"):
            block = re.search(selector + r"([^}]*)\}", css)
            self.assertIsNotNone(block, "no rule for %s" % selector)
            self.assertIn("letter-spacing:normal", block.group(1), selector)

    # -- the server ---------------------------------------------------------

    def test_the_server_serves_the_page_and_404s_everything_else(self) -> None:
        root, dump = self.build()
        process = subprocess.Popen(
            [sys.executable, str(PAGE), "--root", str(root), "--luks-dump", str(dump),
             "--now", self.NOW, "--serve", "--bind", "127.0.0.1", "--port", "0"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            line = process.stdout.readline().strip()
            match = re.search(r"http://(\S+):(\d+)/", line)
            self.assertIsNotNone(match, "the server did not print its address: %r" % line)
            base = "http://%s:%s" % (match.group(1), match.group(2))
            with urllib.request.urlopen(base + "/", timeout=10) as response:
                body = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("text/html", response.headers.get("Content-Type", ""))
                self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertIn(TENANT_MEASUREMENT, body)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + "/nope", timeout=10)
            self.assertEqual(caught.exception.code, 404)
        finally:
            process.terminate()
            process.wait(timeout=10)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

    def test_the_installed_program_finds_the_template_beside_it(self) -> None:
        """The unit runs /usr/local/lib/odile/verify.py with no --template.

        The program's default must be the template the installer writes beside
        it, not a checkout path that does not exist on a tenant.
        """
        root, dump = self.build()
        with tempfile.TemporaryDirectory(prefix="odile-verify-install-") as tmp:
            lib = Path(tmp) / "usr/local/lib/odile"
            lib.mkdir(parents=True)
            shutil.copy2(ROOT / "verify" / "verify.py", lib / "verify.py")
            shutil.copy2(ROOT / "templates" / "verify.html", lib / "verify.html")
            process = subprocess.Popen(
                [sys.executable, str(lib / "verify.py"), "--root", str(root),
                 "--luks-dump", str(dump), "--now", self.NOW,
                 "--serve", "--bind", "127.0.0.1", "--port", "0"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                assert process.stdout is not None
                line = process.stdout.readline().strip()
                match = re.search(r"http://(\S+):(\d+)/", line)
                assert match is not None, "the server did not print its address: %r" % line
                with urllib.request.urlopen(
                        "http://%s:%s/verify" % (match.group(1), match.group(2)),
                        timeout=10) as response:
                    body = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            finally:
                process.terminate()
                process.wait(timeout=10)
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
        self.assertIn(TENANT_MEASUREMENT, body)
        # The page renders the seal policy and the sign-in hash here too: the
        # installed layout is the shipped one, not the checkout's.
        self.assertIn("policy 0x30000", self.plain(body))
        self.assertIn("scrypt hash present", self.plain(body))

    # -- honest failure -----------------------------------------------------

    def test_a_machine_without_the_audit_still_renders_and_says_so(self) -> None:
        body = self.page(audit=False, support=False)
        self.assertIn("This machine, checked.", body)
        self.assertIn("not installed", body)
        self.assertIn(TENANT_MEASUREMENT, body)

    def test_a_machine_without_a_report_says_so_and_keeps_the_marker_rows(self) -> None:
        root, dump = self.build()
        (root / "var/lib/odile/attestation.json").unlink()
        result = self.run_page(root, dump)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Report unavailable.", result.stdout)
        self.assertIn("From your laptop", result.stdout)
        self.assertIn("From the machine", result.stdout)

    def test_a_template_with_an_unfilled_slot_is_refused(self) -> None:
        root, dump = self.build()
        broken = self.base / "broken.html"
        broken.write_text(TEMPLATE.read_text(encoding="utf-8") + "{{UNKNOWN_SLOT}}\n",
                          encoding="utf-8")
        result = self.run_page(root, dump, "--template", str(broken))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("unfilled", result.stderr)

    def test_the_json_document_carries_every_row_and_its_tag(self) -> None:
        document = self.page_json()
        rows = [row for group in document["groups"] for row in group["rows"]]
        self.assertGreaterEqual(len(rows), 15)
        self.assertTrue(all(row["tag"] in ("laptop", "machine") for row in rows))
        self.assertIn("report", [row["name"] for row in rows])
        self.assertEqual(document["schema"], 1)

    # -- parity with the handoff audit -------------------------------------

    def test_the_page_agrees_with_the_handoff_audit_it_wraps(self) -> None:
        root, dump = self.build()
        audit = subprocess.run(
            [sys.executable, str(root / "usr/local/bin/odile-audit"),
             "--root", str(root), "--luks-dump", str(dump), "--json"],
            text=True, capture_output=True)
        self.assertIn(audit.returncode, (0, 1), audit.stderr)
        document = json.loads(audit.stdout)
        verdicts = {check["name"]: check["ok"] for check in document["checks"]}
        self.assertTrue(verdicts["root password"])
        self.assertTrue(verdicts["password auth"])
        self.assertTrue(verdicts["keyslots"])
        self.assertTrue(verdicts["boot key"])
        self.assertTrue(verdicts["authorized keys"])
        page = self.page_json()
        names = {row["name"]: row for group in page["groups"] for row in group["rows"]}
        for page_name, audit_name in (("root password", "root password"),
                                      ("password auth", "password auth"),
                                      ("boot key", "boot key"),
                                      ("authorized keys", "authorized keys")):
            self.assertEqual(names[page_name]["state"],
                             "PASS" if verdicts[audit_name] else "FAIL",
                             "the page and the audit disagree on %s" % audit_name)
        self.assertEqual(names["LUKS"]["state"], "PASS")

    # -- the keyboard contract ---------------------------------------------

    def test_the_first_tab_stop_is_the_skip_link_and_the_landmarks_did_not_move(self) -> None:
        """BRAND.md's Keyboard row on the page a tenant actually reads.

        The rendered page has four named sections, so a skip link comes first:
        it is an anchor before ``main``, points at the page's own ``main``,
        and adds or removes no landmark. The rule that paints the sheet's ring
        is read in ``VerifySkipLinkTests``; here the page is the render
        ``odile-verify`` produced, not the template text.
        """
        body = self.page()
        stops = focus_order(body)
        self.assertTrue(stops, "the rendered page has no focusable element")
        self.assertEqual(stops[0]["tag"], "a",
                         "the first tab stop is <%s>, not the skip link"
                         % stops[0]["tag"])
        self.assertIn("skip", (stops[0]["class"] or "").split())
        target = main_id(body)
        self.assertIsNotNone(target, "the page's <main> carries no id")
        self.assertEqual(stops[0]["href"], "#" + str(target))
        self.assertIn('<a class="skip" href="#%s">Skip to content</a>' % target, body)
        self.assertEqual(
            re.findall(r'<section\b[^>]*aria-labelledby="([^"]+)"', body),
            ["laptop-h", "machine-h", "check-h", "gaps-h"],
            "a landmark was added or removed")
        self.assertEqual(len(re.findall(r"<h1\b", body)), 1)
        self.assertEqual(len(re.findall(r"<main\b", body)), 1)
        self.assertEqual(len(re.findall(r"<header\b", body)), 1)



    # -- the served page's pace (ODL-947 follow-up) -------------------------

    STUB_AUDIT = """\
#!/usr/bin/python3
\"\"\"A stand-in for odile-audit: one JSON answer, and a line per run.\"\"\"
import json, sys
from pathlib import Path
counter = Path(__file__).resolve().parent / "audit-runs"
with counter.open("a", encoding="utf-8") as handle:
    handle.write("run\\n")
checks = [{"name": name, "ok": True, "detail": "stub " + name, "command": "stub"}
          for name in ("boot key", "authorized keys", "root password", "password auth",
                       "operator account", "login history")]
print(json.dumps({"checks": checks}))
"""

    def stub_audit(self, root: Path) -> Path:
        """Replace the copied audit with one that counts its own runs."""
        program = root / "usr/local/bin/odile-audit"
        program.write_text(self.STUB_AUDIT, encoding="utf-8")
        program.chmod(0o755)
        return program.parent / "audit-runs"

    def audit_runs(self, counter: Path) -> int:
        try:
            return len(counter.read_text(encoding="utf-8").splitlines())
        except OSError:
            return 0

    def serve_page(self, root: Path, dump: Path, *extra: str) -> int:
        port = free_port()
        command = [sys.executable, str(PAGE), "--root", str(root),
                   "--luks-dump", str(dump), "--serve", "--bind", "127.0.0.1",
                   "--port", str(port), "--template", str(TEMPLATE), *extra]
        process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)

        def stop() -> None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
            if process.stdout is not None:
                process.stdout.close()

        self.addCleanup(stop)
        for _ in range(200):
            if process.poll() is not None:
                break
            try:
                if self.http_get(port, "/health")[0] == 200:
                    return port
            except OSError:
                pass
            time.sleep(0.05)
        raise AssertionError("the page server did not start: %s"
                             % (process.stdout.read() if process.stdout else ""))

    def http_get(self, port: int, path: str) -> tuple[int, str]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8", "replace")
        finally:
            connection.close()

    def test_the_served_page_keeps_one_audit_and_names_when_it_was_read(self) -> None:
        """One audit answers every request, and a kept one says so."""
        root, dump = self.build()
        counter = self.stub_audit(root)
        port = self.serve_page(root, dump)
        for _ in range(100):
            if self.audit_runs(counter) >= 1:
                break
            time.sleep(0.05)
        self.assertEqual(self.audit_runs(counter), 1,
                         "the server did not read the audit as it started")
        status, first = self.http_get(port, "/verify")
        self.assertEqual(status, 200)
        self.assertIn("audit as of", first,
                      "the page did not name the audit it kept")
        status, second = self.http_get(port, "/verify")
        self.assertEqual(status, 200)
        self.assertEqual(self.audit_runs(counter), 1,
                         "a second request ran the handoff audit again")
        self.assertIn("audit as of", second)
        rows = lambda body: re.findall(r'<tr class="check">.*?</tr>', body, re.S)
        self.assertGreaterEqual(len(rows(second)), 15)
        self.assertEqual(rows(first), rows(second),
                         "the cached request changed a row")
        self.assertIn("read from the machine", second)
        self.assertIn("checked from your laptop", second)

    def test_a_stale_audit_is_served_at_once_while_one_refresh_runs(self) -> None:
        """Past the window the page does not wait: one thread replaces it."""
        page = load_page()
        now = [1000.0]
        calls: list[float] = []
        second_started = threading.Event()
        release_second = threading.Event()

        def produce():
            calls.append(now[0])
            if len(calls) == 2:
                second_started.set()
                release_second.wait(10)
            return page.Source({"checks": []}, "")

        cache = page.AuditCache(produce, ttl=10, clock=lambda: now[0],
                                stamp=lambda: "T%d" % int(now[0]))
        first = cache.get()
        self.assertTrue(first.fresh)
        self.assertEqual((first.read_at, len(calls)), ("T1000", 1))
        now[0] = 1011.0
        stale = cache.get()
        self.assertFalse(stale.fresh)
        self.assertEqual(stale.read_at, "T1000")
        self.assertTrue(second_started.wait(5), "no background refresh started")
        again = cache.get()
        self.assertFalse(again.fresh)
        time.sleep(0.05)
        self.assertEqual(len(calls), 2, "a second request started a second read")
        release_second.set()
        for _ in range(200):
            if cache.get().read_at != "T1000":
                break
            time.sleep(0.02)
        self.assertEqual(cache.get().read_at, "T1011",
                         "the background read never landed")
        self.assertEqual(len(calls), 2)

    def test_a_kept_audit_leaves_every_row_and_verdict_alone(self) -> None:
        """The cache changes the age line, not a single row or verdict."""
        page = load_page()
        root, dump = self.build()
        self.stub_audit(root)
        stamp = page.parse_iso(self.NOW)
        source = page.Machine(root=root, now=stamp, luks_dump=dump).audit()
        self.assertIsNotNone(source.document, "the stub audit did not answer")
        cached = page.Machine(root=root, now=stamp, luks_dump=dump,
                              cached_audit=page.Cached(source, "2026-11-13T11:59:30Z",
                                                       False))
        page.collect(cached)
        live = page.Machine(root=root, now=stamp, luks_dump=dump)
        page.collect(live)
        self.assertEqual(page.document(cached)["groups"], page.document(live)["groups"])
        self.assertEqual(page.document(cached)["summary"], page.document(live)["summary"])
        template = (ROOT / "templates" / "verify.html").read_text(encoding="utf-8")
        body = page.render(cached, template)
        self.assertIn("audit as of", body)
        self.assertIn("2026-11-13T11:59:30Z", body)
        self.assertNotIn("audit as of", page.render(live, template))

    def test_a_failed_audit_is_never_kept(self) -> None:
        """A boot that has not written the audit's files yet retries next time."""
        page = load_page()
        now = [1000.0]
        calls: list[float] = []

        def produce():
            calls.append(now[0])
            return page.Source(None, "no answer")

        cache = page.AuditCache(produce, ttl=600, clock=lambda: now[0],
                                stamp=lambda: "2026-11-25T00:00:00Z")
        failed = cache.get()
        self.assertTrue(failed.fresh)
        now[0] = 1001.0
        again = cache.get()
        self.assertFalse(again.fresh)
        for _ in range(200):
            if len(calls) >= 2:
                break
            time.sleep(0.02)
        self.assertEqual(len(calls), 2, "a failed audit was kept as fresh")


if __name__ == "__main__":
    unittest.main()
