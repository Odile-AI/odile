#!/usr/bin/python3
"""Drive the machine-free tenant simulation for ODL-203.

This is the evidence harness, not the installer. It creates an unprivileged
user and a scratch root, runs a scratch seed service (ODL-204) with a real
NEAR key read at run time, then runs ``installer/odile_install.py --root`` as
that user twice and asserts the card's simulation DoD:

  * the first run installs; the second prints ``tenant: same``;
  * the sign-in gate is 302/401/200 (the deploy_demo_site.gate_ok shape);
  * one chat turn went straight to NEAR AI, ran a shell command, wrote a file,
    and one cron job exists and runs;
  * the gateway's process environment holds the per-user key from
    /etc/odile/key.env (0600), and the profile .env carries no inference key;
  * a replayed setup token is 410 used and a backdated one is 410 expired;
  * the recovery file carries the backup password and the fingerprint
    placeholder, and the copy-off/shred commands are printed.

Nothing outside the scratch root, the scratch seed database and the scratch
user is written. The shell is not the simulation: the installer itself runs as
``odiletest`` and is only given the scratch root.

    ./installer/sim_tenant.py --dry-run          # show what a reset would delete
    ./installer/sim_tenant.py --reset --apply    # fresh scratch root + user
    ./installer/sim_tenant.py                     # run 1, run 2, assertions

Exit codes: 0 every assertion passed, 1 a failed assertion, 2 bad input or a
missing prerequisite.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import pwd
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "installer" / "odile_install.py"
INSTALLER_SIG = REPO / "installer" / "odile_install.py.sig"
PUBKEY = REPO / "installer" / "odile-signing-key.pub"
SIGNING_KEY = Path("/root/.odile/installer-signing.pem")
# ODL-514: the seed key is not the release key. The seed service signs seed
# bodies with the HQ seed key; the release key only certifies its public half.
SEED_SIGNING_KEY = Path("/root/.odile/seed-signing.pem")
NEAR_ENV = Path("/root/.hermes/.env")
SEED_PY = REPO / "seed" / "seed.py"
# The relay's certificate, as the test account pins it (ODL-469).
RELAY_CA_FILE = Path("/etc/ssl/certs/mail.odile.fyi.crt")
# The relay's submission account, when this host has one (ODL-469).
MAIL_ACCOUNT = Path("/root/.odile/mail_test_account.json")

DEFAULT_ROOT = Path("/var/tmp/odile-sim")
DEFAULT_USER = "odiletest"
DEFAULT_PORT = 9140
DEFAULT_SEED_PORT = 8082
SLUG = "odl-sim-203"
INSTANCE = "srv-odl-sim-203"
SIGNIN = "odile-sim-password-2026"
BACKUP = "odile-sim-backup-password-2026-0001"
PUBLIC_URL = "http://odile-sim.test:9140"

PASS = 0
FAIL = 0


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"ok   {label}")


def bad(label: str) -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL {label}")


def expect(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        ok(f"{label}: {detail}" if detail else label)
    else:
        bad(f"{label}: {detail}" if detail else label)


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False, **kwargs)


def near_key() -> str:
    try:
        text = NEAR_ENV.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"error: cannot read {NEAR_ENV}: {exc}") from exc
    match = re.search(r"^HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY=(.+)$", text, re.M)
    if not match or not match.group(1).strip():
        raise SystemExit(f"error: no NEAR key in {NEAR_ENV}")
    return match.group(1).strip()


def sim_ids(user: str) -> tuple[int, int]:
    entry = pwd.getpwnam(user)
    return entry.pw_uid, entry.pw_gid


def reset(root: Path, user: str, apply: bool) -> None:
    print(f"reset        root {root} and user {user}" + ("" if apply else " (dry run)"))
    if not apply:
        if root.exists():
            print(f"would delete {root} ({sum(1 for _ in root.rglob('*'))} entries)")
        if _user_exists(user):
            print(f"would delete user {user}")
        print("dry run: nothing deleted, no user removed")
        return
    stop_gateway(root, user)
    if root.exists():
        shutil.rmtree(root)
        print(f"deleted      {root}")
    if _user_exists(user):
        run(["pkill", "-u", user])
        result = run(["userdel", "-r", user])
        if result.returncode != 0:
            raise SystemExit(f"error: userdel {user}: {result.stderr.strip()}")
        print(f"deleted      user {user}")


def _user_exists(user: str) -> bool:
    try:
        pwd.getpwnam(user)
        return True
    except KeyError:
        return False


def ensure_user(root: Path, user: str) -> None:
    home = root / "home" / user
    if not _user_exists(user):
        result = run(["useradd", "--create-home", "--home-dir", str(home),
                      "--shell", "/bin/bash", "--comment", "Odile simulation", user])
        if result.returncode != 0:
            raise SystemExit(f"error: useradd {user}: {result.stderr.strip()}")
        print(f"create       user {user} (home {home}, no sudo)")
    else:
        print(f"same         user {user}")
    home.mkdir(parents=True, exist_ok=True)
    uid, gid = sim_ids(user)
    os.chown(home, uid, gid)
    os.chmod(home, 0o750)


def ensure_scratch(root: Path, user: str) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o755)
    uid, gid = sim_ids(user)
    # The installer runs as the unprivileged user and creates etc/, var/ and
    # usr/ under this root; the root itself must belong to that user.
    os.chown(root, uid, gid)
    bootstrap = root / "bootstrap"
    evidence = root / "evidence"
    for directory in (bootstrap, evidence):
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o755)
    for source, target in ((INSTALLER, bootstrap / "odile_install.py"),
                           (INSTALLER_SIG, bootstrap / "odile_install.py.sig"),
                           (PUBKEY, bootstrap / "odile-signing-key.pub")):
        shutil.copy2(source, target)
        os.chmod(target, 0o755 if target.suffix == ".py" else 0o644)
    return bootstrap, evidence


def seed_env(db: Path, config: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "ODILE_DB": str(db),
        "ODILE_SEED_CONFIG": str(config),
        "ODILE_SEED_SIGNING_KEY": str(SEED_SIGNING_KEY),
    })
    return env


def relay_ca() -> str:
    """The relay's own certificate (ODL-469): the live one, else a marker.

    The sim tenant's seed must carry the same PEM a real seed carries, or the
    sender's STARTTLS fails against the relay's self-signed certificate. On a
    host without the relay's certificate the string is a shape marker; the sim's
    send proof is run where the real one exists.
    """
    try:
        return RELAY_CA_FILE.read_text(encoding="utf-8")
    except OSError:
        return ("-----BEGIN CERTIFICATE-----\n"
                "MIIBhTCCASugAwIBAgIUSIMRELAYCA000000000000000000\n"
                "-----END CERTIFICATE-----\n")


def relay_account(slug: str) -> tuple[str, str]:
    """The submission login: the live test account when this host has it.

    A simulation that sends for real needs a working SASL account. The
    per-tenant *from* address is composed by the sender itself
    (`brief-<slug>@mail.odile.fyi`), so one account serves every simulated
    tenant; without the file the seed carries an obviously fake pair and the
    send proof is skipped.
    """
    try:
        account = json.loads(MAIL_ACCOUNT.read_text(encoding="utf-8"))
        return str(account["username"]), str(account["secret"])
    except (OSError, ValueError, KeyError):
        return f"{slug}@mail.odile.fyi", "sim-relay-secret-not-a-real-secret"


def seed_setup(root: Path, slug: str, instance: str, seed_port: int) -> tuple[str, Path, Path]:
    """Init the scratch ledger, create the tenant and one setup token."""
    state = Path("/root/.odile/installer-sim")
    state.mkdir(parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    db = state / "seed.db"
    config = state / "seed-config.json"
    if db.exists():
        db.unlink()
    env = seed_env(db, config)
    result = run(["/usr/bin/python3", str(SEED_PY), "init"], env=env, cwd=REPO)
    if result.returncode != 0:
        raise SystemExit(f"error: seed init: {result.stderr.strip()}")
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO tenants (slug, email, server_id, created_at) VALUES (?, ?, ?, ?)",
        (slug, f"{slug}@example.invalid", instance, "2026-11-07T00:00:00Z"),
    )
    con.commit()
    con.close()
    result = run(["/usr/bin/python3", str(SEED_PY), "token", "issue", "--tenant", slug],
                 env=env, cwd=REPO)
    if result.returncode != 0:
        raise SystemExit(f"error: token issue: {result.stdout.strip()} {result.stderr.strip()}")
    match = re.search(r"\b([0-9a-f]{64})\b", result.stdout)
    if not match:
        raise SystemExit(f"error: could not parse the token from: {result.stdout!r}")
    token = match.group(1)
    mail_user, mail_secret = relay_account(slug)
    config.write_text(json.dumps({
        "near_api_key": near_key(),
        "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017-abliterated",
        "support_public_key": (PUBKEY.read_text(encoding="utf-8").strip() + " support@odile.fyi"),
        "backup_repo_url": "sftp:odile-backup@backup.odile.fyi:/tenants/{slug}",
        "hermes_version": "2026.11.1",
        "mail_relay": {"host": "mail.odile.fyi", "port": 587,
                       "username": mail_user,
                       "secret": mail_secret,
                       "tls_ca": relay_ca()},
    }, indent=2) + "\n", encoding="utf-8")
    os.chmod(config, 0o600)
    print(f"seed         {db} · tenant {slug} · instance {instance} · token {token[:8]}…")
    return token, db, config


def start_seed(db: Path, config: Path, seed_port: int) -> subprocess.Popen:
    # A previous run may have left a seed service on the port; the harness owns
    # this scratch port, so reclaim it before starting a fresh one.
    run(["fuser", "-k", f"{seed_port}/tcp"], timeout=10)
    time.sleep(0.2)
    env = seed_env(db, config)
    env.update({"ODILE_SEED_BIND": "127.0.0.1", "ODILE_SEED_PORT": str(seed_port)})
    log = Path("/root/.odile/installer-sim/seed.log")
    handle = open(log, "wb")
    process = subprocess.Popen(["/usr/bin/python3", str(SEED_PY), "serve"],
                               stdout=handle, stderr=subprocess.STDOUT,
                               env=env, cwd=str(REPO), start_new_session=True)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{seed_port}/health", timeout=2) as response:
                if response.status == 200:
                    print(f"listening    seed 127.0.0.1:{seed_port} (pid {process.pid})")
                    return process
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    raise SystemExit(f"error: seed service did not start; see {log}")


def stop(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
    print(f"stopped      pid {process.pid}")


def write_kit(bootstrap: Path, user: str, token: str, slug: str, instance: str,
              pubkey: Path) -> Path:
    kit = bootstrap / "recovery-kit.json"
    kit.write_text(json.dumps({
        "slug": slug,
        "instance_id": instance,
        "setup_token": token,
        "signing_public_key": pubkey.read_text(encoding="utf-8"),
    }, indent=2) + "\n", encoding="utf-8")
    uid, gid = sim_ids(user)
    os.chown(kit, uid, gid)
    os.chmod(kit, 0o600)
    return kit


def installer_command(root: Path, user: str, port: int,
                      seed_port: int, kit: Path) -> list[str]:
    home = root / "home" / user
    bootstrap = root / "bootstrap/odile_install.py"
    return [
        "sudo", "-u", user, "-H", "env", f"HOME={home}",
        "/usr/bin/python3", str(bootstrap),
        "--root", str(root), "--user", user,
        "--port", str(port),
        "--public-url", PUBLIC_URL.replace(str(DEFAULT_PORT), str(port)),
        "--seed-url", f"http://127.0.0.1:{seed_port}/seed",
        "--recovery-kit", str(kit),
        "--signin-password", SIGNIN,
        "--backup-password", BACKUP,
        "--i-have-copied-it", "--no-tmux", "--json",
    ]


def run_installer(root: Path, user: str, port: int, seed_port: int,
                  kit: Path, log: Path, label: str) -> subprocess.CompletedProcess[str]:
    command = installer_command(root, user, port, seed_port, kit)
    print(f"run          {label}: {' '.join(command)}")
    started = time.monotonic()
    result = run(command, timeout=5400)
    elapsed = int(time.monotonic() - started)
    log.write_text(result.stdout + "\n--- stderr ---\n" + result.stderr, encoding="utf-8")
    print(f"run          {label}: exit {result.returncode} in {elapsed // 60}m {elapsed % 60}s; "
          f"log {log}")
    return result


def load_installer():
    spec = importlib.util.spec_from_file_location("odile_install_sim", INSTALLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve a class's module through sys.modules while the class
    # body is executed (Python 3.14), so register it before exec_module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sim_layout(module, root: Path, user: str, port: int):
    return module.Layout.scratch(root=root, user=user, webapp_port=port,
                                 public_url=PUBLIC_URL.replace(str(DEFAULT_PORT), str(port)))


def assert_login(module, root: Path, user: str, port: int) -> None:
    layout = sim_layout(module, root, user, port)
    table = module.login_table(layout, "odile", SIGNIN)
    print("== sign-in gate (independent of the installer's own smoke)")
    for label, passed, observed in table:
        expect(label, passed, observed)
    html = next(code for label, _, code in table if label.startswith("GET / without"))
    api = next(code for label, _, code in table if label.startswith("GET /api/sessions without"))
    session = next(code for label, _, code in table if label.startswith("GET / with the session"))
    expect("the gate has the 302/401/200 shape",
           module.gate_ok(int(html.split()[0]), int(api.split()[0]), int(session.split()[0])),
           f"{html}/{api}/{session}")
    # The FQDN above is accepted because the written config declares it in
    # webapp.public_url, the key the Webapp listener reads to set its own
    # listener_public_url (t_d95b8c13). dashboard.public_url names the same
    # host but is not the Webapp's fallback: measured 2026-11-15, it alone
    # answers 400. Both blocks name the simulation's public host here.
    config = layout.hermes_home / "config.yaml"
    text = config.read_text(encoding="utf-8") if config.is_file() else ""
    _, blocks = module.split_blocks(text)
    flat = {key: [module.setting_line(line).strip() for line in module.settings(lines)]
            for key, lines in blocks.items()}
    wanted = f"public_url: {layout.webapp_public_url}"
    expect("the config declares the public host the Webapp reads",
           wanted in flat.get("webapp", []) and wanted in flat.get("dashboard", []),
           f"webapp {layout.webapp_public_url} · dashboard "
           + ("yes" if wanted in flat.get("dashboard", []) else "no"))


def assert_export_program(root: Path) -> None:
    """The installer places `odile-export`, and it is the checkout's bytes.

    The pair printed here is the card's evidence: the scratch root's copy and
    the bytes `odilectl export <slug> --script` prints, both as SHA-256. The
    program is what the tenant runs to take its data with it (ODL-218).
    """
    target = root / "usr/local/bin/odile-export"
    printed = run([sys.executable, "-m", "odilectl", "export", SLUG, "--script"], cwd=REPO)
    expected = printed.stdout.encode("utf-8") if printed.returncode == 0 else b""
    placed = target.read_bytes() if target.is_file() else b""
    mode = target.stat().st_mode & 0o777 if target.is_file() else 0
    print("== the guest export program (ODL-218 follow-up)")
    print(f"export       {target} mode {oct(mode)} "
          f"sha256 {hashlib.sha256(placed).hexdigest()}")
    print(f"odilectl export {SLUG} --script | sha256sum -> "
          f"{hashlib.sha256(expected).hexdigest()}")
    expect("the installer places /usr/local/bin/odile-export", target.is_file(),
           printed.stderr.strip())
    expect("the placed odile-export is 0755", mode == 0o755, oct(mode))
    expect("the placed odile-export is byte-identical to `odilectl export "
           "<slug> --script`", placed == expected and bool(expected),
           f"{len(placed)} vs {len(expected)} bytes")


def assert_smoke(root: Path, user: str) -> None:
    home = root / "home" / user
    smoke = home / "workspace/smoke.txt"
    expect("one shell command + one file write", smoke.is_file()
           and "odile-smoke-ok" in smoke.read_text(encoding="utf-8"),
           f"{smoke} = {smoke.read_text().splitlines()[:2] if smoke.is_file() else 'missing'}")
    cron_log = home / "workspace/cron-proof.log"
    expect("one cron job ran", cron_log.is_file()
           and "odile-cron-ok" in cron_log.read_text(encoding="utf-8"),
           f"{cron_log} = {cron_log.read_text().strip().splitlines()[-1] if cron_log.is_file() else 'missing'}")
    key_env = root / "etc/odile/key.env"
    key_text = key_env.read_text(encoding="utf-8") if key_env.is_file() else ""
    key_name = "HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY"
    expect("the per-user key is at /etc/odile/key.env, 0600",
           key_env.is_file() and (key_env.stat().st_mode & 0o777) == 0o600
           and any(line.startswith(key_name + "=") and len(line) > len(key_name) + 16
                   for line in key_text.splitlines()),
           f"{key_env} mode {oct(key_env.stat().st_mode & 0o777) if key_env.is_file() else 'missing'}")
    pid_file = root / "var/lib/odile/agent.pid"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = 0
    environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0") if pid else []
    expect("the gateway's process environment holds the key (the unit's EnvironmentFile)",
           any(line.startswith(key_name.encode() + b"=") and len(line) > len(key_name) + 16
               for line in environ),
           f"/proc/{pid}/environ")
    profile_env = home / ".hermes/.env"
    profile_text = profile_env.read_text(encoding="utf-8") if profile_env.is_file() else ""
    expect("the profile .env carries no inference key", key_name not in profile_text)
    recovery = root / "etc/odile/recovery.txt"
    text = recovery.read_text(encoding="utf-8") if recovery.is_file() else ""
    expect("the recovery file carries the backup password", f"Backup password:     {BACKUP}" in text)
    expect("the recovery file carries the fingerprint placeholder",
           "Certificate SHA-256: (pending" in text)
    expect("the recovery file is browser-only (no scp, shred or root@)",
           re.search(r"(?i)scp|shred|root@", text) is None)
    expect("the recovery file names the agent as the way to see it again",
           "ask your agent to show it again" in text.lower())



def post_seed(seed_port: int, token: str, instance: str) -> tuple[int, dict]:
    data = json.dumps({"token": token, "instance_id": instance}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{seed_port}/seed", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def assert_seed_pairs(db: Path, config: Path, seed_port: int, token: str) -> None:
    print("== seed exchange refusals")
    status, body = post_seed(seed_port, token, INSTANCE)
    expect("a replayed token is refused", status == 410 and body.get("reason") == "used",
           f"{status} {body}")
    os.environ["ODILE_DB"] = str(db)
    os.environ["ODILE_SEED_CONFIG"] = str(config)
    os.environ["ODILE_SEED_SIGNING_KEY"] = str(SEED_SIGNING_KEY)
    issued = run(["/usr/bin/python3", str(SEED_PY), "token", "issue", "--tenant", SLUG],
                 env=os.environ.copy(), cwd=REPO)
    match = re.search(r"\b([0-9a-f]{64})\b", issued.stdout)
    if not match:
        raise SystemExit(f"error: could not issue the expiry token: {issued.stdout!r}")
    expired = match.group(1)
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE seed_tokens SET created_at='2026-11-05T00:00:00Z', expires_at='2026-11-05T12:00:00Z' "
        "WHERE token_hash = ?",
        (__import__("hashlib").sha256(expired.encode()).hexdigest(),),
    )
    con.commit()
    con.close()
    status, body = post_seed(seed_port, expired, INSTANCE)
    expect("an expired token is refused", status == 410 and body.get("reason") == "expired",
           f"{status} {body}")


def stop_gateway(root: Path, user: str) -> None:
    launcher = root / "home" / user / ".hermes/hermes-agent/.hermes/bin/hermes"
    if not launcher.is_file():
        return
    home = root / "home" / user
    result = run(["sudo", "-u", user, "-H", "env", f"HOME={home}",
                  f"HERMES_HOME={home}/.hermes", str(launcher), "gateway", "stop"])
    if result.returncode == 0:
        print(f"stopped      agent gateway for {user}")
    _stop_pid(root / "var/lib/odile/agent.pid")


def _stop_pid(path: Path) -> None:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        os.kill(pid, 15)
        print(f"stopped      pid {pid} ({path})")
    except (OSError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drive the ODL-203 tenant simulation.")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--seed-port", type=int, default=DEFAULT_SEED_PORT)
    parser.add_argument("--reset", action="store_true", help="delete the scratch root and user")
    parser.add_argument("--apply", action="store_true", help="apply --reset (default: dry run)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if root == Path("/") or str(root) in {"/root", "/home", "/var"}:
        print(f"error: refusing --root {root}", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("error: the harness needs root (it creates the user and runs the seed service)", file=sys.stderr)
        return 2
    if not (INSTALLER.is_file() and INSTALLER_SIG.is_file() and PUBKEY.is_file()):
        print("error: installer, signature or public key is missing", file=sys.stderr)
        return 2
    for key in (SIGNING_KEY, SEED_SIGNING_KEY):
        if not key.is_file():
            print(f"error: {key} is missing", file=sys.stderr)
            return 2

    if args.reset:
        reset(root, args.user, args.apply)
        if not args.apply:
            return 0

    ensure_user(root, args.user)
    bootstrap, evidence = ensure_scratch(root, args.user)
    token, db, config = seed_setup(root, SLUG, INSTANCE, args.seed_port)
    kit = write_kit(bootstrap, args.user, token, SLUG, INSTANCE, bootstrap / "odile-signing-key.pub")
    seed_process = start_seed(db, config, args.seed_port)
    try:
        first = run_installer(root, args.user, args.port,
                              args.seed_port, kit, evidence / "run1.log", "1")
        expect("the first run exits 0", first.returncode == 0,
               first.stderr.strip().splitlines()[-1] if first.stderr.strip() else "")
        if first.returncode != 0:
            return 1
        expect("the first run provisioned", "tenant: provisioned" in first.stdout)
        module = load_installer()
        assert_login(module, root, args.user, args.port)
        assert_export_program(root)
        assert_smoke(root, args.user)
        assert_seed_pairs(db, config, args.seed_port, token)

        second = run_installer(root, args.user, args.port,
                               args.seed_port, kit, evidence / "run2.log", "2")
        expect("the second run exits 0", second.returncode == 0,
               second.stderr.strip().splitlines()[-1] if second.stderr.strip() else "")
        expect("the second run prints `tenant: same`", "tenant: same" in second.stdout)
        writes = re.findall(r"^(?:create|write|build|install|start|restart|rewrite|chmod|enable)\b",
                            second.stdout, re.M)
        expect("the second run changes nothing", not writes, f"{len(writes)} write line(s)")
    finally:
        stop(seed_process)
        stop_gateway(root, args.user)

    print()
    if FAIL == 0:
        print(f"simulation: PASS ({PASS} passed, 0 failed)")
        print(f"evidence:   {evidence}")
        return 0
    print(f"simulation: FAIL ({PASS} passed, {FAIL} failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
