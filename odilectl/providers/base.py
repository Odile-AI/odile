"""The provider interface: the only vocabulary a command needs.

A command asks for a machine; it never asks the vendor. The five operations
are the whole lifecycle V0.1 needs:

    create(spec)      order a machine
    poll(server_id)   wait until it is active and addressable
    power(...)        start, stop, restart or cut the power
    terminate(...)    destroy it (a second call is a no-op)
    list()            every machine on the account

The provider package has one implementation. If a second provider ever
arrives, the commands do not change; `CreateSpec` and `Machine` are the
contract.

Statuses are normalised to six words so the state machine does not inherit a
vendor's spelling: pending, active, suspended, failed, terminated, unknown. The
provider's own word is kept in `Machine.raw_status` for a human.
"""

from __future__ import annotations

import abc
import base64
import binascii
import re
import struct
from dataclasses import dataclass, field
from typing import Any, ClassVar

POWER_ACTIONS = ("start", "stop", "restart", "forcestop")
CANONICAL_STATES = ("pending", "active", "suspended", "failed", "terminated", "unknown")

_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{1,30}[A-Za-z0-9]$")
_KEY_TYPES = re.compile(
    r"^(?:ssh-(?:ed25519|rsa)|ecdsa-sha2-nistp(?:256|384|521)|"
    r"sk-(?:ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com)$"
)

# The public-key field's byte length for the types that have a fixed one.
# ssh-rsa is variable (e and n), so its shape is checked but not its length.
_FIXED_KEY_BYTES = {
    "ssh-ed25519": 32,
    "sk-ssh-ed25519@openssh.com": 32,
    "ecdsa-sha2-nistp256": 65,
    "ecdsa-sha2-nistp384": 97,
    "ecdsa-sha2-nistp521": 133,
    "sk-ecdsa-sha2-nistp256@openssh.com": 65,
}


@dataclass(frozen=True)
class CreateSpec:
    """Everything a provider needs to order the tenant's machine.

    The defaults are the V0.1 machine: Debian 13, 31 days, SEV-SNP, LUKS and
    measured boot, with exactly the tenant's public key. `ssh_key` is one
    public key, one line — measured boot takes exactly one.
    """

    hostname: str
    plan_id: int
    ssh_key: str
    os: str = "debian13"
    days: int = 31
    snp: bool = True
    fde: bool = True
    measured: bool = True
    preorder: bool = False

    def __post_init__(self) -> None:
        if not _HOSTNAME_RE.match(self.hostname):
            raise ValueError(
                "hostname must be 3-32 letters, digits or hyphens, "
                "not starting or ending with a hyphen"
            )
        if not isinstance(self.plan_id, int) or self.plan_id <= 0:
            raise ValueError("plan_id must be a positive integer")
        if not isinstance(self.days, int) or not 7 <= self.days <= 365:
            raise ValueError("days must be between 7 and 365")
        object.__setattr__(self, "ssh_key", validate_ssh_public_key(self.ssh_key))


def validate_ssh_public_key(text: str) -> str:
    """Return the key as one clean line, or raise `ValueError`.

    This mirrors the refusal the vendor applies before it charges: one key, no
    password pasted into the field, no truncated base64. It is a shape check,
    not a signature check — the private key is the tenant's and never here.
    """
    lines = text.strip().splitlines()
    if len(lines) != 1:
        raise ValueError("ssh_key must be exactly one public key on one line")
    fields = lines[0].split()
    if len(fields) < 2:
        raise ValueError("ssh_key must look like 'ssh-ed25519 AAAA... comment'")
    key_type, encoded = fields[0], fields[1]
    if not _KEY_TYPES.match(key_type):
        raise ValueError(f"unsupported SSH key type {key_type!r}")
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("ssh_key is not valid base64") from None
    declared, offset = _read_field(blob, 0)
    if declared != key_type.encode("ascii"):
        raise ValueError("ssh_key blob does not match its declared type")
    blob_fields: list[bytes] = []
    while offset < len(blob):
        value, offset = _read_field(blob, offset)
        blob_fields.append(value)
    if not blob_fields or not any(blob_fields):
        raise ValueError("ssh_key has no key material")
    expected = _FIXED_KEY_BYTES.get(key_type)
    if expected is not None:
        # A security-key blob ends with its application string; the public key
        # is the field before it.
        index = -2 if key_type.startswith("sk-") and len(blob_fields) >= 2 else -1
        if len(blob_fields[index]) != expected:
            raise ValueError(
                f"{key_type} public key must be {expected} bytes, "
                f"got {len(blob_fields[index])}"
            )
    elif key_type == "ssh-rsa" and len(blob_fields) < 2:
        raise ValueError("ssh-rsa key has no modulus")
    return lines[0].strip()


def _read_field(blob: bytes, offset: int) -> tuple[bytes, int]:
    """Read one SSH wire-format string; a cut-off field is a truncated key."""
    if offset + 4 > len(blob):
        raise ValueError("ssh_key is truncated")
    (length,) = struct.unpack(">I", blob[offset:offset + 4])
    offset += 4
    if offset + length > len(blob):
        raise ValueError("ssh_key is truncated")
    return blob[offset:offset + length], offset + length


@dataclass(frozen=True)
class Machine:
    """One machine, in the provider-neutral vocabulary."""

    server_id: str
    hostname: str
    state: str
    ipv4: str | None = None
    ipv6: str | None = None
    expires_at: int | None = None
    plan_id: int | None = None
    raw_status: str | None = None
    detail: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.state not in CANONICAL_STATES:
            raise ValueError(f"unknown machine state {self.state!r}")

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape every command emits; `detail` stays provider-specific."""
        data: dict[str, Any] = {
            "server_id": self.server_id,
            "hostname": self.hostname,
            "state": self.state,
            "ipv4": self.ipv4,
            "ipv6": self.ipv6,
            "expires_at": self.expires_at,
            "plan_id": self.plan_id,
            "raw_status": self.raw_status,
        }
        if self.detail:
            data["detail"] = dict(self.detail)
        return data


class Provider(abc.ABC):
    """The five calls a command may make. No SSH, no shell, no guest access."""

    name: ClassVar[str] = "abstract"

    @abc.abstractmethod
    def create(self, spec: CreateSpec) -> Machine:
        """Order a machine and return it as soon as the provider has an id."""

    @abc.abstractmethod
    def poll(self, server_id: str, *, timeout: float = 900.0, interval: float = 10.0) -> Machine:
        """Wait for `active`, or raise `ProviderTimeout`/`ProviderError`."""

    @abc.abstractmethod
    def power(self, server_id: str, action: str) -> Machine:
        """Apply a power action and return the machine as it reads afterwards."""

    @abc.abstractmethod
    def terminate(self, server_id: str) -> bool:
        """Destroy the machine. Return False when it was already gone (404)."""

    @abc.abstractmethod
    def list(self) -> list[Machine]:
        """Every machine on the account, in provider order."""
