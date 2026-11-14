#!/usr/bin/python3
"""Turn delivery reports into records, so a bounce is read by a program.

Every invite leaves with a VERP Return-Path. A rejection comes back as an
RFC 3464 delivery-status report in the bounce mailbox; a complaint comes back
as an RFC 5965 feedback report; a DMARC aggregate report arrives there too and
is not a bounce at all. This script tells them apart, writes one JSON record
per report to a JSONL file, and never moves or deletes the mail.

    ./scripts/mail_dsn.py                 parse the bounce mailbox, append new records
    ./scripts/mail_dsn.py --dry-run       print what would be appended
    ./scripts/mail_dsn.py --file FILE     parse one message and print the record

The JSONL file is the durable record: `events.kind` has no mail kind, so a
bounce lives beside the waitlist database rather than inside it.

Standard library only. Idempotent: a report is keyed by its Message-ID (or a
hash of the bytes when it has none), so running the script twice appends
nothing the second time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path

DSN_MAILDIR = "/var/mail/dsn/Maildir"
OUT_PATH = "/var/lib/odile/mail/dsn.jsonl"


def _unfold(value: str) -> str:
    return " ".join(value.split())


def _address(value: str | None) -> str | None:
    """`rfc822; someone@example.com` / `<someone@example.com>` -> the address."""
    if not value:
        return None
    text = _unfold(value)
    text = re.sub(r"^(rfc822|utf-8|dns)\s*;\s*", "", text, flags=re.I)
    text = text.strip().strip("<>").strip()
    # A group syntax recipient can carry a display name; keep the angle address.
    match = re.search(r"<([^<>]+)>", text)
    if match:
        return match.group(1).strip()
    return text or None


def _fields(part: Message) -> dict[str, str]:
    """Headers of one delivery-status block, lowercased and unfolded."""
    return {name.lower(): _unfold(str(value)) for name, value in part.items()}


def _delivery_blocks(message: Message) -> list[Message]:
    blocks: list[Message] = []
    for part in message.walk():
        if part.get_content_type() != "message/delivery-status":
            continue
        payload = part.get_payload()
        if isinstance(payload, list):
            blocks.extend(item for item in payload if isinstance(item, Message))
    return blocks


def _is_recipient_block(fields: dict[str, str]) -> bool:
    return any(key in fields for key in ("final-recipient", "original-recipient", "action", "status"))


def _classify(recipients: list[dict]) -> str:
    statuses = [item.get("status") or "" for item in recipients]
    actions = [(item.get("action") or "").lower() for item in recipients]
    if any(status.startswith("5") for status in statuses) or "failed" in actions:
        return "hard"
    if any(status.startswith("4") for status in statuses) or "delayed" in actions:
        return "soft"
    return "unknown"


def _dsn_record(message: Message) -> dict | None:
    blocks = _delivery_blocks(message)
    if not blocks:
        return None
    per_message: dict[str, str] = {}
    recipients: list[dict] = []
    for block in blocks:
        fields = _fields(block)
        if _is_recipient_block(fields):
            recipients.append(
                {
                    "recipient": _address(fields.get("original-recipient") or fields.get("final-recipient")),
                    "final_recipient": _address(fields.get("final-recipient")),
                    "action": fields.get("action"),
                    "status": fields.get("status"),
                    "remote_mta": _address(fields.get("remote-mta")),
                    "diagnostic": fields.get("diagnostic-code"),
                }
            )
        else:
            per_message = fields
    if not recipients:
        return None
    # The card asks for "the original recipient and the SMTP status": name the
    # one that failed, not the one that was merely delayed.
    failed = next(
        (item for item in recipients if (item.get("status") or "").startswith("5")),
        recipients[0],
    )
    return {
        "kind": "dsn",
        "bounce": _classify(recipients),
        "recipient": failed.get("recipient"),
        "status": failed.get("status"),
        "original_sender": _address(per_message.get("x-postfix-sender")),
        "queue_id": per_message.get("x-postfix-queue-id"),
        "reporting_mta": _address(per_message.get("reporting-mta")),
        "arrival_date": per_message.get("arrival-date"),
        "recipients": recipients,
        "message_id": _unfold(str(message.get("Message-ID", ""))) or None,
        "subject": _unfold(str(message.get("Subject", ""))) or None,
    }


def _feedback_record(message: Message) -> dict | None:
    report: dict[str, str] = {}
    for part in message.walk():
        if part.get_content_type() != "message/feedback-report":
            continue
        payload = part.get_payload()
        if isinstance(payload, list) and payload and isinstance(payload[0], Message):
            report = _fields(payload[0])
        else:
            report = _fields(part)
        break
    if not report:
        return None
    return {
        "kind": "complaint",
        "feedback_type": report.get("feedback-type"),
        "original_recipient": _address(report.get("original-rcpt-to")),
        "original_sender": _address(report.get("original-mail-from")),
        "source_ip": report.get("source-ip"),
        "reported_domain": report.get("reported-domain"),
        "authentication_results": report.get("authentication-results"),
        "arrival_date": report.get("arrival-date"),
        "user_agent": report.get("user-agent"),
        "message_id": _unfold(str(message.get("Message-ID", ""))) or None,
        "subject": _unfold(str(message.get("Subject", ""))) or None,
    }


def parse_report(raw: bytes) -> dict | None:
    """One message in, one record out; None when it is not a bounce or complaint."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if message.get_content_type() != "multipart/report":
        return None
    report_type = str(message.get_param("report-type") or "").lower()
    if report_type == "delivery-status":
        return _dsn_record(message)
    if report_type == "feedback-report":
        return _feedback_record(message)
    # report-type=dmarc (aggregate reports), disposition-notification and
    # anything else that lands in this mailbox is not a bounce.
    return None


