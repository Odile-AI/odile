"""The Servury Blackbox provider — the only file that knows the vendor.

API: https://servury.com/docs/?api=blackbox, read 2026-11-07. The rules this
file exists to keep:

* authentication is the `X-Reseller-Key` header, never a query parameter;
* every POST carries a JSON body and `action`/`id` stay in the query string;
* a non-2xx response is an error even when the JSON says `success`, and the
  message is the provider's own sentence, not a stack trace;
* the reseller key appears in the header and nowhere in an error or a log;
* a 404 from `terminate` means the machine is already gone, so destroy is
  idempotent.

`transport` is the test seam: it takes `(method, url, headers, body)` and
returns `(status, body_bytes)`. The default is `urllib.request`; tests pass a
recorded transport and no socket is opened.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from ..errors import ProviderError, ProviderTimeout
from .base import POWER_ACTIONS, CreateSpec, Machine, Provider

DEFAULT_BASE_URL = "https://servury.com/api/reseller.php"
DEFAULT_TIMEOUT = 30.0
NAME = "servury"

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]

# The vendor's status words, folded into the six the interface speaks.
_STATE = {
    "queued": "pending",
    "deploying": "pending",
    "reinstalling": "pending",
    "active": "active",
    "suspended": "suspended",
    "failed": "failed",
    "terminated": "terminated",
    "deleted": "terminated",
}


def _default_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class ServuryProvider(Provider):
    """`https://servury.com/api/reseller.php` behind the five-call interface."""

    name = NAME

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        if not api_key or not api_key.strip():
            raise ValueError("ServuryProvider needs a reseller key")
        self._api_key = api_key.strip()
        self._base_url = base_url
        self._timeout = timeout
        self._transport = transport or (
            lambda method, url, headers, body: _default_transport(
                method, url, headers, body, self._timeout
            )
        )

    # ------------------------------------------------------------------ public

    def create(self, spec: CreateSpec) -> Machine:
        params = {
            "plan_id": spec.plan_id,
            "hostname": spec.hostname,
            "os": spec.os,
            "days": spec.days,
            "ssh_key": spec.ssh_key,
            "snp": spec.snp,
            "fde": spec.fde,
            "measured": spec.measured,
            "preorder": spec.preorder,
        }
        data = self._call("POST", "create", params=params)
        machine = self._machine(data, fallback_hostname=spec.hostname)
        detail = machine.detail
        for key in ("message", "invoice_id", "amount_charged"):
            if data.get(key) is not None:
                detail[key] = data[key]
        return machine

    def poll(self, server_id: str, *, timeout: float = 900.0, interval: float = 10.0) -> Machine:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            machine = self._fetch(server_id)
            if machine.state in ("failed", "terminated"):
                reason = machine.detail.get("status_reason")
                why = f" ({reason})" if reason else ""
                raise ProviderError(
                    f"server {server_id} is {machine.raw_status or machine.state}{why}"
                )
            # The vendor doc is explicit: wait for active *and* the addresses.
            # An active machine with no IPv4 is not reachable and not a tenant.
            if machine.state == "active" and machine.ipv4:
                return machine
            if time.monotonic() >= deadline:
                if machine.state == "active":
                    last = "active but no IPv4 address yet"
                else:
                    last = f"last status: {machine.raw_status or machine.state}"
                raise ProviderTimeout(
                    f"server {server_id} did not become ready within {timeout:g}s ({last})"
                )
            if interval > 0:
                time.sleep(interval)

    def power(self, server_id: str, action: str) -> Machine:
        if action not in POWER_ACTIONS:
            raise ValueError(f"power action must be one of {', '.join(POWER_ACTIONS)}")
        # The API verb (`power`) is in the query string. The verb to apply
        # (`restart`) travels in the JSON body; the doc's "action is always read
        # from the query string" is ambiguous about this second field, so
        # ODL-201's live capture must confirm where the provider reads it.
        self._call("POST", "power", params={"action": action}, server_id=server_id)
        return self._fetch(server_id)

    def terminate(self, server_id: str) -> bool:
        try:
            self._call("POST", "terminate", params={}, server_id=server_id)
        except ProviderError as error:
            if error.status == 404:
                return False
            raise
        return True

    def list(self) -> list[Machine]:
        data = self._call("GET", "servers")
        rows = data.get("servers")
        if not isinstance(rows, list):
            raise ProviderError("servers: response has no 'servers' list")
        return [self._machine(row) for row in rows]

    # ----------------------------------------------------------------- private

    def _fetch(self, server_id: str) -> Machine:
        data = self._call("GET", "server", server_id=server_id)
        row = data.get("server")
        if not isinstance(row, dict):
            raise ProviderError(f"server {server_id}: response has no 'server' object")
        return self._machine(row)

    def _call(
        self,
        method: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        server_id: str | None = None,
    ) -> dict[str, Any]:
        url = self._base_url
        url += ("&" if "?" in url else "?") + f"action={urllib.parse.quote(action)}"
        if server_id is not None:
            url += f"&id={urllib.parse.quote(server_id)}"
        headers = {"X-Reseller-Key": self._api_key, "Accept": "application/json"}
        body: bytes | None = None
        if method == "POST":
            body = json.dumps(params or {}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            status, raw = self._transport(method, url, headers, body)
        except OSError as error:
            raise ProviderError(f"{action}: cannot reach the provider: {error}") from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            excerpt = raw[:200].decode("utf-8", "replace")
            raise ProviderError(
                f"{action}: HTTP {status} with a non-JSON body: {excerpt!r}", status=status
            ) from None
        if not isinstance(payload, dict):
            raise ProviderError(
                f"{action}: HTTP {status} with a non-object JSON body", status=status
            )
        if status >= 400 or not payload.get("success"):
            message = payload.get("error") or payload.get("message") or "no message"
            raise ProviderError(f"{action}: HTTP {status}: {message}", status=status)
        data = payload.get("data")
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ProviderError(f"{action}: response 'data' is not an object")
        return data

    def _machine(self, row: dict[str, Any], fallback_hostname: str = "") -> Machine:
        if not isinstance(row, dict):
            raise ProviderError("provider response row is not an object")
        server_id = row.get("id") or row.get("server_id")
        if not server_id:
            raise ProviderError("provider response has no server id")
        raw_status = str(row.get("status") or "unknown")
        expires_at = row.get("expiration_date")
        if isinstance(expires_at, str) and expires_at.isdigit():
            expires_at = int(expires_at)
        detail: dict[str, Any] = {}
        for key in ("status_reason", "powered_on", "expired", "plan", "queue_position"):
            if row.get(key) is not None:
                detail[key] = row[key]
        plan_id = row.get("plan_id")
        return Machine(
            server_id=str(server_id),
            hostname=str(row.get("hostname") or fallback_hostname),
            state=_STATE.get(raw_status.lower(), "unknown"),
            ipv4=row.get("ipv4_address") or row.get("ipv4"),
            ipv6=row.get("ipv6_address") or row.get("ipv6"),
            expires_at=expires_at if isinstance(expires_at, int) else None,
            plan_id=plan_id if isinstance(plan_id, int) else None,
            raw_status=raw_status,
            detail=detail,
        )


def build(config) -> Provider:
    """Build this provider from an `odilectl.config.Config`; the registry calls it."""
    return ServuryProvider(
        api_key=config.require("reseller_key"),
        base_url=config.get("servury_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL,
    )
