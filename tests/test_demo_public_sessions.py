"""The demo keeps a judge's session — no prune, no seed rewrite, no caps.

t_ee5aac66 (Savio, 2026-11-13): *"they are free to make changes, can see past
sessions, no unexpected reverts / locks / deletes, etc.; simply that this is a
shared instance."*

The apply program is pure Python, so the behaviour is tested here directly:
the seed is written once and never again, the receipt belongs to the seed, and
the config's turn and session limits are the owner's (the demo writes none).
The CLI surface is tested too: `--tune` and `--keep-hash` existed only for the
deleted boot re-assert.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPLY = ROOT / "deploy" / "demo-host" / "apply_demo_public.py"
CHECK = ROOT / "deploy" / "demo-host" / "check_demo_public.py"


def apply_module():
    spec = importlib.util.spec_from_file_location("odile_apply_sessions", APPLY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def check_module():
    spec = importlib.util.spec_from_file_location("odile_check_public", CHECK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SeedTests(unittest.TestCase):
    """The seed is a row plus two messages plus a receipt, written once."""

    def setUp(self) -> None:
        self.module = apply_module()
        self.con = self.module.sqlite3.connect(":memory:")
        self.con.executescript(self.module.SCHEMA)
        self.addCleanup(self.con.close)

    def messages(self) -> int:
        return self.con.execute("SELECT count(*) FROM messages WHERE session_id = ?",
                                (self.module.SESSION_ID,)).fetchone()[0]

    def test_a_fresh_database_gets_the_seed_and_its_receipt(self) -> None:
        self.assertTrue(self.module.insert_seed(self.con, 1000.0, "Welcome.\n"))
        self.assertTrue(self.module.receipt(self.con, 1000.0))
        row = self.con.execute("SELECT title, pinned, message_count FROM sessions"
                               " WHERE id = ?", (self.module.SESSION_ID,)).fetchone()
        self.assertEqual(row, (self.module.SESSION_TITLE, 1, 2))
        self.assertEqual(self.messages(), 2)

    def test_an_existing_seed_is_never_rewritten(self) -> None:
        self.module.insert_seed(self.con, 1000.0, "Welcome.\n")
        self.module.receipt(self.con, 1000.0)
        # A judge renames, unpins and archives it, and a later turn appends.
        self.con.execute("UPDATE sessions SET title = 'a judge renamed this', pinned = 0,"
                         " archived = 1, message_count = 5 WHERE id = ?",
                         (self.module.SESSION_ID,))
        self.con.execute("INSERT INTO messages (session_id, role, content, timestamp,"
                         " message_uid, active, compacted, display_order)"
                         " VALUES (?,?,?,?,?,?,?,?)",
                         (self.module.SESSION_ID, "assistant", "a later turn", 1500.0,
                          "uid", 1, 0, 3))
        receipt_key = self.module.RECEIPT_PREFIX + self.module.SESSION_ID
        before = self.con.execute("SELECT value FROM state_meta WHERE key = ?",
                                  (receipt_key,)).fetchone()[0]

        self.assertFalse(self.module.insert_seed(self.con, 2000.0, "Welcome.\n"))
        self.assertFalse(self.module.receipt(self.con, 2000.0))

        row = self.con.execute("SELECT title, pinned, archived, message_count FROM sessions"
                               " WHERE id = ?", (self.module.SESSION_ID,)).fetchone()
        self.assertEqual(row, ("a judge renamed this", 0, 1, 5))
        self.assertEqual(self.messages(), 3)
        self.assertEqual(self.con.execute("SELECT value FROM state_meta WHERE key = ?",
                                          (receipt_key,)).fetchone()[0], before)

    def test_a_recreated_seed_lifts_its_deletion_tombstone(self) -> None:
        """A delete writes a tombstone; a re-created seed that keeps it cannot answer."""
        tombstone = self.module.RETIRED_PREFIX + self.module.SESSION_ID
        self.module.insert_seed(self.con, 1000.0, "Welcome.\n")
        self.con.execute("INSERT INTO state_meta (key, value) VALUES (?, ?)",
                         (tombstone, '{"retired_at": 1, "own": true}'))
        self.assertTrue(self.module.clear_seed_tombstone(self.con))
        self.assertFalse(self.module.clear_seed_tombstone(self.con))
        self.assertEqual(self.messages(), 2)

        # The other path: the row is gone (a delete) and the tombstone is
        # still there; writing the seed back must lift it.
        self.con.execute("DELETE FROM messages WHERE session_id = ?", (self.module.SESSION_ID,))
        self.con.execute("DELETE FROM sessions WHERE id = ?", (self.module.SESSION_ID,))
        self.con.execute("INSERT INTO state_meta (key, value) VALUES (?, ?)",
                         (tombstone, '{"retired_at": 2, "own": true}'))
        self.assertTrue(self.module.insert_seed(self.con, 2000.0, "Welcome.\n"))
        self.assertEqual(self.con.execute("SELECT count(*) FROM state_meta WHERE key = ?",
                                          (tombstone,)).fetchone()[0], 0)
        self.assertEqual(self.messages(), 2)

    def test_a_seed_deleted_by_its_owner_is_written_again(self) -> None:
        """The seed row absent is the one case that writes it: a reinstall path."""
        self.module.insert_seed(self.con, 1000.0, "Welcome.\n")
        self.con.execute("DELETE FROM messages WHERE session_id = ?", (self.module.SESSION_ID,))
        self.con.execute("DELETE FROM sessions WHERE id = ?", (self.module.SESSION_ID,))

        self.assertTrue(self.module.insert_seed(self.con, 2000.0, "Welcome.\n"))
        self.assertEqual(self.messages(), 2)

    def test_no_cap_survives_in_the_receipt(self) -> None:
        frozen = self.module.rendered_receipt(0.0)["policy"]["config_json"]
        config = self.module.json.loads(frozen)
        self.assertNotIn("max_concurrent_sessions", config)
        self.assertNotIn("max_turns", (config.get("agent") or {}))


class ConfigTests(unittest.TestCase):
    """The demo owns four settings; the limits are the owner's."""

    def setUp(self) -> None:
        self.module = apply_module()

    def test_a_tuned_config_keeps_the_owners_limits(self) -> None:
        owner = ("model:\n  provider: custom\n"
                 "  base_url: https://cloud-api.near.ai/v1\n"
                 "agent:\n  max_turns: '150'\n"
                 "custom_providers:\n  - name: Near AI\n"
                 "    base_url: https://cloud-api.near.ai/v1\n"
                 "    key_env: HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY\n"
                 "webapp:\n  public_url: \"https://demo.odile.fyi\"\n"
                 "max_concurrent_sessions: 4\n")
        tuned, changed = self.module.tune_config(owner)
        self.assertEqual(tuned, owner)
        self.assertEqual(changed, [])

    def test_a_config_without_limits_gains_none(self) -> None:
        bare = ("model:\n  provider: custom\n"
                "custom_providers:\n  - name: Old Provider\n"
                "    base_url: http://127.0.0.1:8099/v1\n"
                "    key_env: OLD_PROVIDER_API_KEY\n"
                "webapp:\n  port: 9130\n")
        tuned, changed = self.module.tune_config(bare)
        self.assertNotIn("max_turns", tuned)
        self.assertNotIn("max_concurrent_sessions", tuned)
        self.assertNotIn("max_turns", changed)
        self.assertNotIn("max_concurrent_sessions", changed)

    def test_the_report_carries_the_session_count_the_check_compares(self) -> None:
        """The cleanup baseline is the store's own count, not the 20-row list."""
        text = APPLY.read_text(encoding="utf-8")
        self.assertIn("SELECT count(*) FROM sessions", text)
        self.assertIn('"session_count": session_count', text)

    def test_the_report_keeps_reading_the_agent_log(self) -> None:
        """t_098d9cee's max_iterations_lines is the exit line, not a cap value."""
        text = APPLY.read_text(encoding="utf-8")
        self.assertIn("def max_iterations_lines", text)
        self.assertIn("AGENT_LOG", text)
        self.assertIn("exits = max_iterations_lines()", text)


