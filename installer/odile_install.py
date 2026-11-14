#!/usr/bin/python3
"""Odile tenant installer — one signed command, run by the user as root.

ODL-203. The tenant install is the demo host's stack plus the tenant legs, built
once by extraction: the pure helpers below are the same ones
``deploy/demo-host/deploy_demo_host.py`` calls, so the demo host and a tenant
cannot drift apart in the parts that matter (scrypt format, .env allowlist,
config merge, atomic writes, service handling).

What this file is
-----------------
One printable, standard-library Python program. It carries the tenant
templates and the tenant bundle (the standing context and the first-run
skills), the Odile Webapp skin and its brand script inside it, so the machine
needs exactly three files:

    odile_install.py             this program
    odile_install.py.sig         detached Ed25519 signature (base64)
    odile-signing-key.pub        Odile's public key, also in the recovery kit

The signature is checked against a public key **outside** this file — the copy
in the recovery kit. A one-byte edit fails before the first state change, and a
program cannot re-sign itself without the private key.

Modes
-----
    ./odile_install.py --self-test
    ./odile_install.py --verify-signature --pubkey odile-signing-key.pub
    ./odile_install.py --dry-run                     # plan, change nothing
    ./odile_install.py --recovery-kit kit.json       # tenant install, as root
    ./odile_install.py --root /tmp/odile-sim --user odiletest --port 9140

``--root`` is the machine-free tenant simulation: the installer runs as an
unprivileged user, writes only under that root, binds the Webapp to a scratch
port, and skips apt, systemd, sshd, sudoers, root locking, DNS and ACME.
Everything else — the seed exchange and signature, the scrypt password, the
config merge, the Hermes install and Webapp build, the key file, the sign-in
gate, the chat/shell/file/cron smoke tests, the recovery file — is the tenant
path, byte for byte. The legs a simulation cannot prove (measured cmdline,
initramfs encrypt/unlock, DNS/ACME and the real certificate) are ODL-332's.

The tenant flow
---------------
1. print the signing key it will use — the exact file's SHA-256 and the key's
   ``SHA256:`` fingerprint — verify this file's signature against that key,
   and refuse a recovery kit that names a different one (exit 3);
2. assert the boot report bb-seed copied at switch-root
   (/run/blackbox/attest.json) against the measurement the user verified with
   --expect, print its release, measurement and boot-key fingerprint, and write
   /etc/odile/boot-record.json (ODL-396); a re-run without --expect reads that
   record instead, and a moved measurement stops until a new --expect is passed;
3. exchange the setup token + instance id at https://odile.fyi/seed for the
   signed seed (ODL-204), cache it at /etc/odile/seed.json (0600) and verify
   the response signature before parsing a field;
4. ask for the Odile sign-in password (16+ characters, or generate 32) and
   hash it here with scrypt — it never leaves the machine;
5. install the OS packages at exact Debian 13 pins from one dated snapshot
   (git, Caddy, Node, restic, tmux, curl, openssl, Python; Chromium best
   effort); install the pinned Hermes commit (2bd378ed1b8d, v0.23.0) as
   ``odile``; build the Webapp; write the Odile skin and link its brand
   script; write the machine's own NEAR AI key to /etc/odile/key.env;
6. write the five-block config, the three-key .env allowlist, the units, the
   per-slug Caddy site; lock root, disable password SSH, give ``odile``
   NOPASSWD sudo;
7. wait for DNS and retry ACME, print the certificate fingerprint;
8. write the recovery file (backup password + certificate fingerprint), whose
   browser-only copy points at the first-visit download, and require
   "I have copied it off the machine";
9. smoke-test one chat turn straight to NEAR AI (which runs a shell command
   and writes a file) and one cron job.

Every step compares and reports ``same`` when it is already converged, so the
second run changes nothing and spends nothing. The long install re-executes
itself under tmux when a terminal is present, so a dropped SSH session does not
abort it; ``--no-tmux`` runs it inline.

Exit codes: 0 success (changed or already converged), 1 a failed step,
2 bad input or a missing prerequisite, 3 a bad signature, 4 the recovery file
was not confirmed as copied off the machine, 5 the boot report does not match
the number the user verified (no token spent, no secret written).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import getpass
import glob
import grp
import hashlib
import http.client
import io
import json
import os
import pwd
import re
import secrets
import shutil
import socket
import ssl
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"
INSTALLER_PATH = Path(os.path.abspath(__file__))

# The pinned Hermes release: v0.23.0, the tree the demo host, the Webapp gate
# and the config schema were reviewed against. `--commit` makes install.sh
# check out exactly this tree; the version prefix is the run-time assertion.
HERMES_COMMIT = "2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19"
HERMES_VERSION_PREFIX = "Hermes Agent v0.23.0"
INSTALLER_URL = "https://hermes-agent.nousresearch.com/install.sh"
# The install script is the one third-party program this installer executes, and
# it runs as the tenant before the pinned commit applies — so the pin has to
# cover the script, not only the tree the script checks out. `ensure_install`
# hashes the fetched bytes and refuses before `bash` when they differ. A bump is
# deliberate: fetch the script, sha256sum it, update this value, re-run
# --self-test, re-sign the installer (docs/runbooks/install.md §7) and commit
# the new bytes and the signature together. Measured 2026-11-14 (68,015
# bytes); against the 2026-11-13 script the only change is git-location
# hygiene — `drop_git_location_env` unsets an inherited GIT_DIR family before
# `stage_repository` and `stage_complete` touch the checkout, and the
# broken-.git probe is fenced with `GIT_CEILING_DIRECTORIES` so a `.git` git
# cannot read cannot make it act on the user's other repository (#146032). No
# new command, URL, `eval` or privilege (ODL-497 follow-up).
HERMES_INSTALL_SHA256 = "c73dad4dcf7c9ecadb98578e28cc763d23d7be3fa1b513545319038795493214"

DEFAULT_SEED_URL = "https://odile.fyi/seed"
SCRATCH_SEED_URL = "http://127.0.0.1:8081/seed"
# The tenant backup's heartbeat (ODL-216). `scripts/backup.py` defaults to the
# same URL; `docs/runbooks/alerting.md` documents the Caddy route that answers
# it, and the ODL-215 follow-up (t_19148cba) applied it on HQ. The installer
# writes it into the nightly unit's ExecStartPost; `--heartbeat-url` overrides
# it for a receiver at another address.
HEARTBEAT_URL = "https://odile.fyi/heartbeat"
DEFAULT_TENANT_PORT = 9120
# The first-visit service's loopback port (ODL-457); Caddy is the listener.
FIRST_VISIT_PORT = 8091
# The signed-manifest listener's loopback port (ODL-472); Caddy is the
# listener at /manifest and /manifest.sig, exactly as it is for /first-visit.
MANIFEST_PORT = 8012
DEFAULT_SCRATCH_PORT = 9140
CONFIRM_PHRASE = "I have copied it off the machine"

# The OS package pins (ODL-214 follow-up). `ensure_packages` installs every
# tenant package as `name=version` from one dated Debian snapshot, so a point
# release cannot take a pin away and an unavailable pin fails the install
# instead of falling back to the latest version. Measured from Debian 13
# (trixie) 13.7, published 2026-09-12, and the snapshot below — both the
# archive index and the snapshot carry exactly these versions.
# Bumping a version means bumping OS_SNAPSHOT in the same edit, re-running the
# chroot proof (docs/runbooks/install.md §6), re-signing (§7) and releasing.
# A tenant manifest's `packages.os` must equal this table.
OS_SUITE = "trixie"
OS_SNAPSHOT = "20261108T144109Z"
OS_SNAPSHOT_BASE = "https://snapshot.debian.org/archive/debian"
OS_PACKAGE_PINS = {
    "caddy": "2.6.2-12+deb13u1",
    "curl": "8.14.1-2+deb13u5",
    "git": "1:2.47.3-0+deb13u1",
    "nodejs": "20.19.2+dfsg-1+deb13u2",
    "npm": "9.2.0~ds1-3",
    "openssl": "3.5.7-1~deb13u2",
    "python3": "3.13.5-1",
    "restic": "0.18.0-1+b4",
    "tmux": "3.5a-3",
}
# Chromium is best effort (the browser tool wants it; a minimal image may not);
# it is still exact-versioned, and a failure skips it — never "latest".
OS_OPTIONAL_PINS = {"chromium": "150.0.7871.181-1~deb13u1"}


SEED_KEYS = (
    "slug", "instance_id", "near_api_key", "model", "support_public_key",
    "backup_repo_url", "mail_relay", "hermes_version",
)
SEED_FORBIDDEN = ("password", "passphrase", "private_key", "ssh_private_key", "luks")

TENANT_ENV_KEYS = (
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH",
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
)
# The per-user inference key: the seed's `near_api_key`, written to
# /etc/odile/key.env (0600 root) and read by odile-agent.service through
# EnvironmentFile=. The agent's own .env carries no inference credential.
NEAR_KEY_ENV = "HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY"
NEAR_BASE_URL = "https://cloud-api.near.ai/v1"
# /etc/odile is 0750 root:<this group>, and the image's manifest listener is
# its only non-root reader. The group is created by install-stack.sh in the
# image and by this installer on a classic install.
MANIFEST_GROUP = "odile-manifest"
SEAT_SECRET_RE = re.compile(
    r"^(GITHUB_TOKEN|SUDO_PASSWORD|EMAIL_PASSWORD|HERMES_DASHBOARD_SESSION_TOKEN|TELEGRAM_|MATRIX_)=\s*\S+"
)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
INSTANCE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
HEX96_RE = re.compile(r"^[0-9a-fA-F]{96}$")

# ODL-396: the boot report Servury's bb-seed copies at switch-root, and the
# per-machine record the installer leaves behind. The report is read as a file;
# the installer never calls /dev/sev-guest (the running tenant answers that).
BOOT_REPORT_DEFAULT = "/run/blackbox/attest.json"
BOOT_RECORD_SCHEMA = "odile.boot-record.v1"
BOOT_REPORT_SIZE = 1184
BOOT_REPORT_VERSION = 5
MEASUREMENT_OFFSET = 0x90
MEASUREMENT_END = 0xC0

MANAGED_KEYS = ("model", "agent", "custom_providers", "webapp", "onboarding")
# A config written before the onboarding block existed carries exactly these
# four; a re-run adds the fifth in place instead of replacing the whole file.
PRE_ONBOARDING_KEYS = ("model", "agent", "custom_providers", "webapp")
TOP_LEVEL_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):")

# The tenant's boot-report transformer begins here (ODL-397).
# The transformer for a tenant's boot report (ODL-397). attest.py is the
# canonical implementation; this is its source, byte for byte, so the signed
# installer carries exactly the program it writes to
# /usr/local/lib/odile/attest.py. tests/test_attest.py compares the string
# and the pin with attest.py on every make check; a hand edit fails the gate.
ATTEST_SHA256 = "2ce3ffafd355b5f952f641a3109bbba26a659fb750cf94220ac4525ddf435e3a"
ATTEST_SOURCE = r'''#!/usr/bin/python3
"""Fetch this guest's SEV-SNP attestation report and cache it for the web app.

Runs as root before the service starts (the /dev/sev-guest node is 0600).
Writes JSON: the launch measurement, the raw report, and when it was taken.

On a Servury tenant the report is produced once at boot by `bb-seed`, which
copies it to /run/blackbox/attest.json (0644) before switch-root. The same
program transforms that boot report into the served shape, so /attestation on a
tenant is the report the running machine actually booted with:

    ./attest.py                                        # fetch now (root)
    ./attest.py --boot-report /run/blackbox/attest.json [OUT]

The boot report is per boot and does not change while the machine runs, so a
oneshot at boot writes the served copy; a reboot writes a new one. This is the
boot report, not a liveness proof: it does not measure the app layer.
"""
import argparse
import base64
import ctypes
import fcntl
import json
import os
import secrets
from datetime import datetime, timezone

SNP_GET_REPORT = 0xC0205300  # _IOWR('S', 0, struct snp_guest_request_ioctl)
DEFAULT_OUT = "/var/lib/odile/attestation.json"

# SEV-SNP report layout: the 48-byte SHA-384 launch digest is 0x90:0xC0, and
# the first four bytes are the report version. The raw report is the signed
# evidence; the JSON summary only describes it.
MEASUREMENT_OFFSET = 0x90
MEASUREMENT_SIZE = 48
NOTE = ("48-byte SHA-384 launch digest. Verify the full report with snpguest "
        "against the AMD VCEK chain.")
BOOT_NOTE = (NOTE + " This is the boot report, not a liveness proof. It does "
             "not measure the app layer.")


def read_cmdline(path: str = "/proc/cmdline") -> str:
    """The machine's own command line; it contains the release and the boot key."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def cmdline_value(cmdline: str, key: str) -> str | None:
    """The value of one `key=value` token in a command line, or None."""
    prefix = key + "="
    for token in cmdline.split():
        if token.startswith(prefix):
            value = token[len(prefix):]
            if value:
                return value
    return None


class _Req(ctypes.Structure):
    _fields_ = [
        ("msg_version", ctypes.c_uint8),
        ("_pad", ctypes.c_uint8 * 7),
        ("req_data", ctypes.c_uint64),
        ("resp_data", ctypes.c_uint64),
        ("exitinfo2", ctypes.c_uint64),
    ]


def fetch(nonce: bytes) -> bytes:
    req = ctypes.create_string_buffer(96)   # struct snp_report_req
    resp = ctypes.create_string_buffer(4000)
    ctypes.memmove(req, nonce[:64], min(len(nonce), 64))
    io = _Req(1, (ctypes.c_uint8 * 7)(), ctypes.addressof(req), ctypes.addressof(resp), 0)
    fd = os.open("/dev/sev-guest", os.O_RDWR)
    try:
        fcntl.ioctl(fd, SNP_GET_REPORT, io, True)
    finally:
        os.close(fd)
    if io.exitinfo2:
        raise OSError("guest request failed: vmm=%d fw=%d" % (io.exitinfo2 >> 32, io.exitinfo2 & 0xFFFFFFFF))
    return bytes(resp)


def boot_time(path: str) -> str | None:
    """The boot report's write time in UTC — the boot, not the read.

    `bb-seed` writes the file before switch-root, so its mtime is the boot.
    None when the file is missing: an unavailable document has no boot time.
    """
    try:
        stamp = os.stat(path).st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def unavailable(reason: str, fetched_at: str | None) -> dict:
    """The shape served when there is no usable report; the reason is the value."""
    return {
        "available": False,
        "guest": "sev-snp",
        "fetched_at": fetched_at,
        "note": BOOT_NOTE,
        "error": reason,
    }


def transform_boot_report(document: dict, fetched_at: str | None) -> dict:
    """The Servury boot report, in the shape attest.py serves and odile-verify reads.

    Every field comes from the report, never from a guess: the measurement is
    0x90:0xC0 of the signed bytes, the version and size are the report's own,
    and `fetched_at` is the boot report's write time. `release` and `cmdline`
    are carried because they are the measured release and command line the
    boot record hashes; a document without them still verifies, but
    `odile-verify --record` fails closed.
    """
    boot: dict[str, str] = {}
    release = document.get("release")
    if isinstance(release, str) and release.strip():
        boot["release"] = release.strip()
    cmdline = document.get("cmdline")
    if isinstance(cmdline, str) and cmdline.strip():
        boot["cmdline"] = cmdline.strip()

    status = document.get("status")
    if status != "ok":
        return {**unavailable("the boot report status is %r" % (status,), fetched_at), **boot}

    encoded = document.get("report_b64")
    if not isinstance(encoded, str) or not encoded:
        return {**unavailable("the boot report has no report_b64", fetched_at), **boot}
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        return {**unavailable("report_b64 is not valid base64: %s" % exc, fetched_at), **boot}
    if len(raw) < MEASUREMENT_OFFSET + MEASUREMENT_SIZE:
        return {
            **unavailable(
                "the report is %d bytes, shorter than the 0x%X measurement"
                % (len(raw), MEASUREMENT_OFFSET + MEASUREMENT_SIZE),
                fetched_at,
            ),
            **boot,
        }
    measurement = raw[MEASUREMENT_OFFSET:MEASUREMENT_OFFSET + MEASUREMENT_SIZE]
    if measurement == b"\x00" * MEASUREMENT_SIZE:
        return {**unavailable("the report carries an empty measurement", fetched_at), **boot}

    return {
        "available": True,
        "guest": "sev-snp",
        "measurement": measurement.hex(),
        "report_version": int.from_bytes(raw[0:4], "little"),
        "report_size": len(raw),
        "fetched_at": fetched_at,
        "report_b64": base64.b64encode(raw).decode("ascii"),
        "note": BOOT_NOTE,
        **boot,
    }


def read_boot_report(path: str) -> tuple[dict | None, str | None]:
    """The Servury JSON and no error, or None and the reason it cannot be read."""
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError:
        return None, "no boot report at %s" % path
    except ValueError as exc:
        return None, "the boot report at %s is not JSON: %s" % (path, exc)
    if not isinstance(document, dict):
        return None, "the boot report at %s is not a JSON object" % path
    return document, None


def write_document(out: str, payload: dict) -> None:
    """Write the served copy atomically, 0644: a reader sees old or new, never half."""
    directory = os.path.dirname(out) or "."
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(directory, ".%s.%d.tmp" % (os.path.basename(out), os.getpid()))
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.chmod(temp, 0o644)
        os.replace(temp, out)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve this machine's SEV-SNP report as JSON.")
    parser.add_argument("out", nargs="?", default=DEFAULT_OUT,
                        help="served JSON path (default %s)" % DEFAULT_OUT)
    parser.add_argument("--boot-report", metavar="PATH",
                        help="transform the boot report at PATH instead of reading /dev/sev-guest")
    parser.add_argument("--boot-time", metavar="ISO8601",
                        help="fetched_at for --boot-report (default: the file's mtime)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.boot_report:
        fetched_at = args.boot_time or boot_time(args.boot_report)
        document, error = read_boot_report(args.boot_report)
        if document is None:
            payload = unavailable(error or "no boot report at %s" % args.boot_report, fetched_at)
        else:
            payload = transform_boot_report(document, fetched_at)
        write_document(args.out, payload)
        detail = payload.get("measurement") or payload.get("error") or "unavailable"
        print("attestation -> %s (%s)" % (args.out, detail[:64]))
        return 0

    nonce = b"odile:" + secrets.token_bytes(24)
    # The release and the command line come from the same measured string the
    # CPU hashed; the served document uses them to select a pin entry and to
    # hash the command line into the boot record.
    cmdline = read_cmdline()
    boot = {}
    release = cmdline_value(cmdline, "bb.release")
    if release:
        boot["release"] = release
    if cmdline:
        boot["cmdline"] = cmdline
    cpus = os.cpu_count()
    if isinstance(cpus, int) and cpus > 0:
        boot["vcpus"] = cpus
    payload = {"available": False, "guest": "sev-snp", "fetched_at": None, **boot}
    try:
        raw = fetch(nonce)
        at = raw.find(nonce)
        if at < 0:
            raise OSError("nonce not echoed in report")
        # report_data starts at 0x50 in recent report versions, 0x4C before that
        start = -1
        for rdoff in (0x50, 0x4C):
            cand = at - rdoff
            if cand >= 0 and int.from_bytes(raw[cand:cand + 4], "little") in (2, 3, 4, 5):
                start = cand
                break
        if start < 0:
            raise OSError("could not locate the report header")
        # report_data is 64 bytes starting at the nonce; measurement follows it
        measurement = raw[at + 64: at + 64 + 48]
        if len(measurement) != 48 or measurement == b"\x00" * 48:
            raise OSError("empty measurement")
        # the driver returns {u32 status, u32 size, <header>, report}; keep the bare report
        size = int.from_bytes(raw[4:8], "little")
        report = raw[start: start + size] if 0 < size and start + size <= len(raw) else raw
        payload = {
            "available": True,
            "guest": "sev-snp",
            "measurement": measurement.hex(),
            "report_version": int.from_bytes(report[0:4], "little"),
            "report_size": len(report),
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "report_b64": base64.b64encode(report).decode("ascii"),
            "note": NOTE,
            **boot,
        }
    except (OSError, ValueError) as exc:
        payload["error"] = str(exc)

    write_document(args.out, payload)
    print("attestation -> %s (%s)" % (args.out, payload.get("measurement", payload.get("error", "unavailable"))[:32]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

# BEGIN BACKUP (ODL-215)
# Generated by installer/embed_backup.py; do not edit by hand.
# The tenant backup is the reviewed program, byte for byte, so the signed
# installer carries exactly the file it writes to /usr/local/lib/odile/.
# --self-test fails on a one-byte edit.
BACKUP_SHA256 = "d9c4278a0660650e7edc6b50847d96cdbb71ca7709c52402257e94a291a98955"
BACKUP_SOURCE = r'''#!/usr/bin/python3
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
(no restic, no recovery file, no repository URL).
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
    """(user, host, port) for an sftp repository, or None for a local one."""
    if not repo.startswith("sftp:"):
        return None
    match = SFTP_HOST_RE.match(repo)
    if not match:
        raise BadInput(f"cannot parse the SFTP repository {repo!r}")
    return match.group("user"), match.group("host"), match.group("port")


def restic_binary() -> str:
    found = shutil.which("restic")
    if not found:
        raise Missing("restic is not installed (the image pins restic 0.18.0)")
    return found


# --------------------------------------------------------------------------- restic calls


def sftp_args(repo: str, key: str | Path, known_hosts: str | Path) -> list[str]:
    """`-o sftp.command=...` so restic uses this machine's key, not root's agent."""
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
    ]
    if port:
        command += ["-p", port]
    # `-s sftp` is the remote command, so it must follow the destination.
    command += [f"{user}@{host}", "-s", "sftp"]
    return ["-o", "sftp.command=" + " ".join(_quote_arg(part) for part in command)]


def _quote_arg(value: str) -> str:
    """Quote one word for the sftp.command string restic hands to a shell."""
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


class Restic:
    """One restic context: repository, password file, transport, cache."""

    def __init__(self, repo: str, password: str, *, key: str | Path | None = None,
                 known_hosts: str | Path | None = None, cache: str | None = None) -> None:
        self.repo = repo
        self.password = password
        self.key = Path(key).expanduser() if key else None
        self.known_hosts = Path(known_hosts).expanduser() if known_hosts else None
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
            options = sftp_args(self.repo, self.key, self.known_hosts or "")
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
                    known_hosts=known_hosts if target else None)
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
        public = Path(str(key) + ".pub")
        print(f"public key   {public} (register this one line on the backup box)")
        print(public.read_text(encoding="utf-8").strip())
    return 0


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
                    known_hosts=known_hosts if target else None)
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
    if args.dry_run:
        print(f"would run    restic {' '.join(_quote_arg(part) for part in backup_arguments)}")
        print(f"would run    restic {' '.join(_quote_arg(part) for part in forget_arguments)}")
        print(f"would write  {args.status} (last_success_at, snapshot, size)")
        return 0

    started = time.monotonic()
    status_path = Path(args.status).expanduser()
    previous = read_json(status_path)
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
    stats = restic.run(["stats", "--mode", "raw-data", "--json"])
    repo_bytes = None
    if stats.returncode == 0:
        try:
            repo_bytes = json.loads(stats.stdout).get("total_size")
        except ValueError:
            repo_bytes = None
    pruned = restic.run(forget_arguments)
    record = {
        "schema": STATUS_SCHEMA,
        "slug": args.slug or host,
        "host": host,
        "repo": repo,
        "status": "ok",
        "at": int(time.time()),
        "at_iso": utc_now(),
        "last_success_at": int(time.time()),
        "snapshot": snapshot,
        "snapshot_short": snapshot[:8],
        "files_new": summary.get("files_new"),
        "files_changed": summary.get("files_changed"),
        "bytes_processed": summary.get("total_bytes_processed"),
        "repo_bytes": repo_bytes,
        "duration_seconds": duration,
        "retention": {"keep_daily": args.keep_daily, "keep_weekly": args.keep_weekly},
        "forget": "ok" if pruned.returncode == 0 else "failed",
        "restic": _restic_version(restic),
    }
    write_json(status_path, record)
    print(
        f"ok           snapshot {record['snapshot_short']} of {home} in {duration:.1f}s "
        f"({record['bytes_processed']} bytes processed, {record['files_new']} new files)"
    )
    print(f"status       {status_path}")
    if pruned.returncode != 0:
        print("warning      retention/prune failed; the snapshot is still valid", file=sys.stderr)
    return 0


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


def command_restore(args) -> int:
    repo = resolve_repo(args.repo, args.repo_file)
    key = Path(args.key).expanduser()
    known_hosts = Path(args.known_hosts).expanduser()
    target = sftp_target(repo)
    if target and not key.exists():
        raise Missing(f"no SFTP key at {key}; run `backup.py init` first")
    password = read_recovery_password(args.recovery)
    restic = Restic(repo, password, key=key if target else None,
                    known_hosts=known_hosts if target else None)
    arguments = ["restore", args.snapshot, "--target", args.target]
    for include in args.include or []:
        arguments += ["--include", include]
    if args.dry_run:
        print(f"would run    restic {' '.join(_quote_arg(part) for part in arguments)}")
        return 0
    Path(args.target).expanduser().mkdir(parents=True, exist_ok=True)
    done = restic.run(arguments)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise BackupError(f"restic restore failed: {detail[-1] if detail else 'no output'}")
    lines = [line for line in done.stdout.strip().splitlines() if line.strip()]
    print(f"ok           restored {args.snapshot} to {args.target}")
    if lines:
        print(f"restic       {lines[-1]}")
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
                    known_hosts=known_hosts if target else None)
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
    }
    good = Restic(repo, password, key=options["key"], known_hosts=options["known_hosts"]).run(
        ["snapshots", "--json", "--no-lock"]
    )
    wrong = Restic(
        repo, "wrong-" + secrets.token_hex(16),
        key=options["key"], known_hosts=options["known_hosts"],
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

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {script} run --repo-file {repo_file}{repo_arg} --recovery {recovery} --home {home} --key {key} --known-hosts {known_hosts} --status {status} --slug {slug}
ExecStartPost=-/usr/bin/python3 {script} heartbeat --status {status} --slug {slug} --url {url} --key-dir {heartbeat_dir}
Nice=10
IOSchedulingClass=idle
"""

TIMER_TEMPLATE = """\
[Unit]
Description=Odile tenant backup, daily at 03:00

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
RandomizedDelaySec=300

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
    timer = TIMER_TEMPLATE
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
    print("enabled      odile-backup-tenant.timer (daily 03:00, Persistent=true)")
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
'''
# END BACKUP (ODL-215)

# BEGIN FIRST VISIT (ODL-457)
# Generated by installer/embed_first_visit.py; do not edit by hand.
# The first-visit service and its page are the reviewed files, byte for
# byte, so the signed installer carries exactly the program and page it
# writes to /usr/local/lib/odile/. --self-test fails on a one-byte edit.
FIRST_VISIT_SHA256 = "12c5c346d7a08cd3d3531480fc23a0f801f309d4fc11689ed66e702a625a2d9c"
FIRST_VISIT_SOURCE = r'''#!/usr/bin/python3
"""odile first-visit service — set the Odile sign-in on the guest.

The strict bar sets the sign-in on the machine, at first visit, with no
terminal. This service is the one page that does it. The installer writes a
single-use token's SHA-256 and this program's unit; the user opens
``https://<slug>.odile.fyi/first-visit?t=<token>`` from the ready page, chooses
the password that opens the Hermes Webapp, and is shown a recovery code once.
Nothing here ever sends the password, the token or the recovery code anywhere:
the POST is same-origin, the page makes no third-party request, and the only
file that changes is the tenant's own ``.env`` (the scrypt hash) and this
service's state file (the hashes).

    GET  /first-visit?t=<token>   the form, while the token is unspent
    POST /first-visit             write the basic-auth hash; spend the token
    GET  /first-visit/recovery    the recovery file, with the code as the key
    GET  /first-visit/reset       the reset form (recovery code + new password)
    POST /first-visit/reset       write a new hash; rotate the recovery code
    GET  /health                  ok

A refused link renders one of two pages: the spent token still offers the
reset code, and a state with no recovery hash (the sign-in was never set, or
the token is unknown on such a machine) offers the ready email's link instead
of ``/first-visit/reset``, which would answer 409 there.

Design notes a reviewer should check:

* **The token is spent on POST, not on GET.** Mail scanners prefetch links and
  a phone reloads a tab; a GET that consumed the token would lock a user out
  before they typed anything. The same choice is recorded for the claim page in
  ``docs/architecture/DESIGN-claim-ceremony.md``. After the sign-in is set the
  token is dead for good: every later GET or POST answers 410.
* **The recovery file is behind the recovery code.** The done page shows the
  code once and links to ``/first-visit/recovery?c=<code>``; the GET verifies
  the code against its scrypt hash before it reads anything. The link is
  same-origin, the page is ``Referrer-Policy: no-referrer`` and Caddy discards
  the access log, so the code is not copied into a log or a third-party
  request. The file holds the backup password, so an unauthenticated URL would
  be a leak; the code is ~100 bits and rotates on a reset.
* **The fingerprint is the certificate the browser is using.** When the request
  arrived over TLS (Caddy's ``X-Forwarded-Proto: https``), the service reads
  the certificate for the request's host from Caddy's store and writes its
  SHA-256 into the file before serving it — and back to ``/etc/odile/recovery.txt``
  (0600, the same owner), so the copy the agent shows carries the same value.
  With no certificate (a bench, or a host Caddy has not issued for yet) the
  placeholder stays and the page says it is pending. Any other proxy protocol
  keeps the placeholder: this service never claims a TLS value it did not see.
* **The download is the file.** Same bytes, ``text/plain``, a filename from
  the file's own ``Tenant:`` line, ``no-store``. A missing or empty file means
  no link and a 404; an empty body is never served.
* **The token never leaves a hash.** The plaintext arrives in the URL (the
  ready page's link) and in the form body; only SHA-256 stays on disk. The
  tenant Caddyfile logs to ``output discard`` and the page is
  ``Referrer-Policy: no-referrer``, so the query string is not copied into a
  log or a third-party request.
* **One writer.** ``POST`` holds an ``flock`` on ``<state>.lock`` across
  read-verify-write, so two clicks cannot both spend the token or both rotate
  the recovery code.
* **The hash format is Hermes's**, not this program's: the provider stores
  ``scrypt$n$r$p$salt$dk`` with n=2**14, r=8, p=1. The installer has its own
  copy of the two functions and ``tests/test_first_visit.py`` proves the copies
  agree byte for byte in both directions.
* **Fail loudly.** A write or a restart that fails answers 500 and leaves the
  token unspent, so the user can try again; the reason goes to stdout (journal)
  and never contains a secret.

Standard library only. No JavaScript on the page. 390 px is the tested floor.

    /usr/bin/python3 first_visit.py --port 8091 \
        --state /etc/odile/first-visit.json \
        --env-file /home/odile/.hermes/.env \
        --page /usr/local/lib/odile/first-visit.html
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import html
import json
import os
import re
import secrets
import shlex
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple, Sequence
from urllib.parse import parse_qs, quote, urlparse

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8091
DEFAULT_STATE = "/etc/odile/first-visit.json"
DEFAULT_ENV = "/home/odile/.hermes/.env"
DEFAULT_PAGE = "/usr/local/lib/odile/first-visit.html"
DEFAULT_RELOAD = "systemctl restart odile-agent"
# The installer writes this 0600 file; the service completes its fingerprint
# and hands it to the browser that holds the recovery code.
DEFAULT_RECOVERY = "/etc/odile/recovery.txt"
# The machine's own access report (ODL-473). The boot oneshot writes it 0644
# from `verify_and_unlock.sh --mode access --out`; this service renders it at
# /verify with the provenance line the file carries. The page is the same
# template HQ serves (web/verify.html), rendered in its tenant state.
DEFAULT_VERIFY_PAGE = "/usr/local/lib/odile/verify.html"
DEFAULT_REPORT = "/var/lib/odile/access-report.txt"
DEFAULT_ATTEST = "/var/lib/odile/attestation.json"
# After the sign-in hash replaces the placeholder, the report's sign-in line
# would still describe the old file; ask the boot oneshot to rewrite it. It is
# a separate unit, so a slow scan never delays the POST's response. `restart`,
# not `start`: the unit is RemainAfterExit=yes and already active after boot,
# so a start would be a no-op and the report would keep the placeholder line.
DEFAULT_REPORT_COMMAND = "systemctl restart --no-block odile-access-report.service"
# Caddy stores a leaf certificate at <root>/<issuer>/<host>/<host>.crt. Read
# only; the unit's ReadWritePaths already includes /etc/odile for the write-back.
DEFAULT_CADDY_STORES = (
    "/var/lib/caddy/.local/share/caddy/certificates",
    "/root/.local/share/caddy/certificates",
)

# The two keys this service owns in the tenant .env; the rest of the file
# (the cookie secret) is preserved exactly.
PASSWORD_KEY = "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH"
USERNAME_KEY = "HERMES_DASHBOARD_BASIC_AUTH_USERNAME"
DEFAULT_USERNAME = "odile"

STATE_VERSION = 1
MAX_BODY = 8 * 1024
MIN_PASSWORD = 16
MAX_PASSWORD = 1024
RATE_LIMIT = 10
RATE_WINDOW = 600

# Recovery code alphabet: no I, O, 0 or 1, so a code read off a screen and
# typed on a phone is unambiguous. Five groups of four is ~100 bits.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_GROUPS = 5
CODE_GROUP_LEN = 4
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
CODE_RE = re.compile(r"^[A-HJ-NP-Z2-9]{%d}$" % (CODE_GROUPS * CODE_GROUP_LEN))

# The installer's file grammar; the service completes one field and serves the
# rest byte for byte. Tenant: names the download; Certificate SHA-256: is the
# only line this service ever rewrites.
RECOVERY_TENANT_RE = re.compile(r"^Tenant:[ \t]*(\S+)[ \t]*$", re.M)
RECOVERY_CERT_RE = re.compile(r"^Certificate SHA-256:([ \t]*)(.*)$", re.M)
RECOVERY_PASSWORD_RE = re.compile(r"^Backup password:.*$", re.M)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")

# A user can paste anything into the recovery-code field, braces included, so
# that slot is filled after the unfilled-slot check through a sentinel.
CODE_SENTINEL = "\x00recovery-code\x00"
ERROR_FIELDS = ("code", "password", "password2")

BLOCK_RE = re.compile(
    r"<!--(form|done|reset-done|reset|refused-unknown|refused)-->(.*?)<!--/\1-->", re.S)

_hits: dict[str, list[float]] = {}
_lock = threading.Lock()


# --------------------------------------------------------------------- hashing

def hash_password(password: str) -> str:
    """The Hermes dashboard provider's scrypt format, stdlib only."""
    n, r, p, dklen, salt_bytes = 2**14, 8, 1, 32, 16
    salt = secrets.token_bytes(salt_bytes)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=0
    )
    return "scrypt${}${}${}${}${}".format(
        n, r, p, base64.b64encode(salt).decode(), base64.b64encode(derived).decode()
    )


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of a stored hash; False on any malformed input."""
    try:
        scheme, n_s, r_s, p_s, salt_b64, dk_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except (ValueError, TypeError):
        return False
    if not (16 <= len(salt) <= 64 and 16 <= len(expected) <= 128):
        return False
    try:
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected), maxmem=0
        )
    except (ValueError, MemoryError):
        return False
    return secrets.compare_digest(actual, expected)


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return "Use at least %d characters." % MIN_PASSWORD
    if len(password) > MAX_PASSWORD:
        return "Use at most %d characters." % MAX_PASSWORD
    return None


def new_recovery_code() -> str:
    groups = [
        "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_GROUP_LEN))
        for _ in range(CODE_GROUPS)
    ]
    return "-".join(groups)


def normalise_code(raw: str) -> str:
    """Uppercase, letters and digits only; a pasted code with spaces works."""
    return re.sub(r"[^A-Z2-9]", "", raw.upper())


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "strict")).hexdigest()


# ----------------------------------------------------------------------- state

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "token_sha256": "",
        "created_at": utc_now(),
        "used_at": None,
        "signin_set_at": None,
        "recovery_hash": None,
        "recovery_issued_at": None,
        "recovery_used_at": None,
    }


def validate_state(state: object) -> dict:
    """The one shape this program will load; anything else is a loud failure."""
    if not isinstance(state, dict):
        raise ValueError("the state file is not a JSON object")
    if state.get("version") != STATE_VERSION:
        raise ValueError("unsupported state version %r" % (state.get("version"),))
    token_hash = state.get("token_sha256")
    if not isinstance(token_hash, str) or not TOKEN_RE.match(token_hash):
        raise ValueError("token_sha256 is not 64 lowercase hex")
    for key in ("used_at", "signin_set_at", "recovery_issued_at", "recovery_used_at"):
        value = state.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError("%s is not a string or null" % key)
    recovery = state.get("recovery_hash")
    if recovery is not None and not (isinstance(recovery, str) and recovery.startswith("scrypt$")):
        raise ValueError("recovery_hash is not an scrypt hash")
    return state


def load_state(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit("error: %s does not exist; the installer writes it" % path)
    except OSError as exc:
        raise SystemExit("error: cannot read %s: %s" % (path, exc))
    try:
        return validate_state(json.loads(raw))
    except ValueError as exc:
        raise SystemExit("error: %s: %s" % (path, exc))


def initialise_state(path: Path, token: str, *, created_at: str | None = None) -> dict:
    """Write a fresh state for a token whose plaintext the user holds.

    The installer calls this on the first run and never on a re-run: a machine
    that already carries a state keeps it, so a redeploy cannot re-open a
    first visit that is done.
    """
    if not TOKEN_RE.match(token):
        raise SystemExit("error: the first-visit token is not 64 lowercase hex")
    state = empty_state()
    state["token_sha256"] = hash_token(token)
    if created_at:
        state["created_at"] = created_at
    save_state(path, state)
    return state


def save_state(path: Path, state: dict) -> None:
    validate_state(state)
    body = json.dumps(state, indent=2, sort_keys=True) + "\n"
    write_atomic(path, body.encode("utf-8"), 0o600)


def write_atomic(path: Path, data: bytes, mode: int, owner: tuple[int, int] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        if owner is not None:
            os.chown(tmp_name, owner[0], owner[1])
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


@contextmanager
def locked(state_path: Path):
    """One writer across read-verify-write; the lock file is the ledger's neighbour."""
    lock_path = state_path.with_name(state_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


# --------------------------------------------------------------- recovery file

class RecoveryFile(NamedTuple):
    """The bytes to serve, the slug that names the download, and the state."""

    text: str
    slug: str
    filled: bool


def request_host(raw: str) -> str:
    """The Host header's hostname, lowercased; '' when it is not usable."""
    if not raw:
        return ""
    try:
        host = urlparse("//" + raw).hostname or ""
    except ValueError:
        return ""
    host = host.strip().lower().rstrip(".")
    return host if HOST_RE.match(host) else ""


# A certificate store file may hold a chain: certmagic writes the leaf and
# its issuer blocks into one .crt, for internal and ACME issuers alike. The
# value a browser shows, and `openssl x509 -fingerprint` prints, is the leaf
# alone — so only the first block may enter the digest.
PEM_LEAF_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S)


def leaf_der(pem: str) -> bytes | None:
    """DER of the first certificate in a PEM bundle, or None.

    ``ssl.PEM_cert_to_DER_cert`` takes everything between the *first* BEGIN
    header and the *last* END footer. On a store file that carries the issuer
    chain it either returns the concatenated DER of every block — a digest no
    browser shows — or raises, depending on the base64 padding, which left the
    fingerprint unfilled. Slice the first block out first.
    """
    match = PEM_LEAF_RE.search(pem)
    if match is None:
        return None
    try:
        der = ssl.PEM_cert_to_DER_cert(match.group(0) + "\n")
    except (ValueError, ssl.SSLError):
        return None
    return der or None


def certificate_sha256(host: str, stores: Sequence[Path]) -> str | None:
    """SHA-256 of the leaf certificate Caddy holds for `host`, or None.

    The digest is over the leaf's DER certificate, the same value
    ``openssl x509 -fingerprint -sha256`` prints and the one a browser shows
    in the certificate viewer. A store file that carries the issuer chain
    behind the leaf contributes the leaf only. The newest file wins, so a
    renewed certificate is not shadowed by an older one in another issuer dir.
    """
    if not host:
        return None
    newest: tuple[float, bytes] | None = None
    for root in stores:
        try:
            paths = list(root.glob("*/%s/%s.crt" % (host, host)))
        except OSError:
            continue
        for path in paths:
            try:
                info = path.stat()
                pem = path.read_text(encoding="utf-8")
                der = leaf_der(pem)
            except (OSError, ValueError):
                continue
            if not der:
                continue
            if newest is None or info.st_mtime > newest[0]:
                newest = (info.st_mtime, der)
    return "sha256:" + hashlib.sha256(newest[1]).hexdigest() if newest else None


def read_recovery(path: Path) -> str | None:
    """The file's text, or None when it is missing, unreadable or empty."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return text if text.strip() else None


def write_recovery(path: Path, text: str) -> None:
    """Write the completed file back as 0600, keeping the installer's owner."""
    try:
        info = path.stat()
        owner = (info.st_uid, info.st_gid)
    except OSError:
        owner = (0, 0)
    write_atomic(path, text.encode("utf-8"), 0o600, owner)


def complete_recovery(path: Path, host: str, secure: bool,
                      stores: Sequence[Path]) -> RecoveryFile | None:
    """Read the recovery file, fill the certificate line, write it back.

    The write is a compare-and-replace of one line: a second call with the
    same certificate leaves the bytes and the mtime alone. Failure to write is
    logged and does not lose the download; the served bytes are the completed
    ones either way.
    """
    text = read_recovery(path)
    if text is None:
        return None
    tenant = RECOVERY_TENANT_RE.search(text)
    slug = tenant.group(1) if tenant and SLUG_RE.match(tenant.group(1)) else ""

    match = RECOVERY_CERT_RE.search(text)
    value = match.group(2).strip() if match else ""
    if secure:
        fingerprint = certificate_sha256(host, stores)
        if fingerprint and fingerprint != value:
            if match is not None:
                text = text[:match.start(2)] + fingerprint + text[match.end(2):]
            else:
                # A hand-edited file without the line still gets the value,
                # beside the password it belongs with.
                anchor = RECOVERY_PASSWORD_RE.search(text)
                if anchor is not None:
                    at = anchor.end()
                    text = text[:at] + "\nCertificate SHA-256: " + fingerprint + text[at:]
                else:
                    text = text.rstrip("\n") + "\nCertificate SHA-256: " + fingerprint + "\n"
            try:
                write_recovery(path, text)
                print("first-visit: recovery file completed for %s" % host, flush=True)
            except OSError as exc:
                print("first-visit: could not write %s: %s" % (path, exc),
                      file=sys.stderr, flush=True)
            value = fingerprint
    return RecoveryFile(text=text, slug=slug, filled=value.startswith("sha256:"))


def recovery_filename(recovery: RecoveryFile) -> str:
    return "odile-recovery-%s.txt" % recovery.slug if recovery.slug else "odile-recovery.txt"


def recovery_code_ok(state: dict, raw: str) -> bool:
    """The download's key is the recovery code the page showed once."""
    stored = state.get("recovery_hash")
    if not isinstance(stored, str) or not stored.startswith("scrypt$"):
        return False
    normalised = normalise_code(raw)
    if not CODE_RE.match(normalised):
        return False
    return verify_password(normalised, stored)


def recovery_file_block(recovery: RecoveryFile | None, code: str) -> str:
    """The done page's download, or the sentence that names the absence."""
    if recovery is None:
        return ('<p class="note" id="recovery-file">'
                'The recovery file is not available on this machine.</p>')
    href = "/first-visit/recovery?c=" + quote(code, safe="")
    filename = recovery_filename(recovery)
    if recovery.filled:
        holds = "the backup password and this machine's certificate fingerprint"
    else:
        holds = ("the backup password; the certificate fingerprint is pending "
                 "on this machine")
    return ('<div class="recovery-file" id="recovery-file">\n'
            '    <a class="download" href="%s" download="%s">Download your recovery file</a>\n'
            '    <p class="note">It holds %s.</p>\n'
            '  </div>'
            % (html.escape(href, quote=True), html.escape(filename, quote=True),
               html.escape(holds)))


# --------------------------------------------------------------------- the env

def env_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def render_env(text: str, password_hash: str) -> str:
    """Replace the one hash line, keeping every other line and its order.

    A line is replaced in place; a file without the key gets it appended, so a
    hand-edited .env still converges. Comments are preserved because they are
    the installer's explanation of the allowlist.
    """
    lines = text.splitlines()
    replaced = False
    rendered: list[str] = []
    for line in lines:
        if line.startswith(PASSWORD_KEY + "="):
            rendered.append("%s=%s" % (PASSWORD_KEY, password_hash))
            replaced = True
        else:
            rendered.append(line)
    if not replaced:
        rendered.append("%s=%s" % (PASSWORD_KEY, password_hash))
    body = "\n".join(rendered)
    return body + "\n" if body and not body.endswith("\n") else body


def write_env_hash(env_path: Path, password_hash: str) -> str:
    """Write the hash into the tenant .env; return the username it names."""
    try:
        original = env_path.read_text(encoding="utf-8")
        stat = env_path.stat()
    except FileNotFoundError:
        raise SystemExit("error: %s does not exist; run the installer first" % env_path)
    except OSError as exc:
        raise SystemExit("error: cannot read %s: %s" % (env_path, exc))
    owner = (stat.st_uid, stat.st_gid)
    updated = render_env(original, password_hash)
    if updated != original:
        write_atomic(env_path, updated.encode("utf-8"), stat.st_mode & 0o777, owner)
    return env_values(updated).get(USERNAME_KEY) or DEFAULT_USERNAME


# ------------------------------------------------------------------- rendering

def page_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit("error: cannot read the page %s: %s" % (path, exc))


class FieldError(NamedTuple):
    """One message, the field it renders under, and the fields it marks."""

    message: str
    field: str
    invalid: tuple[str, ...]


def error_slot(message: str) -> str:
    return '<p class="error" id="error" role="alert">%s</p>' % html.escape(message)


def field_attrs(field: str, error: FieldError | None, hint: str = "") -> str:
    """aria-invalid and aria-describedby for one field, and nothing else."""
    described = [hint] if hint else []
    attributes: list[str] = []
    if error is not None and field in error.invalid:
        attributes.append('aria-invalid="true"')
        described.append("error")
    if described:
        attributes.append('aria-describedby="%s"' % " ".join(described))
    return "".join(" " + attribute for attribute in attributes)


def wordmark(keep: str) -> str:
    """A link only where the workspace is reachable; a span before the sign-in.

    ``refused-unknown`` means the state carries no recovery hash, so the
    sign-in was never set and opening ``/`` would only meet a password prompt
    no one can answer. It stays plain for the same reason ``form`` does.
    """
    if keep in ("form", "reset", "refused-unknown"):
        return '<span class="brand">Odile</span>'
    return '<a class="brand" href="/">Odile</a>'


def password_error(password: str, repeat: str) -> FieldError | None:
    """The message and the fields it concerns, at the field, not as a summary."""
    problem = password_problem(password)
    if problem is not None:
        return FieldError(problem, "password", ("password",))
    if password != repeat:
        return FieldError("The two passwords do not match.", "password2",
                          ("password", "password2"))
    return None


def render(template: str, keep: str, *, title: str, error: FieldError | None = None,
           token: str = "", username: str = DEFAULT_USERNAME,
           code: str = "", recovery: str = "", recovery_file: str = "") -> str:
    """Keep one state block, drop the rest, then fill the slots.

    Values go in last, so a literal ``{{TOKEN}}`` typed by a user is never
    read back as one of our slots. The recovery-code field is the one slot a
    user can fill with braces, so it goes in after the unfilled-slot check.
    ``recovery_file`` is this service's own HTML (a link or the unavailable
    sentence), never a user value.
    """
    def pick(match: re.Match[str]) -> str:
        return match.group(2) if match.group(1) == keep else ""

    body = BLOCK_RE.sub(pick, template)
    body = body.replace("{{BRAND}}", wordmark(keep))
    for field in ERROR_FIELDS:
        if error is not None and error.field == field:
            body = body.replace("<!--%s-error-->" % field, error_slot(error.message))
        else:
            body = body.replace("<!--%s-error-->" % field, "")
    body = body.replace("{{CODE_ATTRS}}", field_attrs("code", error))
    body = body.replace("{{PASSWORD_ATTRS}}", field_attrs("password", error, "password-hint"))
    body = body.replace("{{PASSWORD2_ATTRS}}", field_attrs("password2", error))
    body = body.replace("{{TITLE}}", html.escape(title))
    body = body.replace("{{RECOVERY}}", html.escape(recovery))
    body = body.replace("{{RECOVERY_FILE}}", recovery_file)
    body = body.replace("{{USERNAME}}", html.escape(username))
    body = body.replace("{{TOKEN}}", html.escape(token, quote=True))
    body = body.replace("{{CODE}}", CODE_SENTINEL)
    if "{{" in body:
        raise SystemExit("error: the page has an unfilled slot")
    body = body.replace(CODE_SENTINEL, html.escape(code, quote=True))
    return body


# ------------------------------------------------------ the machine's report
#
# app.py renders the same template on HQ; the two parsers and the two renders
# are kept identical byte for byte by tests/test_verify.py. The report file is
# written by root (`verify_and_unlock.sh --mode access --out`) and read here by
# a service that runs as root, but the page is public: every value is escaped
# and a file that is not the exact shape the script writes is "bad", never
# markup.

ACCESS_REPORT_MAX = 64 * 1024
ACCESS_TAGS = ("usable keys", "boot key", "seal", "sign-in hash", "operator key")
ACCESS_HEAD = "read from your machine, "
ACCESS_TITLE = "verify_and_unlock \u2014 access report"
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
HEX96_RE = re.compile(r"^[0-9a-f]{96}$")
VERIFY_BLOCK_RE = re.compile(
    r"<!--(demo|tenant|bench|machine|noaccess)-->(.*?)<!--/\1-->", re.S)


def access_report(path: Path) -> dict:
    """The machine's own access report, or why the page cannot show it.

    ``{"state": "ok", "at": <ISO>, "text": <the report>}``, ``{"state":
    "missing"}`` when the file does not exist, ``{"state": "bad"}`` when it
    does and is not what ``--mode access --out`` writes. A file that exists
    and does not parse is never silently replaced by the bench block.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            data = handle.read(ACCESS_REPORT_MAX + 1)
    except FileNotFoundError:
        return {"state": "missing"}
    except OSError:
        return {"state": "bad"}
    if len(data) > ACCESS_REPORT_MAX:
        return {"state": "bad"}
    lines = data.splitlines()
    if len(lines) != 10 or not lines[0].startswith(ACCESS_HEAD):
        return {"state": "bad"}
    at = lines[0][len(ACCESS_HEAD):]
    if not ISO_RE.match(at):
        return {"state": "bad"}
    if lines[1] != ACCESS_TITLE or lines[2] != "" or lines[8] != "":
        return {"state": "bad"}
    for index, tag in enumerate(ACCESS_TAGS):
        if not re.match(r"^  %s +(PASS|FAIL) +" % re.escape(tag), lines[3 + index]):
            return {"state": "bad"}
    if not re.match(r"^  (PASS|FAIL) \u2014 ", lines[9]):
        return {"state": "bad"}
    return {"state": "ok", "at": at, "text": "\n".join(lines[1:])}


def attestation_report(path: Path) -> dict | None:
    """The served attestation summary the page's report card shows, or None.

    The same shape and the same checks as app.py's ``cached_report``: the page
    only renders values attest.py could have written.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("available") is not True:
        return None
    measurement = data.get("measurement")
    fetched_at = data.get("fetched_at")
    version = data.get("report_version")
    size = data.get("report_size")
    if not (isinstance(measurement, str) and HEX96_RE.match(measurement)):
        return None
    if not (isinstance(fetched_at, str) and ISO_RE.match(fetched_at)):
        return None
    if not isinstance(version, int) or isinstance(version, bool):
        return None
    if not isinstance(size, int) or isinstance(size, bool):
        return None
    return {"measurement": measurement, "fetched_at": fetched_at,
            "meta": "version %d, %d bytes" % (version, size)}


def render_verify_page(template: str, report: dict, attestation: dict | None) -> str:
    """The tenant's /verify: the same template HQ serves, in its tenant state.

    One chrome block (tenant) and one access block (machine, or noaccess when
    the report is missing or malformed) survive; the demo's "Check it yourself"
    section and the 10 November bench block are removed. Values go in after the
    blocks are chosen, so a slot in a dropped block never reaches a browser.
    """
    if attestation is None:
        report_values = {
            "REPORT_STATE": "unavailable",
            "REPORT_LINE": "Report unavailable.",
            "REPORT_CAUSE": '<p class="note">The CPU has not produced a report yet. <a href="/verify">Try again</a> in a minute.</p>',
            "DEMO_NOTE": "",
            "FETCHED_AT": "unavailable",
            "MEASUREMENT": "unavailable",
            "REPORT_META": "unavailable",
        }
    else:
        report_values = {
            "REPORT_STATE": "available",
            "REPORT_LINE": "Report available.",
            "REPORT_CAUSE": "",
            "DEMO_NOTE": "",
            "FETCHED_AT": html.escape(attestation["fetched_at"]),
            "MEASUREMENT": html.escape(attestation["measurement"]),
            "REPORT_META": html.escape(attestation["meta"]),
        }
    if report["state"] == "ok":
        keep = {"tenant", "machine"}
        report_values["REPORT_AT"] = html.escape(report["at"])
        report_values["REPORT_TEXT"] = html.escape(report["text"])
        report_values["ACCESS_CAUSE"] = ""
    elif report["state"] == "bad":
        keep = {"tenant", "noaccess"}
        report_values["REPORT_AT"] = ""
        report_values["REPORT_TEXT"] = ""
        report_values["ACCESS_CAUSE"] = ("The report file on this machine is not "
                                        "in the shape the reporter writes. Run it again.")
    else:
        keep = {"tenant", "noaccess"}
        report_values["REPORT_AT"] = ""
        report_values["REPORT_TEXT"] = ""
        report_values["ACCESS_CAUSE"] = ("The machine has not written its access "
                                        "report yet. <a href=\"/verify\">Try again</a> in a minute.")
    body = VERIFY_BLOCK_RE.sub(
        lambda match: match.group(2) if match.group(1) in keep else "", template)
    body = body.replace("{{TITLE}}", html.escape("Verify \u2014 Odile"))
    body = body.replace("{{ROBOTS}}", "noindex")
    for name, value in report_values.items():
        body = body.replace("{{%s}}" % name, value)
    return body


# ---------------------------------------------------------------------- server

class FirstVisit:
    """The configuration one server instance serves from."""

    def __init__(self, state: Path, env: Path, page: Path, recovery: Path,
                 reload_command: list[str] | None, bind: str, port: int,
                 caddy_stores: Sequence[Path] = (),
                 verify_page: Path = Path(DEFAULT_VERIFY_PAGE),
                 report: Path = Path(DEFAULT_REPORT),
                 attest: Path = Path(DEFAULT_ATTEST),
                 report_command: list[str] | None = None):
        self.state = state
        self.env = env
        self.page = page
        self.recovery = recovery
        self.reload_command = reload_command
        self.bind = bind
        self.port = port
        self.caddy_stores = tuple(caddy_stores)
        self.verify_page = verify_page
        self.report = report
        self.attest = attest
        self.report_command = report_command or []

    # -- helpers ---------------------------------------------------------

    def template(self) -> str:
        return page_text(self.page)

    def username(self) -> str:
        try:
            values = env_values(self.env.read_text(encoding="utf-8"))
        except OSError:
            return DEFAULT_USERNAME
        return values.get(USERNAME_KEY) or DEFAULT_USERNAME

    def reload(self) -> None:
        """Restart the gateway so it reads the new hash; no secret in argv."""
        if not self.reload_command:
            return
        try:
            result = subprocess.run(
                self.reload_command, capture_output=True, text=True, timeout=120,
                env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                     "LANG": "C.UTF-8"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("the agent did not restart: %s" % exc)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise RuntimeError("the agent did not restart (%d): %s"
                               % (result.returncode, detail[-1] if detail else "no output"))
        print("first-visit: agent restarted", flush=True)

    def complete_recovery(self, host: str, secure: bool) -> RecoveryFile | None:
        return complete_recovery(self.recovery, host, secure, self.caddy_stores)

    def filename(self, recovery: RecoveryFile) -> str:
        return recovery_filename(recovery)

    # -- responses -------------------------------------------------------

    def done(self, keep: str, title: str, code: str, username: str,
             host: str, secure: bool) -> tuple[int, str]:
        """The one page that shows the code, and the file it unlocks."""
        recovery = self.complete_recovery(host, secure)
        body = render(self.template(), keep, title=title, recovery=code,
                      username=username,
                      recovery_file=recovery_file_block(recovery, code))
        return 200, body

    def refused(self, code: int, can_reset: bool = True) -> tuple[int, str]:
        """The closed-link page; the reset link only where it can render.

        ``/first-visit/reset`` answers 409 when the state has no recovery
        hash, which is exactly the machine whose sign-in was never set. On
        that machine the page names the ready email and the support address
        instead of offering a second dead end (ODL-471 follow-up).
        """
        keep = "refused" if can_reset else "refused-unknown"
        body = render(self.template(), keep, title="This link is closed — Odile")
        return code, body

    def form(self, token: str, error: FieldError | None = None) -> tuple[int, str]:
        body = render(self.template(), "form", title="Set your sign-in — Odile",
                      error=error, token=token, username=self.username())
        return 200, body

    def reset_form(self, error: FieldError | None = None, code: str = "") -> tuple[int, str]:
        body = render(self.template(), "reset", title="Reset your sign-in — Odile",
                      error=error, username=self.username(), code=code)
        return 200, body

    # -- /verify (ODL-473) -----------------------------------------------

    def verify(self) -> tuple[int, str]:
        """The machine's own access report, rendered on the tenant's page.

        A missing report file is a page state, not an error: the boot oneshot
        may still be running, and the page says so and offers a retry. The
        template is read per request so a re-installed page is served without a
        restart; a missing template is a 500, never a half page.
        """
        try:
            template = page_text(self.verify_page)
        except SystemExit:
            return 500, "The verify page is not installed on this machine.\n"
        return 200, render_verify_page(template, access_report(self.report),
                                       attestation_report(self.attest))

    def refresh_report(self) -> None:
        """Ask the boot oneshot to rewrite the report after the sign-in is set.

        The sign-in line would otherwise name the placeholder .env forever.
        Best effort: a machine whose systemd says no keeps its old report and
        the next boot rewrites it; the POST has already succeeded and must not
        be failed by a background refresh.
        """
        if not self.report_command:
            return
        try:
            done = subprocess.run(
                self.report_command, capture_output=True, text=True, timeout=30,
                env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                     "LANG": "C.UTF-8"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print("first-visit: the access report did not refresh: %s" % exc,
                  file=sys.stderr, flush=True)
            return
        if done.returncode != 0:
            detail = (done.stderr or done.stdout).strip().splitlines()
            print("first-visit: the access report did not refresh (%d): %s"
                  % (done.returncode, detail[-1] if detail else "no output"),
                  file=sys.stderr, flush=True)
            return
        print("first-visit: the access report refresh requested", flush=True)


def check_token(state: dict, token: str) -> str:
    """'ok', 'used' or 'unknown' — never the reason a token looks wrong."""
    if not token or not TOKEN_RE.match(token):
        return "unknown"
    if not secrets.compare_digest(hash_token(token), state.get("token_sha256", "")):
        return "unknown"
    return "used" if state.get("used_at") else "ok"


def allow(ip: str, *, window: int = RATE_WINDOW, limit: int = RATE_LIMIT,
          bucket: str = "") -> bool:
    """One in-memory bucket per client and route; the IP is hashed and never written."""
    now = time.time()
    key = hashlib.sha256((bucket + "\x00" + ip).encode("utf-8", "replace")).hexdigest()[:16]
    with _lock:
        recent = [stamp for stamp in _hits.get(key, []) if now - stamp < window]
        if len(recent) >= limit:
            _hits[key] = recent
            return False
        recent.append(now)
        _hits[key] = recent
        if len(_hits) > 512:
            for stale in [name for name, stamps in _hits.items()
                          if not stamps or now - stamps[-1] > window]:
                _hits.pop(stale, None)
    return True


def build_handler(service: FirstVisit):
    class Handler(BaseHTTPRequestHandler):
        server_version = "odile-first-visit"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = 20

        def log_message(self, format, *args):
            pass  # one journal line per state change; no access log

        def send_response(self, code, message=None):
            self.log_request(code)
            self.send_response_only(code, message)
            self.send_header("Date", self.date_time_string())

        def _send(self, code: int, body: str, head: bool = False,
                  ctype: str = "text/html; charset=utf-8", close: bool = False,
                  filename: str = "") -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if filename:
                self.send_header("Content-Disposition",
                                 'attachment; filename="%s"' % filename)
            if close:
                # An unread request body would become the next request line
                # (RFC 9112 §9.3); a refusal that cannot drain it ends the
                # connection instead of corrupting the next one.
                self.send_header("Connection", "close")
                self.close_connection = True
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=(), interest-cohort=()",
            )
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
                "font-src data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def do_HEAD(self):
            self._get_path(head=True)

        def do_GET(self):
            self._get_path(head=False)

        # -- GET ---------------------------------------------------------

        def _load_state(self) -> dict | None:
            """The state, or a 500 when it is missing or malformed.

            ``load_state`` raises ``SystemExit`` for the CLI's sake; in a
            request handler that would end the thread with no answer at all.
            """
            try:
                return load_state(service.state)
            except SystemExit as exc:
                print("first-visit: %s" % exc, file=sys.stderr, flush=True)
                self._send(500, "The sign-in page is not configured on this machine.\n",
                           ctype="text/plain; charset=utf-8")
                return None

        def _host(self) -> str:
            return request_host(self.headers.get("Host", ""))

        def _secure(self) -> bool:
            """Only the protocol Caddy says it terminated counts, never a guess."""
            proto = self.headers.get("X-Forwarded-Proto", "")
            return proto.split(",")[0].strip().lower() == "https"

        def _recovery_download(self, head: bool) -> None:
            state = self._load_state()
            if state is None:
                return
            if not allow(self.client_address[0], bucket="recovery"):
                return self._send(429, "Too many attempts. Try again in ten minutes.\n",
                                  head, "text/plain; charset=utf-8")
            raw = (parse_qs(urlparse(self.path).query, keep_blank_values=True)
                   .get("c") or [""])[0]
            if not recovery_code_ok(state, raw):
                return self._send(404, "The recovery file is not available on this machine.\n",
                                  head, "text/plain; charset=utf-8")
            recovery = service.complete_recovery(self._host(), self._secure())
            if recovery is None:
                return self._send(404, "The recovery file is not available on this machine.\n",
                                  head, "text/plain; charset=utf-8")
            return self._send(200, recovery.text, head, "text/plain; charset=utf-8",
                              filename=service.filename(recovery))

        def _get_path(self, head: bool) -> None:
            url = urlparse(self.path)
            path = url.path
            query = parse_qs(url.query, keep_blank_values=True)
            if path == "/health":
                return self._send(200, "ok\n", head, "text/plain; charset=utf-8")
            if path in ("/verify", "/verify/"):
                code, body = service.verify()
                return self._send(code, body, head)
            if path in ("/first-visit/recovery", "/first-visit/recovery/"):
                return self._recovery_download(head)
            if path in ("/first-visit", "/first-visit/"):
                token = (query.get("t") or [""])[0]
                state = self._load_state()
                if state is None:
                    return
                verdict = check_token(state, token)
                if verdict == "used":
                    code, body = service.refused(410, bool(state.get("recovery_hash")))
                    return self._send(code, body, head)
                if verdict == "unknown":
                    code, body = service.refused(404, bool(state.get("recovery_hash")))
                    return self._send(code, body, head)
                code, body = service.form(token)
                return self._send(code, body, head)
            if path in ("/first-visit/reset", "/first-visit/reset/"):
                state = self._load_state()
                if state is None:
                    return
                if not state.get("recovery_hash"):
                    code, body = service.refused(409, False)
                    return self._send(code, body, head)
                code, body = service.reset_form()
                return self._send(code, body, head)
            return self._send(404, "not found\n", head, "text/plain; charset=utf-8")

        # -- POST --------------------------------------------------------

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if path not in ("/first-visit", "/first-visit/reset"):
                # Drain a small body so the next request on this connection
                # starts where it should; a large one is not worth reading.
                if 0 < length <= MAX_BODY:
                    self.rfile.read(length)
                return self._send(405, "method not allowed\n",
                                  ctype="text/plain; charset=utf-8", close=length > MAX_BODY)
            if length <= 0 or length > MAX_BODY:
                return self._send(413, "That request was too large.\n",
                                  ctype="text/plain; charset=utf-8", close=True)
            raw = self.rfile.read(length).decode("utf-8", "replace")
            fields = parse_qs(raw, keep_blank_values=True)
            if not allow(self.client_address[0]):
                return self._send(429, "Too many attempts. Try again in ten minutes.\n",
                                  ctype="text/plain; charset=utf-8")
            if path == "/first-visit":
                return self._set_signin(fields)
            return self._reset_signin(fields)

        def _set_signin(self, fields: dict[str, list[str]]) -> None:
            token = (fields.get("t") or [""])[0].strip()
            password = (fields.get("password") or [""])[0]
            repeat = (fields.get("password2") or [""])[0]
            with locked(service.state):
                state = self._load_state()
                if state is None:
                    return
                verdict = check_token(state, token)
                if verdict == "used":
                    code, body = service.refused(410, bool(state.get("recovery_hash")))
                    return self._send(code, body)
                if verdict == "unknown":
                    code, body = service.refused(404, bool(state.get("recovery_hash")))
                    return self._send(code, body)
                problem = password_error(password, repeat)
                if problem is not None:
                    code, body = service.form(token, problem)
                    return self._send(code, body)

                password_hash = hash_password(password)
                try:
                    username = write_env_hash(service.env, password_hash)
                    service.reload()
                except (SystemExit, RuntimeError) as exc:
                    print("first-visit: refused: %s" % exc, file=sys.stderr, flush=True)
                    return self._send(500, "The sign-in was not set. Try again.\n",
                                      ctype="text/plain; charset=utf-8")

                code_value = new_recovery_code()
                state["used_at"] = utc_now()
                state["signin_set_at"] = state["used_at"]
                state["recovery_hash"] = hash_password(normalise_code(code_value))
                state["recovery_issued_at"] = state["used_at"]
                save_state(service.state, state)
            print("first-visit: sign-in set for %r (token %s… spent)"
                  % (username, state["token_sha256"][:8]), flush=True)
            # The report's sign-in line describes the .env this POST just
            # replaced; ask the boot oneshot to rewrite it (ODL-473).
            service.refresh_report()
            # The recovery code exists only in this response: no-store, and the
            # state keeps its scrypt hash, never the code.
            code, body = service.done("done", "Your sign-in is set — Odile",
                                      code_value, username, self._host(), self._secure())
            return self._send(code, body)

        def _reset_signin(self, fields: dict[str, list[str]]) -> None:
            code_value = (fields.get("code") or [""])[0]
            password = (fields.get("password") or [""])[0]
            repeat = (fields.get("password2") or [""])[0]
            normalised = normalise_code(code_value)
            with locked(service.state):
                state = self._load_state()
                if state is None:
                    return
                stored = state.get("recovery_hash")
                if not isinstance(stored, str) or not stored.startswith("scrypt$"):
                    code, body = service.refused(409, False)
                    return self._send(code, body)
                if not CODE_RE.match(normalised) or not verify_password(normalised, stored):
                    code, body = service.reset_form(
                        FieldError("That recovery code does not match.", "code", ("code",)),
                        code_value)
                    return self._send(code, body)
                problem = password_error(password, repeat)
                if problem is not None:
                    code, body = service.reset_form(problem, code_value)
                    return self._send(code, body)

                password_hash = hash_password(password)
                try:
                    username = write_env_hash(service.env, password_hash)
                    service.reload()
                except (SystemExit, RuntimeError) as exc:
                    print("first-visit: refused: %s" % exc, file=sys.stderr, flush=True)
                    return self._send(500, "The sign-in was not reset. Try again.\n",
                                      ctype="text/plain; charset=utf-8")

                new_code = new_recovery_code()
                state["recovery_hash"] = hash_password(normalise_code(new_code))
                state["recovery_issued_at"] = utc_now()
                state["recovery_used_at"] = state["recovery_issued_at"]
                save_state(service.state, state)
            print("first-visit: sign-in reset with the recovery code; the code rotated", flush=True)
            code, body = service.done("reset-done", "Your sign-in is reset — Odile",
                                      new_code, username, self._host(), self._secure())
            return self._send(code, body)

    return Handler


def serve(service: FirstVisit) -> int:
    server = ThreadingHTTPServer((service.bind, service.port), build_handler(service))
    server.daemon_threads = True
    print("first-visit on %s:%d · state %s · env %s"
          % (service.bind, service.port, service.state, service.env), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# ------------------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Set the Odile sign-in at first visit.")
    parser.add_argument("--bind", default=DEFAULT_BIND)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--env-file", default=DEFAULT_ENV)
    parser.add_argument("--page", default=DEFAULT_PAGE)
    parser.add_argument("--recovery-file", default=DEFAULT_RECOVERY,
                        help="the recovery file the done page offers as a download")
    parser.add_argument("--caddy-store", action="append", default=None,
                        help="Caddy certificate store root; repeatable "
                             "(default: the two tenant roots)")
    parser.add_argument("--reload-command", default=DEFAULT_RELOAD,
                        help="command run after a hash is written; empty disables it")
    parser.add_argument("--verify-page", default=DEFAULT_VERIFY_PAGE,
                        help="the /verify template (default %s)" % DEFAULT_VERIFY_PAGE)
    parser.add_argument("--report-file", default=DEFAULT_REPORT,
                        help="the access report the boot oneshot writes (default %s)"
                             % DEFAULT_REPORT)
    parser.add_argument("--attest-file", default=DEFAULT_ATTEST,
                        help="the served attestation summary (default %s)" % DEFAULT_ATTEST)
    parser.add_argument("--report-command", default=DEFAULT_REPORT_COMMAND,
                        help="command run after a sign-in is set, to rewrite the "
                             "access report; empty disables it")
    parser.add_argument("--print-state", action="store_true",
                        help="print a secret-free summary of the state file and exit")
    return parser


def summary(state: dict) -> dict:
    return {
        "version": state["version"],
        "token_sha256": state["token_sha256"][:16] + "…",
        "created_at": state["created_at"],
        "used_at": state["used_at"],
        "signin_set_at": state["signin_set_at"],
        "recovery_hash": ("scrypt…" if state.get("recovery_hash") else None),
        "recovery_issued_at": state["recovery_issued_at"],
        "recovery_used_at": state["recovery_used_at"],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_path = Path(args.state)
    if args.print_state:
        print(json.dumps(summary(load_state(state_path)), indent=2))
        return 0
    reload_command = shlex.split(args.reload_command) if args.reload_command else []
    report_command = shlex.split(args.report_command) if args.report_command else []
    stores = (tuple(Path(raw) for raw in args.caddy_store) if args.caddy_store
              else tuple(Path(raw) for raw in DEFAULT_CADDY_STORES))
    service = FirstVisit(
        state=state_path,
        env=Path(args.env_file),
        page=Path(args.page),
        recovery=Path(args.recovery_file),
        reload_command=reload_command,
        bind=args.bind,
        port=args.port,
        caddy_stores=stores,
        verify_page=Path(args.verify_page),
        report=Path(args.report_file),
        attest=Path(args.attest_file),
        report_command=report_command,
    )
    page_text(service.page)  # fail before binding, not on the first visit
    # A machine where ODL-473 has not landed has no verify page; /verify then
    # answers 500 with one line instead of stopping the page that sets the
    # sign-in. The template is checked per request, so the first boot can
    # install it after this unit started.
    load_state(service.state)
    return serve(service)


if __name__ == "__main__":
    sys.exit(main())
'''
FIRST_VISIT_PAGE_SHA256 = "4d7c4960a419d708f1b865fa55728f4927185c36428c316e32ed2465cf347bfb"
FIRST_VISIT_PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- Icon: Lucas Cranach the Elder, Saints Christina and Ottilia (Katharinenaltar, Dresden). Public domain. Source: Wikimedia Commons. -->
<link rel="icon" href="/favicon.ico" sizes="16x16 32x32 48x48">
<link rel="icon" type="image/png" sizes="32x32" href="/icon-32.png">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<title>{{TITLE}}</title>
<meta name="description" content="Set the Odile sign-in on this machine.">
<meta name="color-scheme" content="light">
<meta name="robots" content="noindex">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; font-src data:; form-action 'self'; base-uri 'none'">
<style>
@font-face{font-family:"Cormorant Garamond";font-style:normal;font-weight:300 700;font-display:swap;src:url(data:font/woff2;base64,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) format("woff2")}
*{box-sizing:border-box}
:root{--paper:#eadfc4;--card:#f2e9d4;--panel:#ddd0ae;--ink:#1c1813;--soft:#4a4136;--oxblood:#6e2a2a;
  --edge:rgba(28,24,19,.55);--ink-hover:#2a241c;--line:rgba(28,24,19,.18);--line-soft:rgba(28,24,19,.10);
  --error:#6e2a2a}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);
  font:400 18px/1.55 "Cormorant Garamond","Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
::selection{background:#1c1813;color:#eadfc4}
main{max-width:720px;margin:0 auto;padding:32px 24px 64px}
.top{margin:0 0 24px}
.brand{display:inline-block;padding:12px 0;margin:-12px 0;color:var(--ink);text-decoration:none;
  font-size:2rem;font-weight:500;letter-spacing:-.03em;line-height:.88}
.brand:focus-visible{outline:2px solid var(--ink);outline-offset:3px}
h1{margin:0 0 16px;font-size:clamp(2.5rem,5vw,4.1rem);line-height:1.02;letter-spacing:-.025em;font-weight:500}
p{margin:0 0 16px;max-width:60ch}
a{color:var(--ink);text-decoration-color:var(--soft);text-underline-offset:2px}
a:focus-visible{outline:2px solid var(--ink);outline-offset:3px}
.lede{margin:0 0 24px;font-size:1.11rem;line-height:1.5;color:var(--soft);max-width:60ch}
.label{display:block;margin:0 0 8px;
  font:600 10px/1 "Cormorant Garamond","Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  text-transform:uppercase;letter-spacing:.24em;color:var(--oxblood)}
.note{margin:0;font-size:.92rem;line-height:1.5;color:var(--soft);max-width:54ch}
.field{margin:0 0 16px}
.field label{display:block;margin:0 0 8px;font-weight:600}
.field .hint{margin:0 0 8px;font-size:.9rem;color:var(--soft)}
input[type=text],input[type=password]{display:block;width:100%;min-height:44px;padding:9px 2px 11px;
  background:transparent;color:var(--ink);border:0;border-bottom:1px solid var(--edge);border-radius:0;
  font:400 1.05rem/1.4 ui-monospace,"SF Mono","Cascadia Mono","Liberation Mono",Menlo,Consolas,monospace}
input[type=text]:focus,input[type=password]:focus{outline:2px solid var(--ink);outline-offset:3px;
  border-bottom-color:var(--ink)}
input[aria-invalid=true]{border-bottom-color:var(--error)}
.error{margin:8px 0 0;font-size:.94rem;font-style:italic;line-height:1.4;color:var(--error)}
.fixed{margin:0 0 16px;font-family:ui-monospace,"SF Mono","Cascadia Mono","Liberation Mono",Menlo,Consolas,monospace;
  font-size:1rem;overflow-wrap:anywhere}
.primary{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:0 24px;
  background:var(--ink);color:var(--paper);border:1px solid var(--ink);border-radius:0;cursor:pointer;
  font:600 10px/1 "Cormorant Garamond","Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  text-transform:uppercase;letter-spacing:.28em;transition:background .18s}
.primary:hover{background:var(--ink-hover);border-color:var(--ink-hover)}
.primary:focus-visible{outline:2px solid var(--ink);outline-offset:3px}
.quiet{display:inline-flex;align-items:center;min-height:44px;margin-right:24px;color:var(--ink);
  text-decoration:none;border-bottom:1px solid var(--edge);
  font:600 10px/1 "Cormorant Garamond","Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  text-transform:uppercase;letter-spacing:.20em}
.quiet:hover{border-bottom-color:var(--ink)}
.quiet:focus-visible{outline:2px solid var(--ink);outline-offset:3px}
.act{display:flex;flex-wrap:wrap;align-items:center;gap:8px 24px;margin:24px 0 0}
.code{margin:0 0 12px;padding:16px;background:var(--panel);border:1px solid var(--line);
  font-family:ui-monospace,"SF Mono","Cascadia Mono","Liberation Mono",Menlo,Consolas,monospace;
  font-size:clamp(1.05rem,4.2vw,1.35rem);line-height:1.5;letter-spacing:.06em;overflow-wrap:anywhere;
  -webkit-user-select:all;user-select:all}
.recovery-file{margin:0 0 16px}
.download{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:0 24px;
  margin:0 0 8px;background:var(--ink);border:1px solid var(--ink);color:var(--paper);text-decoration:none;
  font:600 10px/1 "Cormorant Garamond","Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  text-transform:uppercase;letter-spacing:.28em;transition:background .18s,color .18s}
.download:hover{background:var(--ink-hover);border-color:var(--ink-hover);color:var(--paper)}
.download:focus-visible{outline:2px solid var(--ink);outline-offset:3px}
section{margin:0 0 8px}
.rule{margin:24px 0 0;padding-top:16px;border-top:1px solid var(--line)}
.foot{margin:32px 0 0;padding-top:16px;border-top:1px solid var(--line-soft);
  font-size:.92rem;color:var(--soft);max-width:56ch}
@media (max-width:560px){
  body{font-size:17px}
  main{padding:24px 18px 48px}
  .act{display:block;margin-top:24px}
  .primary{width:100%}
  .quiet{margin:10px 0 0}
  .download{width:100%}
  .code{padding:12px}
}
@media (max-width:400px){
  .code{font-size:1.02rem;letter-spacing:.02em}
}
@media (prefers-reduced-motion:reduce){.primary{transition:none}.download{transition:none}}
</style>
</head>
<body>
<main>
  <!-- A7 and A9 of the decided strict-bar deck (docs/product/CLAIMS-RECUT-STRICT.md);
       ODL-462 lands the deck in docs/CLAIMS.md. -->
  <header class="top">{{BRAND}}</header>
<!--form-->
  <h1>Set your sign-in</h1>
  <p class="lede">This is the first visit. Choose the password that opens your Odile workspace.</p>
  <form method="post" action="/first-visit">
    <input type="hidden" name="t" value="{{TOKEN}}">
    <p class="fixed"><span class="label">Sign-in name</span>{{USERNAME}}</p>
    <div class="field">
      <label for="password">Password</label>
      <p class="hint" id="password-hint">At least 16 characters.</p>
      <input type="password" id="password" name="password" autocomplete="new-password"
             minlength="16" required{{PASSWORD_ATTRS}}>
      <!--password-error-->
    </div>
    <div class="field">
      <label for="password2">Repeat it</label>
      <input type="password" id="password2" name="password2" autocomplete="new-password"
             minlength="16" required{{PASSWORD2_ATTRS}}>
      <!--password2-error-->
    </div>
    <button class="primary" type="submit">Set the sign-in</button>
  </form>
  <p class="foot">Your Odile sign-in is set on your machine at first visit and hashed there. We never see it. Your recovery code comes next; keep it somewhere off this machine.</p>
<!--/form-->
<!--done-->
  <h1>Your sign-in is set.</h1>
  <p class="lede">The workspace is open. This page will not open again.</p>
  <section aria-labelledby="recovery">
    <p class="label" id="recovery">Your recovery code</p>
    <p class="code" id="code">{{RECOVERY}}</p>
    <p>This is the only time the code is shown.</p>
    {{RECOVERY_FILE}}
    <p class="note">A recovery code and a recovery file are the way back: the code resets your sign-in, the file opens your backup. Your first visit gives you both; keep them off the machine. While the machine is still running, the agent can show it again.</p>
  </section>
  <div class="act">
    <a class="quiet" href="/">Open your workspace</a>
  </div>
<!--/done-->
<!--reset-->
  <h1>Reset your sign-in</h1>
  <p class="lede">Enter the recovery code you saved, then choose a new password.</p>
  <form method="post" action="/first-visit/reset">
    <div class="field">
      <label for="code">Recovery code</label>
      <input type="text" id="code" name="code" autocomplete="off" autocapitalize="characters"
             spellcheck="false" required value="{{CODE}}"{{CODE_ATTRS}}>
      <!--code-error-->
    </div>
    <div class="field">
      <label for="password">New password</label>
      <p class="hint" id="password-hint">At least 16 characters.</p>
      <input type="password" id="password" name="password" autocomplete="new-password"
             minlength="16" required{{PASSWORD_ATTRS}}>
      <!--password-error-->
    </div>
    <div class="field">
      <label for="password2">Repeat it</label>
      <input type="password" id="password2" name="password2" autocomplete="new-password"
             minlength="16" required{{PASSWORD2_ATTRS}}>
      <!--password2-error-->
    </div>
    <button class="primary" type="submit">Set a new password</button>
  </form>
  <p class="foot">The code you used stops working; a new one is shown next.</p>
<!--/reset-->
<!--reset-done-->
  <h1>Your sign-in is reset.</h1>
  <p class="lede">The old password no longer opens the workspace.</p>
  <section aria-labelledby="recovery">
    <p class="label" id="recovery">Your new recovery code</p>
    <p class="code" id="code">{{RECOVERY}}</p>
    <p>This is the only time the new code is shown. The code you saved before no longer works.</p>
    {{RECOVERY_FILE}}
    <p class="note">A recovery code and a recovery file are the way back: the code resets your sign-in, the file opens your backup. Your first visit gives you both; keep them off the machine. While the machine is still running, the agent can show it again.</p>
  </section>
  <div class="act">
    <a class="quiet" href="/">Open your workspace</a>
  </div>
<!--/reset-done-->
<!--refused-->
  <h1>This link is closed.</h1>
  <p class="lede">The sign-in was already set, or the link is not valid.</p>
  <div class="act">
    <a class="primary" href="/">Open your workspace</a>
    <a class="quiet" href="/first-visit/reset">Reset with a recovery code</a>
  </div>
<!--/refused-->
<!--refused-unknown-->
  <!-- Offered when the state has no recovery hash: the reset route answers 409
       then, so linking it would be a second dead end (ODL-471 follow-up). -->
  <h1>This link is closed.</h1>
  <p class="lede">The link is not valid, and no recovery code has been issued on this machine yet.</p>
  <section aria-labelledby="what-next">
    <p class="label" id="what-next">What next</p>
    <p>Open the link in your ready email again — it carries this machine's first-visit address. If it still does not work, write to help@odile.fyi.</p>
  </section>
<!--/refused-unknown-->
</main>
</body>
</html>
'''
# END FIRST VISIT (ODL-457)

# BEGIN VERIFY SURFACE (ODL-473)
# Generated by installer/embed_verify.py; do not edit by hand.
# The /verify page and the two programs that write and read its report
# are the reviewed files, byte for byte, so the signed installer carries
# exactly the surface it writes to /usr/local/lib/odile/. --self-test
# fails on a one-byte edit. The handoff audit (ODL-208) is embedded the
# same way and installed at /usr/local/bin/odile-audit.
VERIFY_PAGE_SHA256 = "8b1a3198a6c416845dc54407d21506c48eaecf1f93c8df32c5d7941eb20019c3"
VERIFY_PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- Icon: Lucas Cranach the Elder, Saints Christina and Ottilia (Katharinenaltar, Dresden). Public domain. Source: Wikimedia Commons. -->
<link rel="icon" href="/favicon.ico" sizes="16x16 32x32 48x48">
<link rel="icon" type="image/png" sizes="32x32" href="/icon-32.png">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<title>{{TITLE}}</title>
<meta name="color-scheme" content="light">
<meta name="robots" content="{{ROBOTS}}">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; font-src data:; base-uri 'none'">
<style>
@font-face{font-family:"Cormorant Garamond";font-style:normal;font-weight:300 700;font-display:swap;src:url(data:font/woff2;base64,d09GMgABAAAAAFmYABQAAAAA5xgAAFkmAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGoRFG6NKHItqP0hWQVKCEAZgP1NUQVSBACciAIIiL1YRCAqB6ASBuH4LgmwAMIGXBgE2AiQDhVQEIAWHRAcgDAcbx8xlRFZ6PRRR0erhjNSkHjJVtUyIt1cNiF9+++Ovf/77Z+C4Huu84aGns3q/naQ76AG4sDJYKzNLOXuSlpq7qspp1T/9tv7PIOoyAzquYCRGg7p7776ovFWB7xNuRxiv86/C+XLqN/M/S82nla0SOyV0l+kmeeyowN63Pe0JcLTLM9eRRnZaNiWyZYdNFGBvI0v3Pf0BmtP/kwTLESJYEpLcRewid0mIEKKCxCBBpaKUitJ2XcXoNrp5oSa/dKK8X5uXqbN5nZkOsandHURJ2kAUEiIQsYX6vC4zbScu8t28or+hb2r1RV3drO4WkS0Z48AOYDKbI3yE6JacZLK78cwD4i8AGMoMJ3Ec27It2bKgicp/ws3+z53JJEACVIKUtL50RUQp/S7yRIQKld1FLPY/Os1Vyj8i3fFWxGELj0Mn+0uWI38rfmdKZcUhcg4T9oEwkemYpyJNTZ3jqQxjNx4Zl7XD7tvyOrVbu2z9e1PN9r9VAi6SFyldiJwBHS9Nz+NQoGOuUqyuIt8uIOMvQEq7DMcFqLAMPi2ON+JCcaEAggIIiSAo6lJK5EJxFbnKFJ0gyjHVEh1i6FyF2FJXUedYlHbVugy56NxXrgpXru2/w1Q/SPB2273cy7IoY/QPIfblIPpz2ukxpv57ETpm3to2TpbKFoTUpO2fx3JWaeTSU85elVVJEG9gIMyq0XCarynTmhI5yha1GsmEfAhksRGw+fImSzaFRIlKMUh0F81FrjD4i+94hzB7WcpsJjKEcrrSlsZI24Z0MsD5WtELCYGCLF4WjZ/iQiRP4skkYlEDqjbehgYYC3FPHpd/B9uRsNGQpaEt60+WRS06ImHaNm7OcWSRRusJd4nCPSewXkMoYBNBczAp/YV1iQ64CtH1k5lugxLwUaFyugPgJgTiU0hPn4osAU9mtGs8xZopMdWGZuSl7BmDIA3BaqdEJi2VwK0SJKiUPvSB0p/+RhnGMKWMZNTPoRVUIJjIxHbodKYjmMWcH8oiFmllKct+vn1rWINgPRsQbGITgq1sRXCUYwhOcgqBAAruOZOYZyzg9p37f0b+fvhESLKiarrh9piW98/UlrR8+03ATU1aorRt37WUxOGzJodIHD155HgSQ0OnVpKIslVxHL+1xfiRkysxSo0frylJfjSk9mwCA0nUpC2IN8SIZpe56KoaY5ZzJ+JcGNJ8FCq6LHe8O8fG/iK9d3zgV8Ugymz+sdlK58CyfG715gpiCFJGH4Yxgu3sYA972cd+DnKIwxzhNGc4yznCnG9I2sXIuys9HZVSStEoowydcsrRdq6eGvOZj0E0lsxOdqGwl71AyrFkDnAAhYMcRvIRjiBzmvMfBuRRJJGRSkPKLtqpMIIRWtCde9iDrqV9gP0aAocoghlm11voLbWySSFphhPZrpXKJH+7d5UVb0t7gW2TMFPd4EQsG8ewTCdCZewjXdPdd6xU5yHl1k5VuDZsu5aZh2Eh8ZBfG5iqDZx3LM15wTKntrqIcxWeDA/CuWC1RgX4/+d8ZJlOmA55MoRhlRYcIs63XyfIa++8LeedeWPjVNMyB9QZsri1LUB8uJniy2Bz3EkzlypXmgs42rlmaTCbnx1wIiVlGKZF9prW0vM1vJku5GpB1Vi2mhyXzsyx9qH5UFqEUhl14+V1c0dojoyYD6mhvE7YMp2PzIizw0pN1ZOT8YGPORl2DstQ2HmhBrzznMUOKRTDUdaE8WMyq5ywedt5LU5pd5C6OkzrqTlRY5j/Qvp65ZEn3s/5uW+6qjT8377wqWvlybS0LZW7OKcRJ5KTI0yFg7dm04WVMyzJ6V4rVStSac1mtwwsuc2Vw07ZsNtZ52Gcw/LQvBqmxz4emH+kmcHCQd8yf9KWwy13BXfosjVX6Io4VXW5CS+E6bXZ/PEyiHXnW1PnbuWMkzB9mEWObEnoiKQ3YqVgL95oy7pJ+INmmc7P5u24zmvyOcE+SsmLf8d98xsaNw7OuWPOknjoNcnpP21G6P5BcdY/J3EWnZBQ0C8oX44bCy8+AthEEU0MsSSQSAqp591/RjPJIpdCqlGdGtSkFrWpQz3q04BGNKcFLWlFG9rRkS50oxfByy5WVyHqHgzwgAyACRbYEAAFBKisNrZEbSwsBD58jerHn1EigIm98IGZSZ42XwxRxBKLm7ieTWxSSGKJRkqFsNzAmi7c3CCLbFRyOYM8LoaCngkUUozBiCS3nTVQqak4mVqcSm1Opk7PAHWph8yV4qIhOJlGXCKNOZkmXAxNuQSacSrNOS8tSF1SUCx6gaxLSYq+RIVYiIdYigX5L07BBg38YIMGflanGqsW50tAQSERHR0bo8eCYF0uUDx74AMBFsQpJgkgG4pECjgv6ZyPDFLAyi5QAIpCIUhnjFVEriAJoqVY4wJ4yM0SudkADwTeHj+taIVMG9og0452KHSkIzpd6IJCN7qhMwZ+imKGYi60KRmsqu5Du9ymGIAKMbK2inPpU3EaGf6gP9Zv3pJovkBv1a9dOs+1aVWazt42wdJ0lndp1S+did27tk2nX6ltadvSYBeqvtJTZHzmSMgoeDCxfpTtLttVtlG2XrYGqrgO5dStaN4e7/ChoakcHVE5oYL9oyYPHc7O0NjRQ9kcqpxWwdrQhOEhlt/DQpgNU6ESxgwbVjmtYjIDpgZQOT1ZClyggCyr0REgQMPOBTDBBg/rxnWbVX6ilOL+1bjlT+ST8kq5XK4rfSAdloKiqcgUEhHyI0mGSbyGzMnCxfi4mIjF9F5YzWJmM5lxDKMfQbrSnpY0pi7VySeTnkgU0QOJPLojUZduSBTSFYkCuiCRuwSr5tBqqbKqM3qQQ106DQuK6LigkA4LCmi/II92C3JpSznF7IKcuXRLGZRsOgV4UgPkSLeYB1plbSSwKNu3hHe9IMWTk3CrqTRjWfVRK+jZuPiHP3iPk2xkMn2oTzzJ1MbkY0muLfiCo2Qyyh8eBjCaXMtZ8hUXKEUO0ZpuPylKigYlF8YFsbZ0Y6gWWdRI1/3nIpkWpdVdlEjz0hJVm3qRnD6u3u7axDKRSUxmClOZxnRmMJNRjGYMYxnHeEJUUMkEZrGfA1G5ICOTt7OdaHaycwlbpPqlv8JNfwYyiMEMYSjDGM4IBpQlvpy9PY2pjheZePwYrDeJfZSwwSwoYxP1Zc25VU/egjUR19joWXzzPZdSTh9GJsM2Pf9s5jKP+SxgIYtYzJKlctHJJj6dpSxnBStZxWrWsJZ1LFvf/KRB+gEjGYkrfdkeUM5IWifNRrimFgeKCphEAOV6rJBwUSQSgheO9gZ9Ci6nkW4+LwrxJNCEpnqFhk7eQr5AT1rEuCBPXqQvTTSa7gWovOjKTkiK90aneOPiMuwechIXddrMYBGbd9nvs4RinFWSqc/KSQxDOVH0OkXe52+wuu0+bOIY6iOS3WnzOYN2j8PmcjsFSjgXx9lsXDPPwtmquZExGnnOIcGBni7mHQ6OW0PkxMaA+EWrxwmHWTYsimEu+F+Ah2t5SHx4PsF3v/z6+59//f1fHT6xcG+Iwze0U0ejU5RcHq8/FM5/ftHFMgGkSPEyRmc6+F8d7hl1g2b4HroqDWx/GsS7Wz3lN95843k4A7IcNK34/wsA9mXklwEttjei95iRSpXBFAXnB7nG+T5nHJjx/QeMQQRg4JieQ5Agr8mDB9LQuvW/NjeNCCHFYNWGGHSVLsqcODS0IzP69YWs6Qogzo3Rwgw6wnaiw0mz5QycQV4K/E0ZAjeLkO+XPUFiGIfEQBw4FtnUNMgH3uQoZKAJHoZHix+r+el1hyEwCSbDVDgMNVmUvXKwXs8WZV9qVvEn4V2vJT1h6KVO2SMHsEuOQDeA+kl4/aoirqWOnYNs/Zv6x5/P+nzB57OQSIMCrf0Cj83CdjpBl28hLINdcAD2wu4rWsn7YDGsgUWwUprU5bBH+mT/4V4NgLk+8KRPODFxIWGHIDxx4q/G4ckDDiXrR87WH9HPqxIimAHPHaPgLc+3exNGaLwMMcgkzYHCIjOkX82tFmQFX1C1DYjzDyK8ba7XwZ5H0cFpaNBJUC+ATA6g+Gp+4FExmMRHZooJ74XRZEbowSQglZkQY5yMFixOk9Z4COEaK7E9WFgzWe8lVstmcrkhlq7XTHJhLCZhtnzSMfliL7OHOU3SFokDyBVgbt9ABswsb2GxhWdZkqP2uL0o5aQsm82msm4pn+elRyVJEunTjc3H/8kXHsqHVFdSklwud6uszObVp+2qzSVSe96mtZFD7mRYbhJicpIkSYhEJVc0ystyLBJz2OLBkN8ZfzQsBMOK4k+6pMfUJAmRkD/hi7IFanM0CzSo0iZBpc0CDaoqpUKTShXBH1FpsxBRabNXwLkUQfJLKlUFv0qbosJGbfYKllhTVPBZW/AXClRw3Dx/W8tDkUwm9rhCaazQWnWH1dhjSpbSxCvN258uS/mI0pSRwqlUS+rbk49gJYIzTTgTwS1NuEUK45YUbuG9vL6KyGd5Hi/nsbm2KOZyoZCXwbGX7e0sT1EgdoI7WgXLJAIYCMKIINxm6IXbIgJt4DbU1sG0WU7A4Eu0A3H72vxm27+c/m6jUgGtXMZ6FesXsF4ixuHlyFRaduhL1UU89RoxKntBK7fTNywBSG48SIwaKu271eXhi15tt+lCPMVoSw/2/Guevqnczqj5X1TF+hXY10mB9zyXYEBbTUelexjrB5bAe75QQoxarLQQlpRBa1c6hTEixnFUMkBbAtpKQLYkXDqy6d56kRjahxPL2PvBOV5IutxMZIo/nJem8xDphQToYWqasX13857y4C/zWK08/BgqLSbGxqUbJZxibrqJ2tNNavdo27OVkwc3VjI6s6D/om3nevU+vawvHD5m+bE23Wd4RzEv5gi2vEpHfRsqbc+zFbQt7TsnOqw5WSkduuabbX16KNd5toc6FQYs0F3dqj8//kFibCbG0mBHpfvizQVs/I40rB8Jdly69GB7U4LRSD3K7p7aAtegU9e6bpDp25nNmkq7/hRGnedET71araJSrWaYAp76yL2VQatUKu3txoZRD8P6JteixdJCjXn6n/XAEui9s1LxkEizMjek1Oz1wXrfk2j203Bn+NWKR55hfv6kaQYjJg8kpjqrEFzvtWv6WtXrDrGWSBdPGIaxx4JJZ9T5N0p3yEDBBPIXb96YeHM6LQ75HGdB5hSwly+fAtfQW44bUtzx7eu3elNaYfBOgMjCM+DgO5hBEP7251j57E0Vhpwq7k6nRbowwMDcudjZOy8miQbj0Tb7TE5m8ANSPhD7hhYnwyPGKvGNyEP0k+ZwU/msMvJHu733sWMPfp+Ltj2PCquGQ3377e9fQAElX6i4x1Q8iIqOFaGr3MbZOCo+//MeyvVRRRojLU7ydveoP/gxcP+lP37CzaftuuX8bmbKiBuSlRYpFakh/YoZ336S1nuHOAUKZoLnEYGYy8jnhbBb2rjBTHndP9g3B7R9I3tiRAR/eVT7/NiZaKkjVjzdWu+CxmkgNtWpPKHD1ROGEVAxC9qngMcVqY+4ARTGpBWLQIPWe5R8qsdez7tbl2utz0D9+RKNp784/DHFQib1EuCEATCT+u6nxPMi4TYYtCK/ph5wUJkgmH6Xr+pdb3pBYKg49iA9JOHGOL/+lNnu7P8jvpdmhH1l3d7G8jLO9o/eVtrSl0sg/dzLw5jAN68nl5oZuLc8hxqzXNah1kif+HVHF8QOjem0OHf5mdDl0LJ/jBvgPTwRSG/Hhu/rQoDwf/o0Az8pcc8Ge75I/wdAv54q8b6GcCcQ+PTG7WeD9k6QzH+fmbJdhO/qzwVy3PZ7idQA76nWg22mLVb6WgKZDCar2lhfpMtnuCGV3TtUXOvqqcOOtnybfrteIDdw77yHC0vtNIXFHPg+g0AKV4tC+5OhJmTvI9IH4JRATFr6WHpybd4TIPXD1ADY6hl4X+gQ1bF23LmI8+KtQ56mSHsD/LKF486KObKNYxp+GaKjg5+4v75T+gjRdO/rwJjTI/dGvq4GyWNFgqgWRw9Jf2V0IMu1Ti1PsGVem2RXnTOp+tTC45SRzdTtrzVYwbC3Ok0XEX+sd+q7BphasK57mtXI2teOFfxki28EkdMl3a0qeneNpGjpnqHp/aXNl09yvRr6Q9KCrV5NpBWAmUAyGYGk6tGx6Dh5p3GhciJdKQBc2hDI/GnThBGdB541KiJtHUw61ZOZqgP13rpI504YAC9pj9SsCiSnknnHqXytxYXPzHw1R9y6vOJEtSp5a80aI3ABdcakDlxtCjXZkP8atOLTAS53NrqPMWfSCPvP7omNai+qFTuXfVAbEAmVI9IciDJiJXG+NxLfvZnFlmyPUwim4ttOH+otK41ZVEoyvHI2pQntj6aMLZNQz0Jby0dAgEA6nrtgi7NahtOQDWsc4m59f9YwhfWPMpH6teLVDEIKNHCMjlXK9/TJNJ9COS8xSyBJqLfW10XaASBwS2F0nrukCXW7FkYBJZWzqoLZ0E+it/GOOgAzQXjzNj7MrIXWGgsiECPXySgwu9EGeGrVsaQmvqlXhXrr64FcvufEUKSLiw0XY8aYrtw7xo213ZO2G5zeSnd74s4qgva6xGJW0FRnRmkFCV4o9gyeuhiPDnhjvxVD/LHo13SH2+Me4rsZK3fk+w1kygYv4ITR1c97PJmcv54hAqQ0vJDddy4a6wqEbkHKpzwjZZ7abLsm5Lj3a1EoRRV3ZCVdaHq1MF2DefX4ZYiPg3nmklMXF4SwiPn1yTd5bJetK/Jk1gL16o2wZ7iEXUicVT9aHnWiYNicuLcdyL5+Mz7K2rLcpfQMKKH7m7rf4DvXgY7l991+wr6nXeuuKd/LZgTOTjy0sGw//mWIzlhwJnsBW11Bq6mgg9DoGr78m3ymElKbe1f6E/BQKbXAvZZlbpZia/I1Ya1OvX6EdMqw7+/MQH1LIIOLCS1j0Hp211QauL/4pclftvdtNPFZrRthbhD0/RM2mKLx2Sn+5Otub3gGmGQhs7UGV0N02KGbCBFTjm9z9JSOM/huKDrwSX0/TBOWMO95HhLPrwzOT2i4jDiXfX+Bm1HyRcYEhUhKfNz8hhJO68PIzO2e3HImKHoro8yUi77KmGQj9EnOfnp31jrdx11647XextBlNgBubG76chKrCAdWhnTuLFtN2NmK1QLA7k8Q2ZqWrJNvxNSTKJuTj6id2TU10IITs5g7UQS2TyBlw6aiBRYlToBP5IrElHtSEd2LbDgJH++67ihTU5F+BCKoZNMarcyzzQQ3Wd0zPeEsM4N6qRpclZcFavrBBhsfenYj+D/WEl0ahNPiNBqRNc5Fqho4DJGy6IQOJuofZ+My5/AOCMQ7b/I4nWGvj6juWFy4ifuXCaICrdndgaMxrnXrAdi2D7vz+MjpOLQDs9MUEhayuSfa1rZ7G96QjFzNzDKeCqh3/0uhiSKCkLndoOIXuB8XUXM5ZotsM5ulGyefrSIhrCfZGqg/T2UN+VCU2CjSnw2PF3u9GahPCqQ+1qWPt0TaALg9aO/GIl3OjvsZq0ZdFJzbBDhfFkhz70uOmOZ44Pk6yX4Bz3XW6+/PlxE2qBRmTuwvKQuhrDCRLi+EKBbmHQz5kRGBKCwIWX6ukO53LwkoyllNfmbIwJREuMwXbAxZQ3UCRDsAD/RJfpwuzA4srxp8sT3rWLK9jqzsbRWhzmVu93zEBdJPMQmbMtKp5XI69SHbZpIPQqFyItKSXwl+C/ViKxRI5bcJj21BHfBCtBemFlQ/jkgX6jsCsXosF5d3UMBrRyB5kATCPmLNrHKGSrOXZlfNLloutFxP2EHLpZbdsLtmFy0SBXwwi2HThE+4YUg3VRGlIRNzSwlRVW/UyUdJ5MTHNNJI4jcCq2gbbqM+RsGgW/Amg1nU17ng72y2k6T4BupOMJzICYfqNWj2WOPsSLyjvwGGjZa3p2BmQs/zbtUFEGs3s9jJdIxxpPMZy9dH6yWAEwGDBZMe1Is1uwj17k26oT+2QmWZDPwjT6clLmeO42CSHBcf8IQlfaMjeQALzKdF6p9F2hpfgATVeANBmaflCQUIM0Z8zYtMdHHht2K/GGHn3z7suqX6jUpjaqi+enWC4Hhrs6RRPeVYZah7zpJAsEg7R4bDqcZXy/WSq7xs/y4sF4eRfPXD11Khep1vr3LEbDYZqlI/1M3vSbBI5xLgPWzNYmwscO0liLr+BW5I+ZcXFYGryXHHJVbGdUeJcQmQWG2YhWkRPZvNSlHG0LeQP+UHSC5lQLJz6IUf47aosuc0t2WelhTrWVI+LN26zNe5IW2kxc5MyIxAHIW2ev6hcec4Gbj7nz5Dh1Az8luBtDuON/zl5vCakVROO+99a8vzLidfvx4JA3CMm9NhF+L4TBEpQNj99ABSDLGa5ApOZeC7ejYNEDNDnL4h3Tc5D6gGY0Ft1Sqe2c5J6fUwwcVr/lEGh5VsShl6S+8S4MLsX3FffPnNLz+98s8fwi3zoEdQj8yDHga+OyLy+HtQnxgTu9s8Eo+oJ+DBjtyxZovoSDfqnRSVi4jeemzxkcU9bv6RxUfQOqKXuCMT8GuxI1ivT4uKjgDKxmPWNK6NEiYy3UCYsDqqGJ0shxBINvgp/3X+lSYZjMBlEyjXRy21BSR/Saz1Nf3/lr83Lbn8/uxeOOmtrRJt25jXFAvU+qPN7XXA2fjBEyDn308ABVwRp4rRyXIIgWSD0/zX+VeaZBAClU+gaIjCnftIXWZbQ7RyRVtqtaYWSW3whDe2ZXw71yYb7RbdRfxO4OWHIASSZaf5vVK6Mz8fXAHTYGOT2i8NX+E/X/FqWNqisPoH4LOwlRISFgWvapLE/eyHtI6274/8IlYplwMhgBoa5CtiypW/IqA3m2AERiOgGp10faY5aN7I3mLcI+0f4Obzp5rlM2EoQs+VwQgsm3OF/zp/elBWMWQSFY1vqTw7xT95lX2R/eHV0UrifWX0SXPlNH9qcBczMi8jgf+vv1VGxdh4OVl+UFA6B04on8DEE33LAdaii05htNN8V0tqWYOokkH/t5BZeI7OkClSnVH+9RMjvcfwfxSjemBtPf3F/Npy8NWH9jx21RV6XPAF+8JYYyrM3UCl/1GzErkM/JfeWtn4uxOkkE4UwwgsyvLu/e93YFLwSsWrYcmGSrN/BXwAVlJDwqKiK3KuKp9ARYKfwk3TrTv8p5CVYjGTLLaByTICy/vzZ+N5tyCFHtcpnakonyy+klWK/1PM0UHjmJAseQ3qX+Yyr1+mNsN09k+lwkAQQqAiJo2+OIc+O/v1zft+MUQK86hkIXeWdb265Af5rDeQ6SywrP/H+G1DuQpGYPYIh6ie+vtfndBexiW8PKaQjjBJOn0kp3Bs3r1/5b7AbvvZ8IcKK8UbOWVvzdf3Oq4E5tNCQ/pEVj3kiTtW9tevltsjQ8Z4VrW4OmBe3pFdpxcHFrs865rbQ3s3JbJDI3vWBBSrYi2DBuDplAE2US6PnwvhhqkKcxYzBqsq5VEXUVMmKxyZINToCJanrhHrIlazKhqUqyV9NyTAi9TXU5F2JZ7n7sBUjJ4n2JpeRXt99h/B/uBv+0213YUbfV6t2kVo1bGsxld6+LUHPYoc+gjlAGxfPfKaUjjm1ZAql1vrk7g1u3VGgy+Babjbry8ZoOYNUmhwCT1XIcHFaeDRdueOghiEQLwaI8qgItQCxcb8NLQ0mIDkWKgOLqDQyVIBAiFQjc2v+/YqOs3/9LgaQiCuC5RJXCAPQiD86Rv819E/fwZsucRONYzApV9WlH3JhhBIMY8Q1FnnD1gDkflVtjkhJ/9Kk0x6yj5MNFHqVilsnuJzh+ev7W7pWdoLPLQIRmDhD9yPBTCb/WtFTjsKIzD9wvnC8kVgUct5/A5/fvPQu8y3Prb4fSBfBHzt2+iFCzq++9MSfNfk/fTNLe6W6NtlQwpcPVoI2q345hb3w/K3rhWpeaIsceUtmgRCIBpWT6/QcyAEEv5RkPkIeARCIKXrG9/VMeehDrC6mAoPxIERWEm+6/9h/zocq4pnufLxw265ES7FEw99cPyu157I6GAE5s7IOW+itmM7X7hW8av8o4zzlgSSibzaPwEF9sC1Z/+c17E/yjhfll60y/n5qHri16+foY8JpRd1mV1M3hejTJxMGf9l8r9A6CZ/KrvYDRkVYZMYpN44QMs9ESMrd86dYG3+cOAR6BwoHwMnQSwrqz6ziWph7w0ZWbmZbbjuEDAAFkCFK2ETNfz3cs65CCW08Jm56LEgte/L65lNDI5oTu3v/E/m9FK4E0JUL/ifbPpnceBFwQjc+RGLglAqDvKEl3gUhGKZVs58g55/feeoj4pQBWyZ+KCAglB4S8XON35l5UPfzXaaDMDYao0rd8mtpAShLYYm4JJETyKV3RQO3dni1i5NNA6S1Q39iZI6aBzKHaRVJqNNbMfgeKL5nu6QcWlHckQN3E1LbTY0NeuGvVHLUE9ii6rW0lzyVXcJjbYQHmUdQ3LdxerEJn9spKlKMhRrX25N6o/fgxNXCs05DOrbvcASKp26YUG8EnNq83IMndx5RQDBrKmZv9fb9W96xfLdzdU4jMAaK2tpTX3YVmlP95GO/MV7v+ZPrT+hobx+3P7+iTBOXf+ISeeq0Er+0L31BljY4A0CCtrScP5XPvy5uvfsH4g6tteXM6pQioc5WBgLPcH+pJLSXW8OFEZfI6Z4ZFTV9p95WXxPyLWcjtRYz3P85A6S7oyShGd4P6engrQ0+XV857azCygCTEYPDA9Uawvo3ruGMnmdcGdlEjfIhRAIP/4P/3Xh1TFcTclkIAiBIMtASq/c/4kg7rUaFS8iBLuszrmjoCYclRf10dHXUU8tJJHNzEEoCAWxLj5bD80PJCE5ZgGmuuFSZuJAL2DReV17xyOau6BRaBO+UgIK800u580VpweV23AqDblP9+21hc3dVrMOB5e3y2r1r0RvpI0a0yuOi/hg3z/qgd8RWbjMrorjX7lKuA5/tqE12m5Vhyw/DQdTPgmuCirUcUfM1dNLUvIIs+idtxW3JW+wmTdIG7G8CoUgLzwBfEPzparkTTq9tOabKE9a4eAwkC/odDJ7loLanZmQQZyJmKq5C5JYPybwc4HziDKoU6WsFlXCa6rjScUvje25w2WqdNU2JS3i5KcybdDw03BwKCA5/VGXBCgpuPyHO+LuI0hKYUydI3mjOL8xh4wSK20MGPICGIzA6uP4DGfLt2IYgdWX8iiH5+/qNf5bTNcg+b+f+rm7oiqyYXcff/ppNYRA2AWUC34THwxQFCXyEHUTXF2QWw/wYARWrsRzJW+wGP+SEXKVDQRA2ACvxVeqIARiXeByZv6GEVi5Ej+4vE1eq3/FdkOS8ExIbTFgne1P75TkDXZ+329EhFxtRUHIC+cKdP+T+Ad9B8z2slheNsvAkrVfywObcgU6p6css62YEWEUy1rTackbrK7XyQBhA7am6ay8iRVGtRa7lm/XVTQvWfzX9Wd3rT93ZEpQ1Y7UfeakdXztHrASKstnFdIk5gaV1Tz8G6AcTlhVpYh8EFVzhCUzzZ6tb/WCKh0rJGw1LSuNvGjbpwkjbtxeyJY/n6kdOB0CbhasxUuif4sLniwlwuQCAwPODcmvEjiew5kBbPjDP2p/n5TVkq/abqQr1eZnHM/XN1twu9m/QWwNVm2HxSiNfhEuk1SnbZpWo8va3EZesHgjjXZle8SstzprIzZFb7gaQDnL+oHkdknBE7l6fQRJoavVP1XceGXV/a8S7qyYMztNlR3EN1/0a6yVodZkNtZi0foN8LCq1UQofSpl3FatTHtMIS7wP0TpV6kSVS5548/RUkkorFKlRcArKrsr3ZIJdFnUIctPw8GUT6KkQgik/xCXZYWkhWy5B/sB36aEEIj1JrcCA7bTrTVfsBglyxgFt+hIYTUzPyJZXQmP3bE4vunKZ8WcGaDdzTt0bO4Dn7yPor98oSn/ZDGzkgIG4QPQBdWjPLGLgTy6f86OV17GZItexFim3E25RUzrWSI4U8a7BjxHKz+zT3CT09AQpT+bfkDhIjir2qrgbXiZzOjDyxytHvrGejSHWGN/QHmOWRQmo8SAlgFBSfgn+70qZZy4GinMGse380T0Ytp+mbuKXGhgQLkh+YvKi00ZGwEl4NjIcq33V94aHcxmb9VU36cPhQ7SsdkjET1h7dER3rxh9C3c0uQBx0Ulo8ATAGVn/cPApu3JkYq5ywbxcAhaNuR52axrME4XzpYaYUVjIxWr5+V8urAQZgM5GVvAgt+O/PX0vBvrbBGmZzHr/kQ6PG1g7cbk0FO1i99e81D+RMvYTPrM+a2heZ1r2GJjVfm6x4FTI7WvMO/7NTFxu3tBQKNhX2AjSEFxBGdwPjXOF7eV3m9r3k5vRxS8rbondgiMPdi1v0f7n/DP/tRlyq9tKvjqMHQOFK4Ez4LOLF/cF6A0sm90mPIjKfh83zmwAAI7KQGKrn9QTwkGsN5g6v9DaaGSFMSquOBYyOfkgA7O/cVFMwVM/fxJjjKlnZIAJWyHu8pdRkoHeMyi061rtBXL1MHEEK30UgVtON8j40W0e1qB92i2WpO8kbSbO9osFutaY2E0V1eTp7LYUxGLOOPX68WXY8wBlibAOVFA+wouAs6VBSIOrbGhKaw8VTTEkqPnT2jCbWGXclMwj9BGcqYw0cOO45sJVvFRd0FV6ej6sEKpCPpNGQH9QcIjqhJLIn4xaXbJn+VjuFf6j/gTo+oagYk/MhLV0a6TopkxMaTJLQiQXdzywuQEgMFr4tdhMFy2MHV/TGlWB8t8v+10KxRS/e+vI52evnXx67wnRZj1l99WALqD0P10mAYDJ/ossnBJFYEHRbJ0oQmA/WcUzlhX9tp92lDJ/NOHYmqNN758iPaX1E1k7hLD2fGElWVrACY/L1/QugSO7r1RiG1sPjMDEAJpMigIBe8QpcO6qcfr0wUlw6fPxMxab/3I+kI4rg4wCHFZp/2cdZHS9VchO0isvJ63m+76jZm8A423L3yet+5+dDqn9N/1KuqdxvHyBzvH+tVx0b3O1XdfFVvr1+QeDdz143Y6jMBb8EE84ztr9g8J3Bsn8NkDL7Bt6bN7TR4Nv/AmjZUvCsvUPot0uH0kS6+1uQskDLGXmoQd+QXrPTXFvKZMOd8SEXJNFekPwL2e26zVWwbtfquf/HuMOPpBuTZgkS9EwGJEUIqsIZ0WjqSpdhX4PUjfMafOadE6du7RPfdKj3PNXa2N9bkx5a9lCHGyjKapeNr8OytOol8tiB9v7bpIWeq7zz1SpfIiNtxnI8S587owHwg/rNghaXtvX7Qf6R8n3RP54NKKkys6gGYWXguevc3T8JLCRyCv2EkWbqqD123c+3u/H9x3mPA2otltJij2Hp96ZbrLvEGH1phoCipP3fJBcvTsKjzcGnYplwYTwN12O55m9u88vvBMGgVfZI5WX+MzTnry1BklcSfnpSULekxJAXKAwx3SDwZ/lcEHNWD7mvglsfc7QMD9G2Ju7OEJkaRzj5kjoowlBRzk3zUAtxBbc6Wh1V5taGox2GwZo76lulrfkjX+5XH01geVTV5c4fL311VJW/yVQNRFZmrb7qzA6+SKkwg4CUn3QeeYzuO5RSKxYb+a08WJB6b0BkptK0SLZcADUGEnJUAJPhKlNAI2FhEhvOxGq95X7BUII1QcGXkbtRGJYnvIRk7p8RcZLnASkoyyJkDimdwiqcRa5VdLMd+XT/GMHnmRWGfrX5sBnqT0GwTLWReoMtqS/zbT/qr+QioyUKoyQBgMgwfg4mJ4cQxKA/MAoOZS17CuMmm2WltWNXRriSyZVXWn5Ck8sbhlAcITTuboF6Xz25GnriihA7Cz+jHkObhQwLkLkHPITfuFusrajMac7FwypzVx7qHd86WEFevG79jpGa+xi7t9NtLg9FYb9UFzRe5HrDHWeFgEPPyx9d6eh4FekNYqy0svAF259O8S0Dgs3Mw8ABLlWJGjtk5IzbABRiKfKiLu04hLpWj7O6zNUR3h0esttW1qq61Bow2r9MZUvX1t4zFXi02trAkTpHRKoTQSvqh2KetyG6ZghziY4pFbHLHVXws8Ot/4w5RAS/jicr29sYoYDHi1W7LDG5sTHDRMRviLWiILJcvMEkZcGppWk1NWgzVaxR+9WzTGUgB/IramhGFfR7thtDlRbcskyX0d7frRbPya2hLhbckuVI56AwrcVifYlmmV7vUlgFoIgXR/8PknifLCD5VkVv/QkAei5kj4N/JJCIEqvKBC5gMBBaUBe7JzKL28cshsqg5WG4PVMnAcKgq1RL3heNKh1w6Eeuqa3MGcQH7TIuivHXkQDQI7KSaqccUK4Hlaiy/CvlP8vxhPSjzNC+BqNBBXRShMijsk+lhqs7stilqZEjwI9WSqbHq3rFAqxfwzT8mTIVwaI8y6VFILhOr5p/MxCIGKSYFkhgsjsPbolPAz9bV2U35sCdAHLYYpFFi/ENoGtFEaxMpzogtUoC6G8IS0HGNeczfw9bUIeAB+Ar+MXKEU8rmfy1gPB3iymLctEIm1ZdgpOcMIvOOjqxp62prd+myj2qQLS39m3wmIk4Rbh0VxI5FqICzuhEM21xdS7oovGwpHYFhBuGldEfdc4VoHzkhJfS+prYaYM+JoNpX++AC2Bdzow4DDJbEVeRjsg/8ym/LTC0BXLv27BDwOCzezDkDOcqzIUVsnpGZZACNxRhHXOgI2jcIgV93vUlf2BQ2aoMlsijUrLI5GtTamI/WNNa61wQds3Vadoi6i09siXjep9YbIvsA/81Tqv0NCKf6jRWQONy0ArqAGv04Uw01kXVpt9iTssjn+kHJXfNlQKEJBVYSb1hV2zxWucagZKanvJbXVELDXhdqsxfVbWGOsl4DdBH8qK4cRuGwERUdLYQSWDH7K/4I/NSiDEbh0FBWOvmSUJs1+mthfLM92HozAlvtqFlcyTC/450aZflD/9LYEmHOZrrnPZiOqAXXaurzz5/lNu3fdD/QV2+c7RV6+k7hf49mp1ULDfWCksI88T9bmidUuF/oOUuSZhy8l6lXwSdFCEd/NnC7UMKd15/lMNi0QYNPSjznp3NhcTy7vMjWOTnC0AFHNnPXySMuggOP+utAWIT2jQhtj3y09D59v+NcjOddA3wXa3OucppHBeEtPhzTD9qFZO7yuLixTV3GwP4aFhC7b1AGJEGgowp35bhwM2iDpOM5Ddz9udR8Zju3FcrR2JtPp2mlFBNIeORdkOEvrQ5ph+9CoG27HYos2sQ445lYf46ACKteodbkqqIB2j6bwe5e9CHPFvTuDjKhvSywNhquWviHzEIjcLBIHTRaLa4CCjloVjppIla2hRCLvqJt8/KeGX8M7KjzEvP4wCOS5NN6+AnfzkVEXd5ccAtZv9+RZfA+9oRFJESHulhn1SZJAh6fQOT72ImLZhnbPfrOukRj0B8oVoBfTTqKHc3AUhhLryALtzVkUXuvuoRU+mA+obCghJRNU8rA6WsRDMG/neJnGfJANGmUwvn9+4yPLlDpYf4GF2Tknqtb3/UZumdNI0sy74zc+GI23Z5LWBUbjrN1gwizhAOe+6w2rXzfS1jKrVhwCBDy8nsoJYS/+MZtyrQGxndJcVg0A1T6wMMB3M6cLNcxp3Xg+g00PBNjU9KNOOi8mz5PLu0xNohMcLUBIBCKQ9fJIy+CA48G6kBYhPaNCm6Aflp6HzRiBkbqcw1YHP3NoU2wvRvDdw5Zu3rkRPujpDodHEB3JIjtAkXE2WH+OHSIU0lqrjw8kCDHIy7xN69cS9YsgWK5epE059mbgWqI8mpsMHvhmt8RIIY1SyWGq1XQph0b9cnbAMEZqxbCdVnofN58UwzuGJXryIgdTmolbSlT93jO3+jHkXA783nJ4ZO88a42L5wchEWr6JNcvAu1fCPVd4LoNMLG9C0aT8ClWL9b9omwOpwrzik00R3HK+467YSNOpjEoNo7Rn7rDW7GNliecujW7MWpVIH9R/auhY31W5vPbM/6GNaHyD5lZclsTEBpMcioytNUpQRm6tKJu33yF8nqg/aRcblsP5XHKhZIFKB7GxkIYL16gWGiIxN9pKzqw/6vo/GuKoL+EMcEl2I/aPdZsHHAXeYVXQGvRcWWSlm0S9K+l1b/f37tbq4I1v3yk9ddBSoKCz/ej1BIKdB5rnJ3Hs8aWzRiFITxgVVNWYNuGUSQ6g5EU9qN/JUk/iEIZGJWaJKGNsL7FqFheFUFmpyTdnjw56XZ2iipSFWtvMVLYAH4IrYUNdQ8LEn8EIBNBLN9ilLAeWDyYEmvEx8nzw2LkWY+j3bgCdy6iQI6oU43NuXlbBFFHkE0tcuIK3Bt9VJ4ZEy7P5+NARZcdAp7MTQjm4C8m3hFmo03a1l0jJZzYEOZ/U+CU7uEv2Q2mV8PdZVCwP8wg5sH+8pHpw63h+pruk020th8+91J8g588a+DdMBuJbzCcRNCNzUkETYh92CCqh+SvNHNLHBYaBMdORz+Fqf6/iMHqyWTLF53qN40LNRvupzZz59aJLzVrouhzfRNZEkD368xOFlMnR0ZkbGyXntHJ2b0y6g5LyUkaOCIuT0ihvpJNyW499K6cMTEZgxslR/Rv0ahfoH56z/r1OkXXEgqy6wpc76Eq3ZtD1vj8tA5n6D6+3NEJ60HMNMGXklQnKyKppi/WEwAe953ICG+XjE7t84e+HFB3cSyDacL1ytNx7k/u/LbZncbc+mjwuBJTPII+O93j250YkRGSGOVNDPi8yWnuQGhDf/10W6tQpqGAEfhVgCtwu/9x3DxLjpCVENPSUTc6x+tIArZtGEWiDIyksLlzMZsVZWA2GwYa1YX/nQJfv9qen6QQJHcWwq9fyFPPbWBhICIxvSb/tYCPjM2IioxLu+qMFkanuHTxQoQluVZ9vt1/dv5rO56Pf2aPiku75owWRqU49XFChCWZlYP9C2E/C7lhEfy/kRz//0gJHPMvkPPjSnU86AC2gLfpyb/IYSqcDDfDu0iJYlBTNBXtQ2VYONYem4o9wNV4Et4Hn4pvxO+KcFG6aIpon+i3uJH4oPiHhJa0liyS3JeKpbWlHaUbpZelJbIs2QzZTtl/cqu8mXyZ/CYRTEQS3YmNxEMFVEQo8hWLFE+UuLKr8k5QvaCHwf1VCtU80kAeoEKp02qHeqj6vSZaM0/zXLtCe1T7XWfXJeta6ibqVuve6JX6cH1D/QT9Uf19faXBb8gyzDKcMTw1/KNtdB49iV5NX6JLGD3TlJnKbGKeGVXGUGOysY9xmvG6scxEmnymRFNf0ybTFVMpG8yGsx3ZGexm9gz73ewwp5gHm2eb95s/WZSWFpYVlgprpvWYzWEbbRttm23babtk+8XpuByuDdeLG8kt5FZzm7nd3GHuPHeT+8BV8ArexDfh5/B7+Xt8IV9pb27faT9if+BAsWAMBFIwHAAQI5VqeL/fz6lwuUSK/6uuqqr+W/jff//9V/inqrq66h8uJfCgYNbn89l1Rs7pdFqsVgsqKioqwWHF79+/CbGUsVqdTqclGATGgtJx6F/V/6ukUgIXl5eW/ikeC/6NVyMkBqUyGayuKi8rLSstAyKZbPwYCKSAB0CvAxAikRhDECEEcRxhEGEQYjqWJlDF18qKSkSwDMl+ffzFzPM8OROXS6srSsdCMehPRhmBGKa6qqq65EdhYWHhj5If3UUzDMMYadnft1La7nA4HBxZdfYzwRgZhqFpmmFQiUxWBC+9NC1uawqox86ni4r//q0oKSkpKa4QARnxfz9sNKZUEgoZNlZT9KuyshLH8SBCoSAkCFSXFRUVjQciMF1vdElxccmf74ha0WfEblJTlJpU4qCMYSSYTAYkzH4SCFRj58keaopmaorpuRQ7FqCYRcTdLmeDPxxFEEJCKdy9aTmKMEYQIshRyCGICIR49eBkFueiUlPEPJ6dyPE8iZNMKPa3t/tFIY39BX0ESfKnyJHyiKJ5hf+hgB8jTPCXVYXU7K/WDNOgcXI5iZOYGqah/OnW5NtvbtCu1mrV7sb29vZ2ATHbtmNRr7dqJTybzWbLcEiRIGtlULYeGB0JISTXWjdv3lzVCSH20fuyY36/2clkMnWSOE4yRMWCYZpGfbXf71NOZbFoFESK0jiOY+dyEpdv/9Cdl66trbTb7Xan0y4LHEVpNUQYhZ2VnJMrfW7abouUA9T6x/SqhXmoxO8KbD/Vs9HJPjoffS1HsjqkVIQIwYLpIiFGCHLFfof+w/+aUJ4WbceTOJYM0yjsCrxYjFddW+v3HwpObp1Yo95XIhBroqk6geth3oUnFslM/S4djXChgAE5+julocoygpBy8m5RmODEWS4Wy0XWqNcbYEIT0sD3A9deTpEoStLpYc2bNsBCqb+1tbnVlxlImUGooDW7Kyvd3vrGzvVrPdC0QNWC1fI+KL8HP47f53Lwk02Avwsd7wFL+CVvtFuiaBhGs1at1soFhUae63qeqOuGMT6bjCeT8eXSk6+//vpbb+u6LopiNlSQIBfKjWuaOpDIL5uU0yRJ3uN+/iRNJpf74NJTYKMTf6aQzb/4n//zf35po+bt27dfXVOHf0bmOAJZ+wkTiC2IuLYoTu3dTqehza6QhW6mmZ+PRqPR+fzHKoQZZDjjFKNYrtWqlXq99q57qsLF119/feq4tNZBHaBaf4eDECAEvyewLAmDIHD9PfLwYx8zrr9S5iHZo5zIMW86EU+j02x5cXExmQMsz9I8/LMCM4PJ1tZ6K92V1VVor6yVFH+WZnHCo8OYnV67pgPfJgjCME5SS0v1/stb17a2ut3VlVa1VJDbm2sAdCypVGvUKkVVFBEbNKlIKaEuMbpXN2RYHxLbC9sP4yyL86GeIwjYl199/tn/fXwaFCV8RQzmt0vXruvOLGGEP45BiNDyZRxggJMlSZQeA2UpeXYGmy5H6+43vCABzfwpHyg/2m6yNSBbUhyfjs/Hk8tFkP9plYWLi8l4PLmM49gP/jzy3din8P8jk95ibHLqIAaYVwp7kLPwIEkDW/BSu65DlUKx1oT0/s71lVkpUMYAteBkti/NJssoZ8Vh0jj9szQN+JJwOSH4AXpL5mO8kyOyk9I0l+sUnDdPmm/90A+D5vt+6EdawpPnB1ay2hdOmiQgsvbgk4/3QPo+Nxn9zN4HTx6RgUfi3ZTAyCjnsCm93Wm1Wq36y3eKcnAxmVyMPv2YZ+/FpWOEqZseXwjlSqVSafav4fi/9f7PB8+fP39+dH4xuYienDh5luXZ4iqLvQ7dO5+c5ODcAnQPEvw+CUc/uK3twacf74H4Pc4Z/eD2f/viIdI+GL2HXnxMpQe/j951PdXh4fPpxdU3oyADWFRkWemt9Oq1UiRKnUhAGiyXy+WUtCKwXuVFQRApzGKPZIcXvuF6nhskgIgxZLMY6TmIrT2cgyDdB8QCeJ8rv47Zt1oPcQq/Hu/DxWeyhV+q3C27URxkfBmn7YOLch41nmRze4Yy8jTTCsDm2mtvlEed9FZJ3SQSB/PQ831vRs5GI6BYyP3mdJ/azz9b4hfBe+jiFP1l8vDTPfIq/fijjz9O1q6vqBTSePrOdd9Zp/Ei5CvNchwFinWPzyLncjwZT74D2Luc3opCLfFoFSPtWkMHGJYWgrtKINE3C/azpwcHZ9NpkNNieaW4y/+bf/i3/87f/jv/4iBUws9QlJMJpZSXVS4oIhn2lkdPbV0STEEllMflqyfKkCTqeq/fawPTwv/rk0/+1yMOS/8yLpb2RayU4jiJAThCEfzyd/KNEsfxKYMQIYzSmNRf/va3v/1Kz1Qym6OEZFka5bEqkQEhkgliy9pGom6oBd/zfTeKojhN/AgKgoAopZzj2HYGIWSx58XZ3i4DGHYA6wqPBrchVYuSphqmWSwPUy1rKFG33rC9/da6hryFYEezMr1YrNfq9dWta7CS63VQsSyax4HvBsngZaFQ0DWnONB9/mQS2yZZvpiH+ElKIsjfmyz83pW5ezyaM+HVm5v9lW53hR/r3ZfA5saNl29sbUyz1iwViyVTzlN38K92vvI0Eh8EqC5fb2VuY31NDEDXsrgsjnmevCvwFCwSi4miv6UmIA/D0AV5Vrp9H4Of/FziJ2xlZ7NtcPf43B198/T58dni/7mwyyVUFjl0n8/8xWKxdJM/U4T4rfZyvoxIr7fR9Zq7G86l+s7Ozua1qUwncmS5zJQKHswtQfZCWewSwHL821zJ/McKWZL3h1OV6MUYt9aE1UYKqugyj/PevEszYCmsbPYrOaQ6mx0fTWmnmXS31zCEcQyJx3kK3tSd4kr12q3yKnJ6Qvm9SwAqAFdxXaIMgWoNo4NRFEVB9OeLURBF0Xx0fFN+PJpfEj9d9ZzCi5jC/60JMP+GW3k41PD6ZZYuI5VX6/21nZ2dne1Ot9tpVYoFkf6NyZIkeS2x45nzgE4i6N1rt1Z7W9uGHNKxCtStd/kZOBLexWmjgcXDAoCIAbwvYHDw0xV5wZ/WMbh68eKFS+QMIRTkAD96VtDZ14saFTudqiaFh4eHcx+X2q1WWeFFRdM0veQhgrswpI46QZ5nSblcrnU6HSBYlsnz1xoCmGehd35+Tmv1irQH2IBkWZ56luC76+eMvu8PURIrw2AqbhLFbhLGqYZZa7VaBRTMr2wktba3t/utertgGoZpqMC0Bsud2WE9D2aTg4ODUzdhjCGOKqZpGtmAWwgS8CeFu2mSQhPjNJXl2dzJ5drOzk6/qkt5nKYJ150wZAFkScqeHRwen52dnY0vrxa+7zmKkv+z1tv5HHCSxA0KvAH4KIpUgZcLhQKPho1YkAqFgm6rAVEcxwyObSrXD4JgCBIEv0n3nTiKYnc+n8+n86BqZjmbzVxAEELe7NlXT0dhIh0VlLQqzxmKcbZiOCJH8fCB1jHoaGUYzk5OTrxQiep9JUvzHO+rIWKZFzG/S4mTJF4wS7q03TRLY8MslSoUHKgMZYdvOyGlbtsoNlpXLoLNANXakzHwnOVy6fmBz4Y42Hv27eYZeJTG8MvW9OXZ1dVsdHR0dHQ8OruYz+czURBkfQ/cYVAa+YvMHWe4GW9ogGvtVTDwaSi25/mectcmib00APihdHkZIKlcLksoE98GGMDPFK8192lmPt3hanrY2XzpxXEUFg8sN2m2lDKqH4lLgoXJzO079cHrDcqiJv+gZi6gVCpVEYaPoMX40lbrzZL/awUOpZRMno37BSgppFLhqUnYcjnckzRU4vtoNRAFFxZAD/Fk7lztoyxYsoa3B/T1Fwvb8bM8S4pFs5wzvm+aumHomsDBDCgWQQ/SMfy8Zt7q/34hQ9/3oVzudrtlUqqH8rNnY/voojqxx8+kDn+P3s1C0ZH0bECrvdVytdNd2bdTrlTKpiJSEBXW+UxKoDlnxY0mW4tp+nkC07WDrqsy2dx2gjhOonK9+jiiZcAITYALAIa1Z8dzB7lpW7BYZ7+IgbsEZbGYLxf/8+RlYhfgRyvmMEp3zE1wsd3SzcZFJILwHCYyMT7OJsfPnz+3CQkZY25CmZMTvOleiYpSBog9bD7D0EQxOjg4OHa46s2bNzerQLG+ZzAEAcbURyTPc15gOZZ04e/28NSp48xl2wuSbnBvpdvydVplQSYlMOWIwHVrXLCMFjQLwHMc4ejxuZ6qIQlg634hsIUhz2eLuZ/rAsv39XDiJBzHRSCYTCZBjvE9lMapc4ZfO/htnmPmozjHOrLteVyob29vd6pSvFjaIXIpsaFn7a6yeRt1WZO5AJh2ud0cxUEyMGAEUb5Ok4S6Nh6PL4MHYGb5PjF//2HgN6rfrJilZreTk7vr6oZAeEHSTJEInYZhCZpI0orWLguUfAzD+eibb745mkwml95eibiRQsAfVd+pVExZFD7ubwJZwCiP6CKCFCNnfHE5s73A91YPFWSAcEqHNZkzK4BZkl5p1KrlYkFRuMH1PF2RKl8DFMTIti+qIIxzlqVrcp6F85lJ7ek8z/OQCr9cvpEknjj0Li7ms9k8VdvdNrUpFgp6waScpVGooJlG4MRAZ7ANqZKGUZqmCBEcqfWIrJZKpUq1VqupwNewLfwR6R2FY7xekgKe52l/bW0zId7UOztdPorFwubmZr/TKBc03QgC3wnDMPSDBCII2u1ubxMULCVJSA6GtjObz+fD9STWq10yKQxIEsuypmkfmIjhbw4iX6UEJOoLP0hvjCm4JguSKEtxKIWqZWI21rbpUa7f6PMZQObqlhuAt/b4xSXLcwbgPsfA2+xsKjT4Nb67FTpH6wSVO73N7a2t7Vu337pZlFS102532u326vodXudatVSpVh6Ir02s3vr6ltDj+bIkSRK6eiDf5xLwDjib7sPl5Z8H71hAvf+U4bfSly3c+Kej87OrNKc8jHkDmvVqtawmyjIGmZdEm8x4NclKuq7cp2mIsqk9vy8R4fxvD+I7AFp7gLOAvw8yC5B9atSysKrThzjHk8U+nI7zG5TG4hro2dY2Q6LpGfC6qqiqQlkcjMdjKaKz2Btvd/PxtaoUT2fL+xgLwLf24PGTeyqv1Hq93kq3220U6Omfxk3ucOSM4GdsguveixcvIuY5juvmeZ6lOSBmtVatrvZWhZw3ZFnSdWQxcz3PG+5qUYCwPE+K1YoiS+SBlNMQtbqh3+8/CvjbjJtjl0lkSkmukCYgIUqpWkGZfXl5aWeCJNl0qbfSnFm+lwaL6XTKNH62uPH2y71eb7NV18UwSFkGALKGmMquN5BD1z86OTk5v3JTNqDQBbwgCPcqOSlVNRkys9quq2KpVDI0kUNwb5eF8mZJQyHJgGh6t9tdWZlPsVDbpN6tfjexWbmmYQgCUC2LMIAAG1CBsYgcxh1oWOQVg8hTKQoHIgtELIT/OvouJgGxefzhB0f8yoFbmw18JJ2cXyxfjE1xuVgjAS0zE3UtRyjyroNWgIyi/CjdYJp2rw4PD48uAtRsNhMzkfDG1llRzGMAQQ54S83zHM+fHRyee0JDxYO2f7WEB1IZxGmmy4Vaq24Y1UqlWFA1hUe5RTiBKuJgjZdJkgBCWAr4rMm1okbikM0qlCSYXtnpkns+CuZ/NJASfHzPl25ApEqtHqRQMButTvftl9/I8yx0HBuWO51Ot7vSMCW1Vza07OTkxI752urKSl2XFd00zVINyFYUQa3g/BHjgRQni3mWZXEc548M2+5I1uTAiZAh3hHFwqJyszaQkjqHE4vn45H0lBOmiIKFF+aFZ2HruE5m6LK+lIPYdRyW53dYpP7C0o1ZVb7jOBGXDmtj5+jZi0kGCkahu9qs1GqVkiYLSKYOsOB0EqDPbLWgJAz4HCyDNE2FfSVJGMv7cwLBh5EUtqc63B5TTnc/vl0/SsVCs91JIVWr3dV+h083ddeyMAxoo9/vr62td6sFc7NRKaLxeBwwpb2xvt4p60a5Wq3WQbroTkHvkANsrfBl/67IGBjvqtje8G7geW7IEK9QL864chmAtjX0KpQ1RcATuzfEaezd7nFcz12C5zlxEuNqxVAUHacD1KC8/n8yQ+cI4VhMDroWp4DDGfhoJhdU0g6Izo/P5piWt1Y9sbna6nRajZKhCfrRBpTEAZ+BZcDI3MQ/N6WPnOHZDHZnY0nUhT7gEVA7/k8b0G8d3FK0UocMzVkZKC4uVfOtVzao1Gg2i3alMj1bL8ix2t5cr7f66xcVrXdYKI2uzFUDccAPc8kfsQghB6ei3UmA9w0Mrs6yDfg58r3M5cium9CI9Y5WBBD4uVDqsuyBBMwYElQKs7rMuy5CxQKrayh7WUd7CfMI8GxR35dJHs3nz2HaNGXvSVkUYaq+Z2DgLxbHGT4A01lMH3nEhpzvpvuxecyqInMWMW+0adhA1G5GKUc63Y3Kxsbmai4dyTa3qD4Bwj55gDOILPWZX5ilZZHL7nO3Vj6HmEMzNCOyIKS7dJJy785aq9loNPUjhjOt8K1SUJeny4tkTioUo55bYaMSRUDUpxyZAwyadV+vHNBQCQzbvrdg2bGhXCI38SIGgtHSo/l6nuddTCaTsKiUKUjXPI/yJAztihAni6k/K0v29ef8hJB7z9/hRPqgOWp29vKSRcujr5+OIspdylRgh5Tqa6tFpdm5cevmzZs3b6ynVfq9Xq/SW2lUq41ur+czTyWgHeHQAO7WTd+nkCogy3KrP/JRVCnE94+sWOFmdrn476UzdpQaf3Mg+TSObb9uo+oqRQgPih54djLbjnKh1lspVTu4g6KVc1uBwvNtRH+B7aMC/vpHdb701EajYZJ4dukhpcZppCjWyUMIAavXG6sEBdm6zo2z/tFDY1gvjt25+n/91aEQLVmmO53d9fV2t1oplysTWQgY3RAXFEmYvphepABO0ky9uyqG6wIy8dG1LNx4cvPPeGcZ5CVzp88++/LQpeBSBvaVA6Hcba1qfGXrWu5F2FqpjlO3222j3SxDOoirYpeg+sID2WfbXPwNeT73IQBwRw2c2MsIFxqP2YJzMcOiGMnHq9n+cjdNe0zVwnw7aQApTRRC/0kCDxn7fkwLmnbpJVipPj6Efq//dFQajRw+m0KyWZzFpyhYVLZl/pAgCYn64P/zvw+o3vZOVzg/Pz+dLOPF0FWRW42Vfr+/2eL0StkwDNPUIvuDP/iv//UbMnajhu4erqtOLpzk2zwarVarW8qoqqyEwJ3SSiizgJTPwKwtH7uwtahgLCdIt2VCJCYUWOqBN0o0aU6LxSL0ee/s7GyRCL1er8SLsqIZxeNgnUJe4AnQYAArN0s6jorbVW6w486XNtAjRUmBSK1KBW3jL+m6yruyelccirYTNDi4zys0HQFRfyVJJlfNLnqXdBgbWRH0gma5MUqE/d+pghcyUWbTp0+fnpnr6+stwIaSKLKcJQH2j70R+RVJwa6azM8ODw8Xtl0PYnO/jKCgJtOZy21ubW2pKVs0NzMkMGN5PtQKJZ2P3annebVm0niIQT3twMBcPHfVTYoVBBNUyuUHoPDkyK8TwVumrTM5PTk5OXh2ej7N6tFD+EKkvN47qJQiCgiNW5CoBzWTaFUEqGWZDBQqR8cIpQQlZJw0Ci0ckgUGf1qEgCCWpznBt8HbtCfr7vYaZtbtkq6uqrIg8JQKFN2m3gNUrVUHymPAEQPAfx6MnCTFrisy4CgHecJBhOEulsHBbIAhYPx98vjTx59yV0KEhqipmU5ZxCAJHHI2LMtl/RS0FLY8ODgYu5lWq9aqFW2IKMX3MWQA3itBljCdVIMs9TyPZaY8FF0F1xAL8WU0L8YXF8cnJ+ezpe24ruP8aSwIVBjKjymXUlJggsF3qnA+n6eymXXryErTVBRFQCwNXNe9GHueSvQbUh5Pj46Oxm4UJwv8Uppxk6xUakNTKZIjB2SOEgDn6mO414QkdWzPDfI4e804TiDh9PnTZ8/PnTCHexhySGE5ebWq2QAEbXoZjCAPa08LmlVGjYXn+CkSFZmAg4ODCyb0+30TFzT0YcyGBhYECERJtNzXRZEgCNwATyftdsKwLDsh4KepU6oA7IPHjx+fZUBZvXnzZkcaWiSJAU8HZWVagwSIYmQDnPm+L5lg5olPkLyuAb2Dg4ODLz//NATtBx98dTI6OdILuTPJLBfDxCoIobs4HD5Q/+Unn3zyZDQaHQPQlbsp/OLk32IsjBRu/OGHH350iBPxRm4U2nS07vWbm1tbDTGrTaPBmVKa5zVQm0Y2ijvXzcVpOBqN7CFIBkYWRNrG7e9744033njrzp23b99++zt3Dr86pYDsVZKro+PnT548eTFP0zhdnjx79mzkhLlVYiyjMq+USqUCyVKMUlA0cXivWR2rUUmeBJ7n+Qbvwk5LfSrXc4OBgryYOnBs23FDwJBNsONvttuNIiyK1XqIyNOfnp2engJFUmT6PS54BQFq/Vn47x7/pavDj3rvN2EoMI9xzgjOshSB+Or04Msvvzx0zp37HFU5HXIfUGuf/p//8z+pYJEnptA9MQQyUUEpDWbISvmmJKoFzTGSIOF1Z9MMcZSjZv/Wq6+++vKWyaZfffXVLOLqmxvb233dGpgkvMRBNiBphhG8V4mjNItst05ynhwV4zgWO53uSiFHDW8Z9t54883XQBrGx8cnJ+O5V9Tm41lUEVe7R4d4kxqV8P+Tkv/FYfDIHz4O7L41/pRkBIzrKPVqvVzwNsdxIPFdYhMjjjZvHdhTcvtxlq+PzyazBXXwv/lEk1uAWgOd+VZKpMukJIuUE8iAI6JoKtBCuC5amv1ZF8dz7cVstvRyNCAYM5bvIY2lHTfUYn+QshSLiiZCTdnl8y8eP3782XOHr9+6dUsD/vTw6ODgdMjFcRABNlwlKM0ZYH80hGLMqzJGpgq2u1xbOxmfncVGwWET2QKojAji6vQp+iOYY+nRpljRa7oiEgn57XL/tEnC23QNw01fBISkJ0+//PrLQ9txlq1WvYqw2l0k+UxUEMguhg8K//J/ff755589OTs/PzNL+IX2vHn/M+yHbpvBMcPePj8U/8MhhImJu/5texr2efkHWmZv2D5Adx83q4NfkydvJHDYsxiYyMgvx4Z9lEMNbXJkFIe5e+8agqoHFDybTCYxNqpVZQhxYHFZlikufgBSryxogOsKNpTebLU1dz6fXAZBGBiGIivigHP8JE0Stws/W7lh+6G/aC7ZQYyBlrOcEXxKUpkDLM8TwIsI1RQbcZFtA2ztSo2NOvGiiufg9IHOMg/V118EJrTK4udCB/wq+VuUF32CV4AsiZQx8eYPoTedXEynl9PpdBn6JFWeM8Sp+FOTcLjLIQFxPBmAfFhKJEmSVfHcTytjgRMx3/fDIcADkuZxkAbSymu1WhV1hhAAGh8GHnU4v5oHSKl6WmspdZdLOwe1GhCtPw0vTh9xhKYMZKbIa95Om3IEvwwTvhcR4oQMMES7Lc00jVq1VLLDtPHk9Go2cxxeFyHzHVcd8JaC2mwlB5jj8lK+LH/Q0JoYVYBAhKlz6wqBkEjQFMNFrajlc2YBeJ8SHmUZuyq+ok9lNBpiELwG0TgIPdtL0iQb6ixJMBHEQAXh6GjacBewd/Xg0qds0cnJyfHJi+MRJHD/tSNT1ZXcdV0mV7VhGVEq0BRe0PNSqyaapsmDeD5Dlye7aRkCoVgsFpThrs7JqoIQyhClBGFK8XchhAMxjaPySFn23TCs3Qrn4/F4GeavQ0PhGkxPT87Ozi7dhIiiiK5sLgiDIARXlgUhG4JsD8B7MiteTpjnkBCCHBa5oYDdRhYKzp29WGaUiFN1L+g+mb2HCPEtmqapClwy3LiCtvHIoCbJDsxTF9UwOye0S7l+bp0HvyHwWwzykIBQrnlRXMd1K4sEXs3jULmQLTnMcLnERZtr0bRKBVPxGT7AIVMfcVMjbkM9DHNsCNemtU+/ieleuOXrsmfoOO/CoyjoUbWsXNnglPa/RYa//gZ25izvdo32aRqHy0GG4Fy09V7mMtbhtc1yEeywBdDOgT2c68JH+Lk3XOj9ptP9sqDBZ9zw0TmhoEQUV27ZTFFccJzRtu/dctkXCB8OVTG4uYUkmOMgMk/kTa4adpHxhNohl5TNWz+UZlv0p+yjG7DC8ZOIeCBJ7IzgODOXzTmqPT/XsKttntQOscRsRiIQkbfkU1bL8ptYPaC11FxLZr9WxG3mET5LKLKKbEhW0ObaQnqgy98/JuVM934BRmrqXDab0/WgaK5uUNfr1ElEpEXDP2BAjWhfqq/1PqJnwLx+f7ipqUn3K0SU6TSwLEf4Q83NzfV18CeebvS9o4nCx4I21y6RWddBEv1ex4rne9FkzpMi8VSRtWYinI06vyToU2QI5i9FVuyMXW1oCalp5MimQAIT1EFXqKpqhqOqTLlcBok3vkIPu7u7i8nAqFFDm0MoFPKFEvep/B1cCzWPCmDEKPZrxjpE/92a4U4WDK/HCFaq2z0BXdelu/WyyRirhv15QuGVBw8eQLEzmYytVBuGUQ1fgu1CEx4t3DRy5MimsNZ5pegz29KXzguvv37E8FYjEgp5bdssPtTrM6InWMV2O7hrV+wO5lhOB2x9QiqvaRs8IcOoN6ql6o5yWfLWahVlKjnYaiFTp25m0qlUytTbhJZQy2UHzuAbb7zR/7DcKZPv0SoswThjSS6mfsUJxswy5yRQIgun4WpJCBT9/Wokop4gYR0wTRNJSahapWLFsd0kK1BzvMb6NY5Hxf6iGrLuYcWTVprqNG71dD2qerRETipx4owneYYos0uku4URiQRV5M1+gnq4QXHiLSBJdCFoBPEOYxcARWI8hhSSReag6irOOJHIBRb0Vo9J7F6wN9XTDyMaqg7FDAykcs8lkxqxKkipIYlIlklqLT8QMaK6B7nU/VQOtYb2IU/phZwiWTKdiq82otT6OqXMjjtZ1ZJogs+bhV0FIZgg8hpGAMXeIgLGe06RE0GYWHiQTqfNn0Uxsiwrn8vni6YUCAScyMPFtL4KpfkhDQ0N4VA4HK2Pjx07tgnRxPhHHnnyhVS5FosAVrg/WCiTizb49guP7HU4JctOaB1Wuol1B6BAlHHbkLa2lgbd5/PFSPH7/fa1CTlnmqanUsPK9vb2ltktsVB4mH7tv3/+85/+/NdHnn7mkf+Si/bsa+nC3tBrH+pY8WFfX58VD1BJqPYTKF8olCwRDAZdlBJRjlnNM3lhBYiIPQx/xUNsoL+shkMeDwuUSyLS2qLrAUQT94TtEHPN5Ix/JzVEsqll0GiK3plxMeEvlDRNdi0zm88XK27nXOXRGVUkEmmsq61jRu33eTxeVRWy4ZU7+bIvKOgJHKVoCVUX9OmKoaGcqauWrUI+B80vR9vaWg1oiTGqTGq9NbVB34orW5SVQCBQwoSRvawq3C0zqbSSQ8yKOrsx2Sd6pPA6QJbj8eawPhMIrWH4pLhFwBdVs4hcq3y10bQpVbW3t4+ZOmvGwkltrW1t0dwrr3X3p7MhvKZUKjla3cgJD/0YjR07bty4ceNnLJnTHuyIPCzVxRpKTDe2tTa1DB01emijrhCiCb30oOvNt9NZbW+tkhSu9XFQxX2vp3w/9fqLz73WV+moLRZcJglJCeh1RjQalXqfemzwPbJb6HtYst0OcotCvCeqSCaESJCoUEVOdOkYZxSrgl9JUYMBIceHekFVVhBK1JomriJrYNmmBUksYqxrAlK9QS6Ml8qB2NKYOTjljwyMSl5ouI/qai/yWCzBV1PjFYJsi0RSyM8iVCb9MhuItpVN4n6Ay0jCgvqP//XQ3uCsvIfzHkV+45136ma6b31jdsj9rJMSP+CfjzBYaU1ZDZzcqT0hwu5nbVX8gPeAdNcvKI89LI4R7MdoZSGMoApGsAJaaQVG0xz3GfYetLKaviGfg2HsGQxnDsazeoyYrQSlsIBdQ5z9BjH2Odfk30Yd9gFUs73wrscEI6achfHst67DfgTXP5ruz2mzYfswQroxnLtogyx46FOIM1J0aTxi9ACcatGi6MJFiPrACahWbFgJPsaH0ytYT3Jcm95GnI7BRxswnjZiOKXB6Ccg9jkslH4FP/3CfZU+gRHGIO2Gfw2z6BOI0+/RyicixrYiJm1DjN9GTNqEGN/TILuLpTUzK/8YGWNlU2GY6zQ80za9p9HKh+ujzY51ooX+imr6DKbRExhPP0Ad9SoHPYE45df1XZhLf0Ej/Qxt9DQ0+j3GUi9m0+Bb1lWYSH0YBVthJqyCdlgCs2FZPvLhmQ7YAUdhGByDhTALRsO09UnScDSQjGZUUA8TYZIRQRlx0vFBBRnESMcQGooRqMP2o5ltQdsrEWY30czOIczOYJnsKGJIA/gbZuBVzJ5pZyGLNbQUYzY1tBExPIZ5zTZoIQzG0cDnKFb6YPpkLElvoBYw0muw5+A2Wosxi8DfMRp/xRgKYyzeqwSxR7CK/RzztNXewHQ2R7rjE36BHlQFgPBkyjAFwEGSFxVfZzWrTMI4ejOTIUBPZ3Isog/HSqczBWJ0JVPGGFpWHcAUMm4CAjSSG5kEH6wYRjsHcTYTkIQJ+C8yBTT80StjnNTDiEwFVbiDV6A9K8B7M5EcvD6MkhIZEauyL9egdTnwfpmhN24DzuI09uMMluMS9uMUjuMgFuIsLuD0Bx5Ov4SluMLdhZ/BIYzGApzFKRyaRUz/0BtxGKdxPOxmxNphNRw6/WWcXkWsDTa4jnb4UwddqzDjURzDJWzF4RW+BV7COVzEDDnGXWcb/sM4gyMGeC5ZDu74L+IwxrBq/lHGIYAt/gu4iOOH+WaZqvlxsqSHLA4/E2uwFmuwOIoGh/mGDWibfXM3ezPSlp/D9Y5zx9uMCdyMnYxmbAIcbtBA1zoEnsUJHMbBjnc+LlslnnZdbPiH4SiOdwMv4wDG4KC384/FQuw/4NlvHRUXscSa+YsnwjLhh0txW9AX3c8X/MIT9a/JfcePpQ8AAA==) format("woff2")}
*{box-sizing:border-box}
:root{--paper:#eadfc4;--card:#f2e9d4;--panel:#ddd0ae;--ink:#1c1813;--soft:#4a4136;--oxblood:#6e2a2a;
  --line:rgba(28,24,19,.18);--line-soft:rgba(28,24,19,.10);--edge:rgba(28,24,19,.55);
  --term:#17130f;--term-edge:#2a241d;--term-ink:#efe7d8;--term-command:#fff8ea;--term-prompt:#8f8677;--error:#6e2a2a}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);
  font:400 18px/1.55 "Cormorant Garamond","Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
::selection{background:#1c1813;color:#eadfc4}
main{max-width:760px;margin:0 auto;padding:32px 24px 64px}
.top{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:0 24px;margin:0 0 32px}
.brand{display:inline-block;padding:12px 0;margin:-12px 0;color:var(--ink);text-decoration:none;font-size:2rem;font-weight:500;letter-spacing:-.03em;line-height:.88}
.brand:focus-visible{outline:2px solid var(--ink);outline-offset:3px}
.back{display:inline-flex;align-items:center;min-height:44px;color:var(--ink);text-decoration:none;border-bottom:1px solid var(--line);
  font:600 10px/1 "Cormorant Garamond","Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  text-transform:uppercase;letter-spacing:.20em}
.back:hover{border-bottom-color:var(--ink)}
.back:focus-visible{outline:2px solid var(--ink);outline-offset:3px}
.arrow{font-family:ui-monospace,"SF Mono","Cascadia Mono","Liberation Mono",Menlo,Consolas,monospace;font-size:12px;letter-spacing:0;margin-right:8px}
h1{margin:0 0 16px;font-size:clamp(2.5rem,5vw,4.1rem);line-height:1.02;letter-spacing:-.025em;font-weight:500}
h2{margin:0 0 16px;font-size:clamp(1.9rem,3vw,2.6rem);line-height:1.04;letter-spacing:-.02em;font-weight:500}
p{margin:0 0 16px;max-width:66ch}
a{color:var(--ink);text-decoration-color:var(--soft);text-underline-offset:2px}
a:focus-visible{outline:2px solid var(--ink);outline-offset:3px}
.lede{font-size:1.1rem;line-height:1.5;color:var(--soft)}
/* One contained plate: the same painting, a lower detail, no frame, no caption. */
.plate{display:block;width:100%;height:clamp(120px,18vw,180px);object-fit:cover;object-position:center 70%;
  margin:16px 0 24px;background:var(--ink)}
section{margin:48px 0 0;padding-top:24px;border-top:1px solid var(--line)}
.caption{margin:0 0 8px;max-width:none;font:600 9px/1.2 ui-monospace,"SF Mono","Cascadia Mono","Liberation Mono",Menlo,Consolas,monospace;
  letter-spacing:.20em;text-transform:uppercase;color:var(--soft)}
.state{display:inline-block;margin:0 0 12px;padding:4px 9px 3px;border:1px solid var(--ink);color:var(--ink);
  font:600 9px/1.2 ui-monospace,"SF Mono","Cascadia Mono","Liberation Mono",Menlo,Consolas,monospace;
  letter-spacing:.20em;text-transform:uppercase}
.state-unavailable{color:var(--oxblood);border-color:var(--oxblood)}
.report-card{background:var(--panel);border:1px solid var(--line);padding:24px;margin:0 0 16px}
.report-card>p:last-child{margin-bottom:0}
table{width:100%;border-collapse:collapse;margin:0 0 16px}
th,td{text-align:left;vertical-align:top;padding:12px 16px;border-top:1px solid var(--line-soft)}
thead th{font:600 .72rem/1.3 ui-monospace,"SF Mono","Cascadia Mono","Liberation Mono",Menlo,Consolas,monospace;
  letter-spacing:.12em;text-transform:uppercase;color:var(--soft)}
.checks thead th{border-bottom:1px solid var(--oxblood)}
.checks tbody th,.checks tbody td{border-top:1px solid var(--line)}
.report{table-layout:fixed}
.report th{width:8.5rem;padding-left:0;font:600 .78rem/1.4 ui-monospace,"SF Mono","Cascadia Mono","Liberation Mono",Menlo,Consolas,monospace;color:var(--soft)}
.report td{padding-right:0}
.report tr:first-child th,.report tr:first-child td{border-top:0}
.report a{display:inline-block;min-height:44px;padding:9px 0;margin:-9px 0}
.checks th:first-child{width:60%}
code{font-family:ui-monospace,"SF Mono","Cascadia Mono","Liberation Mono",Menlo,Consolas,monospace;font-size:.84em}
.mono{font-family:ui-monospace,"SF Mono","Cascadia Mono","Liberation Mono",Menlo,Consolas,monospace}
.nowrap{white-space:nowrap}
.measure{display:block;overflow-wrap:anywhere;word-break:break-all}
pre{margin:0 0 16px;padding:16px;background:var(--term);border:1px solid var(--term-edge);color:var(--term-ink);overflow-x:auto}
pre:focus-visible{outline:2px solid var(--ink);outline-offset:3px}
pre code{font-size:.86rem;line-height:1.6;color:var(--term-command);white-space:pre}
/* The page's selection is paper on ink; on the terminal that paints ink on
   near-black (1.05:1) and the highlight vanishes while a reader copies the
   fingerprint. Paper on ink inside the terminal instead. */
pre code::selection{background:var(--term-ink);color:var(--term)}
/* The access report is one long line per check: wrap it, never scroll the page. */
.report-pre code{white-space:pre-wrap;overflow-wrap:anywhere}
/* The prompt is decoration: a copy of the command must paste without it. */
.prompt{color:var(--term-prompt);user-select:none;-webkit-user-select:none}
.note{margin:0 0 12px;font-size:.92rem;line-height:1.5;color:var(--soft)}
.gaps{list-style:none;margin:0;padding:0}
.gaps li{display:grid;grid-template-columns:2.2rem minmax(0,1fr);gap:0 12px;margin:0 0 24px;max-width:66ch}
.gaps .num{font-size:1.3rem;line-height:1.15;color:var(--oxblood);font-weight:500;padding-top:2px}
.gaps strong{display:block;margin-bottom:3px;font-size:1.06rem;line-height:1.3;font-weight:600}
.foot{margin:32px 0 0;padding-top:16px;border-top:1px solid var(--line);font-size:.92rem;color:var(--soft)}
.foot a{display:inline-flex;align-items:center;min-height:44px;margin-right:12px;vertical-align:middle}
@media (max-width:560px){
  body{font-size:17px}
  main{padding:24px 18px 48px}
  .top{margin-bottom:24px}
  .plate{height:120px;margin:16px 0 24px}
  section{margin-top:32px;padding-top:24px}
  .report,.report tbody,.report tr,.report th,.report td{display:block;width:auto}
  .report tr{border-top:1px solid var(--line-soft);padding:12px 0}
  .report th,.report td{border:0;padding:0 0 4px}
  .report td{padding-bottom:4px}
  .checks thead{display:none}
  .checks,.checks tbody,.checks tr,.checks td{display:block;width:auto}
  .checks tr{border-top:1px solid var(--line-soft);padding:12px 0}
  .checks td{border:0;padding:0 0 4px}
  .checks td:last-child{color:var(--soft)}
  .checks th:first-child{width:auto}
  th,td{padding:8px}
  .gaps li{grid-template-columns:1.9rem minmax(0,1fr);gap:0 8px}
}
</style>
</head>
<body>
<main>
  <header class="top">
    <a class="brand" href="/">Odile</a>
    <!--demo-->
    <a class="back" href="/judge"><span class="arrow" aria-hidden="true">&larr;</span>Back to the demo</a>
    <!--/demo-->
  </header>
  <h1>Verify</h1>
  <!--demo-->
  <p class="lede">This is Odile's demo machine: one shared workspace on our measured AMD SEV-SNP machine. Odile holds this key and can log in. It is not a tenant machine.</p>
  <!--/demo-->
  <!--tenant-->
  <p class="lede">No one at Odile can log in to a tenant machine.</p>
  <!--/tenant-->
  <!--demo-->
  <!-- Rachel Ruysch, Still-Life with Flowers, c. 1717. Public domain. Lower detail crop; decorative, no caption. -->
  <img class="plate" src="/bg-ruysch.webp" alt="" width="1400" height="875" decoding="async">
  <!--/demo-->

  <!--demo-->
  <section aria-labelledby="check">
    <h2 id="check">Check it yourself</h2>
    <p>Check the machine before you trust it: the verifier prints PASS or FAIL.</p>
    <p class="caption">From your laptop</p>
    <pre><code><span class="prompt" aria-hidden="true">$</span> curl -sSLO https://odile.fyi/odile-verify.tar.gz
<span class="prompt" aria-hidden="true">$</span> tar xzf odile-verify.tar.gz
<span class="prompt" aria-hidden="true">$</span> cd odile-verify &amp;&amp; ./odile-verify https://odile.fyi</code></pre>
    <p class="note">The verifier needs Python 3 and the <code>openssl</code> binary. It fetches one URL: <a href="/attestation">/attestation</a>.</p>
    <table class="checks" aria-labelledby="check">
      <thead><tr><th scope="col">Check</th><th scope="col">Where it is checked</th></tr></thead>
      <tbody>
        <tr><td><code>chain</code> — AMD VCEK <span class="mono">&rarr;</span> ASK <span class="mono">&rarr;</span> ARK, ARK <span class="nowrap">SHA-256</span> 69:d0:63:b4:<span class="mono">&hellip;</span>:9a:54:0b:cd</td><td>checked from your laptop</td></tr>
        <tr><td><code>signature</code> — ECDSA P-384 over the report, with the VCEK</td><td>checked from your laptop</td></tr>
        <tr><td><code>pin</code> — the signed measurement equals <span class="nowrap"><code>odile-verify.pin</code></span></td><td>checked from your laptop</td></tr>
      </tbody>
    </table>
    <p class="note">Each row is tagged: checked from your laptop, or read from the machine. A root shell can change the machine rows.</p>
  </section>
  <!--/demo-->

  <section aria-labelledby="report">
    <h2 id="report">The report</h2>
    <div class="report-card">
      <p class="state state-{{REPORT_STATE}}">{{REPORT_LINE}}</p>{{REPORT_CAUSE}}
      <p class="lede">The machine is an AMD SEV-SNP confidential VM. The report is served live at <a href="/attestation">/attestation</a>. The boot is measured: firmware, kernel, initramfs and command line are hashed into a value the CPU signs.</p>
      <table class="report" aria-labelledby="report">
        <tbody>
          <tr><th scope="row">fetched_at</th><td><code>{{FETCHED_AT}}</code></td></tr>
          <tr><th scope="row">measurement</th><td><code class="measure">{{MEASUREMENT}}</code></td></tr>
          <tr><th scope="row">report format</th><td><code>{{REPORT_META}}</code></td></tr>
          <tr><th scope="row">raw JSON</th><td><a href="/attestation">/attestation</a></td></tr>
        </tbody>
      </table>
      <p class="note"><code>fetched_at</code> is when the CPU produced this report.</p>
    </div>
  </section>

  <!--bench-->
  <section aria-labelledby="access">
    <h2 id="access">The access report, from a test machine</h2>
    <p class="caption">Read from the machine, 10 November 2026</p>
    <pre class="report-pre"><code>verify_and_unlock — access report

  usable keys    PASS  0 usable user keys; every authorized_keys line is the measured boot key (SHA256:LLwDfYyE3VMc9T9o+XvydfNzrXxyzaQ2lysSxCSSEWA)
  boot key       PASS  ssh-ed25519 SHA256:LLwDfYyE3VMc9T9o+XvydfNzrXxyzaQ2lysSxCSSEWA; public key only, no private half of it on the machine
  seal           PASS  policy 0x30000; seal_id strict-bench-v1; measurement 519c7c9aae54bf139cacd783e1b69bd98b01e79b9e8b52feb41aadb7a483f481227a8a3b64ea2b885234ca56e09c9ee8
  sign-in hash   PASS  scrypt hash present in /home/odile/.hermes/.env; no plaintext password
  operator key   PASS  none (authorized_keys: 1 line(s), the measured throwaway)

  PASS — five checks: zero usable user keys, the throwaway boot key, the seal, the sign-in hash, no operator key.</code></pre>
  </section>
  <!--/bench-->

  <!--machine-->
  <section aria-labelledby="access">
    <h2 id="access">The access report, from your machine</h2>
    <p class="caption">read from your machine <span class="mono">&middot;</span> <time datetime="{{REPORT_AT}}">{{REPORT_AT}}</time></p>
    <pre class="report-pre"><code>{{REPORT_TEXT}}</code></pre>
    <p class="note">A root shell can change the machine rows.</p>
  </section>
  <!--/machine-->

  <!--noaccess-->
  <section aria-labelledby="access">
    <h2 id="access">The access report</h2>
    <p class="state state-unavailable">Report unavailable.</p>
    <p class="note">{{ACCESS_CAUSE}}</p>
  </section>
  <!--/noaccess-->

  <section aria-labelledby="gaps">
    <h2 id="gaps">The three gaps we state before anyone asks</h2>
    <ol class="gaps">
      <li><span class="num" aria-hidden="true">1</span><div><strong>The app layer is not hardware-measured yet.</strong> Measured boot covers firmware, kernel, initramfs and command line. Hermes, the installer and the skills are pinned by a signed manifest and a published hash, but the CPU does not measure them. The installer is signed, inspectable and audited at handoff for keys, passwords and keyslots; only a measured image can prove there is no hidden backdoor. The manifest records the machine as provisioned; anything you install afterwards is yours.</div></li>
      <li><span class="num" aria-hidden="true">2</span><div><strong>The console passes through the host.</strong> Serial and VNC can be watched. Never type a secret there; the sign-in goes over TLS to the guest, after the report checks out.</div></li>
      <li><span class="num" aria-hidden="true">3</span><div><strong>Odile keeps three powers.</strong> We can stop or delete the machine (availability, not confidentiality); we control DNS for your slug (pin the certificate fingerprint in your recovery file — the one your machine first served; a renewal changes it) and we can set a different boot image from the panel (detectable via the measurement — compare it to the one in your ready email before you set your sign-in).</div></li>
    </ol>
  </section>

  <p class="foot"><!--demo--><a href="/judge">Back to the demo</a> <span class="mono">&middot;</span> <!--/demo--><a href="/">odile.fyi</a></p>
</main>
</body>
</html>
'''
ACCESS_REPORT_SCRIPT_SHA256 = "c85b4c370b4ef0c44b8a9b5b35d5a0c348155a5b120a7064a4a8e3179caef20b"
ACCESS_REPORT_SCRIPT = r'''#!/bin/sh
# access-report.sh — write the machine's own /verify report (ODL-473).
#
# The boot oneshot runs this as root. It resolves the pinned measurement the
# user verified, then runs the strict bar's report exactly as the card names
# it and writes the file /verify reads:
#
#   verify_and_unlock.sh --mode access --root / --user odile --expect <pin> \
#       --out /var/lib/odile/access-report.txt
#
# Where the pin comes from, in order:
#
#   /etc/odile/tenant-image.pin   uki_signed.measurement — the image's own UKI
#                                 measurement, when a release cut places it
#   /etc/odile/boot-record.json   measurement — the number the user passed to
#                                 `odile_install.py --expect` (ODL-396)
#   /var/lib/odile/attestation.json  measurement — the boot report the machine
#                                 produced; the fallback when nothing pinned it
#   /etc/odile/odile-verify.pin   measurement — the demo/release pin, if a
#                                 tenant copy was ever re-pinned to this image
#
# A pinned number that does not match the report fails the seal line, which is
# the point: the report says `the measurement is not --expect`. No pin at all
# exits 2 and writes nothing, so /verify says "not written yet" instead of
# showing a report checked against nothing.
#
# Read-only on the machine: the reporter reads /proc/cmdline, the attestation
# document, every authorized_keys and the tenant .env; it writes only the
# report file (0644, atomic). The unit re-runs after the first visit, when the
# tenant's hash replaces the placeholder.
#
# The env overrides are the bench's: a guest root, a report path and a pin
# list inside a temp directory.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

ROOT=${ODILE_ACCESS_ROOT:-/}
TENANT_USER=${ODILE_ACCESS_USER:-odile}
OUT=${ODILE_ACCESS_OUT:-/var/lib/odile/access-report.txt}
REPORTER=${ODILE_REPORTER:-/usr/local/lib/odile/verify_and_unlock.sh}
PIN_FILES=${ODILE_ACCESS_PIN_FILES:-/etc/odile/tenant-image.pin:/etc/odile/boot-record.json:/var/lib/odile/attestation.json:/etc/odile/odile-verify.pin}

found=$(/usr/bin/python3 - "$PIN_FILES" <<'PY'
import json
import re
import sys

HEX = re.compile(r"^[0-9a-f]{96}$")


def value(node):
    """A 96-hex measurement under this JSON object, or None."""
    if not isinstance(node, dict):
        return None
    for key in ("measurement", "expected_measurement"):
        candidate = node.get(key)
        if isinstance(candidate, str) and HEX.match(candidate.strip().lower()):
            return candidate.strip().lower()
    uki = node.get("uki_signed")
    if isinstance(uki, dict):
        candidate = uki.get("measurement")
        if isinstance(candidate, str) and HEX.match(candidate.strip().lower()):
            return candidate.strip().lower()
    return None


for path in sys.argv[1].split(":"):
    if not path:
        continue
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        continue
    measurement = value(document)
    if measurement:
        print("%s\t%s" % (path, measurement))
        break
PY
)
if [ -z "$found" ]; then
    echo "access-report: no 96-hex measurement in ${PIN_FILES}" >&2
    exit 2
fi
PIN_FILE=$(printf '%s' "$found" | cut -f1)
PIN=$(printf '%s' "$found" | cut -f2)
if [ -z "$PIN" ]; then
    echo "access-report: $PIN_FILE has no measurement" >&2
    exit 2
fi

echo "access-report: $PIN_FILE pins $PIN"
echo "access-report: verify_and_unlock.sh --mode access --root $ROOT --user $TENANT_USER --expect $PIN --out $OUT"
exec "$REPORTER" --mode access --root "$ROOT" --user "$TENANT_USER" --expect "$PIN" --out "$OUT"
'''
VERIFY_UNLOCK_SHA256 = "7cff6f470e3d1fbd938f92982012bf29132ebc6fc7c16389a3c6f482a1c49caa"
VERIFY_UNLOCK = r'''#!/usr/bin/env bash
# verify_and_unlock.sh — the first-boot ceremony's one command.
#
#   ./scripts/verify_and_unlock.sh --host 203.0.113.87 --release ./2026.11.1 \
#       --boot-key ~/.ssh/odile --mode encrypt
#
# The machine waits at Servury's measured initramfs. Nothing on it may receive
# the disk passphrase until the boot is checked, so this script checks, in
# order, and stops at the first failure:
#
#   1. the release directory is the one `odile-verify.pin` names (three
#      artifact hashes and RELEASE.json, not the unsigned SHA256SUMS alone);
#   2. `blackbox-verify` recomputes the launch measurement from that release
#      and the machine's own command line, checks the AMD chain and report
#      signature, and checks that REPORT_DATA is the SHA-256 of the host key
#      the machine sent;
#   3. the boot key in the measured command line (`bb.key`) is the user's key,
#      and the address in it (`bb.ip4`) is this machine;
#   4. the host key on the wire is the one the report binds;
#   5. `odile-verify` reads the saved report and checks the AMD chain, the
#      policy, the pinned release and the expected number, using the AMD VCEK
#      for this chip (fetched from AMD's key server once and cached under
#      ~/.cache/odile-ceremony, or given with --amd-dir).
#
# Only after all five does it write known_hosts, and only then does it open the
# initramfs command: `encrypt` on the first boot, `unlock` on later ones.
#
# The passphrase is never an argument, never read by this script and never
# logged. It is typed by the user at the initramfs prompt over `ssh -t`. No
# command here puts it in argv, a file or the wrapper's output.
#
# Exit codes: 0 pass, 1 verification failed, 2 usage/precondition,
# 4 transport (ssh/keyscan) failed, 5 host key mismatch.
#
# `--mode access` is the strict bar's report, and it is the one mode that runs
# on the machine itself (or on a bench root): it reads the measured command
# line, the signed report bytes, every account's authorized_keys, every private
# key in the writable state roots and the tenant's Hermes .env, and prints one
# PASS/FAIL line for each of the five access facts the claims re-cut cites. It
# needs no ssh, no release directory and no network. `--out FILE` writes the
# same report for /verify to read: one provenance line, then the report, 0644
# and atomic. The boot oneshot passes it; a human run does not need it.
#
# `--self-test` needs no machine and no network: a local sshd-shaped stub and
# stub verifiers exercise every assertion and the fail-closed paths.

set -euo pipefail

PROG=${0##*/}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)

# The audited sha256 of Servury's verifier, from docs/CLAIMS.md §4.4. If
# Servury republishes the file, this constant and that line move together.
AUDITED_BLACKBOX_VERIFY_SHA256=8c937f02a94e83e7618a98b16a961a0f09b309cefa4100abc1f05e994df30f97
KDS_BASE=https://kdsintf.amd.com/vcek/v1/Milan

HOST=""
RELEASE_DIR=""
RELEASE_ID=""
BOOT_KEY=""
PUBLIC_KEY=""
PUBLIC_LINE=""
# systemd services do not set HOME; the access mode runs as root there.
KNOWN_HOSTS="${HOME:-/root}/.ssh/known_hosts"
BLACKBOX_VERIFY="${PWD}/blackbox-verify"
ODILE_VERIFY="${REPO_ROOT}/odile-verify"
PIN="${REPO_ROOT}/odile-verify.pin"
VERIFIER_SHA256="$AUDITED_BLACKBOX_VERIFY_SHA256"
EXPECT=""
AMD_DIR=""
VCPUS=4
PORT=22
MODE=verify
# The verifiers need sevsnpmeasure and cryptography. An explicit --python or
# PYTHON wins; next the virtualenv the user activated (the guide's recipe, since
# Debian 13 refuses `pip install --user`); then the system interpreter.
PYTHON=${PYTHON:-}
if [ -z "$PYTHON" ]; then
    if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
        PYTHON=$VIRTUAL_ENV/bin/python
    else
        PYTHON=/usr/bin/python3
    fi
fi
ATTEST_OUT=""
RECORD=""
PREVIOUS=""
ACCESS_ROOT=/
ACCESS_USER=odile
ACCESS_OUT=""
SELF_TEST=0
SELFTEST_TMP=""

usage() {
    cat <<'EOF'
Usage:
  scripts/verify_and_unlock.sh --host <ipv4> --release <dir> --boot-key <file>
                               [--mode verify|encrypt|unlock|attest|report]
                               [--public-key <file.pub>] [--expect <96-hex>]
                               [--known-hosts <file>] [--record <file>]
                               [--previous <boot-record.json>]
                               [--vcpus <n>] [--port <n>]
                               [--blackbox-verify <file>] [--verifier-sha256 <64-hex>]
                               [--odile-verify <file>] [--pin <file>]
                               [--amd-dir <dir>] [--attest-out <file>]
  scripts/verify_and_unlock.sh --mode access [--root <dir>] [--user <name>]
                               [--expect <96-hex>] [--out <file>]
  scripts/verify_and_unlock.sh --self-test

Verify the initramfs boot, then run the initramfs command:

  --python <file>  Python 3 interpreter for the verifiers (default: the active
                   virtualenv's python, else /usr/bin/python3)
  --mode verify    check only; print the measurement and write known_hosts
  --mode encrypt   check, then `ssh -t root@<ip> encrypt`   (first boot)
  --mode unlock    check, then `ssh -t root@<ip> unlock`    (later boots)
  --mode attest    check, then print the raw attestation JSON
  --mode report    check, then print the raw SNP report

The strict bar's report runs on the booted machine, not from a laptop:

  --mode access    read the guest root and print the five PASS/FAIL lines:
                   zero usable user keys; the throwaway boot key with no
                   private half; the seal policy and measurement; the sign-in
                   hash; no operator key. Every account's authorized_keys is
                   read, and the private-half scan covers /root, every /home,
                   /etc/odile and /var/lib/odile. No ssh and no network.
  --root <dir>     guest root to inspect (default: /; a bench passes a tree)
  --user <name>    the tenant's Hermes user (default: odile)
  --out <file>     write the report to <file> (0644, atomic) and print it too.
                   The first line is `read from your machine, <UTC time>`; the
                   rest is exactly what the mode prints. A FAIL run writes the
                   FAIL lines and exits 1, so the page can show them. The boot
                   oneshot passes this; a hand run usually does not.

Paths default to the repo root (`odile-verify`, `odile-verify.pin`) and to
`./blackbox-verify` in the current directory. `--boot-key` is the private half
of the key in the measured command line; with an SSH agent, omit it and pass
`--public-key` (the public half) instead. `--expect` is the 96-hex measurement
to compare with; `--record` saves it as an odile.boot-record.v1 record and
`--previous` compares this boot with an earlier record.

The passphrase is typed at the initramfs prompt, never passed to this script.
EOF
}

die() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 2; }
failed() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
transport_failed() { printf 'FAIL: %s\n' "$*" >&2; exit 4; }
hostkey_failed() { printf 'FAIL: %s\n' "$*" >&2; exit 5; }

need_value() {
    [ "$#" -ge 2 ] && [ -n "$2" ] || die "$1 needs a value"
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --host) need_value "$@"; HOST=$2; shift 2 ;;
            --release) need_value "$@"; RELEASE_DIR=$2; shift 2 ;;
            --release-id) need_value "$@"; RELEASE_ID=$2; shift 2 ;;
            --boot-key) need_value "$@"; BOOT_KEY=$2; shift 2 ;;
            --public-key) need_value "$@"; PUBLIC_KEY=$2; shift 2 ;;
            --known-hosts) need_value "$@"; KNOWN_HOSTS=$2; shift 2 ;;
            --blackbox-verify) need_value "$@"; BLACKBOX_VERIFY=$2; shift 2 ;;
            --verifier-sha256) need_value "$@"; VERIFIER_SHA256=$2; shift 2 ;;
            --odile-verify) need_value "$@"; ODILE_VERIFY=$2; shift 2 ;;
            --pin) need_value "$@"; PIN=$2; shift 2 ;;
            --expect) need_value "$@"; EXPECT=$2; shift 2 ;;
            --amd-dir) need_value "$@"; AMD_DIR=$2; shift 2 ;;
            --vcpus) need_value "$@"; VCPUS=$2; shift 2 ;;
            --port) need_value "$@"; PORT=$2; shift 2 ;;
            --mode) need_value "$@"; MODE=$2; shift 2 ;;
            --python) need_value "$@"; PYTHON=$2; shift 2 ;;
            --attest-out) need_value "$@"; ATTEST_OUT=$2; shift 2 ;;
            --record) need_value "$@"; RECORD=$2; shift 2 ;;
            --previous) need_value "$@"; PREVIOUS=$2; shift 2 ;;
            --root) need_value "$@"; ACCESS_ROOT=$2; shift 2 ;;
            --user) need_value "$@"; ACCESS_USER=$2; shift 2 ;;
            --out) need_value "$@"; ACCESS_OUT=$2; shift 2 ;;
            --self-test) SELF_TEST=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) die "unknown argument: $1 (--help for usage)" ;;
        esac
    done
}

validate_args() {
    case "$MODE" in
        verify|encrypt|unlock|attest|report|access) ;;
        *) die "--mode must be verify, encrypt, unlock, attest, report or access (got '$MODE')" ;;
    esac
    case "$VCPUS" in
        ''|*[!0-9]*) die "--vcpus must be a positive integer" ;;
    esac
    [ "$VCPUS" -ge 1 ] || die "--vcpus must be a positive integer"
    case "$PORT" in
        ''|*[!0-9]*) die "--port must be an integer" ;;
    esac
    { [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ]; } || die "--port must be 1-65535"
    if [ -n "$EXPECT" ]; then
        case "$EXPECT" in
            *[!0-9a-fA-F]*) die "--expect needs 96 hex characters (48 bytes)" ;;
        esac
        [ "${#EXPECT}" -eq 96 ] || die "--expect needs 96 hex characters (48 bytes)"
        EXPECT=$(printf '%s' "$EXPECT" | tr 'A-F' 'a-f')
    fi
    if [ "$MODE" = access ]; then
        [ -d "$ACCESS_ROOT" ] || die "no guest root at $ACCESS_ROOT"
        case "$ACCESS_USER" in
            ''|*[!a-z0-9_-]*) die "--user must be a plain account name (got '$ACCESS_USER')" ;;
        esac
        if [ -n "$ACCESS_OUT" ]; then
            case "$ACCESS_OUT" in
                /*) ;;
                *) die "--out needs an absolute path (got '$ACCESS_OUT')" ;;
            esac
            [ -d "$(dirname -- "$ACCESS_OUT")" ] || \
                die "no directory at $(dirname -- "$ACCESS_OUT") for --out"
        fi
        return
    fi
    [ -z "$ACCESS_OUT" ] || die "--out only goes with --mode access"
    [ -n "$HOST" ] || die "--host is required"
    case "$HOST" in
        *[!0-9.]*|.*|*.) die "--host must be an IPv4 address (got '$HOST')" ;;
    esac
    case "$HOST" in
        *.*.*.*) ;;
        *) die "--host must be an IPv4 address (got '$HOST')" ;;
    esac
    [ -n "$RELEASE_DIR" ] || die "--release is required (the downloaded release directory)"
}

preconditions() {
    local cmd
    [ -x "$PYTHON" ] || die "no Python 3 at $PYTHON (set PYTHON=... or --python)"
    if [ "$MODE" = access ]; then
        # The report reads the guest root; it never opens a connection. The
        # tenant image installs no sshd and no openssh-client, so ssh-keygen may
        # be absent: the private-half scan then falls back to the .pub sibling
        # and fails "usable keys" for a private key it cannot identify, rather
        # than passing quietly.
        return
    fi
    for cmd in ssh ssh-keygen ssh-keyscan sha256sum openssl; do
        command -v "$cmd" >/dev/null 2>&1 || die "$cmd is not on PATH"
    done
    [ -d "$RELEASE_DIR" ] || die "no release directory at $RELEASE_DIR"
    local f
    for f in OVMF.amdsev.fd vmlinuz initrd.img RELEASE.json; do
        [ -f "$RELEASE_DIR/$f" ] || die "$RELEASE_DIR/$f is missing"
    done
    [ -f "$BLACKBOX_VERIFY" ] || die "no blackbox-verify at $BLACKBOX_VERIFY
  curl -fsSLO https://servury.com/downloads/blackbox/blackbox-verify"
    [ -x "$ODILE_VERIFY" ] || [ -f "$ODILE_VERIFY" ] || die "no odile-verify at $ODILE_VERIFY"
    [ -f "$PIN" ] || die "no pin at $PIN"
    if [ -n "$BOOT_KEY" ]; then
        [ -f "$BOOT_KEY" ] || die "no boot key at $BOOT_KEY"
    fi
    if [ -n "$PUBLIC_KEY" ]; then
        [ -f "$PUBLIC_KEY" ] || die "no public key at $PUBLIC_KEY"
    fi
    if [ -z "$RELEASE_ID" ]; then
        RELEASE_ID=$("$PYTHON" - "$RELEASE_DIR/RELEASE.json" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8"))["release"])
except Exception as exc:
    sys.stderr.write("cannot read %s: %s\n" % (sys.argv[1], exc))
    sys.exit(1)
PY
        ) || die "cannot read the release id from $RELEASE_DIR/RELEASE.json"
    fi
    case "$RELEASE_ID" in
        ''|*[!0-9A-Za-z._-]*) die "bad release id: $RELEASE_ID" ;;
    esac
    "$PYTHON" -c 'import sevsnpmeasure, cryptography' >/dev/null 2>&1 \
        || die "blackbox-verify needs Python modules sevsnpmeasure and cryptography
  python3 -m venv .venv && . .venv/bin/activate
  pip install sev-snp-measure cryptography
  rerun in that shell, or pass --python .venv/bin/python
  (on Debian/Ubuntu, python3 -m venv needs python3-venv: sudo apt install python3-venv)"
}

check_verifier_hash() {
    local got
    got=$(sha256sum "$BLACKBOX_VERIFY" | awk '{print $1}')
    if [ "$got" != "$VERIFIER_SHA256" ]; then
        failed "blackbox-verify is $got, not the audited $VERIFIER_SHA256
  If Servury published a new verifier, read it and move the pin in one commit."
    fi
    printf '  %-14s %s  %s\n' "verifier" "PASS" "sha256 $got (audited)"
}

check_release_against_pin() {
    "$PYTHON" - "$PIN" "$RELEASE_ID" "$RELEASE_DIR" <<'PY' || failed "the release directory is not the pinned release"
import hashlib, json, os, sys

pin_path, release_id, release_dir = sys.argv[1:4]


def sha256(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


try:
    with open(pin_path, encoding="utf-8") as fh:
        pin = json.load(fh)
except (OSError, ValueError) as exc:
    print("  %-14s %s  %s" % ("pin", "FAIL", "cannot read %s: %s" % (pin_path, exc)))
    sys.exit(1)

entry = (pin.get("releases") or {}).get(release_id)
if not isinstance(entry, dict):
    print("  %-14s %s  release not pinned: %s" % ("release", "FAIL", release_id))
    sys.exit(1)

artifacts = entry.get("artifacts") or {}
bad = 0
for name in ("OVMF.amdsev.fd", "vmlinuz", "initrd.img"):
    want = artifacts.get(name, "")
    got = sha256(os.path.join(release_dir, name))
    ok = bool(want) and got == want
    print("  %-14s %s  %s" % (name, "PASS" if ok else "FAIL", got or "missing"))
    bad += 0 if ok else 1

want = entry.get("release_json_sha256", "")
got = sha256(os.path.join(release_dir, "RELEASE.json"))
ok = bool(want) and got == want
print("  %-14s %s  %s" % ("RELEASE.json", "PASS" if ok else "FAIL", got or "missing"))
bad += 0 if ok else 1
if bad:
    sys.exit(1)
print("  %-14s %s  %s is pinned (%d artifacts, vcpu_counts %s)"
      % ("release", "PASS", release_id, len(artifacts), entry.get("vcpu_counts")))
PY
}

read_public_key() {
    if [ -n "$PUBLIC_KEY" ]; then
        PUBLIC_LINE=$("$PYTHON" - "$PUBLIC_KEY" <<'PY'
import sys
for line in open(sys.argv[1], encoding="utf-8"):
    parts = line.strip().split()
    if len(parts) >= 2 and parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
        print(parts[0] + " " + parts[1])
        break
else:
    sys.exit(1)
PY
        ) || die "$PUBLIC_KEY holds no public key line"
        return
    fi
    [ -n "$BOOT_KEY" ] || die "pass --boot-key (the private half of the boot key) or --public-key"
    PUBLIC_LINE=$(ssh-keygen -y -P '' -f "$BOOT_KEY" 2>/dev/null) \
        || die "cannot read the public half of $BOOT_KEY without a passphrase; pass --public-key"
}

fetch_attestation() {
    local -a opts=(-n -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
                   -o ConnectTimeout=15 -o LogLevel=ERROR -p "$PORT")
    if [ -n "$BOOT_KEY" ]; then
        opts+=(-i "$BOOT_KEY" -o IdentitiesOnly=yes)
    fi
    if ! ssh "${opts[@]}" "root@$HOST" attest > "$ATTEST"; then
        transport_failed "could not fetch the attestation from root@$HOST:$PORT
  The initramfs accepts only the private half of the boot key in its measured
  command line. Check --boot-key, the address, and that the machine is up."
    fi
    if [ ! -s "$ATTEST" ]; then
        transport_failed "root@$HOST:$PORT answered nothing to 'attest'"
    fi
    printf '  %-14s %s  %s\n' "attest" "PASS" "fetched $(wc -c < "$ATTEST") bytes over ssh"
}

check_attestation() {
    "$PYTHON" - "$ATTEST" "$TMP/boot.pub" "$HOST" "$RELEASE_ID" "$VCPUS" "$EXPECT" "$NORMALIZED" "$SUMMARY" <<'PY'
import base64
import hashlib
import json
import sys

(att_path, pub_path, host, release_id, vcpus_text, expect, norm_path,
 summary_path) = sys.argv[1:9]
vcpus = int(vcpus_text)
fails = 0


def row(label, ok, detail):
    global fails
    print("  %-14s %s  %s" % (label, "PASS" if ok else "FAIL", detail))
    if not ok:
        fails += 1


def fingerprint(blob_b64):
    return "SHA256:" + base64.b64encode(
        hashlib.sha256(base64.b64decode(blob_b64)).digest()
    ).decode().rstrip("=")


def read_public_key(path):
    for line in open(path, encoding="utf-8"):
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
            return parts[0] + " " + parts[1]
    raise SystemExit("no public key line in %s" % path)


pub_line = read_public_key(pub_path)
pub_type, pub_blob = pub_line.split()

try:
    with open(att_path, encoding="utf-8") as fh:
        raw = json.load(fh)
except (OSError, ValueError) as exc:
    row("attest", False, "not JSON: %s" % exc)
    sys.exit(1)

row("status", raw.get("status") == "ok", repr(raw.get("status")))

try:
    report = base64.b64decode(raw.get("report_b64", ""), validate=True)
except Exception as exc:  # noqa: BLE001 - a bad blob is a FAIL row, not a crash
    row("report", False, "report_b64 is not valid base64: %s" % exc)
    report = b""

if report:
    version = int.from_bytes(report[0:4], "little") if len(report) >= 4 else -1
    policy = int.from_bytes(report[8:16], "little") if len(report) >= 16 else -1
    vmpl = int.from_bytes(report[0x30:0x34], "little") if len(report) >= 0x34 else -1
    row("report", len(report) == 1184 and version == 5,
        "version %d, %d bytes" % (version, len(report)))
    row("policy", policy == 0x30000, "0x%x" % policy)
    row("vmpl", vmpl == 0, "%d" % vmpl)
else:
    version = -1
    row("report", False, "no report bytes")

hostkey = raw.get("hostkey", "").split()
if len(hostkey) >= 2 and report:
    blob = base64.b64decode(hostkey[1])
    bound = hashlib.sha256(blob).digest() == report[0x50:0x70]
    tail = report[0x70:0x90] == b"\0" * 32
    row("host key", bound and tail,
        "REPORT_DATA binds %s" % fingerprint(hostkey[1]))
    hostkey_fp = fingerprint(hostkey[1])
else:
    row("host key", False, "the attestation carries no ssh-ed25519 host key")
    hostkey_fp = ""

cmdline = raw.get("cmdline", "")
tokens = cmdline.split()

keys = [t for t in tokens if t.startswith("bb.key=")]
ips = [t for t in tokens if t.startswith("bb.ip4=")]
rels = [t for t in tokens if t.startswith("bb.release=")]
row("bb.key count", len(keys) == 1, "%d in the measured command line" % len(keys))

bootkey_fp = ""
if len(keys) == 1:
    try:
        line = base64.b64decode(keys[0].split("=", 1)[1]).decode().strip()
        parts = line.split()
        match = len(parts) >= 2 and parts[0] == pub_type and parts[1] == pub_blob
        if len(parts) >= 2:
            bootkey_fp = fingerprint(parts[1])
        row("boot key", match, "%s %s (the key you hold)" % (parts[0], bootkey_fp)
            if len(parts) >= 2 else line)
    except Exception as exc:  # noqa: BLE001
        row("boot key", False, "bb.key does not decode: %s" % exc)
else:
    row("boot key", False, "the measured command line must carry exactly one bb.key")

if len(ips) == 1:
    addr = ips[0].split("=", 1)[1].split("/", 1)[0]
    row("bb.ip4", addr == host, "%s (expected %s)" % (addr, host))
else:
    row("bb.ip4", False, "%d addresses in the measured command line" % len(ips))

served_release = raw.get("release", "")
row("release", served_release == release_id, "%s (expected %s)" % (served_release, release_id))
if rels:
    row("bb.release", rels[0].split("=", 1)[1] == release_id, rels[0].split("=", 1)[1])

measurement = report[0x90:0xC0].hex() if len(report) >= 0xC0 else ""
if expect:
    row("measurement", measurement == expect, "matches --expect %s" % expect
        if measurement == expect else "is %s, --expect says %s" % (measurement, expect))
else:
    row("measurement", bool(measurement), measurement or "no measurement")

with open(summary_path, "w", encoding="utf-8") as fh:
    fh.write("measurement=%s\n" % measurement)
    fh.write("hostkey_fp=%s\n" % hostkey_fp)
    fh.write("bootkey_fp=%s\n" % bootkey_fp)
    fh.write("hostkey_line=%s\n" % (" ".join(hostkey[:2]) if len(hostkey) >= 2 else ""))

if not fails:
    normalized = {
        "available": True,
        "guest": "sev-snp",
        "fetched_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "report_b64": raw.get("report_b64", ""),
        "report_size": len(report),
        "report_version": version,
        "measurement": measurement,
        "release": served_release,
        "cmdline": cmdline,
        "vcpus": vcpus,
        "hostkey": raw.get("hostkey", ""),
    }
    with open(norm_path, "w", encoding="utf-8") as fh:
        json.dump(normalized, fh)

sys.exit(1 if fails else 0)
PY
}

report_access() {
    "$PYTHON" - "$ACCESS_ROOT" "$ACCESS_USER" "$EXPECT" <<'PY'
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

root, user, expect = sys.argv[1:4]
root = os.path.abspath(root)
fails = 0
att_rel = "var/lib/odile/attestation.json"


def row(label, ok, detail):
    global fails
    print("  %-14s %s  %s" % (label, "PASS" if ok else "FAIL", detail))
    if not ok:
        fails += 1


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def guest(path):
    """The path a reader on the machine sees, not the bench root's prefix."""
    return "/" + os.path.relpath(path, root)


def key_line(text):
    """`type blob` for a public-key line, with or without options, else None."""
    parts = text.strip().split()
    for index, part in enumerate(parts[:-1]):
        if part.startswith(("ssh-", "ecdsa-", "sk-")):
            return part + " " + parts[index + 1]
    return None


def has_options(text):
    """True when the line starts with authorized_keys options, not a key type."""
    parts = text.strip().split()
    return bool(parts) and not parts[0].startswith(("ssh-", "ecdsa-", "sk-"))


def fingerprint(line):
    try:
        blob = base64.b64decode(line.split()[1])
    except (IndexError, ValueError):
        return "unreadable"
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def command_line_value(tokens, prefix):
    found = [token[len(prefix):] for token in tokens if token.startswith(prefix)]
    return found[0] if len(found) == 1 else None


att_path = os.path.join(root, att_rel)
raw = read(att_path)
document = {}
if raw is not None:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            document = parsed
    except ValueError:
        document = {}
try:
    report = base64.b64decode(document.get("report_b64", ""), validate=True) \
        if isinstance(document.get("report_b64"), str) else b""
except (ValueError, TypeError):
    report = b""
if document.get("available") is not True or len(report) < 0xC0:
    report = b""

cmdline = document.get("cmdline") if isinstance(document.get("cmdline"), str) else ""
tokens = cmdline.split()
key_tokens = [token for token in tokens if token.startswith("bb.key=")]
bb_key = command_line_value(tokens, "bb.key=")
seal_id = command_line_value(tokens, "seal_id=")
boot_line = None
if bb_key:
    try:
        boot_line = key_line(base64.b64decode(bb_key).decode("utf-8", "replace"))
    except (ValueError, TypeError):
        boot_line = None

# The running machine's own command line. A root shell can edit the served
# JSON, but it cannot edit the line the CPU measured at boot.
now_tokens = (read(os.path.join(root, "proc/cmdline")) or "").split()
now_key = command_line_value(now_tokens, "bb.key=")
now_seal = command_line_value(now_tokens, "seal_id=")


def listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


# Every account's authorized_keys, not only root's. The tenant user has a
# shell and NOPASSWD sudo, so a key in its own ~/.ssh opens the machine just
# as well as one in root's; sshd's default file list is .ssh/authorized_keys
# and .ssh/authorized_keys2, so both are read wherever they exist.
home_root = os.path.join(root, "home")
ssh_dirs = [os.path.join(root, "root/.ssh")]
ssh_dirs.extend(os.path.join(home_root, name, ".ssh") for name in listdir(home_root))
ak_path = os.path.join(root, "root/.ssh/authorized_keys")
ak_text = read(ak_path)
ak_entries = []  # (path, line), every key line on the machine
for directory in ssh_dirs:
    for name in ("authorized_keys", "authorized_keys2"):
        path = os.path.join(directory, name)
        text = read(path)
        if text is None:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                ak_entries.append((path, line))
ak_lines = [line for _, line in ak_entries]
ak_keys = [key for key in (key_line(line) for line in ak_lines) if key]
ak_fps = {fingerprint(key) for key in ak_keys}

# Private halves anywhere an account or the installer can write, not only in
# */.ssh. A private key that matches an authorized key is a usable login, and
# the measured throwaway's private half is the strict bar's one sin wherever it
# sits (/root/boot-key included). The roots are the machine's persistent,
# writable state: /root, every /home, /etc/odile, /var/lib/odile. A file
# counts when its first bytes carry "PRIVATE KEY"; `ssh-keygen -l -f` names
# the fingerprint even for an encrypted key that refuses `ssh-keygen -y -P ""`,
# and even with no .pub sibling.
def private_candidates(base):
    """Every file under `base` that could hold a private key, cheaply.

    A private key is 400 bytes to a few kilobytes: the size window skips the
    tree's libraries and logs without opening them, and the scan reads only
    the first bytes of a file that fits.
    """
    stack = [base]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    continue
                if not entry.is_file():
                    continue
                if entry.name.endswith(".pub") or entry.name.startswith(("authorized_keys", "known_hosts")):
                    continue
                if not 200 <= entry.stat().st_size <= 65536:
                    continue
            except OSError:
                continue
            yield entry.path


private = []
seen = set()
private_roots = [os.path.join(root, "root"), home_root,
                 os.path.join(root, "etc/odile"), os.path.join(root, "var/lib/odile")]
for base in private_roots:
    for path in private_candidates(base):
        if path in seen:
            continue
        seen.add(path)
        try:
            descriptor = os.open(path, os.O_RDONLY)
            try:
                head = os.read(descriptor, 48)
            finally:
                os.close(descriptor)
        except OSError:
            continue
        if b"PRIVATE KEY" not in head:
            continue
        public = None
        # The image installs no ssh-keygen (no sshd, no openssh-client); the
        # private key's .pub sibling is then the only way to name it, and a key
        # with neither is "unidentified" below, which fails "usable keys".
        keygen = shutil.which("ssh-keygen")
        try:
            done = subprocess.run(
                [keygen or "ssh-keygen", "-y", "-P", "", "-f", path],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            done = None
        if done is not None and done.returncode == 0:
            public = key_line(done.stdout)
        if public is None:
            public = key_line(read(path + ".pub") or "")
        named = None
        try:
            listed = subprocess.run(
                [keygen or "ssh-keygen", "-l", "-f", path],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            listed = None
        if listed is not None and listed.returncode == 0:
            fields = listed.stdout.split()
            if len(fields) > 1 and fields[1].startswith("SHA256:"):
                named = fields[1].rstrip("=")
        private.append((path, public, named))

boot_fp = fingerprint(boot_line) if boot_line else None
# A private key whose public half could not be read: no ssh-keygen and no
# .pub sibling. It could be a usable login, so "usable keys" fails closed on it
# instead of passing because it cannot be named.
unidentified = [path for path, public, named in private
                if public is None and named is None]
boot_private = [path for path, public, named in private
                if (public is not None and public == boot_line)
                or (boot_fp is not None and named == boot_fp)]

# 1. Zero usable user keys: every authorized line is the measured throwaway,
# and no private half on the machine matches an authorized key.
if ak_text is None:
    row("usable keys", False, "no %s" % guest(ak_path))
elif not ak_keys:
    row("usable keys", False, "authorized_keys carries no public key")
else:
    extras = [(path, key) for path, line in ak_entries
              for key in (key_line(line),) if key is not None and key != boot_line]
    malformed = [(path, line) for path, line in ak_entries if key_line(line) is None]
    matched = [path for path, public, named in private
               if (public is not None and public in ak_keys)
               or (named is not None and named in ak_fps)]
    if extras or malformed or matched or unidentified:
        detail = []
        if extras:
            detail.append("%d authorized key(s) other than the measured throwaway in %s: %s"
                          % (len(extras),
                             ", ".join(sorted({guest(path) for path, _ in extras})),
                             ", ".join(fingerprint(key) for _, key in extras)))
        if malformed:
            detail.append("%d line(s) that are not a plain public key in %s"
                          % (len(malformed),
                             ", ".join(sorted({guest(path) for path, _ in malformed}))))
        if matched:
            detail.append("%d private half on the machine: %s"
                          % (len(matched), ", ".join(guest(path) for path in matched)))
        if unidentified:
            detail.append("%d private key(s) whose public half could not be read "
                          "(no ssh-keygen, no .pub): %s"
                          % (len(unidentified),
                             ", ".join(guest(path) for path in unidentified)))
        row("usable keys", False, "; ".join(detail))
    else:
        row("usable keys", True,
            "0 usable user keys; every authorized_keys line is the measured boot key (%s)"
            % fingerprint(boot_line or ak_keys[0]))

# 2. The throwaway boot key: one public key in the measured command line, the
# same key in authorized_keys, and no private half of it anywhere on the machine.
if len(key_tokens) != 1:
    row("boot key", False,
        "the measured command line carries %d bb.key tokens" % len(key_tokens))
elif boot_line is None:
    row("boot key", False, "bb.key does not decode to a public key")
elif boot_line not in ak_keys:
    row("boot key", False, "%s is not in authorized_keys" % fingerprint(boot_line))
elif now_key != bb_key:
    row("boot key", False, "the running command line does not carry the measured bb.key")
elif boot_private:
    row("boot key", False, "%s; private half on the machine: %s"
        % (fingerprint(boot_line), ", ".join(guest(path) for path in boot_private)))
else:
    row("boot key", True, "%s %s; public key only, no private half of it on the machine"
        % (boot_line.split()[0], fingerprint(boot_line)))

# 3. The seal: the signed policy, the measurement, and the per-tenant seal_id
# in the measured command line.
if not report:
    row("seal", False, "no report bytes in %s" % guest(att_path))
else:
    policy = int.from_bytes(report[8:16], "little")
    signed = report[0x90:0xC0].hex()
    problems = []
    if policy != 0x30000:
        problems.append("policy is 0x%x, not 0x30000" % policy)
    if len(signed) != 96 or signed == "0" * 96:
        problems.append("the signed report carries no measurement")
    if seal_id is None:
        problems.append("the measured command line has no seal_id")
    elif now_seal != seal_id:
        problems.append("the running command line has no measured seal_id")
    if expect and signed != expect:
        problems.append("the measurement is not --expect")
    detail = "policy 0x%x; seal_id %s; measurement %s" % (
        policy, seal_id or "missing", signed or "missing")
    if problems:
        detail += " (%s)" % "; ".join(problems)
    row("seal", not problems, detail)

# 4. The sign-in hash: scrypt in the tenant's Hermes .env, no plaintext.
env_path = os.path.join(root, "home", user, ".hermes", ".env")
env_text = read(env_path)
if env_text is None:
    row("sign-in hash", False, "no %s" % guest(env_path))
else:
    values = {}
    for line in env_text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip()
    encoded = values.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH", "")
    plaintext = "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD" in values
    valid = bool(re.match(
        r"^scrypt\$\d+\$\d+\$\d+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+$", encoded))
    if valid:
        cost = int(encoded.split("$")[1])
        if cost < 16384:
            valid = False
    if not encoded:
        row("sign-in hash", False, "no scrypt hash in %s" % guest(env_path))
    elif plaintext:
        row("sign-in hash", False, "plaintext password in %s" % guest(env_path))
    elif not valid:
        row("sign-in hash", False, "the hash in %s is not scrypt" % guest(env_path))
    else:
        row("sign-in hash", True,
            "scrypt hash present in %s; no plaintext password" % guest(env_path))

# 5. No operator key: no grant-shaped line (options plus a forced command),
# and no authorized key that names an operator. A different user key fails
# "usable keys" above; a private key that is not authorized (a backup key for
# the storage box, say) does not open this machine.
if ak_text is None:
    row("operator key", False, "no %s" % guest(ak_path))
else:
    grants = []
    for path, line in ak_entries:
        key = key_line(line)
        if has_options(line) or key is None:
            grants.append((path, line))
            continue
        comment = " ".join(line.split()[2:]).lower()
        if key != boot_line and any(word in comment for word in ("odile", "operator", "support", "hq")):
            grants.append((path, line))
    if grants:
        where = sorted({guest(path) for path, _ in grants})
        suffix = "" if where == [guest(ak_path)] else " (in %s)" % ", ".join(where)
        row("operator key", False, "authorized_keys carries %s%s"
            % ("; ".join(line for _, line in grants), suffix))
    else:
        row("operator key", True,
            "none (authorized_keys: %d line(s), the measured throwaway)" % len(ak_entries))

sys.exit(1 if fails else 0)
PY
}

# The boot oneshot's artifact. The file is the report the mode prints, with
# one provenance line in front, written in one rename so a reader on /verify
# sees either the old report or the new one, never half of each. A FAIL run
# still writes: the five lines and their FAIL are what the page must show
# (a tampered authorized_keys or a missing sign-in hash is a report, not an
# error). Exit status is the mode's own.
write_access_report() {
    local dir tmp stamp rc=0
    dir=$(dirname -- "$ACCESS_OUT")
    tmp=$(mktemp "$dir/.access-report.XXXXXX") || die "could not write in $dir"
    stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    {
        printf 'read from your machine, %s\n' "$stamp"
        printf 'verify_and_unlock — access report\n\n'
        report_access || rc=$?
        if [ "$rc" -ne 0 ]; then
            printf '\n  %s\n' "FAIL — the access report did not pass"
        else
            printf '\n  %s\n' "PASS — five checks: zero usable user keys, the throwaway boot key, the seal, the sign-in hash, no operator key."
        fi
    } > "$tmp"
    chmod 0644 "$tmp"
    mv -f -- "$tmp" "$ACCESS_OUT"
    # The provenance line is the file's, not the terminal's: stdout stays the
    # report itself, so a bench can compare a hand run with the file byte for
    # byte (`tail -n +2 FILE`).
    tail -n +2 -- "$ACCESS_OUT"
    return "$rc"
}

run_blackbox_verify() {
    local out rc
    out=$("$PYTHON" "$BLACKBOX_VERIFY" --release "$RELEASE_DIR" --vcpus "$VCPUS" < "$ATTEST" 2>&1) && rc=0 || rc=$?
    printf '%s\n' "$out" | sed 's/^/  /'
    [ "$rc" -eq 0 ] || failed "blackbox-verify did not pass (exit $rc)
  The machine is not running the pinned release with this command line."
    case "$out" in
        *"PASS:"*) ;;
        *) failed "blackbox-verify printed no PASS line" ;;
    esac
}

check_wire_host_key() {
    local scan line fp
    scan=$(ssh-keyscan -T 10 -p "$PORT" -t ed25519 "$HOST" 2>/dev/null) \
        || transport_failed "ssh-keyscan could not reach $HOST:$PORT"
    line=$(printf '%s\n' "$scan" | awk '$2 == "ssh-ed25519" { print; exit }')
    [ -n "$line" ] || transport_failed "no ed25519 host key on $HOST:$PORT"
    printf '%s\n' "$line" > "$TMP/keyscan.pub"
    fp=$(ssh-keygen -lf "$TMP/keyscan.pub" | awk '{print $2}')
    if [ "$fp" != "$(summary_value hostkey_fp)" ]; then
        hostkey_failed "the host key on the wire is $fp, the report binds $(summary_value hostkey_fp)
  Stop: do not type the passphrase on a machine whose key is not the one the report binds."
    fi
    HOSTKEY_LINE=$(printf '%s\n' "$line" | awk '{print $2 " " $3}')
    printf '  %-14s %s  %s\n' "wire key" "PASS" "$fp is the key on the wire"
}

run_odile_verify() {
    if [ -z "$AMD_DIR" ]; then
        AMD_DIR=$(amd_cache_dir) || failed "could not derive the AMD cache path"
        fetch_amd_certs "$AMD_DIR"
    fi
    local -a args=(--report "$NORMALIZED" --expect "$MEASUREMENT" --release "$RELEASE_ID"
                   --amd-dir "$AMD_DIR")
    [ -n "$RECORD" ] && args+=(--record "$RECORD")
    [ -n "$PREVIOUS" ] && args+=(--previous "$PREVIOUS")
    local out rc
    out=$("$ODILE_VERIFY" "${args[@]}" 2>&1) && rc=0 || rc=$?
    printf '%s\n' "$out" | sed 's/^/  /'
    [ "$rc" -eq 0 ] || failed "odile-verify did not pass (exit $rc)"
    case "$out" in
        *"PASS —"*) ;;
        *) failed "odile-verify printed no PASS line" ;;
    esac
}

# The VCEK is per chip and per TCB, and AMD's key server rate-limits. Cache it
# under ~/.cache/odile-ceremony so a ceremony fetches it once; a firmware update
# changes the TCB and therefore the directory. `--amd-dir` overrides the cache.
amd_cache_dir() {
    "$PYTHON" - "$ATTEST" "${XDG_CACHE_HOME:-${HOME:-/root}/.cache}/odile-ceremony" <<'PY'
import base64
import json
import os
import sys

att_path, root = sys.argv[1:3]
with open(att_path, encoding="utf-8") as fh:
    raw = json.load(fh)
report = base64.b64decode(raw["report_b64"])
tcb = report[0x180:0x188]
print(os.path.join(root, "vcek-%s-%d-%d-%d-%d" % (
    report[0x1A0:0x1E0].hex(), tcb[0], tcb[1], tcb[6], tcb[7])))
PY
}

fetch_amd_certs() {
    local dir=$1
    if [ -f "$dir/vcek.pem" ] && [ -f "$dir/ask.pem" ] && [ -f "$dir/ark.pem" ]; then
        printf '  %-14s %s  %s\n' "AMD chain" "PASS" "VCEK for this chip from the cache ($dir)"
        return 0
    fi
    mkdir -p "$dir"
    "$PYTHON" - "$ATTEST" "$dir" "$KDS_BASE" <<'PY'
import base64
import json
import os
import re
import sys
import time
import urllib.request

att_path, out_dir, kds = sys.argv[1:4]
with open(att_path, encoding="utf-8") as fh:
    raw = json.load(fh)
report = base64.b64decode(raw["report_b64"])
tcb = report[0x180:0x188]
chip = report[0x1A0:0x1E0]
url = "%s/%s?blSPL=%d&teeSPL=%d&snpSPL=%d&ucodeSPL=%d" % (
    kds, chip.hex(), tcb[0], tcb[1], tcb[6], tcb[7])


def get(target):
    # The key server rate-limits and sometimes answers 400 instead of 429;
    # verify.md records the same behaviour. Three quiet retries, then fail.
    last = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(
                target, headers={"User-Agent": "odile-ceremony/1.0"})
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - retried, then reported
            last = exc
            if attempt < 3:
                time.sleep(2.0)
    raise last


try:
    chain = get(kds + "/cert_chain").decode("utf-8")
    certs = re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----\n", chain, re.S)
    if len(certs) != 2:
        raise SystemExit("AMD chain did not contain exactly ASK and ARK")
    with open(os.path.join(out_dir, "ask.pem"), "w", encoding="utf-8") as fh:
        fh.write(certs[0])
    with open(os.path.join(out_dir, "ark.pem"), "w", encoding="utf-8") as fh:
        fh.write(certs[1])
    with open(os.path.join(out_dir, "vcek.der"), "wb") as fh:
        fh.write(get(url))
except Exception as exc:  # noqa: BLE001
    sys.stderr.write("could not fetch the AMD VCEK chain: %s\n" % exc)
    sys.exit(1)
PY
    openssl x509 -inform DER -in "$dir/vcek.der" -out "$dir/vcek.pem" 2>/dev/null \
        || failed "could not read the VCEK downloaded from AMD's key server"
    printf '  %-14s %s  %s\n' "AMD chain" "PASS" "VCEK for this chip fetched from kdsintf.amd.com"
}

summary_value() {
    sed -n "s/^$1=//p" "$SUMMARY" | head -n 1
}

write_known_hosts() {
    local name="$HOST" line old_fp
    if [ "$PORT" -ne 22 ]; then
        name="[$HOST]:$PORT"
    fi
    line="$name $HOSTKEY_LINE"
    mkdir -p "$(dirname -- "$KNOWN_HOSTS")"
    touch "$KNOWN_HOSTS"
    if ssh-keygen -F "$name" -f "$KNOWN_HOSTS" >/dev/null 2>&1; then
        if grep -qxF "$line" "$KNOWN_HOSTS"; then
            printf '  %-14s %s  %s\n' "known_hosts" "PASS" "already carries this key"
            return
        fi
        old_fp=$(ssh-keygen -F "$name" -f "$KNOWN_HOSTS" \
            | awk '$2 == "ssh-ed25519" { print $2 " " $3; exit }')
        printf '  %-14s %s  %s\n' "known_hosts" "CHANGED" \
            "the key for $name changed; the report and the wire agree on the new one"
        ssh-keygen -R "$name" -f "$KNOWN_HOSTS" >/dev/null
        printf '%s\n' "$line" >> "$KNOWN_HOSTS"
        [ -z "$old_fp" ] || printf '  %-14s %s  %s\n' "old key" "REPLACED" "$old_fp"
        return
    fi
    printf '%s\n' "$line" >> "$KNOWN_HOSTS"
    printf '  %-14s %s  %s\n' "known_hosts" "PASS" "wrote the host key to $KNOWN_HOSTS"
}

connect_initramfs() {
    local -a opts=(-p "$PORT" -o StrictHostKeyChecking=yes
                   -o UserKnownHostsFile="$KNOWN_HOSTS" -o ConnectTimeout=15)
    case "$MODE" in
        encrypt|unlock) opts+=(-t) ;;
        *) opts+=(-n) ;;
    esac
    if [ -n "$BOOT_KEY" ]; then
        opts+=(-i "$BOOT_KEY" -o IdentitiesOnly=yes)
    fi
    exec ssh "${opts[@]}" "root@$HOST" "$MODE"
}

summary() {
    printf '\n'
    printf '  %-14s %s\n' "host" "$HOST"
    printf '  %-14s %s (%s)\n' "release" "$RELEASE_ID" "pinned"
    printf '  %-14s %s\n' "measurement" "$MEASUREMENT"
    printf '  %-14s %s\n' "boot key" "$(summary_value bootkey_fp)"
    printf '  %-14s %s\n' "host key" "$(summary_value hostkey_fp)"
    printf '  %-14s %s\n' "known_hosts" "$KNOWN_HOSTS"
    printf '\n  PASS — verified. '
    case "$MODE" in
        verify) printf 'The machine is at the initramfs; rerun with --mode encrypt (first boot) or --mode unlock.\n' ;;
        *) printf 'Opening %s at the initramfs; the passphrase is typed there, not here.\n' "$MODE" ;;
    esac
}

main() {
    parse_args "$@"
    if [ "$SELF_TEST" -eq 1 ]; then
        self_test
        exit 0
    fi
    validate_args
    preconditions

    if [ "$MODE" = access ]; then
        if [ -n "$ACCESS_OUT" ]; then
            write_access_report
            exit $?
        fi
        printf 'verify_and_unlock — access report\n\n'
        report_access || failed "the access report did not pass"
        printf '\n  %s\n' "PASS — five checks: zero usable user keys, the throwaway boot key, the seal, the sign-in hash, no operator key."
        exit 0
    fi

    TMP=$(mktemp -d "${TMPDIR:-/tmp}/odile-ceremony.XXXXXX")
    trap 'rm -rf "$TMP"' EXIT
    ATTEST="$TMP/attest.json"
    NORMALIZED="$TMP/odile-verify.json"
    SUMMARY="$TMP/summary"
    [ -z "$ATTEST_OUT" ] || umask 077

    read_public_key
    printf '%s\n' "$PUBLIC_LINE" > "$TMP/boot.pub"

    printf 'verify_and_unlock — %s at release %s\n\n' "$HOST" "$RELEASE_ID"
    check_verifier_hash
    check_release_against_pin
    fetch_attestation
    if [ -n "$ATTEST_OUT" ]; then
        cp "$ATTEST" "$ATTEST_OUT"
        chmod 600 "$ATTEST_OUT" 2>/dev/null || true
    fi
    check_attestation || failed "the attestation does not bind this machine to your boot key"
    run_blackbox_verify
    check_wire_host_key
    MEASUREMENT=$(summary_value measurement)
    run_odile_verify
    write_known_hosts
    summary
    if [ "$MODE" != verify ]; then
        rm -rf "$TMP"
        trap - EXIT
        connect_initramfs
    fi
}

# --------------------------------------------------------------------- tests

self_test() {
    local tmp
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/odile-selftest.XXXXXX")
    SELFTEST_TMP=$tmp
    trap 'rm -rf "${SELFTEST_TMP:-}"' EXIT
    local host=198.51.100.7
    local failures=0

    ssh-keygen -q -t ed25519 -N '' -C selftest-host -f "$tmp/host" >/dev/null
    ssh-keygen -q -t ed25519 -N '' -C selftest-boot -f "$tmp/boot" >/dev/null
    ssh-keygen -q -t ed25519 -N '' -C selftest-other -f "$tmp/other" >/dev/null
    local host_pub boot_pub other_pub
    host_pub=$(awk '{print $1 " " $2}' "$tmp/host.pub")
    boot_pub=$(awk '{print $1 " " $2}' "$tmp/boot.pub")
    other_pub=$(awk '{print $1 " " $2}' "$tmp/other.pub")
    mkdir -p "$tmp/bin" "$tmp/amd" "$tmp/release"

    printf 'fake OVMF for the self-test\n' > "$tmp/release/OVMF.amdsev.fd"
    printf 'fake kernel for the self-test\n' > "$tmp/release/vmlinuz"
    printf 'fake initrd for the self-test\n' > "$tmp/release/initrd.img"
    printf '{"release": "2026.11.1"}\n' > "$tmp/release/RELEASE.json"

    fixture "$tmp" "$host" "$boot_pub" "$host_pub"
    printf '%s %s\n' "$host" "$host_pub" > "$tmp/keyscan-good"
    printf '%s %s\n' "$host" "$other_pub" > "$tmp/keyscan-bad"

    write_pin "$tmp"
    write_stubs "$tmp"

    run_case() {
        local name=$1 want=$2
        shift 2
        local kh="$tmp/kh-$name" log="$tmp/log-$name" out="$tmp/out-$name"
        if [ -n "${KH_SEED:-}" ]; then
            cp "$KH_SEED" "$kh"
        else
            : > "$kh"
        fi
        : > "$log"
        local rc
        set +e
        PATH="$tmp/bin:$PATH" ODL_ATTEST="$tmp/attest.json" \
            ODL_KEYSCAN="${ODL_KEYSCAN:-$tmp/keyscan-good}" ODL_SSH_LOG="$log" \
            ODL_BB_EXIT="${ODL_BB_EXIT:-0}" ODL_OV_EXIT="${ODL_OV_EXIT:-0}" \
            VIRTUAL_ENV="${ODL_VIRTUAL_ENV:-}" PYTHON="${ODL_CHILD_PYTHON:-}" \
            "$SCRIPT_DIR/verify_and_unlock.sh" --host "$host" \
            --release "$tmp/release" --release-id 2026.11.1 \
            --boot-key "$tmp/boot" --known-hosts "$kh" \
            --blackbox-verify "$tmp/bin/blackbox-verify" \
            --odile-verify "$tmp/bin/odile-verify" --pin "$tmp/pin.json" \
            --verifier-sha256 "$(sha256sum "$tmp/bin/blackbox-verify" | awk '{print $1}')" \
            --amd-dir "$tmp/amd" "$@" >"$out" 2>&1
        rc=$?
        set -e
        if [ "$rc" -ne "$want" ]; then
            printf 'self-test: %s: exit %d, wanted %d\n' "$name" "$rc" "$want"
            sed 's/^/  | /' "$out"
            return 1
        fi
        printf 'ok   %-24s exit %d\n' "$name" "$rc"
    }

    run_case "verify passes" 0 --mode verify || failures=$((failures + 1))
    grep -qxF "$host $host_pub" "$tmp/kh-verify passes" \
        || { printf 'self-test: known_hosts was not written\n'; failures=$((failures + 1)); }
    [ "$(wc -l < "$tmp/log-verify passes")" -eq 1 ] \
        || { printf 'self-test: verify mode made more than the attest call\n'; failures=$((failures + 1)); }
    # The attest fetch must not read the terminal: with ssh without -n the
    # fetch would swallow a passphrase typed ahead before the initramfs asks.
    grep -qE '(^| )-n( |$)' "$tmp/log-verify passes" \
        || { printf 'self-test: the attest fetch lacks ssh -n; it would eat typed-ahead input\n'; failures=$((failures + 1)); }
    if grep -qE '(^| )-t( |$)' "$tmp/log-verify passes"; then
        printf 'self-test: verify mode asked for a tty before the passphrase\n'
        failures=$((failures + 1))
    fi

    cp "$tmp/kh-verify passes" "$tmp/kh-idempotent"
    run_case "idempotent known_hosts" 0 --mode verify || failures=$((failures + 1))
    [ "$(grep -c . "$tmp/kh-idempotent")" -eq 1 ] \
        || { printf 'self-test: a second run duplicated the known_hosts line\n'; failures=$((failures + 1)); }

    run_case "unlock dispatches" 0 --mode unlock || failures=$((failures + 1))
    grep -q -- ' -t .* unlock$' "$tmp/log-unlock dispatches" \
        || { printf 'self-test: unlock was not run under a tty\n'; failures=$((failures + 1)); }

    run_case "wrong boot key" 1 --mode verify --public-key "$tmp/other.pub" || failures=$((failures + 1))
    [ ! -s "$tmp/kh-wrong boot key" ] \
        || { printf 'self-test: known_hosts written on a bb.key mismatch\n'; failures=$((failures + 1)); }

    run_case "wrong address" 1 --host 198.51.100.8 --mode verify || failures=$((failures + 1))

    ODL_KEYSCAN="$tmp/keyscan-bad" run_case "changed wire key" 5 --mode verify || failures=$((failures + 1))
    [ ! -s "$tmp/kh-changed wire key" ] \
        || { printf 'self-test: known_hosts written on a wire-key mismatch\n'; failures=$((failures + 1)); }

    ODL_BB_EXIT=1 run_case "blackbox-verify fails" 1 --mode verify || failures=$((failures + 1))

    run_case "expect mismatch" 1 --mode verify --expect "$(printf 'ff%.0s' {1..48})" || failures=$((failures + 1))

    ODL_OV_EXIT=1 run_case "odile-verify fails" 1 --mode verify || failures=$((failures + 1))

    # A fresh Debian 13 refuses `pip install --user` (PEP 668); the guide makes a
    # venv, and the wrapper must use it when one is active.
    ODL_VIRTUAL_ENV="$tmp/venv" ODL_PY_LOG="$tmp/py.log" \
        run_case "venv python is used" 0 --mode verify || failures=$((failures + 1))
    if [ ! -s "$tmp/py.log" ] || grep -vqxF "$tmp/venv/bin/python" "$tmp/py.log"; then
        printf 'self-test: the active virtualenv python was not used\n'
        failures=$((failures + 1))
    fi

    ODL_CHILD_PYTHON="$tmp/nomods/bin/python" \
        run_case "missing verifier modules" 2 --mode verify || failures=$((failures + 1))
    grep -qF 'python3 -m venv .venv' "$tmp/out-missing verifier modules" \
        || { printf 'self-test: the missing-module hint names no venv recipe\n'; failures=$((failures + 1)); }
    if grep -qF 'pip install --user' "$tmp/out-missing verifier modules"; then
        printf 'self-test: the hint still suggests pip install --user (PEP 668 refuses it)\n'
        failures=$((failures + 1))
    fi

    printf '%s %s\n' "$host" "$other_pub" > "$tmp/kh-seed"
    KH_SEED="$tmp/kh-seed" run_case "changed host key accepted" 0 --mode verify \
        || failures=$((failures + 1))
    grep -q CHANGED "$tmp/out-changed host key accepted" \
        || { printf 'self-test: a changed key was replaced without a CHANGED row\n'; failures=$((failures + 1)); }
    [ "$(grep -c . "$tmp/kh-changed host key accepted")" -eq 1 ] \
        || { printf 'self-test: the changed key was not replaced, got %s lines\n' \
             "$(grep -c . "$tmp/kh-changed host key accepted")"; failures=$((failures + 1)); }
    grep -qxF "$host $host_pub" "$tmp/kh-changed host key accepted" \
        || { printf 'self-test: the new key was not written\n'; failures=$((failures + 1)); }

    if [ "$failures" -ne 0 ]; then
        printf 'self-test: FAIL (%d)\n' "$failures" >&2
        exit 1
    fi
    printf 'self-test: OK (12 cases)\n'
}

fixture() {
    local tmp=$1 host=$2 boot_pub=$3 host_pub=$4
    "$PYTHON" - "$tmp" "$host" "$boot_pub" "$host_pub" <<'PY'
import base64
import hashlib
import json
import struct
import sys

tmp, host, boot_pub, host_pub = sys.argv[1:5]
host_blob = base64.b64decode(host_pub.split()[1])
report = bytearray(1184)
struct.pack_into("<I", report, 0, 5)
struct.pack_into("<Q", report, 8, 0x30000)
struct.pack_into("<I", report, 0x30, 0)
report[0x50:0x90] = hashlib.sha256(host_blob).digest() + b"\0" * 32
report[0x90:0xC0] = hashlib.sha384(b"odile ceremony self-test").digest()
cmdline = ("console=ttyS0,115200 bb.release=2026.11.1 bb.ip4=%s/24 bb.key=%s"
           % (host, base64.b64encode(boot_pub.encode()).decode()))
attestation = {
    "version": 1,
    "release": "2026.11.1",
    "status": "ok",
    "hostkey": host_pub,
    "fingerprint": "SHA256:" + base64.b64encode(
        hashlib.sha256(host_blob).digest()).decode().rstrip("="),
    "report_b64": base64.b64encode(report).decode(),
    "cmdline": cmdline,
}
with open(tmp + "/attest.json", "w", encoding="utf-8") as fh:
    json.dump(attestation, fh)
PY
}

write_pin() {
    local tmp=$1
    "$PYTHON" - "$tmp" <<'PY'
import hashlib
import json
import os
import sys

tmp = sys.argv[1]
release = tmp + "/release"


def sha256(name):
    with open(os.path.join(release, name), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


pin = {
    "schema": 2,
    "provision_release": "2026.11.1",
    "measurement": "00" * 48,
    "report_version": 5,
    "policy": "0x30000",
    "fetched_at": "2026-11-09T00:00:00Z",
    "pinned_at": "2026-11-09",
    "source": "self-test",
    "commit": "self-test",
    "releases": {
        "2026.11.1": {
            "release_url": "https://example.invalid/2026.11.1/",
            "release_json_sha256": sha256("RELEASE.json"),
            "artifacts": {
                "OVMF.amdsev.fd": sha256("OVMF.amdsev.fd"),
                "vmlinuz": sha256("vmlinuz"),
                "initrd.img": sha256("initrd.img"),
            },
            "measurement_parameters": {
                "vcpu_type": "EPYC-v4",
                "guest_features": "0x1",
                "policy": "0x30000",
                "vmm": "QEMU",
            },
            "vcpu_counts": [4],
            "inputs": {
                "kernel_version": "self-test",
                "kernel_package_sha256": "00" * 32,
                "debian_snapshot": "self-test",
                "edk2": "self-test",
            },
            "pinned_at": "2026-11-09",
            "evidence": "self-test",
        }
    },
}
with open(tmp + "/pin.json", "w", encoding="utf-8") as fh:
    json.dump(pin, fh)
PY
}

write_stubs() {
    local tmp=$1
    cat > "$tmp/bin/ssh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$ODL_SSH_LOG"
last=""
for arg in "$@"; do last=$arg; done
if [ "$last" = attest ]; then
    cat "$ODL_ATTEST"
    exit 0
fi
printf 'stub ssh: %s\n' "$last"
exit 0
SH
    cat > "$tmp/bin/ssh-keyscan" <<'SH'
#!/usr/bin/env bash
cat "$ODL_KEYSCAN"
SH
    cat > "$tmp/bin/blackbox-verify" <<'PYSTUB'
#!/usr/bin/env python3
import json
import os
import sys

att = json.load(sys.stdin)
if att.get("status") != "ok":
    print("FAIL: status is %r" % att.get("status"))
    sys.exit(1)
if os.environ.get("ODL_BB_EXIT", "0") != "0":
    print("FAIL: stub asked to fail")
    sys.exit(1)
print("PASS: stub blackbox-verify")
PYSTUB
    cat > "$tmp/bin/odile-verify" <<'SH'
#!/usr/bin/env bash
record=""
previous=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --record) record=$2; shift 2 ;;
        --previous) previous=$2; shift 2 ;;
        *) shift ;;
    esac
done
if [ "${ODL_OV_EXIT:-0}" != 0 ]; then
    echo "  signature   FAIL  stub asked to fail"
    exit 1
fi
if [ -n "$record" ]; then
    printf '{"schema": "odile.boot-record.v1"}\n' > "$record"
fi
if [ -n "$previous" ]; then
    echo "  previous         same"
fi
echo "PASS — stub odile-verify"
SH
    # A stand-in for an activated virtualenv's interpreter: it records that the
    # wrapper chose it, then runs the real Python. The generated stub expands
    # $0 and $ODL_PY_LOG when it runs, not here.
    mkdir -p "$tmp/venv/bin" "$tmp/nomods/bin"
    # shellcheck disable=SC2016
    printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$0" >> "$ODL_PY_LOG"\nexec %q "$@"\n' "$PYTHON" \
        > "$tmp/venv/bin/python"
    # A stand-in for a Python without the verifier's modules, so the hint and
    # its exit code are exercised.
    # shellcheck disable=SC2016
    printf '#!/usr/bin/env bash\nif [ "$1" = "-c" ] && [ "$2" = "import sevsnpmeasure, cryptography" ]; then exit 1; fi\nexec %q "$@"\n' "$PYTHON" \
        > "$tmp/nomods/bin/python"
    chmod +x "$tmp/bin/ssh" "$tmp/bin/ssh-keyscan" "$tmp/bin/blackbox-verify" "$tmp/bin/odile-verify" \
        "$tmp/venv/bin/python" "$tmp/nomods/bin/python"
}

main "$@"
'''
AUDIT_SCRIPT_SHA256 = "539e205f7d26e1c0757a4cdabbcae444c98df10f6e68eff92b4c147604cf4e4e"
AUDIT_SCRIPT = r'''#!/usr/bin/python3
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
'''
# END VERIFY SURFACE (ODL-473)

# BEGIN TENANT BUNDLE
# Generated by installer/embed_tenant.py; do not edit by hand.
# Each file is pinned by SHA-256. --self-test fails on a one-byte edit.
TENANT_AGENTS_SOURCE = r'''# Odile

You are Odile, the agent on this machine. Your human is the only keyholder: no
one at Odile can log in unless they grant it. You run as `odile` with sudo and
can read every file on the machine. Built on Hermes.

If another context file calls you Hermes Agent, this file wins. Hermes is the
engine; Odile is your name.

## First run

If your persistent memory does not contain `onboarded_at`, load the
`odile-first-run` skill and follow it before anything else. If memory contains
`onboarded_at`, never run the ritual again unless the user asks — "set up my
brief" is the way back in.

## Recipes

When they ask for a morning brief, a watchdog, a research memo or a chart from
a CSV, call `skill_view(name='odile-recipes')` before you answer, then follow
that recipe. The skill is the tested version; a recipe done from memory is not
the recipe. For a watchdog, never write your own script: the skill carries the
only tested one and tells you to check its SHA-256 before you schedule it.

Briefs land in `~/briefs/`; the other files land in `~/workspace/`. Confirm the
file with `ls -l` before you say it is done.

## Standing rules

- Say "I", never "we". No emoji, no exclamation marks, no filler.
- Do not bring up what came before this workspace — keys, SSH, the installer,
  the ceremony. If the user asks how they get back in after a reboot: "After a
  reboot, it starts itself. The seal is the key." There is no passphrase to
  type.
- Their files live in `~/workspace`; briefs in `~/briefs`. Both survive a
  restart.
- Inference goes straight to NEAR AI with this machine's own key. The key is
  root-only, at /etc/odile/key.env; never print it.
- Never print `~/.hermes/.env` or any file under `~/.hermes` that holds a
  credential.
- Support access exists only when the user grants it, for as long as they grant
  it. A grant is a handover, not a peek.
'''
TENANT_AGENTS_SHA256 = "7865147fe11e1b795a473fb5c0ea1379c4240d73bb193714f9aad2993a7598fd"

TENANT_SKILLS: tuple[tuple[str, str, str, str], ...] = (
    (
        "odile-first-run",
        "skills/odile-first-run/SKILL.md",
        "0ca5d154b3156367733ecafd2a50ed833bc939639c20361ed76e86646b0bd412",
        r'''---
name: odile-first-run
description: First run: five questions, a real brief, a schedule.
---

# Odile — first run

Load this when persistent memory has no `onboarded_at`, or when the user asks to
set up their brief. If `onboarded_at` is already there, do not run it again.

Five questions, one per message. It ends with a real file on disk and a
scheduled job, in under six minutes.

## Who you are when you speak

- You are Odile. Hermes is the engine; never introduce yourself as Hermes.
- The user is the only keyholder. Approved sentences, first person where needed:
  - "No one at Odile can log in unless you grant it."
  - "I run as `odile` with sudo, so I can read every file on the machine."
- Never bring up the boot key, SSH keys, the installer or the ceremony.

## State first

Read your memory before you speak.

| Memory has | Do |
|---|---|
| `onboarded_at` | Nothing. Answer normally, unless the user asks to set up their brief. |
| Some answers, no `onboarded_at` | Resume at the first answer still missing. |
| Nothing | Start at question 1. |

A resume is a new session. Greet by the name you hold, mention only the answers
you actually hold, then ask the first missing question. Never re-ask an answer
you already hold.

- Name only: "Welcome back, <name>. I have your name. Where are you?"
- Name and time zone: "Welcome back, <name>. I have your name and time zone.
  Name three things you'd like me to handle."
- Name, time zone and three things: "Welcome back, <name>. I have your name,
  your time zone and your three things. What time should your brief land? 07:00
  your time is the default — say a time, or 'default'."
- Everything but email: "Welcome back, <name>. Last one: may I email you the
  brief too, when that's on?"
- All five answers, no `onboarded_at`: finish whatever is missing (the job,
  then `onboarded_at`) and close; do not ask again.
- Time zone without a name (unusual): "Welcome back. What should I call you?"

If the new session's first message is only a greeting ("Hi", "Hello"), it is
still a resume: open with the "Welcome back" line, not the five-question
introduction. If the first message already answers the missing question, store
it and ask the next one.

Save each answer in the same turn it is given, one memory entry, target `user`:

- `Name: …`
- `Time zone: <IANA>` (and `City: …` if they named one)
- `Wants: <a>; <b>; <c>`
- `Brief: HH:MM <IANA>`
- `Email brief: yes|no`

A closed tab must never lose an answer.

## 1. Name

Open with exactly this, then ask question 1:

> I'm Odile, the agent on this machine. Your own computer, with me inside it. No
> one at Odile can log in unless you grant it. I run as `odile` with sudo, so I
> can read every file on the machine. Built on Hermes. Five short questions,
> then I'll get to work. What should I call you?

If the user already gave their name in their first message, keep it and do not
ask.

## 2. Time zone

> Where are you? A city is enough — I'll set your time zone.

If they give an IANA name, use it. If a city, choose the IANA zone. If it is
ambiguous ("Portland"), ask one clarifying question. Validate before you store:

```
/usr/bin/python3 -c 'from zoneinfo import ZoneInfo; ZoneInfo("<zone>")'
```

If it fails, ask once more. If it still fails, use `UTC` and say so. Store the
IANA name exactly, e.g. `Asia/Hong_Kong`.

## 3. Three things

> Name three things you'd like me to handle. Small is fine — a folder to tidy, a
> site to watch, a topic you keep meaning to read about.

Accept anything. If they give one, ask "Two more?" — still one question. Store
all three.

## The first artifact — after answer 3, before questions 4 and 5

Say: "Good. I'll write today's brief while we finish — two more questions."

Then make the file. It must exist before the ritual ends:
`~/briefs/<today in their zone>.md`, markdown, under 200 words.

- the date and the zone;
- one line of weather for their city: `curl -s 'https://wttr.in/<city>?format=3'`;
- one short item for each of the three things, looked up now with `web_search`
  (link included); if a lookup fails, write one honest line;
- one sentence on what you will watch tomorrow.

Do not spend more than about 90 seconds. Write the file even if some lookups
fail: `mkdir -p ~/briefs`, then write it with the terminal, then confirm with
`ls -l ~/briefs/`. Never hand back a promise instead of a file.

If they named no city, use the zone's city (`Asia/Hong_Kong` → Hong Kong).

## 4. Brief time

> What time should your brief land? 07:00 your time is the default — say a time,
> or "default".

Parse `HH:MM` (24-hour). Default 07:00. Store `Brief: HH:MM <IANA>`.

## 5. Email

> Last one: may I email you the brief too, when that's on? For now it always
> lands in `~/briefs/`.

Store `Email brief: yes|no`. Do not promise an address or a date.

## The schedule

Load `cronjob_manage` (`tool_search` for "cron schedule" if it is not in your
tool list) and create one job:

- name: `Morning brief`
- schedule: `<minute> <hour> * * *` from answer 4
- timezone: the IANA zone from answer 2
- deliver: `local`
- enabled_toolsets: `["web", "terminal"]`
- prompt: self-contained; include the name, zone, city and the three things:

```
Morning brief for <name>, <zone>. Write ~/briefs/<today YYYY-MM-DD>.md, where
the date is today in <zone> (TZ=<zone> date +%F). Include the date; one line of
weather for <city> from https://wttr.in/<city>?format=3; one short item with a
link for each of: <thing 1>; <thing 2>; <thing 3>; and one sentence on what you
will watch tomorrow. Use web_search. Under 200 words. If a lookup fails, say so
in one line and continue. Write the file even if some lookups fail.
```

Verify, do not assume: read `$HOME/.hermes/cron/jobs.json` and confirm the job
is enabled, its expression is the time asked for, and its timezone is the IANA
name. If anything is wrong, update it before you speak again.

Then add to memory (target `memory`):
`onboarded_at: <ISO-8601 UTC>; ritual: complete`.

## Close — exactly three sentences

> Saved: your name, your time zone, your three things, and <HH:MM> <zone> for
> the brief. Tomorrow at <HH:MM> <zone> it will be in `~/briefs/`, and if the
> machine is locked it runs when you unlock. Ask me for anything in between.

## Skip

Any message that clearly skips — "skip", "skip for now", "not now", "later",
"no thanks", "pass", "just let me use it" — ends it at once. Keep the answers
already given. Add to memory (target `memory`):
`onboarded_at: <ISO-8601 UTC>; ritual: skipped`.

Then say, in two sentences: "Skipped. Say 'set up my brief' when you want one."

Never ask again after a skip. Never ask "are you sure?".

## Branches

- They ask something unrelated mid-ritual: answer it in one or two sentences,
  then repeat the current question in one line.
- They give several answers at once: keep them all; ask only what is missing.
- The cron tool fails: retry once. If it still fails, write the error to memory,
  say in one sentence that the brief file is there and the schedule needs one
  more try, and never claim a schedule you did not create.
- They ask for a different brief time later: update the job; do not create a
  second one.
''',
    ),
    (
        "odile-help",
        "skills/odile-help/SKILL.md",
        "8d2680f52508b5cd0f12133f74c45997c298e5c6ba707706776b0f48d5a74d35",
        r'''---
name: odile-help
description: Stuck? odile.fyi/faq · run odile-diag · support by grant.
---

# Odile — help

Load this when the user asks for help, says something is broken, cannot get in,
or types `/odile-help`. The public FAQ is the long version; this skill is the
short one, and it never promises access Odile does not have.

## Who you are when you speak

- You are Odile. Hermes is the engine; never introduce yourself as Hermes.
- Say "I". The company is "Odile" or "we" only when the company acts.
- Never promise a reset at HQ, a refund, or the return of anything that was not
  backed up.
- Never ask for the sign-in password or the recovery code. Neither has a copy at
  HQ; the recovery file stays on the machine.

## Invoked with no question: print this and stop

> Three ways out, in order.
>
> 1. The answers: https://odile.fyi/faq
> 2. Something is broken: run `odile-diag` and email the bundle to
>    help@odile.fyi. It holds logs, versions, disk usage and service state — no
>    password, no key, no prompt history, no file contents.
> 3. Not enough: `odile-support grant --hours 24`. It prints the expiry and the
>    revoke command. A grant is a handover, not a peek.
>
> Tell me what happened and I will take the next step with you.

## With a question: ask one thing first

Is the machine up — does the workspace answer? — or did it just restart? The
answer decides the next step: a running machine can show its own logs and files;
a machine that is down cannot run me at all.

Then use the answer below. Keep it under three short sentences before the
command or the address.

## The five answers

### 1. After a reboot

> After a reboot, it starts itself. The seal is the key. The disk unlocks only
> on the boot you verify; change the image, and it stays locked. There is no
> passphrase to type.
>
> A repeating job — the morning brief, a watchdog — runs when the machine comes
> back up if it was due while it was down. If the workspace still does not
> answer, reply and I will take the next step with you.

The machine needs no wait at a prompt and no SSH step. If the workspace answers,
nothing is broken.

### 2. The Odile sign-in is lost

> A lost Odile sign-in is reset on the machine with your recovery code. There
> is no HQ reset. Open https://<slug>.odile.fyi/first-visit/reset, enter the
> code you saved from the first visit and choose a new password; a new code is
> shown once.
>
> If the recovery code is lost too, reply and the machine is rebuilt.

Your Odile sign-in is set on your machine at first visit and hashed there. We
never see it. A recovery code and a recovery file are the way back: the code
resets your sign-in; the recovery file holds the backup password and the
certificate fingerprint.

### 3. The machine dies

> We rebuild the machine. The recovery file — the one you downloaded on your
> first visit — holds the backup password and the certificate fingerprint; keep
> your copy off the machine. While the machine is still running, the agent can
> show it again.
>
> Reply with the address you want the rebuild on and I will take the next step
> with you.

At M1 there is no nightly backup, so do not promise a restore. The restore
sentence lands after the M2 drill (ODL-215) with ODL-474.

### 4. A bill or a charge

> $49 a month, founding price, month-to-month. The $12 inference allowance is
> included and hard-capped; at the cap I stop and tell you. Stripe holds the
> card; we hold the customer id, the subscription id and whether it is paid.
> Reply to help@odile.fyi for anything on the charge; it is read by a person,
> and the target is an answer within 24 hours.

Never promise a refund, a credit or a discount. Say the target is an answer
within 24 hours and mean it.

### 5. "Can you log in to my machine?"

> No one at Odile can log in to a tenant machine. The only sign-in is yours. A
> support grant is a time-boxed door into the running machine. It ends for new
> logins when it expires or you revoke it; an open session keeps running until
> it exits. A root shell, for 24 hours — only when you grant it.
>
> A grant is a handover, not a peek. It can read the mounted disk, every file,
> the agent's memory and past prompts, the sign-in hash, and the backup password
> if the recovery file is still on the machine. It can add a LUKS keyslot or
> copy the volume key, so it can keep disk access after the grant.

## The escalation ladder

Say it in this order, and stop when the problem is solved:

1. The public answers: https://odile.fyi/faq
2. Ask me; I run on this machine and can read the logs.
3. `odile-diag` — the user emails the bundle to help@odile.fyi.
4. `odile-support grant --hours 24` — prints the expiry and the revoke command.
5. A rebuild: last resort when the recovery code is lost or the disk will not
   open. The machine is rebuilt, and the recovery file holds the backup password.

If a command is not on the machine yet, say so and gather the same facts by
hand: versions, disk usage, service state and the last 200 log lines.

## Never

| Never | Say instead |
|---|---|
| "Send me your sign-in password / the recovery code" | "Paste the output of `odile-diag`" |
| "I can reset it" | "Reset it on the machine with your recovery code" |
| "I can log in and fix it" | "Grant support for 24 hours" |
| "We can restore everything" | "Keep the recovery file; it holds the backup password" |
| "Refund", "credit", "discount" | "The target is an answer within 24 hours" |
| "zero-knowledge", "unhackable", "trustless" | The approved sentence in the answer above |

## Where the long version lives

On the repo, `docs/FAQ.md` is the support source and
`templates/emails/support.md` is the reply template. On a tenant machine the
public page at https://odile.fyi/faq is the long version; read it only if the
user asks for detail beyond this skill.
''',
    ),
    (
        "odile-recipes",
        "skills/odile-recipes/SKILL.md",
        "e7e6cf85139acb7fd2e8f2dfcb5dc331fe40e6a763ea6d9ee84df5d8b23c273a",
        r'''---
name: odile-recipes
description: Morning brief, watchdog, research memo, CSV chart.
---

# Odile — four recipes

Load this when the user asks for a morning brief, a watchdog, a research memo or
a chart from a CSV. Each recipe is a complete job: do it in this turn, write the
file, and confirm the path with `ls -l`. A promise is not a result.

| Recipe | They say | Lands in |
|---|---|---|
| Morning brief | "brief", "run my brief", "set up my brief" | `~/briefs/YYYY-MM-DD.md` |
| Silent watchdog | "watch this page", "tell me when it changes" | `~/.hermes/scripts/watch-<slug>.py`, `~/.hermes/watch/<slug>.json`, `~/workspace/watchdog/<slug>.md` |
| Research memo | "research X", "look into X", "write me a memo" | `~/workspace/memos/YYYY-MM-DD-<slug>.md` |
| CSV chart | "analyse this CSV", "chart this file" | `~/workspace/analysis/<stem>-report.md` and `<stem>-chart.svg` |

Rules that hold for all four:

- Say "I", never "we". No emoji, no exclamation marks.
- One recipe per turn. If the user asks for two, finish one and say the other
  is next; do not interleave.
- Write the file even when a lookup fails. One honest line about the failure
  beats an empty promise.
- Never invent a number, a source, a row count or a schedule. If you did not
  read it, say so.
- End with the absolute path and the size, from `ls -l`. The file browser is
  the proof.

## 1. Morning brief

**When.** They ask for today's brief, or to set up, change or repair the daily
brief. If persistent memory has no `onboarded_at`, this is a first run: load
`odile-first-run` and follow that skill instead — it asks the five questions,
writes today's brief and creates the 07:00 job.

**Read first.** Memory holds `Name:`, `Time zone:` (plus `City:` when they
named one), `Wants:` (three things) and `Brief: HH:MM <IANA>`. If an answer is
missing, ask one question; store every answer you get in memory (target `user`)
in the same turn — `Name: …`, `Time zone: <IANA>` (and `City: …`),
`Wants: <a>; <b>; <c>` — and never re-ask an answer you hold. If the time zone
is missing, ask "Where are you? A city is enough." and store the IANA zone. If
the three things are missing, ask for three. One question at a time.

**Run it now.** Build today's brief:

1. Get the date in their zone, not UTC:
   `TZ=<zone> date +%F`.
2. One line of weather for the city:
   `curl -s 'https://wttr.in/<city>?format=3'`.
   If it fails, write "Weather unavailable." and continue.
3. One short item per thing in `Wants:`, looked up now with `web_search`.
   Include a link. If a lookup fails, one honest line for that thing.
4. Write `~/briefs/<date>.md` with the terminal, markdown, under 200 words:
   the date and zone; the weather line; the three items; one sentence on what
   you will watch tomorrow.
5. `mkdir -p ~/briefs` first, then `ls -l ~/briefs/<date>.md`.

**Schedule it.** Load `cronjob_manage` (`tool_search` for "cron schedule" if it
is not in the tool list). Read the existing jobs first: there is at most one job
named `Morning brief`. If it exists, edit it; never create a second one. Create
the job with these fields (the repo template is
`templates/cron/morning-brief.json`):

- name: `Morning brief`
- schedule: `<minute> <hour> * * *` from `Brief: HH:MM <IANA>`
- timezone: the IANA zone
- deliver: `local`
- enabled_toolsets: `["web", "terminal"]`
- prompt: the block below, with every `<placeholder>` replaced. It must be
  self-contained: the job runs in a fresh session and cannot see this chat.

```
Morning brief for <name>, <zone>. Write ~/briefs/<today YYYY-MM-DD>.md, where the date is today in <zone> (TZ=<zone> date +%F). Include the date; one line of weather for <city> from https://wttr.in/<city>?format=3; one short item with a link for each of: <thing 1>; <thing 2>; <thing 3>; and one sentence on what you will watch tomorrow. Use web_search. Under 200 words. If a lookup fails, say so in one line and continue. Write the file even if some lookups fail.
```

Verify, do not assume: read `$HOME/.hermes/cron/jobs.json` and confirm the job
is enabled, its expression matches the time asked for, its timezone is the IANA
name, and `next_run_at` is in the future. If the cron tool fails, retry once;
if it still fails, say the brief file is there and the schedule needs one more
try. Never claim a schedule you did not create.

**Done.** Say, in two sentences, where the file is and when the next one lands.
If this turn also scheduled the job, name the local time and zone.

## 2. Silent watchdog

**When.** They ask to watch a page, a price, a listing, a status page or a
document, and to hear only when it changes. This is not a recurring summary: a
tick with no change must produce no message.

**What you need.** A URL and what to watch on it. If the URL is missing, ask
one question: "Which page should I watch?" If the URL is there but the thing to
watch is vague ("watch this page"), watch the whole page text. If they say "the
price", use a match for the price on that page: fetch it once, find the price
string, and build a regex with one capture group around the number.

**The config.** Pick a slug from the host and the thing watched, lowercase
letters, digits and hyphens: `example-com-pricing`. Write
`~/.hermes/watch/<slug>.json`:

```json
{
  "version": 1,
  "url": "https://example.com/pricing",
  "label": "Example pricing — Pro plan",
  "match": "Pro plan[^$]*\$([0-9.]+)",
  "group": 1,
  "max_failures": 3,
  "log": "~/workspace/watchdog/<slug>.md"
}
```

`match` is a Python regular expression with one capture group, or `null` to
watch the whole page text. `group` defaults to 1 when the pattern has a group.
Keep the label human: it is the first line of every alert. A local
`http://127.0.0.1:<port>/...` URL is fine: the script runs on this machine. The
script writes the state file; never write or edit the state by hand.

**The script.** Write the block below byte for byte to
`~/.hermes/scripts/watch-<slug>.py` (use `write_file`, then `chmod 755`). The
script derives its slug from its own filename: `watch-<slug>.py` reads
`~/.hermes/watch/<slug>.json`. Do not edit the script per watch; the config
carries every difference. Do not write your own watchdog: a hand-written script
is not this recipe.

After writing it, check the copy:
`sha256sum ~/.hermes/scripts/watch-<slug>.py` must print
`ec137ee9abcdd30358451761c2f432fadb2646f55dafc914807b82f472ff07ee`. If it prints anything else, the file is not the tested
script; rewrite it from the block before you schedule anything.

<!-- BEGIN watch.py -->
```python
#!/usr/bin/python3
"""Odile watchdog — print nothing unless the watched value changed.

This is the script half of the silent-watchdog recipe. The odile-recipes skill
copies it to ``~/.hermes/scripts/watch-<slug>.py`` and writes one config per
watch at ``~/.hermes/watch/<slug>.json``:

    {
      "version": 1,
      "url": "https://example.com/pricing",
      "label": "Example pricing — Pro plan",
      "match": "Pro plan[^$]*\\$([0-9.]+)",   optional; group 1 by default
      "group": 1,                              optional; 0 means the whole match
      "max_failures": 3,                       consecutive failures before one alert
      "log": "~/workspace/watchdog/example-pricing.md"   optional
    }

Contract
--------
* First successful read writes the baseline and prints nothing.
* A read identical to the baseline prints nothing.
* A changed read prints one alert and stores the new value; the next identical
  read is silent again, so one change produces one alert.
* A fetch failure is not a change. Failures are counted; the script prints one
  alert when ``max_failures`` consecutive tries fail, then stays silent until
  the source recovers. Recovery resets the counter.
* Alerts are also appended to the watch log (one line per alert), so the file
  browser carries the history even when chat delivery is off.

Exit codes: 0 a normal tick (silent or an alert); 2 a broken config; 1 anything
the scheduler should surface as a failed script.

    python3 watch-example.py --debug        # diagnostics on stderr, never stdout
    python3 watch-example.py --test-change  # show the alert shape, change nothing
    python3 watch-example.py --print-value  # print the extracted value, then exit
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATE_VERSION = 1
TIMEOUT_SECONDS = 20
MAX_BYTES = 512 * 1024
USER_AGENT = "odile-watch/1 (+https://odile.fyi)"
PREVIEW_CHARS = 120
STATE_CHARS = 500


class ConfigError(Exception):
    """The per-watch config is missing or unusable."""


def hermes_home() -> Path:
    """The tenant's Hermes home, or ~/.hermes when the env is absent."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    return Path(raw) if raw else Path.home() / ".hermes"


def watch_slug(script: Path) -> str:
    """The slug is the script's own filename: watch-<slug>.py."""
    stem = script.stem
    return stem[len("watch-"):] if stem.startswith("watch-") else stem


def load_config(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {path}") from exc
    except ValueError as exc:
        raise ConfigError(f"config is not valid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config must be a JSON object: {path}")
    url = str(data.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ConfigError(f"config needs an http(s) url: {path}")
    label = str(data.get("label") or "").strip()
    if not label:
        raise ConfigError(f"config needs a label: {path}")
    match = data.get("match")
    if match is not None and not isinstance(match, str):
        raise ConfigError(f"config match must be a string: {path}")
    group = data.get("group")
    if group is not None and (not isinstance(group, int) or group < 0):
        raise ConfigError(f"config group must be a non-negative integer: {path}")
    max_failures = data.get("max_failures", 3)
    if not isinstance(max_failures, int) or max_failures < 1:
        raise ConfigError(f"config max_failures must be a positive integer: {path}")
    return data


def fetch(url: str, timeout: float = TIMEOUT_SECONDS) -> str:
    """Bounded GET; raises OSError/ValueError on any failure."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 — http(s) checked
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError(f"response larger than {MAX_BYTES} bytes")
    return body.decode("utf-8", errors="replace")


def collapse(text: str) -> str:
    return " ".join(text.split())


def extract_value(body: str, match: str | None, group: int | None) -> str:
    """The watched value: a regex match, or the whole body with whitespace collapsed."""
    if not match:
        return collapse(body)
    found = re.search(match, body, re.DOTALL)
    if found is None:
        raise ValueError(f"pattern not found: {match!r}")
    index = group if group is not None else (1 if found.groups() else 0)
    try:
        return found.group(index).strip()
    except IndexError as exc:
        raise ValueError(f"match group {index} does not exist") from exc


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def preview(value: str) -> str:
    text = collapse(value)
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1] + "…"


def load_state(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return None
    return data


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_log(path: Path, line: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")
    except OSError as exc:
        print(f"watch: could not append {path}: {exc}", file=sys.stderr)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def change_alert(label: str, url: str, old: str, new: str) -> str:
    return f"Watchdog: {label}\nChanged: {preview(old)} → {preview(new)}\n{url}"


def failure_alert(label: str, url: str, failures: int, error: str) -> str:
    return f"Watchdog: {label}\nCould not read {url} on {failures} consecutive tries: {collapse(error)}"


def run_once(config: dict, state: dict | None, body: str | None, error: str | None = None):
    """Decide one tick. Returns ``(state, alert_or_None, summary)``.

    Pure except for the caller's save: the tests drive it with a fake body or a
    fake error, so the state machine is checked without a network.
    """
    label = str(config["label"])
    url = str(config["url"])
    max_failures = int(config.get("max_failures", 3))
    state = dict(state or {})
    failures = int(state.get("failures", 0))
    alerted_failure = bool(state.get("alerted_failure", False))

    if body is None:
        failures += 1
        alert = None
        if failures >= max_failures and not alerted_failure:
            alert = failure_alert(label, url, failures, error or "unknown error")
            alerted_failure = True
        state.update({"version": STATE_VERSION, "failures": failures,
                      "alerted_failure": alerted_failure, "checked_at": utc_now()})
        return state, alert, f"failure {failures}/{max_failures}"

    value = extract_value(body, config.get("match"), config.get("group"))
    new_hash = digest(value)
    if state.get("hash") is None:
        state.update({"version": STATE_VERSION, "hash": new_hash,
                      "preview": preview(value), "failures": 0,
                      "alerted_failure": False, "checked_at": utc_now()})
        return state, None, "baseline"

    if new_hash == state.get("hash"):
        if failures or alerted_failure:
            state.update({"failures": 0, "alerted_failure": False, "checked_at": utc_now()})
        return state, None, "unchanged"

    old = str(state.get("preview", ""))
    state.update({"version": STATE_VERSION, "hash": new_hash, "preview": preview(value),
                  "failures": 0, "alerted_failure": False, "checked_at": utc_now()})
    return state, change_alert(label, url, old, value), "changed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One Odile watchdog tick.")
    parser.add_argument("--config", help="per-watch config (default: $HERMES_HOME/watch/<slug>.json)")
    parser.add_argument("--state", help="state file (default: $HERMES_HOME/watch/<slug>.state.json)")
    parser.add_argument("--debug", action="store_true", help="diagnostics on stderr")
    parser.add_argument("--test-change", action="store_true",
                        help="print the alert shape for the current value, write nothing")
    parser.add_argument("--print-value", action="store_true",
                        help="print the extracted value and exit (debugging)")
    args = parser.parse_args(argv)

    script = Path(__file__).resolve()
    slug = watch_slug(script)
    home = hermes_home()
    config_path = Path(args.config).expanduser() if args.config else home / "watch" / f"{slug}.json"
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"watch: {exc}", file=sys.stderr)
        return 2

    state_path = Path(args.state).expanduser() if args.state else Path(
        config.get("state") or home / "watch" / f"{slug}.state.json").expanduser()
    log_path = Path(
        config.get("log") or Path.home() / "workspace" / "watchdog" / f"{slug}.md").expanduser()
    label = str(config["label"])
    url = str(config["url"])

    try:
        body = fetch(url)
        error = None
    except (OSError, ValueError, urllib.error.URLError) as exc:
        body, error = None, str(exc)

    if args.debug:
        print(f"watch: slug={slug} config={config_path} state={state_path}", file=sys.stderr)
        print(f"watch: url={url} fetch={'failed' if body is None else f'{len(body)} bytes'}"
              + (f" ({error})" if error else ""), file=sys.stderr)

    if body is not None and (args.print_value or args.test_change):
        try:
            value = extract_value(body, config.get("match"), config.get("group"))
        except ValueError as exc:
            print(f"watch: {exc}", file=sys.stderr)
            return 2
        if args.print_value:
            print(value)
            return 0
        current = load_state(state_path) or {}
        print(f"Watchdog: {label}\nChanged: (test) → {preview(value)}\n{url}")
        if args.debug:
            print(f"watch: test-change; stored preview={current.get('preview')!r}", file=sys.stderr)
        return 0

    state, alert, summary = run_once(config, load_state(state_path), body, error)
    try:
        save_state(state_path, state)
    except OSError as exc:
        print(f"watch: could not save {state_path}: {exc}", file=sys.stderr)
        return 1
    if alert:
        append_log(log_path, f"- {utc_now()} — {alert.splitlines()[1]}")
        print(alert)
    if args.debug:
        print(f"watch: {summary}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```
<!-- END watch.py -->

**Test it before you schedule it.** Four commands, four expected results:

| Command | Expected |
|---|---|
| `python3 ~/.hermes/scripts/watch-<slug>.py --print-value` | the value you will compare, nothing else |
| `python3 ~/.hermes/scripts/watch-<slug>.py --debug` | no stdout; stderr says `baseline` |
| `python3 ~/.hermes/scripts/watch-<slug>.py --debug` | no stdout; stderr says `unchanged` |
| `python3 ~/.hermes/scripts/watch-<slug>.py --test-change` | the alert shape, with `(test)`; state unchanged |

If `--print-value` fails, fix the match until it returns the number or line
you want. A broken match must not be scheduled.

**Schedule it.** Load `cronjob_manage`. Create one job with these fields (the
repo template `templates/cron/watchdog.json` records the same shape):

- name: `Watch: <label>`
- schedule: `every 6h` unless they asked for a frequency; use what they asked
- no_agent: `true`
- script: `watch-<slug>.py` (a bare filename under `~/.hermes/scripts/`)
- deliver: `local` — the alert is in the watch log and the job's run output;
  the Webapp/API session is stateless, so a chat delivery can mark a good alert
  as failed
- prompt: none — in no-agent mode the script is the job

Verify: the job is listed, enabled, its schedule is what they asked for, its
name is exactly `Watch: <label>`, its script is exactly `watch-<slug>.py`, its
mode is no-agent, and its deliver is `local`. If any field differs, fix the job
before you speak. The first tick writes the baseline and prints nothing; that
is correct, not a failure.

**Done.** Say what is watched, how often it is checked, and where the alert
lands: `~/workspace/watchdog/<slug>.md`, the history in the file browser, and
the job's run output. They will hear from it only when the value changes or the
page cannot be read three times. Email delivery is ODL-235. "Pause my watchdog"
means `cronjob_manage` edit with `enabled: false`; "stop watching" means remove
the job and leave the files.

## 3. Research memo

**When.** They ask to research a topic or answer a question with sources, and to
keep it. One memo, in a file, with links.

**Do.**

1. Turn the request into one question sentence. If the topic is one ambiguous
   word, ask one clarifying question; otherwise act.
2. `web_search` two or three queries: the plain question, a primary-source
   query (`site:` an official domain when one exists), and a recent-news query.
3. `web_extract` the two or three best sources. Prefer primary sources — the
   organisation's own page, the filing, the paper, the documentation — over a
   summary of them.
4. Write `~/workspace/memos/<YYYY-MM-DD>-<slug>.md` with the terminal, under
   600 words:

```
# <title>
Date: <date> · For: <name if known>

## Question
<one sentence>

## What I found
- <finding> — [<source>](<url>)
- ...

## What I could not verify
- <the gap, or "Nothing material.">

## Sources
1. <title> — <url>
```

Every factual bullet carries a link. A number appears only if a source states
it. If two sources disagree, say both and name the disagreement. If fewer than
two sources survive extraction, write that in "What I could not verify".

5. `ls -l` the file, then answer in chat with three sentences and the path.

**Done.** The memo is in the file browser under `workspace/memos/`. The chat
answer is a summary, not the memo.

## 4. CSV chart

**When.** They upload or point at a CSV and ask what is in it, or ask for a
chart. The work happens on this machine with Python; no service is called.

**Find the file.** Use the path in their message. If there is none, take the
newest `.csv` under `~/workspace` (then `~/Downloads`, then `$HOME`) and say
which one you picked. If there is still none, ask: "Which CSV should I look at?"

**Read it.** Run Python with the standard library (`csv`, `collections`,
`statistics`, `html`). Do not use pandas or matplotlib; they are not installed.
Report the real numbers: rows, columns, each column's type (number, date,
text), missing values, and the top values of the text columns. Do not load a
file larger than 16 MiB into memory at once; count in a stream and aggregate.

**Write the report.** `~/workspace/analysis/<stem>-report.md`, under 400 words:

```
# <stem>.csv
Date: <date>
Rows: <n> · Columns: <m>

## Columns
| column | type | missing | example |

## Findings
- <three to five sentences with the real numbers>

## Chart
`<stem>-chart.svg` — <what it shows, in one sentence>
```

**Draw one chart.** `~/workspace/analysis/<stem>-chart.svg`, 960×540, brand
paper and ink: background `#F6F0E4`, text `#1C1813` and `#4A4136`, bars
`#1C1813`, grid `rgba(28,24,19,.18)`. Choose the shape the data supports:

- a text column and a number column: the number summed (or averaged, if the
  column is a rate) by the top eight categories, sorted descending;
- a text column alone: the top eight value counts;
- numbers alone: a histogram of the first number column, eight bins.

Horizontal bars, category labels on the left, value at the end of each bar,
the title naming both columns, and a footer `odile · <date> · <rows> rows`.
Truncate a label at 28 characters. Escape `&`, `<` and `>` in text. If there
are more than eight categories, say "top 8 of <n>" in the title. Do not draw a
second chart; one clear chart beats four crowded ones.

**Done.** `ls -l` both files. In chat: the row count, the one finding that
matters, and the two paths. Say what the chart does not show if the data is
thin or has many missing values.
''',
    ),
)
# END TENANT BUNDLE

TENANT_CONFIG_TEMPLATE = """\
# Odile tenant profile — written by odile-install (ODL-203); never hand-edited.
#
# The installer carries this file byte-for-byte and renders the model, the
# Webapp port and the public URL from the seed and the layout.
# `--self-test` fails if the embedded copy and this file differ.
#
# Five managed blocks. Everything else in the written file belongs to Hermes
# and survives a re-run: the installer merges these blocks in place.
#   * model            — provider "custom" needs base_url to resolve.
#   * agent            — this model rejects Hermes's default reasoning effort.
#   * custom_providers — inference goes straight to NEAR AI with this machine's
#                        own key; there is no local proxy.
#   * webapp           — loopback bind, public_url so the sign-in gate is on.
#   * onboarding       — Hermes's own first-run chat is off; Odile's ritual is
#                        the first run, and no profile-build offer is made.
#
# The display block is not managed the same way: it carries the Odile skin
# (ODL-240), whose file the installer writes beside this config at
# skins/odile.yaml. `ensure_skin` merges `skin: odile` and keeps every other
# display key Hermes or the owner adds.
model:
  default: "{model}"
  provider: "custom"
  base_url: "https://cloud-api.near.ai/v1"

agent:
  reasoning_effort: "high"

custom_providers:
  - name: Near AI
    base_url: https://cloud-api.near.ai/v1
    key_env: HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY
    model: {model}

webapp:
  enabled: true
  host: 127.0.0.1
  port: {webapp_port}
  public_url: "{public_url}"

onboarding:
  run: false
  profile_build: "off"

display:
  skin: odile
"""

# --- templates/skin.yaml (embedded byte-for-byte; --self-test parity) ---
SKIN_YAML = """# Odile — the Webapp skin (ODL-240).
#
# The installer carries this file byte-for-byte and writes it to
# $HERMES_HOME/skins/odile.yaml; `display.skin: odile` in the tenant config
# makes the gateway resolve it. It is a Hermes skin, not a bundle fork: the
# palette is the engine's, and `customCSS` is the seam the engine documents for
# the few things a palette cannot say (heading face, the wordmark's clear
# space, the developer status bar).
#
# Palette: BRAND.md §Palette. Paper #F6F0E4, ink #1C1813, soft #4A4136, the
# structural line composited on paper #CFC9BE, and the error red #8F3418 as
# the only saturated colour. There is no accent hue: every accent role is ink,
# so nothing default-Hermes gold survives the conversion.
name: odile
description: Paper and ink — Odile's quiet Webapp skin

colors:
  background: "#F6F0E4"
  ui_text: "#1C1813"
  banner_text: "#1C1813"
  banner_title: "#1C1813"
  banner_dim: "#4A4136"
  ui_label: "#4A4136"
  ui_accent: "#1C1813"
  banner_accent: "#1C1813"
  ui_border: "#CFC9BE"
  banner_border: "#CFC9BE"
  ui_ok: "#4A4136"
  ui_warn: "#4A4136"
  ui_error: "#8F3418"
  ui_tool: "#1C1813"
  ui_thinking: "#4A4136"
  prompt: "#1C1813"
  input_rule: "#CFC9BE"
  response_border: "#CFC9BE"
  session_label: "#1C1813"
  session_border: "#CFC9BE"
  status_bar_bg: "#FBF6EA"
  status_bar_text: "#1C1813"
  status_bar_strong: "#1C1813"
  status_bar_dim: "#4A4136"
  status_bar_good: "#4A4136"
  status_bar_warn: "#4A4136"
  status_bar_bad: "#8F3418"
  status_bar_critical: "#8F3418"
  completion_menu_bg: "#FBF6EA"
  completion_menu_current_bg: "#E0DACF"
  selection_bg: "#CFC9BE"
  shell_dollar: "#4A4136"
  voice_status_bg: "#FBF6EA"
  diff_added: "#4A4136"
  diff_removed: "#8F3418"
  syntax_string: "#1C1813"
  syntax_number: "#1C1813"
  syntax_keyword: "#1C1813"
  syntax_comment: "#4A4136"

branding:
  agent_name: Odile
  response_label: " · Odile "
  goodbye: "Goodbye."
  help_header: "Commands"

tool_prefix: "·"

customCSS: |
  /* Odile — paper and ink (ODL-240).
   *
   * The skin engine paints the palette; this block reaches the few things a
   * palette cannot say. Every value is BRAND.md's, and no font host is
   * contacted: the stack below is system-local, the closest available serif on
   * the machine. BRAND.md §Typography allows the fallback stack to carry the
   * page when the embedded face cannot.
   */
  :root {
    --odile-serif: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, "DejaVu Serif", serif;
  }

  /* Headings: the closest available serif, at the sheet's weight. Body stays
   * the bundle's quiet sans. */
  h1, h2, h3, h4, h5, h6,
  [role="heading"] {
    font-family: var(--odile-serif) !important;
    font-weight: 500 !important;
    letter-spacing: -0.02em !important;
  }

  /* The empty state's wordmark: the brand face at the 32 px floor, ink, one
   * line, .88 line height, -.03em tracking, and 16 px (0.5 x 32 px) of clear
   * space below. `!important` beats the vendor's fit-text inline size and its
   * `text-midground` + `mix-blend-plus-lighter` utility, which would otherwise
   * turn ink into a white smudge on paper. */
  p.wordmark {
    display: block !important;
    font-family: var(--odile-serif) !important;
    font-size: 32px !important;
    font-weight: 500 !important;
    line-height: 0.88 !important;
    letter-spacing: -0.03em !important;
    text-transform: none !important;
    color: #1C1813 !important;
    mix-blend-mode: normal !important;
    margin-block-end: 16px !important;
    text-align: center;
  }
  p.wordmark span {
    font: inherit !important;
    letter-spacing: inherit !important;
    color: inherit !important;
    mix-blend-mode: inherit !important;
  }

  /* The status bar is developer chrome — backend state, workspace, density and
   * the client version. BRAND.md §Product surfaces: a panel has one job. */
  footer[data-slot="statusbar"] {
    display: none !important;
  }

  /* Success is not a colour. BRAND.md §Palette: the only saturated colour is
   * the error red, and colour never carries meaning alone. applyTheme writes
   * --ui-success inline, so the stylesheet needs !important to settle it. */
  :root {
    --ui-success: #4A4136 !important;
  }

  /* The About update check is a hard-coded emerald utility in the bundle;
   * success is ink, like --ui-success. */
  [class*="text-emerald-"] {
    color: #4A4136 !important;
  }

  /* BRAND.md §Accessibility tokens: ::selection is paper on ink. */
  ::selection {
    background: #1C1813 !important;
    color: #F6F0E4 !important;
  }

  /* The About credit line the brand script adds ("Powered by Hermes"):
   * quiet, 12.5 px, soft — the open-source core is a trust feature. */
  .odile-powered-by {
    margin: 4px 0 0;
    font-size: 12.5px;
    line-height: 1.5;
    color: #4A4136;
  }
"""

# --- templates/webapp-brand.js (embedded byte-for-byte; --self-test parity) ---
BRAND_JS = r"""/* Odile Webapp brand — the names and the credit a skin cannot reach (ODL-240).
 *
 * The Webapp is Hermes's desktop UI. A Hermes skin (skins/odile.yaml) themes
 * the palette, the custom CSS and the agent name for the CLI/TUI, but v0.23.0
 * still hard-codes four user-visible strings in the bundle:
 *
 *   <title>                 "Hermes", and "<screen> · Hermes" once a session opens
 *   p.wordmark              "HERMES AGENT" on the empty chat
 *   composer placeholder    "Give Hermes a task", from a rotating prompt list
 *   About > Version         the vendor hero "Hermes" (kept: it is the credit)
 *
 * and the About panel carries no "Powered by Hermes" line at all. This script
 * is installed by the installer into the same-origin bundle and linked from
 * the shell — no bundle fork, one same-origin request.
 *
 * It also sets the first-visit appearance to the Odile skin. The desktop
 * resolves a stored skin name against the gateway's skin registry only after
 * `gateway.ready` seeds it, so a fresh browser (no localStorage) would
 * otherwise paint the bundle's default until the user picked a skin by hand.
 * The write happens only when the browser has no saved appearance at all; a
 * later choice by the owner wins and is never overwritten. Until the registry
 * arrives, an !important stylesheet holds Odile's paper and ink over the
 * bundle's blue, then removes itself when the real theme is written.
 *
 * Two false status surfaces the browser bridge leaves over the first screen
 * are hidden by text: the one-second "Software rendering active" toast and the
 * permanent "Waking up …" card (the SPA asks for /api/webapp/socket-ticket,
 * which 404s in v0.23.0). Only those nodes are hidden; the notifications
 * region and real errors are untouched.
 *
 * The vendor's active skin can carry `typography.fontUrl`, and the theme code
 * injects a stylesheet link for it at runtime. BRAND.md §Typography: no font
 * host is ever contacted. Removing the link after insertion is too late (the
 * request has started), so the insertion itself is intercepted: a link marked
 * `data-hermes-theme-font` loses its href before it reaches <head>.
 *
 * Installed by installer/odile_install.py (ensure_webapp_brand); the tag is
 * parse-blocking so the font guard and the first-visit key run before the
 * vendor's module graph. A Webapp rebuild drops the tag; the next installer
 * run puts it back.
 */
;(() => {
  'use strict'

  const NAME = 'Odile'
  const THEME_KEY = 'hermes-desktop-theme-v2'
  const PROFILE_THEMES_KEY = 'hermes-desktop-profile-themes-v1'
  const POWERED_BY = 'Powered by Hermes'
  const NEEDLE = 'Software rendering active'
  const WAKING = 'Waking up'
  const REGION = '[aria-label="Notifications"]'
  const WORDMARK = 'p.wordmark'
  const PLACEHOLDER_ATTRS = ['data-placeholder', 'placeholder']
  const PLACEHOLDER = PLACEHOLDER_ATTRS.map((attr) => '[' + attr + ']').join(', ')
  const THEME_FONT = 'link[data-hermes-theme-font]'
  const TITLE_SUFFIXES = [' · Hermes Agent', ' · Hermes']
  const EARLY_STYLE = 'odile-early-paint'
  let themeFontGuard = false

  // True when this browser already holds an appearance: a global theme key or
  // a per-profile one. On a first visit both are absent and the Odile skin is
  // the default (see chooseDefaultSkin).
  function hasSavedAppearance() {
    try {
      if (localStorage.getItem(THEME_KEY)) {
        return true
      }
      const profiles = JSON.parse(localStorage.getItem(PROFILE_THEMES_KEY) || '{}')
      return Boolean(profiles && typeof profiles === 'object' && Object.keys(profiles).length)
    } catch (_) {
      // Storage unavailable (private mode / quota): treat it as a first visit.
      // The early guard only paints Odile's own palette, so it is harmless.
      return false
    }
  }

  // First visit: pick Odile, but never overwrite an appearance the owner has
  // already chosen (globally or per profile).
  function chooseDefaultSkin() {
    if (hasSavedAppearance()) {
      return
    }
    try {
      localStorage.setItem(THEME_KEY, 'odile')
      // The shell's pre-paint script reads these before the bundle runs; a
      // first visit should open on paper, not a white frame that turns paper
      // a second later. applyTheme() keeps both keys fresh after this.
      localStorage.setItem('hermes-boot-background', '#F6F0E4')
      localStorage.setItem('hermes-boot-color-scheme', 'light')
    } catch (_) {
      // Storage unavailable (private mode / quota): the gateway still offers
      // the skin in Appearance; nothing else depends on the key.
    }
  }

  // First paint: until `gateway.ready` resolves a skin name against the
  // gateway registry, the bundle paints its own palette — a blue accent on the
  // sidebar labels and the connection state, about two seconds on a first
  // visit. Hold the settled values in an !important stylesheet so the first
  // frame is already Odile, then drop the sheet as soon as applyTheme() writes
  // its own inline `--theme-primary`. After that the owner's choice wins.
  function guardFirstPaint() {
    if (hasSavedAppearance()) {
      return
    }
    const root = document.documentElement
    if (!root || !document.head) {
      return
    }
    const style = document.createElement('style')
    style.id = EARLY_STYLE
    style.textContent =
      ':root{' +
      '--theme-primary:#1C1813 !important;' +
      '--theme-foreground:#1C1813 !important;' +
      '--theme-background-seed:#F6F0E4 !important;' +
      '--theme-midground:#1C1813 !important}'
    document.head.appendChild(style)
    let observer = null
    let timer = 0
    const release = () => {
      // The bundle's own boot writes #0053fd before any skin resolves; that is
      // not a theme. Wait for the resolved value applyTheme() writes.
      const value = root.style.getPropertyValue('--theme-primary').trim()
      if (!value || value.toLowerCase() === '#0053fd') {
        return
      }
      style.remove()
      if (observer) {
        observer.disconnect()
      }
      clearTimeout(timer)
    }
    observer = new MutationObserver(release)
    observer.observe(root, { attributes: true, attributeFilter: ['style'] })
    // A skin that never resolves must not pin the palette forever.
    timer = setTimeout(() => {
      style.remove()
      observer.disconnect()
    }, 20000)
    release()
  }

  function isThemeFont(node) {
    return Boolean(node && node.nodeType === 1 && node.matches && node.matches(THEME_FONT))
  }

  function stripThemeFont(link) {
    link.removeAttribute('href')
    link.setAttribute('rel', 'alternate')
  }

  function stripThemeFonts() {
    for (const link of document.querySelectorAll(THEME_FONT)) {
      stripThemeFont(link)
      link.remove()
    }
  }

  function guardThemeFonts() {
    if (themeFontGuard || !document.head) {
      return
    }
    themeFontGuard = true
    const head = document.head
    const wrap = (name) => {
      const original = head[name]
      if (typeof original !== 'function') {
        return
      }
      head[name] = function (...nodes) {
        for (const node of nodes) {
          if (isThemeFont(node)) {
            stripThemeFont(node)
          }
        }
        return original.apply(this, nodes)
      }
    }
    for (const name of ['appendChild', 'insertBefore', 'replaceChild', 'append', 'prepend', 'replaceChildren']) {
      wrap(name)
    }
  }

  function hideNotice(region) {
    for (const card of region.children) {
      if (card.textContent && card.textContent.includes(NEEDLE)) {
        card.style.display = 'none'
      }
    }
  }

  function hideWaking() {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
    let node
    while ((node = walker.nextNode())) {
      if (!node.nodeValue || !node.nodeValue.includes(WAKING)) {
        continue
      }
      const card = node.parentElement
      if (card && card.tagName === 'DIV' && card.classList.contains('font-mono')) {
        card.style.display = 'none'
      }
    }
  }

  function renameWordmark() {
    for (const wordmark of document.querySelectorAll(WORDMARK)) {
      if ((wordmark.textContent || '').trim() !== NAME) {
        wordmark.textContent = NAME
      }
      if (wordmark.getAttribute('aria-label') !== NAME) {
        wordmark.setAttribute('aria-label', NAME)
      }
    }
  }

  // The new-session composer rotates its prompt from a list that includes
  // "Give Hermes a task". The prompt lives in an attribute and is painted by
  // the bundle's `:empty::before`; a skin cannot reach either, so any value
  // that still names the vendor agent is renamed in place. The bundle may
  // rewrite the attribute when the composer remounts, which is why the
  // observer also watches these two attributes.
  function renamePlaceholders() {
    for (const node of document.querySelectorAll(PLACEHOLDER)) {
      for (const attr of PLACEHOLDER_ATTRS) {
        const value = node.getAttribute(attr)
        if (value && value.includes('Hermes')) {
          node.setAttribute(attr, value.split('Hermes').join(NAME))
        }
      }
    }
  }

  function renameTitle() {
    const current = document.title
    if (!current || current === NAME) {
      return
    }
    let next = current
    if (current === 'Hermes' || current === 'Hermes Agent') {
      next = NAME
    } else {
      for (const suffix of TITLE_SUFFIXES) {
        if (current.endsWith(suffix)) {
          next = current.slice(0, -suffix.length) + ' · ' + NAME
          break
        }
      }
    }
    if (next !== current) {
      document.title = next
    }
  }

  // About > Version & updates: after "Version <x.y.z>", add the credit line
  // BRAND.md asks for. The vendor hero "Hermes" stays: About is where the
  // open-source core is named.
  function addPoweredBy() {
    const hero = Array.from(document.querySelectorAll('h2')).find(
      (h) =>
        (h.textContent || '').trim() === 'Hermes' &&
        h.nextElementSibling &&
        /^Version\b/.test((h.nextElementSibling.textContent || '').trim())
    )
    if (!hero) {
      return
    }
    const host = hero.parentElement
    if (!host || host.querySelector('.odile-powered-by')) {
      return
    }
    const line = document.createElement('p')
    line.className = 'odile-powered-by'
    line.textContent = POWERED_BY
    hero.nextElementSibling.insertAdjacentElement('afterend', line)
  }

  function scan() {
    const region = document.querySelector(REGION)
    if (region) {
      hideNotice(region)
    }
    hideWaking()
    renameWordmark()
    renamePlaceholders()
    renameTitle()
    addPoweredBy()
  }

  function touchesChrome(node) {
    if (!node || node.nodeType !== 1) {
      return false
    }
    if (typeof node.closest === 'function' && (node.closest(REGION) || node.closest(WORDMARK))) {
      return true
    }
    if (
      typeof node.matches === 'function' &&
      (node.matches(REGION) || node.matches(WORDMARK) || node.matches(PLACEHOLDER))
    ) {
      return true
    }
    if (node.textContent && (node.textContent.includes(WAKING) || node.textContent.includes('Version'))) {
      return true
    }
    return (
      typeof node.querySelector === 'function' &&
      Boolean(node.querySelector(REGION) || node.querySelector(WORDMARK) || node.querySelector(PLACEHOLDER))
    )
  }

  function watchHead() {
    new MutationObserver(() => {
      renameTitle()
      stripThemeFonts()
    }).observe(document.head, {
      childList: true,
      subtree: true,
      characterData: true,
    })
  }

  function start() {
    scan()
    stripThemeFonts()
    const observer = new MutationObserver((records) => {
      for (const record of records) {
        if (record.type === 'attributes') {
          // The composer rewrote its prompt; rename it before it paints.
          if (record.attributeName && PLACEHOLDER_ATTRS.includes(record.attributeName)) {
            renamePlaceholders()
          }
          continue
        }
        if (
          record.type === 'characterData' &&
          record.target.parentElement &&
          record.target.parentElement.closest(WORDMARK)
        ) {
          scan()
          return
        }
        for (const node of record.addedNodes) {
          if (touchesChrome(node)) {
            scan()
            return
          }
        }
      }
    })
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: PLACEHOLDER_ATTRS,
    })
  }

  guardFirstPaint()
  chooseDefaultSkin()
  guardThemeFonts()
  watchHead()

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true })
  } else {
    start()
  }
})()
"""

TENANT_ENV_TEMPLATE = """\
# Odile tenant .env — an allowlist, not a copy of a seat .env.
# Written by odile-install (ODL-203); mode 0600; outside git.
#
# Exactly these three keys. The NEAR inference key is NOT here: it lives in
# /etc/odile/key.env (0600 root), which odile-agent.service reads through
# EnvironmentFile=, so the profile .env carries no inference credential.
#
# HERMES_DASHBOARD_BASIC_AUTH_USERNAME       the Odile sign-in name
# HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH  scrypt hash, generated on the machine
# HERMES_DASHBOARD_BASIC_AUTH_SECRET         signing key for the session cookie
HERMES_DASHBOARD_BASIC_AUTH_USERNAME={username}
HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH={password_hash}
HERMES_DASHBOARD_BASIC_AUTH_SECRET={secret}
"""

# The tenant backup's two units (ODL-215 follow-up). They are the exact
# templates `scripts/backup.py install` renders, so a machine installed by the
# installer and one installed by the program carry the same unit; the parity
# test in tests/test_installer_copy.py formats both and compares the bytes.
# The paths are the layout's, so a simulation writes the same shape under its
# scratch root. `{repo_arg}` is empty: `--repo-file` is the seed's URL.
BACKUP_SERVICE_TEMPLATE = """\
[Unit]
Description=Odile tenant backup (restic, nightly)
Documentation=file:///usr/local/lib/odile/backup.py
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {script} run --repo-file {repo_file}{repo_arg} --recovery {recovery} --home {home} --key {key} --known-hosts {known_hosts} --status {status} --slug {slug}
ExecStartPost=-/usr/bin/python3 {script} heartbeat --status {status} --slug {slug} --url {url} --key-dir {heartbeat_dir}
Nice=10
IOSchedulingClass=idle
"""

BACKUP_TIMER_TEMPLATE = """\
[Unit]
Description=Odile tenant backup, daily at 03:00

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
"""

AGENT_UNIT_TEMPLATE = """\
# Odile agent — installed by odile-install (ODL-203); edit this program, not the unit.
#
# The gateway runs as `{user}` and serves the Webapp on loopback only; Caddy is
# the public listener. The agent has NOPASSWD sudo inside its VM (D12), so this
# unit deliberately keeps NO NoNewPrivileges and NO filesystem sandbox: either
# would make `sudo apt install` inside the agent's terminal fail. Only
# PrivateTmp is kept; it does not interfere with sudo.
[Unit]
Description=Odile agent — Hermes gateway and Webapp for {slug}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User={user}
Group={group}
Environment=HOME={home}
Environment=HERMES_HOME={hermes_home}
Environment=PATH={node_path}:/usr/local/bin:/usr/bin:/bin
EnvironmentFile={key_env}
WorkingDirectory={home}
ExecStart={launcher} gateway run
Restart=always
RestartSec=3
TimeoutStopSec=90
KillMode=mixed
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""

ATTEST_UNIT_TEMPLATE = """\
# Odile boot report — installed by odile-install (ODL-397); edit this program,
# not the unit.
#
# Servury's bb-seed copies the boot report to {boot_report} (0644) before
# switch-root. The report is per boot and does not change while the machine
# runs, so one oneshot at boot writes the served copy; a reboot refreshes it.
# There is no timer and no listener: Caddy serves the file.
[Unit]
Description=Odile boot report — write the served /attestation document
After=local-fs.target
Before={agent_unit}.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 {source} --boot-report {boot_report} {served}
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths={var}

[Install]
WantedBy=multi-user.target
"""

CADDY_SITE_TEMPLATE = """\
# BEGIN {begin}
# {fqdn} — the Odile tenant Webapp, served by the `{user}` gateway on
# 127.0.0.1:{webapp_port}. Managed by odile-install (ODL-203); edit this
# program, not the live Caddyfile.
#
# The sign-in gate is Hermes's own dashboard basic-auth provider. This block
# terminates TLS, forwards the browser's Host (public_url builds the callback
# and origin check from it), and streams chat tokens as they are produced.
#
# `/attestation` is public: the boot report is the machine's own evidence, not
# app data. The route serves exactly one file under {var}; every other path
# falls through to the Webapp, so nothing else in that directory is reachable.
#
# `/first-visit` is the one page that sets the sign-in (ODL-457). It is a
# separate loopback service because the Webapp is gated by the very hash the
# page writes; Caddy cannot reload itself on that transition, so the route is
# always present and the page itself refuses a spent token. The route is exact
# (`/first-visit*`), so no other path reaches the service.
#
# `/manifest` and `/manifest.sig` are the signed app-layer manifest (ODL-472).
# They proxy to the loopback listener only when the machine holds the signed
# pair, so a classic install with no pair keeps the Webapp catch-all instead of
# proxying to a dead port. `{{first_visit_handle}}` below carries whichever of
# these exact routes exist on this machine.
{fqdn} {{
	encode zstd gzip

	header {{
		Server odile
		Strict-Transport-Security "max-age=31536000"
		X-Robots-Tag "noindex, nofollow"
	}}

	handle /attestation {{
		header Cache-Control "no-store"
		header Content-Type "application/json"
		rewrite * /attestation.json
		root * {var}
		file_server
	}}

	{first_visit_handle}
	handle {{
		reverse_proxy 127.0.0.1:{webapp_port} {{
			header_up Host {{host}}
			flush_interval -1
		}}
	}}

	log {{
		output discard
	}}
}}
# END {begin}
"""

# The /verify and /first-visit routes, spliced only when the first-visit
# service is part of this machine. They share one loopback port: the service
# that sets the sign-in also renders the machine's access report (ODL-473).
# One value, so the site template keeps working when a classic install has no
# first-visit page; Caddy never proxies to a dead port.
CADDY_FIRST_VISIT_HANDLE = """\
	handle /verify {{
		header Cache-Control "no-store"
		reverse_proxy 127.0.0.1:{port}
	}}

	handle /first-visit* {{
		header Cache-Control "no-store"
		reverse_proxy 127.0.0.1:{port}
	}}
"""

# The /manifest and /manifest.sig routes (ODL-472), spliced only when the
# machine holds the signed release pair: the listener verifies the pair before
# it serves, and Caddy only exposes the two exact paths. Same shape as the
# first-visit handle, so a classic install without a pair has no dead route.
CADDY_MANIFEST_HANDLE = """\
	handle /manifest {{
		header Cache-Control "no-store"
		reverse_proxy 127.0.0.1:{port}
	}}
	handle /manifest.sig {{
		header Cache-Control "no-store"
		reverse_proxy 127.0.0.1:{port}
	}}
"""

FIRST_VISIT_UNIT_TEMPLATE = """\
# Odile first visit — installed by odile-install (ODL-457); edit this program,
# not the unit.
#
# One page, one job: set the Odile sign-in on this machine. It runs as root
# because it writes the tenant's 0600 .env and restarts the agent unit, binds
# loopback only, and Caddy is the public listener (/first-visit). The token is
# spent on the POST; after that the page answers 410 for good.
[Unit]
Description=Odile first visit — set the sign-in on the guest
After=network-online.target
Wants=network-online.target
# The agent's .env is read at start; the first-visit POST restarts it itself.
# Only Before= is set: After={unit}.service as well would be an ordering cycle.
Before={unit}.service

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 {source} --bind 127.0.0.1 --port {port} --state {state} --env-file {env_file} --page {page} --verify-page {verify_page} --report-file {report} --attest-file {attest} --report-command 'systemctl restart --no-block odile-access-report.service' --recovery-file {recovery} --reload-command 'systemctl restart {unit}'
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
# The .env is rewritten through a sibling temp file, so the directory is the
# writable path; the state and its lock live in /etc/odile.
ReadWritePaths={hermes_home} {etc}

[Install]
WantedBy=multi-user.target
"""

ACCESS_REPORT_UNIT_TEMPLATE = """\
# Odile access report — installed by odile-install (ODL-473); edit this
# program, not the unit.
#
# /verify shows this machine's own five lines, not a bench block. This oneshot
# runs the strict bar's report as root at boot, writes
# /var/lib/odile/access-report.txt 0644, and the first-visit service renders
# it at /verify. It runs again after the first visit (the first-visit POST
# starts this unit when the tenant's hash replaces the placeholder) and on
# every boot.
#
# Read-only on the machine: the reporter reads /proc/cmdline, the attestation
# document, every authorized_keys and the tenant .env; it writes only the
# report file. The pin it passes to --expect is resolved by the wrapper
# (tenant-image.pin, then the boot record, then the boot report).
[Unit]
Description=Odile access report — write the machine's own /verify report
After=local-fs.target odile-attest-boot.service
Wants=odile-attest-boot.service
Before={agent_unit}.service

[Service]
Type=oneshot
RemainAfterExit=yes
# systemd sets no HOME for a system service; the reporter reads it at startup.
Environment=HOME=/root
ExecStart={wrapper}
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={var}

[Install]
WantedBy=multi-user.target
"""

SSHD_TEMPLATE = """\
# Odile — installed by odile-install (ODL-203). Key-only SSH.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
"""

SUDOERS_TEMPLATE = "{user} ALL=(ALL) NOPASSWD:ALL\n"

SKILL_TEMPLATE = """\
---
name: odile-home
description: Facts about this Odile machine.
---

# Odile home

You are Odile, the agent on this machine. The human you work for is the only
keyholder: there is no operator SSH key and no password escrow.

- You run as `{user}` and have NOPASSWD sudo. You can read every file on the
  machine.
- Your workspace is `{workspace}`. Files you write there survive a restart.
- Inference goes directly to NEAR AI at `{near_base_url}` with this machine's
  own key. The key is root-only, at /etc/odile/key.env, and the agent unit
  reads it through `EnvironmentFile=`.
- The Webapp is gated by the Odile sign-in password. Never print `~/.hermes/.env`.
- The recovery file at /etc/odile/recovery.txt holds the backup password and the
  certificate fingerprint. The user downloaded it on the first visit; it stays on
  this machine, so show it if they ask.
"""

WORKSPACE_README = """\
# Your workspace

This directory belongs to you and your Odile agent. Files here survive a
restart. The agent runs as `{user}` with sudo; inference goes directly to
NEAR AI with this machine's own key.
"""


# --------------------------------------------------------------------------
# small helpers


def run(command: list[str], *, check: bool = False, timeout: float | None = None,
        cwd: str | Path | None = None, env: dict[str, str] | None = None,
        input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command, text=True, capture_output=True, check=False,
        timeout=timeout, cwd=cwd, env=env, input=input_text,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(command)} failed ({result.returncode}): "
            f"{(result.stderr or result.stdout).strip()[-2000:]}"
        )
    return result


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chown(path: str | Path, uid: int, gid: int) -> None:
    """chown, tolerating an unprivileged owner setting its own ids."""
    try:
        os.chown(path, uid, gid)
    except PermissionError:
        stat = os.stat(path)
        if (stat.st_uid, stat.st_gid) != (uid, gid):
            raise


def write_atomic(path: Path, data: bytes, mode: int, uid: int | None = None,
                 gid: int | None = None) -> None:
    """Write through a sibling temp file, fsync, chown, rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        if uid is not None:
            _chown(tmp_name, uid, gid if gid is not None else uid)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def ensure_dir(path: Path, mode: int, uid: int | None = None, gid: int | None = None,
               dry: bool = False, label: str | None = None) -> bool:
    if path.is_dir():
        changed = False
        current = path.stat().st_mode & 0o777
        if current != mode:
            print(f"{'would chmod' if dry else 'chmod':<12} {path} 0o{current:03o} -> 0o{mode:03o}")
            if not dry:
                os.chmod(path, mode)
            changed = True
        if uid is not None and not dry:
            _chown(path, uid, gid if gid is not None else uid)
        return changed
    print(f"{'would create' if dry else 'create':<12} {label or path} (mode 0o{mode:03o})")
    if not dry:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
        if uid is not None:
            _chown(path, uid, gid if gid is not None else uid)
    return True


def ensure_file(target: Path, desired: bytes, mode: int, uid: int | None, gid: int | None,
                dry: bool, *, label: str | None = None, detail: str = "",
                backup: bool = False) -> bool:
    current = target.read_bytes() if target.is_file() else None
    if current == desired:
        print(f"same         {label or target}")
        return False
    print(f"{'would write' if dry else 'write':<12} {label or target}{(' ' + detail) if detail else ''}")
    if dry:
        return True
    if backup and current is not None:
        stamp = utc_stamp()
        saved = target.with_name(f"{target.name}.bak-{stamp}")
        shutil.copy2(target, saved)
        print(f"backup       {saved}")
    write_atomic(target, desired, mode, uid, gid)
    return True


def user_ids(layout: "Layout", name: str | None = None) -> tuple[int, int]:
    try:
        entry = pwd.getpwnam(name or layout.user)
    except KeyError as exc:
        raise SystemExit(f"error: user {name or layout.user} does not exist") from exc
    return entry.pw_uid, entry.pw_gid


def group_id(name: str) -> int:
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError as exc:
        raise SystemExit(f"error: group {name} does not exist") from exc


def node_bin(layout: "Layout") -> str:
    """The Node Hermes installed, for the unit's PATH (first match wins)."""
    roots = sorted((layout.hermes_home / "tools").glob("node-*/bin"))
    return str(roots[-1]) if roots else "/usr/local/bin"


# --------------------------------------------------------------------------
# password hashing — the provider's format, stdlib only
#
# plugins/dashboard_auth/basic.hash_password() is:
#   scrypt$<n>$<r>$<p>$<base64 salt>$<base64 dk>
# with n=2**14, r=8, p=1, 16-byte salt, 32-byte key. The hash carries its own
# parameters, so this stays verifiable if the provider's defaults change.


def hash_password(password: str) -> str:
    n, r, p, dklen, salt_bytes = 2**14, 8, 1, 32, 16
    salt = secrets.token_bytes(salt_bytes)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=0
    )
    return "scrypt${}${}${}${}${}".format(
        n, r, p,
        base64.b64encode(salt).decode(),
        base64.b64encode(derived).decode(),
    )


def verify_password(password: str, encoded: str) -> bool:
    """Independent check of a hash string, mirroring the provider's verifier."""
    try:
        scheme, n_s, r_s, p_s, salt_b64, dk_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except (ValueError, TypeError):
        return False
    actual = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected), maxmem=0
    )
    return secrets.compare_digest(actual, expected)


def password_problem(password: str) -> str | None:
    """None when the password is acceptable; else the one thing to fix."""
    if len(password) < 16:
        return f"the Odile sign-in password needs at least 16 characters (got {len(password)})"
    return None


def generate_password() -> str:
    """32 characters, no shell-hostile punctuation to paste."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(32))


def resolve_signin_password(supplied: str | None, interactive: bool) -> str:
    """The password the user typed, or a generated 32-character one.

    A supplied value (flag or ODILE_SIGNIN_PASSWORD) is validated, never
    echoed. An empty answer at the prompt generates one and prints it once, so
    a user who does not want to choose still gets in. It is never written to a
    file — only its scrypt hash is.
    """
    candidate = supplied or os.environ.get("ODILE_SIGNIN_PASSWORD", "")
    if candidate:
        problem = password_problem(candidate)
        if problem:
            raise SystemExit(f"error: {problem}")
        return candidate
    if interactive and sys.stdin.isatty():
        first = getpass.getpass("Odile sign-in password (16+ characters, blank to generate): ")
        if not first:
            generated = generate_password()
            print(f"signin      generated Odile sign-in password (save it now): {generated}")
            return generated
        problem = password_problem(first)
        if problem:
            raise SystemExit(f"error: {problem}")
        second = getpass.getpass("Repeat the Odile sign-in password: ")
        if not secrets.compare_digest(first, second):
            raise SystemExit("error: the two passwords do not match")
        return first
    generated = generate_password()
    print(f"signin      generated Odile sign-in password (save it now): {generated}")
    return generated


# --------------------------------------------------------------------------
# .env allowlist


def count_env_violations(text: str, allowed: tuple[str, ...] = TENANT_ENV_KEYS) -> tuple[int, int]:
    """(lines outside the allowlist, seat-secret lines) in a .env body."""
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    outside = [line for line in lines if line.split("=", 1)[0] not in allowed]
    seat = [line for line in lines if SEAT_SECRET_RE.match(line)]
    return len(outside), len(seat)


def env_keys(text: str) -> list[str]:
    """The active (non-comment) keys in a .env body, in file order."""
    return [
        line.split("=", 1)[0]
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def env_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def render_env(username: str, password_hash: str, secret: str) -> str:
    return TENANT_ENV_TEMPLATE.format(
        username=username, password_hash=password_hash, secret=secret
    )


# --------------------------------------------------------------------------
# config merge — the managed blocks this program owns
#
# Everything else in a tenant config — and every key Hermes adds inside these
# blocks, such as custom_providers[].models / models_discovered — belongs to
# Hermes and must survive a re-run. A whole-file comparison would strip it.


def split_blocks(text: str) -> tuple[list[str], dict[str, list[str]]]:
    """Split a config into its leading lines and its top-level blocks by key.

    A block is its key line plus every line up to the next column-0 key, so
    nested keys, comments and blank lines stay with the block they belong to.
    """
    preamble: list[str] = []
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        match = TOP_LEVEL_KEY.match(line)
        if match:
            key = match.group(1)
            current = key
            blocks.setdefault(key, []).append(line)
        elif current is None:
            preamble.append(line)
        else:
            blocks[current].append(line)
    return preamble, blocks


def setting_line(line: str) -> str:
    """A setting line normalized just enough to ignore quoting and trailing space."""
    key, sep, value = line.rstrip().partition(":")
    if not sep:
        return line.rstrip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return f"{key.rstrip()}: {value}"


def settings(block: list[str]) -> list[str]:
    """The active setting lines of a block, comments and blanks removed."""
    return [
        setting_line(line)
        for line in block
        if line.strip() and not line.lstrip().startswith("#")
    ]


def block_contains(desired: list[str], current: list[str]) -> bool:
    """True when every setting in `desired` is present with the same value."""
    have = set(settings(current))
    return all(line in have for line in settings(desired))


def merge_block(desired: list[str], current: list[str]) -> list[str]:
    """`desired`'s settings, then every current line the template does not name.

    A current line whose key the template does name is dropped even if its
    value differs, so a drifted value cannot survive as a duplicate key. Lines
    Hermes owns (`models:`, `models_discovered:`, …) are kept; they are
    indented, so appending them at the end of the block leaves them inside the
    same mapping.
    """
    desired_keys = {setting_line(line).partition(":")[0] for line in settings(desired)}
    extra = [
        line.rstrip()
        for line in current
        if line.strip()
        and not line.lstrip().startswith("#")
        and setting_line(line).partition(":")[0] not in desired_keys
    ]
    tail = []
    for line in reversed(current):
        if line.strip():
            break
        tail.append(line)
    tail.reverse()
    return list(desired) + extra + tail


def merge_config(current: str, desired: str) -> str | None:
    """None when every managed block already matches, else the merged config.

    A config that carries the pre-onboarding four managed blocks is one this
    program wrote (a fresh Hermes seed has no ``custom_providers``): keep its
    other top-level blocks (`_config_version` included), merge each managed
    block in place, and append a managed block the file does not have yet —
    that is how an existing tenant gains ``onboarding`` without losing what
    Hermes or the owner put in the file. Anything else is a fresh install's
    seeded config, which this program replaces with the template wholesale.
    """
    current_preamble, current_blocks = split_blocks(current)
    _, desired_blocks = split_blocks(desired)
    if all(
        key in current_blocks and block_contains(desired_blocks[key], current_blocks[key])
        for key in MANAGED_KEYS
    ):
        return None
    if all(key in current_blocks for key in PRE_ONBOARDING_KEYS):
        out: list[str] = list(current_preamble)
        for key, lines in current_blocks.items():
            out.extend(merge_block(desired_blocks[key], lines) if key in MANAGED_KEYS else lines)
        for key in MANAGED_KEYS:
            if key not in current_blocks and key in desired_blocks:
                out.extend(desired_blocks[key])
        return "\n".join(out).rstrip("\n") + "\n"
    return desired


def config_version(paths: list[Path], default: int = 52) -> int:
    """The schema version of the seeded config, so the template never pins one."""
    for path in paths:
        try:
            match = re.search(r"^_config_version:\s*(\d+)", path.read_text(encoding="utf-8"), re.M)
        except OSError:
            continue
        if match:
            return int(match.group(1))
    return default


# --------------------------------------------------------------------------
# tenant bundle — $HOME/AGENTS.md and the first-run skills, carried inside
# this file and pinned by SHA-256. installer/embed_tenant.py is the only
# supported way to change the embedded copies.

MANAGED_SKILLS_MANIFEST = ".odile-managed-skills.json"


@dataclasses.dataclass(frozen=True)
class TenantFile:
    """One embedded file: where it lands, where it came from, and its pin."""

    target: str      # "AGENTS.md" under $HOME, or "skills/<name>/SKILL.md" under $HERMES_HOME
    source: str      # repo path, for audit
    sha256: str
    text: str

    def data(self) -> bytes:
        return self.text.encode("utf-8")

    @property
    def skill(self) -> str | None:
        parts = Path(self.target).parts
        if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
            return parts[1]
        return None


def tenant_files() -> tuple[TenantFile, ...]:
    """The embedded bundle: standing context first, then every embedded skill."""
    files = [TenantFile("AGENTS.md", "templates/tenant-agents.md",
                        TENANT_AGENTS_SHA256, TENANT_AGENTS_SOURCE)]
    for name, source, digest, text in TENANT_SKILLS:
        files.append(TenantFile(f"skills/{name}/SKILL.md", source, digest, text))
    return tuple(files)


def validate_tenant_files(files: tuple[TenantFile, ...] | list[TenantFile]) -> list[TenantFile]:
    """Every pin matches, every target is safe, and there is exactly one AGENTS.md.

    Raises SystemExit with the first problem; returns the files for chaining.
    """
    seen: set[str] = set()
    agents = 0
    for item in files:
        path = Path(item.target)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"error: unsafe embedded target {item.target!r}")
        actual = sha256(item.data())
        if not item.sha256 or actual != item.sha256:
            raise SystemExit(
                f"error: embedded {item.source} has sha256 {actual}, "
                f"the pinned hash is {item.sha256 or '(missing)'}"
            )
        if item.target == "AGENTS.md":
            agents += 1
            continue
        name = item.skill
        if name is None or not SKILL_NAME_RE.match(name):
            raise SystemExit(f"error: unsupported embedded target {item.target!r}")
        if name in seen:
            raise SystemExit(f"error: two embedded skills named {name!r}")
        seen.add(name)
    if agents != 1:
        raise SystemExit(f"error: the tenant bundle has {agents} AGENTS.md files, expected 1")
    return list(files)


def managed_skill_names(layout: "Layout") -> list[str]:
    """The skill names this installer wrote on the last run; [] when unknown."""
    path = layout.hermes_home / MANAGED_SKILLS_MANIFEST
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    names = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(names, list):
        return []
    return sorted(name for name in names
                  if isinstance(name, str) and SKILL_NAME_RE.match(name))


def ensure_tenant_files(layout: "Layout", dry: bool,
                        files: tuple[TenantFile, ...] | list[TenantFile] | None = None) -> bool:
    """Write $HOME/AGENTS.md and $HERMES_HOME/skills/<name>/SKILL.md for the bundle.

    A skill this installer wrote on an earlier run but the bundle no longer
    carries is removed, so a renamed or retired skill does not linger. Skills
    the user added themselves are never in the manifest and are left alone.
    """
    files = validate_tenant_files(tenant_files() if files is None else files)
    changed = False
    for item in files:
        base = layout.home if item.target == "AGENTS.md" else layout.hermes_home
        target = base / item.target
        if item.skill is not None:
            changed |= ensure_dir(target.parent, 0o700, *user_ids(layout), dry=dry,
                                  label=str(target.parent))
        changed |= ensure_file(target, item.data(), 0o644, *user_ids(layout), dry,
                               detail=f"({item.source}, sha256 {item.sha256[:12]}…)")

    wanted = sorted(name for item in files if (name := item.skill) is not None)
    previous = managed_skill_names(layout)
    for name in sorted(set(previous) - set(wanted)):
        path = layout.skills / name
        if path.is_dir() and not path.is_symlink():
            print(f"{'would remove' if dry else 'remove':<12} {path} (no longer in the bundle)")
            if not dry:
                shutil.rmtree(path)
            changed = True
    if previous != wanted:
        manifest = layout.hermes_home / MANAGED_SKILLS_MANIFEST
        body = json.dumps({"skills": wanted}, indent=2) + "\n"
        changed |= ensure_file(manifest, body.encode("utf-8"), 0o644, *user_ids(layout), dry,
                               detail="(the embedded skill names)")
    return changed


# --------------------------------------------------------------------------
# the layout: tenant ("/") or scratch root, one code path


@dataclasses.dataclass(frozen=True)
class Layout:
    mode: str                       # "tenant" or "scratch"
    root: Path
    user: str
    group: str
    home: Path
    hermes_home: Path
    install_dir: Path
    launcher: Path
    webapp_bundle: Path
    webapp_host: str
    webapp_port: int
    webapp_public_url: str
    etc: Path
    var: Path
    unit_dir: Path
    caddyfile: Path
    unit_name: str
    unit_target: Path
    expected_version_prefix: str
    installer_url: str
    installer_args: tuple[str, ...]
    hermes_commit: str
    first_visit_port: int = FIRST_VISIT_PORT

    def path(self, absolute: str) -> Path:
        if self.mode == "tenant":
            return Path(absolute)
        return self.root / absolute.lstrip("/")

    @property
    def seed_cache(self) -> Path:
        return self.etc / "seed.json"

    @property
    def seed_sig(self) -> Path:
        return self.etc / "seed.sig"

    @property
    def key_env(self) -> Path:
        """The seed's per-user inference key, 0600 root, read by the unit."""
        return self.etc / "key.env"

    @property
    def recovery(self) -> Path:
        return self.etc / "recovery.txt"

    @property
    def boot_record(self) -> Path:
        return self.etc / "boot-record.json"

    @property
    def lib_dir(self) -> Path:
        return self.path("/usr/local/lib/odile")

    @property
    def attest_source(self) -> Path:
        return self.path("/usr/local/lib/odile/attest.py")

    @property
    def backup_source(self) -> Path:
        """The nightly backup program the unit runs (ODL-215 follow-up)."""
        return self.path("/usr/local/lib/odile/backup.py")

    @property
    def backup_repo_file(self) -> Path:
        """The seed-written repository URL; the unit reads it, never a copy."""
        return self.etc / "backup-repo.txt"

    @property
    def backup_key_dir(self) -> Path:
        return self.etc / "backup-ssh"

    @property
    def backup_heartbeat_dir(self) -> Path:
        return self.etc / "heartbeat"

    @property
    def backup_status(self) -> Path:
        return self.var / "backup.json"

    @property
    def backup_service(self) -> Path:
        return self.unit_dir / "odile-backup-tenant.service"

    @property
    def backup_timer(self) -> Path:
        return self.unit_dir / "odile-backup-tenant.timer"

    @property
    def installer_copy(self) -> Path:
        """The path `--manifest` and `docs/CLAIMS.md` §4.6 hash (ODL-332 follow-up)."""
        return self.path("/usr/local/bin/odile-install.sh")

    @property
    def boot_report(self) -> Path:
        return self.path("/run/blackbox/attest.json")

    @property
    def served_attestation(self) -> Path:
        return self.var / "attestation.json"

    @property
    def agent_pid(self) -> Path:
        return self.var / "agent.pid"

    @property
    def smoke_marker(self) -> Path:
        return self.etc / "smoke.ok"

    @property
    def workspace(self) -> Path:
        return self.home / "workspace"

    @property
    def skills(self) -> Path:
        return self.hermes_home / "skills"

    @property
    def scripts(self) -> Path:
        return self.hermes_home / "scripts"

    @property
    def first_visit_source(self) -> Path:
        """The first-visit service the unit runs; root-owned, not the tenant's."""
        return self.path("/usr/local/lib/odile/first_visit.py")

    @property
    def first_visit_page(self) -> Path:
        return self.path("/usr/local/lib/odile/first-visit.html")

    @property
    def first_visit_state(self) -> Path:
        """Token hash + recovery hash + the one timestamp that spends them."""
        return self.etc / "first-visit.json"

    @property
    def first_visit_unit(self) -> Path:
        return self.unit_dir / "odile-first-visit.service"

    @property
    def verify_page(self) -> Path:
        """The /verify template the first-visit service renders on a tenant."""
        return self.path("/usr/local/lib/odile/verify.html")

    @property
    def access_report_source(self) -> Path:
        """The boot oneshot's wrapper; root-owned, resolves the pin and runs."""
        return self.path("/usr/local/lib/odile/access_report.sh")

    @property
    def verify_unlock_source(self) -> Path:
        """The strict bar's reporter, the only writer of the report file."""
        return self.path("/usr/local/lib/odile/verify_and_unlock.sh")

    @property
    def audit_source(self) -> Path:
        """The handoff audit a sceptic runs (ODL-208); root:root 0755."""
        return self.path("/usr/local/bin/odile-audit")

    @property
    def access_report_file(self) -> Path:
        return self.var / "access-report.txt"

    @property
    def access_report_unit(self) -> Path:
        return self.unit_dir / "odile-access-report.service"

    @property
    def fqdn(self) -> str:
        return urllib.parse.urlsplit(self.webapp_public_url).hostname or ""

    @classmethod
    def tenant(cls, *, user: str, webapp_port: int,
               public_url: str, commit: str = HERMES_COMMIT,
               first_visit_port: int = FIRST_VISIT_PORT) -> "Layout":
        home = Path("/home") / user
        hermes_home = home / ".hermes"
        install_dir = hermes_home / "hermes-agent"
        launcher = install_dir / ".hermes/bin/hermes"
        return cls(
            mode="tenant", root=Path("/"), user=user, group=user,
            home=home, hermes_home=hermes_home, install_dir=install_dir,
            launcher=launcher,
            webapp_bundle=install_dir / "apps/desktop/dist-webapp/index.html",
            webapp_host="127.0.0.1", webapp_port=webapp_port,
            webapp_public_url=public_url,
            etc=Path("/etc/odile"), var=Path("/var/lib/odile"),
            unit_dir=Path("/etc/systemd/system"),
            caddyfile=Path("/etc/caddy/Caddyfile"),
            unit_name="odile-agent",
            unit_target=Path("/etc/systemd/system/odile-agent.service"),
            expected_version_prefix=HERMES_VERSION_PREFIX,
            installer_url=INSTALLER_URL,
            installer_args=("--commit", commit, "--non-interactive",
                            "--skip-browser", "--skip-computer-use"),
            hermes_commit=commit,
            first_visit_port=first_visit_port,
        )

    @classmethod
    def scratch(cls, *, root: Path, user: str, webapp_port: int,
                public_url: str, commit: str = HERMES_COMMIT,
                first_visit_port: int = FIRST_VISIT_PORT) -> "Layout":
        base = root
        home = base / "home" / user
        hermes_home = home / ".hermes"
        install_dir = hermes_home / "hermes-agent"
        launcher = install_dir / ".hermes/bin/hermes"
        return cls(
            mode="scratch", root=base, user=user, group=user,
            home=home, hermes_home=hermes_home, install_dir=install_dir,
            launcher=launcher,
            webapp_bundle=install_dir / "apps/desktop/dist-webapp/index.html",
            webapp_host="127.0.0.1", webapp_port=webapp_port,
            webapp_public_url=public_url,
            etc=base / "etc/odile", var=base / "var/lib/odile",
            unit_dir=base / "etc/systemd/system",
            caddyfile=base / "etc/caddy/Caddyfile",
            unit_name="odile-agent",
            unit_target=base / "etc/systemd/system/odile-agent.service",
            expected_version_prefix=HERMES_VERSION_PREFIX,
            installer_url=INSTALLER_URL,
            installer_args=("--commit", commit, "--non-interactive",
                            "--skip-browser", "--skip-computer-use"),
            hermes_commit=commit,
            first_visit_port=first_visit_port,
        )


def webapp_heap_mb(meminfo: str | None = None) -> int:
    """The V8 old-space cap for the Webapp build, sized to the machine.

    ``vite`` transforms ~16k modules and peaks near 2.4 GB of anonymous RSS.
    V8 sizes its default heap from total RAM (about half, up to 4 GB), so on a
    small or busy host node grows until the kernel OOM-killer picks it —
    measured on HQ (7.5 GB, no swap): ``Killed process (node-MainThread)
    anon-rss:2447972kB``. A cap makes V8 collect instead; the same build then
    peaks at 2.1 GB and finishes. 60% of RAM, clamped to [1536, 3072] MB.
    """
    text = meminfo if meminfo is not None else Path("/proc/meminfo").read_text(encoding="utf-8")
    match = re.search(r"^MemTotal:\s+(\d+) kB", text, re.M)
    total_mb = int(match.group(1)) // 1024 if match else 2048
    return max(1536, min(3072, int(total_mb * 0.6)))


def as_user(layout: Layout, command: list[str], *, cwd: str | Path | None = None,
            timeout: float = 900,
            extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command as the layout's user with its HOME/HERMES_HOME/PATH."""
    env = {
        "HOME": str(layout.home),
        "HERMES_HOME": str(layout.hermes_home),
        "PATH": f"{node_bin(layout)}:/usr/local/bin:/usr/bin:/bin",
    }
    env.update(extra_env or {})
    if os.geteuid() == 0 and layout.user != pwd.getpwuid(0).pw_name:
        return run(
            ["sudo", "-u", layout.user, "-H", "env",
             *[f"{key}={value}" for key, value in env.items()], *command],
            cwd=cwd, timeout=timeout,
        )
    full = os.environ.copy()
    full.update(env)
    return run(command, cwd=cwd, timeout=timeout, env=full)


def gateway_env(layout: Layout) -> dict[str, str]:
    """A clean environment for a scratch gateway.

    A gateway launched from an agent shell inherits HERMES_KANBAN_* and
    HERMES_DELEGATED_CHILD_CONTEXT; its kanban dispatcher then tries to write
    the real board and fails every tick (measured 2026-11-07). The tenant unit
    has none of these, so scrubbing keeps the simulation honest.
    """
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("HERMES_KANBAN")
        and key not in {"HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_GATEWAY_PROFILE_PIN"}
    }
    env.update({
        "HOME": str(layout.home),
        "HERMES_HOME": str(layout.hermes_home),
        "PATH": f"{node_bin(layout)}:/usr/local/bin:/usr/bin:/bin",
    })
    # The tenant unit reads this same file through EnvironmentFile=; a scratch
    # gateway has no systemd, so it gets the identical variable at exec. That
    # is what makes /proc/<pid>/environ the honest proof for both.
    if layout.key_env.is_file():
        env.update(env_values(layout.key_env.read_text(encoding="utf-8")))
    return env


# --------------------------------------------------------------------------
# signature — detached Ed25519, verified against a key outside this file

# The published signing key (ODL-400). An install prints these two values and
# the user compares the digest with the published file out of band:
#
#     curl -s https://odile.fyi/odile-signing-key.pub | sha256sum
#
# They are not a trust anchor inside this program: a bundle and key swapped
# together would carry their own constants, which is why the comparison is
# kit-vs-key plus the user's check against the URL. `--self-test` pins this
# release's shipped file to them, so a key rotation is a deliberate change.
PUBLISHED_KEY_SHA256 = "13e8c2aee8263f550033448c8193a82b89498f533f52943e09c14d22e5b036e2"
PUBLISHED_KEY_FINGERPRINT = "SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8"


def key_fingerprint(pubkey_path: Path) -> str:
    """The key's SHA256 fingerprint over its SPKI DER, as scripts/manifest.py spells it."""
    proc = subprocess.run(
        ["openssl", "pkey", "-pubin", "-in", str(pubkey_path), "-outform", "DER"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise SystemExit(
            f"error: cannot read the public key {pubkey_path}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    digest = hashlib.sha256(proc.stdout).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def signing_key_values(pubkey_path: Path) -> tuple[str, str]:
    """(exact file sha256, key fingerprint) for the key this run will use."""
    return sha256(pubkey_path.read_bytes()), key_fingerprint(pubkey_path)


def print_signing_key(pubkey_path: Path) -> str | None:
    """Print the `signing key` line before the signature result; return the file digest.

    The digest is `sha256sum` of the bytes, so the user can compare it with the
    published file; the fingerprint is the same key in the spelling the manifest
    and update tools print. Nothing secret is in either value.
    """
    if not pubkey_path.is_file():
        print(f"signing key  missing ({pubkey_path})")
        return None
    digest, fingerprint = signing_key_values(pubkey_path)
    print(f"signing key  sha256 {digest} · {fingerprint}")
    return digest


def signing_key_problem(kit: dict, digest: str | None) -> str | None:
    """Why the kit's `signing_key_sha256` refuses this key, or None when it agrees."""
    expected = kit.get("signing_key_sha256")
    if expected is None:
        return None
    if not isinstance(expected, str) or not expected.strip():
        return f"signing_key_sha256 is not a SHA-256 hex digest: {expected!r}"
    expected = expected.strip().lower()
    if digest is None:
        return f"the kit names key {expected}, but no public key was found"
    if expected != digest:
        return f"the kit names key {expected}, the key in use is {digest}"
    return None


def read_signature(path: Path) -> bytes:
    raw = b"".join(path.read_bytes().split())
    if len(raw) == 64:
        return raw
    try:
        decoded = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise SystemExit(f"error: {path} is not a base64 or raw Ed25519 signature") from exc
    if len(decoded) != 64:
        raise SystemExit(f"error: {path} decodes to {len(decoded)} bytes, expected 64")
    return decoded


def verify_bytes(data: bytes, signature: bytes, pubkey_path: str | Path) -> bool:
    """True when `signature` is a valid Ed25519 signature of `data`."""
    with tempfile.NamedTemporaryFile(prefix="odile-sig-", delete=False) as handle:
        handle.write(signature)
        sig_path = handle.name
    try:
        proc = subprocess.run(
            ["openssl", "dgst", "-verify", str(pubkey_path), "-signature", sig_path],
            input=data, capture_output=True,
        )
        return proc.returncode == 0
    finally:
        try:
            os.unlink(sig_path)
        except OSError:
            pass


def verify_installer(installer: Path, signature_path: Path, pubkey_path: Path) -> tuple[bool, str]:
    if not installer.is_file():
        return False, f"installer {installer} is missing"
    if not signature_path.is_file():
        return False, f"signature {signature_path} is missing"
    if not pubkey_path.is_file():
        return False, f"public key {pubkey_path} is missing"
    try:
        signature = read_signature(signature_path)
    except SystemExit as exc:
        return False, str(exc)
    ok = verify_bytes(installer.read_bytes(), signature, pubkey_path)
    digest = sha256(installer.read_bytes())
    return ok, f"sha256 {digest}"


# --------------------------------------------------------------------------
# the seed: one POST, a signed answer, a verified cache


def load_recovery_kit(path: Path | None, args: argparse.Namespace) -> dict:
    """Read the recovery kit; flags win over the file, but never over the key.

    The kit is the trust anchor: it carries Odile's public key, the setup token
    and the instance id. `--pubkey` may name the same key separately; the seed
    and the installer signature are both checked against the kit's key.
    """
    kit: dict = {}
    if path is not None:
        try:
            kit = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SystemExit(f"error: cannot read recovery kit {path}: {exc}") from exc
        except ValueError as exc:
            raise SystemExit(f"error: recovery kit {path} is not JSON: {exc}") from exc
        if not isinstance(kit, dict):
            raise SystemExit(f"error: recovery kit {path} must be a JSON object")
    for flag, field in (("token", "setup_token"), ("instance_id", "instance_id"),
                        ("slug", "slug")):
        value = getattr(args, flag, None)
        if value:
            kit[field] = value
    if not kit.get("setup_token") and kit.get("token"):
        kit["setup_token"] = kit["token"]
    return kit


def kit_pubkey(kit: dict) -> Path | None:
    """The kit's PEM string as a temp file, or None when it has no key."""
    pem = kit.get("signing_public_key")
    if not pem:
        return None
    if not isinstance(pem, str) or "BEGIN PUBLIC KEY" not in pem:
        raise SystemExit("error: recovery kit signing_public_key is not a PEM public key")
    directory = tempfile.mkdtemp(prefix="odile-kit-")
    path = Path(directory) / "odile-signing-key.pub"
    path.write_text(pem if pem.endswith("\n") else pem + "\n", encoding="utf-8")
    os.chmod(path, 0o644)
    return path


def pem_path(data: bytes) -> Path:
    """Write PEM bytes exactly as given to a temp file (no newline is added).

    The seed signature covers these exact bytes, so a helpful `+ "\\n"` here
    would break the check it serves.
    """
    directory = Path(tempfile.mkdtemp(prefix="odile-seed-key-"))
    path = directory / "seed-signing-key.pub"
    path.write_bytes(data)
    os.chmod(path, 0o644)
    return path


SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def seed_key_candidates(pubkey: Path) -> list[Path]:
    """The keys a seed certificate may be certified by, most current first.

    The kit's key is the anchor; the installer's own `odile-signing-key.pub` is
    the same key in a shop that forgot the kit, and `.prev.pub` is the key being
    retired (ODL-514 ships it for one release), so a rotation does not strip the
    certificate from a seed served while the new one is being cut. Identical
    bytes are tried once.
    """
    candidates = [Path(pubkey), INSTALLER_PATH.parent / "odile-signing-key.pub",
                  INSTALLER_PATH.parent / "odile-signing-key.prev.pub"]
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        digest = sha256(path.read_bytes())
        if digest in seen:
            continue
        seen.add(digest)
        unique.append(path)
    return unique


def certify_seed_key(parsed: dict, candidates: list[Path]) -> Path:
    """The release key that certifies this seed's key, or SystemExit on one line.

    The certificate is the release key's Ed25519 signature over the seed public
    key bytes; `signing_key_sha256` names those bytes, so another key cannot be
    slipped in beside somebody else's certificate.
    """
    pem = parsed.get("signing_key")
    expected = parsed.get("signing_key_sha256")
    if not isinstance(pem, str) or "BEGIN PUBLIC KEY" not in pem:
        raise SystemExit("error: seed signing_key_sha256 is set and the seed carries no signing_key PEM")
    public = pem.encode("utf-8")
    if not isinstance(expected, str) or not SHA256_HEX_RE.match(expected.strip()):
        raise SystemExit("error: seed signing_key_sha256 is not a SHA-256 hex digest")
    digest = sha256(public)
    if digest != expected.strip().lower():
        raise SystemExit(
            f"error: the seed signing key is sha256 {digest}, signing_key_sha256 says "
            f"{expected.strip().lower()}")
    encoded = parsed.get("signing_key_cert")
    if not isinstance(encoded, str) or not encoded:
        raise SystemExit("error: seed signing_key_sha256 is set and the seed carries no signing_key_cert")
    try:
        certificate = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise SystemExit(f"error: seed signing_key_cert is not base64: {exc}") from exc
    if len(certificate) != 64:
        raise SystemExit(f"error: seed signing_key_cert is {len(certificate)} bytes, expected 64")
    if not candidates:
        raise SystemExit("error: no release public key beside this installer to check the seed certificate")
    for path in candidates:
        if verify_bytes(public, certificate, path):
            return path
    raise SystemExit(
        "error: the seed signing key certificate was not signed by "
        + " or ".join(str(path) for path in candidates))


def verify_seed_body(body: bytes, signature: bytes, pubkey: Path) -> tuple[dict, str]:
    """(parsed seed, which key verified it). The certificate path first.

    A seed that carries `signing_key_sha256` is checked through the certificate:
    a release public key certifies the seed key, the seed key signs the body. A
    seed without it (installed before ODL-514) is checked against the release key
    itself, which is the key that signed it.
    """
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SystemExit(f"error: seed response is not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("error: seed response must be a JSON object")
    if "signing_key_sha256" in parsed:
        certifier = certify_seed_key(parsed, seed_key_candidates(pubkey))
        path = pem_path(parsed["signing_key"].encode("utf-8"))
        if not verify_bytes(body, signature, path):
            raise SystemExit("error: seed response signature does NOT verify against the certified seed key")
        return parsed, str(certifier)
    if not verify_bytes(body, signature, pubkey):
        raise SystemExit("error: seed response signature does NOT verify against the recovery kit key")
    return parsed, str(pubkey)


def cached_seed(layout: Layout, pubkey: Path, instance_id: str, slug: str | None) -> dict | None:
    """The cache when its signature verifies and its fields still match."""
    if not (layout.seed_cache.is_file() and layout.seed_sig.is_file()):
        return None
    body = layout.seed_cache.read_bytes()
    try:
        signature = read_signature(layout.seed_sig)
    except SystemExit:
        return None
    try:
        parsed, certifier = verify_seed_body(body, signature, pubkey)
    except SystemExit as exc:
        print(f"warning: cached seed does not verify ({exc}); fetching again", file=sys.stderr)
        return None
    if "signing_key_sha256" in parsed:
        print(f"seed key     sha256 {parsed['signing_key_sha256']} certified by {certifier}")
    try:
        validate_seed(parsed, instance_id, slug)
    except SystemExit as exc:
        print(f"warning: cached seed no longer matches: {exc}", file=sys.stderr)
        return None
    return parsed


# --------------------------------------------------------------------------
# the boot check (ODL-396)


@dataclasses.dataclass(frozen=True)
class BootVerdict:
    """One boot report's decision, with the public numbers it carried."""

    ok: bool
    kind: str                      # "ok", "boot" (exit 5) or "usage" (exit 2)
    detail: str = ""
    release: str = ""
    measurement: str = ""
    fingerprint: str = ""
    cmdline_sha256: str = ""
    # The throwaway boot key's public line (base64 of `type blob`), from
    # bb.key= in the measured command line. ODL-473 writes it to
    # /root/.ssh/authorized_keys: the strict bar's report expects exactly one
    # authorized key — this one, unusable because its private half is shredded.
    boot_key_b64: str = ""
    same: bool = False


def _boot_failure(kind: str, detail: str, **fields: str) -> BootVerdict:
    return BootVerdict(
        ok=False, kind=kind, detail=detail,
        release=fields.get("release", ""),
        measurement=fields.get("measurement", ""),
        fingerprint=fields.get("fingerprint", ""),
        cmdline_sha256=fields.get("cmdline_sha256", ""),
    )


def cmdline_for_hash(cmdline: str) -> str:
    """The measured command line, with the trailing ` initrd=initrd` stripped."""
    text = cmdline.strip()
    suffix = " initrd=initrd"
    if text.endswith(suffix):
        return text[: -len(suffix)]
    return text


def normalise_measurement(value: str | None) -> str | None:
    """A 96-hex measurement, lowercased; None when it is not one."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if HEX96_RE.match(text) else None


def boot_key_fingerprint(value: str) -> str | None:
    """`bb.key` is base64 of an OpenSSH public key; return its SHA256 fingerprint.

    The measured command line carries one token, ``bb.key=<base64>``, because a
    kernel command line has no spaces to spare. Decode it once to the
    ``ssh-ed25519 AAAA… comment`` form and fingerprint the key blob exactly as
    ``ssh-keygen -lf`` does: SHA-256 over the wire format, base64, no padding.
    A trailing comment is not part of the blob and does not change the
    fingerprint; an authorized_keys options prefix (``command="…"``) is skipped
    the way Servury's own key reader skips it.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        text = base64.b64decode(value, validate=True).decode("utf-8", "replace")
    except ValueError:
        return None
    fields = text.split()
    for index, field in enumerate(fields[:-1]):
        if field.startswith(("ssh-", "ecdsa-", "sk-")):
            break
    else:
        return None
    try:
        blob = base64.b64decode(fields[index + 1], validate=True)
    except ValueError:
        return None
    if not blob:
        return None
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def assess_boot_report(payload: dict | None, *, expect: str | None, release: str | None,
                       kit_fingerprint: str | None,
                       record: dict | None) -> BootVerdict:
    """The pure decision: does this boot report match what the user verified?

    No I/O, no printing. ``kind`` maps to the caller's exit code: ``usage``
    means the command lacked a number it needs (exit 2); ``boot`` means the
    report or the boot record disagrees (exit 5, before the seed exchange).
    """
    if not isinstance(payload, dict):
        return _boot_failure("boot", "no boot report")
    status = payload.get("status")
    if status != "ok":
        return _boot_failure("boot", f"the boot report status is {status!r}, not 'ok'")
    encoded = payload.get("report_b64")
    if not isinstance(encoded, str) or not encoded.strip():
        return _boot_failure("boot", "the boot report has no report_b64")
    try:
        raw = base64.b64decode("".join(encoded.split()), validate=True)
    except ValueError as exc:
        return _boot_failure("boot", f"report_b64 is not base64: {exc}")
    if len(raw) != BOOT_REPORT_SIZE:
        return _boot_failure(
            "boot", f"report_b64 is {len(raw)} bytes, a SEV-SNP report is {BOOT_REPORT_SIZE}")
    version = int.from_bytes(raw[0:4], "little")
    if version != BOOT_REPORT_VERSION:
        return _boot_failure("boot", f"the report version is {version}, not {BOOT_REPORT_VERSION}")
    measurement = raw[MEASUREMENT_OFFSET:MEASUREMENT_END].hex()
    report_release = payload.get("release")
    if not isinstance(report_release, str) or not report_release.strip():
        return _boot_failure("boot", "the boot report has no release")
    cmdline = payload.get("cmdline")
    if not isinstance(cmdline, str) or not cmdline.strip():
        return _boot_failure("boot", "the boot report has no command line")
    tokens = cmdline.split()
    keys = [token[len("bb.key="):] for token in tokens if token.startswith("bb.key=")]
    ip4s = [token[len("bb.ip4="):] for token in tokens if token.startswith("bb.ip4=")]
    if len(keys) != 1 or not keys[0]:
        return _boot_failure(
            "boot", f"the command line carries {len(keys)} bb.key= field(s), exactly one is required")
    if len(ip4s) != 1 or not ip4s[0]:
        return _boot_failure(
            "boot", f"the command line carries {len(ip4s)} bb.ip4= field(s), exactly one is required")
    fingerprint = boot_key_fingerprint(keys[0])
    if fingerprint is None:
        return _boot_failure("boot", "bb.key is not base64 of an OpenSSH public key")
    fields = dict(release=report_release, measurement=measurement,
                  fingerprint=fingerprint, boot_key_b64=keys[0],
                  cmdline_sha256=sha256(
                      cmdline_for_hash(cmdline).encode("utf-8")))
    if kit_fingerprint is not None:
        if not isinstance(kit_fingerprint, str) or not kit_fingerprint.strip():
            return _boot_failure(
                "boot", "the recovery kit's boot_key_fingerprint is empty", **fields)
        if fingerprint.rstrip("=") != kit_fingerprint.strip().rstrip("="):
            return _boot_failure(
                "boot", f"the boot key is {fingerprint}, the recovery kit says {kit_fingerprint}",
                **fields)
    if release is not None and report_release != release:
        return _boot_failure(
            "boot", f"the boot report release {report_release!r} is not --release {release!r}",
            **fields)
    if expect is not None:
        want = normalise_measurement(expect)
        if want is None:
            return _boot_failure(
                "usage", f"--expect {expect!r} is not 96 hex characters", **fields)
        if measurement != want:
            return _boot_failure(
                "boot", f"the boot measurement is {measurement}, --expect says {want}", **fields)
        if (isinstance(record, dict)
                and record.get("measurement") == measurement
                and record.get("release") == report_release
                and record.get("cmdline_sha256") == fields["cmdline_sha256"]):
            return BootVerdict(ok=True, kind="ok", same=True,
                               release=fields["release"], measurement=fields["measurement"],
                               fingerprint=fields["fingerprint"],
                               cmdline_sha256=fields["cmdline_sha256"],
                               boot_key_b64=fields["boot_key_b64"])
        return BootVerdict(ok=True, kind="ok",
                           release=fields["release"], measurement=fields["measurement"],
                           fingerprint=fields["fingerprint"],
                           cmdline_sha256=fields["cmdline_sha256"],
                           boot_key_b64=fields["boot_key_b64"])
    if not isinstance(record, dict):
        return _boot_failure(
            "usage", "no --expect and no boot record: pass the measurement the verifier printed",
            **fields)
    recorded = record.get("measurement")
    if recorded != measurement:
        return _boot_failure(
            "boot",
            f"the boot record says {recorded}, this boot is {measurement}; "
            f"re-run the verifier and pass the new --expect {measurement}",
            **fields)
    moved = [name for name in ("release", "cmdline_sha256") if record.get(name) != fields[name]]
    if moved:
        return _boot_failure(
            "boot",
            f"the boot record's {', '.join(moved)} moved; re-run the verifier and "
            f"pass --expect {measurement} to record this boot",
            **fields)
    return BootVerdict(ok=True, kind="ok", same=True,
                       release=fields["release"], measurement=fields["measurement"],
                       fingerprint=fields["fingerprint"],
                       cmdline_sha256=fields["cmdline_sha256"],
                       boot_key_b64=fields["boot_key_b64"])


def read_boot_report(path: Path) -> tuple[dict | None, str]:
    """(the report object, an error). A missing or unreadable file is an error."""
    if not path.is_file():
        return None, f"no boot report at {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, f"cannot read the boot report {path}: {exc}"
    except ValueError as exc:
        return None, f"{path} is not JSON: {exc}"
    if not isinstance(data, dict):
        return None, f"{path} is not a JSON object"
    return data, ""


def read_boot_record(path: Path) -> dict | None:
    """The previous record, when it is one; a broken file is not a record."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"warning: cannot read the boot record {path}: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, dict) or data.get("schema") != BOOT_RECORD_SCHEMA:
        print(f"warning: {path} is not an {BOOT_RECORD_SCHEMA} record", file=sys.stderr)
        return None
    return data


def build_boot_record(verdict: BootVerdict, *, expected: str, source: str, vcpus: int,
                      installer_sha256: str, checked_at: str) -> dict:
    """The installer's copy of PIN-POLICY §4.3: the boot it installed on.

    ``artifacts`` is null because the pin entry carries the artifact hashes;
    this record is per machine, and the installer's own provenance is
    ``expected``/``source``/``installer_sha256``.
    """
    return {
        "schema": BOOT_RECORD_SCHEMA,
        "release": verdict.release,
        "measurement": verdict.measurement,
        "cmdline_sha256": verdict.cmdline_sha256,
        "vcpus": vcpus,
        "checked_at": checked_at,
        "expected": expected,
        "installer_sha256": installer_sha256,
        "source": source,
        "artifacts": None,
    }


def write_boot_record(path: Path, record: dict, *, uid: int | None = None) -> None:
    data = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    write_atomic(path, data, 0o644, uid=uid)


def vcpu_count(text: str | None = None) -> int:
    """The installing machine's CPUs, from /proc/cpuinfo (0 when unreadable)."""
    if text is None:
        try:
            text = Path("/proc/cpuinfo").read_text(encoding="utf-8")
        except OSError:
            text = ""
    return sum(1 for line in text.splitlines() if line.startswith("processor"))


def check_boot(layout: Layout, args: argparse.Namespace, kit: dict) -> BootVerdict | None:
    """Assert the boot report, then write the per-machine record.

    Called after ``verify_or_die`` and before ``load_or_fetch_seed``: a failure
    exits before the setup token is spent and before any secret is written. A
    scratch run with no report (the simulation) prints one line and continues.
    """
    explicit = getattr(args, "boot_report", None)
    report_path = Path(explicit).expanduser() if explicit else layout.path(BOOT_REPORT_DEFAULT)
    payload, error = read_boot_report(report_path)
    if payload is None:
        if layout.mode == "scratch" and not explicit:
            print("skip         boot check (simulation)")
            return None
        print(f"error: boot check failed: {error}", file=sys.stderr)
        print("       no seed was exchanged and no secret was written.", file=sys.stderr)
        raise SystemExit(5)
    verdict = assess_boot_report(
        payload,
        expect=getattr(args, "expect", None),
        release=getattr(args, "release", None),
        kit_fingerprint=kit.get("boot_key_fingerprint"),
        record=read_boot_record(layout.boot_record),
    )
    if not verdict.ok:
        if verdict.kind == "usage":
            print(f"error: {verdict.detail}", file=sys.stderr)
            raise SystemExit(2)
        print(f"error: boot check failed: {verdict.detail}", file=sys.stderr)
        print("       no seed was exchanged and no secret was written.", file=sys.stderr)
        raise SystemExit(5)
    print(f"boot         release {verdict.release} · {report_path}")
    print(f"             measurement {verdict.measurement}")
    print(f"             boot key    {verdict.fingerprint}")
    if verdict.same:
        print(f"same         boot record {layout.boot_record} (release {verdict.release})")
        return verdict
    vcpus = vcpu_count()
    record = build_boot_record(
        verdict, expected=verdict.measurement, source=str(report_path), vcpus=vcpus,
        installer_sha256=sha256(INSTALLER_PATH.read_bytes()), checked_at=utc_now(),
    )
    write_boot_record(layout.boot_record, record,
                      uid=0 if layout.mode == "tenant" else os.geteuid())
    print(f"write        {layout.boot_record} (boot record, release {verdict.release}, "
          f"{vcpus} vCPU, installer {record['installer_sha256'][:12]}…)")
    return verdict


def http_post_json(url: str, payload: dict, timeout: float = 30.0) -> tuple[int, bytes, dict[str, str]]:
    """POST JSON and return (status, body, headers), including 4xx bodies."""
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())
    except urllib.error.URLError as exc:
        raise SystemExit(f"error: cannot reach {url}: {exc.reason}") from exc


def validate_seed(body: dict, instance_id: str, slug: str | None = None) -> None:
    """The ODL-204 contract, enforced before a field is used."""
    missing = [key for key in SEED_KEYS if key not in body]
    if missing:
        raise SystemExit(f"error: seed is missing {', '.join(missing)}")
    forbidden = [key for key in SEED_FORBIDDEN if key in body]
    if forbidden:
        raise SystemExit(f"error: seed carries a forbidden field: {', '.join(forbidden)}")
    if not isinstance(body.get("near_api_key"), str) or not body["near_api_key"].strip():
        raise SystemExit("error: seed near_api_key is empty")
    if not isinstance(body.get("model"), str) or not body["model"].strip():
        raise SystemExit("error: seed model is empty")
    for field in ("support_public_key", "backup_repo_url", "hermes_version"):
        if not isinstance(body.get(field), str) or not body[field].strip():
            raise SystemExit(f"error: seed {field} is empty")
    if body.get("instance_id") != instance_id:
        raise SystemExit(
            f"error: seed is for instance {body.get('instance_id')!r}, expected {instance_id!r}"
        )
    if not isinstance(body.get("slug"), str) or not SLUG_RE.match(body["slug"]):
        raise SystemExit(f"error: seed slug {body.get('slug')!r} is malformed")
    if slug is not None and body["slug"] != slug:
        raise SystemExit(f"error: seed is for {body['slug']!r}, the kit says {slug!r}")
    mail = body.get("mail_relay")
    if not isinstance(mail, dict) or not all(k in mail for k in ("host", "port", "username", "secret")):
        raise SystemExit("error: seed mail_relay is incomplete")


def seed_signature(headers: dict[str, str]) -> bytes:
    value = headers.get("X-Odile-Signature") or headers.get("x-odile-signature") or ""
    if not value.startswith("ed25519="):
        raise SystemExit("error: seed response has no X-Odile-Signature: ed25519= header")
    try:
        signature = base64.b64decode(value.split("=", 1)[1], validate=True)
    except ValueError as exc:
        raise SystemExit("error: seed signature is not base64") from exc
    if len(signature) != 64:
        raise SystemExit(f"error: seed signature is {len(signature)} bytes, expected 64")
    return signature


def load_or_fetch_seed(layout: Layout, kit: dict, args: argparse.Namespace,
                       pubkey: Path) -> dict:
    """Cache first, exchange second, replay-with-a-valid-cache allowed."""
    token = kit.get("setup_token", "")
    instance_id = kit.get("instance_id", "")
    slug = kit.get("slug")
    if not TOKEN_RE.match(token):
        raise SystemExit("error: the recovery kit has no 64-hex setup token")
    if not INSTANCE_RE.match(instance_id):
        raise SystemExit("error: the recovery kit has no valid instance id")

    cached = cached_seed(layout, pubkey, instance_id, slug)
    if cached is not None:
        print(f"same         seed cache {layout.seed_cache} (signed, instance {instance_id})")
        return cached
    if args.dry_run:
        print(f"would fetch  seed from {args.seed_url} (instance {instance_id})")
        return {"slug": slug or "<slug>", "instance_id": instance_id,
                "near_api_key": "<from seed>", "model": "<from seed>",
                "support_public_key": "<from seed>", "backup_repo_url": "<from seed>",
                "mail_relay": {"host": "<from seed>", "port": 587,
                               "username": "<from seed>", "secret": "<from seed>"},
                "hermes_version": "<from seed>"}

    print(f"fetch        seed from {args.seed_url} (instance {instance_id})")
    status, body, headers = http_post_json(
        args.seed_url, {"token": token, "instance_id": instance_id}
    )
    if status == 410:
        reason = "gone"
        try:
            reason = json.loads(body.decode("utf-8")).get("reason", reason)
        except ValueError:
            pass
        raise SystemExit(
            f"error: seed exchange refused with 410 ({reason}) and no cached seed exists.\n"
            "The setup token is single-use; ask Odile for a new one, or restore "
            f"{layout.seed_cache} and {layout.seed_sig} from the first run."
        )
    if status != 200:
        detail = body.decode("utf-8", "replace").strip()[:500]
        raise SystemExit(f"error: seed exchange returned HTTP {status}: {detail}")
    signature = seed_signature(headers)
    parsed, certifier = verify_seed_body(body, signature, pubkey)
    if "signing_key_sha256" in parsed:
        print(f"seed key     sha256 {parsed['signing_key_sha256']} certified by {certifier}")
    validate_seed(parsed, instance_id, slug)
    write_atomic(layout.seed_cache, body, 0o600, *current_ids(layout))
    write_atomic(layout.seed_sig, base64.b64encode(signature) + b"\n", 0o600,
                 *current_ids(layout))
    print(f"write        seed cache {layout.seed_cache} (0600, signature verified)")
    return parsed


def current_ids(layout: Layout) -> tuple[int, int]:
    if layout.mode == "tenant":
        return 0, 0
    return os.geteuid(), os.getegid()


# --------------------------------------------------------------------------
# passwords, recovery, certificate


def render_recovery(slug: str, instance_id: str, backup_password: str,
                    fingerprint: str | None, fqdn: str, written: str | None = None) -> str:
    cert = fingerprint or "(pending — filled in when you download this file on the first-visit page)"
    return f"""\
Odile recovery file
===================
Tenant:              {slug}
Instance:            {instance_id}
Written:             {written or utc_now()}

Backup password:     {backup_password}
Certificate SHA-256: {cert}

This file is the only copy of the backup password. Download it on this machine's
first-visit page and keep it somewhere you can reach when the machine is gone.
The file stays on the machine; ask your agent to show it again if you need it.

Lose it and the encrypted backup cannot be opened; Odile holds no copy.
"""


def parse_recovery(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    match = re.search(r"^Written:\s*(.+?)\s*$", text, re.M)
    if match:
        found["written"] = match.group(1)
    match = re.search(r"^Backup password:\s*(\S+)\s*$", text, re.M)
    if match:
        found["backup_password"] = match.group(1)
    match = re.search(r"^Certificate SHA-256:\s*(.+?)\s*$", text, re.M)
    if match:
        found["certificate"] = match.group(1)
    return found


def ensure_recovery(layout: Layout, slug: str, instance_id: str, fingerprint: str | None,
                    dry: bool, supplied_password: str | None = None) -> tuple[bool, str, bool]:
    """Write the recovery file; return (changed, backup_password, created)."""
    existing: dict[str, str] = {}
    current_text = ""
    if layout.recovery.is_file():
        current_text = layout.recovery.read_text(encoding="utf-8")
        existing = parse_recovery(current_text)
    backup = supplied_password or existing.get("backup_password") or generate_password()
    if supplied_password and len(supplied_password) < 16:
        raise SystemExit("error: the backup password needs at least 16 characters")
    if fingerprint is None and existing.get("certificate", "").startswith("sha256:"):
        fingerprint = existing["certificate"]
    desired = render_recovery(slug, instance_id, backup, fingerprint, layout.fqdn or "<host>",
                              written=existing.get("written"))
    created = not layout.recovery.is_file()
    if current_text == desired:
        print(f"same         {layout.recovery} (backup password, certificate {'present' if fingerprint else 'placeholder'})")
        return False, backup, False
    print(f"{'would write' if dry else 'write':<12} {layout.recovery} "
          f"(backup password, certificate {'present' if fingerprint else 'placeholder'})")
    if not dry:
        write_atomic(layout.recovery, desired.encode("utf-8"), 0o600, *current_ids(layout))
    return True, backup, created


def confirm_recovery(args: argparse.Namespace) -> bool:
    if args.i_have_copied_it:
        print("confirm      recovery file copied off the machine (--i-have-copied-it)")
        return True
    if not sys.stdin.isatty():
        print(
            "error: the recovery file must be copied off the machine before exit.\n"
            f'       Re-run with --i-have-copied-it only after typing "{CONFIRM_PHRASE}".',
            file=sys.stderr,
        )
        return False
    try:
        answer = input(f'Type exactly "{CONFIRM_PHRASE}" to finish: ')
    except EOFError:
        return False
    if answer.strip() != CONFIRM_PHRASE:
        print("error: not confirmed; the recovery file is still on the machine.", file=sys.stderr)
        return False
    print("confirm      recovery file copied off the machine")
    return True


def certificate_fingerprint(layout: Layout, fqdn: str) -> str | None:
    """SHA-256 of the issued certificate, from Caddy's store or the TLS socket."""
    roots = [
        layout.path("/var/lib/caddy/.local/share/caddy/certificates"),
        layout.path("/root/.local/share/caddy/certificates"),
    ]
    for root in roots:
        for path in sorted(root.glob(f"*/{fqdn}/{fqdn}.crt")):
            result = run(["openssl", "x509", "-noout", "-fingerprint", "-sha256", "-in", str(path)])
            match = re.search(r"=\s*([0-9A-Fa-f:]{32,})", result.stdout)
            if result.returncode == 0 and match:
                return "sha256:" + match.group(1).replace(":", "").lower()
    # Fallback: ask the live socket (works when the store path differs).
    if fqdn:
        proc = subprocess.run(
            f"openssl s_client -connect {fqdn}:443 -servername {fqdn} </dev/null 2>/dev/null "
            "| openssl x509 -noout -fingerprint -sha256",
            shell=True, text=True, capture_output=True, timeout=30,
        )
        match = re.search(r"=\s*([0-9A-Fa-f:]{32,})", proc.stdout)
        if proc.returncode == 0 and match:
            return "sha256:" + match.group(1).replace(":", "").lower()
    return None


# --------------------------------------------------------------------------
# Caddy splice — BEGIN/END markers, validate, backup, restart, rollback


def splice_marked(existing: str, block: str, begin: str, end: str) -> str:
    """Return `existing` with the managed block replaced or appended.

    Exactly zero or one marker pair is allowed; an unbalanced or duplicated
    pair is an error rather than a guess, so a half-edited Caddyfile can never
    be overwritten silently.
    """
    block_lines = block.rstrip("\n").splitlines()
    lines = existing.splitlines()
    begins = [i for i, line in enumerate(lines) if line.strip() == begin]
    ends = [i for i, line in enumerate(lines) if line.strip() == end]
    if len(begins) != len(ends) or len(begins) > 1:
        raise ValueError(
            f"expected at most one '{begin}' / '{end}' pair, "
            f"found {len(begins)} begin and {len(ends)} end markers"
        )
    if not begins:
        return "\n".join(lines + [""] + block_lines).rstrip("\n") + "\n"
    first, last = begins[0], ends[0]
    if last < first:
        raise ValueError(f"'{end}' comes before '{begin}'")
    return "\n".join(lines[:first] + block_lines + lines[last + 1:]).rstrip("\n") + "\n"


def exact_handles(layout: Layout) -> str:
    """The exact-route handles this machine's services justify (ODL-457/472).

    A route exists only when its service does: the first-visit page has a state
    file, the manifest listener has the signed pair. Caddy never proxies to a
    port nothing listens on, and a classic install keeps the Webapp catch-all.
    """
    handles = ""
    if layout.first_visit_state.is_file():
        handles += CADDY_FIRST_VISIT_HANDLE.format(port=layout.first_visit_port)
    if ((layout.etc / "manifest.json").is_file()
            and (layout.etc / "manifest.json.sig").is_file()):
        handles += CADDY_MANIFEST_HANDLE.format(port=MANIFEST_PORT)
    return handles


def render_caddy_site(layout: Layout, slug: str) -> tuple[str, str]:
    begin = f"# BEGIN odile-{slug}"
    end = f"# END odile-{slug}"
    return CADDY_SITE_TEMPLATE.format(
        begin=f"odile-{slug}", end=f"odile-{slug}", fqdn=f"{slug}.odile.fyi",
        user=layout.user, webapp_port=layout.webapp_port, var=layout.var,
        first_visit_handle=exact_handles(layout),
    ), begin


def ensure_caddy_site(layout: Layout, slug: str, dry: bool) -> bool:
    # A classic install has no first-visit page and no signed manifest pair, so
    # it gets no dead routes; a machine that has one gets its exact proxy.
    block = CADDY_SITE_TEMPLATE.format(
        begin=f"odile-{slug}", fqdn=f"{slug}.odile.fyi",
        user=layout.user, webapp_port=layout.webapp_port, var=layout.var,
        first_visit_handle=exact_handles(layout),
    )
    begin, end = f"# BEGIN odile-{slug}", f"# END odile-{slug}"
    existing = layout.caddyfile.read_text(encoding="utf-8") if layout.caddyfile.is_file() else ""
    try:
        updated = splice_marked(existing, block, begin, end)
    except ValueError as exc:
        raise SystemExit(f"error: {layout.caddyfile}: {exc}") from exc
    if updated == existing:
        print(f"same         {layout.caddyfile} ({begin})")
        return False
    print(f"{'would edit' if dry else 'edit':<12} {layout.caddyfile} ({begin})")
    if dry:
        return True
    if layout.mode == "scratch":
        write_atomic(layout.caddyfile, updated.encode("utf-8"), 0o644, *current_ids(layout))
        print("skip         caddy restart (simulation has no public listener)")
        return True
    caddy = shutil.which("caddy")
    if caddy is None:
        raise SystemExit("error: caddy is not installed")
    backup = layout.caddyfile.with_name(f"{layout.caddyfile.name}.bak-{utc_stamp()}")
    if layout.caddyfile.is_file():
        shutil.copy2(layout.caddyfile, backup)
        print(f"backup       {backup}")
    write_atomic(layout.caddyfile, updated.encode("utf-8"), 0o644, 0, 0)
    check = run([caddy, "validate", "--config", str(layout.caddyfile), "--adapter", "caddyfile"])
    if check.returncode != 0:
        print(f"error: caddy validate failed:\n{check.stderr.strip()}", file=sys.stderr)
        if backup.is_file():
            shutil.copy2(backup, layout.caddyfile)
        return False
    print("validate     caddy validate: ok")
    run(["systemctl", "restart", "caddy"])
    for _ in range(20):
        if run(["systemctl", "is-active", "caddy"]).stdout.strip() == "active":
            break
        time.sleep(0.5)
    else:
        print("error: caddy did not become active; rolling back", file=sys.stderr)
        if backup.is_file():
            shutil.copy2(backup, layout.caddyfile)
            run(["systemctl", "restart", "caddy"])
        return False
    print("restart      systemctl restart caddy")
    return True


def wait_for_dns(fqdn: str, seconds: float = 900.0, interval: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            infos = socket.getaddrinfo(fqdn, 443, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            infos = []
        if infos:
            addresses = sorted({str(info[4][0]) for info in infos})
            print(f"dns          {fqdn} -> {', '.join(addresses)}")
            return True
        print(f"dns          {fqdn} does not resolve yet; retrying in {int(interval)} s")
        time.sleep(interval)
    return False


def wait_for_acme(fqdn: str, seconds: float = 900.0, interval: float = 10.0) -> bool:
    """Poll HTTPS until Caddy has a certificate the default trust store accepts."""
    context = ssl.create_default_context()
    deadline = time.monotonic() + seconds
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            with urllib.request.urlopen(f"https://{fqdn}/", timeout=15, context=context) as response:
                print(f"acme         attempt {attempt}: HTTPS {response.status} with a valid certificate")
                return True
        except urllib.error.HTTPError as exc:
            print(f"acme         attempt {attempt}: HTTPS {exc.code} with a valid certificate")
            return True
        except (urllib.error.URLError, ssl.SSLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            print(f"acme         attempt {attempt}: not issued yet ({reason}); retrying in {int(interval)} s")
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------
# installation steps


def ensure_user(layout: Layout, dry: bool, *, sudo_text: str = "NOPASSWD sudo",
                allow_sudo_group: bool = True, comment: str = "Odile agent") -> bool:
    try:
        entry = pwd.getpwnam(layout.user)
        created = False
    except KeyError:
        entry = None
        created = True
    if created:
        print(f"{'would create' if dry else 'create':<12} OS user {layout.user} "
              f"(home {layout.home}, shell /bin/bash, {sudo_text})")
        if dry:
            return True
        if layout.mode == "tenant":
            run(["useradd", "--create-home", "--home-dir", str(layout.home),
                 "--shell", "/bin/bash", "--comment", comment, layout.user], check=True)
        else:
            run(["useradd", "--create-home", "--home-dir", str(layout.home),
                 "--shell", "/bin/bash", "--comment", comment, layout.user], check=True)
        entry = pwd.getpwnam(layout.user)

    assert entry is not None
    if layout.mode == "tenant":
        groups = run(["id", "-nG", layout.user]).stdout.split()
        if not allow_sudo_group and "sudo" in groups:
            raise SystemExit(f"error: {layout.user} is in the sudo group: {' '.join(groups)}")
        if entry.pw_shell != "/bin/bash":
            raise SystemExit(f"error: {layout.user} has shell {entry.pw_shell}, expected /bin/bash")
        if Path(entry.pw_dir) != layout.home:
            raise SystemExit(f"error: {layout.user} home is {entry.pw_dir}, expected {layout.home}")
    home = layout.home
    if home.is_dir():
        mode = home.stat().st_mode & 0o777
        if mode != 0o750:
            print(f"{'would chmod' if dry else 'chmod':<12} {home} 0o{mode:03o} -> 0o750")
            if not dry:
                os.chmod(home, 0o750)
    changed = ensure_dir(layout.hermes_home, 0o700, *user_ids(layout), dry=dry,
                         label=str(layout.hermes_home))
    if created:
        print(f"same         {layout.user}: uid {entry.pw_uid}, groups {' '.join(run(['id', '-nG', layout.user]).stdout.split())}")
    return created or changed


def apt_pins_file(layout: Layout) -> Path:
    """The one apt source the package step reads: the dated Debian snapshot."""
    return layout.etc / "apt-pins.list"


def apt_lists_dir(layout: Layout) -> Path:
    """apt's private list state, so the tenant's own sources stay untouched."""
    return layout.path("/var/cache/odile-apt/lists")


def apt_sources() -> str:
    """The pins file: one `deb` line at the snapshot, valid-until waived."""
    base = f"{OS_SNAPSHOT_BASE}/{OS_SNAPSHOT}"
    return (
        "# Odile's pinned OS packages — written by odile_install.py; do not edit.\n"
        f"# Debian 13 ({OS_SUITE}) as of snapshot {OS_SNAPSHOT}. Every package the\n"
        "# installer installs is `name=version` from this snapshot.\n"
        f"deb [check-valid-until=no] {base} {OS_SUITE} main\n"
    )


def apt_command(layout: Layout, *args: str) -> list[str]:
    """apt-get scoped to the pins file and a private list directory."""
    return [
        "apt-get",
        "-o", f"Dir::Etc::sourcelist={apt_pins_file(layout)}",
        "-o", "Dir::Etc::sourceparts=-",
        "-o", f"Dir::State::lists={apt_lists_dir(layout)}",
        "-o", "APT::Get::List-Cleanup=0",
        *args,
    ]


def apt_package_specs(*, optional: bool = False) -> list[str]:
    """Every `name=version` argument; the optional package only when asked."""
    pins = dict(OS_PACKAGE_PINS)
    if optional:
        pins.update(OS_OPTIONAL_PINS)
    return [f"{name}={version}" for name, version in pins.items()]


def wrapped_terms(terms: list[str], width: int = 74) -> list[str]:
    """Greedy wrap for the pin lists, so `--dry-run` stays readable."""
    lines: list[str] = []
    current = ""
    for term in terms:
        if current and len(current) + 1 + len(term) > width:
            lines.append(current)
            current = term
        else:
            current = f"{current} {term}".strip()
    if current:
        lines.append(current)
    return lines


def installed_version(name: str) -> str | None:
    """The installed dpkg version, or None when dpkg has no installed package."""
    status = run(["dpkg-query", "-W", "-f=${Status}", name])
    if status.returncode != 0 or "install ok installed" not in status.stdout:
        return None
    version = run(["dpkg-query", "-W", "-f=${Version}", name])
    return version.stdout.strip() or None


def ensure_packages(layout: Layout, dry: bool) -> bool:
    if layout.mode == "scratch":
        print("skip         OS packages (simulation uses the host's toolchain)")
        return False
    planned = [(name, pin, installed_version(name)) for name, pin in OS_PACKAGE_PINS.items()]
    needed = [entry for entry in planned if entry[2] != entry[1]]
    optional = [(name, pin, installed_version(name)) for name, pin in OS_OPTIONAL_PINS.items()]
    optional_needed = [entry for entry in optional if entry[2] != entry[1]]
    summary = (f"{len(OS_PACKAGE_PINS)} pinned to Debian 13 {OS_SUITE}, "
               f"snapshot {OS_SNAPSHOT}")
    if not needed and not optional_needed:
        print(f"same         OS packages ({summary})")
        return False
    print(f"{'would install' if dry else 'install':<12} OS packages ({summary})")
    for line in wrapped_terms([f"{name}={pin}" for name, pin, _ in needed]):
        print(f"             {line}")
    for name, pin, have in optional_needed:
        print(f"             {name}={pin} (best effort)")
    for name, pin, have in needed:
        if have:
            print(f"             converging {name} {have} -> {pin}")
    if dry:
        return True
    ensure_dir(apt_lists_dir(layout), 0o755, 0, 0, dry,
               label=str(apt_lists_dir(layout)))
    ensure_file(apt_pins_file(layout), apt_sources().encode("utf-8"), 0o644, 0, 0, dry,
                label=str(apt_pins_file(layout)), detail=f"(snapshot {OS_SNAPSHOT})")
    updated = run(apt_command(layout, "update", "-qq"), timeout=600)
    if updated.returncode != 0:
        raise SystemExit(
            f"error: apt-get update from the pinned snapshot {OS_SNAPSHOT} failed\n"
            f"       {(updated.stderr or updated.stdout).strip()[-1200:]}"
        )
    specs = [f"{name}={pin}" for name, pin, _ in needed]
    if specs:
        # Fail before the transaction when a pin is not in the snapshot: a bare
        # `apt-get install` would quietly take the latest version instead.
        simulate = run(apt_command(layout, "install", "-s", "-y", "-qq",
                                   "--no-install-recommends", "--allow-downgrades", *specs),
                       timeout=600)
        if simulate.returncode != 0:
            raise SystemExit(
                f"error: the pinned OS packages are not in the snapshot {OS_SNAPSHOT}:\n"
                f"       {' '.join(specs)}\n"
                f"       {(simulate.stderr or simulate.stdout).strip()[-1200:]}\n"
                "       refusing to fall back to the latest version"
            )
        installed = run(apt_command(layout, "install", "-y", "-qq",
                                    "--no-install-recommends", "--allow-downgrades", *specs),
                        timeout=1800)
        if installed.returncode != 0:
            raise SystemExit(
                f"error: apt-get install at the pins failed:\n"
                f"       {(installed.stderr or installed.stdout).strip()[-1200:]}"
            )
    for name, pin, have in optional_needed:
        spec = f"{name}={pin}"
        result = run(apt_command(layout, "install", "-y", "-qq",
                                 "--no-install-recommends", "--allow-downgrades", spec),
                     timeout=1800)
        if result.returncode == 0:
            print(f"install      {spec} (best effort)")
        else:
            print(f"skip         {name} (best effort; {spec} is not in the snapshot)")
    return True


def ensure_manifest_group(layout: Layout, dry: bool) -> bool:
    """The group that may traverse /etc/odile: the manifest listener only.

    The directory is 0750 root:odile-manifest, so the listener (a DynamicUser)
    reads /etc/odile/manifest.json* while the tenant uid cannot enter at all.
    install-stack.sh creates the group in the image; a classic install gets it
    from here, before ensure_dirs needs the gid.
    """
    if layout.mode == "scratch":
        print("skip         manifest group (the simulation owns its own /etc/odile)")
        return False
    try:
        grp.getgrnam(MANIFEST_GROUP)
        print(f"same         group {MANIFEST_GROUP}")
        return False
    except KeyError:
        pass
    print(f"{'would create' if dry else 'create':<12} system group {MANIFEST_GROUP}")
    if not dry:
        run(["groupadd", "--system", MANIFEST_GROUP], check=True)
    return True


def first_visit_state(token: str, created_at: str | None = None) -> bytes:
    """The state body the first-visit service loads: a token hash, nothing else.

    The token is minted for the user (ODL-455: the setup token the ready page
    shows), so the guest can hold only its SHA-256. `used_at` is null until the
    POST; a re-run never rewrites this file, so a spent page stays spent.
    """
    if not TOKEN_RE.match(token):
        raise SystemExit("error: the first-visit token is not 64 lowercase hex")
    state = {
        "version": 1,
        "token_sha256": sha256(token.encode("ascii")),
        "created_at": created_at or utc_now(),
        "used_at": None,
        "signin_set_at": None,
        "recovery_hash": None,
        "recovery_issued_at": None,
        "recovery_used_at": None,
    }
    return (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")


def ensure_first_visit(layout: Layout, token: str, dry: bool) -> bool:
    """Write the service, its page and — once — the token hash.

    The page is the reviewed `web/first-visit.html` and the program is the
    reviewed `installer/first_visit.py`, both carried inside this signed file
    and pinned; --self-test fails if either copy drifts. On a re-run the state
    file is left exactly as it is: a machine whose sign-in is already set must
    not reopen the page.
    """
    changed = False
    source = FIRST_VISIT_SOURCE.encode("utf-8")
    digest = sha256(source)
    if digest != FIRST_VISIT_SHA256:
        raise SystemExit(
            "error: the embedded first-visit service does not match its pinned hash "
            f"(got {digest[:12]}, expected {FIRST_VISIT_SHA256[:12]})"
        )
    changed |= ensure_file(layout.first_visit_source, source, 0o755,
                           *current_ids(layout), dry, detail=f"(sha256 {digest[:12]}\u2026)")
    page = FIRST_VISIT_PAGE.encode("utf-8")
    page_digest = sha256(page)
    if page_digest != FIRST_VISIT_PAGE_SHA256:
        raise SystemExit(
            "error: the embedded first-visit page does not match its pinned hash "
            f"(got {page_digest[:12]}, expected {FIRST_VISIT_PAGE_SHA256[:12]})"
        )
    changed |= ensure_file(layout.first_visit_page, page, 0o644,
                           *current_ids(layout), dry,
                           detail=f"(sha256 {page_digest[:12]}\u2026)")
    if layout.first_visit_state.is_file():
        try:
            current = json.loads(layout.first_visit_state.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"error: {layout.first_visit_state}: {exc}") from exc
        if not isinstance(current, dict) or not TOKEN_RE.match(
                str(current.get("token_sha256", ""))):
            raise SystemExit(f"error: {layout.first_visit_state} is not a first-visit state")
        print(f"same         {layout.first_visit_state} "
              f"(token {str(current['token_sha256'])[:8]}\u2026, "
              f"{'used' if current.get('used_at') else 'unspent'})")
        return changed
    if not token:
        raise SystemExit(
            "error: --first-visit needs a one-time token: pass --first-visit-token, "
            "--token, or a recovery kit that carries setup_token"
        )
    changed |= ensure_file(layout.first_visit_state, first_visit_state(token), 0o600,
                           *current_ids(layout), dry,
                           detail="(token sha256 only; no plaintext)")
    return changed


def ensure_dirs(layout: Layout, dry: bool) -> bool:
    changed = False
    if layout.mode == "tenant":
        # /etc/odile is 0750 root:odile-manifest: the manifest listener can
        # traverse it to read its signed pair, and the tenant uid cannot enter.
        # key.env and seed.json inside stay 0600 root, so the group grants
        # traversal, not the key.
        changed |= ensure_dir(layout.etc, 0o750, 0, group_id(MANIFEST_GROUP),
                              dry=dry, label=f"{layout.etc} (root, manifest-readable)")
        # Caddy must traverse this directory to read the one file the
        # /attestation route serves. 0755 is traverse-and-list, not a promise
        # that every file is public: the route rewrites /attestation to
        # attestation.json, and nothing else.
        changed |= ensure_dir(layout.var, 0o755, 0, 0, dry=dry,
                              label=f"{layout.var} (Caddy-readable)")
        changed |= ensure_dir(layout.lib_dir, 0o755, 0, 0, dry=dry,
                              label=str(layout.lib_dir))
    else:
        changed |= ensure_dir(layout.etc, 0o700, *current_ids(layout), dry=dry, label=str(layout.etc))
        changed |= ensure_dir(layout.var, 0o755, *current_ids(layout), dry=dry, label=str(layout.var))
        changed |= ensure_dir(layout.lib_dir, 0o755, *current_ids(layout), dry=dry,
                              label=str(layout.lib_dir))
    changed |= ensure_dir(layout.workspace, 0o755, *user_ids(layout), dry=dry,
                          label=str(layout.workspace))
    changed |= ensure_dir(layout.skills, 0o700, *user_ids(layout), dry=dry, label=str(layout.skills))
    changed |= ensure_dir(layout.scripts, 0o700, *user_ids(layout), dry=dry, label=str(layout.scripts))
    return changed


def ensure_key_env(layout: Layout, seed: dict, dry: bool) -> bool:
    """Write the seed's per-user NEAR key where the agent unit reads it.

    /etc/odile/key.env is 0600 root:root and odile-agent.service carries
    `EnvironmentFile=` for it, so the key is in the gateway's process
    environment at exec and nowhere in the profile .env. One key per machine:
    the seed carries it, the same way it carries the slug.
    """
    key = seed["near_api_key"].strip()
    data = f"{NEAR_KEY_ENV}={key}\n"
    owner = (0, 0) if layout.mode == "tenant" else current_ids(layout)
    return ensure_file(layout.key_env, data.encode("utf-8"), 0o600, *owner, dry,
                       detail="(the only inference credential; root-only)")


def ensure_installer_copy(layout: Layout, dry: bool) -> bool:
    """Place this program's exact bytes where the manifest and CLAIMS name them.

    `--manifest`'s ``installer_sha256`` and `docs/CLAIMS.md` §4.6 both name
    ``/usr/local/bin/odile-install.sh``; before ODL-332's follow-up the path
    only existed when a person put it there. The bytes come from
    ``INSTALLER_PATH``, never from an embedded copy, so the file on the machine
    is exactly what ran and the two hashes agree by construction. The write is
    atomic (`write_atomic`) and a converged machine reads ``same``; a copy left
    at another mode is repaired, because 0755 root is the contract.

    Called after the signature check and before the boot record is written, so
    every record that carries ``installer_sha256`` names bytes that are in
    place.
    """
    data = INSTALLER_PATH.read_bytes()
    target = layout.installer_copy
    digest = sha256(data)
    changed = ensure_file(target, data, 0o755, *current_ids(layout), dry,
                          detail=f"(this program, sha256 {digest[:12]}…)")
    if target.is_file():
        mode = target.stat().st_mode & 0o777
        if mode != 0o755:
            print(f"{'would chmod' if dry else 'chmod':<12} {target} 0o{mode:03o} -> 0o755")
            if not dry:
                os.chmod(target, 0o755)
            changed = True
    return changed


def verify_install_script(data: bytes, source: str = INSTALLER_URL) -> str:
    """The fetched install script's sha256, or a refusal before anything runs.

    ``install.sh`` is the only third-party program the installer executes, and
    it runs before the pinned Hermes commit applies. Without this check a
    changed script could do anything as the tenant and then check out the
    reviewed tree, so the hash is the gate, not the commit. A mismatch raises
    before the temporary file is written or ``bash`` starts.
    """
    digest = sha256(data)
    if digest != HERMES_INSTALL_SHA256:
        raise SystemExit(
            "error: the fetched Hermes install script does not match its pinned hash\n"
            f"       fetched  {digest} ({len(data)} bytes)\n"
            f"       pinned   {HERMES_INSTALL_SHA256}\n"
            f"       {source} changed, or the response was rewritten in transit.\n"
            "       refusing to execute it; nothing was installed"
        )
    return digest


def ensure_attest_source(layout: Layout, dry: bool) -> bool:
    """Write the boot-report transformer the oneshot runs (ODL-397)."""
    data = ATTEST_SOURCE.encode("utf-8")
    digest = sha256(data)
    if digest != ATTEST_SHA256:
        raise SystemExit(
            f"error: the embedded boot-report transformer does not match its pinned hash "
            f"(got {digest[:12]}, expected {ATTEST_SHA256[:12]})"
        )
    return ensure_file(layout.attest_source, data, 0o755, *current_ids(layout), dry,
                       detail=f"(sha256 {digest[:12]}…)")


def ensure_scratch_attest(layout: Layout, dry: bool) -> bool:
    """Run the transformer once in the simulation, the way the oneshot will.

    The simulation has no /run/blackbox/attest.json, so the served document
    says `available: false` with the reason; the point is that the scratch root
    carries the same file the Caddy route points at. Compare before writing, so
    a converged re-run prints `same` and changes nothing.
    """
    target = layout.served_attestation
    before = target.read_bytes() if target.is_file() else None
    probe = target.with_name(target.name + ".probe")
    result = run([sys.executable, str(layout.attest_source), "--boot-report",
                  str(layout.boot_report), str(probe)], timeout=60)
    produced = probe.read_bytes() if probe.is_file() else None
    if produced is not None:
        probe.unlink()
    if result.returncode != 0:
        raise SystemExit(f"error: the boot-report transformer failed: {result.stderr.strip()}")
    if produced == before:
        print(f"same         {target} (boot report)")
        return False
    print(f"{'would write' if dry else 'write':<12} {target} (boot report)")
    if dry:
        return True
    if produced is None:
        raise SystemExit("error: the boot-report transformer wrote no document")
    write_atomic(target, produced, 0o644, *current_ids(layout))
    return True


def ensure_install(layout: Layout, dry: bool) -> bool:
    if layout.launcher.is_file() and (layout.install_dir / ".git").exists():
        version = as_user(layout, [str(layout.launcher), "--version"]).stdout.strip().splitlines()
        first = version[0] if version else "(no version line)"
        print(f"same         Hermes install {layout.install_dir}")
        print(f"             {first}")
        if not first.startswith(layout.expected_version_prefix):
            print(
                f"warning: expected a {layout.expected_version_prefix} install; the Webapp gate "
                "and config schema were reviewed against it",
                file=sys.stderr,
            )
        return False

    pin = f"pinned {layout.hermes_commit[:12]}" if layout.hermes_commit else "stable"
    print(f"{'would install' if dry else 'install':<12} Hermes as {layout.user} into "
          f"{layout.hermes_home} ({pin}, browser/cua skipped)")
    if dry:
        return True

    with urllib.request.urlopen(layout.installer_url, timeout=60) as response:
        data = response.read()
    digest = verify_install_script(data, layout.installer_url)
    print(f"             install.sh {len(data)} bytes, sha256 {digest[:12]}… matches the pin")

    fd, script = tempfile.mkstemp(prefix="hermes-install.", suffix=".sh")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(script, 0o644)
        result = as_user(layout, ["bash", script, *layout.installer_args],
                         cwd=layout.home, timeout=3600)
        if result.returncode != 0:
            tail = ""
            log = layout.hermes_home / "logs/install.log"
            if log.is_file():
                tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])
            raise SystemExit(
                f"error: Hermes install failed ({result.returncode})\n"
                f"{result.stderr.strip()[-2000:]}\n{tail}"
            )
        print(result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "installed")
    finally:
        try:
            os.unlink(script)
        except FileNotFoundError:
            pass
    if not layout.launcher.is_file():
        raise SystemExit(f"error: install finished without {layout.launcher}")
    return True


def ensure_webapp_build(layout: Layout, dry: bool) -> bool:
    if layout.webapp_bundle.is_file():
        size = sum(p.stat().st_size for p in layout.webapp_bundle.parent.rglob("*") if p.is_file())
        print(f"same         Webapp bundle {layout.webapp_bundle.parent} ({size // 1_000_000} MB)")
        return False
    heap = webapp_heap_mb()
    print(f"{'would build' if dry else 'build':<12} Webapp bundle "
          f"(first run only; minutes; V8 heap cap {heap} MB)")
    if dry:
        return True
    result = as_user(layout, [str(layout.launcher), "webapp", "--build-only"],
                     cwd=layout.home, timeout=3600,
                     extra_env={"NODE_OPTIONS": f"--max-old-space-size={heap}"})
    if result.returncode != 0 or not layout.webapp_bundle.is_file():
        raise SystemExit(
            f"error: webapp build failed ({result.returncode})\n"
            f"{result.stdout.strip()[-1500:]}\n{result.stderr.strip()[-1500:]}"
        )
    print("built        Webapp bundle")
    return True


# --------------------------------------------------------------------------
# the Webapp brand (ODL-240)
#
# The skin (SKIN_YAML) themes the palette. The title, the wordmark, the
# composer prompt and one credit line live inside the vendor bundle and cannot
# be reached from a skin, so the installer links one same-origin script from the
# built shell — the way ODL-330 does for the demo. No bundle file is forked: a
# Webapp rebuild drops the tag, and the next installer run puts it back.

BRAND_ASSET = "odile-brand.js"
BRAND_COMMENT = "<!-- Odile brand (ODL-240): the names a skin cannot reach. -->"


def brand_version(script: bytes) -> str:
    return sha256(script)[:12]


def brand_tag_lines(version: str) -> tuple[str, str]:
    return (
        BRAND_COMMENT,
        # Not `defer`: the theme-font guard and the first-visit skin key must
        # run before the vendor's module graph. A classic parse-blocking 8 KB
        # same-origin script does that; defer would let a skin inject the font
        # link, and let the bundle read an empty appearance, first.
        f'<script src="./{BRAND_ASSET}?v={version}"></script>',
    )


def strip_brand(source: str) -> str:
    """`source` with our injected block removed, i.e. the vendor's bytes."""
    kept: list[str] = []
    skipping = False
    for line in source.splitlines(keepends=True):
        if BRAND_COMMENT in line:
            skipping = True
            continue
        if skipping and BRAND_ASSET in line:
            continue
        skipping = False
        kept.append(line)
    return "".join(kept)


def with_brand(source: str, version: str) -> str:
    """`source` with the brand script linked exactly once, before the shell's
    own pre-paint script so the first-visit paper keys are readable there."""
    base = strip_brand(source)
    opening = base.find("<head>")
    if opening < 0:
        raise ValueError("the Webapp shell has no <head>")
    insert_at = opening + len("<head>")
    newline = base.find("\n", insert_at)
    if newline < 0:
        raise ValueError("the Webapp shell's <head> has no newline")
    next_line = base[newline + 1:]
    indent = next_line[: len(next_line) - len(next_line.lstrip(" \t"))]
    block = "".join(f"{indent}{line}\n" for line in brand_tag_lines(version))
    return base[: newline + 1] + block + base[newline + 1:]


def ensure_webapp_brand(layout: Layout, dry: bool) -> bool:
    """Install the brand script and link it from the built shell."""
    if not layout.webapp_bundle.is_file():
        print(f"{'would patch' if dry else 'patch':<12} Webapp brand (bundle not built yet)")
        if dry:
            return True
        raise SystemExit(
            f"error: cannot patch the Webapp brand: {layout.webapp_bundle} is missing")
    target = layout.webapp_bundle.parent / BRAND_ASSET
    data = BRAND_JS.encode("utf-8")
    version = brand_version(data)
    source_html = layout.webapp_bundle.read_text(encoding="utf-8")
    desired_html = with_brand(source_html, version)
    asset_changed = not target.is_file() or target.read_bytes() != data
    html_changed = desired_html != source_html
    if not html_changed and not asset_changed:
        print(f"same         Webapp brand ({BRAND_ASSET} v{version}, index.html linked)")
        return False
    print(f"{'would write' if dry else 'write':<12} Webapp brand ({BRAND_ASSET} v{version}, "
          f"index.html {'linked' if html_changed else 'unchanged'})")
    if not dry:
        uid, gid = user_ids(layout)
        write_atomic(target, data, 0o644, uid, gid)
        if html_changed:
            write_atomic(layout.webapp_bundle, desired_html.encode("utf-8"), 0o644, uid, gid)
    return True


def render_tenant_config(layout: Layout, seed: dict) -> str:
    model = seed.get("model") or "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    body = TENANT_CONFIG_TEMPLATE.format(
        model=model, webapp_port=layout.webapp_port,
        public_url=layout.webapp_public_url,
    )
    version = config_version([
        layout.hermes_home / "config.yaml",
        Path("/root/.hermes/config.yaml"),
    ])
    return f"_config_version: {version}\n{body}"


def ensure_config(layout: Layout, seed: dict, dry: bool) -> bool:
    desired = render_tenant_config(layout, seed)
    target = layout.hermes_home / "config.yaml"
    current = target.read_text(encoding="utf-8") if target.is_file() else None
    merged = merge_config(current, desired) if current is not None else desired
    if merged is None or merged == current:
        print(f"same         {target} ({len(MANAGED_KEYS)} managed blocks, "
              f"sha256 {sha256(desired.encode())[:12]}…)")
        return False
    print(f"{'would write' if dry else 'write':<12} {target} "
          f"({len(MANAGED_KEYS)} managed blocks; keys Hermes added are kept)")
    if not dry:
        write_atomic(target, merged.encode("utf-8"), 0o644, *user_ids(layout))
    return True


SKIN_NAME = "odile"
SKIN_BLOCK = ["display:", f"  skin: {SKIN_NAME}"]


def merge_display_skin(text: str) -> str | None:
    """None when display.skin already names the Odile skin, else the merged config.

    Only the `skin` key is managed here; every other key in a display block
    (`interface`, `theme`, anything Hermes adds) is kept, the same contract the
    four managed blocks have.
    """
    preamble, blocks = split_blocks(text)
    current = blocks.get("display", [])
    if block_contains(SKIN_BLOCK, current):
        return None
    if "display" in blocks:
        blocks["display"] = merge_block(SKIN_BLOCK, current)
        lines: list[str] = list(preamble)
        for block in blocks.values():
            lines.extend(block)
        return "\n".join(lines).rstrip("\n") + "\n"
    return text.rstrip("\n") + f"\n\ndisplay:\n  skin: {SKIN_NAME}\n"


def ensure_skin(layout: Layout, dry: bool) -> bool:
    """Write the skin file and point display.skin at it.

    Idempotent by content: a converged home prints `same` and writes nothing.
    The config merge keeps every other display key, so an owner's `interface`
    or a later Hermes setting survives a re-run.
    """
    changed = False
    uid, gid = user_ids(layout)
    skins_dir = layout.hermes_home / "skins"
    target = skins_dir / f"{SKIN_NAME}.yaml"
    data = SKIN_YAML.encode("utf-8")
    if not target.is_file() or target.read_bytes() != data:
        print(f"{'would write' if dry else 'write':<12} {target} (skin {SKIN_NAME}, mode 0644)")
        if not dry:
            ensure_dir(skins_dir, 0o700, uid, gid)
            write_atomic(target, data, 0o644, uid, gid)
        changed = True
    else:
        print(f"same         {target} (skin {SKIN_NAME})")
    config = layout.hermes_home / "config.yaml"
    current = config.read_text(encoding="utf-8") if config.is_file() else ""
    merged = merge_display_skin(current) if current else f"display:\n  skin: {SKIN_NAME}\n"
    if merged is None:
        print(f"same         {config} (display.skin {SKIN_NAME})")
        return changed
    print(f"{'would write' if dry else 'write':<12} {config} "
          f"(display.skin {SKIN_NAME}; other display keys kept)")
    if not dry:
        write_atomic(config, merged.encode("utf-8"), 0o644, uid, gid)
    return True


def ensure_env(layout: Layout, password: str | None, username: str,
               dry: bool, first_visit: bool = False) -> tuple[bool, str | None]:
    """Write the .env; return (changed, the plaintext password when known).

    The plaintext is only needed for this run's sign-in smoke. On a re-run with
    an existing hash and no --signin-password it is unknown, so the smoke
    re-checks the gate only when the caller supplied it.

    Under ``--first-visit`` the hash is a placeholder for a discarded random
    password: nobody can sign in until the first-visit page writes the real
    one, which is the strict bar (the installer never knows the sign-in).
    """
    target = layout.hermes_home / ".env"
    if target.is_file():
        text = target.read_text(encoding="utf-8")
        outside, seat = count_env_violations(text)
        keys = env_keys(text)
        values = env_values(text)
        existing_hash = values.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH", "")
        if (password and existing_hash and not first_visit
                and not verify_password(password, existing_hash)):
            raise SystemExit(
                "error: --signin-password does not match the existing hash in "
                f"{target}; omit it to keep the current password, or reset the file"
            )
        if sorted(keys) == sorted(TENANT_ENV_KEYS) and outside == 0 and seat == 0:
            mode = target.stat().st_mode & 0o777
            changed = False
            if mode != 0o600:
                print(f"{'would chmod' if dry else 'chmod':<12} {target} 0o{mode:03o} -> 0o600")
                if not dry:
                    os.chmod(target, 0o600)
                changed = True
            print(f"same         {target} (mode 0600, 3 allowlisted keys, allowlist ok)")
            return changed, password
        print(f"{'would rewrite' if dry else 'rewrite':<12} {target} "
              f"(outside the allowlist: {outside}; seat secrets: {seat})")
    else:
        values = {}
        print(f"{'would write' if dry else 'write':<12} {target} (3 allowlisted keys, mode 0600)")

    if dry:
        return True, password
    password_hash = values.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH") or ""
    if not password_hash:
        if first_visit:
            # A real scrypt hash of a password nobody knows: the gate is shut,
            # and the first-visit page is the only way to open it. The plaintext
            # is generated and discarded in one expression, never printed.
            password = None
            password_hash = hash_password(generate_password())
            print("write        the sign-in hash is a placeholder until first visit")
        else:
            password = resolve_signin_password(password, interactive=True)
            password_hash = hash_password(password)
    secret = values.get("HERMES_DASHBOARD_BASIC_AUTH_SECRET") or base64.b64encode(
        secrets.token_bytes(32)
    ).decode()
    username = values.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME") or username
    desired = render_env(username, password_hash, secret)
    outside, seat = count_env_violations(desired)
    if outside or seat:
        raise SystemExit(f"error: rendered .env violates the allowlist (outside={outside}, seat={seat})")
    write_atomic(target, desired.encode("utf-8"), 0o600, *user_ids(layout))
    return True, password


def ensure_verify_surface(layout: Layout, dry: bool, boot_key_b64: str = "") -> bool:
    """Write the /verify page, the two programs that write and read its report,
    and the one authorized key the report expects (ODL-473).

    The page is world-readable; the reporter and its wrapper are root-owned
    executables. Each embedded copy is checked against its pin before it is
    written, so a stale embed stops the install instead of shipping a page
    that does not match the program that feeds it.

    ``boot_key_b64`` is bb.key= from the boot report's measured command line.
    The strict bar's report passes only when every authorized_keys line is that
    key and no private half of it is on the machine; the installer therefore
    writes exactly that one line, 0600, to root's authorized keys. A scratch
    run with no boot report writes none, and the unit's own report says so.
    """
    changed = False
    if boot_key_b64:
        try:
            line = base64.b64decode(boot_key_b64, validate=True).decode("utf-8").strip()
        except (ValueError, UnicodeDecodeError):
            line = ""
        if line and "PRIVATE KEY" not in line and "\n" not in line:
            changed |= ensure_dir(layout.path("/root/.ssh"), 0o700, *current_ids(layout),
                                  dry=dry, label="/root/.ssh")
            changed |= ensure_file(layout.path("/root/.ssh/authorized_keys"),
                                   (line + "\n").encode("utf-8"), 0o600,
                                   *current_ids(layout), dry,
                                   detail="(the throwaway boot key, public half only)")
    for target, data, expected, mode, detail in (
        (layout.verify_page, VERIFY_PAGE.encode("utf-8"), VERIFY_PAGE_SHA256,
         0o644, "(the /verify page)"),
        (layout.access_report_source, ACCESS_REPORT_SCRIPT.encode("utf-8"),
         ACCESS_REPORT_SCRIPT_SHA256, 0o755, "(the boot report wrapper)"),
        (layout.verify_unlock_source, VERIFY_UNLOCK.encode("utf-8"),
         VERIFY_UNLOCK_SHA256, 0o755, "(the access reporter)"),
        (layout.audit_source, AUDIT_SCRIPT.encode("utf-8"),
         AUDIT_SCRIPT_SHA256, 0o755, "(the handoff audit)"),
    ):
        digest = sha256(data)
        if digest != expected:
            raise SystemExit(
                f"error: {target} does not match its pinned hash "
                f"(got {digest[:12]}, expected {expected[:12]})"
            )
        changed |= ensure_file(target, data, mode, *current_ids(layout), dry,
                               detail=f"{detail} sha256 {digest[:12]}…")
    return changed


def ensure_pin_files(layout: Layout, seed: dict, dry: bool) -> bool:
    """The seed's public material: support key, backup repo, Hermes version."""
    support = (seed.get("support_public_key") or "").strip() + "\n"
    changed = ensure_file(layout.etc / "support-key.pub", support.encode("utf-8"), 0o644,
                          *current_ids(layout), dry, detail="(support public key)")
    backup = (seed.get("backup_repo_url") or "").strip() + "\n"
    changed |= ensure_file(layout.etc / "backup-repo.txt", backup.encode("utf-8"), 0o644,
                           *current_ids(layout), dry, detail="(backup repo URL)")
    return changed


def ensure_backup_source(layout: Layout, dry: bool) -> bool:
    """Write the tenant backup program the nightly unit runs (ODL-215 follow-up).

    The bytes are the embedded, pinned copy of `scripts/backup.py`; the hash is
    checked here as well as in `--self-test`, so a swapped payload cannot be
    installed. 0755 root is the contract the unit's `Documentation=` and the
    runbook name.
    """
    data = BACKUP_SOURCE.encode("utf-8")
    digest = sha256(data)
    if digest != BACKUP_SHA256:
        raise SystemExit(
            f"error: the embedded backup program does not match its pinned hash "
            f"(got {digest[:12]}, expected {BACKUP_SHA256[:12]})"
        )
    return ensure_file(layout.backup_source, data, 0o755, *current_ids(layout), dry,
                       detail=f"(sha256 {digest[:12]}…)")


def backup_unit_files(layout: Layout, slug: str, heartbeat_url: str) -> dict[Path, str]:
    """The two unit files as `scripts/backup.py install` renders them.

    One renderer, used by the installer and by the tests; the template is the
    program's own, so the installer cannot invent a unit the program would not
    write.
    """
    service = BACKUP_SERVICE_TEMPLATE.format(
        script=layout.backup_source,
        repo_file=layout.backup_repo_file,
        repo_arg="",
        recovery=layout.recovery,
        home=layout.home,
        key=layout.backup_key_dir / "id_ed25519",
        known_hosts=layout.backup_key_dir / "known_hosts",
        status=layout.backup_status,
        slug=slug,
        url=heartbeat_url,
        heartbeat_dir=layout.backup_heartbeat_dir,
    )
    return {layout.backup_service: service, layout.backup_timer: BACKUP_TIMER_TEMPLATE}


def ensure_backup_units(layout: Layout, slug: str, dry: bool,
                        heartbeat_url: str = HEARTBEAT_URL) -> bool:
    """Write the nightly backup unit and its timer, root-owned 0644."""
    changed = False
    for path, text in backup_unit_files(layout, slug, heartbeat_url).items():
        detail = "(nightly restic, 03:00)" if path == layout.backup_service else "(daily 03:00)"
        changed |= ensure_file(path, text.encode("utf-8"), 0o644, *current_ids(layout),
                               dry, detail=detail)
    return changed


def ensure_backup_timer(layout: Layout, dry: bool) -> bool:
    """Enable and start the timer on a tenant; a simulation stops at the files.

    `systemctl enable --now` is the same sentence `backup.py install` uses. A
    converged machine prints `same`; a timer that was disabled or stopped is
    converged, not reported as a change the caller has to notice.
    """
    unit = "odile-backup-tenant.timer"
    if layout.mode != "tenant":
        print(f"skip         {unit} (the simulation has no systemd)")
        return False
    enabled = run(["systemctl", "is-enabled", unit]).stdout.strip()
    active = run(["systemctl", "is-active", unit]).stdout.strip()
    if enabled == "enabled" and active == "active":
        print(f"same         {unit} enabled and active (daily 03:00)")
        return False
    print(f"{'would enable' if dry else 'enable':<12} {unit} --now (daily 03:00)")
    if not dry:
        run(["systemctl", "enable", "--now", unit], check=True)
        state = run(["systemctl", "is-enabled", unit]).stdout.strip()
        if state != "enabled":
            raise SystemExit(
                f"error: systemctl enable {unit} left it {state!r}; "
                "the unit has no usable [Install] section"
            )
    return True


def ensure_units(layout: Layout, slug: str, dry: bool, first_visit: bool = False,
                 heartbeat_url: str = HEARTBEAT_URL) -> bool:
    agent_unit = AGENT_UNIT_TEMPLATE.format(
        slug=slug, user=layout.user, group=layout.group, home=layout.home,
        hermes_home=layout.hermes_home, node_path=node_bin(layout),
        launcher=layout.launcher, key_env=layout.key_env,
    )
    attest_unit = ATTEST_UNIT_TEMPLATE.format(
        source=layout.attest_source, boot_report=layout.boot_report,
        served=layout.served_attestation, var=layout.var, agent_unit=layout.unit_name,
    )
    changed = ensure_file(layout.unit_target, agent_unit.encode("utf-8"), 0o644,
                           *current_ids(layout), dry, detail=f"(User={layout.user})")
    changed |= ensure_file(layout.unit_dir / "odile-attest-boot.service",
                           attest_unit.encode("utf-8"), 0o644, *current_ids(layout), dry,
                           detail="(boot report)")
    # ODL-215 follow-up: the nightly backup and its timer. Both point at the
    # seed-written repository URL and the installer's own recovery file, never
    # at a copy; the timer is enabled below, after systemd has re-read the unit.
    changed |= ensure_backup_units(layout, slug, dry, heartbeat_url)
    if first_visit:
        first_visit_unit = FIRST_VISIT_UNIT_TEMPLATE.format(
            source=layout.first_visit_source, state=layout.first_visit_state,
            env_file=layout.hermes_home / ".env", page=layout.first_visit_page,
            verify_page=layout.verify_page, report=layout.access_report_file,
            attest=layout.served_attestation,
            recovery=layout.recovery,
            port=layout.first_visit_port, unit=layout.unit_name,
            hermes_home=layout.hermes_home, etc=layout.etc,
        )
        changed |= ensure_file(layout.first_visit_unit, first_visit_unit.encode("utf-8"),
                               0o644, *current_ids(layout), dry,
                               detail="(first visit, loopback only)")
        access_unit = ACCESS_REPORT_UNIT_TEMPLATE.format(
            wrapper=layout.access_report_source, var=layout.var,
            agent_unit=layout.unit_name,
        )
        changed |= ensure_file(layout.access_report_unit, access_unit.encode("utf-8"),
                               0o644, *current_ids(layout), dry,
                               detail="(the machine's own access report)")
    return changed


def ensure_sudoers(layout: Layout, dry: bool) -> bool:
    if layout.mode == "scratch":
        print("skip         sudoers (simulation user has no sudo)")
        return False
    target = Path("/etc/sudoers.d/odile")
    desired = SUDOERS_TEMPLATE.format(user=layout.user).encode("utf-8")
    if target.is_file() and target.read_bytes() == desired:
        print(f"same         {target} (NOPASSWD sudo)")
        return False
    print(f"{'would write' if dry else 'write':<12} {target} (NOPASSWD sudo)")
    if dry:
        return True
    backup = target.with_name(f"{target.name}.bak-{utc_stamp()}") if target.is_file() else None
    if backup is not None:
        shutil.copy2(target, backup)
    write_atomic(target, desired, 0o440, 0, 0)
    check = run(["visudo", "-cf", str(target)])
    if check.returncode != 0:
        if backup is not None:
            shutil.copy2(backup, target)
        raise SystemExit(f"error: sudoers file is invalid:\n{check.stderr.strip()}")
    sudo = run(["sudo", "-u", layout.user, "-H", "sudo", "-n", "true"])
    if sudo.returncode != 0:
        raise SystemExit(f"error: {layout.user} cannot sudo without a password:\n{sudo.stderr.strip()}")
    print(f"verify       sudo -u {layout.user} sudo -n true: ok")
    return True


def ensure_sshd(layout: Layout, dry: bool) -> bool:
    if layout.mode == "scratch":
        print("skip         sshd and root lock (simulation)")
        return False
    target = Path("/etc/ssh/sshd_config.d/99-odile.conf")
    desired = SSHD_TEMPLATE.encode("utf-8")
    changed = False
    current = target.read_bytes() if target.is_file() else None
    if current == desired:
        print(f"same         {target} (PasswordAuthentication no)")
    else:
        print(f"{'would write' if dry else 'write':<12} {target} (PasswordAuthentication no)")
        if not dry:
            backup = target.with_name(f"{target.name}.bak-{utc_stamp()}") if target.is_file() else None
            if backup is not None:
                shutil.copy2(target, backup)
            write_atomic(target, desired, 0o644, 0, 0)
            check = run(["sshd", "-t"])
            if check.returncode != 0:
                if backup is not None:
                    shutil.copy2(backup, target)
                raise SystemExit(f"error: sshd -t failed:\n{check.stderr.strip()}")
            run(["systemctl", "reload", "ssh"])
            print("reload       sshd (PasswordAuthentication no)")
            changed = True
    status = run(["passwd", "-S", "root"]).stdout.split()
    locked = len(status) > 1 and status[1] in {"L", "!"}
    if locked:
        print("same         root password locked")
    else:
        print(f"{'would lock' if dry else 'lock':<12} root password (passwd -l root)")
        if not dry:
            run(["passwd", "-l", "root"], check=True)
            changed = True
    return changed


def ensure_workspace(layout: Layout, slug: str, dry: bool) -> bool:
    changed = ensure_file(
        layout.workspace / "README.md",
        WORKSPACE_README.format(user=layout.user).encode("utf-8"),
        0o644, *user_ids(layout), dry, detail="(workspace welcome)",
    )
    skill = SKILL_TEMPLATE.format(
        user=layout.user, workspace=layout.workspace, near_base_url=NEAR_BASE_URL,
    )
    changed |= ensure_file(layout.skills / "odile-home/SKILL.md", skill.encode("utf-8"),
                           0o644, *user_ids(layout), dry, detail="(odile-home skill)")
    # The standing context and the first-run skill travel inside this signed
    # file. The gateway's WorkingDirectory is $HOME, the one place Hermes
    # reads AGENTS.md from; the skill must exist before the first chat.
    changed |= ensure_tenant_files(layout, dry)
    return changed


# --------------------------------------------------------------------------
# services: systemd on a tenant, tracked processes in the simulation


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    with socket.socket() as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def wait_for_http(url: str, seconds: float = 60.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def wait_for_webapp(layout: Layout, seconds: float = 120.0) -> bool:
    """Wait until the Webapp *answers*, not merely until the port is bound.

    uvicorn binds the port early and returns 503 while the dashboard, plugins
    and renderer are still loading — measured in the simulation: an open port
    eight seconds in, a 302 from the sign-in gate about ten seconds later. The
    gate's first non-5xx response is readiness; the smoke tests run after it.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            status, _, _ = http_call(layout.webapp_host, layout.webapp_port, "GET", "/",
                                     headers={"Host": layout.fqdn or "localhost"}, timeout=3)
            if status < 500:
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def _pid_alive(path: Path) -> bool:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ensure_scratch_services(layout: Layout, dry: bool) -> bool:
    changed = False
    if port_open(layout.webapp_host, layout.webapp_port):
        print(f"same         agent listing on {layout.webapp_host}:{layout.webapp_port}")
    else:
        print(f"{'would start' if dry else 'start':<12} agent gateway on "
              f"{layout.webapp_host}:{layout.webapp_port}")
        if not dry:
            log = layout.var / "agent.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "ab") as handle:
                process = subprocess.Popen(
                    [str(layout.launcher), "gateway", "run"],
                    stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
                    env=gateway_env(layout), cwd=str(layout.home),
                )
            layout.agent_pid.write_text(f"{process.pid}\n", encoding="utf-8")
            if not wait_for_webapp(layout, 180):
                raise SystemExit(f"error: the Webapp did not listen; see {log}")
            print(f"listening    agent {layout.webapp_host}:{layout.webapp_port}")
            changed = True
    if layout.first_visit_state.is_file():
        if port_open("127.0.0.1", layout.first_visit_port):
            print(f"same         first visit listening on 127.0.0.1:{layout.first_visit_port}")
        else:
            print(f"{'would start' if dry else 'start':<12} first visit on "
                  f"127.0.0.1:{layout.first_visit_port}")
            if not dry:
                log = layout.var / "first-visit.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                with open(log, "ab") as handle:
                    process = subprocess.Popen(
                        [sys.executable, str(layout.first_visit_source),
                         "--bind", "127.0.0.1", "--port", str(layout.first_visit_port),
                         "--state", str(layout.first_visit_state),
                         "--env-file", str(layout.hermes_home / ".env"),
                         "--page", str(layout.first_visit_page),
                         "--recovery-file", str(layout.recovery),
                         # The simulation has no systemd; the tenant unit passes
                         # `systemctl restart odile-agent` instead. Say so, so a
                         # simulation run does not look like a live reload.
                         "--reload-command", ""],
                        stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
                        env=os.environ.copy(),
                    )
                (layout.var / "first-visit.pid").write_text(f"{process.pid}\n",
                                                            encoding="utf-8")
                if not wait_for_http(
                        f"http://127.0.0.1:{layout.first_visit_port}/health", 60):
                    raise SystemExit(f"error: the first-visit service did not start; see {log}")
                print(f"listening    first visit 127.0.0.1:{layout.first_visit_port} "
                      "(reload: the unit restarts the agent; the simulation does not)")
                changed = True
    return changed


def ensure_one_service(layout: Layout, unit: str, dry: bool, unit_changed: bool,
                       *, wait: bool = True) -> bool:
    """Enable and start/restart one unit; wait for the Webapp when asked."""
    changed = False
    enabled = run(["systemctl", "is-enabled", unit]).stdout.strip()
    active = run(["systemctl", "is-active", unit]).stdout.strip()
    if enabled != "enabled":
        print(f"{'would enable' if dry else 'enable':<12} {unit} (boot)")
        if not dry:
            run(["systemctl", "enable", unit], check=True)
            # `systemctl enable` exits 0 on a unit with no [Install] section
            # ("The unit files have no installation config…") and creates no
            # symlink, so trust the re-read, not the exit code. ODL-391 round 1.
            state = run(["systemctl", "is-enabled", unit]).stdout.strip()
            if state != "enabled":
                raise SystemExit(
                    f"error: systemctl enable {unit} left it {state!r}; "
                    "the unit has no usable [Install] section"
                )
        changed = True
    else:
        print(f"same         {unit} enabled at boot")
    if active != "active":
        print(f"{'would start' if dry else 'start':<12} {unit}")
        if not dry:
            run(["systemctl", "start", unit], check=True)
        changed = True
    elif unit_changed:
        print(f"{'would restart' if dry else 'restart':<12} {unit} (changed)")
        if not dry:
            run(["systemctl", "restart", unit], check=True)
        changed = True
    else:
        print(f"same         {unit} active")
    if wait and not dry:
        if not wait_for_webapp(layout):
            status = run(["systemctl", "status", unit, "--no-pager", "-l"]).stdout
            raise SystemExit(
                f"error: nothing listening on {layout.webapp_host}:{layout.webapp_port}\n"
                f"{status[-3000:]}"
            )
        print(f"listening    {layout.webapp_host}:{layout.webapp_port}")
    return changed


def ensure_systemd_services(layout: Layout, dry: bool, units_changed: bool) -> bool:
    changed = False
    if units_changed:
        print(f"{'would reread' if dry else 'daemon-reload':<12} systemd units")
        if not dry:
            run(["systemctl", "daemon-reload"], check=True)
        changed = True
    # The boot-report oneshot runs before the Webapp so /verify never reads a
    # missing document; RemainAfterExit keeps a converged re-run `same`.
    changed |= ensure_one_service(layout, "odile-attest-boot", dry, units_changed, wait=False)
    if layout.first_visit_unit.is_file():
        changed |= ensure_one_service(layout, "odile-first-visit", dry, units_changed, wait=False)
        if not dry:
            if not wait_for_http(f"http://127.0.0.1:{layout.first_visit_port}/health", 60):
                raise SystemExit("error: the first-visit service is not answering on "
                                 f"127.0.0.1:{layout.first_visit_port}")
            print(f"listening    first visit 127.0.0.1:{layout.first_visit_port}")
    if layout.access_report_unit.is_file():
        # The report the /verify page reads. It runs before the agent unit and
        # again after the first visit; here it runs once so the page has it.
        changed |= ensure_one_service(layout, "odile-access-report", dry, units_changed,
                                      wait=False)
    return ensure_one_service(layout, layout.unit_name, dry, units_changed, wait=True)


# --------------------------------------------------------------------------
# smoke tests: the sign-in gate, one chat turn straight to NEAR AI, one cron job


def http_call(host: str, port: int, method: str, path: str, *,
              headers: dict[str, str] | None = None, body: bytes | None = None,
              timeout: float = 15.0) -> tuple[int, list[tuple[str, str]], bytes]:
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        return response.status, list(response.getheaders()), data
    finally:
        connection.close()


def gate_ok(anonymous_html: int, anonymous_api: int, authenticated: int) -> bool:
    """The measured sign-in gate, the same shape as deploy_demo_site.gate_ok.

    An anonymous HTML request is a login redirect (302), an anonymous API
    request is 401, and a valid session is 200. A 200 on the anonymous HTML
    route means the gate is off.
    """
    return (anonymous_html, anonymous_api, authenticated) == (302, 401, 200)


def login_table(layout: Layout, username: str, password: str) -> list[tuple[str, bool, str]]:
    """The sign-in gate: 302/401 without, 200 with, and the SPA behind it."""
    host = layout.webapp_host
    port = layout.webapp_port
    netloc = urllib.parse.urlsplit(layout.webapp_public_url).netloc
    origin = layout.webapp_public_url
    base = {"Host": netloc, "Origin": origin}
    results: list[tuple[str, bool, str]] = []

    status, _, _ = http_call(host, port, "GET", "/", headers=base)
    html_status = status
    results.append(("GET / without a session -> 302 to /login", status == 302, str(status)))
    status, _, _ = http_call(host, port, "GET", "/api/sessions", headers=base)
    api_status = status
    results.append(("GET /api/sessions without a session -> 401", status == 401, str(status)))
    body = json.dumps({"provider": "basic", "username": username, "password": "definitely-wrong"}).encode()
    status, _, _ = http_call(host, port, "POST", "/auth/password-login", headers={
        **base, "Content-Type": "application/json", "Content-Length": str(len(body)),
    }, body=body)
    results.append(("POST /auth/password-login, wrong password -> 401", status == 401, str(status)))
    body = json.dumps({"provider": "basic", "username": username, "password": password}).encode()
    status, headers, _ = http_call(host, port, "POST", "/auth/password-login", headers={
        **base, "Content-Type": "application/json", "Content-Length": str(len(body)),
    }, body=body)
    login_status = status
    results.append(("POST /auth/password-login, the Odile password -> 200", status == 200, str(status)))
    cookies = []
    for name, value in headers:
        if name.lower() == "set-cookie":
            pair = value.split(";", 1)[0]
            if "=" in pair:
                cookies.append(pair)
    cookie = "; ".join(cookies)
    status, _, page = http_call(host, port, "GET", "/", headers={**base, "Cookie": cookie})
    shell = b'<title>Hermes</title>' in page and b'id="root"' in page
    session_status = status
    results.append(("GET / with the session cookie -> 200 Webapp shell",
                    status == 200 and shell, f"{status} shell={shell}"))
    status, _, _ = http_call(host, port, "GET", "/api/sessions", headers={**base, "Cookie": cookie})
    results.append(("GET /api/sessions with the session cookie -> 200", status == 200, str(status)))
    results.append(("the gate is 302/401/200 (deploy_demo_site.gate_ok shape)",
                    gate_ok(html_status, api_status, session_status if login_status == 200 else 0),
                    f"{html_status}/{api_status}/{session_status}"))
    return results


def print_table(results: list[tuple[str, bool, str]]) -> bool:
    for label, ok, observed in results:
        print(f"{'ok  ' if ok else 'FAIL'} {label}: {observed}")
    return all(ok for _, ok, _ in results)


CHAT_PROMPT = (
    r"You must use the terminal tool. Run exactly: "
    r"printf 'odile-smoke-ok\n' > smoke.txt && whoami >> smoke.txt && cat smoke.txt. "
    r"Then reply with the command output and nothing else."
)


def smoke_chat_command(layout: Layout) -> list[str]:
    """The paid smoke turn's argv. ``--ignore-rules`` is an invariant.

    The smoke runs on a fresh tenant before the user's first chat. Without
    ``--ignore-rules`` it would load ``$HOME/AGENTS.md`` and the
    ``odile-first-run`` skill, start the ritual in a non-interactive turn and
    write ``onboarded_at`` — consuming the user's first run. It also keeps the
    smoke independent of memory and of the bundle.
    """
    return [str(layout.launcher), "chat", "--oneshot", "-Q", "--ignore-rules",
            "--yolo", "-t", "terminal", "-q", CHAT_PROMPT]


def run_smoke(layout: Layout, username: str, password: str | None, dry: bool,
              force: bool = False, first_visit: bool = False) -> bool:
    if dry:
        print("would run    smoke: sign-in 302/401/200, one chat turn straight to NEAR AI, "
              "one shell command, one file write, one cron job")
        return False
    print("== smoke")
    if password:
        table_ok = print_table(login_table(layout, username, password))
        if not table_ok:
            raise SystemExit("error: the sign-in gate did not answer 302/401/200; smoke failed")
    elif first_visit:
        # The installer cannot sign in — it never knew the password. What it can
        # prove is that the gate is shut: no anonymous workspace and no API.
        netloc = urllib.parse.urlsplit(layout.webapp_public_url).netloc
        headers = {"Host": netloc, "Origin": layout.webapp_public_url}
        status_html, _, _ = http_call(layout.webapp_host, layout.webapp_port,
                                      "GET", "/", headers=headers)
        status_api, _, _ = http_call(layout.webapp_host, layout.webapp_port,
                                     "GET", "/api/sessions", headers=headers)
        gate_shut = status_html == 302 and status_api == 401
        print(f"{'ok  ' if gate_shut else 'FAIL'} first visit: the workspace is shut until "
              f"the page sets the sign-in ({status_html}/{status_api})")
        if not gate_shut:
            raise SystemExit("error: the Webapp is not gated before the first visit; smoke failed")
    else:
        print("skip  sign-in 302/401/200 (no plaintext password on this run; the first run proved it)")

    marker = layout.smoke_marker
    if marker.is_file() and not force:
        try:
            recorded = json.loads(marker.read_text(encoding="utf-8"))
        except ValueError:
            recorded = {}
        print(f"same         chat/shell/file/cron smoke recorded at {recorded.get('at', 'an earlier run')}")
        return False

    smoke_file = layout.workspace / "smoke.txt"
    result = as_user(layout, smoke_chat_command(layout), cwd=layout.workspace, timeout=600)
    answer = result.stdout.strip()
    content = smoke_file.read_text(encoding="utf-8") if smoke_file.is_file() else ""
    chat_ok = result.returncode == 0 and "odile-smoke-ok" in content
    print(f"{'ok  ' if chat_ok else 'FAIL'} one chat turn straight to NEAR AI: "
          f"exit {result.returncode}, answer {answer.splitlines()[-1] if answer else '(empty)'!r}")
    print(f"{'ok  ' if 'odile-smoke-ok' in content else 'FAIL'} one shell command + one file write: "
          f"{smoke_file} = {content.splitlines()[:2]!r}")
    if not chat_ok:
        raise SystemExit("error: the chat/shell/file smoke failed")

    cron_ok = ensure_cron_smoke(layout)
    if not cron_ok:
        raise SystemExit("error: the cron smoke failed")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"at": utc_now(), "chat": "ok", "cron": "ok"}),
                      encoding="utf-8")
    return True


def ensure_cron_smoke(layout: Layout) -> bool:
    script = layout.scripts / "odile-smoke.sh"
    log = layout.workspace / "cron-proof.log"
    body = (
        "#!/bin/bash\n"
        "# Odile smoke job — created by odile-install (ODL-203).\n"
        "set -euo pipefail\n"
        f"printf '%s odile-cron-ok\\n' \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" >> {log}\n"
    )
    ensure_file(script, body.encode("utf-8"), 0o755, *user_ids(layout), False,
                detail="(cron smoke script)")
    jobs_file = layout.hermes_home / "cron/jobs.json"
    try:
        data = json.loads(jobs_file.read_text(encoding="utf-8")) if jobs_file.is_file() else {}
    except ValueError:
        data = {}
    jobs = data.get("jobs") if isinstance(data, dict) else data
    jobs = [job for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else []
    match = next((job for job in jobs if job.get("name") == "odile-smoke"), None)
    if match is not None:
        print(f"same         cron job odile-smoke ({match.get('id')}, every 5m, no-agent)")
        return True
    result = as_user(layout, [str(layout.launcher), "cron", "create", "5m",
                              "--name", "odile-smoke", "--no-agent",
                              "--script", "odile-smoke.sh", "--deliver", "local"], timeout=120)
    if result.returncode != 0:
        print(f"FAIL cron create: {result.stdout.strip()} {result.stderr.strip()}", file=sys.stderr)
        return False
    match = re.search(r"Created job:\s*([0-9a-f]+)", result.stdout)
    job_id = match.group(1) if match else ""
    print(f"create       cron job odile-smoke ({job_id}, every 5m, no-agent)")
    if job_id:
        as_user(layout, [str(layout.launcher), "cron", "run", job_id], timeout=120)
    for _ in range(20):
        if log.is_file() and "odile-cron-ok" in log.read_text(encoding="utf-8"):
            print(f"ok   one cron job ran: {log} = {log.read_text().strip().splitlines()[-1]!r}")
            return True
        time.sleep(0.5)
    print(f"FAIL one cron job did not run; see {log}", file=sys.stderr)
    return False


# --------------------------------------------------------------------------
# tmux: a dropped SSH session must not abort the long install


def tmux_command(self_path: Path, argv: list[str]) -> list[str]:
    inner = [sys.executable, str(self_path), *argv, "--stage", "apply"]
    quoted = " ".join("'" + part.replace("'", "'\\''") + "'" for part in inner)
    return ["tmux", "new-session", "-d", "-s", "odile-install", quoted]


def maybe_tmux(args: argparse.Namespace) -> int | None:
    """Re-exec under tmux on an interactive tenant run; None means continue."""
    if (args.root or args.no_tmux or args.stage or args.dry_run
            or not sys.stdin.isatty() or os.environ.get("TMUX")):
        return None
    if shutil.which("tmux") is None:
        print("warning: tmux is not installed; the install runs in this session", file=sys.stderr)
        return None
    extra = [argument for argument in sys.argv[1:] if argument not in {"--stage", "apply"}]
    command = tmux_command(INSTALLER_PATH, extra)
    result = run(command)
    if result.returncode != 0:
        print(f"warning: tmux start failed ({result.stderr.strip()}); running inline", file=sys.stderr)
        return None
    session = "odile-install"
    print(f"tmux         started session {session}; a dropped SSH session will not abort it")
    print(f"             reattach with: tmux attach -t {session}")
    print(f"             the run writes to the pane; status: tmux has-session -t {session}")
    os.execvp("tmux", ["tmux", "attach", "-t", session])
    return 0


# --------------------------------------------------------------------------
# self-test


class Check:
    def __init__(self) -> None:
        self.failures = 0

    def ok(self, name: str, condition: bool, detail: str = "") -> None:
        suffix = f" ({detail})" if detail and not condition else ""
        print(f"{'ok  ' if condition else 'FAIL'} {name}{suffix}")
        self.failures += 0 if condition else 1

    def summary(self) -> int:
        print(f"self-test: {'PASS' if self.failures == 0 else f'{self.failures} FAILED'}")
        return 0 if self.failures == 0 else 1


def self_test() -> int:
    check = Check()
    with tempfile.TemporaryDirectory(prefix="odile-self-test-") as tmp:
        root = Path(tmp)

        # password hash — the provider's scrypt format.
        hashed = hash_password("demo-password-16x")
        check.ok("hash format scrypt$n$r$p$salt$dk",
                 hashed.count("$") == 5 and hashed.startswith("scrypt$"))
        check.ok("hash verifies its password", verify_password("demo-password-16x", hashed))
        check.ok("hash rejects a wrong password", not verify_password("demo-password-16y", hashed))
        check.ok("a 15-character password is refused", password_problem("x" * 15) is not None)
        check.ok("a 16-character password is accepted", password_problem("x" * 16) is None)
        check.ok("generated password is 32 characters", len(generate_password()) == 32)

        # .env allowlist
        good = render_env("odile", hashed, "secret")
        check.ok("tenant .env is in the allowlist", count_env_violations(good) == (0, 0))
        check.ok("tenant .env has exactly three keys", sorted(env_keys(good)) == sorted(TENANT_ENV_KEYS)
                 and len(TENANT_ENV_KEYS) == 3)
        check.ok("the NEAR key is not in the tenant .env",
                 not any("NEAR" in key for key in env_keys(good)))
        seat = good + "GITHUB_TOKEN=ghp_x\nSUDO_PASSWORD=hunter2\n"
        check.ok("a seat-like .env is caught", count_env_violations(seat) == (2, 2))

        # config merge
        seed = {"model": "deepseek-ai/DeepSeek-V4.1-Flash-1017-abliterated",
                "near_api_key": "near-x"}
        layout = Layout.scratch(root=root / "scratch", user="odiletest",
                                webapp_port=9140,
                                public_url="http://odile-sim.test:9140")
        desired = render_tenant_config(layout, seed)
        _, blocks = split_blocks(desired)
        check.ok("the template carries every managed block",
                 all(key in blocks for key in MANAGED_KEYS))
        check.ok("the onboarding block pins run false and profile_build off",
                 'onboarding:\n  run: false\n  profile_build: "off"' in desired)
        check.ok("a converged config reads as `same`", merge_config(desired, desired) is None)
        check.ok("the template's public_url is per slug",
                 'public_url: "http://odile-sim.test:9140"' in desired)
        check.ok("the model route is directly NEAR AI, not a local proxy",
                 'base_url: "https://cloud-api.near.ai/v1"' in desired
                 and not any(re.search(r"127\.0\.0\.1:\d+/v1", line)
                             for line in desired.splitlines()))
        check.ok("the custom provider is named Near AI with the per-user key",
                 "  - name: Near AI\n" in desired
                 and "    key_env: " + NEAR_KEY_ENV + "\n" in desired
                 and "    base_url: " + NEAR_BASE_URL + "\n" in desired)
        check.ok("the tenant template names no meter",
                 "meter" not in desired.replace("parameter", "").lower())
        check.ok("the tenant template carries no meter_port placeholder",
                 "meter_port" not in desired)
        enriched = desired.replace(
            "    model: deepseek-ai/DeepSeek-V4.1-Flash-1017-abliterated\n",
            "    model: deepseek-ai/DeepSeek-V4.1-Flash-1017-abliterated\n"
            "    models:\n      x/y: {}\n    models_discovered: true\n",
        )
        check.ok("Hermes's added provider keys still read as converged",
                 merge_config(enriched, desired) is None)
        drifted = enriched.replace("  port: 9140", "  port: 9120")
        merged = merge_config(drifted, desired)
        check.ok("a drifted port is rewritten and not duplicated",
                 merged is not None and "  port: 9140" in merged and "9120" not in merged)
        check.ok("the rewrite keeps the keys Hermes added",
                 merged is not None and "models_discovered: true" in merged)
        seeded = "_config_version: 52\nmodel:\n  default: something-else\n"
        check.ok("a fresh install's seeded config is replaced whole",
                 merge_config(seeded, desired) == desired)
        # the four-block tenant this program wrote before onboarding existed;
        # it already carries the ODL-240 display block, and a re-run must add
        # onboarding in place without losing display or Hermes's own keys.
        old_tenant = "_config_version: 52\n" + desired.replace(
            '\nonboarding:\n  run: false\n  profile_build: "off"\n', "")
        upgraded = merge_config(old_tenant, desired)
        check.ok("an existing tenant gains onboarding and keeps its own blocks",
                 upgraded is not None
                 and 'onboarding:\n  run: false\n  profile_build: "off"' in upgraded
                 and "display:\n  skin: odile" in upgraded
                 and upgraded.count("onboarding:") == 1)

        # boot-report transformer: pin, checkout parity, and a round trip
        embedded_attest = ATTEST_SOURCE.encode("utf-8")
        check.ok("the embedded boot-report transformer matches its pinned SHA-256",
                 sha256(embedded_attest) == ATTEST_SHA256, sha256(embedded_attest)[:12])
        attest_path = Path(__file__).resolve().parent.parent / "attest.py"
        if attest_path.is_file():
            check.ok("the embedded boot-report transformer matches attest.py",
                     embedded_attest == attest_path.read_bytes())
        else:
            print("skip  attest.py is not beside this checkout")
        embedded_path = root / "embedded-attest.py"
        embedded_path.write_text(ATTEST_SOURCE, encoding="utf-8")
        raw = bytearray(1200)
        raw[0:4] = (5).to_bytes(4, "little")
        raw[0x90:0xC0] = bytes(range(48))
        boot_report = root / "run/blackbox/attest.json"
        boot_report.parent.mkdir(parents=True, exist_ok=True)
        served_path = root / "served/attestation.json"
        boot_report.write_text(json.dumps({
            "status": "ok", "release": "2026.11.1", "cmdline": "console=ttyS0",
            "report_b64": base64.b64encode(bytes(raw)).decode(),
        }), encoding="utf-8")
        result = run([sys.executable, str(embedded_path), "--boot-report", str(boot_report),
                      "--boot-time", "2026-11-09T00:00:00Z", str(served_path)])
        served = json.loads(served_path.read_text(encoding="utf-8")) if served_path.is_file() else {}
        check.ok("the embedded transformer reads the measurement at 0x90:0xC0",
                 result.returncode == 0
                 and served.get("measurement") == bytes(range(48)).hex()
                 and served.get("report_size") == 1200
                 and base64.b64decode(served.get("report_b64", "")) == bytes(raw),
                 result.stderr.strip())
        boot_report.write_text(json.dumps({"status": "error"}), encoding="utf-8")
        failed_path = root / "served/failed.json"
        result = run([sys.executable, str(embedded_path), "--boot-report", str(boot_report),
                      str(failed_path)])
        failed = json.loads(failed_path.read_text(encoding="utf-8")) if failed_path.is_file() else {}
        check.ok("a non-ok boot report is unavailable with a reason",
                 result.returncode == 0 and failed.get("available") is False
                 and "error" in failed, result.stderr.strip())

        # tenant bundle: pins, source parity, write, convergence, removal
        files = tenant_files()
        targets = [item.target for item in files]
        check.ok("the embedded bundle carries $HOME/AGENTS.md and odile-first-run",
                 targets.count("AGENTS.md") == 1
                 and "skills/odile-first-run/SKILL.md" in targets
                 and all(target == "AGENTS.md"
                         or (target.startswith("skills/") and target.endswith("/SKILL.md")
                             and SKILL_NAME_RE.match(target.split("/")[1]))
                         for target in targets),
                 str(targets))
        try:
            validate_tenant_files(files)
            pins_ok, pin_error = True, ""
        except SystemExit as exc:
            pins_ok, pin_error = False, str(exc)
        check.ok("every embedded tenant file matches its pinned SHA-256", pins_ok, pin_error)
        if pins_ok:
            # The embedded copies are the repo's files, byte for byte. A stale
            # embed (source edited, installer not re-embedded) passes the pins
            # above, so compare with the checkout; skip on a tenant, which has
            # only the signed installer.
            repo_root = Path(__file__).resolve().parent.parent
            for item in files:
                source = repo_root / item.source
                if source.is_file():
                    check.ok(f"the embedded {item.source} matches the checkout",
                             item.data() == source.read_bytes())
                else:
                    print(f"skip  {item.source} is not beside this checkout")
            # The loop above proves every embedded file is the repo's file; it
            # cannot see a skill added to the repo after the last embed, which
            # keeps every pin and never reaches a tenant. Membership is the
            # other direction. A tenant has no checkout, so it is skipped there.
            checkout = (repo_root / "templates" / "tenant-agents.md").is_file()
            repo_skills = sorted((repo_root / "skills").glob("*/SKILL.md")) if checkout else []
            if not repo_skills:
                print("skip  skills/ is not beside this checkout")
            else:
                embedded = {item.source for item in files}
                missing = [path.relative_to(repo_root).as_posix()
                           for path in repo_skills
                           if path.relative_to(repo_root).as_posix() not in embedded]
                check.ok("every skills/*/SKILL.md in the checkout is embedded",
                         not missing, ", ".join(missing))
            tampered = tuple(
                dataclasses.replace(item, text=item.text.replace("Odile", "Odjle", 1))
                if item.target == "AGENTS.md" else item
                for item in files
            )
            check.ok("a one-byte edit of an embedded file fails its pin",
                     _raises(SystemExit, validate_tenant_files, tampered))
            me = pwd.getpwuid(os.geteuid()).pw_name
            bundle_layout = Layout.scratch(root=root / "bundle", user=me,
                                           webapp_port=9142,
                                           public_url="http://bundle.test")
            first_write = ensure_tenant_files(bundle_layout, False, files)
            second_write = ensure_tenant_files(bundle_layout, False, files)
            agents_path = bundle_layout.home / "AGENTS.md"
            skill_path = bundle_layout.skills / "odile-first-run/SKILL.md"
            check.ok("the first run writes $HOME/AGENTS.md and the skill",
                     agents_path.is_file()
                     and agents_path.read_text(encoding="utf-8") == TENANT_AGENTS_SOURCE
                     and skill_path.is_file()
                     and skill_path.read_text(encoding="utf-8") == files[1].text)
            check.ok("a second run writes nothing", first_write and not second_write)
            own = bundle_layout.skills / "mine/SKILL.md"
            own.parent.mkdir(parents=True)
            own.write_text("# mine\n", encoding="utf-8")
            reduced = [item for item in files if item.skill != "odile-first-run"]
            removed = ensure_tenant_files(bundle_layout, False, reduced)
            check.ok("a skill removed from the bundle disappears on the next run",
                     removed and not skill_path.parent.exists())
            check.ok("a skill the user added is not removed", own.is_file())
            check.ok("a restored bundle puts the skill back",
                     ensure_tenant_files(bundle_layout, False, files) and skill_path.is_file())
        else:
            print("skip  the bundle write checks: the embedded pins do not verify")

        # the Odile skin (ODL-240)
        check.ok("the template names the Odile skin",
                 "  skin: odile" in blocks.get("display", []))
        check.ok("a converged display block reads as `same`",
                 merge_display_skin(desired) is None)
        with_display = merge_display_skin("_config_version: 52\nmodel:\n  default: x\n")
        check.ok("an older config gains display.skin odile",
                 with_display is not None and "  skin: odile" in with_display)
        kept = merge_display_skin("display:\n  interface: tui\n")
        check.ok("another display key is kept",
                 kept is not None and "  interface: tui" in kept and "  skin: odile" in kept)
        check.ok("the skin is a Hermes skin, not a palette fork",
                 "name: odile" in SKIN_YAML and "customCSS:" in SKIN_YAML
                 and 'background: "#F6F0E4"' in SKIN_YAML
                 and 'ui_accent: "#1C1813"' in SKIN_YAML)
        check.ok("success is ink, including the vendor's emerald icon",
                 "--ui-success: #4A4136 !important" in SKIN_YAML
                 and '[class*="text-emerald-"]' in SKIN_YAML)
        # The vendor strings a skin cannot reach live in the brand script.
        check.ok("the brand script names the agent Odile",
                 "const NAME = 'Odile'" in BRAND_JS
                 and "wordmark.textContent = NAME" in BRAND_JS
                 and "setAttribute('aria-label', NAME)" in BRAND_JS)
        check.ok("the brand script renames the rotating composer prompt",
                 "renamePlaceholders" in BRAND_JS
                 and "value.split('Hermes').join(NAME)" in BRAND_JS
                 and "attributeFilter: PLACEHOLDER_ATTRS" in BRAND_JS)
        check.ok("the brand script credits Hermes in About",
                 "Powered by Hermes" in BRAND_JS and "odile-powered-by" in BRAND_JS)
        check.ok("the brand script guards the theme font",
                 "data-hermes-theme-font" in BRAND_JS and "removeAttribute('href')" in BRAND_JS)
        check.ok("the brand script only seeds a first visit",
                 "hermes-desktop-theme-v2" in BRAND_JS
                 and "localStorage.getItem(THEME_KEY)" in BRAND_JS)
        check.ok("the brand script holds paper over the bundle default",
                 "odile-early-paint" in BRAND_JS
                 and "--theme-primary:#1C1813 !important" in BRAND_JS
                 and "attributeFilter: ['style']" in BRAND_JS)
        check.ok("the early guard waits for a resolved theme",
                 "value.toLowerCase() === '#0053fd'" in BRAND_JS
                 and "setTimeout(" in BRAND_JS)
        shell = ("<!doctype html>\n<html>\n  <head>\n    <title>Hermes</title>\n"
                 "  </head>\n<body></body>\n")
        once = with_brand(shell, "abc123")
        check.ok("the brand tag is linked exactly once",
                 with_brand(once, "abc123") == once and once.count("odile-brand.js") == 1
                 and "odile-brand.js?v=abc123" in once)
        check.ok("stripping the brand tag restores the vendor shell",
                 strip_brand(once) == shell)
        # The embedded copies are the repo's files, byte for byte. The check is
        # skipped on a machine that has only the installer (a tenant, the sim).
        repo_templates = INSTALLER_PATH.parent.parent / "templates"
        if repo_templates.is_dir():
            for name, embedded in (("config.yaml", TENANT_CONFIG_TEMPLATE),
                                   ("skin.yaml", SKIN_YAML),
                                   ("webapp-brand.js", BRAND_JS)):
                path = repo_templates / name
                check.ok(f"installer carries templates/{name} byte for byte",
                         path.is_file() and path.read_text(encoding="utf-8") == embedded)

        # units
        agent_unit = AGENT_UNIT_TEMPLATE.format(
            slug="odl-test", user="odile", group="odile", home=Path("/home/odile"),
            hermes_home=Path("/home/odile/.hermes"), node_path="/opt/node/bin",
            launcher=Path("/home/odile/.hermes/hermes-agent/.hermes/bin/hermes"),
            key_env=Path("/etc/odile/key.env"),
        )
        check.ok("the agent unit reads the per-user key through EnvironmentFile",
                 "EnvironmentFile=/etc/odile/key.env" in agent_unit)
        check.ok("the agent unit no longer pulls in a meter service",
                 "meter" not in agent_unit.lower())
        check.ok("the agent unit runs as odile", "User=odile" in agent_unit)
        check.ok("the agent unit deliberately drops NoNewPrivileges",
                 "NoNewPrivileges=true" not in agent_unit)
        check.ok("the agent unit keeps the gateway's drain timeout",
                 "TimeoutStopSec=90" in agent_unit and "KillMode=mixed" in agent_unit)
        check.ok("the agent unit runs from $HOME (where Hermes reads AGENTS.md)",
                 f"WorkingDirectory={Path('/home/odile')}" in agent_unit)
        smoke_argv = smoke_chat_command(layout)
        check.ok("the smoke chat keeps --ignore-rules (it must not start the ritual)",
                 "--ignore-rules" in smoke_argv and "--yolo" in smoke_argv)

        # first visit (ODL-457): the unit, and the two embedded files
        first_unit = FIRST_VISIT_UNIT_TEMPLATE.format(
            source=Path("/usr/local/lib/odile/first_visit.py"),
            state=Path("/etc/odile/first-visit.json"),
            env_file=Path("/home/odile/.hermes/.env"),
            page=Path("/usr/local/lib/odile/first-visit.html"),
            verify_page=Path("/usr/local/lib/odile/verify.html"),
            report=Path("/var/lib/odile/access-report.txt"),
            attest=Path("/var/lib/odile/attestation.json"),
            recovery=Path("/etc/odile/recovery.txt"),
            port=8091, unit="odile-agent",
            hermes_home=Path("/home/odile/.hermes"), etc=Path("/etc/odile"),
        )
        check.ok("the first-visit unit runs as root, loopback only",
                 "User=root" in first_unit and "--bind 127.0.0.1" in first_unit
                 and "--port 8091" in first_unit and "NoNewPrivileges=true" in first_unit)
        check.ok("the first-visit unit restarts the agent that reads the hash",
                 "--reload-command 'systemctl restart odile-agent'" in first_unit
                 and "ProtectHome=read-only" in first_unit
                 and "ReadWritePaths=/home/odile/.hermes /etc/odile" in first_unit)
        check.ok("the first-visit unit renders /verify from the machine's report",
                 "--verify-page /usr/local/lib/odile/verify.html" in first_unit
                 and "--report-file /var/lib/odile/access-report.txt" in first_unit
                 and "--attest-file /var/lib/odile/attestation.json" in first_unit
                 and "systemctl restart --no-block odile-access-report.service" in first_unit)
        embedded_first = FIRST_VISIT_SOURCE.encode("utf-8")
        check.ok("the embedded first-visit service matches its pinned SHA-256",
                 sha256(embedded_first) == FIRST_VISIT_SHA256,
                 sha256(embedded_first)[:12])
        embedded_page = FIRST_VISIT_PAGE.encode("utf-8")
        check.ok("the embedded first-visit page matches its pinned SHA-256",
                 sha256(embedded_page) == FIRST_VISIT_PAGE_SHA256,
                 sha256(embedded_page)[:12])
        repo_first = INSTALLER_PATH.parent / "first_visit.py"
        if repo_first.is_file():
            check.ok("the embedded first-visit service matches installer/first_visit.py",
                     embedded_first == repo_first.read_bytes())
        else:
            print("skip  installer/first_visit.py is not beside this checkout")
        repo_page = INSTALLER_PATH.parent.parent / "web" / "first-visit.html"
        if repo_page.is_file():
            check.ok("the embedded first-visit page matches web/first-visit.html",
                     embedded_page == repo_page.read_bytes())
        else:
            print("skip  web/first-visit.html is not beside this checkout")

        # the /verify surface (ODL-473): pins, checkout parity, the wrapper
        for name, text, expected in (
            ("/verify page", VERIFY_PAGE, VERIFY_PAGE_SHA256),
            ("access-report wrapper", ACCESS_REPORT_SCRIPT, ACCESS_REPORT_SCRIPT_SHA256),
            ("access reporter", VERIFY_UNLOCK, VERIFY_UNLOCK_SHA256),
            ("handoff audit", AUDIT_SCRIPT, AUDIT_SCRIPT_SHA256),
        ):
            data = text.encode("utf-8")
            check.ok(f"the embedded {name} matches its pinned SHA-256",
                     sha256(data) == expected, sha256(data)[:12])
        for posix, text in (
            ("web/verify.html", VERIFY_PAGE),
            ("installer/access_report.sh", ACCESS_REPORT_SCRIPT),
            ("scripts/verify_and_unlock.sh", VERIFY_UNLOCK),
            ("scripts/audit.py", AUDIT_SCRIPT),
        ):
            source = INSTALLER_PATH.parent.parent / posix
            if source.is_file():
                check.ok(f"the embedded {posix} matches the checkout",
                         text.encode("utf-8") == source.read_bytes())
            else:
                print(f"skip  {posix} is not beside this checkout")
        access_unit = ACCESS_REPORT_UNIT_TEMPLATE.format(
            wrapper=Path("/usr/local/lib/odile/access_report.sh"),
            var=Path("/var/lib/odile"), agent_unit="odile-agent")
        check.ok("the access-report unit runs the wrapper as root at boot",
                 "Type=oneshot" in access_unit
                 and "Environment=HOME=/root" in access_unit
                 and "ExecStart=/usr/local/lib/odile/access_report.sh" in access_unit
                 and "User=" not in access_unit
                 and "ReadWritePaths=/var/lib/odile" in access_unit
                 and "Before=odile-agent.service" in access_unit
                 and "WantedBy=multi-user.target" in access_unit)
        wrapper_path = root / "access_report.sh"
        wrapper_path.write_text(ACCESS_REPORT_SCRIPT, encoding="utf-8")
        wrapper_path.chmod(0o755)
        pin_path = root / "tenant-image.pin"
        pinned = "ab" * 48
        pin_path.write_text(json.dumps({"uki_signed": {"measurement": pinned}}),
                            encoding="utf-8")
        reporter_path = root / "reporter-stub.sh"
        args_path = root / "reporter-args.txt"
        reporter_path.write_text("#!/bin/sh\nprintf '%%s\\n' \"$@\" > '%s'\n"
                                 % args_path, encoding="utf-8")
        reporter_path.chmod(0o755)
        out_path = root / "access-report.txt"
        result = run(["/bin/sh", str(wrapper_path)], env={
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "ODILE_ACCESS_PIN_FILES": str(pin_path),
            "ODILE_REPORTER": str(reporter_path),
            "ODILE_ACCESS_OUT": str(out_path),
        })
        args = args_path.read_text(encoding="utf-8").splitlines() if args_path.is_file() else []
        check.ok("the wrapper runs the card's command with the image's pinned measurement",
                 result.returncode == 0 and args == [
                     "--mode", "access", "--root", "/", "--user", "odile",
                     "--expect", pinned, "--out", str(out_path)],
                 result.stderr.strip())
        pin_path.write_text("{}", encoding="utf-8")
        result = run(["/bin/sh", str(wrapper_path)], env={
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "ODILE_ACCESS_PIN_FILES": str(pin_path),
            "ODILE_REPORTER": str(reporter_path),
            "ODILE_ACCESS_OUT": str(out_path),
        })
        check.ok("the wrapper refuses to run with no pinned measurement",
                 result.returncode == 2 and "no 96-hex measurement" in result.stderr,
                 result.stderr.strip())
        # The report's one authorized key: the throwaway boot key the measured
        # command line carries, public half only, 0600 in a 0700 directory.
        boot_key_line = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILb7n1V0XJ0m0J6j"
                         "Q0k0Q0k0Q0k0Q0k0Q0k0Q0k0Q0k0 odile-bench throwaway boot key")
        authorized = layout.path("/root/.ssh/authorized_keys")
        with contextlib.redirect_stdout(io.StringIO()):
            wrote = ensure_verify_surface(
                layout, False, base64.b64encode(boot_key_line.encode()).decode("ascii"))
        check.ok("the installer authorizes only the throwaway boot key, public half",
                 wrote and authorized.read_text(encoding="utf-8") == boot_key_line + "\n"
                 and (authorized.stat().st_mode & 0o777) == 0o600
                 and (authorized.parent.stat().st_mode & 0o777) == 0o700,
                 str(authorized))
        state_token = "ab" * 32
        state_body = first_visit_state(state_token)
        state = json.loads(state_body)
        check.ok("the first-visit state stores the token as SHA-256 only",
                 state_token not in state_body.decode("utf-8")
                 and state.get("token_sha256") == sha256(state_token.encode("ascii"))
                 and state.get("used_at") is None
                 and state.get("recovery_hash") is None)
        check.ok("a malformed first-visit token is refused",
                 _raises(SystemExit, first_visit_state, "not-a-token"))

        # Caddy splice
        fixture = "odile.fyi {\n\trespond 200\n}\n"
        block = CADDY_SITE_TEMPLATE.format(begin="odile-odl-test", fqdn="odl-test.odile.fyi",
                                           user="odile", webapp_port=9120,
                                           var=Path("/var/lib/odile"),
                                           first_visit_handle="")
        with_first_visit = CADDY_SITE_TEMPLATE.format(
            begin="odile-odl-test", fqdn="odl-test.odile.fyi", user="odile",
            webapp_port=9120, var=Path("/var/lib/odile"),
            first_visit_handle=CADDY_FIRST_VISIT_HANDLE.format(port=8091))
        spliced = splice_marked(fixture, block, "# BEGIN odile-odl-test", "# END odile-odl-test")
        check.ok("the Caddy block is appended after the existing file", "odl-test.odile.fyi {" in spliced)
        replaced = splice_marked(spliced, block, "# BEGIN odile-odl-test", "# END odile-odl-test")
        check.ok("a second splice is a no-op", replaced == spliced)
        check.ok("an unbalanced marker pair is an error",
                 _raises(ValueError, splice_marked, spliced + "# BEGIN odile-odl-test\n",
                         block, "# BEGIN odile-odl-test", "# END odile-odl-test"))
        check.ok("the Caddy block has no demo host", "demo.odile.fyi" not in block)
        check.ok("the Caddy block serves /attestation from the served file, no-store",
                 "handle /attestation {" in block
                 and 'header Cache-Control "no-store"' in block
                 and 'header Content-Type "application/json"' in block
                 and "rewrite * /attestation.json" in block
                 and "root * /var/lib/odile" in block)
        check.ok("the Caddy block sends every other path to the Webapp",
                 block.count("reverse_proxy") == 1
                 and "handle {\n\t\treverse_proxy 127.0.0.1:9120" in block
                 and "handle /first-visit*" not in block)
        check.ok("a first-visit machine proxies /verify and /first-visit* to loopback",
                 with_first_visit.count("reverse_proxy") == 3
                 and "handle /verify {" in with_first_visit
                 and "handle /first-visit* {" in with_first_visit
                 and "reverse_proxy 127.0.0.1:8091" in with_first_visit
                 and with_first_visit.index("handle /first-visit*")
                 < with_first_visit.index("handle {\n\t\treverse_proxy 127.0.0.1:9120"))
        check.ok("the /verify route sits after /attestation and before the Webapp",
                 with_first_visit.index("handle /attestation")
                 < with_first_visit.index("handle /verify {")
                 < with_first_visit.index("handle {\n\t\treverse_proxy 127.0.0.1:9120"))
        check.ok("a classic machine has no dead /verify or /first-visit route",
                 "handle /verify" not in block and "handle /first-visit*" not in block)
        check.ok("the first-visit route sits after /attestation and before the Webapp",
                 with_first_visit.index("handle /attestation")
                 < with_first_visit.index("handle /first-visit*"))
        with_manifest = CADDY_SITE_TEMPLATE.format(
            begin="odile-odl-test", fqdn="odl-test.odile.fyi", user="odile",
            webapp_port=9120, var=Path("/var/lib/odile"),
            first_visit_handle=CADDY_MANIFEST_HANDLE.format(port=MANIFEST_PORT))
        check.ok("a machine with the signed pair proxies /manifest and /manifest.sig, no-store",
                 with_manifest.count("reverse_proxy") == 3
                 and "handle /manifest {" in with_manifest
                 and "handle /manifest.sig {" in with_manifest
                 and "reverse_proxy 127.0.0.1:8012" in with_manifest
                 and "header Cache-Control \"no-store\"" in
                 with_manifest.split("handle /manifest {", 1)[1].split("}", 1)[0])
        check.ok("the manifest routes sit after /attestation and before the Webapp",
                 with_manifest.index("handle /attestation")
                 < with_manifest.index("handle /manifest {")
                 < with_manifest.index("handle {\n\t\treverse_proxy 127.0.0.1:9120"))
        check.ok("a classic install has no /manifest route",
                 "handle /manifest" not in block and "handle /manifest" not in with_first_visit)

        # the sign-in gate shape, reused from deploy_demo_site.py
        check.ok("the measured gate passes 302/401/200", gate_ok(302, 401, 200))
        check.ok("an anonymous 200 is not a gate", not gate_ok(200, 401, 200))
        check.ok("an open API is not a gate", not gate_ok(302, 200, 200))
        check.ok("a broken session is not a gate", not gate_ok(302, 401, 500))

        # recovery
        recovery = render_recovery("odl-test", "srv-1", "B" * 32, None, "odl-test.odile.fyi")
        check.ok("the recovery file carries the backup password", "Backup password:     " + "B" * 32 in recovery)
        check.ok("the recovery file carries the fingerprint placeholder",
                 "Certificate SHA-256: (pending" in recovery)
        check.ok("the recovery file is browser-only (no scp, shred or root@)",
                 re.search(r"(?i)scp|shred|root@", recovery) is None)
        check.ok("the recovery file names the agent as the way to see it again",
                 "ask your agent to show it again" in recovery.lower())
        check.ok("the recovery file points at the first-visit download",
                 "first-visit page" in recovery)
        check.ok("a real fingerprint replaces the placeholder",
                 "sha256:abcd" in render_recovery("odl-test", "srv-1", "B" * 32, "sha256:abcd", "x"))
        check.ok("the backup password is reused from an existing recovery file",
                 parse_recovery(recovery).get("backup_password") == "B" * 32)
        recovery_layout = Layout.scratch(root=root / "recovery", user="odiletest",
                                         webapp_port=9140,
                                         public_url="http://odl-test.odile.fyi")
        recovery_layout.etc.mkdir(parents=True, exist_ok=True)
        first_change, first_backup, first_created = ensure_recovery(
            recovery_layout, "odl-test", "srv-1", None, False, "B" * 32)
        second_change, second_backup, second_created = ensure_recovery(
            recovery_layout, "odl-test", "srv-1", None, False, None)
        check.ok("a second recovery render is `same` (the Written line is kept)",
                 first_change and first_created and not second_change and not second_created
                 and first_backup == second_backup,
                 f"changed={second_change} created={second_created}")

        # seed validation
        body = {"slug": "odl-test", "instance_id": "srv-1", "near_api_key": "near-x",
                "model": "m", "support_public_key": "ssh-ed25519 AAA",
                "backup_repo_url": "sftp:x", "mail_relay": {"host": "h", "port": 587,
                "username": "u", "secret": "s"}, "hermes_version": "v0.23.0"}
        validate_seed(body, "srv-1", "odl-test")
        check.ok("the eight-field seed validates", True)
        check.ok("a missing field is rejected", _raises(SystemExit, validate_seed,
                                                       {k: v for k, v in body.items() if k != "model"},
                                                       "srv-1"))
        check.ok("a password field is rejected",
                 _raises(SystemExit, validate_seed, {**body, "password": "x"}, "srv-1"))
        check.ok("a wrong instance is rejected", _raises(SystemExit, validate_seed, body, "srv-2"))
        check.ok("a wrong slug is rejected", _raises(SystemExit, validate_seed, body, "srv-1", "other"))

        # signature: an ephemeral key proves the mechanism; the shipped key
        # proves this file. A one-byte edit must fail both.
        key = root / "key.pem"
        pub = root / "key.pub"
        data = root / "payload"
        data.write_bytes(b"odile installer payload\n")
        run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(key)], check=True)
        run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)], check=True)
        signature = run(["openssl", "dgst", "-sign", str(key), "-out", str(root / "sig.bin"),
                         str(data)], check=True)
        sig = (root / "sig.bin").read_bytes()
        check.ok("an untouched payload verifies", verify_bytes(data.read_bytes(), sig, pub))
        tampered = bytearray(data.read_bytes())
        tampered[0] ^= 0x01
        check.ok("a one-byte edit fails verification", not verify_bytes(bytes(tampered), sig, pub))

        shipped_sig = INSTALLER_PATH.with_name(INSTALLER_PATH.name + ".sig")
        shipped_pub = INSTALLER_PATH.parent / "odile-signing-key.pub"
        if shipped_sig.is_file() and shipped_pub.is_file():
            ok, detail = verify_installer(INSTALLER_PATH, shipped_sig, shipped_pub)
            check.ok("this installer's shipped signature verifies", ok, detail)
        else:
            print("skip  this installer's shipped signature: .sig/.pub not beside it")

        # The trust anchor is the key, not the signature. The printed digest is
        # `sha256sum` of the published file and the fingerprint is the manifest
        # tools' spelling of the key; the kit's field must agree with the key in
        # use. A bundle and key swapped together carry their own constants, so
        # the user's out-of-band check against the URL is the remaining gate.
        if shipped_pub.is_file():
            fingerprint = key_fingerprint(shipped_pub)
            digest = sha256(shipped_pub.read_bytes())
            check.ok("the shipped key's fingerprint is the published one",
                     fingerprint == PUBLISHED_KEY_FINGERPRINT, fingerprint)
            check.ok("the shipped key's file sha256 is the published one",
                     digest == PUBLISHED_KEY_SHA256, digest)
            check.ok("a kit naming the key in use is accepted (case-insensitive hex)",
                     signing_key_problem({"signing_key_sha256": digest.upper()}, digest) is None)
            check.ok("a kit without signing_key_sha256 is accepted",
                     signing_key_problem({}, digest) is None)
            check.ok("a kit naming another key is refused",
                     signing_key_problem({"signing_key_sha256": "0" * 64}, digest) is not None)
            check.ok("a kit field that is not a string is refused",
                     signing_key_problem({"signing_key_sha256": 7}, digest) is not None)
        else:
            print("skip  the published signing-key values: the key is not beside this installer")

        # the boot report (ODL-396): the pure decision, then the record on disk
        boot_key_text = ("ssh-ed25519 "
                         "AAAAC3NzaC1lZDI1NTE5AAAAIMdWyPF1Bcwd6sFiL4KiUtmYTq2FLe95TmfBQS0fEnu2")
        boot_key = base64.b64encode(boot_key_text.encode("ascii")).decode("ascii")
        boot_measurement = "11" * 48
        boot_cmdline = ("console=tty0 root=/dev/mapper/root_crypt ro "
                        "bb.release=2026.11.1 bb.ip4=203.0.113.120/24 "
                        "bb.key=" + boot_key + " initrd=initrd")

        def boot_payload(*, measurement: str = boot_measurement, status: str = "ok",
                         release: str = "2026.11.1", cmdline: str = boot_cmdline,
                         version: int = 5, size: int = 1184) -> dict:
            raw = bytearray(size)
            if size >= 4:
                raw[0:4] = version.to_bytes(4, "little")
            if size >= MEASUREMENT_END:
                raw[MEASUREMENT_OFFSET:MEASUREMENT_END] = bytes.fromhex(measurement)
            return {"version": 1, "release": release, "status": status,
                    "hostkey": "ssh-ed25519 AAAA", "fingerprint": "SHA256:host",
                    "report_b64": base64.b64encode(bytes(raw)).decode("ascii"),
                    "cmdline": cmdline}

        fingerprint = boot_key_fingerprint(boot_key)
        boot_pub = root / "boot-key.pub"
        boot_pub.write_text(boot_key_text + "\n", encoding="utf-8")
        try:
            listed = run(["ssh-keygen", "-lf", str(boot_pub)])
        except FileNotFoundError:
            listed = None
        printed = re.search(r"(SHA256:[A-Za-z0-9+/]+=?)", listed.stdout) if listed else None
        if listed is not None and listed.returncode == 0 and printed:
            check.ok("the decoded bb.key matches ssh-keygen's SHA256 fingerprint",
                     fingerprint == printed.group(1), f"{fingerprint} vs {printed.group(1)}")
        else:
            print("skip  ssh-keygen is not available for the boot-key fingerprint")
        # A normal ssh-keygen line carries a comment; the comment is not part of
        # the key blob and does not change ssh-keygen -lf. An options prefix
        # (authorized_keys form) is skipped the way Servury's key reader does.
        commented_text = boot_key_text + " odile-fixture@example.com"
        commented_key = base64.b64encode(commented_text.encode("utf-8")).decode("ascii")
        check.ok("a trailing comment on bb.key does not change the fingerprint",
                 boot_key_fingerprint(commented_key) == fingerprint)
        commented = assess_boot_report(
            boot_payload(cmdline=boot_cmdline.replace("bb.key=" + boot_key,
                                                      "bb.key=" + commented_key)),
            expect=boot_measurement, release=None, kit_fingerprint=fingerprint, record=None)
        check.ok("a comment-bearing bb.key matches the kit fingerprint",
                 commented.ok and commented.kind == "ok")
        optioned_text = 'command="/bin/true" ' + boot_key_text + " 例え@example"
        optioned_key = base64.b64encode(optioned_text.encode("utf-8")).decode("ascii")
        check.ok("options and a UTF-8 comment on bb.key do not change the fingerprint",
                 boot_key_fingerprint(optioned_key) == fingerprint)
        optioned = assess_boot_report(
            boot_payload(cmdline=boot_cmdline.replace("bb.key=" + boot_key,
                                                      "bb.key=" + optioned_key)),
            expect=boot_measurement, release=None, kit_fingerprint=fingerprint, record=None)
        check.ok("an options-prefixed bb.key matches the kit fingerprint",
                 optioned.ok and optioned.kind == "ok")
        check.ok("a line with no key type is not a public key",
                 boot_key_fingerprint(base64.b64encode(b"not a key").decode("ascii")) is None)
        matched = assess_boot_report(boot_payload(), expect=boot_measurement, release=None,
                                     kit_fingerprint=fingerprint, record=None)
        check.ok("a boot report that matches --expect passes", matched.ok and matched.kind == "ok")
        check.ok("the measurement comes from the report at 0x90:0xC0",
                 matched.measurement == boot_measurement)
        mismatch = assess_boot_report(boot_payload(), expect="22" * 48, release=None,
                                      kit_fingerprint=None, record=None)
        check.ok("a different --expect fails as a boot check",
                 not mismatch.ok and mismatch.kind == "boot"
                 and boot_measurement in mismatch.detail and "22" * 48 in mismatch.detail)
        stopped = assess_boot_report(boot_payload(status="error"), expect=boot_measurement,
                                     release=None, kit_fingerprint=None, record=None)
        check.ok("status != ok fails closed", not stopped.ok and stopped.kind == "boot")
        absent = assess_boot_report(None, expect=boot_measurement, release=None,
                                    kit_fingerprint=None, record=None)
        check.ok("a missing report fails closed", not absent.ok and absent.kind == "boot")
        boot_record = {"schema": BOOT_RECORD_SCHEMA, "release": "2026.11.1",
                       "measurement": boot_measurement,
                       "cmdline_sha256": sha256(cmdline_for_hash(boot_cmdline).encode("utf-8"))}
        changed = assess_boot_report(
            boot_payload(cmdline=boot_cmdline.replace("console=tty0", "console=tty1")),
            expect=None, release=None, kit_fingerprint=None, record=boot_record)
        check.ok("a changed cmdline fails against the record",
                 not changed.ok and changed.kind == "boot" and "cmdline_sha256" in changed.detail)
        same = assess_boot_report(boot_payload(), expect=None, release=None,
                                  kit_fingerprint=None, record=boot_record)
        check.ok("the same boot reads as `same boot record`", same.ok and same.same)
        missing_expect = assess_boot_report(boot_payload(), expect=None, release=None,
                                            kit_fingerprint=None, record=None)
        check.ok("a missing --expect and no record is a usage error",
                 not missing_expect.ok and missing_expect.kind == "usage")
        wrong_key = assess_boot_report(boot_payload(), expect=boot_measurement, release=None,
                                       kit_fingerprint="SHA256:" + "A" * 43, record=None)
        check.ok("a kit fingerprint mismatch fails", not wrong_key.ok and wrong_key.kind == "boot")
        wrong_release = assess_boot_report(boot_payload(), expect=boot_measurement,
                                           release="2026.12.9", kit_fingerprint=None, record=None)
        check.ok("--release is asserted", not wrong_release.ok and wrong_release.kind == "boot")
        short = assess_boot_report(boot_payload(size=1183), expect=boot_measurement,
                                   release=None, kit_fingerprint=None, record=None)
        check.ok("report_b64 shorter than 1184 bytes fails", not short.ok and short.kind == "boot")
        old_version = assess_boot_report(boot_payload(version=2), expect=boot_measurement,
                                         release=None, kit_fingerprint=None, record=None)
        check.ok("a report version other than 5 fails",
                 not old_version.ok and old_version.kind == "boot")
        no_key = assess_boot_report(
            boot_payload(cmdline=boot_cmdline.replace(" bb.key=" + boot_key, "")),
            expect=boot_measurement, release=None, kit_fingerprint=None, record=None)
        check.ok("a command line without bb.key= fails", not no_key.ok and no_key.kind == "boot")
        two_keys = assess_boot_report(boot_payload(cmdline=boot_cmdline + " bb.key=" + boot_key),
                                      expect=boot_measurement, release=None,
                                      kit_fingerprint=None, record=None)
        check.ok("a command line with two bb.key= fields fails",
                 not two_keys.ok and two_keys.kind == "boot")
        no_ip4 = assess_boot_report(
            boot_payload(cmdline=boot_cmdline.replace(" bb.ip4=203.0.113.120/24", "")),
            expect=boot_measurement, release=None, kit_fingerprint=None, record=None)
        check.ok("a command line without bb.ip4= fails", not no_ip4.ok and no_ip4.kind == "boot")

        # the whole check on disk: write, read `same`, fail a move, replace
        boot_layout = Layout.scratch(root=root / "boot", user="odiletest",
                                     webapp_port=9144,
                                     public_url="http://odl-boot.test")
        boot_path = root / "attest.json"
        boot_path.write_text(json.dumps(boot_payload()), encoding="utf-8")

        def boot_namespace(**overrides: object) -> argparse.Namespace:
            values: dict[str, object] = {"boot_report": str(boot_path),
                                         "expect": None, "release": None}
            values.update(overrides)
            return argparse.Namespace(**values)

        def boot_exit(namespace: argparse.Namespace) -> int:
            try:
                check_boot(boot_layout, namespace, {})
            except SystemExit as exc:
                return exc.code if isinstance(exc.code, int) else 1
            return 0

        first_code = boot_exit(boot_namespace(expect=boot_measurement))
        written = json.loads(boot_layout.boot_record.read_text(encoding="utf-8"))
        check.ok("the first check writes /etc/odile/boot-record.json 0644",
                 first_code == 0 and boot_layout.boot_record.is_file()
                 and (boot_layout.boot_record.stat().st_mode & 0o777) == 0o644)
        check.ok("the boot record carries the §4.3 fields and the provenance",
                 written.get("schema") == BOOT_RECORD_SCHEMA
                 and written.get("release") == "2026.11.1"
                 and written.get("measurement") == boot_measurement
                 and written.get("expected") == boot_measurement
                 and written.get("cmdline_sha256") == boot_record["cmdline_sha256"]
                 and written.get("artifacts") is None
                 and isinstance(written.get("vcpus"), int)
                 and written.get("checked_at")
                 and written.get("installer_sha256") == sha256(INSTALLER_PATH.read_bytes())
                 and written.get("source") == str(boot_path))
        before = boot_layout.boot_record.read_bytes()
        second_code = boot_exit(boot_namespace())
        check.ok("a second run reads `same boot record` and rewrites nothing",
                 second_code == 0 and boot_layout.boot_record.read_bytes() == before)
        third_code = boot_exit(boot_namespace(expect=boot_measurement))
        check.ok("a re-run with the same --expect also reads `same`",
                 third_code == 0 and boot_layout.boot_record.read_bytes() == before)
        moved_path = root / "attest-moved.json"
        moved_path.write_text(json.dumps(boot_payload(measurement="22" * 48)), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            moved_code = boot_exit(boot_namespace(boot_report=str(moved_path)))
        check.ok("a moved measurement with no --expect exits 5", moved_code == 5)
        replaced_code = boot_exit(boot_namespace(boot_report=str(moved_path),
                                                 expect="22" * 48))
        replaced = json.loads(boot_layout.boot_record.read_text(encoding="utf-8"))
        check.ok("a new --expect replaces the record",
                 replaced_code == 0 and replaced.get("measurement") == "22" * 48)
        empty_layout = Layout.scratch(root=root / "boot-empty", user="odiletest",
                                      webapp_port=9146,
                                      public_url="http://odl-boot-empty.test")
        skipped = check_boot(empty_layout, argparse.Namespace(
            boot_report=None, expect=None, release=None), {})
        check.ok("a scratch run with no report prints skip and continues", skipped is None)

        # layout
        tenant = Layout.tenant(user="odile", webapp_port=9120,
                               public_url="https://odl-test.odile.fyi")
        check.ok("tenant paths are absolute", tenant.home == Path("/home/odile")
                 and tenant.seed_cache == Path("/etc/odile/seed.json")
                 and tenant.key_env == Path("/etc/odile/key.env"))
        check.ok("/etc/odile is 0750 root:manifest-group, 0750-or-tighter",
                 MANIFEST_GROUP == "odile-manifest")
        key_layout = Layout.scratch(root=root / "keyenv", user="odiletest",
                                    webapp_port=9152, public_url="http://odl-key.test:9152")
        with contextlib.redirect_stdout(io.StringIO()):
            wrote_key = ensure_key_env(key_layout, {"near_api_key": "near-fixture-key"}, False)
        check.ok("the seed's key lands in /etc/odile/key.env at 0600, one line",
                 wrote_key and key_layout.key_env.is_file()
                 and (key_layout.key_env.stat().st_mode & 0o777) == 0o600
                 and key_layout.key_env.read_text(encoding="utf-8")
                 == NEAR_KEY_ENV + "=near-fixture-key\n")
        check.ok("a second key write is `same`",
                 not ensure_key_env(key_layout, {"near_api_key": "near-fixture-key"}, False))
        check.ok("the scratch gateway gets the key at exec, as the unit's EnvironmentFile does",
                 gateway_env(key_layout).get(NEAR_KEY_ENV) == "near-fixture-key")
        check.ok("scratch paths stay under the root",
                 str(layout.home).startswith(str(root / "scratch"))
                 and str(layout.seed_cache).startswith(str(root / "scratch")))
        check.ok("tenant host comes from public_url", tenant.fqdn == "odl-test.odile.fyi")

        # ODL-332 follow-up: the installer places its own bytes where the
        # manifest and CLAIMS §4.6 hash them, before the boot record is
        # written. The scratch root carries the same absolute path.
        check.ok("the tenant copy path is the one the manifest names",
                 tenant.installer_copy == Path("/usr/local/bin/odile-install.sh"),
                 str(tenant.installer_copy))
        copy_layout = Layout.scratch(root=root / "copy", user="odiletest", webapp_port=9148, public_url="http://odl-copy.test:9148")
        with contextlib.redirect_stdout(io.StringIO()):
            copied = ensure_installer_copy(copy_layout, False)
        target = copy_layout.installer_copy
        check.ok("the installer copies itself byte-identically at 0755",
                 copied and target.read_bytes() == INSTALLER_PATH.read_bytes()
                 and (target.stat().st_mode & 0o777) == 0o755, str(target))
        second_out = io.StringIO()
        with contextlib.redirect_stdout(second_out):
            again = ensure_installer_copy(copy_layout, False)
        check.ok("a second copy of itself reads `same`",
                 not again and target.read_bytes() == INSTALLER_PATH.read_bytes()
                 and f"same         {target}" in second_out.getvalue())

        # OS package pins: every package the tenant install writes is an exact
        # Debian 13 version, and apt may read only the one dated snapshot.
        specs = apt_package_specs()
        check.ok("every required OS package is an exact name=version pin",
                 len(specs) == len(OS_PACKAGE_PINS)
                 and all(re.fullmatch(r"[^=]+=[0-9][A-Za-z0-9.+~:-]*", spec) for spec in specs),
                 ", ".join(specs))
        check.ok("the optional browser is pinned too",
                 apt_package_specs(optional=True)
                 == specs + [f"{name}={version}" for name, version in OS_OPTIONAL_PINS.items()])
        check.ok("the pins file lands in the tenant's /etc/odile",
                 apt_pins_file(tenant) == Path("/etc/odile/apt-pins.list"))
        check.ok("the pinned suite is Debian 13 (trixie)", OS_SUITE == "trixie")
        check.ok("the pinned snapshot is a UTC timestamp",
                 bool(re.fullmatch(r"\d{8}T\d{6}Z", OS_SNAPSHOT)), OS_SNAPSHOT)
        sources = apt_sources()
        check.ok("the pins source names the snapshot and the suite",
                 f"/archive/debian/{OS_SNAPSHOT} {OS_SUITE} main" in sources
                 and "check-valid-until=no" in sources, sources)
        command = apt_command(tenant, "update")
        check.ok("apt reads the pins file and no other sources",
                 f"Dir::Etc::sourcelist={apt_pins_file(tenant)}" in command
                 and "Dir::Etc::sourceparts=-" in command, " ".join(command))

        # the Webapp build's heap cap: sized from RAM, bounded both ways
        check.ok("a 4 GB host caps the build heap below the V8 default",
                 webapp_heap_mb("MemTotal:        4194304 kB\n") == 2457)
        check.ok("a huge host caps the build heap at 3072 MB",
                 webapp_heap_mb("MemTotal:       67108864 kB\n") == 3072)
        check.ok("a tiny host keeps the build heap above the GC floor",
                 webapp_heap_mb("MemTotal:         524288 kB\n") == 1536)
        check.ok("an unreadable meminfo assumes 2 GB", webapp_heap_mb("") == 1536)

        # readiness: the port opens before the Webapp answers, and a 503 during
        # startup is not ready. A stub server proves the wait crosses it.
        import http.server
        import threading

        class _Probe(http.server.BaseHTTPRequestHandler):
            hits = 0

            def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
                type(self).hits += 1
                self.send_response(503 if type(self).hits < 3 else 302)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                pass

        probe = http.server.HTTPServer(("127.0.0.1", 0), _Probe)
        threading.Thread(target=probe.serve_forever, daemon=True).start()
        probe_layout = Layout.scratch(root=root / "probe", user="probe",
                                      webapp_port=probe.server_address[1],
                                      public_url="http://probe.test")
        ready = wait_for_webapp(probe_layout, 10)
        probe.shutdown()
        probe.server_close()
        check.ok("a 503 is not readiness; the gate's first 302 is",
                 ready and _Probe.hits >= 3, f"{_Probe.hits} probe(s)")

        # the tenant backup (ODL-215 follow-up): pin, checkout parity, and the
        # two units it arms
        embedded_backup = BACKUP_SOURCE.encode("utf-8")
        check.ok("the embedded backup program matches its pinned hash",
                 sha256(embedded_backup) == BACKUP_SHA256, sha256(embedded_backup)[:12])
        repo_backup = Path(__file__).resolve().parent.parent / "scripts/backup.py"
        if repo_backup.is_file():
            check.ok("the embedded backup program matches scripts/backup.py in this checkout",
                     embedded_backup == repo_backup.read_bytes())
        else:
            print("skip  scripts/backup.py is not beside this checkout")
        backup_layout = Layout.tenant(user="odile", webapp_port=9120,
                                      public_url="https://odl-test.odile.fyi")
        backup_units = backup_unit_files(backup_layout, "odl-test", HEARTBEAT_URL)
        backup_service = backup_units[backup_layout.backup_service]
        backup_timer = backup_units[backup_layout.backup_timer]
        check.ok("the backup unit runs the placed program against the seed's repository file",
                 f"ExecStart=/usr/bin/python3 {backup_layout.backup_source} run "
                 f"--repo-file {backup_layout.backup_repo_file}" in backup_service
                 and f"--recovery {backup_layout.recovery}" in backup_service)
        check.ok("the backup unit posts its heartbeat to the production route",
                 f"ExecStartPost=-/usr/bin/python3 {backup_layout.backup_source} heartbeat"
                 in backup_service and f"--url {HEARTBEAT_URL}" in backup_service
                 and "--key-dir /etc/odile/heartbeat" in backup_service)
        check.ok("the backup timer is daily at 03:00, persistent and enabled",
                 "OnCalendar=*-*-* 03:00:00" in backup_timer
                 and "Persistent=true" in backup_timer
                 and "WantedBy=timers.target" in backup_timer)
        check.ok("the installer's heartbeat default is the program's default",
                 HEARTBEAT_URL == "https://odile.fyi/heartbeat")

        # the Hermes install script (ODL-399): the one third-party program the
        # installer executes. The pin's shape is checked, then the order — a
        # mismatched script must be refused before `bash` starts, and a matching
        # one must reach it. A file:// URL keeps this machine-free.
        check.ok("the Hermes install-script pin is 64 lowercase hex",
                 bool(re.fullmatch(r"[0-9a-f]{64}", HERMES_INSTALL_SHA256)),
                 HERMES_INSTALL_SHA256)
        script_path = root / "install-script.sh"
        script_path.write_text(
            "#!/bin/bash\n"
            'mkdir -p "$HERMES_HOME/hermes-agent/.hermes/bin"\n'
            'printf \'#!/bin/sh\\n\' > "$HERMES_HOME/hermes-agent/.hermes/bin/hermes"\n'
            'touch "$HOME/install-ran"\n',
            encoding="utf-8",
        )
        install_layout = Layout.scratch(
            root=root / "install", user=pwd.getpwuid(os.geteuid()).pw_name,
            webapp_port=9144, public_url="http://install.test")
        install_layout.home.mkdir(parents=True, exist_ok=True)
        ran = install_layout.home / "install-ran"
        local_layout = dataclasses.replace(install_layout, installer_url=script_path.as_uri())
        refused = _raises(SystemExit, ensure_install, local_layout, False)
        check.ok("a mismatched install script is refused before bash runs",
                 refused and not ran.exists() and not install_layout.launcher.exists())
        saved_pin = HERMES_INSTALL_SHA256
        try:
            globals()["HERMES_INSTALL_SHA256"] = sha256(script_path.read_bytes())
            matched = ensure_install(local_layout, False)
        finally:
            globals()["HERMES_INSTALL_SHA256"] = saved_pin
        check.ok("a matching install script reaches bash and installs",
                 matched and ran.is_file() and install_layout.launcher.is_file())

        # ODL-514: the seed key is not the release key. The certificate walks
        # release key -> seed key -> seed body; the key being retired is
        # accepted for one release, a third key is not.
        pairs = {}
        for name in ("release", "prev", "seed", "third"):
            private = root / f"{name}.pem"
            public = root / f"{name}.pub"
            made = run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private)])
            if made.returncode == 0:
                os.chmod(private, 0o600)
                made = run(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)])
            if made.returncode == 0 and public.is_file():
                pairs[name] = (private, public)
        check.ok("the four seed-certificate fixture keys were generated", len(pairs) == 4)
        if len(pairs) == 4:
            seed_pem = pairs["seed"][1].read_bytes()

            def certificate(signer: str, out: Path) -> bytes:
                made = run(["openssl", "dgst", "-sign", str(pairs[signer][0]), "-out", str(out),
                            str(pairs["seed"][1])])
                return out.read_bytes() if made.returncode == 0 else b""

            def seed_body(path: Path, cert: bytes | None) -> tuple[bytes, bytes]:
                document = {
                    "slug": "odl-test", "instance_id": "srv-selftest",
                    "near_api_key": "near-x", "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
                    "support_public_key": "ssh-ed25519 AAAA test@odile.fyi",
                    "backup_repo_url": "sftp:odile-backup@backup.odile.fyi:/tenants/odl-test",
                    "hermes_version": "2026.11.1",
                    "mail_relay": {"host": "smtp.mail.odile.fyi", "port": 587,
                                   "username": "odl-test@mail.odile.fyi", "secret": "s"},
                }
                if cert is not None:
                    document["signing_key_sha256"] = sha256(seed_pem)
                    document["signing_key"] = seed_pem.decode("ascii")
                    document["signing_key_cert"] = base64.b64encode(cert).decode("ascii")
                path.write_bytes(json.dumps(document, sort_keys=True,
                                            separators=(",", ":")).encode() + b"\n")
                signature = path.with_suffix(".sig")
                run(["openssl", "dgst", "-sign", str(pairs["seed"][0]), "-out", str(signature),
                     str(path)])
                return path.read_bytes(), signature.read_bytes()

            def refusal(func, *args) -> str:
                try:
                    func(*args)
                except SystemExit as exc:
                    return str(exc)
                return ""

            current, retired = pairs["release"][1], pairs["prev"][1]
            body, signature = seed_body(root / "current.json", certificate("release", root / "c.crt"))
            parsed, certifier = verify_seed_body(body, signature, current)
            check.ok("a seed certified by the current release key verifies",
                     certifier == str(current) and
                     parsed["signing_key_sha256"] == sha256(seed_pem))
            body_stale, signature_stale = seed_body(
                root / "retired.json", certificate("prev", root / "p.crt"))
            parsed_stale = json.loads(body_stale.decode("ascii"))
            check.ok("the key being retired certifies a seed too",
                     verify_bytes(body_stale, signature_stale, pem_path(seed_pem)) and
                     certify_seed_key(parsed_stale, [current, retired]) == retired)
            body_third, _ = seed_body(root / "third.json", certificate("third", root / "t.crt"))
            parsed_third = json.loads(body_third.decode("ascii"))
            reason = refusal(certify_seed_key, parsed_third, [current, retired])
            check.ok("a certificate signed by a third key is refused on one line",
                     reason.startswith("error: ") and "\n" not in reason, reason)
            legacy = root / "legacy.json"
            seed_body(legacy, None)
            # The legacy body is signed by the seed key above; sign it with the
            # release key to prove the pre-ODL-514 shape still verifies.
            run(["openssl", "dgst", "-sign", str(pairs["release"][0]),
                 "-out", str(legacy.with_suffix(".sig")), str(legacy)])
            legacy_signature = legacy.with_suffix(".sig").read_bytes()
            legacy_parsed, legacy_certifier = verify_seed_body(
                legacy.read_bytes(), legacy_signature, current)
            check.ok("a seed with no certificate still verifies against the release key",
                     legacy_certifier == str(current) and "signing_key_sha256" not in legacy_parsed)
            tampered = legacy.read_bytes().replace(b'"slug":"odl-test"', b'"slug":"odl-evil"')
            check.ok("a tampered seed body is refused",
                     refusal(verify_seed_body, tampered, legacy_signature, current) != "")
            beside = seed_key_candidates(current)
            own = INSTALLER_PATH.parent / "odile-signing-key.pub"
            check.ok("the installer's own key joins the kit's key as a candidate",
                     current in beside and len(set(beside)) == len(beside)
                     and (not own.is_file() or own in beside))

        # tmux argv
        command = tmux_command(INSTALLER_PATH, ["--recovery-kit", "/root/kit.json"])
        joined = " ".join(command)
        check.ok("tmux re-execs with --stage apply",
                 "--stage" in joined and "apply" in joined and "odile-install" in joined)

        # certificate fingerprint parser (self-signed fixture, written the way
        # certmagic writes a store file: the leaf, then its issuer chain)
        cert = root / "cert.pem"
        cert_key = root / "cert.key"
        generated = run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                         "-keyout", str(cert_key), "-out", str(cert), "-days", "1",
                         "-subj", "/CN=odl-test.odile.fyi"])
        issuer = root / "issuer.pem"
        issuer_key = root / "issuer.key"
        made_issuer = run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                           "-keyout", str(issuer_key), "-out", str(issuer), "-days", "1",
                           "-subj", "/CN=Odile Test Issuer",
                           "-addext", "basicConstraints=critical,CA:TRUE"])
        if generated.returncode == 0:
            expected = run(["openssl", "x509", "-noout", "-fingerprint", "-sha256", "-in", str(cert)])
            printed = re.search(r"=\s*([0-9A-Fa-f:]+)", expected.stdout)
            caddy_root = layout.path("/var/lib/caddy/.local/share/caddy/certificates")
            target = caddy_root / "acme" / "odl-test.odile.fyi" / "odl-test.odile.fyi.crt"
            target.parent.mkdir(parents=True, exist_ok=True)
            bundle = cert.read_bytes()
            if made_issuer.returncode == 0:
                bundle += issuer.read_bytes()
            target.write_bytes(bundle)
            got = certificate_fingerprint(layout, "odl-test.odile.fyi")
            want = "sha256:" + printed.group(1).replace(":", "").lower() if printed else ""
            check.ok("the certificate fingerprint is SHA-256 and matches openssl", got == want,
                     f"got {got!r}")
            if made_issuer.returncode == 0:
                chained = "sha256:" + sha256(ssl.PEM_cert_to_DER_cert(bundle.decode("utf-8")))
                check.ok("a store file with the issuer chain pins the leaf, not the chain",
                         got == want and got != chained,
                         f"got {got!r}, whole-bundle {chained!r}")
        else:
            print("skip  certificate fingerprint: openssl req failed")

    return check.summary()


def _raises(exc_type, func, *args, **kwargs) -> bool:
    try:
        func(*args, **kwargs)
    except exc_type:
        return True
    except BaseException:
        return False
    return False


# --------------------------------------------------------------------------
# command line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Odile tenant installer (ODL-203). One signed command, run as root.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--self-test", action="store_true", help="test the pure functions; change nothing")
    parser.add_argument("--verify-signature", action="store_true",
                        help="print the signing key's sha256 and fingerprint, verify this file "
                             "against --pubkey/--signature, and exit")
    parser.add_argument("--print-source", action="store_true", help="print this program for audit")
    parser.add_argument("--manifest", action="store_true", help="print the static manifest as JSON")
    parser.add_argument("-n", "--dry-run", action="store_true", help="show the plan, change nothing")
    parser.add_argument("--root", help="simulate into this scratch root (implies unprivileged mode)")
    parser.add_argument("--user", help="OS user (tenant default: odile; simulation default: you)")
    parser.add_argument("--port", type=int, help=f"Webapp port (tenant {DEFAULT_TENANT_PORT}, simulation {DEFAULT_SCRATCH_PORT})")
    parser.add_argument("--slug", help="tenant slug, if it is not in the recovery kit")
    parser.add_argument("--seed-url", help=f"seed endpoint (default {DEFAULT_SEED_URL})")
    parser.add_argument("--recovery-kit",
                        help="JSON kit: setup token, instance id, signing public key, "
                             "signing_key_sha256")
    parser.add_argument("--boot-report",
                        help=f"the boot report bb-seed copied (tenant default {BOOT_REPORT_DEFAULT})")
    parser.add_argument("--expect", help="the 96-hex measurement the verifier printed "
                                         "(required when no boot record exists)")
    parser.add_argument("--release", help="assert the boot report's release id")
    parser.add_argument("--token", help="setup token (64 hex), if not using a kit file")
    parser.add_argument("--instance-id", help="Servury instance id, if not using a kit file")
    parser.add_argument("--pubkey", help="Ed25519 public key (PEM) outside this file")
    parser.add_argument("--signature", help="detached signature (default: this file + .sig)")
    parser.add_argument("--signin-password", help="the Odile sign-in password (16+); blank generates 32")
    parser.add_argument("--first-visit", action="store_true",
                        help="set the sign-in on the guest at first visit (no prompt; the "
                             "Webapp stays shut until the page writes the hash)")
    parser.add_argument("--first-visit-token",
                        help="the one-time token that gates the page (default: the kit's "
                             "setup_token). Stored on the guest as SHA-256 only")
    parser.add_argument("--first-visit-port", type=int,
                        help=f"first-visit service loopback port (tenant default {FIRST_VISIT_PORT})")
    parser.add_argument("--signin-username", default="odile", help="the Odile sign-in name (default odile)")
    parser.add_argument("--backup-password", help="backup password for the recovery file (16+); blank generates 32")
    parser.add_argument("--heartbeat-url", default=HEARTBEAT_URL,
                        help=f"where the nightly unit posts its signed heartbeat "
                             f"(default {HEARTBEAT_URL})")
    parser.add_argument("--public-url", help="override the Webapp public URL")
    parser.add_argument("--hermes-commit", default=HERMES_COMMIT, help="pinned Hermes commit")
    parser.add_argument("--no-tmux", action="store_true", help="run the long install inline")
    parser.add_argument("--i-have-copied-it", action="store_true",
                        help=f'confirm the recovery file was copied off (phrase: "{CONFIRM_PHRASE}")')
    parser.add_argument("--force-smoke", action="store_true", help="run the paid smoke turn again")
    parser.add_argument("--json", action="store_true", help="print a machine-readable summary at the end")
    parser.add_argument("--stage", choices=["apply"], help=argparse.SUPPRESS)
    return parser


def resolve_layout(args: argparse.Namespace) -> Layout:
    if args.root:
        root = Path(args.root).resolve()
        user = args.user or pwd.getpwuid(os.geteuid()).pw_name
        port = args.port or DEFAULT_SCRATCH_PORT
        public_url = args.public_url or f"http://odile-sim.test:{port}"
        return Layout.scratch(root=root, user=user, webapp_port=port,
                              public_url=public_url,
                              commit=args.hermes_commit,
                              first_visit_port=args.first_visit_port or FIRST_VISIT_PORT)
    user = args.user or "odile"
    port = args.port or DEFAULT_TENANT_PORT
    slug = args.slug or "<slug>"
    public_url = args.public_url or f"https://{slug}.odile.fyi"
    return Layout.tenant(user=user, webapp_port=port,
                        public_url=public_url, commit=args.hermes_commit,
                        first_visit_port=args.first_visit_port or FIRST_VISIT_PORT)


def dry_run(args: argparse.Namespace) -> int:
    layout = resolve_layout(args)
    kit = load_recovery_kit(Path(args.recovery_kit) if args.recovery_kit else None, args)
    slug = kit.get("slug") or args.slug or "<slug>"
    instance = kit.get("instance_id") or args.instance_id or "<instance>"
    pubkey = kit_pubkey(kit) or (Path(args.pubkey) if args.pubkey else None)
    print(f"odile installer {VERSION} — tenant plan (dry run, {layout.mode})")
    print(f"             sha256 {sha256(INSTALLER_PATH.read_bytes())}")
    print(f"             tenant {slug} · instance {instance}")
    if pubkey:
        print(f"would verify signature against {pubkey}")
    else:
        print("would verify signature against the recovery kit's public key")
    print(f"would write  {layout.installer_copy} (this program's exact bytes, mode 0755, "
          f"sha256 {sha256(INSTALLER_PATH.read_bytes())[:16]}…)")
    boot_report = Path(args.boot_report) if args.boot_report else layout.path(BOOT_REPORT_DEFAULT)
    print(f"would check  the boot report {boot_report} against --expect "
          f"(record {layout.boot_record})")
    print(f"would fetch  seed from {args.seed_url or (SCRATCH_SEED_URL if layout.mode == 'scratch' else DEFAULT_SEED_URL)}")
    if layout.mode == "tenant":
        print(f"would install OS packages from the pinned snapshot {OS_SNAPSHOT} "
              f"(Debian 13 {OS_SUITE}):")
        for line in wrapped_terms(apt_package_specs()):
            print(f"             {line}")
        for name, version in OS_OPTIONAL_PINS.items():
            print(f"             {name}={version} (best effort)")
        print(f"would create OS user {layout.user} (NOPASSWD sudo) and the {MANIFEST_GROUP} group")
    else:
        print(f"skip  OS packages and users (simulation runs as {layout.user})")
    print(f"would install Hermes {HERMES_VERSION_PREFIX} at {layout.hermes_commit[:12]} as {layout.user}")
    print(f"would fetch {layout.installer_url} and require sha256 {HERMES_INSTALL_SHA256[:12]}…")
    print("would build the Webapp bundle (first run only)")
    print(f"would write {layout.hermes_home}/config.yaml ({len(MANAGED_KEYS)} managed blocks; "
          f"public_url {layout.webapp_public_url})")
    print(f"would write {layout.home}/AGENTS.md and {len(TENANT_SKILLS)} embedded skill(s) "
          f"under {layout.skills}")
    print(f"would write {layout.hermes_home}/skins/{SKIN_NAME}.yaml and display.skin {SKIN_NAME} "
          "(paper and ink; other display keys kept)")
    print(f"would link the Webapp brand script ({BRAND_ASSET}) from {layout.webapp_bundle}")
    print(f"would write {layout.hermes_home}/.env (3 allowlisted keys, mode 0600)")
    print(f"would write {layout.key_env} (the seed's per-user NEAR key, 0600 root:root) and "
          f"EnvironmentFile= on {layout.unit_target}")
    if args.first_visit:
        print(f"would write a placeholder sign-in hash; the first-visit page at "
              f"https://{slug}.odile.fyi/first-visit sets the real one")
        print(f"would write {layout.first_visit_source}, {layout.first_visit_page}, "
              f"{layout.first_visit_state} (token sha256 only) and {layout.first_visit_unit}")
        print(f"would listen on 127.0.0.1:{layout.first_visit_port} and proxy /first-visit* to it")
        print(f"would write {layout.verify_page}, {layout.access_report_source}, "
              f"{layout.verify_unlock_source} and {layout.access_report_unit}")
        print(f"would write {layout.access_report_file} (the machine's own report) "
              f"and proxy /verify to the same loopback service")
    print(f"would write the agent unit {layout.unit_target} (EnvironmentFile={layout.key_env})")
    print(f"would write the backup program {layout.backup_source}, {layout.backup_service} "
          f"and {layout.backup_timer} (daily 03:00; heartbeat {args.heartbeat_url})")
    print(f"would write the boot-report transformer {layout.attest_source}, its oneshot unit, "
          f"{layout.served_attestation} and the public /attestation route")
    print(f"would splice {layout.caddyfile} (# BEGIN odile-{slug})")
    if layout.mode == "tenant":
        print("would lock root, set PasswordAuthentication no, and write /etc/sudoers.d/odile")
        print(f"would wait for DNS and retry ACME for {slug}.odile.fyi, then print the fingerprint")
    else:
        print("skip  DNS/ACME/sshd/root lock (the simulation has no public listener)")
    print(f"would write {layout.recovery} (backup password, certificate fingerprint placeholder)")
    print('would require the recovery-file confirmation ("I have copied it off the machine")')
    if args.first_visit:
        print("would run smoke: the workspace is shut (302/401), one chat turn straight "
              "to NEAR AI, one shell command, one file write, one cron job")
    else:
        print("would run smoke: sign-in 302/401/200, one chat turn straight to NEAR AI, "
              "one shell command, one file write, one cron job")
    print("dry run: nothing written, no service started")
    return 0


def manifest() -> int:
    print(json.dumps({
        "name": "odile_install",
        "version": VERSION,
        "installer_sha256": sha256(INSTALLER_PATH.read_bytes()),
        "near_key_env": NEAR_KEY_ENV,
        "attest_sha256": ATTEST_SHA256,
        "backup_sha256": BACKUP_SHA256,
        "first_visit_sha256": FIRST_VISIT_SHA256,
        "first_visit_page_sha256": FIRST_VISIT_PAGE_SHA256,
        "verify_page_sha256": VERIFY_PAGE_SHA256,
        "access_report_script_sha256": ACCESS_REPORT_SCRIPT_SHA256,
        "verify_unlock_sha256": VERIFY_UNLOCK_SHA256,
        "audit_sha256": AUDIT_SCRIPT_SHA256,
        "hermes_commit": HERMES_COMMIT,
        "hermes_install_sha256": HERMES_INSTALL_SHA256,
        "hermes_version": HERMES_VERSION_PREFIX,
        "managed_config_keys": list(MANAGED_KEYS),
        "env_allowlist": list(TENANT_ENV_KEYS),
        "tenant_files": [
            {"target": item.target, "source": item.source, "sha256": item.sha256}
            for item in validate_tenant_files(tenant_files())
        ],
        "skin": SKIN_NAME,
        "skin_sha256": sha256(SKIN_YAML.encode("utf-8")),
        "webapp_brand_sha256": sha256(BRAND_JS.encode("utf-8")),
        "os_suite": OS_SUITE,
        "os_snapshot": OS_SNAPSHOT,
        "os_snapshot_url": f"{OS_SNAPSHOT_BASE}/{OS_SNAPSHOT}",
        "os_packages": dict(OS_PACKAGE_PINS),
        "os_optional_packages": dict(OS_OPTIONAL_PINS),
    }, indent=2, sort_keys=True))
    return 0


def verify_cli(args: argparse.Namespace) -> int:
    signature = Path(args.signature) if args.signature else INSTALLER_PATH.with_name(
        INSTALLER_PATH.name + ".sig")
    pubkey = Path(args.pubkey) if args.pubkey else INSTALLER_PATH.parent / "odile-signing-key.pub"
    print_signing_key(pubkey)
    ok, detail = verify_installer(INSTALLER_PATH, signature, pubkey)
    print(f"signature: {'OK' if ok else 'BAD'} ({detail})")
    return 0 if ok else 3


def verify_or_die(args: argparse.Namespace, kit: dict) -> Path:
    signature = Path(args.signature) if args.signature else INSTALLER_PATH.with_name(
        INSTALLER_PATH.name + ".sig")
    pubkey = kit_pubkey(kit)
    if pubkey is None:
        candidate = Path(args.pubkey) if args.pubkey else INSTALLER_PATH.parent / "odile-signing-key.pub"
        pubkey = candidate
    digest = print_signing_key(pubkey)
    problem = signing_key_problem(kit, digest)
    if problem:
        print(
            "error: the recovery kit names a different signing key.\n"
            f"       {problem}\n"
            "       Do not install: re-download the kit and the signed installer from Odile.",
            file=sys.stderr,
        )
        raise SystemExit(3)
    ok, detail = verify_installer(INSTALLER_PATH, signature, pubkey)
    print(f"signature    {'OK' if ok else 'BAD'} ({detail})")
    if not ok:
        print(
            "error: the installer's signature does not verify against the recovery kit key.\n"
            "       Do not run a modified installer. Re-download the signed copy.",
            file=sys.stderr,
        )
        raise SystemExit(3)
    return pubkey


def tenant_main(args: argparse.Namespace) -> int:
    layout = resolve_layout(args)
    if layout.mode == "tenant" and os.geteuid() != 0:
        print("error: a tenant install needs root; use --root for the simulation", file=sys.stderr)
        return 2
    started = time.monotonic()
    print(f"odile installer {VERSION} — {'simulation' if layout.mode == 'scratch' else 'tenant'} "
          f"· sha256 {sha256(INSTALLER_PATH.read_bytes())[:16]}…")
    print(f"             user {layout.user} · webapp 127.0.0.1:{layout.webapp_port}")

    kit = load_recovery_kit(Path(args.recovery_kit) if args.recovery_kit else None, args)
    pubkey = verify_or_die(args, kit)
    # ODL-332 follow-up: `--manifest` and docs/CLAIMS.md §4.6 name
    # /usr/local/bin/odile-install.sh, so the verified bytes go there before
    # the boot record — which carries installer_sha256 — is written.
    changed = ensure_installer_copy(layout, args.dry_run)
    # The boot report is asserted before the seed exchange: a mismatch exits 5
    # with the token unspent and no secret written (PIN-POLICY §5, §7).
    verdict = check_boot(layout, args, kit)
    slug = kit.get("slug") or args.slug
    seed_url = args.seed_url or (SCRATCH_SEED_URL if layout.mode == "scratch" else DEFAULT_SEED_URL)
    args.seed_url = seed_url

    seed = load_or_fetch_seed(layout, kit, args, pubkey)
    slug = seed["slug"]
    if layout.webapp_public_url.endswith("<slug>.odile.fyi") or layout.mode == "tenant":
        layout = dataclasses.replace(layout, webapp_public_url=(
            args.public_url or f"https://{slug}.odile.fyi"))
    print(f"seed         {slug} · instance {seed['instance_id']} · model {seed['model']}")

    changed |= ensure_packages(layout, args.dry_run)
    changed |= ensure_user(layout, args.dry_run)
    changed |= ensure_manifest_group(layout, args.dry_run)
    changed |= ensure_dirs(layout, args.dry_run)
    changed |= ensure_attest_source(layout, args.dry_run)
    changed |= ensure_backup_source(layout, args.dry_run)
    changed |= ensure_key_env(layout, seed, args.dry_run)
    changed |= ensure_pin_files(layout, seed, args.dry_run)
    changed |= ensure_install(layout, args.dry_run)
    changed |= ensure_webapp_build(layout, args.dry_run)
    changed |= ensure_webapp_brand(layout, args.dry_run)
    changed |= ensure_config(layout, seed, args.dry_run)
    changed |= ensure_skin(layout, args.dry_run)
    env_changed, password = ensure_env(layout, args.signin_password, args.signin_username,
                                       args.dry_run, first_visit=args.first_visit)
    changed |= env_changed
    first_visit_token = (args.first_visit_token or kit.get("setup_token")
                         or args.token or "")
    first_visit_enabled = bool(args.first_visit) or layout.first_visit_state.is_file()
    if first_visit_enabled:
        changed |= ensure_first_visit(layout, first_visit_token, args.dry_run)
        changed |= ensure_verify_surface(layout, args.dry_run,
                                         verdict.boot_key_b64 if verdict else "")
    units_changed = ensure_units(layout, slug, args.dry_run, first_visit=first_visit_enabled,
                                 heartbeat_url=args.heartbeat_url)
    changed |= units_changed
    changed |= ensure_sudoers(layout, args.dry_run)
    changed |= ensure_sshd(layout, args.dry_run)
    changed |= ensure_workspace(layout, slug, args.dry_run)

    if layout.mode == "scratch":
        changed |= ensure_scratch_attest(layout, args.dry_run)
        changed |= ensure_scratch_services(layout, args.dry_run)
    else:
        changed |= ensure_systemd_services(layout, args.dry_run, units_changed)
    # After the daemon reload the branch above may have done: the nightly timer
    # is enabled now, and a converged tenant prints `same` (ODL-215 follow-up).
    changed |= ensure_backup_timer(layout, args.dry_run)

    username = str(args.signin_username or "odile")
    if (layout.hermes_home / ".env").is_file():
        values = env_values((layout.hermes_home / ".env").read_text(encoding="utf-8"))
        username = values.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", username)

    changed |= ensure_caddy_site(layout, slug, args.dry_run)

    fingerprint = None
    if layout.mode == "tenant" and not args.dry_run:
        if not wait_for_dns(f"{slug}.odile.fyi"):
            raise SystemExit(f"error: {slug}.odile.fyi did not resolve within 15 minutes")
        if not wait_for_acme(f"{slug}.odile.fyi"):
            raise SystemExit(f"error: no valid certificate for {slug}.odile.fyi within 15 minutes")
        fingerprint = certificate_fingerprint(layout, f"{slug}.odile.fyi")
        print(f"certificate  {fingerprint or '(not found in Caddy store)'}")

    smoke_ok = run_smoke(layout, username, password, args.dry_run, args.force_smoke,
                         first_visit=args.first_visit)

    changed |= ensure_recovery(layout, slug, seed["instance_id"], fingerprint,
                               args.dry_run, args.backup_password)[0]
    if not args.dry_run:
        if not confirm_recovery(args):
            return 4

    elapsed = int(time.monotonic() - started)
    summary = {
        "ok": True, "mode": layout.mode, "slug": slug,
        "instance_id": seed["instance_id"], "changed": bool(changed),
        "smoke": bool(smoke_ok), "elapsed_s": elapsed,
        "installer_sha256": sha256(INSTALLER_PATH.read_bytes()),
    }
    print(f"elapsed      {elapsed // 60}m {elapsed % 60}s")
    print(f"tenant: {'provisioned' if changed else 'same'}")
    if args.json:
        print(json.dumps(summary, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.first_visit and args.signin_password:
        print("error: --first-visit and --signin-password are mutually exclusive: the "
              "first-visit page sets the password, the installer never learns it",
              file=sys.stderr)
        return 2
    if args.self_test:
        return self_test()
    if args.verify_signature:
        return verify_cli(args)
    if args.print_source:
        sys.stdout.write(INSTALLER_PATH.read_text(encoding="utf-8"))
        return 0
    if args.manifest:
        return manifest()
    if args.dry_run:
        return dry_run(args)
    tmux_result = maybe_tmux(args)
    if tmux_result is not None:
        return tmux_result
    return tenant_main(args)


if __name__ == "__main__":
    sys.exit(main())
