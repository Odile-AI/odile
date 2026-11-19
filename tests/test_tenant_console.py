"""The console CLI deletes the session it opened (t_addab701).

`tenant_console.Console.shell()` needs a session, and Hermes keeps a row per
session, so every `--run`/`--put`/`--get` used to leave an untitled, empty row
in the judge's sidebar. The CLI's lifecycle deletes its own session by id when
it returns — on the success path and on the error path — and `--keep-session`
leaves it for a probe that wants to look at it.

The fake machine below answers the two calls the fence needs, so the tests
check the id, the fence parameters and the 404 rather than the source text.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "deploy" / "demo-host" / "tenant_console.py"


def console_module():
    """Load the console once: a second load would be a second ConsoleError."""
    if "odile_tenant_console_cli" in sys.modules:
        return sys.modules["odile_tenant_console_cli"]
    spec = importlib.util.spec_from_file_location("odile_tenant_console_cli", CONSOLE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Machine:
    """The tenant's dashboard API: one row per session, Hermes's fence."""

    def __init__(self, *, delete_codes: list[int] | None = None) -> None:
        self.rows: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.urls: list[str] = []
        self.delete_codes = list(delete_codes or [])

    def add(self, session_id: str, revision: int = 0, generation: int = 0) -> None:
        self.rows[session_id] = {"runtime_revision": revision,
                                 "runtime_generation": generation}

    def fetch(self, url: str, *, method: str = "GET", cookie: str = "") -> tuple[int, str]:
        self.urls.append(url)
        path = url.split("?", 1)[0]
        session_id = path.rsplit("/", 1)[-1]
        if method == "DELETE":
            if session_id not in self.rows:
                return 404, '{"detail":"Session not found"}'
            if self.delete_codes:
                code = self.delete_codes.pop(0)
                if code != 200:
                    return code, '{"detail":"storage_unavailable"}'
            self.rows.pop(session_id)
            self.deleted.append(session_id)
            return 200, json.dumps({"ok": True, "deleted_ids": [session_id]})
        if session_id in self.rows:
            return 200, json.dumps({"id": session_id, **self.rows[session_id]})
        return 404, '{"detail":"Session not found"}'


class FakeConsole:
    """The transport, without a socket: one session, one command."""

    def __init__(self, base: str = "https://demo.odile.fyi", *,
                 machine: Machine | None = None, fail: bool = False) -> None:
        self.base = base
        self.machine = machine
        self.fail = fail
        self.session_id = ""
        self.cookie = ""
        self.opened = 0
        self.ready = {"params": {"type": "gateway.ready"}}
        self.commands: list[str] = []

    def connect(self, username: str, password: str) -> None:
        self.cookie = "__Host-odile-dashboard=x"
        if self.machine is not None:
            self.machine.add("local-1", revision=0, generation=0)

    def open_session(self) -> str:
        self.opened += 1
        self.session_id = f"local-{self.opened}"
        return self.session_id

    def shell(self, command: str, *, timeout: float = 60) -> dict:
        self.commands.append(command)
        if self.fail:
            raise console_module().ConsoleError(
                "shell.exec -> 4001 dangerous_command")
        return {"code": 0, "stdout": "root\n", "stderr": ""}


class CliCleanupTests(unittest.TestCase):
    """`main()` leaves no session row behind, whatever it ran."""

    def setUp(self) -> None:
        self.module = console_module()
        self.machine = Machine()
        handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        handle.write("demo-password-for-the-tests")
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        self.password_file = handle.name

    def run_main(self, *extra: str, **console) -> int:
        fake = FakeConsole(machine=self.machine, **console)

        def factory(base: str = "https://demo.odile.fyi") -> FakeConsole:
            fake.base = base
            return fake

        self.console = fake
        with unittest.mock.patch.object(self.module, "Console", factory), \
                unittest.mock.patch.object(self.module, "fetch", self.machine.fetch), \
                unittest.mock.patch.object(self.module.time, "sleep", lambda *_: None), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.module.main(["--password-file", self.password_file,
                                     "--run", "id -un", *extra])
        self.stdout, self.stderr = out.getvalue(), err.getvalue()
        return code

    def test_a_run_deletes_the_session_it_opened(self) -> None:
        code = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.machine.deleted, ["local-1"])
        self.assertFalse(self.machine.rows, "the row survived the CLI")
        self.assertIn("local-1", self.stdout)

    def test_the_delete_carries_hermes_fence_and_is_verified_by_a_get(self) -> None:
        """A bare DELETE answers 409; the fence is the SPA's own query."""
        self.run_main()
        deletes = [url for url in self.machine.urls if "?" in url]
        self.assertEqual(len(deletes), 1)
        query = deletes[0].split("?", 1)[1]
        fields = dict(part.split("=", 1) for part in query.split("&"))
        self.assertEqual(sorted(fields), ["expected_generation", "expected_revision",
                                          "request_id"])
        self.assertEqual(fields["expected_revision"], "0")
        self.assertEqual(fields["expected_generation"], "0")
        self.assertEqual(len(fields["request_id"]), 32)
        # The proof is the row's own GET: 200 on the way in, 404 after.
        gets = [url for url in self.machine.urls if "?" not in url]
        self.assertEqual(len(gets), 2)
        self.assertEqual(self.machine.fetch(gets[-1])[0], 404)

    def test_the_revision_the_api_lists_is_the_one_the_delete_sends(self) -> None:
        machine = Machine()
        machine.add("local-1", revision=7, generation=3)

        class RevisionConsole(FakeConsole):
            def connect(self, username: str, password: str) -> None:
                self.cookie = "__Host-odile-dashboard=x"

        fake = RevisionConsole(machine=machine)
        with unittest.mock.patch.object(self.module, "Console", lambda base="": fake), \
                unittest.mock.patch.object(self.module, "fetch", machine.fetch), \
                unittest.mock.patch.object(self.module.time, "sleep", lambda *_: None), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.module.main(["--password-file", self.password_file,
                                               "--run", "id -un"]), 0)
        query = [url for url in machine.urls if "?" in url][0]
        self.assertIn("expected_revision=7", query)
        self.assertIn("expected_generation=3", query)
        self.assertEqual(machine.deleted, ["local-1"])

    def test_an_error_still_deletes_the_session(self) -> None:
        with self.assertRaises(self.module.ConsoleError):
            self.run_main(fail=True)
        self.assertEqual(self.machine.deleted, ["local-1"])
        self.assertFalse(self.machine.rows)

    def test_keep_session_leaves_the_row_for_a_probe(self) -> None:
        code = self.run_main("--keep-session")
        self.assertEqual(code, 0)
        self.assertEqual(self.machine.deleted, [])
        self.assertIn("local-1", self.machine.rows)
        self.assertIn("session kept: local-1", self.stdout)

    def test_a_503_is_retried(self) -> None:
        machine = Machine(delete_codes=[503, 200])
        machine.add("local-1")
        fake = FakeConsole(machine=machine)
        with unittest.mock.patch.object(self.module, "Console", lambda base="": fake), \
                unittest.mock.patch.object(self.module, "fetch", machine.fetch), \
                unittest.mock.patch.object(self.module.time, "sleep", lambda *_: None), \
                contextlib.redirect_stdout(io.StringIO()):
            code = self.module.main(["--password-file", self.password_file,
                                     "--run", "id -un"])
        self.assertEqual(code, 0)
        self.assertEqual(machine.deleted, ["local-1"])

    def test_a_surviving_session_fails_the_cli(self) -> None:
        """A leak must be visible to a script, not only in the sidebar."""
        machine = Machine(delete_codes=[503, 503, 503, 503])
        machine.add("local-1")
        fake = FakeConsole(machine=machine)

        def factory(base: str = "") -> FakeConsole:
            return fake

        with unittest.mock.patch.object(self.module, "Console", factory), \
                unittest.mock.patch.object(self.module, "fetch", machine.fetch), \
                unittest.mock.patch.object(self.module.time, "sleep", lambda *_: None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.module.main(["--password-file", self.password_file,
                                     "--run", "id -un"])
        self.assertEqual(code, 3)
        self.assertIn("local-1", machine.rows)
        self.assertIn("survived", err.getvalue())


class CookieTests(unittest.TestCase):
    """The delete needs the dashboard cookie, so `login` keeps it."""

    def test_login_remembers_the_cookie_it_returns(self) -> None:
        module = console_module()
        console = module.Console("https://demo.odile.fyi")

        def fake_post(url, payload, cookies):
            import http.cookiejar
            cookies.set_cookie(http.cookiejar.Cookie(
                version=0, name="__Host-odile-dashboard", value="abc", port=None,
                port_specified=False, domain="demo.odile.fyi", domain_specified=True,
                domain_initial_dot=False, path="/", path_specified=True, secure=True,
                expires=None, discard=True, comment=None, comment_url=None, rest={}))
            return "{}"

        with unittest.mock.patch.object(module.Console, "_post_json",
                                        staticmethod(fake_post)):
            cookie = console.login("judge", "demo-password-for-the-tests")
        self.assertEqual(cookie, "__Host-odile-dashboard=abc")
        self.assertEqual(console.cookie, cookie)


if __name__ == "__main__":
    unittest.main()
