#!/usr/bin/python3
"""Odile image bench — the five smoke checks, on the host and in the guest.

The checks are the card's five, stated as behaviour rather than status codes:

  1. gate-anonymous  anonymous HTML `/` -> 302 to /login, anonymous `/api/*`
                     -> 401, `/login` -> 200, and a wrong password -> 401.
  2. gate-session    a correct password login -> 200 and a session cookie;
                     `/` with that cookie -> 200 and the Hermes Webapp shell.
  3. shell           one shell command runs as the unprivileged tenant, and
                     its own Hermes launcher answers with v0.23.0.
  4. persistence     a file written by the tenant survives
                     `systemctl restart odile-agent`, and the restarted
                     gateway answers again as the tenant.
  5. cron            the `bench-heartbeat` Hermes cron job fires once and
                     writes its UTC line.

`local` runs all five in the current root (inside the container, or inside the
QEMU guest). `image` boots a disk under QEMU TCG, lets the guest run `local`,
and adds its own external gate checks through QEMU's host-forwarded port.
`container` does the same through podman. Everything is standard library.

Output contract: one line per check, `ODILE-BENCH <name> <PASS|FAIL> <detail>`,
then `ODILE-BENCH final <PASS|FAIL> ...` and exactly one human line
`image-bench: PASS (5/5)` or `image-bench: FAIL ...`.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import pathlib
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time

PREFIX = "ODILE-BENCH"
PASSWORD_DEFAULT = "odile-bench-not-a-secret"
EXPECTED_VERSION = "Hermes Agent v0.23.0"
WEBAPP_PORT = 9130
SERVICE = "odile-agent"
CHECK_NAMES = ("gate-anonymous", "gate-session", "shell", "persistence", "cron")

# ODL-456: the first-boot service's console line and the first-visit page it
# enables. Both are extra evidence, deliberately not members of CHECK_NAMES:
# the plain image and the container keep their 5/5 verdict.
FIRSTBOOT_MARK = "odile-firstboot:"
FIRSTBOOT_EXPECT = ("installed", "same", "fail-closed")
FIRST_VISIT_NAME = "first-visit"
FIRST_VISIT_PORT = 8091

# ODL-472: the signed manifest the guest serves at /manifest, read through the
# host-forwarded port. A tampered pair must leave nothing answering.
MANIFEST_NAME = "manifest"
MANIFEST_REFUSED_NAME = "manifest-refused"
MANIFEST_PORT = 8012

TENANT_USER = os.environ.get("ODILE_TENANT_USER", "tenant")
TENANT_HOME = pathlib.Path(os.environ.get("ODILE_TENANT_HOME", f"/home/{TENANT_USER}"))
LAUNCHER = TENANT_HOME / ".hermes/hermes-agent/.hermes/bin/hermes"

# ODL-335: on this host /tmp is a 10 GB tmpfs — RAM, not disk — and `df /`
# never shows it. On 2026-11-12 a 7.58 GB raw image left in /tmp plus an 8 GB
# QEMU guest drove the 14 GB host into global OOM three times (19:30:58,
# 20:10:40, 20:47:30), and every managed worker died with the gateway. Images
# and their serial logs belong on the volume, under /var/tmp.
TMPFS_ROOT = pathlib.Path("/tmp")
TMPFS_HINT = ("tmpfs is RAM: a 1 GB file there costs 1 GB of host memory and "
              "`df /` does not show it; put the image and the bench output under /var/tmp")


def warn_tmpfs(*paths: pathlib.Path | str) -> list[pathlib.Path]:
    """One stderr line per bench artefact that lands on the host's tmpfs.

    The bench still runs — a serial log of a few KB under /tmp is harmless —
    but the line names the path and says where multi-GB bytes belong.
    """
    offenders: list[pathlib.Path] = []
    for path in paths:
        candidate = pathlib.Path(path).resolve()
        if candidate != TMPFS_ROOT and TMPFS_ROOT not in candidate.parents:
            continue
        print(f"smoke: {path} is on {TMPFS_ROOT} — {TMPFS_HINT}", file=sys.stderr)
        offenders.append(candidate)
    return offenders


class Check:
    __slots__ = ("name", "ok", "detail")

    def __init__(self, name: str, ok: bool, detail: str) -> None:
        self.name = name
        self.ok = ok
        self.detail = detail

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


class Response:
    def __init__(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def header(self, name: str) -> str:
        name = name.lower()
        for key, value in self.headers:
            if key.lower() == name:
                return value
        return ""

    def cookie_header(self) -> str:
        pairs = []
        for key, value in self.headers:
            if key.lower() != "set-cookie":
                continue
            pair = value.split(";", 1)[0].strip()
            if "=" in pair:
                pairs.append(pair)
        return "; ".join(pairs)


# ---------------------------------------------------------------------------
# small process / network helpers


def run(command: list[str], *, timeout: float = 120, check: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(command)} failed ({result.returncode}): {result.stderr.strip()[-400:]}")
    return result


def as_tenant(command: str, *, timeout: float = 120) -> subprocess.CompletedProcess:
    """Run one shell command as the tenant, with its own HOME and launcher."""
    return run(
        ["runuser", "-u", TENANT_USER, "--", "env", f"HOME={TENANT_HOME}",
         f"PATH={TENANT_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin",
         "sh", "-lc", command],
        timeout=timeout,
    )


def wait_port(host: str, port: int, timeout: float) -> float:
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return time.monotonic() - started
        except OSError:
            time.sleep(1)
    raise TimeoutError(f"{host}:{port} did not open within {timeout:.0f}s")


def request(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 30,
) -> Response:
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        raw = connection.getresponse()
        data = raw.read()
        return Response(raw.status, list(raw.getheaders()), data)
    finally:
        connection.close()


def retry_call(fetch, *, attempts: int = 3, pause: float = 3.0):
    """Run a network call again when the guest stalls under emulation.

    The QEMU guest has no KVM: its Webapp can pause for tens of seconds while
    the gateway warms up or ticks cron (measured 2026-11-08: a `GET /` that had
    just answered 302 timed out 15 s later, then answered again). A stall is a
    fact of the bench, not an answer from the gate; the check retries and the
    detail line records what the gate actually said.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fetch()
        except (OSError, http.client.HTTPException) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(pause)
    assert last is not None
    raise last


