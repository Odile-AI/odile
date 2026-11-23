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

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

FOCUSABLE = re.compile(r"<(a|button|input|select|textarea)\b([^>]*)>", re.IGNORECASE)
ATTRIBUTE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')
# A landmark is `<main>` plus the named regions. A `<header>` inside `main`
# is not a banner landmark of its own (HTML-AAM), which is where these pages
# put it; this pattern counts the regions the pages name with aria-labelledby.
REGION = re.compile(r"<(?:section|nav|aside|form)\b[^>]*\baria-labelledby=\"([^\"]+)\"")


def focus_order(document: str) -> list[dict]:
    """Every focusable element in document order, the way Tab reaches them.

    The contract is about order, so this reads the markup instead of a class
    name: an anchor with an href, a button, an input that is neither hidden
    nor disabled, a select, a textarea — minus `tabindex="-1"`.
    """
    stops = []
    for match in FOCUSABLE.finditer(document):
        tag = match.group(1).lower()
        raw = match.group(2)
        attributes = {name.lower(): value for name, value in ATTRIBUTE.findall(raw)}
        if re.search(r"(?:^|\s)disabled(?:\s|=|$)", raw):
            continue
        if attributes.get("type", "").lower() == "hidden":
            continue
        if attributes.get("tabindex") == "-1":
            continue
        stops.append({"tag": tag, "attributes": attributes, "at": match.start()})
    return stops


def main_id(document: str) -> str | None:
    """The id of the page's `<main>`, or None when it carries none."""
    element = re.search(r"<main\b([^>]*)>", document)
    if element is None:
        return None
    attribute = re.search(r'\bid="([^"]+)"', element.group(1))
    return attribute.group(1) if attribute else None


def css_block(document: str, opener: str) -> str:
    """The body of the first at-rule whose prelude is `opener`."""
    start = document.index(opener)
    opening = document.index("{", start)
    depth, index = 1, opening + 1
    while index < len(document) and depth:
        if document[index] == "{":
            depth += 1
        elif document[index] == "}":
            depth -= 1
        index += 1
    return document[opening + 1 : index - 1]


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


