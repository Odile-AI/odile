#!/usr/bin/python3
"""Odile's two support programs: `odile-support` and `odile-diag`.

One file, because they share one rule: a support grant is a handover, and the
default is no access at all.

`odile-support`
    `status`  what the machine has right now; the default answer is `no grant`
    `grant`   install Odile's support public key from `/etc/odile/support-key.pub`
              into root's `authorized_keys` behind a forced command, for N hours
    `revoke`  remove that line now
    `expire`  remove the line when the expiry has passed (a timer calls this)
    `session` the forced command: refuse after expiry, log start and end, run
              the granted shell

`odile-diag`
    one file with versions, disk usage, service state, recent logs and the
    machine's own access report; never a passphrase, token, private key, prompt
    history or file contents.

The one design fact that is easy to get wrong: **a reboot does not close a
grant, but the bytes in `authorized_keys` do not survive one by themselves.**
The tenant image runs the release installer on every boot
(`odile-release-setup.service`), and its `ensure_verify_surface` writes exactly
one line — the measured boot key — to root's `authorized_keys`; a grant line is
deleted by that rewrite, not kept. What makes a grant live is the state file and
its expiry, and the forced command reads that state on every connection. So
`expire` converges in both directions: a due grant loses its line and gets the
`expired_at` tombstone, and a live grant whose line a boot removed gets the
identical line put back (within 30 seconds, by
`odile-support-expire.timer`). Expiry and revoke are the closures; a reboot is
not. An open SSH session keeps running until it exits; the user closes it with
`w` and `pkill -t pts/N`.

Standard library only. `--root` relocates every absolute path so the tests and
the fresh-machine drill run against a throwaway root. The forced command is
`/usr/local/sbin/odile-support session` on a tenant (root is `/`); under a
drill root it names that root's copy of the program and carries the same
`--root`, so a real sshd can be pointed at the throwaway tree and exercise the
exact line `grant` writes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Installed paths. The forced command is absolute: sshd does not run a login
# shell to find it, and a relative path would be resolved under the user's home.
PROGRAM = "/usr/local/sbin/odile-support"
KEY_PATH = "/etc/odile/support-key.pub"
STATE_PATH = "/var/lib/odile/support-grant.json"
LOG_PATH = "/var/log/odile-support.log"
AUTHORIZED_KEYS = "/root/.ssh/authorized_keys"
ACCESS_REPORT = "/var/lib/odile/access-report.txt"
SEED_PATH = "/etc/odile/seed.json"
BOOT_RECORD = "/etc/odile/boot-record.json"
MANIFEST = "/etc/odile/manifest.json"
INSTALLER_COPY = "/usr/local/bin/odile-install.sh"
UNIT_DIR = "/etc/systemd/system"
TIMER = "odile-support-expire.timer"

GRANT_MARKER = "odile-support-grant-"
GRANT_ID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_HOURS = 720.0

SERVICE_UNIT = """\
[Unit]
Description=Odile support grant — expire a due grant, restore a live one
Documentation=file:/var/log/odile-support.log
After=local-fs.target

[Service]
Type=oneshot
ExecStart={program} expire --quiet
"""

TIMER_UNIT = """\
[Unit]
Description=Odile support grant (every 30 seconds)

[Timer]
OnActiveSec=10s
OnBootSec=20s
OnUnitActiveSec=30s
AccuracySec=1s
Persistent=true

