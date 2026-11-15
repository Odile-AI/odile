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

    def test_the_base_pass_runs_before_the_tree_scanner(self):
        base = self.block.index("/tmp/base-leak_scan.py --tree")
        own = self.block.index("tools/leak_scan.py --tree", base)
        self.assertLess(base, own, "the tree's own scanner must not run first")

    def test_the_job_still_scans_with_the_tree_scanner(self):
        self.assertIn("run: /usr/bin/python3 tools/leak_scan.py --tree", self.block)


if __name__ == "__main__":
    unittest.main()
