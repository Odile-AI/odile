#!/usr/bin/python3
"""ODL-950 — the HQ half of an upgrade: the consent fact, the booking, the mail.

Hermetic: a scratch SQLite file, a generated Ed25519 pair, the real
`HeartbeatServer` on a loopback socket, a temporary outbox and a fixture
release document standing in for the one app.py serves.

    /usr/bin/python3 -m unittest tests.test_upgrade_booking -v
"""

from __future__ import annotations

import base64
import email
import email.policy
import importlib.util
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odilectl import health, state  # noqa: E402


def load_heartbeat_script():
    path = ROOT / "scripts" / "heartbeat.py"
    spec = importlib.util.spec_from_file_location("odile_heartbeat_odl950", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


heartbeat = load_heartbeat_script()

# 03:00 UTC on 30 November 2026.  The booking window is the next 03:10, 24 h 10
# min out: inside HQ's 26-hour gate and far outside the 15-minute quiesce.
NOW = int(datetime(2026, 11, 30, 3, 0, tzinfo=timezone.utc).timestamp())
WINDOW = "2026-12-01T03:10:00Z"
RELEASE = "2026.12.1"
PREVIOUS = "2026.11.23-8"
GENERATED = "2026-11-25T09:20:00Z"
DEADLINE = "2026-12-15T03:10:00Z"
REQUEST = "3f0c8ad2f0e14a8b9c6d5e4f3a2b1c0d"
REQUEST_2 = "00112233445566778899aabbccddeeff"
SLUG = "odl-7f3a2c"

# `docs/backlog.yaml` ODL-950's body, verbatim, plus the account footer
# BRAND.md §Email gives every account email.  The receipt signs because its one
# action is a reply, and `— Odile` is who the reader is writing back to
# (t_d2fc0dfd).  Only line breaks moved in the re-wrap (t_b6c9e9ca).
CARD_BODY = """Your machine will be replaced at 03:10 UTC on 1 December 2026.
It takes about 20 minutes, and then your data comes back from your backup.

What happens
- Your machine is replaced, not updated in place.
The same address keeps working.
- Afterwards you paste the recovery file you saved at your first sign-in,
then sign in.
- The agent is offline until the restore finishes.

If that window is wrong, reply to this email before
03:10 UTC on 1 December 2026.

— Odile

Private alpha · Odile pays for the machine and the inference · no price
"""

SECURITY_PARAGRAPH = (
    "This is a security update: if you do nothing, it is applied after\n"
    "03:10 UTC on 15 December 2026."
)
CARD_BODY_SECURITY = CARD_BODY.replace(
    "backup.\n\nWhat happens", f"backup.\n\n{SECURITY_PARAGRAPH}\n\nWhat happens"
)


def upgrade(**overrides) -> dict:
    value = {
        "release": RELEASE,
        "window": WINDOW,
        "origin": "click",
        "request_id": REQUEST,
    }
    value.update(overrides)
    return value


def served(release: str = RELEASE, *, upgrade_block=None, generated: str = GENERATED) -> dict:
    document = {"release": release, "generated_at": generated}
    if upgrade_block is not None:
        document["upgrade"] = upgrade_block
    return document


class Clock:
    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class BookingFixture(unittest.TestCase):
    """One scratch store, one key, one outbox, one served document."""

    slug = SLUG

    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="odile-upgrade-")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.db = Path(self.directory) / "waitlist.db"
        self.outbox = Path(self.directory) / "outbox"
        self.release_document = Path(self.directory) / "release.json"
        self.set_release(served(upgrade_block={"security_deadline": DEADLINE}))
        self.clock = Clock()
        self.state = state.StateStore.open(self.db, clock=self.clock)
        self.addCleanup(self.state.close)
        # The state store's `tenant_state` row carries the owner address; the
        # web app's `tenants` table does not exist in this fixture.
        self.state.invite(self.slug, detail={"owner_email": "ada@example.com"})
        self.beats = health.HeartbeatStore.open(
            self.db,
            clock=self.clock,
            verifier=heartbeat.verify_ed25519,
            outbox=self.outbox,
            release_document=self.release_document,
        )
        self.addCleanup(self.beats.close)
        self.keydir = Path(self.directory) / "keys"
        self.private, public = heartbeat.keygen(self.keydir)
        self.public_pem = public.read_text(encoding="utf-8")

    def set_release(self, document: dict) -> None:
        self.release_document.write_text(json.dumps(document), encoding="utf-8")

    def body(self, *, facts: dict | None = None, at: int = NOW,
             slug: str | None = None) -> bytes:
        payload = {
            "slug": slug or self.slug,
            "at": at,
            "facts": facts if facts is not None else {"upgrade": upgrade()},
            "public_key": self.public_pem,
        }
        return json.dumps(payload, sort_keys=True).encode("utf-8")

    def signature(self, body: bytes) -> str:
        return base64.b64encode(heartbeat.sign_ed25519(body, self.private)).decode()

    def record(self, body: bytes, *, at: int = NOW) -> health.Heartbeat:
        return self.beats.record(body, self.signature(body), now=at)

    def emails(self) -> list[Path]:
        directory = self.outbox / "upgrade"
        return sorted(directory.glob("*.eml")) if directory.is_dir() else []

    def email_body(self) -> str:
        files = self.emails()
        self.assertEqual(len(files), 1, files)
        message = email.message_from_bytes(
            files[0].read_bytes(), policy=email.policy.default
        )
        return message.get_content()


