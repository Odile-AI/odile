"""`reconcile` — compare the local tenant rows to the provider, report drift.

Read-only by default: the comparison still runs and every finding is printed,
but nothing is written. `--apply` repairs only the differences the database can
answer on its own — a tenant whose machine the provider no longer lists is
marked destroyed, and an expired ready machine is suspended. A provider
machine with no tenant row is reported for a human to adopt or destroy; this
command never creates, terminates or powers a machine.

The state machine in `state.py` owns the transitions and the event log; this
module owns the argument surface and the two renderings of one report.
"""

from __future__ import annotations

import json
from argparse import ArgumentParser

from .core import Context, Report
from .state import DEFAULT_STATE_DB, StateStore, reconcile as compare_drift

NAME = "reconcile"
HELP = "Compare tenant rows to the provider and report drift."
EXAMPLE = "odilectl reconcile --apply"


def add_arguments(parser: ArgumentParser) -> None:
    parser.add_argument(
        "slug",
        nargs="?",
        help="one tenant slug; omit to reconcile every tenant",
    )


def _line(finding) -> str:
    detail = ", ".join(
        f"{key}={json.dumps(value, sort_keys=True)}"
        for key, value in sorted(finding.detail.items())
        if value is not None
    )
    suffix = f" ({detail})" if detail else ""
    return f"  {finding.kind}: {finding.slug}{suffix} fix={finding.fix}"


def _lines(report, applied: bool) -> list[str]:
    lines = [
        f"reconcile: {report.tenants} tenant(s), {report.machines} machine(s)",
    ]
    if not report.drift:
        lines.append("reconcile: no drift")
        return lines
    lines.extend(_line(finding) for finding in report.drift)
    manual = sum(1 for finding in report.drift if finding.fix == "manual")
    if applied:
        lines.append(
            f"reconcile: applied; fixed {len(report.fixed)}, {manual} need a human"
        )
    else:
        fixable = len(report.drift) - manual
        lines.append(
            f"reconcile: {len(report.drift)} drift(s); pass --apply to fix {fixable}"
        )
    return lines


def run(ctx: Context, args) -> Report:
    db_path = ctx.config.path_value("state_db", DEFAULT_STATE_DB) or DEFAULT_STATE_DB
    store = StateStore.open(db_path)
    try:
        report = compare_drift(store, ctx.provider, apply=ctx.mutate, slug=args.slug)
    finally:
        store.close()

    payload = {
        "ok": True,
        "command": NAME,
        "dry_run": not ctx.mutate,
        "slug": args.slug,
    }
    payload.update(report.to_dict())
    return Report(payload, _lines(report, ctx.mutate))
