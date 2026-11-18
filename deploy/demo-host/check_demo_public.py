#!/usr/bin/python3
"""Check the demo's definition of done — ODL-503.

Talks to the live hosts and, with `--password-file`, to the tenant's own
Webapp socket (the judge account). With a password it changes the machine only
where a judge would: it opens sessions and deletes them again, appends one turn
to the seeded session, writes one file in the workspace for `--persistence`,
and runs the demo's own apply once (with `--no-restart`) to prove a deploy
changes no session.

Hermes keeps a row for a console's own `shell.exec` session too, so every
session this check opens — the console's included — is recorded and deleted at
the end; existence is the machine's own `--report --session-id` (the
`/api/sessions` endpoint is a 20-row window), and the tenant's session count
must be where the check found it. The seed's turn is a judge's own turn.

    ./deploy/demo-host/check_demo_public.py --password-file /path/to/demo-password
    ./deploy/demo-host/check_demo_public.py            # the public half only
    ./deploy/demo-host/check_demo_public.py --target "$(dig +short demo.odile.fyi)"
                                                       # the public half on the live zone
    ./deploy/demo-host/check_demo_public.py --password-file … --persistence
    ./deploy/demo-host/check_demo_public.py --password-file … --sessions
                                                       # + a fourth concurrent session runs

The two DNS rows take the expected A record from ``--target``. The default is
the tree's redacted address (``create_dns.TENANT_IP``), so a live zone needs
the flag; both public names must answer the one expected address.

Exit codes: 0 every check passed, 1 a failure named, 2 a missing prerequisite.
"""

from __future__ import annotations

import argparse
from http.cookiejar import CookieJar
import hashlib
import html
import io
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import create_dns  # noqa: E402
import tenant_console  # noqa: E402

DEMO_NAME = "demo.odile.fyi"
MACHINE_NAME = "odl-fake01.odile.fyi"
DEMO = "https://" + DEMO_NAME
MACHINE = "https://" + MACHINE_NAME
#: The address the two public names must answer. The tree carries the tenant's
#: redacted address (docs/ops/PUBLIC-TREE.md), so the default is the same value
#: `create_dns.py` and the deploy's `--target` name; a live run passes
#: ``--target "$(dig +short demo.odile.fyi)"``.
DEFAULT_DNS = create_dns.TENANT_IP
TENANT_BASE = tenant_console.DEFAULT_BASE
DEMO_UNIT_GLOB = "odile-demo-*"
APPLY = "/usr/local/lib/odile-demo-public/apply_demo_public.py"
PASSWORD_REMOTE = "/run/odile-demo-secret"
NEAR_BASE_URL = "https://cloud-api.near.ai/v1"
NEAR_KEY_ENV = "HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY"
COMMITTED_BLOCK = HERE.parent / "caddy" / "demo-tenant-site.caddy"
#: The demo machine's /verify lede (ODL-503 finding 1), one committed file.
VERIFY_LEDE = HERE / "verify-lede.html"
CUSTOMER_SENTENCE = "No one at Odile can log in to a tenant machine."
MEASUREMENT = re.compile(r"\b([0-9a-f]{96})\b")
#: The sign-in page's three tab files (t_6b25f6e5), as committed: the same
#: bytes the site's pages link. A served file must hash to these, carry this
#: type, and answer with the one-year immutable cache the block promises.
COMMITTED_ICONS = (
    ("favicon.ico",
     "8f5f997684f2d8eb2336eef46ef8e100776ecaa35c0375faadd4bb7e1ab17fba", "image/x-icon"),
    ("icon-32.png",
     "28bedf61e9f5bad25d9a7b874bab788634299eb1e08a9bd99f614a09f9f904bc", "image/png"),
    ("apple-touch-icon.png",
     "dd5386a9c772a5d67702f35fd22eed649783a8575d54f4cbd641111434123773", "image/png"),
)
#: BRAND.md §Name and mark: the credit sits in the HTML comment beside the
#: link on every page. The sign-in page is a page.
CREDIT_COMMENT = ("<!-- Icon: Lucas Cranach the Elder, Saints Christina and Ottilia "
                  "(Katharinenaltar, Dresden). Public domain. Source: Wikimedia Commons. -->")

#: The verifier a judge downloads: the check runs the page's own line out of
#: these bytes, not out of this checkout, so the guard covers the published
#: program too.
VERIFIER_BUNDLE = "https://odile.fyi/odile-verify.tar.gz"
PASS_LINE = ("PASS — the CPU signed this report and the launch measurement "
             "matches the pin.")
#: The demo's claims row says the report refreshes every five minutes
#: (docs/CLAIMS.md §1 *The demo machine*). The gate measures the age of the
#: document the page actually serves: ten minutes is two timer periods, so the
#: timer's 15 s accuracy cannot fail the check, while a stopped timer fails on
#: its second interval (t_145ca49c).
REPORT_MAX_AGE_SECONDS = 600
#: The seeded session's identity is frozen: the id is a hash over the profile
#: and principal the row was minted with on the retired HQ demo, and app.py's
#: /judge deep link carries the same id. The retired account name is spelled in
#: two pieces so it does not reappear in the tree; the string must never change.
SEED_PROFILE_ID = "/home/odile" "demo/.hermes"
SEED_PRINCIPAL_ID = "uid:1001"
SESSION_ID = "local-" + hashlib.sha256(
    json.dumps([SEED_PROFILE_ID, SEED_PRINCIPAL_ID,
                str(uuid.uuid5(uuid.NAMESPACE_URL, "odile-demo:start-here:v1"))],
               separators=(",", ":")).encode()
).hexdigest()

