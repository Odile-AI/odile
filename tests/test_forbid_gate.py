#!/usr/bin/python3
"""The forward leak gates: deny list, pre-push hook, placeholders, host template.

`tools/leak_scan.py`'s forbidden-value class was only as good as the list it
was handed, and until ODL-911 the HQ checkout never handed it one. These tests
pin the four forward gates the card adds:

* a tracked file carrying a real HQ literal fails, and the documented
  placeholder vocabulary passes (RFC 5737/3849, the angle-bracket tokens);
* the pre-push hook blocks a literal in the outgoing diff or a commit message,
  and lets the same range through once it is rewritten;
* history mode scoped to the deny list (`--forbid-only`) is red on a branch
  that carries a literal and green after the branch is deleted — the CI job's
  command, run for real;
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
EXAMPLE = ROOT / "deploy" / "hosts.example"
sys.path.insert(0, str(ROOT / "tools"))

import hosts_file  # noqa: E402

# Assembled from parts: the source line must not itself be a probe.
HQ_IP = ".".join(["23", "155", "44", "139"])
PUBLIC_IP = ".".join(["45", "33", "32", "156"])
# The list line is a regex: its own text has backslashes between the octets,
# so the file does not match the pattern it carries.
LIST_LINE = "hq-address\t" + r"23\.155\.44\.\d+" + "\n"
# One list, two copies (HQ's canon and the published root copy are
# byte-identical). The box-id row is a literal the row itself matches, which is
# why a commit that publishes the list must not be scanned as content.
LIST_TEXT = (
    "# One list, two copies: docs/ops/leak-forbid.txt is canonical and the\n"
    "# repository publishes a byte-identical copy at its root.\n"
    "servury-box-id\t" + "1680" + "20b4" + "\n"
    + LIST_LINE
)

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
        (self.repo / "leak-forbid.txt").write_text(LIST_LINE, encoding="utf-8")
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
        self.assertIn("forbid list:     leak-forbid.txt (1 labels)", result.stdout)

    def test_an_allowlist_row_waives_only_the_value_it_pins(self) -> None:
        # A synthetic literal: the HQ address is also a public-ip finding, and
        # this test is about the forbid class's own allowlist check.
        literal = "dead" + "beefcafe"
        (self.repo / "leak-forbid.txt").write_text(
            "probe-box\t" + r"dead[0-9a-f]+" + "\n", encoding="utf-8")
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


class HistoryPrivate(unittest.TestCase):
    """The CI job's command: every blob, scoped to the deny list."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        (self.repo / "leak-allow.tsv").write_text("# no exceptions\n", encoding="utf-8")
        (self.repo / "leak-forbid.txt").write_text(LIST_LINE, encoding="utf-8")
        (self.repo / "keep.txt").write_text("nothing private here\n", encoding="utf-8")
        commit_all(self.repo, "base")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def history(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run([sys.executable, str(SCANNER), "--history",
                    "--forbid-file", "leak-forbid.txt", *args], cwd=self.repo)

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
        # The HQ shape: the canonical list under docs/ops (excluded from git at
        # HQ, so only HQ has it at the head) and no root copy yet. The hook
        # resolves the canon here; the published copy arrives in the test.
        (seed / "docs" / "ops").mkdir(parents=True)
        (seed / "docs" / "ops" / "leak-forbid.txt").write_text(LIST_TEXT, encoding="utf-8")
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

    def test_the_published_copy_can_be_added_by_its_own_commit(self) -> None:
        # The HQ shape, which is what blocked this card's own publication: the
        # hook resolves the canon under docs/ops, and the outgoing commit adds
        # the published root copy — every row an added line, including the
        # literals the rows match. Excluding only the resolved path made the
        # list report itself, so `merge_card.py`'s plain `git push origin main`
        # exited 5. Both paths are excluded now, so this pushes.
        (self.work / "leak-forbid.txt").write_text(LIST_TEXT, encoding="utf-8")
        commit_all(self.work, "publish the deny list")
        pushed = self.push()
        self.assertEqual(pushed.returncode, 0, pushed.stdout + pushed.stderr)
        # The maintenance case the list's header requires ("never edit one
        # without the other"): the copy is already published, and an edit that
        # names a literal in a comment is still not scanned as content.
        with (self.work / "leak-forbid.txt").open("a", encoding="utf-8") as handle:
            handle.write(f"# the box id {'1680' + '20b4'} is denied by the row above\n")
        commit_all(self.work, "a comment in the deny list")
        edited = self.push()
        self.assertEqual(edited.returncode, 0, edited.stdout + edited.stderr)

    def test_the_published_copy_does_not_blind_the_rest_of_the_range(self) -> None:
        # The exclusion is two paths, not the whole scan: the same commit that
        # publishes the list, with one real literal beside it, is a red push.
        (self.work / "leak-forbid.txt").write_text(LIST_TEXT, encoding="utf-8")
        (self.work / "probe.md").write_text(f"host {HQ_IP}\n", encoding="utf-8")
        commit_all(self.work, "publish the deny list")
        blocked = self.push()
        self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.assertIn("(hq-address)", blocked.stdout + blocked.stderr)
        (self.work / "probe.md").unlink()
        git("add", "-A", cwd=self.work)
        git("commit", "--amend", "-qm", "publish the deny list", cwd=self.work)
        self.assertEqual(self.push().returncode, 0)


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
            (repo / "leak-forbid.txt").write_text(LIST_LINE, encoding="utf-8")
            shutil.copy2(EXAMPLE, repo / "hosts.example")
            git("add", "-A", cwd=repo)
            result = scan(repo)
            self.assertEqual(result.returncode, 0, result.stdout)


class ListCopies(unittest.TestCase):
    """One list, two copies: the repository publishes it, HQ keeps the canon."""

    def test_the_repository_carries_the_list(self) -> None:
        published = ROOT / "leak-forbid.txt"
        self.assertTrue(published.is_file(), "the repository has no root leak-forbid.txt")
        labels = [line for line in published.read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.startswith("#")]
        self.assertGreaterEqual(len(labels), 10, "the published list is short")

    def test_the_hq_canon_and_the_published_copy_agree(self) -> None:
        canon = ROOT / "docs" / "ops" / "leak-forbid.txt"
        if not canon.is_file():
            self.skipTest("no HQ canon in this checkout (a clone carries only the copy)")
        self.assertEqual(canon.read_bytes(), (ROOT / "leak-forbid.txt").read_bytes(),
                         "docs/ops/leak-forbid.txt and leak-forbid.txt differ")

    def test_make_leak_names_the_list(self) -> None:
        result = run(["make", "-n", "leak"], cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--forbid-file", result.stdout)

    def test_make_hooks_installs_the_pre_push_hook(self) -> None:
        result = run(["make", "-n", "hooks"], cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(".githooks/pre-push", result.stdout)


if __name__ == "__main__":
    unittest.main()
