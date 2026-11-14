#!/usr/bin/python3
"""odile-webapp-brand.py — put the Odile name back after a Webapp rebuild.

`hermes update` and `hermes webapp --build-only` rebuild
``apps/desktop/dist-webapp`` from the vendor package: ``index.html`` becomes the
vendor's shell again and ``odile-brand.js`` is gone with it. The installer's
``ensure_webapp_brand()`` links the brand once, at install time; nothing used to
re-run it, so the next rebuild left the vendor's name on a tenant's page
(measured on the demo, 2026-11-14 01:26Z: title ``Hermes``, wordmark
``HERMES AGENT``, palette still Odile's).

This helper is that re-assert. It is installed into the image by
``deploy/image/install-stack.sh`` beside the bytes it copies, and started two
ways, both of them by the bundle changing rather than by a boot:

  * ``odile-webapp-brand.path`` — a systemd path unit on the built shell, so a
    rebuild is renamed/linked within a second of the build;
  * an ``ExecStartPre`` drop-in on ``odile-agent.service`` — a backstop for the
    update flow that rebuilds and then restarts the gateway.

It uses the installer's own bytes (``templates/webapp-brand.js``, as shipped in
the image at ``/usr/local/lib/odile/webapp-brand.js``) and the installer's own
tag format, content-hashed into ``?v=``. It is idempotent by content: a
converged bundle prints ``same`` and writes nothing. The owner and group of the
shell it patches are preserved, so a user-owned bundle stays user-owned.

    /usr/local/lib/odile/odile-webapp-brand.py --home /home/tenant
    /usr/local/lib/odile/odile-webapp-brand.py --dry-run
    /usr/local/lib/odile/odile-webapp-brand.py --self-test

Exit codes: 0 always for a runtime condition (a missing bundle is `wait`, not a
failure — the path unit can fire before the bundle exists, and this helper is an
``ExecStartPre`` that must never keep the gateway down); 2 only for bad
arguments. Real failures print ``error`` on stderr.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path

#: The name the installer uses for the same-origin script it links.
BRAND_ASSET = "odile-brand.js"
#: The installer's marker line. `strip_brand` removes exactly the pair it
#: introduces, which is what makes a second run converge instead of stacking.
BRAND_COMMENT = "<!-- Odile brand (ODL-240): the names a skin cannot reach. -->"
#: A tenant's home; the image's account is `tenant` (install-stack.sh --user).
DEFAULT_HOME = Path("/home/tenant")
#: The image ships the template's bytes here (install-stack.sh installs it).
DEFAULT_ASSET = Path("/usr/local/lib/odile/webapp-brand.js")
#: The bundle's shell, relative to the tenant's home.
SHELL_REL = Path(".hermes/hermes-agent/apps/desktop/dist-webapp/index.html")


def brand_version(script: bytes) -> str:
    """The content hash the tag carries; the installer uses the same 12 hex."""
    return hashlib.sha256(script).hexdigest()[:12]


def brand_tag_lines(version: str) -> tuple[str, str]:
    """The two lines the installer splices; kept identical on purpose."""
    return (
        BRAND_COMMENT,
        # Not `defer`: the theme-font guard and the first-visit skin key must
        # run before the vendor's module graph. A classic parse-blocking 8 KB
        # same-origin script does that; defer would let a skin inject the font
        # link, and let the bundle read an empty appearance, first.
        f'<script src="./{BRAND_ASSET}?v={version}"></script>',
    )


def strip_brand(source: str) -> str:
    """`source` with our injected block removed, i.e. the vendor's bytes."""
    kept: list[str] = []
    skipping = False
    for line in source.splitlines(keepends=True):
        if BRAND_COMMENT in line:
            skipping = True
            continue
        if skipping and BRAND_ASSET in line:
            continue
        skipping = False
        kept.append(line)
    return "".join(kept)


def with_brand(source: str, version: str) -> str:
    """`source` with the brand script linked exactly once, before the shell's
    own pre-paint script so the first-visit paper keys are readable there."""
    base = strip_brand(source)
    opening = base.find("<head>")
    if opening < 0:
        raise ValueError("the Webapp shell has no <head>")
    insert_at = opening + len("<head>")
    newline = base.find("\n", insert_at)
    if newline < 0:
        raise ValueError("the Webapp shell's <head> has no newline")
    next_line = base[newline + 1:]
    indent = next_line[: len(next_line) - len(next_line.lstrip(" \t"))]
    block = "".join(f"{indent}{line}\n" for line in brand_tag_lines(version))
    return base[: newline + 1] + block + base[newline + 1:]


def shell_problem(source: str) -> str | None:
    """Why `source` is not yet a complete shell, or None when it is.

    A rebuild writes `index.html` over a moment; the path unit can fire on the
    first byte. Splicing a tag into a half-written shell would produce a file
    the vendor's next write replaces anyway, but it would also be a write the
    build could interleave with — so a shell without a head and a closing tag is
    left alone and the next trigger tries again.
    """
    if "</html>" not in source:
        return "index.html is not a complete shell yet"
    if "<head>" not in source:
        return "the Webapp shell has no <head>"
    return None


