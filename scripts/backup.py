#!/usr/bin/python3
"""Tenant backup — restic on the guest, a password only its owner holds (ODL-215).

The repository password is generated on the machine at install, written to
`/etc/odile/recovery.txt`, downloaded by the user on the first-visit page, and
held by nobody at HQ. This program is the only consumer of that line besides
the installer: it reads it, puts it in a 0600 file for exactly one restic call,
and never prints it. HQ sees the repository URL (from `/etc/odile/backup-repo.txt`,
written from the seed) and, at most, the status record this program writes.

    backup.py init        # generate the SFTP key, record the host key, restic init
    backup.py run         # one nightly snapshot + 7 daily / 4 weekly, then status
    backup.py status      # print /var/lib/odile/backup.json (the HQ-visible fields)
    backup.py restore latest --target /var/tmp/restore
    backup.py snapshot-info latest          # the id and time a restore would take
    backup.py drill       # create a canary, back it up, delete it, restore it, compare
    backup.py verify      # the recovery password opens the repo; a wrong one does not
    backup.py heartbeat   # post the status to HQ as a signed heartbeat (optional)
    backup.py install     # write and enable the systemd unit + timer

Every command has `--dry-run`; a second run is safe. `run` writes the status
file only after restic succeeded, and keeps the previous `last_success_at`
when a run fails — a silent failure becomes a freshness alert, not a lie.

The SFTP transport key is generated here, on the machine, and only its public
half leaves (the provisioning step and a rebuild register it again). The
repository password is the one thing a rebuild needs from the user's recovery
file; the runbook is `docs/runbooks/backup.md`.

Exit codes: 0 ok, 1 a restic/verify failure, 2 usage, 3 a missing prerequisite
(no restic, no recovery file, no repository URL), 12 a wrong repository password
(restic's own code, so a rebuilt machine's first visit can tell it from anything else).
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_REPO_FILE = "/etc/odile/backup-repo.txt"
DEFAULT_RECOVERY = "/etc/odile/recovery.txt"
DEFAULT_KEY_DIR = "/etc/odile/backup-ssh"
DEFAULT_HOME = "/home/tenant"
DEFAULT_STATUS = "/var/lib/odile/backup.json"
DEFAULT_DRILL = "/var/lib/odile/backup-drill.json"
DEFAULT_INSTALL_PATH = "/usr/local/lib/odile/backup.py"
DEFAULT_HEARTBEAT_KEY_DIR = "/etc/odile/heartbeat"
# The production name. Measured 2026-11-13: no Caddy route answers it yet
# (GET 404 / POST 405), so `install` must be given a reachable receiver —
# the live tenant uses http://203.0.113.139:8099/heartbeat. t_19148cba owns
# making this default true (the installer places the units and the route).
DEFAULT_HEARTBEAT_URL = "https://odile.fyi/heartbeat"
DEFAULT_KEEP_DAILY = 7
DEFAULT_KEEP_WEEKLY = 4
STATUS_SCHEMA = 1
# ADR-004 v3 §4.1/§7.4: the per-repository fair-use envelope. A repository over
# it is `status: over_cap` and the next run uploads nothing, so growth stops at
# one night's delta above 2.0 GB. It never deletes: the last good snapshot
# stays until an operator prunes.
DEFAULT_TENANT_CAP_BYTES = 2_000_000_000
TENANT_CAP_ENV = "BACKUP_TENANT_CAP_BYTES"
# ADR-004 v3 §4.3: the box publishes 10 concurrent connections (25 measured),
# so one run asks for two and five runs share the published ceiling. restic's
# own default is 5, which would put two runs over the published number.
SFTP_CONNECTIONS = 2
# The nightly window: 02:00-06:00 UTC, one stable minute per slug.
TIMER_WINDOW_START_MINUTE = 2 * 60
TIMER_WINDOW_MINUTES = 240
DRILL_SCHEMA = 1

# Trees that come back from the signed installer and the manifest, not from the
# repository: the Hermes clone, its pinned toolchains and caches, and the usual
# build droppings. ADR-003 §3.3 makes the same call for HQ — a file earns its
# place in a backup only by being irreplaceable. They are named relative to
# --home, so an operator can see exactly what is skipped.
DEFAULT_EXCLUDES = (
    ".hermes/hermes-agent",
    ".hermes/tools",
    ".hermes/installs",
    ".hermes/cache",
    ".hermes/logs",
    ".hermes/audio_cache",
    ".hermes/image_cache",
    ".cache",
    ".npm",
    ".local/share/uv",
    "node_modules",
    "__pycache__",
)


def exclude_arguments(home: Path, extra: list[str] | None = None) -> list[str]:
    """`--exclude` for the rebuildable trees, then the caller's own patterns."""
    arguments: list[str] = ["--exclude", "*.tmp", "--exclude-caches"]
    for name in DEFAULT_EXCLUDES:
        arguments += ["--exclude", str(home / name)]
    for pattern in extra or []:
        arguments += ["--exclude", pattern]
    return arguments

# The installer's own grammar (installer/odile_install.py:parse_recovery). One
# expression, so the file the page writes and the file this reads cannot drift.
RECOVERY_PASSWORD_RE = re.compile(r"^Backup password:\s*(\S+)\s*$", re.M)
SFTP_HOST_RE = re.compile(
    r"^(?:sftp:(?://)?)?(?P<user>[^@/:]+)@(?P<host>[^:/]+)"
    r"(?::(?P<port>\d+))?(?::|/)"
)


class BackupError(Exception):
    """A failure this program reports in one line, without a traceback."""

    exit_code = 1


class BadInput(BackupError):
    exit_code = 2


class Missing(BackupError):
    exit_code = 3


class WrongPassword(BackupError):
    """The recovery file's password does not open the repository (restic's exit 12)."""

    exit_code = 12


# --------------------------------------------------------------------------- plain helpers


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise BackupError(f"cannot read {path}: {error}") from None
    return value if isinstance(value, dict) else {}


