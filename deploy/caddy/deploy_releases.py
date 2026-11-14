#!/usr/bin/python3
"""Serve /releases/* on https://odile.fyi from /srv/odile-releases.

ODL-332: the Servury panel's Boot image and Disk image fields take an https
URL and a SHA-256. The tenant image is a few gigabytes, so it is Caddy's
file_server — not the stdlib app — that streams it.

The block is spliced into the live /etc/caddy/Caddyfile between
`# BEGIN odile-releases` and `# END odile-releases`, inside the `odile.fyi`
site, before its catch-all handle. On a first apply it is inserted immediately
before the `# BEGIN odile-seed` line (which sits inside that site); every other
line is left exactly as it was. Caddy runs with `admin off`, so a change means
`systemctl restart caddy`; the whole file is `caddy validate`d first and a
timestamped backup is restored if the restart breaks the landing page or the
route.

A converged host prints `same` and restarts nothing.

    ./deploy/caddy/deploy_releases.py             apply
    ./deploy/caddy/deploy_releases.py --dry-run   show the plan only

Exit codes: 0 success (changed or already converged), 1 a failed apply that was
rolled back, 2 a bad input or missing prerequisite.
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FRAGMENT = REPO / "deploy/caddy/releases.caddy"

RELEASES_DIR = Path("/srv/odile-releases")
CADDYFILE = Path("/etc/caddy/Caddyfile")
BEGIN = "# BEGIN odile-releases"
END = "# END odile-releases"
ANCHOR = "# BEGIN odile-seed"
BASE = "https://odile.fyi"


def splice(existing: str, block: str) -> str:
    """Return `existing` with our block replaced, or inserted before the anchor."""
    lines = existing.splitlines()
    begins = [i for i, line in enumerate(lines) if line.strip() == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if len(begins) > 1 or len(ends) > 1:
        raise ValueError(f"{CADDYFILE}: more than one '{BEGIN}'/'{END}' pair")
    block_lines = block.rstrip("\n").splitlines()
    if begins:
        if not ends:
            raise ValueError(f"{CADDYFILE}: '{BEGIN}' without '{END}'")
        if ends[0] < begins[0]:
            raise ValueError(f"{CADDYFILE}: '{END}' comes before '{BEGIN}'")
        return "\n".join(
            lines[: begins[0]] + block_lines + lines[ends[0] + 1 :]
        ) + "\n"
    anchor = [i for i, line in enumerate(lines) if line.strip() == ANCHOR]
    if len(anchor) != 1:
        raise ValueError(
            f"{CADDYFILE}: expected exactly one '{ANCHOR}' to insert before, "
            f"found {len(anchor)}"
        )
    return "\n".join(
        lines[: anchor[0]] + block_lines + [""] + lines[anchor[0] :]
    ) + "\n"


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=120)


def fetch(path: str) -> tuple[int, bytes]:
    request = urllib.request.Request(BASE + path, headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not FRAGMENT.is_file():
        print(f"error: missing {FRAGMENT}", file=sys.stderr)
        return 2
    if not CADDYFILE.is_file():
        print(f"error: missing {CADDYFILE}", file=sys.stderr)
        return 2
    block = FRAGMENT.read_text()
    existing = CADDYFILE.read_text()
    try:
        candidate = splice(existing, block)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if candidate == existing:
        print(f"same      {CADDYFILE} ({BEGIN} block)")
    else:
        print(f"splice    {CADDYFILE} ({BEGIN} block, {len(block.splitlines())} lines)")

    probe = f"probe-{secrets.token_hex(8)}.txt"
    probe_bytes = f"{probe}\n".encode()
    if args.dry_run:
        print(f"would     stage {RELEASES_DIR / probe} and verify {BASE}/releases/{probe}")
        print("would     caddy validate, systemctl restart caddy (only if the file changed)")
        return 0

    RELEASES_DIR.mkdir(parents=True, exist_ok=True)
    (RELEASES_DIR / probe).write_bytes(probe_bytes)
    try:
        if candidate != existing:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = CADDYFILE.with_name(f"Caddyfile.bak-releases-{stamp}")
            backup.write_text(existing)
            staged = CADDYFILE.with_name("Caddyfile.releases-candidate")
            staged.write_text(candidate)
            check = run(["caddy", "validate", "--config", str(staged)])
            staged.unlink()
            if check.returncode != 0:
                print(f"error: caddy validate failed:\n{check.stderr.strip()}", file=sys.stderr)
                return 2
            print("validate  caddy validate: ok")
            CADDYFILE.write_text(candidate)
            restart = run(["systemctl", "restart", "caddy"])
            if restart.returncode != 0:
                CADDYFILE.write_text(existing)
                run(["systemctl", "restart", "caddy"])
                print(f"error: caddy restart failed; restored {backup}", file=sys.stderr)
                return 1
            print("restart   systemctl restart caddy")
            time.sleep(2)
        status, body = fetch(f"/releases/{probe}")
        if status != 200 or body != probe_bytes:
            print(
                f"error: {BASE}/releases/{probe} -> {status}, "
                f"{len(body)} bytes (want 200, {len(probe_bytes)} bytes)",
                file=sys.stderr,
            )
            return 1
        print(f"verify    {BASE}/releases/{probe} -> 200, {len(body)} bytes byte-identical")
        landing, _ = fetch("/")
        if landing != 200:
            print(f"error: {BASE}/ -> {landing} after the change", file=sys.stderr)
            return 1
        print(f"verify    {BASE}/ -> 200")
    finally:
        (RELEASES_DIR / probe).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
