"""Create, settle and delete the tenant's A record in Cloudflare DNS.

The record is **DNS-only** (`proxied: false`): Cloudflare answers the name, but
every byte still ends at the tenant's own machine. Proxying it would put
Cloudflare in the data path and break the privacy diagram, so this file never
sends `proxied: true` — not on create and not on update.

Three mutating verbs, each safe to run twice:

* ``create SLUG --ip ADDR`` writes A ``<slug>.<zone>`` with TTL 120 during
  provisioning and converges an existing record in place (PATCH, never
  delete-and-recreate);
* ``settle SLUG`` moves the TTL to 300 once provisioning is done;
* ``delete SLUG`` removes the record; a second delete is a no-op because an
  absent record is success.

Resolution is deliberately not re-implemented here: `dig @<zone-ns>` is the
authoritative check (and the card's evidence), while a system-resolver wrapper
would report a cached NXDOMAIN as a missing record.

A tenant slug is ``odl-`` plus six random hex characters (`new_slug()`), so it
never contains the user's name. The only non-tenant labels accepted are
reserved underscore labels such as ``_odile-test`` — the live leg uses that one
on the real zone.

Token scope (documented, minimal)
---------------------------------
A Cloudflare API token with **Zone → DNS → Edit**, restricted to the single
zone ``odile.fyi``. That is the one permission the create, update and delete
calls need; no account-level permission is involved. The zone is looked up by
name with a read on that same zone; the deployed token performs it (observed
2026-11-08: the token listed exactly one zone, ``odile.fyi``, and
``GET /accounts`` returned an empty list). A token that cannot list zones still
works with ``--zone-id`` — the zone id is an identifier, not a secret.

The token is read at run time — ``dns_token`` in the 0600 config, or the file
``dns_token_file`` (default ``/root/.odile/cloudflare_dns_token``, mode 0600)
— and is never printed, put in a URL or included in an error.

Config keys this file reads (all optional): ``dns_token``, ``dns_token_file``,
``dns_zone``, ``dns_zone_id``.

    python3 -m odilectl.dns create odl-7f3a2c --ip 203.0.113.120 --apply
    python3 -m odilectl.dns settle odl-7f3a2c --apply
    python3 -m odilectl.dns delete odl-7f3a2c --apply

The module follows the command-module protocol (``NAME``, ``HELP``,
``EXAMPLE``, ``add_arguments``, ``run``) so ``cli.py`` can register it with one
line; until then ``python3 -m odilectl.dns`` is the entry point.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO

from .config import DEFAULT_CONFIG_PATH, ENV_CONFIG_PATH, Config
from .core import Context, Report, applied, plan
from .errors import ConfigError, OdileError, ProviderError, UsageError

NAME = "dns"
HELP = "Create, settle or delete the tenant's DNS A record (DNS-only)."
EXAMPLE = "odilectl dns create odl-7f3a2c --ip 203.0.113.120 --apply"

API = "https://api.cloudflare.com/client/v4"
DEFAULT_ZONE = "odile.fyi"
DEFAULT_TOKEN_FILE = Path("/root/.odile/cloudflare_dns_token")
DEFAULT_TIMEOUT = 30.0
PROVISION_TTL = 120
SETTLED_TTL = 300
VERBS = ("create", "settle", "delete")

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TENANT_SLUG = re.compile(r"^odl-[0-9a-f]{6}$")
_ZONE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


# ------------------------------------------------------------------- names and slugs


def new_slug() -> str:
    """A random tenant slug: ``odl-`` plus six hex characters, nothing personal."""
    return "odl-" + secrets.token_hex(3)


def validate_slug(slug: str) -> str:
    """Return the lower-case DNS label for ``slug``, or raise `ValueError`.

    A tenant slug must be exactly ``odl-`` plus six hex characters; a label
    starting with ``_`` is reserved for live test records such as
    ``_odile-test``. This is where "the slug never contains the user's name"
    is enforced at the DNS boundary.
    """
    value = (slug or "").strip().lower()
    if not value or len(value) > 63:
        raise ValueError("slug must be a DNS label of 1-63 characters")
    body = value[1:] if value.startswith("_") else value
    if not body or not _LABEL.match(body):
        raise ValueError(
            "slug must be a DNS label: letters, digits and hyphens, not "
            "starting or ending with a hyphen"
        )
    if not value.startswith("_") and not _TENANT_SLUG.match(value):
        raise ValueError(
            "a tenant slug is 'odl-' plus six hex characters (use the reserved "
            "'_name' form only for a live test record)"
        )
    return value


def validate_zone(zone: str) -> str:
    """Return the lower-case zone name, or raise `ValueError`."""
    value = (zone or "").strip().lower()
    if len(value) > 253 or not _ZONE.match(value):
        raise ValueError(f"{zone!r} is not a DNS zone name")
    return value


def record_name(slug: str, zone: str) -> str:
    """The fully qualified A record name for a tenant slug."""
    return f"{validate_slug(slug)}.{validate_zone(zone)}"


def validate_ipv4(value: str) -> str:
    """An A record takes exactly one IPv4 address."""
    try:
        address = ipaddress.ip_address((value or "").strip())
    except ValueError:
        raise ValueError(f"{value!r} is not an IP address") from None
    if address.version != 4:
        raise ValueError(f"{value!r} is IPv6; an A record takes an IPv4 address")
    return str(address)


def validate_ttl(value: int) -> int:
    """Cloudflare accepts 1 (automatic) or 60-86400 seconds."""
    if value == 1 or 60 <= value <= 86400:
        return value
    raise ValueError("ttl must be 1 (automatic) or between 60 and 86400 seconds")


# ------------------------------------------------------------------------ the record


@dataclass(frozen=True)
class Record:
    """One Cloudflare DNS record, in the few fields this file manages."""

    id: str
    name: str
    type: str
    content: str
    ttl: int
    proxied: bool

    @classmethod
    def from_api(cls, row: Any) -> "Record":
        if not isinstance(row, dict):
            raise ProviderError("dns: Cloudflare returned a record that is not an object")
        record_id = row.get("id")
        name = row.get("name")
        if not record_id or not name:
            raise ProviderError("dns: Cloudflare returned a record without an id or name")
        ttl = row.get("ttl", 1)
        return cls(
            id=str(record_id),
            name=str(name),
            type=str(row.get("type") or "A"),
            content=str(row.get("content") or ""),
            ttl=ttl if isinstance(ttl, int) else 1,
            proxied=bool(row.get("proxied", False)),
        )


@dataclass(frozen=True)
class Change:
    """What a verb did, or would do: created, updated, same, deleted or absent."""

    action: str
    record: Record | None = None
    previous: Record | None = None


def _matches(record: Record, ipv4: str, ttl: int) -> bool:
    return record.content == ipv4 and record.ttl == ttl and not record.proxied


# ---------------------------------------------------------------------- the client


def _default_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class CloudflareDNS:
    """The zone-scoped Cloudflare DNS API, behind the verbs this file needs."""

    def __init__(
        self,
        token: str,
        zone: str,
        *,
        zone_id: str | None = None,
        transport: Transport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        if not token or not token.strip():
            raise ValueError("CloudflareDNS needs a token")
        self._token = token.strip()
        self.zone = validate_zone(zone)
        self._zone_id = zone_id
        self._timeout = timeout
        self._transport = transport or (
            lambda method, url, headers, body: _default_transport(
                method, url, headers, body, self._timeout
            )
        )

    # ------------------------------------------------------------------ public

    def zone_id(self) -> str:
        """The zone id, looked up by name once and cached."""
        if self._zone_id:
            return self._zone_id
        query = urllib.parse.urlencode({"name": self.zone, "per_page": 50})
        found = self._call("GET", f"/zones?{query}")
        if not isinstance(found, list):
            raise ProviderError("dns: Cloudflare returned no zone list")
        if len(found) != 1:
            raise ProviderError(
                f"dns: expected exactly one zone named {self.zone}, found {len(found)}"
            )
        zone_id = found[0].get("id") if isinstance(found[0], dict) else None
        if not zone_id:
            raise ProviderError(f"dns: zone {self.zone} has no id")
        self._zone_id = str(zone_id)
        return self._zone_id

    def find(self, name: str) -> Record | None:
        """The one A record named ``name``, or None."""
        query = urllib.parse.urlencode({"type": "A", "name": name, "per_page": 100})
        found = self._call("GET", f"/zones/{self.zone_id()}/dns_records?{query}")
        if not isinstance(found, list):
            raise ProviderError(f"dns: Cloudflare returned no record list for {name}")
        records = [
            Record.from_api(row)
            for row in found
            if isinstance(row, dict)
            and str(row.get("type")) == "A"
            and str(row.get("name") or "").lower() == name.lower()
        ]
        if len(records) > 1:
            raise ProviderError(
                f"dns: {len(records)} A records are named {name}; refusing to guess"
            )
        return records[0] if records else None

    def ensure(self, name: str, ipv4: str, ttl: int = PROVISION_TTL, *, apply: bool = True) -> Change:
        """Create or converge the DNS-only A record; a second run changes nothing."""
        validate_ttl(ttl)
        desired = {"type": "A", "name": name, "content": ipv4, "proxied": False, "ttl": ttl}
        record = self.find(name)
        if record is None:
            if not apply:
                return Change("created")
            row = self._call("POST", f"/zones/{self.zone_id()}/dns_records", desired)
            return Change("created", Record.from_api(row))
        if _matches(record, ipv4, ttl):
            return Change("same", record, record)
        change = Change("updated", previous=record)
        if not apply:
            return change
        row = self._call(
            "PATCH", f"/zones/{self.zone_id()}/dns_records/{record.id}", desired
        )
        return Change("updated", Record.from_api(row), record)

    def settle(self, name: str, ttl: int = SETTLED_TTL, *, apply: bool = True) -> Change:
        """Move an existing record's TTL to the settled value."""
        validate_ttl(ttl)
        record = self.find(name)
        if record is None:
            raise ProviderError(f"dns: no A record named {name} to settle")
        if record.ttl == ttl and not record.proxied:
            return Change("same", record, record)
        change = Change("updated", previous=record)
        if not apply:
            return change
        body = {
            "type": "A",
            "name": name,
            "content": record.content,
            "proxied": False,
            "ttl": ttl,
        }
        row = self._call(
            "PATCH", f"/zones/{self.zone_id()}/dns_records/{record.id}", body
        )
        return Change("updated", Record.from_api(row), record)

    def remove(self, name: str, *, apply: bool = True) -> Change:
        """Delete the record; an absent record (or a 404) is already gone."""
        record = self.find(name)
        if record is None:
            return Change("absent")
        change = Change("deleted", record)
        if not apply:
            return change
        try:
            self._call("DELETE", f"/zones/{self.zone_id()}/dns_records/{record.id}")
        except ProviderError as error:
            if error.status == 404:
                return Change("absent", record)
            raise
        return change

    # ----------------------------------------------------------------- private

    def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        url = f"{API}{path}"
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            status, raw = self._transport(method, url, headers, data)
        except OSError as error:
            raise ProviderError(f"dns: cannot reach Cloudflare: {error}") from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            excerpt = raw[:200].decode("utf-8", "replace")
            raise ProviderError(
                f"dns: Cloudflare {method} {path} -> HTTP {status} with a non-JSON body: {excerpt!r}",
                status=status,
            ) from None
        if not isinstance(payload, dict):
            raise ProviderError(
                f"dns: Cloudflare {method} {path} -> HTTP {status} with a non-object body",
                status=status,
            )
        if status >= 400 or not payload.get("success"):
            message = _api_message(payload)
            raise ProviderError(
                f"dns: Cloudflare {method} {path} -> HTTP {status}: {message}", status=status
            )
        return payload.get("result")


