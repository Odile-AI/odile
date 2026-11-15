#!/usr/bin/python3
"""Leak scan — the publication gate.

`secret_scan.py` answers "is a credential in this tree?". This answers the wider
question a public repository asks on every commit: *does this tree name a real
machine, a real person or a real account?* It is the gate that runs in the public
repository, where an address, a mailbox or a vendor id is a leak even when it is
not a secret.

Classes:

* ``private-key``        a complete PEM or OpenSSH private-key block, in text or
                         inside a binary (printable runs are extracted first).
* ``provider-token``     a credential with a known prefix (GitHub, GitLab, AWS,
                         Google, Slack, Stripe, SendGrid, Anthropic, OpenAI,
                         Cloudflare, NEAR, and a generic JWT).
* ``credential-assignment``  a literal value assigned to a name ending in
                         SECRET, TOKEN, PASSWORD, API_KEY, ACCESS_KEY, or to a
                         PASSPHRASE / MNEMONIC / SEED / SECRET_KEY / CREDENTIAL
                         name. A passphrase need not contain a digit.
* ``credential-literal`` a credential written as a Markdown table cell under a
                         label that names one (README's published demo password).
* ``machine-identity``   a silicon or machine id: a 64-byte SEV-SNP chip id (in
                         text, in a decoded report, or in a VCEK's HWID), the
                         VCEK certificate serial, a PARTUUID, a machine-id, a MAC
                         address, or a hardware serial.
* ``vendor-resource-id`` a provider's server / box / account id, a tenant slug,
                         or a host name.
* ``dns-resource-id``    a DNS zone or record id (32 hex).
* ``ssh-fingerprint``    a ``SHA256:`` / ``MD5:`` key fingerprint. Unlike the
                         high-entropy rule, this class is not blinded by the
                         ``sha256`` / ``partuuid`` suppression.
* ``high-entropy``       an unbroken 32-128 character mixed-case token whose
                         Shannon entropy is at least 4.5 bits per character.
* ``public-ip``          an IPv4 address outside the loopback, private,
                         link-local, CGNAT, benchmarking and documentation
                         ranges, including an address dashed into a hostname.
* ``public-ipv6``        an IPv6 address outside ``2001:db8::/32`` and ``::1``.
* ``email``              an address whose domain is not a placeholder domain.
* ``home-path``          ``/root/…``, ``/home/<name>/…``, ``/Users/<name>/…`` or
                         ``C:\\Users\\<name>\\…`` where the name is not a service
                         account the product ships.
* ``basic-auth-url``     a URL that carries a username and a password.
* ``phone``              an E.164 or national-format telephone number.
* ``forbidden-value``    a literal from the list named by ``--forbid-file``: the
                         vendor account ids, box ids and checkout path that must
                         never appear in a published tree. The list itself is the
                         one file that carries them, so it is not scanned for its
                         own literals.

Opaque payloads. A base64 run longer than 128 characters is decoded and
scanned; a UTF-16 payload is decoded before the text rules run; a DER
certificate is read for its serial and HWID; an archive is opened and every
member is scanned at its own ``archive!member`` path.

Allowlist discipline. Every exception is one line of ``leak-allow.tsv``:

    rule<TAB>path-glob<TAB>sha256:<12><TAB>class<TAB>credential<TAB>reason<TAB>expires

The digest is over the matched value, so a row cannot hide a *different* value at
the same path; a ``*`` path-glob is allowed because the digest still pins the one
value, so there is no path-wide row.

``class`` is either ``constant`` — a value that is permanent (a published
password, a public key, a reserved-domain address, a test fixture) — and then
expiry does not apply, so it reads ``never`` (a date is accepted and never
expires); or ``debt`` — an allowance a card will clear — and then ``expires`` is
that card's date (``YYYY-MM-DD``). A debt row past its date is a finding; a
constant row never expires. A debt row carrying ``never`` is a format finding.

``credential`` is ``yes`` when the value is a secret a person could present to a
live system, and ``no`` otherwise. Every run prints the count beside the row
count, so a reviewer sees how many secrets the allowlist waives.

A row without a reason is itself a finding, and so is a row whose reason contains
its own value: that row matches itself, so "every row matched" would prove
nothing.

The forbid list. The default is ``leak-forbid.txt`` at the root of the tree, and
the public tree carries one (``tools/build_public_tree.py`` copies it). A missing
default is printed in the header, so a skip is never silent; an explicitly named
``--forbid-file`` that does not exist is a finding and exit 2, because the caller
asked for that list and did not get it.

Usage:

    tools/leak_scan.py --tree                 # tracked and untracked files
    tools/leak_scan.py --history              # every commit in rev-list --all
    tools/leak_scan.py --all --forbid-file docs/ops/leak-forbid.txt

Exit codes: 0 clean, 1 a finding, 2 the scan could not run.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import gzip
import hashlib
import io
import ipaddress
import math
import re
import subprocess
import sys
import tarfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

MIN_TOKEN, MAX_TOKEN, MIN_ENTROPY = 32, 128, 4.5
MIN_ASSIGNMENT_LENGTH, MIN_ASSIGNMENT_ENTROPY = 20, 3.3
SKIP_SUFFIXES = (".sig",)
SKIP_DIRS = (".git", ".worktrees", ".fresh-clone", ".venv", "__pycache__", "node_modules")
ALLOW_FILE = "leak-allow.tsv"
DEFAULT_FORBID = "leak-forbid.txt"

# Addresses that are never a leak: loopback, private, link-local, CGNAT,
# benchmarking, documentation and the public resolvers the product names.
IP_ALLOW = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),      # RFC 6598 CGNAT / Tailscale
    ipaddress.ip_network("198.18.0.0/15"),      # RFC 2544 benchmarking
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("255.255.255.0/32"),
    ipaddress.ip_network("1.1.1.1/32"),
    ipaddress.ip_network("9.9.9.9/32"),
    ipaddress.ip_network("8.8.8.8/32"),
    ipaddress.ip_network("8.8.4.4/32"),
    ipaddress.ip_network("172.64.0.0/13"),
)
IPV6_ALLOW = (
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("2001:db8::/32"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("2002::/16"),
    ipaddress.ip_network("2606:4700::/32"),
    ipaddress.ip_network("2620:fe::/32"),
    ipaddress.ip_network("2001:4860::/32"),
)
# A placeholder domain is not a person. Anything else is.
EMAIL_DOMAIN_ALLOW = {
    "example.com", "example.invalid", "example.test", "example.org",
    "odile.fyi", "mail.odile.fyi", "backup.odile.fyi", "odile.test",
    "users.noreply.github.com", "openssh.com", "localhost",
}
# Example mailboxes in the bundled skill docs: a role, not a person.
EMAIL_ALLOW = {
    "you@gmail.com", "you@icloud.com", "git@github.com", "work@company.com",
    "boss@company.com", "alice@co.com", "bob@co.com", "accounting@company.com",
    "hermes@agent.ai", "hello@cocoon-ai.com",
}
# The service accounts the product ships — including the retired HQ demo's,
# whose home path dated records still name; every other /home/<name>/ is a person.
HOME_ALLOW = {"me", "user", "odile", "runner", "linuxbrew", "ubuntu"}
# /root/ is the service root's home, so its product-owned paths are not a person
# either. The operator checkout path is deliberately absent: it names this
# operator's working copy and the public build replaces it with <checkout>.
ROOT_ALLOW = {
    ".odile", ".hermes", ".ssh", ".local", ".config", ".npm", ".cache",
    ".gitconfig", "AGENTS.md", "bin", "tmp", "usr", "boot-key", "kit.json",
    "backups", "servury-blackbox-verification.md",
}
MAC_HOME_ALLOW = {"Shared", "Public", "Default", "Default User", "All Users"}
WIN_HOME_ALLOW = {"Public", "Default", "Default User", "All Users"}

PEM_BEGIN = re.compile(r"-----BEGIN ([A-Z0-9][A-Z0-9 ]*)-----")
PEM_END = re.compile(r"-----END ([A-Z0-9][A-Z0-9 ]*)-----")
INLINE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----(?:\\n|\s)+[A-Za-z0-9+/=\\n\s]{40,}"
    r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----")
TOKENS = (
    ("github", re.compile(r"\b(?:gh[pousr]_[0-9A-Za-z]{36,}|github_pat_[0-9A-Za-z_]{22,})\b")),
    ("gitlab", re.compile(r"\bglpat-[0-9A-Za-z_-]{20,}\b")),
    ("aws", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws-arn", re.compile(r"\barn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:\d{12}:")),
    ("google-api", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("stripe", re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}\b")),
    ("stripe-webhook", re.compile(r"\bwhsec_[0-9A-Za-z]{20,}\b")),
    ("sendgrid", re.compile(r"\bSG\.[0-9A-Za-z_-]{22}\.[0-9A-Za-z_-]{43}\b")),
    ("anthropic", re.compile(r"\bsk-ant-[0-9A-Za-z_-]{20,}\b")),
    ("openai", re.compile(r"\bsk-(?:proj-)?[0-9A-Za-z_-]{32,}\b")),
    ("cloudflare", re.compile(r"\b[0-9a-f]{37}\b")),
    ("near-secret", re.compile(r"\bed25519:[1-9A-HJ-NP-Za-km-z]{80,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)
CRED_NAME = re.compile(
    r"(?i)^[A-Za-z0-9_.-]*(?:SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE|MNEMONIC|"
    r"SEED|SECRET_?KEY|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL)$")
ASSIGNMENT = re.compile(r"(?i)\b([A-Za-z_][A-Za-z0-9_.-]{0,60})\s*[:=]\s*[\"']?([A-Za-z0-9+/=_.\-]{16,})[\"']?")
QUOTED_ASSIGNMENT = re.compile(r"(?i)\b([A-Za-z_][A-Za-z0-9_.-]{0,60})\s*[:=]\s*[\"']([^\"'\n]{16,})[\"']")
ASSIGN_PREFIX = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]{0,48})[:=](.+)$")
PLACEHOLDER = re.compile(
    r"(?i)^(?:redacted.*|.*redacted|<.*>|\$\{.*\}|\$\(.*\)|%.*|x{4,}|\.{3,}|\*+|-+|=+|"
    r"changeme|example.*|fixture.*|your[-_].*|placeholder.*|dummy.*|fake.*|none|null|true|false|todo|same|"
    r"test.*|demo.*|local.*|.*not[-_]a[-_]secret.*)$")
TOKEN = re.compile(r"(?<![A-Za-z0-9+/=_-])([A-Za-z0-9+/=_-]{32,128})(?![A-Za-z0-9+/=_-])")
B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B64_RUN = re.compile(r"(?<![A-Za-z0-9+/=])([A-Za-z0-9+/]{129,}={0,2})(?![A-Za-z0-9+/=])")
IPV4 = re.compile(r"(?<![\d.])((?:[0-9]{1,3}\.){3}[0-9]{1,3})(?![\d.])")
IPV6 = re.compile(r"(?<![\w:.])([0-9A-Fa-f:]{6,39})(?![\w:.])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
HOME_PATH = re.compile(r"(?<![\w.])/home/([a-z][a-z0-9_-]{0,31})/")
ROOT_PATH = re.compile(r"(?<![\w.])/root/([A-Za-z0-9_.-]+)")
MAC_HOME = re.compile(r"(?<![\w.])/Users/([A-Za-z0-9._-]+)/")
WIN_HOME = re.compile(r"(?i)\b[A-Z]:\\Users\\([A-Za-z0-9._ -]+)\\")
BASIC_AUTH = re.compile(r"\b[a-z][a-z0-9+.-]*" + "://" + r"[^/\s:@]+:[^/\s@]+@[a-z0-9.-]+", re.I)
PHONE = re.compile(r"(?<![\d+])\+[1-9]\d{7,14}(?![\d])")
PHONE_NATIONAL = re.compile(
    r"(?<![\d])(?:\+?1[\s.-]?)?\([0-9]{3}\)\s*[0-9]{3}[-.][0-9]{4}(?![\d])"
    r"|(?<![\d])\b[0-9]{3}[-.][0-9]{3}[-.][0-9]{4}\b")
PRINTABLE = re.compile(rb"[ -~]{6,}")
ALLOW_CLASSES = ("constant", "debt")
ALLOW_CREDENTIAL = ("yes", "no")
ALLOW_DIGEST = re.compile(r"sha256:[0-9a-f]{12}$")

# Machine identity: a silicon id, a volume id, a host's own ids.
CHIP_ID = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{128}(?![0-9A-Fa-f])")
PARTUUID = re.compile(
    r"(?i)\bpartuuid\s*[=:]\s*([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\b")
MACHINE_ID = re.compile(r"(?i)\bmachine[-_ ]?id\b\D{0,24}([0-9A-Fa-f]{32})\b")
MAC = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])")
SERIAL_VALUE = re.compile(
    r"(?i)\b(?:(?:hardware|chassis|system|board|product|device)[ _-]?serial"
    r"(?:[ _-]?(?:number|no))?|serial[ _-]?number)\s*[=:]\s*"
    r"[\"']?([A-Za-z0-9][A-Za-z0-9._-]{7,31})\b")

# Vendor resource ids: how the provider names a machine or an account.
INSTANCE_ID = re.compile(
    r"(?i)\b(?:bb\.iid|iid|instance[_-]?id|server[_-]?id|box[_-]?id|account[_-]?id)\b"
    r"\s*[=:]\s*[\"']?([0-9A-Fa-f]{8})\b")
HOST_NAME = re.compile(
    r"(?i)\b(?:bb\.host|host[_-]?name)\b\s*[=:]\s*[\"']?([a-z][a-z0-9]{5,11})\b")
HOST_NAME_ALLOW = {"hostfake", "hostname", "localhost", "machine", "odlukitest", "scratch", "tokens"}
# Synthetic instance ids the tests use; a fixture is not a provider's machine.
INSTANCE_ID_ALLOW = {"01494715", "0badc0de", "45445445", "7448afca", "abc12345", "cafebabe", "fbf0f1f0"}
TENANT_SLUG = re.compile(r"\bodl-[0-9]{4}[a-z0-9]{2,4}\b")
TENANT_SLUG_ALLOW = {"odl-000000"}

# DNS resource ids: Cloudflare zone and record ids are 32 hex.
ZONE_ID = re.compile(r"(?i)\bzone(?:[_-]?id)?\b\D{0,24}([0-9A-Fa-f]{32})\b")
RECORD_ID = re.compile(r"(?i)\brecord(?:[_-]?id)?\b\D{0,24}([0-9A-Fa-f]{32})\b")

# Key fingerprints, deliberately outside the sha256 suppression.
SSH_FINGERPRINT = re.compile(r"\bSHA256:[A-Za-z0-9+/]{43}\b")
MD5_FINGERPRINT = re.compile(r"\bMD5:(?:[0-9A-Fa-f]{2}:){15}[0-9A-Fa-f]{2}\b")

# A dotted quad dashed into a hostname, e.g. 203-0-113-10.sslip.io.
DASHED_HOST = re.compile(r"(?<![\w.-])((?:[0-9]{1,3}-){3}[0-9]{1,3})(?=\.(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
# A version quadruple is not an address.
VERSION_BEFORE = re.compile(r"(?i)\b(?:version|release|build|firmware|kernel|api|protocol|v)\s*[:=]?\s*$")
VERSION_LINE = re.compile(r"(?i)\b(?:version|release|build|firmware|kernel)\b")

# A credential in a Markdown table cell, e.g. README's published demo password.
TABLE_CREDENTIAL = re.compile(
    r"(?im)^\s*\|\s*(?:\*\*)?(?:password|passphrase|secret|token|credential|api[ _-]?key)"
    r"(?:\*\*)?\s*\|\s*`([^`\n]{8,})`\s*\|")

ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".zip", ".gz")
MAX_ARCHIVE_MEMBERS = 500
MAX_ARCHIVE_MEMBER_BYTES = 8 * 1024 * 1024


@dataclass
class Allow:
    rule: str
    path: str
    digest: str
    klass: str
    credential: str
    reason: str
    expires: str

    def live(self, today: date) -> bool:
        if self.klass == "constant":
            return True
        try:
            return date.fromisoformat(self.expires) >= today
        except ValueError:
            return False


@dataclass
class Finding:
    rule: str
    path: str
    line: int
    value: str
    commit: str = ""
    note: str = ""

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.value.encode("utf-8", "replace")).hexdigest()[:12]

    def render(self) -> str:
        head = f"FINDING {self.rule:<22} "
        if self.commit:
            head += f"commit {self.commit} "
        head += f"{self.path}:{self.line} sha256:{self.digest} len={len(self.value)}"
        if self.note:
            head += f"  ({self.note})"
        return head


@dataclass
class Report:
    mode: str
    files: int = 0
    commits: int = 0
    blobs: int = 0
    bytes_scanned: int = 0
    allowed: int = 0
    findings: list[Finding] = field(default_factory=list)


def shannon(value: str) -> float:
    counts = Counter(value)
    total = len(value)
    return -sum(n / total * math.log2(n / total) for n in counts.values())


def allow_problem(row: Allow, number: int) -> str:
    """One format finding for a row that cannot mean what it says, else ''."""
    where = f"{ALLOW_FILE}:{number}"
    if row.klass not in ALLOW_CLASSES:
        return (f"{where}: class must be {' or '.join(ALLOW_CLASSES)}, "
                f"not {row.klass!r}")
    if row.credential not in ALLOW_CREDENTIAL:
        return (f"{where}: credential must be {' or '.join(ALLOW_CREDENTIAL)}, "
                f"not {row.credential!r}")
    if not ALLOW_DIGEST.match(row.digest):
        return f"{where}: digest must read sha256:<12 lowercase hex>, not {row.digest!r}"
    if not row.reason:
        return f"{where}: a row without a reason is a finding"
    # `class` decides what the expiry means: a constant never expires, so a date
    # on one is accepted and ignored; a debt must carry the date that clears it.
    if row.expires != "never":
        try:
            date.fromisoformat(row.expires)
        except ValueError:
            return f"{where}: expires must be never or YYYY-MM-DD, not {row.expires!r}"
    if row.klass == "debt" and row.expires == "never":
        return f"{where}: a debt row needs the date of the card that clears it, not 'never'"
    return ""


def load_allow(root: Path) -> tuple[list[Allow], list[str]]:
    path = root / ALLOW_FILE
    rows: list[Allow] = []
    problems: list[str] = []
    if not path.exists():
        return rows, problems
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            problems.append(f"{ALLOW_FILE}:{number}: expected 7 tab-separated fields "
                            f"(rule, path-glob, sha256, class, credential, reason, expires)")
            continue
        row = Allow(*(p.strip() for p in parts))
        problem = allow_problem(row, number)
        if problem:
            problems.append(problem)
            continue
        rows.append(row)
    return rows, problems


def is_doc_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    return any(address in net for net in IP_ALLOW)


def is_doc_ipv6(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    return any(address in net for net in IPV6_ALLOW)


def looks_like_path(value: str) -> bool:
    return value.startswith(("/", "./", "../")) or (value.count("/") >= 2 and "+" not in value)


def high_entropy(value: str) -> bool:
    if not MIN_TOKEN <= len(value) <= MAX_TOKEN:
        return False
    if value in B64 or B64 in value or value in B58 or B58 in value:
        return False
    if not (re.search(r"[A-Z]", value) and re.search(r"[a-z]", value) and re.search(r"[0-9]", value)):
        return False
    if len(set(value)) < 12:
        return False
    return shannon(value) >= MIN_ENTROPY


def token_value(token: str) -> str:
    """Strip a leading NAME= / NAME: from a token, when a value follows."""
    match = ASSIGN_PREFIX.match(token)
    if match and match.group(2):
        return match.group(2)
    return token


def secret_assignment(name: str, value: str) -> bool:
    if not CRED_NAME.match(name) or len(value) < MIN_ASSIGNMENT_LENGTH:
        return False
    if not re.search(r"[A-Za-z]", value):
        return False
    if PLACEHOLDER.match(value) or looks_like_path(value):
        return False
    if " " in value:
        words = value.split()
        if len(words) < 2 or not all(re.fullmatch(r"[a-z]+", word) for word in words):
            return False    # a phrase, but not a mnemonic or passphrase
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$", value):
        return False        # a dotted expression, not a value
    if re.match(r"^_?[a-z][a-z0-9]*(?:_[a-z0-9]+)+$", value):
        return False        # a snake_case identifier, not a value
    if re.fullmatch(r"[A-Z][A-Z0-9_]+", value):
        return False        # an environment-variable name, not a value
    return shannon(value) >= MIN_ASSIGNMENT_ENTROPY


def text_candidates(line: str, number: int) -> list[tuple[str, str, str]]:
    """(rule, value, note) for one line of text."""
    data_uri = "base64," in line or "data:image" in line or "data:font" in line
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add(rule: str, value: str, note: str = "") -> None:
        if value and value not in seen:
            found.append((rule, value, note))
            seen.add(value)

    # Machine identity first: a silicon id is the strongest fingerprint there is.
    digest_line = bool(re.search(r"(?i)sha(256|512)|digest|checksum", line))
    for match in CHIP_ID.finditer(line):
        if not digest_line:
            add("machine-identity", match.group(0), "chip id")
    for match in PARTUUID.finditer(line):
        add("machine-identity", match.group(1), "partuuid")
    for match in MACHINE_ID.finditer(line):
        add("machine-identity", match.group(1), "machine-id")
    for match in MAC.finditer(line):
        add("machine-identity", match.group(0), "MAC")
    for match in SERIAL_VALUE.finditer(line):
        add("machine-identity", match.group(1), "hardware serial")

    # Vendor resource ids, then DNS resource ids.
    for match in INSTANCE_ID.finditer(line):
        value = match.group(1)
        if value.lower() not in INSTANCE_ID_ALLOW:
            add("vendor-resource-id", value, "instance id")
    for match in HOST_NAME.finditer(line):
        value = match.group(1)
        if value.lower() not in HOST_NAME_ALLOW:
            add("vendor-resource-id", value, "host name")
    for match in TENANT_SLUG.finditer(line):
        if match.group(0) not in TENANT_SLUG_ALLOW:
            add("vendor-resource-id", match.group(0), "tenant slug")
    for match in ZONE_ID.finditer(line):
        add("dns-resource-id", match.group(1), "zone id")
    for match in RECORD_ID.finditer(line):
        add("dns-resource-id", match.group(1), "record id")

    # Key fingerprints run before high-entropy and are not blinded by sha256.
    for match in SSH_FINGERPRINT.finditer(line):
        add("ssh-fingerprint", match.group(0), "SHA256 fingerprint")
    for match in MD5_FINGERPRINT.finditer(line):
        add("ssh-fingerprint", match.group(0), "MD5 fingerprint")

    for name, pattern in TOKENS:
        for match in pattern.finditer(line):
            add(f"provider-token/{name}", match.group(0), "")
    for pattern in (QUOTED_ASSIGNMENT, ASSIGNMENT):
        for match in pattern.finditer(line):
            name, value = match.group(1), match.group(2).strip()
            if secret_assignment(name, value):
                add("credential-assignment", value, "")
    for match in TABLE_CREDENTIAL.finditer(line):
        value = match.group(1)
        if not PLACEHOLDER.match(value):
            add("credential-literal", value, "Markdown table credential")
    if not data_uri:
        for match in TOKEN.finditer(line):
            value = token_value(match.group(1))
            if value in seen or looks_like_path(value):
                continue
            if (high_entropy(value)
                    and not value.startswith(("AAAAC3", "AAAAB3", "c3NoLWVk"))
                    and not re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-", value)
                    and not re.search(r"(?i)sha(256|512)|digest|checksum|partuuid", line)):
                add("high-entropy", value, "")
    for match in IPV4.finditer(line):
        value = match.group(1)
        if value in seen or is_doc_ip(value):
            continue
        if VERSION_BEFORE.search(line[:match.start()]) or VERSION_LINE.search(line):
            continue
        add("public-ip", value, "")
    for match in DASHED_HOST.finditer(line):
        value = match.group(1)
        octets = value.split("-")
        if len(octets) != 4 or not all(part.isdigit() and int(part) <= 255 for part in octets):
            continue
        if is_doc_ip(".".join(octets)):
            continue
        add("public-ip", value, "dashed into a hostname")
    for match in IPV6.finditer(line):
        value = match.group(1)
        if value in seen or value.count(":") < 2:
            continue
        try:
            ipaddress.ip_address(value)
        except ValueError:
            continue
        if not is_doc_ipv6(value):
            add("public-ipv6", value, "")
    for match in EMAIL.finditer(line):
        domain = match.group(1).lower()
        if re.search(r"\.(service|timer|target|socket|mount|path)$", domain):
            continue
        address = match.group(0)
        if (address.lower() not in EMAIL_ALLOW
                and domain not in EMAIL_DOMAIN_ALLOW
                and not domain.endswith(".odile.test")):
            add("email", address, domain)
    for match in HOME_PATH.finditer(line):
        if match.group(1) not in HOME_ALLOW:
            add("home-path", match.group(0), match.group(1))
    for match in ROOT_PATH.finditer(line):
        segment = match.group(1).rstrip(".")
        if segment and segment not in ROOT_ALLOW:
            add("home-path", "/root/" + segment, segment)
    for match in MAC_HOME.finditer(line):
        if match.group(1) not in MAC_HOME_ALLOW:
            add("home-path", match.group(0), match.group(1))
    for match in WIN_HOME.finditer(line):
        name = match.group(1).strip()
        if name not in WIN_HOME_ALLOW:
            add("home-path", match.group(0), name)
    for match in BASIC_AUTH.finditer(line):
        value = match.group(0)
        if "<" not in value and ">" not in value:
            add("basic-auth-url", value, "")
    for pattern in (PHONE, PHONE_NATIONAL):
        for match in pattern.finditer(line):
            value = match.group(0)
            if value.replace("+", "").replace("(", "").replace(")", "").replace("-", "").replace(" ", "") not in {
                    "14155551212", "1555123456"}:
                add("phone", value, "")
    return found


def _der_tlv(data: bytes, offset: int) -> tuple[int, int, int] | None:
    """(tag, value offset, value length) for one DER TLV, or None."""
    if offset + 2 > len(data):
        return None
    tag, length = data[offset], data[offset + 1]
    header = 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or offset + 2 + count > len(data):
            return None
        length = int.from_bytes(data[offset + 2:offset + 2 + count], "big")
        header = 2 + count
    start = offset + header
    if start + length > len(data):
        return None
    return tag, start, length


def der_identifiers(data: bytes) -> list[tuple[str, str, str]]:
    """A certificate's serial number and its AMD VCEK HWID, when DER."""
    if len(data) < 64 or data[0] != 0x30:
        return []
    outer = _der_tlv(data, 0)
    if outer is None:
        return []
    tbs = _der_tlv(data, outer[1])
    if tbs is None or tbs[0] != 0x30:
        return []
    offset = tbs[1]
    first = _der_tlv(data, offset)
    if first is not None and first[0] == 0xA0:
        offset = first[1] + first[2]
    serial = _der_tlv(data, offset)
    found: list[tuple[str, str, str]] = []
    if serial is not None and serial[0] == 0x02 and 8 <= serial[2] <= 32:
        value = data[serial[1]:serial[1] + serial[2]]
        found.append(("machine-identity", value.hex(), "VCEK certificate serial"))
    oid = bytes.fromhex("2b060104019c780104")  # 1.3.6.1.4.1.3704.1.4 (HWID)
    position = data.find(oid)
    if position >= 0:
        octets = data.find(b"\x04\x40", position + len(oid), position + len(oid) + 64)
        if octets >= 0:
            hwid = data[octets + 2:octets + 66]
            if len(hwid) == 64 and any(hwid):
                found.append(("machine-identity", hwid.hex(), "VCEK HWID (chip id)"))
    return found


