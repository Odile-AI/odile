"""`merge_card.py`: a merge that lands is not a merge that failed.

`make check` is one host-wide gate: it takes `/var/lock/odile-check.lock` and
queues for it, so on a busy board a merge waits behind several suites before its
own gate starts. On 2026-11-15 the tool's fixed 900 s subprocess cap fired while
the branch was already merged into `main`; the tool printed
`subprocess.TimeoutExpired` and exited 1, so a reader saw a failed merge where
`main` held a good one (`9f2b7b1`).

These tests drive the real script against a fixture repository with a stand-in
`make` on `PATH`, so what is asserted is what a caller observes: the exit code,
the printed line, and the state of `main`. The stand-in `make` reproduces the
measured shape of a gate that never got the host lock: `tools/check_lock.py`
answers 75, `make` wraps that into its own status 2 and echoes `Error 75`.

`tools/merge_card.py` is HQ-only machinery — it is excluded from the public
tree — so the suite skips where the tool is absent (a fresh clone).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL_MISSING = "merge_card.py is HQ-only machinery and is not in this tree"


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The caller's environment without the repo a hook may have pinned.

    `.githooks/pre-commit` exports `GIT_DIR`/`GIT_INDEX_FILE` for the outer
    repository; inherited by a fixture `git init` or by the tool itself they
    would act on the wrong repository. The tool's own knobs are cleared too, so
    a value exported in the shell cannot change what a test measures.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
        and not key.startswith("ODILE_MERGE_")
        and key != "CHECK_LOCK_WAIT"
    }
    env.update(extra or {})
    return env


def merge_tool() -> Path | None:
    """The tool in this tree, or in the checkout a linked worktree shares."""
    direct = ROOT / "tools" / "merge_card.py"
    if direct.is_file():
        return direct
    try:
        probe = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            env=clean_env(),
        )
    except OSError:
        return None
    if probe.returncode != 0:
        return None
    candidate = Path(probe.stdout.strip()).parent / "tools" / "merge_card.py"
    return candidate if candidate.is_file() else None


TOOL = merge_tool()


class Fixture:
    """A throwaway repository, a stand-in `make`, and one branch to merge."""

    BRANCH = "probe/merge-gate"

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="merge-card-"))
        self.repo = self.tmp / "repo"
        self.bin_dir = self.tmp / "bin"
        self.record = self.tmp / "gate-env.txt"
        self.bin_dir.mkdir()
        self.probe = self.tmp / "signal_probe.py"
        self.probe.write_text(
            "import signal\n"
            "print('ignored' if signal.getsignal(signal.SIGINT) is signal.SIG_IGN else 'default')\n",
            encoding="utf-8",
        )
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=self.tmp)
        self.git("config", "user.email", "merge-test@example.invalid")
        self.git("config", "user.name", "merge card test")
        (self.repo / "notes.txt").write_text("one\n", encoding="utf-8")
        self.git("add", "notes.txt")
        self.git("commit", "-q", "-m", "fixture: one line")
        self.before = self.head()
        self.git("checkout", "-q", "-b", self.BRANCH)
        self.git("commit", "-q", "--allow-empty", "-m", "probe: an empty commit")
        self.git("checkout", "-q", "main")

    def add_origin(self) -> Path:
        """A bare repository wired as `origin`, holding the fixture's `main`."""
        bare = self.tmp / "origin.git"
        self.git("init", "-q", "--bare", str(bare), cwd=self.tmp)
        self.git("remote", "add", "origin", str(bare))
        self.git("push", "-q", "-u", "origin", "main")
        return bare

    def origin_main(self) -> str:
        return self.git("rev-parse", "main", cwd=self.tmp / "origin.git").stdout.strip()

    def git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repo),
            capture_output=True,
            text=True,
            check=True,
            env=clean_env(),
        )

    def head(self) -> str:
        return self.git("rev-parse", "main").stdout.strip()

    def write_make(self, body: str) -> None:
        tool = self.bin_dir / "make"
        tool.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        tool.chmod(0o755)

    def run_tool(self, *, make: str, env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], float]:
        self.write_make(make)
        run_env = clean_env(env)
        run_env["PATH"] = f"{self.bin_dir}{os.pathsep}{run_env.get('PATH', '')}"
        run_env["MERGE_TEST_RECORD"] = str(self.record)
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(TOOL), "--repo", str(self.repo),
             "--branch", self.BRANCH, "--allow-any"],
            cwd=str(self.tmp),
            env=run_env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return proc, time.monotonic() - started

    def parents_of_main(self) -> list[str]:
        return self.git("rev-list", "--parents", "-n", "1", "main").stdout.split()

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


