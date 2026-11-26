#!/usr/bin/python3
"""The heartbeat receiver and the guest's signed beat (ODL-216).

Two ends of one wire:

* `serve` is HQ's receiver.  It accepts `POST /heartbeat`, verifies the
  Ed25519 signature with the key registered on the tenant's first beat, and
  stores the facts (versions, disk, uptime, last backup, last brief) in
  SQLite.  `GET /health` is the receiver's own liveness surface.
* `collect`, `sign` and `post` are the guest job.  `collect` reads the facts
  from the machine, `sign` puts a 64-byte Ed25519 signature over the exact
  body bytes, and `post` sends both to HQ.  `keygen` creates the pair at
  install time; the private half never leaves the guest.

The signature is over the exact request body, not a re-serialised copy of it,
so a single whitespace change is a failed signature.  The public half is inside
the signed first heartbeat, and the receiver registers it then — trust on first
use, as the card specifies.  A later body that offers a different key is a
conflict, never a quiet re-registration.

    scripts/heartbeat.py keygen --out /etc/odile/heartbeat
    scripts/heartbeat.py collect --slug odl-7f3a2c --public-key /etc/odile/heartbeat/heartbeat.pub
    scripts/heartbeat.py post --url https://odile.fyi/heartbeat --key /etc/odile/heartbeat/heartbeat.key
    scripts/heartbeat.py recovery --slug odl-7f3a2c --state /etc/odile/first-visit.json

`recovery` is the guest half of the one-recovery-code model (ODL-926): it
reads the hash the first-visit service minted and pushes it to
``/tenant/recovery``, signed with the same Ed25519 key the heartbeat uses. A
409 means no key is registered yet, so it offers the public half in a signed
heartbeat and retries once; a 200 records the acknowledged hash in the state
and the five-minute timer then does nothing until the code is replaced.

`openssl` is used for signing and verification because the standard library has
no Ed25519; it is the same binary `seed/seed.py` uses.  No secret is written to
stdout, and a private key that is group- or world-readable is refused.

The receiver is an HTTP server on purpose: Caddy terminates TLS and proxies
`/heartbeat` to loopback (the runbook shows the four lines).  There is no
credential on the endpoint; the signature is the authentication.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _root in (ROOT, ROOT.parent):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

# The receiver's store lives on HQ (`odilectl`); a tenant installs only this
# file, so that package is imported lazily by the receiver verbs and the two
# constants it offers are spelled here unchanged. The guest verbs (collect,
# sign, post, recovery) therefore run on the machine with no HQ package.
DEFAULT_HEARTBEAT_DB = "/var/lib/odile/waitlist.db"
RELEASE_ID_RE = re.compile(r"^[0-9]{4}\.[0-9]{2}(?:\.[0-9]+)?$")
from attest import read_boot_report, transform_boot_report  # noqa: E402

MAX_BODY_BYTES = 64 * 1024
DEFAULT_KEY_DIR = "/etc/odile/heartbeat"
DEFAULT_STATE = "/etc/odile/first-visit.json"
DEFAULT_RECOVERY_FILE = "/etc/odile/recovery.txt"
DEFAULT_RECOVERY_URL = "https://odile.fyi/tenant/recovery"
DEFAULT_HEARTBEAT_URL = "https://odile.fyi/heartbeat"
USER_AGENT = "odile-heartbeat/0.1"


# ---------------------------------------------------------------------------
# Ed25519 over the system OpenSSL (the standard library has no Ed25519)
# ---------------------------------------------------------------------------


def _openssl() -> str:
    found = shutil.which("openssl")
    if not found:
        raise SystemExit("heartbeat: openssl is not installed")
    return found


def _safe_mode(path: Path, what: str) -> None:
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise SystemExit(f"heartbeat: {what} {path} is mode {mode:04o}; it must be 0600")


def verify_ed25519(body: bytes, signature: bytes, public_key_pem: str) -> bool:
    """True when `signature` is Ed25519 over the exact `body` bytes."""
    if not public_key_pem.strip().startswith("-----BEGIN PUBLIC KEY-----"):
        return False
    with tempfile.TemporaryDirectory(prefix="odile-heartbeat-") as directory:
        public = Path(directory) / "public.pem"
        signature_path = Path(directory) / "signature.bin"
        public.write_text(public_key_pem, encoding="utf-8")
        public.chmod(0o600)
        signature_path.write_bytes(signature)
        try:
            done = subprocess.run(
                [
                    _openssl(),
                    "dgst",
                    "-verify",
                    str(public),
                    "-signature",
                    str(signature_path),
                ],
                input=body,
                capture_output=True,
            )
        except OSError as error:
            raise SystemExit(f"heartbeat: cannot run openssl: {error}") from None
    return done.returncode == 0


def sign_ed25519(body: bytes, key_path: str | Path) -> bytes:
    """The 64-byte signature over the exact `body` bytes."""
    path = Path(key_path).expanduser()
    _safe_mode(path, "signing key")
    try:
        done = subprocess.run(
            [_openssl(), "dgst", "-sign", str(path), "-out", "/dev/stdout"],
            input=body,
            capture_output=True,
        )
    except OSError as error:
        raise SystemExit(f"heartbeat: cannot run openssl: {error}") from None
    if done.returncode != 0:
        raise SystemExit(
            "heartbeat: openssl sign failed: "
            + done.stderr.decode("utf-8", "replace").strip()
        )
    if len(done.stdout) != 64:
        raise SystemExit(f"heartbeat: openssl signed {len(done.stdout)} bytes, expected 64")
    return done.stdout


def keygen(directory: str | Path) -> tuple[Path, Path]:
    """Create the install-time Ed25519 pair; refuse to replace an existing key."""
    target = Path(directory).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)
    private = target / "heartbeat.key"
    public = target / "heartbeat.pub"
    if private.exists():
        if not public.exists():
            derived = subprocess.run(
                [_openssl(), "pkey", "-in", str(private), "-pubout", "-out", str(public)],
                capture_output=True,
            )
            if derived.returncode != 0:
                raise SystemExit(
                    "heartbeat: openssl pkey failed: "
                    + derived.stderr.decode("utf-8", "replace").strip()
                )
            public.chmod(0o644)
        return private, public
    try:
        generated = subprocess.run(
            [_openssl(), "genpkey", "-algorithm", "ED25519", "-out", str(private)],
            capture_output=True,
        )
        if generated.returncode != 0:
            raise SystemExit(
                "heartbeat: openssl genpkey failed: "
                + generated.stderr.decode("utf-8", "replace").strip()
            )
        private.chmod(0o600)
        derived = subprocess.run(
            [_openssl(), "pkey", "-in", str(private), "-pubout", "-out", str(public)],
            capture_output=True,
        )
        if derived.returncode != 0:
            raise SystemExit(
                "heartbeat: openssl pkey failed: "
                + derived.stderr.decode("utf-8", "replace").strip()
            )
        public.chmod(0o644)
    except OSError as error:
        raise SystemExit(f"heartbeat: cannot create a signing key: {error}") from None
    return private, public


# ---------------------------------------------------------------------------
# The guest's facts
# ---------------------------------------------------------------------------


def _read_json(path: str | Path | None) -> dict:
    if not path:
        return {}
    try:
        value = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"heartbeat: cannot read {path}: {error}") from None
    return value if isinstance(value, dict) else {}


def _status_epoch(path: str | Path | None, *names: str) -> int | None:
    data = _read_json(path)
    for name in names:
        value = data.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


# The backup record HQ is allowed to hold: the repository URL, its size, the
# last status and the latest snapshot id. `scripts/backup.py` writes exactly
# these to /var/lib/odile/backup.json; the password that opens the repository
# is not among them (the user's recovery file is its only copy), and this tuple
# is what keeps a later field from leaking by accident.
BACKUP_FACT_KEYS = (
    "repo",
    "snapshot",
    "status",
    "bytes_processed",
    "repo_bytes",
    "last_success_at",
    "at_iso",
)


def _backup_fact(path: str | Path | None) -> dict:
    """The allowed backup facts (BACKUP_FACT_KEYS), or nothing when there is no record.

    A missing file is *no fact*, not `0 bytes`: HQ then shows "no backup" and
    the 36-hour freshness alert fires on age alone, which is the honest reading
    of a machine that has never completed a run.
    """
    data = _read_json(path)
    return {key: data[key] for key in BACKUP_FACT_KEYS if key in data}


DEFAULT_BOOT_REPORT = "/run/blackbox/attest.json"


def _boot_fact(path: str | Path | None) -> dict[str, str] | None:
    """The release and launch measurement from the boot report, when usable.

    Servury's `bb-seed` copies `/run/blackbox/attest.json` (0644, no root)
    before switch-root. The file is JSON: `release` is the measured command
    line's release id and `report_b64` is the signed SNP report, whose
    0x90:0xC0 bytes are the 48-byte launch digest. Both fields are required:
    a half-read report is `unknown` at HQ, not a half-truth in the facts.
    """
    if not path:
        return None
    document, error = read_boot_report(str(path))
    if document is None or error is not None:
        return None
    summary = transform_boot_report(document, None)
    release = summary.get("release")
    measurement = summary.get("measurement")
    if not summary.get("available") or not isinstance(release, str) or not release.strip():
        return None
    if not RELEASE_ID_RE.match(release.strip()):
        return None
    if not isinstance(measurement, str) or len(measurement) != 96:
        return None
    return {"release": release.strip(), "measurement": measurement}


def collect(args: argparse.Namespace) -> dict:
    """Read the facts the card names, without root and without a secret."""
    versions: dict[str, str] = {}
    for key, value in _read_json(args.version_file).items():
        if isinstance(key, str) and isinstance(value, str):
            versions[key] = value
    if args.odile_version:
        versions["odile"] = args.odile_version
    if args.hermes_version:
        versions["hermes"] = args.hermes_version
    try:
        release = Path("/etc/os-release").read_text(encoding="utf-8")
        for line in release.splitlines():
            if line.startswith("VERSION_ID="):
                versions.setdefault("debian", line.split("=", 1)[1].strip().strip('"'))
    except OSError:
        pass

    usage = shutil.disk_usage(args.disk)
    facts: dict = {
        "versions": versions,
        "disk": {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "percent": round(usage.used / usage.total * 100, 1) if usage.total else 0.0,
        },
    }
    try:
        facts["uptime_seconds"] = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        pass

    backup = _status_epoch(args.backup_status, "last_success_at", "last_backup_at", "at")
    if backup:
        facts["last_backup_at"] = backup
    backup_record = _backup_fact(args.backup_status)
    if backup_record:
        facts["backup"] = backup_record
    brief = _status_epoch(args.brief_status, "last_sent_at", "last_brief_at", "at")
    if brief:
        facts["last_brief_at"] = brief
    if args.cause:
        facts["cause"] = args.cause

    boot = _boot_fact(getattr(args, "attest", DEFAULT_BOOT_REPORT))
    if boot is not None:
        facts["boot"] = boot

    payload = {
        "slug": args.slug,
        "at": int(args.at or time.time()),
        "facts": facts,
    }
    if args.public_key:
        try:
            payload["public_key"] = Path(args.public_key).expanduser().read_text(encoding="utf-8")
        except OSError as error:
            raise SystemExit(f"heartbeat: cannot read the public key: {error}") from None
    return payload


# ---------------------------------------------------------------------------
# The receiver
# ---------------------------------------------------------------------------


def _health():
    """HQ's heartbeat store, imported only where it exists (the receiver)."""
    from odilectl import health

    return health


class HeartbeatServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], store) -> None:
        super().__init__(address, HeartbeatHandler)
        self.store = store


class HeartbeatHandler(BaseHTTPRequestHandler):
    server_version = "odile-heartbeat/0.1"

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib spelling
        if self.path.split("?", 1)[0] != "/health":
            self._json(404, {"ok": False, "error": "not found"})
            return
        store = self.server.store  # type: ignore[attr-defined]
        self._json(
            200,
            {"ok": True, "service": "odile-heartbeat", "heartbeats": store.heartbeat_count()},
        )

    def do_POST(self) -> None:  # noqa: N802 - stdlib spelling
        if self.path.split("?", 1)[0] != "/heartbeat":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(411, {"ok": False, "error": "Content-Length is required"})
            return
        if length <= 0:
            self._json(400, {"ok": False, "error": "empty heartbeat"})
            return
        if length > MAX_BODY_BYTES:
            self._json(413, {"ok": False, "error": "heartbeat is larger than 64 KiB"})
            return
        body = self.rfile.read(length)
        signature = self.headers.get("X-Odile-Signature", "")
        store = self.server.store  # type: ignore[attr-defined]
        health = _health()
        try:
            beat = store.record(body, signature)
        except health.HeartbeatRejected as error:
            self._json(error.status, {"ok": False, "error": str(error)})
            return
        except health.HeartbeatError as error:
            self._json(500, {"ok": False, "error": str(error)})
            return
        answer = {
            "ok": True,
            "slug": beat.slug,
            "at": beat.at,
            "received_at": beat.received_at,
        }
        # The booking is one added key (ODL-950, ADR-005 §5.3): a plain beat
        # keeps the four, and a refused one carries no `upgrade` at all.
        if beat.upgrade is not None:
            answer["upgrade"] = beat.upgrade
        self._json(200, answer)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        sys.stderr.write(
            "%s heartbeat %s\n"
            % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), format % args)
        )


