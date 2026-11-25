#!/usr/bin/python3
"""ODL-947: what `ready` means, and what the ready email prints.

The card measured three defects on the register -> ready path, and this file is
the one test that drives the whole path in order, over the real service:

* register (POST /register), confirm (POST /confirm) and the account queue
  (`odilectl.provision --worker`) leave the row `building` — the provider's
  machine is active, the product's machine has not answered — and no ready mail
  exists;
* the account page says what it is waiting for, and does not say "ready";
* when the machine's own `/attestation` and `/verify` answer, the account
  queue writes `ready` and exactly one ready mail, with no press;
* that mail prints the machine's own 96-hex measurement, which is what its
  `/verify` page prints — the sentence that asks the tester to compare the two
  is true;
* a later press on *Send the email again* re-queues the identical bytes.

The network shape of the probe (the machine's own two URLs, the heartbeatless
bench included) is pinned here with a fake opener; the live run is recorded in
`docs/evidence/a2-ready-path.md`.

    /usr/bin/python3 -m unittest tests.test_ready_path -v
"""

from __future__ import annotations

import email
from email import policy as email_policy
import hashlib
import http.client
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from odilectl import claim, provision  # noqa: E402
from odilectl.config import Config  # noqa: E402
from odilectl.health import GuestAnswer, guest_answer  # noqa: E402

from tests.test_register import (  # noqa: E402
    SIGN_IN,
    FakeProvider,
    FlowBase,
    form,
    request,
)

#: A fixture boot-report value. Deliberately not the release pin, and not the
#: live machine's number: the whole point of the card is that the two differ.
MACHINE_MEASUREMENT = "c0" * 48
MEASUREMENT_RE = re.compile(r"\b[0-9a-f]{96}\b")


def ready_probe(measurement: str = MACHINE_MEASUREMENT):
    def probe(domain: str, slug: str):
        return GuestAnswer(slug=slug, host="%s.%s" % (slug, domain),
                           measurement=measurement, release="2026.11.23-5",
                           verify_bytes=50079, checked_at="2026-11-24T08:49:07Z")
    return probe


def waiting_probe(domain: str, slug: str):
    return None


