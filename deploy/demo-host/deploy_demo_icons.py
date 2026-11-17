#!/usr/bin/python3
"""Put the website icon on the live demo machine — ODL-505's demo update.

A tenant gets the icon set from the signed installer: the five files land in
``/usr/local/lib/odile/icons``, the first-visit server answers the five site
paths, and the installer repoints the built Webapp's three tab links. The demo
host ``odl-7971a8`` was provisioned before that cut, so this program is the
same write on the machine a judge sees. It goes over the machine's own Webapp
console (``tenant_console.py``): Odile holds no SSH key to a tenant.

It puts, as root:

  * ``installer/first_visit.py``           -> the page server, with the routes
  * ``verify/verify.py``                   -> the /verify page program (ODL-236)
  * ``templates/verify.html``              -> the /verify page the program renders
  * ``verify-lede.html``, ``verify-command.html`` -> the demo's two paragraphs,
    beside the apply that injects them (an icons deploy leaves the page the
    demo-public deploy does: the demo lede and one ``<!--demo-check-->``
    section, ODL-563 follow-up)
  * ``web/first-visit.html``                -> the first-visit page
  * ``web/{favicon.ico,icon-32.png,…}``     -> ``/usr/local/lib/odile/icons``
  * ``deploy/image/odile-webapp-brand.py``  -> the union re-assert (name, chrome, tab)
  * ``deploy/demo-host/webapp-chrome.js``   -> ``/usr/local/lib/odile/webapp-chrome.js``
  * ``deploy/demo-host/login.html``         -> ``/srv/odile-demo/login.html``
  * ``deploy/caddy/demo-tenant-site.caddy`` -> the three static tab-icon routes
    and the two the shell links

Then it converges with ``apply_demo_public.py``'s own functions (the demo
lede, the sign-in page and the Caddy block), restarts ``odile-first-visit``
and ``odile-verify``, and runs the re-assert twice so a second run proves
``same``.

The chrome rides along because the titlebar's ``Verify this machine`` link is
the demo's own ``/verify`` on this host (ODL-564). The installed script is the
re-assert helper's source — it copies it into the bundle as
``odile-demo-chrome.js`` — so a stale copy on the machine would send a judge to
HQ's ``odile.fyi/verify`` and back through its 302, with another host in the
address bar.

Idempotent: every put is content-compared by the installer on the machine's
side (``install`` rewrites only the bytes given), the helper prints ``same``
on a converged bundle, and the Caddy splice is content-compared.

    ./deploy/demo-host/deploy_demo_icons.py --dry-run
    ./deploy/demo-host/deploy_demo_icons.py
    ./deploy/demo-host/deploy_demo_icons.py --verify-only

Exit codes: 0 converged and measured, 1 a failed apply or measurement, 2 bad
input.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import tenant_console  # noqa: E402
import apply_demo_public as demo_apply  # noqa: E402

DEMO_HOST = "demo.odile.fyi"
BASE = f"https://{DEMO_HOST}"
LIB = "/usr/local/lib/odile"
ICON_ROOT = f"{LIB}/icons"
DEMO_LIB = "/usr/local/lib/odile-demo-public"

#: The reviewed set and its pins (ODL-504 round 3, unchanged since).
ICONS = (
    ("favicon.ico", "8f5f997684f2d8eb2336eef46ef8e100776ecaa35c0375faadd4bb7e1ab17fba", "image/x-icon"),
    ("icon-32.png", "28bedf61e9f5bad25d9a7b874bab788634299eb1e08a9bd99f614a09f9f904bc", "image/png"),
    ("icon-192.png", "623f75f50940a5420500996f0a527298fa77b9707f166fbb3b8c716fcb85ca41", "image/png"),
    ("icon-512.png", "35df8e7ba36fb221e7fab823c992c396ac6471d46ce5060383ee7a64f3980382", "image/png"),
    ("apple-touch-icon.png", "dd5386a9c772a5d67702f35fd22eed649783a8575d54f4cbd641111434123773", "image/png"),
)

#: The three files the sign-in page links, which Caddy answers from the page
#: root (t_6b25f6e5); the two shell-only sizes stay in the page server's root.
TAB_ICONS = ("favicon.ico", "icon-32.png", "apple-touch-icon.png")

#: (local source, remote path, mode) — the machine's own copies.
PUTS = (
    (REPO / "installer" / "first_visit.py", f"{LIB}/first_visit.py", "0755"),
    (REPO / "verify" / "verify.py", f"{LIB}/verify.py", "0755"),
    (REPO / "templates" / "verify.html", f"{LIB}/verify.html", "0644"),
    (REPO / "web" / "first-visit.html", f"{LIB}/first-visit.html", "0644"),
    *[(REPO / "web" / name, f"{ICON_ROOT}/{name}", "0644")
      for name, _sha, _type in ICONS],
    *[(REPO / "web" / name, f"{DEMO_LIB}/{name}", "0644") for name in TAB_ICONS],
    (REPO / "deploy" / "image" / "odile-webapp-brand.py",
     f"{LIB}/odile-webapp-brand.py", "0755"),
    (REPO / "deploy" / "demo-host" / "webapp-chrome.js",
     f"{LIB}/webapp-chrome.js", "0644"),
    (REPO / "deploy" / "demo-host" / "login.html",
     f"{DEMO_LIB}/login.html", "0644"),
    # The two demo paragraphs the apply injects. They travel with the apply so
    # an icons deploy reads its own committed copy, not an older one the last
    # demo-public deploy left on the machine (ODL-563 follow-up).
    (REPO / "deploy" / "demo-host" / "verify-lede.html",
     f"{DEMO_LIB}/verify-lede.html", "0644"),
    (REPO / "deploy" / "demo-host" / "verify-command.html",
     f"{DEMO_LIB}/verify-command.html", "0644"),
    (REPO / "deploy" / "caddy" / "demo-tenant-site.caddy",
     f"{DEMO_LIB}/demo-tenant-site.caddy", "0644"),
    (REPO / "deploy" / "demo-host" / "apply_demo_icons.py",
     f"{DEMO_LIB}/apply_demo_icons.py", "0755"),
)

SITE_LINKS = (
    '<link rel="icon" href="/favicon.ico" sizes="16x16 32x32 48x48">',
    '<link rel="icon" type="image/png" sizes="32x32" href="/icon-32.png">',
    '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">',
)
LOGIN_LINKS = (
    '<link rel="icon" href="./favicon.ico" sizes="16x16 32x32 48x48">',
    '<link rel="icon" type="image/png" sizes="32x32" href="./icon-32.png">',
    '<link rel="apple-touch-icon" sizes="180x180" href="./apple-touch-icon.png">',
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def say(word: str, detail: str) -> None:
    print(f"{word:<12} {detail}", flush=True)


def published_password(repo: Path = REPO) -> str:
    """The demo password the sign-in page publishes.

    ODL-901 retired the README's judge table, so the page the judge lands on
    is the single published copy: it carries the password as the literal it
    posts to the gateway, and tests/test_demo_login_page.py pins its sha256.
    README.md's table stays as the fallback for a checkout from before that
    retirement.
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


