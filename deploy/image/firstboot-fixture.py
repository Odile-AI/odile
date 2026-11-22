#!/usr/bin/python3
"""firstboot-fixture — the host-side seed service the QEMU first boot uses.

ODL-456's bench runs the real first-boot program against a real seed service;
only the *machine* is emulated. This program builds that fixture and runs the
service, so the exchange, the signature, the single use and the 24-hour
binding are the production code (seed/seed.py), not a stub:

    ./deploy/image/firstboot-fixture.py prepare --dir /var/tmp/odile-456-bench \\
        --instance 45445445-0000-4000-8000-000000000454
    # -> prints the token once, stores it 0600 in <dir>/token for the bench
    ./deploy/image/firstboot-fixture.py serve --dir /var/tmp/odile-456-bench --port 8081
    ./deploy/image/firstboot-fixture.py show --dir /var/tmp/odile-456-bench

`prepare` creates a SQLite database with one tenant (`odl-bench`) and a token
bound to the instance id, a seed config with public bench values (mode 0600),
a seed signing key and the release key's certificate over its public half.
The seed key defaults to the real one (/root/.odile/seed-signing.pem, mode
0600) because the image opens seeds with it; the certificate is cut with the
release key (default /root/.odile/installer-signing.pem) because the image
carries that key's public half. Pass --key / --release-key to use throwaways.

`serve` execs `seed/seed.py serve` with the fixture's environment; the guest
reaches it at QEMU's host alias 10.0.2.2:<port>. It binds 127.0.0.1 only, the
same as production.

Nothing here is a product secret: the values are fixtures, and the token is
single-use and bound to the bench instance id.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "seed" / "seed.py"
# ODL-514: the seed key signs every seed, the release key certifies its public
# half. The image bakes the release public key, so the fixture must cut the
# certificate with the matching private key.
DEFAULT_KEY = "/root/.odile/seed-signing.pem"
DEFAULT_RELEASE_KEY = "/root/.odile/installer-signing.pem"
FIXTURE_SLUG = "odl-bench"
FIXTURE_EMAIL = "bench@odile.test"


def _openssl(argv: list[str], data: bytes | None = None) -> bytes:
    try:
        proc = subprocess.run(["openssl", *argv], input=data, capture_output=True, timeout=60)
    except FileNotFoundError:
        raise SystemExit("firstboot-fixture: openssl is missing") from None
    if proc.returncode != 0:
        raise SystemExit("firstboot-fixture: openssl %s failed: %s"
                         % (" ".join(argv[:2]), proc.stderr.decode("utf-8", "replace").strip()))
    return proc.stdout


def public_key_bytes(key: str) -> bytes:
    """The public half of `key`, PEM, exactly as `openssl pkey -pubout` writes it."""
    if not os.path.isfile(key):
        raise SystemExit("firstboot-fixture: no key at %s" % key)
    return _openssl(["pkey", "-in", key, "-pubout"])


def seed_identity(directory: Path, key: str, release_key: str | None) -> tuple[bytes, bytes]:
    """(seed public key, the release key's certificate over it), written for the service.

    The certificate is what the guest walks: release key -> seed key -> body.
    The release public half is written beside it so the fixture never depends on
    a checkout's copy of the key that signed the image.
    """
    key = str(key)
    release_key = release_key or DEFAULT_RELEASE_KEY
    if not os.path.isfile(release_key):
        # A clean test box: mint a throwaway release key. The guest must then
        # be given its public half (the bench passes ODL_SEED_RELEASE_PUBKEY).
        release_key = str(directory / "release-key.pem")
        if not os.path.isfile(release_key):
            _openssl(["genpkey", "-algorithm", "ED25519", "-out", release_key])
            os.chmod(release_key, 0o600)
    public = public_key_bytes(key)
    release_public = public_key_bytes(release_key)
    public_file = directory / "seed-key.pub"
    write_public(public_file, public)
    cert = directory / "seed-key.crt"
    certificate = _openssl(["dgst", "-sign", release_key, "-out", "/dev/stdout"], public)
    if len(certificate) != 64:
        raise SystemExit("firstboot-fixture: release key produced %d certificate bytes, expected 64"
                         % len(certificate))
    if not verify_bytes(public, certificate, release_public, directory):
        raise SystemExit("firstboot-fixture: the certificate does not verify against %s" % release_key)
    write_public(cert, certificate)
    write_public(directory / "release-key.pub", release_public)
    return public, certificate


def verify_bytes(data: bytes, signature: bytes, public: bytes, directory: Path) -> bool:
    """Ed25519 over `data`, with `public` written to a throwaway file for openssl."""
    key_file = directory / "verify-key.pub"
    write_public(key_file, public)
    sig_file = directory / "verify.sig"
    write_public(sig_file, signature)
    try:
        proc = subprocess.run(
            ["openssl", "dgst", "-verify", str(key_file), "-signature", str(sig_file)],
            input=data, capture_output=True, timeout=60)
    finally:
        for path in (key_file, sig_file):
            try:
                path.unlink()
            except OSError:
                pass
    return proc.returncode == 0


def _seed_module():
    spec = importlib.util.spec_from_file_location("odile_seed_fixture", SEED)
    if spec is None or spec.loader is None:
        raise SystemExit("firstboot-fixture: cannot load %s" % SEED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_config(slug: str) -> dict:
    return {
        "near_api_key": "bench-near-key-not-a-secret-%s" % slug,
        "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017-abliterated",
        "support_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBenchSupportKey bench@odile.test",
        "backup_repo_url": "sftp:bench@backup.odile.test:/srv/%s" % slug,
        "hermes_version": "Hermes Agent v0.23.0",
        "mail_relay": {
            "host": "smtp.odile.test",
            "port": 587,
            "username": slug,
            "secret": "bench-mail-secret-not-a-secret",
            "tls_ca": ("-----BEGIN CERTIFICATE-----\n"
                       "MIIBhTCCASugAwIBAgIUFIXTURECA000000000000000000\n"
                       "-----END CERTIFICATE-----\n"),
        },
    }


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def write_public(path: Path, data: bytes) -> None:
    """A public artifact: 0644, no secret."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def prepare(directory: Path, instance: str, slug: str, key: str,
            backup_password: str | None = None,
            token: str | None = None,
            release_key: str | None = None) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    seed = _seed_module()
    db = directory / "seed.db"
    config_path = directory / "seed-config.json"
    if not os.path.isfile(key):
        raise SystemExit("firstboot-fixture: no signing key at %s" % key)
    # ODL-514: the seed key signs; the release key certifies its public half.
    # Cut the certificate now so `serve` has the pair the guest will walk.
    seed_identity(directory, key, release_key)

    config = fixture_config(slug)

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        seed.ensure_schema(con)
        # The first boot's record writes the tenant spine (ODL-398 follow-up);
        # a fixture HQ database carries the same tables and a first_boot row,
        # so the bench proves the write, not just the POST.
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from odilectl import state as state_module

        con.executescript(state_module.SCHEMA)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = con.execute("SELECT id FROM tenants WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO tenants (slug, email, state, created_at) VALUES (?, ?, 'invited', ?)",
                (slug, FIXTURE_EMAIL, now),
            )
            con.commit()
        else:
            row = con.execute(
                "SELECT id FROM tenants WHERE slug = ?", (slug,)).fetchone()
            # A re-prepare must not leave two live tokens: end the old ones.
            con.execute(
                "UPDATE seed_tokens SET used_at = ? WHERE tenant_id = ? AND used_at IS NULL",
                (now, row["id"]),
            )
            con.commit()
        con.execute(
            "UPDATE tenants SET server_id = ?, state = 'provisioned',"
            " release = COALESCE(release, '2026.11.1') WHERE slug = ?",
            (instance, slug),
        )
        store = state_module.StateStore(con)
        try:
            spine = store.get(slug)
        except state_module.UnknownTenant:
            spine = store.invite(slug, detail={"source": "firstboot-fixture"})
        if spine.state == "invited":
            spine = store.claim(slug)
        if spine.state == "claimed":
            spine = store.provision(slug, server_id=instance, hostname=slug)
        if spine.state == "provisioning":
            store.await_boot(slug)
        con.execute(
            "UPDATE tenant_state SET initramfs_host_key = ? WHERE slug = ?",
            ("SHA256:fixture-initramfs-key", slug),
        )
        con.commit()
        if token is None:
            binding = seed.bind(con, slug, instance)
        else:
            # Re-arm the token an already-built image carries (a bench rerun):
            # the cmdline is measured and the disk's volume key is sealed to
            # it, so a spent token would otherwise force a full rebuild. A new
            # row, the same 24-hour binding, the same single-use rule.
            if not seed.TOKEN_RE.match(token):
                raise SystemExit("firstboot-fixture: --token-file is not 64 lowercase hex")
            row = con.execute("SELECT id, slug FROM tenants WHERE slug = ?", (slug,)).fetchone()
            now = seed.utcnow()
            digest = seed.hash_token(token)
            # A token is unique per row: re-arm the row the image already names
            # (used_at back to NULL, the clock fresh), or insert it if this
            # database has never seen it. Other live tokens end, as `bind` does.
            con.execute(
                "UPDATE seed_tokens SET used_at = ? WHERE tenant_id = ?"
                " AND used_at IS NULL AND token_hash != ?",
                (seed.iso(now), row["id"], digest))
            rearmed = con.execute(
                "UPDATE seed_tokens SET used_at = NULL, instance_id = ?,"
                " created_at = ?, expires_at = ? WHERE token_hash = ?",
                (instance, seed.iso(now), seed.iso(now + timedelta(hours=24)), digest))
            if rearmed.rowcount == 0:
                con.execute(
                    "INSERT INTO seed_tokens (tenant_id, token_hash, instance_id,"
                    " created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                    (row["id"], digest, instance, seed.iso(now),
                     seed.iso(now + timedelta(hours=24))))
            con.commit()
            binding = {"token": token, "token_hash": digest,
                       "instance_id": instance,
                       "expires_at": seed.iso(now + timedelta(hours=24))}
    finally:
        con.close()

    write_private(config_path,
                  (json.dumps(config, indent=2, sort_keys=True) + "\n").encode())

    # A rebuild: seal the user's OLD repository password under the token this
    # fixture just minted, exactly as `odilectl provision --backup-password-file`
    # does in production. The rebuilds file holds the ciphertext; the guest
    # opens it with the token from the measured command line.
    rebuild = "none"
    rebuild_file = directory / "seed-rebuilds.json"
    if backup_password is not None:
        entry = seed.seal_backup_password(backup_password, binding["token"])
        entry["token_hash"] = binding["token_hash"]
        entry["created_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_private(rebuild_file,
                      (json.dumps({slug: entry}, indent=2, sort_keys=True) + "\n").encode())
        rebuild = "sealed"

    write_private(directory / "token", (binding["token"] + "\n").encode())
    write_private(directory / "instance", (instance + "\n").encode())
    return {
        "dir": str(directory), "db": str(db), "config": str(config_path),
        "key": key, "slug": slug, "instance": instance, "token": binding["token"],
        "expires_at": binding["expires_at"], "rebuild": rebuild,
        "rebuilds": str(rebuild_file),
        "certificate": str(directory / "seed-key.crt"),
        "seed_pubkey": str(directory / "seed-key.pub"),
        "release_pubkey": str(directory / "release-key.pub"),
    }


