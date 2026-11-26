#!/usr/bin/python3
"""odile first-visit service — sign in to the guest and show the recovery code.

B-once (Savio, 2026-11-20; card ODL-925): the registration password is the
machine's sign-in, written into the tenant ``.env`` at provision, so the first
visit is a **login**, not a setup. The installer writes this service's state
and its unit; the user opens ``https://<slug>.odile.fyi/first-visit`` from the
ready email, signs in with the password they chose at registration, and is
shown a recovery code once, with the recovery file. A classic install with no
seed — the image's bench path and a hand-run ``--first-visit`` — keeps the
setup page: there the visit chooses the sign-in, as it did before B-once.

No first-visit link carries the setup token any more and no page accepts one:
``?t=`` answers 410 ("already set"), the stale-link answer. The setup token's
two remaining jobs are machine-side — it authenticates the seed exchange
(``{token, instance_id}`` -> ``POST /seed``, which carries the NEAR key) and
it seals the rebuild blob (``seed/seed.py``) — and neither is this service's.

    GET  /first-visit             the form: a sign-in (seeded) or a setup (classic)
    POST /first-visit             verify the password, or write the setup hash
    GET  /first-visit/recovery    the recovery page: enter the code, get the file
    POST /first-visit/recovery    verify the code (not spent), mint a download token
    GET  /first-visit/recovery?d=<download token>   the recovery file, once
    GET  /first-visit/reset       the reset form (recovery code + new sign-in)
    POST /first-visit/reset       write a new hash; rotate the session secret
    GET  /first-visit/replace     the replace form (the current sign-in)
    POST /first-visit/replace     mint a new code, show it once, push it to HQ
    GET  /health                  ok
    GET  /favicon.ico             the website icon, 16/32/48
    GET  /icon-32.png             the website icon, 32
    GET  /icon-192.png            the website icon, 192
    GET  /icon-512.png            the website icon, 512
    GET  /apple-touch-icon.png    the website icon, 180 on an opaque mount

A refused link renders one of two pages: a state with a recovery hash offers
the reset code, and a state without one (the sign-in was never set) offers the
ready email's page instead of ``/first-visit/reset``, which would answer 409
there.

Design notes a reviewer should check:

* **``signin_set_at`` is a fact about the ``.env``, not about this page.**
  ``docs/security/DECISION-first-visit-state.md`` §1: it means "the ``.env``
  holds a usable sign-in the owner can use" — the seeded first boot wrote the
  seed's verifier and stamped it, the classic page's POST wrote one, or the
  demo deploy installed one. It also picks this page's mode: set means the
  visit is a login, unset means the classic setup. ``first_login_at`` is the
  visit's own record, written beside it in one state write on success.
* **Nothing in a URL is a credential.** The setup token is not on this page
  at all: ``?t=`` (any value, valid or not) answers 410, and the form carries
  no token field. The sign-in is the password the user typed.
* **The code is never a bearer in a URL (ODL-579).** The done page shows the
  code once and links the recovery file with a **one-time download token**
  (``?d=``, 32 bytes, stored as its SHA-256, ten minutes, one use). The code is
  only ever POSTed: at ``/first-visit/recovery`` (download the file) and at
  ``/first-visit/reset`` (change the sign-in). Neither spends it.
* **The code is stable (ODL-926).** It does not rotate on use and never
  expires: one code verifies at HQ and on this machine, and a burn on one side
  cannot reach the other, so a rotation on use would leave a stale code at one
  end (docs/product/DECISION-one-recovery-code.md §2.2). It is replaced only
  on request, from this machine, at ``/first-visit/replace``: the current
  sign-in is asked for, a new code is minted from the same alphabet, its
  scrypt hash is written in one transaction, the code is shown once with the
  save-it checkbox and the recovery file, and the push to HQ is requested.
  The front-end is stable between visits; the account follows the push.
* **The push rides the heartbeat key.** The new hash is POSTed to HQ's
  ``/tenant/recovery`` by ``scripts/heartbeat.py recovery``, signed with the
  Ed25519 key the machine's first heartbeat registered — no new secret. The
  timer retries every five minutes until HQ acknowledges, so a push that failed
  while HQ was down heals without the user doing anything.
* **The recovery file is behind the code or a live download token.** The file
  holds the backup password, so neither an unauthenticated URL nor a permanent
  one may serve it. The link is same-origin, the page is
  ``Referrer-Policy: no-referrer`` and Caddy discards the access log; the
  download token is the file's own bearer, bounded by ten minutes and one use.
* **A whole-machine restore must not restore ``/etc/odile`` (A3's rule).** The
  state file holds this machine's sign-in hash and recovery-code hash; a restore
  that brought back the backup's copy would revoke the code the user was shown
  after the rebuild. A3's restore excludes it (or rotates the code afterwards);
  this service only records the rule, because the state is the anchor the code
  is verified against.
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
* **The token never leaves a hash.** The state keeps the setup token's
  SHA-256, never the plaintext; the token itself is not this page's any more
  (it is the seed badge and the rebuild-seal key). The tenant Caddyfile logs to
  ``output discard`` and the page is ``Referrer-Policy: no-referrer``.
* **One writer.** ``POST`` holds an ``flock`` on ``<state>.lock`` across
  read-verify-write, so two clicks cannot both rotate the recovery code, and no
  writer stamps ``signin_set_at`` while the ``.env`` still holds a placeholder.
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
# from `verify_and_unlock.sh --mode access --out`; the support bundle reads it.
# The tenant's /verify page is not this service's: it is `verify/verify.py`
# under `odile-verify.service` on its own loopback port (t_4fcd465f). The
# renderer below stays for HQ's demo page (`web/verify.html`), which app.py
# serves and the demo-host checks exercise.
DEFAULT_VERIFY_PAGE = "/usr/local/lib/odile/verify.html"
DEFAULT_REPORT = "/var/lib/odile/access-report.txt"
DEFAULT_ATTEST = "/var/lib/odile/attestation.json"
# After the sign-in hash replaces the placeholder, the report's sign-in line
# would still describe the old file; ask the boot oneshot to rewrite it. It is
# a separate unit, so a slow scan never delays the POST's response. `restart`,
# not `start`: the unit is RemainAfterExit=yes and already active after boot,
# so a start would be a no-op and the report would keep the placeholder line.
DEFAULT_REPORT_COMMAND = "systemctl restart --no-block odile-access-report.service"
# The replace action asks systemd to try the push now; the five-minute timer
# retries until HQ acknowledges, so a failed start is not a failed replace.
DEFAULT_PUSH_COMMAND = ""
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
# The recovery file's own bearer (ODL-579): 32 bytes, stored as SHA-256, ten
# minutes and one use. Short enough that a copied link is worth little, long
# enough that the download from the done page does not race the user.
DOWNLOAD_TTL = 600

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
    r"<!--(form|signin|signed-in|done|recover|recovery-done|reset-done|reset"
    r"|replace|replaced-done|refused-unknown|refused-set|refused)-->(.*?)<!--/\1-->", re.S)

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
        # The visit's own record (ODL-925): when the owner first signed in.
        # Written beside signin_set_at in one state write; null until then.
        "first_login_at": None,
        "recovery_hash": None,
        "recovery_issued_at": None,
        # Legacy marker (ODL-579). Kept so an older state still validates; the
        # code is stable now (ODL-926) and nothing reads this key.
        "recovery_used_at": None,
        # When HQ last acknowledged this hash (ODL-926): set by the push client
        # after a 200. A hash that does not match its `recovery_pushed_hash`
        # is pending; the five-minute timer retries until it lands.
        "recovery_pushed_at": None,
        "recovery_pushed_hash": None,
        # The recovery file's one-time bearer (ODL-579): its SHA-256, when it
        # expires, and when it was spent. All null until a page mints one.
        "download_sha256": None,
        "download_expires_at": None,
        "download_used_at": None,
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
    for key in ("used_at", "signin_set_at", "first_login_at", "recovery_issued_at",
                "recovery_used_at", "recovery_pushed_at", "download_expires_at",
                "download_used_at"):
        value = state.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError("%s is not a string or null" % key)
    download = state.get("download_sha256")
    if download is not None and not (isinstance(download, str)
                                     and TOKEN_RE.match(download)):
        raise ValueError("download_sha256 is not 64 lowercase hex or null")
    recovery = state.get("recovery_hash")
    if recovery is not None and not (isinstance(recovery, str) and recovery.startswith("scrypt$")):
        raise ValueError("recovery_hash is not an scrypt hash")
    pushed = state.get("recovery_pushed_hash")
    if pushed is not None and not (isinstance(pushed, str) and pushed.startswith("scrypt$")):
        raise ValueError("recovery_pushed_hash is not an scrypt hash")
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


def recovery_status(state: dict, raw: str) -> str:
    """'ok' or 'bad' for a code presented at either use site.

    The code is stable (ODL-926): a use does not spend it and it never expires,
    so a verifying code is always 'ok'. The legacy ``recovery_used_at`` marker
    is no longer read; an older state that carries one still accepts its code.
    """
    stored = state.get("recovery_hash")
    if not isinstance(stored, str) or not stored.startswith("scrypt$"):
        return "bad"
    normalised = normalise_code(raw)
    if not CODE_RE.match(normalised):
        return "bad"
    return "ok" if verify_password(normalised, stored) else "bad"


def mint_code(state: dict) -> str:
    """Issue the next code and make it live; returns the code to show once.

    The one rotation is a replace on request (ODL-926), not a use: the caller
    writes this hash in the same transaction that retires the old one. The
    push marker is left as it was, so the new hash reads as pending until HQ
    acknowledges it.
    """
    code = new_recovery_code()
    state["recovery_hash"] = hash_password(normalise_code(code))
    state["recovery_issued_at"] = utc_now()
    state["recovery_used_at"] = None
    return code


def push_pending(state: dict) -> bool:
    """True when the live hash is not the one HQ acknowledged."""
    stored = state.get("recovery_hash")
    return bool(stored) and state.get("recovery_pushed_hash") != stored


def issue_download(state: dict) -> str:
    """Mint the recovery file's bearer: 32 bytes, hashed, ten minutes, one use."""
    token = secrets.token_hex(32)
    state["download_sha256"] = hash_token(token)
    state["download_expires_at"] = iso_after(DOWNLOAD_TTL)
    state["download_used_at"] = None
    return token


