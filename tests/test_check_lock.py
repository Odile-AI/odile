#!/usr/bin/python3
"""Two uids on one host must be able to share the gate lock, in ticket order.

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
The timeout is now the *queue cap* (rc 69) and caller-gone stays 75; the
re-entrant (`ODILE_CHECK_LOCK`) contract stays pinned too, so a fix cannot buy
a second uid the lock by deadlocking the gate.

The ticket queue is pinned in `TicketQueueTest`: waiters run in ascending
ticket order, a dead waiter's ticket does not block the next caller, the cap
names the holder and its elapsed hold, and a wrapper killed by SIGKILL keeps
the lock until the suite it started exits.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
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
CAP_EXIT = 69
CALLER_GONE_EXIT = 75

# The holder's command: prove the lock is held (a marker the waiter cannot
# mistake for anything else), then keep it a moment so the waiter must queue.
HOLDER = ("import pathlib, sys, time\n"
          "pathlib.Path(sys.argv[1]).write_text('held')\n"
          "time.sleep(float(sys.argv[2]))\n")

# A suite that leaves a timestamped line in a shared file while it holds the
# lock, sleeps, then leaves another. The line order is the acquisition order,
# and no two ranges may overlap if mutual exclusion holds.
RECORDER = (
    "import pathlib, sys, time\n"
    "path = pathlib.Path(sys.argv[1]); label = sys.argv[2]\n"
    "with path.open('a') as handle:\n"
    "    handle.write(f'{label} start {time.monotonic():.3f}\\n'); handle.flush()\n"
    "time.sleep(0.2)\n"
    "with path.open('a') as handle:\n"
    "    handle.write(f'{label} end {time.monotonic():.3f}\\n'); handle.flush()\n"
)


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
               if key not in ("ODILE_CHECK_LOCK", "ODILE_GATE_EVENT_LOG", "ODILE_GATE_PARENT_PID")}
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
               if key not in ("ODILE_CHECK_LOCK", "ODILE_GATE_EVENT_LOG", "ODILE_GATE_PARENT_PID")}
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

    def test_a_waiter_that_times_out_exits_cap_without_running_the_command(self) -> None:
        self.hold()
        marker = self.tmp / "ran"
        result = subprocess.run(
            self.command([PYTHON, "-c",
                          "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('ran')",
                          str(marker)], wait=1),
            capture_output=True, text=True, timeout=120, env=self.env())
        self.assertEqual(result.returncode, CAP_EXIT, result.stderr)
        self.assertIn(WAIT_LINE, result.stdout)
        self.assertIn("gave up ticket", result.stderr)
        self.assertIn("the suite never started", result.stderr)
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
               if key not in ("ODILE_CHECK_LOCK", "ODILE_GATE_EVENT_LOG", "ODILE_GATE_PARENT_PID")}
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
        rows = self.records()
        tickets = [row for row in rows if row["event"] == "ticket"]
        self.assertEqual(len(tickets), 1, rows)
        self.assertEqual(tickets[0]["ticket"], 1, "the first waiter did not take ticket 1")
        acquire = [row for row in rows if row["event"] == "acquire"]
        self.assertEqual(len(acquire), 1, rows)
        self.assertEqual(acquire[0]["outcome"], "acquired")
        self.assertTrue(acquire[0]["queued"], "the run did not record that it queued")
        self.assertGreaterEqual(acquire[0]["waited_s"], 0.8,
                                "the recorded wait is shorter than the hold")
        self.assertEqual(acquire[0]["holder_pid"], 424242,
                         "the holder pid was not recorded")
        self.assertEqual(acquire[0]["ticket"], 1)
        self.assertEqual(acquire[0]["ahead"], 0)
        self.assertIsNone(acquire[0]["holder_s"],
                          "a pid-only holder must not invent an elapsed time")
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
        self.assertEqual(result.returncode, CALLER_GONE_EXIT, result.stderr)
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
        self.assertEqual(waiter.returncode, CALLER_GONE_EXIT, out)
        self.assertIn("stopped the suite", out)
        time.sleep(0.2)
        self.assertFalse(Path(f"/proc/{child_pid}").exists(),
                         "the suite outlived its caller")


class TicketQueueTest(unittest.TestCase):
    """The FIFO queue, the cap, and the lock's lifetime across a SIGKILL."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="odile-check-ticket-", dir="/var/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lock = self.tmp / "lock"
        self.journal = self.tmp / "journal.jsonl"
        self.order = self.tmp / "order.txt"

    def env(self) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if key not in ("ODILE_CHECK_LOCK", "ODILE_GATE_EVENT_LOG", "ODILE_GATE_PARENT_PID")}
        env["HOME"] = str(self.tmp)
        return env

    def command(self, label: str, *, wait: int = 60,
                event_log: bool = True) -> list[str]:
        command = [PYTHON, str(HELPER), "--lock", str(self.lock), "--wait", str(wait)]
        if event_log:
            command += ["--event-log", str(self.journal)]
        return command + ["--", PYTHON, "-c", RECORDER, str(self.order), label]

    def hold(self, pid: int = 424242, age: float = 0.0) -> int:
        """A holder outside the queue: a raw flock with a pid+epoch stamp."""
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o666)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, f"{pid} {time.time() - age:.3f}\n".encode("ascii"))
        os.lseek(fd, 0, os.SEEK_SET)
        self.addCleanup(lambda: self._close(fd))
        return fd

    @staticmethod
    def _close(fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    def records(self) -> list[dict]:
        if not self.journal.exists():
            return []
        return [json.loads(line) for line in self.journal.read_text().splitlines() if line]

    def wait_for(self, predicate, timeout: float = 20.0, what: str = "condition") -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail(f"timed out waiting for {what}: {self.records()}")
            time.sleep(0.02)

    def ticket_of(self, pid: int) -> int:
        rows = [row for row in self.records()
                if row["event"] == "ticket" and row["pid"] == pid]
        self.assertEqual(len(rows), 1, f"no ticket row for {pid}: {self.records()}")
        return rows[0]["ticket"]

    def test_waiters_take_the_lock_in_ticket_order(self) -> None:
        fd = self.hold()
        waiters: list[tuple[str, subprocess.Popen]] = []
        for label in ("a", "b", "c"):
            proc = subprocess.Popen(self.command(label), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, env=self.env())
            self.wait_for(lambda pid=proc.pid: any(
                row["event"] == "ticket" and row["pid"] == pid for row in self.records()),
                what=f"the ticket row for {label}")
            waiters.append((label, proc))
        time.sleep(0.1)
        os.close(fd)  # release: the queue must now drain in number order
        outputs = []
        for label, proc in waiters:
            out, err = proc.communicate(timeout=60)
            outputs.append((label, proc.returncode, out, err))
        for label, code, out, err in outputs:
            self.assertEqual(code, 0, f"{label}: {err}")

        ticket_by_pid = {proc.pid: self.ticket_of(proc.pid) for _, proc in waiters}
        text = self.order.read_text()
        acquisitions = [line.split()[0] for line in text.splitlines() if " start " in line]
        self.assertEqual(sorted(acquisitions), ["a", "b", "c"],
                         f"not every waiter ran: {text!r}")
        by_label = {label: ticket_by_pid[proc.pid] for label, proc in waiters}
        tickets = [by_label[label] for label in acquisitions]
        self.assertEqual(tickets, sorted(tickets),
                         f"suites ran out of ticket order: {list(zip(acquisitions, tickets))}")
        self.assertEqual(len(set(tickets)), len(tickets), "two waiters shared a ticket")

        # No overlap: every start is followed by its own end before the next start.
        open_label = None
        for line in text.splitlines():
            label, phase, _stamp = line.split()
            if phase == "start":
                self.assertIsNone(open_label, f"{label} started while {open_label} held the lock")
                open_label = label
            else:
                self.assertEqual(label, open_label, "a suite ended before it started")
                open_label = None
        self.assertIsNone(open_label, "a suite never ended")

        acquire_rows = [row for row in self.records()
                        if row["event"] == "acquire" and row["outcome"] == "acquired"]
        self.assertEqual(len(acquire_rows), 3, self.records())
        self.assertEqual(sorted(row["ticket"] for row in acquire_rows), sorted(tickets))

    def test_a_dead_waiter_does_not_block_the_queue(self) -> None:
        fd = self.hold()
        dead = subprocess.Popen(self.command("dead", wait=120), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=self.env())
        self.wait_for(lambda: any(row["event"] == "ticket" and row["pid"] == dead.pid
                                  for row in self.records()), what="the dead waiter's ticket")
        dead_ticket = self.ticket_of(dead.pid)
        dead.kill()
        dead.communicate(timeout=10)
        live = subprocess.Popen(self.command("live", wait=30), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=self.env())
        self.wait_for(lambda: any(row["event"] == "ticket" and row["pid"] == live.pid
                                  for row in self.records()), what="the second waiter's ticket")
        self.assertEqual(self.ticket_of(live.pid), dead_ticket + 1)
        os.close(fd)
        out, err = live.communicate(timeout=30)
        self.assertEqual(live.returncode, 0, err)
        self.assertIn("live start", self.order.read_text())

    def test_the_cap_names_the_holder_and_its_elapsed_hold(self) -> None:
        self.hold(pid=424242, age=120)
        result = subprocess.run(self.command("never", wait=2), capture_output=True,
                                text=True, timeout=30, env=self.env())
        self.assertEqual(result.returncode, CAP_EXIT, result.stderr)
        self.assertIn("gave up ticket", result.stderr)
        self.assertIn("424242", result.stderr)
        self.assertIn("the suite never started", result.stderr)
        match = re.search(r"for (\d+\.\d)s", result.stderr)
        self.assertIsNotNone(match, result.stderr)
        assert match is not None
        self.assertGreaterEqual(float(match.group(1)), 100.0,
                                "the message does not report the holder's elapsed hold")
        self.assertFalse(self.order.exists(), "the command ran after the cap")
        cap = [row for row in self.records()
               if row["event"] == "acquire" and row["outcome"] == "timeout"]
        self.assertEqual(len(cap), 1, self.records())
        self.assertEqual(cap[0]["code"], CAP_EXIT)
        self.assertEqual(cap[0]["holder_pid"], 424242)
        self.assertGreaterEqual(cap[0]["holder_s"], 100.0)
        self.assertEqual(cap[0]["ticket"], 1)

    def test_the_cap_names_a_raw_flock_holder_from_the_kernel(self) -> None:
        """A `flock(1)` holder writes no pid; /proc/locks still names it."""
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o666)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(self._close, fd)
        result = subprocess.run(self.command("never", wait=1), capture_output=True,
                                text=True, timeout=30, env=self.env())
        self.assertEqual(result.returncode, CAP_EXIT, result.stderr)
        self.assertIn(f"pid {os.getpid()}", result.stderr,
                      "the cap did not name the kernel's flock holder")
        cap = [row for row in self.records()
               if row["event"] == "acquire" and row["outcome"] == "timeout"]
        self.assertEqual(cap[-1]["holder_pid"], os.getpid())

    def test_a_killed_wrapper_keeps_the_lock_until_its_suite_exits(self) -> None:
        """The flock follows the suite, not the killed wrapper (no second suite)."""
        marker = self.tmp / "suite-started"
        done = self.tmp / "suite-done"
        script = (f"import pathlib, time\n"
                  f"pathlib.Path({str(marker)!r}).write_text('held')\n"
                  f"time.sleep(3)\n"
                  f"pathlib.Path({str(done)!r}).write_text('done')\n")
        wrapper = subprocess.Popen(
            [PYTHON, str(HELPER), "--lock", str(self.lock), "--wait", "30",
             "--event-log", str(self.journal), "--", PYTHON, "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env())
        self.wait_for(marker.exists, what="the suite to start")
        wrapper.kill()
        wrapper.wait(timeout=10)
        # `communicate` here would wait for the orphan's inherited pipe ends too.
        assert wrapper.stdout is not None and wrapper.stderr is not None
        wrapper.stdout.close()
        wrapper.stderr.close()
        self.assertFalse(done.exists(), "the suite died with the wrapper")

        second = subprocess.run(self.command("second", wait=1), capture_output=True,
                                text=True, timeout=30, env=self.env())
        self.assertEqual(second.returncode, CAP_EXIT,
                         f"a second suite started while the orphan held the lock: "
                         f"{second.stdout}\n{second.stderr}")
        self.assertNotIn("second start", self.order.read_text()
                         if self.order.exists() else "")

        self.wait_for(done.exists, timeout=15, what="the orphan suite to finish")
        time.sleep(0.5)
        third = subprocess.run(self.command("third", wait=10), capture_output=True,
                               text=True, timeout=30, env=self.env())
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertIn("third start", self.order.read_text())

    def test_the_makefile_cap_is_the_helpers_default(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        helper = HELPER.read_text(encoding="utf-8")
        wanted = re.search(r"^CHECK_LOCK_WAIT \?= (\d+)$", makefile, re.MULTILINE)
        default = re.search(r"^DEFAULT_WAIT = (\d+)$", helper, re.MULTILINE)
        self.assertIsNotNone(wanted, "the Makefile no longer states the queue cap")
        self.assertIsNotNone(default, "check_lock.py no longer states its default cap")
        assert wanted is not None and default is not None
        self.assertEqual(int(wanted.group(1)), int(default.group(1)),
                         "the Makefile cap and check_lock.py's default have drifted")
        self.assertLess(int(wanted.group(1)), 3600,
                        "the cap is back above the old one-hour wait")


if __name__ == "__main__":
    unittest.main()
