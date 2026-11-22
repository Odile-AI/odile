#!/usr/bin/python3
"""Reconcile Postfix on this machine with the files committed in deploy/postfix/.

    ./deploy/postfix/apply.py             dry run: say what would change
    ./deploy/postfix/apply.py --apply     write, postmap, create mailboxes, restart
    ./deploy/postfix/apply.py --check     exit 1 if anything differs (monitoring)
    ./deploy/postfix/apply.py --self-test offline checks for the fresh-host path

The committed files are the source of truth; /etc/postfix is a copy of them.
Comparing is byte-for-byte plus owner and mode, so "a second run changes
nothing" is a fact and not a hope.

Two pieces of state are *derived* from the committed files rather than
committed themselves, and a fresh host is exactly where they go missing:

  * `/etc/postfix/virtual.db`, built by `postmap` from `virtual`. Without it
    help@odile.fyi and every VERP bounce address are dead while the config
    still looks right, so the database is checked by asking it the questions
    Postfix will ask, not by trusting its presence;
  * the two local mailboxes — `dsn` for VERP bounces and DMARC reports,
    `help` for the support spool — each a nologin user with a Maildir. No
    reader consumes the support spool yet.

Submission AUTH is Dovecot's, not Postfix's: Postfix connects to
/var/spool/postfix/private/auth, and the accounts are the {SHA512-CRYPT} lines
in /etc/dovecot/users. This script verifies that socket and the hash file, and
retires the old Cyrus /etc/sasldb2 (which held the same passwords in cleartext
for PLAIN/LOGIN) if it is still present.

Exit codes: 0 clean or applied, 1 drift (with --check) or a failed step,
2 usage error.
"""

from __future__ import annotations

import argparse
import grp
import os
import pwd
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple, Sequence

HERE = Path(__file__).resolve().parent
POSTFIX = Path("/etc/postfix")
VIRTUAL = POSTFIX / "virtual"
VIRTUAL_DB = POSTFIX / "virtual.db"
SASLDB = Path("/etc/sasldb2")
DOVECOT_USERS = Path("/etc/dovecot/users")
DOVECOT_AUTH_SOCKET = Path("/var/spool/postfix/private/auth")
# Submission AUTH must always have these two: the support mailbox (one login
# for IMAP and 587) and the test tenant scripts/send_test.py submits with.
DOVECOT_ACCOUNTS = ("help@odile.fyi", "odile-test@mail.odile.fyi")


class Mailbox(NamedTuple):
    user: str
    home: Path
    reason: str


# Every address under mail.odile.fyi that is not help@ resolves here (see
# deploy/postfix/virtual): VERP bounces, DMARC reports, delay notices.
MAILBOXES = (
    Mailbox("dsn", Path("/var/mail/dsn"),
            "every VERP sender and every unmatched address under mail.odile.fyi"),
    Mailbox("help", Path("/var/mail/help"),
            "the support spool every help@, abuse@ and postmaster@ message lands in; "
            "no reader consumes it yet"),
)

# source (relative to this directory), destination, owner, group, mode
FILES = [
    ("main.cf", POSTFIX / "main.cf", "root", "root", 0o644),
    ("master.cf", POSTFIX / "master.cf", "root", "root", 0o644),
    ("virtual", VIRTUAL, "root", "root", 0o644),
    ("sender_login.pcre", POSTFIX / "sender_login.pcre", "root", "root", 0o644),
    ("sender_canonical.pcre", POSTFIX / "sender_canonical.pcre", "root", "root", 0o644),
]

# Files a previous layout deployed that the committed config no longer uses.
# The Cyrus app config is dead once smtpd_sasl_type = dovecot; deleting it is
# what keeps an operator from editing a file Postfix never reads.
REMOVED = [
    POSTFIX / "sasl" / "smtpd.conf",
]


def ids(owner: str, group: str) -> tuple[int, int]:
    return pwd.getpwnam(owner).pw_uid, grp.getgrnam(group).gr_gid


