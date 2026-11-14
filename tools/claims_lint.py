#!/usr/bin/env python3
"""Claims lint — enforce docs/CLAIMS.md on every public surface.

The rule in docs/CLAIMS.md is absolute: a public sentence about privacy,
security or reliability ships only if it is in the deck. This lint is the
mechanical half of that rule. It reads the forbidden-phrase list from
CLAIMS.md itself (so the deck stays the single source of truth) and scans the
files a visitor can read.

Usage:
    ./tools/claims_lint.py [--root .] [paths ...]

Exit codes: 0 clean, 1 violations, 2 the deck could not be parsed.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DECK = Path("docs/CLAIMS.md")

# Files and trees a visitor can reach, plus the deck's own prose. The deck's
# forbidden-word table is fenced by `claims-lint:off` / `claims-lint:on`
# markers; the FAQ is scanned once the file exists. docs/MVP-V0.1.md and
# docs/backlog.yaml are deliberately out of scope: they *discuss* the
# forbidden words while explaining the trust model.
DEFAULT_PATHS = (
    "web",
    "prototype",
    "app.py",
    "README.md",
    "docs/CLAIMS.md",
    "docs/FAQ.md",
    "deploy/demo-host/login.html",
    "deploy/demo-host/verify-command.html",
)

SKIP_DIRS = {".git", "__pycache__", "node_modules", "shots"}
TEXT_SUFFIXES = {".html", ".htm", ".css", ".js", ".py", ".md", ".txt", ".svg", ".json"}

LINT_LIST_RE = re.compile(r"^Lint list:\s*(.+)$", re.MULTILINE)
BACKTICK_RE = re.compile(r"`([^`]+)`")

# A marker must be the whole line (an HTML comment or a bare token), so a
# sentence that merely mentions `claims-lint:off` cannot disable the rest of a
# public page. The deck and the prototype use `<!-- claims-lint:off -->`.
MARKER_RE = re.compile(
    r"^\s*(?:<!--\s*)?claims-lint:\s*(?P<state>off|on)(?:\s*-->)?\s*$"
)


def load_phrases(deck: Path) -> list[str]:
    """Return the forbidden phrases from the deck's one-line lint list."""
    text = deck.read_text(encoding="utf-8")
    match = LINT_LIST_RE.search(text)
    if not match:
        raise SystemExit(f"claims lint: no 'Lint list:' line in {deck}")
    phrases = [p.strip() for p in BACKTICK_RE.findall(match.group(1)) if p.strip()]
    if not phrases:
        raise SystemExit(f"claims lint: empty lint list in {deck}")
    return phrases


def phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Case-insensitive whole-phrase match; spaces also match a hyphen.

    'zero knowledge' and 'zero-knowledge' are separate entries today, but the
    join keeps a future single entry honest.
    """
    body = re.escape(phrase).replace(r"\ ", r"[\s-]+")
    return re.compile(rf"(?<![\w-]){body}(?![\w-])", re.IGNORECASE)


def iter_files(root: Path, paths: tuple[str, ...]):
    for raw in paths:
        path = root / raw
        if path.is_file():
            yield path
            continue
        if not path.exists():
            continue
        for candidate in sorted(path.rglob("*")):
            if candidate.is_file() and candidate.suffix.lower() in TEXT_SUFFIXES:
                if any(part in SKIP_DIRS for part in candidate.parts):
                    continue
                yield candidate


def scan(path: Path, patterns: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    findings: list[str] = []
    disabled = False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"{path}: unreadable: {exc}"]
    for lineno, line in enumerate(lines, 1):
        marker = MARKER_RE.match(line)
        if marker:
            disabled = marker.group("state") == "off"
            continue
        if disabled:
            continue
        for phrase, pattern in patterns:
            if pattern.search(line):
                findings.append(f"{path}:{lineno}: forbidden phrase {phrase!r}")
    if disabled:
        # A missing `on` marker would otherwise skip every line after it and
        # let a forbidden phrase through silently.
        findings.append(f"{path}: claims-lint:off has no matching claims-lint:on")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("paths", nargs="*", help="files or directories to scan")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    deck = root / DECK
    if not deck.is_file():
        print(f"claims lint: missing {deck}", file=sys.stderr)
        return 2

    phrases = load_phrases(deck)
    patterns = [(p, phrase_pattern(p)) for p in phrases]
    paths = tuple(args.paths) if args.paths else DEFAULT_PATHS

    findings: list[str] = []
    scanned = 0
    for path in iter_files(root, paths):
        scanned += 1
        findings.extend(scan(path, patterns))

    if findings:
        print("claims lint: FAIL", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        print(
            f"  {len(findings)} violation(s) across {scanned} file(s); "
            "see docs/CLAIMS.md §2 for approved replacements.",
            file=sys.stderr,
        )
        return 1

    print(f"claims lint: OK ({scanned} file(s), {len(phrases)} forbidden phrases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
