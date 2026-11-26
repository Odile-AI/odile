#!/usr/bin/python3
"""Serve /releases/* on https://odile.fyi from /srv/odile-releases, publish into it, prune it.

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

    ./deploy/caddy/deploy_releases.py             apply the Caddy block
    ./deploy/caddy/deploy_releases.py --dry-run   show the splice plan only

ODL-356: the same directory also holds the build images, and nothing pruned
them. `disks/` and `uki/` name every file by the first 16 hex of its own
SHA-256 (`<digest[:16]>.raw`, `.efi`) with a `.sha256` sidecar, so a rebuild
never overwrites the file a machine is pinned to — and, until this card, every
build stayed forever (85.5 GB measured in `disks/` on 2026-11-25). Two verbs own
that now:

    ./deploy/caddy/deploy_releases.py --publish-disk out/odile-tenant-sealed.raw
        copy the build in sparse-preserving, write the sidecar, print the
        panel's URL + SHA-256; refuses before the first write when free space
        on the volume is below the 16 GB floor, and prints the measured number.
        The floor is one build plus headroom, not a fence outside disk care's
        90 % band — prune between builds (docs/runbooks/release.md §3)

    ./deploy/caddy/deploy_releases.py --retention             dry run
    ./deploy/caddy/deploy_releases.py --retention --apply     prune
        keep the disk image and UKI each live machine's panel fields name —
        the panel *is* the live release manifest — and delete every other
        image in `disks/` and `uki/`. The newest kept pair is the current
        release, the others are its predecessors (docs/runbooks/release.md).
        Refuses before the first delete when a named file is missing or hashes
        to something other than the panel's SHA-256.

`--pins FILE` supplies those panel facts as a TSV (`server`, `disk`,
`disk_sha256`, `uki`, `uki_sha256`; `-` for an unset field) for a machine on
another account or when the panel session is unavailable. `--self-test` drives
publish, the free-space refusal and the prune over a fixture directory, with no
network and no panel.

Exit codes: 0 success (changed, already converged, or a clean dry run), 1 a
failed apply that was rolled back, 2 a bad input, a missing prerequisite or a
refused write.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import shutil
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
SERVED = BASE + "/releases"

# The publish floor, in GiB. A built image is 8 GiB apparent and ~4.8-8 GiB on
# disk (`du` on the 2026-11-25 directory), so 16 GiB lets one copy complete
# with ~8 GiB spare. It is NOT outside the disk-care bands: on the measured
# 187.3 GiB volume, 16 GiB free is 92 % used (18.73 GiB free is 90 %, 9.36 GiB
# is 95 %), and a publish starting at the floor ends at ~9 GiB free / 96 % —
# the no-quarantine band. Prune between builds (docs/runbooks/release.md §3);
# a floor that stays outside the 90 % band would be ~26 GiB.
FREE_FLOOR_GB = 16.0
DISK_SUFFIX = ".raw"
UKI_SUFFIX = ".efi"
SIDECAR_SUFFIX = ".sha256"


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


# --------------------------------------------------------------------------
# The release directory (ODL-356)
# --------------------------------------------------------------------------


def human_gb(count: int) -> str:
    return f"{count / 1024 ** 3:.1f} GB"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def real_bytes(path: Path) -> int:
    """Bytes the file actually occupies — an 8 GiB image is half sparse."""
    return path.stat().st_blocks * 512


def sidecar_for(image: Path) -> Path:
    return image.with_name(image.name + SIDECAR_SUFFIX)


def write_sidecar(image: Path, digest: str) -> None:
    sidecar = sidecar_for(image)
    sidecar.write_text(f"{digest}  {image.name}\n", encoding="utf-8")
    os.chmod(sidecar, 0o644)


def free_bytes(directory: Path) -> int:
    """Free space on the volume that will hold `directory`, before creating it."""
    probe = directory
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def head_check(url: str, size: int) -> str | None:
    """None when the served URL answers 200 with this size, else why not."""
    request = urllib.request.Request(url, method="HEAD", headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            length = response.headers.get("Content-Length")
            if response.status != 200:
                return f"HTTP {response.status}"
            if length is not None and int(length) != size:
                return f"content-length {length} but the file is {size} bytes"
            return None
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}"
    except OSError as error:  # a refused connection, a timeout, a DNS failure
        return str(error)


def publish_disk(
    source: Path,
    destination: Path,
    *,
    floor_gb: float = FREE_FLOOR_GB,
    dry_run: bool = False,
    verify: bool = True,
) -> int:
    """Copy a built disk image into the served directory, sparse, with its sidecar.

    The free-space guard runs before the first write — including the `mkdir`
    that would create the directory — because a publish at the wall is the
    failure this verb exists to stop (`docs/ops/DISK-CARE.md`).
    """
    if not source.is_file():
        print(f"error: no such file: {source}", file=sys.stderr)
        return 2

    free = free_bytes(destination)
    print(
        f"{'free':<10}{human_gb(free)} on {destination.parent} (publish floor {floor_gb:g} GB)",
        flush=True,
    )
    if free < floor_gb * 1024 ** 3:
        print(
            f"{'refused':<10}{human_gb(free)} free < {floor_gb:g} GB floor — nothing written",
            file=sys.stderr,
        )
        return 2

    digest = sha256_of(source)
    target = destination / f"{digest[:16]}{DISK_SUFFIX}"
    if target.is_file():
        if sha256_of(target) != digest:
            print(
                f"error: {target} already exists with other bytes — a name collision "
                "is impossible for a content-addressed file",
                file=sys.stderr,
            )
            return 1
        print(f"{'same':<10}{target} (already published)")
    elif dry_run:
        print(f"{'would':<10}copy {source} to {target} (sha256 {digest[:16]}…)")
        print(f"{'would':<10}write {sidecar_for(target).name}")
        return 0
    else:
        destination.mkdir(parents=True, exist_ok=True)
        staging = target.with_name("." + target.name + ".new")
        staging.unlink(missing_ok=True)
        copied = run(["cp", "--sparse=always", str(source), str(staging)])
        if copied.returncode != 0:
            staging.unlink(missing_ok=True)
            print(f"error: cp failed: {copied.stderr.strip()}", file=sys.stderr)
            return 1
        os.chmod(staging, 0o644)
        os.replace(staging, target)
        print(f"{'write':<10}{target} ({human_gb(real_bytes(target))} on disk, sha256 {digest[:16]}…)")

    written = sha256_of(target)
    if written != digest:
        print(
            f"error: the published copy hashes {written[:16]}… but the build is {digest[:16]}…",
            file=sys.stderr,
        )
        return 1
    if not dry_run:
        write_sidecar(target, digest)
        print(f"{'sidecar':<10}{sidecar_for(target)}")

    url = f"{SERVED}/disks/{target.name}"
    if verify and not dry_run:
        problem = head_check(url, target.stat().st_size)
        if problem:
            print(f"error: {url} does not answer as published: {problem}", file=sys.stderr)
            return 1
        print(f"{'verify':<10}{url} -> 200, {target.stat().st_size} bytes")

    print()
    print(f"URL      {url}")
    print(f"SHA-256  {digest}")
    print()
    print("next: paste those two lines into Manage → Disk image, then recycle")
    print("      (docs/runbooks/recycle.md); prune with --retention.")
    print(f"{'free':<10}{human_gb(free_bytes(destination))} after the publish")
    return 0


def read_pins_file(path: Path) -> list[dict[str, str | None]]:
    """The panel facts as a TSV: server, disk, disk_sha256, uki, uki_sha256."""
    entries: list[dict[str, str | None]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 5:
            raise ValueError(
                f"{path}:{number}: expected 5 columns (server disk disk_sha256 uki uki_sha256), "
                f"found {len(fields)}"
            )
        server, disk, disk_sha, uki, uki_sha = fields
        entries.append(
            {
                "server": server,
                "disk": None if disk == "-" else disk,
                "disk_sha256": None if disk_sha == "-" else disk_sha,
                "uki": None if uki == "-" else uki,
                "uki_sha256": None if uki_sha == "-" else uki_sha,
            }
        )
    if not entries:
        raise ValueError(f"{path}: no machine named")
    return entries


def panel_pins() -> list[dict[str, str | None]]:
    """Every machine's Boot image / Disk image pair, read from the panel.

    The panel's fields are the live release manifest: a URL plus the SHA-256
    the machine will check, so a file they name must keep answering. The read
    is the same one `tools/servury_panel.py` makes for `--recycle`; a session
    that has expired serves the login page, which names no image, and the
    caller refuses rather than guessing.
    """
    sys.path.insert(0, str(REPO / "tools"))
    import servury_panel  # noqa: PLC0415 — one implementation of the panel's HTML

    opener, _ = servury_panel.opener_for(servury_panel.COOKIE_FILE)
    servers = sorted(servury_panel.owned_servers(opener))
    if not servers:
        raise RuntimeError(
            "the panel dashboard named no machine — the session in "
            f"{servury_panel.COOKIE_FILE} has expired; pass --pins"
        )
    entries: list[dict[str, str | None]] = []
    for server in servers:
        page = servury_panel.get(opener, f"{servury_panel.BASE}/manage/{server}/admin/")
        pairs = servury_panel.image_pairs(page)
        if not pairs["disk_url"] and not pairs["boot_url"]:
            continue
        entries.append(
            {
                "server": server,
                "disk": pairs["disk_url"] or None,
                "disk_sha256": pairs["disk_sha256"] or None,
                "uki": pairs["boot_url"] or None,
                "uki_sha256": pairs["boot_sha256"] or None,
            }
        )
    if not entries:
        raise RuntimeError(
            "no machine on the panel names a boot or disk image — the session has "
            "expired or the fields are cleared; pass --pins"
        )
    return entries


def pin_file(value: str, directory: str, suffix: str) -> tuple[str, str]:
    """(directory, filename) for a served URL or a bare filename."""
    text = value.strip()
    for prefix in (SERVED + "/", "releases/"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    if "/" in text:
        where, _, name = text.rpartition("/")
        return where, name
    return directory, text


def kept_files(
    root: Path,
    entries: list[dict[str, str | None]],
    directory: str,
    suffix: str,
    field: str,
    digest_field: str,
) -> tuple[dict[str, str], list[str], list[str]]:
    """{filename: digest} the pins name in this directory, problems, and out-of-rule values."""
    keep: dict[str, str] = {}
    problems: list[str] = []
    outside: list[str] = []
    for entry in entries:
        value = entry.get(field)
        if not value:
            continue
        expected = (entry.get(digest_field) or "").strip().lower()
        where, name = pin_file(value, directory, suffix)
        if where != directory:
            outside.append(f"{entry['server']} {value}")
            continue
        if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            problems.append(f"{entry['server']}: {name} has no usable SHA-256 ({expected or 'empty'})")
            continue
        if not name.endswith(suffix) or name[: -len(suffix)] != expected[:16]:
            problems.append(
                f"{entry['server']}: {name} is not {expected[:16]}{suffix} — the panel's "
                "name and hash disagree"
            )
            continue
        path = root / directory / name
        if not path.is_file():
            problems.append(f"{entry['server']}: the panel names {name}, which is not in {path.parent}")
            continue
        actual = sha256_of(path)
        if actual != expected:
            problems.append(f"{entry['server']}: {name} hashes {actual[:16]}… but the panel says {expected[:16]}…")
            continue
        if name in keep and keep[name] != expected:
            problems.append(f"{name} is named with two different hashes")
            continue
        keep[name] = expected
    return keep, problems, outside


def plan_directory(directory: Path, suffix: str, keep: dict[str, str]) -> tuple[list[Path], list[Path]]:
    """(images to delete, sidecars to delete); anything else is left alone."""
    files = sorted(p for p in directory.iterdir() if p.is_file())
    images = [p for p in files if p.name.endswith(suffix)]
    doomed_images = [p for p in images if p.name not in keep]
    doomed_sidecars = [
        p
        for p in files
        if p.name.endswith(suffix + SIDECAR_SUFFIX) and p.name[: -len(SIDECAR_SUFFIX)] not in keep
    ]
    return doomed_images, doomed_sidecars


def retention(root: Path, entries: list[dict[str, str | None]], *, apply: bool) -> int:
    """Keep the pairs a live machine names; delete every other image. Dry by default."""
    disks = root / "disks"
    ukis = root / "uki"
    for directory in (disks, ukis):
        if not directory.is_dir():
            print(f"error: missing release directory {directory}", file=sys.stderr)
            return 2

    keep_disks, problems, outside_disks = kept_files(root, entries, "disks", DISK_SUFFIX, "disk", "disk_sha256")
    keep_ukis, uki_problems, outside_ukis = kept_files(root, entries, "uki", UKI_SUFFIX, "uki", "uki_sha256")
    problems += uki_problems
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        print(f"{'refused':<10}a live pin does not match the directory — nothing deleted", file=sys.stderr)
        return 2

    # An empty keep set is not a licence to empty the directory: a `--pins`
    # file whose image fields are all `-` (or a panel read that silently
    # returned nothing) would otherwise delete every image a machine might need.
    for directory, keep, suffix, label in (
        (disks, keep_disks, DISK_SUFFIX, "disk"),
        (ukis, keep_ukis, UKI_SUFFIX, "UKI"),
    ):
        images = [p for p in directory.iterdir() if p.name.endswith(suffix)]
        if images and not keep:
            print(
                f"error: no live pin names a {label} image, but {directory} holds {len(images)} — "
                "refusing to empty it",
                file=sys.stderr,
            )
            print(f"{'refused':<10}the keep set is empty — nothing deleted", file=sys.stderr)
            return 2

    doomed: list[Path] = []
    before = 0
    for directory, suffix, keep in ((disks, DISK_SUFFIX, keep_disks), (ukis, UKI_SUFFIX, keep_ukis)):
        doomed_images, doomed_sidecars = plan_directory(directory, suffix, keep)
        doomed += doomed_images + doomed_sidecars
        before += sum(real_bytes(p) for p in directory.iterdir() if p.is_file())
        print(
            f"{'keep':<10}{len(keep)} image(s) in {directory.name}/ of "
            f"{len([p for p in directory.iterdir() if p.name.endswith(suffix)])}"
        )

    # The newest kept disk is the current release; the rest are its predecessors.
    ordered = sorted(keep_disks, key=lambda name: (disks / name).stat().st_mtime, reverse=True)
    for index, name in enumerate(ordered):
        servers = sorted({str(e["server"]) for e in entries if e.get("disk") and pin_file(str(e["disk"]), "disks", DISK_SUFFIX)[1] == name})
        pairs = sorted({pin_file(str(e["uki"]), "uki", UKI_SUFFIX)[1] for e in entries if e.get("uki") and e["server"] in servers})
        label = "current" if index == 0 else f"predecessor {index}"
        print(
            f"{'pair':<10}{label:<16}{name} + {', '.join(pairs) or '(no UKI)'} "
            f"(live on {', '.join(servers)})"
        )
    for outside in outside_disks + outside_ukis:
        print(f"{'outside':<10}{outside} (not in this directory; left alone)")

    doomed_images = [p for p in doomed if p.name.endswith(DISK_SUFFIX)]
    doomed_ukis = [p for p in doomed if p.name.endswith(UKI_SUFFIX)]
    doomed_sidecars = [p for p in doomed if p.name.endswith(SIDECAR_SUFFIX)]
    doomed_bytes = sum(real_bytes(p) for p in doomed)
    print(
        f"{'prune':<10}{len(doomed_images)} disk image(s) and "
        f"{len([p for p in doomed_sidecars if p.name.endswith(DISK_SUFFIX + SIDECAR_SUFFIX)])} sidecar(s), "
        f"{human_gb(sum(real_bytes(p) for p in doomed_images))}"
    )
    print(
        f"{'prune':<10}{len(doomed_ukis)} UKI image(s) and "
        f"{len([p for p in doomed_sidecars if p.name.endswith(UKI_SUFFIX + SIDECAR_SUFFIX)])} sidecar(s), "
        f"{human_gb(sum(real_bytes(p) for p in doomed_ukis))}"
    )
    if not doomed:
        print(f"{'same':<10}nothing to prune ({len(keep_disks)} disk image(s) kept)")
        return 0

    free_before = free_bytes(root)
    for path in doomed:
        if apply:
            real = real_bytes(path)
            path.unlink()
            print(f"{'rm':<10}{path} ({human_gb(real)})")
        else:
            print(f"{'would':<10}rm {path} ({human_gb(real_bytes(path))})")

    if not apply:
        print(
            f"{'dry-run':<10}{human_gb(doomed_bytes)} would be freed; "
            "nothing deleted (run again with --apply)"
        )
        return 0

    after = 0
    for directory in (disks, ukis):
        after += sum(real_bytes(p) for p in directory.iterdir() if p.is_file())
    for name, expected in sorted(keep_disks.items()):
        actual = sha256_of(disks / name)
        if actual != expected:
            print(f"error: kept {name} hashes {actual[:16]}… after the prune", file=sys.stderr)
            return 1
        print(f"{'kept':<10}{name} sha256 {actual} (byte-identical to the panel)")
    for name, expected in sorted(keep_ukis.items()):
        actual = sha256_of(ukis / name)
        if actual != expected:
            print(f"error: kept {name} hashes {actual[:16]}… after the prune", file=sys.stderr)
            return 1
        print(f"{'kept':<10}{name} sha256 {actual} (byte-identical to the panel)")
    print(f"{'freed':<10}{human_gb(doomed_bytes)}; images {human_gb(before)} -> {human_gb(after)}")
    print(f"{'free':<10}{human_gb(free_before)} -> {human_gb(free_bytes(root))} on {root}")
    return 0


def write_pins_file(path: Path, entries: list[dict[str, str | None]]) -> None:
    """Write the panel facts as the TSV `--pins` reads, one machine per line."""
    lines = [
        "# server\tdisk\t\tdisk_sha256\t\tuki\t\tuki_sha256 — written by "
        "deploy_releases.py --pins-out",
    ]
    for entry in entries:
        lines.append(
            "\t".join(
                [
                    str(entry["server"]),
                    entry["disk"] or "-",
                    entry["disk_sha256"] or "-",
                    entry["uki"] or "-",
                    entry["uki_sha256"] or "-",
                ]
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o644)


def self_test() -> int:
    """Drive publish, the free-space refusal and the prune over a fixture tree."""
    import contextlib
    import io
    import tempfile

    checks = 0
    failures = 0

    def check(label: str, condition: bool) -> None:
        nonlocal checks, failures
        checks += 1
        if not condition:
            failures += 1
            print(f"FAIL  {label}")

    def quietly(function) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = function()
        return code, buffer.getvalue()

    with tempfile.TemporaryDirectory(prefix="odile-releases-selftest-") as scratch:
        root = Path(scratch)
        disks = root / "disks"
        ukis = root / "uki"
        disks.mkdir()
        ukis.mkdir()

        def fixture(directory: Path, suffix: str, data: bytes) -> Path:
            # Content-addressed, like the real directory: the name is the hash.
            digest = hashlib.sha256(data).hexdigest()
            path = directory / f"{digest[:16]}{suffix}"
            path.write_bytes(data)
            write_sidecar(path, digest)
            return path

        keep_image = fixture(disks, DISK_SUFFIX, b"A" * 4096)
        stale_image = fixture(disks, DISK_SUFFIX, b"B" * 4096)
        stale_sidecar = sidecar_for(stale_image)
        keep_uki = fixture(ukis, UKI_SUFFIX, b"U" * 1024)
        stale_uki = fixture(ukis, UKI_SUFFIX, b"V" * 1024)
        pins = root / "pins.tsv"
        pins.write_text(
            f"bench\t{keep_image.name}\t{sha256_of(keep_image)}\t{keep_uki.name}\t{sha256_of(keep_uki)}\n",
            encoding="utf-8",
        )
        entries = read_pins_file(pins)
        check("the pins file carries one live pair", len(entries) == 1)

        code, output = quietly(lambda: retention(root, entries, apply=False))
        check("a dry run exits 0", code == 0)
        check("a dry run plans the stale image", "would" in output and stale_image.name in output)
        check("a dry run deletes nothing", stale_image.is_file() and keep_image.is_file())

        code, output = quietly(lambda: retention(root, entries, apply=True))
        check("the apply exits 0", code == 0)
        check("the kept pair survives", keep_image.is_file() and keep_uki.is_file())
        check("its sidecar survives", sidecar_for(keep_image).is_file())
        check("the stale image and its sidecar are gone", not stale_image.exists() and not stale_sidecar.exists())
        check("the stale UKI is gone", not stale_uki.exists())
        check("the kept image is reported byte-identical", "byte-identical to the panel" in output)

        code, output = quietly(lambda: retention(root, entries, apply=True))
        check("a second apply exits 0", code == 0)
        check("a second apply is a no-op", "nothing to prune" in output)

        missing = root / "missing.tsv"
        missing.write_text(f"bench\tcccccccccccccccc.raw\t{'c' * 64}\t-\t-\n", encoding="utf-8")
        code, output = quietly(lambda: retention(root, read_pins_file(missing), apply=True))
        check("a pin naming a missing file refuses", code == 2 and "refused" in output)
        check("the refusal deleted nothing", keep_image.is_file())

        wrong = root / "wrong.tsv"
        wrong.write_text(f"bench\t{keep_image.name}\t{'d' * 64}\t-\t-\n", encoding="utf-8")
        code, _ = quietly(lambda: retention(root, read_pins_file(wrong), apply=True))
        check("a pin whose hash disagrees refuses", code == 2)

        blank = root / "blank.tsv"
        blank.write_text("bench\t-\t-\t-\t-\n", encoding="utf-8")
        code, output = quietly(lambda: retention(root, read_pins_file(blank), apply=True))
        check("a keep set that names nothing refuses", code == 2 and "keep set is empty" in output)
        check("the empty keep set deleted nothing", keep_image.is_file() and keep_uki.is_file())

        source = root / "src.raw"
        source.write_bytes(b"S" * (1024 * 1024))
        code, output = quietly(
            lambda: publish_disk(source, disks, floor_gb=0, dry_run=False, verify=False)
        )
        published = disks / f"{sha256_of(source)[:16]}.raw"
        check("a publish exits 0", code == 0)
        check("the image lands under its own hash", published.is_file())
        check("the sidecar names the image and its digest", sidecar_for(published).read_text().strip() == f"{sha256_of(source)}  {published.name}")

        code, output = quietly(
            lambda: publish_disk(source, disks, floor_gb=0, dry_run=False, verify=False)
        )
        check("publishing the same bytes again is a no-op", code == 0 and "same" in output)

        other = root / "other.raw"
        other.write_bytes(b"O" * (1024 * 1024))
        code, output = quietly(
            lambda: publish_disk(other, disks, floor_gb=10 ** 6, dry_run=False, verify=False)
        )
        check("the free-space floor refuses", code == 2 and "refused" in output)
        check("the refused publish wrote nothing", not (disks / f"{sha256_of(other)[:16]}.raw").exists())
        check("the refusal prints the measured number", "GB" in output)

        code, output = quietly(
            lambda: publish_disk(other, disks, floor_gb=0, dry_run=True, verify=False)
        )
        check("a publish dry run writes nothing", code == 0 and "would" in output)
        check("the dry run left no image", not (disks / f"{sha256_of(other)[:16]}.raw").exists())

    if failures:
        print(f"self-test: FAIL ({failures} of {checks} checks)")
        return 1
    print(f"self-test: PASS ({checks} checks)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="show the splice plan (or a publish) only")
    parser.add_argument("--publish-disk", metavar="PATH", help="publish a built disk image into <dir>/disks")
    parser.add_argument("--retention", action="store_true", help="plan or apply the release-directory retention rule")
    parser.add_argument("--apply", action="store_true", help="with --retention: delete (the default is a dry run)")
    parser.add_argument("--pins", metavar="FILE", help="panel facts as TSV instead of reading the panel")
    parser.add_argument("--pins-out", metavar="FILE", help="write the panel facts as TSV and exit")
    parser.add_argument("--dir", default=str(RELEASES_DIR), help="the served release directory")
    parser.add_argument("--min-free-gb", type=float, default=FREE_FLOOR_GB, help="the publish floor in GiB")
    parser.add_argument("--no-verify", action="store_true", help="with --publish-disk: skip the HTTPS read-back")
    parser.add_argument("--self-test", action="store_true", help="drive publish and the prune over a fixture tree")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    root = Path(args.dir)
    if args.publish_disk:
        return publish_disk(
            Path(args.publish_disk),
            root / "disks",
            floor_gb=args.min_free_gb,
            dry_run=args.dry_run,
            verify=not args.no_verify,
        )
    if args.pins_out:
        try:
            entries = read_pins_file(Path(args.pins)) if args.pins else panel_pins()
        except (OSError, ValueError, RuntimeError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        write_pins_file(Path(args.pins_out), entries)
        print(f"{'write':<10}{args.pins_out} ({len(entries)} machine(s))")
        for entry in entries:
            print(f"          {entry['server']}  {entry['disk'] or '-'}  {entry['uki'] or '-'}")
        return 0
    if args.retention:
        try:
            entries = read_pins_file(Path(args.pins)) if args.pins else panel_pins()
        except (OSError, ValueError, RuntimeError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        return retention(root, entries, apply=args.apply)

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
