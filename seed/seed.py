#!/usr/bin/python3
"""odile seed service — a one-time token in, a signed tenant config out.

A freshly installed machine learns two things from the "ready" email: a 32-byte
setup token, and its own Servury instance id. It POSTs that pair to
https://odile.fyi/seed (TLS ends at Caddy) and receives the tenant config,
signed with the *seed* key. The token is stored as a SHA-256 hash, works
once, expires after 24 hours, and is bound to the tenant *and* the instance id,
so a leaked token cannot seed a different machine.

The seed key is not the release key (ODL-514). The seed key lives on HQ, signs
every tenant seed, and is handed to the unprivileged service through
`LoadCredential=`; the release key signs installers, manifests and releases and
is offline. The chain stays single-rooted: the seed service also carries
`deploy/seed/seed-key.crt`, an Ed25519 signature by the release key over the
seed public key bytes, and every seed carries the seed public key, its
`signing_key_sha256` and that certificate. The installer checks the certificate
against the release public key in the recovery kit, then the seed body against
the certified seed key. A seed with no `signing_key_sha256` still verifies
against the release key itself, so a machine installed before the split keeps
working.

The ledger has three writers, one per moment a token is created:

* `issue` — a token by hand, for a tenant that already records its instance;
* `rotate` — end every live token and mint one; the claim worker uses it;
* `bind` — mint one token *and* write its hash on the tenant row in the same
  transaction. `odilectl provision` calls it the moment the provider's create
  call returns the instance id, which is before the machine is built: Servury
  answers an order with `server_id` at once and builds in the background.

No user password, passphrase or private key is stored here or returned. The
only key-shaped values in a seed are machine API credentials (NEAR, mail relay)
and a public support key.

Each instance's NEAR key (ODL-242) arrives through a second one-shot handoff:
`odilectl provision` mints it with the provider's management API and writes it
to `/var/lib/odile/seed-keys.json` (0600) bound to the setup token's hash; the
redeem that spends the token hands the key over and removes the entry. Exactly
three paths remove an entry — that redeem, the next `provision` (which rewrites
it), and destroy — so an ordered machine that is never installed keeps its raw
key at HQ until one of them runs. A tenant with no live `tenant_keys` record
(the bench and manual shape) falls back to the config's shared `near_api_key`;
a tenant that owns a key never does: `issue`, `rotate` and `bind` re-bind the
waiting raw key to the token they mint, refuse `409 key_left_hq` when the raw
key has already left HQ, and a seed with neither key is refused `503
no_inference_key` without spending the token.

The one exception is a *rebuild* (ODL-215 follow-up): the tenant's old restic
repository password has to reach the new machine, or `backup.py init` meets the
existing repository with a new key and re-initialises. HQ still holds no
readable copy: `odilectl provision --backup-password-file` seals the password
under the token it just minted (HMAC-SHA256 keystream + tag, see
`seal_backup_password`) and writes the ciphertext to `/var/lib/odile/seed-rebuilds.json`
(`{"<slug>": {kdf, sealed, tag, token_hash}}`, 0600, owned by the service
user). The token exists only in the measured command line and in the guest; the
service stores its SHA-256. The seed carries `rebuild: {kdf, sealed, tag}` and
`odile-firstboot` opens it with the token from the same command line.

    ODILE_DB=/tmp/waitlist.db \
    ODILE_SEED_CONFIG=/tmp/seed-config.json \
    ODILE_SEED_SIGNING_KEY=/root/.odile/seed-signing.pem \
    /usr/bin/python3 seed/seed.py serve

    /usr/bin/python3 seed/seed.py init
    /usr/bin/python3 seed/seed.py keygen [--force]
    /usr/bin/python3 seed/seed.py token issue  --tenant odl-7f3a2c [--instance ID]
    /usr/bin/python3 seed/seed.py token rotate --tenant odl-7f3a2c [--dry-run]
    /usr/bin/python3 seed/seed.py token list   --tenant odl-7f3a2c
    /usr/bin/python3 seed/seed.py verify --pubkey odile-signing-key.pub --sig <b64> < seed.json
    /usr/bin/python3 seed/seed.py release-sign --file <path> --key <offline release key>
    /usr/bin/python3 seed/seed.py seed-cert --file <seed public key> --key <offline release key>

`release-sign` and `seed-cert` are the two offline-ceremony verbs behind
`make release-sign` and `make seed-cert` (docs/runbooks/seed.md). Both refuse a
key under /root: the release key does not live on the HQ box.

Environment:
    ODILE_DB                    SQLite path (default /var/lib/odile/waitlist.db)
    ODILE_SEED_BIND             bind address (default 127.0.0.1; Caddy fronts it)
    ODILE_SEED_PORT             port (default 8081)
    ODILE_SEED_CONFIG           service secrets/config, mode 0600
    ODILE_SEED_KEYS             the issued-key handoff (default /var/lib/odile/seed-keys.json)
    ODILE_SEED_SIGNING_KEY      the seed's Ed25519 private key, mode 0600
    ODILE_SEED_CERT             the release key's signature over its public half
    ODILE_SEED_RELEASE_PUBKEY   the release public key (default: the repo's, then .prev)
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_DB = "/var/lib/odile/waitlist.db"
DEFAULT_CONFIG = "/root/.odile/seed-config.json"
# The sealed rebuild entries: a small file beside the ledger, owned by the
# service user, holding ciphertext only (see `seal_backup_password`). It is
# separate from the config because systemd hands the service a *copy* of the
# config at start (LoadCredential), so a config rewrite would be invisible
# until a restart; this file is read per exchange.
DEFAULT_REBUILDS = "/var/lib/odile/seed-rebuilds.json"
# The per-instance inference key waiting for its exchange (ODL-242):
# `odilectl provision` mints the key with the provider's API and writes it
# here, bound to the setup token it just minted. The guest takes it *once*, on
# the redeem that consumes the token, and the entry is removed in the same
# exchange. The other two removals are the next `provision` (a rewrite) and
# destroy — HQ holds the raw `sk-…` from the mint until one of the three runs.
# A file beside the ledger, like `seed-rebuilds`, because systemd hands the
# service a copy of the config at start.
DEFAULT_KEYS = "/var/lib/odile/seed-keys.json"
# ODL-514: the seed key is not the release key. This one lives on HQ and signs
# tenant seeds; the release key (offline) signs installers, manifests, releases
# and the certificate over this key's public half.
DEFAULT_SIGNING_KEY = "/root/.odile/seed-signing.pem"
DEFAULT_SEED_CERT = os.path.join(ROOT, "deploy", "seed", "seed-key.crt")
DEFAULT_RELEASE_PUBKEY = os.path.join(ROOT, "installer", "odile-signing-key.pub")
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8081

MAX_BODY = 4 * 1024
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
INSTANCE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# A rebuild (ODL-215 follow-up) carries the user's OLD repository password to
# the new machine so `backup.py init` meets the existing repository with the
# right key instead of minting a new one. HQ must not hold it in the clear: the
# password is sealed under the one-time setup token, which HQ prints once and
# stores only as a SHA-256. The token is the key, so the blob in seed-config is
# ciphertext to every reader who does not have the measured command line.
REBUILD_KDF = "hmac-sha256-ctr-v1"
REBUILD_KEYSTREAM_LABEL = b"odile-rebuild-v1/keystream"
REBUILD_TAG_LABEL = b"odile-rebuild-v1/tag"
REBUILD_MIN_PASSWORD = 16
REBUILD_MAX_PASSWORD = 1024
REBUILD_SEALED_MAX = 8192          # hex chars; twice REBUILD_MAX_PASSWORD
REBUILD_TAG_HEX = 64               # HMAC-SHA256, hex

# The token ledger. `tenants` comes from app.py's schema v2; this table is
# additive and created idempotently by this service. It is separate from
# `claims` on purpose: a claim token opens the claim page, a setup token seeds
# a machine, and neither may be redeemed as the other.
SEED_SCHEMA = """
CREATE TABLE IF NOT EXISTS seed_tokens (
    id          INTEGER PRIMARY KEY,
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id),
    token_hash  TEXT    NOT NULL UNIQUE,
    instance_id TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    expires_at  TEXT    NOT NULL,
    -- set when the token is redeemed or rotated; a non-null value ends it
    used_at     TEXT,
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS seed_tokens_tenant ON seed_tokens(tenant_id);
"""


class SeedError(Exception):
    """A refusal with an HTTP status and a machine-readable reason."""

    def __init__(self, status: int, reason: str, message: str):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message


class ConfigError(Exception):
    """The service cannot start: a file is missing, unsafe or incomplete."""


# --------------------------------------------------------------------- helpers

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def db_path() -> str:
    return os.environ.get("ODILE_DB", DEFAULT_DB)


def credential_path(name: str, env_name: str, default: str) -> str:
    """Where a secret lives.

    An explicit environment variable wins (tests, `serve` run by hand). Under
    systemd, `LoadCredential=` copies a root-owned 0600 file into
    $CREDENTIALS_DIRECTORY readable by the unprivileged service user, which is
    how the service gets its config and signing key without running as root.
    """
    value = os.environ.get(env_name)
    if value:
        return value
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory:
        candidate = os.path.join(directory, name)
        if os.path.exists(candidate):
            return candidate
    return default


def cert_path() -> str:
    """The release key's signature over the seed public key (`seed-key.crt`)."""
    return os.environ.get("ODILE_SEED_CERT") or DEFAULT_SEED_CERT


