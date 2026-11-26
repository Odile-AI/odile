"""Health, heartbeats and the down alert (ODL-216, re-cut ODL-467).

The tenant posts a signed heartbeat; HQ polls each tenant's `/health`; and when
the heartbeat stops, HQ reads port 22's host key.  Under the sealed boot that
key no longer changes the verdict: the machine comes back by itself, so a
stopped heartbeat is `down`.  The host key is a line of the alert's symptom
(does port 22 answer, and with which key), never a classification, and no owner
email is sent.

Three pieces live here so one test can hold the whole chain:

* `HeartbeatStore` — the signed beats, the public key registered on the first
  beat, and an alert ledger keyed by (slug, kind) so a live condition is one
  row, not one row every five minutes.
* the SSH host-key probe — the smallest legal key exchange that returns the
  server's host key blob.  Pure standard library: no external `ssh-keyscan`
  process, no session, no authentication.  It reads the key; it does not verify
  the exchange signature or derive a shared secret, because the caller's
  question is identity, not secrecy.
* the poll (`check`) — heartbeat age, brief deadline, disk, spend, backup, the
  host-key symptom, and the alert deliveries.

`odilectl health --apply` is the five-minute job.  `scripts/heartbeat.py`
is the receiver and the guest's collector; it owns the `openssl` calls that
verify and create Ed25519 signatures, so the CLI package never spawns a
process.

The module carries no public sentence; the runbook is
`docs/runbooks/alerting.md`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import smtplib
import socket
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from argparse import ArgumentParser
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import backup_usage, state
from .config import Config
from .core import Context, Report
from .errors import ConfigError, OdileError, UsageError
from .state import StateStore, Tenant


# Message numbers we care about (RFC 4253 section 12, RFC 4251 section 5.6).
SSH_MSG_DISCONNECT = 1
SSH_MSG_IGNORE = 2
SSH_MSG_UNIMPLEMENTED = 3
SSH_MSG_DEBUG = 4
SSH_MSG_KEXINIT = 20
SSH_MSG_NEWKEYS = 21
SSH_MSG_KEXDH_INIT = 30
SSH_MSG_KEXDH_REPLY = 31

# RFC 3526 section 3: the 2048-bit MODP group.  Written as one hex string so
# it can be compared against the RFC by eye, then split for readability.
GROUP14_PRIME_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF"
)
GROUP14_PRIME = int(GROUP14_PRIME_HEX, 16)
GROUP14_GENERATOR = 2

KEX_GROUP14 = "diffie-hellman-group14-sha256"
KEX_CURVE25519 = "curve25519-sha256"
# Preference order on the wire: group14 first, X25519 when the server has no
# finite-field DH.  See the module docstring for why the fallback is here.
DEFAULT_KEX_ALGORITHMS = (KEX_GROUP14, KEX_CURVE25519)

# X25519 field arithmetic (RFC 7748 section 5).
_P25519 = 2**255 - 19
_A24 = 121665
_BASE_POINT = (9).to_bytes(32, "little")

DEFAULT_KEY_ALGORITHMS = (
    "ssh-ed25519",
    "rsa-sha2-512",
    "ecdsa-sha2-nistp256",
)

CLIENT_BANNER = b"SSH-2.0-OdileProbe_1.0"
MAX_BANNER_BYTES = 8192
MAX_PACKET_BYTES = 256 * 1024
MAX_MESSAGES_BEFORE_REPLY = 32
MAX_PREAMBLE_LINES = 50

# The algorithms we advertise for the parts of the handshake that never get
# used.  They only have to be things every SSH-2 server accepts; the exchange
# stops before NEWKEYS, so no cipher is ever keyed.
_CIPHER = "aes128-ctr"
_MAC = "hmac-sha2-256"
_COMPRESSION = "none"


class ProbeError(Exception):
    """One probe attempt failed.

    ``connected`` records whether the TCP connection came up, so the caller can
    tell "nothing is listening" apart from "it answered, then said no".
    """

    def __init__(self, message: str, *, connected: bool = False) -> None:
        super().__init__(message)
        self.connected = connected


@dataclass(frozen=True)
class HostKey:
    """One host key as port 22 presented it."""

    key_type: str
    blob: str
    fingerprint: str

    def to_dict(self) -> dict[str, str]:
        return {
            "key_type": self.key_type,
            "blob": self.blob,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class HostKeyObservation:
    """What one host answered on port 22: reachable, and which keys."""

    reachable: bool
    keys: tuple[HostKey, ...]
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "keys": [key.to_dict() for key in self.keys],
            "error": self.error,
        }


# --------------------------------------------------------------------------
# Wire encoding (RFC 4251 section 5)
# --------------------------------------------------------------------------


def _uint32(value: int) -> bytes:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"uint32 out of range: {value}")
    return value.to_bytes(4, "big")


def _string(value: bytes) -> bytes:
    return _uint32(len(value)) + value


def _mpint(value: int) -> bytes:
    """Two's-complement big-endian mpint, with a leading zero when needed."""
    if value == 0:
        return _uint32(0)
    if value < 0:
        raise ValueError("mpint must be non-negative here")
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return _string(raw)


class _Reader:
    """Typed reads over a ``take(n)`` callable (a socket or a bytes buffer)."""

    def __init__(self, take: Callable[[int], bytes]) -> None:
        self._take = take

    def take(self, length: int) -> bytes:
        return self._take(length)

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "big")

    def string(self) -> bytes:
        return self.take(self.u32())

    def mpint(self) -> int:
        raw = self.string()
        return int.from_bytes(raw, "big", signed=True) if raw else 0

    def name_list(self) -> list[str]:
        raw = self.string()
        return [name.decode("ascii") for name in raw.split(b",") if name]


def _bytes_source(data: bytes) -> Callable[[int], bytes]:
    position = 0

    def take(length: int) -> bytes:
        nonlocal position
        if length < 0 or position + length > len(data):
            raise ProbeError("truncated SSH packet payload")
        chunk = data[position : position + length]
        position += length
        return chunk

    return take


def _socket_source(sock: socket.socket) -> Callable[[int], bytes]:
    buffer = bytearray()

    def take(length: int) -> bytes:
        while len(buffer) < length:
            try:
                chunk = sock.recv(max(4096, length - len(buffer)))
            except TimeoutError:
                raise ProbeError("timed out waiting for the server", connected=True)
            except OSError as exc:
                raise ProbeError(f"socket error: {exc}", connected=True)
            if not chunk:
                raise ProbeError(
                    "server closed the connection before the key exchange finished",
                    connected=True,
                )
            buffer.extend(chunk)
        chunk = bytes(buffer[:length])
        del buffer[:length]
        return chunk

    return take


# --------------------------------------------------------------------------
# Binary packet protocol, unencrypted phase (RFC 4253 section 6)
# --------------------------------------------------------------------------


def _read_packet(reader: _Reader) -> bytes:
    packet_length = int.from_bytes(reader.take(4), "big")
    if not 5 <= packet_length <= MAX_PACKET_BYTES:
        raise ProbeError(f"bad SSH packet length {packet_length}")
    body = reader.take(packet_length)
    padding_length = body[0]
    if padding_length < 4 or padding_length > packet_length - 1:
        raise ProbeError(f"bad SSH padding length {padding_length}")
    payload = body[1 : packet_length - padding_length]
    if not payload:
        raise ProbeError("empty SSH packet payload")
    return payload


def _write_packet(sock: socket.socket, payload: bytes) -> None:
    block = 8
    padding_length = block - (5 + len(payload)) % block
    if padding_length < 4:
        padding_length += block
    packet_length = 1 + len(payload) + padding_length
    packet = bytes([padding_length]) + payload + os.urandom(padding_length)
    try:
        sock.sendall(_uint32(packet_length) + packet)
    except OSError as exc:
        raise ProbeError(f"socket error: {exc}", connected=True)


def _read_line(sock: socket.socket, limit: int = MAX_BANNER_BYTES) -> bytes:
    """Read one CRLF/LF line, byte at a time, so no packet bytes are eaten."""
    line = bytearray()
    while len(line) < limit:
        try:
            char = sock.recv(1)
        except TimeoutError:
            raise ProbeError("timed out waiting for the identification string")
        except OSError as exc:
            raise ProbeError(f"socket error: {exc}")
        if not char:
            raise ProbeError("connection closed before the identification string")
        if char == b"\n":
            return bytes(line).rstrip(b"\r")
        line.extend(char)
    raise ProbeError("identification string is longer than 8 KiB")


def _read_banner(sock: socket.socket) -> bytes:
    """Read the server's identification string, skipping any preamble lines."""
    for _ in range(MAX_PREAMBLE_LINES):
        line = _read_line(sock)
        if not line.startswith(b"SSH-"):
            continue  # RFC 4253 4.2 lets a server send text lines first.
        if line.startswith(b"SSH-2.0-") or line.startswith(b"SSH-1.99-"):
            return line
        raise ProbeError(
            f"server speaks {line.decode('ascii', 'replace')!r}, need SSH-2.0"
        )
    raise ProbeError("no SSH identification string in the first 50 lines")


# --------------------------------------------------------------------------
# Key exchange messages (RFC 4253 sections 7.1 and 8)
# --------------------------------------------------------------------------


def _build_kexinit(
    kex_algorithms: Sequence[str] = ("diffie-hellman-group14-sha256",),
    host_key_algorithms: Sequence[str] = ("ssh-ed25519",),
    first_kex_packet_follows: bool = False,
) -> bytes:
    """A minimal SSH_MSG_KEXINIT payload, with lists we can actually honour."""
    payload = bytearray()
    payload.append(SSH_MSG_KEXINIT)
    payload.extend(os.urandom(16))  # cookie, no meaning to us
    for names in (
        kex_algorithms,
        host_key_algorithms,
        (_CIPHER,),
        (_CIPHER,),
        (_MAC,),
        (_MAC,),
        (_COMPRESSION,),
        (_COMPRESSION,),
        (),
        (),
    ):
        payload.extend(_string(",".join(names).encode("ascii")))
    payload.append(1 if first_kex_packet_follows else 0)
    payload.extend(_uint32(0))  # reserved
    return bytes(payload)


def _parse_kexinit(payload: bytes) -> tuple[dict[str, list[str]], bool]:
    """Return (name-lists by field, first_kex_packet_follows)."""
    if payload[0] != SSH_MSG_KEXINIT:
        raise ProbeError(f"expected KEXINIT, got message {payload[0]}")
    reader = _Reader(_bytes_source(payload[1:]))
    reader.take(16)  # cookie
    fields = (
        "kex",
        "host_key",
        "enc_c2s",
        "enc_s2c",
        "mac_c2s",
        "mac_s2c",
        "comp_c2s",
        "comp_s2c",
        "lang_c2s",
        "lang_s2c",
    )
    parsed = {name: reader.name_list() for name in fields}
    follows = bool(reader.u8())
    reader.u32()  # reserved
    return parsed, follows


def _choose_kex(
    kexinit: dict[str, list[str]], kex_algorithms: Sequence[str]
) -> str:
    """First algorithm in our preference order that the server also offers."""
    for algorithm in kex_algorithms:
        if algorithm in kexinit["kex"]:
            return algorithm
    offered = ", ".join(kexinit["kex"][:4]) or "nothing"
    raise ProbeError(
        "server offers no key exchange we implement "
        f"(ours: {', '.join(kex_algorithms)}; offers: {offered})"
    )


def _check_server_offers_host_key(
    kexinit: dict[str, list[str]], key_algorithm: str
) -> None:
    if key_algorithm not in kexinit["host_key"]:
        offered = ", ".join(kexinit["host_key"][:4]) or "nothing"
        raise ProbeError(
            f"server does not offer host key algorithm {key_algorithm} "
            f"(offers: {offered})"
        )


def _x25519(scalar: bytes, u_coordinate: bytes) -> bytes:
    """X25519(scalar, u): the RFC 7748 section 5 function, Montgomery ladder.

    Readable rather than constant-time: this runs once per probe over a
    throwaway key, and no secret outlives the handshake.
    """
    if len(scalar) != 32 or len(u_coordinate) != 32:
        raise ValueError("X25519 inputs must be 32 bytes each")
    k = bytearray(scalar)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    k_int = int.from_bytes(k, "little")
    x1 = int.from_bytes(u_coordinate, "little") & ((1 << 255) - 1)

    x2, z2, x3, z3 = 1, 0, x1, 1
    swap = 0
    for bit in reversed(range(255)):
        k_bit = (k_int >> bit) & 1
        swap ^= k_bit
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = k_bit
        a = (x2 + z2) % _P25519
        b = (x2 - z2) % _P25519
        aa = (a * a) % _P25519
        bb = (b * b) % _P25519
        e = (aa - bb) % _P25519
        c = (x3 + z3) % _P25519
        d = (x3 - z3) % _P25519
        da = (d * a) % _P25519
        cb = (c * b) % _P25519
        x3 = pow(da + cb, 2, _P25519)
        z3 = (x1 * pow(da - cb, 2, _P25519)) % _P25519
        x2 = (aa * bb) % _P25519
        z2 = (e * (aa + _A24 * e)) % _P25519
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return (x2 * pow(z2, _P25519 - 2, _P25519) % _P25519).to_bytes(32, "little")


