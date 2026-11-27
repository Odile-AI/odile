"""The BX11 usage readback and its two ceilings (ODL-934).

One command answers "how full is the box?" from two sources and no new
credential:

* the box's own numbers — `stats.size` / `size_data` / `size_snapshots` from
  `GET /storage_boxes`, read with the project-scoped Console API token that
  provisioning already uses (`/root/.odile/hetzner-api-token`, 0600);
* each tenant's own last `restic stats` (`repo_bytes`, `scripts/backup.py`)
  from the signed heartbeat the poll already stores.  The provider reports the
  box as one number, so a per-tenant figure can only come from the tenant.

The command prints the box total and one row per tenant, names every
sub-account on the box whose `odile-<slug>-rw` / `-ro` slug has no tenant row
(the drill's, the demo's and the bench's while they exist) instead of folding
it into a tenant's number, and counts the sub-accounts against the box's own
`subaccounts_limit` (100, so 50 tenant pairs) before that ceiling binds.

`--apply` writes the record atomically at `/var/lib/odile/backup/usage.json`
(mode 0600, `os.replace` under a temp file in the same directory), and
`odilectl health` reads it to raise the watchdog at **800 GiB warn / 950 GiB
critical** — the fixed 1 TiB box's runaway-repository alarm.  The 2 GB
fair-use cap and 50 tenants use 100 GB at the cap, so the byte ceiling is not
the binding limit today; the readback exists to catch the repository that
ignores the plan.

No new credential, and no write to the box: the only API calls are
`GET /storage_boxes` and `GET /storage_boxes/{id}/subaccounts`, and the only
files read are the token above, the tenant state database and the heartbeat
database the poll already opens.  `git grep` finds no account-wide key, no
main-account password and no per-tenant API call in this module.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.parse
from argparse import ArgumentParser
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import provision, state
from .core import Context, Report, applied
from .errors import ConfigError, ProviderError
from .provision import BackupSettings
from .state import StateStore, Tenant

#: The readback's one record.  HQ's own backup runbook keeps its files in the
#: same directory (`status.json`, `drill.json`), so the basename says what this
#: one is: the box's usage.
DEFAULT_USAGE_PATH = "/var/lib/odile/backup/usage.json"
SCHEMA = "odile.backup-usage/1"

#: The box is one fixed 1 TiB ("1 TB" in the vendor's words: its own `size`
#: field is 1,099,511,627,776 bytes = 1024 GiB), so the ceilings are stated in
#: the same unit: 800 GiB = 858,993,459,200 bytes, 950 GiB = 1,020,054,732,800
#: bytes.  Reaching a ceiling is the condition (`>=`): unlike the age
#: thresholds, the byte line is a place, not a duration.
GIB = 1024 ** 3
WARN_BYTES = 800 * GIB
CRITICAL_BYTES = 950 * GIB

#: The alert ledger's key for the box itself.  A box condition is not a
#: tenant's, so it gets its own slug rather than borrowing a tenant's row.
BOX_SLUG_PREFIX = "backup-box-"


def box_slug(box_id: int) -> str:
    """The ledger slug for one box (`backup-box-709870`)."""
    return f"{BOX_SLUG_PREFIX}{int(box_id)}"


def human_bytes(value: Any) -> str:
    """Bytes for a human: `2.0 MiB`, `800.0 GiB`; unknown is `?`."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "?"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"  # unreachable; the loop returns


def level_for(total: int, *, warn: int = WARN_BYTES,
              critical: int = CRITICAL_BYTES) -> str:
    """`ok`, `warn` or `critical` for one byte total; thresholds are inclusive."""
    if total >= critical:
        return "critical"
    if total >= warn:
        return "warn"
    return "ok"


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- reads


def _checked_token(path: Path) -> str:
    """The token's mode, after refusing one another user could read.

    The token is the *existing* project-scoped credential; this command adds no
    new one, so all it does is hold that file to the 0600 the card names.
    """
    try:
        info = path.stat()
        text = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ConfigError(
            f"backup-usage: cannot read the box API token at {path}: {error}"
        ) from None
    if info.st_mode & 0o077:
        raise ConfigError(
            f"backup-usage: the box API token at {path} is mode"
            f" {info.st_mode & 0o777:04o}; it must be 0600"
        )
    if text.startswith("HETZNER_API_TOKEN="):
        text = text.split("=", 1)[1].strip()
    if not text:
        raise ConfigError(f"backup-usage: the box API token at {path} is empty")
    return f"{info.st_mode & 0o777:04o}"


