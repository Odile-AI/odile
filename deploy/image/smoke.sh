#!/bin/bash
# smoke.sh — the five image-bench checks.
#
#   ./deploy/image/smoke.sh local                  # in this root (container or guest)
#   ./deploy/image/smoke.sh image --disk out/odile-tenant.raw
#   ./deploy/image/smoke.sh container --image localhost/odile-tenant:v0.23.0
#
# The checks live in bench.py (standard library only) so the same code runs on
# the host, inside the container and inside the QEMU guest. The output ends
# with exactly one `image-bench: PASS` or `image-bench: FAIL` line.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec /usr/bin/python3 "$HERE/bench.py" "$@"