def release_pubkey_paths() -> list[str]:
    """The keys a seed certificate may be certified by: the current one, then the retired one.

    `installer/odile-signing-key.prev.pub` ships for one release so the move to
    the CI key (ODL-517) does not strip the certificate from a seed served while
    the new certificate is being cut. Both files are public.
    """
    override = os.environ.get("ODILE_SEED_RELEASE_PUBKEY")
    if override:
        return [override]
    directory = os.path.dirname(DEFAULT_RELEASE_PUBKEY)
    return [DEFAULT_RELEASE_PUBKEY, os.path.join(directory, "odile-signing-key.prev.pub")]


def pem_file(data: bytes) -> str:
    """Write PEM bytes to a throwaway 0644 file for `openssl dgst -verify`.

    The bytes are written exactly as given: the signature is over these bytes,
    so a helpful trailing newline here would break the check it serves.
    """
    handle = tempfile.NamedTemporaryFile(prefix="odile-pem-", suffix=".pub", delete=False)
    try:
        handle.write(data)
        handle.flush()
    finally:
        handle.close()
    os.chmod(handle.name, 0o644)
    return handle.name


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create the v2 spine (via app.py, the single definition) and the ledger.

    Idempotent: app.migrate() and every statement below use IF NOT EXISTS.
    """
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import app  # local import: `seed.py verify` runs without the rest of the tree

    app.migrate(con)
    con.executescript(SEED_SCHEMA)
    con.commit()


def _bootrecord():
    """The measured-boot writer, imported from the tree beside this service."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import odilectl.bootrecord as bootrecord

    return bootrecord


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def new_token() -> str:
    """32 random bytes, hex. The only copy of the token is the one we return."""
    return secrets.token_hex(32)


# ------------------------------------------------------------------ rebuild seal
#
# The construction is deliberately small and standard-library only:
#
#   key        = the 64-hex setup token, as ASCII (32 bytes of entropy)
#   keystream  = HMAC-SHA256(key, "odile-rebuild-v1/keystream" || counter_be32)
#                blocks, XORed with the password's UTF-8 bytes
#   tag        = HMAC-SHA256(key, "odile-rebuild-v1/tag" || ciphertext)
#
# Encrypt-then-MAC, verify-then-decrypt, constant-time compare. No AES: the
# standard library has none, the alternative is a dependency, and a token-sealed
# 32-character password needs a keystream, not a block cipher. The guest's copy
# of `unseal` lives in deploy/image/odile-firstboot.py; tests/test_seed.py runs
# one against the other so the two cannot drift.


def _seal_key(token: str) -> bytes:
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        raise ValueError("the setup token must be 64 lowercase hex characters")
    return token.encode("ascii")


def _keystream(key: bytes, length: int) -> bytes:
    blocks = bytearray()
    counter = 0
    while len(blocks) < length:
        blocks += hmac.new(key, REBUILD_KEYSTREAM_LABEL + counter.to_bytes(4, "big"),
                           hashlib.sha256).digest()
        counter += 1
    return bytes(blocks[:length])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(left ^ right for left, right in zip(data, stream))


def seal_backup_password(password: str, token: str) -> dict:
    """Seal `password` under `token`; return the seed payload's `rebuild` object.

    The password is the user's recovery-file value (16+ characters, no
    whitespace). A wrong token later fails the tag, so a stale entry in the
    rebuilds file cannot silently hand the rebuilt machine a wrong password.
    """
    if not isinstance(password, str) or not (REBUILD_MIN_PASSWORD <= len(password)
                                             <= REBUILD_MAX_PASSWORD):
        raise ValueError("the backup password must be %d to %d characters"
                         % (REBUILD_MIN_PASSWORD, REBUILD_MAX_PASSWORD))
    if any(char.isspace() for char in password):
        raise ValueError("the backup password contains whitespace")
    key = _seal_key(token)
    data = password.encode("utf-8")
    sealed = _xor(data, _keystream(key, len(data)))
    tag = hmac.new(key, REBUILD_TAG_LABEL + sealed, hashlib.sha256).hexdigest()
    return {"kdf": REBUILD_KDF, "sealed": sealed.hex(), "tag": tag}


def unseal_backup_password(entry: dict, token: str) -> str:
    """Open a `rebuild` object with `token`; raise ValueError when it is not ours."""
    sealed, tag = validate_rebuild_entry(entry, "rebuild entry")
    key = _seal_key(token)
    expected = hmac.new(key, REBUILD_TAG_LABEL + sealed, hashlib.sha256).hexdigest()
    if not secrets.compare_digest(expected, tag):
        raise ValueError("the sealed backup password does not open with this setup token")
    return _xor(sealed, _keystream(key, len(sealed))).decode("utf-8")


BACKUP_PASSWORD_RE = re.compile(r"^Backup password:[ \t]*(\S+)[ \t]*$", re.M)


def read_recovery_password(path: str) -> str:
    """The `Backup password:` line of the user's recovery file.

    One grammar for the whole tree: `scripts/backup.py` reads it to open the
    repository, the installer writes it, and a rebuild's provision seals it.
    A missing, short or whitespace-bearing value is a `ValueError` before
    anything is ordered.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        raise ValueError("no recovery file at %s" % path) from None
    except OSError as error:
        raise ValueError("cannot read %s: %s" % (path, error.strerror)) from None
    match = BACKUP_PASSWORD_RE.search(text)
    if match is None:
        raise ValueError("%s carries no 'Backup password:' line; pass the user's "
                         "recovery file" % path)
    password = match.group(1)
    if len(password) < REBUILD_MIN_PASSWORD:
        raise ValueError("the backup password in %s is %d characters; %d or more"
                         % (path, len(password), REBUILD_MIN_PASSWORD))
    if any(char.isspace() for char in password):
        raise ValueError("the backup password in %s contains whitespace" % path)
    return password


def validate_rebuild_entry(entry: object, where: str = "seed config") -> tuple[bytes, str]:
    """The `(ciphertext, tag)` of one rebuild entry, or ConfigError.

    Called on the way in (`set_rebuild`, `load_rebuilds`) and on the way out
    (`build_seed`), so a file edited by hand cannot put a malformed or
    plaintext value into a seed. `backup_password` is refused by name: the
    rebuilds file may hold the sealed blob and nothing else.
    """
    if not isinstance(entry, dict):
        raise ConfigError("%s: rebuild entry is not a JSON object" % where)
    for plaintext in ("backup_password", "password", "passphrase"):
        if plaintext in entry:
            raise ConfigError(
                "%s: rebuild entry carries a plaintext %s; seal it under the "
                "setup token instead (odilectl provision --backup-password-file)"
                % (where, plaintext))
    if entry.get("kdf") != REBUILD_KDF:
        raise ConfigError("%s: rebuild kdf %r is not %r"
                          % (where, entry.get("kdf"), REBUILD_KDF))
    sealed_hex = entry.get("sealed")
    if not isinstance(sealed_hex, str) or not sealed_hex or len(sealed_hex) > REBUILD_SEALED_MAX:
        raise ConfigError("%s: rebuild sealed value is missing or too long" % where)
    if len(sealed_hex) % 2 or re.search(r"[^0-9a-f]", sealed_hex):
        raise ConfigError("%s: rebuild sealed value is not lowercase hex" % where)
    tag = entry.get("tag")
    if not isinstance(tag, str) or len(tag) != REBUILD_TAG_HEX or re.search(r"[^0-9a-f]", tag):
        raise ConfigError("%s: rebuild tag is not %d lowercase hex characters"
                          % (where, REBUILD_TAG_HEX))
    token_hash = entry.get("token_hash")
    if token_hash is not None and (not isinstance(token_hash, str)
                                   or not TOKEN_RE.match(token_hash)):
        raise ConfigError("%s: rebuild token_hash is not 64 lowercase hex" % where)
    return bytes.fromhex(sealed_hex), tag


def rebuild_for(rebuilds: dict, slug: str, token_hash: str) -> dict | None:
    """The sealed rebuild entry for this slug *and this token*, else None.

    Matching the token hash is what makes a stale entry harmless: a later
    provision mints a new token and rewrites the entry, and an older token's
    redeem (it cannot happen twice, but the file can be restored) gets no
    secret rather than a blob it cannot open.
    """
    entry = (rebuilds or {}).get(slug)
    if not isinstance(entry, dict):
        return None
    validate_rebuild_entry(entry, "seed rebuilds.%s" % slug)
    if entry.get("token_hash") != token_hash:
        return None
    return {"kdf": entry["kdf"], "sealed": entry["sealed"].lower(), "tag": entry["tag"].lower()}


def write_private(path: str, data: bytes, owner: tuple[int, int] | None = None,
                  mode: int = 0o600) -> None:
    """Replace `path` through a sibling temp file; the file is 0600 by default.

    `owner` is applied before the rename, so a reader never sees the file with
    the writer's owner for even one exchange (HQ writes as root; the seed
    service reads as `odile`). A public artifact (the seed certificate, the
    seed public key) passes `mode=0o644`.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", dir=directory)
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


