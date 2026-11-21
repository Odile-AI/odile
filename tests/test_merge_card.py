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

The tool's own environment scrub (`caller_env`) is asserted by `CallerEnvTests`
with no wrapper between the caller and the tool: the suite's `clean_env()` used
to hide whether the tool scrubbed anything at all, so deleting
`env=caller_env()` from the tool left this suite green (t_ab1f19e3, 2026-11-20).

`tools/merge_card.py` is HQ-only machinery and this suite is dropped from the
public build with it (`tools/build_public_tree.py`), because a published file
must not name a tool a reader cannot see. The skip below still covers a
hand-built tree that carries `tests/` without the tool. `PublicTreeTests` is
the fence for both halves: the build's exclusion list must name the tool and
this suite, and no file the build publishes may mention the tool.
"""

from __future__ import annotations

import ast
import fnmatch
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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOL_MISSING = "merge_card.py is HQ-only machinery and is not in this tree"
REAL_GIT = shutil.which("git") or "/usr/bin/git"


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
    # The fixture repositories are hermetic: a system or global git config
    # (`commit.gpgsign`, `core.hooksPath`) must not change what a test measures.
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env.update(extra or {})
    return env


def shared_file(*parts: str) -> Path | None:
    """A file in this tree, or in the checkout a linked worktree shares."""
    direct = ROOT.joinpath(*parts)
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
    candidate = Path(probe.stdout.strip()).parent.joinpath(*parts)
    return candidate if candidate.is_file() else None


def merge_tool() -> Path | None:
    """The tool in this tree, or in the checkout a linked worktree shares."""
    return shared_file("tools", "merge_card.py")


TOOL = merge_tool()


def load_tool():
    """The module under test, loaded from `TOOL` so a mutant copy can stand in.

    A reviewer reproduces a red by writing a modified copy of the tool to
    `<worktree>/tools/merge_card.py` (`merge_tool` prefers that path) and
    re-running this module; nothing here reaches into the shipped file.
    """
    spec = importlib.util.spec_from_file_location("merge_card_under_test", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Fixture:
    """A throwaway repository, a stand-in `make`, and one branch to merge."""

    BRANCH = "probe/merge-gate"

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="merge-card-"))
        self.repo = self.tmp / "repo"
        self.bin_dir = self.tmp / "bin"
        self.record = self.tmp / "gate-env.txt"
        self.shim: Path | None = None
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

    def block_reset(self) -> None:
        """Put a `git` on PATH that refuses `reset`, the way a bare repository does.

        `git reset --hard` fails with "this operation must be run in a work tree"
        once `core.bare` is true; here the command is refused outright so the
        failure cannot be repaired away, which is what makes the claim testable.
        Everything else goes to the real git.
        """
        shim = self.tmp / "shim"
        shim.mkdir(exist_ok=True)
        wrapper = shim / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            'for arg in "$@"; do\n'
            '  if [ "$arg" = reset ]; then\n'
            "    echo 'fatal: this operation must be run in a work tree' >&2\n"
            "    exit 128\n"
            "  fi\n"
            "done\n"
            f'exec "{REAL_GIT}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        self.shim = shim

    def tool_env(self, *, shim: bool = False, extra: dict[str, str] | None = None) -> dict[str, str]:
        run_env = clean_env(extra)
        paths = [str(self.bin_dir)]
        if shim:
            paths.insert(0, str(self.shim))
        run_env["PATH"] = os.pathsep.join(paths + [run_env.get("PATH", "")])
        run_env["MERGE_TEST_RECORD"] = str(self.record)
        return run_env

    def tool_command(self) -> list[str]:
        return [sys.executable, str(TOOL), "--repo", str(self.repo),
                "--branch", self.BRANCH, "--allow-any"]

    def run_tool(self, *, make: str, env: dict[str, str] | None = None,
                 shim: bool = False) -> tuple[subprocess.CompletedProcess[str], float]:
        self.write_make(make)
        run_env = self.tool_env(shim=shim, extra=env)
        started = time.monotonic()
        proc = subprocess.run(
            self.tool_command(),
            cwd=str(self.tmp),
            env=run_env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return proc, time.monotonic() - started

    def start_tool(self, *, make: str, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
        """Run the tool without waiting, so a caller can signal it mid-gate."""
        self.write_make(make)
        return subprocess.Popen(
            self.tool_command(),
            cwd=str(self.tmp),
            env=self.tool_env(extra=env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def wait_for(self, path: Path, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.05)
        raise AssertionError(f"{path} never appeared; the stand-in gate did not start")

    def head_commit(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

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
class RollbackTests(unittest.TestCase):
    """A gate failure rolls `main` back, verifiably — or says it did not.

    Three measured ways the old `git reset --hard before` (line ~140, and the
    publish path below it, both `check=False`) told the caller what had not
    happened:

    - t_ce763ea9, 2026-11-15: a fixture inside the gate left `core.bare=true`;
      the reset failed, main stayed at the merge commit, and the log still said
      "main reset to 91aa597893e0" with no reflog entry.
    - t_69832025, 2026-11-15: another session checked out a new branch from
      main mid-gate, so the reset moved *that* branch and left main at the
      merge.
    - the publish path carried the same blind reset and the same claim.

    The tool now names `refs/heads/main` (not HEAD), repairs the bare flag,
    verifies what it moved, and prints the reset's stderr instead of a claim.
    """

    def setUp(self) -> None:
        self.fixture = Fixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_a_blocked_reset_still_rolls_main_back_and_does_not_claim_a_reset(self) -> None:
        """The DoD case: `git reset` fails; main must still be `before`, loudly."""
        self.fixture.block_reset()
        proc, _ = self.fixture.run_tool(make='echo "1 test failed"; exit 1\n', shim=True)

        self.assertIn(proc.returncode, (1, 6), proc.stdout + proc.stderr)
        self.assertEqual(self.fixture.head(), self.fixture.before,
                         "main was left at the merge although the tool reported a rollback")
        self.assertEqual(self.fixture.head_commit(), self.fixture.before,
                         "HEAD is not the pre-merge commit after the rollback")
        self.assertNotIn(f"main reset to {self.fixture.before[:12]}", proc.stdout,
                         "the tool claimed a reset that the shim refused")
        self.assertIn(f"main moved back to {self.fixture.before[:12]}", proc.stdout)
        self.assertIn("must be run in a work tree", proc.stdout,
                      "the reset's own error is not printed")
        self.assertIn("merge_card: main returned to", self.fixture.git("reflog", "show", "main").stdout,
                      "the rollback left no reflog entry a reviewer can see")

    def test_a_bare_flag_left_by_the_gate_is_repaired_before_the_reset(self) -> None:
        """The observed t_ce763ea9 shape: `core.bare=true` mid-gate, gate red."""
        proc, _ = self.fixture.run_tool(
            make='git config core.bare true\necho "1 test failed"; exit 1\n',
        )

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.fixture.head(), self.fixture.before,
                         "main stayed at the merge because core.bare was left true")
        self.assertEqual(self.fixture.head_commit(), self.fixture.before)
        self.assertIn("core.bare was true", proc.stdout)
        self.assertIn(f"main reset to {self.fixture.before[:12]}", proc.stdout)
        self.assertEqual(self.fixture.git("config", "--bool", "core.bare").stdout.strip(), "false",
                         "the bare flag a fixture inside the gate set was left behind")

    def test_a_branch_checked_out_mid_gate_keeps_its_head(self) -> None:
        """The t_69832025 shape: another session checks out a branch from main mid-gate."""
        proc, _ = self.fixture.run_tool(
            make='git checkout -q -b probe/other main\necho "1 test failed"; exit 1\n',
        )

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.fixture.head(), self.fixture.before,
                         "main was not rolled back")
        other = self.fixture.git("rev-list", "--parents", "-n", "1", "probe/other").stdout.split()
        self.assertEqual(len(other), 3,
                         "the branch another session checked out was moved by the rollback")
        self.assertEqual(self.fixture.head_commit(), other[0],
                         "the checked-out branch no longer holds its own head")
        self.assertIn("HEAD is on refs/heads/probe/other", proc.stdout)


@unittest.skipUnless(TOOL, TOOL_MISSING)
class InterruptedGateTests(unittest.TestCase):
    """A signal that stops the gate mid-run must not leave the merge on main.

    Measured by the verifier (t_6f345d59, 2026-11-15 11:22Z): the caller's
    process was cut by the host's foreground cap after `git merge` and before
    the gate printed, so an ungated red merge stayed on main with nobody told.
    """

    def setUp(self) -> None:
        self.fixture = Fixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_a_sigterm_during_the_gate_rolls_main_back(self) -> None:
        marker = self.fixture.tmp / "gate-started"
        proc = self.fixture.start_tool(make=f'touch "{marker}"\nsleep 30\nexit 0\n')
        try:
            self.fixture.wait_for(marker)
            time.sleep(0.2)  # the tool installs its handler right after spawning the gate
            proc.send_signal(signal.SIGTERM)
            out, _ = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, out)
        self.assertEqual(self.fixture.head(), self.fixture.before,
                         "SIGTERM left the ungated merge on main")
        self.assertEqual(self.fixture.head_commit(), self.fixture.before)
        self.assertIn("SIGTERM stopped the gate", out)
        self.assertIn(f"main reset to {self.fixture.before[:12]}", out)


@unittest.skipUnless(TOOL, TOOL_MISSING)
class PolicyTests(unittest.TestCase):
    def test_the_subprocess_outlives_the_gate_lock_wait(self) -> None:
        """The tool's defaults, not the knobs its caller exported.

        `merge_card` hands its own environment to the gate, so a suite that
        read these constants in-process measured the operator's overrides. An
        ODL-339 merge launched with `ODILE_MERGE_CHECK_LOCK_WAIT=3600
        ODILE_MERGE_CHECK_TIMEOUT=3600` failed here — `3600 not greater than
        3600` — and had its merge reset (2026-11-18, the ODL-910 gate run).
        Load the module with those variables removed.
        """
        with mock.patch.dict(os.environ, clean_env(), clear=True):
            module = load_tool()

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

    def _run_publish(self, env: dict[str, str]) -> int:
        """Run `merge_card --card` in-process, in exactly the environment given.

        The card has no branch, so the tool's work is `publish_main`: one
        `make leak` in the repository and one `git push origin main`. The
        stand-in `make` answers the leak gate, so what the exit code and the
        fixture's refs measure is the tool, not this suite. `card_details` is
        replaced so no board is read.
        """
        self.fixture.write_make('case "$1" in\n  leak) echo "make leak: OK" ;;\nesac\nexit 0\n')
        with mock.patch.dict(os.environ, env, clear=True):
            module = load_tool()
            setattr(module, "card_details", lambda card: {"branch_name": None,
                                                          "workspace_path": None,
                                                          "title": "a dir-workspace card"})
            return module.main(["--card", "t_000000", "--repo", str(self.fixture.repo)])

    def publish(self) -> int:
        """Publish with the suite's `clean_env()` standing in for the caller.

        This models a caller that is already clean — a shell outside a hook —
        and `clean_env()` strips every `GIT_*` before the tool sees it. That is
        why it cannot assert the tool's own scrub: it passed on the tool that
        had none (t_ab1f19e3). `publish_unwrapped()` is the version that can.
        """
        run_env = clean_env({
            "PATH": os.pathsep.join([str(self.fixture.bin_dir), os.environ.get("PATH", "")]),
        })
        return self._run_publish(run_env)

    def publish_unwrapped(self, caller: dict[str, str]) -> int:
        """Publish with the caller's own environment reaching the tool.

        Nothing here scrubs a repository-pinning name: the caller's `GIT_DIR`,
        `GIT_INDEX_FILE` and the rest go into `os.environ` and only the tool's
        `caller_env()` can keep them out of its `git` calls. The ambient `GIT_*`
        are dropped first so the caller pinned below is the only repository
        named — a gate's own hook variables would otherwise pin the *real*
        checkout, and a tool that failed to scrub would push it.

        The caller also pins the hermetic config pair (`/dev/null`, `1`), which
        the tool keeps: it is what makes this run independent of the host's
        `$HOME/.gitconfig`.
        """
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env["PATH"] = os.pathsep.join([str(self.fixture.bin_dir), env.get("PATH", "")])
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env.update(caller)
        return self._run_publish(env)

    def decoy_caller(self) -> tuple[Fixture, dict[str, str], str, str]:
        """A second repository pinned the way `.githooks/pre-commit` pins one.

        The decoy plays the caller's repository: an unpushed `main` and its own
        `origin`, so one push is all it takes to tell that the tool acted on it
        instead of on `--repo`. Returns the decoy, the caller's environment, the
        decoy's `main` and its `origin/main`; the caller must `decoy.cleanup()`.
        """
        decoy = Fixture()
        try:
            decoy.add_origin()
            decoy.git("commit", "-q", "--allow-empty", "-m", "decoy: an unpushed commit")
            main = decoy.head()
            origin = decoy.origin_main()
            caller = {
                "GIT_DIR": decoy.git("rev-parse", "--absolute-git-dir").stdout.strip(),
                "GIT_INDEX_FILE": decoy.git("rev-parse", "--path-format=absolute",
                                            "--git-path", "index").stdout.strip(),
            }
        except BaseException:
            decoy.cleanup()
            raise
        return decoy, caller, main, origin

    def test_a_card_without_a_branch_publishes_main(self) -> None:
        code = self.publish()

        self.assertEqual(code, 0)
        self.assertEqual(self.fixture.origin_main(), self.fixture.head(),
                         "the branch-less card's commit was not published")
        self.assertNotEqual(self.fixture.origin_main(), self.fixture.before,
                            "origin/main did not move: nothing was published")

    def test_a_caller_pinned_git_dir_is_scrubbed_by_the_tool_itself(self) -> None:
        """The tool, not this suite, must drop the caller's `GIT_DIR`.

        No `clean_env()` between the caller and the tool: the pin goes into
        `os.environ` and the tool has to scrub it. This is the mutant that
        reproduced the bug — with `env=caller_env()` deleted from the tool's
        `git()`/`publish()`, every `git -C <fixture>` acts on the decoy and the
        push publishes the decoy's `main` (measured 2026-11-20 on the pre-fix
        bytes, `caller_env` removed: this test red, the wrapped one green).
        """
        decoy, caller, decoy_main, decoy_origin = self.decoy_caller()
        try:
            self.assertNotEqual(decoy_main, decoy_origin, "the decoy has nothing to publish")
            code = self.publish_unwrapped(caller)

            self.assertEqual(code, 0)
            self.assertEqual(self.fixture.origin_main(), self.fixture.head(),
                             "the caller's GIT_DIR redirected the publish away from the fixture")
            self.assertEqual(decoy.head(), decoy_main, "the caller's repository's main moved")
            self.assertEqual(decoy.origin_main(), decoy_origin,
                             "the caller's repository's main was pushed to its origin")
        finally:
            decoy.cleanup()

    def test_a_caller_pinned_git_dir_does_not_redirect_the_publish(self) -> None:
        """The pre-commit hook's environment must not reach the tool's git calls.

        `.githooks/pre-commit` runs inside a linked worktree, where git exports
        an absolute `GIT_DIR`/`GIT_INDEX_FILE` for the *outer* repository.
        Inherited by the in-process tool, every `git -C <fixture>` acts on that
        repository instead: the fixture's `main` never moves and the push
        publishes the outer `main` to the real `origin` — measured on a card
        commit, 2026-11-20, where the hook's own suite failed on this test and
        the push fast-forwarded real merges.

        The decoy here plays the outer repository: a second repository with an
        unpushed `main` and its own `origin`, pinned in the caller's
        environment exactly as the hook pins it. Nothing about it may move.

        This test runs the tool inside the suite's `clean_env()`, so it asserts
        the hook-shaped path end to end but cannot tell whether the tool
        scrubbed anything itself (t_ab1f19e3);
        `test_a_caller_pinned_git_dir_is_scrubbed_by_the_tool_itself` is the
        one that reddens when the tool's own scrub is deleted.
        """
        decoy, caller, decoy_main, decoy_origin = self.decoy_caller()
        try:
            self.assertNotEqual(decoy_main, decoy_origin, "the decoy has nothing to publish")

            with mock.patch.dict(os.environ, caller):
                code = self.publish()

            self.assertEqual(code, 0)
            self.assertEqual(self.fixture.origin_main(), self.fixture.head(),
                             "the caller's GIT_DIR redirected the publish away from the fixture")
            self.assertEqual(decoy.head(), decoy_main, "the caller's repository's main moved")
            self.assertEqual(decoy.origin_main(), decoy_origin,
                             "the caller's repository's main was pushed to its origin")
        finally:
            decoy.cleanup()


@unittest.skipUnless(TOOL, TOOL_MISSING)
class CallerEnvTests(unittest.TestCase):
    """`caller_env()`, asserted with nothing between the caller and the tool.

    `NoBranchCardTests` runs the tool inside the suite's own `clean_env()`, so
    deleting the tool's `env=caller_env()` left every test green (t_ab1f19e3,
    2026-11-20). These two load the tool and drive it with the caller's own
    environment, so the tool's scrub is the only thing that can pass them.
    """

    #: Names git uses to pick a repository, a work tree or an object store. Each
    #: outranks `-C`, so a `git -C <repo>` that inherits one acts on the
    #: caller's repository instead.
    PINNED = {
        "GIT_DIR": "/var/tmp/decoy/.git",
        "GIT_INDEX_FILE": "/var/tmp/decoy/.git/index",
        "GIT_WORK_TREE": "/var/tmp/decoy",
        "GIT_PREFIX": "sub/dir/",
        "GIT_OBJECT_DIRECTORY": "/var/tmp/decoy/.git/objects",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/var/tmp/other/.git/objects",
        "GIT_COMMON_DIR": "/var/tmp/decoy/.git",
        "GIT_NAMESPACE": "probe",
        "GIT_CEILING_DIRECTORIES": "/var/tmp",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1",
        # A name this tool has never heard of must go too: the rule is the
        # namespace minus the config knobs, not a list that goes stale.
        "GIT_NAME_FROM_A_FUTURE_GIT": "1",
    }
    #: Configuration names, not repository names. Dropping them hands the tool
    #: the host's `$HOME/.gitconfig`; keeping them lets a caller stay hermetic.
    KEPT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def test_caller_env_drops_every_repository_pinning_name_and_keeps_the_config_knobs(self) -> None:
        """The `GIT_*` namespace goes; the two configuration knobs stay.

        A caller that exported `GIT_CONFIG_GLOBAL=/dev/null` and
        `GIT_CONFIG_NOSYSTEM=1` asked for a hermetic read. A tool that strips
        them reads `$HOME/.gitconfig` instead, where a host may carry
        `commit.gpgsign = true` — 12 failures measured at the t_8e5fb6fc
        review.
        """
        caller = dict(self.PINNED)
        caller.update(self.KEPT)
        caller.update({"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root",
                       "MERGE_TEST_CALLER": "kept"})

        with mock.patch.dict(os.environ, caller, clear=True):
            env = load_tool().caller_env()

        for name in self.PINNED:
            self.assertNotIn(name, env, f"caller_env() left {name}, which pins a repository")
        for name, value in self.KEPT.items():
            self.assertEqual(env.get(name), value, f"caller_env() dropped the config knob {name}")
        self.assertEqual(env.get("MERGE_TEST_CALLER"), "kept",
                         "caller_env() is not the caller's environment minus GIT_*")
        self.assertEqual(env.get("PATH"), caller["PATH"])
        self.assertEqual(env.get("HOME"), caller["HOME"])
        self.assertEqual(
            {name for name in env if name.startswith("GIT_")} - set(self.KEPT),
            set(),
            "a GIT_* the caller exported survived caller_env() without being a config knob",
        )

    def test_a_hermetic_config_keeps_a_hostile_home_out_of_the_tools_own_commit(self) -> None:
        """The kept knobs reach the tool's own `git`, measured by a commit.

        The hostile `$HOME/.gitconfig` signs every commit with `/bin/false`, so
        a tool that loses the knobs cannot write a commit object (`error: gpg
        failed to sign the data`) — the shape that reddened 12 tests at the
        t_8e5fb6fc review. Reaching `git()` here is the point: the knobs have to
        survive the tool's own scrub, not only a direct read of the helper.

        The environment is declared whole (`clear=True`) rather than layered
        over the gate's: a gate exports `GIT_DIR`/`GIT_INDEX_FILE`, and a tool
        under test that failed to scrub would otherwise land this probe commit
        on the caller's own branch — measured while writing this test, where the
        no-scrub mutant added a commit to the card's worktree.
        """
        fixture = Fixture()
        try:
            home = fixture.tmp / "hostile-home"
            home.mkdir()
            (home / ".gitconfig").write_text(
                "[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = /bin/false\n", encoding="utf-8")
            caller = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": str(home),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            }

            with mock.patch.dict(os.environ, caller, clear=True):
                proc = load_tool().git(fixture.repo, "commit", "--allow-empty", "-m",
                                       "probe: the tool's own commit", check=False)

            self.assertEqual(proc.returncode, 0,
                             "the tool inherited $HOME/.gitconfig: " + proc.stdout + proc.stderr)
            self.assertEqual(fixture.git("log", "-1", "--format=%s").stdout.strip(),
                             "probe: the tool's own commit",
                             "the tool's commit did not land in the fixture repository")
        finally:
            fixture.cleanup()


#: The private publish tool: it builds the public tree from an explicit INCLUDE
#: minus an EXCLUDE and is itself excluded, so it exists only at HQ (and in the
#: main worktree the merge gate runs from).
PUBLIC_TREE_BUILDER = ("tools", "build_public_tree.py")
BUILDER = shared_file(*PUBLIC_TREE_BUILDER)


def public_build_lists() -> tuple[list[str], list[str]] | None:
    """The builder's `INCLUDE` and `EXCLUDE`, read with `ast`, or `None`.

    Parsed rather than imported: the builder is a script, and a shipped test
    should not depend on its module surface (`tests/test_no_retired_demo_code.py`
    reads the exclusion list the same way). `None` where the builder is absent —
    a published tree — and the fence below then skips.
    """
    if BUILDER is None:
        return None
    try:
        tree = ast.parse(BUILDER.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    found: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in ("INCLUDE", "EXCLUDE"):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                continue
            if isinstance(value, list):
                found[target.id] = [str(item) for item in value]
    if set(found) != {"INCLUDE", "EXCLUDE"}:
        return None
    return found["INCLUDE"], found["EXCLUDE"]


def is_published(rel: str, include: list[str], exclude: list[str]) -> bool:
    """True when the build copies `rel`: under an INCLUDE root, not excluded.

    Mirrors the builder: `copy_tree` takes a directory's whole subtree, and
    `matches` is `fnmatch`, whose `*` crosses `/`, so a directory glob covers
    its subtree too.
    """
    covered = any(rel == root or rel.startswith(root.rstrip("/") + "/") for root in include)
    return covered and not any(fnmatch.fnmatch(rel, glob) for glob in exclude)


def index_files() -> list[str]:
    """Every path this branch's index carries — what a build from it copies."""
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        env=clean_env(),
    )
    if proc.returncode != 0:
        return []
    return [rel for rel in proc.stdout.split("\0") if rel]


