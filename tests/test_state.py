#!/usr/bin/python3
"""Tests for the tenant state machine and reconcile (ODL-212, re-cut ODL-460).

Hermetic: a scratch SQLite file, a stub provider over `providers/base.py`, and
one recorded Servury `servers` response. No socket, no live API, no SSH.

The strict flow is `claimed -> provisioning -> first_boot -> first_visit ->
ready`; `waiting_for_ceremony` and `installing` are gone, and a v2 database is
migrated on open. The suite is built around three questions the card makes
non-negotiable:

* every declared transition is allowed, audited and tested — the edge loop at
  the top of `StateMachineTest` is the list, and a new edge without a test
  fails it;
* an old row keeps its identity and its release while the vocabulary moves;
* the database and the provider cannot disagree silently: missing, expired and
  unknown machines are separate findings, and a fix happens only with
  `--apply`.

    /usr/bin/python3 -m unittest tests.test_state -v
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odilectl import cli, state  # noqa: E402
from odilectl.providers import servury  # noqa: E402
from odilectl.providers.base import Machine, Provider  # noqa: E402
from tests.test_odilectl import (  # noqa: E402
    TEST_KEY,
    RecordedTransport,
    empty_servers_response,
    machine_row,
    servers_response,
)

# A fixed clock keeps every timestamp in this file explainable: the suite never
# sleeps and never depends on the wall clock.
NOW = 1_794_092_410
DAY = 24 * 60 * 60
SERVER_ID = "srv-7f3a2c"
SLUG = "odl-7f3a2c"


class Clock:
    """A settable epoch clock passed into the store."""

    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def path_to(target: str) -> list[str]:
    """The shortest declared path from `invited` to `target`, inclusive."""
    if target == "invited":
        return []
    paths = {"invited": []}
    queue = ["invited"]
    while queue:
        current = queue.pop(0)
        for nxt in state.TRANSITIONS[current]:
            if nxt not in paths:
                paths[nxt] = paths[current] + [nxt]
                queue.append(nxt)
    if target not in paths:
        raise AssertionError(f"{target} is not reachable from invited")
    return paths[target]


def machine(
    server_id: str = SERVER_ID,
    hostname: str = SLUG,
    *,
    machine_state: str = "active",
    expires_at: int = NOW + 10 * DAY,
    ipv4: str = "203.0.113.120",
    detail: dict | None = None,
) -> Machine:
    return Machine(
        server_id=server_id,
        hostname=hostname,
        state=machine_state,
        ipv4=ipv4,
        expires_at=expires_at,
        plan_id=113,
        detail=detail or {},
    )


class StubProvider(Provider):
    """The fixture provider: a recorded machine list, and a call ledger."""

    name = "fixture"

    def __init__(self, machines=()) -> None:
        self.machines = list(machines)
        self.calls: list[str] = []

    def create(self, spec):  # pragma: no cover - reconcile must never call it
        self.calls.append("create")
        raise AssertionError("reconcile must not create a machine")

    def poll(self, server_id: str, *, timeout: float = 900.0, interval: float = 10.0):  # pragma: no cover
        self.calls.append("poll")
        raise AssertionError("reconcile must not poll a machine")

    def power(self, server_id: str, action: str):  # pragma: no cover
        self.calls.append("power")
        raise AssertionError("reconcile must not power a machine")

    def terminate(self, server_id: str):  # pragma: no cover
        self.calls.append("terminate")
        raise AssertionError("reconcile must not terminate a machine")

    def list(self) -> list[Machine]:
        self.calls.append("list")
        return list(self.machines)


# The state schema as ODL-212 shipped it. The migration test writes this
# fixture, then opens it with the current code; the fixture is the only place
# the retired words remain, and it is data, not a code path.
LEGACY_V2_SCHEMA = """
CREATE TABLE tenant_state (
    slug               TEXT    PRIMARY KEY,
    state              TEXT    NOT NULL
                       CHECK (state IN ('invited', 'claimed', 'provisioning',
                                        'waiting_for_ceremony', 'installing',
                                        'active', 'waiting_for_owner',
                                        'suspended', 'destroyed')),
    server_id          TEXT    UNIQUE,
    hostname           TEXT,
    ipv4               TEXT,
    plan_id            INTEGER,
    release            TEXT,
    expires_at         INTEGER,
    initramfs_host_key TEXT,
    last_heartbeat     INTEGER,
    detail             TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(detail)),
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS tenant_state_state ON tenant_state(state);
CREATE INDEX IF NOT EXISTS tenant_state_heartbeat ON tenant_state(last_heartbeat);
CREATE INDEX IF NOT EXISTS tenant_state_server ON tenant_state(server_id);

CREATE TABLE tenant_state_events (
    id         INTEGER PRIMARY KEY,
    slug       TEXT    NOT NULL REFERENCES tenant_state(slug),
    kind       TEXT    NOT NULL
               CHECK (kind IN ('invited', 'claimed', 'provisioning',
                               'waiting_for_ceremony', 'installing', 'active',
                               'waiting_for_owner', 'suspended', 'destroyed')),
    from_state TEXT,
    to_state   TEXT    NOT NULL,
    detail     TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(detail)),
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS tenant_state_events_slug ON tenant_state_events(slug, id);
CREATE INDEX IF NOT EXISTS tenant_state_events_kind ON tenant_state_events(kind, id);