def local_checks() -> None:
    """The tree's own bytes must be the reviewed ones before a byte travels."""
    for name, pin, _type in ICONS:
        data = (REPO / "web" / name).read_bytes()
        if sha256(data) != pin:
            raise SystemExit(f"error: web/{name} is {sha256(data)}, not the reviewed {pin}")
    page = (REPO / "deploy" / "demo-host" / "login.html").read_text(encoding="utf-8")
    if "data:image/svg+xml" in page:
        raise SystemExit("error: the demo sign-in page still carries a data: favicon")
    for line in LOGIN_LINKS:
        if line not in page:
            raise SystemExit(f"error: the demo sign-in page is missing {line}")
    block = (REPO / "deploy" / "caddy" / "demo-tenant-site.caddy").read_text(encoding="utf-8")
    # t_6b25f6e5: the three paths the sign-in page links are one static handle
    # serving the page root; the two sizes only the shell links keep their own
    # page-server route.
    if "@tabicons path /favicon.ico /icon-32.png /apple-touch-icon.png" not in block:
        raise SystemExit("error: the demo Caddy block has no static tab-icon route")
    for name in ("icon-192.png", "icon-512.png"):
        if f"handle /{name} {{" not in block:
            raise SystemExit(f"error: the demo Caddy block has no /{name} route")
    # The apply re-injects both demo paragraphs after the upload; the page it
    # leaves must carry exactly one command pair, or stop before a byte travels
    # (ODL-563 follow-up: the icons deploy used to drop the section).
    page = template_bytes().decode("utf-8")
    opens, closes = page.count(demo_apply.COMMAND_BEGIN), page.count(demo_apply.COMMAND_END)
    if (opens, closes) != (1, 1):
        raise SystemExit(
            f"error: the converged /verify page carries {opens} demo-check "
            f"marker(s) and {closes} closer(s), expected one of each")
    chrome = (REPO / "deploy" / "demo-host" / "webapp-chrome.js").read_text(encoding="utf-8")
    if "const VERIFY_HREF_DEMO = 'https://demo.odile.fyi/verify'" not in chrome:
        raise SystemExit("error: the chrome's demo Verify link is not the demo's own /verify")


