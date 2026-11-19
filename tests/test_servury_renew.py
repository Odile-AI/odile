"""The renewal tool buys the partner price and refuses to guess.

The failure this pins is a silent one: the panel's auto-renew charges list price
($9.03 for 7 days on HQ, measured 19 November), so a box left on it pays 5% more
every term. The tool must renew only inside its window, only when the balance
covers it, never above the per-charge cap, and never on a box whose auto-renew
is still on (that would stack days and pull cash forward).
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import servury_renew as renew  # noqa: E402


class PriceTests(unittest.TestCase):
    def test_the_partner_price_is_the_documented_double_rounding(self) -> None:
        # retail/31*days rounded, then minus 5%, rounded again.
        self.assertEqual(renew.term_price(9.99, 31, 5), Decimal("9.49"))
        self.assertEqual(renew.term_price(9.99, 7, 5), Decimal("2.15"))
        self.assertEqual(renew.term_price(39.99, 7, 5), Decimal("8.58"))

    def test_no_discount_is_the_list_term(self) -> None:
        self.assertEqual(renew.term_price(9.99, 7, 0), Decimal("2.26"))
        self.assertEqual(renew.term_price(39.99, 7, 0), Decimal("9.03"))

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

    def run_tool(self, server, *, balance=10.0, auto_renew=False, days_left=3, apply=True,
                 extra: list[str] | None = None) -> tuple[int, str, list[tuple]]:
        calls: list[tuple] = []
        expires = int(time.time() + days_left * renew.DAY)

        def fake_call(action, key, *, params=None, server_id=None, timeout=40.0):
            calls.append((action, server_id, params))
            if action == "account":
                return {"balance": balance, "discount_percent": 5}
            if action == "servers":
                return {"servers": [{"id": server, "hostname": "h", "expiration_date": expires}]}
            if action == "server":
                return {"server": {"id": server, "hostname": "h", "expiration_date": expires,
                                   "rate_monthly": 9.99, "auto_renew": auto_renew}}
            if action == "renew":
                return {"amount_charged": "2.15", "new_expiration": expires + 7 * renew.DAY}
            raise AssertionError(action)

        argv = ["--key-file", f"dev={self.key}"] + (["--apply"] if apply else []) + (extra or [])
        out = io.StringIO()
        with mock.patch.object(renew, "call", fake_call), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = renew.main(argv)
        return code, out.getvalue(), calls

    def test_a_box_inside_the_window_is_renewed_for_seven_days(self) -> None:
        code, out, calls = self.run_tool("a1b2c3d4")
        self.assertEqual(code, 0)
        self.assertIn("renewed a1b2c3d4 7d $2.15", out)
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
        code, out, calls = self.run_tool("a1b2c3d4", extra=["--max-charge", "2.10"])
        self.assertEqual(code, 3)
        self.assertIn("exceeds --max-charge", out)
        self.assertNotIn("renew", [c[0] for c in calls])

    def test_a_dry_run_charges_nothing_and_says_so(self) -> None:
        code, out, calls = self.run_tool("a1b2c3d4", apply=False)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertNotIn("renew", [c[0] for c in calls])

    def test_the_status_file_records_the_run(self) -> None:
        self.run_tool("a1b2c3d4")
        import json
        status = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertTrue(status["applied"])
        self.assertEqual(status["accounts"][0]["label"], "dev")
        self.assertEqual(status["actions"][0]["server"], "a1b2c3d4")


if __name__ == "__main__":
    unittest.main()