@unittest.skipUnless(BUILDER, "the private public-tree builder is not in this tree")
class PublicTreeTests(unittest.TestCase):
    """The tool is HQ-only, and no file the build publishes may name it.

    Measured at `main` `97dd803` (2026-11-21): the build copied
    `tests/test_merge_card.py` — 18 skips in the built tree, which is the only
    thing a reader gets from it — and a comment in `tests/test_forbid_gate.py`
    named the tool, so the published tree carried a name for a file it does not
    carry. Both are fixed in the builder's exclusion list and in that comment;
    this fence reddens if a later card drops either entry or publishes another
    file that mentions the tool (t_d648048d).
    """

    def test_the_build_drops_the_tool_and_the_suite_that_tests_it(self) -> None:
        lists = public_build_lists()
        self.assertIsNotNone(lists, "the builder's INCLUDE/EXCLUDE could not be read")
        assert lists is not None
        include, exclude = lists
        for rel in ("tools/merge_card.py", "tests/test_merge_card.py"):
            with self.subTest(path=rel):
                self.assertIn(rel, exclude, f"{rel} is HQ-only but the public build copies it")
                self.assertFalse(is_published(rel, include, exclude),
                                 f"{rel} survives the public build's exclusion list")

    def test_no_published_file_names_the_tool(self) -> None:
        """A reader of the public tree must not meet a name it cannot open."""
        lists = public_build_lists()
        assert lists is not None
        include, exclude = lists
        named = []
        for rel in index_files():
            if not is_published(rel, include, exclude):
                continue
            try:
                if b"merge_card" in (ROOT / rel).read_bytes():
                    named.append(rel)
            except OSError:
                continue
        self.assertEqual(named, [],
                         "a published file names the HQ-only tool merge_card.py: "
                         "reword it, or add the file to the builder's EXCLUDE")


if __name__ == "__main__":
    unittest.main()
