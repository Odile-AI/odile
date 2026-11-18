#!/usr/bin/python3
"""The publication gate must not run the scanner it is gating.

`publish-lint` used to execute `tools/leak_scan.py` and `leak-allow.tsv` out of
the pull request's own checkout, so a PR that weakened the scanner or widened
the allowlist passed its own gate. The job now runs the BASE branch's scanner
with the BASE branch's allowlist over the PR's tree, then the tree's own scanner.
These tests pin that shape: removing either base copy, or moving the tree's own
scanner in front of it, turns the suite red.
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
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"


def job_block(name: str) -> str:
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.rstrip() == f"  {name}:":
            start = index
            break
    if start is None:
        raise AssertionError(f"the workflow has no {name!r} job")
    block = [lines[start]]
    for line in lines[start + 1:]:
        if line.startswith("  ") and not line.startswith("    ") and line.strip().endswith(":"):
            break
        block.append(line)
    return "\n".join(block)


def step_script(job: str, marker: str) -> str:
    """The shell of one step, dedented exactly as the runner gets it."""
    lines = job_block(job).splitlines()
    start = next(index for index, line in enumerate(lines) if marker in line)
    run = next(index for index in range(start, len(lines))
               if lines[index].strip() == "run: |")
    body: list[str] = []
    for line in lines[run + 1:]:
        if line.strip() and not line.startswith("          "):
            break
        body.append(line[10:] if line.startswith("          ") else "")
    return "\n".join(body)


def base_gate_script() -> str:
    """The shell of the base-scanner step, dedented exactly as the runner gets it."""
    return step_script("publish-lint", "Gate the tree with the base branch's scanner")


def history_gate_script() -> str:
    """The history job's deny-list step: base scanner, base allowlist, base list."""
    return step_script("history-secrets", "Gate every blob with the base branch's scanner")


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(["git", *args], cwd=cwd, text=True,
                          capture_output=True, env=env)


