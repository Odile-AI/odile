#!/usr/bin/python3
"""odile-audit — the handoff audit: prove the access claim on this machine.

ODL-208. The user (or a sceptic) runs one command as root and reads a compact
PASS/FAIL report. Every line is a fact read from this machine, not a promise
from the installer:

  * the command line carries exactly one ``bb.key=`` — the key the initramfs
    accepts — and every ``authorized_keys``/``authorized_keys2`` line is that
    same key (sshd's default file list is both);
  * no private half of the boot key is anywhere under ``/root``, ``/home``,
    ``/etc/odile`` or ``/var/lib/odile``;
  * root has no password hash and sshd refuses password authentication;
  * there is no ``odile`` deploy key, no forced-command grant line, no second
    account that can log in and no second uid 0;
  * the root filesystem sits in one LUKS2 container with one keyslot and no
    token, so there is one volume key and no second one to steal;
  * no SSH agent socket is exported to a process, and no ``odile-support``
    grant is active;
  * every successful publickey login in sshd's log used the boot key's
    fingerprint — the list is printed, one line per login.

The passphrase is never read and never printed: the audit opens nothing, it
reads ``cryptsetup luksDump``, which describes the header only. Nothing is
written except the optional ``--save`` report, which is written atomically.

    sudo odile-audit                      # the machine's own report
    sudo odile-audit --save /var/lib/odile/handoff-audit.txt
    sudo odile-audit --json               # the same facts, machine-readable
    sudo odile-audit --root /mnt/guest    # a mounted machine, from a bench
    odile-audit --self-test               # the fixtures, no machine needed

Exit codes: 0 every check passed, 1 at least one FAIL, 2 bad input or a
missing prerequisite (a fixture file that does not exist, ``--json`` with
``--save``, and so on). A FAIL is a report, not an error: the rows and their
detail are printed and ``--save`` still writes the file.

This is the machine's own testimony. A root shell can change every row; the
boot measurement is what says which software was supposed to be running, and
``/verify`` tags the rows accordingly. The Odile sign-in is not an SSH key and
is not part of this audit: this audit covers the machine.
"""

from __future__ import annotations

import argparse
import base64
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0.0"
PREFIX = "odile-audit"

# The checks, in report order. Names are stable: /verify and the runbook quote
# them, and tests assert on them.
CHECK_NAMES = (
    "boot key",
    "authorized keys",
    "root password",
    "password auth",
    "operator key",
    "operator account",
    "keyslots",
    "agent socket",
    "support grant",
    "login history",
)

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
HASH_RE = re.compile(r"^\$[0-9a-z]+\$")
OPERATOR_WORDS = ("operator", "support", "deploy", "hq", "-odile", "odile-")
PUBKEY_ALGOS = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
                "ecdsa-sha2-nistp521", "sk-ssh-ed25519@openssh.com",
                "sk-ecdsa-sha2-nistp256@openssh.com")


class Result:
    """One row of the report: a name, a verdict, the detail and the command."""

    __slots__ = ("name", "ok", "detail", "command", "lines")

    def __init__(self, name: str, ok: bool, detail: str, command: str = "", lines=()) -> None:
        self.name = name
        self.ok = ok
        self.detail = detail
        self.command = command
        self.lines = list(lines)

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "command": self.command, "lines": self.lines}


