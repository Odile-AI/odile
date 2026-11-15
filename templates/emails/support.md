# Support email — the reply template

**Card:** ODL-238 · **Re-cut for the sealed boot:** ODL-470 · **Used by:** a
person answering `help@odile.fyi` · **Answers:** `docs/FAQ.md`.

A support reply is a page, not a room: plain text, one column, no image, no
tracking pixel, no third-party request. It leaves from Odile's own mail server
and comes back to `help@odile.fyi` when the user answers. The five bodies below
are subsets of `docs/CLAIMS.md` (v3.10) and `docs/FAQ.md`; do not add a sentence
that is not in either.

## Envelope

| Field | Value |
|---|---|
| From | `Odile <help@mail.odile.fyi>` |
| Reply-To | `help@odile.fyi` |
| Envelope sender | `help@mail.odile.fyi` |
| To | the address that wrote to `help@odile.fyi` |
| Subject | `Re: <their subject>` — or the first sentence when they wrote none |
| Format | plain text; no HTML unless a link needs it; no attachment unless they sent one |
| Sign-off | `— Odile` |

The submission account may send only an envelope on `mail.odile.fyi`; OpenDKIM
signs that domain with selector `odile`, and the DNS records give SPF, DKIM and
DMARC a pass. Keep `From` on `mail.odile.fyi` too: the signature is made for
that domain, and a From on another domain would not align with it.

## Structure

1. Greeting. Their name if the message carries one; otherwise `Hello,`.
2. One paragraph. The answer, in three short sentences at most. The grant body
   (5) and the machine-dies body (3) are the long answers: both carry the deck's
   §5 text verbatim, and shortening them would drop a fact the user needs.
3. The address or the next step. One command, or one link, never a menu.
4. `— Odile`.

No "Hope this helps", no "Please don't hesitate", no exclamation mark, no emoji.
Do not quote their whole message back; quote the one line that matters when a
line matters.

## The five bodies

### 1. The machine restarted

```text
Hello,

After a reboot, it starts itself. The seal is the key. There is no passphrase
to type. A repeating job — the morning brief, a watchdog — runs when the machine
comes back up if it was due while it was down.

If the workspace still does not answer, reply and I will take the next step
with you.

— Odile
```

### 2. The Odile sign-in is lost

```text
Hello,

A lost Odile sign-in is reset on the machine with your recovery code. There is
no HQ reset. Open https://<slug>.odile.fyi/first-visit/reset, enter the code you
saved from the first visit and choose a new password; a new code is shown once.

If the recovery code is lost too, reply and the machine is rebuilt.

— Odile
```

### 3. The machine dies

```text
Hello,

We rebuild it; you ask the agent to restore last night's backup with your
recovery file — the one you downloaded on your first visit. If the machine is
still running and you lost your copy, ask the agent to show you the file. Target
under an hour; your last day may be lost. The drill is practiced before you get
a machine.

Backed up nightly. Rebuildable in under an hour — without a key.

Reply with the address you want the rebuild on and I will take the next step
with you.

— Odile
```

### 4. A bill or a charge

```text
Hello,

$49 a month, founding price, month-to-month. The $12 inference allowance is
included and hard-capped; at the cap the agent stops and tells you. Stripe holds
the card; we hold the customer id, the subscription id and whether it is paid.
Reply here for anything on the charge; it is read by a person, and the target
is an answer within 24 hours.

— Odile
```

### 5. "Can you log in to my machine?"

```text
Hello,

No one at Odile can log in to a tenant machine. The only sign-in is yours. A
support grant is a time-boxed door into the running machine. It ends for new
logins when it expires or you revoke it. A root shell, for 24 hours — only when
you grant it.

Ask the agent to run odile-diag and email the bundle to help@odile.fyi. If that
is not enough, ask it to run odile-support grant --hours 24; it prints the expiry
and the revoke command, and every session is logged. A grant is a handover: it
can read the mounted disk, every file, the agent's memory and past prompts, the
sign-in hash, and the backup password if the recovery file is still on the
machine. Expiry and revoke block new logins; an open session keeps running until
it exits. A root shell can add a keyslot, copy the volume key, or leave a service
behind, so ask the agent to check authorized_keys, cryptsetup luksDump and
crontab -l after revoke. The grant log is written on the machine; a root shell
can edit it.

— Odile
```

## When the message carries an `odile-diag` bundle

The bundle is an attachment, not a question. Read the last 200 lines, the
service states and the disk figure first; answer with the body that matches the
symptom, and name one fact from the bundle so the user knows a person read it.
Never paste a secret from a bundle into a reply. If the bundle holds a password,
a recovery code or a token, tell the user to rotate it and say which file.

## Never

| Never | Why |
|---|---|
| Ask for the sign-in password or the recovery code | Neither has a copy at HQ; a leaked one is the machine |
| Promise a reset at HQ, a refund, a credit or a restore of anything not backed up | No row in `docs/CLAIMS.md`; money is Savio's |
| Say Odile can log in | False by default |
| Send an HTML template, a logo, a tracking pixel or a third-party font | The no-tracker rule applies to mail too |
| Add a sentence that is not in `docs/FAQ.md` | The five answers are the deck's subset |

## The round trip, in one block

```sh
# inbound: a real message from outside to the support address
ls -t /var/mail/help/Maildir/new | head -1
grep -E '^(X-Original-To|Delivered-To|Authentication-Results)' "$(ls -t /var/mail/help/Maildir/new | head -1)"

# outbound: answer it from the support address
printf '%s\n' \
  'From: Odile <help@mail.odile.fyi>' \
  'Reply-To: help@odile.fyi' \
  'To: <the user>' \
  'Subject: Re: the machine restarted' \
  '' \
  'Hello,' \
  '' \
  'After a reboot, it starts itself. The seal is the key. There is no passphrase' \
  'to type. A repeating job — the morning brief, a watchdog — runs when the' \
  'machine comes back up if it was due while it was down.' \
  '' \
  '— Odile' \
  | sendmail -f help@mail.odile.fyi -t

# the recipient's provider's own verdict; keep the Re: prefix, or the Sent
# copy of the inbound message wins the subject search
./scripts/send_test.py --gmail-headers 'Re: the machine restarted'
```
