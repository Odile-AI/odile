#!/usr/bin/python3
"""The forbid class must run, and it must fail closed; the allowlist must read true.

`tools/leak_scan.py` answers whether a tree names a real machine: an address, a
mailbox, a home path, or a literal from the list of vendor ids and account ids
the operator must never publish. That last class is only as good as its list. On
2026-11-15 the class was dead twice over: the built tree carried no
`leak-forbid.txt`, so the default never resolved, and a `--forbid-file` naming a
file that did not exist was a silent PASS. A missing list looked exactly like a
clean tree.

These tests pin the two behaviours the drill depends on: a planted forbidden
literal fails and its removal passes, and a named list that does not exist is a
finding and exit 2. They also pin the smaller pieces that keep the class honest:
the list is not scanned for its own literals, an absent default is printed (never
a silent skip), and a malformed list row is a finding. `ForbiddenPath` pins the
same list read against a file's *name*: a tree publishes its paths as well as its
bytes, so a file named with a literal fails even when its contents are clean.

The second half pins the allowlist's own record, which is the other way a gate
goes quiet: a `constant` row expires `never`, a `debt` row carries the date of
the card that clears it and fails past it, a row whose reason contains its own
value is a finding, and the count of waived credentials is printed on every run.
The header's format line and the scanner's docstring must name the same fields,
because they are the two places a reader learns the format.

Every test runs the real scanner against a throwaway git repository, so the
`git ls-files` walk and the exit codes are the ones the gate uses.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNER = ROOT / "tools" / "leak_scan.py"
ALLOWLIST = ROOT / "leak-allow.tsv"
LABEL = "probe-box"
LITERAL = "deadbeefcafe"
# Assembled from parts so the scanner's own tree does not carry the probe address:
# a test that plants a leak must not itself be a leak, or the gate it tests fails.
PROBE_IP = ".".join(["45", "33", "32", "156"])
PROBE_IP_2 = ".".join(["45", "33", "32", "157"])
# The bare vendor-id positions: synthetic 8-hex tokens and a synthetic name,
# never a live id. The public build's stand-ins (`0badc0de`, `odl-fake01`) are
# the negative control, so a stand-in stays a stand-in in the new positions too.
PROBE_ID = "ab12" + "cd34"
PROBE_ID_2 = "5c0f" + "fe12"
PROBE_HOST = "probe" + "host1"
STAND_IN_ID = "0bad" + "c0de"
STAND_IN_HOST = "odl-" + "fake01"
# The live dev account id and the scrub's stand-in for it, assembled from parts
# so this file does not carry either literal in a position the gate reads.
DEV_ID = "lkbxu" + "6i9"
SCRUB_STAND_IN_ID = "0acc" + "0001"


def git_env() -> dict[str, str]:
    """A clean environment: a fixture repo must never inherit GIT_DIR.

    `.githooks/pre-commit` runs the suite with GIT_DIR / GIT_INDEX_FILE
    pointing at the real repository, and in a linked worktree — every card
    worktree — both are absolute. An inherited GIT_DIR makes `git init` in
    the fixture commit *into the caller's worktree* and the scanner's
    `git ls-files` walk read the outer tree instead of the fixture (the
    ODL-101 bare-worktree incident). Every child this file spawns gets this
    environment: the fixture git calls and both scanner calls, because the
    scanner shells out to git itself.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env.update(
        {
            "GIT_AUTHOR_NAME": "Probe",
            "GIT_AUTHOR_EMAIL": "probe@example.test",
            "GIT_COMMITTER_NAME": "Probe",
            "GIT_COMMITTER_EMAIL": "probe@example.test",
        }
    )
    return env



