#!/usr/bin/env python3
"""Drive a Blackbox through Servury's panel, without a browser.

The reseller API has a boot-image verb now (`?action=boot-image`, probed 12
November), and `tools/servury_cycle.py` uses it when the account holding a
machine has a reseller key. The panel is the route for an account with a session
but no key — `<servury-account:dev>`, which owns `<server:ukitest>`. Its login is
not an email form: it is one **32-character credential** field
(`input#credential`) plus a CSRF token, with a 30-day "remember me". That makes
the panel drivable with a cookie jar — no headless browser, no brittle clicking.

    ./tools/servury_panel.py --login          # once: reads the credential file, saves cookies
    ./tools/servury_panel.py --show --server <server:ukitest>
    ./tools/servury_panel.py --set --server <server:ukitest> \\
        --url https://odile.fyi/releases/uki/9f1c….efi --sha256 9f1c… [--apply --confirm]
    ./tools/servury_panel.py --set-boot-image --server <server:ukitest> \\
        --url … --sha256 … [--apply --confirm]
    ./tools/servury_panel.py --power --server <server:ukitest> --action restart
    ./tools/servury_panel.py --console --server <server:ukitest> --action on|off|read

`<server:ukitest>` is the one id in `/root/.odile/servury_uki_server_id` (one id
per line; the file is untracked, so a clone has no live target).

The three writes on the manage page are separate forms, and this tool keeps them
separate: `--set` writes the **disk**, `--set-boot-image` writes the **kernel**
(the UKI; replacing one detaches the old image first, because the attach form
only renders when none is set), `--power` presses one of the four buttons, and
`--console` arms the serial log or reads it back. `--apply` alone is a dry run;
a real disk write also needs `--confirm`.

The credential lives at `/root/.odile/servury_panel_credential` (0600, one line)
and the session at `/root/.odile/servury_panel.cookie` (0600). The panel rotates
`PHPSESSID` when it re-authenticates from `remember_token`, so the tool reads the
pasted header **once** into its cookie jar (`/root/.odile/servury_panel.cookies`)
and lets the jar carry the rotated session; a POST that presents the previous id
is answered `Invalid request. Reload the page and try again.` `--dump` prints
every form and field on a server's manage page — inputs, selects and textareas —
which is how the boot-image field names are confirmed the first time.

Exit: 0 done; 1 the panel refused or the field was not found; 2 usage/login.
"""

from __future__ import annotations

import argparse
import datetime
import http.cookiejar
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape as html_unescape
from pathlib import Path

BASE = "https://servury.com"
LOGIN = f"{BASE}/login/"
CREDENTIAL_FILE = Path("/root/.odile/servury_panel_credential")
COOKIE_FILE = Path("/root/.odile/servury_panel.cookies")
UA = "Mozilla/5.0 (X11; Linux x86_64) odile-panel/1"
CSRF = re.compile(r'name="csrf_token"\s+value="([^"]+)"', re.I)
# The credential can terminate every machine on the account, including the one
# running this tool. So the tool is fenced: one server, one form, no destructive
# verb, and a dry run unless --apply is given. The one id is not in the tree;
# it is read from the untracked file below, and a missing file allows nothing.
SERVER_ID_FILE = Path("/root/.odile/servury_uki_server_id")
FORBIDDEN_PATH = re.compile(r"(delete|terminate|rebuild|reinstall|power|destroy|cancel|upgrade|resize)", re.I)
# The disk-image form on /manage/<id>/admin/: replace the disk with a published
# image, verify it, optionally start the machine. Nothing else may be posted here.
POST_FIELDS = frozenset({
    "csrf_token", "action", "disk_url", "disk_sha256", "disk_format", "disk_start", "disk_confirm",
    "image_kernel_url", "image_kernel_sha256", "image_initrd_url", "image_initrd_sha256",
    "image_cmdline", "hostname", "enabled",
})
# Ordering is the one other write this tool may make: a plan, a location, 31 days,
# a coupon, and funds. No payment method other than the account balance.
ORDER_FIELDS = frozenset({
    "csrf_token", "plan", "location", "os", "days", "coupon", "payment_method", "hostname",
    "snp_requested", "fde_requested", "measured_requested", "auto_renew", "ssh_key",
    "calculate", "submit_order", "preset_days",
})
AUDIT = Path("/root/.odile/servury-panel.log")
# Every Servury purchase goes through the referral route: the discount stacks with
# the account's referral tier (10% and better), and an order placed without it
# silently forfeits that. Only a demonstrably cheaper route may beat it.
PURCHASE_URL = "https://servury.com/ref/web"
DEFAULT_COUPON = "WEB"
FORM_ACTION = re.compile(r"<form[^>]*action=\"([^\"]*)\"[^>]*>", re.I)
FIELD = re.compile(r"<(input|select|textarea)\b[^>]*>", re.I)
NAME = re.compile(r"name=[\"']([^\"']+)[\"']", re.I)


