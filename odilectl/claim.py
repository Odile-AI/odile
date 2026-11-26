"""`claim` — a claim becomes a machine and a queued ready email.

The claim-first spine, end to end:

    invite → claim token → the invited email → tenant row
           → provision (fixture here, live in ODL-332)
           → DNS A record → single-use setup token → recovery kit
           → one queued ready email

What this module promises
-------------------------
* **The tenant row carries one boot key, and it has no private half.** The
  strict claim takes no user key; the key in the measured command line is a
  throwaway public key generated at accept time. There is no password or
  passphrase column anywhere in the schema.
* **A claim is accepted once and replays idempotently.** The first accept
  stamps ``claims.used_at`` and moves the tenant to ``claimed``; a second POST
  with the same token and the same key is the same success, not an error. A
  different key against a used token is refused.
* **A paid tenant is provisioned exactly once.** "Paid" is the manual flag
  until Stripe: ``tenants.plan`` is empty until the operator records the plan;
  a non-empty plan means the money cleared. Provisioning looks at the
  provider's own machine list first, so a retry adopts the machine it already
  ordered instead of ordering a second one.
* **The boot release is the pin's, never the provider's.** The worker reads
  `provision_release` from `odile-verify.pin` before it orders anything (the
  same helper `odilectl provision` uses, so the two paths cannot drift), and
  records it on the tenant row and on the state row. The provider's
  `measured_release` is an observation, never the source.
* **A failed provision leaves no billed resource.** A machine this run created
  is terminated, a DNS record this run wrote is removed, the tenant goes back
  to ``claimed`` and the failure is written to the append-only event log; the
  claim stays usable and ``claim run`` retries it. Savio is alerted in the
  same outbox the health poller writes.
* **A claim mints the tenant's BX11 pair before it orders.** The sub-account
  recorder `odilectl provision` runs (ODL-924) is called from this path too,
  before the paid call: the store of record, the seed handoff file and the
  row's `detail` are written, so a claim-ordered machine's seed carries the
  `backup` block, and the 51st pair is refused by name with no machine
  ordered. A pair minted for an order that then fails is reused by the retry
  (idempotent by name), never minted twice.
* **One ready email per tenant.** The queue item is an RFC 5322 message at
  ``<outbox>/ready/<slug>.eml`` with the recovery kit attached, plus a JSON
  sidecar with the non-secret facts. A second run that finds the message
  writes nothing.

Where the web service stops and the worker begins
-------------------------------------------------
``app.py`` runs as the unprivileged ``odile`` user and owns no provider or DNS
credential. It records the claim (token, key, tenant, event) and returns; the
paid work is done by ``python3 -m odilectl.claim run --apply``, which runs as
root because ``/root/.odile/`` holds the provider reseller key and the
Cloudflare token. The scan of ``tenants`` is the queue: a tenant at ``claimed``
with a plan and no server id is work. Until a systemd timer is installed (an
ops follow-up), the operator runs the command by hand; with the timer there is
no human in the loop.

The fixture provider is a test seam, like ODL-211's recorded transport. The
live provision is ODL-332's evidence.

    python3 -m odilectl.claim invite ada@example.com --plan "Blackbox Nano" --apply
    python3 -m odilectl.claim accept <claim-token> --key ~/.ssh/odile.pub --apply
    python3 -m odilectl.claim mark-paid odl-7f3a2c --plan "Blackbox Nano" --apply
    python3 -m odilectl.claim run --apply
    python3 -m odilectl.claim status odl-7f3a2c --json

Exit codes are the CLI's: 0 ok, 1 a provision failed, 2 usage, 4 provider,
5 config. A claim that was accepted but whose provision failed still prints the
claim result; the exit code is 1 so a timer surfaces it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO

from . import dns as dns_module
from .config import DEFAULT_CONFIG_PATH, ENV_CONFIG_PATH, Config
from .core import Context, Report, plan
from .dns import DEFAULT_ZONE, PROVISION_TTL, CloudflareDNS, record_name
from .errors import ConfigError, OdileError, ProviderError, UsageError
from .health import (
    MatrixSender,
    Outbox,
    SmtpSender,
    guest_answer,
    machine_measurement,
)
from .providers.base import CreateSpec, Machine, Provider, validate_ssh_public_key
from .provision import DEFAULT_PIN, BackupPair, pinned_release, _provision_backup
from .state import SCHEMA as STATE_SCHEMA
from .state import StateError, StateStore, UnknownTenant

# `open_db` imports app.py, and `odilectl` may be invoked from any directory:
# the tree root is the one path that makes `import app` and the recovery-kit
# template resolve in a checkout and in the deployed /opt/odile tree.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@lru_cache(maxsize=1)
def _seed_service():
    """Load ``seed/seed.py`` by path, not by package name.

    The seed service runs as ``python3 seed/seed.py``, so ``sys.path[0]`` is
    the ``seed/`` directory and ``import seed.seed`` would resolve ``seed`` to
    the script module itself. A path load works in the service, in the web
    app, and in a test run alike.
    """
    path = Path(__file__).resolve().parent.parent / "seed" / "seed.py"
    spec = importlib.util.spec_from_file_location("odile_seed_service", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the seed service at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SEED = _seed_service()
SEED_SCHEMA = _SEED.SEED_SCHEMA

NAME = "claim"
HELP = "Turn a claim into a machine, a recovery kit and one queued ready email."
EXAMPLE = "python3 -m odilectl.claim run --apply"

DEFAULT_STATE_DB = "/var/lib/odile/waitlist.db"
DEFAULT_OUTBOX = "/var/lib/odile/outbox"
DEFAULT_PLAN_NAME = "Blackbox Nano"
DEFAULT_TTL_HOURS = 168
SETUP_TOKEN_TTL_HOURS = 24
DEFAULT_TIMEOUT = 900.0
DEFAULT_INTERVAL = 5.0

# The claim token is 24 random bytes, hex. Only its SHA-256 hash is stored;
# the plaintext is printed once and travels in the invite link.
CLAIM_TOKEN_RE = re.compile(r"^[0-9a-f]{48}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# The two tenant states that mean "this row may still be provisioned". The
# web schema v2 has no `failed` state (test_flow pins the list), so a failed
# attempt is recorded in the event log and the tenant stays `claimed`; that is
# exactly what makes the claim retryable.
WORKING_STATES = ("claimed", "provisioned")
READY_STATES = ("provisioned", "installed", "active", "waiting_for_owner")
SETTLED_STATES = ("installed", "active", "waiting_for_owner", "churned")

# The ready email is the one public surface this module writes. The subject is
# BRAND.md's; the body is `templates/emails/ready.txt`: the machine URL and the
# measurement the machine reports, printed values, then the one action — the
# machine's tokenless first-sign-in page (B-once, ODL-925: sign in with the
# password chosen at registration) — and one navigation link to the guide. The
# strict bar removed the four terminal commands; the setup token is the
# machine's seed badge and the rebuild-seal key, and no message or kit prints it.
READY_SUBJECT = "Your machine is waiting."
# The live mail runbook's transactional sender (ODL-102): mail.odile.fyi is
# what DKIM signs and SPF authorises. BRAND.md's hello@odile.fyi predates that
# server; ODL-459 owns the body and the first-visit guide it links.
READY_SENDER = "Odile <invite@mail.odile.fyi>"
READY_REPLY_TO = "help@odile.fyi"
READY_GUIDE_URL = "https://odile.fyi/ceremony"
READY_TEMPLATE = ROOT / "templates" / "emails" / "ready.txt"
# The release pin ODL-454 updates: the measurement the image was built for, and
# the value a rebuild is compared with. ODL-947 retired it from the ready email:
# a machine's own boot report is per machine (its command line carries the
# instance id, address and sealed line), so the pin and the boot legitimately
# differ — the mail now prints the report's 96 hex, the one its `/verify` page
# shows. The pin stays the build-time expectation (`expected_measurement`).
MEASUREMENT_PIN = ROOT / "odile-verify.pin"
MEASUREMENT_RE = re.compile(r"^[0-9a-f]{96}$")


# --------------------------------------------------------------------- errors


class ClaimError(OdileError):
    """The claim or its tenant cannot do what was asked; no traceback."""


class ClaimRefused(ClaimError):
    """A refusal with the HTTP status the web service should answer with."""

    def __init__(self, status: int, reason: str, message: str):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message


# --------------------------------------------------------------------- helpers


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_epoch(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return iso(datetime.fromtimestamp(int(epoch), timezone.utc))


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the one database, with the app's row factory and foreign keys."""
    parent = os.path.dirname(str(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create the additive tables the seed service and state machine own.

    ``app.migrate()`` creates the v2 spine; these two ``IF NOT EXISTS``
    scripts add the setup-token ledger and the tenant state spine to the same
    file. Both are already named in ``web/privacy.html``, and running this
    twice changes nothing.
    """
    con.executescript(SEED_SCHEMA)
    con.executescript(STATE_SCHEMA)
    con.commit()


def open_db(path: str | Path = DEFAULT_STATE_DB) -> sqlite3.Connection:
    """Open (or create) every schema this module writes, idempotently."""
    con = connect(path)
    try:
        import app  # local import: `python3 -m odilectl.claim` loads app lazily

        app.migrate(con)
        ensure_schema(con)
    except Exception:
        con.close()
        raise
    return con


def new_claim_token() -> str:
    return secrets.token_hex(24)


def valid_claim_token(token: str) -> bool:
    return bool(CLAIM_TOKEN_RE.match(token or ""))


def hash_token(token: str) -> str:
    """The one token hash: the seed service's, so the ledgers cannot drift."""
    return _SEED.hash_token(token)


def ssh_fingerprint(public_key: str) -> str:
    """The ``SHA256:`` fingerprint ``ssh-keygen -lf`` prints, from the key.

    The key's wire blob is the first base64 field; its SHA-256, base64 with
    the padding removed, is the OpenSSH fingerprint. Parsing is the provider's
    strict one-liner, so a private key or a truncated blob never reaches here.
    """
    normalised = validate_ssh_public_key(public_key)
    fields = normalised.split()
    if len(fields) < 2:
        raise ValueError("ssh_key must look like 'ssh-ed25519 AAAA... comment'")
    blob = base64.b64decode(fields[1], validate=True)
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return "SHA256:" + digest


def _json(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _tenant(con: sqlite3.Connection, slug: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM tenants WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise ClaimRefused(404, "no_tenant", f"no tenant named {slug!r}")
    return row


def _tenant_by_email(con: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM tenants WHERE email = ?", (email,)).fetchone()


def is_paid(tenant: sqlite3.Row) -> bool:
    """The manual paid flag until Stripe: a recorded plan means paid."""
    return bool((tenant["plan"] or "").strip())


def _event(con: sqlite3.Connection, tenant_id: int, kind: str, detail: dict) -> None:
    con.execute(
        "INSERT INTO events (tenant_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
        (tenant_id, kind, json.dumps(detail, sort_keys=True), detail.get("at") or iso(utcnow())),
    )


def _events(con: sqlite3.Connection, tenant_id: int, limit: int = 20) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM events WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
        (tenant_id, limit),
    ).fetchall()


def _has_ready_event(con: sqlite3.Connection, tenant_id: int) -> bool:
    """True when the ready email was queued for this tenant.

    The append-only event, not the queue file, is the durable marker: a mailer
    that sends and removes the .eml must not make the next ``claim run``
    provision again and rotate the token the user already received.
    """
    return con.execute(
        "SELECT 1 FROM events WHERE tenant_id = ? AND kind = 'provisioned'"
        " AND json_extract(detail, '$.step') = 'ready' LIMIT 1",
        (tenant_id,),
    ).fetchone() is not None


def _has_state_table(con: sqlite3.Connection) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tenant_state'"
    ).fetchone() is not None


def _state_row(con: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    if not _has_state_table(con):
        return None
    return con.execute("SELECT * FROM tenant_state WHERE slug = ?", (slug,)).fetchone()


# --------------------------------------------------------------------- results


@dataclass(frozen=True)
class IssuedClaim:
    """A tenant row and the one plaintext claim token that opens it."""

    slug: str
    email: str
    token: str
    expires_at: str
    tenant_id: int
    claim_id: int
    plan: str | None
    reissued: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "email": self.email,
            "claim_token": self.token,
            "claim_expires": self.expires_at,
            "tenant_id": self.tenant_id,
            "claim_id": self.claim_id,
            "plan": self.plan,
            "paid": bool(self.plan),
            "reissued": self.reissued,
        }


@dataclass(frozen=True)
class ClaimResult:
    """What the claim side knows after an accept (before provisioning)."""

    slug: str
    email: str
    tenant_id: int
    state: str
    plan: str | None
    key_fingerprint: str
    replay: bool
    claim_expires: str
    state_note: str = ""

    @property
    def paid(self) -> bool:
        return bool((self.plan or "").strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "email": self.email,
            "tenant_id": self.tenant_id,
            "state": self.state,
            "plan": self.plan,
            "paid": self.paid,
            "key_fingerprint": self.key_fingerprint,
            "replay": self.replay,
            "claim_expires": self.claim_expires,
            "state_note": self.state_note,
        }


@dataclass(frozen=True)
class ProvisionResult:
    """One provision attempt: the machine, the ready email, or the failure."""

    slug: str
    ok: bool
    created: bool = False
    adopted: bool = False
    skipped: bool = False
    # ODL-947: the machine exists but its guest half has not answered `/verify`
    # yet; the row stays `provisioned` and the one ready email waits.
    pending: bool = False
    server_id: str | None = None
    ipv4: str | None = None
    expires_at: str | None = None
    machine: dict[str, Any] | None = None
    dns_action: str | None = None
    dns_record_id: str | None = None
    dns_name: str | None = None
    token_expires: str | None = None
    kit_path: str | None = None
    email_path: str | None = None
    alert_path: str | None = None
    terminated: bool = False
    reason: str = ""
    state_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "ok": self.ok,
            "created": self.created,
            "adopted": self.adopted,
            "skipped": self.skipped,
            "pending": self.pending,
            "server_id": self.server_id,
            "ipv4": self.ipv4,
            "expires_at": self.expires_at,
            "machine": self.machine,
            "dns": {
                "action": self.dns_action,
                "record_id": self.dns_record_id,
                "name": self.dns_name,
            },
            "token_expires": self.token_expires,
            "kit": self.kit_path,
            "ready_email": self.email_path,
            "alert": self.alert_path,
            "terminated": self.terminated,
            "reason": self.reason,
            "state_note": self.state_note,
        }


# --------------------------------------------------------------------- claim side


def issue_claim(
    con: sqlite3.Connection,
    email: str,
    *,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    plan: str | None = None,
    slug: str | None = None,
    now: datetime | None = None,
) -> IssuedClaim:
    """Create the tenant row and one one-time claim token.

    The invite is the first step of the same spine, so its write lives here
    rather than in a second implementation. ``odilectl invite`` (outside this
    card's ``touches``) still raises NotWired; ODL-231/ODL-332 can call this.

    Asking twice for the same address reuses the tenant and revokes any live
    claim token before issuing the new one: the newest invite is the one that
    opens the page.
    """
    moment = now or utcnow()
    address = (email or "").strip().lower()
    if len(address) > 254 or not _EMAIL_RE.match(address):
        raise ClaimRefused(422, "bad_email", f"{email!r} is not an email address")
    if ttl_hours <= 0:
        raise ClaimRefused(422, "bad_ttl", "--ttl-hours must be positive")
    plan = (plan or "").strip() or None
    stamp = iso(moment)
    expires_at = iso(moment.replace(microsecond=0) + timedelta(hours=ttl_hours))

    existing = _tenant_by_email(con, address)
    reissued = existing is not None
    if existing is not None:
        if existing["state"] not in ("invited", "claimed"):
            raise ClaimRefused(
                409, "already_settled",
                f"{existing['slug']} is {existing['state']}; it cannot be re-invited",
            )
        tenant_id = int(existing["id"])
        chosen_slug = existing["slug"]
        con.execute(
            "UPDATE claims SET used_at = ? WHERE tenant_id = ? AND used_at IS NULL",
            (stamp, tenant_id),
        )
        if plan:
            con.execute("UPDATE tenants SET plan = ? WHERE id = ?", (plan, tenant_id))
        _event(con, tenant_id, "invited",
               {"source": "claim", "reissued": True, "plan": plan, "at": stamp})
    else:
        chosen_slug, tenant_id = _insert_tenant(con, chosen_slug=slug, email=address,
                                                plan=plan, stamp=stamp)
        _event(con, tenant_id, "invited",
               {"source": "claim", "reissued": False, "plan": plan, "at": stamp})

    token = new_claim_token()
    cur = con.execute(
        "INSERT INTO claims (tenant_id, token_hash, created_at, expires_at)"
        " VALUES (?, ?, ?, ?)",
        (tenant_id, hash_token(token), stamp, expires_at),
    )
    con.commit()
    _ensure_invited_state(con, chosen_slug)
    claim_id = cur.lastrowid
    if claim_id is None:
        raise ClaimError("claim: the token insert returned no row id")
    return IssuedClaim(
        slug=chosen_slug,
        email=address,
        token=token,
        expires_at=expires_at,
        tenant_id=tenant_id,
        claim_id=int(claim_id),
        plan=plan,
        reissued=reissued,
    )


def _insert_tenant(
    con: sqlite3.Connection,
    *,
    chosen_slug: str | None,
    email: str,
    plan: str | None,
    stamp: str,
) -> tuple[str, int]:
    """Insert one invited tenant; a slug collision is retried, not guessed."""
    attempts = 5 if chosen_slug is None else 1
    last: Exception | None = None
    for _ in range(attempts):
        candidate = chosen_slug or dns_module.new_slug()
        try:
            cur = con.execute(
                "INSERT INTO tenants (slug, email, state, plan, created_at)"
                " VALUES (?, ?, 'invited', ?, ?)",
                (candidate, email, plan, stamp),
            )
            tenant_id = cur.lastrowid
            if tenant_id is None:
                raise ClaimError("claim: the tenant insert returned no row id")
            return candidate, int(tenant_id)
        except sqlite3.IntegrityError as error:
            last = error
            if chosen_slug is not None:
                break
    raise ClaimRefused(409, "duplicate", f"cannot create a tenant for {email}: {last}")


def _ensure_invited_state(con: sqlite3.Connection, slug: str) -> str:
    """Create the state-machine row at ``invited``; a note, never a failure."""
    if not _has_state_table(con):
        return ""
    store = StateStore(con)
    try:
        tenant = store.get(slug)
    except UnknownTenant:
        store.invite(slug)
        return ""
    if tenant.state == "destroyed":
        return f"{slug}: the state row is destroyed; the claim cannot be provisioned"
    return ""


def accept(
    con: sqlite3.Connection,
    token: str,
    public_key: str,
    *,
    now: datetime | None = None,
) -> ClaimResult:
    """Accept one claim: the key, the tenant row, the single-use ledger.

    The key is parsed before anything is written, so a private key or a
    truncated blob cannot burn the token. A used token replays its success for
    the same key and refuses a different one.
    """
    moment = now or utcnow()
    stamp = iso(moment)
    if not valid_claim_token(token or ""):
        raise ClaimRefused(404, "unknown_claim", "That claim link is not valid.")
    try:
        normalised = validate_ssh_public_key(public_key or "")
        fingerprint = ssh_fingerprint(normalised)
    except ValueError as error:
        raise ClaimRefused(422, "bad_key", str(error)) from None

    row = con.execute(
        "SELECT c.*, t.slug AS t_slug, t.email AS t_email, t.state AS t_state,"
        " t.plan AS t_plan, t.ssh_public_key AS t_key, t.claimed_at AS t_claimed_at"
        " FROM claims AS c JOIN tenants AS t ON t.id = c.tenant_id"
        " WHERE c.token_hash = ?",
        (hash_token(token),),
    ).fetchone()
    if row is None:
        raise ClaimRefused(404, "unknown_claim", "That claim link is not valid.")
    if row["used_at"] is not None:
        stored = row["t_key"]
        # A replay is the same token that claimed this tenant: its used_at is
        # the tenant's claimed_at. A token revoked by a re-invite has an
        # earlier used_at and must not reopen the page.
        if (stored and _key_same(stored, normalised)
                and row["used_at"] == row["t_claimed_at"]):
            return ClaimResult(
                slug=row["t_slug"], email=row["t_email"], tenant_id=int(row["tenant_id"]),
                state=row["t_state"], plan=row["t_plan"], key_fingerprint=fingerprint,
                replay=True, claim_expires=row["expires_at"],
            )
        raise ClaimRefused(
            409, "already_used", "That claim link has already been used with another key."
        )
    if row["expires_at"] <= stamp:
        raise ClaimRefused(410, "expired", "That claim link has expired.")
    if row["t_state"] in SETTLED_STATES:
        raise ClaimRefused(
            409, "already_settled", f"{row['t_slug']} is {row['t_state']}"
        )

    try:
        con.execute(
            "UPDATE tenants SET ssh_public_key = ?, claimed_at = ?,"
            " state = CASE WHEN state = 'invited' THEN 'claimed' ELSE state END"
            " WHERE id = ?",
            (normalised, stamp, row["tenant_id"]),
        )
        used = con.execute(
            "UPDATE claims SET used_at = ? WHERE id = ? AND used_at IS NULL",
            (stamp, row["id"]),
        )
        if used.rowcount != 1:
            con.rollback()
            raise ClaimRefused(409, "already_used", "That claim link has already been used.")
        _event(con, int(row["tenant_id"]), "claimed", {
            "source": "web",
            "key_fingerprint": fingerprint,
            "paid": bool((row["t_plan"] or "").strip()),
            "plan": row["t_plan"],
            "at": stamp,
        })
        con.commit()
    except ClaimRefused:
        raise
    except Exception:
        con.rollback()
        raise

    note = _advance_state_claimed(con, row["t_slug"])
    tenant = _tenant(con, row["t_slug"])
    return ClaimResult(
        slug=tenant["slug"],
        email=tenant["email"],
        tenant_id=int(tenant["id"]),
        state=tenant["state"],
        plan=tenant["plan"],
        key_fingerprint=fingerprint,
        replay=False,
        claim_expires=row["expires_at"],
        state_note=note,
    )


def _key_same(left: str, right: str) -> bool:
    try:
        return validate_ssh_public_key(left) == validate_ssh_public_key(right)
    except ValueError:
        return False


def _advance_state_claimed(con: sqlite3.Connection, slug: str) -> str:
    """Move the state row invited → claimed; a note instead of a failure."""
    if not _has_state_table(con):
        return ""
    store = StateStore(con)
    try:
        tenant = store.get(slug)
        if tenant.state == "invited":
            store.claim(slug)
    except (StateError, UnknownTenant) as error:
        return str(error)
    return ""


def mark_paid(con: sqlite3.Connection, slug: str, plan: str, *,
              now: datetime | None = None) -> sqlite3.Row:
    """Record the manual paid flag: a non-empty plan means the money cleared.

    Stripe replaces this with a webhook; until then the operator runs
    ``claim mark-paid`` when the payment lands. The plan name is recorded, not
    the provider's numeric plan id (that stays ``default_plan_id`` in config).
    """
    value = (plan or "").strip()
    if not value:
        raise ClaimRefused(422, "bad_plan", "a plan name is required")
    tenant = _tenant(con, slug)
    if tenant["state"] in SETTLED_STATES:
        raise ClaimRefused(409, "already_settled", f"{slug} is {tenant['state']}")
    con.execute("UPDATE tenants SET plan = ? WHERE id = ?", (value, tenant["id"]))
    con.commit()
    return _tenant(con, slug)


# ------------------------------------------------------------------ ready email


@dataclass(frozen=True)
class QueuedEmail:
    message: Path
    facts: Path
    kit: Path


class ReadyOutbox:
    """One ready email per tenant: the .eml queue item plus its facts sidecar.

    Files, not a new table, for two reasons: the setup token must be in the
    message the user receives, and the web schema v2 is pinned by tests that
    assert the exact table set. The directory is private (0700) and each file
    is 0600 while it waits for the mailer; ODL-232 owns the final template and
    the send path.
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory).expanduser() if directory else Path(DEFAULT_OUTBOX)

    @property
    def ready_dir(self) -> Path:
        return self.directory / "ready"

    def message_path(self, slug: str) -> Path:
        return self.ready_dir / f"{slug}.eml"

    def facts_path(self, slug: str) -> Path:
        return self.ready_dir / f"{slug}.json"

    def kit_path(self, slug: str) -> Path:
        return self.ready_dir / f"{slug}-recovery-kit.txt"

    def queued(self, slug: str) -> QueuedEmail | None:
        message = self.message_path(slug)
        if not message.is_file() or message.stat().st_size == 0:
            return None
        return QueuedEmail(message, self.facts_path(slug), self.kit_path(slug))

    def facts(self, slug: str) -> dict[str, Any] | None:
        """The staged facts sidecar, or None while the tenant waits."""
        path = self.facts_path(slug)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def stage(self, *, slug: str, facts: dict[str, Any], at: int) -> Path:
        """Write the facts sidecar only: the order's facts, no mail yet.

        ODL-947 splits the claim's ready email off from the order: the facts
        (URL, recipient, machine, token expiry) are recorded when the machine
        is ordered, and the message is written when the machine's own
        `/verify` answers. Nothing here is the commit — `queued()` still
        returns None until the `.eml` exists.
        """
        self.ready_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.ready_dir.chmod(0o700)
        except OSError:
            pass
        facts_path = self.facts_path(slug)
        facts_path.write_text(
            json.dumps({**facts, "at": int(at),
                        "at_iso": iso(datetime.fromtimestamp(int(at), timezone.utc))},
                       indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _chmod(facts_path, 0o600)
        return facts_path

    def queue(
        self,
        *,
        slug: str,
        recipient: str,
        subject: str,
        body: str,
        kit_text: str,
        facts: dict[str, Any],
        at: int,
    ) -> QueuedEmail:
        """Write the kit, the facts, then the message; the message is the commit.

        A crash before the .eml leaves a message that ``queued()`` does not
        report, so the next attempt re-mints and overwrites the partial files.
        """
        self.ready_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.ready_dir.chmod(0o700)
        except OSError:
            pass
        kit_path = self.kit_path(slug)
        kit_path.write_text(kit_text, encoding="utf-8")
        _chmod(kit_path, 0o600)

        message_id = make_msgid(idstring=f"odile-ready.{slug}", domain="odile.fyi")
        facts_path = self.facts_path(slug)
        facts_path.write_text(
            json.dumps({**facts, "message_id": message_id, "at": int(at),
                        "at_iso": iso(datetime.fromtimestamp(int(at), timezone.utc))},
                       indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _chmod(facts_path, 0o600)

        message = EmailMessage()
        message["From"] = READY_SENDER
        message["Reply-To"] = READY_REPLY_TO
        message["To"] = recipient
        message["Subject"] = subject
        message["Date"] = formatdate(int(at), usegmt=True)
        message["Message-ID"] = message_id
        message.set_content(body)
        message.add_attachment(
            kit_text.encode("utf-8"),
            maintype="text",
            subtype="plain",
            filename=f"odile-{slug}-recovery-kit.txt",
        )
        message_path = self.message_path(slug)
        message_path.write_bytes(message.as_bytes())
        _chmod(message_path, 0o600)
        return QueuedEmail(message_path, facts_path, kit_path)


def _chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


# ------------------------------------------------------------------ ready mail
#
# ODL-947: one builder, one definition of ready. The account queue (`app.py`'s
# resend press and the registration worker) and the claim queue both queue this
# message, once, when the machine's own report answers — never when the provider
# says the VM is active. The message is rebuilt from its facts sidecar on every
# later press, so the bytes do not change; the sidecar carries the machine's own
# 96-hex measurement, not the release pin.

_ready_lock = threading.Lock()


def _atomic_write(path: str, data: bytes) -> None:
    """Write one queue file at 0600, replaced atomically under the lock."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    fd = os.open(path + ".tmp", os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(path + ".tmp", path)


def ready_boundary(slug: str, body: str, kit: bytes) -> str:
    """The multipart boundary for one ready email: fixed, and absent from its parts.

    A second press must write the same bytes, so the boundary may not be drawn
    at random. It also may not occur in the body or the kit, or the message
    would split at the wrong line; the suffix loop is deterministic, so two
    builds from the same facts still hash equal.
    """
    base = "odile-ready.%s" % slug
    kit_text = kit.decode("utf-8", "replace")
    candidate = base
    suffix = 0
    while candidate in body or candidate in kit_text:
        suffix += 1
        candidate = "%s-%d" % (base, suffix)
    return candidate


def queue_ready_email(
    outbox: str | Path,
    *,
    slug: str,
    to: str,
    url: str,
    measurement: str | None = None,
    host: str | None = None,
    ipv4: str | None = None,
    server_id: str | None = None,
    release: str | None = None,
    at: int | None = None,
    guide_url: str = READY_GUIDE_URL,
) -> Path:
    """Queue the one token-free ready email for a machine that answered.

    `measurement` is the machine's own boot-report value (the 96 hex its
    `/attestation` carries and its `/verify` page prints). It is written into
    the facts sidecar; every later press rebuilds the same message from those
    facts — same Date, Message-ID, body, attachment and boundary — so the
    resend press cannot change the bytes. A press after the mailer removed the
    `.eml` restores those same bytes rather than minting a new message. With a
    sidecar already present and no `measurement` passed, the sidecar's value is
    the mail's (that is the press's path).
    """
    queue = ReadyOutbox(outbox)
    facts_path = str(queue.facts_path(slug))
    message_path = str(queue.message_path(slug))
    kit_path = queue.kit_path(slug)
    with _ready_lock:
        facts = None
        if os.path.isfile(facts_path):
            try:
                with open(facts_path, encoding="utf-8") as handle:
                    facts = json.load(handle)
            except (OSError, ValueError):
                facts = None
        if facts is None and os.path.isfile(message_path):
            # A message with no facts sidecar is already the queued bytes;
            # re-queue it as it stands and mint nothing.
            os.utime(message_path, None)
            return queue.message_path(slug)
        value = (measurement or (facts or {}).get("measurement") or "").strip().lower()
        if not MEASUREMENT_RE.match(value):
            raise ClaimError("the ready email's measurement is not 96 hex characters")
        if facts is None:
            host = host or url.split("//", 1)[-1].split("/", 1)[0]
            facts = {
                "kind": "ready",
                "slug": slug,
                "to": to,
                "subject": READY_SUBJECT,
                "host": host,
                "url": url,
                "first_visit": url + "/first-visit",
                "guide_url": guide_url,
                "measurement": value,
                "ipv4": ipv4,
                "server_id": server_id,
                "release": release,
                "at": int(at if at is not None else time.time()),
                "message_id": make_msgid(idstring="odile-ready.%s" % slug,
                                         domain="odile.fyi"),
            }
        else:
            # The machine's own value wins over a pin an older sidecar carried
            # (a facts file staged at order time, before the report existed).
            facts["measurement"] = value
        if release:
            # ODL-947 rows: the `tenants` row is the source. A recycle or a
            # re-install can move the release under a sidecar written earlier,
            # and a press must re-print the release HQ holds, not the copy. The
            # release is not printed, so the bytes a press writes do not move.
            facts["release"] = release
        moment = int(facts.get("at") or time.time())
        message_id = facts.get("message_id") or make_msgid(
            idstring="odile-ready.%s" % slug, domain="odile.fyi")
        url = str(facts.get("url") or url)
        # ODL-925: the ready body names the tokenless first-sign-in page; the
        # sidecar's `first_visit` is kept for older facts but no longer passed,
        # because a setup token in a link is what B-once retired.
        body = ready_body(
            url=url,
            measurement=str(facts.get("measurement") or value),
            guide_url=str(facts.get("guide_url") or guide_url),
        )
        message = EmailMessage()
        message["From"] = READY_SENDER
        message["Reply-To"] = READY_REPLY_TO
        message["To"] = str(facts.get("to") or to)
        message["Subject"] = str(facts.get("subject") or READY_SUBJECT)
        message["Date"] = formatdate(moment, usegmt=True)
        message["Message-ID"] = message_id
        message.set_content(body)
        if kit_path.is_file() and kit_path.stat().st_size:
            filename = "odile-%s-recovery-kit.txt" % slug
            kit_bytes = kit_path.read_bytes()
            message.add_attachment(kit_bytes, maintype="text",
                                   subtype="plain", filename=filename)
            # Pin the boundary: `add_attachment` leaves it unset, and each
            # `as_bytes()` would draw a fresh random one, so the same mail
            # would be written with different bytes on every press.
            message.set_boundary(ready_boundary(slug, body, kit_bytes))
            facts["kit_filename"] = filename
        facts["at"] = moment
        facts["at_iso"] = iso(datetime.fromtimestamp(moment, timezone.utc))
        _atomic_write(facts_path, (json.dumps(
            facts, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        _atomic_write(message_path, message.as_bytes())
        return queue.message_path(slug)


def expected_measurement(config: Config) -> str:
    """The release pin's 96-hex launch measurement: the build-time expectation.

    ODL-947 retired it from the ready email, which prints the machine's own boot
    report and legitimately differs; the pin stays the value a rebuild is
    compared with. The release pin (`odile-verify.pin`, updated by ODL-454) is
    the default; an operator can pass ``expected_measurement`` in the config
    while a re-pin is pending. An invalid value is a config error, never a
    sentence on the page.
    """
    value = (config.get("expected_measurement") or "").strip().lower()
    if not value:
        try:
            value = str(json.loads(
                MEASUREMENT_PIN.read_text(encoding="utf-8")
            ).get("measurement", "")).strip().lower()
        except (OSError, ValueError):
            value = ""
    if not value:
        raise ConfigError(
            "claim: no expected measurement in the config or "
            f"{MEASUREMENT_PIN}"
        )
    if not MEASUREMENT_RE.match(value):
        raise ConfigError(
            f"claim: expected_measurement {value!r} is not 96 hex characters"
        )
    return value


def ready_body(*, url: str, measurement: str,
               guide_url: str = READY_GUIDE_URL) -> str:
    """The ready email's plain-text body from `templates/emails/ready.txt`.

    The machine URL and the measurement the machine reports are printed values.
    The one action names the machine's tokenless first-visit page (B-once,
    ODL-925): the user signs in with the password chosen at registration, and no
    setup token travels in the message. The guide is the one navigation link. No
    command, no key, no passphrase.
    """
    template = READY_TEMPLATE.read_text(encoding="utf-8")
    return (
        template
        .replace("{{URL}}", url)
        .replace("{{MEASUREMENT}}", measurement)
        .replace("{{GUIDE}}", guide_url)
    )


# --------------------------------------------------------------------- kit


@lru_cache(maxsize=1)
def _kit_module():
    """Load ``scripts/recovery_kit.py`` without making ``scripts`` a package."""
    root = Path(__file__).resolve().parent.parent
    path = root / "scripts" / "recovery_kit.py"
    spec = importlib.util.spec_from_file_location("odile_recovery_kit", path)
    if spec is None or spec.loader is None:
        raise ClaimError(f"cannot load the recovery kit generator at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def render_recovery_kit(
    *,
    slug: str,
    ipv4: str,
    instance_id: str,
    token: str | None,
    measurement: str,
    first_visit_url: str,
    expires_at: str | None,
    release: str,
    vcpus: int,
    backup_repo_url: str | None,
    written_at: str,
    account_host: str | None = None,
) -> str:
    """Render the recovery kit from the values this run minted.

    The kit is the machine's facts and the tokenless first-sign-in page
    (ODL-925); the setup token is no longer a kit value — its two jobs are the
    seed exchange and the rebuild seal, and neither is printed. ``account_host``
    is the account site the invitation
    email names (the operator's ``account_host`` config key): when it is set the
    account-reset sentence prints ``<host>/forgot``; when it is absent the kit
    keeps the sentence that points at the invitation email instead.
    """
    module = _kit_module()
    args = argparse.Namespace(
        slug=slug,
        ipv4=ipv4,
        instance_id=instance_id,
        token=token,
        measurement=measurement,
        first_visit_url=first_visit_url,
        expires_at=expires_at,
        release=release,
        vcpus=vcpus,
        signing_key=str(module.SIGNING_KEY),
        backup_repo_url=backup_repo_url,
        written_at=written_at,
        account_host=account_host,
    )
    template = module.TEMPLATE.read_text(encoding="utf-8")
    return module.render(template, module.build_values(args))


# --------------------------------------------------------------------- alert


def _alert_failure(
    config: Config,
    *,
    slug: str,
    tenant: sqlite3.Row,
    reason: str,
    server_id: str | None,
    terminated: bool,
    at: int,
) -> str:
    """Put one provision failure where a human reads it; return the record path.

    The local outbox is always written (it is the fixture's evidence and the
    production copy); SMTP and Matrix are added only when the operator has
    configured them. A failed provision is the one alert this card owns.
    """
    outbox = Outbox(config.path_value("outbox", DEFAULT_OUTBOX), sender=READY_SENDER)
    subject = f"[Odile] provision failed \u2014 {slug}"
    body = "\n".join(
        [
            subject,
            f"tenant:   {tenant['email']}",
            f"slug:     {slug}",
            f"symptom:  {reason}",
            f"server:   {server_id or '(none created)'}",
            f"cleanup:  {'machine terminated; no billed resource remains' if terminated else 'no machine was created'}",
            f"retry:    python3 -m odilectl.claim run --apply {slug}",
            f"detected: {iso(datetime.fromtimestamp(int(at), timezone.utc))}",
            "",
            "The claim stays usable: the same claim token replays and the",
            "tenant is left at 'claimed' for the next run.",
            "",
        ]
    )
    recipient = config.get("alert_email") or READY_REPLY_TO
    delivery = outbox.record(
        kind="provision_failed",
        slug=slug,
        subject=subject,
        body=body,
        channel="outbox",
        recipient=recipient,
        at=at,
        email=True,
    )
    if config.get("smtp_host"):
        try:
            SmtpSender.from_config(config).send(recipient, subject, body, at=at)
        except (ConfigError, OdileError):
            pass
    if config.get("matrix_room_id"):
        try:
            MatrixSender.from_config(config).send(
                body, at=at, slug=slug, kind="provision_failed"
            )
        except (ConfigError, OdileError):
            pass
    return delivery.detail


# --------------------------------------------------------------------- provision


def _plan_id(config: Config) -> int:
    raw = config.get("default_plan_id")
    if raw is None:
        raise ConfigError("claim: set default_plan_id in the config (mode 0600)")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"claim: default_plan_id {raw!r} is not an integer") from None
    if value <= 0:
        raise ConfigError("claim: default_plan_id must be positive")
    return value


def _days(config: Config) -> int:
    raw = config.get("default_days", "31") or "31"
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"claim: default_days {raw!r} is not an integer") from None


def _release(config: Config) -> str:
    """The release the machine is ordered to boot: the pin's `provision_release`.

    `odile-verify.pin` is the one source (ODL-398); `release_pin` in the config
    points at another copy when a re-pin is staged before the repo one lands.
    The provider's `measured_release` is an observation and is never read here.
    """
    return pinned_release(config.path_value("release_pin", DEFAULT_PIN), prefix="claim")


def _vcpus(config: Config) -> int:
    raw = config.get("default_vcpus")
    if raw is None:
        return _kit_module().DEFAULT_VCPUS
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"claim: default_vcpus {raw!r} is not an integer") from None


def _dns_client(config: Config, *, token_file: str | None = None,
                transport: Callable[[str, str, dict[str, str], bytes | None],
                                    tuple[int, bytes]] | None = None) -> CloudflareDNS:
    ctx = Context(config=config)
    token = dns_module.load_token(ctx, argparse.Namespace(token_file=token_file))
    zone = dns_module.validate_zone(config.get("dns_zone") or DEFAULT_ZONE)
    return CloudflareDNS(
        token, zone, zone_id=config.get("dns_zone_id"), transport=transport
    )


def _find_machine(provider: Provider, slug: str) -> Machine | None:
    """The live machine for this slug, or None; two live rows are a refusal.

    A terminated or failed order is ignored: the hostname is ours, but the
    machine is not a resource, and the provision path should order a new one.
    """
    machines = [
        machine
        for machine in provider.list()
        if machine.hostname == slug and machine.state not in ("terminated", "failed")
    ]
    if len(machines) > 1:
        raise ProviderError(
            f"claim: {len(machines)} machines are named {slug}; refusing to guess"
        )
    return machines[0] if machines else None


def _guest_domain(config: Config) -> str:
    """The suffix the machine's own name hangs under (the bench's `test.…`)."""
    return dns_module.tenant_zone(config)


def _default_guest_probe(domain: str, slug: str):
    from .health import guest_answer

    return guest_answer(domain, slug)


def _finish_ready(
    con: sqlite3.Connection,
    slug: str,
    *,
    config: Config,
    outbox: ReadyOutbox,
    now: int | None = None,
    guest_probe: Callable[[str, str], Any] | None = None,
) -> ProvisionResult:
    """Queue the one ready email once the machine's own `/verify` answers.

    ODL-947: the claim path's email used to be written when the provider said
    the VM was active, which is not when the product's machine exists. The
    order still records its facts (`ReadyOutbox.stage`); this pass asks the
    machine itself, and only a 200 from `/verify` with the machine's own
    `/attestation` report moves the row. The kit and the mail are rebuilt from
    those facts and the machine's measurement, so a later press on the account
    page writes the same bytes (the facts sidecar carries the value).
    """
    tenant = _tenant(con, slug)
    moment = int(now if now is not None else time.time())
    facts = outbox.facts(slug)
    if facts is None:
        return ProvisionResult(
            slug=slug, ok=True, skipped=True, pending=True,
            server_id=tenant["server_id"], ipv4=tenant["ipv4"],
            expires_at=tenant["expires_at"], reason="no staged facts",
            state_note="waiting for the order's facts",
        )
    probe = guest_probe or _default_guest_probe
    domain = _guest_domain(config)
    answer = probe(domain, slug)
    if answer is None:
        return ProvisionResult(
            slug=slug, ok=True, skipped=True, pending=True,
            server_id=tenant["server_id"], ipv4=tenant["ipv4"],
            expires_at=tenant["expires_at"],
            reason="waiting for the machine's /verify",
            state_note="the machine has not answered at its own name yet",
        )

    measurement = str(answer.measurement)
    url = str(facts.get("url") or "https://%s.%s" % (slug, domain))
    host = str(facts.get("host") or "%s.%s" % (slug, domain))
    release = str(facts.get("release") or tenant["release"] or "")
    kit_path = outbox.kit_path(slug)
    try:
        kit_text = render_recovery_kit(
            slug=slug,
            ipv4=str(tenant["ipv4"]),
            instance_id=str(tenant["server_id"]),
            token=None,  # B-once: the kit prints no setup token
            measurement=measurement,
            first_visit_url=url + "/first-visit",
            # The token's own expiry is retired from the kit (ODL-925); the
            # kit's `written_at` is now the moment the machine answered.
            expires_at=None,
            release=release,
            vcpus=_vcpus(config),
            backup_repo_url=_backup_repo(config, slug),
            written_at=iso(datetime.fromtimestamp(moment, timezone.utc)),
            account_host=config.get("account_host"),
        )
        kit_path.write_text(kit_text, encoding="utf-8")
        _chmod(kit_path, 0o600)
        facts.update({
            "measurement": measurement,
            "host": host,
            "url": url,
            "ipv4": tenant["ipv4"],
            "server_id": tenant["server_id"],
            "release": release,
            "kit_filename": "odile-%s-recovery-kit.txt" % slug,
        })
        outbox.stage(slug=slug, facts=facts, at=moment)
        # One builder, both queues: the same function the account page's press
        # calls, rebuilding from the facts sidecar (so the mail's bytes do not
        # change) with the machine's own measurement.
        message = str(queue_ready_email(
            str(outbox.directory),
            slug=slug,
            to=str(facts.get("to") or tenant["email"]),
            url=url,
            host=host,
            measurement=measurement,
            ipv4=tenant["ipv4"],
            server_id=tenant["server_id"],
            release=release,
            at=int(facts.get("at") or moment),
        ))
    except (ClaimError, OSError, ValueError, RuntimeError) as error:
        # The machine exists; a failed write is retried by the next run with
        # the same facts, so it must not become a failed provision.
        return ProvisionResult(
            slug=slug, ok=True, skipped=True, pending=True,
            server_id=tenant["server_id"], ipv4=tenant["ipv4"],
            expires_at=tenant["expires_at"],
            reason="the ready email could not be written: %s" % str(error)[:200],
            state_note="retrying the ready email",
        )
    _event(con, int(tenant["id"]), "provisioned", {
        "step": "ready",
        "server_id": tenant["server_id"],
        "ready_email": message,
        "kit": str(kit_path),
        "measurement": measurement,
        "verify_bytes": answer.verify_bytes,
        "at": iso(datetime.fromtimestamp(moment, timezone.utc)),
    })
    con.commit()
    return ProvisionResult(
        slug=slug, ok=True, adopted=True,
        server_id=tenant["server_id"], ipv4=tenant["ipv4"],
        expires_at=tenant["expires_at"], kit_path=str(kit_path),
        email_path=message, reason="",
        state_note="the machine answered at its own name",
    )


def provision_tenant(
    con: sqlite3.Connection,
    slug: str,
    *,
    provider: Provider,
    config: Config,
    dns_client: CloudflareDNS | None = None,
    outbox: ReadyOutbox | None = None,
    now: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
    guest_probe: Callable[[str, str], Any] | None = None,
) -> ProvisionResult:
    """Provision one paid, claimed tenant; safe to run again.

    The order is the card's: order the machine, wait for it, write the DNS
    record, mint the setup token, stage the ready facts. Every step is checked
    before the next, and a failure after the machine exists terminates it
    before the failure is recorded, so a failed attempt leaves no billed
    resource.

    ODL-947 splits the ready email off the order: the row and the facts are
    written here, and the one mail is queued by `_finish_ready` when the
    machine's own `/verify` answers. A later run of the timer re-enters this
    function for the same tenant and goes straight to that second phase.
    """
    moment = int(now if now is not None else time.time())
    tenant = _tenant(con, slug)
    if not tenant["ssh_public_key"]:
        raise ClaimRefused(409, "no_key", f"{slug} has no boot public key yet")
    if not is_paid(tenant):
        raise ClaimRefused(409, "unpaid", f"{slug} has no recorded plan; mark it paid first")
    outbox = outbox or ReadyOutbox(config.path_value("outbox", DEFAULT_OUTBOX))

    existing = outbox.queued(slug)
    if (tenant["server_id"] and tenant["state"] in READY_STATES
            and _has_ready_event(con, int(tenant["id"]))):
        return ProvisionResult(
            slug=slug,
            ok=True,
            skipped=True,
            server_id=tenant["server_id"],
            ipv4=tenant["ipv4"],
            expires_at=tenant["expires_at"],
            kit_path=str(existing.kit) if existing else None,
            email_path=str(existing.message) if existing else None,
            state_note=_advance_state_provisioned(con, slug, backfill=tenant["release"]),
        )

    # The machine is ordered and its facts are staged: this is the second
    # phase, and it must not rotate the token the machine is already using.
    if (tenant["state"] == "provisioned" and tenant["server_id"]
            and outbox.facts(slug) is not None):
        return _finish_ready(con, slug, config=config, outbox=outbox,
                             now=moment, guest_probe=guest_probe)

    # Validate the release pin's measurement before any spend: an invalid
    # measurement is a config error, not a machine that gets built
    # and then terminated. The boot release is the pin's instruction, not the
    # provider's report; an unpinned release fails here, before the order.
    release = _release(config)
    measurement = expected_measurement(config)

    before = dict(tenant)
    machine: Machine | None = None
    created_here = False
    adopted = False
    staged = False
    dns_client = dns_client or _dns_client(config)
    # The tenant's own name (its URL and its A record) is `tenant_domain` when
    # the config sets it; the client still talks to `dns_zone`.
    domain = dns_module.tenant_zone(config)
    name = record_name(slug, domain)
    dns_action: str | None = None
    dns_record_id: str | None = None
    dns_written = False
    queued: QueuedEmail | None = None
    try:
        # The tenant's BX11 sub-account pair, minted and recorded before any
        # paid call — the same recorder `odilectl provision` runs (ODL-924).
        # The 51st pair, a half pair or a box the store cannot match all
        # refuse here, so no machine is billed; a claim is a first machine,
        # never a rebuild, so `previous_instance` is "".
        backup = _provision_backup(Context(config=config), slug, "")
        machine = _find_machine(provider, slug)
        if machine is not None:
            adopted = True
        else:
            adopted = False
            spec = CreateSpec(
                hostname=slug,
                plan_id=_plan_id(config),
                ssh_key=tenant["ssh_public_key"],
                days=_days(config),
            )
            machine = provider.create(spec)
            created_here = True
        if machine.state != "active" or not machine.ipv4:
            machine = provider.poll(machine.server_id, timeout=timeout, interval=interval)
        if machine.state != "active" or not machine.ipv4:
            raise ProviderError(
                f"claim: {machine.server_id} is {machine.raw_status or machine.state} "
                "with no IPv4 address"
            )
        server_id = machine.server_id
        ipv4 = machine.ipv4

        change = dns_client.ensure(name, ipv4, PROVISION_TTL, apply=True)
        dns_action = change.action
        dns_record_id = change.record.id if change.record else None
        dns_written = True

        # The tenant row now points at the machine. `provisioned` is the row's
        # word for "the machine exists"; the ready email is the next event. The
        # release is the pin's instruction, recorded beside the order (ODL-398):
        # `tenants.release` is what ODL-232 reads, `tenant_state.release` is
        # what `odilectl status` compares with the guest's heartbeat. The
        # `COALESCE` is only the staging write — it keeps an existing row's
        # value on this machine's behalf — and `_advance_state_provisioned`
        # converges both rows on the order's release before the run is done, so
        # a rebuilt tenant does not keep the dead machine's release (ODL-947).
        con.execute(
            "UPDATE tenants SET server_id = ?, ipv4 = ?, expires_at = ?,"
            " state = 'provisioned', setup_token_hash = NULL,"
            " release = COALESCE(release, ?) WHERE id = ?",
            (server_id, ipv4, iso_epoch(machine.expires_at), release, tenant["id"]),
        )
        _event(con, int(tenant["id"]), "provisioned", {
            "step": "vm",
            "server_id": server_id,
            "ipv4": ipv4,
            "plan_id": machine.plan_id,
            "release": release,
            "expires_at": iso_epoch(machine.expires_at),
            "adopted": adopted,
            "at": iso(datetime.fromtimestamp(moment, timezone.utc)),
        })
        con.commit()
        staged = True

        issued = _SEED.rotate(
            con,
            slug,
            instance_id=server_id,
            ttl_hours=SETUP_TOKEN_TTL_HOURS,
            now=datetime.fromtimestamp(moment, timezone.utc),
        )
        token = issued["token"]
        con.execute(
            "UPDATE tenants SET setup_token_hash = ? WHERE id = ?",
            (hash_token(token), tenant["id"]),
        )
        con.commit()

        host = f"{slug}.{domain}"
        url = f"https://{host}"
        # ODL-947: the order records its facts; the message itself waits for
        # the machine's own /verify (the claim used to queue it here, when the
        # provider said "active" and the guest half did not exist yet).
        outbox.stage(
            slug=slug,
            facts={
                "kind": "ready",
                "slug": slug,
                "tenant_id": int(tenant["id"]),
                "to": tenant["email"],
                "subject": READY_SUBJECT,
                "server_id": server_id,
                "ipv4": ipv4,
                "host": host,
                "url": url,
                "measurement": measurement,
                "guide_url": READY_GUIDE_URL,
                "release": release,
                "instance_id": server_id,
                "token_expires": issued["expires_at"],
                "kit_filename": f"odile-{slug}-recovery-kit.txt",
            },
            at=moment,
        )
        con.commit()
    except Exception as error:  # the one place a failure becomes a clean state
        con.rollback()  # discard a Stage A write whose commit did not land
        reason = _reason(error)
        terminated = False
        if machine is not None and machine.server_id:
            try:
                provider.terminate(machine.server_id)
                terminated = True
            except Exception as cleanup:
                reason += f"; terminate failed: {_reason(cleanup)}"
        if dns_written:
            try:
                dns_client.remove(name, apply=True)
            except Exception as cleanup:
                reason += f"; DNS cleanup failed: {_reason(cleanup)}"
        if staged:
            _restore_tenant(con, int(tenant["id"]), before)
        if queued is not None:
            for path in (queued.message, queued.facts, queued.kit):
                try:
                    path.unlink()
                except OSError:
                    pass
        alert_path = _alert_failure(
            config,
            slug=slug,
            tenant=tenant,
            reason=reason,
            server_id=machine.server_id if machine else None,
            terminated=terminated,
            at=moment,
        )
        _record_failure(con, int(tenant["id"]), reason=reason, server_id=(
            machine.server_id if machine else None
        ), terminated=terminated, at=moment)
        return ProvisionResult(
            slug=slug,
            ok=False,
            created=created_here,
            adopted=adopted,
            server_id=machine.server_id if machine else None,
            ipv4=machine.ipv4 if machine else None,
            dns_action=dns_action,
            dns_record_id=dns_record_id,
            dns_name=name,
            terminated=terminated,
            reason=reason,
            alert_path=alert_path,
        )

    note = _advance_state_provisioned(con, slug, release=release,
                                      backup=_pair_detail(backup.pair))
    finished = _finish_ready(con, slug, config=config, outbox=outbox,
                             now=moment, guest_probe=guest_probe)
    return ProvisionResult(
        slug=slug,
        ok=True,
        created=created_here,
        adopted=adopted,
        skipped=finished.skipped,
        pending=finished.pending,
        server_id=machine.server_id,
        ipv4=machine.ipv4,
        expires_at=iso_epoch(machine.expires_at),
        machine=machine.to_dict(),
        dns_action=dns_action,
        dns_record_id=dns_record_id,
        dns_name=name,
        token_expires=issued["expires_at"],
        kit_path=finished.kit_path,
        email_path=finished.email_path,
        reason=finished.reason,
        state_note="; ".join(part for part in (note, finished.state_note) if part),
    )


def _reason(error: BaseException) -> str:
    text = str(error).strip() or error.__class__.__name__
    return text[:500]


def _restore_tenant(con: sqlite3.Connection, tenant_id: int, before: dict[str, Any]) -> None:
    """Put back the row fields this attempt staged; the events stay."""
    con.execute(
        "UPDATE tenants SET server_id = ?, ipv4 = ?, expires_at = ?, state = ?,"
        " setup_token_hash = ?, release = ? WHERE id = ?",
        (
            before.get("server_id"),
            before.get("ipv4"),
            before.get("expires_at"),
            before.get("state"),
            before.get("setup_token_hash"),
            before.get("release"),
            tenant_id,
        ),
    )
    con.commit()


def _record_failure(
    con: sqlite3.Connection,
    tenant_id: int,
    *,
    reason: str,
    server_id: str | None,
    terminated: bool,
    at: int,
) -> None:
    _event(con, tenant_id, "claimed", {
        "step": "provision_failed",
        "reason": reason,
        "server_id": server_id,
        "terminated": terminated,
        "at": iso(datetime.fromtimestamp(at, timezone.utc)),
    })
    con.commit()


def _backup_repo(config: Config, slug: str) -> str | None:
    raw = config.get("backup_repo")
    return raw.replace("{slug}", slug) if raw else None


def _pair_detail(pair: BackupPair) -> dict[str, Any]:
    """The row's cache of a minted pair: the same seven keys `provision` writes.

    `odilectl.provision._record_backup_detail` is the other writer of this
    shape, and the claim path must not spell it differently: the ids and the
    provider logins are cached here so a later run can name the pair without
    the root-only store of record. The env file stays the truth.
    """
    return {
        "backup_box_id": pair.box_id,
        "backup_subaccount_id": pair.subaccount_id,
        "backup_subaccount_username": pair.subaccount_username,
        "backup_readonly_subaccount_id": pair.readonly_subaccount_id,
        "backup_readonly_subaccount_username": pair.readonly_subaccount_username,
        "backup_repository": pair.repo_url,
        "previous_instance": pair.previous_instance,
    }


def _advance_state_provisioned(
    con: sqlite3.Connection,
    slug: str,
    *,
    release: str | None = None,
    backfill: str | None = None,
    backup: dict[str, Any] | None = None,
) -> str:
    """Record the machine on the state spine: claimed → provisioning → first_boot.

    The state machine has no retryable ``failed`` state, so it is advanced only
    after the machine is recorded. ODL-947: the ready email is no longer part
    of this step — the order stages its facts and the machine's own answer
    queues it — so the detail says `ready: False` until that later run. A
    failure here is a note: the tenant and the staged facts are already
    correct, and a later run repairs the state row.

    ``release`` is the pin's ``provision_release``: the order's instruction, and
    the value both release rows must name once it is written. It is written in
    the same transition that records the order; a row that predates ODL-398 (or
    a second run) gets the value through ``record_release``, which converges both
    rows unless the machine has already reported what it installed — that
    observation wins (ODL-947). A rebuilt tenant reaches this write with the web
    row still naming the dead machine (`claim.provision_tenant`'s order write is
    `COALESCE`), so the order's pin has to move the web row too, not only fill
    an empty one.

    ``backfill`` is the release the caller found on the web row, for a run that
    places no order (the skip path). It is used only when the spine has none: a
    skip is not a new order, and the web row's `COALESCE` value is never newer
    than the spine's — a re-ordered tenant's web row still names the dead
    machine — so replaying it as the order's release would walk the spine
    backwards. A spine with a value is the record the web row converges to.

    ``backup`` is the pair's cache from `_pair_detail`, merged into this one
    detail write: `store.provision` replaces the row's detail, so a separate
    `note` before it would be thrown away (ODL-924 follow-up).
    """
    if not _has_state_table(con):
        return ""
    store = StateStore(con)
    try:
        try:
            tenant = store.get(slug)
        except UnknownTenant:
            tenant = store.invite(slug)
        if tenant.state == "invited":
            tenant = store.claim(slug)
        if tenant.state == "claimed":
            tenant = store.provision(
                slug,
                server_id=_tenant(con, slug)["server_id"] or "",
                hostname=slug,
                ipv4=_tenant(con, slug)["ipv4"],
                release=release,
                detail={"source": "claim", "ready": False,
                        "ready_note": "waiting for the machine's /verify",
                        **(backup or {})},
            )
        if tenant.state == "provisioning":
            tenant = store.await_boot(slug)
        if tenant.state != "destroyed":
            # The order's pin first, then the spine's own record (a skip), then
            # the web row's value only to fill an empty spine.
            chosen = release or tenant.release or backfill
            if chosen:
                tenant = store.record_release(slug, chosen)
    except (StateError, UnknownTenant, ValueError) as error:
        return str(error)
    return ""


# --------------------------------------------------------------------- the queue


def eligible(con: sqlite3.Connection, outbox: ReadyOutbox | None = None,
             slug: str | None = None) -> list[sqlite3.Row]:
    """The rows that are still work: claimed or provisioned, paid, keyed.

    A ``ready`` event in the append-only log is the done marker. The outbox
    argument is kept for callers that have one; the mailer may remove the
    message, so the file is not the source of truth.
    """
    sql = (
        "SELECT * FROM tenants AS t WHERE plan IS NOT NULL AND trim(plan) <> ''"
        " AND ssh_public_key IS NOT NULL AND claimed_at IS NOT NULL"
        " AND state IN ('claimed', 'provisioned')"
        " AND NOT EXISTS (SELECT 1 FROM events AS e WHERE e.tenant_id = t.id"
        " AND e.kind = 'provisioned' AND json_extract(e.detail, '$.step') = 'ready')"
    )
    values: list[Any] = []
    if slug is not None:
        sql += " AND slug = ?"
        values.append(slug)
    sql += " ORDER BY t.id"
    return con.execute(sql, values).fetchall()


def run_pending(
    con: sqlite3.Connection,
    *,
    provider_factory: Callable[[Config], Provider],
    config: Config,
    dns_client: CloudflareDNS | None = None,
    outbox: ReadyOutbox | None = None,
    slug: str | None = None,
    now: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
    guest_probe: Callable[[str, str], Any] | None = None,
) -> list[ProvisionResult]:
    """Provision every eligible tenant, one at a time; return every result."""
    outbox = outbox or ReadyOutbox(config.path_value("outbox", DEFAULT_OUTBOX))
    results: list[ProvisionResult] = []
    provider: Provider | None = None
    for tenant in eligible(con, outbox, slug):
        if provider is None:
            provider = provider_factory(config)
        results.append(
            provision_tenant(
                con,
                tenant["slug"],
                provider=provider,
                config=config,
                dns_client=dns_client,
                outbox=outbox,
                now=now,
                timeout=timeout,
                interval=interval,
                guest_probe=guest_probe,
            )
        )
    return results


# --------------------------------------------------------------------- status


def tenant_status(
    con: sqlite3.Connection,
    slug: str,
    *,
    outbox: ReadyOutbox | None = None,
) -> dict[str, Any]:
    """The operator's answer for one tenant, including a failed attempt.

    ``tenant_state`` is the web row (schema v2 has no failed state); the
    ``provision`` word is derived from the latest append-only event, so a
    failed attempt is visible without rewriting the tenant row.
    """
    tenant = _tenant(con, slug)
    outbox = outbox or ReadyOutbox()
    events = _events(con, int(tenant["id"]))
    latest = None
    provision = "not_started"
    if tenant["state"] == "provisioned" and tenant["server_id"]:
        provision = "provisioned"
    for row in events:
        detail = _json(row["detail"])
        step = detail.get("step")
        if step == "provision_failed":
            provision = "failed"
            latest = detail
            break
        if step == "ready":
            provision = "ready"
            latest = detail
            break
        if step == "vm":
            # ODL-947: the machine is ordered and the ready email is waiting
            # for the machine's own /verify; this is a state, not a failure.
            provision = "waiting_for_guest"
            latest = detail
            break
    queued = outbox.queued(slug)
    return {
        "slug": tenant["slug"],
        "email": tenant["email"],
        "tenant_state": tenant["state"],
        "provision": provision,
        "paid": is_paid(tenant),
        "plan": tenant["plan"],
        "server_id": tenant["server_id"],
        "ipv4": tenant["ipv4"],
        "expires_at": tenant["expires_at"],
        # `status` opens the file without the additive schema (it is the
        # read-only command), so a database that predates v3 has no column yet.
        "release": tenant["release"] if "release" in tenant.keys() else None,
        "claimed_at": tenant["claimed_at"],
        "setup_token_hash": (tenant["setup_token_hash"] or "")[:16] or None,
        "ready_email": str(queued.message) if queued else None,
        "kit": str(queued.kit) if queued else None,
        "last_event": latest,
    }


# --------------------------------------------------------------------- CLI


def _action_rows(results: Sequence[ProvisionResult]) -> list[str]:
    lines: list[str] = []
    for result in results:
        if result.ok and result.pending:
            lines.append(
                f"  {result.slug}: {result.server_id} active at {result.ipv4}; "
                f"waiting for the machine's /verify before the ready email"
            )
        elif result.ok and result.skipped:
            lines.append(f"  {result.slug}: already ready ({result.server_id})")
        elif result.ok:
            lines.append(
                f"  {result.slug}: {result.server_id} active at {result.ipv4}; "
                f"email queued at {result.email_path}"
            )
        else:
            lines.append(f"  {result.slug}: FAILED - {result.reason}")
            lines.append(f"    machine {result.server_id or '-'} terminated={result.terminated}")
            lines.append(f"    alert {result.alert_path}")
    return lines


def _add_flags(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    """The flags are valid before or after the subcommand (same rule as cli.py)."""
    default: object = argparse.SUPPRESS if suppress else False
    config_default: object = argparse.SUPPRESS if suppress else None
    parser.add_argument("--config", metavar="PATH", default=config_default,
                        help="config file, mode 0600")
    parser.add_argument("--dry-run", action="store_true", default=default,
                        help="print the plan, change nothing")
    parser.add_argument("--apply", action="store_true", default=default,
                        help="execute the change")
    parser.add_argument("--json", action="store_true", default=default,
                        help="print one JSON object")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="odilectl claim",
        description=f"{HELP}\n\nExample: {EXAMPLE}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_flags(parser)
    sub = parser.add_subparsers(dest="verb", required=True)

    invite = sub.add_parser("invite", help="create the tenant row and one claim token")
    _add_flags(invite, suppress=True)
    invite.add_argument("email")
    invite.add_argument("--ttl-hours", type=int, default=DEFAULT_TTL_HOURS)
    invite.add_argument("--plan", help="record the plan now (pre-paid flow)")
    invite.add_argument("--slug", help="a fixture slug (tests only)")

    accept_p = sub.add_parser("accept", help="accept a claim token with the boot public key")
    _add_flags(accept_p, suppress=True)
    accept_p.add_argument("token")
    accept_p.add_argument("--key", required=True, metavar="PATH",
                          help="the user's SSH public key file")
    accept_p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    accept_p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)

    paid = sub.add_parser("mark-paid", help="record the manual paid flag (plan)")
    _add_flags(paid, suppress=True)
    paid.add_argument("slug")
    paid.add_argument("--plan", default=DEFAULT_PLAN_NAME)
    paid.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    paid.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)

    run_p = sub.add_parser("run", help="provision every paid, claimed tenant")
    _add_flags(run_p, suppress=True)
    run_p.add_argument("slug", nargs="?")
    run_p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    run_p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)

    status_p = sub.add_parser("status", help="read one tenant or every tenant")
    _add_flags(status_p, suppress=True)
    status_p.add_argument("slug", nargs="?")
    return parser


