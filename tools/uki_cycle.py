#!/usr/bin/env python3
"""One iteration of the measured-image loop — publish, set the panel, cycle,
attest, record.

`docs/ops/UKI-CYCLE.md` names the loop: build the UKI, publish it at a
content-addressed URL, put that URL + SHA-256 in the Servury panel, power the
machine, capture the serial transcript, and check the measurement the machine
booted against the one `sev-snp-measure` computes from the published bytes.

    # the whole iteration, no human step
    make uki-cycle UKI=deploy/image/out-sealed/odile-tenant.efi CARD=ODL-xxx

    # publish and print the pair, without touching the panel or the machine
    make uki-cycle UKI=... CARD=... NO_PANEL=1

The tool is the wiring, not a second implementation: publishing is
`tools/publish_uki.py` (which reads the served bytes back over HTTPS), the
panel is `tools/servury_panel.py` (login + form fill, `--set --dry-run` first
if you want to see the fields), and the machine is `tools/servury_cycle.py`
(console log on, power action, poll the transcript, write it with the image
hash in the header, console log off). The off is the cycle's own: the host
keeps the serial log as a plaintext file, so `--keep-console` is the only way
to leave it armed. What is added here is the order, the panel block the
handoff quotes, the attest check and the evidence file.

The attest check has three values and names all of them:

* **expected** — `sev-snp-measure` over the published UKI against Servury's
  published OVMF, with the machine's vCPU count (4 on a Blackbox Plus);
* **boot** — the measurement line the tenant's own boot printed to the serial
  console, read from `/run/blackbox/attest.json` on the guest;
* **live** — `GET /attestation` on the tenant URL, when Caddy is up.

PASS means every value that exists is the expected one. A run where no
measurement could be read FAILS: "the transcript never matched" is a result,
not a warning. A machine whose release setup has already converged prints no
measurement (the installer never ran, and `odile-release-setup` says so), so
the cycle accepts that line as the stage under test and the boot is judged on
the live `/attestation` — the machine's own boot report. `ODILE_ATTEST_URL`
overrides the default `https://<slug>.odile.fyi/attestation`, for a bench whose
name has no certificate yet.

Exit codes: 0 ok (publish-only, or a cycle whose attest check passed);
1 a panel, cycle or attest failure; 2 bad input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLISH_TOOL = ROOT / "tools" / "publish_uki.py"
CYCLE_TOOL = ROOT / "tools" / "servury_cycle.py"
PANEL_TOOL = ROOT / "tools" / "servury_panel.py"
EVIDENCE_ROOT = ROOT / "docs" / "ops" / "evidence" / "uki-cycle"

DEFAULT_SLUG = "odlukitest"
# The live server id is not in the tree: one id per line in this untracked
# file, written by the operator. A clone has no default target.
SERVER_ID_FILE = Path("/root/.odile/servury_uki_server_id")
# odlukitest is a Blackbox Core: 2x EPYC 7B13. The launch measurement covers
# one VMSA per vCPU, so the count must be this machine's, not a Plus's four.
DEFAULT_VCPUS = 2
DEFAULT_VCPU_TYPE = "EPYC-v4"
DEFAULT_GUEST_FEATURES = "0x1"

# Servury's published AmdSev firmware, the one a Blackbox loads; `uki.sh`
# pins the same digest. The measurement is meaningless against another OVMF.
OVMF_SHA256 = "8731846cdbcbb6aa5c624238b839544b1553f4c5af874450f043321b3ef8958b"
OVMF_CANDIDATES = (
    ROOT / "deploy" / "image" / "out-sealed" / "ovmf" / "OVMF.amdsev.fd",
    Path("/var/tmp/bb/published/OVMF.amdsev.fd"),
    Path("/var/tmp/bb/out/firmware/OVMF.amdsev.fd"),
)

PANEL_URL = re.compile(r"^URL\s+(\S+)\s*$", re.MULTILINE)
PANEL_SHA = re.compile(r"^SHA-256\s+([0-9a-f]{64})\s*$", re.MULTILINE)
# The cycle tool names the path it took: "using the panel" or "using the API".
USING = re.compile(r"using the (panel|API)\b")
BOOT_MEASUREMENT = re.compile(r"\bmeasurement\s+([0-9a-f]{96})\b")
HEX96 = re.compile(r"^[0-9a-f]{96}$")
# The stage under test is "release setup ran", and it prints two shapes. On a
# first boot the installer prints the boot's measurement, a minute in. On a
# machine whose setup has already converged it prints its own converged line and
# **no measurement at all** (`odile-release-setup: already converged — ran
# 2026-11-19T17:09:23Z · …`, measured on the bench 168020b4). Waiting only for
# the measurement made every cycle on a converged machine burn its timeout for a
# line that can never come; both forms are one alternation because the cycle
# requires every --wait-for pattern it is given. (`already ran (…` is the
# backfill branch, a machine whose marker was written from its own log.)
MEASUREMENT_LINE = (
    r"odile-release-setup.*measurement\s+[0-9a-f]{96}"
    r"|odile-release-setup: already (?:converged — ran|ran \()"
)
# The kernel prints the command line it received, once. The empty-`.cmdline` UKI
# (ADR-001 §4) gets its per-machine line from the panel as QEMU's append, and
# the launch measurement covers that append — so `sev-snp-measure` without
# `--append` computes a different number from the one the machine reports
# (measured 2026-11-20 on the bench 168020b4: 112bbed0… without the line,
# b22cd431… with it, which is the boot record's own value).
COMMAND_LINE = re.compile(r"^\[\s*[0-9.]+\] (?:Kernel )?[Cc]ommand line: (.*)$", re.MULTILINE)


def utc_stamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def default_server_id() -> str:
    """The first id in the untracked file, or "" when a clone has none."""
    try:
        text = SERVER_ID_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def digest_of(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def parse_publish(stdout: str) -> tuple[str, str]:
    """The (URL, SHA-256) pair `publish_uki.py` printed, or a named failure."""
    url = PANEL_URL.search(stdout or "")
    digest = PANEL_SHA.search(stdout or "")
    if not url or not digest:
        raise ValueError("publish output has no URL/SHA-256 pair")
    return url.group(1), digest.group(1)


def panel_block(card: str, url: str, digest: str, uki: Path, stamp: str) -> str:
    """The panel lines a reviewer can read without opening the panel."""
    return (
        f"# Panel — {card}\n\n"
        f"Boot image URL       {url}\n"
        f"Boot image SHA-256   {digest}\n"
        f"Command line         (leave empty; the per-tenant line is inside the UKI)\n\n"
        f"# local build         {uki}\n"
        f"# published           {stamp}\n"
    )


def measurement_from_transcript(text: str) -> str | None:
    """The last boot measurement the tenant printed to its serial console.

    `odile-release-setup` prints the value it read from the guest's own
    `/run/blackbox/attest.json`, so this is the boot report's launch digest,
    not a value derived from the image on HQ. The last occurrence wins: the
    transcript buffer can hold more than one boot.
    """
    found = BOOT_MEASUREMENT.findall(text or "")
    return found[-1] if found else None


def measurement_from_attestation(document: dict) -> str | None:
    """The launch digest out of the JSON `/attestation` serves."""
    if not isinstance(document, dict) or document.get("available") is False:
        return None
    value = document.get("measurement")
    return value if isinstance(value, str) and HEX96.match(value) else None


def command_line_from_transcript(text: str) -> str | None:
    """The command line the boot printed, or None when it printed none.

    A boot of the empty-`.cmdline` UKI receives the per-machine line from the
    panel as QEMU's append, and the launch measurement covers it — so the
    expected value has to be computed over the same line. The kernel prints it
    as `[    0.000000] Command line: …`, which is the append the hypervisor was
    given; a baked-`.cmdline` UKI prints its own line instead, and the
    recomputation over that line simply will not match (the caller only uses a
    recomputed value that does).
    """
    found = COMMAND_LINE.findall(text or "")
    return found[0].strip() if found and found[0].strip() else None


def attest(expected: str | None, boot: str | None, live: str | None) -> tuple[bool, str]:
    """Compare the measurements; PASS only when every value that exists agrees.

    `expected is None` means the local measurement could not be computed, so a
    live value is still evidence; no value at all is a failure.
    """
    seen = [
        (name, value)
        for name, value in (("expected", expected), ("boot", boot), ("live", live))
        if value
    ]
    if not boot and not live:
        return False, "no measurement: the transcript and /attestation carried none"
    if expected is None:
        if boot and live and boot != live:
            return False, f"boot {boot[:16]}… != live {live[:16]}…"
        return True, f"{seen[0][0]} {seen[0][1][:16]}… (expected not computed locally)"
    for name, value in seen:
        if value != expected:
            return False, f"{name} {value[:16]}… != expected {expected[:16]}…"
    matched = "+".join(name for name, _ in seen if name != "expected")
    return True, f"expected {expected[:16]}… matched by {matched}"


def find_ovmf(explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    from_env = os.environ.get("ODILE_OVMF")
    if from_env:
        return Path(from_env)
    for candidate in OVMF_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def expected_measurement(
    uki: Path,
    ovmf: Path | None,
    *,
    vcpus: int,
    vcpu_type: str,
    guest_features: str,
    ovmf_sha256: str = OVMF_SHA256,
    append: str = "",
) -> tuple[str | None, str]:
    """(measurement, command-or-reason) for the published UKI.

    None means it could not be computed; the second value says why, so the
    evidence file records a missing tool instead of a silent blank.

    `append` is QEMU's command line to the guest, which the launch measurement
    covers. The empty-`.cmdline` shape of ADR-001 §4 has one (the panel line)
    and its number only matches with it; a baked-`.cmdline` UKI has none.
    """
    tool = os.environ.get("ODILE_SEV_SNP_MEASURE", "sev-snp-measure")
    if ovmf is None:
        return None, "no OVMF: pass --ovmf (Servury's published OVMF.amdsev.fd)"
    digest = digest_of(ovmf)
    if digest != ovmf_sha256:
        return None, f"OVMF {ovmf} is {digest[:16]}… not the published {ovmf_sha256[:16]}…"
    command = [
        tool, "--mode", "snp", "--ovmf", str(ovmf), "--kernel", str(uki),
        *(["--append", append] if append else []),
        "--vcpus", str(vcpus), "--vcpu-type", vcpu_type,
        "--guest-features", guest_features,
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=900)
    except FileNotFoundError:
        return None, f"{tool} is not installed"
    if completed.returncode != 0:
        return None, f"{tool} exited {completed.returncode}: {completed.stderr.strip()[:200]}"
    value = completed.stdout.strip()
    if not HEX96.match(value):
        return None, f"{tool} printed something that is not a 96-hex measurement: {value[:80]}"
    return value, " ".join(command)


def fetch_json(url: str, timeout: float = 15.0) -> tuple[dict | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.load(response), ""
    except Exception as error:  # noqa: BLE001 — unreachable is a result here
        return None, str(error)


def run(command: list[str], *, dry_run: bool = False) -> subprocess.CompletedProcess[str] | None:
    printable = " ".join(command)
    print(f"$ {printable}")
    if dry_run:
        return None
    return subprocess.run(command, capture_output=True, text=True, timeout=1800)


def render_evidence(
    *,
    card: str,
    stamp: str,
    server_id: str,
    uki: Path,
    url: str,
    digest: str,
    transcript: Path,
    boot: str | None,
    expected: str | None,
    expected_note: str,
    live: str | None,
    live_note: str,
    passed: bool,
    detail: str,
    action: str,
    wait_for: str,
    panel: str,
    commands: list[list[str]],
) -> str:
    verdict = "PASS" if passed else "FAIL"
    head = transcript.read_text(encoding="utf-8", errors="replace")[:2000] if transcript.is_file() else ""
    return (
        f"# UKI cycle — {card} — {stamp}\n\n"
        f"- server: `{server_id}`, power action `{action}`, waited for `{wait_for}`\n"
        f"- panel: {panel}\n"
        f"- image: {url}\n"
        f"- image SHA-256: `{digest}`\n"
        f"- local build: `{uki}`\n"
        f"- boot transcript: `{transcript}`\n"
        f"- expected measurement: `{expected or expected_note}`"
        + (f" — {expected_note}" if expected else "")
        + "\n"
        f"- boot measurement: `{boot or '(none printed)'}`\n"
        f"- live /attestation: `{live or live_note}`\n"
        f"- attest: **{verdict}** — {detail}\n\n"
        "## Commands\n\n"
        + "".join(f"    $ {' '.join(command)}\n" for command in commands)
        + "\n## Boot transcript head\n\n```\n"
        + head.replace("```", "'''")
        + "\n```\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One iteration of the measured-image loop: publish, cycle, attest, record.")
    parser.add_argument("--uki", required=True, help="the built UKI (.efi)")
    parser.add_argument("--card", required=True, help="board card id (ODL-xxx or t_…)")
    parser.add_argument("--server-id", default=None,
                        help="Servury server id (default: the id in "
                             "/root/.odile/servury_uki_server_id)")
    parser.add_argument("--slug", default=DEFAULT_SLUG, help="tenant hostname for /attestation")
    parser.add_argument("--no-panel", action="store_true",
                        help="publish and print the pair; do not touch the panel or the machine")
    parser.add_argument("--panel-dry-run", action="store_true",
                        help="fill the panel form and print it, but do not submit or cycle")
    parser.add_argument("--panel-done", action="store_true",
                        help="the machine already points at this image: do not set it again")
    parser.add_argument("--via", default="auto", choices=("auto", "api", "panel"),
                        help="how to set the boot image and read the console: "
                             "the reseller API, the panel, or decide by probing (default)")
    parser.add_argument("--action", default="restart", choices=("start", "stop", "restart", "forcestop"))
    parser.add_argument("--wait-for", default="bb-sealed-unlock",
                        help="regex that means the boot reached the stage under test")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--attest-url", default=None,
                        help="default https://<slug>.odile.fyi/attestation, or "
                             "$ODILE_ATTEST_URL when that is set; --no-attest-url skips it")
    parser.add_argument("--no-attest-url", action="store_true")
    parser.add_argument("--expected-measurement", default=None,
                        help="96-hex measurement to compare against (skips sev-snp-measure)")
    parser.add_argument("--ovmf", default=None, help="Servury's OVMF.amdsev.fd")
    parser.add_argument("--ovmf-sha256", default=OVMF_SHA256)
    parser.add_argument("--vcpus", type=int, default=DEFAULT_VCPUS)
    parser.add_argument("--vcpu-type", default=DEFAULT_VCPU_TYPE)
    parser.add_argument("--guest-features", default=DEFAULT_GUEST_FEATURES)
    parser.add_argument("--evidence-dir", default=None,
                        help=f"default {EVIDENCE_ROOT}/<card>")
    parser.add_argument("--publish-tool", default=str(PUBLISH_TOOL))
    parser.add_argument("--cycle-tool", default=str(CYCLE_TOOL))
    parser.add_argument("--panel-tool", default=str(PANEL_TOOL))
    parser.add_argument("--fresh", action="store_true", default=True,
                        help="empty the transcript before arming it (default)")
    parser.add_argument("--keep-old-transcript", dest="fresh", action="store_false")
    parser.add_argument("--keep-console", action="store_true",
                        help="leave the console log on after the cycle; the default "
                             "turns it off once the transcript is saved, because the "
                             "host keeps the log as a plaintext file")
    parser.add_argument("--dry-run", action="store_true", help="print the commands, change nothing")
    args = parser.parse_args(argv)

    if not args.server_id:
        args.server_id = default_server_id()
    if not args.server_id and not args.no_panel:
        print(
            f"uki-cycle: no server id: pass --server-id or write one to {SERVER_ID_FILE}",
            file=sys.stderr,
        )
        return 2

    uki = Path(args.uki)
    if not uki.is_file():
        print(f"uki-cycle: no such file: {uki}", file=sys.stderr)
        return 2
    if args.expected_measurement and not HEX96.match(args.expected_measurement):
        print("uki-cycle: --expected-measurement must be 96 lowercase hex characters", file=sys.stderr)
        return 2

    stamp = utc_stamp()
    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else EVIDENCE_ROOT / args.card
    local_digest = digest_of(uki)
    commands: list[list[str]] = []

    publish_command = [sys.executable, args.publish_tool, "--uki", str(uki)]
    commands.append(publish_command)
    print(f"publish   {uki} ({local_digest[:16]}…)")
    completed = run(publish_command, dry_run=args.dry_run)
    if completed is None:
        print(f"panel     would be written under {evidence_dir}")
        return 0
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.returncode != 0:
        print(completed.stderr.strip() or "publish-uki failed", file=sys.stderr)
        return 1
    try:
        url, digest = parse_publish(completed.stdout)
    except ValueError as error:
        print(f"uki-cycle: {error}", file=sys.stderr)
        return 2

    panel_path = evidence_dir / f"panel-{digest[:16]}.md"
    if not args.dry_run:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        panel_path.write_text(panel_block(args.card, url, digest, uki, stamp), encoding="utf-8")
    print()
    print(f"panel     the pair the Servury panel is given ({panel_path}):")
    print(f"          URL      {url}")
    print(f"          SHA-256  {digest}")
    print("          command line: leave empty")
    print()
    if args.no_panel:
        print("panel     not touched (--no-panel); the pair above is what a human would save")
        return 0

    panel_note = f"already held this pair; not touched (--panel-done, {args.server_id})"
    if args.panel_done:
        print("panel     the machine already points at this pair (--panel-done); not set again")
    elif args.panel_dry_run:
        # Fill the form and print the fields, submit nothing, cycle nothing.
        panel_command = [
            sys.executable, args.panel_tool, "--set-boot-image",
            "--server", args.server_id, "--url", url, "--sha256", digest,
        ]
        commands.append(panel_command)
        print(f"panel     filling the form on {args.server_id} (dry run) …")
        completed = run(panel_command, dry_run=args.dry_run)
        if completed is None:
            print("panel     dry run; nothing submitted")
            return 0
        if completed.stdout:
            print(completed.stdout.rstrip())
        if completed.returncode != 0:
            print(completed.stderr.strip() or "servury-panel failed", file=sys.stderr)
            return 1
        print("panel     dry run; nothing submitted and the machine was not cycled")
        return 0
    else:
        panel_note = f"set by the cycle tool on {args.server_id} (its header names the path)"
        print(f"panel     the cycle tool sets this pair on {args.server_id} before it powers the machine")

    transcript = evidence_dir / f"boot-{digest[:16]}-{stamp}.txt"
    cycle_command = [
        sys.executable, args.cycle_tool,
        "--server-id", args.server_id,
        "--action", args.action,
        "--serial-out", str(transcript),
        "--wait-for", args.wait_for,
        "--wait-for", MEASUREMENT_LINE,
        "--timeout", str(args.timeout),
        "--interval", str(args.interval),
        "--image-sha256", digest,
        "--boot-image", url,
        "--boot-image-sha256", digest,
        "--via", args.via,
    ]
    if args.panel_done:
        cycle_command.append("--panel-done")
    if args.fresh:
        cycle_command.append("--fresh")
    if args.keep_console:
        cycle_command.append("--keep-console")
    commands.append(cycle_command)
    completed = run(cycle_command, dry_run=args.dry_run)
    if completed is None:
        print(f"attest    would compare the boot measurement with sev-snp-measure over {uki}")
        return 0
    if completed.returncode != 0:
        # The cycle's own stdout is the path it took and the transcript it
        # wrote; its stderr is the reason it stopped. Relay the tail so the
        # make log says why, not just "the transcript never matched".
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-8:]
        if tail:
            print("cycle     the tool failed; its last lines:", file=sys.stderr)
            for line in tail:
                print(f"          {line}", file=sys.stderr)
    match = USING.search(completed.stdout or "")
    if match and not args.panel_done:
        panel_note = (
            f"set by the {match.group(1).lower()} path of tools/servury_cycle.py "
            f"at {stamp} ({args.server_id})"
        )

    boot_text = transcript.read_text(encoding="utf-8", errors="replace") if transcript.is_file() else ""
    boot = measurement_from_transcript(boot_text)

    # The live document first: on a converged machine it is the only
    # measurement there is (the installer never ran, so the transcript has
    # none), and the expected value below may need the boot's own command line.
    live: str | None = None
    live_note = "skipped (--no-attest-url)"
    attest_url = (args.attest_url
                  or os.environ.get("ODILE_ATTEST_URL")
                  or f"https://{args.slug}.odile.fyi/attestation")
    if not args.no_attest_url:
        document, error = fetch_json(attest_url)
        if document is None:
            live_note = f"{attest_url} unreachable: {error}"
        else:
            live = measurement_from_attestation(document)
            live_note = "served" if live else f"{attest_url} carried no measurement"

    if args.expected_measurement:
        expected, expected_note = args.expected_measurement, "given by --expected-measurement"
    else:
        ovmf = find_ovmf(args.ovmf)
        expected, expected_note = expected_measurement(
            uki, ovmf, vcpus=args.vcpus, vcpu_type=args.vcpu_type,
            guest_features=args.guest_features, ovmf_sha256=args.ovmf_sha256)
        if expected:
            expected_note = "sev-snp-measure over the published UKI"
        # The empty-`.cmdline` shape booted with the panel's line measures with
        # that append; without it the computed value is a different number. The
        # line is in the transcript, so recompute over what booted — and keep the
        # recomputation only when an observed value agrees with it.
        if expected and (boot or live) and expected not in (boot, live):
            command = command_line_from_transcript(boot_text)
            alternative = None
            if command:
                alternative, _ = expected_measurement(
                    uki, ovmf, vcpus=args.vcpus, vcpu_type=args.vcpu_type,
                    guest_features=args.guest_features, ovmf_sha256=args.ovmf_sha256,
                    append=command)
            if alternative and alternative in (boot, live):
                expected = alternative
                expected_note = ("sev-snp-measure over the published UKI with the "
                                 "boot's own command line as --append")

    passed, detail = attest(expected, boot, live)
    if completed.returncode != 0:
        passed = False
        detail = f"the transcript never matched {args.wait_for!r} and the measurement (or converged) line; {detail}"

    evidence = render_evidence(
        card=args.card, stamp=stamp, server_id=args.server_id, uki=uki, url=url,
        digest=digest, transcript=transcript, boot=boot, expected=expected,
        expected_note=expected_note, live=live, live_note=live_note, passed=passed,
        detail=detail, action=args.action, wait_for=f"{args.wait_for} + measurement-or-converged",
        panel=panel_note, commands=commands)
    evidence_path = evidence_dir / f"cycle-{digest[:16]}-{stamp}.md"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(evidence, encoding="utf-8")

    print()
    print(evidence)
    print(f"evidence  {evidence_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