def write_json(path: Path, payload: dict, *, mode: int = 0o600) -> None:
    """Write a status record atomically; a killed run never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=path.name + ".", suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            os.fchmod(handle.fileno(), mode)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_recovery_password(path: str | Path) -> str:
    """The `Backup password:` line of the user's recovery file, or Missing.

    The file is the user's document (mode 0600 on the guest). A blank or short
    value is refused here rather than sent to restic, so a truncated file reads
    as "no password" instead of "wrong password".
    """
    text = Path(path).expanduser()
    try:
        body = text.read_text(encoding="utf-8")
    except OSError as error:
        raise Missing(f"cannot read the recovery file {text}: {error}") from None
    match = RECOVERY_PASSWORD_RE.search(body)
    if not match:
        raise Missing(f"{text} carries no 'Backup password:' line")
    password = match.group(1)
    if len(password) < 16:
        raise Missing(f"the backup password in {text} is {len(password)} characters; 16 or more")
    return password


def resolve_repo(value: str | None, repo_file: str) -> str:
    """`--repo` wins; otherwise the seed-written file. One token, no whitespace."""
    if value:
        repo = value.strip()
    else:
        path = Path(repo_file).expanduser()
        try:
            repo = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise Missing(f"cannot read the repository URL {path}: {error}") from None
    if not repo:
        raise Missing("the repository URL is empty")
    if any(char.isspace() for char in repo):
        raise BadInput(f"the repository URL has whitespace: {repo!r}")
    return repo


def sftp_target(repo: str) -> tuple[str, str, str | None] | None:
    """(user, host, port) for an sftp repository, or None for a local one.

    The colon form has **no port field**: `sftp:user@host:22/path` is the path
    `22/path` (the drill measured a literal `22/` directory), so a colon-form
    URL that carries one is refused by name rather than sent to restic. The
    port lives in `sftp.command` (`-p 22`). The `sftp://host:port/path` form
    does carry one, and keeps it.
    """
    if not repo.startswith("sftp:"):
        return None
    match = SFTP_HOST_RE.match(repo)
    if not match:
        raise BadInput(f"cannot parse the SFTP repository {repo!r}")
    port = match.group("port")
    if port and not repo.startswith("sftp://"):
        path = repo.split(":", 2)[2]
        raise BadInput(
            f"the colon form has no port field: {repo!r} is the path {path!r}, not a "
            f"port, so restic would create a directory named {path.split('/')[0]!r}; "
            "write sftp:user@host:repo and let sftp.command carry -p 22"
        )
    return match.group("user"), match.group("host"), port


def restic_binary() -> str:
    found = shutil.which("restic")
    if not found:
        raise Missing("restic is not installed (the image pins restic 0.18.0)")
    return found


# --------------------------------------------------------------------------- restic calls


def sftp_args(repo: str, key: str | Path, known_hosts: str | Path,
              connections: int = SFTP_CONNECTIONS) -> list[str]:
    """`-o sftp.command=...` so restic uses this machine's key, not root's agent.

    Two restic options, in this order: the transport command (the key, the
    pinned host file, and `-p 22` for the colon form, which cannot carry a
    port itself) and the connection ceiling (`sftp.connections=2` — ADR-004 v3
    §4.3: five runs share the box's published ten).
    """
    target = sftp_target(repo)
    if target is None:
        return []
    user, host, port = target
    command = [
        "ssh",
        "-i", str(Path(key).expanduser()),
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={Path(known_hosts).expanduser()}",
        "-o", "LogLevel=ERROR",
        # The colon form carries no port, so the box's steady-state 22 is named
        # here; a `sftp://host:port/path` URL names its own.
        "-p", port or "22",
    ]
    # `-s sftp` is the remote command, so it must follow the destination.
    command += [f"{user}@{host}", "-s", "sftp"]
    return [
        "-o", "sftp.command=" + " ".join(_quote_arg(part) for part in command),
        "-o", f"sftp.connections={int(connections)}",
    ]


def _quote_arg(value: str) -> str:
    """Quote one word for the sftp.command string restic hands to a shell."""
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def timer_offset(slug: str) -> int:
    """The slug's stable minute offset inside the nightly window (ADR-004 §4.3)."""
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()
    return int(digest[:2], 16) % TIMER_WINDOW_MINUTES


def timer_start(slug: str) -> str:
    """`HH:MM` in UTC: 02:00 plus the offset, never before 02:00 or after 05:59."""
    total = TIMER_WINDOW_START_MINUTE + timer_offset(slug)
    return f"{total // 60:02d}:{total % 60:02d}"


def timer_window(slug: str) -> str:
    """The one line the installer's handoff prints: the offset and the arithmetic."""
    offset = timer_offset(slug)
    return (
        f"window 02:00-06:00 UTC ({TIMER_WINDOW_MINUTES} min); "
        f"offset int(sha256({slug})[:2], 16) % {TIMER_WINDOW_MINUTES} = {offset}; "
        f"start 02:00 UTC + {offset} min = {timer_start(slug)} UTC; "
        f"-o sftp.connections={SFTP_CONNECTIONS} × {10 // SFTP_CONNECTIONS} runs "
        f"= the published 10"
    )


def cap_bytes() -> int:
    """The fair-use envelope: the environment's byte count, else 2.0 GB."""
    raw = (os.environ.get(TENANT_CAP_ENV) or "").strip()
    if not raw:
        return DEFAULT_TENANT_CAP_BYTES
    try:
        value = int(raw)
    except ValueError:
        raise BadInput(
            f"{TENANT_CAP_ENV}={raw!r} is not a byte count"
        ) from None
    if value <= 0:
        raise BadInput(f"{TENANT_CAP_ENV}={value} must be a positive byte count")
    return value


def repo_size(stdout: str) -> int | None:
    """`total_size` from `restic stats --mode raw-data --json`, or None."""
    try:
        value = json.loads(stdout or "{}")
    except ValueError:
        return None
    total = value.get("total_size") if isinstance(value, dict) else None
    return int(total) if isinstance(total, (int, float)) else None


def over_cap_record(previous: dict, *, slug: str, host: str, repo: str, size: int,
                    cap: int, reason: str) -> dict:
    """The `status: over_cap` record: the size, the cap, and why nothing uploaded."""
    return {
        "schema": STATUS_SCHEMA,
        "slug": slug,
        "host": host,
        "repo": repo,
        "status": "over_cap",
        "at": int(time.time()),
        "at_iso": utc_now(),
        "repo_bytes": size,
        "cap_bytes": cap,
        "reason": reason,
        "last_success_at": previous.get("last_success_at"),
        "snapshot": previous.get("snapshot"),
        "snapshot_short": (previous.get("snapshot") or "")[:8] or None,
    }


class Restic:
    """One restic context: repository, password file, transport, cache."""

    def __init__(self, repo: str, password: str, *, key: str | Path | None = None,
                 known_hosts: str | Path | None = None, cache: str | None = None,
                 connections: int = SFTP_CONNECTIONS) -> None:
        self.repo = repo
        self.password = password
        self.key = Path(key).expanduser() if key else None
        self.known_hosts = Path(known_hosts).expanduser() if known_hosts else None
        self.connections = int(connections)
        self.cache = cache or os.environ.get("RESTIC_CACHE_DIR") or "/var/cache/odile/restic"

    def _environment(self, password_file: Path) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update(
            {
                "RESTIC_REPOSITORY": self.repo,
                "RESTIC_PASSWORD_FILE": str(password_file),
                "RESTIC_CACHE_DIR": self.cache,
                "RESTIC_PROGRESS_FPS": "0",
            }
        )
        return environment

    def _password_file(self, directory: str) -> Path:
        path = Path(directory) / "restic-password"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(self.password)
        return path

    def run(self, arguments: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
        """Run one restic command; the password lives in a 0600 file, never argv."""
        binary = restic_binary()
        options: list[str] = []
        if self.key is not None:
            options = sftp_args(self.repo, self.key, self.known_hosts or "",
                                connections=self.connections)
        Path(self.cache).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="odile-restic-") as directory:
            password_file = self._password_file(directory)
            try:
                done = subprocess.run(
                    [binary, *options, *arguments],
                    capture_output=True,
                    text=True,
                    env=self._environment(password_file),
                    timeout=6 * 60 * 60,
                )
            finally:
                try:
                    password_file.unlink()
                except OSError:
                    pass
        if check and done.returncode != 0:
            detail = (done.stderr or done.stdout).strip().splitlines()
            raise BackupError(
                f"restic {' '.join(arguments[:2])} failed (rc {done.returncode}): "
                f"{detail[-1] if detail else 'no output'}"
            )
        return done


def backup_summary(stdout: str) -> dict:
    """The last `message_type: summary` object of `restic backup --json`."""
    summary: dict = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("message_type") == "summary":
            summary = value
    return summary


# --------------------------------------------------------------------------- commands


def command_init(args) -> int:
    repo = resolve_repo(args.repo, args.repo_file)
    key = Path(args.key).expanduser()
    known_hosts = Path(args.known_hosts).expanduser()
    target = sftp_target(repo)
    print(f"repo         {repo}")
    if args.dry_run:
        if target and not key.exists():
            print(f"would create {key} (ed25519, no passphrase) and print its public half")
        if target:
            print(f"would record {target[1]}'s host key in {known_hosts}")
        print("would run  restic init (skipped when the repository already answers)")
        return 0

    if target and not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(key.parent, 0o700)
        done = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
             f"odile-backup {socket.gethostname()}", "-f", str(key)],
            capture_output=True, text=True,
        )
        if done.returncode != 0:
            raise BackupError(f"ssh-keygen failed: {(done.stderr or done.stdout).strip()}")
        os.chmod(key, 0o600)
        print(f"created      {key} (ed25519, no passphrase)")

    if target and not known_hosts.exists():
        user, host, port = target
        scan = ["ssh-keyscan", "-T", "8", "-H"]
        if port:
            scan += ["-p", port]
        scan.append(host)
        done = subprocess.run(scan, capture_output=True, text=True)
        if done.returncode == 0 and done.stdout.strip():
            known_hosts.parent.mkdir(parents=True, exist_ok=True)
            known_hosts.write_text(done.stdout, encoding="utf-8")
            os.chmod(known_hosts, 0o600)
            print(f"recorded     {host} in {known_hosts} ({len(done.stdout.splitlines())} line(s))")
        else:
            print(f"warning      ssh-keyscan {host} failed; the first restic call will too",
                  file=sys.stderr)

    password = read_recovery_password(args.recovery)
    restic = Restic(repo, password, key=key if target else None,
                    known_hosts=known_hosts if target else None,
                    connections=args.sftp_connections)
    existing = restic.run(["snapshots", "--json", "--no-lock"])
    if existing.returncode == 0:
        print("same         the repository already answers; init is a no-op")
    else:
        output = (existing.stderr + existing.stdout).lower()
        # A repository that exists and refuses the password must not be
        # re-initialised: `restic init` would fail anyway, with a sentence
        # about a config file rather than about the recovery file.
        if "wrong password" in output:
            raise BackupError(
                "the recovery file's password does not open the existing repository; "
                "restore the recovery file instead of re-initialising"
            )
        done = restic.run(["init"])
        if done.returncode != 0:
            detail = (done.stderr or done.stdout).strip().splitlines()
            raise BackupError(f"restic init failed: {detail[-1] if detail else 'no output'}")
        print("created      the restic repository")
    if target:
        print(f"public half  {key} (the line the box registers)")
        print(public_half(key))
    return 0


