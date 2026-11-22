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
        # rows (a user boot key, a passphrase, a sign-in set during install);
        # v3.11 (ODL-577) replaced the first-visit sign-in row with D-1, the
        # operator row with D-3 and the HQ data row with D-8.
        text = (ROOT / "docs" / "CLAIMS.md").read_text(encoding="utf-8")
        for sentence in (
            "No one at Odile holds a key to your machine: no standing key, "
            "no root password, no disk passphrase. The password you chose is "
            "the only sign-in.",
            "The boot key in the command line is a public key with no private half. "
            "It is plumbing, not a credential.",
            "The disk unlocks only on the boot you verify. Change the image, and it stays locked.",
            "There is no passphrase to type. The disk key is sealed to the measured boot, "
            "and no one at Odile holds it.",
            "After a reboot, it starts itself. The seal is the key.",
            "Your Odile password opens your machine and your account page. "
            "You chose it when you registered. It is the only app-layer gate: no 2FA in alpha.0.",
            "A support grant is a time-boxed door into the running machine. It ends for "
            "new logins when it expires or you revoke it.",
            "At HQ we hold your email, the record of your machine, a boot key with "
            "no private half, a hash of your password and a hash of your recovery code. "
            "Not the disk key. Not your password itself.",
            "A repeating job — the morning brief, a watchdog — runs when the machine "
            "comes back up if it was due while it was down.",
        ):
            self.assertIn(sentence, text)

    def test_a_held_row_is_not_on_a_public_page(self):
        # ODL-466's verdict blocks these on the re-key (t_28dda3b7), the
        # sign-in window (ODL-925) and the M2 drills; CLAIMS.md §1a
        # holds them from the public pages until their card lands. The deck
        # keeps the row, the page does not quote the clause. ODL-474 landed the
        # backup and grant rows as v3.10 and t_5f10dd14 landed the 30-day
        # retention row on 19 November 2026; what is left is the disk-custody
        # and sign-in clusters, "Backups are off-site" (ADR-004 measures
        # the destination on HQ's disk until the R2 migration lands), and the
        # dropped timing. ODL-521/522/523 cleared the image's public bench
        # password that §0.2 named; ODL-925 is what writes the registration
        # verifier at provision and turns the first visit into a login.
        held = (
            "The password you chose is the only sign-in.",
            "It is the only app-layer gate",
            "The disk unlocks only on the boot you verify.",
            "The disk key is sealed to the measured boot",
            "Not the disk key",
            "We can stop it. We cannot read it.",
            "On a tenant machine, Odile cannot read the disk",
            "The hosting provider sees ciphertext",
            "off-site",
            "Claiming takes 3 minutes",
        )
        surfaces = sorted((ROOT / "web").glob("*.html")) + [ROOT / "README.md"]
        for path in surfaces:
            text = path.read_text(encoding="utf-8").lower()
            for clause in held:
                self.assertNotIn(clause.lower(), text,
                                 "%s quotes a held clause: %r" % (path.name, clause))

    def test_the_v310_landing_is_verbatim_on_its_surfaces(self):
        # The deck leads the pages: the header, the change-log row and every
        # page carry the same date and the same rows (ODL-474's fourth DoD).
        # v3.11 (ODL-577) re-cut the three sentences this test pinned, so the
        # version pin moves and the two passages D-6 keeps verbatim stay.
        deck = (ROOT / "docs" / "CLAIMS.md").read_text(encoding="utf-8")
        self.assertIn("v3.11", deck.splitlines()[0])
        self.assertIn("| 2026-11-14 | v3.10:", deck)
        faq = (ROOT / "web" / "faq.html").read_text(encoding="utf-8")
        terms = (ROOT / "web" / "terms.html").read_text(encoding="utf-8")
        claim = (ROOT / "web" / "claim.html").read_text(encoding="utf-8")
        first = (ROOT / "web" / "first-visit.html").read_text(encoding="utf-8")
        for passage in (
            "A recovery code and a recovery file are the way back: the code resets "
            "your sign-in, the file opens your backup.",
            "We rebuild it; you ask the agent to restore last night's backup with "
            "your recovery file",
            "If the disk will not open, the machine is rebuilt and you restore from "
            "your backup.",
            "Ask the agent to run <code>odile-diag</code> and email the bundle to "
            "help@odile.fyi.",
        ):
            self.assertIn(passage, faq, "web/faq.html does not carry %r" % passage)
        self.assertIn("Backed up nightly. Rebuildable in under an hour — without a key.",
                      terms, "web/terms.html does not carry the reliability row")
        self.assertNotIn("Backed up nightly", faq,
                         "web/faq.html gained the reliability row; ODL-474 keeps it off")
        self.assertIn(
            "A support grant is a time-boxed door into the running machine. It ends "
            "for new logins when it expires or you revoke it.",
            claim, "web/claim.html does not carry A10 back",
        )
        self.assertEqual(
            first.count("the code resets your sign-in, the file opens your backup"), 2,
            "web/first-visit.html's two done pages must carry A9's full sentence",
        )


