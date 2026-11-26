#!/bin/bash
# install-stack.sh — install the Odile tenant stack into a root filesystem.
#
# One definition, used twice:
#   * deploy/image/Containerfile runs it while building the container image;
#   * deploy/image/mkdisk.sh copies this directory into a debootstrapped root
#     and runs it under chroot.
#
# What it installs:
#   * an unprivileged OS user (default `tenant`; no sudo, own home);
#   * Hermes Agent v0.23.0 (stable, commit 2bd378ed1b8d) as that user,
#     Webapp bundle built;
#   * the tenant config (Webapp on loopback 9130 behind the basic-auth gate)
#     and the .env allowlist (username, scrypt hash, signing secret only);
#   * one Hermes cron job, `bench-heartbeat`, every minute, no agent, no
#     inference;
#   * /etc/systemd/system/odile-agent.service, enabled at boot;
#   * with --guest-smoke: the bench smoke script and the oneshot unit that
#     runs it at boot and prints the result to the serial console.
#   * with --seed FILE --first-visit-token TOKEN (ODL-456, run at first boot
#     by odile-firstboot.service): the verified seed copied to
#     /etc/odile/seed.json 0600, the first-visit state (the token's SHA-256
#     only), the first-visit service and page, its unit, and its bench proxy.
#     A machine that already holds a first-visit state keeps it: a spent page
#     stays spent.
#
# Idempotency: a second run prints `same` for every piece and changes
# nothing. The generated .env keeps its scrypt hash and signing secret because
# a marker records what produced them (the bench password, the seed's verifier,
# or the fact that a discarded one did). A published image's first boot with a
# seed writes the seed's sign-in — the password chosen at registration — and
# this machine's own session secret; without a seed it writes a fresh discarded
# hash, until the first-visit page sets the owner's sign-in; after that the file
# is kept (ODL-521/ODL-522/ODL-925).
#
# The published image carries no password a stranger can use (ODL-521): its
# .env holds a scrypt hash of a discarded password, and the first-visit page is
# the only writer of a usable sign-in. With a seed, the install writes the
# seed's verifier instead (B-once, ODL-925) and stamps signin_set_at *after*
# that write, because the stamp means "the .env holds the owner's sign-in"
# (ODL-947). It carries no SSH host private key either (ODL-523): the build
# removes debootstrap's /etc/ssh/ssh_host_* and fails if a private one
# survives; the first boot makes this machine's own.
#
# Exit codes: 0 ok, 1 an apply failed, 2 bad input or a missing prerequisite.
set -euo pipefail
# The Hermes venv on a seat host puts its own python first; this script wants
# the distribution python and the distribution binaries, in the image and on
# the builder alike.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

TENANT_USER=${ODILE_TENANT_USER:-tenant}
EXPECTED_VERSION=${ODILE_HERMES_VERSION:-Hermes Agent v0.23.0}
# The stable channel moves; the bench and the demo host were verified against
# this commit, and `--channel stable` alone would silently install a newer
# patch (measured 2026-11-10: the tip was v0.23.1 and the shell check failed).
HERMES_COMMIT=${ODILE_HERMES_COMMIT:-2bd378ed1b8d}
BENCH_PASSWORD=${ODILE_BENCH_PASSWORD:-odile-bench-not-a-secret}
WEBAPP_HOST=${ODILE_WEBAPP_HOST:-127.0.0.1}
WEBAPP_PORT=${ODILE_WEBAPP_PORT:-9130}
INSTALLER_URL=${ODILE_INSTALLER_URL:-https://hermes-agent.nousresearch.com/install.sh}
SERVICE_NAME=odile-agent
BENCH_LIB=/usr/local/lib/odile-bench
STATE_DIR=/var/lib/odile-bench

GUEST_SMOKE=0
SEED_FILE=""
FIRST_VISIT_TOKEN=""
FIRST_VISIT_PORT=${ODILE_FIRST_VISIT_PORT:-8091}
# The machine's own /verify page (t_4fcd465f): its own loopback program, root,
# Caddy (or the bench stand-in) is the listener.
VERIFY_PORT=${ODILE_VERIFY_PORT:-8098}
MANIFEST_PORT=${ODILE_MANIFEST_PORT:-8012}

usage() {
    cat <<'EOF'
usage: install-stack.sh [--user NAME] [--password PW] [--webapp-host HOST]
                        [--webapp-port PORT] [--guest-smoke]
                        [--seed FILE --first-visit-token TOKEN]
                        [--first-visit-port PORT]

  --user NAME       unprivileged tenant account (default: tenant, $ODILE_TENANT_USER)
  --password PW     bench Webapp password (default: a public bench fixture,
                    $ODILE_BENCH_PASSWORD). Only its scrypt hash is written,
                    and only into a bench image: a published image's .env
                    holds a discarded password's hash (ODL-521).
  --webapp-host H   address the Webapp binds (default: 127.0.0.1). The
                    container passes 0.0.0.0 because its network namespace is
                    the boundary; a real tenant stays on loopback behind Caddy.
  --webapp-port P   Webapp port (default: 9130)
  --guest-smoke     also install the boot-time smoke unit that prints its
                    result to the serial console (mkdisk.sh uses this).
  --seed FILE       a verified seed (the first boot's /etc/odile/seed.json);
                    store it 0600 and install the first-visit page for TOKEN.
  --first-visit-token TOKEN
                    the one-time token the first-visit page checks (64 hex).
                    Required with --seed; only its SHA-256 is written.
  --first-visit-port P
                    the loopback port the first-visit page binds (default 8091)
EOF
}

say() { printf '%-12s %s\n' "$1" "$2"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --user) TENANT_USER=$2; shift 2 ;;
        --password) BENCH_PASSWORD=$2; shift 2 ;;
        --webapp-host) WEBAPP_HOST=$2; shift 2 ;;
        --webapp-port) WEBAPP_PORT=$2; shift 2 ;;
        --guest-smoke) GUEST_SMOKE=1; shift ;;
        --seed) SEED_FILE=$2; shift 2 ;;
        --first-visit-token) FIRST_VISIT_TOKEN=$2; shift 2 ;;
        --first-visit-port) FIRST_VISIT_PORT=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install-stack: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

