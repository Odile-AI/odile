"""Tests for the claims lint.

The lint is part of `make check`: no public sentence about privacy, security or
reliability ships unless docs/CLAIMS.md allows it. These tests prove both
halves — the real tree is clean, and a planted forbidden phrase in any scanned
surface (a public page, the deck's own prose, a future FAQ) fails the lint.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import claims_lint  # noqa: E402

FAQ = ROOT / "web" / "faq.html"

# CLAIMS.md §3, in order: the FAQ states them before it answers anything.
GAP_TITLES = (
    "The app layer is not hardware-measured yet.",
    "The console passes through the host.",
    "Odile keeps three powers.",
)

# The one sentence the deck does not govern: the link back to the list. Headings
# and the brand link are page furniture and are skipped structurally; every
# other sentence in <main> must be a row in the deck.
UI_SENTENCES = ("Back to the list",)


def run_lint(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / "claims_lint.py"), "--root", str(root)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def load_patterns() -> list[tuple[str, re.Pattern[str]]]:
    return [
        (phrase, claims_lint.phrase_pattern(phrase))
        for phrase in claims_lint.load_phrases(ROOT / "docs" / "CLAIMS.md")
    ]


def normalise(text: str) -> str:
    """One comparison space for a sentence: no markdown, curly quotes or dashes."""
    text = html.unescape(text)
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def deck_copy(root: Path) -> str:
    """The deck's approved copy, minus the forbidden-phrase table."""
    text = (root / "docs" / "CLAIMS.md").read_text(encoding="utf-8")
    text = re.sub(
        r"<!--\s*claims-lint:off\s*-->.*?<!--\s*claims-lint:on\s*-->",
        " ",
        text,
        flags=re.S,
    )
    return normalise(text)


class _MainText(HTMLParser):
    """The page's visible prose, as a browser would join it.

    Headings are skipped: they are page furniture (checked against the deck by
    the ordering test and by the phrase lint), and a heading has no full stop,
    so it would otherwise fuse with the sentence that follows it.
    """

    BLOCKS = {"main", "section", "p", "li", "dt", "dd", "h1", "h2", "h3", "ul", "ol", "dl", "br"}
    HEADINGS = {"h1", "h2", "h3"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.in_main = False
        self.in_heading = False
        self.in_brand = False
        # A table is an inventory, not prose: its cells are field names, reasons
        # and retention fragments, checked by tests/test_legal.py against the
        # schema, not by the sentence mapping. The FAQ has no table; the privacy
        # page is almost all table, so without this every cell would fuse into
        # one unreadable sentence.
        self.in_table = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "main":
            self.in_main = True
        elif tag == "a" and "brand" in (dict(attrs).get("class") or ""):
            self.in_brand = True
        elif tag == "table":
            self.in_table = True
        elif tag in self.HEADINGS:
            self.in_heading = True
        elif tag in self.BLOCKS:
            self.chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "main":
            self.in_main = False
        elif tag == "a" and self.in_brand:
            self.in_brand = False
        elif tag == "table":
            self.in_table = False
        elif tag in self.HEADINGS:
            self.in_heading = False
            self.chunks.append(" ")
        elif tag in self.BLOCKS:
            self.chunks.append(" ")

    def handle_data(self, data: str) -> None:
        if self.in_main and not self.in_heading and not self.in_brand and not self.in_table:
            self.chunks.append(data)


def body_text(page: str) -> str:
    parser = _MainText()
    parser.feed(page)
    return normalise("".join(parser.chunks))


def sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]


