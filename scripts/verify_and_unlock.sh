#!/usr/bin/env bash
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