command -v useradd >/dev/null 2>&1 || { echo "install-stack: useradd is missing (passwd package)" >&2; exit 2; }
command -v curl >/dev/null 2>&1 || { echo "install-stack: curl is missing" >&2; exit 2; }
command -v git >/dev/null 2>&1 || { echo "install-stack: git is missing" >&2; exit 2; }
command -v runuser >/dev/null 2>&1 || { echo "install-stack: runuser is missing (util-linux)" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "install-stack: must run as root" >&2; exit 2; }
[ -f "$HERE/tenant.service" ] || { echo "install-stack: $HERE/tenant.service is missing" >&2; exit 2; }
[ -f "$HERE/config.yaml" ] || { echo "install-stack: $HERE/config.yaml is missing" >&2; exit 2; }
[ -f "$HERE/bench-heartbeat.sh" ] || { echo "install-stack: $HERE/bench-heartbeat.sh is missing" >&2; exit 2; }

# The first-boot apply (ODL-456) is two pieces that only make sense together:
# the verified seed, and the one-time token the page it enables checks.
if [ -n "$FIRST_VISIT_TOKEN" ] && [ -z "$SEED_FILE" ]; then
    echo "install-stack: --first-visit-token needs --seed (the verified seed)" >&2
    exit 2
fi
if [ -n "$SEED_FILE" ]; then
    [ -s "$SEED_FILE" ] || { echo "install-stack: no seed at $SEED_FILE" >&2; exit 2; }
    [ -n "$FIRST_VISIT_TOKEN" ] || {
        echo "install-stack: --seed needs --first-visit-token (the page's one-time token)" >&2
        exit 2
    }
    printf '%s' "$FIRST_VISIT_TOKEN" | grep -Eq '^[0-9a-f]{64}$' || {
        echo "install-stack: --first-visit-token must be 64 lowercase hex" >&2
        exit 2
    }
fi

# --------------------------------------------------------------------------
# 1. the unprivileged tenant account

if id "$TENANT_USER" >/dev/null 2>&1; then
    TENANT_HOME=$(getent passwd "$TENANT_USER" | cut -d: -f6)
    say same "$TENANT_USER exists (uid $(id -u "$TENANT_USER"), home $TENANT_HOME)"
else
    useradd --create-home --user-group --shell /bin/bash "$TENANT_USER"
    TENANT_HOME=$(getent passwd "$TENANT_USER" | cut -d: -f6)
    say create "$TENANT_USER (uid $(id -u "$TENANT_USER"), home $TENANT_HOME)"
fi
[ -n "$TENANT_HOME" ] && [ -d "$TENANT_HOME" ] || {
    echo "install-stack: $TENANT_USER has no home directory" >&2
    exit 1
}
HERMES_HOME=$TENANT_HOME/.hermes
INSTALL_DIR=$HERMES_HOME/hermes-agent
LAUNCHER=$INSTALL_DIR/.hermes/bin/hermes
WEBAPP_INDEX=$INSTALL_DIR/apps/desktop/dist-webapp/index.html

as_tenant() {
    runuser -u "$TENANT_USER" -- env HOME="$TENANT_HOME" \
        HERMES_HOME="$HERMES_HOME" "$@"
}

# --------------------------------------------------------------------------
# 2. Hermes, pinned to the release the demo host runs

installed_version() {
    [ -x "$LAUNCHER" ] || return 1
    as_tenant "$LAUNCHER" --version 2>/dev/null | head -n 1
}

VERSION_LINE=$(installed_version || true)
if [ -n "$VERSION_LINE" ] && [ -d "$INSTALL_DIR/.git" ]; then
    say same "$INSTALL_DIR"
    say same "$VERSION_LINE"
    case "$VERSION_LINE" in
        "$EXPECTED_VERSION"*) ;;
        *)
            echo "install-stack: warning: expected $EXPECTED_VERSION; the Webapp gate and" >&2
            echo "              config schema were verified against that release" >&2
            ;;
    esac
else
    installer=$(mktemp)
    trap 'rm -f "$installer"' EXIT
    curl -fsSL --max-time 120 "$INSTALLER_URL" -o "$installer"
    chmod 0644 "$installer"
    say install "Hermes as $TENANT_USER into $HERMES_HOME (stable, browser and cua skipped)"
    as_tenant bash "$installer" --channel stable --commit "$HERMES_COMMIT" \
        --non-interactive --skip-browser --skip-computer-use
    VERSION_LINE=$(installed_version || true)
    [ -n "$VERSION_LINE" ] || { echo "install-stack: Hermes install produced no launcher" >&2; exit 1; }
    say installed "$VERSION_LINE"
    trap - EXIT
    rm -f "$installer"
fi

if [ -f "$WEBAPP_INDEX" ]; then
    say same "Webapp bundle $WEBAPP_INDEX"
else
    say build "Webapp bundle (first build only; minutes)"
    as_tenant "$LAUNCHER" webapp --build-only
    [ -f "$WEBAPP_INDEX" ] || { echo "install-stack: webapp --build-only produced no bundle" >&2; exit 1; }
    say built "Webapp bundle"
fi

# The demo host's unit puts the installer's node first on PATH so a Webapp
# terminal has it. Find the directory the installer actually created rather
# than pinning a version that a future stable release will move.
TENANT_PATH=$TENANT_HOME/.local/bin
NODE_BIN=$(find "$HERMES_HOME/tools" -maxdepth 2 -type d -name 'node-*-linux-x64' -print -quit 2>/dev/null || true)
if [ -n "$NODE_BIN" ]; then
    TENANT_PATH=$NODE_BIN/bin:$TENANT_PATH
    say same "node on the tenant PATH ($NODE_BIN/bin)"
fi

# --------------------------------------------------------------------------
# 3. config.yaml — the seven managed blocks (the installer's tenant template
#    carries all seven; a bench that dropped one would not serve the profile a
#    tenant gets), and the schema version Hermes itself seeded, so a future
#    schema bump does not need an edit here.

if [ -f "$HERMES_HOME/config.yaml" ]; then
    CONFIG_VERSION=$(sed -n 's/^_config_version:[[:space:]]*//p' "$HERMES_HOME/config.yaml" | head -n 1)
else
    CONFIG_VERSION=""
