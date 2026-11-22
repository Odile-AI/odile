#!/usr/bin/python3
"""The gate's file lists follow the index, not a walk of the working tree.

`make check` runs where the merge gate runs — the shared HQ checkout — and that
checkout carries an untracked private operating record under `docs/ops/`. While
the lists came from `find`, one shellcheck warning in a private evidence script
failed `lint` for every merge (ODL-338). The lists now come from `git ls-files`,
plus the hooks, which are shell but have no `.sh` suffix. A tree with no git —
the built public tree — keeps the directory walk, so it still lints every script
it ships.

Each test builds a throwaway repository and asserts what `make -n` plans, so the
suite pins the scope the gate applies rather than the Makefile's text.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GIT = shutil.which("git") or ""
MAKE = shutil.which("make") or ""
SHELLCHECK = shutil.which("shellcheck")

TRACKED_SH = "#!/bin/sh\nset -eu\nprintf '%s\\n' tracked\n"
HOOK_SH = "#!/bin/sh\nexit 0\n"
TRACKED_PY = "VALUE = 1\n"
PLANTED_SH = "#!/bin/sh\nunused=1\n"  # SC2034: assigned, never used.
PLANTED_PY = "OTHER = 2\n"


def scrub_hook_environment() -> dict[str, str]:
    """The environment without the GIT_* variables a pre-commit hook exports.

    `make check-fast` runs from `.githooks/pre-commit`, where git sets GIT_DIR and
    GIT_INDEX_FILE; a fixture repository that inherits them commits to the outer
    branch instead of its own.
    """
    return {key: value for key, value in os.environ.items()
            if not key.startswith("GIT_")}


def planned(output: str, command: str) -> list[str] | None:
    """The file names the dry run's last line starting with `command` carries."""
    words = command.split()
    for line in reversed(output.splitlines()):
        tokens = line.split()
        if tokens[:len(words)] == words:
            return tokens[len(words):]
    return None


class Fixture(unittest.TestCase):
    """A throwaway repository: tracked files, a hook, and planted warnings."""

    def setUp(self) -> None:
        if not GIT or not MAKE:
            self.skipTest("make or git is not installed")
        self.root = Path(tempfile.mkdtemp(prefix="odl-338-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "home").mkdir()
        shutil.copyfile(ROOT / "Makefile", self.root / "Makefile")
        self.write("tracked.sh", TRACKED_SH)
        self.write(".githooks/pre-commit", HOOK_SH)
        self.write("mod.py", TRACKED_PY)
        self.write("planted.sh", PLANTED_SH)
        self.write("planted.py", PLANTED_PY)
        self.git("init", "--quiet", "-b", "main")
        self.git("add", "tracked.sh", ".githooks/pre-commit", "mod.py")
        self.git("-c", "user.name=gate", "-c", "user.email=gate@example.com",
                 "commit", "--quiet", "-m", "fixture")

    def write(self, name: str, text: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def env(self) -> dict[str, str]:
        env = scrub_hook_environment()
        env["HOME"] = str(self.root / "home")
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["LC_ALL"] = "C"
        return env

    def git(self, *args: str) -> str:
        result = subprocess.run([GIT, *args], cwd=self.root, env=self.env(),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         f"git {' '.join(args)}: {result.stderr}")
        return result.stdout

    def make(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([MAKE, "--no-print-directory", *args], cwd=self.root,
                              env=self.env(), capture_output=True, text=True,
                              timeout=120)

    def planned_lint(self) -> list[str]:
        dry_run = self.make("-n", "lint")
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        names = planned(dry_run.stdout, "shellcheck")
        self.assertIsNotNone(names, dry_run.stdout)
        return names

    def tracked(self, pattern: str) -> list[str]:
        return self.git("ls-files", "--", pattern).split()


class GateScopeTest(Fixture):
    def test_lint_plan_is_the_index_plus_the_hook(self) -> None:
        names = self.planned_lint()
        self.assertEqual(sorted(names),
                         sorted(self.tracked("*.sh") + [".githooks/pre-commit"]))
        self.assertNotIn("planted.sh", names)

    def test_compile_plan_is_the_index(self) -> None:
        dry_run = self.make("-n", "compile")
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        names = planned(dry_run.stdout, "/usr/bin/python3 -m compileall -q")
        self.assertIsNotNone(names, dry_run.stdout)
        self.assertEqual(sorted(names), sorted(self.tracked("*.py")))

    @unittest.skipUnless(SHELLCHECK, "shellcheck is not installed")
    def test_an_untracked_warning_passes_until_the_copy_is_tracked(self) -> None:
        untracked = self.make("lint")
        self.assertEqual(untracked.returncode, 0,
                         untracked.stdout + untracked.stderr)
        self.git("add", "planted.sh")
        tracked = self.make("lint")
        self.assertNotEqual(tracked.returncode, 0,
                            "the tracked copy must fail the linter")
        self.assertIn("SC2034", tracked.stdout + tracked.stderr)

    def test_a_tree_with_no_git_still_walks_every_script(self) -> None:
        plain = Path(tempfile.mkdtemp(prefix="odl-338-plain-"))
        self.addCleanup(shutil.rmtree, plain, True)
        shutil.copyfile(ROOT / "Makefile", plain / "Makefile")
        (plain / "a.sh").write_text(TRACKED_SH, encoding="utf-8")
        dry_run = subprocess.run([MAKE, "--no-print-directory", "-n", "lint",
                                  "GIT=false"], cwd=plain,
                                 env=scrub_hook_environment(),
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertEqual(planned(dry_run.stdout, "shellcheck"), ["./a.sh"])


class ThisCheckoutTest(unittest.TestCase):
    """The checkout the suite runs in obeys the same rule."""

    def setUp(self) -> None:
        if not GIT or not MAKE:
            self.skipTest("make or git is not installed")
        env = scrub_hook_environment()
        inside = subprocess.run([GIT, "rev-parse", "--is-inside-work-tree"],
                                cwd=ROOT, env=env, capture_output=True,
                                text=True, timeout=60)
        if inside.stdout.strip() != "true":
            self.skipTest("this tree is not a git checkout")
        self.env = env

    def plan(self, target: str, command: str) -> list[str]:
        dry_run = subprocess.run([MAKE, "--no-print-directory", "-n", target],
                                 cwd=ROOT, env=self.env, capture_output=True,
                                 text=True, timeout=120)
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        names = planned(dry_run.stdout, command)
        self.assertIsNotNone(names, dry_run.stdout)
        return names

    def tracked(self, pattern: str) -> list[str]:
        result = subprocess.run([GIT, "ls-files", "--", pattern], cwd=ROOT,
                                env=self.env, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def test_the_checkouts_lint_plan_is_its_index(self) -> None:
        self.assertEqual(sorted(self.plan("lint", "shellcheck")),
                         sorted(self.tracked("*.sh")
                                + [".githooks/commit-msg", ".githooks/pre-commit",
                                   ".githooks/pre-push"]))

    def test_the_checkouts_compile_plan_is_its_index(self) -> None:
        self.assertEqual(sorted(self.plan("compile",
                                          "/usr/bin/python3 -m compileall -q")),
                         sorted(self.tracked("*.py")))


if __name__ == "__main__":
    unittest.main()
