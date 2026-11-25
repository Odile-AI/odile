#!/usr/bin/python3
"""Provision exactly one real tenant on a Servury Blackbox, with the user's key.

    scripts/provision_one.py --slug odl-7f3a2c --email ada@example.com \
        --key /root/.odile/tenants/odl-7f3a2c.pub            # dry run
    scripts/provision_one.py ... --apply                      # spends money

The card is ODL-201. The rules this file exists to keep:

* the machine is ordered with **exactly one** SSH public key, the user's boot
  key; measured boot fixes that key in the cmdline, so an operator key cannot
  be added later and none is ever sent;
* the private half never reaches HQ: the script reads a public key file and
  refuses anything that looks private;
* the call spends money, so the default is a dry run; `--apply` confirms the
  account balance and plan stock first, and prints every request and response;
* provisioning is an **observation**, not an SSH session: the initramfs host key
  is learned with `ssh-keyscan` (a key exchange, no auth) and the script
  *requires* `ssh -o BatchMode=yes` to fail;
* the durable facts land in the web `tenants` table (server_id, ipv4, plan,
  expires_at, and the pin's release) and in `tenant_state` (plan_id, epoch
  expiry, the pin's release, and the initramfs host-key fingerprint that
  `park_for_owner` compares later);
* a second run reuses a machine with the same hostname instead of ordering a
  duplicate, so "run it again" is safe.

The release the machine is ordered to boot comes from the repo pin's
`provision_release` (ODL-395/ODL-398), read before anything is ordered or
charged; a pin without it is refused. The provider's `measured_release` is a
post-order observation and stays in the event detail — it never becomes the
tenant's release.

The release floor below (2026.11.1 or later) is about the provider's measured
release: 2026.09 and earlier made a new initramfs host key on every boot, which
breaks host-key-based waiting-for-owner detection. The check runs after the
machine is active; a lower release exits 5 and the machine is left in place for
inspection.

Run `--self-test` for the pure checks (key parsing, price, release, redaction);
it opens no socket and touches no database.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

# The pin helper is shared with `odilectl provision` and the claim worker, so
# the two provisioning paths cannot drift on what a pinned release is
# (ODL-398). The tree root has to be importable before the package is.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odilectl.errors import OdileError  # noqa: E402
from odilectl.provision import DEFAULT_PIN, pinned_release  # noqa: E402

BASE_URL = "https://servury.com/api/reseller.php"
SECRET_FILE = "/root/.odile/servury_reseller_key"
DEFAULT_DB = os.environ.get("ODILE_DB", "/var/lib/odile/waitlist.db")
# Savio's 2026-11-24 direction: a new machine is a Blackbox Nano at the annual
# term (docs/runbooks/deploy.md, docs/evidence/nano-baseline.md). The tier is
# not a preference — Servury cannot downgrade a machine, so a tenant placed on a
# larger plan cannot move to a Nano without a rebuild. 138 is the Canada Nano
# (1 vCPU / 2 GB / 25 GB); 139 is the Netherlands Nano and 141 the US one.
DEFAULT_PLAN = 138  # Blackbox Nano, 1 vCPU / 2 GB / 25 GB, Canada
DEFAULT_DAYS = 365
MIN_RELEASE = (2026, 11, 1)

SLUG_RE = re.compile(r"^odl-[0-9a-f]{6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# A plain authorized_keys line: a key type, one base64 blob, one optional
# comment. No options, no forced command, no private key, no second line.
KEY_LINE_RE = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256)"
    r" ([A-Za-z0-9+/]+={0,2})( \S.*)?$"
)


class Failure(Exception):
    """A step failed in a way the operator can act on; no traceback needed."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


class ProviderError(Failure):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message, 4)
        self.status = status


# --------------------------------------------------------------------------- key


