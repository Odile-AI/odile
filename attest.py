#!/usr/bin/python3
"""Fetch this guest's SEV-SNP attestation report and cache it for the web app.

Runs as root before the service starts (the /dev/sev-guest node is 0600).
Writes JSON: the launch measurement, the raw report, and when it was taken.

On a Servury tenant the report is produced once at boot by `bb-seed`, which
copies it to /run/blackbox/attest.json (0644) before switch-root. The same
program transforms that boot report into the served shape, so /attestation on a
tenant is the report the running machine actually booted with:

    ./attest.py                                        # fetch now (root)
    ./attest.py --boot-report /run/blackbox/attest.json [OUT]

The boot report is per boot and does not change while the machine runs, so a
oneshot at boot writes the served copy; a reboot writes a new one. This is the
boot report, not a liveness proof: it does not measure the app layer.
"""
import argparse
import base64
import ctypes
import fcntl
import json
import os
import secrets
from datetime import datetime, timezone

SNP_GET_REPORT = 0xC0205300  # _IOWR('S', 0, struct snp_guest_request_ioctl)
DEFAULT_OUT = "/var/lib/odile/attestation.json"

# SEV-SNP report layout: the 48-byte SHA-384 launch digest is 0x90:0xC0, and
# the first four bytes are the report version. The raw report is the signed
# evidence; the JSON summary only describes it.
MEASUREMENT_OFFSET = 0x90
MEASUREMENT_SIZE = 48
NOTE = ("48-byte SHA-384 launch digest. Verify the full report with snpguest "
        "against the AMD VCEK chain.")
BOOT_NOTE = (NOTE + " This is the boot report, not a liveness proof. It does "
             "not measure the app layer.")