class Machine:
    """Where the audit reads: a mounted machine root, and the fixtures a bench
    or a test may pass instead of the live paths."""

    def __init__(self, *, root: str = "/", cmdline: str | None = None,
                 sshd_effective: str | None = None, luks_dump: str | None = None,
                 luks_device: str | None = None, auth_log: str | None = None,
                 grant: str | None = None, user: str | None = None,
                 now: datetime | None = None) -> None:
        self.root = Path(root).resolve()
        self._cmdline = Path(cmdline) if cmdline else None
        self._sshd_effective = Path(sshd_effective) if sshd_effective else None
        self._luks_dump = Path(luks_dump) if luks_dump else None
        self.luks_device = luks_device
        self._auth_log = Path(auth_log) if auth_log else None
        self._grant = Path(grant) if grant else None
        self.user = user
        self.now = now or datetime.now(timezone.utc)

    # -- paths -------------------------------------------------------------
    def path(self, absolute: str) -> Path:
        """One absolute path inside this machine."""
        if str(self.root) == "/":
            return Path(absolute)
        return self.root / absolute.lstrip("/")

    def exists(self, absolute: str) -> bool:
        return self.path(absolute).exists()

    def read(self, absolute: str, default: str = "") -> str:
        try:
            return self.path(absolute).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return default

    @property
    def live(self) -> bool:
        return str(self.root) == "/"

    # -- the machine's own records ----------------------------------------
    def cmdline(self) -> str:
        if self._cmdline is not None:
            return self._cmdline.read_text(encoding="utf-8", errors="replace")
        return self.read("/proc/cmdline")

    def accounts(self) -> list[tuple[str, int, str]]:
        """(name, uid, home) for every account with a home directory."""
        rows = []
        for line in self.read("/etc/passwd").splitlines():
            fields = line.split(":")
            if len(fields) < 7:
                continue
            try:
                uid = int(fields[2])
            except ValueError:
                continue
            rows.append((fields[0], uid, fields[5]))
        return rows

    def agent_user(self) -> str:
        """The unprivileged account the agent runs as: ``--user``, else the
        account whose home holds the Hermes tree, else none."""
        if self.user:
            return self.user
        for name, uid, home in self.accounts():
            if uid >= 1000 and (self.path(home) / ".hermes").is_dir():
                return name
        return ""

    def authorized_files(self) -> list[Path]:
        """Every file sshd's default ``AuthorizedKeysFile`` accepts.

        OpenSSH's built-in default is ``.ssh/authorized_keys`` **and**
        ``.ssh/authorized_keys2``; reading only the first lets a second key
        open the machine while this audit says PASS. The strict bar
        (``scripts/verify_and_unlock.sh``) reads both, and so does this.
        """
        files = []
        for _name, _uid, home in self.accounts():
            if not home or home == "/":
                continue
            for name in ("authorized_keys", "authorized_keys2"):
                candidate = Path(home) / ".ssh" / name
                path = candidate if self.live else self.root / str(candidate).lstrip("/")
                if path.is_file():
                    files.append(path)
        return sorted(set(files))

    def sshd_config(self) -> str:
        """The effective sshd configuration: ``sshd -T`` on the live machine,
        else the files under the machine root, include order honoured."""
        if self._sshd_effective is not None:
            return self._sshd_effective.read_text(encoding="utf-8", errors="replace")
        if self.live and shutil.which("sshd"):
            done = run(["sshd", "-T"])
            if done.returncode == 0 and done.stdout.strip():
                return done.stdout
        return self._parsed_sshd_config()

    def _parsed_sshd_config(self) -> str:
        """OpenSSH's first-obtained-value rule: an Include at the top of the
        main file wins, so drop-ins are read where the Include sits."""
        main = self.path("/etc/ssh/sshd_config")
        chunks = []
        try:
            text = main.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("include "):
                pattern = stripped.split(None, 1)[1].strip()
                for candidate in sorted(glob.glob(str(self.root / pattern.lstrip("/")))):
                    try:
                        chunks.append(Path(candidate).read_text(encoding="utf-8", errors="replace"))
                    except OSError:
                        continue
                continue
            chunks.append(line)
        return "\n".join(chunks)

    def luks_dump(self) -> tuple[str, str]:
        """(the dump, the command that produced it) for the root filesystem."""
        if self._luks_dump is not None:
            return self._luks_dump.read_text(encoding="utf-8", errors="replace"), \
                "cat %s" % self._luks_dump
        device = self.luks_device or self.root_device()
        if not device:
            return "", "findmnt -no SOURCE /"
        if self.live and shutil.which("cryptsetup"):
            status = run(["cryptsetup", "status", device])
            if status.returncode == 0:
                for line in status.stdout.splitlines():
                    if line.strip().startswith("device:"):
                        device = line.split(":", 1)[1].strip()
                        break
        done = run(["cryptsetup", "luksDump", device])
        return done.stdout + done.stderr, "cryptsetup luksDump %s" % device

    def root_device(self) -> str:
        if self.live and shutil.which("findmnt"):
            done = run(["findmnt", "-no", "SOURCE", "/"])
            if done.returncode == 0:
                source = done.stdout.strip()
                if source and source.startswith("/dev/"):
                    return source
        for line in self.read("/proc/mounts").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1] == "/" and fields[0].startswith("/dev/"):
                return fields[0]
        return ""

    def auth_log(self) -> str:
        """sshd's log: the journal on a live machine, the files on a fixture."""
        if self._auth_log is not None:
            return self._auth_log.read_text(encoding="utf-8", errors="replace")
        if self.live and shutil.which("journalctl"):
            done = run(["journalctl", "-u", "ssh", "--no-pager", "-o", "cat"])
            if done.returncode == 0 and done.stdout.strip():
                return done.stdout
        chunks = []
        for pattern in ("/var/log/auth.log", "/var/log/auth.log.1",
                        "/var/log/secure", "/var/log/secure.1"):
            path = self.path(pattern)
            if path.is_file():
                try:
                    chunks.append(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
        return "\n".join(chunks)

    def grant(self) -> str:
        if self._grant is not None:
            return self._grant.read_text(encoding="utf-8", errors="replace")
        return self.read("/etc/odile/support-grant.json")


# ---------------------------------------------------------------------------
# small helpers

def run(command: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def fingerprint(blob_b64: str) -> str:
    """The OpenSSH SHA256 fingerprint of one base64 public-key blob."""
    return "SHA256:" + base64.b64encode(
        hashlib.sha256(base64.b64decode(blob_b64)).digest()).decode().rstrip("=")


def parse_public_key(line: str):
    """(algo, blob, comment) for one authorized_keys line, or None.

    Options (a leading ``command="..."``) are skipped; the key is the first
    field whose second field decodes as a base64 blob and whose first field is
    a known algorithm.
    """
    fields = line.split()
    for index in range(len(fields) - 1):
        if fields[index] not in PUBKEY_ALGOS:
            continue
        try:
            base64.b64decode(fields[index + 1], validate=True)
        except (ValueError, TypeError):
            continue
        return fields[index], fields[index + 1], " ".join(fields[index + 2:])
    return None


def has_options(line: str) -> bool:
    """A forced command or any option list before the key type."""
    return bool(line.split()) and line.split()[0] not in PUBKEY_ALGOS


def iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# the checks

def check_boot_key(machine: Machine) -> Result:
    command = "cat /proc/cmdline"
    text = machine.cmdline()
    values = re.findall(r"(?:^|\s)bb\.key=(\S+)", text)
    if not values:
        return Result("boot key", False,
                      "no bb.key= in the measured command line", command)
    if len(values) > 1:
        return Result("boot key", False,
                      "%d bb.key= tokens; the command line must carry exactly one" % len(values),
                      command)
    try:
        line = base64.b64decode(values[0], validate=True).decode("utf-8", "replace").strip()
    except (ValueError, UnicodeDecodeError) as exc:
        return Result("boot key", False, "bb.key does not decode: %s" % exc, command)
    parsed = parse_public_key(line)
    if parsed is None:
        return Result("boot key", False, "bb.key is not one public key line", command)
    algo, blob, _comment = parsed
    key_fp = fingerprint(blob)
    private = _private_half(machine, algo, blob)
    if private is not None:
        return Result("boot key", False,
                      "%s; its private half is on the machine: %s" % (key_fp, private), command)
    return Result("boot key", True,
                  "%s %s; one bb.key in the command line, public key only, "
                  "no private half of it on the machine" % (algo, key_fp), command)


# The roots the strict bar scans: the machine's persistent, writable state.
# A private key that matches an authorized key is a usable login wherever it
# sits (`/root/boot-key` included), not only beside a `.pub` under `*/.ssh`.
PRIVATE_ROOTS = ("/root", "/home", "/etc/odile", "/var/lib/odile")


def _private_candidates(directory: Path):
    """Every file under ``directory`` that could hold a private key.

    A private key is a few hundred bytes to a few kilobytes: the size window
    skips the tree's libraries and logs without opening them, and only a file
    that fits has its first bytes read. Mirrors the strict bar's scan in
    ``scripts/verify_and_unlock.sh``.
    """
    stack = [directory]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                if not entry.is_file():
                    continue
                if entry.name.endswith(".pub") or entry.name.startswith(
                        ("authorized_keys", "known_hosts")):
                    continue
                if not 200 <= entry.stat().st_size <= 65536:
                    continue
            except OSError:
                continue
            yield Path(entry.path)


def _private_public(path: Path) -> tuple[str, str] | None:
    """(algo, blob) the private key names: ``ssh-keygen -y``, else its .pub sibling.

    The image may carry no ``ssh-keygen`` (no sshd, no openssh-client); the
    ``.pub`` sibling is then the only way to name the key. A file with neither
    is not identified, so it cannot be shown to be the boot key's half.
    """
    done = run(["ssh-keygen", "-y", "-P", "", "-f", str(path)])
    if done.returncode == 0:
        parsed = parse_public_key(done.stdout.strip())
        if parsed:
            return parsed[0], parsed[1]
    try:
        text = Path(str(path) + ".pub").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parsed = parse_public_key(text.strip())
    return (parsed[0], parsed[1]) if parsed else None


def _private_half(machine: Machine, algo: str, blob: str) -> str | None:
    """A private key under a persistent root whose public half is this key."""
    seen = set()
    for root in PRIVATE_ROOTS:
        for path in _private_candidates(machine.path(root)):
            if path in seen:
                continue
            seen.add(path)
            try:
                with path.open("rb") as handle:
                    head = handle.read(48)
            except OSError:
                continue
            if b"PRIVATE KEY" not in head:
                continue
            if _private_public(path) == (algo, blob):
                return _display(machine, path)
    return None


def check_authorized_keys(machine: Machine) -> Result:
    command = ("cat /root/.ssh/authorized_keys /root/.ssh/authorized_keys2 "
               "/home/*/.ssh/authorized_keys /home/*/.ssh/authorized_keys2")
    text = machine.cmdline()
    values = re.findall(r"(?:^|\s)bb\.key=(\S+)", text)
    boot = None
    if len(values) == 1:
        try:
            boot = base64.b64decode(values[0], validate=True).decode("utf-8", "replace").strip()
        except (ValueError, UnicodeDecodeError):
            boot = None
    files = machine.authorized_files()
    if not files:
        return Result("authorized keys", False,
                      "no authorized_keys on the machine; the boot key opens nothing", command)
    entries = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            if line.strip() and not line.strip().startswith("#"):
                entries.append((path, number, line.strip()))
    if not entries:
        return Result("authorized keys", False,
                      "authorized_keys carries no public key", command)
    foreign = []
    for path, number, line in entries:
        if boot is None or line != boot:
            foreign.append("%s:%d %s" % (_display(machine, path), number, _short(line)))
    if foreign:
        return Result("authorized keys", False,
                      "%d line(s) that are not the measured boot key: %s"
                      % (len(foreign), "; ".join(foreign)), command)
    return Result("authorized keys", True,
                  "%d line(s) over %d file(s), every one the measured boot key; "
                  "0 usable user keys" % (len(entries), len(files)), command)


def _display(machine: Machine, path: Path) -> str:
    text = str(path)
    prefix = str(machine.root)
    if prefix != "/" and text.startswith(prefix):
        return text[len(prefix):]
    return text


def _short(line: str) -> str:
    parsed = parse_public_key(line)
    if parsed is None:
        return line[:40]
    return "%s %s" % (parsed[0], fingerprint(parsed[1]))


def check_root_password(machine: Machine) -> Result:
    command = "passwd -S root"
    text = machine.read("/etc/shadow")
    if not text:
        return Result("root password", False, "no /etc/shadow to read", command)
    for line in text.splitlines():
        fields = line.split(":")
        if fields and fields[0] == "root" and len(fields) > 1:
            value = fields[1]
            if value == "" or set(value) <= {"!", "*"} or value.startswith("!"):
                return Result("root password", True,
                              "locked (the shadow field is %r, no password hash)" % value,
                              command)
            if HASH_RE.match(value):
                return Result("root password", False,
                              "root has a password hash; the handoff requires none", command)
            return Result("root password", True,
                          "no password hash (the shadow field is %r)" % value, command)
    return Result("root password", False, "no root entry in /etc/shadow", command)


def check_password_auth(machine: Machine) -> Result:
    command = "sshd -T | grep -i '^passwordauthentication'"
    text = machine.sshd_config()
    if not text.strip():
        return Result("password auth", False, "no sshd configuration to read", command)
    value = None
    for line in text.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and fields[0].lower() == "passwordauthentication":
            value = fields[1].strip().lower()
            break
    if value is None:
        return Result("password auth", False,
                      "sshd sets no PasswordAuthentication; the default is yes", command)
    if value != "no":
        return Result("password auth", False,
                      "sshd allows password authentication (PasswordAuthentication %s)" % value,
                      command)
    return Result("password auth", True,
                  "PasswordAuthentication no; sshd refuses a password", command)


def check_operator_key(machine: Machine) -> Result:
    command = ("grep -nE '^(command=|.*odile|.*operator|.*support)' "
               "/root/.ssh/authorized_keys /root/.ssh/authorized_keys2 "
               "/home/*/.ssh/authorized_keys /home/*/.ssh/authorized_keys2")
    text = machine.cmdline()
    values = re.findall(r"(?:^|\s)bb\.key=(\S+)", text)
    boot = None
    if len(values) == 1:
        try:
            boot = base64.b64decode(values[0], validate=True).decode("utf-8", "replace").strip()
        except (ValueError, UnicodeDecodeError):
            boot = None
    grants = []
    for path in machine.authorized_files():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parsed = parse_public_key(stripped)
            if has_options(stripped):
                grants.append("%s:%d has options (a forced command)" % (_display(machine, path), number))
                continue
            if parsed is None:
                grants.append("%s:%d is not a public key" % (_display(machine, path), number))
                continue
            comment = parsed[2].lower()
            if stripped != boot and any(word in comment for word in OPERATOR_WORDS):
                grants.append("%s:%d %s" % (_display(machine, path), number, _short(stripped)))
    if grants:
        return Result("operator key", False,
                      "an operator-shaped authorized key: %s" % "; ".join(grants), command)
    return Result("operator key", True,
                  "none (no options line, no key whose comment names an operator)", command)


def check_operator_account(machine: Machine) -> Result:
    command = "getent passwd"
    agent = machine.agent_user()
    rows = machine.accounts()
    shells = {"", "/bin/bash", "/bin/sh", "/bin/dash", "/bin/zsh", "/usr/bin/bash"}
    logins = []
    for line in machine.read("/etc/passwd").splitlines():
        fields = line.split(":")
        if len(fields) >= 7 and fields[6] in shells and fields[6]:
            logins.append((fields[0], int(fields[2]) if fields[2].isdigit() else -1))
    roots = [name for name, uid in logins if uid == 0]
    if roots != ["root"]:
        return Result("operator account", False,
                      "uid 0 is held by %s, not root alone" % ", ".join(roots or ["nobody"]),
                      command)
    operators = [name for name, _uid in logins
                 if name not in {"root", agent} and any(word.strip("-") in name.lower()
                                                         for word in OPERATOR_WORDS)]
    if operators:
        return Result("operator account", False,
                      "login account(s) that look like an operator: %s" % ", ".join(sorted(operators)),
                      command)
    extra = [name for name, uid in logins if uid not in (0,) and name != agent]
    detail = "root and %s only" % (agent or "no agent account")
    if extra:
        detail += " (plus %s)" % ", ".join(sorted(extra))
    return Result("operator account", True,
                  "%s; no operator or support account" % detail, command)


def check_keyslots(machine: Machine) -> Result:
    dump, command = machine.luks_dump()
    if not dump.strip():
        return Result("keyslots", False,
                      "cannot read the root container's LUKS header", command)
    keyslots = []
    tokens = []
    section = ""
    for line in dump.splitlines():
        stripped = line.strip()
        if stripped in ("Keyslots:", "Tokens:", "Digests:", "Data segments:"):
            section = stripped
            continue
        match = re.match(r"^(\d+):\s+(\S+)", stripped)
        if not match:
            continue
        if section == "Keyslots:":
            keyslots.append(match.group(1))
        elif section == "Tokens:":
            tokens.append(match.group(1))
    if not keyslots:
        return Result("keyslots", False,
                      "the root container has no keyslot; it is not a LUKS volume", command)
    if len(keyslots) != 1:
        return Result("keyslots", False,
                      "%d keyslots (%s); the handoff proves one" % (len(keyslots), ", ".join(keyslots)),
                      command)
    if tokens:
        return Result("keyslots", False,
                      "keyslot %s plus %d token(s) (%s); a token is a second way in"
                      % (keyslots[0], len(tokens), ", ".join(tokens)), command)
    return Result("keyslots", True,
                  "one keyslot (slot %s), no token: one volume key, no second way in"
                  % keyslots[0], command)


def _agent_holds(socket_path: Path) -> tuple[str, str]:
    """('keys' | 'empty' | 'stale', detail) for one agent socket.

    A systemd user session creates ``$XDG_RUNTIME_DIR/openssh_agent`` on
    login (Debian 13 enables ``ssh-agent.socket`` for every user), so a
    socket by itself is the login's artifact, not an operator path. What
    matters is whether it holds a key that can sign.
    """
    if not shutil.which("ssh-add"):
        return "stale", "ssh-add is not installed; its keys cannot be listed"
    environment = dict(os.environ)
    environment["SSH_AUTH_SOCK"] = str(socket_path)
    try:
        done = subprocess.run(["ssh-add", "-l"], capture_output=True, text=True,
                              timeout=15, env=environment)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "stale", str(exc)
    if done.returncode == 0:
        return "keys", " ".join(done.stdout.split())
    if done.returncode == 1:
        return "empty", "no key loaded"
    return "stale", (done.stderr.strip() or "cannot connect")


def _agent_result(sockets, exported, command: str) -> Result:
    """The live machine's verdict: a socket matters only if it holds a key."""
    held = []
    empty = []
    stale = []
    for candidate in sockets:
        state, detail = _agent_holds(Path(candidate))
        if state == "keys":
            held.append("%s holds %s" % (candidate, detail))
        elif state == "empty":
            empty.append(candidate)
        else:
            stale.append("%s (%s)" % (candidate, detail))
    if exported or held:
        detail = []
        if exported:
            detail.append("a process carries SSH_AUTH_SOCK: %s"
                          % "; ".join(sorted(set(exported))[:4]))
        if held:
            detail.append("an agent holds a key: %s" % "; ".join(sorted(held)[:4]))
        return Result("agent socket", False, "; ".join(detail), command)
    if empty or stale:
        parts = []
        if empty:
            parts.append("%d socket(s) with no key loaded (%s)"
                         % (len(empty), ", ".join(sorted(empty))))
        if stale:
            parts.append("%d stale socket(s): %s" % (len(stale), ", ".join(sorted(stale))))
        return Result("agent socket", True,
                      "%s; the login's own agent, not a credential, and no process "
                      "carries SSH_AUTH_SOCK" % "; ".join(parts), command)
    return Result("agent socket", True,
                  "none: no process carries SSH_AUTH_SOCK and no agent socket exists",
                  command)


def check_agent_socket(machine: Machine) -> Result:
    command = "grep -l SSH_AUTH_SOCK /proc/*/environ; ls /tmp/ssh-*/agent.* /run/user/*/openssh_agent"
    exported = []
    if machine.live:
        for path in glob.glob("/proc/[0-9]*/environ"):
            try:
                data = Path(path).read_bytes()
            except OSError:
                continue
            if b"SSH_AUTH_SOCK=" in data:
                try:
                    pid = path.split("/")[2]
                    command_line = Path("/proc/%s/comm" % pid).read_text().strip()
                except OSError:
                    command_line = "pid %s" % path.split("/")[2]
                if command_line == "ssh-agent":
                    # The agent's own environment points at its own socket; it
                    # is the agent, not a consumer that could sign.
                    continue
                exported.append("%s matches SSH_AUTH_SOCK" % command_line)
    sockets = []
    for pattern in ("/tmp/ssh-*/agent.*", "/run/user/*/openssh_agent",
                    "/run/user/*/keyring/ssh"):
        for candidate in glob.glob(str(machine.path(pattern))):
            sockets.append(candidate)
    if machine.live and sockets:
        return _agent_result(sockets, exported, command)
    if exported or sockets:
        detail = []
        if exported:
            detail.append("a process carries SSH_AUTH_SOCK: %s" % "; ".join(sorted(set(exported))[:4]))
        if sockets:
            detail.append("agent socket(s): %s" % ", ".join(sorted(sockets)))
        return Result("agent socket", False, "; ".join(detail), command)
    return Result("agent socket", True,
                  "none: no process carries SSH_AUTH_SOCK and no agent socket exists", command)


def check_support_grant(machine: Machine) -> Result:
    command = ("cat /etc/odile/support-grant.json; grep -n odile-support "
               "/root/.ssh/authorized_keys /root/.ssh/authorized_keys2 "
               "/home/*/.ssh/authorized_keys /home/*/.ssh/authorized_keys2")
    document = machine.grant().strip()
    if document:
        try:
            parsed = json.loads(document)
        except ValueError as exc:
            return Result("support grant", False,
                          "an unreadable support grant file: %s" % exc, command)
        expiry = None
        for key in ("expires_at", "expires", "until", "expiry"):
            if isinstance(parsed.get(key), str):
                expiry = parsed[key].strip()
                break
        if expiry is None or not ISO_RE.match(expiry):
            return Result("support grant", False,
                          "a support grant file with no ISO-8601 expiry: %s" % document[:120],
                          command)
        if iso(machine.now) < expiry:
            return Result("support grant", False,
                          "a support grant is active until %s" % expiry, command)
    for path in machine.authorized_files():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            if "odile-support" in line or "odile-support" in line.lower():
                return Result("support grant", False,
                              "%s:%d carries an odile-support key" % (_display(machine, path), number),
                              command)
    return Result("support grant", True,
                  "no grant: no /etc/odile/support-grant.json and no odile-support key", command)


ACCEPTED_RE = re.compile(
    r"Accepted (?P<method>publickey|password) for (?P<user>\S+) from (?P<from>\S+) "
    r"port (?P<port>\d+) ssh2"
    r"(?:: (?P<algo>\S+) (?P<fp>SHA256:\S+))?")


def check_logins(machine: Machine) -> Result:
    command = "journalctl -u ssh --no-pager -o cat | grep 'Accepted '"
    text = machine.auth_log()
    if not text.strip():
        return Result("login history", False,
                      "no sshd journal and no /var/log/auth.log: the login list cannot be read",
                      command)
    logins = []
    for match in ACCEPTED_RE.finditer(text):
        method = match.group("method")
        entry = "%s %s %s" % (match.group("user"), match.group("from"),
                              match.group("fp") or "(no fingerprint)")
        logins.append((method, entry))
    passwords = [entry for method, entry in logins if method == "password"]
    if passwords:
        return Result("login history", False,
                      "a password login succeeded: %s" % "; ".join(passwords), command,
                      ["login %s" % entry for _method, entry in logins])
    text = machine.cmdline()
    values = re.findall(r"(?:^|\s)bb\.key=(\S+)", text)
    allowed = set()
    if len(values) == 1:
        try:
            line = base64.b64decode(values[0], validate=True).decode("utf-8", "replace").strip()
            parsed = parse_public_key(line)
            if parsed:
                allowed.add(fingerprint(parsed[1]))
        except (ValueError, UnicodeDecodeError):
            pass
    foreign = []
    for _method, entry in logins:
        token = entry.rsplit(" ", 1)[-1]
        if token not in allowed:
            foreign.append(entry)
    lines = ["login %s" % entry for _method, entry in sorted(logins)]
    if foreign:
        return Result("login history", False,
                      "%d successful login(s) with a fingerprint that is not the boot key: %s"
                      % (len(foreign), "; ".join(foreign)), command, lines)
    if not logins:
        return Result("login history", True,
                      "0 successful publickey logins; the list is empty", command, [])
    return Result("login history", True,
                  "%d successful publickey login(s), every one the measured boot key"
                  % len(logins), command, lines)


CHECKS = (
    check_boot_key,
    check_authorized_keys,
    check_root_password,
    check_password_auth,
    check_operator_key,
    check_operator_account,
    check_keyslots,
    check_agent_socket,
    check_support_grant,
    check_logins,
)


def audit(machine: Machine) -> list[Result]:
    return [check(machine) for check in CHECKS]


# ---------------------------------------------------------------------------
# the report

def summary(results: list[Result]) -> str:
    failed = [result.name for result in results if not result.ok]
    if failed:
        return "FAIL — %d of %d checks: %s" % (len(failed), len(results), ", ".join(failed))
    return ("PASS — %d checks: the measured boot key and nothing else in "
            "authorized_keys, no private half, root locked, password auth off, "
            "no operator key or account, one LUKS keyslot, no agent socket holding "
            "a key, no support grant, and only the boot key in the login history."
            % len(results))


def render(results: list[Result], *, stamp: datetime, provenance: bool = True) -> str:
    lines = []
    if provenance:
        lines.append("read from your machine, %s" % iso(stamp))
    lines.append("%s — the handoff audit" % PREFIX)
    lines.append("")
    width = max(len(result.name) for result in results)
    for result in results:
        lines.append("  %-*s %s  %s" % (width, result.name,
                                         "PASS" if result.ok else "FAIL", result.detail))
        if result.command:
            lines.append("      %s" % result.command)
        for extra in result.lines:
            lines.append("    %s" % extra)
    lines.append("")
    lines.append("  %s" % summary(results))
    lines.append("  A root shell can change these rows; the boot measurement says "
                 "which software was supposed to be running.")
    return "\n".join(lines) + "\n"


def save(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%d" % (path.name, os.getpid()))
    temporary.write_text(body, encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


# ---------------------------------------------------------------------------
# fixtures: the same checks, on a machine built in a temp directory

FIXTURE_BOOT_KEY = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDqD6XQJAvXajXxtxXPw4Qcb4vhJWFvJaZIq"
                    "/Hn5QPiX odile-audit self-test boot key")
FIXTURE_OTHER_KEY = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGU5Pru+2th/4Bdp7sk96CHqbhvX7uJ4D2WbNz"
                     "GnL+Cj self-test planted key")
FIXTURE_CMDLINE = (
    "root=/dev/mapper/root_crypt ro console=tty0 console=ttyS0,115200 "
    "systemd.show_status=1 ip=dhcp bb.release=2026.11.2 bb.host=odile-audit-fixture "
    "bb.iid=45445445-0000-4000-8000-000000000208 bb.key=%s seal_id=0123456789abcdef"
    % base64.b64encode(FIXTURE_BOOT_KEY.encode()).decode())
FIXTURE_LUKS = """LUKS header information
Version:       \t2
UUID:          \t7c40dbbe-a3c2-4f05-bc9f-7e676876b51c
Label:         \todile-root

Data segments:
  0: crypt
\tcipher: aes-xts-plain64

Keyslots:
  0: luks2
\tKey:        512 bits
\tPBKDF:      argon2id
Tokens:
Digests:
  0: pbkdf2
\tHash:       sha256
"""
FIXTURE_AUTH = ("Accepted publickey for tenant from 203.0.113.9 port 51514 ssh2: "
                "ED25519 %s\n" % fingerprint(parse_public_key(FIXTURE_BOOT_KEY)[1]))


def build_fixture(root: Path, *, plants: tuple[str, ...] = ()) -> dict:
    """A machine as the audit sees it, in a temp directory.

    ``plants`` names the tampering a self-test or a test wants: ``second-key``
    (a usable key in the tenant's authorized_keys), ``second-key2`` (the same
    in sshd's second default file, authorized_keys2), ``password-auth``,
    ``second-keyslot``, ``support-grant``, ``foreign-login``.
    """
    commands = {
        "cmdline": root / "proc" / "cmdline",
        "auth": root / "var" / "log" / "auth.log",
        "sshd": root / "etc" / "ssh" / "sshd_config",
        "luks": root / "luks-dump.txt",
    }
    for path in commands.values():
        path.parent.mkdir(parents=True, exist_ok=True)

    commands["cmdline"].write_text(FIXTURE_CMDLINE + "\n", encoding="utf-8")
    (root / "etc" / "passwd").parent.mkdir(parents=True, exist_ok=True)
    (root / "etc" / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/bash\n"
        "tenant:x:1000:1000:tenant:/home/tenant:/bin/bash\n"
        "sync:x:4:65534:sync:/bin:/bin/sync\n", encoding="utf-8")
    (root / "etc" / "shadow").write_text(
        "root:*:19793:0:99999:7:::\ntenant:!:19793:0:99999:7:::\n", encoding="utf-8")
    (root / "etc" / "ssh").mkdir(parents=True, exist_ok=True)
    password = "yes" if "password-auth" in plants else "no"
    commands["sshd"].write_text(
        "Include /etc/ssh/sshd_config.d/*.conf\n"
        "PasswordAuthentication %s\nPermitRootLogin prohibit-password\n" % password,
        encoding="utf-8")

    keys = FIXTURE_BOOT_KEY + "\n"
    if "second-key" in plants:
        keys += FIXTURE_OTHER_KEY + "\n"
    for home in ("root", "home/tenant"):
        directory = root / home / ".ssh"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "authorized_keys").write_text(keys, encoding="utf-8")
        os.chmod(directory / "authorized_keys", 0o600)
        if "second-key2" in plants:
            second = directory / "authorized_keys2"
            second.write_text(FIXTURE_OTHER_KEY + "\n", encoding="utf-8")
            os.chmod(second, 0o600)

    dump = FIXTURE_LUKS
    if "second-keyslot" in plants:
        dump = dump.replace("Tokens:", "  1: luks2\n\tKey:        512 bits\nTokens:")
    commands["luks"].write_text(dump, encoding="utf-8")

    if "support-grant" in plants:
        (root / "etc" / "odile").mkdir(parents=True, exist_ok=True)
        (root / "etc" / "odile" / "support-grant.json").write_text(
            '{"reason": "self-test", "expires_at": "2099-01-01T00:00:00Z"}\n',
            encoding="utf-8")

    log = FIXTURE_AUTH
    if "foreign-login" in plants:
        log += ("Accepted publickey for tenant from 203.0.113.10 port 51515 ssh2: "
                "ED25519 %s\n" % fingerprint(parse_public_key(FIXTURE_OTHER_KEY)[1]))
    commands["auth"].write_text(log, encoding="utf-8")
    return commands


def machine_for(commands: dict, root: Path) -> Machine:
    return Machine(root=str(root), cmdline=str(commands["cmdline"]),
                   sshd_effective=str(commands["sshd"]), luks_dump=str(commands["luks"]),
                   auth_log=str(commands["auth"]), user="tenant")


def self_test() -> int:
    """Both directions on a machine in a temp directory: a clean PASS and a
    planted-key FAIL. No root, no network, no live path."""
    import tempfile

    ok = 0
    bad = 0

    def expect(condition: bool, text: str) -> None:
        nonlocal ok, bad
        if condition:
            ok += 1
            print("ok   %s" % text)
        else:
            bad += 1
            print("FAIL %s" % text)

    with tempfile.TemporaryDirectory(prefix="odile-audit-self-test-") as temporary:
        root = Path(temporary)
        clean = build_fixture(root)
        results = audit(machine_for(clean, root))
        expect(all(result.ok for result in results),
               "the clean fixture passes every check (%d/%d)"
               % (len(results), len(CHECK_NAMES)))
        expect([result.name for result in results] == list(CHECK_NAMES),
               "the report rows are the ten checks in order")
        expect("PASS — 10 checks" in summary(results), "the summary is a PASS")
        expect("read from your machine, " in render(results, stamp=datetime.now(timezone.utc)),
               "the report carries the provenance line")

        planted_root = root / "planted"
        planted_root.mkdir()
        planted = build_fixture(planted_root, plants=("second-key",))
        planted_results = audit(machine_for(planted, planted_root))
        failed = [result.name for result in planted_results if not result.ok]
        expect(failed == ["authorized keys"],
               "a second key fails 'authorized keys' and nothing else (got %r)" % failed)
        expect("FAIL — 1 of 10 checks: authorized keys" in summary(planted_results),
               "the summary names the planted-key FAIL")

        cases = (
            ("second-key2", "authorized keys"),
            ("password-auth", "password auth"),
            ("second-keyslot", "keyslots"),
            ("support-grant", "support grant"),
            ("foreign-login", "login history"),
        )
        for plant, row in cases:
            case_root = root / plant
            case_root.mkdir()
            case = build_fixture(case_root, plants=(plant,))
            case_results = audit(machine_for(case, case_root))
            failed = [result.name for result in case_results if not result.ok]
            expect(failed == [row], "the %s plant fails '%s' only (got %r)" % (plant, row, failed))
    print("audit self-test: %s (%d ok, %d failed)" % ("PASS" if bad == 0 else "FAIL", ok, bad))
    return 0 if bad == 0 else 1


# ---------------------------------------------------------------------------
# the command line

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="odile-audit",
        description="The handoff audit: prove the machine's access claim.")
    parser.add_argument("--root", default="/", help="the machine root (default /)")
    parser.add_argument("--cmdline", help="read this file instead of <root>/proc/cmdline")
    parser.add_argument("--sshd-effective", help="read this sshd -T-shaped file instead")
    parser.add_argument("--luks-dump", help="read this luksDump output instead")
    parser.add_argument("--luks-device", help="the LUKS device that holds <root>")
    parser.add_argument("--auth-log", help="read this sshd log instead of the journal")
    parser.add_argument("--grant", help="read this support-grant document instead")
    parser.add_argument("--user", help="the unprivileged agent account (default: detected)")
    parser.add_argument("--save", help="write the report here (0644, atomic)")
    parser.add_argument("--json", action="store_true", help="print the same facts as JSON")
    parser.add_argument("--self-test", action="store_true", help="run the fixtures and exit")
    parser.add_argument("--version", action="version", version="%s %s" % (PREFIX, VERSION))
    args = parser.parse_args(argv)
    if args.json and args.save:
        parser.error("--json and --save cannot be combined")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return self_test()
    fixtures = (("--cmdline", args.cmdline), ("--sshd-effective", args.sshd_effective),
                ("--luks-dump", args.luks_dump), ("--auth-log", args.auth_log),
                ("--grant", args.grant))
    for flag, value in fixtures:
        if value and not Path(value).is_file():
            print("%s: %s does not exist: %s" % (PREFIX, flag, value), file=sys.stderr)
            return 2
    if not Path(args.root).is_dir():
        print("%s: --root is not a directory: %s" % (PREFIX, args.root), file=sys.stderr)
        return 2
    machine = Machine(root=args.root, cmdline=args.cmdline,
                      sshd_effective=args.sshd_effective, luks_dump=args.luks_dump,
                      luks_device=args.luks_device, auth_log=args.auth_log,
                      grant=args.grant, user=args.user)
    results = audit(machine)
    stamp = machine.now
    if args.json:
        document = {
            "schema": 1,
            "tool": "%s %s" % (PREFIX, VERSION),
            "stamp": iso(stamp),
            "ok": all(result.ok for result in results),
            "summary": summary(results),
            "checks": [result.as_dict() for result in results],
        }
        print(json.dumps(document, indent=2, sort_keys=True))
    else:
        body = render(results, stamp=stamp)
        print(body, end="")
        if args.save:
            save(Path(args.save), body)
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
