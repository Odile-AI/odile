"""The balance watch says one thing, once: the NEAR AI wall, with Spent/Limit.

The team stopped on 2026-11-20 because the prepaid balance hit its limit and the
first signal was a 402 on every worker. `cloud-api.near.ai` exposes no usage
endpoint, so the limit exists only inside a 402 body: the only cheap probe is to
ask for one token and read the refusal. These tests drive that probe against a
stub server and the classifier against the status codes the provider actually
returns, then assert the whole job — as a subprocess — prints exactly one line
and exits 0, because a cron runner reads a nonzero exit as a failed job and an
empty stdout as nothing to say.

The log half is a different failure: a worker that died at 06:00 between two
probes is named by the 402 its log kept, so the scan reads `gateway.log` and
every `profiles/*/logs/agent.log` and inherits a traceback continuation's
timestamp from the record that encloses it.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import near_balance_watch as watch  # noqa: E402

TOOL = ROOT / "tools" / "near_balance_watch.py"
# A fake inference key: its first six characters are the only part the alert
# may carry, and its tail must never reach stdout. It is short enough not to
# look like a provider token to `tools/secret_scan.py`.
FAKE_KEY = "sk-986odl-fixture-key"
PREFIX = "sk-986"
WALL = {
    "error": {
        "message": (
            "Credit limit exceeded. Spent: $500.018105032, Limit: $500.00. "
            "Please purchase more credits."
        ),
        "type": "credit_limit_exceeded",
    }
}
SENTENCE = "Spent: $500.018105032, Limit: $500.00"


def log_time(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S,") + f"{moment.microsecond // 1000:03d}"


class Stub:
    """A local endpoint that answers one status, and records each request."""

    def __init__(self, status: int, body: dict | str = "") -> None:
        self.status = status
        self.body = json.dumps(body) if isinstance(body, dict) else body
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                outer.requests.append({"path": self.path, "raw": raw})
                payload = outer.body.encode()
                self.send_response(outer.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args) -> None:  # noqa: D102
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> "Stub":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class ClassifierTests(unittest.TestCase):
    def test_200_is_healthy(self) -> None:
        self.assertEqual(watch.classify(200), "healthy")

    def test_402_is_the_wall(self) -> None:
        self.assertEqual(watch.classify(402), "critical")

    def test_429_is_soft(self) -> None:
        self.assertEqual(watch.classify(429), "soft")

    def test_5xx_is_soft(self) -> None:
        self.assertEqual(watch.classify(503), "soft")
        self.assertEqual(watch.classify(500), "soft")

    def test_a_transport_error_is_soft_not_healthy(self) -> None:
        self.assertEqual(watch.classify(0), "soft")

    def test_an_auth_failure_is_soft_so_one_blip_never_pages(self) -> None:
        self.assertEqual(watch.classify(401), "soft")


class SentenceTests(unittest.TestCase):
    def test_the_spent_limit_sentence_is_extracted_from_the_402_body(self) -> None:
        self.assertEqual(watch.spent_limit(json.dumps(WALL)), SENTENCE)

    def test_it_is_extracted_from_a_traceback_line(self) -> None:
        line = (
            "RuntimeError: HTTP 402: Credit limit exceeded. "
            "Spent: $500.018105032, Limit: $500.00. Please purchase more credits."
        )
        self.assertEqual(watch.spent_limit(line), SENTENCE)

    def test_absent_sentence_is_none(self) -> None:
        self.assertIsNone(watch.spent_limit('{"message":"No spending limit configured."}'))

    def test_the_prefix_is_six_characters_and_short_keys_pass_through(self) -> None:
        self.assertEqual(watch.key_prefix(FAKE_KEY), PREFIX)
        self.assertEqual(watch.key_prefix("sk-1"), "sk-1")


class KeyLoadingTests(unittest.TestCase):
    def test_the_profile_env_file_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text(f"OTHER=1\n{watch.KEY_ENV}={FAKE_KEY}\n", encoding="utf-8")
            self.assertEqual(watch.load_key(env, environ={}), FAKE_KEY)

    def test_a_raw_key_file_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "key.txt"
            env.write_text(FAKE_KEY + "\n", encoding="utf-8")
            self.assertEqual(watch.load_key(env, environ={}), FAKE_KEY)

    def test_the_environment_wins_without_a_key_file(self) -> None:
        self.assertEqual(watch.load_key(None, environ={watch.KEY_ENV: FAKE_KEY}), FAKE_KEY)

    def test_a_missing_key_is_empty_not_an_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(watch.load_key(Path(tmp) / "absent", environ={}), "")


class LogScanTests(unittest.TestCase):
    def test_a_traceback_continuation_inherits_its_record_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs").mkdir()
            (root / "profiles" / "builder" / "logs").mkdir(parents=True)
            now = dt.datetime.now(dt.timezone.utc)
            earlier = now - dt.timedelta(minutes=5)
            (root / "logs" / "gateway.log").write_text(
                f"{log_time(earlier)} ERROR gateway.session_cron: cron execution failed\n"
                "Traceback (most recent call last):\n"
                "RuntimeError: RuntimeError: HTTP 402: Credit limit exceeded. "
                f"{SENTENCE}. Please purchase more credits.\n"
                f"{log_time(earlier)} INFO gateway.run: next record\n",
                encoding="utf-8",
            )
            (root / "profiles" / "builder" / "logs" / "agent.log").write_text(
                f"{log_time(earlier)} WARNING agent.conversation_loop: API call failed "
                f"summary=HTTP 402: Credit limit exceeded. {SENTENCE}.\n",
                encoding="utf-8",
            )

            since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).timestamp()
            hits = watch.scan_logs(root, since)

            self.assertEqual(len(hits), 2)
            self.assertEqual({hit["spent_limit"] for hit in hits}, {SENTENCE})
            for hit in hits:
                self.assertGreaterEqual(hit["at"], earlier.timestamp() - 1)

    def test_an_old_or_clean_record_is_not_a_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs").mkdir()
            old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
            (root / "logs" / "gateway.log").write_text(
                f"{log_time(old)} ERROR gateway.session_cron: HTTP 402: Credit limit "
                f"exceeded. {SENTENCE}.\n",
                encoding="utf-8",
            )
            since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).timestamp()
            self.assertEqual(watch.scan_logs(root, since), [])


class JobTests(unittest.TestCase):
    """The whole job, as the cron runner sees it: stdout and the exit code."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.key_file = self.dir / "key.txt"
        self.key_file.write_text(FAKE_KEY + "\n", encoding="utf-8")
        self.state = self.dir / "state.json"
        self.hermes = self.dir / "hermes"
        (self.hermes / "logs").mkdir(parents=True)

    def run_job(self, stub: Stub) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                str(ROOT / "tools" / "near_balance_watch.py"),
                "--base-url", stub.base_url,
                "--key-file", str(self.key_file),
                "--state", str(self.state),
                "--hermes-root", str(self.hermes),
                "--timeout", "10",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "HERMES_HOME": str(self.dir / "no-profile")},
        )

    def test_a_402_delivers_exactly_one_line_naming_spent_limit_and_exits_0(self) -> None:
        with Stub(402, WALL) as stub:
            result = self.run_job(stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)
        self.assertIn(SENTENCE, result.stdout)
        self.assertIn(PREFIX, result.stdout)
        self.assertNotIn(FAKE_KEY, result.stdout)
        self.assertEqual(len(stub.requests), 1)
        self.assertEqual(stub.requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(json.loads(stub.requests[0]["raw"])["max_tokens"], 1)

    def test_a_healthy_run_prints_nothing_and_exits_0(self) -> None:
        with Stub(200, {"choices": [], "usage": {"total_tokens": 1}}) as stub:
            result = self.run_job(stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(stub.requests), 1)

    def test_one_429_is_silent_and_two_in_a_row_warns(self) -> None:
        with Stub(429, {"message": "rate limited"}) as stub:
            first = self.run_job(stub)
            second = self.run_job(stub)

        self.assertEqual(first.stdout, "")
        self.assertEqual(first.returncode, 0)
        self.assertEqual(len(second.stdout.splitlines()), 1)
        self.assertIn("429", second.stdout)
        self.assertIn("WARN", second.stdout)

    def test_healthy_resets_the_429_streak(self) -> None:
        with Stub(429, {"message": "rate limited"}) as stub:
            self.run_job(stub)
        with Stub(200, {"choices": []}) as stub:
            self.run_job(stub)
        with Stub(429, {"message": "rate limited"}) as stub:
            third = self.run_job(stub)
        self.assertEqual(third.stdout, "")

    def test_a_wall_whose_workers_died_between_probes_is_named_from_the_log(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        (self.hermes / "logs" / "gateway.log").write_text(
            f"{log_time(now - dt.timedelta(minutes=3))} ERROR gateway.session_cron: failed\n"
            "Traceback (most recent call last):\n"
            f"RuntimeError: RuntimeError: HTTP 402: Credit limit exceeded. {SENTENCE}.\n",
            encoding="utf-8",
        )
        with Stub(200, {"choices": []}) as stub:
            result = self.run_job(stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)
        self.assertIn(SENTENCE, result.stdout)
        self.assertIn("between probes", result.stdout)

        # The next probe has already read that record: silence, not a repeat.
        with Stub(200, {"choices": []}) as stub:
            again = self.run_job(stub)
        self.assertEqual(again.stdout, "")

    def test_a_standing_wall_is_one_line_per_reminder_window(self) -> None:
        with Stub(402, WALL) as stub:
            first = self.run_job(stub)
            second = self.run_job(stub)

        self.assertEqual(len(first.stdout.splitlines()), 1)
        self.assertEqual(second.stdout, "")


class InProcessTests(unittest.TestCase):
    """Behaviour a subprocess cannot reach: the reminder clock and the log window."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "key.txt").write_text(FAKE_KEY, encoding="utf-8")
        (self.dir / "hermes" / "logs").mkdir(parents=True)

    def run_main(self, stub: Stub) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = watch.main([
                "--base-url", stub.base_url,
                "--key-file", str(self.dir / "key.txt"),
                "--state", str(self.dir / "state.json"),
                "--hermes-root", str(self.dir / "hermes"),
                "--timeout", "10",
            ])
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_a_standing_wall_is_repeated_after_the_reminder_window(self) -> None:
        with mock.patch.object(watch, "REMIND_SECONDS", 0):
            with Stub(402, WALL) as stub:
                first = self.run_main(stub)
                second = self.run_main(stub)

        self.assertEqual(len(first.splitlines()), 1)
        self.assertEqual(len(second.splitlines()), 1)
        self.assertIn(SENTENCE, second)

    def test_a_missing_key_is_named_and_still_exits_0(self) -> None:
        with Stub(200, {"choices": []}) as stub:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = watch.main([
                    "--base-url", stub.base_url,
                    "--key-file", str(self.dir / "absent"),
                    "--state", str(self.dir / "state.json"),
                    "--hermes-root", str(self.dir / "hermes"),
                ])
        self.assertEqual(code, 0)
        self.assertEqual(len(buffer.getvalue().splitlines()), 1, buffer.getvalue())
        self.assertIn("no inference key", buffer.getvalue())
        self.assertIn(watch.KEY_ENV, buffer.getvalue())

    def test_the_log_window_starts_at_the_previous_probe(self) -> None:
        """A record older than the previous scan is not re-announced."""
        now = dt.datetime.now(dt.timezone.utc)
        log = self.dir / "hermes" / "logs" / "agent.log"
        log.write_text(
            f"{log_time(now - dt.timedelta(minutes=1))} WARNING HTTP 402: Credit limit "
            f"exceeded. {SENTENCE}.\n",
            encoding="utf-8",
        )
        with mock.patch.object(watch, "REMIND_SECONDS", 0):
            with Stub(200, {"choices": []}) as stub:
                first = self.run_main(stub)
        with Stub(200, {"choices": []}) as stub:
            second = self.run_main(stub)
        self.assertEqual(len(first.splitlines()), 1)
        self.assertIn(SENTENCE, first)
        self.assertEqual(second, "")


if __name__ == "__main__":
    unittest.main()
