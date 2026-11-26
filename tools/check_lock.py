#!/usr/bin/python3
"""One host-wide lock around the heavy half of `make check`, on a fair queue.

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

on stdout and takes a **ticket** first: a file in `<lock>.queue/` whose number
is `max(existing)+1`, published atomically by linking an already-`flock`ed temp
file into place (`EEXIST` retries at the new max — there is no counter file to
reset or freeze). The owner holds its ticket's flock for its whole wait, so a
live ticket is always locked and a dead owner's ticket is visibly unlocked. Only
the lowest live ticket may attempt the lock. Stale tickets are ignored, never
unlinked by another uid, so a crash leaves a harmless file rather than a queue
that depends on cross-uid unlink rights (the queue directory is sticky). The
old barge: `flock` has no waiter queue, so a fresh caller could take the lock
ahead of one that had waited an hour (measured 2026-11-25: 3,013.4 s and
3,813.6 s waits landing beside 0 s acquires).

`--wait` is the queue cap (default 900 s): about one full suite, measured at
the shared lock as p50 744.7 s, p90 876.0 s over 84 holds on 2026-11-25/26. At
the cap the caller gets one last lock attempt if it is the head, then prints
its ticket, how many live tickets are ahead, the holder's pid and how long the
holder has held the lock, and exits **69** (EX_UNAVAILABLE: the gate never
started inside the cap). 75 stays `caller-gone`. The lock file carries
`<pid> <epoch>`; the first token is the old format, so an old holder's pid is
still readable.

The lock fd is passed into the suite (`pass_fds`), and SIGTERM/SIGINT/SIGHUP
stop the child's whole session. A wrapper killed by SIGKILL therefore no longer
releases the lock while `make` and its tests keep running: the inherited
open-file description keeps the flock until the last suite process closes it.
That was a mutual-exclusion hole before the queue: the killed waiter held no
lock, so the next caller started a second suite beside the orphan.

The file is shared by every uid on the host, so it must stay openable by all of
them. It lives in a world-writable directory (`/run/lock`), but the file itself
belongs to whoever created it: `os.open(..., 0o666)` is masked by that user's
umask, and a `0644` root-owned file refused `O_RDWR` to the next uid, whose
suite then died on an uncaught `PermissionError` before the gate started
(measured 2026-11-15). Two mechanisms keep it host-wide: the creating holder
chmods the file `0666` (defeating its umask), and a holder that is not the file
owner falls back to `O_RDONLY`, because `flock(2)` needs no write access. A
read-only holder cannot record its pid; the previous holder's pid then stays in
the file until a writable one takes over. The queue directory is created by the
first caller and chmodded 1777 for the same reason; tickets are 0666 and are
read `O_RDONLY`, and a scanner never unlinks another owner's file.

`make check` runs the whole gate under the lock (see the Makefile). The `test`
and `self-test` targets take it too, so a direct `make test` in another
worktree also queues. A nested invocation (the gate's own recursive make) sees
`ODILE_CHECK_LOCK` set to the path it already holds and runs without taking it
again, which is what keeps the lock re-entrant without a second file.

Two instruments make the queue measurable and mortal. Every ticket and every
acquisition appends one JSON line to `--event-log` (`ODILE_GATE_EVENT_LOG`; the
Makefile names `/var/lock/odile-check-waits.jsonl`) with the exact wait, the
ticket, the live tickets ahead, the holder pid and the outcome — the lock is
otherwise silent between "waiting" and "taken", and the ticket rows are what
lets the after-change read prove ascending ticket order instead of inferring
it. And `--parent-pid` (`ODILE_GATE_PARENT_PID`) names the caller: the
pre-commit hook passes `git commit`'s pid and the merge tool passes its own, so
a caller killed by a terminal cap takes its waiter and the suite out of the
queue instead of leaving an orphan that holds the host lock (measured on
ODL-242: the killed commit's hook ran 897 s of tests at 03:30, long after its
caller was gone).

The queue orders cooperating gate callers on one trusted host. A process that
takes the `flock` directly, or a hostile uid that deletes a live ticket, can
still barge; the ticket layer can only reorder attempts, never overlap them,
because the flock remains the sole exclusion primitive.
"""

