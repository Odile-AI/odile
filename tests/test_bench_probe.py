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
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
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
        result = subprocess.run(
            ["/bin/sh", str(SCRIPT), "--probe"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "BENCH_URL": url, "BENCH_PORT": str(port)})
        if result.returncode == 2:
            # `--probe` exits 2 only from its `for bin in curl ss mktemp` loop
            # ("prerequisite: ss is not installed"), which the script's contract
            # and the five-minute timer read as "not installed", never as "the
            # bench is red". A host without iproute2 must therefore skip these
            # four tests and say which binary is missing — not report four
            # failures, which is a red `make check` from a fresh clone.
            raise unittest.SkipTest(result.stderr.strip() or
                                    "the probe's prerequisites are missing")
        return result

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


class PrerequisiteSkipTests(unittest.TestCase):
    """A host without `ss` skips the four probes; it does not fail them.

    ``check_bench.sh`` exits 2 from its ``for bin in curl ss mktemp`` loop when
    a prerequisite is missing, and its contract calls that "a prerequisite, not
    a check" — the five-minute timer and the operator read it that way. A test
    that asserts ``returncode == 0`` turns the same host into four red tests,
    so `make check` is red from a fresh clone with no ``iproute2`` (the G0
    substrate promise). The proof is behavioural: run the real module under a
    ``PATH`` that carries curl and mktemp but no ``ss``, and require the four
    probes to skip with the script's own prerequisite line as the reason.
    """

    # Enough of a PATH for the script to reach its prerequisite loop (it calls
    # `dirname` to resolve REPO) — and deliberately no `ss`.
    PATH_TOOLS = ("dirname", "curl", "mktemp", "sed", "awk", "tr", "head",
                  "grep", "rm", "cat", "basename", "date")

    @unittest.skipUnless(shutil.which("curl") and shutil.which("mktemp"),
                         "curl and mktemp are needed to build a PATH without ss")
    def test_the_four_probes_skip_and_name_ss_when_it_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            bin_dir = Path(name) / "bin"
            bin_dir.mkdir()
            for tool in self.PATH_TOOLS:
                found = shutil.which(tool)
                if found:
                    (bin_dir / tool).symlink_to(found)
            self.assertIsNone(shutil.which("ss", path=str(bin_dir)))
            result = subprocess.run(
                [sys.executable, "-m", "unittest",
                 "tests.test_bench_probe.ProbeTests", "-v"],
                cwd=ROOT, capture_output=True, text=True, timeout=120,
                env={**os.environ, "PATH": str(bin_dir)})
        output = result.stdout + result.stderr
        self.assertIn("Ran 4 tests", output)
        self.assertIn("OK (skipped=4)", output, output)
        self.assertIn("prerequisite: ss is not installed", output, output)
        self.assertEqual(
            output.count("prerequisite: ss is not installed"), 4, output)


class RecordTests(unittest.TestCase):
    """ODL-936: `--record` replaces the baseline, never truncates it in place."""

    EXPECT = "2caf4a5bcea82189" + "0" * 80

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.baseline = self.base / "a1-bench-baseline.json"
        stub = self.bin / "stub"
        stub.write_text("#!/bin/sh\n", encoding="utf-8")
        stub.chmod(0o755)
        # curl writes a body to -o and prints 200; dig answers an address;
        # systemctl reports NRestarts 0. The record path asks for nothing else.
        (self.bin / "curl").write_text(
            "#!/bin/sh\n"
            "while [ $# -gt 0 ]; do\n"
            "  case \"$1\" in -o) body=$2; shift 2 ;;\n"
            "    -w) shift 2 ;;\n"
            "    *) url=$1; shift ;;\n"
            "  esac\n"
            "done\n"
            "[ -n \"$body\" ] && printf 'page %s\\n' \"$url\" > \"$body\"\n"
            "printf '200'\n", encoding="utf-8")
        (self.bin / "dig").write_text("#!/bin/sh\nprintf '203.0.113.7\\n'\n",
                                      encoding="utf-8")
        (self.bin / "sqlite3").write_text("#!/bin/sh\nprintf '0\\n'\n",
                                          encoding="utf-8")
        (self.bin / "systemctl").write_text("#!/bin/sh\nprintf '0\\n'\n",
                                            encoding="utf-8")
        for name in ("curl", "dig", "sqlite3", "systemctl"):
            (self.bin / name).chmod(0o755)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def record(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/sh", str(SCRIPT), "--record", "--expect", self.EXPECT],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                 "BASELINE": str(self.baseline),
                 "PROD_DB": str(self.base / "prod.db"),
                 "BENCH_DB": str(self.base / "bench.db"),
                 "PROD_ROADMAP": str(self.base / "roadmap.html"),
                 "PROD_ATTEST": str(self.base / "attestation.json")})

    def test_the_record_is_one_complete_file_and_replaces_the_old_one(self) -> None:
        self.baseline.write_text('{"schema": "old", "production": {}}\n',
                                 encoding="utf-8")
        before = self.baseline.stat().st_ino
        result = self.record()
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(self.baseline.read_text(encoding="utf-8"))
        self.assertEqual(record["schema"], "odile.a1-bench-baseline.v1")
        self.assertEqual(record["expected_measurement"], self.EXPECT)
        self.assertEqual(set(record["production"]), {"/", "/roadmap", "/attestation"})
        self.assertEqual(record["production_nrestarts"], "0")
        # a rename, not a truncate-in-place: a failed record leaves the whole
        # old baseline behind, and no half-written temp file survives success
        self.assertNotEqual(self.baseline.stat().st_ino, before)
        self.assertEqual(sorted(path.name for path in self.base.iterdir()),
                         ["a1-bench-baseline.json", "bin"])
        self.assertEqual(self.baseline.stat().st_mode & 0o777, 0o644)


if __name__ == "__main__":
    unittest.main()