def _x25519_public_key() -> tuple[bytes, bytes]:
    """Return (scalar, public) for a fresh X25519 key pair."""
    scalar = bytearray(os.urandom(32))
    scalar[0] &= 248
    scalar[31] &= 127
    scalar[31] |= 64
    scalar = bytes(scalar)
    return scalar, _x25519(scalar, _BASE_POINT)


def _build_kexdh_init() -> bytes:
    """SSH_MSG_KEXDH_INIT: e = g**x mod p, with a fresh 256-bit exponent."""
    exponent = int.from_bytes(os.urandom(32), "big") | 1
    e = pow(GROUP14_GENERATOR, exponent, GROUP14_PRIME)
    return bytes([SSH_MSG_KEXDH_INIT]) + _mpint(e)


def _build_kex_ecdh_init() -> bytes:
    """SSH_MSG_KEX_ECDH_INIT: the client's ephemeral X25519 public key."""
    _, public = _x25519_public_key()
    return bytes([SSH_MSG_KEXDH_INIT]) + _string(public)


def _parse_disconnect(payload: bytes) -> str:
    reader = _Reader(_bytes_source(payload[1:]))
    reason = reader.u32()
    description = reader.string().decode("utf-8", "replace")
    return f"server disconnected ({reason}): {description}"


def _parse_kexdh_reply(payload: bytes) -> bytes:
    """Return K_S from SSH_MSG_KEXDH_REPLY, checking the rest is well formed."""
    if payload[0] != SSH_MSG_KEXDH_REPLY:
        raise ProbeError(f"expected KEXDH_REPLY, got message {payload[0]}")
    reader = _Reader(_bytes_source(payload[1:]))
    host_key = reader.string()
    f = reader.mpint()
    reader.string()  # signature blob: shape only, we never verify it
    if len(host_key) < 4:
        raise ProbeError("host key blob is too short to be an SSH string")
    if not 1 < f < GROUP14_PRIME - 1:
        raise ProbeError("server's DH public value f is out of range")
    return host_key


def _parse_kex_ecdh_reply(payload: bytes) -> bytes:
    """Return K_S from SSH_MSG_KEX_ECDH_REPLY (RFC 5656 section 4)."""
    if payload[0] != SSH_MSG_KEXDH_REPLY:
        raise ProbeError(f"expected KEX_ECDH_REPLY, got message {payload[0]}")
    reader = _Reader(_bytes_source(payload[1:]))
    host_key = reader.string()
    server_public = reader.string()
    reader.string()  # signature blob, unverified as above
    if len(host_key) < 4:
        raise ProbeError("host key blob is too short to be an SSH string")
    if len(server_public) != 32:
        raise ProbeError(f"server's X25519 public key is {len(server_public)} bytes")
    return host_key


def ssh_fingerprint(blob: bytes) -> str:
    """OpenSSH's SHA256 fingerprint: base64 of the digest, no '=' padding."""
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _key_type(blob: bytes) -> str:
    reader = _Reader(_bytes_source(blob))
    try:
        name = reader.string().decode("ascii")
    except (ProbeError, UnicodeDecodeError) as exc:
        raise ProbeError(f"host key blob has no readable type: {exc}")
    if not name or not name.replace("-", "").replace("@", "").isalnum():
        raise ProbeError(f"host key type {name!r} is not a plain algorithm name")
    return name


# --------------------------------------------------------------------------
# One attempt, then all of them
# --------------------------------------------------------------------------


def read_host_key(
    host: str,
    port: int = 22,
    timeout: float = 5.0,
    key_algorithm: str = "ssh-ed25519",
    kex_algorithms: Sequence[str] = DEFAULT_KEX_ALGORITHMS,
) -> dict[str, str]:
    """Fetch one host key.  Returns {'type', 'blob', 'fingerprint'}.

    Raises ProbeError on any failure, with ``connected`` set when TCP came up.
    ``blob`` is the base64 of K_S exactly as `ssh-keyscan` prints it.
    """
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise ProbeError(f"connect failed: {exc}", connected=False)

    try:
        sock.settimeout(timeout)
        try:
            sock.sendall(CLIENT_BANNER + b"\r\n")
        except OSError as exc:
            raise ProbeError(f"socket error: {exc}", connected=True)
        _server_banner = _read_banner(sock)  # validated as SSH-2.0 inside

        reader = _Reader(_socket_source(sock))
        _write_packet(
            sock,
            _build_kexinit(
                kex_algorithms=kex_algorithms,
                host_key_algorithms=(key_algorithm,),
            ),
        )

        while True:
            payload = _read_packet(reader)
            if payload[0] == SSH_MSG_KEXINIT:
                kexinit, server_guessed = _parse_kexinit(payload)
                break
            if payload[0] == SSH_MSG_DISCONNECT:
                raise ProbeError(_parse_disconnect(payload), connected=True)
            # IGNORE, DEBUG, UNIMPLEMENTED: nothing to do, keep reading.

        kex_algorithm = _choose_kex(kexinit, kex_algorithms)
        _check_server_offers_host_key(kexinit, key_algorithm)
        if server_guessed and (
            kexinit["kex"][0] != kex_algorithm
            or kexinit["host_key"][0] != key_algorithm
        ):
            # RFC 4253 7.1: the server guessed wrong, so its next packet is
            # for an algorithm we did not agree on.  Drop it unread.
            _read_packet(reader)
        if kex_algorithm == KEX_GROUP14:
            _write_packet(sock, _build_kexdh_init())
        else:
            _write_packet(sock, _build_kex_ecdh_init())

        for _ in range(MAX_MESSAGES_BEFORE_REPLY):
            payload = _read_packet(reader)
            if payload[0] == SSH_MSG_KEXDH_REPLY:
                if kex_algorithm == KEX_GROUP14:
                    host_key = _parse_kexdh_reply(payload)
                else:
                    host_key = _parse_kex_ecdh_reply(payload)
                break
            if payload[0] == SSH_MSG_DISCONNECT:
                raise ProbeError(_parse_disconnect(payload), connected=True)
            if payload[0] == SSH_MSG_NEWKEYS:
                raise ProbeError(
                    "server sent NEWKEYS before KEXDH_REPLY", connected=True
                )
            # Anything else before the reply is ignorable (RFC 4253 7.1).
        else:
            raise ProbeError("no KEXDH_REPLY from the server", connected=True)

        return {
            "type": _key_type(host_key),
            "blob": base64.b64encode(host_key).decode("ascii"),
            "fingerprint": ssh_fingerprint(host_key),
        }
    except ProbeError as exc:
        # Anything that failed after the socket came up proves reachability,
        # whichever helper raised it.
        exc.connected = True
        raise
    finally:
        try:
            sock.close()
        except OSError:
            pass


def probe_host_keys(
    host: str,
    port: int = 22,
    timeout: float = 5.0,
    key_algorithms: Iterable[str] = DEFAULT_KEY_ALGORITHMS,
) -> HostKeyObservation:
    """Try each host key algorithm on its own connection.

    Returns a `HostKeyObservation`: whether TCP came up, the keys it read, and
    an error string.  Keys are deduplicated by blob, so a server that answers
    rsa-sha2-512 with an ssh-rsa key and ssh-ed25519 with an ed25519 key yields
    two entries.  ``error`` is empty whenever at least one key came back: a
    server that simply does not offer every algorithm is not a failure, it is a
    server with a shorter list.  Never raises: every failure lands in ``error``.
    """
    algorithms = tuple(key_algorithms)
    keys: list[dict[str, str]] = []
    seen: set[str] = set()
    failures: list[str] = []
    reachable = False

    if not algorithms:
        return HostKeyObservation(False, (), "no key algorithms given")

    for algorithm in algorithms:
        try:
            found = read_host_key(host, port, timeout, algorithm)
        except ProbeError as exc:
            reachable = reachable or exc.connected
            failures.append(f"{algorithm}: {exc}")
            continue
        except OSError as exc:  # defensive: nothing should escape read_host_key
            reachable = True
            failures.append(f"{algorithm}: {exc}")
            continue
        reachable = True
        if found["blob"] not in seen:
            seen.add(found["blob"])
            keys.append(found)

    error = "" if keys else "; ".join(failures)
    return HostKeyObservation(
        reachable=reachable,
        keys=tuple(HostKey(key["type"], key["blob"], key["fingerprint"]) for key in keys),
        error=error,
    )


# ---------------------------------------------------------------------------
# The heartbeat store: keys, beats, and the alert ledger
# ---------------------------------------------------------------------------

MAX_HEARTBEAT_BYTES = 64 * 1024
MAX_CLOCK_SKEW_SECONDS = 300
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

# The five conditions the poll can raise.  One word per condition; the alert
# ledger is keyed by (slug, kind) so a condition that keeps being true is one
# row, not one row every five minutes.  `waiting_for_owner` stays in the table's
# CHECK because legacy rows keep their enum value (ODL-467), but the poll never
# raises it again: a stopped heartbeat is `down`.
ALERT_KINDS = (
    "down",
    "brief_missing",
    "disk",
    "spend",
    "backup",
    # The box-level pair (ODL-934): the BX11's own bytes against 800/950 GiB.
    # Two kinds, not one, because the ledger is keyed by (slug, kind): a warn
    # row that stayed open would swallow the promotion to critical and its
    # delivery.
    "backup_usage",
    "backup_usage_critical",
)

KIND_LABELS = {
    "down": "down",
    # Legacy ledger value: the retired initramfs park.  Kept so an old row
    # still renders if one is ever read back; nothing new uses it.
    "waiting_for_owner": "waiting for owner",
    "brief_missing": "missing 07:00 brief",
    "disk": "disk filling",
    "spend": "spend near cap",
    "backup": "backup stale",
    "backup_usage": "backup box filling",
    "backup_usage_critical": "backup box almost full",
}

# A legacy ledger value (ODL-467 retired the initramfs park).  Nothing new
# raises it, but a database cut before that still holds rows with it, and a
# rebuild copies them verbatim — so the CHECK keeps it.
LEGACY_ALERT_KINDS = ("waiting_for_owner",)

# One SQL text for the ledger's kind CHECK, built from ALERT_KINDS: a kind can
# never be listed in Python and missing from the table (or the rebuild below).
_KIND_CHECK = ", ".join(f"'{kind}'" for kind in (*ALERT_KINDS, *LEGACY_ALERT_KINDS))

DEFAULT_HEARTBEAT_DB = state.DEFAULT_STATE_DB
DEFAULT_OUTBOX = "/var/lib/odile/outbox"
DEFAULT_TENANT_DOMAIN = "odile.fyi"
DEFAULT_USER_KEY = "~/.ssh/odile"

# ---------------------------------------------------------------------------
# The upgrade booking (ODL-950, ADR-005 §5.3)
# ---------------------------------------------------------------------------
#
# A machine's click rides the heartbeat it already sends: one signed
# `facts.upgrade` object.  HQ validates the window even though the machine
# computes it — the 15 minutes are the machine's quiesce, the 26 hours are HQ's
# gate — and records exactly one open booking per slug.  The row and the beat
# are written in one transaction; the email is the human's receipt.
UPGRADE_ORIGINS = ("click", "deadline")
UPGRADE_OPEN_STATES = ("booked", "writing", "booted")
UPGRADE_STATES = ("booked", "writing", "booted", "restored", "skipped", "failed")
UPGRADE_MIN_SECONDS = 15 * 60
UPGRADE_MAX_SECONDS = 26 * 60 * 60
UPGRADE_WINDOW_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
UPGRADE_REQUEST_RE = re.compile(r"^[0-9a-f]{32}$")

# The receipt the owner can act on.  The body is the card's verbatim copy in
# `templates/emails/upgrade-booked.txt`; the subject is the card's line.  The
# reply address is the help mailbox, because the one action that can change a
# booking is a reply before its window.
UPGRADE_SUBJECT = "Your upgrade is booked"
UPGRADE_TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "emails" / "upgrade-booked.txt"
UPGRADE_SENDER = "Odile <invite@mail.odile.fyi>"
UPGRADE_REPLY_TO = "help@odile.fyi"

# The document app.py serves at `/release.json` (`RELEASE_DIR`, app.py:83).
# The booking compares the machine's `release` with this file's `release`: a
# well-formed id that is not the served one books nothing and answers `stale`.
# `ODILE_RELEASE_DIR` is the same override app.py honours.
def _default_release_document() -> Path:
    directory = os.environ.get("ODILE_RELEASE_DIR")
    if directory:
        return Path(directory) / "release.json"
    return Path(__file__).resolve().parent.parent / "release" / "release.json"


