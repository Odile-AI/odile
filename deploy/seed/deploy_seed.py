#!/usr/bin/python3
"""Ensure the seed service and its Caddy route exist on HQ.

ODL-204 built the seed service but left the unit and the ``/seed`` route as
hand steps in ``docs/runbooks/seed.md`` §3. A rebuilt HQ therefore had no
``/seed`` and every tenant install failed at step 3, before any user-visible
progress. This script owns the two state changes so ``./scripts/deploy.sh`` carries
them:

  * install ``deploy/seed/odile-seed.service`` at
    ``/etc/systemd/system/odile-seed.service`` and make it run at boot
    (``enable --now``), with a timestamped copy of any unit it replaces;
  * splice ``deploy/seed/odile-seed.caddy`` into the live Caddyfile between
    ``# BEGIN odile-seed`` and ``# END odile-seed``, leaving every other block
    exactly as it was. Caddy runs with ``admin off``, so a changed file is
    backed up, checked with ``caddy validate``, and only then applied with a
    restart — a file Caddy rejects is restored and the run fails.

Both steps are idempotent: a converged host prints ``same``, writes nothing and
restarts nothing. The seed module is hashed against the copy the deploy
replaced (``--previous-seed-sha``), so the service restarts only when the code
it runs actually changed.

The service needs two root-only credentials: ``/root/.odile/seed-config.json``
(inference key, model, support key, backup URL, mail relay) and the *seed*
signing key ``/root/.odile/seed-signing.pem``. The key is created here when it
is absent (Ed25519, 0600, root, idempotent) because it belongs to this host;
the config is prepared per ``docs/runbooks/seed.md`` §3. Both are handed to the
unprivileged service with ``LoadCredential=``. A missing or group/world-readable
credential stops the run before anything is installed.

The seed key is not the release key (ODL-514). The release key signs
installers, manifests, releases and the certificate over the seed public key;
it is CI-held (ODL-517) and never lands on this box. The certificate is
``deploy/seed/seed-key.crt`` and the certified public key is
``deploy/seed/seed-key.pub`` — both committed, both public. This script refuses
to install the service when the certificate does not certify the key this host
will sign with; the fix is the signing ceremony (``make seed-cert``, run by the
release workflow; docs/runbooks/seed.md §3a), not a hand edit.

A rebuild's sealed entry is a third file, ``/var/lib/odile/seed-rebuilds.json``:
the service reads it per exchange (not through a credential copy), so the deploy
checks it can be read by the service user and that every entry is the sealed
shape — a plaintext password in it stops the run, tested by the service's own
``load_rebuilds``. A fourth, ``/var/lib/odile/seed-backup-pairs.json``, holds
each tenant's Hetzner sub-account pair (ODL-924: the repository URL, the two
private keys, the pinned host-key line) for the same reason — ``odilectl
provision`` writes it between two exchanges, and a restart per tenant would be
a tenant install failing across that restart — so ``check_backup_pairs`` holds
it to the service's own ``load_backup_pairs`` and the same owner/mode rule.

    /usr/bin/python3 deploy/seed/deploy_seed.py --dry-run
    /usr/bin/python3 deploy/seed/deploy_seed.py --previous-seed-sha <sha256>

Exit codes: 0 success (changed or already converged), 1 a failed apply (Caddy
was restored), 2 a bad input or a missing prerequisite.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
REBUILDS = Path("/var/lib/odile/seed-rebuilds.json")
# ODL-924: each tenant's Hetzner sub-account pair (repository URL, private
# keys, pinned host key), one entry per slug, written by `odilectl provision`
# and read per exchange.
BACKUP_PAIRS = Path("/var/lib/odile/seed-backup-pairs.json")

UNIT_TEMPLATE = HERE / "odile-seed.service"
ROUTE_TEMPLATE = HERE / "odile-seed.caddy"

UNIT = "odile-seed"
UNIT_TARGET = Path("/etc/systemd/system/odile-seed.service")
CADDYFILE = Path("/etc/caddy/Caddyfile")

CONFIG_FILE = Path("/root/.odile/seed-config.json")
# ODL-514: the seed key is not the release key. It signs every tenant seed,
# lives on HQ, and is loaded only by odile-seed.service. The release key (never
# here) signs the certificate over its public half.
SEED_KEY_FILE = Path("/root/.odile/seed-signing.pem")
SEED_PUB = HERE / "seed-key.pub"
SEED_CERT = HERE / "seed-key.crt"
RELEASE_PUBKEY = REPO / "installer" / "odile-signing-key.pub"
RUNTIME_SEED = Path("/opt/odile/seed/seed.py")

SEED_PORT = 8081
SEED_HEALTH_URL = f"http://127.0.0.1:{SEED_PORT}/health"
SEED_ROUTE_URL = "https://odile.fyi/seed"
LANDING_URL = "https://odile.fyi/"

BEGIN = "# BEGIN odile-seed"
END = "# END odile-seed"
SITE = "odile.fyi"
APP_PROXY = "reverse_proxy 127.0.0.1:8080"
SEED_MATCHER = "@seed path /seed"

SYSTEMCTL = "systemctl"
CADDY = "caddy"
CURL = "curl"

# How long a probe waits for Caddy or the service to come back after a restart.
PROBE_SECONDS = 20.0


# --------------------------------------------------------------------------
# the Caddy splice — pure text, so the tests can hold every shape


def _code(line: str) -> str:
    """A Caddyfile line with its trailing `# ...` comment removed."""
    return re.sub(r"(?<!\S)#.*$", "", line)


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _closing(lines: list[str], start: int, where: Path | str) -> int:
    """Index of the `}` that closes the block opened on line `start`."""
    if "{" not in _code(lines[start]):
        raise ValueError(f"{where}: line {start + 1} does not open a block")
    depth = 0
    for index in range(start, len(lines)):
        code = _code(lines[index])
        opens, closes = code.count("{"), code.count("}")
        depth += opens - closes
        if depth < 0:
            raise ValueError(f"{where}: line {index + 1} closes a block that never opened")
        if depth == 0:
            return index
    raise ValueError(f"{where}: no closing brace after line {start + 1}")


def _site_span(lines: list[str], where: Path | str) -> tuple[int, int]:
    """The line index of `odile.fyi {` and of its closing brace."""
    opener = f"{SITE} {{"
    for index, line in enumerate(lines):
        if _code(line).strip() == opener:
            return index, _closing(lines, index, where)
    raise ValueError(f"{where}: no `{opener}` site block")


def _next_meaningful(lines: list[str], index: int, stop: int) -> int:
    while index < stop and not _code(lines[index]).strip():
        index += 1
    return index


def splice_caddyfile(existing: str, fragment: str, *, where: Path | str = "Caddyfile") -> str:
    """Return `existing` with the seed route replaced by `fragment`.

    Four shapes are accepted, because a host can be in any of them: the marked
    region already present; a fresh bare `reverse_proxy 127.0.0.1:8080`; a
    fresh `handle { ... }` catch-all; and the unmarked block ODL-332 applied by
    hand on 2026-11-09. All four converge to the same bytes, so a second run
    changes nothing.

    Anything else — a duplicated or unbalanced marker pair, another site, a
    route tail this script does not recognise — raises ValueError rather than
    guessing, so a half-edited Caddyfile can never be overwritten silently.
    """
    lines = existing.splitlines()
    begins = [i for i, line in enumerate(lines) if line.strip() == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if len(begins) != len(ends) or len(begins) > 1:
        raise ValueError(
            f"{where}: expected at most one '{BEGIN}' / '{END}' pair, found "
            f"{len(begins)} begin and {len(ends)} end markers"
        )
    fragment_lines = fragment.rstrip("\n").splitlines()

    def done(start: int, stop: int) -> str:
        result = _replace(lines, start, stop, fragment_lines)
        _check_route(result, where)
        return result

    if begins:
        start, stop = begins[0], ends[0]
        if stop < start:
            raise ValueError(f"{where}: '{END}' comes before '{BEGIN}'")
        return done(start, stop)

    site_start, site_end = _site_span(lines, where)
    seed_at = next((i for i in range(site_start + 1, site_end)
                    if _code(lines[i]).strip() == SEED_MATCHER), None)
    if seed_at is not None:
        start = seed_at
        while start - 1 > site_start and lines[start - 1].strip().startswith("#"):
            start -= 1
        handler = _next_meaningful(lines, seed_at + 1, site_end)
        if handler >= site_end or _code(lines[handler]).strip() != "handle @seed {":
            raise ValueError(f"{where}: '{SEED_MATCHER}' is not followed by a `handle @seed {{` block")
        stop = _closing(lines, handler, where)
        # A hand-applied block usually carries the catch-all right after it;
        # replace that too, or the file would end up with two catch-alls.
        catch_all = _next_meaningful(lines, stop + 1, site_end)
        if catch_all < site_end and _code(lines[catch_all]).strip() == "handle {":
            stop = _closing(lines, catch_all, where)
        return done(start, stop)

    app_at = next((i for i in range(site_start + 1, site_end)
                   if _code(lines[i]).strip() == APP_PROXY), None)
    if app_at is None:
        raise ValueError(
            f"{where}: `{SITE} {{` has neither a seed route nor "
            f"`{APP_PROXY}`; refusing to guess where the seed route belongs"
        )
    handle_at = next((i for i in range(site_start + 1, app_at)
                      if _code(lines[i]).strip() == "handle {"), None)
    if handle_at is not None:
        closing = _closing(lines, handle_at, where)
        body = [_code(lines[index]).strip()
                for index in range(handle_at + 1, closing) if _code(lines[index]).strip()]
        if closing > app_at and body == [APP_PROXY]:
            return done(handle_at, closing)
        raise ValueError(
            f"{where}: the `handle {{` block at line {handle_at + 1} carries "
            f"{body[:3]}; refusing to replace it"
        )
    return done(app_at, app_at)


def _check_route(text: str, where: Path | str) -> None:
    """The spliced site block must route /seed once and the app once, no more."""
    lines = text.splitlines()
    site_start, site_end = _site_span(lines, where)
    body = [_code(line).strip() for line in lines[site_start + 1:site_end]]
    counts = {
        "@seed matcher": body.count(SEED_MATCHER),
        "seed handler": body.count("handle @seed {"),
        "seed proxy": body.count("reverse_proxy 127.0.0.1:8081"),
        "app catch-all": body.count("handle {"),
        "app proxy": body.count(APP_PROXY),
    }
    wrong = {name: count for name, count in counts.items() if count != 1}
    if wrong:
        raise ValueError(f"{where}: the splice would leave {wrong}; refusing to write it")


def _replace(lines: list[str], start: int, stop: int, fragment_lines: list[str]) -> str:
    indent = _indent(lines[start])
    rendered = [indent + line if line.strip() else "" for line in fragment_lines]
    return "\n".join(lines[:start] + rendered + lines[stop + 1:]).rstrip("\n") + "\n"


# --------------------------------------------------------------------------
# files, units and probes


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_atomic(path: Path, data: bytes, mode: int) -> None:
    """Write through a sibling temp file, fsync, chmod, rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def run(command: list[str], *, check: bool = False, timeout: float | None = None):
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode != 0:
        raise SystemExit(
            f"error: {' '.join(command)} failed ({result.returncode}): "
            f"{(result.stderr or result.stdout).strip()[-2000:]}"
        )
    return result