def parse_csrf(html: str) -> str | None:
    match = CSRF.search(html or "")
    return match.group(1) if match else None


def field_names(html: str) -> list[tuple[str, str]]:
    """(tag, name) for every named form control: input, select and textarea.

    The manage form's disk format is a `<select>` and the boot command line is a
    `<textarea>`; a dump that lists only `<input>` hides exactly the fields the
    tool posts.
    """
    fields: list[tuple[str, str]] = []
    for tag in FIELD.finditer(html or ""):
        name = NAME.search(tag.group(0))
        if name:
            fields.append((tag.group(1).lower(), name.group(1)))
    return fields


def input_names(html: str) -> list[str]:
    return [name for kind, name in field_names(html) if kind == "input"]


def datetime_stamp() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%FT%TZ")


def whoami(opener) -> tuple[str, str]:
    """(User ID, balance) for the account this session belongs to."""
    html = get(opener, BASE + "/settings/")
    html = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", html, flags=re.S)
    lines = [l.strip() for l in re.sub(r"<[^>]+>", "\n", html).split("\n") if l.strip()]
    user = next((l for l in lines if re.fullmatch(r"[a-z0-9]{8}", l)), "?")
    balance = "?"
    if "Balance" in lines:
        tail = lines[lines.index("Balance"):][1:4]
        digits = [t for t in tail if re.fullmatch(r"\$?[0-9.]+", t)]
        balance = "".join(digits) or "?"
    return user, balance


def opener_for(cookie_path: Path) -> tuple[urllib.request.OpenerDirector, http.cookiejar.MozillaCookieJar]:
    global _JAR
    jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
    pairs = header_pairs()
    if pairs:
        # The pasted browser session is authoritative. The panel rotates
        # PHPSESSID on the requests that use it, so start from a clean jar each
        # run: a saved jar's older id, sent alongside this one, is answered
        # 504 at the gateway or "Invalid request" on the POST.
        for name, value in pairs:
            jar.set_cookie(http.cookiejar.Cookie(
                0, name, value, None, False, "servury.com", True, False, "/", True,
                False, None, False, None, None, {}))
        _JAR = None
    else:
        if cookie_path.is_file():
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:  # noqa: BLE001 — a corrupt jar is just a re-login
                pass
        _JAR = jar
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", UA)]
    return opener, jar


def refuse_path(path: str) -> str | None:
    """The reason a path is too dangerous to POST to, or None when it is allowed."""
    match = FORBIDDEN_PATH.search(path or "")
    if match:
        return f"path names a destructive verb: {match.group(0)!r}"
    return None


DASHBOARD = f"{BASE}/dashboard/"
# One `/manage/<id>/` link per machine **the session's account owns**. The
# dashboard is the only page that answers "whose machine is this": the manage
# page itself renders identically for an id on another account, so ownership is
# read from here and a foreign id is a refusal.
MANAGE_LINK = re.compile(r"/manage/([0-9a-f]{8})/")
_ACCOUNT: str | None = None


def owned_servers(opener) -> frozenset[str]:
    """The ids this session's account owns, from the dashboard's manage links."""
    return frozenset(MANAGE_LINK.findall(get(opener, DASHBOARD)))


def account_of(opener) -> str:
    """The account this session belongs to, read once per run.

    Readings are cached because every write needs it and each one is a page
    fetch; `?` (the settings page did not name an id) is **not** cached, so a
    blank read leaves the account unknown and the write refuses.
    """
    global _ACCOUNT
    if _ACCOUNT is None:
        user, _ = whoami(opener)
        if user != "?":
            _ACCOUNT = user
    return _ACCOUNT or "?"


