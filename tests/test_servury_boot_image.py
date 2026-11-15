"""One call repoints a tenant's next start, so the call is fenced and logged.

`POST ?action=boot-image&id=…` is the whole operator power in one request: a
wrong pair breaks the next start, and until this tool nothing at Odile recorded
who changed it. These tests pin the four properties that make the record worth
something — the allow-list, the two-flag gate, one log line per attempt, and the
pair the attempt replaced — plus the read path that reports what the machine
carries now.

The log is a live host file at `/root/.odile/servury-reseller.log`, so every
test that writes one swaps the module constant for a temp path first and proves
the live file did not move. The key is never on a command line and never in a
log line; both are asserted, not promised.
"""

from __future__ import annotations

import ast
import contextlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import servury_boot_image as tool  # noqa: E402

# The defaults as imported, before a test swaps the live paths for temp ones.
DEFAULT_KEY_FILE = tool.KEY_FILE
DEFAULT_LOG = tool.LOG

URL = "https://odile.fyi/releases/m1/odile-tenant.efi"
SHA = "5cb318083d2ce0555132a008b0d5dfb7613e6bd0a3fb19d0ae29adac8b1b6a5e"
OTHER_SHA = "f7bc0ceba60442b0" + "0" * 48
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z BOOT_IMAGE ", re.ASCII)


def state(url: str = URL, sha256: str = SHA, download: str = "ready",
          added: str = "2026-11-13T10:14:05Z") -> dict:
    """A `boot_image` payload shaped like the live GET (measured 2026-11-15)."""
    return {
        "next_start": "custom",
        "release": "2026.11.2",
        "image": {
            "url": url, "sha256": sha256, "size": 120481240, "type": "pe",
            "cmdline": "", "added": added,
            "initrd_url": None, "initrd_sha256": None, "initrd_size": None,
        },
        "download": {
            "state": download, "finished": "2026-11-13T10:14:14Z",
            "kernel_sha256": sha256, "kernel_size": 120481240, "initrd_sha256": None,
        },
        "limits": {"max_bytes": 1073741824, "max_cmdline": 4096, "min_kernel_bytes": 131072},
        "network": {"suggested_cmdline": "console=ttyS0,115200"},
    }


def payload(boot_image: dict | None = None, *, message: str = "") -> dict:
    body = {"success": True, "data": {"boot_image": boot_image if boot_image is not None else state()}}
    if message:
        body["message"] = message
    return body


class ToolCase(unittest.TestCase):
    """A temp log and a fake key, so no test touches /root/.odile."""

    KEY = "k" * 64

    def setUp(self) -> None:
        self.live_log = tool.LOG
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.log = Path(self.dir.name) / "servury-reseller.log"
        self.live_before = self.live_lines()
        patcher = mock.patch.object(tool, "LOG", self.log)
        patcher.start()
        self.addCleanup(patcher.stop)

    def live_lines(self) -> list[str]:
        try:
            return self.live_log.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def lines(self) -> list[str]:
        try:
            return self.log.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def run_tool(self, argv: list[str], *, calls: list | None = None,
                 before: dict | None = None, after: dict | None = None) -> tuple[int, str, str]:
        """Drive main() with the API faked; return (exit, stdout, stderr)."""
        calls = calls if calls is not None else []
        before = before if before is not None else payload()
        after = after if after is not None else payload(message="Download started.")

        def fake_call(action, params=None, *, key, server_id, timeout=40.0):
            calls.append({"action": action, "params": params, "server_id": server_id, "key": key})
            return after if params is not None else before

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(tool, "call", fake_call), \
                mock.patch.object(tool, "read_key", return_value=self.KEY), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(argv)
        return code, out.getvalue(), err.getvalue()

    def tearDown(self) -> None:
        self.assertEqual(self.live_lines(), self.live_before,
                         "make check must not append fake writes to the live reseller log")


