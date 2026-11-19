# The first ten — a hand-recruit script

**Status:** 2026-11-19 · cycle 1, unit G2 · **Owner:** Growth
**Source:** Paul Graham, “Do Things That Don’t Scale” (July 2013) —
<https://paulgraham.com/ds.html>, read 2026-11-19. The section names below are
the source’s: Recruit, Fragile, Delight, Experience, Fire, Consult, Manual, Big.
**Product:** `docs/MVP-V0.1.md` §0, the alpha.0 record (18 November 2026):
private, invitation-based, no payments. The recommended alpha.0 envelope is five
testers (`docs/product/ROADMAP-ALPHA0.md` §5, D2); this script is written for ten
so alpha.1 can reuse it, and the queue stages the first three. The track row was
cut on 6 November for ten paying tenants at $49; every price sentence is gone.
**The one metric:** activation — registered, machine provisioned, first sign-in,
one agent turn that writes a file the user asked for. A signup is an input.

## 1. Who the ten are

People who already pay for an AI subscription and have hit its ceiling: the
thread forgets last week, the file cannot be opened from the next conversation,
the 7 a.m. job does not exist. They have tried to fix it — a second subscription,
a longer context window, a project folder — and the work still lives in a chat
window. They will never open a terminal.

Half the ten come from outside Savio’s network. Friends answer to be kind;
strangers answer to be right.

**The task they bring is the segment test.** One real thing they would do by
hand this week: the weekly brief, the page to watch, the meeting notes. If they
cannot name one, they are not in the ten.

**Poor fits, stop early:** anyone who wants a better chat window (they compare on
model quality and price, where Odile loses); anyone who wants root and a shell (a
VPS is the right answer); teams (one machine per person breaks the shape).

## 2. Where the ten come from

1. **The people who told him.** Start with anyone who has said the ceiling out
   loud. The opener is their sentence, not ours.
2. **Public posts that describe the ceiling.** A post about re-pasting the same
   brief every Monday is a better qualification than a job title. Quote it.
3. **One hop.** At the end of each install, ask for one intro to someone outside
   the network. Do not ask in the first message; ask when the file exists.

Record every name in the tracker (§6) the day it is asked. An unrecorded name
is a conversation, not a recruit.

## 3. The ask

One message, five parts, no deck and no demo video:

1. the thing they said, in their words;
2. one sentence of what this is, from `docs/CLAIMS.md` §1;
3. the one task to bring;
4. the ask — 25 minutes together this week, not “want a link?”;
5. nothing to pay, and nothing to install.

The ask is the meeting. “Great, I’ll send you a link” is how a yes becomes a
maybe; the Collison installation is “right then, give me your laptop”.

## 4. The install

On the call, share the screen and stay until the machine is running.

1. They register at `https://test.odile.fyi/register` (A2; on 2026-11-19 it
   answers 404 — measured, see the evidence). They use the address the message
   reached.
2. The build takes minutes. Use them for the past-tense interview: how do you do
   this today, what is hardest, how often, why does it matter, what have you
   tried? Watch them work if they will share. Do not mention the product again.
3. When the machine is ready: open the report first and check it says PASS. Then
   set the machine’s sign-in. Then ask for the task they brought.
4. Stay until Files shows the file. That is the recruit; the registration was
   only the paperwork.
5. If the build fails, stay and retry. Say what is happening. The call is the
   product.
6. If they ask for SSH or root, they are the VPS buyer. End kindly.

## 5. The first week

The first ten are fragile; about 30 days of attention is the difference between a
tester and a signup. One named human — Savio — asks one question:

- **Day 1:** “Did the file survive?”
- **Day 3:** “What did you ask it for today?”
- **Day 7:** “What did you stop doing by hand?”
- **Week 4:** “Are you still signing in?”

One line each, in the tracker. If there is no reply by day 3, send one nudge:
“Still worth 25 minutes? If not, say no and I’ll stop.” Then close the row; no
fourth message.

The thank-you is a sentence that quotes their file — “the brief you asked for is
in `~/briefs/…`” — not swag and not a survey.

## 6. The tracker

| # | Name | Source | Task | Asked | Installed | First file | Day 1 / 3 / 7 | Week 4 |
|---|---|---|---|---|---|---|---|---|

A yes without a date is a maybe. A signup without a file is not activation. The
tracker is the only count that goes in a gate packet; the queue is not a funnel.

## 7. Kill lines

- Fewer than 2 of the first 5 active in week 4 → the wedge is wrong, not the
  feature list (`docs/MVP-V0.1.md` §0).
- More than 2 of the first 5 cannot get in without a call → fix the flow before
  recruiting the next five.
- 3 of 5 ask for a visible key or terminal → move to the app fallback.
- A message needs a sentence that is not in `docs/CLAIMS.md` → do not send it;
  fix the deck first.
- A candidate asks for root → close the row; they are not the segment.

## 8. The queue

`docs/product/GROWTH-QUEUE.md` holds the three staged messages. Growth stages;
Savio sends; nothing leaves the file without his send. Every row carries a send
condition and a kill line. The common send condition today: `/register` answers
200 and one registration completes end to end. Until then the messages wait.
