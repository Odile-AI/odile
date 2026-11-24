#!/usr/bin/python3
"""Move demo.odile.fyi onto Odile's tenant machine — ODL-503.

One command, idempotent, with a dry run. It does four things in order and
prints one line per fact:

  1. **DNS.** `demo.odile.fyi` A → 203.0.113.87, DNS-only, through
     `deploy/demo-host/create_dns.py` (the Cloudflare token is read at run
     time and never printed).
  2. **The tenant's surface.** The files the machine needs are uploaded over
     its own Webapp socket — Odile holds no SSH key to a tenant — and
     `apply_demo_public.py` runs there as root: Odile's sign-in page and its
     three tab files, the `demo.odile.fyi` Caddy block, Near AI direct with one
     key, the judge account, and the one seeded "Start here — Odile demo"
     session, written only if it is not already there. The release page this
     tree ships (`templates/verify.html`, the bytes the signed installer
     embeds) goes with the apply: the apply re-cuts the served `/verify` from
     it when the image's first-boot page predates a release cut, and keeps it
     as `verify.html.release` for the runbook's rollback. The machine's current
     release-setup script is installed too: it converges the release installer
     once (a done marker under `/var/lib/odile`), so a boot cannot rewrite the
     config the owner changed.
  3. **The gate over HTTPS**, from HQ: `/login` is Odile's page, anonymous `/`
     is a 302 to it, the judge signs in, and `/verify` is the tenant's own
     report whose measurement is the one `/attestation` carries; the page's
     one-line `--expect` command is run end to end.
  4. **The DNS record.** The HQ demo copy is retired (2026-11-13), so the
     program prints the one-line command that moves the record; a switch back
     also needs an HQ demo restored, which this tree no longer carries.

The first run signs in to the tenant with the account it already has
(`--access-user tenant --access-password-file PATH`); every later run signs in
as `judge` with the published password, which this program reads from the
sign-in page's `DEMO_PASSWORD` literal — the one published copy since ODL-901
retired README.md's judge table — unless `--judge-password-file` is given.

    ./deploy/demo-host/deploy_demo_public.py --dry-run
    ./deploy/demo-host/deploy_demo_public.py

Exit codes: 0 converged, 1 a failed apply or verification, 2 bad input.
"""

from __future__ import annotations

import argparse
from http.cookiejar import CookieJar
import hashlib
import importlib.util
import json
import re
import secrets
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import create_dns  # noqa: E402
import tenant_console  # noqa: E402

LIB_REMOTE = "/usr/local/lib/odile-demo-public"
PASSWORD_REMOTE = "/run/odile-demo-secret"
DEMO_HOST = "demo.odile.fyi"
TENANT_BASE = tenant_console.DEFAULT_BASE
TARGET_IP = create_dns.TENANT_IP
MACHINE_HOST = "odl-7971a8.odile.fyi"
NEAR_BASE_URL = "https://cloud-api.near.ai/v1"
NEAR_KEY_ENV = "HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY"
COMMITTED_BLOCK = REPO / "deploy/caddy/demo-tenant-site.caddy"

#: The apply's no-rewrite contract (t_ee5aac66), spelled in
#: `apply_demo_public.py`'s own module constant. This program uploads the file
#: that sits beside it, so a tree whose copy predates that fix would put the
#: pruning program back on the tenant — measured 2026-11-17: the live check lost
#: the seed's messages, found one session more than it opened, and the apply read
#: `demo provisioned` where a converged machine reads `demo converged`. The test
#: suite's fence pins `main`, which cannot see a deploy run from a branch; this
#: reads the bytes about to be uploaded, whatever branch they came from.
APPLY_SOURCE = HERE / "apply_demo_public.py"
APPLY_MARKER = "odile-demo-public-apply/2: seed written once, nothing pruned"


def apply_problem(source: Path | None = None) -> str:
    """Why this tree's apply must not be deployed; '' when it may be.

    A marker rather than the program's shape: the deploy ships the file, not a
    hash of it, so the check is "does the copy about to run carry the contract".
    A missing file is a refusal too — an uploaded program is what runs as root.
    """
    path = source if source is not None else APPLY_SOURCE
    if not path.is_file():
        return f"this tree has no {path.name} beside the deploy program"
    text = path.read_text(encoding="utf-8", errors="replace")
    if APPLY_MARKER not in text:
        return (f"{path} carries no no-rewrite marker ({APPLY_MARKER!r}): this "
                "copy cannot prove it stops the prune and the seed rewrite")
    return ""


