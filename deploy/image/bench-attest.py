#!/usr/bin/python3
"""bench-attest — the QEMU bench's stand-in for Servury's bb-seed report.

On a Blackbox, `bb-seed` writes `/run/blackbox/attest.json` before switch-root:
the CPU-signed SNP report in a small JSON envelope. This box has no
`/dev/sev-guest` and no PSP, so on the bench that file would never exist and
`/verify`'s seal line and `/attestation` would be empty.

This program writes the same envelope from the piece the bench *can* prove:

* the measurement is the image's own pinned UKI measurement — the value
  `uki.sh` wrote to `/etc/odile/tenant-image.pin` for this exact signed UKI
  and this exact command line (`uki_signed.measurement`);
* the policy is the pinned `0x30000`;
* `release` and `cmdline` are the running machine's own `/proc/cmdline`.

Everything the stand-in does *not* prove is what the runbook says only a
Blackbox can prove: that the CPU signed the report, that `SNP_GET_DERIVED_KEY`
produced the disk key, and that the panel delivered the command line. The
envelope is written 0644 and then transformed by the real `attest.py`
(`odile-attest-boot.service`), so the served document has the same shape a
tenant serves.

    /usr/bin/python3 bench-attest.py \
        --pin /etc/odile/tenant-image.pin \
        --out /run/blackbox/attest.json

If the pin is missing (an image built without `uki.sh --pin`), the measurement
is SHA-384 over the command line and the program exits 0: the page then shows a
report whose number is the command line's, not the pin's, and the runbook says
so. Exit 2 only when `/proc/cmdline` is unreadable.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys

REPORT_SIZE = 1184
MEASUREMENT_OFFSET = 0x90
EXIT_OK = 0
EXIT_USAGE = 2
HEX96 = re.compile(r"^[0-9a-f]{96}$")


def read_cmdline(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError as exc:
        raise SystemExit("bench-attest: cannot read %s: %s" % (path, exc))


def pinned_measurement(path: str) -> str | None:
    """The image's own 96-hex measurement from the UKI pin, or None."""
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    uki = document.get("uki_signed")
    if isinstance(uki, dict):
        value = uki.get("measurement")
        if isinstance(value, str) and HEX96.match(value.strip().lower()):
            return value.strip().lower()
    return None


def report_bytes(measurement: str) -> bytes:
    """A 1184-byte SNP-report-shaped buffer: version 5, policy 0x30000, digest.

    Only the fields `attest.py` and `verify_and_unlock.sh --mode access` read
    are set: the version at 0, the guest policy at 8, the launch digest at
    0x90. The rest is zero, which is what makes it a stand-in rather than a
    signature.
    """
    raw = bytearray(REPORT_SIZE)
    raw[0:4] = (5).to_bytes(4, "little")
    raw[8:16] = (0x30000).to_bytes(8, "little")
    raw[MEASUREMENT_OFFSET:MEASUREMENT_OFFSET + 48] = bytes.fromhex(measurement)
    return bytes(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the bench's boot report envelope.")
    parser.add_argument("--pin", default="/etc/odile/tenant-image.pin")
    parser.add_argument("--cmdline", default="/proc/cmdline")
    parser.add_argument("--out", default="/run/blackbox/attest.json")
    args = parser.parse_args(argv)

    cmdline = read_cmdline(args.cmdline)
    measurement = pinned_measurement(args.pin)
    source = "pin"
    if measurement is None:
        digest = hashlib.sha384(("odile-bench-attest-v1\n" + cmdline).encode()).hexdigest()
        measurement = digest
        source = "cmdline"
    document = {
        "status": "ok",
        "release": next((token.split("=", 1)[1] for token in cmdline.split()
                         if token.startswith("bb.release=")), ""),
        "cmdline": cmdline,
        "report_b64": base64.b64encode(report_bytes(measurement)).decode("ascii"),
        "note": ("the QEMU bench stand-in: measurement from the %s, no CPU "
                 "signature; only a Blackbox produces the real report" % source),
    }
    directory = os.path.dirname(args.out) or "."
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(directory, ".%s.%d.tmp" % (os.path.basename(args.out), os.getpid()))
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temp, 0o644)
        os.replace(temp, args.out)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    print("bench-attest -> %s (measurement from the %s)" % (args.out, source), flush=True)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