def post_login(host: str, port: int, username: str, password: str, *, timeout: float = 30) -> Response:
    payload = json.dumps({"provider": "basic", "username": username, "password": password}).encode()
    return request(
        host, port, "POST", "/auth/password-login",
        headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        body=payload,
        timeout=timeout,
    )


def ws_ticket(host: str, port: int, cookie: str, *, timeout: float = 30) -> tuple[int, str]:
    """Mint the single-use ticket the browser uses for a WebSocket upgrade."""
    response = request(
        host, port, "POST", "/api/auth/ws-ticket",
        headers={"Cookie": cookie, "Content-Length": "0"},
        timeout=timeout,
    )
    try:
        ticket = json.loads(response.body).get("ticket") or ""
    except (ValueError, AttributeError):
        ticket = ""
    return response.status, ticket


def ws_upgrade_status(host: str, port: int, ticket: str, *, timeout: float = 30) -> int:
    """Return the HTTP status of a WebSocket upgrade to /api/ws?ticket=...

    Raw socket on purpose: http.client cannot read a 101. A gated Webapp must
    answer 101 for a valid ticket, and refuse the same path without one.
    """
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    target = f"/api/ws?ticket={ticket}" if ticket else "/api/ws"
    handshake = (
        f"GET {target} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(handshake.encode("ascii"))
        data = sock.recv(4096)
    first = data.split(b"\r\n", 1)[0].decode("latin-1").split()
    if len(first) < 2 or not first[1].isdigit():
        return 0
    return int(first[1])


def wait_gate(host: str, port: int, timeout: float = 120) -> None:
    """Wait until the gated Webapp answers twice in a row.

    One 302 is not enough on this guest: the first answer can come while the
    gateway is still warming up, and the next request stalls. Two consecutive
    answers two seconds apart is a gate that is actually serving.
    """
    deadline = time.monotonic() + timeout
    last = ""
    good = 0
    while time.monotonic() < deadline:
        try:
            response = request(host, port, "GET", "/", timeout=15)
            if response.status in (200, 302):
                good += 1
                if good >= 2:
                    return
            else:
                good = 0
                last = f"GET / -> {response.status}"
        except (OSError, http.client.HTTPException) as exc:
            good = 0
            last = str(exc)
        time.sleep(2)
    raise TimeoutError(f"the Webapp gate did not answer twice within {timeout:.0f}s ({last})")


# ---------------------------------------------------------------------------
# the five checks


def check_gate(
    host: str,
    port: int,
    username: str,
    password: str,
    *,
    timeout: float = 60,
    attempts: int = 3,
    pause: float = 3.0,
) -> list[Check]:
    def gate_request(method: str, path: str, **kwargs) -> Response:
        return retry_call(
            lambda: request(host, port, method, path, timeout=timeout, **kwargs),
            attempts=attempts, pause=pause,
        )

    root = gate_request("GET", "/")
    location = root.header("location")
    api = gate_request("GET", "/api/sessions")
    login = gate_request("GET", "/login")
    wrong = retry_call(
        lambda: post_login(host, port, username, "definitely-not-the-password", timeout=timeout),
        attempts=attempts, pause=pause,
    )

    anonymous_ok = (
        root.status == 302 and "/login" in location
        and api.status == 401
        and login.status == 200
        and wrong.status == 401
    )
    anonymous_detail = (
        f"GET / -> {root.status} {location or '-'}; GET /api/sessions -> {api.status}; "
        f"GET /login -> {login.status}; wrong password -> {wrong.status}"
    )

    good = retry_call(
        lambda: post_login(host, port, username, password, timeout=timeout),
        attempts=attempts, pause=pause,
    )
    cookie = good.cookie_header()
    shell = gate_request("GET", "/", headers={"Cookie": cookie}) if cookie else None
    body = shell.body if shell else b""
    shell_markers = b'id="root"' in body and b"<title>Hermes</title>" in body
    ticket_status, ticket = ws_ticket(host, port, cookie, timeout=timeout) if cookie else (0, "")
    ws_status = ws_upgrade_status(host, port, ticket, timeout=timeout) if ticket else 0
    ws_anonymous = ws_upgrade_status(host, port, "", timeout=timeout)
    session_ok = (
        bool(cookie) and good.status == 200
        and shell is not None and shell.status == 200 and shell_markers
        and ticket_status == 200 and ws_status == 101 and ws_anonymous != 101
    )
    session_detail = (
        f"POST /auth/password-login -> {good.status} (cookie: {'yes' if cookie else 'no'}); "
        f"GET / with cookie -> {shell.status if shell else '-'} "
        f"(Webapp shell: {'yes' if shell_markers else 'no'}); "
        f"POST /api/auth/ws-ticket -> {ticket_status or '-'}; "
        f"GET /api/ws -> {ws_status or '-'} (anonymous -> {ws_anonymous or '-'})"
    )

    return [
        Check("gate-anonymous", anonymous_ok, anonymous_detail),
        Check("gate-session", session_ok, session_detail),
    ]


def check_shell() -> Check:
    command = (
        "printf 'odile-shell-ok\\n'; "
        '"$HOME/.hermes/hermes-agent/.hermes/bin/hermes" --version'
    )
    result = as_tenant(command, timeout=180)
    output = (result.stdout + result.stderr).strip()
    lines = output.splitlines()
    version_lines = [line for line in lines if EXPECTED_VERSION in line]
    first = version_lines[0] if version_lines else (lines[-1] if lines else "(no output)")
    ok = result.returncode == 0 and "odile-shell-ok" in output and EXPECTED_VERSION in output
    return Check("shell", ok, f"runuser -u {TENANT_USER} -- sh -lc ... -> {first}")


def check_persistence(host: str, port: int) -> Check:
    nonce = secrets.token_hex(8)
    marker = TENANT_HOME / "bench/restart-marker"
    write = as_tenant(f'mkdir -p "$HOME/bench"; printf %s {json.dumps(nonce)} > "$HOME/bench/restart-marker"')
    if write.returncode != 0:
        return Check("persistence", False, f"tenant could not write the marker: {write.stderr.strip()[-300:]}")

    restart = run(["systemctl", "restart", SERVICE], timeout=180)
    if restart.returncode != 0:
        return Check("persistence", False, f"systemctl restart {SERVICE} failed: {restart.stderr.strip()[-300:]}")

    wait_port(host, port, timeout=300)
    wait_gate(host, port, timeout=180)
    survived = marker.is_file() and marker.read_text().strip() == nonce
    active = run(["systemctl", "is-active", SERVICE])

    main_pid = run(["systemctl", "show", "-p", "MainPID", "--value", SERVICE]).stdout.strip()
    owner = ""
    if main_pid.isdigit() and int(main_pid) > 0:
        owner = run(["ps", "-o", "user=", "-p", main_pid]).stdout.strip()

    ok = survived and active.stdout.strip() == "active" and owner == TENANT_USER
    detail = (
        f"marker {'survived' if survived else 'did NOT survive'} systemctl restart {SERVICE}; "
        f"unit {active.stdout.strip() or 'unknown'}; MainPID owner {owner or 'unknown'}"
    )
    return Check("persistence", ok, detail)


def check_cron(wait_seconds: float) -> Check:
    log = TENANT_HOME / "bench/heartbeat.log"
    started = time.monotonic()
    deadline = started + wait_seconds
    while True:
        if log.is_file():
            hits = [line for line in log.read_text(errors="replace").splitlines() if "bench-heartbeat fired" in line]
            if hits:
                waited = time.monotonic() - started
                return Check("cron", True, f"bench-heartbeat fired at {hits[-1].split()[0]} after {waited:.0f}s")
        if time.monotonic() >= deadline:
            return Check("cron", False, f"no bench-heartbeat line in {log} after {wait_seconds:.0f}s")
        time.sleep(5)


def emit(check: Check) -> None:
    verdict = "PASS" if check.ok else "FAIL"
    print(f"{PREFIX} {check.name} {verdict} {check.detail}", flush=True)


# The sealed bench's own marker. It is emitted by `local` when the boot came
# from a sealed UKI, and parsed by `sealed`; it is deliberately not one of the
# five checks, so the plain image and the container keep their 5/5 verdict.
SEALED_STATE = pathlib.Path("/run/bb/sealed-unlock.state")
SEALED_NAME = "sealed-unlock"
SEALED_MAPPER = "root_crypt"


def root_mapper_source(mounts: pathlib.Path = pathlib.Path("/proc/mounts")) -> str:
    """The device string `/` was mounted from ("" when it cannot be read).

    `/proc/mounts` is readable without privilege and is the same answer the
    kernel gave the mount: after switch_root the initramfs's /dev is gone, so
    the mount's source string is what survives. Measured 2026-11-10: the guest
    image has no `cryptsetup` binary, so a `cryptsetup status` check reported
    "inactive" on a boot whose root had just been opened by the hook.
    """
    try:
        text = mounts.read_text(errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "/":
            return fields[0]
    return ""


def mapper_is_active(
    source: str,
    name: str = SEALED_MAPPER,
    root: pathlib.Path = pathlib.Path("/"),
) -> bool:
    """Is `source` the device-mapper mapping called `name`?

    Resolve the mount's device node (`/dev/mapper/root_crypt` is a symlink to
    `/dev/dm-N`) and read `/sys/class/block/<dev>/dm/name`, the name the
    mapper was actually created with. A symlink renamed to `root_crypt` cannot
    fake this, and no userspace tool has to be installed in the guest.
    """
    if not source.startswith("/dev/"):
        return False
    node = root / source.lstrip("/")
    try:
        real = node.resolve()
        return (root / "sys/class/block" / real.name / "dm" / "name").read_text().strip() == name
    except OSError:
        return False


def check_sealed_unlock() -> Check | None:
    """The hook's own state token plus the live mapper, or None on a plain image."""
    state = ""
    if SEALED_STATE.is_file():
        state = SEALED_STATE.read_text(errors="replace").strip()
    source = root_mapper_source()
    active = mapper_is_active(source)
    if not state and not active:
        return None
    where = f"/ is {source}" if source else "/ has no readable source"
    if active:
        where += f" (dm name {SEALED_MAPPER})"
    return Check(SEALED_NAME, state == "opened" and active, f"state {state or 'absent'}; {where}")


def final_line(checks: list[Check]) -> str:
    failed = [check.name for check in checks if not check.ok]
    if not failed:
        return f"image-bench: PASS ({len(checks)}/{len(CHECK_NAMES)})"
    return f"image-bench: FAIL ({len(checks) - len(failed)}/{len(CHECK_NAMES)}) — failed: {', '.join(failed)}"


def verdict(checks: list[Check]) -> int:
    line = final_line(checks)
    passed = all(check.ok for check in checks)
    print(f"{PREFIX} final {'PASS' if passed else 'FAIL'} {line}", flush=True)
    print(line, flush=True)
    return 0 if passed else 1


# ---------------------------------------------------------------------------
# local mode: the whole set in this root


def local(args: argparse.Namespace) -> int:
    started = time.monotonic()
    print(
        f"{PREFIX} environment user={TENANT_USER} home={TENANT_HOME} "
        f"port={args.port} cron-wait={args.cron_wait}s",
        flush=True,
    )
    checks: list[Check] = []
    try:
        boot = wait_port("127.0.0.1", args.port, timeout=args.boot_timeout)
        wait_gate("127.0.0.1", args.port, timeout=args.boot_timeout)
        print(f"{PREFIX} webapp listening after {boot:.0f}s", flush=True)
    except (OSError, TimeoutError) as exc:
        checks = [Check(name, False, str(exc)) for name in CHECK_NAMES]
        for check in checks:
            emit(check)
        elapsed = time.monotonic() - started
        print(f"{PREFIX} elapsed {elapsed:.0f}s", flush=True)
        return verdict(checks)

    producers = (
        ("gate-anonymous", lambda: check_gate("127.0.0.1", args.port, TENANT_USER, args.password)),
        ("shell", lambda: [check_shell()]),
        ("persistence", lambda: [check_persistence("127.0.0.1", args.port)]),
        ("cron", lambda: [check_cron(args.cron_wait)]),
    )
    for fallback_name, producer in producers:
        try:
            produced = producer()
        except Exception as exc:  # one broken check must not hide the other four
            produced = [Check(fallback_name, False, f"{type(exc).__name__}: {exc}")]
        for check in produced:
            emit(check)
            checks.append(check)

    elapsed = time.monotonic() - started
    # Extra evidence, not a sixth check: a plain image has no state token and
    # no root_crypt, so it emits nothing and the 5/5 verdict is unchanged.
    sealed = check_sealed_unlock()
    if sealed is not None:
        emit(sealed)
    print(f"{PREFIX} elapsed {elapsed:.0f}s", flush=True)
    return verdict(checks)


# ---------------------------------------------------------------------------
# guest output


def parse_guest(text: str) -> tuple[dict[str, Check], bool | None]:
    checks: dict[str, Check] = {}
    final: bool | None = None
    for line in text.splitlines():
        if not line.startswith(PREFIX + " "):
            continue
        parts = line.split(" ", 3)
        if len(parts) < 3:
            continue
        if parts[1] == "final":
            final = parts[2] == "PASS"
            continue
        if parts[1] in CHECK_NAMES and parts[2] in ("PASS", "FAIL"):
            detail = parts[3] if len(parts) > 3 else ""
            checks[parts[1]] = Check(parts[1], parts[2] == "PASS", detail)
    return checks, final


def combined_checks(
    host_gate: list[Check],
    guest_checks: dict[str, Check],
    guest_final: bool | None,
    failure_detail: str = "",
) -> list[Check]:
    checks = list(host_gate)
    for name in CHECK_NAMES[2:]:
        if name in guest_checks:
            checks.append(guest_checks[name])
        else:
            checks.append(Check(name, False, failure_detail or "the guest did not report this check"))
    if guest_final is False:
        # The guest's own five-check verdict is part of the evidence: a PASS
        # from the host side alone cannot outvote a failed guest check.
        for check in checks[2:]:
            if check.ok:
                check.ok = False
                check.detail = f"{check.detail} (guest final: FAIL)"
    return checks


def wait_guest_final(serial_path: pathlib.Path, timeout: float) -> tuple[dict[str, Check], bool | None, str]:
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        if serial_path.is_file():
            text = serial_path.read_text(errors="replace")
            checks, final = parse_guest(text)
            if final is not None:
                return checks, final, text
        time.sleep(2)
    if serial_path.is_file():
        text = serial_path.read_text(errors="replace")
    return parse_guest(text)[0], parse_guest(text)[1], text


# ---------------------------------------------------------------------------
# image mode: QEMU TCG


def boot_qemu(
    disk: pathlib.Path,
    serial: pathlib.Path,
    bench_serial: pathlib.Path,
    port: int,
    memory: int,
    cpus: int,
) -> subprocess.Popen:
    disk_format = "qcow2" if disk.suffix == ".qcow2" else "raw"
    warn_tmpfs(disk, serial, bench_serial)
    # Two serial channels on purpose: ttyS0 carries boot and getty, ttyS1
    # carries the bench result. The guest's smoke unit once printed its first
    # line to ttyS0 and nothing after — measured 2026-11-08: the unit exited 1
    # the moment agetty took the console, and the host saw one line of five.
    # A dedicated channel has no getty on it.
    command = [
        "qemu-system-x86_64",
        "-name", "odile-tenant-bench",
        "-machine", "q35",
        "-accel", "tcg,thread=multi",
        "-cpu", "max",
        "-smp", str(cpus),
        "-m", str(memory),
        "-drive", f"file={disk},format={disk_format},if=virtio,cache=writeback",
        "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{port}-:{WEBAPP_PORT}",
        "-device", "virtio-net-pci,netdev=net0",
        "-display", "none",
        "-serial", f"file:{serial}",
        "-serial", f"file:{bench_serial}",
        "-monitor", "none",
        "-no-reboot",
    ]
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)


def stop_qemu(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def image(args: argparse.Namespace) -> int:
    disk = pathlib.Path(args.disk).resolve()
    if not disk.is_file():
        print(f"smoke: no disk image at {disk}", file=sys.stderr)
        return 2
    if not shutil.which("qemu-system-x86_64"):
        print("smoke: qemu-system-x86_64 is missing", file=sys.stderr)
        return 2
    out = pathlib.Path(args.out).resolve() if args.out else disk.parent
    out.mkdir(parents=True, exist_ok=True)
    serial = out / "boot.log"
    bench_serial = out / "bench.log"
    for path in (serial, bench_serial):
        if path.exists():
            path.unlink()

    started = time.monotonic()
    print(f"{PREFIX} boot {disk} under QEMU TCG (no KVM), serial {serial}, bench channel {bench_serial}", flush=True)
    process = boot_qemu(disk, serial, bench_serial, args.port, args.memory, args.cpus)
    boot_seconds = None
    try:
        try:
            boot_seconds = wait_port("127.0.0.1", args.port, timeout=args.boot_timeout)
            print(f"{PREFIX} guest TCP port forwarded after {boot_seconds:.0f}s wall", flush=True)
        except (OSError, TimeoutError) as exc:
            print(f"{PREFIX} guest port: {exc}", flush=True)
        guest_checks, guest_final, _ = wait_guest_final(bench_serial, args.guest_timeout)
        if guest_final is None:
            print(f"{PREFIX} guest smoke did not finish within {args.guest_timeout:.0f}s", flush=True)
        try:
            wait_gate("127.0.0.1", args.port, timeout=180)
            host_gate = check_gate("127.0.0.1", args.port, TENANT_USER, args.password)
        except Exception as exc:
            host_gate = [Check("gate-anonymous", False, f"host check failed: {type(exc).__name__}: {exc}"),
                         Check("gate-session", False, f"host check failed: {type(exc).__name__}: {exc}")]
        checks = combined_checks(host_gate, guest_checks, guest_final,
                                 "the guest smoke did not report this check")
    finally:
        stop_qemu(process)

    for check in checks:
        emit(check)
    elapsed = time.monotonic() - started
    print(f"{PREFIX} boot={boot_seconds or 0:.0f}s total={elapsed:.0f}s serial={serial} bench={bench_serial}", flush=True)
    print(f"{PREFIX} sizes raw={disk.stat().st_size} bytes", flush=True)
    return verdict(checks)


# ---------------------------------------------------------------------------
# sealed mode: a UKI boot under OVMF (the bench has no SEV, but the UKI is the
# artefact the Blackbox would launch and measure)


SEALED_OPENED = "bb-sealed-unlock: root_crypt opened with the SNP-derived key"
# ODL-332: the first boot of a disk written from the published image has no
# blob sealed to the tenant's chip yet (it was built where no AMD PSP exists).
# The hook opens it with the image's bootstrap key and says so; the booted
# system's odile-seal-provision then replaces that key with a chip-sealed one.
# Both lines are "the hook opened the root", so the bench accepts either; a
# prompt after either is still a failure.
SEALED_OPENED_BOOTSTRAP = ("bb-sealed-unlock: root_crypt opened with the "
                           "first-boot bootstrap key")
SEALED_OPENED_LINES = (SEALED_OPENED, SEALED_OPENED_BOOTSTRAP)
SEALED_FAILED = "bb-sealed-unlock: the blob is not sealed to this boot"
CRYPTROOT_PROMPT = "Please unlock disk root_crypt:"


def parse_guest_sealed(text: str) -> Check | None:
    for line in text.splitlines():
        if line.startswith(f"{PREFIX} {SEALED_NAME} "):
            parts = line.split(" ", 3)
            if len(parts) >= 3 and parts[2] in ("PASS", "FAIL"):
                return Check(SEALED_NAME, parts[2] == "PASS", parts[3] if len(parts) > 3 else "")
    return None


def wait_sealed(serial_path: pathlib.Path, expect: str, timeout: float) -> tuple[bool, str]:
    """Watch the boot transcript for the hook's line, or for cryptroot's prompt."""
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        if serial_path.is_file():
            text = serial_path.read_text(errors="replace")
            opened = any(marker in text for marker in SEALED_OPENED_LINES)
            failed = SEALED_FAILED in text
            prompt = CRYPTROOT_PROMPT in text
            if expect == "opened" and opened:
                return True, "bb-sealed-unlock opened root_crypt before cryptroot ran"
            if expect == "prompt" and prompt:
                if opened:
                    return False, "cryptroot prompted even though the hook reported it opened the root"
                if not failed:
                    return False, ("cryptroot prompted but the hook never reported a seal failure: "
                                   "the prompt may be the stock no-hook default, not fail-closed")
                return True, "the changed cmdline failed the seal, the hook left the prompt"
        time.sleep(2)
    if expect == "opened":
        return False, f"no '{SEALED_OPENED}' line in {serial_path} after {timeout:.0f}s"
    return False, f"no '{CRYPTROOT_PROMPT}' line in {serial_path} after {timeout:.0f}s"


def parse_guest_firstboot(text: str) -> tuple[str, str] | None:
    """The last `odile-firstboot:` line, as (state, detail), or None.

    States: `installed`, `same`, `fail-closed`. The line is the whole console
    contract of the unit, so a parse that finds nothing means the unit never
    ran — not that it passed.
    """
    found = None
    for line in text.splitlines():
        if FIRSTBOOT_MARK not in line:
            continue
        detail = line.split(FIRSTBOOT_MARK, 1)[1].strip()
        if detail == "same":
            found = ("same", detail)
        elif detail.startswith("installed"):
            found = ("installed", detail)
        elif detail.startswith("fail-closed:"):
            found = ("fail-closed", detail[len("fail-closed:"):].strip())
        else:
            found = ("unknown", detail)
    return found


def wait_firstboot(serial_path: pathlib.Path, expect: str, timeout: float) -> tuple[bool, str]:
    """Watch the serial transcript for the unit's one line.

    `installed` also requires the page status in the same line: the unit only
    writes `installed` after the first-visit page answered through loopback.
    """
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        if serial_path.is_file():
            text = serial_path.read_text(errors="replace")
            seen = parse_guest_firstboot(text)
            if seen is not None:
                state, detail = seen
                if expect == "installed" and state == "installed":
                    if "first-visit 200" in detail:
                        return True, detail
                    return False, "installed without the page answering: " + detail
                if expect == "same" and state == "same":
                    return True, detail
                if expect == "fail-closed" and state == "fail-closed":
                    return True, detail
                return False, "saw %r, expected %s" % (state, expect)
        time.sleep(2)
    if not text and serial_path.is_file():
        text = serial_path.read_text(errors="replace")
    return False, "no '%s ...' line for %s after %.0fs" % (FIRSTBOOT_MARK, expect, timeout)


def first_visit_page(port: int, token: str, timeout: float = 120) -> Check:
    """The page answers 200 through the host-forwarded first-visit port.

    GET only: the bench never spends the token, so the same disk still opens
    the form on the second boot (ODL-457 spends it on the POST).
    """
    deadline = time.monotonic() + timeout
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            response = request("127.0.0.1", port, "GET", "/first-visit?t=%s" % token,
                               timeout=15)
        except (OSError, http.client.HTTPException) as exc:
            last = str(exc)
            time.sleep(2)
            continue
        body = response.body
        form = (b"<form" in body and b"first-visit" in body
                and (b"sign-in" in body or b"sign in" in body))
        if response.status == 200 and form:
            return Check(FIRST_VISIT_NAME, True,
                         "GET /first-visit -> 200 (form: yes, %d bytes)" % len(body))
        last = "GET /first-visit -> %d (form: %s)" % (response.status, "yes" if form else "no")
        time.sleep(2)
    return Check(FIRST_VISIT_NAME, False, last)


def manifest_served(port: int, expected: bytes, timeout: float = 120,
                    out: pathlib.Path | None = None) -> Check:
    """`/manifest` answers with the signed file's exact bytes, and no-store.

    `expected` is the release's manifest the image carries: the check is byte
    identity, not "some JSON came back", because the whole point of the route
    is that the served document is the signed one. With `out`, the body the
    guest sent is kept beside the transcript, so the sha256 in the evidence is
    the bytes on the wire rather than a claim about them.
    """
    deadline = time.monotonic() + timeout
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            response = request("127.0.0.1", port, "GET", "/manifest", timeout=15)
        except (OSError, http.client.HTTPException) as exc:
            last = "GET /manifest -> no answer (%s)" % type(exc).__name__
            time.sleep(2)
            continue
        cache = response.header("Cache-Control")
        if response.status == 200 and cache.strip().lower() == "no-store":
            if out is not None:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(response.body)
            if response.body == expected:
                return Check(MANIFEST_NAME, True,
                             "GET /manifest -> 200 (cache-control: no-store, %d bytes, "
                             "byte-identical to the signed file)" % len(response.body))
            last = ("GET /manifest -> 200 but the body is a different file "
                    "(sha256 %s)" % hashlib.sha256(response.body).hexdigest()[:12])
        else:
            last = "GET /manifest -> %d (cache-control: %r)" % (response.status, cache)
        time.sleep(2)
    return Check(MANIFEST_NAME, False, last)


def manifest_refused(port: int, attempts: int = 3, pause: float = 2.0) -> Check:
    """`/manifest` answers nothing: the tampered pair never reached a socket.

    The manifest unit verifies in ExecStartPre and exits, so the port stays
    closed. QEMU's hostfwd accepts and then closes the connection, which is a
    refused answer here; any HTTP status means something was served.
    """
    last = "no answer"
    for attempt in range(attempts):
        try:
            response = request("127.0.0.1", port, "GET", "/manifest", timeout=10)
        except (OSError, http.client.HTTPException) as exc:
            last = "no answer (%s)" % type(exc).__name__
            if attempt + 1 < attempts:
                time.sleep(pause)
            continue
        return Check(MANIFEST_REFUSED_NAME, False,
                     "GET /manifest -> %d; a tampered pair was served" % response.status)
    return Check(MANIFEST_REFUSED_NAME, True,
                 "GET /manifest -> %s; the tampered pair never listened" % last)


def boot_qemu_uki(
    disk: pathlib.Path,
    uki: pathlib.Path,
    ovmf: pathlib.Path,
    serial: pathlib.Path,
    bench_serial: pathlib.Path,
    port: int,
    memory: int,
    cpus: int,
    first_visit_port: int = 0,
    manifest_port: int = 0,
) -> subprocess.Popen:
    warn_tmpfs(disk, serial, bench_serial)
    # virtio-scsi, not virtio-blk: the release initramfs and the hook address
    # /dev/sda2 and /dev/sda3 by name, exactly as on a Blackbox.
    forwards = [f"hostfwd=tcp:127.0.0.1:{port}-:{WEBAPP_PORT}"]
    if first_visit_port:
        forwards.append(f"hostfwd=tcp:127.0.0.1:{first_visit_port}-:{FIRST_VISIT_PORT}")
    if manifest_port:
        forwards.append(f"hostfwd=tcp:127.0.0.1:{manifest_port}-:{MANIFEST_PORT}")
    command = [
        "qemu-system-x86_64",
        "-name", "odile-tenant-sealed",
        "-machine", "q35",
        "-accel", "tcg,thread=multi",
        "-cpu", "max",
        "-smp", str(cpus),
        "-m", str(memory),
        "-bios", str(ovmf),
        "-kernel", str(uki),
        "-drive", f"file={disk},format=raw,if=none,id=disk0,cache=writeback",
        "-device", "virtio-scsi-pci,id=scsi0",
        "-device", "scsi-hd,drive=disk0,bus=scsi0.0",
        "-netdev", "user,id=net0," + ",".join(forwards),
        "-device", "virtio-net-pci,netdev=net0",
        "-display", "none",
        "-serial", f"file:{serial}",
        "-serial", f"file:{bench_serial}",
        "-monitor", "none",
        "-no-reboot",
    ]
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)


def sealed(args: argparse.Namespace) -> int:
    disk = pathlib.Path(args.disk).resolve()
    uki = pathlib.Path(args.uki).resolve()
    ovmf = pathlib.Path(args.ovmf).resolve()
    for label, path in (("disk", disk), ("UKI", uki), ("OVMF", ovmf)):
        if not path.is_file():
            print(f"smoke: no {label} at {path}", file=sys.stderr)
            return 2
    if not shutil.which("qemu-system-x86_64"):
        print("smoke: qemu-system-x86_64 is missing", file=sys.stderr)
        return 2
    out = pathlib.Path(args.out).resolve() if args.out else disk.parent
    out.mkdir(parents=True, exist_ok=True)
    serial = out / "sealed-boot.log"
    bench_serial = out / "sealed-bench.log"
    for path in (serial, bench_serial):
        if path.exists():
            path.unlink()

    started = time.monotonic()
    print(f"{PREFIX} sealed boot {uki.name} + {disk.name} under QEMU TCG/OVMF, serial {serial}", flush=True)
    process = boot_qemu_uki(disk, uki, ovmf, serial, bench_serial, args.port, args.memory,
                            args.cpus, args.first_visit_port, args.manifest_port)
    firstboot: Check | None = None
    page: Check | None = None
    manifest: Check | None = None
    try:
        ok, detail = wait_sealed(serial, args.expect, args.unlock_timeout)
        emit(Check(SEALED_NAME, ok, detail))
        if not ok:
            print(f"sealed-bench: FAIL ({detail})", flush=True)
            return 1
        if args.expect == "prompt":
            # The fail-closed boot never reaches the stack; the prompt is the pass.
            print("sealed-bench: PASS (fail-closed: a changed cmdline did not unseal)", flush=True)
            return 0

        boot_seconds = None
        gate_started = time.monotonic()
        try:
            # wait_port returns the moment QEMU binds its hostfwd socket, not
            # the moment the guest serves; the first gate answer is the boot.
            wait_gate("127.0.0.1", args.port, timeout=args.boot_timeout)
            boot_seconds = time.monotonic() - gate_started
            print(f"{PREFIX} guest gate answered after {boot_seconds:.0f}s wall", flush=True)
        except (OSError, TimeoutError) as exc:
            print(f"{PREFIX} guest gate: {exc}", flush=True)
        guest_checks, guest_final, guest_text = wait_guest_final(bench_serial, args.guest_timeout)
        if guest_final is None:
            print(f"{PREFIX} guest smoke did not finish within {args.guest_timeout:.0f}s", flush=True)
        guest_sealed = parse_guest_sealed(guest_text)
        # ODL-456: the first-boot line and the page are evidence from the same
        # boot, read from the boot serial (ttyS0), not the bench channel.
        if args.expect_firstboot:
            fb_ok, fb_detail = wait_firstboot(serial, args.expect_firstboot, args.firstboot_timeout)
            firstboot = Check("firstboot", fb_ok, fb_detail)
            emit(firstboot)
            if not fb_ok:
                print(f"firstboot-bench: FAIL ({fb_detail})", flush=True)
                return 1
            if args.first_visit_port and args.expect_firstboot in ("installed", "same"):
                page = first_visit_page(args.first_visit_port, args.first_visit_token,
                                        timeout=args.boot_timeout)
                emit(page)
                if not page.ok:
                    print(f"firstboot-bench: FAIL ({page.detail})", flush=True)
                    return 1
            if args.manifest_port:
                # ODL-472: the signed manifest the image carries, read over the
                # forwarded guest URL. A tampered pair must answer nothing.
                if args.expect_firstboot in ("installed", "same"):
                    expected = pathlib.Path(args.manifest_file).read_bytes()
                    manifest_out = pathlib.Path(args.manifest_out) if args.manifest_out else None
                    manifest = manifest_served(args.manifest_port, expected,
                                               timeout=args.boot_timeout, out=manifest_out)
                else:
                    manifest = manifest_refused(args.manifest_port)
                emit(manifest)
                if not manifest.ok:
                    print(f"manifest-bench: FAIL ({manifest.detail})", flush=True)
                    return 1
        try:
            wait_gate("127.0.0.1", args.port, timeout=180)
            host_gate = check_gate("127.0.0.1", args.port, TENANT_USER, args.password)
        except Exception as exc:
            host_gate = [Check("gate-anonymous", False, f"host check failed: {type(exc).__name__}: {exc}"),
                         Check("gate-session", False, f"host check failed: {type(exc).__name__}: {exc}")]
        checks = combined_checks(host_gate, guest_checks, guest_final,
                                 "the guest smoke did not report this check")
    finally:
        stop_qemu(process)

    for check in checks:
        emit(check)
    if guest_sealed is None:
        guest_sealed = Check(SEALED_NAME, False, "the guest did not report the hook's state token")
    emit(guest_sealed)
    elapsed = time.monotonic() - started
    print(f"{PREFIX} sealed boot={boot_seconds or 0:.0f}s total={elapsed:.0f}s serial={serial}", flush=True)
    rc = verdict(checks)
    passed = rc == 0 and guest_sealed.ok
    good = len([check for check in checks if check.ok])
    unlock = ("unlocked without a passphrase" if guest_sealed.ok
              else f"sealed-unlock {guest_sealed.detail}")
    extra = ""
    if firstboot is not None:
        extra = "; " + firstboot.detail
        passed = passed and firstboot.ok
        if page is not None:
            extra += "; " + page.detail
            passed = passed and page.ok
        if manifest is not None:
            extra += "; " + manifest.detail
            passed = passed and manifest.ok
    print(f"sealed-bench: {'PASS' if passed else 'FAIL'} "
          f"({unlock}; {good}/{len(CHECK_NAMES)} image checks{extra})", flush=True)
    return 0 if passed else 1


# ---------------------------------------------------------------------------
# container mode: the same stack under podman + systemd


def container(args: argparse.Namespace) -> int:
    name = f"odile-bench-{os.getpid()}"
    started = time.monotonic()
    print(f"{PREFIX} podman run {args.image} (systemd as PID 1)", flush=True)
    subprocess.run(["podman", "rm", "-f", name], capture_output=True)
    launched = run([
        "podman", "run", "-d", "--name", name,
        # systemd inside a rootful container must keep CAP_SYS_ADMIN across the
        # User= switch to set up the unit's mount-namespace sandbox
        # (ProtectSystem=strict, PrivateTmp=yes); podman's default bounding set
        # lacks it, and the unit fails with 217/USER. --privileged is the
        # narrowest podman flag that works. The tenant process is still uid
        # 1000, and the persistence check asserts the MainPID's owner.
        "--privileged", "--systemd=always", "--stop-timeout", "90",
        "-p", f"127.0.0.1:{args.port}:{WEBAPP_PORT}", args.image,
    ], timeout=180)
    if launched.returncode != 0:
        print(f"smoke: podman run failed: {launched.stderr.strip()[-1000:]}", file=sys.stderr)
        return 1
    checks: list[Check] = []
    boot_seconds = 0.0
    try:
        boot_seconds = wait_port("127.0.0.1", args.port, timeout=args.boot_timeout)
        print(f"{PREFIX} container port mapped after {boot_seconds:.0f}s wall", flush=True)
        inside = run(
            ["podman", "exec", name, "/usr/local/lib/odile-bench/smoke.sh", "local", "--guest",
             "--cron-wait", str(args.cron_wait), "--port", str(WEBAPP_PORT)],
            timeout=args.guest_timeout,
        )
        sys.stdout.write(inside.stdout)
        if inside.stderr.strip():
            sys.stdout.write(inside.stderr)
        sys.stdout.flush()
        guest_checks, guest_final = parse_guest(inside.stdout + inside.stderr)
        try:
            wait_gate("127.0.0.1", args.port, timeout=180)
            host_gate = check_gate("127.0.0.1", args.port, TENANT_USER, args.password)
        except Exception as exc:
            host_gate = [Check("gate-anonymous", False, f"host check failed: {type(exc).__name__}: {exc}"),
                         Check("gate-session", False, f"host check failed: {type(exc).__name__}: {exc}")]
        checks = combined_checks(host_gate, guest_checks, guest_final,
                                 "the container smoke did not report this check")
    finally:
        run(["podman", "rm", "-f", name], timeout=180)

    for check in checks:
        emit(check)
    elapsed = time.monotonic() - started
    print(f"{PREFIX} boot={boot_seconds:.0f}s total={elapsed:.0f}s", flush=True)
    return verdict(checks)


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smoke.sh", description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    # `parents=[common]` gives every subparser the same action objects, and
    # argparse's set_defaults() rewrites the default on that shared action:
    # measured 2026-11-08, `local` in the QEMU guest printed port=19131 and
    # waited 15 minutes on a port nothing listened on. So --port is *not*
    # shared: each mode declares its own with its own default, and local stays
    # the Webapp's 9130.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--password", default=os.environ.get("ODILE_BENCH_PASSWORD", PASSWORD_DEFAULT))
    common.add_argument("--cron-wait", type=float, default=420)
    common.add_argument("--boot-timeout", type=float, default=900)

    local_parser = sub.add_parser("local", parents=[common], help="run the five checks in this root")
    local_parser.add_argument("--guest", action="store_true", help="emit machine-readable lines for the host")
    local_parser.add_argument("--port", type=int, default=WEBAPP_PORT)

    image_parser = sub.add_parser("image", parents=[common], help="boot a disk under QEMU TCG and check it")
    image_parser.add_argument("--disk", required=True)
    image_parser.add_argument("--out")
    image_parser.add_argument("--guest-timeout", type=float, default=2700)
    image_parser.add_argument("--memory", type=int, default=1536)
    image_parser.add_argument("--cpus", type=int, default=2)
    image_parser.add_argument("--port", type=int, default=19130)

    container_parser = sub.add_parser("container", parents=[common], help="run the stack under podman and check it")
    container_parser.add_argument("--image", required=True)
    container_parser.add_argument("--guest-timeout", type=float, default=1200)
    container_parser.add_argument("--port", type=int, default=19131)

    sealed_parser = sub.add_parser(
        "sealed", parents=[common],
        help="boot a sealed disk from its UKI under OVMF and check the unlock",
    )
    sealed_parser.add_argument("--disk", required=True)
    sealed_parser.add_argument("--uki", required=True)
    sealed_parser.add_argument("--ovmf", default="/usr/share/ovmf/OVMF.fd")
    sealed_parser.add_argument("--out")
    sealed_parser.add_argument("--expect", choices=["opened", "prompt"], default="opened")
    sealed_parser.add_argument("--unlock-timeout", type=float, default=900)
    sealed_parser.add_argument("--guest-timeout", type=float, default=2700)
    sealed_parser.add_argument("--memory", type=int, default=2048)
    sealed_parser.add_argument("--cpus", type=int, default=2)
    sealed_parser.add_argument("--port", type=int, default=19132)
    # ODL-456: the first-boot evidence. --first-visit-port is the host port
    # forwarded to the guest's loopback first-visit service (8091); the token
    # is the one in the measured line; --expect-firstboot asserts the unit's
    # one console line.
    sealed_parser.add_argument("--first-visit-port", type=int, default=0,
                               help="host port forwarded to the guest's first-visit page")
    sealed_parser.add_argument("--first-visit-token", default="",
                               help="the one-time token in the measured command line")
    sealed_parser.add_argument("--expect-firstboot", choices=FIRSTBOOT_EXPECT, default=None,
                               help="assert the odile-firstboot console line")
    sealed_parser.add_argument("--firstboot-timeout", type=float, default=900)
    # ODL-472: /manifest through the forwarded guest port; --manifest-file is
    # the release file the served bytes must equal.
    sealed_parser.add_argument("--manifest-port", type=int, default=0,
                               help="host port forwarded to the guest's manifest listener")
    sealed_parser.add_argument("--manifest-file", default="",
                               help="the signed manifest the guest must serve, byte for byte")
    sealed_parser.add_argument("--manifest-out", default="",
                               help="keep the /manifest body the guest sent (evidence)")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not shutil.which("runuser") and args.mode not in ("image", "sealed"):
        print("smoke: runuser is missing (util-linux)", file=sys.stderr)
        return 2
    if args.mode == "local":
        return local(args)
    if args.mode == "image":
        return image(args)
    if args.mode == "sealed":
        return sealed(args)
    return container(args)


if __name__ == "__main__":
    sys.exit(main())
