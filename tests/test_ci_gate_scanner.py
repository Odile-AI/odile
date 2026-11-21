#!/usr/bin/python3
"""The gate's event rule: the base's files on a PR, the tree's own on a push.

`publish-lint` and `history-secrets` used to run the base branch's scanner and
allowlist on every event. On a pull request that is the point: a PR that weakens
`tools/leak_scan.py` or widens `leak-allow.tsv` must not pass its own gate. On a
push to main it was wrong: the pushed tree IS main, and the allowlist rows the
merge adds are exactly the rows the pre-push main lacks, so every landing push
was red on main's own head (measured on ODL-911, 2026-11-20: 104 new rows; the
base's allowlist reported 178 tree findings and 3,643 history findings on the
merged head that the tree's own allowlist pins).

These tests pin both halves: the base pair is guarded to `pull_request`, the
tree's own scan runs on every event, and both events are exercised against real
git fixtures. Removing either guard, or letting the base pair run on a push,
turns the suite red.
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

# The gate steps, in the order the runner runs them. On a push the guarded
# `pull_request` steps are skipped, so only the tree step runs.
GATE_STEPS = {
    "publish-lint": (
        "Gate the tree with the base branch's scanner",
        "Scan the tree with its own deny list",
    ),
    "history-secrets": (
        "Gate every blob with the base branch's scanner",
        "Gate every blob with the tree's own deny list",
    ),
}


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


def step_condition(job: str, marker: str) -> str | None:
    """The step's `if:` line, or None when it runs on every event."""
    lines = job_block(job).splitlines()
    start = next(index for index, line in enumerate(lines) if marker in line)
    run = next(index for index in range(start, len(lines))
               if lines[index].strip() == "run: |")
    for line in lines[start:run]:
        if line.strip().startswith("if:"):
            return line.strip()
    return None


