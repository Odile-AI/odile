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
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPT = ROOT / "scripts" / "backup.py"
INSTALLER = ROOT / "installer" / "odile_install.py"
PASSWORD = "test-backup-password-2026-0001"
SLUG = "odl-fake01"
# The redaction marker between OpenSSH markers, the test_claim.py shape.
# The markers are built from fragments so neither a complete PEM block nor
# the bare header exists in this source file: the value at run time is a
# private-key block the installer accepts and both scans skip (the body is
# the marker alone, never a key).
_KEY_HEAD = "-" * 5 + "BEGIN OPENSSH PRIVATE KEY" + "-" * 5
_KEY_FOOT = "-" * 5 + "END OPENSSH PRIVATE KEY" + "-" * 5
REDACTED_PRIVATE_KEY = f"{_KEY_HEAD}\n[REDACTED PRIVATE KEY]\n{_KEY_FOOT}\n"


def load_backup():
    spec = importlib.util.spec_from_file_location("odile_backup", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BACKUP = load_backup()

# Three snapshots as `restic snapshots --json` prints them: full ids, the
# short id restic computes, RFC3339 times out of order on purpose.
SNAPSHOTS = [
    {"id": "1111" + "a" * 60, "short_id": "1111aaaa",
     "time": "2026-11-20T02:07:31.123456789Z", "hostname": "odl-fake01",
     "paths": ["/home/tenant"]},
    {"id": "2222" + "b" * 60, "short_id": "2222bbbb",
     "time": "2026-11-25T02:09:02Z", "hostname": "odl-fake01",
     "paths": ["/home/tenant"]},
    {"id": "3333" + "c" * 60, "short_id": "3333cccc",
     "time": "2026-11-27T02:10:59.5Z", "hostname": "odl-fake01",
     "paths": ["/home/tenant", "/etc/odile"]},
]

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

# restic takes its `-o` transport options before the command, so a stand-in
# has to skip them the way restic does (`-o k=v` is one two-word pair).
rest = list(arguments)
while rest and rest[0] == "-o":
    rest = rest[2:]
command = rest[0] if rest else ""
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
    # A test hands over the array the real restic would print; the default is
    # an empty repository, which is what `init` asks about.
    print(os.environ.get("FAKE_SNAPSHOTS_JSON", "[]"))
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
    size = int(os.environ.get("FAKE_STATS_SIZE", "8192"))
    # A run reads the repository twice: before the upload (is it already over
    # the cap?) and after the prune (did this night's delta cross it?). A test
    # drives the two numbers apart by setting FAKE_STATS_AFTER_PRUNE.
    if STATE.with_name("forgot").exists() and os.environ.get("FAKE_STATS_AFTER_PRUNE"):
        size = int(os.environ["FAKE_STATS_AFTER_PRUNE"])
    print(json.dumps({"total_size": size}))
    sys.exit(0)
if command == "forget":
    if os.environ.get("FAKE_FAIL_FORGET"):
        print("Fatal: prune failed", file=sys.stderr)
        sys.exit(1)
    STATE.with_name("forgot").write_text("1")
    print("forget done (fake)")
    sys.exit(0)
if command == "restore":
    failure = os.environ.get("FAKE_FAIL_RESTORE_RC")
    if failure:
        print(os.environ.get("FAKE_FAIL_RESTORE_TEXT", "Fatal: unable to restore"),
              file=sys.stderr)
        sys.exit(int(failure))
    target = Path(rest[rest.index("--target") + 1])
    includes = [rest[i + 1] for i, part in enumerate(rest) if part == "--include"]
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

    def run_program(self, *arguments, **environment):
        """The real process, the way the first-visit page runs it: exit status as seen."""
        env = dict(os.environ)
        env.update({key: str(value) for key, value in environment.items()})
        return subprocess.run(
            ["/usr/bin/python3", str(SCRIPT), *[str(part) for part in arguments]],
            capture_output=True, text=True, env=env,
        )

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
        # version, then the pre-flight size, the upload, the prune, and the
        # post-prune size the cap is judged on (ADR-004 v3 §4.1).
        self.assertEqual(kinds, ["version", "stats", "backup", "forget", "stats"])

        def arguments(name):
            return next(call["argv"] for call in calls if call["argv"][0] == name)

        backup = arguments("backup")
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

        forget = arguments("forget")
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
        backup = next(call["argv"] for call in self.calls() if call["argv"][0] == "backup")
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


class RestoreTest(BackupFixture):
    """A wrong repository password is restic's exit 12, not the generic exit 1."""

    def restore(self, **environment):
        return self.run_program(
            "restore", "latest", "--target", self.directory / "restore", *self.common(),
            **environment,
        )

    def test_a_wrong_password_is_exit_12_with_no_traceback(self):
        done = self.restore(
            FAKE_FAIL_RESTORE_RC="12",
            FAKE_FAIL_RESTORE_TEXT="Fatal: wrong password or no key found",
        )
        self.assertEqual(done.returncode, 12, done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertNotIn("Traceback", done.stdout)
        self.assertIn("wrong password", done.stderr)
        self.assertIn("does not open the repository", done.stderr)

    def test_the_wrong_password_sentence_alone_is_exit_12(self):
        # A transport wrapper can lose restic's code; the sentence still decides.
        done = self.restore(
            FAKE_FAIL_RESTORE_RC="1",
            FAKE_FAIL_RESTORE_TEXT="Fatal: wrong password or no key found",
        )
        self.assertEqual(done.returncode, 12, done.stderr)

    def test_any_other_restic_failure_stays_exit_1_and_names_the_code(self):
        done = self.restore(
            FAKE_FAIL_RESTORE_RC="1",
            FAKE_FAIL_RESTORE_TEXT="Fatal: unable to restore",
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertIn("exit 1", done.stderr)
        self.assertIn("unable to restore", done.stderr)


class SnapshotInfoTest(BackupFixture):
    """`snapshot-info` names the snapshot a restore would take, read-only."""

    def setUp(self):
        super().setUp()
        # The fake repository exists, so `snapshots` answers the array below.
        self.fake_state.write_text("init", encoding="utf-8")

    def info(self, *arguments, snapshots=None):
        payload = json.dumps(SNAPSHOTS if snapshots is None else snapshots)
        with mock.patch.dict(os.environ, {"FAKE_SNAPSHOTS_JSON": payload}):
            return self.call("snapshot-info", *arguments, *self.common())

    def test_latest_is_the_newest_of_three_and_the_json_names_the_documented_keys(self):
        code, out, err = self.info("latest", "--json")
        self.assertEqual(code, 0, err)
        record = json.loads(out)
        self.assertEqual(set(record), {"id", "short_id", "time", "host", "paths"})
        self.assertEqual(record["id"], SNAPSHOTS[2]["id"])
        self.assertEqual(record["short_id"], "3333cccc")
        self.assertEqual(record["time"], SNAPSHOTS[2]["time"])
        self.assertEqual(record["host"], "odl-fake01")
        self.assertEqual(record["paths"], SNAPSHOTS[2]["paths"])

    def test_the_human_output_is_two_lines_naming_the_id_and_time(self):
        code, out, err = self.info()
        self.assertEqual(code, 0, err)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2, out)
        self.assertIn(SNAPSHOTS[2]["id"], lines[0])
        self.assertIn(SNAPSHOTS[2]["time"], lines[1])

    def test_a_four_character_prefix_picks_that_snapshot_and_a_stranger_is_missing(self):
        code, out, err = self.info("2222", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["id"], SNAPSHOTS[1]["id"])
        code, out, err = self.info("dead", "--json")
        self.assertEqual(code, 3)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn("no snapshot matches", err)

    def test_it_is_read_only_no_lock_in_the_restic_argv(self):
        code, out, err = self.info("latest", "--json")
        self.assertEqual(code, 0, err)
        snapshots_calls = [call for call in self.calls() if call["argv"][0] == "snapshots"]
        self.assertEqual(len(snapshots_calls), 1)
        argv = snapshots_calls[0]["argv"]
        self.assertIn("--json", argv)
        self.assertIn("--no-lock", argv)
        self.assertNotIn("--target", argv)

    def test_no_snapshots_is_exit_3_with_one_line(self):
        code, out, err = self.info(snapshots=[])
        self.assertEqual(code, 3)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn("no snapshots", err)

    def test_dry_run_prints_the_plan_and_runs_nothing(self):
        payload = json.dumps(SNAPSHOTS)
        with mock.patch.dict(os.environ, {"FAKE_SNAPSHOTS_JSON": payload}):
            code, out, err = self.call("snapshot-info", *self.common("--dry-run"))
        self.assertEqual(code, 0, err)
        self.assertIn("snapshots --json --no-lock", out)
        self.assertEqual(self.calls(), [])


class InitTest(BackupFixture):
    def test_a_seed_written_key_prints_its_public_half_with_no_pub_beside_it(self):
        # The BX11 shape: provision minted the key and the seed carried the
        # private half, so there is no `.pub` on the machine to read.
        key = self.key_dir / "id_ed25519"
        key.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                       check=True, capture_output=True)
        Path(str(key) + ".pub").unlink()
        code, out, err = self.call("init", *self.common(
            "--repo", "sftp:u728180-sub1@u728180-sub1.your-storagebox.de:data/restic",
            "--dry-run"))
        self.assertEqual(code, 0, err)
        self.assertIn("ssh-ed25519", BACKUP.public_half(key))
        code, out, err = self.call("init", *self.common(
            "--repo", "sftp:u728180-sub1@u728180-sub1.your-storagebox.de:data/restic"))
        self.assertEqual(code, 0, err)
        self.assertIn("ssh-ed25519", out)

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
        self.assertIn("ConditionPathExists=/etc/odile/recovery.txt", service)
        self.assertIn("--sftp-connections 2", service)
        self.assertIn("OnCalendar=*-*-* %s:00" % BACKUP.timer_start(SLUG), timer)
        self.assertIn("RandomizedDelaySec=0", timer)
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


class SftpTransportTest(unittest.TestCase):
    """The BX11 transport: two connections, port 22 in the command, no port in the URL."""

    REPO = "sftp:u728180-sub1@u728180-sub1.your-storagebox.de:data/restic"

    def options(self, repo=None):
        return BACKUP.sftp_args(repo or self.REPO,
                                "/etc/odile/backup-ssh/id_ed25519",
                                "/etc/odile/backup-ssh/known_hosts")

    def test_the_colon_form_asks_for_two_connections(self):
        options = self.options()
        self.assertIn("sftp.connections=2", options)
        self.assertEqual(options[2:], ["-o", "sftp.connections=2"])

    def test_the_colon_form_puts_port_22_in_the_command(self):
        command = self.options()[1]
        self.assertIn(" -p 22 ", command)
        self.assertIn("-o sftp.connections=2", " ".join(self.options()))

    def test_a_port_in_the_colon_form_is_refused_by_name(self):
        # Measured on the drill: `sftp:user@host:22/path` is the path `22/path`,
        # so a port written there creates a literal `22/` directory.
        with self.assertRaises(BACKUP.BadInput) as caught:
            BACKUP.sftp_target(
                "sftp:u728180-sub1@u728180-sub1.your-storagebox.de:22/data/restic")
        message = str(caught.exception)
        self.assertIn("22/data/restic", message)
        self.assertIn("path", message)
        self.assertIn("sftp.command", message)

    def test_the_double_slash_form_keeps_its_own_port(self):
        self.assertEqual(BACKUP.sftp_target("sftp://user@host:2222/repo"),
                         ("user", "host", "2222"))
        self.assertIn(" -p 2222 ", self.options("sftp://user@host:2222/repo")[1])

    def test_a_local_repository_gets_no_sftp_options(self):
        self.assertEqual(self.options("/var/lib/odile/repo"), [])

    def test_the_connection_count_can_be_asked_for(self):
        options = BACKUP.sftp_args(self.REPO, "/k", "/kh", connections=5)
        self.assertIn("sftp.connections=5", options)


class TimerWindowTest(unittest.TestCase):
    """The nightly start is the slug's hash offset inside 02:00–06:00 UTC."""

    def expected(self, slug):
        offset = int(hashlib.sha256(slug.encode("utf-8")).hexdigest()[:2], 16) % 240
        total = 120 + offset
        return "%02d:%02d" % (total // 60, total % 60), offset

    def test_the_offset_is_the_slugs_hash_inside_the_window(self):
        for slug in ("odl-alpha", "odl-bench", "odl-fake01"):
            start, offset = self.expected(slug)
            self.assertEqual(BACKUP.timer_start(slug), start)
            self.assertTrue(0 <= int(start[:2]) * 60 + int(start[3:]) - 120 < 240)

    def test_two_slugs_can_land_on_two_minutes(self):
        self.assertNotEqual(BACKUP.timer_start("odl-alpha"), BACKUP.timer_start("odl-bench"))

    def test_the_offset_carries_the_window_and_the_arithmetic(self):
        _, offset = self.expected("odl-alpha")
        line = BACKUP.timer_window("odl-alpha")
        self.assertIn(BACKUP.timer_start("odl-alpha"), line)
        self.assertIn("02:00", line)
        self.assertIn(str(offset), line)
        self.assertIn("sftp.connections=2", line)


class CapTest(BackupFixture):
    """The 2.0 GB fair-use cap: read at the start and again after the prune."""

    CAP = "2000000000"

    def run_with(self, **environment):
        with mock.patch.dict(os.environ, environment):
            return self.call("run", *self.common(
                "--home", self.home, "--slug", SLUG, "--status", self.status))

    def commands(self):
        return [entry["argv"][0] for entry in self.calls()]

    def test_a_run_that_starts_over_cap_uploads_nothing(self):
        code, out, err = self.run_with(BACKUP_TENANT_CAP_BYTES=self.CAP,
                                       FAKE_STATS_SIZE="3000000000")
        self.assertEqual(code, 0, err)
        self.assertIn("stats", self.commands())
        self.assertNotIn("backup", self.commands())
        record = json.loads(self.status.read_text())
        self.assertEqual(record["status"], "over_cap")
        self.assertEqual(record["repo_bytes"], 3000000000)
        self.assertEqual(record["cap_bytes"], 2000000000)
        self.assertIn("over_cap", out)

    def test_the_repository_size_is_read_after_the_prune(self):
        code, out, err = self.run_with(BACKUP_TENANT_CAP_BYTES=self.CAP,
                                       FAKE_STATS_SIZE="1000",
                                       FAKE_STATS_AFTER_PRUNE="2500000000")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.commands()[-2:], ["forget", "stats"])
        record = json.loads(self.status.read_text())
        self.assertEqual(record["status"], "over_cap")
        self.assertEqual(record["repo_bytes"], 2500000000)
        self.assertEqual(record["snapshot_short"], "5f8c1d2e")

    def test_a_run_under_the_cap_is_ok_with_the_size_recorded(self):
        code, out, err = self.run_with(BACKUP_TENANT_CAP_BYTES=self.CAP,
                                       FAKE_STATS_SIZE="1000",
                                       FAKE_STATS_AFTER_PRUNE="1999999999")
        self.assertEqual(code, 0, err)
        record = json.loads(self.status.read_text())
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["repo_bytes"], 1999999999)
        self.assertEqual(record["cap_bytes"], 2000000000)

    def test_the_default_cap_is_two_gigabytes(self):
        code, out, err = self.run_with(FAKE_STATS_SIZE="1000",
                                       FAKE_STATS_AFTER_PRUNE="1000")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(self.status.read_text())["cap_bytes"], 2000000000)

    def test_a_malformed_cap_is_a_usage_error(self):
        with self.assertRaises(BACKUP.BadInput):
            with mock.patch.dict(os.environ, {"BACKUP_TENANT_CAP_BYTES": "2 GB"}):
                BACKUP.cap_bytes()


