"""The boot record goes to the HQ that issued the seed — measured, not assumed.

ODL-340's bench tenant reported `hq refused (400, bad_record)` and the evidence
read that as "the guest posts to production". The guest's reporter (`post_record`
in `deploy/image/odile-firstboot.py`) posts to the one endpoint its measured
command line names, so this test pins the contract the card's second DoD asks
for: a bench seed (`https://test.odile.fyi/seed`) sends the record to the bench,
a production seed (`https://odile.fyi/seed`, or none at all) sends it to
production, and both use the same route the token was exchanged at.

The bench's refusal was its own state spine: the record is written only once the
row has reached `first_boot` (odilectl/bootrecord.py), and the bench harness
moved the row there *after* the boot. That is a bench-procedure fact, recorded
in the card's evidence; the URL is correct here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIRSTBOOT = ROOT / "deploy" / "image" / "odile-firstboot.py"
TOKEN = "a" * 64


def load_firstboot_module():
    spec = importlib.util.spec_from_file_location("firstboot_under_test_record", FIRSTBOOT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load %s" % FIRSTBOOT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def measured_line(seed_url: str = "") -> str:
    line = (
        "root=/dev/mapper/root_crypt ro console=tty0 bb.release=2026.11.2"
        " bb.host=odl-test bb.iid=168020b4"
        " odile.token=" + TOKEN
    )
    return line + (" odile.seed_url=" + seed_url if seed_url else "")


class ReporterEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firstboot = load_firstboot_module()

    def test_a_bench_seed_is_where_the_record_goes(self) -> None:
        facts = self.firstboot.cmdline_facts(measured_line("https://test.odile.fyi/seed"))
        self.assertEqual(facts["seed_url"], "https://test.odile.fyi/seed")

    def test_a_production_seed_is_unchanged(self) -> None:
        facts = self.firstboot.cmdline_facts(measured_line("https://odile.fyi/seed"))
        self.assertEqual(facts["seed_url"], "https://odile.fyi/seed")

    def test_a_boot_with_no_seed_token_keeps_the_production_default(self) -> None:
        facts = self.firstboot.cmdline_facts(measured_line())
        self.assertEqual(facts["seed_url"], self.firstboot.DEFAULT_SEED_URL)

    def test_the_record_is_posted_to_the_endpoint_it_was_given(self) -> None:
        """One route for both halves: the token's URL is the record's URL."""
        seen: list[tuple[str, dict]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (http.server's name)
                length = int(self.headers.get("Content-Length") or 0)
                seen.append((self.path, json.loads(self.rfile.read(length) or b"{}")))
                body = json.dumps({"ok": True, "state": "recorded"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args) -> None:  # noqa: A002 (http.server)
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d/seed" % server.server_address[1]
            line = self.firstboot.report_record(
                {"release": "2026.11.2", "first_visit": "pending", "cmdline": measured_line(url)},
                "SHA256:test", "", token=TOKEN, instance_id="168020b4",
                seed_url=url, log_path=os.devnull, console=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertIn("recorded", line)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "/seed")
        self.assertEqual(seen[0][1]["instance_id"], "168020b4")
        self.assertEqual(seen[0][1]["record"]["cmdline"], measured_line(url))


if __name__ == "__main__":
    unittest.main()
