#!/usr/bin/env python3
"""Drive a Blackbox through Servury's panel, without a browser.

The reseller API has a boot-image verb now (`?action=boot-image`, probed 12
November), and `tools/servury_cycle.py` uses it when the account holding a
machine has a reseller key. The panel is the route for an account with a session
but no key — `lkbxu6i9`, which owns the bench `168020b4` and the tenant box. Its login is
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
    ./tools/servury_panel.py --order --plan 108 --ssh-key-file key.pub --apply
    ./tools/servury_panel.py --reinstall --server <server:ukitest> --os debian13 \\
        --want-snp --want-fde --want-measured --ssh-key-file key.pub [--apply --confirm]
    ./tools/servury_panel.py --recycle --server <server:ukitest> \\
        --os debian13 --want-snp --want-fde --want-measured --ssh-key-file key.pub \\
        --boot-url … --boot-sha256 … [--cmdline-file line.txt] \\
        --disk-url … --disk-sha256 … --format raw [--apply --confirm]

A freed machine comes back as **three writes, not one call**
(`docs/product/SPIKE-servury-reinstall.md` §6): the reinstall, the boot image,
then the disk image. `--recycle` is that composite, in that order; the disk
write starts the machine itself, so the first boot is the operator's step
(`docs/runbooks/recycle.md`).

The reinstall is the Danger Zone form, and the credential can erase every
machine on the account, so the verb is fenced harder than the image writes:

* the allow-list in `SERVER_ID_FILE` must name **exactly one** id and it must
  be `--server` (exact match); `--allow-server` is refused for this verb;
* `ERASE` is typed by the tool, never an argument, and the tool refuses to POST
  unless the page still carries "Erase and reinstall";
* `os`, `want_snp`, `want_fde` and `want_measured` are required with **no
  defaults** — the vendor's own rule is "what you do not send is off";
* `ssh_key` comes from `--ssh-key-file`, never argv (argv is world-readable);
* the audit line is written **before** the POST and carries the pre-write `os`,
  the three flags, the key's comment and the boot/disk pair about to be lost;
* `--apply` alone is a dry run; the destructive write needs `--confirm`.

The path guard is not enough: the form posts to `/manage/<id>/danger/`, whose
path names no forbidden verb. What stops it is the **field** `action=reinstall`,
permitted only by `--reinstall`/`--recycle`, and `action=delete` (the destroy
form next door) is never permitted at all.

The panel's own page for the reinstall says it plainly: everything on the disk
is destroyed, the address and rDNS stay, and the boot-image and disk-image
fields come back cleared.

Buying is the one thing that does **not** use the panel any more: `--order` POSTs
`?action=create` with the account's API key, because the panel order form charges
list price and the key charges the partner price (5% off orders, renewals and
upgrades). The panel is still the only route for the disk image, the boot image,
the console and power. The affiliate route and coupon codes were retired in
November 2026; `--coupon` is gone.

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
import json
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
# The Danger Zone form on /manage/<id>/danger/ (recorded 2026-11-19,
# spike/servury-reinstall/evidence/danger-dump.txt). Its reinstall and destroy
# buttons are one word apart, so the fence for it is the **field** `action`,
# in `refuse_action` below, not the path: `/danger/` names no forbidden verb.
REINSTALL_FIELDS = frozenset({
    "os", "want_snp", "want_fde", "want_measured", "new_password",
    "new_password_confirm", "ssh_key", "confirm",
})
# The configure forms this tool may post: the disk-image and boot-image forms
# on /manage/<id>/admin/, the console switch, the hostname, power and the
# Danger Zone's reinstall. The order form is not one of them any more — it
# charges list price, so buying moved to the API (`order()`), and banning
# /order/ here makes a retail order impossible by tool.
POST_FIELDS = frozenset({
    "csrf_token", "action", "disk_url", "disk_sha256", "disk_format", "disk_start", "disk_confirm",
    "image_kernel_url", "image_kernel_sha256", "image_initrd_url", "image_initrd_sha256",
    "image_cmdline", "hostname", "enabled",
}) | REINSTALL_FIELDS
# `action=delete` is the destroy form next door (literal `DELETE`); it is
# never permitted, by any verb.
NEVER_ACTIONS = frozenset({"delete", "destroy", "terminate", "cancel"})
REINSTALL_ACTION = "reinstall"
# The page's own label for the button. The tool refuses to post at a page that
# no longer carries it, so a vendor rename fails loudly instead of erasing.
REINSTALL_CONTROL = "Erase and reinstall"
# Typed by the tool, never supplied by a caller.
ERASE = "ERASE"
AUDIT = Path("/root/.odile/servury-panel.log")
# Every Servury purchase goes through the API with the account's key: 5% off
# list on orders, renewals and upgrades, applied automatically. The affiliate
# route was retired in November 2026 — /ref/web 301s to / with no cookie and
# `WEB` answers "Invalid coupon code" — and the panel order form is list price
# even on an account that holds a key, so the panel is no longer an order path
# (docs/ops/evidence/2026-11-19-servury-purchase-route/EVIDENCE.md).
ORDER_ROUTE = "https://servury.com/api/reseller.php?action=create"
ORDER_KEY_FILE = Path("/root/.odile/servury_reseller_key_lkbxu6i9")
FORM_ACTION = re.compile(r"<form[^>]*action=\"([^\"]*)\"[^>]*>", re.I)
FIELD = re.compile(r"<(input|select|textarea|button)\b[^>]*>", re.I)
NAME = re.compile(r"name=[\"']([^\"']+)[\"']", re.I)


def parse_csrf(html: str) -> str | None:
    match = CSRF.search(html or "")
    return match.group(1) if match else None


def field_names(html: str) -> list[tuple[str, str]]:
    """(tag, name) for every named form control: input, select, textarea, button.

    The manage form's disk format is a `<select>` and the boot command line is a
    `<textarea>`; a dump that lists only `<input>` hides exactly the fields the
    tool posts. The button matters too: the Danger Zone's two forms differ
    **only** in their button's `name="action"` value (`reinstall` and the
    destroy form's `delete`), so a dump that drops buttons cannot show the
    difference between erasing the disk and the whole machine.
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