fi
: "${CONFIG_VERSION:=52}"
config_tmp=$(mktemp "$HERMES_HOME/.config.yaml.XXXXXX")
{
    printf '_config_version: %s\n' "$CONFIG_VERSION"
    sed -e "s|__WEBAPP_HOST__|$WEBAPP_HOST|g" -e "s|__WEBAPP_PORT__|$WEBAPP_PORT|g" "$HERE/config.yaml"
} > "$config_tmp"
if [ -f "$HERMES_HOME/config.yaml" ] && cmp -s "$config_tmp" "$HERMES_HOME/config.yaml"; then
    say same "$HERMES_HOME/config.yaml"
    rm -f "$config_tmp"
else
    chown "$TENANT_USER:$TENANT_USER" "$config_tmp"
    chmod 0644 "$config_tmp"
    mv "$config_tmp" "$HERMES_HOME/config.yaml"
    say write "$HERMES_HOME/config.yaml"
fi

# --------------------------------------------------------------------------
# 4. .env — the three auth keys, never a provider key. The bench never calls
#    inference, so no NEAR key is copied into the image.
#
#    ODL-521: the published image ships no usable sign-in. A production build
#    (sealed-image.sh --production writes /etc/odile/production before this
#    script runs) writes a scrypt hash of a password generated here and
#    discarded, with the marker that says so; the bench build keeps the
#    documented fixture password its QEMU smoke signs in with.
#
#    ODL-522: the session-signing secret is the machine's, not the image's. A
#    build has to write *some* secret, and every disk built from one image
#    would then sign its cookies with the same value; so the first boot of a
#    published image (the run that carries --seed) replaces it with a secret
#    generated on this machine, together with a fresh discarded hash. Once the
#    first-visit page has written the owner's sign-in (signin_set_at in
#    /etc/odile/first-visit.json) the file is kept byte for byte: a reboot
#    must not rotate the secret, or every session cookie the owner holds dies.
#
#    The marker is what makes a rerun idempotent with a random value: the
#    bench marker records which bench password produced the hash, and the
#    production marker records that the hash belongs to a discarded password.

password_sha=$(printf '%s' "$BENCH_PASSWORD" | sha256sum | cut -d' ' -f1)
env_file=$HERMES_HOME/.env

scrypt_hash() {  # $1 = password; the provider's scrypt encoding
    /usr/bin/python3 - "$1" <<'PY'
import base64, hashlib, secrets, sys
password = sys.argv[1].encode("utf-8")
salt = secrets.token_bytes(16)
dk = hashlib.scrypt(password, salt=salt, n=2**14, r=8, p=1, dklen=32, maxmem=0)
print("scrypt$16384$8$1$%s$%s" % (
    base64.b64encode(salt).decode(), base64.b64encode(dk).decode()))
PY
}

discarded_scrypt_hash() {  # a hash of a password generated and thrown away
    /usr/bin/python3 - <<'PY'
import base64, hashlib, secrets
password = secrets.token_hex(32).encode("ascii")
salt = secrets.token_bytes(16)
dk = hashlib.scrypt(password, salt=salt, n=2**14, r=8, p=1, dklen=32, maxmem=0)
print("scrypt$16384$8$1$%s$%s" % (
    base64.b64encode(salt).decode(), base64.b64encode(dk).decode()))
PY
}

fresh_secret() {
    /usr/bin/python3 -c 'import secrets; print(secrets.token_hex(32))'
}

first_visit_signin_set() {  # 0 once a writer of the owner's sign-in recorded it
    [ -f /etc/odile/first-visit.json ] || return 1
    /usr/bin/python3 - /etc/odile/first-visit.json <<'PY'
import json, sys
try:
    state = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if isinstance(state, dict) and state.get("signin_set_at") else 1)
PY
}

first_visit_owner_visited() {  # 0 once the page or a deploy recorded the owner's use
    # `used_at` (the classic setup and a deploy) or `first_login_at` (the
    # seeded login) is the record that a hash the .env carries is the owner's
    # own, not a placeholder. A reset keeps it, so a manual re-run of this
    # script cannot revert a password the owner chose.
    [ -f /etc/odile/first-visit.json ] || return 1
    /usr/bin/python3 - /etc/odile/first-visit.json <<'PY'
import json, sys
try:
    state = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
visited = isinstance(state, dict) and (state.get("used_at") or state.get("first_login_at"))
raise SystemExit(0 if visited else 1)
PY
}

env_holds_signin() {  # 0 when $env_file already carries exactly this hash
    [ -f "$env_file" ] || return 1
    local current
    current=$(grep -m1 -F 'HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=' "$env_file" \
        | cut -d= -f2-)
    [ "$current" = "$1" ]
}

seed_signin_hash() {  # the seed's scrypt verifier, or exit 2 with a reason
    /usr/bin/python3 - "$SEED_FILE" <<'PY'
import json, re, sys
try:
    seed = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError) as exc:
    print("install-stack: the seed is not JSON: %s" % exc, file=sys.stderr)
    raise SystemExit(2)
value = seed.get("sign_in_hash") if isinstance(seed, dict) else None
if not isinstance(value, str) or not re.match(
        r"^scrypt\$\d+\$\d+\$\d+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+$", value):
    print("install-stack: the seed has no installer-format sign_in_hash", file=sys.stderr)
    raise SystemExit(2)
print(value)
PY
}

stamp_signin_state() {  # 0 stamped, 1 already set (the file is left alone)
    local state=/etc/odile/first-visit.json tmp rc=0
    [ -f "$state" ] || return 1
    tmp=$(mktemp)
    /usr/bin/python3 - "$state" > "$tmp" <<'PY' || rc=$?
import json, sys
from datetime import datetime, timezone
state = json.load(open(sys.argv[1], encoding="utf-8"))
if not isinstance(state, dict):
    raise SystemExit("install-stack: the first-visit state is not an object")
if state.get("signin_set_at"):
    raise SystemExit(3)
state["signin_set_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
print(json.dumps(state, indent=2, sort_keys=True))
PY
    if [ "$rc" -ne 0 ]; then
        rm -f "$tmp"
        [ "$rc" -eq 3 ] && return 1
        echo "install-stack: cannot read $state" >&2
        return 2
    fi
    install -m 0600 "$tmp" "$state"
    rm -f "$tmp"
    return 0
}

bench_password_verifies() {  # 0 when this .env's hash accepts the bench fixture
    [ -f "$env_file" ] || return 1
    /usr/bin/python3 - "$env_file" "$BENCH_PASSWORD" <<'PY'
import base64, hashlib, sys
path, password = sys.argv[1], sys.argv[2]
value = ""
with open(path, encoding="utf-8") as handle:
    for line in handle:
        if line.startswith("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH="):
            value = line.rstrip("\n").split("=", 1)[1]
            break
parts = value.split("$")
if len(parts) != 6 or parts[0] != "scrypt":
    raise SystemExit(1)
try:
    n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
    salt = base64.b64decode(parts[4], validate=True)
    expected = base64.b64decode(parts[5], validate=True)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                             dklen=len(expected), maxmem=0)
except (ValueError, OSError):
    raise SystemExit(1)
raise SystemExit(0 if derived == expected else 1)
PY
}

