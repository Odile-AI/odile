#!/usr/bin/python3
"""Headless acceptance check for the Odile local demo.

Starts the real demo server, speaks HTTP to it, restarts it, and exits 0 only
when the card's surface works: 401 without the password, 200 with it, one
canned chat answer that names the model route, one shell command, a file that
survives a restart, and a heartbeat line inside the 60-second job interval.

    /usr/bin/python3 demo/local/demo_check.py
    make demo-check

No framework, no network, no browser.
"""

from __future__ import annotations

import base64
import html
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"
PASSWORD_ENV = "ODILE_DEMO_PASSWORD"
STATE_ENV = "ODILE_DEMO_STATE"

SENTENCE = (
    "local demo of the Odile workspace; the hosted demo runs the same surface "
    "on Odile's measured machine"
)
MODEL_ID = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
URL_RE = re.compile(r"url (http://127\.0\.0\.1:\d+/)")
PASSWORD_RE = re.compile(r"password (\S+)")
HEARTBEAT_BLOCK_RE = re.compile(r'<pre class="hb" id="heartbeat">(.*?)</pre>', re.S)

STARTUP_TIMEOUT = 15
READY_TIMEOUT = 10
HEARTBEAT_DEADLINE = 65.0
# How long `stop()` lets the child act on SIGINT. The child's own teardown is
# bounded: it polls serve_forever every 0.2 s and joins the heartbeat thread
# with a 2 s timeout. A stop that runs longer means the child never acted on the
# signal, not that the host was busy, so the window is kept short: the module
# starts and stops a server per test, and a window that silently expired cost
# the suite that window once per test (10 s x 19 = +190 s, measured 2026-11-20).
STOP_TIMEOUT = 5.0
KILL_TIMEOUT = 5.0
CHECKS = []


class CheckFailed(RuntimeError):
    pass


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise CheckFailed(f"{name}: {detail or 'assertion failed'}")
    CHECKS.append(name)
    print(f"demo-check: ok {name}")


class DemoProcess:
    """The real server.py, its stdout contract, and a clean SIGINT stop."""

    def __init__(self, state_dir: Path, heartbeat_seconds: float | None = None) -> None:
        env = dict(os.environ)
        env.pop(PASSWORD_ENV, None)
        env[STATE_ENV] = str(state_dir)
        self.lines: list[str] = []
        self.stops: list[float] = []
        self._done = threading.Event()
        command = [
            sys.executable,
            str(SERVER),
            "--port",
            "0",
            "--state",
            str(state_dir),
        ]
        if heartbeat_seconds is not None:
            command += ["--heartbeat-seconds", str(heartbeat_seconds)]
        self.proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        self._pump = threading.Thread(target=self._read, daemon=True)
        self._pump.start()
        self.url = ""
        self.password = ""
        try:
            self._await_startup()
        except Exception:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(timeout=5)
            if self.proc.stdout is not None:
                self.proc.stdout.close()
            raise

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))
        self._done.set()

    def _await_startup(self) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            for line in list(self.lines):
                match = URL_RE.search(line)
                if match:
                    self.url = match.group(1)
                match = PASSWORD_RE.search(line)
                if match:
                    self.password = match.group(1)
                if "serving;" in line:
                    break
            if self.url and self.password:
                # The serving line follows the printed password immediately.
                if any("serving;" in line for line in self.lines):
                    self._wait_ready()
                    return
            if self.proc.poll() is not None and self._done.is_set():
                raise CheckFailed("the server exited during startup:\n" + self.output())
            time.sleep(0.02)
        raise CheckFailed("the server did not print a URL and password in time")

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            try:
                status, _, _ = request(self.url + "health", timeout=1)
            except OSError:
                time.sleep(0.05)
                continue
            if status == 200:
                return
        raise CheckFailed("the server never answered /health")

    def output(self) -> str:
        return "\n".join(self.lines)

    def stop(self) -> tuple[int, str]:
        """Send SIGINT and return (exit code, everything the child printed).

        How long each call waited for the child to exit is kept in
        `self.stops`, so a caller can tell a clean stop (measured under 0.4 s)
        from a child that ignored the signal until the window closed. A kill is
        announced, never silent: the -9 this window used to hide is what the
        full-suite gate failed on.
        """
        started = time.monotonic()
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
        try:
            code = self.proc.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            print(
                f"demo-check: the server did not exit on SIGINT within "
                f"{STOP_TIMEOUT:g}s; killing it",
                file=sys.stderr,
            )
            self.proc.kill()
            code = self.proc.wait(timeout=KILL_TIMEOUT)
        self.stops.append(time.monotonic() - started)
        self._done.wait(timeout=5)
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        return code, self.output()


