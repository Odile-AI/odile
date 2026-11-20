"""The HQ half of the handoff gate, proved on a fixture HQ and a fixture branch.

A card worktree is a fresh checkout, so `unittest discover -s tests` there never
sees the modules that exist only at HQ: measured at `main` `2b4fb01`, 79 test
modules on disk against 64 tracked, so a handoff quoting "1947 tests" was a
subset gate against the merge gate's 2293. `tools/hq_gate.py` closes that gap:
it builds a throwaway worktree from the branch's *commit*, overlays every file
HQ's git does not track (the private machinery — `docs/backlog.yaml`,
`docs/craft/`, `tools/board_sync.py` …), and runs the HQ-only modules there, so
they read the branch's own documents rather than main's.

These tests pin the properties the gate's answer depends on:

* the branch's committed tree is what is under test — a violation that only
  exists in the commit is red, and the same tree without it is green;
* an HQ-only path the branch also tracks keeps the branch's bytes (the overlay
  must never overwrite a tracked file);
* the overlay really is there: a module that only exists at HQ runs at all;
* no HQ checkout means "skipped", not "failed", so a public clone still passes;
* the throwaway worktree does not survive the run.

Every fixture repo runs with `GIT_*` scrubbed, or `.githooks/pre-commit`'s
exported `GIT_DIR` makes the fixture commits land on the outer branch.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import hq_gate  # noqa: E402

# The module HQ carries and the branch cannot: its assertions are the proof that
# the branch's documents are what the gate examined.
HQ_ONLY_MODULE = '''\
"""Fixture: runs only where the HQ-only machinery is overlaid."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class OverlayTest(unittest.TestCase):
    def test_the_private_document_is_overlaid(self):
        self.assertEqual((ROOT / "docs" / "backlog.yaml").read_text().strip(), "hq-only")

    def test_the_branch_document_is_the_one_under_test(self):
        self.assertEqual((ROOT / "docs" / "branch.md").read_text().strip(), "GOOD")

    def test_a_tracked_branch_file_keeps_the_branch_bytes(self):
        self.assertEqual((ROOT / "tools" / "owned.py").read_text().strip(), "BRANCH")
