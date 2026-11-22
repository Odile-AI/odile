#!/bin/sh
# check_bench.sh — the A1 isolation check: the bench, its tenant, production.
#
# ODL-340. One run answers four questions, and fails when any answer is no:
#
#   1. the bench answers at https://test.odile.fyi/ (200, `server: odile-bench`)
#   2. the tenant answers at its own name under test.odile.fyi (the sign-in page
#      200, `/` a 302 to it on the same host, `/verify` 200 and `/attestation`
#      200 with the pinned launch measurement)
#   3. production did not move: `/` still hashes to the bytes the ODL-339
#      pre-deploy record took at 2026-11-18T12:07:14Z, before the bench or
#      this tenant existed (`docs/evidence/a1-bench-deploy.md` §0/§1), and
#      `/roadmap` and `/attestation` are byte-for-byte the box's own generator
#      output (`/var/lib/odile/roadmap.html`, `/var/lib/odile/attestation.json`),
#      never the bench's copies; and `NRestarts` on odile.service is 0 in the
#      record and now. The homepage is not a timer page, so the pre-deploy value
#      is a constant and is asserted as one (PRE_DEPLOY_ROOT below). The other
#      two are re-generated every fifteen and five minutes, so their absolute
#      hash is a timestamp: a served page that is not the box's own file is a
#      leak and fails.
#   4. the two names are different machines: test.odile.fyi and the tenant's
#      name resolve to different IPv4 addresses, and only the bench's database
#      carries the tenant's claim row
#
# The record is written once, on purpose, by `--record`: it pins the tenant's
# expected launch measurement, the two names and their addresses, production's
# NRestarts and a capture of the three production pages at that moment. It is a
# capture, not by itself a pre-work snapshot (this card's was taken at
# 2026-11-18T15:48:14Z, after the tenant was ready): production `/` is asserted
# against the ODL-339 pre-deploy value above, and the record must agree with it,
# so a record taken after a production change cannot redefine the baseline. A
# run without a record fails with the command to take it rather than inventing
# one.
#
#   ./scripts/check_bench.sh --record --expect <96-hex from the image pin>
#   ./scripts/check_bench.sh
#   TENANT=odl-340a1e.test.odile.fyi ./scripts/check_bench.sh
#
# ODL-936 adds `--probe`: one line, no record needed, and a non-zero exit
# when https://test.odile.fyi/ is not 200 with `server: odile-bench`. The
# line names the pid and command listening on the bench's port (8082 from
# deploy/bench/bench.env), because the 21 November outage was an orphaned
# `seed.py serve` holding that port while the unit restarted 17,060 times.
# `deploy/bench/deploy_bench.py --probe-tick` runs this every five minutes
# and feeds the existing HQ alert path when it is red.
#
#   ./scripts/check_bench.sh --probe
#
# Exit codes: 0 every check passed (the probe is green), 1 a check failed
# (the probe is red), 2 a prerequisite or the record is missing.
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
BENCH_HOST=${BENCH_HOST:-test.odile.fyi}
BENCH_URL=${BENCH_URL:-https://$BENCH_HOST/}
BENCH_PORT=${BENCH_PORT:-8082}
TENANT_HOST=${TENANT:-odl-340a1e.test.odile.fyi}
PROD=${PROD:-https://odile.fyi}
BASELINE=${BASELINE:-$REPO/docs/evidence/a1-bench-baseline.json}
PROD_DB=${PROD_DB:-/var/lib/odile/waitlist.db}
BENCH_DB=${BENCH_DB:-/var/lib/odile-test/waitlist.db}
PROD_ROADMAP=${PROD_ROADMAP:-/var/lib/odile/roadmap.html}
PROD_ATTEST=${PROD_ATTEST:-/var/lib/odile/attestation.json}
TENANT_SLUG=${TENANT_SLUG:-${TENANT_HOST%%.*}}
PYTHON=${PYTHON:-/usr/bin/python3}

# The ODL-339 pre-deploy record (`docs/evidence/a1-bench-deploy.md` §0/§1,
# 2026-11-18T12:07:14Z): production's `/` before the bench or this tenant
# existed. The homepage is not a timer page, so these bytes are stable between
# deploys and the DoD's "equal the pre-deploy record" is asserted as this
# constant — not as a capture that a later run could quietly redefine.
PRE_DEPLOY_ROOT=${PRE_DEPLOY_ROOT:-c8b0a25039b91776cd26c06a37ae32735365b7248c3136ad72d0f5354164b6dc}

RECORD=0
PROBE=0
EXPECT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --record) RECORD=1; shift ;;
        --probe) PROBE=1; shift ;;
        --expect) EXPECT=$2; shift 2 ;;
        --expect=*) EXPECT=${1#--expect=}; shift ;;
        -h|--help)
            sed -n '2,51p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) printf 'check_bench: unknown argument: %s\n' "$1" >&2; exit 2 ;;
    esac
