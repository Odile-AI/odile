"""`invite` — a tenant row and a one-time claim link.

The slug is random and carries no part of the person's name: `odl-7f3a2c`.
The row itself (and the claim token's ledger) belongs to the tenant state card;
until it lands, the command plans the write and says so.
"""

from __future__ import annotations

import re
import secrets
from argparse import ArgumentParser

from .core import Context, Report, plan
from .errors import NotWired, UsageError

NAME = "invite"
HELP = "Create a tenant row and a one-time claim link."
EXAMPLE = "odilectl invite ada@example.com --apply"

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def add_arguments(parser: ArgumentParser) -> None:
    parser.add_argument("email", help="the person's email address")
    parser.add_argument(
        "--ttl-hours",
        type=int,
        default=168,
        metavar="N",
        help="how long the claim link stays live (default: 168, one week)",
    )


def run(ctx: Context, args) -> Report:
    email = args.email.strip()
    if not _EMAIL.match(email):
        raise UsageError(f"invite: {args.email!r} is not an email address")
    if args.ttl_hours <= 0:
        raise UsageError("invite: --ttl-hours must be positive")
    slug = "odl-" + secrets.token_hex(3)
    actions = [
        {"action": "tenant.create", "email": email, "slug": slug, "state": "invited"},
        {"action": "claim.issue", "slug": slug, "ttl_hours": args.ttl_hours},
    ]
    notes = [
        "the tenant row and the claim token land with ODL-212; the claim page with ODL-231",
    ]
    if ctx.mutate:
        raise NotWired("invite --apply needs the tenant state store", "ODL-212")
    return plan(NAME, ctx, actions, notes, email=email, slug=slug)
