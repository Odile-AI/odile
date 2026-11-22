#!/usr/bin/python3
"""Exercise Odile's self-hosted mail path.

Odile sends from its own Postfix on HQ over submission (587, STARTTLS, SASL),
signed by OpenDKIM. There is no relay and no API key; the only secret is the
tenant's SASL password, read from a 0600 file and never from argv.

    --self-test          offline logic checks, no network, no root
    --check-dns          live records, forward-confirmed PTR, key match
    --check-mta          services, milter, submission and SASL are wired
    --verify-dkim FILE   prove a signed message's body hash and signature
    --send --to ADDR     submit one invite and print its queue id
    --status ID          what happened to a queued message
    --soak --to ADDR     send one message every --interval seconds and record
                         each queue id in a JSONL file (default 24 × 1800 s)
    --soak-report FILE   summarise a soak: sent, deferred, bounced, oldest 4xx

Standard library only; `dig` is used for DNS because a resolver that lies is
not evidence, and `openssl` for signature math. See docs/runbooks/email.md.
"""

from __future__ import annotations

import argparse
import base64
import imaplib
import grp
import hashlib
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid

DOMAIN = "mail.odile.fyi"
SELECTOR = "odile"
FROM = "Odile <invite@mail.odile.fyi>"
REPLY_TO = "help@odile.fyi"
SUBJECT = "Odile — test invite (ODL-102)"
SOAK_SUBJECT = "Odile soak"

ACCOUNT_FILE = "/root/.odile/mail_test_account.json"
DKIM_KEY = "/etc/dkimkeys/odile.private"
DKIM_TXT = "/etc/dkimkeys/odile.txt"
SASLDB = "/etc/sasldb2"
SENDER_LOGIN_MAP = "/etc/postfix/sender_login.pcre"
MILTER_SOCKET = "/var/spool/postfix/opendkim/opendkim.sock"
MAIL_LOG = "/var/log/mail.log"
SOAK_LOG = "/var/log/odile-mail-soak.jsonl"

DKIM_DNS_NAME = f"{SELECTOR}._domainkey.{DOMAIN}"
DMARC_DNS_NAME = f"_dmarc.{DOMAIN}"

TEXT = """\
This is the ODL-102 mail test.

If you are reading this in a Gmail inbox, mail.odile.fyi can deliver an
invitation and pass authentication.

Reply to this message and it reaches {reply_to}.

— Odile
"""

SOAK_TEXT = """\
This is message {number} of {total} in the ODL-102 deliverability soak.

One message every {minutes} minutes for {hours} hours. If this one is in the
inbox, the path is still healthy.

— Odile
"""


class Fail(Exception):
    """A failure a human can act on, not a traceback."""


# -------------------------------------------------------------------- account

def load_account() -> dict:
    """The tenant interface: host, port, username, secret — from a 0600 file."""
    if not os.path.exists(ACCOUNT_FILE):
        raise Fail(
            f"submission account: no file at {ACCOUNT_FILE}. Create one at mode 0600 "
            "with host, port, username and secret (docs/runbooks/email.md §5.2)."
        )
    mode = oct(os.stat(ACCOUNT_FILE).st_mode & 0o777)[2:]
    if mode not in ("600", "400"):
        raise Fail(f"{ACCOUNT_FILE} is mode {mode}; the secret must be 0600")
    with open(ACCOUNT_FILE, encoding="utf-8") as handle:
        account = json.load(handle)
    for field in ("host", "port", "username", "secret"):
        if not account.get(field):
            raise Fail(f"{ACCOUNT_FILE} has no {field!r}")
    account.setdefault("from", FROM)
    account.setdefault("reply_to", REPLY_TO)
    account.setdefault("tls_ca", "/etc/ssl/certs/mail.odile.fyi.crt")
    return account


# ------------------------------------------------------------------------- dns

_NS_CACHE: dict[str, str | None] = {}


