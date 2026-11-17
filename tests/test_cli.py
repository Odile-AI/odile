#!/usr/bin/python3
"""Tests for `odilectl`'s command table — every module in it is dispatchable.

ODL-218 follow-up: `odilectl.dns` followed the command-module protocol but was
missing from `cli.MODULES`, so `odilectl dns create …` exited 2
(`invalid choice: 'dns'`) and the offboard runbook had to spell
`python3 -m odilectl.dns`. These tests drive the subcommand through `cli.main`
with a recorded Cloudflare transport (no socket, no live API) and prove the
module's own entry point still parses the same flags.

    /usr/bin/python3 -m unittest tests.test_cli -v
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odilectl import cli, dns  # noqa: E402

ZONE = "odile.fyi"
ZONE_ID = "fixture-zone-id-not-real"
TOKEN = "fixture-dns-token-not-a-secret"
SLUG = "odl-7f3a2c"
NAME = f"{SLUG}.{ZONE}"
IP = "203.0.113.10"  # RFC 5737 TEST-NET-3: no live host is named in a test


# --------------------------------------------------------------- recorded fixtures


def api_ok(result):
    return {"success": True, "result": result, "errors": []}


def record_row(**overrides):
    row = {
        "id": "rec123",
        "name": NAME,
        "type": "A",
        "content": IP,
        "proxied": False,
        "ttl": dns.PROVISION_TTL,
    }
    row.update(overrides)
    return row


class RecordedCloudflare:
    """Replays recorded Cloudflare answers in order; a missing route is a bug."""

    def __init__(self, routes):
        self.routes = {key: list(value) for key, value in routes.items()}
        self.calls = []

    def __call__(self, method, url, headers, body, timeout=None):
        parts = urllib.parse.urlsplit(url)
        path = parts.path
        if path.startswith("/client/v4"):
            path = path[len("/client/v4"):]
        self.calls.append(
            {
                "method": method,
                "path": path,
                "headers": dict(headers),
                "body": json.loads(body) if body else None,
            }
        )
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"no recorded response for {method} {path}")
        status, payload = queue.pop(0)
        return status, json.dumps(payload).encode("utf-8")

    def writes(self):
        return [call for call in self.calls if call["method"] in ("POST", "PATCH", "DELETE")]


# ------------------------------------------------------------------ the table


def dns_subparser():
    """The `dns` child parser `cli.build_parser()` builds from the module."""
    parser = cli.build_parser()
    subparsers = next(action for action in parser._actions if action.dest == "command")
    return subparsers.choices[dns.NAME]


def option_strings(parser):
    return {option for action in parser._actions for option in action.option_strings}


def dns_actions(parser):
    """The actions `dns.add_arguments` owns, by destination.

    The two entry points each build these actions themselves, so a default
    written on one and not the other would be silent drift. Comparing the
    actions (not just the option strings) is what makes the parity test catch
    it.
    """
    wanted = {"verb", "slug", "ip", "ttl", "zone", "zone_id", "token_file"}
    return {action.dest: action for action in parser._actions if action.dest in wanted}


class CommandTableTest(unittest.TestCase):
    def test_dns_is_registered_in_the_command_table(self):
        self.assertIn(dns, cli.MODULES)
        self.assertIs(cli.BY_NAME[dns.NAME], dns)

    def test_the_top_level_help_lists_dns_with_its_example(self):
        result = subprocess.run(
            [sys.executable, "-m", "odilectl", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"  {dns.NAME} ", result.stdout)
        self.assertIn(dns.EXAMPLE, result.stdout)

    def test_the_dns_subcommand_help_lists_the_verbs(self):
        result = subprocess.run(
            [sys.executable, "-m", "odilectl", "dns", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: odilectl dns", result.stdout)
        self.assertIn(dns.EXAMPLE, result.stdout)
        for verb in dns.VERBS:
            self.assertIn(verb, result.stdout)

    def test_the_module_entry_point_still_runs_for_scripts_that_call_it(self):
        result = subprocess.run(
            [sys.executable, "-m", "odilectl.dns", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: odilectl dns", result.stdout)
        for verb in dns.VERBS:
            self.assertIn(verb, result.stdout)


class EntryPointParityTest(unittest.TestCase):
    """Both entry points build their flags from `dns.add_arguments`, so they agree."""

    def test_both_entry_points_expose_the_same_options(self):
        self.assertEqual(option_strings(dns.build_parser()), option_strings(dns_subparser()))

    def test_both_entry_points_carry_the_same_defaults_and_types(self):
        standalone = dns_actions(dns.build_parser())
        through_cli = dns_actions(dns_subparser())

        self.assertEqual(set(standalone), set(through_cli))
        for dest in standalone:
            with self.subTest(dest=dest):
                self.assertEqual(standalone[dest].default, through_cli[dest].default)
                self.assertEqual(standalone[dest].type, through_cli[dest].type)
                self.assertEqual(standalone[dest].choices, through_cli[dest].choices)
                self.assertEqual(standalone[dest].metavar, through_cli[dest].metavar)

    def test_both_entry_points_parse_the_same_flags_to_the_same_values(self):
        cases = (
            ["create", SLUG, "--ip", IP, "--ttl", "120"],
            ["settle", SLUG, "--zone", ZONE, "--token-file", "/tmp/odile-token"],
            ["delete", SLUG, "--zone-id", ZONE_ID],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                standalone = dns.build_parser().parse_args(argv)
                through_cli = dns_subparser().parse_args(argv)
                for name in ("verb", "slug", "ip", "ttl", "zone", "zone_id", "token_file"):
                    self.assertEqual(getattr(standalone, name), getattr(through_cli, name))


# ------------------------------------------------------------------- dispatch


class DnsDispatchTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="odile-cli-dns-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.config_path = self.write_config()

    def write_config(self, **overrides):
        values = {"dns_token": TOKEN, "dns_zone": ZONE, "dns_zone_id": ZONE_ID}
        values.update(overrides)
        path = self.directory / "config.yaml"
        path.write_text(
            "".join(f"{key}: {value}\n" for key, value in values.items() if value is not None),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def run_cli(self, argv, routes=None):
        transport = RecordedCloudflare(routes or {})
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(dns, "_default_transport", transport):
            code = cli.main(
                [*argv, "--config", str(self.config_path)],
                stdout=out,
                stderr=err,
            )
        return code, out.getvalue(), err.getvalue(), transport

    def test_create_dry_run_reaches_dns_run_and_writes_nothing(self):
        routes = {("GET", f"/zones/{ZONE_ID}/dns_records"): [(200, api_ok([]))]}

        code, out, err, transport = self.run_cli(
            ["dns", "create", SLUG, "--ip", IP, "--dry-run"], routes
        )

        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("dns: dry run"), out)
        self.assertIn(f"name={json.dumps(NAME)}", out)
        self.assertIn("proxied", out)
        self.assertEqual(transport.writes(), [])
        self.assertEqual([call["method"] for call in transport.calls], ["GET"])

    def test_dry_run_wins_over_apply_through_the_subcommand(self):
        routes = {("GET", f"/zones/{ZONE_ID}/dns_records"): [(200, api_ok([]))]}

        code, out, err, transport = self.run_cli(
            ["dns", "create", SLUG, "--ip", IP, "--apply", "--dry-run"], routes
        )

        self.assertEqual(code, 0, err)
        self.assertIn("dns: dry run", out)
        self.assertEqual(transport.writes(), [])

    def test_create_dry_run_would_update_but_writes_nothing(self):
        """An existing record with a different address makes the plan a PATCH."""
        stale = record_row(content="203.0.113.99")
        routes = {("GET", f"/zones/{ZONE_ID}/dns_records"): [(200, api_ok([stale]))]}

        code, out, err, transport = self.run_cli(
            ["dns", "create", SLUG, "--ip", IP, "--dry-run"], routes
        )

        self.assertEqual(code, 0, err)
        self.assertIn("dns: dry run", out)
        self.assertIn("dns.updated", out)
        self.assertEqual(transport.writes(), [])
        self.assertEqual([call["method"] for call in transport.calls], ["GET"])

    def test_the_module_entry_point_runs_the_same_flags_to_the_same_plan(self):
        routes = {("GET", f"/zones/{ZONE_ID}/dns_records"): [(200, api_ok([]))]}
        transport = RecordedCloudflare(routes)
        out, err = io.StringIO(), io.StringIO()

        code = dns.main(
            ["create", SLUG, "--ip", IP, "--dry-run", "--config", str(self.config_path)],
            transport=transport,
            stdout=out,
            stderr=err,
        )

        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("dns: dry run", out.getvalue())
        self.assertEqual(transport.writes(), [])

    def test_flags_before_the_command_still_reach_dns(self):
        routes = {("GET", f"/zones/{ZONE_ID}/dns_records"): [(200, api_ok([]))]}
        transport = RecordedCloudflare(routes)
        out, err = io.StringIO(), io.StringIO()

        with mock.patch.object(dns, "_default_transport", transport):
            code = cli.main(
                [
                    "--dry-run",
                    "--config",
                    str(self.config_path),
                    "dns",
                    "create",
                    SLUG,
                    "--ip",
                    IP,
                ],
                stdout=out,
                stderr=err,
            )

        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("dns: dry run", out.getvalue())
        self.assertEqual(transport.writes(), [])

    def test_create_apply_writes_one_dns_only_record_and_exits_zero(self):
        routes = {
            ("GET", f"/zones/{ZONE_ID}/dns_records"): [(200, api_ok([]))],
            ("POST", f"/zones/{ZONE_ID}/dns_records"): [(200, api_ok(record_row()))],
        }

        code, out, err, transport = self.run_cli(
            ["dns", "create", SLUG, "--ip", IP, "--apply"], routes
        )

        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("dns: applied"), out)
        self.assertIn(NAME, out)
        writes = transport.writes()
        self.assertEqual([call["method"] for call in writes], ["POST"])
        self.assertEqual(
            writes[0]["body"],
            {"type": "A", "name": NAME, "content": IP, "proxied": False, "ttl": 120},
        )

    def test_delete_apply_removes_the_record_and_exits_zero(self):
        routes = {
            ("GET", f"/zones/{ZONE_ID}/dns_records"): [(200, api_ok([record_row()]))],
            ("DELETE", f"/zones/{ZONE_ID}/dns_records/rec123"): [
                (200, api_ok({"id": "rec123"}))
            ],
        }

        code, out, err, transport = self.run_cli(["dns", "delete", SLUG, "--apply"], routes)

        self.assertEqual(code, 0, err)
        self.assertIn(f"{NAME}: removed", out)
        self.assertEqual([call["method"] for call in transport.writes()], ["DELETE"])

    def test_a_real_run_prints_one_json_object_for_the_dns_command(self):
        routes = {("GET", f"/zones/{ZONE_ID}/dns_records"): [(200, api_ok([]))]}

        code, out, err, transport = self.run_cli(
            ["dns", "delete", SLUG, "--apply", "--json"], routes
        )

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "dns")
        self.assertEqual(payload["action"], "absent")
        self.assertEqual(transport.writes(), [])

    def test_a_usage_error_exits_two_before_any_call(self):
        code, _out, err, transport = self.run_cli(["dns", "create", SLUG])

        self.assertEqual(code, 2)
        self.assertIn("--ip ADDR is required", err)
        self.assertEqual(transport.calls, [])

    def test_an_unknown_verb_exits_two_before_any_call(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            cli.main(["dns", "frobnicate", SLUG, "--config", str(self.config_path)])

        self.assertEqual(caught.exception.code, 2)
        self.assertIn("invalid choice: 'frobnicate'", stderr.getvalue())

    def test_ip_on_settle_is_a_usage_error_exit_two(self):
        code, _out, err, transport = self.run_cli(["dns", "settle", SLUG, "--ip", IP])

        self.assertEqual(code, 2)
        self.assertIn("--ip is only valid with create", err)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
