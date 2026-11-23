#!/usr/bin/python3
"""Brand lint — keep the visual surfaces inside BRAND.md.

Checks the public pages, the prototype, the local demo, the brand documents and
the plain-text email templates for:
  * colours outside the palette their surface may paint — the four v0 tokens in
    §Legacy surfaces belong to the prototype and the demo template only, and
    the finding names the token and the surface
  * pure white/black
  * external scripts, fonts, images or stylesheets
  * palette drift between the brand sheet and the landing page CSS
  * the shell pairing: every page under `web/` and `templates/` sets the header
    wordmark and the h2 role as one of the sheet's two pairs, never a mix
  * a tracked form field — the sheet letterspaces capitals only, and no
    `input`, `textarea` or `select` rule may declare `letter-spacing`
  * the Cormorant face and embedded font on the landing page and the brand
    documents; the Ruysch backdrop on the landing page
  * the sheet's required sections and unfinished markers

    ./tools/brand_lint.py
    ./tools/brand_lint.py --root .

Exit codes: 0 clean, 1 findings, 2 the sheet or a surface is missing.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from pathlib import Path

BRAND = Path("BRAND.md")
SURFACE_GLOBS = (
    "web/*.html",
    "prototype/*.html",
    "webapp/*.html",
    "emails/*.html",
    "templates/*.html",
    "templates/*.txt",
    "templates/emails/*.txt",
    "docs/brand/*.html",
    "demo/local/*.html",
    "deploy/demo-host/*.html",
)
# BRAND.md §Legacy surfaces: "Never use them on `web/` or `docs/brand/`
# surfaces." The sheet names two carriers — the prototype and the demo
# template — so the allowed set is per surface, not one set for the tree.
LEGACY_SURFACE_GLOBS = ("prototype/*.html", "demo/local/*.html")

# The sheet is the build contract: every section below answers a checklist
# question from ODL-328. Deleting or renaming one is a brand decision, not a
# tidy-up, so the lint refuses it until the list here is changed on purpose.
SHEET_SECTIONS = (
    "The name",
    "Name and mark",
    "Palette",
    "Accessibility tokens",
    "Typography",
    "Spacing and layout",
    "Component states",
    "Iconography",
    "Motion",
    "Art direction",
    "The landing page",
    "Voice",
    "Email",
    "Video",
    "Product surfaces",
    "Interaction",
    "Brand review",
    "Change log",
    "Do / Don't",
)
UNFINISHED_RE = re.compile(r"\b(?:TBD|TBC|TODO|FIXME)\b|to be (?:decided|confirmed)", re.IGNORECASE)

# The brand sheet's tables are the only source of allowed colours. Adding one
# is a BRAND.md change, not a code change.
FORBIDDEN = {"#fff", "#ffffff", "#000", "#000000"}
TOKEN_ROW_RE = re.compile(r"\|\s*`--([a-z0-9-]+)`\s*\|\s*`([^`]+)`\s*\|")
HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
BASE64_RE = re.compile(r"base64,[A-Za-z0-9+/=]+")
EXTERNAL_TAG_RE = re.compile(
    r"<(?:script|link|img)\b[^>]*\b(?:src|href)=[\"'](https?://[^\"']+)",
    re.IGNORECASE,
)
EXTERNAL_CSS_RE = re.compile(r"url\(\s*[\"']?https?://", re.IGNORECASE)
CSS_WHITE_BLACK_RE = re.compile(
    r"\b(?:color|background|background-color|border(?:-[a-z]+)?-color)\s*:\s*(white|black)\b",
    re.IGNORECASE,
)
ROOT_VAR_RE = re.compile(r"--([a-z0-9-]+)\s*:\s*([^;}]+)")

# §Typography *Two section-title roles* and §Name and mark *Size*: the header
# wordmark and the h2 role are one shell, and the sheet allows two of them.
# 1.5rem (24 px) travels with the document h2 (1.34rem, 600); 2rem (32 px)
# travels with the display h2 (the clamp, 500). A page that pairs one with the
# other, or invents a third size, has forked the shell.
SHELL_GLOBS = ("web/*.html", "templates/*.html")
CSS_RULE_RE = re.compile(r"(?P<selector>[^{}]+)\{(?P<body>[^{}]*)\}")
STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>(?P<css>.*?)</style>", re.DOTALL | re.IGNORECASE)
CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
FONT_SIZE_RE = re.compile(r"font-size\s*:\s*([^;}]+)", re.IGNORECASE)
LETTER_SPACING_RE = re.compile(r"letter-spacing\s*:", re.IGNORECASE)
WORDMARK_SIZES = {"1.5rem": "document", "2rem": "report"}
# `1.9rem` is the clamp's own floor, which the report pages set again inside
# their ≤560 media query; it is the same role, not a third value.
H2_SIZES = {
    "1.34rem": "document",
    "clamp(1.9rem,3vw,2.6rem)": "report",
    "1.9rem": "report",
}
# A section title is at least 1.2 rem. Below that an `h2` is a label — the demo
# template sets `.panel h2` and `.railbox h2` at 10 px caps — and labels are a
# different role (§Typography, *Label (caps)*).
H2_SECTION_MIN_REM = 1.2
FIELD_SELECTOR_RE = re.compile(r"(?:^|[\s,>+~])(?:input|textarea|select)\b", re.IGNORECASE)


def css_text(text: str) -> str:
    """One surface's stylesheet, comments blanked, line numbers preserved.

    A rule only counts inside an inline `<style>` block, and a `/* … */`
    comment is not a selector: the first-visit preview explains in a comment
    that the page has "no input", and read as a selector that sentence made
    the next rule look like a tracked field. Both cuts keep the text the same
    length and keep every newline, so a finding's line number is the file's.
    """
    def blank(segment: str) -> str:
        return re.sub(r"[^\n]", " ", segment)

    if "<style" not in text.lower():
        return CSS_COMMENT_RE.sub(lambda m: blank(m.group(0)), text)
    parts: list[str] = []
    last = 0
    for match in STYLE_BLOCK_RE.finditer(text):
        parts.append(blank(text[last:match.start("css")]))
        parts.append(match.group("css"))
        last = match.end("css")
    parts.append(blank(text[last:]))
    return CSS_COMMENT_RE.sub(lambda m: blank(m.group(0)), "".join(parts))


def css_rules(text: str) -> list[tuple[str, list[str], str, int]]:
    """Every flat CSS rule: (selector text, its members, body, line number).

    Flat rules only, which is what these pages are: no nesting, and the one
    at-rule block (`@font-face`) carries no selector member the checks test.
    """
    rules = []
    for match in CSS_RULE_RE.finditer(text):
        selector = match.group("selector").strip()
        if not selector or selector.startswith("@"):
            continue
        members = [member.strip() for member in selector.split(",")]
        start = match.start("selector") + len(match.group("selector")) - len(
            match.group("selector").lstrip())
        line = text.count("\n", 0, start) + 1
        rules.append((selector, members, match.group("body"), line))
    return rules


def size_rem(value: str) -> float | None:
    """A bare `Nrem` value in rem; None for clamp(), px, %, or anything else."""
    match = re.fullmatch(r"([0-9.]+)rem", value.strip().lower())
    return float(match.group(1)) if match else None


def check_shell(path: Path, name: str) -> list[str]:
    """The shell pairing on one page: wordmark size and h2 role travel together."""
    findings: list[str] = []
    plain = css_text(strip_base64(path.read_text(encoding="utf-8")))
    wordmark: tuple[str, int] | None = None
    h2_roles: list[tuple[str, str, int]] = []
    for selector, members, body, line in css_rules(plain):
        sizes = [normalise_size(value) for value in FONT_SIZE_RE.findall(body)]
        if ".brand" in members and sizes:
            wordmark = (sizes[0], line)
        for member in members:
            if not re.search(r"(?:^|[\s>+~])h2$", member) or not sizes:
                continue
            size = sizes[0]
            if size in H2_SIZES:
                h2_roles.append((H2_SIZES[size], size, line))
            elif (rem := size_rem(size)) is not None and rem >= H2_SECTION_MIN_REM:
                findings.append(
                    f"{name}:{line}: h2 is {size}; the sheet's section-title sizes are "
                    f"1.34rem (document shell) and clamp(1.9rem,3vw,2.6rem) (report shell)"
                )
    roles = {role for role, _, _ in h2_roles}
    if len(roles) > 1:
        pairs = ", ".join(f"h2 {size} at line {line}" for _, size, line in h2_roles)
        findings.append(
            f"{name}: the page mixes the two section-title roles ({pairs}); a page "
            f"takes one shell"
        )
    if wordmark is None:
        return findings
    size, line = wordmark
    if size not in WORDMARK_SIZES:
        findings.append(
            f"{name}:{line}: the header wordmark is {size}; the sheet allows 1.5rem "
            f"(24 px, document shell) and 2rem (32 px, report shell)"
        )
        return findings
    role = WORDMARK_SIZES[size]
    if not roles:
        return findings
    h2_role = roles.pop()
    if h2_role != role:
        h2_size, h2_line = next((size, line) for role_, size, line in h2_roles
                                if role_ == h2_role)
        want = ("1.34rem" if role == "document"
                else "clamp(1.9rem,3vw,2.6rem)")
        findings.append(
            f"{name}:{h2_line}: the {role} shell pairs its {size} wordmark with h2 "
            f"{want}, but this page sets h2 {h2_size} — the wordmark and the h2 role "
            f"are one shell"
        )
    return findings


def normalise_size(value: str) -> str:
    """A declared size with whitespace removed, for exact comparison."""
    return re.sub(r"\s+", "", value.strip().lower())


def check_shell_surfaces(root: Path) -> list[str]:
    findings: list[str] = []
    for pattern in SHELL_GLOBS:
        for path in sorted(root.glob(pattern)):
            findings.extend(check_shell(path, path.relative_to(root).as_posix()))
    return findings


def strip_base64(text: str) -> str:
    return BASE64_RE.sub("base64,", text)


def brand_palette(sheet: Path) -> dict[str, str]:
    """Parse the token/hex rows from the brand sheet's tables."""
    tokens: dict[str, str] = {}
    for line in sheet.read_text(encoding="utf-8").splitlines():
        match = TOKEN_ROW_RE.match(line)
        if match:
            tokens[match.group(1)] = match.group(2).strip().lower()
    return tokens


