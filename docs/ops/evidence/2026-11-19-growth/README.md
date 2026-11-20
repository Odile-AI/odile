# G2 — the hand-recruit script, and a queue that cannot send

**Seat:** Growth · **Unit:** G2, primary source — do things that don’t scale
**Date:** 2026-11-19 · **Status:** script and three messages staged; nothing sent.

## 1. Source

Paul Graham, “Do Things That Don’t Scale” (July 2013) —
<https://paulgraham.com/ds.html>, read 2026-11-19. The sections used:

- **Recruit** — “You can't wait for users to come to you. You have to go out and
  get them.” The Collison installation: “Right then, give me your laptop.”
- **Fragile** — “about 30 days of going out and engaging in person with users
  made the difference between success and failure.”
- **Delight** — the first users should feel that signing up with you was one of
  the best choices they ever made.
- **Big** — “It's not enough just to do something extraordinary initially. You
  have to make an extraordinary effort initially.”

## 2. Exercise

Wrote `docs/product/RECRUIT-FIRST-TEN.md` — who the ten are, where they come
from, the ask, the install, the first week, the tracker and the kill lines — and
staged three messages in `docs/product/GROWTH-QUEUE.md`: a warm email, a cold X
DM and a forwardable intro. Nothing was sent.

The track row was cut on 6 November for ten paying tenants at $49. The product
re-cut on 18 November to alpha.0 — private, invitation-based, no payments — so
the script keeps the ten for alpha.1, notes alpha.0’s recommended five, and
carries no price sentence.

## 3. What was measured

| Check | Command | Result |
|---|---|---|
| Register page | `curl -sS -D - -o /dev/null https://test.odile.fyi/register` | `HTTP/2 404`, `server: odile-bench`, 10 bytes — the send condition is unmet |
| Queue shape | `python3 check_queue.py` | 3 rows, all `staged — not sent`; 3 message bodies |
| Message length | `check_queue.py` | 129 / 95 / 88 words |
| Placeholders | `check_queue.py`, `grep -n` | 15 in the message bodies; every occurrence named in `placeholders.txt` |
| Claims | `claims-trace.txt` | 3 deck rows found verbatim in `docs/CLAIMS.md`; the install and no-price lines are existing `templates/emails/invite.txt` / `BRAND.md` §Email copy and `docs/MVP-V0.1.md` §0 |
| Claims lint | `./tools/claims_lint.py` | exit 0 — 18 files, 13 forbidden phrases |
| Full gate | `make check` | exit 0 — `Ran 2148 tests in 582.834s`, `make check: OK` |

The three messages are templates, not addressed mail: no recipient is named and
no address is stored at HQ. Fifteen placeholders are the honest form of “staged
before the names exist”; the queue says Savio fills them, not Growth.

## 4. What is not done

- No message sent, and no message sendable: `/register` answers 404 until A2
  lands.
- No recipient named; no email address kept.
- The first-week questions are a script, not a calendar; the tracker is a table
  in the script, not a tool.

## 5. The lesson the artifact carries

The recruit is an install, and the first week is the product. A link is a maybe;
a file in Files is a tester. The queue is the approval surface: Growth stages,
Savio sends, and every row carries a send condition and a kill line.