def _safe_mode(path: str, label: str) -> None:
    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        raise ConfigError("%s %s: %s" % (label, path, exc)) from exc
    # A systemd credential directory is private to the service, so group read
    # there is the unit's business. Everywhere else, nobody but the owner may
    # read a secret.
    forbidden = 0o007
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not (directory and os.path.dirname(os.path.abspath(path)) == os.path.abspath(directory)):
        forbidden |= 0o070
    if mode & forbidden:
        raise ConfigError(
            "%s %s is mode %03o; chmod 600 (group/other readable)" % (label, path, mode & 0o777)
        )


# ---------------------------------------------------------------------- config

def load_config(path: str | None = None) -> dict:
    """Read and validate the HQ-side seed config. Secrets never leave this dict."""
    path = path or credential_path("seed-config", "ODILE_SEED_CONFIG", DEFAULT_CONFIG)
    _safe_mode(path, "seed config")
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except ValueError as exc:
        raise ConfigError("seed config %s is not valid JSON: %s" % (path, exc)) from exc
    if not isinstance(config, dict):
        raise ConfigError("seed config %s must be a JSON object" % path)

    required = {
        "model": str,
        "support_public_key": str,
        "backup_repo_url": str,
        "hermes_version": str,
        "mail_relay": dict,
    }
    for field, kind in required.items():
        value = config.get(field)
        if not isinstance(value, kind) or (kind is str and not value.strip()):
            raise ConfigError("seed config %s: field %r is missing or not a non-empty %s"
                              % (path, field, kind.__name__))
    # `near_api_key` is the shared fallback, not the source (ODL-242): every
    # instance provisioned by `odilectl` carries its own minted key from the
    # issued-key handoff, and the config's value is what a bench or a manual
    # seed uses when no key is waiting. Absent is valid; present must be a
    # non-empty string. A seed with neither is refused at exchange time, not at
    # start, so one tenant's missing key does not stop the service for others.
    shared_key = config.get("near_api_key")
    if shared_key is not None and (not isinstance(shared_key, str) or not shared_key.strip()):
        raise ConfigError(
            "seed config %s: near_api_key is present but not a non-empty string "
            "(remove it, or give the fallback key)" % path
        )
    mail = config["mail_relay"]
    for field, kind in (("host", str), ("port", int), ("username", str), ("secret", str),
                        ("tls_ca", str)):
        value = mail.get(field)
        if kind is int:
            ok = isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65536
        else:
            ok = isinstance(value, str) and bool(value.strip())
        if not ok:
            raise ConfigError("seed config %s: mail_relay.%s is missing or invalid" % (path, field))
    # ODL-469: the relay's certificate is self-signed, so the seed has to carry
    # the PEM or every tenant's brief fails at STARTTLS. A missing CA is refused
    # here, at the only place a seed is minted, rather than discovered in an inbox.
    ca = mail["tls_ca"]
    if "-----BEGIN CERTIFICATE-----" not in ca or "-----END CERTIFICATE-----" not in ca:
        raise ConfigError("seed config %s: mail_relay.tls_ca is not a PEM certificate" % path)
    return config


def rebuilds_path() -> str:
    """The sealed rebuild entries: `ODILE_SEED_REBUILDS`, else the ledger's dir."""
    return os.environ.get("ODILE_SEED_REBUILDS", DEFAULT_REBUILDS)


def load_rebuilds(path: str | None = None) -> dict:
    """The sealed `{slug: entry}` map, or {} when no rebuild has been prepared.

    The file holds only ciphertext (see `seal_backup_password`), so it is read
    by the unprivileged service user directly — no LoadCredential copy, and a
    `odilectl provision` that writes it takes effect on the next exchange.
    """
    path = path or rebuilds_path()
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise ConfigError("%s is not valid JSON: %s" % (path, exc)) from exc
    except OSError as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc)) from exc
    if not isinstance(document, dict):
        raise ConfigError("%s must be a JSON object" % path)
    for slug, entry in document.items():
        if not isinstance(slug, str) or not slug:
            raise ConfigError("%s has a non-string slug" % path)
        validate_rebuild_entry(entry, "%s.%s" % (path, slug))
    return document


def set_rebuild(path: str, slug: str, entry: dict | None) -> None:
    """Write (or remove) one tenant's sealed entry, atomically.

    `odilectl provision --backup-password-file` and the bench fixture are the
    only writers. A provision *without* the flag removes the entry: a token
    either carries a rebuild secret or it does not, and a leftover from an
    earlier attempt must not travel with a token minted for a plain install.
    The file is 0600 and, when written by root, given to the directory's owner
    (`odile`), which is the user the seed service runs as.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        document = {}
    if not isinstance(document, dict):
        raise ConfigError("%s must be a JSON object" % path)
    if entry is None:
        document.pop(slug, None)
    else:
        validate_rebuild_entry(entry, "%s.%s" % (path, slug))
        document[slug] = entry
    data = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    owner = None
    if os.geteuid() == 0:
        try:
            info = os.stat(os.path.dirname(os.path.abspath(path)) or ".")
            owner = (info.st_uid, info.st_gid)
        except OSError:
            owner = None
    write_private(path, data, owner)


def fill(value: str, slug: str) -> str:
    """`{slug}` in a config value is the one per-tenant substitution."""
    return value.replace("{slug}", slug)


def keys_path() -> str:
    """The issued-key handoff: `ODILE_SEED_KEYS`, else the ledger's directory."""
    return os.environ.get("ODILE_SEED_KEYS", DEFAULT_KEYS)


def validate_key_entry(entry: object, where: str = "seed keys") -> dict:
    """Check one issued-key entry; return it with normalised lower-case hex.

    The entry holds a raw `sk-…` from the mint until its machine's first boot
    (or until the next provision or destroy, when it is never installed), so
    its shape is checked on the way in *and* on the way out: a truncated key
    would install a machine whose every request is refused, and the seed's own
    signature would still verify.
    """
    if not isinstance(entry, dict):
        raise ConfigError("%s is not a JSON object" % where)
    key = entry.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ConfigError("%s has no non-empty 'key'" % where)
    if len(key) > 512 or any(character.isspace() for character in key):
        raise ConfigError("%s: 'key' is not one token (whitespace or too long)" % where)
    for field in ("label", "key_id", "prefix", "workspace_id"):
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError("%s has no non-empty %r" % (where, field))
    token_hash = entry.get("token_hash")
    if token_hash is not None and (not isinstance(token_hash, str)
                                   or not TOKEN_RE.match(token_hash)):
        raise ConfigError("%s: token_hash is not 64 lowercase hex" % where)
    instance = entry.get("instance_id")
    if instance is not None and (not isinstance(instance, str) or not instance.strip()):
        raise ConfigError("%s: instance_id is not a non-empty string" % where)
    out = dict(entry)
    out["key"] = key.strip()
    if isinstance(token_hash, str):
        out["token_hash"] = token_hash.lower()
    return out


def load_keys(path: str | None = None) -> dict:
    """The `{slug: entry}` map of keys waiting for their exchange, or {}."""
    path = path or keys_path()
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise ConfigError("%s is not valid JSON: %s" % (path, exc)) from exc
    except OSError as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc)) from exc
    if not isinstance(document, dict):
        raise ConfigError("%s must be a JSON object" % path)
    for slug, entry in document.items():
        if not isinstance(slug, str) or not slug:
            raise ConfigError("%s has a non-string slug" % path)
        validate_key_entry(entry, "%s.%s" % (path, slug))
    return document