def dig(args: list[str], server: str | None = None) -> list[str]:
    """One question, answered by dig. Failure is an empty answer, not silence."""
    command = ["dig", "+short", "+time=3", "+tries=1"]
    if server:
        command.append(f"@{server}")
    command.extend(args)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise Fail(f"dig {args!r} failed: {result.stderr.strip() or result.stdout.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def authoritative(name: str) -> str | None:
    """The zone's own nameserver: a local cache can serve a stale NXDOMAIN."""
    labels = name.rstrip(".").split(".")
    for index in range(len(labels)):
        zone = ".".join(labels[index:])
        if zone not in _NS_CACHE:
            servers = dig(["NS", zone])
            _NS_CACHE[zone] = servers[0].rstrip(".") if servers else None
        if _NS_CACHE[zone]:
            return _NS_CACHE[zone]
    return None


def auth_dig(args: list[str], name: str) -> list[str]:
    return dig(args, server=authoritative(name))


def clean_txt(line: str) -> str:
    """dig prints "chunk" "chunk" for a value over 255 bytes; undo that."""
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', line)
    if parts:
        return "".join(part.replace('\\"', '"') for part in parts)
    return line.strip().strip('"')


def txt(name: str) -> list[str]:
    return [clean_txt(line) for line in auth_dig(["TXT", name], name)]


def mx(name: str) -> list[tuple[int, str]]:
    records = []
    for line in auth_dig(["MX", name], name):
        priority, _, host = line.partition(" ")
        if host:
            records.append((int(priority), host.rstrip(".")))
    return sorted(records)


def a_records(name: str) -> list[str]:
    return auth_dig(["A", name], name)


def aaaa_records(name: str) -> list[str]:
    return auth_dig(["AAAA", name], name)


def reverse_name(ip: str) -> str:
    """The in-addr.arpa / ip6.arpa name, so the PTR is asked authoritatively too."""
    if ":" in ip:
        import ipaddress

        return ipaddress.ip_address(ip).reverse_pointer
    return ".".join(reversed(ip.split("."))) + ".in-addr.arpa"


def ptr_name(ip: str) -> str | None:
    """The PTR, asked of a public resolver first.

    Reverse zones are delegated below the RIR nameservers that serve
    `ip6.arpa` / `in-addr.arpa`, so the forward zone's authoritative server is
    the wrong authority for them: it answers with a referral and `+short`
    prints nothing. The local cache is asked last because it may still hold the
    NXDOMAIN from before the records were published.
    """
    name = reverse_name(ip)
    for server in ("1.1.1.1", "8.8.8.8", None):
        command = ["dig", "+short", "+time=3", "+tries=1"]
        if server:
            command.append(f"@{server}")
        command.extend(["PTR", name])
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if names:
                return names[0].rstrip(".")
    return None


def check_spf(txts: list[str], addresses: str | list[str]) -> tuple[bool, str]:
    """Every address we send from must be authorised, no third party may be, and -all ends it."""
    ips = [addresses] if isinstance(addresses, str) else list(addresses)
    spf = [value for value in txts if value.lower().startswith("v=spf1")]
    if not spf:
        return False, f"{DOMAIN}: no SPF record"
    if len(spf) > 1:
        return False, f"{DOMAIN}: {len(spf)} SPF records (must be exactly one)"
    value = spf[0]
    compact = value.replace(" ", "")
    missing = [ip for ip in ips if f"ip4:{ip}" not in compact and f"ip6:{ip}" not in compact]
    if missing:
        return False, f"{DOMAIN}: SPF does not authorise {', '.join(missing)}: {value}"
    if "include:" in compact or "redirect=" in compact:
        return False, f"{DOMAIN}: SPF leaves the machine ({value})"
    if not compact.rstrip(";").endswith("-all"):
        return False, f"{DOMAIN}: SPF does not end in -all: {value}"
    return True, value


def check_dkim(txts: list[str], expected_public: str | None = None) -> tuple[bool, str]:
    keys = [value for value in txts if "p=" in value]
    if not keys:
        return False, f"{DKIM_DNS_NAME}: no DKIM key"
    if len(keys) > 1:
        return False, f"{DKIM_DNS_NAME}: {len(keys)} DKIM keys (expected one)"
    match = re.search(r"p=([A-Za-z0-9+/=]+)", keys[0])
    if not match:
        return False, f"{DKIM_DNS_NAME}: no usable p= value"
    key = match.group(1)
    if len(key) < 300:
        return False, f"{DKIM_DNS_NAME}: key looks truncated ({len(key)} chars)"
    if expected_public and key != expected_public:
        return False, f"{DKIM_DNS_NAME}: the DNS key is not the key on this machine"
    return True, f"{DKIM_DNS_NAME}: {len(key)}-char p= matches the local key"


def check_dmarc(txts: list[str]) -> tuple[bool, str]:
    records = [value for value in txts if value.upper().startswith("V=DMARC1")]
    if not records:
        return False, f"{DMARC_DNS_NAME}: no DMARC record"
    if len(records) > 1:
        return False, f"{DMARC_DNS_NAME}: {len(records)} DMARC records (must be exactly one)"
    value = records[0]
    policy = re.search(r"(?:^|;)\s*p\s*=\s*(none|quarantine|reject)", value, re.I)
    if not policy:
        return False, f"{DMARC_DNS_NAME}: no policy in {value}"
    if policy.group(1).lower() == "none":
        return False, f"{DMARC_DNS_NAME}: p=none publishes no policy ({value})"
    compact = value.replace(" ", "")
    for tag in ("adkim=s", "aspf=s"):
        if tag not in compact:
            return False, f"{DMARC_DNS_NAME}: strict alignment missing ({tag})"
    if "rua=mailto:" not in compact:
        return False, f"{DMARC_DNS_NAME}: no rua= address for reports"
    return True, value


def check_fcrdns(ip: str, helo: str) -> tuple[bool, str]:
    """Forward-confirmed reverse DNS: Gmail requires it for every sender."""
    name = ptr_name(ip)
    if not name:
        return False, f"{ip}: no PTR record"
    if name.endswith(".local") or "." not in name:
        return False, f"{ip}: PTR is a placeholder ({name}); ask Servury for {helo}"
    forward = a_records(name) + aaaa_records(name)
    if ip not in forward:
        return False, f"{ip}: PTR {name} does not forward-resolve to it ({', '.join(forward) or 'nothing'})"
    if name != helo:
        return False, f"{ip}: PTR {name}, want {helo}"
    return True, f"{ip} → {name} → {ip}"


def check_bounce_mx(name: str, ip: str) -> tuple[bool, str]:
    records = mx(name)
    if not records:
        return False, f"{name}: no MX, bounces have nowhere to return"
    hosts = [host for _, host in records]
    if name not in hosts:
        return False, f"{name}: MX {', '.join(hosts)}, want {name}"
    if ip not in a_records(name):
        return False, f"{name}: MX target does not resolve to {ip}"
    return True, f"MX {records[0][0]} {hosts[0]}"


def local_public_key() -> str | None:
    """The p= value from the TXT file this machine generated, if readable."""
    if not os.path.exists(DKIM_TXT):
        return None
    with open(DKIM_TXT, encoding="utf-8") as handle:
        text = handle.read()
    joined = "".join(re.findall(r'"([^"]*)"', text)) or text
    match = re.search(r"p=([A-Za-z0-9+/=]+)", joined)
    return match.group(1) if match else None


# --------------------------------------------------------------------------- mta

def run(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    return result.returncode, (result.stdout + result.stderr).strip()


def smtp_probe(port: int = 25) -> tuple[bool, str]:
    try:
        with smtplib.SMTP("127.0.0.1", port, timeout=10) as client:
            code, banner = client.ehlo(f"probe.{DOMAIN}")
    except OSError as exc:
        return False, f"port {port}: {exc}"
    return code == 250, banner.decode(errors="replace").splitlines()[0]


def submission_probe(account: dict) -> tuple[bool, str]:
    """STARTTLS, then AUTH, against the name the certificate is for. No message."""
    context = ssl.create_default_context(cafile=account["tls_ca"])
    try:
        with smtplib.SMTP(account["host"], int(account["port"]), timeout=20) as client:
            client.ehlo(f"probe.{DOMAIN}")
            if not client.has_extn("starttls"):
                return False, "submission does not offer STARTTLS"
            client.starttls(context=context)
            client.ehlo(f"probe.{DOMAIN}")
            if not client.has_extn("auth"):
                return False, "submission does not offer AUTH after STARTTLS"
            client.login(account["username"], account["secret"])
            version = getattr(client.sock, "version", lambda: "unknown")()
            return True, f"STARTTLS {version} + AUTH {account['username']}"
    except (OSError, smtplib.SMTPException) as exc:
        return False, f"{account['host']}:{account['port']}: {exc}"


def check_mta() -> list[tuple[bool, str, str]]:
    """Every row is (ok, name, detail). Only the submission probe needs the file."""
    rows: list[tuple[bool, str, str]] = []

    for unit in ("postfix", "opendkim"):
        code, out = run(["systemctl", "is-active", unit])
        rows.append((code == 0 and out == "active", f"{unit} service", out))

    code, out = run(["postconf", "-h", "myhostname"])
    rows.append((out == DOMAIN, "myhostname", f"{out} (want {DOMAIN})"))

    code, out = run(["postconf", "-h", "inet_protocols"])
    rows.append((out == "all", "inet_protocols", f"{out} (want all: the name has an AAAA)"))

    code, out = run(["postconf", "-h", "smtpd_milters"])
    socket_path = out.split("unix:")[-1] if "unix:" in out else ""
    rows.append((bool(socket_path), "milter configured", out or "smtpd_milters is empty"))
    if socket_path:
        present = os.path.exists(socket_path)
        group = grp.getgrgid(os.stat(socket_path).st_gid).gr_name if present else "-"
        rows.append((present and group == "postfix", "milter socket",
                     f"{socket_path} group {group}"))

    present = os.path.exists(DKIM_KEY)
    mode = oct(os.stat(DKIM_KEY).st_mode & 0o777)[2:] if present else "-"
    rows.append((present and mode == "600", "dkim private key", f"{DKIM_KEY} mode {mode}"))

    if os.path.exists(SASLDB):
        info = os.stat(SASLDB)
        group = grp.getgrgid(info.st_gid).gr_name
        mode = oct(info.st_mode & 0o777)[2:]
        rows.append((mode == "640" and group == "postfix", "sasl account db",
                     f"{SASLDB} mode {mode} group {group}"))
    else:
        rows.append((False, "sasl account db", f"{SASLDB} missing"))

    rows.append((os.path.exists(SENDER_LOGIN_MAP), "sender login map", SENDER_LOGIN_MAP))

    try:
        account = load_account()
    except Fail as exc:
        rows.append((False, "submission account", str(exc)))
    else:
        ok, detail = submission_probe(account)
        rows.append((ok, "submission 587", detail))

    ok, banner = smtp_probe(25)
    rows.append((ok, "smtp banner", banner))

    code, out = run(["postfix", "check"])
    rows.append((code == 0, "postfix check", out.splitlines()[-1] if out else "ok"))
    return rows


# ------------------------------------------------------------------------- send

def verp(recipient: str, day: str | None = None, nonce: str = "") -> str:
    """A Return-Path unique per message: bounces identify the send, not just the day."""
    day = day or datetime.now(timezone.utc).strftime("%Y%m%d")
    digest = hashlib.sha256(f"{recipient.lower().strip()}|{nonce}".encode()).hexdigest()[:12]
    return f"b-{day}-{digest}@{DOMAIN}"


def build_message(to: str, subject: str = SUBJECT, body: str | None = None,
                  account: dict | None = None) -> EmailMessage:
    account = account or {}
    message = EmailMessage()
    message["From"] = account.get("from", FROM)
    message["To"] = to
    message["Reply-To"] = account.get("reply_to", REPLY_TO)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=DOMAIN)
    # Gmail asks bulk senders for an unsubscribe route; these are transactional,
    # but the header costs nothing and helps a fresh domain look legitimate.
    message["List-Unsubscribe"] = f"<mailto:{account.get('reply_to', REPLY_TO)}?subject=unsubscribe>"
    message.set_content(body if body is not None else TEXT.format(reply_to=REPLY_TO))
    return message


def smtp_session(account: dict) -> smtplib.SMTP:
    """Submission, as a tenant does it: STARTTLS first, then SASL."""
    context = ssl.create_default_context(cafile=account["tls_ca"])
    client = smtplib.SMTP(account["host"], int(account["port"]), timeout=30)
    client.ehlo(f"probe.{DOMAIN}")
    client.starttls(context=context)
    client.ehlo(f"probe.{DOMAIN}")
    client.login(account["username"], account["secret"])
    return client


def submit(message: EmailMessage, to: str, dry_run: bool,
           account: dict | None = None, nonce: str = "") -> str:
    """Hand the message to submission; return the queue id."""
    account = account or load_account()
    sender = verp(to, nonce=nonce)
    if dry_run:
        print(f"account:    {account['username']}@{account['host']}:{account['port']} (STARTTLS)")
        print(f"MAIL FROM:<{sender}>")
        print(f"RCPT TO:<{to}>")
        print(message.as_string())
        return "dry-run"

    with smtp_session(account) as client:
        code, response = client.mail(sender)
        if code != 250:
            raise Fail(f"submission refused the sender: {code} {response.decode(errors='replace')}")
        code, response = client.rcpt(to)
        if code not in (250, 251):
            raise Fail(f"submission refused the recipient: {code} {response.decode(errors='replace')}")
        code, response = client.data(bytes(message))
        if code != 250:
            raise Fail(f"submission refused the message: {code} {response.decode(errors='replace')}")
    text = response.decode(errors="replace")
    match = re.search(r"queued as ([0-9A-Za-z]+)", text)
    if not match:
        raise Fail(f"no queue id in the reply: {text}")
    return match.group(1)


def queue_status(queue_id: str) -> tuple[str, list[str]]:
    """Queued, delivered or bounced — from the queue first, then the log."""
    code, out = run(["postqueue", "-j"])
    if code == 0:
        for line in out.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("queue_id") == queue_id:
                reasons = [
                    r.get("delay_reason", "")
                    for r in entry.get("recipients", [])
                    if r.get("delay_reason")
                ]
                return "queued", reasons or ["waiting for the next delivery attempt"]
    if not os.path.exists(MAIL_LOG):
        return "unknown", [f"{MAIL_LOG} missing"]
    found: list[str] = []
    with open(MAIL_LOG, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if queue_id in line and "status=" in line:
                found.append(line.strip())
    if not found:
        return "unknown", ["not in the queue and not in the log"]
    last = found[-1]
    status_match = re.search(r"status=(\w+)", last)
    dsn_match = re.search(r"dsn=([\d.]+)", last)
    verdict = status_match.group(1) if status_match else "unknown"
    detail = last if not dsn_match else f"{last}  [dsn {dsn_match.group(1)}]"
    return verdict, [detail]


def dsn_of(lines: list[str]) -> str:
    """The DSN code from a log line: dsn= for a result, (451 4.7.1 …) for a deferral."""
    for line in lines:
        match = re.search(r"dsn=([\d.]+)", line)
        if match:
            return match.group(1)
        match = re.search(r"\((\d{3}) ([\d.]+)", line)
        if match:
            return match.group(2)
    return ""


# ------------------------------------------------------------------------ soak

def append_jsonl(path: str, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def cmd_soak(args: argparse.Namespace) -> int:
    account = load_account()
    total = args.count
    interval = args.interval
    started = datetime.now(timezone.utc)
    print(f"soak: {total} messages, one every {interval} s, to {args.to}", flush=True)
    print(f"log:  {args.log}", flush=True)
    for number in range(args.start, total + 1):
        body = SOAK_TEXT.format(number=number, total=total,
                                minutes=interval // 60,
                                hours=round(total * interval / 3600, 1))
        body += f"\nReference: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        message = build_message(args.to, subject=f"{SOAK_SUBJECT} {number}/{total}",
                                body=body, account=account)
        nonce = f"{number}-{total}-{int(time.time())}"
        queue_id = submit(message, args.to, False, account, nonce=nonce)
        time.sleep(3)
        verdict, lines = queue_status(queue_id)
        record = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "number": number,
            "total": total,
            "queue_id": queue_id,
            "verdict": verdict,
            "dsn": dsn_of(lines),
            "detail": lines[-1] if lines else "",
        }
        append_jsonl(args.log, record)
        print(f"[{number:>2}/{total}] {record['at']} {queue_id} {verdict} {record['dsn']}",
              flush=True)
        if number < total:
            time.sleep(interval)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"soak finished: {total} messages in {elapsed / 3600:.1f} h", flush=True)
    return 0


