#!/usr/bin/python3
"""Tests for the image bench's host-side checks and parsers.

These are small tests: no QEMU, no root and no network beyond a loopback stub
that speaks the measured Hermes gate (302 for anonymous HTML, 401 for anonymous
API, 200 for a valid session). The stub exists so the checker is tested against
behaviour, not against a mocked function call. The QEMU and podman runs are
exercised by the card's evidence, not here.
"""

from __future__ import annotations

import contextlib
import http.server
import io
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "image"))

import bench  # noqa: E402


class QuietServer(http.server.ThreadingHTTPServer):
    """A test server that does not print a traceback when a client hangs up.

    The retry tests deliberately abandon a slow connection; the server's write
    then fails with BrokenPipeError, which is expected here.
    """

    def handle_error(self, request, client_address):  # noqa: ANN001 - http.server API
        return


class GateStub(http.server.BaseHTTPRequestHandler):
    """The measured gate, in miniature: 302, 401 and 200, plus the WS ticket."""

    root_status = 302
    ws_status = 101

    def log_message(self, format, *args):  # noqa: A002 - http.server API
        return

    def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _upgrade(self, status: int) -> None:
        if status != 101:
            self._send(status, b'{"detail": "Forbidden"}')
            return
        self.wfile.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: stub\r\n\r\n"
        )
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        split = urllib.parse.urlsplit(self.path)
        path, query = split.path, urllib.parse.parse_qs(split.query)
        cookie = self.headers.get("Cookie", "")
        if path == "/login":
            self._send(200, b"<html>login</html>")
        elif path == "/api/sessions":
            self._send(401, b'{"detail": "Unauthorized"}')
        elif path == "/api/ws":
            ticketed = query.get("ticket") == ["stub-ticket"]
            self._upgrade(self.ws_status if ticketed else 403)
        elif path == "/":
            if self.root_status == 401:
                self._send(401, b'{"detail": "Unauthorized"}')
            elif "hermes_session=stub" in cookie:
                self._send(200, b'<html><title>Hermes</title><div id="root"></div></html>')
            else:
                self._send(302, b"", {"Location": "/login"})
        else:
            self._send(404, b"not found")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        path = urllib.parse.urlsplit(self.path).path
        cookie = self.headers.get("Cookie", "")
        if path == "/api/auth/ws-ticket":
            if "hermes_session=stub" in cookie:
                body = b'{"ticket": "stub-ticket", "ttl_seconds": 30}'
                self._send(200, body, {"Content-Type": "application/json"})
            else:
                self._send(401, b'{"detail": "Unauthorized"}')
            return
        if path != "/auth/password-login":
            self._send(404, b"not found")
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send(400, b"bad json")
            return
        if payload.get("username") == "tenant" and payload.get("password") == "bench-pw":
            self._send(200, b'{"ok": true}', {"Set-Cookie": "hermes_session=stub; Path=/; HttpOnly"})
        else:
            self._send(401, b'{"detail": "Invalid credentials"}')


class GateTests(unittest.TestCase):
    def start(self, root_status: int = 302, ws_status: int = 101):
        handler = type("Stub", (GateStub,), {"root_status": root_status, "ws_status": ws_status})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server.server_address[1]

    def test_the_measured_gate_passes(self) -> None:
        port = self.start()
        checks = bench.check_gate("127.0.0.1", port, "tenant", "bench-pw")
        self.assertEqual([check.name for check in checks], ["gate-anonymous", "gate-session"])
        self.assertTrue(checks[0].ok, checks[0].detail)
        self.assertTrue(checks[1].ok, checks[1].detail)
        self.assertIn("GET / -> 302", checks[0].detail)
        self.assertIn("GET /api/sessions -> 401", checks[0].detail)
        self.assertIn("GET / with cookie -> 200", checks[1].detail)
        self.assertIn("GET /api/ws -> 101", checks[1].detail)

    def test_a_session_without_a_working_websocket_is_not_a_pass(self) -> None:
        # The Webapp chat rides /api/ws; HTTP 200 with a dead socket is a
        # half-working gate. Pin the check to the 101.
        port = self.start(ws_status=403)
        _anonymous, session = bench.check_gate("127.0.0.1", port, "tenant", "bench-pw")
        self.assertFalse(session.ok)
        self.assertIn("GET /api/ws -> 403", session.detail)

    def test_a_401_on_root_is_not_the_gate(self) -> None:
        # The card body once said "401 without a password". The measured gate
        # answers 302 for anonymous HTML; a smoke that expected 401 would pass
        # the wrong thing. This test pins the checker to the measured shape.
        port = self.start(root_status=401)
        anonymous, _session = bench.check_gate("127.0.0.1", port, "tenant", "bench-pw")
        self.assertFalse(anonymous.ok)
        self.assertIn("GET / -> 401", anonymous.detail)

    def test_wrong_password_is_rejected(self) -> None:
        port = self.start()
        _anonymous, session = bench.check_gate("127.0.0.1", port, "tenant", "wrong")
        self.assertFalse(session.ok)

    def test_a_stalled_request_is_retried(self) -> None:
        # Measured in the QEMU guest: a GET / that had just answered 302
        # stalled on the next connection for more than 15 s. Under TCG that is
        # the bench, not the gate; the check retries and reports what the gate
        # said, and a request that never answers still fails.
        class Stalling(GateStub):
            stalled = False

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                if not type(self).stalled:
                    type(self).stalled = True
                    time.sleep(1.5)
                super().do_GET()

        server = QuietServer(("127.0.0.1", 0), Stalling)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        port = server.server_address[1]

        checks = bench.check_gate("127.0.0.1", port, "tenant", "bench-pw",
                                  timeout=0.5, attempts=3, pause=0.1)
        self.assertTrue(all(check.ok for check in checks), [check.detail for check in checks])

    def test_a_gate_that_never_answers_fails(self) -> None:
        class Dead(GateStub):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                time.sleep(1.5)

        server = QuietServer(("127.0.0.1", 0), Dead)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        with self.assertRaises(TimeoutError):
            bench.check_gate("127.0.0.1", server.server_address[1], "tenant", "bench-pw",
                             timeout=0.3, attempts=2, pause=0.1)