def set_issued_key(path: str, slug: str, entry: dict | None) -> None:
    """Write (or remove) one tenant's waiting key, atomically, at 0600.

    `odilectl provision` is the only writer. A provision that mints a key
    rewrites the entry (the old one is revoked at the provider first); destroy
    removes it. The file is 0600 and, written by root, is given to the
    directory's owner (`odile`) — the user the service runs as.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        document = {}
    if not isinstance(document, dict):
        raise ConfigError("%s must be a JSON object" % path)
    if entry is None:
        document.pop(slug, None)
    else:
        document[slug] = validate_key_entry(entry, "%s.%s" % (path, slug))
    data = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    owner = None
    if os.geteuid() == 0:
        try:
            info = os.stat(os.path.dirname(os.path.abspath(path)) or ".")
            owner = (info.st_uid, info.st_gid)
        except OSError:
            owner = None
    write_private(path, data, owner)


def issued_key_for(keys: dict, slug: str, token_hash: str) -> dict | None:
    """The waiting key for this slug, matched to the token that will redeem it.

    Matching the token hash is what makes a leftover harmless: a key minted for
    an earlier token (a run that failed between mint and order) is not handed
    to a later one. An entry written before the token existed carries no hash
    and is taken by the slug alone — it is always the latest mint, because
    `provision` revokes the previous key before it writes a new entry.
    """
    entry = (keys or {}).get(slug)
    if not isinstance(entry, dict):
        return None
    checked = validate_key_entry(entry, "seed keys.%s" % slug)
    bound = checked.get("token_hash")
    if bound is not None and bound != token_hash:
        return None
    return checked


def take_issued_key(path: str, slug: str, token_hash: str) -> dict | None:
    """Read the waiting key and remove it in one write; None when none waits.

    Called only after the token itself has been consumed, so the key and the
    token are spent together: the machine that exchanged its token gets the one
    key minted for it, and HQ keeps no raw key afterwards. A crash between the
    two writes leaves the entry for an already-spent token, which no exchange
    can reach again (the token is single use); the next provision overwrites it
    and destroy removes it.
    """
    document = load_keys(path)
    entry = issued_key_for(document, slug, token_hash)
    if entry is None:
        return None
    document.pop(slug, None)
    data = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    owner = None
    if os.geteuid() == 0:
        try:
            info = os.stat(os.path.dirname(os.path.abspath(path)) or ".")
            owner = (info.st_uid, info.st_gid)
        except OSError:
            owner = None
    write_private(path, data, owner)
    return entry


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def key_record(con: sqlite3.Connection, slug: str) -> dict | None:
    """The tenant's live inference-key record — id, label, prefix — or None.

    `tenant_keys` belongs to the state spine (`odilectl/state.py`); a bench
    database created by this service alone has no such table, and a tenant with
    no row (or only `revoked` rows) is the manual shape whose seed may carry the
    config's shared key. An `issued` row is what makes the tenant's own key the
    one every seed must carry (ODL-242).
    """
    if not _has_table(con, "tenant_keys"):
        return None
    row = con.execute(
        "SELECT key_id, label, key_prefix FROM tenant_keys"
        " WHERE slug = ? AND state = 'issued'"
        " ORDER BY issued_at DESC, key_id DESC LIMIT 1",
        (slug,),
    ).fetchone()
    return dict(row) if row is not None else None


def waiting_key(path: str, slug: str, record: dict) -> dict:
    """The raw key waiting for this tenant's record, or a refusal naming the remedy.

    Matched on `key_id`, not on the token hash: the caller is about to mint a
    token and re-bind the entry to it. A leftover entry for an older, superseded
    mint is not the tenant's live key, so it does not count as waiting.
    """
    entry = load_keys(path).get(slug)
    if not isinstance(entry, dict) or entry.get("key_id") != record.get("key_id"):
        raise SeedError(
            409, "key_left_hq",
            "key_left_hq: no raw inference key waits for %s: its key %s is "
            "recorded, but the machine already exchanged it, so a token minted "
            "here could only seed the config's shared key; run `odilectl provision "
            "%s --apply` to mint a replacement (the machine must be installed "
            "again), then use the token it prints"
            % (slug, record.get("key_prefix") or "", slug),
        )
    return entry


def rebind_waiting_key(path: str, slug: str, record: dict, token_hash: str,
                       instance_id: str) -> dict:
    """Point the tenant's waiting raw key at the token that will redeem it.

    A second token for a tenant that already owns a key must carry that key
    (ODL-242): `odilectl provision` re-binds on its own path, and this is the
    same write for `token issue`, `token rotate` and `bind`. Refuses through
    `waiting_key` when the raw key has already left HQ; the shared fallback is
    never used for a tenant with an `issued` record.
    """
    entry = waiting_key(path, slug, record)
    updated = dict(entry, token_hash=token_hash, instance_id=instance_id)
    set_issued_key(path, slug, updated)
    return updated


def pin_key_to_token(con: sqlite3.Connection, slug: str, token_hash: str,
                     instance_id: str, keys_file: str | None = None) -> dict | None:
    """Bind the tenant's waiting key to a token this process is about to write.

    Runs *before* the ledger row exists, so a refusal (no raw key at HQ) mints
    nothing and ends nothing: the tenant keeps the token it had. Returns the
    entry it wrote, or None for a tenant with no live key record, whose seed
    keeps whatever path the caller had (the config's shared key, or a 503).
    """
    record = key_record(con, slug)
    if record is None:
        return None
    return rebind_waiting_key(keys_file or keys_path(), slug, record, token_hash,
                              instance_id)


def near_api_key(config: dict, entry: dict | None, slug: str,
                 keyed: bool = False) -> str:
    """The key this seed carries: the instance's own, else the config's shared one.

    Every instance provisioned by `odilectl` gets its own minted key (ODL-242).
    A seed with neither is refused before it is signed: a machine that boots
    with no key answers every request `401`, which reads as a broken install
    rather than a missing key.

    `keyed` says the tenant has a live key record, and then the config's shared
    key is not a fallback at all: a machine that owns a key and boots with the
    account's shared one loses its own `spendLimit` and its own revoke, so the
    exchange refuses (`503 no_inference_key`, token unspent) and names the one
    command that mints a replacement.
    """
    if entry is not None:
        return entry["key"]
    if keyed:
        raise SeedError(
            503,
            "no_inference_key",
            "%s owns its own NEAR AI key, but no raw key waits for this token; "
            "run `odilectl provision %s --apply` again (the token is not spent) "
            "and redeem the token it prints" % (slug, slug),
        )
    shared = config.get("near_api_key")
    if isinstance(shared, str) and shared.strip():
        return fill(shared, slug)
    raise SeedError(
        503,
        "no_inference_key",
        "no inference key is waiting for %s and the seed config carries no "
        "near_api_key; provision the tenant again" % slug,
    )


def build_seed(tenant: sqlite3.Row, instance_id: str, config: dict,
               rebuild: dict | None = None,
               identity: tuple[bytes, bytes] | None = None,
               near_api_key_value: str | None = None,
               sign_in_hash: str | None = None) -> dict:
    slug = tenant["slug"]
    mail = config["mail_relay"]
    seed = {
        "slug": slug,
        "instance_id": instance_id,
        "near_api_key": near_api_key_value if near_api_key_value is not None
        else fill(config.get("near_api_key") or "", slug),
        "model": fill(config["model"], slug),
        "support_public_key": fill(config["support_public_key"], slug),
        "backup_repo_url": fill(config["backup_repo_url"], slug),
        "mail_relay": {
            "host": fill(mail["host"], slug),
            "port": mail["port"],
            "username": fill(mail["username"], slug),
            "secret": fill(mail["secret"], slug),
            # ODL-469: the relay's own certificate, so the tenant can pin it.
            "tls_ca": fill(mail["tls_ca"], slug),
        },
        "hermes_version": fill(config["hermes_version"], slug),
    }
    # B-once (Savio, 2026-11-20, card t_ab499d49): the account's scrypt
    # verifier is the machine's first sign-in. It is the installer's own
    # `scrypt$n$r$p$salt$dk` string, byte-equal to `accounts.password_hash` at
    # this instant; after the seed is signed the two change separately (D-11).
    # A tenant with no account (the manual/claim path) omits the field, and
    # ODL-925's contract is what refuses a seed without it.
    if sign_in_hash:
        seed["sign_in_hash"] = sign_in_hash
    # A rebuild: the old repository password, sealed under this exchange's
    # setup token. Absent on a first install, so the guest mints one.
    if rebuild is not None:
        seed["rebuild"] = {"kdf": rebuild["kdf"], "sealed": rebuild["sealed"],
                          "tag": rebuild["tag"]}
    # ODL-514: the seed key's public half and the release key's certificate
    # over it. The installer checks the certificate against the release key in
    # the recovery kit, then this body against the certified seed key.
    public, certificate = identity if identity is not None else seed_identity()
    seed["signing_key_sha256"] = hashlib.sha256(public).hexdigest()
    seed["signing_key"] = public.decode("ascii")
    seed["signing_key_cert"] = base64.b64encode(certificate).decode("ascii")
    return seed


# --------------------------------------------------------------------- signing

def signing_key_path() -> str:
    return credential_path("seed-key", "ODILE_SEED_SIGNING_KEY", DEFAULT_SIGNING_KEY)


def sign_bytes(data: bytes, key_path: str) -> bytes:
    """Ed25519 over the exact response bytes, via the system OpenSSL.

    `openssl dgst -sign` accepts a pipe (pkeyutl -rawin insists on a seekable
    file), and its Ed25519 output is the 64-byte RFC 8032 signature.
    """
    _safe_mode(key_path, "signing key")
    proc = subprocess.run(
        ["openssl", "dgst", "-sign", key_path, "-out", "/dev/stdout"],
        input=data, capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("openssl sign failed: %s" % proc.stderr.decode("utf-8", "replace").strip())
    if len(proc.stdout) != 64:
        raise RuntimeError("openssl sign returned %d bytes, expected 64" % len(proc.stdout))
    return proc.stdout


def verify_bytes(data: bytes, signature: bytes, pubkey_path: str) -> bool:
    """True when `signature` is a valid Ed25519 signature of `data`."""
    with tempfile.NamedTemporaryFile(prefix="odile-sig-", delete=False) as f:
        f.write(signature)
        sig_path = f.name
    try:
        proc = subprocess.run(
            ["openssl", "dgst", "-verify", pubkey_path, "-signature", sig_path],
            input=data, capture_output=True,
        )
    finally:
        try:
            os.unlink(sig_path)
        except OSError:
            pass
    return proc.returncode == 0


def public_key_bytes(key_path: str) -> bytes:
    """The public half of `key_path`, PEM, exactly as `openssl pkey -pubout` writes it."""
    _safe_mode(key_path, "signing key")
    proc = subprocess.run(["openssl", "pkey", "-in", key_path, "-pubout"], capture_output=True)
    if proc.returncode != 0 or b"BEGIN PUBLIC KEY" not in proc.stdout:
        raise ConfigError("cannot read the public half of %s: %s"
                          % (key_path, proc.stderr.decode("utf-8", "replace").strip()))
    return proc.stdout


def read_certificate(path: str) -> bytes:
    """The 64 raw bytes of a seed certificate, or its base64 spelling."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise ConfigError("seed certificate %s: %s" % (path, exc.strerror)) from exc
    if len(raw) == 64:
        return raw
    compact = b"".join(raw.split())
    try:
        decoded = base64.b64decode(compact, validate=True)
    except ValueError:
        raise ConfigError(
            "seed certificate %s is neither a raw nor a base64 Ed25519 signature" % path) from None
    if len(decoded) != 64:
        raise ConfigError("seed certificate %s decodes to %d bytes, expected 64"
                          % (path, len(decoded)))
    return decoded


def seed_identity(key_path: str | None = None, cert: str | None = None,
                  pubkeys: list[str] | None = None) -> tuple[bytes, bytes]:
    """(seed public key PEM, the release key's signature over it), checked.

    Every seed carries both, so the installer can walk one chain: release key ->
    seed key -> seed body. The service refuses to start on a pair that does not
    hang together; a seed nobody can verify is worse than no seed at all.
    """
    key_path = key_path or signing_key_path()
    public = public_key_bytes(key_path)
    signature = read_certificate(cert or cert_path())
    certificate = cert or cert_path()
    candidates = [path for path in (pubkeys if pubkeys is not None else release_pubkey_paths())
                  if os.path.isfile(path)]
    if not candidates:
        raise ConfigError("no release public key to check the seed certificate against "
                          "(expected %s)" % DEFAULT_RELEASE_PUBKEY)
    for path in candidates:
        if verify_bytes(public, signature, path):
            return public, signature
    raise ConfigError(
        "seed certificate %s does not verify against %s; cut a new one with "
        "`make seed-cert` in the offline ceremony (docs/runbooks/seed.md)"
        % (certificate, ", ".join(candidates)))


def certify_seed_key(body: dict, pubkeys: list[str]) -> tuple[bytes, str]:
    """(seed public key PEM, the release key that certified it), or ConfigError.

    The certificate is the release key's signature over the seed public key
    bytes; `signing_key_sha256` names those bytes, so a substituted key cannot
    be paired with somebody else's certificate.
    """
    pem = body.get("signing_key")
    expected = body.get("signing_key_sha256")
    if not isinstance(pem, str) or "BEGIN PUBLIC KEY" not in pem:
        raise ConfigError("signing_key_sha256 is set and the seed carries no signing_key PEM")
    public = pem.encode("utf-8")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected.strip()):
        raise ConfigError("signing_key_sha256 is not a SHA-256 hex digest")
    digest = hashlib.sha256(public).hexdigest()
    if digest != expected.strip().lower():
        raise ConfigError("the seed signing key is sha256 %s, signing_key_sha256 says %s"
                          % (digest, expected.strip().lower()))
    encoded = body.get("signing_key_cert")
    if not isinstance(encoded, str) or not encoded:
        raise ConfigError("signing_key_sha256 is set and the seed carries no signing_key_cert")
    try:
        certificate = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise ConfigError("signing_key_cert is not base64") from None
    if len(certificate) != 64:
        raise ConfigError("signing_key_cert is %d bytes, expected 64" % len(certificate))
    candidates = [path for path in pubkeys if os.path.isfile(path)]
    if not candidates:
        raise ConfigError("no release public key to check the seed certificate against")
    for path in candidates:
        if verify_bytes(public, certificate, path):
            return public, path
    raise ConfigError("the seed certificate was not signed by %s" % ", ".join(candidates))


