#!/usr/bin/env python3
"""Publish a built UKI at a content-addressed URL, and print the panel line.

The node takes a boot image as **URL + SHA-256** (`docs/product/SPIKE-sealed-
unlock.md` §7), and the panel pins the hash. Overwriting one fixed filename
therefore breaks every pin on the next build — which is why each rebuild needed
a hand edit in the panel. Naming the file after its own hash makes every build
immutable: the old URL keeps working for the old image, and the new one is a
fresh URL.

    ./tools/publish_uki.py --uki out/odile-tenant.efi
    URL      https://odile.fyi/releases/uki/9f1c…efi
    SHA-256  9f1c…

Paste those two lines into the panel's Manage → Boot image. Everything after
that is `tools/servury_cycle.py`.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import urllib.request
from pathlib import Path

PUBLISH_DIR = Path("/srv/odile-releases/uki")
BASE_URL = "https://odile.fyi/releases/uki"


def digest_of(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def destination(directory: Path, digest: str) -> Path:
    """Content-addressed name: the same bytes always land on the same path."""
    return directory / f"{digest[:16]}.efi"


def panel_block(url: str, digest: str) -> str:
    return f"URL      {url}\nSHA-256  {digest}"


def fetch_digest(url: str, timeout: float = 60.0) -> str:
    sha = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=timeout) as response:
        for block in iter(lambda: response.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish a UKI for the panel's boot-image URL.")
    parser.add_argument("--uki", required=True, help="the built UKI (.efi) to publish")
    parser.add_argument("--dir", default=str(PUBLISH_DIR), help="served directory")
    parser.add_argument("--base-url", default=BASE_URL, help="public prefix for that directory")
    parser.add_argument("--no-verify", action="store_true", help="skip the HTTPS read-back")
    args = parser.parse_args(argv)

    source = Path(args.uki)
    if not source.is_file():
        print(f"publish-uki: no such file: {source}", file=sys.stderr)
        return 2

    digest = digest_of(source)
    directory = Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = destination(directory, digest)

    if target.is_file() and digest_of(target) == digest:
        print(f"same      {target} (already published)")
    else:
        staging = target.with_suffix(".efi.tmp")
        shutil.copyfile(source, staging)
        os.chmod(staging, 0o644)
        os.replace(staging, target)
        print(f"write     {target} ({source.stat().st_size} bytes, sha256 {digest[:16]}…)")
    sidecar = target.with_name(target.name + ".sha256")
    sidecar.write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    os.chmod(sidecar, 0o644)

    url = f"{args.base_url.rstrip('/')}/{target.name}"
    if not args.no_verify:
        try:
            served = fetch_digest(url)
        except Exception as error:  # noqa: BLE001 — any transport failure is a failed publish
            print(f"publish-uki: the URL does not answer: {error}", file=sys.stderr)
            return 1
        if served != digest:
            print(
                f"publish-uki: served bytes hash {served[:16]}… but the build is {digest[:16]}…",
                file=sys.stderr,
            )
            return 1
        print(f"verify    {url} serves the same bytes")

    print()
    print(panel_block(url, digest))
    print()
    print("next: paste those two lines into Manage → Boot image, then")
    print(f"      ./tools/servury_cycle.py --server-id <id> --action restart --serial-out /tmp/{digest[:8]}-boot.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