def require_root() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("installing the seed unit and editing /etc/caddy needs root")


def check_config_file() -> str | None:
    """The first problem with the root-only seed config, or None.

    The seed key's mode is checked by `ensure_seed_key` (it may have just been
    created, so it is that function's business).
    """
    path = CONFIG_FILE
    if not path.is_file():
        return (f"error: missing {path} — create it per docs/runbooks/seed.md §3, "
                "then re-run")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        return (f"error: {path} is {mode:03o}; the seed service refuses a "
                f"group/world-readable credential. chmod 600 {path}")
    return None


def seed_public_bytes() -> bytes:
    """The public half of the seed key, exactly as `openssl pkey -pubout` writes it."""
    result = subprocess.run(["openssl", "pkey", "-in", str(SEED_KEY_FILE), "-pubout"],
                            capture_output=True)
    if result.returncode != 0 or b"BEGIN PUBLIC KEY" not in result.stdout:
        raise SystemExit(f"error: cannot read the public half of {SEED_KEY_FILE}: "
                         f"{result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def ensure_seed_key(dry: bool) -> bool:
    """Create the seed key when it is absent; never replace one that exists.

    A rebuilt HQ gets a key of its own, which is why the certificate in the tree
    is checked below: a new key without a new certificate is a service whose
    seeds no installer can verify, so the deploy stops instead of starting it.
    """
    if not SEED_KEY_FILE.is_file():
        print(f"{'would create' if dry else 'create':<12} {SEED_KEY_FILE} "
              "(Ed25519, 0600, root)")
        if dry:
            return True
        SEED_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(SEED_KEY_FILE)],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(f"error: openssl genpkey failed: {result.stderr.strip()}")
        os.chmod(SEED_KEY_FILE, 0o600)
        os.chown(SEED_KEY_FILE, 0, 0)
        ensure_seed_pub(False)
        return True
    mode = SEED_KEY_FILE.stat().st_mode & 0o777
    if mode & 0o077:
        raise SystemExit(f"error: {SEED_KEY_FILE} is {mode:03o}; the seed key is root-only. "
                         f"chmod 600 {SEED_KEY_FILE}")
    print(f"same         {SEED_KEY_FILE} (0600)")
    return ensure_seed_pub(dry)