class RecoveryCodeMemoTests(unittest.TestCase):
    """The deck lands the memo's rows verbatim, not a paraphrase of them.

    The rows and reductions are `docs/product/DECISION-one-recovery-code.md` §4
    (Savio, 20 November 2026). That memo is an HQ-only record — it sits in
    `.git/info/exclude` — so a public clone skips the comparison and the deck's
    own text is still asserted above; on HQ the comparison runs and a reworded
    row fails here before a page quotes it.
    """

    MEMO = ROOT / "docs" / "product" / "DECISION-one-recovery-code.md"

    def memo(self) -> str:
        if not self.MEMO.is_file():
            self.skipTest("the HQ-only memo is absent in a public clone")
        return self.MEMO.read_text(encoding="utf-8")

    def deck(self) -> str:
        return normalise((ROOT / "docs" / "CLAIMS.md").read_text(encoding="utf-8"))

    def test_every_row_the_memo_approves_is_in_the_deck_verbatim(self):
        memo = self.memo()
        rows = re.findall(r'^\| \*\*(D-\d+)\*\* \| "(.*?)" \|', memo, re.M)
        self.assertGreaterEqual(len(rows), 13, "the memo's §4 table did not parse")
        deck = self.deck()
        missing = [rid for rid, sentence in rows if normalise(sentence) not in deck]
        self.assertEqual(missing, [], "rows the memo approves and the deck does not carry")
        self.assertIn("D-1", {rid for rid, _ in rows})
        self.assertIn("D-20", {rid for rid, _ in rows})

    def test_every_reduction_the_memo_records_is_in_the_deck(self):
        memo = self.memo()
        rows = re.findall(r'^\| \*\*(D-1[2-7]|D-19)\*\* \| [^|]+ \| "(.*?)" \(', memo, re.M)
        self.assertEqual(sorted(rid for rid, _ in rows),
                         ["D-12", "D-13", "D-14", "D-15", "D-16", "D-17", "D-19"])
        deck = self.deck()
        missing = [rid for rid, sentence in rows if normalise(sentence) not in deck]
        self.assertEqual(missing, [], "reductions the memo records and the deck does not carry")

    def test_the_deck_names_no_row_the_memo_does_not(self):
        memo = self.memo()
        approved = set(re.findall(r"^\| \*\*(D-\d+)\*\*", memo, re.M))
        deck = (ROOT / "docs" / "CLAIMS.md").read_text(encoding="utf-8")
        # One or two digits only: `D-002` is the claims-deck decision's own
        # number (`docs/AGENT-ORG.md` §3), not a row in this memo.
        named = {"D-" + digits for digits in re.findall(r"\bD-(\d{1,2})\b", deck)}
        self.assertEqual(sorted(named - approved), [],
                         "a D-row the deck cites is not in the memo")


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

    def test_the_backup_retention_row_is_verbatim_on_both_pages(self):
        # t_5f10dd14: the 30-day retention row is a §1 row and lands verbatim.
        # The privacy page's retention list is its home; the FAQ's place answer
        # carries it beside the Montreal sentence, so a reader asking "where is
        # the machine?" is told what happens to the copy after cancellation.
        row = (
            "The encrypted backup is kept 30 days after cancellation, "
            "then deleted. Odile can delete it, not read it."
        )
        privacy = self.PAGE.read_text(encoding="utf-8")
        faq = FAQ.read_text(encoding="utf-8")
        self.assertIn(row, privacy, "web/privacy.html does not carry the 30-day retention row")
        self.assertIn(row, faq, "web/faq.html does not carry the 30-day retention row")
        # The destination cell is measured, not the old "off-site SFTP box"
        # (ADR-004 §1: the bytes are on HQ's disk until the R2 migration lands).
        self.assertIn("an SFTP storage box at HQ", privacy)

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