def show(directory: Path) -> int:
    token_path = directory / "token"
    token = token_path.read_text().strip() if token_path.is_file() else ""
    print("dir        %s" % directory)
    print("token      %s" % (token[:8] + "…" if token else "(none)"))
    print("instance   %s" % ((directory / "instance").read_text().strip()
                             if (directory / "instance").is_file() else "(none)"))
    db = directory / "seed.db"
    if db.is_file():
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            for row in con.execute(
                    "SELECT token_hash, instance_id, created_at, expires_at, used_at"
                    " FROM seed_tokens ORDER BY id"):
                print("token      %s… %s %s" % (
                    row["token_hash"][:8], row["expires_at"],
                    "used" if row["used_at"] else "live"))
        finally:
            con.close()
    return 0


def serve(directory: Path, port: int) -> int:
    seed = _seed_module()
    os.environ["ODILE_DB"] = str(directory / "seed.db")
    os.environ["ODILE_SEED_CONFIG"] = str(directory / "seed-config.json")
    os.environ["ODILE_SEED_SIGNING_KEY"] = str(directory / "signing-key.pem")
    os.environ["ODILE_SEED_CERT"] = str(directory / "seed-key.crt")
    os.environ["ODILE_SEED_RELEASE_PUBKEY"] = str(directory / "release-key.pub")
    os.environ["ODILE_SEED_BIND"] = "127.0.0.1"
    os.environ["ODILE_SEED_PORT"] = str(port)
    os.environ["ODILE_SEED_REBUILDS"] = str(directory / "seed-rebuilds.json")
    return seed.serve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed fixture for the ODL-456 bench")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="create the fixture database and bind a token")
    p.add_argument("--dir", required=True)
    p.add_argument("--instance", required=True)
    p.add_argument("--slug", default=FIXTURE_SLUG)
    p.add_argument("--key", default=DEFAULT_KEY)
    p.add_argument("--release-key", default=DEFAULT_RELEASE_KEY,
                   help="the release key that certifies the seed public key "
                        "(default %s)" % DEFAULT_RELEASE_KEY)
    p.add_argument("--backup-password-file", metavar="PATH",
                   help="the user's old recovery file: its 'Backup password:' line is "
                        "sealed under the token, so the boot keeps the repository")
    p.add_argument("--token-file", metavar="PATH",
                   help="re-arm this token instead of minting one: a bench rerun of "
                        "an image whose measured cmdline already carries it")

    p = sub.add_parser("serve", help="run seed/seed.py serve on the fixture")
    p.add_argument("--dir", required=True)
    p.add_argument("--port", type=int, default=8081)

    p = sub.add_parser("show", help="token prefix, instance and ledger states")
    p.add_argument("--dir", required=True)

    args = parser.parse_args(argv)
    directory = Path(args.dir)
    if args.command == "prepare":
        # The seed service reads the key from ODILE_SEED_SIGNING_KEY or the
        # credential directory; give it a private copy so the fixture is
        # self-contained.
        key_copy = directory / "signing-key.pem"
        key_copy.parent.mkdir(parents=True, exist_ok=True)
        with open(args.key, "rb") as source, open(key_copy, "wb") as target:
            target.write(source.read())
        os.chmod(key_copy, 0o600)
        password = None
        if args.backup_password_file:
            seed = _seed_module()
            try:
                password = seed.read_recovery_password(args.backup_password_file)
            except ValueError as error:
                raise SystemExit("firstboot-fixture: %s" % error) from None
        token = None
        if args.token_file:
            token = Path(args.token_file).read_text(encoding="utf-8").strip()
        result = prepare(directory, args.instance, args.slug, str(key_copy), password, token,
                         args.release_key)
        print("tenant     %s" % result["slug"])
        print("instance   %s" % result["instance"])
        print("expires    %s" % result["expires_at"])
        print("rebuild    %s (%s)" % (result["rebuild"], result["rebuilds"]))
        print("seed cert  %s (release public key %s)"
              % (result["certificate"], result["release_pubkey"]))
        print("token      %s%s" % (result["token"],
                                    " (re-armed)" if token else " (minted)"))
        print("(printed once; only its SHA-256 is stored)", file=sys.stderr)
        return 0
    if args.command == "serve":
        return serve(directory, args.port)
    return show(directory)


if __name__ == "__main__":
    raise SystemExit(main())