def template_bytes(repo: Path = REPO) -> bytes:
    """The converged /verify template: release page + demo lede + one command.

    The machine's file is never the raw release page — the apply injects both
    demo paragraphs after the deploy installs it. Comparing the remote against
    the raw page rewrote the template on every run and left the command off
    until the apply re-added it (ODL-563's root cause, and the reason the
    deploy's `same` line has to compare against this result).
    """
    release = (repo / "templates" / "verify.html").read_text(encoding="utf-8")
    lede = (repo / "deploy" / "demo-host" / "verify-lede.html").read_text(encoding="utf-8")
    command = (repo / "deploy" / "demo-host" / "verify-command.html").read_text(encoding="utf-8")
    try:
        return demo_apply.inject_command(
            demo_apply.inject_lede(release, lede), command).encode("utf-8")
    except ValueError as exc:
        raise SystemExit(f"error: cannot build the demo /verify template: {exc}") from exc


def converge_verify_template(console, say) -> bool:
    """Put the release /verify page only when its converged result differs.

    The machine's file is the release page with the demo's lede and command
    section inside its tenant block, so comparing the raw release page would
    rewrite on every run. `template_bytes` is the target; the upload is still
    the release page, because the apply writes the rollback copy from it (the
    page a customer machine would have).
    """
    desired = template_bytes()
    remote = f"{LIB}/verify.html"
    current = console.run(f"sudo sha256sum {remote}", check=False).get("stdout", "").split()
    wanted = sha256(desired)
    if current and current[0] == wanted:
        say("same", f"{remote} ({wanted[:16]}…)")
        return False
    console.put(str(REPO / "templates" / "verify.html"), remote, mode="0644")
    say("write", f"{remote} (the release page; the demo lede and command follow)")
    return True


def get(path: str, *, head: bool = False, cookie: str = "") -> tuple[int, dict, bytes]:
    request = urllib.request.Request(BASE + path, method="HEAD" if head else "GET")
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers), b"" if head else response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), b"" if head else exc.read()


