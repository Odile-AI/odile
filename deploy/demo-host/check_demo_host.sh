#!/bin/bash
# Assert the demo's definition of done on the machine that serves demo.odile.fyi.
#
# demo.odile.fyi is Odile's tenant machine (odl-fake01): a custom-UKI guest
# running the signed release, one shared `judge` account, the meter removed,
# no boot re-assert and no demo-owned caps — the machine keeps the state its
# owner leaves (t_ee5aac66). Card: ODL-318, re-cut by t_2a847ea3 after HQ's
# demo was retired on 2026-11-13. The runbook is docs/runbooks/demo-public.md;
# the public definition of done is deploy/demo-host/check_demo_public.py.
#
# From the repo root on HQ — a tenant carries no SSH key, so the transport is
# the tenant's own Webapp socket (deploy/demo-host/tenant_console.py, ODL-503):
#
#   DEMO_PASSWORD_FILE=/root/.odile/judge-pass.txt \
#       ./deploy/demo-host/check_demo_host.sh --tenant
#
# On the machine itself, as root (serial console, or `sudo -i`):
#
#   sudo DEMO_PASSWORD_FILE=/run/odile-demo-secret \
#       ./deploy/demo-host/check_demo_host.sh --local
#
# Both run the same checks. `--tenant` uploads this file, runs it detached on
# the tenant and prints the transcript, because the restart leg restarts
# odile-agent — the gateway the console talks over. The restart is part of done
# ("a restart brings the same URL back"); nothing else on the machine changes.
#
# Exit codes: 0 every check passed, 1 at least one failed, 2 a missing
# prerequisite (root on the tenant, curl, python3, the console, no password).
set -u

DEMO_USER=${DEMO_USER:-tenant}
DEMO_HOST=${DEMO_HOST:-demo.odile.fyi}
DEMO_URL=${DEMO_URL:-https://$DEMO_HOST}
#: The machine's own name, for the alias legs. Not an address: the demo host
#: must resolve to this machine, which is asserted from the machine's own
#: interfaces (see "dns and units") rather than from a literal in this file.
MACHINE_HOST=${MACHINE_HOST:-odl-fake01.odile.fyi}
MACHINE_URL=${MACHINE_URL:-https://$MACHINE_HOST}
DEMO_ADDRESS=${DEMO_ADDRESS:-}
DEMO_HOME=${DEMO_HOME:-/home/$DEMO_USER}
LANDING_URL=${LANDING_URL:-https://odile.fyi/}
DEMO_PASSWORD=${DEMO_PASSWORD:-}
DEMO_PASSWORD_FILE=${DEMO_PASSWORD_FILE:-}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
MODE=auto

for arg in "$@"; do
    case "$arg" in
        --local) MODE=local ;;
        --tenant) MODE=tenant ;;
        -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
        *) echo "check_demo_host: unknown argument $arg" >&2; exit 2 ;;
    esac
done

# The password is read from a file at run time and never printed.
if [ -z "$DEMO_PASSWORD" ] && [ -n "$DEMO_PASSWORD_FILE" ] && [ -r "$DEMO_PASSWORD_FILE" ]; then
    DEMO_PASSWORD=$(tr -d '\n' < "$DEMO_PASSWORD_FILE")
fi

if [ "$MODE" = auto ]; then
    if [ -d /srv/odile-demo ] && [ -f /etc/systemd/system/odile-agent.service ]; then
        MODE=local
    else
        MODE=tenant
    fi
fi

