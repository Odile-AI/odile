#!/usr/bin/python3
"""Render the Odile recovery kit — one plain-text file, no secret Odile can use.

The kit attached to the ready email is the machine's own document: its facts
(slug, address, URL, machine id, release, vCPUs, measurement, token expiry,
signing key), the single-use first-visit link, and the two ways back — the
recovery code resets the sign-in, the recovery file opens the backup (A9, A7 and
A5 of `docs/product/CLAIMS-RECUT-STRICT.md`). It carries no terminal, no key
the user handles and no passphrase: the strict bar's install is zero-touch and
the first visit is a browser page.

What the kit does not carry: the private half of any key, the disk key, the
Odile sign-in, the backup password or any seed secret. It carries both *public*
halves — the release signing key (installers, manifests, releases) and the seed
signing key (tenant seeds), plus the release key's certificate over the seed
key — because a kit that names one key stops being an anchor the day the other
one signs (ODL-514). The only Odile-minted value is the setup token: 64 hex,
bound to the tenant and the instance id, single use, and spent by the
first-visit URL in this file. The backup password is generated on the guest and
reaches the user in the recovery file the first visit downloads; Odile holds no
copy.

The generator touches no network and no database: slug, IPv4, instance id,
measurement, token and first-visit URL are parameters, and the template renders
from values. ODL-220 mints the token with `seed/seed.py` and calls this script;
ODL-232 attaches the file.

    ./scripts/recovery_kit.py render \\
        --slug odl-7f3a2c --ipv4 203.0.113.120 \\
        --instance-id srv-… --token <64 lowercase hex> \\
        --measurement <96 lowercase hex> --out kit.txt

Exit codes: 0 success, 2 bad input or a template that does not render cleanly.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "recovery-kit.md"
SIGNING_KEY = ROOT / "installer" / "odile-signing-key.pub"
# ODL-514: the seed key is not the release key. The kit names both public
# halves and the release key's certificate over the seed key.
SEED_SIGNING_KEY = ROOT / "deploy" / "seed" / "seed-key.pub"
SEED_CERT = ROOT / "deploy" / "seed" / "seed-key.crt"

# The release and plan the kit is written for. ODL-398 makes the release per
# tenant; until then this is the release the ADR pins and the runbooks use, and
# the Plus plan has 4 vCPUs. A caller that knows better overrides them.
DEFAULT_RELEASE = "2026.11.1"
DEFAULT_VCPUS = 4

# The installer's own grammar (installer/odile_install.py:SLUG_RE/TOKEN_RE/
# INSTANCE_RE) and the release pin's measurement shape. A kit the machine would
# reject is worse than no kit, so the generator refuses the value first with a
# message a human can act on.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
INSTANCE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
RELEASE_RE = re.compile(r"^[0-9]{4}\.[0-9]{2}(?:\.[0-9]+)?$")
MEASUREMENT_RE = re.compile(r"^[0-9a-f]{96}$")

BLOCK_RE = re.compile(r"{{#([a-z0-9_]+)}}(.*?){{/\1}}", re.S)
INVERTED_RE = re.compile(r"{{\^([a-z0-9_]+)}}(.*?){{/\1}}", re.S)
VALUE_RE = re.compile(r"{{([a-z0-9_]+)}}")


class BadInput(Exception):
    """A value the kit cannot render; exit 2 with the reason."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def prose_instant(value: datetime) -> str:
    """The same instant in the words BRAND.md asks for: 15 November 2026, 22:30 UTC."""
    months = ("January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December")
    return (f"{value.day} {months[value.month - 1]} {value.year}, "
            f"{value.strftime('%H:%M')} UTC")


def parse_instant(raw: str, label: str) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BadInput(f"{label} is not an ISO 8601 instant: {raw!r}") from exc
    if value.tzinfo is None:
        raise BadInput(f"{label} needs a time zone: {raw!r}")
    return value.astimezone(timezone.utc)


def validate(values: str, label: str, pattern: re.Pattern[str], shape: str) -> str:
    if not pattern.match(values):
        raise BadInput(f"{label} must be {shape}, got {values!r}")
    return values


def verify_bytes(data: bytes, signature: bytes, pubkey_path: Path) -> bool:
    """True when `signature` is a valid Ed25519 signature of `data`."""
    with tempfile.NamedTemporaryFile(prefix="odile-kit-sig-", delete=False) as handle:
        handle.write(signature)
        sig_path = handle.name
    try:
        proc = subprocess.run(
            ["openssl", "dgst", "-verify", str(pubkey_path), "-signature", sig_path],
            input=data, capture_output=True,
        )
        return proc.returncode == 0
    finally:
        try:
            Path(sig_path).unlink()
        except OSError:
            pass


