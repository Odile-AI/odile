#!/usr/bin/python3
"""The legal pages against the machine.

Three checks that make "paperwork that matches the machine" mechanical:

* every table and column in every store is named in the privacy inventory,
  and the inventory names nothing the schema does not have (a column added
  without a page row is a bug; a page row without a column is a stale page);
* every access-model sentence on the privacy page is a row in
  `docs/CLAIMS.md`, verbatim;
* the legal pages carry no JavaScript, no third-party request and no cookie
  banner.

The stores are the same schema strings the services execute, read through an
in-memory database, so a migration that forgets the page fails here.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from odilectl import health, state  # noqa: E402
import seed.seed as seed  # noqa: E402

PAGES = {
    "privacy": ROOT / "web" / "privacy.html",
    "terms": ROOT / "web" / "terms.html",
}

# Rows that name a data item of the system but not a column in these five
# stores: the guest's own files, Stripe, and the in-memory rate limit. They
# must stay on the page even though the schema cannot remind us about them.
PLANNED_ITEMS = (
    "The signup rate limit",
    "Stripe",
    "The Hermes home",
    "The Odile sign-in",
    "Your machine's NEAR AI key",
    "The backup status file",
    "The support log",
    "The service journal",
    "The recovery file",
    "The LUKS keyslot",
    "Support and abuse mail",
)

# The access model, exactly as the deck states it. The privacy page must carry
# each row verbatim; a rewording is a new claim. v3.9 (ODL-462) publishes the
# proven half of each row: A1's operator sentence, A2 whole, A4's no-passphrase
# sentence, and "We can stop it."; the clauses ODL-466 marks BLOCK are held
# (docs/CLAIMS.md §1a) until t_28dda3b7 and t_adcdec31 land.
ACCESS_ROWS = (
    "No one at Odile can log in to a tenant machine.",
    "No one at Odile can log in unless you grant it: no standing key, no root password, no disk passphrase.",
    "The boot key in the command line is a public key with no private half. It is plumbing, not a credential.",
    "There is no passphrase to type.",
    "Support access exists only when you grant it, for as long as you grant it.",
    "A grant is a handover, not a peek.",
    "We can stop it.",
)


def schema_columns() -> set[str]:
    """Every `table.column` in every store, read from the live schema strings."""
    con = sqlite3.connect(":memory:")
    for script in (app.SCHEMA, state.SCHEMA, seed.SEED_SCHEMA, health.HEARTBEAT_SCHEMA):
        con.executescript(script)
    found: set[str] = set()
    for table in [row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]:
        for row in con.execute("PRAGMA table_info(%s)" % table):
            found.add("%s.%s" % (table, row[1]))
    con.close()
    return found


class _Inventory(HTMLParser):
    """Collect the `table.column` tokens from the inventory tables only."""

    TOKEN = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.in_code = False
        self.tokens: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table" and "inventory" in (dict(attrs).get("class") or ""):
            self.in_table = True
        elif tag == "code" and self.in_table:
            self.in_code = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "code":
            self.in_code = False
        elif tag == "table":
            self.in_table = False

    def handle_data(self, data: str) -> None:
        if self.in_code and self.in_table:
            for token in data.split():
                if self.TOKEN.match(token):
                    self.tokens.append(token)


def page_tokens(path: Path) -> set[str]:
    parser = _Inventory()
    parser.feed(path.read_text(encoding="utf-8"))
    return set(parser.tokens)


class InventoryTests(unittest.TestCase):
    def test_every_column_in_every_store_is_in_the_inventory(self):
        need = schema_columns()
        have = page_tokens(PAGES["privacy"])
        self.assertEqual(
            sorted(need - have), [],
            "columns in a store with no row in web/privacy.html",
        )

    def test_the_inventory_names_no_column_the_schema_does_not_have(self):
        need = schema_columns()
        have = page_tokens(PAGES["privacy"])
        self.assertEqual(
            sorted(have - need), [],
            "web/privacy.html names a field that no store has",
        )

    def test_the_page_names_every_table(self):
        text = PAGES["privacy"].read_text(encoding="utf-8")
        for table in sorted({column.split(".")[0] for column in schema_columns()}):
            self.assertIn("<code>%s." % table, text, "table %s has no row" % table)

    def test_the_rows_the_schema_cannot_remind_us_about_are_present(self):
        text = PAGES["privacy"].read_text(encoding="utf-8")
        for item in PLANNED_ITEMS:
            self.assertIn(item, text, "inventory row missing: %s" % item)

    def test_the_meter_row_is_retired_and_the_key_row_replaces_it(self):
        # Savio, 2026-11-13: the meter is gone; a tenant's inference runs on
        # its own NEAR AI key (f884184, merged 0a4b726). The page must not
        # assert a meter again, and the key store must be named — it is the one
        # credential a reader looks for.
        page = PAGES["privacy"].read_text(encoding="utf-8")
        deck = (ROOT / "docs" / "CLAIMS.md").read_text(encoding="utf-8")
        self.assertNotIn("The meter on your machine keeps", page)
        self.assertNotIn("The meter on your machine keeps", deck)
        self.assertIn("<code>/etc/odile/key.env</code>", page)
        self.assertIn(
            "Your machine's NEAR AI key is one per user: the only inference "
            "credential on the machine, written at install and read by the agent.",
            deck,
        )

    def test_the_rate_limit_row_names_the_ip_address_the_code_hashes(self):
        # app.allow() hashes the client IP, not the email address; "the
        # address" elsewhere on this page means the email. Name the datum.
        page = PAGES["privacy"].read_text(encoding="utf-8")
        deck = (ROOT / "docs" / "CLAIMS.md").read_text(encoding="utf-8")
        self.assertIn("a short sha256 of the IP address, in memory, ten minutes", page)
        prose = (
            "The signup rate limit keeps a short hash of the IP address in memory "
            "for ten minutes; it never touches disk."
        )
        self.assertIn(prose, page)
        self.assertIn(prose, deck)
        self.assertNotIn("short sha256 of the address", page)
        self.assertNotIn("short hash of the address", page)


class AccessModelTests(unittest.TestCase):
    def test_every_access_sentence_is_a_deck_row(self):
        deck = (ROOT / "docs" / "CLAIMS.md").read_text(encoding="utf-8")
        for sentence in ACCESS_ROWS:
            self.assertIn(sentence, deck, "the deck lost an access row")

    def test_the_privacy_page_states_the_access_model_verbatim(self):
        page = PAGES["privacy"].read_text(encoding="utf-8")
        for sentence in ACCESS_ROWS:
            self.assertIn(sentence, page, "the page reworded an access row")

    def test_the_privacy_page_says_there_are_no_cookies(self):
        page = PAGES["privacy"].read_text(encoding="utf-8")
        self.assertIn("No cookies, no trackers, no third-party requests.", page)

    def test_the_no_read_row_is_scoped_to_a_tenant_machine(self):
        # The demo is an Odile machine whose key Odile holds; an unscoped
        # "Odile cannot read the disk" is false there. v3.5 scoped the tenant
        # access row for exactly this reason. v3.9 keeps the row in the deck
        # and holds it from the privacy page until the disk re-key (t_28dda3b7):
        # the image's key still opens the tenant disk (ODL-466 §0.1), so the
        # page publishes A1's operator sentence instead. ODL-903 retired the
        # abuse page this check used to read; privacy carries the same
        # operator sentence twice.
        deck = (ROOT / "docs" / "CLAIMS.md").read_text(encoding="utf-8")
        page = PAGES["privacy"].read_text(encoding="utf-8")
        row = (
            "On a tenant machine, Odile cannot read the disk, the prompts or the "
            "files; a support grant can."
        )
        self.assertIn(row, deck)
        self.assertNotIn(row, page, "the privacy page quotes a row held until t_28dda3b7")
        self.assertIn("No one at Odile can log in to a tenant machine.", page)
        self.assertNotIn('- "Odile cannot read the disk', deck)
        self.assertNotIn("<li>Odile cannot read the disk", page)


class NoScriptTests(unittest.TestCase):
    def test_no_page_carries_javascript(self):
        for name, path in PAGES.items():
            with self.subTest(page=name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("<script", text.lower())
                self.assertNotIn("javascript:", text.lower())

    def test_no_page_makes_a_third_party_request(self):
        for name, path in PAGES.items():
            with self.subTest(page=name):
                text = path.read_text(encoding="utf-8")
                self.assertEqual(re.findall(r"https?://", text), [])

    def test_no_page_is_a_cookie_banner(self):
        for name, path in PAGES.items():
            with self.subTest(page=name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("document.cookie", text)
                self.assertNotIn("cookie-banner", text)

    def test_the_pages_are_served_by_the_app(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        for route, page in (("/privacy", "privacy.html"), ("/terms", "terms.html")):
            self.assertIn('if path == "%s":' % route, source)
            self.assertIn('"%s"' % page, source)
