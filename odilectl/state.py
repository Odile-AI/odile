"""The tenant state machine and the reconcile that keeps it honest (ODL-212, re-cut ODL-460).

A tenant's life is the strict flow plus its operational states, and every move
between them is explicit:

    invited -> claimed -> provisioning -> first_boot -> first_visit -> ready
            -> waiting_for_owner -> suspended -> destroyed

`waiting_for_ceremony` and `installing` belonged to the ceremony model. The
zero-touch image replaces them with `first_boot` (the machine installs itself)
and `first_visit` (the user sets the Odile sign-in), and the steady state is
`ready`. Opening a v2 database rebuilds the two tables with the new vocabulary,
mapping the old words in place so one row per claim survives.

`TRANSITIONS` is the whole machine. `StateStore.transition()` refuses an edge
that is not in it, updates the row and writes one append-only event in the same
transaction, so a state that changed without a record is not representable.
`waiting_for_owner` is a first-class state: a stopped heartbeat plus the
initramfs host key recorded at provision moves a ready tenant there, and a
returning heartbeat moves it back.

Storage is additive, in the same SQLite file as the waitlist (`state_db` in the
odilectl config, `/var/lib/odile/waitlist.db` by default), exactly as the seed
ledger adds `seed_tokens`: `tenant_state` is the machine spine and
`tenant_state_events` is its append-only log. The web app's `tenants` table
keeps the identity and claim fields; both are joined by slug. This module never
edits the web schema.

`reconcile()` compares those rows to the provider's `?action=servers` list and
returns findings — a missing machine, an expired machine, an unknown machine,
and a machine still running behind a destroyed tenant. It is read-only unless
`apply=True`; when it applies, it changes only the database, never the provider:
a missing machine is marked destroyed, an expired ready machine suspended, and
an unknown machine is left for a human to adopt or destroy.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .errors import OdileError
from .providers.base import Machine, Provider

DEFAULT_STATE_DB = "/var/lib/odile/waitlist.db"

# The state schema revision, independent of app.SCHEMA_VERSION: this module
# adds its tables to the same SQLite file as the waitlist but never edits the
# web schema. v2 added `tenant_state.release` (ODL-398); v3 is the strict-flow
# vocabulary (ODL-460), and is rebuilt on open because a CHECK cannot be
# altered in place.
STATE_SCHEMA_VERSION = 3

# A release id comes from the pin (`provision_release`) or the guest's boot
# report, never from the provider. 2026.12 and 2026.12.0 are both valid ids.
RELEASE_ID_RE = re.compile(r"^[0-9]{4}\.[0-9]{2}(?:\.[0-9]+)?$")

# Ten minutes without a heartbeat is a stopped machine, not a slow one: the
# guest's job fires every five minutes, so two misses is the smallest signal
# that is not a scheduling hiccup. ODL-216 uses the same window for its alert.
HEARTBEAT_STOP_SECONDS = 600

STATES = (
    "invited",
    "claimed",
    "provisioning",
    "first_boot",
    "first_visit",
    "ready",
    "waiting_for_owner",
    "suspended",
    "destroyed",
)

# A machine is supposed to exist at the provider in these states. `invited` and
# `claimed` are before the order; `destroyed` is after it. `first_boot` and
# `first_visit` are the zero-touch image's steps between the order and the
# workspace, and `ready` is the steady state the ceremony used to call `active`.
MACHINE_STATES = frozenset(
    {
        "provisioning",
        "first_boot",
        "first_visit",
        "ready",
        "waiting_for_owner",
        "suspended",
    }
)

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "invited": ("claimed", "destroyed"),
    "claimed": ("provisioning", "destroyed"),
    "provisioning": ("first_boot", "destroyed"),
    "first_boot": ("first_visit", "destroyed"),
    "first_visit": ("ready", "destroyed"),
    "ready": ("waiting_for_owner", "suspended", "destroyed"),
    "waiting_for_owner": ("ready", "suspended", "destroyed"),
    "suspended": ("ready", "destroyed"),
    "destroyed": (),
}

# The columns a transition may fill in. Nothing else is writable through this
# module: the state and its evidence move together or not at all.
_FIELDS = (
    "server_id",
    "hostname",
    "ipv4",
    "plan_id",
    "release",
    "expires_at",
    "initramfs_host_key",
    "last_heartbeat",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenant_state (
    slug               TEXT    PRIMARY KEY,
    state              TEXT    NOT NULL
                       CHECK (state IN ('invited', 'claimed', 'provisioning',
                                        'first_boot', 'first_visit', 'ready',
                                        'waiting_for_owner',
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

CREATE TABLE IF NOT EXISTS tenant_state_events (
    id         INTEGER PRIMARY KEY,
    slug       TEXT    NOT NULL REFERENCES tenant_state(slug),
    kind       TEXT    NOT NULL
               CHECK (kind IN ('invited', 'claimed', 'provisioning',
                               'first_boot', 'first_visit', 'ready',
                               'waiting_for_owner', 'suspended', 'destroyed')),
    from_state TEXT,
    to_state   TEXT    NOT NULL,
    detail     TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(detail)),
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS tenant_state_events_slug ON tenant_state_events(slug, id);
CREATE INDEX IF NOT EXISTS tenant_state_events_kind ON tenant_state_events(kind, id);

-- The transition log is evidence: rows may be added, never rewritten or
-- removed. The pair mirrors the web schema's triggers on `events`.
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

# The v2 -> v3 rebuild. The old table's CHECK only accepts the ceremony
# vocabulary, so SQLite's documented table-rebuild recipe is the only way to
# change it: new table, copy with the word mapping, drop, rename, recreate the
# indexes and triggers. It runs in one transaction with foreign keys off, so a
# crash leaves the v2 tables untouched. The retired words appear here, in the
# module docstring and in the test fixture and nowhere else: the mapping is the
# migration, not a live state.
_MIGRATE_STATES_V3 = """
BEGIN IMMEDIATE;