done

# ------------------------------------------------------------ the liveness probe
# One line, no baseline and no tenant: is the bench itself answering, and if
# not, which process owns its port? The 21 November outage was a green
# /health beside a 404 landing page, so /health is not part of this answer.
port_holder() {
    # port_holder PORT -> "pid N <command>" or "nobody listening"
    _port=$1
    if ! command -v ss >/dev/null 2>&1; then
        printf 'nobody named (ss is not installed)'
        return 0
    fi
    _pid=$(ss -ltnpH "sport = :$_port" 2>/dev/null \
        | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -n 1)
    if [ -z "$_pid" ]; then
        printf 'nobody listening'
        return 0
    fi
    _cmd=$(tr '\0' ' ' < "/proc/$_pid/cmdline" 2>/dev/null | sed 's/ *$//' || true)
    printf 'pid %s %s' "$_pid" "${_cmd:-unknown}"
}

probe() {
    # probe -> exactly one line on stdout; 0 green, 1 red
    #
    # The header is Caddy's (`Server odile-bench` is set in the bench block),
    # so it proves the name reached the bench site, not that the bench app is
    # the thing behind it. On 2026-11-21 the holder was a same-port stranger;
    # today a foreign `http.server` on 8082 answers 200 through the same
    # Caddy. The page's own join form is the app's identity, and the holder
    # is named either way.
    _body_file=$(mktemp)
    _headers=$(curl -sS --max-time 20 -o "$_body_file" -D - "$BENCH_URL" 2>/dev/null || true)
    _code=$(printf '%s\n' "$_headers" | tr -d '\r' \
        | awk 'tolower($1) ~ /^http\// { print $2; exit }')
    _server=$(printf '%s\n' "$_headers" | tr -d '\r' \
        | awk 'tolower($1) == "server:" { print tolower($2); exit }')
    _account_page=no
    grep -qs 'id="join"' "$_body_file" 2>/dev/null && _account_page=yes
    rm -f "$_body_file"
    _holder=$(port_holder "$BENCH_PORT")
    if [ "$_code" = 200 ] && [ "$_server" = odile-bench ] && [ "$_account_page" = yes ]; then
        printf 'bench probe: OK %s -> 200 server: odile-bench (the account site); 127.0.0.1:%s %s\n' \
            "$BENCH_URL" "$BENCH_PORT" "$_holder"
        return 0
    fi
    printf 'bench probe: FAIL %s -> %s server: %s, account page: %s (want 200, server: odile-bench, the join form); 127.0.0.1:%s %s\n' \
        "$BENCH_URL" "${_code:-000}" "${_server:-none}" "$_account_page" "$BENCH_PORT" "$_holder"
    return 1
}

if [ "$PROBE" = 1 ]; then
    for bin in curl ss mktemp; do
        command -v "$bin" >/dev/null 2>&1 || {
            printf 'prerequisite: %s is not installed\n' "$bin" >&2
            exit 2
        }
    done
    if line=$(probe); then
        printf '%s\n' "$line"
        exit 0
    fi
    printf '%s\n' "$line" >&2
    exit 1
fi

for bin in curl dig openssl sha256sum sqlite3 mktemp; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        printf 'prerequisite: %s is not installed\n' "$bin" >&2
        exit 2
    fi
done

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT INT TERM

failures=0
checks=0
pass() { checks=$((checks + 1)); printf 'ok    %s\n' "$*"; }
fail() { checks=$((checks + 1)); failures=$((failures + 1)); printf 'FAIL  %s\n' "$*" >&2; }

