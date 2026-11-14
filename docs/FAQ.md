# Support — the five answers

**Source of truth for a reply from `help@odile.fyi` and for the `odile-help`
skill.** The public page is `web/faq.html`, served at
<https://odile.fyi/faq>. Every trust sentence below is a subset of a sentence
in `docs/CLAIMS.md`, or of a decided v3.9 row in
`docs/product/CLAIMS-RECUT-STRICT.md` §2/§5 that ODL-462 lands in the deck; the
row is named beside it. The backup restore landed with the M2 drills (ODL-215)
and the support-grant answer with ODL-217's drill, as v3.10 (ODL-474).

A user with a problem gets an answer without opening a ticket system. Five
moments cover the alpha. Each answer is short enough to paste into an email,
and none of them promises a reset at HQ, a refund or access Odile does not have.

## The five moments

| # | The moment | The answer in one line | Deck |
|---|---|---|---|
| 1 | The machine restarted and the workspace does not answer | It starts itself; the seal is the key | A5 |
| 2 | The Odile sign-in is lost | The recovery code resets it on the machine; no HQ reset | A7, A9 |
| 3 | The machine dies | We rebuild it; the recovery file restores last night's backup | A9 |
| 4 | A bill or a charge is questioned | $49 a month, $12 of inference included; Stripe holds the card | §1 Money and place (deck; unchanged in v3.9) |
| 5 | "Can you log in to my machine?" | No, unless you grant it; a grant is a handover | A1, A10 |

## 1. The machine restarted

A reboot is not a failure: after a reboot the machine starts itself, and the
workspace answers when the machine is back. Nothing waits at a prompt for a
password, and there is no SSH step in the user's path.

**Ask first:** does the workspace answer? If it does, nothing is broken. If it
does not answer after a few minutes, the machine is down; reply and use answer
3's rebuild.

**Reply:**

> After a reboot, it starts itself. The seal is the key. There is no passphrase
> to type.
>
> A repeating job — the morning brief, a watchdog — runs when the machine comes
> back up if it was due while it was down. If the workspace still does not
> answer, reply and I will take the next step with you.

**Deck:** v3.9 A5 — "After a reboot, it starts itself. The seal is the key." ·
v3.9 A3 — "The disk unlocks only on the boot you verify. Change the image, and
it stays locked." · v3.9 A4 — "There is no passphrase to type. The disk key is
sealed to the measured boot, and no one at Odile holds it." · v3.9 A11 — "A
repeating job — the morning brief, a watchdog — runs when the machine comes back
up if it was due while it was down."

## 2. The Odile sign-in is lost

The sign-in is set on the machine at first visit and only its hash is stored
there; HQ holds no copy. The way back is the recovery code shown once on the
first visit.

**Ask first:** do they still have the recovery code? If they do, the reset is a
browser step. If they do not, this is answer 3's rebuild.

**Reply:**

> A lost Odile sign-in is reset on the machine with your recovery code. There
> is no HQ reset. Open https://<slug>.odile.fyi/first-visit/reset, enter the
> code you saved from the first visit and choose a new password; a new code is
> shown once.
>
> If the recovery code is lost too, reply and the machine is rebuilt; if the disk
> will not open, you restore from your backup.

**Deck:** v3.9 A7 — "Your Odile sign-in is set on your machine at first visit
and hashed there. We never see it. It is the only app-layer gate: no 2FA in
V0.1." · v3.9 A8 — "At HQ we hold your email, the record of your machine, and a
boot key with no private half. Not the disk key, not your sign-in." · v3.9 A9 —
"A recovery code and a recovery file are the way back: the code resets your
sign-in, the file opens your backup. Your first visit gives you both; keep them
off the machine. While the machine is still running, the agent can show it
again." · v3.9 §5 — "No. There is no escrow and no passphrase. A lost sign-in is
reset on the machine with your recovery code; if the disk will not open, the
machine is rebuilt and you restore from your backup."