class ReadyPathTests(FlowBase):
    """One registration, one order, one machine answer, one mail."""

    def write_config(self) -> Path:
        path = self.dir / "config.yaml"
        values = {
            "provider": "servury",
            "state_db": str(self.db),
            "seed_db": str(self.db),
            "seed_keys": str(self.dir / "seed-keys.json"),
            "outbox": str(self.outbox),
            "tenant_domain": "test.odile.fyi",
            "default_plan_id": "108",
            "default_days": "7",
            "near_account": str(self.dir / "near-account.json"),
            "near_workspace_id": "ws-test",
            "expected_measurement": app.pinned_measurement(),
        }
        path.write_text("".join("%s: %s\n" % item for item in values.items()),
                        encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def issued(self):
        from odilectl.keys import Issued

        return Issued(label="odile-odl-test", key_id="key-1",
                      key_prefix="sk-abc****", workspace_id="ws-test",
                      instance_id=None, spend_limit_nano=10_000_000_000,
                      raw_key="sk-abc...mnop")

    def register_and_confirm(self):
        email = self.email()
        status, _headers, body = self.register(email)
        self.assertEqual(status, 200, body[:300])
        account = self.account(email)
        token = self.confirmation_token(account["id"])
        status, headers, body = request(self.port, "POST", "/confirm",
                                        form(token=token))
        self.assertEqual(status, 303, body[:300])
        cookie = headers["Set-Cookie"].split(";")[0]
        fresh = self.account(email)
        self.assertEqual(fresh["provision_state"], "queued")
        return email, fresh, cookie

    def order(self, provider, *, probe):
        config = Config.load(self.write_config())
        with mock.patch.object(provision.keys, "issue", return_value=self.issued()), \
                mock.patch.object(provision, "_ensure_dns", return_value="created"):
            return provision.provision_queue(
                config, apply=True, provider_factory=lambda _c: provider,
                guest_probe=probe)

    def page(self, cookie: str) -> str:
        status, _headers, body = request(self.port, "GET", "/account", cookie=cookie)
        self.assertEqual(status, 200, body[:300])
        return body.decode("utf-8")

    def press(self, cookie: str):
        """The account page's *Send the email again*: POST /account/resend.

        The route is same-origin gated, so the Origin header is the page's own.
        """
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Connection": "close",
            "Cookie": cookie,
            "Origin": "http://127.0.0.1:%d" % self.port,
        }
        connection.request("POST", "/account/resend", body="", headers=headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        connection.close()
        return result

    def test_ready_is_the_machines_own_answer_and_the_mail_prints_its_number(self):
        address, account, cookie = self.register_and_confirm()
        slug = account["slug"]
        message_path = self.outbox / "ready" / ("%s.eml" % slug)
        provider = FakeProvider()

        # 1. The provider's machine is ordered and active. The product's machine
        #    has not answered, so the row is not ready and no mail exists.
        results = self.order(provider, probe=waiting_probe)
        self.assertEqual(len(results), 1, results)
        self.assertTrue(results[0]["ok"], results)
        self.assertFalse(results[0]["ready"], results)
        self.assertEqual(len(provider.created), 1)
        ordered = self.account(address)
        self.assertEqual(ordered["provision_state"], "building")
        self.assertEqual(ordered["server_id"], "srv-1")
        self.assertEqual(ordered["ipv4"], "203.0.113.41")
        self.assertFalse(message_path.exists(), "a ready mail was queued at order time")

        text = self.page(cookie)
        self.assertIn("<h1>Building.</h1>", text)
        self.assertNotIn("Your machine is ready.", text)
        self.assertIn("Its first boot prepares the encrypted disk, then the "
                      "machine's own report page; we are waiting for that page "
                      "to answer.", text)

        # 2. The machine answers at its own name. One run of the same timer
        #    writes exactly one mail, with no press; the row moves to ready.
        results = self.order(provider, probe=ready_probe())
        self.assertEqual(len(results), 1, results)
        self.assertTrue(results[0]["ready"], results)
        self.assertEqual(len(provider.created), 1, "the second run ordered again")
        ready = self.account(address)
        self.assertEqual(ready["provision_state"], "ready")
        self.assertTrue(message_path.is_file(), "the machine answered and no mail was queued")
        queued = sorted((self.outbox / "ready").iterdir())
        self.assertEqual(len([path for path in queued if path.suffix == ".eml"]), 1,
                         "more than one ready mail: %s" % queued)

        message = email.message_from_bytes(message_path.read_bytes(),
                                           policy=email_policy.default)
        part = message.get_body(preferencelist=("plain",))
        self.assertIsNotNone(part)
        body = part.get_content()
        found = MEASUREMENT_RE.findall(body)
        self.assertEqual(found, [MACHINE_MEASUREMENT],
                         "the mail does not print exactly the machine's measurement")
        self.assertNotIn(app.pinned_measurement(), body,
                         "the mail still prints the release pin")
        self.assertIn("Check the report before you sign in", body)
        facts = json.loads((self.outbox / "ready" / ("%s.json" % slug)).read_text())
        self.assertEqual(facts["measurement"], MACHINE_MEASUREMENT)
        before = hashlib.sha256(message_path.read_bytes()).hexdigest()

        # The page now says ready, and the row reads the same.
        text = self.page(cookie)
        self.assertIn("Your machine is ready.", text)
        self.assertIn('action="/account/resend"', text)

        # 3. The press re-queues the same bytes; it is not the only way the
        #    mail exists any more.
        for _press in range(2):
            status, headers, _body = self.press(cookie)
            self.assertEqual(status, 303, _body[:300])
            self.assertEqual(headers.get("Location"), "/account")
            self.assertEqual(before,
                             hashlib.sha256(message_path.read_bytes()).hexdigest(),
                             "a press changed the queued mail's bytes")

        # The account row keeps the machine and the trial writes nothing new.
        after = self.account(address)
        self.assertEqual(after["server_id"], "srv-1")
        self.assertEqual(after["provision_state"], "ready")
        self.assertEqual(after["trial_ends_at"], account["trial_ends_at"])


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, _limit: int | None = None) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def fake_opener(pages: dict[str, tuple[int, bytes]]):
    def opener(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        path = urllib.parse.urlparse(url).path
        status, body = pages.get(path, (404, b"not here"))
        return _FakeResponse(status, body)
    return opener


class GuestAnswerTests(unittest.TestCase):
    """The probe's network shape: the machine's report and its own page."""

    def attestation(self, measurement: str) -> bytes:
        return json.dumps({
            "available": True,
            "guest": "sev-snp",
            "measurement": measurement,
            "release": "2026.11.23-5",
            "fetched_at": "2026-11-24T08:47:24Z",
        }).encode("utf-8")

    def page(self, measurement: str) -> bytes:
        # A page that prints the measurement inside the sentence it makes.
        return ("<html><body><h1>Report</h1><p>Rebuild the measurement from "
                "the published UKI; it must equal the measurement below and "
                "the one in your ready email.</p><code>%s</code>"
                % measurement).encode("utf-8") * 12

    def test_the_page_and_the_report_must_be_the_same_machine(self):
        measurement = MACHINE_MEASUREMENT
        pages = {"/attestation": (200, self.attestation(measurement)),
                 "/verify": (200, self.page(measurement))}
        answer = guest_answer("test.odile.fyi", "odl-12bceb",
                              opener=fake_opener(pages))
        self.assertIsNotNone(answer)
        assert answer is not None
        self.assertEqual(answer.measurement, measurement)
        self.assertEqual(answer.release, "2026.11.23-5")
        self.assertEqual(answer.host, "odl-12bceb.test.odile.fyi")

        # A report with no attestation is not ready; a page that does not
        # print the report's measurement is not ready either (the /verify
        # sentence would be false).
        no_report = {"/attestation": (404, b""), "/verify": (200, self.page(measurement))}
        self.assertIsNone(guest_answer("test.odile.fyi", "odl-12bceb",
                                       opener=fake_opener(no_report)))
        wrong_page = {"/attestation": (200, self.attestation(measurement)),
                      "/verify": (200, self.page("ab" * 48))}
        self.assertIsNone(guest_answer("test.odile.fyi", "odl-12bceb",
                                       opener=fake_opener(wrong_page)))
        unavailable = {"/attestation": (200, json.dumps(
            {"available": False, "measurement": measurement}).encode()),
            "/verify": (200, self.page(measurement))}
        self.assertIsNone(guest_answer("test.odile.fyi", "odl-12bceb",
                                       opener=fake_opener(unavailable)))


class SharedBuilderTests(unittest.TestCase):
    """Both queues and the press build the mail in one place."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="odile-ready-")
        self.addCleanup(self.tmp.cleanup)
        self.outbox = Path(self.tmp.name) / "outbox"

    def test_a_press_rebuilds_the_same_bytes_from_the_facts(self):
        first = claim.queue_ready_email(
            self.outbox, slug="odl-abc123", to="a@example.com",
            url="https://odl-abc123.test.odile.fyi",
            measurement=MACHINE_MEASUREMENT, at=1795379886)
        before = hashlib.sha256(Path(first).read_bytes()).hexdigest()
        again = claim.queue_ready_email(
            self.outbox, slug="odl-abc123", to="a@example.com",
            url="https://odl-abc123.test.odile.fyi", at=1795380486)
        self.assertEqual(before, hashlib.sha256(Path(again).read_bytes()).hexdigest(),
                         "the press minted a different message")
        message = email.message_from_bytes(Path(again).read_bytes(),
                                           policy=email_policy.default)
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn(MACHINE_MEASUREMENT, body)

    def test_a_mail_without_the_machines_number_is_refused(self):
        with self.assertRaises(claim.ClaimError):
            claim.queue_ready_email(
                self.outbox, slug="odl-abc123", to="a@example.com",
                url="https://odl-abc123.test.odile.fyi",
                measurement=app.pinned_measurement()[:90])


if __name__ == "__main__":
    unittest.main()
