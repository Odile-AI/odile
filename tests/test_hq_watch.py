#!/usr/bin/python3
"""Tests for the off-host HQ liveness watcher (ODL-354).

Hermetic: a scratch state file, fake probe/send callables for the state
machine, and a loopback HTTP server for the real ``curl`` probe.  No Matrix
account, no HQ, no sleeps.

The card's line is "after three consecutive failures, exactly one message; one
recovery when HQ answers again", so the tests are organised around the cases a
retry can get wrong:

  * two failures send nothing; the third sends one;
  * a fourth and fifth failure do not send a second alert;
  * the alert attempt (``alert_open``, ``alert_delivered=false``) is on disk
    *before* the Matrix PUT, so a crash between the two leaves an alert owed;
  * a failure to deliver is retried with the *same* transaction id, so the
    homeserver deduplicates it;
  * an alert that landed but whose state write was lost is resent once with the
    same transaction id before the recovery, so the outage still gets both
    messages;
  * a success after a delivered alert sends one recovery, and a second success
    sends none;
  * two separate outages get two separate transaction ids;
  * a body that is 200 but not ``ok`` is a failure;
  * ``--dry-run`` writes no state and sends nothing.

    /usr/bin/python3 -m unittest tests.test_hq_watch -v
"""

from __future__ import annotations

import contextlib
import http.server
import importlib.util
import io
import json
import socket
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCH = ROOT / "deploy" / "hq-watch" / "watch.py"

spec = importlib.util.spec_from_file_location("hq_watch", WATCH)
if spec is None or spec.loader is None:  # pragma: no cover - a missing file is a hard error
    raise SystemExit(f"cannot import {WATCH}")
hq_watch = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hq_watch
spec.loader.exec_module(hq_watch)

NOW = datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc).timestamp()
STEP = 300

CONFIG = {
    "probe_url": "http://127.0.0.1/health",
    "expected_body": "ok",
    "failure_threshold": 3,
    "connect_timeout_s": 2,
    "max_time_s": 3,
    "curl": "/usr/bin/curl",
    "matrix_homeserver": "https://matrix.example",
    "matrix_room_id": "!room:example",
    "matrix_token_file": "/nonexistent/token",
    "state_file": "/nonexistent/state.json",
    "probe_name": "hqrecover",
}


def ok_probe(_config):
    return hq_watch.ProbeResult(True, 200, "ok", 0, "", 0.02)


def fail_probe(_config):
    return hq_watch.ProbeResult(
        False, 0, "", 28, "curl: (28) Connection timed out after 3002 milliseconds", 3.0
    )


class Recorder:
    def __init__(self, outcomes=None):
        self.calls = []
        self.outcomes = list(outcomes or [])

    def __call__(self, _config, text, transaction):
        self.calls.append((text, transaction))
        outcome = self.outcomes.pop(0) if self.outcomes else (True, f"$event{len(self.calls)}")
        if outcome[0]:
            return hq_watch.SendResult(True, outcome[1], "")
        return hq_watch.SendResult(False, None, outcome[1])


class StateMachineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state.json"

    def run_probe(self, probe, sender, index, dry_run=False):
        return hq_watch.run_once(
            CONFIG,
            self.state,
            probe_fn=probe,
            send_fn=sender,
            now=NOW + index * STEP,
            dry_run=dry_run,
            stream=io.StringIO(),
        )

    def test_two_failures_send_nothing(self):
        sender = Recorder()
        self.run_probe(fail_probe, sender, 0)
        result = self.run_probe(fail_probe, sender, 1)
        self.assertEqual(sender.calls, [])
        self.assertEqual(result["state"]["consecutive_failures"], 2)
        self.assertFalse(result["state"]["alert_open"])
        self.assertEqual(
            result["state"]["first_failure_at"], hq_watch.iso(NOW)
        )

    def test_third_failure_sends_exactly_one_alert(self):
        sender = Recorder()
        self.run_probe(fail_probe, sender, 0)
        self.run_probe(fail_probe, sender, 1)
        result = self.run_probe(fail_probe, sender, 2)
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(result["action"], "alert")
        body, transaction = sender.calls[0]
        self.assertIn(CONFIG["probe_name"], body)
        self.assertIn("3 consecutive failures", body)
        self.assertIn(hq_watch.iso(NOW), body)
        self.assertIn(hq_watch.iso(NOW + 2 * STEP), body)
        self.assertIn("UNREACHABLE", body)
        self.assertTrue(transaction.startswith("odile-hq-watch-alert-"))
        self.assertTrue(result["state"]["alert_open"])
        self.assertTrue(result["state"]["alert_delivered"])
        self.assertEqual(result["state"]["alerts_total"], 1)

        # A fourth and fifth failure are the same outage: no second message.
        self.run_probe(fail_probe, sender, 3)
        self.run_probe(fail_probe, sender, 4)
        self.assertEqual(len(sender.calls), 1)

    def test_recovery_is_sent_once(self):
        sender = Recorder()
        for index in range(3):
            self.run_probe(fail_probe, sender, index)
        result = self.run_probe(ok_probe, sender, 3)
        self.assertEqual(len(sender.calls), 2)
        self.assertEqual(result["action"], "recovery")
        body, transaction = sender.calls[1]
        self.assertIn("RECOVERED", body)
        self.assertIn(hq_watch.iso(NOW), body)              # outage began
        self.assertIn(hq_watch.iso(NOW + 3 * STEP), body)   # recovered at
        self.assertIn("15m 00s", body)
        self.assertIn("3 consecutive failures at the peak", body)
        self.assertTrue(transaction.startswith("odile-hq-watch-recovery-"))
        self.assertFalse(result["state"]["alert_open"])
        self.assertEqual(result["state"]["recoveries_total"], 1)

        # The next success is not another recovery.
        self.run_probe(ok_probe, sender, 4)
        self.assertEqual(len(sender.calls), 2)

    def test_alert_attempt_is_persisted_before_the_put(self):
        """The phase-1 record must be on disk when the PUT is made.

        A crash between the PUT and the confirming write leaves
        ``alert_delivered=false``; the next tick must read that record, resend
        the same transaction id (the homeserver deduplicates it) and still send
        the recovery.  If the phase-1 ``save_state`` is deleted or moved after
        the send, the state read inside ``send_fn`` is still the two-failure
        state and this test fails.
        """
        seen = {}

        def sender(_config, _text, _transaction):
            on_disk = hq_watch.load_state(self.state)
            seen["alert_open"] = on_disk.get("alert_open")
            seen["alert_delivered"] = on_disk.get("alert_delivered")
            seen["consecutive_failures"] = on_disk.get("consecutive_failures")
            seen["probes_total"] = on_disk.get("probes_total")
            seen["last_alert_at"] = on_disk.get("last_alert_at")
            return hq_watch.SendResult(True, "$event", "")

        self.run_probe(fail_probe, sender, 0)
        self.run_probe(fail_probe, sender, 1)
        result = self.run_probe(fail_probe, sender, 2)
        self.assertEqual(result["action"], "alert")

        # What the sender saw on disk at PUT time: the phase-1 attempt.
        self.assertIs(seen["alert_open"], True)
        self.assertIs(seen["alert_delivered"], False)
        self.assertEqual(seen["consecutive_failures"], 3)
        self.assertEqual(seen["probes_total"], 3)
        self.assertIsNone(seen["last_alert_at"])

        # And the confirmation that follows the PUT records the delivery.
        final = hq_watch.load_state(self.state)
        self.assertTrue(final["alert_open"])
        self.assertTrue(final["alert_delivered"])
        self.assertEqual(final["alerts_total"], 1)
        self.assertEqual(final["last_alert_event_id"], "$event")

    def test_failed_alert_is_retried_with_the_same_transaction(self):
        sender = Recorder([(False, "HTTP 502: bad gateway")])
        self.run_probe(fail_probe, sender, 0)
        self.run_probe(fail_probe, sender, 1)
        third = self.run_probe(fail_probe, sender, 2)
        self.assertTrue(third["state"]["alert_open"])
        self.assertFalse(third["state"]["alert_delivered"])
        self.assertEqual(third["state"]["alerts_total"], 0)
        self.assertEqual(len(sender.calls), 1)
        first_transaction = sender.calls[0][1]

        fourth = self.run_probe(fail_probe, sender, 3)
        self.assertEqual(len(sender.calls), 2)
        self.assertEqual(sender.calls[1][1], first_transaction)
        self.assertTrue(fourth["state"]["alert_open"])
        self.assertTrue(fourth["state"]["alert_delivered"])
        self.assertEqual(fourth["state"]["alerts_total"], 1)

    def test_alert_whose_confirmation_was_lost_is_finished_before_recovery(self):
        """A crash after the PUT but before the state write must not swallow
        the recovery message: the persisted phase-1 record says an alert is
        owed, so the next success resends it (deduplicated) and then recovers.
        """
        seeded = dict(hq_watch.STATE_DEFAULTS)
        seeded.update(
            {
                "consecutive_failures": 3,
                "outage_peak_failures": 3,
                "first_failure_at": hq_watch.iso(NOW),
                "first_failure_epoch": int(NOW),
                "last_failure_at": hq_watch.iso(NOW + 2 * STEP),
                "last_error": "curl: (28) Connection timed out",
                "alert_open": True,
                "alert_delivered": False,
                "probes_total": 3,
            }
        )
        hq_watch.save_state(self.state, seeded)
        sender = Recorder()

        first = self.run_probe(ok_probe, sender, 3)
        self.assertEqual(first["action"], "alert")
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(sender.calls[0][1], f"odile-hq-watch-alert-{int(NOW)}")
        self.assertIn("curl: (28)", sender.calls[0][0])
        self.assertIn("3 consecutive failures", sender.calls[0][0])
        self.assertTrue(first["state"]["alert_delivered"])

        second = self.run_probe(ok_probe, sender, 4)
        self.assertEqual(second["action"], "recovery")
        self.assertEqual(len(sender.calls), 2)
        self.assertEqual(sender.calls[1][1], f"odile-hq-watch-recovery-{int(NOW)}")
        self.assertFalse(second["state"]["alert_open"])
        self.assertFalse(second["state"]["alert_delivered"])

        third = self.run_probe(ok_probe, sender, 5)
        self.assertIsNone(third["action"])
        self.assertEqual(len(sender.calls), 2)

    def test_failed_recovery_is_retried_with_the_same_transaction(self):
        sender = Recorder([(True, "$alert"), (False, "timeout")])
        for index in range(3):
            self.run_probe(fail_probe, sender, index)
        first = self.run_probe(ok_probe, sender, 3)
        self.assertTrue(first["state"]["alert_open"])
        self.assertEqual(len(sender.calls), 2)
        first_transaction = sender.calls[1][1]

        second = self.run_probe(ok_probe, sender, 4)
        self.assertEqual(len(sender.calls), 3)
        self.assertEqual(sender.calls[2][1], first_transaction)
        self.assertFalse(second["state"]["alert_open"])
        self.assertEqual(second["state"]["recoveries_total"], 1)

    def test_two_outages_get_two_transactions(self):
        sender = Recorder()
        for index in range(3):
            self.run_probe(fail_probe, sender, index)
        self.run_probe(ok_probe, sender, 3)
        for index in range(4, 7):
            self.run_probe(fail_probe, sender, index)
        self.assertEqual(len(sender.calls), 3)
        first_alert = sender.calls[0][1]
        second_alert = sender.calls[2][1]
        self.assertNotEqual(first_alert, second_alert)
        self.assertTrue(second_alert.startswith("odile-hq-watch-alert-"))

    def test_dry_run_writes_nothing_and_sends_nothing(self):
        sender = Recorder()
        # Seed the state as if two failures had already been recorded; the
        # dry run must decide "alert" but write and send nothing.
        seeded = dict(hq_watch.STATE_DEFAULTS)
        seeded.update(
            {
                "consecutive_failures": 2,
                "first_failure_at": hq_watch.iso(NOW),
                "first_failure_epoch": int(NOW),
                "last_failure_at": hq_watch.iso(NOW + STEP),
                "probes_total": 2,
            }
        )
        hq_watch.save_state(self.state, seeded)
        before = self.state.read_bytes()
        output = io.StringIO()
        result = hq_watch.run_once(
            CONFIG,
            self.state,
            probe_fn=fail_probe,
            send_fn=sender,
            now=NOW + 2 * STEP,
            dry_run=True,
            stream=output,
        )
        self.assertEqual(result["action"], "alert")
        self.assertEqual(sender.calls, [])
        self.assertEqual(self.state.read_bytes(), before)
        self.assertIn("DRY-RUN would send", output.getvalue())
        self.assertIn("3 consecutive failures", output.getvalue())

    def test_state_round_trips_as_json(self):
        sender = Recorder()
        result = self.run_probe(fail_probe, sender, 0)
        raw = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(raw["last_probe_at"], result["state"]["last_probe_at"])
        self.assertEqual(raw["schema"], 1)
        self.assertEqual(hq_watch.load_state(self.state)["probes_total"], 1)

    def test_corrupt_state_is_moved_aside(self):
        self.state.write_text("{not json", encoding="utf-8")
        sender = Recorder()
        result = self.run_probe(ok_probe, sender, 0)
        self.assertTrue(result["probe"].ok)
        self.assertTrue(self.state.exists())
        moved = list(self.state.parent.glob("state.json.corrupt-*"))
        self.assertEqual(len(moved), 1)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["probes_total"], 1)


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200
    body = b"ok"

    def do_GET(self):  # noqa: N802 - http.server API
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format, *args):  # noqa: A002 - http.server API
        pass


class CurlProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def config(self, path="/health", body="ok", status=200):
        _Handler.body = body.encode()
        _Handler.status = status
        value = dict(CONFIG)
        value["probe_url"] = f"http://127.0.0.1:{self.port}{path}"
        return value

    def test_200_with_expected_body_is_success(self):
        result = hq_watch.probe_hq(self.config())
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.http_code, 200)
        self.assertEqual(result.body, "ok")

    def test_200_with_wrong_body_is_failure(self):
        result = hq_watch.probe_hq(self.config(body="maintenance"))
        self.assertFalse(result.ok)
        self.assertIn("maintenance", result.error)

    def test_503_is_failure(self):
        result = hq_watch.probe_hq(self.config(status=503, body="down"))
        self.assertFalse(result.ok)
        self.assertEqual(result.http_code, 503)

    def test_closed_port_is_failure(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        value = dict(CONFIG)
        value["probe_url"] = f"http://127.0.0.1:{port}/health"
        result = hq_watch.probe_hq(value)
        self.assertFalse(result.ok)
        self.assertNotEqual(result.exit_code, 0)
        self.assertTrue(result.error)


if __name__ == "__main__":
    unittest.main()