def legacy_palette(sheet: Path) -> dict[str, str]:
    """The token rows under the sheet's `### Legacy surfaces` heading.

    Only that table. The heading is the sheet's statement of which surfaces
    may still paint these values; a token that moves out of it stops being
    legacy, and deleting the table makes the prototype and the demo template
    fail the palette check (a missing legacy row is loud, not a silent skip).
    """
    tokens: dict[str, str] = {}
    in_legacy = False
    for line in sheet.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            in_legacy = line.strip() == "### Legacy surfaces"
            continue
        if not in_legacy:
            continue
        match = TOKEN_ROW_RE.match(line)
        if match:
            tokens[match.group(1)] = match.group(2).strip().lower()
    return tokens


def is_legacy_surface(surface: str) -> bool:
    """True for the two surfaces §Legacy surfaces keeps the v0 values on."""
    return any(fnmatch.fnmatch(surface, glob) for glob in LEGACY_SURFACE_GLOBS)


def allowed_colours(palette: dict[str, str], legacy: dict[str, str],
                    surface: str) -> set[str]:
    """The colours one surface may paint: the sheet's, minus the legacy rows."""
    values = {value for token, value in palette.items()
              if token not in legacy and value.startswith("#")}
    if is_legacy_surface(surface):
        values |= {value for value in legacy.values() if value.startswith("#")}
    return values


