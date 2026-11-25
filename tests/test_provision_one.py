#!/usr/bin/python3
"""The one-machine provisioner ships a Nano default, never a Plus or a Core.

`scripts/provision_one.py` is the command both rebuild runbooks run with no
`--plan` / `--days` flags (`docs/runbooks/incidents.md` §5.1,
`docs/runbooks/provision.md` §3), so its argparse defaults are what a rebuilt
tenant's machine is ordered on. Savio's 2026-11-24 direction makes that a
Blackbox Nano at the annual term (plan 138, the Canada 1 vCPU / 2 GB / 25 GB
tier; `docs/runbooks/deploy.md`, `docs/evidence/nano-baseline.md`). The tier
matters because Servury cannot downgrade a machine: a rebuild placed on a
larger plan cannot come back to a Nano without another rebuild.

Hermetic: the parser and the module's constants only. No socket, no database.

    /usr/bin/python3 -m unittest tests.test_provision_one -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "provision_one.py"


def load_provision_one():
    spec = importlib.util.spec_from_file_location("provision_one_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


provision_one = load_provision_one()


class DefaultTests(unittest.TestCase):
    """The shipped default: plan 138, 365 days."""

    def test_the_module_pins_the_nano_at_the_annual_term(self) -> None:
        self.assertEqual(provision_one.DEFAULT_PLAN, 138)
        self.assertEqual(provision_one.DEFAULT_DAYS, 365)

    def test_no_flags_is_a_nano_for_a_year(self) -> None:
        args = provision_one.parse_args([])
        self.assertEqual((args.plan, args.days), (138, 365))

    def test_explicit_flags_still_win(self) -> None:
        args = provision_one.parse_args(["--plan", "113", "--days", "31"])
        self.assertEqual((args.plan, args.days), (113, 31))

    def test_the_help_names_the_defaults_the_parser_uses(self) -> None:
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            with self.assertRaises(SystemExit) as raised:
                provision_one.parse_args(["--help"])
        self.assertEqual(raised.exception.code, 0)
        help_text = printed.getvalue()
        self.assertIn("default 138", help_text)
        self.assertIn("default 365", help_text)
        self.assertNotIn("default 113", help_text)
        self.assertNotIn("default 31)", help_text)

    def test_the_term_is_within_the_provider_limits(self) -> None:
        """The account's limits are min 7 / max 365 days; 365 is the top, not past it."""
        self.assertGreaterEqual(provision_one.DEFAULT_DAYS, 7)
        self.assertLessEqual(provision_one.DEFAULT_DAYS, 365)


if __name__ == "__main__":
    unittest.main(verbosity=2)
