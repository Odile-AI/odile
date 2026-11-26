#!/usr/bin/python3
"""Tests for ODL-218 follow-up: the 30-day purge of a destroyed tenant's backup.

Three sizes, on purpose:

* **small** — the pure pieces: the repository URL's path below the box's chroot,
  the `keep_until` grammar, and the `authorized_keys` line match.
* **medium, hermetic** — a fake `restic` on PATH records every call, so a plan
  can be proved to call none and to leave the state store byte-identical.
* **medium, real** — the system `restic` (0.18) against a local scratch
  repository and a copy of the state-store schema: the check, the delete, the
  key, the record, and the idempotent second run. Skipped when restic is absent.

    /usr/bin/python3 -m unittest tests.test_purge -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odilectl import cli, provision, purge  # noqa: E402
from odilectl.state import StateStore  # noqa: E402

SLUG = "odl-fake01"
DUE = "2026-11-01T00:00:00Z"  # past for every run of this suite
FUTURE = "2099-01-01T00:00:00Z"
PASSWORD = "test-purge-password-2026-0001"

FAKE_RESTIC = textwrap.dedent(
    '''#!/usr/bin/python3
"""A restic stand-in: records the call and fails loudly if it is made."""
import json, os, sys
from pathlib import Path
LOG = Path(os.environ["FAKE_LOG"])
with LOG.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": sys.argv[1:], "repo": os.environ.get("RESTIC_REPOSITORY", "")}) + "\\n")
print("fake restic: this call should not happen", file=sys.stderr)
sys.exit(9)
'''
)


def destroyed_backup(repo, *, keep_until=DUE, snapshot=None):
    """The facts `odilectl destroy` writes into `detail.backup`."""
    return {"repo": repo, "snapshot": snapshot, "keep_until": keep_until, "days": 30}


class PurgeFixture(unittest.TestCase):
    """A scratch box root, a key file, a config and the live state schema."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="odile-purge-test-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.root = self.directory / "srv"
        self.repo = self.root / "tenants" / SLUG
        self.repo.parent.mkdir(parents=True)
        self.repo_url = f"sftp:odile-backup@backup.odile.fyi:/tenants/{SLUG}"
        self.db = self.directory / "waitlist.db"
        self.recovery = self.directory / "recovery.txt"
        self.recovery.write_text(
            "Odile recovery file\nBackup password:     %s\n" % PASSWORD,
            encoding="utf-8",
        )
        self.authorized = self.directory / "authorized_keys"
        self.authorized.write_text(
            "ssh-ed25519 AAAAdrill odile-215 restore drill key\n"
            f"ssh-ed25519 AAAAtenant odile-backup {SLUG}\n"
            "ssh-ed25519 AAAAother odile-backup odl-0000other\n",
            encoding="utf-8",
        )
        self.config = self.make_config()

    def make_config(self, **overrides) -> Path:
        path = self.directory / "config.yaml"
        values = {
            "state_db": str(self.db),
            "dns_zone": "odile.fyi",
        }
        values.update(overrides)
        path.write_text(
            "".join(f"{key}: {value}\n" for key, value in values.items() if value is not None),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def seed(self, *, state="destroyed", repo="__default__", keep_until=DUE,
             snapshot=None, backup=True, extra=None):
        store = StateStore.open(self.db)
        try:
            detail = dict(extra or {})
            if backup:
                detail["backup"] = destroyed_backup(
                    self.repo_url if repo == "__default__" else repo,
                    keep_until=keep_until,
                    snapshot=snapshot,
                )
            stamp = "2026-11-13T00:00:00Z"
            store.connection.execute(
                "INSERT INTO tenant_state (slug, state, detail, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (SLUG, state, json.dumps(detail), stamp, stamp),
            )
            store.connection.commit()
        finally:
            store.close()

    def call(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(
                ["--config", str(self.config), *[str(part) for part in arguments]],
                stdout=out,
                stderr=err,
            )
        return code, out.getvalue(), err.getvalue()

    def command(self, *extra, apply=False):
        arguments = ["purge", SLUG, "--root", str(self.root),
                     "--sftp-authorized-keys", str(self.authorized)]
        if apply:
            arguments.append("--apply")
        return self.call(*arguments, *extra)

    def row(self):
        connection = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT state, detail, updated_at FROM tenant_state WHERE slug = ?", (SLUG,)
            ).fetchone()
            value = dict(row)
        finally:
            connection.close()
        value["detail"] = json.loads(value["detail"])
        return value

    def event_count(self):
        connection = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            return connection.execute("SELECT COUNT(*) FROM tenant_state_events").fetchone()[0]
        finally:
            connection.close()

    def set_backup(self, **fields):
        connection = sqlite3.connect(str(self.db))
        try:
            detail = self.row()["detail"]
            detail["backup"].update(fields)
            connection.execute("UPDATE tenant_state SET detail = ? WHERE slug = ?",
                               (json.dumps(detail), SLUG))
            connection.commit()
        finally:
            connection.close()


# ------------------------------------------------------------------ the pure pieces


class PurgePlanTest(unittest.TestCase):
    """The URL-to-path map, the date, and the key lines."""

    def test_an_sftp_url_maps_under_the_root(self):
        root = Path("/srv/odile-backup")
        for repo in (
            "sftp:odile-backup@backup.odile.fyi:/tenants/odl-1",
            "sftp://odile-backup@203.0.113.10:2222/tenants/odl-1",
        ):
            self.assertEqual(
                purge.repository_path(repo, root, "odl-1"),
                Path("/srv/odile-backup/tenants/odl-1"),
            )

    def test_a_path_outside_the_root_is_refused(self):
        with self.assertRaises(purge.UsageError) as caught:
            purge.repository_path(
                "sftp:backup@host:/tenants/../../etc/odl-1", Path("/srv/root"), "odl-1"
            )
        self.assertIn("outside", str(caught.exception))

    def test_a_path_that_does_not_end_in_the_slug_is_refused(self):
        with self.assertRaises(purge.UsageError) as caught:
            purge.repository_path("sftp:backup@host:/tenants/odl-2", Path("/srv/root"), "odl-1")
        self.assertIn("not 'odl-1'", str(caught.exception))

    def test_a_local_repository_path_must_end_in_the_slug(self):
        self.assertEqual(
            purge.repository_path("/srv/root/odl-1", Path("/srv/root"), "odl-1"),
            Path("/srv/root/odl-1"),
        )
        for path in ("/srv/root/repo", "/srv/root"):
            with self.assertRaises(purge.UsageError):
                purge.repository_path(path, Path("/srv/root"), "odl-1")

    def test_keep_until_is_an_iso_utc_timestamp(self):
        self.assertEqual(
            purge.parse_keep_until("2026-12-13T03:31:39Z"),
            int(datetime(2026, 12, 13, 3, 31, 39, tzinfo=timezone.utc).timestamp()),
        )

    def test_a_missing_or_malformed_keep_until_is_a_usage_error(self):
        for value in (None, "", 1797132699, "13/12/2026", "2026-12-13"):
            with self.assertRaises(purge.UsageError):
                purge.parse_keep_until(value)

    def test_the_key_lines_are_matched_by_a_whole_slug_token(self):
        text = (
            "# a comment line\n"
            "ssh-ed25519 AAAAdrill odile-215 restore drill key\n"
            "ssh-ed25519 AAAAone odile-backup odl-1\n"
            "ssh-ed25519 AAAAtwo odile-backup odl-12\n"
            'command="/bin/true" ssh-ed25519 AAAAthree odile-backup odl-1\n'
            "ssh-ed25519 AAAAfour odile-backup odile-bench\n"
        )
        found = purge.sftp_key_lines(text, ["odl-1"])
        self.assertEqual([number for number, _, _ in found], [3, 5])
        # The image writes the guest's kernel hostname; a box that registered
        # that comment names it with --sftp-key-comment.
        with_hostname = purge.sftp_key_lines(text, ["odl-1", "odile-bench"])
        self.assertEqual([number for number, _, _ in with_hostname], [3, 5, 6])


# ------------------------------------------------------------------ hermetic size


class PurgePlanCallsNoResticTest(PurgeFixture):
    """The plan reads the row and the key file; it never reaches restic."""

    def setUp(self):
        super().setUp()
        self.seed(snapshot="ab" * 32)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        restic = self.bin / "restic"
        restic.write_text(FAKE_RESTIC, encoding="utf-8")
        restic.chmod(0o755)
        self.log = self.directory / "restic-calls.jsonl"
        self.environment = mock.patch.dict(
            os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": str(self.log)}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_the_plan_lists_every_action_and_changes_nothing(self):
        database = self.db.read_bytes()
        keys = self.authorized.read_text(encoding="utf-8")
        code, out, err = self.command()
        self.assertEqual(code, 0, err)
        self.assertIn("purge: dry run (pass --apply to execute)", out)
        for action in ("backup.verify", "backup.delete", "sftp.revoke", "state.purge"):
            self.assertIn(action, out)
        self.assertFalse(self.log.exists(), "the plan called restic")
        self.assertEqual(self.db.read_bytes(), database)
        self.assertEqual(self.authorized.read_text(encoding="utf-8"), keys)

    def test_the_plan_names_the_key_line_it_would_drop(self):
        code, out, err = self.command()
        self.assertEqual(code, 0, err)
        self.assertIn("lines=[2]", out)
        self.assertNotIn("lines=[1]", out)


# --------------------------------------------------------------- medium, real restic


@unittest.skipUnless(shutil.which("restic"), "restic is not installed")
class PurgeRealResticTest(PurgeFixture):
    """The purge against restic 0.18 and the live state schema."""

    def setUp(self):
        super().setUp()
        self.environment = mock.patch.dict(os.environ, {
            "RESTIC_PASSWORD": PASSWORD,
            "RESTIC_CACHE_DIR": str(self.directory / "cache"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        subprocess.run(["restic", "init", "-r", str(self.repo), "--quiet"],
                       check=True, capture_output=True)
        source = self.directory / "source"
        source.mkdir()
        (source / "note.txt").write_text("a real byte\n", encoding="utf-8")
        subprocess.run(["restic", "-r", str(self.repo), "backup", str(source),
                        "--host", SLUG, "--quiet"], check=True, capture_output=True)
        listed = subprocess.run(["restic", "-r", str(self.repo), "snapshots", "--json"],
                                check=True, capture_output=True, text=True)
        self.snapshot = json.loads(listed.stdout)[0]["id"]
        self.seed(snapshot=self.snapshot)

    def test_apply_deletes_the_repository_drops_the_key_and_records(self):
        events = self.event_count()
        code, out, err = self.command("--password-file", self.recovery, apply=True)
        self.assertEqual(code, 0, err)
        self.assertIn("purge: applied", out)
        self.assertIn("opened, 1 snapshot(s)", out)
        self.assertIn("the recorded {} is present".format(self.snapshot[:8]), out)
        self.assertFalse(self.repo.exists())
        self.assertEqual(
            self.authorized.read_text(encoding="utf-8").splitlines(),
            ["ssh-ed25519 AAAAdrill odile-215 restore drill key",
             "ssh-ed25519 AAAAother odile-backup odl-0000other"],
        )
        row = self.row()
        self.assertEqual(row["detail"]["backup"]["keep_until"], DUE)
        self.assertEqual(row["detail"]["backup"]["repo"], self.repo_url)
        self.assertEqual(row["detail"]["backup"]["snapshot"], self.snapshot)
        purged = row["detail"]["backup"]["purged"]
        self.assertEqual(purged["snapshots"], 1)
        self.assertTrue(purged["snapshot_present"])
        self.assertEqual(purged["sftp_keys_dropped"], 1)
        self.assertGreater(row["updated_at"], "2026-11-13T00:00:00Z")
        self.assertEqual(self.event_count(), events)  # a detail, not a new event row
        self.assertNotIn(PASSWORD, out)
        self.assertNotIn(PASSWORD, err)

    def test_a_second_run_says_already_purged_and_writes_nothing(self):
        first, out, err = self.command("--password-file", self.recovery, apply=True)
        self.assertEqual(first, 0, err)
        database = self.db.read_bytes()
        keys = self.authorized.read_text(encoding="utf-8")
        purged_at = self.row()["detail"]["backup"]["purged"]["at"]
        code, out, err = self.command(apply=True)
        self.assertEqual(code, 0, err)
        self.assertIn("purge: already purged", out)
        self.assertIn(purged_at, out)
        self.assertEqual(self.db.read_bytes(), database)
        self.assertEqual(self.authorized.read_text(encoding="utf-8"), keys)

    def test_without_a_password_the_users_lock_is_not_a_refusal(self):
        code, out, err = self.command(apply=True)
        self.assertEqual(code, 0, err)
        self.assertIn("present, not opened", out)
        self.assertIn("purged", self.row()["detail"]["backup"])
        self.assertFalse(self.repo.exists())

    def test_a_wrong_password_is_exit_four_and_names_the_repository(self):
        self.recovery.write_text(
            "Backup password:     test-wrong-password-0000001\n", encoding="utf-8"
        )
        code, out, err = self.command("--password-file", self.recovery, apply=True)
        self.assertEqual(code, 4)
        self.assertIn(str(self.repo), err)
        self.assertTrue(self.repo.exists())
        self.assertNotIn("purged", self.row()["detail"]["backup"])
        self.assertIn(SLUG, self.authorized.read_text(encoding="utf-8"))

    def test_a_directory_restic_cannot_open_is_exit_four(self):
        shutil.rmtree(self.repo)
        self.repo.mkdir()
        code, out, err = self.command(apply=True)
        self.assertEqual(code, 4)
        self.assertIn(str(self.repo), err)
        self.assertTrue(self.repo.exists())
        self.assertNotIn("purged", self.row()["detail"]["backup"])

    def test_a_repository_already_gone_converges(self):
        shutil.rmtree(self.repo)
        code, out, err = self.command(apply=True)
        self.assertEqual(code, 0, err)
        self.assertIn("already absent", out)
        self.assertIn("purged", self.row()["detail"]["backup"])
        self.assertNotIn(SLUG, self.authorized.read_text(encoding="utf-8"))

    def test_a_future_keep_until_refuses(self):
        self.set_backup(keep_until=FUTURE)
        code, out, err = self.command(apply=True)
        self.assertEqual(code, 2)
        self.assertIn("not due", err)
        self.assertTrue(self.repo.exists())
        self.assertNotIn("purged", self.row()["detail"]["backup"])

    def test_a_live_tenant_refuses(self):
        connection = sqlite3.connect(str(self.db))
        try:
            connection.execute("UPDATE tenant_state SET state = 'ready' WHERE slug = ?", (SLUG,))
            connection.commit()
        finally:
            connection.close()
        code, out, err = self.command(apply=True)
        self.assertEqual(code, 2)
        self.assertIn("not destroyed", err)
        self.assertTrue(self.repo.exists())

    def test_no_tenant_row_refuses(self):
        code, out, err = self.call("purge", "odl-absent", "--root", str(self.root),
                                   "--sftp-authorized-keys", str(self.authorized))
        self.assertEqual(code, 2)
        self.assertIn("no tenant named", err)

    def test_a_row_without_a_repository_refuses(self):
        self.set_backup(repo=None)
        code, out, err = self.command(apply=True)
        self.assertEqual(code, 2)
        self.assertIn("no repository", err)
        self.assertTrue(self.repo.exists())

    def test_a_repository_path_that_is_not_the_slug_refuses(self):
        self.set_backup(repo=f"sftp:odile-backup@backup.odile.fyi:/tenants/{SLUG}-other")
        code, out, err = self.command(apply=True)
        self.assertEqual(code, 2)
        self.assertIn("not 'odl-fake01'", err)
        self.assertTrue(self.repo.exists())

    def test_the_plan_is_the_default_and_apply_is_what_deletes(self):
        code, out, err = self.command()
        self.assertEqual(code, 0, err)
        self.assertIn("pass --apply", out)
        self.assertTrue(self.repo.exists())
        self.assertNotIn("purged", self.row()["detail"]["backup"])

    def test_apply_and_dry_run_together_is_the_plan(self):
        code, out, err = self.command("--apply", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("pass --apply", out)
        self.assertTrue(self.repo.exists())
        self.assertNotIn("purged", self.row()["detail"]["backup"])

    def test_a_row_without_any_backup_detail_refuses(self):
        connection = sqlite3.connect(str(self.db))
        try:
            connection.execute("UPDATE tenant_state SET detail = '{}' WHERE slug = ?", (SLUG,))
            connection.commit()
        finally:
            connection.close()
        code, out, err = self.command(apply=True)
        self.assertEqual(code, 2)
        self.assertIn("records no backup", err)
        self.assertTrue(self.repo.exists())

    def test_a_key_file_with_no_line_for_the_tenant_refuses_before_the_delete(self):
        # The image writes the guest's kernel hostname (`odile-bench`) into the
        # comment; the box's line may carry that instead of the slug.
        self.authorized.write_text(
            "ssh-ed25519 AAAAdrill odile-215 restore drill key\n"
            "ssh-ed25519 AAAAtenant odile-backup odile-bench\n",
            encoding="utf-8",
        )
        code, out, err = self.command(apply=True)
        self.assertEqual(code, 2)
        self.assertIn("no line of", err)
        self.assertIn("--sftp-key-comment", err)
        self.assertTrue(self.repo.exists())
        self.assertNotIn("purged", self.row()["detail"]["backup"])

    def test_the_image_hostname_is_named_with_sftp_key_comment(self):
        self.authorized.write_text(
            "ssh-ed25519 AAAAdrill odile-215 restore drill key\n"
            "ssh-ed25519 AAAAtenant odile-backup odile-bench\n",
            encoding="utf-8",
        )
        code, out, err = self.command("--sftp-key-comment", "odile-bench", apply=True)
        self.assertEqual(code, 0, err)
        self.assertFalse(self.repo.exists())
        self.assertEqual(self.authorized.read_text(encoding="utf-8").splitlines(),
                         ["ssh-ed25519 AAAAdrill odile-215 restore drill key"])
        self.assertEqual(self.row()["detail"]["backup"]["purged"]["sftp_keys_dropped"], 1)

    def test_a_missing_authorized_keys_is_nothing_to_drop(self):
        self.authorized.unlink()
        code, out, err = self.command(apply=True)
        self.assertEqual(code, 0, err)
        self.assertIn("no authorized_keys", out)
        self.assertFalse(self.repo.exists())
        self.assertEqual(self.row()["detail"]["backup"]["purged"]["sftp_keys_dropped"], 0)

    def test_a_recorded_snapshot_that_is_gone_is_reported_not_required(self):
        # `restic forget --prune` can remove the last heartbeat's snapshot
        # before the purge; the repository is still the tenant's.
        self.set_backup(snapshot="0" * 64)
        code, out, err = self.command("--password-file", self.recovery, apply=True)
        self.assertEqual(code, 0, err)
        self.assertIn("NOT present", out)
        self.assertFalse(self.row()["detail"]["backup"]["purged"]["snapshot_present"])
        self.assertFalse(self.repo.exists())

    def test_ambient_restic_variables_are_ignored(self):
        with mock.patch.dict(os.environ, {
            "RESTIC_PASSWORD_COMMAND": "/bin/false",
            "RESTIC_PASSWORD": "an-ambient-password-000001",
            "RESTIC_REPOSITORY_FILE": "/nonexistent",
        }):
            code, out, err = self.command("--password-file", self.recovery, apply=True)
        self.assertEqual(code, 0, err)
        self.assertIn("opened, 1 snapshot(s)", out)

    def test_the_rows_other_detail_keys_survive_the_record(self):
        connection = sqlite3.connect(str(self.db))
        try:
            detail = self.row()["detail"]
            detail.update({"dns": {"action": "deleted"},
                           "machine": {"server_id": "purge001"},
                           "reason": "drill"})
            connection.execute("UPDATE tenant_state SET detail = ? WHERE slug = ?",
                               (json.dumps(detail), SLUG))
            connection.commit()
        finally:
            connection.close()
        code, out, err = self.command("--password-file", self.recovery, apply=True)
        self.assertEqual(code, 0, err)
        row = self.row()["detail"]
        self.assertEqual(row["dns"], {"action": "deleted"})
        self.assertEqual(row["machine"], {"server_id": "purge001"})
        self.assertEqual(row["reason"], "drill")

    def test_a_file_at_the_repository_path_is_exit_four_without_calling_restic(self):
        shutil.rmtree(self.repo)
        self.repo.write_text("not a repository\n", encoding="utf-8")
        with mock.patch.object(
            purge, "check_repository", side_effect=AssertionError("restic was called")
        ):
            code, out, err = self.command(apply=True)
        self.assertEqual(code, 4)
        self.assertIn("not a directory", err)
        self.assertTrue(self.repo.is_file())

    def test_a_directory_with_a_config_and_no_keys_is_exit_four(self):
        # restic answers "wrong password or no key found" for this too; the
        # structure is what tells a keyless directory from a real repository.
        shutil.rmtree(self.repo)
        self.repo.mkdir()
        (self.repo / "config").write_text("not a restic config\n", encoding="utf-8")
        (self.repo / "keys").mkdir()
        code, out, err = self.command(apply=True)
        self.assertEqual(code, 4)
        self.assertIn(str(self.repo), err)
        self.assertIn("no key", err)
        self.assertTrue(self.repo.is_dir())
        self.assertNotIn("purged", self.row()["detail"]["backup"])

    def test_json_plan_lists_the_actions(self):
        code, out, err = self.command("--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["dry_run"])
        self.assertEqual([action["action"] for action in payload["actions"]],
                         ["backup.verify", "backup.delete", "sftp.revoke", "state.purge"])

    def test_json_apply_carries_the_purge_record(self):
        code, out, err = self.command("--json", apply=True)
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["command"], "purge")
        self.assertFalse(payload["dry_run"])
        self.assertEqual(payload["snapshot"], self.snapshot)
        self.assertEqual(payload["purged"]["path"], str(self.repo))


# ------------------------------------------------------------------ the BX11 exit

BX11_RW_ID = 392550
BX11_RO_ID = 392556
BX11_SERVER = "u728180-sub1.your-storagebox.de"
BX11_USERNAME = "u728180-sub1"
BX11_REPO = f"sftp:{BX11_USERNAME}@{BX11_SERVER}:data/restic"
BARE_PASSWORD = "bench-" + "purge-password-" + "2026-000000001"

# A restic stand-in that keeps the snapshot list in a file, so a forget is
# observable. `FAKE_LOCKED=1` is a repository the empty password cannot open
# (the production case); `FAKE_WRONG=1` is a supplied password that is wrong.
FAKE_BX11_RESTIC = textwrap.dedent(
    '''#!/usr/bin/python3
import json, os, sys
from pathlib import Path

state = Path(os.environ["FAKE_SNAPSHOTS"])
password_file = os.environ.get("RESTIC_PASSWORD_FILE")
with Path(os.environ["FAKE_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "tool": "restic",
        "argv": sys.argv[1:],
        "repo": os.environ.get("RESTIC_REPOSITORY"),
        "password_file": password_file,
        "password": Path(password_file).read_text() if password_file else None,
    }) + "\\n")
ids = json.loads(state.read_text()) if state.exists() else []
args = sys.argv[1:]
if "snapshots" in args:
    if os.environ.get("FAKE_WRONG") == "1":
        print("wrong password or no key found", file=sys.stderr)
        sys.exit(12)
    if os.environ.get("FAKE_LOCKED") == "1" and "--insecure-no-password" in args:
        print("wrong password or no key found", file=sys.stderr)
        sys.exit(12)
    print(json.dumps([{"id": value} for value in ids]))
    sys.exit(0)
if "forget" in args:
    ident = args[args.index("forget") + 2]
    state.write_text(json.dumps([value for value in ids if value != ident]))
    print("{}")
    sys.exit(0)
print("fake restic: unknown call %r" % (args,), file=sys.stderr)
sys.exit(2)
'''
)

# An sftp stand-in: a scratch tree, the batch grammar the purge uses, a log.
FAKE_SFTP = textwrap.dedent(
    '''#!/usr/bin/python3
import glob, json, os, sys
from pathlib import Path

root = Path(os.environ["FAKE_SFTP_ROOT"])
commands = sys.stdin.read().splitlines()
with Path(os.environ["FAKE_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"tool": "sftp", "argv": sys.argv[1:],
                             "commands": commands}) + "\\n")
for raw in commands:
    line = raw.strip()
    if not line:
        continue
    if line.startswith("-"):
        line = line[1:]
    verb, _, rest = line.partition(" ")
    rest = rest.strip()
    if verb == "ls":
        target = rest.split()[-1]
        path = root / target
        if path.is_dir():
            for child in sorted(path.iterdir()):
                print("%s/%s" % (target.rstrip("/"), child.name))
        else:
            print("Can't ls: %r not found" % ("/" + target))
    elif verb == "rm":
        for name in glob.glob(str(root / rest)):
            path = Path(name)
            if path.is_file():
                path.unlink()
    elif verb == "rmdir":
        try:
            (root / rest).rmdir()
        except OSError:
            pass
sys.exit(0)
'''
)


class BoxApi:
    """The two API shapes the exit uses: the plan GET, and the DELETEs."""

    def __init__(self):
        self.calls = []
        self.deleted = []
        self.actions = []

    def __call__(self, settings, method, path, body=None):
        self.calls.append({"method": method, "path": path, "body": body})
        if method == "GET" and "/actions/" not in path:
            return 200, {"storage_box": {
                "id": settings.box_id,
                "snapshot_plan": {"hour": 4, "minute": 30, "max_snapshots": 10,
                                  "day_of_week": None, "day_of_month": None},
            }}
        if method == "DELETE":
            ident = int(path.rsplit("/", 1)[1])
            self.deleted.append(ident)
            return 200, {"action": {"id": len(self.deleted), "status": "running"}}
        raise AssertionError("unexpected API call %s %s" % (method, path))

    def await_action(self, settings, payload):
        self.actions.append(payload)


class Bx11PurgeFixture(PurgeFixture):
    """The bench pair's shape: an env store, a locked repository, the box API."""

    def setUp(self):
        super().setUp()
        self.store = self.directory / "hetzner"
        self.store.mkdir(mode=0o700)
        self.token = self.directory / "hetzner-api-token"
        self.token.write_text("HETZNER_API_" + "TOKEN=placeholder\n", encoding="utf-8")
        self.token.chmod(0o600)
        self.config = self.make_config(
            hetzner_dir=str(self.store),
            hetzner_token=str(self.token),
            hetzner_box_id="709870",
            hetzner_api_base="https://api.hetzner.test/v1",
        )
        self.pair = provision.BackupPair(
            slug=SLUG, box_id=709870, box_server="u728180.your-storagebox.de",
            subaccount_id=BX11_RW_ID, subaccount_username=BX11_USERNAME,
            subaccount_server=BX11_SERVER, subaccount_home=f"tenants/{SLUG}",
            readonly_subaccount_id=BX11_RO_ID,
            readonly_subaccount_username="u728180-sub2",
            readonly_subaccount_server="u728180-sub2.your-storagebox.de",
            readonly_subaccount_home=f"tenants/{SLUG}/data",
            repo_url=BX11_REPO,
            readonly_repo_url="sftp:u728180-sub2@u728180-sub2.your-storagebox.de:restic",
            known_hosts=f"{BX11_SERVER} ssh-ed25519 AAAAbench",
            rw_private_key="[REDACTED PRIVATE KEY]\n",
            ro_private_key="[REDACTED PRIVATE KEY]\n",
            previous_instance="", provisioned_at="2026-11-25T17:14:31Z",
        )
        self.env_path = self.store / f"odile-{SLUG}.env"
        self.env_path.write_bytes(self.pair.to_env())
        self.api = BoxApi()
        for name, value in (("_hetzner_api", self.api),
                            ("_await_action", self.api.await_action)):
            patcher = mock.patch.object(provision, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.bin = self.directory / "bin"
        self.bin.mkdir()
        for name, body in (("restic", FAKE_BX11_RESTIC), ("sftp", FAKE_SFTP)):
            path = self.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        self.log = self.directory / "bx11-calls.jsonl"
        self.snapshot_state = self.directory / "snapshots.json"
        self.snapshot_id = "11" * 32
        self.snapshot_state.write_text(json.dumps([self.snapshot_id]), encoding="utf-8")
        self.password = self.directory / "restic.pass"
        self.password.write_text(BARE_PASSWORD + "\n", encoding="utf-8")
        self.sftp_root = self.directory / "box"
        self.sftp_root.mkdir()
        self.environment = mock.patch.dict(os.environ, {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_LOG": str(self.log),
            "FAKE_SNAPSHOTS": str(self.snapshot_state),
            "FAKE_SFTP_ROOT": str(self.sftp_root),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.repo_url = BX11_REPO
        self.seed(repo=BX11_REPO)

    def calls(self, tool):
        if not self.log.exists():
            return []
        entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        return [entry for entry in entries if entry.get("tool") == tool]

    def build_key_only_repository(self, *, repo="data/restic"):
        """A repository tree as sftp sees it: config, keys, the objects."""
        root = self.sftp_root / repo
        (root / "keys").mkdir(parents=True)
        (root / "snapshots").mkdir()
        (root / "index").mkdir()
        (root / "data" / "00").mkdir(parents=True)
        (root / "locks").mkdir()
        (root / "config").write_text("{}", encoding="utf-8")
        (root / "keys" / "abc123").write_text("key", encoding="utf-8")
        (root / "snapshots" / self.snapshot_id).write_text("{}", encoding="utf-8")
        (root / "index" / ("22" * 32)).write_text("{}", encoding="utf-8")
        (root / "data" / "00" / ("33" * 32)).write_text("blob", encoding="utf-8")
        (root / "locks" / "lock").write_text("lock", encoding="utf-8")
        return root

    def backup_record(self):
        return self.row()["detail"]["backup"]


class Bx11PurgeTest(Bx11PurgeFixture):
    """ODL-935: prune with the RW key, delete both halves, record, stay put."""

    def test_the_plan_names_the_snapshot_the_two_accounts_and_the_survivors(self):
        database = self.db.read_bytes()
        code, out, err = self.command("--password-file", self.password)
        self.assertEqual(code, 0, err)
        self.assertIn("purge: dry run (pass --apply to execute)", out)
        for action in ("backup.verify", "backup.prune", "subaccount.delete", "state.record"):
            self.assertIn(action, out)
        self.assertIn(self.snapshot_id, out)
        self.assertIn("restic forget --prune <id>", out)
        self.assertIn(str(BX11_RW_ID), out)
        self.assertIn(str(BX11_RO_ID), out)
        self.assertIn("04:30", out)
        self.assertIn("ten days", out)
        self.assertIn(DUE, out)
        self.assertEqual(self.db.read_bytes(), database)
        self.assertEqual(self.api.calls[0]["method"], "GET")
        self.assertEqual(self.api.deleted, [])
        self.assertEqual([entry["argv"][-3:] for entry in self.calls("restic")],
                         [["snapshots", "--json", "--no-lock"]])
        self.assertFalse(self.calls("sftp"), "the plan opened an sftp batch")

    def test_apply_prunes_with_restic_deletes_both_and_records(self):
        # restic leaves the object directories, empty: the recorded survivor
        # note must name exactly the tree this run left behind.
        self.build_key_only_repository()
        code, out, err = self.command("--password-file", self.password, apply=True)
        self.assertEqual(code, 0, err)
        self.assertIn("purge: applied", out)
        self.assertIn(self.snapshot_id[:8], out)
        restic = self.calls("restic")
        self.assertTrue(any(entry["argv"][-3:] == ["forget", "--prune", self.snapshot_id]
                            for entry in restic), restic)
        self.assertEqual(json.loads(self.snapshot_state.read_text(encoding="utf-8")), [])
        self.assertEqual(self.api.deleted, [BX11_RO_ID, BX11_RW_ID])
        self.assertEqual(len(self.api.actions), 2)
        backup = self.backup_record()
        self.assertTrue(backup["subaccount_deleted_at"].endswith("Z"))
        self.assertEqual(backup["subaccounts"]["read_write"]["id"], BX11_RW_ID)
        self.assertEqual(backup["subaccounts"]["read_only"]["id"], BX11_RO_ID)
        self.assertEqual(backup["pruned"]["mode"], "restic")
        self.assertEqual(backup["pruned"]["snapshots"], 1)
        self.assertEqual(backup["pruned"]["ids"], [self.snapshot_id])
        self.assertEqual(backup["keep_until"], DUE)
        self.assertEqual(backup["survivors"]["directory"], f"tenants/{SLUG}")
        self.assertEqual(backup["survivors"]["box_snapshots"]["kept"], 10)
        self.assertIn("ten days", backup["survivors"]["box_snapshots"]["note"])
        # The note is derived from the tree this run left: restic kept the four
        # object directories, empty, so the note may name them.
        note = backup["survivors"]["directory_note"]
        for name in ("data/", "index/", "snapshots/", "locks/"):
            self.assertIn(name, note)
        self.assertIn("config and keys/", note)
        self.assertNotIn(BARE_PASSWORD, out)
        self.assertNotIn(BARE_PASSWORD, err)

    def test_a_second_run_says_already_deleted_and_writes_nothing(self):
        first, out, err = self.command("--password-file", self.password, apply=True)
        self.assertEqual(first, 0, err)
        database = self.db.read_bytes()
        deleted_at = self.backup_record()["subaccount_deleted_at"]
        self.env_path.unlink()  # the second run must not read the store
        code, out, err = self.command(apply=True)
        self.assertEqual(code, 0, err)
        self.assertIn("purge: already deleted", out)
        self.assertIn(deleted_at, out)
        self.assertEqual(self.db.read_bytes(), database)
        self.assertEqual(self.api.deleted, [BX11_RO_ID, BX11_RW_ID])

    def test_without_a_password_the_objects_are_deleted_with_the_key(self):
        self.build_key_only_repository()
        with mock.patch.dict(os.environ, {"FAKE_LOCKED": "1"}):
            code, out, err = self.command(apply=True)
        self.assertEqual(code, 0, err)
        self.assertIn("purge: applied", out)
        self.assertIn("the repository password is the user's", out)
        root = self.sftp_root / "data" / "restic"
        self.assertEqual(sorted(child.name for child in root.iterdir()),
                         ["config", "keys"])
        self.assertEqual([child.name for child in (root / "keys").iterdir()], ["abc123"])
        backup = self.backup_record()
        self.assertEqual(backup["pruned"]["mode"], "key-only")
        self.assertEqual(backup["pruned"]["ids"], [self.snapshot_id])
        self.assertEqual(self.api.deleted, [BX11_RO_ID, BX11_RW_ID])
        self.assertEqual(self.api.calls[0]["method"], "GET")
        # The recorded survivor sentence follows the post-prune tree: this mode
        # removes the four object directories, so the note must not name them.
        note = backup["survivors"]["directory_note"]
        self.assertIn("config and keys/", note)
        for gone in ("data/", "index/", "snapshots/", "locks/"):
            self.assertNotIn(gone, note)

    def test_the_key_only_plan_still_names_the_snapshot_ids(self):
        self.build_key_only_repository()
        with mock.patch.dict(os.environ, {"FAKE_LOCKED": "1"}):
            code, out, err = self.command("--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["actions"][0]["snapshots"], 1)
        self.assertEqual(payload["actions"][0]["ids"], [self.snapshot_id])
        self.assertIn("read-write key over sftp", payload["actions"][0]["via"])
        self.assertFalse(self.calls("sftp")[0]["commands"][0].startswith("rm"),
                         "the plan deleted something")

    def test_a_missing_env_file_refuses_by_name_and_path(self):
        self.env_path.unlink()
        database = self.db.read_bytes()
        code, out, err = self.command(apply=True)
        self.assertEqual(code, 2)
        self.assertIn(str(self.env_path), err)
        self.assertIn("store of record is missing", err)
        self.assertEqual(self.db.read_bytes(), database)
        self.assertEqual(self.api.deleted, [])
        self.assertFalse(self.calls("restic"))

    def test_the_store_of_record_must_be_this_tenants(self):
        other = self.pair.__class__(**{**self.pair.__dict__, "slug": "odl-other"})
        self.env_path.write_bytes(other.to_env())
        code, out, err = self.command("--password-file", self.password, apply=True)
        self.assertEqual(code, 2)
        self.assertIn("records 'odl-other'", err)
        self.assertEqual(self.api.deleted, [])

    def test_the_repository_must_be_the_read_write_sub_account(self):
        self.set_backup(repo="sftp:u728180-sub2@u728180-sub2.your-storagebox.de:restic")
        code, out, err = self.command("--password-file", self.password, apply=True)
        self.assertEqual(code, 2)
        self.assertIn("not the read-write sub-account", err)
        self.assertEqual(self.api.deleted, [])
        self.assertFalse(self.calls("restic"))

    def test_a_wrong_password_is_exit_four_and_nothing_is_deleted(self):
        database = self.db.read_bytes()
        with mock.patch.dict(os.environ, {"FAKE_WRONG": "1"}):
            code, out, err = self.command("--password-file", self.password, apply=True)
        self.assertEqual(code, 4)
        self.assertIn("wrong password", err)
        self.assertEqual(self.api.deleted, [])
        self.assertEqual(self.db.read_bytes(), database)
        self.assertNotIn(BARE_PASSWORD, err)

    def test_a_repo_hex_directory_is_pruned_without_an_api_call(self):
        self.set_backup(repo=f"sftp:{BX11_USERNAME}@{BX11_SERVER}:data/repo-ab12")
        connection = sqlite3.connect(str(self.db))
        try:
            connection.execute("UPDATE tenant_state SET state = 'ready' WHERE slug = ?", (SLUG,))
            connection.commit()
        finally:
            connection.close()
        code, out, err = self.command("--password-file", self.password, apply=True)
        self.assertEqual(code, 0, err)
        self.assertIn("purge: applied", out)
        self.assertIn("a start-fresh directory", out)
        self.assertEqual(self.api.calls, [])
        backup = self.backup_record()
        self.assertEqual(backup["prunes"][-1]["repo"],
                         f"sftp:{BX11_USERNAME}@{BX11_SERVER}:data/repo-ab12")
        self.assertEqual(backup["keep_until"], DUE)
        database = self.db.read_bytes()
        code, out, err = self.command("--password-file", self.password, apply=True)
        self.assertEqual(code, 0, err)
        self.assertIn("purge: already pruned", out)
        self.assertEqual(self.db.read_bytes(), database)

    def test_the_plan_json_carries_the_actions(self):
        code, out, err = self.command("--password-file", self.password, "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["dry_run"])
        self.assertEqual([action["action"] for action in payload["actions"]],
                         ["backup.verify", "backup.prune", "subaccount.delete",
                          "subaccount.delete", "state.record"])


class Bx11PiecesTest(unittest.TestCase):
    """The URL grammar, the branch choice and the password file."""

    def test_the_sftp_url_keeps_the_colon_form_path_and_reads_a_port_only_in_the_url_form(self):
        self.assertEqual(purge.sftp_url("sftp:u@h:data/restic"),
                         ("u", "h", 22, "data/restic"))
        # restic reads `22` as the first path component in the colon form.
        self.assertEqual(purge.sftp_url("sftp:u@h:22/data/restic"),
                         ("u", "h", 22, "22/data/restic"))
        self.assertEqual(purge.sftp_url("sftp://u@h:2222/data/restic"),
                         ("u", "h", 2222, "data/restic"))
        for bad in ("sftp:u@h", "sftp:u@h:", "sftp:u@h:../restic", "https://h/x"):
            with self.subTest(repo=bad):
                with self.assertRaises(purge.UsageError):
                    purge.sftp_url(bad)

    def test_the_branch_is_the_rows_url_not_a_flag(self):
        self.assertTrue(purge.bx11_repository("sftp:u@h:data/restic", "odl-1"))
        self.assertTrue(purge.bx11_repository("sftp:u@h:data/repo-ab12", "odl-1"))
        # t_9d6fb595's HQ-root rows keep the absolute `/tenants/<slug>` path.
        self.assertFalse(purge.bx11_repository("sftp:u@h:/tenants/odl-1", "odl-1"))
        self.assertFalse(purge.bx11_repository("/srv/odile-backup/tenants/odl-1", "odl-1"))

    def test_a_recovery_file_and_a_bare_password_file_both_open(self):
        directory = Path(tempfile.mkdtemp(prefix="odile-purge-pass-"))
        self.addCleanup(shutil.rmtree, directory, True)
        recovery = directory / "recovery.txt"
        recovery.write_text("Odile recovery file\nBackup password: %s\n" % BARE_PASSWORD,
                            encoding="utf-8")
        self.assertEqual(purge._read_password(recovery, bare=True), BARE_PASSWORD)
        bare = directory / "restic.pass"
        bare.write_text(BARE_PASSWORD + "\n", encoding="utf-8")
        self.assertEqual(purge._read_password(bare, bare=True), BARE_PASSWORD)
        with self.assertRaises(purge.UsageError):
            purge._read_password(bare)  # the HQ-root path keeps the strict grammar
        short = directory / "short.pass"
        short.write_text("short\n", encoding="utf-8")
        with self.assertRaises(purge.UsageError):
            purge._read_password(short, bare=True)


if __name__ == "__main__":
    unittest.main()