def box_facts(settings: BackupSettings) -> dict:
    """`GET /storage_boxes` and the one box whose id is ours.

    The list endpoint is the card's read (`GET /v1/storage_boxes`) and the
    payload already carries `stats` and `subaccounts_limit`, so one call is
    enough.  A missing box, or two boxes with the same id, is a refusal rather
    than a guess.  The call goes through `provision._hetzner_api` (resolved at
    call time), so the token read, the error text and the one HTTP client are
    the provisioning path's.
    """
    _status, payload = provision._hetzner_api(settings, "GET", "/storage_boxes")
    boxes = payload.get("storage_boxes")
    if not isinstance(boxes, list):
        raise ProviderError(
            "backup-usage: GET /storage_boxes answered without a `storage_boxes` list"
        )
    matches = [box for box in boxes
               if isinstance(box, dict) and box.get("id") == settings.box_id]
    if len(matches) != 1:
        raise ProviderError(
            f"backup-usage: GET /storage_boxes answered {len(matches)} box(es)"
            f" with id {settings.box_id}; refusing to guess which one is ours"
        )
    box = matches[0]
    stats = box.get("stats")
    if not isinstance(stats, dict) or any(
        _number(stats.get(name)) is None for name in ("size_data", "size_snapshots")
    ):
        raise ProviderError(
            "backup-usage: the box API answered without integer"
            " stats.size_data / stats.size_snapshots; refusing to report 0 for unknown"
        )
    return box


def slug_of(name: Any) -> str | None:
    """The tenant slug a sub-account name belongs to, or None for a stranger."""
    if not isinstance(name, str) or not name.startswith("odile-"):
        return None
    stem = name[len("odile-"):]
    for suffix in ("-rw", "-ro"):
        if stem.endswith(suffix) and len(stem) > len(suffix):
            return stem[: -len(suffix)]
    return None


