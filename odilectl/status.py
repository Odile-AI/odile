"""`status` — what the provider says about the account's machines.

Read-only, and the one place an operator sees the release pair side by side:
the release recorded on the tenant row (the pin's `provision_release`, written
at provision) and the release the latest heartbeat says the guest booted. The
comparison is informational — `same`, `release_drift` or `unknown`; `status`
never repairs it.

It also prints the owner's last brief from the same heartbeat, so a machine
whose brief job has stopped sending is visible here before the health poll
raises `brief_missing`.
"""

from __future__ import annotations

import time
from argparse import ArgumentParser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import Context, Report, render_datetime, result
from .health import Heartbeat, HeartbeatStore, release_report
from .providers.base import Machine
from .state import DEFAULT_STATE_DB, StateStore, UnknownTenant

NAME = "status"
HELP = "List the account's machines, or one tenant's machine."
EXAMPLE = "odilectl status odl-7f3a2c --json"


def add_arguments(parser: ArgumentParser) -> None:
    parser.add_argument(
        "slug",
        nargs="?",
        help="a tenant slug (the provider hostname); omit to list every machine",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help=f"tenant state database (default: the config's state_db, {DEFAULT_STATE_DB})",
    )
    parser.add_argument(
        "--heartbeat-db",
        metavar="PATH",
        help="heartbeat database (default: the same file as --db)",
    )


def _power_label(machine: Machine) -> str:
    """The provider's power fact as a word: `off` is not `active`."""
    value = machine.detail.get("powered_on")
    if value is True:
        return "on"
    if value is False:
        return "off"
    return "unknown"


def _row(machine: Machine) -> str:
    return (
        f"  {machine.server_id:<10} {machine.hostname:<24} {machine.state:<10} "
        f"{machine.ipv4 or '-':<16} {_power_label(machine):<7} "
        f"{render_datetime(machine.expires_at)}"
    )


def _release_line(report: dict) -> str:
    return (
        f"  release:   intended {report['intended'] or 'not recorded'}, "
        f"booted {report['booted'] or 'no report'} ({report['status']})"
    )


def _duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def backup_report(beat: Heartbeat | None) -> dict:
    """The backup facts a heartbeat carries: age, snapshot, repo, size.

    HQ stores only what the guest chooses to send — the repository URL, the
    snapshot id, the size and the last status. No password, no file list.
    """
    facts = beat.facts if beat is not None else {}
    raw = facts.get("backup")
    record: dict = raw if isinstance(raw, dict) else {}
    report: dict = {
        "last_backup_at": None,
        "age_seconds": None,
        "snapshot": record.get("snapshot"),
        "repo": record.get("repo"),
        "repo_bytes": record.get("repo_bytes"),
        "backup_status": record.get("status"),
        "status": "no_heartbeat" if beat is None else "no_backup",
    }
    last = facts.get("last_backup_at")
    if isinstance(last, int) and not isinstance(last, bool):
        report["last_backup_at"] = last
        report["age_seconds"] = max(0, int(time.time()) - last)
        report["status"] = "reported"
    return report


def _backup_line(report: dict) -> str:
    if report["status"] == "no_heartbeat":
        return "  backup:    no heartbeat yet"
    if report["status"] == "no_backup":
        return "  backup:    the last heartbeat carried no backup"
    bits = [
        f"last {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(report['last_backup_at']))}",
        f"({_duration(report['age_seconds'])} ago)",
    ]
    if report.get("snapshot"):
        bits.append(f"snapshot {str(report['snapshot'])[:8]}")
    if report.get("repo_bytes"):
        bits.append(f"{report['repo_bytes']} bytes in the repository")
    if report.get("repo"):
        bits.append(report["repo"])
    return "  backup:    " + ", ".join(bits)


def brief_fact(beat: Heartbeat | None) -> dict[str, Any]:
    """The last brief the guest reported, as seconds and an ISO instant."""
    value = beat.facts.get("last_brief_at") if beat is not None else None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return {"last_sent_at": None, "last_sent_at_iso": None}
    moment = datetime.fromtimestamp(value, timezone.utc)
    return {
        "last_sent_at": value,
        "last_sent_at_iso": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _brief_line(brief: dict[str, Any]) -> str:
    sent = brief.get("last_sent_at")
    if not isinstance(sent, int):
        return "  brief:     none recorded"
    moment = datetime.fromtimestamp(sent, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"  brief:     last sent {moment}"


def run(ctx: Context, args) -> Report:
    machines = ctx.provider.list()
    if args.slug:
        machines = [machine for machine in machines if machine.hostname == args.slug]
    header = (
        f"status: {len(machines)} machine(s) for {args.slug}"
        if args.slug
        else f"status: {len(machines)} machine(s) at {ctx.provider.name}"
    )

    state_db = Path(args.db).expanduser() if args.db else (
        ctx.config.path_value("state_db", DEFAULT_STATE_DB) or Path(DEFAULT_STATE_DB)
    )
    heartbeat_db = Path(args.heartbeat_db).expanduser() if args.heartbeat_db else (
        ctx.config.path_value("heartbeat_db", state_db) or state_db
    )
    store = StateStore.open(state_db)
    beats = HeartbeatStore.open(heartbeat_db)
    try:
        rows = []
        for machine in machines:
            try:
                intended = store.get(machine.hostname).release
            except UnknownTenant:
                intended = None
            beat = beats.latest(machine.hostname)
            report = release_report(intended, beat)
            backup = backup_report(beat)
            brief = brief_fact(beat)
            entry = machine.to_dict()
            entry["release"] = report
            entry["backup"] = backup
            entry["brief"] = brief
            rows.append((machine, report, backup, brief, entry))
    finally:
        beats.close()
        store.close()

    lines = [header]
    if rows:
        lines.append(
            f"  {'SERVER':<10} {'HOSTNAME':<24} {'STATE':<10} {'IPV4':<16} "
            f"{'POWER':<7} EXPIRES"
        )
        for machine, report, backup, brief, _entry in rows:
            lines.append(_row(machine))
            lines.append(_release_line(report))
            lines.append(_backup_line(backup))
            lines.append(_brief_line(brief))
    elif args.slug:
        lines.append(f"  no machine named {args.slug}")
    lines.append("note: release drift is reported, never repaired")
    return result(
        NAME,
        ctx,
        lines,
        machines=[entry for _machine, _report, _backup, _brief, entry in rows],
        count=len(rows),
        slug=args.slug,
        state_db=str(state_db),
        heartbeat_db=str(heartbeat_db),
    )