def _read_key(path: str) -> str:
    try:
        return Path(path).expanduser().read_text(encoding="utf-8")
    except FileNotFoundError:
        raise UsageError(f"claim: no public key at {path}") from None
    except OSError as error:
        raise UsageError(f"claim: cannot read {path}: {error.strerror}") from None


def _db_path(config: Config) -> Path:
    return config.path_value("state_db", DEFAULT_STATE_DB) or Path(DEFAULT_STATE_DB)


def peek_claim(con: sqlite3.Connection, token: str) -> dict[str, Any]:
    """Read the claim without spending it, so a dry run can print the plan."""
    if not valid_claim_token(token or ""):
        raise ClaimRefused(404, "unknown_claim", "That claim link is not valid.")
    row = con.execute(
        "SELECT c.used_at, c.expires_at, t.slug, t.email, t.state, t.plan"
        " FROM claims AS c JOIN tenants AS t ON t.id = c.tenant_id"
        " WHERE c.token_hash = ?",
        (hash_token(token),),
    ).fetchone()
    if row is None:
        raise ClaimRefused(404, "unknown_claim", "That claim link is not valid.")
    if row["used_at"] is None and row["expires_at"] <= iso(utcnow()):
        raise ClaimRefused(410, "expired", "That claim link has expired.")
    return dict(row)


