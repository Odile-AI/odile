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
* `main`'s side of the merge arrives: what main gained after the branch was cut
  (ODL-941), what main repaired while the branch left it alone (ODL-479), a path
  both sides changed keeps the branch's bytes, a path the branch deleted is
  not resurrected, and a path `main` deleted while the branch left it alone is
  removed from the preview (or the gate reds on a tree the merge never makes);
* the committed bytes arrive, not HQ's working copy, which is often dirty;
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
import os
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

    def test_a_file_only_main_tracks_is_overlaid(self):
        # The branch was cut before main gained this playbook (ODL-941's shape).
        path = ROOT / "docs" / "craft" / "finance.md"
        self.assertTrue(path.is_file(), f"missing {path}")
        self.assertEqual(path.read_text().strip(), "main-only")

    def test_a_file_only_main_tracks_keeps_its_mode(self):
        self.assertTrue(os.access(ROOT / "tools" / "moved.py", os.X_OK),
                        "main's executable bit did not survive the overlay")
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
        # `main` moves after the card's branch was cut — ODL-941's shape, where
        # main had gained docs/craft/finance.md and the branch's HEAD never did.
        (self.hq / "docs" / "craft").mkdir()
        (self.hq / "docs" / "craft" / "finance.md").write_text("main-only\n", encoding="utf-8")
        (self.hq / "tools").mkdir()
        (self.hq / "tools" / "moved.py").write_text("#!/usr/bin/python3\n", encoding="utf-8")
        (self.hq / "tools" / "moved.py").chmod(0o755)
        git("add", "-A", cwd=self.hq)
        git("commit", "-q", "-m", "main gains a playbook", cwd=self.hq)
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
        (self.hq / "tools").mkdir(exist_ok=True)
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
        self.assertIn("Ran 5 tests", output)
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


class MainAheadTest(FixtureCase):
    """A branch cut before `main` moved — ODL-941, 2026-11-20.

    `main` had gained `docs/craft/finance.md`; the branch's HEAD never had it.
    HQ *tracks* the file, so the ignored-and-untracked list does not name it and
    the gate's throwaway tree went without it: `test_craft` failed on a card
    that had touched no craft document. The overlay has to carry `main`'s files
    too, or the gate runs a tree the merge will never produce.
    """

    def test_a_file_only_main_tracks_is_in_the_overlay(self):
        self.assertFalse((self.tree / "docs" / "craft" / "finance.md").exists())
        code, output = self.run_gate("--list")
        self.assertEqual(code, 0, output)
        self.assertIn("docs/craft/finance.md", output.splitlines())

    def test_the_ignored_and_untracked_list_cannot_carry_it(self):
        # Why the fix exists: a tracked file is in neither set the gate read
        # before — HQ's ignored files and the branch's HEAD.
        tracked = hq_gate.tracked_files(self.tree)
        self.assertNotIn("docs/craft/finance.md", hq_gate.hq_only_files(self.hq, tracked))

    def test_main_bytes_come_from_the_commit_not_the_working_tree(self):
        # HQ's checkout is a working copy; an uncommitted edit there is not what
        # the merge carries. The fixture module asserts the committed text.
        (self.hq / "docs" / "craft" / "finance.md").write_text("dirty\n", encoding="utf-8")
        code, output = self.run_gate()
        self.assertEqual(code, 0, output)

    def test_no_main_branch_overlays_nothing_from_main(self):
        # A clone whose default branch is not named main: no crash and no
        # invented history, just the HQ-only set.
        git("branch", "-m", "main", "trunk", cwd=self.hq)
        code, output = self.run_gate("--list")
        self.assertEqual(code, 0, output)
        self.assertNotIn("docs/craft/finance.md", output.splitlines())
        self.assertIn("docs/backlog.yaml", output.splitlines())


PREVIEW_MODULE = '''\
"""Fixture: what the merge takes from main, and what stays the branch's."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PreviewTest(unittest.TestCase):
    def test_a_document_main_gained_arrives(self):
        self.assertEqual((ROOT / "docs" / "gained.md").read_text().strip(), "GAINED")

    def test_a_document_main_repaired_arrives(self):
        self.assertEqual((ROOT / "docs" / "rule.md").read_text().strip(), "REPAIRED")

    def test_a_document_the_branch_changed_keeps_the_branch_bytes(self):
        self.assertEqual((ROOT / "docs" / "mine.md").read_text().strip(), "BRANCH")

    def test_a_document_the_branch_deleted_stays_deleted(self):
        self.assertFalse((ROOT / "docs" / "gone.md").exists())

    def test_a_document_the_branch_wrote_is_here(self):
        self.assertEqual((ROOT / "docs" / "branch.md").read_text().strip(), "GOOD")
'''


