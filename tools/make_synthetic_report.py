#!/usr/bin/python3
"""Build the synthetic SEV-SNP fixtures the verifier tests run on.

The recorded reports in `tests/fixtures/verify/` used to be real silicon: the
64-byte chip id at report offset `0x1A0` and the AMD-issued VCEK's serial and
HWID named the HQ machine and one tenant. Those bytes are inside the CPU's and
AMD's signatures, so they could not be redacted; ODL-906-F3 allowlisted them and
this tool replaces them.

What it writes, into `tests/fixtures/verify/` by default:

    synthetic-report.json       one 1184-byte report, the v1 document
    synthetic-report-v2.json    the same signed bytes, plus release/cmdline/vcpus
    synthetic-demo.json         a report at the pin's public demo measurement
    synthetic-tenant.json       a report whose VCEK the KDS tests serve
    synthetic-vcek.der          the tenant report's VCEK, DER (the KDS payload)
    synthetic-amd/ark.pem       a locally generated P-384 root
    synthetic-amd/ask.pem       signed by that root
    synthetic-amd/vcek.pem      signed by that ASK, for the demo/report fixtures

The chip ids are `ODILE-SYNTHETIC-…` markers, not silicon. `tools/leak_scan.py`
knows the marker and does not report it; a real chip id planted in a fixture
still fails the gate.

The chain is a *test-only trust root*. Each JSON carries a `trust` block naming
its ARK SHA-256 and its chain directory; `odile-verify` honours that block only
for a local `--report` file, never for a document fetched over the network. The
shipped `odile-verify.pin` keeps AMD's ARK, and no fixture chain can satisfy it.

The output is committed. A re-run rotates the generated keys and certificates,
so run it only when the fixture set changes; the tests do not care which chain is
in the tree, only that the fixture names the chain it was built with.

    ./tools/make_synthetic_report.py
    ./tools/make_synthetic_report.py --out-dir /tmp/synthetic-fixtures

Standard library plus the `openssl` binary; no network, no state kept.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

REPORT_SIZE = 1184
SIGNED_SIZE = 0x2A0
SIG_R_OFF = 0x2A0
SIG_S_OFF = 0x2E8
SIG_SLOT = 72

POLICY = 0x30000
REPORT_VERSION = 5
GUEST_SVN = 3
SIGNATURE_ALGO = 1

# The fixture marker: 64 bytes, printable, and obviously not silicon. The
# scanner skips a chip id with this prefix and reports anything else.
CHIP_PREFIX = b"ODILE-SYNTHETIC-"
CHIP_MAIN = (CHIP_PREFIX + b"SEV-SNP-CHIP-ID-0001").ljust(64, b"\x00")
CHIP_TENANT = (CHIP_PREFIX + b"SEV-SNP-CHIP-ID-0002").ljust(64, b"\x00")

# reported_tcb is `blSPL, teeSPL, reserved[4], snpSPL, ucodeSPL`; the committed
# value is what the firmware committed, and the SNP floor is 27 (AMD-SB-3020).
REPORTED_TCB = bytes([4, 0, 0, 0, 0, 0, 23, 115])
COMMITTED_TCB = bytes([4, 0, 0, 0, 0, 0, 28, 115])

# A public key with no private half, in the shape the measured command line
# carries. Generated here rather than copied, so no recorded machine's key
# appears in a fixture. The 32-byte key body is 0x00..0x1f: recognisably a
# fixture, and a structurally valid ed25519 public key.
_KEY_BODY = bytes(range(32))
_KEY_BLOB = (struct.pack(">I", len(b"ssh-ed25519")) + b"ssh-ed25519"
             + struct.pack(">I", len(_KEY_BODY)) + _KEY_BODY)
SEAL_ID = "5e4d0c0ffeec0de1"
BOOT_KEY = base64.b64encode(
    b"ssh-ed25519 " + base64.b64encode(_KEY_BLOB) + b" odile@synthetic"
).decode("ascii")

DEMO_MEASUREMENT = bytes.fromhex(
    "e99728efbbf23ae517bde68b7667448aa98ed8905e48cddae037ff1742cd10f3"
    "116f98b3ece36aad2e4c8b9e72643fbc"
)
# The fixtures keep the recorded, published measurements: a measurement is not
# a machine identity, it is the number the pin and the runbooks already print.
# Only the chip id, the VCEK and the signature area are synthetic.
REPORT_MEASUREMENT = bytes.fromhex(
    "519c7c9aae54bf139cacd783e1b69bd98b01e79b9e8b52feb41aadb7a483f481"
    "227a8a3b64ea2b885234ca56e09c9ee8"
)
TENANT_MEASUREMENT = bytes.fromhex(
    "9f01725afd71968f74e6dec5f7768b9dfaf52a46cbcfe8a49e106b5cff940280"
    "80a47c3d7f56dd7c5c49e6781517f137"
)

REPORT_FETCHED_AT = "2026-11-06T13:33:07Z"
DEMO_FETCHED_AT = "2026-11-13T01:45:02Z"
TENANT_FETCHED_AT = "2026-11-13T01:13:51Z"

# The bench's throwaway boot key, a public key with no private half, already
# quoted by the access-report block. The tenant fixture keeps it so the page
# and the fixture quote the same report.
TENANT_BOOT_KEY = (
    "c3NoLWVkMjU1MTkg" "QUFBQUMzTnphQzFs" "WkRJMU5URTVBQUFB"
    "SURxRDZYUUpBdlhh" "alh4dHhYUHc0UWNi" "NHZoSldGdkphWklx"
    "L0huNVFQaVggb2Rp" "bGUtYmVuY2ggdGhy" "b3dhd2F5IGJvb3Qg"
    "a2V5Cg=="
)

TCB_OIDS = (
    ("1.3.6.1.4.1.3704.1.3.1", 0),
    ("1.3.6.1.4.1.3704.1.3.2", 1),
    ("1.3.6.1.4.1.3704.1.3.3", 6),
    ("1.3.6.1.4.1.3704.1.3.8", 7),
)


class GeneratorError(Exception):
    """The generator could not build or verify its own output."""


def _openssl(args: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess[bytes]:
    cp = subprocess.run(["openssl", *args], capture_output=True, cwd=cwd)
    if cp.returncode != 0:
        raise GeneratorError(
            "openssl %s failed: %s" % (
                " ".join(args), cp.stderr.decode("utf-8", "replace").strip())
        )
    return cp


def _der_tlv(data: bytes, offset: int) -> tuple[int, int, int]:
    """(tag, value offset, value length) for one DER TLV."""
    if offset + 2 > len(data):
        raise GeneratorError("truncated DER")
    tag, length = data[offset], data[offset + 1]
    header = 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or offset + 2 + count > len(data):
            raise GeneratorError("bad DER length")
        length = int.from_bytes(data[offset + 2:offset + 2 + count], "big")
        header = 2 + count
    start = offset + header
    if start + length > len(data):
        raise GeneratorError("truncated DER value")
    return tag, start, length


def _der_integers(der: bytes) -> tuple[int, int]:
    """The (r, s) of an ECDSA-Sig-Value, as integers."""
    tag, start, length = _der_tlv(der, 0)
    if tag != 0x30 or start + length != len(der):
        raise GeneratorError("the ECDSA signature is not a SEQUENCE")
    rtag, roff, rlen = _der_tlv(der, start)
    stag, soff, slen = _der_tlv(der, roff + rlen)
    if rtag != 0x02 or stag != 0x02:
        raise GeneratorError("the ECDSA signature is not two INTEGERs")
    return int.from_bytes(der[roff:roff + rlen], "big"), int.from_bytes(
        der[soff:soff + slen], "big")


def build_report(measurement: bytes, chip_id: bytes, label: str) -> bytes:
    """A 1184-byte SNP report; the caller signs the first 672 bytes."""
    if len(measurement) != 48:
        raise GeneratorError("a measurement is 48 bytes")
    if len(chip_id) != 64:
        raise GeneratorError("a chip id is 64 bytes")
    raw = bytearray(REPORT_SIZE)
    struct.pack_into("<I", raw, 0x00, REPORT_VERSION)
    struct.pack_into("<I", raw, 0x04, GUEST_SVN)
    struct.pack_into("<Q", raw, 0x08, POLICY)
    struct.pack_into("<I", raw, 0x30, 0)          # VMPL
    struct.pack_into("<I", raw, 0x34, SIGNATURE_ALGO)
    raw[0x50:0x90] = (b"ODILE-SYNTHETIC-REPORT-DATA-" + label.encode()).ljust(
        64, b"\x00")[:64]
    raw[0x90:0xC0] = measurement
    raw[0x140:0x160] = (b"ODILE-SYNTHETIC-REPORT-ID-" + label.encode()).ljust(
        32, b"\x00")[:32]
    raw[0x180:0x188] = REPORTED_TCB
    raw[0x1A0:0x1E0] = chip_id
    raw[0x1E0:0x1E8] = COMMITTED_TCB
    raw[0x200:0x208] = COMMITTED_TCB
    raw[0x210:0x218] = COMMITTED_TCB
    return bytes(raw)


def sign_report(raw: bytes, vcek_key: str, workdir: str) -> bytes:
    """Sign the 672-byte body and place r||s at 0x2A0 / 0x2E8."""
    body = os.path.join(workdir, "report.bin")
    sig = os.path.join(workdir, "report.sig")
    with open(body, "wb") as handle:
        handle.write(raw[:SIGNED_SIZE])
    _openssl(["dgst", "-sha384", "-sign", vcek_key, "-out", sig, body])
    with open(sig, "rb") as handle:
        der = handle.read()
    r, s = _der_integers(der)
    # The report stores r and s little-endian in two 72-byte slots; only the
    # first 48 bytes carry the P-384 value.
    signed = bytearray(raw)
    signed[SIG_R_OFF:SIG_R_OFF + 48] = r.to_bytes(48, "little")
    signed[SIG_S_OFF:SIG_S_OFF + 48] = s.to_bytes(48, "little")
    return bytes(signed)


def _extension_lines(chip_id: bytes) -> list[str]:
    lines = [
        "basicConstraints = critical,CA:FALSE",
        "keyUsage = critical,digitalSignature",
        "subjectKeyIdentifier = hash",
        "authorityKeyIdentifier = keyid:always",
        "1.3.6.1.4.1.3704.1.1 = DER:02:01:00",
        "1.3.6.1.4.1.3704.1.2 = DER:16:0E:"
        + ":".join("%02X" % b for b in b"Milan-B0-Synth"),
    ]
    for oid, index in TCB_OIDS:
        value = REPORTED_TCB[index]
        lines.append("%s = DER:02:01:%02X" % (oid, value))
    lines.append("1.3.6.1.4.1.3704.1.4 = DER:"
                 + ":".join("%02X" % b for b in chip_id))
    return lines


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)


def _read(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def generate_chain(workdir: str) -> tuple[str, str, str]:
    """(ark.pem, ask.pem, vcek.pem) for CHIP_MAIN, in `workdir`."""
    ext = os.path.join(workdir, "ext.cnf")
    dn = "/C=US/O=Odile Synthetic Fixture"
    with open(ext, "w", encoding="utf-8") as handle:
        handle.write(
            "[ req ]\n"
            "distinguished_name = dn\n"
            "prompt = no\n"
            "[ dn ]\n"
            "C = US\n"
            "O = Odile Synthetic Fixture\n"
            "CN = ARK-Milan-Synthetic\n"
            "[ v3_ark ]\n"
            "basicConstraints = critical,CA:TRUE\n"
            "keyUsage = critical,keyCertSign,cRLSign\n"
            "subjectKeyIdentifier = hash\n"
            "[ v3_ask ]\n"
            "basicConstraints = critical,CA:TRUE,pathlen:0\n"
            "keyUsage = critical,keyCertSign,cRLSign\n"
            "subjectKeyIdentifier = hash\n"
            "authorityKeyIdentifier = keyid:always\n"
            "[ v3_vcek ]\n"
            + "\n".join(_extension_lines(CHIP_MAIN)) + "\n"
        )
    keys = {}
    for name in ("ark", "ask", "vcek"):
        keys[name] = os.path.join(workdir, name + ".key")
        _openssl(["genpkey", "-algorithm", "EC",
                  "-pkeyopt", "ec_paramgen_curve:P-384", "-out", keys[name]])
    ark = os.path.join(workdir, "ark.pem")
    ask = os.path.join(workdir, "ask.pem")
    vcek = os.path.join(workdir, "vcek.pem")
    _openssl(["req", "-x509", "-new", "-key", keys["ark"], "-sha384",
              "-days", "3650", "-config", ext, "-extensions", "v3_ark",
              "-subj", dn + "/CN=ARK-Milan-Synthetic", "-set_serial", "3",
              "-out", ark])
    ask_csr = os.path.join(workdir, "ask.csr")
    _openssl(["req", "-new", "-key", keys["ask"], "-config", ext,
              "-subj", dn + "/CN=SEV-Milan-Synthetic", "-out", ask_csr])
    _openssl(["x509", "-req", "-in", ask_csr, "-CA", ark, "-CAkey", keys["ark"],
              "-set_serial", "2", "-sha384", "-days", "3650",
              "-extfile", ext, "-extensions", "v3_ask", "-out", ask])
    vcek_csr = os.path.join(workdir, "vcek.csr")
    _openssl(["req", "-new", "-key", keys["vcek"], "-config", ext,
              "-subj", dn + "/CN=SEV-VCEK-Synthetic", "-out", vcek_csr])
    _openssl(["x509", "-req", "-in", vcek_csr, "-CA", ask, "-CAkey", keys["ask"],
              "-set_serial", "1", "-sha384", "-days", "3650",
              "-extfile", ext, "-extensions", "v3_vcek", "-out", vcek])
    _openssl(["verify", "-CAfile", ark, "-untrusted", ask, vcek])
    return ark, ask, vcek


def generate_tenant_vcek(workdir: str, ask: str, ask_key: str) -> bytes:
    """The tenant chip's VCEK, DER: signed by the same synthetic ASK."""
    ext = os.path.join(workdir, "ext.cnf")
    text = _read(ext).decode("utf-8")
    text = re.sub(r"1\.3\.6\.1\.4\.1\.3704\.1\.4 = .*",
                  "1.3.6.1.4.1.3704.1.4 = DER:"
                  + ":".join("%02X" % b for b in CHIP_TENANT), text)
    tenant_ext = os.path.join(workdir, "tenant-ext.cnf")
    with open(tenant_ext, "w", encoding="utf-8") as handle:
        handle.write(text)
    key = os.path.join(workdir, "tenant-vcek.key")
    _openssl(["genpkey", "-algorithm", "EC",
              "-pkeyopt", "ec_paramgen_curve:P-384", "-out", key])
    csr = os.path.join(workdir, "tenant-vcek.csr")
    _openssl(["req", "-new", "-key", key, "-config", tenant_ext,
              "-subj", "/C=US/O=Odile Synthetic Fixture/CN=SEV-VCEK-Synthetic-Tenant",
              "-out", csr])
    pem = os.path.join(workdir, "tenant-vcek.pem")
    _openssl(["x509", "-req", "-in", csr, "-CA", ask, "-CAkey", ask_key,
              "-set_serial", "4", "-sha384", "-days", "3650",
              "-extfile", tenant_ext, "-extensions", "v3_vcek", "-out", pem])
    return _openssl(["x509", "-in", pem, "-outform", "DER"]).stdout


