#!/usr/bin/python3
"""Tests for odilectl — the CLI skeleton and the provider interface.

Hermetic: no socket, no live API, no SSH. The provider tests run against
recorded response payloads shaped from the Blackbox API documentation
(https://servury.com/docs/?api=blackbox, read 2026-11-07); ODL-201 replaces
them with a live capture from a real order. The CLI tests inject the fixture
provider through `main(provider_factory=...)`, so a dry run proves it never
built one.

    /usr/bin/python3 -m unittest tests.test_odilectl -v
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "odilectl"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odilectl import cli, config, health, offboard, providers, state  # noqa: E402
from odilectl.core import Context  # noqa: E402
from odilectl.errors import ConfigError, ProviderError, ProviderTimeout  # noqa: E402
from odilectl.providers import base, servury  # noqa: E402

# A real ed25519 public key; the private half was generated for this fixture
# and thrown away. A public key is not a secret.
KEY_TEXT = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICFh7hQkST5vRDolO3yqwCARRugZahMdc+WgR69JsoXG "
    "odile-fixture@example.com"
)
TEST_KEY = "test-reseller-key"
SERVER_ID = "abc12345"
SLUG = "odl-7f3a2c"


# --------------------------------------------------------------- recorded fixtures


def create_response(**overrides):
    data = {
        "message": "Order accepted. The machine is built in the background ...",
        "server_id": SERVER_ID,
        "hostname": SLUG,
        "status": "deploying",
        "expiration_date": 1792000000,
        "invoice_id": "7d2e9a10",
        "amount_charged": 19.33,
    }
    data.update(overrides)
    return {"success": True, "data": data}


def machine_row(status="active", **overrides):
    row = {
        "id": SERVER_ID,
        "hostname": SLUG,
        "label": "fixture",
        "plan_id": 113,
        "plan": "Blackbox Plus",
        "location": "Canada",
        "os": "debian13",
        "username": "root",
        "ipv4_address": "203.0.113.120",
        "ipv6_address": "2001:0db8:0:2a::1",
        "status": status,
        "status_reason": None,
        "powered_on": True,
        "snp_enabled": True,
        "fde_requested": True,
        "fde_enabled": True,
        "measured_boot": True,
        "expiration_date": 1792000000,
        "expired": False,
    }
    row.update(overrides)
    return row


def server_row(status="active", **overrides):
    return {"success": True, "data": {"server": machine_row(status=status, **overrides)}}


def servers_response(*rows):
    rows = rows or (machine_row(),)
    return {"success": True, "data": {"servers": list(rows), "total": len(rows)}}


def empty_servers_response():
    return {"success": True, "data": {"servers": [], "total": 0}}


def error_response(status=None, message="the provider said no"):
    return {"success": False, "error": message}


class RecordedTransport:
    """Replays recorded payloads in order; raises if a call was not recorded."""

    def __init__(self, routes):
        self.routes = {key: list(value) for key, value in routes.items()}
        self.calls = []

    def __call__(self, method, url, headers, body):
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        action = query.get("action", [None])[0]
        self.calls.append(
            {
                "method": method,
                "url": url,
                "host": parts.hostname,
                "path": parts.path,
                "action": action,
                "id": query.get("id", [None])[0],
                "headers": dict(headers),
                "body": json.loads(body) if body else None,
            }
        )
        queue = self.routes.get((method, action))
        if not queue:
            raise AssertionError(f"no recorded response for {method} action={action!r}")
        status, payload = queue.pop(0)
        return status, json.dumps(payload).encode("utf-8")


def fixture_provider(routes):
    return servury.ServuryProvider(TEST_KEY, transport=RecordedTransport(routes))


class QuietDNS:
    """Cloudflare with no A records: absent find, absent delete."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append(method)
        return 200, json.dumps({"success": True, "result": []}).encode()


# A tenant's own /verify, saying what the default state is.
GRANT_NONE = (
    b"  operator key   PASS  none (authorized_keys: 1 line(s), the measured throwaway)\n"
)
_OFFBOARD_PATCHES = []


def setUpModule():
    """Keep every destroy invocation off Cloudflare, the tenant and the clock."""
    _OFFBOARD_PATCHES.extend(
        [
            mock.patch.object(offboard, "DNS_TRANSPORT", QuietDNS()),
            mock.patch.object(offboard, "VERIFY_FETCH", lambda _url: GRANT_NONE),
            mock.patch.object(offboard, "SLEEP", lambda _seconds: None),
        ]
    )
    for patcher in _OFFBOARD_PATCHES:
        patcher.start()


def tearDownModule():
    for patcher in _OFFBOARD_PATCHES:
        patcher.stop()
    _OFFBOARD_PATCHES.clear()


def explode_provider(_config):
    raise AssertionError("a dry run must not build a provider")