def public_half(key: str | Path) -> str:
    """The key's one `ssh-ed25519 …` line, from the `.pub` or derived from the half.

    A seed-written key (the BX11 shape: the pair is minted at provision) has no
    `.pub` beside it, so the line is derived rather than assumed to exist.
    """
    public = Path(str(key) + ".pub")
    if public.is_file():
        return public.read_text(encoding="utf-8").strip()
    done = subprocess.run(["ssh-keygen", "-y", "-f", str(Path(key).expanduser())],
                          capture_output=True, text=True)
    if done.returncode != 0 or not done.stdout.strip():
        raise BackupError(
            f"cannot read or derive the public half of {key}: "
            f"{(done.stderr or 'no output').strip()}"
        )
    return done.stdout.strip()


def command_run(args) -> int:
    repo = resolve_repo(args.repo, args.repo_file)
    home = Path(args.home).expanduser()
    if not home.is_dir():
        raise Missing(f"the home to back up does not exist: {home}")
    key = Path(args.key).expanduser()
    known_hosts = Path(args.known_hosts).expanduser()
    target = sftp_target(repo)
    if target and not key.exists():
        raise Missing(f"no SFTP key at {key}; run `backup.py init` first")
    password = read_recovery_password(args.recovery)
    restic = Restic(repo, password, key=key if target else None,
                    known_hosts=known_hosts if target else None,
                    connections=args.sftp_connections)
    # The restic host is the slug, not the kernel hostname: a rebuilt guest has
    # a new hostname but the same tenant, and retention groups by --host.
    host = args.host or args.slug or socket.gethostname()
    backup_arguments = [
        "backup", str(home),
        "--tag", args.tag,
        "--host", host,
        "--json",
        *exclude_arguments(home.resolve(), args.exclude),
    ]
    forget_arguments = [
        "forget",
        "--keep-daily", str(args.keep_daily),
        "--keep-weekly", str(args.keep_weekly),
        "--group-by", "host",
        "--host", host,
        "--prune",
    ]
    cap = args.cap_bytes if args.cap_bytes is not None else cap_bytes()
    if args.dry_run:
        print(f"would run    restic stats --mode raw-data --json (start; cap {cap} bytes)")
        print(f"would run    restic {' '.join(_quote_arg(part) for part in backup_arguments)}")
        print(f"would run    restic {' '.join(_quote_arg(part) for part in forget_arguments)}")
        print("would run    restic stats --mode raw-data --json (after the prune)")
        print(f"would write  {args.status} (last_success_at, snapshot, size)")
        return 0

    started = time.monotonic()
    status_path = Path(args.status).expanduser()
    previous = read_json(status_path)
    # ADR-004 v3 §4.1: a run that starts over the cap uploads nothing. The
    # pre-flight read is what makes the cap stop growth instead of measuring it
    # after the fact; a repository that cannot be read yet (a first run before
    # `init`) is not a refusal — the upload decides that.
    version = _restic_version(restic)
    before = restic.run(["stats", "--mode", "raw-data", "--json"])
    used = repo_size(before.stdout) if before.returncode == 0 else None
    if used is not None and used > cap:
        record = over_cap_record(previous, slug=args.slug or host, host=host, repo=repo,
                                 size=used, cap=cap,
                                 reason="the repository was over the cap before this run")
        write_json(status_path, record)
        print(
            f"over_cap     the repository is {human_bytes(used)}, over the "
            f"{cap}-byte cap; nothing uploaded"
        )
        print(f"status       {status_path}")
        return 0
    done = restic.run(backup_arguments)
    duration = round(time.monotonic() - started, 3)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        record = {
            "schema": STATUS_SCHEMA,
            "slug": args.slug or host,
            "host": host,
            "repo": repo,
            "status": "failed",
            "at": int(time.time()),
            "at_iso": utc_now(),
            "error": detail[-1] if detail else f"restic exit {done.returncode}",
            "duration_seconds": duration,
            "cap_bytes": cap,
            "last_success_at": previous.get("last_success_at"),
            "snapshot": previous.get("snapshot"),
        }
        write_json(status_path, record)
        print(f"FAIL         restic backup: {record['error']}", file=sys.stderr)
        return 1

    summary = backup_summary(done.stdout)
    snapshot = summary.get("snapshot_id") or ""
    if not snapshot:
        raise BackupError("restic backup succeeded but printed no snapshot summary")
    pruned = restic.run(forget_arguments)
    # The cap is read *after* the prune: that is the size the next run starts
    # from, not the size the night's delta briefly made.
    stats = restic.run(["stats", "--mode", "raw-data", "--json"])
    repo_bytes = repo_size(stats.stdout) if stats.returncode == 0 else None
    over = repo_bytes is not None and repo_bytes > cap
    record = {
        "schema": STATUS_SCHEMA,
        "slug": args.slug or host,
        "host": host,
        "repo": repo,
        "status": "over_cap" if over else "ok",
        "at": int(time.time()),
        "at_iso": utc_now(),
        "last_success_at": int(time.time()),
        "snapshot": snapshot,
        "snapshot_short": snapshot[:8],
        "files_new": summary.get("files_new"),
        "files_changed": summary.get("files_changed"),
        "bytes_processed": summary.get("total_bytes_processed"),
        "repo_bytes": repo_bytes,
        "cap_bytes": cap,
        "duration_seconds": duration,
        "retention": {"keep_daily": args.keep_daily, "keep_weekly": args.keep_weekly},
        "forget": "ok" if pruned.returncode == 0 else "failed",
        "restic": version,
    }
    write_json(status_path, record)
    print(
        f"ok           snapshot {record['snapshot_short']} of {home} in {duration:.1f}s "
        f"({record['bytes_processed']} bytes processed, {record['files_new']} new files)"
    )
    if over and repo_bytes is not None:
        print(
            f"over_cap     the repository is {human_bytes(repo_bytes)}, "
            f"over the {cap}-byte cap; the next run uploads nothing"
        )
    print(f"status       {status_path}")
    if pruned.returncode != 0:
        print("warning      retention/prune failed; the snapshot is still valid", file=sys.stderr)
    return 0