# ---------------------------------------------------------------------------
# --tenant: the transport. Upload this file through the tenant's Webapp, run
# it detached as root, and print the transcript it leaves behind.
if [ "$MODE" = tenant ]; then
    CONSOLE=$REPO/deploy/demo-host/tenant_console.py
    if [ ! -f "$CONSOLE" ]; then
        echo "check_demo_host: $CONSOLE is missing (ODL-503's tenant console)" >&2
        exit 2
    fi
    for tool in python3 sha256sum; do
        command -v "$tool" >/dev/null 2>&1 || {
            echo "check_demo_host: missing $tool" >&2; exit 2; }
    done
    if [ -z "$DEMO_PASSWORD" ]; then
        echo "check_demo_host: set DEMO_PASSWORD_FILE to the judge password" >&2
        exit 2
    fi
    PW_FILE=$(mktemp)
    chmod 600 "$PW_FILE"
    printf '%s\n' "$DEMO_PASSWORD" > "$PW_FILE"
    trap 'rm -f "$PW_FILE"' EXIT
    # The page this checkout carries, so the tenant leg can compare it to the
    # bytes Caddy serves. `make secrets` never sees it; it is a file hash.
    LOGIN_SHA=$(sha256sum "$REPO/deploy/demo-host/login.html" | awk '{print $1}')
    BACKUP_SHA=$(sha256sum "$REPO/scripts/backup.py" | awk '{print $1}')
    echo "upload       $(sha256sum "$0" | awk '{print $1}') -> /run/odile-check-demo-host.sh"
    echo "backup       $BACKUP_SHA (scripts/backup.py) -> /usr/local/lib/odile/backup.py"
    echo "page         $LOGIN_SHA (deploy/demo-host/login.html)"
    # One console for the whole drive. The Webapp rate-limits a wrong sign-in
    # (`401` then `429 Too many login attempts` for about a minute, measured
    # 2026-11-14), and Caddy answers `502`/`503`/`504` while odile-agent
    # restarts, so a connect that fails while the run is in flight is
    # transient; only a 401 (the password) is fatal. The restart leg closes
    # this socket on purpose, and the run's own log file is its record.
    VERDICT=$(python3 - "$CONSOLE" "$PW_FILE" "$0" "$LOGIN_SHA" \
        "$REPO/scripts/backup.py" "$BACKUP_SHA" "$DEMO_USER" "$DEMO_HOME" <<'PY'
import json
import os
import sqlite3
import sys
import time

console_path, password_path, script_path, login_sha, backup_path, backup_sha = sys.argv[1:7]
console_user, console_home = sys.argv[7:9]
sys.path.insert(0, os.path.dirname(os.path.abspath(console_path)))
import tenant_console  # noqa: E402

password = open(password_path, encoding="utf-8").read().strip()
REMOTE_SCRIPT = "/run/odile-check-demo-host.sh"
REMOTE_PW = "/run/odile-check-demo-host.pass"
REMOTE_BACKUP = "/run/odile-backup.py"
REMOTE_LOG = "/run/odile-check-demo-host.log"
REMOTE_FILES = ("odile-check-demo-host.sh odile-check-demo-host.pass"
                " odile-backup.py odile-check-demo-host.log")
#: The whole drive: sign-in, upload, the detached run (which restarts the
#: gateway) and the read-back. Past this the transport gives up with exit 2.
#: It has to outlast the inner first-backup wait (BACKUP_WAIT_SECONDS, 900 s
#: by default) plus the check's other legs, which take a couple of minutes
#: including the gateway restart.  Forty minutes leaves room for both.
DEADLINE = time.monotonic() + 2400
#: The wait between sign-in attempts while the gateway restarts.
RETRY = 5.0
#: Every console session this driver opened, in order. `Console` leaves the
#: session to its caller — t_addab701 fixes the CLI's own lifecycle, not this
#: class — and the restart leg forces a reconnect, so one run can open two.
CONSOLES: list[str] = []


def tracked(console):
    """Record every session this console opens (its `shell.exec` row)."""
    open_session = console.open_session

    def remember() -> str:
        session_id = open_session()
        if session_id and session_id not in CONSOLES:
            CONSOLES.append(session_id)
            print(f"console      session {session_id}")
        return session_id

    console.open_session = remember
    return console


def connect():
    """One sign-in. A restart's 5xx and the route's 429 are transient.

    While `systemctl restart odile-agent` runs, Caddy answers the sign-in route
    with 503 for tens of seconds (verifier, run 1, 2026-11-14); that is the run
    in flight, not a failed check. A 401 is the password: fail at once.
    """
    while True:
        try:
            return tracked(tenant_console.connect_any("judge", password))
        except (tenant_console.ConsoleError, OSError) as exc:
            text = str(exc)
            if "401" in text or time.monotonic() >= DEADLINE:
                print(f"check_demo_host: the tenant console -> {exc}", file=sys.stderr)
                raise SystemExit(2)
            print(f"check_demo_host: the tenant console -> {exc}; retrying",
                  file=sys.stderr)
            time.sleep(RETRY)


def cleanup(client):
    """Remove this run's /run files and the console sessions it opened.

    The .pass is the judge password and the .log is the run's transcript; a
    run whose gateway died leaves them only until the next one. The console's
    own `shell.exec` session is a row on the machine as surely as a judge's is:
    t_addab701 owns the CLI's lifecycle, but `Console` leaves every session to
    its caller, so this driver deletes the ids it opened — by id, with the
    tenant's own CLI — and proves each with the machine's own report. Measured
    2026-11-17: deleting a live console's own row answers `Deleted session`,
    the socket keeps answering, and `--report --session-id` says exists:false.
    Cleanup never masks the verdict.
    """
    try:
        client.run(f"cd /run && sudo rm -f {REMOTE_FILES}", check=False)
    except (tenant_console.ConsoleError, OSError):
        try:
            client = connect()
            client.run(f"cd /run && sudo rm -f {REMOTE_FILES}", check=False)
        except (tenant_console.ConsoleError, OSError, SystemExit):
            return
    for session_id in list(CONSOLES):
        try:
            deleted = client.run(
                f"sudo -u {console_user} env HOME={console_home}"
                f" HERMES_HOME={console_home}/.hermes"
                f" {console_home}/.hermes/hermes-agent/hermes"
                f" sessions delete --yes {session_id}", check=False)
            print(f"cleanup      console session {session_id[:20]}…: "
                  f"{(deleted.get('stdout') or deleted.get('stderr') or '').strip()[:80]}")
            report = client.run(
                "sudo /usr/bin/python3 /usr/local/lib/odile-demo-public"
                f"/apply_demo_public.py --report --session-id {session_id}",
                check=False)
            row = json.loads((report.get("stdout") or "").strip() or "{}")
            print(f"cleanup      console session {session_id[:20]}… "
                  f"exists:{str(row.get('exists')).lower()}")
        except (tenant_console.ConsoleError, OSError, ValueError) as error:
            print(f"cleanup      console session {session_id[:20]}… -> {error}")
    print(f"cleanup      console sessions this run opened: {len(CONSOLES)}")


client = connect()


def read_remote(path: str) -> str:
    """One small root-owned file, read over the console."""
    try:
        return client.read(path).decode("utf-8", "replace").strip()
    except (tenant_console.ConsoleError, OSError):
        return ""


#: How long the seed waits for a first backup.  Five minutes was short enough
#: to fail a healthy seed on 2026-11-15, and — worse — the failure printed
#: "did not finish within 300 s" while the cause (a wrong repository password)
#: sat in the unit's journal.  A full first backup is a minutes-scale job: the
#: demo's first successful snapshot (47 MB / 635 files) took 1.5 s of restic
#: time once the repository answered, but a cold repository and a slower link
#: are not five-second jobs.  The wait is a budget; a run still going when it
#: ends is a note, and a unit that died is a failure with its journal.
#: BACKUP_WAIT_SECONDS shortens the budget for a test of that branch on a
#: machine whose repository is already warm.
try:
    BACKUP_WAIT = max(1, int(os.environ.get("BACKUP_WAIT_SECONDS") or 900))
except ValueError:
    BACKUP_WAIT = 900

#: The wrong-password remedy the runbook (§6) describes; printed beside the
#: failure so the operator does not have to read the journal to learn it.
REMEDY = (
    "the repository was initialised with another password (a rebuild without"
    " the old recovery file): move the old repository aside and run backup.py"
    " init with /etc/odile/recovery.txt before starting the service"
)


def backup_record() -> dict:
    """The machine's status record, or `{}` when there is none yet."""
    try:
        result = client.run("sudo cat /var/lib/odile/backup.json", check=False)
        value = json.loads((result.get("stdout") or "").strip() or "{}")
        return value if isinstance(value, dict) else {}
    except (tenant_console.ConsoleError, OSError, ValueError, TypeError):
        return {}


def backup_unit_state() -> tuple[str, str]:
    """What systemd says about the backup unit, and the tail of its journal.

    `backup.json` carries the program's own failure record, but a unit can die
    before writing one (an unreadable repository password is one way).  The
    unit is the surface the operator reads, so the seed asks it directly.
    """
    try:
        shown = client.run(
            "systemctl show -p ActiveState -p SubState -p Result -p ExecMainStatus"
            " odile-backup-tenant.service",
            check=False,
        )
        journal = client.run(
            "sudo journalctl -u odile-backup-tenant.service -n 20 --no-pager",
            check=False,
        ).get("stdout") or ""
    except (tenant_console.ConsoleError, OSError) as error:
        return "unknown", str(error)
    text = (shown.get("stdout") or "").strip()
    fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    state = fields.get("ActiveState") or "unknown"
    detail = " ".join(
        f"{key}={value}" for key, value in fields.items() if key != "ActiveState"
    )
    tail = " | ".join(line for line in journal.strip().splitlines()[-4:])
    return state, (detail + (" | " + tail if tail else "")).strip()


def seed_hq() -> tuple[bool, str]:
    """The half only HQ can do after a rebuild, and it is idempotent.

    A rebuilt machine makes a new heartbeat key, and the receiver refuses a
    different key on purpose (409); a new SFTP key is refused by the backup
    box until its public half is authorized.  This half revokes the stale
    registration, appends the new SFTP key, runs one backup when the last
    success is older than twelve hours, and reads the fresh beat back from
    HQ.  A converged machine prints `same` and skips the backup.
    """
    print("== the HQ half (seeding)")
    slug = "odl-fake01"
    try:
        seed = json.loads(read_remote("/etc/odile/seed.json"))
        slug = seed.get("slug") or slug
    except (ValueError, AttributeError):
        pass
    hb_pub = read_remote("/etc/odile/heartbeat/heartbeat.pub")
    sftp_pub = read_remote("/etc/odile/backup-ssh/id_ed25519.pub")
    db = os.environ.get("ODILE_STATE_DB", "/var/lib/odile/waitlist.db")
    authorized = os.environ.get(
        "ODILE_BACKUP_AUTHORIZED_KEYS", "/srv/odile-backup/.ssh/authorized_keys")
    failures: list[str] = []

    # 1. The SFTP key on the backup box.
    parts = sftp_pub.split()
    blob = parts[1] if len(parts) >= 2 else ""
    if not blob:
        failures.append("the machine has no SFTP public key")
    else:
        existing = ""
        try:
            existing = open(authorized, encoding="utf-8").read()
        except OSError:
            existing = ""
        if blob in existing:
            print("same         the backup box authorizes this machine's SFTP key")
        else:
            # The whole line, comment included: `ssh-keygen -C "odile-backup
            # $SLUG"` names the machine, and the comment is how a stale key is
            # later told apart from a live one.
            line = " ".join(parts)
            try:
                with open(authorized, "a", encoding="utf-8") as handle:
                    if existing and not existing.endswith("\n"):
                        handle.write("\n")
                    handle.write(line + "\n")
                print(f"authorized   {line}")
            except OSError as error:
                failures.append(
                    f"cannot write {authorized} ({error}); append: {line}")

    # 2. The heartbeat key the receiver holds for this slug.
    if not hb_pub:
        failures.append("the machine has no heartbeat public key")
    else:
        try:
            connection = sqlite3.connect(db, timeout=10)
            try:
                row = connection.execute(
                    "SELECT public_key FROM heartbeat_keys WHERE slug = ?", (slug,)
                ).fetchone()
                if row is None:
                    print(f"seeded       no key registered yet; the first beat registers {slug}")
                elif row[0].strip() == hb_pub:
                    print("same         the registered heartbeat key is this machine's")
                else:
                    connection.execute(
                        "DELETE FROM heartbeat_keys WHERE slug = ?", (slug,))
                    connection.commit()
                    print(f"revoked      the old heartbeat key for {slug} "
                          "(this machine was rebuilt)")
            finally:
                connection.close()
        except sqlite3.Error as error:
            failures.append(f"cannot read the heartbeat keys: {error}")

    # 3. One backup when the last success is stale, then read it back.
    now = int(time.time())
    record = backup_record()
    last = int(record.get("last_success_at") or 0)
    snapshot = str(record.get("snapshot") or "")
    if last and now - last < 12 * 3600:
        print(f"same         the last backup is {now - last}s old "
              f"(snapshot {snapshot[:12]}…)")
    elif failures:
        print("skip         the first backup (fix the keys above first)")
    else:
        unit = "odile-demo-backup-%d" % now
        try:
            client.run(f"sudo systemd-run --unit={unit} --collect"
                       " systemctl start odile-backup-tenant.service")
            print(f"started      odile-backup-tenant.service ({unit})")
        except (tenant_console.ConsoleError, OSError) as error:
            failures.append(f"cannot start the backup: {error}")
        started = time.monotonic()
        deadline = started + BACKUP_WAIT
        fresh = False
        while not failures and time.monotonic() < deadline:
            # Sleep no longer than the budget: a short BACKUP_WAIT_SECONDS
            # must reach the end of the budget, not overshoot it by a poll.
            time.sleep(min(8.0, max(1.0, deadline - time.monotonic())))
            record = backup_record()
            last = int(record.get("last_success_at") or 0)
            if last >= now - 120:
                snapshot = str(record.get("snapshot") or "")
                print(f"ok           a fresh backup at {last} "
                      f"(snapshot {snapshot[:12]}…) after "
                      f"{int(time.monotonic() - started)}s")
                fresh = True
                break
            if record.get("status") == "failed":
                error = str(record.get("error") or "")[:200]
                failures.append(f"the backup failed: {error}")
                if "wrong password" in error or "no key found" in error:
                    failures.append(REMEDY)
                state, detail = backup_unit_state()
                if detail:
                    print(f"unit         odile-backup-tenant.service: "
                          f"{state} {detail}")
                break
        if not failures and not fresh:
            # The unit may have finished between the last poll and the
            # deadline: one last read before asking systemd.
            record = backup_record()
            last = int(record.get("last_success_at") or 0)
            if last >= now - 120:
                snapshot = str(record.get("snapshot") or "")
                print(f"ok           a fresh backup at {last} "
                      f"(snapshot {snapshot[:12]}…) after "
                      f"{int(time.monotonic() - started)}s")
                fresh = True
        if not failures and not fresh:
            # Either the backup is still running or the unit died before it
            # wrote a record.  Ask systemd which one: a wrong password kills
            # the unit, and "did not finish within N s" would hide the remedy.
            state, detail = backup_unit_state()
            elapsed = int(time.monotonic() - started)
            if detail:
                print(f"unit         odile-backup-tenant.service: {state} {detail}")
            if state in ("failed", "inactive", "unknown"):
                failures.append(
                    f"odile-backup-tenant.service is {state} after {elapsed}s"
                    f" and /var/lib/odile/backup.json has no fresh success"
                    + (f": {detail}" if detail else ""))
                if "wrong password" in detail or "no key found" in detail:
                    failures.append(REMEDY)
            else:
                print(f"note         the first backup is still running after "
                      f"{elapsed}s (a first backup is a minutes-scale job; the "
                      "budget is BACKUP_WAIT_SECONDS); the timer finishes it "
                      "and the next check reads last_success_at")

    # 4. The beat itself, read from HQ's own store.
    try:
        client.run("sudo systemctl start odile-heartbeat.service", check=False)
        time.sleep(2)
    except (tenant_console.ConsoleError, OSError):
        pass
    try:
        connection = sqlite3.connect(db, timeout=10)
        try:
            row = connection.execute(
                "SELECT MAX(at) FROM heartbeats WHERE slug = ?", (slug,)
            ).fetchone()
        finally:
            connection.close()
        latest = int(row[0] or 0)
        if latest >= now - 600:
            print(f"ok           HQ holds a signed heartbeat for {slug} at {latest}")
        else:
            failures.append(f"HQ has no heartbeat newer than ten minutes for {slug}")
    except (sqlite3.Error, TypeError, ValueError) as error:
        failures.append(f"cannot read the heartbeats: {error}")

    for failure in failures:
        print(f"FAIL seeding: {failure}")
    return (not failures), ("seed: " + "; ".join(failures) if failures else "seed: ok")


try:
    client.put(script_path, REMOTE_SCRIPT)
    client.put(password_path, REMOTE_PW)
    client.put(backup_path, REMOTE_BACKUP)
    client.run(f"sudo chmod 0600 {REMOTE_PW}", check=False)
    # Bare names after `cd /run`: the Webapp's command guard reads an absolute
    # path after `rm` as destructive (tenant_console.put's note).
    client.run("cd /run && sudo rm -f odile-check-demo-host.log", check=False)
    # The restart leg kills the socket this driver talks over, so the run is a
    # transient systemd unit with its own log file — the Webapp's command guard
    # refuses `nohup`/`&`, and systemd-run is how a detached job is started on
    # this machine (the same way a tenant service runs).
    unit = "odile-check-demo-host-%d" % int(time.time())
    # The runbook's documented override has to reach the machine: the local
    # half reads BACKUP_REPO_URL, and the console transport is the only way a
    # run from HQ can set it (a real storage box, or a drill's decoy repo).
    repo_override = os.environ.get("BACKUP_REPO_URL", "").strip()
    repo_env = (
        f" --setenv=BACKUP_REPO_URL={repo_override}" if repo_override else ""
    )
    launch = (
        f"sudo systemd-run --unit={unit} --collect"
        f" --setenv=CHECK_LOG={REMOTE_LOG} --setenv=DEMO_PASSWORD_FILE={REMOTE_PW}"
        f" --setenv=LOGIN_SHA={login_sha} --setenv=BACKUP_SHA={backup_sha}{repo_env}"
        f" bash {REMOTE_SCRIPT} --local"
    )
    started = client.run(launch)
    if started.get("code") not in (0, None):
        print("check_demo_host: the tenant did not start the unit: "
              f"{(started.get('stderr') or started.get('stdout') or '').strip()}",
              file=sys.stderr)
        raise SystemExit(1)
    print(f"run          {unit}")
    tail = ""
    while time.monotonic() < DEADLINE:
        time.sleep(10)
        try:
            tail = (client.shell(f"tail -c 2000 {REMOTE_LOG}", timeout=60).get("stdout") or "")
        except (tenant_console.ConsoleError, OSError):
            # The restart closed the gateway (and this socket); reconnect and
            # keep polling — the log file is the run's own record.
            client = connect()
            continue
        if "demo-host check: " in tail:
            break
    log = ""
    while True:
        try:
            log = client.read(REMOTE_LOG).decode("utf-8", "replace")
            break
        except (tenant_console.ConsoleError, OSError):
            if time.monotonic() >= DEADLINE:
                break
            client = connect()
    sys.stdout.write(log)
    if log and not log.endswith("\n"):
        sys.stdout.write("\n")
    sd_ok, sd_note = seed_hq()
    if "demo-host check: PASS (" in log and sd_ok:
        print("VERDICT pass")
    elif "demo-host check: PASS (" in log:
        print(f"VERDICT seed-fail {sd_note}")
    elif "demo-host check: FAIL (" in log:
        print("VERDICT fail")
    else:
        print("VERDICT none")
except (tenant_console.ConsoleError, OSError) as exc:
    print(f"check_demo_host: the tenant console -> {exc}", file=sys.stderr)
    raise SystemExit(2)
finally:
    cleanup(client)
PY
)
    TRANSPORT_RC=$?
    printf '%s\n' "$VERDICT" | sed '/^VERDICT /d'
    case "$VERDICT" in
        *"VERDICT pass"*) exit 0 ;;
        *"VERDICT seed-fail"*) echo "check_demo_host: the machine passed; the HQ seed did not (see above)" >&2; exit 1 ;;
        *"VERDICT fail"*) exit 1 ;;
    esac
    if [ "$TRANSPORT_RC" = 2 ]; then
        echo "check_demo_host: the tenant console did not answer (see above)" >&2
        exit 2
    fi
    echo "check_demo_host: the tenant's transcript has no verdict" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# --local: the checks, on the machine that serves the name.