def download_status(state: dict, raw: str) -> str:
    """'ok', 'unknown', 'used' or 'expired' for a ``?d=`` token."""
    stored = state.get("download_sha256")
    if not isinstance(stored, str) or not TOKEN_RE.match(stored):
        return "unknown"
    if not raw or not TOKEN_RE.match(raw):
        return "unknown"
    if not secrets.compare_digest(hash_token(raw), stored):
        return "unknown"
    if state.get("download_used_at"):
        return "used"
    # Both timestamps are UTC in one fixed-width format, so the string
    # comparison is the chronological one.
    expires = state.get("download_expires_at")
    if not isinstance(expires, str) or utc_now() >= expires:
        return "expired"
    return "ok"


def iso_after(seconds: int) -> str:
    return datetime.fromtimestamp(time.time() + seconds, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def recovery_file_block(recovery: RecoveryFile | None, token: str) -> str:
    """The done page's download, or the sentence that names the absence."""
    if recovery is None:
        return ('<p class="note" id="recovery-file">'
                'The recovery file is not available on this machine.</p>')
    href = "/first-visit/recovery?d=" + quote(token, safe="")
    filename = recovery_filename(recovery)
    if recovery.filled:
        holds = "the backup password and this machine's certificate fingerprint"
    else:
        holds = ("the backup password; the certificate fingerprint is pending "
                 "on this machine")
    return ('<div class="recovery-file" id="recovery-file">\n'
            '    <a class="download" href="%s" download="%s">Download your recovery file</a>\n'
            '    <p class="note">It holds %s. This link works once, for ten minutes.</p>\n'
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


def render_env(text: str, password_hash: str, secret: str | None = None) -> str:
    """Replace the hash line, and the session secret when one is given.

    A line is replaced in place; a file without the key gets it appended, so a
    hand-edited .env still converges. Comments are preserved because they are
    the installer's explanation of the allowlist. ``secret=None`` keeps the
    existing secret byte for byte.
    """
    lines = text.splitlines()
    replaced = False
    secret_replaced = secret is None
    rendered: list[str] = []
    for line in lines:
        if line.startswith(PASSWORD_KEY + "="):
            rendered.append("%s=%s" % (PASSWORD_KEY, password_hash))
            replaced = True
        elif secret is not None and line.startswith(SECRET_KEY + "="):
            rendered.append("%s=%s" % (SECRET_KEY, secret))
            secret_replaced = True
        else:
            rendered.append(line)
    if not replaced:
        rendered.append("%s=%s" % (PASSWORD_KEY, password_hash))
    if not secret_replaced:
        rendered.append("%s=%s" % (SECRET_KEY, secret))
    body = "\n".join(rendered)
    return body + "\n" if body and not body.endswith("\n") else body


def write_env_hash(env_path: Path, password_hash: str, *,
                   rotate_secret: bool = False) -> str:
    """Write the hash into the tenant .env; return the username it names.

    Setting the sign-in the first time keeps the session-signing secret the
    first boot generated for this machine
    (``HERMES_DASHBOARD_BASIC_AUTH_SECRET``) byte for byte, so the owner's
    browsers stay signed in through the password they just chose. A **reset**
    passes ``rotate_secret=True``: the same write replaces the secret with 32
    fresh bytes, so every cookie signed with the old value is refused on its
    next request — the other signed-in browsers end with the old sign-in
    (ODL-579, ADR-006 §5 defect 3). A file without a secret is refused rather
    than written: the tenant would come back with no way to sign a cookie.
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
    secret = None
    if rotate_secret:
        secret = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    owner = (stat.st_uid, stat.st_gid)
    updated = render_env(original, password_hash, secret)
    if updated != original:
        write_atomic(env_path, updated.encode("utf-8"), stat.st_mode & 0o777, owner)
    return env_values(updated).get(USERNAME_KEY) or DEFAULT_USERNAME


def stored_hash(env_path: Path) -> str:
    """The tenant .env's sign-in hash, or '' when there is none.

    Read per request, never cached: a reset writes the file while the service
    runs, and the next sign-in must verify against the new hash.
    """
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return (env_values(text).get(PASSWORD_KEY) or "").strip()


def signin_is_set(state: dict) -> bool:
    """True once the .env holds a usable sign-in (DECISION-first-visit-state §1).

    The state key is the writer's record, not a fact only this page can make:
    the seeded first boot stamps it, the classic page's POST stamps it, and a
    deploy that installs a sign-in stamps it. It picks the form this page
    serves — login when set, the classic setup when not.
    """
    return bool(state.get("signin_set_at"))


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


def notice_slot(message: str) -> str:
    """A form-level line above the button; its own id, so a field error keeps
    ``#error`` and both can announce independently."""
    if not message:
        return ""
    return '<p class="error" id="notice" role="alert">%s</p>' % html.escape(message)


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
    no one can answer. ``form`` and ``signin`` are the two ways the visit
    arrives — a classic setup or the B-once login — and both stay plain: the
    visitor has not typed anything yet.
    """
    if keep in ("form", "signin", "reset", "refused-set", "refused-unknown"):
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
           notice: str = "", token: str = "", username: str = DEFAULT_USERNAME,
           code: str = "", recovery: str = "", recovery_file: str = "",
           heading: str = "") -> str:
    """Keep one state block, drop the rest, then fill the slots.

    Values go in last, so a literal ``{{TOKEN}}`` typed by a user is never
    read back as one of our slots. The recovery-code field is the one slot a
    user can fill with braces, so it goes in after the unfilled-slot check.
    ``recovery_file`` is this service's own HTML (a link or the unavailable
    sentence), never a user value. ``notice`` is a form-level line (the rate
    limit and a refused write), never a field error. ``heading`` is the done
    page's h1: the action this visit completed (a seeded login signed in, a
    classic setup set the sign-in), never a fixed sentence.
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
    body = body.replace("<!--notice-->", notice_slot(notice))
    body = body.replace("{{CODE_ATTRS}}", field_attrs("code", error))
    body = body.replace("{{PASSWORD_ATTRS}}", field_attrs("password", error, "password-hint"))
    # The sign-in block renders no hint, so its field points at the error only.
    body = body.replace("{{SIGNIN_PASSWORD_ATTRS}}", field_attrs("password", error))
    body = body.replace("{{PASSWORD2_ATTRS}}", field_attrs("password2", error))
    body = body.replace("{{TITLE}}", html.escape(title))
    body = body.replace("{{DONE_HEADING}}", html.escape(heading))
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
                 icons: Path = Path(DEFAULT_ICONS),
                 push_command: list[str] | None = None):
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
        self.push_command = push_command or []

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

    def push(self) -> None:
        """Ask for the recovery hash to be pushed; the timer is the fallback.

        Best effort by design: a replace must not fail because HQ is down. The
        five-minute unit retries until the account acknowledges (ODL-926).
        """
        if not self.push_command:
            return
        try:
            result = subprocess.run(
                self.push_command, capture_output=True, text=True, timeout=60,
                env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                     "LANG": "C.UTF-8"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print("first-visit: recovery push not started: %s" % exc, flush=True)
            return
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            print("first-visit: recovery push not started (%d): %s"
                  % (result.returncode, detail[-1] if detail else "no output"),
                  flush=True)
            return
        print("first-visit: recovery push requested", flush=True)

    def complete_recovery(self, host: str, secure: bool) -> RecoveryFile | None:
        return complete_recovery(self.recovery, host, secure, self.caddy_stores)

    def filename(self, recovery: RecoveryFile) -> str:
        return recovery_filename(recovery)

    # -- responses -------------------------------------------------------

    def done(self, keep: str, title: str, code: str, username: str,
             host: str, secure: bool, download: str, heading: str = "") -> tuple[int, str]:
        """The one page that shows the code, and the file it unlocks once.

        ``title`` and ``heading`` name the action the visitor just completed:
        the seeded login signed in, the classic setup set the sign-in.
        """
        recovery = self.complete_recovery(host, secure)
        body = render(self.template(), keep, title=title, recovery=code,
                      username=username, heading=heading,
                      recovery_file=recovery_file_block(recovery, download))
        return 200, body

    def signed_in(self) -> tuple[int, str]:
        """A later sign-in: the code exists and is not shown again (ODL-926)."""
        body = render(self.template(), "signed-in",
                      title="You are signed in — Odile", username=self.username())
        return 200, body

    def file_done(self, keep: str, title: str, host: str, secure: bool,
                  download: str) -> tuple[int, str]:
        """The file-only done page: no code is shown, so none is implied."""
        recovery = self.complete_recovery(host, secure)
        body = render(self.template(), keep, title=title, username=self.username(),
                      recovery_file=recovery_file_block(recovery, download))
        return 200, body

    def replace_form(self, error: FieldError | None = None,
                     notice: str = "") -> tuple[int, str]:
        """The replace form: it asks for this machine's current sign-in."""
        body = render(self.template(), "replace",
                      title="Replace your recovery code — Odile", error=error,
                      notice=notice, username=self.username())
        return 200, body

    def recover_form(self, error: FieldError | None = None,
                     notice: str = "") -> tuple[int, str]:
        """The recovery page: the code is POSTed here, never carried in a URL."""
        body = render(self.template(), "recover",
                      title="Your recovery file — Odile", error=error,
                      notice=notice, username=self.username())
        return 200, body

    def refused(self, code: int, state: dict) -> tuple[int, str]:
        """The closed-link page, chosen by what the state actually holds.

        A set sign-in is the "already set" answer. When no recovery code has
        been issued yet (the seeded state before its first sign-in) the reset
        route answers 409, so the block's one action is the tokenless page that
        signs in. A machine with no sign-in at all (a classic install) points
        at the same tokenless page, which is its setup form.
        """
        if not signin_is_set(state):
            keep = "refused-unknown"
        elif state.get("recovery_hash"):
            keep = "refused"
        else:
            keep = "refused-set"
        body = render(self.template(), keep, title="This link is closed — Odile")
        return code, body

    def form(self, error: FieldError | None = None,
             notice: str = "") -> tuple[int, str]:
        """The classic setup page: no seed, so the visit chooses the sign-in."""
        body = render(self.template(), "form", title="Choose your sign-in — Odile",
                      error=error, notice=notice, username=self.username())
        return 200, body

    def signin_form(self, error: FieldError | None = None,
                    notice: str = "") -> tuple[int, str]:
        """The B-once login: one password field, no token, no repeat."""
        body = render(self.template(), "signin", title="Sign in — Odile",
                      error=error, notice=notice, username=self.username())
        return 200, body

    def reset_form(self, error: FieldError | None = None, code: str = "",
                   notice: str = "") -> tuple[int, str]:
        body = render(self.template(), "reset", title="Reset your sign-in — Odile",
                      error=error, notice=notice, username=self.username(), code=code)
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

        def _recovery_page(self, head: bool, query: dict[str, list[str]]) -> None:
            """The recovery page, or the file its one-time token names.

            ``?d=`` is the file's own bearer (32 bytes, hashed, ten minutes,
            one use); no code ever travels in a URL. Without it the page is a
            form whose POST spends the code and shows the next one once.
            """
            if "d" in query:
                return self._download_file((query.get("d") or [""])[0], head)
            state = self._load_state()
            if state is None:
                return
            if not state.get("recovery_hash"):
                code, body = service.refused(409, state)
                return self._send(code, body, head)
            code, body = service.recover_form()
            return self._send(code, body, head)

        def _download_file(self, token: str, head: bool) -> None:
            if not allow(self.client_address[0], bucket="recovery"):
                return self._send(429, "Too many attempts. Try again in ten minutes.\n",
                                  head, "text/plain; charset=utf-8")
            with locked(service.state):
                state = self._load_state()
                if state is None:
                    return
                verdict = download_status(state, token)
                if verdict == "unknown":
                    return self._send(
                        404, "The recovery file is not available on this machine.\n",
                        head, "text/plain; charset=utf-8")
                if verdict != "ok":
                    return self._send(
                        410, "This download link has been used or has expired.\n",
                        head, "text/plain; charset=utf-8")
                recovery = service.complete_recovery(self._host(), self._secure())
                if recovery is None:
                    return self._send(
                        404, "The recovery file is not available on this machine.\n",
                        head, "text/plain; charset=utf-8")
                # A HEAD answers "is it there" without spending the one use: a
                # link previewer or a scanner must not burn the owner's click.
                if not head:
                    state["download_used_at"] = utc_now()
                    save_state(service.state, state)
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
            if path in ("/first-visit/recovery", "/first-visit/recovery/"):
                return self._recovery_page(head, query)
            if path in ("/first-visit", "/first-visit/"):
                # B-once (ODL-925): no first-visit link carries the setup
                # token, and no page accepts one. Any `?t=`, valid, unknown or
                # empty, is the stale A-era link: 410, the "already set"
                # answer, and it never renders a form or writes the .env. The
                # token's jobs are machine-side now (the seed exchange and the
                # rebuild seal).
                if "t" in query:
                    state = self._load_state()
                    if state is None:
                        return
                    code, body = service.refused(410, state)
                    return self._send(code, body, head)
                state = self._load_state()
                if state is None:
                    return
                if signin_is_set(state):
                    code, body = service.signin_form()
                else:
                    code, body = service.form()
                return self._send(code, body, head)
            if path in ("/first-visit/reset", "/first-visit/reset/"):
                state = self._load_state()
                if state is None:
                    return
                if not state.get("recovery_hash"):
                    code, body = service.refused(409, state)
                    return self._send(code, body, head)
                code, body = service.reset_form()
                return self._send(code, body, head)
            if path in ("/first-visit/replace", "/first-visit/replace/"):
                state = self._load_state()
                if state is None:
                    return
                if not state.get("recovery_hash"):
                    # There is no code to replace yet: the one action is the
                    # sign-in page that mints the first one.
                    code, body = service.refused(409, state)
                    return self._send(code, body, head)
                code, body = service.replace_form()
                return self._send(code, body, head)
            return self._send(404, "not found\n", head, "text/plain; charset=utf-8")

        # -- POST --------------------------------------------------------

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if path not in ("/first-visit", "/first-visit/reset",
                            "/first-visit/recovery", "/first-visit/replace"):
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
                return self._too_many(path)
            if path == "/first-visit":
                return self._set_signin(fields)
            if path == "/first-visit/recovery":
                return self._recover_file(fields)
            if path == "/first-visit/replace":
                return self._replace_code(fields)
            return self._reset_signin(fields)

        def _too_many(self, path: str) -> None:
            """The rate limit as a page, not a bare string.

            The visitor is re-shown the state they were on (a login or the
            classic setup, a reset or the recovery page) with one form-level
            ``role="alert"`` line above the button, rather than the text/plain
            white page this route used to answer.
            """
            notice = "Too many attempts. Try again in ten minutes."
            if path == "/first-visit":
                state = self._load_state()
                if state is None:
                    return
                if signin_is_set(state):
                    code, body = service.signin_form(notice=notice)
                else:
                    code, body = service.form(notice=notice)
            elif path == "/first-visit/recovery":
                code, body = service.recover_form(notice=notice)
            elif path == "/first-visit/replace":
                code, body = service.replace_form(notice=notice)
            else:
                code, body = service.reset_form(notice=notice)
            return self._send(429, body)

        def _set_signin(self, fields: dict[str, list[str]]) -> None:
            """Sign in (seeded) or set the sign-in (classic); show the code once.

            Seeded: ``signin_set_at`` is already set because the .env holds
            the registration password's verifier, so this POST *verifies* with
            ``verify_password`` and writes no hash. A wrong password re-renders
            the one-field form and counts against the POST rate limit.
            Classic (no seed): nothing wrote a usable sign-in, so the visit
            chooses one and the hash is written, as the A-era page did.
            Both end in one state write that records ``first_login_at`` and
            ``signin_set_at`` beside the recovery code.
            """
            password = (fields.get("password") or [""])[0]
            repeat = (fields.get("password2") or [""])[0]
            with locked(service.state):
                state = self._load_state()
                if state is None:
                    return
                login = signin_is_set(state)
                if login:
                    stored = stored_hash(service.env)
                    if not stored or not verify_password(password, stored):
                        code, body = service.signin_form(FieldError(
                            "That password does not match this machine's sign-in.",
                            "password", ("password",)))
                        return self._send(code, body)
                    username = service.username()
                else:
                    problem = password_error(password, repeat)
                    if problem is not None:
                        code, body = service.form(problem)
                        return self._send(code, body)

                    password_hash = hash_password(password)
                    try:
                        username = write_env_hash(service.env, password_hash)
                        service.reload()
                    except (SystemExit, RuntimeError) as exc:
                        print("first-visit: refused: %s" % exc, file=sys.stderr, flush=True)
                        # The page, with the notice, not a bare string: the
                        # visitor can retry the same form (brand round).
                        notice = "The sign-in was not set. Try again."
                        page = (service.signin_form(notice=notice) if login
                                else service.form(notice=notice))
                        return self._send(500, page[1])

                at = utc_now()
                # The code is minted once, at the first sign-in, and is stable
                # after that (ODL-926): a later sign-in neither rotates it nor
                # shows it again. A replace is the only rotation.
                first = not state.get("recovery_hash")
                code_value = ""
                if first:
                    code_value = mint_code(state)
                state["first_login_at"] = state.get("first_login_at") or at
                # `signin_set_at` is a fact about the .env (DECISION-first-visit-
                # state §1): the seeded deploy already made it true, and a
                # classic setup makes it true with the write above. Both keys
                # land in this one write.
                state["signin_set_at"] = state.get("signin_set_at") or at
                if not login:
                    state["used_at"] = at
                # The button beside the first code carries a one-time download
                # token, so fetching the file does not spend the code the
                # visitor is being asked to save (ODL-579).
                download = issue_download(state) if first else ""
                save_state(service.state, state)
            print("first-visit: %s for %r"
                  % ("signed in" if login else "sign-in set", username), flush=True)
            # The report's sign-in line describes the .env the classic setup
            # just wrote; on the seeded path it already names the owner's hash.
            if not login:
                service.refresh_report()
            if not first:
                code, body = service.signed_in()
                return self._send(code, body)
            # The first code exists only in this response: no-store, and the
            # state keeps its scrypt hash, never the code. The push that mirrors
            # it at the account is requested now; the timer retries.
            service.push()
            # The h1 and the title name the action this visit completed. On the
            # seeded path the registration password was *verified* (a sign-in);
            # on the classic path the visit *chose* the sign-in. The code and
            # the download are identical in both, so only the two sentences move.
            if login:
                title, heading = "You are signed in — Odile", "You are signed in."
            else:
                title, heading = "Your sign-in is set — Odile", "Your sign-in is set."
            code, body = service.done("done", title,
                                      code_value, username, self._host(), self._secure(),
                                      download, heading)
            return self._send(code, body)

        def _recover_file(self, fields: dict[str, list[str]]) -> None:
            """Verify the code at the recovery page and serve the file once.

            The file holds the backup password, so the page asks for the code
            and the response carries only a one-time download token. The code is
            not spent (ODL-926): the same code opens this page, the reset and
            the account, until it is replaced.
            """
            code_value = (fields.get("code") or [""])[0]
            with locked(service.state):
                state = self._load_state()
                if state is None:
                    return
                if not state.get("recovery_hash"):
                    code, body = service.refused(409, state)
                    return self._send(code, body)
                if recovery_status(state, code_value) != "ok":
                    code, body = service.recover_form(
                        FieldError("That recovery code does not match.", "code", ("code",)))
                    return self._send(code, body)
                download = issue_download(state)
                save_state(service.state, state)
            print("first-visit: recovery file unlocked with the recovery code; "
                  "the code is unchanged", flush=True)
            code, body = service.file_done(
                "recovery-done", "Your recovery file — Odile",
                self._host(), self._secure(), download)
            return self._send(code, body)

        def _reset_signin(self, fields: dict[str, list[str]]) -> None:
            code_value = (fields.get("code") or [""])[0]
            password = (fields.get("password") or [""])[0]
            repeat = (fields.get("password2") or [""])[0]
            with locked(service.state):
                state = self._load_state()
                if state is None:
                    return
                stored = state.get("recovery_hash")
                if not isinstance(stored, str) or not stored.startswith("scrypt$"):
                    code, body = service.refused(409, state)
                    return self._send(code, body)
                if recovery_status(state, code_value) != "ok":
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
                    # The reset rotates the session secret in the same write as
                    # the hash (ADR-006 §5 defect 3): every other signed-in
                    # browser on this machine is refused on its next request.
                    username = write_env_hash(service.env, password_hash,
                                              rotate_secret=True)
                    service.reload()
                except (SystemExit, RuntimeError) as exc:
                    print("first-visit: refused: %s" % exc, file=sys.stderr, flush=True)
                    code, body = service.reset_form(
                        notice="The sign-in was not reset. Try again.")
                    return self._send(500, body)

                # The code is not spent or rotated by a reset (ODL-926); only
                # the file's one-time bearer is minted, for the download the
                # success page offers.
                download = issue_download(state)
                save_state(service.state, state)
            print("first-visit: sign-in reset with the recovery code; the code is "
                  "unchanged, the session secret rotated", flush=True)
            service.refresh_report()
            code, body = service.file_done(
                "reset-done", "Your sign-in is reset — Odile",
                self._host(), self._secure(), download)
            return self._send(code, body)

        def _replace_code(self, fields: dict[str, list[str]]) -> None:
            """Mint a new code on request, show it once, and request the push.

            The signed-in action of the one-card model (ODL-926): the current
            sign-in is presented, the new scrypt hash replaces the old one in
            one state write (so the old code stops working at this machine at
            once), the code is shown once with the checkbox and the recovery
            file, and the heartbeat-signed push to HQ is requested. HQ keeps
            the old hash until that push lands.
            """
            password = (fields.get("password") or [""])[0]
            with locked(service.state):
                state = self._load_state()
                if state is None:
                    return
                if not state.get("recovery_hash"):
                    code, body = service.refused(409, state)
                    return self._send(code, body)
                stored = stored_hash(service.env)
                if not stored or not verify_password(password, stored):
                    code, body = service.replace_form(FieldError(
                        "That password does not match this machine's sign-in.",
                        "password", ("password",)))
                    return self._send(code, body)
                username = service.username()
                code_value = mint_code(state)
                download = issue_download(state)
                save_state(service.state, state)
            print("first-visit: recovery code replaced for %r; "
                  "the old code is retired on this machine" % username, flush=True)
            service.push()
            code, body = service.done("replaced-done", "Your new recovery code — Odile",
                                      code_value, username, self._host(), self._secure(),
                                      download)
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
    parser.add_argument("--push-command", default=DEFAULT_PUSH_COMMAND,
                        help="command run when a code is minted, to push its hash "
                             "to HQ now; the five-minute timer is the fallback")
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
        "first_login_at": state.get("first_login_at"),
        "recovery_hash": ("scrypt…" if state.get("recovery_hash") else None),
        "recovery_issued_at": state["recovery_issued_at"],
        "recovery_used_at": state["recovery_used_at"],
        "recovery_pushed_at": state.get("recovery_pushed_at"),
        "recovery_pushed_hash": ("scrypt…" if state.get("recovery_pushed_hash") else None),
        "recovery_push_pending": push_pending(state),
        "download_sha256": (state.get("download_sha256") or "")[:16] + "…"
        if state.get("download_sha256") else None,
        "download_expires_at": state.get("download_expires_at"),
        "download_used_at": state.get("download_used_at"),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_path = Path(args.state)
    if args.print_state:
        print(json.dumps(summary(load_state(state_path)), indent=2))
        return 0
    reload_command = shlex.split(args.reload_command) if args.reload_command else []
    report_command = shlex.split(args.report_command) if args.report_command else []
    push_command = shlex.split(args.push_command) if args.push_command else []
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
        push_command=push_command,
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
