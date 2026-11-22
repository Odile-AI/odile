#!/usr/bin/python3
"""Send today's brief to its owner, and record what happened.

The brief is written on the tenant by the ``Morning brief`` cron job; this
script is the delivery half. It reads the job's own dispatch record to know the
owner's zone, the due time and whether this run was late, composes a plain-text
message from ``~/.hermes/brief.txt``, sends it through Odile's relay as
``brief-<slug>@mail.odile.fyi`` with a VERP envelope and ``Reply-To:
help@odile.fyi``, and records the outcome in ``/var/lib/odile/brief.json`` so
the signed heartbeat carries ``last_sent_at`` to HQ and a missed brief becomes
``brief_missing``.

Lateness is read from Hermes, never guessed by a model: the job record's
``last_dispatch`` holds ``scheduled_at``, ``dispatched_at``, ``lateness_seconds``
and ``kind`` (``on_time``, ``late`` or ``catch_up``). If this dispatch was
late, the email says so with the time and the size of the delay.

    sudo -n /usr/bin/python3 ~/.hermes/scripts/send_brief.py
    ... --dry-run                       # compose and print; no send, no write
    ... --date 2026-11-10 --now 1794323000 --template templates/brief.txt

Run it with sudo in the tenant: the seed (relay credential) is root-owned and
``/var/lib/odile`` is root-owned. The owner's home is resolved from
``$SUDO_USER`` unless ``--home`` is given.

Exit codes: 0 sent; 1 the send failed; 2 a missing or unusable setting (no
address, no job, no zone); 3 no brief for that date; 4 the relay, seed,
template or CA could not be read; 5 the status file could not be written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import secrets
import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_SEED = Path("/etc/odile/seed.json")
DEFAULT_STATUS = Path("/var/lib/odile/brief.json")
DEFAULT_JOB = "Morning brief"
DEFAULT_DISPATCH_WINDOW_SECONDS = 3600
DEFAULT_TEMPLATE = Path(".hermes") / "brief.txt"
REPLY_TO = "help@odile.fyi"
FROM_DOMAIN = "mail.odile.fyi"
SMTP_TIMEOUT_SECONDS = 30
ADDRESS_RE = re.compile(r"^[^@\s<>,;:]+@[^@\s<>,;:]+\.[^@\s<>,;:]+$")
CRON_DAILY_RE = re.compile(r"^\s*(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*\s*$")


class BriefError(Exception):
    """A setting or input this run cannot use; carries the exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Relay:
    host: str
    port: int
    username: str
    secret: str
    tls_ca: str | None = None  # a PEM body, or a path to one


@dataclass(frozen=True)
class Job:
    name: str
    zone: ZoneInfo
    due: str | None
    dispatch: dict | None