import argparse
import datetime as dt
import errno
import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import time

DEFAULT_LOCK = "/var/lock/odile-check.lock"
# One full suite at p90: shared-lock holds p50 744.7 s, p90 876.0 s, max
# 2,014.6 s (84 holds, 2026-11-25T05:13Z..2026-11-26T00:14Z). A waiter at 900 s
# has outlived 90% of the suites it could be waiting for.
DEFAULT_WAIT = 900
DEFAULT_EVENT_LOG = "/var/lock/odile-check-waits.jsonl"
POLL = 0.2
HELD_ENV = "ODILE_CHECK_LOCK"
PARENT_ENV = "ODILE_GATE_PARENT_PID"
EVENT_ENV = "ODILE_GATE_EVENT_LOG"
QUEUE_SUFFIX = ".queue"
HIGH_WATER = ".high"  # best-effort hint so numbers keep rising between busy periods
TICKET_DIGITS = 6  # formatted width; comparisons are ints, so growth is safe
CAP_EXIT = 69       # EX_UNAVAILABLE: the gate never started inside the cap
CALLER_GONE_EXIT = 75  # same class: the gate never started, its caller died


def queue_dir(path: str) -> str:
    """The ticket directory that belongs to one lock file."""
    return os.path.abspath(path) + QUEUE_SUFFIX


def ticket_name(number: int) -> str:
    return f"{number:0{TICKET_DIGITS}d}"


def holder_pid(path: str) -> int | None:
    """The pid the current holder wrote, or None when it is unreadable."""
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            first = handle.read(64).split()
        return int(first[0])
    except (OSError, ValueError, IndexError):
        return None


def holder_elapsed(path: str) -> float | None:
    """How long the current holder has held the lock, from its epoch stamp.

    The lock file is `<pid> <epoch>`; a holder from the old code wrote only the
    pid, so the elapsed time is unknown rather than guessed.
    """
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            fields = handle.read(128).split()
    except OSError:
        return None
    if len(fields) < 2:
        return None
    try:
        started = float(fields[1])
    except ValueError:
        return None
    return max(0.0, time.time() - started)


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


def ensure_queue(qdir: str) -> None:
    """Create `<lock>.queue` open to every uid, and refuse a symlink or file.

    The directory lives beside the lock in a world-writable parent, so the
    first caller creates it and chmods it `1777`: every uid may create its own
    ticket, and only a ticket's owner (or root) may unlink it. A pre-existing
    regular file or a symlink pointed at another directory is refused rather
    than followed.
    """
    try:
        os.makedirs(qdir, mode=0o1777, exist_ok=True)
    except FileExistsError:
        pass
    info = os.lstat(qdir)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"{qdir} is not a ticket directory")
    try:
        os.chmod(qdir, 0o1777)
    except OSError:
        pass


def ticket_numbers(qdir: str) -> list[int]:
    """Every decimal ticket name in the queue directory, as integers."""
    try:
        names = os.listdir(qdir)
    except OSError:
        return []
    numbers = []
    for name in names:
        if name.isdigit() and len(name) <= 9:
            numbers.append(int(name))
    return numbers


