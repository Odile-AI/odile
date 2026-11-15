#!/usr/bin/python3
"""Build the public status page from Odile's own history.

The page publishes uptime rather than claiming it.  Every figure comes from
two append-only tables in the HQ database:

* `heartbeats` — the signed beats the guests send every five minutes;
* `tenant_state_events` — the transition log, which says when a machine was
  in service.

Per-tenant status is not public (a slug is semi-private), so the page carries
only aggregates and the last five incidents.  It names no slug, no owner and
no machine.

    /usr/bin/python3 status/status.py --json
    /usr/bin/python3 status/status.py --out /var/lib/odile/status.html

`app.py` serves the same renderer at `/status`: it builds the page from the
database on every request, so a regeneration lands without a restart.  The
command above remains the offline dry run and the cron-shaped writer.

`--stdout` renders the page without writing it, so the job has a dry run.
`--now` pins the clock, which makes the evidence reproducible.

Definitions, because an uptime number without one is a decoration:

* *accountable time* — seconds a machine was `ready` inside the 30-day window.
  Provisioning and suspended time is neither up nor down.  A legacy
  `waiting_for_owner` segment is neither: it was the old initramfs park, which
  the sealed boot removed (ODL-467), and it gets no bucket of its own.
* *up* — a heartbeat arrived no more than ten minutes ago.  Ten minutes is
  two missed five-minute beats, the same threshold the alerting uses.  A
  reboot shorter than that window never leaves the ratio, because the machine
  comes back by itself.
* *downtime* — accountable time that is not up.

Odile's own demo machine is not a tenant and is not published.  Its password is
public by design, so a judge can stop it, restart it or break it; counting its
uptime would turn Odile's reliability figure into a function of who visited.
`DEMO_SLUGS` names the tenancy ids the page leaves out of every aggregate and
out of the incident list, and the page names no slug either way.
"""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import os
import re
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odilectl.state import DEFAULT_STATE_DB, HEARTBEAT_STOP_SECONDS  # noqa: E402

WINDOW_DAYS = 30
WINDOW_SECONDS = WINDOW_DAYS * 24 * 60 * 60
TEMPLATE_PATH = ROOT / "templates" / "status.html"
DEFAULT_OUT = "/var/lib/odile/status.html"
# A machine is in service only while it is `ready`.  `waiting_for_owner` is the
# retired initramfs park (ODL-467): legacy segments stay in the history but are
# neither up nor down, and they get no bucket.
SERVICE_STATES = ("ready",)
# An incident is a stopped heartbeat.  Legacy `waiting_for_owner` ledger rows
# are history of the retired park, not incidents of the sealed boot.
INCIDENT_KINDS = ("down",)
INCIDENT_LIMIT = 5
UTC = timezone.utc

# Odile's own demo machine (the judge path).  Its password is published, so a
# judge can stop or break it; its uptime is Odile's demo, not a tenant's, and
# publishing it would make the reliability figure depend on who visited.  The
# page leaves these tenancy ids out of the aggregate and the incident list.
# `odl-7971a8` is the demo's tenancy id and survives a rebuild of the machine.
DEMO_SLUGS = frozenset({"odl-7971a8"})

# The three questions the page answers, as SQL.  They are kept here rather
# than hidden in the module so `--json` can print them: the evidence for a
# number is the query that produced it.
QUERIES = {
    "tenants": (
        "SELECT slug, state, created_at FROM tenant_state ORDER BY slug"
    ),
    "state_segments": (
        "SELECT slug, to_state, created_at FROM tenant_state_events"
        " WHERE created_at <= :now ORDER BY slug, id"
    ),
    "heartbeats": (
        "SELECT slug, at FROM heartbeats"
        " WHERE at >= :since AND at <= :now ORDER BY slug, at"
    ),
    "incidents": (
        "SELECT slug, kind, first_seen_at, resolved_at FROM health_alerts"
        f" WHERE kind IN ({', '.join(repr(kind) for kind in INCIDENT_KINDS)})"
        " AND (resolved_at IS NULL OR resolved_at >= :since)"
        " ORDER BY first_seen_at DESC, id DESC LIMIT 5"
    ),
}

