#!/usr/bin/python3
"""Apply the public demo surface on Odile's tenant machine — ODL-503.

This program runs **as root on the tenant** (odl-7971a8). The HQ-side deploy
(`deploy/demo-host/deploy_demo_public.py`) uploads it and these files

    /usr/local/lib/odile-demo-public/apply_demo_public.py    this program
    /usr/local/lib/odile-demo-public/demo-tenant-site.caddy  the site block
    /usr/local/lib/odile-demo-public/login.html              Odile's sign-in page
    /usr/local/lib/odile-demo-public/verify-lede.html        the demo machine's /verify lede
    /usr/local/lib/odile-demo-public/start-here.md           the seeded session
    /usr/local/lib/odile-demo-public/session-receipt.json    the receipt template

and runs it over the tenant's own Webapp socket, with one `shell.exec` line.
It can also be run by hand from the machine's serial console when the Webapp is
down; that is the recovery path in docs/runbooks/demo-public.md.

What it owns, each step idempotent and printed as `same` / `write` / `restart`:

  0. the demo machine's /verify page: the release template's one tenant-block
     paragraph is replaced by `verify-lede.html`, because on this machine Odile
     holds the boot key and the sign-in is shared, and the one-line `--expect`
     verifier command is added after it from `verify-command.html`, with the
     measurement the page serves filled in by the first-visit renderer. A
     customer machine never runs this program and keeps the general row and no
     demo command;
  1. the sign-in page at /srv/odile-demo/login.html, and the three tab files
     it links beside it (pinned to the committed web/ renditions);
  2. the `demo.odile.fyi` block in /etc/caddy/Caddyfile — spliced between its
     markers, whole file backed up, `caddy validate`, then `systemctl restart
     caddy` (the machine runs Caddy with `admin off`), rolled back on failure;
  3. the demo's Hermes settings: Near AI direct (no meter in the path), one
     key, and `webapp.public_url` (the Host-header gate);
  4. the judge account in /home/tenant/.hermes/.env, with the cookie secret
     kept byte-for-byte;
  5. the one seeded "Start here — Odile demo" session in the tenant's
     state.db — written only when the row is absent, never rewritten.

Everything else on the machine is the owner's: the sessions a judge creates,
the seed's later turns, title and flags, the config's own turn and session
settings, the meter's unit. A deploy changes none of them (ODL-503 follow-up,
t_ee5aac66: Savio, 2026-11-13, "they are free to make changes, can see past
sessions, no unexpected reverts / locks / deletes … simply that this is a
shared instance"). There is no boot re-assert unit: the machine keeps the
state its owner leaves. The `/verify` lede (step 0) is demo-only and is the
deploy's write; `odile-release-setup.service` converges once, so no boot
rewrites it afterwards.

The published password comes from `--password-file` or ODL_DEMO_PASSWORD and is
never printed. `--dry-run` changes nothing. Exit codes: 0 converged, 1 a failed
apply that was rolled back, 2 bad input.
"""

from __future__ import annotations

import argparse
import base64
from collections import deque
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

LIB = Path("/usr/local/lib/odile-demo-public")
SITE_BLOCK = LIB / "demo-tenant-site.caddy"
LOGIN_SOURCE = LIB / "login.html"
START_HERE = LIB / "start-here.md"
#: The demo's boot re-assert unit, installed by revisions before t_ee5aac66.
#: It re-ran this program with tune flags after the release installer; this
#: program removes it (disable, delete, daemon-reload) so a boot cannot
#: rewrite the owner's state. The name is split so the "no boot re-assert"
#: fence can grep for the full unit and find none in the tree's code.
DEMO_UNIT = "odile-demo-public"
UNIT_TARGET = Path(f"/etc/systemd/system/{DEMO_UNIT}.service")

#: The release installer's /verify template. It is read by the first-visit
#: service on every request (`FirstVisit.verify`), so replacing the file is
#: served without a restart. The release installer writes it once, on the
#: first boot (`odile-release-setup.service` now converges once); this deploy
#: is the only writer after that (ODL-503 finding 1).
VERIFY_TEMPLATE = Path("/usr/local/lib/odile/verify.html")
#: The demo's lede, uploaded beside this program. It replaces the one lede
#: paragraph inside the template's tenant block; a customer machine never runs
#: this program, so its page keeps the general row.
VERIFY_LEDE = LIB / "verify-lede.html"
#: The demo's /verify command section: the one-line --expect run a judge
#: copies, with the live measurement filled by the first-visit renderer. The
#: release template's tenant block has no command (a customer's page is not the
#: public demo and its machine name is its own), so this is injected here,
#: exactly as the lede is: a demo-only page, not a release re-cut (ODL-503
#: finding 1's route).
VERIFY_COMMAND = LIB / "verify-command.html"
#: The page's one line, before the renderer fills the slot. A judge's browser
#: copies it verbatim; check_demo_public.py runs exactly that line.
COMMAND_BEGIN = "<!--demo-check-->"
COMMAND_END = "<!--/demo-check-->"
COMMAND_RE = re.compile(r"<!--demo-check-->.*?<!--/demo-check-->", re.S)
PAGE_COMMAND = ("./odile-verify https://demo.odile.fyi"
                " --expect {{MEASUREMENT}}")
#: The release page as the boot left it, kept for the runbook's rollback.
VERIFY_BACKUP = LIB / "verify.html.release"
TENANT_BLOCK_RE = re.compile(r"(<!--tenant-->)(.*?)(<!--/tenant-->)", re.S)
LEDE_RE = re.compile(r'<p class="lede">.*?</p>', re.S)

CADDYFILE = Path("/etc/caddy/Caddyfile")
BEGIN = "# BEGIN odile-demo-public"
END = "# END odile-demo-public"
SITE_ROOT = Path("/srv/odile-demo")
LOGIN_TARGET = SITE_ROOT / "login.html"
#: The sign-in page's three tab files (t_6b25f6e5). The deploy uploads the
#: committed `web/` bytes and this program puts them beside the page; the pins
#: are checked at both hops, so neither can install a different picture under
#: the same name. Caddy answers them from this root (the block's one static
#: handle), so the tab does not depend on the page server being up.
ICONS = (
    ("favicon.ico",
     "8f5f997684f2d8eb2336eef46ef8e100776ecaa35c0375faadd4bb7e1ab17fba", "image/x-icon"),
    ("icon-32.png",
     "28bedf61e9f5bad25d9a7b874bab788634299eb1e08a9bd99f614a09f9f904bc", "image/png"),
    ("apple-touch-icon.png",
     "dd5386a9c772a5d67702f35fd22eed649783a8575d54f4cbd641111434123773", "image/png"),
)


HERMES_HOME = Path("/home/tenant/.hermes")
CONFIG_FILE = HERMES_HOME / "config.yaml"
ENV_FILE = HERMES_HOME / ".env"
STATE_DB = HERMES_HOME / "state.db"
#: The gateway writes ``Turn ended: reason=…`` at INFO to this file. Its
#: journal handler is WARNING+, so `journalctl -u odile-agent` does not carry
#: the turn-cap line — the report reads both. (t_098d9cee, merged 81093db.)
AGENT_LOG = HERMES_HOME / "logs" / "agent.log"
AGENT_LOG_TAIL = 4000
#: The release installer's key store (t_e237e261): root-only, one line. It is
#: what a machine rebuilt from the release carries when the profile `.env` has
#: not been written yet; it replaces the retired meter key file.
KEY_STORE = Path("/etc/odile/key.env")
TENANT_USER = "tenant"
TENANT_UID = 1000
TENANT_GID = 1000
TENANT_HOME = Path("/home/tenant")
WORKSPACE = TENANT_HOME / "workspace"
PROFILE_ID = str(HERMES_HOME)
PRINCIPAL_ID = f"uid:{TENANT_UID}"

# The machine's public name. Hermes validates the Host header against the
# Webapp's configured public_url (web_server_boundary._is_accepted_host), so a
# browser on demo.odile.fyi is refused with `400 Invalid Host header` until
# this is the machine's public_url. The release installer writes
# https://<machine>.odile.fyi here on every boot; the demo re-assert puts the
# demo host back. The machine's own name becomes an alias whose Webapp path is
# refused; /verify, /first-visit and /manifest on it stay Caddy's own.
DEMO_PUBLIC_URL = "https://demo.odile.fyi"

# The demo's inference route and its caps. Near AI direct: the meter is not in
# the path, so the single key in the .env is the machine's only credential.
NEAR_BASE_URL = "https://cloud-api.near.ai/v1"
NEAR_KEY_ENV = "HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY"
NEAR_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017-abliterated"
#: The label the demo's provider row carries. The release installer writes
#: "Near AI" on every boot (`installer/odile_install.py`), so this program
#: owns the name in both shapes Hermes accepts: the installer's
#: `custom_providers:` list and Hermes's normalised `custom_providers: []` +
#: `providers:` map. The check reads it back and fails on any other label.
PROVIDER_NAME = "Near AI"
#: The Webapp's console banner, found appended to files a concurrent writer
#: touched on 2026-11-13 (`/etc/caddy/Caddyfile`, `config.yaml`). It is neither
#: Caddy syntax nor a setting; the demo's writers remove it on sight.
CONSOLE_RESIDUE = re.compile(r"^ready:\s*gateway\.ready\b.*$")

# The sign-in page's markers: the deploy refuses to install a page that lost the
# title, the provider the form posts to, the two approved sentences or the
# Hermes credit line.
LOGIN_MARKERS = (
    "<title>Sign in — Odile</title>",
    'data-provider="basic"',
    "This is an Odile demo instance.",
    'aria-label="Sign in"',
)

