#!/usr/bin/python3
"""odile-firstboot — the measured image installs itself (ODL-456).

Runs once per boot from `odile-firstboot.service`, before the tenant unit.
The per-tenant facts arrive in the measured command line — the one per-tenant
input the sealed design has (docs/product/DECISION-agent-access.md §3a):

    bb.iid=<instance id>            Servury's instance id (the binding)
    odile.token=<64 lowercase hex>  the one-time setup token (ODL-455)
    odile.seed_url=<url>            the seed endpoint (default https://odile.fyi/seed);
                                    its host names the environment this machine
                                    reports back to (the boot record posts here)
    odile.public_url=<url>          the name this tenant serves, read by the
                                    release setup (ODL-340 follow-up); absent on
                                    production, where the installer derives
                                    `<slug>.<the seed host>` instead

The token is the badge, not a standing key: it is minted at provision bound to
`bb.iid`, valid once and for 24 hours. The command line is public, so the
token is public while it lives; single use and the instance binding are what
keep it from seeding another machine. (The residual: a host that reads the
panel's command line before the guest boots can race it to /seed. That is
recorded in docs/runbooks/image.md §13.)

What this program does, in order:

1. generates this machine's own SSH host keys with `ssh-keygen -A` when the
   image shipped none (ODL-523: the build removes debootstrap's
   /etc/ssh/ssh_host_*, and Debian's sshd-keygen.service cannot make new ones
   on a cloned disk because it carries ConditionFirstBoot=yes). It runs before
   the boot record is read, so the first boot's record carries this machine's
   key (ODL-398);
1b. re-derives the guest hostname from `bb.host`: `/etc/hostname` and the
   image's own `/etc/hosts` lines (the two that name the machine and
   `tenant.odile.test`) are set to the measured slug, and the running kernel
   hostname is set when the unit asks for it. The image build bakes its own
   `GUEST_NAME` (`deploy/image/sealed-image.sh`), which is the *build* tenant's
   slug — on a recycled machine that is the previous tenant's name at the getty
   prompt. A converged boot reads `same` and writes nothing; `--check` never
   writes (ODL-348 follow-up);
2. places the release's exact installer at /usr/local/bin/odile-install.sh
   (0755 root), the signed manifest pair at /etc/odile/manifest.json/.sig and
   the listener that serves them — from the image's own copy of the release,
   verifying the pair before it writes (ODL-472). The placement is idempotent:
   a converged machine reads `same` for every byte in the installer's log;
3. reads the state at /var/lib/odile/firstboot.json; a machine that already
   installed and still holds the seed cache prints exactly `odile-firstboot:
   same` and exits (a second boot does not reinstall and does not spend a
   token);
4. without a token or an instance id it prints one fail-closed line and exits
   0 — the unit is oneshot, so the rest of the boot continues and the machine
   stays reachable;
5. POSTs {token, instance_id} to the seed endpoint, requires the
   X-Odile-Signature Ed25519 signature over the exact response bytes; a seed
   carries the seed key's public half and the release key's certificate over
   it, so the body is checked against the certified seed key after the
   certificate verifies against the release public key baked into the image at
   /etc/odile/odile-signing-key.pub (or the `.prev.pub` beside it for the
   release that retires a key). A seed issued before ODL-514 carries no
   certificate and is checked against the release key itself. A body that is
   not the tenant's (slug / instance / keys) is refused the same way;
6. writes the seed to /etc/odile/seed.json (0600, atomic), then runs the
   image's installer with the seed:
       install-stack.sh --seed /etc/odile/seed.json --first-visit-token <token>
   The installer converges the stack (`same` for everything the build already
   did), hands the sign-in hash in the tenant .env to Hermes basic auth, writes
   /etc/odile/first-visit.json (the token's SHA-256, never the token) and
   starts the first-visit page;
7. asserts the first-visit page answers through loopback, then prints one
   line: `odile-firstboot: installed <slug> · first-visit <status>`.

Exactly one `odile-firstboot:` line reaches stdout — the serial console. On
every boot that runs the steps above, one `odile-hostkeys:` line stands in
front of it: the key this boot generated or kept, which is what lets a reader
of the transcript see that the key existed before the boot record was read.
Nothing else goes to the console; the installer's own output goes to
/var/log/odile-firstboot.log.

No passphrase, private key or operator key is read, written or printed.
Standard library only.

    /usr/bin/python3 odile-firstboot.py                 # the unit's call
    /usr/bin/python3 odile-firstboot.py --print-state    # secret-free summary
    /usr/bin/python3 odile-firstboot.py --check          # exit 0 if installed
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

VERSION = "1.0.0"

STATE_PATH = "/var/lib/odile/firstboot.json"
SEED_CACHE = "/etc/odile/seed.json"
SIGNING_KEY = "/etc/odile/odile-signing-key.pub"
FIRST_VISIT_STATE = "/etc/odile/first-visit.json"
LOG_PATH = "/var/log/odile-firstboot.log"
INSTALLER = "/usr/local/lib/odile-image/install-stack.sh"
CMDLINE = "/proc/cmdline"
DEFAULT_SEED_URL = "https://odile.fyi/seed"
FIRST_VISIT_PORT = 8091

# ODL-523: the image ships no SSH host private key; this boot makes the
# machine's own. `ssh-keygen -A -f <root>` reads and writes <root>/etc/ssh, so
# "/" is the image and a test passes its own root so the host's keys are never
# touched. HOST_KEY_KINDS is what `-A` generates (sshd's three defaults).
SSH_ROOT = "/"
SSH_KEYGEN = "ssh-keygen"
HOST_KEY_KINDS = ("rsa", "ecdsa", "ed25519")

# The measured facts the boot record is built from (ODL-398 follow-up). The
# key is the one in the measured line; the release is the image pin's, the one
# the machine was built to boot (ODL-398's `provision_release` vocabulary). The
# pin is placed inside the production disk by place-pin.sh; the installer's own
# boot record is the fallback for an image built before that.
PIN_PATH = "/etc/odile/tenant-image.pin"
# The boot's own report: Servury's bb-seed copies it before switch-root, and
# its `release` is the image release this boot measured. It is the first
# source, because the image pin can be older than the disk's release after a
# recycle: the row HQ keeps must name what the machine installed (ODL-947).
BOOT_REPORT_PATH = "/run/blackbox/attest.json"
BOOT_KEY_PATH = "/etc/odile/boot-key.pub"
BOOT_RECORD_PATH = "/etc/odile/boot-record.json"
HOST_KEY_PATH = "/etc/ssh/ssh_host_ed25519_key.pub"
# ODL-348 follow-up: the build's own name is the *build* tenant's slug, so a
# recycled machine would show the previous tenant at the prompt. The boot line
# is the only per-tenant input; `/etc/hosts` is rewritten only on the two lines
# the image itself writes, recognised by this marker domain
# (`deploy/image/sealed-image.sh` writes both with `tenant.odile.test`).
HOSTNAME_PATH = "/etc/hostname"
HOSTS_PATH = "/etc/hosts"
IMAGE_HOST_MARKER = "tenant.odile.test"
RELEASE_ID_RE = re.compile(r"^[0-9]{4}\.[0-9]{2}(?:\.[0-9]+)?(?:-[0-9]+)?$")
KEY_TYPES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)

# The image's release/ directory (ODL-472): the same installer, signed
# manifest pair and listener the release serves, copied into the image by
# sealed-image.sh. The first boot places them where CLAIMS 4.6 and the
# manifest route name them, every boot, idempotently.
RELEASE_DIR = "/usr/local/lib/odile-image"
INSTALLER_COPY = "/usr/local/bin/odile-install.sh"
ETC_DIR = "/etc/odile"
ODILE_LIB = "/usr/local/lib/odile"
MANIFEST_UNIT = "/etc/systemd/system/odile-manifest.service"
MANIFEST_SERVICE = "odile-manifest.service"
SYSTEMD_DIR = "/run/systemd/system"

SEED_KEYS = (
    "slug", "instance_id", "near_api_key", "model", "support_public_key",
    "backup_repo_url", "mail_relay", "hermes_version",
    # B-once (ODL-925): the account's scrypt verifier, which the seeded first
    # boot writes into the tenant .env so the first visit is a login.
    "sign_in_hash",
)
SEED_FORBIDDEN = ("password", "passphrase", "private_key", "ssh_private_key", "luks")
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
# The installer's own scrypt format (installer/odile_install.py's SCRYPT_RE);
# the seed's sign_in_hash is written into the tenant .env byte for byte.
SCRYPT_SEED_RE = re.compile(
    r"^scrypt\$\d+\$\d+\$\d+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+$")
# What the log line prints in place of the setup token (ODL-579): the argv is
# echoed for the transcript, but the token is a bearer while it lives.
TOKEN_REDACTED = "<redacted>"
INSTANCE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
SEED_URL_RE = re.compile(r"^https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{1,512}$")
SIGNATURE_PREFIX = "ed25519="

MAX_SEED_BYTES = 64 * 1024
TIMEOUT_SECONDS = 30.0
ATTEMPTS = 3
RETRY_PAUSE = 5.0


class Refusal(Exception):
    """A fail-closed reason: one line to the console, no state change."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------- helpers

