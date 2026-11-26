"""`purge` — delete a destroyed tenant's encrypted repository on its keep_until.

ODL-218 follow-up shipped this command against a local `--root`; ODL-935's
re-cut (ADR-004 v3 §8's exit row) gives it the BX11 destination and the
sub-account pair. The destination is **chosen by the row's repository URL**,
not by a flag:

* `sftp:` — the tenant's repository on the Storage Box. Read the pair from
  `/root/.odile/hetzner/odile-<slug>.env` (0600), forget and prune every
  snapshot with the tenant's read-write key over SFTP on port 22, then delete
  both sub-accounts through `DELETE /v1/storage_boxes/{id}/subaccounts/{id}`
  with the project token, then record `detail.backup.subaccount_deleted_at`.
  A repository named `repo-<4 hex>` is a lost-recovery-file start-fresh
  (ADR-004 §7.4): the old directory is pruned inside the same sub-account and
  the pair is left alone — the tenant still writes to it — so that branch makes
  no API call.
* anything else — a path below `--root`, exactly as `t_9d6fb595` shipped it
  (the demo and bench migration): the restic check, the `shutil.rmtree`, the
  `authorized_keys` line and `detail.backup.purged`.

**The repository password is the user's.** HQ holds the read-write *key*, not
the password (ADR-004 §3), so on an `sftp:` row:

* with `--password-file` (the user's recovery file, or a bare password file)
  the command runs `restic snapshots --json` and `restic forget --prune <id>`
  for every snapshot — the literal plan;
* without it, restic answers "wrong password" (exit 12) and the command prunes
  the repository's `snapshots/`, `index/` and `data/` objects with the
  read-write key over SFTP, leaving `config` and `keys/` exactly as a prune
  does. That is the production path: at keep_until nobody has the password,
  and the key is the only credential that reaches the ciphertext.

Either way the bytes are gone, the two keys that could read them are deleted,
and two things **outlive** the deletion, stated and recorded:

* the empty `tenants/<slug>` directory — only the box's main account can
  delete it, and HQ deliberately does not hold that credential;
* the box's own snapshots (04:30 UTC, 10 kept) — deleting a repository does
  not delete them, so the deleted directory can survive up to about ten days.

The 30-day promise and the row's `keep_until` do not move: the date is the
row's, and a re-run cannot push it forward. A second run says `already
deleted` (or `already pruned` for a start-fresh directory) and writes nothing.

No password, private key or token reaches argv or a log: the key and the
password live in 0600 files for the length of one call, and the token is read
from its 0600 file and sent as an Authorization header by the shared API
client in `provision`.

Exit codes: 0 ok, 2 usage or a refusal, 1 a failed prune, delete or rewrite,
4 a repository restic cannot open (or a provider refusal), 5 config (no
restic or no sftp on this machine).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from argparse import ArgumentParser
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import provision
from .core import Context, Report, applied, plan, result
from .errors import ConfigError, OdileError, UsageError
from .state import DEFAULT_STATE_DB, StateStore, UnknownTenant

NAME = "purge"
HELP = "Delete a destroyed tenant's encrypted repository, once its keep_until has passed."
EXAMPLE = "odilectl purge odl-7f3a2c --root /srv/odile-backup --apply"

# The backup box's SFTP chroot, for the local (`--root`) path only.
DEFAULT_ROOT = "/srv/odile-backup"
SFTP_PORT = 22
RESTIC_TIMEOUT = 6 * 60 * 60
SFTP_TIMEOUT = 30 * 60

# ADR-004 §7.4: a lost recovery file mints a new repository directory named
# `repo-<4 hex>` inside the same sub-account. The old one is pruned at its
# keep_until; the pair stays, because the tenant still writes to it.
START_FRESH_RE = re.compile(r"^repo-[0-9a-f]{4}$")
REPOSITORY_ITEM_RE = re.compile(r"^[0-9a-f]{64}$")

# The installer's grammar, the same one `scripts/backup.py` and `restore.py`
# read (installer/odile_install.py:parse_recovery).
RECOVERY_PASSWORD_RE = re.compile(r"^Backup password:\s*(\S+)\s*$", re.M)
SFTP_HOST_RE = re.compile(
    r"^(?:sftp:(?://)?)?(?P<user>[^@/:]+)@(?P<host>[^:/]+)"
    r"(?::(?P<port>\d+))?(?::|/)"
)


class RepositoryUnreadable(OdileError):
    """The directory is there but restic cannot open it as a repository."""

    exit_code = 4


@dataclass(frozen=True)
class Transport:
    """One repository plus the credentials that reach it, for one call."""

    repo: str
    path: str
    user: str
    host: str
    port: int
    key: Path
    known_hosts: Path


def add_arguments(parser: ArgumentParser) -> None:
    parser.add_argument("slug", help="the destroyed tenant's slug")
    parser.add_argument(
        "--db",
        metavar="PATH",
        help=f"tenant state database (default: the config's state_db, {DEFAULT_STATE_DB})",
    )
    parser.add_argument(
        "--root",
        default=DEFAULT_ROOT,
        metavar="DIR",
        help=f"the local backup chroot for an HQ-root row (default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--sftp-authorized-keys",
        metavar="PATH",
        help="the HQ-root box's key file (default: <root>/.ssh/authorized_keys)",
    )
    parser.add_argument(
        "--sftp-key-comment",
        action="append",
        metavar="TOKEN",
        help="another word that names this tenant in a key's comment (repeatable; "
             "the slug is always one)",
    )
    parser.add_argument(
        "--password-file",
        metavar="PATH",
        help="the user's recovery file (or a bare repository-password file); "
             "opens the repository for the list and the prune",
    )


# --------------------------------------------------------------------- plain pieces


def start_fresh_repository(repo: str) -> bool:
    """ADR-004 §7.4: the old directory of a lost-recovery-file start-fresh."""
    try:
        relative = sftp_url(repo)[3]
    except UsageError:
        return False
    return bool(START_FRESH_RE.match(relative.rstrip("/").split("/")[-1]))


def bx11_repository(repo: str, slug: str) -> bool:
    """True when the row's URL is a BX11 repository, not the old `--root` box.

    The choice is the row's repository URL, not a flag. `t_9d6fb595`'s
    HQ-root rows name the stand-in box's **absolute** chroot path
    (`sftp:user@host:/tenants/<slug>`), which `--root` maps to a local
    directory — the demo and bench migration, kept byte for byte, including
    its "the path must end in the slug" refusal. A BX11 row names the pair's
    own **account-relative** directory (`data/restic`, or `data/repo-<4 hex>`
    after a start-fresh), so it takes the SFTP path with the RW key.
    """
    if not repo.startswith("sftp:"):
        return False
    match = SFTP_HOST_RE.match(repo)
    if not match:
        # Not a shape either branch can use; the HQ-root path names it.
        return False
    relative = repo[match.end():]
    if not relative or relative.startswith("/"):
        return False
    return relative.rstrip("/").split("/")[-1] != slug


def _state_path(ctx: Context, args) -> Path:
    explicit = getattr(args, "db", None)
    if explicit:
        return Path(explicit).expanduser()
    return ctx.config.path_value("state_db", DEFAULT_STATE_DB) or Path(DEFAULT_STATE_DB)


def parse_keep_until(value: Any) -> int:
    """The destroyed row's `keep_until`, as epoch seconds; bad input is usage."""
    if not isinstance(value, str) or not value.strip():
        raise UsageError("the row records no keep_until; nothing is due")
    try:
        moment = datetime.datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise UsageError(
            f"the row's keep_until {value!r} is not an ISO UTC timestamp"
        ) from None
    return int(moment.replace(tzinfo=datetime.timezone.utc).timestamp())


