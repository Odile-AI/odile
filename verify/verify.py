#!/usr/bin/python3
"""The machine's own /verify page: every claim, with where it was checked.

Serve this on the guest and a sceptical stranger can read the machine's own
records for ninety seconds: the live SEV-SNP boot report, the full launch
measurement, the release and vCPU count, the boot key in the measured command
line (fingerprint only, no private half), every line in authorized_keys, the
SSH host key, root lock, password authentication, the panel's root reset (there
is no guest agent), the accounts, the LUKS keyslots, the boot/unlock state, the
support-grant state, the signed manifest, and the three deeper checks a laptop
can run. Every row carries one tag: `checked from your laptop` or `read from
the machine`. A root shell can change the machine rows; the CPU's signature is
what it cannot fake.

Where the numbers come from, all read at request time and none invented:

  /var/lib/odile/attestation.json   the boot report (ODL-397) — measurement,
                                    report bytes, release, command line, vCPUs
  /etc/odile/boot-record.json       what the user verified at install (ODL-396)
  /proc/cmdline                     the measured command line's bb.key=
  /etc/ssh/ssh_host_ed25519_key.pub the host key the user compares
  /etc/passwd, /etc/shadow          the accounts and root's lock
  /run/bb/sealed-unlock.state       the initramfs seal's boot outcome
  <home>/.hermes/.env               the scrypt sign-in hash, never the password
  /etc/odile/manifest.json(.sig)    the signed app-layer manifest
  odile-audit --json                the handoff audit (ODL-208), for the
                                    access rows it already checks
  odile-support status --json       the grant state (ODL-217), read from the
                                    program that writes it
  cryptsetup luksDump               the root container's header, header only

The audit and the support program are the machine's own reviewed programs, so
the page never re-implements a verdict: it wraps theirs. A row whose program is
not on this machine says so instead of guessing.

    /usr/bin/python3 verify/verify.py                      # the page on stdout
    /usr/bin/python3 verify/verify.py --json               # the same rows
    /usr/bin/python3 verify/verify.py --out /var/lib/odile/verify.html
    /usr/bin/python3 verify/verify.py --serve --port 8098  # Caddy's upstream

On a bench, point it at a mounted machine root and the dump the audit uses:

    /usr/bin/python3 verify/verify.py --root /mnt/guest \
        --luks-dump /tmp/luks.txt --now 2026-11-13T12:00:00Z

`--now` pins the provenance line. Nothing is written except `--out`, which is
atomic and 0644. `--serve` answers `/`, `/verify` and `/verify/` with the
page (Caddy proxies the last two), `/health` with `ok` and everything else
with 404, `Cache-Control: no-store`; it holds no state and runs no timer. On a
guest the `odile-verify.service` unit runs it as root because
the handoff audit reads `/etc/shadow` and sshd's journal, and it binds loopback
on 127.0.0.1:8098: Caddy is the public listener at `/verify`.
"""

from __future__ import annotations

