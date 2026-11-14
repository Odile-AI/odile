#!/usr/bin/python3
"""Tests for the brand sheet gate.

`BRAND.md` is the build contract; `tools/brand_lint.py` fails when a required
section disappears or a placeholder is left behind. These tests prove the gate
against the shipped sheet and two broken fixtures.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import brand_lint  # noqa: E402


def write_sheet(directory: str, body: str) -> Path:
    path = Path(directory) / "BRAND.md"
    path.write_text(body, encoding="utf-8")
    return path


class BrandSheetTests(unittest.TestCase):
    def test_the_shipped_sheet_has_every_required_section(self) -> None:
        self.assertEqual(brand_lint.check_sheet(ROOT / "BRAND.md"), [])

    def test_the_palette_parser_reads_the_shipped_sheet(self) -> None:
        palette = brand_lint.brand_palette(ROOT / "BRAND.md")
        self.assertEqual(palette["paper"], "#eadfc4")
        self.assertEqual(palette["ink"], "#1c1813")
        self.assertEqual(palette["soft"], "#4a4136")
        self.assertEqual(palette["oxblood"], "#6e2a2a")
        self.assertEqual(len(palette), 27)

    def test_a_missing_section_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sheet = write_sheet(directory, "# Odile\n")
            findings = brand_lint.check_sheet(sheet)
        self.assertTrue(
            any("'## Voice' is missing" in finding for finding in findings),
            findings,
        )
        self.assertEqual(len(findings), len(brand_lint.SHEET_SECTIONS))

    def test_an_unfinished_marker_is_a_finding(self) -> None:
        body = "# Odile\n\n" + "\n\n".join(
            f"## {section}" for section in brand_lint.SHEET_SECTIONS
        ) + "\n\nTBD\n"
        with tempfile.TemporaryDirectory() as directory:
            sheet = write_sheet(directory, body)
            findings = brand_lint.check_sheet(sheet)
        self.assertTrue(
            any("unfinished marker 'TBD'" in finding for finding in findings),
            findings,
        )


if __name__ == "__main__":
    unittest.main()