def repository_path(repo: str, root: Path, slug: str) -> Path:
    """The directory a repository's bytes occupy inside the box's chroot.

    The row records the URL the guest wrote to; the box holds the same
    `/tenants/<slug>` below its chroot. A local repository path is used as
    given. Both are resolved, must stay inside `--root`, and must end in the
    slug: this is the last guard before `rmtree`, so a row naming another
    tenant's directory cannot delete it.
    """
    base = Path(root).expanduser().resolve()
    if repo.startswith("sftp:"):
        match = SFTP_HOST_RE.match(repo)
        if not match:
            raise UsageError(f"cannot parse the SFTP repository {repo!r}")
        relative = repo[match.end():].lstrip("/")
        if not relative:
            raise UsageError(f"the repository URL {repo!r} carries no path")
        path = base / relative
    else:
        path = Path(repo).expanduser()
    path = path.resolve()
    if not path.is_relative_to(base) or path == base:
        raise UsageError(
            f"the repository path {path} is outside --root {base}; refusing to delete it"
        )
    if path.name != slug:
        raise UsageError(
            f"the repository path ends in {path.name!r}, not {slug!r}; refusing to delete it"
        )
    return path


def sftp_key_lines(text: str, tokens: list[str]) -> list[tuple[int, str, str]]:
    """(line number, line, comment) for the key lines that name this tenant.

    The box registers a guest's key with the tenant in the comment —
    `odile-backup <slug>` is the convention, and the live box's lines carry it
    — but `backup.py init` writes the guest's kernel hostname, which on the
    image is the same `odile-bench` for every tenant. `--sftp-key-comment`
    names that word when a box used it; the token must name only this tenant,
    because every key line carrying it is dropped. A line matches only when
    one of the tokens is a whole word of the comment: `odl-1` must not match
    `odl-12`.
    """
    found: list[tuple[int, str, str]] = []
    wanted = {token for token in tokens if token}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        kind = next(
            (index for index, part in enumerate(parts)
             if part.startswith(("ssh-", "ecdsa-", "sk-"))),
            None,
        )
        if kind is None or kind + 1 >= len(parts):
            continue
        comment = " ".join(parts[kind + 2:])
        if wanted.intersection(comment.split()):
            found.append((number, line, comment))
    return found


def _read_password(path: str | Path, *, bare: bool = False) -> str:
    """The `Backup password:` line of the user's recovery file.

    With `bare` (the BX11 store of record may point at a plain password file,
    as the drill's does) a file with no recovery header is read whole. The
    minimum is the installer's: 16 characters, so a truncated paste fails here
    and not inside restic.
    """
    file = Path(path).expanduser()
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as error:
        raise UsageError(f"cannot read --password-file {file}: {error}") from None
    match = RECOVERY_PASSWORD_RE.search(text)
    if match:
        password = match.group(1)
    elif bare and text.strip():
        password = text.strip().splitlines()[0].strip()
    else:
        raise UsageError(f"{file} carries no 'Backup password:' line")
    if len(password) < 16:
        raise UsageError(f"the backup password in {file} is shorter than 16 characters")
    return password


def _restic_message(done: subprocess.CompletedProcess[str]) -> str:
    """The one line restic printed about a failure, without the JSON wrapper."""
    for stream in (done.stderr, done.stdout):
        for line in reversed((stream or "").splitlines()):
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                try:
                    value = json.loads(line)
                except ValueError:
                    return line
                if isinstance(value, dict) and value.get("message_type") == "exit_error":
                    message = str(value.get("message", "")).strip().splitlines()
                    return message[0] if message else line
                continue
            return line
    return f"restic exit {done.returncode}"


# --------------------------------------------------------------- the BX11 pair


def store_of_record(ctx: Context, slug: str) -> Path:
    """`/root/.odile/hetzner/odile-<slug>.env` (or the config's store)."""
    settings = provision._backup_settings(ctx)
    return settings.store / f"odile-{slug}.env"


