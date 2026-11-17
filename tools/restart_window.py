#!/usr/bin/python3
"""Gate a gateway restart or a Hermes update on the board's live runs.

The 17 November spend audit (§3.1) counted 86 workers that died without
finishing; 27 of them within seven minutes of a logged gateway stop, in bursts
of six and seven across unrelated cards, and 36 exited 130 (SIGINT, the
interrupt a shutdown sends). `dmesg` shows no OOM kill — the restart is the
killer. A restart is a company-wide abort, so it gets one gate.

The window is **03:00–03:30Z**. Outside it a restart refuses while any card is
`running`; inside it, a restart is allowed and the gateway's own drain
(`agent.restart_drain_timeout`, 300 s in this host's config) interrupts what is
still live. An update is stricter: it needs zero live runs at any hour, because
`hermes update` half-applied leaves six runs dying on `a source update is
unfinished`.

    ./tools/restart_window.py                      # dry run: verdict and live cards
    ./tools/restart_window.py --apply              # restart, if the gate allows
    ./tools/restart_window.py --wait 300 --apply   # drain first, then restart
    ./tools/restart_window.py --for update --apply # update only at zero live runs

Exit codes: 0 allowed (and the command ran, with `--apply`); 2 refused; 3 no
board database at the resolved path.
`--at` pins the clock for a reproducible transcript; `--db` and `--hermes-bin`
exist for the tests, and default to this host's board and binary.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BOARD = "odile"
WINDOW_START = dt.time(3, 0)
WINDOW_END = dt.time(3, 30)
DEFAULT_HERMES = "/root/.hermes/hermes-agent/.hermes/bin/hermes"

# The host gateway is the default profile's: `hermes gateway restart` from a
# seat profile would unserve/re-serve that seat on the multiplexer instead of
# restarting the gateway (hermes_cli/gateway_profile_lifecycle.py). Both
# commands are therefore addressed to the default profile, like the pause
# recipe in docs/ops/OPERATING.md.
COMMANDS = {
    "restart": ("-p", "default", "gateway", "restart", "-y"),
    "update": ("-p", "default", "update", "--yes"),
}


# --------------------------------------------------------------------------- board


def hermes_root() -> Path:
    """The Hermes root, also when a seat shell points HERMES_HOME at a profile."""
    home = os.environ.get("HERMES_HOME", "").strip()
    root = Path(home).expanduser() if home else Path.home() / ".hermes"
    return root.parent.parent if root.parent.name == "profiles" else root


def board_db() -> Path:
    """The board's SQLite file, resolved the way the CLI resolves it.

    ``HERMES_KANBAN_DB`` pins the file (the dispatcher injects it into
    workers); otherwise the board lives under the shared Kanban home, which is
    deliberately not the running profile's ``HERMES_HOME``. Same derivation as
    ``tools/product_pulse.py``: on this host the board is
    ``~/.hermes/kanban/boards/odile/kanban.db``, not ``~/.hermes/kanban.db``.
    """
    pin = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if pin:
        return Path(pin).expanduser()
    home = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    root = Path(home).expanduser() if home else hermes_root()
    return root / "kanban" / "boards" / BOARD / "kanban.db"


def live_runs(db: Path) -> list[dict]:
    """Every card the dispatcher is running now, with the pid that holds it.

    A stale `running` row whose pid is dead still counts: this gate prefers
    waiting for the stale sweep over restarting on top of a row another
    supervisor may be about to use.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, title, assignee, current_run_id, worker_pid"
            " FROM tasks WHERE status = 'running' ORDER BY created_at, id"
        ).fetchall()
    finally:
        con.close()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- time


