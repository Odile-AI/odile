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

The file is shared by every uid on the host, so it must stay openable by all of
them. It lives in a world-writable directory (`/run/lock`), but the file itself
belongs to whoever created it: `os.open(..., 0o666)` is masked by that user's
umask, and a `0644` root-owned file refused `O_RDWR` to the next uid, whose
suite then died on an uncaught `PermissionError` before the gate started
(measured 2026-11-15). Two mechanisms keep it host-wide: the creating holder
chmods the file `0666` (defeating its umask), and a holder that is not the file
owner falls back to `O_RDONLY`, because `flock(2)` needs no write access. A
read-only holder cannot record its pid; the previous holder's pid then stays in
the file until a writable one takes over.

`make check` runs the whole gate under the lock (see the Makefile). The `test`
and `self-test` targets take it too, so a direct `make test` in another
worktree also queues. A nested invocation (the gate's own recursive make) sees
`ODILE_CHECK_LOCK` set to the path it already holds and runs without taking it
again, which is what keeps the lock re-entrant without a second file.

Two instruments make the queue measurable and mortal. Every acquisition appends
one JSON line to `--event-log` (`ODILE_GATE_EVENT_LOG`; the Makefile names
`/var/lock/odile-check-waits.jsonl`) with the exact wait, the holder pid and the
outcome — the lock is otherwise silent between "waiting" and "taken". And
`--parent-pid` (`ODILE_GATE_PARENT_PID`) names the caller: the pre-commit hook
passes `git commit`'s pid and the merge tool passes its own, so a caller killed
by a terminal cap takes its waiter and the suite out of the queue instead of
leaving an orphan that holds the host lock (measured on ODL-242: the killed
commit's hook ran 897 s of tests at 03:30, long after its caller was gone).
"""

import argparse
import datetime as dt
import errno
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

DEFAULT_LOCK = "/var/lock/odile-check.lock"
DEFAULT_WAIT = 3600
DEFAULT_EVENT_LOG = "/var/lock/odile-check-waits.jsonl"
POLL = 0.2
HELD_ENV = "ODILE_CHECK_LOCK"
PARENT_ENV = "ODILE_GATE_PARENT_PID"
EVENT_ENV = "ODILE_GATE_EVENT_LOG"
TIMEOUT_EXIT = 75  # EX_TEMPFAIL: the gate never started, another suite held it
CALLER_GONE_EXIT = 75  # same class: the gate never started, its caller died


def holder_pid(path: str) -> int | None:
    """The pid the current holder wrote, or None when it is unreadable."""
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            first = handle.read(64).split()
        return int(first[0])
    except (OSError, ValueError, IndexError):
        return None


def open_lock(path: str) -> tuple[int, bool]:
    """Open the lock file for `flock` from any uid on the host.

    Returns the descriptor and whether it is writable. The owner of an existing
    file may refuse `O_RDWR` to another uid, and `flock(2)` also works on an
    `O_RDONLY` descriptor, so the pid write is the only thing the fallback
    gives up. A holder that *can* write chmods the file `0666` first: the mode
    passed to `os.open` is masked by its umask, and that is what left the host
    lock `0644` root-owned in the first place.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    except OSError as exc:
        if exc.errno != errno.EACCES:
            raise
        return os.open(path, os.O_RDONLY), False
    try:
        os.fchmod(fd, 0o666)
    except OSError:
        pass  # not our file; the O_RDONLY fallback covers the next holder
    return fd, True


def caller_alive(pid: int | None) -> bool:
    """True while the process that started this gate still exists.

    The pre-commit hook and the merge tool pass their own pid.  The hook is a
    child of `git commit`: when the caller's terminal cap sends SIGTERM, git
    dies but the hook and the suite it started keep queueing and then hold the
    host lock — measured on ODL-242, whose killed commit left a gate that ran
    897 s of tests at 03:30 and printed its result into a log its caller could
    no longer read.  A waiter that watches its caller stops that orphan at the
    source.  pid 0/None means "do not watch" (a direct `make check`).
    """
    if not pid or pid <= 1:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as handle:
            fields = handle.read().split()
        if len(fields) > 2 and fields[2] == "Z":
            return False  # exited, not yet reaped by its own parent
    except OSError:
        return False  # no /proc entry: the pid is gone
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def log_event(path: str | None, payload: dict) -> None:
    """Append one JSON line to the gate's wait journal; never fail the gate.

    The journal is the after-change measurement's source: the lock announces a
    wait once and is silent until it is taken, so without a record the wait can
    only be inferred from a suite's total wall time.  The file lives in the
    world-writable lock directory and is opened 0666 for the same reason the
    lock is: the next uid must be able to append.
    """
    if not path:
        return
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
    except OSError:
        return
    try:
        try:
            os.fchmod(fd, 0o666)
        except OSError:
            pass
        record = dict(payload)
        record.setdefault("utc", dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        record.setdefault("pid", os.getpid())
        os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
    except OSError:
        pass
    finally:
        os.close(fd)


def acquire(path: str, wait: int, parent: int | None = None,
            event_log: str | None = None,
            command: list[str] | None = None) -> tuple[int | None, str]:
    """Take the lock; (fd, "acquired") or (None, "timeout"|"caller-gone")."""
    parent_dir = os.path.dirname(os.path.abspath(path))
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    fd, writable = open_lock(path)
    started = time.monotonic()
    deadline = started + wait
    announced = False
    holder: int | None = None
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                os.close(fd)
                raise
        if not announced:
            holder = holder_pid(path)
            print(f"waiting for another make check (pid {holder if holder is not None else 'unknown'})",
                  flush=True)
            announced = True
        if not caller_alive(parent):
            os.close(fd)
            log_event(event_log, {
                "event": "acquire", "lock": os.path.abspath(path),
                "parent": parent, "waited_s": round(time.monotonic() - started, 1),
                "holder_pid": holder, "queued": announced, "outcome": "caller-gone",
                "command": " ".join(command or [])[:300],
            })
            print(f"make check: caller {parent} is gone; leaving the queue", file=sys.stderr)
            return None, "caller-gone"
        if time.monotonic() >= deadline:
            os.close(fd)
            log_event(event_log, {
                "event": "acquire", "lock": os.path.abspath(path),
                "parent": parent, "waited_s": round(time.monotonic() - started, 1),
                "holder_pid": holder, "queued": announced, "outcome": "timeout",
                "command": " ".join(command or [])[:300],
            })
            return None, "timeout"
        time.sleep(POLL)
    waited = time.monotonic() - started
    if writable:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    log_event(event_log, {
        "event": "acquire", "lock": os.path.abspath(path),
        "parent": parent, "waited_s": round(waited, 1),
        "holder_pid": holder, "queued": announced, "outcome": "acquired",
        "command": " ".join(command or [])[:300],
    })
    return fd, "acquired"


def run(argv: list[str], wait: int, held: str | None = None,
        parent: int | None = None, event_log: str | None = None) -> int:
    """Run the command, holding the lock (or trusting an enclosing holder)."""
    env = dict(os.environ)
    if held:
        fd, outcome = acquire(held, wait, parent=parent, event_log=event_log, command=argv)
        if fd is None:
            if outcome == "timeout":
                print(f"make check: could not take {held} after {wait}s; "
                      "another suite still holds it", file=sys.stderr)
            return TIMEOUT_EXIT
        env[HELD_ENV] = os.path.abspath(held)
        started = time.monotonic()
    else:
        fd = None
        started = time.monotonic()
    # The child leads its own session so that a caller that dies can stop the
    # whole suite (`stop`), not just `make`; a direct `make check` forwards the
    # terminal's signals through the handlers below as before.
    proc = subprocess.Popen(argv, env=env, start_new_session=True)

    def forward(signum: int, _frame: object) -> None:
        if proc.poll() is None:
            try:
                proc.send_signal(signum)
            except ProcessLookupError:
                pass

    previous = {sig: signal.signal(sig, forward)
                for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    caller_gone = False
    try:
        while proc.poll() is None:
            if not caller_alive(parent):
                caller_gone = True
                stop(proc)
                break
            time.sleep(0.1)
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        log_event(event_log, {
            "event": "release", "lock": os.path.abspath(held) if held else "",
            "parent": parent, "held_s": round(time.monotonic() - started, 1),
            "exit": proc.returncode, "caller_gone": caller_gone,
        })
    if caller_gone:
        print(f"make check: caller {parent} is gone; stopped the suite", file=sys.stderr)
        return CALLER_GONE_EXIT
    return 128 - proc.returncode if proc.returncode < 0 else proc.returncode


def stop(proc: subprocess.Popen) -> None:
    """Stop the child's whole session and give it a moment to exit.

    The child is a session leader (`start_new_session=True`), so `killpg`
    reaches `make`, the suite's solver and the demo servers it started — the
    orphan measured on ODL-242 was a whole make tree that outlived its caller.
    """
    if proc.poll() is not None:
        return
    for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Serialize the heavy half of `make check` across worktrees on one host.")
    parser.add_argument("--lock", default=DEFAULT_LOCK,
                        help=f"lock file (default {DEFAULT_LOCK})")
    parser.add_argument("--wait", type=int, default=DEFAULT_WAIT,
                        help=f"seconds to wait for the lock (default {DEFAULT_WAIT})")
    parser.add_argument("--parent-pid", type=int,
                        default=int(os.environ.get(PARENT_ENV, "0") or "0"),
                        help=f"the caller to watch; exit 75 when it dies "
                             f"(default ${PARENT_ENV} or 0)")
    parser.add_argument("--event-log",
                        default=os.environ.get(EVENT_ENV, ""),
                        help=f"JSONL wait journal (default ${EVENT_ENV}, else empty; "
                             f"a path enables it)")
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
        return run(command, args.wait, parent=args.parent_pid or None,
                   event_log=args.event_log or None)
    return run(command, args.wait, held=args.lock, parent=args.parent_pid or None,
               event_log=args.event_log or None)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