def ensure_seed_pub(dry: bool) -> bool:
    """Keep `deploy/seed/seed-key.pub` the key this host signs with (it is public)."""
    public = seed_public_bytes()
    current = SEED_PUB.read_bytes() if SEED_PUB.is_file() else None
    if current == public:
        print(f"same         {SEED_PUB}")
        return False
    print(f"{'would write' if dry else 'write':<12} {SEED_PUB} (the seed public key, 0644)")
    if not dry:
        write_atomic(SEED_PUB, public, 0o644)
    return True


def check_seed_identity() -> str | None:
    """The committed certificate must certify the key this host will sign with.

    Returns the reason, in one message naming the ceremony, or None. The service
    embeds the certificate in every seed, so a host that starts with a stale one
    serves seeds no installer accepts.
    """
    seed = seed_module()
    try:
        public = seed.public_key_bytes(str(SEED_KEY_FILE))
        certificate = seed.read_certificate(str(SEED_CERT))
    except seed.ConfigError as exc:
        return f"error: {exc}"
    current = SEED_PUB.read_bytes() if SEED_PUB.is_file() else None
    if current != public:
        return (f"error: {SEED_PUB} is not the public half of {SEED_KEY_FILE}; "
                "the signing ceremony writes both")
    if not RELEASE_PUBKEY.is_file():
        return f"error: missing {RELEASE_PUBKEY} (the release public key the certificate names)"
    if not seed.verify_bytes(public, certificate, str(RELEASE_PUBKEY)):
        return (f"error: {SEED_CERT} does not verify against {RELEASE_PUBKEY}\n"
                f"       the seed key on this host is not certified; run, in the release "
                f"workflow:\n"
                f"       make seed-cert FILE={SEED_PUB} KEY=<file key>   # or SIGNER=<ci signer>\n"
                "       commit deploy/seed/seed-key.crt and deploy/seed/seed-key.pub, re-run")
    print(f"certificate  {SEED_CERT} certifies {SEED_KEY_FILE} "
          f"(release public key {RELEASE_PUBKEY})")
    return None


