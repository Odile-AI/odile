#!/bin/sh
# access-report.sh — write the machine's own /verify report (ODL-473).
#
# The boot oneshot runs this as root. It resolves the measurement the report is
# checked against, then runs the strict bar's report exactly as the card names
# it and writes the file /verify reads:
#
#   verify_and_unlock.sh --mode access --root / --user odile --expect <number> \
#       --out /var/lib/odile/access-report.txt
#
# Where the pinned number comes from, in order:
#
#   /etc/odile/tenant-image.pin   uki_signed.measurement — the image's own UKI
#                                 measurement, when a release cut places it
#   /etc/odile/boot-record.json   measurement — the number the user passed to
#                                 `odile_install.py --expect` (ODL-396)
#   /var/lib/odile/attestation.json  measurement — the boot report the machine
#                                 produced; the fallback when nothing pinned it
#   /etc/odile/odile-verify.pin   measurement — the demo/release pin, if a
#                                 tenant copy was ever re-pinned to this image
#
# **The pin names a file; a boot has its own number (ODL-947).** The UKI is
# built with an empty command line and the machine passes its own line at
# launch (`--append`), so the launch measurement covers the per-machine tokens
# (`odile.token=`, `bb.key=`, the slug) *and* the vCPU count. A pin built by
# `uki.sh --vcpus 2` from the builder's line can therefore never equal a 1 vCPU
# tenant's number — measured on the cold walk, where the pin was
# d1aa68fb…/2 vCPU and the machine's own report was 9f2bc74b…/1 vCPU, so this
# unit failed at every boot of an image that had its pin. The number the report
# is checked against is this boot's own when the machine wrote one, and the pin
# when it did not; a disagreement is never silent — one line names both, exactly
# as odile-release-setup.sh already does for the installer's `--expect`.
#
# A pinned number that does match is still a comparison, not a substitution:
# `--expect` fails the seal line when the report's own bytes disagree with it.
# No pin at all exits 2 and writes nothing, so /verify says "not written yet"
# instead of showing a report checked against nothing.
#
# Read-only on the machine: the reporter reads /proc/cmdline, the attestation
# document, every authorized_keys and the tenant .env; it writes only the
# report file (0644, atomic). The unit re-runs after the first visit, when the
# tenant's hash replaces the placeholder.
#
# The env overrides are the bench's: a guest root, a report path and a pin
# list inside a temp directory.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

ROOT=${ODILE_ACCESS_ROOT:-/}
TENANT_USER=${ODILE_ACCESS_USER:-odile}
OUT=${ODILE_ACCESS_OUT:-/var/lib/odile/access-report.txt}
REPORTER=${ODILE_REPORTER:-/usr/local/lib/odile/verify_and_unlock.sh}
PIN_FILES=${ODILE_ACCESS_PIN_FILES:-/etc/odile/tenant-image.pin:/etc/odile/boot-record.json:/var/lib/odile/attestation.json:/etc/odile/odile-verify.pin}
# This boot's own report, when the machine wrote one (odile-attest-boot.service
# is Wants=/After= this unit). Absent on a bench root that never ran that unit,
# and then the pin is the only number there is.
ATTEST=${ODILE_ACCESS_ATTEST:-/var/lib/odile/attestation.json}

found=$(/usr/bin/python3 - "$PIN_FILES" "$ATTEST" <<'PY'
import json
import re
import sys

HEX = re.compile(r"^[0-9a-f]{96}$")


def value(node):
    """A 96-hex measurement under this JSON object, or None."""
    if not isinstance(node, dict):
        return None
    for key in ("measurement", "expected_measurement"):
        candidate = node.get(key)
        if isinstance(candidate, str) and HEX.match(candidate.strip().lower()):
            return candidate.strip().lower()
    uki = node.get("uki_signed")
    if isinstance(uki, dict):
        candidate = uki.get("measurement")
        if isinstance(candidate, str) and HEX.match(candidate.strip().lower()):
            return candidate.strip().lower()
    return None


def read(path):
    """The parsed JSON at path, or None when it is missing or not JSON."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


pin_path = ""
pin = ""
for path in sys.argv[1].split(":"):
    if not path:
        continue
    measurement = value(read(path))
    if measurement:
        pin_path, pin = path, measurement
        break
boot = value(read(sys.argv[2])) or ""
print("pin\t%s\t%s" % (pin_path, pin))
print("boot\t%s" % boot)
PY
)
PIN_FILE=$(printf '%s\n' "$found" | sed -n '1p' | cut -f2)
PIN=$(printf '%s\n' "$found" | sed -n '1p' | cut -f3)
BOOT=$(printf '%s\n' "$found" | sed -n '2p' | cut -f2)
if [ -z "$PIN" ]; then
    echo "access-report: no 96-hex measurement in ${PIN_FILES}" >&2
    exit 2
fi

EXPECT=$PIN
if [ -n "$BOOT" ] && [ "$BOOT" != "$PIN" ]; then
    EXPECT=$BOOT
    echo "access-report: $PIN_FILE pins $PIN, this boot is $BOOT; reporting with the boot's own measurement" >&2
else
    echo "access-report: $PIN_FILE pins $PIN"
fi
echo "access-report: verify_and_unlock.sh --mode access --root $ROOT --user $TENANT_USER --expect $EXPECT --out $OUT"
exec "$REPORTER" --mode access --root "$ROOT" --user "$TENANT_USER" --expect "$EXPECT" --out "$OUT"
