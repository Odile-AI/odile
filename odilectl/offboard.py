"""`offboard` — the whole of leaving: the bundle, the grant, DNS, the machine, the backup.

`odilectl destroy <slug>` is the offboard path. One run, in order, and each
step is a line in the plan before the first one happens:

1. `export.verify` — the bundle the user downloaded: its SHA-256, its size and
   the members the card names (home, memory, skills and the session store). HQ
   never reads the tenant's files: the tar is written on the machine by the
   user's own `odile-export` and downloaded to the user; this step checks the
   copy the operator holds.
2. `support.check` — the tenant's own `/verify` page, over HTTPS. A live grant
   refuses the destroy unless `--force`: `odile-support revoke` on the guest is
   the user's closure, and destroying under a live grant leaves nothing to
   revoke.
3. `dns.delete` — the A record for `<slug>.<zone>`, through the same client the
   provision path uses. Absent is success.
4. `provider.terminate` — the machine, found by hostname (its slug). Two
   machines with the same name is a refusal, never a guess.
5. `provider.absent` — the account's `?action=servers` no longer lists the id.
   The provider answers 404 on `?action=server&id=` within a minute.
6. `key.revoke` — the tenant's NEAR AI key (ODL-242): the id is resolved from
   the provider's key list by the label and the first characters HQ holds, and
   deleted; the waiting raw key is removed from the seed handoff.
7. `state.destroy` — the tenant row moves to `destroyed` with one event that
   carries the bundle's facts, the grant report, the DNS record and the
   retention date.
8. `backup.retain` — the encrypted repository is kept for 30 days, then
   deleted. HQ cannot read it (the password is the user's) and, in alpha.0,
   cannot delete it either: the plan names the date, the repository and the
   box the operator deletes it on.

A second run converges: an absent record, an absent machine and a `destroyed`
row are each reported as already done. `--skip-export` is for a tenant that
never held data (a scratch machine); every other destroy needs the bundle.

The guest's `odile-export` is `EXPORT_PROGRAM` below, printed byte-for-byte by
`odilectl export <slug> --script` and embedded in the signed installer by
`installer/embed_export.py`, which places it at `/usr/local/bin/odile-export`.
"""

from __future__ import annotations

import hashlib
import re
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import dns as dns_module
from . import keys as keys_module
from .core import Context, Report, applied, plan
from .errors import ProviderError, UsageError
from .health import HeartbeatStore
from .providers.base import Machine
from .state import DEFAULT_STATE_DB, StateStore, UnknownTenant
from .status import backup_report

RETENTION_DAYS = 30
# The four things the card names, plus the home's own agent instructions. A
# bundle missing any of them is not the tenant's data.
REQUIRED_MEMBERS = (
    "AGENTS.md",
    ".hermes",
    ".hermes/memories",
    ".hermes/skills",
    ".hermes/state.db",
)

VERIFY_TIMEOUT = 8.0
DEFAULT_WAIT = 120.0
POLL_INTERVAL = 3.0
LISTING_LINES = 40

# The test seams, module globals for the same reason `reconcile.py` has one:
# the CLI builds a real client from the config, and a test substitutes a
# recorded transport without a flag on the command.
DNS_TRANSPORT: Callable[[str, str, dict, bytes | None], tuple[int, bytes]] | None = None
VERIFY_FETCH: Callable[[str], bytes] | None = None
CLOCK: Callable[[], float] = time.time
SLEEP: Callable[[float], None] = time.sleep

