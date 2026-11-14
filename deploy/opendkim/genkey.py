#!/usr/bin/python3
"""Generate the DKIM key pair on the machine that signs — the private half is
never committed.

    ./deploy/opendkim/genkey.py --check     the key exists and matches odile.txt
    ./deploy/opendkim/genkey.py --apply     generate it if it is absent

`--apply` never overwrites an existing key: replacing a signing key silently
would invalidate every signature in flight. Rotation is explicit and is
described in docs/runbooks/email.md §8.

The public TXT is printed so it can be committed to deploy/opendkim/<selector>.txt
and published at `<selector>._domainkey.mail.odile.fyi`.
"""

from __future__ import annotations

import argparse
import grp
import os
import pwd
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEYS = Path("/etc/dkimkeys")
DOMAIN = "mail.odile.fyi"
DEFAULT_SELECTOR = "odile"


def public_from_private(path: Path) -> str:
    result = subprocess.run(
        ["openssl", "rsa", "-in", str(path), "-pubout"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "openssl rsa failed")
    return "".join(re.sub(r"-----[^-]+-----", "", result.stdout).split())


def committed_public(selector: str) -> str:
    text = (HERE / f"{selector}.txt").read_text(encoding="utf-8")
    joined = "".join(re.findall(r'"([^"]*)"', text)) or text
    match = re.search(r"p=([A-Za-z0-9+/=]+)", joined)
    if not match:
        raise RuntimeError(f"deploy/opendkim/{selector}.txt has no p= value")
    return match.group(1)


def check(selector: str) -> int:
    private = KEYS / f"{selector}.private"
    problems = []
    if not private.exists():
        print(f"FAIL  {private}: missing")
        return 1
    info = private.stat()
    mode = oct(info.st_mode & 0o777)[2:]
    if mode != "600":
        problems.append(f"{private}: mode {mode}, want 600")
    want_uid = pwd.getpwnam("opendkim").pw_uid
    want_gid = grp.getgrnam("opendkim").gr_gid
    if (info.st_uid, info.st_gid) != (want_uid, want_gid):
        problems.append(f"{private}: owner {info.st_uid}:{info.st_gid}, want opendkim:opendkim")
    try:
        derived = public_from_private(private)
        committed = committed_public(selector)
    except (OSError, RuntimeError) as exc:
        problems.append(str(exc))
    else:
        if derived != committed:
            problems.append(
                f"the key in {private} does not match the p= committed in "
                f"deploy/opendkim/{selector}.txt"
            )
    for problem in problems:
        print(f"FAIL  {problem}")
    if problems:
        return 1
    print(f"ok    {private} is mode 0600, owned by opendkim, and matches "
          f"deploy/opendkim/{selector}.txt")
    return 0


def generate(selector: str) -> int:
    private = KEYS / f"{selector}.private"
    if private.exists():
        print(f"nothing to do; {private} already exists (a second run changes nothing)")
        return check(selector)
    KEYS.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["opendkim-genkey", "-b", "2048", "-d", DOMAIN, "-s", selector, "-D", str(KEYS)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(f"opendkim-genkey failed: {result.stdout + result.stderr}", file=sys.stderr)
        return 1
    os.chmod(private, 0o600)
    os.chown(private, pwd.getpwnam("opendkim").pw_uid, grp.getgrnam("opendkim").gr_gid)
    public = KEYS / f"{selector}.txt"
    print(f"wrote {private} (2048-bit, mode 0600, opendkim:opendkim)")
    print(f"wrote {public}; commit it as deploy/opendkim/{selector}.txt and publish the TXT")
    print(public.read_text(encoding="utf-8"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate or verify Odile's DKIM key pair on this machine."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="generate the key if absent")
    group.add_argument("--check", action="store_true", help="verify key, mode and public TXT")
    parser.add_argument("--selector", default=DEFAULT_SELECTOR,
                        help=f"DKIM selector (default {DEFAULT_SELECTOR})")
    args = parser.parse_args(argv)
    if args.apply:
        return generate(args.selector)
    return check(args.selector)


if __name__ == "__main__":
    sys.exit(main())