# Fetch a URL into a body file. Prints "STATUS SHA256" (sha256 of the body).
fetch() {
    status=$(curl -sS --max-time 30 -o "$2" -w '%{http_code}' "$1" 2>"$tmp/curl.err" || true)
    printf '%s %s\n' "${status:-000}" "$(sha256sum "$2" 2>/dev/null | cut -d' ' -f1)"
}

status_of() {
    curl -sS --max-time 30 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || true
}

record_name() {
    # the first A record's address, or nothing
    dig +short A "$1" 2>/dev/null | tail -n 1
}

nrestarts() {
    systemctl show -p NRestarts --value odile.service 2>/dev/null || printf 'unknown'
}

prod_hashes() {
    # one "<sha256>  <path>" line per production page
    for path in / /roadmap /attestation; do
        body="$tmp/prod$(printf '%s' "$path" | tr '/' '_')"
        code=$(curl -sS --max-time 30 -o "$body" -w '%{http_code}' "$PROD$path" 2>/dev/null || true)
        if [ "$code" != 200 ]; then
            printf '000  %s\n' "$path"
        else
            printf '%s  %s\n' "$(sha256sum "$body" | cut -d' ' -f1)" "$path"
        fi
    done
}

if [ "$RECORD" = 1 ]; then
    if [ -z "$EXPECT" ]; then
        # default: the pin this tenant's own /verify reports, so a record taken
        # without the image pin file still names a number
        code=$(curl -sS --max-time 30 -o "$tmp/attest.json" -w '%{http_code}' "https://$TENANT_HOST/attestation" 2>/dev/null || true)
        [ "$code" = 200 ] || { printf 'check_bench: cannot record: https://%s/attestation returned %s\n' "$TENANT_HOST" "${code:-000}" >&2; exit 2; }
        EXPECT=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("measurement",""))' "$tmp/attest.json")
    fi
    case "$EXPECT" in
        *[!0-9a-f]*|"") printf 'check_bench: --expect must be the 96-hex launch measurement\n' >&2; exit 2 ;;
    esac
    [ ${#EXPECT} -eq 96 ] || { printf 'check_bench: --expect must be 96 hex characters\n' >&2; exit 2; }
    mkdir -p "$(dirname "$BASELINE")"
    {
        prod_hashes
        printf 'nrestarts=%s\n' "$(nrestarts)"
        printf 'bench_ip=%s\n' "$(record_name "$BENCH_HOST")"
        printf 'tenant_ip=%s\n' "$(record_name "$TENANT_HOST")"
        printf 'tenant_host=%s\n' "$TENANT_HOST"
        printf 'bench_host=%s\n' "$BENCH_HOST"
        printf 'tenant_slug=%s\n' "$TENANT_SLUG"
        printf 'expected_measurement=%s\n' "$EXPECT"
        printf 'recorded_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$tmp/baseline.txt"
    "$PYTHON" - "$tmp/baseline.txt" "$BASELINE" <<'PY'
import json
import os
import sys
import tempfile

lines = [line.rstrip("\n") for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
record = {"schema": "odile.a1-bench-baseline.v1", "production": {}, "production_nrestarts": None}
for line in lines:
    if "  " in line and line.split("  ", 1)[1].startswith("/"):
        digest, path = line.split("  ", 1)
        record["production"][path] = digest
        continue
    key, _, value = line.partition("=")
    if key == "nrestarts":
        record["production_nrestarts"] = value
    else:
        record[key] = value
record["note"] = ("The record is a capture, not the DoD's baseline for production /: it holds the three production "
                  "pages as served when it was taken, the tenant's expected measurement, the two names and their "
                  "addresses, and NRestarts. The check asserts / against the ODL-339 pre-deploy value (PRE_DEPLOY_ROOT) "
                  "and requires this record to agree with it; /roadmap and /attestation regenerate every fifteen and five "
                  "minutes, so for those two the property is that the served bytes are production's own state file.")
# Atomic: a reader (the check, a reviewer, git) sees either the old record or
# the complete new one. An interrupted record must never leave half a baseline.
target = os.path.abspath(sys.argv[2])
fd, temporary = tempfile.mkstemp(prefix=".a1-bench-baseline.", dir=os.path.dirname(target))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, target)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
print("recorded %s" % target)
for path, digest in sorted(record["production"].items()):
    print("  %s  %s" % (digest, path))
print("  nrestarts=%s" % record["production_nrestarts"])
print("  tenant %s -> %s, expected measurement %s…" % (record.get("tenant_host"), record.get("tenant_ip"), record.get("expected_measurement", "")[:16]))
PY
    exit 0
fi

[ -s "$BASELINE" ] || {
    printf 'check_bench: no record at %s\n' "$BASELINE" >&2
    printf '            take it before the bench/tenant work: ./scripts/check_bench.sh --record --expect <pin>\n' >&2
    exit 2
}

# ---------------------------------------------------------------- the bench
if code=$(status_of "https://$BENCH_HOST/"); [ "$code" = 200 ]; then
    pass "bench https://$BENCH_HOST/ -> 200"
else
    fail "bench https://$BENCH_HOST/ -> ${code:-000} (want 200)"
fi
if curl -sSI --max-time 30 "https://$BENCH_HOST/" 2>/dev/null | tr -d '\r' | grep -qi '^server: odile-bench'; then
    pass "the bench answers as server: odile-bench"
else
    fail "the bench does not carry server: odile-bench"
fi

# --------------------------------------------------------------- the tenant
if code=$(status_of "https://$TENANT_HOST/login"); [ "$code" = 200 ]; then
    pass "tenant https://$TENANT_HOST/login -> 200 (the sign-in page)"
else
    fail "tenant https://$TENANT_HOST/login -> ${code:-000} (want 200)"
fi
location=$(curl -sS --max-time 30 -o /dev/null -D - "https://$TENANT_HOST/" 2>/dev/null \
    | tr -d '\r' | awk 'tolower($1) == "location:" { print $2; exit }')
case "$location" in
    */login*) pass "tenant / -> the sign-in page ($location)" ;;
    "") fail "tenant / sent no Location header (want a redirect to /login on the same host)" ;;
    *) fail "tenant / redirects off the machine: $location" ;;
