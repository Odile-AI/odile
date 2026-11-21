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
  `commit-message` allowlist row waives the value it pins and no other; a
  `git commit -v` message is scanned without the appended diff below its
  scissors line, and a `#`-leading line of an `-m`/`-F` message — which git
  records — is scanned, so a literal on it is refused at write time;
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
worktree — the ODL-101 incident). The tests that ask git about *this* checkout
(`ReadsTheCheckout`) keep that scrub and add `-c safe.directory` for this tree
alone: CI writes the checkout as its own user and runs the gate as root, so an
untrusted read answers 128, and 128 is a skip that names it — never a pass.
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
from unittest import mock

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


# A question about ROOT is not a fixture. `git_env()` scrubs the whole `GIT_*`
# namespace (ODL-101: an inherited `GIT_DIR` committed into the outer worktree)
# and blanks global and system config, which is right for a throwaway repository
# and wrong here: it also drops the `safe.directory` trust CI records in global
# config. So these queries keep the hermetic config and carry the trust on the
# command line, for this checkout and no other.
def checkout_git(*args: str) -> subprocess.CompletedProcess[str]:
    """Ask git about the checkout at ROOT the way this host's git sees it.

    The runner writes the checkout as its own user and runs the gate as root, so
    an untrusted read is `fatal: detected dubious ownership` with rc 128 — the
    red every push to main from 79980eb carried. `-c safe.directory` is the one
    scope that both reaches the query and leaves the host config out; git 2.53
    honours it, and `TheCheckoutQuery` measures that it still does.
    """
    return run(["git", "-c", f"safe.directory={ROOT}", *args], cwd=ROOT)


def one_line(text: str) -> str:
    """git's fatal line on one line, so a skip reason survives a log."""
    return " ".join(text.split())


def unreadable_reason(result: subprocess.CompletedProcess[str]) -> str:
    detail = one_line(result.stderr) or one_line(result.stdout) or "no output"
    return f"git cannot read the checkout at {ROOT} (rc {result.returncode}): {detail}"


def ignore_verdict(result: subprocess.CompletedProcess[str], message: str) -> tuple[str, str]:
    """What `git check-ignore -q` answered: ("ok"|"fail"|"skip", text).

    0 is "ignored" and 1 is "not ignored" — the checkout's answers. Above 1 is
    git itself failing, which is not an answer and must not be asserted as one.
    """
    if result.returncode == 0:
        return "ok", ""
    if result.returncode == 1:
        return "fail", message
    return "skip", unreadable_reason(result)


def tracked_verdict(result: subprocess.CompletedProcess[str], message: str) -> tuple[str, str]:
    """What `git ls-files --error-unmatch` answered.

    `--error-unmatch` answers 1 for "no tracked path matches" — the passing
    answer for a list the repository must not publish — and 0 when the path is
    tracked. 128 is git failing, which used to pass here as if it were 1.
    """
    if result.returncode == 1:
        return "ok", ""
    if result.returncode == 0:
        return "fail", message
    return "skip", unreadable_reason(result)