def say(line: str) -> None:
    """The one console line. Nothing else goes to stdout."""
    print("odile-firstboot: " + line, flush=True)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def read_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def parse_cmdline(text: str) -> dict[str, str]:
    """The `key=value` tokens of a kernel command line; the last one wins."""
    tokens: dict[str, str] = {}
    for word in text.split():
        if "=" not in word:
            continue
        key, value = word.split("=", 1)
        if key:
            tokens[key] = value
    return tokens


def cmdline_facts(cmdline: str) -> dict[str, str]:
    """The facts this program needs, or what is missing, as strings."""
    tokens = parse_cmdline(cmdline)
    instance = tokens.get("bb.iid", "").strip()
    token = tokens.get("odile.token", "").strip()
    seed_url = tokens.get("odile.seed_url", "").strip() or DEFAULT_SEED_URL
    hostname = tokens.get("bb.host", "").strip()
    return {"instance_id": instance, "token": token, "seed_url": seed_url,
            "hostname": hostname}


def rewrite_hosts(text: str, name: str, stale: str = "") -> str:
    """The image's own `/etc/hosts` lines, naming `name` instead of the old one.

    Only the two lines the image writes are touched: they carry the marker
    domain `tenant.odile.test` (`deploy/image/sealed-image.sh`), so the host
    field before the marker is this image's own name. `stale` (the name
    `/etc/hostname` carried when this function was called) is replaced wherever
    it appears as a whole field, which also catches a line an operator added
    the old name to. Every other line, and their spacing and order, is kept.
    """
    out: list[str] = []
    for raw in text.splitlines(keepends=True):
        newline = ""
        body = raw
        if raw.endswith("\n"):
            body, newline = raw[:-1], "\n"
        fields = body.split()
        if not fields or fields[0].startswith("#"):
            out.append(raw)
            continue
        new = list(fields)
        if stale and stale != name:
            new = [name if field == stale else field for field in new]
        for index, field in enumerate(new):
            # address, then at least one host field, before the marker.
            if field == IMAGE_HOST_MARKER and index >= 2:
                new[index - 1] = name
        out.append((" ".join(new) if new != fields else body) + newline)
    return "".join(out)


def ensure_hostname(name: str, *, hostname_path: str = HOSTNAME_PATH,
                    hosts_path: str = HOSTS_PATH,
                    set_kernel=None) -> tuple[str, str]:
    """Set the guest's own name from the measured line, once, idempotently.

    Returns (verb, detail) where `verb` is `same` when both files already say
    `name`. A machine booted from the published image says the build tenant's
    slug in `/etc/hostname` and `/etc/hosts`; a booted-and-converged machine
    says its own. `set_kernel` (the unit passes `socket.sethostname`) makes the
    running kernel agree, so `hostname` and the getty prompt are the line's name
    without waiting for a reboot; a read-only probe passes None.
    """
    current = read_text(hostname_path).strip()
    old_hosts = read_text(hosts_path)
    new_hosts = rewrite_hosts(old_hosts, name, stale=current)
    wrote: list[str] = []
    expected = (name + "\n").encode()
    if read_bytes(hostname_path) != expected:
        write_atomic(hostname_path, expected, 0o644)
        wrote.append("file")
    if new_hosts != old_hosts:
        write_atomic(hosts_path, new_hosts.encode(), 0o644)
        wrote.append("hosts")
    kernel = "not-asked"
    if set_kernel is not None:
        try:
            if socket.gethostname() == name:
                kernel = "same"
            else:
                set_kernel(name)
                kernel = "set"
        except OSError as error:
            kernel = "failed:%s" % (error.strerror or error)
    if not wrote:
        verb = "same"
    else:
        verb = "write"
    detail = "%s -> %s (file %s, hosts %s, kernel %s)" % (
        current or "-", name, ",".join(wrote) or "same",
        "same" if new_hosts == old_hosts else "rewritten", kernel)
    return verb, detail


def read_stdin_token(path: str = "/etc/odile/setup-token") -> str:
    """A token written to the disk instead of the command line, if present."""
    try:
        value = read_text(path).strip()
    except OSError:
        return ""
    return value if TOKEN_RE.match(value) else ""


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(path: str, state: dict) -> None:
    write_atomic(path, (json.dumps(state, indent=2, sort_keys=True) + "\n").encode(), 0o600)


def write_atomic(path: str, data: bytes, mode: int) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".odile-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def installed_already(state: dict, instance_id: str, seed_cache: str) -> bool:
    """A finished first boot for this instance, with its seed still on disk."""
    if state.get("ok") is not True or state.get("instance_id") != instance_id:
        return False
    raw = cached_seed_bytes(seed_cache, instance_id)
    return raw is not None and sha256(raw) == state.get("seed_sha256")


def cached_seed_bytes(seed_cache: str, instance_id: str) -> bytes | None:
    """The cached seed's bytes when they are this instance's, else None."""
    try:
        with open(seed_cache, "rb") as handle:
            raw = handle.read(MAX_SEED_BYTES)
    except OSError:
        return None
    try:
        seed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(seed, dict) or seed.get("instance_id") != instance_id:
        return None
    return raw