def refuse_action(fields: dict, permitted_actions: tuple[str, ...] = ()) -> str | None:
    """The reason a form's `action` is too dangerous to post, or None when allowed.

    The Danger Zone's reinstall form posts to `/manage/<id>/danger/`, whose path
    names no forbidden verb — `refuse_path` waves it through. The destroy form
    sits in the same block: one word apart (`action=delete`, literal `DELETE`).
    So the fence for these two is the **field**. `reinstall` is permitted only
    by the verb that owns it (passed in by `write_reinstall`), and the destroy
    values are never permitted at all.
    """
    action = (fields or {}).get("action", "")
    if action in NEVER_ACTIONS:
        return f"the field action={action} is never permitted (it is the destroy form)"
    if action == REINSTALL_ACTION and REINSTALL_ACTION not in permitted_actions:
        return ("the field action=reinstall is only permitted by --reinstall or "
                "--recycle (it is the Danger Zone's Erase and reinstall form)")
    return None


SECTION = r"<h2>\s*{word} image\b.*?</section>"
PAGE_URL = re.compile(r"https?://[^\s<>\"']+")
PAGE_SHA = re.compile(r"\b[0-9a-f]{64}\b")


def _section(html: str, word: str) -> str:
    match = re.search(SECTION.format(word=word), html or "", re.S | re.I)
    return match.group(0) if match else ""


def _pair(section: str, marker: str) -> tuple[str, str]:
    """(URL, SHA-256) of the image a section shows, or ("", "") when none is set.

    The admin page shows an attached image as a `<dl>` whose first URL is the
    image and whose first 64-hex value is its hash; a cleared field is prose
    ("By default that is our published release"), so the marker — `Since` for
    the boot image, `Written` for the disk — is what says an image is there.
    """
    if marker not in section:
        return "", ""
    text = re.sub(r"<[^>]+>", " ", section)
    url = PAGE_URL.search(text)
    if not url:
        return "", ""
    digest = PAGE_SHA.search(text[url.end():])
    return url.group(0), digest.group(0) if digest else ""


