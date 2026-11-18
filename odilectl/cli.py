"""odilectl — the one command line for the Odile control plane.

    python3 -m odilectl --help
    python3 -m odilectl provision odl-7f3a2c --key ~/.ssh/ada.pub --apply
    python3 -m odilectl status --json

Every command accepts `--dry-run` and `--json`, before or after the command
name. A mutating command changes nothing unless `--apply` is passed, and
`--dry-run` wins over `--apply`; `odilectl provision` with no flags prints the
order it would place, which is the safe way to read the plan.

No command opens a shell on a tenant. The only network calls are the provider's
HTTPS API, the health poll (each tenant's `/health` and a port-22 host-key
handshake), and the alert transports the operator enables.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from typing import Callable, Sequence, TextIO

from . import (
    bootrecord,
    destroy,
    dns,
    export,
    health,
    invite,
    provision,
    purge,
    reconcile,
    restore,
    rotate_token,
    status,
    support,
)
from .config import DEFAULT_CONFIG_PATH, ENV_CONFIG_PATH, Config
from .core import Context
from .errors import NotWired, OdileError, UsageError
from .providers.base import Provider

__version__ = "0.1.0"

# The order is the order in `docs/MVP-V0.1.md` and the card; help follows it.
MODULES = (
    invite,
    provision,
    status,
    health,
    destroy,
    purge,
    rotate_token,
    reconcile,
    restore,
    support,
    export,
    bootrecord,
    dns,
)
BY_NAME = {module.NAME: module for module in MODULES}


def _add_flags(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    """Add the flags that are valid on either side of the command name.

    The subparser is a second parser with its own namespace, so the flags are
    added twice: once with the real defaults on the top-level parser, and once
    with ``SUPPRESS`` defaults on each child. A flag written before the command
    therefore survives the child's parse, and a flag written after it lands in
    the child's namespace. Defining the actions separately (rather than through
    a shared ``parents=[...]`` object) matters: `set_defaults` mutates the
    action objects, and a shared object would let the child's default erase a
    value the top-level parser already saw.
    """
    default: object = argparse.SUPPRESS if suppress else False
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=default,
        help="print the plan and change nothing (wins over --apply)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=default,
        help="execute a mutating command (without it every write is a dry run)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=default,
        help="print one JSON object, including on an error (usage errors too)",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=argparse.SUPPRESS if suppress else None,
        help=f"config file, mode 0600 (default: {DEFAULT_CONFIG_PATH})",
    )


def _epilog() -> str:
    width = max(len(module.NAME) for module in MODULES)
    examples = "\n".join(
        f"  {module.NAME:<{width}}  {module.EXAMPLE}" for module in MODULES
    )
    return (
        "commands and one example each:\n"
        f"{examples}\n"
        "\n"
        "Every command accepts --dry-run and --json, before or after the command.\n"
        "Mutating commands change nothing unless --apply is passed; --dry-run wins.\n"
        f"Config: {DEFAULT_CONFIG_PATH} (0600), or --config PATH / ${ENV_CONFIG_PATH}.\n"
        "Exit codes: 0 ok, 2 usage, 3 not wired yet, 4 provider (or a repository "
        "restic cannot open, `purge`), 5 config."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="odilectl",
        description="Odile's control plane: one CLI, one provider implementation.",
        epilog=_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_flags(parser)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    for module in MODULES:
        child = subparsers.add_parser(
            module.NAME,
            help=module.HELP,
            description=f"{module.HELP}\n\nExample: {module.EXAMPLE}",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        _add_flags(child, suppress=True)
        module.add_arguments(child)
    return parser


def _wants_json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _tokens(argv: Sequence[str] | None) -> list[str]:
    return list(sys.argv[1:] if argv is None else argv)


def _json_requested(tokens: Sequence[str]) -> bool:
    """Find `--json` before argparse runs, because a usage error exits early.

    argparse permits an unambiguous prefix (`--j`), so the prefix form counts
    too; `--json` is the only long option starting with `--j` in every parser.
    """
    for token in tokens:
        if token == "--json":
            return True
        if token.startswith("--j") and "--json".startswith(token):
            return True
    return False


def _command_from_tokens(tokens: Sequence[str]) -> str | None:
    for token in tokens:
        if token in BY_NAME:
            return token
    return None


def _dry_run_from_tokens(tokens: Sequence[str]) -> bool:
    return not ("--apply" in tokens and "--dry-run" not in tokens)


def _usage_message(text: str) -> str:
    """The one-line reason out of argparse's usage block, without the banner."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    for line in reversed(lines):
        if "error:" in line:
            return line.split("error:", 1)[1].strip()
    return lines[-1] if lines else "usage error"


def _emit_error(args: argparse.Namespace, out: TextIO, err: TextIO, error: OdileError) -> None:
    if _wants_json(args):
        payload = {
            "ok": False,
            "command": getattr(args, "command", None),
            "dry_run": not (
                getattr(args, "apply", False) and not getattr(args, "dry_run", False)
            ),
            "error": str(error),
        }
        if isinstance(error, NotWired):
            payload["not_wired"] = error.card
        print(json.dumps(payload, indent=2, sort_keys=True), file=out)
        return
    where = f"odilectl {getattr(args, 'command', '')}".strip()
    print(f"{where}: {error}", file=err)


def main(
    argv: Sequence[str] | None = None,
    *,
    provider_factory: Callable[[Config], Provider] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Parse, run one command, print one result. Return the process exit code.

    `provider_factory` and the two streams are the test seams; production uses
    `providers.build`, `sys.stdout` and `sys.stderr`. When `--json` is present
    a usage error is one JSON object on stdout, so a script never has to parse
    argparse's prose; without it argparse behaves as usual.
    """
    tokens = _tokens(argv)
    parser = build_parser()
    if _json_requested(tokens):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            try:
                args = parser.parse_args(argv)
            except SystemExit as exit_:
                if exit_.code == 0:
                    raise
                out = stdout if stdout is not None else sys.stdout
                payload = {
                    "ok": False,
                    "command": _command_from_tokens(tokens),
                    "dry_run": _dry_run_from_tokens(tokens),
                    "error": _usage_message(captured.getvalue()),
                }
                print(json.dumps(payload, indent=2, sort_keys=True), file=out)
                return 2
    else:
        args = parser.parse_args(argv)
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    config_path = args.config or os.environ.get(ENV_CONFIG_PATH) or DEFAULT_CONFIG_PATH

    try:
        config = Config.load(config_path)
        ctx = Context(
            config=config,
            dry_run=args.dry_run,
            apply=args.apply,
            json=args.json,
            stdout=out,
            stderr=err,
        )
        if provider_factory is not None:
            ctx.provider_factory = provider_factory
        report = BY_NAME[args.command].run(ctx, args)
    except OdileError as error:
        _emit_error(args, out, err, error)
        return error.exit_code
    except ValueError as error:
        # CreateSpec and validate_ssh_public_key speak ValueError; a command
        # that forgot to wrap one still gets a usage error, not a traceback.
        usage = UsageError(str(error))
        _emit_error(args, out, err, usage)
        return usage.exit_code
    except KeyboardInterrupt:
        print("odilectl: interrupted", file=err)
        return 130

    if args.json:
        print(json.dumps(report.payload, indent=2, sort_keys=True), file=out)
    else:
        print("\n".join(report.lines), file=out)
    return 0
