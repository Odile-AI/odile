#!/usr/bin/python3
"""Does the served SEV-SNP report answer *this* caller?

The field under test is REPORT_DATA (offset 0x50, 64 bytes).

AMD SEV-SNP ABI Specification 56860 rev. 1.58, May 2025:
  §7.3, Table 22  MSG_REPORT_REQ.REPORT_DATA is "Guest-provided data to be
                  included in the attestation report"; "the firmware does not
                  interpret this data".
  §7.3, Table 23  REPORT_DATA (50h) is guest-provided, or zero when the host
                  asked (SNP_HV_REPORT_REQ, VMPL = 0xffffffff).
  §7.3, Table 23  MEASUREMENT (90h) is "the measurement calculated at launch".
  §7.3            REPORT_ID is generated per guest and "persists with the guest
                  instance throughout its lifetime".

So REPORT_DATA is the only field a requester chooses and the only field that can
tie a signed report to one request. Everything else describes the launch.

Four checks, each printed as a sentence:

  1. a fresh report carries back the 64 bytes this process chose;
  2. two fetches differ only in REPORT_DATA (and the signature over it);
  3. the report served at /attestation carries HQ's label, not a visitor's
     nonce, so a copy of it is indistinguishable from a fresh one;
  4. a planted one-bit change makes check 1 fail with a plain sentence.

Exit 0 when all four hold; 1 otherwise. Run as root on a SEV-SNP guest:

    ./report_data_test.py [--served URL_OR_PATH]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import sys
import urllib.request

REPORT_DATA_OFF = 0x50
REPORT_DATA_LEN = 64
SIGNED_LEN = 0x2A0
REPORT_SIZE = 1184
HERE = os.path.dirname(os.path.abspath(__file__))


def repo_root() -> str:
    """Walk up until attest.py, so this runs from the evidence directory."""
    path = HERE
    for _ in range(8):
        if os.path.isfile(os.path.join(path, "attest.py")):
            return path
        path = os.path.dirname(path)
    raise SystemExit("cannot find the repository root above %s" % HERE)


sys.path.insert(0, repo_root())
import attest  # noqa: E402  the program that fetches this guest's report


def u32(raw: bytes, off: int) -> int:
    return struct.unpack_from("<I", raw, off)[0]


def u64(raw: bytes, off: int) -> int:
    return struct.unpack_from("<Q", raw, off)[0]


def report_data(raw: bytes) -> bytes:
    return raw[REPORT_DATA_OFF:REPORT_DATA_OFF + REPORT_DATA_LEN]


def fetch_report(nonce: bytes) -> tuple[bytes, bytes]:
    """A fresh report for this guest, and the driver's whole response."""
    if len(nonce) != REPORT_DATA_LEN:
        raise ValueError("a nonce for REPORT_DATA is exactly 64 bytes")
    response = attest.fetch(nonce)
    status, size = u32(response, 0), u32(response, 4)
    if status != 0:
        raise OSError("SNP_GET_REPORT status %d" % status)
    if size != REPORT_SIZE:
        raise OSError("the driver returned %d bytes, not %d" % (size, REPORT_SIZE))
    return response[0x20:0x20 + size], response


def plain_policy(policy: int) -> str:
    """Table 9 of 56860 rev. 1.58, in one sentence a non-specialist can read."""
    bits = []
    bits.append("SMT %s" % ("allowed" if policy & (1 << 16) else "disallowed"))
    bits.append("DEBUG %s" % ("ALLOWED (the host can decrypt this guest's memory)"
                              if policy & (1 << 19) else "clear (no debug decrypt)"))
    bits.append("MIGRATE_MA %s" % ("allowed (a migration agent may be attached)"
                                   if policy & (1 << 18) else "clear (no migration agent)"))
    bits.append("SINGLE_SOCKET %s" % ("required" if policy & (1 << 20) else "not required"))
    if not policy & (1 << 17):
        bits.append("bit 17 is clear, but rev. 1.58 says it must be one")
    return "policy 0x%x: %s; ABI floor %d.%d" % (
        policy, ", ".join(bits), (policy >> 8) & 0xFF, policy & 0xFF)


def plain_platform(info: int) -> str:
    """Table 24 of 56860 rev. 1.58."""
    names = []
    for bit, label in ((0, "SMT on"), (1, "TSME (whole-system encryption) on"),
                       (2, "ECC_EN set"), (3, "RAPL disabled"),
                       (4, "ciphertext hiding for DRAM on"),
                       (5, "alias check complete"), (7, "SEV-TIO on")):
        if info & (1 << bit):
            names.append(label)
    return "platform_info 0x%x: %s" % (info, ", ".join(names) or "nothing set")


def plain_flags(flags: int) -> str:
    """Table 23 offset 48h: bits 4:2 SIGNING_KEY, 1 MASK_CHIP_KEY, 0 AUTHOR_KEY_EN."""
    key = (flags >> 2) & 0x7
    key_name = {0: "VCEK (AMD's per-chip key)", 1: "VLEK (the provider's key)",
                7: "no key; SIGNATURE is zeroes"}.get(key, "reserved value %d" % key)
    return "flags 0x%x: signed by %s; chip id %s; author key %s" % (
        flags, key_name, "masked" if flags & 0x2 else "unmasked",
        "present" if flags & 0x1 else "absent")


