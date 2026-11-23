#!/usr/bin/python3
"""Tests for the brand sheet gate.

`BRAND.md` is the build contract; `tools/brand_lint.py` fails when a required
section disappears or a placeholder is left behind, and it holds each surface
to the colours that surface is allowed to paint. These tests prove the gate
against the shipped sheet, a broken sheet fixture, and the legacy-token rule
the sheet states in §Legacy surfaces.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import brand_lint  # noqa: E402

LEGACY_TOKENS = {
    "paper-v0": "#f6f0e4",
    "card-v0": "#fbf6ea",
    "error-v0": "#8f3418",
    "placeholder-v0": "#776b5a",
}


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


def surface(directory: str, name: str, body: str) -> Path:
    """Write a surface fixture under a temporary root and return it."""
    path = Path(directory) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


class LegacyTokenTests(unittest.TestCase):
    """§Legacy surfaces: the four v0 values belong to two surfaces only."""

    def setUp(self) -> None:
        self.palette = brand_lint.brand_palette(ROOT / "BRAND.md")
        self.legacy = brand_lint.legacy_palette(ROOT / "BRAND.md")

    def test_the_sheet_marks_exactly_the_four_v0_tokens_legacy(self) -> None:
        self.assertEqual(self.legacy, LEGACY_TOKENS)

    def test_a_web_page_painting_a_v0_token_names_the_token_and_the_surface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = surface(directory, "web/claim.html",
                           ":root{--paper:#F6F0E4;--card:#FBF6EA}\n")
            findings = brand_lint.check_surface(
                page, self.palette, self.legacy, name="web/claim.html")
        self.assertEqual(len(findings), 2, findings)
        self.assertIn("web/claim.html:1", findings[0])
        self.assertIn("#f6f0e4", findings[0])
        self.assertIn("--paper-v0", findings[0])
        self.assertIn("--card-v0", findings[1])

    def test_the_prototype_and_the_demo_template_may_paint_them(self) -> None:
        body = ":root{--paper:#F6F0E4;--card:#FBF6EA;--error:#8F3418;" \
               "--placeholder:#776B5A}\n"
        with tempfile.TemporaryDirectory() as directory:
            prototype = surface(directory, "prototype/userflow.html", body)
            template = surface(directory, "demo/local/template.html", body)
            for path, written in ((Path("prototype/userflow.html"), prototype),
                                  (Path("demo/local/template.html"), template)):
                findings = brand_lint.check_surface(
                    written, self.palette, self.legacy, name=path.as_posix())
                self.assertEqual(findings, [], path.as_posix())

    def test_a_v0_token_on_another_product_surface_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = surface(directory, "templates/status.html",
                           "<style>:root{--error:#8F3418}</style>\n")
            findings = brand_lint.check_surface(
                page, self.palette, self.legacy, name="templates/status.html")
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("templates/status.html:1", findings[0])
        self.assertIn("--error-v0", findings[0])

    def test_the_ok_line_counts_the_allowed_set_and_the_legacy_only_part(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = brand_lint.main(["--root", str(ROOT)])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("21 allowed colours, 4 legacy-only", out.getvalue())

    def test_the_root_flag_reads_surfaces_outside_the_working_directory(self) -> None:
        """`--root` names the tree to read; the process may stand anywhere."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "BRAND.md").write_bytes((ROOT / "BRAND.md").read_bytes())
            surface(directory, "prototype/userflow.html",
                    ":root{--paper:#F6F0E4}\n")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = brand_lint.main(["--root", str(root)])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("1 surface(s), 21 allowed colours, 4 legacy-only", out.getvalue())


class ShellPairingTests(unittest.TestCase):
    """§Typography *Two section-title roles*: the wordmark and the h2 are one shell."""

    @staticmethod
    def page(directory: str, css: str) -> Path:
        return surface(directory, "web/page.html", f"<style>{css}</style>\n")

    def test_the_shipped_pages_pair_the_shell(self) -> None:
        self.assertEqual(brand_lint.check_shell_surfaces(ROOT), [])

    def test_a_24px_wordmark_with_the_display_h2_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = self.page(directory, ".brand{font-size:1.5rem}\n"
                                        "h2{font-size:clamp(1.9rem,3vw,2.6rem)}\n")
            findings = brand_lint.check_shell(page, "web/page.html")
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("web/page.html:2", findings[0])
        self.assertIn("are one shell", findings[0])

    def test_a_32px_wordmark_with_the_document_h2_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = self.page(directory, ".brand{font-size:2rem}\n"
                                        "h2{font-size:1.34rem}\n")
            findings = brand_lint.check_shell(page, "web/page.html")
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("web/page.html:2", findings[0])
        self.assertIn("1.34rem", findings[0])

    def test_the_clamp_floor_in_a_media_query_is_the_same_role(self) -> None:
        body = (".brand{font-size:2rem}\n"
                "h2{font-size:clamp(1.9rem,3vw,2.6rem)}\n"
                "@media (max-width:560px){h2{font-size:1.9rem}}\n")
        with tempfile.TemporaryDirectory() as directory:
            page = self.page(directory, body)
            findings = brand_lint.check_shell(page, "web/page.html")
        self.assertEqual(findings, [])

    def test_a_third_wordmark_size_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = self.page(directory, ".brand{font-size:1.25rem}\n")
            findings = brand_lint.check_shell(page, "web/page.html")
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("1.25rem", findings[0])
        self.assertIn("1.5rem", findings[0])

    def test_a_page_with_no_h2_takes_the_shell_of_its_wordmark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = self.page(directory, ".brand{font-size:2rem}\n")
            findings = brand_lint.check_shell(page, "web/page.html")
        self.assertEqual(findings, [])

    def test_a_tracked_field_is_a_finding_and_the_surface_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = self.page(directory,
                             "input[type=password]{letter-spacing:.02em}\n")
            findings = brand_lint.check_surface(
                page, {}, {}, name="web/page.html")
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("web/page.html:1", findings[0])
        self.assertIn("a form field carries letter-spacing", findings[0])

    def test_a_brand_rule_scoped_under_a_parent_is_still_the_wordmark(self) -> None:
        body = (".brand{font-size:1.5rem}\n"
                ".top .brand{font-size:2.4rem}\n"
                "h2{font-size:1.34rem}\n")
        with tempfile.TemporaryDirectory() as directory:
            page = self.page(directory, body)
            findings = brand_lint.check_shell(page, "web/page.html")
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("2.4rem", findings[0])
        self.assertIn("1.5rem", findings[0])

    def test_an_h2_in_px_is_held_to_the_section_title_floor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = self.page(directory, ".brand{font-size:1.5rem}\n"
                                        "h2{font-size:22px}\n")
            findings = brand_lint.check_shell(page, "web/page.html")
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("22px", findings[0])
        self.assertIn("1.34rem", findings[0])

    def test_a_labelled_h2_below_the_section_size_is_not_a_section_title(self) -> None:
        body = ".brand{font-size:1.5rem}\n" \
               "h2{font-size:1.34rem}\n" \
               ".panel h2{font-size:10px;letter-spacing:.24em;text-transform:uppercase}\n"
        with tempfile.TemporaryDirectory() as directory:
            page = self.page(directory, body)
            findings = brand_lint.check_shell(page, "web/page.html")
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
