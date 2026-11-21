#!/usr/bin/python3
"""Odile local demo — the workspace surface, one standard-library server.

This is a reproduction, not a second product. It needs no Hermes install, no
NEAR key and no network: one process, form posts, no JavaScript, no session
state, no third-party request. It serves on loopback only.

    /usr/bin/python3 demo/local/server.py --port 8000
    make demo

The chat answer is canned and names the model route. The shell runs a command
in a temporary workspace on this computer. A file written here survives a
server restart because the workspace is a directory on disk. A background
thread appends a heartbeat line every 60 seconds; that is the scheduled job.

Everything the demo keeps lives under --state (default: demo/local/state).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import html
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
TEMPLATE_PATH = HERE / "template.html"

DEFAULT_PORT = 8000
DEFAULT_HEARTBEAT_SECONDS = 60.0
DEMO_USERNAME = "odile"
PASSWORD_ENV = "ODILE_DEMO_PASSWORD"
STATE_ENV = "ODILE_DEMO_STATE"

# The sentence the card fixes; the page says this and nothing stronger.
SENTENCE = (
    "local demo of the Odile workspace; the hosted demo runs the same surface "
    "on Odile's measured machine"
)
MODEL_ID = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
MODEL_HOST = "cloud-api.near.ai"
CHAT_ANSWER = (
    "The local answer is canned. On the hosted demo, inference runs on NEAR "
    "AI's confidential cloud, inside a hardware enclave: the model is "
    f"{MODEL_ID}, reached at {MODEL_HOST}. Request path: "
    "POST /v1/chat/completions."
)

MAX_BODY = 64 * 1024
MAX_OUTPUT = 8000
MAX_PREVIEW = 80
SHELL_TIMEOUT = 5
HEARTBEAT_SHOWN = 6
CHAT_SHOWN = 4
SHELL_SHOWN = 3
FILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "font-src 'self'; form-action 'self'; base-uri 'none'; "
    "frame-ancestors 'none'"
)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_seconds(value: float) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else format(number, "g")


def byte_label(size: int) -> str:
    return "1 byte" if size == 1 else f"{size} bytes"


@dataclass(frozen=True)
class FileRow:
    name: str
    size: int
    mtime: str
    preview: str


@dataclass(frozen=True)
class ShellRun:
    command: str
    code: int
    output: str


@dataclass(frozen=True)
class Snapshot:
    chat: tuple[tuple[str, str], ...]
    shell: tuple[ShellRun, ...]
    files: tuple[FileRow, ...]
    heartbeat: tuple[str, ...]
    flash: tuple[tuple[str, str, str], ...]


class DemoSession:
    """One workspace: in-memory transcript plus the on-disk state directory."""

    def __init__(self, state_dir: Path, heartbeat_seconds: float) -> None:
        self.state_dir = state_dir
        self.workspace = state_dir / "workspace"
        self.heartbeat_path = state_dir / "heartbeat.log"
        self.heartbeat_seconds = heartbeat_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._chat: list[tuple[str, str]] = []
        self._shell: list[ShellRun] = []
        self._flash: dict[str, tuple[str, str]] = {}

        self.workspace.mkdir(parents=True, exist_ok=True)
        self.heartbeat_path.touch(exist_ok=True)

    # -- the scheduled job -------------------------------------------------
    def start_heartbeat(self) -> None:
        self._append_heartbeat()
        self._thread = threading.Thread(
            target=self._heartbeat_loop, name="odile-demo-heartbeat", daemon=True
        )
        self._thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            self._append_heartbeat()

    def _append_heartbeat(self) -> None:
        line = f"{utc_stamp()} heartbeat — local demo job fired"
        with self._lock:
            with self.heartbeat_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def stop_heartbeat(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    # -- workspace actions -------------------------------------------------
    def add_chat(self, message: str) -> None:
        with self._lock:
            self._chat.append((message, CHAT_ANSWER))
            del self._chat[:-CHAT_SHOWN]

    def add_shell(self, command: str) -> tuple[int, str]:
        code, output = run_shell(command, self.workspace)
        with self._lock:
            self._shell.append(ShellRun(command, code, output))
            del self._shell[:-SHELL_SHOWN]
        return code, output

    def write_file(self, name: str, content: str) -> int:
        if not FILE_NAME_RE.match(name) or name in {".", ".."}:
            raise ValueError(
                "the file name is letters, digits, dot, dash or underscore"
            )
        data = content.encode("utf-8")
        with self._lock:
            (self.workspace / name).write_bytes(data)
        return len(data)

    def set_flash(self, where: str, text: str, kind: str = "info") -> None:
        with self._lock:
            self._flash[where] = (kind, text)

    def snapshot(self) -> Snapshot:
        with self._lock:
            chat = tuple(self._chat)
            shell = tuple(self._shell)
            flash = tuple(
                (where, kind, text) for where, (kind, text) in self._flash.items()
            )
            files = []
            for path in sorted(self.workspace.iterdir(), key=lambda p: p.name.lower()):
                if not path.is_file():
                    continue
                stat = path.stat()
                files.append(
                    FileRow(
                        name=path.name,
                        size=stat.st_size,
                        mtime=datetime.fromtimestamp(
                            stat.st_mtime, timezone.utc
                        ).strftime("%H:%M:%SZ"),
                        preview=file_preview(path),
                    )
                )
            try:
                lines = self.heartbeat_path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []
            heartbeat = tuple(line for line in lines if line.strip())[-HEARTBEAT_SHOWN:]
        return Snapshot(chat=chat, shell=shell, files=tuple(files),
                        heartbeat=heartbeat, flash=flash)


def file_preview(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    first = " ".join(text.split())
    if len(first) > MAX_PREVIEW:
        first = first[: MAX_PREVIEW - 1] + "…"
    return first


def run_shell(command: str, workspace: Path) -> tuple[int, str]:
    """Run one command in the workspace and return (exit code, output)."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(workspace),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=str(workspace),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
        env=env,
    )
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=SHELL_TIMEOUT)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, _ = proc.communicate()
    output = output or ""
    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + "\n[output truncated]"
    if timed_out:
        output += f"\n[timed out after {SHELL_TIMEOUT}s]"
    return proc.returncode, output