'''


def clean_env() -> dict[str, str]:
    """The environment minus `GIT_*` (the hook exports the outer repository)."""
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def git(*args: str, cwd: Path) -> None:
    subprocess.run(("git", *args), cwd=str(cwd), check=True, capture_output=True,
                   text=True, env=clean_env())


def quiet_main(args: list[str]) -> tuple[int, str]:
    """`hq_gate.main` with its own output captured into a string."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = hq_gate.main(args)
    return code, buffer.getvalue()


class FixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="hq-gate-test-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.base = base
        self.hq = base / "hq"
        self.tree = base / "tree"
        self.make_hq()
        self.make_tree()

    def make_hq(self) -> None:
        """A fixture HQ: tracked docs, and the private files git there ignores."""
        self.hq.mkdir(parents=True)
        git("init", "-q", "-b", "main", cwd=self.hq)
        git("config", "user.email", "fixture@example.com", cwd=self.hq)
        git("config", "user.name", "fixture", cwd=self.hq)
        (self.hq / "docs").mkdir()
        (self.hq / "docs" / "README.md").write_text("public\n", encoding="utf-8")
        git("add", "-A", cwd=self.hq)
        git("commit", "-q", "-m", "hq", cwd=self.hq)
        (self.hq / ".git" / "info" / "exclude").write_text(
            "docs/backlog.yaml\n"
            "tests/test_hq_only.py\n"
            "tools/owned.py\n"
            "tests/__pycache__/\n"
            ".worktrees/\n",
            encoding="utf-8",
        )
        (self.hq / "docs" / "backlog.yaml").write_text("hq-only\n", encoding="utf-8")
        (self.hq / "tests").mkdir()
        (self.hq / "tests" / "test_hq_only.py").write_text(HQ_ONLY_MODULE, encoding="utf-8")
        (self.hq / "tools").mkdir()
        (self.hq / "tools" / "owned.py").write_text("HQ\n", encoding="utf-8")
        (self.hq / "tests" / "__pycache__").mkdir()
        (self.hq / "tests" / "__pycache__" / "stale.pyc").write_bytes(b"\0")
        sibling = self.hq / ".worktrees" / "sibling" / "tests"
        sibling.mkdir(parents=True)
        (sibling / "test_sibling.py").write_text(
            "raise SystemExit('the sibling worktree was overlaid')\n", encoding="utf-8")

    def make_tree(self) -> None:
        """A fixture card worktree: the branch's own commit, plus its own tool."""
        self.tree.mkdir(parents=True)
        git("init", "-q", "-b", "main", cwd=self.tree)
        git("config", "user.email", "fixture@example.com", cwd=self.tree)
        git("config", "user.name", "fixture", cwd=self.tree)
        (self.tree / "docs").mkdir()
        (self.tree / "docs" / "branch.md").write_text("GOOD\n", encoding="utf-8")
        (self.tree / "tools").mkdir()
        (self.tree / "tools" / "owned.py").write_text("BRANCH\n", encoding="utf-8")
        git("add", "-A", cwd=self.tree)
        git("commit", "-q", "-m", "branch", cwd=self.tree)

    def commit_branch_doc(self, text: str) -> None:
        (self.tree / "docs" / "branch.md").write_text(text + "\n", encoding="utf-8")
        git("commit", "-q", "-am", f"branch doc {text}", cwd=self.tree)

    def run_gate(self, *args: str) -> tuple[int, str]:
        return quiet_main(["--hq", str(self.hq), "--tree", str(self.tree), *args])


class HqOnlySetTest(FixtureCase):
    def test_the_hq_only_set_is_the_ignored_files_without_noise(self):
        tracked = hq_gate.tracked_files(self.tree)
        found = hq_gate.hq_only_files(self.hq, tracked)
        self.assertIn("tests/test_hq_only.py", found)
        self.assertIn("docs/backlog.yaml", found)
        # A path the branch tracks is the branch's business, even when HQ
        # ignores a file at the same path.
        self.assertNotIn("tools/owned.py", found)
        # Noise: byte-code caches and sibling worktrees are not HQ-only files.
        self.assertFalse([path for path in found if "__pycache__" in path], found)
        self.assertFalse([path for path in found if path.startswith(".worktrees/")], found)

    def test_the_hq_only_modules_are_found_by_name(self):
        tracked = hq_gate.tracked_files(self.tree)
        self.assertEqual(hq_gate.hq_only_modules(self.hq, tracked), ["test_hq_only"])


class GateRunTest(FixtureCase):
    def test_a_clean_branch_passes_and_names_what_it_ran(self):
        code, output = self.run_gate()
        self.assertEqual(code, 0, output)
        self.assertRegex(output, r"pass\s+test_hq_only \(Ran \d+ tests? in ")
        self.assertIn("Ran 3 tests", output)
        self.assertIn("hq-gate: OK", output)

    def test_a_violation_only_in_the_commit_is_red(self):
        self.commit_branch_doc("BAD")
        code, output = self.run_gate()
        self.assertEqual(code, 1, output)
        self.assertRegex(output, r"FAIL\s+test_hq_only")
        self.assertIn("the_branch_document_is_the_one_under_test", output)
        self.assertIn("hq-gate: FAILED", output)

    def test_an_uncommitted_edit_is_not_what_is_tested(self):
        # The gate runs the branch's commit — the artifact the verifier merges —
        # so a working-tree edit can change neither verdict.
        (self.tree / "docs" / "branch.md").write_text("BAD\n", encoding="utf-8")
        code, output = self.run_gate()
        self.assertEqual(code, 0, output)

    def test_the_throwaway_worktree_is_removed(self):
        before = hq_gate.git("worktree", "list", "--porcelain", cwd=self.tree).stdout
        self.run_gate()
        after = hq_gate.git("worktree", "list", "--porcelain", cwd=self.tree).stdout
        self.assertEqual(before, after)

    def test_no_hq_checkout_is_a_skip_not_a_failure(self):
        # The public tree's case: `tools/hq_gate.py` ships, the HQ checkout does not.
        empty = self.base / "empty-hq"
        empty.mkdir()
        code, output = quiet_main(["--hq", str(empty), "--tree", str(self.tree)])
        self.assertEqual(code, 0, output)
        self.assertIn("skipped", output)

    def test_the_default_hq_is_this_repositorys_primary_checkout(self):
        # A card worktree configures nothing: `--git-common-dir` names the
        # primary checkout, which is where the private machinery lives. In the
        # fixture the tree is its own primary checkout, so the default resolves
        # to it — and it holds no HQ-only module, which is the public tree's
        # answer too.
        self.assertEqual(hq_gate.main_checkout(self.tree), self.tree.resolve())
        code, output = quiet_main(["--tree", str(self.tree)])
        self.assertEqual(code, 0, output)
        self.assertIn("no HQ-only test module", output)

    def test_a_tree_is_required(self):
        empty = self.base / "empty-tree"
        empty.mkdir()
        code, output = quiet_main(["--hq", str(self.hq), "--tree", str(empty)])
        self.assertEqual(code, 2, output)


if __name__ == "__main__":
    unittest.main()