def first_difference(wanted: bytes, live: bytes) -> str:
    want = wanted.decode("utf-8", "replace").splitlines()
    have = live.decode("utf-8", "replace").splitlines()
    for number, (left, right) in enumerate(zip(want, have), start=1):
        if left != right:
            return f"line {number}: committed {left!r}, live {right!r}"
    if len(want) > len(have):
        return f"live file ends at line {len(have)}; committed has {len(want)}"
    return f"live file has {len(have)} lines; committed has {len(want)}"


def inspect(entry) -> list[str]:
    """Everything that differs for one file, as human-readable lines."""
    source, destination, owner, group, mode = entry
    problems: list[str] = []
    wanted = (HERE / source).read_bytes()
    if not destination.exists():
        return [f"{destination}: missing"]
    live = destination.read_bytes()
    if wanted != live:
        problems.append(f"{destination}: content differs — {first_difference(wanted, live)}")
    info = destination.stat()
    if stat.S_IMODE(info.st_mode) != mode:
        problems.append(
            f"{destination}: mode {oct(stat.S_IMODE(info.st_mode))[2:]}, want {oct(mode)[2:]}"
        )
    uid, gid = ids(owner, group)
    if (info.st_uid, info.st_gid) != (uid, gid):
        problems.append(f"{destination}: owner {info.st_uid}:{info.st_gid}, want {owner}:{group}")
    return problems


def write(entry) -> bool:
    """Copy one file into place atomically. Returns True if content changed."""
    source, destination, owner, group, mode = entry
    wanted = (HERE / source).read_bytes()
    changed = not destination.exists() or destination.read_bytes() != wanted
    destination.parent.mkdir(parents=True, exist_ok=True)
    uid, gid = ids(owner, group)
    if changed:
        handle = tempfile.NamedTemporaryFile(dir=destination.parent, delete=False)
        try:
            handle.write(wanted)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.chmod(handle.name, mode)
        os.chown(handle.name, uid, gid)
        os.replace(handle.name, destination)
    else:
        os.chmod(destination, mode)
        os.chown(destination, uid, gid)
    return changed


def mailbox_problems(boxes: Sequence[Mailbox] | None = None) -> list[str]:
    """Each mailbox needs a local user, the committed home, and a Maildir."""
    problems: list[str] = []
    for box in MAILBOXES if boxes is None else boxes:
        try:
            account = pwd.getpwnam(box.user)
        except KeyError:
            problems.append(
                f"system user {box.user!r} is missing: {box.reason} has nowhere to land"
            )
            continue
        if Path(account.pw_dir) != box.home:
            problems.append(f"{box.user} home is {account.pw_dir}, want {box.home}")
        maildir = box.home / "Maildir"
        for sub in ("new", "cur", "tmp"):
            directory = maildir / sub
            if not directory.is_dir():
                problems.append(f"{directory}: missing")
        if maildir.is_dir():
            info = maildir.stat()
            if stat.S_IMODE(info.st_mode) != 0o700:
                problems.append(
                    f"{maildir}: mode {oct(stat.S_IMODE(info.st_mode))[2:]}, want 700"
                )
            if (info.st_uid, info.st_gid) != (account.pw_uid, account.pw_gid):
                problems.append(
                    f"{maildir}: owner {info.st_uid}:{info.st_gid}, "
                    f"want {box.user}:{box.user}"
                )
    return problems


def ensure_mailbox(boxes: Sequence[Mailbox] | None = None) -> list[str]:
    """Create whatever mailbox_problems() reports. Returns what it did."""
    actions: list[str] = []
    for box in MAILBOXES if boxes is None else boxes:
        try:
            account = pwd.getpwnam(box.user)
        except KeyError:
            subprocess.run(
                ["useradd", "--create-home", "--home-dir", str(box.home),
                 "--shell", "/usr/sbin/nologin", box.user],
                check=True,
            )
            account = pwd.getpwnam(box.user)
            actions.append(f"{box.home} (system user {box.user})")
        maildir = box.home / "Maildir"
        for sub in ("new", "cur", "tmp"):
            directory = maildir / sub
            if not directory.is_dir():
                directory.mkdir(parents=True, exist_ok=True)
                actions.append(str(directory))
            os.chown(directory, account.pw_uid, account.pw_gid)
        if maildir.is_dir():
            info = maildir.stat()
            if (stat.S_IMODE(info.st_mode) != 0o700
                    or (info.st_uid, info.st_gid) != (account.pw_uid, account.pw_gid)):
                actions.append(f"{maildir} (mode 700, owner {box.user})")
        os.chmod(maildir, 0o700)
        os.chown(maildir, account.pw_uid, account.pw_gid)
    return actions