class NoBootReassertTests(unittest.TestCase):
    """The boot re-assert machinery is gone, and a leftover unit is removed."""

    def setUp(self) -> None:
        self.module = apply_module()

    def test_the_program_names_the_leftover_unit_but_ships_no_template(self) -> None:
        text = APPLY.read_text(encoding="utf-8")
        self.assertNotIn("UNIT_TEMPLATE", text)
        self.assertFalse(hasattr(self.module, "ensure_unit"))
        self.assertFalse(hasattr(self.module, "ensure_no_meter"))
        self.assertFalse(hasattr(self.module, "prune"))
        self.assertEqual(self.module.DEMO_UNIT, "odile-demo-public")

    def test_the_meter_adoption_shim_is_gone(self) -> None:
        """2026-11-14 13:55Z: the adoption was dead code, so it was deleted."""
        for name in ("METER_UNIT", "METER_KEY", "METER_UNIT_FILE",
                     "METER_URL_MARKERS", "_looks_like_meter"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(self.module, name))
        report = APPLY.read_text(encoding="utf-8")
        self.assertNotIn('"meter":', report)
        # The key is the profile's own, with the installer's root-only store
        # as the one fallback on a rebuilt machine.
        self.assertIn("KEY_STORE = Path(\"/etc/odile/key.env\")", report)

    def test_the_old_boot_flags_are_not_cli_options(self) -> None:
        parser = self.module.argparse.ArgumentParser()
        self.assertIn("--report", APPLY.read_text(encoding="utf-8"))
        options = {line.strip() for line in APPLY.read_text(encoding="utf-8").splitlines()
                   if 'parser.add_argument("--' in line}
        self.assertFalse(any("--tune" in line for line in options))
        self.assertFalse(any("keep-hash" in line for line in options))
        self.assertTrue(parser is not None)

    def test_removing_the_old_unit_stops_it_before_unlinking(self) -> None:
        """An already-run oneshot is `active (exited)`; status must not lie."""
        target = Path(tempfile.mkdtemp(prefix="odile-unit-")) / "odile-demo-public.service"
        self.addCleanup(lambda: target.parent.exists() and __import__("shutil").rmtree(
            target.parent, ignore_errors=True))
        target.write_text("[Unit]\nDescription=old\n", encoding="utf-8")
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(list(command))
            stdout = "active" if command[1] == "is-active" else "enabled"
            return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)

        with unittest.mock.patch.object(self.module, "UNIT_TARGET", target), \
                unittest.mock.patch.object(self.module, "run", fake_run):
            self.assertTrue(self.module.remove_boot_unit(False))
        self.assertFalse(target.exists())
        verbs = [command[1] for command in calls]
        self.assertIn("stop", verbs)
        self.assertIn("disable", verbs)
        self.assertIn("reset-failed", verbs)
        self.assertIn("daemon-reload", verbs)
        self.assertLess(verbs.index("stop"), verbs.index("disable"))
        self.assertLess(verbs.index("stop"), verbs.index("daemon-reload"))