def _provision_actions(slug: str, config: Config) -> list[dict[str, Any]]:
    return [
        {"action": "provider.list", "hostname": slug},
        {"action": "provider.create", "hostname": slug, "plan_id": _plan_id(config),
         "days": _days(config), "snp": True, "fde": True, "measured": True},
        {"action": "provider.poll", "until": "active"},
        {"action": "dns.create", "name": f"{slug}.{dns_module.tenant_zone(config)}",
         "ttl": PROVISION_TTL, "proxied": False},
        {"action": "seed.token.rotate", "slug": slug, "ttl_hours": SETUP_TOKEN_TTL_HOURS},
        {"action": "kit.render", "slug": slug, "release": _release(config)},
        {"action": "email.queue", "slug": slug, "subject": READY_SUBJECT},
    ]


def cmd_invite(con: sqlite3.Connection, config: Config, args) -> tuple[Report, bool]:
    if not args.apply:
        actions = [
            {"action": "tenant.create", "email": args.email, "plan": args.plan},
            {"action": "claim.issue", "ttl_hours": args.ttl_hours},
        ]
        return plan(NAME, Context(config=config), actions,
                    ["the claim token is printed once; only its hash is stored"]), True
    issued = issue_claim(con, args.email, ttl_hours=args.ttl_hours, plan=args.plan,
                         slug=args.slug)
    lines = [
        "claim: invited",
        f"  tenant: {issued.slug}",
        f"  email:  {issued.email}",
        f"  plan:   {issued.plan or '-'}",
        f"  expires:{issued.expires_at}",
        f"  token:  {issued.token}",
        "  the token is printed once; only its SHA-256 hash is stored",
    ]
    return Report({"ok": True, "command": NAME, "dry_run": False, **issued.to_dict()}, lines), True