#: Files the tenant needs, by name. The apply program checks the sign-in page
#: for its markers before installing it; the others are data. `login.html` is
#: skipped with `--no-login`: a newer sign-in page (t_9896a577) is live, and a
#: deploy from a tree that has not merged it must not revert the artwork.
UPLOADS = (
    ("apply_demo_public.py", 0o755),
    ("demo-tenant-site.caddy", 0o644),
    ("login.html", 0o644),
    ("verify.html", 0o644),
    ("verify-lede.html", 0o644),
    ("verify-command.html", 0o644),
    ("start-here.md", 0o644),
    ("session-receipt.json", 0o644),
    # The demo's five-minute report refresh (t_145ca49c): the wrapper is the
    # HQ one (deploy/attest-refresh/refresh.sh, unchanged), the two units are
    # this directory's. The apply installs them from these uploaded copies.
    ("attest-refresh.sh", 0o644),
    ("odile-attest-refresh.service", 0o644),
    ("odile-attest-refresh.timer", 0o644),
)
#: The receipt template lives in the reviewed demo seed (moved here from the
#: retired demo workspace tree, t_9d7ee536).
RECEIPT_SOURCE = REPO / "deploy/demo-host/seed/session-receipt.json"
#: The image's release-setup script. It is installed on the machine so the
#: release installer converges once instead of every boot (t_ee5aac66): the
#: script writes a done marker under /var/lib/odile, so a boot cannot rewrite
#: a config key the owner changed.
RELEASE_SETUP_SOURCE = REPO / "deploy/image/odile-release-setup.sh"
RELEASE_SETUP_REMOTE = "/usr/local/lib/odile-image/odile-release-setup.sh"
#: The release page the signed installer embeds and writes at first boot. The
#: image on the demo host predates ODL-929's cut, and the release setup
#: converges once, so the page and its rollback copy would keep the retired
#: sentence forever; the apply uploads this file and refreshes both
#: (t_0dff2799). It is read from the tree, not re-cut here.
RELEASE_PAGE_SOURCE = REPO / "templates" / "verify.html"

LOGIN_MARKERS = (
    "<title>Sign in — Odile</title>",
    'data-provider="basic"',
    "This is an Odile demo instance.",
    'aria-label="Sign in"',
)
#: The sign-in page's three tab files (t_6b25f6e5). The committed `web/`
#: renditions are what a browser's tab shows; the deploy puts them beside the
#: page in `/srv/odile-demo`, where the block's one static handle answers them
#: first-party and immutable. Pinned by hash here, again by the apply, and
#: again by the check: no hop installs a different picture under the name.
ICONS = (
    ("favicon.ico",
     "8f5f997684f2d8eb2336eef46ef8e100776ecaa35c0375faadd4bb7e1ab17fba", "image/x-icon"),
    ("icon-32.png",
     "28bedf61e9f5bad25d9a7b874bab788634299eb1e08a9bd99f614a09f9f904bc", "image/png"),
    ("apple-touch-icon.png",
     "dd5386a9c772a5d67702f35fd22eed649783a8575d54f4cbd641111434123773", "image/png"),
)
ICON_SOURCES = {name: REPO / "web" / name for name, _pin, _type in ICONS}
#: BRAND.md §Name and mark: the credit sits in the HTML comment beside the
#: link on every page. The sign-in page is a page.
CREDIT_COMMENT = ("<!-- Icon: Lucas Cranach the Elder, Saints Christina and Ottilia "
                  "(Katharinenaltar, Dresden). Public domain. Source: Wikimedia Commons. -->")
#: The demo machine's /verify lede. One committed file: the apply uploads and
#: injects it, and this program asserts the served page opens with it.
VERIFY_LEDE = HERE / "verify-lede.html"
CUSTOMER_SENTENCE = "No one at Odile can log in unless you grant it."
SECURITY_HEADERS = ("strict-transport-security:", "x-robots-tag:")
MEASUREMENT = re.compile(r"\b([0-9a-f]{96})\b")
#: The demo command section's own markers; `apply_demo_public.py` inserts the
#: section (and replaces it in place). The deploy asserts the marker itself so
#: a stale template cannot pass: a page with no section serves no command, and
#: two sections give a judge two lines to choose from.
COMMAND_BEGIN = "<!--demo-check-->"
COMMAND_END = "<!--/demo-check-->"
COMMAND_RE = re.compile(re.escape(COMMAND_BEGIN) + r".*?" + re.escape(COMMAND_END), re.S)
DEMO_COMMAND = "./odile-verify https://demo.odile.fyi --expect"

#: Every session this deploy's console opened, in order. A console's own
#: `shell.exec` session is a row in the judge's sidebar, so each one is
#: deleted before the deploy returns (t_ee5aac66): a deploy changes no
#: session, and it leaves none of its own behind either.
OPENED: list[str] = []
#: What the cleanup needs after `_run` returns or raises.
CLEANUP = {"password": "", "base": f"https://{DEMO_HOST}"}


def open_console_session(console: tenant_console.Console) -> None:
    """Open the console's one session and remember it for the cleanup."""
    console.open_session()
    if console.session_id:
        OPENED.append(console.session_id)
    CLEANUP["base"] = console.base


