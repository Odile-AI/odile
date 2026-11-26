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
import sqlite3
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

import app  # noqa: E402
from odilectl import (  # noqa: E402
    backup_usage, bootrecord, cli, config, health, offboard, provision, providers, state,
)
from odilectl.core import Context  # noqa: E402
from odilectl.errors import ConfigError, ProviderError, ProviderTimeout  # noqa: E402
from odilectl.providers import base, servury  # noqa: E402
from tests import box_fixture  # noqa: E402

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
    # ODL-924: a provision mints the tenant's backup sub-account pair. The Box
    # API, ssh-keygen, the host-key scan and install-ssh-key are the four
    # seams; a provision in this module never opens a socket or runs a command.
    _OFFBOARD_PATCHES.extend(box_fixture.start_patchers())


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
        self._directory = Path(directory)
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
            # ODL-924: the box, the store of record, the key tree and the seed
            # handoff all live under this test's own directory.
            "hetzner_box_id": "709870",
            "hetzner_dir": str(self._directory / "hetzner"),
            "hetzner_keys": str(self._directory / "keys"),
            "seed_backup_pairs": str(self._directory / "seed-backup-pairs.json"),
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

    def test_provision_orders_the_nano_default_from_the_config(self):
        """The shipped config's 138 / 365 / 1 reaches the vendor as the order.

        Measured through the create body, not the source text: a config carrying
        the Nano defaults must order plan 138 for 365 days with no --plan or
        --days flag. A Core order (108/113) would fail here.
        """
        transport = self._flow_transport(measured_release="2026.09.9")
        config_path = self.make_config(
            default_plan_id="138", default_days="365", default_vcpus="1")
        code, out, err = run_cli(
            [
                "provision", SLUG,
                "--key", str(self.key_file()),
                "--pin", str(self.pin_file()),
                "--interval", "0",
                "--apply", "--json",
            ],
            config_path,
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 0, err)
        create = transport.calls[1]
        self.assertEqual(create["body"]["plan_id"], 138)
        self.assertEqual(create["body"]["days"], 365)

    def test_provision_with_no_plan_in_the_config_refuses_rather_than_defaulting(self):
        """A missing default_plan_id is an error, never a silent Core order."""
        transport = self._flow_transport()
        config_path = self.make_config(default_plan_id=None)
        code, out, err = run_cli(
            [
                "provision", SLUG,
                "--key", str(self.key_file()),
                "--pin", str(self.pin_file()),
                "--interval", "0",
                "--apply", "--json",
            ],
            config_path,
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("default_plan_id", out + err)
        self.assertEqual([call["action"] for call in transport.calls], [])

    def _destroyed_row(self, config_path) -> Path:
        """A tenant that reached ready and was then destroyed (ADR-007)."""
        db = Path(config_path).parent / "waitlist.db"
        store = state.StateStore.open(db)
        try:
            store.invite(SLUG)
            store.claim(SLUG)
            store.provision(
                SLUG,
                server_id="srv-old-0001",
                ipv4="203.0.113.7",
                plan_id=113,
                release="2026.11.1",
                expires_at=1_792_000_000,
            )
            store.await_boot(SLUG)
            store.start_first_visit(SLUG)
            store.ready(SLUG)
            store.destroy(SLUG, reason="provider wipe")
        finally:
            store.close()
        return db

    def _web_row(self, db: Path, *, server_id: str = "srv-old-0001") -> None:
        """The app's `tenants` row, as a destroyed tenant leaves it.

        The live row after `offboard` was measured like this: `state active`,
        `server_id` still the dead machine (verifier probe on 2026-11-16).
        """
        con = sqlite3.connect(db)
        try:
            app.migrate(con)
            con.execute(
                "INSERT INTO tenants (slug, email, state, server_id, ssh_public_key,"
                " release, expires_at, claimed_at, installed_at, created_at)"
                " VALUES (?, ?, 'active', ?, 'ssh-ed25519 OLD', '2026.11.1',"
                " '2026-12-10T00:00:00Z', '2026-11-09T16:03:46Z',"
                " '2026-11-09T16:10:00Z', '2026-11-09T16:03:46Z')",
                (SLUG, "ada@example.com", server_id),
            )
            con.commit()
        finally:
            con.close()

    def _ready_row(self, config_path) -> Path:
        """A live tenant whose machine is gone: both schemas, no provider row.

        The state row reached `ready` on `srv-old-0001` and the web row still
        names it; `provider.list` is empty, which is what `reconcile` calls a
        missing machine. This is the fixture this card measured on 2026-11-18.
        """
        db = Path(config_path).parent / "waitlist.db"
        store = state.StateStore.open(db)
        try:
            store.invite(SLUG)
            store.claim(SLUG)
            store.provision(
                SLUG,
                server_id="srv-old-0001",
                ipv4="203.0.113.7",
                plan_id=113,
                release="2026.11.1",
                expires_at=1_792_000_000,
            )
            store.await_boot(SLUG)
            store.start_first_visit(SLUG)
            store.ready(SLUG)
        finally:
            store.close()
        self._web_row(db)
        return db

    def test_provision_on_a_destroyed_slug_reopens_it_under_the_same_slug(self):
        """The card's end-to-end: destroy, provision again, the slug survives."""
        config_path = self.make_config()
        db = self._destroyed_row(config_path)
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
                "--json",
            ],
            config_path,
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["reopened"])
        self.assertTrue(payload["created"])
        self.assertEqual(payload["machine"]["hostname"], SLUG)
        self.assertEqual(payload["machine"]["server_id"], SERVER_ID)
        # The order reached the machine: the row is first_boot, not provisioning,
        # so the machine's own boot record has a state to land on (must 1).
        self.assertEqual(payload["tenant_state"], "first_boot")
        self.assertEqual(payload["web_row"], "absent")  # this fixture has no web row
        recorded = state.StateStore.open(db)
        try:
            tenant = recorded.get(SLUG)
            events = recorded.events(SLUG)
        finally:
            recorded.close()
        self.assertEqual(tenant.slug, SLUG, "a rebuild keeps its slug")
        self.assertEqual(tenant.state, "first_boot")
        self.assertEqual(tenant.server_id, SERVER_ID, "the new machine is the row's machine")
        self.assertEqual(tenant.release, "2026.11.1")
        self.assertEqual(
            [event.kind for event in events][-4:],
            ["destroyed", "claimed", "provisioning", "first_boot"],
        )
        reopen = [event for event in events if event.kind == "claimed"][-1]
        self.assertEqual(reopen.from_state, "destroyed")
        self.assertIn("same slug", reopen.detail["reason"])
        self.assertEqual(f"https://{tenant.slug}.odile.fyi", f"https://{SLUG}.odile.fyi")

    def test_a_rebuilt_tenant_records_its_first_boot_on_the_new_machine(self):
        """The way back end-to-end: the new machine's measured line lands.

        The web row is half of the order (must 1): `record_boot` reads
        `tenants.server_id` and refuses a `provisioning` spine, so a rebuild
        that writes only the state row can never record its first boot. This
        fixture carries both schemas, as the live database does.
        """
        config_path = self.make_config()
        db = self._destroyed_row(config_path)
        self._web_row(db)
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
                "--json",
            ],
            config_path,
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["tenant_state"], "first_boot")
        self.assertEqual(payload["web_row"], "repointed")
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        row = con.execute(
            "SELECT state, server_id, ipv4, expires_at, installed_at,"
            " ssh_public_key, release, setup_token_hash FROM tenants WHERE slug = ?",
            (SLUG,),
        ).fetchone()
        # Every machine fact names the new machine before it boots; the old
        # machine's install time is gone, and the token seed.bind wrote survives.
        self.assertEqual(row["server_id"], SERVER_ID)
        self.assertEqual(row["state"], "provisioned")
        self.assertEqual(row["ipv4"], "203.0.113.120")
        self.assertEqual(row["expires_at"], "2026-10-14T17:46:40Z")
        self.assertIsNone(row["installed_at"])
        self.assertEqual(row["ssh_public_key"], KEY_TEXT)
        self.assertEqual(row["release"], "2026.11.1")
        self.assertIsNotNone(row["setup_token_hash"])
        # The new machine's measured line is accepted and the spine walks on.
        measured = (
            "root=/dev/mapper/root_crypt ro console=ttyS0 "
            f"bb.host={SLUG} bb.iid={SERVER_ID} "
            f"bb.key={base64.b64encode(KEY_TEXT.encode()).decode()} "
            f"odile.token={'ab' * 32}"
        )
        parsed = bootrecord.parse_record(
            {"cmdline": measured, "release": "2026.11.1", "first_visit": "done"},
            slug=SLUG,
        )
        written = bootrecord.record_boot(con, slug=SLUG, record=parsed, source="first_boot")
        self.assertEqual(
            written["changed"], ["first_boot -> first_visit", "first_visit -> ready"]
        )
        self.assertEqual(written["state"], "ready")
        self.assertEqual(written["tenants_state"], "active")
        after = con.execute(
            "SELECT state, server_id, ssh_public_key FROM tenants WHERE slug = ?", (SLUG,)
        ).fetchone()
        self.assertEqual(after["state"], "active")
        self.assertEqual(after["server_id"], SERVER_ID)
        self.assertEqual(after["ssh_public_key"], KEY_TEXT, "the measured boot key landed")
        # A record for the dead machine is still refused: the instance check.
        stale = bootrecord.parse_record(
            {"cmdline": measured.replace(SERVER_ID, "srv-old-0001"),
             "release": "2026.11.1", "first_visit": "done"},
            slug=SLUG,
        )
        with self.assertRaises(bootrecord.BootRecordError) as caught:
            bootrecord.record_boot(con, slug=SLUG, record=stale, source="first_boot")
        self.assertIn("srv-old-0001", str(caught.exception))

    def test_provision_refuses_a_live_row_whose_machine_is_gone(self):
        """No paid orphan: the run stops before `create`, and names reconcile.

        The card's fixture (verifier, 2026-11-18): a `ready` tenant naming
        `srv-old-0001`, both schemas, an empty provider list. Before the
        guard, `provision --apply` billed `abc12345` and recorded it on
        neither row (`recorded: false`, `web_row: "skipped"`). The recorded
        transport carries the create and poll routes the old run would take, so
        the assertion on `calls` is the money: a run that orders calls `create`.
        """
        config_path = self.make_config()
        db = self._ready_row(config_path)
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
                "--json",
            ],
            config_path,
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 4, err)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["dry_run"])
        error = payload["error"]
        self.assertIn(f"odilectl reconcile {SLUG} --apply", error)
        self.assertIn("srv-old-0001", error)
        self.assertIn("ready", error)
        # The money: the list ran, and create never did.
        self.assertEqual([call["action"] for call in transport.calls], ["servers"])
        # Neither row moved, and the event log gained nothing.
        store = state.StateStore.open(db)
        try:
            tenant = store.get(SLUG)
            events = store.events(SLUG)
        finally:
            store.close()
        self.assertEqual(tenant.state, "ready")
        self.assertEqual(tenant.server_id, "srv-old-0001")
        self.assertEqual(
            [event.kind for event in events],
            ["invited", "claimed", "provisioning", "first_boot", "first_visit", "ready"],
        )
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        row = con.execute(
            "SELECT state, server_id FROM tenants WHERE slug = ?", (SLUG,)
        ).fetchone()
        self.assertEqual((row["state"], row["server_id"]), ("active", "srv-old-0001"))

    def test_provision_refuses_a_live_row_that_names_another_machine(self):
        """A live row records only its own machine; the provider's other id is drift.

        `provider.list` names `abc12345` for the slug while the row names
        `srv-old-0001`. The old code "reused" that machine, ran
        `record_release`, and reported `web_row: "skipped"` — a row still
        naming a machine the provider does not have. The refusal keeps the
        decision with `reconcile`, whose `missing_vm` and `unknown_vm` findings
        are exactly this pair.
        """
        config_path = self.make_config()
        self._ready_row(config_path)
        transport = RecordedTransport({("GET", "servers"): [(200, servers_response())]})
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
                "--json",
            ],
            config_path,
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 4, err)
        error = json.loads(out)["error"]
        self.assertIn("srv-old-0001", error)
        self.assertIn(SERVER_ID, error)
        self.assertIn(f"odilectl reconcile {SLUG} --apply", error)
        self.assertEqual([call["action"] for call in transport.calls], ["servers"])

    def test_reconcile_then_provision_records_a_live_rows_replacement(self):
        """The refusal names a path that works, and that path records both rows.

        `ready` -> `reconcile --apply` (marks it destroyed, orders nothing) ->
        `provision --apply` (reopens, orders, `first_boot`, web row repointed).
        The new machine's own boot record is pinned by
        `test_a_rebuilt_tenant_records_its_first_boot_on_the_new_machine`; this
        test proves the remedy in the refusal is not a dead end.
        """
        config_path = self.make_config()
        db = self._ready_row(config_path)
        empty = RecordedTransport(
            {("GET", "servers"): [(200, empty_servers_response())]}
        )
        code, out, err = run_cli(
            ["reconcile", SLUG, "--apply", "--json"],
            config_path,
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=empty),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["fixed"], [SLUG])
        self.assertEqual(payload["drift"][0]["kind"], "missing_vm")
        self.assertIn(f"provision {SLUG}", payload["drift"][0]["detail"]["rebuild"])
        self.assertEqual([call["action"] for call in empty.calls], ["servers"])
        store = state.StateStore.open(db)
        try:
            self.assertEqual(store.get(SLUG).state, "destroyed")
        finally:
            store.close()

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
                "--json",
            ],
            config_path,
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["reopened"])
        self.assertTrue(payload["created"])
        self.assertEqual(payload["web_row"], "repointed")
        self.assertEqual(payload["tenant_state"], "first_boot")
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        row = con.execute(
            "SELECT state, server_id FROM tenants WHERE slug = ?", (SLUG,)
        ).fetchone()
        self.assertEqual((row["state"], row["server_id"]), ("provisioned", SERVER_ID))
        store = state.StateStore.open(db)
        try:
            tenant = store.get(SLUG)
            events = store.events(SLUG)
        finally:
            store.close()
        self.assertEqual(tenant.server_id, SERVER_ID)
        self.assertEqual(
            [event.kind for event in events][-4:],
            ["destroyed", "claimed", "provisioning", "first_boot"],
        )

    def _web_row_for(self, db: Path, slug: str, *, server_id: str | None = None) -> None:
        """One app `tenants` row as the claim path leaves it (no machine yet)."""
        con = sqlite3.connect(db)
        try:
            app.migrate(con)
            con.execute(
                "INSERT INTO tenants (slug, email, state, ssh_public_key, server_id,"
                " claimed_at, created_at) VALUES (?, ?, 'claimed', ?, ?,"
                " '2026-11-23T17:00:00Z', '2026-11-23T17:00:00Z')",
                (slug, f"{slug}@example.com", KEY_TEXT, server_id),
            )
            con.commit()
        finally:
            con.close()

    def test_release_dry_run_names_the_columns_and_writes_nothing(self):
        config_path = self.make_config()
        db = self._ready_row(config_path)
        code, out, err = run_cli(
            ["release", SLUG, "--reason", "handover", "--dry-run", "--json"],
            config_path,
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(
            [action["action"] for action in payload["actions"]],
            ["state.release", "web.release"],
        )
        self.assertEqual(payload["actions"][0]["from"], "ready")
        self.assertEqual(payload["actions"][0]["released"]["server_id"], "srv-old-0001")
        self.assertEqual(payload["actions"][1]["cleared"], ["server_id", "ipv4"])
        self.assertTrue(
            any("not terminated" in note for note in payload["notes"]), payload["notes"]
        )
        store = state.StateStore.open(db)
        try:
            tenant = store.get(SLUG)
        finally:
            store.close()
        self.assertEqual((tenant.state, tenant.server_id), ("ready", "srv-old-0001"))
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        row = con.execute("SELECT server_id FROM tenants WHERE slug = ?", (SLUG,)).fetchone()
        self.assertEqual(row["server_id"], "srv-old-0001", "a dry run writes nothing")

    def test_a_released_machine_records_on_the_next_tenants_row(self):
        """The handover the drill did by hand: free A, then B records the box.

        `tenant_state.server_id` and `tenants.server_id` are UNIQUE, so without
        `release` this provision dies `sqlite3.IntegrityError` after its writes
        have started (the ODL-348 drill, evidence §2).
        """
        config_path = self.make_config()
        db = self._ready_row(config_path)
        other = "odl-348b2c"
        self._web_row_for(db, other)

        code, out, err = run_cli(
            ["release", SLUG, "--reason", f"handover to {other}", "--apply", "--json"],
            config_path,
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertFalse(payload["dry_run"])
        self.assertEqual(payload["state"], "destroyed")
        self.assertEqual(payload["released"]["server_id"], "srv-old-0001")
        self.assertEqual(payload["web_row"], "cleared")

        # A second run converges: nothing moves and it says so.
        again = run_cli(
            ["release", SLUG, "--apply", "--json"],
            config_path,
            provider_factory=explode_provider,
        )
        self.assertEqual(again[0], 0, again[2])
        second = json.loads(again[1])
        self.assertFalse(second["changed"])
        self.assertEqual(second["web_row"], "already clear")

        transport = RecordedTransport(
            {
                ("GET", "servers"): [
                    (200, servers_response(machine_row(id="srv-old-0001", hostname=other)))
                ]
            }
        )
        code, out, err = run_cli(
            [
                "provision",
                other,
                "--key",
                str(self.key_file()),
                "--pin",
                str(self.pin_file()),
                "--interval",
                "0",
                "--apply",
                "--json",
            ],
            config_path,
            provider_factory=lambda _config: servury.ServuryProvider(TEST_KEY, transport=transport),
        )
        self.assertEqual(code, 0, err)
        provision = json.loads(out)
        self.assertFalse(provision["created"])
        self.assertEqual(provision["machine"]["server_id"], "srv-old-0001")
        self.assertEqual(provision["machine"]["hostname"], other)
        self.assertEqual(provision["tenant_state"], "first_boot")
        self.assertEqual(provision["web_row"], "repointed")
        self.assertEqual([call["action"] for call in transport.calls], ["servers"])

        store = state.StateStore.open(db)
        try:
            old = store.get(SLUG)
            new = store.get(other)
        finally:
            store.close()
        self.assertEqual((old.state, old.server_id), ("destroyed", None))
        self.assertEqual(old.detail["released"]["server_id"], "srv-old-0001")
        self.assertEqual((new.state, new.server_id), ("first_boot", "srv-old-0001"))
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        rows = {
            row["slug"]: (row["state"], row["server_id"])
            for row in con.execute("SELECT slug, state, server_id FROM tenants")
        }
        self.assertEqual(rows[SLUG], ("active", None), "A's web row keeps its state, no machine")
        self.assertEqual(rows[other], ("provisioned", "srv-old-0001"))

    def test_release_of_an_unknown_slug_is_a_no_op(self):
        code, out, err = run_cli(
            ["release", "odl-abcdef", "--apply", "--json"],
            self.make_config(),
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertFalse(payload["changed"])
        self.assertEqual(payload["state"], None)
        self.assertEqual(payload["web_row"], "absent")

    def test_provision_dry_run_lists_the_guard_before_create(self):
        """The plan names the refusal between the list and the paid call."""
        config_path = self.make_config()
        self._ready_row(config_path)
        code, out, err = run_cli(
            [
                "provision",
                SLUG,
                "--key",
                str(self.key_file()),
                "--pin",
                str(self.pin_file()),
                "--dry-run",
                "--json",
            ],
            config_path,
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        names = [action["action"] for action in payload["actions"]]
        self.assertLess(names.index("provider.list"), names.index("state.guard"))
        self.assertLess(names.index("state.guard"), names.index("provider.create"))
        guard = payload["actions"][names.index("state.guard")]
        self.assertEqual(guard["slug"], SLUG)
        self.assertIn(f"odilectl reconcile {SLUG} --apply", json.dumps(payload["actions"]))
        self.assertTrue(
            any(f"odilectl reconcile {SLUG} --apply" in note for note in payload["notes"]),
            payload["notes"],
        )

    def test_provision_dry_run_on_a_destroyed_slug_lists_the_reopen(self):
        config_path = self.make_config()
        db = self._destroyed_row(config_path)
        code, out, err = run_cli(
            [
                "provision",
                SLUG,
                "--key",
                str(self.key_file()),
                "--pin",
                str(self.pin_file()),
                "--dry-run",
                "--json",
            ],
            config_path,
            provider_factory=explode_provider,
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["actions"][0]["action"], "state.reopen")
        self.assertEqual(payload["actions"][0]["from"], "destroyed")
        self.assertEqual(payload["actions"][0]["to"], "claimed")
        self.assertTrue(
            any("keep their names" in note for note in payload["notes"]), payload["notes"]
        )
        recorded = state.StateStore.open(db)
        try:
            self.assertEqual(recorded.get(SLUG).state, "destroyed", "a dry run writes nothing")
        finally:
            recorded.close()

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

    def test_status_shows_the_last_brief_from_the_heartbeat(self):
        """A failed brief send is visible here: the time does not advance."""
        config_path = self.make_config()
        db = Path(config_path).parent / "waitlist.db"
        store = state.StateStore.open(db)
        try:
            store.invite(SLUG)
            store.claim(SLUG)
            store.provision(SLUG, server_id=SERVER_ID, release="2026.11.1")
        finally:
            store.close()

        routes = {("GET", "servers"): [(200, servers_response())]}
        code, out, err = run_cli(
            ["status", "--json"],
            config_path,
            provider_factory=lambda _config: fixture_provider(routes),
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(out)["machines"][0]["brief"],
            {"last_sent_at": None, "last_sent_at_iso": None},
        )
        code, out, err = run_cli(
            ["status"],
            config_path,
            provider_factory=lambda _config: fixture_provider(routes),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("brief:     none recorded", out)

        beats = health.HeartbeatStore.open(
            db, clock=lambda: 1_800_000_000, verifier=lambda *_args: True
        )
        try:
            body = json.dumps(
                {
                    "slug": SLUG,
                    "at": 1_800_000_000,
                    "facts": {"last_brief_at": 1_800_000_000},
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
        self.assertEqual(
            json.loads(out)["machines"][0]["brief"]["last_sent_at_iso"],
            "2027-01-15T08:00:00Z",
        )
        code, out, err = run_cli(
            ["status"],
            config_path,
            provider_factory=lambda _config: fixture_provider(routes),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("brief:     last sent 2027-01-15 08:00 UTC", out)

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


class BackupPairRecordTests(TempConfigMixin):
    """ODL-924: the recorded pair, the count and the 51st refusal.

    The end-to-end provisioning run lives in `tests/test_seed.py` beside the
    real seed service; these are the deterministic pieces a stranger can re-run
    without any provider at all.
    """

    def pair(self, slug: str = SLUG) -> provision.BackupPair:
        return provision.BackupPair(
            slug=slug, box_id=709870, box_server="u728180.your-storagebox.de",
            subaccount_id=392550, subaccount_username="u728180-sub1",
            subaccount_server="u728180-sub1.your-storagebox.de",
            subaccount_home="tenants/%s" % slug,
            readonly_subaccount_id=392556, readonly_subaccount_username="u728180-sub2",
            readonly_subaccount_server="u728180-sub2.your-storagebox.de",
            readonly_subaccount_home="tenants/%s/data" % slug,
            repo_url="sftp:u728180-sub1@u728180-sub1.your-storagebox.de:data/restic",
            readonly_repo_url="sftp:u728180-sub2@u728180-sub2.your-storagebox.de:restic",
            known_hosts=box_fixture.HOST_LINE,
            rw_private_key=box_fixture.PRIVATE_KEY,
            ro_private_key=box_fixture.PRIVATE_KEY,
            previous_instance="", provisioned_at="2026-11-25T00:00:00Z",
        )

    def test_the_store_of_record_round_trips_both_keys_at_0600(self) -> None:
        config_path = self.make_config()
        ctx = Context(config=config.Config.load(config_path))
        settings = provision._backup_settings(ctx)
        path = settings.store / ("odile-%s.env" % SLUG)
        provision._write_store(path, self.pair())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        recorded = provision.BackupPair.from_env(path.read_text(encoding="utf-8"))
        self.assertEqual(recorded, self.pair())
        self.assertNotIn("password", path.read_text(encoding="utf-8").lower())

    def test_the_count_is_complete_pairs_and_the_51st_is_refused_by_name(self) -> None:
        accounts = []
        for index in range(50):
            accounts.append({"name": "odile-odl-%06d-rw" % index})
            accounts.append({"name": "odile-odl-%06d-ro" % index})
        self.assertEqual(provision._pair_count(accounts), 50)
        self.assertEqual(provision._pair_count(accounts + [{"name": "odile-odl-half-rw"}]),
                         50)

        config_path = self.make_config()
        ctx = Context(config=config.Config.load(config_path))
        settings = provision._backup_settings(ctx)
        settings.store.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.assertRaises(ProviderError) as caught:
            provision._refuse_the_51st(settings, "odl-51st01", 50)
        self.assertIn("51st", str(caught.exception))
        self.assertIn("odl-51st01", str(caught.exception))
        self.assertIn("100 sub-accounts", str(caught.exception))
        # The store of record alone is enough: no API call is needed to refuse.
        for index in range(49):
            (settings.store / ("odile-odl-%06d.env" % index)).write_text(
                "BACKUP_SLUG=fixture\n", encoding="utf-8")
        provision._refuse_the_51st(settings, "odl-ok0001", None)
        (settings.store / "odile-odl-ok0001.env").write_text("BACKUP_SLUG=fixture\n",
                                                             encoding="utf-8")
        with self.assertRaises(ProviderError):
            provision._refuse_the_51st(settings, "odl-51st02", None)

    def test_the_api_pair_is_checked_for_the_nested_homes_and_the_lock(self) -> None:
        rw = {"id": 392550, "name": "odile-%s-rw" % SLUG,
              "username": "u728180-sub1", "server": "u728180-sub1.your-storagebox.de",
              "home_directory": "tenants/%s" % SLUG,
              "access_settings": {"readonly": False}}
        ro = {"id": 392556, "name": "odile-%s-ro" % SLUG,
              "username": "u728180-sub2", "server": "u728180-sub2.your-storagebox.de",
              "home_directory": "tenants/%s/data" % SLUG,
              "access_settings": {"readonly": True}}
        self.assertEqual(provision._pair_from_api(SLUG, rw, ro)["rw"]["id"], 392550)
        for broken, reason in (
            (dict(ro, home_directory="tenants/%s" % SLUG), "never the same home"),
            (dict(ro, home_directory="tenants/another/data"), "data"),
            (dict(ro, access_settings={"readonly": False}), "not locked"),
            (dict(ro, username="odl-%s" % SLUG), "u<box>-sub<N>"),
        ):
            with self.assertRaisesRegex(ProviderError, reason):
                provision._pair_from_api(SLUG, rw, broken)
        # A writable read-write half would starve the nightly backup.
        with self.assertRaisesRegex(ProviderError, "read-only on the box"):
            provision._pair_from_api(SLUG, dict(rw, access_settings={"readonly": True}), ro)

    def test_half_a_pair_is_refused_and_a_duplicate_name_is_not_guessed(self) -> None:
        only_rw = [{"name": "odile-%s-rw" % SLUG, "id": 392550}]
        with self.assertRaisesRegex(ProviderError, "only the read-write half"):
            provision._named_pair(only_rw, SLUG)
        duplicate = only_rw + [{"name": "odile-%s-rw" % SLUG, "id": 392551},
                               {"name": "odile-%s-ro" % SLUG, "id": 392556}]
        with self.assertRaisesRegex(ProviderError, "refusing to guess"):
            provision._named_pair(duplicate, SLUG)


class BackupUsageTests(TempConfigMixin):
    """ODL-934: the box readback, its two ceilings and the watchdog.

    The Box API is the in-memory fake the provisioning tests use, plus the one
    route this card reads (`GET /storage_boxes`). A command resolves
    `provision._hetzner_api` at call time, so patching it for the duration of
    one test keeps the whole check off the network without touching the module
    under test.
    """

    NOW = 1_800_100_000

    def box(self, *, size_data: int = 2_097_152, size_snapshots: int = 0,
            subaccounts_limit: int = 100, **overrides) -> dict:
        document = {
            "id": 709870,
            "name": "odile-backups",
            "location": {"name": "hel1", "country": "FI", "city": "Helsinki"},
            "system": "HEL1-BX192",
            "stats": {"size": size_data + size_snapshots,
                      "size_data": size_data, "size_snapshots": size_snapshots},
            "storage_box_type": {"name": "bx11", "size": 1_099_511_627_776,
                                 "subaccounts_limit": subaccounts_limit},
        }
        document.update(overrides)
        return document

    def fake_box(self, box: dict | None = None,
                 boxes: list[dict] | None = None) -> box_fixture.FakeBox:
        fake = box_fixture.FakeBox()
        listed = boxes if boxes is not None else [box or self.box()]

        def api(settings, method, path, body=None):
            if method == "GET" and path == "/storage_boxes":
                fake.calls.append((method, path, dict(body or {})))
                return 200, {"storage_boxes": listed}
            return fake.api(settings, method, path, body)

        patcher = mock.patch.object(provision, "_hetzner_api", api)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def token(self, mode: int = 0o600) -> Path:
        path = self._directory / "hetzner-api-token"
        path.write_text("HETZNER_API_TOKEN=fixture-project-token\n", encoding="utf-8")
        path.chmod(mode)
        return path

    def seed_tenants(self, config_path: Path, *, tenants, heartbeats) -> Path:
        """One state row per slug, and one signed heartbeat where facts are given."""
        db = Path(config_path).parent / "waitlist.db"
        store = state.StateStore.open(db)
        try:
            for slug in tenants:
                store.invite(slug)
                store.claim(slug)
        finally:
            store.close()
        beats = health.HeartbeatStore.open(
            db, clock=lambda: self.NOW, verifier=lambda *_args: True
        )
        try:
            for slug, facts in heartbeats.items():
                body = json.dumps(
                    {
                        "slug": slug,
                        "at": self.NOW - 60,
                        "facts": facts,
                        "public_key": "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----",
                    },
                    sort_keys=True,
                ).encode()
                beats.record(body, b"\x00" * 64, now=self.NOW - 60)
        finally:
            beats.close()
        return db

    def record(self, *, size_data: int, size_snapshots: int = 0,
               subaccounts_limit: int = 100) -> dict:
        return backup_usage.collect(
            [],
            {},
            self.box(size_data=size_data, size_snapshots=size_snapshots,
                     subaccounts_limit=subaccounts_limit),
            [],
            now=self.NOW,
            token_path=Path(provision.HETZNER_TOKEN_FILE),
            token_mode="0600",
            api_calls=[],
        )

    def test_the_command_sums_the_tenants_and_names_the_unaccounted_pair(self) -> None:
        """One row per tenant with its own number, the box total, the strangers."""
        config_path = self.make_config()
        token = self.token()
        fake = self.fake_box(self.box(size_data=1_000_000_000, size_snapshots=500_000_000))
        self.seed_tenants(
            config_path,
            tenants=["odl-alpha1", "odl-beta22"],
            heartbeats={
                "odl-alpha1": {
                    "backup": {"repo_bytes": 750_000_000, "status": "ok",
                               "at": self.NOW - 3600}
                }
            },
        )
        fake.add_pair("odl-alpha1")
        fake.add_pair("odl-bench")  # the drill's pair: on the box, no tenant row
        out_path = self._directory / "usage.json"

        code, out, err = run_cli(
            ["backup-usage", "--token", str(token), "--out", str(out_path),
             "--now", str(self.NOW), "--apply"],
            config_path,
        )

        self.assertEqual(code, 0, err)
        self.assertIn("size_data 1000000000 bytes", out)
        self.assertIn("size_snapshots 500000000 bytes", out)
        self.assertIn("total 1500000000 bytes", out)
        self.assertIn("odl-alpha1: 750000000 bytes", out)
        self.assertIn("odl-beta22: no restic number", out)
        self.assertIn("unaccounted: 2 sub-account(s)", out)
        self.assertIn("odile-odl-bench-rw", out)
        self.assertIn("odile-odl-bench-ro", out)
        self.assertIn("sub-accounts: 4 of 100 (2 of 50 tenant pairs)", out)
        self.assertIn("ceiling:   ok", out)

        self.assertEqual(out_path.stat().st_mode & 0o777, 0o600)
        record = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(record["schema"], backup_usage.SCHEMA)
        # The box total is the provider's own two stats, never the tenants' sum.
        self.assertEqual(record["box"]["size_data"], 1_000_000_000)
        self.assertEqual(record["box"]["size_snapshots"], 500_000_000)
        self.assertEqual(record["box"]["total_bytes"], 1_500_000_000)
        self.assertEqual(record["totals"]["tenant_bytes"], 750_000_000)
        self.assertEqual(record["totals"]["tenants_with_a_number"], 1)
        self.assertEqual(record["totals"]["tenants"], 2)
        self.assertEqual(
            [entry["name"] for entry in record["unaccounted"]],
            ["odile-odl-bench-ro", "odile-odl-bench-rw"],
        )
        self.assertEqual(record["box"]["subaccounts"], 4)
        self.assertEqual(record["box"]["subaccounts_limit"], 100)
        self.assertEqual(record["box"]["pair_limit"], 50)
        self.assertEqual(record["credential"]["token_path"], str(token))
        self.assertEqual(record["credential"]["token_mode"], "0600")
        self.assertEqual(
            record["credential"]["api_calls"],
            ["GET /storage_boxes", "GET /storage_boxes/709870/subaccounts"],
        )
        # No new credential and no write to the box: two GETs, nothing else.
        self.assertEqual(
            [(method, path) for method, path, _body in fake.calls],
            [("GET", "/storage_boxes"), ("GET", "/storage_boxes/709870/subaccounts")],
        )
        self.assertEqual(fake.mutating(), [])

    def test_both_ceilings_are_inclusive_and_the_record_carries_the_level(self) -> None:
        warn, critical = backup_usage.WARN_BYTES, backup_usage.CRITICAL_BYTES
        self.assertEqual(warn, 800 * 1024 ** 3)
        self.assertEqual(critical, 950 * 1024 ** 3)
        for total, level in (
            (0, "ok"),
            (warn - 1, "ok"),
            (warn, "warn"),
            (critical - 1, "warn"),
            (critical, "critical"),
            (critical + 1, "critical"),
        ):
            self.assertEqual(backup_usage.level_for(total), level, total)

        config_path = self.make_config()
        token = self.token()
        cases = ((warn, "warn"), (critical, "critical"))
        for index, (total, level) in enumerate(cases):
            self.fake_box(self.box(size_data=total))
            out_path = self._directory / ("usage-%d.json" % index)
            code, out, err = run_cli(
                ["backup-usage", "--token", str(token), "--out", str(out_path),
                 "--now", str(self.NOW), "--apply"],
                config_path,
            )
            self.assertEqual(code, 0, err)
            self.assertIn("ceiling:   %s" % level, out)
            record = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(record["thresholds"]["level"], level)
            self.assertEqual(record["thresholds"]["warn_bytes"], warn)
            self.assertEqual(record["thresholds"]["critical_bytes"], critical)

    def test_the_watchdog_raises_warn_then_critical_then_clears(self) -> None:
        """The existing poll, fed the record: two kinds, so a promotion delivers."""
        config_path = self.make_config()
        db = Path(config_path).parent / "waitlist.db"
        usage_path = self._directory / "usage.json"
        store = state.StateStore.open(db)
        beats = health.HeartbeatStore.open(db)
        try:
            backup_usage.write_record(usage_path, self.record(size_data=800 * 1024 ** 3))
            result = health.check(
                store, beats, now=self.NOW, apply=True, backup_usage_path=usage_path
            )
            warn = beats.open_alert("backup-box-709870", "backup_usage")
            self.assertIsNotNone(warn)
            self.assertIn("the backup box 709870 odile-backups uses", warn.symptom)
            self.assertIn("800.0 GiB warn line reached", warn.symptom)
            self.assertEqual(warn.detail["level"], "warn")
            self.assertEqual(warn.detail["threshold_bytes"], 800 * 1024 ** 3)
            self.assertIsNotNone(result.box_usage)
            self.assertIn("box 709870", "\n".join(health._health_lines(result, applied=True)))

            backup_usage.write_record(usage_path, self.record(size_data=950 * 1024 ** 3))
            health.check(store, beats, now=self.NOW + 300, apply=True,
                         backup_usage_path=usage_path)
            critical = beats.open_alert("backup-box-709870", "backup_usage_critical")
            self.assertIsNotNone(critical)
            self.assertIn("950.0 GiB critical line reached", critical.symptom)
            self.assertIsNone(beats.open_alert("backup-box-709870", "backup_usage"))

            backup_usage.write_record(usage_path, self.record(size_data=1_000))
            health.check(store, beats, now=self.NOW + 600, apply=True,
                         backup_usage_path=usage_path)
            self.assertIsNone(beats.open_alert("backup-box-709870", "backup_usage"))
            self.assertIsNone(beats.open_alert("backup-box-709870", "backup_usage_critical"))
        finally:
            beats.close()
            store.close()

    def test_a_missing_or_broken_record_is_named_not_reported_green(self) -> None:
        config_path = self.make_config()
        db = Path(config_path).parent / "waitlist.db"
        store = state.StateStore.open(db)
        beats = health.HeartbeatStore.open(db)
        try:
            result = health.check(
                store, beats, now=self.NOW, apply=False,
                backup_usage_path=self._directory / "absent.json",
            )
            self.assertIsNone(result.box_usage)
            self.assertIn("no readback at", result.box_usage_problem or "")
            self.assertEqual(result.planned, ())

            broken = self._directory / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            self.assertEqual(
                backup_usage.read_record(broken)[0], None
            )
            self.assertIn("is not JSON", backup_usage.read_record(broken)[1] or "")
        finally:
            beats.close()
            store.close()

    def test_an_old_alert_ledger_is_migrated_and_keeps_its_rows(self) -> None:
        """A live database cut before this card still accepts the new kinds."""
        self.make_config()
        db = self._directory / "old-heartbeats.db"
        connection = sqlite3.connect(str(db))
        try:
            connection.executescript(
                """
                CREATE TABLE health_alerts (
                    id            INTEGER PRIMARY KEY,
                    slug          TEXT    NOT NULL,
                    kind          TEXT    NOT NULL
                                  CHECK (kind IN ('down', 'waiting_for_owner',
                                                  'brief_missing', 'disk', 'spend',
                                                  'backup')),
                    tenant        TEXT    NOT NULL DEFAULT '',
                    symptom       TEXT    NOT NULL,
                    first_command TEXT    NOT NULL,
                    detail        TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(detail)),
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at  INTEGER NOT NULL,
                    resolved_at   INTEGER,
                    deliveries    TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(deliveries))
                );
                CREATE UNIQUE INDEX health_alerts_open
                    ON health_alerts(slug, kind) WHERE resolved_at IS NULL;
                INSERT INTO health_alerts (id, slug, kind, symptom, first_command,
                                           first_seen_at, last_seen_at)
                    VALUES (17, 'odl-old001', 'backup', 'last backup 2d 0h ago',
                            'cat /var/lib/odile/backup.json', 1, 1);
                """
            )
            connection.commit()
        finally:
            connection.close()

        beats = health.HeartbeatStore.open(db)
        try:
            kept = beats.open_alert("odl-old001", "backup")
            self.assertIsNotNone(kept)
            self.assertEqual(kept.id, 17)
            record, created = beats.raise_alert(
                health.Alert(
                    slug="backup-box-709870", kind="backup_usage", tenant="",
                    symptom="the backup box uses 800.0 GiB", first_command="backup-usage",
                    at=self.NOW,
                ),
                self.NOW,
            )
            self.assertTrue(created)
            self.assertEqual(record.kind, "backup_usage")
            schema = beats.connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'health_alerts'"
            ).fetchone()[0]
            self.assertIn("backup_usage_critical", schema)
            indexes = {
                row[0] for row in beats.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                    " AND tbl_name = 'health_alerts'"
                )
            }
            self.assertIn("health_alerts_open", indexes)
            self.assertIn("health_alerts_slug", indexes)
            self.assertIsNone(
                beats.connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'health_alerts_new'"
                ).fetchone()
            )
        finally:
            beats.close()

        # A second open is a true no-op: same rows, no error, no second rebuild.
        again = health.HeartbeatStore.open(db)
        try:
            self.assertEqual(len(again.alerts(open_only=False)), 2)
            self.assertIsNotNone(again.open_alert("odl-old001", "backup"))
        finally:
            again.close()

    def test_an_interrupted_rebuild_leaves_the_copy_readable(self) -> None:
        """Only `health_alerts_new` present: the rows come back, not an empty table."""
        self.make_config()
        db = self._directory / "interrupted.db"
        connection = sqlite3.connect(str(db))
        try:
            connection.executescript(
                """
                CREATE TABLE health_alerts_new (
                    id            INTEGER PRIMARY KEY,
                    slug          TEXT    NOT NULL,
                    kind          TEXT    NOT NULL,
                    tenant        TEXT    NOT NULL DEFAULT '',
                    symptom       TEXT    NOT NULL,
                    first_command TEXT    NOT NULL,
                    detail        TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(detail)),
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at  INTEGER NOT NULL,
                    resolved_at   INTEGER,
                    deliveries    TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(deliveries))
                );
                INSERT INTO health_alerts_new (id, slug, kind, symptom, first_command,
                                               first_seen_at, last_seen_at)
                    VALUES (23, 'odl-old002', 'down', 'heartbeat stopped', 'status', 1, 1);
                """
            )
            connection.commit()
        finally:
            connection.close()

        beats = health.HeartbeatStore.open(db)
        try:
            kept = beats.open_alert("odl-old002", "down")
            self.assertIsNotNone(kept)
            self.assertEqual(kept.id, 23)
            schema = beats.connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'health_alerts'"
            ).fetchone()[0]
            self.assertIn("backup_usage", schema)
        finally:
            beats.close()

    def test_the_only_credential_is_the_project_token(self) -> None:
        """No account-wide key, no main-account password, no per-tenant call."""
        config_path = self.make_config()
        ctx = Context(config=config.Config.load(config_path))
        settings = backup_usage._settings(
            ctx, mock.Mock(token=None, box_id=None, api_base=None)
        )
        self.assertEqual(settings.token, Path(provision.HETZNER_TOKEN_FILE))
        self.assertEqual(settings.token, Path("/root/.odile/hetzner-api-token"))

        source = (PACKAGE / "backup_usage.py").read_text(encoding="utf-8").lower()
        for forbidden in ("hetzner_login", "hetzner-account.env", "hetzner_password",
                          "r2_token", "bucket_token", "aws_access"):
            self.assertNotIn(forbidden, source)
        # The module's one mention of a password is the docstring's promise
        # that there is none; a password argument or file read adds a second.
        self.assertEqual(source.count("password"), 1)
        self.assertNotIn("/subaccounts/", source)

        fake = self.fake_box()
        fake.add_pair("odl-alpha1")
        code, out, err = run_cli(
            ["backup-usage", "--token", str(self.token()), "--now", str(self.NOW),
             "--json", "--apply", "--out", str(self._directory / "usage.json")],
            config_path,
        )
        self.assertEqual(code, 0, err)
        calls = [(method, path) for method, path, _body in fake.calls]
        self.assertEqual(
            calls, [("GET", "/storage_boxes"), ("GET", "/storage_boxes/709870/subaccounts")]
        )
        self.assertFalse([path for _method, path in calls if re.search(r"/subaccounts/\d", path)])
        self.assertEqual(fake.mutating(), [])

    def test_the_operator_poll_reads_the_record_and_opens_the_alert(self) -> None:
        """The wiring the DoD names: `odilectl health`, not `check()` directly.

        A build where `health.run` forgets `--backup-usage` still passes every
        unit test that calls `check(..., backup_usage_path=...)`; this one runs
        the command the operator runs and reads the ledger afterwards.
        """
        config_path = self.make_config()
        db = Path(config_path).parent / "waitlist.db"
        usage_path = self._directory / "usage.json"
        outbox = self._directory / "outbox"
        backup_usage.write_record(usage_path, self.record(size_data=950 * 1024 ** 3))

        code, out, err = run_cli(
            ["health", "--db", str(db), "--backup-usage", str(usage_path),
             "--no-http", "--outbox", str(outbox), "--now", str(self.NOW), "--apply"],
            config_path,
        )
        self.assertEqual(code, 0, err)
        self.assertIn("alert backup_usage_critical backup-box-709870 open", out)
        self.assertIn("950.0 GiB critical line reached", out)
        self.assertIn("box 709870 odile-backups: 950.0 GiB used (92.8%)", out)

        store = state.StateStore.open(db)
        try:
            self.assertIsNotNone(store)
        finally:
            store.close()
        beats = health.HeartbeatStore.open(db)
        try:
            self.assertIsNotNone(
                beats.open_alert("backup-box-709870", "backup_usage_critical")
            )
        finally:
            beats.close()
        self.assertEqual(backup_usage.DEFAULT_USAGE_PATH, "/var/lib/odile/backup/usage.json")

    def test_two_boxes_with_one_id_and_a_box_without_stats_are_refused(self) -> None:
        config_path = self.make_config()
        token = self.token()
        duplicate = self.box()
        self.fake_box(boxes=[duplicate, dict(duplicate, name="another")])
        code, out, err = run_cli(
            ["backup-usage", "--token", str(token), "--now", str(self.NOW),
             "--out", str(self._directory / "usage.json"), "--apply"],
            config_path,
        )
        self.assertEqual(code, 4, out + err)
        self.assertIn("2 box(es) with id 709870", err)

        self.fake_box(self.box(stats={"size": 0}))
        code, out, err = run_cli(
            ["backup-usage", "--token", str(token), "--now", str(self.NOW),
             "--out", str(self._directory / "usage.json"), "--apply"],
            config_path,
        )
        self.assertEqual(code, 4, out + err)
        self.assertIn("stats.size_data", err)

    def test_an_http_api_base_off_loopback_is_refused(self) -> None:
        """The bearer token must not cross a clear-text hop."""
        config_path = self.make_config()
        self.fake_box()
        code, out, err = run_cli(
            ["backup-usage", "--token", str(self.token()), "--api-base",
             "http://api.example.invalid/v1", "--now", str(self.NOW),
             "--out", str(self._directory / "usage.json"), "--apply"],
            config_path,
        )
        self.assertEqual(code, 5, out + err)
        self.assertIn("clear text", err)

    def test_a_schema_valid_record_without_a_box_id_is_named(self) -> None:
        self.make_config()
        path = self._directory / "usage.json"
        record = self.record(size_data=2048)
        del record["box"]["id"]
        backup_usage.write_record(path, record)
        self.assertEqual(backup_usage.read_record(path)[0], None)
        self.assertIn("box.id", backup_usage.read_record(path)[1] or "")
        lines = health._health_lines(
            health.HealthResult(
                now=self.NOW, tenants=(), alerts=(), planned=(), resolved=(),
                applied=False, box_usage=None,
                box_usage_problem=backup_usage.read_record(path)[1],
            ),
            applied=False,
        )
        self.assertIn("backup usage: ", "\n".join(lines))

    def test_a_loose_token_is_refused_before_the_first_call(self) -> None:
        config_path = self.make_config()
        fake = self.fake_box()
        code, out, err = run_cli(
            ["backup-usage", "--token", str(self.token(0o644)), "--now", str(self.NOW),
             "--out", str(self._directory / "usage.json"), "--apply"],
            config_path,
        )
        self.assertEqual(code, 5, out + err)
        self.assertIn("must be 0600", err)
        self.assertEqual(fake.calls, [])

    def test_the_record_is_atomic_and_a_dry_run_writes_nothing(self) -> None:
        config_path = self.make_config()
        token = self.token()
        self.fake_box()
        out_path = self._directory / "usage.json"

        code, out, err = run_cli(
            ["backup-usage", "--token", str(token), "--now", str(self.NOW),
             "--out", str(out_path)],
            config_path,
        )
        self.assertEqual(code, 0, err)
        self.assertFalse(out_path.exists())
        self.assertIn("would write; pass --apply", out)

        out_path.write_text("{}", encoding="utf-8")
        out_path.chmod(0o644)
        backup_usage.write_record(out_path, self.record(size_data=2048))
        self.assertEqual(out_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(out_path.read_text(encoding="utf-8"))["box"]["total_bytes"], 2048)
        left = sorted(path.name for path in self._directory.iterdir()
                      if path.name.startswith(".usage-"))
        self.assertEqual(left, [])


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
        # Three deliberate exceptions, all for local binaries and none an
        # operator shell on a tenant: ODL-215's `restore --apply` runs `restic`
        # on the machine that holds the user's recovery file (the user's
        # laptop, or a rebuilt guest); the ODL-218 follow-up's `purge` runs it
        # on the backup box against the repository it is about to delete; and
        # ODL-924's `provision` runs `ssh-keygen`, `ssh-keyscan` and the
        # documented `install-ssh-key` over port 23 on the Storage Box, which
        # has no tenant address and no shell (a restricted command interface).
        # Every other escape hatch stays banned in these files too, so a later
        # `os.system` cannot hide behind the exception.
        local_binary_exceptions = {PACKAGE / "restore.py", PACKAGE / "purge.py",
                                   PACKAGE / "provision.py"}
        for path in self._package_files():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for token in forbidden:
                    if path in local_binary_exceptions and token == "subprocess":
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
