#!/usr/bin/python3
"""Tests for ODL-239, re-cut by ODL-467: the status page from heartbeat history.

Hermetic: a scratch SQLite file, a fixed clock and heartbeats recorded through
the same store the receiver uses.  No tenant machine, no network, no sleeps.

The suite is organised around the card's lines:

* the page shows 30-day uptime, downtime, in-service time and the last five
  incidents — with no owner-wait bucket and no tenant named;
* every number is computed from the heartbeat and transition history;
* a sealed reboot inside the ten-minute window costs no uptime and writes no
  incident;
* the two status/reliability rows are the deck's, verbatim, in the template
  and in its meta description;
* the stylesheet keeps BRAND.md's 4 px scale, the footer link's hover state
  and the incident header row in the accessibility tree at 390 px.

    /usr/bin/python3 -m unittest tests.test_status -v
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odilectl import health, state  # noqa: E402
from status import status as status_page  # noqa: E402

# 08:00 UTC on 8 November 2026.  A fixed clock makes every assertion
# explainable: the window ends here and starts 30 days earlier.
NOW = int(datetime(2026, 11, 8, 8, 0, tzinfo=timezone.utc).timestamp())
HOUR = 60 * 60
DAY = 24 * HOUR
BEAT = 5 * 60
STOP = state.HEARTBEAT_STOP_SECONDS
HOST_KEY = "SHA256:" + "A" * 43
PAGE_TEMPLATE = ROOT / "templates" / "status.html"
PHONE = "@media (max-width:560px)"
SCALE = (4, 8, 12, 16, 24, 32, 48, 64)
STYLE_BLOCK = re.compile(r"<style>(.*?)</style>", re.S)


def _stylesheet(path: Path) -> str:
    match = STYLE_BLOCK.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise AssertionError(f"{path} carries no <style> block")
    return match.group(1)


def _declarations(body: str) -> dict[str, str]:
    declarations: dict[str, str] = {}
    for piece in body.split(";"):
        if ":" not in piece:
            continue
        prop, value = piece.split(":", 1)
        declarations[prop.strip().lower()] = value.strip()
    return declarations


def _rules(source: str, media: str = "") -> list[tuple[str, str, dict[str, str]]]:
    """Every rule as (media query, selector, declarations) — enough CSS for one page."""
    rules: list[tuple[str, str, dict[str, str]]] = []
    index = start = 0
    while index < len(source):
        if source[index] != "{":
            index += 1
            continue
        selector = source[start:index].strip()
        end, depth = index + 1, 1
        while end < len(source) and depth:
            if source[end] == "{":
                depth += 1
            elif source[end] == "}":
                depth -= 1
            end += 1
        body = source[index + 1 : end - 1]
        if selector.startswith("@media"):
            rules.extend(_rules(body, selector))
        elif not selector.startswith("@"):
            rules.append((media, selector, _declarations(body)))
        index = start = end
    return rules


def _rule(
    rules: list[tuple[str, str, dict[str, str]]], selector: str, *, media: str = ""
) -> dict[str, str]:
    for rule_media, rule_selector, declarations in rules:
        if rule_selector == selector and rule_media == media:
            return declarations
    raise AssertionError(f"no rule {selector!r} in {media or 'the sheet'}")


class Clock:
    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class Fixture:
    """One scratch database with the real state and heartbeat schemas."""

    def __init__(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="odile-status-"))
        self.db = self.directory / "waitlist.db"
        self.clock = Clock()
        self.state = state.StateStore.open(self.db, clock=self.clock)
        self.beats = health.HeartbeatStore.open(
            self.db, clock=self.clock, verifier=lambda *_args: True
        )

    def close(self) -> None:
        self.beats.close()
        self.state.close()
        shutil.rmtree(self.directory, ignore_errors=True)

    def tenant(self, slug: str, *, activated_at: int, owner: str = "ada@example.com") -> None:
        """Walk one tenant to `active` at a chosen moment."""
        self.clock.now = activated_at
        self.state.invite(slug, detail={"owner_email": owner})
        self.state.claim(slug)
        self.state.provision(
            slug,
            server_id=f"srv-{slug[-6:]}",
            hostname=slug,
            ipv4="203.0.113.120",
            plan_id=113,
            expires_at=activated_at + 30 * DAY,
            initramfs_host_key=HOST_KEY,
        )
        self.state.await_boot(slug)
        self.state.start_first_visit(slug)
        self.state.ready(slug)

    def beat(self, slug: str, *, at: int) -> None:
        body = json.dumps(
            {
                "slug": slug,
                "at": at,
                "public_key": "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----",
                "facts": {"uptime_seconds": 60},
            },
            sort_keys=True,
        ).encode("utf-8")
        self.clock.now = at
        self.beats.record(body, b"\x00" * 64, now=at)

    def beats_between(self, slug: str, *, start: int, end: int) -> None:
        """One beat every five minutes, endpoints included."""
        moment = start
        while moment <= end:
            self.beat(slug, at=moment)
            moment += BEAT

    def park(self, slug: str, *, at: int) -> None:
        self.clock.now = at
        self.state.park_for_owner(slug, observed_host_key=HOST_KEY, now=at)

    def alert(
        self,
        slug: str,
        kind: str,
        *,
        at: int,
        resolved_at: int | None = None,
        tenant: str = "ada@example.com",
    ) -> None:
        self.clock.now = at
        self.beats.raise_alert(
            health.Alert(
                slug=slug,
                kind=kind,
                tenant=tenant,
                symptom="heartbeat stopped 10m 0s ago",
                first_command=f"python3 -m odilectl status {slug} --json",
                at=at,
            ),
            at,
        )
        if resolved_at is not None:
            self.clock.now = resolved_at
            self.beats.resolve(slug, kind, resolved_at)


class StatusTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)

    def collect(self):
        return status_page.collect(self.fixture.db, now=NOW)

    # ------------------------------------------------------------------ numbers

    def test_uptime_is_measured_from_heartbeat_coverage(self) -> None:
        """A one-hour silence is downtime, counted to the second.

        The machine is active for ten hours and beats every five minutes; the
        beats stop four hours before the window ends and resume one hour later.
        Ten minutes after the last beat is the stop threshold, so the machine
        was down for the remaining fifty minutes of the silence.
        """
        start = NOW - 10 * HOUR
        gap_start = NOW - 4 * HOUR
        gap_end = NOW - 3 * HOUR
        self.fixture.tenant("odl-aa11bb", activated_at=start)
        self.fixture.beats_between("odl-aa11bb", start=start, end=gap_start)
        self.fixture.beats_between("odl-aa11bb", start=gap_end, end=NOW)

        totals = self.collect()

        self.assertEqual(totals.accountable_seconds, 10 * HOUR)
        self.assertEqual(totals.down_seconds, HOUR - STOP)
        self.assertEqual(totals.up_seconds, 10 * HOUR - (HOUR - STOP))
        self.assertEqual(totals.tenants, 1)
        self.assertAlmostEqual(totals.uptime_percent, 91.666, places=2)
        self.assertFalse(hasattr(totals, "waiting_seconds"), "no owner-wait bucket")

    def test_a_sealed_reboot_inside_the_window_loses_no_uptime(self) -> None:
        """A reboot shorter than ten minutes is invisible: the beat still covers it.

        The machine is ready for ten hours and beats every five minutes; a
        90 s reboot falls between two beats, so the last beat's ten-minute
        window covers it.  There is no owner-wait bucket and no incident.
        """
        start = NOW - 10 * HOUR
        reboot = NOW - 4 * HOUR
        self.fixture.tenant("odl-rb0011", activated_at=start)
        self.fixture.beats_between("odl-rb0011", start=start, end=reboot - BEAT)
        self.fixture.beats_between("odl-rb0011", start=reboot + 90, end=NOW)

        totals = self.collect()

        self.assertEqual(totals.accountable_seconds, 10 * HOUR)
        self.assertEqual(totals.down_seconds, 0)
        self.assertEqual(totals.uptime_percent, 100.0)
        self.assertEqual(totals.incidents, ())

        body, _ = self.page()
        self.assertIn("100.0%", body)
        self.assertNotIn("Waiting for its owner", body)
        self.assertNotIn("owner-waiting", body)
        self.assertIn("No incidents in the last 30 days.", body)

    def test_a_legacy_park_is_neither_up_nor_down_and_has_no_bucket(self) -> None:
        """Legacy waiting_for_owner time is history, not a category.

        Twelve hours ready, then the old initramfs park for six.  The park is
        outside in-service time (neither up nor down), the page has no
        owner-wait row, and the legacy ledger row is not an incident.
        """
        start = NOW - 12 * HOUR
        parked = NOW - 6 * HOUR
        self.fixture.tenant("odl-cc22dd", activated_at=start)
        self.fixture.beats_between("odl-cc22dd", start=start, end=parked - STOP)
        self.fixture.state.heartbeat("odl-cc22dd", at=parked - STOP)
        self.fixture.park("odl-cc22dd", at=parked)
        self.fixture.alert("odl-cc22dd", "waiting_for_owner", at=parked)

        totals = self.collect()

        self.assertEqual(totals.accountable_seconds, 6 * HOUR)
        self.assertEqual(totals.up_seconds, 6 * HOUR)
        self.assertEqual(totals.down_seconds, 0)
        self.assertEqual(totals.uptime_percent, 100.0)
        self.assertEqual(totals.incidents, ())

        body, _ = self.page()
        self.assertNotIn("Waiting for its owner", body)
        self.assertNotIn("360 min", body)
        self.assertNotIn("owner-waiting", body)
        self.assertIn("0 min", body)

    def test_only_the_last_five_downtime_incidents_are_kept(self) -> None:
        """Seven incidents, newest first, five shown; disk/spend are not incidents."""
        for index in range(7):
            slug = f"odl-inc{index:02d}"
            self.fixture.alert(
                slug,
                "down",
                at=NOW - (index + 1) * HOUR,
                resolved_at=NOW - (index + 1) * HOUR + 20 * 60,
            )
        # A legacy waiting_for_owner row is history of the retired park, not an
        # incident of the sealed boot; it is not shown and takes no slot.
        self.fixture.alert(
            "odl-legacy",
            "waiting_for_owner",
            at=NOW - 30 * 60,
            resolved_at=NOW,
        )
        # A disk alert is a condition on a live machine, not an incident.
        self.fixture.alert("odl-disk01", "disk", at=NOW - 30 * 60)
        # An incident that ended before the window is not one of the last five.
        self.fixture.alert(
            "odl-old001",
            "down",
            at=NOW - 40 * DAY,
            resolved_at=NOW - 35 * DAY,
        )

        totals = self.collect()

        self.assertEqual(len(totals.incidents), 5)
        self.assertEqual(
            [incident.start for incident in totals.incidents],
            sorted((incident.start for incident in totals.incidents), reverse=True),
        )
        self.assertEqual(totals.incidents[0].start, NOW - HOUR)
        self.assertEqual(totals.incidents[0].kind, "down")
        self.assertTrue(all(incident.kind == "down" for incident in totals.incidents))
        self.assertEqual(totals.incidents[-1].start, NOW - 5 * HOUR)

    def test_an_open_incident_is_shown_as_ongoing(self) -> None:
        self.fixture.alert("odl-open01", "down", at=NOW - 2 * HOUR)

        totals = self.collect()

        self.assertEqual(len(totals.incidents), 1)
        self.assertIsNone(totals.incidents[0].end)

    # ------------------------------------------------------------------- page

    def page(self, fixture: Fixture | None = None) -> tuple[str, status_page.Totals]:
        source = fixture or self.fixture
        return status_page.build(source.db, now=NOW, template_path=PAGE_TEMPLATE)

    def test_page_shows_the_facts_the_card_asks_for(self) -> None:
        """30-day uptime, downtime, in-service time and the last five incidents."""
        start = NOW - 10 * HOUR
        gap_start = NOW - 4 * HOUR
        gap_end = NOW - 3 * HOUR
        self.fixture.tenant("odl-aa11bb", activated_at=start)
        self.fixture.beats_between("odl-aa11bb", start=start, end=gap_start)
        self.fixture.beats_between("odl-aa11bb", start=gap_end, end=NOW)
        self.fixture.alert("odl-aa11bb", "down", at=gap_start + STOP, resolved_at=gap_end)
        for index in range(6):
            self.fixture.alert(
                f"odl-past{index:02d}",
                "down",
                at=NOW - (index + 5) * HOUR,
                resolved_at=NOW - (index + 5) * HOUR + 10 * 60,
            )

        body, totals = self.page()

        self.assertIn("Uptime, last 30 days", body)
        self.assertIn("91.7%", body)
        self.assertIn("Downtime", body)
        self.assertIn("In-service time", body)
        self.assertNotIn("Waiting for its owner", body)
        self.assertNotIn("{{WAITING}}", body)
        self.assertIn("The last five incidents", body)
        # Six past incidents plus the fresh one: only five rows make the page.
        self.assertEqual(body.count('<td class="when">'), 5)
        self.assertIn("50 min", body)
        self.assertIn("Uptime is published on the status page.", body)

    def test_the_page_and_its_meta_copy_carry_the_two_deck_rows_verbatim(self) -> None:
        """The two rows that used to say 'waiting for owner' are now the deck's.

        The reliability row and the status-page row are read from
        `docs/CLAIMS.md` itself, so a page that drifts from the deck fails
        here, not in review.
        """
        deck = (ROOT / "docs" / "CLAIMS.md").read_text(encoding="utf-8")
        rows = (
            "Runs around the clock. Uptime is published on the status page.",
            "The status page counts uptime and names no tenant.",
        )
        for row in rows:
            self.assertIn(row, deck, "the deck lost a row the page publishes")
        template = PAGE_TEMPLATE.read_text(encoding="utf-8")
        meta = re.search(r'<meta name="description" content="([^"]+)"', template)
        self.assertIsNotNone(meta, "the template has no meta description")
        assert meta is not None
        for row in rows:
            self.assertIn(row, template)
            self.assertIn(row, meta.group(1))
        body, _ = self.page()
        for row in rows:
            self.assertIn(row, body)
        self.assertNotIn("owner-waiting", body)
        self.assertNotIn("waiting-for-owner", body)
        self.assertNotIn("Waiting for its owner", body)
        self.assertNotIn("{{WAITING}}", body)

    def test_page_numbers_are_computed_from_the_history_not_typed(self) -> None:
        """The same template, two histories, two different numbers."""
        start = NOW - 10 * HOUR
        gap_start = NOW - 4 * HOUR
        gap_end = NOW - 3 * HOUR
        self.fixture.tenant("odl-aa11bb", activated_at=start)
        self.fixture.beats_between("odl-aa11bb", start=start, end=gap_start)
        self.fixture.beats_between("odl-aa11bb", start=gap_end, end=NOW)

        other = Fixture()
        self.addCleanup(other.close)
        quiet_start = NOW - 10 * HOUR
        other.tenant("odl-ee33ff", activated_at=quiet_start)
        other.beats_between("odl-ee33ff", start=quiet_start, end=NOW)

        noisy, _ = self.page()
        clean, _ = self.page(other)

        self.assertIn("91.7%", noisy)
        self.assertIn("100.0%", clean)
        self.assertIn("0 min", clean)
        self.assertNotIn("91.7%", clean)

    def test_no_tenant_slug_or_email_appears_on_the_page(self) -> None:
        start = NOW - 5 * HOUR
        self.fixture.tenant("odl-sekr1t", activated_at=start, owner="ada@example.com")
        self.fixture.beats_between("odl-sekr1t", start=start, end=NOW - HOUR)
        self.fixture.alert(
            "odl-sekr1t",
            "down",
            at=NOW - 2 * HOUR,
            tenant="ada@example.com",
        )

        body, _ = self.page()

        self.assertNotIn("odl-sekr1t", body)
        self.assertNotIn("ada@example.com", body)
        self.assertNotRegex(body, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

    def test_page_is_static_self_contained_and_javascript_free(self) -> None:
        body, _ = self.page()

        self.assertNotIn("<script", body.lower())
        self.assertNotIn("javascript:", body.lower())
        self.assertNotRegex(body.lower(), r"(?:src|href)=[\"']https?://")

    # --------------------------------------------------------- the stylesheet

    def sheet(self) -> list[tuple[str, str, dict[str, str]]]:
        return _rules(_stylesheet(PAGE_TEMPLATE))

    def test_incident_headers_stay_in_the_accessibility_tree_on_a_phone(self) -> None:
        """At ≤560 the header row is clipped, never `display:none`.

        A screen reader must hear Started / What happened / Duration before the
        five rows; hiding the row would drop all three column headers.
        """
        rules = self.sheet()
        header = _rule(rules, ".incidents thead", media=PHONE)
        self.assertEqual(header.get("position"), "absolute")
        self.assertEqual(header.get("clip"), "rect(0 0 0 0)")
        self.assertEqual(header.get("width"), "1px")
        self.assertNotIn("display", header)
        for media, selector, declarations in rules:
            if "thead" in selector:
                self.assertNotEqual(
                    declarations.get("display"),
                    "none",
                    f"{selector} in {media or 'the sheet'} hides the header row",
                )
        body, _ = self.page()
        self.assertEqual(
            body.count('scope="col"'), 3, "the three column headers are in the page"
        )

    def test_every_margin_padding_and_gap_is_on_the_four_pixel_scale(self) -> None:
        """BRAND.md §Spacing and layout: 4/8/12/16/24/32/48/64; no arbitrary margin.

        The 18 px page inset at ≤560 is the one named exception.  The
        visually-hidden clip recipe keeps a 1 px box; that is not spacing.
        """
        offenders: list[str] = []
        for media, selector, declarations in self.sheet():
            if "clip" in declarations:
                continue
            for prop, value in declarations.items():
                if prop != "gap" and not prop.startswith(("margin", "padding")):
                    continue
                for token in value.split():
                    if token in ("0", "0px", "auto"):
                        continue
                    match = re.fullmatch(r"(-?\d+)px", token)
                    if match is None:
                        offenders.append(f"{selector}: {prop}: {token}")
                        continue
                    size = abs(int(match.group(1)))
                    if size == 18 and selector == "main" and media == PHONE:
                        continue
                    if size not in SCALE:
                        offenders.append(f"{selector}: {prop}: {token}")
        self.assertEqual(offenders, [], "off-scale spacing: " + ", ".join(offenders))

    def test_the_footer_link_has_the_brand_hover_underline(self) -> None:
        """BRAND.md §Component states: a link's underline thickens to 2 px."""
        rules = self.sheet()
        self.assertEqual(_rule(rules, "a").get("text-decoration-color"), "var(--ink)")
        self.assertEqual(_rule(rules, "a:hover").get("text-decoration-thickness"), "2px")

    def test_the_page_paints_the_sheets_current_paper_not_the_legacy_one(self) -> None:
        """BRAND.md §Palette: this page is on odile.fyi, so it uses --paper.

        The sheet's Legacy surfaces carve-out keeps `--paper-v0` for surfaces
        that are not published; `/status` is published now (ODL-467), so the
        token has to equal the sheet's `--paper` and `--card`.  Read from
        BRAND.md itself: a page that drifts from the sheet fails here.
        """
        sheet = (ROOT / "BRAND.md").read_text(encoding="utf-8")
        palette: dict[str, str] = {}
        for line in sheet.splitlines():
            match = re.match(r"\|\s*`--([a-z0-9-]+)`\s*\|\s*`(#[0-9A-Fa-f]{6})`\s*\|", line)
            if match:
                palette.setdefault(match.group(1), match.group(2).lower())
        root = _rule(self.sheet(), ":root")
        self.assertEqual((root.get("--paper") or "").lower(), palette["paper"])
        self.assertEqual((root.get("--card") or "").lower(), palette["card"])
        self.assertNotIn("--paper-v0", root)
        self.assertNotIn("--card-v0", root)

    def test_the_row_labels_are_the_sheets_table_label_role(self) -> None:
        """BRAND.md §Typography: table row labels are mono caps, uppercase, .12em."""
        row = _rule(self.sheet(), ".report th")
        self.assertEqual(row.get("text-transform"), "uppercase")
        self.assertEqual(row.get("letter-spacing"), ".12em")

    def test_the_lede_sets_a_balanced_measure(self) -> None:
        self.assertEqual(_rule(self.sheet(), ".lede").get("text-wrap"), "balance")

    def test_the_summary_uses_the_pages_own_name_for_in_service_time(self) -> None:
        body, _ = self.page()

        self.assertIn('<th scope="row">In-service time</th>', body)
        self.assertIn("In-service time is the time a machine was ready", body)
        self.assertNotIn("Machine-time watched", body)
        self.assertIn("from heartbeats and state changes", body)
        self.assertNotIn("transition log", body)

    def test_a_downtime_total_is_grouped_like_a_quantity_not_a_viewport(self) -> None:
        self.assertEqual(status_page.format_minutes(86400), "1,440 min")
        self.assertEqual(status_page.format_minutes(6600), "110 min")
        self.assertEqual(status_page.format_minutes(59), "under 1 min")
        self.assertEqual(status_page.format_minutes(0), "0 min")

    def test_empty_history_renders_a_dash_not_a_false_perfect_month(self) -> None:
        body, totals = self.page()

        self.assertEqual(totals.tenants, 0)
        self.assertIsNone(totals.uptime_percent)
        self.assertIn("—", body)
        self.assertIn("No machine has been in service in the last 30 days.", body)
        self.assertIn("No incidents in the last 30 days.", body)

    def test_render_refuses_a_template_that_does_not_fit_the_data(self) -> None:
        totals = self.collect()
        with self.assertRaises(status_page.StatusError) as caught:
            status_page.render("{{UPTIME}} {{NOPE}}", totals)
        self.assertIn("NOPE", str(caught.exception))
        with self.assertRaises(status_page.StatusError):
            status_page.render("{{UPTIME}}", totals)

    def test_a_tenant_that_started_mid_window_is_not_counted_before_it_existed(self) -> None:
        """Machine-time is summed per tenant, never against the wall clock."""
        self.fixture.tenant("odl-long01", activated_at=NOW - 10 * HOUR)
        self.fixture.beats_between("odl-long01", start=NOW - 10 * HOUR, end=NOW)
        self.fixture.tenant("odl-new001", activated_at=NOW - 2 * HOUR)
        self.fixture.beats_between("odl-new001", start=NOW - 2 * HOUR, end=NOW)

        totals = self.collect()

        self.assertEqual(totals.tenants, 2)
        self.assertEqual(totals.accountable_seconds, 12 * HOUR)
        self.assertEqual(totals.uptime_percent, 100.0)

    def test_suspended_time_is_neither_uptime_nor_downtime(self) -> None:
        """A suspended machine is out of service; it does not drag the ratio."""
        self.fixture.tenant("odl-susp01", activated_at=NOW - 9 * HOUR)
        self.fixture.beats_between("odl-susp01", start=NOW - 9 * HOUR, end=NOW - 5 * HOUR)
        self.fixture.clock.now = NOW - 5 * HOUR
        self.fixture.state.suspend("odl-susp01", reason="billing")
        self.fixture.clock.now = NOW - 2 * HOUR
        self.fixture.state.resume("odl-susp01")
        self.fixture.beats_between("odl-susp01", start=NOW - 2 * HOUR, end=NOW)

        totals = self.collect()

        self.assertEqual(totals.accountable_seconds, 6 * HOUR)
        self.assertEqual(totals.down_seconds, 0)
        self.assertEqual(totals.uptime_percent, 100.0)

    def test_a_percentage_with_any_downtime_never_reads_as_perfect(self) -> None:
        self.assertEqual(status_page.format_percent(100.0), "100.0%")
        self.assertEqual(status_page.format_percent(99.9999), "99.9%")
        self.assertEqual(status_page.format_percent(99.94), "99.9%")
        self.assertEqual(status_page.format_percent(91.666), "91.7%")
        self.assertEqual(status_page.format_percent(None), "—")

    # -------------------------------------------------------------------- CLI

    def test_cli_writes_the_page_and_is_idempotent(self) -> None:
        start = NOW - 3 * HOUR
        self.fixture.tenant("odl-cli001", activated_at=start)
        self.fixture.beats_between("odl-cli001", start=start, end=NOW)
        out = self.fixture.directory / "status.html"

        with contextlib.redirect_stdout(io.StringIO()):
            first = status_page.main(
                ["--db", str(self.fixture.db), "--now", str(NOW), "--out", str(out)]
            )
        self.assertEqual(first, 0)
        body = out.read_text(encoding="utf-8")
        self.assertIn("100.0%", body)
        with contextlib.redirect_stdout(io.StringIO()):
            second = status_page.main(
                ["--db", str(self.fixture.db), "--now", str(NOW), "--out", str(out)]
            )
        self.assertEqual(second, 0)
        self.assertEqual(out.read_text(encoding="utf-8"), body)

    def test_cli_stdout_is_the_dry_run_and_writes_nothing(self) -> None:
        start = NOW - 2 * HOUR
        self.fixture.tenant("odl-cli002", activated_at=start)
        self.fixture.beats_between("odl-cli002", start=start, end=NOW)
        out = self.fixture.directory / "dry-run.html"
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = status_page.main(
                ["--db", str(self.fixture.db), "--now", str(NOW), "--out", str(out), "--stdout"]
            )
        self.assertEqual(code, 0)
        self.assertIn("Uptime, last 30 days", captured.getvalue())
        self.assertFalse(out.exists())

    def test_cli_json_prints_computed_numbers_and_their_queries(self) -> None:
        start = NOW - 4 * HOUR
        self.fixture.tenant("odl-json01", activated_at=start, owner="ada@example.com")
        self.fixture.beats_between("odl-json01", start=start, end=NOW - HOUR)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = status_page.main(["--db", str(self.fixture.db), "--now", str(NOW), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(captured.getvalue())
        self.assertEqual(payload["totals"]["tenants"], 1)
        self.assertEqual(payload["totals"]["downtime_seconds"], HOUR - STOP)
        self.assertIn("state_segments", payload["queries"])
        self.assertIn("heartbeats", payload["queries"])
        self.assertGreater(payload["inputs"]["heartbeats"], 0)
        self.assertGreaterEqual(payload["inputs"]["state_events"], 6)
        self.assertNotIn("odl-json01", captured.getvalue())
        self.assertNotIn("ada@example.com", captured.getvalue())

    def test_cli_fails_loudly_on_a_database_it_cannot_read(self) -> None:
        missing = self.fixture.directory / "absent.db"
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            code = status_page.main(["--db", str(missing), "--json"])
        self.assertEqual(code, 2)
        self.assertIn("status:", captured.getvalue())
        self.assertFalse(missing.exists(), "a read-only report must not create a database")


def main() -> int:
    unittest.main(verbosity=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
