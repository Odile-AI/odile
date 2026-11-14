#!/usr/bin/python3
"""Tests for ODL-397: the tenant boot report, transformed and served.

The machine-free half of the card. A saved 1184-byte report stands in for the
one `bb-seed` writes to /run/blackbox/attest.json; the transformer turns the
Servury JSON shape into the shape `attest.py` serves and `odile-verify` reads.
The installer test writes the transformer, the oneshot and the Caddy route into
a scratch root through the installer's own functions.

Organised around the card's lines:

* a fixture boot report transforms to the exact served shape, and the
  measurement and report bytes equal the input;
* `status != ok`, a missing file and a malformed report are `available: false`
  with a reason;
* `odile-verify --expect` reads the served document and PASSes;
* the installer writes the transformer and the oneshot, and the per-slug Caddy
  block serves exactly one file at /attestation with `no-store`.

    /usr/bin/python3 -m unittest tests.test_attest -v
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ATTEST = ROOT / "attest.py"
INSTALLER = ROOT / "installer" / "odile_install.py"
VERIFY = ROOT / "odile-verify"
FIXTURE = ROOT / "tests" / "fixtures" / "verify" / "report-2026-11-06.json"

BOOT_AT = "2026-11-09T00:00:00Z"
RELEASE = "2026.11.1"
CMDLINE = (
    "console=ttyS0 consoleblank=0 root=/dev/mapper/root_crypt ro "
    "ds=nocloud;s=/run/blackbox/seed/ bb.release=2026.11.1 bb.iid=test "
    "bb.key=c3NoLWVkMjU1MTkgdGVzdA== end initrd=initrd"
)
NOT_LIVENESS = "the boot report, not a liveness proof"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules; register first.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ATTEST_MODULE = load_module(ATTEST, "odile_attest")
INSTALLER_MODULE = load_module(INSTALLER, "odile_installer")
FIXTURE_DOCUMENT = json.loads(FIXTURE.read_text(encoding="utf-8"))


def boot_document(**overrides) -> dict:
    """A document in the shape `bb-seed` writes, carrying the saved report."""
    document = {
        "version": 1,
        "release": RELEASE,
        "status": "ok",
        "hostkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITEST support@odile.fyi",
        "fingerprint": "SHA256:2XWkeR4wwCkW9+iwfhTV/vFufe88fUTxvsXrcRBrCyA",
        "report_b64": FIXTURE_DOCUMENT["report_b64"],
        "cmdline": CMDLINE,
    }
    document.update(overrides)
    return document


def run_attest(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ATTEST), *args],
        capture_output=True, text=True, timeout=60, cwd=ROOT,
    )


class TransformTests(unittest.TestCase):
    def test_a_boot_report_transforms_to_the_served_shape(self):
        served = ATTEST_MODULE.transform_boot_report(boot_document(), BOOT_AT)
        # The exact keys attest.py writes, in its order; the shape odile-verify
        # reads. `vcpus` is absent because the Servury report does not carry it.
        self.assertEqual(
            list(served),
            ["available", "guest", "measurement", "report_version", "report_size",
             "fetched_at", "report_b64", "note", "release", "cmdline"],
        )
        self.assertIs(served["available"], True)
        self.assertEqual(served["guest"], "sev-snp")
        self.assertEqual(served["measurement"], FIXTURE_DOCUMENT["measurement"])
        self.assertEqual(served["report_version"], 5)
        self.assertEqual(served["report_size"], 1184)
        self.assertEqual(served["fetched_at"], BOOT_AT)
        self.assertEqual(served["release"], RELEASE)
        self.assertEqual(served["cmdline"], CMDLINE)
        self.assertIn(NOT_LIVENESS, served["note"])
        self.assertIn("does not measure the app layer", served["note"])

    def test_the_measurement_and_report_bytes_equal_the_input(self):
        served = ATTEST_MODULE.transform_boot_report(boot_document(), BOOT_AT)
        raw = base64.b64decode(served["report_b64"], validate=True)
        self.assertEqual(raw, base64.b64decode(FIXTURE_DOCUMENT["report_b64"]))
        self.assertEqual(len(raw), 1184)
        self.assertEqual(served["measurement"], raw[0x90:0xC0].hex())
        self.assertEqual(served["report_version"], int.from_bytes(raw[0:4], "little"))
        self.assertEqual(served["report_size"], len(raw))

    def test_a_non_ok_status_is_unavailable_with_a_reason(self):
        served = ATTEST_MODULE.transform_boot_report(boot_document(status="error"), BOOT_AT)
        self.assertIs(served["available"], False)
        self.assertIn("status", served["error"])
        self.assertIn("'error'", served["error"])
        self.assertNotIn("measurement", served)
        self.assertNotIn("report_b64", served)
        self.assertEqual(served["release"], RELEASE)

    def test_an_ok_status_without_report_bytes_is_unavailable(self):
        served = ATTEST_MODULE.transform_boot_report(boot_document(report_b64=""), BOOT_AT)
        self.assertIs(served["available"], False)
        self.assertIn("report_b64", served["error"])

    def test_malformed_base64_is_unavailable(self):
        served = ATTEST_MODULE.transform_boot_report(boot_document(report_b64="not base64!"), BOOT_AT)
        self.assertIs(served["available"], False)
        self.assertIn("base64", served["error"])

    def test_a_report_shorter_than_the_measurement_is_unavailable(self):
        short = base64.b64encode(b"\x05\x00\x00\x00").decode("ascii")
        served = ATTEST_MODULE.transform_boot_report(boot_document(report_b64=short), BOOT_AT)
        self.assertIs(served["available"], False)
        self.assertIn("shorter", served["error"])

    def test_an_empty_measurement_is_unavailable(self):
        raw = bytearray(base64.b64decode(FIXTURE_DOCUMENT["report_b64"]))
        raw[0x90:0xC0] = b"\x00" * 48
        served = ATTEST_MODULE.transform_boot_report(
            boot_document(report_b64=base64.b64encode(bytes(raw)).decode("ascii")), BOOT_AT)
        self.assertIs(served["available"], False)
        self.assertIn("empty measurement", served["error"])


class CommandLineTests(unittest.TestCase):
    def test_a_valid_boot_report_writes_a_0644_document_and_repeats(self):
        with tempfile.TemporaryDirectory(prefix="odl397-attest-") as tmp:
            source = Path(tmp) / "boot.json"
            out = Path(tmp) / "var" / "attestation.json"
            source.write_text(json.dumps(boot_document()), encoding="utf-8")
            stamp = 1794220000
            os.utime(source, (stamp, stamp))
            first = run_attest("--boot-report", str(source), str(out))
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o644)
            served = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(
                served["fetched_at"],
                datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            before = out.read_bytes()
            second = run_attest("--boot-report", str(source), str(out))
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(out.read_bytes(), before)
            self.assertEqual(list(out.parent.glob(".*.tmp")), [])

    def test_an_explicit_boot_time_overrides_the_mtime(self):
        with tempfile.TemporaryDirectory(prefix="odl397-attest-") as tmp:
            source = Path(tmp) / "boot.json"
            out = Path(tmp) / "attestation.json"
            source.write_text(json.dumps(boot_document()), encoding="utf-8")
            result = run_attest("--boot-report", str(source), "--boot-time", BOOT_AT, str(out))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(out.read_text())["fetched_at"], BOOT_AT)

    def test_a_new_boot_report_refreshes_fetched_at_and_keeps_the_measurement(self):
        with tempfile.TemporaryDirectory(prefix="odl397-attest-") as tmp:
            source = Path(tmp) / "boot.json"
            out = Path(tmp) / "attestation.json"
            source.write_text(json.dumps(boot_document()), encoding="utf-8")
            first_stamp, second_stamp = 1794220000, 1794300000
            os.utime(source, (first_stamp, first_stamp))
            self.assertEqual(run_attest("--boot-report", str(source), str(out)).returncode, 0)
            first = json.loads(out.read_text(encoding="utf-8"))
            os.utime(source, (second_stamp, second_stamp))
            self.assertEqual(run_attest("--boot-report", str(source), str(out)).returncode, 0)
            second = json.loads(out.read_text(encoding="utf-8"))
            self.assertNotEqual(first["fetched_at"], second["fetched_at"])
            self.assertEqual(second["fetched_at"],
                             datetime.fromtimestamp(second_stamp, timezone.utc)
                             .strftime("%Y-%m-%dT%H:%M:%SZ"))
            self.assertEqual(first["measurement"], second["measurement"])
            self.assertEqual(first["report_b64"], second["report_b64"])

    def test_a_missing_boot_report_is_unavailable_naming_the_path(self):
        with tempfile.TemporaryDirectory(prefix="odl397-attest-") as tmp:
            source = Path(tmp) / "run/blackbox/attest.json"
            out = Path(tmp) / "attestation.json"
            result = run_attest("--boot-report", str(source), "--boot-time", BOOT_AT, str(out))
            self.assertEqual(result.returncode, 0, result.stderr)
            served = json.loads(out.read_text(encoding="utf-8"))
            self.assertIs(served["available"], False)
            self.assertIn(str(source), served["error"])
            self.assertEqual(served["fetched_at"], BOOT_AT)
            self.assertNotIn("report_b64", served)


class VerifyTests(unittest.TestCase):
    def test_the_served_document_passes_odile_verify(self):
        """The shape is not merely plausible: the real verifier reads it."""
        with tempfile.TemporaryDirectory(prefix="odl397-verify-") as tmp:
            served_path = Path(tmp) / "attestation.json"
            record_path = Path(tmp) / "boot-record.json"
            served_path.write_text(
                json.dumps(ATTEST_MODULE.transform_boot_report(boot_document(), BOOT_AT),
                           indent=2) + "\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [str(VERIFY), "--report", str(served_path),
                 "--expect", FIXTURE_DOCUMENT["measurement"], "--record", str(record_path)],
                capture_output=True, text=True, timeout=120, cwd=ROOT,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS", result.stdout)
            self.assertIn(NOT_LIVENESS, result.stdout)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["release"], RELEASE)
            self.assertEqual(record["measurement"], FIXTURE_DOCUMENT["measurement"])
            self.assertIsNotNone(record["cmdline_sha256"])


class InstallerTests(unittest.TestCase):
    def test_the_tenant_layout_names_the_servury_boot_report(self):
        layout = INSTALLER_MODULE.Layout.tenant(
            user="odile", webapp_port=9120,
            public_url="https://odl-397.odile.fyi",
        )
        self.assertEqual(str(layout.boot_report), "/run/blackbox/attest.json")
        self.assertEqual(str(layout.served_attestation), "/var/lib/odile/attestation.json")
        self.assertEqual(str(layout.attest_source), "/usr/local/lib/odile/attest.py")

    def test_the_installer_carries_attest_py_byte_for_byte(self):
        embedded = INSTALLER_MODULE.ATTEST_SOURCE.encode("utf-8")
        self.assertEqual(embedded, ATTEST.read_bytes())
        self.assertEqual(hashlib.sha256(embedded).hexdigest(), INSTALLER_MODULE.ATTEST_SHA256)

    def test_the_scratch_install_writes_the_served_document_and_repeats(self):
        with tempfile.TemporaryDirectory(prefix="odl397-install-") as tmp:
            user = pwd.getpwuid(os.geteuid()).pw_name
            layout = INSTALLER_MODULE.Layout.scratch(
                root=Path(tmp) / "root", user=user, webapp_port=9140,
                public_url="http://odl-397.test:9140",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                INSTALLER_MODULE.ensure_dirs(layout, False)
                INSTALLER_MODULE.ensure_attest_source(layout, False)
            layout.boot_report.parent.mkdir(parents=True, exist_ok=True)
            layout.boot_report.write_text(json.dumps(boot_document()), encoding="utf-8")
            stamp = 1794220000
            os.utime(layout.boot_report, (stamp, stamp))

            with contextlib.redirect_stdout(io.StringIO()):
                first = INSTALLER_MODULE.ensure_scratch_attest(layout, False)
            self.assertIs(first, True)
            self.assertEqual(stat.S_IMODE(layout.served_attestation.stat().st_mode), 0o644)
            served = json.loads(layout.served_attestation.read_text(encoding="utf-8"))
            self.assertIs(served["available"], True)
            self.assertEqual(served["measurement"], FIXTURE_DOCUMENT["measurement"])
            self.assertEqual(served["report_size"], 1184)
            self.assertEqual(
                served["fetched_at"],
                datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            before = layout.served_attestation.read_bytes()

            with contextlib.redirect_stdout(io.StringIO()):
                again = INSTALLER_MODULE.ensure_scratch_attest(layout, False)
            self.assertIs(again, False)
            self.assertEqual(layout.served_attestation.read_bytes(), before)
            self.assertEqual(list(layout.var.glob("*.probe")), [])

    def test_the_installer_writes_the_transformer_oneshot_and_route(self):
        with tempfile.TemporaryDirectory(prefix="odl397-install-") as tmp:
            user = pwd.getpwuid(os.geteuid()).pw_name
            layout = INSTALLER_MODULE.Layout.scratch(
                root=Path(tmp) / "root", user=user, webapp_port=9140,
                public_url="http://odl-397.test:9140",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                INSTALLER_MODULE.ensure_dirs(layout, False)
                first = INSTALLER_MODULE.ensure_attest_source(layout, False)
                first |= INSTALLER_MODULE.ensure_units(layout, "odl-397", False)
                first |= INSTALLER_MODULE.ensure_caddy_site(layout, "odl-397", False)
            self.assertIs(first, True)

            source = layout.attest_source
            self.assertTrue(source.is_file())
            self.assertEqual(source.read_bytes(), ATTEST.read_bytes())
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(layout.var.stat().st_mode), 0o755)

            unit = (layout.unit_dir / "odile-attest-boot.service").read_text(encoding="utf-8")
            self.assertIn("Type=oneshot", unit)
            self.assertIn("RemainAfterExit=yes", unit)
            self.assertIn(
                f"ExecStart=/usr/bin/python3 {source} --boot-report "
                f"{layout.boot_report} {layout.served_attestation}",
                unit,
            )
            self.assertIn(f"ReadWritePaths={layout.var}", unit)
            self.assertNotIn("User=", unit)

            caddy = layout.caddyfile.read_text(encoding="utf-8")
            self.assertIn("# BEGIN odile-odl-397", caddy)
            self.assertIn("handle /attestation {", caddy)
            self.assertIn('header Cache-Control "no-store"', caddy)
            self.assertIn('header Content-Type "application/json"', caddy)
            self.assertIn("rewrite * /attestation.json", caddy)
            self.assertIn(f"root * {layout.var}", caddy)
            self.assertEqual(caddy.count("file_server"), 1)
            self.assertEqual(caddy.count("reverse_proxy"), 1)

            # The installer is idempotent: the same inputs change nothing.
            with contextlib.redirect_stdout(io.StringIO()):
                again = INSTALLER_MODULE.ensure_attest_source(layout, False)
                again |= INSTALLER_MODULE.ensure_units(layout, "odl-397", False)
                again |= INSTALLER_MODULE.ensure_caddy_site(layout, "odl-397", False)
            self.assertIs(again, False)


if __name__ == "__main__":
    unittest.main()