def virtual_entries(path: Path | None = None) -> dict[str, str]:
    """The alias map as Postfix will read it: key → destination, comments gone."""
    entries: dict[str, str] = {}
    text = (path or VIRTUAL).read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        fields = line.split()
        if len(fields) >= 2:
            entries[fields[0]] = " ".join(fields[1:])
    return entries


def pcre_entries(path: Path) -> dict[str, str]:
    """A pcre table as Postfix reads it: pattern (without /…/) -> result."""
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        fields = line.split()
        if len(fields) >= 2:
            entries[fields[0].strip("/")] = " ".join(fields[1:])
    return entries


def postmap_entries(path: Path | None = None) -> tuple[dict[str, str], str]:
    """List the live database itself with `postmap -s`. Returns (map, error).

    `postmap -s` prints one `key<TAB>value` line per element, which is the
    only way to see an entry the committed source no longer has: `-q` for the
    committed keys cannot answer a question nobody asks.
    """
    target = path or VIRTUAL
    code, output = run(["postmap", "-s", str(target)])
    if code != 0:
        return {}, output or f"postmap -s {target} exited {code}"
    entries: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split(None, 1)
        if len(fields) == 2:
            entries[fields[0]] = fields[1].strip()
    return entries, ""


def virtual_db_problems() -> list[str]:
    """A missing, stale or over-full alias database breaks help@ and bounces.

    Presence is not enough and neither is answering the committed keys: a
    database that still holds an alias removed from `virtual` answers every
    remaining question correctly while routing mail to a mailbox the source no
    longer names. So the whole live entry set (`postmap -s`) is compared with
    the committed file, and a database older than its source is drift because
    Postfix logs exactly that warning on every delivery.
    """
    if not VIRTUAL.exists():
        return []  # inspect() reports the missing source file
    if not VIRTUAL_DB.exists():
        return [
            f"{VIRTUAL_DB}: missing — run postmap {VIRTUAL} (help@ and every "
            "bounce address are dead without it)"
        ]
    problems: list[str] = []
    info = VIRTUAL_DB.stat()
    if stat.S_IMODE(info.st_mode) != 0o644:
        problems.append(
            f"{VIRTUAL_DB}: mode {oct(stat.S_IMODE(info.st_mode))[2:]}, want 644"
        )
    if (info.st_uid, info.st_gid) != (0, 0):
        problems.append(f"{VIRTUAL_DB}: owner {info.st_uid}:{info.st_gid}, want root:root")
    if info.st_mtime < VIRTUAL.stat().st_mtime:
        problems.append(
            f"{VIRTUAL_DB}: older than {VIRTUAL} (Postfix warns on every delivery; "
            "re-run --apply)"
        )
    live, error = postmap_entries()
    if error:
        problems.append(f"{VIRTUAL_DB}: cannot list (postmap -s: {error})")
        return problems
    wanted = virtual_entries()
    extra = sorted(set(live) - set(wanted))
    missing = sorted(set(wanted) - set(live))
    if extra:
        problems.append(
            f"{VIRTUAL_DB}: holds {len(extra)} alias(es) not in {VIRTUAL}: "
            f"{', '.join(extra)} (stale; re-run --apply)"
        )
    if missing:
        problems.append(
            f"{VIRTUAL_DB}: missing {len(missing)} alias(es) from {VIRTUAL}: "
            f"{', '.join(missing)} (stale; re-run --apply)"
        )
    for key in sorted(set(live) & set(wanted)):
        if live[key] != wanted[key]:
            problems.append(
                f"{VIRTUAL_DB}: {key!r} → {live[key]!r}, want {wanted[key]!r} "
                "(stale; re-run --apply)"
            )
    return problems