def plain_report_data(data: bytes, nonce: bytes | None) -> str:
    if nonce is not None and data == nonce:
        return "REPORT_DATA is the 64 bytes this caller sent: the report answers this request"
    if data == b"\x00" * REPORT_DATA_LEN:
        return ("REPORT_DATA is zero: either the host asked for this report "
                "(VMPL 0xffffffff) or the guest sent zeroes; it binds no request")
    if data.startswith(b"odile:"):
        return ("REPORT_DATA is HQ's label odile:%s..., chosen by the machine at fetch "
                "time, not by this visitor" % data[6:14].hex())
    return ("REPORT_DATA is %s..., a value this caller did not send: it binds no request"
            % data[:8].hex())


def served_document(target: str) -> dict:
    if "://" in target:
        request = urllib.request.Request(
            target, headers={"User-Agent": "odile-security-report-data-test/1"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    with open(target, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--served", default="https://odile.fyi/attestation",
                        help="URL or path of the served JSON (default: %(default)s)")
    args = parser.parse_args(argv)

    failures: list[str] = []
    ok = True

    nonce_a = b"odile-test-A:" + b"A" * 51
    nonce_b = b"odile-test-B:" + b"B" * 51
    nonce_a, nonce_b = nonce_a[:64], nonce_b[:64]

    report_a, _ = fetch_report(nonce_a)
    report_b, _ = fetch_report(nonce_b)

    # The document, read once: every sentence below quotes these bytes.
    print("report    %d bytes, version %d, VMPL %s"
          % (len(report_a), u32(report_a, 0), hex(u32(report_a, 0x30))))
    print("measure   %s (the launch digest; computed at launch, not per request)"
          % report_a[0x90:0xC0].hex())
    print("policy    %s" % plain_policy(u64(report_a, 0x08)))
    print("platform  %s" % plain_platform(u64(report_a, 0x40)))
    print("flags     %s" % plain_flags(u32(report_a, 0x48)))
    print("report_id %s (per guest; the same value in both fetches)"
          % report_a[0x140:0x160].hex())
    print()

    # CHECK 1 — the field follows the caller.
    if report_data(report_a) == nonce_a:
        print("CHECK 1  PASS  %s" % plain_report_data(report_data(report_a), nonce_a))
    else:
        ok = False
        print("CHECK 1  FAIL  %s" % plain_report_data(report_data(report_a), nonce_a))
        failures.append("1")

    # CHECK 2 — only REPORT_DATA is per request.
    signed_a = report_a[:SIGNED_LEN]
    signed_b = report_b[:SIGNED_LEN]
    differ = [off for off in range(SIGNED_LEN)
              if signed_a[off] != signed_b[off]]
    only_report_data = all(REPORT_DATA_OFF <= off < REPORT_DATA_OFF + REPORT_DATA_LEN
                           for off in differ)
    same_id = report_a[0x140:0x160] == report_b[0x140:0x160]
    if only_report_data and same_id and report_data(report_b) == nonce_b:
        print("CHECK 2  PASS  two fetches differ only in REPORT_DATA (and the "
              "signature over it); REPORT_ID %s persists across both"
              % report_a[0x140:0x160].hex()[:16])
    else:
        ok = False
        print("CHECK 2  FAIL  the fetches differ in %d signed bytes outside "
              "REPORT_DATA; same report id: %s" % (
                  len([o for o in differ if not REPORT_DATA_OFF <= o < REPORT_DATA_OFF + 64]),
                  same_id))
        failures.append("2")

    # CHECK 3 — the served report answers HQ, not the visitor.
    document = served_document(args.served)
    served_raw = base64.b64decode(document["report_b64"])
    served_data = report_data(served_raw)
    served_id = served_raw[0x140:0x160].hex()
    if (served_data.startswith(b"odile:") and served_data != nonce_a
            and served_data != nonce_b and served_id == report_a[0x140:0x160].hex()):
        print("CHECK 3  PASS  the served report (%s, fetched_at %s) carries HQ's "
              "label odile:%s..., chosen by the machine at fetch time, not by this "
              "visitor: a copy of it verifies identically a week later"
              % (args.served, document.get("fetched_at"), served_data[6:14].hex()))
    else:
        ok = False
        print("CHECK 3  FAIL  the served report's REPORT_DATA is %s; report id %s"
              % (served_data.hex(), served_id))
        failures.append("3")

    # CHECK 4 — a planted positive: the assertion is not vacuous.
    planted = bytearray(report_a)
    planted[REPORT_DATA_OFF] ^= 0x01
    planted_data = report_data(bytes(planted))
    if planted_data != nonce_a:
        print("CHECK 4  PASS  one flipped bit makes the same test say: %s"
              % plain_report_data(planted_data, nonce_a))
    else:
        ok = False
        print("CHECK 4  FAIL  a flipped byte still matched the nonce")
        failures.append("4")

    print()
    print("VERDICT  %s" % (
        "REPORT_DATA is the only field a requester chooses; the served report "
        "was fetched by HQ on its own clock, so it proves the machine's boot, "
        "not that this visitor's request produced it"
        if ok else "checks %s failed" % ", ".join(failures)))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