def owner_home() -> Path:
    """The home of the user whose brief this is.

    Under sudo (the tenant path) HOME is root's; the invoking owner is
    SUDO_USER. In a test or a plain run the current home is right.
    """
    explicit = os.environ.get("ODILE_BRIEF_HOME")
    if explicit:
        return Path(explicit)
    user = os.environ.get("SUDO_USER")
    if user and os.geteuid() == 0:
        try:
            return Path(pwd.getpwnam(user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def load_json(path: Path, what: str) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BriefError(f"cannot read the {what} at {path}: {error}", 4) from None
    try:
        value = json.loads(text)
    except ValueError as error:
        raise BriefError(f"the {what} at {path} is not valid JSON: {error}", 4) from None
    if not isinstance(value, dict):
        raise BriefError(f"the {what} at {path} is not a JSON object", 4)
    return value


def load_relay(seed_path: Path, tls_ca: Path | None) -> tuple[Relay, str]:
    seed = load_json(seed_path, "signed seed")
    slug = seed.get("slug")
    if not isinstance(slug, str) or not slug:
        raise BriefError(f"the signed seed at {seed_path} has no slug", 4)
    mail = seed.get("mail_relay")
    if not isinstance(mail, dict):
        raise BriefError(f"the signed seed at {seed_path} has no mail_relay", 4)
    host = mail.get("host")
    port = mail.get("port")
    username = mail.get("username")
    secret = mail.get("secret")
    if not (isinstance(host, str) and host and isinstance(username, str) and username
            and isinstance(secret, str) and secret):
        raise BriefError(f"the signed seed at {seed_path} has an incomplete mail_relay", 4)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise BriefError(f"the signed seed at {seed_path} has an invalid mail_relay.port", 4)
    pinned = str(tls_ca) if tls_ca else None
    if pinned is None:
        declared = mail.get("tls_ca")
        if isinstance(declared, str) and declared.strip():
            pinned = declared.strip()
    return Relay(host=host, port=port, username=username, secret=secret, tls_ca=pinned), slug


def load_job(jobs_path: Path, name: str) -> Job:
    document = load_json(jobs_path, "cron jobs file")
    jobs = document.get("jobs")
    if not isinstance(jobs, list):
        raise BriefError(f"the cron jobs file at {jobs_path} has no jobs list", 4)
    for job in jobs:
        if isinstance(job, dict) and job.get("name") == name:
            zone_name = job.get("timezone")
            if not isinstance(zone_name, str) or not zone_name:
                raise BriefError(f"the {name} job has no timezone", 2)
            try:
                zone = ZoneInfo(zone_name)
            except (ZoneInfoNotFoundError, ValueError):
                raise BriefError(f"the {name} job names an unknown timezone {zone_name!r}", 2) from None
            due = None
            schedule = job.get("schedule")
            if isinstance(schedule, dict):
                match = CRON_DAILY_RE.match(str(schedule.get("expr") or ""))
                if match:
                    minute, hour = int(match.group(1)), int(match.group(2))
                    if 0 <= minute < 60 and 0 <= hour < 24:
                        due = f"{hour:02d}:{minute:02d}"
            dispatch = job.get("last_dispatch")
            return Job(
                name=name,
                zone=zone,
                due=due,
                dispatch=dispatch if isinstance(dispatch, dict) else None,
            )
    raise BriefError(f"no job named {name!r} in {jobs_path}", 2)


def load_address(path: Path) -> str:
    if not path.is_file():
        raise BriefError(f"no brief address file at {path}", 2)
    document = load_json(path, "brief address file")
    address = document.get("to")
    if not isinstance(address, str) or not ADDRESS_RE.match(address.strip()):
        raise BriefError(f"the brief address file at {path} has no usable 'to'", 2)
    return address.strip()


def reduce_markdown(text: str) -> str:
    """The brief's markdown as plain text: headings, emphasis, links, code."""
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1 (\2)", line)
        line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", line)
        line = line.replace("**", "").replace("__", "")
        line = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", line)
        line = re.sub(r"`([^`\n]+)`", r"\1", line)
        lines.append(line.rstrip())
    body = "\n".join(lines).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body


def late_sentence(job: Job, now: datetime) -> str:
    """The one sentence a late brief carries, from Hermes's dispatch record."""
    dispatch = job.dispatch
    if not dispatch or not job.due:
        return ""
    try:
        dispatched_at = datetime.fromisoformat(str(dispatch.get("dispatched_at")))
    except (TypeError, ValueError):
        return ""
    if dispatched_at.tzinfo is None:
        dispatched_at = dispatched_at.replace(tzinfo=timezone.utc)
    if abs((now - dispatched_at).total_seconds()) > DEFAULT_DISPATCH_WINDOW_SECONDS:
        return ""
    if dispatch.get("kind") not in ("late", "catch_up"):
        return ""
    try:
        lateness = max(0.0, float(dispatch.get("lateness_seconds") or 0))
    except (TypeError, ValueError):
        lateness = 0.0
    minutes = max(1, round(lateness / 60))
    if minutes < 90:
        span = f"{minutes} minute" + ("" if minutes == 1 else "s")
    else:
        hours, rest = divmod(minutes, 60)
        span = f"{hours} hour" + ("" if hours == 1 else "s")
        if rest:
            span += f" {rest} minute" + ("" if rest == 1 else "s")
    local = dispatched_at.astimezone(job.zone).strftime("%H:%M")
    return f"This one ran at {local} — {span} after its {job.due} slot.\n\n"


def compose(template_text: str, *, brief: str, job: Job, slug: str, now: datetime,
            late: str) -> tuple[str, str]:
    local = now.astimezone(job.zone)
    today = local.date()
    fields = {
        "weekday": today.strftime("%A"),
        "day": str(today.day),
        "month": today.strftime("%B"),
        "brief": brief,
        "late": late,
        "url": f"https://{slug}.odile.fyi",
        "date": today.isoformat(),
        "time": local.strftime("%H:%M"),
        "zone": job.zone.key,
        "slug": slug,
    }
    try:
        rendered = template_text.format(**fields)
    except KeyError as error:
        raise BriefError(f"the brief template names an unknown field {error}", 4) from None
    subject, _, body = rendered.partition("\n")
    if not subject.startswith("subject: "):
        raise BriefError("the brief template's first line must be 'subject: …'", 4)
    return subject[len("subject: "):].strip(), body.lstrip("\n").rstrip() + "\n"


def verp_address(slug: str, day: str, recipient: str) -> str:
    digest = hashlib.sha256(
        f"{slug}|{day}|{recipient}|{secrets.token_hex(8)}".encode("utf-8")
    ).hexdigest()[:12]
    return f"b-{day.replace('-', '')}-{digest}@{FROM_DOMAIN}"


def build_message(*, subject: str, body: str, sender: str, recipient: str, now: datetime,
                  slug: str, day: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = f"Odile <{sender}>"
    message["To"] = recipient
    message["Reply-To"] = REPLY_TO
    message["Subject"] = subject
    message["Date"] = formatdate(int(now.timestamp()), localtime=False)
    message["Message-ID"] = make_msgid(
        idstring=f"brief.{slug}.{day.replace('-', '')}", domain=FROM_DOMAIN
    )
    message.set_content(body)
    return message


def read_status(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_status(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def smtp_context(relay: Relay) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if relay.tls_ca is None:
        return context
    try:
        if "BEGIN CERTIFICATE" in relay.tls_ca:
            context.load_verify_locations(cadata=relay.tls_ca)
        else:
            context.load_verify_locations(cafile=relay.tls_ca)
    except (OSError, ssl.SSLError, ValueError) as error:
        where = "the seed" if "BEGIN CERTIFICATE" in relay.tls_ca else relay.tls_ca
        raise BriefError(f"cannot load the relay CA from {where}: {error}", 4) from None
    return context


def send(relay: Relay, message: EmailMessage, *, envelope_from: str, recipient: str) -> None:
    context = smtp_context(relay)
    with smtplib.SMTP(relay.host, relay.port, timeout=SMTP_TIMEOUT_SECONDS) as client:
        client.ehlo()
        client.starttls(context=context)
        client.ehlo()
        client.login(relay.username, relay.secret)
        client.send_message(message, from_addr=envelope_from, to_addrs=[recipient])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="send_brief.py",
        description="Send today's brief through Odile's relay and record the outcome.",
    )
    parser.add_argument("--home", type=Path, help="the owner's home (default: $SUDO_USER's, else $HOME)")
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED, help="signed seed (relay + slug)")
    parser.add_argument("--jobs", type=Path, help="Hermes cron jobs file (default: <home>/.hermes/cron/jobs.json)")
    parser.add_argument("--job", default=DEFAULT_JOB, help=f"the job whose dispatch is this run (default: {DEFAULT_JOB})")
    parser.add_argument("--address", type=Path, help="address file with {\"to\": …} (default: <home>/.hermes/brief.json)")
    parser.add_argument("--template", type=Path, help="message template (default: <home>/.hermes/brief.txt)")
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS, help="status file the heartbeat reads")
    parser.add_argument("--brief", type=Path, help="the brief file (default: <home>/briefs/<date>.md)")
    parser.add_argument("--date", help="the brief's date, YYYY-MM-DD (default: today in the job's zone)")
    parser.add_argument("--now", type=int, help="the run time as an epoch (default: now)")
    parser.add_argument("--tls-ca", type=Path, help="CA to pin for STARTTLS (overrides the seed)")
    parser.add_argument("--dry-run", action="store_true", help="compose and print; no send, no status write")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = datetime.fromtimestamp(args.now if args.now is not None else datetime.now(timezone.utc).timestamp(),
                                 tz=timezone.utc)
    home = args.home or owner_home()
    jobs_path = args.jobs or home / ".hermes" / "cron" / "jobs.json"
    address_path = args.address or home / ".hermes" / "brief.json"
    template_path = args.template or home / ".hermes" / "brief.txt"

    try:
        relay, slug = load_relay(args.seed, args.tls_ca)
        job = load_job(jobs_path, args.job)
        recipient = load_address(address_path)
        day = args.date or now.astimezone(job.zone).date().isoformat()
        brief_path = args.brief or home / "briefs" / f"{day}.md"
        previous = read_status(args.status)
        attempt = {
            "version": 1,
            "slug": slug,
            "date": day,
            "to": recipient,
            "last_attempt_at": int(now.timestamp()),
            "last_sent_at": previous.get("last_sent_at"),
            "ok": False,
            "error": None,
            "late_seconds": 0,
            "message_id": None,
        }
        try:
            brief_text = brief_path.read_text(encoding="utf-8")
        except OSError as error:
            attempt["error"] = f"no brief for {day} at {brief_path}"
            if not args.dry_run:
                _record_failure(args.status, attempt)
            raise BriefError(f"no brief for {day} at {brief_path}: {error}", 3) from None
        try:
            template_text = template_path.read_text(encoding="utf-8")
        except OSError as error:
            raise BriefError(f"cannot read the brief template at {template_path}: {error}", 4) from None
        subject, body = compose(
            template_text,
            brief=reduce_markdown(brief_text),
            job=job,
            slug=slug,
            now=now,
            late=late_sentence(job, now),
        )
        sender = f"brief-{slug}@{FROM_DOMAIN}"
        message = build_message(
            subject=subject, body=body, sender=sender, recipient=recipient,
            now=now, slug=slug, day=day,
        )
        envelope = verp_address(slug, day, recipient)

        if args.dry_run:
            print(f"From:      {message['From']}")
            print(f"To:        {message['To']}")
            print(f"Reply-To:  {message['Reply-To']}")
            print(f"Subject:   {message['Subject']}")
            print(f"Date:      {message['Date']}")
            print(f"Message-ID:{message['Message-ID']}")
            print(f"Envelope:  {envelope}")
            print(f"Relay:     {relay.host}:{relay.port} as {relay.username}")
            print()
            print(body, end="")
            return 0

        previous = read_status(args.status)
        last_sent = previous.get("last_sent_at")
        attempt.update({"error": None, "last_sent_at": last_sent, "message_id": message["Message-ID"]})
        try:
            send(relay, message, envelope_from=envelope, recipient=recipient)
        except (OSError, smtplib.SMTPException) as error:
            attempt["error"] = str(error)[:300]
            try:
                write_status(args.status, attempt)
            except OSError as write_error:
                print(f"brief: send failed ({error}); status not written to {args.status}: {write_error}",
                      file=sys.stderr)
                return 5
            print(f"brief: send failed: {error}", file=sys.stderr)
            return 1

        attempt["ok"] = True
        attempt["last_sent_at"] = int(now.timestamp())
        attempt["late_seconds"] = _lateness_seconds(job, now)
        try:
            write_status(args.status, attempt)
        except OSError as error:
            print(f"brief: sent to {recipient}, but the status was not written to {args.status}: {error}",
                  file=sys.stderr)
            return 5
        local = now.astimezone(job.zone).strftime("%H:%M")
        print(f"brief: sent to {recipient} at {local} {job.zone.key} ({message['Message-ID']})")
        return 0
    except BriefError as error:
        print(f"brief: {error}", file=sys.stderr)
        return error.code


def _record_failure(path: Path, record: dict) -> None:
    """Best effort: a failure record must never mask the failure itself."""
    try:
        write_status(path, record)
    except OSError as error:
        print(f"brief: status not written to {path}: {error}", file=sys.stderr)


def _lateness_seconds(job: Job, now: datetime) -> int:
    dispatch = job.dispatch
    if not dispatch:
        return 0
    try:
        dispatched_at = datetime.fromisoformat(str(dispatch.get("dispatched_at")))
    except (TypeError, ValueError):
        return 0
    if dispatched_at.tzinfo is None:
        dispatched_at = dispatched_at.replace(tzinfo=timezone.utc)
    if abs((now - dispatched_at).total_seconds()) > DEFAULT_DISPATCH_WINDOW_SECONDS:
        return 0
    try:
        return max(0, int(float(dispatch.get("lateness_seconds") or 0)))
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