CREATE TRIGGER IF NOT EXISTS tenant_state_events_no_update
BEFORE UPDATE ON tenant_state_events
BEGIN
    SELECT RAISE(ABORT, 'tenant state events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS tenant_state_events_no_delete
BEFORE DELETE ON tenant_state_events
BEGIN
    SELECT RAISE(ABORT, 'tenant state events are append-only');
END;
"""


class TempStoreMixin(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="odile-state-")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.db_path = Path(self.directory) / "waitlist.db"
        self.clock = Clock()
        self.store = state.StateStore.open(self.db_path, clock=self.clock)
        self.addCleanup(self.store.close)

    def reopen(self) -> state.StateStore:
        """A second connection to the same file: proves the row is durable."""
        return state.StateStore.open(self.db_path, clock=self.clock)

    def advance(self, slug: str, target: str) -> state.Tenant:
        """Create a tenant at `invited` and walk it to `target`."""
        tenant = self.store.invite(slug)
        for step in path_to(target):
            tenant = self.store.transition(slug, step)
        return tenant

    def active_tenant(
        self,
        slug: str = SLUG,
        *,
        server_id: str = SERVER_ID,
        initramfs_host_key: str | None = "SHA256:recorded-initramfs-key",
        expires_at: int = NOW + 10 * DAY,
    ) -> state.Tenant:
        self.store.invite(slug)
        self.store.claim(slug)
        self.store.provision(
            slug,
            server_id=server_id,
            ipv4="203.0.113.120",
            plan_id=113,
            expires_at=expires_at,
            initramfs_host_key=initramfs_host_key,
        )
        self.store.await_boot(slug)
        self.store.start_first_visit(slug)
        return self.store.ready(slug)

    def count_events(self, slug: str | None = None) -> int:
        return len(self.store.events(slug))


# --------------------------------------------------------------- the machine


class StateMachineTest(TempStoreMixin):
    def test_the_declared_states_are_the_strict_flow_plus_the_operational_ones(self):
        self.assertEqual(
            state.STATES,
            (
                "invited",
                "claimed",
                "provisioning",
                "first_boot",
                "first_visit",
                "ready",
                "waiting_for_owner",
                "suspended",
                "destroyed",
            ),
        )
        self.assertEqual(set(state.TRANSITIONS), set(state.STATES))
        self.assertNotIn("waiting_for_ceremony", state.STATES)
        self.assertNotIn("installing", state.STATES)
        self.assertNotIn("active", state.STATES)

    def test_the_onboarding_edges_are_the_strict_flow_in_order(self):
        """The state table is the card: one path from claimed to ready."""
        self.assertEqual(state.TRANSITIONS["claimed"], ("provisioning", "destroyed"))
        self.assertEqual(state.TRANSITIONS["provisioning"], ("first_boot", "destroyed"))
        self.assertEqual(state.TRANSITIONS["first_boot"], ("first_visit", "destroyed"))
        self.assertEqual(state.TRANSITIONS["first_visit"], ("ready", "destroyed"))
        self.assertEqual(
            state.TRANSITIONS["ready"],
            ("waiting_for_owner", "suspended", "destroyed"),
        )
        self.assertEqual(
            state.TRANSITIONS["waiting_for_owner"],
            ("ready", "suspended", "destroyed"),
        )
        self.assertEqual(state.TRANSITIONS["suspended"], ("ready", "destroyed"))

    def test_every_onboarding_state_still_means_a_machine_at_the_provider(self):
        self.assertEqual(
            state.MACHINE_STATES,
            frozenset(
                {
                    "provisioning",
                    "first_boot",
                    "first_visit",
                    "ready",
                    "waiting_for_owner",
                    "suspended",
                }
            ),
        )

    def test_every_state_is_reachable_from_invited_and_destroyed_reopens(self):
        for target in state.STATES:
            with self.subTest(state=target):
                path = path_to(target)
                self.assertEqual(path[-1] if path else "invited", target)
        # The one edge back: destroyed is the end of a machine, not the tenant.
        self.assertEqual(state.TRANSITIONS["destroyed"], ("claimed",))

    def test_every_declared_transition_is_allowed_and_writes_one_event(self):
        """The edge list is the test: an edge without an event is not a transition."""
        edges = [
            (source, target)
            for source, targets in state.TRANSITIONS.items()
            for target in targets
        ]
        self.assertEqual(len(edges), 19)
        for source, target in edges:
            with self.subTest(edge=f"{source} -> {target}"):
                store_dir = tempfile.mkdtemp(prefix="odile-edge-")
                self.addCleanup(shutil.rmtree, store_dir, ignore_errors=True)
                store = state.StateStore.open(Path(store_dir) / "state.db", clock=Clock())
                self.addCleanup(store.close)
                slug = "odl-edge"
                store.invite(slug)
                for step in path_to(source):
                    store.transition(slug, step)
                before = len(store.events(slug))
                tenant = store.transition(slug, target, detail={"test": f"{source}->{target}"})
                self.assertEqual(tenant.state, target)
                events = store.events(slug)
                self.assertEqual(len(events), before + 1)
                event = events[-1]
                self.assertEqual(event.kind, target)
                self.assertEqual(event.from_state, source)
                self.assertEqual(event.to_state, target)
                self.assertEqual(event.detail["test"], f"{source}->{target}")

    def test_the_whole_lifecycle_records_one_event_per_move(self):
        """The named helpers, in the order the card prints, with one event each."""
        self.store.invite(SLUG)
        self.store.claim(SLUG)
        self.store.provision(
            SLUG,
            server_id=SERVER_ID,
            ipv4="203.0.113.120",
            expires_at=NOW + DAY,
            initramfs_host_key="SHA256:recorded-initramfs-key",
        )
        self.store.await_boot(SLUG)
        self.store.start_first_visit(SLUG)
        self.store.ready(SLUG)
        self.store.heartbeat(SLUG, at=NOW)
        self.store.park_for_owner(
            SLUG, observed_host_key="SHA256:recorded-initramfs-key", now=NOW + 601
        )
        self.store.heartbeat(SLUG, at=NOW + 700)
        self.store.suspend(SLUG, reason="provider maintenance")
        self.store.resume(SLUG)
        self.store.destroy(SLUG, reason="owner cancelled")
        self.assertEqual(
            [event.kind for event in self.store.events(SLUG)],
            [
                "invited",
                "claimed",
                "provisioning",
                "first_boot",
                "first_visit",
                "ready",
                "waiting_for_owner",
                "ready",
                "suspended",
                "ready",
                "destroyed",
            ],
        )
        self.assertEqual(self.store.get(SLUG).state, "destroyed")

    def test_a_note_merges_detail_without_moving_the_state(self) -> None:
        self.active_tenant(SLUG)
        before = self.count_events(SLUG)
        self.clock.now += 10
        noted = self.store.note(SLUG, detail={"boot_key": "ssh-ed25519 AAAA test"})
        self.assertEqual(noted.state, "ready")
        self.assertEqual(noted.detail["boot_key"], "ssh-ed25519 AAAA test")
        self.assertEqual(self.count_events(SLUG), before, "a note is not a transition")
        stamp = noted.updated_at
        again = self.store.note(SLUG, detail={"boot_key": "ssh-ed25519 AAAA test"})
        self.assertEqual(again.updated_at, stamp, "an unchanged note writes nothing")
        self.assertEqual(self.count_events(SLUG), before)

    def test_the_initramfs_host_key_is_dropped_once_the_strict_image_boots(self) -> None:
        tenant = self.active_tenant(SLUG)
        self.assertIsNotNone(tenant.initramfs_host_key)
        before = self.count_events(SLUG)
        dropped = self.store.forget_initramfs_host_key(SLUG)
        self.assertIsNone(dropped.initramfs_host_key)
        again = self.store.forget_initramfs_host_key(SLUG)
        self.assertIsNone(again.initramfs_host_key)
        self.assertEqual(self.count_events(SLUG), before)

    def test_start_first_visit_carries_the_record_detail(self) -> None:
        self.advance(SLUG, "first_boot")
        tenant = self.store.start_first_visit(
            SLUG, detail={"boot_key": "ssh-ed25519 AAAA test", "first_visit": "pending"})
        self.assertEqual(tenant.state, "first_visit")
        self.assertEqual(tenant.detail["first_visit"], "pending")
        event = self.store.events(SLUG)[-1]
        self.assertEqual((event.from_state, event.to_state),
                         ("first_boot", "first_visit"))
        self.assertEqual(event.detail["boot_key"], "ssh-ed25519 AAAA test")


    def test_an_illegal_transition_is_refused_and_writes_no_event(self):
        self.advance(SLUG, "ready")
        before = self.count_events(SLUG)
        with self.assertRaises(state.InvalidTransition):
            self.store.transition(SLUG, "provisioning")
        self.assertEqual(self.store.get(SLUG).state, "ready")
        self.assertEqual(self.count_events(SLUG), before)

    def test_a_repeated_transition_is_a_no_op(self):
        self.advance(SLUG, "ready")
        self.store.destroy(SLUG, reason="owner cancelled")
        before = self.count_events(SLUG)
        again = self.store.destroy(SLUG, reason="owner cancelled")
        self.assertEqual(again.state, "destroyed")
        self.assertEqual(self.count_events(SLUG), before)
        self.assertEqual(len([e for e in self.store.events(SLUG) if e.kind == "destroyed"]), 1)

    def test_an_unknown_state_name_is_refused(self):
        self.store.invite(SLUG)
        with self.assertRaises(ValueError):
            self.store.transition(SLUG, "running")
        self.assertEqual(self.store.get(SLUG).state, "invited")

    def test_an_unknown_tenant_is_refused(self):
        with self.assertRaises(state.UnknownTenant):
            self.store.get("odl-nope")
        with self.assertRaises(state.UnknownTenant):
            self.store.transition("odl-nope", "claimed")

    def test_invite_is_idempotent_and_writes_one_event(self):
        first = self.store.invite(SLUG)
        second = self.store.invite(SLUG)
        self.assertEqual(first.slug, second.slug)
        self.assertEqual(second.state, "invited")
        events = self.store.events(SLUG)
        self.assertEqual([event.kind for event in events], ["invited"])
        self.assertIsNone(events[0].from_state)

    def test_invite_refuses_to_reset_a_tenant_that_has_moved_on(self):
        self.store.invite(SLUG)
        self.store.claim(SLUG)
        with self.assertRaises(state.StateError):
            self.store.invite(SLUG)

    def test_a_claim_keeps_the_invite_detail(self):
        """Entering claimed clears machine facts, not the invite's own detail."""
        self.store.invite(SLUG, detail={"owner_email": "ada@example.com"})
        claimed = self.store.claim(SLUG)
        self.assertEqual(claimed.state, "claimed")
        self.assertEqual(claimed.detail["owner_email"], "ada@example.com")

    def test_a_claim_cannot_carry_machine_fields(self):
        """A claim is before the order; a machine id on it is a bug, not a write."""
        self.store.invite(SLUG)
        with self.assertRaises(TypeError):
            self.store.transition(SLUG, "claimed", server_id="srv-too-early")
        self.assertEqual(self.store.get(SLUG).state, "invited")

    def test_provision_records_the_machine_fields_and_the_host_key(self):
        self.store.invite(SLUG)
        self.store.claim(SLUG)
        tenant = self.store.provision(
            SLUG,
            server_id=SERVER_ID,
            ipv4="203.0.113.120",
            plan_id=113,
            expires_at=NOW + 31 * DAY,
            initramfs_host_key="SHA256:recorded-initramfs-key",
            detail={"raw_status": "deploying"},
        )
        self.assertEqual(tenant.state, "provisioning")
        self.assertEqual(tenant.server_id, SERVER_ID)
        self.assertEqual(tenant.ipv4, "203.0.113.120")
        self.assertEqual(tenant.plan_id, 113)
        self.assertEqual(tenant.expires_at, NOW + 31 * DAY)
        self.assertEqual(tenant.initramfs_host_key, "SHA256:recorded-initramfs-key")
        self.assertEqual(tenant.detail["raw_status"], "deploying")
        event = self.store.events(SLUG)[-1]
        self.assertEqual(
            (event.kind, event.from_state, event.to_state),
            ("provisioning", "claimed", "provisioning"),
        )

    def test_provision_records_the_release_it_was_ordered_to_boot(self):
        """The pin's provision_release is written in the same row as the order."""
        self.store.invite(SLUG)
        self.store.claim(SLUG)
        tenant = self.store.provision(
            SLUG,
            server_id=SERVER_ID,
            release="2026.11.1",
        )
        self.assertEqual(tenant.release, "2026.11.1")
        reopened = self.reopen()
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get(SLUG).release, "2026.11.1")

    def test_record_release_fills_a_null_release_once(self):
        """A second provision names the pin again; the first order wins."""
        self.store.invite(SLUG)
        self.store.claim(SLUG)
        self.store.provision(SLUG, server_id=SERVER_ID)
        self.assertIsNone(self.store.get(SLUG).release)
        first = self.store.record_release(SLUG, "2026.11.1")
        second = self.store.record_release(SLUG, "2026.12.0")
        self.assertEqual(first.release, "2026.11.1")
        self.assertEqual(second.release, "2026.11.1")

    def test_a_malformed_release_is_refused(self):
        self.store.invite(SLUG)
        self.store.claim(SLUG)
        self.store.provision(SLUG, server_id=SERVER_ID)
        with self.assertRaises(ValueError):
            self.store.record_release(SLUG, "2026")

    def test_a_transition_to_the_same_state_keeps_the_row_and_the_log(self):
        self.store.invite(SLUG)
        self.store.claim(SLUG)
        before = self.count_events(SLUG)
        self.assertEqual(self.store.claim(SLUG).state, "claimed")
        self.assertEqual(self.count_events(SLUG), before)

    def test_the_event_log_is_append_only(self):
        self.store.invite(SLUG)
        connection = self.store.connection
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE tenant_state_events SET kind = 'tampered'")
        connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM tenant_state_events")
        connection.rollback()
        self.assertEqual(self.count_events(SLUG), 1)

    def test_opening_the_store_twice_keeps_the_ledger(self):
        self.store.invite(SLUG)
        self.store.close()
        again = state.StateStore.open(self.db_path, clock=self.clock)
        try:
            self.assertEqual(again.get(SLUG).state, "invited")
            self.assertEqual([event.kind for event in again.events(SLUG)], ["invited"])
        finally:
            again.close()

    def test_timestamps_are_utc_iso_8601(self):
        tenant = self.store.invite(SLUG)
        self.assertRegex(tenant.created_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(tenant.created_at, tenant.updated_at)


# ---------------------------------------------------------------- reprovision


class ReprovisionTest(TempStoreMixin):
    """The one edge back: a destroyed slug comes back under the same slug.

    ADR-004 / ODL-218 follow-up. `destroyed` is the end of a machine, not of
    the tenant: the reopen keeps every earlier event, writes one `claimed`
    event with the reason, clears the dead machine's columns, and leaves the
    slug — and therefore the URL, the recovery file's name and the repository
    path — untouched.
    """

    FIELDS = (
        "server_id",
        "hostname",
        "ipv4",
        "plan_id",
        "release",
        "expires_at",
        "initramfs_host_key",
        "last_heartbeat",
    )

    def test_a_destroyed_tenant_reopens_to_claimed_and_clears_the_machine(self):
        self.active_tenant()
        self.store.destroy(SLUG, reason="owner cancelled")
        before = self.store.events(SLUG)
        reopened = self.store.reopen(
            SLUG, reason="reprovision: the provider lost the machine"
        )
        self.assertEqual(reopened.state, "claimed")
        self.assertEqual(reopened.slug, SLUG)
        for name in self.FIELDS:
            self.assertIsNone(getattr(reopened, name), name)
        self.assertTrue(reopened.detail["reprovision"])
        self.assertEqual(
            reopened.detail["reason"], "reprovision: the provider lost the machine"
        )
        events = self.store.events(SLUG)
        self.assertEqual(len(events), len(before) + 1)
        self.assertEqual(
            [event.kind for event in events[:-1]],
            [event.kind for event in before],
            "the reopen keeps every earlier event",
        )
        event = events[-1]
        self.assertEqual(
            (event.kind, event.from_state, event.to_state),
            ("claimed", "destroyed", "claimed"),
        )
        self.assertTrue(event.detail["reprovision"])
        self.assertEqual(
            event.detail["reason"], "reprovision: the provider lost the machine"
        )

    def test_a_destroyed_tenant_is_provisioned_again_under_the_same_slug(self):
        self.active_tenant()
        self.store.destroy(SLUG, reason="provider wipe")
        self.store.reopen(SLUG, reason="reprovision after a provider wipe")
        tenant = self.store.provision(
            SLUG,
            server_id="srv-new-0001",
            hostname=SLUG,
            ipv4="203.0.113.99",
            plan_id=113,
            release="2026.12.0",
            expires_at=NOW + 31 * DAY,
            initramfs_host_key="SHA256:new-machine-key",
        )
        self.assertEqual(tenant.slug, SLUG)
        self.assertEqual(tenant.server_id, "srv-new-0001")
        self.assertEqual(tenant.release, "2026.12.0")
        self.store.await_boot(SLUG)
        self.store.start_first_visit(SLUG)
        ready = self.store.ready(SLUG)
        self.assertEqual(ready.state, "ready")
        self.assertEqual(ready.slug, SLUG, "the slug survives the rebuild")
        # The URL is derived from the slug; it cannot have changed.
        self.assertEqual(f"https://{ready.slug}.odile.fyi", f"https://{SLUG}.odile.fyi")
        kinds = [event.kind for event in self.store.events(SLUG)]
        self.assertEqual(
            kinds[-6:],
            ["destroyed", "claimed", "provisioning", "first_boot", "first_visit", "ready"],
        )
        reopened = [event for event in self.store.events(SLUG) if event.kind == "claimed"][-1]
        self.assertEqual(reopened.from_state, "destroyed")
        self.assertEqual(reopened.detail["reason"], "reprovision after a provider wipe")

    def test_reopening_twice_writes_one_event(self):
        self.active_tenant()
        self.store.destroy(SLUG, reason="owner cancelled")
        before = self.count_events(SLUG)
        self.store.reopen(SLUG, reason="rebuild")
        again = self.store.reopen(SLUG, reason="rebuild")
        self.assertEqual(again.state, "claimed")
        self.assertEqual(self.count_events(SLUG), before + 1)

    def test_reopen_refuses_a_live_tenant(self):
        self.active_tenant()
        with self.assertRaises(state.InvalidTransition):
            self.store.reopen(SLUG, reason="rebuild")
        self.assertEqual(self.store.get(SLUG).state, "ready")

    def test_reconcile_apply_marks_missing_destroyed_and_names_the_same_slug(self):
        self.active_tenant()
        report = state.reconcile(self.store, StubProvider([]), now=NOW)
        finding = report.drift[0]
        self.assertEqual(finding.fix, "destroy")
        self.assertIn(SLUG, finding.detail["rebuild"])
        self.assertIn("same slug", finding.detail["rebuild"])
        applied = state.reconcile(self.store, StubProvider([]), apply=True, now=NOW)
        self.assertEqual(applied.fixed, (SLUG,))
        self.assertEqual(self.store.get(SLUG).state, "destroyed")
        # And the way back the finding names works from here.
        self.store.reopen(SLUG, reason="reprovision after reconcile")
        self.assertEqual(self.store.get(SLUG).state, "claimed")
        self.assertEqual(self.store.get(SLUG).slug, SLUG)


# ------------------------------------------------------------- waiting for owner


class WaitingForOwnerTest(TempStoreMixin):
    def test_a_stopped_heartbeat_and_the_recorded_host_key_parks_the_tenant(self):
        self.active_tenant()
        self.store.heartbeat(SLUG, at=NOW)
        moved = self.store.park_for_owner(
            SLUG, observed_host_key="SHA256:recorded-initramfs-key", now=NOW + 601
        )
        self.assertTrue(moved)
        tenant = self.store.get(SLUG)
        self.assertEqual(tenant.state, "waiting_for_owner")
        event = [e for e in self.store.events(SLUG) if e.kind == "waiting_for_owner"][-1]
        self.assertEqual(event.from_state, "ready")
        self.assertEqual(event.detail["reason"], "heartbeat stopped")
        self.assertEqual(event.detail["heartbeat_age_seconds"], 601)
        self.assertEqual(event.detail["initramfs_host_key"], "SHA256:recorded-initramfs-key")

    def test_a_fresh_heartbeat_does_not_park_the_tenant(self):
        self.active_tenant()
        self.store.heartbeat(SLUG, at=NOW)
        moved = self.store.park_for_owner(
            SLUG, observed_host_key="SHA256:recorded-initramfs-key", now=NOW + 300
        )
        self.assertFalse(moved)
        self.assertEqual(self.store.get(SLUG).state, "ready")
        self.assertEqual([e for e in self.store.events(SLUG) if e.kind == "waiting_for_owner"], [])

    def test_a_fresh_heartbeat_does_not_need_the_host_key(self):
        """A beating machine is not waiting, so the key question never arises."""
        self.active_tenant(initramfs_host_key=None)
        self.store.heartbeat(SLUG, at=NOW)
        moved = self.store.park_for_owner(
            SLUG, observed_host_key="SHA256:anything", now=NOW + 60
        )
        self.assertFalse(moved)
        self.assertEqual(self.store.get(SLUG).state, "ready")

    def test_a_different_host_key_does_not_move_the_tenant(self):
        """A changed key is not the recorded initramfs; do not claim it is."""
        self.active_tenant()
        self.store.heartbeat(SLUG, at=NOW)
        with self.assertRaises(state.HostKeyMismatch):
            self.store.park_for_owner(
                SLUG, observed_host_key="SHA256:somebody-else", now=NOW + 601
            )
        self.assertEqual(self.store.get(SLUG).state, "ready")
        self.assertEqual([e for e in self.store.events(SLUG) if e.kind == "waiting_for_owner"], [])

    def test_without_a_recorded_host_key_there_is_no_claim(self):
        self.active_tenant(initramfs_host_key=None)
        self.store.heartbeat(SLUG, at=NOW)
        with self.assertRaises(state.HostKeyUnknown):
            self.store.park_for_owner(
                SLUG, observed_host_key="SHA256:recorded-initramfs-key", now=NOW + 601
            )
        self.assertEqual(self.store.get(SLUG).state, "ready")

    def test_a_returning_heartbeat_moves_the_tenant_back_to_ready(self):
        self.active_tenant()
        self.store.heartbeat(SLUG, at=NOW)
        self.store.park_for_owner(
            SLUG, observed_host_key="SHA256:recorded-initramfs-key", now=NOW + 601
        )
        tenant = self.store.heartbeat(SLUG, at=NOW + 700)
        self.assertEqual(tenant.state, "ready")
        event = self.store.events(SLUG)[-1]
        self.assertEqual((event.from_state, event.to_state), ("waiting_for_owner", "ready"))
        self.assertEqual(event.detail["reason"], "heartbeat returned")

    def test_parking_twice_writes_one_event(self):
        self.active_tenant()
        self.store.heartbeat(SLUG, at=NOW)
        self.assertTrue(
            self.store.park_for_owner(
                SLUG, observed_host_key="SHA256:recorded-initramfs-key", now=NOW + 601
            )
        )
        self.assertFalse(
            self.store.park_for_owner(
                SLUG, observed_host_key="SHA256:recorded-initramfs-key", now=NOW + 900
            )
        )
        self.assertEqual(len([e for e in self.store.events(SLUG) if e.kind == "waiting_for_owner"]), 1)

    def test_parking_needs_a_ready_tenant(self):
        self.advance(SLUG, "first_visit")
        self.assertFalse(
            self.store.park_for_owner(
                SLUG, observed_host_key="SHA256:recorded-initramfs-key", now=NOW + 10_000
            )
        )
        self.assertEqual(self.store.get(SLUG).state, "first_visit")


# ------------------------------------------------------------------ reconcile


class StateMigrationTest(unittest.TestCase):
    """`tenant_state.release` arrives on an old table without losing a row."""

    def test_open_adds_release_to_an_old_state_table_and_is_idempotent(self):
        directory = tempfile.mkdtemp(prefix="odile-state-migrate-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        db_path = Path(directory) / "waitlist.db"
        con = sqlite3.connect(db_path)
        try:
            con.executescript(state.SCHEMA)
            con.execute("ALTER TABLE tenant_state DROP COLUMN release")
            con.execute(
                "INSERT INTO tenant_state (slug, state, detail, created_at, updated_at)"
                " VALUES ('odl-aa11bb', 'invited', '{\"owner_email\": \"ada@example.com\"}',"
                " '2026-11-08T00:00:00Z', '2026-11-08T00:00:00Z')"
            )
            con.commit()
        finally:
            con.close()

        first = state.StateStore.open(db_path)
        try:
            self.assertIsNone(first.get("odl-aa11bb").release)
            self.assertEqual(first.get("odl-aa11bb").state, "invited")
            self.assertEqual(
                first.get("odl-aa11bb").detail["owner_email"], "ada@example.com"
            )
        finally:
            first.close()
        second = state.StateStore.open(db_path)
        try:
            self.assertIsNone(second.get("odl-aa11bb").release)
        finally:
            second.close()

    def test_a_v2_database_migrates_to_the_strict_flow_and_keeps_its_history(self):
        """One row per claim survives; the words map and the log keeps its order."""
        directory = tempfile.mkdtemp(prefix="odile-state-v2-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        db_path = Path(directory) / "waitlist.db"
        stamp = "2026-11-08T00:00:00Z"
        rows = (
            ("odl-aa0001", "invited"),
            ("odl-aa0002", "claimed"),
            ("odl-aa0003", "provisioning"),
            ("odl-aa0004", "waiting_for_ceremony"),
            ("odl-aa0005", "installing"),
            ("odl-aa0006", "active"),
            ("odl-aa0007", "waiting_for_owner"),
            ("odl-aa0008", "suspended"),
            ("odl-aa0009", "destroyed"),
        )
        con = sqlite3.connect(db_path)
        try:
            con.executescript(LEGACY_V2_SCHEMA)
            for slug, legacy_state in rows:
                con.execute(
                    "INSERT INTO tenant_state (slug, state, server_id, release, detail,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        slug,
                        legacy_state,
                        f"srv-{slug[-4:]}",
                        "2026.11.1",
                        '{"owner_email": "ada@example.com"}',
                        stamp,
                        stamp,
                    ),
                )
            con.execute(
                "INSERT INTO tenant_state_events (slug, kind, from_state, to_state,"
                " detail, created_at) VALUES"
                " ('odl-aa0004', 'waiting_for_ceremony', 'provisioning',"
                " 'waiting_for_ceremony', '{}', ?)",
                (stamp,),
            )
            con.execute(
                "INSERT INTO tenant_state_events (slug, kind, from_state, to_state,"
                " detail, created_at) VALUES"
                " ('odl-aa0006', 'active', 'installing', 'active', '{}', ?)",
                (stamp,),
            )
            con.commit()
        finally:
            con.close()

        expected = {
            "odl-aa0001": "invited",
            "odl-aa0002": "claimed",
            "odl-aa0003": "provisioning",
            "odl-aa0004": "first_boot",
            "odl-aa0005": "first_visit",
            "odl-aa0006": "ready",
            "odl-aa0007": "waiting_for_owner",
            "odl-aa0008": "suspended",
            "odl-aa0009": "destroyed",
        }
        store = state.StateStore.open(db_path)
        try:
            self.assertEqual({t.slug: t.state for t in store.tenants()}, expected)
            for slug, wanted in expected.items():
                tenant = store.get(slug)
                self.assertEqual(tenant.state, wanted)
                self.assertEqual(tenant.release, "2026.11.1")
                self.assertEqual(tenant.detail["owner_email"], "ada@example.com")
            first = store.events("odl-aa0004")[-1]
            self.assertEqual(
                (first.kind, first.from_state, first.to_state),
                ("first_boot", "provisioning", "first_boot"),
            )
            second = store.events("odl-aa0006")[-1]
            self.assertEqual(
                (second.kind, second.from_state, second.to_state),
                ("ready", "first_visit", "ready"),
            )
            # The rebuilt CHECK accepts the new vocabulary and refuses the old.
            migrated = store.transition("odl-aa0004", "first_visit")
            self.assertEqual(migrated.state, "first_visit")
            with self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute(
                    "INSERT INTO tenant_state (slug, state, detail, created_at,"
                    " updated_at) VALUES ('odl-aa000a', 'active', '{}', ?, ?)",
                    (stamp, stamp),
                )
            store.connection.rollback()
            # The log is still append-only after the rebuild.
            with self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute(
                    "UPDATE tenant_state_events SET kind = 'tampered'"
                )
            store.connection.rollback()
            schema_sql = store.connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'tenant_state'"
            ).fetchone()[0]
            self.assertNotIn("waiting_for_ceremony", schema_sql)
            self.assertNotIn("'installing'", schema_sql)
        finally:
            store.close()

        again = state.StateStore.open(db_path)
        try:
            self.assertEqual(len(again.tenants()), len(rows))
            self.assertEqual(again.get("odl-aa0004").state, "first_visit")
            self.assertEqual(len(again.events("odl-aa0006")), 1)
        finally:
            again.close()


class ReconcileTest(TempStoreMixin):
    def test_a_matching_tenant_and_machine_are_not_drift(self):
        self.active_tenant()
        report = state.reconcile(self.store, StubProvider([machine()]), now=NOW)
        self.assertEqual(report.drift, ())
        self.assertEqual(report.tenants, 1)
        self.assertEqual(report.machines, 1)

    def test_reconcile_covers_every_onboarding_state(self):
        """A machine behind any of the new states is a match, not drift."""
        for index, target in enumerate(sorted(state.MACHINE_STATES)):
            with self.subTest(state=target):
                slug = f"odl-{index:06d}"
                self.advance(slug, target)
                provider = StubProvider(
                    [machine(server_id=f"srv-{index:06d}", hostname=slug)]
                )
                report = state.reconcile(self.store, provider, now=NOW, slug=slug)
                self.assertEqual(report.drift, ())
                self.assertEqual(report.tenants, 1)

    def test_a_first_boot_tenant_with_no_machine_is_missing_and_destroyable(self):
        self.store.invite(SLUG)
        self.store.claim(SLUG)
        self.store.provision(SLUG, server_id=SERVER_ID)
        self.store.await_boot(SLUG)
        report = state.reconcile(self.store, StubProvider([]), now=NOW)
        self.assertEqual([drift.kind for drift in report.drift], ["missing_vm"])
        self.assertEqual(report.drift[0].fix, "destroy")
        self.assertEqual(report.drift[0].detail["tenant_state"], "first_boot")
        applied = state.reconcile(self.store, StubProvider([]), apply=True, now=NOW)
        self.assertEqual(applied.fixed, (SLUG,))
        self.assertEqual(self.store.get(SLUG).state, "destroyed")

    def test_an_expired_first_visit_machine_is_manual_drift(self):
        self.advance(SLUG, "first_visit")
        report = state.reconcile(
            self.store,
            StubProvider([machine(expires_at=NOW - DAY)]),
            now=NOW,
        )
        self.assertEqual([drift.kind for drift in report.drift], ["expired_vm"])
        self.assertEqual(report.drift[0].fix, "manual")

    def test_a_missing_machine_is_drift_and_a_dry_run_changes_nothing(self):
        self.active_tenant()
        provider = StubProvider([])
        report = state.reconcile(self.store, provider, now=NOW)
        self.assertEqual([drift.kind for drift in report.drift], ["missing_vm"])
        self.assertEqual(report.drift[0].slug, SLUG)
        self.assertEqual(report.drift[0].fix, "destroy")
        self.assertFalse(report.applied)
        self.assertEqual(self.store.get(SLUG).state, "ready")
        self.assertEqual(provider.calls, ["list"])

    def test_apply_marks_a_missing_machine_destroyed_and_events_it(self):
        self.active_tenant()
        before = self.count_events(SLUG)
        report = state.reconcile(self.store, StubProvider([]), apply=True, now=NOW)
        self.assertTrue(report.applied)
        self.assertEqual(report.fixed, (SLUG,))
        self.assertEqual(self.store.get(SLUG).state, "destroyed")
        self.assertEqual(self.count_events(SLUG), before + 1)
        event = self.store.events(SLUG)[-1]
        self.assertEqual((event.from_state, event.to_state), ("ready", "destroyed"))
        self.assertIn("reconcile", event.detail["reason"])

    def test_a_second_apply_is_a_no_op(self):
        self.active_tenant()
        state.reconcile(self.store, StubProvider([]), apply=True, now=NOW)
        second = state.reconcile(self.store, StubProvider([]), apply=True, now=NOW)
        self.assertEqual(second.drift, ())
        self.assertEqual(second.fixed, ())

    def test_an_expired_machine_is_drift(self):
        self.active_tenant(expires_at=NOW - DAY)
        report = state.reconcile(
            self.store, StubProvider([machine(expires_at=NOW - DAY)]), now=NOW
        )
        self.assertEqual([drift.kind for drift in report.drift], ["expired_vm"])
        self.assertEqual(report.drift[0].fix, "suspend")
        self.assertEqual(report.drift[0].detail["expired_days"], 1)

    def test_apply_marks_an_expired_machine_suspended(self):
        self.active_tenant(expires_at=NOW - DAY)
        report = state.reconcile(
            self.store, StubProvider([machine(expires_at=NOW - DAY)]), apply=True, now=NOW
        )
        self.assertEqual(report.fixed, (SLUG,))
        self.assertEqual(self.store.get(SLUG).state, "suspended")
        self.assertEqual(self.store.events(SLUG)[-1].kind, "suspended")
        again = state.reconcile(
            self.store, StubProvider([machine(expires_at=NOW - DAY)]), apply=True, now=NOW
        )
        self.assertEqual(again.drift, ())

    def test_a_provider_expired_flag_is_drift_even_with_a_future_date(self):
        self.active_tenant()
        provider = StubProvider([machine(detail={"expired": True})])
        report = state.reconcile(self.store, provider, now=NOW)
        self.assertEqual([drift.kind for drift in report.drift], ["expired_vm"])
        self.assertTrue(report.drift[0].detail["provider_expired"])

    def test_an_unknown_machine_is_drift_and_apply_does_not_adopt_it(self):
        provider = StubProvider([machine(server_id="srv-orphan", hostname="odl-orphan")])
        report = state.reconcile(self.store, provider, apply=True, now=NOW)
        self.assertEqual([drift.kind for drift in report.drift], ["unknown_vm"])
        self.assertEqual(report.drift[0].slug, "odl-orphan")
        self.assertEqual(report.drift[0].fix, "manual")
        self.assertEqual(report.fixed, ())
        self.assertEqual(self.store.tenants(), ())

    def test_a_machine_still_running_behind_a_destroyed_tenant_is_drift(self):
        self.active_tenant()
        self.store.destroy(SLUG, reason="owner cancelled")
        report = state.reconcile(self.store, StubProvider([machine()]), now=NOW)
        self.assertEqual([drift.kind for drift in report.drift], ["zombie_vm"])
        self.assertEqual(report.drift[0].fix, "manual")

    def test_a_terminated_machine_counts_as_missing(self):
        self.active_tenant()
        report = state.reconcile(
            self.store, StubProvider([machine(machine_state="terminated")]), now=NOW
        )
        self.assertEqual([drift.kind for drift in report.drift], ["missing_vm"])

    def test_a_terminated_orphan_is_not_unknown(self):
        """A machine the provider already terminated is not drift; nothing to do."""
        report = state.reconcile(
            self.store, StubProvider([machine(machine_state="terminated")]), now=NOW
        )
        self.assertEqual(report.drift, ())

    def test_a_machine_named_for_a_claimed_tenant_is_unknown(self):
        self.store.invite(SLUG)
        self.store.claim(SLUG)
        report = state.reconcile(self.store, StubProvider([machine()]), now=NOW)
        self.assertEqual([drift.kind for drift in report.drift], ["unknown_vm"])
        self.assertEqual(report.drift[0].detail["tenant_state"], "claimed")

    def test_a_slug_scopes_the_report_to_one_tenant(self):
        self.active_tenant("odl-aaaaaa", server_id="srv-aaaaaa")
        self.active_tenant("odl-bbbbbb", server_id="srv-bbbbbb")
        report = state.reconcile(self.store, StubProvider([]), now=NOW, slug="odl-aaaaaa")
        self.assertEqual([drift.slug for drift in report.drift], ["odl-aaaaaa"])

    def test_reconcile_never_calls_a_mutating_provider_method(self):
        self.active_tenant()
        provider = StubProvider([])
        state.reconcile(self.store, provider, apply=True, now=NOW)
        self.assertEqual(provider.calls, ["list"])

    def test_reconcile_reads_the_provider_servers_action(self):
        """The comparison is the recorded `?action=servers` list, nothing else."""
        self.active_tenant()
        expiry = NOW + 10 * DAY
        transport = RecordedTransport(
            {
                ("GET", "servers"): [
                    (
                        200,
                        servers_response(
                            machine_row(
                                id=SERVER_ID,
                                hostname=SLUG,
                                expiration_date=expiry,
                            )
                        ),
                    )
                ]
            }
        )
        provider = servury.ServuryProvider(TEST_KEY, transport=transport)
        report = state.reconcile(self.store, provider, now=NOW)
        self.assertEqual(report.drift, ())
        self.assertEqual(transport.calls[0]["method"], "GET")
        self.assertEqual(transport.calls[0]["action"], "servers")

    def test_a_gone_machine_is_seen_through_the_recorded_transport(self):
        self.active_tenant()
        transport = RecordedTransport({("GET", "servers"): [(200, empty_servers_response())]})
        provider = servury.ServuryProvider(TEST_KEY, transport=transport)
        report = state.reconcile(self.store, provider, now=NOW)
        self.assertEqual([drift.kind for drift in report.drift], ["missing_vm"])


# -------------------------------------------------------------- the CLI transcript


class ReconcileCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="odile-reconcile-")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.db_path = Path(self.directory) / "waitlist.db"
        self.store = state.StateStore.open(self.db_path, clock=Clock())
        self.addCleanup(self.store.close)
        self.config_path = Path(self.directory) / "config.yaml"
        self.config_path.write_text(
            "\n".join(
                [
                    "provider: servury",
                    f"reseller_key: {TEST_KEY}",
                    "default_plan_id: 113",
                    "default_days: 31",
                    f"state_db: {self.db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.config_path.chmod(0o600)

    def run_cli(self, argv, provider):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            [*argv, "--config", str(self.config_path)],
            provider_factory=lambda _config: provider,
            stdout=out,
            stderr=err,
        )
        return code, out.getvalue(), err.getvalue()

    def active(self, slug: str = SLUG, server_id: str = SERVER_ID) -> None:
        self.store.invite(slug)
        self.store.claim(slug)
        self.store.provision(
            slug,
            server_id=server_id,
            ipv4="203.0.113.120",
            plan_id=113,
            expires_at=NOW + 10 * DAY,
            initramfs_host_key="SHA256:recorded-initramfs-key",
        )
        self.store.await_boot(slug)
        self.store.start_first_visit(slug)
        self.store.ready(slug)

    def test_the_cli_reports_drift_as_json_without_apply(self):
        self.active()
        provider = StubProvider([])
        code, out, err = self.run_cli(["reconcile", "--json"], provider)
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "reconcile")
        self.assertTrue(payload["dry_run"])
        self.assertEqual([drift["kind"] for drift in payload["drift"]], ["missing_vm"])
        self.assertEqual(payload["drift"][0]["fix"], "destroy")
        self.assertEqual(payload["fixed"], [])
        self.assertEqual(self.store.get(SLUG).state, "ready")

    def test_the_cli_apply_fixes_the_row_and_a_second_run_is_clean(self):
        self.active()
        provider = StubProvider([])
        code, out, err = self.run_cli(["reconcile", "--apply", "--json"], provider)
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertFalse(payload["dry_run"])
        self.assertEqual(payload["fixed"], [SLUG])
        self.assertEqual(self.store.get(SLUG).state, "destroyed")
        code, out, err = self.run_cli(["reconcile", "--apply", "--json"], provider)
        self.assertEqual(code, 0, err)
        second = json.loads(out)
        self.assertEqual(second["drift"], [])
        self.assertEqual(second["fixed"], [])

    def test_the_cli_apply_and_dry_run_together_stay_dry(self):
        self.active()
        code, out, err = self.run_cli(
            ["reconcile", "--apply", "--dry-run", "--json"], StubProvider([])
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(self.store.get(SLUG).state, "ready")

    def test_the_cli_slug_scopes_the_report(self):
        self.active("odl-aaaaaa", "srv-aaaaaa")
        self.active("odl-bbbbbb", "srv-bbbbbb")
        code, out, err = self.run_cli(
            ["reconcile", "odl-aaaaaa", "--json"], StubProvider([])
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual([drift["slug"] for drift in payload["drift"]], ["odl-aaaaaa"])

    def test_the_human_lines_name_the_three_drift_cases(self):
        self.active("odl-missing", "srv-missing")
        self.active("odl-expired", "srv-expired")
        self.store.invite("odl-orphan")
        self.store.claim("odl-orphan")
        provider = StubProvider(
            [
                machine(server_id="srv-expired", hostname="odl-expired", expires_at=NOW - DAY),
                machine(server_id="srv-orphan", hostname="odl-orphan"),
            ]
        )
        code, out, err = self.run_cli(["reconcile"], provider)
        self.assertEqual(code, 0, err)
        self.assertIn("missing_vm", out)
        self.assertIn("expired_vm", out)
        self.assertIn("unknown_vm", out)
        self.assertIn("pass --apply", out)


if __name__ == "__main__":
    unittest.main()