class ReadsTheCheckout(unittest.TestCase):
    """The questions about ROOT itself, answered honestly.

    A `git` that cannot read the checkout (128) is a skip that names the fatal
    line and prints it even without `-v`, so the CI log carries the reason; 0
    and 1 are the checkout's answers and are asserted.
    """

    def assert_gitignored(self, path: str, message: str) -> None:
        self._require_readable_checkout()
        self._apply(ignore_verdict(checkout_git("check-ignore", "-q", path), message))

    def assert_untracked(self, path: str, message: str) -> None:
        self._require_readable_checkout()
        self._apply(tracked_verdict(
            checkout_git("ls-files", "--error-unmatch", path), message))

    def _require_readable_checkout(self) -> None:
        probe = checkout_git("rev-parse", "--is-inside-work-tree")
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            self._skip(unreadable_reason(probe))

    def _apply(self, verdict: tuple[str, str]) -> None:
        kind, text = verdict
        if kind == "fail":
            self.fail(text)
        if kind == "skip":
            self._skip(text)

    def _skip(self, reason: str) -> None:
        print(f"test_forbid_gate: skip: {reason}", flush=True)
        self.skipTest(reason)


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
        # published the list — and the merge tool's plain `git push origin
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

    def test_a_verbose_commit_scans_the_message_not_the_appended_diff(self) -> None:
        # `git commit -v` (or `commit.verbose=true`) hands the hook the staged
        # diff appended after a scissors line. The diff is not the message: a
        # value waived at its own tree path must not be refused at the message
        # surface, which is what the pre-push hook scans per path.
        live = self.repo / "live"
        live.mkdir()
        (live / "box.txt").write_text(f"box {BOX}\n", encoding="utf-8")
        (self.repo / "leak-allow.tsv").write_text(
            "# rule\tpath\tsha256\tclass\tcredential\treason\texpires\n"
            f"forbidden-value\tlive/box.txt\t{digest(BOX)}\tconstant\tno\t"
            "the frozen fixture names this box\tnever\n", encoding="utf-8")
        # The editor keeps git's template and appends nothing: the message is
        # typed above the comments, the diff stays below the scissors line, and
        # a copy proves the hook saw the diff.
        editor = Path(self.temp.name) / "editor.sh"
        editor.write_text(
            "#!/bin/sh\n"
            "cp \"$1\" \"$1.verbose\"\n"
            "printf 'a clean verbose message\\n' | cat - \"$1\" > \"$1.tmp\"\n"
            "mv \"$1.tmp\" \"$1\"\n", encoding="utf-8")
        editor.chmod(0o755)
        git("add", "-A", cwd=self.repo)
        verbose = git("-c", "user.email=probe@example.test", "-c", "user.name=probe",
                      "-c", "commit.gpgsign=false", "-c", f"core.editor={editor}",
                      "commit", "-v", cwd=self.repo)
        self.assertEqual(verbose.returncode, 0, verbose.stdout + verbose.stderr)
        self.assertEqual(git("log", "-1", "--format=%s", cwd=self.repo).stdout.strip(),
                         "a clean verbose message")
        seen = (self.repo / ".git" / "COMMIT_EDITMSG.verbose").read_text(encoding="utf-8")
        self.assertIn("------------------------ >8 ------------------------", seen)
        self.assertIn(BOX, seen, "the fixture did not reproduce git's -v message file")

    def test_a_comment_leading_line_of_a_dash_m_message_is_scanned(self) -> None:
        # `git commit -m` and `git commit -F` use cleanup=whitespace, which
        # keeps a line beginning with the comment character: it lands in the
        # commit object, and the pre-push hook then blocks the push on it. The
        # write-time hook must see it, or it accepts exactly the message this
        # card exists to refuse.
        blocked = self.commit(f"#{BOX} deploy note")
        self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.assertIn("FINDING forbidden-value", blocked.stderr)
        self.assertIn("the commit did not happen", blocked.stderr)
        self.assertNotEqual(
            git("rev-parse", "--verify", "HEAD", cwd=self.repo).returncode, 0,
            "a refused message left a commit behind")
        # A body line as well as a subject line, and the `-F` form.
        blocked = self.commit(f"a dash-m commit\n# the box was {BOX} at the time")
        self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.assertIn("commit-message:2", blocked.stderr)
        message_file = Path(self.temp.name) / "message.txt"
        message_file.write_text(f"a dash-F commit\n# the box was {BOX}\n", encoding="utf-8")
        blocked = git("-c", "user.email=probe@example.test", "-c", "user.name=probe",
                      "-c", "commit.gpgsign=false", "commit", "-F", str(message_file),
                      cwd=self.repo)
        self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.assertIn("FINDING forbidden-value", blocked.stderr)
        # A clean message that carries a comment line commits, and git keeps
        # the line — the fixture's own proof that this shape is not skipped.
        clean = self.commit("a clean dash-m commit\n# a comment line without a literal")
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        body = git("log", "-1", "--format=%B", cwd=self.repo).stdout
        self.assertIn("# a comment line without a literal", body)

    def test_the_hook_passes_when_the_list_is_absent(self) -> None:
        # A public clone carries no list: same rule as the pre-push hook, the
        # gate says so on stderr and lets the commit through.
        write_list(self.repo).unlink()
        passed = self.commit(f"name the host {HQ_IP}")
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertIn("commit-msg: forbid list absent: docs/ops/leak-forbid.txt",
                      passed.stderr)
        self.assertIn("the message was not scanned for private literals", passed.stderr)


class HostsExample(ReadsTheCheckout):
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
        self.assert_gitignored("deploy/hosts.local", "deploy/hosts.local is not gitignored")

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


class OneList(ReadsTheCheckout):
    """One list, at docs/ops/leak-forbid.txt: HQ keeps it, CI holds the secret."""

    def test_the_repository_publishes_no_copy(self) -> None:
        self.assert_untracked("leak-forbid.txt",
                              "the repository still tracks a root leak-forbid.txt")

    def test_the_list_cannot_be_committed_from_a_clone(self) -> None:
        # A clone has no .git/info/exclude entry from HQ, so an installed list
        # would ride into a commit on `git add -A` and the 11 literals would be
        # published. The tracked .gitignore is what stops it.
        self.assert_gitignored(LIST_PATH, f"{LIST_PATH} is not gitignored")

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