def verify_seed_bytes(data: bytes, signature: bytes, release_pubkey: str) -> tuple[bool, str]:
    """(valid, detail) for a seed body and its detached signature.

    A seed that carries `signing_key_sha256` is checked through the certificate:
    the release key certifies the seed key, the seed key signs the body. A seed
    without it (installed before ODL-514) is checked against the release key
    itself, which is what signed it.
    """
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        body = None
    if isinstance(body, dict) and "signing_key_sha256" in body:
        candidates = [release_pubkey] if release_pubkey else []
        candidates += release_pubkey_paths()
        public, certifier = certify_seed_key(body, candidates)
        path = pem_file(public)
        try:
            ok = verify_bytes(data, signature, path)
        finally:
            os.unlink(path)
        if not ok:
            raise ConfigError("the seed body does not verify against the certified seed key")
        return True, "certified by %s" % certifier
    return verify_bytes(data, signature, release_pubkey), ""


# ---------------------------------------------------------------------- ledger

def _insert_token(con: sqlite3.Connection, tenant_id: int, instance_id: str,
                  ttl_hours: int, now: datetime,
                  token: str | None = None) -> tuple[str, str, str]:
    """Write one ledger row; `token` lets the caller bind a key before the row exists."""
    token = token or new_token()
    created = iso(now)
    expires = iso(now + timedelta(hours=ttl_hours))
    con.execute(
        "INSERT INTO seed_tokens (tenant_id, token_hash, instance_id, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (tenant_id, hash_token(token), instance_id, created, expires),
    )
    return token, created, expires


def _tenant(con: sqlite3.Connection, slug: str) -> sqlite3.Row:
    row = con.execute(
        "SELECT id, slug, server_id FROM tenants WHERE slug = ?", (slug,)
    ).fetchone()
    if row is None:
        raise SeedError(404, "no_tenant", "no tenant with slug %r" % slug)
    return row


def _instance(tenant: sqlite3.Row, instance_id: str | None) -> str:
    instance = (instance_id or tenant["server_id"] or "").strip()
    if not instance:
        raise SeedError(
            409, "no_instance",
            "tenant %s has no server_id; record it at provision or pass --instance" % tenant["slug"],
        )
    if not INSTANCE_RE.match(instance):
        raise SeedError(400, "bad_instance", "instance id %r is malformed" % instance)
    return instance


def issue(con: sqlite3.Connection, slug: str, instance_id: str | None = None,
          ttl_hours: int = 24, now: datetime | None = None,
          keys_file: str | None = None) -> dict:
    """Mint one token for a tenant. The plaintext is returned here and nowhere else.

    A tenant with a live key record gets the same treatment as a provision: the
    waiting raw key is re-bound to this token (`key_left_hq` when none waits), so
    the seed this token buys carries the tenant's own key and its `spendLimit`
    (ODL-242).
    """
    now = now or utcnow()
    if ttl_hours <= 0:
        raise SeedError(400, "bad_ttl", "ttl must be a positive number of hours")
    tenant = _tenant(con, slug)
    instance = _instance(tenant, instance_id)
    token = new_token()
    pinned = pin_key_to_token(con, tenant["slug"], hash_token(token), instance, keys_file)
    _, created, expires = _insert_token(con, tenant["id"], instance, ttl_hours, now,
                                        token=token)
    con.commit()
    return {"slug": tenant["slug"], "instance_id": instance, "token": token,
            "created_at": created, "expires_at": expires, "key": pinned}


def _end_live_tokens(con: sqlite3.Connection, tenant_id: int, stamp: str) -> int:
    """Mark every live token for a tenant ended; return how many there were."""
    live = con.execute(
        "SELECT count(*) FROM seed_tokens WHERE tenant_id = ? AND used_at IS NULL AND expires_at > ?",
        (tenant_id, stamp),
    ).fetchone()[0]
    con.execute(
        "UPDATE seed_tokens SET used_at = ? WHERE tenant_id = ? AND used_at IS NULL AND expires_at > ?",
        (stamp, tenant_id, stamp),
    )
    return live