def _api_message(payload: dict) -> str:
    errors = payload.get("errors")
    if isinstance(errors, list):
        messages = [
            str(error.get("message") or error.get("code"))
            for error in errors
            if isinstance(error, dict)
        ]
        if messages:
            return "; ".join(messages)
    return str(payload.get("message") or "no message")


# ------------------------------------------------------------------- token loading


def load_token(ctx: Context, args) -> str:
    """The token from an explicit file, the config, or the default 0600 file."""
    explicit = getattr(args, "token_file", None)
    if explicit:
        return _read_token_file(Path(explicit).expanduser())
    configured = ctx.config.get("dns_token")
    if configured:
        return configured.strip()
    path = ctx.config.path_value("dns_token_file", DEFAULT_TOKEN_FILE) or DEFAULT_TOKEN_FILE
    return _read_token_file(path)


def _read_token_file(path: Path) -> str:
    try:
        info = path.stat()
    except FileNotFoundError:
        raise ConfigError(
            f"dns: no token file at {path}; set dns_token in the config or pass --token-file"
        ) from None
    except OSError as error:
        raise ConfigError(f"dns: cannot read {path}: {error.strerror}") from None
    if not path.is_file():
        raise ConfigError(f"dns: {path} is not a regular file")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise ConfigError(f"dns: {path} is mode {mode:04o}; a token file must be 0600")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ConfigError(f"dns: cannot read {path}: {error.strerror}") from None
    if not token:
        raise ConfigError(f"dns: {path} is empty")
    return token