# The same seed app.py's /judge deep-links: the id is a constant, computed from
# the retired HQ demo's profile and principal so the front door never changes.
# The row written here belongs to *this* machine's principal (uid:1000), which
# is what the tenant's Webapp lists and resumes.
SESSION_SEED = "odile-demo:start-here:v1"
REQUEST_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, SESSION_SEED))
#: The seeded session's identity is frozen: the id is a hash over the profile
#: and principal the row was minted with on the retired HQ demo, and app.py's
#: /judge deep link carries the same id. The retired account name is spelled in
#: two pieces so it does not reappear in the tree; the string must never change.
SEED_PROFILE_ID = "/home/odile" "demo/.hermes"
SEED_PRINCIPAL_ID = "uid:1001"
SESSION_ID = "local-" + hashlib.sha256(
    json.dumps([SEED_PROFILE_ID, SEED_PRINCIPAL_ID, REQUEST_ID],
               separators=(",", ":")).encode()
).hexdigest()
SESSION_TITLE = "Start here — Odile demo"
SESSION_PROMPT = "Start here — what is this, and what should I try first?"
#: Hermes's tombstone for a deleted session. A delete writes this row; a
#: session that is re-created under the same id is refused a turn
#: (`not_found`) until the tombstone is lifted (measured 2026-11-14 on the
#: re-seeded demo session). Only the seed's own tombstone is ever touched.
RETIRED_PREFIX = "gateway.retired_session.v1."
SESSION_MODEL = NEAR_MODEL
RECEIPT_PREFIX = "gateway.local_policy.v1:"
#: The lineage row a legacy-bound receipt needs (hermes_state_local_migration
#: .legacy_lineage_root): the id, profile, principal and legacy id must match
#: byte-for-byte, or the receipt is refused as storage_unavailable.
LEGACY_PREFIX = "gateway.local_legacy.v1:"

HEALTH = "http://127.0.0.1:9130/"


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, **kwargs)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def say(verb: str, message: str) -> None:
    print(f"{verb:<12} {message}", flush=True)


# ---------------------------------------------------------------------------
# files


def write_atomic(path: Path, data: bytes, mode: int, uid: int, gid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.chown(tmp, uid, gid)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------------------------------------------------------------------------
# 0. the /verify lede (ODL-503 finding 1)


def tenant_blocks(page: str) -> list[re.Match[str]]:
    """Every `<!--tenant-->…<!--/tenant-->` block, in page order."""
    return list(TENANT_BLOCK_RE.finditer(page))


def tenant_block(page: str) -> str:
    """The one tenant block's body, or a loud error."""
    matches = tenant_blocks(page)
    if len(matches) != 1:
        raise ValueError("the verify page has %d tenant blocks, expected 1" % len(matches))
    return matches[0].group(2)


def lede_problem(markup: str) -> str:
    """Why *markup* is not one injectable lede paragraph; '' when it is."""
    text = markup.strip()
    if "\n" in text:
        return "the lede is not one line"
    if not (text.startswith('<p class="lede">') and text.endswith("</p>")):
        return 'the lede is not one <p class="lede">…</p> paragraph'
    if text.count("<p") != 1 or text.count("</p>") != 1:
        return "the lede carries more than one paragraph"
    if "{{" in text:
        return "the lede carries a page slot"
    return ""


def inject_lede(page: str, lede: str) -> str:
    """Return *page* with its tenant-block lede replaced by *lede*.

    The demo machine's copy of the release template differs in exactly this
    paragraph: the demo block, the report card and the CSS stay byte-identical.
    A release re-cut that moves the anchor or adds a second lede is an error,
    not a silently half-rendered page.
    """
    problem = lede_problem(lede)
    if problem:
        raise ValueError(problem)
    matches = tenant_blocks(page)
    if len(matches) != 1:
        raise ValueError("the verify page has %d tenant blocks, expected 1" % len(matches))
    match = matches[0]
    block = match.group(2)
    if len(LEDE_RE.findall(block)) != 1:
        raise ValueError("the tenant block has %d ledes, expected 1"
                         % len(LEDE_RE.findall(block)))
    replaced = LEDE_RE.sub(lambda _match: lede.strip(), block, count=1)
    return page[:match.start(2)] + replaced + page[match.end(2):]


def lede_source() -> Path:
    """The uploaded copy beside this program, else the installed one.

    The deploy's dry run executes the uploaded program from `/tmp`, before the
    installed copy exists; the same resolution `_source` uses keeps one lede.
    """
    beside = Path(__file__).resolve().parent / VERIFY_LEDE.name
    return beside if beside.is_file() else VERIFY_LEDE


def ensure_verify_lede(dry: bool) -> bool:
    """Put the demo's lede on this machine's /verify template (ODL-503 f1).

    The customer's template keeps the general row: this program runs only on
    the demo tenant, and the file it replaces is the release installer's page,
    written once on the first boot (the release setup converges once). No boot
    unit re-cuts it; this deploy's `--write` is the writer.
    """
    source = lede_source()
    if not source.is_file():
        raise SystemExit(f"error: missing {source}")
    if not VERIFY_TEMPLATE.is_file():
        raise SystemExit(f"error: missing {VERIFY_TEMPLATE}")
    try:
        lede = source.read_text(encoding="utf-8")
        page = VERIFY_TEMPLATE.read_text(encoding="utf-8")
        wanted = inject_lede(page, lede)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"error: cannot inject {source} into {VERIFY_TEMPLATE}: {exc}") from exc
    if wanted == page:
        say("same", f"{VERIFY_TEMPLATE} (the demo lede)")
        return False
    say("would write" if dry else "write",
        f"{VERIFY_TEMPLATE} (the demo lede; rollback {VERIFY_BACKUP.name})")
    if not dry:
        write_atomic(VERIFY_BACKUP, page.encode("utf-8"), 0o644, 0, 0)
        write_atomic(VERIFY_TEMPLATE, wanted.encode("utf-8"), 0o644, 0, 0)
    return True


def command_source() -> Path:
    """The uploaded command section beside this program, else the installed one.

    Same resolution as lede_source(): the deploy's dry run runs the uploaded
    program from /tmp and must read the uploaded section, not an older one.
    """
    beside = Path(__file__).resolve().parent / VERIFY_COMMAND.name
    return beside if beside.is_file() else VERIFY_COMMAND


def command_problem(markup: str) -> str:
    """Why *markup* is not one injectable command section; '' when it is.

    The section is the judge's one line: one marked block, one
    ``{{MEASUREMENT}}`` slot for the renderer, no script and no image (a
    tenant's page server carries no static route for the painting), and the
    command the page promises. A release template re-cut that moves the block
    is an error, not a silently half-rendered page.
    """
    text = markup.strip()
    if not (text.startswith(COMMAND_BEGIN) and text.endswith(COMMAND_END)):
        return "the command block is not one marked section"
    if text.count(COMMAND_BEGIN) != 1 or text.count(COMMAND_END) != 1:
        return "the command block carries more than one marker pair"
    if "<section" not in text or text.count("<section") != 1:
        return "the command block is not one section"
    if "<script" in text:
        return "the command block carries a script"
    if "<img" in text:
        return "the command block carries an image"
    if text.count("{{MEASUREMENT}}") != 1:
        return "the command block needs exactly one {{MEASUREMENT}} slot"
    if "{{" in text.replace("{{MEASUREMENT}}", ""):
        return "the command block carries another page slot"
    if PAGE_COMMAND not in text:
        return "the command block does not carry the demo's --expect command"
    return ""


def inject_command(page: str, command: str) -> str:
    """Return *page* with the demo's command section inside its tenant block.

    The first apply appends the section after the tenant block's lede; a later
    apply replaces the marked block in place, so a changed command converges
    and a second run is byte-identical. The rest of the page — the demo block,
    the report card, the CSS — is untouched, and a customer machine never runs
    this program.
    """
    problem = command_problem(command)
    if problem:
        raise ValueError(problem)
    matches = tenant_blocks(page)
    if len(matches) != 1:
        raise ValueError("the verify page has %d tenant blocks, expected 1" % len(matches))
    match = matches[0]
    body = match.group(2)
    existing = list(COMMAND_RE.finditer(body))
    if len(existing) > 1:
        raise ValueError("the tenant block has %d command blocks, expected at most 1"
                         % len(existing))
    wanted = command.strip()
    if existing:
        replaced = body[:existing[0].start()] + wanted + body[existing[0].end():]
    else:
        replaced = body.rstrip() + "\n" + wanted + "\n"
    return page[:match.start(2)] + replaced + page[match.end(2):]


def ensure_verify_command(dry: bool) -> bool:
    """Put the demo's verifier command on this machine's /verify template.

    The demo's page is the only public tenant /verify, so the command lives in
    the demo layer: the release template's tenant block stays the customer's
    page (no command, no demo hostname). The write goes to the same file the
    first-visit service reads per request; the lede's update wrote the backup,
    so this step must not overwrite it with a half-injected page.
    """
    source = command_source()
    if not source.is_file():
        raise SystemExit(f"error: missing {source}")
    if not VERIFY_TEMPLATE.is_file():
        raise SystemExit(f"error: missing {VERIFY_TEMPLATE}")
    try:
        command = source.read_text(encoding="utf-8")
        page = VERIFY_TEMPLATE.read_text(encoding="utf-8")
        wanted = inject_command(page, command)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"error: cannot inject {source} into {VERIFY_TEMPLATE}: {exc}") from exc
    if wanted == page:
        say("same", f"{VERIFY_TEMPLATE} (the demo command)")
        return False
    say("would write" if dry else "write",
        f"{VERIFY_TEMPLATE} (the demo command; rollback {VERIFY_BACKUP.name})")
    if not dry and not VERIFY_BACKUP.is_file():
        write_atomic(VERIFY_BACKUP, page.encode("utf-8"), 0o644, 0, 0)
    if not dry:
        write_atomic(VERIFY_TEMPLATE, wanted.encode("utf-8"), 0o644, 0, 0)
    return True


