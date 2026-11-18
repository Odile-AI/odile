#!/usr/bin/python3
"""Set a Blackbox's boot image through the reseller API — and log what it replaced.

`POST ?action=boot-image&id=<server> {"url": …, "sha256": …}` is one request
that changes what the machine starts next. It is not in the action list the
API's own 404 prints, and it was exercised from a shell on 2026-11-12 with
nothing at Odile recording it (ADR-image-vs-installer §15.1). This tool is that
write with a fence and a record:

    ./tools/servury_boot_image.py --show --server 0badc0de
    ./tools/servury_boot_image.py --set --server 0badc0de \\
        --url https://odile.fyi/releases/m1/odile-tenant.efi --sha256 5cb31808…

A `--set` is a **dry run** unless `--apply` is given, and `--apply` also needs
`--confirm`, because a wrong pair breaks the next start (the same two-flag rule
the panel tool uses for a disk write). Every `--set` attempt — dry run
included — appends exactly one line to `/root/.odile/servury-reseller.log`:

    <ISO8601Z> BOOT_IMAGE server=… url=… sha256=… apply=0|1 who=<user>/<uid>
               replaced_url=… replaced_sha256=… replaced_added=…

The replaced triple is read with a GET **before** the POST, and it is the point
of the line: it is the only way to answer "what did the machine carry before".
A machine on the Servury release has no custom image, and the triple reads
`none`; a GET that fails leaves `unknown` and the write does not happen — an
attempt whose predecessor is unknown is not a record. The line is written
before the POST and the log is created 0600, so an unwritable log aborts the
write instead of silently losing it.

The fence is the panel tool's, narrowed to this one power: one server id is
allowed by default (`0badc0de`, the production tenant this record was opened
for), anything else needs `--allow-server`, and the key is read from
`/root/.odile/servury_reseller_key` — never from argv, which `ps` shows to
every user on the box. The key is never printed or logged.

`tools/servury_panel.py` keeps the **disk** write: `?action=disk-image` is
still 404, so that path is panel-only, and this tool does not touch it.

Exit: 0 done or dry run; 1 the API refused or the state could not be read;
2 usage, the allow-list, or a missing key.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pwd
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://servury.com/api/reseller.php"
KEY_FILE = Path("/root/.odile/servury_reseller_key")
LOG = Path("/root/.odile/servury-reseller.log")
# The one id this tool may touch, written out rather than read from a file: the
# record exists for this tenant, and a fresh clone must be able to name it.
# --allow-server widens the fence deliberately, the way the panel tool does.
ALLOWED_SERVERS = frozenset({"0badc0de"})
SHA256 = re.compile(r"[0-9a-fA-F]{64}", re.ASCII)
UNKNOWN = "unknown"
NONE = "none"


def datetime_stamp() -> str:
    """UTC to the second, the format the other Odile logs use."""
    return datetime.datetime.now(datetime.UTC).strftime("%FT%TZ")


def who() -> str:
    """`user/uid` for the actor: a sudo'd root run and a plain one differ."""
    try:
        return f"{pwd.getpwuid(os.getuid()).pw_name}/{os.getuid()}"
    except KeyError:  # a uid with no passwd entry still gets a line
        return f"?/{os.getuid()}"


def allowed(server: str, extra: frozenset[str] = frozenset()) -> bool:
    """True only for an id named here or passed with --allow-server.

    Exact match on purpose: ids are lower-case hex, and a case-folded or
    prefix match is a wider fence than the one this tool claims.
    """
    return server in (ALLOWED_SERVERS | extra)


def read_key(path: Path | None = None) -> str:
    """The reseller key from its 0600 file. The value never reaches argv."""
    key = (path or KEY_FILE).read_text(encoding="utf-8").strip()
    if not key:
        raise OSError(f"{path or KEY_FILE} is empty")
    return key


def refusal(error: urllib.error.HTTPError) -> str:
    """The API's own sentence for a refused call, not a status-code paraphrase."""
    try:
        body = error.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — a body we cannot read is still a refusal
        return str(error.reason)
    try:
        return str(json.loads(body).get("error") or body.strip())[:400]
    except ValueError:
        return body.strip()[:400] or str(error.reason)