def rotate(con: sqlite3.Connection, slug: str, instance_id: str | None = None,
           ttl_hours: int = 24, dry_run: bool = False,
           now: datetime | None = None,
           keys_file: str | None = None) -> dict:
    """End every live token for a tenant and mint exactly one new one.

    A tenant with a live key record gets its waiting raw key re-bound to the new
    token, and refuses `409 key_left_hq` when none waits — before any ledger
    write, so a refused rotation leaves the token the tenant had (ODL-242). The
    dry run answers the same question without writing.
    """
    now = now or utcnow()
    if ttl_hours <= 0:
        raise SeedError(400, "bad_ttl", "ttl must be a positive number of hours")
    tenant = _tenant(con, slug)
    instance = _instance(tenant, instance_id)
    stamp = iso(now)
    if dry_run:
        live = con.execute(
            "SELECT count(*) FROM seed_tokens WHERE tenant_id = ? AND used_at IS NULL AND expires_at > ?",
            (tenant["id"], stamp),
        ).fetchone()[0]
        # The same refusal the real run would make: a dry run that promised a
        # token the next command refuses is worse than no dry run.
        record = key_record(con, tenant["slug"])
        waiting = (waiting_key(keys_file or keys_path(), tenant["slug"], record)
                   if record is not None else None)
        return {"slug": tenant["slug"], "instance_id": instance, "revoked": live,
                "token": None, "expires_at": None, "dry_run": True, "key": waiting}
    token = new_token()
    pinned = pin_key_to_token(con, tenant["slug"], hash_token(token), instance, keys_file)
    # One transaction: a rotation either ends the old token and issues the new
    # one, or changes nothing.
    live = _end_live_tokens(con, tenant["id"], stamp)
    _, created, expires = _insert_token(con, tenant["id"], instance, ttl_hours, now,
                                        token=token)
    con.commit()
    return {"slug": tenant["slug"], "instance_id": instance, "revoked": live,
            "token": token, "created_at": created, "expires_at": expires,
            "dry_run": False, "key": pinned}


def bind(con: sqlite3.Connection, slug: str, instance_id: str | None = None,
         ttl_hours: int = 24, now: datetime | None = None,
         keys_file: str | None = None) -> dict:
    """Mint one token and write both halves of the binding, atomically.

    The provision path knows the machine's instance id the moment the create
    call returns. `bind` writes the two facts that make that id usable in one
    transaction: the ledger row (bound to the instance) and
    `tenants.setup_token_hash` (the SHA-256, never the token). A reader cannot
    see one without the other, and the previous live token ends in the same
    transaction, so exactly one token works for the tenant.

    A tenant with a live key record also gets its waiting raw key re-bound to
    this token (`keys_file` names the handoff; `odilectl provision` passes the
    path its config uses), and refuses `409 key_left_hq` when none waits — before
    the ledger moves (ODL-242).

    The plaintext token is returned here and nowhere else. Callers print it
    once; a later `bind` (or `rotate`) ends it.
    """
    now = now or utcnow()
    if ttl_hours <= 0:
        raise SeedError(400, "bad_ttl", "ttl must be a positive number of hours")
    tenant = _tenant(con, slug)
    instance = _instance(tenant, instance_id)
    stamp = iso(now)
    token = new_token()
    pinned = pin_key_to_token(con, tenant["slug"], hash_token(token), instance, keys_file)
    revoked = _end_live_tokens(con, tenant["id"], stamp)
    _, created, expires = _insert_token(con, tenant["id"], instance, ttl_hours, now,
                                        token=token)
    token_hash = hash_token(token)
    con.execute(
        "UPDATE tenants SET setup_token_hash = ? WHERE id = ?", (token_hash, tenant["id"])
    )
    con.commit()
    return {"slug": tenant["slug"], "instance_id": instance, "token": token,
            "token_hash": token_hash, "created_at": created, "expires_at": expires,
            "revoked": revoked, "key": pinned}