def delete_session(cookie: str, base: str, session_id: str, *,
                   attempts: int = 3) -> bool:
    """Delete one session with Hermes's fence — what the SPA sends.

    The revision and generation are read back from `/api/sessions`; a storage
    hiccup (503, measured 2026-11-14) is retried.
    """
    for attempt in range(attempts):
        _, body, _ = http(base + "/api/sessions", cookie=cookie)
        row: dict = {}
        try:
            row = next((item for item in json.loads(body).get("sessions", [])
                        if isinstance(item, dict) and item.get("id") == session_id), {})
        except ValueError:
            pass
        query = (f"?request_id={secrets.token_hex(16)}"
                 f"&expected_revision={row.get('runtime_revision')}"
                 f"&expected_generation={row.get('runtime_generation')}")
        code, body, _ = http(base + "/api/sessions/" + session_id + query,
                             method="DELETE", cookie=cookie)
        try:
            if code == 200 and json.loads(body).get("ok") is True:
                return True
        except ValueError:
            pass
        if attempt + 1 < attempts:
            time.sleep(2)
    return False


def cleanup_console_sessions() -> None:
    """Delete every session this deploy opened; never mask the deploy's result."""
    if not OPENED or not CLEANUP["password"]:
        return
    base = CLEANUP["base"]
    cookie = sign_in(base, CLEANUP["password"])
    if not cookie:
        print(f"cleanup      cannot sign in to delete {len(OPENED)} console session(s)",
              file=sys.stderr)
        return
    sessions = list(dict.fromkeys(OPENED))
    failed = [session_id for session_id in sessions
              if not delete_session(cookie, base, session_id)]
    print(f"cleanup      {len(sessions) - len(failed)} console session(s) deleted"
          + (f"; {len(failed)} survived (another account owns them?)" if failed else ""))


def published_password(repo: Path = REPO) -> str:
    """The demo password the sign-in page publishes.

    ODL-901 retired the README's judge table, so the page the judge lands on
    is the single published copy: it carries the password as the literal it
    posts to the gateway, and tests/test_demo_login_page.py pins its sha256.
    README.md's table stays as the fallback for a checkout from before that
    retirement.

    This mirrors `deploy_demo_icons.published_password()`: the two deployers
    sign in to the same machine as the same account, so they must read the
    same copy.
    """
    page = (repo / "deploy" / "demo-host" / "login.html").read_text(encoding="utf-8")
    rows = re.findall(r"var DEMO_PASSWORD = '([^']+)';", page)
    if len(set(rows)) == 1:
        return rows[0]
    text = (repo / "README.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\|\s*Password\s*\|\s*`([^`]+)`\s*\|$", text, re.M)
    if len(set(rows)) != 1:
        raise SystemExit("error: deploy/demo-host/login.html has no single DEMO_PASSWORD")
    return rows[0]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return the 3xx itself: the card's DoD is about the 302, not its target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


#: Readings of the gate must see the 302; urllib's default opener follows it.
READER = urllib.request.build_opener(_NoRedirect)


def http(url: str, *, method: str = "GET", body: bytes | None = None,
         headers: dict[str, str] | None = None, cookie: str = "") -> tuple[int, bytes, dict]:
    request = urllib.request.Request(url, data=body, method=method,
                                     headers=headers or {})
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with READER.open(request, timeout=20) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def sign_in(base: str, password: str) -> str:
    """Sign in as judge; return the cookie header, or '' on a refusal."""
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