write_env() {  # $1 marker line, $2 hash, $3 secret, $4 what
    local marker=$1 hash=$2 secret=$3 what=$4 env_tmp
    env_tmp=$(mktemp "$HERMES_HOME/.env.XXXXXX")
    cat > "$env_tmp" <<EOF
# Odile tenant .env — generated by deploy/image/install-stack.sh, never edited
# by hand. Exactly three keys: the owner's sign-in and the session signing
# secret. No provider key: the inference credential is in /etc/odile/key.env.
$marker
HERMES_DASHBOARD_BASIC_AUTH_USERNAME=$TENANT_USER
HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=$hash
HERMES_DASHBOARD_BASIC_AUTH_SECRET=$secret
EOF
    chown "$TENANT_USER:$TENANT_USER" "$env_tmp"
    chmod 0600 "$env_tmp"
    mv "$env_tmp" "$env_file"
    say write "$env_file (mode 0600, $TENANT_USER; $what)"
}

# B-once (ODL-925): when the first boot carries a seed, its sign_in_hash is
# the owner's sign-in — the password chosen at registration — and this block
# writes it instead of a discarded placeholder.
SEED_SIGNIN=""
if [ -n "$SEED_FILE" ]; then
    SEED_SIGNIN=$(seed_signin_hash)
fi
# Set when this run ends with the seed's verifier in the .env, so the state's
# stamp below follows the write and never precedes it (ODL-947).
ENV_HAS_SEED_SIGNIN=0

if [ -f /etc/odile/production ]; then
    # The published image: no usable sign-in in the bytes, and no rewrite of a
    # hash that is already unusable. The Hermes bootstrap leaves its own
    # .env.example copy behind, so "exists" is not enough: the file must carry
    # a scrypt hash, and that hash must not accept the public fixture. The
    # release installer's --first-visit write is what replaces the placeholder
    # on every run until the page sets the owner's; install-stack only repairs
    # a file that still accepts the bench fixture (the bench-disk-as-published
    # case) or has no usable hash at all.
    if [ ! -f "$env_file" ] \
            || ! grep -q '^HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=scrypt[$]' "$env_file" \
            || bench_password_verifies; then
        if [ -n "$SEED_SIGNIN" ]; then
            write_env "# odile-signin-seeded: 1" "$SEED_SIGNIN" "$(fresh_secret)" \
                "the password chosen at registration (the seed's verifier)"
            ENV_HAS_SEED_SIGNIN=1
        else
            write_env "# odile-signin-placeholder: 1" "$(discarded_scrypt_hash)" \
                "$(fresh_secret)" "a discarded sign-in, no usable password"
        fi
    elif [ -n "$SEED_SIGNIN" ]; then
        # ODL-925/ODL-947: the seeded first boot. The seed's verifier is the
        # owner's sign-in and this machine still generates its own session
        # secret (ODL-522: the build's secret is shared by every disk from the
        # image). The state is *not* trusted here: release 2026.11.23-5 stamped
        # signin_set_at before the installer ran, so a stamp can say the
        # sign-in is set while this file still holds the image's discarded
        # hash — and keeping the file then served a login nobody could pass.
        # Write when the .env does not already hold the seed's verifier, and
        # keep a hash the owner's own visit recorded (a page setup, a reset, a
        # deploy set used_at or first_login_at).
        if env_holds_signin "$SEED_SIGNIN"; then
            ENV_HAS_SEED_SIGNIN=1
            say same "$env_file (the seed's verifier; this machine's own secret)"
        elif first_visit_signin_set && first_visit_owner_visited; then
            say same "$env_file (the owner's sign-in; this machine's own secret)"
        else
            write_env "# odile-signin-seeded: 1" "$SEED_SIGNIN" "$(fresh_secret)" \
                "first boot: the password chosen at registration, this machine's own secret"
            ENV_HAS_SEED_SIGNIN=1
        fi
    elif first_visit_signin_set; then
        say same "$env_file (the owner's sign-in; this machine's own secret)"
    elif [ -n "$SEED_FILE" ]; then
        # ODL-522: the first boot of a published image. The build's secret is
        # shared by every disk built from that image, so this machine generates
        # its own (a retried first boot before the visit mints again; no
        # session can exist yet, because the page has not set the sign-in).
        write_env "# odile-signin-placeholder: 1" "$(discarded_scrypt_hash)" \
            "$(fresh_secret)" "first boot: a discarded sign-in, this machine's own secret"
    else
        say same "$env_file (a discarded sign-in, no usable password)"
    fi
elif [ -f "$env_file" ] && grep -qx "# bench-password-sha256: $password_sha" "$env_file"; then
    say same "$env_file (bench password unchanged)"
else
    # The bench image: the documented fixture password its QEMU smoke signs in
    # with. It is public on purpose and never reaches a tenant.
    write_env "# bench-password-sha256: $password_sha" "$(scrypt_hash "$BENCH_PASSWORD")" \
        "$(fresh_secret)" "the bench fixture password"
fi

# The stamp follows the write (ODL-947): `signin_set_at` means "the .env holds
# the owner's sign-in" (docs/security/DECISION-first-visit-state.md §1), so a
# seeded run records it only when the file above carries the seed's verifier.
# A state that already carries the timestamp is left alone (idempotent).
if [ "$ENV_HAS_SEED_SIGNIN" -eq 1 ] && [ -f /etc/odile/first-visit.json ] \
        && stamp_signin_state; then
    say stamp "/etc/odile/first-visit.json (signin_set_at: the .env holds the seed's verifier)"
fi
install -d -o "$TENANT_USER" -g "$TENANT_USER" -m 0700 "$HERMES_HOME/scripts" "$HERMES_HOME/cron"