PASSED = 0
FAILED = 0
#: Every session this check opens, in order. A console's own `shell.exec`
#: session is a row as surely as a judge's is, so the console sessions are in
#: here too; every id is deleted at the end.
OPENED: list[str] = []


def check(ok: bool, text: str) -> bool:
    global PASSED, FAILED
    PASSED += 1 if ok else 0
    FAILED += 0 if ok else 1
    print(("ok   " if ok else "FAIL ") + text)
    return ok


def report_age_seconds(fetched_at: str, now: float | None = None) -> float | None:
    """The report's age in seconds, or None when fetched_at is not `…Z` ISO-8601."""
    try:
        stamp = datetime.strptime(str(fetched_at), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return (now if now is not None else time.time()) - stamp.replace(tzinfo=timezone.utc).timestamp()


def freshness_problem(document: dict, now: float | None = None,
                      max_age: int = REPORT_MAX_AGE_SECONDS) -> str:
    """Why the served report is not fresh; '' when it is (t_145ca49c).

    The approved row is "The report refreshes every five minutes"
    (docs/CLAIMS.md §1 *The demo machine*). The gate measures the document the
    page actually serves, not the timer's intent: a one-shot refresh that
    stopped would otherwise keep a green page for three days, which is the
    regression this check exists to catch. The bound is two timer periods, so
    the timer's own accuracy window is not a failure.
    """
    if not isinstance(document, dict):
        return "the response is not a JSON object"
    fetched_at = document.get("fetched_at")
    if not isinstance(fetched_at, str) or not fetched_at:
        return "the report has no fetched_at"
    age = report_age_seconds(fetched_at, now)
    if age is None:
        return f"fetched_at {fetched_at!r} is not ISO-8601 UTC"
    if age < -60:
        return f"fetched_at {fetched_at} is {-age:.0f}s in the future"
    if age > max_age:
        return f"fetched_at {fetched_at} is {age / 60:.1f} minutes old (max {max_age // 60})"
    return ""


def icon_problem(name: str, status: int, headers: dict, body: bytes) -> str:
    """Why one served tab icon is not the committed file; '' when it is."""
    pin = next((pin for candidate, pin, _type in COMMITTED_ICONS if candidate == name), "")
    content_type = next((kind for candidate, _pin, kind in COMMITTED_ICONS
                         if candidate == name), "")
    if not pin:
        return f"no committed pin for {name}"
    if status != 200:
        return f"status {status}"
    lowered = {key.lower(): str(value) for key, value in headers.items()}
    served_type = lowered.get("content-type", "").split(";")[0].strip()
    if served_type != content_type:
        return f"type {served_type!r}"
    cache = lowered.get("cache-control", "")
    if not ("public" in cache and "max-age=31536000" in cache and "immutable" in cache):
        return f"cache {cache!r}"
    digest = hashlib.sha256(body).hexdigest()
    if digest != pin:
        return f"body {digest[:16]}… is not {pin[:16]}…"
    return ""


def lede_problem(body: str, lede: str = "") -> str:
    """Why the served /verify does not open with the demo lede; '' when it does."""
    expected = (lede or VERIFY_LEDE.read_text(encoding="utf-8")).strip()
    match = re.search(r'<p class="lede">.*?</p>', body, re.S)
    if match is None:
        return "the page has no lede paragraph"
    if match.group(0).strip() != expected:
        return "the first lede is %r" % match.group(0)[:160]
    if CUSTOMER_SENTENCE in body:
        return "the page still carries the customer-tenant row"
    return ""


def page_command(page: str) -> str:
    """The one verifier line the page prints, prompts stripped; '' if not one.

    A judge selects the line in the terminal block and pastes it. The line is
    the whole shell line (``cd odile-verify && ./odile-verify …``) because
    that is what the browser copies; a page with two such lines is refused
    rather than guessing which one the judge took.
    """
    found: list[str] = []
    for block in re.finditer(r"<pre[^>]*>\s*<code[^>]*>(.*?)</code>\s*</pre>", page, re.S):
        text = html.unescape(re.sub(r"<[^>]+>", "", block.group(1)))
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("$ "):
                line = line[2:].strip()
            if "./odile-verify" in line:
                found.append(line)
    return found[0] if len(found) == 1 else ""


def page_command_problem(page: str, measurement: str) -> str:
    """Why the page does not print the judge's exact --expect line; '' when it does."""
    command = page_command(page)
    if not command:
        return "the page prints no single ./odile-verify command"
    expected = ("cd odile-verify && ./odile-verify https://demo.odile.fyi"
                " --expect %s" % measurement)
    if command != expected:
        return "the page's command is %r, expected %r" % (command, expected)
    return ""


def command_script(command: str) -> str:
    """Refuse anything that is not the page's exact line before running it."""
    pattern = (r"^cd odile-verify && \./odile-verify https://demo\.odile\.fyi"
               r" --expect [0-9a-f]{96}$")
    if not re.match(pattern, command):
        raise ValueError("not the page's ./odile-verify command: %r" % command)
    return command


def run_page_command(command: str, *, timeout: int = 240) -> tuple[int, str]:
    """Download the published verifier bundle and run the page's own line.

    The bundle is the judge's copy, not this checkout's: a page that names a
    flag the published verifier does not have fails here, which is the drift
    the card guards against. The shell is the judge's own paste; the line is
    pinned by ``command_script`` before it reaches it.
    """
    command_script(command)
    code, body, _ = http(VERIFIER_BUNDLE)
    if code != 200:
        raise RuntimeError("%s -> HTTP %d" % (VERIFIER_BUNDLE, code))
    with tempfile.TemporaryDirectory(prefix="odile-page-command-") as tmp:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
            archive.extractall(tmp, filter="data")
        script = Path(tmp) / "odile-verify" / "odile-verify"
        if not script.is_file():
            raise RuntimeError("the bundle carries no odile-verify")
        os.chmod(script, 0o755)
        result = subprocess.run(["/bin/sh", "-c", command], cwd=tmp,
                                capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout + result.stderr


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return the 3xx itself: the card's DoD is about the 302, not its target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


#: Readings of the gate must see the 302; urllib's default opener follows it.
READER = urllib.request.build_opener(_NoRedirect)


def http(url: str, *, method: str = "GET", body: bytes | None = None,
         headers: dict[str, str] | None = None, cookie: str = "") -> tuple[int, bytes, dict]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with READER.open(request, timeout=20) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def sign_in(base: str, password: str) -> str:
    cookies = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    body = json.dumps({"provider": "basic", "username": "judge", "password": password}).encode()
    request = urllib.request.Request(base + "/auth/password-login", data=body,
                                     headers={"Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=20) as response:
            if response.status != 200:
                return ""
    except urllib.error.HTTPError:
        return ""
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in cookies)


def dig(name: str) -> list[str]:
    result = subprocess.run(["dig", "+short", name, "A"], text=True, capture_output=True)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def dns_checks(expected: str, resolve=dig) -> list[tuple[bool, str]]:
    """The two DNS rows: both public names answer the one expected address.

    Returns ``(ok, row)`` pairs instead of printing, so a unit test can drive
    both shapes without a network. The second row requires the machine's own
    name to answer the same address — a well-formed answer somewhere else is
    the tenant block pointing at a host that no longer serves this machine.
    """
    demo = resolve(DEMO_NAME)
    machine = resolve(MACHINE_NAME)
    return [
        (demo == [expected],
         f"dig {DEMO_NAME} A -> {', '.join(demo) or 'none'} (expected {expected})"),
        (machine == [expected] and machine == demo,
         f"dig {MACHINE_NAME} A -> {', '.join(machine) or 'none'} "
         "(the tenant's own name answers the same address, "
         f"expected {expected})"),
    ]


def certificate(name: str) -> tuple[str, str]:
    pem = ssl.get_server_certificate((name, 443), timeout=15)
    subject = subprocess.run(["openssl", "x509", "-noout", "-subject"], input=pem,
                             text=True, capture_output=True).stdout.strip()
    issuer = subprocess.run(["openssl", "x509", "-noout", "-issuer"], input=pem,
                            text=True, capture_output=True).stdout.strip()
    san = subprocess.run(["openssl", "x509", "-noout", "-ext", "subjectAltName"], input=pem,
                         text=True, capture_output=True).stdout
    return f"{subject} · {issuer} · {san.strip().splitlines()[-1] if san.strip() else ''}", pem


def wait_for_gate(seconds: float = 120) -> None:
    """The gateway takes ~20-40 s to answer after a restart; wait for it."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if http(DEMO + "/login")[0] == 200:
            return
        time.sleep(5)


def delete_session(cookie: str, session_id: str, *,
                   attempts: int = 6) -> tuple[bool, int, str]:
    """Delete one session with Hermes's fence; return (ok, code, body text).

    A session is a row only once a client opens it, and Hermes's fenced delete
    needs that row's own revision and generation, so they are read back from
    ``/api/sessions`` first — what the SPA sends. A storage hiccup answers 503
    (measured 2026-11-14), so the delete is retried; whether the row still
    exists is decided by the machine's own report, never by this list.
    """
    code, text = 0, ""
    for attempt in range(attempts):
        _, body, _ = http(DEMO + "/api/sessions", cookie=cookie)
        row: dict = {}
        try:
            row = next((item for item in json.loads(body).get("sessions", [])
                        if isinstance(item, dict) and item.get("id") == session_id), {})
        except ValueError:
            pass
        query = (f"?request_id={secrets.token_hex(16)}"
                 f"&expected_revision={row.get('runtime_revision')}"
                 f"&expected_generation={row.get('runtime_generation')}")
        code, body, _ = http(DEMO + "/api/sessions/" + session_id + query,
                             method="DELETE", cookie=cookie)
        text = body.decode("utf-8", "replace")
        try:
            deleted = json.loads(body).get("ok") is True
        except ValueError:
            deleted = False
        if code == 200 and deleted:
            return True, code, text
        if attempt + 1 < attempts:
            time.sleep(2)
    return False, code, text


def connect(username: str, password: str,
            bases: tuple[str, ...] = ()) -> tenant_console.Console:
    """Connect and open the one session every `shell.exec` runs in.

    `tenant_console.shell()` opens a session lazily when the console has none
    (tenant_console.py:192-196); that is a real row a judge sees in the
    sidebar. Open it here and record it, so the check can delete it again.
    """
    console = tenant_console.connect_any(username, password, bases)
    open_session(console)
    return console


def open_session(console: tenant_console.Console) -> str:
    """Create a session through the console and remember it for the cleanup."""
    session_id = console.open_session()
    OPENED.append(session_id)
    return session_id


def judge_delete_a_session(password: str, cookie: str, console=None) -> None:
    """A judge deletes a session: a mutation that must reach Hermes."""
    try:
        console = console or connect("judge", password)
        session_id = open_session(console)
    except tenant_console.ConsoleError as exc:
        check(False, f"open a session to delete: {exc}")
        return
    deleted, code, body = delete_session(cookie, session_id)
    check(deleted, f"DELETE /api/sessions/<a new session> with the judge session -> {code} "
                   f"{body[:80]}")
    check(http(DEMO + "/api/sessions/" + session_id, cookie=cookie)[0] == 404,
          "the session the judge deleted is gone (GET -> 404)")


def public_checks(password: str, target: str = DEFAULT_DNS) -> str:
    wait_for_gate()
    print("== dns")
    for ok, row in dns_checks(target):
        check(ok, row)

    print("== tls")
    detail, _ = certificate("demo.odile.fyi")
    check("demo.odile.fyi" in detail, f"certificate: {detail}")

    print("== the gate")
    code, body, headers = http(DEMO + "/login")
    text = body.decode("utf-8", "replace")
    blob = "\n".join(f"{k.lower()}: {v}" for k, v in headers.items()).lower()
    markers = ("<title>Sign in — Odile</title>", 'data-provider="basic"',
               "This is an Odile demo instance.", 'aria-label="Sign in"')
    missing = [marker for marker in markers if marker not in text]
    check(code == 200 and not missing,
          f"GET /login -> {code} (Odile's page{' — missing ' + repr(missing) if missing else ''})")
    check("strict-transport-security:" in blob and "x-robots-tag:" in blob,
          "the block's HSTS and noindex headers are on the page")
    code, _, headers = http(DEMO + "/")
    location = headers.get("Location") or headers.get("location") or ""
    check(code == 302 and "/login" in location, f"GET / anonymously -> {code} {location or '-'}")
    check(http(DEMO + "/api/sessions")[0] == 401, "GET /api/sessions anonymously -> 401")
    check(http(DEMO + "/openapi.json")[0] == 404, "GET /openapi.json -> 404 (no operator map)")

    print("== the tab icon")
    for name, pin, content_type in COMMITTED_ICONS:
        code, body, headers = http(DEMO + "/" + name)
        problem = icon_problem(name, code, headers, body)
        check(problem == "",
              f"GET /{name} -> {code} {content_type} {pin[:16]}…, immutable"
              + (f" — {problem}" if problem else ""))
        code, body, _ = http(DEMO + "/" + name, method="HEAD")
        check(code == 200 and body == b"",
              f"HEAD /{name} -> {code}, {len(body)} body bytes")
    code, body, _ = http(DEMO + "/login")
    head = body.decode("utf-8", "replace").split("</head>", 1)[0]
    missing = [name for name, _pin, _type in COMMITTED_ICONS
               if f'href="./{name}"' not in head]
    check(not missing and CREDIT_COMMENT in head and "data:image/svg+xml" not in head,
          "the sign-in page's head carries the three links and the credit comment, "
          "and no data: favicon" + (f" — missing {missing!r}" if missing else ""))
    for probe in ("/favicon.ico/../../etc/passwd", "/..%2f..%2fetc%2fpasswd"):
        code, body, _ = http(DEMO + probe)
        check(b"root:" not in body, f"GET {probe} -> {code}, no /etc/passwd")

    print("== the machine's own name (the tenant block survived)")
    check(http(MACHINE + "/verify")[0] == 200,
          f"GET {MACHINE}/verify -> 200")
    check(http(MACHINE + "/manifest")[0] == 200,
          f"GET {MACHINE}/manifest -> 200")
    first_visit = http(MACHINE + "/first-visit")[0]
    check(first_visit in (200, 302, 404),
          f"GET {MACHINE}/first-visit -> {first_visit} (the route answers; not 502)")

    print("== the judge")
    cookie = sign_in(DEMO, password) if password else ""
    if password:
        check(bool(cookie), "POST /auth/password-login as judge -> 200 with a session cookie")
    if cookie:
        code, body, _ = http(DEMO + "/", cookie=cookie)
        check(code == 200 and b'id="root"' in body,
              f"GET / with the judge session -> {code} (the Webapp shell)")
        code, body, _ = http(DEMO + "/api/sessions", cookie=cookie)
        sessions = []
        try:
            sessions = json.loads(body).get("sessions", [])
        except ValueError:
            sessions = []
        # The seed's title and flags are the owner's: a judge may rename or
        # unpin it, and the card says the apply never rewrites them. The row's
        # identity and its messages are the contract.
        seed_row = next((row for row in sessions if isinstance(row, dict)
                         and row.get("id") == SESSION_ID), {})
        check(code == 200 and (seed_row.get("message_count") or 0) >= 2,
              f"GET /api/sessions -> {code}, the seeded session is listed with its messages "
              f"({seed_row.get('id', '-')[:20]}…, {seed_row.get('message_count')} messages, "
              f"title {seed_row.get('title')!r})")

    print("== writes reach Hermes (the block has no mutation matcher)")
    if cookie:
        # t_69f1a5f3 (Savio, 2026-11-13): the deny-by-default matcher and its
        # runtime unfreeze line are deleted from the committed block, so a write
        # is Hermes's to answer — 422 for an empty body, 401 without a session.
        # A 403 from Caddy is the regression, not the expectation.
        code, _, _ = http(DEMO + "/api/config", method="PUT", body=b"{}",
                          headers={"Content-Type": "application/json"}, cookie=cookie)
        check(code == 422,
              f"PUT /api/config with the judge session -> {code} (reaches Hermes; 403 is the regression)")
        code, _, _ = http(DEMO + "/api/cron/jobs", method="POST", body=b"{}",
                          headers={"Content-Type": "application/json"}, cookie=cookie)
        check(code == 422, f"POST /api/cron/jobs with the judge session -> {code} (reaches Hermes)")
        code, _, _ = http(DEMO + "/api/config", method="PUT", body=b"{}",
                          headers={"Content-Type": "application/json"})
        check(code == 401,
              f"PUT /api/config unauthenticated -> {code} (the edge does not refuse it)")
        code, _, _ = http(DEMO + "/api/hermes/update", method="POST")
        check(code == 401,
              f"POST /api/hermes/update unauthenticated -> {code} (self-update is open; Hermes answers)")
        code, _, _ = http(DEMO + "/api/sessions", cookie=cookie)
        check(code == 200, f"GET /api/sessions with the judge session -> {code} (the control)")

    print("== the machine")
    code, body, _ = http(DEMO + "/attestation")
    document: dict = {}
    try:
        parsed = json.loads(body)
        document = parsed if isinstance(parsed, dict) else {}
    except ValueError:
        document = {}
    report_measurement = str(document.get("measurement") or "")
    check(code == 200 and bool(report_measurement), f"GET /attestation -> {code}")
    age = report_age_seconds(str(document.get("fetched_at") or ""))
    freshness = freshness_problem(document)
    observed = (f"fetched_at {document.get('fetched_at')}, {age / 60:.1f} min old"
                if age is not None else f"fetched_at {document.get('fetched_at')!r}")
    check(freshness == "",
          f"the report is no older than {REPORT_MAX_AGE_SECONDS // 60} minutes "
          f"(the five-minute row): {observed}"
          + (f" — {freshness}" if freshness else ""))
    code, body, _ = http(DEMO + "/verify", cookie=cookie)
    page = body.decode("utf-8", "replace")
    page_measurement = MEASUREMENT.search(page)
    problem = lede_problem(page)
    check(code == 200 and page_measurement is not None and problem == "",
          f"GET /verify -> {code} (the machine's own access report, the demo lede)"
          + (f" — {problem}" if problem else ""))
    check(page_measurement is not None and page_measurement.group(1) == report_measurement,
          "the /verify measurement is the CPU's: "
          f"{(page_measurement.group(1) if page_measurement else '-')[:24]}… = "
          f"{report_measurement[:24]}…")

    print("== the judge's own command")
    command = page_command(page)
    command_problem = page_command_problem(page, report_measurement)
    check(command_problem == "",
          "the /verify page prints one verifier command with this machine's "
          "measurement" + (f" — {command_problem}" if command_problem else ""))
    if command_problem == "":
        try:
            command_code, output = run_page_command(command)
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            check(False, f"the page's command ran: {exc}")
        else:
            last = output.strip().splitlines()[-1] if output.strip() else "(no output)"
            check(command_code == 0 and PASS_LINE in output,
                  f"the page's command exits {command_code}: {last[:160]}")
    return cookie


def tenant_state(console, password: str = "") -> tuple[dict, tenant_console.Console]:
    """The tenant's own full report, and a console that can still run one.

    The console survives only while its session is in the gateway's memory, so
    a `not_found` reconnects once (measured 2026-11-13: an idle console session
    is evicted). The new console is returned because its session is the one
    still alive — and the one the cleanup deletes last.
    """
    command = (
        "sudo /usr/bin/python3 /usr/local/lib/odile-demo-public/apply_demo_public.py --report")
    try:
        result = console.run(command, check=False)
    except tenant_console.ConsoleError:
        if not password:
            raise
        console = connect("judge", password)
        result = console.run(command, check=False)
    try:
        return json.loads((result.get("stdout") or "").strip() or "{}"), console
    except ValueError:
        return {}, console


def tenant_report(console, password: str = "") -> dict:
    """The tenant's own reading (the console may be replaced; see tenant_state)."""
    state, _ = tenant_state(console, password)
    return state


def no_demo_units(console) -> None:
    """No demo unit file and no demo timer — the boot re-assert is gone.

    The old unit re-ran the apply after the release installer on every boot.
    The machine must carry none of that: `list-unit-files` finds no
    ``odile-demo-*`` unit and `list-timers` finds no ``odile-demo-*`` timer.
    """
    units = console.run(
        f"systemctl list-unit-files '{DEMO_UNIT_GLOB}' --no-legend --no-pager",
        check=False).get("stdout", "")
    check(not units.strip(),
          f"no {DEMO_UNIT_GLOB} unit file: {units.strip()!r}")
    timers = console.run(
        f"systemctl list-timers --all '{DEMO_UNIT_GLOB}' --no-legend --no-pager",
        check=False).get("stdout", "")
    check(not timers.strip(),
          f"no {DEMO_UNIT_GLOB} timer: {timers.strip()!r}")


def tenant_checks(console, password: str = "") -> tenant_console.Console:
    print("== tenant settings")
    state, console = tenant_state(console, password)
    config = state.get("config") or {}
    check(bool(config.get("near_ai_direct")), "the model route is Near AI direct")
    provider = state.get("provider") or {}
    check(provider.get("name") == "Near AI",
          f"the provider row is named Near AI: {provider.get('name')!r}")
    check(provider.get("base_url") == NEAR_BASE_URL,
          f"the provider row routes at Near AI: {provider.get('base_url')!r}")
    check(provider.get("key_env") == NEAR_KEY_ENV,
          f"the provider row names the one key: {provider.get('key_env')!r}")
    keys = state.get("env_keys") or []
    check("HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY" in keys,
          "the .env carries the one Near AI key")

    print("== the /verify page (this machine's)")
    verify = state.get("verify") or {}
    check(bool(verify.get("demo_lede")),
          "the tenant's /verify template carries the demo lede (boot key, shared judge)")
    check(bool(verify.get("demo_command")),
          "the tenant's /verify template carries the one-line --expect command "
          "with its measurement")
    check(not verify.get("customer_lede"),
          "the tenant's /verify template no longer carries the customer-tenant row")
    check(bool(verify.get("rollback")), "the release page is kept for a rollback")

    print("== the Caddy block (installed and live bytes)")
    committed = hashlib.sha256(COMMITTED_BLOCK.read_bytes()).hexdigest()
    caddy = state.get("caddy") or {}
    check(caddy.get("installed_sha256") == committed,
          f"the installed demo-tenant-site.caddy is the committed block "
          f"({committed[:16]}…)")
    check(caddy.get("live_block_sha256") == committed,
          "the live /etc/caddy/Caddyfile block is the committed block")
    check(caddy.get("markers") == [1, 1],
          f"exactly one marker pair: {caddy.get('markers')}")
    names = caddy.get("matchers")
    check(isinstance(names, list) and not {"mutation", "update"} & set(names),
          f"the live block has no mutation or self-update matcher: {names}")
    check(caddy.get("residue_lines") == 0,
          f"no console residue after END: {caddy.get('residue_lines')}")
    check(caddy.get("active") == "active", f"caddy is {caddy.get('active')}")

    print("== the report refresh (the five-minute row)")
    refresh = state.get("attest_refresh") or {}
    committed_timer = hashlib.sha256(
        (HERE / "odile-attest-refresh.timer").read_bytes()).hexdigest()
    check(refresh.get("timer") == "active",
          f"odile-attest-refresh.timer is {refresh.get('timer') or 'unknown'!r} "
          f"(enabled: {refresh.get('enabled') or 'unknown'})")
    check(refresh.get("unit_sha256") == committed_timer,
          f"the installed timer is the committed file ({committed_timer[:16]}…)")
    check(refresh.get("result") in ("success", ""),
          f"the last refresh run's result is {refresh.get('result') or 'success'!r}")
    machine_problem = freshness_problem({"fetched_at": refresh.get("fetched_at")})
    check(machine_problem == "",
          "the tenant's own served report is no older than ten minutes: "
          + (machine_problem or f"fetched_at {refresh.get('fetched_at')}"))

    print("== the seeded session")
    session = state.get("session") or {}
    # A judge's own turns append messages, so the row carries *at least* the
    # two seed messages, and the seed's title and pinned flag are the owner's
    # (the card makes the apply never rewrite them); the resume below proves
    # the first two messages are the seed's.
    check(session.get("id") == SESSION_ID and (session.get("messages") or 0) >= 2,
          f"the seeded session is the constant id with its messages: {json.dumps(session)}")

    print("== no boot re-assert, no demo timer")
    no_demo_units(console)
    return console


def seed_turn_check(console, password: str) -> tenant_console.Console:
    """The judge's first action: open the seed and get an answer.

    A session-scoped RPC only reaches a session the gateway holds in memory, so
    this resumes the stored seed first — exactly what the Webapp does when the
    judge opens it — and then submits a prompt and reads the answer back out of
    the transcript. Measured 2026-11-13: without the resume, `prompt.submit`
    answers 4001 `permission_denied` (the gateway's "session not in memory"),
    which is why the receipt and the legacy lineage row matter.
    """
    print("== the seed's first turn")
    client = console
    reply = client.call("session.resume", {"session_id": SESSION_ID}, timeout=60)
    result = reply.get("result") or {}
    messages = [row for row in result.get("messages") or [] if isinstance(row, dict)]
    texts = [str(row.get("content") or "") for row in messages]
    check(reply.get("error") is None and len(texts) >= 2
          and texts[0].startswith("Start here") and texts[1].startswith("Welcome."),
          f"session.resume returns the seed's two messages ({len(texts)} in the transcript)")
    marker = "seed-" + secrets.token_hex(4)
    submitted = client.call("prompt.submit", {
        "session_id": SESSION_ID, "text": f"Reply with exactly: {marker}",
        "submission_id": secrets.token_hex(8)}, timeout=60)
    check(not submitted.get("error"),
          f"prompt.submit in the seeded session -> {json.dumps(submitted)[:200]}")
    answered = False
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline and not answered:
        time.sleep(5)
        again = client.call("session.resume", {"session_id": SESSION_ID}, timeout=60)
        rows = (again.get("result") or {}).get("messages") or []
        answered = any(str(row.get("content") or "").find(marker) >= 0
                       for row in rows if isinstance(row, dict) and row.get("role") == "assistant")
    check(answered, f"the seeded session's own turn answered: {marker}")
    return client


def persistence_check(console, password: str):
    """Write a file, restart the Webapp unit, and read the file back.

    The restart kills the socket it is asked over, so the check reconnects and
    returns the new console for the caller.
    """
    print("== persistence")
    nonce = secrets.token_hex(8)
    name = "check-" + nonce
    path = f"/home/tenant/workspace/{name}"
    console.run(f"printf %s {nonce} > {path}")
    try:
        console.run("sudo /usr/local/lib/odile-demo-public/restart-agent", check=False)
        check(False, "the restart did not close the console's own socket")
    except tenant_console.ConsoleError as exc:
        check("socket closed" in str(exc), f"restart odile-agent over its own socket: {exc}")
    # The gateway needs ~20-40 s to answer again (it serves this socket).
    fresh = None
    last: Exception | None = None
    deadline = time.monotonic() + 150
    while fresh is None and time.monotonic() < deadline:
        time.sleep(5)
        try:
            fresh = connect("judge", password)
        except tenant_console.ConsoleError as exc:
            last = exc
    if fresh is None:
        check(False, f"the tenant console after the restart: {last}")
        return console
    check(fresh.run(f"cat {path}", check=False).get("stdout", "").strip() == nonce,
          f"a file written in the workspace survives the restart: {path}")
    fresh.run(f"cd /home/tenant/workspace && rm -f {name}")
    return fresh


def running_sessions(client) -> dict[str, bool]:
    """The gateway's live view: session id -> a turn is running."""
    reply = client.call("session.list", {})
    rows = (reply.get("result") or {}).get("sessions") or []
    return {str(row.get("session_id") or row.get("id")): bool(row.get("running"))
            for row in rows if isinstance(row, dict)}


def fourth_session_check(console) -> tenant_console.Console:
    """The demo has no session cap: a fourth concurrent turn runs.

    Three sessions take a turn that runs a 20-second command; a fourth takes
    the same turn. On 2026-11-13 the old three-session limit held the fourth
    (`status: queued`, `running: false`) while the three ran. With no limit it
    runs too. All four are cancelled here and their rows deleted by the
    cleanup, which sweeps every session the check opened.
    """
    print("== a fourth concurrent session runs")
    client = console
    slow = ("Run exactly this in your terminal and then reply with the single word ok: "
            "sleep 20")
    holders = [open_session(client) for _ in range(3)]
    for session_id in holders:
        client.call("prompt.submit", {"session_id": session_id, "text": slow,
                                      "submission_id": secrets.token_hex(8)})
    time.sleep(10)
    live = running_sessions(client)
    check(sum(1 for session_id in holders if live.get(session_id)) == 3,
          f"three turns run at once: {[live.get(row) for row in holders]}")
    fourth = open_session(client)
    reply = client.call("prompt.submit", {"session_id": fourth, "text": slow,
                                          "submission_id": secrets.token_hex(8)})
    check(not reply.get("error"),
          f"the fourth submit is admitted: {json.dumps(reply)[:200]}")
    time.sleep(5)
    live = running_sessions(client)
    check(live.get(fourth) is True,
          f"the fourth session's turn runs while the other three do: "
          f"{[live.get(row) for row in holders + [fourth]]}")
    for session_id in holders + [fourth]:
        client.call("prompt.cancel", {"session_id": session_id})
    # A cancelled turn is not instantly idle, and Hermes refuses the fenced
    # delete while it is still running (measured 2026-11-14: the row survived
    # the cleanup and the count went red). Wait for the four to be idle.
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        time.sleep(2)
        live = running_sessions(client)
        if not any(live.get(session_id) for session_id in holders + [fourth]):
            break
    return client


def session_report(console, password: str, session_id: str) -> tuple[dict, tenant_console.Console]:
    """One session's row and message digest, from the machine's own report.

    `--report --session-id` reads the tenant's `state.db` directly, so "gone"
    here is a fact about the store, not about the `/api/sessions` window (that
    endpoint lists at most 20 rows). Returns the console that ran it, which may
    be a reconnect.
    """
    command = f"sudo /usr/bin/python3 {APPLY} --report --session-id {session_id}"
    try:
        result = console.run(command, check=False)
    except tenant_console.ConsoleError:
        if not password:
            raise
        console = connect("judge", password)
        result = console.run(command, check=False)
    try:
        return json.loads((result.get("stdout") or "").strip() or "{}"), console
    except ValueError:
        return {}, console


def sweep_sessions(console, password: str,
                   cookie: str) -> tuple[tenant_console.Console, list[str]]:
    """Delete every session the check opened except the live console's own.

    Existence is the machine's own `--report --session-id`, never the list
    endpoint: `/api/sessions` shows at most 20 rows, and a row whose store
    answers `storage_unavailable` can read as `title: null` / `message_count:
    0` there (measured 2026-11-14) — deleting by list shape lost three real
    sessions. The console's own session is left for the caller, which deletes
    it last, because its `shell.exec` is the report transport.
    """
    done: set[str] = set()
    survivors: list[str] = []
    for _ in range(64):
        pending = [session_id for session_id in dict.fromkeys(OPENED)
                   if session_id not in done and session_id != console.session_id]
        if not pending:
            return console, survivors
        for session_id in pending:
            done.add(session_id)
            try:
                row, console = session_report(console, password, session_id)
            except tenant_console.ConsoleError:
                console = connect("judge", password)
                row, console = session_report(console, password, session_id)
            if not row.get("exists"):
                continue
            delete_session(cookie, session_id)
            row, console = session_report(console, password, session_id)
            if row.get("exists"):
                survivors.append(session_id)
    survivors.extend(session_id for session_id in dict.fromkeys(OPENED)
                     if session_id not in done and session_id != console.session_id)
    return console, survivors


def deploy_changes_no_session(console, password: str,
                              cookie: str) -> tenant_console.Console:
    """A deploy changes no session — the card's core sentence, measured.

    Opens a session, gives it one turn, samples the sessions already on the
    machine (the seed and up to four others with messages), runs the demo's own
    apply with `--no-restart` exactly as `deploy_demo_public.py` does, and
    compares. The old apply pruned every session but the seed and rewrote the
    seed's title, flags and messages; either regression fails here. The
    session this check opened is deleted at the end, and the cleanup sweeps it
    if this leg fails before that.
    """
    print("== a deploy changes no session")
    created = open_session(console)
    marker = "deploy-" + secrets.token_hex(4)
    submitted = console.call("prompt.submit", {
        "session_id": created, "text": f"Reply with exactly: {marker}",
        "submission_id": secrets.token_hex(8)}, timeout=60)
    check(not submitted.get("error"),
          f"a session is opened and takes a message: {json.dumps(submitted)[:160]}")
    answered = False
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline and not answered:
        time.sleep(5)
        reply = console.call("session.resume", {"session_id": created}, timeout=60)
        rows = (reply.get("result") or {}).get("messages") or []
        answered = any(str(row.get("content") or "").find(marker) >= 0
                       for row in rows if isinstance(row, dict) and row.get("role") == "assistant")
    check(answered, f"the new session's own turn answered: {marker}")

    code, body, _ = http(DEMO + "/api/sessions", cookie=cookie)
    rows: list[dict] = []
    try:
        rows = [row for row in json.loads(body).get("sessions", []) if isinstance(row, dict)]
    except ValueError:
        rows = []
    sample: list[str] = []
    for session_id in [created, SESSION_ID] + [str(row.get("id")) for row in rows
                                              if row.get("id") and (row.get("message_count") or 0) > 0]:
        if session_id and session_id not in sample:
            sample.append(session_id)
    sample = sample[:6]
    before: dict[str, dict] = {}
    for session_id in sample:
        row, console = session_report(console, password, session_id)
        before[session_id] = row
    check(all(row.get("exists") for row in before.values()),
          f"the sampled sessions exist before the apply: "
          f"{[s for s in sample if not before[s].get('exists')]}")

    console.run(f"printf %s {password} > /tmp/odile-judge-password")
    console.run(f"sudo install -m 0600 -o root -g root /tmp/odile-judge-password "
                f"{PASSWORD_REMOTE}")
    console.run("cd /tmp && rm -f odile-judge-password")
    apply_command = (
        f"sudo PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 {APPLY} --write --no-restart "
        f"--password-file {PASSWORD_REMOTE}")
    try:
        result = console.run(apply_command, check=False)
    except tenant_console.ConsoleError as exc:
        # A non-converged machine's apply restarts Caddy, which drops the very
        # socket it runs over. Reconnect and run the now-converged apply once.
        print(f"apply        the socket closed under Caddy's restart ({exc})")
        time.sleep(4)
        console = connect("judge", password)
        result = console.run(apply_command, check=False)
    console.run("cd /run && sudo rm -f odile-demo-secret", check=False)
    output = (result.get("stdout") or "").strip()
    check(result.get("code") == 0,
          f"the apply runs on the machine: rc={result.get('code')}"
          + (f" ({output.splitlines()[-1]})" if output else ""))
    after: dict[str, dict] = {}
    for session_id in sample:
        row, console = session_report(console, password, session_id)
        after[session_id] = row
    changed = []
    for session_id in sample:
        old, new = before[session_id], after[session_id]
        if not new.get("exists"):
            changed.append(f"{session_id}: gone")
            continue
        # Hermes titles a session after its first turn, so a title that moves
        # is the app's own write; the row's identity, flags and messages are
        # what a deploy must not change. The seed's title is the seed's.
        keys = ["started_at", "pinned", "hidden", "archived", "messages_sha256"]
        if session_id == SESSION_ID:
            keys.append("title")
        if any(new.get(key) != old.get(key) for key in keys):
            changed.append(f"{session_id}: the row or its messages changed")
    check(not changed, f"every sampled session is unchanged by the apply ({len(sample)}): "
                       f"{changed[:3]}")
    deleted, code, body = delete_session(cookie, created)
    check(deleted, f"DELETE the session the check opened -> {code} {body[:80]}")
    check(http(DEMO + "/api/sessions/" + created, cookie=cookie)[0] == 404,
          "the session the check opened is gone (GET -> 404)")
    return console


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the demo's definition of done.")
    parser.add_argument("--password-file", default="", help="the published judge password")
    parser.add_argument("--base", default="",
                        help="force one console base (default: the demo host, then the machine's own name)")
    parser.add_argument("--target", default=DEFAULT_DNS,
                        help="the A record both public names must answer "
                             f"(default: the tree's redacted {DEFAULT_DNS})")
    parser.add_argument("--persistence", action="store_true",
                        help="write a file and restart odile-agent (part of done)")
    parser.add_argument("--sessions", action="store_true",
                        help="measure that a fourth concurrent session runs")
    args = parser.parse_args(argv)

    password = (Path(args.password_file).read_text(encoding="utf-8").strip()
                if args.password_file else "")
    cookie = public_checks(password, args.target)
    if password:
        try:
            console = connect("judge", password, (args.base,) if args.base else ())
        except tenant_console.ConsoleError as exc:
            check(False, f"the tenant console as judge: {exc}")
        else:
            # The baseline is the tenant's own count, read through the console
            # whose row is the one this count includes and the cleanup keeps
            # until last. Everything else the check opens must be gone again.
            state, console = tenant_state(console, password)
            before = state.get("session_count")
            before = before - 1 if isinstance(before, int) else None
            try:
                console = tenant_checks(console, password)
                judge_delete_a_session(password, cookie)
                console = deploy_changes_no_session(console, password, cookie)
                console = seed_turn_check(console, password)
                if args.persistence:
                    console = persistence_check(console, password)
                if args.sessions:
                    console = fourth_session_check(console)
            finally:
                console, survivors = sweep_sessions(console, password, cookie)
                state, console = tenant_state(console, password)
                after = state.get("session_count")
                after = after - 1 if isinstance(after, int) else None
                check(before is not None and after == before,
                      f"the check leaves the session count where it found it "
                      f"({before} before, {after} after, {len(OPENED)} session(s) opened)")
                check(not survivors,
                      f"every session the check opened is gone "
                      f"({len(OPENED)} opened): {survivors}")
                primary = console.session_id
                deleted, code, body = delete_session(cookie, primary)
                check(deleted and http(DEMO + "/api/sessions/" + primary,
                                       cookie=cookie)[0] == 404,
                      f"DELETE the check's own console session ({primary[:20]}…) -> {code} "
                      f"{body[:60]}")
    else:
        print("-- no --password-file: the tenant half skipped")

    print(f"\ndemo public check: {'PASS' if FAILED == 0 else 'FAIL'} "
          f"({PASSED} passed, {FAILED} failed)")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
