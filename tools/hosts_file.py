#!/usr/bin/python3
"""hosts_file — the operator's private host values, one key per line.

`deploy/hosts.example` is the committed template: every key the runbooks need,
each value a placeholder from the vocabulary `tools/leak_scan.py` accepts. The
live file is `deploy/hosts.local` (gitignored) or the path in `ODILE_HOSTS`;
the real values come from the secret store (SOPS/age, 1Password, Vault) and
never from git.

The parser is deliberately strict. An unknown key, a duplicate key, a missing
key, a line that is not ``KEY=VALUE``, or (for the template) a value that is
not a documented placeholder fails and names the line: a silent skip is how a
deploy runs against the wrong machine.

    tools/hosts_file.py --example        # the template: every key, placeholder
    tools/hosts_file.py --path FILE      # a live file: every key present
    tools/hosts_file.py                  # deploy/hosts.local, else the template
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "deploy" / "hosts.example"
LOCAL = REPO / "deploy" / "hosts.local"
ENV_PIN = "ODILE_HOSTS"

# One key per label in the deny list, plus the UKI test machine the Makefile
# reads. Keep this tuple and the example in step; KEYS is the contract.
KEYS = (
    "SERVURY_BOX_ID",
    "SERVURY_ACCOUNT_DEV",
    "SERVURY_ACCOUNT_HOST",
    "SERVURY_ACCOUNT",
    "HQ_ADDRESS",
    "DEMO_ADDRESS",
    "DEMO_TENANT_ADDRESS",
    "PROVIDER_IPV6",
    "PERSONAL_MAILBOX",
    "CHECKOUT_PATH",
    "TENANT_INSTANCE_ID",
    "HOST_BOX_ID",
    "TENANT_SLUG",
    "BENCH_INSTANCE_ID",
    "SERVER_ID",
)

# The placeholder vocabulary. RFC 5737 and RFC 3849 addresses, an angle-bracket
# token naming what the value is, and the two stand-ins the scanner itself
# allows (a synthetic slug and an example-domain mailbox).
PLACEHOLDER_IPV4 = tuple(ipaddress.ip_network(net) for net in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
PLACEHOLDER_IPV6 = ipaddress.ip_network("2001:db8::/32")
TOKEN = re.compile(r"<[a-z][a-z0-9-]*(?::[a-z0-9-]+)?>")
STAND_INS = {"odl-000000", "owner@example.com"}
KEY_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


class HostsError(ValueError):
    """One line that cannot be read as the template says it must be."""


def is_placeholder(value: str) -> bool:
    """True for a value the documented placeholder vocabulary allows."""
    if TOKEN.fullmatch(value):
        return True
    if value in STAND_INS:
        return True
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        return any(address in net for net in PLACEHOLDER_IPV4)
    return address in PLACEHOLDER_IPV6


def parse(text: str, *, placeholders: bool) -> dict[str, str]:
    """The file's values, or a HostsError naming the first bad line."""
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = KEY_LINE.match(stripped)
        if not match:
            raise HostsError(f"line {number}: expected KEY=VALUE, got {stripped!r}")
        key, value = match.group(1), match.group(2)
        if key not in KEYS:
            raise HostsError(f"line {number}: unknown key {key!r} (not in hosts_file.KEYS)")
        if key in values:
            raise HostsError(f"line {number}: duplicate key {key!r}")
        if not value:
            raise HostsError(f"line {number}: {key} has no value")
        if placeholders and not is_placeholder(value):
            raise HostsError(f"line {number}: {key}={value!r} is not a placeholder "
                             f"(RFC 5737/3849, <token>, odl-000000 or owner@example.com)")
        values[key] = value
    missing = [key for key in KEYS if key not in values]
    if missing:
        raise HostsError(f"missing keys: {', '.join(missing)}")
    return values


def load(path: Path, *, placeholders: bool = False) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise HostsError(f"{path}: {error.strerror or error}") from error
    return parse(text, placeholders=placeholders)


def default_path() -> Path:
    pinned = os.environ.get(ENV_PIN, "").strip()
    if pinned:
        return Path(pinned).expanduser()
    return LOCAL if LOCAL.is_file() else EXAMPLE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read the Odile host values file.")
    parser.add_argument("--path", default=None, help="read this file instead of the default")
    parser.add_argument("--example", action="store_true",
                        help="read the committed template and require placeholder values")
    args = parser.parse_args(argv)
    path = EXAMPLE if args.example else (Path(args.path) if args.path else default_path())
    try:
        values = load(path, placeholders=args.example)
    except HostsError as error:
        print(f"hosts: {error}", file=sys.stderr)
        return 1
    kind = "placeholders" if args.example else "values"
    print(f"hosts: {len(values)} keys, {kind} from {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
