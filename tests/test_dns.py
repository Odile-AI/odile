#!/usr/bin/python3
"""Tests for `odilectl.dns` — the tenant's A record, hermetic.

No socket, no live API. Cloudflare is a recorded transport keyed by
`(method, path)`; the zone id, record ids and token are fixtures. The live leg
(the `_odile-test` record on odile.fyi) is the card's evidence, not a test.

    /usr/bin/python3 -m unittest discover -s tests -p 'test_dns.py' -v
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odilectl import dns  # noqa: E402
from odilectl.config import Config  # noqa: E402
from odilectl.core import Context  # noqa: E402
from odilectl.errors import ConfigError, ProviderError, UsageError  # noqa: E402

ZONE = "odile.fyi"
ZONE_ID = "fixture-zone-id-not-real"
TOKEN = "fixture-dns-token-not-a-secret"
NAME = "_odile-test.odile.fyi"
SLUG = "_odile-test"
TENANT = "odl-7f3a2c"
IP = "203.0.113.10"   # RFC 5737 TEST-NET-3: no live host is named in a test


# --------------------------------------------------------------- recorded fixtures


def api_ok(result):
    return {"success": True, "result": result, "errors": []}


def api_error(message="Record does not exist.", code=81044):
    return {"success": False, "result": None, "errors": [{"code": code, "message": message}]}


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


ZONE_ROW = {"id": ZONE_ID, "name": ZONE}


class RecordedTransport:
    """Replays recorded Cloudflare answers in order; a missing route is a bug."""

    def __init__(self, routes):
        self.routes = {key: list(value) for key, value in routes.items()}
        self.calls = []

    def __call__(self, method, url, headers, body):
        parts = urllib.parse.urlsplit(url)
        # Fixtures name the API path without the /client/v4 prefix.
        path = parts.path
        if path.startswith("/client/v4"):
            path = path[len("/client/v4"):]
        self.calls.append(
            {
                "method": method,
                "url": url,
                "path": path,
                "query": urllib.parse.parse_qs(parts.query),
                "headers": dict(headers),
                "body": json.loads(body) if body else None,
            }
        )
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"no recorded response for {method} {path}")
        status, payload = queue.pop(0)
        if isinstance(payload, bytes):
            return status, payload
        return status, json.dumps(payload).encode("utf-8")

    def writes(self):
        return [call for call in self.calls if call["method"] in ("POST", "PATCH", "DELETE")]


def zone_routes(*record_queues):
    """The zone lookup each fresh client makes, plus the record queues.

    A test that runs two commands builds a fresh client each time, so the
    lookup is answered once per operation; an unused queued answer is harmless.
    """
    zone_answers = [(200, api_ok([ZONE_ROW])) for _ in record_queues] or [
        (200, api_ok([ZONE_ROW]))
    ]
    return {
        ("GET", "/zones"): zone_answers,
        ("GET", f"/zones/{ZONE_ID}/dns_records"): [
            response for queue in record_queues for response in queue
        ],
    }


class DnsBase(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="odile-dns-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.token_file = self.directory / "cloudflare_dns_token"
        self.token_file.write_text(TOKEN + "\n", encoding="utf-8")
        self.token_file.chmod(0o600)
        self.config_path = self.directory / "config.yaml"
        self.write_config(dns_token=TOKEN)

    def write_config(self, **overrides):
        values = {
            "dns_token": TOKEN,
            "dns_token_file": None,
            "dns_zone": None,
            "dns_zone_id": None,
        }
        values.update(overrides)
        text = "".join(f"{key}: {value}\n" for key, value in values.items() if value is not None)
        self.config_path.write_text(text, encoding="utf-8")
        self.config_path.chmod(0o600)
        return self.config_path

    def context(self, *, apply=True, dry_run=False):
        return Context(
            config=Config.load(self.config_path),
            dry_run=dry_run,
            apply=apply,
        )

    def args(self, *argv):
        return dns.build_parser().parse_args(list(argv))

    def client(self, routes):
        transport = RecordedTransport(routes)
        return dns.CloudflareDNS(TOKEN, ZONE, transport=transport), transport


# ------------------------------------------------------------------- slugs and names


class SlugTest(unittest.TestCase):
    def test_a_new_slug_is_odl_plus_six_hex_characters(self):
        for _ in range(100):
            slug = dns.new_slug()
            self.assertRegex(slug, r"^odl-[0-9a-f]{6}$")

    def test_new_slugs_are_random(self):
        slugs = {dns.new_slug() for _ in range(100)}
        self.assertGreater(len(slugs), 90)

    def test_a_tenant_slug_is_not_a_person_name(self):
        for slug in ("ada-lovelace", "savio", "odl-7f3a2", "odl-7f3a2c9", "odl-zzzzzz"):
            with self.subTest(slug=slug):
                with self.assertRaises(ValueError):
                    dns.record_name(slug, ZONE)

    def test_the_live_test_label_is_allowed(self):
        self.assertEqual(dns.record_name(SLUG, ZONE), NAME)

    def test_a_slug_cannot_carry_a_second_label(self):
        with self.assertRaises(ValueError):
            dns.record_name("odl-7f3a2c.evil.fyi", ZONE)

    def test_a_slug_is_folded_to_lower_case(self):
        self.assertEqual(dns.record_name("ODL-7F3A2C", ZONE), "odl-7f3a2c.odile.fyi")

    def test_a_bad_zone_is_refused(self):
        for zone in ("", "odile", ".odile.fyi", "odile.fyi.", "od ile.fyi"):
            with self.subTest(zone=zone):
                with self.assertRaises(ValueError):
                    dns.record_name(TENANT, zone)

    def test_an_ipv6_address_is_not_an_a_record(self):
        with self.assertRaises(ValueError):
            dns.validate_ipv4("2001:0db8:0:2c::1")
        self.assertEqual(dns.validate_ipv4("203.0.113.10"), "203.0.113.10")


# ------------------------------------------------------------------- the API client


class CloudflareRecordTest(DnsBase):
    def test_create_posts_a_dns_only_record_with_the_provisioning_ttl(self):
        client, transport = self.client(
            {
                **zone_routes([(200, api_ok([]))]),
                ("POST", f"/zones/{ZONE_ID}/dns_records"): [(200, api_ok(record_row()))],
            }
        )

        change = client.ensure(NAME, IP)

        self.assertEqual(change.action, "created")
        assert change.record is not None
        self.assertEqual(change.record.id, "rec123")
        self.assertEqual(change.record.proxied, False)
        writes = transport.writes()
        self.assertEqual([call["method"] for call in writes], ["POST"])
        self.assertEqual(
            writes[0]["body"],
            {"type": "A", "name": NAME, "content": IP, "proxied": False, "ttl": 120},
        )
        self.assertEqual(writes[0]["headers"]["Authorization"], f"Bearer {TOKEN}")

    def test_create_without_apply_only_reads_and_reports_the_plan(self):
        client, transport = self.client(zone_routes([(200, api_ok([]))]))

        change = client.ensure(NAME, IP, apply=False)

        self.assertEqual(change.action, "created")
        self.assertIsNone(change.record)
        self.assertEqual(transport.writes(), [])

    def test_an_identical_record_is_a_no_op(self):
        client, transport = self.client(zone_routes([(200, api_ok([record_row()]))]))

        change = client.ensure(NAME, IP)

        self.assertEqual(change.action, "same")
        assert change.record is not None
        self.assertEqual(change.record.id, "rec123")
        self.assertEqual(transport.writes(), [])

    def test_a_changed_address_is_patched_in_place(self):
        old = record_row(content="203.0.113.120")
        client, transport = self.client(
            {
                **zone_routes([(200, api_ok([old]))]),
                ("PATCH", f"/zones/{ZONE_ID}/dns_records/rec123"): [
                    (200, api_ok(record_row()))
                ],
            }
        )

        change = client.ensure(NAME, IP)

        self.assertEqual(change.action, "updated")
        assert change.previous is not None and change.record is not None
        self.assertEqual(change.previous.content, "203.0.113.120")
        self.assertEqual(change.record.content, IP)
        writes = transport.writes()
        self.assertEqual([call["method"] for call in writes], ["PATCH"])
        self.assertEqual(writes[0]["body"]["content"], IP)
        self.assertFalse(writes[0]["body"]["proxied"])

    def test_a_proxied_record_is_converged_back_to_dns_only(self):
        proxied = record_row(proxied=True)
        client, transport = self.client(
            {
                **zone_routes([(200, api_ok([proxied]))]),
                ("PATCH", f"/zones/{ZONE_ID}/dns_records/rec123"): [
                    (200, api_ok(record_row()))
                ],
            }
        )

        change = client.ensure(NAME, IP)

        self.assertEqual(change.action, "updated")
        self.assertFalse(transport.writes()[0]["body"]["proxied"])
        assert change.record is not None
        self.assertFalse(change.record.proxied)

    def test_two_records_with_one_name_are_refused_before_any_write(self):
        rows = [record_row(id="rec123"), record_row(id="rec456")]
        client, transport = self.client(zone_routes([(200, api_ok(rows))]))

        with self.assertRaises(ProviderError) as caught:
            client.ensure(NAME, IP)

        self.assertIn("2", str(caught.exception))
        self.assertIn(NAME, str(caught.exception))
        self.assertEqual(transport.writes(), [])

    def test_settle_moves_the_ttl_to_the_settled_value(self):
        provisioned = record_row(ttl=dns.PROVISION_TTL)
        settled = record_row(ttl=dns.SETTLED_TTL)
        client, transport = self.client(
            {
                **zone_routes([(200, api_ok([provisioned]))]),
                ("PATCH", f"/zones/{ZONE_ID}/dns_records/rec123"): [(200, api_ok(settled))],
            }
        )

        change = client.settle(NAME)

        self.assertEqual(change.action, "updated")
        assert change.record is not None
        self.assertEqual(change.record.ttl, 300)
        self.assertEqual(transport.writes()[0]["body"]["ttl"], 300)
        self.assertEqual(transport.writes()[0]["body"]["content"], IP)
        self.assertFalse(transport.writes()[0]["body"]["proxied"])

    def test_settle_on_a_settled_record_is_a_no_op(self):
        client, transport = self.client(
            zone_routes([(200, api_ok([record_row(ttl=dns.SETTLED_TTL)]))])
        )

        change = client.settle(NAME)

        self.assertEqual(change.action, "same")
        self.assertEqual(transport.writes(), [])

    def test_settle_on_a_missing_record_is_an_error(self):
        client, transport = self.client(zone_routes([(200, api_ok([]))]))

        with self.assertRaises(ProviderError):
            client.settle(NAME)
        self.assertEqual(transport.writes(), [])

    def test_remove_sends_one_delete(self):
        client, transport = self.client(
            {
                **zone_routes([(200, api_ok([record_row()]))]),
                ("DELETE", f"/zones/{ZONE_ID}/dns_records/rec123"): [
                    (200, api_ok({"id": "rec123"}))
                ],
            }
        )

        change = client.remove(NAME)

        self.assertEqual(change.action, "deleted")
        assert change.record is not None
        self.assertEqual(change.record.id, "rec123")
        self.assertEqual([call["method"] for call in transport.writes()], ["DELETE"])

    def test_remove_of_an_absent_record_is_a_no_op(self):
        client, transport = self.client(zone_routes([(200, api_ok([]))]))

        change = client.remove(NAME)

        self.assertEqual(change.action, "absent")
        self.assertIsNone(change.record)
        self.assertEqual(transport.writes(), [])

    def test_remove_treats_a_404_as_already_gone(self):
        client, transport = self.client(
            {
                **zone_routes([(200, api_ok([record_row()]))]),
                ("DELETE", f"/zones/{ZONE_ID}/dns_records/rec123"): [
                    (404, api_error())
                ],
            }
        )

        change = client.remove(NAME)

        self.assertEqual(change.action, "absent")

    def test_remove_does_not_swallow_a_server_error(self):
        client, _ = self.client(
            {
                **zone_routes([(200, api_ok([record_row()]))]),
                ("DELETE", f"/zones/{ZONE_ID}/dns_records/rec123"): [
                    (500, api_error("internal error", code=1000))
                ],
            }
        )

        with self.assertRaises(ProviderError) as caught:
            client.remove(NAME)
        self.assertEqual(caught.exception.status, 500)

    def test_the_token_is_in_the_header_and_never_in_a_url_or_body(self):
        client, transport = self.client(
            {
                **zone_routes([(200, api_ok([]))]),
                ("POST", f"/zones/{ZONE_ID}/dns_records"): [(200, api_ok(record_row()))],
            }
        )

        client.ensure(NAME, IP)

        for call in transport.calls:
            self.assertNotIn(TOKEN, call["url"])
            self.assertNotIn(TOKEN, json.dumps(call["body"]))
            self.assertEqual(call["headers"]["Authorization"], f"Bearer {TOKEN}")

    def test_the_zone_is_looked_up_once_and_cached(self):
        client, transport = self.client(
            {
                **zone_routes([(200, api_ok([record_row()])), (200, api_ok([record_row()]))]),
                ("DELETE", f"/zones/{ZONE_ID}/dns_records/rec123"): [
                    (200, api_ok({"id": "rec123"}))
                ],
            }
        )

        client.ensure(NAME, IP)
        client.remove(NAME)

        zone_calls = [call for call in transport.calls if call["path"] == "/zones"]
        self.assertEqual(len(zone_calls), 1)

    def test_an_explicit_zone_id_skips_the_zone_lookup(self):
        transport = RecordedTransport(
            {
                ("GET", f"/zones/{ZONE_ID}/dns_records"): [(200, api_ok([record_row()]))],
            }
        )
        client = dns.CloudflareDNS(TOKEN, ZONE, zone_id=ZONE_ID, transport=transport)

        change = client.ensure(NAME, IP)

        self.assertEqual(change.action, "same")
        self.assertNotIn("/zones", [call["path"] for call in transport.calls])

    def test_an_unknown_zone_is_an_error(self):
        transport = RecordedTransport({("GET", "/zones"): [(200, api_ok([]))]})
        client = dns.CloudflareDNS(TOKEN, ZONE, transport=transport)

        with self.assertRaises(ProviderError) as caught:
            client.find(NAME)
        self.assertIn(ZONE, str(caught.exception))

    def test_two_zones_with_one_name_are_an_error(self):
        transport = RecordedTransport(
            {("GET", "/zones"): [(200, api_ok([ZONE_ROW, dict(ZONE_ROW, id="other")]))]}
        )
        client = dns.CloudflareDNS(TOKEN, ZONE, transport=transport)

        with self.assertRaises(ProviderError):
            client.find(NAME)

    def test_a_rejected_write_carries_the_status_and_the_api_message(self):
        client, _ = self.client(
            {
                **zone_routes([(200, api_ok([]))]),
                ("POST", f"/zones/{ZONE_ID}/dns_records"): [
                    (403, api_error("Authentication error", code=10000))
                ],
            }
        )

        with self.assertRaises(ProviderError) as caught:
            client.ensure(NAME, IP)

        message = str(caught.exception)
        self.assertEqual(caught.exception.status, 403)
        self.assertIn("Authentication error", message)
        self.assertNotIn(TOKEN, message)

    def test_a_non_json_body_is_a_provider_error(self):
        transport = RecordedTransport(
            {
                ("GET", "/zones"): [(502, b"<html>bad gateway</html>")],
            }
        )
        client = dns.CloudflareDNS(TOKEN, ZONE, transport=transport)

        with self.assertRaises(ProviderError) as caught:
            client.find(NAME)
        self.assertEqual(caught.exception.status, 502)

    def test_success_false_on_http_200_is_an_error(self):
        transport = RecordedTransport(
            {("GET", "/zones"): [(200, api_error("zone rejected"))]}
        )
        client = dns.CloudflareDNS(TOKEN, ZONE, transport=transport)

        with self.assertRaises(ProviderError):
            client.find(NAME)


# ------------------------------------------------------------------- the command


class DnsCommandTest(DnsBase):
    def test_create_apply_reports_the_record(self):
        transport = RecordedTransport(
            {
                **zone_routes([(200, api_ok([]))]),
                ("POST", f"/zones/{ZONE_ID}/dns_records"): [(200, api_ok(record_row()))],
            }
        )
        report = dns.run(
            self.context(),
            self.args("create", SLUG, "--ip", IP),
            transport=transport,
        )

        self.assertTrue(report.payload["ok"])
        self.assertFalse(report.payload["dry_run"])
        self.assertEqual(report.payload["action"], "created")
        self.assertEqual(report.payload["name"], NAME)
        self.assertFalse(report.payload["proxied"])
        self.assertEqual(report.payload["ttl"], 120)
        self.assertEqual(report.payload["record_id"], "rec123")
        self.assertNotIn(TOKEN, "\n".join(report.lines))
        self.assertEqual(report.lines[0], "dns: applied")

    def test_create_without_apply_is_a_dry_run_that_writes_nothing(self):
        transport = RecordedTransport(zone_routes([(200, api_ok([]))]))
        report = dns.run(
            self.context(apply=False),
            self.args("create", SLUG, "--ip", IP),
            transport=transport,
        )

        self.assertTrue(report.payload["dry_run"])
        self.assertEqual(report.payload["action"], "created")
        self.assertEqual(report.payload["actions"][0]["action"], "dns.created")
        self.assertEqual(transport.writes(), [])
        self.assertIn("dry run", report.lines[0])

    def test_dry_run_wins_over_apply(self):
        transport = RecordedTransport(zone_routes([(200, api_ok([]))]))
        report = dns.run(
            self.context(apply=True, dry_run=True),
            self.args("create", SLUG, "--ip", IP),
            transport=transport,
        )

        self.assertTrue(report.payload["dry_run"])
        self.assertEqual(transport.writes(), [])

    def test_create_requires_an_ipv4(self):
        with self.assertRaises(UsageError):
            dns.run(self.context(), self.args("create", SLUG), transport=RecordedTransport({}))
        with self.assertRaises(UsageError):
            dns.run(
                self.context(),
                self.args("create", SLUG, "--ip", "2001:0db8::1"),
                transport=RecordedTransport({}),
            )

    def test_create_refuses_a_ttl_cloudflare_would_reject(self):
        with self.assertRaises(UsageError):
            dns.run(
                self.context(),
                self.args("create", SLUG, "--ip", IP, "--ttl", "30"),
                transport=RecordedTransport({}),
            )

    def test_create_without_apply_is_safe_with_no_token(self):
        self.write_config(dns_token=None)
        transport = RecordedTransport(zone_routes([(200, api_ok([]))]))

        with self.assertRaises(ConfigError):
            dns.run(
                self.context(apply=False),
                self.args("create", SLUG, "--ip", IP, "--token-file", str(self.directory / "nope")),
                transport=transport,
            )
        self.assertEqual(transport.writes(), [])

    def test_delete_apply_twice_removes_once_then_is_a_no_op(self):
        transport = RecordedTransport(
            {
                **zone_routes(
                    [(200, api_ok([record_row()]))],
                    [(200, api_ok([]))],
                ),
                ("DELETE", f"/zones/{ZONE_ID}/dns_records/rec123"): [
                    (200, api_ok({"id": "rec123"}))
                ],
            }
        )
        context = self.context()

        first = dns.run(context, self.args("delete", SLUG), transport=transport)
        second = dns.run(context, self.args("delete", SLUG), transport=transport)

        self.assertEqual(first.payload["action"], "deleted")
        self.assertEqual(second.payload["action"], "absent")
        self.assertTrue(second.payload["ok"])
        deletes = [call for call in transport.calls if call["method"] == "DELETE"]
        self.assertEqual(len(deletes), 1)

    def test_delete_without_apply_writes_nothing(self):
        transport = RecordedTransport(zone_routes([(200, api_ok([record_row()]))]))

        report = dns.run(
            self.context(apply=False), self.args("delete", SLUG), transport=transport
        )

        self.assertTrue(report.payload["dry_run"])
        self.assertEqual(report.payload["action"], "deleted")
        self.assertEqual(transport.writes(), [])

    def test_settle_moves_the_record_and_reports_it(self):
        settled = record_row(ttl=dns.SETTLED_TTL)
        transport = RecordedTransport(
            {
                **zone_routes([(200, api_ok([record_row(ttl=120)]))]),
                ("PATCH", f"/zones/{ZONE_ID}/dns_records/rec123"): [(200, api_ok(settled))],
            }
        )

        report = dns.run(self.context(), self.args("settle", SLUG), transport=transport)

        self.assertEqual(report.payload["action"], "updated")
        self.assertEqual(report.payload["ttl"], 300)

    def test_a_missing_token_is_a_config_error(self):
        self.write_config(dns_token=None, dns_token_file=str(self.directory / "absent"))

        with self.assertRaises(ConfigError) as caught:
            dns.run(
                self.context(), self.args("delete", SLUG), transport=RecordedTransport({})
            )
        self.assertIn("absent", str(caught.exception))

    def test_a_world_readable_token_file_is_refused(self):
        self.write_config(dns_token=None, dns_token_file=str(self.token_file))
        self.token_file.chmod(0o644)

        with self.assertRaises(ConfigError) as caught:
            dns.run(
                self.context(), self.args("delete", SLUG), transport=RecordedTransport({})
            )
        self.assertIn("0600", str(caught.exception))

    def test_the_token_file_is_used_when_the_config_has_no_token(self):
        self.write_config(dns_token=None, dns_token_file=str(self.token_file))
        transport = RecordedTransport(zone_routes([(200, api_ok([]))]))

        dns.run(self.context(apply=False), self.args("delete", SLUG), transport=transport)

        self.assertEqual(transport.calls[0]["headers"]["Authorization"], f"Bearer {TOKEN}")

    def test_an_explicit_token_file_wins_over_the_config(self):
        other = self.directory / "other-token"
        other.write_text("other-token\n", encoding="utf-8")
        other.chmod(0o600)
        transport = RecordedTransport(zone_routes([(200, api_ok([]))]))

        dns.run(
            self.context(apply=False),
            self.args("delete", SLUG, "--token-file", str(other)),
            transport=transport,
        )

        self.assertEqual(transport.calls[0]["headers"]["Authorization"], "Bearer other-token")

    def test_main_prints_one_json_object(self):
        out, err = io.StringIO(), io.StringIO()
        transport = RecordedTransport(
            {
                **zone_routes([(200, api_ok([]))]),
                ("POST", f"/zones/{ZONE_ID}/dns_records"): [(200, api_ok(record_row()))],
            }
        )

        code = dns.main(
            [
                "create",
                SLUG,
                "--ip",
                IP,
                "--apply",
                "--json",
                "--config",
                str(self.config_path),
            ],
            transport=transport,
            stdout=out,
            stderr=err,
        )

        self.assertEqual(code, 0, err.getvalue())
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "created")
        self.assertEqual(payload["command"], "dns")

    def test_main_returns_a_usage_error_code_without_ip(self):
        out, err = io.StringIO(), io.StringIO()

        code = dns.main(
            ["create", SLUG, "--json", "--config", str(self.config_path)],
            transport=RecordedTransport({}),
            stdout=out,
            stderr=err,
        )

        self.assertEqual(code, 2)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn("--ip", payload["error"])

    def test_main_returns_the_config_exit_code(self):
        out, err = io.StringIO(), io.StringIO()

        code = dns.main(
            ["delete", SLUG, "--json", "--config", str(self.directory / "absent.yaml")],
            transport=RecordedTransport({}),
            stdout=out,
            stderr=err,
        )

        self.assertEqual(code, 5)
        self.assertFalse(json.loads(out.getvalue())["ok"])

    def test_main_can_run_with_no_config_file_when_the_default_token_file_exists(self):
        """The HQ box has a token but not necessarily a config; the default works."""
        original = dns.DEFAULT_TOKEN_FILE
        original_config = dns.DEFAULT_CONFIG_PATH
        try:
            dns.DEFAULT_TOKEN_FILE = self.token_file
            dns.DEFAULT_CONFIG_PATH = self.directory / "no-config.yaml"
            transport = RecordedTransport(zone_routes([(200, api_ok([]))]))
            out = io.StringIO()
            code = dns.main(
                ["delete", SLUG, "--json"],
                transport=transport,
                stdout=out,
                stderr=io.StringIO(),
            )
        finally:
            dns.DEFAULT_TOKEN_FILE = original
            dns.DEFAULT_CONFIG_PATH = original_config

        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(transport.calls[0]["headers"]["Authorization"], f"Bearer {TOKEN}")


class CommandProtocolTest(unittest.TestCase):
    def test_the_module_is_ready_to_register_in_cli(self):
        for attribute in ("NAME", "HELP", "EXAMPLE", "add_arguments", "run"):
            self.assertTrue(hasattr(dns, attribute), f"dns has no {attribute}")
        self.assertEqual(dns.NAME, "dns")
        self.assertIn("odilectl dns", dns.EXAMPLE)


if __name__ == "__main__":
    unittest.main()