class MergePreviewCase(unittest.TestCase):
    """HQ and the card's branch share one git directory, as they do on this host.

    The branch is a linked worktree, so `merge-base` is answerable: this is the
    only fixture shape that can tell "main moved" from "the branch decided".
    """

    def setUp(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="hq-gate-merge-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.base = base
        self.hq = base / "hq"
        self.tree = base / "tree"
        self.make_hq()
        self.branch_it()
        self.main_moves()

    def make_hq(self) -> None:
        self.hq.mkdir(parents=True)
        git("init", "-q", "-b", "main", cwd=self.hq)
        git("config", "user.email", "fixture@example.com", cwd=self.hq)
        git("config", "user.name", "fixture", cwd=self.hq)
        (self.hq / "docs").mkdir()
        (self.hq / "docs" / "rule.md").write_text("GOOD\n", encoding="utf-8")
        (self.hq / "docs" / "mine.md").write_text("BASE\n", encoding="utf-8")
        (self.hq / "docs" / "gone.md").write_text("KEPT\n", encoding="utf-8")
        (self.hq / "docs" / "kept.md").write_text("BASE\n", encoding="utf-8")
        (self.hq / "docs" / "README.md").write_text("public\n", encoding="utf-8")
        git("add", "-A", cwd=self.hq)
        git("commit", "-q", "-m", "hq base", cwd=self.hq)
        (self.hq / ".git" / "info" / "exclude").write_text(
            "docs/backlog.yaml\n"
            "tests/test_hq_only.py\n",
            encoding="utf-8",
        )
        (self.hq / "docs" / "backlog.yaml").write_text("hq-only\n", encoding="utf-8")
        (self.hq / "tests").mkdir()
        (self.hq / "tests" / "test_hq_only.py").write_text(PREVIEW_MODULE, encoding="utf-8")

    def branch_it(self) -> None:
        """The card's commit: one document written, one changed, one deleted."""
        git("worktree", "add", "-q", "-b", "card", str(self.tree), cwd=self.hq)
        (self.tree / "docs" / "branch.md").write_text("GOOD\n", encoding="utf-8")
        (self.tree / "docs" / "mine.md").write_text("BRANCH\n", encoding="utf-8")
        (self.tree / "docs" / "kept.md").write_text("BRANCH\n", encoding="utf-8")
        (self.tree / "docs" / "gone.md").unlink()
        git("add", "-A", cwd=self.tree)
        git("commit", "-q", "-m", "the card", cwd=self.tree)

    def main_moves(self) -> None:
        """Main after the fork: a document gained, a repair, a competing edit."""
        (self.hq / "docs" / "gained.md").write_text("GAINED\n", encoding="utf-8")
        (self.hq / "docs" / "rule.md").write_text("REPAIRED\n", encoding="utf-8")
        (self.hq / "docs" / "mine.md").write_text("MAIN-EDITED\n", encoding="utf-8")
        git("add", "-A", cwd=self.hq)
        git("commit", "-q", "-m", "main moves", cwd=self.hq)

    def carried(self) -> list[str]:
        revision = hq_gate.integration_commit(self.hq)
        shared = hq_gate.merge_base(self.tree, revision)
        return hq_gate.carried_paths(
            hq_gate.tree_entries(self.hq, revision),
            hq_gate.tree_entries(self.tree, shared) if shared else {},
            hq_gate.tree_entries(self.tree, "HEAD"),
        )

    def run_gate(self, *args: str) -> tuple[int, str]:
        return quiet_main(["--hq", str(self.hq), "--tree", str(self.tree), *args])


class MergePreviewTest(MergePreviewCase):
    def test_main_side_is_added_and_untouched_paths_only(self):
        carried = self.carried()
        # Main gained it; the branch never had it.
        self.assertIn("docs/gained.md", carried)
        # The branch left it at the base's bytes and main repaired it.
        self.assertIn("docs/rule.md", carried)
        # Both sides changed it: the branch's bytes stay.
        self.assertNotIn("docs/mine.md", carried)
        # The branch deleted it: the merge deletes it.
        self.assertNotIn("docs/gone.md", carried)
        # Private machinery is not in HQ's HEAD; the HQ-only overlay owns it.
        self.assertNotIn("docs/backlog.yaml", carried)

    def test_the_gate_runs_the_merge_preview_not_the_stale_branch(self):
        # Without the carry, two of the five assertions red: main's gained
        # document and main's repair of a file the branch left alone.
        code, output = self.run_gate()
        self.assertEqual(code, 0, output)
        self.assertIn("hq-gate: OK", output)
        self.assertIn("Ran 5 tests", output)

    def test_the_committed_bytes_arrive_not_the_dirty_working_copy(self):
        (self.hq / "docs" / "gained.md").write_text("UNCOMMITTED\n", encoding="utf-8")
        code, output = self.run_gate()
        self.assertEqual(code, 0, output)

    def test_a_violation_in_the_branchs_own_commit_is_still_red(self):
        # The carry only fills paths the branch left alone; it cannot excuse a
        # document the branch itself wrote wrong.
        (self.tree / "docs" / "branch.md").write_text("BAD\n", encoding="utf-8")
        git("commit", "-q", "-am", "break the branch", cwd=self.tree)
        code, output = self.run_gate()
        self.assertEqual(code, 1, output)
        self.assertRegex(output, r"FAIL\s+test_hq_only")

    def test_the_default_hq_is_the_primary_checkout_of_a_linked_worktree(self):
        self.assertEqual(hq_gate.main_checkout(self.tree), self.hq.resolve())


DELETION_MODULE = '''\
"""Fixture: main deleted a document; the preview must delete it too."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DeletionTest(unittest.TestCase):
    def test_a_document_main_deleted_is_gone_from_the_preview(self):
        # The branch left it at the base bytes, so the merge deletes it.
        self.assertFalse((ROOT / "docs" / "README.md").exists())

    def test_a_document_the_branch_changed_survives_main_deleting_it(self):
        # A modify/delete conflict keeps the branch's bytes; card_acceptance
        # owns the conflict, the preview must not invent the deletion.
        self.assertEqual((ROOT / "docs" / "kept.md").read_text().strip(), "BRANCH")

    def test_main_repairs_still_arrive(self):
        self.assertEqual((ROOT / "docs" / "rule.md").read_text().strip(), "REPAIRED")

    def test_the_branch_document_is_here(self):
        self.assertEqual((ROOT / "docs" / "branch.md").read_text().strip(), "GOOD")
'''


class MainDeletesTest(MergePreviewCase):
    """`main` deletes documents after the cut — ODL-941's mirror image.

    A path `main` deleted and the branch left at the base bytes is neither
    carried nor overlaid, so the preview kept it: an HQ-only rule that requires
    it gone red on a tree nobody's merge produces. The preview has to remove it,
    and only it: the branch's own edits and deletions stay the branch's answer.
    """

    def setUp(self) -> None:
        super().setUp()
        (self.hq / "docs" / "README.md").unlink()
        (self.hq / "docs" / "kept.md").unlink()
        git("commit", "-q", "-am", "main deletes", cwd=self.hq)
        (self.hq / "tests" / "test_hq_only.py").write_text(DELETION_MODULE, encoding="utf-8")

    def deleted(self) -> list[str]:
        revision = hq_gate.integration_commit(self.hq)
        shared = hq_gate.merge_base(self.tree, revision)
        return hq_gate.deleted_paths(
            hq_gate.tree_entries(self.hq, revision),
            hq_gate.tree_entries(self.tree, shared) if shared else {},
            hq_gate.tree_entries(self.tree, "HEAD"),
        )

    def test_only_the_path_the_branch_left_alone_is_deleted(self):
        deleted = self.deleted()
        # Main deleted it; the branch never touched it.
        self.assertIn("docs/README.md", deleted)
        # Main deleted it; the branch changed it — the branch's bytes survive.
        self.assertNotIn("docs/kept.md", deleted)
        # The branch deleted it itself; no second removal is needed.
        self.assertNotIn("docs/gone.md", deleted)

    def test_the_preview_removes_what_main_deleted(self):
        code, output = self.run_gate()
        self.assertEqual(code, 0, output)
        self.assertIn("1 path(s) main deleted removed from the preview", output)
        self.assertIn("Ran 4 tests", output)

    def test_the_removal_is_what_turns_the_gate_green(self):
        # Pin the failure the fix removes: with the removal disabled, the
        # fixture's first assertion reds on the file the merge would delete.
        original = hq_gate.remove_paths
        hq_gate.remove_paths = lambda destination, files: []  # type: ignore[assignment]
        try:
            code, output = self.run_gate()
        finally:
            hq_gate.remove_paths = original  # type: ignore[assignment]
        self.assertEqual(code, 1, output)
        self.assertIn("a_document_main_deleted_is_gone_from_the_preview", output)

    def test_list_names_the_removal(self):
        code, output = self.run_gate("--list")
        self.assertEqual(code, 0, output)
        self.assertIn("-docs/README.md", output.splitlines())
        self.assertIn("1 deleted by main", output)


if __name__ == "__main__":
    unittest.main()