def _injected_dns(config: Config, transport):
    """A DNS client for the CLI when the test seam supplies a transport."""
    if transport is None:
        return None
    return _dns_client(config, transport=transport)


def cmd_accept(con: sqlite3.Connection, config: Config, args, ctx: Context,
               *, dns_transport=None, guest_probe=None) -> tuple[Report, bool]:
    if not args.apply:
        preview = peek_claim(con, args.token)
        actions = [{"action": "claim.accept", "slug": preview["slug"]}]
        if (preview["plan"] or "").strip() and preview["state"] in WORKING_STATES:
            actions += _provision_actions(preview["slug"], config)
        return plan(NAME, ctx, actions,
                    ["a used token replays for the same key; a different key is refused"]), True
    result = accept(con, args.token, _read_key(args.key))
    lines = [
        "claim: accepted" + (" (replay)" if result.replay else ""),
        f"  tenant: {result.slug}",
        f"  state:  {result.state}",
        f"  key:    {result.key_fingerprint}",
        f"  plan:   {result.plan or '-'}",
    ]
    ok = True
    payload: dict[str, Any] = {"claim": result.to_dict()}
    if result.paid and result.state in WORKING_STATES:
        provision = provision_tenant(
            con, result.slug, provider=ctx.provider, config=config,
            dns_client=_injected_dns(config, dns_transport),
            timeout=args.timeout, interval=args.interval,
            guest_probe=guest_probe,
        )
        payload["provision"] = provision.to_dict()
        lines += _action_rows([provision])
        ok = provision.ok
    return Report({"ok": ok, "command": NAME, "dry_run": False, **payload}, lines), ok