class CheckCleanupTests(unittest.TestCase):
    """Every session the check opens is deleted, and a leak fails the check."""

    def setUp(self) -> None:
        self.module = check_module()
        self.module.OPENED.clear()
        self.addCleanup(self.module.OPENED.clear)

    def test_connect_opens_and_records_the_console_session(self) -> None:
        class FakeConsole:
            session_id = ""

            def open_session(self) -> str:
                self.session_id = "local-" + "c0" * 32
                return self.session_id

        fake = FakeConsole()
        with unittest.mock.patch.object(self.module.tenant_console, "connect_any",
                                        return_value=fake):
            console = self.module.connect("judge", "password")
        self.assertEqual(console.session_id, "local-" + "c0" * 32)
        self.assertEqual(self.module.OPENED, [console.session_id])

    def test_every_explicitly_opened_session_is_recorded(self) -> None:
        class FakeConsole:
            session_id = ""
            opened = 0

            def open_session(self) -> str:
                self.opened += 1
                self.session_id = f"local-{self.opened}"
                return self.session_id

        fake = FakeConsole()
        self.assertEqual(self.module.open_session(fake), "local-1")
        self.assertEqual(self.module.open_session(fake), "local-2")
        self.assertEqual(self.module.OPENED, ["local-1", "local-2"])

    def _machine(self, console_id: str, opened: list[str], *, delete_works: bool = True):
        """A fake tenant: the console's `--report --session-id` and the API."""
        rows = {session_id: True for session_id in opened + [console_id]}

        class Console:
            session_id = console_id

            def run(self, command: str, check: bool = True) -> dict:
                session_id = command.rsplit(" ", 1)[-1]
                return {"code": 0, "stderr": "",
                        "stdout": json.dumps({"id": session_id,
                                              "exists": session_id in rows})}

        class Machine:
            deleted: list[str] = []

            @staticmethod
            def http(url, *, method="GET", body=None, headers=None, cookie=""):
                if method == "DELETE":
                    session_id = url.split("/api/sessions/", 1)[1].split("?", 1)[0]
                    if session_id not in rows:
                        return 404, b'{"ok": false}', {}
                    if delete_works:
                        rows.pop(session_id)
                    Machine.deleted.append(session_id)
                    return 200, b'{"ok": true}', {}
                return 200, json.dumps({"sessions": [
                    {"id": session_id, "runtime_revision": 0, "runtime_generation": 0}
                    for session_id in rows]}).encode(), {}

        return Console(), Machine()

    def test_the_sweep_deletes_every_session_the_check_opened(self) -> None:
        console, machine = self._machine("local-console", ["local-a", "local-b"])
        self.module.OPENED.extend(["local-a", "local-b", "local-console"])
        with unittest.mock.patch.object(self.module, "http", machine.http):
            console, survivors = self.module.sweep_sessions(console, "password", "cookie")
        self.assertEqual(sorted(machine.deleted), ["local-a", "local-b"])
        self.assertEqual(survivors, [])
        # The console's own row stays: its shell is the report transport, and
        # the caller deletes it last.
        self.assertEqual(console.session_id, "local-console")

    def test_a_delete_that_does_not_delete_is_a_survivor(self) -> None:
        """The negative control: a 200 that leaves the row must fail the check."""
        console, machine = self._machine("local-console", ["local-a"], delete_works=False)
        self.module.OPENED.extend(["local-a", "local-console"])
        with unittest.mock.patch.object(self.module, "http", machine.http):
            _, survivors = self.module.sweep_sessions(console, "password", "cookie")
        self.assertEqual(survivors, ["local-a"])

    def test_a_503_is_retried(self) -> None:
        answers = iter([(503, b'{"detail":"storage_unavailable"}'),
                        (200, b'{"ok": true}')])
        calls: list[str] = []

        def fake_http(url, *, method="GET", body=None, headers=None, cookie=""):
            if method == "DELETE":
                calls.append("delete")
                code, text = next(answers)
                return code, text, {}
            return 200, b'{"sessions": [{"id": "local-a", "runtime_revision": 0}]}', {}

        with unittest.mock.patch.object(self.module, "http", fake_http), \
                unittest.mock.patch.object(self.module.time, "sleep", lambda *_: None):
            ok, code, _ = self.module.delete_session("cookie", "local-a")
        self.assertTrue(ok)
        self.assertEqual(code, 200)
        self.assertEqual(calls, ["delete", "delete"])


