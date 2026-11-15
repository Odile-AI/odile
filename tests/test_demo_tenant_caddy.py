"""The tenant demo's Caddy block is unfrozen.

Savio, 2026-11-13: *"Remove everything about freezing in our project codebase
(self-update block included)"*. The block carried a deny-by-default mutation
matcher, neutralised at runtime by a first path line naming a path no request
uses; both are deleted, so a judge's writes reach Hermes and a self-update is
open. The ops note that described the runtime workaround is deleted with it.

Three properties, in the order a reader needs them:

* the committed block carries no mutation matcher, no self-update matcher and
  no unfreeze line -- and neither does the Caddyfile the apply renders from it;
* the keeps survive: Odile's ``/login`` page, the machine's own ``/verify`` and
  ``/attestation``, the ``@apidocs`` 404 and the reverse proxy to the gateway;
* Caddy itself agrees. The rendered block is started against a stub gateway, so
  an unauthenticated ``POST /api/hermes/update`` and a judge's
  ``PUT /api/config`` come back as the stub's 401 (with its marker header) --
  not as Caddy's 403, which was the freeze's signature.
"""

from __future__ import annotations

import http.server
import importlib.util
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE_BLOCK = ROOT / "deploy" / "caddy" / "demo-tenant-site.caddy"
APPLY = ROOT / "deploy" / "demo-host" / "apply_demo_public.py"
BEGIN = "# BEGIN odile-demo-public"
END = "# END odile-demo-public"
GATEWAY = "127.0.0.1:9130"

#: The machine's own Caddyfile above the demo's block. `admin off` is the
#: machine's setting (apply_demo_public.py's docstring), not the block's; the
#: block comment says so and must stay true.
BASE = "# the machine's own Caddyfile\n{\n\tadmin off\n}\n"

#: Spelled in two pieces on purpose: the freeze tokens are gone from the tree,
#: and the repo-wide fence (t_dd815e89) greps for them whole, so this file must
#: not be the one place they reappear.
FORBIDDEN = ("@mut" + "ation", "@upd" + "ate", "__dis" + "abled__")