def require_owned(opener, server: str) -> int | None:
    """`None` when `server` is this account's, else 2 after saying why.

    Two logins exist on the vendor: the reseller account that owns HQ and the
    old demo tenant, and the account the tenant now lives on. A POST naming a
    machine the session does not own is the one mistake that reaches another
    account, so it is refused before any form is posted.
    """
    account = account_of(opener)
    if account == "?":
        print("panel: the session's account could not be read — refusing to write",
              file=sys.stderr)
        return 2
    owned = owned_servers(opener)
    if server not in owned:
        print(
            f"panel: {server} is not a machine on account {account} "
            f"(the dashboard lists {sorted(owned) or 'none'}) — refusing",
            file=sys.stderr,
        )
        return 2
    return None


def allowed_servers() -> frozenset[str]:
    """The server ids this tool may touch, from the untracked file.

    One id per line. A missing or empty file is an empty allow-list, not a
    default: a fresh clone must not silently point at a live machine.
    """
    try:
        text = SERVER_ID_FILE.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    return frozenset(line.strip() for line in text.splitlines() if line.strip())


def server_allowed(server: str, extra: frozenset[str] = frozenset()) -> bool:
    return server in (allowed_servers() | extra)


def audit(line: str) -> None:
    try:
        with AUDIT.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip() + "\n")
    except OSError:
        pass


def audit_line(opener, message: str) -> str:
    """The line every write leaves: UTC stamp, **the account**, then the write.

    A `server=<id>` alone stopped being enough once the tenant moved onto the
    account the team owns: two logins hold machines, and the log has to answer
    "which account did this land in" without a second command.
    """
    return f"{datetime_stamp()} account={account_of(opener)} {message}"


COOKIE_HEADER = Path("/root/.odile/servury_panel.cookie")   # "name=value; name=value", 0600
_JAR: http.cookiejar.MozillaCookieJar | None = None


def header_pairs(text: str | None = None) -> list[tuple[str, str]]:
    """The name=value pairs out of a browser-exported Cookie header."""
    if text is None:
        try:
            text = COOKIE_HEADER.read_text(encoding="utf-8").strip()
        except OSError:
            return []
    pairs = []
    for part in (text or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name and value:
            pairs.append((name, value))
    return pairs


def cookie_header() -> str | None:
    """A browser-exported session, pasted verbatim. Beats the jar: login is captcha-gated."""
    try:
        text = COOKIE_HEADER.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def request_for(url: str, data: bytes | None = None) -> urllib.request.Request:
    # The cookie jar, not a raw header, carries the session: the panel rotates
    # PHPSESSID when it re-authenticates from remember_token, and a POST that
    # presents the previous id is answered "Invalid request. Reload the page".
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) odile-panel/1"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    return urllib.request.Request(url, data=data, headers=headers)


def save_session(jar) -> None:
    try:
        jar.save(ignore_discard=True, ignore_expires=True)
        if COOKIE_FILE.is_file():
            os.chmod(COOKIE_FILE, 0o600)
    except OSError:
        pass


def get(opener, url: str) -> str:
    with opener.open(request_for(url), timeout=40) as response:
        body = response.read().decode("utf-8", "replace")
    if _JAR is not None:
        save_session(_JAR)
    return body


def post(opener, url: str, fields: dict[str, str]) -> str:
    why = refuse_path(urllib.parse.urlparse(url).path)
    if why:
        raise SystemExit(f"panel: refusing to POST to {url}: {why}")
    allowed = ORDER_FIELDS if "/order/" in urllib.parse.urlparse(url).path else POST_FIELDS
    unknown = set(fields) - allowed
    if unknown:
        raise SystemExit(f"panel: refusing unknown form field(s) {sorted(unknown)}")
    account = f" account={_ACCOUNT}" if _ACCOUNT else ""
    audit(f"{datetime_stamp()} POST {url} fields={sorted(fields)}{account}")
    body = urllib.parse.urlencode(fields).encode()
    with opener.open(request_for(url, data=body), timeout=40) as response:
        text = response.read().decode("utf-8", "replace")
    if _JAR is not None:
        save_session(_JAR)
    return text


NOTICE = re.compile(
    r'<(?:p|div)[^>]*class="[^"]*(?:alert|notice|flash)[^"]*"[^>]*>(.*?)</(?:p|div)>',
    re.S | re.I,
)


def notice(html: str) -> str:
    """The page's own notice text, if it has one (the whole page has Stripe JS)."""
    match = NOTICE.search(html or "")
    if not match:
        return ""
    return " ".join(re.sub(r"<[^>]+>", " ", match.group(1)).split())