def load_pair(path: Path, slug: str) -> provision.BackupPair:
    """The pair from the store of record; a missing file refuses by name."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise UsageError(
            f"{slug}'s backup store of record is missing: {path}; the pair's ids, "
            "keys and host key live there"
        ) from None
    except OSError as error:
        raise UsageError(f"cannot read the backup store of record {path}: {error}") from None
    try:
        pair = provision.BackupPair.from_env(text)
    except provision.ProviderError as error:
        raise UsageError(f"{path} is not a backup pair file: {error}") from None
    if pair.slug != slug:
        raise UsageError(
            f"{path} records {pair.slug!r}, not {slug!r}; refusing to use another "
            "tenant's pair"
        )
    return pair


def sftp_url(repo: str) -> tuple[str, str, int, str]:
    """(user, host, port, path), read the way restic reads an sftp: URL.

    The port belongs in `sftp://user@host:port/path`; in the
    `sftp:user@host:path` form everything after the host's colon is the path —
    `sftp:user@host:22/repo` is the path `22/repo`, which is why the seed's
    URLs carry no port and the ssh command carries `-p 22` (measured in
    `docs/evidence/hetzner-bx11/08-finding-url-form.md`).
    """
    if repo.startswith("sftp://"):
        match = re.match(
            r"^sftp://(?P<user>[^@/:]+)@(?P<host>[^:/]+)(?::(?P<port>\d+))?"
            r"(?P<path>/.*)$",
            repo,
        )
        if not match:
            raise UsageError(f"cannot parse the SFTP repository {repo!r}")
        user, host = match.group("user"), match.group("host")
        port = int(match.group("port") or SFTP_PORT)
        relative = match.group("path")
    elif repo.startswith("sftp:"):
        user, separator, tail = repo[len("sftp:"):].partition("@")
        if not separator:
            raise UsageError(f"cannot parse the SFTP repository {repo!r}")
        host, separator, relative = tail.partition(":")
        if not separator:
            raise UsageError(f"the repository URL {repo!r} carries no path")
        port = SFTP_PORT
    else:
        raise UsageError(f"{repo!r} is not an sftp: repository")
    relative = relative.lstrip("/")
    parts = relative.split("/")
    if not relative or any(part in ("", "..") for part in parts):
        raise UsageError(f"the repository URL {repo!r} carries no usable path")
    return user, host, port, relative


def _secret_file(directory: Path, name: str, text: str) -> Path:
    """Write one secret beside the call, 0600, and return its path."""
    path = directory / name
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(text)
    return path


def _quote_arg(value: str) -> str:
    """Quote one word for the `sftp.command` string restic hands to a shell."""
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def _ssh_command(transport: Transport) -> str:
    words = [
        "ssh",
        "-i", str(transport.key),
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={transport.known_hosts}",
        "-o", "LogLevel=ERROR",
        "-p", str(transport.port),
        f"{transport.user}@{transport.host}",
        "-s", "sftp",
    ]
    return " ".join(_quote_arg(word) for word in words)


def transport_for(repo: str, pair: provision.BackupPair, directory: Path) -> Transport:
    """The three files and the URL one restic/sftp call needs.

    The row's URL must name the pair's **read-write** sub-account: the rw key
    is what the store of record carries, and pruning another account's
    directory with it would be a silent cross-tenant delete.
    """
    user, host, port, relative = sftp_url(repo)
    if user != pair.subaccount_username or host.lower() != pair.subaccount_server.lower():
        raise UsageError(
            f"the row's repository {repo} is not the read-write sub-account in the "
            f"store of record ({pair.subaccount_username}@{pair.subaccount_server}); "
            "refusing to prune another account's repository"
        )
    key = _secret_file(directory, "rw-key", pair.rw_private_key)
    known_hosts = _secret_file(directory, "known_hosts", pair.known_hosts)
    return Transport(repo=repo, path=relative, user=user, host=host, port=port,
                     key=key, known_hosts=known_hosts)


def _restic_environment(transport: Transport, password_file: Path | None,
                        cache: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("RESTIC_")}
    env.update({
        "RESTIC_REPOSITORY": transport.repo,
        "RESTIC_CACHE_DIR": cache,
        "RESTIC_PROGRESS_FPS": "0",
    })
    if password_file is not None:
        env["RESTIC_PASSWORD_FILE"] = str(password_file)
    return env


def _run_restic(transport: Transport, arguments: list[str], *,
                password_file: Path | None, cache: str,
                timeout: int = RESTIC_TIMEOUT) -> subprocess.CompletedProcess[str]:
    binary = shutil.which("restic")
    if not binary:
        raise ConfigError("restic is not installed on this machine")
    argv = [
        binary,
        "-o", "sftp.command=" + _ssh_command(transport),
        "-o", "sftp.connections=2",
        *arguments,
    ]
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            env=_restic_environment(transport, password_file, cache),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise OdileError(f"cannot run restic {binary}: {error}") from None


def list_snapshots(transport: Transport, *, password_file: Path | None,
                   cache: str) -> dict:
    """`restic snapshots --json` with the pair's key.

    `locked` means the repository did not open because there is no password;
    that is the production case, not a failure, and the caller prunes the
    snapshot objects with the key instead. A **supplied** password that does
    not open it is a refusal: the wrong repository, or a wrong file.
    """
    arguments = ["snapshots", "--json", "--no-lock"]
    if password_file is None:
        arguments.append("--insecure-no-password")
    done = _run_restic(transport, arguments, password_file=password_file, cache=cache)
    if done.returncode == 0:
        try:
            listed = json.loads(done.stdout or "[]")
        except ValueError:
            listed = []
        ids = [str(item.get("id")) for item in listed
               if isinstance(item, dict) and item.get("id")]
        return {"status": "opened", "ids": ids, "count": len(ids)}
    if done.returncode == 12 and password_file is None:
        return {"status": "locked", "ids": [], "count": 0}
    raise RepositoryUnreadable(
        f"restic cannot open the repository {transport.repo}: {_restic_message(done)}"
    )


def forget_snapshot(transport: Transport, ident: str, *, password_file: Path | None,
                    cache: str) -> None:
    """`restic forget --prune <id>`: the snapshot and its unreferenced data."""
    done = _run_restic(transport, ["forget", "--prune", ident],
                       password_file=password_file, cache=cache)
    if done.returncode != 0:
        raise OdileError(
            f"restic forget --prune {ident} failed on {transport.repo}: "
            f"{_restic_message(done)}"
        )


def _sftp_argv(transport: Transport) -> list[str]:
    binary = shutil.which("sftp")
    if not binary:
        raise ConfigError("the OpenSSH sftp client is not installed on this machine")
    return [
        binary, "-b", "-",
        "-i", str(transport.key),
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={transport.known_hosts}",
        "-o", "LogLevel=ERROR",
        "-P", str(transport.port),
        f"{transport.user}@{transport.host}",
    ]


def _sftp_run(transport: Transport, commands: list[str], *,
              timeout: int = SFTP_TIMEOUT) -> str:
    try:
        done = subprocess.run(
            _sftp_argv(transport), input="\n".join(commands) + "\n",
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise OdileError(f"cannot run sftp: {error}") from None
    output = (done.stdout or "") + (done.stderr or "")
    if done.returncode != 0:
        raise OdileError(
            f"sftp to {transport.user}@{transport.host} failed "
            f"(exit {done.returncode}): {output.strip()[-400:]}"
        )
    return output


def sftp_list(transport: Transport, remote: str) -> list[str]:
    """The entries of one remote directory; a missing directory is empty."""
    prefix = remote.rstrip("/") + "/"
    output = _sftp_run(transport, [f"-ls -1 {remote}"])
    names: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            name = line[len(prefix):].rstrip("/")
            if name and "/" not in name:
                names.append(name)
    return names


def prune_with_key(transport: Transport) -> dict:
    """Delete the repository's snapshot objects with the read-write key.

    The end state `restic forget --prune` leaves — `config` and `keys/`, no
    user data — reached without the user's password. Every snapshot is pruned,
    so every data blob is unreferenced; deleting `data/`, `index/` and
    `snapshots/` is the same set of bytes. The ids are the snapshot files'
    own names, so the record still names what was pruned.
    """
    repo = transport.path.strip("/")
    snapshot_names = sftp_list(transport, f"{repo}/snapshots")
    ids = [name for name in snapshot_names if REPOSITORY_ITEM_RE.match(name)]
    data_dirs = sftp_list(transport, f"{repo}/data")
    commands = [
        f"-rm {repo}/snapshots/*",
        f"-rm {repo}/index/*",
        f"-rm {repo}/data/*/*",
        f"-rm {repo}/locks/*",
    ]
    commands += [f"-rmdir {repo}/data/{name}" for name in data_dirs]
    commands += [
        f"-rmdir {repo}/snapshots",
        f"-rmdir {repo}/index",
        f"-rmdir {repo}/data",
        f"-rmdir {repo}/locks",
    ]
    _sftp_run(transport, commands)
    return {"ids": ids, "left": sorted(sftp_list(transport, repo))}


def box_snapshot_plan(settings: provision.BackupSettings) -> dict:
    """`GET /storage_boxes/{id}`: the box's own 04:30 × 10 plan."""
    _status, payload = provision._hetzner_api(
        settings, "GET", f"/storage_boxes/{settings.box_id}")
    plan_now = (payload.get("storage_box") or {}).get("snapshot_plan") or {}
    if not isinstance(plan_now, dict) or not plan_now:
        raise provision.ProviderError(
            f"the box API answered without a snapshot_plan for {settings.box_id}; "
            "refusing to state the expiry window from memory")
    return plan_now