def seed_module():
    """The runtime program, loaded by path the way the service and the CLI do."""
    path = REPO / "seed" / "seed.py"
    spec = importlib.util.spec_from_file_location("odile_seed_for_deploy", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_rebuilds() -> str | None:
    """The sealed rebuild entries the service will read, and who can read them.

    The file holds the user's old repository password *sealed* under a setup
    token, so a plaintext `backup_password` is refused here by the service's own
    validator (`seed.load_rebuilds`), not by a second copy of the rule. The
    service runs as `odile`, so a root-owned 0600 file would break the next
    rebuild: the deploy names it instead of letting the first rebuilt guest
    fail closed.
    """
    seed = seed_module()
    try:
        rebuilds = seed.load_rebuilds(str(REBUILDS))
    except seed.ConfigError as exc:
        return f"error: {exc}"
    if not rebuilds:
        return None
    if os.geteuid() == 0:
        try:
            owner = REBUILDS.stat().st_uid
        except OSError as exc:
            return f"error: cannot stat {REBUILDS}: {exc}"
        try:
            import pwd

            service = pwd.getpwnam("odile").pw_uid
        except KeyError:
            service = owner
        if owner != service:
            return (f"error: {REBUILDS} is owned by uid {owner}; the seed service runs "
                    f"as odile (uid {service}). chown odile {REBUILDS}")
    print(f"{'rebuilds':<12} {len(rebuilds)} sealed entr"
          f"{'y' if len(rebuilds) == 1 else 'ies'} ({REBUILDS}; no plaintext password)")
    return None


def check_backup_pairs() -> str | None:
    """The per-tenant sub-account pairs the service will read, and who can read them.

    ODL-924. The service's own validator (`seed.load_backup_pairs`) checks
    every entry's shape — the two logins, the nested homes, the no-port URLs,
    both private keys, one host-key line, no plaintext password. The
    0600-and-owned-by-`odile` rule is `check_rebuilds`'s reason: the service
    runs as `odile`, so a root-owned file would break the next provision with
    no request to blame.
    """
    seed = seed_module()
    try:
        pairs = seed.load_backup_pairs(str(BACKUP_PAIRS))
    except seed.ConfigError as exc:
        return f"error: {exc}"
    if not pairs:
        return None
    if os.geteuid() == 0:
        try:
            info = BACKUP_PAIRS.stat()
        except OSError as exc:
            return f"error: cannot stat {BACKUP_PAIRS}: {exc}"
        try:
            import pwd

            service = pwd.getpwnam("odile").pw_uid
        except KeyError:
            service = info.st_uid
        if info.st_uid != service:
            return (f"error: {BACKUP_PAIRS} is owned by uid {info.st_uid}; the seed "
                    f"service runs as odile (uid {service}). chown odile {BACKUP_PAIRS}")
        if info.st_mode & 0o077:
            return (f"error: {BACKUP_PAIRS} is {info.st_mode & 0o777:03o}; the "
                    f"sub-account pairs are root-and-service only. chmod 600 {BACKUP_PAIRS}")
    print(f"{'pairs':<12} {len(pairs)} entr{'y' if len(pairs) == 1 else 'ies'} "
          f"({BACKUP_PAIRS}; per-tenant keys, no password)")
    return None


def seed_module_sha() -> str:
    return hashlib.sha256(RUNTIME_SEED.read_bytes()).hexdigest() if RUNTIME_SEED.is_file() else ""


def ensure_unit(dry: bool) -> bool:
    desired = UNIT_TEMPLATE.read_bytes()
    current = UNIT_TARGET.read_bytes() if UNIT_TARGET.is_file() else None
    if current == desired:
        print(f"same         {UNIT_TARGET}")
        return False
    print(f"{'would write' if dry else 'write':<12} {UNIT_TARGET} (User=odile, two LoadCredential lines)")
    if dry:
        return True
    if current is not None:
        backup = UNIT_TARGET.with_name(f"{UNIT_TARGET.name}.bak-{utc_stamp()}")
        shutil.copy2(UNIT_TARGET, backup)
        print(f"backup       {backup}")
    write_atomic(UNIT_TARGET, desired, 0o644)
    return True


def ensure_service(dry: bool, unit_changed: bool, module_changed: bool) -> bool:
    """Enabled at boot, running, and running the bytes this deploy installed."""
    changed = unit_changed
    if unit_changed:
        print(f"{'would reread' if dry else 'daemon-reload':<12} systemd units")
        if not dry:
            run([SYSTEMCTL, "daemon-reload"], check=True)
    enabled = run([SYSTEMCTL, "is-enabled", UNIT]).stdout.strip()
    was_active = run([SYSTEMCTL, "is-active", UNIT]).stdout.strip() == "active"
    just_started = False
    if enabled != "enabled":
        print(f"{'would enable' if dry else 'enable':<12} {UNIT} (enable --now)")
        just_started = True
        if not dry:
            run([SYSTEMCTL, "enable", "--now", UNIT], check=True)
            # `enable` exits 0 on a unit with no [Install] section and creates
            # no symlink, so trust the re-read, not the exit code.
            state = run([SYSTEMCTL, "is-enabled", UNIT]).stdout.strip()
            if state != "enabled":
                raise SystemExit(
                    f"error: systemctl enable {UNIT} left it {state!r}; "
                    "the unit has no usable [Install] section"
                )
        changed = True
    else:
        print(f"same         {UNIT} enabled at boot")
    if not was_active and not just_started:
        print(f"{'would start' if dry else 'start':<12} {UNIT}")
        just_started = True
        if not dry:
            run([SYSTEMCTL, "start", UNIT], check=True)
        changed = True
    if just_started:
        return changed
    if unit_changed or module_changed:
        reason = "unit changed" if unit_changed else "seed/ changed"
        print(f"{'would restart' if dry else 'restart':<12} {UNIT} ({reason})")
        if not dry:
            run([SYSTEMCTL, "restart", UNIT], check=True)
        changed = True
    else:
        print(f"same         {UNIT} active")
    return changed


def ensure_route(dry: bool) -> bool:
    current = CADDYFILE.read_text(encoding="utf-8")
    desired = splice_caddyfile(current, ROUTE_TEMPLATE.read_text(encoding="utf-8"),
                               where=CADDYFILE)
    label = f"{CADDYFILE} (@seed -> 127.0.0.1:{SEED_PORT})"
    if desired == current:
        print(f"same         {label}")
        return False
    if dry:
        print(f"would write  {label} (backup {CADDYFILE.name}.bak-<UTC>, "
              "caddy validate, restart caddy)")
        return True
    backup = CADDYFILE.with_name(f"{CADDYFILE.name}.bak-{utc_stamp()}")
    shutil.copy2(CADDYFILE, backup)
    print(f"backup       {backup}")
    write_atomic(CADDYFILE, desired.encode("utf-8"), 0o644)
    print(f"write        {label}")
    result = run([CADDY, "validate", "--config", str(CADDYFILE)])
    if result.returncode != 0:
        write_atomic(CADDYFILE, current.encode("utf-8"), 0o644)
        detail = (result.stdout + result.stderr).strip()
        raise SystemExit(f"error: caddy validate rejected {CADDYFILE}; restored it "
                         f"from {backup}\n{detail[-2000:]}")
    print(f"validate     {CADDYFILE} ok")
    run([SYSTEMCTL, "restart", "caddy"], check=True)
    print("restart      caddy (route changed)")
    return True


def _deadline() -> float:
    return time.monotonic() + PROBE_SECONDS


def poll_health() -> tuple[bool, str]:
    deadline = _deadline()
    detail = "no answer"
    while True:
        result = run([CURL, "-fsS", "--max-time", "5", SEED_HEALTH_URL])
        detail = (result.stdout or result.stderr).strip() or "no answer"
        if result.returncode == 0 and result.stdout.strip() == "ok":
            return True, "ok"
        if time.monotonic() >= deadline:
            return False, detail
        time.sleep(0.25)


def poll_code(url: str, expected: str) -> str:
    deadline = _deadline()
    code = "000"
    while True:
        result = run([CURL, "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                      "--max-time", "5", url])
        code = result.stdout.strip() or "000"
        if code == expected or time.monotonic() >= deadline:
            return code
        time.sleep(0.25)


def probe(dry: bool) -> None:
    if dry:
        print(f"would probe  {SEED_HEALTH_URL} (expect ok)")
        print(f"would probe  {SEED_ROUTE_URL} (expect 405, POST only)")
        print(f"would probe  {LANDING_URL} (expect 200)")
        return
    healthy, detail = poll_health()
    if not healthy:
        raise SystemExit(
            f"error: {SEED_HEALTH_URL} answered {detail!r}, expected 'ok'; "
            f"check `systemctl status {UNIT}` and `journalctl -u {UNIT}`"
        )
    print(f"healthy      {SEED_HEALTH_URL} ok")
    code = poll_code(SEED_ROUTE_URL, "405")
    if code != "405":
        raise SystemExit(
            f"error: GET {SEED_ROUTE_URL} answered {code}, expected 405; check the "
            f"route in {CADDYFILE} and `systemctl status caddy`"
        )
    print(f"route        {SEED_ROUTE_URL} 405 (POST only; GET is 405 by design)")
    code = poll_code(LANDING_URL, "200")
    if code != "200":
        raise SystemExit(f"error: {LANDING_URL} answered {code}, expected 200")
    print(f"page         {LANDING_URL} 200")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the seed service and its Caddy route (idempotent).")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan; write nothing, run no command")
    parser.add_argument("--previous-seed-sha", default="",
                        help="sha256 of /opt/odile/seed/seed.py before this deploy; "
                             "the service restarts only when it differs")
    args = parser.parse_args(argv)

    try:
        require_root()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    problem = check_config_file()
    if problem:
        print(problem, file=sys.stderr)
        return 2
    key_changed = ensure_seed_key(args.dry_run)
    if SEED_KEY_FILE.is_file():
        problem = check_seed_identity()
        if problem:
            print(problem, file=sys.stderr)
            return 2
    else:
        # A dry run on a box with no key yet: the key is created on apply, and
        # the certificate for it has to be cut before the service starts.
        print(f"would check  {SEED_CERT} certifies the new {SEED_KEY_FILE} "
              "(cut it with make seed-cert in the release workflow)")
    problem = check_rebuilds()
    if problem:
        print(problem, file=sys.stderr)
        return 2
    problem = check_backup_pairs()
    if problem:
        print(problem, file=sys.stderr)
        return 2

    module_changed = bool(args.previous_seed_sha) and args.previous_seed_sha != seed_module_sha()
    unit_changed = ensure_unit(args.dry_run)
    changed = key_changed or unit_changed
    changed |= ensure_service(args.dry_run, unit_changed, module_changed)
    changed |= ensure_route(args.dry_run)
    probe(args.dry_run)
    state = "planned" if args.dry_run else ("deployed" if changed else "converged")
    print(f"seed: {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