def first_visit_state(token: str, created_at: str | None = None) -> bytes:
    """ODL-457's state file: the token's SHA-256, and nothing secret.

    Written before the installer runs, so the page it enables has a hash to
    check. A machine that already has the file keeps it: a spent page stays
    spent, and a re-run must not re-open it.

    ``signin_set_at`` is null here and stays null until a writer of the owner's
    sign-in has put it in the tenant ``.env``: the key is a fact about that
    file (``docs/security/DECISION-first-visit-state.md`` §1). The seeded first
    boot's verifier reaches the file inside the installer — ``install-stack.sh``
    writes it, then ``installer.ensure_env`` — so those writers stamp it, not
    this one. Stamping on the seed's mere presence made the installer keep the
    image's placeholder and the page serve a login nobody could pass (ODL-947,
    release 2026.11.23-5). ``first_login_at`` stays null until the owner signs
    in.
    """
    created = created_at or utcnow()
    return (json.dumps({
        "version": 1,
        "token_sha256": sha256(token.encode("ascii")),
        "created_at": created,
        "used_at": None,
        "signin_set_at": None,
        "first_login_at": None,
        "recovery_hash": None,
        "recovery_issued_at": None,
        "recovery_used_at": None,
    }, indent=2, sort_keys=True) + "\n").encode()


def write_first_visit_state(path: str, token: str) -> bool:
    """Write the state once; True when written, False when it already existed."""
    if os.path.exists(path):
        return False
    write_atomic(path, first_visit_state(token), 0o600)
    return True


# ---------------------------------------------------------------- host keys

def host_key_paths(root: str = SSH_ROOT) -> list[str]:
    """The three private host keys `ssh-keygen -A` writes under <root>/etc/ssh."""
    base = os.path.join(root, "etc", "ssh")
    return [os.path.join(base, "ssh_host_%s_key" % kind) for kind in HOST_KEY_KINDS]


