#!/usr/bin/python3
"""Reconcile Dovecot on this machine with deploy/dovecot/.

    ./deploy/dovecot/apply.py                  dry run: say what would change
    ./deploy/dovecot/apply.py --apply          write the config, reconcile the service
    ./deploy/dovecot/apply.py --check          exit 1 if anything differs (monitoring)
    ./deploy/dovecot/apply.py --self-test      offline checks for the fresh-host path
    ./deploy/dovecot/apply.py --set-password   mint/rotate the one mailbox password

The committed `dovecot.conf` is the source of truth: one IMAPS listener on 993,
no plaintext listener at all, the Maildir at /var/mail/help/Maildir read in
place, and one passwd-file account. This script copies it to /etc/dovecot,
fixes the modes the auth path depends on, and proves the listener set is exactly
`993` — not `993 and 143`, which is what the packaged defaults give.

Credential handling is deliberately split from configuration:

  * `/etc/dovecot/users` (0600 root) is the IMAP account — one line, a
    `{SHA512-CRYPT}` hash and the help uid/gid/home. It is *state*, like
    /etc/sasldb2, and never appears in git.
  * `/etc/sasldb2` (0640 root:postfix) is the same password's hash for
    submission on 587, created with `saslpasswd2` under the same account name,
    so Thunderbird has one username and one secret for IMAP and SMTP.
  * `/root/.odile/mailbox-help-password.txt` (0600 root) is the one plaintext
    copy, written for the handover to Savio. `--set-password` writes all three
    from the same generated secret, hashes it through `doveadm pw` and
    `saslpasswd2 -p` on stdin, and never prints it or puts it on a command
    line (argv is visible in the process list).

The packaged `dovecot.socket` is masked: it binds 143 *and* 993, so leaving it
enabled would silently reintroduce the plaintext port. The daemon
(`dovecot.service`, `-F`) owns the listener that `--check` inspects.

Exit codes: 0 clean or applied, 1 drift or a failed step, 2 usage error.
"""

from __future__ import annotations

import argparse
import grp
import os
import pwd
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = Path("/etc/dovecot/dovecot.conf")
USERS = Path("/etc/dovecot/users")
HOME = Path("/var/mail/help")
MAILDIR = HOME / "Maildir"
MAILBOX_USER = "help"
ACCOUNT = "help@odile.fyi"
HANDOVER = Path("/root/.odile/mailbox-help-password.txt")
SASLDB = Path("/etc/sasldb2")
# One username for IMAP and SMTP. `saslpasswd2 -u odile.fyi help` stores
# `help@odile.fyi`; the card's `-u mail.odile.fyi` would store a second name.
SASL_REALM = "odile.fyi"
SASL_USER = "help"
HASH_PREFIX = "{SHA512-CRYPT}"
HASH_RE = re.compile(r"^\{SHA512-CRYPT\}\$6\$[^:\s]{1,200}$")

FILES = [
    # source, destination, owner, group, mode
    ("dovecot.conf", CONFIG, "root", "root", 0o644),
]

# The properties the diff exists to keep. Parsed from the committed file by the
# self-test, so a future edit that re-opens 143 or moves the Maildir fails
# offline instead of on the live host.
REQUIRED = [
    ((), "protocols", "imap"),
    ((), "ssl", "required"),
    ((), "auth_allow_cleartext", "no"),
    ((), "mail_driver", "maildir"),
    ((), "mail_path", str(MAILDIR)),
    ((), "mail_uid", MAILBOX_USER),
    ((), "mail_gid", MAILBOX_USER),
    ((), "ssl_server_cert_file", "/etc/ssl/certs/mail.odile.fyi.crt"),
    ((), "ssl_server_key_file", "/etc/ssl/private/mail.odile.fyi.key"),
    (("service imap-login", "inet_listener imap"), "port", "0"),
    (("service imap-login", "inet_listener imaps"), "port", "993"),
    (("passdb passwd-file",), "passwd_file_path", str(USERS)),
    (("passdb passwd-file",), "default_password_scheme", "SHA512-CRYPT"),
    (("passdb passwd-file",), "use_worker", "yes"),
    (("userdb passwd-file",), "passwd_file_path", str(USERS)),
    (("userdb passwd-file",), "use_worker", "yes"),
]


def run(command: list[str], input_text: str | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False,
            input=input_text,
        )
    except FileNotFoundError:
        return 127, f"{command[0]}: command not found"
    return result.returncode, (result.stdout + result.stderr).strip()


