#!/usr/bin/env python3
"""Cycle a Servury Blackbox and capture its boot transcript.

The manual loop was: edit the panel's boot image, power the machine, watch the
serial console, copy the output. All four are commands now — the boot image has
an API verb (probed 12 November: `POST ?action=boot-image&id=<server>
{"url": …, "sha256": …}`), the console and power are `?action=serial` and
`?action=power`, and this tool does all of them: set the image, wait for the
node's download to report `ready`, turn the console log on, apply the power
action, wait for the line that means the boot reached the stage under test,
save the transcript as evidence, and turn the console log **off** again.

The off is not housekeeping. The panel keeps the machine's serial output as a
plaintext file on the host — *"209 KB so far … switch it off when you are done
debugging and the file is emptied"* — so a cycle that armed the log and walked
away left the whole boot transcript there (measured 2026-11-19: the tenant box
held 209 KB, the bench 1,174 KB). `--keep-console` is the operator asking to
keep it. The off is checked rather than assumed: the switch lands seconds after
the POST, so the way out watches the host's copy for a settle window and posts
again if a tail from the still-booting guest arrives behind the first off.

    ./tools/servury_cycle.py --server-id 31a63bbb --action restart \\
        --wait-for "bb-sealed-unlock" --serial-out docs/evidence/uki-boot.txt

**The key is account-scoped.** `/root/.odile/servury_reseller_key` owns
`31a63bbb` and `<server:hq>`; `<server:ukitest>` (the image box) belongs to
`<servury-account:dev>`, which has a panel session and no reseller key — the API
answers `Server not found` for it. So `--via auto` (the default) uses the API
where the key answers and falls back to `tools/servury_panel.py` otherwise, and
the transcript header says which path ran.

Doc: https://servury.com/docs/?api=blackbox — GET/POST ?action=serial&id, POST
?action=power&id. The `boot-image` verb is not in the action list the API's own
404 prints; it answers 200 when probed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://servury.com/api/reseller.php"
KEY_FILE = Path("/root/.odile/servury_reseller_key")
PANEL_TOOL = Path(__file__).resolve().parent / "servury_panel.py"
POWER_ACTIONS = ("start", "stop", "restart", "forcestop")
# The panel's serial buffer is a fixed ~128 KiB window: new output pushes the
# old boot out, so the length never grows. A new boot is therefore recognised
# by content, not by an offset: the kernel's first line anchors the latest boot,
# and a --fresh cycle waits until the buffer has *gained* one since the
# baseline it read before the power action.
#
# The anchor cannot be the release-setup stamp. A *converged* machine prints
# `odile-release-setup: already converged — ran <stamp>`, the same stamp on
# every boot, and no `odile-release-setup <stamp>` line at all; keying freshness
# on that stamp made every --fresh cycle skip a matched boot until --timeout
# (measured 2026-11-19 on the bench 168020b4: 603 s, matched=False, while the
# same boot without --fresh matched in 86 s; ODL-913's transcripts). The stamp
# is still counted for the header, as evidence of what the buffer held.
BOOT_MARKER = "Linux version "
RELEASE_LINE = re.compile(r"odile-release-setup (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)")
# The console switch lands a moment after the off POST, and a boot that is still
# printing flushes its last lines into the host's copy seconds later (measured
# 2026-11-19: 55 bytes of getty banner at +8 s on two consecutive bench cycles,
# after an immediate read-back had seen nothing). So "off" is not the same fact
# as "empty": the disarm watches the copy for this window and posts again when a
# tail arrives.
CONSOLE_SETTLE_READS = 15
CONSOLE_SETTLE_INTERVAL = 1.0
CONSOLE_MAX_OFFS = 3


def latest_boot(text: str) -> str:
    """The text from the last kernel start, or everything when it has none."""
    index = (text or "").rfind(BOOT_MARKER)
    return text[index:] if index >= 0 else (text or "")


def appended_text(earlier: str, current: str) -> str:
    """The bytes the transcript gained since `earlier` was read.

    The host's buffer is a rolling window: the new output is written after the
    text that was already there, and the oldest bytes fall off the front, so
    what is left of the earlier read is a *prefix* of `current` and everything
    after that prefix is new. (The two boots' text is nearly identical — same
    kernel line, same command line, often the same second — so comparing the
    two, or matching the earlier boot's tail, cannot tell them apart.)

    The probe is the first 64 characters of `current`: in the common case it
    is the head of the retained prefix, and `find` locates it in `earlier` in
    one C scan. A candidate is accepted only when the whole remaining earlier
    text is a prefix of `current`, which is what rejects a buffer that rolled
    all the way past the earlier read (then all of `current` is new).
    """
    if not earlier:
        return current
    if not current:
        return ""
    probe = current[:64]
    start = 0
    while True:
        at = earlier.find(probe, start)
        if at < 0:
            return current
        if current.startswith(earlier[at:]):
            return current[len(earlier) - at:]
        start = at + 1


def release_times(text: str) -> set[str]:
    """The release-setup stamps in the text; the header reports how many."""
    return set(RELEASE_LINE.findall(text or ""))


def call(action: str, params: dict | None = None, *, key: str, server_id: str | None = None,
         timeout: float = 40.0) -> dict:
    query = {"action": action}
    if server_id:
        query["id"] = server_id
    url = f"{API}?{urllib.parse.urlencode(query)}"
    body = json.dumps(params).encode() if params is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={"X-Reseller-Key": key, "Accept": "application/json", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not payload.get("success", True):
        raise RuntimeError(f"servury: {action} failed: {payload}")
    return payload.get("data") or {}


def transcript(data: dict) -> str:
    """The console text out of a GET ?action=serial payload."""
    for field in ("output", "log", "transcript"):
        value = data.get(field)
        if isinstance(value, str):
            return value
    return ""


def reached(text: str, patterns: list[str]) -> bool:
    """True when every pattern is somewhere in the transcript."""
    return all(re.search(pattern, text or "", re.MULTILINE) for pattern in patterns)


def power(opener_key: str, server_id: str, action: str) -> str:
    """Apply the power action; return a note about how the API answered.

    The gateway in front of the API gives up after 30 s and answers 504, but
    the node has already taken the action by then (`recording_now` and the
    transcript show it). A 504 is therefore "sent, unconfirmed", not a
    failure — the transcript poll decides whether the boot happened.
    """
    try:
        call("power", {"action": action}, key=opener_key, server_id=server_id)
        return "sent"
    except urllib.error.HTTPError as error:
        if error.code == 504:
            return "sent; the API gateway timed out at 30s (504), the node takes it anyway"
        raise


def boot_image_state(key: str, server_id: str) -> dict:
    """The machine's boot-image state, as the API reports it."""
    return call("boot-image", None, key=key, server_id=server_id).get("boot_image") or {}


def set_boot_image_api(key: str, server_id: str, url: str, sha256: str,
                       *, timeout: float, interval: float) -> str:
    """POST the published UKI and wait for the node to report its download ready."""
    call("boot-image", {"url": url, "sha256": sha256}, key=key, server_id=server_id)
    print(f"boot-image  submitted {url} ({sha256[:16]}…)")
    started = time.time()
    last = ""
    while time.time() - started < timeout:
        state = boot_image_state(key, server_id)
        download = state.get("download") or {}
        last = str(download.get("state") or "?")
        if last == "ready" and (download.get("kernel_sha256") in (None, sha256)):
            seconds = round(time.time() - started)
            print(f"boot-image  download ready in {seconds}s (next start: {state.get('next_start')})")
            return "api"
        if last in ("failed", "error"):
            raise RuntimeError(f"the node reported download.state={last!r} for {url}")
        time.sleep(max(interval, 1))
    raise RuntimeError(f"the node never reported download.state=ready (last {last!r})")


def panel(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the fenced panel tool; its stdout is relayed, its stderr kept."""
    command = [sys.executable, str(PANEL_TOOL), *args]
    print(f"$ {' '.join(command)}")
    return subprocess.run(command, capture_output=True, text=True, timeout=300)


def panel_boot_image(server_id: str, url: str, sha256: str) -> str:
    done = panel("--set-boot-image", "--server", server_id, "--url", url,
                 "--sha256", sha256, "--apply", "--confirm")
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or "the panel refused the boot image")
    print(done.stdout.rstrip())
    return "panel"


def panel_console(server_id: str, action: str) -> subprocess.CompletedProcess[str]:
    return panel("--console", "--server", server_id, "--action", action)


def host_console_copy(via: str, key: str, server_id: str) -> tuple[str | None, str]:
    """The host's transcript copy; (None, why) when the read itself failed.

    "The copy is empty" and "the read did not answer" are different facts, and
    only the first can support the word *emptied*. The panel's read reports a
    nonzero code through `panel_log` → None; the API's bare GET can raise.
    """
    try:
        if via == "api":
            return transcript(call("serial", None, key=key, server_id=server_id)), ""
        text = panel_log(server_id)
    except Exception as error:  # noqa: BLE001 — an unread copy is not an empty one
        return None, str(error) or error.__class__.__name__
    if text is None:
        return None, "the panel did not answer the read"
    return text, ""


def switch_console_off(via: str, key: str, server_id: str) -> tuple[bool, str]:
    """POST the off; (True, the panel's own line) or (False, one refusal line)."""
    if via == "api":
        try:
            call("serial", {"log": False}, key=key, server_id=server_id)
        except Exception as error:  # noqa: BLE001 — a refusal is a note, not a failure
            return False, str(error) or error.__class__.__name__
        return True, ""
    try:
        done = panel_console(server_id, "off")
    except Exception as error:  # noqa: BLE001 — the wrapper's subprocess.run raises on a hang
        return False, str(error) or error.__class__.__name__
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        return False, detail[-1] if detail else "the panel refused"
    return True, done.stdout.strip()


def disarm_console(via: str, key: str, server_id: str) -> bool:
    """Turn the console log off; True when the host's plaintext copy is emptied.

    The panel says why this matters in its own words: *the hypervisor keeps a
    transcript of everything this machine prints to its serial console … It is a
    plaintext file on the host: switch it off when you are done debugging and the
    file is emptied.* A cycle arms the log to capture one boot, so it must disarm
    it once the transcript is saved — otherwise the machine's whole serial output
    sits on the host until a human notices (measured 2026-11-19: the tenant box
    held 209 KB and the bench 1,174 KB, both armed by this tool).

    A refusal is one line and never a changed exit code: the boot transcript is
    what the cycle is judged on, and a panel hiccup must not turn a good
    transcript into a failed cycle. The audit log still names the attempt.

    "Off" is checked, not assumed. The switch applies a moment after the POST
    and a still-printing boot can flush its last lines after it, so this watches
    the copy for the settle window (`CONSOLE_SETTLE_READS`) and posts another
    off whenever a tail arrives. It prints *emptied* only from a read that
    succeeded and returned nothing; otherwise it says what it actually saw.
    """
    ok, note = switch_console_off(via, key, server_id)
    if not ok:
        print(f"console   disarm refused ({note}); the log stays on and the host keeps its transcript")
        return False
    if note:
        print(note)

    offs = 1
    left = ""
    unread = ""
    for _ in range(CONSOLE_SETTLE_READS):
        time.sleep(CONSOLE_SETTLE_INTERVAL)
        copy, why = host_console_copy(via, key, server_id)
        if copy is None:
            unread = why
            continue
        unread = ""
        left = copy
        if copy and offs < CONSOLE_MAX_OFFS:
            # The tail arrived before the switch applied; recording stops with
            # it, so another off empties what landed. (Measured: one manual
            # second off emptied the 55-byte getty banner.)
            ok, note = switch_console_off(via, key, server_id)
            if not ok:
                print(f"console   disarm refused ({note}); the log stays on and the host keeps its transcript")
                return False
            offs += 1
    if unread:
        print(f"console   off, but the host's copy could not be read ({unread}); "
              "check it with --console --action read")
        return False
    if left:
        print(f"console   off, but the host still shows {len(left)} bytes "
              "(the tail landed as the switch applied)")
        return False
    print("console   off; the host's transcript file is emptied")
    return True


def panel_log(server_id: str) -> str | None:
    """The serial log the panel holds: "" when empty, None when the read failed.

    None rather than "": a read-back that did not answer must not look like an
    empty file, or the disarm says "emptied" without having seen it.
    """
    done = panel_console(server_id, "read")
    return done.stdout if done.returncode == 0 else None


def choose_via(via: str, key: str | None, server_id: str) -> str:
    """api or panel, decided by whether the key answers for this server."""
    if via == "panel":
        return "panel"
    if via == "api":
        return "api"
    if not key:
        print("via         no reseller key: using the panel")
        return "panel"
    try:
        state = boot_image_state(key, server_id)
    except Exception as error:  # noqa: BLE001 — the panel is the fallback, not a crash
        print(f"via         the key does not answer for {server_id} ({error}): using the panel")
        return "panel"
    print(f"via         the key answers for {server_id} (next start: {state.get('next_start')}): using the API")
    return "api"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Power a Blackbox and capture its serial transcript.")
    parser.add_argument("--server-id", required=True, help="8-character server id")
    parser.add_argument("--action", default="restart", choices=POWER_ACTIONS)
    parser.add_argument("--serial-out", required=True, help="where to write the transcript")
    parser.add_argument(
        "--wait-for", action="append", default=None,
        help="regex that means the boot reached the stage under test; repeat it "
             "and every pattern must appear (e.g. the unlock and its measurement)")
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds to wait for --wait-for")
    parser.add_argument("--interval", type=float, default=10.0, help="poll interval in seconds")
    parser.add_argument("--key-file", default=str(KEY_FILE))
    parser.add_argument("--image-sha256", default="", help="the published image hash, recorded in the header")
    parser.add_argument("--boot-image", default="", help="published UKI URL to set before the power action")
    parser.add_argument("--boot-image-sha256", default="", help="that UKI's SHA-256")
    parser.add_argument("--via", default="auto", choices=("auto", "api", "panel"),
                        help="use the reseller API, the panel, or decide by probing (default)")
    parser.add_argument("--panel-done", action="store_true",
                        help="the machine already points at this image: do not set it again")
    parser.add_argument(
        "--fresh", action="store_true",
        help="ignore the boot the transcript already holds, so --wait-for cannot "
             "match a line from a previous boot (the API path empties the buffer; "
             "the panel path waits until the buffer has gained a kernel start)")
    parser.add_argument(
        "--keep-console", action="store_true",
        help="leave the console log on after the cycle (default: turn it off once "
             "the transcript is saved, so the host keeps no plaintext copy)")
    args = parser.parse_args(argv)

    if args.boot_image and not args.boot_image_sha256:
        print("cycle: --boot-image also needs --boot-image-sha256", file=sys.stderr)
        return 2

    key = ""
    key_path = Path(args.key_file)
    if key_path.is_file():
        key = key_path.read_text().strip()
    out = Path(args.serial_out)
    wanted = args.wait_for or []
    via = choose_via(args.via, key, args.server_id)
    seen: set[str] = set()
    earlier = ""
    earlier_full = ""

    image_path = "unchanged (--panel-done)"
    # Once the log is armed the host is holding a plaintext transcript, so every
    # path out of this function has to disarm it — including a --wait-for timeout
    # and a panel error. `--keep-console` is the operator saying "I want it".
    console_armed = False
    started = time.time()
    text = ""
    matched = not wanted
    try:
        if args.boot_image and not args.panel_done:
            if via == "api":
                image_path = set_boot_image_api(
                    key, args.server_id, args.boot_image, args.boot_image_sha256,
                    timeout=args.timeout, interval=args.interval)
            else:
                image_path = panel_boot_image(args.server_id, args.boot_image, args.boot_image_sha256)
        elif args.boot_image:
            image_path = "already set (--panel-done)"
            print(f"boot-image  not set again (--panel-done); the machine holds {args.boot_image_sha256[:16]}…")

        if via == "api":
            if args.fresh:
                # `log: false` empties the host's copy; without it the buffer can hold
                # earlier boots and a restart would match --wait-for before it happened.
                call("serial", {"log": False}, key=key, server_id=args.server_id)
                print("serial    transcript emptied before this boot")
            # The flag goes up before the call, not after: the API documents 504s
            # at 30 s, and a 504 can arrive after the gateway applied the switch.
            # A raising arm therefore still owes a disarm.
            console_armed = True
            state = call("serial", {"log": True}, key=key, server_id=args.server_id)
            print(f"serial    logging={state.get('logging')} (starts at the next start)")
            print(f"power     {args.action} {power(key, args.server_id, args.action)}")
        else:
            # Arming is idempotent. The host's buffer is not emptied by off/on
            # (measured: off→on left the previous boot in place) and it rotates
            # rather than growing, so --fresh records the boot the buffer already
            # holds and only a *different* latest boot counts as this one. The
            # comparison is on the boot's own text, not on a setup stamp: a
            # converged machine prints the same stamp on every boot and no
            # `odile-release-setup <stamp>` line at all (see appended_text).
            # Even a failed or hung arm may have posted the switch, so the way
            # out disarms unconditionally once the attempt is made. The flag is
            # set before the call: the panel wrapper's subprocess.run(timeout=300)
            # raises on a hang, and that path has no return code to test.
            console_armed = True
            armed = panel_console(args.server_id, "on")
            if armed.returncode != 0:
                print(armed.stderr.strip() or "the panel refused to arm the console log", file=sys.stderr)
                return 1
            print(armed.stdout.rstrip())
            if args.fresh:
                earlier_full = panel_log(args.server_id) or ""
                earlier = latest_boot(earlier_full)
                seen = release_times(earlier)
                print(f"serial    {len(seen)} earlier boot stamp(s) in the buffer; "
                      f"the {len(earlier)}-byte boot already there does not count as this one")
            pressed = panel("--power", "--server", args.server_id, "--action", args.action)
            if pressed.returncode != 0:
                print(pressed.stderr.strip() or f"the panel refused power {args.action}", file=sys.stderr)
                return 1
            print(pressed.stdout.rstrip())

        started = time.time()
        while time.time() - started < args.timeout:
            time.sleep(args.interval)
            try:
                if via == "api":
                    # A bare GET returns the transcript. A body must carry `log`; the
                    # API rejects `{"bytes": …}` alone ("log is required"), and the
                    # old poll posted exactly that, so --wait-for never matched.
                    text = transcript(call("serial", None, key=key, server_id=args.server_id))
                else:
                    full = panel_log(args.server_id) or ""
                    text = latest_boot(full) if args.fresh else full
                    # A boot counts only once the transcript has *gained* a
                    # kernel start since the baseline: the node can take a
                    # minute to start after a restart (measured 2026-11-20 on
                    # the bench 168020b4: 75 s), and until then the buffer
                    # holds the earlier boot plus its shutdown tail.
                    if args.fresh and BOOT_MARKER not in appended_text(earlier_full, full):
                        continue
            except Exception as error:  # noqa: BLE001 — a poll failure is not a boot failure
                print(f"serial    poll error: {error}")
                continue
            if wanted and reached(text, wanted):
                matched = True
                break
            if not wanted and text:
                break

        header = [
            f"# servury_cycle {args.server_id} action={args.action} via={via}",
            f"# image_sha256 {args.image_sha256 or '(not given)'}",
            f"# boot_image {image_path}",
            f"# fresh {args.fresh} earlier_boot_stamps {len(seen)} earlier_boot_bytes {len(earlier)}",
            f"# waited_for {' + '.join(wanted) or '(none)'} matched={matched}",
            f"# seconds {round(time.time() - started)}",
            "",
        ]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(header) + text, encoding="utf-8")
        tail = "\n".join(text.splitlines()[-12:])
        print(f"transcript {out} ({len(text)} bytes)")
        print(tail)
    finally:
        if console_armed:
            if args.keep_console:
                print("console   left on (--keep-console); the host keeps its plaintext transcript")
            else:
                disarm_console(via, key, args.server_id)
    if not matched:
        print(f"cycle: the transcript never matched {' + '.join(wanted)!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