if [ -n "${CHECK_LOG:-}" ]; then
    # The transport starts this run detached and reads this file back; the
    # script owns its own redirect so the launcher needs no shell '&' or '>'
    # (the Webapp's command guard refuses both).
    exec >>"$CHECK_LOG" 2>&1
fi
PASS=0
FAIL=0
ok() { printf 'ok   %s\n' "$*"; PASS=$((PASS + 1)); }
bad() { printf 'FAIL %s\n' "$*"; FAIL=$((FAIL + 1)); }
note() { printf '     %s\n' "$*"; }
expect() { # label expected actual
    if [ "$2" = "$3" ]; then
        ok "$1: $3"
    else
        bad "$1: got '$3', expected '$2'"
    fi
}

for tool in curl getent ip openssl pgrep python3 sha256sum ssh-keygen ssh-keyscan ss stat systemctl; do
    command -v "$tool" >/dev/null 2>&1 || { echo "check_demo_host: missing $tool" >&2; exit 2; }
done
if [ "$(id -u)" != 0 ]; then
    echo "check_demo_host: run as root on the tenant (the .env and the unit need it)" >&2
    exit 2
fi
if [ -z "$DEMO_PASSWORD" ]; then
    echo "check_demo_host: set DEMO_PASSWORD_FILE (or DEMO_PASSWORD) to the judge password" >&2
    exit 2
fi

ENV_PATH=$DEMO_HOME/.hermes/.env
CONFIG_PATH=$DEMO_HOME/.hermes/config.yaml
LIB=/usr/local/lib/odile-demo-public
STATE_DB=$DEMO_HOME/.hermes/state.db
CHECK_PROMPT='Reply with exactly: ok'
demo_env() { sed -n "s/^$1=//p" "$ENV_PATH" | head -n 1; }

# The session this check mints is a row in the judge's sidebar as surely as a
# judge's own is. The socket leg creates it, and step 11 restarts odile-agent,
# which reaps it as a startup orphan; measured 2026-11-16, one row per run and
# ~20 of them on the live machine (Savio). These three functions are the
# lifecycle: count the rows that carry this check's prompt, delete one by id
# with the tenant's own CLI, and prove the delete with the machine's own
# report. Existence is never read from `/api/sessions` — that endpoint is a
# 20-row window (t_ee5aac66).
SESSION_ID=""
SESSION_CLEANED=0

# The store's own count of rows whose first user message is the check prompt.
# Read-only (`mode=ro`): a check never writes a row.
count_check_prompt_rows() {
    /usr/bin/python3 - "$STATE_DB" "$CHECK_PROMPT" <<'PY'
import sqlite3
import sys

con = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
try:
    print(con.execute(
        "SELECT count(*) FROM sessions s WHERE (SELECT content FROM messages m"
        " WHERE m.session_id = s.id AND m.role = 'user'"
        " ORDER BY m.display_order, m.id LIMIT 1) = ?", (sys.argv[2],)).fetchone()[0])
finally:
    con.close()
PY
}

# The owner path the runbook documents for a row whose Delete a judge cannot
# take (§4): the tenant's own CLI, as the machine's owner.
delete_check_session() { # id
    sudo -u "$DEMO_USER" env HOME="$DEMO_HOME" HERMES_HOME="$DEMO_HOME/.hermes" \
        "$DEMO_HOME/.hermes/hermes-agent/hermes" sessions delete --yes "$1"
}

# Delete the id this check minted and prove it with `--report --session-id`.
# `quiet` is the EXIT trap's form: it must not move PASS/FAIL after the
# verdict line has been printed.
cleanup_check_session() { # id [quiet]
    local session_id=$1 output
    if ! output=$(delete_check_session "$session_id" 2>&1); then
        [ "${2:-}" = quiet ] || bad "hermes sessions delete --yes $session_id -> ${output:-no output}"
        return 1
    fi
    SESSION_CLEANED=1
    if output=$(/usr/bin/python3 "$LIB/apply_demo_public.py" \
            --report --session-id "$session_id" 2>&1) \
            && printf '%s' "$output" | grep -Eq '"exists"[[:space:]]*:[[:space:]]*false'; then
        [ "${2:-}" = quiet ] || ok "the session this check minted is gone: $session_id (report exists:false)"
        return 0
    fi
    [ "${2:-}" = quiet ] || bad "the session this check minted still exists: ${output:-no report}"
    return 1
}

# The failure path: a run that dies between `session.create` and the cleanup
# still deletes the row it minted (the id is parsed from the transcript the
# socket leg printed before it died).
# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup_on_exit() {
    rm -f "${JAR:-}"
    if [ -n "${SESSION_ID:-}" ] && [ "${SESSION_CLEANED:-0}" != 1 ]; then
        cleanup_check_session "$SESSION_ID" quiet >/dev/null 2>&1 || true
    fi
}


# 1. The account and its home. There is one OS user on this machine: the
# machine's owner, and the judge's Webapp shell runs as it (ODL-503 §3).
echo "== account"
if id "$DEMO_USER" >/dev/null 2>&1; then
    ok "id $DEMO_USER: $(id "$DEMO_USER")"
else
    bad "user $DEMO_USER does not exist"
fi
DEMO_GROUPS=$(id -nG "$DEMO_USER" 2>/dev/null || true)
case " $DEMO_GROUPS " in
    *" sudo "*) bad "$DEMO_USER is in the sudo group: $DEMO_GROUPS" ;;
    *) ok "$DEMO_USER groups: $DEMO_GROUPS (no sudo group)" ;;
esac
# The owner rule: the console runs `shell.exec` as this user, which holds
# NOPASSWD sudo. A judge with the published password therefore reaches root on
# the demo machine — the machine is Odile's, not a tenant's (ODL-503).
if sudo -u "$DEMO_USER" sudo -n true 2>/dev/null; then
    ok "$DEMO_USER holds NOPASSWD sudo (the owner rule; a judge's shell runs as it)"