def verify_state() -> dict:
    """The template's lede, command and the rollback copy, as the report states them."""
    page = VERIFY_TEMPLATE.read_text(encoding="utf-8") if VERIFY_TEMPLATE.is_file() else ""
    source = lede_source()
    lede = source.read_text(encoding="utf-8").strip() if source.is_file() else ""
    command_source_path = command_source()
    command = (command_source_path.read_text(encoding="utf-8").strip()
               if command_source_path.is_file() else "")
    try:
        block = tenant_block(page)
    except ValueError:
        block = ""
    first_lede = LEDE_RE.search(block)
    section = COMMAND_RE.search(block)
    return {
        "template_sha256": hashlib.sha256(page.encode("utf-8")).hexdigest() if page else "",
        "demo_lede": bool(block) and first_lede is not None
                     and first_lede.group(0).strip() == lede and bool(lede),
        "demo_command": section is not None and section.group(0).strip() == command
                        and bool(command),
        "customer_lede": "No one at Odile can log in to a tenant machine." in block,
        "rollback": VERIFY_BACKUP.is_file(),
    }


# ---------------------------------------------------------------------------
# 1. the sign-in page


def ensure_login_page(dry: bool) -> bool:
    if not LOGIN_SOURCE.is_file():
        raise SystemExit(f"error: missing {LOGIN_SOURCE}")
    source = LOGIN_SOURCE.read_bytes()
    text = source.decode("utf-8")
    missing = [marker for marker in LOGIN_MARKERS if marker not in text]
    if missing:
        raise SystemExit(f"error: {LOGIN_SOURCE} is missing {missing!r}")
    current = LOGIN_TARGET.read_bytes() if LOGIN_TARGET.is_file() else None
    if current == source:
        say("same", f"{LOGIN_TARGET} (Odile sign-in page)")
        return False
    say("would write" if dry else "write", f"{LOGIN_TARGET} (Odile sign-in page)")
    if not dry:
        write_atomic(LOGIN_TARGET, source, 0o644, 0, 0)
    return True


def icon_source(name: str) -> Path:
    """The uploaded copy beside this program, else the installed one.

    The deploy's dry run executes the uploaded program from its `/tmp` scratch
    directory, before the installed copy exists; the same resolution
    `lede_source` uses keeps one file. In a checkout this falls through to
    `LIB`, which a test points at its own scratch copy.
    """
    beside = Path(__file__).resolve().parent / name
    return beside if beside.is_file() else LIB / name


def ensure_icons(dry: bool) -> bool:
    """Put the three tab files beside the page, byte-pinned (t_6b25f6e5).

    Caddy answers them from this root (the block's one static handle), so the
    sign-in page's tab does not depend on the page server. A file that is not
    the reviewed bytes is refused before anything is written.
    """
    changed = False
    for name, pin, _content_type in ICONS:
        source = icon_source(name)
        if not source.is_file():
            raise SystemExit(f"error: missing {source}")
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != pin:
            raise SystemExit(f"error: {source} is {digest}, not the reviewed {pin}")
        target = SITE_ROOT / name
        if target.is_file() and target.read_bytes() == data:
            say("same", f"{target} (tab icon)")
            continue
        say("would write" if dry else "write", f"{target} (tab icon)")
        if not dry:
            write_atomic(target, data, 0o644, 0, 0)
        changed = True
    return changed


# ---------------------------------------------------------------------------
# 2. the Caddy block


def splice(existing: str, block: str, begin: str, end: str) -> str:
    """Return *existing* with the marked block replaced or appended.

    Exactly zero or one marker pair is allowed; an unbalanced or duplicated
    pair is an error rather than a guess, so a half-edited Caddyfile is never
    overwritten silently.
    """
    block_lines = block.rstrip("\n").splitlines()
    lines = existing.splitlines()
    begins = [i for i, line in enumerate(lines) if line.strip() == begin]
    ends = [i for i, line in enumerate(lines) if line.strip() == end]
    if len(begins) != len(ends) or len(begins) > 1:
        raise ValueError(
            f"expected at most one '{begin}' / '{end}' pair, found "
            f"{len(begins)} begin and {len(ends)} end markers")
    if not begins:
        return "\n".join(lines + [""] + block_lines).rstrip("\n") + "\n"
    first, last = begins[0], ends[0]
    if last < first:
        raise ValueError(f"'{end}' comes before '{begin}'")
    return "\n".join(lines[:first] + block_lines + lines[last + 1:]).rstrip("\n") + "\n"


def site_block_file() -> Path:
    """The block to apply: the installed copy, or the one uploaded beside a
    scratch run.

    `--dry-run` runs this program from `/tmp/odile-demo-public-dryrun` with
    that run's block beside it; the installed copy may be stale (it was the
    whole Caddyfile on 2026-11-13), so the scratch copy wins when present.
    """
    beside = Path(__file__).resolve().parent / SITE_BLOCK.name
    if beside != SITE_BLOCK and beside.is_file():
        return beside
    return SITE_BLOCK


def ensure_caddy_block(dry: bool) -> bool:
    source_path = site_block_file()
    if not source_path.is_file():
        raise SystemExit(f"error: missing {source_path}")
    source = source_path.read_text(encoding="utf-8")
    existing = CADDYFILE.read_text(encoding="utf-8")
    # The console's `ready: gateway.ready session: …` banner has landed after
    # END twice (2026-11-13, a concurrent writer). Caddy parsed it as site
    # addresses; `splice` alone keeps it, so remove it before the splice.
    existing, residue = strip_console_residue(existing)
    try:
        updated = splice(existing, source, BEGIN, END)
    except ValueError as exc:
        raise SystemExit(f"error: {CADDYFILE}: {exc}")
    lines = updated.splitlines()
    begins = [index for index, line in enumerate(lines) if line.strip() == BEGIN]
    ends = [index for index, line in enumerate(lines) if line.strip() == END]
    if len(begins) != 1 or len(ends) != 1:
        raise SystemExit(f"error: {CADDYFILE}: expected one marker pair after the splice")
    block = "\n".join(lines[begins[0]:ends[0] + 1]) + "\n"
    expected = source.rstrip("\n") + "\n"
    if block != expected:
        raise SystemExit(f"error: {CADDYFILE}: the block bytes are not {SITE_BLOCK}")
    if updated == existing and not residue:
        say("same", f"{CADDYFILE} ({BEGIN} block)")
        return False
    say("would edit" if dry else "edit", f"{CADDYFILE} ({BEGIN} block)"
        + (", console residue removed" if residue else ""))
    if dry:
        return True
    backup = CADDYFILE.with_name(f"{CADDYFILE.name}.bak-{utc_stamp()}")
    shutil.copy2(CADDYFILE, backup)
    say("backup", str(backup))
    write_atomic(CADDYFILE, updated.encode("utf-8"), 0o644, 0, 0)
    check = run(["caddy", "validate", "--config", str(CADDYFILE), "--adapter", "caddyfile"])
    if check.returncode != 0:
        shutil.copy2(backup, CADDYFILE)
        print(f"error: caddy validate failed; restored {backup}\n{check.stderr.strip()}",
              file=sys.stderr)
        raise SystemExit(1)
    say("validate", "caddy validate: ok")
    run(["systemctl", "restart", "caddy"])
    say("restart", "systemctl restart caddy")
    for _ in range(20):
        if run(["systemctl", "is-active", "caddy"]).stdout.strip() == "active":
            return True
        time.sleep(0.5)
    shutil.copy2(backup, CADDYFILE)
    run(["systemctl", "restart", "caddy"])
    print(f"error: caddy did not become active; restored {backup}", file=sys.stderr)
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# 3. the demo's Hermes settings
#
# The config is Hermes's file. This program owns four settings the demo needs
# and nothing else: the model route, the provider's route and key name, and the
# Webapp's public_url. Every other key — the provider catalogue Hermes writes,
# the skin, the owner's own edits (turn and session limits included) — is kept.


TOP_LEVEL = re.compile(r"^([A-Za-z_][\w.-]*):(\s*)(.*)$")


def _blocks(lines: list[str]) -> list[tuple[str | None, int, int]]:
    """(key, start, end) for every top-level block; key None is the preamble."""
    marks: list[tuple[str | None, int]] = [(None, 0)]
    for index, line in enumerate(lines):
        match = TOP_LEVEL.match(line)
        if match and not line.startswith((" ", "\t")):
            marks.append((match.group(1), index))
    spans: list[tuple[str | None, int, int]] = []
    for position, (key, start) in enumerate(marks):
        end = marks[position + 1][1] if position + 1 < len(marks) else len(lines)
        spans.append((key, start, end))
    return spans


def set_child(text: str, block: str, child: str, value: str, *, indent: str = "  ") -> tuple[str, bool]:
    """Set `child` inside top-level `block`, keeping every other line.

    The child may live inside a list item (custom_providers), so the match is
    on the first line whose stripped form starts with `child:` anywhere in the
    block; a missing child is inserted after the block's last non-blank line at
    `indent`.
    """
    lines = text.splitlines()
    for span_key, start, end in _blocks(lines):
        if span_key != block:
            continue
        if _inline_flow(lines[start]) in ("[]", "[ ]", "{}", "{ }"):
            # `key: []` has no children; appending indented lines under it is
            # not YAML (the live-shape bug, 2026-11-13). Callers that may meet
            # this shape own the rewrite (tune_providers does).
            raise ValueError(f"{block}: an inline {_inline_flow(lines[start])} has no child to edit")
        for index in range(start + 1, end):
            stripped = lines[index].strip()
            if stripped.startswith(child + ":"):
                prefix = lines[index][:len(lines[index]) - len(lines[index].lstrip())]
                new_line = f"{prefix}{child}: {value}"
                changed = lines[index] != new_line
                lines[index] = new_line
                return "\n".join(lines).rstrip("\n") + "\n", changed
        insert = end
        while insert > start + 1 and not lines[insert - 1].strip():
            insert -= 1
        lines.insert(insert, f"{indent}{child}: {value}")
        return "\n".join(lines).rstrip("\n") + "\n", True
    # Not present: append the block with the child, rather than fail on a
    # config that lost a managed block.
    return ("\n".join(lines).rstrip("\n")
            + f"\n\n{block}:\n{indent}{child}: {value}\n", True)


