# Growth queue — staged outbound

**Status:** 2026-11-19 · **Owner:** Growth stages; Savio sends. Nothing in this
file has been sent.
**Companion:** `docs/product/RECRUIT-FIRST-TEN.md` — the script these three
messages serve.

## The queue

| id | to | surface | relationship | status | staged | send condition | kill line |
|---|---|---|---|---|---|---|---|
| GQ-001 | `[Name]` | email | warm — has said the ceiling out loud | staged — not sent | 2026-11-19 | `/register` 200 and one registration completes; every placeholder filled | no reply in 3 days → one nudge, then close |
| GQ-002 | `[Name]` | X DM | cold — a public post names the ceiling | staged — not sent | 2026-11-19 | same | no reply in 3 days → one nudge, then close |
| GQ-003 | `[Name]` | email | forwardable intro via `[Mutual]` | staged — not sent | 2026-11-19 | same | no reply in 3 days → close; the mutual owns the nudge |

The send condition is measured, not assumed:

```console
$ curl -sS -o /dev/null -w '%{http_code}\n' https://test.odile.fyi/register
404
```

A2 builds the register page. No message is sendable before it answers 200 and
one registration has completed end to end.

**Placeholders.** Fifteen across the three messages, all named in
`docs/ops/evidence/2026-11-19-growth/placeholders.txt`. Growth fills none of
them; Savio fills the name, the quoted sentence and the times. No recipient is
named here and no address is stored at HQ — the queue is a template, not a list.

## GQ-001 — warm: someone who has said the ceiling out loud

**Surface:** email from Savio’s own address · **Status:** staged — not sent
**Why this shape:** their sentence is the opener; the ask is the meeting, not a
link.

```text
Subject: A machine for [the task]

[Name],

You said [what they said] — [the consequence, in their words].

I built the thing for it: your own computer, with an agent inside it. It keeps
working when you close the tab. One machine per person, in a private alpha.

Give me 25 minutes this week and bring one real task — [the task]. We do it
together on a screen share: you register, the machine builds, you check its
report, set its sign-in, and ask for the task. You leave with the file in Files.

Nothing to install. No key to make, no terminal. Nothing to pay — I pay for the
machine and the inference in the alpha.

Does [two times] work? If not, name a time.

— Savio
```

## GQ-002 — cold: a public post that names the ceiling

**Surface:** X DM from `@savio_sou` · **Status:** staged — not sent
**Why this shape:** the post is the qualification; no demo, no deck, one ask.

```text
Subject: none (X DM)

[Name] — your [post] about [the ceiling] is the exact thing I built for.

Odile gives you your own computer, on the internet, with an agent inside it. It
keeps working when you close the tab. One machine per person, private alpha, no
price.

No demo. Give me 25 minutes and bring one real task — the thing you would do by
hand this week. You register, the machine builds, you check the report, set the
sign-in, and ask for it. You leave with the file.

Does [time] work?

— Savio
```

## GQ-003 — forwardable intro: one hop outside the network

**Surface:** email from `[Mutual]`, forwarded · **Status:** staged — not sent
**Why this shape:** the mutual’s name is the trust; the ask stays the same, and
the recipient replies to a person they know.

```text
Subject: Intro — [Name] and Savio

[Name] — [Mutual] thought this might be for you.

Savio built Odile: your own computer, with an agent inside it. It keeps working
when you close the tab. He is inviting a few people to a private alpha; nothing
to pay.

The ask is 25 minutes and one real task you would do by hand this week. He sets
it up with you on a screen share, and you leave with the file.

Reply here and [Mutual] will connect you.

— [Mutual]
```