def sign_in(password: str) -> str:
    """POST the judge sign-in; return the Cookie header, or '' on a refusal."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    body = json.dumps({"provider": "basic", "username": "judge",
                       "password": password}).encode()
    request = urllib.request.Request(BASE + "/auth/password-login", data=body,
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=30) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    if status != 200:
        return ""
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in jar)


def verify(password: str) -> int:
    failures = 0

    def check(good: bool, label: str) -> None:
        nonlocal failures
        if not good:
            failures += 1
        print(f"  {'ok  ' if good else 'FAIL'} {label}")

    for name, pin, content_type in ICONS:
        status, headers, body = get(f"/{name}")
        check(status == 200, f"GET /{name} -> {status}")
        check(headers.get("Content-Type", "").split(";")[0] == content_type,
              f"/{name} type {headers.get('Content-Type')!r}")
        cache = headers.get("Cache-Control", "")
        check("public" in cache and "max-age=31536000" in cache and "immutable" in cache,
              f"/{name} cache {cache!r}")
        check(sha256(body) == pin, f"/{name} body {sha256(body)[:16]}… == {pin[:16]}…")
        status, headers, body = get(f"/{name}", head=True)
        check(status == 200 and body == b"", f"HEAD /{name} -> {status}, no body")
        length = headers.get("Content-Length", "")
        check(length.isdigit() and int(length) > 0, f"HEAD /{name} Content-Length {length!r}")

    for path, links in (("/login", LOGIN_LINKS), ("/verify", SITE_LINKS)):
        status, _headers, body = get(path)
        text = body.decode("utf-8", "replace")
        check(status == 200, f"GET {path} -> {status}")
        for line in links:
            check(line in text, f"{path} links {line.split('href=')[1].split(' ')[0]}")

    # ODL-563 follow-up: the icons deploy is a writer of /verify, so it
    # measures the section it must keep — no pair or two pairs fails here.
    status, _headers, body = get("/verify")
    text = body.decode("utf-8", "replace")
    opens, closes = text.count(demo_apply.COMMAND_BEGIN), text.count(demo_apply.COMMAND_END)
    check(status == 200 and (opens, closes) == (1, 1),
          f"GET /verify carries one demo-check pair -> {opens} open, {closes} close")
    check("./odile-verify https://demo.odile.fyi --expect" in text,
          "GET /verify prints the --expect line")

    cookie = sign_in(password)
    check(bool(cookie), "POST /auth/password-login as judge -> a session cookie")
    if cookie:
        status, _headers, body = get("/", cookie=cookie)
        text = body.decode("utf-8", "replace")
        check(status == 200, f"GET / with the judge session -> {status}")
        check(text.count('href="./odile-favicon.ico"') == 2
              and text.count('href="./odile-apple-touch-icon.png"') == 1,
              "the Webapp shell's three tab links are Odile's")
        check("./apple-touch-icon.png" not in text.replace(
            "./odile-apple-touch-icon.png", ""),
            "the shell no longer links the vendor's mark as a tab icon")
        # The href the browser runs: the link is built client-side, so the
        # served script is the measurement (ODL-564).
        status, _headers, body = get("/odile-demo-chrome.js", cookie=cookie)
        script = body.decode("utf-8", "replace")
        check(status == 200, f"GET /odile-demo-chrome.js -> {status}")
        check("const VERIFY_HREF_DEMO = 'https://demo.odile.fyi/verify'" in script,
              "the served chrome links the demo's own /verify (same host, one hop)")

    return 0 if failures == 0 else 1


def bundle_head(console) -> str:
    result = console.run(
        'sed -n "1,14p" /home/tenant/.hermes/hermes-agent/apps/desktop/'
        'dist-webapp/index.html')
    return result.get("stdout") or ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Put ODL-505's icon set on the live demo machine.")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    parser.add_argument("--verify-only", action="store_true",
                        help="measure the live surface and exit")
    parser.add_argument("--password-file", default="",
                        help="the published demo password (default: deploy/demo-host/login.html)")
    args = parser.parse_args(argv)

    local_checks()
    for source, remote, mode in PUTS:
        say("plan", f"{source.relative_to(REPO)} -> {remote} ({mode})")
    if args.dry_run:
        say("dry run", "nothing uploaded, no service restarted")
        return 0

    password = (Path(args.password_file).read_text(encoding="utf-8").strip()
                if args.password_file else published_password())
    if args.verify_only:
        return verify(password)

    console = tenant_console.Console(tenant_console.DEFAULT_BASE)
    console.connect("judge", password)
    console.open_session()
    say("console", f"judge on {tenant_console.DEFAULT_BASE}")

    console.run(f"sudo install -d -m 0755 -o root -g root {ICON_ROOT}")
    wrote = False
    for source, remote, mode in PUTS:
        current = console.run(f"sudo sha256sum {remote}", check=False).get("stdout", "").split()
        wanted = sha256(source.read_bytes())
        if current and current[0] == wanted:
            say("same", f"{remote} ({wanted[:16]}…)")
            continue
        console.put(str(source), remote, mode=mode)
        wrote = True
        after = console.run(f"sudo sha256sum {remote}", check=False).get("stdout", "").split()
        if not after or after[0] != wanted:
            raise SystemExit(f"error: {remote} did not take {wanted}")
        say("write", f"{remote} ({wanted[:16]}…)")

    wrote = converge_verify_template(console, say) or wrote

    try:
        result = console.run(
            f"sudo /usr/bin/python3 {DEMO_LIB}/apply_demo_icons.py"
            + (" --restart" if wrote else ""))
        print((result.get("stdout") or "").rstrip())
    except tenant_console.ConsoleError as exc:
        # `ensure_caddy_block` restarts Caddy, and this console rides the
        # public host through it. The block is written and validated before the
        # restart; the socket's death *is* the restart, not a failure.
        if "socket closed" not in str(exc):
            raise
        say("reconnect", "the Caddy restart closed the console; reconnecting")
        console = tenant_console.Console(tenant_console.DEFAULT_BASE)
        console.connect("judge", password)
        console.open_session()
    for command in ("systemctl is-active caddy",
                    "systemctl is-active odile-first-visit",
                    "systemctl is-active odile-verify"):
        result = console.run(f"sudo {command}", check=False)
        say("state", f"{command}: {(result.get('stdout') or '').strip()}")
    block = console.run("sudo grep -c '@tabicons path /favicon.ico' /etc/caddy/Caddyfile",
                        check=False).get("stdout", "").strip()
    if block != "1":
        raise SystemExit("error: the Caddyfile does not carry the static tab-icon route")

    head = bundle_head(console)
    if head:
        print(head.rstrip())
    if head.count('href="./odile-favicon.ico"') != 2:
        raise SystemExit("error: the bundle head does not carry the three Odile links")

    return verify(password)


if __name__ == "__main__":
    sys.exit(main())
