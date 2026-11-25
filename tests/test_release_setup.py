"""The release setup converges once — a done marker, not a boot rewrite.

t_ee5aac66 (Savio, 2026-11-13): a user's config must survive a reboot. The
image's second install half used to re-run the signed installer at every boot,
and that installer rewrites its managed config blocks and the `.env`
allowlist — so a key the owner changed (`webapp.public_url`, the provider row,
a key in `.env`) came back on the next boot. The script now writes a done
marker under `/var/lib/odile` and exits `same` on every later boot.

Every branch is driven through the script's own path overrides; the stub
installer records that it ran and with which arguments.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "image" / "odile-release-setup.sh"
MEASUREMENT = "ab" * 48


class ReleaseSetupOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="odile-release-setup-"))
        self.addCleanup(self._cleanup)
        self.cmdline = self.tmp / "cmdline"
        self.cmdline.write_text(
            "BOOT_IMAGE=test odile.token=" + "a" * 64
            + " bb.iid=i-test bb.host=odile-test\n", encoding="utf-8")
        self.pin = self.tmp / "pin.json"
        self.pin.write_text(json.dumps({"uki_signed": {"measurement": MEASUREMENT}}),
                            encoding="utf-8")
        self.attest = self.tmp / "attest.json"
        self.attest.write_text(json.dumps({"measurement": MEASUREMENT}), encoding="utf-8")
        self.installer = self.tmp / "installer.sh"
        self.installer.write_text(
            "#! /usr/bin/python3\nimport sys\n"
            "print('stub-installer ' + ' '.join(sys.argv[1:]))\nsys.exit(0)\n",
            encoding="utf-8")
        self.installer.chmod(0o755)
        self.signature = self.tmp / "installer.sig"
        self.signature.write_text("sig\n", encoding="utf-8")
        self.pubkey = self.tmp / "signing-key.pub"
        self.pubkey.write_text("pub\n", encoding="utf-8")
        self.log = self.tmp / "release-setup.log"
        self.done = self.tmp / "state" / "release-setup.done"
        # The access-report check (ODL-947). The default is the healthy first
        # boot: the report exists and was written after this host's boot, and
        # the unit answers `active`; the tests below bend each of the three.
        self.report = self.tmp / "access-report.txt"
        self.report.write_text("read from your machine, 2026-11-24T09:16:26Z\n",
                               encoding="utf-8")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.systemctl = self.bin / "systemctl"
        self.systemctl.write_text(
            "#! /bin/sh\n"
            "# STUB_UNIT_STATE is what `systemctl is-active` answers.\n"
            "if [ \"$1\" = is-active ]; then\n"
            "    printf '%s\\n' \"${STUB_UNIT_STATE:-active}\"\n"
            "    [ \"${STUB_UNIT_STATE:-active}\" = active ] || exit 3\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8")
        self.systemctl.chmod(0o755)

    def _cleanup(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": str(self.bin) + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
            "ODILE_RELEASE_CMDLINE": str(self.cmdline),
            "ODILE_RELEASE_PIN": str(self.pin),
            "ODILE_RELEASE_ATTEST": str(self.attest),
            "ODILE_RELEASE_INSTALLER": str(self.installer),
            "ODILE_RELEASE_SIGNATURE": str(self.signature),
            "ODILE_RELEASE_PUBKEY": str(self.pubkey),
            "ODILE_RELEASE_LOG": str(self.log),
            "ODILE_RELEASE_DONE": str(self.done),
            "ODILE_RELEASE_REPORT": str(self.report),
            "ODILE_RELEASE_UNIT": "odile-access-report",
        }
        env.update(extra)
        return env

    def run_script(self, *argv: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["sh", str(SCRIPT)] + list(argv), env=self.env(**extra),
                              text=True, capture_output=True, timeout=60)

    def test_the_first_run_installs_and_writes_the_done_marker(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("=== odile-release-setup ", text)
        self.assertIn("stub-installer --first-visit --first-visit-token " + "a" * 64, text)
        self.assertIn("--public-url https://odile-test.odile.fyi", text)
        self.assertIn("=== rc=0", text)
        marker = self.done.read_text(encoding="utf-8")
        self.assertIn("odile-test", marker)
        self.assertIn("rc=0", marker)

    def test_a_converged_machine_runs_nothing(self) -> None:
        self.done.parent.mkdir(parents=True, exist_ok=True)
        self.done.write_text("ran 2026-11-13T10:29:14Z · odile-test · instance i-test · rc=4\n",
                             encoding="utf-8")
        before = self.done.read_text(encoding="utf-8")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already converged", result.stdout)
        self.assertFalse(self.log.exists(), "an already converged machine writes no log line")
        self.assertEqual(self.done.read_text(encoding="utf-8"), before)

    def test_a_machine_that_ran_before_the_marker_backfills_and_runs_nothing(self) -> None:
        """The live tenant's shape: a log from before this card, no marker yet."""
        self.log.write_text(
            "=== odile-release-setup 2026-11-13T10:16:46Z · odile-test · instance i-test\n"
            "=== expect " + MEASUREMENT + "\n"
            "error: the first visit is already set\n"
            "=== rc=1\n"
            "=== odile-release-setup 2026-11-13T10:29:14Z · odile-test · instance i-test\n"
            "=== expect " + MEASUREMENT + "\n"
            "=== rc=4\n", encoding="utf-8")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already ran", result.stdout)
        self.assertIn("marker backfilled", result.stdout)
        self.assertNotIn("stub-installer", self.log.read_text(encoding="utf-8"))
        marker = self.done.read_text(encoding="utf-8")
        self.assertIn("backfilled", marker)
        self.assertIn("=== rc=4", marker)

    def test_the_marker_records_a_failed_run_and_does_not_retry_it(self) -> None:
        self.installer.write_text(
            "#! /usr/bin/python3\nimport sys\n"
            "print('stub-installer ' + ' '.join(sys.argv[1:]))\nsys.exit(4)\n",
            encoding="utf-8")
        self.installer.chmod(0o755)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("=== rc=4", self.log.read_text(encoding="utf-8"))
        self.assertIn("rc=4", self.done.read_text(encoding="utf-8"))

        # The next boot does not run the installer again.
        self.installer.write_text(
            "#! /usr/bin/python3\nprint('stub-installer AGAIN')\nsys.exit(0)\n",
            encoding="utf-8")
        self.installer.chmod(0o755)
        again = self.run_script()
        self.assertIn("already converged", again.stdout)
        self.assertNotIn("AGAIN", self.log.read_text(encoding="utf-8"))

    def test_force_retries_past_the_log_backfill(self) -> None:
        """Removing the marker alone is not a retry; `--force` is.

        The live shape: a log from before the marker exists, so the backfill
        branch would fire on every run and write the marker back without
        running the installer. The retry instruction used to say only "remove
        the marker and start the unit" — a no-op (verifier, 2026-11-14).
        """
        self.log.write_text(
            "=== odile-release-setup 2026-11-13T10:29:14Z · odile-test · instance i-test\n"
            "error: the first visit is already set\n"
            "=== rc=4\n", encoding="utf-8")
        # Without the flag the backfill re-writes the marker and runs nothing.
        self.assertFalse(self.done.exists())
        backfill = self.run_script()
        self.assertIn("marker backfilled", backfill.stdout)
        self.assertNotIn("stub-installer", self.log.read_text(encoding="utf-8"))
        marker = self.done.read_text(encoding="utf-8")
        self.assertIn("backfilled", marker)

        # The documented retry: remove the marker, then force one run.
        self.done.unlink()
        forced = self.run_script("--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertNotIn("already ran", forced.stdout)
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("stub-installer --first-visit", text)
        self.assertIn("=== rc=0", text)
        marker = self.done.read_text(encoding="utf-8")
        self.assertIn("ran ", marker)
        self.assertNotIn("backfilled", marker)

        # And the next boot converges again: one forced run, not a new loop.
        normal = self.run_script()
        self.assertIn("already converged", normal.stdout)
        self.assertEqual(self.log.read_text(encoding="utf-8"), text)

    def test_force_can_be_asked_with_the_environment(self) -> None:
        self.done.parent.mkdir(parents=True, exist_ok=True)
        self.done.write_text("ran 2026-11-13T10:29:14Z · odile-test · instance i-test · rc=4\n",
                             encoding="utf-8")
        forced = self.run_script(ODILE_RELEASE_FORCE="1")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertIn("stub-installer --first-visit", self.log.read_text(encoding="utf-8"))
        self.assertIn("ran ", self.done.read_text(encoding="utf-8"))

    def test_a_boot_without_a_token_writes_no_marker(self) -> None:
        self.cmdline.write_text("BOOT_IMAGE=test bb.iid=i-test bb.host=odile-test\n",
                                encoding="utf-8")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no setup token", result.stderr)
        self.assertFalse(self.done.exists())
        self.assertFalse(self.log.exists())

    def test_the_default_paths_are_the_images(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("/var/lib/odile/release-setup.done", text)
        self.assertIn("/usr/local/bin/odile-install.sh", text)
        self.assertIn("/usr/local/lib/odile-image/odile_install.py.sig", text)

    # ODL-340 follow-up: a second environment names its tenant in the measured
    # command line, so the machine needs no systemd drop-in and the installer
    # serves one name — the one it was built for.

    def _bench_cmdline(self, public_url: str) -> None:
        self.cmdline.write_text(
            "BOOT_IMAGE=test odile.token=" + "a" * 64
            + " bb.iid=i-test bb.host=odile-test"
            + " odile.seed_url=https://test.odile.fyi/seed"
            + " odile.public_url=" + public_url + "\n",
            encoding="utf-8")

    def test_the_measured_name_and_seed_reach_the_installer(self) -> None:
        self._bench_cmdline("https://odile-test.test.odile.fyi")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("--public-url https://odile-test.test.odile.fyi", text)
        self.assertIn("--seed-url https://test.odile.fyi/seed", text)

    def test_a_production_boot_keeps_the_production_name_and_seed(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("--public-url https://odile-test.odile.fyi", text)
        self.assertIn("--seed-url https://odile.fyi/seed", text)

    def test_a_malformed_public_url_token_is_not_a_site_name(self) -> None:
        self._bench_cmdline("not-a-url")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--public-url https://odile-test.odile.fyi",
                      self.log.read_text(encoding="utf-8"))

    def test_an_operators_public_url_still_wins_over_the_token(self) -> None:
        self._bench_cmdline("https://odile-test.test.odile.fyi")
        result = self.run_script(ODILE_RELEASE_PUBLIC_URL="https://moved.odile.fyi")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--public-url https://moved.odile.fyi",
                      self.log.read_text(encoding="utf-8"))

    # ODL-947: the boot's own exit code, its log line and the unit's state
    # agree about the access report. Before this check the release setup logged
    # `FAIL odile-access-report did not start` and still ended rc=0, so the
    # console and the exit code disagreed and the support bundle kept whatever
    # report an earlier boot had written.

    def test_a_report_written_this_boot_keeps_rc_zero_and_says_so(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("ok           " + str(self.report) + " written ", text)
        self.assertIn("ok           odile-access-report is active", text)
        self.assertIn("(this boot started ", text)
        self.assertNotIn("the access report did not pass", text)

    def test_a_stale_report_fails_the_run_and_names_its_timestamp(self) -> None:
        boot = self.host_boot_epoch()
        # One second before the boot is unambiguously stale and stays inside the
        # epoch on a machine that booted moments ago (a fresh gate host).
        stamp = boot - 1 if boot else 1795421600
        os.utime(self.report, (stamp, stamp))
        result = self.run_script()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("FAIL         the access report is stale", text)
        self.assertIn(self.utc(stamp), text)
        self.assertIn("this boot started", text)
        self.assertIn("the access report did not pass this boot", result.stdout)
        self.assertIn("the access report did not pass this boot", text)

    def test_a_missing_report_fails_the_run(self) -> None:
        self.report.unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("was not written this boot",
                      self.log.read_text(encoding="utf-8"))

    def test_a_unit_that_did_not_come_up_fails_the_run(self) -> None:
        result = self.run_script(STUB_UNIT_STATE="failed")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL         odile-access-report is failed",
                      self.log.read_text(encoding="utf-8"))

    def test_a_failed_report_still_records_the_installed_run(self) -> None:
        """The report is the machine's record, not the install's verdict."""
        self.report.unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("=== rc=0", self.log.read_text(encoding="utf-8"))
        self.assertIn("rc=0", self.done.read_text(encoding="utf-8"))

    def test_a_converged_boot_leaves_the_report_check_to_its_own_unit(self) -> None:
        """The check is the first boot's: a converged boot does no work at all.

        The report's unit is enabled at every boot (`WantedBy=multi-user`), so
        a failed report on a converged machine is still visible in `systemctl`
        and on the console; this script's early exit says only that the install
        is done.
        """
        self.done.parent.mkdir(parents=True, exist_ok=True)
        self.done.write_text("ran 2026-11-24T09:16:26Z · odile-test · instance i-test · rc=0\n",
                             encoding="utf-8")
        self.report.unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already converged", result.stdout)

    # -- helpers for the two clocks --------------------------------------

    def host_boot_epoch(self) -> int:
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return int(line.split()[1])
        return 0

    def utc(self, epoch: int) -> str:
        import datetime
        return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)\
            .strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    unittest.main()
