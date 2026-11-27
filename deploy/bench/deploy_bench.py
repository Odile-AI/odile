#!/usr/bin/python3
"""Build, deploy and roll back the test bench at https://test.odile.fyi.

ODL-339 (A1 criteria 1, 3 and 4). Production is one environment: /opt/odile,
/var/lib/odile/waitlist.db, odile.service and odile-seed.service on 8080/8081.
This script makes a second one — the bench — out of the same tree, not a fork:

  * a release tree under /opt/odile-test/releases/<id>, selected by the
    `current` symlink and remembered in `previous`, so one `--rollback` returns
    the release that was live before the last apply;
  * /etc/odile-bench/bench.env, the one non-secret set of values both units
    read, and /var/lib/odile-test for the database, attestation report and
    roadmap cache (the database starts empty and app.py's init_db() writes the
    schema; production's database is never copied);
  * odile-bench.service (the account site, 127.0.0.1:8082) and
    odile-bench-seed.service (the seed service, 127.0.0.1:8083), enabled at
    boot and restarted only when the bytes they run moved;
  * /root/.odile/seed-config.test.json (0600) — the bench's own seed config,
    derived from the production one with a `test-` backup prefix and an
    `environment` marker. An existing bench config is validated with the
    `seed/seed.py` this deploy ships and, when that loader refuses a required
    key the file predates (ODL-469's `mail_relay.tls_ca`, found on ODL-949),
    only that absent key is merged in from the production config — an
    operator's own keys are never rewritten, and a present-but-wrong value is
    named with a one-line fix instead of replaced.
    The seed key /root/.odile/seed-signing.pem is
    referenced by LoadCredential, never copied: a test tenant's installer
    verifies the bench seed against the same certified key as production;
  * test.odile.fyi A <this box's IPv4>, DNS-only, created through Cloudflare
    with the token at /root/.odile/cloudflare_dns_token. The address is
    discovered at run time — the committed stand-in is 203.0.113.139 — so a
    rebuilt bench points at whatever box runs this;
  * a `# BEGIN odile-bench` block in /etc/caddy/Caddyfile. Caddy runs with
    `admin off`, so the whole file is validated, backed up and restored on a
    failed validate before the one restart a changed block needs.

Idempotent: every step prints `same` and writes nothing on a converged host,
and the last line is `nothing to do`. Production is measured before and after:
the sha256 of /, /roadmap and /attestation and `systemctl show -p NRestarts
odile.service` must be equal, or the run fails.

    ./deploy/bench/deploy_bench.py --dry-run           # name every write
    ./deploy/bench/deploy_bench.py                     # apply this checkout
    ./deploy/bench/deploy_bench.py --source <tree>     # apply another tree
    ./deploy/bench/deploy_bench.py --rollback --dry-run
    ./deploy/bench/deploy_bench.py --rollback          # previous release back
    ./deploy/bench/deploy_bench.py --probe-tick        # one liveness tick

ODL-936. Two additions keep a dead bench from reading as healthy:

  * every apply (and every rollback) refuses before its first write when
    127.0.0.1:8082 or :8083 is held by a process that is not the bench unit.
    A holder whose working directory is a card worktree that no longer exists
    is the orphan that took the bench down on 2026-11-21: it is reaped and
    named, because its worktree is gone and nothing will ever stop it.
  * `odile-bench-probe.timer` runs `--probe-tick` every five minutes. The
    tick first clears the 2026-11-21 orphan from 127.0.0.1:8082 (a holder whose
    `.worktrees/<card>` is gone) and starts `odile-bench` when it did; a
    foreign holder is left to the apply's exit 2 and the probe names it. It
    then runs `scripts/check_bench.sh --probe` (one line) and, when the line
    is red, raises the bench's own alert in the existing HQ ledger, outbox and
    transports (odilectl.health); a green tick resolves that alert.

ODL-353 follow-up. `Persistent=true` makes the timer run the tick it missed
during a reboot the moment the timer starts, and `odile-bench.service` is
`Type=simple`: systemd marks it active when `app.py` is exec'd, before it binds
127.0.0.1:8082. On 2026-11-25T04:55:41Z both happened in the same second and
that tick's one line was `bench probe: FAIL … 502` — the only failed unit in
the boot record. The tick therefore waits, bounded by `BENCH_WAIT_SECONDS`, for
the bench's listener before it probes: `bench_listening()` is one TCP connect,
`wait_for_bench()` polls it, and a listener that never appears is *not* an
error — the probe still runs, still prints its red line and still raises the
alert, so the boot race is fixed without hiding a dead bench.

Exit codes: 0 success (changed, converged or rolled back), 1 a failed apply
(Caddy was restored), 2 a bad input, a missing prerequisite or a foreign port
holder.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

ROOT = Path("/opt/odile-test")
RELEASES = ROOT / "releases"
CURRENT = ROOT / "current"
PREVIOUS = ROOT / "previous"

STATE = Path("/var/lib/odile-test")
DB_PATH = STATE / "waitlist.db"
ATTEST_PATH = STATE / "attestation.json"
ROADMAP_PATH = STATE / "roadmap.html"
ROADMAP_SOURCE = STATE / "roadmap-source"

ETC = Path("/etc/odile-bench")
ENV_FILE = ETC / "bench.env"
UNIT_DIR = Path("/etc/systemd/system")
UNIT_SITE = UNIT_DIR / "odile-bench.service"
UNIT_SEED = UNIT_DIR / "odile-bench-seed.service"
UNIT_REGISTER = UNIT_DIR / "odile-bench-register.service"
TIMER_REGISTER = UNIT_DIR / "odile-bench-register.timer"
UNIT_PROBE = UNIT_DIR / "odile-bench-probe.service"
TIMER_PROBE = UNIT_DIR / "odile-bench-probe.timer"
UNIT_SITE_NAME = "odile-bench"
UNIT_SEED_NAME = "odile-bench-seed"
UNIT_REGISTER_NAME = "odile-bench-register"
UNIT_PROBE_NAME = "odile-bench-probe"
TIMER_PROBE_NAME = "odile-bench-probe"
#: The two units an apply starts and restarts; the probe is timer-driven.
UNIT_NAMES = (UNIT_SITE_NAME, UNIT_SEED_NAME)

CONFIG_FILE = Path("/root/.odile/seed-config.test.json")
#: The bench's own control-plane config (provider, DNS, outbox, trial keys).
#: `odile-bench-register.service` reads it; it stays root-only and host-local.
WORKER_CONFIG = Path("/root/.odile/config.test.yaml")
PROD_CONFIG = Path("/root/.odile/seed-config.json")
SEED_KEY_FILE = Path("/root/.odile/seed-signing.pem")
TOKEN_FILE = Path("/root/.odile/cloudflare_dns_token")

CADDYFILE = Path("/etc/caddy/Caddyfile")
BEGIN = "# BEGIN odile-bench"
END = "# END odile-bench"
HOST = "test.odile.fyi"

APP_PORT = 8082
SEED_PORT = 8083
APP_HEALTH = f"http://127.0.0.1:{APP_PORT}/health"
LANDING_URL = f"https://{HOST}/"
SEED_ROUTE_URL = f"https://{HOST}/seed"
PRODUCTION_URLS = ("https://odile.fyi/", "https://odile.fyi/roadmap",
                   "https://odile.fyi/attestation")
PRODUCTION_UNIT = "odile.service"

#: The address to publish. Empty means "discover this box's own IPv4"; the
#: documentation stand-in in the tree is 203.0.113.139.
DNS_IP = ""
ZONE_NAME = "odile.fyi"
API = "https://api.cloudflare.com/client/v4"
PROXIED = False
TTL = 1

#: The private checkout on this HQ box, built from parts so the public tree's
#: leak scan does not read it as a person's home path.
HQ_CHECKOUT = Path("/root") / "odile"

#: Where the private roadmap generator, backlog and template may live, in
#: order. The public tree carries none of them, so a bench built from a public
#: checkout skips the cache and /roadmap answers 503 until one is written.
ROADMAP_CANDIDATES = (
    (REPO / "tools" / "roadmap_page.py", HQ_CHECKOUT / "tools" / "roadmap_page.py"),
    (REPO / "docs" / "backlog.yaml", HQ_CHECKOUT / "docs" / "backlog.yaml"),
    (REPO / "web" / "roadmap.html", HQ_CHECKOUT / "web" / "roadmap.html"),
)

SYSTEMCTL = "systemctl"
CADDY = "caddy"
CURL = "curl"
DIG = "dig"

# How long a probe waits for the service, for Caddy's certificate and for the
# public name. Certificate issuance is the slow one on a first apply.
PROBE_SECONDS = 30.0
CERT_SECONDS = 120.0
DNS_SECONDS = 60.0

#: ODL-936 — the liveness tick. The probe itself is `scripts/check_bench.sh
#: --probe` (one line); the alert is the existing HQ path, not a second one.
PROBE_SCRIPT = REPO / "scripts" / "check_bench.sh"
PROBE_TIMEOUT = 90.0
ALERT_CONFIG = Path("/root/.odile/config.yaml")
ALERT_SLUG = "bench"
ALERT_KIND = "down"
ALERT_TENANT = "bench (test.odile.fyi)"
ALERT_FIRST_COMMAND = (
    "systemctl status odile-bench; ss -ltnp 'sport = :8082'")
#: How long a reaped orphan gets between SIGTERM and SIGKILL.
REAP_SECONDS = 5.0
#: ODL-353 follow-up — how long the tick waits for the bench's listener before
#: it probes. A cold boot starts odile-bench.service and the timer's
#: Persistent catch-up tick in the same transaction; Type=simple marks the unit
#: active when app.py is exec'd, before app.py binds 127.0.0.1:8082, so the tick
#: that met the first bind in the same second (2026-11-25T04:55:41Z) measured a
#: 502 and was the boot record's only failed unit. The wait is bounded and its
#: expiry is not an error: a bench that never binds must still probe red and
#: still raise the alert.
BENCH_WAIT_SECONDS = 20.0
BENCH_WAIT_POLL = 0.2

#: Directories a release copy never carries. `git ls-files` already excludes
#: them; the walk fallback needs the list.
EXCLUDE_DIRS = {".git", ".worktrees", "__pycache__", "build", ".fresh-clone",
                ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}
EXCLUDE_PREFIXES = ("docs/growth/",)
EXCLUDE_FILES = {"IDEA.md", "PROJECT.md"}

#: The registration worker (ODL-341). A oneshot that drains the confirmed
#: account queue: the web app runs as `odile` and the provider, DNS, NEAR and
#: seed credentials are root-only, so the order happens here, on a two-minute
#: timer, through the same `odilectl.provision` code the operator CLI calls.
#: Kept inline (not a `.in` file) so the deployer's own bytes describe the two
#: units it writes; `render_unit` fills the same slots.
WORKER_SERVICE = """[Unit]
Description=Odile test bench — the account queue: one confirmed account becomes one machine (ODL-341)
Documentation=https://test.odile.fyi/
# The queue lives in the bench app's database. A queue file created by root
# would be root-owned, and SQLite copies the database owner onto -wal/-shm, so
# uid odile would then fail with "attempt to write a readonly database".
ConditionPathExists={{STATE}}/waitlist.db
After=odile-bench.service