class ReceiverTest(BookingFixture):
    """The route the machine already uses, end to end over a real socket."""

    def setUp(self) -> None:
        super().setUp()
        self.server = heartbeat.HeartbeatServer(("127.0.0.1", 0), self.beats)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, body: bytes, signature: str | None = None) -> tuple[int, dict]:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            "/heartbeat",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-Odile-Signature": signature if signature is not None else self.signature(body),
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def test_a_signed_upgrade_beat_books_the_window_and_answers_once(self) -> None:
        status, payload = self.post(self.body())
        self.assertEqual(status, 200)
        self.assertEqual(
            set(payload), {"ok", "slug", "at", "received_at", "upgrade"}
        )
        self.assertEqual(
            payload["upgrade"],
            {"booked": True, "release": RELEASE, "window": WINDOW},
        )
        bookings = self.beats.upgrade_bookings(SLUG)
        self.assertEqual(len(bookings), 1)
        booking = bookings[0]
        self.assertEqual(booking.state, "booked")
        self.assertEqual(booking.origin, "click")
        self.assertEqual(booking.release, RELEASE)
        self.assertEqual(booking.window, WINDOW)
        self.assertEqual(booking.request_id, REQUEST)
        latest = self.beats.latest(SLUG)
        self.assertIsNotNone(latest)
        self.assertEqual(booking.heartbeat_id, latest.id)

    def test_the_queued_email_is_the_card_body_once(self) -> None:
        status, _ = self.post(self.body())
        self.assertEqual(status, 200)
        files = self.emails()
        self.assertEqual(len(files), 1, files)
        self.assertEqual(files[0].name, f"{SLUG}-{REQUEST}.eml")
        message = email.message_from_bytes(
            files[0].read_bytes(), policy=email.policy.default
        )
        self.assertEqual(message["Subject"], "Your upgrade is booked")
        self.assertEqual(message["To"], "ada@example.com")
        self.assertEqual(message["Reply-To"], "help@odile.fyi")
        self.assertEqual(message.get_content(), CARD_BODY_SECURITY)
        self.assertEqual(
            health.render_upgrade_body(WINDOW, DEADLINE), CARD_BODY_SECURITY
        )
        self.assertNotIn("{{WINDOW}}", message.get_content())
        self.assertNotIn("{{DEADLINE}}", message.get_content())

    def test_the_deadline_falls_back_to_generated_at_plus_14_days(self) -> None:
        self.set_release(served(upgrade_block={}))
        self.assertEqual(self.post(self.body())[0], 200)
        body = self.email_body()
        self.assertIn(
            "This is a security update: if you do nothing, it is applied after\n"
            "09:20 UTC on 9 December 2026.",
            body,
        )

    def test_a_document_with_no_upgrade_block_drops_the_paragraph(self) -> None:
        self.set_release(served())
        self.assertEqual(self.post(self.body())[0], 200)
        body = self.email_body()
        self.assertEqual(body, CARD_BODY)
        self.assertNotIn("This is a security update", body)

    def test_the_same_request_id_replayed_is_one_row_one_email_one_beat(self) -> None:
        body = self.body()
        first_status, first = self.post(body)
        self.assertEqual(first_status, 200)
        # Byte-identical replay: the same decision, not a second beat.
        second_status, second = self.post(body)
        self.assertEqual(second_status, 200)
        self.assertEqual(second["upgrade"], first["upgrade"])
        # A retry re-signs with a newer `at`; the booking is still the same.
        retry = self.body(at=NOW + 60)
        third_status, third = self.post(retry)
        self.assertEqual(third_status, 200)
        self.assertEqual(third["upgrade"], first["upgrade"])
        self.assertEqual(len(self.beats.upgrade_bookings(SLUG)), 1)
        self.assertEqual(self.beats.heartbeat_count(SLUG), 1)
        self.assertEqual(len(self.emails()), 1)

    def test_a_replay_answers_the_rows_current_window(self) -> None:
        # ODL-954 advances a failed gate to the next window; the machine's
        # retry must read the row, not the window it first posted.
        self.assertEqual(self.post(self.body())[0], 200)
        later = "2026-12-02T03:10:00Z"
        self.beats.connection.execute(
            "UPDATE upgrade_windows SET window = ? WHERE slug = ?", (later, SLUG)
        )
        self.beats.connection.commit()
        status, payload = self.post(self.body(at=NOW + 60))
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["upgrade"], {"booked": True, "release": RELEASE, "window": later}
        )
        self.assertEqual(len(self.emails()), 1)

    def test_a_previous_cuts_release_is_stale_and_books_nothing(self) -> None:
        status, payload = self.post(
            self.body(facts={"upgrade": upgrade(release=PREVIOUS, request_id=REQUEST_2)})
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            set(payload), {"ok", "slug", "at", "received_at", "upgrade"}
        )
        self.assertEqual(payload["upgrade"], {"booked": False, "stale": True})
        self.assertEqual(self.beats.heartbeat_count(SLUG), 1)
        self.assertEqual(self.beats.upgrade_bookings(SLUG), ())
        self.assertEqual(self.emails(), [])

    def test_a_missing_release_document_books_nothing(self) -> None:
        self.beats._release_document = Path(self.directory) / "no-release.json"
        status, payload = self.post(self.body())
        self.assertEqual(status, 200)
        self.assertEqual(payload["upgrade"], {"booked": False, "stale": True})
        self.assertEqual(self.beats.upgrade_bookings(SLUG), ())
        self.assertEqual(self.emails(), [])

    def test_a_second_booking_while_one_is_open_is_409_naming_the_window(self) -> None:
        self.assertEqual(self.post(self.body())[0], 200)
        status, payload = self.post(
            self.body(
                facts={"upgrade": upgrade(request_id=REQUEST_2)},
                at=NOW + 60,
            )
        )
        self.assertEqual(status, 409)
        self.assertFalse(payload["ok"])
        self.assertIn(WINDOW, payload["error"])
        self.assertNotIn("upgrade", payload)
        self.assertEqual(len(self.beats.upgrade_bookings(SLUG)), 1)
        self.assertEqual(self.beats.heartbeat_count(SLUG), 1)
        self.assertEqual(len(self.emails()), 1)

    def test_a_booking_after_the_open_one_is_settled_is_accepted(self) -> None:
        self.assertEqual(self.post(self.body())[0], 200)
        self.beats.connection.execute(
            "UPDATE upgrade_windows SET state = 'restored' WHERE slug = ?", (SLUG,)
        )
        self.beats.connection.commit()
        later = "2026-12-01T03:20:00Z"
        status, payload = self.post(
            self.body(
                facts={"upgrade": upgrade(request_id=REQUEST_2, window=later)},
                at=NOW + 120,
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["upgrade"]["window"], later)
        self.assertEqual(len(self.beats.upgrade_bookings(SLUG)), 2)
        self.assertEqual(len(self.emails()), 2)

    def test_a_refused_booking_carries_no_upgrade_key(self) -> None:
        status, payload = self.post(
            self.body(facts={"upgrade": upgrade(origin="cron")})
        )
        self.assertEqual(status, 400)
        self.assertEqual(set(payload), {"ok", "error"})
        self.assertFalse(payload["ok"])

    def test_the_consent_fact_is_refused_at_the_door(self) -> None:
        five_minutes = "2026-11-30T03:05:00Z"
        twenty_seven_hours = "2026-12-01T06:00:00Z"
        cases = (
            ("release", upgrade(release="next"), "facts.upgrade.release"),
            ("origin", upgrade(origin="cron"), "facts.upgrade.origin"),
            ("window-shape", upgrade(window="2026-12-01 03:10:00"), "facts.upgrade.window"),
            ("window-early", upgrade(window=five_minutes), "facts.upgrade.window"),
            ("window-late", upgrade(window=twenty_seven_hours), "facts.upgrade.window"),
            ("request-id", upgrade(request_id="abc123"), "facts.upgrade.request_id"),
        )
        for name, fact, field in cases:
            with self.subTest(name=name):
                status, payload = self.post(self.body(facts={"upgrade": fact}))
                self.assertEqual(status, 400, payload)
                self.assertIn(field, payload["error"])
        self.assertEqual(self.beats.heartbeat_count(SLUG), 0)
        self.assertEqual(self.beats.upgrade_bookings(SLUG), ())
        self.assertEqual(self.emails(), [])

    def test_a_beat_without_upgrade_is_stored_and_books_nothing(self) -> None:
        status, payload = self.post(self.body(facts={"disk": {"percent": 40}}))
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"ok", "slug", "at", "received_at"})
        self.assertEqual(self.beats.heartbeat_count(SLUG), 1)
        self.assertEqual(self.beats.upgrade_bookings(SLUG), ())
        self.assertEqual(self.emails(), [])