def cmd_mark_paid(con: sqlite3.Connection, config: Config, args, ctx: Context,
                  *, dns_transport=None, guest_probe=None) -> tuple[Report, bool]:
    if not args.apply:
        actions = [{"action": "tenant.mark_paid", "slug": args.slug, "plan": args.plan}]
        actions += _provision_actions(args.slug, config)
        return plan(NAME, ctx, actions,
                    ["a non-empty plan is the manual paid flag until Stripe"]), True
    tenant = mark_paid(con, args.slug, args.plan)
    lines = [f"claim: {tenant['slug']} marked paid ({tenant['plan']})"]
    ok = True
    payload: dict[str, Any] = {"slug": tenant["slug"], "plan": tenant["plan"]}
    if tenant["ssh_public_key"] and tenant["claimed_at"]:
        provision = provision_tenant(
            con, args.slug, provider=ctx.provider, config=config,
            dns_client=_injected_dns(config, dns_transport),
            timeout=args.timeout, interval=args.interval,
            guest_probe=guest_probe,
        )
        payload["provision"] = provision.to_dict()
        lines += _action_rows([provision])
        ok = provision.ok
    return Report({"ok": ok, "command": NAME, "dry_run": False, **payload}, lines), ok


def cmd_run(con: sqlite3.Connection, config: Config, args, ctx: Context,
            *, dns_transport=None, guest_probe=None) -> tuple[Report, bool]:
    outbox = ReadyOutbox(config.path_value("outbox", DEFAULT_OUTBOX))
    if not args.apply:
        rows = eligible(con, outbox, args.slug)
        actions = []
        for row in rows:
            actions += _provision_actions(row["slug"], config)
        return plan(NAME, ctx, actions,
                    ["the scan of tenants is the queue; a ready email means done"]), True
    results = run_pending(
        con, provider_factory=lambda cfg: ctx.provider, config=config,
        dns_client=_injected_dns(config, dns_transport),
        outbox=outbox, slug=args.slug, timeout=args.timeout, interval=args.interval,
        guest_probe=guest_probe,
    )
    if not results:
        return Report({"ok": True, "command": NAME, "dry_run": False, "results": [],
                       "count": 0}, ["claim: nothing to provision"]), True
    ready = sum(1 for result in results if result.ok and not result.pending)
    waiting = sum(1 for result in results if result.ok and result.pending)
    headline = "claim: %d provisioned" % ready
    if waiting:
        headline += ", %d waiting for the machine" % waiting
    return Report(
        {"ok": all(result.ok for result in results), "command": NAME, "dry_run": False,
         "results": [result.to_dict() for result in results], "count": len(results),
         "ready": ready, "waiting": waiting},
        [headline] + _action_rows(results),
    ), all(result.ok for result in results)