def image_pairs(html: str) -> dict[str, str]:
    """The boot and disk pairs a reinstall is about to clear.

    Both image fields live on the machine's storage, so the reinstall wipes
    them (`docs/product/SPIKE-servury-reinstall.md` §4). The line the operator
    needs back is exactly this pair, read before the POST.
    """
    boot_url, boot_sha = _pair(_section(html, "Boot"), "Since")
    disk_url, disk_sha = _pair(_section(html, "Disk"), "Written")
    return {"boot_url": boot_url, "boot_sha256": boot_sha,
            "disk_url": disk_url, "disk_sha256": disk_sha}


def recycle_target(server: str) -> str | None:
    """The reason the destructive verb may not name `server`, or None when it may.

    Rule (a) of the fence (`SPIKE-servury-reinstall.md` §8): one id, exact
    match. The allow-list is shared with the non-destructive writes, and those
    legitimately reach more than one machine (the tenant's box is on it), so the
    destructive verb reads it strictly: the file must name exactly one id and
    `--server` must be that id. A widened list refuses rather than guessing.
    """
    allowed = allowed_servers()
    if len(allowed) != 1:
        return (f"the allow-list {SERVER_ID_FILE} names {len(allowed)} ids "
                f"{sorted(allowed) or '[]'}; the reinstall is fenced to exactly one "
                "(trim the file to the machine that may be erased)")
    only = next(iter(allowed))
    if server != only:
        return f"{server} is not the allow-list's one id ({only}) — no prefix, no case folding"
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


def post(opener, url: str, fields: dict[str, str], *,
         permitted_actions: tuple[str, ...] = ()) -> str:
    why = refuse_path(urllib.parse.urlparse(url).path)
    if why:
        raise SystemExit(f"panel: refusing to POST to {url}: {why}")
    why = refuse_action(fields, permitted_actions)
    if why:
        raise SystemExit(f"panel: refusing to POST to {url}: {why}")
    allowed = POST_FIELDS
    unknown = set(fields) - allowed
    if unknown:
        raise SystemExit(f"panel: refusing unknown form field(s) {sorted(unknown)}")
    # Every write path audits before it posts, so `account_of` is normally a
    # cache hit here. Reading it through the function (not `_ACCOUNT`) means a
    # future path that posts first still writes a line naming the account it
    # landed in, or `?` when the session could not be read — never a bare POST.
    account = account_of(opener)
    audit(f"{datetime_stamp()} POST {url} fields={sorted(fields)} account={account}")
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


def danger_url(server: str) -> str:
    """The Danger Zone page: where the reinstall (and the destroy) form posts."""
    return f"{BASE}/manage/{server}/danger/"


def manage_url(server: str) -> str:
    return f"{BASE}/manage/{server}/"


STATUS_REINSTALLING = re.compile(r"\bReinstalling\b", re.I)
STATUS_RUNNING = re.compile(r"\bRunning\b", re.I)


def machine_status(html: str) -> str:
    """"reinstalling", "running" or "?" from the manage page's own header."""
    if STATUS_REINSTALLING.search(html or ""):
        return "reinstalling"
    if STATUS_RUNNING.search(html or ""):
        return "running"
    return "?"


def ssh_keys_from(path: str) -> tuple[str, str, int]:
    """(field value, first key's comment, count) for the reinstall form.

    The vendor's textarea takes "SSH public keys, one per line" and "whatever is
    in this box becomes the whole authorized_keys file of the new install". The
    key comes from a file, never from argv: argv is world-readable in `ps`, and
    a caller who could hand the form a line could hand it another tenant's key.
    """
    text = Path(path).read_text(encoding="utf-8")
    keys = [line.strip() for line in text.splitlines() if line.strip()]
    if not keys:
        raise ValueError(f"{path} holds no public key")
    for key in keys:
        if not key.startswith(("ssh-", "ecdsa-", "sk-")):
            raise ValueError(f"{path} holds a line that is not a public key: {key[:24]!r}")
    comment = keys[0].split()[-1] if len(keys[0].split()) > 2 else "(no comment)"
    return "\n".join(keys), comment, len(keys)


