"""The measured boot record — what the strict first boot reports, and where it lands.

ODL-398 records the release a machine was *ordered* to boot. This module records
what it *did* boot, once, from the machine's own measured line:

* the public key in `bb.key=` (the throwaway boot key with no private half),
  written to `tenants.ssh_public_key` and `tenant_state.detail.boot_key`;
* the release the machine reports it booted (the boot report's `release`; the
  image pin's for an image that predates the report), written to
  `tenants.release` and `tenant_state.release`; a later record that names a
  different release (a recycle or a re-install) moves both rows, because the row
  must name the release the machine installed (ODL-947). The order's release is
  the pre-boot value, and after a boot record it is never rewritten —
  `StateStore.record_release` refuses to walk it back;
* the state the machine is in — `first_boot` advances to `first_visit` while the
  first-visit page serves, and to `ready` once the sign-in is set — and the
  current SSH host key in `detail.host_key`; the old `initramfs_host_key` is
  dropped because the strict image has no initramfs SSH wait (ODL-467).

The machine sends this record after its first boot's install (the seed service
carries it, see `seed/seed.py`); the same writer backs `odilectl boot-record`,
the operator's repair path for a machine that booted before the reporting code
was on its disk.

The record is idempotent by construction: the same facts write the same row,
write no event when the state does not move, and touch `updated_at` only when a
value actually changes. A tenant that never first-boots sends nothing and keeps
its old row — the failure is closed, not guessed.

`detail.recorded_at` is the **first** record's stamp — the moment this row's
boot record was written, like `tenants.installed_at` — and a later observation
only fills it when it is absent. A replay is therefore a no-op in the row as
well as in the events: `recorded_at` is the only key that would otherwise be a
function of *when* the writer ran rather than of the facts it carries, and one
such key is enough to break "the same facts write the same row". It is not a
last-seen stamp: liveness already has exactly one home,
`tenant_state.last_heartbeat`, refreshed by the guest every five minutes; a
second, coarser stamp under a different name and type would only disagree with
it, and an operator reading the wrong one would call a live machine stale. If a
last-seen time is ever needed, it belongs beside the heartbeat under a last-seen
name, not in this key. `detail.record_source` is the one key that moves when no
machine fact does: it names *who* wrote the record the row holds (the guest's
`first_boot` report or an operator's `boot-record` repair), so a different
writer is a different fact, while a replay by the same writer is still a no-op.

    from odilectl.bootrecord import parse_record, record_boot
    record_boot(connection, slug="odl-7f3a2c", record=parsed)
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from argparse import ArgumentParser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import Context, Report, plan, result
from .errors import OdileError, UsageError
from .state import (
    DEFAULT_STATE_DB,
    RELEASE_ID_RE,
    StateStore,
    UnknownTenant,
    _detail,
)

NAME = "boot-record"
HELP = "Record the measured boot facts a machine's first boot reported."
EXAMPLE = (
    "odilectl boot-record odl-7f3a2c --pin deploy/image/tenant-image.pin "
    "--first-visit done --apply"
)

# The key types `validate_ssh_public_key` accepts; the measured key is decoded
# here so the record names the same line the machine put in its command line.
KEY_TYPES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)
FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
STAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
MAX_CMDLINE = 4096
FIRST_VISIT_VALUES = ("pending", "done")
RECORD_KEYS = ("cmdline", "release", "first_visit", "host_key", "serving", "at")


class BootRecordError(OdileError):
    """The record is not the measured line's key/release, or the row refuses it."""

    exit_code = 1


# ----------------------------------------------------------------- the measured line

def parse_cmdline(text: str) -> dict[str, list[str]]:
    """Every `key=value` token by key; duplicates are kept, not collapsed.

    The measured line must carry exactly one `bb.key=` and exactly one
    `bb.iid=`, so the caller needs the count, not the last value.
    """
    tokens: dict[str, list[str]] = {}
    for word in text.split():
        if "=" not in word:
            continue
        key, value = word.split("=", 1)
        if key:
            tokens.setdefault(key, []).append(value)
    return tokens


def _one(tokens: dict[str, list[str]], name: str) -> str:
    values = tokens.get(name, [])
    if len(values) != 1:
        raise BootRecordError(
            "the measured command line carries %d %s= token(s); exactly one is required"
            % (len(values), name)
        )
    return values[0]