def cmd_status(con: sqlite3.Connection, config: Config, args) -> tuple[Report, bool]:
    outbox = ReadyOutbox(config.path_value("outbox", DEFAULT_OUTBOX))
    if args.slug:
        payload = tenant_status(con, args.slug, outbox=outbox)
        lines = [
            f"claim: {payload['slug']}",
            f"  tenant state: {payload['tenant_state']}",
            f"  provision:    {payload['provision']}",
            f"  paid:         {payload['paid']} ({payload['plan'] or 'no plan'})",
            f"  release:      {payload['release'] or '-'}",
            f"  machine:      {payload['server_id'] or '-'} {payload['ipv4'] or '-'}",
            f"  ready email:  {payload['ready_email'] or '-'}",
        ]
        return Report({"ok": True, "command": NAME, "dry_run": True, **payload}, lines), True
    rows = con.execute("SELECT slug FROM tenants ORDER BY id").fetchall()
    payloads = [tenant_status(con, row["slug"], outbox=outbox) for row in rows]
    lines = [f"claim: {len(payloads)} tenant(s)"]
    for item in payloads:
        lines.append(
            f"  {item['slug']:<14} {item['tenant_state']:<12} {item['provision']:<18} "
            f"{item['server_id'] or '-'}"
        )
    return Report({"ok": True, "command": NAME, "dry_run": True,
                   "tenants": payloads, "count": len(payloads)}, lines), True