def delete_subaccount(settings: provision.BackupSettings, ident: int) -> str:
    """`DELETE /storage_boxes/{id}/subaccounts/{subaccount_id}`, waited out.

    A 404 is the converged case: the id is gone, which is what the exit wants.
    The write answers an action that is still running, so the poll is the
    completion, not the POST.
    """
    path = f"/storage_boxes/{settings.box_id}/subaccounts/{ident}"
    try:
        _status, payload = provision._hetzner_api(settings, "DELETE", path)
    except provision.ProviderError as error:
        if "answered 404" in str(error):
            return "already absent"
        raise
    provision._await_action(settings, payload)
    return "deleted"


def _settings_for(ctx: Context, pair: provision.BackupPair) -> provision.BackupSettings:
    settings = provision._backup_settings(ctx)
    return replace(settings, box_id=pair.box_id)


def _survivors(pair: provision.BackupPair, plan_now: dict,
               left: list[str] | None = None) -> dict:
    """The two things that outlive the deletion, as the runbook states them.

    Measured on the bench pair (2026-11-26): deleting a sub-account removes
    the *account*, not its home — so the directory stays, with `.ssh` and the
    pruned repository's `config` and `keys/`. What else stays depends on the
    mode: `restic forget --prune` leaves the four object directories, empty,
    while the key-only prune removes them. `left` is the post-prune listing
    when the caller has one (the apply), so the recorded sentence names only
    what that run actually left; without it (the plan) the sentence is
    mode-agnostic. "Empty" means no user data, not a directory with nothing
    in it.
    """
    kept = plan_now.get("max_snapshots")
    hour = plan_now.get("hour")
    minute = plan_now.get("minute")
    if left is None:
        kept_note = (
            "the pruned repository's config and keys/ and .ssh/authorized_keys "
            "— no user data")
    else:
        empty = [name for name in left if name in ("data", "index", "locks", "snapshots")]
        kept_note = (
            "the pruned repository's config and keys/ and .ssh/authorized_keys")
        if empty:
            kept_note += ", its empty " + ", ".join(name + "/" for name in empty) + " directories"
        kept_note += " — no user data"
    return {
        "directory": pair.subaccount_home,
        "directory_note": (
            f"stays after both sub-accounts are deleted: {kept_note}. Only the "
            "box's main account can delete the directory, and HQ does not hold "
            "that credential"
        ),
        "box_snapshots": {
            "hour": hour,
            "minute": minute,
            "kept": kept,
            "note": (
                f"the box's own snapshots ({hour:02d}:{minute:02d} UTC, {kept} kept) "
                "are not deleted by this; the deleted directory can survive up to "
                "about ten days"
                if isinstance(hour, int) and isinstance(minute, int) and isinstance(kept, int)
                else "the box's own snapshots are not deleted by this"
            ),
        },
    }