class GuestOutputTests(unittest.TestCase):
    def test_parse_five_passes(self) -> None:
        text = "\n".join(
            [f"ODILE-BENCH {name} PASS detail {name}" for name in bench.CHECK_NAMES]
            + ["ODILE-BENCH final PASS image-bench: PASS (5/5)",
               "image-bench: PASS (5/5)"]
        )
        checks, final = bench.parse_guest(text)
        self.assertTrue(final)
        self.assertEqual(set(checks), set(bench.CHECK_NAMES))
        self.assertTrue(all(check.ok for check in checks.values()))

    def test_parse_keeps_the_whole_detail(self) -> None:
        _checks, _final = bench.parse_guest("ODILE-BENCH cron PASS bench-heartbeat fired at 2026-11-07T20:00:00Z after 42s")
        self.assertEqual(_checks["cron"].detail, "bench-heartbeat fired at 2026-11-07T20:00:00Z after 42s")

    def test_missing_guest_check_fails_the_host_verdict(self) -> None:
        text = "\n".join(
            [f"ODILE-BENCH {name} PASS detail" for name in bench.CHECK_NAMES[:4]]
            + ["ODILE-BENCH final FAIL image-bench: FAIL (4/5)"]
        )
        guest, final = bench.parse_guest(text)
        host_gate = [bench.Check("gate-anonymous", True, "ok"), bench.Check("gate-session", True, "ok")]
        checks = bench.combined_checks(host_gate, guest, final)
        self.assertEqual([check.name for check in checks], list(bench.CHECK_NAMES))
        self.assertFalse(checks[-1].ok)
        self.assertIn("did not report", checks[-1].detail)

    def test_guest_failure_outvotes_a_local_pass(self) -> None:
        guest = {name: bench.Check(name, True, "ok") for name in bench.CHECK_NAMES}
        host_gate = [bench.Check("gate-anonymous", True, "ok"), bench.Check("gate-session", True, "ok")]
        checks = bench.combined_checks(host_gate, guest, False)
        self.assertFalse(all(check.ok for check in checks))


class VerdictTests(unittest.TestCase):
    def test_one_pass_line(self) -> None:
        checks = [bench.Check(name, True, "ok") for name in bench.CHECK_NAMES]
        self.assertEqual(bench.final_line(checks), "image-bench: PASS (5/5)")

    def test_one_fail_line_names_the_failure(self) -> None:
        checks = [bench.Check(name, name != "cron", "detail") for name in bench.CHECK_NAMES]
        line = bench.final_line(checks)
        self.assertTrue(line.startswith("image-bench: FAIL (4/5)"))
        self.assertIn("cron", line)


class ParserTests(unittest.TestCase):
    """Each mode must carry its own --port default.

    argparse's `parents=` shares action objects, so one subparser's
    `set_defaults(port=…)` rewrote the default for all of them. Measured in
    the QEMU guest: `local` printed port=19131 and waited 15 minutes on a
    port nothing listened on.
    """

    def test_each_mode_has_its_own_port_default(self) -> None:
        parser = bench.build_parser()
        self.assertEqual(parser.parse_args(["local"]).port, bench.WEBAPP_PORT)
        self.assertEqual(parser.parse_args(["image", "--disk", "x"]).port, 19130)
        self.assertEqual(parser.parse_args(["container", "--image", "x"]).port, 19131)

    def test_local_port_can_be_overridden(self) -> None:
        parser = bench.build_parser()
        self.assertEqual(parser.parse_args(["local", "--port", "9999"]).port, 9999)