# ---------------------------------------------------------------------- the command


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "verb",
        choices=VERBS,
        help="create, settle or delete the A record",
    )
    parser.add_argument("slug", help="the tenant slug (or the reserved test label)")
    parser.add_argument("--ip", metavar="ADDR", help="the machine's IPv4 address (create only)")
    parser.add_argument(
        "--ttl",
        type=int,
        metavar="SECONDS",
        help=f"record TTL for create (default: {PROVISION_TTL}; settle uses {SETTLED_TTL})",
    )
    parser.add_argument("--zone", metavar="NAME", help=f"DNS zone (default: {DEFAULT_ZONE})")
    parser.add_argument(
        "--zone-id",
        metavar="ID",
        help="the Cloudflare zone id; skips the lookup by name",
    )
    parser.add_argument(
        "--token-file",
        metavar="PATH",
        help=f"token file at mode 0600 (default: {DEFAULT_TOKEN_FILE})",
    )


def build_parser() -> argparse.ArgumentParser:
    """The standalone parser; `cli.py` builds its own and calls `add_arguments`."""
    parser = argparse.ArgumentParser(
        prog="odilectl dns",
        description=f"{HELP}\n\nExample: {EXAMPLE}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", metavar="PATH", default=None, help="config file, mode 0600")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--apply", action="store_true", help="execute the change")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    add_arguments(parser)
    return parser