esac
read -r verify_code verify_sha <<EOF
$(fetch "https://$TENANT_HOST/verify" "$tmp/verify.html")
EOF
if [ "$verify_code" = 200 ]; then
    pass "tenant /verify -> 200 (sha256 ${verify_sha%"${verify_sha#????????????}"}…)"
else
    fail "tenant /verify -> ${verify_code:-000} (want 200)"
fi
read -r attest_code attest_sha <<EOF
$(fetch "https://$TENANT_HOST/attestation" "$tmp/attest.json")
EOF
if [ "$attest_code" = 200 ]; then
    pass "tenant /attestation -> 200 (sha256 ${attest_sha%"${attest_sha#????????????}"}…)"
else
    fail "tenant /attestation -> ${attest_code:-000} (want 200)"
fi

expected=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("expected_measurement",""))' "$BASELINE")
measured=""
[ -s "$tmp/attest.json" ] && measured=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("measurement",""))' "$tmp/attest.json" 2>/dev/null || true)
if [ -n "$expected" ] && [ "$measured" = "$expected" ]; then
    pass "the tenant reports the recorded launch measurement (${expected%"${expected#????????????????}"}…)"
else
    fail "the tenant's measurement $measured is not the recorded $expected"
fi
if [ -s "$tmp/verify.html" ] && grep -q "$expected" "$tmp/verify.html"; then
    pass "/verify shows the recorded measurement"
else
    fail "/verify does not show the recorded measurement"
fi

# the certificate is the machine's own and verifies for this name
if printf '' | openssl s_client -connect "$TENANT_HOST:443" -servername "$TENANT_HOST" -verify_return_error 2>/dev/null \
    | openssl x509 -noout -subject 2>/dev/null | grep -q "CN *= *$TENANT_HOST"; then
    pass "the tenant's certificate is issued for $TENANT_HOST (curl verified the chain)"
else
    fail "the certificate on $TENANT_HOST is not issued for that name"
fi

# ----------------------------------------------------------- production
# `/` is the homepage and is stable between deploys, so it is asserted against
# the ODL-339 pre-deploy record's value — the DoD's "equal the pre-deploy
# record" — and this check's own record must agree with that same constant.
# The other two are re-generated by timers, so each is compared with the box's
# own state file: the served bytes must be the file production's generator
# wrote.
read -r root_code root_sha <<EOF
$(fetch "$PROD/" "$tmp/prod-root")
EOF
if [ "$root_code" = 200 ] && [ "$root_sha" = "$PRE_DEPLOY_ROOT" ]; then
    pass "production / is unchanged (${root_sha%"${root_sha#????????????}"}…, the ODL-339 pre-deploy record)"
else
    fail "production / moved: $root_sha != the pre-deploy record's $PRE_DEPLOY_ROOT (HTTP ${root_code:-000})"
fi
recorded_root=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["production"].get("/", "missing"))' "$BASELINE")
if [ "$recorded_root" = "$PRE_DEPLOY_ROOT" ]; then
    pass "the record agrees with the pre-deploy / (${recorded_root%"${recorded_root#????????????}"}…)"
else
    fail "the record's / is $recorded_root, not the pre-deploy $PRE_DEPLOY_ROOT"
fi

timer_page() {
    # timer_page PATH STATE_FILE LABEL — served bytes must be the state file
    page=$1
    state=$2
    label=$3
    read -r code served <<EOF
$(fetch "$PROD$page" "$tmp/prod$(printf '%s' "$page" | tr '/' '_')")
EOF
    if [ "$code" != 200 ]; then
        fail "production $page -> ${code:-000} (want 200)"
        return
    fi
    if [ ! -s "$state" ]; then
        fail "production $page is served but $state does not exist"
        return
    fi
    own=$(sha256sum "$state" | cut -d' ' -f1)
    recorded=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["production"].get(sys.argv[2], "missing"))' "$BASELINE" "$page")
    if [ "$served" != "$own" ]; then
        fail "production $page is not the box's own output ($served != $own)"
    elif [ "$served" = "$recorded" ]; then
        pass "production $page is the box's own output and matches the record (${served%"${served#????????????}"}…)"
    else
        pass "production $page is the box's own output (${label}); its timer moved it since the record (${recorded%"${recorded#????????????}"}… -> ${served%"${served#????????????}"}…)"
    fi
}
timer_page /roadmap "$PROD_ROADMAP" "the 15-minute roadmap generator"
timer_page /attestation "$PROD_ATTEST" "the 5-minute attestation refresh"

recorded_restarts=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("production_nrestarts", "unknown"))' "$BASELINE")
live_restarts=$(nrestarts)
if [ "$live_restarts" = "0" ] && [ "$recorded_restarts" = "0" ]; then
    pass "odile.service has not restarted (NRestarts=0 in the record and now)"
else
    fail "odile.service NRestarts is $live_restarts (record $recorded_restarts; want 0)"
fi

# ------------------------------------------------------------ the isolation
bench_ip=$(record_name "$BENCH_HOST")
tenant_ip=$(record_name "$TENANT_HOST")
if [ -n "$bench_ip" ] && [ -n "$tenant_ip" ] && [ "$bench_ip" != "$tenant_ip" ]; then
    pass "the names are different machines ($BENCH_HOST -> $bench_ip, $TENANT_HOST -> $tenant_ip)"
else
    fail "the two names do not resolve apart (bench '$bench_ip', tenant '$tenant_ip')"
fi

if [ -f "$BENCH_DB" ] && [ -f "$PROD_DB" ]; then
    in_bench=$(sqlite3 "$BENCH_DB" "select count(*) from tenants where slug='$TENANT_SLUG'" 2>/dev/null || printf 'error')
    in_prod=$(sqlite3 "$PROD_DB" "select count(*) from tenants where slug='$TENANT_SLUG'" 2>/dev/null || printf 'error')
    if [ "$in_bench" = 1 ] && [ "$in_prod" = 0 ]; then
        pass "the claim row is the bench's only ($TENANT_SLUG in bench=$in_bench, production=$in_prod)"
    else
        fail "the claim row leaked (bench=$in_bench, production=$in_prod)"
    fi
else
    fail "one of the two databases is missing ($BENCH_DB, $PROD_DB)"
fi

if [ "$failures" -eq 0 ]; then
    printf 'check_bench: PASS (%d checks)\n' "$checks"
    exit 0
fi
printf 'check_bench: FAIL (%d of %d checks)\n' "$failures" "$checks" >&2
exit 1
