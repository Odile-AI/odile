"""The access report is checked against a number this machine can have.

ODL-947's first boot of the bench image ended `rc=0` while
`odile-access-report` had failed at every boot. The cause was the number the
wrapper passed to the reporter as `--expect`: it was the image pin, and a pin
is a **file's** measurement. The UKI is built with an empty command line and
the machine passes its own line at launch (`--append`), so a boot's number
covers the per-machine tokens and the vCPU count. Measured on the cold walk:
the pin was `d1aa68fb…` from `uki.sh --vcpus 2`, the 1 vCPU machine's own
report was `9f2bc74b…`, and the seal row failed at every boot — the unit
exited non-zero and `/var/lib/odile/access-report.txt` was rewritten with a
FAIL until the release setup's restart gave up.

The rule now: the pin and this boot's own report are both read. When they
agree, the pin is the number compared. When they disagree, the boot's own
measurement is compared and one line names both numbers — the same choice
`odile-release-setup.sh` already made for the installer's `--expect`.

The reporter is this test's stub: it records its argv and exits with the rc the
test asks for. Every path the wrapper reads is overridable, which is the
wrapper's own bench contract.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "installer" / "access_report.sh"

PIN = "a" * 96
BOOT = "b" * 96


class AccessReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="odile-access-report-"))
        self.addCleanup(self._cleanup)
        self.pin = self.tmp / "tenant-image.pin"
        self.attest = self.tmp / "attestation.json"
        self.args = self.tmp / "reporter.args"
        self.reporter = self.tmp / "reporter.sh"
        self.reporter.write_text(
            "#! /bin/sh\n"
            "printf '%s\\n' \"$@\" > \"" + str(self.args) + "\"\n"
            "exit \"${STUB_REPORTER_RC:-0}\"\n",
            encoding="utf-8")
        self.reporter.chmod(self.reporter.stat().st_mode | stat.S_IEXEC)
        self.write_pin(PIN)
        self.write_attest(BOOT)

    def _cleanup(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_pin(self, measurement: str) -> None:
        self.pin.write_text(json.dumps({"uki_signed": {"measurement": measurement}}),
                            encoding="utf-8")

    def write_attest(self, measurement: str) -> None:
        self.attest.write_text(json.dumps({"measurement": measurement}), encoding="utf-8")

    def run_script(self, **extra: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "ODILE_ACCESS_PIN_FILES": str(self.pin),
            "ODILE_ACCESS_ATTEST": str(self.attest),
            "ODILE_ACCESS_USER": "tenant",
            "ODILE_ACCESS_OUT": str(self.tmp / "access-report.txt"),
            "ODILE_REPORTER": str(self.reporter),
            "ODILE_ACCESS_ROOT": str(self.tmp),
        }
        env.update(extra)
        return subprocess.run(["sh", str(SCRIPT)], env=env, text=True,
                              capture_output=True, timeout=60)

    def reporter_args(self) -> list[str]:
        return self.args.read_text(encoding="utf-8").splitlines()

    def value_after(self, flag: str) -> str:
        args = self.reporter_args()
        return args[args.index(flag) + 1]

    # -- the two numbers agree ------------------------------------------

    def test_the_pin_is_the_expect_when_this_boot_agrees_with_it(self) -> None:
        self.write_attest(PIN)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.value_after("--expect"), PIN)
        self.assertIn("pins " + PIN, result.stdout)
        self.assertNotIn("this boot is", result.stderr)

    # -- the two numbers disagree ---------------------------------------

    def test_the_boots_own_measurement_is_the_expect_when_the_pin_differs(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.value_after("--expect"), BOOT)
        self.assertNotIn(PIN, self.reporter_args())
        # One line names both numbers, so the log is never "the report failed"
        # with no way to see which number was compared.
        self.assertIn(str(self.pin) + " pins " + PIN, result.stderr)
        self.assertIn("this boot is " + BOOT, result.stderr)
        self.assertIn("reporting with the boot's own measurement", result.stderr)

    def test_the_reporters_own_failure_is_the_scripts_exit_code(self) -> None:
        """The wrapper does not turn a FAIL report into a pass."""
        result = self.run_script(STUB_REPORTER_RC="1")
        self.assertEqual(result.returncode, 1)

    # -- the report's own arguments -------------------------------------

    def test_the_report_goes_to_the_unit_user_and_path(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.value_after("--user"), "tenant")
        self.assertEqual(self.value_after("--out"), str(self.tmp / "access-report.txt"))
        self.assertEqual(self.value_after("--mode"), "access")
        self.assertEqual(self.value_after("--root"), str(self.tmp))

    # -- no pin at all ---------------------------------------------------

    def test_no_pin_writes_nothing_and_exits_two(self) -> None:
        self.pin.unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 2)
        self.assertIn("no 96-hex measurement", result.stderr)
        self.assertFalse(self.args.exists(), "the reporter must not run without a pin")

    def test_a_pin_without_a_measurement_is_not_a_pin(self) -> None:
        self.pin.write_text(json.dumps({"uki_signed": {"sha256": "c" * 64}}),
                            encoding="utf-8")
        result = self.run_script()
        self.assertEqual(result.returncode, 2)
        self.assertIn("no 96-hex measurement", result.stderr)

    # -- this boot wrote no report ---------------------------------------

    def test_a_boot_with_no_report_uses_the_pin(self) -> None:
        """Attestation absent: the pin is the only number, and is compared."""
        self.attest.unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.value_after("--expect"), PIN)

    def test_a_boot_report_with_no_measurement_uses_the_pin(self) -> None:
        """A half-written or erroring report is not a number to compare."""
        self.attest.write_text(json.dumps({"error": "no /dev/sev"}),
                               encoding="utf-8")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.value_after("--expect"), PIN)


if __name__ == "__main__":
    unittest.main()
