#!/usr/bin/python3
"""The local gate must run the publication gate.

`make check-fast` is the only gate every commit passes — `.githooks/pre-commit`
runs it — so the publication scan has to be one of its prerequisites, not a CI-only
job. On 2026-11-15 `main` was red on `tools/leak_scan.py --tree` for a day
while every local gate was green, because `check` did not include the scan and
only the `publish-lint` job would have seen the next push.

These tests pin the dependency (`check` → `leak`), the recipe (the scanner in
tree mode) and the order (the scan fails before the suite runs), so deleting the
target or dropping it from `check` turns the suite red instead of silently
reopening the hole. `check` now takes the host-wide gate lock and hands the
actual prerequisites to `check-inner`, so the tests follow that hand-off and
also pin the lock on the heavy targets.

The handoff gate's two tools are private machinery at HQ (`.git/info/exclude`),
so a card worktree carries neither; `make acceptance` must resolve them from the
checkout the worktree shares, or the recipe dies on a missing relative path.
Those tests run `make -n` in a throwaway repository and its linked worktree, so
they measure the plan, not the Makefile's text.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"
LOCK_HELPER = "tools/check_lock.py"
LOCK_VAR = "CHECK_LOCK_PY"

# The two tools `make acceptance` runs that HQ keeps out of the repository.
HQ_TOOLS = ("tools/board_sync.py", "tools/card_acceptance.py")


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


def gate_prerequisites() -> list[str]:
    """The gate's prerequisites, reached through the lock wrapper.

    `make check` is one locked recipe that runs `check-inner`; the scanner must
    stay a prerequisite of whichever target actually runs the steps, and the
    wrapper must name the lock helper, or a second suite stacks on this host.
    """
    wrapper = "\n".join(rule("check"))
    assert LOCK_VAR in wrapper, "make check does not take the host gate lock"
    assert "check-inner" in wrapper, "make check does not run the locked gate"
    assert f"{LOCK_VAR} := $(PYTHON) {LOCK_HELPER}" in MAKEFILE.read_text(encoding="utf-8"), \
        f"{LOCK_VAR} does not name {LOCK_HELPER}"
    return rule("check-inner")[0].split(":", 1)[1].split()


class CheckIncludesThePublicationGate(unittest.TestCase):
    def test_check_requires_leak(self):
        prerequisites = gate_prerequisites()
        self.assertIn("leak", prerequisites, "make check does not run the publication gate")
        for target in ("test-unit", "self-test"):
            self.assertIn(LOCK_VAR, "\n".join(rule(target)),
                          f"make {target} does not take the host gate lock")

    def test_leak_runs_before_the_test_suite(self):
        prerequisites = gate_prerequisites()
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


def clean_env() -> dict[str, str]:
    """The environment a fixture `make` runs in: no inherited git pointing at HQ."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["LC_ALL"] = "C"
    return env


class AcceptanceResolvesThePrivateTools(unittest.TestCase):
    """`make acceptance` plans both tools from wherever the tree sits.

    A card worktree is a *linked* checkout: git checks out the commit's files
    only, and the two tools HQ excludes never enter a commit, so the recipe's
    relative path names a file the worktree does not have. The fixture mirrors
    that shape — a primary checkout holding the tools untracked, and a worktree
    of its commit without them — and reads what `make -n` plans.
    """

    def setUp(self) -> None:
        self.make = shutil.which("make") or ""
        self.git_bin = shutil.which("git") or ""
        if not self.make or not self.git_bin:
            self.skipTest("make or git is not installed")
        self.root = Path(tempfile.mkdtemp(prefix="odl-acceptance-recipe-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copyfile(MAKEFILE, self.root / "Makefile")
        for name in HQ_TOOLS:
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text("# stand-in for the HQ-only tool\n",
                                           encoding="utf-8")
        self.git("init", "--quiet", "-b", "main")
        self.git("add", "Makefile")  # the tools stay untracked, as at HQ
        self.git("-c", "user.name=gate", "-c", "user.email=gate@example.com",
                 "commit", "--quiet", "-m", "fixture")
        self.card = self.root / "card"
        self.git("worktree", "add", "--quiet", str(self.card), "-b", "probe", "main")

    def git(self, *args: str) -> str:
        result = subprocess.run([self.git_bin, *args], cwd=self.root,
                                env=clean_env(), capture_output=True, text=True,
                                timeout=120)
        self.assertEqual(result.returncode, 0,
                         f"git {' '.join(args)}: {result.stderr}")
        return result.stdout

    def plan(self) -> tuple[str, Path]:
        """The acceptance plan run from the card worktree, and that worktree."""
        result = subprocess.run([self.make, "--no-print-directory", "-n",
                                 "acceptance", "CARD=probe"],
                                cwd=self.card, env=clean_env(),
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout, self.card

    def test_the_card_worktree_plans_the_shared_checkouts_tools(self) -> None:
        plan, card = self.plan()
        for name in HQ_TOOLS:
            self.assertIn(str(self.root / name), plan,
                          f"the plan does not name the shared checkout's {name}")
            self.assertNotIn(str(card / name), plan,
                             f"the plan names a {name} the worktree does not carry")
            self.assertTrue((self.root / name).is_file())

    def test_the_primary_checkout_plans_its_own_copy(self) -> None:
        plan = subprocess.run(
            [self.make, "--no-print-directory", "-n", "acceptance", "CARD=probe"],
            cwd=self.root, env=clean_env(), capture_output=True, text=True,
            timeout=120)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        for name in HQ_TOOLS:
            self.assertIn(f'"{name}"', plan.stdout,
                          f"the checkout with a {name} must plan the relative one")

    def test_the_card_worktree_prefers_its_own_copy_when_it_has_one(self) -> None:
        own = self.card / HQ_TOOLS[0]
        own.parent.mkdir(parents=True, exist_ok=True)
        own.write_text("# a reviewer's mutant copy\n", encoding="utf-8")
        plan, card = self.plan()
        self.assertIn(f'"{HQ_TOOLS[0]}"', plan,
                      "a copy in the worktree must win over the shared checkout's")
        self.assertNotIn(str(card / HQ_TOOLS[0]), plan)


if __name__ == "__main__":
    unittest.main()
