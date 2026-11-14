#!/usr/bin/python3
"""odile-webapp-brand.py — put the Odile surface back after a Webapp rebuild.

`hermes update` and `hermes webapp --build-only` rebuild
``apps/desktop/dist-webapp`` from the vendor package: ``index.html`` becomes the
vendor's shell again, ``odile-brand.js`` is gone with it, and so are the ODL-500
chrome stylesheet and script. The installer links the brand once, at install
time; nothing used to re-run it, so the next rebuild left the vendor's name on a
tenant's page (measured on the demo, 2026-11-14 01:26Z: title ``Hermes``,
wordmark ``HERMES AGENT``), and the same rebuild also dropped the paper, the
embedded Cormorant Garamond subset and the one Verify link (measured on the
same tenant, 2026-11-14 09:01Z: ``--paper-v0`` #F6F0E4 and the system serif
stack).

This helper is that re-assert for the whole surface, not just the name:

  * the brand (ODL-240) — ``odile-brand.js`` and its one tag;
  * the chrome (ODL-330/ODL-500) — ``odile-demo-chrome.css`` and
    ``odile-demo-chrome.js`` and their two tags.

It is installed into the image by ``deploy/image/install-stack.sh`` beside the
bytes it copies, and started two ways, both of them by the bundle changing
rather than by a boot:

  * ``odile-webapp-brand.path`` — a systemd path unit on the built shell and
    its three assets, so a rebuild is renamed/linked within a second of the
    build;
  * an ``ExecStartPre`` drop-in on ``odile-agent.service`` — a backstop for the
    update flow that rebuilds and then restarts the gateway.

It uses the image's own bytes (``webapp-brand.js`` and the two
``webapp-chrome.*`` files, installed at ``/usr/local/lib/odile/``), the
installer's own tag format for the brand, content-hashed into ``?v=``, and the
demo deployer's tag format for the chrome (script before stylesheet, one
combined content hash). It is idempotent by content: a converged bundle prints
``same`` and writes nothing. The owner and group of the shell it patches are
preserved, so a user-owned bundle stays user-owned.

A rebuild also restores the vendor's three tab links (ODL-505). The same run
therefore copies the installer's two icon files — ``favicon.ico`` and
``apple-touch-icon.png``, from the icon root it wrote at install — into the
bundle under Odile names and repoints the three links, exactly as the installer
does. A machine whose icon root is missing keeps the vendor links and prints
``wait``; the brand splice still happens.

    /usr/local/lib/odile/odile-webapp-brand.py --home /home/tenant
    /usr/local/lib/odile/odile-webapp-brand.py --dry-run
    /usr/local/lib/odile/odile-webapp-brand.py --self-test

Exit codes: 0 always for a runtime condition (a missing bundle is `wait`, not a
failure — the path unit can fire before the bundle exists, and this helper is an
``ExecStartPre`` that must never keep the gateway down); 2 only for bad
arguments. A missing chrome asset is a named ``error`` and the brand still
converges. Real failures print ``error`` on stderr.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tempfile
import time
from pathlib import Path

#: The name the installer uses for the same-origin script it links.
BRAND_ASSET = "odile-brand.js"
#: The installer's marker line. `strip_brand` removes exactly the pair it
#: introduces, which is what makes a second run converge instead of stacking.
BRAND_COMMENT = "<!-- Odile brand (ODL-240): the names a skin cannot reach. -->"
#: The chrome the image carries: (installed source name, name in the bundle).
CHROME_ASSETS = (
    ("webapp-chrome.css", "odile-demo-chrome.css"),
    ("webapp-chrome.js", "odile-demo-chrome.js"),
)
#: The demo deployer's marker line, kept identical so `strip_chrome` also
#: removes a block an older deploy wrote.
CHROME_COMMENT = "<!-- Odile demo chrome (ODL-330): the vendor's dev labels, hidden. -->"
#: A tenant's home; the image's account is `tenant` (install-stack.sh --user).
DEFAULT_HOME = Path("/home/tenant")
#: The image ships the template's bytes here (install-stack.sh installs it).
DEFAULT_ASSET = Path("/usr/local/lib/odile/webapp-brand.js")
DEFAULT_CHROME_CSS = Path("/usr/local/lib/odile/webapp-chrome.css")
DEFAULT_CHROME_JS = Path("/usr/local/lib/odile/webapp-chrome.js")
#: The bundle's shell, relative to the tenant's home.
SHELL_REL = Path(".hermes/hermes-agent/apps/desktop/dist-webapp/index.html")
#: The icon root the installer writes (ODL-505). A rebuild drops the shell's
#: three tab links back to the vendor's mark too, so the re-assert carries the
#: same two files and the same three lines the installer splices.
DEFAULT_ICONS = Path("/usr/local/lib/odile/icons")
ICON_ASSETS = (
    ("favicon.ico", "odile-favicon.ico"),
    ("apple-touch-icon.png", "odile-apple-touch-icon.png"),
)
ICON_COMMENT = "<!-- Odile icon (ODL-505): the tab this machine shows, not the vendor's. -->"
ICON_LINK_LINES = (
    '<link rel="icon" sizes="16x16 32x32 48x48" href="./odile-favicon.ico" />',
    '<link rel="apple-touch-icon" sizes="180x180" href="./odile-apple-touch-icon.png" />',
    '<link rel="shortcut icon" href="./odile-favicon.ico" />',
)
#: The vendor lines this rewrite owns: one of the three tab relations, pointed
#: at the vendor's own ./apple-touch-icon.png. Matched whole-line so a
#: modulepreload or a font preload is never touched.
VENDOR_ICON_LINE = re.compile(
    r'^\s*<link\s+rel="(?:icon|apple-touch-icon|shortcut icon)"[^>]*'
    r'href="\./apple-touch-icon\.png"[^>]*>\s*$'
)


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


def chrome_version(assets: list[bytes]) -> str:
    """A short content hash over both chrome files, for the same `?v=` reason."""
    return hashlib.sha256(b"\0".join(assets)).hexdigest()[:12]


def chrome_tag_lines(version: str) -> tuple[str, str, str]:
    """The demo deployer's block: marker, script, stylesheet."""
    return (
        CHROME_COMMENT,
        # Script first, and not `defer`: the theme-font guard must be installed
        # before the vendor's module graph runs, and a classic same-origin
        # script is not delayed by the stylesheet fetch behind it.
        f'<script src="./{CHROME_ASSETS[1][1]}?v={version}"></script>',
        f'<link rel="stylesheet" href="./{CHROME_ASSETS[0][1]}?v={version}" />',
    )


