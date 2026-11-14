#!/bin/sh
# Refresh the cached SEV-SNP report that /attestation and /verify read.
#
# Root only: /dev/sev-guest is 0600. The report is written to a sibling temp
# file and renamed over the cache, so a reader always sees one complete JSON
# document — the old one or the new one, never a partial write.
#
#   /opt/odile/deploy/attest-refresh/refresh.sh
#
# The two overrides exist for the tests; production uses the defaults.
set -eu

OUT=${ODILE_ATTEST_OUT:-/var/lib/odile/attestation.json}
ATTEST=${ODILE_ATTEST_PY:-/opt/odile/attest.py}

tmp=$(mktemp "${OUT}.XXXXXX")
trap 'rm -f "$tmp"' EXIT
trap 'exit 1' HUP INT TERM

/usr/bin/python3 "$ATTEST" "$tmp"
chmod 0644 "$tmp"
mv -f "$tmp" "$OUT"