def _plan_lines(plan_now: dict) -> str:
    hour = plan_now.get("hour")
    minute = plan_now.get("minute")
    kept = plan_now.get("max_snapshots")
    if not all(isinstance(value, int) for value in (hour, minute, kept)):
        return "the box's own snapshot plan (reported above)"
    return (f"the box's own snapshots: {hour:02d}:{minute:02d} UTC, {kept} kept — "
            f"the deleted directory can survive up to about ten days")


# --------------------------------------------------------------------- the check


def check_repository(path: Path, *, password: str | None, snapshot: str) -> dict:
    """`restic -r <path> snapshots` as the purge's control check.

    With the user's recovery file the repository opens and the recorded snapshot
    is looked for by id. Without it the check still runs, with restic's empty
    password explicitly allowed: restic's "wrong password" (exit 12) proves the
    directory is a restic repository that this password does not open — the key
    is the user's. No file name and no byte is ever read.
    """
    binary = shutil.which("restic")
    if not binary:
        raise ConfigError("restic is not installed on this machine")
    arguments = ["snapshots", "--json", "--no-lock"]
    if password is None:
        arguments.append("--insecure-no-password")
    # An ambient RESTIC_* wins over what this call sets (RESTIC_PASSWORD over
    # the password file, RESTIC_REPOSITORY_FILE over -r, a password command
    # beside either), so the purge clears every one of them first.
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("RESTIC_")
    }
    environment.update(
        {
            "RESTIC_REPOSITORY": str(path),
            "RESTIC_CACHE_DIR": os.environ.get("RESTIC_CACHE_DIR") or tempfile.gettempdir(),
            "RESTIC_PROGRESS_FPS": "0",
        }
    )
    with tempfile.TemporaryDirectory(prefix="odile-purge-") as directory:
        password_file = Path(directory) / "restic-password"
        handle = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(password or "")
        environment["RESTIC_PASSWORD_FILE"] = str(password_file)
        try:
            try:
                done = subprocess.run(
                    [binary, *arguments], capture_output=True, text=True, env=environment,
                    timeout=6 * 60 * 60,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise OdileError(f"cannot run restic {binary}: {error}") from None
        finally:
            try:
                password_file.unlink()
            except OSError:
                pass
    if done.returncode == 0:
        try:
            listed = json.loads(done.stdout or "[]")
        except ValueError:
            listed = []
        identifiers = [item.get("id", "") for item in listed if isinstance(item, dict)]
        return {
            "status": "opened",
            "snapshots": len(identifiers),
            "snapshot_present": (snapshot in identifiers) if snapshot else None,
        }
    if done.returncode == 12 and password is None:
        return {"status": "locked", "snapshots": None, "snapshot_present": None}
    raise RepositoryUnreadable(
        f"restic cannot open the repository {path}: {_restic_message(done)}"
    )


def _check_structure(path: Path) -> None:
    """A restic repository has a config and at least one key file.

    restic answers "wrong password or no key found" (exit 12) for a real
    repository it cannot open **and** for a directory with a stray `config`
    and no keys, so the structure is the discriminator the exit-4 refusal
    needs. `restic key remove` cannot remove the last key, so a keyless
    directory is never a repository.
    """
    keys = path / "keys"
    if not (path / "config").is_file() or not keys.is_dir() or not any(keys.iterdir()):
        raise RepositoryUnreadable(
            f"restic cannot open the repository {path}: it has no config file or no key"
        )


def remove_repository(path: Path) -> bool:
    """Delete the repository directory; False when it was already gone."""
    if not path.exists():
        return False
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as error:
        raise OdileError(f"cannot delete the repository {path}: {error}") from None
    if path.exists():
        raise OdileError(f"the repository directory {path} is still there after the delete")
    return True


def drop_sftp_keys(path: Path, tokens: list[str]) -> list[int]:
    """Remove the tenant's key line(s); return the line numbers dropped.

    A missing `authorized_keys` is not an error — a box that never registered
    the guest's key has no access to drop — and no matching line means the
    access is already gone. The rewrite is atomic and keeps the file's owner
    and mode.
    """
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
        info = path.stat()
    except (OSError, UnicodeDecodeError) as error:
        raise OdileError(f"cannot read {path}: {error}") from None
    doomed = {number for number, _, _ in sftp_key_lines(text, tokens)}
    if not doomed:
        return []
    kept = [
        line
        for number, line in enumerate(text.splitlines(keepends=True), start=1)
        if number not in doomed
    ]
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=path.name + ".", suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.writelines(kept)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(handle.name, info.st_uid, info.st_gid)
        os.chmod(handle.name, info.st_mode & 0o777)
        os.replace(handle.name, path)
    except OSError as error:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise OdileError(f"cannot rewrite {path}: {error}") from None
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    return sorted(doomed)


# --------------------------------------------------------------------- the command


def _iso(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _already_deleted(ctx: Context, slug: str, backup: dict, repo: str) -> Report:
    """The second run: the row already records the pair's deletion."""
    deleted_at = backup.get("subaccount_deleted_at")
    accounts = backup.get("subaccounts") or {}
    pruned = backup.get("pruned") or {}
    survivors = backup.get("survivors") or {}
    lines = [
        f"  deleted_at: {deleted_at}",
        f"  repository: {repo}",
    ]
    for name in ("read_write", "read_only"):
        facts = accounts.get(name) or {}
        if facts:
            lines.append(
                f"  subaccount: {facts.get('name')} {facts.get('id')} "
                f"({facts.get('result')})"
            )
    if pruned:
        lines.append(f"  pruned:     {pruned.get('snapshots')} snapshot(s) via {pruned.get('mode')}")
    if survivors:
        lines.append(
            f"  survives:   {survivors.get('directory')} (stays, no user data: .ssh and "
            "the repository's config and keys/; the box's main account would delete it)")
        box = survivors.get("box_snapshots") or {}
        if box.get("note"):
            lines.append(f"  box:        {box['note']}")
    return result(NAME, ctx, ["purge: already deleted", *lines],
                  dry_run=not ctx.mutate, slug=slug, already_deleted=True,
                  subaccount_deleted_at=deleted_at, repository=repo,
                  subaccounts=accounts, survivors=survivors)


def _already_pruned(ctx: Context, slug: str, backup: dict, repo: str,
                    done: dict) -> Report:
    lines = [
        f"  pruned_at:  {done.get('at')}",
        f"  repository: {repo}",
        f"  snapshots:  {done.get('snapshots')} pruned via {done.get('mode')}",
        "  subaccounts: kept (a start-fresh directory; the tenant still writes here)",
    ]
    return result(NAME, ctx, ["purge: already pruned", *lines],
                  dry_run=not ctx.mutate, slug=slug, already_pruned=True,
                  repository=repo, pruned=done)


def _purge_bx11(ctx: Context, args, *, slug: str, tenant, backup: dict, repo: str,
                db: Path) -> Report:
    """One `sftp:` row: the BX11 exit (or the start-fresh directory's prune)."""
    relative = sftp_url(repo)[3]
    fresh = start_fresh_repository(repo)
    if fresh:
        done = next(
            (item for item in (backup.get("prunes") or [])
             if isinstance(item, dict) and item.get("repo") == repo),
            None,
        )
        if done is not None:
            return _already_pruned(ctx, slug, backup, repo, done)
    elif isinstance(backup.get("subaccount_deleted_at"), str) and backup["subaccount_deleted_at"]:
        return _already_deleted(ctx, slug, backup, repo)

    if not fresh and tenant.state != "destroyed":
        raise UsageError(f"{slug} is {tenant.state}, not destroyed; nothing is due")

    store_path = store_of_record(ctx, slug)
    pair = load_pair(store_path, slug)
    settings = _settings_for(ctx, pair)
    now = int(time.time())

    with tempfile.TemporaryDirectory(prefix="odile-purge-") as directory:
        workspace = Path(directory)
        transport = transport_for(repo, pair, workspace)
        password = _read_password(args.password_file, bare=True) if args.password_file else None
        password_file = (_secret_file(workspace, "restic-password", password)
                         if password is not None else None)
        cache = str(workspace / "cache")
        listing = list_snapshots(transport, password_file=password_file, cache=cache)
        mode = "restic" if listing["status"] == "opened" else "key-only"
        ids = list(listing["ids"])
        if mode == "key-only":
            # The repository password is the user's, so the snapshot files'
            # own names are the ids; the plan states them before it prunes.
            ids = [name for name in sftp_list(transport, f"{relative}/snapshots")
                   if REPOSITORY_ITEM_RE.match(name)]
        plan_now = {} if fresh else box_snapshot_plan(settings)
        survivors = {} if fresh else _survivors(pair, plan_now)

        if not ctx.mutate:
            actions: list[dict[str, Any]] = [
                {
                    "action": "backup.verify",
                    "repository": repo,
                    "via": ("restic snapshots --json" if mode == "restic"
                            else "the read-write key over sftp (the password is the user's)"),
                    "snapshots": len(ids),
                    "ids": ids,
                },
                {
                    "action": "backup.prune",
                    "repository": repo,
                    "via": ("restic forget --prune <id>" if mode == "restic"
                            else "delete snapshots/, index/ and data/ with the read-write key"),
                    "snapshots": len(ids),
                    "ids": ids,
                },
            ]
            if not fresh:
                for wanted, name in (("read_only", f"odile-{slug}-ro"),
                                     ("read_write", f"odile-{slug}-rw")):
                    ident = (pair.readonly_subaccount_id if wanted == "read_only"
                             else pair.subaccount_id)
                    actions.append({
                        "action": "subaccount.delete",
                        "box": settings.box_id,
                        "id": ident,
                        "name": name,
                    })
                actions.append({
                    "action": "state.record",
                    "slug": slug,
                    "detail": "detail.backup.subaccount_deleted_at",
                    "keep_until": backup.get("keep_until"),
                })
            else:
                actions.append({
                    "action": "state.record",
                    "slug": slug,
                    "detail": "detail.backup.prunes (this repository)",
                    "keep_until": backup.get("keep_until"),
                })
            notes = [
                f"the store of record is {store_path} (0600); its read-write key is the "
                "only credential used, and it never reaches argv",
            ]
            if mode == "restic":
                notes.append(
                    "the password file opened the repository; the prune runs "
                    "restic forget --prune for every snapshot")
            else:
                notes.append(
                    "the repository password is the user's: restic answered wrong-password, "
                    "so the snapshot objects are deleted with the read-write key and only "
                    "config and keys/ remain")
            if fresh:
                notes.append(
                    "a repo-<4 hex> start-fresh directory: pruned inside the same "
                    "sub-account, no API call, the pair stays")
            else:
                notes.append(
                    f"survives: {survivors['directory']} (stays, no user data: .ssh and "
                    "the repository's config and keys/; only the box's main account can "
                    "delete it, and HQ does not hold it)")
                notes.append(_plan_lines(plan_now))
            notes.append(
                f"the 30-day promise and keep_until {backup.get('keep_until')} are unchanged")
            notes.append(f"run: odilectl purge {slug} --apply")
            return plan(NAME, ctx, actions, notes, slug=slug, repository=repo,
                        mode=mode, snapshots=len(ids), ids=ids,
                        keep_until=backup.get("keep_until"))

        # Past the date: prune, delete both halves, record.
        if mode == "restic":
            for ident in ids:
                forget_snapshot(transport, ident, password_file=password_file, cache=cache)
            after = list_snapshots(transport, password_file=password_file, cache=cache)
            if after["status"] != "opened" or after["ids"]:
                raise OdileError(
                    f"the repository {repo} still lists {len(after['ids'])} snapshot(s) "
                    "after the prune")
            left = sorted(sftp_list(transport, relative))
        else:
            pruned = prune_with_key(transport)
            ids = list(pruned["ids"])
            left = pruned["left"]
        if not fresh:
            # The recorded sentence is the tree this run left, not the plan's
            # mode-agnostic one: key-only removes the object directories.
            survivors = _survivors(pair, plan_now, left)
        pruned = {
            "at": _iso(now),
            "mode": mode,
            "repo": repo,
            "repository": repo,
            "path": relative,
            "snapshots": len(ids),
            "ids": ids,
            "left": left,
        }
        lines = [
            f"  repository: {repo}",
            (f"  prune:      {len(ids)} snapshot(s) via restic forget --prune: "
             + (", ".join(ident[:8] for ident in ids) if ids else "none"))
            if mode == "restic" else
            (f"  prune:      {len(ids)} snapshot(s) deleted with the read-write key "
             "(the repository password is the user's)"),
        ]
        left_files = [name for name in left
                      if name not in ("data", "index", "locks", "snapshots")]
        left_empties = [name for name in left
                        if name in ("data", "index", "locks", "snapshots")]
        left_line = f"  left:       {' '.join(left_files) if left_files else 'nothing'}"
        if left_empties:
            left_line += f" ({', '.join(name + '/' for name in left_empties)} empty)"
        lines.append(left_line)
        recorded: dict[str, Any] = {}
        results: dict[str, dict[str, Any]] = {}
        if fresh:
            lines.append(
                "  subaccounts: kept (a start-fresh directory; the tenant still writes here)")
        else:
            for wanted, name, ident in (
                ("read_only", f"odile-{slug}-ro", pair.readonly_subaccount_id),
                ("read_write", f"odile-{slug}-rw", pair.subaccount_id),
            ):
                outcome = delete_subaccount(settings, ident)
                results[wanted] = {
                    "id": ident, "name": name, "result": outcome, "deleted_at": pruned["at"],
                }
                lines.append(f"  subaccount: {name} {ident} {outcome}")
            recorded.update({
                "pruned": pruned,
                "subaccounts": results,
                "subaccount_deleted_at": pruned["at"],
                "survivors": survivors,
            })
            lines.append(
                f"  survives:   {survivors['directory']} (stays, no user data: .ssh and "
                "the repository's config and keys/; the box's main account would delete it)")
            lines.append(f"  box:        {_plan_lines(plan_now)}")
            lines.append(
                f"  unchanged:  keep_until {backup.get('keep_until')} and the 30-day promise")
        # Re-read the row under the writer so a concurrent change is merged,
        # not overwritten; a start-fresh appends one entry, the exit records
        # its own keys beside whatever `destroy` and `offline` wrote.
        with StateStore.open(db) as writer:
            current = writer.get(slug)
            if not fresh and current.state != "destroyed":
                raise UsageError(
                    f"{slug} is {current.state}, not destroyed; the row moved during the purge")
            merged = dict((current.detail or {}).get("backup") or {})
            if fresh:
                merged["prunes"] = list(merged.get("prunes") or []) + [pruned]
                merged["last_repo_pruned_at"] = pruned["at"]
            else:
                merged.update(recorded)
            writer.note(slug, detail={"backup": merged})
        lines.append(
            f"  state:      detail.backup."
            f"{'prunes' if fresh else 'subaccount_deleted_at'} at {pruned['at']} in {db}")
        return applied(NAME, ctx, lines, slug=slug, repository=repo, mode=mode,
                       snapshots=len(ids), ids=ids, pruned=pruned,
                       subaccount_deleted_at=None if fresh else pruned["at"],
                       subaccounts=None if fresh else results,
                       survivors=None if fresh else survivors)


def run(ctx: Context, args) -> Report:
    slug = args.slug.strip()
    if not slug:
        raise UsageError("a tenant slug cannot be empty")
    tokens = [slug, *(args.sftp_key_comment or [])]
    db = _state_path(ctx, args)
    root = Path(args.root).expanduser()
    authorized = (
        Path(args.sftp_authorized_keys).expanduser()
        if args.sftp_authorized_keys
        else root / ".ssh" / "authorized_keys"
    )
    bx11: tuple[Any, dict, str] | None = None
    store = StateStore.open(db)
    try:
        try:
            tenant = store.get(slug)
        except UnknownTenant:
            raise UsageError(f"no tenant named {slug!r} in {db}") from None
        backup = (tenant.detail or {}).get("backup")
        if not isinstance(backup, dict) or not backup:
            raise UsageError(
                f"{slug} records no backup (tenant_state.detail.backup); nothing to purge"
            )
        repo = backup.get("repo")
        if not repo:
            raise UsageError(
                f"{slug} records no repository (detail.backup.repo is null); "
                "nothing to purge"
            )
        if tenant.state != "destroyed" and not (
                bx11_repository(repo, slug) and start_fresh_repository(repo)):
            raise UsageError(f"{slug} is {tenant.state}, not destroyed; nothing is due")
        if not bx11_repository(repo, slug):
            # The HQ-root path, exactly as `t_9d6fb595` shipped it: its own
            # idempotency key (`purged`) is checked before the date, as before.
            purged = backup.get("purged")
            if isinstance(purged, dict) and purged.get("at"):
                lines = [
                    f"  purged_at:  {purged.get('at')}",
                    f"  repository: {backup.get('repo')}",
                ]
                if purged.get("path"):
                    lines.append(f"  path:       {purged['path']}")
                return result(NAME, ctx, ["purge: already purged", *lines],
                              dry_run=not ctx.mutate,
                              slug=slug, already_purged=True, purged=purged,
                              repository=backup.get("repo"))
        due = parse_keep_until(backup.get("keep_until"))
        now = int(time.time())
        if now < due:
            remaining = (due - now) / 86400
            raise UsageError(
                f"{slug} is not due: keep_until {backup['keep_until']} "
                f"({remaining:.1f} days away)"
            )
        if bx11_repository(repo, slug):
            # ODL-935: the BX11 destination, chosen by the row's URL. The
            # older store connection closes first (below), so the exit's own
            # record is a single writer.
            bx11 = (tenant, backup, repo)
            path = root  # unused on this branch
            snapshot, keys = "", []
        else:
            path = repository_path(repo, root, slug)
            snapshot = backup.get("snapshot") or ""
            keys = (
                sftp_key_lines(authorized.read_text(encoding="utf-8"), tokens)
                if authorized.is_file()
                else []
            )
        if not ctx.mutate and bx11 is None:
            actions = [
                {
                    "action": "backup.verify",
                    "repository": repo,
                    "path": str(path),
                    "snapshot": snapshot or None,
                    "command": f"restic -r {path} snapshots --no-lock",
                    "password_file": args.password_file or None,
                },
                {
                    "action": "backup.delete",
                    "path": str(path),
                    "result": "present" if path.exists() else "already absent",
                },
                {
                    "action": "sftp.revoke",
                    "file": str(authorized),
                    "lines": [number for number, _, _ in keys],
                    "comments": tokens,
                    "result": (
                        f"{len(keys)} line(s)" if keys
                        else ("no line names the slug" if authorized.is_file()
                              else "no authorized_keys")
                    ),
                },
                {
                    "action": "state.purge",
                    "slug": slug,
                    "detail": "detail.backup.purged",
                    "keep_until": backup["keep_until"],
                },
            ]
            notes = [
                "the repository password is the user's: --password-file opens the repository "
                "for the check, and without it restic's wrong-password answer still proves the "
                "directory is a restic repository",
                f"run: odilectl purge {slug} --root {root} --apply",
                "a second run says already purged; the date is the row's",
            ]
            return plan(NAME, ctx, actions, notes, slug=slug,
                        keep_until=backup["keep_until"], repository=repo,
                        path=str(path), snapshot=snapshot or None)
    finally:
        store.close()

    if bx11 is not None:
        tenant_now, backup_now, repo_now = bx11
        return _purge_bx11(ctx, args, slug=slug, tenant=tenant_now,
                           backup=backup_now, repo=repo_now, db=db)

    # Past the date: the check, the delete, the key and the record.
    status, snapshots, snapshot_present, existed = "absent", None, None, path.exists()
    if existed:
        if not path.is_dir():
            # restic 0.18 retries a non-directory repository path for hours;
            # the refusal is the same "cannot open" and names it.
            raise RepositoryUnreadable(
                f"restic cannot open the repository {path}: the path is not a directory"
            )
        _check_structure(path)
        if authorized.is_file() and not keys:
            # The access is part of the purge, so a key file with no line for
            # this tenant refuses before the repository is deleted: a silent
            # zero would leave the tenant's key working on the box.
            raise UsageError(
                f"no line of {authorized} names {slug}; pass --sftp-key-comment TOKEN "
                "(a token that names only this tenant — every line carrying it is "
                "dropped — for a box that registered the image's hostname) or drop "
                "the line by hand"
            )
        password = _read_password(args.password_file) if args.password_file else None
        check = check_repository(path, password=password, snapshot=snapshot)
        status = check["status"]
        snapshots = check["snapshots"]
        snapshot_present = check["snapshot_present"]
    lines = [f"  repository: {repo}", f"  path:       {path}"]
    if existed:
        if status == "opened":
            bits = f"opened, {snapshots} snapshot(s)"
            if snapshot_present is not None:
                bits += (
                    f"; the recorded {snapshot[:8]} is present"
                    if snapshot_present
                    else f"; the recorded {snapshot[:8]} is NOT present"
                )
            lines.append(f"  restic:     {bits}")
        else:
            lines.append("  restic:     present, not opened (its password is the user's)")
    else:
        lines.append("  restic:     already absent (nothing to verify)")
    removed = remove_repository(path)
    lines.append(
        f"  delete:     removed {path}" if removed
        else f"  delete:     {path} was already absent"
    )
    dropped = drop_sftp_keys(authorized, tokens)
    if dropped:
        lines.append(f"  sftp:       dropped {len(dropped)} line(s) from {authorized}")
    else:
        lines.append(
            f"  sftp:       no line of {authorized} names {slug}"
            if authorized.is_file()
            else f"  sftp:       no authorized_keys at {authorized}; nothing to drop"
        )
    purged = {
        "at": _iso(int(time.time())),
        "path": str(path),
        "repo": repo,
        "snapshot": snapshot or None,
        "restic": status,
        "snapshots": snapshots,
        "snapshot_present": snapshot_present,
        "sftp_keys_dropped": len(dropped),
        "already_absent": not removed,
    }
    store = StateStore.open(db)
    try:
        current = store.get(slug)
        if current.state != "destroyed":
            raise UsageError(
                f"{slug} is {current.state}, not destroyed; the row moved during the purge"
            )
        merged = dict((current.detail or {}).get("backup") or {})
        merged["purged"] = purged
        store.note(slug, detail={"backup": merged})
    finally:
        store.close()
    lines.append(f"  state:      detail.backup.purged at {purged['at']} in {db}")
    return applied(NAME, ctx, lines, slug=slug, repository=repo, path=str(path),
                   snapshot=snapshot or None, restic=status, snapshots=snapshots,
                   snapshot_present=snapshot_present, sftp_keys_dropped=len(dropped),
                   already_absent=not removed, purged=purged)
