#!/usr/bin/python3
"""The forbid class must run, and it must fail closed.

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
a silent skip), and a malformed list row is a finding.

Every test runs the real scanner against a throwaway git repository, so the
`git ls-files` walk and the exit codes are the ones the gate uses.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNER = ROOT / "tools" / "leak_scan.py"
LABEL = "probe-box"
LITERAL = "deadbeefcafe"


class ForbidClass(unittest.TestCase):
    """A git repo with one clean file; each test adds the list and the probe."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "leak-allow.tsv").write_text(
            "# no exceptions in this fixture\n", encoding="utf-8")
        (self.repo / "keep.txt").write_text("nothing private here\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def scan(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCANNER), "--tree", *args],
            cwd=self.repo, capture_output=True, text=True, timeout=120)

    def commit(self, message: str = "fixture") -> None:
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=probe@example.test", "-c", "user.name=probe",
             "commit", "-qm", message],
            cwd=self.repo, check=True)

    def write_list(self) -> None:
        (self.repo / "leak-forbid.txt").write_text(
            f"# label<TAB>regex\n{LABEL}\t{LITERAL}\n", encoding="utf-8")

    def test_a_named_list_that_does_not_exist_exits_2(self) -> None:
        result = self.scan("--forbid-file", "missing-forbid.txt")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("FINDING allow-file", result.stdout)
        self.assertIn("missing-forbid.txt", result.stdout)

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

    def test_the_list_itself_is_not_a_finding(self) -> None:
        # The list carries the literals by definition; if the walk scanned it,
        # every label would be its own finding and the gate could never pass.
        self.write_list()
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"forbid list:     leak-forbid.txt (1 labels)", result.stdout)

    def test_an_absent_default_is_printed_not_silent(self) -> None:
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("absent (leak-forbid.txt)", result.stdout)
        self.assertIn("forbidden-value class did not run", result.stdout)

    def test_a_malformed_list_row_is_a_finding(self) -> None:
        (self.repo / "leak-forbid.txt").write_text(
            f"# label<TAB>regex\n{LABEL} no-tab-here\n", encoding="utf-8")
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
            cwd=self.repo, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 1, result.stdout)
        findings = [line for line in result.stdout.splitlines()
                    if line.startswith("FINDING forbidden-value")]
        self.assertEqual(len(findings), 1, result.stdout)
        self.assertIn("probe.txt", findings[0])


if __name__ == "__main__":
    unittest.main()