def wait_for_dns(name: str, address: str, seconds: float = 900.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            answers = {info[4][0] for info in socket.getaddrinfo(name, 443)}
        except socket.gaierror:
            answers = set()
        if address in answers:
            print(f"dns          {name} -> {', '.join(sorted(answers))}")
            return True
        print(f"dns          {name} -> {', '.join(sorted(answers)) or 'no answer'}; "
              "waiting for the record")
        time.sleep(5)
    return False


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


def demo_check_problem(body: str) -> str:
    """Why the served /verify is not the demo's one marked command section.

    The demo's promise is a section present once, holding the one-line
    ``--expect`` command a judge copies. A stale page (no section) and a
    doubled page (two lines to copy) both fail; the deploy records a FAIL, so
    a run that leaves either behind cannot print PASS (ODL-563).
    """
    opens, closes = body.count(COMMAND_BEGIN), body.count(COMMAND_END)
    if (opens, closes) != (1, 1):
        return (f"the page carries {opens} {COMMAND_BEGIN!r} marker(s) and "
                f"{closes} {COMMAND_END!r} closer(s), expected one of each")
    section = COMMAND_RE.search(body)
    if section is None or DEMO_COMMAND not in section.group(0):
        return "the demo-check section prints no ./odile-verify --expect command"
    return ""


def verify_public(judge_password: str) -> tuple[bool, list[str]]:
    """Assert the card's definition of done over HTTPS, from HQ."""
    base = f"https://{DEMO_HOST}"
    lines: list[str] = []
    ok = True

    def record(good: bool, text: str) -> None:
        nonlocal ok
        ok = ok and good
        lines.append(("ok   " if good else "FAIL ") + text)

    code, body, headers = http(base + "/login")
    text = body.decode("utf-8", "replace")
    missing = [marker for marker in LOGIN_MARKERS if marker not in text]
    header_blob = "\n".join(f"{k.lower()}: {v}" for k, v in headers.items()).lower()
    missing_headers = [name for name in SECURITY_HEADERS if name not in header_blob]
    record(code == 200 and not missing and not missing_headers,
           f"GET /login -> {code}" + (f", missing {missing!r}" if missing else "")
           + (f", missing headers {missing_headers!r}" if missing_headers else ""))
    head = text.split("</head>", 1)[0]
    head_missing = [name for name, _pin, _type in ICONS
                    if f'href="./{name}"' not in head]
    record(not head_missing and CREDIT_COMMENT in head
           and "data:image/svg+xml" not in head,
           "the sign-in page's head carries the three tab links and the credit "
           "comment, and no data: favicon"
           + (f", missing {head_missing!r}" if head_missing else ""))

    for name, pin, content_type in ICONS:
        code, body, headers = http(base + "/" + name)
        digest = hashlib.sha256(body).hexdigest() if body else ""
        cache = (headers.get("Cache-Control") or "").lower()
        record(code == 200 and digest == pin
               and (headers.get("Content-Type") or "").split(";")[0] == content_type
               and "immutable" in cache and "max-age=31536000" in cache,
               f"GET /{name} -> {code} {(headers.get('Content-Type') or '-').split(';')[0]} "
               f"{digest[:16]}… == {pin[:16]}…, {cache or '-'}")
        code, body, _ = http(base + "/" + name, method="HEAD")
        record(code == 200 and body == b"",
               f"HEAD /{name} -> {code}, {len(body)} body bytes")

    for probe in ("/favicon.ico/../../etc/passwd", "/..%2f..%2fetc%2fpasswd"):
        code, body, _ = http(base + probe)
        record(b"root:" not in body, f"GET {probe} -> {code}, no /etc/passwd")

    code, _, headers = http(base + "/")
    location = headers.get("Location") or headers.get("location") or ""
    record(code == 302 and "/login" in location,
           f"GET / anonymously -> {code} {location or '-'}")

    code, _, _ = http(base + "/api/sessions")
    record(code == 401, f"GET /api/sessions anonymously -> {code}")

    machine = f"https://{MACHINE_HOST}"
    code, _, _ = http(machine + "/verify")
    record(code == 200,
           f"GET {MACHINE_HOST}/verify -> {code} (the machine's own block survived)")
    code, _, _ = http(machine + "/manifest")
    record(code == 200, f"GET {MACHINE_HOST}/manifest -> {code}")
    code, _, _ = http(machine + "/first-visit")
    record(code in (200, 302, 404),
           f"GET {MACHINE_HOST}/first-visit -> {code} (the route answers)")

    cookie = sign_in(base, judge_password)
    record(bool(cookie), "POST /auth/password-login as judge -> 200 with a session cookie")
    if cookie:
        code, body, _ = http(base + "/", cookie=cookie)
        record(code == 200 and b'id="root"' in body,
               f"GET / with the judge session -> {code} (the Webapp shell)")
        code, body, _ = http(base + "/api/sessions", cookie=cookie)
        titles: set[str] = set()
        try:
            titles = {row.get("title") or "" for row in json.loads(body).get("sessions", [])
                      if isinstance(row, dict)}
        except ValueError:
            titles = set()
        record(code == 200 and "Start here — Odile demo" in titles,
               f"GET /api/sessions with the judge session -> {code}; the seeded "
               "'Start here — Odile demo' session is the one it lands in")
        code, body, _ = http(base + "/verify", cookie=cookie)
        verify_text = body.decode("utf-8", "replace")
        page_measurement = MEASUREMENT.search(verify_text)
        problem = lede_problem(verify_text)
        record(code == 200 and page_measurement is not None and problem == "",
               f"GET /verify -> {code} (the machine's access report, the demo lede)"
               + (f" — {problem}" if problem else ""))
        section_problem = demo_check_problem(verify_text)
        record(section_problem == "",
               "the /verify page carries exactly one demo-check section with its "
               "one-line --expect command"
               + (f" — {section_problem}" if section_problem else ""))
        code, body, _ = http(base + "/attestation")
        try:
            report_measurement = str(json.loads(body).get("measurement") or "")
        except ValueError:
            report_measurement = ""
        record(code == 200 and bool(report_measurement),
               f"GET /attestation -> {code} (the CPU's report)")
        record(bool(page_measurement) and page_measurement.group(1) == report_measurement,
               "/verify's measurement is /attestation's: "
               f"{(page_measurement.group(1) if page_measurement else '-')[:24]}… vs "
               f"{report_measurement[:24]}…")
        command = re.search(
            r"\./odile-verify https://demo\.odile\.fyi --expect ([0-9a-f]{96})",
            verify_text)
        record(command is not None and command.group(1) == report_measurement,
               "the /verify page prints the one-line --expect command with this "
               "machine's measurement")
    return ok, lines


def tenant_assertions(console) -> bool:
    """Assert the tenant's own bytes: the two Caddy copies and the provider row.

    The live repair after review: `/usr/local/lib/odile-demo-public/
    demo-tenant-site.caddy` had become a copy of the whole Caddyfile and the
    live file carried a console banner after `# END`. The apply re-uploads the
    block and strips the banner; this reads the result back and fails loudly if
    either came back.
    """
    command = ("sudo /usr/bin/python3 /usr/local/lib/odile-demo-public/"
               "apply_demo_public.py --report")
    result = console.run(command, check=False)
    try:
        state = json.loads((result.get("stdout") or "").strip() or "{}")
    except ValueError:
        print(f"error: apply --report returned no JSON: "
              f"{(result.get('stdout') or '')[:300]}", file=sys.stderr)
        return False
    committed = hashlib.sha256(COMMITTED_BLOCK.read_bytes()).hexdigest()
    caddy = state.get("caddy") or {}
    provider = state.get("provider") or {}
    ok = True

    def record(good: bool, text: str) -> None:
        nonlocal ok
        ok = ok and good
        print(("ok   " if good else "FAIL ") + text)

    record(caddy.get("installed_sha256") == committed,
           f"installed demo-tenant-site.caddy == the committed block ({committed[:16]}…)")
    record(caddy.get("live_block_sha256") == committed,
           "the live /etc/caddy/Caddyfile block == the committed block")
    record(caddy.get("markers") == [1, 1],
           f"exactly one marker pair: {caddy.get('markers')}")
    icons = state.get("icons") or {}
    for name, pin, _type in ICONS:
        record(icons.get(name) == pin,
               f"/srv/odile-demo/{name} == the committed {pin[:16]}…")
    names = caddy.get("matchers")
    record(isinstance(names, list) and not {"mutation", "update"} & set(names),
           f"the live block has no mutation or self-update matcher: {names}")
    record(caddy.get("residue_lines") == 0,
           f"no console residue after END: {caddy.get('residue_lines')}")
    record(caddy.get("active") == "active", f"caddy is {caddy.get('active')}")
    record(provider.get("name") == "Near AI",
           f"the provider row is named Near AI: {provider.get('name')!r}")
    record(provider.get("base_url") == NEAR_BASE_URL,
           f"the provider route is Near AI direct: {provider.get('base_url')!r}")
    record(provider.get("key_env") == NEAR_KEY_ENV,
           f"the provider row names the one key: {provider.get('key_env')!r}")
    refresh = state.get("attest_refresh") or {}
    record(refresh.get("timer") == "active",
           f"odile-attest-refresh.timer is active (enabled: {refresh.get('enabled')!r})")
    record(refresh.get("result") in ("success", ""),
           f"the last refresh run's result is {refresh.get('result') or 'success'!r}")
    return ok


def ensure_release_setup(console, dry: bool) -> bool:
    """Install this tree's release-setup script on the machine, once.

    The script is the image's second install half. It used to re-run the
    signed installer at every boot, which rewrote the config's managed blocks
    (back to the meter) and the .env allowlist. It now writes a done marker
    under ``/var/lib/odile`` after its first run and exits `same` on every
    later boot, so the owner's config survives a reboot. This deploy is how a
    running machine gets those bytes; a fresh image build gets them from
    ``deploy/image/``.
    """
    local = RELEASE_SETUP_SOURCE.read_bytes()
    digest = hashlib.sha256(local).hexdigest()
    remote = console.run(f"sudo sha256sum {RELEASE_SETUP_REMOTE}", check=False)
    current = (remote.get("stdout") or "").split(" ")[0].strip()
    if current == digest:
        print(f"same         {RELEASE_SETUP_REMOTE} (release setup, converges once)")
        return False
    print(f"{'would upload' if dry else 'upload'}       "
          f"odile-release-setup.sh -> {RELEASE_SETUP_REMOTE}")
    if not dry:
        console.put(str(RELEASE_SETUP_SOURCE), RELEASE_SETUP_REMOTE, mode="0755")
        check = console.run(f"sudo sha256sum {RELEASE_SETUP_REMOTE}", check=False)
        installed = (check.get("stdout") or "").split(" ")[0].strip()
        if installed != digest:
            raise SystemExit(f"error: {RELEASE_SETUP_REMOTE} is not the uploaded bytes")
    return True


def self_test() -> int:
    """The pure parts, plus the tenant program's own self-test."""
    failures = 0

    def check(ok: bool, label: str) -> None:
        nonlocal failures
        print(f"{'ok  ' if ok else 'FAIL'} {label}")
        failures += 0 if ok else 1

    password = published_password()
    check(len(password) >= 16 and " " not in password,
          "the sign-in page carries the one published password")
    check(create_dns.switch_back() == "./deploy/demo-host/create_dns.py --target 203.0.113.139",
          "the DNS record's way-back line names HQ's address")
    check(TARGET_IP == "203.0.113.87", "the baseline target is the tenant")
    def source_of(name: str) -> Path:
        if name == "session-receipt.json":
            return RECEIPT_SOURCE
        if name == "demo-tenant-site.caddy":
            return REPO / "deploy/caddy" / name
        if name == "verify.html":
            return RELEASE_PAGE_SOURCE
        if name == "start-here.md":
            return HERE / "seed/start-here-tenant.md"
        if name == "attest-refresh.sh":
            return REPO / "deploy/attest-refresh" / "refresh.sh"
        return HERE / name

    check(all(source_of(name).is_file() for name, _ in UPLOADS),
          "every uploaded file exists in the tree")
    check(all(ICON_SOURCES[name].is_file() for name, _pin, _type in ICONS)
          and all(hashlib.sha256(ICON_SOURCES[name].read_bytes()).hexdigest() == pin
                  for name, pin, _type in ICONS),
          "the three tab files are the committed web/ renditions, byte for byte")
    head = (HERE / "login.html").read_text(encoding="utf-8").split("</head>", 1)[0]
    check(CREDIT_COMMENT in head and "data:image/svg+xml" not in head
          and all(f'href="./{name}"' in head for name, _pin, _type in ICONS),
          "the sign-in page's head carries the three links and the credit comment")
    lede = VERIFY_LEDE.read_text(encoding="utf-8").strip()
    check(lede.startswith('<p class="lede">') and lede.endswith("</p>")
          and "\n" not in lede and "{{" not in lede,
          "the demo lede is one paragraph with no page slot")
    check(lede_problem("<main>%s</main>" % lede) == "",
          "the demo page passes the lede check")
    check(bool(lede_problem('<main><p class="lede">%s</p></main>' % CUSTOMER_SENTENCE)),
          "a page that opens with the customer row fails the lede check")
    command = (HERE / "verify-command.html").read_text(encoding="utf-8")
    check(command.count("<!--demo-check-->") == 1
          and command.count("<!--/demo-check-->") == 1
          and command.count("{{MEASUREMENT}}") == 1
          and "./odile-verify https://demo.odile.fyi --expect {{MEASUREMENT}}" in command
          and "<script" not in command and "<img" not in command,
          "the demo command is one marked section with one measurement slot")
    check(demo_check_problem("<main>%s</main>" % command.strip()) == ""
          and bool(demo_check_problem("<main>%s</main>" % lede)),
          "the served-page demo-check assertion passes the committed section and "
          "fails a page that lacks it")
    check(RELEASE_SETUP_SOURCE.is_file()
          and "release-setup.done" in RELEASE_SETUP_SOURCE.read_text(encoding="utf-8"),
          "the release setup script this deploy installs writes its done marker once")
    release_page = RELEASE_PAGE_SOURCE.read_text(encoding="utf-8")
    check(("verify.html", 0o644) in UPLOADS
          and CUSTOMER_SENTENCE in release_page
          and "before you sign in" in release_page
          and "before you set your sign-in" not in release_page,
          "the release page this deploy ships is ODL-929's cut: the grant row, "
          "the deck's sign-in sentence, not the retired one")
    check(apply_problem() == "",
          "this tree's apply carries the no-rewrite marker the deploy ships")
    with tempfile.TemporaryDirectory(prefix="odile-old-apply-") as tmp:
        planted = Path(tmp) / "apply_demo_public.py"
        planted.write_text(APPLY_SOURCE.read_text(encoding="utf-8").replace(APPLY_MARKER, ""),
                           encoding="utf-8")
        problem = apply_problem(planted)
        check(bool(problem) and "no-rewrite" in problem,
              "a planted apply without the marker is refused")

    spec = importlib.util.spec_from_file_location("apply_demo_public",
                                                  HERE / "apply_demo_public.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    check(module.RELEASE_PAGE.name == "verify.html"
          and module.RELEASE_PAGE.name in [name for name, _mode in UPLOADS],
          "the release page the deploy uploads is the file the apply reads")
    print("-- the tenant program's own self-test --")
    failures += module.self_test()
    return 0 if failures == 0 else 1


def icon_uploads(remote_dir: str = LIB_REMOTE) -> tuple[tuple[Path, str, str], ...]:
    """The three tab files this deploy puts beside the sign-in page.

    The source is the committed `web/` file — one rendition for the site and
    the demo — and the remote name is the path the page's links already use.
    """
    return tuple((ICON_SOURCES[name], f"{remote_dir}/{name}", "0644")
                 for name, _pin, _type in ICONS)


def cleanup_dry_run(console) -> None:
    """Remove the dry run's scratch files; `rm -f` on relative paths is the
    one form the Webapp's command guard allows (no `rm -rf`, no `find -delete`,
    and the uploaded files are root-owned)."""
    names = ("apply_demo_public.py", "demo-tenant-site.caddy", "login.html",
             "verify.html", "verify-lede.html", "verify-command.html",
             "start-here.md", "session-receipt.json", "attest-refresh.sh",
             "odile-attest-refresh.service", "odile-attest-refresh.timer",
             *(name for name, _pin, _type in ICONS))

    quoted = " ".join(f"odile-demo-public-dryrun/{name}" for name in names)
    console.run(f"cd /tmp && sudo rm -f {quoted}", check=False)
    console.run("cd /tmp && sudo rmdir odile-demo-public-dryrun", check=False)
    console.run("cd /tmp && sudo rm -f odile-demo-secret-dryrun", check=False)


def main(argv: list[str] | None = None) -> int:
    """Run the deploy, then delete every session its console opened."""
    try:
        return _run(argv)
    finally:
        cleanup_console_sessions()


def _run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Move demo.odile.fyi onto the tenant.")
    parser.add_argument("-n", "--dry-run", action="store_true", help="show the plan, change nothing")
    parser.add_argument("--self-test", action="store_true", help="run the pure-function tests")
    parser.add_argument("--target", default=TARGET_IP, help=f"the A record (default {TARGET_IP})")
    parser.add_argument("--base", default="",
                        help="force one console base (default: the demo host, then the machine's own name)")
    parser.add_argument("--access-user", default="judge", help="the account that opens the console")
    parser.add_argument("--access-password-file", default="",
                        help="the console account's password (first run: the tenant's own)")
    parser.add_argument("--no-login", action="store_true",
                        help="keep the live sign-in page (a newer page is installed by another card)")
    parser.add_argument("--judge-password-file", default="",
                        help="the published judge password (default: the sign-in page's "
                             "DEMO_PASSWORD)")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    # 0. the tree's own apply. It is the program that will run as root, and
    # this deploy ships whatever copy sits beside it — a branch tree's older
    # copy is the regression of 2026-11-17. Refused before DNS or any upload.
    problem = apply_problem()
    if problem:
        print(f"error: refusing this tree: {problem}", file=sys.stderr)
        return 2

    judge_password = (Path(args.judge_password_file).read_text(encoding="utf-8").strip()
                      if args.judge_password_file else published_password())
    access_password = (Path(args.access_password_file).read_text(encoding="utf-8").strip()
                       if args.access_password_file else judge_password)
    CLEANUP["password"] = judge_password

    # 1. DNS
    code = create_dns.ensure(args.target, args.dry_run)
    if code != 0:
        return code
    if not args.dry_run and not wait_for_dns(DEMO_HOST, args.target):
        print(f"error: {DEMO_HOST} never answered with {args.target}", file=sys.stderr)
        return 1

    # 2. the tenant's surface
    bases = (args.base,) if args.base else ()
    try:
        console = tenant_console.connect_any(args.access_user, access_password, bases)
    except tenant_console.ConsoleError as exc:
        print(f"error: cannot open the tenant console as {args.access_user!r}: {exc}",
              file=sys.stderr)
        return 1
    print(f"console      {console.base} as {args.access_user} "
          f"({(console.ready.get('params') or {}).get('type', 'ready')})")
    open_console_session(console)
    # A dry run must leave the tenant as it found it: the program and the
    # password go to a scratch directory that is removed before the return.
    # /tmp is the tenant's own directory: a dry run needs no root mkdir and
    # its cleanup is a relative `rm`, which the Webapp's command guard allows.
    remote_dir = "/tmp/odile-demo-public-dryrun" if args.dry_run else LIB_REMOTE
    password_remote = "/tmp/odile-demo-secret-dryrun" if args.dry_run else PASSWORD_REMOTE
    if args.dry_run:
        cleanup_dry_run(console)
        console.run(f"mkdir -p {remote_dir}")
    else:
        console.run(f"sudo mkdir -p {remote_dir}")
    for name, mode in UPLOADS:
        if name == "login.html" and args.no_login:
            print("keep         login.html (the live sign-in page is newer; --no-login)")
            continue
        if name == "start-here.md":
            source = HERE / "seed/start-here-tenant.md"
        elif name == "session-receipt.json":
            source = RECEIPT_SOURCE
        elif name == "demo-tenant-site.caddy":
            source = COMMITTED_BLOCK
        elif name == "verify.html":
            source = RELEASE_PAGE_SOURCE
        elif name == "attest-refresh.sh":
            source = REPO / "deploy/attest-refresh" / "refresh.sh"
        else:
            source = HERE / name
        console.put(str(source), f"{remote_dir}/{name}", mode=f"{mode:04o}")
        print(f"upload       {source.name} -> {remote_dir}/{name}")
    for source, remote, mode in icon_uploads(remote_dir):
        console.put(str(source), remote, mode=mode)
        print(f"upload       web/{source.name} -> {remote}")
    login_flag = " --no-login" if args.no_login else ""
    if args.judge_password_file:
        console.put(str(Path(args.judge_password_file)), password_remote, mode="0600")
    else:
        console.run(f"printf %s {judge_password} > /tmp/odile-judge-password")
        console.run(f"sudo install -m 0600 -o root -g root /tmp/odile-judge-password {password_remote}")
        console.run("cd /tmp && rm -f odile-judge-password")

    dry = console.run(
        f"sudo PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 {remote_dir}/apply_demo_public.py --dry-run"
        f" --password-file {password_remote}{login_flag}", check=False)
    print("-- tenant dry run --")
    print((dry.get("stdout") or "").strip() or "(no output)")
    if dry.get("code") != 0:
        print(f"error: the tenant dry run failed: {(dry.get('stderr') or '').strip()[-600:]}",
              file=sys.stderr)
        cleanup_dry_run(console)
        if not args.dry_run:
            console.run("cd /run && sudo rm -f odile-demo-secret")
        return 1
    if args.dry_run:
        ensure_release_setup(console, dry=True)
        cleanup_dry_run(console)
        print("dry run: DNS planned, the tenant's own dry run above; nothing applied")
        return 0
    # The apply restarts Caddy, which drops the very Webapp socket it runs
    # over: its output goes to a file on the machine, and the deploy reads it
    # back after reconnecting. The exit code rides the same file.
    log = "/tmp/odile-demo-apply.log"
    apply_command = (
        f"cd /tmp && rm -f odile-demo-apply.log && "
        f"sudo /usr/bin/python3 {LIB_REMOTE}/apply_demo_public.py --write --no-restart"
        f" --password-file {PASSWORD_REMOTE}{login_flag} > odile-demo-apply.log 2>&1; "
        f"echo \"rc=$?\" >> odile-demo-apply.log")
    try:
        console.run(apply_command, check=False)
    except tenant_console.ConsoleError as exc:
        print(f"apply        the socket closed under Caddy's restart ({exc})")
    time.sleep(4)
    console = tenant_console.connect_any(args.access_user, access_password, bases)
    open_console_session(console)
    output = console.run(f"cat {log}", check=False).get("stdout", "")
    print("-- tenant apply --")
    print(output.strip() or "(no output)")
    console.run("cd /run && sudo rm -f odile-demo-secret")
    if "rc=0" not in output:
        print("error: the tenant apply did not exit 0", file=sys.stderr)
        return 1

    # The apply's repairs are asserted from the machine's own report: the
    # installed block copy is the committed bytes, and the live block carries
    # no mutation matcher and no console residue behind it.
    print("-- tenant state --")
    if not tenant_assertions(console):
        print("error: the tenant's Caddy copies are not the committed block",
              file=sys.stderr)
        return 1

    # The release setup converges once, so the next boot cannot rewrite the
    # config this deploy just converged.
    print("-- release setup --")
    ensure_release_setup(console, dry=False)

    # 3. restart the gateway the new settings need, then reconnect as judge
    print("-- restart --")
    try:
        console.run(f"sudo {LIB_REMOTE}/restart-agent", check=False)
    except tenant_console.ConsoleError as exc:
        print(f"restart      {exc} (the restart closes the socket it runs over)")
    # The gateway takes a few seconds to answer again; retry rather than race it.
    for attempt in range(15):
        time.sleep(4)
        try:
            console = tenant_console.connect_any("judge", judge_password)
            print(f"console      reconnected as judge on {console.base} "
                  f"(after {4 * (attempt + 1)}s)")
            open_console_session(console)
            break
        except tenant_console.ConsoleError as exc:
            if attempt == 14:
                print(f"console      reconnected as judge: {exc}", file=sys.stderr)
                return 1

    # 4. the gate over HTTPS, from HQ
    print("-- verify --")
    for _ in range(24):
        ok, lines = verify_public(judge_password)
        if ok:
            break
        time.sleep(5)
    for line in lines:
        print(line)
    if not ok:
        print("error: the demo did not pass its own definition of done", file=sys.stderr)
        return 1

    # 5. the fallback
    print(f"fallback     {create_dns.switch_back()}")
    print("demo public: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
