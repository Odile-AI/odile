#!/usr/bin/python3
"""Secret scan — prove the working tree and the history carry no live credential.

The M0 exit criterion is "no secret in git". `make secrets` runs the fast tree
scan as part of `make check`; the full-history scan is one command, documented
in `docs/evidence/m0-bench.md`:

    /usr/bin/python3 tools/secret_scan.py --history

Four classes of match are looked for:

* ``pem-private-key`` — a complete PEM or OpenSSH private-key block (a BEGIN
  header, a body, a matching END), on one line (a key escaped into JSON) or
  many. A lone header line is the redaction marker several fixtures and pages
  use, not a key; nor is a complete block whose only body line is
  ``[REDACTED PRIVATE KEY]``. Both are skipped structurally, so putting a real
  key back between the same markers is a finding.
* ``provider-token`` — a credential with a known prefix: Stripe (secret,
  restricted and webhook), GitHub, GitLab, Slack, Google API, AWS access key,
  SendGrid, Anthropic, OpenAI, and a NEAR ed25519 *secret* key (the 64-byte
  form; the 32-byte public form is shorter and does not match).
* ``credential-assignment`` — a literal value of 20+ characters assigned to a
  name that ends in SECRET, TOKEN, PASSWORD, PASSWD, API_KEY or ACCESS_KEY,
  with a digit and a letter in it and enough entropy to be a key rather than a
  word, a path or a variable name.
* ``high-entropy`` — an unbroken 32–128 character token over the base64
  alphabet with Shannon entropy of at least 4.5 bits per character, mixed case
  and a digit. Pure hex (a git or sha256 digest tops out at 4.0 bits) is not
  high-entropy by this measure and is covered by the assignment rule instead.

Public material is skipped structurally rather than by allowlist: a PUBLIC KEY
or CERTIFICATE PEM block, an SSH public-key body (``AAAAC3NzaC1…`` or the
base64 ``ssh-ed25519 `` form inside a measured command line), a ``SHA256:``
fingerprint, a detached ``.sig`` signature, an inline ``data:`` asset, a
filesystem path and the base64 alphabet itself. The ``ALLOW`` table below is
the explicit allowlist the card asks for: each row names a path, a rule and why
the value is not a live credential — a public key, a test fixture, the public
demo password, or a redaction marker.

A finding never prints the value. The report carries the commit (history mode),
the path, the line and a truncated SHA-256 of the value, so a match can be
identified and compared without copying the credential into a log.

Usage:
    tools/secret_scan.py --tree       # tracked files plus untracked, not ignored
    tools/secret_scan.py --history    # every commit in `git rev-list --all`
    tools/secret_scan.py --all

Exit codes: 0 clean, 1 a finding, 2 the scan could not run.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import math
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------- limits

MIN_TOKEN = 32
MAX_TOKEN = 128
MIN_ENTROPY = 4.5
# Hex is a weaker alphabet: a random 64-hex key measures ~3.9 bits/char, so
# the assignment rule uses its own, lower floor.
MIN_ASSIGNMENT_ENTROPY = 3.3
MIN_ASSIGNMENT_LENGTH = 20

# The one string every base64 implementation carries; a table, not a secret.
B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
# The base58 alphabet, the same idea one base down (NEAR/bitcoin code carries it).
B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

SKIP_SUFFIXES = (".sig",)  # detached signature: public by construction
SKIP_DIRS = (".git", ".worktrees", ".fresh-clone", ".venv", "__pycache__", "node_modules")

# ---------------------------------------------------------------------- rules

PEM_BEGIN = re.compile(r"-----BEGIN ([A-Z0-9][A-Z0-9 ]*)-----")
PEM_END = re.compile(r"-----END ([A-Z0-9][A-Z0-9 ]*)-----")
# A key pasted into a JSON or TOML string: the newlines are literal `\n`.
INLINE_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
    r"(?:\\n|\s)+[A-Za-z0-9+/=\\n\s]{40,}"
    r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----"
)

PROVIDER_TOKENS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("stripe", re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}\b")),
    ("stripe-webhook", re.compile(r"\bwhsec_[0-9A-Za-z]{20,}\b")),
    ("github", re.compile(r"\b(?:gh[pousr]_[0-9A-Za-z]{36,}|github_pat_[0-9A-Za-z_]{22,})\b")),
    ("gitlab", re.compile(r"\bglpat-[0-9A-Za-z_-]{20,}\b")),
    ("slack", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("google-api", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("sendgrid", re.compile(r"\bSG\.[0-9A-Za-z_-]{22}\.[0-9A-Za-z_-]{43}\b")),
    ("anthropic", re.compile(r"\bsk-ant-[0-9A-Za-z_-]{20,}\b")),
    ("openai", re.compile(r"\bsk-(?:proj-)?[0-9A-Za-z_-]{32,}\b")),
    ("near-secret-key", re.compile(r"\bed25519:[1-9A-HJ-NP-Za-km-z]{80,}\b")),
)

CREDENTIAL_NAME = re.compile(
    r"(?i)^[A-Za-z0-9_.-]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|APIKEY|ACCESS_?KEY)$"
)
ASSIGNMENT = re.compile(r"(?i)\b([A-Za-z_][A-Za-z0-9_.-]{0,60})\s*[:=]\s*[\"']?([A-Za-z0-9+/=_.\-]{16,})[\"']?")
PLACEHOLDER = re.compile(
    r"(?i)^(?:"
    r"redacted.*|.*redacted|"
    r"<.*>|\$\{.*\}|\$\(.*\)|%.*|"
    r"x{4,}|\.{3,}|\*+|-+|=+|"
    r"changeme|example.*|your[-_].*|placeholder.*|dummy.*|fake.*|"
    r"none|null|true|false|todo|same|"
    r"test.*|demo.*|local.*"
    r")$"
)
TOKEN = re.compile(r"(?<![A-Za-z0-9+/=_-])([A-Za-z0-9+/=_-]{32,128})(?![A-Za-z0-9+/=_-])")
ASSIGN_PREFIX = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]{0,48})[:=](.+)$")

# Base64 of "ssh-ed25519 " (the SSH wire-format public-key prefix) and of the
# public-key blob inside a measured command line.
PUBLIC_KEY_PREFIXES = ("AAAAC3NzaC1", "c3NoLWVkMjU1MTkg")
FINGERPRINT_BEFORE = re.compile(r"(?i)(?:SHA256|MD5)\s*[:=]\s*$")


# ----------------------------------------------------------------- allowlist


@dataclass(frozen=True)
class Allow:
    """One documented exception: a path glob, a rule, and why it is public."""

    rule: str
    path: str
    reason: str
    values: tuple[str, ...] = ()  # truncated sha256 digests, when the row is narrower than a path


# The line a fixture writes between real PEM markers instead of key material.
REDACTION_BODY = "[REDACTED PRIVATE KEY]"

# Each row is a public key, a test fixture, the published demo password or a
# redaction marker — the four classes the card names. A row is deliberately
# narrow (one path, one rule); a new surface needs a new row, with a reason.
ALLOW: tuple[Allow, ...] = (
    Allow(
        "pem-private-key",
        "tests/test_claim.py",
        "history only: the pre-ODL-513 fixture block (215 chars) that ODL-513 replaced "
        "with [REDACTED PRIVATE KEY]; value-scoped, so a real key at this path is a finding",
        values=("3be4456682fb",),
    ),
    Allow(
        "pem-private-key",
        "docs/ops/evidence/ODL-513/evidence.txt",
        "the card's record quotes the redacted fixture between real OpenSSH markers; "
        "value-scoped, so a real key pasted at that path is still a finding",
        values=("a47672cb25d2",),
    ),
    Allow(
        "credential-assignment",
        "tests/test_first_visit.py",
        "test fixture: base64 of the literal 'secret-placeholder'",
    ),
    Allow(
        "high-entropy",
        "docs/evidence/odl-238/email-roundtrip.md",
        "DKIM-Signature b=/bh= values of a delivered message — public headers",
    ),
    Allow(
        "high-entropy",
        "docs/ops/evidence/mh-apply-2026-11-07/geometry.json",
        "NEAR testnet transaction id — public chain data",
    ),
    Allow(
        "high-entropy",
        "prototype/userflow.html",
        "minisign public key in the prototype's verify screen — public",
    ),
    Allow(
        "high-entropy",
        "deploy/opendkim/odile.txt",
        "DKIM public key — published in DNS",
    ),
    Allow(
        "credential-assignment",
        "deploy/image/tenant-image.pin",
        "bench fixture: the measured command line of the offline bench image carries a "
        "throwaway boot public key and a fixture setup token; the private half is shredded",
    ),
    Allow(
        "credential-assignment",
        "docs/ops/evidence/uki-cycle/*/boot-*.txt",
        "serial transcript of the scratch tenant 0badc0de: the measured command line "
        "necessarily carries its one-time setup token (already used) and the public boot key",
    ),
    Allow(
        "credential-assignment",
        "docs/ops/evidence/uki-cycle/*/cycle-*.md",
        "evidence block: it quotes the transcript head, so the measured command line "
        "carries the same one-time setup token (already used) and the public boot key",
    ),
    Allow(
        "credential-assignment",
        "docs/evidence/odl-234/file_browser.py",
        "local sim fixture: the password of the disposable ODL-234 file browser on 127.0.0.1",
    ),
    Allow(
        "credential-assignment",
        "docs/product/REVIEW-UX-2026-11-07.md",
        "the published demo password — public by design",
    ),
    Allow(
        "credential-assignment",
        "docs/ops/evidence/2026-11-08-odl221/*",
        "recovery-kit fixture: the setup token is the patterned fixture value, never live",
    ),
    Allow(
        "high-entropy",
        "docs/evidence/odl-235/*",
        "email-header evidence: DKIM signatures and Message-IDs of delivered test messages — public headers",
    ),
    Allow(
        "high-entropy",
        "docs/evidence/m0-bench.md",
        "this file's Gmail transcript: the delivered invite's public DKIM b=/bh= values",
    ),
    Allow(
        "credential-assignment",
        "deploy/demo-host/login.html",
        "the published demo password — public by design; the one-button sign-in posts "
        "it, and tests/test_demo_login_page.py fails if the two drift",
    ),
    Allow(
        "high-entropy",
        "*",
        "NEAR testnet transaction id, block hash and public key — public chain data",
        values=("333510ca0353", "d30742eb5af9", "1c91875af7ff", "58469404c965"),
    ),
    Allow(
        "high-entropy",
        "docs/runbooks/image.md",
        "history only: the scrypt hash of the published bench fixture password "
        "(odile-bench-not-a-secret), retired with the 2026.11.14-5 image; value-scoped, "
        "so a different value at this path is still a finding",
        values=("6a3d71aa28cc",),
    ),
)


# --------------------------------------------------------------------- result


@dataclass
class Candidate:
    rule: str
    line: int
    value: str


@dataclass
class Finding:
    rule: str
    path: str
    line: int
    value: str
    commit: str = ""
    blob: str = ""
    occurrences: int = 1

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.value.encode("utf-8", "replace")).hexdigest()[:12]

    def render(self) -> str:
        head = f"FINDING {self.rule} "
        if self.commit:
            head += f"commit {self.commit} "
        head += f"{self.path}:{self.line} sha256:{self.digest} len={len(self.value)}"
        if self.occurrences > 1:
            head += f" (blob {self.blob} in {self.occurrences} commits)"
        return head


@dataclass
class Report:
    mode: str
    files: int = 0
    commits: int = 0
    blobs: int = 0
    bytes_scanned: int = 0
    allowlisted: int = 0
    findings: list[Finding] = field(default_factory=list)


# ------------------------------------------------------------------- helpers


def shannon(value: str) -> float:
    counts = Counter(value)
    total = len(value)
    return -sum(n / total * math.log2(n / total) for n in counts.values())


def allowlisted(rule: str, path: str, value: str) -> bool:
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]
    for row in ALLOW:
        if row.rule != rule or not fnmatch.fnmatch(path, row.path):
            continue
        if row.values and digest not in row.values:
            continue
        return True
    return False


def token_value(token: str) -> str:
    """Strip a leading NAME= / NAME: from a token, when a value follows."""
    match = ASSIGN_PREFIX.match(token)
    if match and match.group(2):
        return match.group(2)
    return token


def looks_like_path(value: str) -> bool:
    if value.startswith("/") or value.startswith("./") or value.startswith("../"):
        return True
    # A base64 secret with two slashes is possible, but a path with two is far
    # more common; one slash stays in scope (a DKIM body hash has one).
    if value.count("/") >= 2 and "+" not in value:
        return True
    return False


def public_material(value: str, line: str, start: int) -> bool:
    """True when the token is a public key, fingerprint or path."""
    if value.startswith(PUBLIC_KEY_PREFIXES):
        return True
    if looks_like_path(value):
        return True
    if FINGERPRINT_BEFORE.search(line[:start]):
        return True
    return False


def is_high_entropy(value: str) -> bool:
    if not MIN_TOKEN <= len(value) <= MAX_TOKEN:
        return False
    if value in B64_ALPHABET or B64_ALPHABET in value:
        return False
    if value in B58_ALPHABET or B58_ALPHABET in value:
        return False
    if not (re.search(r"[A-Z]", value) and re.search(r"[a-z]", value) and re.search(r"[0-9]", value)):
        return False
    if len(set(value)) < 12:
        return False
    return shannon(value) >= MIN_ENTROPY


def is_secret_assignment(name: str, value: str) -> bool:
    if not CREDENTIAL_NAME.match(name):
        return False
    if len(value) < MIN_ASSIGNMENT_LENGTH:
        return False
    if not (re.search(r"[0-9]", value) and re.search(r"[A-Za-z]", value)):
        return False
    if PLACEHOLDER.match(value):
        return False
    if looks_like_path(value):
        return False
    return shannon(value) >= MIN_ASSIGNMENT_ENTROPY


# ------------------------------------------------------------------ scanning


def line_candidates(line: str, line_no: int) -> list[Candidate]:
    if "base64," in line or "data:image" in line or "data:font" in line:
        return []  # an inline asset, not a credential
    if "BEGIN PUBLIC KEY" in line or "BEGIN CERTIFICATE" in line:
        return []  # a public key or certificate escaped into one line (JSON/TOML)
    found: list[Candidate] = []
    seen: set[str] = set()
    for name, pattern in PROVIDER_TOKENS:
        for match in pattern.finditer(line):
            value = match.group(0)
            if value in seen:
                continue  # one credential, one finding (an Anthropic key fits OpenAI's shape too)
            found.append(Candidate(name, line_no, value))
            seen.add(value)
    for match in ASSIGNMENT.finditer(line):
        name, value = match.group(1), match.group(2)
        if value in seen:
            continue
        if is_secret_assignment(name, value):
            found.append(Candidate("credential-assignment", line_no, value))
            seen.add(value)
    for match in TOKEN.finditer(line):
        value = token_value(match.group(1))
        if value in seen:
            continue
        if public_material(value, line, match.start()):
            continue
        if is_high_entropy(value):
            found.append(Candidate("high-entropy", line_no, value))
            seen.add(value)
    return found


def scan_text(text: str) -> list[Candidate]:
    """Every candidate in one file's text, with a 1-based line number."""
    lines = text.splitlines()
    found: list[Candidate] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        for match in INLINE_PRIVATE_KEY.finditer(line):
            found.append(Candidate("pem-private-key", index + 1, match.group(0)))
        begin = PEM_BEGIN.search(line)
        if begin:
            kind = begin.group(1).strip()
            end = None
            for ahead in range(index + 1, min(len(lines), index + 401)):
                if PEM_END.search(lines[ahead]):
                    end = ahead
                    break
            if end is not None:
                block = "\n".join(lines[index : end + 1])
                body = [line.strip() for line in lines[index + 1 : end] if line.strip()]
                if "PRIVATE KEY" in kind and body != [REDACTION_BODY]:
                    found.append(Candidate("pem-private-key", index + 1, block))
                index = end + 1  # a public/certificate body is public material
                continue
        found.extend(line_candidates(line, index + 1))
        index += 1
    return found


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if is_binary(data):
        return None
    return data.decode("utf-8", "replace")


