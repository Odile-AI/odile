"""The bench liveness probe: one line, red or green. ODL-936.

`check_bench.sh --probe` is the signal the five-minute timer runs. Its job is
narrow on purpose:

* green only when https://test.odile.fyi/ answers 200 **and** carries
  ``server: odile-bench`` — `/health` alone was green for half a day beside a
  404 in November 2026;
* red with the pid and command that hold the bench port, because the outage
  was an orphaned ``seed.py serve`` on 127.0.0.1:8082 while the unit looped;
* exactly one line either way, and no baseline file, no tenant and no record
  needed — a dead bench must be reportable before any other check can run.

The tests are medium: a loopback HTTP server, a real subprocess and no sleep.
"""

from __future__ import annotations

import http.server
import os
import socket
import subprocess
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_bench.sh"


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200
    server_header = "odile-bench"
    body = b'<form id="join" method="post" action="/waitlist"></form>\n'

    def version_string(self) -> str:
        return self.server_header

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        self.send_response(self.status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format: str, *args) -> None:
        pass


def _serve(status: int, server_header: str,
           body: bytes | None = None) -> tuple[http.server.ThreadingHTTPServer, int]:
    handler = type("Handler", (_Handler,), {"status": status,
                                            "server_header": server_header,
                                            "body": body or _Handler.body})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


@unittest.skipUnless(SCRIPT.is_file(), "the bench check is missing")
class ProbeTests(unittest.TestCase):
    def run_probe(self, url: str, port: int) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/sh", str(SCRIPT), "--probe"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "BENCH_URL": url, "BENCH_PORT": str(port)})

    def test_a_healthy_bench_answers_one_line_and_exits_zero(self) -> None:
        server, port = _serve(200, "odile-bench")
        try:
            result = self.run_probe(f"http://127.0.0.1:{port}/", port)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stdout)
        self.assertIn("bench probe: OK", lines[0])
        self.assertIn("server: odile-bench", lines[0])
        self.assertIn(f"pid {os.getpid()}", lines[0])

    def test_a_foreign_listener_is_red_and_named(self) -> None:
        server, port = _serve(404, "SimpleHTTP", b"not found\n")
        try:
            result = self.run_probe(f"http://127.0.0.1:{port}/", port)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result.returncode, 1)
        lines = [line for line in result.stderr.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stderr)
        self.assertIn("bench probe: FAIL", lines[0])
        self.assertIn("404", lines[0])
        self.assertIn("server: simplehttp", lines[0])
        self.assertIn(f"pid {os.getpid()}", lines[0])

    def test_a_200_from_a_stranger_is_red_because_it_is_not_the_account_site(self) -> None:
        # The live failure on 2026-11-22: Caddy forces `Server: odile-bench`,
        # so a foreign server on 8082 answered 200 with the right header.
        # The account page's own join form is what distinguishes the app.
        server, port = _serve(200, "odile-bench", b"<title>Directory listing for /</title>\n")
        try:
            result = self.run_probe(f"http://127.0.0.1:{port}/", port)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result.returncode, 1)
        lines = [line for line in result.stderr.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stderr)
        self.assertIn("account page: no", lines[0])
        self.assertIn(f"pid {os.getpid()}", lines[0])

    def test_a_dead_port_is_red_and_says_nobody_listens(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        result = self.run_probe(f"http://127.0.0.1:{port}/", port)
        self.assertEqual(result.returncode, 1)
        lines = [line for line in result.stderr.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stderr)
        self.assertIn("bench probe: FAIL", lines[0])
        self.assertIn("nobody listening", lines[0])


if __name__ == "__main__":
    unittest.main()