import argparse
import base64
import glob
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Beside the program on a tenant (/usr/local/lib/odile/verify.html, written by
# the installer's embedded copy), the repository template on a checkout.
_TEMPLATE_BESIDE = Path(__file__).resolve().parent / "verify.html"
TEMPLATE_PATH = _TEMPLATE_BESIDE if _TEMPLATE_BESIDE.is_file() else ROOT / "templates" / "verify.html"
DEFAULT_PORT = 8098

ATTESTATION = "/var/lib/odile/attestation.json"
BOOT_RECORD = "/etc/odile/boot-record.json"
CMDLINE = "/proc/cmdline"
HOST_KEYS = ("/etc/ssh/ssh_host_ed25519_key.pub",
             "/etc/ssh/ssh_host_ecdsa_key.pub",
             "/etc/ssh/ssh_host_rsa_key.pub")
SEAL_STATE = "/run/bb/sealed-unlock.state"
MANIFEST = "/etc/odile/manifest.json"
MANIFEST_SIG = "/etc/odile/manifest.json.sig"
MANIFEST_KEY = "/etc/odile/odile-signing-key.pub"
AUDIT_PROGRAM = "/usr/local/bin/odile-audit"
SUPPORT_PROGRAM = "/usr/local/sbin/odile-support"
GRANT_STATE = "/var/lib/odile/support-grant.json"
STATUS_URL = "https://odile.fyi/status"
# The deck's own command for the header, printed on every LUKS row. The LUKS
# container is not the mapper: `cryptsetup luksDump /dev/mapper/root_crypt` is
# refused because the mapper is plaintext (docs/runbooks/support.md, measured on
# the tenant). `cryptsetup status` names the device the mapper was opened from,
# which is what luksDump reads.
LUKS_DEVICE = "cryptsetup status root_crypt | sed -n 's/^ *device: *//p'"
LUKS_COMMAND = ('cryptsetup luksDump "$(%s)" | '
                "sed -n '/Keyslots:/,/Tokens:/p'" % LUKS_DEVICE)

HEX96_RE = re.compile(r"^[0-9a-f]{96}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# A machine value the eye compares character by character: a fingerprint, a
# hash (full or truncated), a timestamp, a path, a config pair. Rendered in the
# mono role wherever it appears in a row's value.
VALUE_RE = re.compile(
    r"SHA256:[A-Za-z0-9+/=]{16,}"                     # an OpenSSH key fingerprint
    r"|\b[0-9a-f]{96}\b"                              # the launch measurement
    r"|\b[0-9a-f]{64}\b"                              # a SHA-256
    r"|\b[0-9a-f]{8,}\u2026"                          # a truncated hash
    r"|\b[0-9a-f]{12}\b"                              # a grant id
    r"|\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\b"      # a UTC timestamp
    r"|\b(?:PasswordAuthentication|PermitRootLogin) \S+\b"  # sshd's answer
    r"|(?:/[A-Za-z0-9._*<>-]+)+")                     # a path on the machine
PUBKEY_ALGOS = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256",
                "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
                "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com",
                "the-platform-does-not-know-this-algorithm")
GUEST_AGENT_PATHS = ("/usr/local/bin/bb-agent", "/usr/sbin/bb-agent",
                     "/usr/bin/blackbox-agent", "/usr/local/bin/servury-agent",
                     "/usr/sbin/servury-agent")
GUEST_AGENT_NAMES = ("servury", "blackbox", "bb-agent", "servury-agent")

LAPTOP_TAG = "checked from your laptop"
MACHINE_TAG = "read from the machine"

# The seal's signed policy (SNP guest policy) and the two .env keys the
# sign-in row reads. The sign-in row never prints the hash, only that it is a
# scrypt hash and that no plaintext password sits beside it.
SEAL_POLICY = 0x30000
SIGNIN_HASH_KEY = "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH"
SIGNIN_PASSWORD_KEY = "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"
SCRYPT_RE = re.compile(
    r"^scrypt\$\d+\$\d+\$\d+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+$")


class PageError(Exception):
    """Bad input: a missing template, an unreadable root."""


# ---------------------------------------------------------------------------
# reading the machine


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(text: str | None) -> datetime | None:
    if not isinstance(text, str) or not ISO_RE.match(text.strip()):
        return None
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default


def read_json(path: Path) -> dict | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(blob_b64: str) -> str:
    """The OpenSSH SHA256 fingerprint of one base64 public-key blob."""
    return "SHA256:" + base64.b64encode(
        hashlib.sha256(base64.b64decode(blob_b64)).digest()).decode().rstrip("=")


def parse_public_key(line: str) -> tuple[str, str, str] | None:
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


def cmdline_token(cmdline: str, name: str) -> str | None:
    match = re.search(r"(?:^|\s)%s=(\S+)" % re.escape(name), cmdline)
    return match.group(1) if match else None


def run(argv: list[str], *, timeout: int = 60, env: dict | None = None):
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


@dataclass
class Row:
    name: str
    state: str            # PASS, FAIL, INFO or N/A
    value: str
    tag: str              # "laptop" or "machine"
    command: str = ""
    # Values the reader compares character by character, set in the mono role.
    # Fingerprints, hashes, timestamps and paths are found automatically; this
    # tuple adds the ones no pattern can know (a release id, a tool name).
    mono: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"name": self.name, "state": self.state, "value": self.value,
                "tag": self.tag, "command": self.command}


@dataclass
class Source:
    """One program the page reads, and the JSON it answered with."""

    document: dict | None = None
    error: str = ""