def fingerprint(blob: bytes) -> str:
    """OpenSSH's SHA256 fingerprint: base64 of the digest, no '=' padding."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def read_boot_key(path: Path) -> tuple[str, str]:
    """Return (the one key line, its SHA256 fingerprint), or raise Failure.

    The file is the *public* half, collected at claim. A file with two lines is
    refused because measured boot takes exactly one key and silently choosing
    one of two is how an operator key gets in.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise Failure(f"no public key at {path}", 2) from None
    except OSError as error:
        raise Failure(f"cannot read {path}: {error.strerror}", 2) from None
    if "PRIVATE KEY" in text:
        raise Failure(f"{path} is a private key; only the public half belongs here", 2)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise Failure(f"{path} is empty", 2)
    if len(lines) > 1:
        raise Failure(
            f"{path} holds {len(lines)} keys; measured boot takes exactly one "
            "(a second key cannot enter the measured cmdline)", 2
        )
    key = lines[0]
    match = KEY_LINE_RE.match(key)
    if not match:
        raise Failure(
            f"{path} is not a plain authorized_keys line (options, a forced "
            "command, a password or more than one key are all refused)", 2
        )
    key_type, encoded = match.group(1), match.group(2)
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise Failure(f"{path}: the key body is not valid base64", 2) from None
    if len(blob) < 4 or struct.unpack(">I", blob[:4])[0] != len(key_type):
        raise Failure(f"{path}: the key body does not start with {key_type}", 2)
    if blob[4 : 4 + len(key_type)].decode("ascii", "replace") != key_type:
        raise Failure(f"{path}: the key type in the text and in the body disagree", 2)
    return key, fingerprint(blob)


def redact_key(key: str) -> str:
    """A public key is not a secret, but a transcript is easier to read short."""
    key_type, _, rest = key.partition(" ")
    blob, _, _comment = rest.partition(" ")
    if len(blob) <= 24:
        return f"{key_type} <short key>"
    return f"{key_type} {blob[:12]}…{blob[-8:]}"


# ------------------------------------------------------------------------- money