else
    bad "$DEMO_USER does not hold the owner's NOPASSWD sudo"
fi
expect "$DEMO_HOME mode and owner" "750 $DEMO_USER:$DEMO_USER" "$(stat -c '%a %U:%G' "$DEMO_HOME" 2>&1)"
expect "$DEMO_HOME/.hermes mode and owner" "700 $DEMO_USER:$DEMO_USER" "$(stat -c '%a %U:%G' "$DEMO_HOME/.hermes" 2>&1)"
expect "$ENV_PATH mode and owner" "600 $DEMO_USER:$DEMO_USER" "$(stat -c '%a %U:%G' "$ENV_PATH" 2>&1)"
if [ -d "$DEMO_HOME/.hermes/hermes-agent" ]; then
    ok "the Hermes install tree is $DEMO_HOME/.hermes/hermes-agent"
else
    bad "$DEMO_HOME/.hermes/hermes-agent is missing"
fi
if [ -e "$DEMO_HOME/.git" ] || [ -e "$DEMO_HOME/.hermes/.git" ]; then
    bad "the tenant profile is inside a git checkout"
else
    ok "the tenant profile is outside any git checkout"
fi
# The demo password and the cookie secret live in the tenant's .env, which is
# 0600: a process running as the user reads it, and nothing else does.
if sudo -u "$DEMO_USER" test -r /root/.hermes/.env; then
    bad "$DEMO_USER can read /root/.hermes/.env"
else
    ok "$DEMO_USER cannot read /root/.hermes/.env"
fi
# The recovery code is the one secret the machine writes for its owner.
expect "/etc/odile/recovery.txt mode and owner" "600 root:root" "$(stat -c '%a %U:%G' /etc/odile/recovery.txt 2>&1)"

# 2. The .env is an allowlist: four keys, the inference key among them.
echo "== .env allowlist"
ALLOW='HERMES_DASHBOARD_BASIC_AUTH_(USERNAME|PASSWORD_HASH|SECRET)|HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY'
OUTSIDE=$(grep -vE "^($ALLOW)=" "$ENV_PATH" | grep -cvE '^[[:space:]]*(#|$)' || true)
expect "lines outside the allowlist" "0" "$OUTSIDE"
expect "allowlisted lines" "4" "$(grep -cE "^($ALLOW)=" "$ENV_PATH")"
SEAT=$(grep -cE '^(GITHUB_TOKEN|SUDO_PASSWORD|EMAIL_PASSWORD|HERMES_DASHBOARD_SESSION_TOKEN|TELEGRAM_|MATRIX_)=' "$ENV_PATH" || true)
expect "seat-secret lines" "0" "$SEAT"
expect "meter placeholder lines (DEMO_METER_TOKEN)" "0" "$(grep -c '^DEMO_METER_TOKEN=' "$ENV_PATH" || true)"
expect "inference-key lines" "1" "$(grep -c '^HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY=' "$ENV_PATH")"

# 3. The listeners: 9130 on loopback only, owned by the machine's owner;
# Caddy is the only public listener beside sshd.
echo "== listeners"
LISTEN=$(ss -ltnp 2>/dev/null || true)
case "$LISTEN" in
    *"127.0.0.1:9130"*) ok "the demo Webapp listens on 127.0.0.1:9130" ;;
    *) bad "no listener on 127.0.0.1:9130" ;;
esac
case "$LISTEN" in
    *"0.0.0.0:9130"*|*"[::]:9130"*) bad "9130 is bound to a public address" ;;
    *) ok "9130 has no 0.0.0.0/[::] listener" ;;
esac
# The meter is out of the path by decision: masked, inactive, and absent from
# the listener table. A listener here would mean something restarted it.
case "$LISTEN" in
    *":8011"*) bad "the retired demo meter listens on 8011" ;;
    *) ok "no listener on 8011 (the meter is out of the path)" ;;
esac
WEBAPP_PID=$(printf '%s\n' "$LISTEN" | grep '127.0.0.1:9130' | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -n 1)
if [ -n "$WEBAPP_PID" ]; then
    expect "the 9130 process runs as" "$DEMO_USER" "$(ps -o user= -p "$WEBAPP_PID" | tr -d ' ')"
else
    bad "cannot identify the process on 9130"
fi
# The machine's own report servers are loopback only: the signed manifest and
# the first-visit gate a fresh machine needs.
case "$LISTEN" in
    *"127.0.0.1:8012"*) ok "the signed manifest server listens on 127.0.0.1:8012" ;;
    *) bad "no listener on 127.0.0.1:8012 (the signed manifest)" ;;
esac
case "$LISTEN" in
    *"127.0.0.1:8091"*) ok "the first-visit server listens on 127.0.0.1:8091" ;;
    *) bad "no listener on 127.0.0.1:8091 (the first-visit gate)" ;;
esac
if printf '%s\n' "$LISTEN" | grep -q 'caddy'; then
    ok "Caddy holds the public listeners"
else
    bad "no caddy listener found in ss -ltnp"
fi
PUBLIC=$(printf '%s\n' "$LISTEN" | awk 'NR > 1 && $4 !~ /^127\./ && $4 !~ /^\[::1\]/ && $4 !~ /^::1$/ {print $4" "$6}' | sed 's/  */ /g' | sort)
PUBLIC_PORTS=$(printf '%s\n' "$PUBLIC" | awk '{n = split($1, a, ":"); print a[n]}' | sort -n | uniq | tr '\n' ' ' | sed 's/ $//')
BASELINE_PORTS=" 22 80 443 "
NEW_PORTS=""
for port in $PUBLIC_PORTS; do
    case "$BASELINE_PORTS" in
        *" $port "*) ;;
        *) NEW_PORTS="$NEW_PORTS $port" ;;
    esac
done
note "public listeners (address, process):"
printf '%s\n' "$PUBLIC" | sed '/^$/d; s/^/     /'
if [ -z "$NEW_PORTS" ]; then
    ok "public ports are the tenant baseline: $PUBLIC_PORTS"
else
    bad "new public listener(s):$NEW_PORTS"
fi
if printf '%s\n' "$PUBLIC" | grep -q '"hermes"'; then
    bad "a hermes process holds a public listener"
else
    ok "no hermes process holds a public listener"
fi

# 3b. Egress: no loaded nft/iptables ruleset, and the demo shell reaches the
# public internet. The HQ demo's rule was loopback-only — its own header:
# "Internet traffic is untouched" — and it retired with the HQ demo (t_9d7ee536).
# A loaded ruleset added here makes the runbook's "Egress: no nft or iptables
# ruleset" paragraph false, so this block fails first and the paragraph is
# re-cut with it (t_d969339e). It proves those two shapes — the ones a later
# change would plausibly install. An eBPF or non-nft container-network filter
# would pass it; that is the verdict's residual, docs/security/DEMO-EGRESS-VERDICT.md §5.
echo "== egress"
FILTER=""
if command -v nft >/dev/null 2>&1 && [ -n "$(nft list ruleset 2>/dev/null)" ]; then
    FILTER="nft ruleset"
fi
for IPT in iptables iptables-legacy; do
    command -v "$IPT" >/dev/null 2>&1 || continue
    if "$IPT" -S 2>/dev/null | grep -Eq '^-A |^-P [A-Z]+ (DROP|REJECT)$'; then
        FILTER="${FILTER:+$FILTER + }$IPT rule or non-ACCEPT policy"
    fi
done
if [ -n "$FILTER" ]; then
    bad "a ruleset is loaded on the demo machine ($FILTER; re-cut the runbook's egress paragraph)"
else
    ok "no loaded nft or iptables ruleset on the demo machine"
fi
expect "the demo shell reaches the public internet" "200" \
    "$(sudo -u "$DEMO_USER" curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://odile.fyi/)"

# 4. The name and the units. The demo host must resolve to this machine, and
# the machine's own name to the same address; the release installer converges
# the machine at every boot and odile-demo-public re-asserts the demo after it.
echo "== dns and units"
RESOLVED=$(getent hosts "$DEMO_HOST" | awk '{print $1}' | sort -u | tr '\n' ' ' | sed 's/ $//')
MACHINE_RESOLVED=$(getent hosts "$MACHINE_HOST" | awk '{print $1}' | sort -u | tr '\n' ' ' | sed 's/ $//')
OWN=$(ip -4 -o addr show | awk '{print $4}' | cut -d/ -f1 | sort -u | tr '\n' ' ')
note "$DEMO_HOST -> ${RESOLVED:-nothing}; $MACHINE_HOST -> ${MACHINE_RESOLVED:-nothing}"
if [ -z "$RESOLVED" ]; then
    bad "$DEMO_HOST resolves to nothing on this machine"
elif [ -n "$DEMO_ADDRESS" ] && [ "$RESOLVED" != "$DEMO_ADDRESS" ]; then
    bad "getent hosts $DEMO_HOST: got '$RESOLVED', expected '$DEMO_ADDRESS'"
else
    UNMATCHED=""
    for address in $RESOLVED; do
        case " $OWN " in
            *" $address "*) ;;
            *) UNMATCHED="$UNMATCHED $address" ;;
        esac
    done
    if [ -z "$UNMATCHED" ]; then
        ok "getent hosts $DEMO_HOST -> $RESOLVED, this machine's own address"
    else
        bad "$DEMO_HOST resolves to$UNMATCHED, not this machine ($OWN)"
    fi
fi
if [ -n "$MACHINE_RESOLVED" ] && [ "$MACHINE_RESOLVED" = "$RESOLVED" ]; then
    ok "$MACHINE_HOST resolves to the same address as the demo host"
else
    bad "$MACHINE_HOST -> '${MACHINE_RESOLVED:-nothing}', $DEMO_HOST -> '${RESOLVED:-nothing}'"