DEFAULT_RELEASE_DOCUMENT = _default_release_document()
# The template's deadline paragraph; removed whole (with its blank line) when
# the served document carries no `upgrade` block, so the body is the card's
# text without it.
DEADLINE_SENTENCE = (
    "This is a security update: if you do nothing, it is applied after {{DEADLINE}}."
)
DEADLINE_PARAGRAPH = DEADLINE_SENTENCE + "\n\n"

# Thresholds from ODL-216.  `HEARTBEAT_STOP_SECONDS` is imported from the
# state machine so the alert and the transition can never disagree.
DISK_ALERT_PERCENT = 80.0
SPEND_ALERT_RATIO = 0.80
BACKUP_ALERT_SECONDS = 36 * 60 * 60
BRIEF_HOUR_UTC = 7


class HeartbeatError(OdileError):
    """The heartbeat store could not do what it was asked."""


class HeartbeatRejected(HeartbeatError):
    """One heartbeat failed validation; `status` is the HTTP answer to use."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Heartbeat:
    """One accepted, signature-checked heartbeat."""

    id: int
    slug: str
    at: int
    received_at: str
    body: dict[str, Any]
    signature: str
    # The booking this beat made, when it carried a `facts.upgrade`.  Present
    # only on the record that wrote the row (and on the idempotent replay of
    # its request id); `latest()` reads beats, not bookings, so it is None.
    upgrade: dict[str, Any] | None = None

    @property
    def facts(self) -> dict[str, Any]:
        value = self.body.get("facts")
        return value if isinstance(value, dict) else {}

    @property
    def boot(self) -> dict[str, Any] | None:
        """The guest's report of what it actually booted, when it sent one.

        `facts.boot` is `{release, measurement}`: the release id from the
        machine's measured command line and the 48-byte launch digest from its
        boot report. A heartbeat without it is `unknown`, not a mismatch.
        """
        value = self.facts.get("boot")
        return value if isinstance(value, dict) else None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "slug": self.slug,
            "at": self.at,
            "at_iso": _iso(self.at),
            "received_at": self.received_at,
            "facts": dict(self.facts),
            "boot": dict(self.boot) if self.boot is not None else None,
        }
        if self.upgrade is not None:
            payload["upgrade"] = dict(self.upgrade)
        return payload


@dataclass(frozen=True)
class UpgradeBooking:
    """One row of `upgrade_windows`: a machine's own choice of release/window."""

    id: int
    slug: str
    request_id: str
    release: str
    window: str
    origin: str
    state: str
    heartbeat_id: int | None
    created_at: str
    updated_at: str

    def answer(self) -> dict[str, Any]:
        """The one key the heartbeat answer gains; never the whole row."""
        return {"booked": True, "release": self.release, "window": self.window}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "request_id": self.request_id,
            "release": self.release,
            "window": self.window,
            "origin": self.origin,
            "state": self.state,
            "heartbeat_id": self.heartbeat_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class Alert:
    """One condition worth telling a human about, before it is stored."""

    slug: str
    kind: str
    tenant: str
    symptom: str
    first_command: str
    at: int
    detail: dict[str, Any] = field(default_factory=dict)

    def subject(self) -> str:
        return f"[Odile] {KIND_LABELS[self.kind]} — {self.slug}"

    def text(self) -> str:
        """The alert as a person reads it: who, where, what, and what to run."""
        return (
            f"[Odile] {KIND_LABELS[self.kind]} — {self.slug}\n"
            f"tenant:   {self.tenant or 'unassigned'}\n"
            f"slug:     {self.slug}\n"
            f"symptom:  {self.symptom}\n"
            f"first command: {self.first_command}\n"
            f"detected: {_iso(self.at)}\n"
        )


@dataclass(frozen=True)
class AlertRecord:
    """One row of the alert ledger: open until the condition clears."""

    id: int
    slug: str
    kind: str
    tenant: str
    symptom: str
    first_command: str
    detail: dict[str, Any]
    first_seen_at: int
    last_seen_at: int
    resolved_at: int | None
    deliveries: tuple[dict[str, Any], ...]

    @property
    def open(self) -> bool:
        return self.resolved_at is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "kind": self.kind,
            "tenant": self.tenant,
            "symptom": self.symptom,
            "first_command": self.first_command,
            "detail": dict(self.detail),
            "first_seen_at": self.first_seen_at,
            "first_seen_at_iso": _iso(self.first_seen_at),
            "last_seen_at": self.last_seen_at,
            "last_seen_at_iso": _iso(self.last_seen_at),
            "resolved_at": self.resolved_at,
            "resolved_at_iso": _iso(self.resolved_at) if self.resolved_at else None,
            "open": self.open,
            "deliveries": [dict(item) for item in self.deliveries],
        }


HEARTBEAT_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS heartbeat_keys (
    slug          TEXT PRIMARY KEY,
    public_key    TEXT NOT NULL,
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeats (
    id          INTEGER PRIMARY KEY,
    slug        TEXT    NOT NULL,
    at          INTEGER NOT NULL,
    received_at TEXT    NOT NULL,
    body        TEXT    NOT NULL CHECK (json_valid(body)),
    signature   TEXT    NOT NULL,
    UNIQUE (slug, at)
);

CREATE INDEX IF NOT EXISTS heartbeats_slug_at ON heartbeats(slug, at DESC);

-- One row per condition, open or closed.  The partial unique index is what
-- makes "run it again" safe: a live condition cannot acquire a second row.
CREATE TABLE IF NOT EXISTS health_alerts (
    id            INTEGER PRIMARY KEY,
    slug          TEXT    NOT NULL,
    kind          TEXT    NOT NULL
                  CHECK (kind IN ({_KIND_CHECK})),
    tenant        TEXT    NOT NULL DEFAULT '',
    symptom       TEXT    NOT NULL,
    first_command TEXT    NOT NULL,
    detail        TEXT    NOT NULL DEFAULT '{{}}' CHECK (json_valid(detail)),
    first_seen_at INTEGER NOT NULL,
    last_seen_at  INTEGER NOT NULL,
    resolved_at   INTEGER,
    deliveries    TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(deliveries))
);

CREATE UNIQUE INDEX IF NOT EXISTS health_alerts_open
    ON health_alerts(slug, kind) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS health_alerts_slug ON health_alerts(slug, id);