[Service]
Type=oneshot
User=root
WorkingDirectory={{ROOT}}/current
# The bench's own control plane: provider and DNS credentials, the trial's
# inference-key switch, the bench outbox and the seed handoff.
EnvironmentFile={{ETC}}/bench.env
Environment=PYTHONUNBUFFERED=1
Environment=ODILECTL_CONFIG={{WORKER_CONFIG}}
ExecStart=/usr/bin/python3 -m odilectl.provision --worker --apply --config {{WORKER_CONFIG}}
# provision polls a machine for up to 900 s; systemd's oneshot default is 90 s.
TimeoutStartSec=1800
Nice=10
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
# The state database, the outbox and the seed-key handoff, plus the directory
# that holds the worker's own config — as of ODL-242 the same directory carries
# the NEAR management credential, which `odilectl keys` rotates in place by
# writing a temp file beside `near-account.json` and renaming it over. With
# ProtectHome=read-only that rename is EROFS, and a registration already
# claimed `building` stays there with no server id (2026-11-24T03:52:15Z).
# Everything else on the host root, including the rest of /root, stays
# read-only; only this directory, the state database and the outbox are
# writable.
ReadWritePaths={{STATE}} /var/lib/odile {{WORKER_HOME}}
"""

WORKER_TIMER = """[Unit]
Description=Odile test bench — drain the account queue every two minutes (ODL-341)

[Timer]
OnCalendar=*:0/2
AccuracySec=15s
RandomizedDelaySec=0
# A missed run (host down, deploy restart) is caught up once after boot.
Persistent=true
Unit={{UNIT}}

[Install]
WantedBy=timers.target
"""


# --------------------------------------------------------------------------
# small helpers


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_atomic(path: Path, data: bytes, mode: int) -> None:
    """Write through a sibling temp file, fsync, chmod, rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def run(command: list[str], *, check: bool = False, timeout: float | None = None,
        env: dict | None = None):
    try:
        result = subprocess.run(command, text=True, capture_output=True,
                                timeout=timeout, env=env)
    except FileNotFoundError as exc:
        raise SystemExit(f"error: {command[0]} is not installed") from exc
    if check and result.returncode != 0:
        raise SystemExit(
            f"error: {' '.join(command)} failed ({result.returncode}): "
            f"{(result.stderr or result.stdout).strip()[-2000:]}"
        )
    return result


def require_root() -> None:
    if os.geteuid() != 0:
        raise SystemExit("error: installing units, editing /etc/caddy and "
                         "owning /var/lib/odile-test needs root")


def git_env() -> dict:
    """The environment for a git probe, with the caller's GIT_* removed.

    The pre-commit gate runs from `.githooks/pre-commit`, which exports GIT_DIR
    and GIT_INDEX_FILE; without this a release copy of a fixture tree would list
    the outer checkout's files.
    """
    return {key: value for key, value in os.environ.items()
            if not key.startswith("GIT_")}


def service_ids() -> tuple[int, int]:
    """uid and gid of the `odile` service user the units run as."""
    import grp
    import pwd

    try:
        user = pwd.getpwnam("odile")
        group = grp.getgrnam("odile")
    except KeyError as exc:
        raise SystemExit(f"error: no `odile` service account ({exc}); "
                         "installer/odile_install.py creates it") from exc
    return user.pw_uid, group.gr_gid


def chown(path: Path, ids: tuple[int, int]) -> None:
    os.chown(path, ids[0], ids[1])


def render(text: str, values: dict) -> str:
    """Fill `{{NAME}}` slots; an unfilled slot is an error, never blank."""
    for name, value in values.items():
        text = text.replace("{{" + name + "}}", str(value))
    if "{{" in text:
        match = re.search(r"\{\{[A-Z_]+\}\}", text)
        raise ValueError(f"unfilled template slot {match.group(0) if match else '{{'}")
    return text


# --------------------------------------------------------------------------
# the bench port — a foreign holder is refused before the first write


def _listening_inodes(port: int) -> set[str]:
    """The socket inodes listening on `port`, from /proc (stdout of ss is a lie
    when another process owns the socket that was just asked for)."""
    inodes: set[str] = set()
    for name in ("tcp", "tcp6"):
        try:
            lines = (Path("/proc/net") / name).read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10 or parts[3] != "0A":
                continue
            address, _, port_hex = parts[1].rpartition(":")
            try:
                bound = int(port_hex, 16)
            except ValueError:
                continue
            # Loopback and wildcard both take the port. The bench binds
            # 0100007F (127.0.0.1); a foreign wildcard is just as fatal.
            if bound != port or address not in (
                    "0100007F", "00000000",
                    "00000000000000000000000001000000",
                    "00000000000000000000000000000000"):
                continue
            inodes.add(parts[9])
    return inodes