def commit_all(repo: Path, message: str) -> None:
    git("config", "user.email", "probe@example.invalid", cwd=repo)
    git("config", "user.name", "probe", cwd=repo)
    git("config", "commit.gpgsign", "false", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", message, cwd=repo)


STRICT_SCANNER = """#!/usr/bin/python3
import argparse, pathlib, sys
parser = argparse.ArgumentParser()
parser.add_argument("--tree", action="store_true")
parser.add_argument("--root", default=".")
args = parser.parse_args()
root = pathlib.Path(args.root)
findings = []
for path in sorted(root.rglob("README.md")):
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if "198." + "18.7.7" in line:
            findings.append(f"{path.relative_to(root)}:{number}")
if findings:
    print(f"FAIL: {len(findings)} finding(s)")
    for finding in findings:
        print("FINDING public-ip", finding)
    sys.exit(1)
print("PASS: nothing to leak")
"""

WEAKENED_SCANNER = """#!/usr/bin/python3
import sys
sys.exit(0)
"""


class PublishLintUsesTheBaseScanner(unittest.TestCase):
    def setUp(self):
        self.block = job_block("publish-lint")

    def test_it_runs_the_base_scanner(self):
        self.assertIn("git show \"$base_sha:tools/leak_scan.py\"", self.block)
        self.assertIn("/tmp/base-leak_scan.py --tree", self.block)

    def test_it_runs_the_base_allowlist(self):
        self.assertIn("git show \"$base_sha:leak-allow.tsv\"", self.block)
        self.assertIn("cp /tmp/base-leak-allow.tsv leak-allow.tsv", self.block)

    def test_it_fetches_the_base_branch(self):
        self.assertIn("git fetch", self.block)
        self.assertIn("github.base_ref", self.block)

    def test_a_push_uses_the_event_before_as_the_base(self):
        self.assertIn("github.event.before", self.block)
        self.assertIn("PUSH_BASE", self.block)

    def test_the_base_pass_runs_before_the_tree_scanner(self):
        base = self.block.index("/tmp/base-leak_scan.py --tree")
        own = self.block.index("tools/leak_scan.py --tree", base)
        self.assertLess(base, own, "the tree's own scanner must not run first")

    def test_the_job_still_scans_with_the_tree_scanner(self):
        self.assertIn("tools/leak_scan.py --tree", self.block)
        # The repository publishes the deny list at its root, so the tree's own
        # scan always names it; a missing list is a finding (exit 2), not a
        # silent skip. The comment in the workflow says so; the `test -f` is
        # the same promise made by the shell.
        self.assertIn("--forbid-file leak-forbid.txt", self.block)
        self.assertIn("test -f leak-forbid.txt", self.block)

    # -- behaviour: run the step's own shell against a fixture tree ----------
    def test_the_step_fails_a_pr_that_weakens_its_own_scanner(self):
        tmp = Path(tempfile.mkdtemp(prefix="ci-gate-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        origin = tmp / "origin.git"
        base_repo = tmp / "base"
        tree = tmp / "tree"
        git("init", "-q", "--bare", "--initial-branch=main", str(origin), cwd=tmp)
        git("init", "-q", "--initial-branch=main", str(base_repo), cwd=tmp)
        (base_repo / "tools").mkdir()
        (base_repo / "tools" / "leak_scan.py").write_text(STRICT_SCANNER, encoding="utf-8")
        (base_repo / "leak-allow.tsv").write_text("# path\tsha256\treason\texpiry\n", encoding="utf-8")
        (base_repo / "README.md").write_text("clean tree\n", encoding="utf-8")
        commit_all(base_repo, "base")
        base_sha = git("rev-parse", "HEAD", cwd=base_repo).stdout.strip()
        git("remote", "add", "origin", str(origin), cwd=base_repo)
        git("push", "-q", "origin", "main", cwd=base_repo)
        git("clone", "-q", "-b", "main", str(origin), str(tree), cwd=tmp)

        # The pull request: a scanner that always passes, a self-granted
        # allowlist row, and the leaked address the base scanner knows.
        (tree / "tools" / "leak_scan.py").write_text(WEAKENED_SCANNER, encoding="utf-8")
        (tree / "leak-allow.tsv").write_text(
            "# path\tsha256\treason\texpiry\nREADME.md\tdeadbeef\tmine\t2099-01-01\n",
            encoding="utf-8")
        (tree / "README.md").write_text("address " + "198." + "18.7.7" + "\n", encoding="utf-8")
        commit_all(tree, "weaken the gate")

        own = subprocess.run([sys.executable, "tools/leak_scan.py", "--tree"],
                             cwd=tree, text=True, capture_output=True)
        self.assertEqual(own.returncode, 0, own.stdout + own.stderr)

        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"EVENT_NAME": "push", "REF_NAME": "main", "PUSH_BASE": base_sha,
                    "BASE_REF": "main"})
        result = subprocess.run(["/bin/sh", "-c", base_gate_script()],
                                cwd=tree, text=True, capture_output=True, env=env)
        self.assertNotEqual(result.returncode, 0,
                            "the base gate passed a self-weakened tree")
        self.assertIn("FINDING public-ip README.md:1", result.stdout + result.stderr)
        self.assertIn("FAIL: 1 finding(s)", result.stdout + result.stderr)


# Assembled from parts: this file must stay clean under the gate it exercises.
HQ_IP = ".".join(["23", "155", "44", "139"])
LIST_TEXT = "hq-address\t" + r"23\.155\.44\.\d+" + "\n"


class HistoryUsesTheBaseScanner(unittest.TestCase):
    """The history job must not run the files it is gating.

    It used to run the PR checkout's own scanner and allowlist, so a branch
    that added a literal in one commit and deleted it in the next could waive
    its own history finding with one appended row — the "a commit that is later
    deleted" surface the job exists for. It now runs the base branch's scanner,
    allowlist and list, the mirror of `publish-lint`.
    """

    def setUp(self) -> None:
        self.block = job_block("history-secrets")

    def test_it_runs_the_base_scanner(self):
        self.assertIn('git show "$base_sha:tools/leak_scan.py"', self.block)
        self.assertIn("/tmp/base-leak_scan.py --history", self.block)
        self.assertNotIn("python3 tools/leak_scan.py --history", self.block)

    def test_it_runs_the_base_allowlist_and_the_base_list(self):
        self.assertIn('git show "$base_sha:leak-allow.tsv"', self.block)
        self.assertIn("cp /tmp/base-leak-allow.tsv leak-allow.tsv", self.block)
        self.assertIn('git show "$base_sha:leak-forbid.txt"', self.block)
        self.assertIn("--forbid-file /tmp/base-leak-forbid.txt", self.block)

    def test_it_fetches_the_base_branch(self):
        self.assertIn("github.base_ref", self.block)
        self.assertIn("github.event.before", self.block)
        self.assertIn("git fetch", self.block)

    def test_the_class_is_scoped_to_the_deny_list(self):
        # The replaced roots' public addresses must not fail a job about the
        # deny list; every blob is still read.
        self.assertIn("--forbid-only", self.block)

    def test_a_base_without_the_list_skips_with_a_line(self):
        # The list and the class landed together, so a base that predates the
        # list has nothing to run; the skip must say so, never pass silently.
        self.assertIn('git cat-file -e "$base_sha:leak-forbid.txt"', self.block)
        self.assertIn("absent at base", self.block)

    def test_a_pr_cannot_waive_its_own_history_finding(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="ci-history-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        origin = tmp / "origin.git"
        base_repo = tmp / "base"
        tree = tmp / "tree"
        git("init", "-q", "--bare", "--initial-branch=main", str(origin), cwd=tmp)
        git("init", "-q", "--initial-branch=main", str(base_repo), cwd=tmp)
        (base_repo / "tools").mkdir()
        # The base's scanner is the real one, so the behaviour under test is the
        # job's file choice, not a stub's.
        shutil.copy2(ROOT / "tools" / "leak_scan.py", base_repo / "tools" / "leak_scan.py")
        (base_repo / "leak-allow.tsv").write_text("# no exceptions\n", encoding="utf-8")
        (base_repo / "leak-forbid.txt").write_text(LIST_TEXT, encoding="utf-8")
        (base_repo / "README.md").write_text("clean tree\n", encoding="utf-8")
        commit_all(base_repo, "base")
        base_sha = git("rev-parse", "HEAD", cwd=base_repo).stdout.strip()
        git("remote", "add", "origin", str(origin), cwd=base_repo)
        git("push", "-q", "origin", "main", cwd=base_repo)
        git("clone", "-q", "-b", "main", str(origin), str(tree), cwd=tmp)

        # The pull request: the literal in one commit, deleted in the next (a
        # clean tree), plus one allowlist row of its own that pins the value.
        (tree / "probe.md").write_text(f"host {HQ_IP}\n", encoding="utf-8")
        commit_all(tree, "add a probe")
        (tree / "probe.md").unlink()
        commit_all(tree, "remove the probe again")
        digest = hashlib.sha256(HQ_IP.encode()).hexdigest()[:12]
        with (tree / "leak-allow.tsv").open("a", encoding="utf-8") as handle:
            handle.write(f"forbidden-value\t*\tsha256:{digest}\tconstant\tno\ta probe\tnever\n")
        commit_all(tree, "waive my own history finding")
        self.assertEqual(git("push", "-q", "origin", "HEAD:pr", cwd=tree).returncode, 0)

        # The gate this replaces passes: the PR's own scanner, list and row.
        own = subprocess.run([sys.executable, "tools/leak_scan.py", "--history",
                              "--forbid-file", "leak-forbid.txt", "--forbid-only"],
                             cwd=tree, text=True, capture_output=True)
        self.assertEqual(own.returncode, 0, own.stdout + own.stderr)

        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"EVENT_NAME": "push", "REF_NAME": "pr", "BASE_REF": "main",
                    "PUSH_BASE": base_sha})
        result = subprocess.run(["/bin/sh", "-c", history_gate_script()], cwd=tree,
                                text=True, capture_output=True, env=env)
        self.assertNotEqual(result.returncode, 0,
                            "the history gate let a PR waive its own finding")
        self.assertIn("FINDING forbidden-value", result.stdout + result.stderr)
        self.assertIn("probe.md:1", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
