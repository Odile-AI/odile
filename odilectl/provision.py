"""`provision` — order the tenant's machine and wait for it to come up.

This is the one command that is fully wired in the skeleton: it is the flow the
provider interface exists for. `create` returns as soon as the order has an id;
`poll` waits for `active` and the address.

The release the machine is ordered to boot comes from the repo's pin, never
from the provider: `odile-verify.pin` carries `provision_release` (ODL-395), and
a pin without it is a usage error before anything is ordered. When the machine
is active the release is recorded on the tenant's state row (ODL-398), so a
later heartbeat can be compared with what was intended.

The machine is ordered with exactly one public key, the tenant's, and with
SEV-SNP, LUKS and measured boot on. There is no operator key to add later.

The seed token is bound the moment the provider answers `create` (ODL-455).
The provider answers an order before it builds the machine: the live capture in
`docs/runbooks/provision.md` (16:01:30Z) got `server_id=31a63bbb
status=deploying` in 0.232 s and `active` 120.7 s later. `_bind_seed_token`
writes one single-use token to the seed ledger, bound to that id, and only its
SHA-256 hash on the tenant row, before the machine is polled — long before it
can boot and ask for a seed. The plaintext is printed once. A second run ends
the previous token and mints a new one (the manual recovery path: rerun and
print the fresh token). If the slug has no tenant row yet, the claim worker
mints the token when the claim exists (ODL-220) — the fallback, and the report
names it instead of inventing a tenant row.

**A rebuild keeps the repository (ODL-215 follow-up).** The user's old recovery
file already opens the repository; a new machine that mints a new password
meets that repository with a key it cannot use. With `--backup-password-file`
the run reads the `Backup password:` line of the user's file and seals it under
the token it just minted (`seed.seal_backup_password`), storing only ciphertext
in `/var/lib/odile/seed-rebuilds.json`, one entry per slug: the guest opens it
with the token from the measured command line, which HQ prints once and keeps
as a hash. A run *without* the flag removes the slug's entry, so a token never
carries a stale secret.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from argparse import ArgumentParser
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .core import Context, Report, applied, plan, render_datetime
from .errors import ProviderError, UsageError
from .providers import configured_name
from .providers.base import CreateSpec, Machine
from .state import (
    DEFAULT_STATE_DB,
    RELEASE_ID_RE,
    StateError,
    StateStore,
    UnknownTenant,
)

NAME = "provision"
HELP = "Order the tenant's machine and wait until it is active."
EXAMPLE = "odilectl provision odl-7f3a2c --key ~/.ssh/ada.pub --apply"

DEFAULT_TIMEOUT = 900.0
DEFAULT_INTERVAL = 10.0
DEFAULT_PIN = Path(__file__).resolve().parent.parent / "odile-verify.pin"
SETUP_TOKEN_TTL_HOURS = 24


@lru_cache(maxsize=1)
def _seed_service():
    """Load ``seed/seed.py`` by path, the way ``odilectl.claim`` does.

    A package import cannot work: the service runs as ``python3 seed/seed.py``,
    so ``sys.path[0]`` is the ``seed/`` directory and ``seed`` resolves to the
    script itself. The path load works in the CLI, the web app and a test alike.
    """
    path = Path(__file__).resolve().parent.parent / "seed" / "seed.py"
    spec = importlib.util.spec_from_file_location("odile_seed_service_provision", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the seed service at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class SeedBinding:
    """The token a provision run minted, or why it minted none."""

    instance_id: str
    bound: bool
    token: str | None = None
    token_hash: str | None = None
    expires_at: str | None = None
    reason: str = ""
    # "sealed" when the old repository password travelled with this token,
    # "none" when it did not (a first install, or a rebuild without the file).
    rebuild: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "bound": self.bound,
            "instance_id": self.instance_id,
            "token": self.token,
            "token_hash": self.token_hash,
            "expires_at": self.expires_at,
            "reason": self.reason,
            "rebuild": self.rebuild,
        }


def _seed_db(ctx: Context) -> Path:
    """The seed ledger's file: ``seed_db`` when set, else the state database.

    Both keys point at ``/var/lib/odile/waitlist.db`` in the shipped configs;
    the ledger and the tenant row it hashes into must live in one SQLite file.
    """
    configured = ctx.config.path_value("seed_db")
    if configured is not None:
        return configured
    return ctx.config.path_value("state_db", DEFAULT_STATE_DB) or Path(DEFAULT_STATE_DB)


def _bind_seed_token(ctx: Context, slug: str, instance_id: str,
                     backup_password: str | None = None) -> SeedBinding:
    """Mint one single-use token bound to the machine that was just ordered.

    Called immediately after the instance id exists (the `create` response, or
    an adopted machine), before the machine is polled. A tenant row without a
    token yet is the manual/claim path, so a missing row is a reported fallback,
    not a failure; any other ledger error is loud.

    `backup_password` is the user's old repository password (a rebuild). It is
    sealed under the token that was just minted and written to
    `/var/lib/odile/seed-rebuilds.json` as ciphertext; the run's report says
    which happened.
    """
    path = _seed_db(ctx)
    try:
        con = sqlite3.connect(str(path), timeout=5.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
    except sqlite3.Error as error:
        raise ProviderError(f"provision: cannot open the seed ledger at {path}: {error}") from None
    seed = _seed_service()
    try:
        seed.ensure_schema(con)
        issued = seed.bind(
            con, slug, instance_id=instance_id, ttl_hours=SETUP_TOKEN_TTL_HOURS
        )
        rebuild = _write_rebuild_entry(ctx, seed, slug, issued, backup_password)
        return SeedBinding(
            instance_id=issued["instance_id"],
            bound=True,
            token=issued["token"],
            token_hash=issued["token_hash"],
            expires_at=issued["expires_at"],
            rebuild=rebuild,
        )
    except seed.SeedError as error:
        if error.reason == "no_tenant":
            return SeedBinding(
                instance_id=instance_id,
                bound=False,
                reason=(
                    f"no tenants row for {slug}; the claim worker mints the token "
                    "when the claim exists (ODL-220)"
                ),
            )
        raise ProviderError(
            f"provision: {instance_id} is ordered but its seed token was not bound: {error}"
        ) from None
    except (sqlite3.Error, OSError) as error:
        raise ProviderError(
            f"provision: {instance_id} is ordered but its seed token was not bound: {error}"
        ) from None
    finally:
        con.close()


def _read_backup_password(path: Path) -> str:
    """The `Backup password:` line of the user's recovery file.

    The reader is `seed.read_recovery_password`: the same grammar
    `scripts/backup.py` uses to open the repository. A missing or short value
    is a usage error *before* anything is ordered.
    """
    seed = _seed_service()
    try:
        return seed.read_recovery_password(str(path))
    except ValueError as error:
        raise UsageError(f"provision: {error}") from None


def _seed_rebuilds_path(ctx: Context, seed) -> str:
    """The file the seed service reads per exchange: ciphertext, one entry per rebuild.

    Not the seed config: systemd hands the service a *copy* of the config at
    start (`LoadCredential=`), so a rewrite would be invisible until a restart.
    This file is read per redeem, is 0600 and is chowned to the service user.
    """
    configured = ctx.config.path_value("seed_rebuilds")
    if configured is not None:
        return str(configured)
    return seed.rebuilds_path()


def _write_rebuild_entry(ctx: Context, seed, slug: str, issued: dict,
                         backup_password: str | None) -> str:
    """Seal the old password under this token, or clear the slug's entry.

    Returns "sealed" or "none" for the report. The file holds the ciphertext,
    never the password; a stale entry cannot be opened by a later token.
    """
    path = _seed_rebuilds_path(ctx, seed)
    if backup_password is None:
        if os.path.isfile(path):
            try:
                seed.set_rebuild(path, slug, None)
            except (seed.ConfigError, OSError, ValueError) as error:
                raise ProviderError(
                    f"provision: {issued['instance_id']} is ordered but the rebuilds file "
                    f"at {path} could not be cleared for {slug}: {error}"
                ) from None
        return "none"
    try:
        entry = seed.seal_backup_password(backup_password, issued["token"])
        entry["token_hash"] = issued["token_hash"]
        entry["created_at"] = seed.iso(seed.utcnow())
        seed.set_rebuild(path, slug, entry)
    except (seed.ConfigError, OSError, ValueError) as error:
        raise ProviderError(
            f"provision: {issued['instance_id']} is ordered but its rebuild password "
            f"was not sealed into {path}: {error}"
        ) from None
    return "sealed"


def _seed_lines(binding: SeedBinding) -> list[str]:
    if not binding.bound:
        return [f"  setup token: not minted ({binding.reason})"]
    lines = [
        f"  setup token: {binding.token}",
        f"  token hash:  {binding.token_hash}",
        f"  token expires: {binding.expires_at}",
        "  printed once; the seed service stores only its SHA-256 hash",
    ]
    if binding.rebuild == "sealed":
        lines.append(
            "  rebuild:     the old repository password is sealed under this token "
            "(seed-rebuilds.json; ciphertext, no plaintext at HQ)"
        )
    else:
        lines.append("  rebuild:     no sealed password (a first install mints one)")
    return lines


def add_arguments(parser: ArgumentParser) -> None:
    parser.add_argument("slug", help="the tenant's random slug, e.g. odl-7f3a2c")
    parser.add_argument(
        "--key",
        metavar="PATH",
        help="the tenant's SSH public key file (required to apply until ODL-212 holds the row)",
    )
    parser.add_argument(
        "--backup-password-file",
        metavar="PATH",
        help="the user's old recovery file (its 'Backup password:' line is sealed "
             "under the new setup token, so a rebuilt machine keeps its repository)",
    )
    parser.add_argument(
        "--plan", metavar="ID", help="the provider's plan id (default: default_plan_id)"
    )
    parser.add_argument(
        "--pin",
        metavar="PATH",
        help=f"the release pin whose provision_release is recorded (default: {DEFAULT_PIN})",
    )
    parser.add_argument("--days", type=int, metavar="N", help="term in days (default: 31)")
    parser.add_argument(
        "--preorder",
        action="store_true",
        help="accept a pre-order when the plan is sold out (final and paid now)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help="how long to wait for active (default: 900)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        metavar="SECONDS",
        help="seconds between polls (default: 10)",
    )


def _key_path(args) -> Path | None:
    return Path(args.key).expanduser() if args.key else None


def _read_key(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise UsageError(f"provision: no public key at {path}") from None
    except OSError as error:
        raise UsageError(f"provision: cannot read {path}: {error.strerror}") from None


def _plan_id(ctx: Context, args) -> int:
    raw = args.plan or ctx.config.get("default_plan_id")
    if raw is None:
        raise UsageError("provision: pass --plan ID, or set default_plan_id in the config")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise UsageError(f"provision: plan id {raw!r} is not an integer") from None


def _days(ctx: Context, args) -> int:
    if args.days is not None:
        return args.days
    raw = ctx.config.get("default_days", "31") or "31"
    try:
        return int(raw)
    except ValueError:
        raise UsageError(f"provision: default_days {raw!r} is not an integer") from None


def pinned_release(pin: str | Path | None = None, *, prefix: str = "provision") -> str:
    """The release id from the pin, or a usage error before anything is ordered.

    Only `provision_release` is a source. The provider may know what it last
    booted, and a stale pin may name an older release, but neither is an
    instruction: the field in this file is what the machine is ordered to boot.

    `prefix` names the calling command in the error. One helper serves
    `odilectl provision` and the claim worker (ODL-398 follow-up), so the
    validation and the fail-closed wording cannot drift between the two paths.
    """
    path = Path(pin).expanduser() if pin else DEFAULT_PIN
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UsageError(f"{prefix}: no pin at {path}; the boot release is unpinned") from None
    except OSError as error:
        raise UsageError(f"{prefix}: cannot read the pin at {path}: {error.strerror}") from None
    except ValueError as error:
        raise UsageError(f"{prefix}: the pin at {path} is not JSON: {error}") from None
    if not isinstance(document, dict):
        raise UsageError(f"{prefix}: the pin at {path} is not a JSON object")
    release = document.get("provision_release")
    if not isinstance(release, str) or not release.strip():
        raise UsageError(
            f"{prefix}: the pin at {path} has no provision_release; "
            "refusing to order a machine whose boot release is unpinned"
        )
    chosen = release.strip()
    if not RELEASE_ID_RE.match(chosen):
        raise UsageError(
            f"{prefix}: the pin's provision_release {chosen!r} is not a release id"
        )
    return chosen


def _record_machine(
    ctx: Context, spec: CreateSpec, machine: Machine, release: str
) -> dict[str, object]:
    """Write the order's release onto the tenant's state row, idempotently.

    A tenant created by the claim spine already has a state row; a manual
    provision creates the missing one at `invited` and walks it to
    `provisioning`, the same path the claim code uses. A second run does not
    move the state again, and `record_release` fills a NULL release only, so
    the release the machine was actually ordered under is never rewritten.
    """
    db_path = ctx.config.path_value("state_db", DEFAULT_STATE_DB) or Path(DEFAULT_STATE_DB)
    store = StateStore.open(db_path)
    try:
        try:
            tenant = store.get(spec.hostname)
        except UnknownTenant:
            tenant = store.invite(
                spec.hostname, detail={"source": "odilectl provision"}
            )
        if tenant.state == "invited":
            tenant = store.claim(spec.hostname)
        if tenant.state == "claimed":
            tenant = store.provision(
                spec.hostname,
                server_id=machine.server_id,
                hostname=machine.hostname,
                ipv4=machine.ipv4,
                plan_id=machine.plan_id,
                release=release,
                expires_at=machine.expires_at,
                detail={"source": "odilectl provision", "release": release},
            )
            return {"recorded": True, "tenant_state": tenant.state, "release": tenant.release}
        if tenant.state == "destroyed":
            raise StateError(
                f"{spec.hostname} is destroyed; its release cannot be recorded"
            )
        before = tenant.release
        tenant = store.record_release(spec.hostname, release)
        return {
            "recorded": before is None,
            "tenant_state": tenant.state,
            "release": tenant.release,
        }
    finally:
        store.close()


def _record(ctx: Context, spec: CreateSpec, machine: Machine, release: str) -> dict[str, object]:
    try:
        return _record_machine(ctx, spec, machine, release)
    except StateError as error:
        raise ProviderError(
            f"provision: {machine.server_id} is active but its release was not "
            f"recorded: {error}"
        ) from None


def run(ctx: Context, args) -> Report:
    release = pinned_release(args.pin)
    plan_id = _plan_id(ctx, args)
    days = _days(ctx, args)
    key_path = _key_path(args)
    key_label = str(key_path) if key_path else "<the tenant record>"
    # A rebuild: the old password is read before anything is ordered, so a
    # missing or unreadable recovery file is a usage error, not a half-done
    # provision. The value never reaches argv, the plan or the report.
    backup_password = _read_backup_password(Path(args.backup_password_file)) if (
        args.backup_password_file) else None
    backup_label = str(Path(args.backup_password_file).expanduser()) if (
        args.backup_password_file) else "none"

    try:
        # Validating the shape before planning also catches a bad slug early.
        spec = CreateSpec(
            hostname=args.slug,
            plan_id=plan_id,
            ssh_key=_read_key(key_path) if key_path else _placeholder_key(),
            days=days,
            preorder=args.preorder,
        )
    except ValueError as error:
        raise UsageError(f"provision: {error}") from None

    actions = [
        {
            "action": "provider.list",
            "provider": configured_name(ctx.config),
            "find_hostname": spec.hostname,
        },
        {
            "action": "provider.create",
            "provider": configured_name(ctx.config),
            "hostname": spec.hostname,
            "plan_id": spec.plan_id,
            "os": spec.os,
            "days": spec.days,
            "snp": spec.snp,
            "fde": spec.fde,
            "measured": spec.measured,
            "preorder": spec.preorder,
            "ssh_key": key_label,
            "release": release,
            "when": "absent from the list above",
        },
        {
            "action": "seed.token.bind",
            "slug": spec.hostname,
            "instance": "the server_id provider.create returns",
            "ttl_hours": SETUP_TOKEN_TTL_HOURS,
        },
        {
            "action": "seed.rebuild.seal",
            "slug": spec.hostname,
            "file": backup_label,
            "sealed_under": "the setup token the bind above mints",
            "when": "always: the entry is written with the file, removed without it",
        },
        {
            "action": "provider.poll",
            "until": "active",
            "timeout_seconds": args.timeout,
            "interval_seconds": args.interval,
        },
    ]
    notes = [
        "an active, pending or queued machine with this hostname is reused, "
        "so a second run orders no duplicate",
        f"the release {release} comes from the pin's provision_release and is "
        "recorded with the tenant once the machine is active",
        "the setup token is bound to the instance id the create call returns, "
        "before the machine is polled: single-use, and refused on any other instance",
        "the token is printed once; the seed service stores only its SHA-256 hash",
        f"rebuild password: {backup_label} (its 'Backup password:' line is sealed "
        "under the token; `/var/lib/odile/seed-rebuilds.json` holds ciphertext, "
        "never the password)",
    ]

    if not ctx.mutate:
        return plan(
            NAME,
            ctx,
            actions,
            notes,
            plan_id=plan_id,
            days=days,
            ssh_key=key_label,
            release=release,
            backup_password_file=backup_label,
        )

    if key_path is None:
        raise UsageError(
            "provision --apply needs --key PATH until the tenant record holds the key"
        )

    existing = [machine for machine in ctx.provider.list() if machine.hostname == spec.hostname]
    if len(existing) > 1:
        raise ProviderError(
            f"provision: {len(existing)} machines are named {spec.hostname}; refusing to guess"
        )
    if existing:
        machine = existing[0]
        if _is_queued(machine):
            seed = _bind_seed_token(ctx, spec.hostname, machine.server_id,
                                    backup_password=backup_password)
            return _queued_report(ctx, machine, created=False, release=release, seed=seed)
        if machine.state == "active" and machine.ipv4:
            seed = _bind_seed_token(ctx, spec.hostname, machine.server_id,
                                    backup_password=backup_password)
            record = _record(ctx, spec, machine, release)
            return applied(
                NAME,
                ctx,
                [
                    f"  {machine.server_id} ({machine.hostname}) is already active",
                    f"  release:   {record['release']} (from the pin, recorded with the tenant)",
                    *_seed_lines(seed),
                ],
                head="provision: nothing ordered (the machine already exists)",
                machine=machine.to_dict(),
                created=False,
                recorded=record["recorded"],
                release=record["release"],
                tenant_state=record["tenant_state"],
                seed=seed.to_dict(),
            )
        if machine.state not in ("pending", "active"):
            raise ProviderError(
                f"provision: {machine.server_id} exists in state {machine.state}; "
                "destroy it or inspect it instead of ordering a second machine"
            )
        seed = _bind_seed_token(ctx, spec.hostname, machine.server_id,
                                backup_password=backup_password)
        machine = ctx.provider.poll(machine.server_id, timeout=args.timeout, interval=args.interval)
        created = False
    else:
        machine = ctx.provider.create(spec)
        # The order's id is in the create response and the machine is still
        # deploying, so the token exists before the guest can boot (ODL-455).
        seed = _bind_seed_token(ctx, spec.hostname, machine.server_id,
                                backup_password=backup_password)
        if _is_queued(machine):
            return _queued_report(ctx, machine, created=True, release=release, seed=seed)
        # `create` may answer active before the address is filled in; the poll
        # is what guarantees the tenant actually has somewhere to connect.
        if machine.state != "active" or not machine.ipv4:
            machine = ctx.provider.poll(
                machine.server_id, timeout=args.timeout, interval=args.interval
            )
        created = True

    record = _record(ctx, spec, machine, release)
    lines = [
        f"  server_id: {machine.server_id}",
        f"  hostname:  {machine.hostname}",
        f"  state:     {machine.state} ({machine.raw_status or 'no raw status'})",
        f"  ipv4:      {machine.ipv4 or '-'}",
        f"  expires:   {render_datetime(machine.expires_at)}",
        f"  release:   {record['release']} (from the pin, recorded with the tenant)",
        *_seed_lines(seed),
    ]
    return applied(
        NAME,
        ctx,
        lines,
        machine=machine.to_dict(),
        created=created,
        recorded=record["recorded"],
        release=record["release"],
        tenant_state=record["tenant_state"],
        seed=seed.to_dict(),
    )


def _is_queued(machine: Machine) -> bool:
    """A paid preorder the vendor will build when the queue clears."""
    return (machine.raw_status or "").strip().lower() == "queued"


def _queued_report(
    ctx: Context, machine: Machine, *, created: bool, release: str, seed: SeedBinding
) -> Report:
    """A queued preorder is a success: the money left, the order exists."""
    position = machine.detail.get("queue_position")
    lines = [
        f"  server_id: {machine.server_id}",
        f"  hostname:  {machine.hostname}",
        f"  state:     {machine.state} (queued)",
        f"  queue:     {position if position is not None else 'position not reported'}",
        f"  release:   {release} (from the pin; recorded when the machine is active)",
        "  the order is paid; the provider builds the machine when the queue clears",
        *_seed_lines(seed),
    ]
    head = (
        "provision: order queued (paid; the machine is built when the queue clears)"
        if created
        else "provision: nothing ordered (the order is already queued)"
    )
    return applied(
        NAME,
        ctx,
        lines,
        head=head,
        machine=machine.to_dict(),
        created=created,
        queued=True,
        queue_position=position,
        recorded=False,
        release=release,
        note="the release is recorded when the machine becomes active",
        seed=seed.to_dict(),
    )


def _placeholder_key() -> str:
    """A syntactically valid key for a dry run with no `--key`.

    It never reaches a provider: the dry-run path validates the slug and the
    plan and prints the plan. Keeping the placeholder here means `CreateSpec`
    can stay strict about what a real key looks like.
    """
    return (
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICFh7hQkST5vRDolO3yqwCARRugZahMdc+WgR69JsoXG "
        "dry-run@odile"
    )