def sync_virtual_db(source_rewritten: bool = False) -> tuple[bool, str]:
    """Rebuild the alias database when it is stale or its source was rewritten.

    Returns (rebuilt, error). `--apply` passes source_rewritten=True whenever
    it has just written `virtual`, because a *removal* from the committed file
    leaves every remaining key answering correctly — only the rewrite itself
    says the database must be rebuilt.
    """
    if not virtual_db_problems() and not source_rewritten:
        return False, ""
    code, output = rebuild_virtual_db()
    return True, ("" if code == 0 else output)


def rebuild_virtual_db() -> tuple[int, str]:
    return run(["postmap", str(VIRTUAL)])


def sasldb_accounts(path: Path | None = None) -> tuple[list[str], str]:
    """The accounts still in the retired Cyrus database, and why not if unreadable."""
    target = path or SASLDB
    if not target.exists():
        return [], ""
    command = ["sasldblistusers2"]
    if path is not None:
        command += ["-f", str(path)]
    code, output = run(command)
    if code != 0:
        return [], (output or f"sasldblistusers2 exited {code}")
    accounts = []
    for line in output.splitlines():
        name = line.split(":", 1)[0].strip()
        if name:
            accounts.append(name)
    return accounts, ""


def dovecot_accounts(path: Path | None = None) -> set[str]:
    """The passwd-file keys in /etc/dovecot/users."""
    target = path or DOVECOT_USERS
    if not target.exists():
        return set()
    names: set[str] = set()
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(line.split(":", 1)[0])
    return names


def dovecot_users_problems(path: Path | None = None) -> list[str]:
    """The hash file submission AUTH depends on: mode, owner, both accounts."""
    target = path or DOVECOT_USERS
    if not target.exists():
        return [
            f"{target}: missing — create the submission accounts "
            "(deploy/dovecot/apply.py; docs/runbooks/email.md §4.4)"
        ]
    problems: list[str] = []
    info = target.stat()
    if stat.S_IMODE(info.st_mode) != 0o600:
        problems.append(
            f"{target}: mode {oct(stat.S_IMODE(info.st_mode))[2:]}, want 600"
        )
    if (info.st_uid, info.st_gid) != (0, 0):
        problems.append(f"{target}: owner {info.st_uid}:{info.st_gid}, want root:root")
    present = dovecot_accounts(target)
    missing = [account for account in DOVECOT_ACCOUNTS if account not in present]
    if missing:
        problems.append(
            f"{target}: missing account(s) {', '.join(missing)}; submission AUTH "
            "for them fails (deploy/dovecot/apply.py --set-account-password)"
        )
    return problems


def sasl_problems() -> list[str]:
    """Submission AUTH needs the Dovecot socket and hash file, and no sasldb2."""
    problems: list[str] = dovecot_users_problems()
    if not DOVECOT_AUTH_SOCKET.exists():
        problems.append(
            f"{DOVECOT_AUTH_SOCKET}: missing — is dovecot running? "
            "(deploy/dovecot/apply.py --apply)"
        )
    else:
        info = DOVECOT_AUTH_SOCKET.stat()
        mode = stat.S_IMODE(info.st_mode)
        if mode != 0o660:
            problems.append(f"{DOVECOT_AUTH_SOCKET}: mode {oct(mode)[2:]}, want 660")
        postfix = pwd.getpwnam("postfix")
        group = grp.getgrnam("postfix")
        if (info.st_uid, info.st_gid) != (postfix.pw_uid, group.gr_gid):
            problems.append(
                f"{DOVECOT_AUTH_SOCKET}: owner {info.st_uid}:{info.st_gid}, "
                "want postfix:postfix"
            )
    accounts, error = sasldb_accounts()
    if error:
        problems.append(
            f"{SASLDB}: cannot list its accounts ({error}); --apply keeps it "
            "rather than delete the only copy of a credential"
        )
    elif accounts:
        uncovered = sorted(set(accounts) - dovecot_accounts())
        if uncovered:
            problems.append(
                f"{SASLDB}: holds {len(accounts)} account(s), {len(uncovered)} with no "
                f"{DOVECOT_USERS} line ({', '.join(uncovered)}); migrate each with "
                "deploy/dovecot/apply.py --set-account-password, then re-run --apply"
            )
        else:
            problems.append(
                f"{SASLDB}: still holds {len(accounts)} account(s) "
                f"({', '.join(accounts)}); submission AUTH is the Dovecot passwd-file "
                "now — remove it with --apply"
            )
    return problems


