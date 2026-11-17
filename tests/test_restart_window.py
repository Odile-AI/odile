"""The restart/update gate: a live board refuses a restart outside its window.

The audit of 17 November measured 86 workers killed without finishing; 27 of
them within seven minutes of a logged gateway stop, in bursts of six and seven
across unrelated cards. The restart is the killer, so the restart path gets one
gate: `tools/restart_window.py`. These tests drive it at the CLI boundary a
person uses, against a fixture board, with a stub `hermes` that records the
command it was asked to run — so a refusal is observed as "the stub never ran",
not as a sentence in a docstring.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "restart_window.py"
sys.path.insert(0, str(ROOT / "tools"))

import restart_window  # noqa: E402

SCHEMA = """
CREATE TABLE tasks (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    assignee        TEXT,
    status          TEXT NOT NULL,
    current_run_id  INTEGER,
    worker_pid      INTEGER,
    created_at      INTEGER NOT NULL
)
"""


def utc(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


class FixtureBoard:
    """A board database with the one predicate the gate reads."""

    def __init__(self, cards: list[dict] | None = None) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "kanban.db"
        con = sqlite3.connect(self.path)
        con.executescript(SCHEMA)
        for index, card in enumerate(cards or []):
            con.execute(
                "INSERT INTO tasks (id, title, assignee, status, current_run_id,"
                " worker_pid, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    card["id"],
                    card.get("title", "a card"),
                    card.get("assignee", "builder"),
                    card.get("status", "running"),
                    card.get("current_run_id", index + 1),
                    card.get("worker_pid"),
                    index,
                ),
            )
        con.commit()
        con.close()

    def set_status(self, task_id: str, status: str) -> None:
        con = sqlite3.connect(self.path)
        con.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        con.commit()
        con.close()

    def close(self) -> None:
        self.dir.cleanup()


class WindowTests(unittest.TestCase):
    def test_the_window_is_half_open(self) -> None:
        self.assertFalse(restart_window.in_window(utc("2026-11-17T02:59:59Z")))
        self.assertTrue(restart_window.in_window(utc("2026-11-17T03:00:00Z")))
        self.assertTrue(restart_window.in_window(utc("2026-11-17T03:29:59Z")))
        self.assertFalse(restart_window.in_window(utc("2026-11-17T03:30:00Z")))
        self.assertFalse(restart_window.in_window(utc("2026-11-17T15:30:00Z")))


class LiveRunTests(unittest.TestCase):
    def test_only_running_cards_are_live(self) -> None:
        board = FixtureBoard(
            [
                {"id": "t_live", "title": "doing the work"},
                {"id": "t_ready", "status": "ready"},
                {"id": "t_review", "status": "review"},
                {"id": "t_blocked", "status": "blocked"},
            ]
        )
        try:
            live = restart_window.live_runs(board.path)
        finally:
            board.close()
        self.assertEqual([run["id"] for run in live], ["t_live"])
        self.assertEqual(live[0]["title"], "doing the work")
        self.assertEqual(live[0]["assignee"], "builder")


class DecisionTests(unittest.TestCase):
    def test_a_quiet_board_allows_everything(self) -> None:
        for action in ("restart", "update"):
            with self.subTest(action=action):
                allowed, _ = restart_window.decide(
                    action, [], utc("2026-11-17T18:19:00Z")
                )
                self.assertTrue(allowed)

    def test_a_restart_with_a_live_run_refuses_outside_the_window(self) -> None:
        allowed, reason = restart_window.decide(
            "restart", [{"id": "t_live"}], utc("2026-11-17T18:19:00Z")
        )
        self.assertFalse(allowed)
        self.assertIn("03:00", reason)

    def test_a_restart_with_a_live_run_is_allowed_inside_the_window(self) -> None:
        allowed, _ = restart_window.decide(
            "restart", [{"id": "t_live"}], utc("2026-11-17T03:05:00Z")
        )
        self.assertTrue(allowed)

    def test_an_update_always_needs_zero_live_runs(self) -> None:
        for at in ("2026-11-17T18:19:00Z", "2026-11-17T03:05:00Z"):
            with self.subTest(at=at):
                allowed, reason = restart_window.decide(
                    "update", [{"id": "t_live"}], utc(at)
                )
                self.assertFalse(allowed)
                self.assertIn("zero live runs", reason)


class WaitTests(unittest.TestCase):
    def test_wait_returns_when_the_last_run_finishes(self) -> None:
        board = FixtureBoard([{"id": "t_live"}])
        ticks = []

        def fake_sleep(seconds: float) -> None:
            ticks.append(seconds)
            board.set_status("t_live", "done")

        try:
            remaining = restart_window.wait_for_idle(
                board.path, timeout=10, poll=2, sleep=fake_sleep
            )
        finally:
            board.close()
        self.assertEqual(remaining, [])
        self.assertEqual(ticks, [2])

    def test_wait_gives_up_at_its_deadline(self) -> None:
        board = FixtureBoard([{"id": "t_live"}])
        try:
            remaining = restart_window.wait_for_idle(
                board.path, timeout=5, poll=2, sleep=lambda _seconds: None
            )
        finally:
            board.close()
        self.assertEqual([run["id"] for run in remaining], ["t_live"])


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.log = self.dir / "hermes.log"
        self.stub = self.dir / "hermes"
        self.stub.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$@" >> "$STUB_LOG"\n',
            encoding="utf-8",
        )
        self.stub.chmod(0o755)
        self.env = dict(os.environ, STUB_LOG=str(self.log))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_tool(self, board: FixtureBoard, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(TOOL),
                "--db",
                str(board.path),
                "--hermes-bin",
                str(self.stub),
                *args,
            ],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )

    def test_a_restart_with_a_live_run_refuses_and_never_calls_hermes(self) -> None:
        board = FixtureBoard([{"id": "t_live", "title": "a long card"}])
        try:
            done = self.run_tool(
                board, "--at", "2026-11-17T18:19:00Z", "--apply"
            )
        finally:
            board.close()
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("t_live", done.stdout)
        self.assertIn("REFUSED", done.stdout)
        self.assertIn("03:00–03:30Z", done.stdout)
        self.assertFalse(self.log.exists(), "hermes ran on a refused restart")

    def test_an_update_with_a_live_run_refuses_and_never_calls_hermes(self) -> None:
        board = FixtureBoard([{"id": "t_live", "title": "a long card"}])
        try:
            done = self.run_tool(
                board, "--for", "update", "--at", "2026-11-17T03:05:00Z", "--apply"
            )
        finally:
            board.close()
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("REFUSED", done.stdout)
        self.assertIn("zero live runs", done.stdout)
        self.assertFalse(self.log.exists(), "hermes update ran on a live board")

    def test_a_restart_inside_the_window_runs_the_drain_aware_command(self) -> None:
        board = FixtureBoard([])
        try:
            done = self.run_tool(
                board, "--at", "2026-11-17T03:05:00Z", "--apply"
            )
        finally:
            board.close()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(
            self.log.read_text(encoding="utf-8").splitlines(),
            ["-p", "default", "gateway", "restart", "-y"],
        )

    def test_a_dry_run_names_the_command_and_changes_nothing(self) -> None:
        board = FixtureBoard([])
        try:
            done = self.run_tool(board, "--at", "2026-11-17T03:05:00Z")
        finally:
            board.close()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("would run:", done.stdout)
        self.assertIn("-p default gateway restart -y", done.stdout)
        self.assertFalse(self.log.exists())

    def test_update_on_a_quiet_board_runs_hermes_update(self) -> None:
        board = FixtureBoard([])
        try:
            done = self.run_tool(
                board, "--for", "update", "--at", "2026-11-17T18:19:00Z", "--apply"
            )
        finally:
            board.close()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(
            self.log.read_text(encoding="utf-8").splitlines(),
            ["-p", "default", "update", "--yes"],
        )


if __name__ == "__main__":
    unittest.main()
