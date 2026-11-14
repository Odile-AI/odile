"""`rotate-token` — end the tenant's live setup tokens and mint one new one.

The token ledger is the seed service's (`seed/seed.py token rotate`, ODL-204).
Nothing here touches a tenant: the seed service holds the hash, and the machine
redeems a token at install time. The odilectl wrapper waits for the tenant
record it looks the slug up in (ODL-212).
"""

from __future__ import annotations

from argparse import ArgumentParser

from .core import Context, Report, plan
from .errors import NotWired, UsageError

NAME = "rotate-token"
HELP = "End every live setup token for a tenant and mint one new one."
EXAMPLE = "odilectl rotate-token odl-7f3a2c --apply"


def add_arguments(parser: ArgumentParser) -> None:
    parser.add_argument("slug", help="the tenant's slug")
    parser.add_argument(
        "--ttl-hours",
        type=int,
        default=24,
        metavar="N",
        help="how long the new token lives (default: 24)",
    )


def run(ctx: Context, args) -> Report:
    if args.ttl_hours <= 0:
        raise UsageError("rotate-token: --ttl-hours must be positive")
    actions = [
        {
            "action": "seed.token.rotate",
            "slug": args.slug,
            "ttl_hours": args.ttl_hours,
            "via": "seed/seed.py token rotate",
        }
    ]
    notes = ["the seed ledger is ODL-204; the odilectl wrapper needs the tenant record (ODL-212)"]
    if ctx.mutate:
        raise NotWired("rotate-token --apply needs the tenant record and the seed ledger", "ODL-212")
    return plan(NAME, ctx, actions, notes, slug=args.slug, ttl_hours=args.ttl_hours)