def refused_note(html: str) -> str:
    """The notice text when it reads like a refusal, else the empty string."""
    text = notice(html)
    for word in ("failed", "error", "invalid", "refused", "not allowed", "required"):
        if word in text.lower():
            return text
    return ""


def login(opener, jar) -> int:
    credential = CREDENTIAL_FILE.read_text(encoding="utf-8").strip()
    if len(credential) != 32:
        print(
            f"panel: {CREDENTIAL_FILE} must hold the 32-character credential (it holds {len(credential)})",
            file=sys.stderr,
        )
        return 2
    html = get(opener, LOGIN)
    token = parse_csrf(html)
    if not token:
        # A valid session redirects /login/ to the dashboard, which has no form.
        if "dashboard" in html.lower() or "logout" in html.lower():
            print("panel: already logged in (the saved session is still valid)")
            user, balance = whoami(opener)
            print(f"panel: user {user} balance {balance}")
            return 0
        print("panel: no csrf_token on the login page — the form changed", file=sys.stderr)
        return 1
    landed = post(opener, LOGIN, {"csrf_token": token, "credential": credential, "remember_me": "on"})
    if "logout" not in landed.lower() and "csrf_token" in landed.lower() and "credential" in landed.lower():
        print("panel: login refused (still on the form)", file=sys.stderr)
        return 1
    jar.save(ignore_discard=True, ignore_expires=True)
    os.chmod(COOKIE_FILE, 0o600)
    print(f"panel: logged in, session saved to {COOKIE_FILE}")
    return 0


def admin_url(server: str) -> str:
    return f"{BASE}/manage/{server}/admin/"


def dump(opener, server: str) -> int:
    url = admin_url(server) if server else f"{BASE}/servers/"
    html = get(opener, url)
    print(f"page {url} ({len(html)} bytes)")
    for action in FORM_ACTION.findall(html):
        print(f"  form action={action!r}")
    fields = field_names(html)
    for kind, name in fields:
        print(f"  {kind:<8} {name}")
    if not fields:
        print("  (no form fields — check the URL, or the login did not stick)", file=sys.stderr)
        return 1
    return 0


def write_disk(opener, server: str, url: str, fmt: str, apply: bool, sha256: str = "") -> int:
    """Point the machine's disk at a published image. Two flags are needed, on purpose."""
    refused = require_owned(opener, server)
    if refused is not None:
        return refused
    page = admin_url(server)
    html = get(opener, page)
    token = parse_csrf(html)
    if not token:
        print("panel: no csrf_token on the admin page — the session may have expired", file=sys.stderr)
        return 1
    fields = {
        "csrf_token": token,
        "action": "disk_write",
        "disk_url": url,
        "disk_format": fmt,
        "disk_start": "1",
        "disk_confirm": "1",
    }
    if sha256:
        fields["disk_sha256"] = sha256
    audit(audit_line(opener, f"DISK_WRITE server={server} url={url} format={fmt} apply={apply}"))
    print(f"panel: {'wrote' if apply else 'would write'} {server} ← {url} ({fmt})")
    if not apply:
        print("panel: dry run — pass --apply and --confirm to submit")
        return 0
    result = post(opener, page, fields)
    refusal = refused_note(result)
    if refusal:
        print(f"panel: disk write refused: {refusal}", file=sys.stderr)
        return 1
    submitted = notice(result)
    print(f"panel: disk write submitted for {server}" + (f" — {submitted}" if submitted else ""))
    return 0


def attached_image(html: str) -> bool:
    """True when the Boot image section is showing a kernel that is in place."""
    return bool(re.search(r"<dt>Since</dt>", html or ""))