def _load_config(path: str | None) -> Config:
    """Load a config; a missing default is fine for a read-only status."""
    chosen = path or os.environ.get(ENV_CONFIG_PATH)
    if chosen is None and not Path(DEFAULT_CONFIG_PATH).exists():
        return Config(Path(DEFAULT_CONFIG_PATH), {})
    return Config.load(chosen)


def _emit_error(args, out: TextIO, err: TextIO, error: OdileError) -> None:
    if getattr(args, "json", False):
        payload = {
            "ok": False,
            "command": NAME,
            "dry_run": not (getattr(args, "apply", False) and not getattr(args, "dry_run", False)),
            "error": str(error),
        }
        if isinstance(error, ClaimRefused):
            payload["reason"] = error.reason
            payload["status"] = error.status
        print(json.dumps(payload, indent=2, sort_keys=True), file=out)
        return
    print(f"odilectl {NAME}: {error}", file=err)


def main(
    argv: Sequence[str] | None = None,
    *,
    provider_factory: Callable[[Config], Provider] | None = None,
    dns_transport=None,
    guest_probe: Callable[[str, str], Any] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    try:
        config = _load_config(args.config)
        ctx = Context(
            config=config,
            dry_run=args.dry_run,
            apply=args.apply,
            json=args.json,
            stdout=out,
            stderr=err,
        )
        if provider_factory is not None:
            ctx.provider_factory = provider_factory
        # `status` is read-only: open the file without running the additive
        # schema, so an operator can inspect a live database without a write.
        path = _db_path(config)
        con = connect(path) if args.verb == "status" else open_db(path)
        try:
            if args.verb == "invite":
                report, ok = cmd_invite(con, config, args)
            elif args.verb == "accept":
                report, ok = cmd_accept(con, config, args, ctx,
                                        dns_transport=dns_transport,
                                        guest_probe=guest_probe)
            elif args.verb == "mark-paid":
                report, ok = cmd_mark_paid(con, config, args, ctx,
                                           dns_transport=dns_transport,
                                           guest_probe=guest_probe)
            elif args.verb == "run":
                report, ok = cmd_run(con, config, args, ctx,
                                     dns_transport=dns_transport,
                                     guest_probe=guest_probe)
            else:
                report, ok = cmd_status(con, config, args)
        finally:
            con.close()
    except OdileError as error:
        _emit_error(args, out, err, error)
        return error.exit_code
    except ValueError as error:
        usage = UsageError(str(error))
        _emit_error(args, out, err, usage)
        return usage.exit_code
    except sqlite3.Error as error:
        database = ConfigError(f"claim: database error: {error}")
        _emit_error(args, out, err, database)
        return database.exit_code
    except KeyboardInterrupt:
        print("odilectl claim: interrupted", file=err)
        return 130
    if args.json:
        print(json.dumps(report.payload, indent=2, sort_keys=True), file=out)
    else:
        print("\n".join(report.lines), file=out)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