def check_sheet(sheet: Path) -> list[str]:
    """The sheet must carry every required section and no unfinished marker."""
    findings: list[str] = []
    text = sheet.read_text(encoding="utf-8")
    headings = {
        line[3:].strip() for line in text.splitlines() if line.startswith("## ")
    }
    for section in SHEET_SECTIONS:
        if section not in headings:
            findings.append(f"{sheet.name}: required section '## {section}' is missing")
    for match in UNFINISHED_RE.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        findings.append(f"{sheet.name}:{line}: unfinished marker '{match.group(0)}'")
    return findings


def css_root(text: str) -> dict[str, str]:
    match = re.search(r":root\s*\{(.*?)\}", text, re.DOTALL)
    if not match:
        return {}
    return {name: value.strip().lower() for name, value in ROOT_VAR_RE.findall(match.group(1))}


def check_surface(path: Path, palette: dict[str, str],
                  legacy: dict[str, str] | None = None,
                  name: str | None = None) -> list[str]:
    """One surface's findings; `name` is the path a finding shows.

    `path` is what is read, `name` is what the tree calls it (the relative path
    in `main`, so `--root` works from any working directory). `name` also
    decides which palette rows apply.
    """
    findings: list[str] = []
    legacy = {} if legacy is None else legacy
    if name is None:
        name = path.as_posix()
    allowed = allowed_colours(palette, legacy, name)
    legacy_token = {value: token for token, value in legacy.items()
                    if value.startswith("#")}
    text = path.read_text(encoding="utf-8")
    plain = strip_base64(text)

    for match in HEX_RE.finditer(plain):
        colour = match.group(0).lower()
        line = plain.count("\n", 0, match.start()) + 1
        if colour in FORBIDDEN:
            findings.append(f"{name}:{line}: pure white/black {colour} is forbidden")
        elif colour not in allowed:
            if colour in legacy_token:
                findings.append(
                    f"{name}:{line}: {colour} is the legacy token "
                    f"--{legacy_token[colour]}; allowed only on prototype/ and "
                    f"demo/local/"
                )
            else:
                findings.append(f"{name}:{line}: colour {colour} is not in BRAND.md")

    for pattern in ("rgba(255,255,255", "rgba(255, 255, 255", "rgba(0,0,0", "rgba(0, 0, 0"):
        if pattern in plain.replace("\n", ""):
            findings.append(f"{name}: pure white/black rgb() is forbidden")

    for match in EXTERNAL_TAG_RE.finditer(plain):
        findings.append(f"{name}: external request {match.group(1)}")

    for match in EXTERNAL_CSS_RE.finditer(plain):
        findings.append(f"{name}: external CSS url {match.group(0)}")

    for match in CSS_WHITE_BLACK_RE.finditer(plain):
        line = plain.count("\n", 0, match.start()) + 1
        findings.append(f"{name}:{line}: CSS keyword '{match.group(1)}' is forbidden")

    # §Typography: capitals are letterspaced, lowercase never; a field holds a
    # value, not a label, so no field rule declares tracking. The three
    # password fields carried `.02em` (0.336 px at 16.8 px — under one device
    # pixel, invisible as a mask aid) until 2026-11-23.
    for selector, members, body, line in css_rules(css_text(plain)):
        if LETTER_SPACING_RE.search(body) and any(
                FIELD_SELECTOR_RE.search(member) for member in members):
            findings.append(
                f"{name}:{line}: a form field carries letter-spacing; the sheet "
                f"tracks capitals only and a field is never tracked"
            )

    if name.endswith("web/index.html") or name.startswith("docs/brand/"):
        if "Cormorant Garamond" not in text:
            findings.append(f"{name}: Cormorant Garamond is missing")
        if "@font-face" not in text or "data:font/woff2" not in text:
            findings.append(f"{name}: the font is not embedded")

    if name.endswith("web/index.html"):
        if "bg-ruysch.webp" not in text:
            findings.append(f"{name}: the Ruysch backdrop is missing")

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    sheet = root / BRAND
    if not sheet.is_file():
        print(f"brand lint: missing {sheet}", file=sys.stderr)
        return 2

    findings: list[str] = []
    palette = brand_palette(sheet)
    legacy = legacy_palette(sheet)
    findings.extend(check_sheet(sheet))
    findings.extend(check_shell_surfaces(root))
    # The count on the OK line is the whole sheet: the colours every surface
    # may paint, and how many of them are the legacy-only exceptions.
    legacy_only = {value for value in legacy.values() if value.startswith("#")}
    allowed = {value for token, value in palette.items()
               if token not in legacy and value.startswith("#")} | legacy_only
    if not allowed:
        findings.append("BRAND.md: no palette colours found")
    for token in ("paper", "ink", "soft"):
        if token not in palette:
            findings.append(f"BRAND.md: palette token --{token} is missing")

    scanned = 0
    surfaces = sorted({path for pattern in SURFACE_GLOBS for path in root.glob(pattern)})
    if not surfaces:
        findings.append("no visual surface found under web/, prototype/, webapp/, emails/, templates/, docs/brand/ or demo/local/")
    for path in surfaces:
        scanned += 1
        findings.extend(check_surface(path, palette, legacy,
                                      name=path.relative_to(root).as_posix()))

    landing = root / "web/index.html"
    if landing.is_file():
        declared = css_root(strip_base64(landing.read_text(encoding="utf-8")))
        for token in ("paper", "ink", "soft"):
            if token in palette and token in declared:
                if palette[token] != declared[token]:
                    findings.append(
                        f"BRAND.md / web/index.html: --{token} is "
                        f"{palette[token]} in the sheet but {declared[token]} in CSS"
                    )

    if findings:
        print("brand lint: FAIL", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1

    print(f"brand lint: OK ({scanned} surface(s), {len(allowed)} allowed colours, "
          f"{len(legacy_only)} legacy-only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