def write_boot_image(
    opener, server: str, url: str, sha256: str, cmdline: str, apply: bool,
    *, wait: float = 300.0, interval: float = 5.0,
) -> int:
    """Point the machine's kernel at a published UKI.

    The panel's boot-image form takes a kernel URL and its SHA-256; a UKI carries
    its own initramfs and command line, so both of those stay empty unless the
    caller passes a line deliberately (the panel passes it verbatim).

    The form only renders when **no** image is attached — a machine that already
    boots a custom kernel shows a "Back to the Servury release" button instead —
    so replacing an image is detach, attach, then wait for the node to report the
    download ready. Rushing the power action before that boots the old image.
    """
    refused = require_owned(opener, server)
    if refused is not None:
        return refused
    page = admin_url(server)
    html = get(opener, page)
    token = parse_csrf(html)
    if not token:
        print("panel: no csrf_token on the admin page — the session may have expired", file=sys.stderr)
        return 1
    if sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        print("panel: --sha256 must be 64 hex characters", file=sys.stderr)
        return 2
    fields = {
        "csrf_token": token,
        "action": "image_attach",
        "image_kernel_url": url,
        "image_kernel_sha256": sha256,
        "image_initrd_url": "",
        "image_initrd_sha256": "",
        "image_cmdline": cmdline,
    }
    audit(audit_line(
        opener,
        f"BOOT_IMAGE server={server} url={url} "
        f"sha256={sha256[:16]} cmdline={'yes' if cmdline else 'empty'} apply={apply}",
    ))
    print(f"panel: {'set' if apply else 'would set'} {server} boot image ← {url}")
    print(f"panel:   sha256 {sha256 or '(not given)'}")
    print(f"panel:   cmdline {'passed verbatim' if cmdline else 'empty (the UKI carries its own)'}")
    if not apply:
        if attached_image(html):
            print("panel:   an image is attached; a real write detaches it first, then attaches this pair")
        print("panel: dry run — pass --apply and --confirm to submit")
        return 0
    if attached_image(html):
        print(f"panel: {server} already holds a boot image; detaching it first "
              "(the attach form only renders when none is set)")
        result = post(opener, page, {"csrf_token": token, "action": "image_detach"})
        refusal = refused_note(result)
        if refusal:
            print(f"panel: boot-image detach refused: {refusal}", file=sys.stderr)
            return 1
        html = result
        token = parse_csrf(html) or parse_csrf(get(opener, page))
        if not token:
            print("panel: no csrf_token after the detach — the page changed", file=sys.stderr)
            return 1
        fields["csrf_token"] = token
    if "image_kernel_url" not in html:
        print("panel: the boot-image form is not on the page — the panel changed", file=sys.stderr)
        return 1
    result = post(opener, page, fields)
    refusal = refused_note(result)
    if refusal:
        print(f"panel: boot-image write refused: {refusal}", file=sys.stderr)
        return 1
    started = time.time()
    while True:
        if sha256 in result and attached_image(result):
            print(f"panel: boot image set — {page} now shows {sha256[:16]}…")
            return 0
        if time.time() - started >= wait:
            break
        time.sleep(max(interval, 1))
        result = get(opener, page)
        refusal = refused_note(result)
        if refusal:
            print(f"panel: boot-image download refused: {refusal}", file=sys.stderr)
            return 1
        print(f"panel:   downloading… ({round(time.time() - started)}s)")
    print(f"panel: submitted, but the node did not report the image ready in {int(wait)}s",
          file=sys.stderr)
    return 1


def power(opener, server: str, action: str, *, dry_run: bool = False) -> int:
    """Press one of the manage page's power buttons (start/stop/restart/forcestop)."""
    refused = require_owned(opener, server)
    if refused is not None:
        return refused
    page = admin_url(server)
    html = get(opener, page)
    token = parse_csrf(html)
    if not token:
        print("panel: no csrf_token on the admin page — the session may have expired", file=sys.stderr)
        return 1
    audit(audit_line(opener, f"POWER server={server} action={action} apply={not dry_run}"))
    print(f"panel: {'would press' if dry_run else 'pressing'} {action} on {server}")
    if dry_run:
        return 0
    try:
        result = post(opener, page, {"csrf_token": token, "action": action})
    except urllib.error.HTTPError as error:
        # The gateway gives up at 30 s with a 504 while the node takes the
        # action anyway (measured on the restart that booted the 21:50 image);
        # treating it as a failure aborted the cycle before the transcript.
        if error.code == 504:
            print(f"panel: power {action} sent; the gateway timed out at 30s (504), the node takes it")
            return 0
        raise
    refusal = refused_note(result)
    if refusal:
        print(f"panel: power {action} refused: {refusal}", file=sys.stderr)
        return 1
    print(f"panel: power {action} submitted for {server}")
    return 0


LOG_BLOCK = re.compile(r"<pre[^>]*>(.*?)</pre>", re.S | re.I)
LOG_AREA = re.compile(r"<textarea[^>]*\bname=[\"']log[\"'][^>]*>(.*?)</textarea>", re.S | re.I)


