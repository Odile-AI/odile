#!/bin/sh
# access-report.sh — write the machine's own /verify report (ODL-473).
#
# The boot oneshot runs this as root. It resolves the pinned measurement the
# user verified, then runs the strict bar's report exactly as the card names
# it and writes the file /verify reads:
#
#   verify_and_unlock.sh --mode access --root / --user odile --expect <pin> \
#       --out /var/lib/odile/access-report.txt
#
# Where the pin comes from, in order:
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
# A pinned number that does not match the report fails the seal line, which is
# the point: the report says `the measurement is not --expect`. No pin at all
# exits 2 and writes nothing, so /verify says "not written yet" instead of
# showing a report checked against nothing.
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

found=$(/usr/bin/python3 - "$PIN_FILES" <<'PY'
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


for path in sys.argv[1].split(":"):
    if not path:
        continue
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        continue
    measurement = value(document)
    if measurement:
        print("%s\t%s" % (path, measurement))
        break
PY
)
if [ -z "$found" ]; then
    echo "access-report: no 96-hex measurement in ${PIN_FILES}" >&2
    exit 2
fi
PIN_FILE=$(printf '%s' "$found" | cut -f1)
PIN=$(printf '%s' "$found" | cut -f2)
if [ -z "$PIN" ]; then
    echo "access-report: $PIN_FILE has no measurement" >&2
    exit 2
fi

echo "access-report: $PIN_FILE pins $PIN"
echo "access-report: verify_and_unlock.sh --mode access --root $ROOT --user $TENANT_USER --expect $PIN --out $OUT"
exec "$REPORTER" --mode access --root "$ROOT" --user "$TENANT_USER" --expect "$PIN" --out "$OUT"