def report_identifiers(data: bytes) -> list[tuple[str, str, str]]:
    """A byte string that is an SEV-SNP report: its chip id at 0x1A0."""
    if len(data) < 0x2A0:
        return []
    version = int.from_bytes(data[0:4], "little")
    if version not in (2, 3, 4, 5):
        return []
    if not any(data[0x90:0xC0]):        # the measurement is never all zero
        return []
    chip = data[0x1A0:0x1E0]
    if not any(chip):
        return []
    return [("machine-identity", chip.hex(), "chip id in SEV-SNP report")]


def payload_identifiers(data: bytes) -> list[tuple[str, str, str]]:
    return report_identifiers(data) or der_identifiers(data)


def utf16_text(data: bytes) -> str | None:
    """Decode a UTF-16 payload, with or without a BOM."""
    if data[:2] == b"\xff\xfe":
        return data[2:].decode("utf-16-le", "replace")
    if data[:2] == b"\xfe\xff":
        return data[2:].decode("utf-16-be", "replace")
    sample = data[:4096]
    if len(sample) < 8 or sample.count(0) < len(sample) // 4:
        return None
    candidates = []
    for encoding in ("utf-16-le", "utf-16-be"):
        try:
            text = data.decode(encoding, "replace")
        except UnicodeError:
            continue
        printable = sum(1 for char in text if char.isprintable() or char in "\r\n\t")
        candidates.append((printable / max(len(text), 1), text))
    if not candidates:
        return None
    ratio, text = max(candidates)
    return text if ratio >= 0.85 else None


