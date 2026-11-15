#!/usr/bin/python3
"""One host-wide lock around the heavy half of `make check`.

`make check` is the CPU ceiling on this box: `tests/test_sealed_image.py` runs
the real first-boot re-key, and `cryptsetup reencrypt` uses four threads, so one
suite peaks at about four cores. Two suites that overlap plus the boot-image
QEMU exceed the six vCPUs (measured 2026-11-14: load 13.0, cryptsetup 341%
average with a single process peak at 405%). The gate therefore takes one lock,
not one per worktree, so a suite started anywhere on the host queues behind the
one already running instead of stacking on top of it.

The lock is an `flock(2)` on `--lock` (default `/var/lock/odile-check.lock`).
A second suite prints

    waiting for another make check (pid N)

on stdout and waits up to `--wait` seconds (default 3600) before giving up.
The holder's pid is written into the lock file, which is what N names; the
file is only read while the lock is contended, so a stale pid after a clean
exit is never mistaken for a live holder.

`make check` runs the whole gate under the lock (see the Makefile). The `test`
and `self-test` targets take it too, so a direct `make test` in another
worktree also queues. A nested invocation (the gate's own recursive make) sees
`ODILE_CHECK_LOCK` set to the path it already holds and runs without taking it
again, which is what keeps the lock re-entrant without a second file.
"""

import argparse
import errno
import fcntl
import os
import signal
import subprocess
import sys
import time

DEFAULT_LOCK = "/var/lock/odile-check.lock"
DEFAULT_WAIT = 3600
POLL = 0.2
HELD_ENV = "ODILE_CHECK_LOCK"
TIMEOUT_EXIT = 75  # EX_TEMPFAIL: the gate never started, another suite held it


def holder_pid(path: str) -> int | None:
    """The pid the current holder wrote, or None when it is unreadable."""
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            first = handle.read(64).split()
        return int(first[0])
    except (OSError, ValueError, IndexError):
        return None


def acquire(path: str, wait: int) -> int | None:
    """Take the lock, announcing a wait once; None when the wait expired."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    deadline = time.monotonic() + wait
    announced = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                os.close(fd)
                raise
        if not announced:
            pid = holder_pid(path)
            print(f"waiting for another make check (pid {pid if pid is not None else 'unknown'})",
                  flush=True)
            announced = True
        if time.monotonic() >= deadline:
            os.close(fd)
            return None
        time.sleep(POLL)
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    return fd


def run(argv: list[str], wait: int, held: str | None = None) -> int:
    """Run the command, holding the lock (or trusting an enclosing holder)."""
    env = dict(os.environ)
    if held:
        fd = acquire(held, wait)
        if fd is None:
            print(f"make check: could not take {held} after {wait}s; "
                  "another suite still holds it", file=sys.stderr)
            return TIMEOUT_EXIT
        env[HELD_ENV] = os.path.abspath(held)
    else:
        fd = None
    proc = subprocess.Popen(argv, env=env)

    def forward(signum: int, _frame: object) -> None:
        if proc.poll() is None:
            try:
                proc.send_signal(signum)
            except ProcessLookupError:
                pass

    previous = {sig: signal.signal(sig, forward)
                for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        while proc.poll() is None:
            time.sleep(0.1)
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    return 128 - proc.returncode if proc.returncode < 0 else proc.returncode


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Serialize the heavy half of `make check` across worktrees on one host.")
    parser.add_argument("--lock", default=DEFAULT_LOCK,
                        help=f"lock file (default {DEFAULT_LOCK})")
    parser.add_argument("--wait", type=int, default=DEFAULT_WAIT,
                        help=f"seconds to wait for the lock (default {DEFAULT_WAIT})")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="command to run under the lock, after `--`")
    args = parser.parse_args(argv)
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("no command given (write `-- <command> [args...]`)")
    enclosing = os.environ.get(HELD_ENV)
    if enclosing and os.path.abspath(enclosing) == os.path.abspath(args.lock):
        return run(command, args.wait)
    return run(command, args.wait, held=args.lock)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