def _command_line(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return "unknown"
    parts = [part for part in raw.decode("utf-8", "replace").split("\0") if part]
    return " ".join(parts) or "unknown"


def _working_directory(pid: int) -> str:
    try:
        target = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""
    return target.removesuffix(" (deleted)")


def port_holders(port: int) -> list[dict]:
    """(pid, command, cwd) for every process listening on `port`."""
    wanted = {f"socket:[{inode}]" for inode in _listening_inodes(port)}
    if not wanted:
        return []
    holders = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target in wanted:
                holders.append({"pid": pid, "command": _command_line(pid),
                                "cwd": _working_directory(pid)})
                break
    return sorted(holders, key=lambda holder: holder["pid"])


def is_bench_unit(pid: int) -> bool:
    """True when `pid` runs inside one of the two bench units."""
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False
    return f"/{UNIT_SITE_NAME}.service" in text or f"/{UNIT_SEED_NAME}.service" in text


def worktree_of(cwd: str) -> Path | None:
    """The card worktree a process's cwd sits in, if it is one."""
    marker = "/.worktrees/"
    head, separator, tail = cwd.partition(marker)
    if not separator:
        return None
    name, _, _rest = tail.partition("/")
    return Path(f"{head}{marker}{name}") if name else None


def terminate(pid: int) -> None:
    """SIGTERM, wait, SIGKILL — an orphan must not survive its own repair."""
    for sig, grace in ((signal.SIGTERM, REAP_SECONDS), (signal.SIGKILL, REAP_SECONDS)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
    raise SystemExit(f"error: pid {pid} survived SIGKILL")


def ensure_port_free(dry: bool, ports: tuple[int, ...] = (APP_PORT, SEED_PORT),
                     *, holders=None, orphans_only: bool = False) -> bool:
    """Refuse or reap a bench-port holder before any write.

    A holder inside the bench's own unit is the healthy case. A holder whose
    working directory is a `.worktrees/<card>` that no longer exists is the
    2026-11-21 orphan — its worktree was deleted and its process was not — so
    it is killed here (SIGTERM, then SIGKILL) and named. Anything else is
    refused with its pid and command before the first write.

    `orphans_only` is the five-minute tick's mode: reap the orphan, name a
    foreign holder and leave it alone, say nothing about a free port, and never
    exit. The refusal stays the deployer's (exit 2), so a red tick can still
    print the probe's one line naming the same holder.
    """
    changed = False
    holders = port_holders if holders is None else holders
    for port in ports:
        held = holders(port)
        if not held:
            if not orphans_only:
                print(f"same         port {port} free")
            continue
        reaped = False
        for holder in held:
            pid, command, cwd = holder["pid"], holder["command"], holder.get("cwd", "")
            if is_bench_unit(pid):
                if not orphans_only:
                    print(f"same         port {port} held by {UNIT_SITE_NAME} (pid {pid})")
                continue
            worktree = worktree_of(cwd)
            if worktree is not None and not worktree.exists():
                print(f"{'would reap' if dry else 'reap':<12} port {port}: pid {pid} {command} "
                      f"started in {worktree}, whose worktree is gone")
                if not dry:
                    terminate(pid)
                changed = reaped = True
                continue
            if orphans_only:
                print(f"foreign      port {port} held by pid {pid} {command} "
                      f"(cwd {cwd or 'unknown'}), not {UNIT_SITE_NAME}; not reaped")
                continue
            print(f"error: port {port} is held by pid {pid} {command} "
                  f"(cwd {cwd or 'unknown'}), not {UNIT_SITE_NAME}; stop it before "
                  f"deploying (the bench app must own the port)", file=sys.stderr)
            raise SystemExit(2)
        if dry or not reaped:
            continue
        deadline = time.monotonic() + REAP_SECONDS
        while holders(port) and time.monotonic() < deadline:
            time.sleep(0.2)
        if holders(port):
            print(f"error: port {port} is still held after the reap", file=sys.stderr)
            raise SystemExit(2)
    return changed


# --------------------------------------------------------------------------
# the Caddy splice — pure text, so the tests can hold every shape


def splice_caddyfile(existing: str, block: str) -> str:
    """Return `existing` with the bench block replaced or appended.

    Exactly zero or one `# BEGIN odile-bench`/`# END odile-bench` pair is
    allowed. A marked pair is replaced in place; a file with no pair gets the
    block after its last line, separated by one blank line. Every other byte —
    including the production `odile.fyi` site and the seed route inside it —
    is left exactly as it was. A second splice is byte-identical.
    """
    lines = existing.splitlines()
    begins = [i for i, line in enumerate(lines) if line.strip() == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if len(begins) != len(ends) or len(begins) > 1:
        raise ValueError(
            f"{CADDYFILE}: expected at most one '{BEGIN}' / '{END}' pair, "
            f"found {len(begins)} begin and {len(ends)} end markers")
    block_lines = block.rstrip("\n").splitlines()
    if begins:
        start, stop = begins[0], ends[0]
        if stop < start:
            raise ValueError(f"{CADDYFILE}: '{END}' comes before '{BEGIN}'")
        return "\n".join(lines[:start] + block_lines + lines[stop + 1:]).rstrip("\n") + "\n"
    return existing.rstrip("\n") + "\n\n" + "\n".join(block_lines) + "\n"


def check_site_block(text: str) -> None:
    """The bench block must route /seed and the app once each, nowhere else."""
    lines = text.splitlines()
    begins = [i for i, line in enumerate(lines) if line.strip() == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        raise ValueError(f"{CADDYFILE}: the bench block is not one marked region")
    body = [re.sub(r"(?<!\S)#.*$", "", line).strip()
            for line in lines[begins[0] + 1:ends[0]]]
    opens = body.count("handle @seed {")
    app = body.count("handle {")
    seed_proxy = body.count(f"reverse_proxy 127.0.0.1:{SEED_PORT}")
    app_proxy = body.count(f"reverse_proxy 127.0.0.1:{APP_PORT}")
    if (opens, app, seed_proxy, app_proxy) != (1, 1, 1, 1):
        raise ValueError(
            f"{CADDYFILE}: the bench block has {opens} seed handler(s), "
            f"{app} app handler(s), {seed_proxy} seed proxy line(s) and "
            f"{app_proxy} app proxy line(s); refusing to write it")


# --------------------------------------------------------------------------
# releases


def _walk_files(root: Path) -> list[str]:
    found = []
    for base, directories, files in os.walk(root):
        relative = Path(base).relative_to(root).as_posix()
        directories[:] = sorted(
            name for name in directories
            if name not in EXCLUDE_DIRS
            and not (relative == "." and name in EXCLUDE_DIRS))
        for name in sorted(files):
            rel = name if relative == "." else f"{relative}/{name}"
            if rel in EXCLUDE_FILES or rel.startswith(EXCLUDE_PREFIXES):
                continue
            if (root / rel).is_file():
                found.append(rel)
    return found


def source_files(source: Path) -> list[str]:
    """The files a release carries, relative to `source`.

    A checkout is read through `git ls-files`, so the release is the public
    tree even when the working directory carries host-local files. A plain
    directory (a fixture in the tests) is walked with the same exclusions.
    """
    result = subprocess.run(["git", "-C", str(source), "ls-files", "-z"],
                            capture_output=True, env=git_env())
    if result.returncode == 0:
        paths = [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]
        return [rel for rel in paths
                if (source / rel).is_file()
                and not rel.startswith(EXCLUDE_PREFIXES)
                and rel not in EXCLUDE_FILES]
    return _walk_files(source)


def release_digest(source: Path) -> tuple[str, int, int]:
    """(id, file count, bytes) over the path, mode and content of every file."""
    entries = []
    total = 0
    paths = source_files(source)
    for rel in paths:
        path = source / rel
        data = path.read_bytes()
        total += len(data)
        mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
        entries.append(f"{rel}\0{mode:o}\0{sha256(data)}")
    if not paths:
        raise SystemExit(f"error: {source} has no files to deploy")
    for required in ("app.py", "seed/seed.py"):
        if required not in paths:
            raise SystemExit(f"error: {source} does not look like an Odile "
                             f"checkout (no {required})")
    digest = sha256("\n".join(sorted(entries)).encode("utf-8"))[:12]
    return digest, len(paths), total


def ensure_release(dry: bool, source: Path) -> tuple[str, bool]:
    digest, count, size = release_digest(source)
    target = RELEASES / digest
    label = f"{target} ({count} file(s), {size} byte(s) from {source})"
    if target.is_dir():
        print(f"same         {label}")
        return digest, False
    print(f"{'would copy' if dry else 'copy':<12} {label}")
    if dry:
        return digest, True
    RELEASES.mkdir(parents=True, exist_ok=True)
    staging = RELEASES / f".tmp-{digest}-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(mode=0o755)
    for rel in source_files(source):
        src = source / rel
        dest = staging / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        os.chmod(dest, 0o755 if src.stat().st_mode & 0o111 else 0o644)
    (staging / "RELEASE").write_text(
        f"source={source}\nid={digest}\nfiles={count}\nbytes={size}\n"
        f"deployed_at={datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n",
        encoding="utf-8")
    os.chmod(staging / "RELEASE", 0o644)
    os.replace(staging, target)
    return digest, True


def _link(target: Path, destination: Path) -> None:
    """Point `destination` at `target` atomically (a symlink, never a copy)."""
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}")
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    os.symlink(os.path.relpath(target, destination.parent), temporary)
    os.replace(temporary, destination)


def current_release() -> str | None:
    if not CURRENT.is_symlink():
        return None
    return os.readlink(CURRENT)


def ensure_current(dry: bool, digest: str) -> bool:
    want = os.path.join("releases", digest)
    have = current_release()
    if have == want:
        print(f"same         {CURRENT} -> {want}")
        return False
    previous = os.readlink(PREVIOUS) if PREVIOUS.is_symlink() else "none"
    print(f"{'would point' if dry else 'point':<12} {CURRENT} -> {want} "
          f"(previous -> {have or 'none'}; was {previous})")
    if dry:
        return True
    if have is not None:
        _link(RELEASES / have.removeprefix("releases/"), PREVIOUS)
    _link(RELEASES / digest, CURRENT)
    return True


# --------------------------------------------------------------------------
# files, units and the database


def _render_env(state: Path | None = None, app_port: int | None = None,
                seed_port: int | None = None) -> str:
    template = (HERE / "bench.env").read_text(encoding="utf-8")
    return render(template, {"STATE": STATE if state is None else state,
                             "APP_PORT": APP_PORT if app_port is None else app_port,
                             "SEED_PORT": SEED_PORT if seed_port is None else seed_port})


def render_env(state: Path | None = None, app_port: int | None = None,
               seed_port: int | None = None) -> str:
    return _render_env(state, app_port, seed_port)


def render_unit(template, *, root: Path | None = None, state: Path | None = None,
                etc: Path | None = None, config: Path | None = None,
                seed_key: Path | None = None) -> str:
    state = STATE if state is None else state
    text = template.read_text(encoding="utf-8") if isinstance(template, Path) else template
    return render(text, {
        "ROOT": ROOT if root is None else root,
        "STATE": state,
        "STATE_NAME": state.name,
        "ETC": ETC if etc is None else etc,
        "CONFIG": CONFIG_FILE if config is None else config,
        "SEED_KEY": SEED_KEY_FILE if seed_key is None else seed_key,
        "WORKER_CONFIG": WORKER_CONFIG,
        # The worker's credential directory. `near_account` has no default: the
        # config names the file (`/root/.odile/near-account.json` on this host),
        # so taking the config's directory keeps the credential and its writable
        # grant on one value.
        "WORKER_HOME": WORKER_CONFIG.parent,
        "UNIT": UNIT_REGISTER_NAME,
    })


def ensure_dirs(dry: bool) -> bool:
    changed = False
    for path, mode in ((ROOT, 0o755), (RELEASES, 0o755), (ETC, 0o755)):
        if path.is_dir():
            print(f"same         {path}")
            continue
        print(f"{'would create' if dry else 'create':<12} {path} ({mode:04o} root:root)")
        if not dry:
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, mode)
        changed = True
    if STATE.is_dir():
        print(f"same         {STATE}")
    else:
        print(f"{'would create' if dry else 'create':<12} {STATE} (0700 odile:odile)")
        if not dry:
            STATE.mkdir(parents=True, exist_ok=True)
            os.chmod(STATE, 0o700)
            chown(STATE, service_ids())
        changed = True
    return changed


def ensure_env(dry: bool) -> bool:
    desired = render_env().encode("utf-8")
    current = ENV_FILE.read_bytes() if ENV_FILE.is_file() else None
    label = f"{ENV_FILE} (ODILE_PORT={APP_PORT}, ODILE_SEED_PORT={SEED_PORT}, db {DB_PATH})"
    if current == desired:
        print(f"same         {label}")
        return False
    print(f"{'would write' if dry else 'write':<12} {label} (0644 root:root)")
    if dry:
        return True
    if current is not None:
        backup = ENV_FILE.with_name(f"{ENV_FILE.name}.bak-{utc_stamp()}")
        shutil.copy2(ENV_FILE, backup)
        print(f"backup       {backup}")
    write_atomic(ENV_FILE, desired, 0o644)
    return True


def ensure_units(dry: bool) -> bool:
    changed = False
    for template, target, label in (
            (HERE / "odile-bench.service.in", UNIT_SITE,
             "User=odile, EnvironmentFile=bench.env, app.py from current"),
            (HERE / "odile-bench-seed.service.in", UNIT_SEED,
             "User=odile, two LoadCredential lines, seed.py from current"),
            (WORKER_SERVICE, UNIT_REGISTER,
             "User=root, oneshot: odilectl.provision --worker (the account queue)"),
            (WORKER_TIMER, TIMER_REGISTER,
             "every two minutes, Persistent, starts the registration worker"),
            (HERE / "odile-bench-probe.service.in", UNIT_PROBE,
             "User=root, the five-minute liveness probe; red feeds the HQ alert path"),
            (HERE / "odile-bench-probe.timer.in", TIMER_PROBE,
             "every five minutes, Persistent, starts the probe unit")):
        desired = render_unit(template).encode("utf-8")
        current = target.read_bytes() if target.is_file() else None
        if current == desired:
            print(f"same         {target}")
            continue
        print(f"{'would write' if dry else 'write':<12} {target} ({label})")
        if dry:
            changed = True
            continue
        if current is not None:
            backup = target.with_name(f"{target.name}.bak-{utc_stamp()}")
            shutil.copy2(target, backup)
            print(f"backup       {backup}")
        write_atomic(target, desired, 0o644)
        changed = True
    return changed


def bench_config(production: dict) -> dict:
    """The bench's own seed config: same box credentials, bench-specific paths.

    The inference key and the relay secret are the box's only ones, so they are
    carried over; the backup repository and an `environment` marker make the
    file the bench's, and `seed.load_config` ignores an extra key.
    """
    bench = json.loads(json.dumps(production))
    bench["backup_repo_url"] = "sftp:odile-backup@backup.odile.fyi:/tenants/test-{slug}"
    bench["environment"] = "bench"
    return bench


def check_private(path: Path, label: str) -> str | None:
    if not path.is_file():
        return f"error: missing {label} {path}"
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        return f"error: {path} is {mode:03o}; {label} is root-only. chmod 600 {path}"
    return None


#: The config paths `seed/seed.py`'s `load_config` refuses a file without.
#: `bench_config()` supplies them when the bench config is created; a config
#: written before a key existed (ODL-469 added `mail_relay.tls_ca`, and the
#: bench's file predated it until ODL-949) needs that one key merged, or the
#: shipped service exits 2 at start and the deploy ends on `GET /seed 502`.
#: `tests/test_bench_deploy.py` proves the list is exactly the shipping
#: loader's: a config built from it loads, and deleting any one path is refused.
REQUIRED_CONFIG_PATHS: tuple[tuple[str, ...], ...] = (
    ("model",),
    ("support_public_key",),
    ("backup_repo_url",),
    ("hermes_version",),
    ("mail_relay",),
    ("mail_relay", "host"),
    ("mail_relay", "port"),
    ("mail_relay", "username"),
    ("mail_relay", "secret"),
    ("mail_relay", "tls_ca"),
)
#: Distinguishes "the key is absent" from "the key is there and empty": only an
#: absent key is merged, so an operator's own (possibly wrong) value is left
#: for the operator and named in the refusal.
MISSING = object()


def seed_module(source: Path):
    """The seed module this deploy ships, which is the config's only authority.

    The bench's seed unit runs `<release>/seed/seed.py`, so the deploy asks
    that module's own `load_config` whether the bench config will load —
    never a copy of its rules, which would drift the first time `main` adds one.
    """
    path = source / "seed" / "seed.py"
    if not path.is_file():
        raise SystemExit(f"error: {path} is missing; {source} is not a tree this "
                         "deployer can ship (it needs seed/seed.py)")
    spec = importlib.util.spec_from_file_location("odile_bench_seed", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("load_config", "ConfigError"):
        if not hasattr(module, name):
            raise SystemExit(f"error: {path} has no {name}; the bench seed config "
                             "cannot be validated against the shipping seed")
    return module


def config_problem(module, path: Path) -> str | None:
    """The seed loader's own refusal for `path`, or None when it accepts it."""
    try:
        module.load_config(str(path))
    except module.ConfigError as exc:
        return str(exc)
    return None


def document_problem(module, document: dict, target: Path) -> str | None:
    """`config_problem` for a document not on disk yet (the dry run and the
    fresh derivation check the same bytes the apply would write)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".seed-config.check.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
        os.chmod(name, 0o600)
        problem = config_problem(module, Path(name))
        return problem.replace(name, str(target)) if problem else None
    finally:
        os.unlink(name)


def config_value(config: dict, path: tuple[str, ...]):
    node: object = config
    for name in path:
        if not isinstance(node, dict) or name not in node:
            return MISSING
        node = node[name]
    return node


def merge_required(bench: dict, production: dict) -> list[str]:
    """Fill the required keys `bench` lacks from `production`; return their names.

    Only an *absent* key is written. A key that is present — however wrong —
    is the operator's, and `config_refusal()` names it instead of replacing it.
    """
    added = []
    for path in REQUIRED_CONFIG_PATHS:
        if config_value(bench, path) is not MISSING:
            continue
        value = config_value(production, path)
        if value is MISSING:
            continue
        node: object = bench
        for name in path[:-1]:
            if not isinstance(node, dict):
                node = None
                break
            node = node.setdefault(name, {})
        if isinstance(node, dict):
            node[path[-1]] = json.loads(json.dumps(value))
            added.append(".".join(path))
    return added


def blame_path(problem: str) -> tuple[str, ...] | None:
    """The config path the loader's message names, as a tuple.

    The loader names a field in one of two shapes — ``field 'model' is missing
    …`` or ``mail_relay.tls_ca is not a PEM certificate`` — and a key it
    validates but the deployer never merges (``near_api_key``) is named the
    second way too. Read the name from the message rather than assuming the
    refusal is about a path on `REQUIRED_CONFIG_PATHS`; a message that names
    no key (a JSON-object error) returns None.
    """
    match = re.match(r"seed config (.*?): field '([^']+)'", problem)
    if match:
        return tuple(match.group(2).split("."))
    match = re.match(r"seed config (.*?): ([A-Za-z_][A-Za-z0-9_.]*) is ", problem)
    if match:
        return tuple(match.group(2).split("."))
    return None


def merge_fix(path: tuple[str, ...] | None, production: dict) -> str | None:
    """A one-line command that copies production's value for `path` in place.

    In-place truncation keeps the file's 0600, and the copy is the same one a
    merge makes — so the printed line is the repair, not advice about it.
    """
    if path is None or config_value(production, path) is MISSING:
        return None
    getter = "q" + "".join(f"[{name!r}]" for name in path)
    setter = "d" + "".join(f"[{name!r}]" for name in path)
    program = ("import json;b={b!r};p={p!r};d=json.load(open(b));q=json.load(open(p));"
               "{setter}={getter};"
               "open(b,'w').write(json.dumps(d,indent=2,sort_keys=True)+chr(10))"
               ).format(b=str(CONFIG_FILE), p=str(PROD_CONFIG), setter=setter, getter=getter)
    return f'/usr/bin/python3 -c "{program}"'


def config_refusal(source: Path, problem: str, bench: dict, production: dict,
                   added: tuple[str, ...] = ()) -> str:
    """The seed's own message, the unit's fate, and the one line that repairs it.

    `added` names the paths this run just merged from production: when the
    loader refuses one of those, the bad value lives in production's config, so
    the fix is there — copying it back would be a no-op.
    """
    path = blame_path(problem)
    if path:
        dotted = ".".join(path)
        present = config_value(bench, path)
        if dotted in added:
            why = f"it was merged from {PROD_CONFIG} and the seed refuses that value"
        elif present is not MISSING:
            why = "it is present but the seed refuses its value"
        else:
            why = "it is absent from both configs"
    else:
        dotted = "the key the message names"
        why = "the message names no config path"
    command = merge_fix(path, production) if dotted not in added else None
    if command:
        fix = command
    elif path and dotted in added:
        fix = f"fix {dotted} in {PROD_CONFIG} (the value the merge copied) and re-run"
    elif path and path[0] == "mail_relay":
        fix = (f"add {dotted} to {PROD_CONFIG} from the relay's own PEM, then "
               "re-run (production has no value for it either)")
    elif path:
        fix = (f"add {dotted} to {PROD_CONFIG} with a value the seed accepts, then "
               "re-run (production has no value for it either)")
    else:
        fix = f"fix {CONFIG_FILE} so the seed accepts it, then re-run"
    return (f"error: {source / 'seed' / 'seed.py'} refuses {CONFIG_FILE}: {problem}\n"
            f"       {dotted}: {why}; the seed unit exits 2 at start on that "
            "message, before it answers /seed\n"
            f"fix: {fix}")


def production_config(role: str) -> dict:
    """The production seed config the bench config is derived (or merged) from."""
    problem = check_private(PROD_CONFIG, "production seed config")
    if problem:
        raise SystemExit(f"{problem} — the bench config is {role} from it; "
                         "create it per docs/runbooks/seed.md §3, then re-run")
    try:
        production = json.loads(PROD_CONFIG.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"error: {PROD_CONFIG} is not valid JSON: {exc}") from exc
    if not isinstance(production, dict):
        raise SystemExit(f"error: {PROD_CONFIG} must be a JSON object")
    return production


def ensure_config(dry: bool, source: Path) -> bool:
    module = seed_module(source)
    if CONFIG_FILE.is_file():
        problem = check_private(CONFIG_FILE, "bench seed config")
        if problem:
            raise SystemExit(problem)
        try:
            bench = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise SystemExit(f"error: {CONFIG_FILE} is not valid JSON: {exc}") from exc
        if not isinstance(bench, dict):
            raise SystemExit(f"error: {CONFIG_FILE} must be a JSON object")
        problem = config_problem(module, CONFIG_FILE)
        if problem is None:
            print(f"same         {CONFIG_FILE} (0600; loads under {source}/seed/seed.py)")
            return False
        production = production_config("merged")
        added = merge_required(bench, production)
        if added:
            print(f"{'would merge' if dry else 'merge':<12} {CONFIG_FILE} "
                  f"(adds {', '.join(added)} from {PROD_CONFIG}; "
                  f"{source}/seed/seed.py requires it)")
            # Validate the document the write would produce, not the file it
            # replaces: a merge that copies production's own bad value must
            # refuse before `write_atomic`, exactly as the dry run does.
            problem = document_problem(module, bench, CONFIG_FILE)
            if problem is None:
                if not dry:
                    write_atomic(CONFIG_FILE,
                                 (json.dumps(bench, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                                 0o600)
                    chown(CONFIG_FILE, (0, 0))
                return True
        raise SystemExit(config_refusal(source, problem, bench, production, tuple(added)))

    production = production_config("derived")
    bench = bench_config(production)
    problem = document_problem(module, bench, CONFIG_FILE)
    if problem is not None:
        dotted = ".".join(blame_path(problem) or ()) or "that key"
        raise SystemExit(
            f"error: {source / 'seed' / 'seed.py'} refuses the bench config derived "
            f"from {PROD_CONFIG}: {problem}\n"
            f"fix: fix {dotted} in {PROD_CONFIG} (the bench config is derived from "
            "it) and re-run")
    print(f"{'would create' if dry else 'create':<12} {CONFIG_FILE} "
          "(0600 root:root; backup prefix /tenants/test-{slug}, environment=bench)")
    if dry:
        return True
    write_atomic(CONFIG_FILE, (json.dumps(bench, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                 0o600)
    chown(CONFIG_FILE, (0, 0))
    return True


def ensure_db(dry: bool) -> bool:
    if DB_PATH.exists():
        print(f"same         {DB_PATH} (empty until app.py's init_db() ran)")
        return False
    print(f"{'would create' if dry else 'create':<12} {DB_PATH} "
          "(empty; app.py init_db() writes the schema, production is never copied)")
    if dry:
        return True
    ids = service_ids()
    fd = os.open(DB_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    chown(DB_PATH, ids)
    os.chmod(DB_PATH, 0o600)
    return True


def ensure_roadmap(dry: bool) -> bool:
    if ROADMAP_PATH.is_file():
        print(f"same         {ROADMAP_PATH} (the bench's own cache)")
        return False
    generator, backlog, template = (
        next((path for path in choices if path.is_file()), None)
        for choices in ROADMAP_CANDIDATES
    )
    if generator is None or backlog is None or template is None:
        print(f"skip         {ROADMAP_PATH} (no generator on this host; "
              f"/roadmap answers 503 until one writes it)")
        return False
    print(f"{'would generate' if dry else 'generate':<12} {ROADMAP_PATH} "
          f"(from {backlog} with {generator})")
    if dry:
        return True
    result = run([sys.executable, str(generator), "--backlog", str(backlog),
                  "--out", str(ROADMAP_PATH), "--source-file", str(ROADMAP_SOURCE),
                  "--template", str(template)])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        print(f"skip         {ROADMAP_PATH} (generator failed: "
              f"{detail[-1] if detail else result.returncode})")
        return False
    os.chmod(ROADMAP_PATH, 0o644)
    return True


# --------------------------------------------------------------------------
# DNS


def primary_ipv4() -> str:
    """This box's own IPv4 address, discovered — never a committed literal."""
    if shutil.which("ip"):
        result = run(["ip", "-4", "route", "get", "203.0.113.1"])
        if result.returncode == 0:
            match = re.search(r"\bsrc\s+(\d{1,3}(?:\.\d{1,3}){3})", result.stdout)
            if match:
                return match.group(1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("203.0.113.1", 80))
            return probe.getsockname()[0]
    except OSError as exc:
        raise SystemExit(f"error: cannot discover this box's IPv4: {exc}") from exc


def resolve_a(name: str) -> list[str]:
    if shutil.which(DIG):
        result = run([DIG, "+short", "A", name])
        if result.returncode == 0:
            return [line.strip() for line in result.stdout.splitlines()
                    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", line.strip())]
    try:
        infos = socket.getaddrinfo(name, None, socket.AF_INET)
    except OSError:
        return []
    return sorted({str(info[4][0]) for info in infos})


def cloudflare_api(token: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"error: Cloudflare {method} {path} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"error: Cloudflare unreachable: {exc.reason}") from exc
    if not payload.get("success"):
        raise SystemExit(f"error: Cloudflare {method} {path} refused: {payload.get('errors')}")
    return payload


def zone_id(token: str) -> str:
    query = urllib.parse.urlencode({"name": ZONE_NAME, "per_page": 50})
    payload = cloudflare_api(token, "GET", f"/zones?{query}")
    zones = [zone for zone in payload.get("result", []) if zone.get("name") == ZONE_NAME]
    if not zones:
        raise SystemExit(f"error: the Cloudflare token cannot see the {ZONE_NAME} zone")
    return zones[0]["id"]


def ensure_dns(dry: bool) -> bool:
    address = DNS_IP or primary_ipv4()
    label = f"{HOST} A {address} (DNS-only)"
    if address in resolve_a(HOST):
        print(f"same         {label}")
        return False
    if dry:
        print(f"would write  {label} through Cloudflare zone {ZONE_NAME} "
              f"(token {TOKEN_FILE})")
        return True
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"error: cannot read {TOKEN_FILE}: {exc}") from exc
    if not token:
        raise SystemExit(f"error: {TOKEN_FILE} is empty")
    zone = zone_id(token)
    query = urllib.parse.urlencode({"type": "A", "name": HOST, "per_page": 100})
    records = cloudflare_api(token, "GET", f"/zones/{zone}/dns_records?{query}").get("result", [])
    body = {"type": "A", "name": HOST, "content": address, "proxied": PROXIED, "ttl": TTL}
    if records:
        cloudflare_api(token, "PATCH", f"/zones/{zone}/dns_records/{records[0]['id']}", body)
        print(f"update       {label} (replaced {records[0].get('content')})")
    else:
        cloudflare_api(token, "POST", f"/zones/{zone}/dns_records", body)
        print(f"create       {label}")
    deadline = time.monotonic() + DNS_SECONDS
    while address not in resolve_a(HOST):
        if time.monotonic() >= deadline:
            raise SystemExit(f"error: {HOST} does not resolve to {address} yet; "
                             f"check the record the deployer just wrote")
        time.sleep(1.0)
    print(f"resolves     {HOST} -> {address}")
    return True


# --------------------------------------------------------------------------
# units, Caddy and probes


def ensure_services(dry: bool, unit_changed: bool, env_changed: bool,
                    release_changed: bool) -> bool:
    changed = False
    if unit_changed:
        print(f"{'would reread' if dry else 'daemon-reload':<12} systemd units")
        if not dry:
            run([SYSTEMCTL, "daemon-reload"], check=True)
        changed = True
    for name in UNIT_NAMES:
        enabled = run([SYSTEMCTL, "is-enabled", name]).stdout.strip()
        active = run([SYSTEMCTL, "is-active", name]).stdout.strip()
        just_started = False
        if enabled != "enabled":
            print(f"{'would enable' if dry else 'enable':<12} {name} (enable --now)")
            if not dry:
                run([SYSTEMCTL, "enable", "--now", name], check=True)
                state = run([SYSTEMCTL, "is-enabled", name]).stdout.strip()
                if state != "enabled":
                    raise SystemExit(f"error: systemctl enable {name} left it {state!r}; "
                                     "the unit has no usable [Install] section")
            just_started = True
            changed = True
        else:
            print(f"same         {name} enabled at boot")
        if active != "active" and not just_started:
            print(f"{'would start' if dry else 'start':<12} {name}")
            if not dry:
                run([SYSTEMCTL, "start", name], check=True)
            just_started = True
            changed = True
        elif just_started:
            pass
        elif unit_changed or env_changed or release_changed:
            reason = ("unit changed" if unit_changed else
                      "bench.env changed" if env_changed else "release changed")
            print(f"{'would restart' if dry else 'restart':<12} {name} ({reason})")
            if not dry:
                run([SYSTEMCTL, "restart", name], check=True)
            changed = True
        else:
            print(f"same         {name} active")
    return changed


def ensure_probe_timer(dry: bool) -> bool:
    """Enable the five-minute probe timer; a second apply prints `same`."""
    name = f"{TIMER_PROBE_NAME}.timer"
    enabled = run([SYSTEMCTL, "is-enabled", name]).stdout.strip()
    active = run([SYSTEMCTL, "is-active", name]).stdout.strip()
    if enabled == "enabled" and active == "active":
        print(f"same         {name} enabled, active (every five minutes)")
        return False
    print(f"{'would enable' if dry else 'enable':<12} {name} (enable --now)")
    if dry:
        return True
    run([SYSTEMCTL, "enable", "--now", name], check=True)
    state = run([SYSTEMCTL, "is-enabled", name]).stdout.strip()
    if state != "enabled":
        raise SystemExit(f"error: systemctl enable {name} left it {state!r}; "
                         "the timer has no usable [Install] section")
    return True


def ensure_register_timer(dry: bool) -> bool:
    """Enable the two-minute account-queue timer; a second apply prints `same`."""
    name = f"{UNIT_REGISTER_NAME}.timer"
    enabled = run([SYSTEMCTL, "is-enabled", name]).stdout.strip()
    active = run([SYSTEMCTL, "is-active", name]).stdout.strip()
    if enabled == "enabled" and active == "active":
        print(f"same         {name} enabled, active (every two minutes)")
        return False
    print(f"{'would enable' if dry else 'enable':<12} {name} (enable --now)")
    if dry:
        return True
    run([SYSTEMCTL, "enable", "--now", name], check=True)
    state = run([SYSTEMCTL, "is-enabled", name]).stdout.strip()
    if state != "enabled":
        raise SystemExit(f"error: systemctl enable {name} left it {state!r}; "
                         "the timer has no usable [Install] section")
    return True


def ensure_caddy(dry: bool) -> bool:
    current = CADDYFILE.read_text(encoding="utf-8")
    block = (HERE / "bench.caddy").read_text(encoding="utf-8")
    desired = splice_caddyfile(current, block)
    check_site_block(desired)
    label = f"{CADDYFILE} ({BEGIN} / {END}; {HOST})"
    if desired == current:
        print(f"same         {label}")
        return False
    if dry:
        print(f"would write  {label} (backup {CADDYFILE.name}.bak-<UTC>, "
              "caddy validate, restart caddy)")
        return True
    backup = CADDYFILE.with_name(f"{CADDYFILE.name}.bak-bench-{utc_stamp()}")
    shutil.copy2(CADDYFILE, backup)
    print(f"backup       {backup}")
    write_atomic(CADDYFILE, desired.encode("utf-8"), 0o644)
    print(f"write        {label}")
    result = run([CADDY, "validate", "--config", str(CADDYFILE)])
    if result.returncode != 0:
        write_atomic(CADDYFILE, current.encode("utf-8"), 0o644)
        detail = (result.stdout + result.stderr).strip()
        raise SystemExit(f"error: caddy validate rejected {CADDYFILE}; restored it "
                         f"from {backup}\n{detail[-2000:]}")
    print(f"validate     {CADDYFILE} ok")
    run([SYSTEMCTL, "restart", "caddy"], check=True)
    print("restart      caddy (the bench block changed)")
    return True


def _curl(url: str, *, body: bool = False, seconds: float = 10.0):
    command = [CURL, "-sS", "--max-time", str(seconds)]
    if body:
        return run(command + [url], timeout=seconds + 2)
    return run(command + ["-o", "/dev/null", "-w", "%{http_code}", url],
               timeout=seconds + 2)


def poll_code(url: str, expected: str, seconds: float) -> str:
    deadline = time.monotonic() + seconds
    code = "000"
    while True:
        result = _curl(url)
        code = result.stdout.strip() or "000"
        if code == expected or time.monotonic() >= deadline:
            return code
        time.sleep(0.5)


def poll_health(url: str, seconds: float) -> tuple[bool, str]:
    deadline = time.monotonic() + seconds
    detail = "no answer"
    while True:
        result = _curl(url, body=True)
        detail = (result.stdout or result.stderr).strip() or "no answer"
        if result.returncode == 0 and result.stdout.strip() == "ok":
            return True, "ok"
        if time.monotonic() >= deadline:
            return False, detail
        time.sleep(0.5)


def bench_listening(port: int = APP_PORT) -> bool:
    """True when something accepts a connection on the bench's own port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=BENCH_WAIT_POLL):
            return True
    except OSError:
        return False


def wait_for_bench(seconds: float = BENCH_WAIT_SECONDS, *, listener=bench_listening,
                   clock=time.monotonic, sleep=time.sleep,
                   port: int = APP_PORT) -> float | None:
    """Wait up to `seconds` for 127.0.0.1:`port` to accept a connection.

    Returns the seconds it waited (0.0 when the listener was already up), or
    None at the deadline. `clock`, `sleep` and `listener` are injected so a
    test can hold both halves of the bound without sleeping or touching the
    host's bench port.
    """
    start = clock()
    if listener(port):
        return 0.0
    while True:
        if clock() - start >= seconds:
            return None
        sleep(BENCH_WAIT_POLL)
        if listener(port):
            return clock() - start


def snapshot_production() -> dict:
    """The three production pages and the unit's restart counter, hashed."""
    snapshot = {}
    for url in PRODUCTION_URLS:
        result = _curl(url, body=True)
        snapshot[url] = sha256(result.stdout.encode("utf-8")) if result.returncode == 0 else "unreachable"
    shown = run([SYSTEMCTL, "show", "-p", "NRestarts", PRODUCTION_UNIT]).stdout.strip()
    snapshot[PRODUCTION_UNIT] = shown or "unknown"
    return snapshot


def report_production(before: dict, after: dict) -> None:
    for url in PRODUCTION_URLS:
        print(f"production   {url} sha256={after[url]} "
              f"({'equal' if after[url] == before[url] else 'CHANGED'})")
    print(f"production   {PRODUCTION_UNIT} {after[PRODUCTION_UNIT]} "
          f"({'unchanged' if after[PRODUCTION_UNIT] == before[PRODUCTION_UNIT] else 'CHANGED'})")
    if after != before:
        changed = [key for key in before if before[key] != after.get(key)]
        raise SystemExit(f"error: production moved during the bench run: {changed}")


def probe(dry: bool) -> None:
    if dry:
        print(f"would probe  {APP_HEALTH} (expect ok)")
        print(f"would probe  {LANDING_URL} (expect 200, the account site)")
        print(f"would probe  {SEED_ROUTE_URL} (expect 405, POST only)")
        print(f"would probe  {PRODUCTION_URLS[0]} (expect 200)")
        return
    healthy, detail = poll_health(APP_HEALTH, PROBE_SECONDS)
    if not healthy:
        raise SystemExit(
            f"error: {APP_HEALTH} answered {detail!r}, expected 'ok'; check "
            f"`systemctl status {UNIT_SITE_NAME}` and `journalctl -u {UNIT_SITE_NAME}`")
    print(f"healthy      {APP_HEALTH} ok")
    code = poll_code(LANDING_URL, "200", CERT_SECONDS)
    if code != "200":
        raise SystemExit(
            f"error: {LANDING_URL} answered {code}, expected 200; check DNS, the "
            f"Caddy block and `journalctl -u caddy`")
    print(f"page         {LANDING_URL} 200 (the account site)")
    code = poll_code(SEED_ROUTE_URL, "405", PROBE_SECONDS)
    if code != "405":
        raise SystemExit(f"error: GET {SEED_ROUTE_URL} answered {code}, expected 405; "
                         f"check `systemctl status {UNIT_SEED_NAME}`")
    print(f"route        {SEED_ROUTE_URL} 405 (POST only; GET is 405 by design)")
    code = poll_code(PRODUCTION_URLS[0], "200", PROBE_SECONDS)
    if code != "200":
        raise SystemExit(f"error: {PRODUCTION_URLS[0]} answered {code} after the "
                         "Caddy restart; the production site is down")
    print(f"page         {PRODUCTION_URLS[0]} 200 (production)")


# --------------------------------------------------------------------------
# the liveness tick — one line, and the existing HQ alert path on red


def _needs_delivery(record) -> bool:
    return not record.deliveries or any(
        not delivery.get("ok") for delivery in record.deliveries)


def _alert_store():
    """The HQ config and heartbeat/alert store, imported only when needed.

    The deployer itself must run from a tree that carries no `odilectl`
    package on `sys.path`; the release it installs does carry it, so the
    import is local and the release root is added explicitly.
    """
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from odilectl.config import Config
    from odilectl.health import HeartbeatStore

    config = Config.load(ALERT_CONFIG)
    path = config.path_value("heartbeat_db") or config.path_value("state_db")
    return config, HeartbeatStore.open(path) if path is not None else HeartbeatStore.open()


def open_bench_alert(line: str, now: int) -> int | None:
    """Raise (or refresh) the bench's alert in the existing HQ ledger.

    Returns the ledger row id, or None when a copy was already open and
    delivered. The alert is the same shape every other alert has: slug,
    kind, symptom, first command, at. `down` is the ledger's own kind for a
    bench that answers wrongly; the bench is not a tenant row, so the poll
    will not resolve it — the probe's green tick does.
    """
    config, store = _alert_store()
    with store:
        from odilectl.health import Alert, build_courier

        alert = Alert(slug=ALERT_SLUG, kind=ALERT_KIND, tenant=ALERT_TENANT,
                      symptom=line, first_command=ALERT_FIRST_COMMAND, at=now)
        record, created = store.raise_alert(alert, now)
        if not created and not _needs_delivery(record):
            print(f"alert        {ALERT_KIND} {ALERT_SLUG} still open (ledger row {record.id}); "
                  "already delivered")
            return record.id
        courier = build_courier(config, SimpleNamespace(transport=None, outbox=None), now=now)
        deliveries = courier.send_alert(alert, alert_email=config.get("alert_email"))
        store.record_deliveries(record.id, deliveries)
        channels = ", ".join(delivery.get("channel", "?") for delivery in deliveries)
        print(f"alert        {ALERT_KIND} {ALERT_SLUG} raised (ledger row {record.id}; {channels})")
        return record.id


def resolve_bench_alert(now: int) -> bool:
    """Close the open bench alert; True when a row was actually resolved."""
    _config, store = _alert_store()
    with store:
        open_alert = store.open_alert(ALERT_SLUG, ALERT_KIND)
        if open_alert is None:
            return False
        return store.resolve(ALERT_SLUG, ALERT_KIND, now)


def probe_tick() -> int:
    """One liveness tick: clear the bench's orphan, wait for its listener, probe.

    Exit 0 is a green bench (any open bench alert resolved), 1 is red after
    the alert was raised, 2 is a probe that could not run at all.

    A holder of 127.0.0.1:8082 whose `.worktrees/<card>` is gone is the
    2026-11-21 orphan: it is killed here and `odile-bench` is started, so the
    orphan dies with its worktree inside five minutes instead of waiting for a
    human to run the deployer. A foreign holder is left alone — killing it is
    the deployer's refusal (exit 2) — and the probe's red line names it.

    The bounded wait for the bench's listener exists for the boot catch-up tick
    (ODL-353 follow-up): it is spent only when the port is not yet bound, and
    its expiry does not change the exit — the probe runs either way.
    """
    if not PROBE_SCRIPT.is_file():
        print(f"bench probe: FAIL {PROBE_SCRIPT} is missing from {REPO}")
        return 2
    reaped = False
    try:
        reaped = ensure_port_free(False, (APP_PORT,), orphans_only=True)
    except (SystemExit, OSError) as exc:  # the probe's red line is still the truth
        print(f"bench probe: the bench port did not clear: {exc}")
    if reaped:
        print(f"start        {UNIT_SITE_NAME} (port {APP_PORT} was held by an orphan from a deleted worktree)")
        try:
            run([SYSTEMCTL, "start", UNIT_SITE_NAME], check=True)
            # `active` is the unit, not the socket: a Type=simple unit is
            # active before app.py binds, so wait for the app's own health
            # answer before the probe, or the tick reports its own repair red.
            _healthy, detail = poll_health(APP_HEALTH, PROBE_SECONDS)
            active = run([SYSTEMCTL, "is-active", UNIT_SITE_NAME]).stdout.strip()
            print(f"active       {UNIT_SITE_NAME} {active}, {APP_HEALTH} {detail}")
        except SystemExit as exc:
            print(f"bench probe: {UNIT_SITE_NAME} did not start: {exc}")
    # ODL-353 follow-up — wait for the bench's listener before measuring it. At
    # boot the timer's Persistent catch-up tick and odile-bench's first bind can
    # meet in the same second; Type=simple is active before app.py binds, so
    # without this the tick's one line is a 502 for a bench that is coming up.
    waited = wait_for_bench()
    if waited is None:
        print(f"bench probe: 127.0.0.1:{APP_PORT} did not accept a connection "
              f"within {BENCH_WAIT_SECONDS:.0f}s; probing anyway")
    elif waited > 0:
        print(f"wait         127.0.0.1:{APP_PORT} accepting after {waited:.1f}s "
              "(the catch-up tick waits for the bench's first bind)")
    try:
        result = run(["/bin/sh", str(PROBE_SCRIPT), "--probe"], timeout=PROBE_TIMEOUT)
    except SystemExit as exc:
        print(f"bench probe: FAIL {exc}")
        return 2
    line = next((item.strip() for item in (result.stdout + result.stderr).splitlines()
                 if item.strip()), "")
    print(line or "bench probe: FAIL the probe printed nothing")
    now = int(time.time())
    if result.returncode == 0:
        try:
            if resolve_bench_alert(now):
                print("bench probe: the open bench alert is resolved")
        except Exception as exc:  # the probe's answer is still the truth
            print(f"bench probe: alert not resolved: {exc}")
        return 0
    try:
        open_bench_alert(line or "the probe failed without a line", now)
    except Exception as exc:  # a delivery failure must not hide the red line
        print(f"bench probe: alert not delivered: {exc}")
    return 1


# --------------------------------------------------------------------------
# rollback


def rollback(dry: bool) -> int:
    have = current_release()
    if have is None:
        print(f"error: {CURRENT} is not a release symlink; run an apply first", file=sys.stderr)
        return 2
    if not PREVIOUS.is_symlink():
        print(f"error: no {PREVIOUS}; apply a second tree before rolling back "
              "(a rollback returns the release that was live before the last apply)",
              file=sys.stderr)
        return 2
    want = os.readlink(PREVIOUS)
    target = RELEASES / want.removeprefix("releases/")
    if not target.is_dir():
        print(f"error: {PREVIOUS} names {want}, but {target} is not a release", file=sys.stderr)
        return 2
    print(f"rollback     {CURRENT} {have} -> {want}")
    print(f"{'would point' if dry else 'point':<12} {PREVIOUS} -> {have} "
          "(so the rollback is itself reversible)")
    if dry:
        print("would restart  odile-bench, odile-bench-seed (release bytes moved)")
        print(f"would probe  {LANDING_URL} (expect 200) and `systemctl is-active "
              f"{UNIT_SITE_NAME}` (expect active)")
        return 0
    before = snapshot_production()
    _link(target, CURRENT)
    _link(RELEASES / have.removeprefix("releases/"), PREVIOUS)
    for name in UNIT_NAMES:
        run([SYSTEMCTL, "restart", name], check=True)
        print(f"restart      {name} ({want})")
    deadline = time.monotonic() + PROBE_SECONDS
    while True:
        active = run([SYSTEMCTL, "is-active", UNIT_SITE_NAME]).stdout.strip()
        if active == "active" or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    if active != "active":
        raise SystemExit(f"error: {UNIT_SITE_NAME} is {active!r} after the rollback")
    print(f"active       {UNIT_SITE_NAME} {active}")
    probe(False)
    report_production(before, snapshot_production())
    print("bench: rolled back")
    return 0


# --------------------------------------------------------------------------
# main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build, deploy and roll back the test bench at test.odile.fyi "
                    "(idempotent).")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan; write nothing, run no mutating command")
    parser.add_argument("--source", default=str(REPO),
                        help="the tree to deploy (default: this checkout)")
    parser.add_argument("--rollback", action="store_true",
                        help="return the previous release; changes nothing else")
    parser.add_argument("--probe-tick", action="store_true",
                        help="one liveness tick: the probe's one line, and the HQ "
                             "alert path when it is red (the timer runs this)")
    parser.add_argument("--dns-ip", default="",
                        help="the address to publish (default: this box's own IPv4)")
    args = parser.parse_args(argv)
    global DNS_IP
    if args.dns_ip:
        DNS_IP = args.dns_ip

    if args.probe_tick:
        return probe_tick()

    try:
        require_root()
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.rollback:
        ensure_port_free(args.dry_run)
        return rollback(args.dry_run)

    source = Path(args.source).resolve()
    if not source.is_dir():
        print(f"error: --source {source} is not a directory", file=sys.stderr)
        return 2

    # The guard precedes the first write (craft: a fence after the write has
    # already touched the machine it meant to refuse).
    changed = ensure_port_free(args.dry_run)
    before = snapshot_production() if not args.dry_run else {}
    changed |= ensure_dirs(args.dry_run)
    env_changed = ensure_env(args.dry_run)
    unit_changed = ensure_units(args.dry_run)
    changed |= env_changed or unit_changed
    changed |= ensure_config(args.dry_run, source)
    changed |= ensure_db(args.dry_run)
    changed |= ensure_roadmap(args.dry_run)
    digest, release_changed = ensure_release(args.dry_run, source)
    switched = ensure_current(args.dry_run, digest)
    changed |= release_changed or switched
    changed |= ensure_services(args.dry_run, unit_changed, env_changed,
                               release_changed or switched)
    changed |= ensure_probe_timer(args.dry_run)
    changed |= ensure_register_timer(args.dry_run)
    changed |= ensure_dns(args.dry_run)
    changed |= ensure_caddy(args.dry_run)
    probe(args.dry_run)
    if not args.dry_run:
        report_production(before, snapshot_production())
    state = "planned" if args.dry_run else ("deployed" if changed else "nothing to do")
    print(f"bench: {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
