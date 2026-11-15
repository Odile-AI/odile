#!/usr/bin/python3
"""The local gate must run the publication gate.

`make check` is the only gate every commit passes — `.githooks/pre-commit` runs
it — so the publication scan has to be one of its prerequisites, not a CI-only
job. On 2026-11-15 `main` was red on `tools/leak_scan.py --tree` for a day
while every local gate was green, because `check` did not include the scan and
only the `publish-lint` job would have seen the next push.

These tests pin the dependency (`check` → `leak`), the recipe (the scanner in
tree mode) and the order (the scan fails before the suite runs), so deleting the
target or dropping it from `check` turns the suite red instead of silently
reopening the hole.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"


def rule(name: str) -> list[str]:
    """The rule's own lines and its recipe, with a first line kept whole."""
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith(name + ":"):
            block = [line]
            for following in lines[index + 1:]:
                if following and not following[0].isspace():
                    break
                block.append(following)
            return block
    raise AssertionError(f"the Makefile has no {name!r} rule")


class CheckIncludesThePublicationGate(unittest.TestCase):
    def test_check_requires_leak(self):
        prerequisites = rule("check")[0].split(":", 1)[1].split()
        self.assertIn("leak", prerequisites, "make check does not run the publication gate")

    def test_leak_runs_before_the_test_suite(self):
        prerequisites = rule("check")[0].split(":", 1)[1].split()
        self.assertLess(prerequisites.index("leak"), prerequisites.index("test"),
                        "the scan must fail fast, before the suite runs")

    def test_leak_target_runs_the_scanner_on_the_tree(self):
        recipe = "\n".join(rule("leak"))
        self.assertIn("tools/leak_scan.py", recipe)
        self.assertIn("--tree", recipe)

    def test_make_dry_run_prints_the_scan(self):
        make = shutil.which("make")
        if make is None:
            self.skipTest("make is not installed")
        result = subprocess.run([make, "-n", "leak"], cwd=ROOT,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"tools/leak_scan\.py --tree")


if __name__ == "__main__":
    unittest.main()