[Install]
WantedBy=timers.target
"""

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_UNSAFE = 3          # no safe grant can be made (key or forced command)
EXIT_NOT_ROOT = 4
EXIT_TIMER = 5


# --------------------------------------------------------------------------
# Errors and the clock


class SupportError(Exception):
    """A refusal with a stable exit code and one sentence the operator can act on."""

    exit_code = EXIT_RUNTIME

    def __init__(self, message: str, exit_code: int | None = None) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class KeyFileError(SupportError):
    exit_code = EXIT_UNSAFE


class NotRoot(SupportError):
    exit_code = EXIT_NOT_ROOT


class TimerError(SupportError):
    exit_code = EXIT_TIMER


def now_utc() -> datetime:
    """The current time, unless a test or drill set `ODILE_SUPPORT_NOW`.

    The override exists so an expiry can be exercised without sleeping for an
    hour; it is read from the environment, never from a flag a user might pass
    by accident. The drill uses a short real grant as well, so the timer path is
    exercised with the real clock.
    """
    raw = os.environ.get("ODILE_SUPPORT_NOW", "").strip()
    if raw:
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SupportError(f"ODILE_SUPPORT_NOW is not an ISO-8601 time: {raw!r}") from exc
        return value.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# --------------------------------------------------------------------------
# Paths


@dataclass(frozen=True)
class Paths:
    """Every absolute path the program touches, relative to an optional root."""

    root: Path

    def at(self, absolute: str) -> Path:
        if str(self.root) in ("", "."):
            return Path(absolute)
        return self.root / absolute.lstrip("/")

    @property
    def authorized_keys(self) -> Path:
        return self.at(AUTHORIZED_KEYS)

    @property
    def state(self) -> Path:
        return self.at(STATE_PATH)

    @property
    def log(self) -> Path:
        return self.at(LOG_PATH)

    @property
    def key_file(self) -> Path:
        return self.at(KEY_PATH)

    @property
    def program(self) -> Path:
        return self.at(PROGRAM)

    @property
    def timer(self) -> Path:
        return self.at(UNIT_DIR + "/" + TIMER)

    @property
    def systemd_live(self) -> bool:
        return self.at("/run/systemd/system").is_dir()


def program_command(paths: Paths, *arguments: str) -> str:
    """The program plus its arguments, quoted for sshd's `command="..."`.

    sshd runs the forced command through the user's shell, so every token is
    shell-quoted. On a tenant the root is `/` and the result is exactly
    `/usr/local/sbin/odile-support session`; a drill root adds its own `--root`
    so the same authorized_keys line can be executed by a real sshd.
    """
    tokens = [str(paths.program)]
    if paths.root != Path("/"):
        tokens += ["--root", str(paths.root)]
    tokens += list(arguments)
    return " ".join(shlex.quote(token) for token in tokens)


def session_command(paths: Paths) -> str:
    return program_command(paths, "session")


# --------------------------------------------------------------------------
# Public keys


@dataclass(frozen=True)
class PublicKey:
    key_type: str
    blob: bytes
    blob_b64: str
    comment: str

    @property
    def line(self) -> str:
        return f"{self.key_type} {self.blob_b64}" + (f" {self.comment}" if self.comment else "")


KEY_TYPE_RE = re.compile(r"^(ssh-|ecdsa-|sk-ssh-|sk-ecdsa-)")


def parse_public_key(text: str, where: str = KEY_PATH) -> PublicKey:
    """One OpenSSH public key, or a refusal.

    The seed file is written by the installer from the signed seed, but this
    parser is the boundary that keeps a stray `command="..."` option or a second
    line from turning into an `authorized_keys` injection. It accepts the
    comment's spaces and parentheses (the real seed key carries
    `support@odile.fyi (ODL-217 pending)`) and refuses control characters.
    `where` names the file in the refusal; the grant state reuses the same
    parser for the key it holds.
    """
    if "PRIVATE KEY" in text:
        raise KeyFileError(f"{where} holds a private key, not a public key")
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise KeyFileError(f"{where} must hold exactly one public key (found {len(lines)} lines)")
    line = lines[0].strip()
    if any(ord(char) < 32 for char in line):
        raise KeyFileError(f"{where} contains a control character")
    fields = line.split(None, 2)
    if len(fields) < 2 or not KEY_TYPE_RE.match(fields[0]):
        raise KeyFileError(
            f"{where} is not a bare public key (an options prefix is not accepted)")
    key_type, encoded = fields[0], fields[1]
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise KeyFileError(f"{where} has a key blob that is not valid base64") from exc
    if len(blob) < 8:
        raise KeyFileError(f"{where} has an empty key blob")
    length = int.from_bytes(blob[:4], "big")
    if length <= 0 or length > len(blob) - 4:
        raise KeyFileError(f"{where} has a malformed key blob")
    try:
        wire_type = blob[4:4 + length].decode("ascii")
    except UnicodeDecodeError as exc:
        raise KeyFileError(f"{where} has a key blob with a non-ASCII type") from exc
    if wire_type != key_type:
        raise KeyFileError(
            f"{where} says {key_type!r} but its blob says {wire_type!r}")
    return PublicKey(key_type=key_type, blob=blob, blob_b64=encoded,
                     comment=fields[2].strip() if len(fields) > 2 else "")


def state_public_key(state: dict) -> PublicKey:
    """The public key a live grant's state holds, validated before it is used.

    The state is root-owned and written by this program, but a restore must not
    install a line built from a corrupted file: an invalid blob is a refusal,
    not a key. There is no private material in the state to protect.
    """
    key_type = str(state.get("key_type") or "").strip()
    blob = str(state.get("key_blob") or "").strip()
    comment = str(state.get("key_comment") or "").strip()
    if not key_type or not blob:
        raise KeyFileError(f"{STATE_PATH} holds no public key for the grant")
    line = f"{key_type} {blob}" + (f" {comment}" if comment else "")
    return parse_public_key(line, where=STATE_PATH)


def fingerprint(blob: bytes) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def fingerprint_from_line(line: str) -> str:
    return fingerprint(parse_public_key(line).blob)


# --------------------------------------------------------------------------
# State and the authorized_keys line


def load_state(paths: Paths) -> dict | None:
    try:
        raw = paths.state.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SupportError(f"cannot read {paths.state}: {exc}") from exc
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SupportError(f"{paths.state} is not valid JSON: {exc}") from exc
    if not isinstance(state, dict):
        raise SupportError(f"{paths.state} does not hold an object")
    return state


def write_atomic(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        if os.geteuid() == 0:
            os.chown(temporary, 0, 0)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_state(paths: Paths, state: dict) -> None:
    # Public material only: the key blob, its fingerprint, the expiry, the slug.
    # 0644 lets `odile-support status` run as the tenant user without sudo.
    write_atomic(paths.state, (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8"), 0o644)


def read_authorized(paths: Paths) -> str:
    try:
        return paths.authorized_keys.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except PermissionError:
        # A status run as the tenant user cannot read root's key file; the
        # caller says so instead of claiming the key is missing.
        return ""


def grant_line(paths: Paths, state: dict) -> str:
    """The one authorized_keys line a grant owns.

    `no-pty` is deliberately absent: the whole point is an interactive shell.
    Every forwarding channel is closed, and `command=` pins the key to the
    session wrapper, so the key cannot be used for a tunnel or an arbitrary
    command.
    """
    options = (
        f'command="{session_command(paths)}",no-agent-forwarding,no-port-forwarding,'
        "no-X11-forwarding,no-user-rc"
    )
    return (
        f"{options} {state['key_type']} {state['key_blob']} "
        f"{GRANT_MARKER}{state['grant_id']}"
    )


def is_our_grant_line(line: str, paths: Paths) -> bool:
    """True only for the exact forced-command line this grant wrote.

    The marker alone is not enough: a hand-written line that merely mentions
    `odile-support-grant-` is left alone. The command must be this root's
    session wrapper.
    """
    return f'command="{session_command(paths)}"' in line and GRANT_MARKER in line


def remove_our_lines(text: str, paths: Paths) -> tuple[str, int]:
    kept: list[str] = []
    removed = 0
    for line in text.splitlines():
        if is_our_grant_line(line, paths):
            removed += 1
        else:
            kept.append(line)
    if not kept:
        return "", removed
    return "\n".join(kept) + "\n", removed


def write_authorized(paths: Paths, text: str) -> None:
    """Replace root's key file atomically, 0600, with a 0700 parent."""
    if paths.authorized_keys.parent == paths.at("/root/.ssh"):
        paths.authorized_keys.parent.mkdir(parents=True, exist_ok=True)
        try:
            paths.authorized_keys.parent.chmod(0o700)
        except OSError:
            pass
    write_atomic(paths.authorized_keys, text.encode("utf-8"), 0o600)


