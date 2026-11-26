"""The renewal tool buys the partner price and refuses to guess.

The failure this pins is a silent one: the panel's auto-renew charges list price
($9.03 for 7 days on HQ, measured 19 November), so a box left on it pays 5% more
every term. The tool must renew only inside its window, only when the balance
covers it, never above the per-charge cap, and never on a box whose auto-renew
is still on (that would stack days and pull cash forward).

The price it guards is the vendor's, measured 2026-11-24: a term under 31 days
carries a 15% short-term fee, terms of 90/180/365 days take 5/10/15% off, and
the account's partner discount applies last. The old linear quote under-read a
7-day renewal by 15%, so the balance and cap checks admitted a charge they were
meant to refuse.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import servury_renew as renew  # noqa: E402


class PriceTests(unittest.TestCase):
    """The vendor's order of operations, as measured on 2026-11-24.

    `retail = rate/31 * days` rounded to the cent; a 15% fee under 31 days, or
    the highest term discount reached (5% at 90 days, 10% at 180, 15% at 365);
    then the partner discount, rounded again. Rounding happens at every step.
    """

    def test_a_short_term_carries_the_fee_before_the_partner_discount(self) -> None:
        # The dev box's real 7-day renewal charged $2.47 (invoice 4330d310);
        # the linear model quoted $2.15.
        self.assertEqual(renew.term_price(9.99, 7, 5), Decimal("2.47"))

    def test_a_month_term_has_neither_fee_nor_discount(self) -> None:
        self.assertEqual(renew.term_price(9.99, 31, 5), Decimal("9.49"))

    def test_a_year_term_takes_the_discount_before_the_partner_discount(self) -> None:
        # Both are the API's own quotes on 2026-11-24, refused on balance so
        # nothing was charged: the dev box's 365-day and HQ's.
        self.assertEqual(renew.term_price(9.99, 365, 5), Decimal("94.98"))
        self.assertEqual(renew.term_price(39.99, 365, 5), Decimal("380.21"))

    def test_the_short_term_fee_runs_to_thirty_days_and_stops_at_thirty_one(self) -> None:
        # The boundary is a cliff, not a taper: 30 days still pays the 15% fee,
        # 31 does not. Without the 30-day side a fee test one day short of the
        # vendor's threshold would stay green.
        self.assertEqual(renew.term_price(9.99, 30, 5), Decimal("10.56"))
        self.assertEqual(renew.term_price(9.99, 31, 5), Decimal("9.49"))

    def test_the_highest_term_discount_reached_applies(self) -> None:
        # One discount, not a stack: nothing at 89 days, 5% from 90, 10% from
        # 180, 15% from 365.
        self.assertEqual(renew.term_price(9.99, 89, 5), Decimal("27.25"))
        self.assertEqual(renew.term_price(9.99, 90, 5), Decimal("26.17"))
        self.assertEqual(renew.term_price(9.99, 179, 5), Decimal("52.06"))
        self.assertEqual(renew.term_price(9.99, 180, 5), Decimal("49.60"))
        self.assertEqual(renew.term_price(9.99, 364, 5), Decimal("100.29"))
        self.assertEqual(renew.term_price(9.99, 365, 5), Decimal("94.98"))

    def test_no_partner_discount_leaves_the_vendor_price(self) -> None:
        self.assertEqual(renew.term_price(9.99, 7, 0), Decimal("2.60"))
        self.assertEqual(renew.term_price(39.99, 7, 0), Decimal("10.38"))
        self.assertEqual(renew.term_price(9.99, 31, 0), Decimal("9.99"))

    def test_days_left_is_signed_and_exact(self) -> None:
        at = renew.now()
        self.assertAlmostEqual(renew.days_left(at.timestamp() + 3 * renew.DAY, at), 3.0, places=3)
        self.assertAlmostEqual(renew.days_left(at.timestamp() - renew.DAY, at), -1.0, places=3)


class RunTests(unittest.TestCase):
    """main() against a fake API, with every file it writes kept in a temp dir."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = pathlib.Path(self.dir.name)
        self.key = base / "key"
        self.key.write_text("k" * 64 + "\n", encoding="utf-8")
        self.audit, self.status, self.lock = base / "renew.log", base / "renew.json", base / "renew.lock"
        for name, value in (("AUDIT", self.audit), ("STATUS", self.status), ("LOCK", self.lock)):
            patcher = mock.patch.object(renew, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The tenant DB the tool reads: only the columns it maps, shaped as app.py
        # writes them (accounts.server_id and accounts.trial_ends_at).
        self.trial_db = base / "trial.db"
        with sqlite3.connect(self.trial_db) as db:
            db.execute("CREATE TABLE accounts (server_id TEXT, trial_ends_at TEXT NOT NULL)")
        # The run-off list: empty unless a test writes one, so no test depends on
        # the committed file's contents.
        self.run_off = base / "run-off.txt"
        self.run_off.write_text("# no pinned ids\n", encoding="utf-8")

    def add_account(self, server_id: str, trial_ends_at: str) -> None:
        with sqlite3.connect(self.trial_db) as db:
            db.execute("INSERT INTO accounts (server_id, trial_ends_at) VALUES (?, ?)",
                       (server_id, trial_ends_at))

    def stamp(self, at: datetime.datetime, days: float) -> str:
        return (at + datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def run_tool(self, server, *, balance=10.0, auto_renew=False, days_left=3, apply=True,
                 extra: list[str] | None = None, at: datetime.datetime | None = None,
                 trial_flag: bool = True, plan_id: int = 109,
                 others: list[tuple[str, int]] | None = None,
                 run_off_text: str | None = None,
                 run_off_file: pathlib.Path | None = None) -> tuple[int, str, list[tuple]]:
        calls: list[tuple] = []
        at = at or renew.now()
        expires = int(at.timestamp() + days_left * renew.DAY)
        plans = {server: plan_id, **dict(others or [])}
        if run_off_text is not None:
            self.run_off.write_text(run_off_text, encoding="utf-8")

        def brief(server_id: str) -> dict:
            plan = plans.get(server_id, plan_id)
            return {"id": server_id, "hostname": "h", "expiration_date": expires,
                    "plan_id": plan,
                    "plan": "Blackbox Core" if plan == 108 else "Blackbox Max"}

        def fake_call(action, key, *, params=None, server_id=None, timeout=40.0):
            calls.append((action, server_id, params))
            if action == "account":
                return {"balance": balance, "discount_percent": 5}
            if action == "servers":
                return {"servers": [brief(server)] + [brief(s) for s, _ in (others or [])]}
            if action == "server":
                assert server_id is not None
                return {"server": {**brief(server_id), "rate_monthly": 9.99, "auto_renew": auto_renew}}
            if action == "renew":
                return {"amount_charged": "2.47", "new_expiration": expires + 7 * renew.DAY}
            raise AssertionError(action)

        argv = ["--key-file", f"dev={self.key}"] \
            + (["--trial-db", str(self.trial_db)] if trial_flag else []) \
            + ["--run-off-file", str(run_off_file or self.run_off)] \
            + (["--apply"] if apply else []) + (extra or [])
        out = io.StringIO()
        with mock.patch.object(renew, "call", fake_call), \
                mock.patch.object(renew, "now", lambda: at), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = renew.main(argv)
        return code, out.getvalue(), calls

    def test_a_box_inside_the_window_is_renewed_for_seven_days(self) -> None:
        code, out, calls = self.run_tool("a1b2c3d4")
        self.assertEqual(code, 0)
        self.assertIn("renewed a1b2c3d4 7d $2.47", out)
        self.assertIn(("renew", "a1b2c3d4", {"days": 7}), calls)
        self.assertIn("RENEW key=dev server=a1b2c3d4", self.audit.read_text(encoding="utf-8"))

    def test_a_box_outside_the_window_is_left_alone(self) -> None:
        code, out, calls = self.run_tool("a1b2c3d4", days_left=30)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertNotIn("renew", [c[0] for c in calls])

    def test_auto_renew_is_skipped_and_named(self) -> None:
        code, out, calls = self.run_tool("a1b2c3d4", auto_renew=True, days_left=1)
        self.assertEqual(code, 0)
        self.assertIn("auto-renews in", out)
        self.assertNotIn("renew", [c[0] for c in calls])

    def test_a_short_balance_alerts_instead_of_charging(self) -> None:
        code, out, calls = self.run_tool("a1b2c3d4", balance=2.0)
        self.assertEqual(code, 3)
        self.assertIn("ALERT dev: balance $2.0", out)
        self.assertNotIn("renew", [c[0] for c in calls])

    def test_a_charge_over_the_cap_alerts_instead_of_charging(self) -> None:
        # $2.30 admitted the old linear quote ($2.15) and refuses the real $2.47,
        # so the cap is compared against the fee-inclusive price.
        code, out, calls = self.run_tool("a1b2c3d4", extra=["--max-charge", "2.30"])
        self.assertEqual(code, 3)
        self.assertIn("$2.47 for 7d exceeds --max-charge $2.30", out)
        self.assertNotIn("renew", [c[0] for c in calls])

    def test_the_balance_check_sees_the_short_term_fee(self) -> None:
        # $2.30 covered the old linear quote and not the real $2.47.
        code, out, calls = self.run_tool("a1b2c3d4", balance=2.30)
        self.assertEqual(code, 3)
        self.assertIn("balance $2.3 < $2.47", out)
        self.assertNotIn("renew", [c[0] for c in calls])

    def test_a_dry_run_charges_nothing_and_says_so(self) -> None:
        code, out, calls = self.run_tool("a1b2c3d4", apply=False)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertNotIn("renew", [c[0] for c in calls])

    def test_the_named_dry_run_flag_is_accepted_and_charges_nothing(self) -> None:
        code, out, calls = self.run_tool("a1b2c3d4", apply=False, extra=["--dry-run"])
        self.assertEqual(code, 0)
        self.assertNotIn("renew", [c[0] for c in calls])

    def test_apply_and_dry_run_together_are_a_usage_error(self) -> None:
        code, out, calls = self.run_tool("a1b2c3d4", extra=["--dry-run"])
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])

    def test_the_status_file_records_the_run(self) -> None:
        self.run_tool("a1b2c3d4")
        status = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertTrue(status["applied"])
        self.assertEqual(status["accounts"][0]["label"], "dev")
        self.assertEqual(status["actions"][0]["server"], "a1b2c3d4")

    # The trial pays for the machine. Past `trial_ends_at` nothing is renewed,
    # and both records a person reads (the status document and the audit log)
    # carry the reason, so a skipped machine is never silent.

    def test_an_ended_trial_is_skipped_and_both_records_carry_the_reason(self) -> None:
        at = renew.now()
        stamp = self.stamp(at, -1)
        self.add_account("a1b2c3d4", stamp)
        code, out, calls = self.run_tool("a1b2c3d4", at=at)
        self.assertEqual(code, 0)
        self.assertIn("skipped a1b2c3d4 — trial_ended", out)
        self.assertNotIn("renew", [c[0] for c in calls])
        status = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertEqual(status["actions"][0]["skipped"], "trial_ended")
        self.assertEqual(status["actions"][0]["trial_ends_at"], stamp)
        self.assertFalse(status["actions"][0]["applied"])
        self.assertEqual(status["alerts"], [])
        self.assertIn(f"TRIAL_ENDED key=dev server=a1b2c3d4 trial_ends_at={stamp}",
                      self.audit.read_text(encoding="utf-8"))

    def test_a_live_trial_is_renewed_and_audits_no_trial_end(self) -> None:
        at = renew.now()
        self.add_account("a1b2c3d4", self.stamp(at, 10))
        code, out, calls = self.run_tool("a1b2c3d4", at=at)
        self.assertEqual(code, 0)
        self.assertIn(("renew", "a1b2c3d4", {"days": 7}), calls)
        self.assertIn("renewed a1b2c3d4 7d $2.47", out)
        self.assertNotIn("TRIAL_ENDED", self.audit.read_text(encoding="utf-8"))

    def test_a_server_with_no_account_row_is_unchanged(self) -> None:
        at = renew.now()
        self.add_account("998405b2", self.stamp(at, -1))  # another machine's ended trial
        code, out, calls = self.run_tool("a1b2c3d4", at=at)
        self.assertEqual(code, 0)
        self.assertIn(("renew", "a1b2c3d4", {"days": 7}), calls)

    def test_a_dry_run_names_the_ended_trial_and_audits_it_without_charging(self) -> None:
        at = renew.now()
        self.add_account("a1b2c3d4", self.stamp(at, -0.5))
        code, out, calls = self.run_tool("a1b2c3d4", apply=False, at=at)
        self.assertEqual(code, 0)
        self.assertIn("skipped a1b2c3d4 — trial_ended", out)
        self.assertNotIn("renew", [c[0] for c in calls])
        self.assertIn("TRIAL_ENDED", self.audit.read_text(encoding="utf-8"))

    def test_the_trial_end_instant_itself_is_ended(self) -> None:
        at = datetime.datetime(2026, 12, 6, 11, 17, 47, tzinfo=datetime.UTC)
        self.add_account("a1b2c3d4", "2026-12-06T11:17:47Z")
        code, out, calls = self.run_tool("a1b2c3d4", at=at)
        self.assertEqual(code, 0)
        self.assertNotIn("renew", [c[0] for c in calls])
        self.assertIn("trial_ended", out)

    def test_one_second_before_the_trial_end_is_still_live(self) -> None:
        at = datetime.datetime(2026, 12, 6, 11, 17, 46, tzinfo=datetime.UTC)
        self.add_account("a1b2c3d4", "2026-12-06T11:17:47Z")
        code, out, calls = self.run_tool("a1b2c3d4", at=at)
        self.assertEqual(code, 0)
        self.assertIn(("renew", "a1b2c3d4", {"days": 7}), calls)

    def test_the_served_databases_are_the_default_source(self) -> None:
        at = renew.now()
        self.add_account("a1b2c3d4", self.stamp(at, -1))
        with mock.patch.object(renew, "TRIAL_DBS", (self.trial_db,)):
            code, out, calls = self.run_tool("a1b2c3d4", at=at, trial_flag=False)
        self.assertEqual(code, 0)
        self.assertIn("trial_ended", out)
        self.assertNotIn("renew", [c[0] for c in calls])

    def test_a_database_without_an_accounts_table_is_a_note_not_an_alert(self) -> None:
        empty = pathlib.Path(self.dir.name) / "no-accounts.db"
        with sqlite3.connect(empty):
            pass
        code, out, calls = self.run_tool("a1b2c3d4", extra=["--trial-db", str(empty)])
        self.assertEqual(code, 0)
        self.assertIn("no such table: accounts", out)
        self.assertIn(("renew", "a1b2c3d4", {"days": 7}), calls)

    def test_a_missing_database_is_a_note(self) -> None:
        absent = pathlib.Path(self.dir.name) / "absent.db"
        code, out, calls = self.run_tool("a1b2c3d4", extra=["--trial-db", str(absent)])
        self.assertEqual(code, 0)
        self.assertIn(f"trial db {absent}: absent", out)
        self.assertIn(("renew", "a1b2c3d4", {"days": 7}), calls)

    def test_an_unreadable_trial_date_is_a_note_and_the_server_is_renewed(self) -> None:
        self.add_account("a1b2c3d4", "6 December 2026")
        code, out, calls = self.run_tool("a1b2c3d4")
        self.assertEqual(code, 0)
        self.assertIn("unreadable", out)
        self.assertIn(("renew", "a1b2c3d4", {"days": 7}), calls)

    # Run-off. A box retired by policy buys no more days: no price, no balance
    # check, no cap alert, no renewal. It is not `trial_ended` — that says the
    # trial that paid for the machine has finished, this says the fleet is going.

    def test_a_listed_server_is_skipped_as_run_off_and_never_priced(self) -> None:
        at = renew.now()
        code, out, calls = self.run_tool(
            "a1b2c3d4", plan_id=109, at=at,
            run_off_text="# retired by policy\n\na1b2c3d4   # the pinned box\n")
        self.assertEqual(code, 0)
        self.assertIn("skipped a1b2c3d4 — run_off", out)
        self.assertNotIn("renew", [c[0] for c in calls])
        # Not even the per-server detail call: a run-off box is not priced.
        self.assertNotIn("server", [c[0] for c in calls])
        status = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertEqual(status["actions"][0]["skipped"], "run_off")
        self.assertEqual(status["actions"][0]["run_off_source"], "listed")
        self.assertFalse(status["actions"][0]["applied"])
        self.assertEqual(status["alerts"], [])
        expires = (at + datetime.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertIn(f"RUN_OFF key=dev server=a1b2c3d4 expires={expires}",
                      self.audit.read_text(encoding="utf-8"))

    def test_a_blackbox_core_is_run_off_by_plan_without_being_listed(self) -> None:
        # The rule, not the list: a Core the bench orders later needs no edit to
        # the committed file to be retired at its own term's end.
        code, out, calls = self.run_tool("0badc0de", plan_id=108)
        self.assertEqual(code, 0)
        self.assertIn("skipped 0badc0de — run_off", out)
        self.assertNotIn("renew", [c[0] for c in calls])
        status = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertEqual(status["actions"][0]["skipped"], "run_off")
        self.assertEqual(status["actions"][0]["run_off_source"], "plan 108")
        self.assertEqual(status["actions"][0]["plan_id"], 108)
        self.assertIn("RUN_OFF key=dev server=0badc0de expires=",
                      self.audit.read_text(encoding="utf-8"))

    def test_a_run_off_box_raises_no_balance_or_cap_alert(self) -> None:
        code, out, calls = self.run_tool(
            "a1b2c3d4", balance=0.0, extra=["--max-charge", "0.01"],
            run_off_text="a1b2c3d4\n")
        self.assertEqual(code, 0)
        status = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertEqual(status["alerts"], [])
        self.assertNotIn("renew", [c[0] for c in calls])

    def test_a_control_server_keeps_the_old_behaviour_beside_a_run_off_box(self) -> None:
        code, out, calls = self.run_tool(
            "a1b2c3d4", others=[("c0ffee11", 109)], run_off_text="a1b2c3d4\n")
        self.assertEqual(code, 0)
        self.assertIn("skipped a1b2c3d4 — run_off", out)
        self.assertIn("renewed c0ffee11 7d $2.47", out)
        self.assertEqual([c[1] for c in calls if c[0] == "renew"], ["c0ffee11"])

    def test_run_off_beats_an_ended_trial_and_carries_no_trial_reason(self) -> None:
        at = renew.now()
        self.add_account("a1b2c3d4", self.stamp(at, -1))
        code, out, calls = self.run_tool("a1b2c3d4", at=at, run_off_text="a1b2c3d4\n")
        self.assertEqual(code, 0)
        self.assertNotIn("renew", [c[0] for c in calls])
        status = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertEqual(status["actions"][0]["skipped"], "run_off")
        self.assertNotIn("trial_ends_at", status["actions"][0])
        log = self.audit.read_text(encoding="utf-8")
        self.assertIn("RUN_OFF", log)
        self.assertNotIn("TRIAL_ENDED", log)

    def test_a_missing_run_off_list_is_an_empty_list(self) -> None:
        absent = pathlib.Path(self.dir.name) / "absent-run-off.txt"
        code, out, calls = self.run_tool("a1b2c3d4", run_off_file=absent)
        self.assertEqual(code, 0)
        self.assertIn(f"run-off list {absent}: absent", out)
        self.assertIn(("renew", "a1b2c3d4", {"days": 7}), calls)

    def test_a_malformed_run_off_line_is_named_not_obeyed(self) -> None:
        code, out, calls = self.run_tool("a1b2c3d4", run_off_text="a1b2c3d4-extra\n")
        self.assertEqual(code, 0)
        self.assertIn("not an 8-character server id", out)
        self.assertIn(("renew", "a1b2c3d4", {"days": 7}), calls)


class RunOffFileTests(unittest.TestCase):
    """The committed list is the data the daily job reads: pin what it names."""

    def test_the_committed_list_names_exactly_the_three_blackbox_cores(self) -> None:
        ids, notes = renew.read_run_off(ROOT / "deploy" / "servury" / "run-off.txt")
        self.assertEqual(notes, [])
        self.assertEqual(ids, {"998405b2", "4b0f450a", "168020b4"})


if __name__ == "__main__":
    unittest.main()