# -- rendering -------------------------------------------------------------
PLACEHOLDER_RE = re.compile(r"\{\{([a-z_]+)\}\}")


def render_page(session: DemoSession) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    snap = session.snapshot()
    flash = {where: (kind, text) for where, kind, text in snap.flash}
    values = {
        "sentence": SENTENCE,
        "model_id": MODEL_ID,
        "model_host": MODEL_HOST,
        "heartbeat_seconds": format_seconds(session.heartbeat_seconds),
        "heartbeat": html.escape("\n".join(snap.heartbeat) or "No line yet."),
        "chat": chat_html(snap),
        "shell": shell_html(snap),
        "files": files_html(snap),
        "chat_flash": flash_html(flash.get("chat")),
        "shell_flash": flash_html(flash.get("shell")),
        "file_flash": flash_html(flash.get("file")),
    }
    return PLACEHOLDER_RE.sub(lambda match: values.get(match.group(1), ""), template)


def flash_html(value: tuple[str, str] | None) -> str:
    """One flash paragraph; an error is coloured, a confirmation stays soft."""
    if not value or not value[1]:
        return ""
    kind, text = value
    css = "flash error" if kind == "error" else "flash"
    return f'<p class="{css}">{html.escape(text)}</p>'


def chat_html(snap: Snapshot) -> str:
    if not snap.chat:
        return '<p class="empty">No message yet. Ask something; the answer is canned.</p>'
    blocks = []
    for message, answer in snap.chat:
        blocks.append(
            '<div class="turn user"><p class="role">You</p>'
            f"<p>{html.escape(message)}</p></div>"
            '<div class="turn"><p class="role">Odile</p>'
            f"<p>{html.escape(answer)}</p></div>"
        )
    return "".join(blocks)


def shell_html(snap: Snapshot) -> str:
    if not snap.shell:
        return (
            '<p class="empty">No command yet. It runs here, in the demo workspace.</p>'
        )
    blocks = []
    for run in snap.shell:
        output = run.output
        if output and not output.endswith("\n"):
            output += "\n"
        blocks.append(
            '<pre class="term" tabindex="0">'
            f'<span class="term-cmd">$ {html.escape(run.command)}</span>\n'
            f'{html.escape(output)}'
            f'<span class="term-exit">[exit {run.code}]</span></pre>'
        )
    return "".join(blocks)


