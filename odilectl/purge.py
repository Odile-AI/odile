"""`purge` — delete a destroyed tenant's encrypted repository on its keep_until.

ODL-218 follow-up. `odilectl destroy` records `detail.backup` (the repository
URL, the last heartbeat's snapshot and the date 30 days out) and one
`destroyed` event. The repository is kept until that date, because the user may
still want it; this command is the other half of the offboard path: it reads the
destroyed row, refuses before `keep_until`, and past it

1. runs `restic -r <path> snapshots --no-lock` as the control check,
2. deletes the repository's directory on the backup box,
3. drops the guest's line from the box's `authorized_keys`,
4. records `detail.backup.purged` on the row.

A second run says `already purged` and writes nothing, so "run it again" is true
here too. The date is the **row's**: a re-run cannot push it forward.

It is a control-plane command, not a `scripts/backup.py` verb: that program is
embedded byte-for-byte in the signed installer, and the facts this command reads
(the tenant row) live on HQ. The box has no agent (`t_8eb17a54` buys it), so the
operator runs this on the date the row names:

    odilectl purge odl-7f3a2c --root /srv/odile-backup           # the plan
    odilectl purge odl-7f3a2c --root /srv/odile-backup --apply   # delete

The repository password is the user's, so the control check opens the
repository only when `--password-file` names the user's recovery file; without
it restic's "wrong password" (exit 12) still proves the directory is a restic
repository that this password does not open — the key is the user's, which is
why nothing here reads a file name or a byte.

The box registers each guest's key with the tenant in its comment —
`odile-backup <slug>` — and the purge drops every line whose comment carries
the slug (or the token named with `--sftp-key-comment` — a token that names
only this tenant, since every line carrying it is dropped — for a box that
registered the image's hostname). A key file with no such line refuses before the
repository is deleted: dropping the access is part of the purge, not a silent
zero. A missing file, or a repository directory that is already gone, is the
converged case and records what it found.

Exit codes: 0 ok, 2 usage or a refusal, 1 a failed delete or rewrite, 4 a
repository restic cannot open, 5 config (no restic on this machine).
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
from pathlib import Path
from typing import Any

from .core import Context, Report, applied, plan, result
from .errors import ConfigError, OdileError, UsageError
from .state import DEFAULT_STATE_DB, StateStore, UnknownTenant

NAME = "purge"
HELP = "Delete a destroyed tenant's encrypted repository, once its keep_until has passed."
EXAMPLE = "odilectl purge odl-7f3a2c --root /srv/odile-backup --apply"

# The backup box's SFTP chroot: the seed's `/tenants/<slug>` lands below it.
DEFAULT_ROOT = "/srv/odile-backup"

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
        help=f"the backup box's SFTP chroot (default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--sftp-authorized-keys",
        metavar="PATH",
        help="the box's key file (default: <root>/.ssh/authorized_keys)",
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
        help="the user's recovery file; opens the repository for the control check",
    )


# --------------------------------------------------------------------- plain pieces


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


def _read_password(path: str | Path) -> str:
    """The `Backup password:` line of the user's recovery file."""
    file = Path(path).expanduser()
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as error:
        raise UsageError(f"cannot read --password-file {file}: {error}") from None
    match = RECOVERY_PASSWORD_RE.search(text)
    if not match:
        raise UsageError(f"{file} carries no 'Backup password:' line")
    password = match.group(1)
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
    store = StateStore.open(db)
    try:
        try:
            tenant = store.get(slug)
        except UnknownTenant:
            raise UsageError(f"no tenant named {slug!r} in {db}") from None
        if tenant.state != "destroyed":
            raise UsageError(f"{slug} is {tenant.state}, not destroyed; nothing is due")
        backup = (tenant.detail or {}).get("backup")
        if not isinstance(backup, dict) or not backup:
            raise UsageError(
                f"{slug} records no backup (tenant_state.detail.backup); nothing to purge"
            )
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
        repo = backup.get("repo")
        if not repo:
            raise UsageError(
                f"{slug} records no repository (detail.backup.repo is null); "
                "nothing to purge"
            )
        due = parse_keep_until(backup.get("keep_until"))
        now = int(time.time())
        if now < due:
            remaining = (due - now) / 86400
            raise UsageError(
                f"{slug} is not due: keep_until {backup['keep_until']} "
                f"({remaining:.1f} days away)"
            )
        path = repository_path(repo, root, slug)
        snapshot = backup.get("snapshot") or ""
        keys = (
            sftp_key_lines(authorized.read_text(encoding="utf-8"), tokens)
            if authorized.is_file()
            else []
        )
        if not ctx.mutate:
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