@unittest.skipUnless(TOOL, TOOL_MISSING)
class UnfinishedGateTests(unittest.TestCase):
    """A gate that does not finish leaves the merge in place and exits 4."""

    def setUp(self) -> None:
        self.fixture = Fixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def assert_merge_landed(self) -> str:
        parents = self.fixture.parents_of_main()
        self.assertEqual(len(parents), 3, "main is not a merge commit")
        merge_sha = parents[0]
        self.assertNotEqual(merge_sha, self.fixture.before, "main did not move")
        return merge_sha

    def test_a_gate_that_times_out_leaves_the_merge_on_main(self) -> None:
        proc, elapsed = self.fixture.run_tool(
            make="sleep 30\nexit 0\n",
            env={"ODILE_MERGE_CHECK_TIMEOUT": "1"},
        )

        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        merge_sha = self.assert_merge_landed()
        self.assertIn(merge_sha[:12], proc.stdout, "the printed line does not name the landed commit")
        self.assertIn("make check did not finish", proc.stdout)
        self.assertIn("run `make check` yourself", proc.stdout)
        self.assertLess(elapsed, 20, "the tool waited for the slow gate instead of stopping it")

    def test_a_gate_that_never_gets_the_lock_leaves_the_merge_on_main(self) -> None:
        # `tools/check_lock.py` answers 75 when another suite holds the lock
        # past CHECK_LOCK_WAIT, and `make` wraps that into its own status 2
        # after echoing `Error 75` — measured against the real lock. The
        # stand-in reproduces that shape, not a bare 75.
        proc, _ = self.fixture.run_tool(
            make=(
                'echo "make check: could not take /var/lock/odile-check.lock after 10s; '
                'another suite still holds it" >&2\n'
                'echo "make: *** [Makefile:56: check] Error 75" >&2\n'
                "exit 2\n"
            ),
        )

        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        merge_sha = self.assert_merge_landed()
        self.assertIn(merge_sha[:12], proc.stdout)
        self.assertIn("make check never started", proc.stdout)
        self.assertIn("run `make check` yourself", proc.stdout)


@unittest.skipUnless(TOOL, TOOL_MISSING)
class GateSemanticsTests(unittest.TestCase):
    """The normal paths keep today's behaviour: green merges, red resets."""

    def setUp(self) -> None:
        self.fixture = Fixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_a_green_gate_merges_and_bounds_the_childs_lock_wait(self) -> None:
        proc, _ = self.fixture.run_tool(
            make='echo "make check: OK"\nprintf "%s" "$CHECK_LOCK_WAIT" > "$MERGE_TEST_RECORD"\nexit 0\n',
        )

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        parents = self.fixture.parents_of_main()
        self.assertEqual(len(parents), 3, "main is not a merge commit")
        self.assertIn(parents[0][:12], proc.stdout)
        self.assertEqual(self.fixture.record.read_text(encoding="utf-8"), "900")

    def test_a_gate_that_fails_resets_main_to_the_pre_merge_commit(self) -> None:
        proc, _ = self.fixture.run_tool(make='echo "1 test failed"; exit 1\n')

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.fixture.head(), self.fixture.before)
        self.assertIn(f"main reset to {self.fixture.before[:12]}", proc.stdout)

    def test_a_failure_that_quotes_the_lock_line_still_resets_main(self) -> None:
        """A gate that ran and failed is exit 1 even when its output carries the lock line.

        `tests/test_check_lock.py` asserts check_lock's timeout exit code and
        prints `result.stderr` in the assertion message, so a drift in that
        contract makes *every* gate failure quote `could not take … another
        suite still holds it` verbatim. The suite still ran: it prints
        unittest's `Ran N tests` summary after the assertion. Reporting that as
        "never started" would leave `main` at a red merge.
        """
        proc, _ = self.fixture.run_tool(
            make=(
                'echo "======================================================================"\n'
                'echo "FAIL: test_a_waiter_that_times_out_exits_75 (tests.test_check_lock.LockContractTest)"\n'
                'echo "----------------------------------------------------------------------"\n'
                'echo "AssertionError: 76 != 75 : make check: could not take /var/tmp/odile-check-lock-hwi9jhlw/lock after 1s; another suite still holds it"\n'
                'echo "----------------------------------------------------------------------"\n'
                'echo "Ran 5 tests in 0.2s"\n'
                'echo "FAILED (failures=1)"\n'
                'echo "make: *** [Makefile:56: check] Error 1" >&2\n'
                "exit 2\n"
            ),
        )

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.fixture.head(), self.fixture.before,
                         "main was left at a merge whose gate ran and failed")
        self.assertIn(f"main reset to {self.fixture.before[:12]}", proc.stdout)
        self.assertIn("make check failed", proc.stdout)