def atomic_write(path: Path, data: bytes, mode: int, uid: int = 0, gid: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, uid, gid)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def parse_settings(path: Path) -> dict[tuple[tuple[str, ...], str], str]:
    """(section path, setting) -> value, from a Dovecot-style config file.

    Enough of the grammar to make the self-test assert on the *committed* file:
    `section {` opens a level, `}` closes it, everything else is `name = value`.
    A malformed line is an error, not something silently skipped.
    """
    settings: dict[tuple[tuple[str, ...], str], str] = {}
    stack: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.endswith("{"):
            name = line[:-1].strip()
            if not name:
                raise ValueError(f"{path}:{number}: section without a name")
            stack.append(name)
            continue
        if line == "}":
            if not stack:
                raise ValueError(f"{path}:{number}: unmatched closing brace")
            stack.pop()
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{number}: not `name = value`: {raw!r}")
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            raise ValueError(f"{path}:{number}: empty setting name")
        key = (tuple(stack), name)
        if key in settings:
            raise ValueError(f"{path}:{number}: {name} set twice in {stack or ['top level']}")
        settings[key] = value
    if stack:
        raise ValueError(f"{path}: unclosed section {stack[-1]!r}")
    return settings


def config_problems(path: Path | None = None) -> list[str]:
    source = path or CONFIG
    if not source.exists():
        return [f"{source}: missing"]
    problems: list[str] = []
    wanted = (HERE / "dovecot.conf").read_bytes()
    same_bytes = True
    if source == CONFIG:
        if source.read_bytes() != wanted:
            same_bytes = False
            problems.append(f"{source}: content differs from deploy/dovecot/dovecot.conf")
        info = source.stat()
        if stat.S_IMODE(info.st_mode) != 0o644:
            problems.append(
                f"{source}: mode {oct(stat.S_IMODE(info.st_mode))[2:]}, want 644"
            )
        if (info.st_uid, info.st_gid) != (0, 0):
            problems.append(f"{source}: owner {info.st_uid}:{info.st_gid}, want root:root")
    # The committed grammar is what the assertions describe; the packaged file
    # in /etc/dovecot uses `!include`, which is valid but is not our shape.
    try:
        settings = parse_settings(source if same_bytes else HERE / "dovecot.conf")
        for section, name, value in REQUIRED:
            got = settings.get((section, name))
            if got != value:
                where = ".".join(section) or "top level"
                problems.append(f"{source}: {where}.{name} = {got!r}, want {value!r}")
        ports = {value for (_, name), value in settings.items() if name == "port"}
        extra = ports - {"0", "993"}
        if extra:
            problems.append(f"{source}: listener port(s) {sorted(extra)} are not 0 or 993")
    except ValueError as error:
        problems.append(str(error))
    code, output = run(["doveconf", "-c", str(source), "-n"])
    if code != 0:
        problems.append(f"{source}: doveconf rejects it: {output}")
    return problems


def parse_users(path: Path) -> list[list[str]]:
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            entries.append(line.split(":"))
    return entries


def entry_problems(fields: list[str], uid: int, gid: int) -> list[str]:
    """The rules for the one credential line, independent of /etc/dovecot/users.

    Split out so `--self-test` can exercise a good line and the failure shapes
    (plaintext, wrong scheme, a second account, wrong owner) without touching
    root-owned state.
    """
    if len(fields) < 7:
        return [f"line has {len(fields)} colon-separated fields, want 7 (user:hash:uid:gid:gecos:home:shell)"]
    problems: list[str] = []
    user, password, owner, group, _gecos, home, shell = fields[:7]
    if user != ACCOUNT:
        problems.append(f"account {user!r}, want {ACCOUNT!r}")
    if not HASH_RE.match(password):
        if password.startswith(HASH_PREFIX):
            problems.append("password is a {SHA512-CRYPT} line that is not a $6$ crypt hash")
        elif password.startswith("{"):
            problems.append(f"password uses {password.split('$', 1)[0]}, want {HASH_PREFIX}")
        else:
            problems.append("password has no {SCHEME} prefix — looks like plaintext")
    if (owner, group) != (str(uid), str(gid)):
        problems.append(f"uid:gid {owner}:{group}, want {uid}:{gid} ({MAILBOX_USER})")
    if home != str(HOME):
        problems.append(f"home {home!r}, want {HOME!r}")
    if shell not in ("", "/usr/sbin/nologin", "/bin/false"):
        problems.append(f"shell {shell!r}, want nologin")
    return problems


