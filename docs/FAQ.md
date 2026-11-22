# Support — the five answers

**Source of truth for a reply from `help@odile.fyi` and for the `odile-help`
skill.** The public page is `web/faq.html`, served at
<https://odile.fyi/faq>. Every trust sentence below is a subset of a sentence
in `docs/CLAIMS.md` (v3.11, the one-password and one-code rows) or of a v3.9
row the deck keeps; the row is named beside it. The backup restore landed with
the M2 drills (ODL-215) and the support-grant answer with ODL-217's drill, as
v3.10 (ODL-474).

A user with a problem gets an answer without opening a ticket system. Five
moments cover the alpha. Each answer is short enough to paste into an email,
and none of them promises a reset at HQ, a refund or access Odile does not have.

## The five moments

| # | The moment | The answer in one line | Deck |
|---|---|---|---|
| 1 | The machine restarted and the workspace does not answer | It starts itself; the seal is the key | A5 |
| 2 | The Odile sign-in is lost | The recovery code resets it on the machine; HQ cannot reset the machine's sign-in | D-4, D-19 |
| 3 | The machine dies | We rebuild it; the recovery file restores last night's backup | A9 |
| 4 | A bill or a charge is questioned | No price in the alpha; Stripe holds the card | `docs/MVP-V0.1.md` §0; its allowance row is held, §1a |
| 5 | "Can you log in to my machine?" | No, unless you grant it; a grant is a handover | D-3, A10 |

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

**What it is.** The username and password that open your workspace. You chose it
when you registered; the machine holds a hash of it. A lost sign-in is reset with
your recovery code. HQ cannot reset your machine's sign-in.

**Ask first:** do they still have the recovery code? If they do, the reset is a
browser step. If they do not, this is the lost-code path (ladder step 6): a new
account, with the backup as the only data path.

**Reply:**

> One recovery code resets both: your account password at the account page and
> your machine's sign-in on the machine. There is no emailed reset.
>
> Not the machine's sign-in. There is no escrow and no passphrase. A lost Odile
> sign-in is reset on the machine with your recovery code; HQ cannot reset your
> machine's sign-in. Your account password is reset at the account page with the
> same code. If the disk will not open, the machine is rebuilt and you restore
> from your backup.
>
> Open https://<slug>.odile.fyi/first-visit/reset, enter the recovery code you
> saved at your first sign-in, then choose a new sign-in. This changes this
> machine's sign-in; your account password is reset at the account page with the
> same code. Your recovery code works at both places until you replace it; using
> it does not spend it.
>
> If you lose your recovery code there is no other way back: you start a new
> account, and a backup is the only way to bring the files.

**Deck:** v3.11 D-4 — "One recovery code resets both: your account password at
the account page and your machine's sign-in on the machine. There is no emailed
reset." · D-14 — "Enter the recovery code you saved at your first sign-in, then
choose a new sign-in. This changes this machine's sign-in; your account password
is reset at the account page with the same code." · D-20 — "Your recovery code
works at both places until you replace it; using it does not spend it." · D-5 —
"If you lose your recovery code there is no other way back: you start a new
account, and a backup is the only way to bring the files." · D-19 — "A lost
Odile sign-in is reset on the machine with your recovery code; HQ cannot reset
your machine's sign-in."

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
"Backed up nightly. Rebuildable in under an hour — without a key." · v3.11 D-6 —
"Your first visit gives you both; keep them off the machine. While the machine is
still running, the agent can replace the code and show you the new one." · "The
recovery file holds the backup password and the certificate fingerprint." (deck, A9)

## 4. A bill or a charge

**Reply:**

> The private alpha has no price: Odile pays for the machine and the inference.
> Stripe holds the card; we hold the customer id, the subscription id and
> whether it is paid. Reply here for anything on the charge; it is read by a
> person, and the target is an answer within 24 hours.

**Deck:** "Stripe holds the card; Odile holds the customer id, the subscription
id and whether it is paid." · "Support and abuse mail goes to help@odile.fyi and
is read by a person; the target is an answer within 24 hours." Price: alpha.0
has none — `docs/MVP-V0.1.md` §0 says so, and §8 keeps the v5 $49/month as
**not current**. The reply above, the one-line answer in the table, the invite
page (`web/claim.html`) and the support template
(`templates/emails/support.md`) now state no price (`t_02310cbf`): the page's
notice reuses the certified invitation and email-footer wording — nothing to
pay, and Odile pays for the machine and the inference — and the ready email's
footer already carries it (`238c22b`, ODL-581). **The allowance sentence is
held** (17 November 2026, `t_ddcb00fc`): "The $12 inference allowance is
included and hard-capped; at the cap the agent stops and tells you." is §1 Money
and place, now in §1a —
`web/terms.html` (`4b09cac`) and `web/faq.html` (`c8fe39b`) no longer carry it,
its proof (ODL-242, `t_8ec06788`) has not run, and the allowance's return to the
two pages waits on Savio's re-cut of the machine-and-price details. Do not quote
an allowance until it returns; the installed help (`skills/odile-help/SKILL.md`)
carried it, with the v5 price line, and its re-cut landed as `t_080f8a83`
(`a20566d`, merged at `6f4936b` — an installer re-embed and a re-sign), so its
bill answer now states alpha.0's no price. The served installer
(`odile.fyi/odile_install.py`) is still the pre-re-cut build until `t_a5cd44ba`
deploys from a clean `main`.

Do not promise a refund, a credit or a discount. Refunds are Savio's; forward
the message and say the target is an answer within 24 hours.

## 5. "Can you log in to my machine?"

**Reply:**

> No one at Odile holds a key to your machine: no standing key, no root password,
> no disk passphrase. A support grant is a time-boxed door into the running
> machine. It ends for new logins when it expires or you revoke it; an open
> session keeps running until it exits. A root shell, for 24 hours — only when
> you grant it.
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

**Deck:** v3.11 D-3 — "No one at Odile holds a key to your machine: no standing
key, no root password, no disk passphrase." · v3.9 A10 — "A support grant is a
time-boxed door into the running machine. It ends for new logins when it expires
or you revoke it." ·
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
5. **A rebuild.** Last resort when the disk will not open: We rebuild it; you ask
   the agent to restore last night's backup with the recovery file.
6. **A lost recovery code.** There is no other way back: ask us to delete your
   account and machine, then register again. Your backup is the only way to bring
   the files.

In the Webapp the same three exits are one command: `/odile-help`. It prints
the FAQ link, the `odile-diag` command and the grant command. Hermes's own
`/help` lists the engine's commands; `/help skills` lists Odile's skill under
`/odile-help`.

## What never goes into a reply

<!-- claims-lint:off -->

| Never | Why | Write instead |
|---|---|---|
| "Send us your sign-in password or the recovery code" | HQ holds only hashes of them; a leaked one is the machine | "Copy the output of `odile-diag`" |
| "We can reset it" | HQ cannot reset the machine's sign-in | "Reset it on the machine with your recovery code" |
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
