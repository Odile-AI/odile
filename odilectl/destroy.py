"""`destroy` — terminate the tenant's machine and leave nothing behind.

The machine is found by its hostname, which is the slug we set at provision.
The command refuses to guess when two machines answer to one name, and treats
"already gone" as success: the first support instruction is "run it again".

This module is the command's surface; `offboard.py` is the path it runs — the
bundle check, the grant check, the DNS record, the machine, the row and the
backup's retention date, in that order, each one a line of the plan.
"""

from __future__ import annotations

from argparse import ArgumentParser

from . import offboard
from .core import Context, Report

NAME = "destroy"
HELP = "Offboard a tenant: terminate its machine, delete its DNS record, keep the backup."
EXAMPLE = "odilectl destroy odl-7f3a2c --export ~/odl-7f3a2c.tar.gz --apply"


def add_arguments(parser: ArgumentParser) -> None:
    parser.add_argument("slug", help="the tenant's slug (the provider hostname)")
    parser.add_argument(
        "--export",
        metavar="PATH",
        help="the bundle the user downloaded; verified and recorded before anything is destroyed",
    )
    parser.add_argument(
        "--skip-export",
        action="store_true",
        help="destroy without a bundle: only for a tenant that never held data",
    )
    parser.add_argument(
        "--reason",
        metavar="TEXT",
        help="why the tenant is leaving; recorded on the destroyed row",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="destroy although the tenant reports a live support grant",
    )
    parser.add_argument(
        "--wait",
        type=float,
        metavar="SECONDS",
        help=f"how long to wait for the machine to leave the account (default: {offboard.DEFAULT_WAIT:g})",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help="tenant state database (default: the config's state_db)",
    )


def run(ctx: Context, args) -> Report:
    return offboard.execute(ctx, args, name=NAME)
