#!/usr/bin/python3
"""Reconcile OpenDKIM on this machine with the files committed in deploy/opendkim/.

    ./deploy/opendkim/apply.py             dry run: say what would change
    ./deploy/opendkim/apply.py --apply     write, fix ownership, reload
    ./deploy/opendkim/apply.py --check     exit 1 if anything differs
    ./deploy/opendkim/apply.py --self-test offline checks, including rotation

The private key is not in git and never will be. This script follows the
committed KeyTable: for every selector it names, the private key on disk must
be the key whose public half is committed in `deploy/opendkim/<selector>.txt`,
mode 0600 and owned by opendkim, and the milter socket directory must be
group-owned by postfix (without that every connection is `451 4.7.1`).

Following the KeyTable is what makes a rotation safe: the day the tables point
at a new selector, `--check` starts demanding that selector's key and TXT, so a
half-done rotation fails instead of signing with the old key.

Exit codes: 0 clean or applied, 1 drift or a failed step, 2 usage error.
"""

from __future__ import annotations

import argparse
import grp
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEYS = Path("/etc/dkimkeys")
SOCKET_DIR = Path("/var/spool/postfix/opendkim")

BASE_FILES = [
    # source, destination, owner, group, mode
    ("opendkim.conf", Path("/etc/opendkim.conf"), "root", "root", 0o644),
    ("KeyTable", KEYS / "KeyTable", "opendkim", "opendkim", 0o640),
    ("SigningTable", KEYS / "SigningTable", "opendkim", "opendkim", 0o640),
    ("TrustedHosts", KEYS / "TrustedHosts", "opendkim", "opendkim", 0o640),
]

DIRECTORIES = [
    (KEYS, "opendkim", "opendkim", 0o750),
    (SOCKET_DIR, "opendkim", "postfix", 0o750),
]


def ids(owner: str, group: str) -> tuple[int, int]:
    return pwd.getpwnam(owner).pw_uid, grp.getgrnam(group).gr_gid


def public_from_private(path: Path) -> str:
    """The base64 body of the public key that belongs to `path`, no whitespace."""
    result = subprocess.run(
        ["openssl", "rsa", "-in", str(path), "-pubout"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "openssl rsa failed")
    body = re.sub(r"-----[^-]+-----\s*", "", result.stdout)
    return "".join(body.split())


def committed_public(selector: str) -> str:
    path = HERE / f"{selector}.txt"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"deploy/opendkim/{selector}.txt is missing: commit the TXT for the "
            f"selector KeyTable names ({selector})"
        ) from exc
    match = re.search(r"p=([A-Za-z0-9+/=]+)", "".join(re.findall(r'"([^"]*)"', text)) or text)
    if not match:
        raise RuntimeError(f"deploy/opendkim/{selector}.txt has no p= value")
    return match.group(1)