def price_for(retail_monthly: float, days: int, discount_percent: float) -> Decimal:
    """The vendor's arithmetic: retail/31*days, to the cent, less the discount."""
    base = (Decimal(str(retail_monthly)) / Decimal(31) * Decimal(days)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return (base * (Decimal(100) - Decimal(str(discount_percent))) / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def parse_release(value: str | None) -> tuple[int, ...]:
    """'2026.11.1' -> (2026, 11, 1); anything unparseable is a failure."""
    if not value:
        raise Failure("the provider reported no measured release", 5)
    parts = value.strip().split(".")
    if not parts or not all(part.isdigit() for part in parts):
        raise Failure(f"measured release {value!r} is not dotted digits", 5)
    return tuple(int(part) for part in parts)


def release_at_least(value: str | None, floor: tuple[int, ...]) -> bool:
    try:
        got = parse_release(value)
    except Failure:
        return False
    width = max(len(got), len(floor))
    return got + (0,) * (width - len(got)) >= floor + (0,) * (width - len(floor))


# ---------------------------------------------------------------------- provider


class Servury:
    """The three calls this card needs, with the key in a header and nowhere else."""

    def __init__(self, api_key: str, base_url: str = BASE_URL, timeout: float = 30.0):
        if not api_key.strip():
            raise Failure("the reseller key file is empty", 2)
        self._key = api_key.strip()
        self._base = base_url
        self._timeout = timeout

    def get(self, action: str, *, server_id: str | None = None, attempts: int = 3) -> dict:
        return self._call("GET", action, server_id=server_id, attempts=attempts)

    def post(self, action: str, params: dict, *, server_id: str | None = None) -> dict:
        # A POST is never retried here: `create` is a paid call, and a retry
        # after a timeout could order a second machine.
        return self._call("POST", action, params=params, server_id=server_id, attempts=1)

    def _call(
        self,
        method: str,
        action: str,
        *,
        params: dict | None = None,
        server_id: str | None = None,
        attempts: int = 1,
    ) -> dict:
        url = self._base + "?action=" + urllib.parse.quote(action)
        if server_id is not None:
            url += "&id=" + urllib.parse.quote(server_id)
        headers = {"X-Reseller-Key": self._key, "Accept": "application/json"}
        body = None
        if method == "POST":
            body = json.dumps(params or {}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        last: str | None = None
        for attempt in range(1, attempts + 1):
            try:
                request = urllib.request.Request(url, data=body, headers=headers, method=method)
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    status, raw = response.status, response.read()
            except urllib.error.HTTPError as error:
                status, raw = error.code, error.read()
            except OSError as error:
                last = f"cannot reach the provider: {error}"
                if attempt < attempts:
                    time.sleep(2 * attempt)
                    continue
                raise ProviderError(f"{action}: {last}") from None
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                excerpt = raw[:200].decode("utf-8", "replace")
                raise ProviderError(f"{action}: HTTP {status} with non-JSON body: {excerpt!r}", status)
            if not isinstance(payload, dict):
                raise ProviderError(f"{action}: HTTP {status} with a non-object body", status)
            if status >= 400 or not payload.get("success"):
                message = payload.get("error") or payload.get("message") or "no message"
                raise ProviderError(f"{action}: HTTP {status}: {message}", status)
            data = payload.get("data")
            if data is None:
                return {}
            if not isinstance(data, dict):
                raise ProviderError(f"{action}: 'data' is not an object", status)
            return data
        raise ProviderError(f"{action}: {last or 'no attempt made'}")


def find_plan(plans: list[dict], plan_id: int) -> dict:
    for plan in plans:
        if plan.get("id") == plan_id:
            return plan
    raise Failure(f"plan {plan_id} is not on this account", 3)


# ------------------------------------------------------------------- observation


def ssh_keyscan(host: str, timeout: float = 5.0) -> tuple[str, str, str]:
    """Return (type, base64 blob, SHA256 fingerprint) for the port-22 ed25519 key.

    `ssh-keyscan` is a key exchange, not a login: no key is offered, no session
    is opened. That is how HQ learns the initramfs key without ever holding the
    user's private half.
    """
    try:
        done = subprocess.run(
            ["ssh-keyscan", "-T", str(int(timeout)), "-t", "ed25519", host],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except FileNotFoundError:
        raise Failure("ssh-keyscan is not installed (openssh-client)", 2) from None
    except subprocess.TimeoutExpired:
        raise Failure(f"ssh-keyscan {host} timed out", 5) from None
    for line in done.stdout.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split()
        if len(parts) != 3 or parts[1] != "ssh-ed25519":
            continue
        try:
            blob = base64.b64decode(parts[2], validate=True)
        except (ValueError, binascii.Error):
            continue
        return parts[1], parts[2], fingerprint(blob)
    detail = (done.stderr or "").strip().splitlines()
    raise Failure(
        f"no ssh-ed25519 host key from {host} (exit {done.returncode}"
        + (f": {detail[-1]}" if detail else "")
        + ")",
        5,
    )


def wait_for_host_key(host: str, timeout: float, interval: float) -> dict:
    deadline = time.monotonic() + timeout
    last: Failure | None = None
    while True:
        try:
            key_type, blob, print_ = ssh_keyscan(host)
            return {"type": key_type, "key": f"{key_type} {blob}", "fingerprint": print_}
        except Failure as error:
            last = error
            if time.monotonic() >= deadline:
                raise Failure(f"{error} within {timeout:g}s", 5) from None
            time.sleep(max(1.0, interval))


def prove_no_hq_login(host: str, timeout: float = 15.0) -> dict:
    """Run the card's evidence command and require it to fail.

    `ssh -o BatchMode=yes` cannot authenticate without the user's private key.
    If it *succeeds*, an operator credential exists on a tenant machine and the
    whole access model is broken, so that is a hard failure, not a warning.
    """
    command = [
        "ssh",
        "-F", "/dev/null",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", "PreferredAuthentications=publickey",
        f"root@{host}",
        "true",
    ]
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout + 10
        )
    except FileNotFoundError:
        raise Failure("ssh is not installed (openssh-client)", 2) from None
    except subprocess.TimeoutExpired:
        raise Failure(f"ssh BatchMode probe to {host} timed out", 5) from None
    stderr = (done.stderr or "").strip()
    passed = done.returncode != 0 and "Permission denied" in stderr
    return {
        "command": " ".join(command),
        "exit_code": done.returncode,
        "stderr": stderr.splitlines()[-1] if stderr else "",
        "passed": passed,
    }


# -------------------------------------------------------------------- recording


def record(
    db_path: str,
    *,
    slug: str,
    email: str,
    boot_key: str,
    machine: dict,
    host_key: str | None,
    release: str,
    measured_release: str | None,
) -> dict:
    """Write the web tenant row and the state-machine row, idempotently.

    `tenants` is what manage.py's funnel reads; `tenant_state` is what
    waiting-for-owner reads (initramfs_host_key) and what reconcile uses. This
    card is the first tenant, so there is no claim page yet (ODL-231): the row
    is created here at `provisioned`, and the claim page will replace that step
    without changing either table.

    `release` is the pin's `provision_release`, recorded on both rows — the
    web column ODL-232 reads and the state column `odilectl status` reads.
    `measured_release` is the provider's post-order observation and stays in
    the event detail, exactly as before (ODL-398: never a source).
    """
    import app  # the one schema authority: app.SCHEMA / app.migrate
    import odilectl.state as state_module

    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    expiry = machine.get("expiration_date")
    expires_iso = (
        datetime.fromtimestamp(int(expiry), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if expiry
        else None
    )
    ipv4 = machine.get("ipv4_address")
    con = sqlite3.connect(db_path, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        app.migrate(con)
        con.executescript(state_module.SCHEMA)
        existing = con.execute(
            "SELECT id, slug, email, state FROM tenants WHERE slug = ? OR email = ?",
            (slug, email),
        ).fetchall()
        if len(existing) > 1:
            raise Failure(f"tenants holds {len(existing)} rows for {slug}/{email}", 6)
        if existing:
            row = existing[0]
            if row["slug"] != slug or row["email"] != email:
                raise Failure(
                    f"tenant {row['slug']} already uses {row['email']}; refusing to merge "
                    f"{slug}/{email} into it",
                    6,
                )
            con.execute(
                "UPDATE tenants SET ssh_public_key = ?, state = 'provisioned', server_id = ?,"
                " ipv4 = ?, plan = ?, expires_at = ?, release = COALESCE(release, ?),"
                " claimed_at = COALESCE(claimed_at, ?) WHERE slug = ?",
                (boot_key, machine["id"], ipv4, machine["plan"],
                 expires_iso, release, created_at, slug),
            )
            tenant_row = "updated"
        else:
            con.execute(
                "INSERT INTO tenants (slug, email, ssh_public_key, state, server_id, ipv4,"
                " plan, expires_at, release, claimed_at, created_at)"
                " VALUES (?, ?, ?, 'provisioned', ?, ?, ?, ?, ?, ?, ?)",
                (slug, email, boot_key, machine["id"], ipv4,
                 machine["plan"], expires_iso, release, created_at, created_at),
            )
            tenant_row = "inserted"

        store = state_module.StateStore(con)
        try:
            tenant = store.get(slug)
        except state_module.UnknownTenant:
            store.invite(slug, detail={"source": "scripts/provision_one.py"})
            store.claim(slug)
            tenant = None
        if tenant is None or tenant.state == "invited":
            if tenant is not None:
                store.claim(slug)
        tenant = store.get(slug)
        if tenant.state == "claimed":
            store.provision(
                slug,
                server_id=machine["id"],
                hostname=machine["hostname"],
                ipv4=ipv4,
                plan_id=machine["plan_id"],
                release=release,
                expires_at=int(expiry) if expiry else None,
                initramfs_host_key=host_key,
                detail={"measured_release": measured_release,
                        "boot_key": redact_key(boot_key)},
            )
        elif tenant.state == "provisioning":
            if tenant.server_id not in (None, machine["id"]):
                raise Failure(
                    f"{slug} is already provisioning server {tenant.server_id}, not "
                    f"{machine['id']}",
                    6,
                )
            if tenant.initramfs_host_key and host_key and tenant.initramfs_host_key != host_key:
                raise Failure(
                    f"{slug}: port 22 now offers {host_key}, not the recorded "
                    f"{tenant.initramfs_host_key}; the initramfs key moved",
                    5,
                )
            store.record_release(slug, release)
        else:
            raise Failure(
                f"{slug} is in state {tenant.state}; this script only creates or "
                "completes a provisioning row",
                6,
            )
        con.commit()
        tenant = store.get(slug)
        result = {
            "tenants_row": tenant_row,
            "db": db_path,
            "tenant_state": tenant.state,
            "release": tenant.release,
            "initramfs_host_key": tenant.initramfs_host_key,
        }
    except sqlite3.Error as error:
        con.rollback()
        raise Failure(f"database {db_path}: {error}", 6) from None
    finally:
        con.close()
    return result


# ------------------------------------------------------------------------ steps


class Log:
    """Every step, with the clock the runbook quotes."""

    def __init__(self, echo: bool = True):
        self.started = time.monotonic()
        self.echo = echo
        self.steps: list[dict] = []

    def step(self, name: str, detail: str = "", **facts) -> None:
        elapsed = time.monotonic() - self.started
        self.steps.append(
            {"step": name, "elapsed_s": round(elapsed, 3), "detail": detail, **facts}
        )
        if self.echo:
            stamp = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
            suffix = f" {detail}" if detail else ""
            print(f"[{stamp} +{elapsed:6.2f}s] {name}{suffix}", flush=True)


def provision(args, log: Log) -> dict:
    key_path = Path(args.key).expanduser()
    boot_key, boot_fingerprint = read_boot_key(key_path)
    log.step("key", f"{redact_key(boot_key)} {boot_fingerprint}")

    # The order's boot release is the pin's instruction, read before the
    # account, the plan or the paid create call: an unpinned release must fail
    # with nothing ordered and nothing charged (ODL-398).
    pin = Path(args.pin).expanduser() if args.pin else DEFAULT_PIN
    release = pinned_release(pin)
    log.step("release", f"{release} from {pin} (the pin's provision_release)")

    secret = Path(args.secret_file).expanduser()
    try:
        api_key = secret.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise Failure(f"no reseller key at {secret}", 2) from None
    except OSError as error:
        raise Failure(f"cannot read {secret}: {error.strerror}", 2) from None
    client = Servury(api_key)

    started = time.monotonic()
    account = client.get("account")
    log.step("account", f"balance {account.get('balance')} USD, "
             f"discount {account.get('discount_percent')}% in "
             f"{time.monotonic() - started:.3f}s")

    plans = client.get("plans").get("plans") or []
    plan = find_plan(plans, args.plan)
    price = price_for(float(plan["retail_price"]), args.days, float(account["discount_percent"]))
    balance = Decimal(str(account["balance"]))
    log.step(
        "plan",
        f"{plan['id']} {plan['plan_name']} ({plan['location']}) in_stock={plan['in_stock']} "
        f"queue={plan.get('queue_length')} price={price} USD for {args.days} days",
        plan_id=plan["id"],
    )

    # Find an existing machine before the balance check: a rerun must reuse the
    # machine it ordered, and after the order the balance is deliberately below
    # the price (the money has been spent).
    servers = client.get("servers").get("servers") or []
    existing = [row for row in servers if row.get("hostname") == args.slug]
    if len(existing) > 1:
        raise Failure(f"{len(existing)} machines are named {args.slug}; refusing to guess", 4)
    machine: dict | None = existing[0] if existing else None
    if machine is not None:
        log.step("reuse", f"{machine['id']} already exists as {args.slug} "
                 f"({machine.get('status')})")
        created = False
    else:
        created = True
        if balance < price:
            raise Failure(
                f"balance {balance} USD is below the {price} USD order; nothing was charged",
                3,
            )
        if not plan.get("in_stock") and not args.preorder:
            raise Failure(
                f"plan {plan['id']} is sold out (queue {plan.get('queue_length')}); "
                "nothing was charged. Pass --preorder to pay now and queue (final)",
                3,
            )

    body = {
        "plan_id": plan["id"],
        "hostname": args.slug,
        "os": "debian13",
        "days": args.days,
        "ssh_key": boot_key,
        "snp": True,
        "fde": True,
        "measured": True,
        "preorder": bool(args.preorder),
    }
    shown = dict(body, ssh_key=f"{redact_key(boot_key)}  # exactly one key")
    log.step(
        "request",
        ("POST ?action=create " if created else "not sent (the machine exists) ")
        + json.dumps(shown, ensure_ascii=False),
    )
    if not args.apply:
        if created:
            print(
                "\ndry run: no money spent. The create body above is what --apply sends;\n"
                f"balance {balance} -> {balance - price} USD after the order.\n"
                "Pass --apply to order the machine.",
                flush=True,
            )
        else:
            assert machine is not None
            print(
                f"\ndry run: {args.slug} already exists as {machine['id']}; --apply would "
                "verify and record it, not order a second machine.",
                flush=True,
            )
        return {
            "dry_run": True,
            "request": shown,
            "plan": plan,
            "price": str(price),
            "balance": str(balance),
            "balance_after": str(balance - price),
            "steps": log.steps,
        }

    if created:
        started = time.monotonic()
        data = client.post("create", body)
        elapsed = time.monotonic() - started
        server_id = str(data.get("server_id") or data.get("id") or "")
        if not server_id:
            raise Failure(f"create: response has no server_id: {data}", 4)
        log.step(
            "create",
            f"HTTP accepted in {elapsed:.3f}s server_id={server_id} "
            f"status={data.get('status')} amount={data.get('amount_charged')} "
            f"invoice={data.get('invoice_id')} expiry={data.get('expiration_date')}",
            server_id=server_id,
            create=data,
        )
        if str(data.get("status") or "").lower() == "queued":
            return _queued(args, plan, boot_key, boot_fingerprint, data, log, release)
        poll_started = time.monotonic()
        deadline = poll_started + args.timeout
        while True:
            server = client.get("server", server_id=server_id).get("server") or {}
            status = str(server.get("status") or "unknown")
            if status == "queued":
                queued = dict(server)
                queued.setdefault("expiration_date", data.get("expiration_date"))
                return _queued(args, plan, boot_key, boot_fingerprint, queued, log, release)
            if status in ("failed", "terminated"):
                raise Failure(
                    f"server {server_id} is {status}: {server.get('status_reason')} "
                    "(a failed deploy is refunded; terminate the row and order again)",
                    4,
                )
            if status == "active" and server.get("ipv4_address"):
                break
            if time.monotonic() >= deadline:
                raise Failure(
                    f"server {server_id} not active with an address within "
                    f"{args.timeout:g}s (last status {status})",
                    5,
                )
            time.sleep(max(1.0, args.interval))
        log.step(
            "active",
            f"{server_id} active with {server['ipv4_address']} after "
            f"{time.monotonic() - poll_started:.1f}s polling",
            server=server,
        )
    else:
        server = machine
        assert server is not None
        log.step("active", f"{server['id']} active with {server.get('ipv4_address')}",
                 server=server)

    # The vendor's own flags are the first half of the proof, not a formality.
    if server.get("measured_boot") is not True:
        raise Failure(f"{server['id']}: measured_boot is {server.get('measured_boot')!r}", 5)
    measured = server.get("measured_release")
    if not release_at_least(measured, MIN_RELEASE):
        raise Failure(
            f"{server['id']}: measured release {measured!r} is below 2026.11.1; its initramfs "
            "host key would change on every boot and break waiting-for-owner",
            5,
        )
    if server.get("snp_enabled") is not True:
        raise Failure(f"{server['id']}: snp_enabled is {server.get('snp_enabled')!r}", 5)
    if server.get("fde_requested") is not True:
        raise Failure(f"{server['id']}: fde_requested is {server.get('fde_requested')!r}", 5)
    log.step(
        "flags",
        f"snp={server.get('snp_enabled')} fde_requested={server.get('fde_requested')} "
        f"measured={server.get('measured_boot')} provider_release={measured}",
    )

    started = time.monotonic()
    host = wait_for_host_key(server["ipv4_address"], args.host_key_timeout, args.interval)
    log.step(
        "host-key",
        f"{host['fingerprint']} from port 22 in {time.monotonic() - started:.1f}s",
        fingerprint=host["fingerprint"],
    )

    probe = prove_no_hq_login(server["ipv4_address"])
    log.step(
        "no-hq-login",
        f"exit {probe['exit_code']} ({probe['stderr'] or 'no stderr'})",
        probe=probe,
    )
    if not probe["passed"]:
        raise Failure(
            "ssh -o BatchMode=yes did not fail with Permission denied: "
            "HQ may hold a credential on the tenant",
            5,
        )

    started = time.monotonic()
    recorded = record(
        args.db,
        slug=args.slug,
        email=args.email,
        boot_key=boot_key,
        machine={
            **server,
            "plan_id": plan["id"],
            "plan": plan["plan_name"],
            "expiration_date": server.get("expiration_date"),
        },
        host_key=host["fingerprint"],
        release=release,
        measured_release=measured,
    )
    log.step("record", f"{recorded['tenant_state']} in {args.db} "
             f"in {time.monotonic() - started:.3f}s", recorded=recorded)

    return {
        "dry_run": False,
        "created": created,
        "server_id": server["id"],
        "hostname": server.get("hostname"),
        "ipv4": server.get("ipv4_address"),
        "plan_id": plan["id"],
        "plan": plan["plan_name"],
        "expires_at": server.get("expiration_date"),
        "expires_iso": datetime.fromtimestamp(
            int(server["expiration_date"]), timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "release": release,
        "measured_release": measured,
        "initramfs_host_key": host["fingerprint"],
        "boot_key": redact_key(boot_key),
        "boot_key_fingerprint": boot_fingerprint,
        "ssh_probe": probe,
        "recorded": recorded,
        "steps": log.steps,
    }


def _queued(
    args, plan: dict, boot_key: str, boot_fingerprint: str, data: dict, log: Log,
    release: str,
) -> dict:
    """A paid pre-order: record it now, verify the machine when it deploys.

    A queued order has no address yet, so the flags, the host key and the
    no-login probe have to wait for a rerun; the money has already left and the
    order is final, so the row must exist either way. The pin's release is
    recorded now: it is the release the order was placed to boot.
    """
    server_id = str(data.get("server_id") or data.get("id") or "")
    if not server_id:
        raise Failure(f"queued order has no server id: {data}", 4)
    position = data.get("queue_position")
    log.step(
        "queued",
        f"{server_id} paid and queued at position {position}; the term starts when "
        "it deploys, and a pre-order is final",
    )
    machine = {
        "id": server_id,
        "hostname": args.slug,
        "ipv4_address": data.get("ipv4_address"),
        "plan": plan["plan_name"],
        "plan_id": plan["id"],
        "expiration_date": data.get("expiration_date"),
    }
    recorded = record(
        args.db,
        slug=args.slug,
        email=args.email,
        boot_key=boot_key,
        machine=machine,
        host_key=None,
        release=release,
        measured_release=None,
    )
    log.step("record", f"{recorded['tenant_state']} in {args.db}")
    return {
        "dry_run": False,
        "created": True,
        "queued": True,
        "queue_position": position,
        "server_id": server_id,
        "expires_at": machine["expiration_date"],
        "boot_key": redact_key(boot_key),
        "boot_key_fingerprint": boot_fingerprint,
        "recorded": recorded,
        "steps": log.steps,
    }


# --------------------------------------------------------------------- self-test

def self_test() -> int:
    """Pure checks only: no socket, no database, no money."""
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(("ok   " if condition else "FAIL ") + name + (f" — {detail}" if detail else ""))
        if not condition:
            failures.append(name)

    fixture = (
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICFh7hQkST5vRDolO3yqwCARRugZahMdc+WgR69JsoXG t@e"
    )
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        good = Path(tmp) / "one.pub"
        good.write_text(fixture + "\n", encoding="utf-8")
        key, print_ = read_boot_key(good)
        check("one key parses", key.split()[0] == "ssh-ed25519")
        check(
            "fingerprint is OpenSSH's",
            print_ == "SHA256:wUuvXx+gnMMh9fB0VyzJ32HT8vs7XtvGzMlxBVqD93M",
            print_,
        )

        two = Path(tmp) / "two.pub"
        two.write_text(fixture + "\n" + fixture + "\n", encoding="utf-8")
        try:
            read_boot_key(two)
            check("two keys are refused", False)
        except Failure as error:
            check("two keys are refused", "exactly one" in str(error))

        private = Path(tmp) / "id_ed25519"
        private.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n", encoding="utf-8")
        try:
            read_boot_key(private)
            check("a private key is refused", False)
        except Failure as error:
            check("a private key is refused", "private" in str(error).lower())

        forced = Path(tmp) / "forced.pub"
        forced.write_text(f'command="x" {fixture}\n', encoding="utf-8")
        try:
            read_boot_key(forced)
            check("options are refused", False)
        except Failure as error:
            check("options are refused", "authorized_keys" in str(error))

        truncated = Path(tmp) / "short.pub"
        truncated.write_text("ssh-ed25519 AAAAC3\n", encoding="utf-8")
        try:
            read_boot_key(truncated)
            check("a truncated key is refused", False)
        except Failure as error:
            check("a truncated key is refused", "base64" in str(error) or "body" in str(error))

        # The release comes from the shared pin helper, never the provider.
        good_pin = Path(tmp) / "good.pin"
        good_pin.write_text(
            json.dumps({"provision_release": "2026.11.1"}), encoding="utf-8"
        )
        check("the pin's provision_release is the order's release",
              pinned_release(good_pin) == "2026.11.1")
        bare_pin = Path(tmp) / "bare.pin"
        bare_pin.write_text(json.dumps({"schema": 2}), encoding="utf-8")
        try:
            pinned_release(bare_pin)
            check("a pin without provision_release is refused", False)
        except OdileError as error:
            check("a pin without provision_release is refused",
                  "provision_release" in str(error) and "unpinned" in str(error))
        bad_pin = Path(tmp) / "bad.pin"
        bad_pin.write_text(json.dumps({"provision_release": "latest"}), encoding="utf-8")
        try:
            pinned_release(bad_pin)
            check("a pin release that is not an id is refused", False)
        except OdileError as error:
            check("a pin release that is not an id is refused",
                  "not a release id" in str(error))

    check("price 19.99/31d/-5% = 18.99", price_for(19.99, 31, 5) == Decimal("18.99"),
          str(price_for(19.99, 31, 5)))
    check("price 19.99/7d/-5% = 4.28", price_for(19.99, 7, 5) == Decimal("4.28"),
          str(price_for(19.99, 7, 5)))
    check("price 9.99/31d/-5% = 9.49", price_for(9.99, 31, 5) == Decimal("9.49"),
          str(price_for(9.99, 31, 5)))
    check("2026.11.1 passes the pin", release_at_least("2026.11.1", MIN_RELEASE))
    check("2026.12 passes the pin", release_at_least("2026.12", MIN_RELEASE))
    check("2026.11 fails the pin", not release_at_least("2026.11", MIN_RELEASE))
    check("2026.09 fails the pin", not release_at_least("2026.09", MIN_RELEASE))
    check("no release fails the pin", not release_at_least(None, MIN_RELEASE))
    shown = redact_key(fixture)
    check("redaction hides the body", fixture.split()[1] not in shown, shown)
    check("redaction keeps the type", shown.startswith("ssh-ed25519 "))
    check("slug accepts odl-7f3a2c", bool(SLUG_RE.match("odl-7f3a2c")))
    check("slug rejects odl-7F3A2C", not SLUG_RE.match("odl-7F3A2C"))
    check("email accepts ada@example.com", bool(EMAIL_RE.match("ada@example.com")))
    check("email rejects a bare word", not EMAIL_RE.match("ada"))

    if failures:
        print(f"self-test: {len(failures)} failure(s)")
        return 1
    print("self-test: OK")
    return 0


# -------------------------------------------------------------------------- main


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provision exactly one real tenant with the user's boot key.",
        epilog="The default is a dry run; --apply spends money.",
    )
    parser.add_argument("--slug", help="tenant slug, odl- plus six hex digits")
    parser.add_argument("--email", help="the tenant's email (tenants.email)")
    parser.add_argument("--key", help="the user's boot PUBLIC key file (exactly one line)")
    parser.add_argument("--plan", type=int, default=DEFAULT_PLAN,
                        help=f"plan id (default {DEFAULT_PLAN}: Blackbox Nano)")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"term in days (default {DEFAULT_DAYS})")
    parser.add_argument("--preorder", action="store_true", help="pay now and queue a sold-out plan")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database (default {DEFAULT_DB})")
    parser.add_argument("--secret-file", default=SECRET_FILE, help="reseller key file")
    parser.add_argument("--pin", default=None,
                        help=f"release pin whose provision_release is recorded "
                             f"(default {DEFAULT_PIN})")
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds to wait for active")
    parser.add_argument("--host-key-timeout", type=float, default=120.0,
                        help="seconds to wait for port 22 after active")
    parser.add_argument("--interval", type=float, default=10.0, help="seconds between polls")
    parser.add_argument("--apply", action="store_true", help="order the machine (spends money)")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    parser.add_argument("--self-test", action="store_true", help="run the pure checks and exit")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.self_test:
        return self_test()
    missing = [name for name in ("slug", "email", "key") if not getattr(args, name)]
    if missing:
        print(f"provision_one: --{' --'.join(missing)} required", file=sys.stderr)
        return 2
    if not SLUG_RE.match(args.slug):
        print("provision_one: slug must be odl- followed by six lowercase hex digits",
              file=sys.stderr)
        return 2
    if not EMAIL_RE.match(args.email):
        print(f"provision_one: {args.email!r} is not an email address", file=sys.stderr)
        return 2
    if not 7 <= args.days <= 365:
        print("provision_one: --days must be 7..365", file=sys.stderr)
        return 2

    log = Log(echo=not args.json)
    try:
        summary = provision(args, log)
    except OdileError as error:
        # The shared pin helper's usage error: never a traceback, and nothing
        # ordered or charged (ODL-398).
        print(f"provision_one: {error}", file=sys.stderr)
        return error.exit_code
    except ProviderError as error:
        print(f"provision_one: {error}", file=sys.stderr)
        return error.code
    except Failure as error:
        print(f"provision_one: {error}", file=sys.stderr)
        return error.code

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    if summary.get("dry_run"):
        return 0
    if summary.get("queued"):
        print(
            "\nqueued (paid; a pre-order is final)\n"
            f"  server_id   {summary['server_id']}\n"
            f"  queue       {summary['queue_position']}\n"
            f"  tenant      {summary['recorded']['tenants_row']} in "
            f"{summary['recorded']['db']}\n"
            "  rerun --apply with the same slug once it is active to verify the "
            "flags, the host key and the no-login probe\n",
            flush=True,
        )
        return 0
    print(
        "\nprovisioned\n"
        f"  server_id   {summary['server_id']}\n"
        f"  ipv4        {summary['ipv4']}\n"
        f"  plan        {summary['plan_id']} {summary['plan']}\n"
        f"  expires     {summary['expires_iso']} ({summary['expires_at']})\n"
        f"  release     {summary['release']} (the pin; provider measured "
        f"{summary['measured_release']})\n"
        f"  host key    {summary['initramfs_host_key']}\n"
        f"  boot key    {summary['boot_key']} {summary['boot_key_fingerprint']}\n"
        f"  tenants     {summary['recorded']['tenants_row']} in {summary['recorded']['db']}\n"
        f"  no HQ login ssh exit {summary['ssh_probe']['exit_code']} "
        f"({summary['ssh_probe']['stderr']})\n",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