class TemplateTests(unittest.TestCase):
    IMAGE = ROOT / "deploy" / "image"

    def test_the_tenant_config_pins_hermes_onboarding_off(self) -> None:
        # The bench serves the same first screen a tenant gets (ODL-387). Without
        # this block the Webapp opens Hermes's own onboarding chat (scripted
        # greeting and name card) on first load, before Odile's ritual.
        config = (self.IMAGE / "config.yaml").read_text(encoding="utf-8")
        self.assertIn('onboarding:\n  run: false\n  profile_build: "off"', config)

    def test_the_tenant_config_carries_every_block_the_installer_manages(self) -> None:
        # A bench built from this file is the profile a tenant gets, and its
        # Webapp is where an image is answered. Every block the installer owns
        # has to be here: ODL-341-F1 added `auxiliary`, and a bench that kept
        # the older set served a config whose vision lane rode the text model.
        # The count is read from the installer, not repeated here, so the next
        # block the installer gains fails this test instead of being forgotten.
        installer = (ROOT / "installer" / "odile_install.py").read_text(encoding="utf-8")
        managed = re.search(r"MANAGED_KEYS = \((.*?)\)", installer, re.S)
        assert managed is not None, "MANAGED_KEYS is not where this test expects it"
        keys = re.findall(r'"([a-z_]+)"', managed.group(1))
        self.assertEqual(len(keys), 7, f"the installer's managed set moved: {keys}")
        config = (self.IMAGE / "config.yaml").read_text(encoding="utf-8")
        for key in keys:
            self.assertIn(f"\n{key}:\n", config, f"the bench config has no {key} block")

    def test_the_tenant_config_pins_the_installers_vision_model(self) -> None:
        # The pin is one constant: `vision_analyze` describes an image with
        # THIS model inside NEAR AI's Private TEE tier. Without the block the
        # lane resolves to `model.default` and the bench answers an image with
        # the text model (empty at this token budget).
        installer = (ROOT / "installer" / "odile_install.py").read_text(encoding="utf-8")
        pin = re.search(r'^VISION_MODEL = "([^"]+)"', installer, re.M)
        assert pin is not None, "VISION_MODEL is not where this test expects it"
        config = (self.IMAGE / "config.yaml").read_text(encoding="utf-8")
        self.assertIn(f'auxiliary:\n  vision:\n    model: "{pin.group(1)}"', config)

    def test_every_template_placeholder_has_a_substitution(self) -> None:
        # A raw template installed as a unit is a real failure mode: the first
        # image booted with ODILE_TENANT_USER=__TENANT_USER__ because the unit
        # was copied in by hand instead of through install-stack.sh.
        installer = (self.IMAGE / "install-stack.sh").read_text(encoding="utf-8")
        for name in ("tenant.service", "odile-bench-smoke.service",
                     "odile-bench-proxy.service", "odile-bench-proxy.socket"):
            text = (self.IMAGE / name).read_text(encoding="utf-8")
            for placeholder in sorted(set(re.findall(r"__[A-Z_]+__", text))):
                self.assertIn(f"s|{placeholder}|", installer,
                              f"{name}: {placeholder} has no substitution in install-stack.sh")

    def test_the_guest_smoke_runs_on_the_webapp_port_and_its_own_serial(self) -> None:
        unit = (self.IMAGE / "odile-bench-smoke.service").read_text(encoding="utf-8")
        self.assertIn("smoke.sh local --guest", unit)
        self.assertIn("--port __WEBAPP_PORT__", unit)
        # ttyS0 belongs to getty; a unit writing there lost everything after
        # its first line once agetty took the console (measured 2026-11-08).
        self.assertIn("StandardOutput=append:/dev/ttyS1", unit)
        self.assertNotIn("append:/dev/ttyS0", unit)

    def test_the_proxy_is_not_rate_limited(self) -> None:
        # One proxyd per connection; the default 5-starts/10 s limit made the
        # host's 1 s gate poll hang on an accepted-but-unserved connection.
        unit = (self.IMAGE / "odile-bench-proxy.service").read_text(encoding="utf-8")
        self.assertIn("StartLimitIntervalSec=0", unit)
        self.assertIn("StartLimitBurst=0", unit)

    def test_the_uki_describes_the_image_not_the_builder(self) -> None:
        # ukify embeds the running host's /etc/os-release by default; a Debian
        # image built on an Ubuntu host produced a UKI that said Ubuntu.
        mkdisk = (self.IMAGE / "mkdisk.sh").read_text(encoding="utf-8")
        uki = (self.IMAGE / "uki.sh").read_text(encoding="utf-8")
        self.assertIn('cp "$MNT/etc/os-release" "$BOOT/os-release"', mkdisk)
        self.assertIn('--os-release "@$OSREL_FILE"', uki)

    def test_mkdisk_never_propagates_an_unmount_to_the_host(self) -> None:
        # A shared rbind plus `umount -R` took the host's /sys/fs/cgroup and
        # /dev nodes on 2026-11-08. The copy must be slave, and cleanup must
        # fail loudly if the host mounts did not survive.
        script = (self.IMAGE / "mkdisk.sh").read_text(encoding="utf-8")
        for path in ("$MNT/dev", "$MNT/sys"):
            self.assertIn("mount --rbind", script)
            self.assertIn(f'mount --make-rslave "{path}"', script,
                          f"{path}: rbind without --make-rslave")
        self.assertIn("cgroup2 /sys/fs/cgroup", script)
        self.assertIn("HOST DAMAGE", script)


