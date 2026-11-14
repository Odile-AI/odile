#!/usr/bin/python3
"""Seed the recovery file before the first-visit page can be answered (ODL-332).

The release installer writes /etc/odile/recovery.txt near the *end* of its
run, after Caddy is answering. The first-visit page is reachable as soon as
Caddy is up, so a user who sets the sign-in inside that window gets the done
page rendered with no recovery file: "The recovery file is not available on
this machine", and the one-shot page never offers the download. Measured on the
tenant (ODL-332 round 7): the page was rendered at 09:17:37.890Z and the file
written at 09:17:37.994Z.

This program runs as the first-visit unit's ExecStartPre and writes the same
file first — a fresh password generated *on this machine* (never baked into the
published disk image), and the certificate line pending. The release installer's
``ensure_recovery`` keeps the password and the ``Written`` time it finds, and
the page's download fills the certificate line from Caddy's live store
(installer/first_visit.py, ODL-471).

**A rebuild passes the old password instead.** With ``--password-stdin`` the
program writes the password it reads on stdin rather than generating one, and
rewrites an existing file: `odile-firstboot`, which holds the token that opens
the seed's sealed password, runs it before the installer, so the rebuilt
machine's recovery file opens the repository it already has. A first install
passes no such flag and the generated password is unchanged. The value never
appears on argv.

The bytes are exactly ``render_recovery``'s in installer/odile_install.py with
``fingerprint=None``; ``tests/test_sealed_image.py`` compares the two so the
formats cannot drift.

Idempotent: an existing non-empty file is left byte-identical, so a reboot and
a re-run of the installer converge on the same file. The one exception is the
explicit ``--password-stdin`` write, which keeps the file's own ``Written:``
line.

    /usr/bin/python3 recovery-seed.py --path /etc/odile/recovery.txt \\
        --cmdline /proc/cmdline
    printf '%s' "$PASSWORD" | /usr/bin/python3 recovery-seed.py \\
        --path /etc/odile/recovery.txt --tenant odl-7f3a2c --instance <id> \\
        --password-stdin
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import string
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = "/etc/odile/recovery.txt"
DEFAULT_CMDLINE = "/proc/cmdline"
PENDING = ("(pending — filled in when you download this file on the "
           "first-visit page)")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_password() -> str:
    """32 characters, no shell-hostile punctuation to paste.

    The same alphabet and length as installer/odile_install.py's
    ``generate_password``: the installer adopts this password, so the user's
    recovery file must read the same on the page, in the file and in a later
    ``ensure_recovery``.
    """
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(32))


def render(slug: str, instance_id: str, backup_password: str,
           written: str | None = None) -> str:
    """The recovery file, byte-identical to the installer's render_recovery."""
    return f"""\
Odile recovery file
===================
Tenant:              {slug}
Instance:            {instance_id}
Written:             {written or utc_now()}

Backup password:     {backup_password}
Certificate SHA-256: {PENDING}

This file is the only copy of the backup password. Download it on this machine's
first-visit page and keep it somewhere you can reach when the machine is gone.
The file stays on the machine; ask your agent to show it again if you need it.

Lose it and the encrypted backup cannot be opened; Odile holds no copy.
"""


def cmdline_values(text: str) -> dict[str, str]:
    """The bb.host and bb.iid tokens of one kernel command line."""
    values: dict[str, str] = {}
    for token in text.split():
        if token.startswith("bb.host="):
            values["host"] = token.split("=", 1)[1]
        elif token.startswith("bb.iid="):
            values["instance"] = token.split("=", 1)[1]
    return values


def write_atomic(path: Path, text: str) -> None:
    """Write 0600 through a sibling temp file; never leave a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False)
    try:
        with handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def existing_text(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return text if text.strip() else None


MIN_PASSWORD = 16
MAX_PASSWORD = 1024
WRITTEN_RE = re.compile(r"^Written:[ \t]*(.+?)[ \t]*$", re.M)


def existing_written(text: str | None) -> str | None:
    """The `Written:` stamp of a file this program is about to replace."""
    if not text:
        return None
    match = WRITTEN_RE.search(text)
    return match.group(1) if match else None


def password_from_stdin() -> str:
    """The seeded password, validated before it reaches the file.

    A truncated read is refused here rather than written: an empty or short
    value would become a recovery file that restic rejects as "wrong password".
    """
    password = sys.stdin.read().strip()
    if not (MIN_PASSWORD <= len(password) <= MAX_PASSWORD):
        raise SystemExit("recovery-seed: --password-stdin needs %d to %d characters, got %d"
                         % (MIN_PASSWORD, MAX_PASSWORD, len(password)))
    if any(char.isspace() for char in password):
        raise SystemExit("recovery-seed: --password-stdin value contains whitespace")
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--cmdline", default=DEFAULT_CMDLINE)
    parser.add_argument("--tenant", default="", help="override bb.host")
    parser.add_argument("--instance", default="", help="override bb.iid")
    parser.add_argument("--password-stdin", action="store_true",
                        help="write the password read on stdin (a rebuild), not a new one")
    args = parser.parse_args(argv)

    path = Path(args.path)
    text = existing_text(path)
    password = password_from_stdin() if args.password_stdin else None
    if text is not None and password is None:
        print("recovery-seed: %s exists (%d bytes), left as it is"
              % (path, len(text.encode("utf-8"))), flush=True)
        return 0

    tenant, instance = args.tenant, args.instance
    if not tenant or not instance:
        try:
            values = cmdline_values(Path(args.cmdline).read_text(encoding="utf-8"))
        except OSError:
            values = {}
        tenant = tenant or values.get("host", "")
        instance = instance or values.get("instance", "")
    if not tenant:
        print("recovery-seed: no bb.host in %s; not writing %s"
              % (args.cmdline, path), flush=True)
        return 0

    write_atomic(path, render(tenant, instance, password or generate_password(),
                              written=existing_written(text)))
    if password is not None:
        print("recovery-seed: wrote %s (tenant %s, the password from the seed, "
              "certificate pending)" % (path, tenant), flush=True)
    else:
        print("recovery-seed: wrote %s (tenant %s, certificate pending)"
              % (path, tenant), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
