#!/usr/bin/python3
"""ADR-008 §3.5 — a planned drain hands a card attempt back instead of leaving it owned.

Measured 2026-11-28 on this host (Hermes checkout `e89b13c77b`). A `systemctl --user restart`
with a card running started a drain that waited its full 300 s for a turn that can never finish
inside the envelope; the card's submitter redialled the draining owner for its 180 s patience and
died with `Kanban owner silent for 181s: Gateway draining: not_ready`, the run was booked
`crashed` (the board's breaker is one failure here), and the owner's own teardown then missed the
shutdown watchdog at 360 s. The fix is entirely in the three files ADR-008 §6 assigns to `0002`:

* `session_kanban.record_drain_handoff` writes the run's `worker_lost` evidence and closes the run
  `reclaimed` in one board transaction: the card returns to its source lane (``ready``, or
  ``review`` for a reviewer run) with ``consecutive_failures`` reset, so a host restart neither
  leaves a card owned nor blocks one that merely happened to be in flight;
* `session_managed_worker.hand_off_kanban_turns` finds every live card attempt (a ``kanban_json``
  policy, spawned or still spawning), writes that hand-off, and only then sends the turn's Stop;
* `run_shutdown._drain_work_counts` subtracts those attempts, so the drain waits for cron/API work
  and not for a card run it has already handed back (``_hand_off_kanban_turns`` gives a turn that
  is just finishing a five-second grace first).

The ledger half needs no edit: the clean-exit funnel (`gateway/run.py::_exit_after_graceful_shutdown`
→ `_mark_exited`, and the watchdogs) already stamps `phase=exited`.

The behavioural group drives the real modules in a child interpreter (the same shape as
`test_gateway_unit_dropins.LockHeldStartPathTests`): a throwaway HERMES_HOME owns a real Kanban
board, and a fake authority exposes one live managed worker. It skips without a Hermes checkout
carrying the patch, so a public clone and a sibling card's worktree stay green.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "deploy" / "hermes-gateway" / "patches" / "0002-drain-and-reclaim.patch"
CHECKOUT = Path(os.environ.get("HERMES_CHECKOUT", "/root/.hermes/hermes-agent"))

#: ADR-008 §6: the only paths `0002` may touch.
ADR_FILES = {
    "gateway/run_shutdown.py",
    "gateway/session_managed_worker.py",
    "gateway/session_kanban.py",
}


def clean_env(**overrides: str) -> dict[str, str]:
    """This process's environment without git's hook plumbing (the 0001 test's rule)."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(overrides)
    return env


def patch_paths() -> set[str]:
    return {
        line.split(" b/", 1)[1].strip()
        for line in PATCH.read_text(encoding="utf-8").splitlines()
        if line.startswith("+++ b/")
    }


def checkout_carries_patch() -> bool:
    """The checkout exists and this patch is applied to it (the deployed state)."""
    if not (CHECKOUT / "gateway" / "run_shutdown.py").is_file() or not PATCH.is_file():
        return False
    done = subprocess.run(
        ["git", "-C", str(CHECKOUT), "apply", "--reverse", "--check", str(PATCH)],
        capture_output=True, text=True, env=clean_env(),
    )
    return done.returncode == 0


class PatchArtifactTests(unittest.TestCase):
    def test_the_patch_touches_exactly_the_adr_files(self) -> None:
        self.assertEqual(ADR_FILES, patch_paths())

    def test_the_patch_applies_to_the_measured_checkout(self) -> None:
        if not (CHECKOUT / "gateway" / "run_shutdown.py").is_file():
            self.skipTest("needs a Hermes checkout")
        with tempfile.TemporaryDirectory(prefix="odile-drain-patch-") as tmp:
            for rel in ADR_FILES:
                done = subprocess.run(
                    ["git", "-C", str(CHECKOUT), "show", f"HEAD:{rel}"],
                    capture_output=True, env=clean_env(),
                )
                if done.returncode != 0:
                    self.skipTest(f"{rel} is not at HEAD in the checkout")
                target = Path(tmp) / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(done.stdout)
            done = subprocess.run(
                ["git", "apply", "--check", str(PATCH)], cwd=tmp, capture_output=True, text=True,
                env=clean_env(),
            )
            self.assertEqual(0, done.returncode, done.stdout + done.stderr)

    def test_the_patch_does_not_edit_the_exit_funnel(self) -> None:
        # ADR-008 §3.5: the ledger funnel is in gateway/run.py and needs no change.
        self.assertNotIn("gateway/run.py", patch_paths())


# The child drives the real modules (not mocks) in its own interpreter, under a throwaway
# HERMES_HOME that owns a real board. Each mode prints one JSON object.
DRIVER = r'''
import asyncio, json, os, sqlite3, sys, time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.environ["HERMES_CHECKOUT"])
import hermes_bootstrap
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from gateway.session_kanban import record_drain_handoff
from gateway.session_managed_worker import hand_off_kanban_turns, kanban_turn_count
from gateway.run_shutdown import GatewayShutdownMixin

MODE = sys.argv[1]


def board(tid, lock, *, status="running", run_current=True):
    kb.init_db()
    db = str(kb.kanban_db_path())
    conn = kbc.connect()
    now = int(time.time())
    run_id = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, worker_pid, started_at) "
        "VALUES (?, 'builder', 'running', ?, ?, 5555, ?)", (tid, lock, now + 900, now - 60)).lastrowid
    conn.execute(
        "INSERT INTO tasks (id, title, assignee, status, created_at, claim_lock, claim_expires, "
        "worker_pid, worker_started_at, current_run_id, started_at, priority, consecutive_failures) "
        "VALUES (?, ?, 'builder', ?, ?, ?, ?, 5555, 'fp', ?, ?, 5, 3)",
        (tid, tid, status, now - 86400, lock, now + 900, run_id if run_current else None, now - 60))
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                 (tid, run_id, "claimed", json.dumps({"lock": lock, "source_status": "ready"}), now - 60))
    conn.commit()
    conn.close()
    return db, run_id


def policy_for(tid, run_id, lock, db):
    return SimpleNamespace(kanban_json=json.dumps(
        {"task_id": tid, "run_id": run_id, "claim_lock": lock, "db": db}))


def read(db, tid, run_id):
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    run = dict(conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone())
    events = [dict(row) for row in conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id", (tid,))]
    conn.close()
    return task, run, events


class Worker:
    def __init__(self):
        self.worker = (5555, "birth")
        self.process = SimpleNamespace(pid=5555)
        self.closed = __import__("threading").Event()
        self.kanban_handed_off = False
        self.stop_cause = None
        self.frames = []

    def control(self, frame):
        self.frames.append(frame)


class Authority:
    def __init__(self):
        self._managed_workers = {}
        self._managed_policies = {}
        self.sessions = {}
        self._mutation_tasks = set()
        self._side_agents = {}


class Runner:
    def __init__(self, authority):
        self.session_authority = authority
        self._running_agents = {}
    _running_agent_count = lambda self: 0
    _active_cron_job_count = lambda self: 1
    _active_api_run_count = lambda self: 0
    _active_deferred_agent_worker_count = lambda self: 0


out = {}
tid = "t_drain_probe"
lock = f"{kb._host_prefix()}770010"
db, run_id = board(tid, lock, status="review" if MODE == "moved" else "running",
                   run_current=(MODE != "moved"))
policy = policy_for(tid, run_id, lock, db)

if MODE == "handoff":
    out["ok"] = record_drain_handoff(policy, "Gateway restarting", pid=5555, bound=True)
    out["again"] = record_drain_handoff(policy, "Gateway restarting", pid=5555, bound=True)
    task, run, events = read(db, tid, run_id)
    out.update(task={k: task[k] for k in (
        "status", "claim_lock", "worker_pid", "current_run_id",
        "consecutive_failures", "last_failure_error")},
        run={k: run[k] for k in ("status", "outcome", "error")},
        kinds=[event["kind"] for event in events],
        lost=json.loads([event for event in events if event["kind"] == "worker_lost"][0]["payload"]))
elif MODE == "moved":
    out["ok"] = record_drain_handoff(policy, "Gateway restarting", pid=5555, bound=True)
    task, run, events = read(db, tid, run_id)
    out.update(status=task["status"], kinds=[event["kind"] for event in events])
elif MODE == "stop":
    authority = Authority()
    worker = Worker()
    authority._managed_workers["s1"] = worker
    authority._managed_policies["s1"] = policy
    runner = Runner(authority)
    out["count_before"] = kanban_turn_count(runner)
    out["closed"] = asyncio.run(hand_off_kanban_turns(runner, "Gateway restarting"))
    out["closed_again"] = asyncio.run(hand_off_kanban_turns(runner, "Gateway restarting"))
    out["frames"] = worker.frames
    out["stop_cause"] = worker.stop_cause
    out["counts"] = GatewayShutdownMixin._drain_work_counts(runner)
    task, run, events = read(db, tid, run_id)
    out.update(status=task["status"], kinds=[event["kind"] for event in events])
    authority._managed_policies.pop("s1")  # the turn settles and the policy map forgets it
    worker.closed.set()
    out["count_after_settle"] = kanban_turn_count(runner)
elif MODE == "cron":
    class CronRunner(Runner):
        _external_drain_active = False
        HARD_STOP_RESERVE_S = GatewayShutdownMixin.HARD_STOP_RESERVE_S

        def __init__(self):
            super().__init__(Authority())
            self.cron_until = time.monotonic() + 0.5
            self.statuses = []

        def _active_cron_job_count(self):
            return 1 if time.monotonic() < self.cron_until else 0

        _active_work_count = lambda self: 0

        def _update_runtime_status(self, state, *_a, **_k):
            self.statuses.append(state)

    CronRunner._enter_external_drain = GatewayShutdownMixin._enter_external_drain

    runner = CronRunner()
    budget, spent = asyncio.run(
        GatewayShutdownMixin._settle_cron_before_the_hard_drain(runner, 300.0, 360.0))
    out = {"budget": budget, "spent": spent, "external_drain": runner._external_drain_active,
           "statuses": runner.statuses}
    idle = CronRunner()
    idle.cron_until = 0.0
    out["idle"] = asyncio.run(
        GatewayShutdownMixin._settle_cron_before_the_hard_drain(idle, 300.0, 360.0))
    out["idle_drain"] = idle._external_drain_active
print(json.dumps(out))
'''


def driver_interpreter() -> str:
    """An interpreter that can import the checkout: this one, after ``hermes_bootstrap``."""
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import hermes_bootstrap; "
         "import gateway.session_kanban; print('ok')", str(CHECKOUT)],
        capture_output=True, text=True, timeout=120, env=clean_env(),
    )
    return sys.executable if probe.returncode == 0 else ""