-- One booking per machine decision (ODL-950).  The row is written in the same
-- transaction as the heartbeat that carried `facts.upgrade`: no beat without a
-- row, no row without a beat.  The partial unique index is what makes a double
-- click one booking and a second booking while one is open a 409; the
-- (slug, request_id) pair is the idempotency key across retries.  The states
-- are ODL-953's page stages and ODL-954's work list.
CREATE TABLE IF NOT EXISTS upgrade_windows (
    id           INTEGER PRIMARY KEY,
    slug         TEXT    NOT NULL,
    request_id   TEXT    NOT NULL,
    release      TEXT    NOT NULL,
    window       TEXT    NOT NULL,
    origin       TEXT    NOT NULL CHECK (origin IN ('click', 'deadline')),
    state        TEXT    NOT NULL DEFAULT 'booked'
                 CHECK (state IN ('booked', 'writing', 'booted', 'restored',
                                  'skipped', 'failed')),
    heartbeat_id INTEGER REFERENCES heartbeats(id),
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    UNIQUE (slug, request_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS upgrade_windows_open
    ON upgrade_windows(slug) WHERE state IN ('booked', 'writing', 'booted');
CREATE INDEX IF NOT EXISTS upgrade_windows_due ON upgrade_windows(state, window);
"""

# The ledger's CHECK is part of the table, so a new alert kind cannot be added
# by `CREATE TABLE IF NOT EXISTS`; the upgrade is the rebuild below, run only
# when the live table's SQL predates a kind in `ALERT_KINDS`.  It is one
# transaction (`BEGIN IMMEDIATE` … `COMMIT`) because `executescript` autocommits:
# a crash between `DROP TABLE health_alerts` and the rename would otherwise leave
# the rows only in `health_alerts_new`, and the next open would recreate an empty
# table whose SQL names every kind — a permanent no-op guard and a lost ledger.
# Every row is copied by name, ids kept, so an open alert stays open.
_MIGRATE_ALERT_KINDS = f"""
BEGIN IMMEDIATE;
DROP TABLE IF EXISTS health_alerts_new;
CREATE TABLE health_alerts_new (
    id            INTEGER PRIMARY KEY,
    slug          TEXT    NOT NULL,
    kind          TEXT    NOT NULL
                  CHECK (kind IN ({_KIND_CHECK})),
    tenant        TEXT    NOT NULL DEFAULT '',
    symptom       TEXT    NOT NULL,
    first_command TEXT    NOT NULL,
    detail        TEXT    NOT NULL DEFAULT '{{}}' CHECK (json_valid(detail)),
    first_seen_at INTEGER NOT NULL,
    last_seen_at  INTEGER NOT NULL,
    resolved_at   INTEGER,
    deliveries    TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(deliveries))
);
INSERT INTO health_alerts_new (id, slug, kind, tenant, symptom, first_command,
                               detail, first_seen_at, last_seen_at, resolved_at,
                               deliveries)
    SELECT id, slug, kind, tenant, symptom, first_command, detail,
           first_seen_at, last_seen_at, resolved_at, deliveries
      FROM health_alerts;
DROP TABLE health_alerts;
ALTER TABLE health_alerts_new RENAME TO health_alerts;
CREATE UNIQUE INDEX IF NOT EXISTS health_alerts_open
    ON health_alerts(slug, kind) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS health_alerts_slug ON health_alerts(slug, id);
COMMIT;
"""

# The interrupted-rebuild rescue: the copy is the only ledger, so it becomes the
# table before anything reads an empty one.  Defensive (the transaction above
# makes the state unreachable on a clean run) and idempotent.
_RECOVER_ALERT_KINDS = """
BEGIN IMMEDIATE;
ALTER TABLE health_alerts_new RENAME TO health_alerts;
CREATE UNIQUE INDEX IF NOT EXISTS health_alerts_open
    ON health_alerts(slug, kind) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS health_alerts_slug ON health_alerts(slug, id);
COMMIT;
"""


def _migrate_alert_kinds(connection: sqlite3.Connection) -> None:
    """Give an older alert ledger the new kinds without losing a row.

    `sqlite_master` is the guard, and it is the *SQL text* that answers: a
    fresh table already names every kind in `ALERT_KINDS`, a migrated one does
    too, and a table cut before ODL-934 does not.  Idempotent because the
    second open sees the new text and returns.
    """
    present = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name IN ('health_alerts', 'health_alerts_new')"
        )
    }
    if "health_alerts" not in present and "health_alerts_new" in present:
        try:
            connection.executescript(_RECOVER_ALERT_KINDS)
        except sqlite3.Error as error:
            connection.rollback()
            raise HeartbeatError(
                f"cannot finish an interrupted alert-ledger rebuild: {error}"
            ) from None
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'health_alerts'"
    ).fetchone()
    if row is None:
        return
    sql = row["sql"] if isinstance(row, sqlite3.Row) else row[0]
    if not sql or all(f"'{kind}'" in sql for kind in ALERT_KINDS):
        return
    connection.commit()
    try:
        connection.executescript(_MIGRATE_ALERT_KINDS)
    except sqlite3.Error as error:
        connection.rollback()
        raise HeartbeatError(f"cannot migrate the alert ledger: {error}") from None

Verifier = Callable[[bytes, bytes, str], bool]


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _upgrade_window_instant(text: str) -> datetime:
    """Parse the one window spelling HQ accepts; raise ValueError otherwise."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def owner_window_text(text: str) -> str:
    """`03:10 UTC on 1 December 2026` — BRAND.md §Voice's owner-facing form.

    The account has no zone, so UTC is named; the date is `1 December 2026`,
    never `01/12/2026` or `December 1`.  The month names are spelled here so
    the message does not change with the service's locale.
    """
    moment = _upgrade_window_instant(text)
    return (
        f"{moment.strftime('%H:%M UTC on')} {moment.day}"
        f" {_MONTH_NAMES[moment.month - 1]} {moment.year}"
    )


def render_upgrade_body(window: str, deadline: str | None = None) -> str:
    """The booking email's body from `templates/emails/upgrade-booked.txt`.

    The deadline paragraph is present only when the served release document
    carries an `upgrade` block; without one the body is the card's text, byte
    for byte, so the two rendered forms differ by exactly that paragraph.
    """
    template = UPGRADE_TEMPLATE.read_text(encoding="utf-8")
    if deadline is None:
        template = template.replace(DEADLINE_PARAGRAPH, "")
    else:
        template = template.replace("{{DEADLINE}}", owner_window_text(deadline))
    return template.replace("{{WINDOW}}", owner_window_text(window))


def served_deadline(document: dict[str, Any] | None) -> str | None:
    """The document's deadline: its own `security_deadline`, else +14 days.

    A document with no `upgrade` block has no deadline, so the email drops the
    paragraph.  `generated_at + 14 days` is the ADR's fallback when the block
    carries no date; a malformed value is treated as no date rather than
    inventing one.
    """
    if not isinstance(document, dict):
        return None
    block = document.get("upgrade")
    if not isinstance(block, dict):
        return None
    value = block.get("security_deadline")
    if isinstance(value, str) and UPGRADE_WINDOW_RE.match(value):
        return value
    generated = document.get("generated_at")
    if isinstance(generated, str) and UPGRADE_WINDOW_RE.match(generated):
        try:
            moment = _upgrade_window_instant(generated) + timedelta(days=14)
        except ValueError:
            return None
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def _epoch(text: str | None) -> int | None:
    """Parse the store's ISO timestamps, returning None instead of raising."""
    if not text:
        return None
    try:
        return int(datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def _json_object(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(text: str | None) -> tuple[dict[str, Any], ...]:
    try:
        value = json.loads(text or "[]")
    except (TypeError, ValueError):
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number(value: Any, default: float | None = None) -> float | None:
    return float(value) if _is_number(value) else default


def decode_signature(value: bytes | str) -> bytes:
    """A raw 64-byte signature or its base64 spelling, never anything else."""
    if isinstance(value, bytes) and len(value) == 64:
        return value
    text = value.decode("ascii", "strict") if isinstance(value, bytes) else value
    text = "".join(text.split())
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error):
        raise HeartbeatRejected("signature is not base64", 400) from None
    if len(raw) != 64:
        raise HeartbeatRejected(
            f"signature is {len(raw)} bytes; an Ed25519 signature is 64", 400
        )
    return raw


def _validated_body(body: bytes) -> dict[str, Any]:
    """Parse and validate one heartbeat envelope; raise `HeartbeatRejected`."""
    if len(body) > MAX_HEARTBEAT_BYTES:
        raise HeartbeatRejected(
            f"heartbeat body is {len(body)} bytes; the limit is {MAX_HEARTBEAT_BYTES}", 413
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise HeartbeatRejected(f"heartbeat body is not UTF-8 JSON: {error}", 400) from None
    if not isinstance(payload, dict):
        raise HeartbeatRejected("heartbeat body must be a JSON object", 400)
    slug = payload.get("slug")
    at = payload.get("at")
    facts = payload.get("facts")
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise HeartbeatRejected(f"slug {slug!r} is not a tenant slug", 400)
    if not isinstance(at, int) or isinstance(at, bool) or at <= 0:
        raise HeartbeatRejected("heartbeat 'at' must be a positive Unix timestamp", 400)
    if not isinstance(facts, dict):
        raise HeartbeatRejected("heartbeat 'facts' must be an object", 400)
    public_key = payload.get("public_key")
    if public_key is not None and not isinstance(public_key, str):
        raise HeartbeatRejected("heartbeat 'public_key' must be a PEM string", 400)
    _validated_facts(facts)
    return payload


def _validated_facts(facts: dict[str, Any]) -> None:
    """Reject a malformed fact at the door, not when an alert divides by it."""
    versions = facts.get("versions")
    if versions is not None:
        if not isinstance(versions, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in versions.items()
        ):
            raise HeartbeatRejected("facts.versions must map names to strings", 400)
    disk = facts.get("disk")
    if disk is not None:
        if not isinstance(disk, dict):
            raise HeartbeatRejected("facts.disk must be an object", 400)
        percent = disk.get("percent")
        if percent is not None and (not _is_number(percent) or not 0 <= float(percent) <= 100):
            raise HeartbeatRejected("facts.disk.percent must be 0..100", 400)
        for name in ("total_bytes", "used_bytes"):
            value = disk.get(name)
            if value is not None and (not _is_number(value) or value < 0):
                raise HeartbeatRejected(f"facts.disk.{name} must be a non-negative number", 400)
    spend = facts.get("spend")
    if spend is not None:
        if not isinstance(spend, dict):
            raise HeartbeatRejected("facts.spend must be an object", 400)
        for name in ("spent_usd", "cap_usd"):
            value = spend.get(name)
            if value is not None and (not _is_number(value) or value < 0):
                raise HeartbeatRejected(f"facts.spend.{name} must be a non-negative number", 400)
    uptime = facts.get("uptime_seconds")
    if uptime is not None and (not _is_number(uptime) or uptime < 0):
        raise HeartbeatRejected("facts.uptime_seconds must be a non-negative number", 400)
    for name in ("last_backup_at", "last_brief_at"):
        value = facts.get(name)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
        ):
            raise HeartbeatRejected(f"facts.{name} must be a positive Unix timestamp", 400)
    cause = facts.get("cause")
    if cause is not None and not isinstance(cause, str):
        raise HeartbeatRejected("facts.cause must be a string", 400)
    boot = facts.get("boot")
    if boot is not None:
        if not isinstance(boot, dict):
            raise HeartbeatRejected("facts.boot must be an object", 400)
        release = boot.get("release")
        if not isinstance(release, str) or not state.RELEASE_ID_RE.match(release.strip()):
            raise HeartbeatRejected(
                "facts.boot.release must look like 2026.11.1", 400
            )
        measurement = boot.get("measurement")
        if not isinstance(measurement, str) or not re.fullmatch(
            r"[0-9a-fA-F]{96}", measurement
        ):
            raise HeartbeatRejected(
                "facts.boot.measurement must be 96 hex characters", 400
            )
    upgrade = facts.get("upgrade")
    if upgrade is not None:
        _validated_upgrade(upgrade)


def _validated_upgrade(upgrade: Any) -> None:
    """The consent fact's shape (ODL-950, ADR-005 §5.3).

    The window's *range* needs the receipt time, so `HeartbeatStore.record`
    checks it after the signature and the idempotency lookup; an idempotent
    replay of an old booking must still answer 200 even when its window has
    since slipped inside the 15-minute quiesce.  Shape is checked here, at the
    door, for every beat.
    """
    if not isinstance(upgrade, dict):
        raise HeartbeatRejected("facts.upgrade must be an object", 400)
    release = upgrade.get("release")
    if not isinstance(release, str) or not state.RELEASE_ID_RE.match(release.strip()):
        raise HeartbeatRejected("facts.upgrade.release must look like 2026.12.1", 400)
    window = upgrade.get("window")
    if not isinstance(window, str) or not UPGRADE_WINDOW_RE.match(window):
        raise HeartbeatRejected(
            "facts.upgrade.window must be YYYY-MM-DDTHH:MM:SSZ", 400
        )
    try:
        _upgrade_window_instant(window)
    except ValueError:
        raise HeartbeatRejected(
            "facts.upgrade.window must be YYYY-MM-DDTHH:MM:SSZ", 400
        ) from None
    if upgrade.get("origin") not in UPGRADE_ORIGINS:
        raise HeartbeatRejected(
            "facts.upgrade.origin must be click or deadline", 400
        )
    request_id = upgrade.get("request_id")
    if not isinstance(request_id, str) or not UPGRADE_REQUEST_RE.match(request_id):
        raise HeartbeatRejected(
            "facts.upgrade.request_id must be 32 lowercase hex characters", 400
        )


def release_report(intended: str | None, beat: Heartbeat | None) -> dict[str, Any]:
    """The intended release beside the release the guest says it booted.

    Three answers, and only one of them is drift: `same`, `release_drift`, and
    `unknown` when either side is silent. A missing `facts.boot` is `unknown`
    by design — an old heartbeat is not evidence of a changed release.
    """
    booted: str | None = None
    if beat is not None and beat.boot is not None:
        value = beat.boot.get("release")
        if isinstance(value, str) and value.strip():
            booted = value.strip()
    chosen = intended.strip() if isinstance(intended, str) and intended.strip() else None
    if chosen is None or booted is None:
        status = "unknown"
    elif chosen == booted:
        status = "same"
    else:
        status = "release_drift"
    return {"intended": chosen, "booted": booted, "status": status}


def _heartbeat(row: sqlite3.Row) -> Heartbeat:
    return Heartbeat(
        id=row["id"],
        slug=row["slug"],
        at=row["at"],
        received_at=row["received_at"],
        body=_json_object(row["body"]),
        signature=row["signature"],
    )


def _upgrade_booking(row: sqlite3.Row) -> UpgradeBooking:
    return UpgradeBooking(
        id=row["id"],
        slug=row["slug"],
        request_id=row["request_id"],
        release=row["release"],
        window=row["window"],
        origin=row["origin"],
        state=row["state"],
        heartbeat_id=row["heartbeat_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _alert(row: sqlite3.Row) -> AlertRecord:
    return AlertRecord(
        id=row["id"],
        slug=row["slug"],
        kind=row["kind"],
        tenant=row["tenant"],
        symptom=row["symptom"],
        first_command=row["first_command"],
        detail=_json_object(row["detail"]),
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        resolved_at=row["resolved_at"],
        deliveries=_json_list(row["deliveries"]),
    )


class HeartbeatStore:
    """The signed-heartbeat table and the alert ledger, on one connection.

    The receiver (`scripts/heartbeat.py`) writes heartbeats; the poller
    (`odilectl health`) reads them and owns the alerts.  Signature verification
    is injected because the receiver is the only place that may run the
    `openssl` binary; a store opened for reading needs no verifier at all.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], int | float] = time.time,
        verifier: Verifier | None = None,
        outbox: str | Path | None = None,
        release_document: str | Path | None = None,
    ) -> None:
        self._con = connection
        self._clock = clock
        self._verifier = verifier
        self._outbox = Path(outbox).expanduser() if outbox else Path(DEFAULT_OUTBOX)
        self._release_document = (
            Path(release_document).expanduser()
            if release_document
            else DEFAULT_RELEASE_DOCUMENT
        )
        # The receiver serves requests on a thread pool; one connection plus a
        # re-entrant lock is enough because SQLite serialises the writes.
        self._lock = threading.RLock()

    @classmethod
    def open(
        cls,
        path: str | Path = DEFAULT_HEARTBEAT_DB,
        *,
        clock: Callable[[], int | float] = time.time,
        verifier: Verifier | None = None,
        outbox: str | Path | None = None,
        release_document: str | Path | None = None,
    ) -> "HeartbeatStore":
        try:
            connection = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            # The ledger's rebuild runs *before* the schema script: after an
            # interrupted rebuild only `health_alerts_new` exists, and
            # `CREATE TABLE IF NOT EXISTS health_alerts` would create an empty
            # table that shadows the copy (and pass the kind guard forever).
            _migrate_alert_kinds(connection)
            connection.executescript(HEARTBEAT_SCHEMA)
            connection.commit()
        except sqlite3.Error as error:
            raise HeartbeatError(f"cannot open the heartbeat database {path}: {error}") from None
        return cls(
            connection,
            clock=clock,
            verifier=verifier,
            outbox=outbox,
            release_document=release_document,
        )

    def close(self) -> None:
        self._con.close()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._con

    def __enter__(self) -> "HeartbeatStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def now(self) -> int:
        return int(self._clock())

    # --------------------------------------------------------------- heartbeat

    def public_key(self, slug: str) -> str | None:
        row = self._con.execute(
            "SELECT public_key FROM heartbeat_keys WHERE slug = ?", (slug,)
        ).fetchone()
        return row["public_key"] if row else None

    def record(
        self,
        body: bytes,
        signature: bytes | str,
        *,
        now: int | None = None,
        verifier: Verifier | None = None,
    ) -> Heartbeat:
        with self._lock:
            return self._record(body, signature, now=now, verifier=verifier)

    def _record(
        self,
        body: bytes,
        signature: bytes | str,
        *,
        now: int | None = None,
        verifier: Verifier | None = None,
    ) -> Heartbeat:
        """Verify one heartbeat, register its key on first sight, store the row.

        The first heartbeat for a slug carries the public half and registers
        it (trust on first use, as the card specifies).  Every later heartbeat
        must verify against that registered key; a different key in the body is
        a conflict, not a quiet re-registration.  `at` must move forward, so a
        captured heartbeat cannot be replayed.

        A beat that carries `facts.upgrade` books its window in the same
        transaction as the beat (ODL-950).  The request id is the idempotency
        key: a replay is the same booking and sends no second email; a second
        decision while one is open is a 409 naming that window.
        """
        payload = _validated_body(body)
        raw_signature = decode_signature(signature)
        slug = payload["slug"]
        at = payload["at"]
        moment = int(now if now is not None else self.now())
        if at > moment + MAX_CLOCK_SKEW_SECONDS:
            raise HeartbeatRejected(
                f"heartbeat at {at} is {at - moment}s in the future", 400
            )
        registered = self.public_key(slug)
        offered = payload.get("public_key")
        if registered is None:
            if not isinstance(offered, str) or "BEGIN PUBLIC KEY" not in offered:
                raise HeartbeatRejected(
                    "the first heartbeat for a tenant must carry its Ed25519 public key", 400
                )
            chosen = offered.strip()
            first = True
        else:
            if isinstance(offered, str) and offered.strip() != registered.strip():
                raise HeartbeatRejected(
                    "heartbeat carries a public key different from the registered key", 409
                )
            chosen = registered
            first = False
        check = verifier or self._verifier
        if check is None:
            raise HeartbeatRejected("no signature verifier configured", 500)
        if not check(body, raw_signature, chosen):
            raise HeartbeatRejected("heartbeat signature does not verify", 401)

        facts = payload["facts"]
        upgrade = facts.get("upgrade")
        if not isinstance(upgrade, dict):
            upgrade = None
        booking: UpgradeBooking | None = None
        answer: dict[str, Any] | None = None
        document: dict[str, Any] | None = None
        if upgrade is not None:
            # The idempotency lookup runs before the `at` and window-range
            # gates: a retry of the same decision is the same booking and gets
            # the same 200, even once its window has slipped inside the quiesce
            # and (ODL-954) even once the row has advanced to a later window.
            existing = self.booking_for(slug, upgrade["request_id"])
            if existing is not None:
                original = None
                if existing.heartbeat_id is not None:
                    original = self._con.execute(
                        "SELECT * FROM heartbeats WHERE id = ?",
                        (int(existing.heartbeat_id),),
                    ).fetchone()
                if original is None:
                    raise HeartbeatError(
                        f"booking {existing.id} has no heartbeat row to replay"
                    )
                return replace(_heartbeat(original), upgrade=existing.answer())
            open_row = self.open_booking(slug)
            if open_row is not None:
                raise HeartbeatRejected(
                    f"an upgrade is already booked for {open_row.window}"
                    f" (request {open_row.request_id})",
                    409,
                )
            after = int(_upgrade_window_instant(upgrade["window"]).timestamp()) - moment
            if after < UPGRADE_MIN_SECONDS:
                raise HeartbeatRejected(
                    f"facts.upgrade.window is {after}s after receipt;"
                    " it must be at least 15 minutes",
                    400,
                )
            if after > UPGRADE_MAX_SECONDS:
                raise HeartbeatRejected(
                    f"facts.upgrade.window is {after}s after receipt;"
                    " it must be at most 26 hours",
                    400,
                )
            # The release must be the one HQ serves, not merely well-formed:
            # a machine that has not re-read the document (or names the previous
            # cut) stores its beat and gets `stale`, with no row and no email.
            document = self._served_release_document()
            served = document.get("release") if document else None
            if not isinstance(served, str) or upgrade["release"].strip() != served.strip():
                answer = {"booked": False, "stale": True}

        latest = self.latest(slug)
        if latest is not None and at <= latest.at:
            raise HeartbeatRejected(
                f"heartbeat at {at} is not newer than the last accepted one ({latest.at})",
                409,
            )

        stamp = _iso(moment)
        signature_text = base64.b64encode(raw_signature).decode("ascii")
        email_path: Path | None = None
        try:
            if first:
                self._con.execute(
                    "INSERT INTO heartbeat_keys (slug, public_key, registered_at) VALUES (?, ?, ?)",
                    (slug, chosen, stamp),
                )
            cursor = self._con.execute(
                "INSERT INTO heartbeats (slug, at, received_at, body, signature)"
                " VALUES (?, ?, ?, ?, ?)",
                (slug, at, stamp, body.decode("utf-8"), signature_text),
            )
            if upgrade is not None and answer is None:
                heartbeat_id = cursor.lastrowid
                if heartbeat_id is None:
                    raise HeartbeatError("the heartbeat insert returned no row id")
                booking = self._insert_booking(
                    slug, upgrade, moment, int(heartbeat_id)
                )
                email_path = self._queue_upgrade_email(booking, moment, document)
            self._con.commit()
        except Exception:
            self._con.rollback()
            if email_path is not None:
                try:
                    email_path.unlink()
                except OSError:
                    pass
            raise
        row = self._con.execute(
            "SELECT * FROM heartbeats WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        beat = _heartbeat(row)
        if booking is not None:
            answer = booking.answer()
        if answer is not None:
            beat = replace(beat, upgrade=answer)
        return beat

    # ----------------------------------------------------------- upgrade booking

    def booking_for(self, slug: str, request_id: str) -> UpgradeBooking | None:
        """The booking this idempotency key already made, if any."""
        row = self._con.execute(
            "SELECT * FROM upgrade_windows WHERE slug = ? AND request_id = ?",
            (slug, request_id),
        ).fetchone()
        return _upgrade_booking(row) if row else None

    def open_booking(self, slug: str) -> UpgradeBooking | None:
        """The one row that is still open for this machine, if any."""
        row = self._con.execute(
            "SELECT * FROM upgrade_windows WHERE slug = ?"
            " AND state IN ('booked', 'writing', 'booted')"
            " ORDER BY id DESC LIMIT 1",
            (slug,),
        ).fetchone()
        return _upgrade_booking(row) if row else None

    def upgrade_bookings(self, slug: str | None = None) -> tuple[UpgradeBooking, ...]:
        """Every booking, newest first; the executor's work list starts here."""
        if slug is None:
            rows = self._con.execute(
                "SELECT * FROM upgrade_windows ORDER BY id DESC"
            ).fetchall()
        else:
            rows = self._con.execute(
                "SELECT * FROM upgrade_windows WHERE slug = ? ORDER BY id DESC",
                (slug,),
            ).fetchall()
        return tuple(_upgrade_booking(row) for row in rows)

    def _insert_booking(
        self, slug: str, upgrade: dict[str, Any], moment: int, heartbeat_id: int
    ) -> UpgradeBooking:
        stamp = _iso(moment)
        self._con.execute(
            "INSERT INTO upgrade_windows (slug, request_id, release, window, origin,"
            " state, heartbeat_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'booked', ?, ?, ?)",
            (
                slug,
                upgrade["request_id"],
                upgrade["release"].strip(),
                upgrade["window"],
                upgrade["origin"],
                int(heartbeat_id),
                stamp,
                stamp,
            ),
        )
        row = self._con.execute(
            "SELECT * FROM upgrade_windows WHERE slug = ? AND request_id = ?",
            (slug, upgrade["request_id"]),
        ).fetchone()
        return _upgrade_booking(row)

    def _served_release_document(self) -> dict[str, Any] | None:
        """The document app.py serves at `/release.json`, or None if unreadable.

        The booking compares its `release` and reads its deadline block; an
        unreadable document means "no release HQ intends", which books nothing
        (`stale`) rather than guessing.
        """
        try:
            document = json.loads(self._release_document.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def _queue_upgrade_email(
        self, booking: UpgradeBooking, moment: int, document: dict[str, Any] | None
    ) -> Path:
        """Queue the one receipt for this booking row, at 0600 until it is sent.

        Keyed by the request id, so a retry rewrites the same file rather than
        adding a second message.  The caller removes it if the transaction
        rolls back.
        """
        directory = self._outbox / "upgrade"
        directory.mkdir(parents=True, exist_ok=True)
        for path in (self._outbox, directory):
            try:
                path.chmod(0o700)
            except OSError:
                pass
        message = EmailMessage()
        message["From"] = UPGRADE_SENDER
        message["To"] = self._owner_address(booking.slug) or "unassigned"
        message["Reply-To"] = UPGRADE_REPLY_TO
        message["Subject"] = UPGRADE_SUBJECT
        message["Date"] = formatdate(int(moment), usegmt=True)
        message["Message-ID"] = make_msgid(
            idstring=f"odile-upgrade.{booking.request_id}", domain="odile.fyi"
        )
        message.set_content(
            render_upgrade_body(booking.window, served_deadline(document)),
            cte="8bit",
        )
        path = directory / f"{booking.slug}-{booking.request_id}.eml"
        try:
            path.write_bytes(message.as_bytes())
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return path

    def _owner_address(self, slug: str) -> str | None:
        """The owner's address: the web tenants row, then the state detail.

        The heartbeat receiver and the web app share one database, but a test
        store may carry only `tenant_state`; a missing table is "no address",
        never a failed booking.
        """
        try:
            row = self._con.execute(
                "SELECT email FROM tenants WHERE slug = ?", (slug,)
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None and row["email"]:
            return str(row["email"])
        try:
            row = self._con.execute(
                "SELECT detail FROM tenant_state WHERE slug = ?", (slug,)
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None:
            value = _json_object(row["detail"]).get("owner_email")
            if isinstance(value, str) and "@" in value:
                return value
        try:
            row = self._con.execute(
                "SELECT email FROM accounts WHERE slug = ?", (slug,)
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None and row["email"]:
            return str(row["email"])
        return None

    def latest(self, slug: str) -> Heartbeat | None:
        row = self._con.execute(
            "SELECT * FROM heartbeats WHERE slug = ? ORDER BY at DESC, id DESC LIMIT 1",
            (slug,),
        ).fetchone()
        return _heartbeat(row) if row else None

    def latest_all(self) -> dict[str, Heartbeat]:
        rows = self._con.execute(
            "SELECT * FROM heartbeats ORDER BY slug, at DESC, id DESC"
        ).fetchall()
        latest: dict[str, Heartbeat] = {}
        for row in rows:
            latest.setdefault(row["slug"], _heartbeat(row))
        return latest

    def heartbeat_count(self, slug: str | None = None) -> int:
        with self._lock:
            if slug is None:
                row = self._con.execute("SELECT COUNT(*) AS n FROM heartbeats").fetchone()
            else:
                row = self._con.execute(
                    "SELECT COUNT(*) AS n FROM heartbeats WHERE slug = ?", (slug,)
                ).fetchone()
            return int(row["n"])

    # ------------------------------------------------------------ alert ledger

    def open_alert(self, slug: str, kind: str) -> AlertRecord | None:
        row = self._con.execute(
            "SELECT * FROM health_alerts WHERE slug = ? AND kind = ? AND resolved_at IS NULL"
            " ORDER BY id DESC LIMIT 1",
            (slug, kind),
        ).fetchone()
        return _alert(row) if row else None

    def raise_alert(self, alert: Alert, now: int) -> tuple[AlertRecord, bool]:
        """Open one alert, or refresh the open one; return (row, created)."""
        existing = self.open_alert(alert.slug, alert.kind)
        detail = json.dumps(alert.detail, sort_keys=True)
        if existing is not None:
            # A tenant row can lose its owner detail when the state machine
            # writes a transition detail (park_for_owner does); the alert ledger
            # remembers who this tenant was, so a later poll does not blank it.
            self._con.execute(
                "UPDATE health_alerts SET tenant = ?, symptom = ?, first_command = ?,"
                " detail = ?, last_seen_at = ? WHERE id = ?",
                (
                    alert.tenant or existing.tenant,
                    alert.symptom,
                    alert.first_command,
                    detail,
                    int(now),
                    existing.id,
                ),
            )
            self._con.commit()
            return self.open_alert(alert.slug, alert.kind) or existing, False
        cursor = self._con.execute(
            "INSERT INTO health_alerts (slug, kind, tenant, symptom, first_command,"
            " detail, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                alert.slug,
                alert.kind,
                alert.tenant,
                alert.symptom,
                alert.first_command,
                detail,
                int(now),
                int(now),
            ),
        )
        self._con.commit()
        row = self._con.execute(
            "SELECT * FROM health_alerts WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _alert(row), True

    def record_deliveries(self, alert_id: int, deliveries: Sequence[dict[str, Any]]) -> AlertRecord:
        self._con.execute(
            "UPDATE health_alerts SET deliveries = ? WHERE id = ?",
            (json.dumps(list(deliveries), sort_keys=True), int(alert_id)),
        )
        self._con.commit()
        row = self._con.execute(
            "SELECT * FROM health_alerts WHERE id = ?", (int(alert_id),)
        ).fetchone()
        return _alert(row)

    def resolve(self, slug: str, kind: str, now: int) -> bool:
        cursor = self._con.execute(
            "UPDATE health_alerts SET resolved_at = ?"
            " WHERE slug = ? AND kind = ? AND resolved_at IS NULL",
            (int(now), slug, kind),
        )
        self._con.commit()
        return cursor.rowcount > 0

    def alerts(
        self, slug: str | None = None, *, open_only: bool = False
    ) -> tuple[AlertRecord, ...]:
        clauses: list[str] = []
        values: list[Any] = []
        if slug is not None:
            clauses.append("slug = ?")
            values.append(slug)
        if open_only:
            clauses.append("resolved_at IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._con.execute(
            f"SELECT * FROM health_alerts{where} ORDER BY first_seen_at, id", values
        ).fetchall()
        return tuple(_alert(row) for row in rows)


# ---------------------------------------------------------------------------
# Where an alert goes: a local copy, Savio's Matrix, Savio's email
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Delivery:
    """One attempt to put one message in front of one person."""

    channel: str
    recipient: str
    ok: bool
    at: int
    error: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "recipient": self.recipient,
            "ok": self.ok,
            "at": self.at,
            "at_iso": _iso(self.at),
            "error": self.error,
            "detail": self.detail,
        }


class Outbox:
    """The local copy of every alert: JSON lines plus the email bytes.

    It is a transport on purpose.  The fixture evidence runs without a mail
    server or a Matrix homeserver, and this is what it reads back; production
    keeps the same copy next to the live sends, so a delivery failure never
    loses the message.
    """

    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        sender: str = "Odile <alerts@odile.fyi>",
    ) -> None:
        self.directory = Path(directory).expanduser() if directory else None
        self.sender = sender

    def record(
        self,
        *,
        kind: str,
        slug: str,
        subject: str,
        body: str,
        channel: str,
        recipient: str,
        at: int,
        email: bool = False,
        message_id: str | None = None,
    ) -> Delivery:
        if self.directory is None:
            return Delivery(channel, recipient, True, at)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            line = {
                "at": int(at),
                "at_iso": _iso(at),
                "kind": kind,
                "slug": slug,
                "channel": channel,
                "recipient": recipient,
                "subject": subject,
                "body": body,
            }
            with (self.directory / "alerts.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, sort_keys=True) + "\n")
            detail = str(self.directory / "alerts.jsonl")
            if email:
                email_dir = self.directory / "emails"
                email_dir.mkdir(parents=True, exist_ok=True)
                message = EmailMessage()
                message["From"] = self.sender
                message["To"] = recipient or "unassigned"
                message["Subject"] = subject
                message["Date"] = formatdate(int(at), usegmt=True)
                message["Message-ID"] = message_id or make_msgid(
                    idstring=f"odile-health.{kind}.{slug}", domain="odile.fyi"
                )
                message.set_content(body)
                path = email_dir / f"{int(at)}-{kind}-{slug}.eml"
                path.write_bytes(message.as_bytes())
                detail = str(path)
            return Delivery(channel, recipient, True, int(at), detail=detail)
        except OSError as error:
            return Delivery(channel, recipient, False, int(at), str(error))


class SmtpSender:
    """Email over the standard library, with the password read at run time."""

    channel = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        sender: str,
        username: str | None = None,
        password: str | None = None,
        starttls: bool = True,
        ca_file: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.sender = sender
        self.username = username
        self.password = password
        self.starttls = starttls
        self.ca_file = ca_file
        self.timeout = timeout

    @classmethod
    def from_config(cls, config: Config) -> "SmtpSender":
        host = config.get("smtp_host")
        sender = config.get("smtp_from") or config.get("smtp_username")
        if not host or not sender:
            raise ConfigError("smtp transport needs smtp_host and smtp_from (mode 0600 config)")
        password = config.get("smtp_password")
        password_file = config.path_value("smtp_password_file")
        if password is None and password_file is not None:
            try:
                password = password_file.read_text(encoding="utf-8").strip()
            except OSError as error:
                raise ConfigError(f"cannot read smtp_password_file {password_file}: {error}") from None
        return cls(
            host=host,
            port=int(config.get("smtp_port") or 587),
            sender=sender,
            username=config.get("smtp_username"),
            password=password,
            starttls=(config.get("smtp_starttls") or "true").strip().lower()
            not in ("0", "false", "no"),
            ca_file=config.get("smtp_ca_file") or config.get("smtp_tls_ca"),
        )

    def send(self, to: str, subject: str, body: str, *, at: int) -> Delivery:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject
        message["Date"] = formatdate(int(at), usegmt=True)
        message["Message-ID"] = make_msgid(
            idstring=f"odile-health.{int(at)}", domain="odile.fyi"
        )
        message.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                smtp.ehlo()
                if self.starttls:
                    context = (
                        ssl.create_default_context(cafile=self.ca_file)
                        if self.ca_file
                        else ssl.create_default_context()
                    )
                    smtp.starttls(context=context)
                    smtp.ehlo()
                if self.username:
                    smtp.login(self.username, self.password or "")
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as error:
            return Delivery(self.channel, to, False, int(at), str(error)[:300])
        return Delivery(self.channel, to, True, int(at))


class MatrixSender:
    """One Matrix message, via the client-server API, with a bearer token."""

    channel = "matrix"

    def __init__(
        self,
        *,
        homeserver: str,
        room_id: str,
        token: str,
        timeout: float = 10.0,
    ) -> None:
        self.homeserver = homeserver.rstrip("/")
        self.room_id = room_id
        self.token = token
        self.timeout = timeout

    @classmethod
    def from_config(cls, config: Config) -> "MatrixSender":
        homeserver = config.get("matrix_homeserver") or "https://matrix.org"
        room_id = config.get("matrix_room_id")
        token = config.get("matrix_access_token")
        token_file = config.path_value("matrix_token_file")
        if token is None and token_file is not None:
            try:
                token = token_file.read_text(encoding="utf-8").strip()
            except OSError as error:
                raise ConfigError(f"cannot read matrix_token_file {token_file}: {error}") from None
        if not room_id or not token:
            raise ConfigError(
                "matrix transport needs matrix_room_id and matrix_token_file (mode 0600)"
            )
        return cls(homeserver=homeserver, room_id=room_id, token=token)

    def send(self, text: str, *, at: int, slug: str, kind: str) -> Delivery:
        transaction = f"odile-health-{int(at)}-{slug}-{kind}"
        url = (
            f"{self.homeserver}/_matrix/client/v3/rooms/"
            f"{urllib.parse.quote(self.room_id, safe='')}/send/m.room.message/"
            f"{urllib.parse.quote(transaction, safe='')}"
        )
        request = urllib.request.Request(
            url,
            data=json.dumps({"msgtype": "m.text", "body": text}).encode("utf-8"),
            method="PUT",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read(4096)
        except (urllib.error.URLError, OSError) as error:
            return Delivery(self.channel, self.room_id, False, int(at), str(error)[:300])
        return Delivery(self.channel, self.room_id, True, int(at))


class Courier:
    """Compose a message once, put a copy in the outbox, then send it."""

    def __init__(
        self,
        *,
        outbox: Outbox | None = None,
        email_sender: SmtpSender | None = None,
        matrix_sender: MatrixSender | None = None,
    ) -> None:
        self.outbox = outbox or Outbox(None)
        self.email_sender = email_sender
        self.matrix_sender = matrix_sender

    def send_alert(self, alert: Alert, *, alert_email: str | None) -> list[dict[str, Any]]:
        subject = alert.subject()
        body = alert.text()
        deliveries: list[Delivery] = [
            self.outbox.record(
                kind=alert.kind,
                slug=alert.slug,
                subject=subject,
                body=body,
                channel="outbox",
                recipient=alert_email or "",
                at=alert.at,
                email=bool(alert_email),
            )
        ]
        if alert_email and self.email_sender is not None:
            deliveries.append(self.email_sender.send(alert_email, subject, body, at=alert.at))
        if self.matrix_sender is not None:
            deliveries.append(
                self.matrix_sender.send(body, at=alert.at, slug=alert.slug, kind=alert.kind)
            )
        return [delivery.to_dict() for delivery in deliveries]


# ---------------------------------------------------------------------------
# The poll: tenant state + heartbeat + port 22 + /health -> alerts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpObservation:
    status: int | None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "detail": self.detail}


class TenantHttpProbe:
    """GET one tenant's `/health`; a failure is an observation, never a crash."""

    def __init__(self, template: str = f"https://{{slug}}.{DEFAULT_TENANT_DOMAIN}/health", timeout: float = 5.0) -> None:
        self.template = template
        self.timeout = timeout

    def __call__(self, slug: str) -> HttpObservation:
        url = self.template.format(slug=slug)
        request = urllib.request.Request(url, headers={"User-Agent": "odile-health/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(4096).decode("utf-8", "replace").strip()
                return HttpObservation(response.status, body[:200])
        except urllib.error.HTTPError as error:
            return HttpObservation(error.code, f"HTTP {error.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            return HttpObservation(None, str(error)[:200])


# ---------------------------------------------------------------- guest ready
#
# ODL-947: "ready" means the machine's own guest half is serving, not that the
# provider says the VM is active. The one definition both the account queue and
# the claim queue use is this probe: the machine's own `/verify` page answers
# 200 *and* its own `/attestation` report is available, and the page prints the
# same measurement the report carries. The page is the object the ready email
# tells the tester to read, so a mail written from a report the page does not
# show would be the defect this card measured. No credential, no SSH, no port
# other than 443: the two URLs are the machine's public surfaces.

VERIFY_TIMEOUT = 45.0        # /verify runs the machine's audit: ~20 s measured
ATTESTATION_TIMEOUT = 15.0
HEX96_RE = re.compile(r"^[0-9a-f]{96}$")
REPORT_RELEASE_RE = re.compile(r"^[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}(-[0-9]+)?$")


@dataclass(frozen=True)
class GuestAnswer:
    """The machine answered at its own name: its page and its own report."""

    slug: str
    host: str
    measurement: str
    release: str
    verify_bytes: int
    checked_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "host": self.host,
            "measurement": self.measurement,
            "release": self.release,
            "verify_bytes": self.verify_bytes,
            "checked_at": self.checked_at,
        }


def _guest_get(url: str, timeout: float,
               opener: Callable[..., Any] | None = None) -> tuple[int, bytes] | None:
    """One GET; `(status, body)` for any HTTP answer, None for no answer.

    A 404/502 is an answer ("not ready"), not an error: the caller only tests
    200. A connection refused or a timeout is also "not ready". Nothing here
    raises, because a machine that is still booting is the normal case.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "odile-ready/0.1"})
    fetch = opener or urllib.request.urlopen
    try:
        with fetch(request, timeout=timeout) as response:
            return int(response.status), response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        try:
            body = error.read(4096)
        except OSError:
            body = b""
        return int(error.code), body
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def machine_measurement(domain: str, slug: str, *,
                        timeout: float = ATTESTATION_TIMEOUT,
                        opener: Callable[..., Any] | None = None) -> str | None:
    """The machine's own 96-hex boot-report measurement, or None.

    This is the number the ready email prints (ODL-947) and the one its
    `/verify` page reports: the 48-byte SHA-384 launch digest from the SEV-SNP
    report, which is also what the machine's boot record calls `measurement`.
    Cheap: one JSON file served by the machine's own Caddy.
    """
    if not SLUG_RE.match(slug or "") or not domain or "/" in domain or "?" in domain:
        return None
    host = "%s.%s" % (slug, domain)
    attestation = _guest_get("https://%s/attestation" % host, timeout, opener)
    if attestation is None or attestation[0] != 200:
        return None
    try:
        document = json.loads(attestation[1].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("available") is not True:
        return None
    measurement = document.get("measurement")
    if not isinstance(measurement, str) or not HEX96_RE.match(measurement):
        return None
    return measurement


def guest_answer(domain: str, slug: str, *,
                 verify_timeout: float = VERIFY_TIMEOUT,
                 attestation_timeout: float = ATTESTATION_TIMEOUT,
                 opener: Callable[..., Any] | None = None) -> GuestAnswer | None:
    """The machine's own answer, or None while the guest half is not up.

    `domain` is the tenant suffix (`test.odile.fyi` on the bench); the host is
    built from it, never from a request header. The order is deliberate: the
    report first (cheap JSON, and the value the mail needs), then the page
    (the expensive audit) only when the report exists. The page must print the
    report's measurement; a page that answers without it is a machine whose
    verify surface is half up, and the row keeps waiting.
    """
    if not SLUG_RE.match(slug or "") or not domain or "/" in domain or "?" in domain:
        return None
    host = "%s.%s" % (slug, domain)
    attestation = _guest_get("https://%s/attestation" % host, attestation_timeout, opener)
    if attestation is None or attestation[0] != 200:
        return None
    try:
        document = json.loads(attestation[1].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("available") is not True:
        return None
    measurement = document.get("measurement")
    if not isinstance(measurement, str) or not HEX96_RE.match(measurement):
        return None
    release = document.get("release")
    if not isinstance(release, str) or not REPORT_RELEASE_RE.match(release):
        release = ""
    verify = _guest_get("https://%s/verify" % host, verify_timeout, opener)
    if verify is None or verify[0] != 200 or len(verify[1]) < 1024:
        return None
    if measurement.encode("ascii") not in verify[1]:
        # The page the tester is told to read does not carry the value the
        # mail would print; the comparison sentence in /verify would be false.
        return None
    return GuestAnswer(
        slug=slug,
        host=host,
        measurement=measurement,
        release=release,
        verify_bytes=len(verify[1]),
        checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _host_key_symptom(observation: HostKeyObservation | None) -> str:
    if observation is None:
        return "port 22 was not probed"
    if not observation.reachable:
        return f"port 22 did not answer ({observation.error or 'unreachable'})"
    if not observation.keys:
        return f"port 22 answered but no host key was read ({observation.error})"
    shown = ", ".join(sorted({key.fingerprint for key in observation.keys}))
    return f"port 22 answers host key {shown}"


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s"


def _owner_email(store: StateStore, tenant: Tenant) -> str | None:
    """The owner's address: the state detail first, the web tenants row second."""
    value = tenant.detail.get("owner_email")
    if isinstance(value, str) and "@" in value:
        return value
    try:
        row = store.connection.execute(
            "SELECT email FROM tenants WHERE slug = ?", (tenant.slug,)
        ).fetchone()
    except sqlite3.Error:
        return None
    return row["email"] if row else None


def _activated_at(store: StateStore, tenant: Tenant) -> int:
    """The last time the tenant became ready, for the first-brief deadline."""
    for event in reversed(store.events(tenant.slug)):
        if event.to_state == "ready":
            moment = _epoch(event.created_at)
            if moment is not None:
                return moment
    return _epoch(tenant.created_at) or 0


def _brief_due_at(now: int, activated_at: int) -> int | None:
    """The most recent 07:00 that was already due, never before activation."""
    moment = datetime.fromtimestamp(int(now), timezone.utc)
    latest = moment.replace(hour=BRIEF_HOUR_UTC, minute=0, second=0, microsecond=0)
    if latest > moment:
        latest -= timedelta(days=1)
    started = datetime.fromtimestamp(int(activated_at), timezone.utc)
    first = started.replace(hour=BRIEF_HOUR_UTC, minute=0, second=0, microsecond=0)
    if first <= started:
        first += timedelta(days=1)
    due = max(latest, first)
    return int(due.timestamp())


def _ssh_command(ipv4: str | None, remote: str) -> str:
    host = ipv4 or "<ip>"
    return f"ssh -i {DEFAULT_USER_KEY} -o IdentitiesOnly=yes root@{host} {remote}"


@dataclass(frozen=True)
class TenantHealth:
    """What one poll saw for one tenant."""

    slug: str
    state: str
    owner: str
    heartbeat_age_seconds: int | None
    last_heartbeat: int | None
    brief_due_at: int | None
    brief_missed: bool
    classification: str
    host_key: dict[str, Any] | None
    http: dict[str, Any] | None
    release: dict[str, Any]
    alerts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "state": self.state,
            "owner": self.owner,
            "heartbeat_age_seconds": self.heartbeat_age_seconds,
            "last_heartbeat": self.last_heartbeat,
            "last_heartbeat_iso": _iso(self.last_heartbeat) if self.last_heartbeat else None,
            "brief_due_at": self.brief_due_at,
            "brief_due_at_iso": _iso(self.brief_due_at) if self.brief_due_at else None,
            "brief_missed": self.brief_missed,
            "classification": self.classification,
            "host_key": self.host_key,
            "http": self.http,
            "release": dict(self.release),
            "alerts": list(self.alerts),
        }


@dataclass(frozen=True)
class HealthResult:
    now: int
    tenants: tuple[TenantHealth, ...]
    alerts: tuple[AlertRecord, ...]
    planned: tuple[Alert, ...]
    resolved: tuple[tuple[str, str], ...]
    applied: bool
    # The box readback this poll was handed (ODL-934); `problem` says why
    # there is none, so a missing or broken record is visible, not green.
    box_usage: dict[str, Any] | None = None
    box_usage_problem: str | None = None

    @property
    def delivery_errors(self) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        for alert in self.alerts:
            for delivery in alert.deliveries:
                if not delivery.get("ok"):
                    errors.append({"slug": alert.slug, "kind": alert.kind, **delivery})
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": _iso(self.now),
            "applied": self.applied,
            "tenants": [tenant.to_dict() for tenant in self.tenants],
            "summary": {
                "tenants": len(self.tenants),
                "alerts": len(self.alerts),
                "open": sum(1 for alert in self.alerts if alert.open),
                "planned": len(self.planned),
                "resolved": len(self.resolved),
            },
            "alerts": [alert.to_dict() for alert in self.alerts],
            "planned_alerts": [
                {
                    "slug": alert.slug,
                    "kind": alert.kind,
                    "tenant": alert.tenant,
                    "symptom": alert.symptom,
                    "first_command": alert.first_command,
                    "at": alert.at,
                    "at_iso": _iso(alert.at),
                }
                for alert in self.planned
            ],
            "resolved": [
                {"slug": slug, "kind": kind} for slug, kind in self.resolved
            ],
            "backup_usage": self.box_usage,
            "backup_usage_problem": self.box_usage_problem,
            "delivery_errors": self.delivery_errors,
        }


def _needs_delivery(record: AlertRecord) -> bool:
    return not record.deliveries or any(
        not delivery.get("ok") for delivery in record.deliveries
    )


def check(
    store: StateStore,
    heartbeats: HeartbeatStore,
    *,
    now: int,
    host_key_probe: Callable[[str], HostKeyObservation] = probe_host_keys,
    http_probe: Callable[[str], HttpObservation] | None = None,
    courier: Courier | None = None,
    apply: bool = False,
    stop_seconds: int = state.HEARTBEAT_STOP_SECONDS,
    disk_percent: float = DISK_ALERT_PERCENT,
    spend_ratio: float = SPEND_ALERT_RATIO,
    backup_seconds: int = BACKUP_ALERT_SECONDS,
    owner_of: Callable[[Tenant], str | None] | None = None,
    alert_email: str | None = None,
    backup_usage_path: str | Path | None = None,
) -> HealthResult:
    """One poll: reconcile the heartbeat, classify, then raise or clear alerts.

    A heartbeat age of `stop_seconds` or more is `down`: under the sealed boot
    the machine comes back by itself, so there is no owner-waiting state to
    classify.  Port 22's answer is a symptom line, and the legacy
    `waiting_for_owner` ledger rows are resolved by this poll and never raised.
    A missed 07:00 brief is folded into the `down` symptom while the heartbeat
    is stopped, and is its own `brief_missing` alert while heartbeats are live.
    The alert ledger is idempotent: an open condition is one row and one
    delivery per channel, not one per five-minute poll.

    `backup_usage_path` is the box readback ODL-934 writes; `None` means this
    poll was not handed one (the unit tests), `odilectl health` always passes
    the configured path.  The box's bytes are its own `stats`, so this is one
    box-level alert under `backup-box-<id>`, never a tenant's.
    """
    moment = int(now)
    owner_lookup = owner_of or (lambda tenant: _owner_email(store, tenant))
    reports: list[TenantHealth] = []
    planned: list[Alert] = []
    resolutions: list[tuple[str, str]] = []

    for tenant in store.tenants():
        if tenant.state not in ("ready", "waiting_for_owner"):
            continue
        # The state machine replaces `detail` on each move, so the alert ledger
        # is the durable memory of who this tenant is: the last non-empty
        # owner wins, open or resolved.
        remembered = ""
        for previous in reversed(heartbeats.alerts(tenant.slug)):
            if previous.tenant:
                remembered = previous.tenant
                break
        owner = owner_lookup(tenant) or remembered or ""
        beat = heartbeats.latest(tenant.slug)
        facts = beat.facts if beat else {}
        last = tenant.last_heartbeat
        if beat is not None and (last is None or beat.at > last):
            if apply:
                tenant = store.heartbeat(tenant.slug, at=beat.at)
            else:
                tenant = replace(tenant, last_heartbeat=beat.at)
            last = beat.at
        age = moment - last if last is not None else None
        activated = _activated_at(store, tenant)
        due = _brief_due_at(moment, activated)
        last_brief = facts.get("last_brief_at")
        if not isinstance(last_brief, int) or isinstance(last_brief, bool):
            last_brief = tenant.detail.get("last_brief_at")
        brief_missed = bool(
            due is not None
            and moment >= due
            and (not isinstance(last_brief, int) or last_brief < due)
        )

        observation: HostKeyObservation | None = None
        classification = "ok"
        if age is not None and age >= stop_seconds:
            # The sealed boot comes back by itself, so a stopped heartbeat is
            # down, whatever port 22 answers.  The probe is read for the
            # symptom only: whether the port answers, and with which key.
            if tenant.ipv4:
                observation = host_key_probe(tenant.ipv4)
            classification = "down"
        http = http_probe(tenant.slug) if http_probe is not None else None

        kinds: list[str] = []
        symptom: str
        cause = facts.get("cause")
        if not isinstance(cause, str) or not cause.strip():
            cause = tenant.detail.get("last_cause")
        if not isinstance(cause, str) or not cause.strip():
            cause = "not reported by the machine"
        else:
            cause = cause.strip()

        if classification == "down":
            symptom = "; ".join(
                [
                    f"heartbeat stopped {_duration(age)} ago",
                    _host_key_symptom(observation),
                    f"brief missed: {'yes' if brief_missed else 'no'}",
                ]
            )
            planned.append(
                Alert(
                    slug=tenant.slug,
                    kind="down",
                    tenant=owner,
                    symptom=symptom,
                    first_command=f"python3 -m odilectl status {tenant.slug} --json",
                    at=moment,
                    detail={
                        "heartbeat_age_seconds": age,
                        "cause": cause,
                        "brief_missed": brief_missed,
                        "http_status": http.status if http else None,
                    },
                )
            )
            kinds.append("down")
            # A legacy open waiting_for_owner row is the same stopped
            # heartbeat; this poll closes it and never raises it again.
            resolutions.append((tenant.slug, "waiting_for_owner"))
        else:
            resolutions.extend(
                [(tenant.slug, "down"), (tenant.slug, "waiting_for_owner")]
            )

        if brief_missed and classification == "ok":
            planned.append(
                Alert(
                    slug=tenant.slug,
                    kind="brief_missing",
                    tenant=owner,
                    symptom=(
                        f"the 07:00 brief did not arrive; heartbeats are live"
                        f" (last beat {_duration(age)} ago)"
                    ),
                    first_command=_ssh_command(tenant.ipv4, "'hermes cron list'"),
                    at=moment,
                    detail={"brief_due_at": due},
                )
            )
            kinds.append("brief_missing")
        elif not brief_missed:
            resolutions.append((tenant.slug, "brief_missing"))

        disk = facts.get("disk")
        percent = _number(disk.get("percent")) if isinstance(disk, dict) else None
        if percent is not None and percent > disk_percent:
            planned.append(
                Alert(
                    slug=tenant.slug,
                    kind="disk",
                    tenant=owner,
                    symptom=f"disk {percent:.1f}% used (threshold {disk_percent:.0f}%)",
                    first_command=_ssh_command(tenant.ipv4, "'df -h /'"),
                    at=moment,
                    detail={"percent": percent, "threshold": disk_percent},
                )
            )
            kinds.append("disk")
        elif percent is not None:
            resolutions.append((tenant.slug, "disk"))

        spend = facts.get("spend")
        spent = _number(spend.get("spent_usd")) if isinstance(spend, dict) else None
        cap = _number(spend.get("cap_usd")) if isinstance(spend, dict) else None
        ratio = (spent / cap) if spent is not None and cap and cap > 0 else None
        if ratio is not None and ratio > spend_ratio:
            planned.append(
                Alert(
                    slug=tenant.slug,
                    kind="spend",
                    tenant=owner,
                    symptom=(
                        f"spend ${spent:.2f} of ${cap:.2f} cap"
                        f" ({ratio * 100:.1f}%, threshold {spend_ratio * 100:.0f}%)"
                    ),
                    first_command=_ssh_command(
                        tenant.ipv4, "'journalctl -u odile-agent -n 50 --no-pager'"
                    ),
                    at=moment,
                    detail={"spent_usd": spent, "cap_usd": cap, "ratio": ratio},
                )
            )
            kinds.append("spend")
        elif ratio is not None:
            resolutions.append((tenant.slug, "spend"))

        last_backup = facts.get("last_backup_at")
        if not isinstance(last_backup, int) or isinstance(last_backup, bool):
            last_backup = tenant.detail.get("last_backup_at")
            if not isinstance(last_backup, int) or isinstance(last_backup, bool):
                last_backup = activated
        backup_age = moment - last_backup if last_backup else None
        if backup_age is not None and backup_age > backup_seconds:
            planned.append(
                Alert(
                    slug=tenant.slug,
                    kind="backup",
                    tenant=owner,
                    symptom=(
                        f"last backup {_duration(backup_age)} ago"
                        f" (threshold {_duration(backup_seconds)})"
                    ),
                    first_command=_ssh_command(
                        tenant.ipv4, "'cat /var/lib/odile/backup.json'"
                    ),
                    at=moment,
                    detail={"last_backup_at": last_backup, "age_seconds": backup_age},
                )
            )
            kinds.append("backup")
        elif backup_age is not None:
            resolutions.append((tenant.slug, "backup"))

        reports.append(
            TenantHealth(
                slug=tenant.slug,
                state=tenant.state,
                owner=owner,
                heartbeat_age_seconds=age,
                last_heartbeat=last,
                brief_due_at=due,
                brief_missed=brief_missed,
                classification=classification,
                host_key=observation.to_dict() if observation else None,
                http=http.to_dict() if http else None,
                release=release_report(tenant.release, beat),
                alerts=tuple(kinds),
            )
        )

    # The box's own bytes (ODL-934): one box-level condition, read from the
    # record the readback wrote.  It is not a tenant's row, so its ledger slug
    # is the box's; a warn and a critical are two kinds so a promotion is a new
    # row and a new delivery, not a rewrite of the open warn row.
    box_usage: dict[str, Any] | None = None
    box_usage_problem: str | None = None
    if backup_usage_path is not None:
        box_usage, box_usage_problem = backup_usage.read_record(backup_usage_path)
    if box_usage is not None:
        box = box_usage["box"]
        total = int(box["total_bytes"])
        level = backup_usage.level_for(total)
        identifier = backup_usage.box_slug(int(box["id"]))
        pieces = [
            f"the backup box {box.get('id')} {box.get('name') or '(unnamed)'} uses"
            f" {backup_usage.human_bytes(total)}"
        ]
        size = box.get("size_bytes")
        if isinstance(size, int) and size > 0:
            pieces[-1] += (
                f" ({total * 100.0 / size:.1f}% of {backup_usage.human_bytes(size)})"
            )
        pieces.append(
            f"{box.get('subaccounts')} of {box.get('subaccounts_limit')} sub-accounts"
        )
        strangers = box_usage.get("unaccounted")
        if isinstance(strangers, list) and strangers:
            pieces.append(f"{len(strangers)} unaccounted")
        readback_at = box_usage.get("at")
        if isinstance(readback_at, int) and not isinstance(readback_at, bool):
            pieces.append(f"readback {_duration(moment - readback_at)} ago")
        symptom = "; ".join(pieces)
        if level == "ok":
            resolutions.extend(
                [(identifier, "backup_usage"), (identifier, "backup_usage_critical")]
            )
        else:
            kind = "backup_usage" if level == "warn" else "backup_usage_critical"
            other = "backup_usage_critical" if level == "warn" else "backup_usage"
            threshold = (
                backup_usage.WARN_BYTES if level == "warn" else backup_usage.CRITICAL_BYTES
            )
            planned.append(
                Alert(
                    slug=identifier,
                    kind=kind,
                    tenant="",
                    symptom=(
                        f"{symptom}; {backup_usage.human_bytes(threshold)}"
                        f" {level} line reached"
                    ),
                    first_command="python3 -m odilectl backup-usage --apply --json",
                    at=moment,
                    detail={
                        "box_id": box.get("id"),
                        "total_bytes": total,
                        "threshold_bytes": threshold,
                        "level": level,
                        "readback_at": readback_at,
                        "unaccounted": [
                            entry.get("name")
                            for entry in (strangers or [])
                            if isinstance(entry, dict)
                        ],
                    },
                )
            )
            resolutions.append((identifier, other))

    # One alert per (slug, kind), even if two branches produced the same one.
    unique: dict[tuple[str, str], Alert] = {}
    for alert in planned:
        unique.setdefault((alert.slug, alert.kind), alert)
    planned = list(unique.values())
    resolutions = list(dict.fromkeys(resolutions))

    final: list[AlertRecord] = []
    resolved_now: list[tuple[str, str]] = []
    if apply:
        for alert in planned:
            record, created = heartbeats.raise_alert(alert, moment)
            if created or _needs_delivery(record):
                deliveries = (
                    courier.send_alert(alert, alert_email=alert_email)
                    if courier is not None
                    else []
                )
                record = heartbeats.record_deliveries(record.id, deliveries)
            final.append(record)
        for slug, kind in resolutions:
            if heartbeats.resolve(slug, kind, moment):
                resolved_now.append((slug, kind))
        final = list(heartbeats.alerts(open_only=False))
    else:
        for alert in planned:
            existing = heartbeats.open_alert(alert.slug, alert.kind)
            if existing is not None:
                final.append(existing)
        for slug, kind in resolutions:
            if heartbeats.open_alert(slug, kind) is not None:
                resolved_now.append((slug, kind))

    return HealthResult(
        now=moment,
        tenants=tuple(reports),
        alerts=tuple(final),
        planned=tuple(planned),
        resolved=tuple(resolved_now),
        applied=bool(apply),
        box_usage=box_usage,
        box_usage_problem=box_usage_problem,
    )


# ---------------------------------------------------------------------------
# The recorded probe and the odilectl command
# ---------------------------------------------------------------------------


def recorded_host_key_probe(path: str | Path) -> Callable[[str], HostKeyObservation]:
    """A probe that answers from `ssh-keyscan`-shaped lines in a file.

    The fixture evidence needs a stopped machine and an initramfs key without a
    stopped VM.  Each non-comment line is `<host> <keytype> <base64 blob>`; a
    line `<host> unreachable` records a host that does not answer.
    """
    entries: dict[str, tuple[HostKey, ...] | None] = {}
    text = Path(path).expanduser().read_text(encoding="utf-8")
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 2 and parts[1].lower() in ("unreachable", "down"):
            entries[parts[0]] = None
            continue
        if len(parts) < 3:
            raise ValueError(f"{path}:{lineno}: expected '<host> <keytype> <base64>'")
        host, key_type, blob = parts[0], parts[1], parts[2]
        try:
            raw_blob = base64.b64decode(blob, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError(f"{path}:{lineno}: host key is not base64") from None
        existing = entries.get(host) or ()
        entries[host] = (*existing, HostKey(key_type, blob, ssh_fingerprint(raw_blob)))

    def probe(host: str) -> HostKeyObservation:
        keys = entries.get(host)
        if keys is None and host in entries:
            return HostKeyObservation(False, (), f"recorded: {host} is unreachable")
        if not keys:
            return HostKeyObservation(False, (), f"recorded: no key for {host}")
        return HostKeyObservation(True, keys)

    return probe


def build_courier(config: Config, args: Any, *, now: int) -> Courier:
    transports = args.transport
    if transports is None:
        raw = config.get("alert_transports") or ""
        transports = [item.strip() for item in raw.split(",") if item.strip()]
    transports = [item.lower() for item in transports]
    unknown = [item for item in transports if item not in ("file", "smtp", "matrix")]
    if unknown:
        raise UsageError(f"unknown alert transport(s): {', '.join(unknown)}")
    outbox_path = args.outbox or config.path_value("alert_outbox", DEFAULT_OUTBOX)
    email_sender = SmtpSender.from_config(config) if "smtp" in transports else None
    matrix_sender = MatrixSender.from_config(config) if "matrix" in transports else None
    return Courier(
        outbox=Outbox(
            outbox_path,
            sender=email_sender.sender if email_sender is not None else "Odile <alerts@odile.fyi>",
        ),
        email_sender=email_sender,
        matrix_sender=matrix_sender,
    )


def _health_lines(result: HealthResult, *, applied: bool) -> list[str]:
    head = (
        f"health: {len(result.tenants)} tenant(s), {len(result.planned)} alert(s),"
        f" {len(result.resolved)} resolved"
    )
    lines = [head if applied else head + " (dry run; pass --apply to write and send)"]
    for tenant in result.tenants:
        age = _duration(tenant.heartbeat_age_seconds)
        release = tenant.release
        release_line = (
            f"release {release['intended'] or 'not recorded'}"
            f" (booted {release['booted'] or 'no report'}: {release['status']})"
        )
        lines.append(
            f"  {tenant.slug}: {tenant.state} -> {tenant.classification};"
            f" heartbeat {age} ago; {release_line}; {', '.join(tenant.alerts) or 'no alert'}"
        )
    for alert in result.alerts:
        mark = "open" if alert.open else "resolved"
        lines.append(
            f"  alert {alert.kind} {alert.slug} {mark} at {_iso(alert.first_seen_at)}:"
            f" {alert.symptom}"
        )
    if result.box_usage is not None:
        box = result.box_usage["box"]
        size = box.get("size_bytes")
        share = (
            f" ({box['total_bytes'] * 100.0 / size:.1f}%)"
            if isinstance(size, int) and size > 0 else ""
        )
        lines.append(
            f"  box {box.get('id')} {box.get('name') or '(unnamed)'}:"
            f" {backup_usage.human_bytes(box['total_bytes'])} used{share};"
            f" {box.get('subaccounts')} of {box.get('subaccounts_limit')} sub-accounts;"
            f" level {backup_usage.level_for(int(box['total_bytes']))};"
            f" readback {_iso(int(result.box_usage.get('at') or 0))}"
        )
    elif result.box_usage_problem is not None:
        lines.append(f"  backup usage: {result.box_usage_problem}")
    return lines


NAME = "health"
HELP = "Poll heartbeats and port 22; a stopped heartbeat is down. Send the alerts."
EXAMPLE = "odilectl health --apply --json"


def add_arguments(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        metavar="PATH",
        help=f"tenant state database (default: the config's state_db, {DEFAULT_HEARTBEAT_DB})",
    )
    parser.add_argument(
        "--heartbeat-db",
        metavar="PATH",
        help="heartbeat and alert database (default: the same file as --db)",
    )
    parser.add_argument(
        "--outbox",
        metavar="PATH",
        help=f"local copy of every alert (default: {DEFAULT_OUTBOX})",
    )
    parser.add_argument(
        "--alert-email",
        metavar="ADDRESS",
        help="Savio's alert address (default: the config's alert_email)",
    )
    parser.add_argument(
        "--transport",
        action="append",
        choices=("file", "smtp", "matrix"),
        help="where alerts go in addition to the outbox; repeatable",
    )
    parser.add_argument(
        "--recorded-host-keys",
        metavar="PATH",
        help="read port-22 keys from a recorded ssh-keyscan-shaped file instead of the network",
    )
    parser.add_argument("--host-key-port", type=int, default=22)
    parser.add_argument("--host-key-timeout", type=float, default=5.0)
    parser.add_argument(
        "--health-url",
        metavar="TEMPLATE",
        help="one tenant's /health URL; {slug} is substituted"
        f" (default: https://{{slug}}.{DEFAULT_TENANT_DOMAIN}/health)",
    )
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument(
        "--no-http", action="store_true", help="do not poll each tenant's /health"
    )
    parser.add_argument(
        "--now",
        type=int,
        metavar="EPOCH",
        help="evaluate at a fixed Unix time; the fixture replay uses it",
    )
    parser.add_argument(
        "--backup-usage",
        metavar="PATH",
        help="the box usage record ODL-934 writes"
        f" (default: {backup_usage.DEFAULT_USAGE_PATH})",
    )


def run(ctx: Context, args: Any) -> Report:
    state_db = Path(args.db).expanduser() if args.db else (
        ctx.config.path_value("state_db", state.DEFAULT_STATE_DB)
        or Path(state.DEFAULT_STATE_DB)
    )
    heartbeat_db = Path(args.heartbeat_db).expanduser() if args.heartbeat_db else (
        ctx.config.path_value("heartbeat_db", state_db) or state_db
    )
    store = StateStore.open(
        state_db, clock=(lambda: int(args.now)) if args.now is not None else time.time
    )
    heartbeats = HeartbeatStore.open(heartbeat_db)
    try:
        now = int(args.now if args.now is not None else time.time())
        if args.recorded_host_keys:
            probe = recorded_host_key_probe(args.recorded_host_keys)
        else:
            probe = lambda host: probe_host_keys(  # noqa: E731 - one small adapter
                host, port=args.host_key_port, timeout=args.host_key_timeout
            )
        http_probe = None
        if not args.no_http:
            template = (
                args.health_url
                or ctx.config.get("tenant_health_url")
                or f"https://{{slug}}.{ctx.config.get('tenant_domain', DEFAULT_TENANT_DOMAIN)}/health"
            )
            http_probe = TenantHttpProbe(template, timeout=args.http_timeout)
        alert_email = args.alert_email or ctx.config.get("alert_email")
        courier = build_courier(ctx.config, args, now=now)
        usage_path = (
            Path(args.backup_usage).expanduser()
            if args.backup_usage
            else Path(backup_usage.DEFAULT_USAGE_PATH)
        )
        result = check(
            store,
            heartbeats,
            now=now,
            host_key_probe=probe,
            http_probe=http_probe,
            courier=courier,
            apply=ctx.mutate,
            alert_email=alert_email,
            backup_usage_path=usage_path,
        )
    finally:
        heartbeats.close()
        store.close()

    payload = {
        "ok": True,
        "command": NAME,
        "dry_run": not ctx.mutate,
        "state_db": str(state_db),
        "heartbeat_db": str(heartbeat_db),
    }
    payload.update(result.to_dict())
    return Report(payload, _health_lines(result, applied=ctx.mutate))
