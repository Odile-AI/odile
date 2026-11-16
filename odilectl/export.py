"""`export` — package a tenant's data for offboarding.

The user runs `odile-export` on the guest; HQ never reads the files. This
command is the two ends HQ does own:

* `--script` prints the guest program byte-for-byte, so an operator can compare
  it with the copy the installer placed (`/usr/local/bin/odile-export`);
* `--verify PATH` inspects the bundle the user downloaded — its SHA-256, size,
  entry count and members — and is the same check `destroy` refuses without.

The default invocation is a dry run: what the bundle will contain and how to
get it off the machine.
"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

from . import offboard
from .core import Context, Report, plan, result
from .errors import UsageError

NAME = "export"
HELP = "Package a tenant's home, memory, skills and sessions for offboarding."
EXAMPLE = "odilectl export odl-7f3a2c --verify ~/odile-export-odl-7f3a2c.tar.gz"


def add_arguments(parser: ArgumentParser) -> None:
    parser.add_argument("slug", help="the tenant's slug")
    parser.add_argument(
        "--script",
        action="store_true",
        help="print the guest program `odile-export` and change nothing",
    )
    parser.add_argument(
        "--verify",
        metavar="PATH",
        help="inspect the bundle the user downloaded: size, sha256, entries and members",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help="where the guest writes the bundle (default: the tenant's home)",
    )


def run(ctx: Context, args) -> Report:
    if args.script:
        payload = {
            "ok": True,
            "command": NAME,
            "dry_run": True,
            "slug": args.slug,
            "program": "odile-export",
            "script": offboard.EXPORT_PROGRAM,
        }
        return Report(payload, [offboard.EXPORT_PROGRAM.rstrip("\n")])

    if args.verify:
        bundle = offboard.inspect_bundle(Path(args.verify))
        lines = [f"export: {args.slug}", *offboard.bundle_lines(bundle, listing=True)]
        if not bundle.complete:
            raise UsageError(
                f"export: {bundle.path.name} is missing "
                + ", ".join(bundle.missing)
                + "; export again on the guest"
            )
        lines.append("  verdict:   complete; destroy --export accepts this file")
        return result(NAME, ctx, lines, slug=args.slug, export=bundle.to_dict())

    if ctx.mutate:
        raise UsageError(
            "export --apply needs --verify PATH: the export runs on the guest, "
            "so HQ verifies the bundle the user downloaded"
        )

    out = Path(args.out).expanduser() if args.out else Path("<the tenant's home>")
    actions = [
        {
            "action": "guest.export",
            "slug": args.slug,
            "program": "odile-export",
            "contains": ["AGENTS.md", ".hermes/memories", ".hermes/skills", ".hermes/state.db", "workspace"],
            "out": str(out),
            "download": "the Webapp's Files pane",
        },
        {
            "action": "bundle.verify",
            "slug": args.slug,
            "when": "after the download, on the operator's copy",
            "command": f"odilectl export {args.slug} --verify PATH",
        },
    ]
    notes = [
        "the bundle is written on the machine by the user's own odile-export; "
        "HQ never reads the files",
        "`odilectl export <slug> --script` prints the guest program",
        f"destroy needs the verified bundle: --export PATH (and refuses without it)",
    ]
    return plan(NAME, ctx, actions, notes, slug=args.slug, out=str(out))
