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
                         SECRET, TOKEN, PASSWORD, API_KEY, ACCESS_KEY.
* ``high-entropy``       an unbroken 32-128 character mixed-case token whose
                         Shannon entropy is at least 4.5 bits per character.
* ``public-ip``          an IPv4 address outside the loopback, private,
                         link-local and documentation ranges.
* ``public-ipv6``        an IPv6 address outside ``2001:db8::/32`` and ``::1``.
* ``email``              an address whose domain is not a placeholder domain.
* ``home-path``          ``/root/…`` or ``/home/<name>/…`` where the name is not
                         the service account the product ships.
* ``basic-auth-url``     a URL that carries a username and a password.
* ``phone``              an E.164 number.
* ``forbidden-value``    a literal from the list named by ``--forbid-file``: the
                         vendor account ids, box ids and checkout path that must
                         never appear in a published tree. The list itself is the
                         one file that carries them, so it is not scanned for its
                         own literals.

Allowlist discipline. Every exception is one line of ``leak-allow.tsv``:

    rule<TAB>path-glob<TAB>sha256:<12><TAB>reason<TAB>expires YYYY-MM-DD

The digest is over the matched value, so a row cannot hide a *different* value at
the same path. A row past its expiry is itself a finding. A row without a reason
is itself a finding. There is no wildcard rule, and no path-wide row.

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
import fnmatch
import hashlib
import ipaddress
import math
import re
import subprocess
import sys
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

# Addresses that are never a leak: loopback, private, link-local, documentation.
IP_ALLOW = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
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
# The service accounts the product ships — including the retired HQ demo's,
# whose home path dated records still name; every other /home/<name>/ is a person.
HOME_ALLOW = {"odile", "odile" "demo", "runner", "linuxbrew", "ubuntu"}

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
CRED_NAME = re.compile(r"(?i)^[A-Za-z0-9_.-]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY)$")
ASSIGNMENT = re.compile(r"(?i)\b([A-Za-z_][A-Za-z0-9_.-]{0,60})\s*[:=]\s*[\"']?([A-Za-z0-9+/=_.\-]{16,})[\"']?")
PLACEHOLDER = re.compile(
    r"(?i)^(?:redacted.*|.*redacted|<.*>|\$\{.*\}|\$\(.*\)|%.*|x{4,}|\.{3,}|\*+|-+|=+|"
    r"changeme|example.*|your[-_].*|placeholder.*|dummy.*|fake.*|none|null|true|false|todo|same|"
    r"test.*|demo.*|local.*)$")
TOKEN = re.compile(r"(?<![A-Za-z0-9+/=_-])([A-Za-z0-9+/=_-]{32,128})(?![A-Za-z0-9+/=_-])")
B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
IPV4 = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")
IPV6 = re.compile(r"(?<![\w:.])([0-9A-Fa-f:]{6,39})(?![\w:.])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
HOME_PATH = re.compile(r"(?<![\w.])/home/([a-z][a-z0-9_-]{0,31})/")
ROOT_PATH = re.compile(r"(?<![\w.])/root/(?=[A-Za-z0-9_-])")
BASIC_AUTH = re.compile(r"\b[a-z][a-z0-9+.-]*" + "://" + r"[^/\s:@]+:[^/\s@]+@[a-z0-9.-]+", re.I)
PHONE = re.compile(r"(?<![\d+])\+[1-9]\d{7,14}(?![\d])")
PRINTABLE = re.compile(rb"[ -~]{6,}")


@dataclass
class Allow:
    rule: str
    path: str
    digest: str
    reason: str
    expires: str

    def live(self, today: date) -> bool:
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
        if len(parts) != 5:
            problems.append(f"{ALLOW_FILE}:{number}: expected 5 tab-separated fields")
            continue
        rows.append(Allow(*(p.strip() for p in parts)))
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


def secret_assignment(name: str, value: str) -> bool:
    if not CRED_NAME.match(name) or len(value) < MIN_ASSIGNMENT_LENGTH:
        return False
    if not (re.search(r"[0-9]", value) and re.search(r"[A-Za-z]", value)):
        return False
    if PLACEHOLDER.match(value) or looks_like_path(value):
        return False
    return shannon(value) >= MIN_ASSIGNMENT_ENTROPY


def text_candidates(line: str, number: int) -> list[tuple[str, str, str]]:
    """(rule, value, note) for one line of text."""
    if "base64," in line or "data:image" in line or "data:font" in line:
        return []
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for name, pattern in TOKENS:
        for match in pattern.finditer(line):
            value = match.group(0)
            if value not in seen:
                found.append((f"provider-token/{name}", value, ""))
                seen.add(value)
    for match in ASSIGNMENT.finditer(line):
        name, value = match.group(1), match.group(2)
        if value not in seen and secret_assignment(name, value):
            found.append(("credential-assignment", value, ""))
            seen.add(value)
    for match in TOKEN.finditer(line):
        value = match.group(1)
        if value in seen or looks_like_path(value):
            continue
        if (high_entropy(value)
                and not value.startswith(("AAAAC3", "AAAAB3", "c3NoLWVk"))
                and not re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-", value)
                and not re.search(r"(?i)sha(256|512)|digest|checksum|partuuid", line)):
            found.append(("high-entropy", value, ""))
            seen.add(value)
    for match in IPV4.finditer(line):
        value = match.group(1)
        if value not in seen and not is_doc_ip(value):
            found.append(("public-ip", value, ""))
            seen.add(value)
    for match in IPV6.finditer(line):
        value = match.group(1)
        if value in seen or value.count(":") < 2:
            continue
        try:
            ipaddress.ip_address(value)
        except ValueError:
            continue
        if not is_doc_ipv6(value):
            found.append(("public-ipv6", value, ""))
            seen.add(value)
    for match in EMAIL.finditer(line):
        domain = match.group(1).lower()
        if re.search(r"\.(service|timer|target|socket|mount|path)$", domain):
            continue
        if domain not in EMAIL_DOMAIN_ALLOW and not domain.endswith(".odile.test"):
            found.append(("email", match.group(0), domain))
    for match in HOME_PATH.finditer(line):
        if match.group(1) not in HOME_ALLOW:
            found.append(("home-path", match.group(0), match.group(1)))
    for match in BASIC_AUTH.finditer(line):
        found.append(("basic-auth-url", match.group(0), ""))
    for match in PHONE.finditer(line):
        found.append(("phone", match.group(0), ""))
    return found


def scan_text(text: str) -> list[tuple[str, int, str, str]]:
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
        index += 1
    return out


def scan_bytes(data: bytes) -> list[tuple[str, int, str, str]]:
    """A binary: run the text rules over its printable runs."""
    text = "\n".join(m.group(0).decode("ascii", "replace") for m in PRINTABLE.finditer(data))
    return scan_text(text)


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
                                               note=f"expired {hit.expires}"))
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
        if is_binary(data):
            text = data.decode("latin-1")
            apply(report, root, allows, today, relative, scan_bytes(data))
            report.findings.extend(scan_forbidden(text, relative, forbid))
            continue
        text = data.decode("utf-8", "replace")
        apply(report, root, allows, today, relative, scan_text(text))
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
            apply(report, root, allows, today, path,
                  scan_bytes(data) if is_binary(data) else scan_text(text))
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
