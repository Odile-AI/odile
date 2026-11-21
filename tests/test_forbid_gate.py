#!/usr/bin/python3
"""The forward leak gates: deny list, pre-push hook, placeholders, host template.

`tools/leak_scan.py`'s forbidden-value class was only as good as the list it
was handed, and until ODL-911 the HQ checkout never handed it one. These tests
pin the four forward gates the card adds:

* a tracked file carrying a real HQ literal fails, and the documented
  placeholder vocabulary passes (RFC 5737/3849, the angle-bracket tokens);
* the pre-push hook blocks a literal in the outgoing diff or a commit message,
  and lets the same range through once it is rewritten — and, on a clone that
  has no list, prints `forbid list absent` instead of passing silently;
* the commit-msg hook refuses a message carrying a literal before the commit
  object exists, accepts a clean one and passes on a clone with no list; a
  `commit-message` allowlist row waives the value it pins and no other;
* history mode scoped to the deny list (`--forbid-only`) is red on a branch
  that carries a literal and green after the branch is deleted — the CI job's
  command, run for real;
* there is one list, at `docs/ops/leak-forbid.txt` (HQ-only; CI installs the
  same bytes from the `LEAK_FORBID_B64` secret), and the repository publishes
  no copy;
* `deploy/hosts.example` carries every key with a placeholder value, the live
  `deploy/hosts.local` is gitignored, and the reader refuses a real value.

The probe values are assembled from parts, so this file is clean under the gate
it exercises. Every test runs the real scanner and the real hook against a
throwaway git repository; the fixture environment is scrubbed of `GIT_*` (the
pre-commit hook exports `GIT_DIR`, and an inherited one commits into the outer
worktree — the ODL-101 incident).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNER = ROOT / "tools" / "leak_scan.py"
HOOK = ROOT / ".githooks" / "pre-push"
COMMIT_MSG = ROOT / ".githooks" / "commit-msg"
EXAMPLE = ROOT / "deploy" / "hosts.example"
sys.path.insert(0, str(ROOT / "tools"))

import hosts_file  # noqa: E402

# Assembled from parts: the source line must not itself be a probe.
HQ_IP = ".".join(["23", "155", "44", "139"])
PUBLIC_IP = ".".join(["45", "33", "32", "156"])
# The list line is a regex: its own text has backslashes between the octets,
# so the file does not match the pattern it carries.
LIST_LINE = "hq-address\t" + r"23\.155\.44\.\d+" + "\n"
# One list, at the documented path. It is HQ-only (docs/ops/ is excluded from
# git) and CI installs the same bytes from the LEAK_FORBID_B64 secret; the
# repository publishes no copy. The box-id row is a literal the row itself
# matches, which is why the list is never scanned as content.
LIST_PATH = "docs/ops/leak-forbid.txt"
LIST_TEXT = (
    "# One list: docs/ops/leak-forbid.txt, HQ-only, also the CI secret.\n"
    "# The repository publishes no copy.\n"
    "servury-box-id\t" + "1680" + "20b4" + "\n"
    + LIST_LINE
)


def write_list(repo: Path, text: str = LIST_TEXT) -> Path:
    target = repo / LIST_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target

# The synthetic live constant the allowlist tests pin: one value, one path.
BOX = "1680" + "20b4"


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:12]


PLACEHOLDERS = (
    "192.0.2.10",
    "198.51.100.7",
    "203.0.113.9",
    "2001:db8::1",
    "<servury-account:dev>",
    "<servury-account:host>",
    "<server:ukitest>",
    "odl-000000",
    "owner@example.com",
)


def git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env.update({
        "GIT_AUTHOR_NAME": "Probe", "GIT_AUTHOR_EMAIL": "probe@example.test",
        "GIT_COMMITTER_NAME": "Probe", "GIT_COMMITTER_EMAIL": "probe@example.test",
    })
    return env


def run(args: list[str], cwd: Path, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=git_env(), capture_output=True,
                          text=True, timeout=180, **kwargs)


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd=cwd)


def commit_all(repo: Path, message: str = "fixture") -> None:
    git("add", "-A", cwd=repo)
    git("-c", "user.email=probe@example.test", "-c", "user.name=probe",
        "-c", "commit.gpgsign=false", "commit", "-qm", message, cwd=repo)


def scan(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run([sys.executable, str(SCANNER), "--tree", *args], cwd=repo)


class ForbidClass(unittest.TestCase):
    """A tracked literal fails; a placeholder and a pinned fixture pass."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        (self.repo / "leak-allow.tsv").write_text("# no exceptions\n", encoding="utf-8")
        write_list(self.repo, LIST_LINE)
        (self.repo / "keep.txt").write_text("nothing private here\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_a_tracked_literal_fails_and_its_removal_passes(self) -> None:
        probe = self.repo / "probe.md"
        probe.write_text(f"host {HQ_IP}\n", encoding="utf-8")
        git("add", "probe.md", cwd=self.repo)
        failed = scan(self.repo)
        self.assertEqual(failed.returncode, 1, failed.stdout)
        self.assertIn("FINDING forbidden-value", failed.stdout)
        self.assertIn("(hq-address)", failed.stdout)
        self.assertIn("probe.md:1", failed.stdout)
        git("rm", "-q", "--cached", "probe.md", cwd=self.repo)
        probe.unlink()
        passed = scan(self.repo)
        self.assertEqual(passed.returncode, 0, passed.stdout)
        self.assertIn("PASS: nothing to leak", passed.stdout)

    def test_the_placeholder_vocabulary_passes(self) -> None:
        probe = self.repo / "placeholders.md"
        probe.write_text("".join(f"value {value}\n" for value in PLACEHOLDERS),
                         encoding="utf-8")
        git("add", "placeholders.md", cwd=self.repo)
        result = scan(self.repo)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"forbid list:     {LIST_PATH} (1 labels)", result.stdout)

    def test_an_allowlist_row_waives_only_the_value_it_pins(self) -> None:
        # A synthetic literal: the HQ address is also a public-ip finding, and
        # this test is about the forbid class's own allowlist check.
        literal = "dead" + "beefcafe"
        write_list(self.repo, "probe-box\t" + r"dead[0-9a-f]+" + "\n")
        probe = self.repo / "probe.md"
        probe.write_text(f"box {literal}\n", encoding="utf-8")
        git("add", "probe.md", cwd=self.repo)
        digest = hashlib.sha256(literal.encode()).hexdigest()[:12]
        (self.repo / "leak-allow.tsv").write_text(
            "# rule\tpath\tsha256\tclass\tcredential\treason\texpires\n"
            f"forbidden-value\t*\tsha256:{digest}\tconstant\tno\ta published fixture\tnever\n",
            encoding="utf-8")
        waived = scan(self.repo)
        self.assertEqual(waived.returncode, 0, waived.stdout)
        self.assertEqual(waived.stdout.count("allowlisted:    1"), 1, waived.stdout)
        probe.write_text("box " + "dead" + "beefcab\n", encoding="utf-8")
        other = scan(self.repo)
        self.assertEqual(other.returncode, 1, other.stdout)
        self.assertIn("(probe-box)", other.stdout)