def _top_span(lines: list[str], key: str) -> tuple[int, int] | None:
    for span_key, start, end in _blocks(lines):
        if span_key == key:
            return start, end
    return None


def _inline_flow(line: str) -> str:
    match = TOP_LEVEL.match(line)
    return match.group(3).strip() if match else ""


def _children(lines: list[str], start: int, end: int) -> list[tuple[int, int]]:
    """(start, end) of each direct child of a top-level block."""
    first: int | None = None
    spans: list[list[int]] = []
    current: list[int] | None = None
    for index in range(start + 1, end):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        if first is None:
            first = indent
        if indent == first:
            if current is not None:
                spans.append(current)
            current = [index, index]
        elif current is not None:
            current[1] = index
    if current is not None:
        spans.append(current)
    return [(span[0], span[1]) for span in spans]


def _field_line(lines: list[str], start: int, end: int,
                names: tuple[str, ...]) -> tuple[int | None, str]:
    for index in range(start, end + 1):
        stripped = lines[index].strip()
        probe = stripped[2:] if stripped.startswith("- ") else stripped
        for name in names:
            if probe.startswith(name + ":"):
                return index, probe[len(name) + 1:].strip()
    return None, ""


def _field_indent(line: str) -> str:
    return " " * (len(line) - len(line.lstrip(" \t")) + 2)


def _set_field(lines: list[str], start: int, end: int, names: tuple[str, ...],
               value: str, indent: str) -> bool:
    index, _ = _field_line(lines, start, end, names)
    if index is not None:
        prefix = lines[index][:len(lines[index]) - len(lines[index].lstrip())]
        if lines[index].lstrip().startswith("- "):
            prefix += "- "
        new_line = f"{prefix}{names[0]}: {value}"
        changed = lines[index] != new_line
        lines[index] = new_line
        return changed
    insert = end + 1
    while insert > start and not lines[insert - 1].strip():
        insert -= 1
    lines.insert(insert, f"{indent}{names[0]}: {value}")
    return True


def _is_demo_row(lines: list[str], start: int, end: int) -> bool:
    """Whether this provider entry is the row the demo owns (or will own)."""
    blob = "\n".join(lines[start:end + 1])
    return (PROVIDER_NAME in blob or NEAR_BASE_URL in blob
            or NEAR_KEY_ENV in blob)


def provider_entry(text: str) -> tuple[list[str], tuple[int, int] | None, str]:
    """(lines, (start, end), where) of the demo's provider row, or (lines, None, '')."""
    lines = text.splitlines()
    for block in ("custom_providers", "providers"):
        span = _top_span(lines, block)
        if span is None:
            continue
        if block == "custom_providers" and _inline_flow(lines[span[0]]) in ("[]", "[ ]"):
            continue
        entries = _children(lines, *span)
        for entry in entries:
            if _is_demo_row(lines, *entry):
                return lines, entry, block
        if len(entries) == 1:
            return lines, entries[0], block
    return lines, None, ""


def provider_row(text: str) -> dict[str, str]:
    """The row the demo's inference actually uses, as the report prints it."""
    lines, entry, where = provider_entry(text)
    if entry is None:
        return {"where": "", "name": "", "base_url": "", "key_env": ""}
    start, end = entry

    def value(names: tuple[str, ...]) -> str:
        _, raw = _field_line(lines, start, end, names)
        return raw.strip().strip('"').strip("'")

    return {"where": where, "name": value(("name",)),
            "base_url": value(("base_url", "url", "api")),
            "key_env": value(("key_env", "api_key_env"))}


def tune_providers(text: str) -> tuple[str, list[str]]:
    """Own the provider row in both shapes: the list and the `[]` + map.

    The release installer and Hermes's Settings rewrite may store the row
    either as a `custom_providers:` list or as `custom_providers: []` with the
    same endpoint in `providers:`. Both are edited in place, and a missing row
    is written as a proper list — never as indented children under an inline
    `[]`, which is not YAML (the bug this function was re-cut for; measured on
    the live shape 2026-11-13).
    """
    lines, entry, where = provider_entry(text)
    changed: list[str] = []
    if entry is None:
        span = _top_span(lines, "custom_providers")
        if span is not None and _inline_flow(lines[span[0]]) in ("[]", "[ ]"):
            lines[span[0]:span[0] + 1] = [
                "custom_providers:",
                f"  - name: {PROVIDER_NAME}",
                f"    base_url: {NEAR_BASE_URL}",
                f"    key_env: {NEAR_KEY_ENV}",
            ]
            return "\n".join(lines).rstrip("\n") + "\n", ["custom_providers"]
        if span is not None:
            lines[span[0] + 1:span[0] + 1] = [
                f"  - name: {PROVIDER_NAME}",
                f"    base_url: {NEAR_BASE_URL}",
                f"    key_env: {NEAR_KEY_ENV}",
            ]
            return "\n".join(lines).rstrip("\n") + "\n", ["custom_providers"]
        lines += ["", "custom_providers:", f"  - name: {PROVIDER_NAME}",
                  f"    base_url: {NEAR_BASE_URL}", f"    key_env: {NEAR_KEY_ENV}"]
        return "\n".join(lines).rstrip("\n") + "\n", ["custom_providers"]
    start, end = entry
    url_index, _ = _field_line(lines, start, end, ("base_url", "url", "api"))
    url_name = "base_url"
    if url_index is not None:
        probe = lines[url_index].strip()
        probe = probe[2:] if probe.startswith("- ") else probe
        url_name = probe.split(":", 1)[0].strip()
    indent = _field_indent(lines[start])
    if _set_field(lines, start, end, ("name",), PROVIDER_NAME, indent):
        changed.append(f"{where}.name")
    if _set_field(lines, start, end, (url_name,), NEAR_BASE_URL, indent):
        changed.append(f"{where}.{url_name}")
    if _set_field(lines, start, end, ("key_env", "api_key_env"), NEAR_KEY_ENV, indent):
        changed.append(f"{where}.key_env")
    return "\n".join(lines).rstrip("\n") + "\n", changed


def strip_console_residue(text: str) -> tuple[str, bool]:
    """Drop the Webapp console's `ready: …` banner from a file it landed in."""
    lines = text.splitlines()
    kept = [line for line in lines if not CONSOLE_RESIDUE.match(line)]
    return "\n".join(kept).rstrip("\n") + "\n", len(kept) != len(lines)


def _shape_problem(text: str) -> str:
    """The stdlib half of the YAML check: no indented block under `[]`/`{}`."""
    lines = text.splitlines()
    for key, start, end in _blocks(lines):
        if _inline_flow(lines[start]) in ("[]", "[ ]", "{}", "{ }"):
            body = [line for line in lines[start + 1:end]
                    if line.strip() and not line.lstrip().startswith("#")]
            if body:
                return f"{key}: an inline flow value followed by an indented block"
    span = _top_span(lines, "custom_providers")
    if span is not None and _inline_flow(lines[span[0]]) not in ("[]", "[ ]"):
        body = [line for line in lines[span[0] + 1:span[1]]
                if line.strip() and not line.lstrip().startswith("#")]
        if not body or not body[0].lstrip().startswith("- "):
            return "custom_providers: expected a list item"
    return ""


def config_problem(text: str) -> str:
    """Return why *text* is not a config this program may write, or ''.

    PyYAML when the interpreter has it (HQ, the self-tests); otherwise a
    structural check that rejects exactly the corruption this program once
    wrote — indented children under `custom_providers: []` — because a tenant's
    `/usr/bin/python3` has no YAML package (measured 2026-11-13).
    """
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return _shape_problem(text)
    try:
        yaml.safe_load(text)
    except Exception as exc:  # yaml.YAMLError and the scanner's ValueError
        return f"{type(exc).__name__}: {exc}"
    return _shape_problem(text)


def tune_config(text: str) -> tuple[str, list[str]]:
    """Return (config, the settings that changed)."""
    changed: list[str] = []
    text, residue = strip_console_residue(text)
    if residue:
        changed.append("console residue")
    for block, child, value in (
        ("model", "base_url", NEAR_BASE_URL),
        ("model", "provider", "custom"),
        ("webapp", "public_url", f'"{DEMO_PUBLIC_URL}"'),
    ):
        text, edited = set_child(text, block, child, value)
        if edited:
            changed.append(f"{block}.{child}")
    text, provider_changed = tune_providers(text)
    changed.extend(provider_changed)
    return text, changed


def ensure_config(dry: bool) -> bool:
    if not CONFIG_FILE.is_file():
        raise SystemExit(f"error: {CONFIG_FILE} is missing")
    current = CONFIG_FILE.read_text(encoding="utf-8")
    updated, changed = tune_config(current)
    problem = config_problem(updated)
    if problem:
        raise SystemExit(f"error: refusing to write {CONFIG_FILE}: {problem}")
    if updated == current:
        say("same", f"{CONFIG_FILE} (Near AI direct, provider {PROVIDER_NAME!r})")
        return False
    say("would write" if dry else "write",
        f"{CONFIG_FILE} ({', '.join(changed)})")
    if dry:
        return True
    write_atomic(CONFIG_FILE, updated.encode("utf-8"), 0o644, TENANT_UID, TENANT_GID)
    return True


# ---------------------------------------------------------------------------
# 4. the judge account, the one key, and no meter


def hash_password(password: str) -> str:
    """The Hermes dashboard provider's scrypt format, stdlib only."""
    n, r, p, dklen = 2 ** 14, 8, 1, 32
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                             dklen=dklen, maxmem=0)
    return "scrypt${}${}${}${}${}".format(
        n, r, p, base64.b64encode(salt).decode(), base64.b64encode(derived).decode())


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n_s, r_s, p_s, salt_b64, dk_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                n=int(n_s), r=int(r_s), p=int(p_s),
                                dklen=len(expected), maxmem=0)
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(actual, expected)


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value
    return values