class QemuCommandTests(unittest.TestCase):
    def test_the_vm_has_two_serial_channels(self) -> None:
        from unittest import mock

        captured: dict[str, list[str]] = {}

        def fake_popen(command, **kwargs):  # noqa: ANN001 - subprocess API
            captured["command"] = command
            return mock.MagicMock()

        with mock.patch.object(bench.subprocess, "Popen", side_effect=fake_popen):
            bench.boot_qemu(pathlib.Path("/var/tmp/x.raw"), pathlib.Path("/var/tmp/boot.log"),
                            pathlib.Path("/var/tmp/bench.log"), 19130, 512, 1)
        command = captured["command"]
        serials = [command[i + 1] for i, value in enumerate(command) if value == "-serial"]
        self.assertEqual(serials, ["file:/var/tmp/boot.log", "file:/var/tmp/bench.log"])

    def test_a_disk_on_tmpfs_warns_and_names_var_tmp(self) -> None:
        # ODL-335: /tmp is a 10 GB tmpfs, so a raw image there is host memory,
        # not disk, and `df /` never shows it. A 7.58 GB image in /tmp plus an
        # 8 GB QEMU guest took this host into global OOM three times on
        # 2026-11-12; the warning has to say where the image belongs.
        from unittest import mock

        with mock.patch.object(bench.subprocess, "Popen", return_value=mock.MagicMock()):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                bench.boot_qemu(pathlib.Path("/tmp/x.raw"), pathlib.Path("/var/tmp/boot.log"),
                                pathlib.Path("/var/tmp/bench.log"), 19130, 512, 1)
        warning = err.getvalue()
        self.assertIn("/tmp/x.raw", warning)
        self.assertIn("/var/tmp", warning)
        self.assertEqual(len(warning.strip().splitlines()), 1, warning)
        self.assertNotIn("boot.log", warning, "a /var/tmp path must not warn")

    def test_a_var_tmp_bench_is_silent(self) -> None:
        from unittest import mock

        with mock.patch.object(bench.subprocess, "Popen", return_value=mock.MagicMock()):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                bench.boot_qemu(pathlib.Path("/var/tmp/x.raw"), pathlib.Path("/var/tmp/boot.log"),
                                pathlib.Path("/var/tmp/bench.log"), 19130, 512, 1)
        self.assertEqual(err.getvalue(), "")


class ScriptTests(unittest.TestCase):
    def test_mkdisk_dry_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "out"
            result = subprocess.run(
                ["bash", str(ROOT / "deploy" / "image" / "mkdisk.sh"), "--dry-run", "--out", str(out)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("would", result.stdout)
            self.assertFalse(out.exists(), "mkdisk --dry-run created its output directory")

    def test_uki_dry_run_names_the_firmware_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            boot = pathlib.Path(tmp) / "boot"
            boot.mkdir()
            for name in ("vmlinuz", "initrd.img"):
                (boot / name).write_bytes(b"x")
            (boot / "cmdline").write_text("root=/dev/vda2 ro\n")
            (boot / "uname").write_text("6.12.0-test\n")
            result = subprocess.run(
                ["bash", str(ROOT / "deploy" / "image" / "uki.sh"), "--dry-run", "--in", tmp],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("8731846c", result.stdout)
            self.assertIn("sev-snp-measure --mode snp", result.stdout)


if __name__ == "__main__":
    unittest.main()
