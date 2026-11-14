#!/usr/bin/python3
"""Embed scripts/backup.py into installer/odile_install.py.

The installer is one printable file: the reviewed tenant backup program
(ODL-215) travels inside it, pinned by SHA-256, and `ensure_backup_source`
writes those exact bytes to ``/usr/local/lib/odile/backup.py``. This tool is
the only supported way to refresh that copy, so the embedded text and the pin
can never disagree:

    ./installer/embed_backup.py
    ./installer/odile_install.py --self-test   # checks the hash and the source

After a change here, re-sign the installer (see docs/runbooks/install.md).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "installer" / "odile_install.py"
BACKUP = ROOT / "scripts" / "backup.py"

START = "BACKUP_SOURCE = r'''"
END = "'''"


def main() -> int:
    backup = BACKUP.read_bytes()
    if b"'''" in backup:
        raise SystemExit(f"error: {BACKUP} contains a triple single quote")
    try:
        backup.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"error: {BACKUP} is not UTF-8: {exc}") from exc
    text = INSTALLER.read_text(encoding="utf-8")
    try:
        start = text.index(START) + len(START)
        end = text.index(END, start)
    except ValueError as exc:
        raise SystemExit(f"error: {INSTALLER} has no BACKUP_SOURCE block") from exc
    digest = hashlib.sha256(backup).hexdigest()
    text = text[:start] + backup.decode("utf-8") + text[end:]
    text = re.sub(r'BACKUP_SHA256 = "[^"]+"', f'BACKUP_SHA256 = "{digest}"', text, count=1)
    INSTALLER.write_text(text, encoding="utf-8")
    print(f"embedded {BACKUP} ({len(backup)} bytes, sha256 {digest}) into {INSTALLER}")
    print("next: ./installer/odile_install.py --self-test, then re-sign")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
