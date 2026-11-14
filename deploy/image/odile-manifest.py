#!/usr/bin/python3
"""odile-manifest — check the signed manifest pair, then serve it (ODL-472).

`scripts/manifest.py serve` answers `/manifest` with the file's exact bytes and
does not verify them: it is a listener, not a gate. This wrapper is the gate in
front of it, and it is the only program the first boot and the manifest unit
run:

  * `--check` decodes the release's base64 signature, verifies it over the
    exact manifest bytes with Odile's published key, and exits 0 or 1 with one
    line. The first boot runs it before it installs the pair; the unit runs it
    in `ExecStartPre`, so a tampered pair never reaches a listening socket.
  * `serve` runs the same check and then execs the release listener, so the
    bytes on the wire are the bytes that verified — no re-serialisation, no
    second reader of the signature.

The pair the first boot installs is the release's own pair (`manifest.json`
and `manifest.json.sig` from release/, byte for byte), which is why the
signature is base64 and not raw: it is the file a stranger downloads.

    /usr/bin/python3 odile-manifest.py --check \
        --manifest /etc/odile/manifest.json \
        --signature /etc/odile/manifest.json.sig \
        --pubkey /etc/odile/odile-signing-key.pub
    /usr/bin/python3 odile-manifest.py serve --program /usr/local/lib/odile/manifest.py \
        --manifest /etc/odile/manifest.json \
        --signature /etc/odile/manifest.json.sig \
        --pubkey /etc/odile/odile-signing-key.pub --bind 127.0.0.1 --port 8012

Exit codes: 0 the pair verifies, 1 the pair is missing, malformed or does not
verify, 2 bad usage. Standard library only; the system OpenSSL does the
Ed25519 arithmetic.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import os
import subprocess
import sys
import tempfile

VERSION = "1.0.0"
OPENSSL = "/usr/bin/openssl"


class Refused(Exception):
    """A pair that must not be served; the message is the one console line."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bytes(path: str, what: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as error:
        raise Refused("the %s %s is missing: %s" % (what, path, error.strerror or error)) from None


def signature_bytes(path: str) -> bytes:
    """The base64 detached signature the release ships, as its 64 raw bytes."""
    try:
        text = read_bytes(path, "manifest signature").decode("ascii")
    except UnicodeDecodeError:
        raise Refused("the manifest signature %s is not base64" % path) from None
    try:
        signature = base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, ValueError):
        raise Refused("the manifest signature %s is not base64" % path) from None
    if len(signature) != 64:
        raise Refused("the manifest signature %s is %d bytes, not 64"
                      % (path, len(signature)))
    return signature


def verify(manifest: bytes, signature: bytes, pubkey: str) -> bool:
    if not os.path.isfile(pubkey):
        raise Refused("the signing key %s is missing" % pubkey)
    handle, sig_path = tempfile.mkstemp(prefix="odile-manifest-sig-")
    try:
        with os.fdopen(handle, "wb") as temporary:
            temporary.write(signature)
        try:
            proc = subprocess.run(
                [OPENSSL, "dgst", "-verify", pubkey, "-signature", sig_path],
                input=manifest, capture_output=True, timeout=30,
            )
        except FileNotFoundError:
            raise Refused("openssl is missing; cannot verify the manifest") from None
    finally:
        try:
            os.unlink(sig_path)
        except OSError:
            pass
    return proc.returncode == 0


def fingerprint(pubkey: str) -> str:
    """The key's SHA256 over its SPKI DER, as scripts/manifest.py spells it."""
    try:
        proc = subprocess.run(
            [OPENSSL, "pkey", "-pubin", "-in", pubkey, "-outform", "DER"],
            capture_output=True, timeout=30,
        )
    except FileNotFoundError:
        return "-"
    if proc.returncode != 0 or not proc.stdout:
        return "-"
    digest = hashlib.sha256(proc.stdout).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def check_pair(manifest_path: str, signature_path: str, pubkey: str) -> tuple[bytes, bytes]:
    """The exact pair that will be served, after the one check that matters."""
    manifest = read_bytes(manifest_path, "manifest")
    signature = signature_bytes(signature_path)
    if not verify(manifest, signature, pubkey):
        raise Refused("the manifest signature does not verify over %s" % manifest_path)
    return manifest, signature


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="odile-manifest",
        description="Check the signed manifest pair, then serve it (ODL-472).",
    )
    parser.add_argument("--version", action="version", version="odile-manifest " + VERSION)
    parser.add_argument("mode", nargs="?", choices=("check", "serve"),
                        help="check the pair and exit, or serve it")
    parser.add_argument("--check", action="store_true",
                        help="verify the pair and exit (the unit's ExecStartPre)")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--pubkey", required=True)
    parser.add_argument("--program", help="the release listener to exec on serve")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8012)
    args = parser.parse_args(argv)
    if args.mode is None and not args.check:
        parser.error("a mode is required: check (or --check) or serve")
    serve = args.mode == "serve" and not args.check

    try:
        manifest, _ = check_pair(args.manifest, args.signature, args.pubkey)
    except Refused as refusal:
        print("error: %s" % refusal, file=sys.stderr, flush=True)
        return 1
    installer = ""
    try:
        import json
        document = json.loads(manifest.decode("utf-8"))
        if isinstance(document, dict) and isinstance(document.get("installer_sha256"), str):
            installer = ", installer %s" % document["installer_sha256"][:12]
    except (ValueError, UnicodeDecodeError):
        pass
    line = ("%s: OK (sha256 %s, %d bytes%s, key %s)"
            % (args.manifest, sha256(manifest)[:12], len(manifest), installer,
               fingerprint(args.pubkey)))
    if not serve:
        print(line, flush=True)
        return 0
    if not args.program:
        print("error: serve needs --program (the release's manifest.py)", file=sys.stderr)
        return 2
    if not os.path.isfile(args.program):
        print("error: the listener program %s is missing" % args.program, file=sys.stderr)
        return 1
    print(line, flush=True)
    os.execv(sys.executable, [
        sys.executable, args.program, "serve",
        "--manifest", args.manifest, "--signature", args.signature,
        "--bind", args.bind, "--port", str(args.port),
    ])
    return 0  # not reached


if __name__ == "__main__":
    raise SystemExit(main())
