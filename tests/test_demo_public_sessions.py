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
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPLY = ROOT / "deploy" / "demo-host" / "apply_demo_public.py"
CHECK = ROOT / "deploy" / "demo-host" / "check_demo_public.py"
CHECK_HOST = ROOT / "deploy" / "demo-host" / "check_demo_host.sh"


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


def host_report_fields(report: dict) -> str:
    """Run check_demo_host.sh's own field generator over one report object.

    The text is cut from the script, not copied: a copy would keep passing
    after the script's generator and its case arms drifted apart (round 2).
    """
    text = CHECK_HOST.read_text(encoding="utf-8")
    marker = """FIELDS=$(REPORT="$REPORT" python3 - <<'PY'"""
    start = text.index(marker)
    start = text.index("\n", start) + 1
    end = text.index("\nPY\n)", start)
    source = text[start:end]
    out = subprocess.run([sys.executable, "-c", source],
                         env=dict(os.environ, REPORT=json.dumps(report)),
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def meter_verdict(fields: str) -> str:
    """The check's meter case arm, run against one ``FIELDS`` line: ok or bad."""
    text = CHECK_HOST.read_text(encoding="utf-8")
    hit = text.index('*"meter_object=False"*')
    start = text.rindex('case "$FIELDS" in', 0, hit)
    end = text.index("\nesac", hit)
    block = text[start:end]
    script = ("ok() { echo ok; }\n"
              "bad() { echo bad; }\n"
              f"FIELDS={shlex.quote(fields)}\n{block}\nesac\n")
    out = subprocess.run(["bash", "-c", script], capture_output=True,
                         text=True, check=True)
    return out.stdout.strip()


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


class HostReportFieldTests(unittest.TestCase):
    """The machine-side check accepts the old report too (round 2's defect).

    ``--tenant`` uploads *this* check and reads the tenant's installed
    ``apply_demo_public.py --report``; until the next deploy that installed
    apply prints the old meter object. The generator must therefore print the
    old enabled/active/installed triple, and the case arm must accept the
    absent values while still failing a live meter. Without this test the
    generator can print one shape and the case arm match another — which is
    exactly what round 2 found.
    """

    def test_the_new_report_without_a_meter_object_passes(self) -> None:
        fields = host_report_fields({"config": {"near_ai_direct": True}})
        self.assertIn("meter_object=False", fields)
        self.assertEqual(meter_verdict(fields), "ok")

    def test_the_old_report_with_an_absent_meter_passes(self) -> None:
        fields = host_report_fields(
            {"meter": {"enabled": "not-found", "active": "inactive",
                       "unit_installed": False}})
        self.assertIn("meter_object=True", fields)
        self.assertIn("meter=not-found/inactive/False", fields)
        self.assertEqual(meter_verdict(fields), "ok")

    def test_the_old_report_with_a_live_meter_still_fails(self) -> None:
        fields = host_report_fields(
            {"meter": {"enabled": "enabled", "active": "active",
                       "unit_installed": True}})
        self.assertIn("meter=enabled/active/True", fields)
        self.assertEqual(meter_verdict(fields), "bad")


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


class SharedDemoConcurrencyTests(unittest.TestCase):
    """A judge's own traffic must not turn the check red (t_693bc8eb).

    The demo is a shared, public instance, so a session can appear at any
    moment. The check's cleanup used an exact global session count, and its
    deploy leg compared ``messages_sha256`` for sessions it does not own; one
    of the verifier's two live runs went red on exactly that (82 before, 83
    after, all eight of the check's own ids gone). The leak gate stays by id;
    the count and digest assertions are by id and by ownership.
    """

    def setUp(self) -> None:
        self.module = check_module()

    @staticmethod
    def _row(**fields) -> dict:
        row = {"exists": True, "started_at": 1.0, "pinned": 0, "hidden": 0,
               "archived": 0, "messages_sha256": "a" * 64, "messages": 2}
        row.update(fields)
        return row

    def test_an_external_session_between_the_snapshots_passes(self) -> None:
        before = {"local-seed", "local-console"}
        after = {"local-seed", "local-console", "local-judge-opened-midrun"}
        self.assertEqual(self.module.session_ids_problem(before, after, []), "")
        # The assertion this replaced was an exact global count equality, so
        # the judge's own session alone turned it red with the machine correct.
        self.assertNotEqual(len(after), len(before))

    def test_the_checks_own_console_session_may_go(self) -> None:
        """A reconnect replaces the console's row; the sweep deletes the old one."""
        before = {"local-seed", "local-console-1"}
        after = {"local-seed", "local-console-2"}
        self.assertEqual(
            self.module.session_ids_problem(before, after, ["local-console-1"]), "")

    def test_a_session_that_vanishes_still_fails(self) -> None:
        problem = self.module.session_ids_problem({"local-seed", "local-x"},
                                                  {"local-seed"}, [])
        self.assertIn("gone", problem)
        self.assertIn("local-x", problem)

    def test_an_unreadable_snapshot_fails(self) -> None:
        self.assertIn("could not be read",
                      self.module.session_ids_problem(None, {"local-seed"}, []))

    def test_an_external_turn_on_a_sampled_session_passes(self) -> None:
        before = {"check-owned": self._row(), "local-seed": self._row(),
                  "local-judge": self._row(messages_sha256="b" * 64)}
        after = {"check-owned": self._row(), "local-seed": self._row(),
                 "local-judge": self._row(messages_sha256="c" * 64, messages=3,
                                          last_activity_at=9.0)}
        sample = ["check-owned", "local-seed", "local-judge"]
        self.assertEqual(
            self.module.deploy_session_drift(sample, before, after,
                                             {"check-owned", "local-seed"}), [])
        # The old assertion compared this digest for every sampled session,
        # including the judge's, whose own turn moved it.
        self.assertNotEqual(before["local-judge"]["messages_sha256"],
                            after["local-judge"]["messages_sha256"])

    def test_an_owned_session_changing_still_fails(self) -> None:
        before = {"check-owned": self._row()}
        after = {"check-owned": self._row(messages_sha256="d" * 64)}
        self.assertTrue(self.module.deploy_session_drift(
            ["check-owned"], before, after, {"check-owned"}))

    def test_a_sampled_session_disappearing_still_fails(self) -> None:
        before = {"local-judge": self._row()}
        after = {"local-judge": {"id": "local-judge", "exists": False}}
        self.assertEqual(
            self.module.deploy_session_drift(["local-judge"], before, after, set()),
            ["local-judge: gone"])

    def test_the_ids_come_from_the_machines_own_store(self) -> None:
        commands: list[str] = []
        uploads: list[tuple[str, str]] = []

        class Console:
            def put(self, local: str, remote: str, *, mode: str = "0644") -> None:
                uploads.append((Path(local).read_text(encoding="utf-8"), remote))

            def run(self, command: str, check: bool = True) -> dict:
                commands.append(command)
                return {"code": 0, "stderr": "",
                        "stdout": json.dumps(["local-a", "local-b"])}

        ids, _ = self.module.tenant_session_ids(Console())
        self.assertEqual(ids, {"local-a", "local-b"})
        source, remote = uploads[0]
        self.assertIn("SELECT id FROM sessions", source)
        self.assertIn("mode=ro", source)
        self.assertEqual(remote, self.module.SESSION_IDS_REMOTE)
        # `shell.exec` refuses `python3 -c` (measured live 2026-11-18: 4001
        # dangerous_command), so the query must travel as a file it runs.
        self.assertEqual(commands[0], f"sudo /usr/bin/python3 {remote}")
        self.assertNotIn(" -c ", commands[0])
        # The staged copy is removed again, from /tmp, by a relative name.
        self.assertEqual(commands[-1], f"cd /tmp && rm -f {self.module.SESSION_IDS_NAME}")

    def test_an_answer_that_is_not_a_list_is_unreadable(self) -> None:
        class Console:
            def put(self, local: str, remote: str, *, mode: str = "0644") -> None:
                pass

            def run(self, command: str, check: bool = True) -> dict:
                return {"code": 1, "stderr": "no such file", "stdout": ""}

        ids, _ = self.module.tenant_session_ids(Console())
        self.assertIsNone(ids)


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
        # t_693bc8eb: the demo is shared, so the cleanup compares ids, never
        # an exact global count that a judge's own session moves.
        self.assertIn("every session the check found is still there", text)
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


class HostCheckSessionLeakTests(unittest.TestCase):
    """One run of check_demo_host.sh leaves no "Reply with exactly: ok" row.

    Savio, 2026-11-16: the demo's sidebar carried ~20 such rows, one per run
    of the host check — the socket leg mints a session and step 11's restart
    reaps it as a startup orphan. The check now deletes the id it minted, by
    id, with the tenant's own CLI, on the success and the failure path, and
    proves the delete with the machine's own `--report --session-id`.
    """

    def functions(self) -> str:
        """The check's own session-lifecycle shell functions, cut from the script."""
        text = CHECK_HOST.read_text(encoding="utf-8")
        start = text.index("count_check_prompt_rows() {")
        end = text.index("\n}\n", text.index("cleanup_on_exit() {")) + 3
        return text[start:end]

    def scratch(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="odile-host-cleanup-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        return directory

    def stub_sudo(self, directory: Path) -> tuple[Path, Path]:
        """A `sudo` that records its argv and answers with SUDO_RC."""
        binary = directory / "bin" / "sudo"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n"
                          "printf '%s\\n' \"$*\" >> \"$SUDO_LOG\"\n"
                          "exit \"${SUDO_RC:-0}\"\n", encoding="utf-8")
        binary.chmod(0o755)
        log = directory / "sudo.log"
        return binary.parent, log

    def stub_apply(self, directory: Path) -> None:
        """The installed report: exists follows REPORT_EXISTS, as the real one does."""
        (directory / "apply_demo_public.py").write_text(
            "import json, os, sys\n"
            "print(json.dumps({'id': sys.argv[-1],\n"
            "                  'exists': os.environ.get('REPORT_EXISTS') == '1'}))\n",
            encoding="utf-8")

    def run_functions(self, directory: Path, body: str, *args: str,
                      env: dict | None = None) -> subprocess.CompletedProcess:
        script = (
            "set -u\n"
            "DEMO_USER=tenant\n"
            "DEMO_HOME=/home/tenant\n"
            f"LIB={shlex.quote(str(directory))}\n"
            f"STATE_DB={shlex.quote(str(directory / 'state.db'))}\n"
            "CHECK_PROMPT='Reply with exactly: ok'\n"
            "PASS=0\nFAIL=0\n"
            "ok() { printf 'ok   %s\\n' \"$*\"; PASS=$((PASS + 1)); }\n"
            "bad() { printf 'FAIL %s\\n' \"$*\"; FAIL=$((FAIL + 1)); }\n"
            "note() { printf '     %s\\n' \"$*\"; }\n"
            "SESSION_CLEANED=0\n"
            f"{self.functions()}\n"
            f"{body} " + " ".join(shlex.quote(argument) for argument in args) + "\n"
            "rc=$?\n"
            "printf 'PASS=%d FAIL=%d\\n' \"$PASS\" \"$FAIL\"\n"
            "exit \"$rc\"\n"
        )
        environment = dict(os.environ, SUDO_LOG=str(directory / "sudo.log"),
                           PATH=f"{directory / 'bin'}:{os.environ['PATH']}")
        environment.update(env or {})
        return subprocess.run(["/bin/bash", "-c", script], env=environment,
                              capture_output=True, text=True)

    def test_the_minted_session_is_deleted_with_the_tenants_own_cli(self) -> None:
        directory = self.scratch()
        _, log = self.stub_sudo(directory)
        self.stub_apply(directory)
        session_id = "local-" + "a1" * 32
        result = self.run_functions(directory, "cleanup_check_session", session_id,
                                    env={"REPORT_EXISTS": "0"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok   the session this check minted is gone", result.stdout)
        self.assertIn("PASS=1 FAIL=0", result.stdout)
        self.assertEqual(
            log.read_text(encoding="utf-8").strip(),
            "-u tenant env HOME=/home/tenant HERMES_HOME=/home/tenant/.hermes"
            " /home/tenant/.hermes/hermes-agent/hermes sessions delete --yes " + session_id)

    def test_a_failed_delete_is_a_failure(self) -> None:
        directory = self.scratch()
        self.stub_sudo(directory)
        self.stub_apply(directory)
        result = self.run_functions(directory, "cleanup_check_session", "local-b2",
                                    env={"SUDO_RC": "1", "REPORT_EXISTS": "0"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL hermes sessions delete --yes local-b2", result.stdout)
        self.assertIn("PASS=0 FAIL=1", result.stdout)

    def test_a_row_that_survives_the_delete_is_a_failure(self) -> None:
        directory = self.scratch()
        self.stub_sudo(directory)
        self.stub_apply(directory)
        result = self.run_functions(directory, "cleanup_check_session", "local-c3",
                                    env={"REPORT_EXISTS": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL the session this check minted still exists", result.stdout)

    def test_the_quiet_cleanup_prints_nothing_and_still_deletes(self) -> None:
        directory = self.scratch()
        _, log = self.stub_sudo(directory)
        self.stub_apply(directory)
        result = self.run_functions(directory, "cleanup_check_session", "local-d4", "quiet",
                                    env={"REPORT_EXISTS": "0"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "PASS=0 FAIL=0")
        self.assertIn("sessions delete --yes local-d4", log.read_text(encoding="utf-8"))

    def test_the_exit_trap_deletes_a_session_the_run_left(self) -> None:
        directory = self.scratch()
        _, log = self.stub_sudo(directory)
        self.stub_apply(directory)
        result = self.run_functions(
            directory, "SESSION_ID=local-e5; cleanup_on_exit",
            env={"REPORT_EXISTS": "0", "JAR": "/nonexistent"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sessions delete --yes local-e5", log.read_text(encoding="utf-8"))
        self.assertIn("PASS=0 FAIL=0", result.stdout)

    def test_the_count_query_counts_only_the_checks_prompt_rows(self) -> None:
        """The fence's number is the store's own count, first user message only."""
        directory = self.scratch()
        con = sqlite3.connect(str(directory / "state.db"))
        con.executescript(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, message_count INTEGER);"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
            " content TEXT, display_order INTEGER);")
        rows = {
            "s-first": [("user", "Reply with exactly: ok", 1), ("assistant", "ok", 2)],
            "s-later": [("user", "hello", 1), ("user", "Reply with exactly: ok", 2)],
            "s-near": [("user", "Reply with exactly: ok.", 1)],
            "s-system": [("system", "rules", 1), ("user", "Reply with exactly: ok", 2)],
        }
        for session_id, messages in rows.items():
            con.execute("INSERT INTO sessions VALUES (?, NULL, ?)", (session_id, len(messages)))
            for role, content, order in messages:
                con.execute("INSERT INTO messages (session_id, role, content, display_order)"
                            " VALUES (?, ?, ?, ?)", (session_id, role, content, order))
        con.commit()
        con.close()
        result = self.run_functions(directory, "count_check_prompt_rows")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split("PASS=")[0].strip(), "2")

    def test_the_cleanup_runs_before_the_restart_leg(self) -> None:
        text = CHECK_HOST.read_text(encoding="utf-8")
        self.assertLess(text.index('cleanup_check_session "$SESSION_ID"'),
                        text.index('echo "== restart"'))
        # The before-count is taken before the socket mints a row.
        self.assertLess(text.index("SESSION_BEFORE=$(count_check_prompt_rows)"),
                        text.index('send_request(tls, 2, "session.create"'))

    def test_the_tenant_driver_hands_the_cleanup_its_own_user_and_home(self) -> None:
        text = CHECK_HOST.read_text(encoding="utf-8")
        self.assertIn('"$BACKUP_SHA" "$DEMO_USER" "$DEMO_HOME"', text)
        self.assertIn("console_user, console_home = sys.argv[7:9]", text)


class HostDriverConsoleTests(unittest.TestCase):
    """The --tenant driver deletes the console sessions it opened, by id."""

    def driver_function(self, name: str) -> str:
        text = CHECK_HOST.read_text(encoding="utf-8")
        start = text.index(f"def {name}(")
        end = text.index("\n\n\nclient = connect()", start)
        return text[start:end]

    def test_every_console_session_is_deleted_and_proved_gone(self) -> None:
        calls: list[str] = []

        class ConsoleError(RuntimeError):
            pass

        class FakeConsole:
            def run(self, command: str, check: bool = True) -> dict:
                calls.append(command)
                if "--report --session-id" in command:
                    return {"code": 0, "stdout": json.dumps({"id": "x", "exists": False})}
                return {"code": 0, "stdout": "Deleted session 'x'."}

        class FakeModule:
            pass

        FakeModule.ConsoleError = ConsoleError

        namespace = {
            "json": json,
            "tenant_console": FakeModule,
            "REMOTE_FILES": "odile-check-demo-host.sh odile-check-demo-host.log",
            "CONSOLES": ["local-a" + "0" * 64, "local-b" + "1" * 64],
            "console_user": "tenant",
            "console_home": "/home/tenant",
            "connect": lambda: (_ for _ in ()).throw(SystemExit(2)),
        }
        exec(compile(self.driver_function("cleanup"), "check_demo_host-driver", "exec"),
             namespace)
        namespace["cleanup"](FakeConsole())
        deletes = [call for call in calls if "sessions delete --yes" in call]
        reports = [call for call in calls if "--report --session-id" in call]
        self.assertEqual(len(deletes), 2)
        self.assertEqual(len(reports), 2)
        self.assertEqual(
            deletes[0],
            "sudo -u tenant env HOME=/home/tenant HERMES_HOME=/home/tenant/.hermes"
            " /home/tenant/.hermes/hermes-agent/hermes sessions delete --yes local-a"
            + "0" * 64)
        self.assertIn("local-b" + "1" * 64, deletes[1])

    def test_a_reconnected_console_session_is_recorded(self) -> None:
        class FakeConsole:
            session_id = ""

            def open_session(self) -> str:
                self.session_id = "local-" + "c7" * 32
                return self.session_id

        class FakeModule:
            class ConsoleError(RuntimeError):
                pass

        namespace = {"tenant_console": FakeModule, "CONSOLES": []}
        exec(compile(self.driver_function("tracked"), "check_demo_host-driver", "exec"),
             namespace)
        console = namespace["tracked"](FakeConsole())
        self.assertEqual(console.open_session(), "local-" + "c7" * 32)
        self.assertEqual(console.open_session(), "local-" + "c7" * 32)
        self.assertEqual(namespace["CONSOLES"], ["local-" + "c7" * 32])


if __name__ == "__main__":
    unittest.main()