class PatchRecords(unittest.TestCase):
    """`--stdin-patch`: an added line is filed under the path it came from."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        write_list(self.repo, LIST_LINE)
        (self.repo / "keep.txt").write_text("nothing private here\n", encoding="utf-8")
        (self.repo / "live").mkdir()
        (self.repo / "live" / "host.txt").write_text(f"host {HQ_IP}\n", encoding="utf-8")
        (self.repo / "leak-allow.tsv").write_text(
            "# rule\tpath\tsha256\tclass\tcredential\treason\texpires\n"
            f"forbidden-value\tlive/host.txt\t{digest(HQ_IP)}\tconstant\tno\t"
            "the live address this path reads\tnever\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def patch(self, path: str) -> subprocess.CompletedProcess[str]:
        body = (
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            "@@ -0,0 +1 @@\n"
            f"+host {HQ_IP}\n"
        )
        return self.raw(body)

    def raw(self, body: str) -> subprocess.CompletedProcess[str]:
        return run([sys.executable, str(SCANNER), "--stdin-patch", "--label",
                    "outgoing-diff", "--forbid-file", LIST_PATH],
                   cwd=self.repo, input=body)

    def test_the_header_names_the_path_the_row_pins(self) -> None:
        allowed = self.patch("live/host.txt")
        self.assertEqual(allowed.returncode, 0, allowed.stdout)
        self.assertIn("allowlisted:    1", allowed.stdout)
        self.assertIn("outgoing-diff (1 paths", allowed.stdout)
        other = self.patch("elsewhere/host.txt")
        self.assertEqual(other.returncode, 1, other.stdout)
        self.assertIn("FINDING forbidden-value", other.stdout)
        self.assertIn("elsewhere/host.txt:1", other.stdout)
        self.assertIn("(hq-address)", other.stdout)

    def test_an_added_line_that_begins_with_two_pluses_is_scanned(self) -> None:
        # A pasted `set -x` transcript line reaches a patch as `+++ ssh …`,
        # byte-for-byte the shape of a `+++ b/<path>` header. The hunk's own
        # counts say it is body, so it is scanned and the section that follows
        # keeps its path — the header-only parser lost the line and misfiled
        # the rest of the hunk under a path named after its own text.
        body = (
            "diff --git a/notes.md b/notes.md\n"
            "--- a/notes.md\n"
            "+++ b/notes.md\n"
            "@@ -0,0 +1,2 @@\n"
            f"+++ ssh root@{HQ_IP}\n"
            "+plain after the transcript\n"
            "diff --git a/keep.txt b/keep.txt\n"
            "--- a/keep.txt\n"
            "+++ b/keep.txt\n"
            "@@ -1 +1 @@\n"
            "-nothing private here\n"
            "+still nothing private here\n"
        )
        result = self.raw(body)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("notes.md:1", result.stdout)
        self.assertIn("(hq-address)", result.stdout)
        self.assertIn("outgoing-diff (2 paths", result.stdout)
        self.assertNotIn("keep.txt:", result.stdout)
        # Removal and context lines still leave the new side alone: three added
        # lines are counted once each, not once per side of the hunk.
        expected = sum(len(line) + 1 for line in (
            f"++ ssh root@{HQ_IP}", "plain after the transcript",
            "still nothing private here"))
        self.assertIn(f"lines scanned:  {expected} bytes", result.stdout)


class HistoryPrivate(unittest.TestCase):
    """The CI job's command: every blob, scoped to the deny list."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        (self.repo / "leak-allow.tsv").write_text("# no exceptions\n", encoding="utf-8")
        write_list(self.repo, LIST_LINE)
        (self.repo / "keep.txt").write_text("nothing private here\n", encoding="utf-8")
        commit_all(self.repo, "base")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def history(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run([sys.executable, str(SCANNER), "--history",
                    "--forbid-file", LIST_PATH, *args], cwd=self.repo)

    def test_a_branch_carrying_a_literal_is_red_until_it_is_deleted(self) -> None:
        git("checkout", "-q", "-b", "leaky", cwd=self.repo)
        (self.repo / "probe.md").write_text(f"host {HQ_IP}\n", encoding="utf-8")
        commit_all(self.repo, "a literal on a branch")
        git("checkout", "-q", "main", cwd=self.repo)
        leaked = self.history("--forbid-only")
        self.assertEqual(leaked.returncode, 1, leaked.stdout)
        self.assertIn("commit ", leaked.stdout)
        self.assertIn("probe.md:1", leaked.stdout)
        git("branch", "-D", "leaky", cwd=self.repo)
        clean = self.history("--forbid-only")
        self.assertEqual(clean.returncode, 0, clean.stdout)
        self.assertIn("PASS: nothing to leak", clean.stdout)

    def test_forbid_only_skips_the_public_classes(self) -> None:
        (self.repo / "public.md").write_text(f"host {PUBLIC_IP}\n", encoding="utf-8")
        commit_all(self.repo, "a public address")
        full = self.history()
        self.assertEqual(full.returncode, 1, full.stdout)
        self.assertIn("FINDING public-ip", full.stdout)
        scoped = self.history("--forbid-only")
        self.assertEqual(scoped.returncode, 0, scoped.stdout)


class PrePushHook(unittest.TestCase):
    """The real hook, against a local bare remote."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        seed = self.base / "seed"
        seed.mkdir()
        git("init", "-q", "-b", "main", cwd=seed)
        (seed / "leak-allow.tsv").write_text("# no exceptions\n", encoding="utf-8")
        # The HQ shape: the one list at docs/ops/leak-forbid.txt. HQ keeps it
        # there untracked (docs/ops/ is excluded from git) and CI installs the
        # same bytes from a secret; the fixture commits it so a clone has it,
        # which is the state the hook reads either way.
        write_list(seed)
        (seed / "keep.txt").write_text("nothing private here\n", encoding="utf-8")
        (seed / "tools").mkdir()
        shutil.copy2(SCANNER, seed / "tools" / "leak_scan.py")
        commit_all(seed, "base")
        remote = self.base / "origin.git"
        git("init", "-q", "--bare", "-b", "main", str(remote), cwd=self.base)
        git("remote", "add", "origin", str(remote), cwd=seed)
        self.assertEqual(git("push", "-q", "origin", "main", cwd=seed).returncode, 0)
        self.work = self.base / "work"
        git("clone", "-q", str(remote), str(self.work), cwd=self.base)
        (self.work / ".githooks").mkdir(exist_ok=True)
        shutil.copy2(HOOK, self.work / ".githooks" / "pre-push")
        (self.work / ".githooks" / "pre-push").chmod(0o755)
        git("config", "core.hooksPath", ".githooks", cwd=self.work)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def push(self) -> subprocess.CompletedProcess[str]:
        return git("push", "origin", "main", cwd=self.work)

    def test_a_clean_range_pushes(self) -> None:
        (self.work / "clean.txt").write_text("nothing private\n", encoding="utf-8")
        commit_all(self.work, "a clean commit")
        # Git may swallow a successful hook's stdout; run the hook itself to
        # prove it saw a clean range, then prove the push went through.
        direct = run([str(self.work / ".githooks" / "pre-push")], cwd=self.work)
        self.assertEqual(direct.returncode, 0, direct.stdout + direct.stderr)
        self.assertIn("pre-push: the outgoing range carries no literal", direct.stdout)
        result = self.push()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_literal_in_a_commit_message_blocks_the_push(self) -> None:
        (self.work / "clean.txt").write_text("nothing private\n", encoding="utf-8")
        commit_all(self.work, f"name the host {HQ_IP}")
        blocked = self.push()
        self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.assertIn("FINDING forbidden-value", blocked.stdout + blocked.stderr)
        self.assertIn("commit-message", blocked.stdout + blocked.stderr)
        self.assertIn("git push --no-verify", blocked.stderr)
        git("commit", "--amend", "-qm", "a clean message", cwd=self.work)
        self.assertEqual(self.push().returncode, 0)

    def test_a_literal_in_the_outgoing_diff_blocks_the_push(self) -> None:
        (self.work / "probe.md").write_text(f"host {HQ_IP}\n", encoding="utf-8")
        commit_all(self.work, "a clean message")
        blocked = self.push()
        self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.assertIn("outgoing-diff", blocked.stdout + blocked.stderr)
        (self.work / "probe.md").unlink()
        git("add", "-A", cwd=self.work)
        git("commit", "--amend", "-qm", "a clean message", cwd=self.work)
        self.assertEqual(self.push().returncode, 0)

    def test_the_retired_root_copy_in_history_pushes(self) -> None:
        # This branch's own history carried a published root copy of the list,
        # and the re-cut deletes it. The commit that ADDED it carries every row
        # as an added line, including the literals the rows match, so a scan
        # that did not skip that path by name would refuse the very commit that
        # published the list — and `merge_card.py`'s plain `git push origin
        # main` would never land the removal.
        (self.work / "leak-forbid.txt").write_text(LIST_TEXT, encoding="utf-8")
        commit_all(self.work, "the old published copy")
        added = self.push()
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        (self.work / "leak-forbid.txt").unlink()
        commit_all(self.work, "retire the published copy")
        removed = self.push()
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)

    def test_a_root_list_beside_a_real_literal_still_blocks(self) -> None:
        # The exclusion is the list's two paths, not the whole scan: the same
        # commit that carries the list, with one real literal beside it, is red.
        (self.work / "leak-forbid.txt").write_text(LIST_TEXT, encoding="utf-8")
        (self.work / "probe.md").write_text(f"host {HQ_IP}\n", encoding="utf-8")
        commit_all(self.work, "a list and a leak")
        blocked = self.push()
        self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.assertIn("(hq-address)", blocked.stdout + blocked.stderr)
        (self.work / "probe.md").unlink()
        git("add", "-A", cwd=self.work)
        git("commit", "--amend", "-qm", "a list and no leak", cwd=self.work)
        self.assertEqual(self.push().returncode, 0)

    def test_an_edit_to_the_documented_list_is_not_scanned_as_content(self) -> None:
        # The list matches itself: a row's own literal is an added line in the
        # commit that edits the list. Skipping it by path is what lets the list
        # be maintained at all.
        with (self.work / LIST_PATH).open("a", encoding="utf-8") as handle:
            handle.write(f"# the box id {'1680' + '20b4'} is denied by the row above\n")
        commit_all(self.work, "a comment in the deny list")
        pushed = self.push()
        self.assertEqual(pushed.returncode, 0, pushed.stdout + pushed.stderr)

    def test_an_xtrace_line_in_the_outgoing_diff_blocks_the_push(self) -> None:
        # DoD 2 is "block on a hit", and a pasted `set -x` transcript is the
        # archetypal runbook line: `++ ssh root@…` becomes `+++ ssh root@…` in
        # the patch, the shape of a `+++ b/<path>` header. A prefix-reading
        # parser swallowed it as a header and pushed; the real hook must refuse.
        (self.work / "notes.md").write_text(f"++ ssh root@{HQ_IP}\n", encoding="utf-8")
        commit_all(self.work, "a clean message")
        direct = run([str(self.work / ".githooks" / "pre-push")], cwd=self.work)
        self.assertEqual(direct.returncode, 1, direct.stdout + direct.stderr)
        self.assertIn("FINDING forbidden-value", direct.stdout)
        self.assertIn("notes.md:1", direct.stdout)
        self.assertIn("(hq-address)", direct.stdout)
        blocked = self.push()
        self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        (self.work / "notes.md").unlink()
        git("add", "-A", cwd=self.work)
        git("commit", "--amend", "-qm", "a clean message", cwd=self.work)
        self.assertEqual(self.push().returncode, 0)

    def test_a_live_value_pinned_at_its_path_pushes_and_elsewhere_blocks(self) -> None:
        # The tree keeps the identifiers a program reads and pins each one in
        # `leak-allow.tsv` at its own path. The diff scan must honour that row
        # by path — otherwise the commit that restores a live constant is
        # blocked — and must not turn it into a path-wide waiver.
        (self.work / "live").mkdir()
        (self.work / "live" / "host.txt").write_text(f"box {BOX}\n", encoding="utf-8")
        (self.work / "leak-allow.tsv").write_text(
            "# rule\tpath\tsha256\tclass\tcredential\treason\texpires\n"
            f"forbidden-value\tlive/host.txt\t{digest(BOX)}\tconstant\tno\t"
            "the live constant this path reads\tnever\n", encoding="utf-8")
        commit_all(self.work, "a live constant at its own path")
        direct = run([str(self.work / ".githooks" / "pre-push")], cwd=self.work)
        self.assertEqual(direct.returncode, 0, direct.stdout + direct.stderr)
        self.assertIn("allowlisted:    1", direct.stdout)
        self.assertEqual(self.push().returncode, 0)

        (self.work / "other.txt").write_text(f"box {BOX}\n", encoding="utf-8")
        commit_all(self.work, "the same value at another path")
        blocked = self.push()
        self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.assertIn("other.txt", blocked.stdout + blocked.stderr)
        self.assertIn("(servury-box-id)", blocked.stdout + blocked.stderr)

    def test_a_path_pinned_value_in_a_commit_message_still_blocks(self) -> None:
        # The message surface's path is the label `commit-message`, so a row
        # pinned at a tree path cannot waive it: the value may live in that file
        # and nowhere else, and a message is not that file.
        (self.work / "clean.txt").write_text("nothing private\n", encoding="utf-8")
        (self.work / "leak-allow.tsv").write_text(
            "# rule\tpath\tsha256\tclass\tcredential\treason\texpires\n"
            f"forbidden-value\tlive/host.txt\t{digest(BOX)}\tconstant\tno\t"
            "the live constant this one path reads\tnever\n", encoding="utf-8")
        commit_all(self.work, f"the box {BOX} moved")
        blocked = self.push()
        self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.assertIn("commit-message", blocked.stdout + blocked.stderr)
        self.assertIn("(servury-box-id)", blocked.stdout + blocked.stderr)
        git("commit", "--amend", "-qm", "a clean message", cwd=self.work)
        self.assertEqual(self.push().returncode, 0)


class MessageSurface(unittest.TestCase):
    """`--stdin --label commit-message`: the literal, its waiver, a neighbour."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        (self.repo / "leak-allow.tsv").write_text("# no exceptions\n", encoding="utf-8")
        write_list(self.repo, LIST_LINE)
        (self.repo / "keep.txt").write_text("nothing private here\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def message(self, text: str) -> subprocess.CompletedProcess[str]:
        return run([sys.executable, str(SCANNER), "--stdin", "--label",
                    "commit-message", "--forbid-file", LIST_PATH],
                   cwd=self.repo, input=text)

    def pin(self, value: str) -> None:
        (self.repo / "leak-allow.tsv").write_text(
            "# rule\tpath\tsha256\tclass\tcredential\treason\texpires\n"
            f"forbidden-value\tcommit-message\t{digest(value)}\tconstant\tno\t"
            "the committed transcript names this host\tnever\n", encoding="utf-8")

    def test_a_message_carrying_a_literal_fails(self) -> None:
        result = self.message(f"name the host {HQ_IP}\n")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FINDING forbidden-value", result.stdout)
        self.assertIn("commit-message:1", result.stdout)
        self.assertIn("(hq-address)", result.stdout)
        self.assertIn("FAIL: 1 finding(s) in commit-message", result.stdout)

    def test_a_commit_message_row_waives_the_value_it_pins(self) -> None:
        self.pin(HQ_IP)
        waived = self.message(f"name the host {HQ_IP}\n")
        self.assertEqual(waived.returncode, 0, waived.stdout)
        self.assertIn("PASS: nothing forbidden in commit-message", waived.stdout)

    def test_the_row_does_not_waive_a_different_literal(self) -> None:
        # The digest pins the value, not the row: the neighbouring address in
        # the same class is still a finding under the same `commit-message` path.
        self.pin(HQ_IP)
        neighbour = ".".join(["23", "155", "44", "7"])
        other = self.message(f"name another host {neighbour}\n")
        self.assertEqual(other.returncode, 1, other.stdout)
        self.assertIn("(hq-address)", other.stdout)


class CommitMsgHook(unittest.TestCase):
    """The write-time gate: the real hook, run by a real `git commit`."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        (self.repo / "leak-allow.tsv").write_text("# no exceptions\n", encoding="utf-8")
        write_list(self.repo)
        (self.repo / "keep.txt").write_text("nothing private here\n", encoding="utf-8")
        (self.repo / "tools").mkdir()
        shutil.copy2(SCANNER, self.repo / "tools" / "leak_scan.py")
        (self.repo / ".githooks").mkdir()
        shutil.copy2(COMMIT_MSG, self.repo / ".githooks" / "commit-msg")
        (self.repo / ".githooks" / "commit-msg").chmod(0o755)
        git("config", "core.hooksPath", ".githooks", cwd=self.repo)
        git("add", "-A", cwd=self.repo)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def commit(self, message: str) -> subprocess.CompletedProcess[str]:
        return git("-c", "user.email=probe@example.test", "-c", "user.name=probe",
                   "-c", "commit.gpgsign=false", "commit", "-m", message,
                   cwd=self.repo)

    def test_a_literal_message_is_refused_and_a_clean_one_commits(self) -> None:
        blocked = self.commit(f"name the host {HQ_IP}")
        self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.assertIn("FINDING forbidden-value", blocked.stderr)
        self.assertIn("commit-message:1", blocked.stderr)
        self.assertIn("the commit did not happen", blocked.stderr)
        self.assertNotEqual(
            git("rev-parse", "--verify", "HEAD", cwd=self.repo).returncode, 0,
            "a refused message left a commit behind")
        clean = self.commit("a clean message")
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        self.assertEqual(git("log", "-1", "--format=%s", cwd=self.repo).stdout.strip(),
                         "a clean message")

    def test_a_commit_message_row_lets_the_commit_through(self) -> None:
        (self.repo / "leak-allow.tsv").write_text(
            "# rule\tpath\tsha256\tclass\tcredential\treason\texpires\n"
            f"forbidden-value\tcommit-message\t{digest(HQ_IP)}\tconstant\tno\t"
            "the committed transcript names this host\tnever\n", encoding="utf-8")
        allowed = self.commit(f"name the host {HQ_IP}")
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)
        self.assertEqual(git("log", "-1", "--format=%s", cwd=self.repo).stdout.strip(),
                         f"name the host {HQ_IP}")

    def test_the_hook_passes_when_the_list_is_absent(self) -> None:
        # A public clone carries no list: same rule as the pre-push hook, the
        # gate says so on stderr and lets the commit through.
        write_list(self.repo).unlink()
        passed = self.commit(f"name the host {HQ_IP}")
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertIn("commit-msg: forbid list absent: docs/ops/leak-forbid.txt",
                      passed.stderr)
        self.assertIn("the message was not scanned for private literals", passed.stderr)


class HostsExample(unittest.TestCase):
    """The `.env.example` equivalent: every key, every value a placeholder."""

    def test_every_key_is_present_and_a_placeholder(self) -> None:
        values = hosts_file.load(EXAMPLE, placeholders=True)
        self.assertEqual(set(values), set(hosts_file.KEYS))
        for key, value in values.items():
            self.assertTrue(hosts_file.is_placeholder(value), f"{key}={value!r}")

    def test_a_real_value_is_refused(self) -> None:
        text = EXAMPLE.read_text(encoding="utf-8").replace(
            "HQ_ADDRESS=203.0.113.139", f"HQ_ADDRESS={HQ_IP}")
        with self.assertRaises(hosts_file.HostsError) as caught:
            hosts_file.parse(text, placeholders=True)
        self.assertIn("HQ_ADDRESS", str(caught.exception))
        self.assertIn("is not a placeholder", str(caught.exception))

    def test_a_missing_key_is_refused(self) -> None:
        text = "\n".join(line for line in EXAMPLE.read_text(encoding="utf-8").splitlines()
                         if not line.startswith("SERVER_ID=")) + "\n"
        with self.assertRaises(hosts_file.HostsError) as caught:
            hosts_file.parse(text, placeholders=True)
        self.assertIn("SERVER_ID", str(caught.exception))

    def test_the_live_file_is_gitignored(self) -> None:
        ignored = git("check-ignore", "-q", "deploy/hosts.local", cwd=ROOT)
        self.assertEqual(ignored.returncode, 0, "deploy/hosts.local is not gitignored")

    def test_every_template_value_passes_the_scanner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            git("init", "-q", "-b", "main", cwd=repo)
            (repo / "leak-allow.tsv").write_text("# no exceptions\n", encoding="utf-8")
            write_list(repo, LIST_LINE)
            shutil.copy2(EXAMPLE, repo / "hosts.example")
            git("add", "-A", cwd=repo)
            result = scan(repo)
            self.assertEqual(result.returncode, 0, result.stdout)


class HookWithoutTheList(unittest.TestCase):
    """A clone carries no list; the hook must say so, not pass as if it ran."""

    def test_a_clone_without_the_list_prints_the_absent_line(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        seed = base / "seed"
        seed.mkdir()
        git("init", "-q", "-b", "main", cwd=seed)
        (seed / "leak-allow.tsv").write_text("# no exceptions\n", encoding="utf-8")
        (seed / "keep.txt").write_text("nothing private here\n", encoding="utf-8")
        (seed / "tools").mkdir()
        shutil.copy2(SCANNER, seed / "tools" / "leak_scan.py")
        commit_all(seed, "base without a list")
        remote = base / "origin.git"
        git("init", "-q", "--bare", "-b", "main", str(remote), cwd=base)
        git("remote", "add", "origin", str(remote), cwd=seed)
        self.assertEqual(git("push", "-q", "origin", "main", cwd=seed).returncode, 0)
        work = base / "work"
        git("clone", "-q", str(remote), str(work), cwd=base)
        (work / ".githooks").mkdir()
        shutil.copy2(HOOK, work / ".githooks" / "pre-push")
        (work / ".githooks" / "pre-push").chmod(0o755)
        git("config", "core.hooksPath", ".githooks", cwd=work)

        # A literal that the armed hook refuses (the class above proves that),
        # plus a clean push: the hook has nothing to scan it against and says so.
        (work / "probe.md").write_text(f"host {HQ_IP}\n", encoding="utf-8")
        commit_all(work, "a clean message")
        direct = run([str(work / ".githooks" / "pre-push")], cwd=work)
        self.assertEqual(direct.returncode, 0, direct.stdout + direct.stderr)
        self.assertIn("pre-push: forbid list absent: docs/ops/leak-forbid.txt",
                      direct.stderr)
        self.assertIn("was not scanned for private literals", direct.stderr)
        self.assertNotIn("PASS", direct.stdout)


class OneList(unittest.TestCase):
    """One list, at docs/ops/leak-forbid.txt: HQ keeps it, CI holds the secret."""

    def test_the_repository_publishes_no_copy(self) -> None:
        tracked = git("ls-files", "--error-unmatch", "leak-forbid.txt", cwd=ROOT)
        self.assertNotEqual(tracked.returncode, 0,
                            "the repository still tracks a root leak-forbid.txt")

    def test_the_list_cannot_be_committed_from_a_clone(self) -> None:
        # A clone has no .git/info/exclude entry from HQ, so an installed list
        # would ride into a commit on `git add -A` and the 11 literals would be
        # published. The tracked .gitignore is what stops it.
        ignored = git("check-ignore", "-q", LIST_PATH, cwd=ROOT)
        self.assertEqual(ignored.returncode, 0, f"{LIST_PATH} is not gitignored")

    def test_every_gate_names_the_documented_path(self) -> None:
        # One path in the scanner's default, the Makefile, the hook and CI, so
        # there is one place to put the list and one place to rotate it.
        for path in (SCANNER, ROOT / "Makefile", HOOK, COMMIT_MSG,
                     ROOT / ".github" / "workflows" / "check.yml"):
            self.assertIn(LIST_PATH, path.read_text(encoding="utf-8"), str(path))

    def test_make_leak_names_the_list(self) -> None:
        result = run(["make", "-n", "leak"], cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"--forbid-file \"{LIST_PATH}\"", result.stdout)

    def test_make_leak_says_absent_on_a_tree_without_the_list(self) -> None:
        # The re-cut's DoD 1: a clone (no list) must print `forbid list absent`
        # and still run the tree classes — never a PASS that reads as if the
        # forbidden-value class had run.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        repo = Path(temp.name) / "repo"
        repo.mkdir()
        git("init", "-q", "-b", "main", cwd=repo)
        (repo / "leak-allow.tsv").write_text("# no exceptions\n", encoding="utf-8")
        (repo / "keep.txt").write_text("nothing private here\n", encoding="utf-8")
        (repo / "tools").mkdir()
        shutil.copy2(SCANNER, repo / "tools" / "leak_scan.py")
        result = subprocess.run(["make", "-f", str(ROOT / "Makefile"), "leak",
                                 f"PYTHON={sys.executable}"],
                                cwd=repo, env=git_env(), capture_output=True,
                                text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"leak: forbid list absent: {LIST_PATH}", result.stdout)
        self.assertIn(f"forbid list:     absent ({LIST_PATH})", result.stdout)
        self.assertIn("forbidden-value and forbidden-path classes did not run", result.stdout)

        # Naming a path that does not exist is the same absent branch in the
        # target: `make leak` is about the tree, so it passes with the line.
        named = subprocess.run(["make", "-f", str(ROOT / "Makefile"), "leak",
                                "FORBID_FILE=/nonexistent",
                                f"PYTHON={sys.executable}"],
                               cwd=repo, env=git_env(), capture_output=True,
                               text=True, timeout=180)
        self.assertEqual(named.returncode, 0, named.stdout + named.stderr)
        self.assertIn("leak: forbid list absent: /nonexistent", named.stdout)

        # Exit 2 is the scanner's contract: a caller that names a list the scan
        # must have gets a finding and exit 2, never a silent skip.
        direct = subprocess.run([sys.executable, "tools/leak_scan.py", "--tree",
                                 "--forbid-file", "/nonexistent"],
                                cwd=repo, env=git_env(), capture_output=True,
                                text=True, timeout=180)
        self.assertEqual(direct.returncode, 2, direct.stdout + direct.stderr)
        self.assertIn("FINDING allow-file  --forbid-file /nonexistent", direct.stdout)

    def test_the_hq_checkout_carries_the_list_at_that_path(self) -> None:
        if not (ROOT / LIST_PATH).is_file():
            self.skipTest("no HQ list in this checkout (a clone carries none)")
        rows = [line for line in (ROOT / LIST_PATH).read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")]
        self.assertGreaterEqual(len(rows), 10, "the HQ list is short")

    def test_make_hooks_installs_the_pre_push_hook(self) -> None:
        result = run(["make", "-n", "hooks"], cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(".githooks/pre-push", result.stdout)


if __name__ == "__main__":
    unittest.main()