def ticket_live(path: str) -> bool:
    """True while the ticket's owner still holds its flock.

    A live owner keeps its ticket descriptor open and flocked for its whole
    wait, so an unlocked — or vanished — ticket is stale. Non-regular entries
    (a FIFO or directory named like a ticket) and unreadable files are handled
    without blocking: a FIFO would hang the poll loop, and a directory cannot
    be flocked the way a ticket is.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    except OSError as exc:
        return exc.errno != errno.ELOOP  # ELOOP is a symlink, not a ticket
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        return False
    finally:
        os.close(fd)


def tickets_ahead(qdir: str, number: int) -> int:
    """How many live tickets with a lower number are still waiting."""
    try:
        names = os.listdir(qdir)
    except OSError:
        return 0
    ahead = 0
    for name in names:
        if not (name.isdigit() and len(name) <= 9):
            continue
        other = int(name)
        if other >= number:
            continue
        if ticket_live(os.path.join(qdir, name)):
            ahead += 1
    return ahead


def high_water(qdir: str) -> int:
    """The last ticket number this queue issued, as a best-effort hint.

    Without it, numbers would restart at 1 every time the queue empties — fair,
    but the journal would repeat 1 all day. It is never locked and never
    authoritative: a missing, stale or unreadable file only restarts the count,
    while `max(live names)` keeps every published ticket ordered.
    """
    try:
        with open(os.path.join(qdir, HIGH_WATER), encoding="ascii", errors="replace") as handle:
            return int(handle.read(32).split()[0])
    except (OSError, ValueError, IndexError):
        return 0


def note_high_water(qdir: str, number: int) -> None:
    """Remember the highest issued number; a failure must never fail the gate."""
    try:
        fd = os.open(os.path.join(qdir, HIGH_WATER), os.O_RDWR | os.O_CREAT, 0o666)
    except OSError:
        return
    try:
        try:
            os.fchmod(fd, 0o666)
        except OSError:
            pass
        try:
            current = int(os.read(fd, 32).split()[0])
        except (OSError, ValueError, IndexError):
            current = 0
        if number > current:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, f"{number}\n".encode("ascii"))
    except OSError:
        pass
    finally:
        os.close(fd)


def take_ticket(qdir: str) -> tuple[int, int]:
    """Publish a ticket and return `(number, descriptor)`.

    The number is one past the highest live or issued number; the temp file is
    flocked *before* it is linked into place, so a visible ticket is always
    owned by a live process (or visibly stale). Two callers that choose the same
    number race on the link: one wins, the other retries at the new max. There
    is no counter to reset, freeze, or make every later caller wait on.
    """
    ensure_queue(qdir)
    for _ in range(1000):
        number = max(max(ticket_numbers(qdir), default=0), high_water(qdir)) + 1
        temp = os.path.join(qdir, f".t{os.getpid()}-{number}")
        try:
            fd = os.open(temp, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            continue
        try:
            try:
                os.fchmod(fd, 0o666)
            except OSError:
                pass
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                os.link(temp, os.path.join(qdir, ticket_name(number)))
            except FileExistsError:
                os.close(fd)
                continue
            note_high_water(qdir, number)
            return number, fd
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        finally:
            try:
                os.unlink(temp)
            except OSError:
                pass
    raise RuntimeError(f"could not publish a ticket in {qdir}")


def release_ticket(qdir: str, number: int | None, fd: int | None) -> None:
    """Remove this caller's own ticket (never another owner's)."""
    if number is not None:
        try:
            os.unlink(os.path.join(qdir, ticket_name(number)))
        except OSError:
            pass
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


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
    qdir = queue_dir(path)
    number: int | None = None
    ticket_fd: int | None = None
    try:
        number, ticket_fd = take_ticket(qdir)
        log_event(event_log, {
            "event": "ticket", "lock": os.path.abspath(path),
            "ticket": number, "command": " ".join(command or [])[:300],
        })
    except (OSError, RuntimeError) as exc:
        # The lock still works without the queue; a fair queue is the point,
        # but losing it must not lose the gate.
        print(f"make check: ticket queue unavailable ({exc}); waiting without a ticket",
              file=sys.stderr)
    announced = False
    holder: int | None = None
    while True:
        ahead = tickets_ahead(qdir, number) if number is not None else 0
        if ahead == 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    release_ticket(qdir, number, ticket_fd)
                    os.close(fd)
                    raise
        if not announced:
            holder = holder_pid(path)
            place = f"; ticket {number}" if number is not None else "; no ticket"
            if number is not None:
                place += f", {ahead} ahead" if ahead else ", next"
            print(f"waiting for another make check (pid {holder if holder is not None else 'unknown'}){place}",
                  flush=True)
            announced = True
        if not caller_alive(parent):
            release_ticket(qdir, number, ticket_fd)
            os.close(fd)
            log_event(event_log, {
                "event": "acquire", "lock": os.path.abspath(path),
                "parent": parent, "waited_s": round(time.monotonic() - started, 1),
                "holder_pid": holder, "queued": announced, "outcome": "caller-gone",
                "ticket": number, "ahead": ahead,
                "command": " ".join(command or [])[:300],
            })
            print(f"make check: caller {parent} is gone; leaving the queue", file=sys.stderr)
            return None, "caller-gone"
        if time.monotonic() >= deadline:
            if ahead == 0:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    pass
            holder = holder_pid(path)
            held = holder_elapsed(path)
            release_ticket(qdir, number, ticket_fd)
            os.close(fd)
            log_event(event_log, {
                "event": "acquire", "lock": os.path.abspath(path),
                "parent": parent, "waited_s": round(time.monotonic() - started, 1),
                "holder_pid": holder, "queued": announced, "outcome": "timeout",
                "ticket": number, "ahead": ahead, "holder_s": None if held is None else round(held, 1),
                "code": CAP_EXIT,
                "command": " ".join(command or [])[:300],
            })
            where = f"{ahead} live ticket{'s' if ahead != 1 else ''} ahead" if number is not None else "no ticket"
            for_how_long = f"{held:.1f}s" if held is not None else "an unknown time"
            print(f"make check: gave up ticket {number} after {wait}s ({where}); "
                  f"pid {holder if holder is not None else 'unknown'} has held "
                  f"{os.path.abspath(path)} for {for_how_long}; the suite never started. "
                  "another suite still holds it",
                  file=sys.stderr)
            return None, "timeout"
        time.sleep(POLL)
    waited = time.monotonic() - started
    held_before = holder_elapsed(path)
    if writable:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {time.time():.3f}\n".encode("ascii"))
    release_ticket(qdir, number, ticket_fd)
    log_event(event_log, {
        "event": "acquire", "lock": os.path.abspath(path),
        "parent": parent, "waited_s": round(waited, 1),
        "holder_pid": holder, "queued": announced, "outcome": "acquired",
        "ticket": number, "ahead": 0,
        "holder_s": None if held_before is None else round(held_before, 1),
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
            return CAP_EXIT if outcome == "timeout" else CALLER_GONE_EXIT
        env[HELD_ENV] = os.path.abspath(held)
        started = time.monotonic()
    else:
        fd = None
        started = time.monotonic()
    # The child leads its own session so that a caller that dies can stop the
    # whole suite (`stop`), not just `make`; the lock descriptor is passed into
    # it so a wrapper killed by SIGKILL does not release the lock while the
    # suite it started keeps running. A nested `check_lock` trusts
    # ODILE_CHECK_LOCK and does not queue again.
    state = {"signal": 0}

    def forward(signum: int, _frame: object) -> None:
        state["signal"] = signum

    previous = {sig: signal.signal(sig, forward)
                for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    proc: subprocess.Popen | None = None
    caller_gone = False
    try:
        try:
            proc = subprocess.Popen(
                argv, env=env, start_new_session=True,
                pass_fds=(fd,) if fd is not None else ())
        except OSError as exc:
            print(f"make check: could not start {argv[0]}: {exc}", file=sys.stderr)
            return 127
        while proc.poll() is None:
            if state["signal"]:
                stop(proc)
                break
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
            "exit": proc.returncode if proc is not None else None,
            "caller_gone": caller_gone,
        })
    if caller_gone:
        print(f"make check: caller {parent} is gone; stopped the suite", file=sys.stderr)
        return CALLER_GONE_EXIT
    assert proc is not None  # a Popen that failed returned 127 above
    return 128 - proc.returncode if proc.returncode < 0 else proc.returncode


def stop(proc: subprocess.Popen) -> None:
    """Stop the child's whole session and give it a moment to exit.

    The child is a session leader (`start_new_session=True`), so `killpg`
    reaches `make`, the suite's solver and the demo servers it started — the
    orphan measured on ODL-242 was a whole make tree that outlived its caller.
    The inherited lock descriptor is released when the last of those processes
    exits, which is the point: the lock now follows the suite, not the wrapper.
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
                        help=f"seconds to hold a ticket before exiting {CAP_EXIT} "
                             f"(default {DEFAULT_WAIT})")
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