def run_gate(job: str, event: str, cwd: Path, base_ref: str = "main") -> subprocess.CompletedProcess:
    """Run the steps the runner would run for `event`, in order.

    Only the `github.event_name == '<event>'` guard shape is used by the two
    gate jobs, so that is the one this runner evaluates.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"EVENT_NAME": event, "BASE_REF": base_ref})
    result: subprocess.CompletedProcess | None = None
    for marker in GATE_STEPS[job]:
        condition = step_condition(job, marker) or ""
        if "pull_request" in condition and event != "pull_request":
            continue
        if "'push'" in condition and event != "push":
            continue
        result = subprocess.run(["/bin/sh", "-c", step_script(job, marker)],
                                cwd=cwd, text=True, capture_output=True, env=env)
        if result.returncode != 0:
            return result
    assert result is not None
    return result


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
parser.add_argument("--forbid-file", default=None)
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


# Assembled from parts: this file must stay clean under the gate it exercises.
HQ_IP = ".".join(["23", "155", "44", "139"])
LIST_TEXT = "hq-address\t" + r"23\.155\.44\.\d+" + "\n"
# A synthetic private literal no built-in class matches, so the tree test
# exercises the deny list (and its allowlist row) alone.
ACCOUNT = "zq" + "7xprivate"
ACCOUNT_LIST = "probe-account\t" + ACCOUNT + "\n"


def make_pair(tmp: Path, base_scanner: str | None = None,
              base_allowlist: str = "# no exceptions\n") -> tuple[Path, Path, str]:
    """A bare origin, a base repo on `main`, and a clone of it as the tree."""
    origin = tmp / "origin.git"
    base_repo = tmp / "base"
    tree = tmp / "tree"
    git("init", "-q", "--bare", "--initial-branch=main", str(origin), cwd=tmp)
    git("init", "-q", "--initial-branch=main", str(base_repo), cwd=tmp)
    (base_repo / "tools").mkdir()
    scanner = Path(base_scanner) if base_scanner else ROOT / "tools" / "leak_scan.py"
    shutil.copy2(scanner, base_repo / "tools" / "leak_scan.py")
    (base_repo / "leak-allow.tsv").write_text(base_allowlist, encoding="utf-8")
    (base_repo / "README.md").write_text("clean tree\n", encoding="utf-8")
    commit_all(base_repo, "base")
    base_sha = git("rev-parse", "HEAD", cwd=base_repo).stdout.strip()
    git("remote", "add", "origin", str(origin), cwd=base_repo)
    git("push", "-q", "origin", "main", cwd=base_repo)
    git("clone", "-q", "-b", "main", str(origin), str(tree), cwd=tmp)
    return origin, base_repo, base_sha


class PublishLintUsesTheBaseScanner(unittest.TestCase):
    def setUp(self):
        self.block = job_block("publish-lint")

    def test_the_base_gate_is_the_pull_request_gate(self):
        # A PR cannot widen its own gate; the base's files run there and only
        # there. A push to main runs the tree's own files instead.
        self.assertEqual(
            step_condition("publish-lint", "Gate the tree with the base branch's scanner"),
            "if: github.event_name == 'pull_request'")

    def test_it_runs_the_base_scanner(self):
        self.assertIn("git show \"$base_sha:tools/leak_scan.py\"", self.block)
        self.assertIn("/tmp/base-leak_scan.py --tree", self.block)

    def test_it_runs_the_base_allowlist(self):
        self.assertIn("git show \"$base_sha:leak-allow.tsv\"", self.block)
        self.assertIn("cp /tmp/base-leak-allow.tsv leak-allow.tsv", self.block)

    def test_it_fetches_the_base_branch(self):
        self.assertIn("git fetch", self.block)
        self.assertIn("github.base_ref", self.block)

    def test_the_base_pass_runs_before_the_tree_scanner(self):
        base = self.block.index("/tmp/base-leak_scan.py --tree")
        own = self.block.index("tools/leak_scan.py --tree", base)
        self.assertLess(base, own, "the tree's own scanner must not run first")

    def test_the_tree_scan_is_the_push_gate(self):
        marker = "Scan the tree with its own deny list"
        self.assertIsNone(step_condition("publish-lint", marker),
                          "the tree's own scan must run on every event")
        script = step_script("publish-lint", marker)
        self.assertIn("tools/leak_scan.py --tree", script)
        self.assertIn("--forbid-file docs/ops/leak-forbid.txt", script)
        # On a push the tree's own allowlist is used: no base file is copied.
        self.assertNotIn("base-leak", script)
        self.assertNotIn("cp ", script)

    def test_the_deny_list_comes_from_the_repository_secret(self):
        # Neither the PR nor the base can edit the list out of this checkout:
        # it arrives as LEAK_FORBID_B64 and is decoded to the documented path.
        self.assertIn("secrets.LEAK_FORBID_B64", self.block)
        self.assertIn("base64 -d > docs/ops/leak-forbid.txt", self.block)
        self.assertNotIn('git show "$base_sha:leak-forbid.txt"', self.block)
        self.assertNotIn("test -f leak-forbid.txt", self.block)

    # -- behaviour: run the job's own shells against a fixture tree ----------
    def test_the_step_fails_a_pr_that_weakens_its_own_scanner(self):
        tmp = Path(tempfile.mkdtemp(prefix="ci-gate-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        strict = tmp / "strict-scanner.py"
        strict.write_text(STRICT_SCANNER, encoding="utf-8")
        origin, base_repo, base_sha = make_pair(tmp, base_scanner=str(strict))
        tree = tmp / "tree"

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

        result = run_gate("publish-lint", "pull_request", tree)
        self.assertNotEqual(result.returncode, 0,
                            "the base gate passed a self-weakened tree")
        self.assertIn("FINDING public-ip README.md:1", result.stdout + result.stderr)
        self.assertIn("FAIL: 1 finding(s)", result.stdout + result.stderr)

    def test_a_push_to_main_gates_the_tree_with_its_own_allowlist(self):
        # The landing push shape: the merged tree carries a live value its own
        # allowlist pins at that path, and the pre-push main's allowlist does
        # not. The push must be green, and the base gate that used to run on it
        # must be red on the same tree — that is the regression this pins.
        tmp = Path(tempfile.mkdtemp(prefix="ci-push-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        origin, base_repo, base_sha = make_pair(tmp)
        tree = tmp / "tree"

        (tree / "notes.md").write_text(f"account {ACCOUNT}\n", encoding="utf-8")
        digest = hashlib.sha256(ACCOUNT.encode()).hexdigest()[:12]
        with (tree / "leak-allow.tsv").open("a", encoding="utf-8") as handle:
            handle.write("forbidden-value\tnotes.md\tsha256:"
                         f"{digest}\tconstant\tno\tthe live account id at its path\tnever\n")
        (tree / "docs" / "ops").mkdir(parents=True)
        (tree / "docs" / "ops" / "leak-forbid.txt").write_text(ACCOUNT_LIST, encoding="utf-8")
        commit_all(tree, "the merged tree")
        tree_allowlist = (tree / "leak-allow.tsv").read_text(encoding="utf-8")

        pushed = run_gate("publish-lint", "push", tree)
        self.assertEqual(pushed.returncode, 0, pushed.stdout + pushed.stderr)
        self.assertIn("PASS: nothing to leak", pushed.stdout)

        # What the push used to run: the base scanner and allowlist. Red.
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env["BASE_REF"] = "main"
        base = subprocess.run(
            ["/bin/sh", "-c", step_script("publish-lint",
                                          "Gate the tree with the base branch's scanner")],
            cwd=tree, text=True, capture_output=True, env=env)
        self.assertNotEqual(base.returncode, 0,
                            "the base gate passed the merged tree it used to fail")
        self.assertIn("FINDING forbidden-value", base.stdout + base.stderr)
        self.assertIn("notes.md", base.stdout + base.stderr)
        # Its restore is a trap, so the red path leaves the tree's allowlist.
        self.assertEqual((tree / "leak-allow.tsv").read_text(encoding="utf-8"),
                         tree_allowlist)


class HistoryUsesTheBaseScanner(unittest.TestCase):
    """The history job: the base's files on a PR, the tree's own on a push.

    On a PR it must not run the files it is gating: a branch that added a
    literal in one commit and deleted it in the next (a clean tree) could waive
    its own history finding with one appended allowlist row — the "a commit that
    is later deleted" surface the job exists for. On a push to main the tree's
    own scanner and allowlist run, because the rows a merge adds are exactly the
    rows its base lacks.
    """

    def setUp(self) -> None:
        self.block = job_block("history-secrets")

    def test_the_base_gate_is_the_pull_request_gate(self):
        self.assertEqual(
            step_condition("history-secrets", "Gate every blob with the base branch's scanner"),
            "if: github.event_name == 'pull_request'")

    def test_it_runs_the_base_scanner(self):
        self.assertIn('git show "$base_sha:tools/leak_scan.py"', self.block)
        self.assertIn("scanner=/tmp/base-leak_scan.py", self.block)
        self.assertIn('"$scanner" --history', self.block)

    def test_a_base_that_predates_the_flag_falls_back_with_a_line(self):
        # `--forbid-only` lands with this branch, so the base of the pull
        # request that carries it has a scanner that rejects the flag. The step
        # probes for it, says so, and uses the tree's scanner for that one run;
        # every later run reads the base's.
        self.assertIn("--help", self.block)
        self.assertIn("predates --forbid-only", self.block)
        self.assertIn("scanner=tools/leak_scan.py", self.block)

    def test_it_runs_the_base_allowlist_and_the_secret_list(self):
        self.assertIn('git show "$base_sha:leak-allow.tsv"', self.block)
        self.assertIn("cp /tmp/base-leak-allow.tsv leak-allow.tsv", self.block)
        # The list is not a file in either checkout: it is the secret, decoded
        # to the documented path by the step above.
        self.assertIn("secrets.LEAK_FORBID_B64", self.block)
        self.assertIn("base64 -d > docs/ops/leak-forbid.txt", self.block)
        self.assertIn("--forbid-file docs/ops/leak-forbid.txt", self.block)
        self.assertNotIn('git show "$base_sha:leak-forbid.txt"', self.block)

    def test_it_fetches_the_base_branch(self):
        self.assertIn("github.base_ref", self.block)
        self.assertIn("git fetch", self.block)

    def test_the_class_is_scoped_to_the_deny_list(self):
        # The replaced roots' public addresses must not fail a job about the
        # deny list; every blob is still read.
        self.assertIn("--forbid-only", self.block)

    def test_a_run_without_the_list_skips_with_a_line(self):
        # No secret installed the list: the class has nothing to run against,
        # and the skip must say so, never pass silently. The same line names
        # the secret, so an operator knows what to set.
        script = step_script("history-secrets", "Gate every blob with the tree's own deny list")
        self.assertIn("[ ! -f docs/ops/leak-forbid.txt ]", script)
        self.assertIn("forbid list: absent", script)

    def test_the_tree_scan_is_the_push_gate(self):
        marker = "Gate every blob with the tree's own deny list"
        self.assertIsNone(step_condition("history-secrets", marker),
                          "the tree's own history scan must run on every event")
        script = step_script("history-secrets", marker)
        self.assertIn("tools/leak_scan.py --history", script)
        self.assertIn("--forbid-file docs/ops/leak-forbid.txt", script)
        self.assertIn("--forbid-only", script)
        self.assertNotIn("base-leak", script)

    def test_a_pr_cannot_waive_its_own_history_finding(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="ci-history-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        origin, base_repo, base_sha = make_pair(tmp)
        tree = tmp / "tree"

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

        # The list arrives from the secret, at the documented path (the CI
        # step installs it before either scan).
        (tree / "docs" / "ops").mkdir(parents=True, exist_ok=True)
        (tree / "docs" / "ops" / "leak-forbid.txt").write_text(LIST_TEXT, encoding="utf-8")

        # The gate this replaces passes: the PR's own scanner, list and row.
        own = subprocess.run([sys.executable, "tools/leak_scan.py", "--history",
                              "--forbid-file", "docs/ops/leak-forbid.txt", "--forbid-only"],
                             cwd=tree, text=True, capture_output=True)
        self.assertEqual(own.returncode, 0, own.stdout + own.stderr)

        pr_allowlist = (tree / "leak-allow.tsv").read_text(encoding="utf-8")
        result = run_gate("history-secrets", "pull_request", tree)
        self.assertNotEqual(result.returncode, 0,
                            "the history gate let a PR waive its own finding")
        self.assertIn("FINDING forbidden-value", result.stdout + result.stderr)
        self.assertIn("probe.md:1", result.stdout + result.stderr)
        # The restore is an EXIT trap, not a command the scan's non-zero exit
        # would skip, so the red path leaves the PR's own allowlist in place.
        self.assertEqual((tree / "leak-allow.tsv").read_text(encoding="utf-8"), pr_allowlist,
                         "the red path left the base's allowlist in the checkout")

    def test_a_push_to_main_passes_the_rows_its_base_lacks(self) -> None:
        # The same branch, once merged: the literal is only in history and the
        # tree's own allowlist row pins it. On the landing push the tree's own
        # files run and the job is green; the base gate that used to run on the
        # push is red on the same tree. This is the round-5 regression.
        tmp = Path(tempfile.mkdtemp(prefix="ci-history-push-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        origin, base_repo, base_sha = make_pair(tmp)
        tree = tmp / "tree"

        (tree / "probe.md").write_text(f"host {HQ_IP}\n", encoding="utf-8")
        commit_all(tree, "add a probe")
        (tree / "probe.md").unlink()
        commit_all(tree, "remove the probe again")
        digest = hashlib.sha256(HQ_IP.encode()).hexdigest()[:12]
        with (tree / "leak-allow.tsv").open("a", encoding="utf-8") as handle:
            handle.write(f"forbidden-value\tprobe.md\tsha256:{digest}\tconstant\tno\ta probe\tnever\n")
        (tree / "docs" / "ops").mkdir(parents=True, exist_ok=True)
        (tree / "docs" / "ops" / "leak-forbid.txt").write_text(LIST_TEXT, encoding="utf-8")
        commit_all(tree, "the merge's own allowlist row")

        pushed = run_gate("history-secrets", "push", tree)
        self.assertEqual(pushed.returncode, 0, pushed.stdout + pushed.stderr)
        self.assertIn("PASS: nothing to leak", pushed.stdout)

        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env["BASE_REF"] = "main"
        base = subprocess.run(
            ["/bin/sh", "-c", step_script("history-secrets",
                                          "Gate every blob with the base branch's scanner")],
            cwd=tree, text=True, capture_output=True, env=env)
        self.assertNotEqual(base.returncode, 0,
                            "the base gate passed the merged history it used to fail")
        self.assertIn("FINDING forbidden-value", base.stdout + base.stderr)
        self.assertIn("probe.md:1", base.stdout + base.stderr)

    def test_a_base_without_the_flag_still_catches_the_literal(self) -> None:
        # The transition run: the base scanner knows only `--history`, so the
        # flag probe must fall back to the tree's scanner. A base scanner that
        # would pass everything is the discriminating control — if the step ran
        # it with the flag appended the run would die, and if it ran it without
        # the flag this PR's literal would pass. Both are red here.
        tmp = Path(tempfile.mkdtemp(prefix="ci-history-old-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        old = tmp / "old-scanner.py"
        old.write_text(
            "#!/usr/bin/python3\nimport sys\nprint('PASS: a base scanner that knows nothing')\n"
            "sys.exit(0)\n", encoding="utf-8")
        origin, base_repo, base_sha = make_pair(tmp, base_scanner=str(old))
        tree = tmp / "tree"

        # The PR's tree carries the real scanner (the branch that lands the
        # flag); only the BASE is old.
        shutil.copy2(ROOT / "tools" / "leak_scan.py", tree / "tools" / "leak_scan.py")
        (tree / "probe.md").write_text(f"host {HQ_IP}\n", encoding="utf-8")
        commit_all(tree, "a literal the base scanner cannot see")
        (tree / "docs" / "ops").mkdir(parents=True)
        (tree / "docs" / "ops" / "leak-forbid.txt").write_text(LIST_TEXT, encoding="utf-8")

        result = run_gate("history-secrets", "pull_request", tree)
        self.assertIn("predates --forbid-only", result.stdout + result.stderr)
        self.assertNotEqual(result.returncode, 0,
                            "the transition run passed a literal its base scanner cannot see")
        self.assertIn("FINDING forbidden-value", result.stdout + result.stderr)


class TheSkipLinesNameBothDenyListClasses(unittest.TestCase):
    """Every "the class is off" line names both deny-list classes.

    The gate runs two deny-list classes now, `forbidden-value` and the
    `forbidden-path` class that landed beside it, and both skip together when
    the secret is absent. A message that still says "the forbidden-value class"
    (or "the history deny-list class") tells an operator one class was lost
    when two were, so the skip line has to name the pair. Pinned over the whole
    workflow, not one step: the absent branches are the path a run takes when
    nobody armed the secret, which is exactly when the line is read.
    """

    def test_every_class_off_line_names_both_classes(self) -> None:
        lines = [line.strip() for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
                 if "class" in line and " off" in line]
        self.assertTrue(lines, "the workflow has no skip line to check")
        for line in lines:
            with self.subTest(line=line):
                self.assertIn("forbidden-value", line)
                self.assertIn("forbidden-path", line)


if __name__ == "__main__":
    unittest.main()