def auth_header(password: str) -> str:
    raw = f"odile:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def request(
    url: str,
    method: str = "GET",
    fields: dict[str, str] | None = None,
    password: str | None = None,
    timeout: float = 10,
):
    data = urllib.parse.urlencode(fields).encode("utf-8") if fields is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if password is not None:
        req.add_header("Authorization", auth_header(password))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.headers, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            body = error.read().decode("utf-8", "replace")
        finally:
            error.close()
        return error.code, error.headers, body


def heartbeat_lines(page: str) -> list[str]:
    match = HEARTBEAT_BLOCK_RE.search(page)
    if not match:
        return []
    text = html.unescape(match.group(1)).strip()
    return [line for line in text.splitlines() if line.strip()]


def run() -> int:
    started = time.monotonic()
    durations: list[float] = []
    print("demo-check: local demo, headless")
    with tempfile.TemporaryDirectory(prefix="odile-demo-check-") as tmp:
        state = Path(tmp) / "state"
        server = DemoProcess(state)
        restarted = None
        try:
            print(f"demo-check: server at {server.url}")

            status, headers, _ = request(server.url)
            check(
                "401 without the password",
                status == 401 and headers.get("WWW-Authenticate", "").startswith("Basic "),
                f"GET / returned {status}",
            )

            status, _, _ = request(server.url, password="not-the-password")
            check("401 with a wrong password", status == 401, f"GET / returned {status}")

            status, headers, page = request(server.url, password=server.password)
            check("200 with the printed password", status == 200, f"GET / returned {status}")
            check("the page states what this is", SENTENCE in page, "exact sentence missing")
            check(
                "the page sets no state header",
                not any(name.lower().startswith("set-") for name in headers),
                "a state-setting header was present",
            )

            status, _, page = request(
                server.url + "chat",
                method="POST",
                fields={"message": "What runs this machine?"},
                password=server.password,
            )
            check("chat returns 200", status == 200, f"POST /chat returned {status}")
            check(
                "one canned chat answer names the model route",
                page.count('class="role">Odile') == 1
                and MODEL_ID in page
                and "canned" in page.lower(),
                "the answer did not name the route or was not exactly one turn",
            )

            status, _, page = request(
                server.url + "shell",
                method="POST",
                fields={"command": "printf 'shell-ok-%s\\n' \"$((6*7))\""},
                password=server.password,
            )
            check("shell returns 200", status == 200, f"POST /shell returned {status}")
            check(
                "one shell command runs in the workspace",
                "shell-ok-42" in page and "[exit 0]" in page,
                "the command output or exit code is missing",
            )

            marker = "survives-restart-" + str(int(time.time()))
            status, _, page = request(
                server.url + "file",
                method="POST",
                fields={"name": "proof.txt", "content": marker + "\n"},
                password=server.password,
            )
            check("file write returns 200", status == 200, f"POST /file returned {status}")
            check(
                "the file appears in the workspace",
                "proof.txt" in page and marker in page,
                "the written file is not listed",
            )

            code, output = server.stop()
            check(
                "Ctrl-C stops the server cleanly",
                code == 0 and "stopped" in output and "Traceback" not in output,
                f"exit code {code}\n{output}",
            )

            restarted = DemoProcess(state)
            status, _, page = request(restarted.url, password=restarted.password)
            check(
                "the same file survives a server restart",
                status == 200 and "proof.txt" in page and marker in page,
                "proof.txt or its contents are missing after restart",
            )

            lines = heartbeat_lines(page)
            check("the job log is visible", bool(lines), "no heartbeat line on the page")
            before = lines[-1]
            t0 = time.monotonic()
            after = before
            while time.monotonic() - t0 < HEARTBEAT_DEADLINE:
                time.sleep(0.25)
                _, _, page = request(restarted.url, password=restarted.password)
                lines = heartbeat_lines(page)
                if lines and lines[-1] != before:
                    after = lines[-1]
                    break
            elapsed = time.monotonic() - t0
            check(
                "the job fires within 60 seconds",
                after != before and elapsed <= 61.0,
                f"no new heartbeat line within the interval (waited {elapsed:.1f}s)",
            )
            print(f"demo-check: heartbeat at {elapsed:.1f}s after the restart ({after})")

            check(
                "the page still states what this is after restart",
                SENTENCE in page,
                "the exact sentence is missing after restart",
            )
        finally:
            if restarted is not None and restarted.proc.poll() is None:
                restarted.stop()
            elif restarted is None and server.proc.poll() is None:
                server.stop()
            durations = list(server.stops)
            if restarted is not None:
                durations += restarted.stops
    elapsed = time.monotonic() - started
    # One line a stranger can compare against a green run: a clean stop is a
    # fraction of a second, a window that expired is the whole STOP_TIMEOUT.
    print(
        "demo-check: stop() took "
        + (", ".join(f"{value:.2f}s" for value in durations) or "no stop")
    )
    print(f"demo-check: PASS {len(CHECKS)} checks in {elapsed:.1f}s")
    return 0


def main() -> int:
    try:
        return run()
    except CheckFailed as error:
        print(f"demo-check: FAIL {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