fi
expect "systemctl is-enabled odile-agent" "enabled" "$(systemctl is-enabled odile-agent 2>&1 || true)"
expect "systemctl is-active odile-agent" "active" "$(systemctl is-active odile-agent 2>&1 || true)"
expect "the odile-agent user" "$DEMO_USER" "$(systemctl show -p User --value odile-agent 2>/dev/null)"
AGENT_PID=$(systemctl show -p MainPID --value odile-agent 2>/dev/null)
GW_PID=$(sed -n 's/.*"pid": *\([0-9]*\).*/\1/p' "$DEMO_HOME/.hermes/gateway.pid" 2>/dev/null | head -n 1)
expect "the gateway pid file is the running unit" "$AGENT_PID" "$GW_PID"
expect "gateway processes on this machine" "1" "$(pgrep -c -f '[g]ateway run' || true)"
# The tenant is converged by the signed release, not by an HQ reconcile timer
# or a demo boot unit: odile-release-setup converges once and the machine keeps
# the state its owner leaves (t_ee5aac66). An odile-demo-* unit file, an
# odile-demo-* timer or an odile reconcile unit here would be an HQ leftover.
expect "the last odile-release-setup run" "success" "$(systemctl show -p Result --value odile-release-setup.service 2>/dev/null)"
DEMO_UNITS=$(systemctl list-unit-files 'odile-demo-*' --no-legend --no-pager 2>/dev/null || true)
if [ -z "$DEMO_UNITS" ]; then
    ok "no odile-demo-* unit file (the boot re-assert is gone)"
else
    bad "odile-demo-* unit file(s) present: $(printf '%s' "$DEMO_UNITS" | tr '\n' ';')"
fi
DEMO_TIMERS=$(systemctl list-timers --all 'odile-demo-*' --no-legend --no-pager 2>/dev/null || true)
if [ -z "$DEMO_TIMERS" ]; then
    ok "no odile-demo-* timer"
else
    bad "odile-demo-* timer(s) present: $(printf '%s' "$DEMO_TIMERS" | tr '\n' ';')"
fi
expect "odile-meter unit" "not-found" "$(systemctl is-enabled odile-meter 2>&1 || true)"

