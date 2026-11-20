#!/usr/bin/python3
"""Tests for ODL-215: the tenant backup, its status and the drill.

Three sizes, on purpose:

* **small** — the pure pieces: the recovery-file grammar, the repository URL,
  the SFTP command string, the backup summary parser.
* **medium, hermetic** — a fake `restic` on PATH records every argument and
  the password file, so the tests can prove the nightly run's shape without a
  repository: 7 daily / 4 weekly, `--host` fixed to the slug, no password on
  argv, a status record with the snapshot id, and a failed run that keeps
  `last_success_at`.
* **medium, real** — the system `restic` (0.18.1) against a local temporary
  repository: init, run, delete a file, drill restores it and the checksums
  match, and `verify` proves the password opens the repository while a wrong
  one does not. Skipped when restic is absent.

    /usr/bin/python3 -m unittest tests.test_backup -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "backup.py"
PASSWORD = "test-backup-password-2026-0001"
SLUG = "odl-fake01"


def load_backup():
    spec = importlib.util.spec_from_file_location("odile_backup", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BACKUP = load_backup()

FAKE_RESTIC = textwrap.dedent(
    '''#!/usr/bin/python3
"""A restic stand-in: records argv and the password file, keeps a tiny state."""
import json, os, shutil, sys
from pathlib import Path

LOG = Path(os.environ["FAKE_LOG"])
STATE = Path(os.environ["FAKE_STATE"])
SOURCE = Path(os.environ["FAKE_SOURCE"])
PASSWORD = os.environ.get("FAKE_PASSWORD", "")
arguments = sys.argv[1:]
password_file = os.environ.get("RESTIC_PASSWORD_FILE", "")
entry = {
    "argv": arguments,
    "repo": os.environ.get("RESTIC_REPOSITORY", ""),
    "password_file": password_file,
    "password": Path(password_file).read_text() if password_file and Path(password_file).exists() else None,
}
with LOG.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(entry) + "\\n")

command = arguments[0] if arguments else ""
if command == "version":
    print("restic 0.18.1 (fake)")
    sys.exit(0)
if command == "init":
    if os.environ.get("FAKE_FAIL_INIT"):
        print("Fatal: create repository failed", file=sys.stderr)
        sys.exit(1)
    STATE.write_text("init")
    print("created restic repository (fake)")
    sys.exit(0)
if command == "snapshots":
    if not STATE.exists():
        print("Fatal: unable to open config file: stat: no such file", file=sys.stderr)
        sys.exit(1)
    if entry["password"] != PASSWORD:
        print("Fatal: wrong password", file=sys.stderr)
        sys.exit(1)
    print("[]")
    sys.exit(0)
if command == "backup":
    if os.environ.get("FAKE_FAIL_BACKUP"):
        print("Fatal: unable to save snapshot", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"message_type": "status", "percent_done": 1.0}))
    print(json.dumps({
        "message_type": "summary",
        "snapshot_id": "5f8c1d2e3a4b5c6d7e8f9012a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d",
        "total_bytes_processed": 4096,
        "files_new": 4,
        "files_changed": 1,
    }))
    sys.exit(0)
if command == "stats":
    print(json.dumps({"total_size": 8192}))
    sys.exit(0)
if command == "forget":
    if os.environ.get("FAKE_FAIL_FORGET"):
        print("Fatal: prune failed", file=sys.stderr)
        sys.exit(1)
    print("forget done (fake)")
    sys.exit(0)
if command == "restore":
    target = Path(arguments[arguments.index("--target") + 1])
    includes = [arguments[i + 1] for i, part in enumerate(arguments) if part == "--include"]
    for include in includes:
        source = SOURCE / include.lstrip("/")
        destination = target / include.lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    print("Summary: Restored 1 files (fake)")
    sys.exit(0)
print("fake restic: unknown command %r" % command, file=sys.stderr)
sys.exit(2)
'''
)


class BackupFixture(unittest.TestCase):
    """A temp machine: home, recovery file, repo, status, and a fake restic."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="odile-backup-test-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.home = self.directory / "home"
        (self.home / "documents").mkdir(parents=True)
        (self.home / "documents" / "note.txt").write_text("the only copy\n", encoding="utf-8")
        self.recovery = self.directory / "recovery.txt"
        self.recovery.write_text(
            "Odile recovery file\n"
            "===================\n"
            "Tenant:              %s\n"
            "\n"
            "Backup password:     %s\n"
            "Certificate SHA-256: (pending)\n" % (SLUG, PASSWORD),
            encoding="utf-8",
        )
        self.repo_file = self.directory / "backup-repo.txt"
        self.repo = str(self.directory / "repo")
        self.repo_file.write_text(self.repo + "\n", encoding="utf-8")
        self.status = self.directory / "backup.json"
        self.drill = self.directory / "drill.json"
        self.key_dir = self.directory / "backup-ssh"
        self.session = self.directory / "session"
        self.session.mkdir()
        self.fake_state = self.directory / "fake-state"
        self.fake_source = self.directory / "fake-source"
        self.fake_source.mkdir()
        self.fake_log = self.directory / "fake-log.jsonl"
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        restic = self.bin / "restic"
        restic.write_text(FAKE_RESTIC, encoding="utf-8")
        restic.chmod(0o755)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "FAKE_LOG": str(self.fake_log),
                "FAKE_STATE": str(self.fake_state),
                "FAKE_SOURCE": str(self.fake_source),
                "FAKE_PASSWORD": PASSWORD,
                "RESTIC_CACHE_DIR": str(self.directory / "cache"),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def call(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = BACKUP.main([str(part) for part in arguments])
        return code, out.getvalue(), err.getvalue()

    def common(self, *extra):
        return [
            "--repo-file", str(self.repo_file),
            "--recovery", str(self.recovery),
            "--key", str(self.key_dir / "id_ed25519"),
            "--known-hosts", str(self.key_dir / "known_hosts"),
            *extra,
        ]

    def calls(self):
        if not self.fake_log.exists():
            return []
        return [json.loads(line) for line in self.fake_log.read_text().splitlines() if line]


class RecoveryFileTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="odile-backup-recovery-"))
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_the_installer_line_is_read(self):
        path = self.directory / "recovery.txt"
        path.write_text("Backup password:     %s\n" % PASSWORD, encoding="utf-8")
        self.assertEqual(BACKUP.read_recovery_password(path), PASSWORD)

    def test_a_missing_file_is_a_prerequisite_not_a_crash(self):
        with self.assertRaises(BACKUP.Missing) as caught:
            BACKUP.read_recovery_password(self.directory / "absent.txt")
        self.assertIn("cannot read", str(caught.exception))

    def test_a_file_without_the_line_is_refused(self):
        path = self.directory / "recovery.txt"
        path.write_text("Tenant: odl-1\n", encoding="utf-8")
        with self.assertRaises(BACKUP.Missing) as caught:
            BACKUP.read_recovery_password(path)
        self.assertIn("no 'Backup password:' line", str(caught.exception))

    def test_a_short_password_is_refused(self):
        path = self.directory / "recovery.txt"
        path.write_text("Backup password:     short\n", encoding="utf-8")
        with self.assertRaises(BACKUP.Missing):
            BACKUP.read_recovery_password(path)

    def test_the_error_never_carries_the_password(self):
        path = self.directory / "recovery.txt"
        path.write_text("Backup password:     abc\n", encoding="utf-8")
        with self.assertRaises(BACKUP.Missing) as caught:
            BACKUP.read_recovery_password(path)
        self.assertNotIn(PASSWORD, str(caught.exception))


class RepositoryURLTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="odile-backup-repo-"))
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_a_file_is_trimmed(self):
        path = self.directory / "repo.txt"
        path.write_text("sftp:odile-backup@backup.odile.fyi:/tenants/odl-1\n\n", encoding="utf-8")
        self.assertEqual(
            BACKUP.resolve_repo(None, str(path)),
            "sftp:odile-backup@backup.odile.fyi:/tenants/odl-1",
        )

    def test_whitespace_inside_the_url_is_a_usage_error(self):
        path = self.directory / "repo.txt"
        path.write_text("sftp:user name@host:/x\n", encoding="utf-8")
        with self.assertRaises(BACKUP.BadInput):
            BACKUP.resolve_repo(None, str(path))

    def test_the_sftp_target_is_parsed(self):
        self.assertEqual(
            BACKUP.sftp_target("sftp:odile-backup@backup.odile.fyi:/tenants/odl-1"),
            ("odile-backup", "backup.odile.fyi", None),
        )
        self.assertEqual(
            BACKUP.sftp_target("sftp://odile-backup@203.0.113.10:2222/tenants/odl-1"),
            ("odile-backup", "203.0.113.10", "2222"),
        )
        self.assertIsNone(BACKUP.sftp_target("/var/lib/odile/repo"))

    def test_the_sftp_command_pins_the_key_and_the_host_file(self):
        options = BACKUP.sftp_args(
            "sftp:odile-backup@backup.odile.fyi:/tenants/odl-1",
            "/etc/odile/backup-ssh/id_ed25519",
            "/etc/odile/backup-ssh/known_hosts",
        )
        self.assertEqual(options[0], "-o")
        command = options[1]
        self.assertTrue(command.startswith("sftp.command="))
        self.assertIn("-i /etc/odile/backup-ssh/id_ed25519", command)
        self.assertIn("IdentitiesOnly=yes", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("UserKnownHostsFile=/etc/odile/backup-ssh/known_hosts", command)
        self.assertIn("odile-backup@backup.odile.fyi -s sftp", command)


class BackupSummaryTest(unittest.TestCase):
    def test_the_last_summary_wins(self):
        stdout = "\n".join(
            [
                json.dumps({"message_type": "status", "percent_done": 0.5}),
                json.dumps({"message_type": "summary", "snapshot_id": "aa"}),
                "noise",
            ]
        )
        self.assertEqual(BACKUP.backup_summary(stdout)["snapshot_id"], "aa")

    def test_a_run_without_a_summary_is_empty(self):
        self.assertEqual(BACKUP.backup_summary('{"message_type": "status"}'), {})


class RunTest(BackupFixture):
    def test_dry_run_invokes_nothing_and_writes_nothing(self):
        code, out, err = self.call("run", *self.common("--home", self.home, "--slug", SLUG,
                                                        "--status", self.status, "--dry-run"))
        self.assertEqual(code, 0, err)
        self.assertIn("would run", out)
        self.assertIn("--keep-daily 7", out)
        self.assertIn("--keep-weekly 4", out)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.status.exists())

    def test_the_nightly_run_backs_up_prunes_and_records(self):
        code, out, err = self.call("run", *self.common("--home", self.home, "--slug", SLUG,
                                                        "--status", self.status))
        self.assertEqual(code, 0, err)
        calls = self.calls()
        kinds = [call["argv"][0] for call in calls]
        self.assertEqual(kinds, ["backup", "stats", "forget", "version"])

        backup = calls[0]["argv"]
        self.assertEqual(backup[1], str(self.home))
        self.assertIn("--host", backup)
        self.assertEqual(backup[backup.index("--host") + 1], SLUG)
        self.assertIn("tenant", backup)
        # The rebuildable trees are skipped by absolute path, and a caller's
        # own pattern is added after them.
        excludes = [backup[index + 1] for index, part in enumerate(backup) if part == "--exclude"]
        self.assertIn(str(self.home.resolve() / ".hermes" / "hermes-agent"), excludes)
        self.assertIn(str(self.home.resolve() / ".hermes" / "tools"), excludes)
        self.assertIn("*.tmp", excludes)
        self.assertIn("--exclude-caches", backup)

        forget = calls[2]["argv"]
        self.assertEqual(forget[forget.index("--keep-daily") + 1], "7")
        self.assertEqual(forget[forget.index("--keep-weekly") + 1], "4")
        self.assertEqual(forget[forget.index("--group-by") + 1], "host")
        self.assertIn("--prune", forget)

        record = json.loads(self.status.read_text())
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["snapshot_short"], "5f8c1d2e")
        self.assertEqual(record["repo"], self.repo)
        self.assertEqual(record["repo_bytes"], 8192)
        self.assertEqual(record["retention"], {"keep_daily": 7, "keep_weekly": 4})
        self.assertGreater(record["last_success_at"], 0)
        self.assertNotIn(PASSWORD, self.status.read_text())

    def test_the_password_is_in_a_0600_file_and_never_on_argv_or_stdout(self):
        code, out, err = self.call("run", *self.common("--home", self.home, "--slug", SLUG,
                                                        "--status", self.status))
        self.assertEqual(code, 0, err)
        self.assertNotIn(PASSWORD, out)
        self.assertNotIn(PASSWORD, err)
        for call in self.calls():
            self.assertNotIn(PASSWORD, call["argv"])
            self.assertEqual(call["password"], PASSWORD)
        self.assertIn("snapshot 5f8c1d2e", out)

    def test_a_failed_run_keeps_last_success_at_and_records_why(self):
        first, _, _ = self.call("run", *self.common("--home", self.home, "--slug", SLUG,
                                                     "--status", self.status))
        self.assertEqual(first, 0)
        successful = json.loads(self.status.read_text())["last_success_at"]
        with mock.patch.dict(os.environ, {"FAKE_FAIL_BACKUP": "1"}):
            code, out, err = self.call("run", *self.common("--home", self.home, "--slug", SLUG,
                                                            "--status", self.status))
        self.assertEqual(code, 1)
        record = json.loads(self.status.read_text())
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["last_success_at"], successful)
        self.assertIn("unable to save snapshot", record["error"])
        self.assertNotIn(PASSWORD, self.status.read_text())

    def test_a_missing_home_is_a_prerequisite(self):
        code, out, err = self.call("run", *self.common("--home", self.directory / "nope",
                                                        "--status", self.status))
        self.assertEqual(code, 3)
        self.assertIn("does not exist", err)

    def test_an_extra_exclude_is_appended_after_the_defaults(self):
        code, out, err = self.call("run", *self.common("--home", self.home, "--slug", SLUG,
                                                        "--status", self.status,
                                                        "--exclude", "/home/tenant/scratch"))
        self.assertEqual(code, 0, err)
        backup = self.calls()[0]["argv"]
        excludes = [backup[index + 1] for index, part in enumerate(backup) if part == "--exclude"]
        self.assertEqual(excludes[-1], "/home/tenant/scratch")
        self.assertIn(str(self.home.resolve() / ".hermes" / "hermes-agent"), excludes)

    def test_a_failed_prune_does_not_fail_the_snapshot(self):
        with mock.patch.dict(os.environ, {"FAKE_FAIL_FORGET": "1"}):
            code, out, err = self.call("run", *self.common("--home", self.home, "--slug", SLUG,
                                                            "--status", self.status))
        self.assertEqual(code, 0)
        self.assertIn("retention/prune failed", err)
        self.assertEqual(json.loads(self.status.read_text())["forget"], "failed")