def apply_module():
    """The apply program, loaded so its own `splice()` renders the block."""
    spec = importlib.util.spec_from_file_location("odile_apply_demo_public", APPLY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def rendered() -> str:
    """The Caddyfile a deploy writes: the committed block spliced in place."""
    module = apply_module()
    return module.splice(BASE, SITE_BLOCK.read_text(encoding="utf-8"), BEGIN, END)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ShapeTests(unittest.TestCase):
    def test_the_committed_block_carries_no_freeze(self) -> None:
        text = SITE_BLOCK.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            self.assertNotIn(token, text, f"the committed block still names {token!r}")

    def test_the_rendered_caddyfile_carries_no_freeze(self) -> None:
        text = rendered()
        for token in FORBIDDEN:
            self.assertNotIn(token, text, f"the rendered Caddyfile still names {token!r}")
        self.assertEqual(text.count(BEGIN), 1)
        self.assertEqual(text.count(END), 1)
        self.assertIn(BASE.rstrip("\n"), text, "the machine's own Caddyfile is kept")

    def test_the_keeps_survive(self) -> None:
        text = rendered()
        for keep in ("handle /login*", "handle /verify", "handle /attestation",
                     f"reverse_proxy {GATEWAY}", "@apidocs path /openapi.json"):
            self.assertIn(keep, text, f"the block lost {keep!r}")
        # ODL-505: the five icon paths belong to the machine's own page server,
        # exact, with no Cache-Control of Caddy's own — the service sends the
        # immutable on a 200 and the no-store on its 404.
        for icon in ("/favicon.ico", "/icon-32.png", "/icon-192.png",
                     "/icon-512.png", "/apple-touch-icon.png"):
            self.assertIn(f"handle {icon} {{\n\t\treverse_proxy 127.0.0.1:8091",
                          text, f"the block lost the {icon} route")
            self.assertNotIn(f"handle {icon} {{\n\t\theader", text)
        # `admin off` stays the machine's setting; the block must not re-enable
        # the admin endpoint (nor rely on a hot reload that is not there).
        self.assertIn("admin off", text)
        self.assertNotRegex(text, r"(?m)^\s*admin\s+on\b")


@unittest.skipUnless(shutil.which("caddy"), "caddy is not installed")
class CaddyValidateTests(unittest.TestCase):
    def test_caddy_validates_the_rendered_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo-tenant-site.Caddyfile"
            path.write_text(rendered(), encoding="utf-8")
            result = subprocess.run(
                ["caddy", "validate", "--config", str(path), "--adapter", "caddyfile"],
                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("Valid configuration", result.stdout + result.stderr)


class _StubGateway(http.server.BaseHTTPRequestHandler):
    """Every request answers 401, the gateway's own refusal, with a marker.

    The marker is what separates "Caddy forwarded it" from "Caddy answered it":
    Hermes's unauthenticated 401 and this stub's 401 are the same status, and a
    test that only read the status could pass with the block answering itself.
    """

    def answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = b'{"detail":"Unauthorized","stub":true}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Odile-Stub", "gateway")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = answer

    def log_message(self, format: str, *args) -> None:  # the test output stays readable
        pass


@unittest.skipUnless(shutil.which("caddy"), "caddy is not installed")
class CaddyForwardsWritesTests(unittest.TestCase):
    """Start the real Caddy on the rendered block and probe the writes."""

    def start(self) -> tuple[subprocess.Popen[bytes], int, tempfile.TemporaryDirectory[str], int]:
        stub_port = free_port()
        stub = http.server.HTTPServer(("127.0.0.1", stub_port), _StubGateway)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        self.addCleanup(stub.shutdown)
        child: subprocess.Popen[bytes] | None = None
        for _ in range(5):
            port = free_port()
            config = rendered().replace("demo.odile.fyi {", f"http://127.0.0.1:{port} {{", 1)
            config = config.replace(GATEWAY, f"127.0.0.1:{stub_port}")
            tmp = tempfile.TemporaryDirectory()
            path = Path(tmp.name) / "Caddyfile"
            path.write_text(config, encoding="utf-8")
            child = subprocess.Popen(
                ["caddy", "run", "--config", str(path), "--adapter", "caddyfile"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            for _ in range(40):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        return child, port, tmp, stub_port
                except OSError:
                    if child.poll() is not None:
                        break
                    time.sleep(0.1)
            child.terminate()
            child.wait(timeout=10)
            tmp.cleanup()
        detail = child.stderr.read()[-2000:] if child and child.stderr else b""
        raise AssertionError(f"caddy did not start: {detail!r}")

    def probe(self, port: int, method: str, path: str, body: bytes | None = None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", method=method, data=body,
            headers={"Content-Type": "application/json"} if body else {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers)
        except urllib.error.URLError:
            return 0, {}

    def test_a_write_reaches_the_gateway_not_caddy(self) -> None:
        child, port, tmp, _ = self.start()
        try:
            writes = (
                ("POST", "/api/hermes/update", None),
                ("PUT", "/api/config", b"{}"),
                ("POST", "/api/cron/jobs", b"{}"),
                ("DELETE", "/api/sessions/local-abc", None),
                ("PATCH", "/api/config", b"{}"),
            )
            for method, path, body in writes:
                status, headers = self.probe(port, method, path, body)
                self.assertNotEqual(status, 403, f"{method} {path} is still refused by Caddy")
                self.assertEqual(status, 401, f"{method} {path} -> {status}")
                self.assertEqual(headers.get("X-Odile-Stub"), "gateway",
                                 f"{method} {path} did not reach the gateway")
            status, _ = self.probe(port, "GET", "/api/sessions")
            self.assertEqual(status, 401, "the control read reaches the gateway")
        finally:
            child.terminate()
            child.wait(timeout=10)
            tmp.cleanup()

    def test_the_api_browser_is_still_hidden(self) -> None:
        child, port, tmp, _ = self.start()
        try:
            self.assertEqual(self.probe(port, "GET", "/openapi.json")[0], 404)
        finally:
            child.terminate()
            child.wait(timeout=10)
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