def read_cmdline(path: str = "/proc/cmdline") -> str:
    """The machine's own command line; it contains the release and the boot key."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def cmdline_value(cmdline: str, key: str) -> str | None:
    """The value of one `key=value` token in a command line, or None."""
    prefix = key + "="
    for token in cmdline.split():
        if token.startswith(prefix):
            value = token[len(prefix):]
            if value:
                return value
    return None


class _Req(ctypes.Structure):
    _fields_ = [
        ("msg_version", ctypes.c_uint8),
        ("_pad", ctypes.c_uint8 * 7),
        ("req_data", ctypes.c_uint64),
        ("resp_data", ctypes.c_uint64),
        ("exitinfo2", ctypes.c_uint64),
    ]


def fetch(nonce: bytes) -> bytes:
    req = ctypes.create_string_buffer(96)   # struct snp_report_req
    resp = ctypes.create_string_buffer(4000)
    ctypes.memmove(req, nonce[:64], min(len(nonce), 64))
    io = _Req(1, (ctypes.c_uint8 * 7)(), ctypes.addressof(req), ctypes.addressof(resp), 0)
    fd = os.open("/dev/sev-guest", os.O_RDWR)
    try:
        fcntl.ioctl(fd, SNP_GET_REPORT, io, True)
    finally:
        os.close(fd)
    if io.exitinfo2:
        raise OSError("guest request failed: vmm=%d fw=%d" % (io.exitinfo2 >> 32, io.exitinfo2 & 0xFFFFFFFF))
    return bytes(resp)


def boot_time(path: str) -> str | None:
    """The boot report's write time in UTC — the boot, not the read.

    `bb-seed` writes the file before switch-root, so its mtime is the boot.
    None when the file is missing: an unavailable document has no boot time.
    """
    try:
        stamp = os.stat(path).st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def unavailable(reason: str, fetched_at: str | None) -> dict:
    """The shape served when there is no usable report; the reason is the value."""
    return {
        "available": False,
        "guest": "sev-snp",
        "fetched_at": fetched_at,
        "note": BOOT_NOTE,
        "error": reason,
    }


def transform_boot_report(document: dict, fetched_at: str | None) -> dict:
    """The Servury boot report, in the shape attest.py serves and odile-verify reads.

    Every field comes from the report, never from a guess: the measurement is
    0x90:0xC0 of the signed bytes, the version and size are the report's own,
    and `fetched_at` is the boot report's write time. `release` and `cmdline`
    are carried because they are the measured release and command line the
    boot record hashes; a document without them still verifies, but
    `odile-verify --record` fails closed.
    """
    boot: dict[str, str] = {}
    release = document.get("release")
    if isinstance(release, str) and release.strip():
        boot["release"] = release.strip()
    cmdline = document.get("cmdline")
    if isinstance(cmdline, str) and cmdline.strip():
        boot["cmdline"] = cmdline.strip()

    status = document.get("status")
    if status != "ok":
        return {**unavailable("the boot report status is %r" % (status,), fetched_at), **boot}

    encoded = document.get("report_b64")
    if not isinstance(encoded, str) or not encoded:
        return {**unavailable("the boot report has no report_b64", fetched_at), **boot}
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        return {**unavailable("report_b64 is not valid base64: %s" % exc, fetched_at), **boot}
    if len(raw) < MEASUREMENT_OFFSET + MEASUREMENT_SIZE:
        return {
            **unavailable(
                "the report is %d bytes, shorter than the 0x%X measurement"
                % (len(raw), MEASUREMENT_OFFSET + MEASUREMENT_SIZE),
                fetched_at,
            ),
            **boot,
        }
    measurement = raw[MEASUREMENT_OFFSET:MEASUREMENT_OFFSET + MEASUREMENT_SIZE]
    if measurement == b"\x00" * MEASUREMENT_SIZE:
        return {**unavailable("the report carries an empty measurement", fetched_at), **boot}

    return {
        "available": True,
        "guest": "sev-snp",
        "measurement": measurement.hex(),
        "report_version": int.from_bytes(raw[0:4], "little"),
        "report_size": len(raw),
        "fetched_at": fetched_at,
        "report_b64": base64.b64encode(raw).decode("ascii"),
        "note": BOOT_NOTE,
        **boot,
    }


def read_boot_report(path: str) -> tuple[dict | None, str | None]:
    """The Servury JSON and no error, or None and the reason it cannot be read."""
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError:
        return None, "no boot report at %s" % path
    except ValueError as exc:
        return None, "the boot report at %s is not JSON: %s" % (path, exc)
    if not isinstance(document, dict):
        return None, "the boot report at %s is not a JSON object" % path
    return document, None


def write_document(out: str, payload: dict) -> None:
    """Write the served copy atomically, 0644: a reader sees old or new, never half."""
    directory = os.path.dirname(out) or "."
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(directory, ".%s.%d.tmp" % (os.path.basename(out), os.getpid()))
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.chmod(temp, 0o644)
        os.replace(temp, out)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve this machine's SEV-SNP report as JSON.")
    parser.add_argument("out", nargs="?", default=DEFAULT_OUT,
                        help="served JSON path (default %s)" % DEFAULT_OUT)
    parser.add_argument("--boot-report", metavar="PATH",
                        help="transform the boot report at PATH instead of reading /dev/sev-guest")
    parser.add_argument("--boot-time", metavar="ISO8601",
                        help="fetched_at for --boot-report (default: the file's mtime)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.boot_report:
        fetched_at = args.boot_time or boot_time(args.boot_report)
        document, error = read_boot_report(args.boot_report)
        if document is None:
            payload = unavailable(error or "no boot report at %s" % args.boot_report, fetched_at)
        else:
            payload = transform_boot_report(document, fetched_at)
        write_document(args.out, payload)
        detail = payload.get("measurement") or payload.get("error") or "unavailable"
        print("attestation -> %s (%s)" % (args.out, detail[:64]))
        return 0

    nonce = b"odile:" + secrets.token_bytes(24)
    # The release and the command line come from the same measured string the
    # CPU hashed; the served document uses them to select a pin entry and to
    # hash the command line into the boot record.
    cmdline = read_cmdline()
    boot = {}
    release = cmdline_value(cmdline, "bb.release")
    if release:
        boot["release"] = release
    if cmdline:
        boot["cmdline"] = cmdline
    cpus = os.cpu_count()
    if isinstance(cpus, int) and cpus > 0:
        boot["vcpus"] = cpus
    payload = {"available": False, "guest": "sev-snp", "fetched_at": None, **boot}
    try:
        raw = fetch(nonce)
        at = raw.find(nonce)
        if at < 0:
            raise OSError("nonce not echoed in report")
        # report_data starts at 0x50 in recent report versions, 0x4C before that
        start = -1
        for rdoff in (0x50, 0x4C):
            cand = at - rdoff
            if cand >= 0 and int.from_bytes(raw[cand:cand + 4], "little") in (2, 3, 4, 5):
                start = cand
                break
        if start < 0:
            raise OSError("could not locate the report header")
        # report_data is 64 bytes starting at the nonce; measurement follows it
        measurement = raw[at + 64: at + 64 + 48]
        if len(measurement) != 48 or measurement == b"\x00" * 48:
            raise OSError("empty measurement")
        # the driver returns {u32 status, u32 size, <header>, report}; keep the bare report
        size = int.from_bytes(raw[4:8], "little")
        report = raw[start: start + size] if 0 < size and start + size <= len(raw) else raw
        payload = {
            "available": True,
            "guest": "sev-snp",
            "measurement": measurement.hex(),
            "report_version": int.from_bytes(report[0:4], "little"),
            "report_size": len(report),
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "report_b64": base64.b64encode(report).decode("ascii"),
            "note": NOTE,
            **boot,
        }
    except (OSError, ValueError) as exc:
        payload["error"] = str(exc)

    write_document(args.out, payload)
    print("attestation -> %s (%s)" % (args.out, payload.get("measurement", payload.get("error", "unavailable"))[:32]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