class GitCouldNotAnswer(unittest.TestCase):
    """128 is git failing against the checkout, never the checkout's answer.

    The three tests below ask git about ROOT itself. Read as a boolean, a 128
    became `AssertionError: 128 != 0` on every push to main from 79980eb — the
    runner writes the checkout as its own user and runs the gate as root, so git
    answers `fatal: detected dubious ownership` unless the trust CI records in
    global config reaches the query, and `git_env()` blanks that config. Worse,
    `test_the_repository_publishes_no_copy` read the same 128 as "the root copy
    is untracked" and *passed* on it. So: 0/1 are answers, above 1 is a fault,
    and a fault is a skip that names it — never a pass.
    """

    FATAL = ("fatal: detected dubious ownership in repository at "
             f"'{ROOT}'\nTo add an exception for this directory, call:\n\n"
             f"\tgit config --global --add safe.directory {ROOT}\n")

    @staticmethod
    def answer(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["git"], returncode, "", stderr)

    def test_check_ignore_zero_is_the_ignored_answer(self) -> None:
        self.assertEqual(ignore_verdict(self.answer(0), "m"), ("ok", ""))

    def test_check_ignore_one_fails_with_the_existing_message(self) -> None:
        message = f"{LIST_PATH} is not gitignored"
        self.assertEqual(ignore_verdict(self.answer(1), message), ("fail", message))

    def test_check_ignore_128_is_a_skip_that_names_the_fatal_line(self) -> None:
        kind, reason = ignore_verdict(self.answer(128, self.FATAL), "m")
        self.assertEqual(kind, "skip")
        self.assertIn("rc 128", reason)
        self.assertIn("dubious ownership", reason)

    def test_ls_files_one_is_the_untracked_answer(self) -> None:
        self.assertEqual(tracked_verdict(self.answer(1), "m"), ("ok", ""))

    def test_ls_files_zero_fails_because_the_path_is_tracked(self) -> None:
        message = "the repository still tracks a root leak-forbid.txt"
        self.assertEqual(tracked_verdict(self.answer(0), message), ("fail", message))

    def test_ls_files_128_cannot_pass_as_untracked(self) -> None:
        kind, reason = tracked_verdict(self.answer(128, self.FATAL), "m")
        self.assertEqual(kind, "skip")
        self.assertIn("dubious ownership", reason)


class TheCheckoutQuery(unittest.TestCase):
    """A question about ROOT carries its trust and keeps the host config out."""

    def test_the_query_trusts_this_checkout_and_stays_hermetic(self) -> None:
        with mock.patch("subprocess.run") as spawn:
            spawn.return_value = subprocess.CompletedProcess(["git"], 0, "true\n", "")
            checkout_git("check-ignore", "-q", LIST_PATH)
        command, kwargs = spawn.call_args[0][0], spawn.call_args[1]
        self.assertEqual(command, ["git", "-c", f"safe.directory={ROOT}",
                                   "check-ignore", "-q", LIST_PATH])
        self.assertEqual(kwargs["cwd"], ROOT)
        # The hermetic config stays: a host `~/.gitconfig` carrying
        # `core.excludesFile` could otherwise answer "ignored" for a tree whose
        # .gitignore lost the line, and the gate would pass on a host file.
        self.assertEqual(kwargs["env"]["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(kwargs["env"]["GIT_CONFIG_NOSYSTEM"], "1")

    def test_this_git_honours_the_command_line_trust_on_a_foreign_owned_checkout(self) -> None:
        # The premise of `checkout_git`: git 2.53 honours `-c safe.directory`,
        # so the runner's checkout (written by github-actions, read by root) is
        # readable without also reading a host config. If a future git drops
        # that, this goes red instead of CI silently skipping.
        if os.geteuid() != 0:
            self.skipTest("needs root to hand a fixture to another uid")
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            git("init", "-q", "-b", "main", cwd=repo)
            (repo / ".gitignore").write_text("secret.local\n", encoding="utf-8")
            (repo / "keep.txt").write_text("nothing private\n", encoding="utf-8")
            chowned = subprocess.run(["chown", "-R", "65534:65534", str(repo)],
                                     capture_output=True, text=True)
            self.assertEqual(chowned.returncode, 0, chowned.stderr)

            untrusted = subprocess.run(
                ["git", "-C", str(repo), "check-ignore", "-q", "secret.local"],
                env=git_env(), capture_output=True, text=True)
            self.assertEqual(untrusted.returncode, 128, untrusted.stderr)
            self.assertIn("dubious ownership", untrusted.stderr)

            trusted = subprocess.run(
                ["git", "-c", f"safe.directory={repo}", "-C", str(repo),
                 "check-ignore", "-q", "secret.local"],
                env=git_env(), capture_output=True, text=True)
            self.assertEqual(trusted.returncode, 0, trusted.stderr)


if __name__ == "__main__":
    unittest.main()