SLOT_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


class StatusError(Exception):
    """The history could not be read, or the template did not fit the data."""


@dataclass(frozen=True)
class Interval:
    """A half-open span of seconds, [start, end)."""

    start: int
    end: int


@dataclass(frozen=True)
class Incident:
    kind: str
    start: int
    end: int | None


@dataclass(frozen=True)
class Totals:
    """One aggregate over the window; integers are seconds."""

    now: int
    window_start: int
    tenants: int
    accountable_seconds: int
    up_seconds: int
    down_seconds: int
    first_accountable_at: int | None
    incidents: tuple[Incident, ...]
    inputs: dict[str, int]

    @property
    def service_seconds(self) -> int:
        """Time the machine was expected to answer.

        There is no owner-wait subtraction: `accountable_seconds` counts the
        time a machine was `ready`, and a reboot leaves it ready.
        """
        return self.accountable_seconds

    @property
    def uptime_percent(self) -> float | None:
        if self.service_seconds <= 0:
            return None
        return 100.0 * self.up_seconds / self.service_seconds

    def to_dict(self) -> dict:
        return {
            "window": {
                "start": _iso(self.window_start),
                "end": _iso(self.now),
                "days": WINDOW_DAYS,
            },
            "totals": {
                "tenants": self.tenants,
                "accountable_seconds": self.accountable_seconds,
                "downtime_seconds": self.down_seconds,
                "uptime_seconds": self.up_seconds,
                "service_seconds": self.service_seconds,
                "uptime_percent": self.uptime_percent,
                "first_accountable_at": (
                    _iso(self.first_accountable_at) if self.first_accountable_at else None
                ),
            },
            "inputs": dict(self.inputs),
            "incidents": [
                {
                    "kind": incident.kind,
                    "start": _iso(incident.start),
                    "end": _iso(incident.end) if incident.end is not None else None,
                    "seconds": (incident.end if incident.end is not None else self.now)
                    - incident.start,
                }
                for incident in self.incidents
            ],
            "queries": QUERIES,
        }


# ---------------------------------------------------------------------------
# Intervals: clip, merge, intersect, subtract.  Seconds, half-open.
# ---------------------------------------------------------------------------


def _clip(intervals: list[Interval], start: int, end: int) -> list[Interval]:
    clipped: list[Interval] = []
    for interval in intervals:
        left = max(interval.start, start)
        right = min(interval.end, end)
        if right > left:
            clipped.append(Interval(left, right))
    return clipped


def _merge(intervals: list[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for interval in sorted(intervals, key=lambda item: (item.start, item.end)):
        if merged and interval.start <= merged[-1].end:
            last = merged[-1]
            merged[-1] = Interval(last.start, max(last.end, interval.end))
        else:
            merged.append(interval)
    return merged


def _intersect(left: list[Interval], right: list[Interval]) -> list[Interval]:
    """Both lists must be merged and sorted."""
    out: list[Interval] = []
    i = j = 0
    while i < len(left) and j < len(right):
        start = max(left[i].start, right[j].start)
        end = min(left[i].end, right[j].end)
        if end > start:
            out.append(Interval(start, end))
        if left[i].end < right[j].end:
            i += 1
        else:
            j += 1
    return out


def _seconds(intervals: list[Interval]) -> int:
    return sum(interval.end - interval.start for interval in intervals)


# ---------------------------------------------------------------------------
# Time formatting
# ---------------------------------------------------------------------------


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())


def _date(epoch: int) -> str:
    """`8 November 2026`: no ordinal, no comma, as BRAND.md requires."""
    stamp = datetime.fromtimestamp(int(epoch), UTC)
    return f"{stamp.day} {stamp.strftime('%B %Y')}"


def _moment(epoch: int) -> str:
    """An operator time: date, then UTC, because that is the zone we keep."""
    stamp = datetime.fromtimestamp(int(epoch), UTC)
    return f"{_date(epoch)} · {stamp.strftime('%H:%M')} UTC"


def format_percent(value: float | None) -> str:
    """One decimal, and never 100.0% for a month with downtime.

    BRAND.md asks measured percentages to keep one decimal.  A month with a
    single minute of downtime rounds to 100.0% at one decimal, which would
    contradict the downtime line beside it, so a non-perfect figure is capped
    at 99.9%.
    """
    if value is None:
        return "—"
    if value >= 100.0:
        return "100.0%"
    shown = round(value, 1)
    if shown >= 100.0:
        shown = 99.9
    return f"{shown:.1f}%"


def format_minutes(seconds: int) -> str:
    """The downtime unit the page shows, floored and honest.

    Grouped by thousands so "1,440 min" reads as a quantity, not a viewport.
    """
    if seconds <= 0:
        return "0 min"
    minutes = seconds // 60
    return "under 1 min" if minutes == 0 else f"{minutes:,} min"


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0 min"
    days, rest = divmod(int(seconds), 24 * 60 * 60)
    hours, rest = divmod(rest, 60 * 60)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} m"
    if minutes:
        return f"{minutes} min"
    return f"{secs} s"