def keytable_entries(root: Path | None = None) -> list[tuple[str, Path]]:
    """(selector, private key path) for every line of the committed KeyTable.

    Format: <selector>._domainkey.<domain>  <domain>:<selector>:<keyfile>
    """
    text = ((root or HERE) / "KeyTable").read_text(encoding="utf-8")
    entries: list[tuple[str, Path]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != 2:
            raise RuntimeError(f"deploy/opendkim/KeyTable line {number}: {line!r}")
        parts = fields[1].split(":", 2)
        if len(parts) != 3:
            raise RuntimeError(
                f"deploy/opendkim/KeyTable line {number}: {fields[1]!r} is not "
                "domain:selector:keyfile"
            )
        entries.append((parts[1], Path(parts[2])))
    return entries


def text_files() -> list[tuple]:
    """The committed TXT for every selector in the KeyTable (never a guessed one)."""
    try:
        selectors = [selector for selector, _ in keytable_entries()]
    except (OSError, RuntimeError):
        selectors = []
    entries: list[tuple] = []
    for selector in selectors:
        source = HERE / f"{selector}.txt"
        if source.exists():
            entries.append((f"{selector}.txt", KEYS / f"{selector}.txt", "root", "root", 0o644))
    return entries


def files() -> list[tuple]:
    return list(BASE_FILES) + text_files()


def first_difference(wanted: bytes, live: bytes) -> str:
    want = wanted.decode("utf-8", "replace").splitlines()
    have = live.decode("utf-8", "replace").splitlines()
    for number, (left, right) in enumerate(zip(want, have), start=1):
        if left != right:
            return f"line {number}: committed {left!r}, live {right!r}"
    if len(want) > len(have):
        return f"live file ends at line {len(have)}; committed has {len(want)}"
    return f"live file has {len(have)} lines; committed has {len(want)}"


def inspect(entry) -> list[str]:
    source, destination, owner, group, mode = entry
    wanted = (HERE / source).read_bytes()
    if not destination.exists():
        return [f"{destination}: missing"]
    problems = []
    live = destination.read_bytes()
    if wanted != live:
        problems.append(f"{destination}: content differs — {first_difference(wanted, live)}")
    info = destination.stat()
    if stat.S_IMODE(info.st_mode) != mode:
        problems.append(
            f"{destination}: mode {oct(stat.S_IMODE(info.st_mode))[2:]}, want {oct(mode)[2:]}"
        )
    uid, gid = ids(owner, group)
    if (info.st_uid, info.st_gid) != (uid, gid):
        problems.append(f"{destination}: owner {info.st_uid}:{info.st_gid}, want {owner}:{group}")
    return problems


def write(entry) -> bool:
    source, destination, owner, group, mode = entry
    wanted = (HERE / source).read_bytes()
    changed = not destination.exists() or destination.read_bytes() != wanted
    destination.parent.mkdir(parents=True, exist_ok=True)
    uid, gid = ids(owner, group)
    if changed:
        handle = tempfile.NamedTemporaryFile(dir=destination.parent, delete=False)
        try:
            handle.write(wanted)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.chmod(handle.name, mode)
        os.chown(handle.name, uid, gid)
        os.replace(handle.name, destination)
    else:
        os.chmod(destination, mode)
        os.chown(destination, uid, gid)
    return changed


def directory_problems() -> list[str]:
    problems = []
    for path, owner, group, mode in DIRECTORIES:
        if not path.is_dir():
            problems.append(f"{path}: missing directory")
            continue
        info = path.stat()
        if stat.S_IMODE(info.st_mode) != mode:
            problems.append(f"{path}: mode {oct(stat.S_IMODE(info.st_mode))[2:]}, want {oct(mode)[2:]}")
        uid, gid = ids(owner, group)
        if (info.st_uid, info.st_gid) != (uid, gid):
            problems.append(f"{path}: owner {info.st_uid}:{info.st_gid}, want {owner}:{group}")
    return problems


def key_problems() -> list[str]:
    """Every selector KeyTable names: key on disk, mode, owner, committed TXT."""
    problems: list[str] = []
    try:
        entries = keytable_entries()
    except (OSError, RuntimeError) as exc:
        return [str(exc)]
    if not entries:
        return ["deploy/opendkim/KeyTable names no signing key"]
    for selector, keyfile in entries:
        try:
            committed = committed_public(selector)
        except (OSError, RuntimeError) as exc:
            problems.append(str(exc))
            committed = None
        private = Path(keyfile)
        if not private.exists():
            problems.append(
                f"{private}: missing — run ./deploy/opendkim/genkey.py --apply "
                f"--selector {selector}"
            )
            continue
        info = private.stat()
        if stat.S_IMODE(info.st_mode) != 0o600:
            problems.append(
                f"{private}: mode {oct(stat.S_IMODE(info.st_mode))[2:]}, want 600"
            )
        uid, gid = ids("opendkim", "opendkim")
        if (info.st_uid, info.st_gid) != (uid, gid):
            problems.append(f"{private}: owner {info.st_uid}:{info.st_gid}, want opendkim:opendkim")
        try:
            derived = public_from_private(private)
        except RuntimeError as exc:
            problems.append(f"{private}: cannot read the key: {exc}")
            continue
        if committed is not None and derived != committed:
            problems.append(
                f"the key in {private} does not match deploy/opendkim/{selector}.txt — "
                "rotate or commit the right TXT"
            )
    return problems


def drift() -> list[str]:
    return [problem for entry in files() for problem in inspect(entry)] + \
        directory_problems() + key_problems()


def run(command: list[str]) -> tuple[int, str]:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    return result.returncode, (result.stdout + result.stderr).strip()


def self_test() -> int:
    """Offline checks for the rotation path. No network, no /etc writes."""
    failures = 0
    count = 0

    def check(name: str, got, want) -> None:
        nonlocal failures, count
        count += 1
        if got == want:
            print(f"ok    {name}")
        else:
            failures += 1
            print(f"FAIL  {name}: got {got!r}, want {want!r}")

    entries = keytable_entries()
    check("the committed KeyTable names one signing key",
          entries, [("odile", Path("/etc/dkimkeys/odile.private"))])
    check("the committed TXT carries a full public key",
          len(committed_public("odile")) > 300, True)

    global HERE
    saved = HERE
    with tempfile.TemporaryDirectory(prefix="odile-opendkim-rotation-") as tmp:
        HERE = Path(tmp)
        (HERE / "KeyTable").write_text(
            "odile-2027-01._domainkey.mail.odile.fyi  "
            "mail.odile.fyi:odile-2027-01:/etc/dkimkeys/odile-2027-01.private\n",
            encoding="utf-8",
        )
        try:
            problems = key_problems()
        finally:
            HERE = saved
        check("a rotated selector with no key is drift",
              any("odile-2027-01.private" in problem for problem in problems), True)
        check("a rotated selector with no committed TXT is drift",
              any("odile-2027-01.txt" in problem for problem in problems), True)

    print(f"\n{count} checks, {failures} failed")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile OpenDKIM on this machine with deploy/opendkim/."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="write the changes")
    group.add_argument("--check", action="store_true", help="exit 1 on any drift")
    group.add_argument("--self-test", action="store_true", help="offline checks")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    problems = drift()

    if args.check:
        for problem in problems:
            print(f"DRIFT  {problem}")
        if problems:
            print(f"\n{len(problems)} difference(s) from deploy/opendkim")
            return 1
        print("clean: /etc/opendkim.conf, the signing tables, every active key and "
              "its TXT match deploy/opendkim")
        return 0

    if not args.apply:
        if not problems:
            print("nothing to do; OpenDKIM matches deploy/opendkim")
            return 0
        for problem in problems:
            print(f"would fix  {problem}")
        print(f"\ndry run: {len(problems)} change(s); pass --apply to write")
        return 0

    content_changed = False
    for entry in files():
        changed = write(entry)
        content_changed = content_changed or changed
        print(f"{'wrote' if changed else 'ok':<6} {entry[1]}")
    for path, owner, group_name, mode in DIRECTORIES:
        path.mkdir(parents=True, exist_ok=True)
        uid, gid = ids(owner, group_name)
        os.chmod(path, mode)
        os.chown(path, uid, gid)
    print(f"ok     {KEYS} and {SOCKET_DIR} (owner and mode)")

    left = drift()
    if left:
        for problem in left:
            print(f"still  {problem}")
        print("\nOpenDKIM was not reloaded")
        return 1

    if content_changed:
        code, output = run(["systemctl", "reload", "opendkim"])
        if code != 0:
            print(f"opendkim reload failed:\n{output}")
            return 1
        print("reloaded opendkim")
    else:
        print("nothing to do; OpenDKIM matches deploy/opendkim")
    return 0


if __name__ == "__main__":
    sys.exit(main())