class ClaimsDeckTests(unittest.TestCase):
    def test_lint_list_is_parsed_from_the_deck(self):
        phrases = claims_lint.load_phrases(ROOT / "docs" / "CLAIMS.md")
        self.assertIn("unhackable", phrases)
        self.assertIn("zero-knowledge", phrases)
        self.assertGreaterEqual(len(phrases), 10)

    def test_v2_access_phrases_are_forbidden(self):
        phrases = claims_lint.load_phrases(ROOT / "docs" / "CLAIMS.md")
        for phrase in (
            "we hold your keys",
            "we can log in",
            "operator access",
            "no one can access",
        ):
            self.assertIn(phrase, phrases)

    def test_phrase_matching_handles_hyphenation_and_case(self):
        pattern = claims_lint.phrase_pattern("zero knowledge")
        self.assertTrue(pattern.search("ZERO-KNOWLEDGE"))
        self.assertTrue(pattern.search("zero knowledge"))
        self.assertFalse(pattern.search("knowledgeless"))

    def test_off_block_is_skipped(self):
        patterns = [("unhackable", claims_lint.phrase_pattern("unhackable"))]
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "page.html"
            page.write_text(
                "<!-- claims-lint:off -->\n"
                "<p>unhackable</p>\n"
                "<!-- claims-lint:on -->\n"
                "<p>verifiable</p>\n",
                encoding="utf-8",
            )
            self.assertEqual(claims_lint.scan(page, patterns), [])

    def test_unclosed_off_block_is_reported(self):
        patterns = [("unhackable", claims_lint.phrase_pattern("unhackable"))]
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "page.html"
            page.write_text(
                "<!-- claims-lint:off -->\n<p>unhackable</p>\n",
                encoding="utf-8",
            )
            findings = claims_lint.scan(page, patterns)
        self.assertEqual(len(findings), 1)
        self.assertIn("no matching claims-lint:on", findings[0])

    def test_marker_mentioned_in_prose_does_not_toggle(self):
        patterns = [("unhackable", claims_lint.phrase_pattern("unhackable"))]
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "page.html"
            page.write_text(
                "<p>Fence the table with a claims-lint:off block.</p>\n"
                "<p>unhackable</p>\n",
                encoding="utf-8",
            )
            findings = claims_lint.scan(page, patterns)
        self.assertEqual(len(findings), 1)
        self.assertIn("page.html:2", findings[0])

    def test_deck_prose_is_clean_outside_the_off_block(self):
        self.assertEqual(claims_lint.scan(ROOT / "docs" / "CLAIMS.md", load_patterns()), [])

    def test_the_deck_carries_the_strict_bar_rows(self):
        # ODL-459/470/471/472 quote these; the pages and the ready email ship
        # first, so the deck must lead them. v3.9 replaced the v3.4 Model A
        # rows (a user boot key, a passphrase, a sign-in set during install).
        text = (ROOT / "docs" / "CLAIMS.md").read_text(encoding="utf-8")
        for sentence in (
            "No one at Odile can log in to a tenant machine. The only sign-in is yours.",
            "The boot key in the command line is a public key with no private half. "
            "It is plumbing, not a credential.",
            "The disk unlocks only on the boot you verify. Change the image, and it stays locked.",
            "There is no passphrase to type. The disk key is sealed to the measured boot, "
            "and no one at Odile holds it.",
            "After a reboot, it starts itself. The seal is the key.",
            "Your Odile sign-in is set on your machine at first visit and hashed there. "
            "We never see it. It is the only app-layer gate: no 2FA in V0.1.",
            "A support grant is a time-boxed door into the running machine. It ends for "
            "new logins when it expires or you revoke it.",
            "At HQ we hold your email, the record of your machine, and a boot key with "
            "no private half. Not the disk key, not your sign-in.",
            "A repeating job — the morning brief, a watchdog — runs when the machine "
            "comes back up if it was due while it was down.",
        ):
            self.assertIn(sentence, text)

    def test_a_held_row_is_not_on_a_public_page(self):
        # ODL-466's verdict blocks these on the re-key (t_28dda3b7), the
        # pre-first-visit gate (t_adcdec31) and the M2 drills; CLAIMS.md §1a
        # holds them from the public pages until their card lands. The deck
        # keeps the row, the page does not quote the clause.
        held = (
            "The only sign-in is yours.",
            "The disk unlocks only on the boot you verify.",
            "The disk key is sealed to the measured boot",
            "Not the disk key",
            "We can stop it. We cannot read it.",
            "On a tenant machine, Odile cannot read the disk",
            "The hosting provider sees ciphertext",
            "Backed up nightly",
            "The encrypted backup is kept 30 days",
            "backups are off-site",
            "the file opens your backup",
            "Claiming takes 3 minutes",
        )
        # web/first-visit.html is the installer's embedded FIRST_VISIT_PAGE
        # (pinned byte-for-byte by tests/test_first_visit.py); re-cutting its A9
        # row needs a re-embed, a re-sign and a new release cut, so ODL-474 owns
        # it (its touches and DoD name the page). Every page this landing owns is
        # scanned.
        surfaces = [
            path for path in sorted((ROOT / "web").glob("*.html"))
            if path.name != "first-visit.html"
        ] + [ROOT / "README.md"]
        for path in surfaces:
            text = path.read_text(encoding="utf-8")
            for clause in held:
                self.assertNotIn(clause, text, "%s quotes a held clause: %r" % (path.name, clause))