class StoreTest(BookingFixture):
    """The store's own rules: one open row, one receipt, one transaction."""

    def test_the_partial_unique_index_is_the_one_open_row_rule(self) -> None:
        self.record(self.body())
        index = self.beats.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index'"
            " AND name = 'upgrade_windows_open'"
        ).fetchone()
        self.assertIsNotNone(index)
        self.assertIn("WHERE state IN ('booked', 'writing', 'booted')", index["sql"])
        stamp = "2026-11-30T03:00:00Z"
        with self.assertRaises(sqlite3.IntegrityError):
            self.beats.connection.execute(
                "INSERT INTO upgrade_windows (slug, request_id, release, window,"
                " origin, state, heartbeat_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'click', 'writing', NULL, ?, ?)",
                (SLUG, REQUEST_2, RELEASE, WINDOW, stamp, stamp),
            )
        self.beats.connection.rollback()

    def test_a_failed_email_write_rolls_the_heartbeat_back_too(self) -> None:
        def full(*_args, **_kwargs):
            raise health.HeartbeatError("outbox is full")

        self.beats._queue_upgrade_email = full  # type: ignore[method-assign]
        body = self.body()
        with self.assertRaises(health.HeartbeatError):
            self.beats.record(body, self.signature(body), now=NOW)
        self.assertEqual(self.beats.heartbeat_count(SLUG), 0)
        self.assertEqual(self.beats.upgrade_bookings(SLUG), ())
        self.assertEqual(self.emails(), [])

    def test_the_template_is_the_cards_body_verbatim_plus_the_account_footer(self) -> None:
        template = (ROOT / "templates" / "emails" / "upgrade-booked.txt").read_text(
            encoding="utf-8"
        )
        expected = (
            CARD_BODY_SECURITY
            .replace("03:10 UTC on 1 December 2026", "{{WINDOW}}")
            .replace("03:10 UTC on 15 December 2026", "{{DEADLINE}}")
        )
        self.assertEqual(template, expected)
        self.assertEqual(health.UPGRADE_SUBJECT, "Your upgrade is booked")
        self.assertEqual(health.owner_window_text(WINDOW), "03:10 UTC on 1 December 2026")

    def test_the_schema_keeps_the_states_the_page_and_executor_read(self) -> None:
        columns = {
            row["name"]
            for row in self.beats.connection.execute(
                "PRAGMA table_info(upgrade_windows)"
            )
        }
        self.assertEqual(
            columns,
            {
                "id", "slug", "request_id", "release", "window", "origin",
                "state", "heartbeat_id", "created_at", "updated_at",
            },
        )


if __name__ == "__main__":
    unittest.main()
