#!/usr/bin/python3
"""Deploy the M-1 prototype host, https://testing.odile.fyi.

Three state changes, all idempotent:

  * stage prototype/userflow.html at /srv/odile-testing/index.html, byte for
    byte, and check it still hashes to the reviewed value;
  * stage deploy/caddy/testing-robots.txt at /srv/odile-testing/robots.txt;
  * splice deploy/caddy/testing-site.caddy into the live /etc/caddy/Caddyfile
    between `# BEGIN odile-testing` and `# END odile-testing`, leaving every
    other block exactly as it was.

Caddy runs with `admin off`, so a changed Caddyfile means `systemctl restart
caddy`. Before the restart the whole file is checked with `caddy validate`; a
timestamped backup of the last good file is kept, and the script restores it if
the restart leaves either host broken.

A converged host prints `nothing to do` and restarts nothing.

    ./deploy/caddy/deploy_testing_host.py             apply
    ./deploy/caddy/deploy_testing_host.py --dry-run   show the plan only

Exit codes: 0 success (changed or already converged), 1 a failed apply that was
rolled back, 2 a bad input or missing prerequisite.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROTOTYPE = REPO / "prototype/userflow.html"
SITE_BLOCK = REPO / "deploy/caddy/testing-site.caddy"
ROBOTS = REPO / "deploy/caddy/testing-robots.txt"

WEB_ROOT = Path("/srv/odile-testing")
CADDYFILE = Path("/etc/caddy/Caddyfile")
CADDY_BACKUP_DIR = Path("/etc/caddy")

BEGIN = "# BEGIN odile-testing"
END = "# END odile-testing"

# The card's number: the sha256 of prototype/userflow.html on main, audited by
# ODL-096. If the prototype changes, change this constant and the card together.
EXPECTED_SHA256 = "9ab262d8aeae08c855fd894cb5d1cad5d491f8597f42e7795c7748e74af0c95e"

LANDING_URL = "https://odile.fyi/"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stages_prototype(block: str) -> bool:
    """True when the site block serves files, so the prototype must be staged.

    A retired host's block is a redirect and serves nothing; requiring the
    reviewed prototype hash there would block a one-line retirement on a file
    the host no longer reads.
    """
    return "file_server" in block


def splice_caddyfile(existing: str, block: str) -> str:
    """Return `existing` with the managed block replaced or appended.

    Exactly zero or one marker pair is allowed; an unbalanced or duplicated
    pair is an error rather than a guess, so a half-edited Caddyfile can never
    be overwritten silently.
    """
    block_lines = block.rstrip("\n").splitlines()
    lines = existing.splitlines()
    begins = [i for i, line in enumerate(lines) if line.strip() == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if len(begins) != len(ends) or len(begins) > 1:
        raise ValueError(
            f"{CADDYFILE}: expected at most one '{BEGIN}' / '{END}' pair, "
            f"found {len(begins)} begin and {len(ends)} end markers"
        )
    if not begins:
        return "\n".join(lines + [""] + block_lines).rstrip("\n") + "\n"
    begin, end = begins[0], ends[0]
    if end < begin:
        raise ValueError(f"{CADDYFILE}: '{END}' comes before '{BEGIN}'")
    return "\n".join(lines[:begin] + block_lines + lines[end + 1:]).rstrip("\n") + "\n"


def write_atomic(path: Path, data: bytes, mode: int) -> None:
    """Write `data` to `path` through a sibling temp file, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, **kwargs)


def wait_for_landing(seconds: float = 15.0) -> str:
    """Poll odile.fyi until it answers 200; return the last status seen."""
    deadline = time.monotonic() + seconds
    status = "000"
    while time.monotonic() < deadline:
        result = run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5", LANDING_URL]
        )
        status = result.stdout.strip() or "000"
        if status == "200":
            return status
        time.sleep(0.5)
    return status


