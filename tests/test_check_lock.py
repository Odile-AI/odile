#!/usr/bin/python3
"""Two uids on one host must be able to share the gate lock.

`tools/check_lock.py` serialises `make check` across worktrees on one host.
The lock file lives in a world-writable directory and is owned by whichever uid
ran the gate first, so the next uid has to be able to take it as well. On
2026-11-15 the live file was `root:root 0644`, and a suite running as any other
uid died with an uncaught `PermissionError` before the gate started -- the
opposite of "host-wide".

These tests drive the helper as two real uids, in both orders (root holder with
a nobody waiter, and the reverse), and pin the two mechanisms that make it
work: a creating holder leaves the file open to every uid, and a legacy `0644`
file falls back to `O_RDONLY` because `flock(2)` does not need write access.
The timeout (rc 75) and re-entrant (`ODILE_CHECK_LOCK`) contracts stay pinned
here too, so a fix cannot buy a second uid the lock by deadlocking the gate.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "tools" / "check_lock.py"
PYTHON = "/usr/bin/python3"
NOBODY = 65534
WAIT_LINE = "waiting for another make check (pid "
TIMEOUT_EXIT = 75

# The holder's command: prove the lock is held (a marker the waiter cannot
# mistake for anything else), then keep it a moment so the waiter must queue.
HOLDER = ("import pathlib, sys, time\n"
          "pathlib.Path(sys.argv[1]).write_text('held')\n"
          "time.sleep(float(sys.argv[2]))\n")


def _can_switch_uids() -> bool:
    return os.geteuid() == 0 and shutil.which("setpriv") is not None


@unittest.skipUnless(_can_switch_uids(), "needs root and setpriv to run a second uid")
class TwoUidLockTest(unittest.TestCase):
    """Drive the real helper under root and nobody."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="odile-check-lock-", dir="/var/tmp"))
        # nobody has to create the lock file here, and traverse the copy.
        self.tmp.chmod(0o777)
        self.helper = self.tmp / "check_lock.py"
        shutil.copyfile(HELPER, self.helper)
        self.helper.chmod(0o755)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if key != "ODILE_CHECK_LOCK"}
        env["HOME"] = str(self.tmp)
        if extra:
            env.update(extra)
        return env

    def command(self, lock: Path, argv: list[str], *, uid: int | None = None,
                wait: int = 120) -> list[str]:
        cmd = [PYTHON, str(self.helper), "--lock", str(lock), "--wait", str(wait), "--"] + argv
        if uid is not None:
            cmd = [str(shutil.which("setpriv")),
                   f"--reuid={uid}", f"--regid={uid}", "--clear-groups"] + cmd
        return cmd

    def wait_for(self, path: Path, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while not path.exists():
            if time.monotonic() >= deadline:
                self.fail(f"the holder never wrote {path}")
            time.sleep(0.02)

    def take_pair(self, lock: Path, holder_uid: int | None, waiter_uid: int | None) -> None:
        """One uid holds the lock, the other queues and then runs."""
        marker = self.tmp / "held"
        holder = subprocess.Popen(
            self.command(lock, [PYTHON, "-c", HOLDER, str(marker), "5"], uid=holder_uid),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env())
        try:
            self.wait_for(marker)
            waiter = subprocess.run(
                self.command(lock, [PYTHON, "-c", "pass"], uid=waiter_uid),
                capture_output=True, text=True, timeout=120, env=self.env())
            holder_out, holder_err = holder.communicate(timeout=120)
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.communicate()
        self.assertEqual(holder.returncode, 0, holder_err)
        self.assertEqual(waiter.returncode, 0, waiter.stderr)
        self.assertIn(WAIT_LINE, waiter.stdout,
                      f"the waiter did not announce the queue: {waiter.stdout!r}")
        self.assertIn(f"(pid {holder.pid})", waiter.stdout,
                      f"the wait line does not name the holder: {waiter.stdout!r}")

    def test_a_root_holder_queues_a_nobody_waiter(self) -> None:
        self.take_pair(self.tmp / "lock", holder_uid=None, waiter_uid=NOBODY)

    def test_a_nobody_holder_queues_a_root_waiter(self) -> None:
        self.take_pair(self.tmp / "lock", holder_uid=NOBODY, waiter_uid=None)

    def test_a_legacy_root_owned_0644_lock_does_not_block_another_uid(self) -> None:
        """The defect exactly: a file root left behind at 0644."""
        lock = self.tmp / "lock"
        os.close(os.open(lock, os.O_RDWR | os.O_CREAT, 0o644))
        lock.chmod(0o644)  # defeat this process's umask
        info = lock.stat()
        self.assertEqual(info.st_uid, 0)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o644)

        result = subprocess.run(
            self.command(lock, [PYTHON, "-c", "pass"], uid=NOBODY),
            capture_output=True, text=True, timeout=120, env=self.env())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_creating_holder_opens_the_lock_to_every_uid(self) -> None:
        """A restrictive umask must not make the file uid-private."""
        lock = self.tmp / "lock"
        result = subprocess.run(
            self.command(lock, [PYTHON, "-c", "pass"]),
            capture_output=True, text=True, timeout=120, env=self.env(),
            preexec_fn=lambda: os.umask(0o077))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o666)