def apply_allowlist(report: Report, path: str, candidates: list[Candidate]) -> None:
    for candidate in candidates:
        if allowlisted(candidate.rule, path, candidate.value):
            report.allowlisted += 1
            continue
        report.findings.append(
            Finding(candidate.rule, path, candidate.line, candidate.value)
        )


# ---------------------------------------------------------------------- git


class ScanError(Exception):
    """The scan could not run (no git, not a checkout, unreadable tree)."""


def git(root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    except OSError as exc:
        raise ScanError(f"git is not runnable: {exc}") from exc
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ScanError(
            f"git {' '.join(args)} failed: {message[-1] if message else 'no output'}"
        )
    return result.stdout


def scan_tree(root: Path) -> Report:
    report = Report("tree")
    listed = git(root, "ls-files", "-z").split(b"\0")
    untracked = git(root, "ls-files", "-z", "--others", "--exclude-standard").split(b"\0")
    paths = sorted({p.decode("utf-8", "replace") for p in listed + untracked if p})
    for relative in paths:
        if any(part in SKIP_DIRS for part in relative.split("/")):
            continue
        if relative.endswith(SKIP_SUFFIXES):
            continue  # a detached signature, public by construction
        path = root / relative
        text = read_text(path)
        if text is None:
            continue
        report.files += 1
        report.bytes_scanned += len(text.encode("utf-8", "replace"))
        apply_allowlist(report, relative, scan_text(text))
    return report


def scan_history(root: Path) -> Report:
    report = Report("history")
    commits = [
        c.decode()
        for c in git(root, "rev-list", "--all", "--reverse").split()
        if c
    ]
    report.commits = len(commits)
    # One entry per unique blob: the oldest commit and path that introduced it.
    first_seen: dict[str, tuple[str, str]] = {}
    occurrences: Counter[str] = Counter()
    for commit in commits:
        tree = git(root, "ls-tree", "-r", "-z", "--full-tree", commit)
        seen_here: set[str] = set()
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            meta, _, raw_path = entry.partition(b"\t")
            fields = meta.split()
            if len(fields) < 3 or fields[1] != b"blob":
                continue
            blob = fields[2].decode()
            path = raw_path.decode("utf-8", "replace")
            if blob not in seen_here:  # a commit, not a tree entry
                seen_here.add(blob)
                occurrences[blob] += 1
            first_seen.setdefault(blob, (commit, path))
    report.blobs = len(first_seen)

    process = subprocess.Popen(
        ["git", "-C", str(root), "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    assert process.stdin and process.stdout
    try:
        for blob, (commit, path) in first_seen.items():
            process.stdin.write(blob.encode() + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().decode().split()
            if len(header) < 3 or header[1] != "blob":
                continue
            size = int(header[2])
            data = process.stdout.read(size)
            process.stdout.read(1)  # the newline cat-file appends
            report.bytes_scanned += size
            if is_binary(data):
                continue
            text = data.decode("utf-8", "replace")
            if path.endswith(SKIP_SUFFIXES):
                continue  # a detached signature, public by construction
            candidates = scan_text(text)
            if not candidates:
                continue
            start = len(report.findings)
            apply_allowlist(report, path, candidates)
            for finding in report.findings[start:]:
                finding.commit = commit[:7]
                finding.blob = blob[:7]
                finding.occurrences = occurrences[blob]
    finally:
        process.stdin.close()
        process.wait()
    return report


# -------------------------------------------------------------------- report


def render(report: Report, out=sys.stdout) -> None:
    print(f"secret scan: {report.mode}", file=out)
    if report.mode == "history":
        print(f"  commits scanned: {report.commits:,} (git rev-list --all)", file=out)
        print(f"  unique blobs:    {report.blobs:,}", file=out)
    else:
        print(f"  files scanned:  {report.files:,}", file=out)
    print(f"  bytes scanned:  {report.bytes_scanned:,}", file=out)
    print(f"  allowlisted:    {report.allowlisted}", file=out)
    for finding in report.findings:
        print(finding.render(), file=out)
    if report.findings:
        print(f"FAIL: {len(report.findings)} finding(s) in the {report.mode}", file=out)
        return
    where = (
        f"{report.commits:,} commits ({report.blobs:,} unique blobs)"
        if report.mode == "history"
        else f"{report.files:,} files"
    )
    print(f"PASS: no secret in {where}, {report.bytes_scanned:,} bytes scanned", file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prove the tree and its history carry no live secret.")
    parser.add_argument("--tree", action="store_true", help="scan the working tree (default)")
    parser.add_argument("--history", action="store_true", help="scan every commit in git rev-list --all")
    parser.add_argument("--all", action="store_true", help="both scans")
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    args = parser.parse_args(argv)

    modes: list[str] = []
    if args.all:
        modes = ["tree", "history"]
    else:
        if args.tree:
            modes.append("tree")
        if args.history:
            modes.append("history")
        if not modes:
            modes = ["tree"]

    root = Path(args.root).resolve()
    if not (root / ".git").exists():
        print(f"secret scan: {root} is not a git checkout", file=sys.stderr)
        return 2

    status = 0
    try:
        for mode in modes:
            report = scan_tree(root) if mode == "tree" else scan_history(root)
            render(report)
            if report.findings:
                status = 1
    except ScanError as exc:
        print(f"secret scan: {exc}", file=sys.stderr)
        return 2
    return status


if __name__ == "__main__":
    sys.exit(main())