def read_stable(path: Path, *, attempts: int = 20, delay: float = 0.15,
                sleep=time.sleep) -> bytes:
    """Read *path* when it stops changing.

    Two consecutive reads that agree on (inode, size, mtime_ns) are one settle
    time apart, so a build that is still writing the shell is waited out rather
    than read through. Bounded: after `attempts` reads the latest bytes are
    returned and `shell_problem` decides whether they are usable.
    """
    previous: tuple[int, int, int] | None = None
    data = b""
    for attempt in range(attempts):
        st = path.stat()
        key = (st.st_ino, st.st_size, st.st_mtime_ns)
        if key == previous:
            return data
        previous = key
        data = path.read_bytes()
        if attempt < attempts - 1:
            sleep(delay)
    return data


def write_atomic(path: Path, data: bytes, mode: int,
                 owner: tuple[int, int] | None) -> None:
    """Replace *path* with *data* in one rename, keeping `owner` when we can."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        if owner is not None:
            try:
                os.chown(tmp_name, owner[0], owner[1])
            except PermissionError:
                # Not root, and the file is already the caller's: the rename
                # will not change its owner.
                pass
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def apply(bundle: Path, asset_source: Path, *, dry: bool = False,
          say=print) -> bool:
    """Converge *bundle* on the brand. True when something would change.

    Order matters: the asset is written first, so the tag a browser reads never
    points at a file that is not there yet.
    """
    if not bundle.is_file():
        say(f"{'would patch' if dry else 'wait':<12} Webapp brand "
            f"(bundle not built yet: {bundle})")
        return False
    data = asset_source.read_bytes()
    version = brand_version(data)
    source = read_stable(bundle).decode("utf-8")
    problem = shell_problem(source)
    if problem is not None:
        say(f"{'wait':<12} Webapp brand ({problem}: {bundle})")
        return False
    desired = with_brand(source, version)
    target = bundle.parent / BRAND_ASSET
    asset_changed = not target.is_file() or target.read_bytes() != data
    html_changed = desired != source
    if not html_changed and not asset_changed:
        say(f"{'same':<12} Webapp brand ({BRAND_ASSET} v{version}, index.html linked)")
        return False
    say(f"{'would write' if dry else 'write':<12} Webapp brand "
        f"({BRAND_ASSET} v{version}, index.html {'linked' if html_changed else 'unchanged'})")
    if dry:
        return True
    st = bundle.stat()
    owner = (st.st_uid, st.st_gid)
    write_atomic(target, data, 0o644, owner)
    if html_changed:
        write_atomic(bundle, desired.encode("utf-8"), 0o644, owner)
    return True


def self_test() -> int:
    """The pure checks, then one whole convergence in a scratch directory."""
    failures = 0

    def check(good: bool, label: str) -> None:
        nonlocal failures
        if not good:
            failures += 1
        print(f"  {'ok  ' if good else 'FAIL'} {label}")

    template = DEFAULT_ASSET
    if not template.is_file():
        template = Path(__file__).resolve().parent / "webapp-brand.js"
    script = template.read_bytes()
    version = brand_version(script)
    check(len(version) == 12 and all(c in "0123456789abcdef" for c in version),
          "the tag's version is the script's sha256, 12 hex")
    shell = ("<!doctype html>\n<html>\n  <head>\n    <title>Hermes</title>\n"
             "  </head>\n  <body></body>\n</html>\n")
    once = with_brand(shell, version)
    check(with_brand(once, version) == once and once.count(BRAND_ASSET) == 1,
          "the tag is linked exactly once, even on a second splice")
    check(f"{BRAND_ASSET}?v={version}" in once, "the tag carries the content hash")
    check(strip_brand(once) == shell, "stripping the tag restores the vendor shell")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bundle = root / "index.html"
        bundle.write_text(shell, encoding="utf-8")
        asset = root / "webapp-brand.js"
        asset.write_bytes(script)
        first = apply(bundle, asset, say=lambda line: None)
        second = apply(bundle, asset, say=lambda line: None)
        check(first and not second, "the first run writes, the second is `same`")
        check(bundle.read_text(encoding="utf-8").count(BRAND_ASSET) == 1,
              "one tag after the re-assert")
        check((root / BRAND_ASSET).read_bytes() == script,
              "the asset is the shipped bytes, byte for byte")
        missing = apply(root / "nope" / "index.html", asset, say=lambda line: None)
        check(not missing, "a missing bundle is a `wait`, not a write")

    if failures:
        print(f"self-test: {failures} failure(s)")
        return 1
    print("self-test: ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-assert the Odile name in a rebuilt Hermes Webapp.")
    parser.add_argument("--home", default=str(DEFAULT_HOME),
                        help=f"the tenant's home (default {DEFAULT_HOME})")
    parser.add_argument("--bundle", default="",
                        help="the built shell (default <home>/" + str(SHELL_REL) + ")")
    parser.add_argument("--asset", default=str(DEFAULT_ASSET),
                        help=f"the brand bytes (default {DEFAULT_ASSET})")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    parser.add_argument("--quiet", action="store_true",
                        help="print nothing when converged (the gateway's ExecStartPre)")
    parser.add_argument("--self-test", action="store_true", help="test the splice and one run")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()

    def say(line: str) -> None:
        if args.quiet and line.startswith("same"):
            return
        print(line, flush=True)

    bundle = Path(args.bundle) if args.bundle else Path(args.home) / SHELL_REL
    asset = Path(args.asset)
    if not asset.is_file():
        print(f"error        Webapp brand ({asset} is missing; the image did not "
              f"install the bytes)", file=sys.stderr, flush=True)
        return 0
    try:
        apply(bundle, asset, dry=args.dry_run, say=say)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        # Never keep the gateway down for the brand: an ExecStartPre that exits
        # non-zero would. The journal line is the report.
        print(f"error        Webapp brand ({exc})", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
