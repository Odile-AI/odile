#!/usr/bin/python3
"""odile-update — the guest's pull-based app-layer update (ODL-219).

The tenant fetches a signed release document from HQ (by default
``https://odile.fyi/release.json``), verifies its Ed25519 signature and the
app-layer manifest the release names, and applies **only** the app layer:

  * the installer (``/usr/local/bin/odile-install.sh``),
  * the skills (``~/.hermes/skills``), in a canonical, machine-independent form,
  * the Webapp bundle and, when a release changes it, the Hermes commit.

The boot chain is pinned. Firmware, kernel, initramfs and the command line are
not app layer: a release built for another kernel release is refused, because
those change by rebuild-and-restore, not in place. The same is true of the OS
packages, the Python environment and the npm tree: the manifest records them,
and a release whose pins no longer match this machine is refused rather than
half-applied.

The guest initiates. Nothing here listens for a connection from HQ and no
operator key or shell is involved; the only credential is Odile's public signing
key, which the recovery kit carries (``installer/odile-signing-key.pub``).

    odile-update status
    odile-update check                 # fetch, verify, print the plan; no writes
    odile-update apply                 # asks first; --yes for a timer
    odile-update apply --dry-run
    odile-update rollback --yes        # restores the one snapshot kept
    odile-update release --root <candidate> --user <u> --manifest <manifest.json> \
        --release 2026.11.08-2 --boot-kernel "$(uname -r)" --out-dir <dir> \
        --key /root/.odile/installer-signing.pem     # HQ only, cuts the release
    odile-update self-test

Exit codes: 0 success; 2 a bad input or prerequisite; 3 a signature does not
verify; 4 the release is internally inconsistent (hash, schema, artifact);
5 refused for this machine (boot chain, downgrade, missing Hermes commit);
6 the post-update check failed and the snapshot was restored; 7 nothing to roll
back to. ``check`` also exits 10 when the machine is already at the release.

The runbook is ``docs/runbooks/update.md``. The release schema is documented
there; in short, ``release.json`` carries the release id, the boot kernel it was
built for, a digest and URLs for the signed ``manifest.json``, and one URL and
sha256 per artifact. Everything in it is covered by one Ed25519 signature over
the exact bytes served.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

VERSION = "0.1.0"
SCHEMA = 1
DEFAULT_RELEASE_URL = "https://odile.fyi/release.json"
SIG_SUFFIX = ".sig"
DEFAULT_TENANT_USER = "odile"
ARTIFACT_NAMES = ("installer", "skills", "webapp")
SIGNATURE_LEN = 64
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
RELEASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_BAD_SIGNATURE = 3
EXIT_INCONSISTENT = 4
EXIT_REFUSED = 5
EXIT_VERIFY_FAILED = 6
EXIT_NO_SNAPSHOT = 7
EXIT_ALREADY_CURRENT = 10


class UpdateError(Exception):
    """A refusal with an exit code and one sentence a user can act on."""

    def __init__(self, message: str, code: int = EXIT_BAD_INPUT):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# small helpers


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    try:
        return sha256(Path(path).read_bytes())
    except OSError:
        return None


def read_bytes(path: Path) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def write_atomic(path: Path, data: bytes, mode: int = 0o644,
                 uid: int | None = None, gid: int | None = None) -> None:
    """Write a file whole; a reader sees the old bytes or the new, never half."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False)
    try:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.chmod(handle.name, mode)
        if uid is not None and gid is not None:
            os.chown(handle.name, uid, gid)
        os.replace(handle.name, path)
    except BaseException:
        handle.close()
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def owner_of(path: Path) -> tuple[int, int]:
    try:
        info = path.stat()
    except OSError:
        return 0, 0
    return info.st_uid, info.st_gid


def mode_of(path: Path, default: int = 0o644) -> int:
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return default


def run(command: list[str], *, timeout: float = 300, env: dict[str, str] | None = None,
        check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)
    if check and proc.returncode != 0:
        raise UpdateError(f"{command[0]} failed ({proc.returncode}): {proc.stderr.strip()[:500]}")
    return proc


def git_env() -> dict[str, str]:
    """A child git must not inherit GIT_DIR/GIT_INDEX_FILE from a hook."""
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


# ---------------------------------------------------------------------------
# the layout: a tenant root ("/") or a scratch root, one code path


@dataclass(frozen=True)
class Layout:
    mode: str
    root: Path
    user: str
    home: Path
    hermes_home: Path
    install_dir: Path
    skills: Path
    webapp: Path
    installer: Path
    etc: Path
    var: Path
    unit_dir: Path

    @property
    def records(self) -> Path:
        return self.etc

    @property
    def backup_program(self) -> Path:
        """The tenant backup program the nightly unit runs (ODL-215 follow-up)."""
        return self.root / "usr/local/lib/odile/backup.py"