@dataclass
class Machine:
    root: Path
    now: datetime
    luks_dump: Path | None = None
    audit_program: Path | None = None
    support_program: Path | None = None
    now_pinned: bool = False
    rows: list[Row] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)

    def path(self, absolute: str) -> Path:
        if str(self.root) == "/":
            return Path(absolute)
        return self.root / absolute.lstrip("/")

    def read(self, absolute: str) -> str:
        return read_text(self.path(absolute))

    # -- programs -----------------------------------------------------------

    def program(self, override: Path | None, default: str) -> Path:
        if override is not None:
            return override
        if str(self.root) == "/":
            return Path(default)
        return self.path(default)

    def run_program(self, path: Path, arguments: list[str], env: dict | None = None) -> Source:
        if not path.is_file():
            return Source(None, "not installed on this machine yet (%s)" % path)
        argv = [sys.executable, str(path)]
        if str(self.root) != "/":
            argv += ["--root", str(self.root)]
        argv += arguments
        done = run(argv, env=env)
        if done.returncode not in (0, 1):
            detail = (done.stderr or done.stdout).strip().splitlines()
            return Source(None, "no answer (%s)" % (detail[-1] if detail else
                                                    "exit %d" % done.returncode))
        try:
            document = json.loads(done.stdout)
        except ValueError:
            return Source(None, "its answer was not JSON")
        return Source(document if isinstance(document, dict) else None,
                      "" if isinstance(document, dict) else "its answer was not a JSON object")

    def audit(self) -> Source:
        arguments = ["--json"]
        if self.luks_dump is not None:
            arguments = ["--luks-dump", str(self.luks_dump)] + arguments
        return self.run_program(self.program(self.audit_program, AUDIT_PROGRAM),
                                arguments)

    def support(self) -> Source:
        env = dict(os.environ)
        env["ODILE_SUPPORT_NOW"] = iso(self.now)
        return self.run_program(self.program(self.support_program, SUPPORT_PROGRAM),
                                ["status", "--json", "--no-expire"], env)

    # -- the machine's records ---------------------------------------------

    def attestation(self) -> tuple[dict | None, bytes | None, str]:
        document = read_json(self.path(ATTESTATION))
        if document is None:
            return None, None, "no boot report at %s" % ATTESTATION
        if document.get("available") is not True:
            return None, None, str(document.get("error") or "the boot report is unavailable")
        measurement = document.get("measurement")
        if not isinstance(measurement, str) or not HEX96_RE.match(measurement):
            return None, None, "the boot report has no 96-hex measurement"
        try:
            report = base64.b64decode(document.get("report_b64") or "", validate=True)
        except ValueError:
            return None, None, "the boot report's bytes are not base64"
        if not report:
            return None, None, "the boot report has no bytes"
        return document, report, ""

    def boot_record(self) -> dict | None:
        document = read_json(self.path(BOOT_RECORD))
        if document is None or document.get("schema") != "odile.boot-record.v1":
            return None
        return document

    def cmdline(self) -> str:
        return self.read(CMDLINE).strip()

    def boot_key(self) -> tuple[str, str, int]:
        """(public key line, fingerprint, count of bb.key= tokens)."""
        values = re.findall(r"(?:^|\s)bb\.key=(\S+)", self.cmdline())
        if len(values) != 1:
            return "", "", len(values)
        try:
            line = base64.b64decode(values[0], validate=True).decode(
                "utf-8", "replace").strip()
        except (ValueError, UnicodeDecodeError):
            return "", "", 1
        parsed = parse_public_key(line)
        if parsed is None:
            return "", "", 1
        return line, fingerprint(parsed[1]), 1

    def host_key(self) -> tuple[str, str]:
        for absolute in HOST_KEYS:
            text = self.read(absolute).strip()
            parsed = parse_public_key(text)
            if parsed is not None:
                return fingerprint(parsed[1]), absolute
        return "", ""

    def accounts(self) -> list[tuple[str, int, str, str]]:
        rows = []
        for line in self.read("/etc/passwd").splitlines():
            fields = line.split(":")
            if len(fields) < 7:
                continue
            try:
                uid = int(fields[2])
            except ValueError:
                continue
            rows.append((fields[0], uid, fields[5], fields[6]))
        return rows

    def agent_user(self) -> str:
        for name, uid, home, shell in self.accounts():
            if uid >= 1000 and shell not in ("", "/usr/sbin/nologin", "/bin/false"):
                if (self.path(home) / ".hermes").is_dir():
                    return name
        for name, uid, home, shell in self.accounts():
            if uid >= 1000 and shell not in ("", "/usr/sbin/nologin", "/bin/false"):
                return name
        return ""

    def guest_agent(self) -> str:
        """A Servury/Blackbox guest agent, if this machine carries one."""
        for absolute in GUEST_AGENT_PATHS:
            if self.path(absolute).exists():
                return absolute
        for name, _uid, _home, _shell in self.accounts():
            if name in GUEST_AGENT_NAMES:
                return "the %s account" % name
        for unit in sorted(glob.glob(str(self.path("/etc/systemd/system") / "*"))):
            base = os.path.basename(unit).lower()
            if "servury" in base or "blackbox" in base:
                return os.path.basename(unit)
        return ""

    def seal_state(self) -> str:
        return self.read(SEAL_STATE).strip()

    def home_of(self, user: str) -> str:
        for name, _uid, home, _shell in self.accounts():
            if name == user:
                return home
        return ""

    def seal(self, document: dict | None, report: bytes | None) -> tuple[str, str, tuple[str, ...]]:
        """(state, detail, mono) for the signed seal policy and the seal_id.

        The policy is the report's own first 8-byte field; ``seal_id`` is in
        the measured command line and must still be the running one. This is
        the same check ``verify_and_unlock.sh --mode access`` prints, so the
        page and the report cannot disagree about the seal.
        """
        if not report or len(report) < 16:
            return "FAIL", "no report bytes to read a policy from", ()
        policy = int.from_bytes(report[8:16], "little")
        signed = report[0x90:0xC0].hex() if len(report) >= 0xC0 else ""
        measured = document.get("cmdline") if isinstance(document, dict) else ""
        if not isinstance(measured, str):
            measured = ""
        seal_id = cmdline_token(measured, "seal_id")
        running_seal = cmdline_token(self.cmdline(), "seal_id")
        problems = []
        if policy != SEAL_POLICY:
            problems.append("policy is 0x%x, not 0x%x" % (policy, SEAL_POLICY))
        if len(signed) != 96 or signed == "0" * 96:
            problems.append("the signed report carries no measurement")
        if seal_id is None:
            problems.append("the measured command line has no seal_id")
        elif running_seal != seal_id:
            problems.append("the running command line has no measured seal_id")
        detail = "policy 0x%x; seal_id %s; measurement %s" % (
            policy, seal_id or "missing", signed or "missing")
        if problems:
            detail += " (%s)" % "; ".join(problems)
        marks = tuple(mark for mark in ("0x%x" % policy, seal_id or "", signed)
                      if mark)
        return ("FAIL" if problems else "PASS"), detail, marks

    def signin_hash(self) -> tuple[str, str, str]:
        """(state, detail, command) for the scrypt sign-in hash, never its value."""
        user = self.agent_user()
        home = self.home_of(user) if user else ""
        if not home:
            return "N/A", "no agent account with a home directory", ""
        absolute = home.rstrip("/") + "/.hermes/.env"
        command = "grep -c '^%s=' %s" % (SIGNIN_HASH_KEY, absolute)
        path = self.path(absolute)
        if not path.is_file():
            return "FAIL", "no %s" % absolute, command
        values = {}
        for line in read_text(path).splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, value = line.split("=", 1)
                values[name.strip()] = value.strip()
        encoded = values.get(SIGNIN_HASH_KEY, "")
        plaintext = SIGNIN_PASSWORD_KEY in values
        valid = bool(SCRYPT_RE.match(encoded))
        if valid:
            try:
                valid = int(encoded.split("$")[1]) >= 16384
            except (IndexError, ValueError):
                valid = False
        if not encoded:
            return "FAIL", "no scrypt hash in %s" % absolute, command
        if plaintext:
            return "FAIL", "plaintext password in %s" % absolute, command
        if not valid:
            return "FAIL", "the hash in %s is not scrypt" % absolute, command
        return ("PASS",
                "scrypt hash present in %s; no plaintext password" % absolute,
                command)

    def luks_device(self) -> str:
        """The block device the root mapper was opened from, or ''.

        `cryptsetup luksDump /dev/mapper/root_crypt` is refused: the mapper is
        the plaintext device, not the LUKS container (docs/runbooks/support.md,
        measured on the tenant). `cryptsetup status` names the container; a
        machine without it falls back to the mounted root's source, and a
        source that is itself a mapper is reported as-is rather than guessed.
        """
        status = run(["cryptsetup", "status", "root_crypt"])
        for line in (status.stdout + status.stderr).splitlines():
            match = re.match(r"^\s*device:\s+(\S+)", line)
            if match and Path(match.group(1)).exists():
                return match.group(1)
        mounted = run(["findmnt", "-no", "SOURCE", "/"])
        source = (mounted.stdout or "").strip().splitlines()
        if source and Path(source[0]).exists() and not source[0].startswith("/dev/mapper/"):
            return source[0]
        return ""

    def luks(self) -> tuple[list[str], list[str], str]:
        """(keyslots, tokens, command) from the header, never the volume."""
        if self.luks_dump is not None:
            dump = read_text(self.luks_dump)
            command = "cryptsetup luksDump (from %s)" % self.luks_dump
        elif str(self.root) == "/":
            device = self.luks_device()
            done = run(["cryptsetup", "luksDump", device]) if device else None
            dump = (done.stdout + done.stderr) if done is not None else ""
            command = "cryptsetup luksDump %s" % device if device else "lsblk -f"
        else:
            # A mounted guest root's /dev/mapper name is not a host device; the
            # bench passes the dump it took inside the guest with --luks-dump.
            dump, command = "", "cryptsetup luksDump (a mounted root needs --luks-dump)"
        keyslots, tokens = [], []
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
        return keyslots, tokens, command

    def manifest(self) -> tuple[str, str, str]:
        """(state, detail, key fingerprint) for the signed app-layer manifest."""
        path = self.path(MANIFEST)
        signature_path = self.path(MANIFEST_SIG)
        key_path = self.path(MANIFEST_KEY)
        if not path.is_file():
            return "N/A", "no signed manifest at %s" % MANIFEST, ""
        try:
            data = path.read_bytes()
            document = json.loads(data)
        except (OSError, ValueError):
            return "FAIL", "the manifest at %s is not JSON" % MANIFEST, ""
        if not isinstance(document, dict):
            return "FAIL", "the manifest at %s is not a JSON object" % MANIFEST, ""
        if not signature_path.is_file() or not key_path.is_file():
            return "N/A", ("the manifest is present but its signature or key is "
                           "missing; verify it against the key in your recovery kit"), ""
        try:
            raw = b"".join(signature_path.read_bytes().split())
            signature = base64.b64decode(raw, validate=True) if len(raw) != 64 else raw
        except ValueError:
            return "FAIL", "the manifest signature is not base64", ""
        if len(signature) != 64:
            return "FAIL", "the manifest signature is not 64 bytes", ""
        key_fingerprint = self.signing_key_fingerprint(key_path)
        handle = tempfile.NamedTemporaryFile(prefix="odile-manifest-", delete=False)
        try:
            handle.write(signature)
            handle.close()
            # `openssl dgst -verify` reads the data on stdin.
            done = subprocess.run(["openssl", "dgst", "-verify", str(key_path),
                                   "-signature", handle.name],
                                  input=data, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return "FAIL", "openssl could not check the manifest signature", key_fingerprint
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
        generated = document.get("generated_at")
        installer = document.get("installer_sha256")
        detail = []
        if isinstance(generated, str):
            detail.append("generated %s" % generated)
        if isinstance(installer, str):
            detail.append("installer %s…" % installer[:12])
        if done.returncode == 0:
            detail.append("signature verified with Odile's key %s" % key_fingerprint)
            return "PASS", " · ".join(detail), key_fingerprint
        detail.append("signature BAD: the bytes do not match the signature")
        return "FAIL", " · ".join(detail), key_fingerprint

    def signing_key_fingerprint(self, key_path: Path) -> str:
        try:
            done = subprocess.run(["openssl", "pkey", "-pubin", "-in", str(key_path),
                                   "-outform", "DER"], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if done.returncode != 0 or not done.stdout:
            return ""
        digest = hashlib.sha256(done.stdout).digest()
        return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


# ---------------------------------------------------------------------------
# the rows


def audit_row(source: Source, name: str) -> dict | None:
    if source.document is None:
        return None
    for check in source.document.get("checks") or []:
        if isinstance(check, dict) and check.get("name") == name:
            return check
    return None


def value_marks(text: str) -> tuple[str, ...]:
    """Every machine value in a row's text, in the order it appears."""
    return tuple(dict.fromkeys(match.group(0) for match in VALUE_RE.finditer(text)))


def from_audit(source: Source, name: str, page_name: str,
               mono: tuple[str, ...] = ()) -> Row:
    check = audit_row(source, name)
    if check is None:
        return Row(page_name, "N/A",
                   "the handoff audit did not report this row (%s)" % (source.error or "no answer"),
                   "machine", "sudo odile-audit")
    return Row(page_name, "PASS" if check.get("ok") else "FAIL",
               str(check.get("detail") or ""), "machine",
               str(check.get("command") or ""), mono=mono)


def collect(machine: Machine) -> Machine:
    """Every row the page shows, built from the machine and its programs."""
    audit = machine.audit()
    support = machine.support()
    document, report, report_error = machine.attestation()
    record = machine.boot_record()
    boot_line, boot_fingerprint, boot_count = machine.boot_key()
    host_fingerprint, host_path = machine.host_key()
    keyslots, tokens, _luks_command = machine.luks()
    seal = machine.seal_state()
    agent = machine.guest_agent()
    manifest_state, manifest_detail, signing_fingerprint = machine.manifest()
    stamp_text = iso(machine.now)

    release = None
    vcpus = None
    if document is not None:
        release = document.get("release") if isinstance(document.get("release"), str) else None
        reported = document.get("vcpus")
        if isinstance(reported, int) and not isinstance(reported, bool):
            vcpus = reported
    if record is not None:
        release = release or (record.get("release") if isinstance(record.get("release"), str) else None)
        recorded = record.get("vcpus")
        if vcpus is None and isinstance(recorded, int) and not isinstance(recorded, bool):
            vcpus = recorded

    machine.values = {
        "stamp": stamp_text,
        "measurement": document.get("measurement") if document else "",
        "measurement_short": (document.get("measurement") or "")[:16] if document else "",
        "release": release or "",
        "vcpus": str(vcpus) if vcpus is not None else "",
        "report_fingerprint": sha256_hex(report) if report else "",
        "fetched_at": document.get("fetched_at") if document and isinstance(
            document.get("fetched_at"), str) else "",
        "boot_key": boot_fingerprint,
        "host_key": host_fingerprint,
        "signing_key": signing_fingerprint,
        "state": "available" if document is not None else "unavailable",
        "state_text": "" if document is not None else report_error,
    }

    # -- from your laptop ---------------------------------------------------
    laptop = [
        Row("chain", "CHECK",
            "AMD VCEK \u2192 ASK \u2192 ARK, and the ARK is pinned at SHA-256 "
            "69:d0:63:b4\u2026:9a:54:0b:cd. snpguest walks the chain on your own "
            "computer.", "laptop",
            mono=("69:d0:63:b4\u2026:9a:54:0b:cd", "snpguest")),
        Row("signature", "CHECK",
            "ECDSA P-384 over the report's signed bytes, with the VCEK. Change one "
            "byte and the signature fails.", "laptop"),
        Row("pin", "CHECK",
            "Rebuild the measurement from the published UKI, the firmware, the vCPU "
            "count and the command line (sev-snp-measure); it must equal the "
            "measurement below and the one in your ready email.", "laptop",
            mono=("sev-snp-measure",)),
    ]

    # -- read from the machine ---------------------------------------------
    reported = []
    if document is None:
        reported.append(Row("report", "FAIL", report_error, "machine",
                            "curl -s https://<slug>.odile.fyi/attestation"))
        reported.append(Row("measurement", "N/A", "no report to measure", "machine"))
        reported.append(Row("release", "N/A", "no report to name a release", "machine"))
    else:
        fetched = machine.values["fetched_at"]
        report_bytes = report if report is not None else b""
        report_hash = sha256_hex(report_bytes)
        reported.append(Row(
            "report", "PASS",
            "fetched %s; the SHA-256 of the signed %d-byte report is %s" % (
                fetched, len(report_bytes), report_hash),
            "machine", "curl -s https://<slug>.odile.fyi/attestation",
            mono=(fetched, "%d-byte" % len(report_bytes), report_hash)))
        reported.append(Row(
            "measurement", "PASS", document["measurement"], "machine",
            "curl -s https://<slug>.odile.fyi/attestation | "
            "jq '{measurement, report_version, fetched_at}'",
            mono=(document["measurement"],)))
        if release:
            detail = release
            marks = [release]
            if vcpus is not None:
                detail += " \u00b7 %d vCPU" % vcpus
                marks.append("%d vCPU" % vcpus)
            if record is not None and record.get("measurement") not in (None, document["measurement"]):
                reported.append(Row(
                    "release", "FAIL",
                    "the installer's boot record names a different measurement than "
                    "the live report", "machine"))
            elif record is not None and record.get("release") not in (None, release):
                reported.append(Row(
                    "release", "FAIL",
                    "the installer recorded release %s, the live report names %s" % (
                        record.get("release"), release), "machine"))
            else:
                reported.append(Row("release", "PASS", detail, "machine",
                                    mono=tuple(marks)))
        else:
            reported.append(Row(
                "release", "N/A",
                "the report names no release id; the boot image is older than the "
                "release field", "machine"))

    # The boot key is the one key in the measured command line: the audit reads
    # /proc/cmdline and names the fingerprint, so the binding is the row itself.
    reported.append(from_audit(audit, "boot key", "boot key", mono=("bb.key",)))
    # The door, not the key: every line in authorized_keys must be that same
    # measured boot key. During a grant this row fails and names the grant's key,
    # which is what the ODL-217 drill measured on the live tenant.
    reported.append(from_audit(audit, "authorized keys", "authorized keys"))

    if host_fingerprint and boot_fingerprint and host_fingerprint != boot_fingerprint:
        reported.append(Row(
            "host key", "PASS",
            "%s from %s \u2014 the machine's SSH host key, distinct from the boot key"
            % (host_fingerprint, host_path), "machine",
            "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub",
            mono=(host_fingerprint, host_path)))
    elif host_fingerprint:
        reported.append(Row(
            "host key", "FAIL",
            "%s is the boot key as well; the host key must be a different key"
            % host_fingerprint, "machine",
            "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub",
            mono=(host_fingerprint,)))
    else:
        reported.append(Row("host key", "FAIL",
                            "no SSH host key on the machine", "machine"))

    reported.append(from_audit(audit, "root password", "root password"))
    reported.append(from_audit(audit, "password auth", "password auth",
                               mono=("PasswordAuthentication no",)))
    if agent:
        reported.append(Row("panel root reset", "FAIL",
                            "a guest agent is installed: %s \u2014 the panel may reset root"
                            % agent, "machine", "getent passwd"))
    else:
        reported.append(Row(
            "panel root reset", "PASS",
            "none \u2014 no guest agent on the machine", "machine",
            "getent passwd; ls /etc/systemd/system"))
    reported.append(from_audit(audit, "operator account", "accounts"))
    reported.append(from_audit(audit, "login history", "logins"))

    if keyslots and len(keyslots) == 1 and not tokens:
        reported.append(Row("LUKS", "PASS",
                            "one keyslot (slot %s), no token: one volume key, no "
                            "second way in" % keyslots[0], "machine",
                            LUKS_COMMAND, mono=("slot %s" % keyslots[0],)))
    elif not keyslots:
        reported.append(Row("LUKS", "FAIL",
                            "cannot read the root container's LUKS header",
                            "machine", LUKS_COMMAND))
    elif (support.document is not None and support.document.get("grant")
          and len(keyslots) == 2 and not tokens):
        reported.append(Row("LUKS", "INFO",
                            "2 keyslots (%s) \u2014 the second is the support grant's "
                            "handover; after revoke the header must be one keyslot again"
                            % ", ".join(keyslots), "machine", LUKS_COMMAND,
                            mono=(", ".join(keyslots),)))
    else:
        reported.append(Row("LUKS", "FAIL",
                            "%d keyslots (%s)%s" % (
                                len(keyslots), ", ".join(keyslots),
                                " and %d token(s)" % len(tokens) if tokens else ""),
                            "machine", LUKS_COMMAND,
                            mono=(", ".join(keyslots),) if keyslots else ()))

    if seal == "opened":
        reported.append(Row("boot and unlock", "PASS",
                            "booted %s; the initramfs seal opened the root "
                            "(/run/bb/sealed-unlock.state: opened)" % (
                                machine.values["fetched_at"] or "at an unrecorded time"),
                            "machine", "cat /run/bb/sealed-unlock.state"))
    elif seal.startswith("opened-bootstrap"):
        reported.append(Row("boot and unlock", "INFO",
                            "first boot: the bootstrap key opened the root; the "
                            "chip-derived key is added on this boot", "machine",
                            "cat /run/bb/sealed-unlock.state"))
    elif seal:
        reported.append(Row("boot and unlock", "FAIL",
                            "/run/bb/sealed-unlock.state says %r" % seal,
                            "machine", "cat /run/bb/sealed-unlock.state"))
    else:
        reported.append(Row("boot and unlock", "N/A",
                            "this image does not record an initramfs unlock "
                            "(/run/bb/sealed-unlock.state is absent)", "machine",
                            "cat /run/bb/sealed-unlock.state"))

    seal_verdict, seal_detail, seal_marks = machine.seal(document, report)
    reported.append(Row("seal policy", seal_verdict, seal_detail, "machine",
                        "cat /proc/cmdline", mono=seal_marks))

    signin_state, signin_detail, signin_command = machine.signin_hash()
    reported.append(Row("sign-in hash", signin_state, signin_detail, "machine",
                        signin_command))

    reported.append(support_row(machine, support, keyslots))
    reported.append(Row("manifest", manifest_state, manifest_detail, "machine",
                        "curl -s https://<slug>.odile.fyi/manifest | jq"))
    reported.append(Row(
        "app layer", "INFO",
        "not hardware-measured: Hermes, the installer and the skills are pinned by "
        "the signed manifest and a published hash, and the manifest records the "
        "machine as provisioned", "machine"))

    machine.rows = laptop + reported
    return machine


def support_row(machine: Machine, support: Source, keyslots: list[str]) -> Row:
    """The grant state, from the program that writes it."""
    if support.document is None:
        return Row("support access", "N/A",
                   "the grant program is not installed on this machine yet (%s)"
                   % (support.error or "no answer"), "machine",
                   "odile-support status")
    grant = support.document.get("grant")
    if isinstance(grant, dict):
        keys = support.document.get("authorized_keys") or {}
        lines = keys.get("lines")
        grant_lines = keys.get("grant_lines")
        detail = "grant live: %s" % (grant.get("grant_id") or "no id")
        if isinstance(grant.get("expires_at"), str):
            detail += ", expires %s" % grant["expires_at"]
        if grant.get("reason"):
            detail += ", reason %s" % grant["reason"]
        if isinstance(lines, int):
            detail += " \u00b7 %d keys in authorized_keys" % lines
        if isinstance(grant_lines, int) and grant_lines:
            detail += " (%d grant)" % grant_lines
        detail += " \u00b7 %d keyslots" % len(keyslots)
        if grant.get("key_fingerprint"):
            detail += " \u00b7 support key %s" % grant["key_fingerprint"]
        if not grant.get("key_installed"):
            detail += " \u00b7 the key line is missing from authorized_keys"
        grant_id = grant.get("grant_id")
        return Row("support access", "INFO", detail, "machine",
                   "odile-support status",
                   mono=(grant_id,) if isinstance(grant_id, str) and grant_id else ())
    expired = support.document.get("expired_at")
    revoked = support.document.get("revoked_at")
    if isinstance(expired, str):
        return Row("support access", "PASS", "no grant (expired %s)" % expired,
                   "machine", "odile-support status")
    if isinstance(revoked, str):
        return Row("support access", "PASS", "no grant (revoked %s)" % revoked,
                   "machine", "odile-support status")
    return Row("support access", "PASS", "no grant", "machine", "odile-support status")


# ---------------------------------------------------------------------------
# the page


class Verdict:
    LABELS = {"PASS": "PASS", "FAIL": "FAIL", "CHECK": "CHECK",
              "INFO": "NOTE", "N/A": "N/A"}

    @classmethod
    def css(cls, state: str) -> str:
        return {"PASS": "pass", "FAIL": "fail", "CHECK": "note",
                "INFO": "note", "N/A": "none"}.get(state, "none")

    @classmethod
    def label(cls, state: str) -> str:
        return cls.LABELS.get(state, "N/A")


def row_html(row: Row) -> str:
    value = html.escape(row.value)
    # The mono role is the machine-value role (BRAND.md §Typography): every hash,
    # fingerprint, timestamp and path in a value is set in it, so a stranger can
    # compare the page with their own command output character by character.
    marks = tuple(dict.fromkeys(
        mark for mark in row.mono + value_marks(row.value) if mark.strip()))
    for mark in sorted(marks, key=len, reverse=True):
        escaped = html.escape(mark)
        if escaped in value:
            value = value.replace(escaped, '<span class="mono">%s</span>' % escaped)
    parts = [
        '<tr class="check">',
        '<th scope="row">%s</th>' % html.escape(row.name),
        '<td><span class="verdict %s">%s</span> %s' % (
            Verdict.css(row.state), Verdict.label(row.state), value),
    ]
    if row.command:
        parts.append(' <code class="cmd">%s</code>' % html.escape(row.command))
    parts.append('</td><td class="prov">%s</td></tr>' % html.escape(
        LAPTOP_TAG if row.tag == "laptop" else MACHINE_TAG))
    return "".join(parts)


def render(machine: Machine, template: str) -> str:
    values = machine.values
    available = values["state"] == "available"
    state_block = ""
    if not available:
        state_block = (
            '<p class="state state-unavailable">Report unavailable.</p>'
            '<p class="note">%s. <a href="/verify">Try again</a> in a minute.</p>'
            % html.escape(values["state_text"] or "The CPU has not produced a report yet"))
    head = ""
    if available:
        parts = []
        if values["release"]:
            parts.append("release <span class=\"mono\">%s</span>" % html.escape(values["release"]))
        if values["vcpus"]:
            parts.append("<span class=\"mono\">%s</span> vCPU" % html.escape(values["vcpus"]))
        if values["measurement_short"]:
            parts.append("measurement <span class=\"mono\">%s\u2026</span>" % html.escape(
                values["measurement_short"]))
        if parts:
            head = " <span class=\"mono\">\u00b7</span> " + " <span class=\"mono\">\u00b7</span> ".join(parts)
    body = template
    for name, value in (
        ("TITLE", "Verify \u2014 Odile"),
        ("STAMP", html.escape(values["stamp"])),
        # The demo page's one-line verifier command carries {{MEASUREMENT}}
        # (deploy/demo-host/apply_demo_public.py); a customer page has no
        # such slot. Filled here, never left to a second renderer.
        ("MEASUREMENT", html.escape(values.get("measurement") or "unavailable")),
        ("ROBOTS", "noindex"),
        ("STATE_BLOCK", state_block),
        ("HEAD", head),
        ("LAPTOP_ROWS", "".join(row_html(row) for row in machine.rows
                                if row.tag == "laptop")),
        ("MACHINE_ROWS", "".join(row_html(row) for row in machine.rows
                                 if row.tag == "machine")),
    ):
        body = body.replace("{{%s}}" % name, value)
    leftover = re.search(r"\{\{[A-Z_]+\}\}", body)
    if leftover:
        raise PageError("the template left %s unfilled" % leftover.group(0))
    return body


def document(machine: Machine) -> dict:
    groups = [
        {"name": "From your laptop", "tag": "laptop",
         "rows": [row.as_dict() for row in machine.rows if row.tag == "laptop"]},
        {"name": "From the machine", "tag": "machine",
         "rows": [row.as_dict() for row in machine.rows if row.tag == "machine"]},
    ]
    failed = [row.name for row in machine.rows if row.state == "FAIL"]
    return {"schema": 1, "stamp": machine.values["stamp"],
            "state": machine.values["state"],
            "summary": ("PASS \u2014 no row failed" if not failed else
                        "FAIL \u2014 %d row(s): %s" % (len(failed), ", ".join(failed))),
            "groups": groups}


# ---------------------------------------------------------------------------
# serving


class Handler(BaseHTTPRequestHandler):
    root: Path = Path("/")
    luks_dump: Path | None = None
    audit_path: Path | None = None
    support_path: Path | None = None
    stamp: datetime | None = None
    template_path: Path = TEMPLATE_PATH
    server_version = "odile-verify"
    sys_version = ""

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def _machine(self) -> Machine:
        # One Machine per request: collect() fills it in, so two requests must
        # never share one.
        return Machine(root=self.root, now=self.stamp or now_utc(),
                       luks_dump=self.luks_dump, audit_program=self.audit_path,
                       support_program=self.support_path)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; font-src data:; "
            "img-src data:; base-uri 'none'; form-action 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.split("?")[0] in ("/", "/verify", "/verify/"):
            try:
                page = render(collect(self._machine()), read_text(self.template_path))
            except PageError as exc:
                self._send(500, ("%s\n" % exc).encode("utf-8"), "text/plain; charset=utf-8")
                return
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if self.path.split("?")[0] == "/health":
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return
        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    do_HEAD = do_GET


def serve(machine: Machine, template_path: Path, bind: str, port: int) -> int:
    if not template_path.is_file():
        print("verify: no template at %s" % template_path, file=sys.stderr)
        return 2
    handler = type("BoundHandler", (Handler,), {
        "root": machine.root, "luks_dump": machine.luks_dump,
        "audit_path": machine.audit_program, "support_path": machine.support_program,
        "stamp": machine.now if machine.now_pinned else None,
        "template_path": template_path})
    with ThreadingHTTPServer((bind, port), handler) as server:
        host, chosen = server.server_address[:2]
        print("http://%s:%d/" % (host, chosen), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


# ---------------------------------------------------------------------------
# the command line


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render the machine's own /verify page.")
    parser.add_argument("--root", default="/", help="the machine root (default /)")
    parser.add_argument("--out", help="write the page here (atomic, 0644)")
    parser.add_argument("--json", action="store_true", help="print the rows")
    parser.add_argument("--serve", action="store_true", help="serve the page")
    parser.add_argument("--bind", default="127.0.0.1", help="address to bind")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="port to bind (default %d)" % DEFAULT_PORT)
    parser.add_argument("--audit", help="the handoff audit program (default %s)" % AUDIT_PROGRAM)
    parser.add_argument("--support", help="the grant program (default %s)" % SUPPORT_PROGRAM)
    parser.add_argument("--luks-dump", help="read this luksDump output instead of a live device")
    parser.add_argument("--now", help="pin the provenance clock (ISO-8601 UTC)")
    parser.add_argument("--template", default=str(TEMPLATE_PATH),
                        help="the page template (default %s)" % TEMPLATE_PATH)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print("verify: --root is not a directory: %s" % root, file=sys.stderr)
        return 2
    if args.now:
        stamp = parse_iso(args.now)
        if stamp is None:
            print("verify: --now is not an ISO-8601 UTC time: %s" % args.now, file=sys.stderr)
            return 2
    else:
        stamp = now_utc()
    luks_dump = Path(args.luks_dump).resolve() if args.luks_dump else None
    if luks_dump is not None and not luks_dump.is_file():
        print("verify: --luks-dump does not exist: %s" % luks_dump, file=sys.stderr)
        return 2
    machine = Machine(root=root, now=stamp, luks_dump=luks_dump,
                      audit_program=Path(args.audit) if args.audit else None,
                      support_program=Path(args.support) if args.support else None,
                      now_pinned=bool(args.now))
    template_path = Path(args.template).resolve()

    if args.serve:
        return serve(machine, template_path, args.bind, args.port)
    if not template_path.is_file():
        print("verify: no template at %s" % template_path, file=sys.stderr)
        return 2
    collect(machine)
    if args.json:
        print(json.dumps(document(machine), indent=2, sort_keys=True))
        return 0
    try:
        page = render(machine, read_text(template_path))
    except PageError as exc:
        print("verify: %s" % exc, file=sys.stderr)
        return 2
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(".%s.%d.tmp" % (target.name, os.getpid()))
        temporary.write_text(page, encoding="utf-8")
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
        print("verify: wrote %s (%d bytes)" % (target, len(page)), file=sys.stderr)
        return 0
    sys.stdout.write(page)
    return 0


if __name__ == "__main__":
    sys.exit(main())