# --------------------------------------------------------------------------
# 4b. SSH host keys — generated on the machine, never shipped in the image.
#
#    ODL-523: debootstrap's openssh-server postinst writes
#    /etc/ssh/ssh_host_*_key at build time, so every disk built from the image
#    presented the same private host keys. The build removes them and asserts
#    none is left; the first boot makes this machine's own with `ssh-keygen -A`.
#
#    Debian's sshd-keygen.service is the unit meant to do that, but it carries
#    ConditionFirstBoot=yes and this image ships a machine-id, so on a cloned
#    disk the condition is false and it never fires. The drop-in clears the
#    condition: ssh-keygen -A leaves existing keys alone, and the unit is
#    ordered before ssh.service, so sshd -t never runs against a keyless
#    /etc/ssh.
#
#    `--seed` is the first boot (odile-firstboot.py has already run
#    ssh-keygen -A through ensure_host_keys, so this prints `same`); without it
#    this is the build, which removes and then asserts.

SSH_KEYGEN_DROPIN=/etc/systemd/system/sshd-keygen.service.d/10-odile-hostkeys.conf
install -d -m 0755 "${SSH_KEYGEN_DROPIN%/*}"
dropin_tmp=$(mktemp)
cat > "$dropin_tmp" <<'EOF'
# Odile: the image ships no private SSH host keys (ODL-523). The first boot
# generates this machine's own; there is no first boot left for
# ConditionFirstBoot to detect once the image is cloned, so clear it.
# ssh-keygen -A leaves existing keys alone, and this unit is Before=ssh.service.
[Unit]
ConditionFirstBoot=
EOF
if cmp -s "$dropin_tmp" "$SSH_KEYGEN_DROPIN"; then
    say same "$SSH_KEYGEN_DROPIN"
    rm -f "$dropin_tmp"
else
    chmod 0644 "$dropin_tmp"
    mv "$dropin_tmp" "$SSH_KEYGEN_DROPIN"
    say write "$SSH_KEYGEN_DROPIN (ssh-keygen -A before sshd -t)"
fi

if [ -n "$SEED_FILE" ]; then
    if [ -f /etc/ssh/ssh_host_ed25519_key ]; then
        say same "the SSH host keys (this machine's own)"
    elif ssh-keygen -A >/dev/null 2>&1; then
        say create "the SSH host keys (ssh-keygen -A)"
    else
        say warn "ssh-keygen -A failed; sshd-keygen.service retries it at the next ssh start"
    fi
else
    shipped=0
    for key in /etc/ssh/ssh_host_*; do
        [ -e "$key" ] || continue
        rm -f "$key"
        shipped=1
    done
    if [ "$shipped" = 1 ]; then
        say remove "the debootstrap SSH host keys (/etc/ssh/ssh_host_*)"
    else
        say same "no SSH host key in the image"
    fi
    if compgen -G "/etc/ssh/ssh_host_*_key" >/dev/null; then
        echo "install-stack: the image still carries an SSH host private key" >&2
        exit 1
    fi
fi

# --------------------------------------------------------------------------
# 5. one cron job, no agent: the demo's `demo-heartbeat`, every minute so the
#    bench check is bounded. Its stdout is the job's stored output.

install -o "$TENANT_USER" -g "$TENANT_USER" -m 0755 \
    "$HERE/bench-heartbeat.sh" "$HERMES_HOME/scripts/bench-heartbeat.sh"
# v0.23.0's `cron list` has no --json flag; the job store is the stable
# surface. A rerun must not create a second job (the bench fires it every
# minute, so duplicates would double the log lines and the evidence).
cron_job_exists() {
    [ -s "$HERMES_HOME/cron/jobs.json" ] || return 1
    /usr/bin/python3 - "$HERMES_HOME/cron/jobs.json" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
jobs = data.get("jobs", []) if isinstance(data, dict) else data
matches = [j for j in jobs if isinstance(j, dict) and j.get("name") == "bench-heartbeat"]
raise SystemExit(0 if matches else 1)
PY
}
if cron_job_exists; then
    say same "cron job bench-heartbeat (every 1m, no-agent)"
else
    as_tenant "$LAUNCHER" cron create 1m \
        --name bench-heartbeat --no-agent --script bench-heartbeat.sh --deliver local
    say create "cron job bench-heartbeat (every 1m, no-agent)"
fi

# --------------------------------------------------------------------------
# 6. the service unit

unit_tmp=$(mktemp)
sed -e "s|__TENANT_USER__|$TENANT_USER|g" \
    -e "s|__TENANT_HOME__|$TENANT_HOME|g" \
    -e "s|__TENANT_PATH__|$TENANT_PATH|g" \
    "$HERE/tenant.service" > "$unit_tmp"
if cmp -s "$unit_tmp" "/etc/systemd/system/$SERVICE_NAME.service"; then
    say same "/etc/systemd/system/$SERVICE_NAME.service"
    rm -f "$unit_tmp"
else
    chmod 0644 "$unit_tmp"
    mv "$unit_tmp" "/etc/systemd/system/$SERVICE_NAME.service"
    say write "/etc/systemd/system/$SERVICE_NAME.service"
fi
systemctl enable "$SERVICE_NAME.service" >/dev/null
say same "$SERVICE_NAME.service enabled at boot"

