"""`restore` — restore a tenant's home from the off-site restic repository.

The repository password is generated on the guest and held by the user, in the
recovery file the first visit downloads. HQ never holds it, so the default
invocation is a **plan**: the repository URL, the snapshot, the target and the
one command that needs the user's file. `--apply` runs that command, and then
only when `--password-file` names the user's recovery file — the password is
put in a 0600 file for exactly one restic call and never appears on argv or in
the report.

    odilectl restore odl-7f3a2c
    odilectl restore odl-7f3a2c --apply --password-file ~/recovery.txt \\
        --target /var/tmp/restore --include /home/tenant

Exit codes follow the CLI: 0 ok, 2 usage, 5 config, 1 a restic failure.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from argparse import ArgumentParser
from pathlib import Path

from .core import Context, Report, applied, plan
from .errors import ConfigError, UsageError

NAME = "restore"
HELP = "Plan or run a restore of a tenant's home from the off-site restic repository."
EXAMPLE = (
    "odilectl restore odl-7f3a2c --apply --password-file ~/recovery.txt "
    "--target /var/tmp/restore --include /home/tenant"
)

# The installer's line (installer/odile_install.py:parse_recovery) and the
# guest script's copy (scripts/backup.py). One grammar, three readers.
RECOVERY_PASSWORD_RE = re.compile(r"^Backup password:\s*(\S+)\s*$", re.M)
DEFAULT_REPO = "sftp:odile-backup@backup.odile.fyi:/tenants/{slug}"
DEFAULT_TARGET = "/var/tmp/odile-restore/{slug}"


def add_arguments(parser: ArgumentParser) -> None:
    parser.add_argument("slug", help="the tenant's slug")
    parser.add_argument(
        "--snapshot",
        default="latest",
        metavar="ID",
        help="restic snapshot id (default: latest)",
    )
    parser.add_argument(
        "--repo",
        metavar="URL",
        help="restic repository URL (default: the config's backup_repo with {slug})",
    )
    parser.add_argument(
        "--target",
        metavar="DIR",
        help=f"where the restore lands (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--include",
        action="append",
        metavar="PATH",
        help="one absolute path to restore, e.g. /home/tenant (repeatable)",
    )
    parser.add_argument(
        "--password-file",
        metavar="PATH",
        help="the user's recovery file; required by --apply and the only copy of the password",
    )
    parser.add_argument(
        "--ssh-key",
        metavar="PATH",
        help="the SFTP private key, when the repository is sftp:",
    )
    parser.add_argument(
        "--known-hosts",
        metavar="PATH",
        help="the SSH known_hosts file that pins the backup box",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="list the repository's snapshots and exit; proves the password opens it",
    )


def _repo(ctx: Context, args) -> str:
    template = args.repo or ctx.config.get("backup_repo", DEFAULT_REPO)
    if not isinstance(template, str) or not template.strip():
        raise ConfigError("restore: backup_repo is empty; set it in the config or pass --repo")
    return template.strip().replace("{slug}", args.slug)


def _target(args) -> str:
    return args.target or DEFAULT_TARGET.format(slug=args.slug)


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


def _sftp_options(repo: str, key: str | None, known_hosts: str | None) -> list[str]:
    if not repo.startswith("sftp:") or not key:
        return []
    match = re.match(r"^(?:sftp:(?://)?)?([^@/:]+)@([^:/]+)(?::(\d+))?(?::|/)", repo)
    if not match:
        raise UsageError(f"restore: cannot parse the SFTP repository {repo!r}")
    user, host, port = match.groups()
    command = [
        "ssh", "-i", str(Path(key).expanduser()),
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "LogLevel=ERROR",
    ]
    if known_hosts:
        command += ["-o", f"UserKnownHostsFile={Path(known_hosts).expanduser()}"]
    if port:
        command += ["-p", port]
    # `-s sftp` is the remote command, so it must follow the destination.
    command += [f"{user}@{host}", "-s", "sftp"]
    return ["-o", "sftp.command=" + " ".join(_quote(part) for part in command)]


def _quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def _run_restic(repo: str, password: str, arguments: list[str],
                options: list[str]) -> subprocess.CompletedProcess[str]:
    binary = shutil.which("restic")
    if not binary:
        raise ConfigError("restore: restic is not installed on this machine")
    cache = os.environ.get("RESTIC_CACHE_DIR") or tempfile.gettempdir()
    with tempfile.TemporaryDirectory(prefix="odile-restore-") as directory:
        password_file = Path(directory) / "restic-password"
        handle = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(password)
        environment = dict(os.environ)
        environment.update(
            {
                "RESTIC_REPOSITORY": repo,
                "RESTIC_PASSWORD_FILE": str(password_file),
                "RESTIC_CACHE_DIR": cache,
                "RESTIC_PROGRESS_FPS": "0",
            }
        )
        try:
            return subprocess.run(
                [binary, *options, *arguments],
                capture_output=True,
                text=True,
                env=environment,
                timeout=6 * 60 * 60,
            )
        finally:
            try:
                password_file.unlink()
            except OSError:
                pass


def _snapshots(repo: str, password: str, options: list[str]) -> list[dict]:
    done = _run_restic(repo, password, ["snapshots", "--json", "--no-lock"], options)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise ConfigError(
            "restore: the recovery password did not open the repository: "
            f"{detail[-1] if detail else 'no output'}"
        )
    try:
        value = json.loads(done.stdout or "[]")
    except ValueError as error:
        raise ConfigError(f"restore: restic printed no snapshot list: {error}") from None
    return value if isinstance(value, list) else []


def run(ctx: Context, args) -> Report:
    repo = _repo(ctx, args)
    target = _target(args)
    actions = [
        {
            "action": "restic.restore",
            "slug": args.slug,
            "snapshot": args.snapshot,
            "repository": repo,
            "target": target,
            "include": args.include or ["(everything)"],
            "password_file": args.password_file or "(the user's recovery file; required with --apply)",
        }
    ]
    notes = [
        "the repository password is generated on the guest and held by the user; "
        "HQ holds no copy, so the default invocation changes nothing",
        f"run: odilectl restore {args.slug} --apply --password-file <recovery.txt> "
        f"--target {target}" + (f" --include {args.include[0]}" if args.include else ""),
    ]

    if not ctx.mutate and not args.verify:
        return plan(NAME, ctx, actions, notes, slug=args.slug, snapshot=args.snapshot,
                    repository=repo, target=target)

    if not args.password_file:
        raise UsageError(
            "restore --apply needs --password-file: the user's recovery file is the only "
            "copy of the repository password"
        )
    password = _read_password(args.password_file)
    options = _sftp_options(repo, args.ssh_key, args.known_hosts)
    if args.verify:
        snapshots = _snapshots(repo, password, options)
        lines = [f"  {len(snapshots)} snapshot(s) in {repo}"]
        for snapshot in snapshots[-5:]:
            lines.append(
                f"  {str(snapshot.get('short_id', snapshot.get('id', '?'))):<10} "
                f"{snapshot.get('time', '?')} "
                f"{', '.join(snapshot.get('paths', []) or [])}"
            )
        return applied(NAME, ctx, lines, head=f"restore: the password opens {repo}",
                       snapshots=len(snapshots), repository=repo, slug=args.slug)

    arguments = ["restore", args.snapshot, "--target", target]
    for include in args.include or []:
        arguments += ["--include", include]
    Path(target).expanduser().mkdir(parents=True, exist_ok=True)
    done = _run_restic(repo, password, arguments, options)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise ConfigError(f"restore: restic restore failed: {detail[-1] if detail else 'no output'}")
    lines = [
        f"  repository: {repo}",
        f"  snapshot:   {args.snapshot}",
        f"  target:     {target}",
    ]
    summary = [line for line in done.stdout.strip().splitlines() if line.strip()]
    if summary:
        lines.append(f"  restic:     {summary[-1]}")
    return applied(NAME, ctx, lines, head="restore: applied", slug=args.slug,
                   snapshot=args.snapshot, repository=repo, target=target,
                   include=args.include or [])
