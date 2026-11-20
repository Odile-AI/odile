"""The UKI cycle is a sequence, so its tests assert the sequence, not the lines.

`tools/uki_cycle.py` is the wiring between three claims: the published bytes
are the ones the panel holds, the machine booted the stage under test, and the
measurement it booted equals the one the published UKI produces. Each test
replaces the two tools with small scripts in a temp directory, so no test
touches the network, Servury or a machine — the sequence and the evidence file
are what is asserted.

The `--fresh` test on `tools/servury_cycle.py` exists because a buffer that
still holds a previous boot made `--wait-for` match before the restart: the
cycle passed without booting. The order is now log-off (empty), log-on, power,
poll.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import servury_cycle  # noqa: E402
import uki_cycle  # noqa: E402

MEASUREMENT = "ab" * 48
OTHER = "cd" * 48
# A fixture server id, not a live one: the live id is in
# /root/.odile/servury_uki_server_id and never in the tree.
SERVER = "a1b2c3d4"

PUBLISH_STUB = '''
import hashlib, sys
from pathlib import Path
args = sys.argv[1:]
uki = Path(args[args.index("--uki") + 1])
digest = hashlib.sha256(uki.read_bytes()).hexdigest()
print("same      %s (already published)" % uki)
print("verify    https://example.test/releases/uki/%s.efi serves the same bytes" % digest[:16])
print()
print("URL      https://example.test/releases/uki/%s.efi" % digest[:16])
print("SHA-256  %s" % digest)
'''

CYCLE_STUB = '''
import sys
from pathlib import Path
args = sys.argv[1:]
out = Path(args[args.index("--serial-out") + 1])
sha = args[args.index("--image-sha256") + 1]
marker = out.with_name("cycle-ran")
marker.write_text("yes")
out.parent.mkdir(parents=True, exist_ok=True)
out.with_name("cycle-args.txt").write_text("\\n".join(args))
out.write_text(
    "# servury_cycle 0badc0de action=restart\\n"
    "# image_sha256 %s\\n\\n"
    "[    1.0] Kernel command line: bb.host=odl-fake01\\n"
    "[    2.0] bb-sealed-unlock: root_crypt opened with the SNP-derived key\\n"
    "[   45.0] odile-release-setup.sh: measurement %s\\n" % (sha, "%MEASUREMENT%")
)
print("transcript %s" % out)
'''

SILENT_CYCLE_STUB = '''
import sys
from pathlib import Path
args = sys.argv[1:]
out = Path(args[args.index("--serial-out") + 1])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("# servury_cycle 0badc0de action=restart\\n# stage never reached\\n")
print("transcript %s" % out)
sys.exit(1)
'''

PANEL_STUB = '''
import sys
from pathlib import Path
Path(r"%MARKER%").write_text("\\n".join(sys.argv[1:]))
print("panel: set (stub)")
'''


class ParseTests(unittest.TestCase):
    def test_parse_publish_reads_the_pair(self) -> None:
        stdout = (
            "verify    https://example.test/releases/uki/0123456789abcdef.efi serves the same bytes\n"
            "\n"
            "URL      https://example.test/releases/uki/0123456789abcdef.efi\n"
            "SHA-256  0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"
        )

        url, digest = uki_cycle.parse_publish(stdout)

        self.assertEqual(url, "https://example.test/releases/uki/0123456789abcdef.efi")
        self.assertEqual(digest, "0123456789abcdef" * 4)

    def test_parse_publish_rejects_output_without_a_pair(self) -> None:
        with self.assertRaisesRegex(ValueError, "no URL/SHA-256 pair"):
            uki_cycle.parse_publish("write /srv/odile-releases/uki/x.efi\n")

    def test_panel_block_names_the_pair_it_asks_a_human_to_save(self) -> None:
        block = uki_cycle.panel_block("ODL-1", "https://example.test/x.efi", MEASUREMENT, Path("x.efi"), "2026-01-01T00:00:00Z")

        self.assertIn("https://example.test/x.efi", block)
        self.assertIn(MEASUREMENT, block)
        self.assertIn("leave empty", block)


class MeasurementTests(unittest.TestCase):
    def test_the_last_boot_in_the_buffer_wins(self) -> None:
        text = "measurement " + "11" * 48 + "\nreboot\nmeasurement " + MEASUREMENT + "\n"

        self.assertEqual(uki_cycle.measurement_from_transcript(text), MEASUREMENT)

    def test_a_transcript_without_a_measurement_returns_none(self) -> None:
        self.assertIsNone(uki_cycle.measurement_from_transcript("bb-sealed-unlock: opened\n"))

    def test_attestation_json_carries_the_launch_digest(self) -> None:
        self.assertEqual(
            uki_cycle.measurement_from_attestation({"available": True, "measurement": MEASUREMENT}),
            MEASUREMENT,
        )
        self.assertIsNone(uki_cycle.measurement_from_attestation({"available": False, "error": "no report"}))
        self.assertIsNone(uki_cycle.measurement_from_attestation({"measurement": "not-hex"}))

    def test_attest_passes_only_when_every_value_that_exists_agrees(self) -> None:
        passed, detail = uki_cycle.attest(MEASUREMENT, MEASUREMENT, MEASUREMENT)
        self.assertTrue(passed, detail)

        passed, detail = uki_cycle.attest(MEASUREMENT, MEASUREMENT, OTHER)
        self.assertFalse(passed)
        self.assertIn("live", detail)

        passed, _ = uki_cycle.attest(None, MEASUREMENT, MEASUREMENT)
        self.assertTrue(passed)

        passed, _ = uki_cycle.attest(None, MEASUREMENT, OTHER)
        self.assertFalse(passed)

        passed, detail = uki_cycle.attest(None, None, None)
        self.assertFalse(passed)
        self.assertIn("no measurement", detail)

        # An expected value is not an observation: nothing booted was read.
        passed, detail = uki_cycle.attest(MEASUREMENT, None, None)
        self.assertFalse(passed)
        self.assertIn("no measurement", detail)


class CycleSequenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.uki = self.dir / "odile-tenant.efi"
        self.uki.write_bytes(b"a ukernel image")
        self.publish = self.write_tool("publish_stub.py", PUBLISH_STUB)
        self.cycle = self.write_tool("cycle_stub.py", SILENT_CYCLE_STUB)
        self.panel_marker = self.dir / "panel-called.txt"
        self.panel = self.write_tool("panel_stub.py", PANEL_STUB.replace("%MARKER%", str(self.panel_marker)))
        self.evidence = self.dir / "evidence"

    def write_tool(self, name: str, body: str) -> Path:
        path = self.dir / name
        path.write_text(body, encoding="utf-8")
        return path

    def run_main(self, *extra: str, live: bool = False,
                 given: str | None = MEASUREMENT) -> tuple[int, str]:
        argv = [
            "--uki", str(self.uki),
            "--card", "t_test",
            "--server-id", "0badc0de",
            "--evidence-dir", str(self.evidence),
            "--publish-tool", str(self.publish),
            "--cycle-tool", str(self.cycle),
            "--panel-tool", str(self.panel),
        ]
        if given:
            argv += ["--expected-measurement", given]
        if not live:
            argv.append("--no-attest-url")
        argv += list(extra)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = uki_cycle.main(argv)
        return code, buffer.getvalue()

    def test_no_panel_stops_before_the_panel_or_the_machine(self) -> None:
        code, output = self.run_main("--no-panel")

        self.assertEqual(code, 0, output)
        digest = hashlib.sha256(self.uki.read_bytes()).hexdigest()
        panel = self.evidence / f"panel-{digest[:16]}.md"
        self.assertTrue(panel.is_file(), output)
        self.assertIn(digest, panel.read_text())
        self.assertIn("not touched", output)
        self.assertFalse(self.panel_marker.exists())
        self.assertFalse((self.evidence / "cycle-ran").exists())

    def test_the_default_run_hands_the_boot_image_to_the_cycle_then_attests(self) -> None:
        self.cycle = self.write_tool("cycle_stub_ok.py", CYCLE_STUB.replace("%MEASUREMENT%", MEASUREMENT))

        code, output = self.run_main()

        self.assertEqual(code, 0, output)
        digest = hashlib.sha256(self.uki.read_bytes()).hexdigest()
        # The boot image is no longer a separate panel call: the cycle tool
        # sets it (API where a key answers, panel otherwise) before powering on.
        args = (self.evidence / "cycle-args.txt").read_text().splitlines()
        self.assertIn("--boot-image", args)
        self.assertIn(f"https://example.test/releases/uki/{digest[:16]}.efi", args)
        self.assertIn("--boot-image-sha256", args)
        self.assertIn(digest, args)
        self.assertIn("--via", args)
        self.assertNotIn("--panel-done", args)
        self.assertNotIn("--keep-console", args, "the cycle disarms the log by default")
        cycles = list(self.evidence.glob(f"cycle-{digest[:16]}-*.md"))
        self.assertEqual(len(cycles), 1, output)
        evidence = cycles[0].read_text()
        self.assertIn(digest, evidence)
        self.assertIn("bb-sealed-unlock", evidence)
        self.assertIn(MEASUREMENT, evidence)
        self.assertIn("attest: **PASS**", evidence)
        self.assertIn("set by the cycle tool", evidence)
        self.assertEqual(len(list(self.evidence.glob("boot-*.txt"))), 1)

    def test_the_evidence_names_the_panel_path_when_the_cycle_took_it(self) -> None:
        # The cycle tool prints the path it chose; the evidence quotes it.
        body = CYCLE_STUB.replace("%MEASUREMENT%", MEASUREMENT).replace(
            'print("transcript %s" % out)',
            'print("via         the key does not answer for 0badc0de (Server not found): using the panel")\n'
            'print("transcript %s" % out)')
        self.cycle = self.write_tool("cycle_stub_panel.py", body)

        code, output = self.run_main()

        self.assertEqual(code, 0, output)
        digest = hashlib.sha256(self.uki.read_bytes()).hexdigest()
        evidence = next(self.evidence.glob(f"cycle-{digest[:16]}-*.md")).read_text()
        self.assertIn("set by the panel path", evidence)

    def test_a_panel_dry_run_submits_nothing_and_does_not_cycle(self) -> None:
        code, output = self.run_main("--panel-dry-run")

        self.assertEqual(code, 0, output)
        panel_call = self.panel_marker.read_text()
        self.assertIn("--set-boot-image", panel_call)
        self.assertNotIn("--apply", panel_call, "without --apply the fenced tool is a dry run")
        self.assertFalse((self.evidence / "cycle-ran").exists())

    def test_panel_done_cycles_without_touching_the_panel(self) -> None:
        self.cycle = self.write_tool("cycle_stub_ok.py", CYCLE_STUB.replace("%MEASUREMENT%", MEASUREMENT))

        code, output = self.run_main("--panel-done")

        self.assertEqual(code, 0, output)
        self.assertFalse(self.panel_marker.exists(), output)
        digest = hashlib.sha256(self.uki.read_bytes()).hexdigest()
        evidence = next(self.evidence.glob(f"cycle-{digest[:16]}-*.md")).read_text()
        self.assertIn("--panel-done", evidence)

    def test_the_cycle_waits_for_the_unlock_and_for_the_measurement(self) -> None:
        # The unlock prints at ~2 s and the measurement at ~45 s; a wait on the
        # unlock alone captures the transcript before the number exists.
        self.cycle = self.write_tool("cycle_stub_ok.py", CYCLE_STUB.replace("%MEASUREMENT%", MEASUREMENT))

        code, output = self.run_main("--panel-done")

        self.assertEqual(code, 0, output)
        args = (self.evidence / "cycle-args.txt").read_text().splitlines()
        waits = [args[index + 1] for index, value in enumerate(args) if value == "--wait-for"]
        self.assertEqual(len(waits), 2, args)
        self.assertIn("bb-sealed-unlock", waits[0])
        self.assertIn("measurement", waits[1])

    def test_a_boot_that_never_reached_the_stage_fails_loudly(self) -> None:
        code, output = self.run_main()

        self.assertEqual(code, 1, output)
        digest = hashlib.sha256(self.uki.read_bytes()).hexdigest()
        evidence = next(self.evidence.glob(f"cycle-{digest[:16]}-*.md")).read_text()
        self.assertIn("attest: **FAIL**", evidence)
        self.assertIn("never matched", evidence)

    def test_keep_console_is_handed_to_the_cycle(self) -> None:
        self.cycle = self.write_tool("cycle_stub_ok.py", CYCLE_STUB.replace("%MEASUREMENT%", MEASUREMENT))

        code, output = self.run_main("--keep-console")

        self.assertEqual(code, 0, output)
        args = (self.evidence / "cycle-args.txt").read_text().splitlines()
        self.assertIn("--keep-console", args)

    def test_a_missing_uki_is_bad_input(self) -> None:
        self.uki.unlink()

        code, output = self.run_main()

        self.assertEqual(code, 2, output)
        self.assertIn("no such file", output)

    def test_a_converged_boot_is_accepted_and_judged_on_the_live_attestation(self) -> None:
        # The captured converged boot: the unlock, the converged line, and no
        # measurement anywhere in it. The lane must accept that boot and take
        # the measurement from the machine's own /attestation.
        self.cycle = self.write_tool("cycle_converged.py", CONVERGED_CYCLE_STUB)

        with mock.patch.object(uki_cycle, "fetch_json",
                               return_value=({"available": True, "measurement": MEASUREMENT}, "")) as fetch:
            code, output = self.run_main(live=True)

        self.assertEqual(code, 0, output)
        self.assertEqual(fetch.call_args.args[0], "https://odlukitest.odile.fyi/attestation")
        evidence = next(self.evidence.glob("cycle-*.md")).read_text()
        self.assertIn("attest: **PASS**", evidence)
        self.assertIn("matched by live", evidence)
        self.assertIn("(none printed)", evidence, "the boot printed no measurement, and the evidence says so")

    def test_the_attest_url_can_come_from_the_environment(self) -> None:
        self.cycle = self.write_tool("cycle_converged.py", CONVERGED_CYCLE_STUB)

        with mock.patch.dict(os.environ, {"ODILE_ATTEST_URL": "http://127.0.0.1:19099/attestation"}), \
                mock.patch.object(uki_cycle, "fetch_json",
                                  return_value=({"available": True, "measurement": MEASUREMENT}, "")) as fetch:
            code, output = self.run_main(live=True)

        self.assertEqual(code, 0, output)
        self.assertEqual(fetch.call_args.args[0], "http://127.0.0.1:19099/attestation")

    def test_the_expected_value_is_recomputed_over_the_boots_command_line(self) -> None:
        self.cycle = self.write_tool("cycle_converged.py", CONVERGED_CYCLE_STUB)
        appends: list[str] = []

        def fake_expected(uki, ovmf, *, vcpus, vcpu_type, guest_features,
                          ovmf_sha256="", append="") -> tuple[str | None, str]:
            appends.append(append)
            return (MEASUREMENT if append else OTHER), "sev-snp-measure over the published UKI"

        with mock.patch.object(uki_cycle, "expected_measurement", side_effect=fake_expected), \
                mock.patch.object(uki_cycle, "find_ovmf", return_value=Path("/ovmf")), \
                mock.patch.object(uki_cycle, "fetch_json",
                                  return_value=({"available": True, "measurement": MEASUREMENT}, "")):
            code, output = self.run_main(live=True, given=None)

        self.assertEqual(code, 0, output)
        self.assertEqual(appends[0], "", "the first computation is the plain one")
        self.assertTrue(appends[1].startswith("root=/dev/mapper/root_crypt"), appends)
        evidence = next(self.evidence.glob("cycle-*.md")).read_text()
        self.assertIn("as --append", evidence)


class FreshTranscriptTests(unittest.TestCase):
    def test_fresh_empties_the_transcript_before_arming_and_polling(self) -> None:
        calls: list[tuple[str, dict]] = []

        def fake_call(action, params=None, *, key, server_id=None, timeout=40.0):
            calls.append((action, params or {}))
            if action == "serial" and params and params.get("log") is False:
                return {"logging": False}
            if action == "serial" and params and params.get("log") is True:
                return {"logging": True}
            if action == "power":
                return {}
            return {"output": "bb-sealed-unlock: opened", "bytes": 1}

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("k", encoding="utf-8")
            out = Path(tmp) / "boot.txt"
            argv = [
                "--server-id", "0badc0de", "--action", "restart",
                "--serial-out", str(out), "--wait-for", "bb-sealed-unlock",
                "--timeout", "1", "--interval", "0", "--key-file", str(key), "--fresh",
                "--via", "api",
            ]
            with mock.patch.object(servury_cycle, "call", fake_call), \
                    mock.patch.object(servury_cycle.time, "sleep", lambda seconds: None):
                code = servury_cycle.main(argv)
            written = out.read_text()

        self.assertEqual(code, 0)
        self.assertEqual(calls[0], ("serial", {"log": False}))
        self.assertEqual(calls[1], ("serial", {"log": True}))
        self.assertEqual(calls[2][0], "power")
        self.assertIn("bb-sealed-unlock", written)

    def test_without_fresh_the_first_call_is_the_arming(self) -> None:
        calls: list[tuple[str, dict]] = []

        def fake_call(action, params=None, *, key, server_id=None, timeout=40.0):
            calls.append((action, params or {}))
            if action == "power":
                return {}
            if params and params.get("log") is True:
                return {"logging": True}
            return {"output": "bb-sealed-unlock: opened"}

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("k", encoding="utf-8")
            argv = [
                "--server-id", "0badc0de", "--serial-out", str(Path(tmp) / "b.txt"),
                "--wait-for", "bb-sealed-unlock", "--timeout", "1", "--interval", "0",
                "--key-file", str(key), "--via", "api",
            ]
            with mock.patch.object(servury_cycle, "call", fake_call), \
                    mock.patch.object(servury_cycle.time, "sleep", lambda seconds: None):
                self.assertEqual(servury_cycle.main(argv), 0)

        self.assertEqual(calls[0], ("serial", {"log": True}))


class ReachTests(unittest.TestCase):
    def test_every_pattern_must_be_present(self) -> None:
        text = "bb-sealed-unlock: opened\nmeasurement " + MEASUREMENT + "\n"

        self.assertTrue(servury_cycle.reached(text, ["bb-sealed-unlock"]))
        self.assertFalse(servury_cycle.reached(text, ["bb-sealed-unlock", "rc=0"]))
        self.assertTrue(servury_cycle.reached(text, ["bb-sealed-unlock", "measurement [0-9a-f]{96}"]))
        self.assertFalse(servury_cycle.reached("", ["bb-sealed-unlock"]))


class PowerTests(unittest.TestCase):
    def test_a_504_is_sent_not_failed(self) -> None:
        # The gateway in front of the API gives up at 30 s; the node has taken
        # the action. Treating the 504 as a failure aborted every restart.
        def fake_call(action, params=None, *, key, server_id=None, timeout=40.0):
            if action == "power":
                raise urllib.error.HTTPError("https://servury.com", 504, "Gateway Timeout", Message(), None)
            return {"logging": True, "output": "bb-sealed-unlock: opened\nmeasurement " + MEASUREMENT}

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("k", encoding="utf-8")
            argv = [
                "--server-id", "0badc0de", "--serial-out", str(Path(tmp) / "b.txt"),
                "--wait-for", "bb-sealed-unlock", "--timeout", "1", "--interval", "0",
                "--key-file", str(key), "--via", "api",
            ]
            buffer = io.StringIO()
            with mock.patch.object(servury_cycle, "call", fake_call), \
                    mock.patch.object(servury_cycle.time, "sleep", lambda seconds: None), \
                    contextlib.redirect_stdout(buffer):
                code = servury_cycle.main(argv)

        self.assertEqual(code, 0, buffer.getvalue())
        self.assertIn("504", buffer.getvalue())

    def test_another_http_error_is_a_failure(self) -> None:
        def fake_call(action, params=None, *, key, server_id=None, timeout=40.0):
            if action == "power":
                raise urllib.error.HTTPError("https://servury.com", 403, "Forbidden", Message(), None)
            return {"logging": True}

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("k", encoding="utf-8")
            argv = [
                "--server-id", "0badc0de", "--serial-out", str(Path(tmp) / "b.txt"),
                "--timeout", "1", "--interval", "0", "--key-file", str(key),
            ]
            with mock.patch.object(servury_cycle, "call", fake_call), \
                    mock.patch.object(servury_cycle.time, "sleep", lambda seconds: None):
                with self.assertRaises(urllib.error.HTTPError):
                    servury_cycle.main(argv)




class ServerIdTests(unittest.TestCase):
    """The live server id is untracked; the tool reads it at run time."""

    def test_the_default_comes_from_the_named_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "servury_uki_server_id"
            path.write_text(SERVER + "\n", encoding="utf-8")
            with mock.patch.object(uki_cycle, "SERVER_ID_FILE", path):
                self.assertEqual(uki_cycle.default_server_id(), SERVER)

    def test_a_missing_file_has_no_default(self) -> None:
        with mock.patch.object(uki_cycle, "SERVER_ID_FILE", Path("/nonexistent/odile-513-id")):
            self.assertEqual(uki_cycle.default_server_id(), "")

    def test_main_without_an_id_names_the_file_and_publishes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            uki = Path(tmp) / "odile.efi"
            uki.write_bytes(b"not really a UKI")
            with mock.patch.object(uki_cycle, "SERVER_ID_FILE", Path(tmp) / "servury_uki_server_id"):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    code = uki_cycle.main(["--uki", str(uki), "--card", "ODL-513"])
        self.assertEqual(code, 2)
        self.assertIn("servury_uki_server_id", err.getvalue())


class ViaTests(unittest.TestCase):
    def test_a_key_that_answers_keeps_the_api(self) -> None:
        with mock.patch.object(servury_cycle, "boot_image_state", return_value={"next_start": "custom"}):
            self.assertEqual(servury_cycle.choose_via("auto", "k", "0badc0de"), "api")

    def test_server_not_found_falls_back_to_the_panel(self) -> None:
        # Measured 12 November: the reseller key owns 0badc0de; the ukitest box
        # belongs to the account with a panel session and no key.
        with mock.patch.object(servury_cycle, "boot_image_state", side_effect=RuntimeError("Server not found.")):
            self.assertEqual(servury_cycle.choose_via("auto", "k", SERVER), "panel")

    def test_no_key_means_the_panel(self) -> None:
        self.assertEqual(servury_cycle.choose_via("auto", "", SERVER), "panel")

    def test_an_explicit_choice_is_not_probed(self) -> None:
        with mock.patch.object(servury_cycle, "boot_image_state", side_effect=AssertionError("probed")):
            self.assertEqual(servury_cycle.choose_via("api", "k", "x"), "api")
            self.assertEqual(servury_cycle.choose_via("panel", "k", "x"), "panel")


class BootImageApiTests(unittest.TestCase):
    def test_it_posts_the_pair_then_waits_for_the_download(self) -> None:
        calls: list[tuple[str, dict | None]] = []

        def fake_call(action, params=None, *, key, server_id=None, timeout=40.0):
            calls.append((action, params))
            if params is not None:
                return {"boot_image": {"download": {"state": "downloading"}}}
            return {"boot_image": {"next_start": "custom",
                                   "download": {"state": "ready", "kernel_sha256": MEASUREMENT}}}

        with mock.patch.object(servury_cycle, "call", fake_call), \
                mock.patch.object(servury_cycle.time, "sleep", lambda seconds: None):
            path = servury_cycle.set_boot_image_api(
                "k", "0badc0de", "https://example.test/x.efi", MEASUREMENT, timeout=10, interval=0)

        self.assertEqual(path, "api")
        self.assertEqual(calls[0], ("boot-image", {"url": "https://example.test/x.efi", "sha256": MEASUREMENT}))
        self.assertEqual(calls[1], ("boot-image", None))

    def test_a_download_that_never_becomes_ready_raises(self) -> None:
        def fake_call(action, params=None, *, key, server_id=None, timeout=40.0):
            return {"boot_image": {"download": {"state": "downloading"}}}

        with mock.patch.object(servury_cycle, "call", fake_call), \
                mock.patch.object(servury_cycle.time, "sleep", lambda seconds: None):
            with self.assertRaisesRegex(RuntimeError, "never reported download.state=ready"):
                servury_cycle.set_boot_image_api(
                    "k", "0badc0de", "https://example.test/x.efi", MEASUREMENT, timeout=0, interval=0)


class PanelFreshnessTests(unittest.TestCase):
    """The panel's log rotates at ~128 KiB; an old boot must not count."""

    OLD = ("Linux version 6.12.115 (old)\n[ 3.0] bb-sealed-unlock: opened\n"
           "[ 30.0] odile-release-setup 2026-11-12T18:00:00Z\n"
           "[ 30.5] measurement " + MEASUREMENT + "\n")
    NEW = ("Linux version 6.12.115 (new)\n[ 3.0] bb-sealed-unlock: opened\n"
           "[ 30.0] odile-release-setup 2026-11-12T19:30:00Z\n"
           "[ 30.5] measurement " + MEASUREMENT + "\n")

    def run_cycle(self, log: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("k", encoding="utf-8")
            out = Path(tmp) / "boot.txt"
            argv = [
                "--server-id", SERVER, "--serial-out", str(out),
                "--wait-for", "bb-sealed-unlock", "--wait-for", "measurement [0-9a-f]{96}",
                "--timeout", "1", "--interval", "0",
                "--key-file", str(key), "--via", "panel", "--fresh",
            ]
            with mock.patch.object(servury_cycle, "panel_log", return_value=log), \
                    mock.patch.object(servury_cycle, "panel_console",
                                      return_value=mock.Mock(returncode=0, stdout="", stderr="")), \
                    mock.patch.object(servury_cycle, "panel",
                                      return_value=mock.Mock(returncode=0, stdout="", stderr="")), \
                    mock.patch.object(servury_cycle.time, "sleep", lambda seconds: None):
                code = servury_cycle.main(argv)
            return code, out.read_text()

    def test_an_old_boot_in_the_buffer_does_not_count(self) -> None:
        code, written = self.run_cycle(self.OLD)

        self.assertEqual(code, 1, "a boot that was already in the buffer is not this boot")
        self.assertIn("earlier_boot_stamps 1", written)

    def test_a_new_boot_stamp_counts_and_is_the_only_text_written(self) -> None:
        # Before the power action the buffer held the old boot; after it, the
        # new boot rotated in. Both stamps are present in this fixture.
        def log_at_call() -> str:
            return self.OLD

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("k", encoding="utf-8")
            out = Path(tmp) / "boot.txt"
            argv = [
                "--server-id", SERVER, "--serial-out", str(out),
                "--wait-for", "bb-sealed-unlock", "--wait-for", "measurement [0-9a-f]{96}",
                "--timeout", "1", "--interval", "0",
                "--key-file", str(key), "--via", "panel", "--fresh",
            ]
            calls = {"n": 0}

            def fake_log(server: str) -> str:
                calls["n"] += 1
                return self.OLD if calls["n"] == 1 else self.NEW

            with mock.patch.object(servury_cycle, "panel_log", side_effect=fake_log), \
                    mock.patch.object(servury_cycle, "panel_console",
                                      return_value=mock.Mock(returncode=0, stdout="", stderr="")), \
                    mock.patch.object(servury_cycle, "panel",
                                      return_value=mock.Mock(returncode=0, stdout="", stderr="")), \
                    mock.patch.object(servury_cycle.time, "sleep", lambda seconds: None):
                code = servury_cycle.main(argv)
            written = out.read_text()

        self.assertEqual(code, 0, written)
        self.assertIn("Linux version 6.12.115 (new)", written)
        self.assertNotIn("(old)", written)


class ConsoleDisarmTests(unittest.TestCase):
    """A cycle arms the console log for one boot; it must disarm it afterwards.

    The panel says the reason in its own words: the hypervisor keeps everything
    the machine prints to its serial console as a *plaintext file on the host*,
    and the switch empties it. Measured 2026-11-19, the tool armed both machines
    and never disarmed either — the tenant box carried 209 KB, the bench 1,174 KB.
    So the switch going off is part of the sequence, after the transcript is
    written, whether the boot matched or timed out.
    """

    LOG = ("Linux version 6.12.115 (fixture)\n"
           "[    3.0] bb-sealed-unlock: root_crypt opened with the SNP-derived key\n"
           "[   30.0] odile-release-setup 2026-11-19T00:00:00Z\n"
           "[   30.5] measurement " + MEASUREMENT + "\n")
    # What the host held before this boot: no release-setup stamp, so nothing
    # in it can count as this boot's.
    EARLIER = "Linux version 6.12.115 (earlier)\n[    3.0] nothing yet\n"

    def run_cycle(self, *extra: str, log: str | None = None,
                  off: mock.Mock | None = None,
                  on_error: BaseException | None = None,
                  off_error: BaseException | None = None,
                  after_off: list[str | None] | None = None,
                  ) -> tuple[int | BaseException, list[tuple[str, str]], str]:
        """(exit code or the exception that escaped, [(console action, transcript at that moment)], output).

        `after_off` is what the host's copy holds on each read once the switch is
        off: "" is an empty file, a string is a tail, None is a read that failed.
        The last entry repeats, so ["tail"] is a tail that never clears.
        """
        events: list[tuple[str, str]] = []
        fixture = self.LOG if log is None else log
        reads_after_off = [0]
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("k", encoding="utf-8")
            out = Path(tmp) / "boot.txt"

            def fake_console(server: str, action: str) -> mock.Mock:
                events.append((action, "transcript" if out.is_file() else "absent"))
                if action == "on" and on_error is not None:
                    raise on_error
                if action == "off" and off_error is not None:
                    raise off_error
                if action == "off" and off is not None:
                    return off
                return mock.Mock(returncode=0, stdout="", stderr="")

            def fake_log(server: str) -> str | None:
                events.append(("read", "transcript" if out.is_file() else "absent"))
                # Once the switch is off the host's copy is empty — except for a
                # tail the still-printing boot landed as the switch applied.
                if any(action == "off" for action, _ in events):
                    if after_off:
                        at = min(reads_after_off[0], len(after_off) - 1)
                        reads_after_off[0] += 1
                        return after_off[at]
                    return ""
                # The first read is the --fresh baseline (the buffer before this
                # boot); every later read is the new boot.
                return self.EARLIER if len([a for a, _ in events if a == "read"]) == 1 else fixture

            argv = [
                "--server-id", SERVER, "--serial-out", str(out),
                "--wait-for", "bb-sealed-unlock", "--timeout", "1", "--interval", "0",
                "--key-file", str(key), "--via", "panel", "--fresh", *extra,
            ]
            buffer = io.StringIO()
            with mock.patch.object(servury_cycle, "panel_log", fake_log), \
                    mock.patch.object(servury_cycle, "panel_console", fake_console), \
                    mock.patch.object(servury_cycle, "panel",
                                      return_value=mock.Mock(returncode=0, stdout="", stderr="")), \
                    mock.patch.object(servury_cycle.time, "sleep", lambda seconds: None), \
                    contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                try:
                    outcome: int | BaseException = servury_cycle.main(argv)
                except BaseException as error:  # the arm or the disarm itself can raise
                    outcome = error
        return outcome, events, buffer.getvalue()

    def test_a_finished_cycle_turns_the_log_off_after_saving_the_transcript(self) -> None:
        code, events, output = self.run_cycle()

        self.assertEqual(code, 0, output)
        actions = [action for action, _ in events]
        self.assertEqual(actions[0], "on", events)
        self.assertIn("read", actions[1:], "the log is polled between arming and disarming")
        self.assertIn(("off", "transcript"), events,
                      "the transcript is written before the switch goes off")
        after_off = actions[actions.index("off"):]
        self.assertNotIn("on", after_off, events)
        self.assertEqual(actions[-1], "read", "the way out ends by reading the host's copy")
        self.assertIn("off; the host's transcript file is emptied", output)

    def test_keep_console_leaves_the_log_on(self) -> None:
        code, events, output = self.run_cycle("--keep-console")

        self.assertEqual(code, 0, output)
        self.assertNotIn("off", [action for action, _ in events], events)
        self.assertIn("--keep-console", output)

    def test_a_refused_disarm_is_one_line_and_keeps_the_exit_code(self) -> None:
        refused = mock.Mock(returncode=1, stdout="", stderr="panel: console log off refused: nope")

        code, events, output = self.run_cycle(off=refused)

        self.assertEqual(code, 0, output)
        self.assertEqual(events[-1][0], "off", events)
        self.assertEqual(output.count("disarm refused"), 1, output)
        self.assertIn("nope", output)

    def test_a_hung_arm_still_turns_the_log_off(self) -> None:
        # The panel wrapper runs subprocess.run(timeout=300): a hang raises before
        # any return code exists, and the switch may already have been posted.
        hung = subprocess.TimeoutExpired(["servury_panel.py", "--console"], 300)

        outcome, events, output = self.run_cycle(on_error=hung)

        self.assertIsInstance(outcome, subprocess.TimeoutExpired, output)
        actions = [action for action, _ in events]
        self.assertEqual(actions[0], "on", events)
        self.assertEqual(actions[1], "off", "the arm was attempted, so the way out disarms")
        self.assertTrue(all(action == "read" for action in actions[2:]), events)

    def test_a_tail_that_lands_as_the_switch_applies_is_emptied(self) -> None:
        # Measured 2026-11-19: a cycle matched mid-boot left 55 bytes of getty
        # banner on the host, because the switch lands a moment after the POST.
        # The disarm reads the copy back and a second off empties the tail, so
        # the line it prints ("emptied") is true rather than hopeful.
        outcome, events, output = self.run_cycle(
            after_off=["odl-340a1e login:", ""])

        self.assertEqual(outcome, 0, output)
        self.assertEqual([action for action, _ in events].count("off"), 2, events)
        self.assertIn("off; the host's transcript file is emptied", output)
        self.assertNotIn("still shows", output)

    def test_a_tail_that_lands_after_an_empty_read_is_emptied(self) -> None:
        # The measured failure of the single immediate read-back: the first read
        # after the off sees nothing, and 55 bytes of getty banner arrive 3–8 s
        # later. The settle window must see the tail, post another off, and only
        # then call the copy empty.
        outcome, events, output = self.run_cycle(
            after_off=["", "", "odl-340a1e login:", ""])

        self.assertEqual(outcome, 0, output)
        self.assertEqual([action for action, _ in events].count("off"), 2, events)
        self.assertIn("off; the host's transcript file is emptied", output)
        self.assertNotIn("still shows", output)

    def test_a_read_that_fails_is_not_reported_as_emptied(self) -> None:
        # panel_log returns None for a read that did not answer; "" is the only
        # answer that supports the word "emptied".
        code, events, output = self.run_cycle(after_off=[None])

        self.assertEqual(code, 0, output)
        self.assertNotIn("transcript file is emptied", output)
        self.assertIn("could not be read", output)
        self.assertIn("check it with --console --action read", output)

    def test_a_tail_that_never_clears_says_so_and_keeps_the_exit_code(self) -> None:
        code, events, output = self.run_cycle(after_off=["odl-340a1e login:"])

        self.assertEqual(code, 0, output)
        self.assertIn(f"still shows {len('odl-340a1e login:')} bytes", output)
        self.assertNotIn("transcript file is emptied", output)
        self.assertLessEqual([action for action, _ in events].count("off"), 3,
                             "the re-off is bounded, not a loop")

    def test_a_hung_disarm_is_one_line_and_keeps_the_exit_code(self) -> None:
        # The transcript matched, so the cycle's verdict is 0; a slow way out is
        # one line, not a traceback that throws the verdict away.
        hung = subprocess.TimeoutExpired(["servury_panel.py", "--console"], 300)

        outcome, events, output = self.run_cycle(off_error=hung)

        self.assertEqual(outcome, 0, output)
        self.assertEqual(events[-1][0], "off", events)
        self.assertEqual(output.count("disarm refused"), 1, output)
        self.assertIn("timed out after 300 seconds", output)

    def test_a_wait_for_timeout_still_turns_the_log_off(self) -> None:
        # The boot never reaches the stage under test, so the cycle exits 1 —
        # and still disarms: a timed-out cycle is exactly when nobody looks again.
        code, events, output = self.run_cycle(log="Linux version 6.12.115 (fixture)\n[ 3.0] nothing yet\n")

        self.assertEqual(code, 1, output)
        self.assertIn(("off", "transcript"), events,
                      "a timed-out cycle still disarms after writing its transcript")
        self.assertEqual(events[-1][0], "read", events)
        self.assertIn("never matched", output)


class ApiConsoleDisarmTests(unittest.TestCase):
    """The API path arms with `log: true`; it must disarm with `log: false`."""

    def run_api(self, *extra: str, arm_error: BaseException | None = None,
                after_off: list[str | None] | None = None,
                ) -> tuple[int | BaseException, list[tuple[str, dict | None]], str]:
        """(exit code or exception, calls, output).

        `after_off` is the bare GET's answer once the switch is off: "" empty,
        a string a tail, None a read that failed. The last entry repeats.
        """
        calls: list[tuple[str, dict | None]] = []
        armed = {"on": False}
        reads_after_off = [0]

        def fake_call(action, params=None, *, key, server_id=None, timeout=40.0):
            calls.append((action, params))
            if action == "power":
                return {}
            if params and params.get("log") is True:
                if arm_error is not None:
                    raise arm_error
                armed["on"] = True
                return {"logging": True}
            if params and params.get("log") is False:
                armed["on"] = False
                return {}
            # A bare GET: the transcript, or what the host's copy holds after the off.
            if not armed["on"]:
                if after_off:
                    at = min(reads_after_off[0], len(after_off) - 1)
                    reads_after_off[0] += 1
                    if after_off[at] is None:
                        raise RuntimeError("HTTP 502: the console did not answer")
                    return {"output": after_off[at]}
                return {"output": ""}
            return {"output": "bb-sealed-unlock: opened"}

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("k", encoding="utf-8")
            argv = [
                "--server-id", SERVER, "--serial-out", str(Path(tmp) / "boot.txt"),
                "--wait-for", "bb-sealed-unlock", "--timeout", "1", "--interval", "0",
                "--key-file", str(key), "--via", "api", *extra,
            ]
            buffer = io.StringIO()
            with mock.patch.object(servury_cycle, "call", fake_call), \
                    mock.patch.object(servury_cycle.time, "sleep", lambda seconds: None), \
                    contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                try:
                    outcome: int | BaseException = servury_cycle.main(argv)
                except BaseException as error:  # the arm itself can raise
                    outcome = error
        return outcome, calls, buffer.getvalue()

    def test_the_api_path_turns_logging_off_after_the_poll(self) -> None:
        code, calls, output = self.run_api()

        self.assertEqual(code, 0, output)
        self.assertIn(("serial", {"log": False}), calls,
                      "the write that empties the host's copy")
        self.assertEqual(calls[-1], ("serial", None),
                         "the way out ends by reading the host's copy")
        self.assertIn("transcript file is emptied", output)

    def test_keep_console_posts_no_log_false(self) -> None:
        code, calls, output = self.run_api("--keep-console")

        self.assertEqual(code, 0, output)
        self.assertNotIn(("serial", {"log": False}), calls)

    def test_a_tail_that_lands_as_the_switch_applies_is_emptied(self) -> None:
        # Same race as the panel path: the off lands after the POST, so the
        # second write is what empties the getty banner that arrived with it.
        outcome, calls, output = self.run_api(after_off=["odl-340a1e login:", ""])

        self.assertEqual(outcome, 0, output)
        self.assertEqual([call for call in calls if call == ("serial", {"log": False})], [
            ("serial", {"log": False}), ("serial", {"log": False})], calls)
        self.assertIn("transcript file is emptied", output)

    def test_a_tail_that_lands_after_an_empty_read_is_emptied(self) -> None:
        outcome, calls, output = self.run_api(after_off=["", "odl-340a1e login:", ""])

        self.assertEqual(outcome, 0, output)
        self.assertEqual([call for call in calls if call == ("serial", {"log": False})], [
            ("serial", {"log": False}), ("serial", {"log": False})], calls)
        self.assertIn("transcript file is emptied", output)

    def test_a_read_that_fails_is_not_reported_as_emptied(self) -> None:
        outcome, calls, output = self.run_api(after_off=[None])

        self.assertEqual(outcome, 0, output)
        self.assertNotIn("transcript file is emptied", output)
        self.assertIn("could not be read", output)

    def test_an_arm_that_raises_after_the_switch_still_disarms(self) -> None:
        # The gateway documents 504s at 30 s; the switch may have been applied
        # before the answer was lost, so a raising arm still owes an off.
        outcome, calls, output = self.run_api(arm_error=RuntimeError("HTTP 504 the node took it"))

        self.assertIsInstance(outcome, RuntimeError, calls)
        self.assertEqual(calls[0], ("serial", {"log": True}), calls)
        self.assertIn(("serial", {"log": False}), calls,
                      "the way out still empties the host's copy")


class LatestBootTests(unittest.TestCase):
    def test_the_last_kernel_start_anchors_the_boot(self) -> None:
        text = "junk\nLinux version old\na\nLinux version new\nb\n"

        self.assertTrue(servury_cycle.latest_boot(text).startswith("Linux version new"))

    def test_text_without_a_marker_is_returned_whole(self) -> None:
        self.assertEqual(servury_cycle.latest_boot("bb-sealed-unlock: opened"), "bb-sealed-unlock: opened")

    def test_release_stamps_are_read(self) -> None:
        text = "odile-release-setup 2026-11-12T19:30:00Z and odile-release-setup 2026-11-12T18:00:00Z"

        self.assertEqual(servury_cycle.release_times(text),
                         {"2026-11-12T19:30:00Z", "2026-11-12T18:00:00Z"})




# --- ODL-913's converged bench boots (t_638011f9) ---------------------------
# Two consecutive boots of the same converged machine, captured on
# <server:ukitest> (168020b4) on 2026-11-19; ODL-913's evidence bundle:
#   /var/tmp/odile-t_59ab6cc5-console/bench-cycle-boot.txt
#     sha256 e11ce2cdee44e1e43c4726ca8c663b204339955afe043c88368a419697ff3b47
#     the --fresh cycle that printed matched=False after 603 s
#   /var/tmp/odile-t_59ab6cc5-console/bench-cycle-boot-2.txt
#     sha256 11652b998b31fb4f0aaf3fb58f8307b309f76ab5a79ac48f3e10a2af7e4264b2
#     the same boot without --fresh, matched=True in 86 s
# The converged line carries the *same* setup stamp in both boots, and neither
# carries an `odile-release-setup <stamp>` line: a stamp cannot be the boot's
# identity. Each constant is the capture's latest boot cut at line boundaries;
# the two fields the leak gate forbids in a tree — the packager's email
# address in the kernel string and the per-machine addresses and token in the
# command line — are marked `[elided]`. Every line the checks read (the
# unlock, the converged line) is byte-identical to the capture, and
# `ConvergedFixtureTests` re-checks that against the bundle when it is on the
# host.
CONVERGED_EVIDENCE = Path("/var/tmp/odile-t_59ab6cc5-console")


BENCH_BOOT_1 = (
    'Linux version 6.12.115+deb13.1-amd64 (debian-kernel [elided]) (x86_64-linux-gnu-gcc-14 (Debian 14.2.0-19) 14.2.0, GNU ld (GNU Binutils for Debian) 2.44) #1 SMP PREEMPT_DYNAMIC Debian 6.12.115-2 (2026-11-01)\n'
    '[    0.000000] Command line: root=/dev/mapper/root_crypt ro console=tty0 console=ttyS0,115200 consoleblank=0 systemd.show_status=1 ip=[elided] bb.release=2026.11.19-3 bb.host=odl-340a1e bb.iid=168020b4 [elided: the ip pair, bb.key, seal_id, odile.token, the seed URL and the public URL]\n'
    "[… the boot's service start-up, elided; no check here reads it …]\n"
    'bb-sealed-unlock: root_crypt opened with the SNP-derived key\n'
    "[… the boot's service start-up, elided; no check here reads it …]\n"
    '         Starting odile-release-setup.servi…lease (Caddy, /verify, manifest)...\n'
    '[    9.440770] odile-release-setup.sh[687]: odile-release-setup: already converged — ran 2026-11-19T17:09:23Z · odl-340a1e · instance 168020b4 · rc=1 ; nothing to do\n'
    '[  OK  ] Finished odile-release-setup.servi…release (Caddy, /verify, manifest).\n'
    "[… the boot's service start-up, elided; no check here reads it …]\n"
    'Debian GNU/Linux 13 odl-340a1e ttyS0\n'
    '\n'
    'odl-340a1e login:'
)

BENCH_BOOT_2 = (
    '[    0.000000] Linux version 6.12.115+deb13.1-amd64 (debian-kernel [elided]) (x86_64-linux-gnu-gcc-14 (Debian 14.2.0-19) 14.2.0, GNU ld (GNU Binutils for Debian) 2.44) #1 SMP PREEMPT_DYNAMIC Debian 6.12.115-2 (2026-11-01)\n'
    '[    0.000000] Command line: root=/dev/mapper/root_crypt ro console=tty0 console=ttyS0,115200 consoleblank=0 systemd.show_status=1 ip=[elided] bb.release=2026.11.19-3 bb.host=odl-340a1e bb.iid=168020b4 [elided: the ip pair, bb.key, seal_id, odile.token, the seed URL and the public URL]\n'
    "[… the boot's service start-up, elided; no check here reads it …]\n"
    'bb-sealed-unlock: root_crypt opened with the SNP-derived key\n'
    "[… the boot's service start-up, elided; no check here reads it …]\n"
    '         Starting odile-release-setup.servi…lease (Caddy, /verify, manifest)...\n'
    '[   10.131349] odile-release-setup.sh[689]: odile-release-setup: already converged — ran 2026-11-19T17:09:23Z · odl-340a1e · instance 168020b4 · rc=1 ; nothing to do\n'
    '[  OK  ] Finished odile-release-setup.servi…release (Caddy, /verify, manifest).\n'
    "[… the boot's service start-up, elided; no check here reads it …]\n"
    'Debian GNU/Linux 13 odl-340a1e ttyS0\n'
    '\n'
    'odl-340a1e login:'
)


CONVERGED_CYCLE_STUB = (
    "import sys\n"
    "from pathlib import Path\n"
    "args = sys.argv[1:]\n"
    "out = Path(args[args.index('--serial-out') + 1])\n"
    "out.parent.mkdir(parents=True, exist_ok=True)\n"
    "out.write_text(" + repr(BENCH_BOOT_2) + ")\n"
    "print('transcript %s' % out)\n"
)


class ConvergedFreshnessTests(unittest.TestCase):
    """The panel path's --fresh check, fed the two captured converged boots."""

    def run_cycle(self, *texts: str) -> tuple[int, str]:
        """Poll 1 is the --fresh baseline; each later poll returns the next text.

        The last entry repeats, so a two-entry call is "the same buffer all
        along" and a three-entry call is "the buffer changed, then this boot".
        """
        reads = {"n": 0}
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("k", encoding="utf-8")
            out = Path(tmp) / "boot.txt"

            def fake_log(server: str) -> str:
                reads["n"] += 1
                return texts[min(reads["n"] - 1, len(texts) - 1)]

            argv = [
                "--server-id", SERVER, "--serial-out", str(out),
                "--wait-for", "bb-sealed-unlock",
                "--wait-for", "odile-release-setup: already converged",
                "--timeout", "1", "--interval", "0",
                "--key-file", str(key), "--via", "panel", "--fresh",
            ]
            with mock.patch.object(servury_cycle, "panel_log", fake_log), \
                    mock.patch.object(servury_cycle, "panel_console",
                                      return_value=mock.Mock(returncode=0, stdout="", stderr="")), \
                    mock.patch.object(servury_cycle, "panel",
                                      return_value=mock.Mock(returncode=0, stdout="", stderr="")), \
                    mock.patch.object(servury_cycle.time, "sleep", lambda seconds: None), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = servury_cycle.main(argv)
            return code, out.read_text()

    def test_a_converged_boot_after_an_earlier_converged_boot_is_accepted(self) -> None:
        # The real sequence: the buffer holds the earlier converged boot; the
        # power action appends the old boot's shutdown; then the new boot's
        # kernel line arrives after both.
        code, written = self.run_cycle(
            BENCH_BOOT_1,
            BENCH_BOOT_1 + self.SHUTDOWN,
            BENCH_BOOT_1 + self.SHUTDOWN + BENCH_BOOT_2,
        )

        self.assertEqual(code, 0, written)
        self.assertIn("matched=True", written)
        self.assertIn("fresh True earlier_boot_stamps 0", written,
                      "a converged boot carries no setup stamp, which is what made "
                      "the old check skip it")
        self.assertIn(f"earlier_boot_bytes {len(servury_cycle.latest_boot(BENCH_BOOT_1))}", written)
        self.assertIn("odile-release-setup.sh[689]", written, "the new boot is what was written")

    def test_a_previous_converged_boot_does_not_count_as_this_one(self) -> None:
        code, written = self.run_cycle(BENCH_BOOT_2, BENCH_BOOT_2)

        self.assertEqual(code, 1, written)
        self.assertIn("matched=False", written)
        self.assertIn(f"earlier_boot_bytes {len(servury_cycle.latest_boot(BENCH_BOOT_2))}", written)

    SHUTDOWN = ("\n         Stopping ssh.service - OpenBSD Secure Shell server...\n"
                "[  OK  ] Reached target shutdown.target - System Shutdown.\n"
                "[  OK  ] Finished systemd-poweroff.service - System Power Off.\n")

    def test_the_earlier_boot_growing_a_shutdown_tail_still_does_not_count(self) -> None:
        # The power action makes the old boot print its own shutdown into the
        # buffer before the machine goes down. Appended text must not turn the
        # old boot into "this boot": measured on the bench 2026-11-20, the node
        # takes ~75 s to start after a restart, and equality alone matched the
        # old converged line as soon as the shutdown tail landed.
        code, written = self.run_cycle(BENCH_BOOT_2, BENCH_BOOT_2 + self.SHUTDOWN)

        self.assertEqual(code, 1, written)
        self.assertIn("matched=False", written)

    def test_a_rotated_buffer_that_kept_the_old_boot_does_not_count(self) -> None:
        # The window evicted the old boot's head *and* gained its shutdown:
        # neither the text nor a suffix of it describes the new boot.
        code, written = self.run_cycle(BENCH_BOOT_2, BENCH_BOOT_2[5000:] + self.SHUTDOWN)

        self.assertEqual(code, 1, written)
        self.assertIn("matched=False", written)

    def test_a_new_boot_that_starts_like_the_old_one_counts_once_it_diverges(self) -> None:
        old = ("Linux version 6.12 (bench)\n[    0.000000] Command line: root=/dev/x\n"
               "[    5.0] bb-sealed-unlock: root_crypt opened with the SNP-derived key\n"
               "[   10.0] odile-release-setup: already converged — ran 2026-11-20T11:20:09Z\n")
        head = "Linux version 6.12 (bench)\n[    0.000000] Command line: root=/dev/x\n"
        fresh = ("Linux version 6.12 (bench)\n[    0.000000] Command line: root=/dev/x\n"
                 "[    0.004000] ACPI: Using ACPI (MADT)\n"
                 "[    6.0] bb-sealed-unlock: root_crypt opened with the SNP-derived key\n"
                 "[   11.0] odile-release-setup: already converged — ran 2026-11-20T11:20:09Z\n")

        code, written = self.run_cycle(old, old + self.SHUTDOWN + head, old + self.SHUTDOWN + fresh)

        self.assertEqual(code, 0, written)
        self.assertIn("ACPI: Using ACPI", written)


class AppendedTextTests(unittest.TestCase):
    """What the transcript gained since the baseline — the boot's identity."""

    EARLIER = "".join(f"line {index:03d} of the earlier boot\n" for index in range(40))

    def test_what_follows_the_earlier_boots_tail_is_new(self) -> None:
        self.assertEqual(servury_cycle.appended_text(self.EARLIER, self.EARLIER), "")
        self.assertEqual(servury_cycle.appended_text(self.EARLIER, self.EARLIER + "shutdown\n"),
                         "shutdown\n")
        self.assertEqual(servury_cycle.appended_text(self.EARLIER, self.EARLIER[500:] + "shutdown\n"),
                         "shutdown\n", "the window dropped the earlier boot's head")

    def test_a_buffer_that_lost_the_earlier_boot_is_all_new(self) -> None:
        self.assertEqual(servury_cycle.appended_text("abcdef", "defgh"), "defgh",
                         "nothing of the earlier read is left")
        self.assertEqual(servury_cycle.appended_text("", "anything"), "anything")

    def test_only_a_gained_kernel_start_marks_a_new_boot(self) -> None:
        shutdown = "[  OK  ] Reached target shutdown.target\n"

        self.assertNotIn(servury_cycle.BOOT_MARKER,
                         servury_cycle.appended_text(self.EARLIER, self.EARLIER + shutdown))
        self.assertNotIn(servury_cycle.BOOT_MARKER,
                         servury_cycle.appended_text(self.EARLIER, self.EARLIER[500:] + shutdown))
        self.assertIn(servury_cycle.BOOT_MARKER,
                      servury_cycle.appended_text(self.EARLIER, self.EARLIER + shutdown + "Linux version x\n"))


class ConvergedStageTests(unittest.TestCase):
    """The cycle's second wait must accept a boot whose setup had converged."""

    CONVERGED = ("[   10.131349] odile-release-setup.sh[689]: odile-release-setup: already "
                 "converged — ran 2026-11-20T11:20:09Z · odl-340a1e · instance 168020b4 · "
                 "rc=1 ; nothing to do")

    def test_the_wait_for_accepts_a_measurement_or_a_converged_line(self) -> None:
        pattern = uki_cycle.MEASUREMENT_LINE

        self.assertTrue(re.search(pattern, self.CONVERGED))
        self.assertTrue(re.search(pattern, "odile-release-setup.sh[727]:              measurement " + MEASUREMENT))
        self.assertTrue(re.search(pattern, "odile-release-setup: already ran (=== odile-release-setup "
                                           "2026-11-19T17:09:23Z · odl-340a1e · instance 168020b4); "
                                           "marker backfilled, nothing to do"))
        self.assertFalse(re.search(pattern, "odile-release-setup: nothing to do"))
        self.assertFalse(re.search(pattern, "Duplicate IMA measurements will not be recorded"))

    def test_the_kernels_command_line_is_read_for_the_measurement(self) -> None:
        text = ("[    0.000000] Command line: root=/dev/mapper/root_crypt ro bb.iid=168020b4\n"
                "[    0.103364] Kernel command line: root=/dev/mapper/root_crypt ro bb.iid=168020b4\n")

        self.assertEqual(uki_cycle.command_line_from_transcript(text),
                         "root=/dev/mapper/root_crypt ro bb.iid=168020b4")
        self.assertTrue(uki_cycle.command_line_from_transcript(BENCH_BOOT_2)
                        .startswith("root=/dev/mapper/root_crypt ro console=tty0"))
        self.assertIsNone(uki_cycle.command_line_from_transcript("bb-sealed-unlock: opened\n"))


class ExpectedAppendTests(unittest.TestCase):
    def test_the_boots_command_line_is_passed_to_sev_snp_measure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            uki = Path(tmp) / "u.efi"
            uki.write_bytes(b"x")
            ovmf = Path(tmp) / "ovmf.fd"
            ovmf.write_bytes(b"firmware")
            tool = Path(tmp) / "measure.py"
            tool.write_text("#!/usr/bin/python3\nimport sys\nprint(%r if '--append' in sys.argv else %r)\n"
                            % (MEASUREMENT, OTHER), encoding="utf-8")
            tool.chmod(0o755)

            with mock.patch.dict(os.environ, {"ODILE_SEV_SNP_MEASURE": str(tool)}):
                value, note = uki_cycle.expected_measurement(
                    uki, ovmf, vcpus=2, vcpu_type="EPYC-v4", guest_features="0x1",
                    ovmf_sha256=hashlib.sha256(b"firmware").hexdigest(), append="root=/dev/x")

        self.assertEqual(value, MEASUREMENT)
        self.assertIn("--append root=/dev/x", note)


@unittest.skipUnless(CONVERGED_EVIDENCE.is_dir(), "ODL-913's bench captures are not on this host")
class ConvergedFixtureTests(unittest.TestCase):
    def test_the_fixture_lines_are_the_captures_lines(self) -> None:
        for name, fixture in (("bench-cycle-boot.txt", BENCH_BOOT_1),
                              ("bench-cycle-boot-2.txt", BENCH_BOOT_2)):
            capture = (CONVERGED_EVIDENCE / name).read_text(encoding="utf-8", errors="replace")
            for line in fixture.splitlines():
                if not line or line.startswith("[…") or "[elided" in line:
                    continue
                self.assertIn(line, capture, f"{name}: {line[:60]}")

    def test_the_captures_are_the_ones_the_card_quoted(self) -> None:
        for name, digest in (
            ("bench-cycle-boot.txt", "e11ce2cdee44e1e43c4726ca8c663b204339955afe043c88368a419697ff3b47"),
            ("bench-cycle-boot-2.txt", "11652b998b31fb4f0aaf3fb58f8307b309f76ab5a79ac48f3e10a2af7e4264b2"),
        ):
            self.assertEqual(hashlib.sha256((CONVERGED_EVIDENCE / name).read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