class TempConfigMixin(unittest.TestCase):
    def make_config(self, **overrides) -> Path:
        directory = tempfile.mkdtemp(prefix="odilectl-test-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = Path(directory) / "config.yaml"
        values = {
            "provider": "servury",
            "reseller_key": TEST_KEY,
            "default_plan_id": "113",
            "default_days": "31",
            "state_db": str(Path(directory) / "waitlist.db"),
            "backup_repo": "sftp://backup@example.invalid/odile",
            "dns_token": "fixture-token",
            "dns_zone": "odile.fyi",
            "dns_zone_id": "fixture-zone",
        }
        values.update(overrides)
        path.write_text(
            "".join(f"{key}: {value}\n" for key, value in values.items() if value is not None),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def key_file(self) -> Path:
        directory = tempfile.mkdtemp(prefix="odilectl-key-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = Path(directory) / "odile.pub"
        path.write_text(KEY_TEXT + "\n", encoding="utf-8")
        return path

    def pin_file(self, *, release: str | None = "2026.11.1") -> Path:
        """A fixture pin: the release field is present unless `release` is None."""
        directory = tempfile.mkdtemp(prefix="odilectl-pin-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = Path(directory) / "odile-verify.pin"
        document = {"schema": 2, "measurement": "ab" * 48, "releases": {}}
        if release is not None:
            document["provision_release"] = release
            document["releases"][release] = {}
        path.write_text(json.dumps(document), encoding="utf-8")
        return path


def run_cli(argv, config_path, provider_factory=None):
    argv = list(argv)
    if "--config" not in argv:
        argv += ["--config", str(config_path)]
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(
        argv,
        provider_factory=provider_factory,
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------------ help


class HelpTest(TempConfigMixin):
    def test_help_lists_every_command_with_an_example(self):
        help_text = cli.build_parser().format_help()
        for module in cli.MODULES:
            self.assertIn(module.NAME, help_text)
            self.assertIn(module.EXAMPLE, help_text, f"{module.NAME} has no example in --help")

    def test_each_subcommand_help_carries_its_example(self):
        parser = cli.build_parser()
        subparsers = next(
            action for action in parser._actions if action.dest == "command"
        )
        choices = getattr(subparsers, "choices")
        for module in cli.MODULES:
            child = choices[module.NAME]
            text = child.format_help()
            self.assertIn(f"Example: {module.EXAMPLE}", text)

    def test_the_help_does_not_list_removed_commands(self):
        help_text = cli.build_parser().format_help().lower()
        self.assertNotIn("bootstrap", help_text)
        self.assertNotIn("key-release", help_text)

    def test_python_dash_m_help_runs(self):
        result = subprocess.run(
            [sys.executable, "-m", "odilectl", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for module in cli.MODULES:
            self.assertIn(module.NAME, result.stdout)
            self.assertIn(module.EXAMPLE, result.stdout)
        self.assertIn("usage: odilectl", result.stdout)

    def test_the_installed_name_wrapper_runs(self):
        result = subprocess.run(
            [sys.executable, str(PACKAGE / "odilectl"), "--version"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"odilectl {cli.__version__}")


# ----------------------------------------------------------------------- parsing


class ParseTest(unittest.TestCase):
    MINIMAL = {
        "invite": ["invite", "ada@example.com"],
        "provision": ["provision", SLUG, "--key", "/tmp/ada.pub"],
        "status": ["status"],
        "destroy": ["destroy", SLUG],
        "rotate-token": ["rotate-token", SLUG],
        "reconcile": ["reconcile"],
        "restore": ["restore", SLUG],
        "support": ["support", SLUG],
        "export": ["export", SLUG],
    }

    def test_every_command_parses(self):
        parser = cli.build_parser()
        for name, argv in self.MINIMAL.items():
            with self.subTest(command=name):
                args = parser.parse_args(argv)
                self.assertEqual(args.command, name)

    def test_dry_run_and_json_work_before_or_after_the_command(self):
        parser = cli.build_parser()
        cases = (
            (["--dry-run", "status"], True, False),
            (["status", "--dry-run"], True, False),
            (["--json", "status"], False, True),
            (["status", "--json"], False, True),
            (["--dry-run", "--json", "status"], True, True),
            (["status", "--json", "--dry-run"], True, True),
        )
        for argv, dry_run, wants_json in cases:
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertEqual(args.dry_run, dry_run)
                self.assertEqual(args.json, wants_json)

    def test_flags_default_off(self):
        args = cli.build_parser().parse_args(["status"])
        self.assertFalse(args.dry_run)
        self.assertFalse(args.apply)
        self.assertFalse(args.json)
        self.assertIsNone(args.config)

    def test_an_unknown_command_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(["bootstrap"])
        self.assertEqual(caught.exception.code, 2)

    def test_a_missing_argument_is_a_usage_error(self):
        for argv in (["invite"], ["provision"], ["destroy"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as caught:
                    cli.build_parser().parse_args(argv)
                self.assertEqual(caught.exception.code, 2)

    def test_mutate_needs_apply_and_dry_run_wins(self):
        unused = config.Config(Path("/unused"), {})
        self.assertFalse(Context(config=unused, apply=False).mutate)
        self.assertTrue(Context(config=unused, apply=True).mutate)
        self.assertFalse(Context(config=unused, apply=True, dry_run=True).mutate)


# ------------------------------------------------------------------------ config


class ConfigTest(TempConfigMixin):
    def test_a_flat_file_loads_values_comments_and_quotes(self):
        path = self.make_config(
            **{
                "reseller_key": "s3cr3t",
                "servury_url": "https://servury.com/api/reseller.php?x=1",
                "relay_key": "has a # inside",
            }
        )
        text = path.read_text(encoding="utf-8")
        path.write_text("# a comment\n\n" + text + 'quoted: "  spaced  "\n', encoding="utf-8")
        loaded = config.Config.load(path)
        self.assertEqual(loaded.require("reseller_key"), "s3cr3t")
        self.assertEqual(loaded.get("servury_url"), "https://servury.com/api/reseller.php?x=1")
        self.assertEqual(loaded.get("relay_key"), "has a # inside")
        self.assertEqual(loaded.get("quoted"), "  spaced  ")

    def test_repr_never_shows_a_secret(self):
        loaded = config.Config.load(self.make_config(reseller_key="do-not-print-me"))
        self.assertNotIn("do-not-print-me", repr(loaded))
        self.assertNotIn("do-not-print-me", str(loaded))

    def test_group_or_world_readable_config_is_refused(self):
        path = self.make_config()
        path.chmod(0o644)
        with self.assertRaises(ConfigError) as caught:
            config.Config.load(path)
        self.assertIn("0600", str(caught.exception))

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            config.Config.load("/nonexistent/odilectl/config.yaml")
        self.assertIn("no config at", str(caught.exception))

    def test_a_duplicate_key_is_refused(self):
        path = self.make_config()
        path.write_text(path.read_text(encoding="utf-8") + "provider: servury\n", encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            config.Config.load(path)
        self.assertIn("duplicate key", str(caught.exception))

    def test_an_indented_line_is_refused(self):
        path = self.make_config()
        path.write_text("paths:\n  state_db: /tmp/x.db\n", encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            config.Config.load(path)
        self.assertIn("flat", str(caught.exception))

    def test_a_malformed_line_is_refused(self):
        path = self.make_config()
        path.write_text("just words\n", encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            config.Config.load(path)
        self.assertIn("key: value", str(caught.exception))

    def test_require_names_the_missing_key_not_the_file_contents(self):
        path = self.make_config(dns_token=None)
        loaded = config.Config.load(path)
        with self.assertRaises(ConfigError) as caught:
            loaded.require("dns_token")
        self.assertIn("dns_token", str(caught.exception))
        self.assertNotIn(TEST_KEY, str(caught.exception))

    def test_path_value_expands_home(self):
        loaded = config.Config.load(self.make_config(state_db="~/odile/state.db"))
        self.assertEqual(loaded.path_value("state_db"), Path.home() / "odile/state.db")

    def test_a_non_utf8_config_is_a_config_error_not_a_usage_error(self):
        path = self.make_config()
        path.write_bytes(b"reseller_key: \xff\xfe\n")
        path.chmod(0o600)
        with self.assertRaises(ConfigError) as caught:
            config.Config.load(path)
        self.assertIn("UTF-8", str(caught.exception))


# ------------------------------------------------------------------ provider base


class ProviderBaseTest(unittest.TestCase):
    def test_a_valid_spec_keeps_the_full_machine_and_one_key(self):
        spec = base.CreateSpec(hostname=SLUG, plan_id=113, ssh_key="  " + KEY_TEXT + "  ")
        self.assertEqual(spec.ssh_key, KEY_TEXT)
        self.assertTrue(spec.snp and spec.fde and spec.measured)

    def test_a_bad_hostname_is_refused(self):
        for hostname in ("ab", "-" + SLUG, SLUG + "-", "a" * 33, "bad_name"):
            with self.subTest(hostname=hostname):
                with self.assertRaises(ValueError):
                    base.CreateSpec(hostname=hostname, plan_id=113, ssh_key=KEY_TEXT)

    def test_a_bad_term_is_refused(self):
        for days in (0, 6, 366):
            with self.subTest(days=days):
                with self.assertRaises(ValueError):
                    base.CreateSpec(hostname=SLUG, plan_id=113, ssh_key=KEY_TEXT, days=days)

    def test_a_password_or_truncated_key_is_refused(self):
        one_byte_ed25519 = "ssh-ed25519 " + base64.b64encode(
            struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 1) + b"\x00"
        ).decode("ascii")
        for key in (
            "hunter2",
            "ssh-ed25519",
            "ssh-ed25519 AAAAC3",
            "ssh-rsa AAAAC3NzaC1lZDI1NTE5AAAAICFh7hQkST5vRDolO3yqwCARRugZahMdc+WgR69JsoXG",
            KEY_TEXT + "\n" + KEY_TEXT,
            one_byte_ed25519,
        ):
            with self.subTest(key=key[:40]):
                with self.assertRaises(ValueError):
                    base.validate_ssh_public_key(key)

    def test_the_provider_interface_cannot_be_instantiated(self):
        with self.assertRaises(TypeError):
            base.Provider()  # type: ignore[abstract]

    def test_machine_json_is_provider_neutral(self):
        machine = base.Machine(
            server_id=SERVER_ID,
            hostname=SLUG,
            state="active",
            ipv4="203.0.113.120",
            expires_at=1792000000,
            raw_status="active",
        )
        data = machine.to_dict()
        self.assertEqual(data["server_id"], SERVER_ID)
        self.assertEqual(data["state"], "active")
        self.assertNotIn("api_key", data)


class ProviderRegistryTest(TempConfigMixin):
    """The registry resolves the one implementation without hard-coding it."""

    def test_build_returns_the_one_implementation(self):
        loaded = config.Config.load(self.make_config())
        provider = providers.build(loaded)
        self.assertIsInstance(provider, servury.ServuryProvider)
        self.assertEqual(provider.name, servury.NAME)

    def test_the_default_name_comes_from_the_implementation(self):
        loaded = config.Config.load(self.make_config(provider=None))
        self.assertEqual(providers.configured_name(loaded), servury.NAME)

    def test_a_provider_name_is_case_folded(self):
        loaded = config.Config.load(self.make_config(provider=servury.NAME.upper()))
        self.assertEqual(providers.configured_name(loaded), servury.NAME)

    def test_an_unknown_provider_is_a_config_error_naming_the_known_one(self):
        loaded = config.Config.load(self.make_config(provider="other"))
        with self.assertRaises(ConfigError) as caught:
            providers.build(loaded)
        self.assertIn(servury.NAME, str(caught.exception))


# -------------------------------------------------------------- servury provider


class ServuryProviderTest(unittest.TestCase):
    def test_create_sends_the_documented_order_and_keeps_the_vendor_status(self):
        transport = RecordedTransport({("POST", "create"): [(200, create_response())]})
        provider = servury.ServuryProvider(TEST_KEY, transport=transport)
        spec = base.CreateSpec(hostname=SLUG, plan_id=113, ssh_key=KEY_TEXT)
        machine = provider.create(spec)

        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["host"], "servury.com")
        self.assertEqual(call["path"], "/api/reseller.php")
        self.assertEqual(call["headers"]["X-Reseller-Key"], TEST_KEY)
        self.assertEqual(
            call["body"],
            {
                "plan_id": 113,
                "hostname": SLUG,
                "os": "debian13",
                "days": 31,
                "ssh_key": KEY_TEXT,
                "snp": True,
                "fde": True,
                "measured": True,
                "preorder": False,
            },
        )
        self.assertEqual(machine.server_id, SERVER_ID)
        self.assertEqual(machine.state, "pending")
        self.assertEqual(machine.raw_status, "deploying")
        self.assertEqual(machine.detail["amount_charged"], 19.33)

    def test_create_keeps_the_queue_position_of_a_paid_preorder(self):
        transport = RecordedTransport(
            {("POST", "create"): [(200, create_response(status="queued", queue_position=3))]}
        )
        provider = servury.ServuryProvider(TEST_KEY, transport=transport)
        spec = base.CreateSpec(hostname=SLUG, plan_id=113, ssh_key=KEY_TEXT, preorder=True)
        machine = provider.create(spec)
        self.assertEqual(machine.state, "pending")
        self.assertEqual(machine.raw_status, "queued")
        self.assertEqual(machine.detail["queue_position"], 3)
        self.assertTrue(transport.calls[0]["body"]["preorder"])

    def test_poll_waits_for_active_and_returns_the_address(self):
        transport = RecordedTransport(
            {
                ("GET", "server"): [
                    (200, server_row(status="deploying", ipv4_address=None)),
                    (200, server_row()),
                ]
            }
        )
        provider = servury.ServuryProvider(TEST_KEY, transport=transport)
        machine = provider.poll(SERVER_ID, timeout=5, interval=0)
        self.assertEqual(machine.state, "active")
        self.assertEqual(machine.ipv4, "203.0.113.120")
        self.assertEqual([call["action"] for call in transport.calls], ["server", "server"])
        self.assertEqual(transport.calls[0]["id"], SERVER_ID)

    def test_poll_waits_for_active_and_a_filled_address(self):
        """The vendor doc: active *and* the addresses are filled in."""
        transport = RecordedTransport(
            {
                ("GET", "server"): [
                    (200, server_row(status="active", ipv4_address=None)),
                    (200, server_row()),
                ]
            }
        )
        provider = servury.ServuryProvider(TEST_KEY, transport=transport)
        machine = provider.poll(SERVER_ID, timeout=5, interval=0)
        self.assertEqual(machine.ipv4, "203.0.113.120")
        self.assertEqual([call["action"] for call in transport.calls], ["server", "server"])

    def test_poll_times_out_when_active_has_no_address(self):
        transport = RecordedTransport(
            {("GET", "server"): [(200, server_row(status="active", ipv4_address=None))]}
        )
        provider = servury.ServuryProvider(TEST_KEY, transport=transport)
        with self.assertRaises(ProviderTimeout) as caught:
            provider.poll(SERVER_ID, timeout=0, interval=0)
        self.assertIn("IPv4", str(caught.exception))

    def test_poll_times_out_without_sleeping_forever(self):
        transport = RecordedTransport(
            {("GET", "server"): [(200, server_row(status="deploying", ipv4_address=None))]}
        )
        provider = servury.ServuryProvider(TEST_KEY, transport=transport)
        with self.assertRaises(ProviderTimeout) as caught:
            provider.poll(SERVER_ID, timeout=0, interval=0)
        self.assertIn("deploying", str(caught.exception))

    def test_poll_reports_a_failed_build_with_its_reason(self):
        row = server_row(status="failed", status_reason="deploy")
        provider = servury.ServuryProvider(
            TEST_KEY, transport=RecordedTransport({("GET", "server"): [(200, row)]})
        )
        with self.assertRaises(ProviderError) as caught:
            provider.poll(SERVER_ID, timeout=5, interval=0)
        self.assertIn("failed", str(caught.exception))
        self.assertIn("deploy", str(caught.exception))

    def test_list_reads_every_machine(self):
        transport = RecordedTransport({("GET", "servers"): [(200, servers_response())]})
        provider = servury.ServuryProvider(TEST_KEY, transport=transport)
        machines = provider.list()
        self.assertEqual([machine.server_id for machine in machines], [SERVER_ID])
        self.assertEqual(machines[0].state, "active")

    def test_a_base_url_with_a_query_is_joined_with_an_ampersand(self):
        transport = RecordedTransport({("GET", "servers"): [(200, servers_response())]})
        provider = servury.ServuryProvider(
            TEST_KEY,
            base_url="https://servury.com/api/reseller.php?x=1",
            transport=transport,
        )
        provider.list()
        self.assertIn("?x=1&action=servers", transport.calls[0]["url"])
        self.assertNotIn("?x=1?action", transport.calls[0]["url"])

    def test_power_sends_the_action_and_reads_the_machine_back(self):
        transport = RecordedTransport(
            {
                ("POST", "power"): [(200, {"success": True, "data": {"status": "active"}})],
                ("GET", "server"): [(200, server_row())],
            }
        )
        provider = servury.ServuryProvider(TEST_KEY, transport=transport)
        machine = provider.power(SERVER_ID, "restart")
        self.assertEqual(machine.state, "active")
        self.assertEqual(transport.calls[0]["body"], {"action": "restart"})
        self.assertEqual(transport.calls[0]["id"], SERVER_ID)

    def test_power_refuses_an_unknown_action_before_calling(self):
        provider = servury.ServuryProvider(TEST_KEY, transport=RecordedTransport({}))
        with self.assertRaises(ValueError):
            provider.power(SERVER_ID, "delete")

    def test_terminate_is_idempotent_on_404(self):
        transport = RecordedTransport(
            {("POST", "terminate"): [(404, error_response(message="Server not found"))]}
        )
        provider = servury.ServuryProvider(TEST_KEY, transport=transport)
        self.assertFalse(provider.terminate(SERVER_ID))

    def test_terminate_returns_true_when_the_call_was_accepted(self):
        transport = RecordedTransport(
            {("POST", "terminate"): [(200, {"success": True, "data": {"message": "terminated"}})]}
        )
        provider = servury.ServuryProvider(TEST_KEY, transport=transport)
        self.assertTrue(provider.terminate(SERVER_ID))

    def test_terminate_does_not_swallow_a_server_error(self):
        transport = RecordedTransport(
            {("POST", "terminate"): [(503, error_response(message="try much later"))]}
        )
        provider = servury.ServuryProvider(TEST_KEY, transport=transport)
        with self.assertRaises(ProviderError) as caught:
            provider.terminate(SERVER_ID)
        self.assertEqual(caught.exception.status, 503)
        self.assertIn("try much later", str(caught.exception))

    def test_a_200_response_with_success_false_is_an_error(self):
        transport = RecordedTransport(
            {("GET", "servers"): [(200, {"success": False, "error": "account locked"})]}
        )
        provider = servury.ServuryProvider(TEST_KEY, transport=transport)
        with self.assertRaises(ProviderError) as caught:
            provider.list()
        self.assertEqual(caught.exception.status, 200)
        self.assertIn("account locked", str(caught.exception))

    def test_an_error_response_carries_the_status_and_never_the_key(self):
        transport = RecordedTransport(
            {
                ("POST", "create"): [
                    (402, error_response(message="Payment refused: insufficient balance."))
                ]
            }
        )
        provider = servury.ServuryProvider(TEST_KEY, transport=transport)
        with self.assertRaises(ProviderError) as caught:
            provider.create(base.CreateSpec(hostname=SLUG, plan_id=113, ssh_key=KEY_TEXT))
        self.assertEqual(caught.exception.status, 402)
        self.assertIn("insufficient balance", str(caught.exception))
        self.assertNotIn(TEST_KEY, str(caught.exception))

    def test_a_non_json_response_is_a_provider_error(self):
        provider = servury.ServuryProvider(
            TEST_KEY, transport=lambda method, url, headers, body: (502, b"<html>bad gateway</html>")
        )
        with self.assertRaises(ProviderError) as caught:
            provider.list()
        self.assertEqual(caught.exception.status, 502)
        self.assertIn("non-JSON", str(caught.exception))

    def test_a_non_object_json_response_is_a_provider_error(self):
        for payload in ([], None, "ok"):
            with self.subTest(payload=payload):
                provider = servury.ServuryProvider(
                    TEST_KEY,
                    transport=lambda method, url, headers, body, payload=payload: (
                        200,
                        json.dumps(payload).encode("utf-8"),
                    ),
                )
                with self.assertRaises(ProviderError) as caught:
                    provider.list()
                self.assertEqual(caught.exception.status, 200)

    def test_a_missing_server_object_is_a_provider_error(self):
        provider = servury.ServuryProvider(
            TEST_KEY,
            transport=RecordedTransport(
                {("GET", "server"): [(200, {"success": True, "data": {}})]}
            ),
        )
        with self.assertRaises(ProviderError):
            provider.poll(SERVER_ID, timeout=1, interval=0)


# -------------------------------------------------------------------- CLI dry run


class DryRunTest(TempConfigMixin):
    def test_provision_dry_run_never_builds_a_provider(self):
        code, out, err = run_cli(
            [
                "provision",
                SLUG,
                "--key",
                str(self.key_file()),
                "--pin",
                str(self.pin_file()),
                "--dry-run",
            ],
            self.make_config(),
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 0, err)
        self.assertIn("dry run", out)
        self.assertIn("provider.create", out)
        self.assertIn("provider.poll", out)
        self.assertIn("2026.11.1", out)
        self.assertIn("provision_release", out)

    def test_every_command_has_a_dry_run_with_json(self):
        key = str(self.key_file())
        argv_by_command = {
            "invite": ["invite", "ada@example.com"],
            "provision": ["provision", SLUG, "--key", key],
            "status": ["status"],
            "destroy": ["destroy", SLUG],
            "rotate-token": ["rotate-token", SLUG],
            "reconcile": ["reconcile"],
            "restore": ["restore", SLUG],
            "support": ["support", SLUG],
            "export": ["export", SLUG],
        }
        routes = {("GET", "servers"): [(200, servers_response())]}
        for name, argv in argv_by_command.items():
            with self.subTest(command=name):
                if name == "reconcile":
                    # Reconcile is read-only by default *and* has to read the
                    # provider to report drift, so it is the one command whose
                    # dry run builds a provider. An empty account keeps the
                    # expected report at zero findings.
                    factory = lambda _config: fixture_provider(  # noqa: E731
                        {("GET", "servers"): [(200, empty_servers_response())]}
                    )
                elif name in ("status", "destroy"):
                    factory = lambda _config: fixture_provider(routes)  # noqa: E731
                else:
                    factory = explode_provider
                code, out, err = run_cli(
                    [*argv, "--dry-run", "--json"],
                    self.make_config(),
                    provider_factory=factory,
                )
                self.assertEqual(code, 0, err)
                payload = json.loads(out)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["command"], name)
                self.assertTrue(payload["dry_run"])
                if name == "status":
                    self.assertIn("machines", payload)
                elif name == "reconcile":
                    self.assertEqual(payload["drift"], [])
                else:
                    self.assertIn("actions", payload)

    def test_apply_and_dry_run_together_stay_dry(self):
        code, out, err = run_cli(
            ["--apply", "--dry-run", "invite", "ada@example.com"],
            self.make_config(),
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 0, err)
        self.assertIn("dry run", out)

    def test_a_mutating_command_without_apply_is_a_dry_run(self):
        code, out, err = run_cli(
            ["invite", "ada@example.com"],
            self.make_config(),
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 0, err)
        self.assertIn("dry run", out)

    def test_the_default_safe_invocation_reports_dry_run_in_json(self):
        """No --apply and no --dry-run still means "nothing happened"."""
        key = str(self.key_file())
        argv_by_command = {
            "invite": ["invite", "ada@example.com"],
            "provision": ["provision", SLUG, "--key", key],
            "destroy": ["destroy", SLUG],
            "rotate-token": ["rotate-token", SLUG],
            "reconcile": ["reconcile"],
            "restore": ["restore", SLUG],
            "support": ["support", SLUG],
            "export": ["export", SLUG],
        }
        for name, argv in argv_by_command.items():
            with self.subTest(command=name):
                if name == "destroy":
                    routes = {("GET", "servers"): [(200, servers_response())]}
                    factory = lambda _config: servury.ServuryProvider(  # noqa: E731
                        TEST_KEY, transport=RecordedTransport(routes)
                    )
                elif name == "reconcile":
                    factory = lambda _config: fixture_provider(  # noqa: E731
                        {("GET", "servers"): [(200, empty_servers_response())]}
                    )
                else:
                    factory = explode_provider
                code, out, err = run_cli(
                    [*argv, "--json"], self.make_config(), provider_factory=factory
                )
                self.assertEqual(code, 0, err)
                payload = json.loads(out)
                self.assertTrue(
                    payload["dry_run"],
                    f"{name} reported dry_run=false although no --apply was passed",
                )
                if name == "reconcile":
                    self.assertEqual(payload["drift"], [])
                else:
                    self.assertIn("actions", payload)
                self.assertTrue(payload["ok"])

    def test_a_usage_error_with_json_is_one_json_object(self):
        code, out, err = run_cli(
            ["status", "--json", "--bogus"],
            self.make_config(),
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["command"], "status")
        self.assertIn("--bogus", payload["error"])

    def test_an_unknown_command_with_json_is_a_usage_error_object(self):
        code, out, err = run_cli(
            ["--json", "bootstrap"],
            self.make_config(),
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("bootstrap", payload["error"])

    def test_rotate_token_refuses_a_non_positive_ttl(self):
        code, out, err = run_cli(
            ["rotate-token", SLUG, "--ttl-hours", "0", "--dry-run"],
            self.make_config(),
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 2)
        self.assertIn("--ttl-hours", err)

    def test_restore_apply_without_a_password_file_is_a_usage_error(self):
        code, out, err = run_cli(
            ["restore", SLUG, "--apply", "--repo", "/var/tmp/does-not-matter", "--json"],
            self.make_config(),
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("--password-file", payload["error"])
        self.assertIn("recovery file", payload["error"])

    def test_a_bad_email_is_a_usage_error(self):
        code, out, err = run_cli(
            ["invite", "not-an-email", "--dry-run"],
            self.make_config(),
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 2)
        self.assertIn("not an email", err)

    def test_provision_apply_without_a_key_is_a_usage_error(self):
        code, out, err = run_cli(
            ["provision", SLUG, "--apply"],
            self.make_config(),
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 2)
        self.assertIn("--key", err)

    def test_a_missing_config_is_exit_5_and_json_still_parses(self):
        code, out, err = run_cli(
            ["status", "--json", "--config", "/nonexistent/odilectl.yaml"],
            self.make_config(),
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 5)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("no config", payload["error"])


# --------------------------------------------------------------- the fixture flow


class FixtureFlowTest(TempConfigMixin):
    def _flow_transport(self, **machine_overrides):
        return RecordedTransport(
            {
                ("GET", "servers"): [(200, empty_servers_response())],
                ("POST", "create"): [(200, create_response())],
                ("GET", "server"): [
                    (200, server_row(status="deploying", ipv4_address=None)),
                    (200, server_row(**machine_overrides)),
                ],
            }
        )

    def test_provision_apply_runs_create_then_poll_and_reports_the_machine(self):
        transport = self._flow_transport(measured_release="2026.09.9")
        pin = self.pin_file()
        config_path = self.make_config()
        code, out, err = run_cli(
            [
                "provision",
                SLUG,
                "--key",
                str(self.key_file()),
                "--pin",
                str(pin),
                "--interval",
                "0",
                "--apply",
                "--json",
            ],
            config_path,
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["dry_run"])
        self.assertTrue(payload["recorded"])
        self.assertEqual(payload["release"], "2026.11.1")
        self.assertEqual(payload["machine"]["server_id"], SERVER_ID)
        self.assertEqual(payload["machine"]["state"], "active")
        # The provider's row said measured_release 2026.09.9; only the pin's
        # provision_release is a source, so the row must say 2026.11.1.
        self.assertEqual(payload["machine"]["detail"].get("measured_release"), None)
        self.assertEqual(payload["machine"]["ipv4"], "203.0.113.120")
        self.assertEqual(payload["machine"]["expires_at"], 1792000000)
        recorded = state.StateStore.open(Path(config_path).parent / "waitlist.db")
        self.addCleanup(recorded.close)
        self.assertEqual(recorded.get(SLUG).release, "2026.11.1")
        self.assertEqual(
            [call["action"] for call in transport.calls],
            ["servers", "create", "server", "server"],
        )
        create = transport.calls[1]
        self.assertEqual(create["body"]["ssh_key"], KEY_TEXT)
        self.assertTrue(create["body"]["snp"])
        self.assertTrue(create["body"]["fde"])
        self.assertTrue(create["body"]["measured"])
        self.assertEqual(transport.calls[2]["id"], SERVER_ID)

    def test_provision_refuses_a_pin_without_provision_release_and_orders_nothing(self):
        config_path = self.make_config()
        code, out, err = run_cli(
            [
                "provision",
                SLUG,
                "--key",
                str(self.key_file()),
                "--pin",
                str(self.pin_file(release=None)),
                "--apply",
            ],
            config_path,
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 2)
        self.assertIn("provision_release", err)
        self.assertIn("unpinned", err)
        self.assertEqual(out, "")
        self.assertFalse((Path(config_path).parent / "waitlist.db").exists())

    def test_provision_apply_reuses_an_active_machine(self):
        transport = RecordedTransport({("GET", "servers"): [(200, servers_response())]})
        code, out, err = run_cli(
            ["provision", SLUG, "--key", str(self.key_file()), "--apply", "--json"],
            self.make_config(),
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertFalse(payload["created"])
        self.assertEqual(payload["machine"]["state"], "active")
        self.assertEqual([call["action"] for call in transport.calls], ["servers"])

    def test_provision_apply_polls_an_existing_pending_machine(self):
        transport = RecordedTransport(
            {
                ("GET", "servers"): [
                    (200, servers_response(machine_row(status="deploying", ipv4_address=None)))
                ],
                ("GET", "server"): [(200, server_row())],
            }
        )
        code, out, err = run_cli(
            ["provision", SLUG, "--key", str(self.key_file()), "--apply", "--json"],
            self.make_config(),
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertFalse(payload["created"])
        self.assertEqual([call["action"] for call in transport.calls], ["servers", "server"])

    def test_provision_polls_an_active_order_until_the_address_is_filled(self):
        """`create` may answer active before the address exists; poll anyway."""
        transport = RecordedTransport(
            {
                ("GET", "servers"): [(200, empty_servers_response())],
                ("POST", "create"): [
                    (200, create_response(status="active", ipv4_address=None))
                ],
                ("GET", "server"): [(200, server_row())],
            }
        )
        code, out, err = run_cli(
            [
                "provision",
                SLUG,
                "--key",
                str(self.key_file()),
                "--interval",
                "0",
                "--apply",
                "--json",
            ],
            self.make_config(),
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["machine"]["ipv4"], "203.0.113.120")
        self.assertEqual(
            [call["action"] for call in transport.calls], ["servers", "create", "server"]
        )

    def test_provision_polls_an_existing_active_machine_without_an_address(self):
        transport = RecordedTransport(
            {
                ("GET", "servers"): [
                    (200, servers_response(machine_row(status="active", ipv4_address=None)))
                ],
                ("GET", "server"): [(200, server_row())],
            }
        )
        code, out, err = run_cli(
            ["provision", SLUG, "--key", str(self.key_file()), "--apply", "--json"],
            self.make_config(),
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertFalse(payload["created"])
        self.assertEqual(payload["machine"]["ipv4"], "203.0.113.120")
        self.assertEqual([call["action"] for call in transport.calls], ["servers", "server"])

    def test_provision_preorder_reports_a_queued_paid_order(self):
        transport = RecordedTransport(
            {
                ("GET", "servers"): [(200, empty_servers_response())],
                ("POST", "create"): [
                    (200, create_response(status="queued", queue_position=3))
                ],
            }
        )
        code, out, err = run_cli(
            [
                "provision",
                SLUG,
                "--key",
                str(self.key_file()),
                "--preorder",
                "--timeout",
                "0",
                "--apply",
                "--json",
            ],
            self.make_config(),
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["created"])
        self.assertTrue(payload["queued"])
        self.assertEqual(payload["queue_position"], 3)
        self.assertEqual(payload["machine"]["state"], "pending")
        self.assertEqual(payload["machine"]["raw_status"], "queued")
        self.assertEqual(payload["machine"]["detail"]["queue_position"], 3)
        self.assertEqual([call["action"] for call in transport.calls], ["servers", "create"])
        self.assertTrue(transport.calls[1]["body"]["preorder"])

    def test_provision_preorder_second_run_reuses_the_queued_order(self):
        transport = RecordedTransport(
            {
                ("GET", "servers"): [
                    (
                        200,
                        servers_response(
                            machine_row(status="queued", queue_position=3, ipv4_address=None)
                        ),
                    )
                ]
            }
        )
        code, out, err = run_cli(
            ["provision", SLUG, "--key", str(self.key_file()), "--preorder", "--apply", "--json"],
            self.make_config(),
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertFalse(payload["created"])
        self.assertTrue(payload["queued"])
        self.assertEqual(payload["queue_position"], 3)
        self.assertEqual([call["action"] for call in transport.calls], ["servers"])

    def test_provision_refuses_two_machines_with_one_name(self):
        transport = RecordedTransport(
            {
                ("GET", "servers"): [
                    (200, servers_response(machine_row(), machine_row(id="def67890")))
                ]
            }
        )
        code, out, err = run_cli(
            ["provision", SLUG, "--key", str(self.key_file()), "--apply"],
            self.make_config(),
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 4)
        self.assertIn("refusing to guess", err)

    def test_provision_human_output_names_the_address_and_the_release(self):
        transport = self._flow_transport()
        code, out, err = run_cli(
            [
                "provision",
                SLUG,
                "--key",
                str(self.key_file()),
                "--pin",
                str(self.pin_file()),
                "--interval",
                "0",
                "--apply",
            ],
            self.make_config(),
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 0, err)
        self.assertIn(SERVER_ID, out)
        self.assertIn("203.0.113.120", out)
        self.assertIn("2026.11.1", out)
        self.assertIn("recorded with the tenant", out)

    def test_a_failed_order_is_a_provider_error_and_not_a_traceback(self):
        transport = RecordedTransport(
            {
                ("GET", "servers"): [(200, empty_servers_response())],
                ("POST", "create"): [
                    (409, error_response(message="Plan is sold out; send preorder true."))
                ],
            }
        )
        code, out, err = run_cli(
            ["provision", SLUG, "--key", str(self.key_file()), "--apply"],
            self.make_config(),
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 4)
        self.assertIn("sold out", err)


# --------------------------------------------------------------- status and destroy


class StatusDestroyTest(TempConfigMixin):
    def test_status_lists_one_machine_and_filters_by_slug(self):
        routes = {("GET", "servers"): [(200, servers_response())]}
        code, out, err = run_cli(
            ["status", "--json"],
            self.make_config(),
            provider_factory=lambda _config: fixture_provider(routes),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["machines"][0]["server_id"], SERVER_ID)

        routes = {("GET", "servers"): [(200, servers_response())]}
        code, out, err = run_cli(
            ["status", "odl-000000", "--json"],
            self.make_config(),
            provider_factory=lambda _config: fixture_provider(routes),
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["count"], 0)

    def test_status_shows_release_drift_and_same(self):
        """The status view prints the intended and booted release side by side."""
        config_path = self.make_config()
        db = Path(config_path).parent / "waitlist.db"
        store = state.StateStore.open(db)
        try:
            store.invite(SLUG)
            store.claim(SLUG)
            store.provision(SLUG, server_id=SERVER_ID, release="2026.11.1")
        finally:
            store.close()
        beats = health.HeartbeatStore.open(
            db, clock=lambda: 1_800_000_000, verifier=lambda *_args: True
        )
        try:
            body = json.dumps(
                {
                    "slug": SLUG,
                    "at": 1_800_000_000,
                    "facts": {
                        "boot": {"release": "2026.12.0", "measurement": "11" * 48}
                    },
                    "public_key": "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----",
                },
                sort_keys=True,
            ).encode()
            beats.record(body, b"\x00" * 64, now=1_800_000_000)
        finally:
            beats.close()

        routes = {("GET", "servers"): [(200, servers_response())]}
        code, out, err = run_cli(
            ["status", "--json"],
            config_path,
            provider_factory=lambda _config: fixture_provider(routes),
        )
        self.assertEqual(code, 0, err)
        release = json.loads(out)["machines"][0]["release"]
        self.assertEqual(release["intended"], "2026.11.1")
        self.assertEqual(release["booted"], "2026.12.0")
        self.assertEqual(release["status"], "release_drift")

        beats = health.HeartbeatStore.open(
            db, clock=lambda: 1_800_000_060, verifier=lambda *_args: True
        )
        try:
            body = json.dumps(
                {
                    "slug": SLUG,
                    "at": 1_800_000_060,
                    "facts": {
                        "boot": {"release": "2026.11.1", "measurement": "11" * 48}
                    },
                },
                sort_keys=True,
            ).encode()
            beats.record(body, b"\x00" * 64, now=1_800_000_060)
        finally:
            beats.close()

        routes = {("GET", "servers"): [(200, servers_response())]}
        code, out, err = run_cli(
            ["status", "--json"],
            config_path,
            provider_factory=lambda _config: fixture_provider(routes),
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["machines"][0]["release"]["status"], "same")

    def test_status_shows_the_backup_age_and_snapshot(self):
        """The nightly job's last success is visible in `odilectl status`."""
        config_path = self.make_config()
        db = Path(config_path).parent / "waitlist.db"
        beats = health.HeartbeatStore.open(
            db, clock=lambda: 1_800_000_000, verifier=lambda *_args: True
        )
        try:
            body = json.dumps(
                {
                    "slug": SLUG,
                    "at": 1_800_000_000,
                    "facts": {
                        "last_backup_at": 1_799_999_000,
                        "backup": {
                            "repo": "sftp:odile-backup@backup.odile.fyi:/tenants/" + SLUG,
                            "snapshot": "5f8c1d2e3a4b5c6d7e8f9012a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d",
                            "status": "ok",
                            "repo_bytes": 4096,
                        },
                    },
                    "public_key": "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----",
                },
                sort_keys=True,
            ).encode()
            beats.record(body, b"\x00" * 64, now=1_800_000_000)
        finally:
            beats.close()

        routes = {("GET", "servers"): [(200, servers_response())]}
        code, out, err = run_cli(
            ["status", "--json"],
            config_path,
            provider_factory=lambda _config: fixture_provider(routes),
        )
        self.assertEqual(code, 0, err)
        backup = json.loads(out)["machines"][0]["backup"]
        self.assertEqual(backup["status"], "reported")
        self.assertEqual(backup["snapshot"], "5f8c1d2e3a4b5c6d7e8f9012a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d")
        self.assertEqual(backup["repo_bytes"], 4096)
        self.assertEqual(backup["last_backup_at"], 1_799_999_000)

        routes = {("GET", "servers"): [(200, servers_response())]}
        code, out, err = run_cli(
            ["status", "odl-7f3a2c"],
            config_path,
            provider_factory=lambda _config: fixture_provider(routes),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("backup:    last 2027-01-15T07:43:20Z", out)
        self.assertIn("snapshot 5f8c1d2e", out)
        self.assertIn("4096 bytes in the repository", out)

    def test_status_without_a_heartbeat_says_so(self):
        routes = {("GET", "servers"): [(200, servers_response())]}
        code, out, err = run_cli(
            ["status", "odl-7f3a2c"],
            self.make_config(),
            provider_factory=lambda _config: fixture_provider(routes),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("backup:    no heartbeat yet", out)

    def test_destroy_dry_run_only_reads(self):
        transport = RecordedTransport({("GET", "servers"): [(200, servers_response())]})
        code, out, err = run_cli(
            ["destroy", SLUG, "--dry-run"],
            self.make_config(),
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 0, err)
        self.assertEqual([call["action"] for call in transport.calls], ["servers"])
        self.assertIn("provider.terminate", out)
        self.assertIn("dns.delete", out)
        self.assertIn("backup.retain", out)

    def test_destroy_apply_terminates_the_machine_it_found(self):
        transport = RecordedTransport(
            {
                ("GET", "servers"): [
                    (200, servers_response()),
                    (200, empty_servers_response()),
                ],
                ("POST", "terminate"): [
                    (200, {"success": True, "data": {"message": "Server terminated."}})
                ],
            }
        )
        code, out, err = run_cli(
            ["destroy", SLUG, "--skip-export", "--apply", "--json"],
            self.make_config(),
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["destroyed"])
        self.assertEqual(payload["server_id"], SERVER_ID)
        self.assertEqual(
            [call["action"] for call in transport.calls],
            ["servers", "terminate", "servers"],
        )

    def test_destroy_of_an_absent_machine_is_success(self):
        transport = RecordedTransport({("GET", "servers"): [(200, servers_response())]})
        code, out, err = run_cli(
            ["destroy", "odl-000000", "--skip-export", "--apply", "--json"],
            self.make_config(),
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertFalse(payload["destroyed"])
        self.assertTrue(payload["already_gone"])
        self.assertEqual([call["action"] for call in transport.calls], ["servers"])

    def test_destroy_refuses_two_machines_with_one_name(self):
        two = servers_response(machine_row(), machine_row(id="def67890"))
        transport = RecordedTransport({("GET", "servers"): [(200, two)]})
        code, out, err = run_cli(
            ["destroy", SLUG, "--skip-export", "--apply"],
            self.make_config(),
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 4)
        self.assertIn("refusing to guess", err)


# ------------------------------------------------------- no shell, no vendor leak


class BoundaryTest(unittest.TestCase):
    """Two architectural promises, checked on the source as a last resort.

    These are not behaviour tests on purpose: a new `subprocess` call is a new
    way to need a shell on a tenant, and a vendor URL outside `servury.py` is a
    second place to change when the API changes.
    """

    def _package_files(self):
        for path in sorted(PACKAGE.rglob("*")):
            if path.is_file() and (path.suffix == ".py" or path.name == "odilectl"):
                yield path

    def test_no_command_can_open_a_shell_on_a_tenant(self):
        """A command may talk to the provider and probe a host key; never a shell.

        The health poll (ODL-216) opens one SSH *key exchange* per tenant on
        port 22 to read the host key — sockets, no session, no authentication.
        It also prints an `ssh -i ... unlock` instruction into the owner's
        email.  Neither is an operator shell on the tenant, so the rule is the
        process/terminal escape hatches below, not the word `ssh` in a string.
        """
        forbidden = (
            "subprocess",
            "paramiko",
            "pexpect",
            "os.system",
            "os.popen",
            "pty.spawn",
        )
        # ODL-215 is the one deliberate exception, and only for `subprocess`:
        # `restore --apply` runs the local `restic` binary on the machine that
        # holds the user's recovery file — the user's laptop, or a rebuilt
        # guest — never an operator session on a tenant. It learns no tenant
        # address and opens no shell there; the SFTP transport is a string
        # restic itself parses. Every other escape hatch stays banned in this
        # file too, so a later `os.system` cannot hide behind the exception.
        local_binary_exception = PACKAGE / "restore.py"
        for path in self._package_files():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for token in forbidden:
                    if path == local_binary_exception and token == "subprocess":
                        continue
                    self.assertNotIn(
                        token, line, f"{path.relative_to(ROOT)}:{lineno} has {token!r}"
                    )

    def test_servury_stays_inside_providers_servury_py(self):
        for path in self._package_files():
            if path == PACKAGE / "providers" / "servury.py":
                continue
            text = path.read_text(encoding="utf-8")
            for token in ("reseller.php", "servury.com", "X-Reseller-Key"):
                self.assertNotIn(token, text, f"{path.relative_to(ROOT)} leaks {token}")

    def test_the_vendor_name_itself_stays_inside_the_implementation(self):
        """The goal sentence is literal: one file knows the vendor's name.

        The registry discovers implementations by scanning the package, so no
        module outside the implementation has to name it.
        """
        pattern = re.compile(r"servury", re.IGNORECASE)
        for path in self._package_files():
            if path == PACKAGE / "providers" / "servury.py":
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                self.assertIsNone(
                    pattern.search(line),
                    f"{path.relative_to(ROOT)}:{lineno} names the vendor: {line.strip()}",
                )

    def test_the_interface_declares_the_five_operations(self):
        for name in ("create", "poll", "power", "terminate", "list"):
            method = getattr(base.Provider, name, None)
            self.assertIsNotNone(method, f"Provider.{name} is missing")
            self.assertTrue(
                getattr(method, "__isabstractmethod__", False),
                f"Provider.{name} is not abstract",
            )


class RestoreTest(TempConfigMixin):
    """ODL-215: the restore plans without a password and applies with the user's file."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="odile-restore-test-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.recovery = self.directory / "recovery.txt"
        self.recovery.write_text(
            "Odile recovery file\nBackup password:     " + "A" * 32 + "\n",
            encoding="utf-8",
        )
        self.argv_log = self.directory / "argv.txt"
        self.password_log = self.directory / "password.txt"
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        restic = self.bin / "restic"
        restic.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$@\" > \"$FAKE_ARGV\"\n"
            "if [ -n \"$RESTIC_PASSWORD_FILE\" ]; then cat \"$RESTIC_PASSWORD_FILE\" > \"$FAKE_PASS\"; fi\n"
            "if [ \"$1\" = snapshots ]; then printf '[]\\n'; exit 0; fi\n"
            "if [ \"$1\" = restore ]; then\n"
            "  prev=''\n"
            "  for arg in \"$@\"; do\n"
            "    [ \"$prev\" = --target ] && mkdir -p \"$arg\"\n"
            "    prev=$arg\n"
            "  done\n"
            "  echo 'Summary: Restored 1 files (fake)'\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        restic.chmod(0o755)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "FAKE_ARGV": str(self.argv_log),
                "FAKE_PASS": str(self.password_log),
                "RESTIC_CACHE_DIR": str(self.directory / "cache"),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_the_plan_changes_nothing_and_names_the_repository(self):
        code, out, err = run_cli(
            ["restore", SLUG, "--json"],
            self.make_config(backup_repo="sftp:odile-backup@backup.example:/tenants/{slug}"),
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["repository"], "sftp:odile-backup@backup.example:/tenants/" + SLUG)
        self.assertFalse(self.argv_log.exists())

    def test_apply_runs_restic_with_the_password_in_a_file_not_argv(self):
        target = self.directory / "restore"
        code, out, err = run_cli(
            [
                "restore", SLUG, "--apply", "--repo", str(self.directory / "repo"),
                "--password-file", str(self.recovery), "--target", str(target),
                "--include", "/home/tenant",
            ],
            self.make_config(),
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 0, err)
        argv = self.argv_log.read_text().splitlines()
        self.assertEqual(argv[:2], ["restore", "latest"])
        self.assertIn("--include", argv)
        self.assertIn("/home/tenant", argv)
        self.assertNotIn("A" * 32, "\n".join(argv))
        self.assertEqual(self.password_log.read_text(), "A" * 32)
        self.assertTrue(target.is_dir())
        self.assertIn("restore: applied", out)

    def test_verify_lists_the_snapshots_and_proves_the_password(self):
        code, out, err = run_cli(
            [
                "restore", SLUG, "--verify", "--repo", str(self.directory / "repo"),
                "--password-file", str(self.recovery),
            ],
            self.make_config(),
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 0, err)
        self.assertIn("the password opens", out)
        self.assertEqual(self.argv_log.read_text().splitlines()[:2], ["snapshots", "--json"])

    def test_a_recovery_file_without_the_line_is_a_usage_error(self):
        self.recovery.write_text("Tenant: " + SLUG + "\n", encoding="utf-8")
        code, out, err = run_cli(
            ["restore", SLUG, "--apply", "--repo", str(self.directory / "repo"),
             "--password-file", str(self.recovery), "--target", str(self.directory / "r"),
             "--json"],
            self.make_config(),
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 2)
        self.assertIn("no 'Backup password:' line", json.loads(out)["error"])


if __name__ == "__main__":
    unittest.main()