def inference_key(current: dict[str, str] | None = None) -> str:
    """The machine's one inference key.

    The machine's own `.env` is authoritative when it already carries the key:
    that is the credential Hermes uses, and on this machine it is where the
    demo's key lives. A machine rebuilt from the release writes the key to the
    installer's root-only store instead (t_e237e261), so that store is read as
    the one fallback; a missing key is an error, never a silent empty value.
    """
    existing = ((current or {}).get(NEAR_KEY_ENV) or "").strip()
    if len(existing) >= 16:
        return existing
    if KEY_STORE.is_file():
        values = parse_env(KEY_STORE.read_text(encoding="utf-8"))
        key = (values.get(NEAR_KEY_ENV) or "").strip()
        if len(key) >= 16:
            return key
        raise SystemExit(f"error: {KEY_STORE} does not hold a usable key")
    raise SystemExit(f"error: {ENV_FILE} has no {NEAR_KEY_ENV} and {KEY_STORE} is missing")


def ensure_env(password: str, *, dry: bool) -> tuple[bool, bool]:
    """Write the tenant .env: judge account + the one Near AI key.

    Returns (changed, hash_changed). The cookie secret is kept byte-for-byte:
    it is what keeps judges signed in across a restart. The file carries
    exactly one inference credential — the Near AI key — and no meter token.
    """
    if not ENV_FILE.is_file():
        raise SystemExit(f"error: {ENV_FILE} is missing; run the release installer first")
    current = ENV_FILE.read_text(encoding="utf-8")
    values = parse_env(current)
    secret = values.get("HERMES_DASHBOARD_BASIC_AUTH_SECRET", "")
    if not secret:
        raise SystemExit(f"error: {ENV_FILE} has no HERMES_DASHBOARD_BASIC_AUTH_SECRET")
    username = values.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", "")
    stored = values.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH", "")
    key = inference_key(values)

    hash_ok = bool(stored) and verify_password(password, stored)
    desired_hash = hash_password(password) if not hash_ok else stored
    if not desired_hash:
        raise SystemExit("error: no sign-in hash and no --password-file")

    lines = [
        f"HERMES_DASHBOARD_BASIC_AUTH_USERNAME=judge",
        f"HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH={desired_hash}",
        f"HERMES_DASHBOARD_BASIC_AUTH_SECRET={secret}",
        f"{NEAR_KEY_ENV}={key}",
    ]
    rendered = "\n".join(lines) + "\n"
    if rendered == current:
        say("same", f"{ENV_FILE} (judge account, one Near AI key, mode 0600)")
        return False, False
    hash_changed = username != "judge" or bool(password and not hash_ok)
    say("would write" if dry else "write",
        f"{ENV_FILE} (judge account, one Near AI key)")
    if dry:
        return True, hash_changed
    write_atomic(ENV_FILE, rendered.encode("utf-8"), 0o600, TENANT_UID, TENANT_GID)
    return True, hash_changed


# ---------------------------------------------------------------------------
# 5. the seeded session


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, created_source TEXT,
    user_id TEXT, session_key TEXT, chat_id TEXT, chat_type TEXT,
    display_name TEXT, origin_json TEXT, model TEXT, model_config TEXT,
    cwd TEXT, started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
    message_count INTEGER DEFAULT 0, last_activity_at REAL, title TEXT,
    title_source TEXT, archived INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0,
    profile_name TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT, timestamp REAL NOT NULL, message_uid TEXT,
    active INTEGER NOT NULL DEFAULT 1, compacted INTEGER NOT NULL DEFAULT 0,
    display_order INTEGER
);
CREATE TABLE IF NOT EXISTS state_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def origin() -> str:
    return json.dumps({
        "platform": "local", "chat_id": SESSION_ID, "chat_name": None,
        "chat_type": "dm", "user_id": PRINCIPAL_ID, "user_name": None,
        "thread_id": None, "chat_topic": None,
    })


def clear_seed_tombstone(con: sqlite3.Connection) -> bool:
    """Lift Hermes's deletion tombstone for the seed, if one is present.

    A judge's delete writes ``gateway.retired_session.v1.<id>`` and the
    gateway refuses a turn on a row with that marker (`not_found`, measured
    2026-11-14). Writing the seed back under its constant id — the one
    ``/judge`` deep-links — must lift it, or the demo's front door lands in a
    session that cannot answer.
    """
    key = RETIRED_PREFIX + SESSION_ID
    row = con.execute("SELECT 1 FROM state_meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return False
    con.execute("DELETE FROM state_meta WHERE key = ?", (key,))
    return True


def insert_seed(con: sqlite3.Connection, stamp: float, text: str) -> bool:
    """Write the seed's row and its two messages, only if the row is absent.

    An existing seed is the owner's from then on: a judge's later turns, a
    renamed title, an archived flag, a different message set are all kept. The
    only rows this function may remove are an orphan message under the seed's
    id on a fresh insert (a message with no session row has no reader) and the
    seed's own deletion tombstone.
    """
    row = con.execute("SELECT 1 FROM sessions WHERE id = ?", (SESSION_ID,)).fetchone()
    if row is not None:
        return False
    clear_seed_tombstone(con)
    con.execute(
        "INSERT INTO sessions (id, source, created_source, user_id, session_key,"
        " chat_id, chat_type, origin_json, model, model_config, cwd, started_at,"
        " last_activity_at, message_count, title, title_source, pinned, hidden,"
        " archived, profile_name) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        # source/created_source are the Webapp's own values (a judge's
        # session is source 'gui', created_source 'desktop'); a row seeded
        # as 'local' is listed but its first turn is refused
        # (permission_denied) because the turn's surface has no route to it.
        (SESSION_ID, "gui", "desktop", PRINCIPAL_ID,
         "agent:main:local:dm:" + SESSION_ID, SESSION_ID, "dm", origin(),
         SESSION_MODEL, json.dumps({"seeded": True}), str(WORKSPACE), stamp,
         stamp, 2, SESSION_TITLE, "seed", 1, 0, 0, "default"))
    con.execute("DELETE FROM messages WHERE session_id = ?", (SESSION_ID,))
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, message_uid,"
        " active, compacted, display_order) VALUES (?,?,?,?,?,?,?,?)",
        (SESSION_ID, "user", SESSION_PROMPT, stamp - 1, secrets.token_hex(16), 1, 0, 1))
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, message_uid,"
        " active, compacted, display_order) VALUES (?,?,?,?,?,?,?,?)",
        (SESSION_ID, "assistant", text, stamp, secrets.token_hex(16), 1, 0, 2))
    return True


def receipt_template() -> Path:
    """The installed template, or the repository copy when run from the tree."""
    installed = LIB / "session-receipt.json"
    if installed.is_file():
        return installed
    return (Path(__file__).resolve().parent
            / "seed" / "session-receipt.json")


def rendered_receipt(stamp: float) -> dict:
    """The receipt document for the seeded session, minted for *this* machine.

    The reviewed template (`deploy/demo-host/seed/session-receipt.json`) is
    uploaded beside this program; only the ids, the timestamp and the machine's
    own route change, so the row is a real receipt from the same Hermes release.
    """
    template = receipt_template().read_text(encoding="utf-8")
    created = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    rendered = (template
                .replace("{{SESSION_ID}}", SESSION_ID)
                .replace("{{PROFILE_ID}}", PROFILE_ID)
                .replace("{{USER_ID}}", PRINCIPAL_ID)
                .replace("{{STAMP}}", created)
                .replace("{{REQUEST_ID}}", REQUEST_ID))
    # The template was written for the retired HQ demo (a loopback provider
    # and its own workspace path). The identity above must stay the frozen
    # one, but the policy's working directory must be this machine's, or the
    # session resumes into a path that does not exist here.
    receipt_doc = json.loads(rendered)
    # The session id is the HQ demo's constant (the one app.py's /judge deep
    # links), but this receipt is minted by *this* machine's profile: the
    # tenant's revision of gateway/session_local_recovery.owned_receipt refuses
    # a receipt whose profile_id is another home (profile_mismatch). A
    # `legacy_session_id` binds the receipt by its route instead of re-hashing
    # the identity, which is exactly the legacy-adopted shape the same function
    # documents — so the echoed id, the route and the row stay the HQ constant.
    receipt_doc["legacy_session_id"] = SESSION_ID
    policy = receipt_doc["policy"]
    policy["cwd"] = str(WORKSPACE)
    request = json.loads(policy["request_json"])
    request["cwd"] = str(WORKSPACE)
    policy["request_json"] = json.dumps(request)
    terminal = json.loads(policy["terminal_json"])
    terminal["TERMINAL_CWD"] = str(WORKSPACE)
    policy["terminal_json"] = json.dumps(terminal)
    # The receipt also freezes the whole config it was minted under. The HQ
    # template's copy routes inference through a loopback URL with the HQ
    # credential's name; resumed as-is, the seeded session's first turn calls
    # a port this machine does not run and fails with an `APIConnectionError`
    # (measured 2026-11-13). Re-point the snapshot at the route this program
    # owns. The template carries no turn or session limit and neither does the
    # snapshot: the demo sets no cap a real tenant would not have (t_ee5aac66).
    config = json.loads(policy["config_json"])
    model = config.setdefault("model", {})
    model["base_url"] = NEAR_BASE_URL
    model["provider"] = "custom"
    for provider in config.get("custom_providers") or []:
        if isinstance(provider, dict):
            provider["name"] = PROVIDER_NAME
            provider["base_url"] = NEAR_BASE_URL
            provider["key_env"] = NEAR_KEY_ENV
    providers_map = config.get("providers")
    if isinstance(providers_map, dict):
        for provider in providers_map.values():
            if isinstance(provider, dict):
                provider["name"] = PROVIDER_NAME
                provider["base_url"] = NEAR_BASE_URL
                if "api" in provider:
                    provider["api"] = NEAR_BASE_URL
                provider["key_env"] = NEAR_KEY_ENV
    webapp = config.get("webapp")
    if isinstance(webapp, dict):
        webapp["public_url"] = DEMO_PUBLIC_URL
    policy["config_json"] = json.dumps(config, separators=(",", ":"))
    return receipt_doc