def classify_soak(records: list[dict], log_lines: list[str]) -> tuple[dict, datetime | None, int]:
    """Tally a soak, refreshing each verdict from mail.log where the line survives.

    The JSONL is the durable record. mail.log keeps three rotations, so a report
    read the next day finds no line for most queue ids; in that case the verdict
    captured at send time is the truth, not `unknown`.
    """
    tally = {"sent": 0, "deferred": 0, "bounced": 0, "unknown": 0}
    oldest_deferred: datetime | None = None
    refreshed = 0
    for record in records:
        queue_id = record.get("queue_id") or ""
        lines = [line for line in log_lines
                 if queue_id and queue_id in line and "status=" in line]
        verdict = record.get("verdict") or "unknown"
        if lines:
            refreshed += 1
            match = re.search(r"status=(\w+)", lines[-1])
            if match:
                verdict = match.group(1)
            record["dsn"] = dsn_of(lines) or record.get("dsn", "")
        if verdict == "sent":
            tally["sent"] += 1
        elif verdict == "deferred":
            tally["deferred"] += 1
            stamp = lines[-1].split()[0] if lines else record.get("at", "")
            try:
                when = datetime.fromisoformat(stamp)
            except ValueError:
                when = datetime.now(timezone.utc)
            oldest_deferred = when if oldest_deferred is None else min(oldest_deferred, when)
        elif verdict == "bounced":
            tally["bounced"] += 1
        else:
            tally["unknown"] += 1
    return tally, oldest_deferred, refreshed


def soak_verdict(tally: dict, messages: int, expected: int,
                 oldest_age: float | None) -> tuple[bool, str]:
    """The pass rule for a soak, separated so it can be checked offline.

    A 24-message soak that loses 23 records must not pass on the strength of
    the one it kept: the count is part of the claim.
    """
    if messages != expected:
        return False, f"{messages} message(s) in the log, expected {expected}"
    if tally["sent"] != expected:
        return False, (f"{tally['sent']}/{expected} sent (2.x), "
                       f"{tally['deferred']} deferred, {tally['bounced']} bounced, "
                       f"{tally['unknown']} unknown")
    if oldest_age is not None and oldest_age > 60:
        return False, f"an open 4xx is {oldest_age:.0f} min old (limit 60)"
    return True, f"{expected}/{expected} sent (2.x), no open 4xx older than an hour"