def load_installer(name="odile_install_backup"):
    spec = importlib.util.spec_from_file_location(name, INSTALLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class InstallerPinFilesTest(unittest.TestCase):
    """The seed's `sftp:` half: the URL, the key, and the pinned host key."""

    def setUp(self):
        self.installer = load_installer()
        self.directory = Path(tempfile.mkdtemp(prefix="odile-backup-pin-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.layout = self.installer.Layout.scratch(
            root=self.directory / "root", user="tenant", webapp_port=9141,
            public_url="https://odl-test.odile.fyi")
        self.seed = {
            "slug": "odl-test", "instance_id": "srv-1",
            "backup_repo_url": "sftp:u728180-sub1@u728180-sub1.your-storagebox.de:data/restic",
            "backup": {
                "repo_url": "sftp:u728180-sub1@u728180-sub1.your-storagebox.de:data/restic",
                "readonly_repo_url": "sftp:u728180-sub2@u728180-sub2.your-storagebox.de:restic",
                "ssh_port": 22,
                "known_hosts": (
                    "u728180-sub1.your-storagebox.de ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBench\n"
                    "u728180-sub2.your-storagebox.de ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBench\n"),
                "ssh_private_key": REDACTED_PRIVATE_KEY,
                "readonly_ssh_private_key": REDACTED_PRIVATE_KEY,
                "previous_instance": "",
            },
        }

    def capture(self, dry=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            changed = self.installer.ensure_pin_files(self.layout, self.seed, dry)
        return changed, out.getvalue()

    def test_the_seed_writes_the_url_the_key_and_the_host_key(self):
        changed, out = self.capture()
        self.assertTrue(changed)
        self.assertEqual(self.layout.backup_repo_file.read_text(),
                         "sftp:u728180-sub1@u728180-sub1.your-storagebox.de:data/restic\n")
        key = self.layout.backup_key_dir / "id_ed25519"
        known_hosts = self.layout.backup_key_dir / "known_hosts"
        self.assertEqual(key.read_text(), self.seed["backup"]["ssh_private_key"])
        self.assertEqual(known_hosts.read_text(), self.seed["backup"]["known_hosts"])
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(known_hosts.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.layout.backup_key_dir.stat().st_mode), 0o700)
        self.assertEqual(key.stat().st_uid, os.getuid())
        # no R2 leftovers on an sftp: seed
        self.assertFalse((self.layout.etc / "backup-s3.env").exists())
        for path in self.layout.root.rglob("*"):
            if path.is_file():
                self.assertNotIn("AWS_", path.read_text(errors="replace"),
                                 f"{path} carries an AWS variable")

    def test_a_second_run_prints_same(self):
        self.capture()
        changed, out = self.capture()
        self.assertNotIn("write", out)
        self.assertIn("same", out)
        self.assertTrue(out.count("same") >= 3)

    def test_a_port_in_the_colon_url_is_refused_by_name(self):
        self.seed["backup_repo_url"] = (
            "sftp:u728180-sub1@u728180-sub1.your-storagebox.de:22/data/restic")
        self.seed["backup"]["repo_url"] = self.seed["backup_repo_url"]
        with self.assertRaises(SystemExit) as caught:
            self.capture()
        message = str(caught.exception)
        self.assertIn("22/data/restic", message)
        self.assertIn("path", message)
        self.assertFalse(self.layout.backup_repo_file.exists())

    def test_a_seed_without_the_pair_writes_only_the_url(self):
        seed = {"slug": "odl-test", "instance_id": "srv-1",
                "backup_repo_url": "sftp:odile-backup@backup.odile.fyi:/tenants/odl-test"}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(self.installer.ensure_pin_files(self.layout, seed, False))
        self.assertEqual(self.layout.backup_repo_file.read_text(),
                         "sftp:odile-backup@backup.odile.fyi:/tenants/odl-test\n")
        self.assertFalse((self.layout.backup_key_dir / "id_ed25519").exists())


class InstallerUnitTest(unittest.TestCase):
    """The rendered timer and service carry the card's schedule and transport."""

    def setUp(self):
        self.installer = load_installer("odile_install_backup_units")
        self.layout = self.installer.Layout.tenant(
            user="odile", webapp_port=9120, public_url="https://odl-test.odile.fyi")

    def units(self, slug):
        files = self.installer.backup_unit_files(self.layout, slug, "https://odile.fyi/heartbeat")
        return files[self.layout.backup_service], files[self.layout.backup_timer]

    def test_the_timer_starts_at_the_slugs_offset_with_no_random_delay(self):
        for slug in ("odl-alpha", "odl-bench"):
            service, timer = self.units(slug)
            start = BACKUP.timer_start(slug)
            self.assertIn(f"OnCalendar=*-*-* {start}:00", timer)
            self.assertIn("RandomizedDelaySec=0", timer)
            self.assertIn("Persistent=true", timer)

    def test_the_service_carries_two_connections_and_the_recovery_condition(self):
        service, _ = self.units("odl-alpha")
        self.assertIn("-o sftp.connections=2", service)
        self.assertIn("--sftp-connections 2", service)
        self.assertIn(f"ConditionPathExists={self.layout.recovery}", service)

    def test_the_rendered_units_are_the_programs_own(self):
        service, timer = self.units("odl-alpha")
        self.assertEqual(
            service,
            BACKUP.SERVICE_TEMPLATE.format(
                script=self.layout.backup_source,
                repo_file=self.layout.backup_repo_file,
                repo_arg="",
                recovery=self.layout.recovery,
                home=self.layout.home,
                key=self.layout.backup_key_dir / "id_ed25519",
                known_hosts=self.layout.backup_key_dir / "known_hosts",
                status=self.layout.backup_status,
                slug="odl-alpha",
                url="https://odile.fyi/heartbeat",
                heartbeat_dir=self.layout.backup_heartbeat_dir,
            ))
        self.assertEqual(timer, BACKUP.TIMER_TEMPLATE.format(start=BACKUP.timer_start("odl-alpha")))


class RecoverySkipTest(unittest.TestCase):
    """ADR-004 §7.4: a rebuild mints no password until the user pastes."""

    def setUp(self):
        self.installer = load_installer("odile_install_backup_recovery")
        self.directory = Path(tempfile.mkdtemp(prefix="odile-backup-recovery-skip-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.layout = self.installer.Layout.scratch(
            root=self.directory / "root", user="tenant", webapp_port=9142,
            public_url="https://odl-test.odile.fyi")
        self.layout.etc.mkdir(parents=True, exist_ok=True)

    def call(self, previous, supplied=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            changed, password, created = self.installer.ensure_recovery(
                self.layout, "odl-test", "srv-2", None, False, supplied,
                previous_instance=previous)
        return changed, password, created, out.getvalue()

    def test_a_rebuild_with_no_file_skips_and_keeps_no_password(self):
        changed, password, created, out = self.call("srv-old-0001")
        self.assertFalse(changed)
        self.assertFalse(created)
        self.assertEqual(password, "")
        self.assertFalse(self.layout.recovery.exists())
        self.assertIn("skip", out)
        self.assertIn("rebuild", out)

    def test_a_fresh_install_still_mints_the_file(self):
        changed, password, created, out = self.call("")
        self.assertTrue(changed)
        self.assertTrue(created)
        self.assertEqual(len(password), 32)
        self.assertTrue(self.layout.recovery.exists())
        self.assertEqual(stat.S_IMODE(self.layout.recovery.stat().st_mode), 0o600)

    def test_once_the_file_exists_a_later_run_keeps_the_password(self):
        _, pasted, _ = self.installer.ensure_recovery(
            self.layout, "odl-test", "srv-1", None, False, "P" * 32)
        changed, password, created, out = self.call("srv-old-0001")
        self.assertEqual(password, pasted)
        self.assertTrue(changed)   # the instance line moved srv-1 -> srv-2
        self.assertFalse(created)
        text = self.layout.recovery.read_text()
        self.assertIn("Backup password:     " + "P" * 32, text)
        self.assertIn("Instance:            srv-2", text)


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

        # The page names the snapshot the restore just took, from restic's own
        # `snapshots --json`: the drill left the newest one, so `latest` is it.
        code, out, err = self.call("snapshot-info", *self.common("latest", "--json"))
        self.assertEqual(code, 0, err)
        info = json.loads(out)
        self.assertEqual(info["id"], drill["snapshot"])
        self.assertEqual(info["short_id"], drill["snapshot"][:8])
        self.assertEqual(info["host"], SLUG)
        self.assertTrue(info["time"].endswith("Z"), info["time"])
        self.assertEqual(len(info["id"]), 64)

    def test_a_wrong_password_does_not_open_the_repository(self):
        self.call("init", *self.common())
        self.recovery.write_text("Backup password:     test-wrong-password-000000\n", encoding="utf-8")
        code, out, err = self.call("verify", *self.common())
        self.assertEqual(code, 1)
        self.assertIn("did not open", err)


if __name__ == "__main__":
    unittest.main()