def files_html(snap: Snapshot) -> str:
    if not snap.files:
        return '<li class="empty">No files yet. Write one below; it survives a restart.</li>'
    rows = []
    for row in snap.files:
        preview = (
            f'<span class="preview">{html.escape(row.preview)}</span>'
            if row.preview
            else ""
        )
        rows.append(
            f'<li><span class="fname">{html.escape(row.name)}</span>'
            f'<span class="meta">{byte_label(row.size)} · {row.mtime}</span>'
            f"{preview}</li>"
        )
    return "".join(rows)


# -- HTTP ------------------------------------------------------------------
def make_handler(session: DemoSession, password: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "odile-demo/1"

        def version_string(self) -> str:
            return self.server_version

        def log_message(self, format: str, *args) -> None:
            sys.stderr.write(
                "odile-demo: %s %s\n" % (self.address_string(), format % args)
            )

        # -- plumbing --------------------------------------------------
        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()

        def _send_text(self, status: int, text: str) -> None:
            data = text.encode("utf-8")
            self._headers(status, "text/plain; charset=utf-8", len(data))
            self.wfile.write(data)

        def _send_html(self, status: int, text: str) -> None:
            data = text.encode("utf-8")
            self._headers(status, "text/html; charset=utf-8", len(data))
            self.wfile.write(data)

        def _unauthorized(self) -> None:
            body = b"password required\n"
            self.send_response(401)
            self.send_header(
                "WWW-Authenticate", 'Basic realm="Odile local demo", charset="UTF-8"'
            )
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                raw = base64.b64decode(header[6:].strip(), validate=True)
                decoded = raw.decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                return False
            user, separator, supplied = decoded.partition(":")
            if not separator:
                return False
            return hmac.compare_digest(
                user.encode("utf-8"), DEMO_USERNAME.encode("utf-8")
            ) and hmac.compare_digest(
                supplied.encode("utf-8"), password.encode("utf-8")
            )

        def _route(self, body: bytes | None) -> None:
            path = urlparse(self.path).path
            if path == "/health":
                self._send_text(200, "ok\n")
                return
            if not self._authorized():
                self._unauthorized()
                return
            if body is None:
                self._get(path)
            else:
                self._post(path, body)

        def _get(self, path: str) -> None:
            if path == "/":
                self._send_html(200, render_page(session))
            elif path == "/assets/cormorant-garamond.woff2":
                font = (HERE / "assets" / "cormorant-garamond.woff2").read_bytes()
                self._headers(200, "font/woff2", len(font))
                self.wfile.write(font)
            else:
                self._send_text(404, "not found\n")

        def _post(self, path: str, body: bytes) -> None:
            fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)

            def field(name: str) -> str:
                values = fields.get(name) or [""]
                return values[0].strip()

            if path == "/chat":
                message = field("message")
                if message:
                    session.add_chat(message)
                else:
                    session.set_flash("chat", "Type a message first.", "error")
            elif path == "/shell":
                command = field("command")
                if command:
                    session.add_shell(command)
                else:
                    session.set_flash("shell", "Type a command first.", "error")
            elif path == "/file":
                name = field("name")
                content = fields.get("content") or [""]
                try:
                    written = session.write_file(name, content[0])
                except ValueError as exc:
                    session.set_flash("file", str(exc), "error")
                else:
                    session.set_flash("file", f"wrote {name} ({byte_label(written)})")
            else:
                self._send_text(404, "not found\n")
                return
            self._send_html(200, render_page(session))

        def do_GET(self) -> None:
            try:
                self._route(None)
            except BrokenPipeError:
                pass
            except Exception:
                traceback.print_exc()
                self._safe_500()

        def do_POST(self) -> None:
            try:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length < 0:
                    length = 0
                if length > MAX_BODY:
                    self.close_connection = True
                    self._send_text(413, "request body too large\n")
                    return
                body = self.rfile.read(length) if length else b""
                self._route(body)
            except BrokenPipeError:
                pass
            except Exception:
                traceback.print_exc()
                self._safe_500()

        def _safe_500(self) -> None:
            try:
                self._send_text(500, "internal error\n")
            except OSError:
                pass

    return Handler