def padded_cmdline(release: str, total: int, boot_key: str = BOOT_KEY) -> str:
    """A measured command line of exactly `total` bytes before initrd.

    The real fixture's cmdline is 1144 bytes; `build_record` hashes exactly
    that prefix. The cloud-config is padded with YAML comments, which are legal
    and do not change the meaning of the document.
    """
    head = (
        "console=tty0 console=ttyS0,115200 consoleblank=0 "
        "root=/dev/mapper/root_crypt rootfstype=ext4 ro "
        "cryptopts=target=root_crypt,source=/dev/sda3,luks,discard "
        "ip=192.0.2.10::192.0.2.1:255.255.255.0:synthetic:ens18:off "
        "ds=nocloud;s=/run/blackbox/seed/ "
        "bb.release=%s bb.iid=cafebabe bb.host=hostfake "
        "bb.ip4=192.0.2.10/24 bb.gw4=192.0.2.1 "
        "bb.ip6=2001:db8:0:1::10/64 bb.gw6=2001:db8::1 "
        "bb.dns=1.1.1.1,9.9.9.9,2606:4700:4700::1111,2620:fe::fe "
        "bb.key=%s seal_id=%s " % (release, boot_key, SEAL_ID)
    )
    tail = " end_cc"
    prefix = "network-config="
    room = total - len(head) - len(prefix) - len(tail)
    if room <= 0:
        raise GeneratorError("the cmdline budget is negative")
    # base64 length is a multiple of four; a bare padding token absorbs the
    # remainder so the value itself stays well formed.
    remainder = room % 4
    if remainder:
        head += "x" * remainder
        room -= remainder
    # cloud-config YAML plus comment padding, base64-encoded without newlines.
    yaml = "#cloud-config\nnetwork:\n  version: 2\n"
    pad = 0
    while True:
        body = (yaml + "#" + " " * pad + "\n").encode("utf-8")
        encoded = base64.b64encode(body).decode("ascii")
        if len(encoded) >= room:
            break
        pad += 1
    if len(encoded) != room:
        raise GeneratorError("the cmdline value is %d bytes, expected %d"
                             % (len(encoded), room))
    line = head + prefix + encoded + tail + " initrd=initrd"
    if len(line) - len(" initrd=initrd") != total:
        raise GeneratorError("the cmdline is %d bytes, expected %d"
                             % (len(line) - len(" initrd=initrd"), total))
    return line