def users_problems(path: Path | None = None) -> list[str]:
    target = path or USERS
    if not target.exists():
        return [f"{target}: missing — run --set-password to mint the account"]
    problems: list[str] = []
    info = target.stat()
    if stat.S_IMODE(info.st_mode) != 0o600:
        problems.append(f"{target}: mode {oct(stat.S_IMODE(info.st_mode))[2:]}, want 600")
    if (info.st_uid, info.st_gid) != (0, 0):
        problems.append(f"{target}: owner {info.st_uid}:{info.st_gid}, want root:root")
    entries = parse_users(target)
    if len(entries) != 1:
        problems.append(f"{target}: holds {len(entries)} account(s), want exactly 1 ({ACCOUNT})")
    try:
        account = pwd.getpwnam(MAILBOX_USER)
    except KeyError:
        return problems + [f"system user {MAILBOX_USER!r} is missing (deploy/postfix/apply.py --apply)"]
    for fields in entries:
        problems.extend(f"{target}: {problem}" for problem in
                        entry_problems(fields, account.pw_uid, account.pw_gid))
    return problems


def maildir_problems() -> list[str]:
    try:
        account = pwd.getpwnam(MAILBOX_USER)
    except KeyError:
        return [f"system user {MAILBOX_USER!r} is missing"]
    problems: list[str] = []
    if Path(account.pw_dir) != HOME:
        problems.append(f"{MAILBOX_USER} home is {account.pw_dir}, want {HOME}")
    for sub in ("new", "cur", "tmp"):
        if not (MAILDIR / sub).is_dir():
            problems.append(f"{MAILDIR / sub}: missing")
    if MAILDIR.is_dir():
        info = MAILDIR.stat()
        if stat.S_IMODE(info.st_mode) != 0o700:
            problems.append(f"{MAILDIR}: mode {oct(stat.S_IMODE(info.st_mode))[2:]}, want 700")
        if (info.st_uid, info.st_gid) != (account.pw_uid, account.pw_gid):
            problems.append(
                f"{MAILDIR}: owner {info.st_uid}:{info.st_gid}, want {MAILBOX_USER}"
            )
    return problems


def listeners() -> dict[str, list[str]]:
    """port -> addresses, from `ss -lnt` (no -p: the check needs no names)."""
    code, output = run(["ss", "-lnt"])
    found: dict[str, list[str]] = {}
    if code != 0:
        return found
    for line in output.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        address = fields[3]
        port = address.rsplit(":", 1)[-1]
        found.setdefault(port, []).append(address.rsplit(":", 1)[0])
    return found


def service_problems() -> list[str]:
    problems: list[str] = []
    code, output = run(["systemctl", "is-active", "dovecot"])
    if output != "active":
        problems.append(f"dovecot.service is {output or 'unknown'}, want active")
    code, output = run(["systemctl", "is-enabled", "dovecot"])
    if output != "enabled":
        problems.append(f"dovecot.service is {output or 'unknown'}, want enabled")
    code, output = run(["systemctl", "is-enabled", "dovecot.socket"])
    if output != "masked":
        problems.append(
            f"dovecot.socket is {output or 'unknown'}, want masked "
            "(it binds 143 as well as 993)"
        )
    ports = listeners()
    if "993" not in ports:
        problems.append("nothing listens on 993")
    if "143" in ports:
        problems.append(f"plaintext IMAP listens on 143 ({', '.join(ports['143'])})")
    return problems


def drift() -> list[str]:
    return config_problems() + users_problems() + maildir_problems() + service_problems()


def ensure_maildir() -> list[str]:
    try:
        account = pwd.getpwnam(MAILBOX_USER)
    except KeyError:
        return []
    actions: list[str] = []
    for sub in ("new", "cur", "tmp"):
        directory = MAILDIR / sub
        if not directory.is_dir():
            directory.mkdir(parents=True, exist_ok=True)
            actions.append(str(directory))
        os.chown(directory, account.pw_uid, account.pw_gid)
    if MAILDIR.is_dir():
        os.chmod(MAILDIR, 0o700)
        os.chown(MAILDIR, account.pw_uid, account.pw_gid)
    return actions