# The guest program. `odilectl export <slug> --script` prints it unchanged.
EXPORT_PROGRAM = r"""#!/bin/sh
# odile-export — the tenant's own copy of its home, memory, skills and sessions.
#
# ODL-218. Run it in the Webapp's Terminal pane as the tenant user; it writes
# one gzipped tar in the home and prints its path, size and SHA-256. Download
# that file from the Files pane. Nothing at Odile reads it.
#
# The bundle is the home's *data*: the agent's instructions, memory, skills,
# sessions and configuration (.hermes), and the user's own files (documents,
# workspace, AGENTS.md). The runtime is excluded because a rebuilt guest
# reinstalls it and it does not fit on the guest's own disk: the Hermes
# checkout, the node tools, the installed bundles, caches and logs. Any other
# name in the home can be added: `odile-export <name> <dir> ...`.
#
# The session store and the task board are copied with SQLite's own backup API,
# so each is consistent while the agent is running; their -wal/-shm files are
# never copied raw. The bundle carries every secret in the data (.env, /etc/odile/key.env) because it is the user's.
set -eu
name=${1:-$(hostname)}
[ $# -gt 0 ] && shift
home=${HOME:-/home/tenant}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="${home}/odile-export-${name}-${stamp}.tar.gz"
members=""
for candidate in AGENTS.md documents workspace .hermes "$@"; do
    if [ -e "${home}/${candidate}" ]; then
        members="${members} ${candidate}"
    fi
done
if [ -z "${members}" ]; then
    echo "odile-export: nothing to export in ${home}" >&2
    exit 1
fi
# Outside the home: TMPDIR on a tenant is ${HOME}/.hermes/cache/scratch, and a
# stage there would make `tar .hermes` copy a directory into itself.
stage=$(mktemp -d /var/tmp/odile-export.XXXXXX)
staging_archive="${stage}.tar"
trap 'rm -rf "${stage}" "${staging_archive}"' EXIT INT TERM
# The member list is a list on purpose: unquoted so tar splits it. The first
# tar writes a file, not a pipe: `tar | tar` reports the extractor's status, so
# a tar that read a file twice (exit 2) would still print "wrote".
# shellcheck disable=SC2086
tar -C "${home}" -cf "${staging_archive}" \
    --exclude='.cache' --exclude='.npm' --exclude='node_modules' \
    --exclude='.hermes/cache' --exclude='.hermes/logs' \
    --exclude='.hermes/tools' --exclude='.hermes/installs' \
    --exclude='.hermes/hermes-agent' --exclude='.hermes/runtime' \
    --exclude='.hermes/audio_cache' --exclude='.hermes/image_cache' \
    --exclude='.hermes/models_dev_cache.json' \
    --exclude='.hermes/state.db' --exclude='.hermes/state.db-wal' \
    --exclude='.hermes/state.db-shm' --exclude='.hermes/kanban.db' \
    --exclude='.hermes/kanban.db-wal' --exclude='.hermes/kanban.db-shm' \
    --exclude='*.lock' --exclude='*.sock' \
    ${members}
tar -C "${stage}" -xf "${staging_archive}"
rm -f "${staging_archive}"
# Each live SQLite store is copied by SQLite's own backup API, never raw: a
# checkpoint between two raw copies leaves a stale database beside its WAL.
/usr/bin/python3 - "${stage}" "${home}/.hermes/state.db" .hermes/state.db \
    "${home}/.hermes/kanban.db" .hermes/kanban.db <<'PYDB'
import os
import sqlite3
import sys

stage = sys.argv[1]
pairs = sys.argv[2:]
for index in range(0, len(pairs), 2):
    source_path, relative = pairs[index], pairs[index + 1]
    if not os.path.exists(source_path):
        continue
    target_path = os.path.join(stage, relative)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    source = sqlite3.connect("file:%s?mode=ro" % source_path, uri=True)
    target = sqlite3.connect(target_path + ".copy")
    with target:
        source.backup(target)
    target.close()
    source.close()
    os.replace(target_path + ".copy", target_path)
PYDB
tar --create --gzip --file "${out}" -C "${stage}" .
chmod 0644 "${out}"
printf 'wrote %s\n' "${out}"
printf 'bytes %s\n' "$(wc -c < "${out}")"
printf 'sha256 %s\n' "$(sha256sum "${out}" | cut -d' ' -f1)"
printf 'members %s\n' "${members# }"
"""
# ------------------------------------------------------------------- the bundle


@dataclass(frozen=True)
class Bundle:
    """What one downloaded bundle is: its bytes, its hash and its members."""

    path: Path
    bytes: int
    sha256: str
    entries: int
    top: tuple[str, ...]
    missing: tuple[str, ...]
    listing: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.path.name,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "entries": self.entries,
            "top": list(self.top),
            "missing": list(self.missing),
        }