def soak_report(path: str, expected: int = 24) -> int:
    if not os.path.exists(path):
        print(f"no soak log at {path}")
        return 1
    records = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    if not records:
        print(f"no records in {path}")
        return 1

    # Re-read the MTA log once: a status can change after the soak recorded it.
    log_lines: list[str] = []
    if os.path.exists(MAIL_LOG):
        with open(MAIL_LOG, encoding="utf-8", errors="replace") as handle:
            log_lines = handle.readlines()

    tally, oldest_deferred, refreshed = classify_soak(records, log_lines)
    sent, deferred, bounced, unknown = (tally["sent"], tally["deferred"],
                                        tally["bounced"], tally["unknown"])
    age: float | None = None

    last = records[-1]
    print(f"soak log:        {path}")
    print(f"window:          {records[0]['at']} → {last['at']}  ({len(records)} messages)")
    print(f"verdicts:        {refreshed} refreshed from {MAIL_LOG}, "
          f"{len(records) - refreshed} kept from the soak log (rotated out)")
    print(f"sent (2.x):      {sent} of {expected}")
    print(f"deferred (4.x):  {deferred}")
    print(f"bounced (5.x):   {bounced}")
    print(f"unknown:         {unknown}")
    if oldest_deferred:
        age = (datetime.now(timezone.utc) - oldest_deferred).total_seconds() / 60
        print(f"oldest open 4xx: {age:.0f} min (since {oldest_deferred.isoformat()})")
    else:
        print("oldest open 4xx: none")
    print(f"last message:    {last['queue_id']} {last.get('verdict')} {last.get('dsn')}")
    ok, detail = soak_verdict(tally, len(records), expected, age)
    print(f"{'PASS' if ok else 'FAIL'}  {detail}")
    return 0 if ok else 1


# ------------------------------------------------------------------- dkim verify

def relaxed_body(data: bytes) -> bytes:
    """RFC 6376 3.4.4: strip trailing WSP, squeeze runs, drop trailing empty lines."""
    lines = data.replace(b"\r\n", b"\n").split(b"\n")
    cleaned = [re.sub(rb"[ \t]+", b" ", line).rstrip(b" \t") for line in lines]
    while cleaned and cleaned[-1] == b"":
        cleaned.pop()
    return b"\r\n".join(cleaned) + b"\r\n"


def relaxed_header(name: str, value: str) -> str:
    unfolded = re.sub(r"\r?\n(?=[ \t])", "", value)
    squeezed = re.sub(r"[ \t]+", " ", unfolded).strip()
    return f"{name.strip().lower()}:{squeezed}"


def split_dkim_header(value: str) -> dict[str, str]:
    tags = {}
    for part in re.split(r";\s*", value.replace("\r\n", " ").replace("\n", " ")):
        if "=" not in part:
            continue
        key, _, val = part.partition("=")
        tags[key.strip().lower()] = val.strip()
    return tags


def select_signed_headers(message, names: list[str]) -> list[tuple[str, str]]:
    """RFC 6376 5.4.2 header selection, matching OpenDKIM's behaviour.

    Each h= entry takes the next unused instance of that field from the
    bottom. OpenDKIM oversigns From by naming it twice, so a reference with no
    instance left is dropped, not signed as an empty header.
    """
    selected: list[tuple[str, str]] = []
    last: dict[str, int] = {}
    for name in names:
        key = name.lower()
        values = message.get_all(name) or []
        index = last.get(key, len(values))
        while index > 0:
            index -= 1
            selected.append((name, str(values[index])))
            break
        last[key] = index
    return selected


def dkim_verify(path: str, pubkey_path: str | None = None) -> list[tuple[bool, str, str]]:
    """Check bh= against the body and b= against the key. Relaxed/relaxed only."""
    with open(path, "rb") as handle:
        raw = handle.read()
    header_blob, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        header_blob, _, body = raw.partition(b"\n\n")

    message = BytesParser(policy=policy.compat32).parsebytes(raw)
    signature = message.get("DKIM-Signature")
    if not signature:
        return [(False, "signature", "no DKIM-Signature header")]
    tags = split_dkim_header(str(signature))
    if tags.get("c", "simple/simple") != "relaxed/relaxed":
        return [(False, "c=", f"only relaxed/relaxed is implemented, got {tags.get('c')}")]

    rows: list[tuple[bool, str, str]] = [
        (tags.get("d") == DOMAIN, "d=", tags.get("d", "missing")),
        (tags.get("s") == SELECTOR, "s=", tags.get("s", "missing")),
    ]

    body_hash = base64.b64encode(hashlib.sha256(relaxed_body(body)).digest()).decode()
    rows.append((body_hash == tags.get("bh"), "body hash",
                 "match" if body_hash == tags.get("bh") else "MISMATCH"))

    names = [item for item in tags.get("h", "").split(":") if item]
    selected = select_signed_headers(message, names)
    lines = [relaxed_header(name, value) for name, value in selected]

    stripped = re.sub(r"(;\s*)b=[^;]*", r"\1b=", str(signature))
    lines.append(relaxed_header("DKIM-Signature", stripped))
    signed_data = "\r\n".join(lines).encode()

    with tempfile.TemporaryDirectory() as tmp:
        if pubkey_path:
            with open(pubkey_path, "rb") as handle:
                pem = handle.read()
        else:
            code, pem_out = run(["openssl", "rsa", "-in", DKIM_KEY, "-pubout"])
            if code != 0:
                return rows + [(False, "public key", pem_out)]
            pem = pem_out.encode()
        pem_path = os.path.join(tmp, "pub.pem")
        sig_path = os.path.join(tmp, "sig.bin")
        data_path = os.path.join(tmp, "signed.txt")
        with open(pem_path, "wb") as handle:
            handle.write(pem)
        with open(sig_path, "wb") as handle:
            handle.write(base64.b64decode(tags.get("b", "")))
        with open(data_path, "wb") as handle:
            handle.write(signed_data)
        code, out = run(["openssl", "dgst", "-sha256", "-verify", pem_path,
                         "-signature", sig_path, data_path])
    rows.append((code == 0, "signature", out.splitlines()[-1] if out else "no output"))
    return rows


# ------------------------------------------------------------------- evidence

GMAIL_USER = "owner@example.com"
GMAIL_PASSWORD_FILE = "/root/.odile/gmail_app_password"


def read_secret_file(path: str, label: str) -> str:
    if not os.path.exists(path):
        raise Fail(
            f"{label}: no secret at {path}. Create it at mode 0600 "
            f"(install -m 600 /dev/null {path}) and paste the value in."
        )
    mode = oct(os.stat(path).st_mode & 0o777)[2:]
    if mode not in ("600", "400"):
        print(f"warning: {path} is mode {mode}; want 600", file=sys.stderr)
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


def verdicts(authentication_results: str) -> dict[str, str]:
    """spf/dkim/dmarc verdicts from an Authentication-Results header."""
    found: dict[str, str] = {}
    for mechanism in ("spf", "dkim", "dmarc"):
        match = re.search(rf"\b{mechanism}\s*=\s*([a-z]+)", authentication_results, re.I)
        if match:
            found[mechanism] = match.group(1).lower()
    return found


def evidence_headers(raw: bytes) -> dict[str, str]:
    """The four fields the card asks for, unfolded and trimmed."""
    message = BytesParser(policy=policy.compat32).parsebytes(raw)

    def flat(name: str, limit: int = 400) -> str:
        value = message.get(name)
        return " ".join(str(value).split())[:limit] if value else ""

    received = message.get_all("Received") or []
    return {
        "Authentication-Results": flat("Authentication-Results", 600),
        "DKIM-Signature": flat("DKIM-Signature", 300),
        "Return-Path": flat("Return-Path", 120),
        "Received": " ".join(str(received[0]).split())[:200] if received else "",
    }