def format_machine_days(seconds: int) -> str:
    value = seconds / (24 * 60 * 60)
    return f"{value:.1f} machine-days"


# ---------------------------------------------------------------------------
# Reading the history
# ---------------------------------------------------------------------------


def _connect(path: str | Path) -> sqlite3.Connection:
    if not Path(path).is_file():
        raise StatusError(f"no database at {path}; run the health poll first")
    try:
        connection = sqlite3.connect(str(path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        # This program only reads; make an accidental write fail loudly.
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error as error:
        raise StatusError(f"cannot open {path}: {error}") from None
    return connection


def _rows(connection: sqlite3.Connection, query: str, **values) -> list[sqlite3.Row]:
    try:
        return connection.execute(query, values).fetchall()
    except sqlite3.Error as error:
        raise StatusError(
            f"the database has no usable heartbeat history ({error}); run the health poll first"
        ) from None


def _state_segments(
    events: list[sqlite3.Row],
    tenants: list[sqlite3.Row],
    *,
    start: int,
    end: int,
    exclude: frozenset[str] = frozenset(),
) -> dict[str, list[tuple[int, int, str]]]:
    """Per slug, the in-service state that holds over [start, end), in order."""
    by_slug: dict[str, list[tuple[int, str]]] = {}
    for row in events:
        if row["slug"] in exclude:
            continue
        by_slug.setdefault(row["slug"], []).append((_epoch(row["created_at"]), row["to_state"]))
    # A tenant row without events (an older database) still has a state and a
    # creation stamp; treat that as one segment rather than hiding the tenant.
    # A tenant with events must not also get this row, or the current state
    # would be replayed at the creation time and rewrite its own history.
    for row in tenants:
        if row["slug"] in exclude:
            continue
        if row["slug"] not in by_slug:
            by_slug[row["slug"]] = [(_epoch(row["created_at"]), row["state"])]

    segments: dict[str, list[tuple[int, int, str]]] = {}
    for slug, moves in by_slug.items():
        # Sort by time only: the query's id order is the event order, and two
        # transitions written in the same second must keep that order.
        moves.sort(key=lambda item: item[0])
        spans: list[tuple[int, int, str]] = []
        for index, (moment, to_state) in enumerate(moves):
            left = max(moment, start)
            right = moves[index + 1][0] if index + 1 < len(moves) else end
            right = min(right, end)
            if right > left and to_state in SERVICE_STATES:
                spans.append((left, right, to_state))
        if spans:
            segments[slug] = spans
    return segments


def _coverage(rows: list[sqlite3.Row], *, start: int, end: int) -> dict[str, list[Interval]]:
    """Heartbeat coverage: a beat covers the next ten minutes at most."""
    beats: dict[str, list[int]] = {}
    for row in rows:
        beats.setdefault(row["slug"], []).append(int(row["at"]))
    covered: dict[str, list[Interval]] = {}
    for slug, moments in beats.items():
        moments.sort()
        intervals: list[Interval] = []
        for index, moment in enumerate(moments):
            right = moments[index + 1] if index + 1 < len(moments) else end
            intervals.append(Interval(moment, min(right, moment + HEARTBEAT_STOP_SECONDS)))
        clipped = _clip(intervals, start, end)
        if clipped:
            covered[slug] = _merge(clipped)
    return covered


def _incidents(
    rows: list[sqlite3.Row],
    *,
    now: int,
    start: int,
    exclude: frozenset[str] = frozenset(),
) -> tuple[Incident, ...]:
    incidents: list[Incident] = []
    for row in rows:
        if row["slug"] in exclude:
            continue
        first = int(row["first_seen_at"])
        resolved = row["resolved_at"]
        last = int(resolved) if resolved is not None else None
        if last is not None and last < start:
            continue
        incidents.append(Incident(kind=row["kind"], start=first, end=last))
    # The query already orders and limits; sorting again keeps the contract
    # true if the SQL is ever changed in isolation.
    incidents.sort(key=lambda item: item.start, reverse=True)
    return tuple(incidents[:INCIDENT_LIMIT])


def collect(
    path: str | Path = DEFAULT_STATE_DB,
    *,
    now: int | None = None,
    exclude: frozenset[str] = DEMO_SLUGS,
) -> Totals:
    """Compute one aggregate over the trailing 30 days.  No tenant is named.

    `exclude` is the demo machine's tenancy ids (`DEMO_SLUGS` by default): its
    rows are read but never counted, so a silent demo cannot become Odile's
    published downtime and its ledger row is not one of the five incidents.
    """
    moment = int(now if now is not None else datetime.now(tz=UTC).timestamp())
    start = moment - WINDOW_SECONDS
    connection = _connect(path)
    try:
        tenants = _rows(connection, QUERIES["tenants"])
        events = _rows(
            connection, QUERIES["state_segments"], now=_iso(moment)
        )
        beats = _rows(
            connection, QUERIES["heartbeats"], since=start - HEARTBEAT_STOP_SECONDS, now=moment
        )
        incident_rows = _rows(
            connection, QUERIES["incidents"], since=start
        )
    finally:
        connection.close()

    segments = _state_segments(events, tenants, start=start, end=moment, exclude=exclude)
    coverage = _coverage(beats, start=start, end=moment)

    accountable_total = 0
    up_total = 0
    first_seen: int | None = None
    counted_tenants = 0

    for slug, spans in segments.items():
        accountable = _merge([Interval(left, right) for left, right, _state in spans])
        up = _intersect(coverage.get(slug, []), accountable)
        accountable_total += _seconds(accountable)
        up_total += _seconds(up)
        if accountable:
            counted_tenants += 1
            first_seen = (
                accountable[0].start
                if first_seen is None
                else min(accountable[0].start, first_seen)
            )

    downtime = max(0, accountable_total - up_total)
    return Totals(
        now=moment,
        window_start=start,
        tenants=counted_tenants,
        accountable_seconds=accountable_total,
        up_seconds=up_total,
        down_seconds=downtime,
        first_accountable_at=first_seen,
        incidents=_incidents(incident_rows, now=moment, start=start, exclude=exclude),
        inputs={
            "tenants": len(tenants),
            "excluded_tenants": sum(1 for row in tenants if row["slug"] in exclude),
            "state_events": len(events),
            "heartbeats": len(beats),
            "incident_rows": len(incident_rows),
        },
    )


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def _incident_rows(incidents: tuple[Incident, ...], *, now: int) -> str:
    if not incidents:
        return '<tr><td colspan="3">No incidents in the last 30 days.</td></tr>'
    rows: list[str] = []
    for incident in incidents:
        end = incident.end if incident.end is not None else now
        note = "The heartbeat stopped"
        duration = format_duration(end - incident.start)
        if incident.end is None:
            duration = f"{duration}, ongoing"
        rows.append(
            "<tr>"
            f'<td class="when">{html.escape(_moment(incident.start))}</td>'
            f"<td>{html.escape(note)}</td>"
            f'<td class="dur">{html.escape(duration)}</td>'
            "</tr>"
        )
    return "\n        ".join(rows)


def _coverage_note(totals: Totals) -> str:
    if totals.first_accountable_at is None or totals.first_accountable_at <= totals.window_start:
        return ""
    complete = totals.first_accountable_at + WINDOW_SECONDS
    return (
        '<p class="note">A full 30-day window completes on '
        f"{html.escape(_date(complete))}.</p>"
    )


def _coverage_line(totals: Totals) -> str:
    if totals.first_accountable_at is None:
        return "No machine has been in service in the last 30 days."
    since = max(totals.first_accountable_at, totals.window_start)
    machines = "machine" if totals.tenants == 1 else "machines"
    return f"{totals.tenants} {machines} · measured since {_date(since)}"


def render(template: str, totals: Totals) -> str:
    """Fill every slot, refuse a template that does not match the data."""
    slots = set(SLOT_RE.findall(template))
    values = {
        "UPTIME": format_percent(totals.uptime_percent),
        "COVERAGE": _coverage_line(totals),
        "COVERAGE_NOTE": _coverage_note(totals),
        "DOWN": format_minutes(totals.down_seconds),
        "MACHINE_TIME": format_machine_days(totals.accountable_seconds),
        "GENERATED_AT": _moment(totals.now),
        "INCIDENT_ROWS": _incident_rows(totals.incidents, now=totals.now),
    }
    missing = slots - set(values)
    if missing:
        raise StatusError(f"the template has slots with no value: {', '.join(sorted(missing))}")
    unknown = set(values) - slots
    if unknown:
        raise StatusError(
            f"the template lost slots the page needs: {', '.join(sorted(unknown))}"
        )
    return SLOT_RE.sub(lambda match: values[match.group(1)], template)


def build(
    path: str | Path = DEFAULT_STATE_DB,
    *,
    now: int | None = None,
    template_path: str | Path = TEMPLATE_PATH,
) -> tuple[str, Totals]:
    try:
        template = Path(template_path).read_text(encoding="utf-8")
    except OSError as error:
        raise StatusError(f"cannot read the template {template_path}: {error}") from None
    totals = collect(path, now=now)
    return render(template, totals), totals


def _write_atomic(path: Path, body: str) -> None:
    """A reader of the live page never sees half a file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=str(path.parent), text=True
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
    except OSError as error:
        raise StatusError(f"cannot write {path}: {error}") from None


def _parse_now(value: str) -> int:
    if value.isdigit():
        return int(value)
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise StatusError(f"--now {value!r} is not an ISO timestamp or a Unix epoch") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the public status page from heartbeat history."
    )
    parser.add_argument("--db", default=DEFAULT_STATE_DB, help="the HQ SQLite database")
    parser.add_argument("--template", default=str(TEMPLATE_PATH), help="the HTML template")
    parser.add_argument("--out", default=DEFAULT_OUT, help="where to write the page")
    parser.add_argument("--now", help="pin the clock (ISO 8601 or Unix epoch)")
    parser.add_argument("--json", action="store_true", help="print the numbers and their queries")
    parser.add_argument("--stdout", action="store_true", help="render the page to stdout; write nothing")
    args = parser.parse_args(argv)

    moment = _parse_now(args.now) if args.now else None
    try:
        if args.json:
            totals = collect(args.db, now=moment)
            print(json.dumps(totals.to_dict(), indent=2, sort_keys=True))
            return 0
        body, totals = build(args.db, now=moment, template_path=args.template)
        if args.stdout:
            sys.stdout.write(body)
            return 0
        _write_atomic(Path(args.out), body)
    except StatusError as error:
        print(f"status: {error}", file=sys.stderr)
        return 2
    print(
        f"status: wrote {args.out} ({format_percent(totals.uptime_percent)} uptime,"
        f" {totals.tenants} machine(s), {len(totals.incidents)} incident(s))"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