# 4b. The signed heartbeat and the nightly backup — seeded here, not assumed.
#
# The release installer writes these when it converges, but a rebuild puts the
# image back and can lose them: measured 2026-11-15, the demo's last heartbeat
# was 2026-11-13T03:01:14Z, the day it was reinstalled, and the poll planned a
# `down` and a `backup` alert for a machine that was up. So the machine's own
# check is the seed. Every step compares before it writes and prints `same` on
# a converged machine; the two public keys are printed for the HQ half, which
# registers the SFTP key and revokes a rebuilt machine's old heartbeat key.
#
# The seed URL names `backup.odile.fyi`, a host that does not resolve yet; the
# storage box today is HQ's own sftp service on port 2222 (docs/runbooks/
# backup.md §3). The landing host resolves to it, and BACKUP_REPO_URL overrides
# both when a real box is bought.
echo "== heartbeat and backup timers"
BACKUP=/usr/local/lib/odile/backup.py
KEY_DIR=/etc/odile/backup-ssh
HEARTBEAT_DIR=/etc/odile/heartbeat
SLUG=$(/usr/bin/python3 - <<'PY' 2>/dev/null || true
import json
for path in ("/etc/odile/seed.json", "/var/lib/odile/firstboot.json"):
    try:
        value = json.load(open(path, encoding="utf-8")).get("slug")
    except (OSError, ValueError):
        continue
    if isinstance(value, str) and value:
        print(value)
        break
PY
)
[ -n "$SLUG" ] || SLUG=${MACHINE_HOST%%.*}
LANDING_HOST=${LANDING_URL#*://}
LANDING_HOST=${LANDING_HOST%%/*}
REPO_URL=${BACKUP_REPO_URL:-}
if [ -z "$REPO_URL" ] && [ -s /etc/odile/backup-repo.txt ]; then
    # Keep a seed (or an override) whose host resolves: that is a real box.
    CANDIDATE=$(head -n 1 /etc/odile/backup-repo.txt)
    CANDIDATE_HOST=$(printf '%s' "$CANDIDATE" \
        | sed -n 's|^sftp://\{0,1\}[^@]*@\([^:/]*\).*|\1|p')
    if [ -n "$CANDIDATE_HOST" ] && getent hosts "$CANDIDATE_HOST" >/dev/null 2>&1; then
        REPO_URL=$CANDIDATE
    fi
fi
[ -n "$REPO_URL" ] || REPO_URL="sftp://odile-backup@$LANDING_HOST:2222/tenants/$SLUG"
REPO_AUTH=${REPO_URL#sftp:}
REPO_AUTH=${REPO_AUTH#//}
REPO_HOSTPORT=${REPO_AUTH#*@}
REPO_HOSTPORT=${REPO_HOSTPORT%%/*}
REPO_HOST=${REPO_HOSTPORT%%:*}
REPO_PORT=""
case "$REPO_HOSTPORT" in
    *:*) REPO_PORT=${REPO_HOSTPORT##*:} ;;
esac
note "$SLUG -> $REPO_URL"

# The program: uploaded with this check on the HQ path, or already installed.
if [ ! -x "$BACKUP" ]; then
    if [ -s /run/odile-backup.py ]; then
        if [ -n "${BACKUP_SHA:-}" ]; then
            expect "the uploaded backup program is this checkout's scripts/backup.py" \
                "$BACKUP_SHA" "$(sha256sum /run/odile-backup.py | awk '{print $1}')"
        fi
        if install -m 0755 -o root -g root /run/odile-backup.py "$BACKUP"; then
            ok "installed $BACKUP (uploaded with this check)"
        else
            bad "could not install $BACKUP from /run/odile-backup.py"
        fi
    else
        bad "$BACKUP is missing; run this check with --tenant from HQ (it uploads the program)"
    fi
else
    ok "$BACKUP exists (sha256 $(sha256sum "$BACKUP" | cut -c1-12)…)"
fi

# The repository URL the units and `backup.py run` read.
if [ -s /etc/odile/backup-repo.txt ] && [ "$(head -n 1 /etc/odile/backup-repo.txt)" = "$REPO_URL" ]; then
    ok "/etc/odile/backup-repo.txt is the live repository URL"
else
    printf '%s\n' "$REPO_URL" > /etc/odile/backup-repo.txt
    chmod 0644 /etc/odile/backup-repo.txt
    ok "wrote /etc/odile/backup-repo.txt ($REPO_URL)"
fi

# The SFTP transport key: generated here, public half registered on the box.
if [ ! -s "$KEY_DIR/id_ed25519" ]; then
    install -d -m 0700 "$KEY_DIR"
    if ssh-keygen -q -t ed25519 -N "" -C "odile-backup $SLUG" -f "$KEY_DIR/id_ed25519"; then
        chmod 0600 "$KEY_DIR/id_ed25519"
        ok "created the SFTP key for $SLUG"
    else
        bad "ssh-keygen did not create the SFTP key"
    fi
else
    expect "the SFTP key mode" "600" "$(stat -c '%a' "$KEY_DIR/id_ed25519")"
fi
if [ ! -s "$KEY_DIR/known_hosts" ]; then
    SCAN_ARGS=(-T 8 -H)
    if [ -n "$REPO_PORT" ]; then
        SCAN_ARGS+=(-p "$REPO_PORT")
    fi
    if ssh-keyscan "${SCAN_ARGS[@]}" "$REPO_HOST" > "$KEY_DIR/known_hosts" 2>/dev/null \
        && [ -s "$KEY_DIR/known_hosts" ]; then
        chmod 0600 "$KEY_DIR/known_hosts"
        ok "recorded $REPO_HOST's host key ($(wc -l < "$KEY_DIR/known_hosts") line(s))"
    else
        bad "ssh-keyscan $REPO_HOST did not answer; the first backup will fail"
    fi
else
    expect "the SFTP known_hosts mode" "600" "$(stat -c '%a' "$KEY_DIR/known_hosts")"
fi

# The heartbeat key: generated here, the private half never leaves the machine.
if [ ! -s "$HEARTBEAT_DIR/heartbeat.key" ]; then
    install -d -m 0700 "$HEARTBEAT_DIR"
    if openssl genpkey -algorithm ED25519 -out "$HEARTBEAT_DIR/heartbeat.key" \
        && chmod 0600 "$HEARTBEAT_DIR/heartbeat.key" \
        && openssl pkey -in "$HEARTBEAT_DIR/heartbeat.key" -pubout \
            -out "$HEARTBEAT_DIR/heartbeat.pub"; then
        chmod 0644 "$HEARTBEAT_DIR/heartbeat.pub"
        ok "created the heartbeat key"
    else
        bad "openssl did not create the heartbeat key"
    fi
else
    expect "the heartbeat key mode" "600" "$(stat -c '%a' "$HEARTBEAT_DIR/heartbeat.key")"
fi

# The nightly backup unit and timer, from the program's own templates.
if [ -x "$BACKUP" ]; then
    if "$BACKUP" install --repo "$REPO_URL" --slug "$SLUG" --home "$DEMO_HOME" \
        --recovery /etc/odile/recovery.txt --status /var/lib/odile/backup.json \
        --key "$KEY_DIR/id_ed25519" --known-hosts "$KEY_DIR/known_hosts" \
        --heartbeat-url https://odile.fyi/heartbeat \
        --heartbeat-key-dir "$HEARTBEAT_DIR" \
        --unit-dir /etc/systemd/system --install-path "$BACKUP"; then
        ok "the nightly backup unit and timer converged (daily 03:00)"
    else
        bad "backup.py install did not converge the nightly unit"
    fi
fi

# The five-minute heartbeat: a signed note from this machine, independent of
# the backup run. `Persistent=true` means a beat missed while down fires on the
# next boot rather than waiting for the next five-minute boundary.
write_unit() { # path text
    if [ -f "$1" ] && [ "$(cat "$1")" = "$2" ]; then
        note "same         $1"
        return 0
    fi
    printf '%s\n' "$2" > "$1"
    chmod 0644 "$1"
    note "wrote        $1"
}
write_unit /etc/systemd/system/odile-heartbeat.service "[Unit]
Description=Odile tenant heartbeat (signed, every five minutes)
Documentation=file://$BACKUP
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $BACKUP heartbeat --status /var/lib/odile/backup.json --slug $SLUG --url https://odile.fyi/heartbeat --key-dir $HEARTBEAT_DIR
Nice=10
IOSchedulingClass=idle"
write_unit /etc/systemd/system/odile-heartbeat.timer "[Unit]
Description=Odile tenant heartbeat, every five minutes

[Timer]
OnCalendar=*:0/5
Persistent=true
RandomizedDelaySec=30

[Install]
WantedBy=timers.target"

systemctl daemon-reload
for unit in odile-backup-tenant.timer odile-heartbeat.timer; do
    systemctl enable "$unit" >/dev/null 2>&1 || true
    systemctl start "$unit" >/dev/null 2>&1 || true
    expect "systemctl is-enabled $unit" "enabled" "$(systemctl is-enabled "$unit" 2>&1 || true)"
    expect "systemctl is-active $unit" "active" "$(systemctl is-active "$unit" 2>&1 || true)"
done
expect "odile timers on this machine" "2" \
    "$(systemctl list-timers --all --no-legend --no-pager 2>/dev/null | grep -c 'odile' || true)"

if [ -s /var/lib/odile/backup.json ]; then
    LAST_BACKUP=$(/usr/bin/python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("last_success_at") or "")' /var/lib/odile/backup.json 2>/dev/null || true)
    if [ -n "$LAST_BACKUP" ]; then
        ok "the last successful backup is $LAST_BACKUP"
    else
        bad "/var/lib/odile/backup.json carries no last_success_at"
    fi
else
    note "no backup yet: the timer runs daily at 03:00; the SFTP key below must be authorized first"
fi
if [ -s "$HEARTBEAT_DIR/heartbeat.pub" ]; then
    note "heartbeat public key: $(cat "$HEARTBEAT_DIR/heartbeat.pub" | tr '\n' ' ')"
fi
if [ -s "$KEY_DIR/id_ed25519.pub" ]; then
    note "sftp public key: $(cat "$KEY_DIR/id_ed25519.pub")"
fi

for unit in odile-first-visit odile-manifest odile-access-report odile-attest-boot; do
    expect "systemctl is-active $unit" "active" "$(systemctl is-active "$unit" 2>&1 || true)"
done

# 5. The deployed pieces: the sign-in page, the Caddy block and the apply.
echo "== deployed pieces"
LOGIN_FILE=/srv/odile-demo/login.html
if [ -f "$LOGIN_FILE" ]; then
    ok "$LOGIN_FILE exists ($(stat -c '%s' "$LOGIN_FILE") B)"
    expect "$LOGIN_FILE mode and owner" "644 root:root" "$(stat -c '%a %U:%G' "$LOGIN_FILE")"
    if [ -n "${LOGIN_SHA:-}" ]; then
        # ODL-503 §2: the committed page and the live page are one file. A
        # deploy from an older tree fails here on purpose — re-run from the
        # tree that deployed the page.
        expect "the live page is this checkout's deploy/demo-host/login.html" \
            "$LOGIN_SHA" "$(sha256sum "$LOGIN_FILE" | awk '{print $1}')"
    else
        note "no LOGIN_SHA passed: the live page's bytes were not compared"
    fi
else
    bad "$LOGIN_FILE is missing"
fi
if [ -x "$LIB/apply_demo_public.py" ]; then
    ok "$LIB/apply_demo_public.py is installed"
else
    bad "$LIB/apply_demo_public.py is missing"
fi
if [ -x "$LIB/restart-agent" ]; then
    ok "$LIB/restart-agent is installed (the restart the guard lets through)"
else
    bad "$LIB/restart-agent is missing"
fi
expect "$LIB mode and owner" "755 root:root" "$(stat -c '%a %U:%G' "$LIB" 2>&1)"
expect "/srv/odile-demo mode and owner" "755 root:root" "$(stat -c '%a %U:%G' /srv/odile-demo 2>&1)"
BEGIN_MARK='# BEGIN odile-demo-public'
END_MARK='# END odile-demo-public'
# The block Caddy actually loads, cut out of the live file: the per-site facts
# below are asserted on it, not on the whole Caddyfile (the machine's own block
# proxies to the same Webapp).
BLOCK_FILE=$(mktemp)
awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
    { line=$0; sub(/^[ \t]+/, "", line); sub(/[ \t]+$/, "", line)
      if (line == b) f=1
      if (f) print
      if (line == e) f=0 }' /etc/caddy/Caddyfile > "$BLOCK_FILE"
LIVE_BLOCK=$(sha256sum "$BLOCK_FILE" | awk '{print $1}')
INSTALLED_BLOCK=$(sha256sum "$LIB/demo-tenant-site.caddy" 2>/dev/null | awk '{print $1}')
expect "the live Caddy block is the installed block" "$INSTALLED_BLOCK" "$LIVE_BLOCK"
expect "marker lines (BEGIN END)" "1 1" "$(grep -cxF "$BEGIN_MARK" /etc/caddy/Caddyfile) $(grep -cxF "$END_MARK" /etc/caddy/Caddyfile)"
# t_69f1a5f3 (Savio, 2026-11-13): the freeze is removed product-wide — no
# deny-by-default matcher and no `path /__disabled__` runtime line. The only
# named matcher left in the block is the operator map's 404.
expect "freeze lines in the Caddyfile (__disabled__)" "0" "$(grep -c '__disabled__' /etc/caddy/Caddyfile || true)"
expect "named matchers in the demo block" "apidocs" \
    "$(grep -oE '@[A-Za-z_][A-Za-z0-9_-]*' "$BLOCK_FILE" | sed 's/^@//' | sort -u | tr '\n' ' ' | sed 's/ $//')"
expect "the sign-in page handler in the demo block" "1" "$(grep -c 'handle /login\*' "$BLOCK_FILE" || true)"
expect "the Webapp upstream in the demo block" "1" "$(grep -c 'reverse_proxy 127.0.0.1:9130' "$BLOCK_FILE" || true)"
rm -f "$BLOCK_FILE"
RESIDUE=$(awk -v e="$END_MARK" '{ line=$0; sub(/^[ \t]+/, "", line)
    if (line == e) f=1; else if (f && line ~ /gateway\.ready/) n++ } END { print n + 0 }' /etc/caddy/Caddyfile)
expect "console residue lines after the block" "0" "$RESIDUE"

# 6. The machine's own report, read from the installed apply. One JSON object
# carries the route, the provider row, the Caddy hashes and the seeded session;
# the check asserts the fields it needs.
echo "== the tenant's own report"
REPORT=$(/usr/bin/python3 "$LIB/apply_demo_public.py" --report 2>/dev/null || true)
FIELDS=$(REPORT="$REPORT" python3 - <<'PY' 2>/dev/null || true
import json, os
try:
    r = json.loads(os.environ["REPORT"])
except Exception:
    print("unreadable")
    raise SystemExit(0)
c = r.get("config") or {}
p = r.get("provider") or {}
m = "meter" in r
mo = r.get("meter") or {}
d = r.get("caddy") or {}
s = r.get("session") or {}
v = r.get("verify") or {}
messages = s.get("messages") or 0
print("near_ai_direct={} public_url={} provider={} at={} meter_object={} meter={}/{}/{} caddy_same={} markers={} matchers={} residue={} lede={}/{} rollback={} seed={} seed_messages={} seed_ok={}".format(
    c.get("near_ai_direct"), c.get("public_url"),
    p.get("name"), p.get("base_url"),
    m,
    mo.get("enabled"), mo.get("active"), mo.get("unit_installed"),
    d.get("installed_sha256") == d.get("live_block_sha256"),
    d.get("markers"), d.get("matchers"), d.get("residue_lines"),
    v.get("demo_lede"), v.get("customer_lede"), v.get("rollback"),
    bool(s.get("id")), messages, bool(s.get("id")) and messages >= 2))
PY
)
case "$FIELDS" in
    *"near_ai_direct=True"*) ok "the tenant calls Near AI directly (no meter in the path)" ;;
    *) bad "the tenant is not on the Near AI route: $FIELDS" ;;
esac
case "$FIELDS" in
    *"public_url=True"*) ok "webapp.public_url is the demo host" ;;
    *) bad "webapp.public_url is not the demo host: $FIELDS" ;;
esac
case "$FIELDS" in
    *"provider=Near AI at=https://cloud-api.near.ai/v1"*) ok "the provider row is Near AI at cloud-api.near.ai" ;;
    *) bad "the provider row is not the demo's: $FIELDS" ;;
esac
# t_e237e261 removed the meter product-wide; t_dd815e89 deleted the apply's
# adoption shim, so a report from this tree carries no meter object at all.
# The report installed on the machine until the next deploy still prints the
# old object, whose values must also say the meter is absent: the generator
# prints both shapes (`meter_object` and the old enabled/active/installed
# triple), so this check is green before and after the deploy and red on a
# live meter.
case "$FIELDS" in
    *"meter_object=False"*|*"meter=not-found/inactive/False"*)
        ok "the meter is not installed (removed product-wide)" ;;
    *) bad "the meter is still on this machine: $FIELDS" ;;
esac
case "$FIELDS" in
    *"caddy_same=True markers=[1, 1] matchers=['apidocs'] residue=0"*)
        ok "the installed Caddy block is the live block (1 marker pair, only @apidocs, residue 0)" ;;
    *) bad "the Caddy block is not converged: $FIELDS" ;;
esac
case "$FIELDS" in
    *"lede=True/False rollback=True"*) ok "the tenant's /verify carries the demo lede, with the release page kept" ;;
    *) bad "the /verify lede is not the demo's: $FIELDS" ;;
esac
# The seed is written once and never rewritten (t_ee5aac66): it must exist
# with its two messages; its title, pinned flag and later turns are the
# owner's, so they are not asserted here.
case "$FIELDS" in
    *" seed_ok=True"*) ok "the seeded session is present with its messages" ;;
    *) bad "the seeded session is missing or empty: $FIELDS" ;;
esac

# 7. The money and the route: no meter, the key in the tenant's own .env, and
# the seeded session's frozen receipt on the Near AI route (the HQ receipt
# pointed at 127.0.0.1:8011 and would fail on this machine). The config's own
# turn and session limits are the owner's (t_ee5aac66) and are not asserted.
echo "== the route"
expect "8011 occurrences in the tenant config" "0" "$(grep -c '8011' "$CONFIG_PATH" || true)"
expect "model.base_url" "https://cloud-api.near.ai/v1" "$(sed -n 's/^  base_url: *//p' "$CONFIG_PATH" | head -n 1)"
RECEIPTS=$(python3 - "$DEMO_HOME" <<'PY' 2>/dev/null || true
import json, sqlite3, sys
con = sqlite3.connect(sys.argv[1] + "/.hermes/state.db")
rows = con.execute("select value from state_meta where key like 'gateway.local_policy%'").fetchall()
routes, broken = [], 0
for row in rows:
    try:
        routes.append(json.loads(json.loads(row[0])["policy"]["config_json"])["model"]["base_url"])
    except Exception:
        broken += 1
print("rows={} with8011={} near={} other={} broken={}".format(
    len(rows), sum("8011" in r for r in routes),
    sum(r == "https://cloud-api.near.ai/v1" for r in routes),
    sum("8011" not in r and r != "https://cloud-api.near.ai/v1" for r in routes),
    broken))
PY
)
case "$RECEIPTS" in
    rows=0) bad "no seeded-session receipt in state.db" ;;
    "rows="[1-9]*" with8011=0 near="[1-9]*" other=0 broken=0")
        ok "every session receipt routes to Near AI: $RECEIPTS" ;;
    *) bad "a session receipt does not route to Near AI: $RECEIPTS" ;;