def call(action: str, params: dict | None = None, *, key: str, server_id: str,
         timeout: float = 40.0) -> dict:
    """One API call; returns the whole payload so a caller can read `message`."""
    query = {"action": action, "id": server_id}
    url = f"{API}?{urllib.parse.urlencode(query)}"
    body = json.dumps(params).encode() if params is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={"X-Reseller-Key": key, "Accept": "application/json",
                 "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"{action} answered {error.code}: {refusal(error)}") from None
    if not payload.get("success", True):
        raise RuntimeError(f"{action} refused: {payload.get('error') or payload}")
    return payload


def state_of(payload: dict) -> dict:
    """The `boot_image` block out of a GET payload ({} when it carries none)."""
    return (payload.get("data") or {}).get("boot_image") or {}


def message_of(payload: dict) -> str:
    """The API's own sentence, whichever of its two shapes carried it."""
    top = payload.get("message")
    if isinstance(top, str) and top.strip():
        return top.strip()
    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("message")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def pair(state: dict) -> tuple[str, str, str]:
    """(url, sha256, added) for the image the machine carries now.

    `none` is a state, not a failure: a machine on the Servury release has no
    custom image, and the line records the difference.
    """
    image = state.get("image") or {}
    return (str(image.get("url") or NONE),
            str(image.get("sha256") or NONE),
            str(image.get("added") or NONE))


def audit(line: str) -> None:
    """Append one line at 0600. Raises OSError so the caller can abort the write."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(handle, (line.rstrip() + "\n").encode("utf-8"))
    finally:
        os.close(handle)
    os.chmod(LOG, 0o600)


def show(key: str, server: str) -> int:
    """Print the four fields that answer what the machine carries now."""
    state = state_of(call("boot-image", None, key=key, server_id=server))
    if not state:
        print(f"boot-image: {server} answered without a boot_image block", file=sys.stderr)
        return 1
    url, sha256, added = pair(state)
    download = state.get("download") or {}
    finished = f" (finished {download['finished']})" if download.get("finished") else ""
    print(f"boot-image  {server} next_start={state.get('next_start') or '?'} "
          f"release={state.get('release') or '?'}")
    print(f"  url       {url}")
    print(f"  sha256    {sha256}")
    print(f"  download  {download.get('state') or '?'}{finished}")
    print(f"  added     {added}")
    return 0


def set_image(key: str, server: str, url: str, sha256: str, apply: bool) -> int:
    """Log the attempt with the pair it replaces, then (only) write if `apply`."""
    readable = True
    try:
        before_url, before_sha, before_added = pair(
            state_of(call("boot-image", None, key=key, server_id=server)))
    except (RuntimeError, OSError) as error:   # URLError is an OSError
        # Fail closed: a write whose predecessor is unknown is not a record.
        print(f"boot-image: could not read what {server} carries now: {error}", file=sys.stderr)
        before_url = before_sha = before_added = UNKNOWN
        readable = False
    try:
        audit(f"{datetime_stamp()} BOOT_IMAGE server={server} url={url} sha256={sha256} "
              f"apply={int(apply)} who={who()} replaced_url={before_url} "
              f"replaced_sha256={before_sha} replaced_added={before_added}")
    except OSError as error:
        print(f"boot-image: could not append to {LOG}: {error}", file=sys.stderr)
        return 1
    if not readable:
        return 1
    print(f"boot-image: {server} carries {before_url} ({before_sha[:16]}…"
          f"{', added ' + before_added if before_added != NONE else ''})")
    print(f"boot-image: {'set' if apply else 'would set'} → {url} ({sha256[:16]}…)")
    if not apply:
        print("boot-image: dry run — pass --apply and --confirm to submit")
        return 0
    try:
        payload = call("boot-image", {"url": url, "sha256": sha256}, key=key, server_id=server)
    except (RuntimeError, OSError) as error:
        print(f"boot-image: the write was refused: {error}", file=sys.stderr)
        return 1
    message = message_of(payload)
    print(f"boot-image: submitted for {server}" + (f" — {message}" if message else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show or set a Blackbox's boot image through the reseller API, "
                    "logging every attempt with the pair it replaced.")
    parser.add_argument("--show", action="store_true",
                        help="print the boot image the machine carries now")
    parser.add_argument("--set", action="store_true",
                        help="point the next start at --url/--sha256; a dry run unless --apply")
    parser.add_argument("--server", default="", help="8-character server id")
    parser.add_argument("--url", default="", help="with --set: the published image URL")
    parser.add_argument("--sha256", default="", help="with --set: that image's SHA-256 (64 hex)")
    parser.add_argument("--apply", action="store_true", help="with --set: actually submit")
    parser.add_argument("--confirm", action="store_true",
                        help="with --set --apply: the next start will use this pair")
    parser.add_argument("--allow-server", action="append", default=[],
                        help=f"extra server id this tool may touch (default: {sorted(ALLOWED_SERVERS)})")
    args = parser.parse_args(argv)

    if not (args.show or args.set):
        parser.print_help()
        return 2
    if args.show and args.set:
        print("boot-image: --show and --set are separate verbs — pass one", file=sys.stderr)
        return 2
    if not args.server:
        print("boot-image: --server is required (the 8-character id)", file=sys.stderr)
        return 2
    if not allowed(args.server, frozenset(args.allow_server or ())):
        print(f"boot-image: {args.server} is not in the allow-list "
              f"{sorted(ALLOWED_SERVERS)}; pass --allow-server to add it deliberately",
              file=sys.stderr)
        return 2
    if args.set:
        if not (args.url and args.sha256):
            print("boot-image: --set needs --url and --sha256", file=sys.stderr)
            return 2
        if not SHA256.fullmatch(args.sha256):
            print("boot-image: --sha256 must be 64 hex characters", file=sys.stderr)
            return 2
        if args.apply and not args.confirm:
            print("boot-image: --apply also needs --confirm (a wrong pair breaks the "
                  "next start)", file=sys.stderr)
            return 2
    try:
        key = read_key()
    except OSError as error:
        print(f"boot-image: cannot read the reseller key at {KEY_FILE}: {error}", file=sys.stderr)
        return 2
    if args.show:
        return show(key, args.server)
    return set_image(key, args.server, args.url, args.sha256, args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
