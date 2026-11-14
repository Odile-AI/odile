"""`support` — grant, revoke or report the time-boxed support access.

Default is no grant. A grant installs Odile's support public key behind a
forced command for a fixed number of hours and logs every use; it expires on
its own. This is the only way an operator gets a shell, and the user starts it.
"""

from __future__ import annotations

from argparse import ArgumentParser

from .core import Context, Report, plan
from .errors import NotWired, UsageError

NAME = "support"
HELP = "Grant, revoke or report the time-boxed support access."
EXAMPLE = "odilectl support odl-7f3a2c --grant 24 --apply"


def add_arguments(parser: ArgumentParser) -> None:
    parser.add_argument("slug", help="the tenant's slug")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--grant",
        type=int,
        nargs="?",
        const=24,
        metavar="HOURS",
        help="create a grant for HOURS (default: 24)",
    )
    mode.add_argument("--revoke", action="store_true", help="end the live grant now")


def run(ctx: Context, args) -> Report:
    if args.grant is not None and args.grant <= 0:
        raise UsageError("support: --grant hours must be positive")
    if args.revoke:
        action = {"action": "support.revoke", "slug": args.slug}
        human = "revoke the live support grant"
    elif args.grant is not None:
        action = {"action": "support.grant", "slug": args.slug, "hours": args.grant}
        human = f"create a {args.grant}-hour support grant"
    else:
        action = {"action": "support.status", "slug": args.slug}
        human = "report the live support grant"
    notes = [
        "the grant, its forced command, its log and its expiry land with ODL-217",
    ]
    if ctx.mutate:
        raise NotWired(f"support --apply needs the grant implementation to {human}", "ODL-217")
    return plan(NAME, ctx, [action], notes, slug=args.slug)