def write_config() -> bool:
    source, destination, owner, group, mode = FILES[0]
    wanted = (HERE / source).read_bytes()
    changed = not destination.exists() or destination.read_bytes() != wanted
    atomic_write(destination, wanted, mode,
                 pwd.getpwnam(owner).pw_uid, grp.getgrnam(group).gr_gid)
    return changed


def wait_for_993(timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if "993" in listeners() and "143" not in listeners():
            return True
        time.sleep(0.1)
    return False


def hash_password(password: str) -> str:
    code, output = run(["doveadm", "pw", "-s", "SHA512-CRYPT"],
                       input_text=f"{password}\n{password}\n")
    if code != 0:
        raise SystemExit(f"doveadm pw failed: {output}")
    line = output.strip().splitlines()[-1].strip()
    if not HASH_RE.match(line):
        raise SystemExit(f"doveadm pw returned an unexpected line: {line!r}")
    return line


def set_password(password: str) -> int:
    account = pwd.getpwnam(MAILBOX_USER)
    hashed = hash_password(password)
    atomic_write(
        USERS, f"{ACCOUNT}:{hashed}:{account.pw_uid}:{account.pw_gid}"
               f"::{HOME}::\n".encode("utf-8"),
        0o600,
    )
    print(f"wrote  {USERS} (0600 root, {HASH_PREFIX}, one line)")

    atomic_write(HANDOVER, f"{password}\n".encode("utf-8"), 0o600)
    print(f"wrote  {HANDOVER} (0600 root — the handover copy, not printed here)")

    code, output = run(["saslpasswd2", "-c", "-p", "-u", SASL_REALM, SASL_USER],
                       input_text=f"{password}\n")
    if code != 0:
        print(f"saslpasswd2 failed: {output}")
        return 1
    if SASLDB.exists():
        os.chmod(SASLDB, 0o640)
        os.chown(SASLDB, 0, grp.getgrnam("postfix").gr_gid)
    print(f"wrote  {SASLDB} ({ACCOUNT} for submission 587, 0640 root:postfix)")

    if run(["systemctl", "is-active", "dovecot"])[1] == "active":
        code, output = run(["systemctl", "reload", "dovecot"])
        if code != 0:
            print(f"dovecot reload failed: {output}")
            return 1
    code, output = run(["doveadm", "auth", "test", ACCOUNT], input_text=f"{password}\n")
    if code != 0 or "auth succeeded" not in output:
        print(f"doveadm auth test failed: {output}")
        return 1
    print(f"ok     doveadm auth test {ACCOUNT}: auth succeeded")
    return 0


def self_test() -> int:
    """Offline checks for the fresh-host path. No network, no /etc writes."""
    failures = 0
    count = 0

    def check(name: str, got, want) -> None:
        nonlocal failures, count
        count += 1
        if got == want:
            print(f"ok    {name}")
        else:
            failures += 1
            print(f"FAIL  {name}: got {got!r}, want {want!r}")

    settings = parse_settings(HERE / "dovecot.conf")
    for section, name, value in REQUIRED:
        where = ".".join(section) or "top level"
        check(f"{where}.{name} = {value}", settings.get((section, name)), value)

    ports = sorted(value for (_, name), value in settings.items() if name == "port")
    check("exactly the two listener ports are declared", ports, ["0", "993"])
    check("no plaintext IMAP port anywhere", "143" in ports, False)
    check("the config is the only tracked file the applier copies",
          [entry[0] for entry in FILES], ["dovecot.conf"])

    # One username for IMAP and SMTP: the realm the SASL account is created in
    # must produce the same string the passwd-file and Thunderbird use.
    check("the SASL account name equals the IMAP login", f"{SASL_USER}@{SASL_REALM}", ACCOUNT)

    uid, gid = os.getuid(), os.getgid()
    good = [ACCOUNT, "{SHA512-CRYPT}$6$salt$" + "a" * 86, str(uid), str(gid), "",
            str(HOME), ""]
    check("a well-formed credential line passes", entry_problems(good, uid, gid), [])
    plaintext = list(good)
    plaintext[1] = "hunter2"
    check("a plaintext password is rejected",
          any("plaintext" in problem for problem in entry_problems(plaintext, uid, gid)), True)
    wrong_scheme = list(good)
    wrong_scheme[1] = "{PLAIN}hunter2"
    check("a {PLAIN} password is rejected",
          any("want {SHA512-CRYPT}" in problem for problem in
              entry_problems(wrong_scheme, uid, gid)), True)
    wrong_user = list(good)
    wrong_user[0] = "someone@odile.fyi"
    check("a second account is rejected",
          entry_problems(wrong_user, uid, gid), [f"account 'someone@odile.fyi', want {ACCOUNT!r}"])
    wrong_home = list(good)
    wrong_home[5] = "/root"
    check("a credential pointing at another home is rejected",
          any("home" in problem for problem in entry_problems(wrong_home, uid, gid)), True)
    wrong_owner = list(good)
    wrong_owner[2] = str(uid + 1)
    check("a credential for another uid is rejected",
          any("uid:gid" in problem for problem in entry_problems(wrong_owner, uid, gid)), True)

    hashed = hash_password("self-test-only")
    check("doveadm pw produces a {SHA512-CRYPT} line", hashed.startswith(HASH_PREFIX + "$6$"), True)
    check("the hash is not the password", "self-test-only" in hashed, False)
    check("the generated hash satisfies the users-file rule",
          entry_problems([ACCOUNT, hashed, str(uid), str(gid), "", str(HOME), ""], uid, gid), [])

    password = secrets.token_hex(16)
    check("a generated password is 128 bits of hex", bool(re.fullmatch(r"[0-9a-f]{32}", password)), True)

    print(f"\n{count} checks, {failures} failed")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile Dovecot on this machine with deploy/dovecot/."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="write the changes")
    group.add_argument("--check", action="store_true", help="exit 1 on any drift")
    group.add_argument("--self-test", action="store_true", help="offline checks")
    group.add_argument("--set-password", action="store_true",
                       help="mint or rotate the mailbox password (generated, never printed)")
    group.add_argument("--password-stdin", action="store_true",
                       help="with --set-password: read the secret from stdin instead of generating")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.password_stdin and not args.set_password:
        parser.error("--password-stdin is only meaningful with --set-password")

    if args.set_password:
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
            if not password:
                parser.error("--password-stdin read an empty line")
        else:
            password = secrets.token_hex(16)
        return set_password(password)

    if args.check:
        problems = drift()
        for problem in problems:
            print(f"DRIFT  {problem}")
        if problems:
            print(f"\n{len(problems)} difference(s) from deploy/dovecot")
            return 1
        print("clean: /etc/dovecot matches deploy/dovecot, one hashed account, "
              "the Maildir is in place, and the only listener is 993")
        return 0

    problems = drift()
    if not args.apply:
        if not problems:
            print("nothing to do; /etc/dovecot matches deploy/dovecot")
            return 0
        for problem in problems:
            print(f"would fix  {problem}")
        print(f"\ndry run: {len(problems)} change(s); pass --apply to write")
        return 0

    # --apply: validate the committed file, then write it, then the state it
    # needs, then reconcile the listener once.
    for problem in config_problems(HERE / "dovecot.conf"):
        print(f"invalid  {problem}")
        return 1

    changed = write_config()
    print(f"{'wrote' if changed else 'ok':<6} {CONFIG}")

    if USERS.exists():
        os.chmod(USERS, 0o600)
        os.chown(USERS, 0, 0)
    else:
        print(f"warn   {USERS} is missing: run --set-password before clients can log in")

    for action in ensure_maildir():
        print(f"wrote  {action}")

    # The packaged socket binds 143 and 993; masking it is what keeps the
    # listener set to the daemon's own 993.
    run(["systemctl", "mask", "dovecot.socket"])
    run(["systemctl", "unmask", "dovecot"])
    run(["systemctl", "enable", "dovecot"])
    if changed or run(["systemctl", "is-active", "dovecot"])[1] != "active":
        code, output = run(["systemctl", "restart", "dovecot"])
        if code != 0:
            print(f"dovecot restart failed:\n{output}")
            return 1
        print("restarted dovecot")
        if not wait_for_993():
            print("dovecot is not listening on 993 (or 143 is still open)")
            return 1

    left = drift()
    # A missing users file is state --set-password owns, like /etc/sasldb2 in
    # deploy/postfix/apply.py: warn, do not fail the config reconciliation.
    left = [problem for problem in left
            if not problem.startswith(f"{USERS}: missing")]
    if left:
        for problem in left:
            print(f"still  {problem}")
        return 1
    if not changed:
        print("nothing to do; /etc/dovecot matches deploy/dovecot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