def receipt(con: sqlite3.Connection, stamp: float) -> bool:
    """Write the gateway's local-policy receipt; without it the Webapp says "Resume failed".

    Written only when the row is absent: the receipt belongs to the seed, and
    an existing one — even one a judge's turn has since made stale — is not
    rewritten (t_ee5aac66). This function is only called on the fresh-insert
    path.
    """
    key = RECEIPT_PREFIX + SESSION_ID
    value = json.dumps(rendered_receipt(stamp), separators=(",", ":"))
    changed = False
    row = con.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        con.execute("INSERT INTO state_meta (key, value) VALUES (?, ?)", (key, value))
        changed = True
    legacy_key = LEGACY_PREFIX + SESSION_ID
    legacy_value = json.dumps({"session_id": SESSION_ID, "profile_id": PROFILE_ID,
                               "principal_id": PRINCIPAL_ID,
                               "legacy_session_id": SESSION_ID}, separators=(",", ":"))
    legacy = con.execute("SELECT value FROM state_meta WHERE key = ?", (legacy_key,)).fetchone()
    if legacy is None:
        con.execute("INSERT INTO state_meta (key, value) VALUES (?, ?)",
                    (legacy_key, legacy_value))
        changed = True
    return changed


def ensure_session(dry: bool) -> bool:
    """Write the seed once; leave an existing one exactly as it stands.

    The tenant starts as the demo, so the first apply writes the seeded
    session. Every later apply — including the boot after a reboot — finds the
    row and changes nothing: not its title, not its pinned/hidden/archived
    flags, not its messages (a judge's turns append to them) and not its
    receipt.
    """
    if not START_HERE.is_file():
        raise SystemExit(f"error: missing {START_HERE}")
    text = START_HERE.read_text(encoding="utf-8").strip() + "\n"
    if not STATE_DB.is_file():
        raise SystemExit(f"error: {STATE_DB} is missing; start the Webapp once first")
    con = sqlite3.connect(str(STATE_DB), timeout=10)
    try:
        con.executescript(SCHEMA)
        row = con.execute("SELECT started_at FROM sessions WHERE id = ?",
                          (SESSION_ID,)).fetchone()
        if row is not None:
            # The row is the owner's, but a tombstone from a delete-then-
            # re-seed would make it a session that cannot answer: lift it.
            if clear_seed_tombstone(con):
                if dry:
                    con.rollback()
                else:
                    con.commit()
                say("would clear" if dry else "clear",
                    f"{STATE_DB} (the seed's retirement marker; the row and messages are kept)")
                return True
            con.rollback()
            say("same", f"{STATE_DB} (seeded session {SESSION_ID[:20]}…)")
            return False
        stamp = datetime.now(timezone.utc).timestamp()
        insert_seed(con, stamp, text)
        receipt(con, stamp)
        if dry:
            con.rollback()
        else:
            con.commit()
    finally:
        con.close()
    verb = "would write" if dry else "write"
    say(verb, f"{STATE_DB} (the seeded session {SESSION_ID[:20]}…)")
    if not dry:
        os.chown(STATE_DB, TENANT_UID, TENANT_GID)
        os.chmod(STATE_DB, 0o600)
    return True


# ---------------------------------------------------------------------------
# 6. the removed boot re-assert, and the restart helper
#
# Revisions before t_ee5aac66 installed a oneshot unit that re-ran this
# program with its old tune flags after `odile-release-setup.service` on every
# boot, to undo the release installer's config rewrite. The owner's config must
# survive a reboot on its own (the release setup now converges once), so the
# unit is removed here — any later deploy converges a machine that still has
# it. Nothing re-asserts anything at boot.

def remove_boot_unit(dry: bool) -> bool:
    """Delete the old boot re-assert unit: stop it, disable it, remove, reload.

    Stopping first matters: a unit that has already run is `active (exited)`,
    and without a stop the machine reports `Loaded: not-found` with a stale
    `Active: active (exited)` until someone stops it by hand (measured
    2026-11-14).
    """
    enabled = run(["systemctl", "is-enabled", DEMO_UNIT]).stdout.strip()
    active = run(["systemctl", "is-active", DEMO_UNIT]).stdout.strip()
    present = UNIT_TARGET.is_file() or UNIT_TARGET.is_symlink()
    if (not present and enabled not in ("enabled", "enabled-runtime", "static")
            and active not in ("active", "activating", "reloading")):
        say("same", f"{DEMO_UNIT} (no boot unit)")
        return False
    say("would stop, disable and remove" if dry else "stop, disable and remove",
        f"{DEMO_UNIT} (no boot re-assert; active={active or '-'})")
    if not dry:
        if active in ("active", "activating", "reloading"):
            run(["systemctl", "stop", DEMO_UNIT])
        run(["systemctl", "disable", DEMO_UNIT])
        UNIT_TARGET.unlink(missing_ok=True)
        # The executed unit keeps its loaded state until the manager forgets it;
        # `reset-failed` is what lets `status` say "could not be found".
        run(["systemctl", "reset-failed", DEMO_UNIT])
        run(["systemctl", "daemon-reload"])
    return True


RESTART_HELPER = """#!/bin/sh
# Installed by deploy/demo-host/apply_demo_public.py (ODL-503).
# The Webapp's shell.exec refuses any command line carrying `systemctl
# restart`, and the deploy needs one restart of the unit whose socket it is
# talking over. This script is that restart, named so the guard lets it pass.
# Nothing else comes back with it: the product ships no meter unit
# (removed at 0a4b726), so no Wants= can start one.
systemctl restart odile-agent
"""


def ensure_restart_helper(dry: bool) -> bool:
    target = LIB / "restart-agent"
    desired = RESTART_HELPER.encode("utf-8")
    if target.is_file() and target.read_bytes() == desired:
        say("same", f"{target} (the restart helper)")
        return False
    say("would write" if dry else "write", f"{target} (the restart helper)")
    if not dry:
        write_atomic(target, desired, 0o755, 0, 0)
    return True


# ---------------------------------------------------------------------------
# 7. the local gate