def decoded_candidates(raw: bytes, line: int, depth: int) -> list[tuple[str, int, str, str]]:
    """Scan what a base64 run decodes to: a report, a certificate, or text."""
    found: list[tuple[str, int, str, str]] = []
    payload = payload_identifiers(raw)
    for rule, value, note in payload:
        found.append((rule, line, value, note + " (base64)"))
    if payload:
        return found
    text = utf16_text(raw)
    if text is None and raw:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
    if text is not None:
        printable = sum(1 for char in text if char.isprintable() or char in "\r\n\t")
        if len(text) >= 8 and printable / len(text) >= 0.9:
            for rule, number, value, note in scan_text(text, depth + 1):
                found.append((rule, line, value, ("base64: " + note).strip()))
    elif raw:
        for rule, number, value, note in scan_bytes(raw, depth + 1):
            found.append((rule, line, value, "base64 binary"))
    return found


def scan_text(text: str, depth: int = 0) -> list[tuple[str, int, str, str]]:
    lines = text.splitlines()
    out: list[tuple[str, int, str, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        for match in INLINE_KEY.finditer(line):
            out.append(("private-key", index + 1, match.group(0), ""))
        begin = PEM_BEGIN.search(line)
        if begin:
            end = None
            for ahead in range(index + 1, min(len(lines), index + 401)):
                if PEM_END.search(lines[ahead]):
                    end = ahead
                    break
            if end is not None:
                block = "\n".join(lines[index:end + 1])
                if "PRIVATE KEY" in begin.group(1):
                    out.append(("private-key", index + 1, block, ""))
                index = end + 1
                continue
        for rule, value, note in text_candidates(line, index + 1):
            out.append((rule, index + 1, value, note))
        if depth < 2 and not any(part in line for part in ("data:image", "data:font", "base64,")):
            for match in B64_RUN.finditer(line):
                run = match.group(1)
                if len(run) % 4:
                    run += "=" * (-len(run) % 4)
                try:
                    raw = base64.b64decode(run, validate=False)
                except ValueError:
                    continue
                if len(raw) >= 32:
                    out.extend(decoded_candidates(raw, index + 1, depth))
        index += 1
    return out


def scan_bytes(data: bytes, depth: int = 0) -> list[tuple[str, int, str, str]]:
    """A binary: its payload ids, a UTF-16 payload, and its printable runs."""
    out: list[tuple[str, int, str, str]] = []
    for rule, value, note in payload_identifiers(data):
        out.append((rule, 1, value, note))
    text = utf16_text(data)
    if text is not None:
        out.extend(scan_text(text, depth))
    printable = "\n".join(m.group(0).decode("ascii", "replace") for m in PRINTABLE.finditer(data))
    out.extend(scan_text(printable, depth + 1))
    return out


def scan_archive(name: str, data: bytes) -> list[tuple[str, list[tuple[str, int, str, str]]]]:
    """Every member of a tar / zip / gzip archive, at archive!member paths."""
    out: list[tuple[str, list[tuple[str, int, str, str]]]] = []
    members = 0

    def take(member: str, blob: bytes) -> None:
        nonlocal members
        members += 1
        if members > MAX_ARCHIVE_MEMBERS or len(blob) > MAX_ARCHIVE_MEMBER_BYTES:
            return
        path = f"{name}!{member}"
        if is_binary(blob):
            out.append((path, scan_bytes(blob)))
        else:
            out.append((path, scan_text(blob.decode("utf-8", "replace"))))

    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                try:
                    blob = archive.read(info)
                except (OSError, RuntimeError, zipfile.BadZipFile):
                    continue
                take(info.filename, blob)
        return out
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                take(member.name, handle.read(MAX_ARCHIVE_MEMBER_BYTES + 1))
        return out
    except (tarfile.TarError, OSError):
        pass
    if name.endswith(".gz") or data[:2] == b"\x1f\x8b":
        try:
            blob = gzip.decompress(data)
        except OSError:
            return out
        take(name[:-3] if name.endswith(".gz") else "member", blob)
    return out


def scan_data(relative: str, data: bytes) -> list[tuple[str, list[tuple[str, int, str, str]]]]:
    """(path, candidates) for one file; archives fan out to their members."""
    if relative.endswith(ARCHIVE_SUFFIXES):
        return scan_archive(relative, data)
    if is_binary(data):
        return [(relative, scan_bytes(data))]
    return [(relative, scan_text(data.decode("utf-8", "replace")))]


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def apply(report: Report, root: Path, allows: list[Allow], today: date,
          path: str, candidates: list[tuple[str, int, str, str]]) -> None:
    for rule, line, value, note in candidates:
        hit = None
        for row in allows:
            if row.rule != rule or not fnmatch.fnmatch(path, row.path):
                continue
            if row.digest == "sha256:" + hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]:
                hit = row
                break
        if hit is not None:
            if not hit.live(today):
                report.findings.append(Finding("stale-allow", path, line, value,
                                               note=f"expired {hit.expires} (debt)"))
            elif value.strip() and value.strip() in hit.reason:
                # The reason quotes the value it waives, so the row matches its
                # own evidence and "every row matched" proves nothing.
                report.findings.append(Finding("self-satisfying", path, line, value,
                                               note="the row's reason contains its own value"))
            else:
                report.allowed += 1
            continue
        report.findings.append(Finding(rule, path, line, value, note=note))


def scan_forbidden(text: str, path: str, forbid: list[tuple[str, re.Pattern[str]]]) -> list[Finding]:
    found = []
    for label, pattern in forbid:
        for match in pattern.finditer(text):
            found.append(Finding("forbidden-value", path, text[:match.start()].count("\n") + 1,
                                 match.group(0), note=label))
    return found


def scan_tree(root: Path, allows: list[Allow], today: date,
              forbid: list[tuple[str, re.Pattern[str]]],
              skip: set[str] | None = None) -> Report:
    report = Report("tree")
    skip = skip or set()
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True).stdout.split(b"\0")
    untracked = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--others", "--exclude-standard"],
                               capture_output=True).stdout.split(b"\0")
    paths = sorted({p.decode("utf-8", "replace") for p in listed + untracked if p})
    for relative in paths:
        if relative in skip:
            continue
        if any(part in SKIP_DIRS for part in relative.split("/")):
            continue
        if relative.endswith(SKIP_SUFFIXES):
            continue
        path = root / relative
        try:
            data = path.read_bytes()
        except OSError:
            continue
        report.files += 1
        report.bytes_scanned += len(data)
        for scanned_path, candidates in scan_data(relative, data):
            apply(report, root, allows, today, scanned_path, candidates)
        if not relative.endswith(ARCHIVE_SUFFIXES):
            text = data.decode("latin-1") if is_binary(data) else data.decode("utf-8", "replace")
            report.findings.extend(scan_forbidden(text, relative, forbid))
    return report