def resolve_installer(root: Path, mode: str) -> Path:
    if mode == "tenant":
        candidates = [Path("/usr/local/bin/odile-install.sh"), Path("/etc/odile/odile_install.py")]
    else:
        candidates = [root / "bootstrap/odile_install.py", root / "usr/local/bin/odile-install.sh"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    named = ", ".join(str(path) for path in candidates)
    raise UpdateError(f"no installer found at {named}; is this an installed root?", EXIT_BAD_INPUT)


def resolve_layout(root: str | Path = "/", user: str | None = None) -> Layout:
    root = Path(root).expanduser()
    if not root.is_dir():
        raise UpdateError(f"the root {root} is not a directory", EXIT_BAD_INPUT)
    mode = "tenant" if root.resolve() == Path("/") else "scratch"
    home_root = root / "home"
    if user:
        home = home_root / user
    elif mode == "tenant":
        home = home_root / DEFAULT_TENANT_USER
    else:
        candidates = sorted(path for path in home_root.iterdir() if path.is_dir()) if home_root.is_dir() else []
        if len(candidates) != 1:
            raise UpdateError(f"--user is required: {home_root} has {len(candidates)} home directories",
                              EXIT_BAD_INPUT)
        home = candidates[0]
    if not home.is_dir():
        raise UpdateError(f"no home directory at {home}; is this an installed root?", EXIT_BAD_INPUT)
    hermes_home = home / ".hermes"
    if not hermes_home.is_dir():
        raise UpdateError(f"no Hermes home at {hermes_home}; is this an installed root?", EXIT_BAD_INPUT)
    install_dir = hermes_home / "hermes-agent"
    return Layout(
        mode=mode,
        root=root,
        user=home.name,
        home=home,
        hermes_home=hermes_home,
        install_dir=install_dir,
        skills=hermes_home / "skills",
        webapp=install_dir / "apps/desktop/dist-webapp",
        installer=resolve_installer(root, mode),
        etc=root / "etc/odile",
        var=root / "var/lib/odile",
        unit_dir=root / "etc/systemd/system",
    )


# ---------------------------------------------------------------------------
# canonical skills: the machine facts are layout, not app layer
#
# The two functions must stay byte-compatible with `scripts/manifest.py`:
# `canonical_skill_bytes` there and `canonical_bytes` here decide whether a
# tenant and the release it was installed from agree on one skill hash. The
# self-test cross-checks them on the same fixture when the repo is present.


def canonical_bytes(data: bytes, layout: Layout) -> bytes:
    data = data.replace(str(layout.home).encode("utf-8"), b"<home>")
    if str(layout.root) != "/":
        data = data.replace(str(layout.root).encode("utf-8"), b"<root>")
    return data.replace(f"`{layout.user}`".encode("utf-8"), b"`<user>`")


def render_bytes(data: bytes, layout: Layout) -> bytes:
    """The inverse of canonical_bytes, for installing a release skill here."""
    if str(layout.root) != "/":
        data = data.replace(b"<root>", str(layout.root).encode("utf-8"))
    data = data.replace(b"<home>", str(layout.home).encode("utf-8"))
    return data.replace(b"`<user>`", f"`{layout.user}`".encode("utf-8"))


def hash_tree(directory: Path, layout: Layout) -> str:
    """A content hash over one skill directory, in canonical form.

    Paths are relative to the skill, so two roots at different absolute paths
    agree. Bytecode and caches are excluded: they are build output.
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
        data = canonical_bytes(data, layout)
        digest.update(relative.encode("utf-8") + b"\0" + sha256(data).encode("ascii") + b"\n")
    return digest.hexdigest()


def collect_skills(skills_root: Path, layout: Layout) -> dict[str, str]:
    if not skills_root.is_dir():
        raise UpdateError(f"no skills directory at {skills_root}; is this an installed root?", EXIT_BAD_INPUT)
    skills: dict[str, str] = {}
    for skill_md in sorted(skills_root.rglob("SKILL.md")):
        if skill_md.is_file():
            skills[skill_md.parent.relative_to(skills_root).as_posix()] = hash_tree(skill_md.parent, layout)
    if not skills:
        raise UpdateError(f"no SKILL.md under {skills_root}", EXIT_BAD_INPUT)
    return skills


def tree_digest(directory: Path) -> str:
    """A byte-exact digest of a tree, used to prove a rollback restored it."""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            digest.update(path.relative_to(directory).as_posix().encode() + b"\0link\0"
                          + os.readlink(path).encode() + b"\n")
        elif path.is_file():
            digest.update(path.relative_to(directory).as_posix().encode() + b"\0file\0"
                          + sha256(path.read_bytes()).encode("ascii") + b"\n")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Ed25519 over the system OpenSSL (the standard library has no Ed25519)


def openssl() -> str:
    found = shutil.which("openssl")
    if found is None:
        raise UpdateError("openssl is not installed", EXIT_BAD_INPUT)
    return found


def verify_bytes(data: bytes, signature: bytes, pubkey_path: Path) -> bool:
    pubkey = Path(pubkey_path)
    if not pubkey.is_file():
        raise UpdateError(f"the public key {pubkey} does not exist", EXIT_BAD_INPUT)
    with tempfile.NamedTemporaryFile(prefix="odile-update-sig-", delete=False) as handle:
        handle.write(signature)
        sig_path = handle.name
    try:
        proc = subprocess.run([openssl(), "dgst", "-verify", str(pubkey), "-signature", sig_path],
                              input=data, capture_output=True)
    finally:
        try:
            os.unlink(sig_path)
        except OSError:
            pass
    return proc.returncode == 0


def signature_bytes(raw: bytes, label: str) -> bytes:
    """A detached signature as raw 64 bytes, or base64 with whitespace."""
    if len(raw) == SIGNATURE_LEN:
        return raw
    text = b"".join(raw.split())
    try:
        decoded = base64.b64decode(text, validate=True)
    except ValueError as exc:
        raise UpdateError(f"{label} is neither a raw nor a base64 Ed25519 signature", EXIT_INCONSISTENT) from exc
    if len(decoded) != SIGNATURE_LEN:
        raise UpdateError(f"{label} decodes to {len(decoded)} bytes, expected {SIGNATURE_LEN}",
                          EXIT_INCONSISTENT)
    return decoded


def sign_bytes(data: bytes, key_path: Path) -> bytes:
    key = Path(key_path)
    try:
        mode = key.stat().st_mode & 0o777
    except OSError as exc:
        raise UpdateError(f"cannot stat the private key {key}: {exc}", EXIT_BAD_INPUT) from exc
    if mode & 0o077:
        raise UpdateError(f"the private key {key} is mode {mode:04o}; it must be 0600", EXIT_BAD_INPUT)
    proc = subprocess.run([openssl(), "dgst", "-sign", str(key), "-out", "/dev/stdout"],
                          input=data, capture_output=True)
    if proc.returncode != 0 or len(proc.stdout) != SIGNATURE_LEN:
        raise UpdateError(f"openssl could not sign with {key}: "
                          f"{proc.stderr.decode('utf-8', 'replace').strip()}", EXIT_BAD_INPUT)
    return proc.stdout


def key_fingerprint(path: Path, *, private: bool) -> str:
    if private:
        command = [openssl(), "pkey", "-in", str(path), "-pubout", "-outform", "DER"]
    else:
        command = [openssl(), "pkey", "-pubin", "-in", str(path), "-outform", "DER"]
    proc = subprocess.run(command, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise UpdateError(f"cannot read the public half of {path}: "
                          f"{proc.stderr.decode('utf-8', 'replace').strip()}", EXIT_BAD_INPUT)
    return "SHA256:" + base64.b64encode(hashlib.sha256(proc.stdout).digest()).decode("ascii").rstrip("=")


def signature_file_bytes(signature: bytes) -> bytes:
    """The repo's on-disk form: base64, 76 columns, one trailing newline."""
    text = base64.b64encode(signature).decode("ascii")
    return ("\n".join(text[index:index + 76] for index in range(0, len(text), 76)) + "\n").encode("ascii")


# ---------------------------------------------------------------------------
# fetching and the release document


def fetch_url(url: str, timeout: float = 60) -> bytes:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UpdateError(f"{url} is not an http(s) URL", EXIT_BAD_INPUT)
    request = urllib.request.Request(url, headers={
        "User-Agent": f"odile-update/{VERSION}",
        "Cache-Control": "no-cache",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            data = response.read()
    except OSError as exc:
        raise UpdateError(f"cannot fetch {url}: {exc}", EXIT_BAD_INPUT) from exc
    if status != 200:
        raise UpdateError(f"{url} answered HTTP {status}", EXIT_BAD_INPUT)
    return data


def resolve_url(base: str, reference: str) -> str:
    joined = urllib.parse.urljoin(base, reference)
    if urllib.parse.urlsplit(joined).scheme not in ("http", "https"):
        raise UpdateError(f"{reference!r} does not resolve to an http(s) URL", EXIT_INCONSISTENT)
    return joined


def parse_json(data: bytes, label: str) -> dict:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError(f"{label} is not JSON: {exc}", EXIT_INCONSISTENT) from exc
    if not isinstance(document, dict):
        raise UpdateError(f"{label} is not a JSON object", EXIT_INCONSISTENT)
    return document


def require(condition: object, message: str, code: int = EXIT_INCONSISTENT) -> None:
    if not condition:
        raise UpdateError(message, code)


def validate_release_document(document: dict) -> None:
    require(document.get("schema") == SCHEMA, f"release schema {document.get('schema')!r} is not {SCHEMA}")
    require(document.get("product") == "odile", "release product is not 'odile'")
    release = document.get("release")
    require(isinstance(release, str) and bool(RELEASE_RE.match(release)),
            "release has no valid id (letters, digits, '.', '_', '-')")
    generated = document.get("generated_at")
    require(isinstance(generated, str) and bool(ISO_RE.match(generated)),
            "release has no ISO-8601 generated_at")
    boot = document.get("boot")
    require(isinstance(boot, dict) and isinstance(boot.get("kernel"), str) and boot["kernel"].strip(),
            "release has no boot.kernel")
    manifest = document.get("manifest")
    require(isinstance(manifest, dict), "release has no manifest block")
    require(isinstance(manifest.get("url"), str) and manifest["url"], "release manifest has no url")
    require(isinstance(manifest.get("sha256"), str) and bool(HEX_RE.match(manifest["sha256"])),
            "release manifest has no sha256")
    signature_url = manifest.get("signature_url", manifest["url"] + SIG_SUFFIX)
    require(isinstance(signature_url, str) and signature_url, "release manifest has no signature_url")
    artifacts = document.get("artifacts", {})
    require(isinstance(artifacts, dict), "release artifacts is not an object")
    for name, spec in artifacts.items():
        require(name in ARTIFACT_NAMES, f"release carries an unknown artifact {name!r}")
        require(isinstance(spec, dict), f"artifact {name} is not an object")
        require(isinstance(spec.get("url"), str) and spec["url"], f"artifact {name} has no url")
        require(isinstance(spec.get("sha256"), str) and bool(HEX_RE.match(spec["sha256"])),
                f"artifact {name} has no sha256")
    notes = document.get("notes")
    require(notes is None or isinstance(notes, str), "release notes is not a string")


def validate_manifest(manifest: dict) -> None:
    require(manifest.get("schema") == SCHEMA, f"manifest schema {manifest.get('schema')!r} is not {SCHEMA}")
    installer = manifest.get("installer_sha256")
    require(isinstance(installer, str) and bool(HEX_RE.match(installer)),
            "manifest has no installer_sha256")
    hermes = manifest.get("hermes")
    require(isinstance(hermes, dict), "manifest has no hermes block")
    require(isinstance(hermes.get("commit"), str) and bool(COMMIT_RE.match(hermes["commit"])),
            "manifest hermes.commit is not a 40-hex commit")
    webapp = hermes.get("webapp")
    require(isinstance(webapp, dict), "manifest hermes.webapp is missing")
    require(isinstance(webapp.get("node"), str) and webapp["node"], "manifest hermes.webapp.node is missing")
    for key in ("source_sha256", "output_sha256"):
        require(isinstance(webapp.get(key), str) and bool(HEX_RE.match(webapp[key])),
                f"manifest hermes.webapp.{key} is missing")
    skills = manifest.get("skills")
    require(isinstance(skills, dict) and bool(skills), "manifest has no skills map")
    for name, digest in skills.items():
        require(isinstance(name, str) and name, "manifest skills has an empty name")
        require(isinstance(digest, str) and bool(HEX_RE.match(digest)),
                f"manifest skill {name!r} has no sha256")
    bundle = manifest.get("skills_bundle_sha256")
    require(bundle is None or (isinstance(bundle, str) and bool(HEX_RE.match(bundle))),
            "manifest skills_bundle_sha256 is neither null nor a sha256")
    # Optional: a release cut before the row existed still verifies (the live
    # `2026.11.19-1` manifest is one), and `load_release` warns about it. A
    # value that is present has to be a real digest, not a placeholder.
    backup = manifest.get("backup_sha256")
    require(backup is None or (isinstance(backup, str) and bool(HEX_RE.match(backup))),
            "manifest backup_sha256 is neither absent nor a sha256")


@dataclass
class Release:
    url: str
    document: dict
    document_bytes: bytes
    document_signature: bytes
    manifest: dict
    manifest_bytes: bytes
    manifest_signature: bytes
    artifacts: dict[str, bytes] = field(default_factory=dict)
    artifact_urls: dict[str, str] = field(default_factory=dict)

    @property
    def release_id(self) -> str:
        return self.document["release"]

    @property
    def generated_at(self) -> str:
        return self.document["generated_at"]

    @property
    def boot_kernel(self) -> str:
        return self.document["boot"]["kernel"]

    @property
    def notes(self) -> str:
        return self.document.get("notes") or ""


def load_release(url: str, pubkey_path: Path, *, fetch=fetch_url,
                 kernel: str | None = None) -> Release:
    """Fetch and verify the release, its manifest and every artifact it names.

    Nothing is written. Every failure is classified: a bad signature is 3, a
    document that contradicts itself or its manifest is 4, and a release built
    for another boot chain is 5.
    """
    document_bytes = fetch(url)
    document_signature_raw = fetch(url + SIG_SUFFIX)
    document_signature = signature_bytes(document_signature_raw, url + SIG_SUFFIX)
    if not verify_bytes(document_bytes, document_signature, pubkey_path):
        raise UpdateError(f"the signature on {url} is not valid for this key", EXIT_BAD_SIGNATURE)
    document = parse_json(document_bytes, url)
    validate_release_document(document)

    manifest_url = resolve_url(url, document["manifest"]["url"])
    manifest_bytes = fetch(manifest_url)
    if sha256(manifest_bytes) != document["manifest"]["sha256"]:
        raise UpdateError("the manifest bytes are not the ones the signed release names",
                          EXIT_INCONSISTENT)
    signature_url = resolve_url(url, document["manifest"].get("signature_url",
                                                              document["manifest"]["url"] + SIG_SUFFIX))
    manifest_signature = signature_bytes(fetch(signature_url), signature_url)
    if not verify_bytes(manifest_bytes, manifest_signature, pubkey_path):
        raise UpdateError(f"the signature on {manifest_url} is not valid for this key",
                          EXIT_BAD_SIGNATURE)
    manifest = parse_json(manifest_bytes, manifest_url)
    validate_manifest(manifest)
    if not manifest.get("backup_sha256"):
        print(f"warning: the manifest at {manifest_url} names no backup_sha256; it predates "
              "the tenant backup program's row, so this release does not pin the program "
              "the nightly unit runs (a later cut carries it)", file=sys.stderr)

    machine_kernel = kernel if kernel is not None else os.uname().release
    if document["boot"]["kernel"] != machine_kernel:
        raise UpdateError(
            f"release {document['release']} was built for kernel {document['boot']['kernel']}, "
            f"this machine runs {machine_kernel}: a boot-chain change is rebuild-and-restore",
            EXIT_REFUSED)

    artifacts: dict[str, bytes] = {}
    artifact_urls: dict[str, str] = {}
    for name, spec in document.get("artifacts", {}).items():
        artifact_url = resolve_url(url, spec["url"])
        data = fetch(artifact_url)
        if sha256(data) != spec["sha256"]:
            raise UpdateError(f"the {name} artifact does not match its signed sha256",
                              EXIT_INCONSISTENT)
        artifacts[name] = data
        artifact_urls[name] = artifact_url

    for name, field_name in (("installer", "installer_sha256"),):
        if name in artifacts and sha256(artifacts[name]) != manifest[field_name]:
            raise UpdateError(f"the {name} artifact contradicts {field_name} in the manifest",
                              EXIT_INCONSISTENT)
    if "skills" in artifacts and not artifacts["skills"]:
        raise UpdateError("the skills artifact is empty", EXIT_INCONSISTENT)

    return Release(url=url, document=document, document_bytes=document_bytes,
                   document_signature=document_signature, manifest=manifest,
                   manifest_bytes=manifest_bytes, manifest_signature=manifest_signature,
                   artifacts=artifacts, artifact_urls=artifact_urls)


# ---------------------------------------------------------------------------
# the installed machine


def git_run(layout: Layout, *args: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise UpdateError("git is not installed", EXIT_BAD_INPUT)
    if not (layout.install_dir / ".git").exists():
        raise UpdateError(f"{layout.install_dir} is not a git checkout", EXIT_BAD_INPUT)
    return subprocess.run([git, "-c", f"safe.directory={layout.install_dir}",
                           "-C", str(layout.install_dir), *args],
                          capture_output=True, text=True, timeout=120, env=git_env())


def git_head(layout: Layout) -> str | None:
    proc = git_run(layout, "rev-parse", "HEAD")
    commit = proc.stdout.strip()
    return commit if proc.returncode == 0 and COMMIT_RE.match(commit) else None


def git_has_commit(layout: Layout, commit: str) -> bool:
    return git_run(layout, "cat-file", "-e", f"{commit}^{{commit}}").returncode == 0


def git_is_clean(layout: Layout) -> tuple[bool, str]:
    proc = git_run(layout, "status", "--porcelain", "--untracked-files=no")
    return proc.returncode == 0 and not proc.stdout.strip(), proc.stdout.strip()


def read_webapp_build(layout: Layout) -> dict | None:
    data = read_bytes(layout.webapp / "hermes-build.json")
    if data is None:
        return None
    try:
        build = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(build, dict):
        return None
    inputs = build.get("inputs") if isinstance(build.get("inputs"), dict) else {}
    return {
        "node": build.get("node"),
        "source_sha256": inputs.get("sourceHash"),
        "output_sha256": build.get("outputHash"),
    }


def installed_release(layout: Layout) -> dict | None:
    data = read_bytes(layout.etc / "release.json")
    if data is None:
        return None
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def installed_manifest(layout: Layout) -> dict | None:
    data = read_bytes(layout.etc / "manifest.json")
    if data is None:
        return None
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return document if isinstance(document, dict) else None


# ---------------------------------------------------------------------------
# the plan: every comparison before the first write


def installed_python_pins(layout: Layout) -> list[str] | None:
    """The live venv's installed pins, the way scripts/manifest.py reads them.

    Used only to prove that a release which moves the Hermes commit does not
    also move the Python environment: a dependency change is a reinstall, not
    an in-place update.
    """
    environments: list[Path] = []
    for facts_path in sorted((layout.hermes_home / "installs").glob("*/facts.json")):
        try:
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        packages = facts.get("packages") if isinstance(facts, dict) else None
        venv_entry = packages.get("venv") if isinstance(packages, dict) else None
        venv = venv_entry.get("environment") if isinstance(venv_entry, dict) else None
        if isinstance(venv, str) and venv and Path(venv).is_dir():
            environments.append(Path(venv))
    if not environments:
        environments = sorted((layout.hermes_home / "installs").glob("*/environments/*/venv"))
    sites: list[Path] = []
    for venv in environments:
        sites.extend(sorted(venv.glob("lib/python*/site-packages")))
    pins: set[str] = set()
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
            if name and version:
                pins.add(f"{re.sub(r'[-_.]+', '-', name).lower()}=={version}")
    return sorted(pins) if pins else None


@dataclass
class Change:
    name: str
    action: str
    detail: str


@dataclass
class Plan:
    changes: list[Change] = field(default_factory=list)
    installed_skills: dict[str, str] = field(default_factory=dict)
    target_skills: dict[str, str] = field(default_factory=dict)
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    staging: Path | None = None
    records_differ: bool = False

    @property
    def skills_changed(self) -> bool:
        return bool(self.added or self.updated)

    def empty(self) -> bool:
        return not self.changes

    def stage(self) -> Path:
        """One temporary root per plan; no component leaves another's litter."""
        if self.staging is None:
            self.staging = Path(tempfile.mkdtemp(prefix="odile-update-"))
        return self.staging

    def cleanup(self) -> None:
        if self.staging is not None:
            shutil.rmtree(self.staging, ignore_errors=True)
            self.staging = None


def extract_tar(data: bytes, destination: Path, label: str) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            archive.extractall(path=destination, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise UpdateError(f"{label} is not a readable tar.gz: {exc}", EXIT_INCONSISTENT) from exc
    return destination


def plan_changes(release: Release, layout: Layout) -> Plan:
    """Compare the release's app layer to this machine; refuse before writing."""
    plan = Plan()
    manifest = release.manifest

    installer_now = sha256_file(layout.installer)
    if installer_now != manifest["installer_sha256"]:
        if "installer" not in release.artifacts:
            raise UpdateError("the release changes the installer but carries no installer artifact",
                              EXIT_INCONSISTENT)
        plan.changes.append(Change("installer", "replace",
                                   f"{(installer_now or 'missing')[:12]} → "
                                   f"{manifest['installer_sha256'][:12]}"))

    plan.installed_skills = collect_skills(layout.skills, layout)
    plan.target_skills = dict(manifest["skills"])
    plan.added = sorted(set(plan.target_skills) - set(plan.installed_skills))
    plan.updated = sorted(name for name in set(plan.target_skills) & set(plan.installed_skills)
                          if plan.target_skills[name] != plan.installed_skills[name])
    plan.extra = sorted(set(plan.installed_skills) - set(plan.target_skills))
    bundle_now = sha256_file(layout.skills / ".bundled_manifest")
    bundle_target = manifest.get("skills_bundle_sha256")
    bundle_changed = bool(bundle_target) and bundle_now != bundle_target
    if plan.skills_changed or bundle_changed:
        if "skills" not in release.artifacts:
            raise UpdateError("the release changes skills but carries no skills artifact",
                              EXIT_INCONSISTENT)
        extract_tar(release.artifacts["skills"], plan.stage() / "skills", "skills artifact")
        staged = collect_skills(plan.stage() / "skills", layout)
        missing = sorted(set(plan.target_skills) - set(staged))
        extra = sorted(set(staged) - set(plan.target_skills))
        if missing or extra:
            raise UpdateError(
                f"the skills artifact does not match the manifest "
                f"(missing {missing[:3]}, extra {extra[:3]})", EXIT_INCONSISTENT)
        wrong = sorted(name for name in staged if staged[name] != plan.target_skills[name])
        if wrong:
            raise UpdateError(f"the skills artifact does not match the manifest for {wrong[:3]}",
                              EXIT_INCONSISTENT)
        bundle = plan.stage() / "skills/.bundled_manifest"
        recorded_bundle = manifest.get("skills_bundle_sha256")
        if bundle.is_file():
            rendered = render_bytes(bundle.read_bytes(), layout)
            if recorded_bundle and sha256(rendered) != recorded_bundle:
                raise UpdateError("the skills bundle manifest does not match the release",
                                  EXIT_INCONSISTENT)
        elif recorded_bundle:
            raise UpdateError("the release records a skills bundle the artifact does not carry",
                              EXIT_INCONSISTENT)
        detail = (f"{len(plan.updated)} changed, {len(plan.added)} added, "
                  f"{len(plan.extra)} left in place")
        if bundle_changed and not plan.skills_changed:
            detail = "bundle manifest only"
        plan.changes.append(Change("skills", "replace", detail))

    head = git_head(layout)
    if head != manifest["hermes"]["commit"]:
        target = manifest["hermes"]["commit"]
        require(git_has_commit(layout, target),
                f"this checkout does not carry Hermes commit {target[:12]}; "
                "fetch it or reinstall at that commit", EXIT_REFUSED)
        clean, dirty = git_is_clean(layout)
        require(clean, f"the Hermes checkout has local changes ({dirty.splitlines()[:3]}); "
                       "commit or discard them first", EXIT_REFUSED)
        recorded_lockfiles = manifest["hermes"].get("lockfiles")
        if isinstance(recorded_lockfiles, dict) and recorded_lockfiles:
            now_lockfiles = {name: sha256_file(layout.install_dir / name) for name in recorded_lockfiles}
            require(now_lockfiles == recorded_lockfiles,
                    f"the release moves the Hermes commit and changes {', '.join(sorted(recorded_lockfiles))}; "
                    "a dependency change is a reinstall, not an in-place update", EXIT_REFUSED)
        recorded_pins = manifest["hermes"].get("python_packages")
        if isinstance(recorded_pins, list) and recorded_pins:
            now_pins = installed_python_pins(layout)
            require(now_pins is not None and sorted(recorded_pins) == now_pins,
                    "the release moves the Hermes commit and changes the Python pins; "
                    "a dependency change is a reinstall, not an in-place update", EXIT_REFUSED)
        plan.changes.append(Change("hermes", "checkout",
                                   f"{(head or 'none')[:12]} → {target[:12]}"))

    build_now = read_webapp_build(layout)
    target_webapp = {key: manifest["hermes"]["webapp"][key]
                     for key in ("node", "source_sha256", "output_sha256")}
    if build_now != target_webapp:
        if "webapp" not in release.artifacts:
            raise UpdateError("the release changes the Webapp bundle but carries no webapp artifact",
                              EXIT_INCONSISTENT)
        extract_tar(release.artifacts["webapp"], plan.stage() / "webapp", "webapp artifact")
        staged_build = _webapp_build_in(plan.stage() / "webapp")
        if staged_build != target_webapp:
            raise UpdateError("the webapp artifact does not match hermes.webapp in the manifest",
                              EXIT_INCONSISTENT)
        plan.changes.append(Change("webapp", "replace",
                                   f"{str((build_now or {}).get('output_sha256'))[:12]} → "
                                   f"{target_webapp['output_sha256'][:12]}"))

    plan.records_differ = (
        read_bytes(layout.etc / "manifest.json") != release.manifest_bytes
        or read_bytes(layout.etc / "manifest.json.sig") != signature_file_bytes(release.manifest_signature)
        or read_bytes(layout.etc / "release.json") != release.document_bytes
        or read_bytes(layout.etc / "release.json.sig") != signature_file_bytes(release.document_signature)
    )
    if plan.records_differ:
        plan.changes.append(Change("records", "write", "manifest.json and release.json"))
    return plan


def _webapp_build_in(directory: Path) -> dict | None:
    data = read_bytes(directory / "hermes-build.json")
    if data is None:
        return None
    try:
        build = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(build, dict):
        return None
    inputs = build.get("inputs") if isinstance(build.get("inputs"), dict) else {}
    return {"node": build.get("node"), "source_sha256": inputs.get("sourceHash"),
            "output_sha256": build.get("outputHash")}


def print_plan(release: Release, plan: Plan, layout: Layout, *, key_id: str) -> None:
    manifest = release.manifest
    print(f"release      {release.release_id} (generated {release.generated_at})")
    if release.notes:
        print(f"notes        {release.notes}")
    print(f"signature    OK (release.json and manifest.json; key {key_id})")
    print(f"boot         kernel {release.boot_kernel} matches this machine")
    backup = manifest.get("backup_sha256")
    print(f"manifest     schema {manifest['schema']} · {len(manifest['skills'])} skills · "
          f"hermes {manifest['hermes']['commit'][:12]} · "
          f"installer {manifest['installer_sha256'][:12]}"
          + (f" · backup {backup[:12]}" if backup else ""))
    drift = verify_backup_drift(release, layout)
    if drift:
        print(f"warning      {drift}", file=sys.stderr)
    if plan.empty():
        print("plan         same: this machine already matches the release")
        return
    for change in plan.changes:
        print(f"plan         {change.name:<9} {change.action:<9} {change.detail}")
    if plan.extra:
        print(f"plan         skills    leave {len(plan.extra)} installed skill(s) not in the release")


# ---------------------------------------------------------------------------
# the snapshot: one rollback step, written before the first change


SNAPSHOT_SCHEMA = 1


def snapshot_path(layout: Layout) -> Path:
    return layout.var / "odile-update/snapshot"


def take_snapshot(layout: Layout, plan: Plan, release: Release) -> Path:
    root = snapshot_path(layout)
    if root.exists():
        shutil.rmtree(root)
    payload = root / "payload"
    payload.mkdir(parents=True)
    record: dict = {
        "schema": SNAPSHOT_SCHEMA,
        "created_at": utc_now(),
        "release": release.release_id,
        "previous_release": (installed_release(layout) or {}).get("release"),
        "hermes_head": git_head(layout),
        "files": {},
        "skills": None,
        "webapp": None,
        "records": {},
    }
    names = {change.name for change in plan.changes}
    for name, path in (("installer", layout.installer),):
        if name not in names:
            continue
        data = read_bytes(path)
        if data is None:
            record["files"][name] = None
            continue
        target = payload / "files" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        record["files"][name] = {"sha256": sha256(data), "mode": mode_of(path),
                                 "uid": owner_of(path)[0], "gid": owner_of(path)[1]}
    if "skills" in names:
        shutil.copytree(layout.skills, payload / "skills", symlinks=True)
        record["skills"] = {"digest": tree_digest(layout.skills), "mode": mode_of(layout.skills, 0o700),
                            "uid": owner_of(layout.skills)[0], "gid": owner_of(layout.skills)[1]}
    if "webapp" in names:
        shutil.copytree(layout.webapp, payload / "webapp", symlinks=True)
        record["webapp"] = {"digest": tree_digest(layout.webapp),
                            "mode": mode_of(layout.webapp, 0o755),
                            "uid": owner_of(layout.webapp)[0], "gid": owner_of(layout.webapp)[1]}
    for name in ("manifest.json", "manifest.json.sig", "release.json", "release.json.sig"):
        data = read_bytes(layout.etc / name)
        if data is None:
            record["records"][name] = None
            continue
        target = payload / "records" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        record["records"][name] = {"sha256": sha256(data), "mode": mode_of(layout.etc / name),
                                   "uid": owner_of(layout.etc / name)[0],
                                   "gid": owner_of(layout.etc / name)[1]}
    write_atomic(root / "snapshot.json",
                 (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8"), 0o644)
    print(f"snapshot     {root} (release {record['previous_release'] or 'none'}, "
          f"hermes {str(record['hermes_head'])[:12]})")
    return root


def _restore_owner(path: Path, uid: int, gid: int) -> None:
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass


def restore_snapshot(layout: Layout, root: Path, *, restart: bool = True) -> bool:
    """Restore the tree to the snapshot. Returns True when anything moved.

    When anything moved, the units whose bytes moved are restarted once each:
    ``odile-agent`` for the Hermes checkout or the Webapp bundle. A rollback
    that leaves the agent running the rolled-back-away release is not a
    rollback. The installer is a file the next install reads, not a running
    process, so restoring it restarts nothing.
    """
    record_path = root / "snapshot.json"
    if not record_path.is_file():
        raise UpdateError(f"no snapshot at {root}", EXIT_NO_SNAPSHOT)
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdateError(f"cannot read {record_path}: {exc}", EXIT_BAD_INPUT) from exc
    payload = root / "payload"
    changed = False

    restored_agent = False
    for name, path in (("installer", layout.installer),):
        entry = record["files"].get(name)
        if name not in record["files"]:
            continue
        if entry is None:
            if path.exists():
                path.unlink()
                changed = True
            continue
        data = (payload / "files" / name).read_bytes()
        if read_bytes(path) != data or mode_of(path) != entry["mode"]:
            write_atomic(path, data, entry["mode"], entry["uid"], entry["gid"])
            changed = True

    if record["skills"] is not None:
        data = payload / "skills"
        if tree_digest(layout.skills) != record["skills"]["digest"]:
            shutil.rmtree(layout.skills, ignore_errors=True)
            shutil.copytree(data, layout.skills, symlinks=True)
            os.chmod(layout.skills, record["skills"]["mode"])
            for item in [layout.skills, *layout.skills.rglob("*")]:
                _restore_owner(item, record["skills"]["uid"], record["skills"]["gid"])
            changed = True

    if record["webapp"] is not None:
        data = payload / "webapp"
        if tree_digest(layout.webapp) != record["webapp"]["digest"]:
            shutil.rmtree(layout.webapp, ignore_errors=True)
            shutil.copytree(data, layout.webapp, symlinks=True)
            os.chmod(layout.webapp, record["webapp"]["mode"])
            for item in [layout.webapp, *layout.webapp.rglob("*")]:
                _restore_owner(item, record["webapp"]["uid"], record["webapp"]["gid"])
            changed = True
            restored_agent = True

    head = record.get("hermes_head")
    if head and COMMIT_RE.match(head) and git_head(layout) != head:
        proc = git_run(layout, "checkout", "--detach", head)
        if proc.returncode != 0:
            raise UpdateError(f"cannot check out {head[:12]}: {proc.stderr.strip()[:300]}",
                              EXIT_VERIFY_FAILED)
        changed = True
        restored_agent = True

    for name, entry in record["records"].items():
        path = layout.etc / name
        if entry is None:
            if path.exists():
                path.unlink()
                changed = True
            continue
        data = (payload / "records" / name).read_bytes()
        if read_bytes(path) != data:
            write_atomic(path, data, entry["mode"], entry["uid"], entry["gid"])
            changed = True

    if changed and restart:
        units: list[str] = []
        for moved, unit in ((restored_agent, "odile-agent"),):
            if moved and unit not in units:
                units.append(unit)
        if units:
            restart_units(layout, units)
    return changed


# ---------------------------------------------------------------------------
# applying


def restart_units(layout: Layout, units: list[str]) -> None:
    if layout.mode != "tenant":
        print(f"scratch      no systemd units started; restart the simulation by hand if it is running")
        return
    for unit in units:
        proc = run(["systemctl", "restart", unit], timeout=120)
        if proc.returncode != 0:
            raise UpdateError(f"systemctl restart {unit} failed: {proc.stderr.strip()[:300]}",
                              EXIT_VERIFY_FAILED)
        print(f"restart      {unit}")


def apply_change(change: Change, release: Release, layout: Layout, plan: Plan) -> None:
    if change.name == "installer":
        uid, gid = (owner_of(layout.installer) if layout.installer.exists()
                    else owner_of(layout.installer.parent))
        write_atomic(layout.installer, release.artifacts["installer"],
                     mode_of(layout.installer, 0o755), uid, gid)
        print(f"write        {layout.installer}")
    elif change.name == "skills":
        install_skills(release, layout, plan)
    elif change.name == "hermes":
        target = release.manifest["hermes"]["commit"]
        proc = git_run(layout, "checkout", "--detach", target)
        if proc.returncode != 0:
            raise UpdateError(f"git checkout {target[:12]} failed: {proc.stderr.strip()[:300]}",
                              EXIT_VERIFY_FAILED)
        print(f"checkout     Hermes {target[:12]}")
    elif change.name == "webapp":
        install_webapp(release, layout, plan)
    elif change.name == "records":
        write_records(release, layout)
    else:
        raise UpdateError(f"unknown change {change.name!r}", EXIT_BAD_INPUT)


def install_skills(release: Release, layout: Layout, plan: Plan) -> None:
    assert plan.staging is not None
    staged = plan.staging / "skills"
    rendered = plan.staging / "rendered"
    rendered.mkdir(parents=True, exist_ok=True)
    uid, gid = owner_of(layout.skills)
    for name in plan.updated + plan.added:
        source = staged / name
        target = rendered / name
        _render_tree(source, target, layout)
        if hash_tree(target, layout) != release.manifest["skills"][name]:
            raise UpdateError(f"skill {name} does not verify after rendering", EXIT_VERIFY_FAILED)
        live = layout.skills / name
        if live.exists():
            shutil.rmtree(live)
        shutil.copytree(target, live, symlinks=True)
        for item in [live, *live.rglob("*")]:
            _restore_owner(item, uid, gid)
        print(f"write        {live}")
    bundle = staged / ".bundled_manifest"
    if bundle.is_file():
        data = render_bytes(bundle.read_bytes(), layout)
        target_digest = release.manifest.get("skills_bundle_sha256")
        if target_digest and sha256(data) != target_digest:
            raise UpdateError("the skills bundle manifest does not match the release",
                              EXIT_VERIFY_FAILED)
        write_atomic(layout.skills / ".bundled_manifest", data, 0o644, uid, gid)
        print(f"write        {layout.skills / '.bundled_manifest'}")


def _render_tree(source: Path, target: Path, layout: Layout) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_symlink():
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(render_bytes(os.readlink(path).encode(), layout).decode(), destination)
        elif path.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(render_bytes(path.read_bytes(), layout))
            os.chmod(destination, path.stat().st_mode & 0o777)
        elif path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)


def install_webapp(release: Release, layout: Layout, plan: Plan) -> None:
    assert plan.staging is not None
    staged = plan.staging / "webapp"
    incoming = layout.webapp.with_name(f".{layout.webapp.name}.incoming")
    if incoming.exists():
        shutil.rmtree(incoming)
    shutil.copytree(staged, incoming, symlinks=True)
    if _webapp_build_in(incoming) != {key: release.manifest["hermes"]["webapp"][key]
                                       for key in ("node", "source_sha256", "output_sha256")}:
        shutil.rmtree(incoming, ignore_errors=True)
        raise UpdateError("the Webapp bundle does not match the manifest after extraction",
                          EXIT_VERIFY_FAILED)
    uid, gid = owner_of(layout.webapp)
    for item in [incoming, *incoming.rglob("*")]:
        _restore_owner(item, uid, gid)
    previous = layout.webapp.with_name(f".{layout.webapp.name}.previous")
    if previous.exists():
        shutil.rmtree(previous)
    if layout.webapp.exists():
        os.replace(layout.webapp, previous)
    os.replace(incoming, layout.webapp)
    shutil.rmtree(previous, ignore_errors=True)
    print(f"write        {layout.webapp}")


def write_records(release: Release, layout: Layout) -> None:
    uid, gid = owner_of(layout.etc)
    layout.etc.mkdir(parents=True, exist_ok=True)
    write_atomic(layout.etc / "manifest.json", release.manifest_bytes, 0o644, uid, gid)
    write_atomic(layout.etc / "manifest.json.sig", signature_file_bytes(release.manifest_signature),
                 0o644, uid, gid)
    write_atomic(layout.etc / "release.json", release.document_bytes, 0o644, uid, gid)
    write_atomic(layout.etc / "release.json.sig", signature_file_bytes(release.document_signature),
                 0o644, uid, gid)
    print(f"record       {layout.etc}/manifest.json and release.json")


def verify_installed(release: Release, layout: Layout) -> list[str]:
    """Every app-layer fact the release names, checked against the tree."""
    problems: list[str] = []
    manifest = release.manifest
    if sha256_file(layout.installer) != manifest["installer_sha256"]:
        problems.append("installer_sha256")
    installed = collect_skills(layout.skills, layout)
    wrong = sorted(name for name, digest in manifest["skills"].items()
                   if installed.get(name) != digest)
    if wrong:
        problems.append(f"skills ({', '.join(wrong[:3])})")
    recorded_bundle = manifest.get("skills_bundle_sha256")
    if recorded_bundle and sha256_file(layout.skills / ".bundled_manifest") != recorded_bundle:
        problems.append("skills bundle manifest")
    if git_head(layout) != manifest["hermes"]["commit"]:
        problems.append("hermes commit")
    if read_webapp_build(layout) != {key: manifest["hermes"]["webapp"][key]
                                     for key in ("node", "source_sha256", "output_sha256")}:
        problems.append("webapp bundle")
    if read_bytes(layout.etc / "manifest.json") != release.manifest_bytes:
        problems.append("recorded manifest")
    if read_bytes(layout.etc / "release.json") != release.document_bytes:
        problems.append("recorded release")
    return problems


def installer_backup_pin(installer: Path) -> str | None:
    """The backup program the installer itself pins, or None if it cannot say.

    The installer's own `--manifest` is the existing spelling of this record
    (`scripts/manifest.py read_installer_manifest` reads the same one). It is
    the bytes that installer writes when it runs, so it is the program a fresh
    install from a bundle would carry. A fixture or a pre-pin installer that
    prints nothing usable returns None: the caller says the row was not
    cross-checked rather than inventing a pin.
    """
    try:
        proc = run([sys.executable, str(installer), "--manifest"], timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        document = json.loads(proc.stdout)
    except ValueError:
        return None
    pin = document.get("backup_sha256") if isinstance(document, dict) else None
    return pin if isinstance(pin, str) and bool(HEX_RE.match(pin)) else None


def verify_backup_drift(release: Release, layout: Layout) -> str | None:
    """One line when this machine's program is not the release's row, else None.

    A warning, not a refusal: `backup_sha256` is a fact about the release, and
    the file is a fact about the machine. `apply` writes the installer, the
    skills and the Webapp — never the backup program — so refusing would roll
    back an otherwise correct update and could never converge; the drift has to
    be repaired by running the installer. The machine's bytes are still the ones
    the nightly unit executes, so they are named rather than left implied.
    """
    row = release.manifest.get("backup_sha256")
    if not row:
        return None
    actual = sha256_file(layout.backup_program)
    if actual is None:
        return (f"no {layout.backup_program}; the manifest names backup {row[:12]} "
                "and the nightly unit has no program to run")
    if actual == row:
        return None
    return (f"backup {actual[:12]} on this machine is not the manifest's {row[:12]}: "
            "the program drifted from the release, which does not rewrite it")


def append_log(layout: Layout, line: str) -> None:
    try:
        layout.var.mkdir(parents=True, exist_ok=True)
        with open(layout.var / "odile-update.log", "a", encoding="utf-8") as handle:
            handle.write(f"{utc_now()} {line}\n")
    except OSError as exc:
        print(f"warning: cannot write {layout.var}/odile-update.log: {exc}", file=sys.stderr)


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        raise UpdateError("stdin is not a terminal; pass --yes", EXIT_BAD_INPUT)
    return input(question).strip().lower() in ("y", "yes")


def check_ordering(release: Release, layout: Layout, allow_downgrade: bool) -> None:
    current = installed_release(layout)
    if not current:
        return
    current_stamp = current.get("generated_at")
    if not isinstance(current_stamp, str) or not ISO_RE.match(current_stamp):
        return
    if release.generated_at < current_stamp and not allow_downgrade:
        raise UpdateError(
            f"release {release.release_id} ({release.generated_at}) is older than the installed "
            f"{current.get('release')} ({current_stamp}); pass --allow-downgrade to apply", EXIT_REFUSED)


def apply_release(release: Release, layout: Layout, *, assume_yes: bool,
                  allow_downgrade: bool, restart: bool = True, key_id: str = "") -> int:
    check_ordering(release, layout, allow_downgrade)
    plan = plan_changes(release, layout)
    try:
        print_plan(release, plan, layout, key_id=key_id)
        if plan.empty():
            print(f"already at release {release.release_id}")
            append_log(layout, f"release {release.release_id} apply same")
            return EXIT_OK
        if not assume_yes:
            sys.stdout.flush()
            if not confirm(f"Apply release {release.release_id}? [y/N] "):
                print("cancelled")
                return EXIT_OK
        snapshot = take_snapshot(layout, plan, release)
        try:
            for change in plan.changes:
                apply_change(change, release, layout, plan)
            problems = verify_installed(release, layout)
            if problems:
                raise UpdateError("post-update check failed: " + ", ".join(problems),
                                  EXIT_VERIFY_FAILED)
            restart_after: list[str] = []
            for name, unit in (("hermes", "odile-agent"), ("webapp", "odile-agent")):
                if name in {change.name for change in plan.changes} and unit not in restart_after:
                    restart_after.append(unit)
            restart_units(layout, restart_after)
        except BaseException as exc:
            print(f"error: {exc}", file=sys.stderr)
            print(f"rollback     restoring {snapshot}", file=sys.stderr)
            try:
                restore_snapshot(layout, snapshot, restart=restart)
            except Exception as rollback_error:  # noqa: BLE001 - report both failures
                raise UpdateError(f"{exc}; rollback also failed: {rollback_error}",
                                  EXIT_VERIFY_FAILED) from exc
            append_log(layout, f"release {release.release_id} apply failed, rolled back: {exc}")
            if isinstance(exc, UpdateError):
                raise UpdateError(f"{exc} (rolled back)", EXIT_VERIFY_FAILED) from exc
            raise UpdateError(f"{exc} (rolled back)", EXIT_VERIFY_FAILED) from exc
        append_log(layout, f"release {release.release_id} apply ok "
                           f"({', '.join(change.name for change in plan.changes)})")
        print(f"applied      release {release.release_id}")
        print("rollback     odile-update rollback --yes")
        return EXIT_OK
    finally:
        plan.cleanup()


# ---------------------------------------------------------------------------
# cutting a release (HQ only)


def canonical_tar(entries: list[tuple[str, Path]], *, render: Callable[[bytes], bytes]) -> bytes:
    """A deterministic tar.gz: sorted, mtime 0, uid/gid 0, canonical bytes.

    Two cuts of the same tree produce the same sha256, so a release hash is a
    statement about content, not about when it was packed.
    """
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for relative, path in sorted(entries, key=lambda item: item[0]):
                if path.is_symlink():
                    info = tarfile.TarInfo(relative)
                    info.type = tarfile.SYMTYPE
                    info.linkname = render(os.readlink(path).encode()).decode()
                    info.mode = 0o777
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    archive.addfile(info)
                    continue
                data = render(path.read_bytes())
                info = tarfile.TarInfo(relative)
                info.size = len(data)
                info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def skill_entries(skills_root: Path) -> list[tuple[str, Path]]:
    entries: list[tuple[str, Path]] = []
    for skill_md in sorted(skills_root.rglob("SKILL.md")):
        directory = skill_md.parent
        for path in sorted(directory.rglob("*")):
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.is_file() or path.is_symlink():
                entries.append((path.relative_to(skills_root).as_posix(), path))
    bundle = skills_root / ".bundled_manifest"
    if bundle.is_file():
        entries.append((".bundled_manifest", bundle))
    return entries


def installer_pair_paths(layout: Layout) -> tuple[Path, Path]:
    """The cut's source for the installer's signature and the published key.

    The signature must be the one made over the installer bytes this cut
    exports. A scratch root exports ``bootstrap/odile_install.py``, whose signed
    pair is committed in the checkout's ``installer/``; a tenant root exports
    the installed installer, with its signature beside it. The published key is
    the committed one either way: it is the identity the recovery kit carries.
    """
    checkout = Path(__file__).resolve().parent.parent
    published = checkout / "installer/odile-signing-key.pub"
    if layout.mode == "tenant":
        return Path(str(layout.installer) + SIG_SUFFIX), published
    return checkout / "installer/odile_install.py.sig", published


def build_release(layout: Layout, manifest_path: Path, *, release_id: str, boot_kernel: str,
                  out_dir: Path, generated_at: str | None = None, key: Path | None = None,
                  with_webapp: bool = False, notes: str | None = None) -> dict:
    manifest_bytes = Path(manifest_path).read_bytes()
    manifest = parse_json(manifest_bytes, str(manifest_path))
    validate_manifest(manifest)
    require(manifest.get("mode") == layout.mode,
            f"the manifest was built for mode {manifest.get('mode')!r}, this root is {layout.mode!r}",
            EXIT_BAD_INPUT)
    require(manifest["hermes"]["commit"] == git_head(layout),
            "the manifest's Hermes commit is not this checkout's HEAD", EXIT_BAD_INPUT)
    require(manifest["installer_sha256"] == sha256_file(layout.installer),
            "the manifest's installer_sha256 is not this root's installer", EXIT_BAD_INPUT)
    if manifest.get("backup_sha256") and layout.backup_program.is_file():
        require(manifest["backup_sha256"] == sha256_file(layout.backup_program),
                "the manifest's backup_sha256 is not this root's tenant backup program",
                EXIT_BAD_INPUT)
    # The row has to agree with the installer this bundle ships, not only with
    # the candidate root. An installer-only update replaces the installer and
    # never runs it, so a candidate root can carry the old program while its
    # installer pins the new one; the bundle would then name one program and
    # ship an installer that writes another. `tools/recut_release.py --check`
    # already fails that pair on the surgical path; a full cut refuses it here.
    row = manifest.get("backup_sha256")
    pin = installer_backup_pin(layout.installer)
    if row and pin:
        require(row == pin,
                f"the manifest's backup_sha256 {row[:12]} is not the program the installer "
                f"it ships pins ({pin[:12]}): the root's installed program is the old bytes, "
                f"the installer writes the new ones — run {layout.installer} on this root, "
                "or re-cut the row from the installer", EXIT_BAD_INPUT)
    elif row:
        print(f"warning: {layout.installer} does not answer --manifest with a backup_sha256; "
              f"the manifest's row {row[:12]} is not cross-checked against the installer "
              "this bundle ships", file=sys.stderr)
    else:
        print("warning: the manifest names no backup_sha256; this release does not pin the "
              "program the nightly unit runs (rebuild it from this root to carry the row)",
              file=sys.stderr)
    target_webapp = {key_name: manifest["hermes"]["webapp"][key_name]
                     for key_name in ("node", "source_sha256", "output_sha256")}
    require(target_webapp == read_webapp_build(layout),
            "the manifest's hermes.webapp is not this root's bundle", EXIT_BAD_INPUT)
    skills = collect_skills(layout.skills, layout)
    require(skills == manifest["skills"],
            "the manifest's skills do not match this root", EXIT_BAD_INPUT)
    bundle = layout.skills / ".bundled_manifest"
    if manifest.get("skills_bundle_sha256"):
        require(sha256_file(bundle) == manifest["skills_bundle_sha256"],
                "the manifest's skills_bundle_sha256 is not this root's bundle manifest",
                EXIT_BAD_INPUT)
    else:
        require(not bundle.is_file(), "the root has a .bundled_manifest the manifest does not record",
                EXIT_BAD_INPUT)

    # The pair that makes the download verifiable on its own. It is checked
    # before the first write: a cut with a stale signature would otherwise ship
    # a bundle that fails at the user's first command. The manifest check above
    # already ties these installer bytes to installer_sha256.
    installer_bytes = layout.installer.read_bytes()
    signature_path, pubkey_path = installer_pair_paths(layout)
    signature_raw = read_bytes(signature_path)
    if signature_raw is None:
        raise UpdateError(f"no installer signature at {signature_path}; the bundle would not verify",
                          EXIT_BAD_INPUT)
    if not pubkey_path.is_file():
        raise UpdateError(f"no published key at {pubkey_path}; the bundle would not verify",
                          EXIT_BAD_INPUT)
    installer_signature = signature_bytes(signature_raw, str(signature_path))
    require(verify_bytes(installer_bytes, installer_signature, pubkey_path),
            f"{signature_path} does not verify {layout.installer}; "
            "re-sign the installer before cutting", EXIT_BAD_SIGNATURE)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    released_at = generated_at or utc_now()
    require(bool(ISO_RE.match(released_at)), "--generated-at is not an ISO-8601 UTC stamp",
            EXIT_BAD_INPUT)

    artifacts: dict[str, dict] = {}

    def add(name: str, filename: str, data: bytes) -> None:
        (out / filename).write_bytes(data)
        artifacts[name] = {"url": filename, "sha256": sha256(data), "size": len(data)}
        print(f"artifact     {name:<9} {filename} ({len(data)} bytes, sha256 {sha256(data)[:12]}…)")

    installer_name = layout.installer.name if layout.mode == "tenant" else "odile_install.py"
    add("installer", installer_name, layout.installer.read_bytes())
    skills_tar = canonical_tar(skill_entries(layout.skills),
                                render=lambda data: canonical_bytes(data, layout))
    add("skills", "skills.tar.gz", skills_tar)
    if with_webapp:
        webapp_tar = canonical_tar(
            [(path.relative_to(layout.webapp).as_posix(), path)
             for path in sorted(layout.webapp.rglob("*"))
             if path.is_file() or path.is_symlink()],
            render=lambda data: data)
        add("webapp", "webapp.tar.gz", webapp_tar)

    for source, filename in ((signature_path, installer_name + SIG_SUFFIX),
                             (pubkey_path, "odile-signing-key.pub")):
        data = source.read_bytes()
        (out / filename).write_bytes(data)
        print(f"copy         {out / filename} ({source} → {len(data)} bytes, "
              f"sha256 {sha256(data)[:12]}…)")

    (out / "manifest.json").write_bytes(manifest_bytes)
    document = {
        "schema": SCHEMA,
        "product": "odile",
        "release": release_id,
        "generated_at": released_at,
        "boot": {"kernel": boot_kernel},
        "manifest": {"url": "manifest.json", "sha256": sha256(manifest_bytes),
                     "signature_url": "manifest.json" + SIG_SUFFIX},
        "artifacts": artifacts,
    }
    if notes:
        document["notes"] = notes
    validate_release_document(document)
    document_bytes = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (out / "release.json").write_bytes(document_bytes)
    print(f"release      {out / 'release.json'} (release {release_id}, "
          f"manifest sha256 {sha256(manifest_bytes)[:12]}…)")
    if key is not None:
        for name in ("manifest.json", "release.json"):
            data = (out / name).read_bytes()
            signature = sign_bytes(data, key)
            (out / (name + SIG_SUFFIX)).write_bytes(signature_file_bytes(signature))
            print(f"sign         {out / (name + SIG_SUFFIX)} (key {key_fingerprint(key, private=True)})")
    else:
        print(f"sign         manifest.py sign --manifest {out / 'manifest.json'} --key <key>")
        print(f"sign         manifest.py sign --manifest {out / 'release.json'} --key <key>")
    return document


# ---------------------------------------------------------------------------
# the command line


def require_root(what: str) -> None:
    if os.geteuid() != 0:
        raise UpdateError(f"{what} writes the app layer; run it as root (sudo odile-update {what})",
                          EXIT_BAD_INPUT)


def add_common(parser: argparse.ArgumentParser, *, network: bool = True) -> None:
    parser.add_argument("--root", default="/", help="the installed root (default /; a scratch root for the simulation)")
    parser.add_argument("--user", help="the tenant user (tenant default: odile; a scratch root needs one home)")
    if not network:
        return
    parser.add_argument("--release-url", default=DEFAULT_RELEASE_URL,
                        help=f"the signed release document (default {DEFAULT_RELEASE_URL})")
    parser.add_argument("--pubkey", help="the Ed25519 public key (default <root>/etc/odile/odile-signing-key.pub)")
    parser.add_argument("--kernel", help="the kernel release to compare against (default: this machine's)")


def pubkey_path(args: argparse.Namespace, layout: Layout) -> Path:
    return Path(args.pubkey) if args.pubkey else layout.etc / "odile-signing-key.pub"


def cmd_check(args: argparse.Namespace) -> int:
    require_root("check")
    layout = resolve_layout(args.root, args.user)
    key = pubkey_path(args, layout)
    release = load_release(args.release_url, key, kernel=args.kernel)
    check_ordering(release, layout, allow_downgrade=False)
    plan = plan_changes(release, layout)
    try:
        print_plan(release, plan, layout, key_id=key_fingerprint(key, private=False))
        current = installed_release(layout)
        if plan.empty():
            print(f"current      already at release {release.release_id}")
            return EXIT_ALREADY_CURRENT
        print(f"available    {(current or {}).get('release') or 'unrecorded'} → {release.release_id}")
        return EXIT_OK
    finally:
        plan.cleanup()


def dry_run(release: Release, layout: Layout, *, allow_downgrade: bool, key_id: str) -> int:
    """Print the plan a release would apply; write nothing.

    It is the same first step as ``apply``: the ordering check runs before the
    plan, so a dry run tells the same truth about an older release.
    """
    check_ordering(release, layout, allow_downgrade=allow_downgrade)
    plan = plan_changes(release, layout)
    try:
        print_plan(release, plan, layout, key_id=key_id)
        print("dry run      nothing written")
        return EXIT_OK
    finally:
        plan.cleanup()


def cmd_apply(args: argparse.Namespace) -> int:
    require_root("apply")
    layout = resolve_layout(args.root, args.user)
    key = pubkey_path(args, layout)
    release = load_release(args.release_url, key, kernel=args.kernel)
    if args.dry_run:
        return dry_run(release, layout, allow_downgrade=args.allow_downgrade,
                       key_id=key_fingerprint(key, private=False))
    return apply_release(release, layout, assume_yes=args.yes,
                         allow_downgrade=args.allow_downgrade,
                         key_id=key_fingerprint(key, private=False))


def cmd_rollback(args: argparse.Namespace) -> int:
    require_root("rollback")
    layout = resolve_layout(args.root, args.user)
    root = snapshot_path(layout) if not args.snapshot else Path(args.snapshot)
    if not (root / "snapshot.json").is_file():
        raise UpdateError(f"no snapshot at {root}; nothing to roll back to", EXIT_NO_SNAPSHOT)
    if not args.yes and not confirm(f"Roll back to {root}? [y/N] "):
        print("cancelled")
        return EXIT_OK
    changed = restore_snapshot(layout, root)
    if changed:
        append_log(layout, f"release rollback ok ({root})")
        print(f"rolled back  {root}")
    else:
        print(f"same         the tree already matches {root}")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    require_root("status")
    layout = resolve_layout(args.root, args.user)
    key = pubkey_path(args, layout)
    current = installed_release(layout)
    manifest = installed_manifest(layout)
    if current:
        print(f"release      {current.get('release')} (generated {current.get('generated_at')})")
        if current.get("notes"):
            print(f"notes        {current['notes']}")
    else:
        print("release      none recorded (this machine has not been updated by odile-update)")
    if manifest:
        hermes = manifest.get("hermes") if isinstance(manifest.get("hermes"), dict) else {}
        skills = manifest.get("skills") if isinstance(manifest.get("skills"), dict) else {}
        print(f"manifest     {len(skills)} skills · hermes {str(hermes.get('commit'))[:12]} · "
              f"installer {str(manifest.get('installer_sha256'))[:12]}")
    print(f"machine      {layout.mode} root {layout.root} · user {layout.user}")
    print(f"kernel       {os.uname().release}")
    if key.is_file():
        print(f"key          {key} ({key_fingerprint(key, private=False)})")
    else:
        print(f"key          missing: {key}")
    root = snapshot_path(layout)
    if (root / "snapshot.json").is_file():
        try:
            record = json.loads((root / "snapshot.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {}
        print(f"snapshot     {root} (release {record.get('previous_release') or 'none'}, "
              f"created {record.get('created_at')})")
    else:
        print("snapshot     none")
    return EXIT_OK


def cmd_release(args: argparse.Namespace) -> int:
    layout = resolve_layout(args.root, args.user)
    require(args.release, "--release is required", EXIT_BAD_INPUT)
    require(bool(RELEASE_RE.match(args.release)), "--release is not a release id", EXIT_BAD_INPUT)
    require(args.boot_kernel, "--boot-kernel is required: a release names the boot chain it was built for",
            EXIT_BAD_INPUT)
    build_release(layout, Path(args.manifest), release_id=args.release,
                  boot_kernel=args.boot_kernel, out_dir=Path(args.out_dir),
                  generated_at=args.generated_at, key=Path(args.key) if args.key else None,
                  with_webapp=args.with_webapp, notes=args.notes)
    return EXIT_OK


def cmd_self_test(args: argparse.Namespace) -> int:
    return self_test()


# ---------------------------------------------------------------------------
# self-test: the pure functions and one whole release, hermetically
#
# This is the installer's precedent (`--self-test`): the module is a script,
# not a package, and the tests that matter most are the ones that can build a
# machine, a release and a rollback in one temporary directory and compare
# bytes. The runbook records the transcript; `make check` compiles the file.


class Check:
    def __init__(self) -> None:
        self.passed = 0
        self.failures = 0

    def ok(self, label: str) -> None:
        self.passed += 1
        print(f"ok   {label}")

    def bad(self, label: str, detail: str = "") -> None:
        self.failures += 1
        print(f"FAIL {label}{': ' + detail if detail else ''}")

    def expect(self, label: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.ok(label)
        else:
            self.bad(label, detail)

    def raises(self, label: str, code: int, function, *args, **kwargs) -> None:
        try:
            function(*args, **kwargs)
        except UpdateError as exc:
            if exc.code == code:
                self.ok(f"{label}: {exc}")
            else:
                self.bad(label, f"expected exit {code}, got {exc.code}: {exc}")
        except Exception as exc:  # noqa: BLE001 - a self-test reports what happened
            self.bad(label, f"expected UpdateError {code}, got {type(exc).__name__}: {exc}")
        else:
            self.bad(label, "no error")

    def report(self) -> int:
        total = self.passed + self.failures
        print(f"self-test: {self.passed}/{total} passed" if self.failures == 0
              else f"self-test: {self.failures} of {total} FAILED")
        return 0 if self.failures == 0 else 1


def _self_test_root(base: Path) -> Path:
    root = base / "machine"
    home = root / "home/tester"
    skills = home / ".hermes/skills"
    (skills / "odile-home").mkdir(parents=True)
    (skills / "alpha").mkdir(parents=True)
    (skills / "odile-home/SKILL.md").write_text(
        "# Odile home\n\n- You run as `tester` and have NOPASSWD sudo.\n"
        "- Inference goes straight to NEAR AI.\n", encoding="utf-8")
    (skills / "alpha/SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (skills / "alpha/run.sh").write_text("#!/bin/sh\necho alpha\n", encoding="utf-8")
    os.chmod(skills / "alpha/run.sh", 0o755)
    (skills / ".bundled_manifest").write_text("alpha:aaaaaaaaaaaa\n", encoding="utf-8")
    (root / "usr/local/lib/odile").mkdir(parents=True)
    (root / "usr/local/lib/odile/backup.py").write_text(
        "#!/usr/bin/python3\n# the fixture tenant backup program\n", encoding="utf-8")
    (root / "bootstrap").mkdir(parents=True)
    (root / "bootstrap/odile_install.py").write_text("#!/usr/bin/python3\n# installer\n", encoding="utf-8")
    (root / "etc/systemd/system").mkdir(parents=True)
    checkout = home / ".hermes/hermes-agent"
    (checkout / "apps/desktop/dist-webapp").mkdir(parents=True)
    (checkout / "uv.lock").write_text("lock\n", encoding="utf-8")
    (checkout / "package-lock.json").write_text('{"packages": {}}\n', encoding="utf-8")
    (checkout / "apps/desktop/dist-webapp/hermes-build.json").write_text(json.dumps({
        "node": "26.12.1", "inputs": {"sourceHash": "a" * 64}, "outputHash": "b" * 64,
    }), encoding="utf-8")
    env = git_env()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True, env=env)
    subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(checkout), "-c", "user.email=t@example.invalid",
                    "-c", "user.name=test", "commit", "-q", "-m", "fixture"], check=True, env=env)
    (root / "etc/odile").mkdir(parents=True)
    return root


def _self_test_manifest(layout: Layout, *, generated_at: str) -> dict:
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "mode": layout.mode,
        "skills": collect_skills(layout.skills, layout),
        "skills_bundle_sha256": sha256_file(layout.skills / ".bundled_manifest"),
        "installer_sha256": sha256_file(layout.installer),
        "backup_sha256": sha256_file(layout.backup_program),
        "hermes": {"commit": git_head(layout), "webapp": read_webapp_build(layout)},
    }


def _self_test_sign(document: dict, key: Path) -> tuple[bytes, bytes]:
    data = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return data, signature_file_bytes(sign_bytes(data, key))


def self_test() -> int:
    check = Check()
    with tempfile.TemporaryDirectory(prefix="odile-update-self-test-") as tmp_text:
        tmp = Path(tmp_text)
        key = tmp / "key.pem"
        pub = tmp / "key.pub"
        subprocess.run([openssl(), "genpkey", "-algorithm", "ED25519", "-out", str(key)],
                       check=True, capture_output=True)
        os.chmod(key, 0o600)
        subprocess.run([openssl(), "pkey", "-in", str(key), "-pubout", "-out", str(pub)],
                       check=True, capture_output=True)

        machine = _self_test_root(tmp)
        layout = resolve_layout(machine, "tester")
        # The self-test's fixture installer is not the committed one, so the
        # committed pair cannot verify it. Mint a pair with the temporary key
        # for this run, exactly as an operator would after re-signing.
        fixture_pair = (tmp / "odile_install.py.sig", pub)
        fixture_pair[0].write_bytes(signature_file_bytes(
            sign_bytes((machine / "bootstrap/odile_install.py").read_bytes(), key)))
        check.expect("layout: scratch root, one home", layout.mode == "scratch" and layout.user == "tester")

        # The tenant backup program's row (ODL-215 follow-up): the release
        # manifest is its carrier, so the guest must accept an older manifest
        # without it and refuse a value that is not a digest.
        manifest_row = _self_test_manifest(layout, generated_at="2026-11-08T15:00:00Z")
        check.expect("backup row: the root's program is the hash the manifest records",
                     manifest_row["backup_sha256"] == sha256_file(layout.backup_program))
        older = dict(manifest_row)
        older.pop("backup_sha256")
        try:
            validate_manifest(older)
            older_accepted = True
        except UpdateError:
            older_accepted = False
        check.expect("backup row: a manifest without it still validates (older release)",
                     older_accepted)
        try:
            validate_manifest(dict(manifest_row, backup_sha256="not-a-sha256"))
            malformed_refused = False
        except UpdateError:
            malformed_refused = True
        check.expect("backup row: a malformed row is refused", malformed_refused)

        # canonical form is an exact round trip, and matches manifest.py's
        skill_file = layout.skills / "odile-home/SKILL.md"
        original = skill_file.read_bytes()
        canonical = canonical_bytes(original, layout)
        check.expect("canonical: user replaced",
                     b"`<user>`" in canonical and b"tester" not in canonical)
        sample = (f"home={layout.home}\nroot={layout.root}\n`{layout.user}`\n").encode()
        canonical_sample = canonical_bytes(sample, layout)
        check.expect("canonical: home, root and user replaced",
                     b"<home>" in canonical_sample and b"<root>" in canonical_sample
                     and b"`<user>`" in canonical_sample
                     and str(layout.home).encode() not in canonical_sample
                     and str(layout.root).encode() not in canonical_sample)
        check.expect("canonical: render is the inverse",
                     render_bytes(canonical_sample, layout) == sample
                     and canonical_bytes(render_bytes(canonical, layout), layout) == canonical)
        check.expect("canonical: hash_tree is stable across the round trip",
                     hash_tree(layout.skills / "odile-home", layout)
                     == hash_tree_dir_from_bytes(tmp, canonical, layout))

        manifest_script = Path(__file__).resolve().parent.parent / "scripts/manifest.py"
        if manifest_script.is_file():
            import importlib.util
            import sys as _sys
            spec = importlib.util.spec_from_file_location("odile_manifest_ref", manifest_script)
            reference = importlib.util.module_from_spec(spec)
            _sys.modules["odile_manifest_ref"] = reference
            assert spec.loader is not None
            spec.loader.exec_module(reference)
            check.expect("cross-check: scripts/manifest.py canonicalises the same bytes",
                         reference.canonical_skill_bytes(original, root=layout.root, home=layout.home,
                                                         user=layout.user) == canonical)
            check.expect("cross-check: scripts/manifest.py hashes the same skill",
                         reference.hash_tree(layout.skills / "odile-home",
                                             canonicalize=lambda _rel, data: reference.canonical_skill_bytes(
                                                 data, root=layout.root, home=layout.home, user=layout.user))
                         == hash_tree(layout.skills / "odile-home", layout))
        else:
            check.bad("cross-check: scripts/manifest.py is missing")

        # a candidate root: the same machine with a new installer and one changed skill
        candidate = tmp / "candidate"
        shutil.copytree(machine, candidate, symlinks=True)
        candidate_layout = resolve_layout(candidate, "tester")
        (candidate_layout.installer).write_text("#!/usr/bin/python3\n# installer v2\n",
                                                encoding="utf-8")
        (candidate_layout.skills / "alpha/SKILL.md").write_text("# alpha v2\n", encoding="utf-8")
        candidate_manifest = _self_test_manifest(candidate_layout, generated_at="2026-11-08T16:00:00Z")
        manifest_path = tmp / "candidate-manifest.json"
        manifest_path.write_text(json.dumps(candidate_manifest, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
        out = tmp / "release"
        # The candidate installer is not the fixture pair's bytes: mint the
        # pair the cut will carry, exactly as an operator does after re-signing.
        candidate_pair = (tmp / "candidate-installer.sig", pub)
        candidate_pair[0].write_bytes(signature_file_bytes(
            sign_bytes((candidate_layout.installer).read_bytes(), key)))
        real_pair = globals()["installer_pair_paths"]
        globals()["installer_pair_paths"] = lambda _layout: candidate_pair
        try:
            document = build_release(candidate_layout, manifest_path, release_id="2026.11.08-2",
                                     boot_kernel=os.uname().release, out_dir=out,
                                     generated_at="2026-11-08T16:10:00Z", key=key)
        finally:
            globals()["installer_pair_paths"] = real_pair
        check.expect("release: artifacts hashed and named",
                     set(document["artifacts"]) == {"installer", "skills"}
                     and (out / "release.json.sig").is_file()
                     and (out / "manifest.json.sig").is_file())
        check.expect("release: the bundle carries the signed pair byte for byte",
                     (out / "odile_install.py.sig").read_bytes() == candidate_pair[0].read_bytes()
                     and (out / "odile-signing-key.pub").read_bytes() == fixture_pair[1].read_bytes()
                     and verify_bytes((out / "odile_install.py").read_bytes(),
                                      signature_bytes((out / "odile_install.py.sig").read_bytes(),
                                                      "odile_install.py.sig"),
                                      out / "odile-signing-key.pub"))

        files = {"http://rel.invalid/" + path.name: path.read_bytes() for path in out.iterdir()}
        fetch = lambda url: files[url]  # noqa: E731 - a fixture
        url = "http://rel.invalid/release.json"
        release = load_release(url, pub, fetch=fetch, kernel=os.uname().release)
        check.expect("load: release and manifest verify", release.release_id == "2026.11.08-2")

        # the release this machine was installed from, cut the same way: the
        # dry-run ordering check below must see a genuinely older release
        old_manifest = _self_test_manifest(layout, generated_at="2026-11-08T15:00:00Z")
        old_manifest_path = tmp / "old-manifest.json"
        old_manifest_path.write_text(json.dumps(old_manifest, indent=2, sort_keys=True) + "\n",
                                     encoding="utf-8")
        old_out = tmp / "old-release"
        globals()["installer_pair_paths"] = lambda _layout: fixture_pair
        try:
            build_release(layout, old_manifest_path, release_id="2026.11.08-1",
                          boot_kernel=os.uname().release, out_dir=old_out,
                          generated_at="2026-11-08T14:00:00Z", key=key)
        finally:
            globals()["installer_pair_paths"] = real_pair
        old_files = {"http://rel.invalid/old/" + path.name: path.read_bytes()
                     for path in old_out.iterdir()}
        old_url = "http://rel.invalid/old/release.json"
        old_release = load_release(old_url, pub, fetch=lambda u: old_files[u],
                                   kernel=os.uname().release)
        plan = plan_changes(release, layout)
        try:
            check.expect("plan: installer, skills and records change",
                         {change.name for change in plan.changes}
                         == {"installer", "skills", "records"},
                         f"got {[change.name for change in plan.changes]}")
            check.expect("plan: one skill updated", plan.updated == ["alpha"],
                         f"got {plan.updated}")
        finally:
            plan.cleanup()

        applied = apply_release(release, layout, assume_yes=True, allow_downgrade=False,
                                key_id=key_fingerprint(pub, private=False))
        check.expect("apply: exit 0", applied == EXIT_OK)
        check.expect("apply: installer is the release's bytes",
                     layout.installer.read_bytes() == (out / "odile_install.py").read_bytes())
        check.expect("apply: skill updated",
                     hash_tree(layout.skills / "alpha", layout) == candidate_manifest["skills"]["alpha"])
        check.expect("apply: odile-home rendered for this machine",
                     b"tester" in (layout.skills / "odile-home/SKILL.md").read_bytes())
        check.expect("apply: no post-check problems", verify_installed(release, layout) == [])
        # Drift between the machine's program and the release row (t_f6dd9b65):
        # reported, never refused, because apply does not write the file.
        check.expect("backup drift: a machine at the row reports none",
                     verify_backup_drift(release, layout) is None)
        program_path = layout.backup_program
        program_bytes = program_path.read_bytes()
        program_path.write_bytes(program_bytes + b"# drifted\n")
        drift_line = verify_backup_drift(release, layout)
        check.expect("backup drift: a drifted program is named with both hashes",
                     drift_line is not None
                     and str(sha256_file(program_path))[:12] in drift_line
                     and release.manifest["backup_sha256"][:12] in drift_line,
                     f"got {drift_line}")
        program_path.write_bytes(program_bytes)
        again = apply_release(release, layout, assume_yes=True, allow_downgrade=False,
                              key_id=key_fingerprint(pub, private=False))
        check.expect("apply: a second run is 'same' and writes nothing", again == EXIT_OK)

        # the post-check has teeth: a changed file on the machine is named
        installer_bytes = layout.installer.read_bytes()
        layout.installer.write_bytes(installer_bytes + b"# tampered\n")
        problems = verify_installed(release, layout)
        check.expect("post-check: a changed installer is named", "installer_sha256" in problems,
                     f"got {problems}")
        layout.installer.write_bytes(installer_bytes)
        skill_file = layout.skills / "alpha/SKILL.md"
        skill_bytes = skill_file.read_bytes()
        skill_file.write_bytes(skill_bytes + b"tampered\n")
        problems = verify_installed(release, layout)
        check.expect("post-check: a changed skill is named",
                     any(problem.startswith("skills") for problem in problems), f"got {problems}")
        skill_file.write_bytes(skill_bytes)

        # apply --dry-run must refuse an older release exactly as check and
        # apply do; it used to print a plan and exit 0
        check.raises("dry run: an older release is refused", EXIT_REFUSED,
                     dry_run, old_release, layout, allow_downgrade=False,
                     key_id=key_fingerprint(pub, private=False))
        check.expect("dry run: --allow-downgrade still prints the plan",
                     dry_run(old_release, layout, allow_downgrade=True,
                             key_id=key_fingerprint(pub, private=False)) == EXIT_OK)

        # a rollback must leave every unit on the restored bytes; the scratch
        # root only prints the scratch line, so record the units it asks for
        restart_calls: list[list[str]] = []

        def record_restart(_layout, units):
            restart_calls.append(list(units))

        real_restart = globals()["restart_units"]
        globals()["restart_units"] = record_restart
        try:
            rolled = restore_snapshot(layout, snapshot_path(layout))
        finally:
            globals()["restart_units"] = real_restart
        check.expect("rollback: restored the tree", rolled)
        check.expect("rollback: a restored installer restarts nothing",
                     restart_calls == [], f"got {restart_calls}")
        check.expect("rollback: installer is the old bytes",
                     layout.installer.read_bytes() == b"#!/usr/bin/python3\n# installer\n")
        check.expect("rollback: skill digest is the old one",
                     hash_tree(layout.skills / "alpha", layout)
                     == _self_test_manifest(layout, generated_at="2026-11-08T15:00:00Z")["skills"]["alpha"])
        check.expect("rollback: second run is 'same'",
                     restore_snapshot(layout, snapshot_path(layout)) is False)

        # an apply that moved the Hermes commit: the rollback checks it out and
        # must restart the agent, which is otherwise still running the new one
        probe = layout.install_dir / "ROLLBACK-PROBE"
        probe.write_text("a release moved Hermes here\n", encoding="utf-8")
        env = git_env()
        subprocess.run(["git", "-C", str(layout.install_dir), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(layout.install_dir), "-c", "user.email=t@example.invalid",
                        "-c", "user.name=test", "commit", "-q", "-m", "rollback probe"],
                       check=True, env=env)
        snapshot_head = json.loads((snapshot_path(layout) / "snapshot.json").read_text(
            encoding="utf-8"))["hermes_head"]
        restart_calls.clear()
        globals()["restart_units"] = record_restart
        try:
            rolled = restore_snapshot(layout, snapshot_path(layout))
        finally:
            globals()["restart_units"] = real_restart
        check.expect("rollback: a restored Hermes head restarts odile-agent",
                     bool(rolled) and restart_calls == [["odile-agent"]]
                     and git_head(layout) == snapshot_head,
                     f"got {restart_calls}, head {str(git_head(layout))[:12]}, "
                     f"snapshot {str(snapshot_head)[:12]}")

        # an apply that replaced the Webapp bundle: the rollback restores it and
        # restarts the agent; a no-op rollback restarts nothing
        take_snapshot(layout, Plan(changes=[Change("webapp", "replace", "probe")]), release)
        build_file = layout.webapp / "hermes-build.json"
        bundle = read_webapp_build(layout)
        build_file.write_text(json.dumps({"node": "26.12.1", "inputs": {"sourceHash": "c" * 64},
                                          "outputHash": "d" * 64}), encoding="utf-8")
        restart_calls.clear()
        globals()["restart_units"] = record_restart
        try:
            rolled = restore_snapshot(layout, snapshot_path(layout))
            again = restore_snapshot(layout, snapshot_path(layout))
        finally:
            globals()["restart_units"] = real_restart
        check.expect("rollback: a restored Webapp bundle restarts odile-agent",
                     bool(rolled) and restart_calls == [["odile-agent"]]
                     and read_webapp_build(layout) == bundle,
                     f"got {restart_calls}, build {read_webapp_build(layout)}")
        check.expect("rollback: a no-op restore restarts nothing", again is False)

        # a tampered release document
        bad = dict(files)
        bad[url] = files[url].replace(b'"2026.11.08-2"', b'"2026.11.08-3"')
        check.raises("tamper: release bytes changed", EXIT_BAD_SIGNATURE,
                     load_release, url, pub, fetch=lambda u: bad[u], kernel=os.uname().release)
        # a tampered manifest, with a re-signed release that points at it
        manifest_bytes = files[url.replace("release.json", "manifest.json")]
        manifest_object = json.loads(manifest_bytes.decode("utf-8"))
        manifest_object["installer_sha256"] = "0" * 64
        manifest_tampered = (json.dumps(manifest_object, indent=2, sort_keys=True) + "\n").encode("utf-8")
        files2 = dict(files)
        files2["http://rel.invalid/manifest.json"] = manifest_tampered
        document_reissued = json.loads(files[url].decode())
        document_reissued["manifest"]["sha256"] = sha256(manifest_tampered)
        data, sig = _self_test_sign(document_reissued, key)
        files2[url] = data
        files2[url + SIG_SUFFIX] = sig
        check.raises("tamper: manifest bytes changed under a re-signed release", EXIT_BAD_SIGNATURE,
                     load_release, url, pub, fetch=lambda u: files2[u], kernel=os.uname().release)
        # a tampered artifact
        files3 = dict(files)
        files3["http://rel.invalid/odile_install.py"] = files[
            "http://rel.invalid/odile_install.py"].replace(b"v2", b"v3")
        check.raises("tamper: installer artifact changed", EXIT_INCONSISTENT,
                     load_release, url, pub, fetch=lambda u: files3[u], kernel=os.uname().release)
        # another boot chain
        check.raises("boot: another kernel is refused", EXIT_REFUSED,
                     load_release, url, pub, fetch=fetch, kernel="0.0.0-not-this-machine")
        # another Hermes commit, not present in the checkout
        document_commit = json.loads(files[url].decode())
        document_commit["notes"] = "commit"
        files4 = dict(files)
        data, sig = _self_test_sign(document_commit, key)
        files4[url] = data
        files4[url + SIG_SUFFIX] = sig
        manifest_commit = json.loads(files["http://rel.invalid/manifest.json"].decode())
        manifest_commit["hermes"]["commit"] = "c" * 40
        manifest_bytes = (json.dumps(manifest_commit, indent=2, sort_keys=True) + "\n").encode()
        data, sig = _self_test_sign(manifest_commit, key)
        files4["http://rel.invalid/manifest.json"] = manifest_bytes
        files4["http://rel.invalid/manifest.json.sig"] = sig
        document_commit["manifest"]["sha256"] = sha256(manifest_bytes)
        data, sig = _self_test_sign(document_commit, key)
        files4[url] = data
        files4[url + SIG_SUFFIX] = sig
        release_commit = load_release(url, pub, fetch=lambda u: files4[u], kernel=os.uname().release)
        # the installer and skills differ again after the rollback, so plan reaches the commit check
        check.raises("commit: a commit this checkout lacks is refused", EXIT_REFUSED,
                     plan_changes, release_commit, layout)
        # schema refusals
        check.raises("schema: an unknown artifact is refused", EXIT_INCONSISTENT,
                     validate_release_document,
                     {"schema": 1, "product": "odile", "release": "r", "generated_at": "2026-11-08T00:00:00Z",
                      "boot": {"kernel": "k"}, "manifest": {"url": "m", "sha256": "0" * 64},
                      "artifacts": {"kernel": {"url": "k", "sha256": "0" * 64}}})
        check.raises("schema: a manifest without skills is refused", EXIT_INCONSISTENT,
                     validate_manifest, {"schema": 1,
                                         "installer_sha256": "0" * 64,
                                         "hermes": {"commit": "0" * 40,
                                                    "webapp": {"node": "n", "source_sha256": "0" * 64,
                                                               "output_sha256": "0" * 64}}})
    return check.report()


def hash_tree_dir_from_bytes(tmp: Path, canonical: bytes, layout: Layout) -> str:
    """Hash a one-file skill built from the canonical bytes, for the round trip."""
    directory = tmp / "round-trip" / "odile-home"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_bytes(canonical)
    return hash_tree(directory, layout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="odile-update",
        description="Pull and apply a signed Odile app-layer release, or roll it back.")
    parser.add_argument("--version", action="version", version=f"odile-update {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help="show the recorded release and the installed app layer")
    add_common(status)
    status.set_defaults(function=cmd_status)

    check = commands.add_parser("check", help="fetch, verify and print the plan; write nothing")
    add_common(check)
    check.set_defaults(function=cmd_check)

    apply = commands.add_parser("apply", help="apply a signed release")
    add_common(apply)
    apply.add_argument("--yes", action="store_true", help="do not ask for confirmation (the timer's flag)")
    apply.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
    apply.add_argument("--allow-downgrade", action="store_true",
                       help="apply a release older than the recorded one")
    apply.set_defaults(function=cmd_apply)

    rollback = commands.add_parser("rollback", help="restore the snapshot taken by the last apply")
    add_common(rollback)
    rollback.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    rollback.add_argument("--snapshot", help="a snapshot directory (default <var>/odile-update/snapshot)")
    rollback.set_defaults(function=cmd_rollback)

    release = commands.add_parser("release", help="cut a signed release from a candidate root (HQ only)")
    add_common(release, network=False)
    release.add_argument("--manifest", required=True, help="the manifest built from the candidate root")
    release.add_argument("--release", required=True, help="the release id, e.g. 2026.11.08-2")
    release.add_argument("--boot-kernel", required=True,
                         help="the kernel release the release is built for (uname -r on the image)")
    release.add_argument("--out-dir", required=True, help="where to write the release directory")
    release.add_argument("--generated-at", help="an ISO-8601 UTC stamp (default: now)")
    release.add_argument("--key", help="the Ed25519 private key (0600); signs the manifest and the release")
    release.add_argument("--with-webapp", action="store_true",
                         help="also ship the Webapp bundle (needed when the Hermes commit or bundle changes)")
    release.add_argument("--notes", help="one line of human text for the release document")
    release.set_defaults(function=cmd_release)

    self_test_parser = commands.add_parser("self-test", help="test the pure functions and one whole release")
    self_test_parser.set_defaults(function=cmd_self_test)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.function(args)
    except UpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
