# PR/FAQ — the alpha.0 tester's first hour

**Status:** draft 2 · 18 November 2026 · amended 19 November 2026 (ODL-916: S1 is alpha.1’s; alpha.0 keeps the guest command; ODL-581, merged at `5eba636`: the three account emails are built, and §2, §3a and §5 quote them) · amended 20 November 2026 (Savio’s A2 envelope answer, card `t_ad1783c4`: each tester gets 14 days and a ten-dollar inference key, with no cap on the number of testers — the press release, the invitation table, “What they pay today”, §4's wait copy, the moment table, the glossary and internal FAQ items 6, 11 and 14 carry it; `ODL-341` records the trial end on the account row and `ODL-583` shows it on the account page) · Product lead · card `ODL-575`; re-cut on cards `t_80bd9627` and `t_2d9f9210` · re-read against `main` `ea3907a` after the 18 November repo cut, re-cut against `5eba636` (ODL-581’s merge)
**Read with:** ADR-006, `docs/architecture/ADR-account-credential.md` — the two-credential model this page assumes (§3.1 A). The ADR was in review on `odl/ODL-569`, not on `main`, when this draft was written; the page is judged against it. `docs/MVP-V0.1.md` §0, §2, §3 (re-cut 18 November 2026, `ODL-576`); `docs/product/ROADMAP-ALPHA0.md` A1–A2, S1 and G1–G17; `docs/product/FIRST-RUN-SIX-ACTS.md` (the v5 six acts the bench serves today); `docs/CLAIMS.md` v3.10.
**What this is:** the Working Backwards artifact A2 is judged against. The backlog's A2 exit says “a person registers…”; this page names that person, the invitation, the wait, the first task and the return visit, from their side. It is not a public page; every public sentence in it is a `docs/CLAIMS.md` row, or is marked **pending** a named re-cut.
**The one metric:** activation — registered, machine provisioned, first sign-in, one agent turn that writes a file the user asked for (`docs/backlog.yaml` meta). Alpha.0's own question is narrower than week-4 retention: **does an invited tester finish the first hour without a call?**

---

## The press release

### Odile — your own computer, with an agent inside it

**For the invited testers of the private alpha:** people who already pay for an AI subscription, have hit its ceiling, and will never open a terminal. Someone with real work to keep — a client file, a weekly brief, a page to watch — and no wish to become a sysadmin.

**The problem.** You pay for the best chat model, and you still hit the ceiling. The thread forgets what you said last week. The file it wrote cannot be opened from the next conversation. The job that should run at 7 a.m. does not exist. Your workaround is a folder of prompts and the same brief pasted again every Monday.

You have tried to fix it: a second subscription, a longer context window, a project folder. The ceiling moves; the work still lives in a chat window.

You would put the real work on a machine you own — except that the only machines you can rent are sysadmin projects, and the only people who can read them are the people who run them.

**The solution.** Odile gives you your own computer, on the internet, with an agent inside it. [C-1] It is a real Linux machine, not a chat window. [C-2] One machine per person. [C-3] It keeps working when you close the tab. [C-4]

You register with an email and a password, confirm the address, and we build the machine. You check its report, set the machine's own sign-in, and ask for one real thing. The file is still there tomorrow, and the machine has already scheduled your first brief.

Nothing to install. No key to make, no terminal required. Odile pays for the machine and the inference during the alpha; there is no price to agree to. *(MVP §0; the envelope is Savio’s answer of 20 November 2026 — D2 in `docs/product/ROADMAP-ALPHA0.md` §5, card `t_ad1783c4`: every tester gets 14 days of the machine and the inference, and one inference key capped at ten dollars; there is no cap on the number of testers — as many invitation codes as Savio wants to issue. `ODL-245` holds the list.)*

Once you have set your sign-in, nobody at Odile can log in to your machine. [C-6; the pre-first-visit window is item 2] After a reboot on the verified image, it starts itself. [C-7; a changed image stays locked, C-13] And the proof is on the machine, not on a slide: the report, the measurement and the sign-in line are on `/verify`, one page. [C-10; the deck’s §4.4 rows]

**A quote we expect to hear (draft — not yet collected).** “I registered on my phone at lunch, checked the report, set my sign-in, and asked it to turn my meeting notes into a one-page brief. The file was there when I got back to my desk, and it was still there the next morning.”

### How the tester is invited

The tester is **a person Savio invites by name**, from people he already knows have hit their model's ceiling: not a campaign, not a waitlist blast. This page names the segment and the invitation; the individuals are his list, and no one had accepted when it was written. The first cohort is the test, not a market: if they cannot finish the first hour, the segment or the flow is wrong, and the next draft starts from the interviews.

| When | How | What the email carries | Why this shape |
|---|---|---|---|
| **Now (A2, the bench)** | A personal email from Savio. The bench registers openly by design (G12), so there is no code and no claim token. | One link to `https://test.odile.fyi/register`, the one task to bring, and what it costs (nothing). | The cohort is recruited by hand (`ODL-245`); its size is the signed answer of 20 November 2026 (D2 in `ROADMAP-ALPHA0` §5, card `t_ad1783c4`) — as many testers as Savio invites, with no cap, each on a 14-day trial with a ten-dollar inference key. A personal note from a person, not a campaign. |
| **From A7** | The invitation email from `Odile <invite@mail.odile.fyi>`, carrying a single-use invitation code. | The same link, plus an eight-character code the register form asks for. | Production's registration is gated by codes the team issues and revokes; proven on the bench first, then production after Savio's sign-off. |

The invitation is the first surface of the product. Its copy is below, as built: `templates/emails/invite.txt`, landed by ODL-581 (merged at `5eba636`) with the `BRAND.md` §Email re-cut in the same commit. The footer is `Private alpha · Odile pays for the machine and the inference · no price`; “Claiming takes 3 minutes” is gone, dropped as unmeasured; and the fallback is a reply to Savio's message, because alpha.0 has no support desk with a person on it (`ROADMAP-ALPHA0` §1).

### What the bench shows today, and what A2 adds

| Surface | On the A1 bench today | After A2 |
|---|---|---|
| `test.odile.fyi/` | The account site; a tester reaches `/claim?t=…`, the alpha.0 claim page: the email is filled in from the link, one consent box, one button, “Nothing to pay.” No password, no account. | `/register` collects email, password, repeat and consent; `/account` shows the machine’s state. |
| The ready email | `templates/emails/ready.txt`, built (ODL-581): the machine URL, the report URL, the measurement, the first-visit link, the reset page, the guide, and the alpha footer `Private alpha · Odile pays for the machine and the inference · no price`. | Queued when the machine is ready, with no operator (the account path is ODL-341’s); the copy is the built template below. |
| The machine’s first page | `/first-visit?t=…` is a password form; `/verify` is a separate page. | The report above the form, so “set your sign-in only after PASS” is a gate, not a guide. |

The bench is the first place these words are read by a person; A1’s copy is judged here, not on `odile.fyi`. The “today” column is what the bench serves: the three account templates are built (ODL-581), and the account path that queues the confirmation and provisions the machine is ODL-341’s. Until ODL-341 lands, the invitation is a personal email Savio sends, and no tester receives a confirmation or a ready email from the bench.

### What they pay today

| | Today | In alpha.0 |
|---|---|---|
| **The incumbent** | A chat subscription. Claude Pro is $20 a month billed monthly ($17 a month on the annual plan); Max is $100; Ultra is $200. ChatGPT’s comparable tiers are the same shape. *(claude.com/pricing, read 18 November 2026.)* | They keep it. Alpha.0 does not ask anyone to cancel; the bet is that they open Odile daily by week 4, not that they churn on day one. |
| **Odile** | — | **Nothing.** Odile pays for the machine and the inference during the tester’s **14-day trial**, counted from registration; the account page states the trial’s end date, and what happens at its end is A8/A9’s decision (item 14). A2 caps each tester’s prepaid inference key at **$10 lifetime** (`docs/backlog.yaml` A2, exit criterion 5), so the trial can end at the cap; at zero the provider refuses and the agent says so. |
| **The price** | — | **Alpha.0 has no price.** The price and the paid cohort are alpha.1’s (A8); the $49 row is the v5 record, not a bill. |

### The one task they bring

The invitation asks for one real task, named before they register: **the thing they would do by hand this week**. Not “try the agent”. For this segment the task is usually one of three:

1. **The weekly brief** — turn a page of notes into a one-page brief, in a file they can keep.
2. **The page to watch** — check a competitor’s pricing page every morning and write down only what moved.
3. **The meeting notes** — turn a transcript into decisions, owners and dates.

The first hour uses **whichever task the tester brought**; the copy’s example is the first — turn a page of notes into a one-page brief and leave it in Files. It is one message, it needs a real filesystem, it is verifiable (the file exists and survives a sign-out), and it is the user’s work — not a demo. The ritual’s own brief is the first file; the tester’s task is the first file they asked for.

### What they keep

The account (their email and the account password), the machine’s sign-in, the recovery code and the recovery file, and every file in the workspace. The machine’s sign-in is theirs alone: set on the machine at first visit and hashed there; Odile never sees it. [C-8] The account password opens the account page — a bookmark that holds the machine’s address and state — and, by the mechanism, never opens the machine; if the tester reuses it, the habit makes the two one secret (C-9, pending).

---

## The first hour, moment by moment

The clock below is the tester’s, not ours. “Minutes” are not promised anywhere in the copy; the account page is a state, not a spinner.

| # | Moment | Surface | What the tester sees | What must exist |
|---|---|---|---|---|
| 1 | The invitation | email | The one link, the one task to bring, “nothing to pay”. | `templates/emails/invite.txt` (built, ODL-581); `BRAND.md` §Email (re-cut, ODL-581) |
| 2 | Register | `test.odile.fyi/register` | Four fields: email, password, repeat, consent. One button. | `/register` (not built); `accounts` table (not built) |
| 3 | Confirm the address | email → `test.odile.fyi/confirm` | “Check your email. The machine is ordered after you confirm.” | `/confirm` (not built — ODL-341); the confirmation email `templates/emails/confirm.txt` (built, ODL-581) |
| 4 | The wait | `test.odile.fyi/account` | “Building. You can close this page. We email you when it is ready.” Under the state: “Your trial ends on 4 December 2026.” | `/account` and its states (not built — ODL-583), including the trial end read from the account row (ODL-341); the provision trigger (ODL-341) |
| 5 | The ready email | email | The machine’s address, the report’s address, the expected measurement, one action: check the report, then set the machine’s sign-in. | `templates/emails/ready.txt` (built, ODL-581: URL, report, measurement, first-visit link, reset page, guide, alpha footer) |
| 6 | First sign-in | `<slug>.test.odile.fyi/verify`, then `/first-visit?t=…` | The live report (PASS/FAIL, the measurement), then the form: the machine’s sign-in, ≥16 characters. Then the recovery code once, the recovery file, “Open your workspace.” | The first-visit page (built); the report above the form (**not built** — R6’s “only after PASS” is currently an instruction, not a gate) |
| 7 | The first task | the workspace | The agent’s first session: the ritual asks name, time zone and three things, writes `~/briefs/<date>.md`, finishes the schedule, then takes the task they brought and leaves the file in Files. | The `odile-first-run` skill (built); the first task copy (not built) |
| 8 | The return visit | `<slug>.test.odile.fyi` | Their sign-in, the session, the brief in Files, the file still there. | Nothing new; this is the activation proof |

**The one card conflict this page exposed — resolved in the card.** ADR-006 §7 says the address is verified before the machine exists; `ODL-341`, cut on 18 November, queued the provision on the registration POST. The register → confirm → build order above is the tested one: a mistyped address must not order a machine. `ODL-341`’s DoD now carries the gate — “The confirm POST spends the token once, sets `email_verified_at`, and only then queues exactly one provision” — and the rate limit on the public registration POST. The account page is `ODL-583`’s. What remains is dispatch: the gate is written, and neither card has started.

### The words on the tester's screen

The copy above uses eleven words a non-technical tester has not met. Each is defined once, on the surface where it first appears, and never with a second meaning.

| Word | What it means to the tester | First appears |
|---|---|---|
| **Report** | The machine's own page. It says PASS or FAIL: PASS means the machine booted the image whose fingerprint is in the ready email. | The ready email's report link |
| **Measurement** | That fingerprint: a 96-character value. The page shows it; comparing it with the one in the ready email is optional — the PASS/FAIL line is the check. You never type it. | Ready email |
| **Account page** | `test.odile.fyi/account` — the machine’s address, its state and your trial’s end date. It is a bookmark, not a key. | Register confirmation |
| **Trial** | The 14 days of the machine and the inference that Odile pays for, counted from the day you register. The account page shows the last day; nothing is charged on it. | Register confirmation |
| **Your machine's sign-in** | The password the tester chose at first visit; the name is always `odile`. It opens the workspace, and only the tester knows the password. | First visit |
| **Your account password** | The password chosen at registration. It opens the account page and nothing else. | Register |
| **Workspace / Files** | The Webapp: chat, Files (the file browser), terminal. The brief and the tester's file land in Files. | The first task |
| **Your machine's address** | `<word>.test.odile.fyi` — the URL the ready email gives you. The word is yours for the life of the machine. | The ready email |
| **Image** | The operating system the machine boots. Odile chooses it; its fingerprint is the measurement. You check the image did not change by checking the report. | Ready email |
| **Recovery file** | The small file the first visit downloads. It holds the backup password and the certificate fingerprint; keep it off the machine. It opens the nightly backup; it does not reset the sign-in. | First visit |
| **Key** | Five different things, named once each: the **boot key** (a public key with no private half), the **disk key** (generated and sealed on the machine), the **inference key** (on the machine, read by the agent), the **setup token** (the one-time link in the ready email), and the **recovery code** (resets the machine's sign-in). | Where each is named |

### 2. Invitation email (as built)

```text
From: Savio, personally, in alpha.0; Odile <invite@mail.odile.fyi> from A7
Subject: Your Odile machine

Hello,

You asked for a computer that keeps working when you close the tab. A machine is
reserved for you in the private alpha.

Register here: https://test.odile.fyi/register

Use the address this email reached. The machine has its own sign-in; you will set
it after you check the machine's report.

Bring one real thing you would do by hand this week — a page of notes to turn
into a brief, a page to watch, a meeting to write up. After five short questions,
that is the first thing you will ask it for.

Nothing to install. No key to make, no terminal required. Nothing to pay: Odile
pays for the machine and the inference during the alpha.

When your machine is ready, we email you its address. If anything goes wrong,
reply to this email — there is no support desk in the alpha.

— Odile

Private alpha · Odile pays for the machine and the inference · no price
```

The block’s body is `templates/emails/invite.txt` verbatim (ODL-581, merged at `5eba636`); the From and Subject lines are `BRAND.md` §Email’s table, and the register link is the bench’s account host, written out because the invitation must be sendable as it stands.

### 3. Register (draft copy)

> **Register for the private alpha**
>
> Your account page holds your machine’s address and its state. Your machine has its own sign-in, which you set after you check its report.
>
> Email · Password (at least 16 characters) · Repeat the password
> ☐ I agree to the terms and the privacy policy.
> **Create account**
>
> We email you a link to confirm the address. The machine is ordered after you confirm.

The account password may be the same as the machine’s sign-in, and the copy says so; most people will reuse one, and the page does not pretend otherwise (ADR-006 §4.2). The two are separate on purpose: the account resets from an email, the machine resets on the machine.

### 3a. Confirmation email (as built)

```text
From: Odile <invite@mail.odile.fyi>
Subject: Confirm your email address

Hello,

Open this link to confirm your address:

https://test.odile.fyi/confirm?t=<64 hex>

After you confirm, the machine is built and starts itself. If you did not
register, ignore this message.

— Odile

Private alpha · Odile pays for the machine and the inference · no price
```

The body is `templates/emails/confirm.txt` with `{{CONFIRM}}` filled as the bench fills it — `https://test.odile.fyi/confirm?t=<64 hex>`, the configured account host, not the request’s `Host` header (`DECISION-password-reset.md` §3.1). The link is single-use: the GET renders a button and the POST spends the token. The email states no expiry, because no clock is measured (`BRAND.md` §Email); the earlier draft’s “expires in 24 hours” was never a built or approved sentence, and any TTL ODL-341 builds is silent in the copy.

### 4. The wait (draft copy)

> **Building.**
>
> We have your address. The machine is being set up. Its first boot prepares the encrypted disk, which is the slow part.
>
> You can close this page. We email you when the machine is ready.
>
> **Your machine:** `<slug>.test.odile.fyi` *— appears when ready*
> **Send the ready email again** *— replaces the old link*
> **Your trial ends on 4 December 2026.** *— in every signed-in state, read from the account row*

The trial line is the one date the page carries. It is the account row’s `trial_ends_at` (`ODL-341`), written at 14 days when the account is created and rendered in the brand’s date form; it appears in all four states (`confirm`, `building`, `ready`, `failed`), not only when the machine is ready. The alpha's own end date is A8/A9's (item 14), and the page never states it as the trial's.

If the build fails: “The build failed. We are retrying. You do not need to do anything.” If it fails twice: “The build failed. We are looking at it; write to help@odile.fyi if you have not heard from us.” A failed provision leaves no billed machine and returns the account to retryable (A2, exit criterion 4).

### 5. Ready email (as built)

```text
Subject: Your machine is waiting.

URL https://<slug>.test.odile.fyi
Report https://<slug>.test.odile.fyi/verify
Measurement <96 hex>

Check the report first: it says PASS or FAIL. PASS means the machine booted the
image whose fingerprint is above. The page shows the measurement too, if you want
to compare.

Set your sign-in: https://<slug>.test.odile.fyi/first-visit?t=<64 hex>

Set it only after the report says PASS. That password opens your workspace.

Later, if you forget your sign-in:

https://<slug>.test.odile.fyi/first-visit/reset — your recovery code resets it.

The first-visit guide is at https://odile.fyi/ceremony

— Odile

Private alpha · Odile pays for the machine and the inference · no price
```

The body is `templates/emails/ready.txt` verbatim (ODL-581, with the guide line re-cut by ODL-912) with its four placeholders filled: `{{URL}}` is the machine’s address, `{{MEASUREMENT}}` the expected fingerprint, `{{FIRST_VISIT}}` the single-use sign-in link, and `{{GUIDE}}` the shipped `READY_GUIDE_URL`, `https://odile.fyi/ceremony`. The subject is the `READY_SUBJECT` constant, “Your machine is waiting.”, and the From is `BRAND.md` §Email’s table.

**Why the report comes before the form.** The email carries five addresses — the machine, the report, the first-visit form, the reset page and the guide — and the order is the point: today `/verify` is a separate page and `/first-visit?t=…` is a password form, so the report must be named before the form or the email teaches the tester to skip the check. When item 9 lands — the report above the form — the report and first-visit addresses become one and the email says "open your first page; it shows the report, then asks for your sign-in". Until then the report link is the one the tester opens first.

### 6. First sign-in (draft copy)

The first-visit page will carry the machine’s report above the form — the live PASS/FAIL line and the measurement — once item 9 lands, so R6’s “you set your sign-in only after PASS” is a gate the tester sees, not a sentence in a guide. [C-10] Until then the tester opens `/verify` first and the first-visit link second, and both the ready email and the guide say so. The copy says what PASS means: the machine booted the image whose fingerprint is in the ready email. Then:

> **Set your sign-in**
>
> This is the first visit. Choose the password that opens your workspace. You will type it each time; your account password only opens your account page.
>
> Sign-in name `odile` · Password (at least 16 characters) · Repeat it
> **Set the sign-in**

Then, once: “Your sign-in is set. The workspace is open. This page will not open again.” The recovery code, the recovery file download, and “Open your workspace.” The page tells the tester to download the file and write the code down **before** opening the workspace: the page will not open again, and there is no HQ reset. [C-11]

If the report says **FAIL**, the page says: do not set the sign-in; write to help@odile.fyi with the measurement; the machine is rebuilt before the first visit. A FAIL is the one case where the tester stops before choosing a password.

### 7. The first task (draft copy)

The workspace opens; the agent speaks first:

> I’m Odile, the agent on this machine. Your own computer, with me inside it. No one at Odile can log in unless you grant it. Five short questions, then I’ll get to work. What should I call you?

*(The greeting is the deck’s rows C-1 and C-36.)*

After the third answer it writes today’s brief to `~/briefs/<date>.md` — a real file, before the ritual ends: the date and the time zone, the weather for their city, one line for each of the three things they named, and one sentence on what it will watch tomorrow. After the fifth answer it schedules the morning brief. The three things are the tester’s own: a folder to tidy, a site to watch, a topic to read about. Then the tester pastes the page of notes into the message — the first task needs no upload — and asks for the task they brought:

> Turn this page of notes into a one-page brief and leave it in Files.

The agent writes the file, says where it is, and stops. For a page to watch or a meeting to write up, the tester asks in their own words in the same message; the notes-to-brief is the page’s example, not a required script. The tester opens Files — the file browser in the workspace — and sees both files. That is activation: one agent turn, one file the user asked for.

### 8. The return visit (draft copy)

The next day the tester signs in at the machine’s address — not the account page. The first session is in the list, the brief is in `~/briefs/`, and yesterday’s file is still there. The account page is for the machine’s state and the address; it is a bookmark, not a key. The first-run skill’s close line still says the brief “runs when you unlock” (item 10); the copy in this page says “when the machine comes back up”, and the embedded skill is re-cut with the first-visit page.

---

## What the tester holds, and what happens when

| Question | Answer | Claim |
|---|---|---|
| Who holds the machine’s sign-in? | The tester. It is set on the machine at first visit and hashed there; Odile never sees it. | C-8 |
| Who holds the account password? | The tester. HQ holds a scrypt hash of it; it opens the account page, never the machine. If the tester reuses the same string for the machine’s sign-in — most will, and the copy says so (ADR-006 §4.2) — HQ’s hash verifies that same string: the mechanism never sends it, but the habit makes the two one secret. | C-9 (pending) |
| Who holds the inference key? | The machine. One key per tester, written at install to the machine (0600, root) and read by the agent; HQ keeps only its label and its first few characters. | C-37; A2 exit criterion 6 |
| Who holds the disk key? | No person at Odile, on a machine built from the current image: the first boot generates a fresh volume key on the guest and seals it to the measured boot; the published image’s key no longer opens the disk. | C-12 (approved; held in the deck until a tenant on the round-9 image proves it) |
| Who can read the machine? | Odile: no login; the three powers are stop or delete the machine, change DNS, and choose the boot image [C-6, C-18]. The agent is on the machine, not a login: it runs as `odile` with sudo and can read every file [C-34], but Odile’s people cannot reach it without a grant. Servury: the console and ciphertext, no guest login (the no-login half is MVP §2’s table, not a deck row) [C-17; the ciphertext clause is held with C-12]. NEAR AI: the prompts inside the TEE, not the machine [C-20, C-21]. A support grant: everything, because a grant is a handover [C-22–C-26]. | C-6, C-17, C-18, C-20–C-26 (C-12 held) |
| After a reboot? | On the verified image it starts itself; the Webapp comes back; a repeating job that was due runs once. A changed image stays locked (C-13). | C-7; §1 Access, the repeating-job row (verdict A11) |
| When it breaks? | Write to help@odile.fyi; the agent runs `odile-diag` and, if it is not enough, `odile-support grant --hours 24` with the tester’s consent — the one-control form is alpha.1’s (S1), not alpha.0’s. If the machine dies, it is rebuilt and the backup is restored with the recovery file. | C-11, C-19, C-22, C-27 |
| What does Odile keep? | The email, the account and a hash of its password, the machine record, the boot key with no private half, and hashes of the setup and claim tokens. Not the disk key, not the machine’s sign-in. | C-28, C-29, C-9 (pending) |
| What does the tester keep? | The account, the machine’s sign-in, the recovery code, the recovery file, the files. | C-8, C-9 (pending), C-11, C-35 |
| What is not promised? | A price, a paid cohort, a support desk, a one-control support grant (S1, alpha.1), the alpha’s own end date, or a permanent home for the machine — each tester’s 14-day trial end is stated on the account page. Alpha.0 is private and time-boxed; A8 decides the price, A9 the public alpha. | — |

---

## External FAQ — the tester asks

**What does it cost?** Nothing in the alpha. Odile pays for the machine and the inference, up to $10 of inference per tester; at zero the provider refuses and the agent says so. There is no card and no price to agree to.

**Do I need to install anything?** No. A browser. It works on a phone and a laptop.

**Do I need a key or a terminal?** No. There is no key to make and no command to run. The boot key in the command line is a public key with no private half; it is plumbing, not a credential. [C-30]

**Which password do I use?** Your account password opens your account page. Your machine’s sign-in opens your workspace. They are separate; you may use the same one for both.

**What happens after I register?** You confirm your email, we build the machine, and we email you its address. Open its report, check that it says PASS, set the machine’s sign-in, and start.

**What if the report says FAIL?** Do not set the sign-in. Write to help@odile.fyi with the measurement; the machine is rebuilt before the first visit.

**What if I mistype my email?** The confirmation never arrives and no machine is ordered. Write to help@odile.fyi and the address is fixed.

**How long does it take?** You do not have to wait on the page. The ready email is the signal. The machine’s first boot re-encrypts the disk, which is the slow part.

**I lost the ready email.** Before your first visit, open `https://test.odile.fyi/account` and ask for it again: the old link stops working and a new one is minted [C-29; ADR-006 §7]. After the first visit the link is spent — sign in at the machine’s address.

**I forgot my account password.** Write to help@odile.fyi. Odile resets the account, never the machine [C-9, pending]. After A5, the reset is a link to your email.

**I forgot my machine’s sign-in.** Use the recovery code from your first visit on the machine’s own reset page. If you lost the code, the way back is the recovery file and a rebuild; there is no HQ reset. [C-19]

**Can Odile log in?** No — once you have set your sign-in. Before that, the one-time link in your ready email can set it; it is a bearer, so whoever has it before your first visit can set the sign-in — keep the email to yourself. That is the only window in time. [C-6, C-22] Support access exists only when you grant it, for as long as you grant it.

**Can I get my files out?** Yes: the Files page, and the backup. The user-held backup copy is A3; until it lands, the recovery file opens the nightly backup and the team restores a rebuilt machine with you.

**Can I use it on my phone?** Yes. The register, account and first-visit pages are one column at 390; the workspace is the Webapp.

**Can I invite someone?** Not in alpha.0. Invitation codes are A7; the public alpha is A9.

---

## Internal FAQ — the dragons

**1. Two credentials, and the support load that comes with them.** The tester types the machine’s sign-in daily and the account password rarely. The failure is predictable: “which password?” The mitigation is copy where each password is chosen — the invitation (“The machine has its own sign-in”), the register page (“Your account page holds your machine’s address and its state. Your machine has its own sign-in, which you set after you check its report.”), the first-visit page (“your account password only opens your account page”), and the account page (“This password opens this page, never your machine.”). The ready email names only the machine’s sign-in — “Set your sign-in” — the pair’s reduction at the moment the second password is typed, because C-9 is pending until ODL-577 and the email quotes published rows only. The cost is real and is priced in ADR-006 §3.1: one more password, one more reset, one more form. ODL-572 decides the reset flow; its input is now fixed.

**2. The pre-first-visit window.** Before the owner sets the machine’s sign-in, the setup token in the ready email is a path to the machine’s gate. HQ mints it, holds the mail queue and the relay, and the boot log carried a copy at 0644. The promise is true **from the first visit onwards**; the token window is stated, not hidden (ADR-006 §2, §6). The 0644 copy is one of A5’s four defects to fix; the bench password that used to open the image is gone (ODL-521/522/523). The deck still lists A1’s “only sign-in is yours” and A7’s “only gate” as held on `t_adcdec31` — a card that is superseded, with its work landed. And C-29’s “the token you were sent is not stored” is true of HQ’s database but not of the mail queue or the 0644 boot log; the same re-cut names the queue, or the 0600 fix lands first. **A deck pass must re-verdict and unhold those rows before A2 ships**, or the tester’s first page cites a held sentence.

**3. The account at HQ falsifies four approved sentences.** “There is no password and no account at HQ” (C-31); “The sign-in is yours; HQ holds no password” (C-32); “At HQ we hold your email, the record of your machine, and a boot key with no private half. Not the disk key, not your sign-in” (C-28 — gains the account and its hash); and “No cookies, no trackers, no third-party requests” (C-33 — the account page sets one first-party session cookie). The privacy inventory gains the `accounts` rows, and `tests/test_legal.py` fails until `web/privacy.html` carries every column. The schema comment at `app.py` (“No passwords… are stored”) becomes “no **machine** password is stored; the account’s scrypt verifier is.” This is one deck pass, owned by the Product Lead with verifier review; it is not done here.

**4. The account emails are built; the deck row and the send path are not.** ODL-581 (merged at `5eba636`) built `templates/emails/invite.txt` and `confirm.txt` and re-cut `ready.txt`; `BRAND.md` §Email now carries the three templates, the alpha footer `Private alpha · Odile pays for the machine and the inference · no price`, and drops “Claiming takes 3 minutes” (unmeasured). Two gaps remain, and neither is copy. The deck: C-9's account row is ODL-577's; the money copy is re-cut — the ready email by ODL-581 (`238c22b`, merged at `5eba636`), and `web/claim.html`, `templates/emails/support.md` and `docs/FAQ.md` moment 4 by `t_02310cbf` (`ddcde16`) — so `docs/CLAIMS.md` §1a's money notes name all three homes, and the installed help (`t_080f8a83`) is the one copy home left on the v5 price. The path: ODL-341 queues the confirmation and provisions the machine, and it is `todo`; until it lands no account exists to receive either email. The emails can be sent; the deck pass and the account path must land before A2 is judged.

**5. A typo’d email would be a billed machine — if the machine were ordered before the address was confirmed.** The account must be verified before the provision worker runs (ADR-006 §7). That makes the confirmation email the cheapest safety in the flow, and it adds a step: register → confirm → build. The copy says so. The reminder path for an unconfirmed address is not built; the account page’s “send it again” covers the confirmed-address case, not the typo. The confirmation link must survive a mail scanner’s prefetch: the GET renders a **Confirm** button and the POST spends the token, or a scanner that fetches every link spends the tester’s confirmation before they see it. `ODL-341`’s DoD carries the gate — the confirm POST sets `email_verified_at` before it queues the provision — so the copy is not ahead of the card; what is missing is the build, because the card is `todo`, not dispatched.

**6. The bench registers openly before A7.** Anyone with the URL can create an account, and each account orders or recycles a machine. G12 permits this on the bench, and the envelope is per tester, not per cohort (14 days and a ten-dollar key each; no cap on testers, Savio 20 November 2026, card `t_ad1783c4`), so between A2 and A7 the bench’s bill is bounded by the registration rate limit and the team’s daily watch of the account list, not by construction; A7 closes it. A rate limit on the registration POST is part of the A2 card, not a later hardening — `ODL-341`’s DoD carries it on both the registration and the confirm POST. No card caps the number of accounts the bench will serve.

**7. The wait has no measured end-to-end time.** The provider answered `deploying` in 0.232 s and `active` 120.7 s later (`docs/product/ROADMAP-ALPHA0.md` §3); the first boot then re-encrypts the root — “minutes, not seconds, on a 2-vCPU guest” (`docs/runbooks/image.md` §5.1) — and the install follows. No card has measured register → ready. The copy avoids a number; the first A2 tenant measures it, and the account page shows a state, not a percentage.

**8. A recycled machine must be blank.** A2 prefers a freed machine over a new order, and the promise only holds if the reinstall leaves nothing: a fresh disk, a fresh boot key, a fresh inference key (A2, exit criterion 3; `t_6d956aee`). If the wipe or the re-key is wrong, the next tester inherits the previous one’s files — the worst failure in the product. The recycle card’s evidence is a filesystem check, not a green boot.

**9. The first-visit page does not yet show the report.** R6 says “you set your sign-in only after PASS”; the page today is a form reached before `/verify`, and the check is an instruction in the guide. The first-visit page is embedded in the signed installer, so adding the live report above the form is a re-embed, a re-sign and a release cut — a guest change, not an HQ one. Until it lands, the sentence is a hope, and the PR/FAQ marks it **not built**.

**10. The first-run skill still says “locked” and “unlock”.** Its close line promises the brief “runs when you unlock”, and there is no unlock under the sealed boot. The sentence is inside the embedded help/first-run bundle, so it needs the same re-embed/re-sign as item 9. The PR/FAQ’s copy uses “when the machine comes back up”.

**11. The account page is a new surface with no design.** It has four states (`confirm`, `building`, `ready`, `failed`), two actions (“open your machine”, “send the ready email again”), one line about which password is which, and one line under the state: **“Your trial ends on 4 December 2026.”** The date is the account row’s `trial_ends_at`, written once when the account is created (registration; at A7 the same instant the code is spent) at 14 days, and rendered in the brand’s date form; it is a date, not a countdown. It must be no-JS, one column, 390 and 1440, and it must not become a dashboard. The page is `ODL-583` (cut 18 November, depends on `ODL-341`); its DoD quotes the four states, this copy and the trial line, so the states are fixed here and built there; the row and the date are `ODL-341`’s.

**12. What the tester keeps is thin until A3.** The recovery file opens the nightly backup, and the restore drill exists (38m03s), but the user-held copy, its third-provider destination and the whole-machine restore are A3’s (ADR-004, `ODL-570`). Until A3, “backed up nightly” is true of the machine and the repo, not of a copy the tester holds. The first-hour page must not promise the copy; the external FAQ says which half exists.

**13. Help@ has no reader.** The first hour’s fallback for a lost account password, a failed build or a stuck first visit is `help@odile.fyi`, and the human card to give it a reader (`t_a3f83ee6`) was 3.2 days old at the 17 November gate packet. An alpha whose only support path is an unread inbox is not an alpha. This is Savio’s decision, not a build card.

**14. The alpha has no end date, though every tester’s trial does.** Each tester gets 14 days and a ten-dollar inference key, with no cap on how many testers Savio invites (Savio, 20 November 2026, card `t_ad1783c4`; D2 in `ROADMAP-ALPHA0` §5). The 14 days are the tester’s trial, not the alpha’s run: the alpha’s end date is A8/A9’s decision, and the two are not the same number. The trial starts when the account is created — registration, which at A7 is the moment the code is spent — so the build and the wait sit inside the 14 days; `ODL-341` writes the end date on the account row and `ODL-583` shows it in every signed-in state. Alpha.0 is private and time-boxed; A8 decides the price and A9 the public alpha. The tester is told before the machine is built that the machine is theirs for the trial, not forever, and what happens at the end is A8/A9’s decision. Not deciding is a dragon; the invitation says “private alpha” and the page carries the date.

**15. The tester may ask for SSH or root.** There is no user SSH key and no user key to generate (R1–R2); the Webapp’s terminal runs as the agent’s user with sudo. The copy answers with C-34 verbatim — “The agent runs as `odile` with sudo; it can read every file on the machine” — and “there is no key to lose” (C-30: the boot key has no private half). “You are root inside the workspace” is not a deck sentence and stays out of the public copy until a row exists.

**16. A2 has three cards, and none is dispatched.** The 17 November gate packet named the gap: A2 opened with this PR/FAQ and three exploration cards, and no registration → machine → key build card. On 18 November the Product Lead cut `ODL-341` (registration on the bench becomes a tenant with a machine, no operator). Its DoD now carries the two gates this page’s items 5 and 6 name — the email-verification gate before provision (ADR-006 §7) and the rate limit on the public registration POST — and the register page’s pair sentence. The account page is `ODL-583`; the deck row is `ODL-577`. All three are `todo` at this writing (19 November), and `ODL-341` waits on `ODL-242`. A2’s demo sentence has a mechanism and a written flow; it has no built path yet.

**17. The support grant is a command today; the one-control grant is alpha.1’s (S1).** The guest half is built and proven: `odile-support grant --hours 24` installs Odile’s support key behind a forced command, logs `event=start` and `event=end`, refuses a new login after the expiry and removes the line on revoke (ODL-217’s drill; the installer places the program, ODL-474). What alpha.0 does not have is a control a non-technical tester can reach when the workspace — the surface that would carry the request — is the thing that is broken: today the tester asks the agent, or types the command in the workspace’s terminal. The route out is the account page, which is why S1 sits in alpha.1’s window (11–24 January) in `docs/product/ROADMAP-ALPHA0.md` §2, depends on A2, and keeps the four constraints `AGENTS.md` sets — user-initiated, time-boxed, a forced command, a log — with the default off. Alpha.0 promises none of it: the invitation and the first hour do not name a support control, and the custody row above says which form the tester gets. The account-page control caps a grant at 72 hours; the guest command's own 720-hour ceiling is unchanged. Revoke and expiry stop new logins; the same control ends an open session in one action, and a session nobody ends runs until it exits (C-23). The HQ `grants` row the privacy inventory discloses is S1’s to write; the guest command records only the machine’s log.

---

## What the other two credential models would feel like

ADR-006 chose **A — two credentials, one link**. The tester’s experience under B and C is the price the choice avoids; the page records it so the choice is judged against a person, not a diagram.

| | A — two credentials (chosen) | B — one credential, set at registration | C — an auth proxy |
|---|---|---|---|
| **What the tester types** | The machine’s sign-in daily; the account password twice a year. | One password for everything; the account is the authority. | One login at HQ, then every machine opens. |
| **What they see at first visit** | The report, then “set the machine’s sign-in”. | No first visit; the password they chose at registration already opens the machine. | A redirect through HQ; the guest’s gate is hidden. |
| **What Odile can do** | Set the account, never the machine’s sign-in. | Set any tenant’s sign-in at any time (B1 sees the password once; B2 holds a verifier). | Mint a session for any tenant; sit in every request. |
| **What dies** | Nothing new: the pre-first-visit token window is unchanged (item 2); the account forces one sentence out (C-31) and three rows to name it (C-28, C-32, C-33). | “We never see it”, “there is no HQ reset”, “the only sign-in is yours” — eight approved sentences, two embedded surfaces and `/verify`’s proof. | “Your prompts do not pass through Odile’s servers”, and HQ availability becomes every tester’s sign-in. |
| **Who pays** | The user (one more password), Product Lead (copy), support (the lost-code path). | The product (the promise), Builder (seed, installer, push), security (an offline-crackable copy at HQ). | The product (the access model), Builder (a new service), Savio (an access change). |
| **Reversible?** | Two-way: A→B is a seed field and a push path, per tenant. | One-way on the promise: B→A cannot restore it for machines already seeded. | One-way; it is a different product. |

---

## The claim table

Every privacy, security or reliability sentence in this page maps to a `docs/CLAIMS.md` row (v3.10). The account emails’ sentences are mapped template by template in `BRAND.md` §Email and are not repeated here. The deck does not number its rows: **C-nn is this page's index**, and the `Deck row` column is what to check — the deck section and, for an access row, the verdict row in `docs/security/CLAIMS-VERDICT-strict.md` (A1–A11). **Published** means the row is approved and its proof has run; **held** means the row is approved but §1a keeps it off the public pages until the named card's evidence exists; **pending** means A2 changes the deck and the re-cut has not landed.

| # | Sentence as this page uses it | Deck row | Status |
|---|---|---|---|
| C-1 | “Odile gives you your own computer, on the internet, with an agent inside it.” | §1 The machine | Published |
| C-2 | “A real Linux machine, not a chat window.” | §1 The machine | Published |
| C-3 | “One machine per person. Nothing is shared.” | §1 The machine | Published |
| C-4 | “It keeps working when you close the tab.” | §1 The machine | Published |
| C-5 | “The $12 inference allowance is included and hard-capped.” | §1 Money and place | **Not used in alpha.0** — the $12 is the v5 paid-cohort row; alpha.0 has no price, and A2 caps each key at $10 lifetime |
| C-6 | “No one at Odile can log in to a tenant machine.” | §1 Access, A1 | Published (the operator half PASS); true from the first visit onwards — before it, the setup token is a path HQ mints (item 2) |
| C-7 | “After a reboot, it starts itself. The seal is the key.” | §1 Access, A5 | Published (PASS) |
| C-8 | “Your Odile sign-in is set on your machine at first visit and hashed there. We never see it.” | §1 Access, A7 | Published (set-at-first-visit PASS) |
| C-9 | “Your Odile account holds your email and a password that opens your account page — never your machine. The sign-in on the machine is set there and hashed there; Odile never sees it.” | §1 First run (replaces “There is no password and no account at HQ”), ADR-006 §4.1 | **Pending** — A2 forces the re-cut. True of the mechanism; ADR-006 §4.2 says most testers will reuse the password, and the page says so |
| C-10 | “Check the machine before you trust it: the verify page shows PASS or FAIL, and you set your sign-in only after PASS.” | §1 Confidential computing, §4.4 | Published; **the first-visit page does not yet show the report** (item 9) |
| C-11 | “A recovery code and a recovery file are the way back: the code resets your sign-in, the file opens your backup. Your first visit gives you both; keep them off the machine.” | §1 Access, A9 | Published |
| C-12 | “There is no passphrase to type. The disk key is sealed to the measured boot, and no one at Odile holds it.” | §1 Access, A4 | **Held** in §1a on `t_28dda3b7` (done); the served round-9 image is `t_51ebf972` (done) and re-keys at first boot. Unhold after a tenant proves it |
| C-13 | “The disk unlocks only on the boot you verify. Change the image, and it stays locked.” | §1 Access, A3 | **Held** on the same cards; same unhold |
| C-14 | “Memory is encrypted by the CPU.” | §1 Confidential computing | Published (the ciphertext half is C-12’s hold) |
| C-15 | “The boot is measured: firmware, kernel, initramfs and command line are hashed into a value the CPU signs.” | §1 Confidential computing | Published |
| C-16 | “The app layer is not hardware-measured yet.” | §3 gap 1 | Published (a stated gap) |
| C-17 | “The console passes through the host.” | §3 gap 2 | Published (a stated gap) |
| C-18 | “Odile keeps three powers…” | §3 gap 3 | Published (a stated gap) |
| C-19 | “A lost Odile sign-in is reset on the machine with your recovery code. There is no HQ reset.” | §1 First run | Published |
| C-20 | “Your prompts go from your machine to the enclave. They do not pass through Odile’s servers.” | §1 Inference | Published |
| C-21 | “Inference runs on NEAR AI’s confidential cloud, inside a hardware enclave.” | §1 Inference | Published |
| C-22 | “Support access exists only when you grant it, for as long as you grant it.” | §1 Access, A10 | Published (ODL-217/474) |
| C-23 | “A support grant is a time-boxed door into the running machine. It ends for new logins when it expires or you revoke it.” | §1 Access, A10 | Published |
| C-24 | “A grant is a handover, not a peek.” | §1 Access | Published |
| C-25 | “A grant can read the mounted disk, every file, the agent’s memory and past prompts, the sign-in hash, and the backup password if the recovery file is still on the machine.” | §1 First run, §5 | Published |
| C-26 | “It can add a LUKS keyslot or copy the volume key… The grant log is written on the machine; a root shell can edit it.” | §1 First run | Published |
| C-27 | “Backed up nightly. Rebuildable in under an hour — without a key.” | §1 Reliability | Published (v3.10); **not promised on the A2 page** until A3 names the copy the tester holds (the nightly backup and the 38m03s restore drill exist; the user-held copy is A3). “Without a key” means no boot key; the recovery file is the way back to the backup |
| C-28 | “At HQ we hold your email, the record of your machine, and a boot key with no private half. Not the disk key, not your sign-in.” | §1 Data handling, A8 | Published; **pending**: gains “and your account and a hash of its password” (C-9) |
| C-29 | “The claim and setup tokens are kept as hashes; the token you were sent is not stored.” | §1 Data handling | Published; **qualified** — HQ’s store keeps hashes, but the ready email’s setup token sits in the mail queue and, until A5’s 0600 fix, in the boot log at 0644 (item 2). The row must name the queue, or the fix lands first |
| C-30 | “The boot key in the command line is a public key with no private half. It is plumbing, not a credential.” | §1 Access, A2 | Published |
| C-31 | “There is no password and no account at HQ.” | §1 First run | **Falsified by A2** — replaced by C-9 |
| C-32 | “The sign-in is yours; HQ holds no password.” | §4.11 | **Pending** — the account hash must be named |
| C-33 | “No cookies, no trackers, no third-party requests.” | §1 Data handling | **Pending** — the account pages set one first-party session cookie: “The public pages set no cookies, no trackers, no third-party requests. The account pages set one first-party session cookie.” |
| C-34 | “The agent runs as `odile` with sudo; it can read every file on the machine.” | §1 First run | Published |
| C-35 | “The only sign-in is yours.” | §1 Access, A1 (second sentence) | **Held** in §1a on `t_adcdec31`, which is superseded: its work landed as ODL-521/522/523 (the image’s bench password is gone) and the setup-token window remains. Unhold after a fresh-boot check |
| C-36 | “No one at Odile can log in unless you grant it: no standing key, no root password, no disk passphrase.” | §1 Access | Published |
| C-37 | “Your machine’s NEAR AI key is one per user: the only inference credential on the machine, written at install and read by the agent.” | §1 Data handling (inventory) | Published |

**The deck state is stale in two halves, and A2 inherits both.** The disk block in §1a — A3, A4, A8’s “not the disk key” clause, §3’s disk row, §4.3, §4.8 and gap 3 — is held on `t_28dda3b7`, which is done, and the served image’s re-key landed on `t_51ebf972`, also done. The “only sign-in is yours” block — A1’s second sentence, A7’s “only app-layer gate” clause, §4.2, §2’s replacement and §5 — is held on `t_adcdec31`, which is superseded, and its work landed as ODL-521/522/523; the setup-token window is the reason that still stands. The unhold is a deck pass with fresh-boot evidence on a tenant, not a wording change. And C-9’s re-cut is the only way the account can ship: the moment `/register` exists, C-31 is false. Both passes are the Product Lead’s, with verifier review, before A2 is judged.

---

## What this page changes

| Change | Where | Owner | State |
|---|---|---|---|
| A2’s exit criterion 2: “The registered email and password reach that instance…” → “The registered email opens the account page, which reaches that instance; the sign-in is set on arrival; a chat turn and a file write succeed.” | `docs/backlog.yaml` A2 | Product Lead | **Done** — the record carries this sentence (read 19 November) |
| The deck re-cut: C-9, C-31, C-32, C-33, C-28, the `accounts` inventory rows, `tests/test_legal.py`, the `app.py` schema comment | `docs/CLAIMS.md`, `web/privacy.html`, `app.py` | Product Lead + verifier | **Carded** — `ODL-577` (deck, inventory, cookie clause); the schema comment is `ODL-578`’s |
| `docs/MVP-V0.1.md` §2 “the only user credential is the Odile sign-in”, §4 R3 and §12 D26: the account is a second credential, named as ADR-006 §8 requires | `docs/MVP-V0.1.md` | Product Lead (`ODL-576`) | **In progress** — the re-cut must add the account row, not only the naming pass |
| `AGENTS.md` §What this is and §Rules (“never store a Webapp password at HQ”): alpha.0 has no payments, and HQ keeps the account’s scrypt verifier — no **machine** password | `AGENTS.md` (HQ-only, untracked) | Product Lead | **Half done** — the payments sentence was re-cut on 19 November (ODL-919); §Rules still reads “no Webapp password at HQ” |
| The unhold: A1/A7’s held rows and A3/A4’s disk rows, on fresh-boot evidence | `docs/CLAIMS.md` §1a, `docs/security/CLAIMS-VERDICT-strict.md` | Product Lead + security | **Not yet carded** |
| `BRAND.md` §Email re-cut; `invite.txt` and `confirm.txt` built; `ready.txt` re-cut (footer, sign-in copy, reset page, guide) | `BRAND.md`, `templates/emails/`, `tools/brand_lint.py` | Product Lead + designer | **Done** — ODL-581, merged at `5eba636`; the brand gate passed and `brand_lint` scans the three templates |
| `ODL-341`'s DoD: the email-verification gate before provision and the registration rate limit; the account page is `ODL-583`'s | `docs/backlog.yaml` A2, `ODL-341`, `ODL-583` | Product Lead re-cuts; builder builds | **Done in the cards** — both DoDs carry it; neither is dispatched |
| The first-visit page shows the live report above the form; the first-run skill’s “unlock” line is re-cut | `web/first-visit.html`, `installer/odile_install.py`, the embedded bundle, the release cut | Builder | **Not yet carded** |
| ODL-572’s reset memo now has its input: two credentials, two authorities, two flows | `docs/product/DECISION-password-reset.md` | Product Lead | **Done** — the memo landed 18 November; its §3.5 is the emails’ and the account page’s copy rule |
| The one metric and the kill criterion are pinned | this page | Product Lead | Done |

**The kill criterion.** If a cold reader — one who did not write this page and has not read the ADR — cannot answer “what does the tester see after registering?” and “who holds the key?” in one pass, A2 is a mechanism without a product: stop the build and rewrite the flow, not the card.

---

## Evidence

**Cold-read record, 18 November 2026.** The check the kill criterion names, run on draft 2. Each reader was a subagent given one file — this page — with the two questions and no other context, no ADR and no repository. No reader edited the page.

**Rounds 1 and 2 — draft 1.** Two independent readers answered both questions in one pass. The first named four copy defects: “report” and “measurement” were never explained in plain language; “Files” and the workspace were never described; the ritual’s “three things” were not named; and “key” named five different objects (boot key, disk key, setup token, inference key, recovery code). The second found two more: “key” was still undefined, and the inference key’s custodian was unstated. The page now carries the glossary “The words on the tester’s screen”, the wait copy says “sets up the encrypted disk”, the ready email says what PASS means, the first task names Files, and the “who holds” table names the inference key. The internal names (“Savio”, “bench”, A2/A7) and the “not built” labels are the document’s audience and status, not the tester’s copy.

**Round 3 — draft 2, two lenses.** A product reader and a security reader each answered both questions in one pass. The security reader’s custody table matched the page and named the windows exactly: the pre-first-visit setup token, a re-minted ready-email token, a support grant, password reuse, and the operator’s boot-image power. It also found six copy defects, all fixed before the final read: C-6 was stated unconditionally (now “once you have set your sign-in”); the 96-character comparison was presented as automatic (now optional; the PASS line is the check); there was no FAIL branch (now “do not set the sign-in; write to help@”); the password-reuse caveat was missing (now in “what they keep”, the custody table and C-9); C-29’s “token you were sent is not stored” was unqualified against the mail queue and the 0644 boot log (now qualified); and “the three powers” counted four verbs (now named as three). The product reader’s only hesitation was the second password — the cost ADR-006 §3.1 already prices.

**Round 4 — the file it read, `sha256 e6391e3c…`, one reader, one pass.** Its two answers, verbatim:

> **What does the tester see after registering?** “Four fields — email, password (at least 16 characters), repeat, consent — then one button. The page says to check my email: ‘The machine is ordered after you confirm.’ The confirmation link works once (24 hours). After it, the account page says ‘Building. You can close this page. We email you when it is ready.’ It says the first boot prepares the encrypted disk, which is the slow part. The ready email gives the machine address, the report page, a 96-character measurement, the first-visit link and a guide. I open the report first and check PASS. Then the first-visit page: sign-in name `odile`, password (at least 16 characters; the one I will type each time), repeat. The page says it will not open again; it gives a recovery code, a recovery file to download, and ‘Open your workspace.’ The agent greets me, asks five short questions — name, time zone, three things — writes today’s brief to ~/briefs/<date>.md after the third, schedules the morning brief after the fifth, then takes my task: I paste my page of notes, ask for a one-page brief in Files, it writes the file and says where. I use chat and Files; I never open the terminal. Next day I sign in at the machine’s address; the file is still there.”

> **Who holds the key?** “I hold the account (email and account password), the account page (a bookmark with the machine’s address and state, not a key), the machine’s sign-in, the recovery code, the recovery file, the setup token (in the ready email until first visit) and every file in the workspace. Odile holds my email, the account and a scrypt hash of its password, the machine record, the boot key (public, no private half), hashes of the setup and claim tokens, and only the label and first characters of the inference key — not the disk key, not the machine’s sign-in. The disk key is generated fresh on the machine at first boot and sealed to the measured boot — on a machine built from the current image; no person at Odile holds it. The inference key is on the machine (0600, root), read by the agent. If I reuse the same string for both passwords, Odile’s hash of the account password verifies the same secret. Who can read the machine: Odile has no login — only stop/delete, change DNS, choose the boot image; the agent runs as `odile` with sudo and can read every file; Servury has the console and ciphertext, no guest login; NEAR AI sees prompts inside the TEE, not the machine; a support grant — only when I give it — is everything (‘a grant is a handover’). Before my first visit, the setup token is a bearer HQ mints; a copy sat in the mail queue and the boot log at 0644.”

It answered **yes** to finishing the first hour without a call; the one hesitation is the second password, which is ADR-006’s priced cost. The quote is the 18 November file — its confirmation “works once (24 hours)” is draft 2’s line, which the built template does not carry (the re-cut record above). Its twelve residual questions are answered at the place named; every wording contradiction among them was fixed after its read, and the rest are the dragons the internal FAQ carries:

| The reader asked | Where the page answers it |
|---|---|
| “A typo’d email is a billed machine” vs “no machine is ordered” | item 5 (the header now states the counterfactual: if the machine were ordered before confirmation) |
| Account password “never opens the machine” vs reuse | “What they keep”, the custody table, C-9 |
| The ready email still carries the v5 price footer (as draft 2 was read) | **Fixed by ODL-581** (`5eba636`): the footer is the alpha footer; “What the bench shows today” and item 4 record the built state |
| “Set the sign-in only after PASS” while the report is not on the form | moments 5–6, item 9 (the email orders the check; the page enforces it when the report-above-form lands) |
| What the recovery file is | the glossary row **Recovery file** |
| What an “image” is | the glossary row **Image** |
| The skill still says “when you unlock” | §8 and item 10 |
| The first task for a page to watch or meeting notes | §7 (the notes-to-brief is the example; the other two are asked in the tester’s words) |
| How long the wait is | the external FAQ and item 7 (no measured end-to-end time; the account page is a state) |
| help@ has no reader | the external FAQ and item 13 |
| Who can use the bearer link | the external FAQ (“keep the email to yourself”) and item 2 |
| End date vs the trial’s 14 days | item 14 (the trial is 14 days and its end date is on the account page; the alpha’s own end date is A8/A9’s) |

**The cold reader’s verdict is the kill criterion’s answer:** both questions in one pass, yes to the first hour, with one priced hesitation. The flow is ready to be tested by the first cohort; it is not evidence that a tester has finished it.

**Re-cut record, 19 November 2026 (ODL-581’s merge, `5eba636`).** The three account templates are built, so the copy blocks in §2, §3a and §5 are now the built text, quoted verbatim from `templates/emails/invite.txt`, `confirm.txt` and `ready.txt`. Where draft 2 differed, the template is the copy of record: the ready subject is the `READY_SUBJECT` constant “Your machine is waiting.”; the confirmation says “confirm your address” and states no expiry, because no clock is measured; the invitation’s fallback is a reply to Savio’s message, not `help@`; the guide is named in a sentence at the shipped `https://odile.fyi/ceremony`, not a second imperative action at `test.odile.fyi`; and the ready email carries the reset page. Each template’s privacy, security and reliability sentences map to `docs/CLAIMS.md` rows in `BRAND.md` §Email; the footer and the no-desk sentence are `ROADMAP-ALPHA0` §1’s facts, not deck rows. No public claim is added.

**What was read for this page.** `docs/MVP-V0.1.md` §0–§3 (the 18 November re-cut); `docs/product/ROADMAP-ALPHA0.md` A1–A2 and G1–G17; `docs/architecture/ADR-account-credential.md` (ADR-006, merged at `3b4df95`) §1–§10; `docs/CLAIMS.md` v3.10 §1–§5 and §1a; `docs/security/CLAIMS-VERDICT-strict.md` §0.1, §1 A1–A11, §5; `docs/product/REVIEW-2026-11-17.md`; `docs/ops/GATE-PACKET.md` (17 November); `docs/backlog.yaml` A1/A2, ODL-341, ODL-569, ODL-572, ODL-575, ODL-577, ODL-583; Savio’s A2 envelope answer of 20 November 2026 (card `t_ad1783c4`); `templates/emails/invite.txt`, `confirm.txt` and `ready.txt` (at `5eba636`); `templates/emails/support.md`; `web/claim.html`; `web/first-visit.html`; `web/index.html`; `BRAND.md` §Voice, §Email; `installer/odile_install.py` `odile-first-run`; `docs/runbooks/image.md` §5.1; `odilectl/claim.py` `invite`/`ready` (`READY_SUBJECT`, `READY_GUIDE_URL`); `claude.com/pricing` (read 18 November 2026).

**What was not measured.** Register → ready has no end-to-end time on a fresh A2 machine; the first-visit report-above-form change and the account page do not exist; `ODL-341` and `ODL-583` are cut but not started, so the confirmation and ready emails are built but unsent; no tester has been recruited or watched. This page is written to be tested by the first cohort, not to report them.