def _number(value: Any) -> int | None:
    """A byte count from JSON: a non-negative int, or None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _epoch(value: Any) -> int | None:
    """A heartbeat timestamp as an epoch: a non-negative int, or an ISO string.

    The two heartbeat writers speak different dialects.  `scripts/backup.py
    heartbeat` sends `at`; `scripts/heartbeat.py` forwards its backup fact
    through `BACKUP_FACT_KEYS`, which carries `last_success_at` and `at_iso` and
    never `at`.  Reading `at` alone left every real tenant's `backup_at` null.
    A string is parsed as ISO-8601 (a missing offset is UTC); anything else is
    no time — `None`, never a guess.
    """
    number = _number(value)
    if number is not None:
        return number
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


def _stat(record: Mapping[str, Any], name: str) -> int:
    stats = record.get("stats")
    if not isinstance(stats, dict):
        return 0
    value = _number(stats.get(name))
    return value if value is not None else 0


def _limit(record: Mapping[str, Any]) -> int:
    box_type = record.get("storage_box_type")
    if isinstance(box_type, dict):
        value = _number(box_type.get("subaccounts_limit"))
        if value:
            return value
    return 100  # the BX11's ordered limit; the API's own field wins above


def collect(
    tenants: Iterable[Tenant],
    facts_by_slug: Mapping[str, Mapping[str, Any]],
    box: Mapping[str, Any],
    subaccounts: Sequence[Mapping[str, Any]],
    *,
    now: int,
    token_path: Path,
    token_mode: str,
    api_calls: Sequence[str],
) -> dict:
    """The readback record: the box, one row per tenant, the strangers, totals.

    `facts_by_slug` is each tenant's latest heartbeat `facts` (empty when none
    arrived).  `box` is the API's box object and `subaccounts` its list; both
    are passed in so the whole computation is one pure function a test can
    call without a socket.
    """
    rows = list(tenants)
    slugs = {tenant.slug for tenant in rows}
    names_by_slug: dict[str, list[str]] = {tenant.slug: [] for tenant in rows}
    unaccounted: list[dict] = []
    for item in subaccounts:
        name = item.get("name")
        slug = slug_of(name)
        if slug is not None and slug in slugs and isinstance(name, str):
            names_by_slug[slug].append(name)
            continue
        unaccounted.append({
            "name": name if isinstance(name, str) else "",
            "username": item.get("username") or "",
            "id": item.get("id"),
            "home_directory": item.get("home_directory") or "",
            "slug": slug,
        })
    unaccounted.sort(key=lambda entry: entry["name"])

    tenant_rows: list[dict] = []
    tenant_bytes = 0
    with_a_number = 0
    for tenant in rows:
        facts = facts_by_slug.get(tenant.slug) or {}
        raw_backup = facts.get("backup")
        backup = raw_backup if isinstance(raw_backup, dict) else {}
        repo_bytes = _number(backup.get("repo_bytes"))
        status = backup.get("status") if isinstance(backup.get("status"), str) else None
        # The time that number is from.  `last_success_at` is the producer's
        # own key (scripts/heartbeat.py forwards it and `at_iso`); `at` is the
        # older `scripts/backup.py heartbeat` dialect; `at_iso` is the same
        # moment as a string.  First readable one wins.
        at = None
        for key in ("last_success_at", "at", "at_iso"):
            if at is None:
                at = _epoch(backup.get(key))
        if repo_bytes is not None:
            tenant_bytes += repo_bytes
            with_a_number += 1
        tenant_rows.append({
            "slug": tenant.slug,
            "state": tenant.state,
            "subaccounts": sorted(names_by_slug[tenant.slug]),
            "repo_bytes": repo_bytes,
            "backup_at": at,
            "status": status,
            "source": "heartbeat" if repo_bytes is not None else "none",
        })

    size_data = _stat(box, "size_data")
    size_snapshots = _stat(box, "size_snapshots")
    total = size_data + size_snapshots
    limit = _limit(box)
    pairs = sum(1 for name in (item.get("name") for item in subaccounts)
                if isinstance(name, str) and name.endswith("-rw")
                and any(other.get("name") == name[:-3] + "-ro" for other in subaccounts))
    return {
        "schema": SCHEMA,
        "at": int(now),
        "at_iso": iso(now),
        "box": {
            "id": box.get("id"),
            "name": box.get("name") or "",
            "location": (box.get("location") or {}).get("name", "")
            if isinstance(box.get("location"), dict) else "",
            "system": box.get("system") or "",
            "size_bytes": _number(box.get("storage_box_type", {}).get("size"))
            if isinstance(box.get("storage_box_type"), dict) else 0,
            "size_data": size_data,
            "size_snapshots": size_snapshots,
            "total_bytes": total,
            "subaccounts": len(subaccounts),
            "subaccounts_limit": limit,
            "pairs": pairs,
            "pair_limit": limit // 2,
        },
        "tenants": tenant_rows,
        "unaccounted": unaccounted,
        "totals": {
            "tenants": len(tenant_rows),
            "tenants_with_a_number": with_a_number,
            "tenant_bytes": tenant_bytes,
            "box_bytes": total,
        },
        "thresholds": {
            "warn_bytes": WARN_BYTES,
            "critical_bytes": CRITICAL_BYTES,
            "level": level_for(total),
        },
        "credential": {
            "token_path": str(token_path),
            "token_mode": token_mode,
            "api_calls": list(api_calls),
        },
    }


# --------------------------------------------------------------------------- writes


def write_record(path: str | Path, record: Mapping[str, Any]) -> Path:
    """Write the record atomically at mode 0600; a reader sees old or new.

    The temp file lives in the target's own directory (a rename across
    filesystems is not atomic), carries 0600 before any byte is written, and
    `os.replace` swaps it in.  A second run overwrites; a reader never sees a
    half-written record.
    """
    target = Path(path).expanduser()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle_fd, temporary = tempfile.mkstemp(prefix=".usage-", dir=str(target.parent))
    try:
        os.fchmod(handle_fd, 0o600)
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(dict(record), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    os.chmod(target, 0o600)
    return target


def read_record(path: str | Path) -> tuple[dict | None, str | None]:
    """The record and, when there is none, the reason — never a silent miss.

    A file another command wrote (a future schema, a truncated write, a
    non-integer `at`) is `None` with the reason, so `odilectl health` prints
    why the box line is missing instead of reporting a healthy box it never
    read.
    """
    target = Path(path).expanduser()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"no readback at {target}"
    except OSError as error:
        return None, f"cannot read {target}: {error}"
    try:
        record = json.loads(text)
    except ValueError as error:
        return None, f"{target} is not JSON: {error}"
    if not isinstance(record, dict):
        return None, f"{target} is not a JSON object"
    if record.get("schema") != SCHEMA:
        return None, f"{target} has schema {record.get('schema')!r}, not {SCHEMA!r}"
    # The record's own time is what the poll prints beside the box's bytes; a
    # string here used to reach `int(...)` and kill `odilectl health`, so it is
    # named as unusable where every other malformed field is.
    if _number(record.get("at")) is None:
        return None, (
            f"{target} carries at {record.get('at')!r}, not an integer epoch"
        )
    box = record.get("box")
    if (
        not isinstance(box, dict)
        or _number(box.get("id")) is None
        or _number(box.get("total_bytes")) is None
    ):
        return None, f"{target} carries no integer box.id and box.total_bytes"
    return record, None


# --------------------------------------------------------------------------- output


def _status_text(row: Mapping[str, Any]) -> str:
    if row.get("source") == "heartbeat":
        at = row.get("backup_at")
        stamp = f" at {iso(at)}" if isinstance(at, int) else ""
        return (
            f"{row['repo_bytes']} bytes ({human_bytes(row['repo_bytes'])}"
            f" from its last restic stats{stamp})"
        )
    status = row.get("status") or "no backup reported"
    return f"no restic number (heartbeat backup status: {status})"


def lines(record: Mapping[str, Any], *, path: str | Path, wrote: bool) -> list[str]:
    """The human readback: the box, the tenants, the strangers, the ceilings."""
    box = record["box"]
    totals = record["totals"]
    unwritten = "wrote" if wrote else "would write; pass --apply"
    share = (f"{box['total_bytes'] * 100.0 / box['size_bytes']:.1f}%"
             if box.get("size_bytes") else "?%")
    out = [
        f"backup usage: box {box['id']} {box['name'] or '(unnamed)'}"
        f" ({box['location'] or '?'}, {box['system'] or '?'}),"
        f" {human_bytes(box['total_bytes'])} of {human_bytes(box['size_bytes'])}"
        f" used ({share})",
        f"  stats:     size_data {box['size_data']} bytes,"
        f" size_snapshots {box['size_snapshots']} bytes,"
        f" total {box['total_bytes']} bytes",
        f"  tenants:   {totals['tenants']} row(s);"
        f" {totals['tenants_with_a_number']} with their own restic number,"
        f" {totals['tenant_bytes']} bytes ({human_bytes(totals['tenant_bytes'])})",
    ]
    for row in record["tenants"]:
        pair = f"; pair {', '.join(row['subaccounts'])}" if row["subaccounts"] else ""
        out.append(f"    {row['slug']}: {_status_text(row)}{pair}")
    out.append(
        f"  unaccounted: {len(record['unaccounted'])} sub-account(s) on the box"
        " with no tenant row"
    )
    for entry in record["unaccounted"]:
        out.append(
            f"    {entry['name'] or '(unnamed)'} {entry['username']}"
            f" (id {entry['id']}, {entry['home_directory'] or '?'})"
        )
    out.append(
        f"  sub-accounts: {box['subaccounts']} of {box['subaccounts_limit']}"
        f" ({box['pairs']} of {box['pair_limit']} tenant pairs)"
    )
    thresholds = record["thresholds"]
    out.append(
        f"  ceiling:   {thresholds['level']} (warn at {human_bytes(thresholds['warn_bytes'])}"
        f" = {thresholds['warn_bytes']} bytes, critical at"
        f" {human_bytes(thresholds['critical_bytes'])} = {thresholds['critical_bytes']} bytes)"
    )
    credential = record["credential"]
    out.append(
        f"  token:     {credential['token_path']} (mode {credential['token_mode']},"
        " project-scoped; no new credential)"
    )
    out.append(f"  calls:     {', '.join(credential['api_calls'])}")
    out.append(f"  readback:  {path} ({unwritten}, mode 0600)")
    return out


# --------------------------------------------------------------------------- the command


NAME = "backup-usage"
HELP = ("Read the BX11's stats and each tenant's last restic stats; write the usage"
        " record the watchdog reads.")
EXAMPLE = "odilectl backup-usage --apply"


def add_arguments(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--token",
        metavar="PATH",
        help=f"the project-scoped box API token, mode 0600"
        f" (default: the config's hetzner_token, {provision.HETZNER_TOKEN_FILE})",
    )
    parser.add_argument("--box-id", type=int, help="the box's API id (default: the config's)")
    parser.add_argument("--api-base", metavar="URL", help="the Box API base (default: Hetzner's)")
    parser.add_argument(
        "--db",
        metavar="PATH",
        help=f"tenant state database (default: the config's state_db, {state.DEFAULT_STATE_DB})",
    )
    parser.add_argument(
        "--heartbeat-db",
        metavar="PATH",
        help="heartbeat database (default: the same file as --db)",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help=f"the readback record (default: {DEFAULT_USAGE_PATH})",
    )
    parser.add_argument("--now", type=int, metavar="EPOCH", help="evaluate at a fixed Unix time")


def _settings(ctx: Context, args: Any) -> BackupSettings:
    settings = provision._backup_settings(ctx)
    base = (args.api_base.rstrip("/") if args.api_base else settings.base)
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError(
            f"backup-usage: the box API base {base!r} is neither https nor loopback;"
            " the bearer token would travel in clear text"
        )
    return replace(
        settings,
        base=base,
        box_id=(args.box_id if args.box_id is not None else settings.box_id),
        token=(Path(args.token).expanduser() if args.token else settings.token),
    )


def run(ctx: Context, args: Any) -> Report:
    settings = _settings(ctx, args)
    usage_path = Path(args.out).expanduser() if args.out else Path(DEFAULT_USAGE_PATH)
    state_db = Path(args.db).expanduser() if args.db else (
        ctx.config.path_value("state_db", state.DEFAULT_STATE_DB)
        or Path(state.DEFAULT_STATE_DB)
    )
    heartbeat_db = Path(args.heartbeat_db).expanduser() if args.heartbeat_db else (
        ctx.config.path_value("heartbeat_db", state_db) or state_db
    )
    now = int(args.now if args.now is not None else time.time())
    calls = ["GET /storage_boxes", f"GET /storage_boxes/{settings.box_id}/subaccounts"]

    # The credential is checked before the first call, so a loose token refuses
    # before anything is read from the provider.
    token_mode = _checked_token(settings.token)
    box = box_facts(settings)
    subaccounts = provision._subaccounts(settings)

    # The heartbeat store lives in `health`; import it here so the watchdog can
    # import this module for its thresholds without a cycle.
    from .health import HeartbeatStore

    store = StateStore.open(state_db)
    heartbeats = HeartbeatStore.open(heartbeat_db)
    try:
        tenants = store.tenants()
        beats = heartbeats.latest_all()
        facts_by_slug = {slug: beat.facts for slug, beat in beats.items()}
    finally:
        heartbeats.close()
        store.close()

    record = collect(
        tenants,
        facts_by_slug,
        box,
        subaccounts,
        now=now,
        token_path=settings.token,
        token_mode=token_mode,
        api_calls=calls,
    )
    wrote = False
    if ctx.mutate:
        write_record(usage_path, record)
        wrote = True
    rendered = lines(record, path=usage_path, wrote=wrote)
    if not ctx.mutate:
        rendered[0] += " (dry run)"
        return Report(
            {
                "ok": True,
                "command": NAME,
                "dry_run": True,
                "path": str(usage_path),
                "wrote": False,
                "usage": record,
            },
            rendered,
        )
    return applied(NAME, ctx, rendered[1:], head=rendered[0],
                   path=str(usage_path), wrote=True, usage=record)