def read_certificate(path: Path) -> bytes:
    """The 64 raw bytes of a seed certificate, or its base64 spelling."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BadInput(f"seed certificate not found: {path} ({exc.strerror})") from exc
    if len(raw) == 64:
        return raw
    try:
        decoded = base64.b64decode(b"".join(raw.split()), validate=True)
    except ValueError:
        raise BadInput(f"seed certificate {path} is neither a raw nor a base64 signature") from None
    if len(decoded) != 64:
        raise BadInput(f"seed certificate {path} decodes to {len(decoded)} bytes, expected 64")
    return decoded


def render(template: str, values: dict[str, object]) -> str:
    """Resolve `{{#key}}…{{/key}}`, `{{^key}}…{{/key}}` and `{{key}}`.

    The template is plain text, so substitution is literal. A placeholder the
    caller did not supply is an error: shipping `{{slug}}` to a user is worse
    than failing the render.
    """
    def block(match: re.Match[str]) -> str:
        return match.group(2) if values.get(match.group(1)) else ""

    def inverted(match: re.Match[str]) -> str:
        return "" if values.get(match.group(1)) else match.group(2)

    text = BLOCK_RE.sub(block, template)
    text = INVERTED_RE.sub(inverted, text)

    def value(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise BadInput(f"template placeholder {{{{{key}}}}} has no value")
        return str(values[key])

    text = VALUE_RE.sub(value, text)
    leftover = VALUE_RE.search(text)
    if leftover:
        raise BadInput(f"template placeholder {leftover.group(0)} did not resolve")
    return text


def build_values(args: argparse.Namespace) -> dict[str, object]:
    slug = validate(args.slug, "slug", SLUG_RE, "lowercase letters, digits and hyphens, 2–63 characters")
    instance = validate(args.instance_id, "instance id", INSTANCE_RE,
                        "letters, digits, dot, underscore, colon or hyphen, up to 128 characters")
    token = validate(args.token, "setup token", TOKEN_RE, "64 lowercase hex characters")
    measurement = validate(args.measurement, "measurement", MEASUREMENT_RE,
                           "96 lowercase hex characters")
    if not RELEASE_RE.match(args.release):
        raise BadInput(f"release must look like 2026.11.1, got {args.release!r}")
    if args.vcpus < 1 or args.vcpus > 128:
        raise BadInput(f"vcpus must be between 1 and 128, got {args.vcpus}")
    try:
        address = ipaddress.ip_address(args.ipv4)
    except ValueError as exc:
        raise BadInput(f"ipv4 is not an address: {args.ipv4!r}") from exc
    if address.version != 4:
        raise BadInput(f"ipv4 must be IPv4, got {args.ipv4!r}")

    written_at = parse_instant(args.written_at or utc_now(), "--written-at")
    expires_at = None
    if args.expires_at:
        expires_at = parse_instant(args.expires_at, "--expires-at")
        if expires_at <= written_at:
            raise BadInput(
                f"--expires-at {args.expires_at!r} is not after the kit's written time;"
                " the token would arrive dead"
            )

    url = f"https://{slug}.odile.fyi"
    first_visit_url = args.first_visit_url or f"{url}/first-visit?t={token}"
    if not first_visit_url.startswith("https://") or any(
            char.isspace() for char in first_visit_url):
        raise BadInput(f"first-visit URL must be one https:// URL, got {first_visit_url!r}")
    if token not in first_visit_url:
        raise BadInput(
            f"first-visit URL does not carry the setup token: {first_visit_url!r}"
        )

    key_path = Path(args.signing_key)
    if not key_path.is_file():
        raise BadInput(f"signing key not found: {key_path}")
    signing_public_key = key_path.read_text(encoding="utf-8")
    if "BEGIN PUBLIC KEY" not in signing_public_key:
        raise BadInput(f"signing key {key_path} is not a PEM public key")
    if not signing_public_key.endswith("\n"):
        signing_public_key += "\n"
    signing_key_sha256 = hashlib.sha256(key_path.read_bytes()).hexdigest()

    # ODL-514: the seed key, and the release key's certificate over it. Both
    # are public; the certificate is checked here so a kit never ships an
    # anchor that does not hang together.
    seed_key_path = Path(getattr(args, "seed_signing_key", None) or SEED_SIGNING_KEY)
    if not seed_key_path.is_file():
        raise BadInput(f"seed signing key not found: {seed_key_path} "
                       "(deploy/seed/seed-key.pub; cut it with make seed-cert)")
    seed_bytes = seed_key_path.read_bytes()
    try:
        seed_public_key = seed_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BadInput(f"seed signing key {seed_key_path} is not text: {exc}") from exc
    if "BEGIN PUBLIC KEY" not in seed_public_key:
        raise BadInput(f"seed signing key {seed_key_path} is not a PEM public key")
    seed_key_sha256 = hashlib.sha256(seed_bytes).hexdigest()
    certificate_path = Path(getattr(args, "seed_cert", None) or SEED_CERT)
    certificate = read_certificate(certificate_path)
    if not verify_bytes(seed_bytes, certificate, key_path):
        raise BadInput(f"seed certificate {certificate_path} is not {key_path}'s signature "
                       f"over {seed_key_path}; re-cut it with make seed-cert")
    if not seed_public_key.endswith("\n"):
        seed_public_key += "\n"

    backup_repo_url = None
    if args.backup_repo_url:
        backup_repo_url = args.backup_repo_url.strip()
        if not backup_repo_url or any(char.isspace() for char in backup_repo_url):
            raise BadInput(f"--backup-repo-url must be one token, got {args.backup_repo_url!r}")

    return {
        "slug": slug,
        "ipv4": str(address),
        "url": url,
        "instance_id": instance,
        "release": args.release,
        "vcpus": str(args.vcpus),
        "measurement": measurement,
        "first_visit_url": first_visit_url,
        "written_at": written_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        # The fact line keeps the ISO instant a support runbook can match; the
        # prose gets the same instant in owner-facing words (BRAND: dates).
        "token_expiry": (expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                         if expires_at else "24 hours after issue"),
        "token_expiry_prose": (prose_instant(expires_at)
                               if expires_at else "24 hours after issue"),
        "signing_public_key": signing_public_key,
        "signing_key_sha256": signing_key_sha256,
        "seed_signing_public_key": seed_public_key,
        "seed_signing_key_sha256": seed_key_sha256,
        "seed_certificate": base64.b64encode(certificate).decode("ascii"),
        "seed_certificate_sha256": hashlib.sha256(certificate).hexdigest(),
        "backup_repo_url": backup_repo_url,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render the Odile recovery kit (plain text; no machine, no network).")
    sub = parser.add_subparsers(dest="command", required=True)
    render_p = sub.add_parser("render", help="render the kit from values")
    render_p.add_argument("--slug", required=True, help="tenant slug, e.g. odl-7f3a2c")
    render_p.add_argument("--ipv4", required=True, help="the machine's public IPv4 address")
    render_p.add_argument("--instance-id", required=True,
                          help="the provider instance id the setup token is bound to")
    render_p.add_argument("--token", required=True, help="64 lowercase hex, single use")
    render_p.add_argument("--measurement", required=True,
                          help="the 96 lowercase hex measurement from the release pin")
    render_p.add_argument("--first-visit-url",
                          help="the single-use first-visit URL (default: "
                               "https://<slug>.odile.fyi/first-visit?t=<token>)")
    render_p.add_argument("--expires-at", help="the token's expiry, ISO 8601 UTC")
    render_p.add_argument("--release", default=DEFAULT_RELEASE,
                          help=f"measured-boot release id (default {DEFAULT_RELEASE})")
    render_p.add_argument("--vcpus", type=int, default=DEFAULT_VCPUS,
                          help=f"plan vCPUs (default {DEFAULT_VCPUS})")
    render_p.add_argument("--signing-key", default=str(SIGNING_KEY),
                          help="the published Odile signing public key (PEM)")
    render_p.add_argument("--seed-signing-key", default=str(SEED_SIGNING_KEY),
                          help="the seed signing public key (PEM; ODL-514)")
    render_p.add_argument("--seed-cert", default=str(SEED_CERT),
                          help="the release key's signature over the seed public key")
    render_p.add_argument("--backup-repo-url",
                          help="the tenant's restic repository URL, if known")
    render_p.add_argument("--written-at", help="render timestamp, ISO 8601 UTC (tests)")
    render_p.add_argument("--out", help="write the kit here (default: stdout)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        template = TEMPLATE.read_text(encoding="utf-8")
        values = build_values(args)
        text = render(template, values)
    except BadInput as exc:
        print(f"recovery kit: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"recovery kit: {exc}", file=sys.stderr)
        return 2
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path} ({len(text)} bytes)")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