@unittest.skipUnless(TOOL, TOOL_MISSING)
class PolicyTests(unittest.TestCase):
    def test_the_subprocess_outlives_the_gate_lock_wait(self) -> None:
        spec = importlib.util.spec_from_file_location("merge_card_under_test", TOOL)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertGreater(module.CHECK_TIMEOUT, module.CHECK_LOCK_WAIT)
        self.assertLess(module.CHECK_LOCK_WAIT, 3600, "the queue must be bounded below the Makefile default")


@unittest.skipUnless(TOOL, TOOL_MISSING)
class SignalDispositionTests(unittest.TestCase):
    """The gate child starts with a default SIGINT, whatever launched the tool.

    A launcher that starts the tool as an asynchronous job (`nohup ./tools/
    merge_card.py … &` in a non-interactive shell) leaves SIGINT ignored in
    every descendant. The suite stops its demo server with SIGINT, so under
    that disposition the server never stops and two tests fail `-9` — measured
    at the ODL-910 review (2026-11-18): five proof merges were launched that
    way and every one failed the full-suite gate. The tool must not depend on
    how its caller's shell launched it.
    """

    def setUp(self) -> None:
        self.fixture = Fixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_the_gate_child_gets_a_default_sigint_when_its_launcher_ignores_it(self) -> None:
        make = f'"{sys.executable}" "{self.fixture.probe}" > "$MERGE_TEST_RECORD"\nexit 0\n'
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            proc, _ = self.fixture.run_tool(make=make)
        finally:
            signal.signal(signal.SIGINT, previous)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            self.fixture.record.read_text(encoding="utf-8").strip(),
            "default",
            "the gate inherited an ignored SIGINT; the suite's stop paths would never stop",
        )


@unittest.skipUnless(TOOL, TOOL_MISSING)
class PublicationTests(unittest.TestCase):
    """A merge is published after a green gate, and only then."""

    def setUp(self) -> None:
        self.fixture = Fixture()
        self.fixture.add_origin()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_a_green_merge_is_pushed_to_origin(self) -> None:
        proc, _ = self.fixture.run_tool(
            make=(
                'case "$1" in\n'
                '  check) echo "make check: OK" ;;\n'
                '  leak) echo "make leak: OK" ;;\n'
                "esac\n"
                "exit 0\n"
            ),
        )

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.fixture.origin_main(), self.fixture.head(),
                         "the merge landed on main but origin/main did not move")
        self.assertNotEqual(self.fixture.origin_main(), self.fixture.before)

    def test_a_failing_leak_scan_resets_main_and_leaves_origin_alone(self) -> None:
        proc, _ = self.fixture.run_tool(
            make=(
                'case "$1" in\n'
                '  check) echo "make check: OK" ;;\n'
                '  leak) echo "make leak: FAIL"; exit 1 ;;\n'
                "esac\n"
                "exit 0\n"
            ),
        )

        self.assertEqual(proc.returncode, 5, proc.stdout + proc.stderr)
        self.assertEqual(self.fixture.head(), self.fixture.before,
                         "main was left at a merge that was not published")
        self.assertEqual(self.fixture.origin_main(), self.fixture.before,
                         "origin moved even though the publication gate failed")
        self.assertIn("make leak failed", proc.stdout)


@unittest.skipUnless(TOOL, TOOL_MISSING)
class NoBranchCardTests(unittest.TestCase):
    """A `dir`-workspace card commits on main; its merge is its publish.

    Cards that run in a shared directory have no branch, so `merge_card --card`
    had nothing to merge and printed "has no branch" while `main` stayed ahead
    of `origin/main` (t_0284f0cf, 2026-11-18 09:55Z — the review measured 277
    local commits against 276 on origin). The same tool now publishes the
    direct commit instead of returning silently.
    """

    def setUp(self) -> None:
        self.fixture = Fixture()
        self.fixture.add_origin()
        self.fixture.git("commit", "-q", "--allow-empty", "-m", "card: a direct commit")

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def load_tool(self):
        spec = importlib.util.spec_from_file_location("merge_card_under_test", TOOL)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_card_without_a_branch_publishes_main(self) -> None:
        module = self.load_tool()
        setattr(module, "card_details", lambda card: {"branch_name": None, "workspace_path": None,
                                                      "title": "a dir-workspace card"})
        previous = os.environ.copy()
        self.fixture.write_make('case "$1" in\n  leak) echo "make leak: OK" ;;\nesac\nexit 0\n')
        os.environ["PATH"] = f"{self.fixture.bin_dir}{os.pathsep}{previous['PATH']}"
        try:
            code = module.main(["--card", "t_000000", "--repo", str(self.fixture.repo)])
        finally:
            os.environ.clear()
            os.environ.update(previous)

        self.assertEqual(code, 0)
        self.assertEqual(self.fixture.origin_main(), self.fixture.head(),
                         "the branch-less card's commit was not published")


if __name__ == "__main__":
    unittest.main()