class StatusTest(BackupFixture):
    def test_missing_status_is_one_line_and_exit_one(self):
        code, out, err = self.call("status", "--status", self.status)
        self.assertEqual(code, 1)
        self.assertIn("no status", out)

    def test_json_carries_the_hq_visible_fields(self):
        self.call("run", *self.common("--home", self.home, "--slug", SLUG, "--status", self.status))
        code, out, err = self.call("status", "--status", self.status, "--json")
        self.assertEqual(code, 0, err)
        record = json.loads(out)
        for key in ("repo", "snapshot", "status", "bytes_processed", "repo_bytes",
                    "last_success_at", "host"):
            self.assertIn(key, record)


class InitTest(BackupFixture):
    def test_dry_run_does_not_touch_the_repository(self):
        code, out, err = self.call("init", *self.common("--dry-run"))
        self.assertEqual(code, 0, err)
        self.assertIn("would run  restic init", out)
        self.assertEqual(self.calls(), [])

    def test_it_initialises_once_and_a_second_run_is_same(self):
        code, out, err = self.call("init", *self.common())
        self.assertEqual(code, 0, err)
        self.assertIn("created      the restic repository", out)
        self.assertTrue(self.fake_state.exists())
        code, out, err = self.call("init", *self.common())
        self.assertEqual(code, 0, err)
        self.assertIn("already answers", out)
        self.assertEqual([call["argv"][0] for call in self.calls()], ["snapshots", "init", "snapshots"])

    def test_a_wrong_password_is_not_reported_as_created(self):
        self.recovery.write_text("Backup password:     test-different-password-00016\n", encoding="utf-8")
        self.fake_state.write_text("init")
        code, out, err = self.call("init", *self.common())
        self.assertEqual(code, 1)
        self.assertIn("does not open the existing repository", err)


