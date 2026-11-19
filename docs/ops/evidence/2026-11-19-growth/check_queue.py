#!/usr/bin/env python3
"""Check the G2 growth queue: rows, statuses, placeholders, word counts."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
QUEUE = ROOT / "docs/product/GROWTH-QUEUE.md"
text = QUEUE.read_text(encoding="utf-8")

rows = [line for line in text.splitlines() if re.match(r"^\| GQ-\d{3} \|", line)]
print(f"queue rows: {len(rows)}")
for row in rows:
    cells = [c.strip() for c in row.strip("|").split("|")]
    qid, to, surface, relationship, status = cells[0], cells[1], cells[2], cells[3], cells[4]
    send_condition = cells[6]
    kill_line = cells[7]
    print(f"  {qid}: status={status!r} send_condition={'yes' if send_condition else 'NO'} kill_line={'yes' if kill_line else 'NO'}")

assert len(rows) == 3, "expected exactly three staged messages"
assert all("staged — not sent" in row for row in rows), "a row is not staged — not sent"
assert "sent —" not in text and "status: sent" not in text, "a row claims sent"

blocks = re.findall(r"```text\n(.*?)```", text, re.DOTALL)
print(f"message bodies: {len(blocks)}")
assert len(blocks) == 3, "expected three message bodies"

total_placeholders = 0
for i, block in enumerate(blocks, 1):
    words = len(block.split())
    placeholders = re.findall(r"\[[^\]]+\]", block)
    total_placeholders += len(placeholders)
    print(f"  GQ-{i:03d}: {words} words, {len(placeholders)} placeholders: {', '.join(sorted(set(placeholders)))}")
    assert "Subject:" in block, f"GQ-{i:03d} has no subject line"

print(f"total placeholders in message bodies: {total_placeholders}")
assert total_placeholders == 15, f"expected 15 placeholders, found {total_placeholders}"

print("OK: three rows, all staged — not sent; three bodies; 15 placeholders")