def human_bytes(value: int) -> str:
    """`2194 bytes (2.1 KiB)` — one number a reader can compare to 2.0 GB."""
    for unit, scale in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024)):
        if value >= scale:
            return f"{value} bytes ({value / scale:.1f} {unit})"
    return f"{value} bytes"


def _restic_version(restic: Restic) -> str:
    try:
        done = restic.run(["version"])
    except BackupError:
        return "unknown"
    line = (done.stdout or done.stderr).strip().splitlines()
    return line[0] if line else "unknown"


def command_status(args) -> int:
    path = Path(args.status).expanduser()
    record = read_json(path)
    if not record:
        if args.json:
            print(json.dumps({"schema": STATUS_SCHEMA, "status": "missing", "path": str(path)},
                             indent=2, sort_keys=True))
        else:
            print(f"backup: no status at {path}")
        return 1
    if args.json:
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0
    age = int(time.time()) - int(record.get("last_success_at") or 0)
    print(f"backup: {record.get('status', 'unknown')} on {record.get('host', '-')}")
    print(f"  repo:      {record.get('repo', '-')}")
    print(f"  snapshot:  {record.get('snapshot', '-')}")
    print(f"  last ok:   {record.get('at_iso', '-')} ({_duration(age)} ago)")
    print(f"  files:     {record.get('files_new', '?')} new / {record.get('files_changed', '?')} changed")
    print(f"  size:      {record.get('bytes_processed', '?')} bytes processed; "
          f"{record.get('repo_bytes', '?')} bytes in the repository")
    return 0