def ascii_search_term(query: str) -> str:
    """The longest ASCII run of a subject, for IMAP SEARCH which is ASCII-only."""
    parts = [part.strip() for part in re.split(r"[^\x20-\x7e]+", query) if part.strip()]
    if not parts:
        raise Fail(f"subject {query!r} has no ASCII part to search for")
    return max(parts, key=len)


def gmail_search(client: imaplib.IMAP4_SSL, query: str):
    """IMAP SEARCH arguments are ASCII; fall back to the longest ASCII run."""
    try:
        return client.search(None, "SUBJECT", f'"{query}"')
    except UnicodeEncodeError:
        return client.search(None, "SUBJECT", f'"{ascii_search_term(query)}"')


def gmail_labels(client: imaplib.IMAP4_SSL, uid: bytes) -> list[str]:
    key = uid.decode() if isinstance(uid, bytes) else uid
    status, payload = client.fetch(key, "(X-GM-LABELS)")
    if status != "OK" or not payload:
        return []
    text = b" ".join(part for part in payload if isinstance(part, bytes)).decode(
        "utf-8", "replace")
    return [label.replace("\\\\", "\\") for label in re.findall(r'"((?:[^"\\]|\\.)*)"', text)]


def placement_of(labels: list[str], folder: str | None = None) -> str:
    """Where Gmail filed a message: INBOX, SPAM or elsewhere.

    A message in ``[Gmail]/Spam`` answers ``X-GM-LABELS ()`` — it carries no
    ``\\Spam`` label, because Spam is a folder and not a label (measured
    2026-11-22 on two quarantined briefs, card t_7378e496). Reading labels
    alone called those messages OTHER, so the folder the copy came from is part
    of the verdict.
    """
    folded = {label.lower().lstrip("\\") for label in labels}
    where = (folder or "").lower().strip('"')
    if "inbox" in folded or where == "inbox":
        return "INBOX"
    if "spam" in folded or where.endswith("/spam"):
        return "SPAM"
    return "OTHER"


GMAIL_ALL_MAIL = '"[Gmail]/All Mail"'
GMAIL_SPAM = '"[Gmail]/Spam"'


def gmail_search_folder(client: imaplib.IMAP4_SSL, folder: str, query: str) -> list[bytes]:
    client.select(folder, readonly=True)
    status, data = gmail_search(client, query)
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def gmail_fetch(query: str, user: str) -> tuple[bytes, list[str], str]:
    """Find a message by subject in All Mail or Spam; return bytes, labels, folder."""
    password = read_secret_file(GMAIL_PASSWORD_FILE, "Gmail app password")
    client = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        client.login(user, password)
        # Gmail's "All Mail" deliberately excludes Spam and Trash, so a message
        # filed to Spam is invisible unless Spam is searched too.
        uids: list[bytes] = []
        folder = GMAIL_ALL_MAIL
        for candidate in (GMAIL_ALL_MAIL, GMAIL_SPAM):
            uids = gmail_search_folder(client, candidate, query)
            if uids:
                folder = candidate
                break
        if not uids:
            raise Fail(f"no message in {user} with subject {query!r} (All Mail and Spam)")
        newest = uids[-1]
        labels = gmail_labels(client, newest)
        status, payload = client.fetch(newest.decode() if isinstance(newest, bytes) else newest,
                                       "(RFC822)")
        if status != "OK" or not payload or not isinstance(payload[0], tuple):
            raise Fail(f"could not fetch message {newest!r}")
        return payload[0][1], labels, folder
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001 - best effort on the way out
            pass


def cmd_gmail_headers(args: argparse.Namespace) -> int:
    raw, labels, folder = gmail_fetch(args.gmail_headers, args.user)
    fields = evidence_headers(raw)
    results = verdicts(fields["Authentication-Results"])
    print(f"search:    {args.gmail_headers!r} as {args.user}")
    print(f"placement: {placement_of(labels, folder)}  ({', '.join(labels) or 'no labels'}; found in {folder})")
    for name in ("Authentication-Results", "DKIM-Signature", "Return-Path", "Received"):
        print(f"{name}: {fields[name] or '(absent)'}")
    print()
    expected = {"spf": "pass", "dkim": "pass", "dmarc": "pass"}
    ok = all(results.get(key) == value for key, value in expected.items())
    for key, value in expected.items():
        got = results.get(key, "missing")
        print(f"{'PASS' if got == value else 'FAIL'}  {key:<6} {got}")
    placement = placement_of(labels, folder)
    print(f"{'PASS' if placement == 'INBOX' else 'FAIL'}  inbox  {placement}")
    return 0 if ok and placement == "INBOX" else 1


def placement_verdict(tally: dict, total: int, expected: int) -> tuple[bool, str]:
    """The soak's inbox-not-spam rule: every expected message, all in the INBOX."""
    if total != expected:
        return False, f"{total} message(s) matched, expected {expected}"
    if tally["INBOX"] != expected:
        return False, (f"{tally['INBOX']}/{expected} in the inbox, "
                       f"{tally['SPAM']} spam, {tally['OTHER']} other")
    return True, f"{expected}/{expected} in the inbox, nothing in spam or elsewhere"


def cmd_gmail_placement(args: argparse.Namespace) -> int:
    """Tally where Gmail filed every message matching a subject (the soak check)."""
    password = read_secret_file(GMAIL_PASSWORD_FILE, "Gmail app password")
    client = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        client.login(args.user, password)
        tally = {"INBOX": 0, "SPAM": 0, "OTHER": 0}
        total = 0
        # All Mail excludes Spam, so both folders have to be asked.
        for folder, where in ((GMAIL_ALL_MAIL, "ALL"), (GMAIL_SPAM, "SPAM")):
            uids = gmail_search_folder(client, folder, args.gmail_placement)
            if where == "SPAM":
                tally["SPAM"] += len(uids)
                total += len(uids)
                continue
            for uid in uids:
                tally[placement_of(gmail_labels(client, uid))] += 1
            total += len(uids)
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass
    print(f"search:  {args.gmail_placement!r} as {args.user}")
    print(f"matched: {total} message(s) (All Mail + Spam)")
    for where in ("INBOX", "SPAM", "OTHER"):
        print(f"  {where:<5} {tally[where]}")
    ok, detail = placement_verdict(tally, total, args.count)
    print(f"{'PASS' if ok else 'FAIL'}  placement  {detail}")
    return 0 if ok else 1



# ---------------------------------------------------------------------- records

ZONE = "odile.fyi"
DNS_IP4 = "203.0.113.139"
DNS_IP6 = "2001:0db8:0:1c9::1"
DMARC_VALUE = ("v=DMARC1; p=quarantine; adkim=s; aspf=s; pct=100; "
               "rua=mailto:dmarc@mail.odile.fyi")
CF_API = "https://api.cloudflare.com/client/v4"
CF_TOKEN_FILES = (
    "/root/.odile/cloudflare_dns_token",
)


def cf_token() -> str:
    """Read the Cloudflare token from disk; it never appears in argv or ps."""
    for path in CF_TOKEN_FILES:
        if path and os.path.exists(path):
            mode = oct(os.stat(path).st_mode & 0o777)[2:]
            if mode not in ("600", "400"):
                print(f"warning: {path} is mode {mode}; want 600", file=sys.stderr)
            with open(path, encoding="utf-8") as handle:
                return handle.read().strip()
    raise Fail(f"Cloudflare token: write one to {CF_TOKEN_FILES[0]} at mode 0600")