def write_reinstall(
    opener, server: str, os_name: str, want_snp: bool, want_fde: bool,
    want_measured: bool, ssh_key_file: str, apply: bool, *,
    wait: float = 300.0, interval: float = 10.0,
) -> int:
    """ERASE the machine's disk and rebuild it from the chosen stock template.

    This is the one call that replaces a tenant's disk and its measured boot
    image without the user, and it sets the machine's `authorized_keys` to
    whatever key is posted — so a machine recycled for tenant B must be given
    tenant B's key here, and this is the step that enforces it. The tool types
    `ERASE` itself, requires the page's own "Erase and reinstall" label, and
    writes the line before it posts.
    """
    why = recycle_target(server)
    if why:
        print(f"panel: refusing the reinstall: {why}", file=sys.stderr)
        return 2
    refused = require_owned(opener, server)
    if refused is not None:
        return refused
    try:
        ssh_key, comment, count = ssh_keys_from(ssh_key_file)
    except (OSError, ValueError) as error:
        print(f"panel: --ssh-key-file {ssh_key_file}: {error}", file=sys.stderr)
        return 2
    page = danger_url(server)
    html = get(opener, page)
    token = parse_csrf(html)
    if not token:
        print("panel: no csrf_token on the danger page — the session may have expired", file=sys.stderr)
        return 1
    if REINSTALL_CONTROL not in html:
        print(
            f"panel: the danger page carries no {REINSTALL_CONTROL!r} control — refusing "
            "(the form changed, or this page is not the reinstall form)",
            file=sys.stderr,
        )
        return 1
    pairs = image_pairs(get(opener, admin_url(server)))
    snp = "1" if want_snp else "0"
    fde = "1" if want_fde else "0"
    measured = "1" if want_measured else "0"
    audit(audit_line(
        opener,
        f"REINSTALL server={server} os={os_name} snp={snp} fde={fde} measured={measured} "
        f"key={comment} keys={count} "
        f"boot={pairs['boot_url'] or 'none'} boot_sha256={pairs['boot_sha256'] or 'none'} "
        f"disk={pairs['disk_url'] or 'none'} disk_sha256={pairs['disk_sha256'] or 'none'} "
        f"apply={int(bool(apply))} route=panel/danger",
    ))
    print(f"panel: {'erasing and reinstalling' if apply else 'would erase and reinstall'} "
          f"{server} (os={os_name})")
    print(f"panel:   page {page} carries {REINSTALL_CONTROL!r}")
    print(f"panel:   builds on snp={snp} fde={fde} measured={measured}")
    print(f"panel:   authorized_keys ← {count} key(s) from {ssh_key_file} ({comment})")
    print(f"panel:   confirm {ERASE} — typed by the tool, never an argument")
    print(f"panel:   loses boot   {pairs['boot_url'] or '(none)'} "
          f"{('(' + pairs['boot_sha256'][:16] + '…)') if pairs['boot_sha256'] else ''}")
    print(f"panel:   loses disk   {pairs['disk_url'] or '(none)'} "
          f"{('(' + pairs['disk_sha256'][:16] + '…)') if pairs['disk_sha256'] else ''}")
    if not apply:
        print("panel: dry run — pass --apply and --confirm to erase the disk")
        return 0
    fields = {
        "csrf_token": token,
        "os": os_name,
        "want_snp": snp,
        "want_fde": fde,
        "want_measured": measured,
        "ssh_key": ssh_key,
        "confirm": ERASE,
        "action": REINSTALL_ACTION,
    }
    result = post(opener, page, fields, permitted_actions=(REINSTALL_ACTION,))
    refusal = refused_note(result)
    if refusal:
        print(f"panel: reinstall refused: {refusal}", file=sys.stderr)
        return 1
    submitted = notice(result)
    print(f"panel: reinstall submitted for {server}" + (f" — {submitted}" if submitted else ""))
    if wait <= 0:
        return 0
    started = time.time()
    status = machine_status(get(opener, manage_url(server)))
    while True:
        print(f"panel:   {round(time.time() - started)}s — {status}")
        if status == "running":
            break
        if time.time() - started >= wait:
            print(f"panel: the machine did not report Running in {int(wait)}s "
                  f"(last {status!r})", file=sys.stderr)
            return 1
        time.sleep(max(interval, 1))
        status = machine_status(get(opener, manage_url(server)))
    print(f"panel: {server} is Running; its boot and disk image fields are now cleared")
    return 0