class ForbidClass(unittest.TestCase):
    """A git repo with one clean file; each test adds the list and the probe."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, env=git_env(), check=True)
        (self.repo / "leak-allow.tsv").write_text(
            "# no exceptions in this fixture\n", encoding="utf-8")
        (self.repo / "keep.txt").write_text("nothing private here\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def scan(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCANNER), "--tree", *args],
            cwd=self.repo, env=git_env(), capture_output=True, text=True, timeout=120)

    def commit(self, message: str = "fixture") -> None:
        subprocess.run(["git", "add", "-A"], cwd=self.repo, env=git_env(), check=True)
        subprocess.run(
            ["git", "-c", "user.email=probe@example.test", "-c", "user.name=probe",
             "commit", "-qm", message],
            cwd=self.repo, env=git_env(), check=True)

    def write_list(self) -> None:
        target = self.repo / "docs" / "ops" / "leak-forbid.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# label<TAB>regex\n{LABEL}\t{LITERAL}\n", encoding="utf-8")

    def test_a_named_list_that_does_not_exist_exits_2(self) -> None:
        result = self.scan("--forbid-file", "missing-forbid.txt")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("FINDING allow-file", result.stdout)
        self.assertIn("missing-forbid.txt", result.stdout)

    def write_tar(self, member: str, payload: bytes, name: str = "bundle.tar.gz") -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            info = tarfile.TarInfo(member)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        (self.repo / name).write_bytes(buffer.getvalue())

    def write_zip(self, member: str, payload: bytes, name: str = "bundle.zip") -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(member, payload)
        (self.repo / name).write_bytes(buffer.getvalue())

    def test_a_planted_forbidden_literal_fails_then_passes(self) -> None:
        self.write_list()
        probe = self.repo / "probe.txt"
        probe.write_text(f"vendor box {LITERAL}\n", encoding="utf-8")
        failed = self.scan()
        self.assertEqual(failed.returncode, 1, failed.stdout)
        self.assertIn("FINDING forbidden-value", failed.stdout)
        self.assertIn(f"({LABEL})", failed.stdout)
        probe.unlink()
        passed = self.scan()
        self.assertEqual(passed.returncode, 0, passed.stdout)
        self.assertIn("PASS: nothing to leak", passed.stdout)

    def test_a_forbidden_literal_inside_a_tar_member_fails(self) -> None:
        # The forbid class read a whole file's text but not an archive member's;
        # a literal planted in a member passed the class the list exists for.
        self.write_list()
        self.write_tar("member.txt", f"vendor box {LITERAL}\n".encode("utf-8"))
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FINDING forbidden-value", result.stdout)
        self.assertIn("bundle.tar.gz!member.txt:1", result.stdout)
        self.assertIn(f"({LABEL})", result.stdout)

    def test_a_forbidden_literal_inside_a_zip_member_fails(self) -> None:
        self.write_list()
        self.write_zip("member.txt", f"vendor box {LITERAL}\n".encode("utf-8"))
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FINDING forbidden-value", result.stdout)
        self.assertIn("bundle.zip!member.txt:1", result.stdout)

    def test_a_forbidden_literal_outside_an_archive_fails(self) -> None:
        # The same member bytes outside the archive: the wrapper is not what
        # makes the literal a finding, so the archive test above isolates it.
        self.write_list()
        (self.repo / "member.txt").write_text(f"vendor box {LITERAL}\n", encoding="utf-8")
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        findings = [line for line in result.stdout.splitlines()
                    if line.startswith("FINDING forbidden-value")]
        self.assertEqual(len(findings), 1, result.stdout)
        self.assertIn("member.txt:1", findings[0])
        self.assertNotIn("!", findings[0])

    def test_a_benign_archive_member_is_not_a_finding(self) -> None:
        self.write_list()
        self.write_tar("member.txt", b"nothing private here\n")
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("PASS: nothing to leak", result.stdout)

    def test_the_list_itself_is_not_a_finding(self) -> None:
        # The list carries the literals by definition; if the walk scanned it,
        # every label would be its own finding and the gate could never pass.
        self.write_list()
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("forbid list:     docs/ops/leak-forbid.txt (1 labels)", result.stdout)

    def test_an_absent_default_is_printed_not_silent(self) -> None:
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("absent (docs/ops/leak-forbid.txt)", result.stdout)
        self.assertIn("forbidden-value and forbidden-path classes did not run", result.stdout)

    def test_a_malformed_list_row_is_a_finding(self) -> None:
        target = self.repo / "docs" / "ops" / "leak-forbid.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# label<TAB>regex\n{LABEL} no-tab-here\n", encoding="utf-8")
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FINDING forbid-file", result.stdout)
        self.assertIn("expected label<TAB>regex", result.stdout)

    def test_history_scans_the_probe_but_not_the_list(self) -> None:
        # The published repository carries the list from its first commit, so
        # history mode must skip the list by path or every publish re-finds it.
        self.write_list()
        (self.repo / "probe.txt").write_text(f"vendor box {LITERAL}\n", encoding="utf-8")
        self.commit()
        result = subprocess.run(
            [sys.executable, str(SCANNER), "--history"],
            cwd=self.repo, env=git_env(), capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 1, result.stdout)
        findings = [line for line in result.stdout.splitlines()
                    if line.startswith("FINDING forbidden-value")]
        self.assertEqual(len(findings), 1, result.stdout)
        self.assertIn("probe.txt", findings[0])


class ForbiddenPath(unittest.TestCase):
    """A path is a publication surface: the same list, matched against a name.

    ``scan_tree`` walked ``git ls-files`` and opened each file, so it read every
    file's *contents* and never its name: a file called
    ``servury_reseller_key_<dev account id>.txt`` shipped with clean bytes while
    both scanners said PASS (measured 2026-11-19, the gap card ``t_b781c1f4``
    closed). These tests pin the class that closes it — the tree gate fails on a
    name, a scrub stand-in in a name is not a finding, an archive member's name
    is a name, an allowlist row waives it only under the ``forbidden-path``
    rule, and the history scan sees a name a later commit deleted.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, env=git_env(), check=True)
        (self.repo / "leak-allow.tsv").write_text(
            "# no exceptions in this fixture\n", encoding="utf-8")
        (self.repo / "keep.txt").write_text("nothing private here\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def scan(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCANNER), "--tree", *args],
            cwd=self.repo, env=git_env(), capture_output=True, text=True, timeout=120)

    def history(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCANNER), "--history", *args],
            cwd=self.repo, env=git_env(), capture_output=True, text=True, timeout=120)

    def commit(self, message: str = "fixture") -> None:
        subprocess.run(["git", "add", "-A"], cwd=self.repo, env=git_env(), check=True)
        subprocess.run(
            ["git", "-c", "user.email=probe@example.test", "-c", "user.name=probe",
             "commit", "-qm", message],
            cwd=self.repo, env=git_env(), check=True)

    def write_list(self) -> None:
        target = self.repo / "docs" / "ops" / "leak-forbid.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"# label<TAB>regex\n{LABEL}\t{LITERAL}\n", encoding="utf-8")

    def write_allow(self, *rows: str) -> None:
        (self.repo / "leak-allow.tsv").write_text(
            "# fixture allowlist\n" + "".join(rows), encoding="utf-8")

    @staticmethod
    def row(value: str, rule: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return "\t".join([rule, "*", f"sha256:{digest}", "constant", "no",
                          "a fixture path", "never"]) + "\n"

    def write_tar(self, member: str, payload: bytes, name: str = "bundle.tar.gz") -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            info = tarfile.TarInfo(member)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        (self.repo / name).write_bytes(buffer.getvalue())

    def test_a_file_named_with_a_forbidden_literal_fails_then_passes(self) -> None:
        self.write_list()
        probe = self.repo / f"probe_{LITERAL}.txt"
        probe.write_text("a plain marker file\n", encoding="utf-8")
        failed = self.scan()
        self.assertEqual(failed.returncode, 1, failed.stdout)
        self.assertIn("FINDING forbidden-path", failed.stdout)
        self.assertIn(f"probe_{LITERAL}.txt:1", failed.stdout)
        self.assertIn(f"sha256:{hashlib.sha256(LITERAL.encode()).hexdigest()[:12]}", failed.stdout)
        self.assertIn(f"len={len(LITERAL)}", failed.stdout)
        self.assertIn(f"({LABEL})", failed.stdout)
        # The bytes are clean, so the contents class must not fire: this is the
        # name, not a second view of the same bytes.
        self.assertNotIn("FINDING forbidden-value", failed.stdout)
        probe.unlink()
        passed = self.scan()
        self.assertEqual(passed.returncode, 0, passed.stdout)
        self.assertIn("PASS: nothing to leak", passed.stdout)

    def test_a_skipped_suffix_cannot_hide_a_name(self) -> None:
        # `.sig` bytes are never opened (see SKIP_SUFFIXES), so the path read
        # has to happen before the suffix skip. Moving `scan_forbidden_path`
        # below it leaves this case green — this test is that mutation's
        # witness, and the name is a publication surface exactly as a `.txt`
        # name is.
        self.write_list()
        probe = self.repo / f"probe_{LITERAL}.txt.sig"
        probe.write_text("a detached signature\n", encoding="utf-8")
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FINDING forbidden-path", result.stdout)
        self.assertIn(f"probe_{LITERAL}.txt.sig:1", result.stdout)

    def test_a_stand_in_in_a_file_name_is_not_a_path_finding(self) -> None:
        # The builder's scrub writes this stand-in for the dev account id; a
        # stand-in is a stand-in in a path exactly as it is in a file's bytes.
        self.write_list()
        (self.repo / f"servury_reseller_key_{SCRUB_STAND_IN_ID}.txt").write_text(
            "a plain marker file\n", encoding="utf-8")
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("PASS: nothing to leak", result.stdout)

    def test_a_forbidden_literal_in_a_directory_name_fails(self) -> None:
        # The name is not only the leaf: a directory component is published too.
        self.write_list()
        (self.repo / LITERAL).mkdir()
        (self.repo / LITERAL / "notes.txt").write_text("a plain marker file\n", encoding="utf-8")
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FINDING forbidden-path", result.stdout)
        self.assertIn(f"{LITERAL}/notes.txt:1", result.stdout)

    def test_an_archive_member_name_is_a_path(self) -> None:
        # A member's name is published inside the archive, exactly as its bytes
        # are; the contents rule already reads the member's data.
        self.write_list()
        self.write_tar(f"{LITERAL}.txt", b"a plain marker file\n")
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FINDING forbidden-path", result.stdout)
        self.assertIn(f"bundle.tar.gz!{LITERAL}.txt:1", result.stdout)

    def test_a_path_finding_is_waived_only_by_a_forbidden_path_row(self) -> None:
        # A distinct class exists so the allowlist names the shape it waives: a
        # `forbidden-value` row pins the same digest and still does not waive a
        # name, and a `forbidden-path` row does.
        self.write_list()
        (self.repo / f"probe_{LITERAL}.txt").write_text("a plain marker file\n", encoding="utf-8")
        self.write_allow(self.row(LITERAL, "forbidden-value"))
        wrong = self.scan()
        self.assertEqual(wrong.returncode, 1, wrong.stdout)
        self.assertIn("FINDING forbidden-path", wrong.stdout)
        self.write_allow(self.row(LITERAL, "forbidden-path"))
        right = self.scan()
        self.assertEqual(right.returncode, 0, right.stdout)
        self.assertIn("allowlisted:    1", right.stdout)

    def test_the_dev_account_id_in_a_file_name_fails_the_gate(self) -> None:
        # The card's own shape: the reseller key file named with the live dev
        # account id, contents clean. The id and the label are assembled here,
        # so the test file itself is not a leak.
        (self.repo / "docs" / "ops").mkdir(parents=True, exist_ok=True)
        (self.repo / "docs" / "ops" / "leak-forbid.txt").write_text(
            f"servury-dev-account\t{DEV_ID}\n", encoding="utf-8")
        planted = f"servury_reseller_key_{DEV_ID}.txt"
        (self.repo / planted).write_text("a plain marker file\n", encoding="utf-8")
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FINDING forbidden-path", result.stdout)
        self.assertIn(f"{planted}:1", result.stdout)
        self.assertIn("(servury-dev-account)", result.stdout)

    def test_a_path_deleted_before_the_push_is_still_in_history(self) -> None:
        # The tree scan at HEAD cannot see a name a later commit deleted; the
        # history walk reads every commit's paths, which is the only layer that
        # sees a commit that never reaches a built tree.
        self.write_list()
        probe = self.repo / f"probe_{LITERAL}.txt"
        probe.write_text("a plain marker file\n", encoding="utf-8")
        self.commit()
        probe.unlink()
        self.commit("delete the probe path")
        result = self.history()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FINDING forbidden-path", result.stdout)
        self.assertIn(f"probe_{LITERAL}.txt:1", result.stdout)
        self.assertIn("commit ", result.stdout)

    def test_an_archive_member_name_deleted_before_the_push_is_still_in_history(self) -> None:
        # The same shape one layer in: the archive path is clean, its member's
        # name is not, and the archive itself is gone from HEAD.
        self.write_list()
        self.write_tar(f"{LITERAL}.txt", b"a plain marker file\n")
        self.commit()
        (self.repo / "bundle.tar.gz").unlink()
        self.commit("delete the archive")
        result = self.history()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FINDING forbidden-path", result.stdout)
        self.assertIn(f"bundle.tar.gz!{LITERAL}.txt:1", result.stdout)


class AllowlistRecord(unittest.TestCase):
    """The allowlist is a record: its format, its classes, its reasons, its count."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, env=git_env(), check=True)
        (self.repo / "leak-allow.tsv").write_text("# no exceptions in this fixture\n",
                                                   encoding="utf-8")
        (self.repo / "keep.txt").write_text("nothing private here\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def scan(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCANNER), "--tree"],
            cwd=self.repo, env=git_env(), capture_output=True, text=True, timeout=120)

    def write_probe(self, *values: str) -> None:
        (self.repo / "probe.txt").write_text(
            "".join(f"host {value}\n" for value in values), encoding="utf-8")

    def write_allow(self, *rows: str) -> None:
        (self.repo / "leak-allow.tsv").write_text(
            "# fixture allowlist\n" + "".join(rows), encoding="utf-8")

    @staticmethod
    def row(value: str, *, klass: str = "constant", credential: str = "no",
            reason: str = "a fixture value", expires: str = "never",
            rule: str = "public-ip", path: str = "*") -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return "\t".join([rule, path, f"sha256:{digest}", klass, credential,
                          reason, expires]) + "\n"

    def test_an_expired_debt_row_is_a_finding(self) -> None:
        self.write_probe(PROBE_IP)
        self.write_allow(self.row(PROBE_IP, klass="debt", expires="2026-01-01"))
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FINDING stale-allow", result.stdout)
        self.assertIn("expired 2026-01-01", result.stdout)

    def test_a_live_debt_row_passes(self) -> None:
        self.write_probe(PROBE_IP)
        self.write_allow(self.row(PROBE_IP, klass="debt", expires="2027-06-30"))
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("allowlisted:    1", result.stdout)

    def test_a_constant_row_expires_never_and_passes(self) -> None:
        self.write_probe(PROBE_IP)
        self.write_allow(self.row(PROBE_IP))
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("allowlisted:    1", result.stdout)

    def test_an_expired_constant_row_is_not_a_finding(self) -> None:
        # `class` decides what the expiry means: a constant never expires, so a
        # date on one is accepted and means nothing (the card's own sentence).
        self.write_probe(PROBE_IP)
        self.write_allow(self.row(PROBE_IP, expires="2026-01-01"))
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("allowlisted:    1", result.stdout)

    def test_a_malformed_expiry_is_a_format_finding(self) -> None:
        self.write_probe(PROBE_IP)
        self.write_allow(self.row(PROBE_IP, expires="next year"))
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FINDING allow-file", result.stdout)
        self.assertIn("expires must be never or YYYY-MM-DD", result.stdout)

    def test_a_debt_row_cannot_carry_never(self) -> None:
        self.write_probe(PROBE_IP)
        self.write_allow(self.row(PROBE_IP, klass="debt", expires="never"))
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("needs the date of the card that clears it", result.stdout)

    def test_the_old_five_field_row_is_rejected(self) -> None:
        self.write_probe(PROBE_IP)
        digest = hashlib.sha256(PROBE_IP.encode("utf-8")).hexdigest()[:12]
        self.write_allow("\t".join(["public-ip", "*", f"sha256:{digest}",
                                     "a fixture value", "2027-12-31"]) + "\n")
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("expected 7 tab-separated fields", result.stdout)

    def test_a_reason_containing_its_own_value_is_a_finding(self) -> None:
        # Rows 17 and 18 of the live allowlist did this: the row matched the
        # reason text that described it, so "every row matched" proved nothing.
        self.write_probe(PROBE_IP)
        self.write_allow(self.row(PROBE_IP, reason=f"the probe address {PROBE_IP} must be rejected"))
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FINDING self-satisfying", result.stdout)
        self.assertIn("reason contains its own value", result.stdout)

    def test_the_summary_counts_rows_credentials_and_debt(self) -> None:
        self.write_probe(PROBE_IP, PROBE_IP_2)
        self.write_allow(self.row(PROBE_IP, credential="yes"),
                         self.row(PROBE_IP_2))
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("allowlist:       2 rows, 1 credentials, 0 debt", result.stdout)

    def test_the_header_and_the_docstring_name_the_same_format(self) -> None:
        header = next(line for line in ALLOWLIST.read_text(encoding="utf-8").splitlines()
                      if line.lstrip("# ").startswith("rule "))
        docstring = next(line for line in SCANNER.read_text(encoding="utf-8").splitlines()
                         if "<TAB>path-glob" in line)
        self.assertEqual("".join(header.lstrip("#").split()),
                         "".join(docstring.split()))
        self.assertIn("<TAB>class<TAB>credential<TAB>reason<TAB>expires",
                      "".join(header.split()))

    def test_the_live_allowlist_loads_with_no_format_finding(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("leak_scan", SCANNER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["leak_scan"] = module  # dataclasses resolves its own module by name
        spec.loader.exec_module(module)
        rows, problems = module.load_allow(ROOT)
        self.assertEqual(problems, [])
        # Four credentials at the merged head: the published demo password (its
        # login-page row and the README copy of the same value) and the two
        # demo secrets (the spent image seed token, the retired session seed).
        self.assertEqual(sum(1 for row in rows if row.credential == "yes"), 4)

    def test_a_planted_chip_id_in_a_fixture_fails_the_gate(self) -> None:
        import base64
        import json
        raw = bytearray(1184)
        raw[0:4] = (5).to_bytes(4, "little")
        raw[0x90:0xC0] = b"\x11" * 48
        raw[0x1A0:0x1E0] = bytes.fromhex("0123456789abcdef" * 8)
        fixture = self.repo / "synthetic-report.json"
        fixture.write_text(json.dumps({
            "report_b64": base64.b64encode(bytes(raw)).decode("ascii"),
        }), encoding="utf-8")
        first = self.scan()
        self.assertEqual(first.returncode, 1, first.stdout)
        self.assertIn("machine-identity", first.stdout)
        # The generator's marker is not silicon; the same fixture passes.
        raw[0x1A0:0x1E0] = b"ODILE-SYNTHETIC-CHIP-ID".ljust(64, b"\x00")
        fixture.write_text(json.dumps({
            "report_b64": base64.b64encode(bytes(raw)).decode("ascii"),
        }), encoding="utf-8")
        second = self.scan()
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertIn("PASS:", second.stdout)

    def test_a_pem_certificate_with_a_live_chip_id_fails_the_gate(self) -> None:
        import base64
        import json
        hwid = bytes.fromhex("0123456789abcdef" * 8)
        oid = bytes.fromhex("2b060104019c780104")
        inner = (b"\x02\x01\x01" + b"\x06\x09" + oid
                 + b"\x04\x40" + hwid)
        der = (b"\x30" + bytes([len(inner) + 2])
               + b"\x30" + bytes([len(inner)]) + inner)
        pem = ("-----BEGIN CERTIFICATE-----\n"
               + base64.b64encode(der).decode("ascii") + "\n"
               "-----END CERTIFICATE-----\n")
        (self.repo / "fixture.pem").write_text(pem, encoding="utf-8")
        first = self.scan()
        self.assertEqual(first.returncode, 1, first.stdout)
        self.assertIn("VCEK HWID (chip id) (PEM)", first.stdout)
        # A certificate without the HWID extension is not a VCEK.
        inner = b"\x02\x01\x01"
        der = (b"\x30" + bytes([len(inner) + 2])
               + b"\x30" + bytes([len(inner)]) + inner)
        pem = ("-----BEGIN CERTIFICATE-----\n"
               + base64.b64encode(der).decode("ascii") + "\n"
               "-----END CERTIFICATE-----\n")
        (self.repo / "fixture.pem").write_text(pem, encoding="utf-8")
        second = self.scan()
        self.assertEqual(second.returncode, 0, second.stdout)


class BareVendorIdPositions(unittest.TestCase):
    """The positions a live vendor id used, and the look-alikes it does not.

    A card's tree hid the tenant's instance id and slug in three shapes the
    bare rules did not pin: an unlabelled assignment of a quoted token, a
    `/manage/<id>/` path whose link text is the machine's name, and two ids
    written as one whitespace-separated list on a `printf` line. `make check`
    was green on that tree; only the publication scrub pass caught it. Each
    test runs the real scanner against a throwaway git repository and asserts
    the exit code, so the rule and the walk that carries it are one behaviour.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, env=git_env(), check=True)
        (self.repo / "leak-allow.tsv").write_text(
            "# no exceptions in this fixture\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def scan(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCANNER), "--tree"],
            cwd=self.repo, env=git_env(), capture_output=True, text=True, timeout=120)

    def write(self, name: str, text: str) -> None:
        (self.repo / name).write_text(text, encoding="utf-8")

    @staticmethod
    def reference(value: str) -> str:
        # The finding line names the path and line, then the digest of the
        # value; the value itself is never echoed, so the digest is the pin.
        return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

    def test_an_unlabelled_quoted_assignment_fails_then_the_stand_in_passes(self) -> None:
        self.write("probe.py", f'OTHER = "{PROBE_ID}"\n')
        failed = self.scan()
        self.assertEqual(failed.returncode, 1, failed.stdout)
        self.assertIn("FINDING vendor-resource-id", failed.stdout)
        self.assertIn(f"probe.py:1 {self.reference(PROBE_ID)}", failed.stdout)
        self.assertIn("bare instance id", failed.stdout)
        self.write("probe.py", f'OTHER = "{STAND_IN_ID}"\n')
        passed = self.scan()
        self.assertEqual(passed.returncode, 0, passed.stdout)
        self.assertIn("PASS: nothing to leak", passed.stdout)

    def test_a_manage_path_and_its_link_text_fail_then_the_stand_ins_pass(self) -> None:
        self.write("dashboard.html", f'<a href="/manage/{PROBE_ID}/">{PROBE_HOST}</a>\n')
        failed = self.scan()
        self.assertEqual(failed.returncode, 1, failed.stdout)
        self.assertIn(f"dashboard.html:1 {self.reference(PROBE_ID)}", failed.stdout)
        self.assertIn(f"dashboard.html:1 {self.reference(PROBE_HOST)}", failed.stdout)
        self.assertIn("bare host name", failed.stdout)
        self.write("dashboard.html",
                   f'<a href="/manage/{STAND_IN_ID}/">{STAND_IN_HOST}</a>\n')
        passed = self.scan()
        self.assertEqual(passed.returncode, 0, passed.stdout)
        self.assertIn("PASS: nothing to leak", passed.stdout)

    def test_a_bare_whitespace_list_of_ids_fails_then_the_stand_ins_pass(self) -> None:
        # The recycle runbook restored the fence with one `printf` line naming
        # two machines; neither token sat in any of the labelled positions.
        self.write("restore.sh", f"printf '%s\\n' {PROBE_ID} {PROBE_ID_2} > fence\n")
        failed = self.scan()
        self.assertEqual(failed.returncode, 1, failed.stdout)
        self.assertIn(f"restore.sh:1 {self.reference(PROBE_ID)}", failed.stdout)
        self.assertIn(f"restore.sh:1 {self.reference(PROBE_ID_2)}", failed.stdout)
        self.write("restore.sh", f"printf '%s\\n' {STAND_IN_ID} cafebabe > fence\n")
        passed = self.scan()
        self.assertEqual(passed.returncode, 0, passed.stdout)
        self.assertIn("PASS: nothing to leak", passed.stdout)

    def test_the_look_alikes_are_not_bare_ids(self) -> None:
        # The shapes an 8-hex token is far more often than a machine id.
        self.write("probe.txt", "\n".join([
            f"merged pre-flip at `{PROBE_ID}`",
            f"the tint is #{PROBE_ID}",
            f"uuid {PROBE_ID}-0000-4000-8000-000000000000",
            f"checksum sha256:{PROBE_ID}" + "0" * 56,
        ]) + "\n")
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("PASS: nothing to leak", result.stdout)


if __name__ == "__main__":
    unittest.main()