def strip_brand(source: str) -> str:
    """`source` with our injected brand block removed, i.e. the vendor's bytes."""
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


def strip_chrome(source: str) -> str:
    """`source` with the chrome block removed, i.e. the shell before ODL-330.

    Only the comment line and the lines that name the chrome assets are
    removed, so any other line of the vendor shell is left exactly as served.
    """
    kept: list[str] = []
    skipping = False
    for line in source.splitlines(keepends=True):
        if CHROME_COMMENT in line:
            skipping = True
            continue
        if skipping and "odile-demo-chrome" in line:
            continue
        skipping = False
        kept.append(line)
    return "".join(kept)


def _preamble_insert(base: str, anchors: tuple[str, ...] = ()) -> int:
    """Where a block goes: after `<head>`'s newline, or after the icon block.

    The icon block is the first thing under `<head>` (ODL-505). A splice that
    runs without the icon root must not move it below the brand line, or a
    machine installed before the icons existed would see one reorder write.
    """
    opening = base.find("<head>")
    if opening < 0:
        raise ValueError("the Webapp shell has no <head>")
    newline = base.find("\n", opening + len("<head>"))
    if newline < 0:
        raise ValueError("the Webapp shell's <head> has no newline")
    if not anchors:
        return newline + 1
    anchor = next((base.index(name) for name in anchors if name in base), -1)
    if anchor < 0:
        return newline + 1
    lines = base.splitlines(keepends=True)
    offset = 0
    end = newline + 1
    seen = False
    for line in lines:
        offset += len(line)
        if not seen:
            if ICON_COMMENT in line:
                seen = True
                end = offset
            continue
        if any(name in line for name in anchors):
            end = offset
        else:
            break
    return end


def with_brand(source: str, version: str) -> str:
    """`source` with the brand script linked exactly once, before the shell's
    own pre-paint script so the first-visit paper keys are readable there."""
    base = strip_brand(source)
    insert_at = _preamble_insert(
        base, tuple(bundle_name for _, bundle_name in ICON_ASSETS))
    next_line = base[insert_at:]
    indent = next_line[: len(next_line) - len(next_line.lstrip(" \t"))]
    block = "".join(f"{indent}{line}\n" for line in brand_tag_lines(version))
    return base[:insert_at] + block + base[insert_at:]