def console_text(html: str) -> str:
    """The serial log inside a console page, as text (empty when the log is off)."""
    match = LOG_BLOCK.search(html or "") or LOG_AREA.search(html or "")
    if not match:
        return ""
    body = re.sub(r"<br\s*/?>", "\n", match.group(1), flags=re.I)
    body = re.sub(r"<[^>]+>", "", body)
    return html_unescape(body)


def console(opener, server: str, action: str, *, dry_run: bool = False) -> int:
    """Arm the serial log, disarm it, or print what it holds.

    The panel records nothing by default; `on` submits the console-log form and
    the recording starts at the next restart. `read` is a plain GET, so it can
    poll a boot without changing anything.
    """
    url = f"{BASE}/manage/{server}/console/"
    if action == "read":
        print(console_text(get(opener, url)), end="")
        return 0
    refused = require_owned(opener, server)
    if refused is not None:
        return refused
    html = get(opener, url)
    token = parse_csrf(html)
    if not token:
        print("panel: no csrf_token on the console page — the session may have expired", file=sys.stderr)
        return 1
    enabled = "1" if action == "on" else "0"
    audit(audit_line(opener, f"CONSOLE server={server} enabled={enabled} apply={not dry_run}"))
    print(f"panel: {'would turn' if dry_run else 'turning'} the console log {action} for {server}")
    if dry_run:
        return 0
    result = post(opener, url, {"csrf_token": token, "action": "console_log", "enabled": enabled})
    refusal = refused_note(result)
    if refusal:
        print(f"panel: console log {action} refused: {refusal}", file=sys.stderr)
        return 1
    want = "on" if action == "on" else "off"
    # The page states the new state in words next to "Console log".
    state = re.sub(r"<[^>]+>", " ", result)
    if "Console log" in state and not re.search(rf"console log\b[^.]{{0,80}}\b{want}\b", state, re.I):
        print(f"panel: console log {action} submitted, but the page does not say {want!r}", file=sys.stderr)
        return 1
    print(f"panel: console log {action} — it applies at the next restart")
    return 0


def purchase_route(opener) -> str:
    """Visit the referral route, which is what actually attaches the discount.

    Measured 2026-11-18: an order posted with `coupon=WEB` and no route visit
    was charged list price ($9.99), while the 12 November order on the same
    account — placed after this page — was $8.99. The coupon field alone is not
    the route, so the order path takes it before it reads the form.
    """
    get(opener, PURCHASE_URL)
    return PURCHASE_URL