def ensure_host_keys(root: str = SSH_ROOT, keygen: str = SSH_KEYGEN) -> tuple[str, str]:
    """Generate this machine's own SSH host keys when the image shipped none.

    ODL-523: the image removes debootstrap's /etc/ssh/ssh_host_* at build time,
    and Debian's sshd-keygen.service cannot put them back on a cloned disk — it
    carries ConditionFirstBoot=yes and the image ships a machine-id. `ssh-keygen
    -A` leaves existing keys alone, so this is idempotent: a second boot reads
    `same` and the key bytes do not move.

    Returns (verb, log line). A missing ssh-keygen is a `warn`, not a refusal:
    the machine still installs, and the sshd-keygen drop-in retries at the first
    ssh start.
    """
    present = [path for path in host_key_paths(root) if os.path.isfile(path)]
    if len(present) == len(HOST_KEY_KINDS):
        return "same", "the SSH host keys (this machine's own, no new key written)"
    # `ssh-keygen -A -f <root>` requires <root>/etc/ssh to exist; the image has
    # it, and a test's fresh root does not.
    try:
        os.makedirs(os.path.join(root, "etc", "ssh"), mode=0o755, exist_ok=True)
    except OSError as error:
        return "warn", "cannot create %s/etc/ssh (%s)" % (root, error.strerror or error)
    try:
        proc = subprocess.run([keygen, "-A", "-f", root],
                              capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return "warn", "ssh-keygen is missing; sshd-keygen.service retries at the next ssh start"
    except (OSError, subprocess.SubprocessError) as error:
        return "warn", "ssh-keygen -A failed (%s)" % error
    if proc.returncode != 0:
        detail = ((proc.stderr or proc.stdout).strip().splitlines() or [""])[0]
        return "warn", "ssh-keygen -A failed (%s)" % (detail or "rc %d" % proc.returncode)
    generated = [path for path in host_key_paths(root) if os.path.isfile(path)]
    if len(generated) != len(HOST_KEY_KINDS):
        return "warn", ("ssh-keygen -A wrote %d of %d host keys; sshd-keygen.service"
                        " retries at the next ssh start" % (len(generated), len(HOST_KEY_KINDS)))
    return "create", "the SSH host keys (%d generated by ssh-keygen -A)" % len(generated)


# ------------------------------------------------------------ the boot record

def fingerprint(blob: bytes) -> str:
    """OpenSSH's SHA256 fingerprint: base64 of the digest, no `=` padding."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def public_key_line(text: str) -> tuple[str, str] | None:
    """One OpenSSH public key line and its fingerprint, or None.

    The measured `bb.key=` is base64 of this line. One line, one known key
    type, one base64 body with key material — the same shape the provider and
    the audit require, so a malformed value is a skipped record, never a guess.
    """
    line = (text or "").strip()
    if not line or len(line.splitlines()) != 1:
        return None
    fields = line.split()
    if len(fields) < 2 or fields[0] not in KEY_TYPES:
        return None
    try:
        blob = base64.b64decode(fields[1], validate=True)
    except (ValueError, TypeError):
        return None
    if len(blob) < 4:
        return None
    declared = int.from_bytes(blob[:4], "big")
    if declared <= 0 or len(blob) < 4 + declared:
        return None
    if blob[4:4 + declared].decode("ascii", "replace") != fields[0]:
        return None
    return line, fingerprint(blob)


def measured_boot_key(cmdline: str, boot_key_path: str = BOOT_KEY_PATH,
                      refresh: bool = True) -> tuple[str, str, str, str]:
    """`(key line, fingerprint, reason, note)` from the measured line.

    The command line is the measured source and wins. `/etc/odile/boot-key.pub`
    is the image's own copy of the bootstrap key — metadata for the verify
    surface, never the source of truth. One disk image is shared across
    machines (one UKI per release, the per-machine line supplied at attach
    time), so on a recycled disk the file names the key of whichever machine
    the disk was built for while the line carries this machine's own `bb.key`.
    The measured key wins and the file is rewritten from it, idempotently: a
    second boot finds the bytes equal and writes nothing. `refresh=False` is
    the read-only probe (`--check`), which must not touch the image.

    A line with **no** `bb.key=` is still refused: the file is the build's
    key, not this boot's, and nothing here measured it.
    """
    values = [word[len("bb.key="):] for word in cmdline.split()
              if word.startswith("bb.key=")]
    from_line = ""
    if len(values) > 1:
        return "", "", "the command line carries %d bb.key= tokens" % len(values), ""
    if not values:
        return "", "", "no bb.key= in the command line", ""
    try:
        from_line = base64.b64decode(values[0], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return "", "", "bb.key= is not base64 of a public key line", ""
    parsed = public_key_line(from_line)
    if parsed is None:
        return "", "", "the measured boot key is not one public key line", ""
    from_file = read_text(boot_key_path).strip() if os.path.isfile(boot_key_path) else ""
    parsed_file = public_key_line(from_file)
    if from_file and parsed_file is None:
        return "", "", "%s is not one public key line" % boot_key_path, ""
    note = ""
    if parsed_file and parsed[0] != parsed_file[0]:
        note = "refreshed %s from the measured line" % boot_key_path
        if refresh:
            try:
                write_atomic(boot_key_path, (parsed[0] + "\n").encode("utf-8"), 0o644)
            except OSError as error:
                return "", "", "could not refresh %s: %s" % (boot_key_path, error), ""
        else:
            note += " (read-only: not written)"
    return parsed[0], parsed[1], "", note


def pinned_release(boot_report_path: str = BOOT_REPORT_PATH,
                   pin_path: str = PIN_PATH,
                   boot_record_path: str = BOOT_RECORD_PATH) -> tuple[str, str]:
    """`(release, reason)` from this boot's own report, else the pin, else the record.

    The boot report names the release the running image measured, so it wins:
    after a recycle the image pin can still name the release the disk was first
    built with, and posting that pin made HQ's row stale while the machine had
    installed the newer release (ODL-947).
    """
    for path, label in ((boot_report_path, "boot report"),
                        (pin_path, "pin"),
                        (boot_record_path, "boot record")):
        value = load_state(path)
        release = value.get("release") or value.get("provision_release")
        if isinstance(release, str) and release.strip():
            chosen = release.strip()
            if RELEASE_ID_RE.match(chosen):
                return chosen, ""
            return "", "the %s release %r is not a release id" % (label, chosen)
    return "", "no pinned release at %s, %s or %s" % (boot_report_path, pin_path,
                                                      boot_record_path)


def current_host_key(path: str = HOST_KEY_PATH) -> str:
    """The SSH host key the machine presents now, or `""` (optional fact)."""
    parsed = public_key_line(read_text(path))
    return parsed[1] if parsed else ""


def signin_is_set(path: str = FIRST_VISIT_STATE) -> bool:
    """True once the owner has completed the first visit on this machine."""
    state = load_state(path)
    value = state.get("signin_set_at")
    return isinstance(value, str) and bool(value.strip())


def boot_record_facts(cmdline: str, *, boot_report_path: str = BOOT_REPORT_PATH,
                      pin_path: str = PIN_PATH,
                      boot_key_path: str = BOOT_KEY_PATH,
                      boot_record_path: str = BOOT_RECORD_PATH,
                      host_key_path: str = HOST_KEY_PATH,
                      first_visit_path: str = FIRST_VISIT_STATE,
                      refresh: bool = True
                      ) -> tuple[dict | None, str, str, str]:
    """`(record, key fingerprint, why it is skipped, note)`.

    A skipped record is not a failed boot: the machine serves, HQ keeps the row
    it had, and the next boot retries. That is the card's fail-closed edge — a
    tenant that never first-boots never writes. `note` is the one fact that is
    not in the record and still belongs on the transcript: the image's
    `boot-key.pub` was rewritten from the measured line (t_556631d6).
    """
    line = (cmdline or "").strip()
    if not line:
        return None, "", "no measured command line", ""
    key_line, key_fingerprint, reason, note = measured_boot_key(
        line, boot_key_path, refresh=refresh)
    if not key_line:
        return None, "", reason, ""
    release, reason = pinned_release(boot_report_path, pin_path, boot_record_path)
    if not release:
        return None, "", reason, ""
    record = {
        "cmdline": line,
        "release": release,
        "first_visit": "done" if signin_is_set(first_visit_path) else "pending",
    }
    host_key = current_host_key(host_key_path)
    if host_key:
        record["host_key"] = host_key
    return record, key_fingerprint, "", note


def post_record(url: str, token: str, instance_id: str,
                record: dict) -> tuple[bool, dict | str]:
    """POST the boot record to the seed service; `(ok, answer-or-reason)`.

    The same endpoint and the same bearer token as the seed exchange: the seed
    is single-use, the record is idempotent. A service that refuses leaves the
    machine installed and serving; the console line says why.
    """
    body = json.dumps(
        {"token": token, "instance_id": instance_id, "record": record},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    request = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "odile-firstboot/%s" % VERSION},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read(MAX_SEED_BYTES) or b"{}")
            return True, payload if isinstance(payload, dict) else {}
    except urllib.error.HTTPError as error:
        reason = ""
        try:
            payload = json.loads(error.read(MAX_SEED_BYTES) or b"{}")
            if isinstance(payload, dict):
                reason = str(payload.get("reason") or payload.get("error") or "")
        except (ValueError, UnicodeDecodeError):
            pass
        return False, "refused (%d%s)" % (error.code, ", " + reason if reason else "")
    except urllib.error.URLError as error:
        return False, "unreachable: %s" % getattr(error, "reason", error)
    except (OSError, ValueError) as error:
        return False, "unreachable: %s" % error


def report_record(facts: dict | None, key_fingerprint: str, reason: str, *,
                  token: str, instance_id: str, seed_url: str, log_path: str,
                  console: bool, note: str = "") -> str:
    """Send the record and return the one-line result for the console or log.

    `console` is False on a converged (`same`) boot, where the console contract
    is exactly `odile-firstboot: same`; the log still carries the record line.
    `note` is the transcript's note (the image's boot-key.pub refreshed from
    the measured line), never part of what HQ is sent.
    """
    if facts is None:
        line = "skipped (%s)" % reason
    elif not token or not instance_id:
        line = "skipped (no setup token or instance)"
    else:
        ok, answer = post_record(seed_url, token, instance_id, facts)
        head = "%s %s key %s" % (facts["release"], facts["first_visit"], key_fingerprint)
        if ok and isinstance(answer, dict):
            changed = ", ".join(answer.get("changed") or [])
            line = "%s (hq %s%s)" % (head, answer.get("state", "recorded"),
                                     ", " + changed if changed else "")
        else:
            line = "%s (hq %s)" % (head, answer)
    if note:
        line = "%s · %s" % (line, note)
    if console:
        return line
    log_placements(log_path, ["record      %s" % line])
    return line




# ------------------------------------------------------------------- release

def log_placements(path: str, lines: list[str], label: str = "release placement") -> None:
    """The placement record: the installer's log, never the console.

    The console carries exactly one `odile-firstboot:` line. The log carries
    what was placed and, on a second boot, the `same` lines a reader compares
    with the first. `label` names the section a reader is looking at (the
    release placement, or this machine's SSH host keys).
    """
    if not lines:
        return
    try:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        with open(path, "ab") as handle:
            handle.write(("=== odile-firstboot %s: %s\n" % (utcnow(), label)).encode())
            for line in lines:
                handle.write((line + "\n").encode())
            handle.flush()
    except OSError:
        pass


def place_file(source: str, target: str, mode: int, what: str,
               dry: bool = False) -> tuple[str, str]:
    """One release artifact, atomically and idempotently.

    Returns (verb, log line). `same` means the bytes and the mode are already
    what the image carries; anything else is written through `write_atomic`,
    so a half-written file is never what the next boot reads.
    """
    try:
        with open(source, "rb") as handle:
            data = handle.read()
    except OSError as error:
        raise Refusal("the image has no %s (%s): %s"
                      % (what, source, error.strerror or error)) from None
    digest = sha256(data)
    verb = "install"
    try:
        with open(target, "rb") as handle:
            existing = handle.read()
        same_mode = (os.stat(target).st_mode & 0o777) == mode
    except OSError:
        existing = None
        same_mode = False
    if existing == data and same_mode:
        verb = "same"
    elif not dry:
        write_atomic(target, data, mode)
    return verb, "%-12s %s (%d B, sha256 %s, 0o%03o)" % (
        verb, target, len(data), digest[:12], mode)


def check_pair(checker: str, manifest: str, signature: str, signing_key: str) -> tuple[bool, str]:
    """Run the image's checker over the pair that will be served.

    The checker is the same program the manifest unit runs in ExecStartPre;
    running it here means the first boot and the unit cannot disagree about
    what a valid pair is.
    """
    try:
        proc = subprocess.run(
            [sys.executable, checker, "--check", "--manifest", manifest,
             "--signature", signature, "--pubkey", signing_key],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        return False, "error: the manifest checker %s is missing" % checker
    except subprocess.TimeoutExpired:
        return False, "error: the manifest check timed out"
    output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    line = output.splitlines()[0] if output else "the checker refused the pair"
    return proc.returncode == 0, line


def place_release(*, release_dir: str = RELEASE_DIR, installer_target: str = INSTALLER_COPY,
                  etc_dir: str = ETC_DIR, lib_dir: str = ODILE_LIB,
                  unit_target: str = MANIFEST_UNIT, signing_key: str = SIGNING_KEY,
                  log_path: str = LOG_PATH, systemctl=None) -> list[str]:
    """Place the release installer, the signed pair and its listener (ODL-472).

    `CLAIMS.md` §4.6 names `/usr/local/bin/odile-install.sh` and the tenant's
    Caddy proxies `/manifest` to this machine's listener; both are the
    release's bytes, placed before the installer runs and before any seed
    exchange, so an installed boot and a fail-closed boot agree.

    Which manifest pair? The image's, unless the machine already holds its own
    valid pair (an update places a newer one) — then the machine's is kept and
    verified, together with the installer that pair names: the disk's installer
    is kept when its hash is the manifest's `installer_sha256`, and the boot
    refuses when it is not, so CLAIMS 4.6 holds after an update and a reboot.
    Either way the pair is verified *before* anything is written, so a tampered
    image pair places nothing and a tampered installed pair fails the boot
    instead of being served.
    """
    artifacts = {
        "installer": (os.path.join(release_dir, "odile_install.py"), installer_target,
                      0o755, "release installer"),
        "program": (os.path.join(release_dir, "manifest.py"),
                    os.path.join(lib_dir, "manifest.py"), 0o755, "manifest listener"),
        "checker": (os.path.join(release_dir, "odile-manifest.py"),
                    os.path.join(lib_dir, "odile-manifest.py"), 0o755,
                    "manifest checker"),
        "unit": (os.path.join(release_dir, "odile-manifest.service"), unit_target,
                 0o644, "manifest unit"),
    }
    for source, _, _, what in artifacts.values():
        if not os.path.isfile(source):
            raise Refusal("the image has no %s (%s)" % (what, source))

    manifest_source = os.path.join(release_dir, "manifest.json")
    signature_source = manifest_source + ".sig"
    manifest_target = os.path.join(etc_dir, "manifest.json")
    signature_target = manifest_target + ".sig"
    pair_verb = "image"
    target_bytes = None
    target_signature = b""
    try:
        with open(manifest_target, "rb") as handle:
            target_bytes = handle.read()
        with open(signature_target, "rb") as handle:
            target_signature = handle.read()
    except OSError:
        target_bytes = None
    if target_bytes is not None:
        try:
            with open(manifest_source, "rb") as handle:
                source_bytes = handle.read()
            with open(signature_source, "rb") as handle:
                source_signature = handle.read()
        except OSError:
            source_bytes, source_signature = None, None
        if (target_bytes, target_signature) == (source_bytes, source_signature):
            pair_verb = "same"
        else:
            pair_verb = "installed"

    if pair_verb == "image":
        pair = (manifest_source, signature_source)
    elif pair_verb == "same":
        pair = (manifest_target, signature_target)
    else:
        pair = (manifest_target, signature_target)
    ok, detail = check_pair(artifacts["checker"][0], pair[0], pair[1], signing_key)
    if not ok:
        log_placements(log_path, ["%-12s %s" % ("refuse", detail)])
        raise Refusal("manifest signature does not verify")

    lines = ["%-12s %s" % ("verify", "%s (%s)" % (pair[0], detail))]
    keep_installer = False
    if pair_verb == "installed":
        # The machine's own pair names its installer (an update writes both,
        # update/odile_update.py apply_change). Keep the disk's installer when
        # it is the file the pair names; rewriting the image's over it would
        # leave CLAIMS 4.6 comparing two different files on every later boot.
        try:
            with open(manifest_target, "rb") as handle:
                named = json.load(handle).get("installer_sha256")
        except (OSError, ValueError):
            named = None
        try:
            with open(installer_target, "rb") as handle:
                installed = handle.read()
            installed_mode = os.stat(installer_target).st_mode & 0o777
        except OSError:
            installed, installed_mode = None, 0
        if not isinstance(named, str) or installed is None or sha256(installed) != named:
            detail = "the machine's manifest does not name %s" % installer_target
            log_placements(log_path, ["%-12s %s" % ("refuse", detail)])
            raise Refusal(detail)
        if installed_mode != 0o755:
            write_atomic(installer_target, installed, 0o755)
        lines.append("%-12s %s (%d B, sha256 %s, 0o755)"
                     % ("keep", installer_target, len(installed), named[:12]))
        keep_installer = True
    unit_verb = "same"
    for name in ("installer", "program", "checker", "unit"):
        source, target, mode, _ = artifacts[name]
        if name == "installer" and keep_installer:
            continue
        verb, line = place_file(source, target, mode, name)
        if name == "unit":
            unit_verb = verb
        lines.append(line)
    if pair_verb == "image":
        for source_path, target_path, what in (
                (manifest_source, manifest_target, "signed manifest"),
                (signature_source, signature_target, "manifest signature")):
            verb, line = place_file(source_path, target_path, 0o644, what)
            lines.append(line)
    elif pair_verb == "installed":
        lines.append("%-12s %s (the machine's own pair, verified)" % ("keep", manifest_target))
    else:
        lines.append("%-12s %s (the image's pair, byte-identical)" % ("same", manifest_target))
        lines.append("%-12s %s.sig (the image's pair, byte-identical)"
                     % ("same", manifest_target))

    if systemctl is not None:
        if unit_verb != "same":
            systemctl(["daemon-reload"])
        was_active = systemctl(["is-active", MANIFEST_SERVICE]).stdout.strip() == "active"
        systemctl(["enable", MANIFEST_SERVICE])
        started = systemctl(["start", MANIFEST_SERVICE])
        state = systemctl(["is-active", MANIFEST_SERVICE]).stdout.strip() or "unknown"
        if started.returncode != 0 or state != "active":
            detail = (started.stderr or "").strip().splitlines()
            raise Refusal("the manifest listener did not start (%s)"
                          % (detail[0] if detail else state))
        lines.append("%-12s %s (%s)" % ("same" if was_active else "start",
                                        MANIFEST_SERVICE, state))
    log_placements(log_path, lines)
    return lines


def systemctl_run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *argv], capture_output=True, text=True, timeout=120)


# --------------------------------------------------------------------- signing

def seed_signature(headers) -> bytes:
    value = headers.get("X-Odile-Signature", "") if headers is not None else ""
    if not isinstance(value, str) or not value.startswith(SIGNATURE_PREFIX):
        raise Refusal("seed response is unsigned")
    try:
        signature = base64.b64decode(value[len(SIGNATURE_PREFIX):], validate=True)
    except ValueError:
        raise Refusal("seed signature is not base64") from None
    if len(signature) != 64:
        raise Refusal("seed signature is %d bytes, not 64" % len(signature))
    return signature


def verify_bytes(data: bytes, signature: bytes, pubkey_path: str) -> bool:
    """Ed25519 over the exact response bytes, via the system OpenSSL."""
    if not os.path.isfile(pubkey_path):
        raise Refusal("the signing key %s is missing" % pubkey_path)
    fd, sig_path = tempfile.mkstemp(prefix="odile-seed-sig-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(signature)
        proc = subprocess.run(
            ["openssl", "dgst", "-verify", pubkey_path, "-signature", sig_path],
            input=data, capture_output=True, timeout=30,
        )
    except FileNotFoundError:
        raise Refusal("openssl is missing; cannot verify the seed") from None
    finally:
        try:
            os.unlink(sig_path)
        except OSError:
            pass
    return proc.returncode == 0


def seed_key_candidates(pubkey_path: str) -> list[str]:
    """The release keys a seed certificate may be certified by, current first.

    The image bakes the current release public key at
    /etc/odile/odile-signing-key.pub; `.prev.pub` beside it is the key being
    retired, shipped for one release (ODL-514), so the move to the CI key does
    not strip the certificate from a seed served while the new certificate is
    being cut. Identical bytes are tried once.
    """
    directory = os.path.dirname(os.path.abspath(pubkey_path))
    candidates = [pubkey_path, os.path.join(directory, "odile-signing-key.prev.pub")]
    seen: set[str] = set()
    unique: list[str] = []
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as handle:
                digest = sha256(handle.read())
        except OSError:
            continue
        if digest in seen:
            continue
        seen.add(digest)
        unique.append(path)
    return unique


def certify_seed_key(seed: dict, pubkey_path: str) -> tuple[str, str]:
    """(temp file holding the seed public key, the release key that certified it).

    The certificate is the release key's Ed25519 signature over the seed public
    key bytes; `signing_key_sha256` names those bytes, so another key cannot be
    slipped in beside somebody else's certificate. The caller removes the file.
    """
    pem = seed.get("signing_key")
    expected = seed.get("signing_key_sha256")
    if not isinstance(pem, str) or "BEGIN PUBLIC KEY" not in pem:
        raise Refusal("seed signing_key_sha256 is set and the seed carries no signing_key PEM")
    public = pem.encode("utf-8")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected.strip()):
        raise Refusal("seed signing_key_sha256 is not a SHA-256 hex digest")
    digest = sha256(public)
    if digest != expected.strip().lower():
        raise Refusal("the seed signing key is sha256 %s, signing_key_sha256 says %s"
                      % (digest, expected.strip().lower()))
    encoded = seed.get("signing_key_cert")
    if not isinstance(encoded, str) or not encoded:
        raise Refusal("seed signing_key_sha256 is set and the seed carries no signing_key_cert")
    try:
        certificate = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise Refusal("seed signing_key_cert is not base64") from None
    if len(certificate) != 64:
        raise Refusal("seed signing_key_cert is %d bytes, expected 64" % len(certificate))
    candidates = seed_key_candidates(pubkey_path)
    if not candidates:
        raise Refusal("no release public key to check the seed certificate against "
                      "(expected %s)" % pubkey_path)
    for path in candidates:
        if verify_bytes(public, certificate, path):
            directory = tempfile.mkdtemp(prefix="odile-seed-key-")
            key_path = os.path.join(directory, "seed-signing-key.pub")
            with open(key_path, "wb") as handle:
                handle.write(public)
            os.chmod(key_path, 0o644)
            return key_path, path
    raise Refusal("seed signing key certificate was not signed by %s"
                  % " or ".join(candidates))


def verify_seed_body(body: bytes, signature: bytes, pubkey_path: str) -> dict:
    """The parsed seed after its signature verifies, through the chain if carried.

    A seed that carries `signing_key_sha256` is checked through the certificate:
    the release key certifies the seed key, the seed key signs the body. A seed
    without it (issued before ODL-514) is checked against the release key
    itself, which is what signed it.
    """
    try:
        seed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise Refusal("seed is not JSON") from None
    if isinstance(seed, dict) and "signing_key_sha256" in seed:
        key_path, certifier = certify_seed_key(seed, pubkey_path)
        directory = os.path.dirname(key_path)
        try:
            ok = verify_bytes(body, signature, key_path)
        finally:
            try:
                os.unlink(key_path)
                os.rmdir(directory)
            except OSError:
                pass
        if not ok:
            raise Refusal("seed signature does not verify against the certified seed key")
        return seed
    if not verify_bytes(body, signature, pubkey_path):
        raise Refusal("seed signature does not verify")
    return seed


# ----------------------------------------------------------------------- seed

def validate_seed(seed, instance_id: str) -> dict:
    """The seed's shape and binding; refuse anything the installer cannot use."""
    if not isinstance(seed, dict):
        raise Refusal("seed is not a JSON object")
    for key in SEED_KEYS:
        if key not in seed:
            raise Refusal("seed has no %s" % key)
    if not isinstance(seed["slug"], str) or not SLUG_RE.match(seed["slug"]):
        raise Refusal("seed slug is malformed")
    if seed["instance_id"] != instance_id:
        raise Refusal("seed is for another instance")
    for key in ("near_api_key", "model", "support_public_key", "backup_repo_url", "hermes_version"):
        value = seed[key]
        if not isinstance(value, str) or not value.strip():
            raise Refusal("seed %s is empty" % key)
    # B-once (ODL-925): the installer writes this string into the tenant .env
    # byte for byte, so a malformed one would produce a gate nobody can open.
    sign_in_hash = seed["sign_in_hash"]
    if not isinstance(sign_in_hash, str) or not SCRYPT_SEED_RE.match(sign_in_hash):
        raise Refusal("seed sign_in_hash is not an installer-format scrypt string")
    mail = seed["mail_relay"]
    if not isinstance(mail, dict):
        raise Refusal("seed mail_relay is not an object")
    for key, kind in (("host", str), ("port", int), ("username", str), ("secret", str)):
        value = mail.get(key)
        if kind is int:
            ok = isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65536
        else:
            ok = isinstance(value, str) and bool(value.strip())
        if not ok:
            raise Refusal("seed mail_relay.%s is invalid" % key)
    for key in SEED_FORBIDDEN:
        if key in seed:
            raise Refusal("seed carries a forbidden %s field" % key)
    return seed


def post_seed(url: str, token: str, instance_id: str) -> tuple[int, bytes, object]:
    """One seed exchange; returns (status, body, headers) for any answer."""
    body = json.dumps({"token": token, "instance_id": instance_id},
                      sort_keys=True, separators=(",", ":")).encode()
    request = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "odile-firstboot/%s" % VERSION},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read(MAX_SEED_BYTES), response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(MAX_SEED_BYTES), error.headers
    except urllib.error.URLError as error:
        raise Refusal("seed service unreachable: %s" % getattr(error, "reason", error)) from None
    except OSError as error:
        raise Refusal("seed service unreachable: %s" % error) from None


