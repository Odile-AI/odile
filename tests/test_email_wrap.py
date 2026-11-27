#!/usr/bin/python3
"""The plain-text account emails stay inside BRAND.md §Email's wrap band.

`BRAND.md` §Email sets the band: a prose line is at most 78 characters as
written, and at most 78 once a `{{WINDOW}}` or `{{DEADLINE}}` value is filled —
the longest value is 29 characters.  The template is hard-wrapped, so the file
is the instrument, and the sheet's own block is the same bytes.

    /usr/bin/python3 -m unittest tests.test_email_wrap -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odilectl import health  # noqa: E402

LIMIT = 78
WINDOW = "2026-12-01T03:10:00Z"
DEADLINE = "2026-12-15T03:10:00Z"
WINDOW_TEXT = "03:10 UTC on 1 December 2026"


def longest_line(text: str) -> int:
    return max((len(line) for line in text.splitlines()), default=0)


def fenced_block(sheet: str, heading: str) -> str:
    """The first ```text block after `heading`, byte for byte."""
    start = sheet.index(heading)
    fence = sheet.index("```", start)
    body_start = sheet.index("\n", fence) + 1
    body_end = sheet.index("```", body_start)
    return sheet[body_start:body_end]


def template_bytes(name: str) -> bytes:
    return (ROOT / "templates" / "emails" / name).read_bytes()


class WrapBandTest(unittest.TestCase):
    def test_the_booking_receipt_fits_the_band_on_both_rendered_faces(self) -> None:
        written = template_bytes("upgrade-booked.txt").decode("utf-8")
        self.assertLessEqual(longest_line(written), LIMIT, "template as written")
        for label, body in (
            ("with the deadline", health.render_upgrade_body(WINDOW, DEADLINE)),
            ("without the deadline", health.render_upgrade_body(WINDOW)),
        ):
            self.assertLessEqual(longest_line(body), LIMIT, label)

    def test_the_receipt_never_splits_the_rendered_date(self) -> None:
        body = health.render_upgrade_body(WINDOW, DEADLINE)
        self.assertTrue(
            any(WINDOW_TEXT in line for line in body.splitlines()),
            body,
        )

    def test_the_invitation_fits_the_band(self) -> None:
        written = template_bytes("invite.txt").decode("utf-8")
        self.assertLessEqual(longest_line(written), LIMIT, "invite.txt")

    def test_the_sheets_two_blocks_are_their_templates(self) -> None:
        sheet = (ROOT / "BRAND.md").read_text(encoding="utf-8")
        for heading, name in (
            ("### Invitation (`invite.txt`)", "invite.txt"),
            ("### Upgrade booking (`upgrade-booked.txt`)", "upgrade-booked.txt"),
        ):
            self.assertEqual(
                fenced_block(sheet, heading).encode("utf-8"),
                template_bytes(name),
                heading,
            )


if __name__ == "__main__":
    unittest.main()