def order(opener, plan: str, location: str, os_name: str, days: str, hostname: str,
          coupon: str, apply: bool, ssh_key_file: str) -> int:
    """Buy one machine through the referral route. Dry run unless --apply."""
    route = purchase_route(opener)
    print(f"panel: referral route {route}")
    page = f"{BASE}/order/?location={location}&plan={plan}&os={os_name}"
    html = get(opener, page)
    token = parse_csrf(html)
    if not token:
        print("panel: no csrf_token on the order page — the session may have expired", file=sys.stderr)
        return 1
    fields = {
        "csrf_token": token, "plan": plan, "location": location, "os": os_name,
        "days": days, "coupon": coupon, "payment_method": "account_funds",
        "hostname": hostname, "snp_requested": "1", "fde_requested": "1",
        "measured_requested": "1", "auto_renew": "1", "submit_order": "1",
    }
    if ssh_key_file:
        fields["ssh_key"] = Path(ssh_key_file).read_text(encoding="utf-8").strip()
    audit(audit_line(
        opener,
        f"ORDER plan={plan} loc={location} days={days} coupon={coupon} "
        f"host={hostname} apply={apply}",
    ))
    print(f"panel: {'ordering' if apply else 'would order'} plan {plan} in {location} for {days} days,"
          f" coupon {coupon} — {PURCHASE_URL}")
    if not apply:
        print("panel: dry run — pass --apply to buy")
        return 0
    landed = post(opener, page, fields)
    if "You must set either" in landed:
        print("panel: the order form needs a root password or a public key — pass --ssh-key-file", file=sys.stderr)
        return 1
    print(f"panel: order submitted; check /servers/ for the new machine (coupon {coupon})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drive Servury's panel (boot image, disk, power, console) without a browser.")
    parser.add_argument("--login", action="store_true", help="log in once and save the session cookies")
    parser.add_argument("--dump", action="store_true", help="print the forms and their fields (input, select, textarea)")
    parser.add_argument("--show", action="store_true", help="print the current boot image, if present")
    parser.add_argument("--set", action="store_true", help="write the disk image from --url and --sha256")
    parser.add_argument("--set-boot-image", action="store_true", help="set the kernel (UKI) from --url and --sha256")
    parser.add_argument("--power", action="store_true", help="press a power button: --action start|stop|restart|forcestop")
    parser.add_argument("--console", action="store_true", help="serial log: --action on|off|read")
    parser.add_argument("--action", default="restart", help="the power/console action (default: restart)")
    parser.add_argument("--cmdline", default="", help="with --set-boot-image: a command line passed verbatim (default empty)")
    parser.add_argument("--server", default="", help="8-character server id")
    parser.add_argument("--url", default="")
    parser.add_argument("--sha256", default="")
    parser.add_argument("--order", action="store_true", help="buy one machine through the referral route")
    parser.add_argument("--plan", default="108")
    parser.add_argument("--location", default="2")
    parser.add_argument("--os", dest="os_name", default="debian12")
    parser.add_argument("--days", default="31")
    parser.add_argument("--hostname", default="odl-dev")
    parser.add_argument("--coupon", default=DEFAULT_COUPON, help=f"default {DEFAULT_COUPON}: the referral stacks with the tier")
    parser.add_argument("--ssh-key-file", default="", help="public key pasted into the order form (required for measured/FDE)")
    parser.add_argument("--whoami", action="store_true", help="print the account this session belongs to")
    parser.add_argument("--expect-user", default=None, help="refuse to write unless the session's User ID matches")
    parser.add_argument("--apply", action="store_true", help="with --set/--set-boot-image: actually submit")
    parser.add_argument("--confirm", action="store_true", help="with --set: the disk's contents will be replaced")
    parser.add_argument("--dry-run", action="store_true", help="with --power/--console: print the action, change nothing")
    parser.add_argument("--format", default="auto", choices=("auto", "qcow2", "raw"), help="image format")
    parser.add_argument("--allow-server", action="append", default=[], help="extra server id this tool may touch")
    args = parser.parse_args(argv)

    fresh = bool(args.login or args.whoami)
    if args.login:
        COOKIE_FILE.unlink(missing_ok=True)   # a stale jar may belong to another account
    opener, jar = opener_for(COOKIE_FILE)
    extra = frozenset(args.allow_server or ())
    if args.whoami:
        user, balance = whoami(opener)
        print(f"panel: user {user} balance {balance}")
        return 0
    if args.expect_user:
        user, _ = whoami(opener)
        if user != args.expect_user:
            print(f"panel: session is user {user}, not {args.expect_user} — refusing", file=sys.stderr)
            return 2
    if args.server and not server_allowed(args.server, extra):
        print(
            f"panel: {args.server} is not in the allow-list {sorted(allowed_servers())};"
            " pass --allow-server to add it deliberately",
            file=sys.stderr,
        )
        return 2
    if args.login:
        return login(opener, jar)
    if args.dump:
        return dump(opener, args.server)
    if args.show:
        return dump(opener, args.server)
    if args.order:
        return order(opener, args.plan, args.location, args.os_name, args.days, args.hostname,
                     args.coupon, args.apply, args.ssh_key_file)
    if args.set:
        if not (args.server and args.url):
            print("panel: --set needs --server and --url", file=sys.stderr)
            return 2
        if args.apply and not args.confirm:
            print("panel: --apply also needs --confirm (the disk's contents are replaced)", file=sys.stderr)
            return 2
        return write_disk(opener, args.server, args.url, args.format, args.apply, args.sha256)
    if args.set_boot_image:
        if not (args.server and args.url and args.sha256):
            print("panel: --set-boot-image needs --server, --url and --sha256", file=sys.stderr)
            return 2
        return write_boot_image(
            opener, args.server, args.url, args.sha256, args.cmdline,
            apply=args.apply and args.confirm,
        )
    if args.power:
        if not args.server:
            print("panel: --power needs --server", file=sys.stderr)
            return 2
        if args.action not in ("start", "stop", "restart", "forcestop"):
            print("panel: --power --action must be start, stop, restart or forcestop", file=sys.stderr)
            return 2
        return power(opener, args.server, args.action, dry_run=args.dry_run)
    if args.console:
        if not args.server:
            print("panel: --console needs --server", file=sys.stderr)
            return 2
        if args.action not in ("on", "off", "read"):
            print("panel: --console --action must be on, off or read", file=sys.stderr)
            return 2
        return console(opener, args.server, args.action, dry_run=args.dry_run)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