## 3. The machine dies

We rebuild it; you ask the agent to restore last night's backup with the
recovery file. Backed up nightly. Rebuildable in under an hour — without a key.
The drill is practiced before you get a machine.

**Ask first:** is the machine still running? If it is, the agent can show the
recovery file again. If it is gone, the rebuild makes a new machine.

**Reply:**

> We rebuild it; you ask the agent to restore last night's backup with your
> recovery file — the one you downloaded on your first visit. If the machine is
> still running and you lost your copy, ask the agent to show you the file.
> Target under an hour; your last day may be lost. The drill is practiced before
> you get a machine.
>
> Backed up nightly. Rebuildable in under an hour — without a key.
>
> Reply with the address you want the rebuild on and I will take the next step
> with you.

**Deck:** v3.9 §5 — "We rebuild it; you ask the agent to restore last night's
backup with your recovery file — the one you downloaded on your first visit." ·
"Backed up nightly. Rebuildable in under an hour — without a key." · v3.9 A9 —
"Your first visit gives you both; keep them off the machine. While the machine is
still running, the agent can show it again." · "The recovery file holds the
backup password and the certificate fingerprint." (deck, A9)

## 4. A bill or a charge

**Reply:**

> $49 a month, month-to-month. The $12 inference allowance is
> included and hard-capped; at the cap the agent stops and tells you. Stripe
> holds the card; we hold the customer id, the subscription id and whether it is
> paid. Reply here for anything on the charge; it is read by a person, and the
> target is an answer within 24 hours.

**Deck:** "The $12 inference allowance is included and hard-capped; at the cap
the agent stops and tells you." · "Stripe holds the card; Odile holds the
customer id, the subscription id and whether it is paid." · "Support and abuse
mail goes to help@odile.fyi and is read by a person; the target is an answer
within 24 hours." Price: `docs/MVP-V0.1.md` §0, $49/month.

Do not promise a refund, a credit or a discount. Refunds are Savio's; forward
the message and say the target is an answer within 24 hours.

## 5. "Can you log in to my machine?"

**Reply:**

> No one at Odile can log in to a tenant machine. A support grant is a
> time-boxed door into the running machine. It ends for new logins when it
> expires or you revoke it; an open session keeps running until it exits. A root
> shell, for 24 hours — only when you grant it.
>
> Ask the agent to run `odile-diag` and email the bundle to help@odile.fyi. If
> that is not enough, ask it to run `odile-support grant --hours 24`; it prints
> the expiry and the revoke command, and every session is logged. A grant is a
> handover: it can read the mounted disk, every file, the agent's memory and past
> prompts, the sign-in hash, and the backup password if the recovery file is
> still on the machine. Expiry and revoke block new logins; an open session
> keeps running until it exits. A root shell can add a keyslot, copy the volume
> key, or leave a service behind, so ask the agent to check `authorized_keys`,
> `cryptsetup luksDump` and `crontab -l` after revoke. The grant log is written
> on the machine; a root shell can edit it.

**Deck:** v3.9 A1 — "No one at Odile can log in to a tenant machine. The only
sign-in is yours." · v3.9 A10 — "A support grant is a time-boxed door into the
running machine. It ends for new logins when it expires or you revoke it." ·
v3.9 §1 — "A root shell, for 24 hours — only when you grant it." · "A grant is a
handover, not a peek." · v3.9 §5 — "Ask the agent to run `odile-diag` and email
the bundle to help@odile.fyi. If that is not enough, ask it to run
`odile-support grant --hours 24`; it prints the expiry and the revoke command,
and every session is logged. A grant is a handover: it can read the mounted disk,
every file, the agent's memory and past prompts, the sign-in hash, and the
backup password if the recovery file is still on the machine. Expiry and revoke
block new logins; an open session keeps running until it exits. A root shell can
add a keyslot, copy the volume key, or leave a service behind, so ask the agent
to check `authorized_keys`, `cryptsetup luksDump` and `crontab -l` after revoke.
The grant log is written on the machine; a root shell can edit it."