class DrillTest(BackupFixture):
    def _prime_repo(self):
        """Put the pre-deletion home in the fake repository's source mirror."""
        source = self.fake_source / str(self.home).lstrip("/")
        shutil.copytree(self.home, source)

    def test_the_drill_restores_the_deleted_file_and_the_checksums_match(self):
        self._prime_repo()
        canary = self.home / "documents" / "note.txt"
        before = BACKUP.sha256_file(canary)
        code, out, err = self.call(
            "drill", *self.common("--home", self.home, "--slug", SLUG, "--host", SLUG,
                                  "--file", canary, "--drill", self.drill)
        )
        self.assertEqual(code, 0, err)
        record = json.loads(self.drill.read_text())
        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["sha256_before"], before)
        self.assertEqual(record["sha256_restored"], before)
        self.assertEqual(record["sha256_in_place"], before)
        self.assertTrue(canary.exists())
        self.assertEqual(canary.read_text(), "the only copy\n")
        self.assertIn("PASS", out)
        self.assertIn("--include", self.calls()[-1]["argv"])

    def test_a_second_drill_is_safe_and_still_passes(self):
        self._prime_repo()
        canary = self.home / "documents" / "note.txt"
        for _ in range(2):
            code, out, err = self.call(
                "drill", *self.common("--home", self.home, "--slug", SLUG,
                                      "--file", canary, "--drill", self.drill)
            )
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(self.drill.read_text())["verdict"], "pass")

    def test_a_restore_that_differs_is_a_failed_verdict(self):
        self._prime_repo()
        canary = self.home / "documents" / "note.txt"
        # The "repository" holds different bytes than the live file.
        (self.fake_source / str(canary).lstrip("/")).write_text("tampered\n", encoding="utf-8")
        code, out, err = self.call(
            "drill", *self.common("--home", self.home, "--slug", SLUG,
                                  "--file", canary, "--drill", self.drill)
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(self.drill.read_text())["verdict"], "fail")

    def test_a_missing_file_is_a_prerequisite_not_a_repository_error(self):
        missing = self.home / "documents" / "absent.txt"
        code, out, err = self.call(
            "drill", *self.common("--home", self.home, "--slug", SLUG,
                                  "--file", missing, "--drill", self.drill)
        )
        self.assertEqual(code, 3)
        self.assertIn("does not exist", err)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.drill.exists())