def _digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def inspect_bundle(path: Path) -> Bundle:
    """Read a bundle once: hash it, count it, and check the members.

    A file that is not a gzipped tar is a usage error, not a provider error:
    the operator passed the wrong file.
    """
    path = Path(path).expanduser()
    if not path.exists():
        raise UsageError(f"no bundle at {path}")
    if not path.is_file():
        raise UsageError(f"{path} is not a regular file")
    sha256, size = _digest(path)
    entries = 0
    top: set[str] = set()
    seen: set[str] = set()
    listing: list[str] = []
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                name = member.name[2:] if member.name.startswith("./") else member.name
                if not name or name == "." or name == "./":
                    continue
                entries += 1
                first = name.split("/", 1)[0]
                top.add(first)
                clean = name.rstrip("/")
                for required in REQUIRED_MEMBERS:
                    if clean == required or clean.startswith(required + "/"):
                        seen.add(required)
                if len(listing) < LISTING_LINES:
                    listing.append(("-" if member.isdir() else "f") + " " + clean)
    except (tarfile.TarError, OSError) as error:
        raise UsageError(f"{path} is not a gzipped tar: {error}") from None
    missing = tuple(name for name in REQUIRED_MEMBERS if name not in seen)
    return Bundle(
        path=path,
        bytes=size,
        sha256=sha256,
        entries=entries,
        top=tuple(sorted(top)),
        missing=missing,
        listing=tuple(listing),
    )


def bundle_lines(bundle: "Bundle", *, listing: bool = False) -> list[str]:
    """The human facts of one bundle; `listing` adds its first entries."""
    lines = [
        f"  bundle:    {bundle.path}",
        f"  size:      {bundle.bytes} bytes in {bundle.entries} entries",
        f"  sha256:    {bundle.sha256}",
        f"  top:       {', '.join(bundle.top) or '-'}",
    ]
    if bundle.missing:
        lines.append(f"  missing:   {', '.join(bundle.missing)}")
    else:
        lines.append("  members:   home, memory, skills and the session store are present")
    if listing and bundle.listing:
        lines.append(f"  listing:   the first {len(bundle.listing)} entries")
        lines.extend(f"    {entry}" for entry in bundle.listing)
    return lines


# -------------------------------------------------------------------- the grant


_GRANT_LINE = re.compile(r"operator key\s+(PASS|FAIL)\s+(.+)", re.IGNORECASE)


def probe_grant(url: str, *, fetch: Callable[[str], bytes] | None = None) -> dict[str, Any]:
    """Read the tenant's own `/verify` page; never a shell on the machine.

    `none` is the default state; `live` means the report says an operator key
    is in `authorized_keys`; `unknown` is a machine that did not answer (an
    initramfs, a scratch box, a dead DNS name). `unknown` never blocks: the
    guest is the only place a grant lives, and it is about to be destroyed.
    """
    reader = fetch or VERIFY_FETCH or _http_get
    report: dict[str, Any] = {"status": "unknown", "source": url, "line": None, "error": None}
    try:
        body = reader(url)
    except (urllib.error.URLError, OSError, ValueError) as error:
        report["error"] = str(error)
        return report
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    match = _GRANT_LINE.search(text)
    if not match:
        report["error"] = "the page carried no operator-key row"
        return report
    verdict, detail = match.group(1).upper(), " ".join(match.group(2).split())
    report["line"] = f"operator key {verdict} {detail}"
    report["status"] = "live" if verdict == "FAIL" else "none"
    return report


def _http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "text/html"})
    with urllib.request.urlopen(request, timeout=VERIFY_TIMEOUT) as response:
        return response.read()


# ------------------------------------------------------------------- the backup