class CheckSurfaceTests(unittest.TestCase):
    """The live check asks for a fourth session, not for a cap."""

    def test_caps_are_replaced_by_a_fourth_session(self) -> None:
        text = CHECK.read_text(encoding="utf-8")
        self.assertNotIn("--caps", text)
        self.assertNotIn("max_concurrent_sessions", text)
        self.assertNotIn("max_turns", text)
        self.assertIn("--sessions", text)

    def test_the_check_asserts_no_demo_unit_and_no_demo_timer(self) -> None:
        text = CHECK.read_text(encoding="utf-8")
        self.assertIn("odile-demo-*", text)
        self.assertIn("list-unit-files", text)
        self.assertIn("list-timers", text)
        # The old unit's full name would fail the card's own fence.
        self.assertNotIn("odile-demo-public.service", text)

    def test_the_check_asserts_the_seed_by_id_and_messages(self) -> None:
        """A judge may rename or unpin the seed; that must not turn it red."""
        text = CHECK.read_text(encoding="utf-8")
        self.assertNotIn('session.get("title") ==', text)
        self.assertNotIn('row.get("title") or ""', text)
        self.assertIn("the seeded session is the constant id with its messages", text)
        self.assertIn("the seeded session is listed with its messages", text)

    def test_the_check_cleans_up_every_session_it_opens(self) -> None:
        text = CHECK.read_text(encoding="utf-8")
        self.assertIn("sweep_sessions", text)
        self.assertIn("the check leaves the session count where it found it", text)
        self.assertIn("every session the check opened is gone", text)
        self.assertIn("--report --session-id", text)
        # The list endpoint is a 20-row window; it must not decide existence.
        self.assertIn("at most 20 rows", text)


class DeployCleanupTests(unittest.TestCase):
    """The deploy leaves no console session behind either (t_ee5aac66)."""

    def test_the_deploy_records_and_deletes_every_console_session(self) -> None:
        text = (ROOT / "deploy" / "demo-host" / "deploy_demo_public.py").read_text(
            encoding="utf-8")
        self.assertIn("def open_console_session", text)
        self.assertIn("def cleanup_console_sessions", text)
        self.assertIn("return _run(argv)", text)
        # Three call sites: the first console, the one after the apply, and the
        # one after the restart (the definition carries a type annotation).
        self.assertEqual(text.count("open_console_session(console)"), 3)
        # The only raw `console.open_session()` is inside the recorder.
        self.assertEqual(text.count("console.open_session()"), 1)


if __name__ == "__main__":
    unittest.main()
