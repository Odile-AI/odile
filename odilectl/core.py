"""The small pieces every command shares: context, report, plan rendering.

A command is a module with `NAME`, `HELP`, `EXAMPLE`, `add_arguments()` and
`run(ctx, args)`. It receives a `Context`, asks the provider or the config for
what it needs, and returns a `Report`: one JSON payload and the human lines for
the same facts. `cli.py` owns the printing; a command never writes to stdout.

The provider is built on first use, so `--help` and every dry run work with no
reseller key on disk.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, TextIO

from .config import Config
from .providers import build as build_provider
from .providers.base import Provider


@dataclass
class Context:
    """One invocation: config, flags, output streams, and the lazy provider."""

    config: Config
    dry_run: bool = False
    apply: bool = False
    json: bool = False
    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    stderr: TextIO = field(default_factory=lambda: sys.stderr)
    provider_factory: Callable[[Config], Provider] = build_provider
    _provider: Provider | None = field(default=None, repr=False)

    @property
    def mutate(self) -> bool:
        """True only when the command should change the world.

        A mutating command changes nothing unless `--apply` is passed, and an
        explicit `--dry-run` wins over `--apply`. That makes every write safe
        to type twice and safe to preview.
        """
        return self.apply and not self.dry_run

    @property
    def provider(self) -> Provider:
        if self._provider is None:
            self._provider = self.provider_factory(self.config)
        return self._provider


@dataclass
class Report:
    """One command's result: a JSON object and the same facts as lines."""

    payload: dict[str, Any]
    lines: list[str]


def result(name: str, ctx: Context, lines: list[str], **fields: Any) -> Report:
    """A successful read-only report (`status`)."""
    payload: dict[str, Any] = {"ok": True, "command": name, "dry_run": ctx.dry_run}
    payload.update(fields)
    return Report(payload, lines)


def plan(name: str, ctx: Context, actions: list[dict], notes: list[str], **fields: Any) -> Report:
    """A dry-run plan: numbered actions, then the cards that fill any gap.

    Every caller of `plan()` is on a path that changes nothing, so `dry_run`
    is not the `--dry-run` flag: it is "this invocation mutated nothing", which
    is also true of the default no-flag invocation. A JSON consumer that reads
    `dry_run: false` may safely assume an applied command.
    """
    payload: dict[str, Any] = {
        "ok": True,
        "command": name,
        "dry_run": not ctx.mutate,
        "actions": actions,
        "notes": notes,
    }
    payload.update(fields)
    lines = [f"{name}: dry run (pass --apply to execute)"]
    for index, action in enumerate(actions, 1):
        kind = action.get("action", "action")
        rest = ", ".join(
            f"{key}={json.dumps(value, sort_keys=True)}"
            for key, value in action.items()
            if key != "action"
        )
        lines.append(f"  {index}. {kind}: {rest}" if rest else f"  {index}. {kind}")
    for note in notes:
        lines.append(f"note: {note}")
    return Report(payload, lines)


def applied(name: str, ctx: Context, lines: list[str], head: str | None = None, **fields: Any) -> Report:
    """A report for a command that actually ran; the first line says so."""
    payload: dict[str, Any] = {"ok": True, "command": name, "dry_run": False}
    payload.update(fields)
    return Report(payload, [head or f"{name}: applied", *lines])


def render_datetime(epoch: int | None) -> str:
    """Unix seconds as a UTC date; the API speaks seconds, humans read days."""
    if epoch is None:
        return "-"
    import datetime as dt

    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%d")