class FaqTests(unittest.TestCase):
    """The FAQ is public copy: every sentence maps to a row, gaps first."""

    def test_the_faq_exists_and_is_scanned(self):
        self.assertTrue(FAQ.is_file(), "web/faq.html is the card's page")
        scanned = {
            path.resolve()
            for path in claims_lint.iter_files(ROOT.resolve(), claims_lint.DEFAULT_PATHS)
        }
        self.assertIn(FAQ.resolve(), scanned, "the claims lint does not scan the FAQ")

    def test_every_faq_sentence_maps_to_the_deck(self):
        deck = deck_copy(ROOT)
        allowed = {normalise(sentence) for sentence in UI_SENTENCES}
        unmatched = [
            sentence
            for sentence in sentences(body_text(FAQ.read_text(encoding="utf-8")))
            if normalise(sentence) not in allowed and normalise(sentence) not in deck
        ]
        self.assertEqual(unmatched, [], "sentences with no CLAIMS.md row: %r" % (unmatched,))

    def test_the_faq_states_the_three_gaps_before_the_first_question(self):
        page = FAQ.read_text(encoding="utf-8")
        self.assertIn("<dt>", page, "the FAQ has no questions")
        first_question = page.index("<dt>")
        positions = []
        for gap in GAP_TITLES:
            self.assertIn(gap, page)
            positions.append(page.index(gap))
        self.assertEqual(positions, sorted(positions), "the gaps are out of order")
        self.assertLess(max(positions), first_question, "the gaps come after the first question")

    def test_the_short_version_scopes_the_no_login_claim(self):
        # The read-through accepted this: "No one at Odile can log in" is false
        # during a user-granted support session, so the page's summary and the
        # meta description carry the scoped row, not the absolute one.
        page = FAQ.read_text(encoding="utf-8")
        short = re.search(
            r'<section aria-labelledby="short">(.*?)</section>', page, re.S
        )
        self.assertIsNotNone(short, "the FAQ has no short version")
        assert short is not None
        self.assertIn("No one at Odile can log in unless you grant it", short.group(1))
        self.assertNotIn("No one at Odile can log in to your machine", short.group(1))
        meta = re.search(r'<meta name="description" content="([^"]+)"', page)
        self.assertIsNotNone(meta, "the FAQ has no meta description")
        assert meta is not None
        self.assertIn("No one at Odile can log in unless you grant it", meta.group(1))