def refusal_from_answer(status: int, body: bytes) -> Refusal:
    """The one-line reason for a refused exchange, from the service's own JSON."""
    reason = ""
    try:
        payload = json.loads(body.decode("utf-8"))
        if isinstance(payload, dict):
            reason = str(payload.get("reason") or "")
    except (ValueError, UnicodeDecodeError):
        pass
    if status == 410:
        return Refusal("seed token %s" % (reason or "used"))
    if status == 403:
        return Refusal("seed token is bound to another instance")
    if status == 404:
        return Refusal("seed token unknown")
    if 400 <= status < 500:
        return Refusal("seed request refused (%d%s)"
                       % (status, ", " + reason if reason else ""))
    return Refusal("seed service failed (%d%s)" % (status, ", " + reason if reason else ""))


def fetch_seed(url: str, token: str, instance_id: str, pubkey_path: str) -> tuple[dict, bytes, bytes]:
    """Exchange the token once; retry only what a retry can fix.

    Returns the validated seed, the exact signed response bytes and the
    signature over them, so the caller can write the cache pair the release
    installer reads (/etc/odile/seed.json and /etc/odile/seed.sig, ODL-204).
    """
    last: Refusal | None = None
    for attempt in range(ATTEMPTS):
        try:
            status, body, headers = post_seed(url, token, instance_id)
        except Refusal as refusal:
            last = refusal
            if attempt + 1 < ATTEMPTS:
                time.sleep(RETRY_PAUSE)
                continue
            raise
        if status == 200:
            signature = seed_signature(headers)
            # ODL-514: the body is signed by the seed key; the release key baked
            # into the image certifies that key. A seed issued before the split
            # carries no certificate and still verifies against the release key.
            seed = verify_seed_body(body, signature, pubkey_path)
            return validate_seed(seed, instance_id), body, signature
        if status >= 500 and attempt + 1 < ATTEMPTS:
            last = refusal_from_answer(status, body)
            time.sleep(RETRY_PAUSE)
            continue
        raise refusal_from_answer(status, body)
    raise last or Refusal("seed service unreachable")