def _duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def _last_output_line(done: subprocess.CompletedProcess[str]) -> str:
    """restic's last word for a one-line error: stderr when it spoke, else stdout."""
    lines = (done.stderr or done.stdout or "").strip().splitlines()
    return lines[-1].strip() if lines else "no output"


def command_restore(args) -> int:
    repo = resolve_repo(args.repo, args.repo_file)
    key = Path(args.key).expanduser()
    known_hosts = Path(args.known_hosts).expanduser()
    target = sftp_target(repo)
    if target and not key.exists():
        raise Missing(f"no SFTP key at {key}; run `backup.py init` first")
    password = read_recovery_password(args.recovery)
    restic = Restic(repo, password, key=key if target else None,
                    known_hosts=known_hosts if target else None,
                    connections=args.sftp_connections)
    arguments = ["restore", args.snapshot, "--target", args.target]
    for include in args.include or []:
        arguments += ["--include", include]
    if args.dry_run:
        print(f"would run    restic {' '.join(_quote_arg(part) for part in arguments)}")
        return 0
    Path(args.target).expanduser().mkdir(parents=True, exist_ok=True)
    done = restic.run(arguments)
    if done.returncode != 0:
        detail = _last_output_line(done)
        # restic exits 12 for a password that does not open the repository.
        # A rebuilt machine's first visit shows that pasted recovery file and
        # needs to tell "wrong password" from every other failure; the same
        # sentence is also matched, because a transport wrapper can lose the code.
        if done.returncode == 12 or "wrong password" in (done.stderr + done.stdout).lower():
            raise WrongPassword(
                f"restic restore failed (exit {done.returncode}): {detail}; "
                "the recovery file's password does not open the repository"
            )
        raise BackupError(f"restic restore failed (exit {done.returncode}): {detail}")
    lines = [line for line in done.stdout.strip().splitlines() if line.strip()]
    print(f"ok           restored {args.snapshot} to {args.target}")
    if lines:
        print(f"restic       {lines[-1]}")
    return 0


def _snapshot_entries(stdout: str) -> list[dict]:
    """`restic snapshots --json` is one array; anything else is a failure to read."""
    try:
        value = json.loads(stdout or "[]")
    except ValueError:
        raise BackupError("restic snapshots printed no JSON") from None
    if not isinstance(value, list):
        raise BackupError("restic snapshots printed no JSON array")
    return [item for item in value if isinstance(item, dict)]


def _snapshot_time(value: object) -> datetime.datetime:
    """restic's RFC3339 time as a comparable instant; unreadable times sort first."""
    text = str(value or "").replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def _newest_snapshot(snapshots: list[dict]) -> dict:
    return max(snapshots, key=lambda item: (_snapshot_time(item.get("time")),
                                             str(item.get("id") or "")))