## The escalation ladder

Say it in this order. Each step is optional; the user stops when the problem is
solved.

1. **The public answers.** <https://odile.fyi/faq> — no JavaScript, no cookie,
   no third-party request.
2. **The workspace agent.** Ask it what broke; it runs on the machine and can
   read the logs.
3. **`odile-diag`.** The user runs it and emails the bundle to `help@odile.fyi`.
   It holds logs, versions, disk usage and service state — no password, no key,
   no prompt history, no file contents.
4. **A support grant.** `odile-support grant --hours 24` prints the expiry and
   the revoke command. It is a root shell for the hours named, and it is logged
   on the machine.
5. **A rebuild.** Last resort when the recovery code is lost or the disk will not
   open: We rebuild it; you ask the agent to restore last night's backup with
   the recovery file.

In the Webapp the same three exits are one command: `/odile-help`. It prints
the FAQ link, the `odile-diag` command and the grant command. Hermes's own
`/help` lists the engine's commands; `/help skills` lists Odile's skill under
`/odile-help`.

## What never goes into a reply

<!-- claims-lint:off -->

| Never | Why | Write instead |
|---|---|---|
| "Send us your sign-in password or the recovery code" | HQ holds no copy; a leaked one is the machine | "Copy the output of `odile-diag`" |
| "We can reset it" | There is no HQ reset | "Reset it on the machine with your recovery code" |
| "We can log in and fix it" | False by default | "Grant support for 24 hours" |
| "We can restore everything" | The restore is last night's backup; the last day may be lost | "Keep the recovery file; it restores last night's backup" |
| "Refund" / "credit" / "discount" | Money is Savio's, and no row exists | "The target is an answer within 24 hours" |
| "zero-knowledge", "unhackable", "trustless" | Forbidden in `docs/CLAIMS.md` §2 | The approved sentence beside the answer |

<!-- claims-lint:on -->

## How a reply is sent

Every reply leaves from the support mailbox on Odile's own mail server, so it
passes SPF, DKIM and DMARC and comes back to `help@odile.fyi` when the user
answers.

| Field | Value |
|---|---|
| From | `Odile <help@mail.odile.fyi>` |
| Reply-To | `help@odile.fyi` |
| Envelope sender | `help@mail.odile.fyi` (the only domain the submission account may use) |
| Subject | `Re: <their subject>` |
| Body | plain text; the answer above, under three short sentences before an address |
| Sign-off | `— Odile` |

The copy-paste bodies and the structure rules are in
`templates/emails/support.md`.

One deliverability fact, measured on 9 November 2026: Gmail files the first
message from a new sender address in Spam until the recipient marks it not
spam. `invite@mail.odile.fyi` is warm from the soak and the invites;
`help@mail.odile.fyi`, `support@mail.odile.fyi` and `desk@mail.odile.fyi` all
landed in Spam on first contact, and `help@` landed in the Inbox after one
not-spam action. Warm the support address to a seed list before the first
tenant, and tell the first ten to mark it once.

## How this is tested

The round trip is proved from outside the building, not by reading a config:

```sh
# 1. Inbound: Gmail -> help@odile.fyi, then read the envelope and auth headers
ls -t /var/mail/help/Maildir/new | head -1
grep -E '^(X-Original-To|Delivered-To|Authentication-Results)' <that file>

# 2. Outbound: the reply, from the support address
printf '%s\n' 'From: Odile <help@mail.odile.fyi>' \
  'Reply-To: help@odile.fyi' 'To: <the user>' \
  'Subject: Re: the machine restarted' '' '<the answer above>' \
  | sendmail -f help@mail.odile.fyi -t

# 3. Gmail's own verdict on the reply
./scripts/send_test.py --gmail-headers 'Re: <their subject>'
```

The run of 9 November 2026 is in `docs/evidence/odl-238/`.
