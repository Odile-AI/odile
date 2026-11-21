#!/usr/bin/python3
"""Re-cut the signed release after the installer's sources change.

The installer, the manifest that names it, and the release document that names
both are a chain of hashes, and three of them are signed. Editing anything the
installer embeds — a script it pins, the wrapper, the meter it carries — breaks
that chain in a way no test can paper over: the pair ships a signature that does
not match its own bytes.

This script re-derives the chain from the tree and re-signs it with the HQ key:

    /usr/bin/python3 tools/recut_release.py            # cut and sign
    /usr/bin/python3 tools/recut_release.py --check     # verify only

It writes four files and four signatures:

    installer/odile_install.py.sig      over the installer
    release/odile_install.py            a byte copy of the installer
    release/odile_install.py.sig
    release/manifest.json               installer_sha256, audit_sha256 and
                                        backup_sha256 re-derived from the installer
    release/manifest.json.sig
    release/release.json                manifest sha256, artefact size + sha256
    release/release.json.sig

It does **not** touch deploy/image/tenant-image.pin: that pin is produced by the
image build (deploy/image/uki.sh) from built artefacts, so re-deriving it needs
the bench, not this script. `--check` reports whether the pin's hook_sha256 still
matches the hook in the tree, so a stale pin is named rather than shipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
KEY = pathlib.Path("/root/.odile/installer-signing.pem")
PIN = ROOT / "deploy" / "image" / "tenant-image.pin"
HOOK = ROOT / "spike" / "sealed-unlock" / "initramfs" / "bb-sealed-unlock"


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sign(path: pathlib.Path, key: pathlib.Path) -> None:
    subprocess.run([sys.executable, str(ROOT / "scripts" / "manifest.py"), "sign",
                    "--manifest", str(path), "--key", str(key)], check=True,
                   stdout=subprocess.DEVNULL)


def verify(path: pathlib.Path) -> str:
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "manifest.py"),
                             "verify", "--manifest", str(path)],
                            capture_output=True, text=True)
    output = (result.stdout or "") + (result.stderr or "")
    for line in output.splitlines():
        if line.startswith("signature: OK"):
            return line
    return "FAILED: " + output.strip().splitlines()[-1] if output.strip() else "FAILED"


def installer_manifest(path: pathlib.Path) -> dict:
    """The installer's own `--manifest` record, read from its bytes."""
    result = subprocess.run([sys.executable, str(path), "--manifest"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {}


def check() -> int:
    manifest = json.loads((ROOT / "release" / "manifest.json").read_text())
    release = json.loads((ROOT / "release" / "release.json").read_text())
    installer = ROOT / "release" / "odile_install.py"
    ok = True

    def same(label: str, left: str, right: str) -> None:
        nonlocal ok
        if left == right:
            print(f"  ok    {label} {left[:16]}…")
        else:
            print(f"  FAIL  {label} {left[:16]}… != {right[:16]}…")
            ok = False

    same("installer", sha256(installer), manifest["installer_sha256"])
    same("installer", sha256(installer), release["artifacts"]["installer"]["sha256"])
    same("installer size", str(installer.stat().st_size), str(release["artifacts"]["installer"]["size"]))
    same("manifest", sha256(ROOT / "release" / "manifest.json"), release["manifest"]["sha256"])
    same("installer copy", sha256(ROOT / "installer" / "odile_install.py"), sha256(installer))
    # The programs the installer embeds are the ones the release manifest names
    # (the handoff audit, t_87943990; the tenant backup, t_e0475dba). A row that
    # disagrees with the installer this document names is the mismatch the row
    # exists to catch; a document cut before the row existed is reported, not
    # failed.
    shipped = installer_manifest(installer)
    for label, field in (("audit", "audit_sha256"), ("backup", "backup_sha256")):
        pin = shipped.get(field)
        recorded = manifest.get(field)
        if pin and recorded:
            same(label, recorded, pin)
        elif pin:
            print(f"  warn  {label} (the manifest predates the row; the installer pins {pin[:16]}…)")
    for name in ("installer/odile_install.py", "release/odile_install.py",
                 "release/manifest.json", "release/release.json"):
        line = verify(ROOT / name)
        print(f"  {'ok   ' if line.startswith('signature: OK') else 'FAIL '} signature {name}: {line[:60]}")
        ok = ok and line.startswith("signature: OK")

    if PIN.exists() and HOOK.exists():
        pin = json.loads(PIN.read_text())
        same("pin hook_sha256", sha256(HOOK), pin.get("hook_sha256", ""))
    return 0 if ok else 1


def cut(key: pathlib.Path) -> int:
    installer = ROOT / "installer" / "odile_install.py"
    release_installer = ROOT / "release" / "odile_install.py"
    release_installer.write_bytes(installer.read_bytes())
    installer_hash = sha256(release_installer)

    manifest_path = ROOT / "release" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["installer_sha256"] = installer_hash
    # A re-cut after a program the installer embeds moved must move its row
    # too, or the release would name a program the installer it ships does not
    # write: the handoff audit `/verify` quotes (t_87943990) and the tenant
    # backup program (t_e0475dba).
    shipped = installer_manifest(release_installer)
    for field in ("audit_sha256", "backup_sha256"):
        pin = shipped.get(field)
        if pin:
            manifest[field] = pin
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    manifest_hash = sha256(manifest_path)

    release_path = ROOT / "release" / "release.json"
    release = json.loads(release_path.read_text())
    release["manifest"]["sha256"] = manifest_hash
    release["artifacts"]["installer"]["sha256"] = installer_hash
    release["artifacts"]["installer"]["size"] = release_installer.stat().st_size
    release_path.write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")

    for target in (release_installer, manifest_path, release_path, installer):
        sign(target, key)
        print(f"signed {target.relative_to(ROOT)}")
    print(f"installer {installer_hash}")
    print(f"manifest  {manifest_hash}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-cut and re-sign the release pair.")
    parser.add_argument("--check", action="store_true", help="verify only, change nothing")
    parser.add_argument("--key", default=str(KEY))
    args = parser.parse_args(argv)
    if args.check:
        return check()
    if not pathlib.Path(args.key).exists():
        print(f"recut: no key at {args.key}", file=sys.stderr)
        return 2
    code = cut(pathlib.Path(args.key))
    print("--- verify ---")
    return code or check()


if __name__ == "__main__":
    sys.exit(main())