def cf(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        CF_API + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {cf_token()}",
            "Content-Type": "application/json",
            "User-Agent": "Odile-MailCheck/1.0 (odl-102)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read() or b"{}")
        errors = body.get("errors") or body
        raise Fail(f"Cloudflare {exc.code}: {errors}") from exc
    if not body.get("success", False):
        raise Fail(f"Cloudflare: {body.get('errors')}")
    return body.get("result", {})


def dkim_value() -> str:
    public = local_public_key()
    if not public:
        raise Fail(f"{DKIM_TXT} has no p= value")
    return f"v=DKIM1; k=rsa; p={public}"


def desired_records() -> list[dict]:
    """The seven rows §3 of the runbook states, in the machine's own terms."""
    return [
        {"type": "A", "name": DOMAIN, "content": DNS_IP4},
        {"type": "AAAA", "name": DOMAIN, "content": DNS_IP6},
        {"type": "TXT", "name": DOMAIN,
         "content": f"v=spf1 ip4:{DNS_IP4} ip6:{DNS_IP6} -all"},
        {"type": "MX", "name": DOMAIN, "content": DOMAIN, "priority": 10},
        {"type": "MX", "name": ZONE, "content": DOMAIN, "priority": 10},
        {"type": "TXT", "name": DKIM_DNS_NAME, "content": dkim_value()},
        {"type": "TXT", "name": DMARC_DNS_NAME, "content": DMARC_VALUE},
    ]


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def same_record(wanted: dict, existing: dict) -> bool:
    if unquote(str(existing.get("content", ""))) != unquote(str(wanted["content"])):
        return False
    if wanted["type"] == "MX":
        return int(existing.get("priority") or 0) == int(wanted["priority"])
    return True


def cf_zone_id() -> str:
    zones = cf(f"/zones?name={ZONE}")
    if not isinstance(zones, list) or not zones:
        raise Fail(f"the token cannot see the {ZONE} zone")
    return str(zones[0]["id"])


def cf_existing(zone_id: str) -> list[dict]:
    records, page = [], 1
    while True:
        batch = cf(f"/zones/{zone_id}/dns_records?per_page=100&page={page}")
        if not isinstance(batch, list) or not batch:
            break
        records.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return records


def plan_records(wanted: list[dict], existing: list[dict]) -> list[dict]:
    plan = []
    for item in wanted:
        matches = [
            record for record in existing
            if record.get("type") == item["type"]
            and str(record.get("name", "")).rstrip(".").lower() == item["name"].lower()
        ]
        if not matches:
            plan.append({"action": "create", "wanted": item})
        elif len(matches) > 1:
            plan.append({"action": "duplicate", "wanted": item, "existing": matches})
        elif same_record(item, matches[0]):
            plan.append({"action": "unchanged", "wanted": item, "existing": matches[0]})
        else:
            plan.append({"action": "update", "wanted": item, "existing": matches[0]})
    return plan


def cmd_apply_dns(args: argparse.Namespace) -> int:
    zone_id = cf_zone_id()
    plan = plan_records(desired_records(), cf_existing(zone_id))
    for row in plan:
        item = row["wanted"]
        shown = item["content"] if len(item["content"]) <= 60 else item["content"][:57] + "…"
        extra = f" prio {item['priority']}" if "priority" in item else ""
        print(f"{row['action']:<9} {item['type']:<4} {item['name']:<28} {shown}{extra}")
    if any(row["action"] == "duplicate" for row in plan):
        raise Fail("a record exists more than once; fix it by hand before applying")
    changes = [row for row in plan if row["action"] in ("create", "update")]
    if not changes:
        print("nothing to do; Cloudflare already matches the runbook")
        return 0
    if not args.apply:
        print(f"\ndry run: {len(changes)} change(s); pass --apply to write")
        return 0
    for row in changes:
        item = row["wanted"]
        payload = {"type": item["type"], "name": item["name"],
                   "content": item["content"], "ttl": 1}
        if item["type"] in ("A", "AAAA", "CNAME"):
            payload["proxied"] = False
        if "priority" in item:
            payload["priority"] = item["priority"]
        if row["action"] == "create":
            result = cf(f"/zones/{zone_id}/dns_records", method="POST", payload=payload)
        else:
            record_id = row["existing"]["id"]
            result = cf(f"/zones/{zone_id}/dns_records/{record_id}", method="PUT", payload=payload)
        print(f"  wrote {item['type']} {item['name']} id={result.get('id')}")
    print(f"\n{len(changes)} change(s) applied; nothing was deleted")
    return 0


# --------------------------------------------------------------------- commands

def cmd_check_dns(_: argparse.Namespace) -> int:
    failures = 0
    checks = 0

    def report(ok: bool, name: str, detail: str) -> None:
        nonlocal failures, checks
        checks += 1
        if not ok:
            failures += 1
        print(f"{'PASS' if ok else 'FAIL':<4}  {name:<11} {detail}")

    server = authoritative(DOMAIN)
    print(f"authority: {server or 'not found'}  (a local resolver may hold an old NXDOMAIN "
          f"for up to the SOA minimum)")
    addresses = a_records(DOMAIN)
    v6_addresses = aaaa_records(DOMAIN)
    report(bool(addresses), "A", ", ".join(addresses) or f"no A record for {DOMAIN}")
    report(bool(v6_addresses), "AAAA",
           ", ".join(v6_addresses) or f"no AAAA record for {DOMAIN}")

    spf_ok, spf_detail = check_spf(txt(DOMAIN), addresses + v6_addresses)
    report(spf_ok, "SPF", spf_detail)

    dkim_ok, dkim_detail = check_dkim(txt(DKIM_DNS_NAME), local_public_key())
    report(dkim_ok, "DKIM", dkim_detail)

    dmarc_ok, dmarc_detail = check_dmarc(txt(DMARC_DNS_NAME))
    report(dmarc_ok, "DMARC", dmarc_detail)

    # Both families carry mail (inet_protocols = all), so both need a
    # forward-confirmed PTR — a v4-only check would pass while v6 mail fails.
    for address in addresses + v6_addresses:
        fc_ok, fc_detail = check_fcrdns(address, DOMAIN)
        report(fc_ok, "PTR" if ":" not in address else "PTR v6", fc_detail)
    if not addresses and not v6_addresses:
        report(False, "PTR", "skipped: no address to check")

    if addresses:
        bounce_ok, bounce_detail = check_bounce_mx(DOMAIN, addresses[0])
        report(bounce_ok, "bounce MX", bounce_detail)
    else:
        report(False, "bounce MX", "skipped: no A record to check")

    apex_mx = mx(ZONE)
    report(bool(apex_mx), "reply-to MX",
           ", ".join(f"{prio} {host}" for prio, host in apex_mx)
           or "odile.fyi has no MX: replies to help@odile.fyi bounce")

    print(f"\n{checks} live checks, {failures} not satisfied")
    return 0 if failures == 0 else 1


def cmd_check_mta(_: argparse.Namespace) -> int:
    rows = check_mta()
    for ok, name, detail in rows:
        print(f"{'PASS' if ok else 'FAIL':<4}  {name:<18} {detail}")
    bad = sum(1 for ok, _, _ in rows if not ok)
    print(f"\n{len(rows)} checks, {bad} failed")
    return 0 if bad == 0 else 1


def cmd_verify_dkim(args: argparse.Namespace) -> int:
    rows = dkim_verify(args.verify_dkim, args.pubkey)
    for ok, name, detail in rows:
        print(f"{'PASS' if ok else 'FAIL':<4}  {name:<12} {detail}")
    return 0 if all(ok for ok, _, _ in rows) else 1


def cmd_send(args: argparse.Namespace) -> int:
    account = load_account()
    message = build_message(args.to, subject=args.subject or SUBJECT, account=account)
    nonce = os.urandom(6).hex()
    queue_id = submit(message, args.to, args.dry_run, account, nonce=nonce)
    print(f"envelope:   {verp(args.to, nonce=nonce)}")
    print(f"message-id: {message['Message-ID']}")
    print(f"queue id:   {queue_id}")
    if queue_id != "dry-run":
        print(f"next:       ./scripts/send_test.py --status {queue_id}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    verdict, lines = queue_status(args.status)
    print(f"queue {args.status}: {verdict}")
    for line in lines:
        print(f"  {line}")
    return 0 if verdict in ("sent", "delivered") else 1


# -------------------------------------------------------------------- self test

def self_test() -> int:
    """Offline checks. No network, no root, no MTA, no secrets."""
    global ptr_name, a_records, aaaa_records
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

    check("clean_txt joins quoted chunks",
          clean_txt('"v=spf1 " "ip4:203.0.113.4 -all"'), "v=spf1 ip4:203.0.113.4 -all")
    check("clean_txt leaves a bare value alone", clean_txt("p=abc"), "p=abc")
    check("SPF with our own IP passes",
          check_spf(["v=spf1 ip4:203.0.113.139 -all"], "203.0.113.139")[0], True)
    check("SPF without our IP fails",
          check_spf(["v=spf1 ip4:203.0.113.4 -all"], "203.0.113.139")[0], False)
    check("SPF with a third-party include fails",
          check_spf(["v=spf1 include:amazonses.com ~all"], "203.0.113.139")[0], False)
    check("SPF that is not -all fails",
          check_spf(["v=spf1 ip4:203.0.113.139 ~all"], "203.0.113.139")[0], False)
    check("two SPF records fail",
          check_spf(["v=spf1 ip4:203.0.113.139 -all", "v=spf1 -all"], "203.0.113.139")[0], False)
    v6 = "2001:0db8:0:1c9::1"
    check("SPF that authorises only v4 fails on the AAAA",
          check_spf(["v=spf1 ip4:203.0.113.139 -all"],
                    ["203.0.113.139", v6])[0], False)
    check("SPF that names both families passes",
          check_spf([f"v=spf1 ip4:203.0.113.139 ip6:{v6} -all"],
                    ["203.0.113.139", v6])[0], True)
    check("DKIM key of a full length passes",
          check_dkim(["v=DKIM1; k=rsa; p=" + "A" * 392])[0], True)
    check("DKIM truncation fails", check_dkim(["v=DKIM1; k=rsa; p=abc"])[0], False)
    check("DKIM key mismatch is caught",
          check_dkim(["v=DKIM1; k=rsa; p=" + "A" * 392], "B" * 392)[0], False)
    check("DMARC quarantine passes",
          check_dmarc(["v=DMARC1; p=quarantine; adkim=s; aspf=s; rua=mailto:d@x.y"])[0], True)
    check("DMARC p=none is not enough", check_dmarc(["v=DMARC1; p=none"])[0], False)
    check("DMARC without a policy fails", check_dmarc(["v=DMARC1; rua=mailto:a@b.c"])[0], False)
    check("DMARC without reports fails",
          check_dmarc(["v=DMARC1; p=quarantine; adkim=s; aspf=s"])[0], False)
    check("DMARC without strict alignment fails",
          check_dmarc(["v=DMARC1; p=quarantine; rua=mailto:d@x.y"])[0], False)

    real_ptr = ptr_name
    real_a = a_records
    real_aaaa = aaaa_records
    try:
        ptr_name = lambda ip: "hostfake.local"  # noqa: E731
        a_records = lambda name: ["203.0.113.139"]  # noqa: E731
        aaaa_records = lambda name: []  # noqa: E731
        check("placeholder PTR fails", check_fcrdns("203.0.113.139", DOMAIN)[0], False)
        ptr_name = lambda ip: DOMAIN  # noqa: E731
        a_records = lambda name: ["10.0.0.1"]  # noqa: E731
        check("PTR that does not forward-resolve fails",
              check_fcrdns("203.0.113.139", DOMAIN)[0], False)
        a_records = lambda name: ["203.0.113.139"]  # noqa: E731
        check("forward-confirmed PTR passes", check_fcrdns("203.0.113.139", DOMAIN)[0], True)
        a_records = lambda name: []  # noqa: E731
        aaaa_records = lambda name: [v6]  # noqa: E731
        check("forward-confirmed PTR passes for IPv6", check_fcrdns(v6, DOMAIN)[0], True)
    finally:
        ptr_name = real_ptr
        a_records = real_a
        aaaa_records = real_aaaa

    check("VERP is stable within a day", verp("a@b.c", "20261106"), verp("A@B.C", "20261106"))
    check("VERP changes with the day",
          verp("a@b.c", "20261106") == verp("a@b.c", "20261107"), False)
    check("VERP is on our domain", verp("a@b.c").endswith("@" + DOMAIN), True)

    account = {"from": FROM, "reply_to": REPLY_TO}
    message = build_message("someone@gmail.com", account=account)
    check("From is the invite address", message["From"], FROM)
    check("Reply-To is the support address", message["Reply-To"], REPLY_TO)
    check("message is plain text only", message.get_content_type(), "text/plain")
    check("soak subject is identifiable",
          build_message("a@b.c", subject=f"{SOAK_SUBJECT} 3/24")["Subject"],
          "Odile soak 3/24")

    recorded = [
        {"at": "2026-11-06T15:49:51+00:00", "queue_id": "AAA", "verdict": "sent", "dsn": "2.0.0"},
        {"at": "2026-11-06T16:19:54+00:00", "queue_id": "BBB", "verdict": "sent", "dsn": "2.0.0"},
    ]
    tally, oldest, refreshed = classify_soak([dict(r) for r in recorded], [])
    check("a rotated log keeps the verdict the soak recorded",
          (tally["sent"], tally["unknown"]), (2, 0))
    check("a rotated log refreshes nothing and leaves no 4xx", (refreshed, oldest), (0, None))
    tally, oldest, refreshed = classify_soak(
        [dict(r) for r in recorded],
        ["2026-11-06T15:49:52.001234+00:00 host postfix/smtp[1]: AAA: to=<x@y>,"
         " dsn=4.7.1, status=deferred (451 4.7.1 try later)\n"])
    check("a surviving log line overrides the recorded send",
          (tally["deferred"], tally["sent"], refreshed), (1, 1, 1))
    check("an open 4xx is aged from its log line",
          oldest.isoformat() if oldest else None, "2026-11-06T15:49:52.001234+00:00")
    check("a 24-message soak in the inbox passes",
          placement_verdict({"INBOX": 24, "SPAM": 0, "OTHER": 0}, 24, 24)[0], True)
    check("a message in spam fails placement",
          placement_verdict({"INBOX": 23, "SPAM": 1, "OTHER": 0}, 24, 24)[0], False)
    check("a message filed elsewhere fails placement",
          placement_verdict({"INBOX": 23, "SPAM": 0, "OTHER": 1}, 24, 24)[0], False)
    check("a short placement tally is not a pass",
          placement_verdict({"INBOX": 12, "SPAM": 0, "OTHER": 0}, 12, 24)[0], False)
    check("a fully sent 24-message soak passes",
          soak_verdict({"sent": 24, "deferred": 0, "bounced": 0, "unknown": 0},
                       24, 24, None)[0], True)
    check("a one-message log is not a 24-message soak",
          soak_verdict({"sent": 1, "deferred": 0, "bounced": 0, "unknown": 0},
                       1, 24, None)[0], False)
    check("an open 4xx older than an hour fails the soak",
          soak_verdict({"sent": 23, "deferred": 1, "bounced": 0, "unknown": 0},
                       24, 24, 90)[0], False)

    check("queue id is parsed from a real reply",
          re.search(r"queued as ([0-9A-Za-z]+)", "2.0.0 Ok: queued as 8B0311AA1E3").group(1),
          "8B0311AA1E3")
    check("dsn 2.x is read from a delivery",
          dsn_of(["2026-11-06T15:31:07 postfix/smtp: status=sent (250 2.0.0 OK)"]), "2.0.0")
    check("dsn 4.x is read from a deferral",
          dsn_of(["2026-11-06T15:31:07 postfix/smtp: status=deferred (451 4.7.1 try later)"]),
          "4.7.1")

    oversigned = build_message("someone@gmail.com", account=account)
    picked = [name for name, _ in select_signed_headers(
        oversigned, ["From", "To", "Subject", "Date", "From"])]
    gmailish = (b"Return-Path: <b-1@mail.odile.fyi>\r\n"
                b"Authentication-Results: mx.google.com;\r\n"
                b" dkim=pass header.i=@mail.odile.fyi header.s=odile;\r\n"
                b" spf=pass smtp.mailfrom=b-1@mail.odile.fyi;\r\n"
                b" dmarc=pass (p=QUARANTINE) header.from=mail.odile.fyi\r\n"
                b"DKIM-Signature: v=1; a=rsa-sha256;\r\n\td=mail.odile.fyi; s=odile;\r\n"
                b"Received: from mail.odile.fyi by mx.google.com\r\n\r\nbody\r\n")
    fields = evidence_headers(gmailish)
    check("all three verdicts are read", verdicts(fields["Authentication-Results"]),
          {"spf": "pass", "dkim": "pass", "dmarc": "pass"})
    check("placement prefers the inbox", placement_of(["\\Inbox", "\\All", "\\Important"]), "INBOX")
    check("placement sees spam", placement_of(["\\Spam", "\\All"]), "SPAM")
    # Measured 2026-11-22 (t_7378e496): a copy in [Gmail]/Spam answers
    # X-GM-LABELS (), so labels alone called a quarantined brief OTHER.
    check("an unlabelled copy in the Spam folder is spam",
          placement_of([], '"[Gmail]/Spam"'), "SPAM")
    check("an unlabelled copy in the INBOX folder is the inbox",
          placement_of([], "INBOX"), "INBOX")
    check("an unlabelled copy in All Mail is still other",
          placement_of([], '"[Gmail]/All Mail"'), "OTHER")
    check("an em dash subject falls back to its ASCII run",
          ascii_search_term("Odile — test invite (ODL-102)"), "test invite (ODL-102)")
    check("a folded DKIM header is unfolded",
          fields["DKIM-Signature"].startswith("v=1; a=rsa-sha256; d=mail.odile.fyi"), True)
    check("a dkim=fail is reported as such",
          verdicts("mx.google.com; dkim=fail header.d=mail.odile.fyi").get("dkim"), "fail")
    check("Return-Path is kept whole", fields["Return-Path"], "<b-1@mail.odile.fyi>")

    check("a repeated h= reference with no instance left is dropped",
          picked, ["From", "To", "Subject", "Date"])
    picked = [name for name, _ in select_signed_headers(
        oversigned, ["From", "From"])]
    check("the first reference still finds the only instance", picked, ["From"])

    check("TXT quoting is ignored when comparing",
          same_record({"type": "TXT", "content": "v=spf1 -all"},
                      {"content": '"v=spf1 -all"', "type": "TXT"}), True)
    check("a different MX priority is a change",
          same_record({"type": "MX", "content": DOMAIN, "priority": 10},
                      {"content": DOMAIN, "priority": 20, "type": "MX"}), False)
    check("a missing record is a create",
          [row["action"] for row in plan_records(
              [{"type": "A", "name": DOMAIN, "content": "203.0.113.4"}], [])], ["create"])
    check("an identical record is unchanged",
          [row["action"] for row in plan_records(
              [{"type": "A", "name": DOMAIN, "content": "203.0.113.4"}],
              [{"type": "A", "name": DOMAIN, "content": "203.0.113.4", "id": "x"}])], ["unchanged"])

    check("relaxed body squeezes runs and strips trailing WSP",
          relaxed_body(b"a  b \r\nc\r\n\r\n\r\n"), b"a b\r\nc\r\n")
    check("relaxed body of an empty body is a single CRLF", relaxed_body(b"\r\n\r\n"), b"\r\n")
    # RFC 6376 3.4.2 strips WSP around the value, so there is no space after ':'.
    check("relaxed header unfolds and lowercases",
          relaxed_header("Subject", " one\r\n\ttwo "), "subject:one two")
    check("relaxed header keeps the value on one line",
          relaxed_header("From", "Odile <invite@mail.odile.fyi>"),
          "from:Odile <invite@mail.odile.fyi>")

    print(f"\n{count} checks, {failures} failed")
    return 0 if failures == 0 else 1


# ------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Exercise Odile's self-hosted mail path.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"The only secret is the submission account at {ACCOUNT_FILE} "
               f"(0600). See docs/runbooks/email.md.",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--self-test", action="store_true", help="offline logic checks")
    action.add_argument("--check-dns", action="store_true", help="live records and PTR")
    action.add_argument("--check-mta", action="store_true", help="services, milter, submission")
    action.add_argument("--verify-dkim", metavar="FILE", help="verify a signed message")
    action.add_argument("--send", action="store_true", help="send the test invitation")
    action.add_argument("--status", metavar="ID", help="what happened to a queued message")
    action.add_argument("--soak", action="store_true", help="send a timed soak")
    action.add_argument("--soak-report", metavar="FILE", help="summarise a soak log")
    action.add_argument("--gmail-headers", metavar="SUBJECT",
                        help="read a delivered message's headers over IMAP (needs an app password)")
    action.add_argument("--gmail-placement", metavar="SUBJECT",
                        help="tally INBOX vs SPAM for every message matching a subject")
    action.add_argument("--apply-dns", action="store_true",
                        help="write the seven records at Cloudflare")
    parser.add_argument("--to", metavar="ADDRESS", help="recipient (a Gmail address)")
    parser.add_argument("--subject", metavar="TEXT", help="override the subject for --send")
    parser.add_argument("--count", type=int, default=24, help="messages in a soak (default 24)")
    parser.add_argument("--start", type=int, default=1, help="first message number (resume a soak)")
    parser.add_argument("--interval", type=int, default=1800,
                        help="seconds between soak messages (default 1800)")
    parser.add_argument("--log", default=SOAK_LOG, help=f"soak JSONL path (default {SOAK_LOG})")
    parser.add_argument("--pubkey", metavar="FILE", help="public key for --verify-dkim")
    parser.add_argument("--user", metavar="ADDRESS", default=GMAIL_USER,
                        help=f"IMAP account for --gmail-headers (default {GMAIL_USER})")
    parser.add_argument("--dry-run", action="store_true", help="print the message, send nothing")
    parser.add_argument("--apply", action="store_true", help="write changes; without it, dry run")
    args = parser.parse_args(argv)

    try:
        if args.self_test:
            return self_test()
        if args.check_dns:
            return cmd_check_dns(args)
        if args.check_mta:
            return cmd_check_mta(args)
        if args.verify_dkim:
            return cmd_verify_dkim(args)
        if args.apply_dns:
            return cmd_apply_dns(args)
        if args.gmail_headers:
            return cmd_gmail_headers(args)
        if args.gmail_placement:
            return cmd_gmail_placement(args)
        if args.status:
            return cmd_status(args)
        if args.soak_report:
            return soak_report(args.soak_report, args.count)
        if args.send:
            if not args.to:
                raise Fail("--send needs --to <gmail address>")
            return cmd_send(args)
        if args.soak:
            if not args.to:
                raise Fail("--soak needs --to <gmail address>")
            return cmd_soak(args)
        parser.print_help()
        return 2
    except Fail as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
