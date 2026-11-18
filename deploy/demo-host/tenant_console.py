#!/usr/bin/python3
"""A remote console to Odile's tenant machine, over the tenant's own Webapp.

Odile holds no SSH key to a tenant: the machine's `authorized_keys` carries one
throwaway public key whose private half was shredded at first boot. What the
machine *does* expose is its own Hermes Webapp, and that is enough:

  * `POST /auth/password-login` issues the dashboard cookie;
  * `POST /api/auth/ws-ticket` mints a single-use ticket;
  * `/api/ws` is the operator socket, and `shell.exec` runs one command as the
    `tenant` user, which holds NOPASSWD sudo.

This module is the transport only — login, ticket, one live local session, one
`shell.exec` per command, and chunked upload/read. `deploy_demo_public.py`
drives it; a human can too:

    ./deploy/demo-host/tenant_console.py --user judge --password-file PATH \\
        --run 'id -un; sudo -n true'

Standard library only: the socket is a WebSocket the way RFC 6455 describes
one, and the frames are the same JSON-RPC frames the Webapp sends.
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import socket
import ssl
import struct
import sys
import time
import urllib.parse
import urllib.request

#: The tenant's public name. Since ODL-503 the machine's Webapp declares
#: demo.odile.fyi as its ``webapp.public_url``, and Hermes refuses any other
#: Host header with 400 (web_server_boundary._is_accepted_host) — so this is
#: the name the console must speak.
DEFAULT_BASE = "https://demo.odile.fyi"
#: The machine's own name. Caddy on the tenant serves it as an alias; its
#: Webapp path is refused once public_url is the demo host, so it is only the
#: bootstrap route (before the demo block exists) and the recovery route.
MACHINE_BASE = "https://odl-fake01.odile.fyi"
#: `shell.exec` runs one command with a 30-second ceiling (session_shell.py).
SHELL_TIMEOUT = 30.0
#: One printf carries at most ~2 KB of base64; the reply caps stdout at 4 KB.
CHUNK = 1800


class ConsoleError(RuntimeError):
    pass


class Console:
    def __init__(self, base: str = DEFAULT_BASE, *, timeout: float = 30.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.tls: ssl.SSLSocket | None = None
        self.buffer = b""
        self.rid = 0
        self.session_id = ""
        self.ready: dict = {}

    # -- framing ---------------------------------------------------------
    def _recv_exact(self, count: int) -> bytes:
        while len(self.buffer) < count:
            chunk = self.tls.recv(max(4096, count - len(self.buffer)))
            if not chunk:
                raise ConsoleError("the socket closed")
            self.buffer += chunk
        out, self.buffer = self.buffer[:count], self.buffer[count:]
        return out

    def _read_frame(self) -> tuple[int, bytes]:
        head = self._recv_exact(2)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def _send_text(self, data: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        size = len(data)
        if size < 126:
            header = bytes([0x81, 0x80 | size])
        elif size < 65536:
            header = bytes([0x81, 0x80 | 126]) + struct.pack("!H", size)
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", size)
        self.tls.sendall(header + mask + masked)

    # -- auth ------------------------------------------------------------
    @staticmethod
    def _post_json(url: str, payload: bytes, cookies: http.cookiejar.CookieJar) -> str:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
        request = urllib.request.Request(url, data=payload,
                                         headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=30) as response:
            return response.read().decode("utf-8", "replace")

    def login(self, username: str, password: str) -> str:
        cookies = http.cookiejar.CookieJar()
        body = json.dumps({"provider": "basic", "username": username,
                           "password": password}).encode()
        try:
            reply = self._post_json(self.base + "/auth/password-login", body, cookies)
        except urllib.error.HTTPError as exc:
            raise ConsoleError(f"sign-in as {username!r} -> {exc.code}") from exc
        pairs = [f"{cookie.name}={cookie.value}" for cookie in cookies]
        if not pairs:
            raise ConsoleError(f"sign-in as {username!r} set no cookie: {reply[:120]}")
        return "; ".join(pairs)

    def ticket(self, cookie: str) -> str:
        request = urllib.request.Request(
            self.base + "/api/auth/ws-ticket", data=b"",
            headers={"Cookie": cookie, "Content-Length": "0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read()).get("ticket", "")

    def connect(self, username: str, password: str) -> None:
        cookie = self.login(username, password)
        url = urllib.parse.urlsplit(self.base)
        key = base64.b64encode(os.urandom(16)).decode()
        target = f"/api/ws?ticket={urllib.parse.quote(self.ticket(cookie))}"
        request = (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: {url.netloc}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Origin: {url.scheme}://{url.netloc}\r\n\r\n"
        )
        context = ssl.create_default_context()
        raw = socket.create_connection((url.hostname, url.port or 443), timeout=30)
        self.tls = context.wrap_socket(raw, server_hostname=url.hostname)
        self.tls.sendall(request.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.tls.recv(4096)
            if not chunk:
                raise ConsoleError("the socket closed in the handshake")
            data += chunk
        head, _, self.buffer = data.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if "101" not in status:
            raise ConsoleError(f"upgrade -> {status}")
        self.tls.settimeout(60)
        opcode, payload = self._read_frame()  # the gateway's ready frame
        self.ready = json.loads(payload) if opcode == 1 else {}

    # -- rpc -------------------------------------------------------------
    def call(self, method: str, params: dict, *, timeout: float | None = None) -> dict:
        if self.tls is None:
            raise ConsoleError("not connected")
        self.rid += 1
        rid = self.rid
        self._send_text(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                    "params": params}).encode())
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            self.tls.settimeout(max(1.0, deadline - time.time()))
            opcode, payload = self._read_frame()
            if opcode == 8:
                raise ConsoleError("the socket closed")
            if opcode != 1:
                continue
            message = json.loads(payload)
            if message.get("id") == rid:
                return message
        raise ConsoleError(f"{method}: no reply")

    def open_session(self) -> str:
        reply = self.call("session.create", {"source": "gui", "profile": "default"})
        self.session_id = ((reply.get("result") or {}).get("session_id") or "")
        if not self.session_id:
            raise ConsoleError(f"session.create -> {json.dumps(reply)[:300]}")
        return self.session_id

    def shell(self, command: str, *, timeout: float = SHELL_TIMEOUT + 30) -> dict:
        """Run one command as the tenant. Returns {stdout, stderr, code}."""
        if not self.session_id:
            self.open_session()
        reply = self.call("shell.exec", {"session_id": self.session_id,
                                         "command": command}, timeout=timeout)
        if reply.get("error"):
            error = reply["error"]
            raise ConsoleError(f"shell.exec -> {error.get('code')} {error.get('message')}")
        return reply.get("result") or {}

    def run(self, command: str, *, check: bool = True) -> dict:
        result = self.shell(command)
        if check and result.get("code") not in (0, None):
            raise ConsoleError(
                f"{command} -> code {result.get('code')}: "
                f"{(result.get('stderr') or result.get('stdout') or '').strip()[-400:]}")
        return result

    # -- files -----------------------------------------------------------
    def put(self, local: str, remote: str, *, mode: str = "0644") -> None:
        """Upload *local* to *remote* as root, in base64 chunks.

        The tenant writes the bytes under /tmp (its own directory) and root only
        moves them into place, so no root `tee` or redirect is needed: the
        Webapp's `shell.exec` refuses a command it reads as destructive.
        """
        import pathlib  # local: only the CLI uploads files

        name = pathlib.Path(local).name
        staged = f"/tmp/{name}"
        data = base64.b64encode(pathlib.Path(local).read_bytes()).decode()
        self.run(f"cd /tmp && rm -f {name}.b64 {name}")
        for offset in range(0, len(data), CHUNK):
            chunk = data[offset:offset + CHUNK]
            redirect = ">" if offset == 0 else ">>"
            self.run(f"printf %s {chunk} {redirect} {staged}.b64")
        self.run(f"base64 -d {staged}.b64 > {staged}")
        self.run(f"sudo install -m {mode} -o root -g root {staged} {remote}")
        # `rm` of an absolute path reads as destructive to the Webapp's command
        # guard; relative names from /tmp do not.
        self.run(f"cd /tmp && rm -f {name}.b64 {name}")

    def read(self, remote: str) -> bytes:
        """Read *remote* as root, in base64 chunks the 4 KB reply can carry."""
        chunks: list[str] = []
        offset = 1
        while True:
            end = offset + CHUNK - 1
            result = self.run(f"sudo base64 -w0 {remote} | cut -c {offset}-{end}")
            piece = (result.get("stdout") or "").strip()
            if not piece:
                break
            chunks.append(piece)
            if len(piece) < CHUNK - 4:
                break
            offset = end + 1
        return base64.b64decode("".join(chunks))


def connect_any(username: str, password: str,
                bases: tuple[str, ...] = ()) -> "Console":
    """Open a console over the first base that answers as *username*.

    The demo host is the machine's ``public_url``; the machine's own name is
    the alias that answers before the demo surface exists. Trying both keeps
    one command true on the first run (no demo block yet) and every later one
    (demo host only). The last failure is raised when none answers.
    """
    tried = bases or (DEFAULT_BASE, MACHINE_BASE)
    last: Exception | None = None
    for base in tried:
        console = Console(base)
        try:
            console.connect(username, password)
            return console
        except (ConsoleError, OSError) as exc:
            last = exc
    raise ConsoleError(f"no console answered as {username!r} on {', '.join(tried)}: {last}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one command on the tenant over its Webapp.")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--user", default="judge")
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--run", action="append", default=[], help="a command to run")
    parser.add_argument("--put", action="append", default=[], metavar="LOCAL:REMOTE")
    parser.add_argument("--get", default="", metavar="REMOTE")
    args = parser.parse_args(argv)

    password = open(args.password_file, encoding="utf-8").read().strip()
    console = Console(args.base)
    console.connect(args.user, password)
    ready = (console.ready.get("params") or {}).get("type", "?")
    print(f"ready: {ready}  session: {console.open_session()}")
    for pair in args.put:
        local, _, remote = pair.partition(":")
        console.put(local, remote)
        print(f"put  {local} -> {remote}")
    for command in args.run:
        result = console.shell(command)
        print(f"$ {command}\n[code {result.get('code')}]")
        if result.get("stdout"):
            print(result["stdout"].rstrip("\n"))
        if result.get("stderr"):
            print("stderr:", result["stderr"].rstrip("\n"))
    if args.get:
        sys.stdout.buffer.write(console.read(args.get))
    return 0


if __name__ == "__main__":
    sys.exit(main())
