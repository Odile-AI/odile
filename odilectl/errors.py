"""The errors `odilectl` maps to exit codes.

One module so `cli.py` can turn an error into an exit code without importing
the provider package, and commands can raise without importing the CLI. The
codes are part of the command's contract: a script that sees 3 knows the
command exists but its backend has not landed, not that the provider failed.
"""

from __future__ import annotations


class OdileError(Exception):
    """The base class for every error `odilectl` reports without a traceback."""

    exit_code = 1


class UsageError(OdileError):
    """The arguments are wrong in a way argparse cannot see."""

    exit_code = 2


class NotWired(OdileError):
    """The command is part of the surface; the card that fills it in has not merged.

    ``card`` names the backlog card, so the reader knows where the work lives
    instead of reading a dead end.
    """

    exit_code = 3

    def __init__(self, message: str, card: str):
        super().__init__(message)
        self.card = card

    def __str__(self) -> str:
        return f"{super().__str__()} (not wired yet: {self.card})"


class ProviderError(OdileError):
    """The provider refused the call, or answered something we cannot parse."""

    exit_code = 4

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ProviderTimeout(ProviderError):
    """The provider never reached a usable state inside the deadline."""


class ConfigError(OdileError):
    """The config file is missing, unsafe, or does not carry a required key."""

    exit_code = 5