class LockContractTest(unittest.TestCase):
    """The queueing contracts a second uid must not break."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="odile-check-lock-", dir="/var/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lock = self.tmp / "lock"

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if key != "ODILE_CHECK_LOCK"}
        env["HOME"] = str(self.tmp)
        if extra:
            env.update(extra)
        return env

    def command(self, argv: list[str], *, wait: int) -> list[str]:
        return [PYTHON, str(HELPER), "--lock", str(self.lock),
                "--wait", str(wait), "--"] + argv

    def hold(self) -> int:
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o666)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        return fd

    def test_a_waiter_that_times_out_exits_75_without_running_the_command(self) -> None:
        self.hold()
        marker = self.tmp / "ran"
        result = subprocess.run(
            self.command([PYTHON, "-c",
                          "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('ran')",
                          str(marker)], wait=1),
            capture_output=True, text=True, timeout=120, env=self.env())
        self.assertEqual(result.returncode, TIMEOUT_EXIT, result.stderr)
        self.assertIn(WAIT_LINE, result.stdout)
        self.assertFalse(marker.exists(), "the command ran without the lock")

    def test_a_nested_invocation_trusts_the_enclosing_holder(self) -> None:
        self.hold()
        marker = self.tmp / "ran"
        result = subprocess.run(
            self.command([PYTHON, "-c",
                          "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('ran')",
                          str(marker)], wait=1),
            capture_output=True, text=True, timeout=120,
            env=self.env({"ODILE_CHECK_LOCK": str(self.lock)}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists(), "the nested run did not reach its command")
        self.assertNotIn(WAIT_LINE, result.stdout,
                         "the nested run queued on a lock it already holds")


class QueueInstrumentTest(unittest.TestCase):
    """The wait journal and the caller's pid — the two ODL-346 instruments."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="odile-check-queue-", dir="/var/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lock = self.tmp / "lock"
        self.journal = self.tmp / "journal.jsonl"

    def env(self) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if key != "ODILE_CHECK_LOCK"}
        env["HOME"] = str(self.tmp)
        return env

    def command(self, marker: Path, *, wait: int = 30,
                parent: int | None = None) -> list[str]:
        command = [PYTHON, str(HELPER), "--lock", str(self.lock),
                   "--wait", str(wait), "--event-log", str(self.journal)]
        if parent is not None:
            command += ["--parent-pid", str(parent)]
        return command + ["--", PYTHON, "-c",
                          "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('ran')",
                          str(marker)]

    def records(self) -> list[dict]:
        if not self.journal.exists():
            return []
        return [json.loads(line) for line in self.journal.read_text().splitlines() if line]

    def test_the_journal_records_the_exact_wait_and_the_holder(self) -> None:
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o666)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, b"424242\n")
        os.lseek(fd, 0, os.SEEK_SET)

        def release() -> None:
            try:
                os.close(fd)
            except OSError:
                pass  # the timer already released it

        self.addCleanup(release)
        timer = threading.Timer(1.2, release)
        timer.start()
        self.addCleanup(timer.cancel)
        marker = self.tmp / "ran"
        result = subprocess.run(self.command(marker), capture_output=True, text=True,
                                timeout=120, env=self.env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists())
        acquire = [row for row in self.records() if row["event"] == "acquire"]
        self.assertEqual(len(acquire), 1, self.records())
        self.assertEqual(acquire[0]["outcome"], "acquired")
        self.assertTrue(acquire[0]["queued"], "the run did not record that it queued")
        self.assertGreaterEqual(acquire[0]["waited_s"], 0.8,
                                "the recorded wait is shorter than the hold")
        self.assertEqual(acquire[0]["holder_pid"], 424242,
                         "the holder pid was not recorded")
        self.assertEqual(stat.S_IMODE(os.stat(self.journal).st_mode) & 0o666, 0o666,
                         "the journal is not appendable by the next uid")

    def test_a_waiter_leaves_the_queue_when_its_caller_dies(self) -> None:
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o666)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        parent = subprocess.Popen(["/bin/sleep", "30"])
        threading.Timer(0.6, parent.terminate).start()
        marker = self.tmp / "ran"
        started = time.monotonic()
        result = subprocess.run(self.command(marker, parent=parent.pid),
                                capture_output=True, text=True, timeout=30, env=self.env())
        parent.wait(timeout=10)
        self.assertEqual(result.returncode, TIMEOUT_EXIT, result.stderr)
        self.assertLess(time.monotonic() - started, 10, "the waiter did not notice")
        self.assertFalse(marker.exists(), "the command ran after its caller died")
        self.assertIn("is gone", result.stderr)
        acquire = [row for row in self.records() if row["event"] == "acquire"]
        self.assertEqual(acquire[-1]["outcome"], "caller-gone", self.records())

    def test_a_running_suite_is_stopped_when_its_caller_dies(self) -> None:
        parent = subprocess.Popen(["/bin/sleep", "30"])
        child_pid_file = self.tmp / "child.pid"
        command = [PYTHON, str(HELPER), "--lock", str(self.lock), "--wait", "10",
                   "--parent-pid", str(parent.pid), "--event-log", str(self.journal),
                   "--", "/bin/sh", "-c",
                   f"echo $$ > {child_pid_file}; exec sleep 30"]
        waiter = subprocess.Popen(command, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, env=self.env())
        deadline = time.monotonic() + 20
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(child_pid_file.exists(), "the suite never started")
        child_pid = int(child_pid_file.read_text().strip())
        parent.terminate()
        parent.wait(timeout=10)
        out, _ = waiter.communicate(timeout=20)
        self.assertEqual(waiter.returncode, TIMEOUT_EXIT, out)
        self.assertIn("stopped the suite", out)
        time.sleep(0.2)
        self.assertFalse(Path(f"/proc/{child_pid}").exists(),
                         "the suite outlived its caller")


if __name__ == "__main__":
    unittest.main()
