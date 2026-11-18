#!/usr/bin/python3
"""Point demo.odile.fyi at the machine that serves it.

The demo lives on Odile's tenant machine since ODL-503 (203.0.113.87); before
that it was 203.0.113.139, the HQ box. Cloudflare is the zone's authority. The
token lives at /root/.odile/cloudflare_dns_token (mode 0600, read at run time,
never printed); the zone is looked up by name, so no id is committed.

Idempotent: an existing record with the right type, content and DNS-only
proxying prints `same` and changes nothing. A wrong record is updated in place
(PATCH), never deleted and recreated. --dry-run prints the plan.

The record is DNS-only (proxied false), like odile.fyi and testing.odile.fyi:
Caddy terminates TLS on the machine, and Cloudflare's proxy would answer the
ACME challenge itself or move the connection to a Cloudflare edge.

    ./deploy/demo-host/create_dns.py --dry-run
    ./deploy/demo-host/create_dns.py                     # the tenant
    ./deploy/demo-host/create_dns.py --target 203.0.113.139   # HQ's address (no demo there since 2026-11-13)

Exit codes: 0 success (created, updated or already converged), 1 the API
refused the write, 2 a missing token or bad response. If the token cannot
write, stop and comment on the card — do not invent a hostname.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_FILE = Path("/root/.odile/cloudflare_dns_token")
API = "https://api.cloudflare.com/client/v4"
ZONE_NAME = "odile.fyi"
RECORD_NAME = "demo.odile.fyi"
RECORD_TYPE = "A"
#: The tenant machine (odl-fake01) — the demo's home since ODL-503.
TENANT_IP = "203.0.113.87"
#: The HQ box — the fallback, one flag away: `--target 203.0.113.139`.
HQ_IP = "203.0.113.139"
PROXIED = False
TTL = 1  # 1 = automatic


def read_token() -> str:
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"error: cannot read {TOKEN_FILE}: {exc}") from exc
    if not token:
        raise SystemExit(f"error: {TOKEN_FILE} is empty")
    return token


def api(token: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"error: Cloudflare {method} {path} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"error: Cloudflare unreachable: {exc.reason}") from exc
    if not payload.get("success"):
        raise SystemExit(f"error: Cloudflare {method} {path} refused: {payload.get('errors')}")
    return payload


def zone_id(token: str) -> str:
    query = urllib.parse.urlencode({"name": ZONE_NAME, "per_page": 50})
    payload = api(token, "GET", f"/zones?{query}")
    zones = payload.get("result") or []
    if len(zones) != 1:
        raise SystemExit(f"error: expected exactly one zone named {ZONE_NAME}, found {len(zones)}")
    return zones[0]["id"]


def existing(token: str, zone: str) -> dict | None:
    query = urllib.parse.urlencode({"type": RECORD_TYPE, "name": RECORD_NAME, "per_page": 50})
    payload = api(token, "GET", f"/zones/{zone}/dns_records?{query}")
    records = [r for r in payload.get("result") or [] if r.get("name") == RECORD_NAME]
    if len(records) > 1:
        raise SystemExit(f"error: {len(records)} records named {RECORD_NAME}; expected at most one")
    return records[0] if records else None


def switch_back() -> str:
    """The one line the runbook repeats: how the fallback is taken."""
    return f"./deploy/demo-host/create_dns.py --target {HQ_IP}"


def ensure(target: str, dry_run: bool = False) -> int:
    token = read_token()
    zone = zone_id(token)
    record = existing(token, zone)
    desired = {
        "type": RECORD_TYPE,
        "name": RECORD_NAME,
        "content": target,
        "proxied": PROXIED,
        "ttl": TTL,
    }

    if record is None:
        print(f"{'would create' if dry_run else 'create':<12} {RECORD_TYPE} {RECORD_NAME} "
              f"-> {target} (proxied={PROXIED}, ttl={TTL})")
        if not dry_run:
            api(token, "POST", f"/zones/{zone}/dns_records", desired)
            print("created      record is live")
        return 0

    same = (
        record.get("type") == RECORD_TYPE
        and record.get("content") == target
        and bool(record.get("proxied")) is PROXIED
    )
    if same:
        print(f"same         {RECORD_TYPE} {RECORD_NAME} -> {target} (proxied={PROXIED})")
        return 0

    print(f"{'would update' if dry_run else 'update':<12} {RECORD_TYPE} {RECORD_NAME} "
          f"-> {target} (was {record.get('content')}, proxied={record.get('proxied')})")
    if not dry_run:
        api(token, "PATCH", f"/zones/{zone}/dns_records/{record['id']}", desired)
        print("updated      record is live")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Point demo.odile.fyi at the machine that serves it (the tenant by default).")
    parser.add_argument("-n", "--dry-run", action="store_true", help="show the plan, change nothing")
    parser.add_argument("--target", default=TENANT_IP,
                        help=f"the A record's address (default {TENANT_IP}; {HQ_IP} is the fallback)")
    args = parser.parse_args(argv)

    code = ensure(args.target, args.dry_run)
    if args.target == TENANT_IP:
        print(f"fallback     {switch_back()}")
    return code


if __name__ == "__main__":
    sys.exit(main())
