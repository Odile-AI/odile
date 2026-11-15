#!/usr/bin/python3
"""The icon contract every page under web/ carries (the public tree).

`tests/test_web_icon.py` proves the raster set: the committed bytes are a
fresh `tools/icon_build.py` build, the ICO carries its three tab sizes and the
touch icon is an opaque-paper mount. That proof needs PIL and the committed
proof sheet, so the module is HQ-only (`.git/info/exclude`, named in
`docs/ops/PUBLIC-TREE.md` §3) and the public tree's gate never sees it.

The page-level half needs neither PIL nor the icon build. It lives here, in the
tree that ships, so a page that loses its icon links or brings back the
`data:image/svg+xml` favicon fails `make check` on a public runner as well as at
HQ. `docs/brand/REVIEW.md`'s art test requires the same three links and the
same credit on every page.

Run it alone:

    /usr/bin/python3 -m unittest tests.test_web_pages -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


class Pages(unittest.TestCase):
    def pages(self) -> list[Path]:
        found = sorted(WEB.glob("*.html"))
        # Discovery, not a count: a page added to web/ is covered the moment it
        # exists, and a glob that stops matching is caught by the anchor rather
        # than by an empty loop that asserts nothing.
        self.assertIn(WEB / "index.html", found, "the landing page is not under web/")
        return found

    def test_every_page_carries_the_three_links(self) -> None:
        for page in self.pages():
            text = page.read_text(encoding="utf-8")
            self.assertIn('href="/favicon.ico"', text, page.name)
            self.assertIn('href="/icon-32.png"', text, page.name)
            self.assertIn('href="/apple-touch-icon.png"', text, page.name)

    def test_no_page_keeps_the_old_data_uri_favicon(self) -> None:
        for page in self.pages():
            self.assertNotIn("data:image/svg+xml", page.read_text(encoding="utf-8"),
                             page.name)

    def test_the_credit_sits_beside_the_icon_link(self) -> None:
        for page in self.pages():
            lines = page.read_text(encoding="utf-8").splitlines()
            link = next(i for i, line in enumerate(lines) if 'href="/favicon.ico"' in line)
            self.assertTrue(any("Lucas Cranach the Elder" in line for line in lines[link - 2:link]),
                            "%s: the icon credit is not beside the icon link" % page.name)


if __name__ == "__main__":
    unittest.main()