def restore(backup: Path) -> None:
    shutil.copy2(backup, CADDYFILE)
    print(f"rollback  restored {CADDYFILE} from {backup}", file=sys.stderr)
    run(["systemctl", "restart", "caddy"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage the M-1 prototype and add its Caddy site block.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-n", "--dry-run", action="store_true", help="show the plan, change nothing")
    args = parser.parse_args(argv)
    dry = args.dry_run

    missing = [str(path) for path in (PROTOTYPE, SITE_BLOCK, ROBOTS) if not path.is_file()]
    if missing:
        print(f"error: missing source file(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    block = SITE_BLOCK.read_text(encoding="utf-8")
    if stages_prototype(block):
        prototype = PROTOTYPE.read_bytes()
        digest = sha256(prototype)
        print(f"source    {PROTOTYPE.relative_to(REPO)} sha256 {digest}")
        if digest != EXPECTED_SHA256:
            print(
                "error: prototype/userflow.html does not match the reviewed hash "
                f"{EXPECTED_SHA256}",
                file=sys.stderr,
            )
            return 2
        desired = {
            WEB_ROOT / "index.html": prototype,
            WEB_ROOT / "robots.txt": ROBOTS.read_bytes(),
        }
    else:
        # A retired host serves no files: the block is a redirect, so the
        # prototype's hash is no longer this host's contract.
        print(f"retired   {SITE_BLOCK.relative_to(REPO)} — redirect only, prototype not staged")
        desired = {}
    writes: list[tuple[Path, bytes]] = []
    for target, data in desired.items():
        current = target.read_bytes() if target.is_file() else None
        if current == data:
            print(f"same      {target}")
        else:
            verb = "would write" if dry else "write"
            print(f"{verb:<9} {target} ({len(data)} bytes, sha256 {sha256(data)})")
            writes.append((target, data))

    if not CADDYFILE.is_file():
        print(f"error: {CADDYFILE} is missing", file=sys.stderr)
        return 2
    existing = CADDYFILE.read_text(encoding="utf-8")
    try:
        updated = splice_caddyfile(existing, block)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    caddy_changed = updated != existing
    if caddy_changed:
        verb = "would edit" if dry else "edit"
        print(f"{verb:<9} {CADDYFILE} ({BEGIN} block)")
    else:
        print(f"same      {CADDYFILE} ({BEGIN} block)")

    if not writes and not caddy_changed:
        print("nothing to do")
        return 0
    if dry:
        print("dry run: nothing written, caddy untouched")
        return 0

    if os.geteuid() != 0:
        print("error: applying needs root; reinstalling state under /srv and /etc", file=sys.stderr)
        return 2

    for target, data in writes:
        write_atomic(target, data, 0o644)

    backup: Path | None = None
    if caddy_changed:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = CADDY_BACKUP_DIR / f"Caddyfile.bak-{stamp}"
        shutil.copy2(CADDYFILE, backup)
        os.chmod(backup, 0o644)
        print(f"backup    {backup}")
        write_atomic(CADDYFILE, updated.encode("utf-8"), CADDYFILE.stat().st_mode & 0o777)

        check = run(["caddy", "validate", "--config", str(CADDYFILE), "--adapter", "caddyfile"])
        if check.returncode != 0:
            print(f"error: caddy validate failed:\n{check.stderr.strip()}", file=sys.stderr)
            restore(backup)
            return 1
        print("validate  caddy validate: ok")

        run(["systemctl", "restart", "caddy"])
        print("restart   systemctl restart caddy")
        for _ in range(20):
            if run(["systemctl", "is-active", "caddy"]).stdout.strip() == "active":
                break
            time.sleep(0.5)
        else:
            print("error: caddy did not come back active", file=sys.stderr)
            restore(backup)
            return 1

        status = wait_for_landing()
        if status != "200":
            print(f"error: {LANDING_URL} answered {status} after the restart", file=sys.stderr)
            restore(backup)
            return 1
        print(f"verify    {LANDING_URL} -> 200")

    print(f"deployed  {len(writes)} file(s), caddyfile {'updated' if caddy_changed else 'unchanged'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