DROP TABLE IF EXISTS tenant_state_v3;
CREATE TABLE tenant_state_v3 (
    slug               TEXT    PRIMARY KEY,
    state              TEXT    NOT NULL
                       CHECK (state IN ('invited', 'claimed', 'provisioning',
                                        'first_boot', 'first_visit', 'ready',
                                        'waiting_for_owner',
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

INSERT INTO tenant_state_v3 (
    slug, state, server_id, hostname, ipv4, plan_id, release, expires_at,
    initramfs_host_key, last_heartbeat, detail, created_at, updated_at
)
SELECT slug,
       CASE state
           WHEN 'waiting_for_ceremony' THEN 'first_boot'
           WHEN 'installing' THEN 'first_visit'
           WHEN 'active' THEN 'ready'
           ELSE state
       END,
       server_id, hostname, ipv4, plan_id, release, expires_at,
       initramfs_host_key, last_heartbeat, detail, created_at, updated_at
FROM tenant_state;

DROP TABLE tenant_state;
ALTER TABLE tenant_state_v3 RENAME TO tenant_state;

CREATE INDEX IF NOT EXISTS tenant_state_state ON tenant_state(state);
CREATE INDEX IF NOT EXISTS tenant_state_heartbeat ON tenant_state(last_heartbeat);
CREATE INDEX IF NOT EXISTS tenant_state_server ON tenant_state(server_id);

DROP TABLE IF EXISTS tenant_state_events_v3;
CREATE TABLE tenant_state_events_v3 (
    id         INTEGER PRIMARY KEY,
    slug       TEXT    NOT NULL REFERENCES tenant_state(slug),
    kind       TEXT    NOT NULL
               CHECK (kind IN ('invited', 'claimed', 'provisioning',
                               'first_boot', 'first_visit', 'ready',
                               'waiting_for_owner', 'suspended', 'destroyed')),
    from_state TEXT,
    to_state   TEXT    NOT NULL,
    detail     TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(detail)),
    created_at TEXT    NOT NULL
);

INSERT INTO tenant_state_events_v3 (id, slug, kind, from_state, to_state, detail, created_at)
SELECT id, slug,
       CASE kind
           WHEN 'waiting_for_ceremony' THEN 'first_boot'
           WHEN 'installing' THEN 'first_visit'
           WHEN 'active' THEN 'ready'
           ELSE kind
       END,
       CASE from_state
           WHEN 'waiting_for_ceremony' THEN 'first_boot'
           WHEN 'installing' THEN 'first_visit'
           WHEN 'active' THEN 'ready'
           ELSE from_state
       END,
       CASE to_state
           WHEN 'waiting_for_ceremony' THEN 'first_boot'
           WHEN 'installing' THEN 'first_visit'
           WHEN 'active' THEN 'ready'
           ELSE to_state
       END,
       detail, created_at
FROM tenant_state_events;

DROP TABLE tenant_state_events;
ALTER TABLE tenant_state_events_v3 RENAME TO tenant_state_events;

CREATE INDEX IF NOT EXISTS tenant_state_events_slug ON tenant_state_events(slug, id);
CREATE INDEX IF NOT EXISTS tenant_state_events_kind ON tenant_state_events(kind, id);

CREATE TRIGGER tenant_state_events_no_update
BEFORE UPDATE ON tenant_state_events
BEGIN
    SELECT RAISE(ABORT, 'tenant state events are append-only');
END;

CREATE TRIGGER tenant_state_events_no_delete
BEFORE DELETE ON tenant_state_events
BEGIN
    SELECT RAISE(ABORT, 'tenant state events are append-only');
END;

COMMIT;
"""


class StateError(OdileError):
    """The state store or the machine refused a move; no traceback needed."""


class UnknownTenant(StateError):
    """There is no tenant row with that slug."""


class InvalidTransition(StateError):
    """The edge is not in `TRANSITIONS`, or the row moved underneath us."""


class HostKeyUnknown(StateError):
    """The tenant has no recorded initramfs key, so nobody can call it a reboot."""


class HostKeyMismatch(StateError):
    """The port-22 key is not the key recorded at provision: not the known initramfs."""


@dataclass(frozen=True)
class Tenant:
    """One tenant row: the machine facts and the state they belong to."""

    slug: str
    state: str
    server_id: str | None
    hostname: str | None
    ipv4: str | None
    plan_id: int | None
    release: str | None
    expires_at: int | None
    initramfs_host_key: str | None
    last_heartbeat: int | None
    detail: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class StateEvent:
    """One append-only transition record."""

    id: int
    slug: str
    kind: str
    from_state: str | None
    to_state: str
    detail: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class Drift:
    """One disagreement between the database and the provider."""

    kind: str  # missing_vm, expired_vm, unknown_vm, zombie_vm
    slug: str
    fix: str  # destroy, suspend, manual
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "slug": self.slug,
            "fix": self.fix,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class ReconcileReport:
    """The comparison's whole answer, with no hidden mutation."""

    tenants: int
    machines: int
    drift: tuple[Drift, ...]
    fixed: tuple[str, ...]
    applied: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenants": self.tenants,
            "machines": self.machines,
            "drift": [drift.to_dict() for drift in self.drift],
            "fixed": list(self.fixed),
            "applied": self.applied,
        }


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _detail(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _normal_key(value: str) -> str:
    return " ".join(value.split())


def _truthy(value: Any) -> bool:
    """Read the provider's flag without turning the string "false" into True."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def _tenant(row: sqlite3.Row) -> Tenant:
    return Tenant(
        slug=row["slug"],
        state=row["state"],
        server_id=row["server_id"],
        hostname=row["hostname"],
        ipv4=row["ipv4"],
        plan_id=row["plan_id"],
        release=row["release"],
        expires_at=row["expires_at"],
        initramfs_host_key=row["initramfs_host_key"],
        last_heartbeat=row["last_heartbeat"],
        detail=_detail(row["detail"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _event(row: sqlite3.Row) -> StateEvent:
    return StateEvent(
        id=row["id"],
        slug=row["slug"],
        kind=row["kind"],
        from_state=row["from_state"],
        to_state=row["to_state"],
        detail=_detail(row["detail"]),
        created_at=row["created_at"],
    )


def _migrate_release(connection: sqlite3.Connection) -> None:
    """Add `tenant_state.release` to an older row without losing anything.

    `CREATE TABLE IF NOT EXISTS` does not add a column to an existing table,
    so the upgrade is an explicit additive `ALTER TABLE`. `PRAGMA table_info`
    is the guard: run it on a new table and it does nothing, run it on the
    live table twice and only the first changes the schema. The migration
    lives here as well as in `StateStore.open` because claim and provision
    paths construct a store around a connection they already opened.
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(tenant_state)")}
    if columns and "release" not in columns:
        connection.execute("ALTER TABLE tenant_state ADD COLUMN release TEXT")
        connection.commit()


def _migrate_states(connection: sqlite3.Connection) -> None:
    """Rebuild a v2 state database with the strict-flow vocabulary (ODL-460).

    `CREATE TABLE IF NOT EXISTS` cannot add a column to an existing table and
    cannot change a CHECK, so the upgrade is the explicit rebuild in
    `_MIGRATE_STATES_V3`. `sqlite_master` is the guard: a fresh table already
    carries `first_boot`, a migrated one does too, and a v2 table does not. The
    migration is idempotent because the second open sees the new words and
    returns.
    """
    tables = {
        row[0]: row[1] or ""
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
            " AND name IN ('tenant_state', 'tenant_state_events')"
        )
    }
    if "tenant_state" not in tables:
        return  # a bare connection; `StateStore.open` creates the schema
    if (
        "tenant_state_events" in tables
        and "first_boot" in tables["tenant_state"]
        and "first_boot" in tables["tenant_state_events"]
    ):
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.executescript(_MIGRATE_STATES_V3)
    except sqlite3.Error as error:
        connection.rollback()
        raise StateError(f"cannot migrate the tenant state tables: {error}") from None
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


class StateStore:
    """One SQLite connection to the tenant spine and its transition log."""

    def __init__(self, connection: sqlite3.Connection, *, clock: Callable[[], int | float] = time.time):
        self._con = connection
        self._clock = clock
        _migrate_release(connection)
        _migrate_states(connection)

    # ------------------------------------------------------------- lifecycle

    @classmethod
    def open(
        cls,
        path: str | Path = DEFAULT_STATE_DB,
        *,
        clock: Callable[[], int | float] = time.time,
    ) -> "StateStore":
        """Open (or create) the store; the additive schema is idempotent."""
        try:
            connection = sqlite3.connect(str(path), timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(SCHEMA)
            connection.commit()
        except sqlite3.Error as error:
            raise StateError(f"cannot open the state database {path}: {error}") from None
        return cls(connection, clock=clock)

    def close(self) -> None:
        self._con.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """The raw connection; the event-log test uses it to try a rewrite."""
        return self._con

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def now(self) -> int:
        return int(self._clock())

    # ------------------------------------------------------------------- read

    def get(self, slug: str) -> Tenant:
        row = self._con.execute(
            "SELECT * FROM tenant_state WHERE slug = ?", (slug,)
        ).fetchone()
        if row is None:
            raise UnknownTenant(f"no tenant named {slug!r}")
        return _tenant(row)

    def tenants(self) -> tuple[Tenant, ...]:
        rows = self._con.execute("SELECT * FROM tenant_state ORDER BY slug").fetchall()
        return tuple(_tenant(row) for row in rows)

    def events(self, slug: str | None = None) -> tuple[StateEvent, ...]:
        if slug is None:
            rows = self._con.execute(
                "SELECT * FROM tenant_state_events ORDER BY id"
            ).fetchall()
        else:
            rows = self._con.execute(
                "SELECT * FROM tenant_state_events WHERE slug = ? ORDER BY id", (slug,)
            ).fetchall()
        return tuple(_event(row) for row in rows)

    # ------------------------------------------------------------------ write

    def invite(self, slug: str, *, detail: dict[str, Any] | None = None) -> Tenant:
        """Create the tenant at `invited`; asking twice is the same row."""
        slug = slug.strip()
        if not slug:
            raise ValueError("a tenant slug cannot be empty")
        existing = self._con.execute(
            "SELECT * FROM tenant_state WHERE slug = ?", (slug,)
        ).fetchone()
        if existing is not None:
            tenant = _tenant(existing)
            if tenant.state != "invited":
                raise StateError(
                    f"{slug} already exists in state {tenant.state}; invite creates only an invited tenant"
                )
            return tenant
        stamp = _iso(self.now())
        payload = json.dumps(detail or {}, sort_keys=True)
        try:
            self._con.execute(
                "INSERT INTO tenant_state (slug, state, detail, created_at, updated_at)"
                " VALUES (?, 'invited', ?, ?, ?)",
                (slug, payload, stamp, stamp),
            )
            self._con.execute(
                "INSERT INTO tenant_state_events (slug, kind, from_state, to_state, detail, created_at)"
                " VALUES (?, 'invited', NULL, 'invited', ?, ?)",
                (slug, payload, stamp),
            )
            self._con.commit()
        except Exception:
            self._con.rollback()
            raise
        return self.get(slug)

    def transition(
        self,
        slug: str,
        to_state: str,
        *,
        detail: dict[str, Any] | None = None,
        **fields: Any,
    ) -> Tenant:
        """Move one tenant along one declared edge and write one event.

        A repeat of the current state is a no-op (the world already agrees), so
        "run it again" is safe. Unknown columns are a programming error, not a
        silent write.
        """
        if to_state not in STATES:
            raise ValueError(f"unknown tenant state {to_state!r}")
        unknown = set(fields) - set(_FIELDS)
        if unknown:
            raise TypeError(f"unknown tenant field(s): {', '.join(sorted(unknown))}")
        tenant = self.get(slug)
        if tenant.state == to_state:
            return tenant
        allowed = TRANSITIONS[tenant.state]
        if to_state not in allowed:
            raise InvalidTransition(
                f"{slug}: {tenant.state} -> {to_state} is not a transition "
                f"(allowed: {', '.join(allowed) or 'none'})"
            )

        stamp = _iso(self.now())
        assignments = ["state = ?", "updated_at = ?"]
        values: list[Any] = [to_state, stamp]
        for name in _FIELDS:
            if fields.get(name) is not None:
                assignments.append(f"{name} = ?")
                values.append(fields[name])
        event_detail = dict(detail or {})
        if detail is not None:
            assignments.append("detail = ?")
            values.append(json.dumps(event_detail, sort_keys=True))
        values.append(slug)

        try:
            cursor = self._con.execute(
                f"UPDATE tenant_state SET {', '.join(assignments)}"
                " WHERE slug = ? AND state = ?",
                (*values, tenant.state),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition(
                    f"{slug}: the row moved while {tenant.state} -> {to_state} was applied"
                )
            self._con.execute(
                "INSERT INTO tenant_state_events (slug, kind, from_state, to_state, detail, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    slug,
                    to_state,
                    tenant.state,
                    to_state,
                    json.dumps(event_detail, sort_keys=True),
                    stamp,
                ),
            )
            self._con.commit()
        except Exception:
            self._con.rollback()
            raise
        return self.get(slug)

    # -------------------------------------------------------- named moves

    def claim(self, slug: str) -> Tenant:
        return self.transition(slug, "claimed")

    def provision(
        self,
        slug: str,
        *,
        server_id: str,
        hostname: str | None = None,
        ipv4: str | None = None,
        plan_id: int | None = None,
        release: str | None = None,
        expires_at: int | None = None,
        initramfs_host_key: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Tenant:
        """Record the provider's order: the one moment the host key is learned.

        `release` is the id the machine was ordered to boot, read from the
        pin's `provision_release` (ODL-398). It is never inferred from the
        provider.
        """
        return self.transition(
            slug,
            "provisioning",
            detail=detail,
            server_id=server_id,
            hostname=hostname,
            ipv4=ipv4,
            plan_id=plan_id,
            release=release,
            expires_at=expires_at,
            initramfs_host_key=initramfs_host_key,
        )

    def record_release(self, slug: str, release: str) -> Tenant:
        """Record the intended release when the row already exists.

        Used by `provision` on a second run: the state has already moved past
        `claimed`, so there is no transition to make, but a row created before
        ODL-398 may still have a NULL release. First write wins — a re-run must
        not rewrite the release the machine was actually ordered under.
        """
        chosen = (release or "").strip()
        if not RELEASE_ID_RE.match(chosen):
            raise ValueError(f"release {release!r} is not a release id")
        tenant = self.get(slug)
        if tenant.release is not None:
            return tenant
        stamp = _iso(self.now())
        try:
            self._con.execute(
                "UPDATE tenant_state SET release = ?, updated_at = ? WHERE slug = ?",
                (chosen, stamp, slug),
            )
            self._con.commit()
        except Exception:
            self._con.rollback()
            raise
        return self.get(slug)

    def await_boot(self, slug: str) -> Tenant:
        """The order exists; the machine now runs its zero-touch first boot."""
        return self.transition(slug, "first_boot")

    def start_first_visit(self, slug: str, *, detail: dict[str, Any] | None = None) -> Tenant:
        """The image finished; the user's first visit sets the sign-in."""
        return self.transition(slug, "first_visit", detail=detail)

    def note(self, slug: str, *, detail: dict[str, Any] | None = None,
             **fields: Any) -> Tenant:
        """Merge facts into the row without moving the state.

        A ready tenant's machine facts (the boot record the first boot
        reported) change without a transition; `detail` is merged, never
        replaced, because it is the current machine record, not an event. A
        payload that changes nothing writes nothing, so a repeated record is
        idempotent down to `updated_at`.
        """
        unknown = set(fields) - set(_FIELDS)
        if unknown:
            raise TypeError(f"unknown tenant field(s): {', '.join(sorted(unknown))}")
        tenant = self.get(slug)
        merged = dict(tenant.detail)
        merged.update(detail or {})
        assignments: list[str] = []
        values: list[Any] = []
        for name in _FIELDS:
            if fields.get(name) is not None and fields[name] != getattr(tenant, name):
                assignments.append(f"{name} = ?")
                values.append(fields[name])
        if merged != tenant.detail:
            assignments.append("detail = ?")
            values.append(json.dumps(merged, sort_keys=True))
        if not assignments:
            return tenant
        stamp = _iso(self.now())
        assignments.append("updated_at = ?")
        values.append(stamp)
        values.append(slug)
        try:
            self._con.execute(
                f"UPDATE tenant_state SET {', '.join(assignments)} WHERE slug = ?",
                values,
            )
            self._con.commit()
        except Exception:
            self._con.rollback()
            raise
        return self.get(slug)

    def forget_initramfs_host_key(self, slug: str) -> Tenant:
        """Drop the initramfs host key once the strict image has booted.

        Under the zero-touch image there is no initramfs SSH wait to identify,
        so the column's old meaning is retired (ODL-467); the machine's current
        host key is carried in `detail.host_key` instead.
        """
        tenant = self.get(slug)
        if tenant.initramfs_host_key is None:
            return tenant
        try:
            self._con.execute(
                "UPDATE tenant_state SET initramfs_host_key = NULL, updated_at = ?"
                " WHERE slug = ?",
                (_iso(self.now()), slug),
            )
            self._con.commit()
        except Exception:
            self._con.rollback()
            raise
        return self.get(slug)

    def ready(
        self,
        slug: str,
        *,
        ipv4: str | None = None,
        expires_at: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Tenant:
        """The workspace is live: the steady state the ceremony called `active`."""
        return self.transition(
            slug, "ready", detail=detail, ipv4=ipv4, expires_at=expires_at
        )

    def suspend(self, slug: str, *, reason: str = "") -> Tenant:
        return self.transition(slug, "suspended", detail={"reason": reason})

    def resume(self, slug: str) -> Tenant:
        return self.transition(slug, "ready")

    def destroy(self, slug: str, *, reason: str = "") -> Tenant:
        return self.transition(slug, "destroyed", detail={"reason": reason})

    # ------------------------------------------------------- heartbeat

    def heartbeat(self, slug: str, *, at: int | None = None) -> Tenant:
        """Record a live guest; a beat from a legacy park is the return signal."""
        tenant = self.get(slug)
        if tenant.state == "destroyed":
            return tenant
        stamp = int(at if at is not None else self.now())
        if tenant.state == "waiting_for_owner":
            return self.transition(
                slug,
                "ready",
                last_heartbeat=stamp,
                detail={"reason": "heartbeat returned"},
            )
        try:
            self._con.execute(
                "UPDATE tenant_state SET last_heartbeat = ?, updated_at = ? WHERE slug = ?",
                (stamp, _iso(self.now()), slug),
            )
            self._con.commit()
        except Exception:
            self._con.rollback()
            raise
        return self.get(slug)

    def park_for_owner(
        self,
        slug: str,
        *,
        observed_host_key: str,
        now: int | None = None,
        threshold: int = HEARTBEAT_STOP_SECONDS,
    ) -> bool:
        """Move a ready tenant to `waiting_for_owner` when the evidence fits.

        The evidence is three facts together: the tenant is ready, its last
        heartbeat is older than `threshold`, and port 22 answers with the
        initramfs host key recorded at provision. A different key is refused
        loudly — a changed key is not the machine coming back. ODL-467 retires
        the park itself once the sealed image boots without the initramfs wait.
        """
        tenant = self.get(slug)
        if tenant.state == "waiting_for_owner":
            return False
        if tenant.state != "ready":
            return False
        if tenant.last_heartbeat is None:
            return False
        moment = int(now if now is not None else self.now())
        age = moment - tenant.last_heartbeat
        if age < threshold:
            return False
        if not tenant.initramfs_host_key:
            raise HostKeyUnknown(
                f"{slug} has no recorded initramfs host key; cannot call a stopped "
                "heartbeat waiting_for_owner"
            )
        if _normal_key(observed_host_key) != _normal_key(tenant.initramfs_host_key):
            raise HostKeyMismatch(
                f"{slug}: observed host key {observed_host_key!r} is not the recorded "
                f"initramfs key {tenant.initramfs_host_key!r}"
            )
        self.transition(
            slug,
            "waiting_for_owner",
            detail={
                "reason": "heartbeat stopped",
                "heartbeat_age_seconds": age,
                "initramfs_host_key": observed_host_key,
            },
        )
        return True


# --------------------------------------------------------------------- reconcile


def _match(tenant: Tenant, machines: Iterable[Machine]) -> Machine | None:
    """The provider row this tenant points at, by server id first, name second.

    A tenant before `provisioning` has no server id; only then is the hostname
    an identity. A destroyed tenant is matched so a machine that outlived its
    row is visible as a zombie, not silently ignored.
    """
    if tenant.server_id:
        for machine in machines:
            if machine.server_id == tenant.server_id:
                return machine
        return None
    name = tenant.hostname or tenant.slug
    if tenant.state in MACHINE_STATES or tenant.state == "destroyed":
        for machine in machines:
            if machine.hostname == name:
                return machine
    return None


def _known_machine_ids(tenants: Iterable[Tenant], machines: list[Machine]) -> set[str]:
    known: set[str] = set()
    for tenant in tenants:
        match = _match(tenant, machines)
        if match is not None:
            known.add(match.server_id)
    return known


def _expired_days(expires_at: int, now: int) -> int:
    return max(1, (now - expires_at + 86_399) // 86_400)


def reconcile(
    store: StateStore,
    provider: Provider,
    *,
    apply: bool = False,
    now: int | None = None,
    slug: str | None = None,
) -> ReconcileReport:
    """Compare the tenant rows to the provider's machine list.

    Read-only by default. `apply=True` repairs only what the database can
    truthfully say on its own: a machine the provider no longer lists marks the
    tenant destroyed, and an expired ready machine is suspended. An unknown
    machine has no owner in the database, so it is reported for a human; this
    function never terminates, creates or powers a machine.
    """
    moment = int(now if now is not None else time.time())
    machines = list(provider.list())
    all_tenants = store.tenants()
    known = _known_machine_ids(all_tenants, machines)
    scoped = [tenant for tenant in all_tenants if slug is None or tenant.slug == slug]

    drift: list[Drift] = []
    for tenant in scoped:
        match = _match(tenant, machines)
        if tenant.state == "destroyed":
            if match is not None and match.state != "terminated":
                drift.append(
                    Drift(
                        "zombie_vm",
                        tenant.slug,
                        "manual",
                        {
                            "server_id": match.server_id,
                            "provider_state": match.state,
                            "raw_status": match.raw_status,
                        },
                    )
                )
            continue
        if tenant.state not in MACHINE_STATES:
            continue
        if match is None or match.state == "terminated":
            drift.append(
                Drift(
                    "missing_vm",
                    tenant.slug,
                    "destroy",
                    {
                        "server_id": tenant.server_id,
                        "hostname": tenant.hostname or tenant.slug,
                        "tenant_state": tenant.state,
                        "provider_state": match.state if match is not None else None,
                    },
                )
            )
            continue
        provider_expired = _truthy(match.detail.get("expired"))
        expired = provider_expired or (
            match.expires_at is not None and match.expires_at <= moment
        )
        if expired and tenant.state != "suspended":
            fix = "suspend" if tenant.state in ("ready", "waiting_for_owner") else "manual"
            drift.append(
                Drift(
                    "expired_vm",
                    tenant.slug,
                    fix,
                    {
                        "server_id": match.server_id,
                        "expires_at": match.expires_at,
                        "expired_days": (
                            _expired_days(match.expires_at, moment)
                            if match.expires_at is not None
                            else 0
                        ),
                        "provider_expired": provider_expired,
                        "tenant_state": tenant.state,
                        "provider_state": match.state,
                    },
                )
            )

    for machine in machines:
        if machine.server_id in known:
            continue
        if machine.state == "terminated":
            continue
        if slug is not None and machine.hostname != slug:
            continue
        detail: dict[str, Any] = {
            "server_id": machine.server_id,
            "hostname": machine.hostname,
            "provider_state": machine.state,
            "expires_at": machine.expires_at,
            "raw_status": machine.raw_status,
        }
        try:
            owner = store.get(machine.hostname)
        except UnknownTenant:
            owner = None
        if owner is not None:
            detail["tenant_state"] = owner.state
        drift.append(
            Drift("unknown_vm", machine.hostname or machine.server_id, "manual", detail)
        )

    fixed: list[str] = []
    if apply:
        for finding in drift:
            if finding.fix == "destroy":
                store.destroy(
                    finding.slug, reason="reconcile: the provider lists no machine"
                )
                fixed.append(finding.slug)
            elif finding.fix == "suspend":
                store.suspend(
                    finding.slug, reason="reconcile: the machine is expired at the provider"
                )
                fixed.append(finding.slug)

    return ReconcileReport(
        tenants=len(scoped),
        machines=len(machines),
        drift=tuple(drift),
        fixed=tuple(fixed),
        applied=bool(apply),
    )