class AllowListTests(ToolCase):
    """The key owns the production tenant, so the tool names one id by default."""

    def test_the_production_tenant_is_the_only_default(self) -> None:
        self.assertTrue(tool.allowed("31a63bbb"))

    def test_another_id_is_refused(self) -> None:
        self.assertFalse(tool.allowed("e5f6a7b8"))

    def test_the_fence_is_exact_not_case_folded(self) -> None:
        self.assertFalse(tool.allowed("31A63BBB"), "a widened fence is not the allow-list")

    def test_an_extra_id_is_deliberate(self) -> None:
        self.assertTrue(tool.allowed("e5f6a7b8", frozenset({"e5f6a7b8"})))

    def test_an_id_outside_the_list_never_reaches_the_api(self) -> None:
        for argv in (["--show", "--server", "e5f6a7b8"],
                     ["--set", "--server", "e5f6a7b8", "--url", URL, "--sha256", SHA,
                      "--apply", "--confirm"]):
            calls: list = []
            code, _, err = self.run_tool(argv, calls=calls)
            self.assertEqual(code, 2, argv)
            self.assertEqual(calls, [], "a refused id must not spend a request")
            self.assertIn("e5f6a7b8", err)
        self.assertEqual(self.lines(), [], "a refused id is not an attempt")


class TwoFlagTests(ToolCase):
    """A wrong pair breaks the next start, so a real write needs both flags."""

    def test_apply_without_confirm_is_a_usage_error(self) -> None:
        calls: list = []
        code, _, err = self.run_tool(
            ["--set", "--server", "31a63bbb", "--url", URL, "--sha256", SHA, "--apply"],
            calls=calls)

        self.assertEqual(code, 2)
        self.assertIn("--confirm", err)
        self.assertEqual(calls, [])
        self.assertEqual(self.lines(), [])

    def test_a_bare_set_is_a_dry_run(self) -> None:
        calls: list = []
        code, out, _ = self.run_tool(
            ["--set", "--server", "31a63bbb", "--url", URL, "--sha256", SHA], calls=calls)

        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertEqual([c["action"] for c in calls], ["boot-image"], "only the GET ran")
        self.assertIsNone(calls[0]["params"])

    def test_both_flags_submit_the_pair(self) -> None:
        calls: list = []
        code, out, _ = self.run_tool(
            ["--set", "--server", "31a63bbb", "--url", URL, "--sha256", SHA,
             "--apply", "--confirm"], calls=calls)

        self.assertEqual(code, 0)
        self.assertEqual([c["params"] for c in calls], [None, {"url": URL, "sha256": SHA}])
        self.assertIn("set", out)

    def test_the_api_s_own_sentence_is_printed_when_it_sends_one(self) -> None:
        # The live POST answers "Download started. Poll GET …"; a tool that
        # drops it leaves the operator to guess whether the node took the write.
        for body in ({"success": True, "message": "Download started.",
                      "data": {"boot_image": state()}},
                     {"success": True, "data": {"boot_image": state(),
                                                "message": "Download started."}}):
            calls: list = []

            def fake_call(action, params=None, *, key, server_id, timeout=40.0, _body=body):
                calls.append(params)
                return _body if params is not None else payload()

            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(tool, "call", fake_call), \
                    mock.patch.object(tool, "read_key", return_value=self.KEY), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = tool.main(["--set", "--server", "31a63bbb", "--url", URL,
                                  "--sha256", SHA, "--apply", "--confirm"])

            self.assertEqual(code, 0)
            self.assertIn("Download started.", out.getvalue(), body.keys())

    def test_a_short_hash_is_refused_before_the_api(self) -> None:
        calls: list = []
        code, _, err = self.run_tool(
            ["--set", "--server", "31a63bbb", "--url", URL, "--sha256", "5cb31808",
             "--apply", "--confirm"], calls=calls)

        self.assertEqual(code, 2)
        self.assertIn("64", err)
        self.assertEqual(calls, [])

    def test_a_sha_that_is_not_hex_is_refused(self) -> None:
        code, _, _ = self.run_tool(
            ["--set", "--server", "31a63bbb", "--url", URL, "--sha256", "z" * 64,
             "--apply", "--confirm"])
        self.assertEqual(code, 2)

    def test_set_needs_a_url_and_a_hash(self) -> None:
        for argv in (["--set", "--server", "31a63bbb", "--url", URL],
                     ["--set", "--server", "31a63bbb", "--sha256", SHA]):
            code, _, _ = self.run_tool(argv)
            self.assertEqual(code, 2, argv)

    def test_one_verb_at_a_time(self) -> None:
        code, _, _ = self.run_tool(["--show", "--set", "--server", "31a63bbb",
                                    "--url", URL, "--sha256", SHA])
        self.assertEqual(code, 2)

    def test_no_verb_and_no_server_are_usage_errors(self) -> None:
        self.assertEqual(self.run_tool([])[0], 2)
        self.assertEqual(self.run_tool(["--show"])[0], 2)