class PlantedPhraseTests(unittest.TestCase):
    """A clean tree passes; a phrase planted in any scanned surface fails."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "docs").mkdir()
        shutil.copyfile(ROOT / "docs" / "CLAIMS.md", self.root / "docs" / "CLAIMS.md")

    def test_default_scan_covers_the_deck_and_the_future_faq(self):
        self.assertIn("docs/CLAIMS.md", claims_lint.DEFAULT_PATHS)
        self.assertIn("docs/FAQ.md", claims_lint.DEFAULT_PATHS)

    def test_clean_tree_scans_the_deck(self):
        scanned = {
            path.resolve()
            for path in claims_lint.iter_files(ROOT.resolve(), claims_lint.DEFAULT_PATHS)
        }
        self.assertIn((ROOT / "docs" / "CLAIMS.md").resolve(), scanned)

    def test_planted_phrase_in_a_public_page_fails(self):
        web = self.root / "web"
        web.mkdir()
        (web / "index.html").write_text("<p>We hold your keys.</p>\n", encoding="utf-8")
        result = run_lint(self.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("web/index.html:1", result.stderr)
        self.assertIn("we hold your keys", result.stderr)

    def test_planted_phrase_in_the_faq_page_fails(self):
        web = self.root / "web"
        web.mkdir()
        (web / "faq.html").write_text(
            "<p>No one can access the machine.</p>\n", encoding="utf-8"
        )
        result = run_lint(self.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("web/faq.html:1", result.stderr)
        self.assertIn("no one can access", result.stderr)

    def test_planted_phrase_in_deck_prose_fails(self):
        deck = self.root / "docs" / "CLAIMS.md"
        deck.write_text(
            deck.read_text(encoding="utf-8") + "\nThe operator access is included.\n",
            encoding="utf-8",
        )
        result = run_lint(self.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("docs/CLAIMS.md", result.stderr)
        self.assertIn("operator access", result.stderr)

    def test_planted_phrase_inside_the_off_block_is_skipped(self):
        deck = self.root / "docs" / "CLAIMS.md"
        deck.write_text(
            deck.read_text(encoding="utf-8").replace(
                "<!-- claims-lint:off -->",
                "<!-- claims-lint:off -->\n\nOperator access.\n",
                1,
            ),
            encoding="utf-8",
        )
        result = run_lint(self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_planted_phrase_in_an_existing_faq_fails(self):
        (self.root / "docs" / "FAQ.md").write_text(
            "No one can access the machine.\n", encoding="utf-8"
        )
        result = run_lint(self.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("docs/FAQ.md:1", result.stderr)


class PrivacyTests(unittest.TestCase):
    """The privacy page is public copy too: prose must be a deck row."""

    PAGE = ROOT / "web" / "privacy.html"

    def test_the_page_exists_and_is_scanned(self):
        self.assertTrue(self.PAGE.is_file(), "web/privacy.html is the card's page")
        scanned = {
            path.resolve()
            for path in claims_lint.iter_files(ROOT.resolve(), claims_lint.DEFAULT_PATHS)
        }
        self.assertIn(self.PAGE.resolve(), scanned, "the claims lint does not scan the privacy page")

    def test_every_privacy_sentence_maps_to_the_deck(self):
        deck = deck_copy(ROOT)
        allowed = {normalise(sentence) for sentence in UI_SENTENCES}
        unmatched = [
            sentence
            for sentence in sentences(body_text(self.PAGE.read_text(encoding="utf-8")))
            if normalise(sentence) not in allowed and normalise(sentence) not in deck
        ]
        self.assertEqual(unmatched, [], "sentences with no CLAIMS.md row: %r" % (unmatched,))


class ReadmeTests(unittest.TestCase):
    """The README's opening is public copy: every sentence is a deck row.

    `claims_lint` catches a forbidden phrase, not an unapproved sentence. Savio
    rewrote the README on the public tree on 2026-11-14 (`2d8999d`, `a5c3428`)
    and its headline, its lede and one closing clause were privacy sentences no
    row carried; the lint stayed green (t_0d4ebfd8). The FAQ and the privacy
    page already have this test; the opening block is the part Savio rewrites by
    hand, so it gets one too. The rest of the README is component lists and the
    demo table, not claims.
    """

    PAGE = ROOT / "README.md"

    def opening(self) -> str:
        text = self.PAGE.read_text(encoding="utf-8")
        head = text.split("\n## ", 1)[0]
        # The H1 is the product name, not a sentence; a heading has no full stop
        # and would otherwise fuse with the headline under the splitter.
        body = "\n".join(line for line in head.splitlines() if not line.startswith("# "))
        # A markdown link is a citation, not copy: compare the words.
        body = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", body)
        return normalise(body)

    def test_every_readme_opening_sentence_maps_to_the_deck(self):
        deck = deck_copy(ROOT)
        allowed = {normalise(sentence) for sentence in UI_SENTENCES}
        unmatched = [
            sentence
            for sentence in sentences(self.opening())
            if normalise(sentence) not in allowed and normalise(sentence) not in deck
        ]
        self.assertEqual(unmatched, [], "sentences with no CLAIMS.md row: %r" % (unmatched,))

    def test_the_opening_span_is_not_empty(self):
        # A silent pass is the failure mode: if the split stops finding the
        # opening, the mapping above would check nothing. Pin its shape.
        self.assertGreaterEqual(len(sentences(self.opening())), 3)


class PublicSurfaceTests(unittest.TestCase):
    def test_public_surfaces_pass_the_lint(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "claims_lint.py"), "--root", str(ROOT)],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("claims lint: OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