esac

# 8. The HTTPS gate. The sign-in page is Odile's, the gate is Hermes's, and
# the facts on the page are Savio's (t_330cea5c and t_d95ad43d).
echo "== https gate"
NOAUTH=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$DEMO_URL/")
NOAUTH_TO=$(curl -s -o /dev/null -w '%{redirect_url}' --max-time 10 "$DEMO_URL/")
expect "GET / without credentials" "302" "$NOAUTH"
note "Location: $NOAUTH_TO"
LOGIN=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$DEMO_URL/login")
expect "GET /login" "200" "$LOGIN"
LOGIN_HEADERS=$(curl -s -D - -o /dev/null --max-time 10 "$DEMO_URL/login" || true)
for header in 'strict-transport-security:' 'x-robots-tag:' 'server: odile-demo'; do
    if printf '%s\n' "$LOGIN_HEADERS" | tr '[:upper:]' '[:lower:]' | grep -q "^$header"; then
        ok "GET /login sends $header"
    else
        bad "GET /login does not send $header"
    fi
done
LOGIN_BODY=$(curl -s --max-time 10 "$DEMO_URL/login")
case "$LOGIN_BODY" in
    *'<title>Sign in — Odile</title>'*) ok "GET /login title is Odile's" ;;
    *) bad "GET /login title is not Odile's" ;;
esac
case "$LOGIN_BODY" in
    *'🐴'*) bad "GET /login carries a vendor emoji" ;;
    *) ok "GET /login carries no vendor emoji" ;;
esac
# t_330cea5c's page: one quiet proof link and Savio's boundary sentence; the
# first-visit preview link and the Hermes credit are retired (t_d95ad43d).
case "$LOGIN_BODY" in
    *'href="/verify"'*) ok "GET /login links the boot report" ;;
    *) bad "GET /login is missing the boot report link" ;;
esac
case "$LOGIN_BODY" in
    *'This is an Odile demo instance.'*) ok "GET /login carries Savio's demo sentence" ;;
    *) bad "GET /login is missing Savio's demo sentence" ;;
esac
case "$LOGIN_BODY" in
    *'Runs on Hermes Agent'*) bad "GET /login still carries the Hermes credit" ;;
    *'first-visit-preview'*) bad "GET /login still links the retired first-visit preview" ;;
    *) ok "GET /login carries no Hermes credit and no first-visit preview link" ;;
esac
case "$LOGIN_BODY" in
    *'Hermes Agent dashboard'*) bad "GET /login still carries the vendor sign-in copy" ;;
    *) ok "GET /login has no vendor sign-in copy" ;;
esac
API=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$DEMO_URL/api/sessions")
expect "GET /api/sessions without credentials" "401" "$API"
expect "GET /openapi.json (the operator's map stays closed)" "404" \
    "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$DEMO_URL/openapi.json")"
# The machine's own name still answers its own report: the tenant's own Caddy
# block survives beside the demo one.
expect "GET $MACHINE_URL/verify" "200" \
    "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$MACHINE_URL/verify")"
expect "GET $MACHINE_URL/manifest" "200" \
    "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$MACHINE_URL/manifest")"

DEMO_USERNAME=$(demo_env HERMES_DASHBOARD_BASIC_AUTH_USERNAME)
JAR=$(mktemp)
trap cleanup_on_exit EXIT
# No wrong-password attempt here, unlike the HQ script. Measured 2026-11-14:
# one bad sign-in answers 401 and then rate-limits the route (`429 Too many
# login attempts`) for about a minute, so a check that spent one would leave
# the demo's own sign-in down — and the driver that uploaded this file needs
# it. The gate's refusal is asserted anonymously instead: the 302 on /, the
# 401 on /api/sessions and the 401 on PUT /api/config.
BODY=$(python3 -c 'import json,sys; print(json.dumps({"provider":"basic","username":sys.argv[1],"password":sys.argv[2]}))' "$DEMO_USERNAME" "$DEMO_PASSWORD")
LOGIN_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -c "$JAR" -X POST -H 'Content-Type: application/json' -d "$BODY" "$DEMO_URL/auth/password-login")
expect "POST /auth/password-login, demo credentials" "200" "$LOGIN_CODE"

SHELL_BODY=$(mktemp)
SHELL_CODE=$(curl -s -o "$SHELL_BODY" -w '%{http_code}' --max-time 10 -b "$JAR" "$DEMO_URL/")
expect "GET / with the session cookie" "200" "$SHELL_CODE"
if grep -q 'id="root"' "$SHELL_BODY" && grep -q '<title>Hermes</title>' "$SHELL_BODY"; then
    ok "the 200 body is the Hermes Webapp shell"
else
    bad "the 200 body is not the Hermes Webapp shell (root marker: $(grep -c 'id="root"' "$SHELL_BODY"), title: $(grep -c '<title>Hermes</title>' "$SHELL_BODY"))"
fi
rm -f "$SHELL_BODY"

# The fence this leg is measured by: one full run leaves no row whose first
# user message is the check prompt. The before-count is the store's own, taken
# before the socket mints a row.
echo "== the session this check mints"
SESSION_BEFORE="?"
if [ -f "$STATE_DB" ]; then
    SESSION_BEFORE=$(count_check_prompt_rows)
else
    bad "$STATE_DB is missing; the check-prompt rows cannot be counted"
fi
note "check-prompt rows before this run: $SESSION_BEFORE"