def build_document(raw: bytes, *, release: str | None = None,
                   cmdline: str | None = None, vcpus: int | None = None,
                   fetched_at: str, trust: dict[str, str], note: str) -> dict[str, object]:
    measurement = raw[0x90:0xC0].hex()
    doc: dict[str, object] = {
        "available": True,
        "guest": "sev-snp",
        "measurement": measurement,
        "report_version": REPORT_VERSION,
        "report_size": REPORT_SIZE,
        "fetched_at": fetched_at,
        "report_b64": base64.b64encode(raw).decode("ascii"),
        "note": note,
        "trust": trust,
    }
    if release is not None:
        doc["release"] = release
    if cmdline is not None:
        doc["cmdline"] = cmdline
    if vcpus is not None:
        doc["vcpus"] = vcpus
    return doc


def _write_json(path: str, doc: dict[str, object]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2)
        handle.write("\n")


def generate(out_dir: str) -> list[str]:
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    chain_dirname = "synthetic-amd"
    chain_dir = os.path.join(out_dir, chain_dirname)
    if os.path.isdir(chain_dir):
        shutil.rmtree(chain_dir)
    os.makedirs(chain_dir)

    workdir = tempfile.mkdtemp(prefix="odile-synthetic-")
    try:
        ark, ask, vcek = generate_chain(workdir)
        ark_hash = _openssl(
            ["x509", "-in", ark, "-noout", "-fingerprint", "-sha256"]
        ).stdout.decode("ascii").split("=", 1)[-1].replace(":", "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", ark_hash):
            raise GeneratorError("cannot read the synthetic ARK fingerprint")
        trust = {"amd_dir": chain_dirname, "ark_sha256": ark_hash,
                 "note": "test-only trust root; the shipped pin keeps AMD's ARK"}
        _write(os.path.join(chain_dir, "ark.pem"), _read(ark))
        _write(os.path.join(chain_dir, "ask.pem"), _read(ask))
        _write(os.path.join(chain_dir, "vcek.pem"), _read(vcek))

        tenant_der = generate_tenant_vcek(workdir, ask,
                                          os.path.join(workdir, "ask.key"))
        _write(os.path.join(out_dir, "synthetic-vcek.der"), tenant_der)

        report_raw = sign_report(
            build_report(REPORT_MEASUREMENT, CHIP_MAIN, "REPORT"),
            os.path.join(workdir, "vcek.key"), workdir)
        demo_raw = sign_report(
            build_report(DEMO_MEASUREMENT, CHIP_MAIN, "DEMO"),
            os.path.join(workdir, "vcek.key"), workdir)
        tenant_raw = sign_report(
            build_report(TENANT_MEASUREMENT, CHIP_TENANT, "TENANT"),
            os.path.join(workdir, "tenant-vcek.key"), workdir)

        v1 = build_document(
            report_raw, fetched_at=REPORT_FETCHED_AT, trust=trust,
            note="synthetic SEV-SNP report fixture; no silicon id")
        _write_json(os.path.join(out_dir, "synthetic-report.json"), v1)
        v2 = build_document(
            report_raw, release="2026.11.1",
            cmdline=padded_cmdline("2026.11.1", 1144), vcpus=4,
            fetched_at=REPORT_FETCHED_AT, trust=trust,
            note="synthetic SEV-SNP report fixture (release and cmdline added)")
        _write_json(os.path.join(out_dir, "synthetic-report-v2.json"), v2)
        demo = build_document(
            demo_raw, release="2026.11.2",
            cmdline=padded_cmdline("2026.11.2", 1144), vcpus=6,
            fetched_at=DEMO_FETCHED_AT, trust=trust,
            note="synthetic demo fixture at the pin's public measurement")
        _write_json(os.path.join(out_dir, "synthetic-demo.json"), demo)
        tenant = build_document(
            tenant_raw, release="2026.11.2",
            cmdline=padded_cmdline("2026.11.2", 1144, TENANT_BOOT_KEY), vcpus=4,
            fetched_at=TENANT_FETCHED_AT, trust=trust,
            note="synthetic tenant fixture; its VCEK is synthetic-vcek.der")
        _write_json(os.path.join(out_dir, "synthetic-tenant.json"), tenant)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return [
        os.path.join(out_dir, "synthetic-amd", "ark.pem"),
        os.path.join(out_dir, "synthetic-amd", "ask.pem"),
        os.path.join(out_dir, "synthetic-amd", "vcek.pem"),
        os.path.join(out_dir, "synthetic-vcek.der"),
        os.path.join(out_dir, "synthetic-report.json"),
        os.path.join(out_dir, "synthetic-report-v2.json"),
        os.path.join(out_dir, "synthetic-demo.json"),
        os.path.join(out_dir, "synthetic-tenant.json"),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the synthetic SEV-SNP report and VCEK fixtures.")
    parser.add_argument(
        "--out-dir", default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tests", "fixtures", "verify"),
        help="where to write the fixtures (default: tests/fixtures/verify)")
    args = parser.parse_args(argv)
    try:
        paths = generate(args.out_dir)
    except GeneratorError as exc:
        print("make_synthetic_report: %s" % exc, file=sys.stderr)
        return 1
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