def recycle(
    opener, server: str, *, os_name: str, want_snp: bool, want_fde: bool,
    want_measured: bool, ssh_key_file: str, boot_url: str, boot_sha256: str,
    cmdline: str, disk_url: str, disk_sha256: str, fmt: str, apply: bool,
    wait: float = 300.0, interval: float = 10.0,
) -> int:
    """The three writes a freed machine needs, in the order the vendor requires.

    A reinstall clears the boot-image and disk-image fields and leaves a stock
    template, so the pair has to be re-applied afterwards; and the disk write
    starts the machine itself, which is the first boot of the recycled disk
    (`docs/product/SPIKE-servury-reinstall.md` §6). The console is armed first:
    the first boot's transcript is the machine's own evidence, and it cannot be
    recovered after the fact.

    The one-id fence runs **before the first write**, not on the way to the
    erase: arming the console is a POST too, so a `--recycle` aimed at a
    widened allow-list must be refused before it touches anything. (Round-1
    review of this card found the console going out to the live tenant before
    `write_reinstall` reached its own fence.)
    """
    why = recycle_target(server)
    if why:
        print(f"panel: refusing the recycle: {why}", file=sys.stderr)
        return 2
    print(f"panel: recycle {server} — console log, reinstall, boot image, disk image")
    code = console(opener, server, "on", dry_run=not apply)
    if code:
        return code
    code = write_reinstall(opener, server, os_name, want_snp, want_fde, want_measured,
                           ssh_key_file, apply=apply, wait=wait, interval=interval)
    if code:
        return code
    code = write_boot_image(opener, server, boot_url, boot_sha256, cmdline,
                            apply=apply, wait=wait, interval=interval)
    if code:
        return code
    code = write_disk(opener, server, disk_url, fmt, apply, disk_sha256)
    if code:
        return code
    if apply:
        print(f"panel: recycle submitted. The disk write starts {server}; its first boot "
              "is the machine's own step (docs/runbooks/recycle.md §3)")
    else:
        print("panel: dry run — pass --apply and --confirm to run the three writes")
    return 0


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