def fingerprint(blob: bytes) -> str:
    """OpenSSH's SHA256 fingerprint: base64 of the digest, no `=` padding."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def public_key_line(line: str) -> tuple[str, str]:
    """Return `(the one normalised key line, its fingerprint)` or raise.

    The measured `bb.key=` is base64 of an OpenSSH public key line. One line,
    one key type, one base64 body, and key material that is not empty — the
    same shape the provider's create call and the audit already require.
    """
    text = line.strip()
    if not text or len(text.splitlines()) != 1:
        raise BootRecordError("the boot key is not exactly one line")
    fields = text.split()
    if len(fields) < 2:
        raise BootRecordError("the boot key does not look like 'ssh-ed25519 AAAA…'")
    key_type, encoded = fields[0], fields[1]
    if key_type not in KEY_TYPES:
        raise BootRecordError("unsupported boot key type %r" % (key_type,))
    try:
        blob = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise BootRecordError("the boot key body is not valid base64") from None
    if len(blob) < 4:
        raise BootRecordError("the boot key is truncated")
    declared = int.from_bytes(blob[:4], "big")
    if declared <= 0 or len(blob) < 4 + declared:
        raise BootRecordError("the boot key has no key type field")
    inner = blob[4:4 + declared].decode("ascii", "replace")
    if inner != key_type:
        raise BootRecordError("the boot key blob does not match its declared type")
    if len(blob) <= 4 + declared:
        raise BootRecordError("the boot key has no key material")
    return text, fingerprint(blob)


def measured_cmdline(text: str) -> dict[str, str]:
    """The facts a measured command line carries, or a refusal.

    `bb.key=` is decoded to the public key line; `bb.iid=` is the machine the
    line boots; `bb.host=` (when the measured line carries it) is the tenant
    slug. The returned `cmdline_sha256` is over the exact string, so a reader
    can compare it with `/proc/cmdline` on the machine or in the boot report.
    """
    line = (text or "").strip()
    if not line or len(line) > MAX_CMDLINE or "\n" in line:
        raise BootRecordError("the measured command line is empty, multi-line or too long")
    tokens = parse_cmdline(line)
    encoded = _one(tokens, "bb.key")
    instance = _one(tokens, "bb.iid").strip()
    if not instance:
        raise BootRecordError("the measured command line has an empty bb.iid")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise BootRecordError("bb.key= is not base64 of a public key line") from None
    key_line, key_fingerprint = public_key_line(decoded)
    hostname = tokens.get("bb.host", [])
    return {
        "cmdline": line,
        "cmdline_sha256": hashlib.sha256(line.encode("utf-8")).hexdigest(),
        "instance_id": instance,
        "hostname": hostname[0].strip() if len(hostname) == 1 else "",
        "boot_key": key_line,
        "boot_key_fingerprint": key_fingerprint,
    }


def parse_record(record: Any, *, instance_id: str | None = None,
                 slug: str | None = None) -> dict[str, str]:
    """Validate one record from the first boot; return the facts to write.

    The boot key is never taken from a separate field: it is decoded from the
    measured command line, and `instance_id`/`slug` (when supplied by the
    caller) must match that line. A record that names another machine than the
    one its token or row belongs to is refused.
    """
    if not isinstance(record, dict):
        raise BootRecordError("the boot record is not a JSON object")
    unknown = sorted(set(record) - set(RECORD_KEYS))
    if unknown:
        raise BootRecordError("the boot record carries unknown field(s): %s" % ", ".join(unknown))
    measured = measured_cmdline(record.get("cmdline", ""))
    if instance_id is not None and measured["instance_id"] != instance_id:
        raise BootRecordError(
            "the measured line boots instance %r, not %r"
            % (measured["instance_id"], instance_id)
        )
    if slug is not None and measured["hostname"] and measured["hostname"] != slug:
        raise BootRecordError(
            "the measured line is for %r, not %r" % (measured["hostname"], slug)
        )
    release = record.get("release")
    if not isinstance(release, str) or not RELEASE_ID_RE.match(release.strip()):
        raise BootRecordError(
            "the boot record's release must look like 2026.11.1 or 2026.11.23-5")
    first_visit = record.get("first_visit")
    if first_visit not in FIRST_VISIT_VALUES:
        raise BootRecordError("the boot record's first_visit must be 'pending' or 'done'")
    host_key = record.get("host_key")
    if host_key is not None and (
        not isinstance(host_key, str) or not FINGERPRINT_RE.match(host_key)
    ):
        raise BootRecordError("the boot record's host_key must be an SSH SHA256 fingerprint")
    serving = record.get("serving")
    if serving is not None and (not isinstance(serving, str) or len(serving) > 64):
        raise BootRecordError("the boot record's serving must be a short string")
    at = record.get("at")
    if at is not None and (not isinstance(at, str) or not STAMP_RE.match(at)):
        raise BootRecordError("the boot record's at must be a UTC timestamp")
    return {
        **measured,
        "release": release.strip(),
        "first_visit": first_visit,
        "host_key": host_key or "",
        "serving": serving or "",
        "at": at or "",
    }


# ------------------------------------------------------------------- the writer

def _tenant_row(con: sqlite3.Connection, slug: str) -> sqlite3.Row:
    row = con.execute(
        "SELECT id, slug, state, server_id, ssh_public_key, release, installed_at"
        " FROM tenants WHERE slug = ?",
        (slug,),
    ).fetchone()
    if row is None:
        raise BootRecordError("there is no tenants row for %r" % (slug,))
    return row


def _web_state(current: str, target: str) -> str:
    """The web row's word for the state spine's move, forward only.

    `tenants.state` predates the strict flow (`invited → claimed → provisioned
    → installed → active`). The spine's `first_visit` is the web row's
    `installed` and `ready` is its `active`; a row already past it keeps its
    word so a late record cannot move it backwards.
    """
    if target == "active":
        return "active" if current in ("invited", "claimed", "provisioned", "installed") else current
    if target == "installed":
        return "installed" if current in ("invited", "claimed", "provisioned") else current
    return current


def record_boot(
    con: sqlite3.Connection,
    *,
    record: dict[str, str],
    slug: str | None = None,
    source: str = "first_boot",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write one validated record to the tenant and state rows; idempotent.

    `record` is `parse_record`'s output. The caller passes `slug` (the CLI) or
    derives it from the token (the seed service passes the token's tenant).
    `now` is the caller's clock: the seed passes its request time, so the token
    expiry check and the row's stamps cannot land in different seconds. A
    record's own `at` wins over the clock on the first write, and no later
    write moves either stamp.
    """
    if not isinstance(record, dict):
        raise BootRecordError("the boot record is not an object")
    tenant = _tenant_row(con, slug) if slug else None
    if tenant is None:
        raise BootRecordError("record_boot needs a slug")
    name = tenant["slug"]
    measured = measured_cmdline(record.get("cmdline", ""))
    if tenant["server_id"] and measured["instance_id"] != tenant["server_id"]:
        raise BootRecordError(
            "%s is ordered for instance %r, the measured line boots %r"
            % (name, tenant["server_id"], measured["instance_id"])
        )
    if measured["hostname"] and measured["hostname"] != name:
        raise BootRecordError(
            "the measured line is for %r, the tenant row is %r" % (measured["hostname"], name)
        )
    release = (record.get("release") or "").strip()
    if not RELEASE_ID_RE.match(release):
        raise BootRecordError(
            "the boot record's release must look like 2026.11.1 or 2026.11.23-5")
    first_visit = record.get("first_visit")
    if first_visit not in FIRST_VISIT_VALUES:
        raise BootRecordError("the boot record's first_visit must be 'pending' or 'done'")

    store = StateStore(con)
    try:
        spine = store.get(name)
    except UnknownTenant:
        raise BootRecordError(
            "%s has no tenant_state row; the first boot has no order to write against" % name
        ) from None
    if spine.state in ("invited", "claimed", "provisioning"):
        raise BootRecordError(
            "%s is %s; a boot record needs the order to have reached first_boot"
            % (name, spine.state)
        )
    if spine.state == "destroyed":
        raise BootRecordError("%s is destroyed; a boot record cannot revive it" % name)

    changed: list[str] = []
    if spine.state == "first_boot":
        changed.append("first_boot -> first_visit")
        if first_visit == "done":
            changed.append("first_visit -> ready")
    elif spine.state == "first_visit" and first_visit == "done":
        changed.append("first_visit -> ready")

    detail = dict(_detail(con.execute(
        "SELECT detail FROM tenant_state WHERE slug = ?", (name,)
    ).fetchone()[0]))
    moment = now or datetime.now(timezone.utc)
    stamp = record.get("at") or moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    detail.update({
        "boot_key": measured["boot_key"],
        "boot_key_fingerprint": measured["boot_key_fingerprint"],
        "boot_release": release,
        "first_visit": first_visit,
        "record_source": source,
        "cmdline_sha256": measured["cmdline_sha256"],
    })
    # The row's first record stamps it; a replay keeps what is already there.
    # A first write prefers the record's own `at` to the writer's clock.
    if not detail.get("recorded_at"):
        detail["recorded_at"] = stamp
    if record.get("host_key"):
        detail["host_key"] = record["host_key"]
    if record.get("serving"):
        detail["serving"] = record["serving"]

    # The web row first: the throwaway key and the machine's state are the
    # record CLAIMS 4.11 names. The release is written by `record_boot_release`
    # below, which moves both rows only when the machine names a different one.
    # `installed_at` and `detail.recorded_at` are the first record's time; a
    # replay keeps both.
    current = tenant["state"]
    if first_visit == "done" and spine.state != "destroyed":
        target_web = "active"
    elif spine.state in ("first_boot", "first_visit"):
        target_web = "installed"
    else:
        target_web = current
    new_web = _web_state(current, target_web)
    installed_at = stamp if new_web in ("installed", "active") else None
    before = {
        "tenants.ssh_public_key": tenant["ssh_public_key"],
        "tenants.release": tenant["release"],
        "tenants.state": current,
        "tenant_state.state": spine.state,
        "tenant_state.release": spine.release,
        "tenant_state.initramfs_host_key": spine.initramfs_host_key,
        "tenant_state.detail": dict(spine.detail),
    }
    web_changed = (
        tenant["ssh_public_key"] != measured["boot_key"]
        or new_web != current
        or (installed_at is not None and tenant["installed_at"] is None)
    )
    if web_changed:
        try:
            con.execute(
                "UPDATE tenants SET ssh_public_key = ?, state = ?, installed_at ="
                " CASE WHEN ? IS NOT NULL THEN COALESCE(installed_at, ?)"
                " ELSE installed_at END WHERE id = ?",
                (measured["boot_key"], new_web, installed_at, installed_at,
                 tenant["id"]),
            )
            con.commit()
        except sqlite3.Error as error:
            con.rollback()
            raise BootRecordError("cannot write the tenant row: %s" % error) from None

    # The spine: one transition per edge the machine passed through. A machine
    # that is already ready takes only the note, and the note writes nothing
    # when its facts are unchanged.
    try:
        if spine.state == "first_boot":
            store.start_first_visit(name, detail=detail)
            if first_visit == "done":
                store.ready(name, detail=detail)
        elif spine.state == "first_visit" and first_visit == "done":
            store.ready(name, detail=detail)
        else:
            store.note(name, detail=detail)
        release_moved = store.record_boot_release(name, release)
        # The strict image has no initramfs wait; the old column's meaning is
        # retired (ODL-467), so the first strict record drops it. The current
        # host key lives in `detail.host_key` instead.
        if spine.state == "first_boot":
            store.forget_initramfs_host_key(name)
    except (UnknownTenant, ValueError) as error:
        raise BootRecordError(str(error)) from None

    after_tenant = _tenant_row(con, name)
    after_spine = store.get(name)
    after = {
        "tenants.ssh_public_key": after_tenant["ssh_public_key"],
        "tenants.release": after_tenant["release"],
        "tenants.state": after_tenant["state"],
        "tenant_state.state": after_spine.state,
        "tenant_state.release": after_spine.release,
        "tenant_state.initramfs_host_key": after_spine.initramfs_host_key,
        "tenant_state.detail": dict(after_spine.detail),
    }
    return {
        "slug": name,
        "ok": True,
        "state": after_spine.state,
        "tenants_state": after_tenant["state"],
        "release": release,
        "release_moved": release_moved,
        "boot_key_fingerprint": measured["boot_key_fingerprint"],
        "first_visit": first_visit,
        "changed": changed,
        "before": before,
        "after": after,
    }