@unittest.skipUnless(checkout_carries_patch(), "needs a Hermes checkout carrying the patch")
class DrainHandoffTests(unittest.TestCase):
    maxDiff = None

    def drive(self, mode: str) -> dict:
        home = tempfile.mkdtemp(prefix=f"odile-drain-{mode}-")
        self.addCleanup(shutil.rmtree, home, True)
        interpreter = driver_interpreter()
        if not interpreter:
            self.skipTest("this interpreter cannot import the Hermes checkout")
        env = clean_env(
            HERMES_HOME=home,
            HERMES_KANBAN_HOME=home,
            HERMES_CHECKOUT=str(CHECKOUT),
            HERMES_DISABLE_LAZY_INSTALLS="1",
        )
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
        env.pop("HERMES_KANBAN_DB", None)
        done = subprocess.run(
            [interpreter, "-c", DRIVER, mode], cwd=str(CHECKOUT), capture_output=True,
            text=True, timeout=300, env=env,
        )
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_a_planned_loss_writes_worker_lost_and_reclaims_the_card(self) -> None:
        out = self.drive("handoff")
        self.assertTrue(out["ok"], "the hand-off did not close the attempt")
        self.assertFalse(out["again"], "a second hand-off re-closed the same attempt")
        self.assertEqual("ready", out["task"]["status"])
        self.assertEqual((None, None, None), (
            out["task"]["claim_lock"], out["task"]["worker_pid"], out["task"]["current_run_id"]))
        self.assertEqual(0, out["task"]["consecutive_failures"], "a host restart charged the breaker")
        self.assertIsNone(out["task"]["last_failure_error"])
        self.assertEqual(("reclaimed", "reclaimed"), (out["run"]["status"], out["run"]["outcome"]))
        self.assertIn("managed worker lost: Gateway restarting", out["run"]["error"])
        self.assertEqual(["claimed", "worker_lost", "reclaimed"], out["kinds"])
        self.assertEqual("Gateway restarting", out["lost"]["reason"])
        self.assertTrue(out["lost"]["drain"])

    def test_a_card_that_moved_past_the_run_is_not_touched(self) -> None:
        out = self.drive("moved")
        self.assertFalse(out["ok"])
        self.assertEqual("review", out["status"])
        self.assertEqual(["claimed"], out["kinds"])

    def test_the_drain_hands_the_attempt_back_and_does_not_wait_it(self) -> None:
        out = self.drive("stop")
        self.assertEqual(1, out["count_before"])
        self.assertEqual(1, out["closed"])
        self.assertEqual(0, out["closed_again"], "the hand-off is not idempotent")
        self.assertEqual([{"type": "stop"}], out["frames"])
        self.assertEqual("Gateway restarting", out["stop_cause"], "the turn did not carry the drain cause")
        # One cron job is still waited for; the card attempt is not.
        self.assertEqual((0, 1, 0, 0, 0), tuple(out["counts"]))
        self.assertEqual("ready", out["status"])
        self.assertEqual(["claimed", "worker_lost", "reclaimed"], out["kinds"])
        self.assertEqual(0, out["count_after_settle"])

    def test_a_cron_run_inside_the_envelope_finishes_before_the_hard_drain(self) -> None:
        out = self.drive("cron")
        self.assertTrue(out["external_drain"], "the hand-out window did not refuse new platform work")
        self.assertEqual("draining", out["statuses"][0])
        self.assertGreater(out["spent"], 0.2, "the window did not wait for the cron run")
        self.assertLess(out["budget"], 300.0, "the hard drain kept the full budget after the wait")
        self.assertGreaterEqual(out["budget"], 1.0)
        self.assertEqual([300.0, 0.0], out["idle"], "an idle gateway paid the window")
        self.assertFalse(out["idle_drain"])


if __name__ == "__main__":
    unittest.main()