# ---------------------------------------------------------------------- apply

def run_installer(installer: str, seed_path: str, token: str, first_visit_port: int,
                  log_path: str) -> int:
    """Run the image's installer with the seed; its output goes to the log.

    The setup token is a bearer while it lives, so the line that echoes the
    argv never carries it: the log says ``--first-visit-token <redacted>``
    (ADR-006 §5 defect 4, ODL-579). The log is also forced to 0600 — the unit
    sets ``UMask=0077``, but a file an older boot left 0644 must be narrowed,
    not appended to.
    """
    command = [installer, "--seed", seed_path, "--first-visit-token", token]
    logged = [installer, "--seed", seed_path, "--first-visit-token", TOKEN_REDACTED]
    if first_visit_port:
        command += ["--first-visit-port", str(first_visit_port)]
        logged += ["--first-visit-port", str(first_visit_port)]
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "ab") as log:
        log.write(("\n=== odile-firstboot %s: %s\n" % (utcnow(), " ".join(logged))).encode())
        log.flush()
        try:
            proc = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                  timeout=900)
        except FileNotFoundError:
            return 127
        except subprocess.TimeoutExpired:
            return 124
    return proc.returncode


def wait_first_visit(port: int, seconds: float = 120.0) -> tuple[bool, str]:
    """The page answers through loopback with a form, tokenless (B-once).

    The first visit is a login (ODL-925): ``/first-visit`` carries no setup
    token, so the probe asks the bare path and requires 200 with the form. The
    token still rides ``install-stack.sh --seed`` for the seed exchange; it is
    not a page credential any more.
    """
    deadline = time.monotonic() + seconds
    last = "no answer"
    while time.monotonic() < deadline:
        health = _get(port, "/health")
        if health is not None and health[0] == 200 and b"ok" in health[1]:
            page = _get(port, "/first-visit")
            if page is not None and page[0] == 200 and (
                    b"<form" in page[1]
                    and (b"first-visit" in page[1] or b"sign-in" in page[1])):
                return True, "200"
            last = "health 200, page %s" % (
                "status %d" % page[0] if page is not None else "no answer")
        else:
            last = "health %s" % ("status %d" % health[0] if health is not None else "no answer")
        time.sleep(2)
    return False, last


