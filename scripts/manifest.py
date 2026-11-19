#!/usr/bin/python3
"""The Odile app-layer manifest — build it, sign it, check it, serve it.

ODL-214. One signed JSON document that makes the app layer pinnable: the Hermes
commit, the hash the installer required of the fetched `install.sh` before it
ran, the versions of every package the installer puts on the machine, the hash
of every installed skill, the installer's own sha256, the tenant backup program
the nightly unit executes, and the handoff audit script. The guest serves the
document at `/manifest`; Caddy terminates TLS and proxies the loopback listener
the way it does for the heartbeat (the runbook shows the four lines).

The signature is Ed25519, detached, made on HQ over the **exact bytes** of the
manifest. The private key never enters git and never leaves HQ; the public key
is `installer/odile-signing-key.pub`, the same key the recovery kit carries and
the installer verifies itself against. This is an operator signature over the
app layer, not a hardware measurement: measured boot covers firmware, kernel,
initramfs and the command line, and the CPU does not measure these files.

    ./scripts/manifest.py build --root /var/tmp/odile-a --out /tmp/a.json
    ./scripts/manifest.py build --root / --user odile --out /etc/odile/manifest.json
    ./scripts/manifest.py sign --manifest /tmp/a.json --key /root/.odile/installer-signing.pem
    ./scripts/manifest.py verify --manifest /tmp/a.json --signature /tmp/a.json.sig \\
        --pubkey installer/odile-signing-key.pub
    ./scripts/manifest.py diff /tmp/a.json /tmp/b.json
    ./scripts/manifest.py serve --manifest /etc/odile/manifest.json --port 8012

`build` reads an **installed** root, never a checkout: the point is to record
what the installer put on the machine. Two fresh installs in two scratch roots
must produce byte-identical manifests except `generated_at`; `diff` enforces
exactly that. Nothing in the document carries a secret: the seed's model name is
recorded, its NEAR key is not, and no file path from the host appears.

Exit codes: 0 success, 1 `diff` found a difference, 2 a bad prerequisite or
input, 3 the signature does not verify.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.1.0"
SCHEMA = 1

# The installer's package set (ODL-203's ensure_packages). Every one needs an
# exact version in the manifest: an unpinned apt install is a moving target.
RUNTIME_PACKAGES = ("caddy", "curl", "git", "nodejs", "npm", "openssl", "python3", "restic", "tmux")

# Probe paths for a scratch root: the simulation uses the host's toolchain, and
# an absolute path is stable where PATH is not.
PROBES = {
    "caddy": ("/usr/bin/caddy", ("version",)),
    "curl": ("/usr/bin/curl", ("--version",)),
    "git": ("/usr/bin/git", ("--version",)),
    "nodejs": ("/usr/bin/node", ("--version",)),
    "npm": ("/usr/bin/npm", ("--version",)),
    "openssl": ("/usr/bin/openssl", ("version",)),
    "python3": ("/usr/bin/python3", ("--version",)),
    "restic": ("/usr/bin/restic", ("version",)),
    "tmux": ("/usr/bin/tmux", ("-V",)),
}
DPKG_QUERY = "/usr/bin/dpkg-query"
VERSION_RE = re.compile(r"(\d+(?:\.\d+)+[A-Za-z0-9+.]*)")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SIGNATURE_LEN = 64

# The installer's record of the one third-party program it executes (ODL-399).
# `install.sh` is fetched from a vendor host and run before the pinned commit
# applies; the pin lives in the signed installer, so the manifest reads it from
# there rather than keeping a second copy that could drift.
INSTALL_SCRIPT_FIELD = "hermes_install_sha256"

# The tenant backup program the nightly unit runs (ODL-215 follow-up). The
# manifest records the bytes the machine carries. A scratch root that predates
# the installer placing the file falls back to the installer's own embedded pin
# — the same record `--manifest` prints — because those are the bytes the
# installer would write, and a fresh root agrees with the pin by construction.
BACKUP_FIELD = "backup_sha256"
BACKUP_PROGRAM = "usr/local/lib/odile/backup.py"


class ManifestError(Exception):
    """A bad prerequisite, a bad input, or a fact the manifest cannot trust."""


# --------------------------------------------------------------------------
# small helpers


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> str:
    try:
        return sha256(Path(path).read_bytes())
    except OSError as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_now(now: str | datetime | None) -> str:
    if now is None:
        return utc_now()
    if isinstance(now, datetime):
        return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = str(now).strip()
    if not ISO_RE.match(text):
        raise ManifestError(f"--generated-at {text!r} is not an ISO-8601 UTC stamp like 2026-11-08T00:00:00Z")
    return text


def write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False)
    try:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.chmod(handle.name, mode)
        os.replace(handle.name, path)
    except BaseException:
        handle.close()
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# package versions: dpkg on a tenant, the toolchain on the simulation


def _run(command: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def extract_version(output: str) -> str | None:
    """The first dotted version in a `--version` line, e.g. 'restic 0.18.0'."""
    match = VERSION_RE.search(output)
    return match.group(1) if match else None


def collect_os_versions(*, mode: str, root: Path, runner=None) -> dict:
    """Every OS package version, or a loud refusal.

    A tenant reads dpkg: a package without an installed version means the
    installer did not pin it, and the manifest is not allowed to guess. The
    scratch simulation has no dpkg, so it probes the host binaries the
    simulation actually runs; a missing binary is recorded as null and named in
    `missing` rather than silently dropped.
    """
    run = runner or _run
    versions: dict[str, str | None] = {}
    missing: list[str] = []
    if mode == "tenant":
        for name in RUNTIME_PACKAGES:
            code, out, _err = run([DPKG_QUERY, "-W", "-f=${Version}", name])
            version = out.strip()
            if code != 0 or not version:
                missing.append(name)
            else:
                versions[name] = version
        if missing:
            raise ManifestError(
                f"no installed version for {', '.join(missing)}: no apt install without a "
                "version or a snapshot (dpkg-query on the tenant)"
            )
        code, out, _err = run([DPKG_QUERY, "-W", "-f=${Version}", "chromium"])
        if code == 0 and out.strip():
            versions["chromium"] = out.strip()
        return {"source": "dpkg", "os": versions, "missing": []}

    for name, (binary, flags) in PROBES.items():
        code, out, _err = run([binary, *flags])
        version = extract_version(out) if code == 0 else None
        versions[name] = version
        if version is None:
            missing.append(name)
    return {"source": "probe", "os": versions, "missing": missing}


def read_toolchain(facts_path: Path) -> dict:
    """The Hermes toolchain pins: version and digest per managed package."""
    try:
        data = json.loads(facts_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read the toolchain facts {facts_path}: {exc}") from exc
    except ValueError as exc:
        raise ManifestError(f"{facts_path} is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{facts_path} is not a JSON object")
    packages = data.get("packages")
    if not isinstance(packages, dict) or not packages:
        raise ManifestError(f"{facts_path} has no packages")
    toolchain = {}
    for name in sorted(packages):
        entry = packages[name]
        version = entry.get("version") if isinstance(entry, dict) else None
        if not version:
            raise ManifestError(f"{facts_path}: the {name} pin has no version")
        toolchain[name] = {"version": version, "digest": entry.get("digest")}
    return toolchain


# --------------------------------------------------------------------------
# skills and the Hermes source tree


def canonical_skill_bytes(data: bytes, *, root: Path, home: Path, user: str) -> bytes:
    """Canonicalise the machine facts the installer renders into a skill.

    `odile-home` names the tenant user and the home path. Those are machine
    layout, not app layer: if they stayed in the hash, two fresh roots could
    never agree and a tenant's manifest could never match the release it was
    installed from. The substitutions are exact and narrow.
    """
    data = data.replace(str(home).encode("utf-8"), b"<home>")
    if str(root) != "/":
        data = data.replace(str(root).encode("utf-8"), b"<root>")
    return data.replace(f"`{user}`".encode("utf-8"), b"`<user>`")


def hash_tree(directory: Path, *, canonicalize=None) -> str:
    """A content hash over every file in one skill directory.

    Paths are relative to the skill, so two roots at different absolute paths
    agree. Bytecode and caches are excluded: they are build output, not app
    layer, and `make compile` must not move a skill's hash.
    """
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            relative, data = path.relative_to(directory).as_posix(), os.readlink(path).encode()
        elif path.is_file():
            relative, data = path.relative_to(directory).as_posix(), path.read_bytes()
        else:
            continue
        if canonicalize is not None:
            data = canonicalize(relative, data)
        digest.update(relative.encode("utf-8") + b"\0" + sha256(data).encode("ascii") + b"\n")
    return digest.hexdigest()


def collect_skills(skills_root: Path, *, canonicalize=None) -> dict:
    if not skills_root.is_dir():
        raise ManifestError(f"no skills directory at {skills_root}; is this an installed root?")
    skills: dict[str, str] = {}
    for skill_md in sorted(skills_root.rglob("SKILL.md")):
        if skill_md.is_file():
            skills[skill_md.parent.relative_to(skills_root).as_posix()] = hash_tree(
                skill_md.parent, canonicalize=canonicalize)
    if not skills:
        raise ManifestError(f"no SKILL.md under {skills_root}")
    return skills


def git_head(checkout: Path) -> str | None:
    """The checkout's HEAD. The tree belongs to the tenant user, not to root:
    `safe.directory` names it exactly rather than trusting every repository.
    `GIT_*` is scrubbed so a hook's `GIT_INDEX_FILE` cannot redirect the read.
    """
    git = shutil.which("git")
    if git is None or not (checkout / ".git").exists():
        return None
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    proc = subprocess.run([git, "-c", f"safe.directory={checkout}", "-C", str(checkout),
                           "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=60, env=env)
    commit = proc.stdout.strip()
    if proc.returncode != 0 or not COMMIT_RE.match(commit):
        raise ManifestError(f"cannot read the Hermes commit from {checkout}: {proc.stderr.strip()}")
    return commit


def read_build_metadata(build_json: Path) -> dict:
    try:
        data = json.loads(build_json.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read the Webapp build metadata {build_json}: {exc}") from exc
    except ValueError as exc:
        raise ManifestError(f"{build_json} is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{build_json} is not a JSON object")
    raw_inputs = data.get("inputs")
    inputs = raw_inputs if isinstance(raw_inputs, dict) else {}
    webapp = {
        "node": data.get("node"),
        "source_sha256": inputs.get("sourceHash"),
        "output_sha256": data.get("outputHash"),
    }
    for key, value in webapp.items():
        if not value:
            raise ManifestError(f"{build_json} does not record the Webapp {key}")
    return {"webapp": webapp, "pinned_commit": data.get("pinnedCommit")}


def installed_python_packages(hermes_home: Path) -> list[str]:
    """Every Python package in the environment Hermes is actually running.

    The PM records the live venv in `installs/*/facts.json`; its dist-info
    metadata is the installed truth, not the lock's cross-platform resolution.
    A fresh install has exactly one environment, and a missing one is a broken
    install, not a manifest to guess at.
    """
    environments: list[Path] = []
    for facts_path in sorted((hermes_home / "installs").glob("*/facts.json")):
        try:
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ManifestError(f"cannot read the install facts {facts_path}: {exc}") from exc
        packages = facts.get("packages") if isinstance(facts, dict) else None
        venv_entry = packages.get("venv") if isinstance(packages, dict) else None
        venv = venv_entry.get("environment") if isinstance(venv_entry, dict) else None
        if isinstance(venv, str) and venv:
            environments.append(Path(venv))
    if not environments:
        environments = sorted((hermes_home / "installs").glob("*/environments/*/venv"))
    sites = []
    for venv in environments:
        sites.extend(sorted(venv.glob("lib/python*/site-packages")))
    if not sites:
        raise ManifestError(f"no installed Python environment under {hermes_home}/installs")
    pins = set()
    for site in sites:
        for info in sorted(site.glob("*.dist-info")):
            metadata = info / "METADATA"
            try:
                text = metadata.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name = version = None
            for line in text.splitlines():
                if not line.strip():
                    break
                if line.startswith("Name: "):
                    name = line[6:].strip()
                elif line.startswith("Version: "):
                    version = line[9:].strip()
            if not name or not version:
                raise ManifestError(f"{metadata} has no Name or Version")
            pins.add(f"{re.sub(r'[-_.]+', '-', name).lower()}=={version}")
    if not pins:
        raise ManifestError(f"no dist-info packages under {', '.join(str(site) for site in sites)}")
    return sorted(pins)


def read_npm_lock(lock: Path) -> list[str]:
    """Every resolved npm package version from the workspace lock file."""
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read {lock}: {exc}") from exc
    except ValueError as exc:
        raise ManifestError(f"{lock} is not JSON: {exc}") from exc
    packages = data.get("packages")
    if not isinstance(packages, dict) or not packages:
        raise ManifestError(f"{lock} records no packages")
    pins = set()
    for path, entry in packages.items():
        if not isinstance(entry, dict) or path == "":
            continue
        version = entry.get("version")
        if not version:
            continue
        name = entry.get("name") or str(path).rsplit("node_modules/", 1)[-1] or str(path)
        pins.add(f"{name}=={version}")
    if not pins:
        raise ManifestError(f"{lock} records no package versions")
    return sorted(pins)


# --------------------------------------------------------------------------
# the manifest


def resolve_layout(*, root: Path, user: str | None) -> dict:
    root = Path(root).expanduser()
    if not root.is_dir():
        raise ManifestError(f"the root {root} is not a directory")
    mode = "tenant" if root.resolve() == Path("/") else "scratch"
    home_root = root / "home"
    if user:
        home = home_root / user
    elif mode == "tenant":
        home = home_root / "odile"
    else:
        candidates = sorted(path for path in home_root.iterdir() if path.is_dir()) if home_root.is_dir() else []
        if len(candidates) != 1:
            raise ManifestError(f"--user is required: {home_root} has {len(candidates)} home directories")
        home = candidates[0]
    hermes_home = home / ".hermes"
    if not hermes_home.is_dir():
        raise ManifestError(f"no Hermes home at {hermes_home}; is this an installed root?")
    return {
        "root": root,
        "mode": mode,
        "home": home,
        "hermes_home": hermes_home,
        "install_dir": hermes_home / "hermes-agent",
        "skills_root": hermes_home / "skills",
        "seed": root / "etc/odile/seed.json",
    }


def resolve_installer(*, layout: dict, installer: str | None) -> Path:
    if installer:
        path = Path(installer).expanduser()
        if not path.is_file():
            raise ManifestError(f"the installer {path} does not exist")
        return path
    if layout["mode"] == "tenant":
        candidates = [Path("/usr/local/bin/odile-install.sh"), Path("/etc/odile/odile_install.py")]
    else:
        candidates = [layout["root"] / "bootstrap/odile_install.py",
                      layout["root"] / "usr/local/bin/odile-install.sh"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    named = ", ".join(str(path) for path in candidates)
    raise ManifestError(f"no installer found at {named}; pass --installer")


def resolve_audit(*, layout: dict, audit: str | None) -> Path | None:
    if audit:
        path = Path(audit).expanduser()
        if not path.is_file():
            raise ManifestError(f"the audit script {path} does not exist")
        return path
    candidates = [layout["root"] / "usr/local/bin/odile-audit"]
    if layout["mode"] == "tenant":
        candidates.append(Path("/usr/local/bin/odile-audit"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def read_installer_manifest(installer: Path) -> dict:
    """The installer's own `--manifest` record, which carries the install-script pin.

    `install.sh` is fetched from a vendor host and executed before the pinned
    commit applies, so the only record of it on a machine is the pin inside the
    installer that fetched and checked it. Reading that record keeps one source
    of truth: the signed installer, whose sha256 the manifest already names. An
    installer that cannot answer, or that predates the pin, is a refusal — a
    manifest that cannot name the script that ran is not this document.
    """
    code, out, err = _run([sys.executable, str(installer), "--manifest"])
    if code != 0:
        raise ManifestError(
            f"the installer {installer} could not print its --manifest ({code}): "
            f"{(err.strip() or out.strip())[-500:]}")
    try:
        document = json.loads(out)
    except ValueError as exc:
        raise ManifestError(f"the installer {installer} --manifest is not JSON: {exc}") from exc
    digest = document.get(INSTALL_SCRIPT_FIELD) if isinstance(document, dict) else None
    if not isinstance(digest, str) or not SHA256_RE.match(digest):
        raise ManifestError(
            f"the installer {installer} records no {INSTALL_SCRIPT_FIELD}; it predates the "
            "Hermes install-script pin (ODL-399), so the manifest cannot name the script that ran")
    return document


def collect_backup_hash(layout: dict, installer_record: dict) -> str:
    """The sha256 of the tenant backup program, from the root or the installer's pin.

    `build` records what the installer put on the machine, so the installed
    program is the fact: a one-byte edit on a root moves this row, which is how
    a release is shown to name the bytes it ships. A scratch root that carries
    no such file (one made before the installer placed it) falls back to the
    installer's own `--manifest` pin — the bytes that installer would write, so
    two fresh roots still agree row for row. A tenant root without the program
    is a refusal: the installer writes it, so its absence is a broken app layer,
    not a fact to paper over.
    """
    path = Path(layout["root"]) / BACKUP_PROGRAM
    if path.is_file():
        return hash_file(path)
    if layout["mode"] == "tenant":
        raise ManifestError(
            f"no tenant backup program at {path}; the installer writes it, so a tenant "
            "root without it is not the app layer this manifest claims")
    pin = installer_record.get(BACKUP_FIELD)
    if not isinstance(pin, str) or not SHA256_RE.match(pin):
        raise ManifestError(
            f"no tenant backup program at {path} and the installer records no "
            f"{BACKUP_FIELD}: the manifest cannot name the program the nightly unit runs")
    return pin


def build_manifest(root: str | Path, *, installer: str | Path | None = None,
                   audit: str | Path | None = None, model: str | None = None,
                   user: str | None = None, os_versions: dict | None = None,
                   now: str | datetime | None = None, runner=None) -> dict:
    layout = resolve_layout(root=Path(root), user=user)
    installer_path = resolve_installer(layout=layout, installer=str(installer) if installer else None)
    installer_record = read_installer_manifest(installer_path)
    audit_path = resolve_audit(layout=layout, audit=str(audit) if audit else None)

    if model:
        model_name = model
    else:
        try:
            seed = json.loads(layout["seed"].read_text(encoding="utf-8"))
        except OSError as exc:
            raise ManifestError(f"cannot read the seed cache {layout['seed']}: {exc}") from exc
        except ValueError as exc:
            raise ManifestError(f"{layout['seed']} is not JSON: {exc}") from exc
        model_name = seed.get("model")
        if not isinstance(model_name, str) or not model_name.strip():
            raise ManifestError(f"{layout['seed']} records no model")
        model_name = model_name.strip()

    if os_versions is None:
        packages = collect_os_versions(mode=layout["mode"], root=layout["root"], runner=runner)
    else:
        supplied = {name: os_versions.get(name) for name in os_versions}
        packages = {
            "source": "file",
            "os": supplied,
            "missing": [name for name in RUNTIME_PACKAGES if not supplied.get(name)],
        }
    packages["toolchain"] = read_toolchain(layout["hermes_home"] / "tools/facts.json")

    head = git_head(layout["install_dir"])
    build = read_build_metadata(
        layout["install_dir"] / "apps/desktop/dist-webapp/hermes-build.json")
    pinned = build["pinned_commit"]
    if head and pinned and head != pinned:
        raise ManifestError(
            f"the Webapp bundle was built from {pinned}, the checkout is at {head}")
    commit = head or pinned
    if not commit or not COMMIT_RE.match(commit):
        raise ManifestError("no Hermes commit: neither the checkout nor the bundle records one")

    bundle = layout["skills_root"] / ".bundled_manifest"
    return {
        "schema": SCHEMA,
        "generated_at": normalize_now(now),
        "mode": layout["mode"],
        "model": model_name,
        "installer_sha256": hash_file(installer_path),
        "backup_sha256": collect_backup_hash(layout, installer_record),
        "audit_sha256": hash_file(audit_path) if audit_path else None,
        "hermes": {
            "commit": commit,
            "commit_source": "git" if head else "build-metadata",
            "install_script_sha256": installer_record[INSTALL_SCRIPT_FIELD],
            "lockfiles": {
                "package-lock.json": hash_file(layout["install_dir"] / "package-lock.json"),
                "uv.lock": hash_file(layout["install_dir"] / "uv.lock"),
            },
            "node_packages": read_npm_lock(layout["install_dir"] / "package-lock.json"),
            "python_packages": installed_python_packages(layout["hermes_home"]),
            "webapp": build["webapp"],
        },
        "skills": collect_skills(
            layout["skills_root"],
            canonicalize=lambda _relative, data: canonical_skill_bytes(
                data, root=layout["root"], home=layout["home"], user=layout["home"].name),
        ),
        "skills_bundle_sha256": hash_file(bundle) if bundle.is_file() else None,
        "packages": packages,
    }


def render(manifest: dict) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def diff_manifests(first: dict, second: dict) -> tuple[bool, str]:
    """True when the two manifests differ in nothing but the timestamp."""
    left = {key: value for key, value in first.items() if key != "generated_at"}
    right = {key: value for key, value in second.items() if key != "generated_at"}
    if left == right:
        return True, "same (except generated_at)"
    lines = list(difflib.unified_diff(
        json.dumps(left, indent=2, sort_keys=True).splitlines(),
        json.dumps(right, indent=2, sort_keys=True).splitlines(),
        fromfile="manifest-a", tofile="manifest-b", lineterm="",
    ))
    return False, "\n".join(lines)


# --------------------------------------------------------------------------
# Ed25519 over the system OpenSSL (the standard library has no Ed25519)


def _openssl() -> str:
    found = shutil.which("openssl")
    if found is None:
        raise ManifestError("openssl is not installed")
    return found


def _safe_mode(path: Path, what: str) -> None:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        raise ManifestError(f"cannot stat the {what} {path}: {exc}") from exc
    if mode & 0o077:
        raise ManifestError(f"the {what} {path} is mode {mode:04o}; it must be 0600")


def sign_bytes(data: bytes, key_path: str | Path) -> bytes:
    """A 64-byte Ed25519 signature over the exact bytes, via `openssl dgst`."""
    key = Path(key_path)
    _safe_mode(key, "private key")
    proc = subprocess.run(
        [_openssl(), "dgst", "-sign", str(key), "-out", "/dev/stdout"],
        input=data, capture_output=True,
    )
    if proc.returncode != 0:
        raise ManifestError(f"openssl could not sign with {key}: "
                            f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    if len(proc.stdout) != SIGNATURE_LEN:
        raise ManifestError(f"openssl returned {len(proc.stdout)} bytes, expected {SIGNATURE_LEN}")
    return proc.stdout


def verify_bytes(data: bytes, signature: bytes, pubkey_path: str | Path) -> bool:
    """True when `signature` is a valid Ed25519 signature over the exact bytes."""
    pubkey = Path(pubkey_path)
    if not pubkey.is_file():
        raise ManifestError(f"the public key {pubkey} does not exist")
    with tempfile.NamedTemporaryFile(prefix="odile-manifest-sig-", delete=False) as handle:
        handle.write(signature)
        sig_path = handle.name
    try:
        proc = subprocess.run(
            [_openssl(), "dgst", "-verify", str(pubkey), "-signature", sig_path],
            input=data, capture_output=True,
        )
    finally:
        try:
            os.unlink(sig_path)
        except OSError:
            pass
    return proc.returncode == 0


def read_signature(path: Path) -> bytes:
    raw = Path(path).read_bytes()
    if len(raw) == SIGNATURE_LEN:
        return raw
    text = b"".join(raw.split())
    try:
        decoded = base64.b64decode(text, validate=True)
    except ValueError as exc:
        raise ManifestError(f"{path} is neither a raw nor a base64 Ed25519 signature") from exc
    if len(decoded) != SIGNATURE_LEN:
        raise ManifestError(f"{path} decodes to {len(decoded)} bytes, expected {SIGNATURE_LEN}")
    return decoded


def write_signature(path: Path, signature: bytes) -> None:
    text = base64.b64encode(signature).decode("ascii")
    body = "\n".join(text[index:index + 76] for index in range(0, len(text), 76)) + "\n"
    write_atomic(Path(path), body.encode("ascii"), 0o644)


def key_fingerprint(path: Path, *, private: bool) -> str:
    """An SSH-style SHA256 fingerprint of the key's public half."""
    if private:
        command = [_openssl(), "pkey", "-in", str(path), "-pubout", "-outform", "DER"]
    else:
        command = [_openssl(), "pkey", "-pubin", "-in", str(path), "-outform", "DER"]
    proc = subprocess.run(command, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise ManifestError(f"cannot read the public half of {path}: "
                            f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    digest = hashlib.sha256(proc.stdout).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------
# the guest's /manifest listener


class ManifestHandler(BaseHTTPRequestHandler):
    """`/manifest`, `/manifest.sig` and `/health`; nothing else, no access log."""

    server_version = "odile-manifest/0.1"
    manifest_path: Path
    signature_path: Path | None = None

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass

    def do_HEAD(self) -> None:
        self._route(head=True)

    def do_GET(self) -> None:
        self._route(head=False)

    def _route(self, head: bool) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/manifest":
            return self._file(self.manifest_path, "application/json; charset=utf-8", head)
        if path == "/manifest.sig" and self.signature_path is not None:
            return self._file(self.signature_path, "text/plain; charset=utf-8", head)
        if path == "/health":
            return self._send(200, b"ok\n", "text/plain; charset=utf-8", head)
        return self._send(404, b"not found\n", "text/plain; charset=utf-8", head)

    def _file(self, path: Path, content_type: str, head: bool) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            return self._send(503, b"manifest unavailable\n",
                              "text/plain; charset=utf-8", head, cache="no-store")
        return self._send(200, data, content_type, head, cache="no-store")

    def _send(self, status: int, body: bytes, content_type: str, head: bool,
              *, cache: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        if not head:
            self.wfile.write(body)


def make_server(manifest: Path, signature: Path | None, bind: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundManifestHandler", (ManifestHandler,), {
        "manifest_path": Path(manifest),
        "signature_path": Path(signature) if signature else None,
    })
    server = ThreadingHTTPServer((bind, port), handler)
    server.daemon_threads = True
    return server


# --------------------------------------------------------------------------
# the command line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manifest.py",
        description="Build, sign, verify, diff and serve the Odile app-layer manifest.",
    )
    parser.add_argument("--version", action="version", version=f"odile-manifest {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="build a manifest from an installed root")
    build.add_argument("--root", default="/", help="the installed root (default /; a scratch root for the simulation)")
    build.add_argument("--user", help="the tenant user (tenant default: odile; a scratch root needs one home)")
    build.add_argument("--installer", help="the installer file that made the machine")
    build.add_argument("--audit", help="the handoff audit script (ODL-208), if it is installed")
    build.add_argument("--model", help="the model pin (default: the seed cache's model)")
    build.add_argument("--packages-json", help="a JSON object of package versions, instead of probing or dpkg")
    build.add_argument("--generated-at", help="an ISO-8601 UTC stamp, for a reproducible diff")
    build.add_argument("--out", help="write the manifest here instead of stdout")

    sign = commands.add_parser("sign", help="sign the exact manifest bytes with an HQ key")
    sign.add_argument("--manifest", required=True)
    sign.add_argument("--key", required=True, help="the Ed25519 private key (0600)")
    sign.add_argument("--out", help="the signature file (default: <manifest>.sig)")

    verify = commands.add_parser("verify", help="check a manifest against its signature")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--signature", help="the signature file (default: <manifest>.sig)")
    verify.add_argument("--pubkey",
                        help="the public key (default: the repo's installer/odile-signing-key.pub)")

    diff = commands.add_parser("diff", help="compare two manifests, ignoring the timestamp")
    diff.add_argument("first")
    diff.add_argument("second")

    serve = commands.add_parser("serve", help="serve /manifest on loopback for Caddy")
    serve.add_argument("--manifest", required=True)
    serve.add_argument("--signature", help="the signature file (default: <manifest>.sig if it exists)")
    serve.add_argument("--bind", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8012)
    return parser


def _read_manifest(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise ManifestError(f"{path} is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{path} is not a JSON object")
    return data


def cmd_build(args: argparse.Namespace) -> int:
    versions = None
    if args.packages_json:
        data = _read_manifest(args.packages_json)
        if not isinstance(data, dict) or not all(isinstance(value, str) for value in data.values()):
            raise ManifestError(f"{args.packages_json} must be a JSON object of name -> version")
        versions = data
    manifest = build_manifest(
        args.root, installer=args.installer, audit=args.audit, model=args.model,
        user=args.user, os_versions=versions, now=args.generated_at,
    )
    data = render(manifest)
    if args.out:
        write_atomic(Path(args.out), data, 0o644)
        audit = manifest["audit_sha256"] or "null"
        print(f"manifest     {args.out} (sha256 {sha256(data)}, "
              f"{len(manifest['skills'])} skills, {len(manifest['packages']['os'])} packages, "
              f"audit {audit})")
    else:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest)
    data = manifest.read_bytes()
    signature = sign_bytes(data, args.key)
    out = Path(args.out) if args.out else manifest.with_name(manifest.name + ".sig")
    write_signature(out, signature)
    print(f"signature    {out} (key {key_fingerprint(Path(args.key), private=True)}, "
          f"sha256 {sha256(data)})")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest)
    signature_path = Path(args.signature) if args.signature else manifest.with_name(manifest.name + ".sig")
    pubkey = Path(args.pubkey) if args.pubkey else (
        Path(__file__).resolve().parent.parent / "installer" / "odile-signing-key.pub")
    data = manifest.read_bytes()
    ok = verify_bytes(data, read_signature(signature_path), pubkey)
    print(f"signature: {'OK' if ok else 'BAD'} (sha256 {sha256(data)}, "
          f"key {key_fingerprint(pubkey, private=False)})")
    if not ok:
        return 3
    document = _read_manifest(manifest)
    skills = document.get("skills")
    hermes = document.get("hermes")
    count = len(skills) if isinstance(skills, dict) else 0
    commit = hermes.get("commit") if isinstance(hermes, dict) else ""
    print(f"manifest     schema {document.get('schema')} · {count} skills · "
          f"hermes {str(commit)[:12]} · {document.get('model')}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    same, detail = diff_manifests(_read_manifest(args.first), _read_manifest(args.second))
    print(detail)
    return 0 if same else 1


def cmd_serve(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest)
    signature = Path(args.signature) if args.signature else manifest.with_name(manifest.name + ".sig")
    server = make_server(manifest, signature if signature.is_file() else None, args.bind, args.port)
    print(f"listening    {args.bind}:{server.server_address[1]} (pid {os.getpid()})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


COMMANDS = {
    "build": cmd_build,
    "sign": cmd_sign,
    "verify": cmd_verify,
    "diff": cmd_diff,
    "serve": cmd_serve,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