def parse_at(text: str) -> dt.datetime:
    """An ISO timestamp as aware UTC; a bare timestamp is read as UTC."""
    value = dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def iso(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def in_window(now: dt.datetime) -> bool:
    """True inside 03:00–03:30Z; the window is half-open, as windows should be."""
    moment = now.astimezone(dt.timezone.utc).time().replace(tzinfo=None)
    return WINDOW_START <= moment < WINDOW_END


def next_window(now: dt.datetime) -> dt.datetime:
    moment = now.astimezone(dt.timezone.utc)
    candidate = moment.replace(
        hour=WINDOW_START.hour, minute=WINDOW_START.minute, second=0, microsecond=0
    )
    if candidate <= moment:
        candidate += dt.timedelta(days=1)
    return candidate


# --------------------------------------------------------------------------- rule


def decide(action: str, live: list[dict], now: dt.datetime) -> tuple[bool, str]:
    """The rule, with no I/O: may this action run now, and why.

    An update needs zero live runs at any hour. A restart needs zero live runs
    outside the window; inside it, the gateway drains what is left.
    """
    if not live:
        return True, "no live run"
    if action == "update":
        return False, f"an update needs zero live runs ({len(live)} live)"
    if in_window(now):
        return True, (
            f"inside the 03:00–03:30Z window with {len(live)} live run(s); "
            "the gateway drains up to agent.restart_drain_timeout"
        )
    return False, (
        f"a restart outside 03:00–03:30Z needs zero live runs ({len(live)} live)"
    )


def wait_for_idle(
    db: Path, timeout: float, poll: float = 5.0, sleep=time.sleep
) -> list[dict]:
    """Poll the board until it has no live run or the deadline passes.

    Returns whatever is still live at the deadline (empty means drained). The
    sleep is injectable so the test drives the clock instead of waiting on it.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    remaining = live_runs(db)
    while remaining and time.monotonic() < deadline:
        sleep(min(poll, max(0.0, deadline - time.monotonic())))
        remaining = live_runs(db)
    return remaining


# --------------------------------------------------------------------------- cli


def hermes_bin(explicit: str | None) -> str:
    if explicit:
        return explicit
    pinned = os.environ.get("ODILE_HERMES_BIN", "").strip()
    if pinned:
        return pinned
    found = shutil.which("hermes")
    if found:
        return found
    return DEFAULT_HERMES


def print_runs(live: list[dict]) -> None:
    for run in live:
        pid = f"  pid {run['worker_pid']}" if run.get("worker_pid") else ""
        title = str(run.get("title") or "")[:60]
        print(
            f"  {run['id']}  {run.get('assignee') or '-'}  \"{title}\"{pid}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refuse a gateway restart outside 03:00–03:30Z while any "
        "kanban run is live, and an update while any run is live."
    )
    parser.add_argument(
        "--for",
        dest="action",
        choices=sorted(COMMANDS),
        default="restart",
        help="what to gate (default: restart)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="run the command when the gate allows; without it this is a dry run",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="wait up to SECONDS for live runs to finish before deciding again",
    )
    parser.add_argument("--poll", type=float, default=5.0, help="seconds between polls")
    parser.add_argument("--at", help="pin the clock, ISO-8601 UTC (for evidence)")
    parser.add_argument("--db", help="board database (default: the odile board)")
    parser.add_argument("--hermes-bin", help="the hermes executable to run")
    args = parser.parse_args(argv)

    now = parse_at(args.at) if args.at else dt.datetime.now(dt.timezone.utc)
    db = Path(args.db).expanduser() if args.db else board_db()
    if not db.is_file():
        print(f"restart_window: no board database at {db}", file=sys.stderr)
        return 3

    print(f"restart_window: board {db}")
    print(
        f"restart_window: now {iso(now)} — "
        + ("inside" if in_window(now) else "outside")
        + " 03:00–03:30Z"
    )
    live = live_runs(db)
    print(f"restart_window: {len(live)} live run(s)")
    print_runs(live)

    if args.wait > 0 and live:
        print(f"restart_window: draining up to {args.wait:g}s (poll {args.poll:g}s)")
        live = wait_for_idle(db, args.wait, args.poll)
        print(f"restart_window: {len(live)} live run(s) after the wait")
        print_runs(live)

    allowed, reason = decide(args.action, live, now)
    if not allowed:
        print(f"restart_window: REFUSED — {reason}")
        if args.action == "restart":
            print(f"restart_window: next window {iso(next_window(now))}")
        return 2

    command = [hermes_bin(args.hermes_bin), *COMMANDS[args.action]]
    print(f"restart_window: allowed — {reason}")
    if not args.apply:
        print(f"restart_window: would run: {' '.join(command)}")
        return 0
    print(f"restart_window: running: {' '.join(command)}")
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
