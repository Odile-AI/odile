#!/usr/bin/python3
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
    GET  /favicon.ico             the website icon, 16/32/48
    GET  /icon-32.png             the website icon, 32
    GET  /icon-192.png            the website icon, 192
    GET  /icon-512.png            the website icon, 512
    GET  /apple-touch-icon.png    the website icon, 180 on an opaque mount

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
* **The icons are the reviewed set, served from disk.** `/verify` and the
  first-visit page link the five website-icon files by their site paths; this
  service answers exactly those five and nothing else, with the reviewed
  content types and a one-year immutable cache. The request path is a key in
  the table below, so no filename comes from the request and there is no
  traversal to guard; a missing file is a 404, not a 500.
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
# The website icon set (ODL-504/ODL-505). The installer writes these five
# files, pinned inside the signed installer, into this root; the pages link
# them by the site paths below. A request path is a key in this table — the
# filename never comes from the request — so the route cannot traverse.
DEFAULT_ICONS = "/usr/local/lib/odile/icons"
ICON_ROUTES = {
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/icon-32.png": ("icon-32.png", "image/png"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}
# The same cache the HQ routes send (ODL-504): the bytes never change under a
# name, and the name changes when the rendition does.
ICON_CACHE = "public, max-age=31536000, immutable"

# The two keys this service owns in the tenant .env; the rest of the file
# (the cookie secret) is preserved exactly.
PASSWORD_KEY = "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH"
USERNAME_KEY = "HERMES_DASHBOARD_BASIC_AUTH_USERNAME"
SECRET_KEY = "HERMES_DASHBOARD_BASIC_AUTH_SECRET"
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
    """Write the hash into the tenant .env; return the username it names.

    Only the hash line moves: the session-signing secret the first boot
    generated for this machine (``HERMES_DASHBOARD_BASIC_AUTH_SECRET``) is kept
    byte for byte, or every session cookie the owner had would be invalidated
    by setting a password. A file without a secret is refused rather than
    written: the tenant would come back with no way to sign a cookie.
    """
    try:
        original = env_path.read_text(encoding="utf-8")
        stat = env_path.stat()
    except FileNotFoundError:
        raise SystemExit("error: %s does not exist; run the installer first" % env_path)
    except OSError as exc:
        raise SystemExit("error: cannot read %s: %s" % (env_path, exc))
    if not (env_values(original).get(SECRET_KEY) or "").strip():
        raise SystemExit(
            "error: %s has no %s; the first-visit write would leave the tenant "
            "with no session secret" % (env_path, SECRET_KEY))
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
ACCESS_GRANT_TAG = "support grant"
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

    Two shapes are the script's own: five rows and a PASS/FAIL verdict, or the
    same five plus a ``support grant`` row and an OPEN verdict while a grant is
    live (t_e23f3288). A grant row without an OPEN verdict (or the reverse) is
    a bad file, not a state the page may guess at.
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
    if len(lines) not in (10, 11) or not lines[0].startswith(ACCESS_HEAD):
        return {"state": "bad"}
    at = lines[0][len(ACCESS_HEAD):]
    if not ISO_RE.match(at):
        return {"state": "bad"}
    if lines[1] != ACCESS_TITLE or lines[2] != "":
        return {"state": "bad"}
    granted = len(lines) == 11
    tags = ACCESS_TAGS + ((ACCESS_GRANT_TAG,) if granted else ())
    for index, tag in enumerate(tags):
        row = lines[3 + index]
        if tag == ACCESS_GRANT_TAG:
            if not re.match(r"^  %s +OPEN +" % re.escape(tag), row):
                return {"state": "bad"}
        elif not re.match(r"^  %s +(PASS|FAIL) +" % re.escape(tag), row):
            return {"state": "bad"}
    if lines[3 + len(tags)] != "":
        return {"state": "bad"}
    verdict = lines[4 + len(tags)]
    if not re.match(r"^  (PASS|FAIL|OPEN) \u2014 ", verdict):
        return {"state": "bad"}
    if granted != verdict.startswith("  OPEN "):
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
                 report_command: list[str] | None = None,
                 icons: Path = Path(DEFAULT_ICONS)):
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
        self.icons = icons

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

    def icon(self, path: str) -> tuple[int, str, bytes] | None:
        """One website-icon response: (status, content type, body).

        ``None`` when ``path`` is not one of the five routes, so the caller
        keeps its 404. A missing or unreadable file is a 404 too: the page's
        icon must never be the reason a request 500s. The body is the installed
        file's own bytes — the ones the signed installer pinned.
        """
        route = ICON_ROUTES.get(path)
        if route is None:
            return None
        name, ctype = route
        try:
            data = (self.icons / name).read_bytes()
        except OSError:
            return 404, "text/plain; charset=utf-8", b"not found\n"
        return 200, ctype, data

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
            self._send_bytes(code, body.encode("utf-8"), head, ctype,
                             close=close, filename=filename)

        def _send_bytes(self, code: int, data: bytes, head: bool = False,
                        ctype: str = "text/html; charset=utf-8", close: bool = False,
                        filename: str = "", cache: str = "no-store") -> None:
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
            self.send_header("Cache-Control", cache)
            if cache == "no-store":
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
            # The website icon (ODL-505): five exact paths, the reviewed set.
            # The bytes are immutable under their names, so a 200 may be
            # cached for a year; a 404 is not, or a half-written install would
            # be remembered as a missing icon.
            icon = service.icon(path)
            if icon is not None:
                code, ctype, data = icon
                return self._send_bytes(code, data, head, ctype,
                                        cache=ICON_CACHE if code == 200 else "no-store")
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
    print("first-visit on %s:%d · state %s · env %s · icons %s"
          % (service.bind, service.port, service.state, service.env, service.icons),
          flush=True)
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
    parser.add_argument("--icons", default=DEFAULT_ICONS,
                        help="the website-icon set this service serves "
                             "(default %s)" % DEFAULT_ICONS)
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
        icons=Path(args.icons),
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