def _validate_flags(verb: str, args) -> None:
    if verb == "create":
        if not getattr(args, "ip", None):
            raise UsageError("dns create: --ip ADDR is required")
    else:
        if getattr(args, "ip", None):
            raise UsageError(f"dns {verb}: --ip is only valid with create")
        if getattr(args, "ttl", None) is not None:
            raise UsageError(f"dns {verb}: --ttl is only valid with create")


def _build_client(ctx: Context, args, zone: str, transport: Transport | None) -> CloudflareDNS:
    zone_id = getattr(args, "zone_id", None) or ctx.config.get("dns_zone_id")
    token = load_token(ctx, args)
    return CloudflareDNS(token, zone, zone_id=zone_id, transport=transport)


def run(
    ctx: Context,
    args,
    *,
    transport: Transport | None = None,
) -> Report:
    verb = args.verb
    try:
        zone = validate_zone(
            getattr(args, "zone", None) or ctx.config.get("dns_zone") or DEFAULT_ZONE
        )
        name = record_name(args.slug, zone)
    except ValueError as error:
        raise UsageError(f"dns {verb}: {error}") from None
    _validate_flags(verb, args)

    client = _build_client(ctx, args, zone, transport)
    if verb == "create":
        try:
            ipv4 = validate_ipv4(args.ip)
            ttl = validate_ttl(args.ttl if args.ttl is not None else PROVISION_TTL)
        except ValueError as error:
            raise UsageError(f"dns create: {error}") from None
        return _create_report(ctx, client, name, zone, ipv4, ttl)
    if verb == "settle":
        return _settle_report(ctx, client, name, zone)
    return _delete_report(ctx, client, name, zone)


def _create_report(
    ctx: Context, client: CloudflareDNS, name: str, zone: str, ipv4: str, ttl: int
) -> Report:
    change = client.ensure(name, ipv4, ttl, apply=ctx.mutate)
    fields = {
        "zone": zone,
        "type": "A",
        "content": ipv4,
        "proxied": False,
        "ttl": ttl,
        "action": change.action,
        "record_id": change.record.id if change.record else None,
    }
    if not ctx.mutate:
        actions = [
            {
                "action": f"dns.{change.action}",
                "zone": zone,
                "name": name,
                "type": "A",
                "content": ipv4,
                "proxied": False,
                "ttl": ttl,
            }
        ]
        notes = [
            "DNS-only: proxied stays false, so Cloudflare answers the name and "
            "never carries the tenant's bytes",
            f"provisioning uses TTL {PROVISION_TTL}s; run `odilectl dns settle` to "
            f"move it to {SETTLED_TTL}s",
        ]
        return _named(plan(NAME, ctx, actions, notes, **fields), name)
    return _named(
        applied(
            NAME,
            ctx,
            [
                f"  name:    {name}",
                "  type:    A",
                f"  content: {ipv4}",
                "  proxied: false (DNS-only)",
                f"  ttl:     {ttl}",
                f"  record:  {fields['record_id'] or '-'} ({change.action})",
            ],
            **fields,
        ),
        name,
    )