def api_order(key: str, plan: str, os_name: str, days: str, hostname: str,
              ssh_key: str, preorder: bool = False) -> dict:
    """POST ?action=create; the key's account is charged its partner price."""
    params: dict[str, object] = {
        "plan_id": int(plan), "hostname": hostname, "os": os_name, "days": int(days),
        "ssh_key": ssh_key, "snp": True, "fde": True, "measured": True,
    }
    if preorder:
        params["preorder"] = True
    request = urllib.request.Request(
        ORDER_ROUTE,
        data=json.dumps(params).encode(),
        headers={"X-Reseller-Key": key, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {error.code} {detail[:300]}") from None
    if not payload.get("success", True):
        raise RuntimeError(str(payload.get("error") or "order refused"))
    return payload.get("data") or {}


def order(opener, plan: str, location: str, os_name: str, days: str, hostname: str,
          apply: bool, ssh_key_file: str, key_file: str = str(ORDER_KEY_FILE),
          preorder: bool = False) -> int:
    """Buy one machine through the API at the account's partner price.

    The panel order form charges list price even on an account that holds a key,
    so it is no longer an order path. `location` does not travel: a plan id names
    its location (108 is Canada Core, 127 the US one), and it stays in the
    signature only so the audit line reads the same way it always did. A measured
    box takes exactly one SSH key and it must be given here — there is no later
    add.
    """
    if not ssh_key_file:
        print("panel: --order needs --ssh-key-file (measured boot takes exactly one key)", file=sys.stderr)
        return 2
    if not str(plan).isdigit():
        print(f"panel: --plan must be an API plan id (a number), not {plan!r}", file=sys.stderr)
        return 2
    key_path = Path(key_file)
    if not key_path.is_file():
        print(f"panel: no API key at {key_path}", file=sys.stderr)
        return 2
    ssh_key = Path(ssh_key_file).read_text(encoding="utf-8").strip()
    audit(audit_line(
        opener,
        f"ORDER route=api plan={plan} loc={location} days={days} host={hostname} "
        f"key={key_path.name} apply={apply}",
    ))
    print(f"panel: {'ordering' if apply else 'would order'} plan {plan} for {days} days at the "
          f"partner price — {ORDER_ROUTE}")
    if not apply:
        print("panel: dry run — pass --apply to buy")
        return 0
    try:
        result = api_order(key_path.read_text(encoding="utf-8").strip(), plan, os_name, days,
                           hostname, ssh_key, preorder)
    except (RuntimeError, OSError) as error:
        print(f"panel: order refused — {error}", file=sys.stderr)
        return 1
    print(f"panel: ordered {result.get('server_id')} — {result.get('hostname')} "
          f"{result.get('status')}, charged ${result.get('amount_charged')}")
    return 0


def _reinstall_missing(args) -> str | None:
    """The flags the destructive verb needs and was not given, or None.

    No defaults, on purpose: the vendor's rule is "what you do not send is off",
    so an omitted `--want-measured` would silently build an unmeasured machine
    and an omitted `--os` a stock template nobody chose.
    """
    missing = []
    if not args.os_name:
        missing.append("--os")
    for flag, value in (("--want-snp", args.want_snp),
                        ("--want-fde", args.want_fde),
                        ("--want-measured", args.want_measured)):
        if value is None:
            missing.append(flag)
    if not args.ssh_key_file:
        missing.append("--ssh-key-file")
    return ", ".join(missing) or None


def _recycle_missing(args, cmdline: str) -> str | None:
    missing = [flag for flag, value in (
        ("--boot-url", args.boot_url), ("--boot-sha256", args.boot_sha256),
        ("--disk-url", args.disk_url), ("--disk-sha256", args.disk_sha256),
    ) if not value]
    if not cmdline:
        missing.append("--cmdline-file")
    for flag, value in (("--boot-sha256", args.boot_sha256),
                        ("--disk-sha256", args.disk_sha256)):
        if value and not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            return f"{flag} must be 64 hex characters"
    return ", ".join(missing) or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drive Servury's panel (boot image, disk, power, console) without a browser.")
    parser.add_argument("--login", action="store_true", help="log in once and save the session cookies")
    parser.add_argument("--dump", action="store_true", help="print the forms and their fields (input, select, textarea)")
    parser.add_argument("--show", action="store_true", help="print the current boot image, if present")
    parser.add_argument("--set", action="store_true", help="write the disk image from --url and --sha256")
    parser.add_argument("--set-boot-image", action="store_true", help="set the kernel (UKI) from --url and --sha256")
    parser.add_argument("--power", action="store_true", help="press a power button: --action start|stop|restart|forcestop")
    parser.add_argument("--console", action="store_true", help="serial log: --action on|off|read")
    parser.add_argument("--reinstall", action="store_true",
                        help="the Danger Zone reinstall (ERASE), fenced to the allow-list's one id")
    parser.add_argument("--recycle", action="store_true",
                        help="console log, reinstall, boot image, disk image — the order a freed "
                             "machine needs; the disk write starts the machine itself")
    parser.add_argument("--want-snp", action="store_true", default=None,
                        help="build the machine with RAM encryption (required, no default: "
                             "the vendor's rule is 'what you do not send is off')")
    parser.add_argument("--want-fde", action="store_true", default=None,
                        help="build the machine with disk encryption (required, no default)")
    parser.add_argument("--want-measured", action="store_true", default=None,
                        help="build the machine with measured boot (required, no default)")
    parser.add_argument("--action", default="restart", help="the power/console action (default: restart)")
    parser.add_argument("--cmdline", default="", help="with --set-boot-image: a command line passed verbatim (default empty)")
    parser.add_argument("--cmdline-file", default="",
                        help="with --recycle: read the per-machine command line from a file "
                             "(passed verbatim to the boot image)")
    parser.add_argument("--boot-url", default="", help="with --recycle: the published UKI URL")
    parser.add_argument("--boot-sha256", default="", help="with --recycle: that UKI's SHA-256")
    parser.add_argument("--disk-url", default="", help="with --recycle: the published disk image URL")
    parser.add_argument("--disk-sha256", default="", help="with --recycle: that image's SHA-256")
    parser.add_argument("--wait", type=float, default=300.0,
                        help="with --reinstall/--recycle: seconds to wait for Running (default 300)")
    parser.add_argument("--interval", type=float, default=10.0,
                        help="with --reinstall/--recycle: poll interval in seconds (default 10)")
    parser.add_argument("--server", default="", help="8-character server id")
    parser.add_argument("--url", default="")
    parser.add_argument("--sha256", default="")
    parser.add_argument("--order", action="store_true", help="buy one machine through the API at the partner price")
    parser.add_argument("--plan", default="108", help="API plan id (a plan id names its location: 108 Canada, 127 US)")
    parser.add_argument("--location", default="2", help="label only — the plan id names the location")
    parser.add_argument("--os", dest="os_name", default=None,
                        help="the stock template id (required for --reinstall/--recycle; "
                             "default debian12 for --order)")
    parser.add_argument("--days", default="31")
    parser.add_argument("--hostname", default="odl-dev")
    parser.add_argument("--key-file", default=str(ORDER_KEY_FILE), help="API key of the account that pays")
    parser.add_argument("--preorder", action="store_true", help="accept a sold-out plan (paid now, final)")
    parser.add_argument("--ssh-key-file", default="", help="public key pasted into the order (required: measured boot)")
    parser.add_argument("--whoami", action="store_true", help="print the account this session belongs to")
    parser.add_argument("--expect-user", default=None, help="refuse to write unless the session's User ID matches")
    parser.add_argument("--apply", action="store_true", help="with --set/--set-boot-image/--reinstall/--recycle: actually submit")
    parser.add_argument("--confirm", action="store_true", help="with --set/--reinstall/--recycle: the disk's contents will be replaced")
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
        return order(opener, args.plan, args.location, args.os_name or "debian12", args.days,
                     args.hostname, args.apply, args.ssh_key_file, args.key_file, args.preorder)
    if args.reinstall or args.recycle:
        # The destructive verb is fenced to the allow-list's own one id, so the
        # deliberate-widening flag is refused rather than folded in.
        if args.allow_server:
            print("panel: --reinstall/--recycle refuse --allow-server: the destructive "
                  f"verb is fenced to the one id in {SERVER_ID_FILE}", file=sys.stderr)
            return 2
        if not args.server:
            print("panel: --reinstall/--recycle need --server", file=sys.stderr)
            return 2
        missing = _reinstall_missing(args)
        cmdline = None
        if args.recycle:
            if args.cmdline and args.cmdline_file:
                print("panel: --recycle takes --cmdline-file, not both", file=sys.stderr)
                return 2
            if args.cmdline_file:
                try:
                    cmdline = Path(args.cmdline_file).read_text(encoding="utf-8").strip()
                except OSError as error:
                    print(f"panel: --cmdline-file {args.cmdline_file}: {error}", file=sys.stderr)
                    return 2
            elif args.cmdline:
                cmdline = args.cmdline
            if missing is None:
                missing = _recycle_missing(args, cmdline)
        if missing:
            if missing.endswith("characters"):
                print(f"panel: {missing}", file=sys.stderr)
            else:
                print(f"panel: {'--recycle' if args.recycle else '--reinstall'} needs "
                      f"{missing} (no defaults: what you do not send is off)", file=sys.stderr)
            return 2
        if args.apply and not args.confirm:
            print("panel: --apply also needs --confirm (the disk's contents are replaced)",
                  file=sys.stderr)
            return 2
        if args.reinstall:
            return write_reinstall(
                opener, args.server, args.os_name, args.want_snp, args.want_fde,
                args.want_measured, args.ssh_key_file, apply=args.apply and args.confirm,
                wait=args.wait, interval=args.interval,
            )
        return recycle(
            opener, args.server, os_name=args.os_name, want_snp=args.want_snp,
            want_fde=args.want_fde, want_measured=args.want_measured,
            ssh_key_file=args.ssh_key_file, boot_url=args.boot_url,
            boot_sha256=args.boot_sha256, cmdline=cmdline or "",
            disk_url=args.disk_url, disk_sha256=args.disk_sha256, fmt=args.format,
            apply=args.apply and args.confirm, wait=args.wait, interval=args.interval,
        )
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
