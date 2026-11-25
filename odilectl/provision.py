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
`docs/runbooks/provision.md` (16:01:30Z) got `server_id=0badc0de
status=deploying` in 0.232 s and `active` 120.7 s later. `_bind_seed_token`
writes one single-use token to the seed ledger, bound to that id, and only its
SHA-256 hash on the tenant row, before the machine is polled — long before it
can boot and ask for a seed. The plaintext is printed once. A second run ends
the previous token and mints a new one (the manual recovery path: rerun and
print the fresh token). If the slug has no tenant row yet, the claim worker
mints the token when the claim exists (ODL-220) — the fallback, and the report
names it instead of inventing a tenant row.

**A destroyed tenant is reopened, not replaced (ODL-218 follow-up, ADR-007).**
`destroyed -> claimed` is the one edge back: `_reopen_destroyed` moves the row
before the first provider call, so the order always lands on a row that can
record it. The slug — and therefore `https://<slug>.odile.fyi`, the recovery
file's name and the `tenants/<slug>` repository — does not change; the reopen
is one `claimed` event whose detail carries the reason, and the dead machine's
columns are cleared. Running the command on a live tenant whose machine is
still listed does nothing extra.

**A live row whose machine is gone is refused, not re-ordered (this card).** A
row in `MACHINE_STATES` already names a machine, and `TRANSITIONS` has no edge
from any of those states back to `provisioning`. So `_refuse_an_unrecordable_order`
stops the run *before* `provider.create` when the provider lists no machine for
the slug, and the message names the way back: `odilectl reconcile <slug>
--apply` marks the row `destroyed`, and the reopen above orders onto it. This is
the bug the card measured: `provision` on a `ready` tenant with an empty
provider list billed a machine, `_record_machine` skipped the web row (the new
id was not the row's), and both rows kept naming the dead one — one paid
machine no row named, and a first boot that could never land. The alternative —
recording the new machine through a `ready -> provisioning` edge, the way a
reopen records one — was rejected: it adds a second door into `provisioning`
(or six, one per machine state), gives the state machine two ways to say "the
machine is gone", and lets a provider list that is down or short rewind a live
tenant whose machine is fine. `reconcile` owns that judgement because it owns
the comparison; `provision` only knows what `provider.list` answered. The
refusal is still recorded on a live tenant's own machine: a reused provider row
whose `server_id` is the row's is recorded without an order (`web_row:
unchanged`, or `absent` on a state-only database).

**The order is recorded in both schemas.** A `provision` that has placed the
order names the machine on the state row *and* on the web `tenants` row, and
moves the spine to `first_boot`: `record_boot` reads `tenants.server_id` and
refuses a `claimed`/`provisioning` row, so without both writes the machine's
own first boot cannot land — on a rebuild it would still be checked against the
dead machine's id. The web row is pointed at the new `server_id`, address,
expiry and ordered release, with `state='provisioned'` until the boot record
moves it on; a row that already names the machine is not rewritten.

**A rebuild keeps the repository (ODL-215 follow-up).** The user's old recovery
file already opens the repository; a new machine that mints a new password
meets that repository with a key it cannot use. With `--backup-password-file`
the run reads the `Backup password:` line of the user's file and seals it under
the token it just minted (`seed.seal_backup_password`), storing only ciphertext
in `/var/lib/odile/seed-rebuilds.json`, one entry per slug: the guest opens it
with the token from the measured command line, which HQ prints once and keeps
as a hash. A run *without* the flag removes the slug's entry, so a token never
carries a stale secret.

**The backup pair is provisioned with the tenant, before the paid order.** One
`odile-<slug>-rw`/`-ro` sub-account pair on the BX11 (ADR-004 v3 §4.1/§4.2), the
read-only home nested under the read-write one, each account created, its
settings corrected (the create call drops them), its password reset once and
used once to install a generated key, read back, and only then locked read-only.
The store of record is `/root/.odile/hetzner/odile-<slug>.env` (0600) and the
seed service's per-exchange copy is `/var/lib/odile/seed-backup-pairs.json`;
the row's `detail` is the cache. A pair that is already there is read back and
printed `same` (the drill's bench pair is adopted, never re-created), and the
51st pair is refused by name before any create — 100 sub-accounts per box, a
pair per tenant.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import secrets
import socket
import sqlite3
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from argparse import ArgumentParser
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from . import keys
from .config import Config
from .core import Context, Report, applied, plan, render_datetime
from .errors import ConfigError, ProviderError, UsageError
from .providers import configured_name
from .providers.base import CreateSpec, Machine, Provider
from .state import (
    DEFAULT_STATE_DB,
    MACHINE_STATES,
    RELEASE_ID_RE,
    StateError,
    StateStore,
    Tenant,
    UnknownTenant,
)

NAME = "provision"
HELP = "Order the tenant's machine and wait until it is active."
EXAMPLE = "odilectl provision odl-7f3a2c --key ~/.ssh/ada.pub --apply"

DEFAULT_TIMEOUT = 900.0
DEFAULT_INTERVAL = 10.0
DEFAULT_PIN = Path(__file__).resolve().parent.parent / "odile-verify.pin"
SETUP_TOKEN_TTL_HOURS = 24
# Written to the `claimed` event when provision brings a destroyed tenant back
# (ADR-007). The slug, and therefore the URL, does not change.
REPROVISION_REASON = (
    "odilectl provision: a destroyed machine is replaced under the same slug"
)


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


@dataclass(frozen=True)
class KeyBinding:
    """The instance's inference key, or why the seed carries the shared one.

    `state` is one of:

    * ``issued``  — minted by this run, recorded with the label and first
      characters, and waiting in the seed handoff for this token;
    * ``reissued`` — a second run on a live machine whose raw key had already
      left HQ: a new key was minted (the old one revoked at the provider), and
      the report says the machine must be installed again with the new token;
    * ``kept``    — the tenant already had a live key and the raw key still
      waits, so it was re-bound to this run's token and nothing was minted;
    * ``shared``  — no ``near_account`` in the config: alpha.0's manual path,
      where the seed carries the config's shared key.
    """

    state: str
    label: str | None = None
    prefix: str | None = None
    key_id: str | None = None
    spend_limit_nano: int | None = None
    workspace_id: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "label": self.label,
            "key_prefix": self.prefix,
            "key_id": self.key_id,
            "spend_limit_nano": self.spend_limit_nano,
            "workspace": self.workspace_id,
            "reason": self.reason,
        }


def _state_path(ctx: Context) -> Path:
    return ctx.config.path_value("state_db", DEFAULT_STATE_DB) or Path(DEFAULT_STATE_DB)


def _seed_keys_path(ctx: Context, seed) -> str:
    """The issued-key handoff the seed service reads per exchange."""
    configured = ctx.config.path_value("seed_keys")
    return str(configured) if configured is not None else seed.keys_path()


def _issue_key(ctx: Context, slug: str, *, keep: bool,
               ) -> tuple[KeyBinding, "keys.Issued | None", dict | None]:
    """Mint this instance's key before the machine is ordered, or reissue the live one.

    The mint happens *before* `provider.create` so a provider refusal leaves no
    paid machine without a key: the order is the expensive half. `keep` is the
    reuse path — a second run on a tenant whose machine is already up must not
    revoke the key that machine is spending with. The raw key decides what that
    means (verifier round 4, ODL-242):

    * it still waits in the handoff -> the same key is re-bound to the token
      this run mints, so a re-issued token seeds the instance's own key;
    * it has already left HQ (the machine exchanged it) -> the record's raw key
      is unrecoverable, so this run mints a new key. `keys.issue` revokes the
      live one at the provider first, and the report says the running machine
      must be installed again to pick the new key up. The config's shared key is
      never substituted for a tenant that has a record.

    The record (label, first characters, id, cap) is written to the state
    database here; the raw key goes to the seed handoff, never to the record.
    Returns the raw `Issued` (None on the keep path) and the waiting entry, so
    `_bind_seed_token`'s caller can bind whichever one this run has.
    """
    account = keys.account_from_config(ctx.config)
    store = StateStore.open(_state_path(ctx))
    replaced = None
    try:
        record = store.key_for(slug)
        if account is None:
            # No `near_account` is the bench/manual switch, but it cannot undo a
            # key a tenant already owns: `seed.bind` refuses to mint a token that
            # would seed the shared key (ODL-242), so refuse here instead — before
            # the order, which is the expensive half.
            if record is not None and record.state == "issued":
                raise ProviderError(
                    f"provision: {slug} owns the NEAR AI key {record.key_prefix} "
                    f"({record.label}), but this config has no `near_account`, so "
                    "this run could neither re-bind nor replace it; restore the "
                    "switch or destroy the tenant first"
                )
            return KeyBinding(
                state="shared",
                reason="no near_account in the config: the seed carries the config's shared key",
            ), None, None
        if keep and record is not None and record.state == "issued":
            waiting = _waiting_entry(ctx, slug, record.key_id)
            if waiting is not None:
                return KeyBinding(
                    state="kept",
                    label=record.label,
                    prefix=record.key_prefix,
                    key_id=record.key_id,
                    spend_limit_nano=record.spend_limit_nano,
                    workspace_id=record.workspace_id,
                    reason="the waiting raw key is re-bound to this run's setup token",
                ), None, waiting
            replaced = record
        try:
            issued = keys.issue(account, slug)
        except ProviderError as error:
            raise ProviderError(
                f"provision: {slug}'s inference key was not minted, so nothing was "
                f"ordered: {error}"
            ) from None
        detail = {"source": "odilectl provision", "by": "the provider's mint call"}
        if replaced is not None:
            detail.update({
                "reason": "reissued: the live machine's raw key had already left HQ",
                "replaces": replaced.key_id,
            })
        store.record_key(
            slug,
            label=issued.label,
            key_id=issued.key_id,
            key_prefix=issued.key_prefix,
            workspace_id=issued.workspace_id,
            spend_limit_nano=issued.spend_limit_nano,
            detail=detail,
        )
    finally:
        store.close()
    seed = _seed_service()
    _write_waiting_key(ctx, seed, slug, issued, token_hash=None, instance_id=None)
    return KeyBinding(
        state="reissued" if replaced is not None else "issued",
        label=issued.label,
        prefix=issued.key_prefix,
        key_id=issued.key_id,
        spend_limit_nano=issued.spend_limit_nano,
        workspace_id=issued.workspace_id,
        reason=(
            f"it replaces {replaced.key_prefix}, which was revoked at the provider; "
            "the live machine must be installed again with the new setup token to "
            "pick the new key up"
            if replaced is not None else ""
        ),
    ), issued, None


def _waiting_entry(ctx: Context, slug: str, key_id: str) -> dict | None:
    """The raw key waiting for this tenant and this record, or None.

    Not matched to a token hash on purpose: this is the path where the entry is
    about to be *re-bound* to the token this run mints. The key id must match
    the record, so a leftover entry for an earlier, superseded mint is not
    handed over.
    """
    seed = _seed_service()
    path = _seed_keys_path(ctx, seed)
    try:
        document = seed.load_keys(path)
    except (seed.ConfigError, OSError) as error:
        raise ProviderError(f"provision: cannot read the seed handoff at {path}: {error}") from None
    entry = document.get(slug)
    if not isinstance(entry, dict) or entry.get("key_id") != key_id:
        return None
    return entry


def _write_waiting_key(ctx: Context, seed, slug: str, issued, *,
                       token_hash: str | None, instance_id: str | None) -> None:
    """Put the raw key where the next redeem for this token will find it.

    Written twice on purpose: once right after the mint (so a machine that is
    already ordered can be seeded even if the bookkeeping below fails), and
    again once the token exists, carrying its hash and the instance id. The
    second write is what binds the key to *this* token.
    """
    path = _seed_keys_path(ctx, seed)
    entry = {
        "key": issued.raw_key,
        "label": issued.label,
        "key_id": issued.key_id,
        "prefix": issued.key_prefix,
        "workspace_id": issued.workspace_id,
        "token_hash": token_hash,
        "instance_id": instance_id,
        "created_at": seed.iso(seed.utcnow()),
    }
    try:
        seed.set_issued_key(path, slug, entry)
    except (seed.ConfigError, OSError, ValueError) as error:
        raise ProviderError(
            f"provision: {slug}'s key was minted but not written to the seed handoff "
            f"at {path}: {error}"
        ) from None


def _bind_key_to_token(ctx: Context, slug: str, issued: "keys.Issued",
                       token_hash: str, instance_id: str) -> None:
    """Bind the waiting key to the token and the instance that will redeem it."""
    seed = _seed_service()
    _write_waiting_key(ctx, seed, slug, issued, token_hash=token_hash, instance_id=instance_id)
    store = StateStore.open(_state_path(ctx))
    try:
        store.record_key(
            slug,
            label=issued.label,
            key_id=issued.key_id,
            key_prefix=issued.key_prefix,
            workspace_id=issued.workspace_id,
            instance_id=instance_id,
            spend_limit_nano=issued.spend_limit_nano,
        )
    finally:
        store.close()


def _rebind_waiting_key(ctx: Context, slug: str, binding: KeyBinding, waiting: dict,
                        token_hash: str, instance_id: str) -> None:
    """Point the kept raw key at the token this run minted, and at this instance.

    A re-issued setup token must seed the instance's own key (verifier round 4,
    ODL-242): `seed.bind` ends the previous token in the same transaction, so
    exactly one token is live and this entry must name it. The raw key is the
    same one the record already names; only its binding moves.
    """
    seed = _seed_service()
    path = _seed_keys_path(ctx, seed)
    updated = dict(waiting, token_hash=token_hash, instance_id=instance_id)
    try:
        seed.set_issued_key(path, slug, updated)
    except (seed.ConfigError, OSError, ValueError) as error:
        raise ProviderError(
            f"provision: {slug}'s setup token was minted but its waiting key was not "
            f"re-bound at {path}: {error}"
        ) from None
    store = StateStore.open(_state_path(ctx))
    try:
        store.record_key(
            slug,
            label=str(binding.label),
            key_id=str(binding.key_id),
            key_prefix=str(binding.prefix),
            workspace_id=str(binding.workspace_id),
            instance_id=instance_id,
            spend_limit_nano=binding.spend_limit_nano,
        )
    finally:
        store.close()


def _key_lines(binding: KeyBinding) -> list[str]:
    if binding.state == "shared":
        return [f"  inference: shared key ({binding.reason})"]
    if binding.state == "kept":
        return [
            f"  inference: {binding.label} {binding.prefix} kept "
            "(the live machine's key; nothing was minted)",
            "  inference: the waiting raw key was re-bound to this run's setup token, "
            "so the re-issued token seeds this instance's own key",
        ]
    lines = [
        f"  inference: {binding.label} {binding.prefix} "
        f"(cap {keys.money(binding.spend_limit_nano)}; the provider's spendLimit)",
    ]
    if binding.reason:
        lines.append(f"  inference: {binding.reason}")
    lines.append(
        "  inference: the raw key is in the seed handoff for this token, and the "
        "record holds only the label and first characters"
    )
    return lines


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
            con, slug, instance_id=instance_id, ttl_hours=SETUP_TOKEN_TTL_HOURS,
            keys_file=_seed_keys_path(ctx, seed),
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

    Returns "sealed" or "none" for the report.
    `/var/lib/odile/seed-rebuilds.json` holds the ciphertext, never the
    password; a stale entry cannot be opened by a later token.
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


# ------------------------------------------------- the tenant's backup pair
#
# ADR-004 v3 §4.1/§4.2: one sub-account pair per tenant on the BX11. The
# read-write home is `tenants/<slug>`; the read-only home is
# `tenants/<slug>/data`, a subdirectory of it that holds the repository — never
# the same home, because two accounts on one home share one
# `.ssh/authorized_keys` (the drill measured the read-only key authenticating
# as read-write). The provider assigns the logins (`u<box>-sub<N>`), so nothing
# here derives one from the slug: every login in the store of record came back
# from the API.
#
# The order is the measured one, not the documented one: `POST /subaccounts`
# silently drops `access_settings`, so every create is followed by
# `update_access_settings` and a readback; a public key installs only while the
# home is writable, so both keys go in before the read-only account is locked;
# and the create-time password may not be live, so it is reset once, used once
# to install the key, and reset again to a value that is never read back.
HETZNER_API_BASE = "https://api.hetzner.com/v1"
HETZNER_BOX_ID = 709870
HETZNER_TOKEN_FILE = "/root/.odile/hetzner-api-token"
HETZNER_STORE_DIR = "/root/.odile/hetzner"
HETZNER_KEYS_DIR = "/root/.odile/keys"
# The per-exchange file the seed service reads (ODL-924); the seed config is
# handed to the service at start, so a rewrite would be invisible until restart.
SEED_BACKUP_PAIRS = "/var/lib/odile/seed-backup-pairs.json"
# 100 sub-accounts per box, a pair per tenant: the 51st tenant is a second box.
MAX_BACKUP_TENANTS = 50
BACKUP_SFTP_PORT = 22
KEY_INSTALL_PORT = 23
# The path the repository lives at, relative to each account's home: the
# read-write account sees `data/restic`, the read-only account `restic`,
# because its home is the `data` directory.
BACKUP_REPO_PATH = "data/restic"
BACKUP_READONLY_REPO_PATH = "restic"
# Hetzner publishes the only fingerprints its Storage Box hosts present
# (`docs/hetzner.com/storage/storage-box/general`, read 2026-11-24). A scanned
# host key outside this set is a mismatch, not a new pin.
HETZNER_HOST_FINGERPRINTS = frozenset({
    "SHA256:XqONwb1S0zuj5A1CDxpOSuD2hnAArV1A3wKY7Z3sdgM",   # ED25519
    "SHA256:EMlfI8GsRIfpVkoW1H2u0zYVpFGKkIMKHFZIRkf2ioI",   # RSA
    "SHA256:RWkLouD9tfTwdboJOzjiWo5njZI59Hcta82ttAWxDA0",   # DSA
    "SHA256:oDHZqKXnoMtgvPBjjC57pcuFez28roaEuFcfwyg8O5c",   # ECDSA
})
SLEEP = time.sleep


@dataclass(frozen=True)
class BackupSettings:
    """Where the pair lives: the API, the store of record, the seed handoff."""

    base: str
    box_id: int
    token: Path
    store: Path
    keys: Path
    pairs: Path


@dataclass(frozen=True)
class BackupPair:
    """One tenant's pair, exactly as the provider and the files record it."""

    slug: str
    box_id: int
    box_server: str
    subaccount_id: int
    subaccount_username: str
    subaccount_server: str
    subaccount_home: str
    readonly_subaccount_id: int
    readonly_subaccount_username: str
    readonly_subaccount_server: str
    readonly_subaccount_home: str
    repo_url: str
    readonly_repo_url: str
    known_hosts: str
    rw_private_key: str
    ro_private_key: str
    previous_instance: str
    provisioned_at: str

    def to_entry(self) -> dict:
        """The seed handoff's shape (and the store of record's, minus keys)."""
        return {
            "box_id": self.box_id,
            "box_server": self.box_server,
            "subaccount_id": self.subaccount_id,
            "subaccount_username": self.subaccount_username,
            "subaccount_home": self.subaccount_home,
            "readonly_subaccount_id": self.readonly_subaccount_id,
            "readonly_subaccount_username": self.readonly_subaccount_username,
            "readonly_subaccount_home": self.readonly_subaccount_home,
            "repo_url": self.repo_url,
            "readonly_repo_url": self.readonly_repo_url,
            "ssh_port": BACKUP_SFTP_PORT,
            "known_hosts": self.known_hosts,
            "ssh_private_key": self.rw_private_key,
            "readonly_ssh_private_key": self.ro_private_key,
            "previous_instance": self.previous_instance,
            "provisioned_at": self.provisioned_at,
        }

    def to_env(self) -> bytes:
        """The store of record: one flat KEY=VALUE line per fact, keys at 0600.

        Both private keys are here because the store of record is what `reopen`
        re-reads: a fresh tenant's recovery file is rendered on the guest from
        the seed, so the read-only half has to travel there once, and a rebuild
        must be able to re-render it without the user's paste.
        """
        rows = [
            ("BACKUP_SLUG", self.slug),
            ("BACKUP_BOX_ID", str(self.box_id)),
            ("BACKUP_BOX_SERVER", self.box_server),
            ("BACKUP_SUBACCOUNT_ID", str(self.subaccount_id)),
            ("BACKUP_SUBACCOUNT_USERNAME", self.subaccount_username),
            ("BACKUP_SUBACCOUNT_SERVER", self.subaccount_server),
            ("BACKUP_SUBACCOUNT_HOME", self.subaccount_home),
            ("BACKUP_READONLY_SUBACCOUNT_ID", str(self.readonly_subaccount_id)),
            ("BACKUP_READONLY_SUBACCOUNT_USERNAME", self.readonly_subaccount_username),
            ("BACKUP_READONLY_SUBACCOUNT_SERVER", self.readonly_subaccount_server),
            ("BACKUP_READONLY_SUBACCOUNT_HOME", self.readonly_subaccount_home),
            ("BACKUP_REPOSITORY", self.repo_url),
            ("BACKUP_READONLY_REPOSITORY", self.readonly_repo_url),
            ("BACKUP_SSH_PORT", str(BACKUP_SFTP_PORT)),
            ("BACKUP_KNOWN_HOSTS", base64.b64encode(
                self.known_hosts.encode("utf-8")).decode("ascii")),
            ("BACKUP_RW_PRIVATE_KEY", base64.b64encode(
                self.rw_private_key.encode("utf-8")).decode("ascii")),
            ("BACKUP_RO_PRIVATE_KEY", base64.b64encode(
                self.ro_private_key.encode("utf-8")).decode("ascii")),
            ("BACKUP_PREVIOUS_INSTANCE", self.previous_instance),
            ("BACKUP_PROVISIONED_AT", self.provisioned_at),
        ]
        return "".join("%s=%s\n" % (key, value) for key, value in rows).encode("utf-8")

    @classmethod
    def from_env(cls, text: str) -> "BackupPair":
        values: dict[str, str] = {}
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator:
                raise ProviderError(f"provision: the store of record has a line without '=': {key!r}")
            values[key.strip()] = value
        try:
            return cls(
                slug=values["BACKUP_SLUG"],
                box_id=int(values["BACKUP_BOX_ID"]),
                box_server=values["BACKUP_BOX_SERVER"],
                subaccount_id=int(values["BACKUP_SUBACCOUNT_ID"]),
                subaccount_username=values["BACKUP_SUBACCOUNT_USERNAME"],
                subaccount_server=values["BACKUP_SUBACCOUNT_SERVER"],
                subaccount_home=values["BACKUP_SUBACCOUNT_HOME"],
                readonly_subaccount_id=int(values["BACKUP_READONLY_SUBACCOUNT_ID"]),
                readonly_subaccount_username=values["BACKUP_READONLY_SUBACCOUNT_USERNAME"],
                readonly_subaccount_server=values["BACKUP_READONLY_SUBACCOUNT_SERVER"],
                readonly_subaccount_home=values["BACKUP_READONLY_SUBACCOUNT_HOME"],
                repo_url=values["BACKUP_REPOSITORY"],
                readonly_repo_url=values["BACKUP_READONLY_REPOSITORY"],
                known_hosts=base64.b64decode(values["BACKUP_KNOWN_HOSTS"]).decode("utf-8"),
                rw_private_key=base64.b64decode(values["BACKUP_RW_PRIVATE_KEY"]).decode("utf-8"),
                ro_private_key=base64.b64decode(values["BACKUP_RO_PRIVATE_KEY"]).decode("utf-8"),
                previous_instance=values["BACKUP_PREVIOUS_INSTANCE"],
                provisioned_at=values["BACKUP_PROVISIONED_AT"],
            )
        except (KeyError, ValueError) as error:
            raise ProviderError(
                f"provision: the store of record is not a pair file: {error}") from None


@dataclass(frozen=True)
class BackupResult:
    """What a provision did about the pair: created, adopted or already there."""

    state: str  # "created" | "adopted" | "same"
    pair: BackupPair
    resets: int
    row: str

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "slug": self.pair.slug,
            "box_id": self.pair.box_id,
            "subaccount_id": self.pair.subaccount_id,
            "subaccount_username": self.pair.subaccount_username,
            "readonly_subaccount_id": self.pair.readonly_subaccount_id,
            "readonly_subaccount_username": self.pair.readonly_subaccount_username,
            "repository": self.pair.repo_url,
            "previous_instance": self.pair.previous_instance,
            "password_resets": self.resets,
            "row": self.row,
        }


def _backup_settings(ctx: Context) -> BackupSettings:
    configured_box = ctx.config.get("hetzner_box_id")
    try:
        box_id = int(configured_box) if configured_box else HETZNER_BOX_ID
    except (TypeError, ValueError):
        raise ConfigError(
            f"provision: hetzner_box_id is {configured_box!r}; it must be an integer"
        ) from None
    return BackupSettings(
        base=(ctx.config.get("hetzner_api_base") or HETZNER_API_BASE).rstrip("/"),
        box_id=box_id,
        token=(ctx.config.path_value("hetzner_token", HETZNER_TOKEN_FILE)
               or Path(HETZNER_TOKEN_FILE)),
        store=(ctx.config.path_value("hetzner_dir", HETZNER_STORE_DIR)
               or Path(HETZNER_STORE_DIR)),
        keys=(ctx.config.path_value("hetzner_keys", HETZNER_KEYS_DIR)
              or Path(HETZNER_KEYS_DIR)),
        pairs=(ctx.config.path_value("seed_backup_pairs", SEED_BACKUP_PAIRS)
               or Path(SEED_BACKUP_PAIRS)),
    )


def _redact(body: str) -> str:
    """A provider error body without a password value in it."""
    return re.sub(r'"password"\s*:\s*"[^"]*"', '"password": "***"', body)


def _hetzner_api(settings: BackupSettings, method: str, path: str,
                 body: dict | None = None) -> tuple[int, dict]:
    """One Box API call, stdlib only; the token comes from a 0600 file.

    Returns `(status, payload)` for every status; a 4xx/5xx raises with the
    provider's own message (password redacted) so a refusal names the reason.
    """
    try:
        token = settings.token.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ConfigError(
            f"provision: cannot read the box API token at {settings.token}: {error}"
        ) from None
    if token.startswith("HETZNER_API_TOKEN="):
        token = token.split("=", 1)[1].strip()
    data = None
    if body is not None:
        data = json.dumps(body, sort_keys=True).encode("utf-8")
    request = urllib.request.Request(settings.base + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except urllib.error.URLError as error:
        raise ProviderError(
            f"provision: the box API at {settings.base} did not answer: {error.reason}"
        ) from None
    text = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(text) if text.strip() else {}
    except ValueError:
        payload = {"raw": text}
    if status >= 400:
        error = payload.get("error") if isinstance(payload, dict) else None
        message = error.get("message") if isinstance(error, dict) else _redact(text[:400])
        raise ProviderError(
            f"provision: the box API answered {status} on {method} {path}: {message}"
        )
    return status, payload


def _subaccounts(settings: BackupSettings) -> list[dict]:
    """Every sub-account on the box, read once per run."""
    _status, payload = _hetzner_api(
        settings, "GET", f"/storage_boxes/{settings.box_id}/subaccounts")
    found = payload.get("subaccounts")
    if not isinstance(found, list):
        raise ProviderError(
            "provision: the box API answered without a `subaccounts` list; "
            "refusing to guess what the box holds")
    return found


def _named_pair(subaccounts: list[dict], slug: str) -> tuple[dict, dict] | None:
    """The pair whose API `name` is `odile-<slug>-rw` / `-ro`, or None."""
    found: dict[str, list[dict]] = {}
    for wanted in ("rw", "ro"):
        name = f"odile-{slug}-{wanted}"
        matches = [item for item in subaccounts if item.get("name") == name]
        if len(matches) > 1:
            raise ProviderError(
                f"provision: {len(matches)} sub-accounts are named {name}; refusing to guess")
        if matches:
            found[wanted] = matches
    if not found:
        return None
    if len(found) != 2:
        half = "read-write" if "rw" in found else "read-only"
        raise ProviderError(
            f"provision: the box holds only the {half} half of {slug}'s pair "
            f"(odile-{slug}-{'rw' if 'rw' in found else 'ro'}). A pair is both "
            "accounts; delete the half and provision again")
    return found["rw"][0], found["ro"][0]


def _pair_count(subaccounts: list[dict]) -> int:
    """Complete `-rw`/`-ro` pairs on the box, counted by name."""
    names = {item.get("name") for item in subaccounts}
    return sum(1 for name in names
               if isinstance(name, str) and name.endswith("-rw")
               and name[:-3] + "-ro" in names)


def _stored_pair_count(settings: BackupSettings) -> int:
    """The pairs this HQ has a store of record for (no provider call)."""
    try:
        return sum(1 for path in settings.store.glob("odile-*.env") if path.is_file())
    except OSError:
        return 0


def _refuse_the_51st(settings: BackupSettings, slug: str, api_pairs: int | None) -> None:
    """Refuse the 51st pair by name, before any create.

    100 sub-accounts per box, a pair per tenant: 50 tenants, and the 51st is a
    second box, not an upgrade (the feature table lists 100 sub-accounts and 10
    connections for every BX tier). The count is the store of record's when no
    API read has happened yet, and the box's own when one has.
    """
    counted = max(_stored_pair_count(settings), api_pairs or 0)
    if counted < MAX_BACKUP_TENANTS:
        return
    raise ProviderError(
        f"provision: refusing the 51st pair — {settings.box_id} records {counted} "
        f"tenant pairs (100 sub-accounts, a pair per tenant), and {slug} would be "
        "number %d. A second box is the next purchase, not an upgrade."
        % (counted + 1))


def _password() -> str:
    """A sub-account password: used once, then replaced and forgotten.

    The provider refuses a password without one upper-case letter, one
    lower-case letter, one digit and one special character, so the four are
    guaranteed before the rest is filled in.
    """
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    special = "!@#%^*_-=+"
    alphabet = upper + lower + digits + special
    characters = [secrets.choice(upper), secrets.choice(lower),
                  secrets.choice(digits), secrets.choice(special)]
    characters.extend(secrets.choice(alphabet) for _ in range(36))
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)


def _await_action(settings: BackupSettings, payload: dict, *,
                  attempts: int = 90, delay: float = 2.0) -> None:
    """Wait for the action a write answered, by its own id.

    Every sub-account write answers an action that is still `running`: the
    create, the settings update and the password reset. Acting on the next step
    before it finishes is what produced `404 Sub Account … not found` and
    `Permission denied` on 2026-11-25 — the id and the password exist, the
    action has not landed. The poll endpoint is
    `GET /storage_boxes/{id}/actions/{action_id}`.
    """
    action = payload.get("action") or {}
    ident = action.get("id")
    if not isinstance(ident, int):
        return
    if action.get("status") == "success":
        return
    for attempt in range(attempts):
        _status, current = _hetzner_api(
            settings, "GET", f"/storage_boxes/{settings.box_id}/actions/{ident}")
        done = current.get("action") or {}
        if done.get("status") == "success":
            return
        if done.get("status") == "error":
            raise ProviderError(
                f"provision: the box's {done.get('command')} action {ident} failed: "
                f"{done.get('error')}")
        if attempt + 1 < attempts:
            SLEEP(delay)
    raise ProviderError(
        f"provision: the box's action {ident} did not finish in "
        f"{attempts * delay:.0f}s")


def _create_subaccount(settings: BackupSettings, name: str, home: str,
                       password: str) -> int:
    """`POST /subaccounts`; returns the id the provider assigned.

    The body carries the settings the create call ignores, so the request says
    what the pair wants and the follow-up call is what makes it true.
    """
    status, payload = _hetzner_api(
        settings, "POST", f"/storage_boxes/{settings.box_id}/subaccounts",
        {"name": name, "home_directory": home, "password": password,
         "readonly": False, "ssh_enabled": True, "reachable_externally": True})
    ids = (payload.get("subaccount") or {}).get("id")
    if not isinstance(ids, int):
        raise ProviderError(
            f"provision: the create of {name} answered {status} without a sub-account id")
    _await_action(settings, payload)
    return ids


def _api_when_ready(settings: BackupSettings, method: str, path: str,
                    body: dict | None = None, attempts: int = 20,
                    delay: float = 2.0) -> tuple[int, dict]:
    """One call, retried while the provider says the new id does not exist.

    A create answers before the sub-account is visible to the follow-up call:
    measured on 2026-11-25, the `update_access_settings` right after a create
    answered `404 Sub Account with ID … not found` and then succeeded. Only a
    404 is retried; every other refusal is the provider's answer.
    """
    for attempt in range(attempts):
        try:
            return _hetzner_api(settings, method, path, body)
        except ProviderError as error:
            if "answered 404" not in str(error) or attempt + 1 >= attempts:
                raise
            SLEEP(delay)
    raise AssertionError("unreachable")


def _update_access_settings(settings: BackupSettings, subaccount_id: int, *,
                            readonly: bool) -> None:
    _status, payload = _api_when_ready(
        settings, "POST",
        f"/storage_boxes/{settings.box_id}/subaccounts/{subaccount_id}"
        "/actions/update_access_settings",
        {"readonly": readonly, "ssh_enabled": True, "reachable_externally": True})
    _await_action(settings, payload)


def _reset_password(settings: BackupSettings, subaccount_id: int) -> str:
    """Set a password explicitly, because the create-time one may not be live."""
    password = _password()
    _status, payload = _api_when_ready(
        settings, "POST",
        f"/storage_boxes/{settings.box_id}/subaccounts/{subaccount_id}"
        "/actions/reset_subaccount_password",
        {"password": password})
    _await_action(settings, payload)
    return password


def _readback(settings: BackupSettings, subaccount_id: int, *, readonly: bool,
              attempts: int = 15) -> dict:
    """Read the sub-account back until the settings it must carry are there.

    `update_access_settings` answers an action that is still running, so the
    readback is the proof, not the POST. A pair that never converges is a loud
    failure, never a silent one: the key install and the `readonly` lock order
    both depend on this state.
    """
    settings_now: dict = {}
    for attempt in range(attempts):
        _status, payload = _api_when_ready(
            settings, "GET",
            f"/storage_boxes/{settings.box_id}/subaccounts/{subaccount_id}",
            attempts=3)
        subaccount = payload.get("subaccount") or {}
        settings_now = subaccount.get("access_settings") or {}
        if (settings_now.get("readonly") == readonly
                and settings_now.get("ssh_enabled") is True
                and settings_now.get("reachable_externally") is True):
            return subaccount
        if attempt + 1 < attempts:
            SLEEP(2)
    raise ProviderError(
        f"provision: sub-account {subaccount_id} did not read back with "
        f"readonly={readonly}, ssh_enabled=true, reachable_externally=true "
        f"(saw {settings_now!r})")

def _generate_keypair(directory: Path, name: str, comment: str) -> tuple[str, str]:
    """One ed25519 pair on HQ: `(private PEM, public OpenSSH line)` at 0600/0644."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    private = directory / name
    if private.exists():
        private.unlink()
    done = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(private)],
        capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise ProviderError(
            f"provision: ssh-keygen for {name} failed: {(done.stderr or done.stdout).strip()}")
    os.chmod(private, 0o600)
    public = Path(str(private) + ".pub")
    os.chmod(public, 0o644)
    return private.read_text(encoding="utf-8"), public.read_text(encoding="utf-8").strip()


def _wait_for_dns(server: str, *, attempts: int = 36, delay: float = 5.0) -> None:
    """Wait until the provider's new sub-account hostname resolves.

    Measured 2026-11-25: a sub-account created seconds ago has an id, a login
    and a home, but its `u<box>-sub<N>.your-storagebox.de` name is not in DNS
    yet — `ssh-keyscan` answers `getaddrinfo … Name or service not known` for
    minutes. The wait is bounded and names the host when it gives up.
    """
    for attempt in range(attempts):
        try:
            socket.getaddrinfo(server, BACKUP_SFTP_PORT)
            return
        except socket.gaierror:
            if attempt + 1 < attempts:
                SLEEP(delay)
    raise ProviderError(
        f"provision: {server} did not resolve after {attempts * delay:.0f}s; the "
        "provider's new sub-account hostname is not in DNS yet")


def _scan_host_key(server: str, known_hosts: Path) -> str:
    """Scan the host's keys on both ports, accept only published fingerprints.

    Hetzner publishes the fingerprints its Storage Box hosts present; anything
    else is a mismatch and stops the run. Both ports are scanned because the
    key install runs on port 23 (`[host]:23` in `known_hosts`) while the steady
    state is port 22; the accepted lines are written once at 0600, and that
    file is what the install pins. The seed carries the port-22 line.
    """
    accepted: list[str] = []
    for port in (BACKUP_SFTP_PORT, KEY_INSTALL_PORT):
        done = subprocess.run(
            ["ssh-keyscan", "-T", "10", "-p", str(port), server],
            capture_output=True, text=True, timeout=60)
        lines = [line for line in done.stdout.splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
        if done.returncode != 0 or not lines:
            raise ProviderError(
                f"provision: ssh-keyscan of {server} on port {port} failed: "
                f"{(done.stderr or 'no host key').strip()}")
        for line in lines:
            fingerprint = subprocess.run(
                ["ssh-keygen", "-lf", "-"], input=line + "\n",
                capture_output=True, text=True, timeout=30)
            fields = fingerprint.stdout.split()
            if fingerprint.returncode != 0 or len(fields) < 2:
                continue
            if fields[1] in HETZNER_HOST_FINGERPRINTS:
                accepted.append(line)
    if not accepted:
        raise ProviderError(
            f"provision: {server}'s host key is not one of the fingerprints the "
            "product publishes; refusing to pin it")
    known_hosts.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Merge, never replace: a pair is two hostnames on one box, and the second
    # account's scan must not drop the first's lines (measured 2026-11-25: the
    # read-only scan overwrote the read-write pin and restic then refused the
    # read-write URL with "No RSA host key is known").
    existing: list[str] = []
    if known_hosts.is_file():
        existing = [line for line in known_hosts.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
    combined = list(dict.fromkeys(existing + accepted))
    known_hosts.write_text("\n".join(combined) + "\n", encoding="utf-8")
    os.chmod(known_hosts, 0o600)
    # One line for the seed: an ed25519 line on port 22 when the host offers
    # one, else the first accepted (the drill measured RSA on port 22).
    for line in accepted:
        if " ssh-ed25519 " in line and not line.startswith("["):
            return line
    for line in accepted:
        if not line.startswith("["):
            return line
    return accepted[0]


def _install_public_key(server: str, username: str, public_key: str,
                        password: str, known_hosts: Path) -> str:
    """Install one public key over the documented port-23 `install-ssh-key`.

    The command writes both formats in one step — RFC4716 for port 22, OpenSSH
    for port 23 — which is why the pair is usable on the steady-state port
    after the read-only account is locked. The password reaches `sshpass`
    through a 0600 file, never argv.
    """
    handle, path = tempfile.mkstemp(prefix=".odile-keyinstall-", suffix=".pass")
    try:
        os.write(handle, password.encode("utf-8"))
        os.close(handle)
        os.chmod(path, 0o600)
        command = [
            "sshpass", "-f", path,
            "ssh", "-p", str(KEY_INSTALL_PORT),
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=password,keyboard-interactive",
            f"{username}@{server}", "install-ssh-key",
        ]
        done = subprocess.run(command, input=public_key + "\n",
                              capture_output=True, text=True, timeout=120)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    output = (done.stdout or "") + (done.stderr or "")
    if (done.returncode != 0 or "RFC4716 format" not in output
            or "OpenSSH format" not in output):
        raise ProviderError(
            f"provision: installing the key on {username}@{server} failed "
            f"(exit {done.returncode}): {output.strip()[-400:]}")
    return output.strip()


def _write_store(path: Path, pair: BackupPair) -> None:
    """Write the store of record atomically, 0600, in a 0700 directory."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    handle, temporary = tempfile.mkstemp(prefix=".odile-pair-", dir=str(path.parent))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(pair.to_env())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _write_seed_pair(ctx: Context, settings: BackupSettings, pair: BackupPair) -> None:
    """Hand the pair to the seed service's per-exchange file.

    The service runs as `odile` and cannot read `/root/.odile/hetzner/` (0700
    root), so this is a copy in the shape the service reads: 0600, owned by the
    directory's owner, one entry per slug. A `seed-config.json` rewrite would be
    invisible until a restart.
    """
    seed = _seed_service()
    try:
        seed.set_backup_pair(str(settings.pairs), pair.slug, pair.to_entry())
    except (seed.ConfigError, OSError, ValueError) as error:
        raise ProviderError(
            f"provision: {pair.slug}'s pair was not written to {settings.pairs}: {error}"
        ) from None


def _record_backup_detail(ctx: Context, pair: BackupPair) -> str:
    """Cache the pair's ids and logins on the tenant row; the env file is truth."""
    db_path = ctx.config.path_value("state_db", DEFAULT_STATE_DB) or Path(DEFAULT_STATE_DB)
    try:
        store = StateStore.open(db_path)
    except StateError as error:
        raise ProviderError(f"provision: cannot open the state database: {error}") from None
    try:
        try:
            store.note(pair.slug, detail={
                "backup_box_id": pair.box_id,
                "backup_subaccount_id": pair.subaccount_id,
                "backup_subaccount_username": pair.subaccount_username,
                "backup_readonly_subaccount_id": pair.readonly_subaccount_id,
                "backup_readonly_subaccount_username": pair.readonly_subaccount_username,
                "backup_repository": pair.repo_url,
                "previous_instance": pair.previous_instance,
            })
        except UnknownTenant:
            return "absent"
        return "recorded"
    except StateError as error:
        raise ProviderError(
            f"provision: {pair.slug}'s pair is recorded but the row was not updated: "
            f"{error}") from None
    finally:
        store.close()


def _remember_backup(ctx: Context, result: "BackupResult") -> "BackupResult":
    """Record the pair on the row after the machine's own write.

    `store.provision(...)` replaces the row detail, so the pair's cache is
    written after every transition this run makes — otherwise the machine's
    `source`/`release` detail would be the only detail left.
    """
    return replace(result, row=_record_backup_detail(ctx, result.pair))


def _box_server(subaccount_server: str) -> str:
    """The box's own hostname from a sub-account's (`u728180-sub1…` -> `u728180…`)."""
    return re.sub(r"-sub\d+(?=\.)", "", subaccount_server)


def _pair_from_api(slug: str, rw: dict, ro: dict) -> dict:
    """The facts the API returned for a pair, checked against the ADR's shape."""
    rw_home = str(rw.get("home_directory") or "")
    ro_home = str(ro.get("home_directory") or "")
    if rw_home != f"tenants/{slug}":
        raise ProviderError(
            f"provision: {slug}'s read-write home is {rw_home!r}; ADR-004 §4.1 "
            f"says tenants/{slug}")
    if ro_home != f"tenants/{slug}/data":
        raise ProviderError(
            f"provision: {slug}'s read-only home is {ro_home!r}; it must be "
            f"tenants/{slug}/data — a subdirectory of the read-write home, never "
            "the same home (shared homes share authorized_keys)")
    ro_settings = ro.get("access_settings") or {}
    if ro_settings.get("readonly") is not True:
        raise ProviderError(
            f"provision: {slug}'s read-only sub-account is not locked "
            f"(readonly={ro_settings.get('readonly')!r})")
    rw_settings = rw.get("access_settings") or {}
    if rw_settings.get("readonly") is not False:
        raise ProviderError(
            f"provision: {slug}'s read-write sub-account is read-only on the box; "
            "the nightly backup could not upload")
    for item in (rw, ro):
        login = str(item.get("username") or "")
        if not re.match(r"^u[0-9]+-sub[0-9]+$", login):
            raise ProviderError(
                f"provision: the API returned {login!r} as a login; expected "
                "u<box>-sub<N>, and it is recorded, never derived")
    return {"rw": rw, "ro": ro}


def _pair_keys(settings: BackupSettings, slug: str) -> tuple[str, str]:
    """The two private halves HQ holds for a pair that already exists."""
    directory = settings.keys / slug
    rw = directory / f"odile-{slug}-rw"
    ro = directory / f"odile-{slug}-ro"
    missing = [str(path) for path in (rw, ro) if not path.is_file()]
    if missing:
        raise ProviderError(
            f"provision: {slug}'s pair exists on the box but HQ holds no private "
            f"key at {', '.join(missing)}; restore the key or delete the pair and "
            "provision again")
    return (rw.read_text(encoding="utf-8"), ro.read_text(encoding="utf-8"))


def _provision_backup(ctx: Context, slug: str, previous_instance: str) -> BackupResult:
    """Mint (or adopt, or read back) the tenant's pair. Idempotent by name.

    The order is the measured one: read the box, refuse the 51st, adopt a pair
    that is already there (the drill's `odl-bench`), or create both accounts
    and walk each one through update-settings, a password reset, the key
    install, a readback, and — for the read-only half — the `readonly: true`
    lock after the readback. The store of record, the row's `detail` and the
    seed handoff are written last, so a failure before them leaves nothing a
    later run would believe.
    """
    settings = _backup_settings(ctx)
    _refuse_the_51st(settings, slug, None)
    store_path = settings.store / f"odile-{slug}.env"
    subaccounts = _subaccounts(settings)
    named = _named_pair(subaccounts, slug)
    api_pairs = _pair_count(subaccounts)
    rw_home = f"tenants/{slug}"
    ro_home = f"tenants/{slug}/data"

    recorded = None
    if store_path.is_file():
        try:
            recorded = BackupPair.from_env(store_path.read_text(encoding="utf-8"))
        except ProviderError:
            # The drill (t_12b3720a) wrote its own `odile-odl-bench.env` shape.
            # Keep it under a dated name and record this card's format beside
            # it: the API pair is the truth, and this file is only the cache.
            kept = store_path.with_name(store_path.name + ".pre-odl924")
            if not kept.exists():
                store_path.rename(kept)
                os.chmod(kept, 0o600)
            recorded = None
    if recorded is not None:
        if named is None:
            raise ProviderError(
                f"provision: {store_path} records sub-accounts "
                f"{recorded.subaccount_id}/{recorded.readonly_subaccount_id}, but the "
                f"box holds no pair named odile-{slug}-rw/-ro. Delete the store "
                "file to mint a new pair")
        facts = _pair_from_api(slug, *named)
        if (facts["rw"].get("id") != recorded.subaccount_id
                or facts["ro"].get("id") != recorded.readonly_subaccount_id
                or facts["rw"].get("username") != recorded.subaccount_username
                or facts["ro"].get("username") != recorded.readonly_subaccount_username):
            raise ProviderError(
                f"provision: {store_path} and the box disagree about {slug}'s pair "
                f"(store {recorded.subaccount_id}/{recorded.readonly_subaccount_id}, "
                f"box {facts['rw'].get('id')}/{facts['ro'].get('id')}); refusing to "
                "guess which pair the tenant owns")
        _write_seed_pair(ctx, settings, recorded)
        return BackupResult("same", recorded, 0, "pending")

    _refuse_the_51st(settings, slug, api_pairs)
    if named is not None:
        facts = _pair_from_api(slug, *named)
        rw_key, ro_key = _pair_keys(settings, slug)
        known_hosts = settings.keys / slug / "known_hosts"
        pinned = ""
        if known_hosts.is_file():
            # A drill's file starts with `# host:22 SSH-2.0-…` comment lines;
            # the first real line is the pin.
            for line in known_hosts.read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.lstrip().startswith("#"):
                    pinned = line
                    break
        if not pinned:
            pinned = _scan_host_key(str(facts["rw"].get("server")), known_hosts)
        pair = BackupPair(
            slug=slug, box_id=settings.box_id,
            box_server=_box_server(str(facts["rw"].get("server") or "")),
            subaccount_id=int(facts["rw"]["id"]),
            subaccount_username=str(facts["rw"]["username"]),
            subaccount_server=str(facts["rw"]["server"]),
            subaccount_home=str(facts["rw"]["home_directory"]),
            readonly_subaccount_id=int(facts["ro"]["id"]),
            readonly_subaccount_username=str(facts["ro"]["username"]),
            readonly_subaccount_server=str(facts["ro"]["server"]),
            readonly_subaccount_home=str(facts["ro"]["home_directory"]),
            repo_url=f"sftp:{facts['rw']['username']}@{facts['rw']['server']}:"
                     f"{BACKUP_REPO_PATH}",
            readonly_repo_url=f"sftp:{facts['ro']['username']}@{facts['ro']['server']}:"
                              f"{BACKUP_READONLY_REPO_PATH}",
            known_hosts=pinned, rw_private_key=rw_key, ro_private_key=ro_key,
            previous_instance=previous_instance,
            provisioned_at=_stamp_now(),
        )
        _write_store(store_path, pair)
        _write_seed_pair(ctx, settings, pair)
        return BackupResult("adopted", pair, 0, "pending")

    # ---- create both halves
    directory = settings.keys / slug
    rw_key, rw_public = _generate_keypair(directory, f"odile-{slug}-rw",
                                          f"odile-{slug}-rw@hq")
    ro_key, ro_public = _generate_keypair(directory, f"odile-{slug}-ro",
                                          f"odile-{slug}-ro@hq")
    resets = 0
    created: dict[str, dict] = {}
    for wanted, home, public, lock in (
        ("rw", rw_home, rw_public, False),
        ("ro", ro_home, ro_public, True),
    ):
        name = f"odile-{slug}-{wanted}"
        subaccount_id = _create_subaccount(settings, name, home, _password())
        # The create call dropped these; the follow-up and the readback are what
        # make them true. The read-only half stays writable until its key is in.
        _update_access_settings(settings, subaccount_id, readonly=False)
        password = _reset_password(settings, subaccount_id)
        resets += 1
        facts = _readback(settings, subaccount_id, readonly=False)
        server = str(facts.get("server") or "")
        known_hosts = directory / "known_hosts"
        # A brand-new sub-account's hostname appears a beat after the create,
        # and the reset password takes a beat to reach the SFTP path (the drill
        # saw both). Retry only the failures that mean "not yet"; a key-format
        # or a host-key mismatch is final.
        for attempt in range(24):
            try:
                pinned_now = (known_hosts.read_text(encoding="utf-8")
                              if known_hosts.is_file() else "")
                if f"[{server}]:{KEY_INSTALL_PORT}" not in pinned_now:
                    # A first attempt's scan may name another sub-account (the
                    # provider reuses a login, and a failed run leaves its own
                    # file behind); scan again for THIS hostname.
                    _wait_for_dns(server)
                    _scan_host_key(server, known_hosts)
                _install_public_key(server, str(facts.get("username") or ""), public,
                                    password, known_hosts)
                break
            except ProviderError as error:
                # A new sub-account's hostname takes minutes to appear in DNS,
                # and the reset password takes a beat to reach SFTP. Retry only
                # the failures that mean "not yet"; a key-format or a host-key
                # mismatch is final.
                transient = any(
                    marker in str(error) for marker in
                    ("Permission denied", "Could not resolve hostname",
                     "Name or service not known", "Temporary failure in name resolution",
                     "Connection closed", "Connection refused", "timed out"))
                if not transient or attempt == 23:
                    raise
                SLEEP(5)
        password = ""
        if lock:
            _update_access_settings(settings, subaccount_id, readonly=True)
            _readback(settings, subaccount_id, readonly=True)
        # The password was used once; the reset below is the discard, and its
        # value is never read, printed or stored.
        _reset_password(settings, subaccount_id)
        resets += 1
        _status, payload = _hetzner_api(
            settings, "GET",
            f"/storage_boxes/{settings.box_id}/subaccounts/{subaccount_id}")
        created[wanted] = payload.get("subaccount") or {}

    facts = _pair_from_api(slug, created["rw"], created["ro"])
    known_hosts = directory / "known_hosts"
    pinned = known_hosts.read_text(encoding="utf-8").strip().splitlines()[0]
    pair = BackupPair(
        slug=slug, box_id=settings.box_id,
        box_server=_box_server(str(facts["rw"].get("server") or "")),
        subaccount_id=int(facts["rw"]["id"]),
        subaccount_username=str(facts["rw"]["username"]),
        subaccount_server=str(facts["rw"]["server"]),
        subaccount_home=str(facts["rw"]["home_directory"]),
        readonly_subaccount_id=int(facts["ro"]["id"]),
        readonly_subaccount_username=str(facts["ro"]["username"]),
        readonly_subaccount_server=str(facts["ro"]["server"]),
        readonly_subaccount_home=str(facts["ro"]["home_directory"]),
        repo_url=f"sftp:{facts['rw']['username']}@{facts['rw']['server']}:"
                 f"{BACKUP_REPO_PATH}",
        readonly_repo_url=f"sftp:{facts['ro']['username']}@{facts['ro']['server']}:"
                          f"{BACKUP_READONLY_REPO_PATH}",
        known_hosts=pinned, rw_private_key=rw_key, ro_private_key=ro_key,
        previous_instance=previous_instance,
        provisioned_at=_stamp_now(),
    )
    _write_store(store_path, pair)
    _write_seed_pair(ctx, settings, pair)
    return BackupResult("created", pair, resets, "pending")


def _stamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _backup_lines(result: BackupResult) -> list[str]:
    pair = result.pair
    state = {"created": "created", "adopted": "adopted (read back, not re-created)",
             "same": "same"}[result.state]
    lines = [
        f"  backup:     odile-{pair.slug}-rw {pair.subaccount_id} "
        f"{pair.subaccount_username} {pair.subaccount_home} ({state})",
        f"  backup ro:  odile-{pair.slug}-ro {pair.readonly_subaccount_id} "
        f"{pair.readonly_subaccount_username} {pair.readonly_subaccount_home} "
        f"(readonly: true)",
        f"  backup repo:{pair.repo_url} (no port; 22 lives in sftp.command)",
    ]
    if result.state == "created":
        lines.append(
            f"  backup keys: both formats installed (RFC4716 for port 22, OpenSSH "
            f"for 23); passwords reset twice and discarded")
    lines.append(f"  backup file:{pair.slug} in the store of record; row detail {result.row}")
    return lines



def _reopen_line(slug: str) -> str:
    """The one line a rebuild adds: same slug, same URL (ADR-007)."""
    return (
        f"  reprovision: destroyed -> claimed (same slug, {slug}); "
        "the URL and the recovery file keep their names"
    )


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


def _row_before(ctx: Context, slug: str) -> tuple[str, str | None] | None:
    """The tenant's state and machine, read without writing the database.

    The dry run must list the reopen it would take, and `StateStore.open`
    writes the schema (and creates the file), so this peeks at an existing file
    in SQLite's read-only URI mode instead. A missing file or table is `None`:
    a new tenant has no row yet. The machine is read with the state because the
    refusal below has to name the machine the row would keep.
    """
    path = ctx.config.path_value("state_db", DEFAULT_STATE_DB) or Path(DEFAULT_STATE_DB)
    if not Path(path).exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        try:
            row = con.execute(
                "SELECT state, server_id FROM tenant_state WHERE slug = ?", (slug,)
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return (row[0], row[1]) if row else None


def _refuse_an_unrecordable_order(
    slug: str, state: str | None, server_id: str | None, existing: list[Machine]
) -> None:
    """Refuse, before the paid call, an order the row could not record.

    A row in `MACHINE_STATES` already names a machine and `TRANSITIONS` has no
    edge from it back to `provisioning`, so a new `server_id` could only reach
    `_record_machine` as a silent `web_row: "skipped"` — the paid machine no
    row names that this card measured. A live row is allowed through only when
    the provider's machine for the slug *is* the row's machine (the reuse path
    records it without ordering). Every other row — `invited`, `claimed`, a
    destroyed row the caller has reopened, or no row at all — names no machine
    and can record an order.

    The remedy in the message is the documented two-step: `reconcile --apply`
    marks the row `destroyed` and its finding names this same-slug rebuild,
    then `provision` reopens the row and orders. A terminated provider row
    counts as absent, the way `state.reconcile` reads it.
    """
    if state not in MACHINE_STATES:
        return
    present = [machine for machine in existing if machine.state != "terminated"]
    if present and present[0].server_id == server_id:
        return
    named = repr(server_id) if server_id else "no machine"
    if present:
        listed = f"the provider's machine for {slug} is {present[0].server_id!r}"
    else:
        listed = f"the provider lists no machine named {slug}"
    raise ProviderError(
        f"provision: {slug} is {state} and its state row names {named}, but "
        f"{listed}; ordering one now would be a paid machine no row names. "
        "Mark the old machine gone first, then run this command again:\n"
        f"  odilectl reconcile {slug} --apply"
    )


def _reopen_destroyed(ctx: Context, slug: str) -> tuple[bool, Tenant | None]:
    """Move a destroyed row to `claimed` before the order (ADR-007).

    The order must land on a row that can record it. A destroyed row is
    reopened first, so a failed `create` leaves a retryable claim rather than a
    paid machine no row names. The reason is in the event log; the slug, the
    URL and the repository keep their names.

    Returns the row as it now stands, read through the same store that wrote
    it. `_refuse_an_unrecordable_order` uses that row, not the dry run's
    read-only peek: SQLite's `mode=ro` needs the WAL side files, so the peek
    can miss a row that exists, and the guard must not.
    """
    db_path = (
        ctx.config.path_value("state_db", DEFAULT_STATE_DB) or Path(DEFAULT_STATE_DB)
    )
    try:
        store = StateStore.open(db_path)
    except StateError as error:
        raise ProviderError(f"provision: cannot open the state database: {error}") from None
    try:
        try:
            tenant = store.get(slug)
        except UnknownTenant:
            return (False, None)
        if tenant.state != "destroyed":
            return (False, tenant)
        return (True, store.reopen(slug, reason=REPROVISION_REASON))
    except StateError as error:
        raise ProviderError(f"provision: {slug} could not be reopened: {error}") from None
    finally:
        store.close()


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _iso_epoch(epoch: int | None) -> str | None:
    """The app's `tenants.expires_at` is ISO text; the provider speaks seconds."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _point_web_row_at_machine(
    connection: sqlite3.Connection, spec: CreateSpec, machine: Machine
) -> str:
    """Point the web `tenants` row at the machine this run ordered.

    The row the app and the first-boot writer read must name the machine that
    exists now. After a rebuild it still named the dead one, and `record_boot`
    refused the new machine's measured line ("is ordered for instance
    'srv-old', the measured line boots 'srv-new'"). This is the write the claim
    path makes when it orders (server_id, ipv4, expires_at, state
    `provisioned`), plus the key it is ordered with; `installed_at` goes back
    to NULL because this machine has not installed. The release is not written
    here: `StateStore.record_release` is its one owner, so the spine and the
    web row cannot name different releases. `setup_token_hash` is untouched —
    `seed.bind` wrote the new token's hash moments before this write. A row
    that already names this machine is not rewritten, so a second run cannot
    move an `active` row backwards.

    Returns "absent" (a state-only database, or a row the claim never made),
    "unchanged" (the row already names this machine) or "repointed".
    """
    if not _has_table(connection, "tenants"):
        return "absent"
    row = connection.execute(
        "SELECT id, server_id FROM tenants WHERE slug = ?", (spec.hostname,)
    ).fetchone()
    if row is None:
        return "absent"
    if row["server_id"] == machine.server_id:
        return "unchanged"
    try:
        connection.execute(
            "UPDATE tenants SET server_id = ?, ipv4 = ?, expires_at = ?,"
            " state = 'provisioned', ssh_public_key = ?,"
            " installed_at = NULL WHERE id = ?",
            (
                machine.server_id,
                machine.ipv4,
                _iso_epoch(machine.expires_at),
                spec.ssh_key.strip(),
                row["id"],
            ),
        )
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        raise ProviderError(
            f"provision: cannot point the tenant row at {machine.server_id}: {error}"
        ) from None
    return "repointed"


def _web_row_line(machine: Machine) -> str:
    """The report line for a web row that now names this run's machine."""
    return (
        f"  web row:   {machine.server_id} (state provisioned; the app and the "
        "first boot read this row)"
    )


def _record_machine(
    ctx: Context, spec: CreateSpec, machine: Machine, release: str
) -> dict[str, object]:
    """Write the order onto the tenant's state row and web row, idempotently.

    A tenant created by the claim spine already has a state row; a manual
    provision creates the missing one at `invited` and walks it to
    `provisioning`, the same path the claim code uses. A second run does not
    move the state again. `record_release` names the order's release on both
    rows, unless the machine has already reported what it installed — that
    observation wins (ODL-947), so a re-run cannot walk a booted row back to a
    stale pin.

    An order is recorded in both schemas (ODL-218 follow-up, ADR-007). The
    state row carries the machine and then reaches `first_boot` — the order
    exists, the machine runs its zero-touch boot — and the web row is pointed
    at the same machine. Only then can `record_boot` take the machine's
    measured line: it reads `tenants.server_id` and refuses a
    `claimed`/`provisioning` row. A same-slug rebuild takes this path again.
    A **live** row (`ready`, `suspended`, …) reaches this function only from
    the reuse path, whose machine is the row's own: `run()`'s
    `_refuse_an_unrecordable_order` has already refused an order the row could
    not record, and a row that still names another machine raises here.
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
        if tenant.state == "destroyed":
            raise StateError(
                f"{spec.hostname} is destroyed; reopen it (run this command, which "
                "reopens it before the order) before recording a machine"
            )
        recorded = False
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
            recorded = True
        else:
            before = tenant.release
            tenant = store.record_release(spec.hostname, release)
            recorded = before is None
        # The web row follows the spine: it is pointed at the machine only once
        # the state row names it, so the two can never disagree about which
        # machine exists. `_refuse_an_unrecordable_order` makes that true for
        # every order this run places — a live row reaches here only through
        # the reuse path, its own machine — so a row that still names another
        # machine is a loud failure, never a silent `web_row: "skipped"` that
        # would leave a paid order unrecorded. Then the spine's `first_boot`,
        # the state the machine's own record is written from.
        if tenant.server_id != machine.server_id:
            raise ProviderError(
                f"provision: {spec.hostname} is {tenant.state} and its state row "
                f"names {tenant.server_id!r}; the order for {machine.server_id} "
                "was not recorded (a live row has no edge back to provisioning). "
                "Run this, then this command again:\n"
                f"  odilectl reconcile {spec.hostname} --apply"
            )
        web_row = _point_web_row_at_machine(store.connection, spec, machine)
        if web_row == "repointed":
            # The boot facts belonged to the dead machine; the new order's
            # release is the best evidence until the new machine boots, so drop
            # the old observation before the release is written again.
            store.note(spec.hostname, detail={"boot_release": None})
        tenant = store.record_release(spec.hostname, release)
        if tenant.state == "provisioning":
            tenant = store.await_boot(spec.hostname)
        return {
            "recorded": recorded,
            "tenant_state": tenant.state,
            "release": tenant.release,
            "web_row": web_row,
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
    # A worker passes the tenant row's public key directly: the account path has
    # no operator shell and no key file (ODL-341). The CLI keeps `--key`.
    key_text = (getattr(args, "key_text", "") or "").strip() or None
    key_label = str(key_path) if key_path else (
        "the tenant row's public key" if key_text else "<the tenant record>")
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
            ssh_key=_read_key(key_path) if key_path else (key_text or _placeholder_key()),
            days=days,
            preorder=args.preorder,
        )
    except ValueError as error:
        raise UsageError(f"provision: {error}") from None

    row_before = _row_before(ctx, spec.hostname)
    state_before = row_before[0] if row_before else None
    reprovision = state_before == "destroyed"

    actions = []
    if reprovision:
        actions.append(
            {
                "action": "state.reopen",
                "slug": spec.hostname,
                "from": "destroyed",
                "to": "claimed",
                "reason": REPROVISION_REASON,
            }
        )
    account = keys.account_from_config(ctx.config)
    if account is None:
        actions.append({
            "action": "keys.shared",
            "slug": spec.hostname,
            "reason": "no near_account in the config; the seed carries the shared key",
        })
    else:
        actions.append({
            "action": "keys.issue",
            "slug": spec.hostname,
            "label": keys.label_for(spec.hostname),
            "workspace": account.workspace_id,
            "spend_limit": keys.money(account.spend_limit_nano),
            "spend_limit_nano": account.spend_limit_nano,
            "when": ("before the order; a live machine's waiting key is re-bound to the "
                     "new token, and a key that already left HQ is re-minted"),
            "credential": account.credential_path,
        })
    actions += [
        {
            "action": "backup.pair",
            "slug": spec.hostname,
            "rw_home": f"tenants/{spec.hostname}",
            "ro_home": f"tenants/{spec.hostname}/data",
            "when": (
                "read back and printed `same` when the box already names the pair; "
                "adopted without a create when only HQ is missing the record; "
                "created (each create, update_access_settings, key install, "
                "readback, then readonly: true) when it is absent"
            ),
            "refuse": (
                f"the 51st pair by name: {MAX_BACKUP_TENANTS} tenants per box "
                "(100 sub-accounts, a pair per tenant), before any create"
            ),
            "store_of_record": str(_backup_settings(ctx).store
                                   / f"odile-{spec.hostname}.env"),
            "seed_handoff": str(_backup_settings(ctx).pairs),
        },
        {
            "action": "provider.list",
            "provider": configured_name(ctx.config),
            "find_hostname": spec.hostname,
        },
        {
            "action": "state.guard",
            "slug": spec.hostname,
            "refuse_when": (
                "the row is a live machine state and the list above does not "
                "name that same server_id"
            ),
            "remedy": f"odilectl reconcile {spec.hostname} --apply, then this command again",
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
            "action": "seed.key.handoff",
            "slug": spec.hostname,
            "file": str(ctx.config.path_value("seed_keys") or "<the seed service's default>"),
            "bound_to": "the setup token the bind above mints",
            "consumed_by": "the redeem that spends the token; HQ keeps no raw key after it",
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
        "a live row (a machine state) whose machine the provider no longer lists "
        "is refused before the order — no paid machine no row names; mark it gone "
        f"first with `odilectl reconcile {spec.hostname} --apply`",
        f"the release {release} comes from the pin's provision_release and is "
        "recorded with the tenant once the machine is active",
        "the setup token is bound to the instance id the create call returns, "
        "before the machine is polled: single-use, and refused on any other instance",
        "the token is printed once; the seed service stores only its SHA-256 hash",
        (
            "the inference key is minted for this instance before the order and "
            "written once into the seed: HQ keeps the label and the first characters, "
            "never the raw key"
            if account is not None
            else "no near_account in the config: the seed carries the config's shared "
            "key (alpha.0's manual path)"
        ),
        f"rebuild password: {backup_label} (its 'Backup password:' line is sealed "
        "under the token; `/var/lib/odile/seed-rebuilds.json` holds ciphertext, "
        "never the password)",
        (
            f"the backup pair is one sub-account pair on the BX11: 100 sub-accounts "
            f"per box and a pair per tenant, so {MAX_BACKUP_TENANTS} tenants per box "
            "and the 51st is refused by name — a second box is the next purchase"
        ),
        (
            "the pair shares the box-wide SFTP ceiling: 10 concurrent connections "
            "published, 25 measured, the 26th refused; ODL-933 sizes the nightly "
            "timer to the published 10 with sftp.connections=2"
        ),
    ]
    if reprovision:
        notes.insert(
            0,
            "the destroyed tenant is reopened (destroyed -> claimed) under the same "
            f"slug before the order: https://{spec.hostname}.odile.fyi and "
            f"odile-recovery-{spec.hostname}.txt keep their names",
        )
    notes.append(
        "the tenant row is pointed at the new server_id and the state row reaches "
        "first_boot, so the machine's first boot writes its own record"
    )

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

    if key_path is None and not key_text:
        raise UsageError(
            "provision --apply needs --key PATH until the tenant record holds the key"
        )

    # Before the first provider call: a destroyed row is reopened, so every
    # order this run places has a row that can record it (ADR-007). The row
    # this returns is the one the guard below reads: the store that writes it,
    # not the read-only peek the dry run uses.
    reopened, tenant_before = _reopen_destroyed(ctx, spec.hostname)
    state_now, machine_now = (
        (tenant_before.state, tenant_before.server_id) if tenant_before else (None, None)
    )

    # The tenant's pair, before any paid call: the 51st refusal, a half pair or
    # a login HQ cannot match all leave no machine billed. `previous_instance`
    # is the dead machine a rebuild replaces (the reopen kept the slug, not the
    # id), and "" on a first install.
    previous_instance = (row_before[1] or "") if (
        row_before and row_before[0] == "destroyed") else ""
    backup = _provision_backup(ctx, spec.hostname, previous_instance)

    existing = [machine for machine in ctx.provider.list() if machine.hostname == spec.hostname]
    if len(existing) > 1:
        raise ProviderError(
            f"provision: {len(existing)} machines are named {spec.hostname}; refusing to guess"
        )
    # Before the paid call: a live row can only record its own machine. A
    # missing machine is `reconcile`'s finding to make, not this command's.
    _refuse_an_unrecordable_order(spec.hostname, state_now, machine_now, existing)

    # The instance's inference key is minted *before* the order: a provider
    # refusal here leaves no paid machine without a key (ODL-242). A machine
    # that is already up re-issues its setup token without revoking the key it
    # is spending with — the raw key is re-bound to the new token when it still
    # waits, and re-minted (old one revoked) when it has already left HQ. A
    # reopen is always a re-mint: destroy revoked that key.
    key_binding, issued_key, waiting_key = _issue_key(
        ctx, spec.hostname, keep=bool(existing) and not reopened
    )

    def bind(instance_id: str) -> SeedBinding:
        """Bind the token, then pin this instance's key to it."""
        binding = _bind_seed_token(ctx, spec.hostname, instance_id,
                                   backup_password=backup_password)
        if binding.bound and binding.token_hash:
            if issued_key is not None:
                _bind_key_to_token(ctx, spec.hostname, issued_key, binding.token_hash, instance_id)
            elif waiting_key is not None:
                _rebind_waiting_key(ctx, spec.hostname, key_binding, waiting_key,
                                    binding.token_hash, instance_id)
        return binding

    if existing:
        machine = existing[0]
        if _is_queued(machine):
            seed = bind(machine.server_id)
            backup = _remember_backup(ctx, backup)
            return _queued_report(
                ctx, machine, created=False, release=release, seed=seed,
                reopened=reopened, key=key_binding, backup=backup,
            )
        if machine.state == "active" and machine.ipv4:
            seed = bind(machine.server_id)
            record = _record(ctx, spec, machine, release)
            backup = _remember_backup(ctx, backup)
            return applied(
                NAME,
                ctx,
                [
                    f"  {machine.server_id} ({machine.hostname}) is already active",
                    *([_reopen_line(spec.hostname)] if reopened else []),
                    *([_web_row_line(machine)] if record["web_row"] == "repointed" else []),
                    f"  release:   {record['release']} (from the pin, recorded with the tenant)",
                    *_key_lines(key_binding),
                    *_seed_lines(seed),
                    *_backup_lines(backup),
                ],
                head="provision: nothing ordered (the machine already exists)",
                machine=machine.to_dict(),
                created=False,
                recorded=record["recorded"],
                reopened=reopened,
                web_row=record["web_row"],
                release=record["release"],
                tenant_state=record["tenant_state"],
                key=key_binding.to_dict(),
                seed=seed.to_dict(),
                backup=backup.to_dict(),
            )
        if machine.state not in ("pending", "active"):
            raise ProviderError(
                f"provision: {machine.server_id} exists in state {machine.state}; "
                "destroy it or inspect it instead of ordering a second machine"
            )
        seed = bind(machine.server_id)
        machine = ctx.provider.poll(machine.server_id, timeout=args.timeout, interval=args.interval)
        created = False
    else:
        machine = ctx.provider.create(spec)
        # The order's id is in the create response and the machine is still
        # deploying, so the token exists before the guest can boot (ODL-455).
        seed = bind(machine.server_id)
        if _is_queued(machine):
            backup = _remember_backup(ctx, backup)
            return _queued_report(
                ctx, machine, created=True, release=release, seed=seed,
                reopened=reopened, key=key_binding, backup=backup,
            )
        # `create` may answer active before the address is filled in; the poll
        # is what guarantees the tenant actually has somewhere to connect.
        if machine.state != "active" or not machine.ipv4:
            machine = ctx.provider.poll(
                machine.server_id, timeout=args.timeout, interval=args.interval
            )
        created = True

    record = _record(ctx, spec, machine, release)
    backup = _remember_backup(ctx, backup)
    lines = [
        f"  server_id: {machine.server_id}",
        f"  hostname:  {machine.hostname}",
        *([_reopen_line(spec.hostname)] if reopened else []),
        *([_web_row_line(machine)] if record["web_row"] == "repointed" else []),
        f"  state:     {machine.state} ({machine.raw_status or 'no raw status'})",
        f"  ipv4:      {machine.ipv4 or '-'}",
        f"  expires:   {render_datetime(machine.expires_at)}",
        f"  release:   {record['release']} (from the pin, recorded with the tenant)",
        *_key_lines(key_binding),
        *_seed_lines(seed),
        *_backup_lines(backup),
    ]
    return applied(
        NAME,
        ctx,
        lines,
        machine=machine.to_dict(),
        created=created,
        recorded=record["recorded"],
        reopened=reopened,
        web_row=record["web_row"],
        release=record["release"],
        tenant_state=record["tenant_state"],
        key=key_binding.to_dict(),
        seed=seed.to_dict(),
        backup=backup.to_dict(),
    )


def _is_queued(machine: Machine) -> bool:
    """A paid preorder the vendor will build when the queue clears."""
    return (machine.raw_status or "").strip().lower() == "queued"


def _queued_report(
    ctx: Context,
    machine: Machine,
    *,
    created: bool,
    release: str,
    seed: SeedBinding,
    reopened: bool = False,
    key: KeyBinding | None = None,
    backup: "BackupResult | None" = None,
) -> Report:
    """A queued preorder is a success: the money left, the order exists."""
    position = machine.detail.get("queue_position")
    lines = [
        f"  server_id: {machine.server_id}",
        f"  hostname:  {machine.hostname}",
        *([_reopen_line(machine.hostname)] if reopened else []),
        f"  state:     {machine.state} (queued)",
        f"  queue:     {position if position is not None else 'position not reported'}",
        f"  release:   {release} (from the pin; recorded when the machine is active)",
        "  the order is paid; the provider builds the machine when the queue clears",
        *(_key_lines(key) if key is not None else []),
        *_seed_lines(seed),
        *(_backup_lines(backup) if backup is not None else []),
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
        reopened=reopened,
        release=release,
        note="the release is recorded when the machine becomes active",
        key=key.to_dict() if key is not None else None,
        seed=seed.to_dict(),
        backup=backup.to_dict() if backup is not None else None,
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


# --------------------------------------------------------------- the account queue
#
# ODL-341: a confirmed registration becomes a machine with no operator command.
# The web app writes one `accounts` row and moves it to `queued` on the
# confirmation POST; this worker is the root half, because the provider, DNS and
# key credentials are root-only. It calls the same `run()` the CLI calls, with
# the tenant row's one public key and the pin's release, and it keeps the
# existing no-bill rule: a failure terminates whatever this run created before
# the row is marked `failed`.

QUEUED = "queued"
MAX_REASON = 500


class _RecordingProvider(Provider):
    """A provider that delegates every call and remembers what `create` returned.

    The worker needs the id of a machine it must terminate when a later step
    fails. `run()` raises before returning the report, so the only place the id
    exists is the call that made it. `list` is passed through untouched, so
    adoption and the existing reuse paths behave exactly as they do from the
    CLI.
    """

    def __init__(self, inner: Provider) -> None:
        self.inner = inner
        self.created: list[Machine] = []

    def create(self, spec: CreateSpec) -> Machine:
        machine = self.inner.create(spec)
        self.created.append(machine)
        return machine

    def poll(self, server_id: str, *, timeout: float = 900.0,
             interval: float = 10.0) -> Machine:
        return self.inner.poll(server_id, timeout=timeout, interval=interval)

    def power(self, server_id: str, action: str) -> Machine:
        return self.inner.power(server_id, action)

    def terminate(self, server_id: str) -> bool:
        return self.inner.terminate(server_id)

    def list(self) -> list[Machine]:
        return self.inner.list()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state_path_for(config) -> Path:
    return config.path_value("state_db", DEFAULT_STATE_DB) or Path(DEFAULT_STATE_DB)


def _queue_rows(connection: sqlite3.Connection, slug: str | None) -> list[sqlite3.Row]:
    """Confirmed accounts whose one provision has not been claimed yet."""
    sql = ("SELECT id, slug, email FROM accounts"
           " WHERE provision_state = ? AND email_verified_at IS NOT NULL")
    values: list[object] = [QUEUED]
    if slug is not None:
        sql += " AND slug = ?"
        values.append(slug)
    sql += " ORDER BY id"
    return connection.execute(sql, values).fetchall()


def _claim_queued(connection: sqlite3.Connection, account_id: int, stamp: str) -> bool:
    """Move one row `queued -> building`; True for exactly one run."""
    changed = connection.execute(
        "UPDATE accounts SET provision_state = 'building', provision_started_at = ?"
        " WHERE id = ? AND provision_state = ?",
        (stamp, account_id, QUEUED),
    ).rowcount
    connection.commit()
    return changed == 1


def _finish(connection: sqlite3.Connection, account_id: int, state: str, stamp: str,
            *, server_id: str | None = None, ipv4: str | None = None,
            key_label: str | None = None, key_prefix: str | None = None,
            reason: str | None = None, finished: bool = True) -> None:
    """Write one account's provision facts.

    ODL-947 split the row's two moments: the order's own finish is stamped when
    the provider's machine is recorded (`provision_finished_at`, the queue's
    column), and the ready transition later (`finished=False`) keeps that stamp
    instead of overwriting it with the day the guest answered — the mail's facts
    sidecar carries the ready time. A `building` row with a `server_id` is the
    provider-active state: ordered, not ready, no ready mail.
    """
    connection.execute(
        "UPDATE accounts SET provision_state = ?,"
        " provision_finished_at = CASE WHEN ? THEN ? ELSE provision_finished_at END,"
        " server_id = COALESCE(?, server_id), ipv4 = COALESCE(?, ipv4),"
        " key_label = COALESCE(?, key_label), key_prefix = COALESCE(?, key_prefix),"
        " provision_reason = ? WHERE id = ?",
        (state, 1 if finished else 0, stamp, server_id, ipv4, key_label, key_prefix,
         (reason or None) and reason[:MAX_REASON], account_id),
    )
    connection.commit()


def _dns_name(config, slug: str) -> str:
    from .dns import tenant_zone

    return "%s.%s" % (slug, tenant_zone(config))


def _ensure_dns(config, slug: str, ipv4: str) -> str:
    """Write the tenant's DNS-only A record through the existing client.

    The name is the tenant's own (`<slug>.<tenant_domain>`), and the client
    talks to the zone that owns it (`dns_zone`), so a bench record lands in
    `odile.fyi` under `test.odile.fyi` without a second provider path.
    """
    from .dns import DEFAULT_TOKEN_FILE, PROVISION_TTL, CloudflareDNS, validate_zone

    token_path = config.path_value("dns_token_file", DEFAULT_TOKEN_FILE)
    try:
        token = Path(str(token_path)).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ProviderError(f"dns: cannot read the token at {token_path}: {error}") from None
    client = CloudflareDNS(
        token,
        validate_zone(config.get("dns_zone") or "odile.fyi"),
        zone_id=config.get("dns_zone_id"),
    )
    return client.ensure(_dns_name(config, slug), ipv4, PROVISION_TTL, apply=True).action


def _provision_one(connection: sqlite3.Connection, config, row: sqlite3.Row, *,
                   timeout: float, interval: float,
                   provider_factory=None) -> dict[str, object]:
    """Order one account's machine, record it, and never leave a billed orphan.

    The steps are the CLI's `run()`; what this adds is the queue's bookkeeping:
    the row is claimed before the first provider call, the machine id, IPv4 and
    key label/first characters are written back on success, and a failure
    terminates anything this run created before the reason is stored.
    """
    slug = row["slug"]
    tenant = connection.execute(
        "SELECT ssh_public_key FROM tenants WHERE slug = ?", (slug,)
    ).fetchone()
    if tenant is None or not (tenant["ssh_public_key"] or "").strip():
        reason = "no tenants row with a public key; nothing was ordered"
        _finish(connection, int(row["id"]), "failed", _stamp(), reason=reason)
        return {"slug": slug, "ok": False, "reason": reason, "terminated": False}

    factory = provider_factory or _build_provider
    recorder = _RecordingProvider(factory(config))
    ctx = Context(config=config, apply=True, dry_run=False,
                  provider_factory=lambda _config: recorder)
    args = SimpleNamespace(
        slug=slug, key=None, key_text=tenant["ssh_public_key"], plan=None,
        days=None, pin=None, backup_password_file=None, preorder=False,
        timeout=timeout, interval=interval,
    )
    try:
        report = run(ctx, args)
    except (ProviderError, UsageError, StateError) as error:
        reason = str(error).strip()[:MAX_REASON] or error.__class__.__name__
        terminated = False
        for machine in recorder.created:
            try:
                recorder.terminate(machine.server_id)
                terminated = True
            except Exception as cleanup:  # a cleanup failure must not hide the reason
                reason += f"; terminate failed: {str(cleanup)[:120]}"
        _finish(connection, int(row["id"]), "failed", _stamp(), reason=reason)
        return {"slug": slug, "ok": False, "reason": reason,
                "machine": recorder.created[-1].server_id if recorder.created else None,
                "terminated": terminated}

    machine = report.payload.get("machine") or {}
    keyinfo = report.payload.get("key") or {}
    server_id = machine.get("server_id")
    ipv4 = machine.get("ipv4")
    dns = "skipped (no IPv4)"
    if ipv4:
        try:
            dns = _ensure_dns(config, slug, ipv4)
        except (ProviderError, ValueError, OSError) as error:
            # The machine exists and is recorded; DNS is one more step a later
            # run (or `odilectl dns create`) can finish. It is named, not
            # swallowed, and it does not turn a built machine into a failure.
            dns = f"failed: {str(error)[:200]}"
    _finish(connection, int(row["id"]), "building", _stamp(),
            server_id=server_id, ipv4=ipv4,
            key_label=keyinfo.get("label"), key_prefix=keyinfo.get("key_prefix"))
    return {
        "slug": slug,
        "ok": True,
        "ready": False,
        "server_id": server_id,
        "ipv4": ipv4,
        "release": report.payload.get("release"),
        "key_label": keyinfo.get("label"),
        "key_prefix": keyinfo.get("key_prefix"),
        "dns": dns,
        "seed_expires": (report.payload.get("seed") or {}).get("expires_at"),
    }


def _pending_guest_rows(connection: sqlite3.Connection,
                        slug: str | None = None) -> list[sqlite3.Row]:
    """Accounts whose machine was ordered but whose guest half has not answered.

    ODL-947: the provider's `active` is not the product's ready. The row keeps
    `provision_state='building'` with the machine recorded, and
    `provision_finished_at` keeps the order's own stamp; the ready moment is
    the machine's answer (the mail's facts carry it), not the row's finish
    column. The next run asks the machine itself.
    """
    sql = ("SELECT * FROM accounts WHERE provision_state = 'building'"
           " AND server_id IS NOT NULL")
    values: list[object] = []
    if slug is not None:
        sql += " AND slug = ?"
        values.append(slug)
    sql += " ORDER BY id"
    return connection.execute(sql, values).fetchall()


def _guest_domain(config) -> str:
    from .dns import tenant_zone

    return tenant_zone(config)


def _default_guest_probe(domain: str, slug: str):
    from .health import guest_answer

    return guest_answer(domain, slug)


def _drain_guest_ready(connection: sqlite3.Connection, config, *,
                       guest_probe=None, out=print,
                       limit: int | None = None, slug: str | None = None) -> list[dict[str, object]]:
    """Promote an ordered account to ready when the machine itself answers.

    The ready email is written first and the row moves second: a crash between
    the two leaves the same bytes to be rewritten (the builder is idempotent),
    never a `ready` row with no mail. The measurement written into the mail is
    the machine's own boot-report value, so the `/verify` sentence that asks
    the tester to compare the two is true.
    """
    from . import claim as claim_module

    probe = guest_probe or _default_guest_probe
    domain = _guest_domain(config)
    outbox = str(config.path_value("outbox", claim_module.DEFAULT_OUTBOX))
    results: list[dict[str, object]] = []
    rows = _pending_guest_rows(connection, slug)
    for row in rows[:limit] if limit else rows:
        name = str(row["slug"])
        answer = probe(domain, name)
        if answer is None:
            out("provision %s: waiting for https://%s.%s/verify" % (name, name, domain))
            continue
        tenant = connection.execute(
            "SELECT * FROM tenants WHERE slug = ?", (name,)).fetchone()
        host = "%s.%s" % (name, domain)
        url = "https://" + host
        try:
            message = str(claim_module.queue_ready_email(
                outbox,
                slug=name,
                to=str(row["email"]),
                url=url,
                host=host,
                measurement=answer.measurement,
                ipv4=str(row["ipv4"] or ""),
                server_id=str(row["server_id"] or ""),
                release=(tenant["release"] if tenant is not None else None),
            ))
        except (claim_module.ClaimError, OSError, ValueError) as error:
            # The machine answered; a failed queue write is retried by the next
            # timer tick with the same facts, and must not stop the other rows.
            out("provision %s: ready but the mail could not be written: %s"
                % (name, str(error)[:200]))
            results.append({"slug": name, "ok": True, "ready": False,
                            "error": str(error)[:200]})
            continue
        _finish(connection, int(row["id"]), "ready", _stamp(),
                server_id=row["server_id"], ipv4=row["ipv4"], finished=False)
        out("provision %s: ready at https://%s.%s/verify · measurement %s"
            % (name, name, domain, answer.measurement[:16]))
        results.append({
            "slug": name,
            "ok": True,
            "ready": True,
            "server_id": row["server_id"],
            "ipv4": row["ipv4"],
            "measurement": answer.measurement,
            "verify_bytes": answer.verify_bytes,
            "email": message,
        })
    return results


def _build_provider(config) -> Provider:
    from .providers import build

    return build(config)


def provision_queue(config, *, apply: bool = True, slug: str | None = None,
                    limit: int | None = None, timeout: float = DEFAULT_TIMEOUT,
                    interval: float = DEFAULT_INTERVAL, provider_factory=None,
                    guest_probe=None, out=print) -> list[dict[str, object]]:
    """Drain the confirmed-account queue, one row at a time. Safe to re-run.

    Without `apply` nothing is claimed and nothing is ordered: the plan names
    every row the next run would provision. A row is claimed with a
    conditional UPDATE before the first provider call, so two workers on one
    database order one machine, not two.

    Two phases (ODL-947): the provider call orders the machine and records it
    while the row stays `building`; a later pass asks the machine's own
    `/verify` and only then writes `ready` and queues the one ready email. On
    the bench the guest half installs after the machine boots, so the two
    phases are normally separate runs of the timer.
    """
    db_path = _state_path_for(config)
    try:
        connection = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error as error:
        raise ProviderError(f"provision: cannot open the state database {db_path}: {error}") from None
    connection.row_factory = sqlite3.Row
    results: list[dict[str, object]] = []
    try:
        rows = _queue_rows(connection, slug)
        pending = _pending_guest_rows(connection, slug)
        if not apply:
            for row in rows[:limit] if limit else rows:
                out(f"would provision {row['slug']} ({row['email']})")
            for row in pending[:limit] if limit else pending:
                out(f"would wait for {row['slug']}'s /verify before ready")
            return results
        for row in rows[:limit] if limit else rows:
            stamp = _stamp()
            if not _claim_queued(connection, int(row["id"]), stamp):
                continue
            out(f"provision {row['slug']}: queued at {stamp}")
            result = _provision_one(connection, config, row, timeout=timeout,
                                    interval=interval, provider_factory=provider_factory)
            results.append(result)
            if result["ok"]:
                out("provision %(slug)s: server %(server_id)s at %(ipv4)s; "
                    "key %(key_label)s %(key_prefix)s; dns %(dns)s; "
                    "setup token bound until %(seed_expires)s" % result)
            else:
                out("provision %(slug)s: FAILED - %(reason)s; machine %(machine)s "
                    "terminated=%(terminated)s" % {"machine": "-", **result})
        results.extend(_drain_guest_ready(connection, config, slug=slug,
                                          guest_probe=guest_probe, out=out,
                                          limit=limit))
        return results
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    """`python3 -m odilectl.provision --worker` — the account queue's root half."""
    parser = ArgumentParser(
        prog="odilectl.provision",
        description="The account queue: order the machine a confirmed account queued.",
    )
    parser.add_argument("--worker", action="store_true",
                        help="drain the confirmed-account queue (required)")
    parser.add_argument("--config", metavar="PATH", default=None,
                        help="config file, mode 0600")
    parser.add_argument("--dry-run", action="store_true",
                        help="name every row the next run would provision")
    parser.add_argument("--apply", action="store_true",
                        help="execute (without it nothing is ordered)")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    parser.add_argument("--slug", default=None, help="one account, by its slug")
    parser.add_argument("--limit", type=int, default=None, help="at most N rows")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    args = parser.parse_args(argv)
    if not args.worker:
        parser.error("pass --worker: this entry point drains the account queue")
    try:
        config = Config.load(args.config)
    except ConfigError as error:
        print(f"provision: {error}", file=sys.stderr)
        return 2
    try:
        results = provision_queue(
            config, apply=bool(args.apply and not args.dry_run), slug=args.slug,
            limit=args.limit, timeout=args.timeout, interval=args.interval,
            out=(lambda line: None) if args.json else print,
        )
    except ProviderError as error:
        print(f"provision: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"ok": all(r["ok"] for r in results), "count": len(results),
                          "results": results}, sort_keys=True, default=str))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