class InstallTest(BackupFixture):
    def test_the_units_name_the_timer_and_the_retention(self):
        unit_dir = self.directory / "units"
        code, out, err = self.call(
            "install", "--unit-dir", str(unit_dir),
            "--install-path", str(self.directory / "installed" / "backup.py"),
            "--home", str(self.home), "--slug", SLUG,
            "--repo", "sftp://backup@example.invalid/tenants/" + SLUG,
        )
        self.assertEqual(code, 0, err)
        service = (unit_dir / "odile-backup-tenant.service").read_text()
        self.assertIn("--repo sftp://backup@example.invalid/tenants/" + SLUG, service)
        timer = (unit_dir / "odile-backup-tenant.timer").read_text()
        self.assertIn("backup.py run", service)
        self.assertIn(f"--home {self.home}", service)
        self.assertIn(f"--slug {SLUG}", service)
        self.assertIn("heartbeat --status", service)
        self.assertIn("OnCalendar=*-*-* 03:00:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertTrue((self.directory / "installed" / "backup.py").is_file())

    def test_install_dry_run_writes_nothing(self):
        unit_dir = self.directory / "units"
        code, out, err = self.call("install", "--unit-dir", str(unit_dir), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertFalse(unit_dir.exists())
        self.assertIn("OnCalendar", out)

    def test_a_second_install_is_same(self):
        unit_dir = self.directory / "units"
        out = ""
        for _ in range(2):
            code, out, err = self.call(
                "install", "--unit-dir", str(unit_dir),
                "--install-path", str(self.directory / "installed" / "backup.py"),
                "--home", str(self.home), "--slug", SLUG,
            )
            self.assertEqual(code, 0, err)
        self.assertEqual(out.count("same"), 2)


class HeartbeatTest(BackupFixture):
    def test_dry_run_creates_no_key(self):
        self.call("run", *self.common("--home", self.home, "--slug", SLUG, "--status", self.status))
        code, out, err = self.call(
            "heartbeat", "--status", self.status, "--slug", SLUG,
            "--key-dir", str(self.directory / "hb"), "--url", "http://127.0.0.1:1/heartbeat",
            "--dry-run",
        )
        self.assertEqual(code, 0, err)
        self.assertFalse((self.directory / "hb").exists())

    def test_no_status_is_a_prerequisite(self):
        code, out, err = self.call("heartbeat", "--status", self.status,
                                   "--key-dir", str(self.directory / "hb"), "--dry-run")
        self.assertEqual(code, 3)
        self.assertIn("run `backup.py run` first", err)


@unittest.skipUnless(shutil.which("restic"), "restic is not installed")
class RealResticTest(unittest.TestCase):
    """The same commands against restic 0.18.1 and a local repository."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="odile-backup-real-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.home = self.directory / "home"
        (self.home / "documents").mkdir(parents=True)
        (self.home / "documents" / "note.txt").write_text("a real byte\n", encoding="utf-8")
        self.recovery = self.directory / "recovery.txt"
        self.recovery.write_text("Backup password:     %s\n" % PASSWORD, encoding="utf-8")
        self.repo = str(self.directory / "repo")
        self.repo_file = self.directory / "repo.txt"
        self.repo_file.write_text(self.repo + "\n", encoding="utf-8")
        self.status = self.directory / "backup.json"
        self.drill = self.directory / "drill.json"
        self.environment = mock.patch.dict(os.environ, {
            "RESTIC_CACHE_DIR": str(self.directory / "cache"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def call(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = BACKUP.main([str(part) for part in arguments])
        return code, out.getvalue(), err.getvalue()

    def common(self, *extra):
        return [
            "--repo-file", str(self.repo_file), "--recovery", str(self.recovery),
            "--key", str(self.directory / "id_ed25519"),
            "--known-hosts", str(self.directory / "known_hosts"),
            *extra,
        ]

    def test_init_run_drill_and_verify_on_a_real_repository(self):
        code, out, err = self.call("init", *self.common())
        self.assertEqual(code, 0, err)
        self.assertIn("created", out)

        code, out, err = self.call("run", *self.common(
            "--home", self.home, "--slug", SLUG, "--status", self.status))
        self.assertEqual(code, 0, err)
        record = json.loads(self.status.read_text())
        self.assertEqual(record["status"], "ok")
        self.assertEqual(len(record["snapshot"]), 64)

        canary = self.home / "documents" / "note.txt"
        before = BACKUP.sha256_file(canary)
        code, out, err = self.call("drill", *self.common(
            "--home", self.home, "--slug", SLUG, "--file", canary, "--drill", self.drill))
        self.assertEqual(code, 0, err)
        drill = json.loads(self.drill.read_text())
        self.assertEqual(drill["verdict"], "pass")
        self.assertEqual(drill["sha256_before"], before)
        self.assertEqual(drill["sha256_restored"], before)

        code, out, err = self.call("verify", *self.common())
        self.assertEqual(code, 0, err)
        self.assertIn("opens the repository", out)
        self.assertIn("refused", out)
        self.assertNotIn(PASSWORD, out)
        self.assertNotIn(PASSWORD, err)

        code, out, err = self.call("restore", *self.common(
            "latest", "--target", str(self.directory / "restore"),
            "--include", str(canary)))
        self.assertEqual(code, 0, err)
        restored = self.directory / "restore" / str(canary).lstrip("/")
        self.assertEqual(BACKUP.sha256_file(restored), before)

    def test_a_wrong_password_does_not_open_the_repository(self):
        self.call("init", *self.common())
        self.recovery.write_text("Backup password:     test-wrong-password-000000\n", encoding="utf-8")
        code, out, err = self.call("verify", *self.common())
        self.assertEqual(code, 1)
        self.assertIn("did not open", err)


if __name__ == "__main__":
    unittest.main()