def gate_codes() -> dict[str, str]:
    """Ask the Webapp what it answers, over loopback."""
    codes: dict[str, str] = {}

    def code(method: str, path: str) -> str:
        request = urllib.request.Request(HEALTH.rstrip("/") + path, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return str(response.status)
        except urllib.error.HTTPError as exc:
            return str(exc.code)
        except OSError:
            return "000"

    codes["anonymous"] = code("GET", "/")
    codes["api"] = code("GET", "/api/sessions")
    codes["verify"] = code("GET", "/verify")
    return codes


def judge_sign_in(password: str) -> str:
    """Sign in as judge over loopback; return the status as a string."""
    body = json.dumps({"provider": "basic", "username": "judge",
                       "password": password}).encode()
    request = urllib.request.Request(HEALTH.rstrip("/") + "/auth/password-login",
                                     data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return str(response.status)
    except urllib.error.HTTPError as exc:
        return str(exc.code)
    except OSError:
        return "000"


def restart_agent(dry: bool) -> None:
    if dry:
        return
    run(["systemctl", "restart", "odile-agent"])
    say("restart", "systemctl restart odile-agent")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        codes = gate_codes()
        if codes["anonymous"] in ("302", "200"):
            say("verify", f"gate after restart: / -> {codes['anonymous']}, "
                          f"/api/sessions -> {codes['api']}, /verify -> {codes['verify']}")
            return
        time.sleep(1)
    raise SystemExit("error: the Webapp did not answer after restarting odile-agent")


# ---------------------------------------------------------------------------
# the report the check script reads


def matchers(block: str) -> list[str]:
    """Every named matcher the block defines, in file order.

    Named so a reader can see the live shape: the demo's own `@tabicons`
    static route (t_6b25f6e5) and `@apidocs` 404 are the only names left, and
    the freeze's deny-by-default matcher is gone (t_69f1a5f3). Counting names
    rather than grepping one literal also catches a matcher reintroduced under
    different whitespace.
    """
    return re.findall(r"^\s*@([A-Za-z_][\w-]*)", block, re.M)


def caddy_state() -> dict:
    """The committed block, the live block and the installed copy, by hash."""
    installed = SITE_BLOCK.read_bytes() if SITE_BLOCK.is_file() else b""
    live = CADDYFILE.read_text(encoding="utf-8") if CADDYFILE.is_file() else ""
    lines = live.splitlines()
    begins = [index for index, line in enumerate(lines) if line.strip() == BEGIN]
    ends = [index for index, line in enumerate(lines) if line.strip() == END]
    block = ""
    residue = 0
    if len(begins) == 1 and len(ends) == 1 and ends[0] > begins[0]:
        block = "\n".join(lines[begins[0]:ends[0] + 1]) + "\n"
        residue = sum(1 for line in lines[ends[0] + 1:]
                      if CONSOLE_RESIDUE.match(line.strip()))
    return {
        "installed_sha256": hashlib.sha256(installed).hexdigest(),
        "live_block_sha256": (hashlib.sha256(block.encode("utf-8")).hexdigest()
                              if block else ""),
        "markers": [len(begins), len(ends)],
        "matchers": matchers(block),
        "residue_lines": residue,
        "active": run(["systemctl", "is-active", "caddy"]).stdout.strip(),
    }


def session_digest(con: sqlite3.Connection, session_id: str) -> dict:
    """One session's row and a digest of every message it holds.

    The check compares this across a deploy: the row (title, flags, counts,
    times) and the ordered message rows must be byte-identical, or the deploy
    changed a session it does not own.
    """
    row = con.execute(
        "SELECT id, title, message_count, pinned, hidden, archived, started_at,"
        " last_activity_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return {"id": session_id, "exists": False}
    digest = hashlib.sha256()
    for message in con.execute(
            "SELECT role, content, timestamp, active, compacted, display_order"
            " FROM messages WHERE session_id = ? ORDER BY display_order, id",
            (session_id,)):
        digest.update("\x1f".join("" if value is None else str(value)
                                 for value in message).encode("utf-8"))
        digest.update(b"\x1e")
    return {"id": row[0], "exists": True, "title": row[1], "messages": row[2],
            "pinned": row[3], "hidden": row[4], "archived": row[5],
            "started_at": row[6], "last_activity_at": row[7],
            "messages_sha256": digest.hexdigest()}


def max_iterations_lines() -> list[str]:
    """The agent's own turn-cap exits, newest last.

    ``Turn ended: reason=max_iterations_reached(25/25) …`` is logged at INFO
    to ``agent.log``. The unit's journal handler is WARNING+, so a
    ``journalctl`` scan alone finds nothing (measured 2026-11-14 on the demo
    tenant); read the last lines of the agent log too.
    """
    lines: list[str] = []
    journal = run(["journalctl", "-u", "odile-agent", "--since", "-90 min", "--no-pager"])
    lines.extend(journal.stdout.splitlines())
    if AGENT_LOG.is_file():
        with AGENT_LOG.open(encoding="utf-8", errors="replace") as handle:
            lines.extend(deque(handle, maxlen=AGENT_LOG_TAIL))
    return [line.strip() for line in lines if "reason=max_iterations_reached(" in line]


def report(session_id: str = "") -> int:
    """Print the demo's tenant-side state as one JSON object.

    Read-only: the config's managed settings and the provider row it actually
    uses, the .env's key names, the seeded session's row, the last runs' turn
    counts, the agent's own `max_iterations_reached` lines (agent log and
    journal), the number of session rows, and the Caddy block's hashes
    (committed copy, live block, residue). The check script asserts on this.

    With `session_id`, print that one session's row and message digest instead
    (`--session-id`): the report stays under the console's 4 KB reply for the
    full state, and the check can compare any number of sessions one at a time.
    """
    if session_id:
        if not STATE_DB.is_file():
            raise SystemExit(f"error: {STATE_DB} is missing")
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
        try:
            print(json.dumps(session_digest(con, session_id), separators=(",", ":")))
        finally:
            con.close()
        return 0
    config = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.is_file() else ""
    env_keys = (sorted(parse_env(ENV_FILE.read_text(encoding="utf-8")).keys())
                if ENV_FILE.is_file() else [])
    session = None
    runs: list[dict] = []
    session_count = 0
    if STATE_DB.is_file():
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
        try:
            session_count = con.execute("SELECT count(*) FROM sessions").fetchone()[0]
            row = con.execute(
                "SELECT id, title, pinned, message_count FROM sessions WHERE id = ?",
                (SESSION_ID,)).fetchone()
            session = {"id": row[0], "title": row[1], "pinned": row[2],
                       "messages": row[3]} if row else None
            for record in con.execute(
                    "SELECT id, title, api_call_count, tool_call_count, message_count"
                    " FROM sessions WHERE message_count > 0"
                    " ORDER BY started_at DESC LIMIT 6"):
                runs.append({"id": record[0], "title": record[1], "api_calls": record[2],
                             "tool_calls": record[3], "messages": record[4]})
        finally:
            con.close()
    exits = max_iterations_lines()
    print(json.dumps({
        "config": {
            "near_ai_direct": f"base_url: {NEAR_BASE_URL}" in config,
            "public_url": f'public_url: "{DEMO_PUBLIC_URL}"' in config,
        },
        "provider": provider_row(config),
        "env_keys": env_keys,
        "verify": verify_state(),
        "icons": {
            name: (hashlib.sha256((SITE_ROOT / name).read_bytes()).hexdigest()
                   if (SITE_ROOT / name).is_file() else None)
            for name, _pin, _content_type in ICONS
        },
        "caddy": caddy_state(),
        "session": session,
        "session_count": session_count,
        "runs": runs,
        "max_iterations_exits": exits[-3:],
    }, separators=(",", ":")))
    return 0


# ---------------------------------------------------------------------------
# self-test


def _source(name: str) -> Path:
    """The uploaded copy under LIB, or the repository copy (next to or above)."""
    installed = LIB / name
    if installed.is_file():
        return installed
    beside = Path(__file__).resolve().parent / name
    if beside.is_file():
        return beside
    return Path(__file__).resolve().parent.parent / "caddy" / name


def self_test() -> int:
    failures = 0

    def check(ok: bool, label: str) -> None:
        nonlocal failures
        print(f"{'ok  ' if ok else 'FAIL'} {label}")
        failures += 0 if ok else 1

    block = _source(SITE_BLOCK.name).read_text(encoding="utf-8")
    names = matchers(block)
    check(not {"mutation", "update"} & set(names),
          f"the committed block has no mutation or self-update matcher: {names}")
    base = "# the vendor block\n:80 {\n\tfile_server\n}\n"
    once = splice(base, block, BEGIN, END)
    twice = splice(once, block, BEGIN, END)
    check(once == twice, "splicing the block twice is a no-op")
    check(once.count(BEGIN) == 1 and once.count(END) == 1, "one marker pair after a splice")
    check(base.rstrip("\n") in once, "the existing Caddyfile is kept")
    try:
        splice(once + block, block, BEGIN, END)
    except ValueError:
        check(True, "a duplicated marker pair is refused")
    else:
        check(False, "a duplicated marker pair is refused")
    dirty = once + "ready: gateway.ready  session: local-" + "a" * 64 + "\n"
    clean, had_residue = strip_console_residue(dirty)
    check(had_residue and clean == once, "the console banner after END is removed")

    sample = (
        "_config_version: 52\n"
        "model:\n"
        "  default: \"m\"\n"
        "  provider: \"custom\"\n"
        "  base_url: \"http://127.0.0.1:8099/v1\"\n"
        "  extra_key: kept\n"
        "agent:\n"
        "  reasoning_effort: \"high\"\n"
        "custom_providers:\n"
        "  - name: Old Provider\n"
        "    base_url: http://127.0.0.1:8099/v1\n"
        "    key_env: OLD_PROVIDER_API_KEY\n"
        "    model: m\n"
        "webapp:\n"
        "  port: 9130\n"
    )
    tuned, changed = tune_config(sample)
    check("base_url: https://cloud-api.near.ai/v1" in tuned, "the model route moves to Near AI")
    check("key_env: HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY" in tuned,
          "the provider names the one key")
    check("name: Near AI" in tuned,
          "the provider row is renamed to Near AI")
    bare = ("model:\n  provider: custom\n"
            "agent:\n  reasoning_effort: high\n"
            "custom_providers:\n  - name: Old Provider\n"
            "    base_url: http://127.0.0.1:8099/v1\n"
            "    key_env: OLD_PROVIDER_API_KEY\n"
            "webapp:\n  port: 9130\n")
    tuned_bare, _ = tune_config(bare)

    def agent_block(text: str) -> set[str]:
        lines = text.splitlines()
        span = _top_span(lines, "agent")
        return {line.strip() for line in lines[span[0] + 1:span[1]]} if span else set()

    def top_keys(text: str) -> set[str]:
        return {line.split(":")[0] for line in text.splitlines()
                if line and not line.startswith((" ", "\t", "#")) and ":" in line}

    check(agent_block(tuned_bare) == agent_block(bare)
          and top_keys(tuned_bare) == top_keys(bare),
          "a config without limits gains none (the demo sets no cap of its own)")
    check('public_url: "https://demo.odile.fyi"' in tuned,
          "the Webapp's public_url is the demo host (the Host-header gate)")
    check("extra_key: kept" in tuned and "port: 9130" in tuned,
          "keys this program does not own are kept")
    check(set(changed) == {"model.base_url", "model.provider", "custom_providers.name",
                           "custom_providers.base_url", "custom_providers.key_env",
                           "webapp.public_url"},
          "every managed setting is reported once")
    check(config_problem(tuned) == "",
          "the tuned installer shape parses, with no inline-block corruption")
    again, changed_again = tune_config(tuned)
    check(again == tuned and changed_again == [], "a tuned config is converged")

    owner = ("_config_version: 52\nmodel:\n  provider: custom\n"
             "  base_url: https://cloud-api.near.ai/v1\n"
             "agent:\n  reasoning_effort: low\n"
             "custom_providers:\n  - name: Near AI\n"
             "    base_url: https://cloud-api.near.ai/v1\n"
             "    key_env: HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY\n"
             "webapp:\n  public_url: \"https://demo.odile.fyi\"\n"
             "max_agents: 4\n")
    kept, kept_changed = tune_config(owner)
    check(kept == owner and kept_changed == [],
          "a config the owner tuned (its own keys included) is left byte-identical")


    normalised = (
        "_config_version: 52\n"
        "model:\n"
        "  provider: custom\n"
        "  base_url: http://127.0.0.1:8099/v1\n"
        "custom_providers: []\n"
        "providers:\n"
        "  old-provider:\n"
        "    api: http://127.0.0.1:8099/v1\n"
        "    name: Old Provider\n"
        "webapp:\n"
        "  port: 9130\n"
    )
    tuned_normalised, changed_normalised = tune_config(normalised)
    check("custom_providers: []" in tuned_normalised,
          "a normalised config keeps its empty custom_providers list")
    check("name: Near AI" in tuned_normalised
          and "api: https://cloud-api.near.ai/v1" in tuned_normalised,
          "the providers map row is renamed and re-pointed")
    check(config_problem(tuned_normalised) == "",
          "the tuned normalised shape parses (no indented children under [])")
    row = provider_row(tuned_normalised)
    check(row["where"] == "providers" and row["name"] == PROVIDER_NAME
          and row["base_url"] == NEAR_BASE_URL and row["key_env"] == NEAR_KEY_ENV,
          "the report names the row the machine actually uses")
    check({"providers.name", "providers.api", "providers.key_env"}
          <= set(changed_normalised),
          "every normalised-shape edit is reported")
    again_normalised, changed_normalised_again = tune_config(tuned_normalised)
    check(again_normalised == tuned_normalised and changed_normalised_again == [],
          "a tuned normalised config is converged")

    broken = ("_config_version: 1\n"
              "custom_providers: []\n"
              "    base_url: http://127.0.0.1:8099/v1\n")
    check(bool(config_problem(broken)),
          "children under an inline [] are refused before a write")

    hashed = hash_password("odile-demo-kestrel-2026")
    check(hashed.startswith("scrypt$") and hashed.count("$") == 5,
          "hash format scrypt$n$r$p$salt$dk")
    check(verify_password("odile-demo-kestrel-2026", hashed), "hash verifies its password")
    check(not verify_password("odile-demo-kestrel-2027", hashed), "hash rejects a wrong password")

    seeded = rendered_receipt(0.0)
    frozen = json.loads(seeded["policy"]["config_json"])
    check(frozen["model"]["base_url"] == NEAR_BASE_URL
          and all(row.get("name") == PROVIDER_NAME
                  and row.get("base_url") == NEAR_BASE_URL
                  and row.get("key_env") == NEAR_KEY_ENV
                  for row in frozen.get("custom_providers") or []),
          "the seed's receipt names the Near AI row and routes inference there, not a loopback")
    check("127.0.0.1:" not in json.dumps(seeded),
          "no loopback URL survives in the seed's receipt")
    template_config = json.loads(json.loads(
        receipt_template().read_text(encoding="utf-8"))["policy"]["config_json"])
    check(set(frozen) == set(template_config)
          and set(frozen.get("agent") or {}) == set(template_config.get("agent") or {}),
          "the seed's receipt snapshot adds no key (no cap the template did not carry)")
    check(seeded["policy"]["cwd"] == str(WORKSPACE), "the seed's receipt works in this machine's home")

    con = sqlite3.connect(":memory:")
    try:
        con.executescript(SCHEMA)
        check(insert_seed(con, 1000.0, "Welcome.\n") and receipt(con, 1000.0),
              "a fresh state.db gets the seed's row, two messages and its receipt")
        check(not insert_seed(con, 2000.0, "Welcome.\n"),
              "an existing seed row is never rewritten")
        check(con.execute("SELECT count(*) FROM messages WHERE session_id = ?",
                          (SESSION_ID,)).fetchone()[0] == 2,
              "the seed keeps exactly its two messages")
        con.execute("UPDATE sessions SET title = 'a judge renamed this', pinned = 0,"
                    " archived = 1, hidden = 0, message_count = 5 WHERE id = ?",
                    (SESSION_ID,))
        con.execute("INSERT INTO messages (session_id, role, content, timestamp,"
                    " message_uid, active, compacted, display_order)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (SESSION_ID, "assistant", "a later turn", 1500.0, "uid", 1, 0, 3))
        receipt_before = con.execute("SELECT value FROM state_meta WHERE key = ?",
                                     (RECEIPT_PREFIX + SESSION_ID,)).fetchone()[0]
        check(not insert_seed(con, 3000.0, "Welcome.\n") and not receipt(con, 3000.0),
              "a second apply writes neither the row nor the receipt")
        row = con.execute("SELECT title, pinned, archived, message_count FROM sessions"
                          " WHERE id = ?", (SESSION_ID,)).fetchone()
        check(row == ("a judge renamed this", 0, 1, 5),
              "the seed's title, flags and count are the owner's")
        check(con.execute("SELECT count(*) FROM messages WHERE session_id = ?",
                          (SESSION_ID,)).fetchone()[0] == 3,
              "a judge's later message survives")
        check(con.execute("SELECT value FROM state_meta WHERE key = ?",
                          (RECEIPT_PREFIX + SESSION_ID,)).fetchone()[0] == receipt_before,
              "the seed's receipt is byte-unchanged")
        tombstone = RETIRED_PREFIX + SESSION_ID
        con.execute("INSERT INTO state_meta (key, value) VALUES (?, ?)",
                    (tombstone, '{"retired_at": 1, "own": true}'))
        check(clear_seed_tombstone(con) and not clear_seed_tombstone(con),
              "the seed's deletion tombstone is lifted once")
        con.execute("DELETE FROM messages WHERE session_id = ?", (SESSION_ID,))
        con.execute("DELETE FROM sessions WHERE id = ?", (SESSION_ID,))
        con.execute("INSERT INTO state_meta (key, value) VALUES (?, ?)",
                    (tombstone, '{"retired_at": 2, "own": true}'))
        check(insert_seed(con, 4000.0, "Welcome.\n")
              and con.execute("SELECT count(*) FROM state_meta WHERE key = ?",
                              (tombstone,)).fetchone()[0] == 0,
              "a re-created seed carries no deletion tombstone")
    finally:
        con.close()

    page = _source(LOGIN_SOURCE.name).read_text(encoding="utf-8")
    check(all(marker in page for marker in LOGIN_MARKERS), "the sign-in page carries every marker")

    lede = _source(VERIFY_LEDE.name).read_text(encoding="utf-8")
    check(lede_problem(lede) == "", "the demo lede is one injectable paragraph")
    check("boot key" in lede and "judge" in lede.lower()
          and "No one at Odile can log in" not in lede,
          "the demo lede names the boot key and the shared judge sign-in, never the customer row")
    command = _source(VERIFY_COMMAND.name).read_text(encoding="utf-8")
    check(command_problem(command) == "", "the demo command is one injectable section")
    check(command.count("{{MEASUREMENT}}") == 1 and PAGE_COMMAND in command
          and "--expect" in command,
          "the demo command carries the --expect line and one live measurement slot")
    release = "<!--tenant-->\n<p class=\"lede\">No one at Odile can log in to a tenant machine.</p>\n<!--/tenant-->\n"
    check(COMMAND_BEGIN not in release, "the release tenant block carries no demo command")
    demo = inject_lede(release, lede)
    check(tenant_block(demo).strip() == lede.strip()
          and "No one at Odile can log in" not in demo
          and inject_lede(demo, lede) == demo,
          "the injection replaces the tenant lede once and is idempotent")
    with_command = inject_command(demo, command)
    block = tenant_block(with_command)
    check(block.strip().startswith(lede.strip()) and PAGE_COMMAND in block,
          "the command lands in the tenant block after the lede")
    check(inject_command(with_command, command) == with_command
          and with_command.count(COMMAND_BEGIN) == 1,
          "the command injection is idempotent")
    check(bool(command_problem(command.replace("--expect {{MEASUREMENT}}", ""))),
          "a command without --expect is refused")

    check(len(SESSION_ID) == 70 and SESSION_ID.startswith("local-"),
          "the seeded id is app.py's 64-hex session id")
    return 0 if failures == 0 else 1


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply the demo.odile.fyi surface on the tenant.")
    parser.add_argument("--write", action="store_true",
                        help="write the changes (default: dry run; --apply is read as sudo's askpass flag)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--no-login", action="store_true",
                        help="keep the live sign-in page (installed by a newer card)")
    parser.add_argument("--no-restart", action="store_true",
                        help="write everything but leave odile-agent running")
    parser.add_argument("--check", action="store_true", help="read the gate and exit")
    parser.add_argument("--report", action="store_true",
                        help="print the tenant-side state as JSON and exit")
    parser.add_argument("--session-id", default="",
                        help="with --report: print one session's row and message digest")
    parser.add_argument("--restart-agent", action="store_true",
                        help="restart odile-agent and wait for the gate (the persistence check)")
    parser.add_argument("--self-test", action="store_true", help="run the pure-function tests")
    parser.add_argument("--password-file", default="", help="file holding the published password")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    if args.report:
        return report(args.session_id)

    if args.restart_agent:
        restart_agent(dry=False)
        return 0

    if args.check:
        codes = gate_codes()
        print(f"gate: / -> {codes['anonymous']}, /api/sessions -> {codes['api']}, "
              f"/verify -> {codes['verify']}")
        return 0

    password = os.environ.get("ODL_DEMO_PASSWORD", "")
    if args.password_file:
        password = Path(args.password_file).read_text(encoding="utf-8").strip()
    if not password:
        print("error: give --password-file (or ODL_DEMO_PASSWORD)", file=sys.stderr)
        return 2
    if len(password) < 16:
        print("error: the demo password needs at least 16 characters", file=sys.stderr)
        return 2

    dry = not args.write or args.dry_run
    changed = ensure_verify_lede(dry)
    changed |= ensure_verify_command(dry)
    if not args.no_login:
        changed |= ensure_login_page(dry)
    changed |= ensure_icons(dry)
    changed |= ensure_caddy_block(dry)
    config_changed = ensure_config(dry)
    env_changed, hash_changed = ensure_env(password, dry=dry)
    changed |= config_changed or env_changed
    changed |= ensure_session(dry)
    changed |= remove_boot_unit(dry)
    changed |= ensure_restart_helper(dry)
    if dry:
        say("dry run", "nothing written, caddy and odile-agent untouched")
        return 0
    if (config_changed or env_changed) and not args.no_restart:
        restart_agent(dry)
    say("verify", f"the judge sign-in over loopback -> {judge_sign_in(password)}")
    codes = gate_codes()
    say("verify", f"/ -> {codes['anonymous']}, /api/sessions -> {codes['api']}, "
                  f"/verify -> {codes['verify']}")
    say("demo", "provisioned" if changed else "converged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