def list_tokens(con: sqlite3.Connection, slug: str, now: datetime | None = None) -> list[dict]:
    now = now or utcnow()
    stamp = iso(now)
    tenant = _tenant(con, slug)
    rows = con.execute(
        "SELECT token_hash, instance_id, created_at, expires_at, used_at"
        " FROM seed_tokens WHERE tenant_id = ? ORDER BY id",
        (tenant["id"],),
    ).fetchall()
    out = []
    for row in rows:
        if row["used_at"] is not None:
            state = "ended"
        elif row["expires_at"] <= stamp:
            state = "expired"
        else:
            state = "live"
        out.append({
            "hash8": row["token_hash"][:8],
            "instance_id": row["instance_id"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "used_at": row["used_at"],
            "state": state,
        })
    return out


def redeem(con: sqlite3.Connection, token: str, instance_id: str,
           config: dict, rebuilds: dict | None = None,
           now: datetime | None = None,
           identity: tuple[bytes, bytes] | None = None,
           keys: dict | None = None,
           keys_file: str | None = None) -> dict:
    """Exchange a token for its seed, exactly once.

    Checks are ordered so a refused request never burns the token: only the
    final conditional UPDATE consumes it, and SQLite serialises that UPDATE, so
    two simultaneous exchanges yield one seed and one 410.

    The instance's own inference key (ODL-242) is read *before* the token is
    consumed and removed from the handoff after it, so a seed that cannot be
    built (no key anywhere) refuses with `503 no_inference_key` and leaves the
    token spendable — the machine's retry then gets its seed once provision has
    put a key in place. The key entry is matched to this token's hash, so a
    key minted for an earlier run cannot ride a later token. A tenant with a
    live `tenant_keys` record never reaches the config's shared key: with no
    waiting key the exchange refuses `503 no_inference_key` and names
    `odilectl provision`, the only command that mints a replacement.
    """
    now = now or utcnow()
    stamp = iso(now)
    token_hash = hash_token(token)
    row = con.execute(
        "SELECT id, tenant_id, instance_id, expires_at, used_at"
        " FROM seed_tokens WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    if row is None:
        raise SeedError(404, "unknown_token", "no such token")
    if row["used_at"] is not None:
        raise SeedError(410, "used", "token already used")
    if row["expires_at"] <= stamp:
        raise SeedError(410, "expired", "token expired")
    if not secrets.compare_digest(row["instance_id"], instance_id):
        raise SeedError(403, "wrong_instance", "token is bound to another instance")
    tenant = con.execute(
        "SELECT slug, server_id FROM tenants WHERE id = ?", (row["tenant_id"],)
    ).fetchone()
    if tenant is None:
        raise SeedError(500, "no_tenant", "tenant record disappeared")
    issued = issued_key_for(keys or {}, tenant["slug"], token_hash)
    key = near_api_key(config, issued, tenant["slug"],
                       keyed=key_record(con, tenant["slug"]) is not None)
    # The account's verifier travels as the machine's first sign-in (ODL-341).
    # Read at signing time, so the seed and the account agree byte for byte; a
    # tenant with no account row (the manual path) omits the field.
    account = con.execute(
        "SELECT password_hash FROM accounts WHERE slug = ?", (tenant["slug"],)
    ).fetchone()
    seed = build_seed(tenant, row["instance_id"], config,
                      rebuild=rebuild_for(rebuilds or {}, tenant["slug"], token_hash),
                      identity=identity,
                      near_api_key_value=key,
                      sign_in_hash=account["password_hash"] if account is not None else None)
    cur = con.execute(
        "UPDATE seed_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL AND expires_at > ?",
        (stamp, row["id"], stamp),
    )
    con.commit()
    if cur.rowcount != 1:
        raise SeedError(410, "used", "token already used")
    if issued is not None:
        # The key travelled; HQ must not keep a copy. A failure here is loud
        # but does not un-spend the token: the seed above is already built.
        take_issued_key(keys_file or keys_path(), tenant["slug"], token_hash)
    return seed


def record_for_token(con: sqlite3.Connection, token: str, instance_id: str,
                     record: dict, now: datetime | None = None) -> dict:
    """Write the measured boot record a spent token's machine reported.

    The seed exchange is single-use; the record is not. A machine that redeemed
    its token (the first boot installed), then reports what it booted, so the
    row carries the measured throwaway key, the pinned release and the state the
    machine is in. The token is the bearer and the instance binding is the
    scope: a record for another instance is refused, and a token that was never
    exchanged has no boot to describe. The record is idempotent — a reboot
    sends it again and the writer changes nothing it already holds, the row's
    `recorded_at` included (it is the first record's stamp, not the replay's).
    """
    if not isinstance(record, dict):
        raise SeedError(400, "bad_record", "the boot record must be a JSON object")
    now = now or utcnow()
    stamp = iso(now)
    row = con.execute(
        "SELECT id, tenant_id, instance_id, expires_at, used_at"
        " FROM seed_tokens WHERE token_hash = ?",
        (hash_token(token),),
    ).fetchone()
    if row is None:
        raise SeedError(404, "unknown_token", "no such token")
    if row["used_at"] is None:
        raise SeedError(409, "not_redeemed", "the seed has not been exchanged")
    if row["expires_at"] <= stamp:
        raise SeedError(410, "expired", "token expired")
    if not secrets.compare_digest(row["instance_id"], instance_id):
        raise SeedError(403, "wrong_instance", "token is bound to another instance")
    tenant = con.execute(
        "SELECT id, slug FROM tenants WHERE id = ?", (row["tenant_id"],)
    ).fetchone()
    if tenant is None:
        raise SeedError(500, "no_tenant", "tenant record disappeared")
    bootrecord = _bootrecord()
    try:
        parsed = bootrecord.parse_record(
            record, instance_id=row["instance_id"], slug=tenant["slug"]
        )
        return bootrecord.record_boot(
            con, slug=tenant["slug"], record=parsed, source="first_boot", now=now
        )
    except bootrecord.BootRecordError as error:
        raise SeedError(400, "bad_record", str(error)) from None


# ---------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    server_version = "odile-seed"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # no access log, by design: the token travels in the body, but silence is
    # still one less place for it to leak
    def log_message(self, fmt, *args):
        pass

    def send_response(self, code, message=None):
        self.log_request(code)
        self.send_response_only(code, message)
        self.send_header("Date", self.date_time_string())

    def _send(self, code: int, data: bytes, ctype: str, headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code: int, payload: dict, headers: dict | None = None):
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        self._send(code, data, "application/json; charset=utf-8", headers)

    def _text(self, code: int, text: str):
        self._send(code, text.encode("utf-8"), "text/plain; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._text(200, "ok\n")
        if path == "/seed":
            return self._text(405, "method not allowed: POST a token\n")
        return self._text(404, "not found\n")

    def do_HEAD(self):
        if self.path.split("?", 1)[0] == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "3")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/seed":
            return self._text(404, "not found\n")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            return self._json(413, {"error": "request too large", "reason": "too_large"})
        if length <= 0:
            return self._json(400, {"error": "empty request body", "reason": "bad_request"})
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._json(400, {"error": "body must be JSON", "reason": "bad_request"})
        if not isinstance(payload, dict):
            return self._json(400, {"error": "body must be a JSON object", "reason": "bad_request"})
        token = payload.get("token")
        instance_id = payload.get("instance_id")
        if not isinstance(token, str) or not TOKEN_RE.match(token):
            return self._json(400, {"error": "token must be 64 lowercase hex characters",
                                    "reason": "bad_token"})
        if not isinstance(instance_id, str) or not INSTANCE_RE.match(instance_id):
            return self._json(400, {"error": "instance_id is missing or malformed",
                                    "reason": "bad_instance"})
        extra = sorted(set(payload) - {"token", "instance_id", "record"})
        if extra:
            return self._json(400, {"error": "unknown field(s): %s" % ", ".join(extra),
                                    "reason": "bad_request"})

        if "record" in payload:
            con = connect(getattr(self.server, "db_path", db_path()))
            try:
                written = record_for_token(con, token, instance_id, payload["record"])
            except SeedError as exc:
                return self._json(exc.status, {"error": exc.message, "reason": exc.reason})
            except Exception as exc:  # fail loudly, keep the detail in the log
                print("seed: internal error: %r" % (exc,), file=sys.stderr, flush=True)
                return self._json(500, {"error": "internal error", "reason": "internal"})
            finally:
                con.close()
            return self._json(200, {
                "ok": True,
                "recorded": True,
                "slug": written["slug"],
                "state": written["state"],
                "tenants_state": written["tenants_state"],
                "release": written["release"],
                "boot_key_fingerprint": written["boot_key_fingerprint"],
                "first_visit": written["first_visit"],
                "changed": written["changed"],
            })

        con = connect(getattr(self.server, "db_path", db_path()))
        try:
            # The rebuild entries are read per exchange, not once at start: a
            # rebuild's sealed entry is written by `odilectl provision` between
            # two requests, and a service that cached the file would hand the
            # guest a seed without it. One small 0600 file per redeem is
            # nothing. (`rebuilds_path` is set by the in-process test server;
            # `serve()` leaves it unset and the env/default decide.)
            seed = redeem(con, token, instance_id,
                          load_config(getattr(self.server, "config_path", None)),
                          load_rebuilds(getattr(self.server, "rebuilds_path", None)),
                          identity=getattr(self.server, "identity", None),
                          keys=load_keys(getattr(self.server, "keys_path", None)),
                          keys_file=getattr(self.server, "keys_path", None))
        except ConfigError as exc:
            print("seed: config error: %s" % exc, file=sys.stderr, flush=True)
            return self._json(500, {"error": "the seed config or its rebuild entries are invalid",
                                    "reason": "bad_config"})
        except SeedError as exc:
            return self._json(exc.status, {"error": exc.message, "reason": exc.reason})
        except Exception as exc:  # fail loudly, keep the detail in the log
            print("seed: internal error: %r" % (exc,), file=sys.stderr, flush=True)
            return self._json(500, {"error": "internal error", "reason": "internal"})
        finally:
            con.close()

        body = json.dumps(seed, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            signature = sign_bytes(body, self.server.signing_key)
        except Exception as exc:
            print("seed: signing failed: %r" % (exc,), file=sys.stderr, flush=True)
            return self._json(500, {"error": "signing failed", "reason": "internal"})
        self._send(
            200, body, "application/json; charset=utf-8",
            {"X-Odile-Signature": "ed25519=" + base64.b64encode(signature).decode("ascii")},
        )


def serve() -> int:
    config = load_config()
    key_path = signing_key_path()
    _safe_mode(key_path, "signing key")
    identity = seed_identity(key_path)

    con = connect()
    try:
        ensure_schema(con)
    finally:
        con.close()

    bind = os.environ.get("ODILE_SEED_BIND", DEFAULT_BIND)
    port = int(os.environ.get("ODILE_SEED_PORT", str(DEFAULT_PORT)))
    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    server.config = config
    server.signing_key = key_path
    server.identity = identity
    server.db_path = db_path()
    print("odile seed on %s:%d · db %s · signing key %s · certificate %s"
          % (bind, port, db_path(), key_path, cert_path()), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# ------------------------------------------------------------------------- cli

def _fail(message: str, code: int = 2) -> int:
    print("seed: %s" % message, file=sys.stderr)
    return code


def cmd_init(_args) -> int:
    con = connect()
    try:
        ensure_schema(con)
    finally:
        con.close()
    print("seed: schema ready in %s" % db_path())
    return 0


def cmd_keygen(args) -> int:
    out = args.out or signing_key_path()
    pub = args.pub or (out + ".pub")
    if os.path.exists(out) and not args.force:
        return _fail("%s exists; pass --force to replace it" % out, 2)
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    proc = subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", out],
                          capture_output=True)
    if proc.returncode != 0:
        return _fail("openssl keygen failed: %s" % proc.stderr.decode("utf-8", "replace").strip())
    os.chmod(out, 0o600)
    proc = subprocess.run(["openssl", "pkey", "-in", out, "-pubout", "-out", pub],
                          capture_output=True)
    if proc.returncode != 0:
        return _fail("openssl pubkey failed: %s" % proc.stderr.decode("utf-8", "replace").strip())
    os.chmod(pub, 0o644)
    with open(pub, "rb") as f:
        fingerprint = hashlib.sha256(f.read()).hexdigest()
    print("key:         %s (0600)" % out)
    print("public:      %s (0644)" % pub)
    print("fingerprint: sha256:%s" % fingerprint)
    return 0


def _key_line(result: dict, verb: str) -> str | None:
    """One line naming the tenant's own key the new token carries, or None.

    A bench tenant has no key record, so nothing is printed; a keyed tenant's
    token always carries its own key, which is the fact an operator needs to see
    in the transcript (`odilectl provision` says the same on its own path).
    """
    entry = result.get("key")
    if not isinstance(entry, dict):
        return None
    return "key:      %s %s (%s this token)" % (
        entry.get("label") or "", entry.get("prefix") or "", verb)


def cmd_token_issue(args) -> int:
    con = connect()
    try:
        ensure_schema(con)
        result = issue(con, args.tenant, args.instance, args.ttl_hours)
    finally:
        con.close()
    print("tenant:   %s" % result["slug"])
    print("instance: %s" % result["instance_id"])
    print("expires:  %s" % result["expires_at"])
    key_line = _key_line(result, "re-bound to")
    if key_line:
        print(key_line)
    print("token:    %s" % result["token"])
    print("(printed once; only its SHA-256 hash is stored)", file=sys.stderr)
    return 0


def cmd_token_rotate(args) -> int:
    con = connect()
    try:
        ensure_schema(con)
        result = rotate(con, args.tenant, args.instance, args.ttl_hours, dry_run=args.dry_run)
    finally:
        con.close()
    if result["dry_run"]:
        print("tenant:       %s" % result["slug"])
        print("would revoke: %d live token(s)" % result["revoked"])
        print("would issue:  1 token for %s" % result["instance_id"])
        key_line = _key_line(result, "would be re-bound to")
        if key_line:
            print(key_line)
        print("dry run only — nothing changed")
        return 0
    print("tenant:   %s" % result["slug"])
    print("instance: %s" % result["instance_id"])
    print("revoked:  %d live token(s)" % result["revoked"])
    print("expires:  %s" % result["expires_at"])
    key_line = _key_line(result, "re-bound to")
    if key_line:
        print(key_line)
    print("token:    %s" % result["token"])
    print("(printed once; only its SHA-256 hash is stored)", file=sys.stderr)
    return 0


def cmd_token_list(args) -> int:
    con = connect()
    try:
        ensure_schema(con)
        rows = list_tokens(con, args.tenant)
    finally:
        con.close()
    if not rows:
        print("no tokens for %s" % args.tenant)
        return 0
    print("%-9s %-20s %-20s %-8s %s" % ("hash8", "created", "expires", "state", "instance"))
    for row in rows:
        print("%-9s %-20s %-20s %-8s %s" % (
            row["hash8"], row["created_at"], row["expires_at"], row["state"], row["instance_id"]))
    return 0


def cmd_verify(args) -> int:
    if args.body:
        with open(args.body, "rb") as f:
            data = f.read()
    else:
        data = sys.stdin.buffer.read()
    try:
        signature = base64.b64decode(args.sig, validate=True)
    except ValueError:
        return _fail("--sig is not valid base64", 2)
    try:
        ok, detail = verify_seed_bytes(data, signature, args.pubkey)
    except ConfigError as exc:
        print("signature: BAD (%s)" % exc, file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        return _fail("cannot verify: %s" % exc, 2)
    if not ok:
        print("signature: BAD", file=sys.stderr)
        return 1
    print("signature: OK%s" % (" (%s)" % detail if detail else ""))
    return 0


def _root_key_problem(key_path: str) -> str | None:
    """The /root refusal: the release key signs offline and does not live here."""
    resolved = os.path.realpath(key_path)
    root = os.path.realpath("/root")
    if resolved == root or resolved.startswith(root + os.sep):
        return ("the release key must be offline: %s is under %s, and the key that "
                "signs releases never lives on the HQ box.\n"
                "       Run this on the offline machine, or pass KEY=<a copy outside "
                "/root> (docs/runbooks/seed.md, the offline ceremony)" % (resolved, root))
    return None


def _read_regular(path: str, what: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        print("seed: cannot read the %s %s: %s" % (what, path, exc.strerror), file=sys.stderr)
        return None


def cmd_release_sign(args) -> int:
    """Sign a release file with the offline release key; same bytes as a re-run."""
    problem = _root_key_problem(args.key)
    if problem:
        return _fail(problem)
    data = _read_regular(args.file, "release file")
    if data is None:
        return 2
    try:
        signature = sign_bytes(data, args.key)
    except (RuntimeError, ConfigError) as exc:
        return _fail(str(exc))
    pubkey = args.pubkey or DEFAULT_RELEASE_PUBKEY
    if not verify_bytes(data, signature, pubkey):
        return _fail(
            "the signature does not verify against %s; if you signed with another key, "
            "pass PUBKEY=<its public half>. Nothing was written." % pubkey)
    digest = hashlib.sha256(data).hexdigest()
    encoded = base64.b64encode(signature).decode("ascii")
    print("%s  %s" % (digest, args.file))
    print(encoded)
    out = args.file + ".sig"
    body = "\n".join(encoded[index:index + 76] for index in range(0, len(encoded), 76)) + "\n"
    write_private(out, body.encode("ascii"), mode=0o644)
    print("write        %s (base64, %d bytes)" % (out, len(signature)))
    with open(pubkey, "rb") as handle:
        key_digest = hashlib.sha256(handle.read()).hexdigest()
    print("verify       OK against %s (sha256 %s)" % (pubkey, key_digest))
    return 0


def cmd_seed_cert(args) -> int:
    """Certify the seed public key with the offline release key; reproducible bytes."""
    problem = _root_key_problem(args.key)
    if problem:
        return _fail(problem)
    public = _read_regular(args.file, "seed public key")
    if public is None:
        return 2
    if b"PRIVATE KEY" in public:
        return _fail("%s is a private key; pass the seed public key (its `.pub` half)" % args.file)
    if b"BEGIN PUBLIC KEY" not in public:
        return _fail("%s is not a PEM public key" % args.file)
    try:
        certificate = sign_bytes(public, args.key)
    except (RuntimeError, ConfigError) as exc:
        return _fail(str(exc))
    out = args.out or DEFAULT_SEED_CERT
    temp_pub = None
    if args.pubkey:
        pubkey = args.pubkey
    else:
        handle = tempfile.NamedTemporaryFile(prefix="odile-release-", suffix=".pub", delete=False)
        try:
            handle.write(public_key_bytes(args.key))
            handle.flush()
        finally:
            handle.close()
        pubkey = temp_pub = handle.name
    try:
        if not verify_bytes(public, certificate, pubkey):
            return _fail("the certificate does not verify against %s; nothing was written"
                         % pubkey)
        digest = hashlib.sha256(certificate).hexdigest()
        write_private(out, certificate, mode=0o644)
        pub_out = args.pub_out or os.path.join(os.path.dirname(out), "seed-key.pub")
        write_private(pub_out, public, mode=0o644)
    finally:
        if temp_pub is not None:
            os.unlink(temp_pub)
    print("seed key     %s (sha256 %s)" % (args.file, hashlib.sha256(public).hexdigest()))
    print("certificate  %s (64 bytes, sha256 %s)" % (out, digest))
    print("public key   %s (the same bytes, 0644)" % pub_out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="odile seed service and token ledger")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the HTTP service on 127.0.0.1").set_defaults(fn=lambda _a: serve())

    sub.add_parser("init", help="create the schema; idempotent").set_defaults(fn=cmd_init)

    keygen = sub.add_parser("keygen", help="generate the Ed25519 signing key pair")
    keygen.add_argument("--out", help="private key path (default $ODILE_SEED_SIGNING_KEY)")
    keygen.add_argument("--pub", help="public key path (default <out>.pub)")
    keygen.add_argument("--force", action="store_true", help="replace an existing key")
    keygen.set_defaults(fn=cmd_keygen)

    token = sub.add_parser("token", help="issue, rotate or inspect setup tokens")
    token_sub = token.add_subparsers(dest="token_cmd", required=True)

    issue_p = token_sub.add_parser("issue", help="mint one token; print it once")
    issue_p.add_argument("--tenant", required=True, help="tenant slug")
    issue_p.add_argument("--instance", help="Servury instance id (default: tenant.server_id)")
    issue_p.add_argument("--ttl-hours", type=int, default=24)
    issue_p.set_defaults(fn=cmd_token_issue)

    rotate_p = token_sub.add_parser("rotate", help="end every live token and issue one new one")
    rotate_p.add_argument("--tenant", required=True, help="tenant slug")
    rotate_p.add_argument("--instance", help="Servury instance id (default: tenant.server_id)")
    rotate_p.add_argument("--ttl-hours", type=int, default=24)
    rotate_p.add_argument("--dry-run", action="store_true", help="print what would change")
    rotate_p.set_defaults(fn=cmd_token_rotate)

    list_p = token_sub.add_parser("list", help="hashes, expiry and state; never the token")
    list_p.add_argument("--tenant", required=True, help="tenant slug")
    list_p.set_defaults(fn=cmd_token_list)

    verify_p = sub.add_parser("verify", help="check a seed signature (installer helper)")
    verify_p.add_argument("--pubkey", required=True, help="Odile signing public key (PEM)")
    verify_p.add_argument("--sig", required=True, help="base64 signature from X-Odile-Signature")
    verify_p.add_argument("--body", help="signed file (default: stdin)")
    verify_p.set_defaults(fn=cmd_verify)

    sign_p = sub.add_parser("release-sign", help="sign one release file with the offline key")
    sign_p.add_argument("--file", required=True, help="the file to sign (manifest.json, …)")
    sign_p.add_argument("--key", required=True, help="the offline release key (not under /root)")
    sign_p.add_argument("--pubkey", help="the release public key to verify against "
                                         f"(default {DEFAULT_RELEASE_PUBKEY})")
    sign_p.set_defaults(fn=cmd_release_sign)

    cert_p = sub.add_parser("seed-cert", help="certify the seed public key with the offline key")
    cert_p.add_argument("--file", required=True, help="the seed's public key (PEM)")
    cert_p.add_argument("--key", required=True, help="the offline release key (not under /root)")
    cert_p.add_argument("--pubkey", help="the release public key to verify against "
                                         "(default: the public half of --key)")
    cert_p.add_argument("--out", help=f"where the certificate goes (default {DEFAULT_SEED_CERT})")
    cert_p.add_argument("--pub-out", help="where the public key copy goes "
                                          "(default: seed-key.pub beside --out)")
    cert_p.set_defaults(fn=cmd_seed_cert)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except SeedError as exc:
        return _fail(exc.message, 2)
    except ConfigError as exc:
        return _fail(str(exc), 2)


if __name__ == "__main__":
    raise SystemExit(main())