# --------------------------------------------------------------------------
# 6b. the Webapp surface re-assert (ODL-503 follow-up, ODL-500 chrome)
#
# The installer links the Odile brand once, at install time (ODL-240), and the
# ODL-500 chrome (paper, the embedded Cormorant Garamond subset, the Verify
# link) was only ever part of the HQ demo. A `hermes update` rebuilds
# dist-webapp from the vendor package, and that rebuild drops the asset, the
# tag, the chrome stylesheet and its tag: measured on the demo tenant,
# 2026-11-14 01:26Z, title `Hermes` and wordmark `HERMES AGENT`; and again at
# 09:01Z, paper `--paper-v0` #F6F0E4 and the system serif stack. These pieces
# make the re-assert part of the machine instead of part of a repair: the
# bytes, the helper, a path unit on the built shell and its three assets, and
# a drop-in that runs the same helper before the gateway starts.
#
# The trigger is the bundle changing (the path unit), not a boot; the drop-in
# is the backstop for the update flow that rebuilds and then restarts the
# gateway. A drop-in, not an ExecStartPre in tenant.service, because the
# release installer writes odile-agent.service on its first run (and on
# --force) and would drop an ExecStartPre written into it.
BRAND_LIB=/usr/local/lib/odile
install -d -m 0755 "$BRAND_LIB"
for pair in "webapp-brand.js:0644" "webapp-chrome.css:0644" \
            "webapp-chrome.js:0644" "odile-webapp-brand.py:0755"; do
    f=${pair%%:*}; mode=${pair##*:}
    if cmp -s "$HERE/$f" "$BRAND_LIB/$f"; then
        say same "$BRAND_LIB/$f"
    else
        install -m "$mode" "$HERE/$f" "$BRAND_LIB/$f"
        say write "$BRAND_LIB/$f"
    fi
done
for unit in odile-webapp-brand.service odile-webapp-brand.path; do
    unit_tmp=$(mktemp)
    sed -e "s|__TENANT_HOME__|$TENANT_HOME|g" "$HERE/$unit" > "$unit_tmp"
    if cmp -s "$unit_tmp" "/etc/systemd/system/$unit"; then
        say same "/etc/systemd/system/$unit"
        rm -f "$unit_tmp"
    else
        chmod 0644 "$unit_tmp"
        mv "$unit_tmp" "/etc/systemd/system/$unit"
        say write "/etc/systemd/system/$unit"
    fi
done
BRAND_DROPIN=/etc/systemd/system/$SERVICE_NAME.service.d/10-odile-webapp-brand.conf
install -d -m 0755 "${BRAND_DROPIN%/*}"
unit_tmp=$(mktemp)
sed -e "s|__TENANT_HOME__|$TENANT_HOME|g" "$HERE/odile-webapp-brand.conf" > "$unit_tmp"
if cmp -s "$unit_tmp" "$BRAND_DROPIN"; then
    say same "$BRAND_DROPIN"
    rm -f "$unit_tmp"
else
    chmod 0644 "$unit_tmp"
    mv "$unit_tmp" "$BRAND_DROPIN"
    say write "$BRAND_DROPIN"
fi
systemctl enable odile-webapp-brand.path >/dev/null
say same "odile-webapp-brand.path enabled (a rebuild starts it)"
# Converge this build now, so a fresh machine leaves the image with the name
# and the paper on it and does not wait for the first rebuild. In a build
# chroot there is no PID 1 to ask; the guest gets the unit started below.
if ! /usr/bin/python3 "$BRAND_LIB/odile-webapp-brand.py" --home "$TENANT_HOME"; then
    say warn "the Webapp surface re-assert failed; the path unit will retry"
fi
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
    systemctl start odile-webapp-brand.path
    say start "odile-webapp-brand.path (bundle watches armed)"
fi

# --------------------------------------------------------------------------
# 7. optional: the boot-time smoke unit (QEMU image only)

install -d -m 0755 "$BENCH_LIB" "$STATE_DIR"
for f in bench.py smoke.sh; do
    install -m 0755 "$HERE/$f" "$BENCH_LIB/$f"
done
# The fixture password in the clear is a bench artefact; a published image
# ships no plaintext password at all (ODL-521).
if [ ! -f /etc/odile/production ]; then
    printf '%s\n' "$BENCH_PASSWORD" > "$STATE_DIR/password"
    chmod 0600 "$STATE_DIR/password"
fi

if [ "$GUEST_SMOKE" = 1 ]; then
    unit_tmp=$(mktemp)
    sed -e "s|__TENANT_USER__|$TENANT_USER|g" \
        -e "s|__TENANT_HOME__|$TENANT_HOME|g" \
        -e "s|__BENCH_PASSWORD__|$BENCH_PASSWORD|g" \
        -e "s|__WEBAPP_PORT__|$WEBAPP_PORT|g" \
        "$HERE/odile-bench-smoke.service" > "$unit_tmp"
    if cmp -s "$unit_tmp" /etc/systemd/system/odile-bench-smoke.service; then
        say same "/etc/systemd/system/odile-bench-smoke.service"
        rm -f "$unit_tmp"
    else
        chmod 0644 "$unit_tmp"
        mv "$unit_tmp" /etc/systemd/system/odile-bench-smoke.service
        say write "/etc/systemd/system/odile-bench-smoke.service"
    fi
    systemctl enable odile-bench-smoke.service >/dev/null
    say same "odile-bench-smoke.service enabled at boot"

    # The Caddy stand-in: the host-forwarded port reaches the guest's slirp
    # address; socket-proxyd hands the connection to the loopback Webapp.
    for unit in odile-bench-proxy.socket odile-bench-proxy.service; do
        unit_tmp=$(mktemp)
        sed -e "s|__WEBAPP_PORT__|$WEBAPP_PORT|g" "$HERE/$unit" > "$unit_tmp"
        if cmp -s "$unit_tmp" "/etc/systemd/system/$unit"; then
            say same "/etc/systemd/system/$unit"
            rm -f "$unit_tmp"
        else
            chmod 0644 "$unit_tmp"
            mv "$unit_tmp" "/etc/systemd/system/$unit"
            say write "/etc/systemd/system/$unit"
        fi
    done
    systemctl enable odile-bench-proxy.socket >/dev/null
    say same "odile-bench-proxy.socket enabled at boot"

    # The first-visit page has its own loopback port (ODL-457); the bench's
    # stand-in Caddy needs a socket on the slirp address for it as well, or the
    # host-forwarded connection would reach nothing. Installed here (build
    # time); the page itself is installed by the first boot (step 8).
    for unit in odile-bench-proxy-firstvisit.socket odile-bench-proxy-firstvisit.service; do
        unit_tmp=$(mktemp)
        sed -e "s|__FIRST_VISIT_PORT__|$FIRST_VISIT_PORT|g" "$HERE/$unit" > "$unit_tmp"
        if cmp -s "$unit_tmp" "/etc/systemd/system/$unit"; then
            say same "/etc/systemd/system/$unit"
            rm -f "$unit_tmp"
        else
            chmod 0644 "$unit_tmp"
            mv "$unit_tmp" "/etc/systemd/system/$unit"
            say write "/etc/systemd/system/$unit"
        fi
    done
    systemctl enable odile-bench-proxy-firstvisit.socket >/dev/null
    say same "odile-bench-proxy-firstvisit.socket enabled at boot"

    # The machine's own /verify page has its own loopback port (t_4fcd465f);
    # the bench's stand-in Caddy needs a socket on the slirp address for it as
    # well. Installed at build time; the page program is installed by step 7b.
    for unit in odile-bench-proxy-verify.socket odile-bench-proxy-verify.service; do
        unit_tmp=$(mktemp)
        sed -e "s|__VERIFY_PORT__|$VERIFY_PORT|g" "$HERE/$unit" > "$unit_tmp"
        if cmp -s "$unit_tmp" "/etc/systemd/system/$unit"; then
            say same "/etc/systemd/system/$unit"
            rm -f "$unit_tmp"
        else
            chmod 0644 "$unit_tmp"
            mv "$unit_tmp" "/etc/systemd/system/$unit"
            say write "/etc/systemd/system/$unit"
        fi
    done
    systemctl enable odile-bench-proxy-verify.socket >/dev/null
    say same "odile-bench-proxy-verify.socket enabled at boot"

    # The manifest listener binds loopback too (ODL-472); the host reads
    # /manifest through this stand-in for Caddy, exactly as it reads the
    # first-visit page. Installed at build time; the first boot starts the
    # listener itself once the signed pair is in place.
    for unit in odile-bench-proxy-manifest.socket odile-bench-proxy-manifest.service; do
        unit_tmp=$(mktemp)
        sed -e "s|__MANIFEST_PORT__|$MANIFEST_PORT|g" "$HERE/$unit" > "$unit_tmp"
        if cmp -s "$unit_tmp" "/etc/systemd/system/$unit"; then
            say same "/etc/systemd/system/$unit"
            rm -f "$unit_tmp"
        else
            chmod 0644 "$unit_tmp"
            mv "$unit_tmp" "/etc/systemd/system/$unit"
            say write "/etc/systemd/system/$unit"
        fi
    done
    systemctl enable odile-bench-proxy-manifest.socket >/dev/null
    say same "odile-bench-proxy-manifest.socket enabled at boot"
fi

# --------------------------------------------------------------------------
# 7b. the machine's own attestation and access report (ODL-397, ODL-473)
#
# The /verify page reads two files this step's units write: the served
# attestation document (from Servury's boot report, or the bench stand-in
# derived from the image's own pin) and the access report (the strict bar's
# five lines). The units run before the gateway on every boot; here they run
# once so a machine that just installed has both. The page itself is installed
# in two places: the image root has it from the build, and the first boot
# installs it with the seed (step 8).

install_file() {  # src dst mode label
    if cmp -s "$1" "$2" 2>/dev/null; then
        say same "$2"
    else
        install -m "$3" "$1" "$2"
        say write "$2"
    fi
}

if [ ! -f "$HERE/verify.html" ] || [ ! -f "$HERE/verify.py" ] \
        || [ ! -f "$HERE/access_report.sh" ] \
        || [ ! -f "$HERE/verify_and_unlock.sh" ]; then
    say skip "the /verify report surface (ODL-473 is not in this image)"
else
    install -d -m 0755 /usr/local/lib/odile
    # The units below run with ProtectSystem=strict and ReadWritePaths=/var/lib/odile;
    # systemd refuses to start them (226/NAMESPACE) when that path does not exist.
    # It is created here, in the image build's chroot and on a machine whose first
    # boot installed later than the units' own start, so the first run is not a race.
    install -d -m 0755 /var/lib/odile
    # The release installer creates this group too; creating it here means the
    # manifest listener's SupplementaryGroups=odile-manifest resolves before the
    # first boot reaches the release half, and /etc/odile stays 0750 when the
    # installer sets it. Same flags as odile_install.py's ensure_manifest_group.
    if ! getent group odile-manifest >/dev/null 2>&1; then
        groupadd --system odile-manifest || true
        say create "system group odile-manifest (the manifest listener's traverse)"
    fi
    install_file "$HERE/verify.html" /usr/local/lib/odile/verify.html 0644
    install_file "$HERE/verify.py" /usr/local/lib/odile/verify.py 0755
    install_file "$HERE/access_report.sh" /usr/local/lib/odile/access_report.sh 0755
    install_file "$HERE/verify_and_unlock.sh" /usr/local/lib/odile/verify_and_unlock.sh 0755
    # The page's own loopback unit (t_4fcd465f). Root because the audit reads
    # /etc/shadow and the journal; 127.0.0.1 only, Caddy is the listener.
    if [ -f "$HERE/odile-verify.service" ]; then
        unit_tmp=$(mktemp)
        sed -e "s|__VERIFY_PORT__|$VERIFY_PORT|g" \
            "$HERE/odile-verify.service" > "$unit_tmp"
        install_file "$unit_tmp" /etc/systemd/system/odile-verify.service 0644
        rm -f "$unit_tmp"
        systemctl enable odile-verify.service >/dev/null
        say same "odile-verify.service enabled at boot"
    fi
    if [ -f "$HERE/attest.py" ]; then
        install_file "$HERE/attest.py" /usr/local/lib/odile/attest.py 0755
    fi
    if [ -f "$HERE/bench-attest.py" ] && [ ! -f /etc/odile/production ]; then
        install_file "$HERE/bench-attest.py" /usr/local/lib/odile/bench_attest.py 0755
    fi
    # The release installer rewrites odile-access-report.service itself, with
    # no Environment= line, and its wrapper then looks up the user `odile`;
    # this image's tenant account is `tenant`. A drop-in survives that rewrite
    # because the installer writes the unit file, not this directory. Measured
    # on ODL-332 round 6: without it the unit failed to start ("no
    # /home/odile/.hermes/.env") and the release install stopped there.
    if [ -f "$HERE/odile-access-report-user.conf" ]; then
        install -d -m 0755 /etc/systemd/system/odile-access-report.service.d
        install_file "$HERE/odile-access-report-user.conf" \
            /etc/systemd/system/odile-access-report.service.d/10-tenant-user.conf 0644
        say same "odile-access-report.service.d/10-tenant-user.conf (ODILE_ACCESS_USER=tenant)"
    fi
    for unit in odile-bench-attest.service odile-attest-boot.service odile-access-report.service; do
        # The bench attest stand-in derives a report from the pin and the
        # command line; on a Blackbox Servury's bb-seed already wrote the real
        # /run/blackbox/attest.json and the stand-in would overwrite it.
        if [ "$unit" = odile-bench-attest.service ] && [ -f /etc/odile/production ]; then
            say skip "$unit (production image: Servury's own boot report)"
            continue
        fi
        [ -f "$HERE/$unit" ] || continue
        install_file "$HERE/$unit" "/etc/systemd/system/$unit" 0644
        systemctl enable "$unit" >/dev/null
        say same "$unit enabled at boot"
    done
    if [ -d /run/systemd/system ]; then
        # The units order themselves (bench stand-in -> attestation -> report).
        systemctl start odile-bench-attest.service odile-attest-boot.service \
            odile-access-report.service odile-verify.service 2>/dev/null || true
        say start "the boot report, the machine's access report and its /verify page"
    fi
fi

# --------------------------------------------------------------------------
# 8. optional: the seed and the first-visit page (ODL-456's first boot)

if [ -n "$SEED_FILE" ]; then
    # The seed arrives already signature-checked by odile-firstboot; this
    # checks only what the installer is about to rely on.
    SEED_SLUG=$(/usr/bin/python3 - "$SEED_FILE" "$FIRST_VISIT_TOKEN" <<'PY'
import json, re, sys
path, token = sys.argv[1], sys.argv[2]
try:
    seed = json.load(open(path, encoding="utf-8"))
except (OSError, ValueError) as exc:
    raise SystemExit("install-stack: the seed is not JSON: %s" % exc)
if not isinstance(seed, dict):
    raise SystemExit("install-stack: the seed is not a JSON object")
slug = seed.get("slug")
if not isinstance(slug, str) or not re.match(r"^[a-z0-9][a-z0-9-]{1,62}$", slug):
    raise SystemExit("install-stack: the seed has no valid slug")
for key in ("instance_id", "near_api_key", "model", "hermes_version"):
    if not isinstance(seed.get(key), str) or not seed[key].strip():
        raise SystemExit("install-stack: the seed has no %s" % key)
for key in ("password", "passphrase", "private_key", "ssh_private_key", "luks"):
    if key in seed:
        raise SystemExit("install-stack: the seed carries a forbidden %s" % key)
print(slug)
PY
)
    install -d -m 0755 /etc/odile /usr/local/lib/odile

    if cmp -s "$SEED_FILE" /etc/odile/seed.json; then
        say same "/etc/odile/seed.json (0600)"
    else
        install -m 0600 "$SEED_FILE" /etc/odile/seed.json
        say write "/etc/odile/seed.json (0600, tenant $SEED_SLUG)"
    fi

    # The state is written once. A re-run must not re-open a page the user has
    # already spent; that is the one thing an idempotent apply must not repair.
    # B-once (ODL-925): a seeded run writes the seed's verifier into the .env,
    # so the state says signin_set_at — the page serves a login — and the stamp
    # is taken in section 4, after that write (ODL-947). A run that finds no
    # state creates it stamped only when section 4 left the verifier in the
    # file; a state that already exists keeps its timestamps.
    if [ -f /etc/odile/first-visit.json ]; then
        say same "/etc/odile/first-visit.json (written; a spent page stays spent)"
    else
        state_tmp=$(mktemp)
        /usr/bin/python3 - "$FIRST_VISIT_TOKEN" \
            "$([ "$ENV_HAS_SEED_SIGNIN" -eq 1 ] && echo seeded || echo classic)" \
            > "$state_tmp" <<'PY'
import hashlib, json, sys
from datetime import datetime, timezone

seeded = sys.argv[2] == "seeded"
created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
print(json.dumps({
    "version": 1,
    "token_sha256": hashlib.sha256(sys.argv[1].encode("ascii")).hexdigest(),
    "created_at": created,
    "used_at": None,
    "signin_set_at": created if seeded else None,
    "first_login_at": None,
    "recovery_hash": None,
    "recovery_issued_at": None,
    "recovery_used_at": None,
}, indent=2, sort_keys=True))
PY
        install -m 0600 "$state_tmp" /etc/odile/first-visit.json
        rm -f "$state_tmp"
        say write "/etc/odile/first-visit.json (token sha256 only, no plaintext)"
    fi

    if [ ! -f "$HERE/first_visit.py" ] || [ ! -f "$HERE/first-visit.html" ]; then
        # A tree where ODL-457 has not merged: the seed is stored and the
        # state written, but there is no page to install. The first boot says
        # so in its one line instead of failing the machine.
        say skip "the first-visit page (ODL-457 is not in this image)"
    else
    install_file "$HERE/first_visit.py" /usr/local/lib/odile/first_visit.py 0755
    install_file "$HERE/first-visit.html" /usr/local/lib/odile/first-visit.html 0644
    # ODL-332 round 7: the done page offers the recovery file, and the file
    # must exist before the page can be answered; the release installer writes
    # it only near the end of its run. The unit's ExecStartPre writes it first.
    install_file "$HERE/recovery-seed.py" /usr/local/lib/odile/recovery-seed.py 0755

    unit_tmp=$(mktemp)
    sed -e "s|__TENANT_USER__|$TENANT_USER|g" \
        -e "s|__TENANT_HOME__|$TENANT_HOME|g" \
        -e "s|__FIRST_VISIT_PORT__|$FIRST_VISIT_PORT|g" \
        -e "s|__UNIT__|$SERVICE_NAME|g" \
        "$HERE/odile-first-visit.service" > "$unit_tmp"
    if cmp -s "$unit_tmp" /etc/systemd/system/odile-first-visit.service; then
        say same "/etc/systemd/system/odile-first-visit.service"
        rm -f "$unit_tmp"
    else
        chmod 0644 "$unit_tmp"
        mv "$unit_tmp" /etc/systemd/system/odile-first-visit.service
        say write "/etc/systemd/system/odile-first-visit.service"
    fi
    systemctl enable odile-first-visit.service >/dev/null
    say same "odile-first-visit.service enabled at boot"
    # In the guest (the first boot) start it now; the first-boot program then
    # proves the page answers before it writes its state. In a build chroot
    # there is no PID 1 to ask.
    if [ -d /run/systemd/system ]; then
        systemctl restart odile-first-visit.service
        say start "odile-first-visit.service (127.0.0.1:$FIRST_VISIT_PORT)"
    fi
    fi
fi

say ready "Hermes $VERSION_LINE for $TENANT_USER; Webapp $WEBAPP_HOST:$WEBAPP_PORT"