class LogTests(ToolCase):
    """The line is the record: one per attempt, carrying the pair it replaced."""

    def test_a_dry_run_is_logged_with_the_pair_it_would_replace(self) -> None:
        code, _, _ = self.run_tool(
            ["--set", "--server", "31a63bbb", "--url", URL, "--sha256", SHA])

        self.assertEqual(code, 0)
        self.assertEqual(len(self.lines()), 1, self.lines())
        line = self.lines()[0]
        self.assertRegex(line, STAMP)
        self.assertIn("server=31a63bbb", line)
        self.assertIn(f"url={URL}", line)
        self.assertIn(f"sha256={SHA}", line)
        self.assertIn("apply=0", line)
        self.assertIn("who=", line)
        self.assertIn(f"replaced_sha256={SHA}", line)
        self.assertIn("replaced_added=2026-11-13T10:14:05Z", line)

    def test_a_real_write_is_logged_with_the_pair_it_replaced(self) -> None:
        before = payload(state(sha256=OTHER_SHA, added="2026-11-12T19:19:08Z"))

        code, _, _ = self.run_tool(
            ["--set", "--server", "31a63bbb", "--url", URL, "--sha256", SHA,
             "--apply", "--confirm"], before=before)

        self.assertEqual(code, 0)
        self.assertEqual(len(self.lines()), 1, self.lines())
        line = self.lines()[0]
        self.assertIn("apply=1", line)
        self.assertIn(f"replaced_sha256={OTHER_SHA}", line)
        self.assertIn("replaced_added=2026-11-12T19:19:08Z", line)

    def test_the_key_is_never_in_the_line(self) -> None:
        self.run_tool(["--set", "--server", "31a63bbb", "--url", URL, "--sha256", SHA])
        self.assertNotIn(self.KEY, self.lines()[0])

    def test_the_log_is_private(self) -> None:
        self.run_tool(["--set", "--server", "31a63bbb", "--url", URL, "--sha256", SHA])

        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)

    def test_a_state_that_cannot_be_read_logs_the_attempt_and_writes_nothing(self) -> None:
        calls: list = []

        def failing(action, params=None, *, key, server_id, timeout=40.0):
            calls.append(params)
            raise RuntimeError("Server not found")

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(tool, "call", failing), \
                mock.patch.object(tool, "read_key", return_value=self.KEY), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(["--set", "--server", "31a63bbb", "--url", URL, "--sha256", SHA,
                              "--apply", "--confirm"])

        self.assertEqual(code, 1, "an unreadable predecessor fails closed")
        self.assertEqual(calls, [None], "the POST must not run when the GET failed")
        self.assertEqual(len(self.lines()), 1)
        self.assertIn("replaced_sha256=unknown", self.lines()[0])
        self.assertIn("apply=1", self.lines()[0])

    def test_a_missing_key_file_is_a_usage_error(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(tool, "read_key", side_effect=FileNotFoundError("nope")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(["--show", "--server", "31a63bbb"])

        self.assertEqual(code, 2)
        self.assertIn(str(tool.KEY_FILE), err.getvalue())

    def test_two_attempts_are_two_lines(self) -> None:
        argv = ["--set", "--server", "31a63bbb", "--url", URL, "--sha256", SHA]
        self.run_tool(argv)
        self.run_tool(argv)

        self.assertEqual(len(self.lines()), 2)
        self.assertIn("apply=0", self.lines()[0])
        self.assertIn("apply=0", self.lines()[1])

    def test_an_unwritable_log_stops_the_write(self) -> None:
        # The line is the record; a write whose line cannot be written is the
        # silent repoint this card exists to end. Log first, then POST.
        calls: list = []
        broken = Path(self.dir.name) / "not-a-dir"
        broken.write_text("", encoding="utf-8")
        with mock.patch.object(tool, "LOG", broken / "log"):
            code, _, err = self.run_tool(
                ["--set", "--server", "31a63bbb", "--url", URL, "--sha256", SHA,
                 "--apply", "--confirm"], calls=calls)

        self.assertEqual(code, 1)
        self.assertIn("could not append", err)
        self.assertEqual([call["params"] for call in calls], [None],
                         "the GET reads the predecessor; the POST must not run")

    def test_the_default_paths_are_the_contract(self) -> None:
        # Read before setUp swaps the live log for a temp one.
        self.assertEqual(DEFAULT_KEY_FILE, Path("/root/.odile/servury_reseller_key"))
        self.assertEqual(DEFAULT_LOG, Path("/root/.odile/servury-reseller.log"))


class ShowTests(ToolCase):
    """`--show` answers what the machine carries now, in four fields."""

    def test_the_pair_the_state_and_the_added_stamp_are_printed(self) -> None:
        code, out, _ = self.run_tool(["--show", "--server", "31a63bbb"])

        self.assertEqual(code, 0)
        self.assertIn(URL, out)
        self.assertIn(SHA, out)
        self.assertIn("ready", out)
        self.assertIn("2026-11-13T10:14:05Z", out)
        self.assertIn("next_start=custom", out)

    def test_a_download_still_running_says_so(self) -> None:
        code, out, _ = self.run_tool(["--show", "--server", "31a63bbb"],
                                     before=payload(state(download="downloading")))

        self.assertEqual(code, 0)
        self.assertIn("downloading", out)

    def test_show_writes_no_line(self) -> None:
        self.run_tool(["--show", "--server", "31a63bbb"])

        self.assertEqual(self.lines(), [], "a read is not an attempt")


class NoKeyOnTheCommandLineTests(ToolCase):
    def test_there_is_no_key_option(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            tool.main(["--help"])

        self.assertNotIn("--key", out.getvalue(), "argv is world-readable in ps")

    def test_an_unknown_key_flag_is_refused(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            tool.main(["--show", "--server", "31a63bbb", "--key", "deadbeef"])


class StdlibOnlyTests(unittest.TestCase):
    """The tool must run on a fresh box with nothing installed."""

    def test_every_import_is_in_the_standard_library(self) -> None:
        tree = ast.parse((ROOT / "tools" / "servury_boot_image.py").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])

        self.assertTrue(imported)
        self.assertEqual(imported - sys.stdlib_module_names, set())

    def test_it_does_not_borrow_another_tool_s_client(self) -> None:
        # The record must not change behaviour when the cycle tool is refactored,
        # so this tool is a leaf: no sibling module on its import list.
        tree = ast.parse((ROOT / "tools" / "servury_boot_image.py").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])

        self.assertEqual(imported & {"servury_cycle", "servury_panel", "uki_cycle"}, set())


if __name__ == "__main__":
    unittest.main()