def scan_history(root: Path, allows: list[Allow], today: date,
                 forbid: list[tuple[str, re.Pattern[str]]],
                 skip: set[str] | None = None) -> Report:
    report = Report("history")
    skip = skip or set()
    commits = [c.decode() for c in subprocess.run(
        ["git", "-C", str(root), "rev-list", "--all", "--reverse"],
        capture_output=True).stdout.split() if c]
    report.commits = len(commits)
    first: dict[str, tuple[str, str]] = {}
    for commit in commits:
        tree = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "-z", "--full-tree", commit],
                              capture_output=True).stdout
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            meta, _, raw_path = entry.partition(b"\t")
            fields = meta.split()
            if len(fields) < 3 or fields[1] != b"blob":
                continue
            first.setdefault(fields[2].decode(), (commit, raw_path.decode("utf-8", "replace")))
    report.blobs = len(first)
    process = subprocess.Popen(["git", "-C", str(root), "cat-file", "--batch"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert process.stdin and process.stdout
    try:
        for blob, (commit, path) in first.items():
            if path in skip:
                continue
            process.stdin.write(blob.encode() + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().decode().split()
            if len(header) < 3 or header[1] != "blob":
                continue
            size = int(header[2])
            data = process.stdout.read(size)
            process.stdout.read(1)
            report.bytes_scanned += size
            if path.endswith(SKIP_SUFFIXES):
                continue
            text = data.decode("latin-1") if is_binary(data) else data.decode("utf-8", "replace")
            before = len(report.findings)
            for scanned_path, candidates in scan_data(path, data):
                apply(report, root, allows, today, scanned_path, candidates)
            if not path.endswith(ARCHIVE_SUFFIXES):
                report.findings.extend(scan_forbidden(text, path, forbid))
            for finding in report.findings[before:]:
                finding.commit = commit[:7]
    finally:
        process.stdin.close()
        process.wait()
    return report


def render(report: Report) -> None:
    print(f"leak scan: {report.mode}")
    if report.mode == "history":
        print(f"  commits scanned: {report.commits:,} (git rev-list --all)")
        print(f"  unique blobs:    {report.blobs:,}")
    else:
        print(f"  files scanned:  {report.files:,}")
    print(f"  bytes scanned:  {report.bytes_scanned:,}")
    print(f"  allowlisted:    {report.allowed}")
    for finding in report.findings:
        print(finding.render())
    if report.findings:
        print(f"FAIL: {len(report.findings)} finding(s) in the {report.mode}")
        return
    where = (f"{report.commits:,} commits ({report.blobs:,} unique blobs)"
             if report.mode == "history" else f"{report.files:,} files")
    print(f"PASS: nothing to leak in {where}, {report.bytes_scanned:,} bytes scanned")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publication gate: no address, person or account.")
    parser.add_argument("--tree", action="store_true")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--root", default=".")
    parser.add_argument("--forbid-file", default=None,
                        help="file of private literals, one per line: label<TAB>regex "
                             "(default: leak-forbid.txt at the tree root; a missing "
                             "default is reported, a named file that is missing exits 2)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not (root / ".git").exists():
        print(f"leak scan: {root} is not a git checkout", file=sys.stderr)
        return 2

    allows, problems = load_allow(root)
    credentials = sum(1 for row in allows if row.credential == "yes")
    debt = sum(1 for row in allows if row.klass == "debt")
    print(f"allowlist:       {len(allows)} rows, {credentials} credentials, {debt} debt")
    forbid: list[tuple[str, re.Pattern[str]]] = []
    forbid_problems: list[str] = []
    named = args.forbid_file is not None
    forbid_name = args.forbid_file or DEFAULT_FORBID
    forbid_path = Path(forbid_name)
    if not forbid_path.is_absolute():
        forbid_path = root / forbid_path
    if forbid_path.is_file():
        labels = 0
        for number, line in enumerate(forbid_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip() or line.startswith("#"):
                continue
            label, _, pattern = line.partition("\t")
            if not label.strip() or not pattern.strip():
                forbid_problems.append(f"{forbid_name}:{number}: expected label<TAB>regex")
                continue
            forbid.append((label.strip(), re.compile(pattern)))
            labels += 1
        print(f"forbid list:     {forbid_name} ({labels} labels)")
    elif named:
        # The caller asked for this list; a scan without it would silently skip a
        # whole class, so this is a finding, not a note.
        print(f"FINDING allow-file  --forbid-file {forbid_name}: no such file")
        return 2
    else:
        # The default is absent in the private checkout, where the list lives
        # under docs/ops/. Say so, so a skip is never silent.
        print(f"forbid list:     absent ({forbid_name}) — the forbidden-value class did not run")
    skip: set[str] = set()
    try:
        skip = {forbid_path.resolve().relative_to(root).as_posix()}
    except ValueError:
        skip = set()
    today = date.today()

    modes = ["tree", "history"] if args.all else (
        [m for m, on in (("tree", args.tree), ("history", args.history)) if on] or ["tree"])

    status = 0
    for problem in problems:
        print(f"FINDING allow-file {problem}")
        status = 1
    for problem in forbid_problems:
        print(f"FINDING forbid-file {problem}")
        status = 1
    for mode in modes:
        report = (scan_tree(root, allows, today, forbid, skip) if mode == "tree"
                  else scan_history(root, allows, today, forbid, skip))
        render(report)
        if report.findings:
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