def backup_facts(
    ctx: Context, slug: str, heartbeat_db: Path, *, recorded: str | None = None
) -> dict[str, Any]:
    """The last heartbeat's backup fields, and the date the copy is deleted.

    A second destroy must not push the deletion date forward: a row that
    already recorded `keep_until` keeps it.
    """
    try:
        beats = HeartbeatStore.open(heartbeat_db)
    except Exception:  # a missing or unreadable store is not a destroy blocker
        beats = None
    try:
        beat = beats.latest(slug) if beats is not None else None
        report = backup_report(beat)
    finally:
        if beats is not None:
            beats.close()
    now = int(CLOCK())
    keep_until = now + RETENTION_DAYS * 86400
    if recorded:
        try:
            keep_until = int(
                datetime.strptime(recorded, "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
        except ValueError:
            keep_until = now + RETENTION_DAYS * 86400
    return {
        "status": report["status"],
        "repo": report.get("repo"),
        "snapshot": report.get("snapshot"),
        "last_backup_at": report.get("last_backup_at"),
        "repo_bytes": report.get("repo_bytes"),
        "days": RETENTION_DAYS,
        "keep_until": _iso(keep_until),
        "keep_until_epoch": keep_until,
        "due": now >= keep_until,
    }


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ the survey


@dataclass
class Survey:
    """Everything the plan is built from, gathered before the first write."""

    slug: str
    machine: Machine | None
    tenant: Any | None
    record: Any | None
    record_name: str | None
    zone: str
    grant: dict[str, Any]
    bundle: Bundle | None
    backup: dict[str, Any]
    skip_export: bool
    key_record: Any | None = None
    key_account: Any | None = None


def _state_path(ctx: Context, args) -> Path:
    explicit = getattr(args, "db", None)
    if explicit:
        return Path(explicit).expanduser()
    return ctx.config.path_value("state_db", DEFAULT_STATE_DB) or Path(DEFAULT_STATE_DB)


def _dns_client(ctx: Context):
    zone = dns_module.validate_zone(ctx.config.get("dns_zone") or dns_module.DEFAULT_ZONE)
    transport = DNS_TRANSPORT
    if transport is None:
        token = dns_module.load_token(ctx, _NoFlags())
    else:
        token = ctx.config.get("dns_token") or "test-token"
    return dns_module.CloudflareDNS(
        token, zone, zone_id=ctx.config.get("dns_zone_id"), transport=transport
    )


class _NoFlags:
    """The DNS token loader reads `--token-file`; the destroy command has none."""

    token_file = None


def _survey(ctx: Context, args, bundle: Bundle | None) -> Survey:
    slug = args.slug
    skip_export = bool(getattr(args, "skip_export", False))

    zone = dns_module.validate_zone(ctx.config.get("dns_zone") or dns_module.DEFAULT_ZONE)
    # The record hangs under the tenant's own name (`tenant_domain`), not the
    # apex zone the token talks to: the same rule `odilectl dns` follows.
    domain = dns_module.tenant_zone(ctx.config)
    try:
        name = dns_module.record_name(slug, domain)
    except ValueError:
        name = None  # a machine named outside the slug grammar has no record
    record = _dns_client(ctx).find(name) if name else None

    machines = [machine for machine in ctx.provider.list() if machine.hostname == slug]
    if len(machines) > 1:
        raise ProviderError(
            f"destroy: {len(machines)} machines are named {slug}; refusing to guess which to terminate"
        )
    machine = machines[0] if machines else None

    state_db = _state_path(ctx, args)
    store = StateStore.open(state_db)
    try:
        try:
            tenant = store.get(slug)
        except UnknownTenant:
            tenant = None
        key_record = store.key_for(slug)
    finally:
        store.close()

    heartbeat_db = ctx.config.path_value("heartbeat_db", state_db) or state_db
    recorded = None
    if tenant is not None:
        recorded = ((tenant.detail or {}).get("backup") or {}).get("keep_until")
    return Survey(
        slug=slug,
        machine=machine,
        tenant=tenant,
        record=record,
        record_name=name,
        zone=zone,
        grant=probe_grant(f"https://{name or slug + '.' + domain}/verify"),
        bundle=bundle,
        backup=backup_facts(ctx, slug, Path(heartbeat_db), recorded=recorded),
        skip_export=skip_export,
        key_record=key_record,
        key_account=keys_module.account_from_config(ctx.config),
    )


# ------------------------------------------------------------------- the steps


def _actions(survey: Survey, args) -> list[dict[str, Any]]:
    """The ordered plan: every action, with the facts it will act on."""
    if survey.bundle is not None:
        export = {
            "action": "export.verify",
            "file": survey.bundle.path.name,
            "sha256": survey.bundle.sha256,
            "bytes": survey.bundle.bytes,
            "entries": survey.bundle.entries,
            "members": "ok" if survey.bundle.complete else "missing",
        }
    elif survey.skip_export:
        export = {"action": "export.verify", "result": "skipped (--skip-export)"}
    else:
        export = {"action": "export.verify", "result": "no bundle given (--export PATH)"}

    grant = {
        "action": "support.check",
        "url": survey.grant["source"],
        "result": survey.grant["status"],
    }
    if survey.grant.get("line"):
        grant["line"] = survey.grant["line"]

    dns_action = {
        "action": "dns.delete",
        "zone": survey.zone,
        "name": survey.record_name,
        "type": "A",
        "record_id": survey.record.id if survey.record else None,
        "result": (
            "present"
            if survey.record
            else ("absent" if survey.record_name else "not a tenant slug")
        ),
    }

    if survey.machine is not None:
        terminate = {
            "action": "provider.terminate",
            "hostname": survey.slug,
            "server_id": survey.machine.server_id,
            "ipv4": survey.machine.ipv4,
        }
        absent = {
            "action": "provider.absent",
            "server_id": survey.machine.server_id,
            "until": "not listed by ?action=servers",
            "timeout_seconds": _wait_seconds(args),
        }
    else:
        terminate = {
            "action": "provider.terminate",
            "hostname": survey.slug,
            "result": "nothing to do",
        }
        absent = {"action": "provider.absent", "hostname": survey.slug, "result": "nothing to do"}

    if survey.tenant is None:
        state = {"action": "state.destroy", "slug": survey.slug, "result": "no tenant row"}
    elif survey.tenant.state == "destroyed":
        state = {"action": "state.destroy", "slug": survey.slug, "result": "already destroyed"}
    else:
        state = {
            "action": "state.destroy",
            "slug": survey.slug,
            "from": survey.tenant.state,
            "detail": "bundle, grant, DNS, machine and retention",
        }

    backup = {
        "action": "backup.retain",
        "repo": survey.backup.get("repo"),
        "snapshot": survey.backup.get("snapshot"),
        "status": survey.backup["status"],
        "keep_until": survey.backup["keep_until"],
        "days": RETENTION_DAYS,
        "then": "delete the repository on the backup box",
    }
    if survey.key_record is None:
        key_action: dict[str, Any] = {
            "action": "key.revoke",
            "slug": survey.slug,
            "result": "no key record for this tenant",
        }
    elif survey.key_account is None:
        key_action = {
            "action": "key.revoke",
            "label": survey.key_record.label,
            "key_prefix": survey.key_record.key_prefix,
            "result": "skipped (no near_account in the config)",
        }
    else:
        key_action = {
            "action": "key.revoke",
            "label": survey.key_record.label,
            "key_prefix": survey.key_record.key_prefix,
            "state": survey.key_record.state,
            "workspace": survey.key_account.workspace_id,
            "resolve": "the list call's id where name and key_prefix both match",
            "then": "remove the waiting raw key from the seed handoff",
        }
    return [export, grant, dns_action, terminate, absent, key_action, state, backup]


def _notes(survey: Survey, args) -> list[str]:
    notes = [
        "the bundle is written on the guest by the user's own odile-export and "
        "downloaded by the user; HQ checks the operator's copy, not the machine",
        "a live support grant refuses the destroy unless --force: its closure is "
        "`odile-support revoke` on the guest",
        "the encrypted backup is kept for "
        f"{RETENTION_DAYS} days, then deleted; HQ holds no password for it",
        "the tenant's NEAR AI key is revoked after the machine is gone: the id is "
        "resolved from the provider's list by the label and first characters HQ "
        "holds, and the old machine's key cannot spend afterwards",
    ]
    if survey.bundle is None and not survey.skip_export:
        notes.append("pass --export PATH (the downloaded bundle) or --skip-export")
    if survey.bundle is not None and not survey.bundle.complete:
        notes.append(
            "the bundle is missing " + ", ".join(survey.bundle.missing)
        )
    if survey.grant["status"] == "unknown":
        notes.append(
            f"the tenant's /verify did not answer ({survey.grant.get('error')}); "
            "no grant was found, and the machine is destroyed either way"
        )
    return notes


def _detail(survey: Survey, *, dns_result: str, absent_at: str | None, reason: str,
            key_result: dict[str, Any] | None = None) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "reason": reason,
        "grant": {"status": survey.grant["status"], "source": survey.grant["source"]},
        "dns": {
            "name": survey.record_name,
            "zone": survey.zone,
            "action": dns_result,
            "record_id": survey.record.id if survey.record else None,
        },
        "backup": {
            "repo": survey.backup.get("repo"),
            "snapshot": survey.backup.get("snapshot"),
            "keep_until": survey.backup["keep_until"],
            "days": RETENTION_DAYS,
        },
    }
    if key_result is not None:
        detail["key"] = key_result
    if survey.bundle is not None:
        detail["export"] = survey.bundle.to_dict()
    else:
        detail["export"] = {"skipped": True} if survey.skip_export else None
    if survey.machine is not None:
        detail["machine"] = {
            "server_id": survey.machine.server_id,
            "ipv4": survey.machine.ipv4,
        }
        detail["absent_at"] = absent_at
    return detail


def _wait_seconds(args) -> float:
    """The --wait value; an absent or None flag is the default."""
    value = getattr(args, "wait", None)
    return float(DEFAULT_WAIT if value is None else value)


def _wait_absent(ctx: Context, server_id: str, *, timeout: float, interval: float) -> str | None:
    """Poll the account until the id is gone; return the moment, or None."""
    waited = 0.0
    while True:
        if not any(machine.server_id == server_id for machine in ctx.provider.list()):
            return _iso(int(CLOCK()))
        if waited >= timeout:
            return None
        SLEEP(interval)
        waited += interval


def _revoke_key(ctx: Context, survey: Survey, lines: list[str], state_db: Path) -> dict[str, Any]:
    """Revoke the tenant's inference key after its machine is gone.

    The provider's list call is the id resolver: HQ holds the label and the
    first characters, and a key is only deleted when both match — so a label
    reused by another key cannot revoke the wrong one. The waiting raw key in
    the seed handoff is removed too: nothing at HQ should outlive the instance
    it belonged to.

    A host with no `near_account` has no management credential, so it says so
    and marks nothing; the record still names the key a later `destroy` on a
    configured host will revoke.
    """
    record = survey.key_record
    if record is None:
        lines.append("  key:        no key record for this tenant; nothing to revoke")
        return {"result": "none", "slug": survey.slug}
    account = survey.key_account
    if account is None:
        lines.append(
            f"  key:        {record.label} {record.key_prefix} not revoked "
            "(no near_account in the config)"
        )
        return {
            "result": "skipped",
            "reason": "no near_account in the config",
            "label": record.label,
            "key_prefix": record.key_prefix,
            "state": record.state,
        }
    store = StateStore.open(state_db)
    try:
        try:
            revoked = keys_module.revoke_for(account, store, survey.slug)
        except ProviderError as error:
            raise ProviderError(
                f"destroy: {record.label} was not revoked, so the old machine's key "
                f"can still spend: {error}"
            ) from None
    finally:
        store.close()
    dropped = keys_module.drop_waiting_key(ctx.config, survey.slug)
    if revoked is None:
        lines.append(f"  key:        {record.label} has no record to revoke")
        return {"result": "none", "label": record.label, "key_prefix": record.key_prefix}
    if revoked.resolved == "list":
        lines.append(
            f"  key:        {revoked.label} {revoked.key_prefix} revoked at the provider "
            f"(id {revoked.key_id} resolved from the list call)"
        )
    else:
        lines.append(
            f"  key:        {revoked.label} {revoked.key_prefix} was already absent at "
            "the provider; the record is marked revoked"
        )
    if dropped:
        lines.append("  key:        the waiting raw key was removed from the seed handoff")
    return {**revoked.to_dict(), "waiting_key_removed": dropped}


# --------------------------------------------------------------------- the run


def execute(ctx: Context, args, *, name: str = "destroy") -> Report:
    """Plan or apply the offboard path; `destroy.py` calls this."""
    slug = args.slug
    export_path = getattr(args, "export", None)
    skip_export = bool(getattr(args, "skip_export", False))
    bundle = inspect_bundle(Path(export_path)) if export_path else None

    # The bundle is an input, so its refusals come before any call: a missing
    # or incomplete bundle must not read the provider, the zone or the guest.
    if ctx.mutate:
        if bundle is None and not skip_export:
            raise UsageError(
                "destroy: pass --export PATH (the bundle you downloaded) or --skip-export "
                "for a tenant that never held data"
            )
        if bundle is not None and not bundle.complete:
            raise UsageError(
                "destroy: the bundle is missing "
                + ", ".join(bundle.missing)
                + "; export again before destroying"
            )

    survey = _survey(ctx, args, bundle)
    actions = _actions(survey, args)
    notes = _notes(survey, args)

    if not ctx.mutate:
        fields: dict[str, Any] = {
            "slug": slug,
            "server_id": survey.machine.server_id if survey.machine else None,
            "dns": {
                "name": survey.record_name,
                "zone": survey.zone,
                "record_id": survey.record.id if survey.record else None,
            },
            "grant": survey.grant,
            "backup": survey.backup,
        }
        if survey.bundle is not None:
            fields["export"] = survey.bundle.to_dict()
        return plan(name, ctx, actions, notes, **fields)

    # The grant is read from the guest, so its refusal comes after the survey
    # and still before the first write.
    if survey.grant["status"] == "live" and not getattr(args, "force", False):
        raise UsageError(
            f"destroy: {slug} reports a live support grant ({survey.grant.get('line')}); "
            "revoke it on the guest with `odile-support revoke`, or pass --force"
        )

    lines: list[str] = []
    if survey.bundle is not None:
        lines.append(
            f"  bundle:     {survey.bundle.path.name} sha256 {survey.bundle.sha256} "
            f"({survey.bundle.bytes} bytes, {survey.bundle.entries} entries)"
        )
    else:
        lines.append("  bundle:     skipped (--skip-export)")
    lines.append(f"  grant:      {survey.grant['status']} ({survey.grant['source']})")

    if survey.record_name:
        change = _dns_client(ctx).remove(survey.record_name, apply=True)
        dns_result = change.action
        if change.action == "deleted":
            lines.append(
                f"  dns:        removed {survey.record_name} (record {change.record.id})"
            )
        else:
            lines.append(f"  dns:        {survey.record_name} was already absent in {survey.zone}")
        record_id = change.record.id if change.record else None
    else:
        dns_result = "not a tenant slug"
        record_id = None
        lines.append(f"  dns:        {slug} is not a tenant slug; no record to delete")

    absent_at: str | None = None
    destroyed = False
    already_gone = False
    if survey.machine is not None:
        server_id = survey.machine.server_id
        destroyed = ctx.provider.terminate(server_id)
        if destroyed:
            lines.append(f"  machine:    terminate requested for {server_id} ({slug})")
        else:
            already_gone = True
            lines.append(f"  machine:    {server_id} ({slug}) was already gone")
        absent_at = _wait_absent(
            ctx,
            server_id,
            timeout=_wait_seconds(args),
            interval=POLL_INTERVAL,
        )
        if absent_at is None:
            raise ProviderError(
                f"destroy: {server_id} was terminated but is still listed after "
                f"{_wait_seconds(args):g}s; run the command again to finish"
            )
        lines.append(f"  machine:    {server_id} is gone from the account at {absent_at}")
    else:
        already_gone = True
        lines.append(f"  machine:    {slug} is already gone (no machine at {ctx.provider.name})")

    reason = getattr(args, "reason", None) or "offboard"
    key_result = _revoke_key(ctx, survey, lines, _state_path(ctx, args))
    state_db = _state_path(ctx, args)
    store = StateStore.open(state_db)
    try:
        tenant = None
        try:
            tenant = store.get(slug)
        except UnknownTenant:
            tenant = None
        if tenant is None:
            lines.append(f"  state:      no tenant row for {slug}; nothing to mark")
        elif tenant.state == "destroyed":
            lines.append(f"  state:      {slug} was already destroyed")
        else:
            detail = _detail(
                survey,
                dns_result=dns_result,
                absent_at=absent_at,
                reason=reason,
                key_result=key_result,
            )
            store.transition(slug, "destroyed", detail=detail)
            lines.append(f"  state:      {slug}: {tenant.state} -> destroyed (one event)")
    finally:
        store.close()

    lines.append(
        f"  backup:     kept until {survey.backup['keep_until']} "
        f"({RETENTION_DAYS} days){_backup_bits(survey.backup)}"
    )
    if survey.backup["due"]:
        lines.append(f"  backup:     due now — delete it on the backup box: {survey.backup['repo']}")

    payload: dict[str, Any] = {
        "slug": slug,
        "server_id": survey.machine.server_id if survey.machine else None,
        "destroyed": destroyed,
        "already_gone": already_gone,
        "absent_at": absent_at,
        "dns": {
            "name": survey.record_name,
            "zone": survey.zone,
            "action": dns_result,
            "record_id": record_id,
        },
        "grant": survey.grant,
        "backup": survey.backup,
        "key": key_result,
    }
    if survey.bundle is not None:
        payload["export"] = survey.bundle.to_dict()
    return applied(name, ctx, lines, **payload)


def _backup_bits(backup: dict[str, Any]) -> str:
    bits = []
    if backup.get("snapshot"):
        bits.append(f"snapshot {str(backup['snapshot'])[:8]}")
    if backup.get("repo"):
        bits.append(str(backup["repo"]))
    if backup["status"] == "no_heartbeat":
        bits.append("no heartbeat carried a repository")
    return ", " + ", ".join(bits) if bits else ""