def _settle_report(ctx: Context, client: CloudflareDNS, name: str, zone: str) -> Report:
    change = client.settle(name, apply=ctx.mutate)
    actual_ttl = change.record.ttl if change.record else SETTLED_TTL
    fields = {
        "zone": zone,
        "type": "A",
        "content": change.record.content if change.record else None,
        "proxied": False,
        "ttl": actual_ttl,
        "action": change.action,
        "record_id": change.record.id if change.record else None,
    }
    if not ctx.mutate:
        actions = [
            {
                "action": f"dns.{change.action}",
                "zone": zone,
                "name": name,
                "type": "A",
                "ttl": SETTLED_TTL,
            }
        ]
        notes = [f"settle moves the TTL from {PROVISION_TTL}s to {SETTLED_TTL}s"]
        return _named(plan(NAME, ctx, actions, notes, **fields), name)
    return _named(
        applied(
            NAME,
            ctx,
            [
                f"  name:    {name}",
                f"  ttl:     {actual_ttl}",
                f"  record:  {fields['record_id'] or '-'} ({change.action})",
            ],
            **fields,
        ),
        name,
    )


def _delete_report(ctx: Context, client: CloudflareDNS, name: str, zone: str) -> Report:
    change = client.remove(name, apply=ctx.mutate)
    fields = {
        "zone": zone,
        "type": "A",
        "action": change.action,
        "record_id": change.record.id if change.record else None,
    }
    if not ctx.mutate:
        actions = [
            {
                "action": f"dns.{change.action}",
                "zone": zone,
                "name": name,
                "type": "A",
                "record_id": fields["record_id"],
            }
        ]
        return _named(
            plan(NAME, ctx, actions, ["a second delete is a no-op: absent is success"], **fields),
            name,
        )
    if change.action == "deleted":
        line = f"  {name}: removed (record {change.record.id if change.record else '-'})"
    else:
        line = f"  {name}: already absent (no A record in {zone})"
    return _named(applied(NAME, ctx, [line], **fields), name)


def _named(report: Report, name: str) -> Report:
    """Put the record name in the payload; `plan()`/`applied()` own `name`."""
    report.payload["name"] = name
    return report


# ------------------------------------------------------------------------ entry point


def _load_config(path: str | None) -> Config:
    """Load ``--config``/``$ODILECTL_CONFIG``, or defaults when no file exists.

    On HQ the DNS token usually lives in its own 0600 file and there may be no
    odilectl config yet; a missing default config must not block a DNS command.
    An explicitly named config is always loaded strictly.
    """
    chosen = path or os.environ.get(ENV_CONFIG_PATH)
    if chosen is None and not Path(DEFAULT_CONFIG_PATH).exists():
        return Config(Path(DEFAULT_CONFIG_PATH), {})
    return Config.load(chosen)


def _emit_error(args, out: TextIO, err: TextIO, error: OdileError) -> None:
    if getattr(args, "json", False):
        payload = {
            "ok": False,
            "command": NAME,
            "dry_run": not (getattr(args, "apply", False) and not getattr(args, "dry_run", False)),
            "error": str(error),
        }
        print(json.dumps(payload, indent=2, sort_keys=True), file=out)
        return
    print(f"odilectl {NAME}: {error}", file=err)


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Transport | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one dns verb and return the process exit code."""
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    try:
        config = _load_config(args.config)
        ctx = Context(
            config=config,
            dry_run=args.dry_run,
            apply=args.apply,
            json=args.json,
            stdout=out,
            stderr=err,
        )
        report = run(ctx, args, transport=transport)
    except OdileError as error:
        _emit_error(args, out, err, error)
        return error.exit_code
    except ValueError as error:
        usage = UsageError(str(error))
        _emit_error(args, out, err, usage)
        return usage.exit_code
    except KeyboardInterrupt:
        print("odilectl dns: interrupted", file=err)
        return 130
    if args.json:
        print(json.dumps(report.payload, indent=2, sort_keys=True), file=out)
    else:
        print("\n".join(report.lines), file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