def choose_snapshot(snapshots: list[dict], selector: str | None) -> dict:
    """The snapshot a restore would take: exact id, 4+ hex prefix, or the newest.

    `restore` itself passes the word to restic, which has the same rules; this
    repeats them so `snapshot-info` names the snapshot the restore would land on.
    """
    if not snapshots:
        raise Missing("the repository has no snapshots")
    text = (selector or "latest").strip()
    if text == "latest":
        return _newest_snapshot(snapshots)
    lowered = text.lower()
    for item in snapshots:
        if str(item.get("id") or "").lower() == lowered:
            return item
    if re.fullmatch(r"[0-9a-fA-F]{4,}", text):
        matches = [item for item in snapshots
                   if str(item.get("id") or "").lower().startswith(lowered)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise BadInput(f"{text!r} matches {len(matches)} snapshots; give more characters")
    raise Missing(f"no snapshot matches {text!r}")


def command_snapshot_info(args) -> int:
    """The id and time of the snapshot a restore would take (read-only: --no-lock)."""
    repo = resolve_repo(args.repo, args.repo_file)
    key = Path(args.key).expanduser()
    known_hosts = Path(args.known_hosts).expanduser()
    target = sftp_target(repo)
    if target and not key.exists():
        raise Missing(f"no SFTP key at {key}; run `backup.py init` first")
    password = read_recovery_password(args.recovery)
    restic = Restic(repo, password, key=key if target else None,
                    known_hosts=known_hosts if target else None,
                    connections=args.sftp_connections)
    arguments = ["snapshots", "--json", "--no-lock"]
    selector = args.snapshot or "latest"
    if args.dry_run:
        print(f"would run    restic {' '.join(_quote_arg(part) for part in arguments)}")
        print(f"would pick   {selector} (exact id, a 4+ hex prefix, or the newest by time)")
        return 0
    done = restic.run(arguments)
    if done.returncode != 0:
        detail = _last_output_line(done)
        if done.returncode == 12 or "wrong password" in detail.lower():
            # The read is where a wrong password is first seen; exit 12, the
            # code restore would also give, so the caller can say so.
            raise WrongPassword(f"restic snapshots failed (exit 12): {detail}")
        raise Missing(
            f"restic snapshots failed (exit {done.returncode}): {detail}"
        )
    snapshot = choose_snapshot(_snapshot_entries(done.stdout), selector)
    full_id = str(snapshot.get("id") or "")
    record = {
        "id": full_id,
        "short_id": str(snapshot.get("short_id") or full_id[:8]),
        "time": snapshot.get("time"),
        # restic's JSON calls the snapshot's host `hostname`.
        "host": snapshot.get("hostname") or snapshot.get("host") or None,
        "paths": list(snapshot.get("paths") or []),
    }
    if args.json:
        print(json.dumps(record, sort_keys=True))
        return 0
    print(f"snapshot     {record['id']}")
    print(f"time         {record['time']}")
    return 0


def command_drill(args) -> int:
    """Back up a canary, delete it, restore it, compare; then put it back.

    `--file` must name a file that exists *before* the backup: a missing path
    is a prerequisite error here, not a restic repository error after a
    pointless backup.
    """
    repo = resolve_repo(args.repo, args.repo_file)
    home = Path(args.home).expanduser()
    key = Path(args.key).expanduser()
    known_hosts = Path(args.known_hosts).expanduser()
    target = sftp_target(repo)
    if target and not key.exists():
        raise Missing(f"no SFTP key at {key}; run `backup.py init` first")
    password = read_recovery_password(args.recovery)
    restic = Restic(repo, password, key=key if target else None,
                    known_hosts=known_hosts if target else None,
                    connections=args.sftp_connections)
    host = args.host or args.slug or socket.gethostname()
    canary = Path(args.file).expanduser() if args.file else home / "backup-drill" / "canary.txt"
    record_path = Path(args.drill).expanduser()
    if args.file and not canary.is_file():
        raise Missing(f"--file {canary} does not exist; the drill deletes and restores a file")

    if args.dry_run:
        print(f"would write  {canary} (if absent) and back up {home}")
        print(f"would delete {canary}, restore snapshot latest into a temp dir, compare sha256")
        print(f"would write  {record_path}")
        return 0

    canary.parent.mkdir(parents=True, exist_ok=True)
    if not canary.exists() or args.file is None:
        canary.write_text(
            f"odile backup drill {utc_now()} {secrets.token_hex(16)}\n", encoding="utf-8"
        )
    before = sha256_file(canary)

    started = time.monotonic()
    backup = restic.run([
        "backup", str(home), "--tag", args.tag, "--host", host, "--json",
        *exclude_arguments(home.resolve(), args.exclude),
    ])
    if backup.returncode != 0:
        detail = (backup.stderr or backup.stdout).strip().splitlines()
        raise BackupError(f"the drill's backup failed: {detail[-1] if detail else 'no output'}")
    snapshot = backup_summary(backup.stdout).get("snapshot_id") or "latest"
    backed_up_at = time.time()

    canary.unlink()
    if canary.exists():
        raise BackupError(f"could not delete the canary {canary}")
    deleted_at = time.time()

    with tempfile.TemporaryDirectory(prefix="odile-drill-") as directory:
        restore = restic.run([
            "restore", snapshot, "--target", directory, "--include", str(canary),
        ])
        if restore.returncode != 0:
            detail = (restore.stderr or restore.stdout).strip().splitlines()
            raise BackupError(f"the drill's restore failed: {detail[-1] if detail else 'no output'}")
        restored = Path(directory) / str(canary).lstrip("/")
        if not restored.is_file():
            raise BackupError(f"the restore did not produce {restored}")
        after = sha256_file(restored)
        restored_mode = restored.stat().st_mode & 0o777
        canary.write_bytes(restored.read_bytes())
        os.chmod(canary, restored_mode)
    in_place = sha256_file(canary)
    elapsed = round(time.monotonic() - started, 3)
    record = {
        "schema": DRILL_SCHEMA,
        "at": int(time.time()),
        "at_iso": utc_now(),
        "host": host,
        "repo": repo,
        "snapshot": snapshot,
        "file": str(canary),
        "sha256_before": before,
        "sha256_restored": after,
        "sha256_in_place": in_place,
        "verdict": "pass" if before == after == in_place else "fail",
        # The measured pair, stated exactly: if the machine had died at the
        # deletion, the backup was already `backed_up_at` old; the restore from
        # that deletion to a checksum-verified file is the RTO.
        "rpo_seconds": round(deleted_at - backed_up_at, 3),
        "rto_seconds": round(time.time() - deleted_at, 3),
        "duration_seconds": elapsed,
    }
    write_json(record_path, record)
    print(f"drill        backed up {canary} as {snapshot[:8]}, deleted it, restored it")
    print(f"sha256       before {before}")
    print(f"             after  {after}")
    print(f"verdict      {record['verdict'].upper()} in {elapsed:.1f}s -> {record_path}")
    return 0 if record["verdict"] == "pass" else 1


def command_verify(args) -> int:
    """The password opens the repository; a wrong one is refused (HQ cannot read it)."""
    repo = resolve_repo(args.repo, args.repo_file)
    key = Path(args.key).expanduser()
    known_hosts = Path(args.known_hosts).expanduser()
    target = sftp_target(repo)
    if target and not key.exists():
        raise Missing(f"no SFTP key at {key}; run `backup.py init` first")
    password = read_recovery_password(args.recovery)
    options = {
        "key": key if target else None,
        "known_hosts": known_hosts if target else None,
        "connections": args.sftp_connections,
    }
    good = Restic(repo, password, key=options["key"], known_hosts=options["known_hosts"],
                  connections=options["connections"]).run(
        ["snapshots", "--json", "--no-lock"]
    )
    wrong = Restic(
        repo, "wrong-" + secrets.token_hex(16),
        key=options["key"], known_hosts=options["known_hosts"],
        connections=options["connections"],
    ).run(["snapshots", "--json", "--no-lock"])
    if good.returncode != 0:
        detail = (good.stderr or good.stdout).strip().splitlines()
        raise BackupError(
            f"the recovery password did not open the repository: "
            f"{detail[-1] if detail else 'no output'}"
        )
    count = len(json.loads(good.stdout or "[]"))
    refused = wrong.returncode != 0 and "wrong password" in (wrong.stderr + wrong.stdout).lower()
    print(f"repo         {repo}")
    print(f"password     opens the repository ({count} snapshot(s))")
    print(f"wrong one    {'refused' if refused else 'NOT refused'}")
    if args.json:
        print(json.dumps({"repo": repo, "snapshots": count, "password_opens": True,
                          "wrong_password_refused": refused}, indent=2, sort_keys=True))
    return 0 if refused else 1


def command_heartbeat(args) -> int:
    """Post the status record to HQ as a signed heartbeat (the freshness signal)."""
    status = read_json(Path(args.status).expanduser())
    if not status:
        raise Missing(f"no status record at {args.status}; run `backup.py run` first")
    facts = {
        "last_backup_at": status.get("last_success_at"),
        "backup": {
            "repo": status.get("repo"),
            "snapshot": status.get("snapshot"),
            "status": status.get("status"),
            "bytes": status.get("bytes_processed"),
            "repo_bytes": status.get("repo_bytes"),
            "at": status.get("last_success_at"),
        },
    }
    if args.slug:
        facts["slug"] = args.slug
    body = {
        "slug": args.slug or status.get("slug") or socket.gethostname(),
        "at": int(time.time()),
        "facts": facts,
    }
    key_dir = Path(args.key_dir).expanduser()
    private = key_dir / "heartbeat.key"
    public = key_dir / "heartbeat.pub"
    if not private.exists():
        if args.dry_run:
            print(f"would create {private} (ed25519, 0600) and post to {args.url}")
            return 0
        key_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(key_dir, 0o700)
        done = subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private)],
            capture_output=True, text=True,
        )
        if done.returncode != 0:
            raise BackupError(f"openssl genpkey failed: {done.stderr.strip()}")
        os.chmod(private, 0o600)
        done = subprocess.run(
            ["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
            capture_output=True, text=True,
        )
        if done.returncode != 0:
            raise BackupError(f"openssl pkey failed: {done.stderr.strip()}")
        print(f"created      {private} (the private half never leaves this machine)")
    body["public_key"] = public.read_text(encoding="utf-8")
    payload = json.dumps(body, sort_keys=True).encode("utf-8")
    signed = subprocess.run(
        ["openssl", "dgst", "-sign", str(private), "-out", "/dev/stdout"],
        input=payload, capture_output=True,
    )
    if signed.returncode != 0:
        raise BackupError(f"openssl dgst -sign failed: {signed.stderr.decode().strip()}")
    if args.dry_run:
        print(f"would post   {len(payload)} bytes to {args.url}")
        return 0
    request = urllib.request.Request(
        args.url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Odile-Signature": base64.b64encode(signed.stdout).decode("ascii"),
            "User-Agent": "odile-backup/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            answer = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raise BackupError(f"HQ refused the heartbeat: {error.code} {error.read()[:200]!r}") from None
    except urllib.error.URLError as error:
        raise BackupError(f"cannot reach {args.url}: {error.reason}") from None
    print(f"posted       heartbeat for {body['slug']} at {body['at']} -> {args.url}")
    print(f"HQ said      {json.dumps(answer, sort_keys=True)}")
    return 0


SERVICE_TEMPLATE = """\
[Unit]
Description=Odile tenant backup (restic, nightly)
Documentation=file:///usr/local/lib/odile/backup.py
After=network-online.target
Wants=network-online.target
# ADR-004 §7.4: a rebuilt machine has no recovery file until the user pastes it,
# so the nightly run is skipped rather than reported as a failed backup.
ConditionPathExists={recovery}

[Service]
Type=oneshot
# Transport: restic -o sftp.connections=2, so one run asks for two of the box's
# published ten connections (five runs share the ceiling, ADR-004 §4.3).
ExecStart=/usr/bin/python3 {script} run --repo-file {repo_file}{repo_arg} --recovery {recovery} --home {home} --key {key} --known-hosts {known_hosts} --status {status} --slug {slug} --sftp-connections 2
ExecStartPost=-/usr/bin/python3 {script} heartbeat --status {status} --slug {slug} --url {url} --key-dir {heartbeat_dir}
Nice=10
IOSchedulingClass=idle
"""

TIMER_TEMPLATE = """\
[Unit]
Description=Odile tenant backup, daily in the 02:00-06:00 UTC window

[Timer]
OnCalendar=*-*-* {start}:00
Persistent=true
RandomizedDelaySec=0

[Install]
WantedBy=timers.target
"""


def command_install(args) -> int:
    script = Path(args.install_path).expanduser()
    source = Path(__file__).resolve()
    service = SERVICE_TEMPLATE.format(
        script=script, repo_file=args.repo_file,
        repo_arg=f" --repo {args.repo}" if args.repo else "",
        recovery=args.recovery, home=args.home,
        key=args.key, known_hosts=args.known_hosts, status=args.status, slug=args.slug,
        url=args.heartbeat_url, heartbeat_dir=args.heartbeat_key_dir,
    )
    timer = TIMER_TEMPLATE.format(start=timer_start(args.slug))
    units = {
        Path(args.unit_dir).expanduser() / "odile-backup-tenant.service": service,
        Path(args.unit_dir).expanduser() / "odile-backup-tenant.timer": timer,
    }
    if args.dry_run:
        print(f"would install {script} (from {source})")
        for path, text in units.items():
            print(f"would write  {path}:\n{text}")
        print("would run    systemctl daemon-reload && systemctl enable --now odile-backup-tenant.timer")
        return 0

    if source != script:
        script.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, script)
        os.chmod(script, 0o755)
        print(f"installed    {script}")
    for path, text in units.items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            print(f"same         {path}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o644)
        print(f"wrote        {path}")
    print(f"window       {timer_window(args.slug)}")
    if Path(args.unit_dir).expanduser() != Path("/etc/systemd/system"):
        # A test or a packager owns this tree; enabling a unit systemd cannot
        # see would be a lie, so stop at the files.
        print("skip         systemctl (the unit directory is not /etc/systemd/system)")
        return 0
    done = subprocess.run(["systemctl", "daemon-reload"], capture_output=True, text=True)
    if done.returncode != 0:
        raise BackupError(f"systemctl daemon-reload failed: {done.stderr.strip()}")
    done = subprocess.run(
        ["systemctl", "enable", "--now", "odile-backup-tenant.timer"],
        capture_output=True, text=True,
    )
    if done.returncode != 0:
        raise BackupError(f"systemctl enable failed: {done.stderr.strip()}")
    print(f"enabled      odile-backup-tenant.timer (daily {timer_start(args.slug)} UTC, "
          f"Persistent=true)")
    return 0


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backup.py",
        description="Odile tenant backup: restic, the recovery-file password, and the drill.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(sub, *, repo: bool = True) -> None:
        if repo:
            sub.add_argument("--repo", help="restic repository URL (else --repo-file)")
            sub.add_argument("--repo-file", default=DEFAULT_REPO_FILE,
                             help=f"the seed-written URL (default: {DEFAULT_REPO_FILE})")
        sub.add_argument("--recovery", default=DEFAULT_RECOVERY,
                         help=f"the user's recovery file (default: {DEFAULT_RECOVERY})")
        sub.add_argument("--key", default=str(Path(DEFAULT_KEY_DIR) / "id_ed25519"),
                         help="the SFTP private key (generated by init)")
        sub.add_argument("--known-hosts", default=str(Path(DEFAULT_KEY_DIR) / "known_hosts"),
                         help="the SFTP host key file init records")
        sub.add_argument("--sftp-connections", type=int, default=SFTP_CONNECTIONS,
                         help="restic's -o sftp.connections for this run "
                              f"(default: {SFTP_CONNECTIONS})")
        sub.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")

    init = subparsers.add_parser("init", help="key, host key, and an idempotent restic init")
    common(init)
    init.set_defaults(function=command_init)

    run = subparsers.add_parser("run", help="one nightly snapshot, retention, status record")
    common(run)
    run.add_argument("--home", default=DEFAULT_HOME, help=f"tree to back up (default: {DEFAULT_HOME})")
    run.add_argument("--slug", help="tenant slug (default: this hostname)")
    run.add_argument("--host", help="restic --host, stable across a rebuild (default: this hostname)")
    run.add_argument("--tag", default="tenant", help="restic tag (default: tenant)")
    run.add_argument("--exclude", action="append", metavar="PATTERN",
                     help="one more restic exclusion (repeatable)")
    run.add_argument("--keep-daily", type=int, default=DEFAULT_KEEP_DAILY)
    run.add_argument("--keep-weekly", type=int, default=DEFAULT_KEEP_WEEKLY)
    run.add_argument("--status", default=DEFAULT_STATUS,
                     help=f"the HQ-visible status record (default: {DEFAULT_STATUS})")
    run.add_argument("--cap-bytes", type=int, default=None,
                     help=f"the fair-use envelope in bytes (default: ${TENANT_CAP_ENV} "
                          f"or {DEFAULT_TENANT_CAP_BYTES})")
    run.set_defaults(function=command_run)

    status = subparsers.add_parser("status", help="print the last status record")
    status.add_argument("--status", default=DEFAULT_STATUS)
    status.add_argument("--json", action="store_true")
    status.set_defaults(function=command_status)

    restore = subparsers.add_parser("restore", help="restore a snapshot into a target")
    common(restore)
    restore.add_argument("snapshot", nargs="?", default="latest")
    restore.add_argument("--target", required=True)
    restore.add_argument("--include", action="append", help="one absolute path (repeatable)")
    restore.set_defaults(function=command_restore)

    info = subparsers.add_parser(
        "snapshot-info", help="the id and time of the snapshot a restore would take")
    common(info)
    info.add_argument("snapshot", nargs="?", default="latest",
                      help="a full id, a 4+ hex prefix, or latest (default)")
    info.add_argument("--json", action="store_true", help="one JSON object on stdout")
    info.set_defaults(function=command_snapshot_info)

    drill = subparsers.add_parser("drill", help="delete a backed-up file, restore it, compare")
    common(drill)
    drill.add_argument("--home", default=DEFAULT_HOME)
    drill.add_argument("--slug")
    drill.add_argument("--host")
    drill.add_argument("--tag", default="tenant")
    drill.add_argument("--exclude", action="append", metavar="PATTERN",
                       help="one more restic exclusion (repeatable)")
    drill.add_argument("--file", help="an existing file inside the home (default: a canary)")
    drill.add_argument("--drill", default=DEFAULT_DRILL)
    drill.set_defaults(function=command_drill)

    verify = subparsers.add_parser("verify", help="the recovery password opens the repo")
    common(verify)
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(function=command_verify)

    beat = subparsers.add_parser("heartbeat", help="post the status to HQ, signed")
    beat.add_argument("--status", default=DEFAULT_STATUS)
    beat.add_argument("--slug")
    beat.add_argument("--url", default=DEFAULT_HEARTBEAT_URL)
    beat.add_argument("--key-dir", default=DEFAULT_HEARTBEAT_KEY_DIR)
    beat.add_argument("--dry-run", action="store_true")
    beat.set_defaults(function=command_heartbeat)

    install = subparsers.add_parser("install", help="write and enable the systemd timer")
    install.add_argument("--install-path", default=DEFAULT_INSTALL_PATH)
    install.add_argument("--unit-dir", default="/etc/systemd/system")
    install.add_argument("--repo", help="restic repository URL (default: --repo-file)")
    install.add_argument("--repo-file", default=DEFAULT_REPO_FILE)
    install.add_argument("--recovery", default=DEFAULT_RECOVERY)
    install.add_argument("--home", default=DEFAULT_HOME)
    install.add_argument("--key", default=str(Path(DEFAULT_KEY_DIR) / "id_ed25519"))
    install.add_argument("--known-hosts", default=str(Path(DEFAULT_KEY_DIR) / "known_hosts"))
    install.add_argument("--status", default=DEFAULT_STATUS)
    install.add_argument("--heartbeat-url", default=DEFAULT_HEARTBEAT_URL)
    install.add_argument("--heartbeat-key-dir", default=DEFAULT_HEARTBEAT_KEY_DIR)
    install.add_argument("--slug", default=socket.gethostname())
    install.add_argument("--dry-run", action="store_true")
    install.set_defaults(function=command_install)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.function(args))
    except BackupError as error:
        print(f"backup: {error}", file=sys.stderr)
        return error.exit_code
    except KeyboardInterrupt:
        print("backup: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
