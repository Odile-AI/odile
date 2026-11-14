#!/usr/bin/python3
"""Behaviour tests for the Odile local demo.

Each test starts the real demo server as a subprocess and speaks HTTP to it,
so the suite covers the same path a judge runs: the printed password, the 401,
the page, the form posts, the restart and the scheduled job. The heartbeat
interval is shortened here; `demo/local/demo_check.py` runs it at the real 60
seconds.
"""

from __future__ import annotations

import re
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from demo.local import demo_check, server as demo_server  # noqa: E402
from tools import brand_lint  # noqa: E402

SENTENCE = (
    "local demo of the Odile workspace; the hosted demo runs the same surface "
    "on Odile's measured machine"
)


def strip_tags(text: str) -> str:
    """The visible text, so a test can assert what a reader sees."""
    return re.sub(r"<[^>]+>", "", text)


def css_rule(template: str, selector: str) -> str:
    """One inline rule from the template stylesheet.

    The stdlib cannot measure a rendered box, so the two layout contracts the
    Designer gate checks (a long name wraps, the terminal uses the brand
    tokens) are pinned here against the rule a browser would apply.
    """
    match = re.search(re.escape(selector) + r"\{([^}]*)\}", template)
    if match is None:
        raise AssertionError(f"no CSS rule for {selector}")
    return match.group(1).replace(" ", "")


class LocalDemoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="odile-demo-test-")
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.start_server()

    def start_server(self) -> None:
        self.server = demo_check.DemoProcess(self.state, heartbeat_seconds=0.25)
        self.addCleanup(self.stop_server)

    def stop_server(self) -> None:
        if self.server.proc.poll() is None:
            self.server.stop()

    def get(self, path: str = "", password: str | None = None):
        password = self.server.password if password is None else password
        return demo_check.request(
            self.server.url + path, password=password
        )

    def post(self, path: str, fields: dict[str, str]):
        return demo_check.request(
            self.server.url + path,
            method="POST",
            fields=fields,
            password=self.server.password,
        )

    # -- the gate ----------------------------------------------------------
    def test_401_without_a_password(self) -> None:
        status, headers, _ = demo_check.request(self.server.url)
        self.assertEqual(status, 401)
        self.assertTrue(headers.get("WWW-Authenticate", "").startswith("Basic "))

    def test_401_with_the_wrong_password(self) -> None:
        status, _, _ = self.get(password="not-the-password")
        self.assertEqual(status, 401)

    def test_200_with_the_printed_password_and_the_exact_sentence(self) -> None:
        status, _, page = self.get()
        self.assertEqual(status, 200)
        self.assertIn(SENTENCE, page)
        self.assertIn("Odile", page)

    # -- the surface -------------------------------------------------------
    def test_chat_returns_one_canned_answer_naming_the_route(self) -> None:
        status, _, page = self.post("/chat", {"message": "What runs this machine?"})
        self.assertEqual(status, 200)
        self.assertEqual(page.count('class="role">Odile'), 1)
        self.assertIn(demo_server.MODEL_ID, page)
        self.assertIn(demo_server.MODEL_HOST, page)
        self.assertIn("canned", page.lower())

    def test_shell_runs_one_command_in_the_workspace(self) -> None:
        status, _, page = self.post(
            "/shell", {"command": "printf 'shell-ok-%s\\n' \"$((6*7))\"; pwd"}
        )
        self.assertEqual(status, 200)
        self.assertIn("shell-ok-42", page)
        self.assertIn("[exit 0]", page)
        self.assertIn(str(self.state / "workspace"), page)

    def test_a_file_survives_a_restart(self) -> None:
        marker = "survives-" + secrets.token_hex(4)
        _, _, page = self.post(
            "/file", {"name": "proof.txt", "content": marker + "\n"}
        )
        self.assertIn("proof.txt", page)
        self.assertIn(marker, page)

        code, output = self.server.stop()
        self.assertEqual(code, 0, output)
        self.start_server()

        status, _, page = self.get()
        self.assertEqual(status, 200)
        self.assertIn("proof.txt", page)
        self.assertIn(marker, page)

    def test_a_file_name_cannot_escape_the_workspace(self) -> None:
        status, _, page = self.post(
            "/file", {"name": "../escaped.txt", "content": "nope"}
        )
        self.assertEqual(status, 200)
        self.assertIn("the file name is", page)
        self.assertFalse((self.state / "escaped.txt").exists())

    # -- flashes: an error is not a confirmation ---------------------------
    def test_an_empty_message_is_an_error_flash(self) -> None:
        status, _, page = self.post("/chat", {"message": ""})
        self.assertEqual(status, 200)
        self.assertIn('<p class="flash error">Type a message first.</p>', page)
        template = (ROOT / "demo" / "local" / "template.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("color:var(--error)", css_rule(template, ".flash.error"))

    def test_an_invalid_file_name_is_an_error_flash(self) -> None:
        status, _, page = self.post(
            "/file", {"name": "../escaped.txt", "content": "nope"}
        )
        self.assertEqual(status, 200)
        self.assertIn('<p class="flash error">the file name is', page)

    def test_a_written_file_is_a_plain_flash_and_one_byte_is_singular(self) -> None:
        status, _, page = self.post("/file", {"name": "note.txt", "content": "x"})
        self.assertEqual(status, 200)
        self.assertIn('<p class="flash">wrote note.txt (1 byte)</p>', page)
        self.assertIn("1 byte · ", strip_tags(page))
        self.assertNotIn('class="flash error"', page)

    def test_a_maximum_length_file_name_is_accepted_and_the_flash_wraps(self) -> None:
        name = "a" * 60 + ".txt"
        self.assertEqual(len(name), 64)
        status, _, page = self.post("/file", {"name": name, "content": "x"})
        self.assertEqual(status, 200)
        self.assertIn(f'<p class="flash">wrote {name} (1 byte)</p>', page)

        template = (ROOT / "demo" / "local" / "template.html").read_text(
            encoding="utf-8"
        )
        flash = css_rule(template, ".flash")
        self.assertIn("overflow-wrap:anywhere", flash)
        self.assertIn("min-width:0", flash)
        heading = css_rule(template, "h1")
        self.assertIn("overflow-wrap:anywhere", heading)

    def test_the_terminal_uses_the_brand_tokens(self) -> None:
        template = (ROOT / "demo" / "local" / "template.html").read_text(
            encoding="utf-8"
        )
        term = css_rule(template, "pre.term")
        self.assertIn("background:var(--term)", term)
        self.assertIn("color:var(--term-ink)", term)
        for token in ("term-command", "term-dim", "term-edge"):
            self.assertIn(f"--{token}:", template)

    def test_the_exit_line_starts_on_its_own_line(self) -> None:
        status, _, page = self.post("/shell", {"command": "printf 'no-newline'"})
        self.assertEqual(status, 200)
        self.assertIn("no-newline\n[exit 0]", strip_tags(page))

    def test_the_heartbeat_appends_a_line(self) -> None:
        _, _, page = self.get()
        first = demo_check.heartbeat_lines(page)
        self.assertTrue(first, "the job log was empty at startup")
        lines = first
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(0.1)
            _, _, page = self.get()
            lines = demo_check.heartbeat_lines(page)
            if lines and lines[-1] != first[-1]:
                break
        self.assertNotEqual(lines[-1], first[-1], "no new heartbeat line")
        self.assertIn("heartbeat", lines[-1])

    def test_the_default_heartbeat_is_sixty_seconds(self) -> None:
        self.assertEqual(demo_server.DEFAULT_HEARTBEAT_SECONDS, 60.0)
        self.assertEqual(demo_server.format_seconds(60.0), "60")
        other = demo_check.DemoProcess(Path(self.tmp.name) / "default-state")
        try:
            _, _, page = demo_check.request(
                other.url, password=other.password
            )
        finally:
            other.stop()
        self.assertIn("every 60 seconds", page)

    def test_sigint_stops_the_server_cleanly(self) -> None:
        code, output = self.server.stop()
        self.assertEqual(code, 0)
        self.assertIn("odile-demo: stopped", output)
        self.assertNotIn("Traceback", output)

    # -- the no-JavaScript, no-state, no-third-party rule ------------------
    def test_page_has_no_javascript_state_header_or_external_url(self) -> None:
        _, headers, page = self.get()
        self.assertNotIn("<script", page.lower())
        self.assertNotIn("{{", page)
        self.assertIsNone(re.search(r"https?://", page), "an absolute URL is in the page")
        self.assertFalse(headers.get_all("Set-Cookie"))

        for name in ("template.html", "server.py", "demo_check.py"):
            text = (ROOT / "demo" / "local" / name).read_text(encoding="utf-8")
            self.assertNotIn("<script", text.lower(), name)
            self.assertNotIn("cookie", text, name)
            self.assertIsNone(
                re.search(r"https?://(?!127)", text),
                f"{name} carries an external URL",
            )

    # -- the brand gate covers this surface --------------------------------
    def test_brand_lint_scans_the_demo_template(self) -> None:
        self.assertIn("demo/local/*.html", brand_lint.SURFACE_GLOBS)
        palette = brand_lint.brand_palette(ROOT / "BRAND.md")
        allowed = {value for value in palette.values() if value.startswith("#")}
        findings = brand_lint.check_surface(
            Path("demo/local/template.html"), allowed
        )
        self.assertEqual(findings, [])

    # -- the reset path is a dry run by default ---------------------------
    def test_reset_is_a_dry_run_until_applied(self) -> None:
        self.post("/file", {"name": "keep.txt", "content": "x"})
        self.server.stop()
        self.assertTrue(self.state.exists())

        dry = subprocess.run(
            [sys.executable, str(ROOT / "demo" / "local" / "server.py"),
             "--state", str(self.state), "--reset"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(dry.returncode, 0)
        self.assertIn("would remove", dry.stdout)
        self.assertTrue(self.state.exists())

        applied = subprocess.run(
            [sys.executable, str(ROOT / "demo" / "local" / "server.py"),
             "--state", str(self.state), "--reset", "--apply"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(applied.returncode, 0)
        self.assertIn("state removed", applied.stdout)
        self.assertFalse(self.state.exists())

        again = subprocess.run(
            [sys.executable, str(ROOT / "demo" / "local" / "server.py"),
             "--state", str(self.state), "--reset", "--apply"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(again.returncode, 0)
        self.assertIn("nothing to remove", again.stdout)


if __name__ == "__main__":
    unittest.main()