# ------------------------------------------------------------------------ the CLI

def add_arguments(parser: ArgumentParser) -> None:
    parser.add_argument("slug", help="the tenant slug (the machine's hostname)")
    parser.add_argument(
        "--pin",
        required=True,
        metavar="PATH",
        help="the image pin the machine booted (its `cmdline` carries bb.key=)",
    )
    parser.add_argument(
        "--first-visit",
        choices=FIRST_VISIT_VALUES,
        default="pending",
        help="whether the owner has set the Odile sign-in (default: pending)",
    )
    parser.add_argument(
        "--host-key",
        metavar="SHA256:…",
        help="the SSH host key the machine presents now (stored in detail.host_key)",
    )
    parser.add_argument(
        "--serving",
        metavar="TEXT",
        help="what the first-visit check said (`200`, `absent`, `not checked`)",
    )
    parser.add_argument(
        "--at",
        metavar="STAMP",
        help="the record's UTC time (default: now); the first record stamps"
             " installed_at and detail.recorded_at, a replay moves neither",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help=f"the tenant database (default: the config's state_db, {DEFAULT_STATE_DB})",
    )


def _read_pin(path: str) -> dict[str, Any]:
    try:
        with open(Path(path).expanduser(), encoding="utf-8") as handle:
            value = json.load(handle)
    except OSError as error:
        raise UsageError("boot-record: cannot read the pin %s: %s" % (path, error)) from None
    except ValueError as error:
        raise UsageError("boot-record: the pin %s is not JSON: %s" % (path, error)) from None
    if not isinstance(value, dict):
        raise UsageError("boot-record: the pin %s is not a JSON object" % path)
    return value