def rewrite_authorized(paths: Paths, transform) -> int:
    text = read_authorized(paths)
    new, removed = transform(text)
    if new != text:
        write_authorized(paths, new)
    return removed


def append_log(paths: Paths, line: str) -> None:
    """Append one audit line. The log is on the guest; a root shell can edit it."""
    paths.log.parent.mkdir(parents=True, exist_ok=True)
    with open(paths.log, "a", encoding="utf-8") as handle:
        handle.write(line.rstrip("\n") + "\n")
    try:
        os.chmod(paths.log, 0o644)
    except OSError:
        pass


def log_event(paths: Paths, state: dict | None, event: str, **fields: object) -> None:
    parts = [iso(now_utc()), f"event={event}"]
    if state:
        parts.append(f"grant={state.get('grant_id', '-')}")
    for key in sorted(fields):
        value = str(fields[key]).replace("\n", " ").replace("\r", " ")
        if " " in value:
            value = shlex.quote(value)
        parts.append(f"{key}={value}")
    append_log(paths, " ".join(parts))


# --------------------------------------------------------------------------
# Grant commands


def require_root() -> None:
    if os.geteuid() != 0:
        raise NotRoot("run this with sudo: it writes /root/.ssh/authorized_keys")


def load_live_state(paths: Paths, now: datetime) -> dict | None:
    state = load_state(paths)
    if state is None:
        return None
    if state.get("revoked_at") or state.get("expired_at"):
        return None
    try:
        expires = datetime.fromisoformat(str(state["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    return state if expires > now else None


def ensure_grant_line(paths: Paths, state: dict) -> int:
    """Put a live grant's one line back if something removed it.

    The tenant image's release installer rewrites `authorized_keys` to exactly
    one line — the measured boot key — on every boot, so a grant line does not
    survive a reboot on its own. The line is rebuilt from the state (public
    material only) after validating the key, and the boot key's own line is
    preserved byte-for-byte. Returns 1 when it wrote, 0 when the line was
    already there.
    """
    text = read_authorized(paths)
    if any(is_our_grant_line(line, paths) for line in text.splitlines()):
        return 0
    state_public_key(state)
    line = grant_line(paths, state)
    body = text if not text or text.endswith("\n") else text + "\n"
    write_authorized(paths, body + line + "\n")
    return 1


def converge_grant(paths: Paths, now: datetime, quiet: bool = False) -> bool:
    """Make `authorized_keys` agree with the grant state.

    - no state, or a tombstone: remove any line of ours; restore nothing.
    - live: put the line back when a boot removed it (a `reassert` log line).
    - due: remove the line, write the `expired_at` tombstone, log the expiry.

    `status`, `expire`, `session` and `diag` all call this, so a machine whose
    timer never started still converges the next time anyone looks.
    """
    state = load_state(paths)
    if state is None:
        return False
    if state.get("revoked_at") or state.get("expired_at"):
        return rewrite_authorized(paths, lambda text: remove_our_lines(text, paths)) > 0
    try:
        expires = datetime.fromisoformat(str(state["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return False
    if expires <= now:
        removed = rewrite_authorized(paths, lambda text: remove_our_lines(text, paths))
        state["expired_at"] = iso(now)
        save_state(paths, state)
        log_event(paths, state, "expire", removed=removed, expires=state["expires_at"])
        if not quiet:
            print(f"expired {state.get('grant_id', '-')}: removed {removed} key line(s) from {AUTHORIZED_KEYS}")
        return True
    try:
        restored = ensure_grant_line(paths, state)
    except SupportError as exc:
        # Leave the machine as it is: no key is safer than a bad key.
        log_event(paths, state, "reassert_failed", detail=str(exc))
        print(f"odile-support: cannot restore the grant line: {exc}", file=sys.stderr)
        return False
    if restored:
        log_event(paths, state, "reassert", restored=restored)
        if not quiet:
            print(f"restored {state.get('grant_id', '-')}: the grant is live and its line was"
                  f" missing from {AUTHORIZED_KEYS}; wrote it back")
        return True
    return False


def install_timer(paths: Paths, dry: bool, no_systemd: bool) -> bool:
    """Write and enable the expiry timer. Returns True when systemd is live."""
    if not paths.systemd_live or no_systemd:
        return False
    service = paths.at(UNIT_DIR + "/odile-support-expire.service")
    timer = paths.timer
    if dry:
        print(f"would write {service} and {timer}")
        print(f"would run: systemctl daemon-reload; systemctl enable --now {TIMER}")
        return True
    write_atomic(service, SERVICE_UNIT.format(program=program_command(paths)).encode("utf-8"), 0o644)
    write_atomic(timer, TIMER_UNIT.encode("utf-8"), 0o644)
    for command in (["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", TIMER]):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TimerError(f"{' '.join(command)} failed: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise TimerError(f"{' '.join(command)} exited {result.returncode}: {detail}")
        print(f"{' '.join(command)}: ok")
    return True


def grant_card(paths: Paths, state: dict, expires: datetime, now: datetime, refreshed: bool) -> str:
    left = human_duration((expires - now).total_seconds())
    header = "grant live (refreshed)" if refreshed else "grant live"
    return f"""\
{header}
  tenant:     {state['slug']}
  reason:     {state.get('reason', '-')}
  expires:    {state['expires_at']} ({left} left)
  key:        {state['key_fingerprint']} ({GRANT_MARKER}{state['grant_id']})
  revoke:     sudo odile-support revoke
  log:        {LOG_PATH}

  This is a handover, not a peek.
  On this unlocked machine the key can read the mounted disk, every file, the
  agent's memory and past prompts, the sign-in hash, and the backup password if
  the recovery file is still on the machine. It can add a LUKS keyslot or copy
  the volume key, so it can keep disk access after the grant.
  Expiry and revoke block new logins; an open session keeps running until it
  exits. To end one: run `w` to find its pts/N, then `pkill -t pts/N`.
  A reboot does not close the grant: this image's release installer rewrites
  root's authorized_keys at boot, and this grant's timer puts its line back
  within 30 seconds. The closures are the expiry above and `sudo odile-support
  revoke`.
  After revoke, check for what a root shell could leave behind:
    sudo cat /root/.ssh/authorized_keys
    DEV=$(sudo cryptsetup status root_crypt | sed -n 's/^ *device: *//p')
    sudo cryptsetup luksDump "$DEV"
    if command -v crontab >/dev/null; then sudo crontab -l; else echo 'crontab not installed'; fi
    sudo systemctl list-timers --all
  The log is written on this machine; a root shell can edit it.
"""


def cmd_status(args, paths: Paths) -> int:
    now = now_utc()
    if getattr(args, "expire", True) and os.geteuid() == 0:
        converge_grant(paths, now, quiet=True)
    state = load_state(paths)
    live = load_live_state(paths, now)
    readable = os.access(paths.authorized_keys, os.R_OK)
    authorized = read_authorized(paths)
    key_lines = [line for line in authorized.splitlines() if line.strip()]
    our_lines = [line for line in key_lines if is_our_grant_line(line, paths)]

    if live is not None:
        try:
            expires = datetime.fromisoformat(str(live["expires_at"]).replace("Z", "+00:00"))
        except ValueError:
            expires = now
        restored = last_event(paths, str(live.get("grant_id", "")), "reassert")
        data = {
            "grant": {
                "grant_id": live.get("grant_id"),
                "slug": live.get("slug"),
                "reason": live.get("reason", "-"),
                "created_at": live.get("created_at"),
                "expires_at": live.get("expires_at"),
                "hours": live.get("hours"),
                "key_fingerprint": live.get("key_fingerprint"),
                "key_comment": live.get("key_comment"),
                "key_installed": bool(our_lines),
                "restored_at": restored.split(" ", 1)[0] if restored else None,
            },
            "authorized_keys": {"lines": len(key_lines), "grant_lines": len(our_lines),
                                "readable": readable},
        }
        if args.json:
            print(json.dumps(data, indent=2, sort_keys=True))
            return EXIT_OK
        print("grant live")
        print(f"  tenant:     {live.get('slug', '-')}")
        print(f"  reason:     {live.get('reason', '-')}")
        print(f"  expires:    {live.get('expires_at')} ({human_duration((expires - now).total_seconds())} left)")
        print(f"  key:        {live.get('key_fingerprint')} ({GRANT_MARKER}{live.get('grant_id')})")
        if not readable:
            print("  key line:   unreadable without root (run: sudo odile-support status)")
        else:
            print(f"  key line:   {'present' if our_lines else 'MISSING from ' + AUTHORIZED_KEYS}")
        if restored:
            print(f"  restored:   {restored.split(' ', 1)[0]}"
                  " (a boot rewrote authorized_keys; the timer put the line back)")
        print(f"  revoke:     sudo odile-support revoke")
        last = last_session(paths, str(live.get("grant_id", "")))
        if last:
            print(f"  last session: {last}")
        return EXIT_OK

    if args.json:
        print(json.dumps({
            "grant": None,
            "expired_at": (state or {}).get("expired_at"),
            "revoked_at": (state or {}).get("revoked_at"),
            "authorized_keys": {"lines": len(key_lines), "grant_lines": len(our_lines)},
        }, indent=2, sort_keys=True))
        return EXIT_OK
    if state and state.get("expired_at"):
        print(f"no grant (expired {state.get('expires_at', '-')})")
    elif state and not live and state.get("expires_at") and not state.get("revoked_at"):
        # A non-root status run cannot close the grant; it still reports it as due.
        print(f"no grant (expired {state.get('expires_at')})")
    elif state and state.get("revoked_at"):
        print(f"no grant (revoked {state['revoked_at']})")
    else:
        print("no grant")
    return EXIT_OK


def last_event(paths: Paths, grant_id: str, event: str) -> str:
    """The most recent exact-event log line for a grant, or ""."""
    try:
        lines = paths.log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    needle = f"event={event}"
    for line in reversed(lines):
        if needle in line.split() and (not grant_id or f"grant={grant_id}" in line):
            return line
    return ""


def last_session(paths: Paths, grant_id: str) -> str:
    return last_event(paths, grant_id, "start")


def tenant_slug(paths: Paths, explicit: str = "") -> str:
    """The slug the card names: the flag, the cached seed's public field, hostname."""
    if explicit:
        return explicit
    text = _read(paths.at(SEED_PATH))
    if text:
        try:
            slug = str(json.loads(text).get("slug") or "").strip()
        except json.JSONDecodeError:
            slug = ""
        if slug:
            return slug
    return platform.node() or "unknown"


def cmd_grant(args, paths: Paths) -> int:
    require_root()
    hours = float(args.hours)
    if not (0 < hours <= MAX_HOURS):
        raise SupportError(f"--hours must be greater than 0 and at most {MAX_HOURS:g}", EXIT_USAGE)
    try:
        key = parse_public_key(paths.key_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KeyFileError(f"no support public key at {KEY_PATH}") from exc
    except OSError as exc:
        raise KeyFileError(f"cannot read the support public key at {KEY_PATH}: {exc}") from exc
    except KeyFileError as exc:
        raise KeyFileError(f"support public key: {exc}") from exc
    if not paths.program.is_file():
        raise KeyFileError(
            f"the forced command {PROGRAM} is not installed; place it before granting")
    if not os.access(paths.program, os.X_OK):
        raise KeyFileError(f"{PROGRAM} is not executable; place it before granting")

    now = now_utc()
    expires = now + timedelta(hours=hours)
    previous = load_state(paths)
    live_before = load_live_state(paths, now)
    state = {
        "version": 1,
        "grant_id": base64.b16encode(os.urandom(6)).decode("ascii").lower(),
        "slug": tenant_slug(paths, args.slug),
        "reason": (" ".join(args.reason.split())[:200] or "-"),
        "created_at": iso(now),
        "expires_at": iso(expires),
        "hours": hours,
        "key_type": key.key_type,
        "key_blob": key.blob_b64,
        "key_fingerprint": fingerprint(key.blob),
        "key_comment": key.comment,
    }
    line = grant_line(paths, state)
    new_keys, _ = remove_our_lines(read_authorized(paths), paths)
    if new_keys and not new_keys.endswith("\n"):
        new_keys += "\n"
    new_keys += line + "\n"

    if args.dry_run:
        print(f"would grant {hours:g}h until {state['expires_at']} to {state['key_fingerprint']}")
        print(f"would write {STATE_PATH} (0644) and one line in {AUTHORIZED_KEYS}:")
        print("  " + line)
        if paths.systemd_live and not args.no_systemd:
            print(f"would write and enable {UNIT_DIR}/{TIMER}")
        else:
            print("systemd is not running; expiry is enforced on the next status, grant or expire run")
        return EXIT_OK

    previous_keys = read_authorized(paths)
    save_state(paths, state)
    try:
        write_atomic(paths.authorized_keys, new_keys.encode("utf-8"), 0o600)
        install_timer(paths, dry=False, no_systemd=args.no_systemd)
    except (SupportError, OSError) as exc:
        # Put the machine back exactly: the old key file and the old state (or
        # no state). A grant that cannot install its expiry timer is not a grant.
        write_atomic(paths.authorized_keys, previous_keys.encode("utf-8"), 0o600)
        if previous is None:
            try:
                paths.state.unlink()
            except FileNotFoundError:
                pass
        else:
            save_state(paths, previous)
        raise SupportError(f"grant rolled back: {exc}", getattr(exc, "exit_code", EXIT_RUNTIME)) from exc

    log_event(paths, state, "grant", hours=f"{hours:g}", expires=state["expires_at"],
              fingerprint=state["key_fingerprint"], reason=state["reason"])
    print(grant_card(paths, state, expires, now, refreshed=bool(live_before)))
    if not paths.systemd_live or args.no_systemd:
        print("systemd is not running; expiry and the post-boot restore are enforced"
              " on the next status, grant or expire run")
    return EXIT_OK


def cmd_revoke(args, paths: Paths) -> int:
    require_root()
    now = now_utc()
    state = load_state(paths)
    removed = 0
    if args.dry_run:
        text = read_authorized(paths)
        count = len([line for line in text.splitlines() if is_our_grant_line(line, paths)])
        print(f"would remove {count} key line(s) from {AUTHORIZED_KEYS} and clear the grant")
        return EXIT_OK
    removed = rewrite_authorized(paths, lambda text: remove_our_lines(text, paths))
    if state is not None:
        state["revoked_at"] = iso(now)
        save_state(paths, state)
        log_event(paths, state, "revoke", removed=removed)
    if state is None and removed == 0:
        print("no grant")
        return EXIT_OK
    print("grant revoked")
    print(f"  removed: {removed} key line(s) from {AUTHORIZED_KEYS}")
    if state:
        print(f"  key:     {state.get('key_fingerprint', '-')} ({GRANT_MARKER}{state.get('grant_id', '-')})")
    print("  An open session keeps running until it exits. Run `w` to find its pts/N,")
    print("  then `pkill -t pts/N`.")
    print("  After revoke, check for what a root shell could leave behind:")
    print("    sudo cat /root/.ssh/authorized_keys")
    print("    DEV=$(sudo cryptsetup status root_crypt | sed -n 's/^ *device: *//p')")
    print('    sudo cryptsetup luksDump "$DEV"')
    print("    if command -v crontab >/dev/null; then sudo crontab -l;"
          " else echo 'crontab not installed'; fi")
    print("    sudo systemctl list-timers --all")
    return EXIT_OK


def cmd_expire(args, paths: Paths) -> int:
    require_root()
    converge_grant(paths, now_utc(), quiet=args.quiet)
    return EXIT_OK


def cmd_session(args, paths: Paths) -> int:
    now = now_utc()
    state = load_state(paths)
    if state is None or state.get("revoked_at"):
        print("odile-support: no live grant", file=sys.stderr)
        return EXIT_RUNTIME
    if state.get("expired_at"):
        print(f"odile-support: the support grant expired at {state.get('expires_at', '-')}",
              file=sys.stderr)
        return EXIT_RUNTIME
    try:
        expires = datetime.fromisoformat(str(state["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        print("odile-support: the grant state has no usable expiry", file=sys.stderr)
        return EXIT_RUNTIME
    if expires <= now:
        if os.geteuid() == 0:
            converge_grant(paths, now, quiet=True)
        print(f"odile-support: the support grant expired at {state.get('expires_at')}", file=sys.stderr)
        return EXIT_RUNTIME

    connection = os.environ.get("SSH_CONNECTION", "").split()
    remote = f"{connection[0]}:{connection[1]}" if len(connection) >= 2 else "-"
    original = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip() or "-"
    log_event(paths, state, "start", key=state.get("key_fingerprint", "-"), remote=remote,
              command=original[:200])
    started = time.monotonic()
    shell = args.shell or os.environ.get("SHELL") or "/bin/bash"
    if not os.path.isabs(shell) or not os.access(shell, os.X_OK):
        shell = "/bin/sh"
    argv = [shell, "-l"] if Path(shell).name in ("bash", "zsh", "sh", "dash") else [shell]

    # The user ends a session with `pkill -t pts/N`, which sends SIGTERM to
    # everything on the terminal, this wrapper included. A start line without an
    # end line is an audit gap, so the wrapper catches the signal, stops the
    # shell and writes the end line with the signal name and 128+signum.

    class SessionSignal(Exception):
        def __init__(self, number: int) -> None:
            super().__init__(number)
            self.number = number

    def catch(signum: int, frame) -> None:
        raise SessionSignal(int(signum))

    handlers = {}
    delivered = 0
    code = 0
    try:
        try:
            process = subprocess.Popen(argv)
        except OSError as exc:
            print(f"odile-support: cannot start {shell}: {exc}", file=sys.stderr)
            code = 127
        else:
            for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                try:
                    handlers[signum] = signal.signal(signum, catch)
                except (ValueError, OSError):
                    pass
            try:
                code = process.wait()
            except SessionSignal as exc:
                # The shell may still be alive if the wrapper alone was signalled.
                delivered = exc.number
                try:
                    process.terminate()
                except OSError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                code = 128 + delivered
            if code < 0:
                code = 128 - code
    finally:
        for signum, handler in handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
    seconds = int(round(time.monotonic() - started))
    fields: dict[str, object] = {"rc": code, "seconds": seconds}
    if delivered:
        try:
            fields["signal"] = signal.Signals(delivered).name
        except ValueError:
            fields["signal"] = str(delivered)
    log_event(paths, state, "end", **fields)
    return code


# --------------------------------------------------------------------------
# Redaction


PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
SECRET_NAME = (
    r"(?:PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|API_KEY|APIKEY|PRIVATE_KEY|"
    r"CREDENTIAL|AUTH[A-Z0-9_]*|NEAR_API_KEY)"
)
ASSIGNMENT_RE = re.compile(
    rf"(?im)(?P<prefix>(?:^|[ \t])(?:export[ \t]+)?[A-Za-z0-9_.-]*{SECRET_NAME}[A-Za-z0-9_.-]*"
    rf"[ \t]*[=:][ \t]*)(?P<value>\S.*)$"
)
JSON_SECRET_RE = re.compile(
    rf'(?i)("(?:[A-Za-z0-9_.-]*)(?:password|passwd|passphrase|secret|token|api_key|apikey|'
    rf'private_key|credential|near_api_key)(?:[A-Za-z0-9_.-]*)"\s*:\s*")[^"]*(")'
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
TOKENISH_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,}|xox[bp]-[A-Za-z0-9-]{10,})")
ODILE_TOKEN_RE = re.compile(r"odile\.token=[0-9a-fA-F]+")
SCRYPT_RE = re.compile(r"scrypt\$[A-Za-z0-9$./+=-]{8,}")


def _redact(text: str) -> tuple[str, int]:
    count = 0

    def swap(pattern: re.Pattern[str], replacement, source: str) -> str:
        nonlocal count
        new, number = pattern.subn(replacement, source)
        count += number
        return new

    text = swap(PRIVATE_KEY_RE, "[redacted private key]", text)
    text = swap(JSON_SECRET_RE, r"\1[redacted]\2", text)
    text = swap(ASSIGNMENT_RE, lambda match: match.group("prefix") + "[redacted]", text)
    text = swap(BEARER_RE, "Bearer [redacted]", text)
    text = swap(TOKENISH_RE, "[redacted]", text)
    text = swap(ODILE_TOKEN_RE, "odile.token=[redacted]", text)
    text = swap(SCRYPT_RE, "scrypt$[redacted]", text)
    return text, count


def redact(text: str) -> str:
    """The public form: every secret-shaped string replaced, count discarded."""
    return _redact(text)[0]


# --------------------------------------------------------------------------
# Diagnostics


def _command(argv: list[str], timeout: float = 10.0, max_lines: int = 400) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                errors="replace")
    except FileNotFoundError:
        return f"[not installed: {argv[0]}]"
    except subprocess.TimeoutExpired:
        return f"[timed out after {timeout:g}s: {' '.join(argv)}]"
    except OSError as exc:
        return f"[error: {exc}]"
    text = (result.stdout or "") + (result.stderr or "")
    if not text.strip():
        return f"[no output; exit {result.returncode}]"
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"[{len(lines) - max_lines} more lines]"]
    return "\n".join(lines).rstrip()


def _read(path: Path, limit: int = 65536) -> str | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"[unreadable: {exc}]"
    text = data[:limit].decode("utf-8", "replace")
    if len(data) > limit:
        text += f"\n[{len(data) - limit} bytes truncated]"
    return text.rstrip()


def _sha256(path: Path) -> tuple[str, int] | None:
    try:
        digest = hashlib.sha256()
        size = 0
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
                size += len(block)
        return digest.hexdigest(), size
    except OSError:
        return None


def _os_release(paths: Paths) -> str:
    text = _read(paths.at("/etc/os-release"))
    if text is None:
        return "[no /etc/os-release]"
    wanted = ("PRETTY_NAME", "ID", "VERSION_ID")
    rows = [line for line in text.splitlines() if line.split("=", 1)[0] in wanted]
    return "\n".join(rows) or "[no PRETTY_NAME]"


def _seed_public(paths: Paths) -> str:
    text = _read(paths.at(SEED_PATH))
    if text is None:
        return "[no cached seed]"
    try:
        seed = json.loads(text)
    except json.JSONDecodeError:
        return "[cached seed is not JSON]"
    rows = []
    for name in ("slug", "hermes_version", "backup_repo_url"):
        if name in seed:
            rows.append(f"{name}: {seed[name]}")
    return "\n".join(rows) or "[cached seed has no public fields]"


def _boot_record(paths: Paths) -> str:
    text = _read(paths.at(BOOT_RECORD))
    if text is None:
        return "[no boot record]"
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return "[boot record is not JSON]"
    rows = []
    for name in ("release", "measurement", "cmdline_sha256", "installer_sha256", "checked_at"):
        if name in record:
            rows.append(f"{name}: {record[name]}")
    return "\n".join(rows)


def _manifest(paths: Paths) -> str:
    path = paths.at(MANIFEST)
    if not path.is_file():
        return "[no signed manifest]"
    digest = _sha256(path)
    return f"present, {digest[1]} bytes, sha256 {digest[0]}" if digest else "[unreadable]"


def _installer(paths: Paths) -> str:
    path = paths.at(INSTALLER_COPY)
    if not path.is_file():
        return "[no placed installer]"
    digest = _sha256(path)
    return f"present, {digest[1]} bytes, sha256 {digest[0]}" if digest else "[unreadable]"


def _support_key_summary(paths: Paths) -> str:
    text = _read(paths.key_file)
    if text is None:
        return f"[no support public key at {KEY_PATH}]"
    try:
        key = parse_public_key(text)
    except SupportError as exc:
        return f"[invalid support public key: {exc}]"
    return f"{fingerprint(key.blob)} ({key.comment or 'no comment'})"


def _support_log_tail(paths: Paths, lines: int = 100) -> str:
    text = _read(paths.log)
    if text is None:
        return f"[no {LOG_PATH}]"
    rows = text.splitlines()
    return "\n".join(rows[-lines:])


def diag_text(paths: Paths, now: datetime) -> str:
    tenant = "unknown"
    seed_text = _read(paths.at(SEED_PATH))
    if seed_text:
        try:
            tenant = str(json.loads(seed_text).get("slug") or tenant)
        except json.JSONDecodeError:
            pass
    boot = _boot_record(paths)
    release = "-"
    for line in boot.splitlines():
        if line.startswith("release: "):
            release = line.split(": ", 1)[1]
            break

    system = "\n".join([
        _os_release(paths),
        "",
        "$ uname -a",
        _command(["uname", "-a"]),
        "$ uptime -p",
        _command(["uptime", "-p"]),
        "$ nproc",
        _command(["nproc"]),
        "$ free -h",
        _command(["free", "-h"]),
        "$ python3 --version",
        f"Python {platform.python_version()}",
        "$ openssl version",
        _command(["openssl", "version"]),
        "$ ssh -V",
        _command(["ssh", "-V"]),
        "",
        f"/etc/odile/production: {_read(paths.at('/etc/odile/production')) or '[not present]'}",
        f"/etc/odile/seal-id: {_read(paths.at('/etc/odile/seal-id')) or '[not present]'}",
        "",
        "seed (public fields):",
        _seed_public(paths),
        "",
        "boot record:",
        boot,
        "",
        f"placed installer: {_installer(paths)}",
        f"signed manifest: {_manifest(paths)}",
    ])
    disk = "\n".join([
        "$ df -hT",
        _command(["df", "-hT"]),
        "",
        "$ lsblk",
        _command(["lsblk", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS"]),
        "",
        "$ cryptsetup status root_crypt",
        _command(["cryptsetup", "status", "root_crypt"]),
        "",
        "$ du -sh /var/lib/odile /var/log",
        _command(["du", "-sh", "/var/lib/odile", "/var/log"]),
    ])
    services = "\n".join([
        "$ systemctl --failed",
        _command(["systemctl", "--failed", "--no-legend", "--no-pager"]),
        "",
        "$ systemctl is-active odile-agent odile-attest-boot odile-access-report caddy",
        _command(["systemctl", "is-active", "odile-agent",
                  "odile-attest-boot", "odile-access-report", "caddy"]),
        "",
        "$ systemctl status --lines=0",
        _command(["systemctl", "status", "--no-pager", "--lines=0",
                  "odile-agent", "odile-attest-boot",
                  "odile-access-report", "caddy"]),
        "",
        "$ systemctl list-timers odile-support-expire.timer",
        _command(["systemctl", "list-timers", "--no-pager", "odile-support-expire.timer"]),
    ])
    logs = "\n".join([
        "$ journalctl -u odile-agent -u odile-access-report -u odile-attest-boot -n 200",
        _command(["journalctl", "-u", "odile-agent",
                  "-u", "odile-access-report", "-u", "odile-attest-boot",
                  "-n", "200", "--no-pager", "-o", "short-iso"]),
        "",
        f"tail of {LOG_PATH}:",
        _support_log_tail(paths),
    ])
    report = _read(paths.at(ACCESS_REPORT))
    audit = "\n".join([
        f"{ACCESS_REPORT}:",
        report if report is not None else
        "audit report not present (run: systemctl restart odile-access-report.service)",
        "",
        "support public key: " + _support_key_summary(paths),
    ])
    support = "\n".join([
        "$ odile-support status",
        status_text(paths, now),
        "",
        f"support key: {_support_key_summary(paths)}",
        f"support log: {LOG_PATH}",
    ])

    header = "\n".join([
        "odile-diag — diagnostics bundle",
        f"generated_at: {iso(now)}",
        f"tenant: {tenant}",
        f"hostname: {platform.node()}",
        f"kernel: {platform.release()}",
        f"release: {release}",
        "excluded: passphrases, private keys, tokens, prompt history and file contents",
    ])
    body = "\n\n".join([
        header,
        "===== system =====\n" + system,
        "===== disk =====\n" + disk,
        "===== services =====\n" + services,
        "===== logs =====\n" + logs,
        "===== audit =====\n" + audit,
        "===== support =====\n" + support,
    ])
    cleaned, count = _redact(body)
    # A second pass is the belt-and-braces check: a replacement can expose a
    # match that overlapped the first one (a key block inside a JSON string).
    cleaned, again = _redact(cleaned)
    footer = f"===== redaction =====\n{count + again} replacement(s); no secret-shaped value remains"
    return cleaned + "\n\n" + footer + "\n"


def status_text(paths: Paths, now: datetime) -> str:
    """`status` as text, without the argparse namespace's JSON branch."""
    state = load_state(paths)
    live = load_live_state(paths, now)
    if live is not None:
        try:
            expires = datetime.fromisoformat(str(live["expires_at"]).replace("Z", "+00:00"))
        except ValueError:
            expires = now
        restored = last_event(paths, str(live.get("grant_id", "")), "reassert")
        rows = [f"grant live\n  tenant:     {live.get('slug', '-')}\n"
                f"  reason:     {live.get('reason', '-')}\n"
                f"  expires:    {live.get('expires_at')} "
                f"({human_duration((expires - now).total_seconds())} left)\n"
                f"  key:        {live.get('key_fingerprint')} "
                f"({GRANT_MARKER}{live.get('grant_id')})"]
        if restored:
            rows.append(f"  restored:   {restored.split(' ', 1)[0]}"
                        " (a boot rewrote authorized_keys; the timer put the line back)")
        return "\n".join(rows)
    if state and state.get("expired_at"):
        return f"no grant (expired {state.get('expires_at', '-')})"
    if state and state.get("revoked_at"):
        return f"no grant (revoked {state['revoked_at']})"
    return "no grant"


def cmd_diag(args, paths: Paths) -> int:
    now = now_utc()
    if os.geteuid() == 0:
        converge_grant(paths, now, quiet=True)
    text = diag_text(paths, now)
    if args.out == "-":
        sys.stdout.write(text)
        return EXIT_OK
    if args.out:
        target = Path(args.out)
    else:
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        if os.geteuid() == 0:
            target = paths.at(f"/var/lib/odile/diag-{stamp}.txt")
        else:
            target = Path.home() / f"odile-diag-{stamp}.txt"
    target = target.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    os.chmod(target, 0o644)
    print(f"wrote {target} ({len(text.encode('utf-8'))} bytes)")
    print("Send this file to help@odile.fyi. It holds no password, key, prompt history or file contents.")
    return EXIT_OK


# --------------------------------------------------------------------------
# Command line


COMMANDS = {"status", "grant", "revoke", "expire", "session", "diag"}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--root", default=argparse.SUPPRESS,
        help="relocate every absolute path under this directory (drill and tests)",
    )
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name if Path(sys.argv[0]).name in ("odile-support", "odile-diag")
        else "odile-support",
        description="Odile support: a diagnostics bundle by default, a time-boxed grant by choice.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", parents=[common], help="report the live grant; the default is none")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.add_argument("--no-expire", dest="expire", action="store_false",
                        help="do not close a due grant first")
    status.set_defaults(func=cmd_status)

    grant = sub.add_parser("grant", parents=[common], help="install the support key for HOURS")
    grant.add_argument("--hours", type=float, default=24.0,
                       help="hours until the grant expires (default 24, max 720)")
    grant.add_argument("--slug", default="", help="the tenant slug shown on the card (default: hostname)")
    grant.add_argument("--reason", default="",
                       help="why the grant was made; recorded in the state and the log")
    grant.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    grant.add_argument("--no-systemd", action="store_true",
                       help="do not write or enable the expiry timer")
    grant.set_defaults(func=cmd_grant)

    revoke = sub.add_parser("revoke", parents=[common], help="remove the support key now")
    revoke.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    revoke.set_defaults(func=cmd_revoke)

    expire = sub.add_parser("expire", parents=[common],
                            help="expire a due grant, or restore a live grant's evicted line")
    expire.add_argument("--quiet", action="store_true", help="print nothing when there is nothing to do")
    expire.set_defaults(func=cmd_expire)

    session = sub.add_parser("session", parents=[common], help="the forced command sshd runs")
    session.add_argument("--shell", default="", help="shell to run (default: $SHELL or /bin/bash)")
    session.set_defaults(func=cmd_session)

    diag = sub.add_parser("diag", parents=[common], help="write one diagnostics bundle")
    diag.add_argument("--out", default="", help="write here ('-' for stdout); default /var/lib/odile")
    diag.set_defaults(func=cmd_diag)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    invoked = Path(sys.argv[0]).name
    if invoked.startswith("odile-diag") and (not argv or argv[0] not in COMMANDS):
        argv = ["diag", *argv]
    args = build_parser().parse_args(argv)
    paths = Paths(Path(getattr(args, "root", "/")))
    try:
        return args.func(args, paths)
    except SupportError as exc:
        print(f"odile-support: {exc}", file=sys.stderr)
        return exc.exit_code
    except BrokenPipeError:
        return EXIT_OK
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