def strip_icons(source: str) -> str:
    """`source` with our injected icon block removed, i.e. the shell before it."""
    kept: list[str] = []
    skipping = False
    for line in source.splitlines(keepends=True):
        if ICON_COMMENT in line:
            skipping = True
            continue
        if skipping and any(bundle_name in line for _, bundle_name in ICON_ASSETS):
            continue
        skipping = False
        kept.append(line)
    return "".join(kept)


def drop_vendor_icons(source: str) -> str:
    """`source` without the vendor's three tab links (the Nous mark)."""
    return "".join(
        line for line in source.splitlines(keepends=True)
        if not VENDOR_ICON_LINE.match(line)
    )


def with_icons(source: str) -> str:
    """`source` with the three tab links pointing at Odile's files, once.

    The installer's splice, line for line: our block is removed, the vendor's
    three lines are dropped, and the reviewed three are inserted at the top of
    `<head>`. A rebuild converges on the next run; a second run changes nothing.
    """
    base = drop_vendor_icons(strip_icons(source))
    opening = base.find("<head>")
    if opening < 0:
        raise ValueError("the Webapp shell has no <head>")
    insert_at = opening + len("<head>")
    newline = base.find("\n", insert_at)
    if newline < 0:
        raise ValueError("the Webapp shell's <head> has no newline")
    next_line = base[newline + 1:]
    indent = next_line[: len(next_line) - len(next_line.lstrip(" \t"))]
    block = "".join(f"{indent}{line}\n" for line in (ICON_COMMENT, *ICON_LINK_LINES))
    return base[: newline + 1] + block + base[newline + 1:]


def icon_files(root: Path) -> list[tuple[str, bytes]]:
    """The two files the tab links name, read from the installer's icon root.

    Empty when the root or either file is missing — a machine installed before
    ODL-505 keeps the vendor's links until the installer runs again, and the
    brand splice still happens.
    """
    files = []
    for source_name, bundle_name in ICON_ASSETS:
        path = root / source_name
        if not path.is_file():
            return []
        files.append((bundle_name, path.read_bytes()))
    return files


def with_chrome(source: str, version: str) -> str:
    """`source` with the chrome stylesheet and script linked exactly once."""
    base = strip_chrome(source)
    head, sep, tail = base.partition("</head>")
    if not sep:
        raise ValueError("the Webapp shell has no </head>")
    newline = head.rfind("\n")
    if newline < 0:
        raise ValueError("the Webapp shell's </head> has no newline")
    indent = head[newline + 1:]
    if indent.strip():
        raise ValueError(f"the Webapp shell's </head> is not on its own line: {indent!r}")
    block = "".join(f"{indent}  {line}\n" for line in chrome_tag_lines(version))
    return head[: newline + 1] + block + indent + sep + tail


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
    if "</head>" not in source:
        return "the Webapp shell has no </head>"
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


def read_chrome(chrome_sources, say) -> list[tuple[str, bytes]]:
    """The chrome bytes as (bundle name, data), or `[]` when a source is absent.

    Both files and both tags are one unit: a stylesheet without its script
    would leave the font guard uninstalled, and a script without its stylesheet
    would paint the vendor's palette. A missing source is a named `error` and
    the brand still converges — this helper is an ExecStartPre.
    """
    found: list[tuple[str, bytes]] = []
    for (source_name, target_name), source in zip(CHROME_ASSETS, chrome_sources):
        if not source.is_file():
            say(f"error        Webapp surface ({source} is missing; the image did "
                f"not install {source_name})")
            return []
        found.append((target_name, source.read_bytes()))
    return found


