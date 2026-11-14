#!/usr/bin/python3
"""Brand lint — keep the visual surfaces inside BRAND.md.

Checks the public pages, the prototype, the local demo and the brand documents
for:
  * colours outside the documented palette (including pure white/black)
  * external scripts, fonts, images or stylesheets
  * palette drift between the brand sheet and the landing page CSS
  * the Cormorant face and embedded font on the landing page and the brand
    documents; the Ruysch backdrop on the landing page
  * the sheet's required sections and unfinished markers

    ./tools/brand_lint.py
    ./tools/brand_lint.py --root .

Exit codes: 0 clean, 1 findings, 2 the sheet or a surface is missing.
"""

from __future__ import annotations

import argparse
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
    "docs/brand/*.html",
    "demo/local/*.html",
    "deploy/demo-host/*.html",
)

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


def strip_base64(text: str) -> str:
    return BASE64_RE.sub("base64,", text)


def brand_palette(sheet: Path) -> dict[str, str]:
    """Parse the token/hex rows from the brand sheet's first palette table."""
    tokens: dict[str, str] = {}
    for line in sheet.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\|\s*`--([a-z0-9-]+)`\s*\|\s*`([^`]+)`\s*\|", line)
        if match:
            tokens[match.group(1)] = match.group(2).strip().lower()
    return tokens


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


def check_surface(path: Path, allowed: set[str]) -> list[str]:
    findings: list[str] = []
    text = path.read_text(encoding="utf-8")
    plain = strip_base64(text)

    for match in HEX_RE.finditer(plain):
        colour = match.group(0).lower()
        line = plain.count("\n", 0, match.start()) + 1
        if colour in FORBIDDEN:
            findings.append(f"{path}:{line}: pure white/black {colour} is forbidden")
        elif colour not in allowed:
            findings.append(f"{path}:{line}: colour {colour} is not in BRAND.md")

    for pattern in ("rgba(255,255,255", "rgba(255, 255, 255", "rgba(0,0,0", "rgba(0, 0, 0"):
        if pattern in plain.replace("\n", ""):
            findings.append(f"{path}: pure white/black rgb() is forbidden")

    for match in EXTERNAL_TAG_RE.finditer(plain):
        findings.append(f"{path}: external request {match.group(1)}")

    for match in EXTERNAL_CSS_RE.finditer(plain):
        findings.append(f"{path}: external CSS url {match.group(0)}")

    for match in CSS_WHITE_BLACK_RE.finditer(plain):
        line = plain.count("\n", 0, match.start()) + 1
        findings.append(f"{path}:{line}: CSS keyword '{match.group(1)}' is forbidden")

    if path.as_posix().endswith("web/index.html") or path.as_posix().startswith("docs/brand/"):
        if "Cormorant Garamond" not in text:
            findings.append(f"{path}: Cormorant Garamond is missing")
        if "@font-face" not in text or "data:font/woff2" not in text:
            findings.append(f"{path}: the font is not embedded")

    if path.as_posix().endswith("web/index.html"):
        if "bg-ruysch.webp" not in text:
            findings.append(f"{path}: the Ruysch backdrop is missing")

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
    findings.extend(check_sheet(sheet))
    allowed = {value for value in palette.values() if value.startswith("#")}
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
        findings.extend(check_surface(path.relative_to(root), allowed))

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

    print(f"brand lint: OK ({scanned} surface(s), {len(allowed)} allowed colours)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
