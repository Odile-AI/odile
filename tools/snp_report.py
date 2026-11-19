#!/usr/bin/env python3
"""Parse and verify an AMD SEV-SNP attestation report.

Standard library plus the `openssl` binary. No pip, no state. The default
checks read AMD's Milan ASK and ARK from `tests/fixtures/amd/`; the VCEK names
one chip and one TCB, so it is fetched for the report (`install_tenant_chain`)
or supplied in `--amd-dir`. `verify_chain` compares the ARK to the pinned
`ARK_SHA256`, unless the caller passes a test-only `ark_sha256` (the synthetic
fixtures name their own root).

The report is 1184 bytes. The first 672 (0x2A0) are the signed payload; the
signature area follows and holds two 72-byte little-endian integers, r and s,
for ECDSA P-384 / SHA-384. Only the first 48 bytes of each slot carry the
P-384 value; the rest is zero padding.

The verifier checks, in this order:

  1. the certificate chain: VCEK -> ASK -> ARK, with the ARK SHA-256
     fingerprint pinned in this file (AMD Milan root);
  2. the VCEK names this report's chip id and TCB (the certificate is not
     for some other machine);
  3. the ECDSA signature over the report's signed bytes;
  4. the guest policy is exactly 0x30000 (SMT allowed, reserved bit set,
     DEBUG and MIGRATE_MA clear);
  5. the committed TCB is at or above the AMD-SB-3020 SNP SPL floor;
  6. the report version, size, VMPL and signature algorithm.

What this proves is narrow and worth stating: the CPU that produced this
report signed these bytes, the signing key chains to the pinned AMD root, and
the signing key belongs to the chip named in the report. What it does not
prove is anything about liveness or about the honesty of the boot image.

    ./tools/snp_report.py report.bin [--amd-dir tests/fixtures/amd]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

REPORT_SIZE = 1184
SIGNED_SIZE = 0x2A0
SIG_R_OFF = 0x2A0
SIG_S_OFF = 0x2E8
SIG_SLOT = 72

REPORT_VERSIONS = (2, 3, 5)
POLICY_EXPECTED = 0x30000
SIGNATURE_ALGO_ECDSA_P384_SHA384 = 1

# Minimum committed SNP SPL for Milan, from AMD-SB-3020. The hypervisor may
# lower `reported_tcb` (SNP_SET_REPORTED_TCB), so a floor on the reported value
# is not a floor at all; this checks the firmware-committed value instead.
# Revisit when AMD publishes a newer advisory.
MIN_SNP_SPL = 0x1B

# SHA-256 of AMD's Milan ARK (DER), as printed by
#   openssl x509 -in ark.pem -noout -fingerprint -sha256
# Compare it out of band with AMD's published root before trusting this file.
ARK_SHA256 = "69d063b45344d26a2e94e1f4210de49ef555308287d4c174445c95639a540bcd"

OID_HWID = "1.3.6.1.4.1.3704.1.4"
# VCEK TCB extensions, in report-byte order: blSPL, teeSPL, snpSPL, ucodeSPL.
OID_TCB = {
    "1.3.6.1.4.1.3704.1.3.1": 0,
    "1.3.6.1.4.1.3704.1.3.2": 1,
    "1.3.6.1.4.1.3704.1.3.3": 6,
    "1.3.6.1.4.1.3704.1.3.8": 7,
}

DEFAULT_AMD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests", "fixtures", "amd",
)

# AMD's Key Distribution Service. A VCEK names one chip and one TCB, so the
# repository does not vendor one; a run fetches the VCEK for the report's raw
# chip id and reported TCB (the KDS hwid segment is the 64-byte chip id in hex,
# never a digest of it). The ASK and ARK come from the repository or, if absent,
# from the KDS chain — and `verify_chain` always compares the ARK to the pinned
# fingerprint, so a substituted root fails.
KDS_BASE = "https://kdsintf.amd.com"
KDS_PRODUCT = "Milan"
KDS_TIMEOUT = 30
KDS_ATTEMPTS = 3
KDS_MAX_BYTES = 64 * 1024


class ReportError(Exception):
    """The report, the chain or the openssl call did not verify."""


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _u32(raw: bytes, off: int) -> int:
    return struct.unpack_from("<I", raw, off)[0]


def _u64(raw: bytes, off: int) -> int:
    return struct.unpack_from("<Q", raw, off)[0]


class Report:
    """A parsed 1184-byte SNP attestation report."""

    def __init__(self, raw: bytes):
        if len(raw) != REPORT_SIZE:
            raise ReportError(
                "report is %d bytes; a SEV-SNP report is %d" % (len(raw), REPORT_SIZE)
            )
        self.raw = raw
        self.version = _u32(raw, 0x00)
        self.guest_svn = _u32(raw, 0x04)
        self.policy = _u64(raw, 0x08)
        self.vmpl = _u32(raw, 0x30)
        self.signature_algo = _u32(raw, 0x34)
        self.report_data = raw[0x50:0x90]
        self.measurement = raw[0x90:0xC0]
        self.host_data = raw[0xC0:0xE0]
        self.reported_tcb = raw[0x180:0x188]
        self.chip_id = raw[0x1A0:0x1E0]
        self.committed_tcb = raw[0x1E0:0x1E8]
        self.signed = raw[:SIGNED_SIZE]
        self.sig_r = raw[SIG_R_OFF:SIG_R_OFF + SIG_SLOT]
        self.sig_s = raw[SIG_S_OFF:SIG_S_OFF + SIG_SLOT]

    @property
    def measurement_hex(self) -> str:
        return self.measurement.hex()

    def tcb(self, field: bytes | None = None) -> str:
        b = self.reported_tcb if field is None else field
        return "bl=%d tee=%d snp=%d ucode=%d" % (b[0], b[1], b[6], b[7])


# ------------------------------------------------------------------ openssl

def _openssl(args: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["openssl", *args],
            input=stdin,
            capture_output=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise ReportError("openssl binary not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReportError("openssl timed out") from exc
    except OSError as exc:
        raise ReportError("openssl failed: %s" % exc) from exc


def _output(cp: subprocess.CompletedProcess) -> str:
    text = (cp.stdout + cp.stderr).decode("utf-8", "replace").strip()
    return text.splitlines()[0] if text else "no openssl output"


def _require_file(path: str, what: str) -> str:
    if not os.path.isfile(path):
        raise ReportError("%s not found: %s" % (what, path))
    return path


# ---------------------------------------------------------------- AMD KDS

def kds_vcek_url(report: Report, base: str = KDS_BASE,
                 product: str = KDS_PRODUCT) -> str:
    """The KDS URL for this report's own chip id and reported TCB.

    The ``hwid`` segment is the report's 64-byte chip id in hex (128
    characters). ``sha256(chip_id)`` is 64 hex characters, is not what the KDS
    takes, and answers ``400 Bad Request``.
    """
    tcb = report.reported_tcb
    query = urllib.parse.urlencode({
        "blSPL": tcb[0],
        "teeSPL": tcb[1],
        "snpSPL": tcb[6],
        "ucodeSPL": tcb[7],
    })
    return "%s/vcek/v1/%s/%s?%s" % (
        base.rstrip("/"), product, report.chip_id.hex(), query)


def _kds_body(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read(200).decode("utf-8", "replace").strip()
    except OSError:
        return ""
    return body.replace("\n", " ")[:200]


def _kds_get(url: str, *, timeout: int = KDS_TIMEOUT,
             attempts: int = KDS_ATTEMPTS) -> bytes:
    """Fetch one KDS object; retry the codes the KDS is known to emit under load.

    The KDS rate-limits and sometimes answers ``400`` instead of ``429``; a
    persistent ``400`` means the TCB tuple cannot be encoded. ``404`` means the
    chip id is not known and is not retried.
    """
    last = "AMD KDS request failed"
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                if response.status != 200:
                    raise ReportError(
                        "AMD KDS returned HTTP %d for %s" % (response.status, url))
                body = response.read(KDS_MAX_BYTES + 1)
            if len(body) > KDS_MAX_BYTES:
                raise ReportError(
                    "AMD KDS returned more than %d bytes for %s"
                    % (KDS_MAX_BYTES, url))
            return body
        except urllib.error.HTTPError as exc:
            detail = _kds_body(exc)
            last = "AMD KDS returned HTTP %d for %s%s" % (
                exc.code, url, (": " + detail) if detail else "")
            if exc.code in (400, 429, 503) and attempt < attempts:
                time.sleep(attempt)
                continue
            raise ReportError(last) from exc
        except urllib.error.URLError as exc:
            last = "AMD KDS is unreachable at %s: %s" % (url, exc.reason)
            if attempt < attempts:
                time.sleep(attempt)
                continue
            raise ReportError(last) from exc
        except OSError as exc:
            last = "AMD KDS request to %s failed: %s" % (url, exc)
            if attempt < attempts:
                time.sleep(attempt)
                continue
            raise ReportError(last) from exc
    raise ReportError(last)


def _pem_certificate(body: bytes) -> bytes:
    """The KDS answers DER for a VCEK; accept PEM too and fail closed on neither."""
    if body.lstrip().startswith(b"-----BEGIN CERTIFICATE-----"):
        text = body.strip()
        return text + b"\n"
    try:
        return ssl.DER_cert_to_PEM_cert(body).encode("ascii")
    except ValueError as exc:
        raise ReportError(
            "AMD KDS did not return a DER or PEM certificate: %s" % exc) from exc


def _split_chain(chain_pem: bytes) -> tuple[bytes, bytes]:
    """Return (ASK, ARK) from the KDS chain: the certificate order is ASK then ARK."""
    certs = re.findall(
        rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        chain_pem, re.S)
    if len(certs) != 2:
        raise ReportError(
            "AMD KDS cert_chain carried %d certificates; expected ASK and ARK"
            % len(certs))
    return certs[0] + b"\n", certs[1] + b"\n"


def install_tenant_chain(report: Report, dest_dir: str, *,
                         fallback_dir: str | None = None,
                         base: str = KDS_BASE,
                         product: str = KDS_PRODUCT,
                         timeout: int = KDS_TIMEOUT,
                         attempts: int = KDS_ATTEMPTS) -> tuple[str, str]:
    """Write ark.pem, ask.pem and vcek.pem for this report into ``dest_dir``.

    The VCEK is fetched from AMD's KDS for the report's raw chip id and
    reported TCB. The ASK and ARK come from ``fallback_dir`` when both are
    present (the repository's pinned roots); otherwise they are fetched from
    the KDS chain. ``verify_chain`` compares the ARK to the pinned fingerprint
    either way, so a substituted root still fails.

    Returns ``(vcek_url, sha256_of_the_der)``.
    """
    os.makedirs(dest_dir, exist_ok=True)
    ark_src = os.path.join(fallback_dir, "ark.pem") if fallback_dir else None
    ask_src = os.path.join(fallback_dir, "ask.pem") if fallback_dir else None
    if (ark_src and os.path.isfile(ark_src)
            and ask_src and os.path.isfile(ask_src)):
        with open(ark_src, "rb") as handle:
            ark = handle.read()
        with open(ask_src, "rb") as handle:
            ask = handle.read()
    else:
        chain = _kds_get(
            "%s/vcek/v1/%s/cert_chain" % (base.rstrip("/"), product),
            timeout=timeout, attempts=attempts)
        ask, ark = _split_chain(chain)
    with open(os.path.join(dest_dir, "ark.pem"), "wb") as handle:
        handle.write(ark)
    with open(os.path.join(dest_dir, "ask.pem"), "wb") as handle:
        handle.write(ask)

    url = kds_vcek_url(report, base, product)
    der = _kds_get(url, timeout=timeout, attempts=attempts)
    vcek_path = os.path.join(dest_dir, "vcek.pem")
    with open(vcek_path, "wb") as handle:
        handle.write(_pem_certificate(der))
    # `ssl.DER_cert_to_PEM_cert` only wraps base64; it does not parse. An
    # openssl round-trip is what proves the KDS answered a certificate.
    cp = _openssl(["x509", "-in", vcek_path, "-noout"])
    if cp.returncode != 0:
        raise ReportError(
            "AMD KDS did not return a certificate: " + _output(cp))
    return url, hashlib.sha256(der).hexdigest()


def vcek_binds(report: Report, amd_dir: str) -> tuple[bool, str]:
    """Does the chain in ``amd_dir`` carry the VCEK for this report's chip and TCB?"""
    try:
        return True, verify_vcek_binding(report, amd_dir)
    except ReportError as exc:
        return False, str(exc)


# ------------------------------------------------------------------- checks

def verify_chain(amd_dir: str, ark_sha256: str = ARK_SHA256) -> str:
    """Verify VCEK -> ASK -> ARK and pin the ARK fingerprint.

    ``ark_sha256`` is AMD's Milan root by default. The synthetic test fixtures
    name their own test-only root; the caller passes that value explicitly, so
    no remote document can choose the trust root.
    """
    ark = _require_file(os.path.join(amd_dir, "ark.pem"), "ARK")
    ask = _require_file(os.path.join(amd_dir, "ask.pem"), "ASK")
    vcek = _require_file(os.path.join(amd_dir, "vcek.pem"), "VCEK")

    cp = _openssl(["verify", "-CAfile", ark, "-untrusted", ask, vcek])
    if cp.returncode != 0:
        raise ReportError("certificate chain does not verify: " + _output(cp))

    cp = _openssl(["x509", "-in", ark, "-noout", "-fingerprint", "-sha256"])
    if cp.returncode != 0:
        raise ReportError("cannot read the ARK fingerprint: " + _output(cp))
    text = cp.stdout.decode("ascii", "replace")
    actual = text.split("=", 1)[-1].replace(":", "").strip().lower()
    expected = ark_sha256.lower()
    if actual != expected:
        raise ReportError(
            "ARK fingerprint is %s, expected %s; the root is not the pinned one"
            % (actual or "unreadable", expected)
        )
    return "VCEK -> ASK -> ARK verifies; ARK SHA-256 %s" % _colon(actual)


def _colon(digest: str) -> str:
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def _asn1_extensions(path: str) -> dict[str, bytes]:
    """Return {OID: raw OCTET STRING bytes} from an X.509 certificate.

    `openssl x509 -text` prints custom extension values ambiguously, so this
    walks the DER with `openssl asn1parse`, which prints the bytes as hex.
    The parser is deliberately narrow: an OBJECT line followed by an OCTET
    STRING line. Anything else is ignored.
    """
    cp = _openssl(["asn1parse", "-in", path, "-i"])
    if cp.returncode != 0:
        raise ReportError("cannot parse the VCEK certificate: " + _output(cp))
    lines = cp.stdout.decode("utf-8", "replace").splitlines()
    found: dict[str, bytes] = {}
    pending: str | None = None
    for line in lines:
        if "OBJECT" in line and ":1.3.6.1.4.1.3704." in line:
            pending = line.rsplit(":", 1)[-1].strip()
            continue
        if pending and "OCTET STRING" in line and "[HEX DUMP]:" in line:
            raw = line.split("[HEX DUMP]:", 1)[1].strip()
            try:
                found[pending] = bytes.fromhex(raw)
            except ValueError as exc:
                raise ReportError("bad extension bytes for %s" % pending) from exc
            pending = None
    return found


def _der_int(raw: bytes) -> int:
    """Decode the DER INTEGER inside a custom extension (02 len value...)."""
    if len(raw) < 3 or raw[0] != 0x02:
        raise ReportError("extension is not a DER INTEGER: %s" % raw.hex())
    length = raw[1]
    if length & 0x80:
        raise ReportError("long-form DER length is not expected here")
    if len(raw) != 2 + length:
        raise ReportError("truncated DER INTEGER: %s" % raw.hex())
    return int.from_bytes(raw[2:], "big")


def verify_vcek_binding(report: Report, amd_dir: str) -> str:
    """Check the VCEK names this chip id and this reported TCB."""
    vcek = _require_file(os.path.join(amd_dir, "vcek.pem"), "VCEK")
    ext = _asn1_extensions(vcek)
    hwid = ext.get(OID_HWID)
    if hwid is None:
        raise ReportError("the VCEK has no AMD HWID extension")
    if hwid != report.chip_id:
        raise ReportError(
            "the VCEK is for chip %s, the report names %s"
            % (hwid.hex(), report.chip_id.hex())
        )
    for oid, index in OID_TCB.items():
        value = ext.get(oid)
        if value is None:
            raise ReportError("the VCEK has no TCB extension %s" % oid)
        got = _der_int(value)
        want = report.reported_tcb[index]
        if got != want:
            raise ReportError(
                "the VCEK is for TCB %s, the report says %s; refresh the fixture"
                % (got, report.tcb())
            )
    return "the VCEK names this chip and TCB (%s)" % report.tcb()


def _der_len(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    body = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _der_integer(value: int) -> bytes:
    if value < 0:
        raise ReportError("negative ECDSA integer")
    body = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
    if body[0] & 0x80:
        body = b"\x00" + body
    return b"\x02" + _der_len(len(body)) + body


def signature_der(report: Report) -> bytes:
    """Convert the report's little-endian r || s slots to a DER signature."""
    r = int.from_bytes(report.sig_r, "little")
    s = int.from_bytes(report.sig_s, "little")
    if r == 0 or s == 0:
        raise ReportError("the report signature is empty")
    body = _der_integer(r) + _der_integer(s)
    return b"\x30" + _der_len(len(body)) + body


def verify_signature(report: Report, amd_dir: str) -> str:
    """Verify ECDSA P-384 / SHA-384 over the signed report bytes."""
    vcek = _require_file(os.path.join(amd_dir, "vcek.pem"), "VCEK")
    cp = _openssl(["x509", "-in", vcek, "-pubkey", "-noout"])
    if cp.returncode != 0:
        raise ReportError("cannot read the VCEK public key: " + _output(cp))
    public_key = cp.stdout

    with tempfile.TemporaryDirectory(prefix="odile-verify-") as tmp:
        signed_path = os.path.join(tmp, "signed.bin")
        sig_path = os.path.join(tmp, "signature.der")
        pub_path = os.path.join(tmp, "vcek.pub")
        with open(signed_path, "wb") as f:
            f.write(report.signed)
        with open(sig_path, "wb") as f:
            f.write(signature_der(report))
        with open(pub_path, "wb") as f:
            f.write(public_key)
        cp = _openssl(
            ["dgst", "-sha384", "-verify", pub_path, "-signature", sig_path, signed_path]
        )
    if cp.returncode != 0 or b"Verified OK" not in cp.stdout:
        raise ReportError("the report signature does not verify with the VCEK")
    return "ECDSA P-384 over the 672-byte report verifies with the VCEK"


def check_policy(report: Report) -> str:
    if report.policy != POLICY_EXPECTED:
        debug = bool(report.policy & (1 << 19))
        migrate = bool(report.policy & (1 << 18))
        raise ReportError(
            "policy is 0x%x, expected 0x%x (DEBUG=%s, MIGRATE_MA=%s)"
            % (report.policy, POLICY_EXPECTED, "set" if debug else "clear",
               "set" if migrate else "clear")
        )
    return "policy 0x%x: SMT allowed, DEBUG clear, migration agent not permitted" % report.policy


def check_header(report: Report) -> str:
    if report.version not in REPORT_VERSIONS:
        raise ReportError("report version %d is not one of %s"
                          % (report.version, REPORT_VERSIONS))
    if report.vmpl != 0:
        raise ReportError("VMPL is %d; a report from a lower-privilege layer is not the guest"
                          % report.vmpl)
    if report.signature_algo != SIGNATURE_ALGO_ECDSA_P384_SHA384:
        raise ReportError("signature algorithm %d is not ECDSA P-384 / SHA-384"
                          % report.signature_algo)
    return "version %d, %d bytes, VMPL %d, ECDSA P-384 / SHA-384" % (
        report.version, len(report.raw), report.vmpl)


def check_tcb(report: Report) -> str:
    if report.version == 2:
        return "version 2 has no committed TCB field; not checked"
    committed = report.committed_tcb
    if not any(committed):
        raise ReportError("committed_tcb is zero")
    snp = committed[6]
    if snp < MIN_SNP_SPL:
        raise ReportError(
            "committed SNP SPL %d is below the minimum %d (AMD-SB-3020)"
            % (snp, MIN_SNP_SPL)
        )
    return "committed %s; SNP SPL %d >= %d (AMD-SB-3020)" % (
        report.tcb(committed), snp, MIN_SNP_SPL)


def run_checks(report: Report, amd_dir: str = DEFAULT_AMD_DIR,
               ark_sha256: str = ARK_SHA256) -> list[Check]:
    """Run every check; a failure is a result, not an early return."""
    checks: list[tuple[str, Callable[[], str]]] = [
        ("chain", lambda: verify_chain(amd_dir, ark_sha256)),
        ("cpu binding", lambda: verify_vcek_binding(report, amd_dir)),
        ("signature", lambda: verify_signature(report, amd_dir)),
        ("policy", lambda: check_policy(report)),
        ("tcb", lambda: check_tcb(report)),
        ("report", lambda: check_header(report)),
    ]
    results: list[Check] = []
    for name, fn in checks:
        try:
            results.append(Check(name, True, fn()))
        except ReportError as exc:
            results.append(Check(name, False, str(exc)))
    return results


# ---------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse and verify a raw AMD SEV-SNP report (1184 bytes).",
    )
    parser.add_argument("report", help="path to the raw report binary")
    parser.add_argument("--amd-dir", default=DEFAULT_AMD_DIR,
                        help="directory holding ark.pem, ask.pem and vcek.pem")
    parser.add_argument("--ark-sha256", default=ARK_SHA256,
                        help="hex SHA-256 the ARK must have (default: AMD's Milan root)")
    parser.add_argument("--kds-base", default=KDS_BASE,
                        help="AMD KDS base URL (default: %(default)s)")
    parser.add_argument("--no-kds", action="store_true",
                        help="never fetch a VCEK from AMD's KDS; use --amd-dir only")
    args = parser.parse_args(argv)
    ark_sha256 = args.ark_sha256.replace(":", "").lower()
    if len(ark_sha256) != 64 or any(c not in "0123456789abcdef" for c in ark_sha256):
        parser.error("--ark-sha256 wants 64 hex characters")

    try:
        with open(args.report, "rb") as f:
            report = Report(f.read())
    except OSError as exc:
        print("FAIL: cannot read %s: %s" % (args.report, exc), file=sys.stderr)
        return 2

    print("measurement %s" % report.measurement_hex)
    print("reported_tcb %s" % report.tcb())
    print("committed_tcb %s" % report.tcb(report.committed_tcb))
    print("chip_id %s" % report.chip_id.hex())
    print("policy 0x%x" % report.policy)

    failed = False
    amd_dir = args.amd_dir
    binds, bind_detail = vcek_binds(report, amd_dir)
    if not binds and not args.no_kds:
        try:
            with tempfile.TemporaryDirectory(prefix="snp-report-kds-") as kds_dir:
                url, digest = install_tenant_chain(
                    report, kds_dir, fallback_dir=args.amd_dir,
                    base=args.kds_base)
                checks = run_checks(report, kds_dir, ark_sha256)
            print("kds %s" % url)
            print("vcek PASS  fetched the report's own VCEK from AMD KDS (sha256 %s)"
                  % digest)
        except ReportError as exc:
            print("vcek FAIL  %s" % exc)
            failed = True
            checks = run_checks(report, amd_dir, ark_sha256)
    elif not binds:
        print("vcek FAIL  the chain in --amd-dir does not bind the report and "
              "--no-kds is set: %s" % bind_detail)
        failed = True
        checks = run_checks(report, amd_dir, ark_sha256)
    else:
        checks = run_checks(report, amd_dir, ark_sha256)

    for check in checks:
        print("%-11s %s  %s" % (check.name, "PASS" if check.ok else "FAIL", check.detail))
        failed = failed or not check.ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