def serve(args: argparse.Namespace) -> int:
    db_path = args.db or _health().DEFAULT_HEARTBEAT_DB
    store = _health().HeartbeatStore.open(db_path, verifier=verify_ed25519)
    try:
        server = HeartbeatServer((args.host, args.port), store)
        host, port = server.server_address[:2]
        sys.stderr.write(
            "heartbeat: receiver on http://%s:%s/heartbeat; db %s\n" % (host, port, db_path)
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        store.close()
    return 0


# ---------------------------------------------------------------------------
# The verbs
# ---------------------------------------------------------------------------


def _body_bytes(path: str | None) -> bytes:
    if path:
        try:
            body = Path(path).expanduser().read_bytes()
        except OSError as error:
            raise SystemExit(f"heartbeat: cannot read {path}: {error}") from None
    else:
        body = sys.stdin.buffer.read()
    if len(body) > MAX_BODY_BYTES:
        raise SystemExit(f"heartbeat: body is {len(body)} bytes; the limit is 64 KiB")
    return body


def _signature_from(args: argparse.Namespace, body: bytes) -> bytes:
    if args.key:
        return base64.b64encode(sign_ed25519(body, args.key))
    if args.signature:
        try:
            return Path(args.signature).expanduser().read_bytes()
        except OSError as error:
            raise SystemExit(f"heartbeat: cannot read the signature: {error}") from None
    raise SystemExit("heartbeat: pass --key PRIVATE.pem or --signature FILE")


def record(args: argparse.Namespace) -> int:
    body = _body_bytes(args.body)
    signature = _signature_from(args, body)
    health = _health()
    store = health.HeartbeatStore.open(args.db or health.DEFAULT_HEARTBEAT_DB,
                                       verifier=verify_ed25519)
    try:
        beat = store.record(body, signature, now=args.now)
    except health.HeartbeatRejected as error:
        print(f"heartbeat: rejected: {error}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(json.dumps(beat.to_dict(), indent=2, sort_keys=True))
    return 0


def show(args: argparse.Namespace) -> int:
    health = _health()
    store = health.HeartbeatStore.open(args.db or health.DEFAULT_HEARTBEAT_DB)
    try:
        if args.slug:
            beat = store.latest(args.slug)
            if beat is None:
                print(f"heartbeat: no beat for {args.slug}", file=sys.stderr)
                return 1
            print(json.dumps(beat.to_dict(), indent=2, sort_keys=True))
            return 0
        latest = store.latest_all()
        print(
            json.dumps(
                [
                    {
                        "slug": slug,
                        "at": beat.at,
                        "received_at": beat.received_at,
                        "facts": beat.facts,
                    }
                    for slug, beat in sorted(latest.items())
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    finally:
        store.close()


def sign(args: argparse.Namespace) -> int:
    body = _body_bytes(args.body)
    print(base64.b64encode(sign_ed25519(body, args.key)).decode("ascii"))
    return 0


def post(args: argparse.Namespace) -> int:
    body = _body_bytes(args.body)
    signature = _signature_from(args, body)
    request = urllib.request.Request(
        args.url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Odile-Signature": signature.decode("ascii")
            if isinstance(signature, bytes)
            else signature,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            answer = response.read(4096).decode("utf-8", "replace")
            print(answer)
            return 0 if response.status == 200 else 1
    except urllib.error.HTTPError as error:
        print(error.read(4096).decode("utf-8", "replace"), file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as error:
        print(f"heartbeat: post failed: {error}", file=sys.stderr)
        return 1


RECOVERY_INSTANCE_RE = re.compile(r"^Instance:[ \t]*(\S+)[ \t]*$", re.M)


def _load_recovery_state(path: str | Path) -> dict:
    try:
        state = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"heartbeat: cannot read the first-visit state {path}: {error}") from None
    if not isinstance(state, dict):
        raise SystemExit(f"heartbeat: {path} is not a JSON object")
    return state


@contextmanager
def _state_lock(path: str | Path):
    """The first-visit service's own lock, taken by the client for one read or write.

    The client is a second writer of the same document (ODL-926): the service
    rewrites it on a replace while this process may be mid-POST. The lock is
    the same ``<state>.lock`` the service holds across its read-verify-write,
    so neither side can land between the other's read and its write.
    """
    target = Path(path).expanduser()
    lock_path = target.with_name(target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _acknowledge_recovery(path: str | Path, recovery_hash: str) -> bool:
    """Record the acknowledged hash, but only if that hash is still live.

    Returns False when a replace landed while the POST was open. The service
    wrote a new ``recovery_hash`` in that window; this client's copy is stale,
    and rewriting it from the copy would resurrect the retired code and mark the
    new one as pushed, so the new code would never reach HQ. Re-read under the
    lock and leave the state — and the push pending — alone in that case.
    """
    with _state_lock(path):
        live = _load_recovery_state(path)
        if live.get("recovery_hash") != recovery_hash:
            return False
        live["recovery_pushed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        live["recovery_pushed_hash"] = recovery_hash
        _save_recovery_state(path, live)
    return True


def _save_recovery_state(path: str | Path, state: dict) -> None:
    """Write the state atomically, 0600, beside itself (the service's own rule)."""
    target = Path(path).expanduser()
    body = json.dumps(state, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".%s." % target.name, dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _instance_id(args: argparse.Namespace) -> str:
    """The machine's recorded id, from --instance-id or the recovery file."""
    if getattr(args, "instance_id", None):
        return args.instance_id.strip()
    path = getattr(args, "recovery_file", None)
    if not path:
        raise SystemExit("heartbeat: pass --instance-id or --recovery-file")
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"heartbeat: cannot read {path}: {error}") from None
    match = RECOVERY_INSTANCE_RE.search(text) if RECOVERY_INSTANCE_RE else None
    if match is None:
        raise SystemExit(f"heartbeat: {path} carries no Instance: line")
    return match.group(1).strip()


def _post_signed(url: str, body: bytes, key: str | Path, timeout: float) -> tuple[int, str]:
    """Sign the exact bytes and POST them; returns (status, body text)."""
    signature = base64.b64encode(sign_ed25519(body, key)).decode("ascii")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Odile-Signature": signature,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read(4096).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as error:
        raise OSError(str(getattr(error, "reason", error))) from None


def _register_heartbeat(args: argparse.Namespace, slug: str, public: Path) -> tuple[int, str]:
    """The machine's first heartbeat: offer the public half, trust on first use.

    This is the registration the recovery endpoint looks for. It is a normal
    signed heartbeat, so the record is the same row the backup job writes; the
    timer calls it only when the push met 409 (no key registered for the slug).
    """
    body = json.dumps({
        "slug": slug,
        "at": int(time.time()),
        "facts": {"versions": {}},
        "public_key": public.read_text(encoding="utf-8"),
    }, sort_keys=True).encode("utf-8")
    return _post_signed(args.heartbeat_url, body, args.key, args.timeout)


def recovery(args: argparse.Namespace) -> int:
    """Push the live recovery hash to HQ, signed with the heartbeat key.

    The body carries the machine's slug and instance id and the hash — no code,
    no password (docs/product/DECISION-one-recovery-code.md §2.3). A 200 is the
    account's acknowledgement and is recorded in the state, so the timer's next
    run is a no-op until the code is replaced. Anything else leaves the push
    pending: the unit runs every five minutes until HQ answers.

    The state is a second writer's document: the first-visit service rewrites
    it on a replace. The client takes the service's own lock for the read, and
    after a 200 it re-reads under that lock and records the markers only while
    the pushed hash is still live; a replace that landed during the POST stays
    pending and is pushed by the next tick.
    """
    with _state_lock(args.state):
        state = _load_recovery_state(args.state)
    recovery_hash = state.get("recovery_hash")
    if not isinstance(recovery_hash, str) or not recovery_hash.startswith("scrypt$"):
        print("recovery: no recovery code minted yet; nothing to push")
        return 0
    if state.get("recovery_pushed_hash") == recovery_hash:
        print("recovery: HQ already acknowledged this hash; nothing to push")
        return 0
    instance_id = _instance_id(args)
    key = Path(args.key).expanduser()
    public = Path(args.public_key).expanduser() if args.public_key else key.with_suffix(".pub")
    if not key.is_file():
        if args.dry_run:
            print(f"would create {key} (ed25519, 0600) and push")
            return 0
        keygen(key.parent)
    if not public.is_file():
        public = keygen(key.parent)[1]
    body = json.dumps({
        "slug": args.slug,
        "instance_id": instance_id,
        "recovery_hash": recovery_hash,
    }, sort_keys=True).encode("utf-8")
    if args.dry_run:
        print(f"would post {len(body)} bytes to {args.url}: "
              f"keys {sorted(json.loads(body))}")
        return 0
    try:
        status, answer = _post_signed(args.url, body, key, args.timeout)
        if status == 409 and not args.no_register:
            # No heartbeat key is registered for this slug yet: register it with
            # the machine's first signed heartbeat, then try the push once more.
            reg_status, reg_answer = _register_heartbeat(args, args.slug, public)
            if reg_status != 200:
                print(f"recovery: heartbeat registration answered {reg_status}: "
                      f"{reg_answer.strip()[:200]}", file=sys.stderr)
                return 1
            status, answer = _post_signed(args.url, body, key, args.timeout)
    except OSError as error:
        print(f"recovery: cannot reach {args.url}: {error}", file=sys.stderr)
        return 1
    if status != 200:
        print(f"recovery: HQ answered {status}: {answer.strip()[:200]}", file=sys.stderr)
        return 1
    try:
        parsed = json.loads(answer or "{}")
    except ValueError:
        parsed = {}
    changed = bool(parsed.get("changed")) if isinstance(parsed, dict) else False
    if not _acknowledge_recovery(args.state, recovery_hash):
        # A replace landed while the POST was open: the code the user was shown
        # is live, and this push acknowledged the retired one. Leave the new
        # hash pending so the timer pushes it; never rewrite the state from the
        # stale copy (that would resurrect the old code).
        print(f"recovery: a replace landed while the push was open; "
              f"{args.slug}'s new hash stays pending for the next tick")
        return 0
    print(f"recovery: pushed for {args.slug}; HQ says changed={str(changed).lower()}")
    return 0


def keygen_verb(args: argparse.Namespace) -> int:
    private, public = keygen(args.out)
    print(
        json.dumps(
            {
                "private": str(private),
                "public": str(public),
                "note": "the private half never leaves this machine; the public half rides the first heartbeat",
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def collect_verb(args: argparse.Namespace) -> int:
    print(json.dumps(collect(args), indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="heartbeat",
        description="Odile's signed heartbeat: the receiver on HQ and the guest's job.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    receiver = subparsers.add_parser("serve", help="run the HQ receiver")
    receiver.add_argument("--db", default=DEFAULT_HEARTBEAT_DB)
    receiver.add_argument("--host", default="127.0.0.1")
    receiver.add_argument("--port", type=int, default=8099)
    receiver.set_defaults(function=serve)

    accept = subparsers.add_parser("record", help="verify and store one heartbeat body")
    accept.add_argument("--db", default=DEFAULT_HEARTBEAT_DB)
    accept.add_argument("--body", help="JSON body file (default: stdin)")
    accept.add_argument("--signature", help="signature file (raw 64 bytes or base64)")
    accept.add_argument("--key", help="sign the body with this private key first")
    accept.add_argument("--now", type=int)
    accept.set_defaults(function=record)

    listing = subparsers.add_parser("show", help="print the latest accepted beat")
    listing.add_argument("--db", default=DEFAULT_HEARTBEAT_DB)
    listing.add_argument("--slug")
    listing.set_defaults(function=show)

    signing = subparsers.add_parser("sign", help="sign a body and print the base64 signature")
    signing.add_argument("--key", required=True)
    signing.add_argument("--body", help="JSON body file (default: stdin)")
    signing.set_defaults(function=sign)

    sender = subparsers.add_parser("post", help="sign and POST a heartbeat to HQ")
    sender.add_argument("--url", required=True)
    sender.add_argument("--body", help="JSON body file (default: stdin)")
    sender.add_argument("--key")
    sender.add_argument("--signature")
    sender.add_argument("--timeout", type=float, default=10.0)
    sender.set_defaults(function=post)

    generator = subparsers.add_parser("keygen", help="create the install-time Ed25519 pair")
    generator.add_argument("--out", default=DEFAULT_KEY_DIR)
    generator.set_defaults(function=keygen_verb)

    facts = subparsers.add_parser("collect", help="build the guest's heartbeat body")
    facts.add_argument("--slug", required=True)
    facts.add_argument("--at", type=int)
    facts.add_argument("--disk", default="/")
    facts.add_argument("--backup-status")
    facts.add_argument("--brief-status")
    facts.add_argument("--version-file")
    facts.add_argument("--odile-version")
    facts.add_argument("--hermes-version")
    facts.add_argument("--cause")
    facts.add_argument("--public-key")
    facts.add_argument(
        "--attest",
        default=DEFAULT_BOOT_REPORT,
        metavar="PATH",
        help=f"the boot report (default: {DEFAULT_BOOT_REPORT})",
    )
    facts.set_defaults(function=collect_verb)

    push = subparsers.add_parser(
        "recovery", help="push the recovery-code hash to HQ, signed")
    push.add_argument("--state", default=DEFAULT_STATE)
    push.add_argument("--slug", required=True)
    push.add_argument("--instance-id")
    push.add_argument("--recovery-file", default=DEFAULT_RECOVERY_FILE,
                      help="the recovery file whose Instance: line names the machine")
    push.add_argument("--url", default=DEFAULT_RECOVERY_URL)
    push.add_argument("--heartbeat-url", default=DEFAULT_HEARTBEAT_URL,
                      help="where the first signed heartbeat registers the key")
    push.add_argument("--key", default=str(Path(DEFAULT_KEY_DIR) / "heartbeat.key"))
    push.add_argument("--public-key")
    push.add_argument("--timeout", type=float, default=15.0)
    push.add_argument("--no-register", action="store_true",
                      help="do not register the key on a 409 (a proof run)")
    push.add_argument("--dry-run", action="store_true")
    push.set_defaults(function=recovery)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.function(args))


if __name__ == "__main__":
    raise SystemExit(main())