def parse_file(path: str | os.PathLike[str]) -> dict | None:
    with open(path, "rb") as handle:
        return parse_report(handle.read())


def iter_maildir(base: str | os.PathLike[str]):
    """Every message in new/ and cur/, in a stable order. Nothing is moved."""
    root = Path(base)
    for sub in ("new", "cur"):
        directory = root / sub
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.is_file():
                yield path


def record_key(record: dict, raw: bytes) -> str:
    if record.get("message_id"):
        return str(record["message_id"])
    return hashlib.sha256(raw).hexdigest()


def load_keys(out: Path) -> set[str]:
    keys: set[str] = set()
    if not out.exists():
        return keys
    with open(out, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("key"):
                keys.add(str(record["key"]))
    return keys


def process(maildir: str | os.PathLike[str] = DSN_MAILDIR,
            out: str | os.PathLike[str] = OUT_PATH,
            dry_run: bool = False) -> dict:
    out_path = Path(out)
    seen = load_keys(out_path)
    counts = {"scanned": 0, "dsn": 0, "complaint": 0, "ignored": 0,
              "duplicate": 0, "appended": 0}
    writable = None if dry_run else out_path
    for path in iter_maildir(maildir):
        raw = path.read_bytes()
        counts["scanned"] += 1
        record = parse_report(raw)
        if not record:
            counts["ignored"] += 1
            continue
        counts[record["kind"]] += 1
        key = record_key(record, raw)
        if key in seen:
            counts["duplicate"] += 1
            continue
        seen.add(key)
        record["key"] = key
        record["source"] = path.name
        record["parsed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if dry_run:
            print(json.dumps(record, ensure_ascii=False, sort_keys=True))
            continue
        if writable is not None:
            writable.parent.mkdir(parents=True, exist_ok=True)
            with open(writable, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        counts["appended"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse delivery reports into JSONL records (never moves mail)."
    )
    parser.add_argument("--maildir", default=DSN_MAILDIR,
                        help=f"Maildir to read (default {DSN_MAILDIR})")
    parser.add_argument("--out", default=OUT_PATH,
                        help=f"JSONL record file (default {OUT_PATH})")
    parser.add_argument("--file", metavar="FILE", help="parse one message and print it")
    parser.add_argument("--dry-run", action="store_true", help="print records, write nothing")
    args = parser.parse_args(argv)

    if args.file:
        record = parse_file(args.file)
        if not record:
            print(f"{args.file}: not a delivery report or feedback report", file=sys.stderr)
            return 1
        print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if not os.path.isdir(args.maildir):
        print(f"no Maildir at {args.maildir}", file=sys.stderr)
        return 2
    counts = process(args.maildir, args.out, args.dry_run)
    print(f"scanned {counts['scanned']} message(s): {counts['dsn']} delivery report(s), "
          f"{counts['complaint']} complaint(s), {counts['ignored']} ignored, "
          f"{counts['duplicate']} duplicate")
    if args.dry_run:
        print("dry run: nothing written")
    else:
        print(f"appended {counts['appended']} record(s) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