class KeyboardContractTests(unittest.TestCase):
    """BRAND.md §Accessibility tokens, Keyboard row, on every page under web/.

    "A skip link is the first focusable element when a page has more than one
    landmark." The order is read from the document, not from a class name: a
    page fails when the first thing Tab reaches is anything but an anchor whose
    href is that page's own main. The rule's declarations are read one by one,
    because a selector string alone let `web/roadmap.html`'s ring-less copy
    through — its `.skip:focus` revealed the link and let the user agent paint
    the ring, not the sheet's 2 px `--ink` at 3 px.
    """

    # name: (headings, named regions in document order, headers). The claim
    # template keeps its three state blocks, so three headings and the same
    # "next" region twice are the file's shape; the rendered states are pinned
    # in test_claim_page. first-visit.html and the two /verify files carry
    # state blocks too, and their shapes are pinned by their own modules.
    SHAPE = {
        # ODL-583: the account page keeps its state blocks in the file (one
        # visible per render) — the four row states plus the signed-in 404
        # (Brand gate round 2, should 2); one <main>, no named regions and no
        # <header>. ODL-578 adds the account's own actions as one named
        # region (`account-password`: the change form, the two reset links and
        # the lost-code block). ODL-582 adds the re-pointed confirm state, a
        # sixth heading in the file (one visible per render).
        "account.html": (6, ["account-password"], 0),
        # ODL-582: the console is two states (the admin's page and the
        # refusal), one visible per render; the admin's page names the one
        # action and the account table as two regions, in that order — the
        # action leads so it is above the fold (Brand gate round 1, must 4).
        "admin.html": (2, ["repoint", "accounts"], 1),
        "ceremony.html": (1, ["visit", "machine", "support"], 1),
        "claim.html": (3, ["who", "next", "next"], 1),
        # ODL-578: the reset page is register.html's multi-state shape — the
        # form and the success page, one visible per render — with no named
        # regions of its own.
        "forgot.html": (2, [], 1),
        # ODL-341: the register page is claim.html's three-state shape — the
        # form, the check-your-email page and the confirm button, one visible
        # per render — with no aria-labelledby regions of its own.
        "register.html": (3, [], 1),
        "faq.html": (1, ["short", "gaps", "keys", "failure", "support", "place", "when"], 0),
        "first-visit-preview.html": (1, ["signin-name"], 1),
        "index.html": (1, [], 0),
        "privacy.html": (1, ["short", "hq", "onboard", "leaves", "never", "retention", "access", "logs"], 0),
        "roadmap.html": (1, ["now-title", "needs-title", "gates-title"], 1),
        "terms.html": (1, ["yours", "use", "egress", "suspension", "cancel", "uptime", "changes"], 0),
    }
    ELSEWHERE = {"first-visit.html", "verify.html", "verify-public.html"}

    def pages(self) -> list[Path]:
        found = sorted(WEB.glob("*.html"))
        self.assertIn(WEB / "index.html", found)
        return found

    def test_the_first_tab_stop_on_a_multi_landmark_page_is_the_skip_link_to_its_main(self):
        for page in self.pages():
            document = page.read_text(encoding="utf-8")
            if 1 + len(REGION.findall(document)) <= 1:
                continue
            with self.subTest(page=page.name):
                target = main_id(document)
                self.assertIsNotNone(target, "the page's <main> carries no id")
                stops = focus_order(document)
                self.assertTrue(stops, "the page has no focusable element")
                first = stops[0]
                self.assertEqual(first["tag"], "a",
                                 "the first tab stop is <%s>, not the skip link" % first["tag"])
                self.assertEqual(first["attributes"].get("href"), "#" + str(target),
                                 "the skip link points at %r, not the page's main"
                                 % first["attributes"].get("href"))
                self.assertLess(first["at"], document.index("<main"),
                                "the skip link sits inside main, not before it")
                self.assertEqual(document.count('href="#%s"' % target), 1,
                                 "more than one link targets the page's main")

    def test_every_skip_link_is_off_screen_until_a_keyboard_focus_paints_the_ring(self):
        for page in self.pages():
            document = page.read_text(encoding="utf-8")
            target = main_id(document)
            stops = focus_order(document)
            if not target or not stops:
                continue
            first = stops[0]
            if not (first["tag"] == "a" and first["attributes"].get("href") == "#" + target):
                continue
            with self.subTest(page=page.name):
                self.assertIn(".skip{position:absolute;left:-9999px;top:0}", document,
                              "the skip link is not the sheet's off-screen element")
                rule = re.search(r"\.skip:focus-visible\{([^}]*)\}", document)
                self.assertIsNotNone(rule, "no :focus-visible rule reveals the skip link")
                assert rule is not None  # the assertion above names the failure
                declarations = rule.group(1)
                self.assertIn("left:24px", declarations, "the link stays off screen")
                self.assertIn("min-height:44px", declarations,
                              "the link is under the target floor")
                self.assertIn("background:var(--ink)", declarations)
                self.assertIn("color:var(--paper)", declarations)
                self.assertIn("outline:2px solid var(--ink)", declarations,
                              "the focus ring is not the sheet's 2 px --ink")
                self.assertIn("outline-offset:3px", declarations,
                              "the focus ring is not at the sheet's 3 px offset")
                self.assertNotIn(".skip:focus{", document,
                                 "the link shows on a mouse focus too; the sheet says "
                                 ":focus-visible")
                self.assertIn(".skip:focus-visible{left:18px}",
                              css_block(document, "@media (max-width:560px)"),
                              "the link does not follow main's 18 px inset at 390")

    def test_each_page_keeps_its_one_heading_and_its_landmarks(self):
        for page in self.pages():
            if page.name not in self.SHAPE:
                self.assertIn(page.name, self.ELSEWHERE,
                              "%s has no pinned heading/landmark shape" % page.name)
        for name, (headings, regions, headers) in self.SHAPE.items():
            document = (WEB / name).read_text(encoding="utf-8")
            with self.subTest(page=name):
                self.assertEqual(len(re.findall(r"<h1\b", document)), headings,
                                 "a heading moved")
                self.assertEqual(REGION.findall(document), regions,
                                 "a landmark was added or removed")
                self.assertEqual(len(re.findall(r"<main\b", document)), 1)
                self.assertEqual(len(re.findall(r"<header\b", document)), headers)


if __name__ == "__main__":
    unittest.main()