def _cli_record(args) -> dict[str, str]:
    pin = _read_pin(args.pin)
    cmdline = pin.get("cmdline")
    if not isinstance(cmdline, str) or not cmdline.strip():
        raise UsageError("boot-record: the pin carries no measured cmdline")
    return parse_record(
        {
            "cmdline": cmdline,
            "release": pin.get("release") or pin.get("provision_release"),
            "first_visit": args.first_visit,
            "host_key": args.host_key,
            "serving": args.serving,
            "at": args.at,
        },
        slug=args.slug,
    )


def run(ctx: Context, args) -> Report:
    record = _cli_record(args)
    db = Path(args.db).expanduser() if args.db else (
        ctx.config.path_value("state_db", DEFAULT_STATE_DB) or Path(DEFAULT_STATE_DB)
    )
    actions = [{
        "action": "tenant.boot_record",
        "slug": args.slug,
        "instance_id": record["instance_id"],
        "release": record["release"],
        "boot_key_fingerprint": record["boot_key_fingerprint"],
        "first_visit": record["first_visit"],
        "db": str(db),
    }]
    notes = [
        "the key is decoded from the pin's measured line, never supplied by hand",
        "a tenant that never first-booted has no row to write and is refused",
    ]
    if not ctx.mutate:
        return plan(NAME, ctx, actions, notes)

    store = StateStore.open(db)
    try:
        written = record_boot(store.connection, slug=args.slug, record=record,
                              source="operator")
    finally:
        store.close()
    lines = [
        f"  {written['slug']}: {written['before']['tenant_state.state']} -> {written['state']}",
        f"  boot key:  {written['boot_key_fingerprint']}",
        f"  release:   {written['release']}",
        f"  web row:   {written['tenants_state']}",
    ]
    if written["release_moved"]:
        lines.append("  release rows: " + ", ".join(written["release_moved"]))
    moved = list(written["changed"])
    if written["release_moved"]:
        moved.append("release: " + written["release"])
    if moved:
        lines.append("  moved:     " + ", ".join(moved))
    else:
        lines.append("  moved:     nothing (already recorded)")
    return result(
        NAME, ctx, lines, dry_run=False, slug=args.slug, record=written,
        db=str(db),
    )