def run(command: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        # A host without the package should report drift, not a traceback.
        return 127, f"{command[0]}: command not found"
    return result.returncode, (result.stdout + result.stderr).strip()


def drift() -> list[str]:
    return [problem for entry in FILES for problem in inspect(entry)] + \
        mailbox_problems() + virtual_db_problems() + sasl_problems()


def main_cf_settings(source: Path | None = None) -> dict[str, str]:
    """The committed main.cf as a parameter map.

    The applier compares the file byte-for-byte, which catches drift but not a
    parameter Postfix would silently ignore (`smtp_address_preferance = ipv6`
    is accepted by the file and does nothing). Parsing it here gives the
    offline self-test something to assert on a fresh host: one name, one value,
    no duplicates, no line Postfix would reject.
    """
    path = HERE / "main.cf" if source is None else source
    settings: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ValueError(f"{path}:{number}: not a `name = value` line: {line!r}")
        name, _, value = stripped.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            raise ValueError(f"{path}:{number}: empty parameter name: {line!r}")
        # An empty value is how main.cf clears a setting (`mailbox_command =`).
        if name in settings:
            raise ValueError(f"{path}:{number}: {name} is set twice")
        settings[name] = value
    return settings


def self_test() -> int:
    """Offline checks for the fresh-host path. No network, no /etc writes.

    The ownership half of --apply is root-only by nature; the checks that
    create a mailbox use the current uid/gid so they run anywhere.
    """
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

    # Gmail filed the same brief to Spam over the A and to the INBOX over the
    # AAAA, 4 of 4 each way (docs/evidence/t_7378e496/); `any` is a coin flip.
    settings = main_cf_settings()
    check("the outbound leg prefers the AAAA",
          settings.get("smtp_address_preference"), "ipv6")

    # Submission AUTH is Dovecot's; Postfix has no credential store of its own.
    # The Cyrus-only knobs would look applied and do nothing, so assert they
    # are gone rather than carry dead config.
    check("submission AUTH is Dovecot's", settings.get("smtpd_sasl_type"), "dovecot")
    check("Postfix reaches the Dovecot auth socket",
          settings.get("smtpd_sasl_path"), "private/auth")
    check("the Cyrus-only SASL settings are gone",
          sorted(name for name in settings
                 if name.startswith("cyrus_")
                 or name.startswith("smtpd_sasl_security")
                 or name.startswith("smtpd_sasl_tls_security")), [])
    check("the dead Cyrus app config is retired",
          [path.name for path in REMOVED], ["smtpd.conf"])
    check("the committed files no longer deploy it",
          [entry[0] for entry in FILES if "sasl" in entry[0]], [])

    # Retiring /etc/sasldb2 removes the only copy of every credential in it, so
    # the unlink is gated on each account already having a Dovecot hash line.
    # A help-only store during a Cyrus upgrade is the case that used to lose the
    # tenant secret.
    with tempfile.TemporaryDirectory(prefix="odile-postfix-dovecot-users-") as tmp:
        store = Path(tmp) / "users"
        hash_line = "{SHA512-CRYPT}$6$salt$" + "a" * 86
        store.write_text(
            f"help@odile.fyi:{hash_line}:1000:1000::/var/mail/help::\n",
            encoding="utf-8",
        )
        store.chmod(0o600)
        check("a passwd-file line is read as an account",
              dovecot_accounts(store), {"help@odile.fyi"})
        check("an sasldb2 account with a Dovecot line is covered",
              sorted({"help@odile.fyi"} - dovecot_accounts(store)), [])
        check("an sasldb2 account without a Dovecot line is not covered",
              sorted({"help@odile.fyi", DOVECOT_ACCOUNTS[1]} - dovecot_accounts(store)),
              [DOVECOT_ACCOUNTS[1]])
        check("a help-only store is drift (the tenant account is missing)",
              any(f"missing account(s) {DOVECOT_ACCOUNTS[1]}" in problem
                  for problem in dovecot_users_problems(store)), True)
        store.write_text(
            "".join(f"{account}:{hash_line}:1000:1000::/var/mail/help::\n"
                    for account in DOVECOT_ACCOUNTS),
            encoding="utf-8",
        )
        store.chmod(0o600)
        check("a store with both managed accounts is clean",
              dovecot_users_problems(store), [])

    # Without this, sender_canonical_maps also rewrites From:/Reply-To: — the
    # default class list includes header_sender (postconf(5)), measured on
    # 2026-11-22 when the first submission arrived as help@mail.odile.fyi.
    check("the envelope rewrite leaves header senders alone",
          main_cf_settings().get("sender_canonical_classes"), "envelope_sender")

    check("both mailboxes are reconciled, not only help",
          [box.user for box in MAILBOXES], ["dsn", "help"])
    entries = virtual_entries(HERE / "virtual")
    check("the catch-all routes to dsn", entries.get("@mail.odile.fyi"), "dsn")
    check("help@odile.fyi routes to help", entries.get("help@odile.fyi"), "help")
    check("abuse@odile.fyi routes to help", entries.get("abuse@odile.fyi"), "help")
    check("postmaster@odile.fyi routes to help", entries.get("postmaster@odile.fyi"), "help")

    # The one apex identity: allowed for its own login, then rewritten onto the
    # SPF-authorised, DKIM-signed domain. Both halves are one decision.
    login = pcre_entries(HERE / "sender_login.pcre")
    canonical = pcre_entries(HERE / "sender_canonical.pcre")
    check("the support login may claim its own apex envelope sender",
          login.get(r"^help@odile\.fyi$"), "help@odile.fyi")
    check("the tenant sender is owned by the canonical Dovecot login",
          login.get(r"^[^@]+@mail\.odile\.fyi$"), "odile-test@mail.odile.fyi")
    check("the apex envelope sender is rewritten onto the signed domain",
          canonical.get(r"^help@odile\.fyi$"), "help@mail.odile.fyi")
    check("the canonical table rewrites exactly one sender", len(canonical), 1)

    real_getpwnam = pwd.getpwnam
    real_run = subprocess.run
    with tempfile.TemporaryDirectory(prefix="odile-postfix-mailbox-") as tmp:
        home = Path(tmp) / "dsn"
        box = Mailbox("dsn", home, "bounces")
        state = {"created": False}
        calls: list[list[str]] = []
        uid, gid = os.getuid(), os.getgid()

        def fake_getpwnam(name):
            if name == box.user and state["created"]:
                return pwd.struct_passwd(
                    (box.user, "x", uid, gid, "", str(home), "/usr/sbin/nologin")
                )
            raise KeyError(name)

        def fake_run(command, **kwargs):
            calls.append(list(command))
            if command[0] == "useradd":
                state["created"] = True
            return subprocess.CompletedProcess(command, 0, "", "")

        try:
            pwd.getpwnam = fake_getpwnam
            subprocess.run = fake_run
            problems = mailbox_problems([box])
            check("a host without the dsn user is drift",
                  any("dsn" in problem and "missing" in problem for problem in problems), True)
            actions = ensure_mailbox([box])
            check("--apply creates the missing user with useradd",
                  calls[0][:3], ["useradd", "--create-home", "--home-dir"])
            check("useradd targets the committed home", calls[0][3], str(home))
            check("the Maildir exists afterwards",
                  all((home / "Maildir" / sub).is_dir() for sub in ("new", "cur", "tmp")),
                  True)
            check("ensure_mailbox reports what it created", len(actions) >= 2, True)
            check("nothing is left to fix", mailbox_problems([box]), [])

            calls.clear()
            for sub in ("new", "cur", "tmp"):
                (home / "Maildir" / sub).rmdir()
            ensure_mailbox([box])
            check("an existing user is not useradd'ed again", [call[0] for call in calls], [])
            check("a missing Maildir is recreated",
                  all((home / "Maildir" / sub).is_dir() for sub in ("new", "cur", "tmp")),
                  True)
        finally:
            pwd.getpwnam = real_getpwnam
            subprocess.run = real_run

    global VIRTUAL, VIRTUAL_DB
    saved = (VIRTUAL, VIRTUAL_DB)
    with tempfile.TemporaryDirectory(prefix="odile-postfix-db-") as tmp:
        VIRTUAL = Path(tmp) / "virtual"
        VIRTUAL_DB = Path(tmp) / "virtual.db"

        def answers(key: str) -> bool:
            return run(["postmap", "-q", key, str(VIRTUAL)])[0] == 0

        VIRTUAL.write_text(
            "@mail.odile.fyi dsn\nhelp@odile.fyi help\ninvite@odile.fyi help\n",
            encoding="utf-8",
        )
        try:
            check("a missing virtual.db is drift",
                  any("missing" in problem for problem in virtual_db_problems()), True)
            code, output = rebuild_virtual_db()
            check("postmap builds the database", (code, VIRTUAL_DB.exists()), (0, True))
            check("a fresh database matches the committed entries",
                  virtual_db_problems(), [])

            # A key removed from the source but still present in the database.
            VIRTUAL.write_text("@mail.odile.fyi dsn\nhelp@odile.fyi help\n",
                               encoding="utf-8")
            check("a removed key still in the database is drift",
                  any("invite@odile.fyi" in problem and "not in" in problem
                      for problem in virtual_db_problems()), True)
            rebuilt, error = sync_virtual_db(source_rewritten=True)
            check("--apply after a removal rebuilds the database",
                  (rebuilt, error), (True, ""))
            check("the removed key stops answering", answers("invite@odile.fyi"), False)
            check("the removal leaves nothing behind", virtual_db_problems(), [])
            check("a second run with no rewrite is a no-op",
                  sync_virtual_db(False), (False, ""))

            # An extra entry the source never had.
            stale_source = Path(tmp) / "stale-source"
            stale_source.write_text(
                VIRTUAL.read_text(encoding="utf-8") + "stale-alias@mail.odile.fyi help\n",
                encoding="utf-8",
            )
            code, output = run(["postmap", str(stale_source)])
            check("postmap builds the over-full fixture",
                  (code, Path(str(stale_source) + ".db").exists()), (0, True))
            VIRTUAL_DB.write_bytes(Path(str(stale_source) + ".db").read_bytes())
            os.chmod(VIRTUAL_DB, 0o644)
            check("an extra alias in the database is drift",
                  any("stale-alias@mail.odile.fyi" in problem
                      for problem in virtual_db_problems()), True)
            rebuilt, error = sync_virtual_db(False)
            check("--apply removes an extra alias", (rebuilt, error), (True, ""))
            check("the extra key stops answering", answers("stale-alias@mail.odile.fyi"), False)
            check("the extra entry leaves nothing behind", virtual_db_problems(), [])

            # A destination changed in the source.
            VIRTUAL.write_text("@mail.odile.fyi someone-else\nhelp@odile.fyi help\n",
                               encoding="utf-8")
            check("a database built from older content is caught by its answers",
                  any("stale" in problem for problem in virtual_db_problems()), True)
            sync_virtual_db(True)
            check("postmap fixes the stale database", virtual_db_problems(), [])

            # A database older than its source with identical entries.
            info = VIRTUAL_DB.stat()
            os.utime(VIRTUAL_DB, (info.st_mtime - 300, info.st_mtime - 300))
            check("a database older than its source is drift",
                  any("older than" in problem for problem in virtual_db_problems()), True)
            rebuilt, error = sync_virtual_db(False)
            check("--apply rebuilds a database older than its source",
                  (rebuilt, error), (True, ""))
            check("a rebuilt database is clean and the next run is a no-op",
                  (virtual_db_problems(), sync_virtual_db(False)), ([], (False, "")))
        finally:
            VIRTUAL, VIRTUAL_DB = saved

    print(f"\n{count} checks, {failures} failed")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile Postfix on this machine with deploy/postfix/."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="write the changes")
    group.add_argument("--check", action="store_true", help="exit 1 on any drift")
    group.add_argument("--self-test", action="store_true", help="offline checks")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    problems = drift()

    if args.check:
        for problem in problems:
            print(f"DRIFT  {problem}")
        if problems:
            print(f"\n{len(problems)} difference(s) from deploy/postfix")
            return 1
        print("clean: /etc/postfix matches deploy/postfix, virtual.db matches virtual, "
              "both mailboxes and the Dovecot auth store are present")
        return 0

    if not args.apply:
        if not problems:
            print("nothing to do; /etc/postfix matches deploy/postfix")
            return 0
        for problem in problems:
            print(f"would fix  {problem}")
        print(f"\ndry run: {len(problems)} change(s); pass --apply to write")
        return 0

    # --apply: files first, then the state they depend on, then reload once.
    content_changed = False
    virtual_rewritten = False
    for entry in FILES:
        changed = write(entry)
        content_changed = content_changed or changed
        if changed and entry[1] == VIRTUAL:
            virtual_rewritten = True
        print(f"{'wrote' if changed else 'ok':<6} {entry[1]}")

    # The alias database is rebuilt whenever it is missing, stale, older than
    # its source, or the source was just rewritten — the last case is what
    # makes a removed alias take effect.
    rebuilt, error = sync_virtual_db(virtual_rewritten)
    if rebuilt:
        if error:
            print(f"postmap {VIRTUAL} failed:\n{error}")
            return 1
        print(f"wrote  {VIRTUAL_DB} (postmap)")

    if mailbox_problems():
        for action in ensure_mailbox():
            print(f"wrote  {action}")

    # Validate the committed config before deleting the old store, so a broken
    # main.cf leaves submission's previous credentials on disk.
    code, output = run(["postfix", "check"])
    if code != 0:
        print(f"postfix check failed:\n{output}")
        return 1

    for path in REMOVED:
        if path.exists():
            path.unlink()
            print(f"removed {path} (superseded by smtpd_sasl_type = dovecot)")

    # Delete the old store only once every account in it has a hash line in the
    # Dovecot passwd-file. On a Cyrus upgrade /etc/dovecot/users may exist but
    # hold only the mailbox; unlinking then would take the only copy of the
    # tenant secret with it.
    if SASLDB.exists():
        accounts, error = sasldb_accounts()
        uncovered = [] if error else sorted(set(accounts) - dovecot_accounts())
        if error or uncovered:
            reason = error or (f"no {DOVECOT_USERS} line for {', '.join(uncovered)}")
            print(f"kept   {SASLDB} (still the only copy of a credential: {reason})")
        else:
            held = ", ".join(accounts) if accounts else "no accounts"
            SASLDB.unlink()
            print(f"removed {SASLDB} (Cyrus store superseded; held {held} in cleartext)")

    left = drift()
    if left:
        for problem in left:
            print(f"still  {problem}")
        return 1

    if content_changed:
        # A reload re-reads main.cf but does not rebuild listening sockets, so
        # a change to inet_protocols would look applied and not be. Restart is
        # the honest verb; the queue survives it.
        code, output = run(["systemctl", "restart", "postfix"])
        if code != 0:
            print(f"postfix restart failed:\n{output}")
            return 1
        print("restarted postfix")
    if not content_changed:
        print("nothing to do; /etc/postfix matches deploy/postfix")
    return 0


if __name__ == "__main__":
    sys.exit(main())
