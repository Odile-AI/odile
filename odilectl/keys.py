"""`keys` — one NEAR AI inference key per tenant, and the record of it.

Every instance's inference runs on its own key (Savio, 2026-11-13). The key is
minted by the provider's management API when the machine is ordered and written
once into that instance's seed; HQ keeps the key's *label* and *first
characters* — never the raw `sk-…`. `odilectl provision` issues, `odilectl
destroy` revokes, and `odilectl keys <slug>` reads the record back.

Why the shape is what it is (measured by ODL-567, `docs/product/SPIKE-near-key-api.md`
§3, §5.5):

* Management lives on `cloud-platform.near.ai` and takes the account's session
  JWT (`security: [session_token]`), not the inference key. The JWT is short
  lived (~1 h) and is minted from a rotating refresh token; the refresh route is
  the console's own and is bound to the exact `User-Agent` used at sign-in, so
  the pair and the UA live together in one 0600 file at HQ and this module is
  the only reader.
* `POST /v1/workspaces/{id}/api-keys` takes `{"name", "spendLimit": {"amount",
  "currency"}}` in **nano-USD** (`$1.00 = 1000000000`) and returns the full
  `sk-…` once; every later read returns `key: null` and the masked
  `key_prefix`. That `spendLimit` is the cap — there is no proxy, no meter and
  no per-request ledger in the path, and this module keeps none. The check is
  post-charge, so the last admitted request may cross the limit by up to its own
  cost (measured: `2,180` nano-USD over a `$0.05` limit, §6–§7).
* Revoke needs no stored id from the mint response: `GET …/api-keys` lists
  every key with `id`, `name` and `key_prefix`, so destroy resolves the id by
  the label and the first characters HQ already holds and deletes that (`§3.2`,
  `§3.4`).

The two failure classes the tenant sees are the provider's own: at the key's
limit `402 api_key_limit_exceeded` (the body names the spend and the limit), at
zero org credit `402 no_limit_configured`. This module only *reads* those
numbers for the operator (`keys --check`); the sentence the user sees comes from
the provider through the agent.

`near_account` in the odilectl config names the credential file. Without it the
mint is off and `provision` keeps the seed config's shared key — alpha.0's
manual path, and the bench/sim shape. That is one config line, not a default,
so a host with no NEAR credential never mints by accident.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from argparse import ArgumentParser
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping

from .core import Context, Report, result
from .errors import ProviderError, UsageError
from .state import DEFAULT_STATE_DB, StateError, StateStore, TenantKey, UnknownTenant

NAME = "keys"
HELP = "Show a tenant's NEAR AI key record, and with --check what the provider says."
EXAMPLE = "odilectl keys odl-7f3a2c --check"

# Where the provider serves management (`session_token`) and the console's own
# refresh route. Both are config-overridable; they are different hosts on
# purpose (the inference key is rejected on management, §5.1).
PLATFORM_URL = "https://cloud-platform.near.ai"
CONSOLE_URL = "https://cloud.near.ai"

DEFAULT_ACCOUNT = "/root/.odile/near-account.json"
# A2's cost envelope (t_ad1783c4, 2026-11-20): a ten-dollar lifetime inference
# limit per tester. It is the *default* for a key minted without a value on the
# command line; this card does not own the envelope.
DEFAULT_SPEND_LIMIT_USD = 10
NANO_PER_USD = 1_000_000_000
KEY_PREFIX_CHARS = 6
TIMEOUT = 40.0
PAGE = 100
# The provider says a fresh key "may take up to 10 seconds to become active"
# (§6). A seed exchange happens minutes later, so the issue path does not poll;
# `--check` reports `active:false` if it is read that fast.
CREDENTIAL_MODE = 0o600

# The test seam: (method, url, headers, body) -> (status, body bytes). The CLI
# builds a real urllib call; a test substitutes a recorded transport, the same
# way `dns.DNS_TRANSPORT` and `offboard.DNS_TRANSPORT` work.
TRANSPORT: Callable[[str, str, dict, bytes | None], tuple[int, bytes]] | None = None


# ----------------------------------------------------------------- the credential


@dataclass(frozen=True)
class Credential:
    """The account's rotating management credential, and the file it lives in.

    `user_agent` is part of the credential: the refresh route answers
    `401 Failed to refresh token` for a different one (§5.3), so the pair and
    the UA are read and written together.
    """

    path: str
    user_agent: str
    access_token: str
    refresh_token: str
    refreshed_at: str | None = None

    def to_dict(self) -> dict[str, str]:
        return {
            "user_agent": self.user_agent,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "refreshed_at": self.refreshed_at or iso(time.time()),
        }

    def masked(self) -> dict[str, Any]:
        """What a report may print: lengths and the refresh time, no secret."""
        return {
            "file": self.path,
            "user_agent": self.user_agent,
            "access": f"len {len(self.access_token)}",
            "refresh": f"len {len(self.refresh_token)}",
            "refreshed_at": self.refreshed_at,
        }


def iso(epoch: float | int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(epoch)))


def _safe_mode(path: str) -> None:
    """A file with a refresh token is 0600; anything else is a config error.

    The same rule `config.py` and `seed/seed.py` apply, for the same reason: a
    group- or world-readable credential is a leak with a correct-looking path.
    """
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError as error:
        raise ProviderError(f"keys: cannot read the account credential {path}: {error}") from None
    if mode & 0o077:
        raise ProviderError(
            f"keys: the account credential {path} is mode {mode:03o}; chmod 600"
        )


def load_credential(path: str) -> Credential:
    """Read the 0600 credential file; a shape error names the field, not its value."""
    _safe_mode(path)
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        raise ProviderError(
            f"keys: no account credential at {path}; set `near_account` in the config"
        ) from None
    except (OSError, ValueError) as error:
        raise ProviderError(f"keys: cannot read the account credential {path}: {error}") from None
    if not isinstance(document, dict):
        raise ProviderError(f"keys: the account credential {path} is not a JSON object")
    for field in ("user_agent", "access_token", "refresh_token"):
        value = document.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ProviderError(
                f"keys: the account credential {path} has no non-empty {field!r}"
            )
    return Credential(
        path=path,
        user_agent=document["user_agent"].strip(),
        access_token=document["access_token"].strip(),
        refresh_token=document["refresh_token"].strip(),
        refreshed_at=document.get("refreshed_at"),
    )


def save_credential(credential: Credential) -> None:
    """Replace the file atomically at 0600, before the new tokens are used.

    Every refresh rotates the refresh token and invalidates the previous one,
    so a rotated pair that is used but not persisted strands HQ at the next
    call. `os.replace` on a 0600 temp file makes the write all-or-nothing.
    """
    path = credential.path
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".near-account.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(credential.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, CREDENTIAL_MODE)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _http(method: str, url: str, headers: dict, body: bytes | None = None) -> tuple[int, bytes]:
    """One HTTPS call, never raising: a transport failure is status 0."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read(1 << 20)
    except urllib.error.HTTPError as error:
        try:
            return error.code, error.read(1 << 20) if error.fp else b""
        finally:
            error.close()
    except (urllib.error.URLError, OSError, ValueError) as error:
        return 0, f"{type(error).__name__}: {error}".encode("utf-8", "replace")