def apply(bundle: Path, asset_source: Path, chrome_sources=(),
          *, icons_root: Path | None = None,
          dry: bool = False, say=print) -> bool:
    """Converge *bundle* on the brand, the chrome and the tab. True when
    something would change.

    Order matters: every asset is written first, so the tags a browser reads
    never point at a file that is not there yet. The icon files are written
    before the shell too, for the same reason. `icons_root` is the installer's
    icon root; when it is absent or incomplete the brand splice still runs and
    the three tab links are left exactly as the build (or the last run) wrote
    them.
    """
    if not bundle.is_file():
        say(f"{'would patch' if dry else 'wait':<12} Webapp surface "
            f"(bundle not built yet: {bundle})")
        return False
    brand = asset_source.read_bytes()
    version = brand_version(brand)
    chrome = read_chrome(chrome_sources, say)
    source = read_stable(bundle).decode("utf-8")
    problem = shell_problem(source)
    if problem is not None:
        say(f"{'wait':<12} Webapp surface ({problem}: {bundle})")
        return False
    icons = icon_files(icons_root) if icons_root is not None else []
    if icons_root is not None and not icons:
        say(f"{'wait':<12} Webapp icons (the installer has not written "
            f"{icons_root} yet; the tab keeps the build's links)")
    desired = with_brand(source, version)
    if icons:
        desired = with_icons(desired)
    if chrome:
        desired = with_chrome(desired, chrome_version([blob for _, blob in chrome]))
    targets: list[tuple[Path, bytes]] = [(bundle.parent / BRAND_ASSET, brand)]
    for name, blob in chrome:
        targets.append((bundle.parent / name, blob))
    for name, blob in icons:
        targets.append((bundle.parent / name, blob))
    asset_changes = [(path, blob) for path, blob in targets
                     if not path.is_file() or path.read_bytes() != blob]
    html_changed = desired != source
    detail = f"odile-brand.js v{version}"
    if chrome:
        detail += f", odile-demo-chrome v{chrome_version([blob for _, blob in chrome])}"
    elif strip_chrome(source) != source:
        # A missing source is an image defect, not a reason to strip a working
        # surface: the block the bundle already carries is kept.
        detail += ", chrome kept (sources missing)"
    else:
        detail += ", chrome absent"
    if icons:
        detail += f", {len(icons)} icon files"
    if not html_changed and not asset_changes:
        say(f"{'same':<12} Webapp surface ({detail}, index.html linked)")
        return False
    say(f"{'would write' if dry else 'write':<12} Webapp surface "
        f"({detail}, index.html {'linked' if html_changed else 'unchanged'})")
    if dry:
        return True
    st = bundle.stat()
    owner = (st.st_uid, st.st_gid)
    for path, blob in targets:
        write_atomic(path, blob, 0o644, owner)
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

    here = Path(__file__).resolve().parent
    template = DEFAULT_ASSET
    if not template.is_file():
        template = here / "webapp-brand.js"
    script = template.read_bytes()
    version = brand_version(script)
    check(len(version) == 12 and all(c in "0123456789abcdef" for c in version),
          "the tag's version is the script's sha256, 12 hex")
    css = here / "webapp-chrome.css"
    js = here / "webapp-chrome.js"
    check(css.is_file() and js.is_file(),
          "the image carries both chrome files beside this helper")
    if css.is_file() and js.is_file():
        combined = chrome_version([css.read_bytes(), js.read_bytes()])
        check(combined != version and len(combined) == 12,
              "the chrome hash is its own 12 hex over both files")
        check(chrome_tag_lines(combined)[1].count("odile-demo-chrome.js") == 1
              and chrome_tag_lines(combined)[2].count("odile-demo-chrome.css") == 1,
              "one script tag and one stylesheet tag")
    shell = ("<!doctype html>\n<html>\n  <head>\n    <title>Hermes</title>\n"
             "  </head>\n  <body></body>\n</html>\n")
    once = with_brand(shell, version)
    check(with_brand(once, version) == once and once.count(BRAND_ASSET) == 1,
          "the brand tag is linked exactly once, even on a second splice")
    check(f"{BRAND_ASSET}?v={version}" in once, "the brand tag carries the content hash")
    check(strip_brand(once) == shell, "stripping the brand restores the vendor shell")

    if css.is_file() and js.is_file():
        combined = chrome_version([css.read_bytes(), js.read_bytes()])
        surface = with_chrome(once, combined)
        check(surface.count("odile-demo-chrome.js") == 1
              and surface.count("odile-demo-chrome.css") == 1,
              "the chrome is linked exactly once, even after the brand")
        check(with_chrome(surface, combined) == surface, "the chrome splice is idempotent")
        check(strip_chrome(surface) == once, "stripping the chrome leaves the brand")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bundle = root / "index.html"
        bundle.write_text(shell, encoding="utf-8")
        asset = root / "webapp-brand.js"
        asset.write_bytes(script)
        sources = []
        if css.is_file() and js.is_file():
            for path in (css, js):
                copy = root / path.name
                copy.write_bytes(path.read_bytes())
                sources.append(copy)
        icons = root / "icons"
        icons.mkdir()
        for source_name, _bundle_name in ICON_ASSETS:
            (icons / source_name).write_bytes(b"icon:" + source_name.encode())
        first = apply(bundle, asset, sources, icons_root=icons,
                      say=lambda line: None)
        second = apply(bundle, asset, sources, icons_root=icons,
                       say=lambda line: None)
        check(first and not second, "the first run writes, the second is `same`")
        text = bundle.read_text(encoding="utf-8")
        check(text.count(BRAND_ASSET) == 1, "one brand tag after the re-assert")
        check((root / BRAND_ASSET).read_bytes() == script,
              "the brand asset is the shipped bytes, byte for byte")
        text = bundle.read_text(encoding="utf-8")
        check(text.count('href="./odile-favicon.ico"') == 2
              and text.count('href="./odile-apple-touch-icon.png"') == 1
              and "./apple-touch-icon.png" not in text,
              "the three tab links are Odile's, not the vendor's")
        for source_name, bundle_name in ICON_ASSETS:
            check((root / bundle_name).read_bytes()
                  == b"icon:" + source_name.encode(),
                  f"{bundle_name} is the icon root's bytes")
        if sources:
            check(text.count("odile-demo-chrome.js") == 1
                  and text.count("odile-demo-chrome.css") == 1,
                  "one tag per chrome asset after the re-assert")
            check((root / "odile-demo-chrome.css").read_bytes() == css.read_bytes()
                  and (root / "odile-demo-chrome.js").read_bytes() == js.read_bytes(),
                  "the chrome assets are the shipped bytes, byte for byte")
        bare = apply(bundle, asset, sources, icons_root=root / "no-icons",
                     say=lambda line: None)
        check(not bare,
              "a missing icon root is a `wait` and keeps the links in place")
        missing = apply(root / "nope" / "index.html", asset, sources,
                        say=lambda line: None)
        check(not missing, "a missing bundle is a `wait`, not a write")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bundle = root / "index.html"
        bundle.write_text(shell, encoding="utf-8")
        asset = root / "webapp-brand.js"
        asset.write_bytes(script)
        lines: list[str] = []
        changed = apply(bundle, asset, [root / "absent.css", root / "absent.js"],
                        say=lines.append)
        text = bundle.read_text(encoding="utf-8")
        check(changed and text.count(BRAND_ASSET) == 1
              and "odile-demo-chrome" not in text,
              "a missing chrome source leaves the brand converged and the chrome out")
        check(any(line.startswith("error") for line in lines),
              "a missing chrome source is named as an error")

    if css.is_file() and js.is_file():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "index.html"
            combined = chrome_version([css.read_bytes(), js.read_bytes()])
            bundle.write_text(with_chrome(with_brand(shell, version), combined),
                              encoding="utf-8")
            asset = root / "webapp-brand.js"
            asset.write_bytes(script)
            (root / BRAND_ASSET).write_bytes(script)
            lines = []
            changed = apply(bundle, asset, [root / "absent.css", root / "absent.js"],
                            say=lines.append)
            text = bundle.read_text(encoding="utf-8")
            check(not changed and text.count("odile-demo-chrome.js") == 1,
                  "a missing source keeps the chrome block the bundle already carries")
            check(any("chrome kept" in line for line in lines),
                  "the kept block is reported as kept")

    if failures:
        print(f"self-test: {failures} failure(s)")
        return 1
    print("self-test: ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-assert the Odile name and chrome in a rebuilt Hermes Webapp.")
    parser.add_argument("--home", default=str(DEFAULT_HOME),
                        help=f"the tenant's home (default {DEFAULT_HOME})")
    parser.add_argument("--bundle", default="",
                        help="the built shell (default <home>/" + str(SHELL_REL) + ")")
    parser.add_argument("--asset", default=str(DEFAULT_ASSET),
                        help=f"the brand bytes (default {DEFAULT_ASSET})")
    parser.add_argument("--icons", default=str(DEFAULT_ICONS),
                        help=f"the installer's icon root (default {DEFAULT_ICONS})")
    parser.add_argument("--chrome-css", default=str(DEFAULT_CHROME_CSS),
                        help=f"the chrome stylesheet (default {DEFAULT_CHROME_CSS})")
    parser.add_argument("--chrome-js", default=str(DEFAULT_CHROME_JS),
                        help=f"the chrome script (default {DEFAULT_CHROME_JS})")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    parser.add_argument("--quiet", action="store_true",
                        help="print nothing when converged (the gateway's ExecStartPre)")
    parser.add_argument("--self-test", action="store_true", help="test the splices and one run")
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
        print(f"error        Webapp surface ({asset} is missing; the image did not "
              f"install the bytes)", file=sys.stderr, flush=True)
        return 0
    try:
        apply(bundle, asset, [Path(args.chrome_css), Path(args.chrome_js)],
              icons_root=Path(args.icons), dry=args.dry_run, say=say)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        # Never keep the gateway down for the surface: an ExecStartPre that
        # exits non-zero would. The journal line is the report.
        print(f"error        Webapp surface ({exc})", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