# The Webapp's socket is the same-origin /api/ws. A gated Webapp refuses the
# cookie alone: the SPA mints a 30 s single-use ticket (/api/auth/ws-ticket)
# and presents it on the upgrade. A 101 means Caddy forwards the upgrade and
# the authenticated session minted the ticket.
#
# This machine runs the real flow, not the HQ demo's hardened one: there is no
# ws_guard here, so the judge's operator socket runs the verbs the product
# uses — cli.exec and shell.exec as the machine's owner — and a chat turn
# answers through the machine's own Near AI key. The console transport in
# --tenant mode is the same surface, so a regression here breaks the deploy
# too.
TICKET=$(curl -s -b "$JAR" -X POST --max-time 10 "$DEMO_URL/api/auth/ws-ticket" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ticket",""))')
WS=$(DEMO_URL="$DEMO_URL" TICKET="$TICKET" python3 - <<'PY'
import base64, json, os, socket, ssl, struct, time, urllib.parse, uuid


def recv_exact(tls, buf, n):
    while len(buf) < n:
        chunk = tls.recv(max(4096, n - len(buf)))
        if not chunk:
            raise ConnectionError("eof")
        buf += chunk
    return buf[:n], buf[n:]


def read_frame(tls, buf):
    head, buf = recv_exact(tls, buf, 2)
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    if length == 126:
        ext, buf = recv_exact(tls, buf, 2)
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext, buf = recv_exact(tls, buf, 8)
        length = struct.unpack("!Q", ext)[0]
    mask = b""
    if masked:
        mask, buf = recv_exact(tls, buf, 4)
    payload, buf = recv_exact(tls, buf, length)
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload, buf


def send_text(tls, data):
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    size = len(data)
    if size < 126:
        header = bytes([0x81, 0x80 | size])
    elif size < 65536:
        header = bytes([0x81, 0x80 | 126]) + struct.pack("!H", size)
    else:
        header = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", size)
    tls.sendall(header + mask + masked)


def send_request(tls, rid, method, params):
    send_text(tls, json.dumps({"jsonrpc": "2.0", "id": rid,
                               "method": method, "params": params}).encode())


def wait_for(tls, buf, *, want_id=None, event_type=None, session_id=None, timeout=180.0):
    """Read frames until the reply id or the event type/session arrives."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        tls.settimeout(max(1.0, deadline - time.time()))
        opcode, payload, buf = read_frame(tls, buf)
        if opcode == 8:
            return None, buf
        if opcode != 1:
            continue
        message = json.loads(payload)
        if want_id is not None and message.get("id") == want_id:
            return message, buf
        if event_type and message.get("method") == "event":
            params = message.get("params") or {}
            if params.get("type") == event_type and (
                    session_id is None or params.get("session_id") == session_id):
                return message, buf
    return None, buf


url = urllib.parse.urlsplit(os.environ["DEMO_URL"])
ticket = urllib.parse.quote(os.environ["TICKET"])
key = base64.b64encode(os.urandom(16)).decode()
request = (
    f"GET /api/ws?ticket={ticket} HTTP/1.1\r\n"
    f"Host: {url.netloc}\r\n"
    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
    f"Origin: {url.scheme}://{url.netloc}\r\n\r\n"
)
try:
    context = ssl.create_default_context()
    with socket.create_connection((url.hostname, url.port or 443), timeout=15) as raw:
        with context.wrap_socket(raw, server_hostname=url.hostname) as tls:
            tls.sendall(request.encode())
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = tls.recv(4096)
                if not chunk:
                    raise ConnectionError("eof in the handshake")
                data += chunk
            head, _, buf = data.partition(b"\r\n\r\n")
            status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            print(f"status: {status}")
            if "101" not in status:
                raise SystemExit(0)
            # Read the gateway's ready frame before sending: a close that beats
            # it makes the gateway log `ws ready frame send failed` and exit
            # (systemd then restarts it); measured twice on 2026-11-07.
            tls.settimeout(30)
            opcode, payload, buf = read_frame(tls, buf)
            ready = ""
            if opcode == 1:
                with_ready = json.loads(payload)
                ready = (with_ready.get("params") or {}).get("type", "")
            print(f"ready: {ready or 'opcode ' + str(opcode)}")

            # 1-3. The operator verbs the Webapp's terminal uses. On this
            # machine they run: there is no method policy in front of them.
            # `shell.exec` needs a session the gateway holds in memory, so the
            # session is created between the two probes.
            send_request(tls, 1, "cli.exec", {"argv": ["config", "get", "webapp.port"], "timeout": 120})
            response, buf = wait_for(tls, buf, want_id=1, timeout=60)
            if response is None:
                print("policy: cli.exec -> no reply")
            elif response.get("error"):
                error = response["error"]
                print(f"policy: cli.exec -> {error.get('code')} {error.get('message')}")
            else:
                body = response.get("result") or {}
                print(f"policy: cli.exec -> result {str(body.get('output', '')).strip()[:40]}")

            send_request(tls, 2, "session.create", {"source": "gui", "profile": "default"})
            response, buf = wait_for(tls, buf, want_id=2, timeout=60)
            session_id = ((response or {}).get("result") or {}).get("session_id", "")
            print(f"session: {session_id}")
            if not session_id:
                print(f"chat: session.create -> {json.dumps(response)[:300]}")
                raise SystemExit(0)
            send_request(tls, 3, "shell.exec", {"session_id": session_id, "command": "id -un"})
            response, buf = wait_for(tls, buf, want_id=3, timeout=60)
            if response is None:
                print("policy: shell.exec -> no reply")
            elif response.get("error"):
                error = response["error"]
                print(f"policy: shell.exec -> {error.get('code')} {error.get('message')}")
            else:
                body = response.get("result") or {}
                print(f"policy: shell.exec -> result {str(body.get('stdout', '')).strip()[:40]}")

            # 4. The clarify answer path the Webapp uses: a batch question is
            # answered per question with clarify.lock (apps/desktop/src/store/
            # clarify.ts). The ids are dummy, so the vendor answers a result
            # (status "expired") or a parameter error; the assertion is that
            # the socket reaches the vendor at all.
            send_request(tls, 4, "clarify.lock", {
                "request_id": "check-demo-probe", "question_id": "q",
                "answer": "probe"})
            response, buf = wait_for(tls, buf, want_id=4, timeout=60)
            if response is None:
                print("clarify: clarify.lock -> no reply")
            elif response.get("error"):
                error = response["error"]
                print(f"clarify: clarify.lock -> error {error.get('code')} {error.get('message')}")
            else:
                print(f"clarify: clarify.lock -> result {json.dumps(response.get('result'))[:160]}")

            # 5. A real chat turn: submit one prompt to the session minted
            # above, wait for the terminal event, and print the answer.
            send_request(tls, 5, "prompt.submit", {
                "session_id": session_id,
                "text": "Reply with exactly: ok",
                "submission_id": str(uuid.uuid4()),
            })
            response, buf = wait_for(tls, buf, want_id=5, timeout=60)
            if response is not None and response.get("error"):
                print(f"chat: prompt.submit -> {json.dumps(response.get('error'))[:300]}")
                raise SystemExit(0)
            complete, buf = wait_for(tls, buf, event_type="message.complete",
                                     session_id=session_id, timeout=180)
            params = (complete or {}).get("params") or {}
            payload = params.get("payload") or {}
            content = str(payload.get("content", payload.get("text", ""))).strip()
            # The model is asked for one word; normalise case/punctuation so a
            # "OK." is the same proof as "ok" and the assertion is about the
            # turn finishing, not about the model's casing.
            normalized = content.lower().rstrip(".!")
            print(f"chat: reply={content!r} normalized={normalized!r} "
                  f"outcome={payload.get('outcome')}")
except (OSError, ValueError) as exc:
    print(f"status: error: {exc}")
PY
)

# The socket leg minted a session and Hermes keeps its row. Delete it here, by
# id, on every path — the transcript above carries the id even when the turn
# failed (`session.create` prints it before the chat) — and prove the delete
# with the machine's own report. This sits before step 9 and the restart so a
# cleanup failure is in the transcript next to the leg that caused it, and
# before step 11 so the restart cannot orphan the row first.
SESSION_ID=$(printf '%s\n' "$WS" | sed -n 's/^session: //p' | head -n 1)
if [ -n "$SESSION_ID" ]; then
    cleanup_check_session "$SESSION_ID"
else
    note "the socket leg minted no session (nothing to delete)"
fi
SESSION_AFTER="?"
if [ -f "$STATE_DB" ]; then
    SESSION_AFTER=$(count_check_prompt_rows)
fi
note "check-prompt rows after this run: $SESSION_AFTER"
expect "the run leaves the check-prompt row count where it found it" \
    "$SESSION_BEFORE" "$SESSION_AFTER"
expect "no row whose first user message is the check prompt is left" \
    "0" "$SESSION_AFTER"

case "$WS" in
    *"101"*) ok "GET /api/ws with a minted ticket -> 101 Switching Protocols" ;;
    *) bad "GET /api/ws with a minted ticket -> $WS" ;;
esac
case "$WS" in
    *"ready: gateway.ready"*"policy: cli.exec -> result 9130"*"policy: shell.exec -> result $DEMO_USER"*)
        ok "the judge's operator socket runs the real flow: cli.exec 9130, shell.exec $DEMO_USER" ;;
    *) bad "the /api/ws operator verbs did not answer as the product does: $WS" ;;
esac
case "$WS" in
    *"clarify: clarify.lock -> no reply"*)
        bad "the /api/ws clarify.lock canary got no reply" ;;
    *"clarify: clarify.lock -> result "*|*"clarify: clarify.lock -> error "*)
        ok "the clarify answer path reaches the vendor: clarify.lock answers" ;;
    *) bad "the /api/ws clarify.lock canary did not run: $WS" ;;
esac
case "$WS" in
    *"chat: reply="*"normalized='ok'"*"outcome=completed"*)
        ok "a judge's chat turn still works over /api/ws (the model answered 'ok' through the machine's key)" ;;
    *) bad "the /api/ws chat turn did not answer: $WS" ;;
esac

# 9. The demo is not frozen (docs/ops/DEMO-UNFREEZE.md): a mutation with the
# judge's session must reach Hermes, which answers 422 for an empty body. A
# 403 from the edge is the regression, not the expectation.
echo "== mutations reach the gateway"
for spec in "PUT /api/config" "POST /api/cron/jobs"; do
    method=${spec%% *}
    path=${spec#* }
    CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -b "$JAR" -X "$method" -H 'Content-Type: application/json' -d '{}' "$DEMO_URL$path")
    expect "$method $path with the judge session reaches Hermes" "422" "$CODE"
done
expect "PUT /api/config without credentials" "401" \
    "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X PUT -H 'Content-Type: application/json' -d '{}' "$DEMO_URL/api/config")"
expect "GET /api/status (the shell paints)" "200" \
    "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -b "$JAR" "$DEMO_URL/api/status")"

# 10. One gateway, one Hermes home. Unlike HQ, where the demo ran beside a
# default gateway, the demo IS this machine's gateway: a second home or a
# second `gateway run` would be a second agent on one machine.
echo "== one gateway"
expect "Hermes homes under /home" "1" "$(find /home -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ')"
expect "the landing page is untouched" "200" \
    "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$LANDING_URL")"
LANDING_HEADERS=$(curl -s -D - -o /dev/null --max-time 10 "$LANDING_URL" || true)
if printf '%s\n' "$LANDING_HEADERS" | grep -qi '^server: odile'; then
    ok "$LANDING_URL answers server: odile"
else
    bad "$LANDING_URL has no 'server: odile' header"
fi

# 11. A restart brings the same URL back; the stable _SECRET keeps the session.
echo "== restart"
BEFORE=$(systemctl show -p MainPID --value odile-agent)
if systemctl restart odile-agent; then
    ok "systemctl restart odile-agent"
else
    bad "systemctl restart odile-agent failed"
fi
BACK="000"
for _ in $(seq 1 180); do
    BACK=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$DEMO_URL/")
    [ "$BACK" = "302" ] && break
    sleep 1
done
expect "after restart, GET / (gate is back)" "302" "$BACK"
AFTER=$(systemctl show -p MainPID --value odile-agent)
if [ -n "$BEFORE" ] && [ "$BEFORE" != "$AFTER" ]; then
    ok "the restart replaced the gateway ($BEFORE -> $AFTER)"
else
    bad "the gateway pid did not change ($BEFORE -> $AFTER)"
fi
expect "after restart, the same session cookie" "200" \
    "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -b "$JAR" "$DEMO_URL/")"

echo
if [ "$FAIL" -eq 0 ]; then
    printf 'demo-host check: PASS (%d passed, 0 failed)\n' "$PASS"
    exit 0
fi
printf 'demo-host check: FAIL (%d passed, %d failed)\n' "$PASS" "$FAIL"
exit 1