def _call(method: str, url: str, headers: dict, body: bytes | None = None) -> tuple[int, bytes]:
    transport = TRANSPORT or _http
    return transport(method, url, headers, body)


def refresh(credential: Credential, console_url: str) -> Credential:
    """Rotate the pair through the console's route, persisting it first.

    The console's route, with the cookies *and* the exact UA the session was
    created with (§5.3). `Set-Cookie` carries both new values and it is the
    only place they appear; the pair is written to the 0600 file before the new
    access token is used, because every refresh invalidates the previous one.
    The test seam answers the same two names in a JSON body instead, so a
    recorded transport can drive the rotation without a socket.
    """
    if TRANSPORT is not None:
        cookie = "; ".join(
            (
                f"__Host-cloud-access_token={credential.access_token}",
                f"__Host-cloud-refresh_token={credential.refresh_token}",
            )
        )
        status, body = _call(
            "POST",
            console_url.rstrip("/") + "/api/auth/refresh",
            {
                "accept": "application/json",
                "cookie": cookie,
                "user-agent": credential.user_agent,
                "origin": console_url,
                "referer": console_url + "/",
            },
            b"",
        )
        return _rotated(credential, status, _json_body(body))
    request = urllib.request.Request(
        console_url.rstrip("/") + "/api/auth/refresh",
        data=b"",
        method="POST",
        headers={
            "accept": "application/json",
            "cookie": "; ".join(
                (
                    f"__Host-cloud-access_token={credential.access_token}",
                    f"__Host-cloud-refresh_token={credential.refresh_token}",
                )
            ),
            "user-agent": credential.user_agent,
            "origin": console_url,
            "referer": console_url + "/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            status = response.status
            cookies = response.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as error:
        status = error.code
        cookies = error.headers.get_all("Set-Cookie") or []
        error.close()
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ProviderError(f"keys: the refresh route did not answer: {error}") from None
    pairs = {}
    for raw in cookies:
        name, _, rest = raw.partition("=")
        pairs[name.strip()] = rest.split(";")[0]
    return _rotated(credential, status, pairs)


def _rotated(credential: Credential, status: int, pairs: Mapping[str, Any]) -> Credential:
    if status not in (200, 204):
        raise ProviderError(
            "keys: the account credential could not be refreshed "
            f"(HTTP {status}); re-sign-in in the console and rewrite {credential.path}"
        )
    access = str(pairs.get("__Host-cloud-access_token") or "")
    renew = str(pairs.get("__Host-cloud-refresh_token") or "")
    if not access or not renew:
        raise ProviderError(
            "keys: the refresh answered without both __Host-cloud cookies; "
            f"rewrite {credential.path} from a fresh console sign-in"
        )
    rotated = Credential(
        path=credential.path,
        user_agent=credential.user_agent,
        access_token=access,
        refresh_token=renew,
        refreshed_at=iso(time.time()),
    )
    save_credential(rotated)
    return rotated


def _json_body(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, ValueError):
        return None


# ------------------------------------------------------------------- the account


@dataclass(frozen=True)
class Account:
    """One management account: where to call, which workspace, what cap to set."""

    credential_path: str
    workspace_id: str
    org_id: str | None = None
    platform_url: str = PLATFORM_URL
    console_url: str = CONSOLE_URL
    spend_limit_nano: int = DEFAULT_SPEND_LIMIT_USD * NANO_PER_USD

    @property
    def credential(self) -> Credential:
        return load_credential(self.credential_path)

    def to_dict(self) -> dict[str, Any]:
        """No secret: the workspace, the cap and the credential's path."""
        return {
            "workspace_id": self.workspace_id,
            "org_id": self.org_id,
            "platform_url": self.platform_url,
            "spend_limit_nano": self.spend_limit_nano,
            "spend_limit_display": money(self.spend_limit_nano),
            "credential": self.credential_path,
        }


def account_from_config(config) -> Account | None:
    """The account the config names, or None when this host mints no keys.

    `near_account` is the switch, and it is deliberately not defaulted: the
    credential file *is* the authority to mint keys for the org, so a host that
    has not been given one must not mint. `near_spend_limit_usd` carries A2's
    envelope; it defaults to the envelope only because the card that fixes it
    (`t_ad1783c4`) is behind this one.
    """
    path = config.get("near_account")
    if not path:
        return None
    workspace = config.get("near_workspace_id")
    if not workspace:
        raise UsageError(
            "keys: `near_account` is set but `near_workspace_id` is not; "
            "the mint call needs the workspace id"
        )
    raw = config.get("near_spend_limit_usd")
    try:
        amount = float(raw) if raw not in (None, "") else float(DEFAULT_SPEND_LIMIT_USD)
    except (TypeError, ValueError):
        raise UsageError(f"keys: near_spend_limit_usd {raw!r} is not a number") from None
    if amount <= 0:
        raise UsageError("keys: near_spend_limit_usd must be greater than zero")
    return Account(
        credential_path=str(path),
        workspace_id=str(workspace),
        org_id=config.get("near_org_id") or None,
        platform_url=config.get("near_platform_url") or PLATFORM_URL,
        console_url=config.get("near_console_url") or CONSOLE_URL,
        spend_limit_nano=int(round(amount * NANO_PER_USD)),
    )


def money(nano: int | None) -> str:
    """Nano-USD as the provider prints it (`$0.05`), for a report line."""
    if nano is None:
        return "-"
    return f"${nano / NANO_PER_USD:.8f}".rstrip("0").rstrip(".") or "$0"


# ----------------------------------------------------------------- the API calls


def api(account: Account, method: str, path: str, body: dict | None = None,
        credential: Credential | None = None) -> tuple[int, Any]:
    """One management call with the session JWT; a 401 refreshes once and retries.

    The JWT is short lived and every `provision`/`destroy` may be the first
    call in an hour, so the refresh is on the request path, not on a timer.
    """
    current = credential or account.credential
    headers = {
        "accept": "application/json",
        "authorization": "Bearer " + current.access_token,
        "user-agent": "odilectl/keys",
    }
    data = None
    if body is not None:
        headers["content-type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    url = account.platform_url.rstrip("/") + path
    status, raw = _call(method, url, headers, data)
    if status == 401:
        current = refresh(current, account.console_url)
        headers["authorization"] = "Bearer " + current.access_token
        status, raw = _call(method, url, headers, data)
    return status, _json_body(raw)


def _provider_error(what: str, status: int, body: Any) -> ProviderError:
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("message") or body.get("error") or body)[:300]
    elif isinstance(body, str):
        detail = body[:300]
    return ProviderError(f"keys: {what} answered HTTP {status}{(': ' + detail) if detail else ''}")


def list_keys(account: Account) -> list[dict]:
    """Every key in the workspace, as the provider lists it (`key: null`)."""
    status, body = api(
        account,
        "GET",
        f"/v1/workspaces/{account.workspace_id}/api-keys?limit={PAGE}",
    )
    if status != 200 or not isinstance(body, dict):
        raise _provider_error("the key list", status, body)
    keys = body.get("api_keys")
    if not isinstance(keys, list):
        raise ProviderError("keys: the key list carried no `api_keys` array")
    return [key for key in keys if isinstance(key, dict)]


def mint(account: Account, label: str) -> dict:
    """Create one key with the per-key `spendLimit`; the raw value is returned once."""
    status, body = api(
        account,
        "POST",
        f"/v1/workspaces/{account.workspace_id}/api-keys",
        {"name": label, "spendLimit": {"amount": account.spend_limit_nano, "currency": "USD"}},
    )
    if status not in (200, 201) or not isinstance(body, dict):
        raise _provider_error(f"the mint of {label}", status, body)
    if not isinstance(body.get("key"), str) or not body["key"].strip():
        raise ProviderError(
            f"keys: the mint of {label} returned no raw key; "
            "the API returns it once, so revoke this key and mint again"
        )
    return body


def revoke(account: Account, key_id: str) -> int:
    """Delete one key by id; 204 is the provider's answer and 404 is already gone."""
    status, body = api(
        account, "DELETE", f"/v1/workspaces/{account.workspace_id}/api-keys/{key_id}"
    )
    if status not in (200, 204, 404):
        raise _provider_error(f"the revoke of {key_id}", status, body)
    return status


def balance(account: Account) -> dict | None:
    """The org's prepaid ledger: `remaining`/`spend_limit`, when the org has them.

    Both fields are **omitted** while the org has no credit (§3.3), which is
    exactly the `no_limit_configured` case; a caller must read absence as "no
    funded limit", not as an error.
    """
    if not account.org_id:
        return None
    status, body = api(account, "GET", f"/v1/organizations/{account.org_id}/usage/balance")
    if status != 200 or not isinstance(body, dict):
        raise _provider_error("the org balance", status, body)
    return body


# ----------------------------------------------------------------- the lifecycle


def label_for(slug: str) -> str:
    """The key's label: the slug, once. It is what `destroy` looks for.

    No random suffix: the label must be derivable at destroy time from what HQ
    holds, and a slug is unique for the life of the tenant (ADR-007). A name
    collision the provider might answer 409 for is handled by revoking the
    label's live keys first, which is also what makes a re-run idempotent.
    """
    return f"odile-{slug}"


def prefix_of(key: str) -> str:
    """The first characters of a raw key — our own spelling, never the key."""
    return key[:KEY_PREFIX_CHARS]


@dataclass(frozen=True)
class Issued:
    """What the issue path learned: enough to record, never the raw key."""

    label: str
    key_id: str
    key_prefix: str
    workspace_id: str
    instance_id: str | None
    spend_limit_nano: int | None
    raw_key: str


def issue(account: Account, slug: str, *, instance_id: str | None = None,
          retries: int = 0) -> Issued:
    """Mint this instance's key, revoking any earlier key with the same label.

    A re-run is the rebuild path, and a stranded key must not draw on the pool
    beside the new one: every live key whose name is this label is revoked
    first. Revoke-then-mint is also the name-collision answer (§6), so the call
    does not need a random suffix.

    `retries` re-mints after a provider-side 5xx, which is a request the
    provider did not complete; it is off by default so a caller can see the
    failure and the plan.
    """
    label = label_for(slug)
    for stale in list_keys(account):
        if stale.get("name") == label and stale.get("is_active", True):
            revoke(account, str(stale.get("id")))
    attempt = 0
    while True:
        try:
            body = mint(account, label)
            break
        except ProviderError:
            attempt += 1
            if attempt > retries:
                raise
            time.sleep(2.0)
    raw_key = str(body["key"]).strip()
    limit = body.get("spend_limit")
    return Issued(
        label=str(body.get("name") or label),
        key_id=str(body.get("id") or ""),
        key_prefix=str(body.get("key_prefix") or prefix_of(raw_key) + "****"),
        workspace_id=str(body.get("workspace_id") or account.workspace_id),
        instance_id=instance_id,
        spend_limit_nano=(
            int(limit["amount"]) if isinstance(limit, dict) and "amount" in limit
            else account.spend_limit_nano
        ),
        raw_key=raw_key,
    )


@dataclass(frozen=True)
class Revoked:
    """What the revoke path found: the record it started from, and the id it used."""

    label: str
    key_prefix: str
    key_id: str | None
    resolved: str  # "record" (the id in the row) or "list" (label + prefix)
    status: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "key_prefix": self.key_prefix,
            "key_id": self.key_id,
            "resolved": self.resolved,
            "status": self.status,
        }


def revoke_for(account: Account, store: StateStore, slug: str) -> Revoked | None:
    """Resolve the provider's key id from the list call and delete it.

    The record holds the label and the first characters; the list call holds
    the ids. The id is resolved by name **and** `key_prefix`, so a label that
    was somehow reused for another key is not revoked by mistake. A key the
    provider no longer lists (already revoked, or wiped in the console) is
    recorded as revoked without a call — destroy converges.
    """
    try:
        record = store.key_for(slug)
    except StateError:
        record = None
    if record is None:
        return None
    match = None
    try:
        for candidate in list_keys(account):
            if (
                candidate.get("name") == record.label
                and candidate.get("key_prefix") == record.key_prefix
            ):
                match = candidate
                break
    except ProviderError:
        raise
    if match is not None:
        status = revoke(account, str(match.get("id")))
        key_id, resolved = str(match.get("id")), "list"
    else:
        status, key_id, resolved = 404, record.key_id, "record"
    store.revoke_key(record.key_id, reason="destroy" if resolved == "list" else "not listed")
    return Revoked(
        label=record.label,
        key_prefix=record.key_prefix,
        key_id=key_id,
        resolved=resolved,
        status=status,
    )


def allowance(account: Account, key: dict | None) -> dict[str, Any]:
    """One plain sentence for the allowance, from the provider's own numbers.

    `at_limit` is the key's `spendLimit` reached (the post-charge check may
    leave usage a hair over it); `no_credit` is an org whose balance carries no
    `remaining` — the `402 no_limit_configured` case. Nothing is stored: this
    is a read of the provider's ledger at the moment of the call.
    """
    limit = key.get("spend_limit") if isinstance(key, dict) else None
    usage = key.get("usage") if isinstance(key, dict) else None
    spent = int(usage["amount"]) if isinstance(usage, dict) and "amount" in usage else None
    cap = int(limit["amount"]) if isinstance(limit, dict) and "amount" in limit else None
    ledger = balance(account) if account.org_id else None
    remaining = None
    if isinstance(ledger, dict):
        # The call answered and the org has a funded limit: `remaining` is a
        # number. When it is **omitted** the org holds no credit at all
        # (§3.3) — that is `402 no_limit_configured`, not "unknown".
        remaining = int(ledger["remaining"]) if ledger.get("remaining") is not None else 0
    if not isinstance(key, dict):
        state = "unlisted"
        sentence = (
            "the provider does not list this key: it was revoked, or it was "
            "never minted for this host"
        )
    elif cap is not None and spent is not None and spent >= cap:
        state = "at_limit"
        sentence = (
            f"the key's allowance is spent: spent {money(spent)} of {money(cap)} — "
            "the provider refuses with 402 api_key_limit_exceeded"
        )
    elif remaining == 0:
        state = "no_credit"
        sentence = (
            "the account has no credit left: the provider refuses with "
            "402 no_limit_configured; top up the account in the console"
        )
    else:
        state = "ok"
        sentence = (
            f"spent {money(spent)} of {money(cap)}; the account holds "
            f"{money(remaining)} — add credit in the console when it runs out"
        )
    return {
        "state": state,
        "sentence": sentence,
        "spent_nano": spent,
        "spend_limit_nano": cap,
        "remaining_nano": remaining,
    }


# ------------------------------------------------------- the seed handoff file


@lru_cache(maxsize=1)
def _seed_service():
    """Load ``seed/seed.py`` by path, the way ``odilectl.provision`` does.

    A package import cannot work: the service runs as ``python3 seed/seed.py``,
    so ``seed`` resolves to the script itself. This module is the second writer
    of the handoff file (destroy removes the entry it would otherwise leave
    behind), and the reader/writer of its shape lives there.
    """
    path = Path(__file__).resolve().parent.parent / "seed" / "seed.py"
    spec = importlib.util.spec_from_file_location("odile_seed_service_keys", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the seed service at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def waiting_key_path(config) -> str:
    """The handoff file: the config's `seed_keys`, else the service's default."""
    configured = config.path_value("seed_keys") if config is not None else None
    seed = _seed_service()
    return str(configured) if configured is not None else seed.keys_path()


def drop_waiting_key(config, slug: str) -> bool:
    """Remove a tenant's waiting key after its machine is gone.

    `destroy` revokes the provider key; this removes the copy at HQ so no raw
    `sk-…` outlives the instance it belonged to. Returns whether an entry was
    there — false is the converged, already-destroyed case.
    """
    seed = _seed_service()
    path = waiting_key_path(config)
    document = seed.load_keys(path)
    if slug not in document:
        return False
    seed.set_issued_key(path, slug, None)
    return True


# ------------------------------------------------------------------ the command


def add_arguments(parser: ArgumentParser) -> None:
    parser.add_argument("slug", help="the tenant's slug")
    parser.add_argument(
        "--check",
        action="store_true",
        help="also read the provider: the key's usage, its cap and the org balance",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help=f"tenant state database (default: the config's state_db, {DEFAULT_STATE_DB})",
    )


def _store(ctx: Context, args) -> StateStore:
    path = getattr(args, "db", None)
    chosen = path or ctx.config.path_value("state_db", DEFAULT_STATE_DB) or DEFAULT_STATE_DB
    try:
        return StateStore.open(chosen)
    except StateError as error:
        raise ProviderError(f"keys: cannot open the state database: {error}") from None


def _key_lines(record: TenantKey | None, history: list[TenantKey]) -> list[str]:
    if record is None:
        return ["  record:    no key has been issued for this tenant"]
    lines = [
        f"  label:     {record.label}",
        f"  prefix:    {record.key_prefix} (the first characters; the raw key is not at HQ)",
        f"  key id:    {record.key_id}",
        f"  state:     {record.state}"
        + (f" ({record.revoked_at})" if record.revoked_at else ""),
        f"  instance:  {record.instance_id or '-'}",
        f"  cap:       {money(record.spend_limit_nano)} per key (the provider's spendLimit)",
        f"  issued:    {record.issued_at}",
    ]
    if len(history) > 1:
        older = ", ".join(f"{row.key_prefix} {row.state}" for row in history[:-1])
        lines.append(f"  earlier:   {older}")
    return lines


def run(ctx: Context, args) -> Report:
    account = account_from_config(ctx.config)
    store = _store(ctx, args)
    try:
        try:
            tenant = store.get(args.slug)
        except UnknownTenant:
            tenant = None
        history = store.keys(args.slug)
    finally:
        store.close()
    record = history[-1] if history else None
    lines = [f"keys: {args.slug}" + (f" ({tenant.state})" if tenant else " (no tenant row)")]
    lines += _key_lines(record, list(history))
    payload: dict[str, Any] = {
        "slug": args.slug,
        "tenant_state": tenant.state if tenant else None,
        "record": record.to_dict() if record else None,
        "history": [row.to_dict() for row in history],
        "account": account.to_dict() if account else None,
    }
    notes: list[str] = []
    if account is None:
        notes.append(
            "no `near_account` in the config: this host mints no keys, and "
            "`--check` has no management credential to read the provider with"
        )
    if args.check:
        if account is None:
            raise UsageError(
                "keys: --check needs `near_account` and `near_workspace_id` in the config"
            )
        keys = list_keys(account)
        listed = next(
            (key for key in keys if key.get("key_prefix") == (record.key_prefix if record else None)),
            None,
        )
        reading = allowance(account, listed)
        payload["provider"] = {
            "listed": listed is not None,
            "key": {
                "id": listed.get("id"),
                "name": listed.get("name"),
                "key_prefix": listed.get("key_prefix"),
                "is_active": listed.get("is_active"),
                "last_used_at": listed.get("last_used_at"),
            } if listed else None,
            "allowance": reading,
        }
        lines.append(f"  provider:  {'listed' if listed else 'not listed'}")
        if listed:
            lines.append(
                f"  active:    {listed.get('is_active')} (last used {listed.get('last_used_at') or 'never'})"
            )
        lines.append(f"  allowance: {reading['state']} — {reading['sentence']}")
    return result(NAME, ctx, lines + [f"note: {note}" for note in notes], **payload)
