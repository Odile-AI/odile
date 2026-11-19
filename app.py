#!/usr/bin/python3
"""odile — waitlist service.

Standard library only. No JavaScript, no cookies, no access logs, no third-party
requests. Stores one row per signup: email, timestamp, confirmation token, and
an append-only event for the funnel. Schema v2 adds the claim-first spine:
tenants, claims, events and support grants.

It also serves the proof surfaces: the cached attestation, the verifier bundle,
the published signing key and the signed release the guest's `odile-update`
fetches (docs/runbooks/update.md).

    ODILE_PORT=8080 ODILE_DB=/tmp/waitlist.db ./app.py
"""
import base64
import gzip
import hashlib
import html
import io
import json
import os
import re
import secrets
import struct
import sqlite3
import sys
import tarfile
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from odilectl import claim
from status import status as status_page

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")

# The website icon set (ODL-504). One set, first-party, immutable: a browser
# that has seen a byte of it never asks again. The bytes are the committed
# files; tests/test_web_icon.py pins status, type and body.
ICON_FILES = {
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/icon-32.png": ("icon-32.png", "image/png"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}
DB_PATH = os.environ.get("ODILE_DB", "/var/lib/odile/waitlist.db")
ATTEST_PATH = os.environ.get("ODILE_ATTEST", "/var/lib/odile/attestation.json")
# The roadmap page is generated state, not a template: tools/roadmap_page.py
# writes it from docs/backlog.yaml and the board on a 15-minute timer, and
# this route serves those bytes. Unlisted by design (noindex in the page, no
# link from any other page) — not that the path is a secret.
ROADMAP_PATH = os.environ.get("ODILE_ROADMAP", "/var/lib/odile/roadmap.html")

BIND = os.environ.get("ODILE_BIND", "0.0.0.0")
PORT = int(os.environ.get("ODILE_PORT", "80"))

# Odile's published signing key: the one copy the installer and the recovery
# kit carry, served at the URL docs/CLAIMS.md §4.6 names.
SIGNING_KEY = os.path.join(ROOT, "installer", "odile-signing-key.pub")

# The signed release, served at the URLs docs/runbooks/update.md §3 defines.
# Artifact names are fixed by the release schema (an unknown name is refused),
# so the route is an exact-path map: a request cannot name a path inside the
# directory, only one of these eight. `webapp.tar.gz` exists only in a release
# that moves the Webapp bundle; `odile_install.py.sig` is what makes the
# download pass `--verify-signature` without the repository.
RELEASE_DIR = os.environ.get("ODILE_RELEASE_DIR", os.path.join(ROOT, "release"))
RELEASE_FILES = {
    "/release.json": ("release.json", "application/json"),
    "/release.json.sig": ("release.json.sig", "application/octet-stream"),
    "/manifest.json": ("manifest.json", "application/json"),
    "/manifest.json.sig": ("manifest.json.sig", "application/octet-stream"),
    "/odile_install.py": ("odile_install.py", "text/plain; charset=utf-8"),
    "/odile_install.py.sig": ("odile_install.py.sig", "application/octet-stream"),
    "/skills.tar.gz": ("skills.tar.gz", "application/gzip"),
    "/webapp.tar.gz": ("webapp.tar.gz", "application/gzip"),
}

MAX_BODY = 8 * 1024
RATE_LIMIT = int(os.environ.get("ODILE_RATE_LIMIT", "6"))   # signups per IP
RATE_WINDOW = 600       # seconds

# The retired demo front door. `/judge` is printed on the title and end cards
# of the posted demo cut (docs/ops/evidence/2026-11-07-demo/demo-final.mp4), so
# the address must keep answering after the page is gone: a permanent redirect
# to the tenant's own sign-in, never a 404. The demo is the tenant machine, not
# an HQ page; the two nav links on the landing page point at this URL directly.
DEMO_LOGIN_URL = "https://demo.odile.fyi/login"

# odile.fyi/verify is the demo tenant's page, not HQ's (ODL-562). The demo's
# own page pairs the v3.8b tenant sentence with the report and the access
# report of one machine; HQ's guest is a different machine and has no tenant
# access report. The Host gate keeps a DNS fallback (demo.odile.fyi -> HQ, with
# the demo role) from redirecting to itself.
DEMO_HOST = "demo.odile.fyi"
DEMO_VERIFY_URL = "https://demo.odile.fyi/verify"

# The verifier bundle /verify offers: the script, its one module, the pin and
# AMD's ASK and ARK. The chip-specific VCEK is fetched from AMD's KDS for the
# report's chip and TCB, so no machine's certificate is published here.
VERIFIER_BUNDLE_FILES = (
    ("odile-verify", "odile-verify/odile-verify", 0o755),
    ("tools/snp_report.py", "odile-verify/tools/snp_report.py", 0o644),
    ("odile-verify.pin", "odile-verify/odile-verify.pin", 0o644),
    ("tests/fixtures/amd/ark.pem", "odile-verify/tests/fixtures/amd/ark.pem", 0o644),
    ("tests/fixtures/amd/ask.pem", "odile-verify/tests/fixtures/amd/ask.pem", 0o644),
)
_bundle: bytes | None = None

EMAIL_RE = re.compile(
    r"^[^@\s]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

_hits: dict[str, list[float]] = {}
_lock = threading.Lock()


# --------------------------------------------------------------------------- db

# Schema v3. One constant, applied by both the service on boot and
# `manage.py migrate`, so the live database and a test database cannot drift.
# Every statement is IF NOT EXISTS: running this twice changes nothing.
#
# v3 adds `tenants.release`: the release the machine was ordered to boot
# (ODL-398). The migration is an additive `ALTER TABLE`, guarded by
# `PRAGMA table_info`, because `CREATE TABLE IF NOT EXISTS` cannot add a
# column to a table that already exists.
#
# No passwords, passphrases, private keys or IP history are stored. A token is
# kept as a hash, never as itself; the only key material is the throwaway boot
# key, whose private half was never written.
SCHEMA_VERSION = 3
SCHEMA_TABLES = ("waitlist", "tenants", "claims", "events", "grants")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS waitlist (
    id           INTEGER PRIMARY KEY,
    email        TEXT    NOT NULL UNIQUE,
    created_at   TEXT    NOT NULL,
    token        TEXT    NOT NULL UNIQUE,
    source       TEXT    NOT NULL DEFAULT 'web',
    unsubscribed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tenants (
    id               INTEGER PRIMARY KEY,
    slug             TEXT    NOT NULL UNIQUE,
    email            TEXT    NOT NULL UNIQUE,
    ssh_public_key   TEXT,
    state            TEXT    NOT NULL DEFAULT 'invited'
                     CHECK (state IN ('invited', 'claimed', 'provisioned', 'installed',
                                      'active', 'waiting_for_owner', 'churned')),
    server_id        TEXT    UNIQUE,
    ipv4             TEXT,
    plan             TEXT,
    -- the release the machine was ordered to boot, from the pin (ODL-398)
    release          TEXT,
    expires_at       TEXT,
    setup_token_hash TEXT    UNIQUE,
    claimed_at       TEXT,
    installed_at     TEXT,
    last_heartbeat   TEXT,
    created_at       TEXT    NOT NULL,
    -- a claimed machine carries a boot key; under the strict bar it is a
    -- throwaway public key with no private half
    CHECK (claimed_at IS NULL OR ssh_public_key IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS tenants_state ON tenants(state);
CREATE INDEX IF NOT EXISTS tenants_heartbeat ON tenants(last_heartbeat);

CREATE TABLE IF NOT EXISTS claims (
    id         INTEGER PRIMARY KEY,
    tenant_id  INTEGER NOT NULL REFERENCES tenants(id),
    token_hash TEXT    NOT NULL UNIQUE,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL,
    used_at    TEXT
);

CREATE INDEX IF NOT EXISTS claims_tenant ON claims(tenant_id);

-- A claim is redeemed once: the first UPDATE records used_at, a later one aborts.
CREATE TRIGGER IF NOT EXISTS claims_single_use
BEFORE UPDATE OF used_at ON claims
WHEN OLD.used_at IS NOT NULL AND NEW.used_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'claim already used');
END;

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    tenant_id   INTEGER REFERENCES tenants(id),
    waitlist_id INTEGER,
    kind        TEXT    NOT NULL
                CHECK (kind IN ('signup', 'invited', 'claimed', 'provisioned', 'installed',
                                'activated', 'waiting_for_owner', 'churned')),
    detail      TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(detail)),
    created_at  TEXT    NOT NULL,
    -- every event has a subject: a tenant, or a waitlist row before there is one.
    -- The waitlist id is not a foreign key on purpose: `manage.py rm` may remove
    -- the address while the count stays in the log, and the log is append-only.
    CHECK (tenant_id IS NOT NULL OR waitlist_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS events_kind_time ON events(kind, created_at);
CREATE INDEX IF NOT EXISTS events_tenant ON events(tenant_id);

-- The event log is evidence: rows may be added, never rewritten or removed.
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TABLE IF NOT EXISTS grants (
    id         INTEGER PRIMARY KEY,
    tenant_id  INTEGER NOT NULL REFERENCES tenants(id),
    reason     TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL,
    revoked_at TEXT,
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS grants_tenant ON grants(tenant_id, expires_at);
"""

# One signup event per waitlist row, with the signup's own timestamp. The WHERE
# clause is what makes the backfill idempotent on the next migrate.
BACKFILL_SIGNUPS = """
INSERT INTO events (waitlist_id, kind, detail, created_at)
SELECT w.id, 'signup', json_object('source', w.source), w.created_at
FROM waitlist AS w
WHERE NOT EXISTS (
    SELECT 1 FROM events AS e
    WHERE e.kind = 'signup' AND e.waitlist_id = w.id AND e.created_at = w.created_at
);
"""


def db():
    con = sqlite3.connect(DB_PATH, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _add_column(con, table: str, column: str, declaration: str) -> bool:
    """Add one additive column when it is absent; True when this run added it.

    `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so a new
    column needs an explicit `ALTER TABLE`. The guard is the table's own
    description, which makes the migration safe to run on every service start
    and twice on a copy of the live database.
    """
    columns = {row[1] for row in con.execute("PRAGMA table_info(%s)" % table)}
    if column in columns:
        return False
    con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, declaration))
    return True


def migrate(con):
    """Create or upgrade the schema in place, and report what changed.

    Idempotent by construction: safe against a copy of the live database, safe
    on every service start, and safe to run twice. Existing waitlist rows are
    left alone; their signup events are backfilled once.
    """
    existing = {
        row[0]
        for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    con.executescript(SCHEMA)
    added = []
    if _add_column(con, "tenants", "release", "TEXT"):
        added.append("tenants.release")
    backfilled = con.execute(BACKFILL_SIGNUPS).rowcount
    con.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
    con.commit()
    return {
        "version": SCHEMA_VERSION,
        "backfilled": backfilled,
        "added": added,
        "tables": {
            name: {
                "created": name not in existing,
                "rows": con.execute("SELECT count(*) FROM %s" % name).fetchone()[0],
            }
            for name in SCHEMA_TABLES
        },
    }


def init_db():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = db()
    migrate(con)
    # The claim spine is additive to the v2 schema: the setup-token ledger and
    # the tenant state machine live in the same file, created idempotently.
    claim.ensure_schema(con)
    con.close()


# --------------------------------------------------------------------- helpers

def attested():
    """Short fingerprint of this guest's SEV-SNP launch measurement."""
    try:
        with open(ATTEST_PATH, encoding="utf-8") as f:
            data = json.load(f)
        m = data.get("measurement", "")
        if len(m) == 96:
            return m[:12] + "\u2026" + m[-10:]
    except (OSError, ValueError):
        pass
    return "unavailable"


def allow(ip: str) -> bool:
    """In-memory rate limit. The IP is hashed and never touches disk."""
    key = hashlib.sha256(ip.encode()).hexdigest()[:16]
    now = time.time()
    with _lock:
        q = [t for t in _hits.get(key, []) if now - t < RATE_WINDOW]
        if len(q) >= RATE_LIMIT:
            _hits[key] = q
            return False
        q.append(now)
        _hits[key] = q
        if len(_hits) > 4000:
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > RATE_WINDOW]:
                _hits.pop(k, None)
    return True


def signing_key() -> bytes:
    """The committed bytes of installer/odile-signing-key.pub, read per request.

    The deployed tree holds the only copy, so the body served at
    /odile-signing-key.pub cannot drift from the key the installer and the
    recovery kit carry.
    """
    with open(SIGNING_KEY, "rb") as handle:
        return handle.read()


def page(name: str, **subs) -> str:
    with open(os.path.join(WEB, name), encoding="utf-8") as f:
        body = f.read()
    for key, value in subs.items():
        body = body.replace("{{%s}}" % key, value)
    return body


def err_slot(message: str, slot_id: str = "email-err") -> str:
    """The in-place error paragraph: one message, tied to the field it names."""
    return '<p class="err" id="%s" role="alert">%s</p>' % (slot_id, html.escape(message))


BLOCK_RE = re.compile(r"<!--(form|done)-->(.*?)<!--/\1-->", re.S)
# The claim page has three states, not two: a block that is not the current
# state is removed from the page, so no unsubstituted slot reaches a browser.
CLAIM_BLOCK_RE = re.compile(r"<!--(form|done|error)-->(.*?)<!--/\1-->", re.S)


def render_index(*, done: bool = False, email: str = "", error: str = "",
                 title: str = "Odile", robots: str = "index") -> str:
    """One page, two states: the form (with an optional error beside the field),
    or the confirmation. The unused block is removed on the server, so a person
    who has just signed up never sees an email field or a Join button again."""
    body = page(
        "index.html",
        TITLE=html.escape(title),
        ROBOTS=robots,
        INVALID=' aria-invalid="true" aria-describedby="email-err"' if error else "",
        AUTOFOCUS=" autofocus" if error else "",
    )
    keep = "done" if done else "form"
    body = BLOCK_RE.sub(lambda m: m.group(2) if m.group(1) == keep else "", body)
    body = body.replace("<!--state-->", err_slot(error) if error else "")
    # The address goes in last: a literal {{...}} typed into the field must never
    # be read back as a slot of ours.
    return body.replace("{{EMAIL}}", html.escape(email, quote=True))


def valid_email(raw: str) -> str | None:
    email = raw.strip().strip("<>").lower()
    if len(email) < 6 or len(email) > 254:
        return None
    return email if EMAIL_RE.match(email) else None


# ------------------------------------------------------------------ the claim
#
# The strict claim takes an email and a consent tick, nothing else: no public
# key, no passphrase, no command. The boot key is the one piece of plumbing the
# provider still wants at create time, so the service stores a throwaway public
# key whose private half was never written. The disk opens on the measured
# image, not on this key (docs/product/CLAIMS-RECUT-STRICT.md A2/A4).

CLAIM_TITLES = {
    "form": "Claim — Odile",
    "done": "Your machine is claimed — Odile",
    "error": "This claim link is closed — Odile",
}


def throwaway_boot_key() -> str:
    """A one-line ed25519 public key with no private half, ever written.

    The SSH wire format is two strings: the key type and 32 bytes of key
    material. The bytes are random and the matching private key is discarded by
    construction, so the tenant has zero usable user keys. Servury still takes
    an ``ssh_key`` at create time; this is that value and nothing else.
    """
    blob = struct.pack(">I", len(b"ssh-ed25519")) + b"ssh-ed25519"
    blob += struct.pack(">I", 32) + secrets.token_bytes(32)
    return "ssh-ed25519 %s odile-throwaway" % base64.b64encode(blob).decode("ascii")


# The claim form has two fields, and each owns one error slot: the message
# renders inside that field's block and only that input carries the ARIA pair.
# The closed page has no field, so its message is page-level (`claim-err`).
CLAIM_FIELDS = ("email", "consent")


def render_claim(*, state: str, email: str = "", token: str = "",
                 error: str = "", field: str = "") -> str:
    """One template, three states: the form, the claimed page and a closed link.

    A form error belongs to a field: the consent message renders inside the
    checkbox row with the ARIA pair on ``#consent``, and every other form
    error sits under the email field. Slots are replaced in two passes: the
    fixed values first, the invitation's own address last, so a literal
    ``{{...}}`` in an address cannot be read back as one of our slots.
    """
    body = page("claim.html", TITLE=html.escape(CLAIM_TITLES[state]), ROBOTS="noindex")
    body = CLAIM_BLOCK_RE.sub(lambda m: m.group(2) if m.group(1) == state else "", body)
    body = body.replace("{{TOKEN}}", html.escape(token, quote=True))
    owner = field if field in CLAIM_FIELDS else "email"
    for name in CLAIM_FIELDS:
        hit = bool(error) and state == "form" and owner == name
        slot_id = "%s-err" % name
        body = body.replace(
            "{{%s_INVALID}}" % name.upper(),
            ' aria-invalid="true" aria-describedby="%s"' % slot_id if hit else "",
        )
        body = body.replace(
            "{{%s_ERROR}}" % name.upper(),
            err_slot(error, slot_id) if hit else "",
        )
    body = body.replace(
        "{{CLAIM_ERROR}}",
        err_slot(error, "claim-err") if error and state == "error" else "",
    )
    return body.replace("{{EMAIL}}", html.escape(email))


def render_ceremony() -> str:
    """The first-visit explainer the ready email links to.

    It adds no claim of its own: every sentence is a row in the decided v3.9
    text until ODL-462 lands it in docs/CLAIMS.md.
    """
    return page("ceremony.html", TITLE="First visit — Odile", ROBOTS="noindex")


# The cached report is written by root from /dev/sev-guest, but /verify is a
# public page: only values with the exact shape attest.py writes are rendered,
# so a corrupted or hostile cache becomes "unavailable", not markup.
HEX96_RE = re.compile(r"^[0-9a-f]{96}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# The report `verify_and_unlock.sh --mode access --out` writes on the machine
# (ODL-473). The boot oneshot writes it as root 0644; /verify reads it. It is
# not templated: the page shows the five lines the script printed, escaped.
ACCESS_REPORT_PATH = os.environ.get("ODILE_ACCESS_REPORT",
                                    "/var/lib/odile/access-report.txt")
ACCESS_REPORT_MAX = 64 * 1024
ACCESS_TAGS = ("usable keys", "boot key", "seal", "sign-in hash", "operator key")
ACCESS_HEAD = "read from your machine, "
ACCESS_TITLE = "verify_and_unlock \u2014 access report"
# Blocks: one chrome state and one access state survive a render; the rest are
# removed, so a slot in a dropped block can never reach a browser.
VERIFY_BLOCK_RE = re.compile(
    r"<!--(demo|tenant|bench|machine|noaccess)-->(.*?)<!--/\1-->", re.S)


def access_report() -> dict:
    """The machine's own access report, in the shape the script writes.

    Returns ``{"state": "ok", "at": <ISO>, "text": <the report>}``,
    ``{"state": "missing"}`` when no file exists, or ``{"state": "bad"}`` when
    the file is present but is not what ``--mode access --out`` writes. A
    report file that exists and does not parse is never silently dropped: the
    page says so instead of showing the bench block as if it were this
    machine's state.
    """
    try:
        with open(ACCESS_REPORT_PATH, encoding="utf-8", errors="replace") as handle:
            data = handle.read(ACCESS_REPORT_MAX + 1)
    except FileNotFoundError:
        return {"state": "missing"}
    except OSError:
        return {"state": "bad"}
    if len(data) > ACCESS_REPORT_MAX:
        return {"state": "bad"}
    lines = data.splitlines()
    if len(lines) != 10 or not lines[0].startswith(ACCESS_HEAD):
        return {"state": "bad"}
    at = lines[0][len(ACCESS_HEAD):]
    if not ISO_RE.match(at):
        return {"state": "bad"}
    if lines[1] != ACCESS_TITLE or lines[2] != "" or lines[8] != "":
        return {"state": "bad"}
    for index, tag in enumerate(ACCESS_TAGS):
        row = lines[3 + index]
        if not re.match(r"^  %s +(PASS|FAIL) +" % re.escape(tag), row):
            return {"state": "bad"}
    if not re.match(r"^  (PASS|FAIL) \u2014 ", lines[9]):
        return {"state": "bad"}
    return {"state": "ok", "at": at, "text": "\n".join(lines[1:])}


def app_role(role: str = "") -> str:
    """The role this app renders: ``demo`` on HQ, ``tenant`` on a tenant.

    One normalisation for the page render and the `/verify` Host gate, so the
    two cannot disagree about what the environment said.
    """
    name = (role or os.environ.get("ODILE_ROLE") or "demo").strip().lower()
    return name if name in ("demo", "tenant") else "demo"


def render_verify(role: str = "") -> str:
    """The proof page: the report, the verifier command, the three gaps.

    The sentence set is docs/CLAIMS.md §3 and the attestation rows; the page
    adds no claim of its own. `fetched_at` is shown so a reader knows how old
    the report is, and the raw JSON stays one click away at /attestation.

    ``role`` is ``demo`` on HQ and ``tenant`` on a tenant machine. On a tenant
    the access section is the machine's own report file (ODL-473), tagged
    `read from your machine`; the 10 November bench block is only the demo's
    fallback when no report file exists. Every value goes in escaped: the file
    is root-written but the page is public.

    There is no demo note. The public demo moved to the tenant (ODL-503), so
    HQ's copy of the old demo-reset note published a reliability claim the deck
    had retired; the demo's own page is the tenant's (t_9c2d31f1).
    """
    role = app_role(role)
    report = cached_report()
    if report is None:
        values = {
            "REPORT_STATE": "unavailable",
            "REPORT_LINE": "Report unavailable.",
            "REPORT_CAUSE": '<p class="note">The CPU has not produced a report yet. <a href="/verify">Try again</a> in a minute.</p>',
            "FETCHED_AT": "unavailable",
            "MEASUREMENT": "unavailable",
            "REPORT_META": "unavailable",
        }
    else:
        measurement, fetched_at, meta = report
        values = {
            "REPORT_STATE": "available",
            "REPORT_LINE": "Report available.",
            "REPORT_CAUSE": "",
            "FETCHED_AT": html.escape(fetched_at),
            "MEASUREMENT": html.escape(measurement),
            "REPORT_META": html.escape(meta),
        }
    access = access_report()
    if access["state"] == "ok":
        keep = {"machine"}
        values["REPORT_AT"] = html.escape(access["at"])
        values["REPORT_TEXT"] = html.escape(access["text"])
        values["ACCESS_CAUSE"] = ""
    elif access["state"] == "bad":
        keep = {"noaccess"}
        values["REPORT_AT"] = ""
        values["REPORT_TEXT"] = ""
        values["ACCESS_CAUSE"] = ("The report file on this machine is not in the "
                                  "shape the reporter writes. Run it again.")
    elif role == "demo":
        keep = {"bench"}
        values["REPORT_AT"] = ""
        values["REPORT_TEXT"] = ""
        values["ACCESS_CAUSE"] = ""
    else:
        keep = {"noaccess"}
        values["REPORT_AT"] = ""
        values["REPORT_TEXT"] = ""
        values["ACCESS_CAUSE"] = ("The machine has not written its access report "
                                  "yet. <a href=\"/verify\">Try again</a> in a minute.")
    keep.add(role)
    body = page("verify-public.html", TITLE="Verify \u2014 Odile", ROBOTS="noindex")
    body = VERIFY_BLOCK_RE.sub(
        lambda match: match.group(2) if match.group(1) in keep else "", body)
    for name, value in values.items():
        body = body.replace("{{%s}}" % name, value)
    return body


def cached_report() -> tuple[str, str, str] | None:
    """(measurement, fetched_at, 'version N, M bytes') or None.

    Reads the same JSON /attestation serves; no device call on a page view.
    """
    try:
        with open(ATTEST_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("available") is not True:
        return None
    measurement = data.get("measurement")
    fetched_at = data.get("fetched_at")
    version = data.get("report_version")
    size = data.get("report_size")
    if not (isinstance(measurement, str) and HEX96_RE.match(measurement)):
        return None
    if not (isinstance(fetched_at, str) and ISO_RE.match(fetched_at)):
        return None
    if not isinstance(version, int) or isinstance(version, bool):
        return None
    if not isinstance(size, int) or isinstance(size, bool):
        return None
    return measurement, fetched_at, "version %d, %d bytes" % (version, size)


def render_faq() -> str:
    """The FAQ: the access model and the three gaps, in the deck's sentences.

    The page adds no claim of its own: every sentence is a row in
    docs/CLAIMS.md, and tests/test_claims.py checks that mechanically.
    """
    return page("faq.html", TITLE="Questions \u2014 Odile", ROBOTS="index")


def render_legal(name: str, title: str) -> str:
    """The paperwork: privacy and terms, in the same voice.

    Static pages with no JavaScript. The privacy page's inventory is bound to
    the live schema by tests/test_legal.py; its prose is bound to CLAIMS.md by
    tests/test_claims.py. Terms makes no privacy or security claim that is not
    also a deck row. `/abuse` is retired to a 301 at `/terms` (ODL-903): every
    sentence it carried lives on one of these two pages.
    """
    return page(name, TITLE="%s \u2014 Odile" % title, ROBOTS="index")


def verifier_bundle() -> bytes:
    """The verifier and everything it needs, as one deterministic tar.gz.

    A judge can run the proof from the page without a repository checkout. The
    archive is built from the reviewed files in this tree; no request can name
    a path outside VERIFIER_BUNDLE_FILES.

    The gzip header carries ``mtime=0`` and every tar member carries mtime 0,
    so the bytes — and the sha256 the page names — depend on the reviewed files
    alone, not on the second the service happened to build them. Tarfile's
    default gzip writer stamps the current time, which made the published hash
    move on every restart with identical inputs.
    """
    global _bundle
    if _bundle is not None:
        return _bundle
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
            for source, arcname, mode in VERIFIER_BUNDLE_FILES:
                with open(os.path.join(ROOT, source), "rb") as handle:
                    data = handle.read()
                info = tarfile.TarInfo(arcname)
                info.size = len(data)
                info.mode = mode
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                tar.addfile(info, io.BytesIO(data))
    _bundle = buf.getvalue()
    return _bundle


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    server_version = "odile"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # no access log, by design
    def log_message(self, fmt, *args):
        pass

    def send_response(self, code, message=None):
        """Omit the Server header: the reverse proxy sets one, and a second
        copy of it is just noise."""
        self.log_request(code)
        self.send_response_only(code, message)
        self.send_header("Date", self.date_time_string())

    def _send(self, code, body, ctype="text/html; charset=utf-8", head=False, cache="no-store",
              location=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        if location is not None:
            self.send_header("Location", location)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), interest-cohort=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
            "font-src data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        if not head:
            self.wfile.write(data)

    # -- routing ----------------------------------------------------------

    def do_HEAD(self):
        self._dispatch(head=True)

    def do_GET(self):
        self._dispatch(head=False)

    def _dispatch(self, head):
        url = urlparse(self.path)
        path = url.path
        qs = parse_qs(url.query)

        if path in ("/", "/index.html"):
            token = (qs.get("t") or [""])[0]
            if token:
                con = db()
                row = con.execute("SELECT email FROM waitlist WHERE token = ?", (token,)).fetchone()
                con.close()
                if row:
                    return self._send(200, render_index(
                        done=True, email=row["email"],
                        title="You're on the list \u2014 Odile", robots="noindex"), head=head)
            return self._send(200, render_index(), head=head)
        if path == "/claim":
            # The one-time token in the invite is what opens the page. A bare
            # GET keeps the method note ODL-220 pinned for an API caller.
            token = (qs.get("t") or [""])[0].strip()
            if not token:
                return self._send(405, "method not allowed: POST a claim\n",
                                  "text/plain; charset=utf-8", head=head)
            return self._claim_page(token, head)
        if path == "/ceremony":
            return self._send(200, render_ceremony(), head=head)
        if path == "/first-visit-preview":
            # A read-only snapshot of the reviewed first-visit screen (ODL-500).
            # It is a public page like the others: no form, no script, and the
            # same paper/ink tokens and embedded Cormorant face.
            return self._send(200, page(
                "first-visit-preview.html",
                TITLE="First visit — Odile",
                BRAND='<a class="brand" href="/">Odile</a>',
            ), head=head)
        if path == "/attestation":
            return self._send(200, self._attestation(), "application/json", head=head)
        if path == "/verify":
            # GET and HEAD alike: the HQ app is not the machine the demo's
            # sentence describes (ODL-562). Only the demo host renders the
            # page, so a DNS fallback cannot loop; a tenant always renders.
            role = app_role()
            host = (self.headers.get("Host") or "").split(":", 1)[0].strip().lower()
            if role == "demo" and host != DEMO_HOST:
                return self._send(302, "", head=head, location=DEMO_VERIFY_URL)
            return self._send(200, render_verify(role), head=head)
        if path == "/faq":
            return self._send(200, render_faq(), head=head)
        if path == "/privacy":
            return self._send(200, render_legal("privacy.html", "Privacy"), head=head)
        if path == "/terms":
            return self._send(200, render_legal("terms.html", "Terms"), head=head)
        if path == "/abuse":
            # The page is retired, the address is not: a conventional abuse
            # report belongs in the same spool as every other message, and
            # every sentence the page carried lives on privacy or terms.
            # 301, not the signup flow's 303, because the move is permanent —
            # and no query string is carried, or the redirect would become a
            # second front door into this service.
            return self._permanent_redirect("/terms")
        if path == "/judge":
            # The page is retired, the address is not: both cards of the posted
            # demo cut print it. A judge who types it lands on the tenant's own
            # sign-in page. 301, not the signup flow's 303, because the move is
            # permanent — and no query string is carried, or the redirect would
            # become a second front door into this service.
            return self._permanent_redirect(DEMO_LOGIN_URL)
        if path == "/roadmap":
            # State, not a page: read on every request so a regeneration lands
            # without a restart, and let neither the browser nor a proxy keep a
            # copy. A missing file is one line of 503, not a traceback.
            try:
                with open(ROADMAP_PATH, "rb") as handle:
                    state = handle.read()
            except OSError:
                return self._send(503, "roadmap not generated yet\n",
                                  "text/plain; charset=utf-8", head=head)
            return self._send(200, state, "text/html; charset=utf-8", head=head,
                              cache="no-store")
        if path == "/status":
            # The same renderer as `status/status.py --stdout`, computed from
            # the heartbeat history on every request: a regeneration lands
            # without a restart and no visitor reads a stale figure. A database
            # without the heartbeat history is one line of 503, not a
            # traceback; the page names no tenant by construction.
            try:
                body, _ = status_page.build(DB_PATH)
            except status_page.StatusError:
                return self._send(503, "the status page has no history yet\n",
                                  "text/plain; charset=utf-8", head=head,
                                  cache="no-store")
            return self._send(200, body, "text/html; charset=utf-8", head=head,
                              cache="no-store")
        if path == "/odile-verify.tar.gz":
            return self._send(200, verifier_bundle(), "application/gzip", head=head,
                              cache="public, max-age=300")
        if path == "/odile-signing-key.pub":
            # The public key, not a page: no JavaScript, no request, one copy.
            try:
                key = signing_key()
            except OSError:
                return self._send(500, "signing key unavailable\n",
                                  "text/plain; charset=utf-8", head=head)
            return self._send(200, key, "application/x-pem-file", head=head,
                              cache="public, max-age=300")
        if path in RELEASE_FILES:
            # A release is never served stale: read the file on every request,
            # and let neither the browser nor the guest keep a copy.
            name, ctype = RELEASE_FILES[path]
            try:
                with open(os.path.join(RELEASE_DIR, name), "rb") as handle:
                    data = handle.read()
            except OSError:
                return self._send(404, "not found\n", "text/plain; charset=utf-8",
                                  head=head)
            return self._send(200, data, ctype, head=head, cache="no-store")
        if path == "/health":
            return self._send(200, "ok\n", "text/plain; charset=utf-8", head=head)
        if path == "/robots.txt":
            return self._send(200, "User-agent: *\nAllow: /\n", "text/plain; charset=utf-8", head=head)
        if path in ICON_FILES:
            name, ctype = ICON_FILES[path]
            try:
                with open(os.path.join(WEB, name), "rb") as handle:
                    icon = handle.read()
            except OSError:
                return self._send(404, "not found\n", "text/plain; charset=utf-8", head=head)
            return self._send(200, icon, ctype, head=head,
                              cache="public, max-age=31536000, immutable")
        if path.startswith("/bg-") and path.endswith(".webp"):
            try:
                with open(os.path.join(WEB, os.path.basename(path)), "rb") as f:
                    art = f.read()
            except OSError:
                return self._send(404, "not found\n", "text/plain; charset=utf-8", head=head)
            return self._send(200, art, "image/webp", head=head,
                              cache="public, max-age=31536000, immutable")
        return self._send(404, "not found\n", "text/plain; charset=utf-8", head=head)

    # -- pages ------------------------------------------------------------

    def _attestation(self):
        try:
            with open(ATTEST_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {"available": False, "guest": "sev-snp"}
        return json.dumps(data, indent=2) + "\n"

    # -- signup -----------------------------------------------------------

    def _client_ip(self) -> str:
        """Caddy terminates TLS on loopback and sets X-Forwarded-For."""
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1"):
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return ip

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/waitlist", "/claim"):
            return self._send(405, "method not allowed\n", "text/plain; charset=utf-8")

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            if path == "/claim":
                return self._send(413, "That request was too large.\n",
                                  "text/plain; charset=utf-8")
            return self._send(413, render_index(error="That request was too large."))

        raw = self.rfile.read(length).decode("utf-8", "replace")
        fields = parse_qs(raw, keep_blank_values=True)
        if path == "/claim":
            return self._claim(fields)
        email_raw = (fields.get("email") or [""])[0]

        # honeypot: filled only by bots. Pretend everything went fine.
        if (fields.get("hp") or [""])[0].strip():
            return self._redirect("/")

        if not allow(self._client_ip()):
            return self._send(429, render_index(
                error="Too many signups from here. Try again in ten minutes.",
                email=email_raw))

        email = valid_email(email_raw)
        if not email:
            return self._send(422, render_index(
                error="That doesn't look like an email address.", email=email_raw))

        con = db()
        token = secrets.token_hex(12)
        stamp = now_iso()
        try:
            cur = con.execute(
                "INSERT INTO waitlist (email, created_at, token) VALUES (?, ?, ?)",
                (email, stamp, token),
            )
        except sqlite3.IntegrityError:
            row = con.execute(
                "SELECT token, unsubscribed FROM waitlist WHERE email = ?", (email,)
            ).fetchone()
            if row is None:
                con.close()
                return self._send(500, render_index(
                    error="Something broke. Try again?", email=email_raw))
            token = row["token"]
            if row["unsubscribed"]:
                con.execute("UPDATE waitlist SET unsubscribed = 0 WHERE email = ?", (email,))
                con.commit()
            con.close()
            return self._redirect("/?t=%s" % quote(token))
        # A new signup is the first event in the funnel; a duplicate is not.
        # The event points at the waitlist row, so `rm` leaves no address behind.
        con.execute(
            "INSERT INTO events (waitlist_id, kind, detail, created_at)"
            " VALUES (?, 'signup', ?, ?)",
            (cur.lastrowid, json.dumps({"source": "web"}), stamp),
        )
        con.commit()
        con.close()
        return self._redirect("/?t=%s" % quote(token))

    # -- claim ------------------------------------------------------------

    def _claim_row(self, token):
        """The claim and its tenant for one token, or None. Read-only."""
        con = db()
        try:
            return con.execute(
                "SELECT c.expires_at AS expires_at, c.used_at AS used_at,"
                " t.email AS email, t.state AS state, t.claimed_at AS claimed_at"
                " FROM claims AS c JOIN tenants AS t ON t.id = c.tenant_id"
                " WHERE c.token_hash = ?",
                (claim.hash_token(token),),
            ).fetchone()
        finally:
            con.close()

    def _claim_page(self, token, head):
        """GET /claim?t=…: the form, the claimed page, or a closed-link page.

        A GET never spends the token.
        """
        row = self._claim_row(token)
        if row is None:
            return self._send(404, render_claim(
                state="error", error="That claim link is not valid."), head=head)
        if row["used_at"] is not None:
            # A replay of the token that claimed this tenant reopens the
            # claimed page; a token revoked by a re-invite stays closed.
            if row["claimed_at"] == row["used_at"]:
                return self._send(200, render_claim(
                    state="done", email=row["email"]), head=head)
            return self._send(409, render_claim(
                state="error", error="That claim link has already been used."), head=head)
        if row["expires_at"] <= now_iso():
            return self._send(410, render_claim(
                state="error", error="That claim link has expired."), head=head)
        if row["state"] in claim.SETTLED_STATES:
            return self._send(409, render_claim(
                state="error", error="That claim link has already been used."), head=head)
        return self._send(200, render_claim(
            state="form", email=row["email"], token=token), head=head)

    def _claim(self, fields):
        """Record a claim: the token, the invited email and one consent tick.

        Two callers share the route. The no-JS page posts ``step=claim`` with
        the email and the consent checkbox, and the service stores a throwaway
        boot key with no private half. A bare token+key POST is the API contract
        ODL-220 pinned and keeps its plain-text answers.

        The paid work happens in the root worker (`python3 -m odilectl.claim
        run --apply`), because the provider and DNS credentials are root-only.
        A claim accepted here is therefore a success before the machine exists:
        the user is told the email will come.
        """
        token = (fields.get("token") or [""])[0].strip()
        key = (fields.get("key") or [""])[0]
        if key.strip():
            return self._claim_api(token, key)
        return self._claim_form(fields, token)

    def _claim_form(self, fields, token):
        """The no-JS page step: consent and the invited address, nothing else."""
        email_raw = (fields.get("email") or [""])[0].strip()
        consent = (fields.get("consent") or [""])[0].strip().lower()

        row = self._claim_row(token)
        if row is None:
            return self._send(404, render_claim(
                state="error", error="That claim link is not valid."))
        if row["used_at"] is not None:
            if row["claimed_at"] == row["used_at"]:
                return self._send(200, render_claim(
                    state="done", email=row["email"]))
            return self._send(409, render_claim(
                state="error", error="That claim link has already been used."))
        if row["expires_at"] <= now_iso():
            return self._send(410, render_claim(
                state="error", error="That claim link has expired."))
        if row["state"] in claim.SETTLED_STATES:
            return self._send(409, render_claim(
                state="error", error="That claim link has already been used."))

        if email_raw:
            email = valid_email(email_raw)
            if not email or email != row["email"]:
                return self._send(422, render_claim(
                    state="form", email=row["email"], token=token, field="email",
                    error="That email does not match the invitation."))
        if consent not in ("yes", "on", "1", "true"):
            return self._send(422, render_claim(
                state="form", email=row["email"], token=token, field="consent",
                error='Tick "I agree to the terms and the privacy policy." '
                      "before you claim."))

        con = db()
        try:
            result = claim.accept(con, token, throwaway_boot_key())
        except claim.ClaimRefused as error:
            if error.status == 422:
                return self._send(422, render_claim(
                    state="form", email=row["email"], token=token,
                    error=error.message))
            if error.status == 409:
                # A double-submit can lose the conditional UPDATE after the
                # row was read; the winner's claim is this token's claim, so
                # answer with the claimed page, not a refusal.
                again = self._claim_row(token)
                if (again is not None and again["used_at"] is not None
                        and again["claimed_at"] == again["used_at"]):
                    return self._send(200, render_claim(
                        state="done", email=again["email"]))
            return self._send(error.status, render_claim(
                state="error", error=error.message))
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return self._send(500, render_claim(
                state="error", error="Something broke. Try again?"))
        finally:
            con.close()
        return self._send(200, render_claim(state="done", email=result.email))

    def _claim_api(self, token, key):
        """The ODL-220 API contract: token and public key, plain-text answers."""
        con = db()
        try:
            claim.accept(con, token, key)
        except claim.ClaimRefused as error:
            return self._send(error.status, error.message + "\n",
                              "text/plain; charset=utf-8")
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return self._send(500, "Something broke. Try again?\n",
                              "text/plain; charset=utf-8")
        finally:
            con.close()
        return self._send(
            200,
            "Your machine is on its way.\n"
            "We will email you when the machine is ready.\n",
            "text/plain; charset=utf-8",
        )

    def _permanent_redirect(self, location):
        """A retired address answers 301: the page now lives at `location`.

        Its own method rather than a status argument on `_redirect`: the signup
        flow's 303 is a temporary, uncached bounce back into this service, while
        a 301 is the one answer a printed address needs. Five minutes of cache
        is short enough to move the target again, long enough that a judge's
        browser does not ask twice.
        """
        self.send_response(301)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()

    def _redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


def main():
    init_db()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    server.daemon_threads = True
    print("odile on %s:%d · db %s · measurement %s" % (BIND, PORT, DB_PATH, attested()), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