# -- entry points ----------------------------------------------------------
def reset_state(state_dir: Path, apply: bool) -> int:
    """Remove the demo state directory; a dry run unless --apply is given."""
    if not state_dir.exists():
        print(f"odile-demo: nothing to remove at {state_dir}")
        return 0
    files = [path for path in state_dir.rglob("*") if path.is_file()]
    total = sum(path.stat().st_size for path in files)
    verb = "removing" if apply else "would remove"
    print(f"odile-demo: {verb} {len(files)} files ({total} bytes) under {state_dir}")
    for path in files[:20]:
        print(f"  {path.relative_to(state_dir)}")
    if apply:
        shutil.rmtree(state_dir)
        print("odile-demo: state removed")
    else:
        print("odile-demo: dry run; apply with: make demo-reset APPLY=1")
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Odile local demo server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--state",
        default=os.environ.get(STATE_ENV, str(HERE / "state")),
        help="state directory (default: demo/local/state)",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=DEFAULT_HEARTBEAT_SECONDS,
        help="scheduled-job interval (default: 60)",
    )
    parser.add_argument(
        "--reset", action="store_true", help="remove the state directory and exit"
    )
    parser.add_argument(
        "--apply", action="store_true", help="with --reset: actually remove"
    )
    args = parser.parse_args(argv)
    if not args.reset and args.heartbeat_seconds <= 0:
        parser.error("--heartbeat-seconds must be greater than zero")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state_dir = Path(args.state).expanduser().resolve()
    if args.reset:
        return reset_state(state_dir, apply=args.apply)
    if not TEMPLATE_PATH.is_file():
        print(f"odile-demo: missing {TEMPLATE_PATH}", file=sys.stderr)
        return 2

    password = os.environ.get(PASSWORD_ENV) or secrets.token_urlsafe(9)
    session = DemoSession(state_dir, args.heartbeat_seconds)
    session.start_heartbeat()

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(session, password))
        # A handler thread reading the next request on a keep-alive connection
        # must not hold Ctrl-C hostage: with the default block_on_close,
        # server_close() waits for every request thread, and the stop window
        # (10 s in the demo tests) can expire while an idle HTTP/1.1 handler is
        # still blocked in readline(). That was the -9 in the full-suite gate.
        httpd.daemon_threads = True
        httpd.block_on_close = False
    except OSError as exc:
        session.stop_heartbeat()
        print(
            f"odile-demo: cannot listen on 127.0.0.1:{args.port}: {exc}",
            file=sys.stderr,
        )
        if args.port == DEFAULT_PORT:
            print(
                "odile-demo: another demo may be running; try make demo DEMO_PORT=8001",
                file=sys.stderr,
            )
        return 2

    def _sigterm(_signum, _frame):
        raise KeyboardInterrupt

    # Ctrl-C stops this server even when SIGINT arrives already ignored.
    # CPython installs its default SIGINT handler only when the inherited
    # disposition is not SIG_IGN, and SIG_IGN -- unlike a handler -- survives
    # exec: a job started as a background command by a non-interactive shell
    # (`make check > log 2>&1 &`, job control off) is handed SIGINT=SIG_IGN,
    # every descendant inherits it, and this server then ignored the interrupt.
    # The demo tests killed it when their stop window closed: -9, and the module
    # took 203.8 s instead of 4.3 s (measured 2026-11-20). Installing the
    # handler here, rather than inheriting one, is what makes the printed
    # promise -- "Ctrl-C stops it" -- true however the parent was launched.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, _sigterm)
    port = httpd.server_address[1]
    print(f"odile-demo: url http://127.0.0.1:{port}/")
    print(f"odile-demo: user {DEMO_USERNAME}")
    print(f"odile-demo: password {password}")
    print(f"odile-demo: state {state_dir}")
    print(f"odile-demo: heartbeat {format_seconds(args.heartbeat_seconds)}s")
    print("odile-demo: serving; Ctrl-C stops it")
    sys.stdout.flush()

    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        session.stop_heartbeat()
        httpd.server_close()
        print("odile-demo: stopped")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