def _get(port: int, path: str) -> tuple[int, bytes] | None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path),
                                    timeout=10) as response:
            return response.status, response.read(MAX_SEED_BYTES)
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (urllib.error.URLError, OSError):
        return None


# ------------------------------------------------------------------------ cli

def print_state(state_path: str, seed_path: str) -> int:
    state = load_state(state_path)
    seed = load_state(seed_path)
    print("odile-firstboot %s" % VERSION)
    print("state        %s" % state_path)
    print("ok           %s" % state.get("ok"))
    print("instance     %s" % (state.get("instance_id") or "-"))
    print("slug         %s" % (state.get("slug") or seed.get("slug") or "-"))
    print("seed sha256  %s" % (state.get("seed_sha256") or "-"))
    print("first boot   %s" % (state.get("first_boot_at") or "-"))
    raw_record = state.get("boot_record")
    record: dict = raw_record if isinstance(raw_record, dict) else {}
    print("boot record  %s %s %s" % (
        record.get("release") or "-", record.get("first_visit") or "-",
        record.get("boot_key_fingerprint") or "-"))
    print("record line  %s" % (record.get("result") or "-"))
    print("reason       %s" % (state.get("reason") or "-"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Odile zero-touch first boot (ODL-456)")
    parser.add_argument("--cmdline", default=CMDLINE)
    parser.add_argument("--state", default=STATE_PATH)
    parser.add_argument("--seed-cache", default=SEED_CACHE)
    parser.add_argument("--signing-key", default=SIGNING_KEY)
    parser.add_argument("--installer", default=INSTALLER)
    parser.add_argument("--first-visit-port", type=int, default=FIRST_VISIT_PORT)
    parser.add_argument("--first-visit-token", default="",
                        help="the setup token for the seed exchange, when an older kit "
                             "passes it here instead of the measured command line; "
                             "optional, and never a page credential (ODL-925)")
    parser.add_argument("--first-visit-service",
                        default="/usr/local/lib/odile/first_visit.py",
                        help="the page's service; absent means the image has no page")
    parser.add_argument("--log", default=LOG_PATH)
    parser.add_argument("--release-dir", default=RELEASE_DIR,
                        help="the image's release artifacts (ODL-472)")
    parser.add_argument("--installer-copy", default=INSTALLER_COPY,
                        help="where the release installer is placed (CLAIMS 4.6)")
    parser.add_argument("--ssh-root", default=SSH_ROOT,
                        help="the root ssh-keygen -A writes <root>/etc/ssh under")
    parser.add_argument("--ssh-keygen", default=SSH_KEYGEN,
                        help="ssh-keygen to run (a test may pass a stub)")
    parser.add_argument("--etc-dir", default=ETC_DIR)
    parser.add_argument("--odile-lib", default=ODILE_LIB)
    parser.add_argument("--manifest-unit", default=MANIFEST_UNIT)
    parser.add_argument("--pin", default=PIN_PATH,
                        help="the image pin the machine booted (its release)")
    parser.add_argument("--boot-key", default=BOOT_KEY_PATH,
                        help="the image's own copy of the measured boot key")
    parser.add_argument("--boot-report", default=BOOT_REPORT_PATH,
                        help="this boot's attest report (its release wins)")
    parser.add_argument("--boot-record", default=BOOT_RECORD_PATH,
                        help="the installer's boot record (the pin's fallback)")
    parser.add_argument("--host-key", default=HOST_KEY_PATH,
                        help="the sshd host key the record carries in detail.host_key")
    parser.add_argument("--hostname-path", default=HOSTNAME_PATH,
                        help="the file `bb.host` re-derives (default /etc/hostname)")
    parser.add_argument("--hosts-path", default=HOSTS_PATH,
                        help="the file whose image-written lines name the machine")
    parser.add_argument("--kernel-hostname", choices=("apply", "skip"), default="skip",
                        help="set the running kernel hostname too; the unit passes "
                             "`apply`, a test or probe `skip` (default)")
    parser.add_argument("--first-visit-state", default=FIRST_VISIT_STATE,
                        help="the first-visit state that says the sign-in is set")
    parser.add_argument("--print-state", action="store_true",
                        help="print the secret-free state and exit")
    parser.add_argument("--check", action="store_true",
                        help="exit 0 when the first boot is installed, 1 otherwise")
    args = parser.parse_args(argv)

    if args.print_state:
        return print_state(args.state, args.seed_cache)

    try:
        cmdline_text = read_text(args.cmdline)
        facts = cmdline_facts(cmdline_text)
        instance_id = facts["instance_id"]
        token = facts["token"] or read_stdin_token(os.path.join(
            os.path.dirname(args.seed_cache), "setup-token")) or args.first_visit_token
        # ODL-523: the image ships no SSH host private key; this boot makes the
        # machine's own before any unit can need one — and before the boot
        # record is read, so the first boot's record carries this machine's host
        # key (ODL-398 reads current_host_key()). A fail-closed boot still gets
        # them, so the machine stays reachable.
        if not args.check:
            verb, detail = ensure_host_keys(args.ssh_root, args.ssh_keygen)
            log_placements(args.log, ["%-12s %s" % (verb, detail)], label="ssh host keys")
            # The serial transcript's host-key line (ODL-523): before the boot
            # record is read, and before the one `odile-firstboot:` line, so a
            # reader sees the key came first. Its own prefix keeps the
            # one-`odile-firstboot:`-line console contract.
            print("odile-hostkeys: %s %s" % (verb, detail), flush=True)
        # ODL-348 follow-up: the image's `/etc/hostname` is the build tenant's
        # slug. Re-derive it from the measured line on every boot, before the
        # release placement can read it; a converged boot reads `same` and
        # writes nothing. A malformed or absent bb.host is cosmetic and must
        # not fail the boot, so it is a log line, not a Refusal.
        if not args.check:
            name = facts["hostname"]
            if not name:
                log_placements(args.log, ["%-12s %s" % (
                    "absent", "no bb.host in the command line")], label="hostname")
            elif not SLUG_RE.match(name):
                log_placements(args.log, ["%-12s %s" % (
                    "skip", "bb.host is not one label: %r" % name)], label="hostname")
            else:
                verb, detail = ensure_hostname(
                    name, hostname_path=args.hostname_path,
                    hosts_path=args.hosts_path,
                    set_kernel=(socket.sethostname
                                if args.kernel_hostname == "apply" else None))
                log_placements(args.log, ["%-12s %s" % (verb, detail)],
                               label="hostname")
        record, record_key, record_reason, record_note = boot_record_facts(
            cmdline_text, boot_report_path=args.boot_report,
            pin_path=args.pin, boot_key_path=args.boot_key,
            boot_record_path=args.boot_record, host_key_path=args.host_key,
            first_visit_path=args.first_visit_state,
            # --check is a read-only probe: it must not rewrite the image's
            # boot-key.pub the way the boot itself does (t_556631d6).
            refresh=not args.check,
        )
        state = load_state(args.state)
        # ODL-472: the release's exact installer and signed manifest pair, and
        # the listener that serves them. Placed before the state short-circuit
        # so a second boot reads `same` for every byte, and before the seed
        # exchange so a fail-closed machine still carries the release.
        if not args.check:
            place_release(
                release_dir=args.release_dir,
                installer_target=args.installer_copy,
                etc_dir=args.etc_dir,
                lib_dir=args.odile_lib,
                unit_target=args.manifest_unit,
                signing_key=args.signing_key,
                log_path=args.log,
                systemctl=systemctl_run if os.path.isdir(SYSTEMD_DIR) else None,
            )
        if instance_id and installed_already(state, instance_id, args.seed_cache):
            if args.check:
                return 0
            report_record(record, record_key, record_reason, token=token,
                          instance_id=instance_id, seed_url=facts["seed_url"],
                          log_path=args.log, console=False, note=record_note)
            say("same")
            return 0
        if args.check:
            return 1
        if not instance_id:
            raise Refusal("no instance id (bb.iid) in the command line")
        if not INSTANCE_RE.match(instance_id):
            raise Refusal("instance id in the command line is malformed")

        # A failed apply (a transient installer error) must be able to retry
        # from the cache without a second exchange: the token is spent by the
        # first one. The cache is only trusted for this instance.
        cached = cached_seed_bytes(args.seed_cache, instance_id)
        if cached is not None:
            seed = validate_seed(json.loads(cached.decode("utf-8")), instance_id)
            body = cached
        else:
            if not token:
                raise Refusal("no setup token in the command line")
            if not TOKEN_RE.match(token):
                raise Refusal("setup token in the command line is malformed")
            if not SEED_URL_RE.match(facts["seed_url"]):
                raise Refusal("seed URL is malformed")
            seed, body, signature = fetch_seed(facts["seed_url"], token, instance_id,
                                               args.signing_key)
            # The cache pair the release installer reads (ODL-204/ODL-203):
            # the exact signed bytes and the signature over them. The
            # signature is base64, one line, 0600, beside the seed.
            write_atomic(args.seed_cache, body, 0o600)
            # The installer's own name for the pair is seed.json + seed.sig
            # (Layout.seed_cache / Layout.seed_sig, ODL-204); a .json.sig
            # beside it would not be found.
            write_atomic(os.path.splitext(args.seed_cache)[0] + ".sig",
                         base64.b64encode(signature) + b"\n", 0o600)
        if not token:
            raise Refusal("no setup token for the seed exchange")
        # The pre-installer state: no sign-in yet. The seeded verifier reaches
        # the .env inside the installer, and that writer stamps the state after
        # the write (ODL-947).
        write_first_visit_state(os.path.join(os.path.dirname(args.seed_cache),
                                             "first-visit.json"), token)

        started = time.monotonic()
        code = run_installer(args.installer, args.seed_cache, token,
                             args.first_visit_port, args.log)
        if code != 0:
            raise Refusal("installer failed (rc %d)" % code)
        if args.first_visit_port and os.path.exists(args.first_visit_service):
            ok, detail = wait_first_visit(args.first_visit_port)
            if not ok:
                raise Refusal("first-visit page did not answer (%s)" % detail)
            page = detail
        elif args.first_visit_port:
            # A tree without ODL-457's page: the seed is applied and the
            # machine is up, but there is no page to reach. Say so.
            page = "absent"
        else:
            page = "not checked"
        record_line = report_record(record, record_key, record_reason, token=token,
                                    instance_id=instance_id, seed_url=facts["seed_url"],
                                    log_path=args.log, console=True, note=record_note)
        save_state(args.state, {
            "version": 1,
            "ok": True,
            "instance_id": instance_id,
            "slug": seed["slug"],
            "seed_sha256": sha256(body),
            "first_boot_at": utcnow(),
            "reason": None,
            "boot_record": {
                "release": record["release"] if record else None,
                "first_visit": record["first_visit"] if record else None,
                "boot_key_fingerprint": record_key or None,
                "result": record_line,
            },
        })
        say("installed %s (instance %s) · first-visit %s · record %s · %ds"
            % (seed["slug"], instance_id, page, record_line,
               int(time.monotonic() - started)))
        return 0
    except Refusal as refusal:
        state = load_state(args.state)
        state.update({"version": 1, "ok": False, "reason": refusal.reason})
        try:
            save_state(args.state, state)
        except OSError:
            pass
        say("fail-closed: %s" % refusal.reason)
        return 0
    except Exception as error:  # noqa: BLE001 - the console contract is one line
        # A bug or a full disk must reach the serial as one line, not a
        # traceback that pushes the contract off the transcript. The detail is
        # the exception type and message; never a secret (nothing here holds
        # one in its repr).
        detail = "%s: %s" % (type(error).__name__, error)
        try:
            state = load_state(args.state)
            state.update({"version": 1, "ok": False, "reason": detail})
            save_state(args.state, state)
        except OSError:
            pass
        say("fail-closed: internal error: %s" % detail)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
