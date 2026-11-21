# Odile — claims deck (v3.10, the strict bar: no user SSH key, no passphrase, sealed boot, sign-in at first visit; one NEAR key per user, no meter)

**Rule:** no public sentence about privacy, security or reliability ships unless it is in this file. If a sentence is not here, it is not approved. `docs/MVP-V0.1.md` §7 is the rationale; this is the copy.

**Last reviewed:** 19 November 2026 · **Owner:** Savio · **Lint:** `tests/test_claims.py` (M0)

**Evidence verdict:** `docs/security/CLAIMS-VERDICT-strict.md` (ODL-466, evidence base `main` `8c2ad99`). Every access row carries its verdict there. A row marked **BLOCK** is approved here and **held from the public pages** until its card lands — the list is §1a, and the rule is the re-cut's §6: a row whose proof has not run is not published.

---

## 1. Approved sentences

Use these verbatim, or a subset that does not change the meaning. Anything stronger is a new claim and needs a new row.

### The machine

- "Odile gives you your own computer, on the internet, with an agent inside it."
- "Your own computer, with an agent inside it."
- "A real Linux machine, not a chat window."
- "One machine per person. Nothing is shared."
- "It keeps working when you close the tab."
- "Full agent with full privacy. Always-on agents running on confidential computers and powered by confidential models."
- "The waitlist hears first."

### The README opening (Savio's words, 19 November 2026)

Savio rewrote `README.md` on the public tree on 19 November 2026 (`2876eda`,
"Clean up"). A visitor reads these three first, so they are rows here.

- "Odile is a privacy-friendly AI agent that gets things done for you."
- "Every Odile agent runs on its own AMD SEV-SNP confidential virtual machine with encrypted memory and NEAR AI confidential model inference within hardware enclaves, enabling full confidentiality throughout agentic workflows."

The headline is his "Full agent with full privacy." — the v3.9d landing row
above, so it is not repeated here. The lede restates the 14 November lede with
a weaker adjective ("privacy-friendly" for "with privacy") and "for you" added:
the same row, naming no mechanism. The mechanism sentence is his, verbatim: "encrypted memory"
is §1 *Confidential computing*'s "Memory is encrypted by the CPU."; "NEAR AI
confidential model inference within hardware enclaves" is §1 *Inference*'s
"Inference runs on NEAR AI's confidential cloud, inside a hardware enclave."; and
"full confidentiality" is the v3.9d landing row's "full privacy". It claims no
access property, so the 14 November re-cut's reason (the host console, the
boot-image choice) does not bite. "Its own machine" is one VM per agent; the
demo's agent is Odile's, on Odile's tenant machine, and the demo rows below
scope that machine.

The 14 November rows this section carried (his headline, his lede, and the
mechanism sentence with its "outsider access" clause re-cut) are retired by the
19 November rewrite; the v3.10a change-log row keeps that record.

### Access — the core claim

- "No one at Odile can log in to a tenant machine. The only sign-in is yours."
- "No one at Odile can log in unless you grant it: no standing key, no root password, no disk passphrase."
- "The boot key in the command line is a public key with no private half. It is plumbing, not a credential."
- "The disk unlocks only on the boot you verify. Change the image, and it stays locked."
- "There is no passphrase to type. The disk key is sealed to the measured boot, and no one at Odile holds it."
- "After a reboot, it starts itself. The seal is the key."
- "Your Odile sign-in is set on your machine at first visit and hashed there. We never see it. It is the only app-layer gate: no 2FA in alpha.0."
- "Support access exists only when you grant it, for as long as you grant it."
- "A root shell, for 24 hours — only when you grant it."
- "We can stop it. We cannot read it."
- "A grant is a handover, not a peek."
- "On a tenant machine, Odile cannot read the disk, the prompts or the files; a support grant can."
- "A support grant is a time-boxed door into the running machine. It ends for new logins when it expires or you revoke it."
- "A recovery code and a recovery file are the way back: the code resets your sign-in, the file opens your backup. Your first visit gives you both; keep them off the machine. While the machine is still running, the agent can show it again."
- "The recovery file holds the backup password and the certificate fingerprint."
- "A repeating job — the morning brief, a watchdog — runs when the machine comes back up if it was due while it was down."
- "The status page counts uptime and names no tenant."

### The demo machine (the judge path, MH)

The demo runs on Odile's own tenant machine: Odile holds its boot key and can
log in, the judge password is published, and there is no nightly reset. These
rows exist so the judge-facing surfaces can say which machine they show, who
holds its key, and what survives — without borrowing a tenant-owned-key
sentence.

- "This is an Odile demo instance. Despite that it runs on confidential compute and inference, access is open to public. Everyone could see everything on this machine. Refrain from uploading personal and sensitive details."
- "This is Odile's tenant machine: one shared workspace on our measured AMD SEV-SNP machine. Odile holds this machine's boot key and can log in."
- "Every judge sees these sessions and files. There is no nightly reset: the machine keeps its state until Odile rebuilds it."
- "The demo password is public by design."
- "In this demo your prompts go from the browser to Odile's tenant machine and then to the NEAR AI enclave."
- "The demo shell runs as the same user as the profile, so it can spend against Odile's shared balance. It is not a tenant secret."
- "This demo comes back without you: no passphrase, no unlock step. The report refreshes every five minutes."

The sign-in page (Savio, 13 November 2026, card t_3d717ffc) carries the first
row above and nothing else: one **Sign in** button, the published `judge`
credential, no version credit. The tenant-machine, public-password and
shared-workspace rows are carried by the demo tenant's own `/verify`
(`deploy/demo-host/verify-lede.html`, served at `demo.odile.fyi/verify`), which
is their public home. The HQ app's `https://odile.fyi/verify` answers **302**
there (ODL-562): HQ's own guest is a different machine and has no tenant access
report. `web/verify.html`'s demo block, which quotes the tenant-machine row
verbatim, remains the HQ app's render; it is no longer served at
`odile.fyi/verify`, and HQ's own report is the JSON at
`https://odile.fyi/attestation`. `web/judge.html` carried the rows until
ODL-901 retired `/judge` to a 301 (Savio, 14 November 2026), and `README.md`
was simplified the same day to link the login page only. The no-nightly-reset
row stays approved here and has no public home until a card publishes it;
`deploy/demo-host/login.html` carries only the first row.

### First run and support

- "A machine is reserved for you. When you claim it, it is built and starts itself."
- "Claiming is a form and a confirmation. Then the machine builds itself: you check the report, set your sign-in, and start."
- "The installer sends only the setup token and the instance id to odile.fyi to fetch the signed seed."
- "Your Odile sign-in is the username and password that open the workspace; only its hash is stored, on the machine. There is no password and no account at HQ."
- "A lost Odile sign-in is reset on the machine with your recovery code. There is no HQ reset."
- "`odile-diag` bundles logs, versions, disk usage and service state. No password, no key, no prompt history, no file contents."
- "The agent runs as `odile` with sudo; it can read every file on the machine."
- "Invite and brief emails go out through our own mail server at mail.odile.fyi; it carries the message, never your disk."
- "The manifest is signed by Odile — provenance, not innocence."
- "Expiry and revoke block new logins. An open session keeps running until it exits or you kill it."
- "The grant log is written on the machine; a root shell can edit it."
- "A grant can read the mounted disk, every file, the agent's memory and past prompts, the sign-in hash, and the backup password if the recovery file is still on the machine."
- "It can add a LUKS keyslot or copy the volume key, so it can keep disk access after the grant."
- "Your slug is yours for the life of the tenant, so the URL and the recovery file's name stay the same."

### Money and place

- "The $12 inference allowance is included and hard-capped; at the cap the agent stops and tells you."
- "The machine is in Montreal, Canada. Backups are off-site."
- "The encrypted backup is kept 30 days after cancellation, then deleted. Odile can delete it, not read it."

**Inference path and allowance (Savio, 13 November 2026; ODL-205 retired, card
t_7f339817; mechanism measured by ODL-567, 19 November 2026).** There is no
meter, no proxy and no per-request ledger in the path: one NEAR AI key per user
is deployed at provision, the machine calls `https://cloud-api.near.ai/v1`
directly with it from `/etc/odile/key.env` (0600 root), and `odile-agent` reads
it at start. The cap is the provider's per-key `spendLimit`, set at mint; the
$12-a-month allowance is credit on that key, topped up by Odile at the billing
anchor, and the "hard-capped" row above is that limit. At the limit the
provider refuses the request with `402 api_key_limit_exceeded`; at zero credit
it answers `402 no_limit_configured`. Either way inference stops, the machine
keeps running, and the agent reports the provider's message; a top-up is a
manual Stripe link in alpha.0. The check is post-charge, so the last admitted
request can cross the limit by up to its own cost — measured at a $0.05 limit
on 19 November 2026 (`2,180` nano-USD over;
`docs/product/SPIKE-near-key-api.md` §7). ODL-242 makes issue → top-up → zero
→ revoke real and proves the zero-balance message; until it lands the tenant
lifecycle is the model, not a measured behaviour.

**Held 17 November 2026 (card `t_ddcb00fc`): the allowance row is not
publishable.** `web/terms.html` lost its "The machine and the price" section on
14 November (`4b09cac`) and `web/faq.html` lost the inference-cost answer on
17 November (`c8fe39b`), so no web page carries the row; Savio is re-cutting the
machine-and-price details, and no replacement copy is invented here. Its proof
never ran either: ODL-242 (`t_8ec06788`) is still open — parked for A2's window
by the ODL-573 re-cut — so "at the cap the agent stops and tells you" is the
model, not a measured behaviour. §1a holds the row. It was live on the
installed help (`skills/odile-help/SKILL.md`), whose copy rides in the signed
installer (`installer/odile_install.py` and `release/odile_install.py`), until
`t_080f8a83` re-cut it — `a20566d`, merged at `6f4936b`: a re-embed and a
re-sign, release `2026.11.20-1` — and the help's bill answer now states
alpha.0's no-price line, so no public page, price home or installed surface in
the repo carries the v5 price or the held allowance. The *served* installer is
still the pre-re-cut build (`odile.fyi/odile_install.py`, `1d946c44…`,
1,503,889 B); `t_a5cd44ba` deploys from a clean `main` that carries the re-cut.
The support template's money copy left with `t_02310cbf` (`ddcde16`). The price is
not a deck row: it left the invite page (`web/claim.html`) and the support template
with `t_02310cbf` (`ddcde16`), and the ready email (`templates/emails/ready.txt`)
with ODL-581 (`238c22b`); `docs/MVP-V0.1.md` §0 now reads "Alpha.0 has no
price" and keeps the v5 $49/month in §8 marked **not current**. The ready
email's footer is
`Private alpha · Odile pays for the machine and the inference · no price`.
The alpha and ten-machine wording v3.9e retired on the site is gone from its
last home.

### Confidential computing

- "Memory is encrypted by the CPU. The hosting provider sees ciphertext."
- "The machine is an AMD SEV-SNP confidential VM. The report is served live at `/attestation`."
- "The boot is measured: firmware, kernel, initramfs and command line are hashed into a value the CPU signs."
- "The disk is encrypted with LUKS2. Its key is sealed to the measured boot."
- "Check the machine before you trust it: the verify page shows PASS or FAIL, and you set your sign-in only after PASS."
- "Each row is tagged: checked from your laptop, or read from the machine. A root shell can change the machine rows."

### Inference

- "Inference runs on NEAR AI's confidential cloud, inside a hardware enclave."
- "The model attestation report is available from NEAR AI's gateway, and your machine can fetch it."
- "Your prompts go from your machine to the enclave. They do not pass through Odile's servers."

### Data handling

- "No cookies, no trackers, no third-party requests."
- "We store your email address, your payment record at Stripe, and the metadata needed to run your machine."
- "At HQ we hold your email, the record of your machine, and a boot key with no private half. Not the disk key, not your sign-in."
- "Your prompts are not used to train models, per our inference provider's terms." *(Conditional: publish only after NEAR AI confirms this in writing.)*

The inventory below is the exact item list the privacy page carries (ODL-243).
Each sentence names part of what the three rows above already cover, or a store
on the machine that the page's second table carries; none of them widens the
access model. The schema is the evidence: `tests/test_legal.py` fails when a
column in any store is missing from `web/privacy.html`.

- "The record of your machine is its slug, its provider id, its address, its plan, its expiry, the release it was ordered to boot, its state, its boot host key and the times it changed."
- "The waitlist keeps the address, the day it arrived and the link in the confirmation; deleting the address leaves the count."
- "The claim and setup tokens are kept as hashes; the token you were sent is not stored."
- "A heartbeat is a signed note from your machine: that it is up, its disk, uptime, spend and versions, the last backup and the last brief. It carries no prompt and no file."
- "Your machine's NEAR AI key is one per user: the only inference credential on the machine, written at install and read by the agent."
- "A support grant is the reason, the start and the expiry at HQ, and a line in the log on your machine."
- "Stripe holds the card; Odile holds the customer id, the subscription id and whether it is paid."
- "The machine record stays after the machine is destroyed; the event log is append-only and holds no credential."
- "Our web server keeps no access log; mail logs keep addresses, times and delivery status for four weeks."
- "The signup rate limit keeps a short hash of the IP address in memory for ten minutes; it never touches disk."
- "Support and abuse mail goes to help@odile.fyi and is read by a person; the target is an answer within 24 hours."

### Reliability

- "Runs around the clock. Uptime is published on the status page."
- "Backed up nightly. Rebuildable in under an hour — without a key."

## 1a. Held from the public pages — the row is approved, its proof or its landing has not run

`docs/product/CLAIMS-RECUT-STRICT.md` §6: the deck can carry a decided row
ahead of its proof; the landing card holds the page sentence until the drill.
ODL-466 (`docs/security/CLAIMS-VERDICT-strict.md`) is the verdict on each row.
A held row is published by the card named here, in a deck row that already
says what it will say. One row is held for a different reason: the allowance's
return to `web/terms.html` and `web/faq.html` waits on Savio's re-cut of the
machine-and-price details rather than on a card, while the money copy on
`web/claim.html` and the support template left with `t_02310cbf` (`ddcde16`),
the ready email's with ODL-581 (`238c22b`), and the installed help's with
`t_080f8a83` (`a20566d`, merged at `6f4936b`: a re-embed and a re-sign); its
cell says which half is which. ODL-217
(support grant) merged at `ebaf266` and
ODL-215 (backups and the restore drill) merged at `9f43829`, both on
13 November 2026;
**ODL-474 landed their sentences as v3.10 on 14 November 2026** — A9's backup
clause, the machine-dies answer, the reset/unlock answer's restore half, the
"Backed up nightly" row and A10's support-grant answer are published on
`web/faq.html`, `web/terms.html`, `web/claim.html` and the installed help
surface, and the installer now places `odile-support` and `odile-diag`.
**ODL-474's follow-up (`t_5f10dd14`) landed the 30-day retention row on 19
November 2026** — "The encrypted backup is kept 30 days after cancellation,
then deleted. Odile can delete it, not read it." is on `web/privacy.html`'s
retention list and in `web/faq.html`'s "Where is the machine?" answer — and
corrected the privacy page's backup destination cell to "an SFTP storage box
at HQ", because ADR-004 §1 measures the bytes on HQ's disk. The place row's
second sentence, "Backups are off-site", is still held: the same ADR measures
the destination as HQ's own disk, the same provider and the same city as the
machine. The table below is what is still held.

| Held sentence or clause | Why it is held | Lands with |
|---|---|---|
| A1, "The only sign-in is yours." | before the first visit the image's public bench password opens the workspace, and the session secret is the image's (§0.2) | `t_adcdec31` |
| A7, "It is the only app-layer gate: no 2FA in alpha.0." | same pre-first-visit gate (§0.2) | `t_adcdec31` |
| §4.2, "the only sign-in is yours" | same (§0.2) | `t_adcdec31` |
| §2 replacement, "the only sign-in is yours" | same (§0.2) | `t_adcdec31` |
| §5 "What is the Odile sign-in?", the only-gate sentence | same (§0.2) | `t_adcdec31` |
| A3, the whole row | the seal wraps a keyslot key, not the volume key; the published image's key opens the tenant disk (§0.1) | `t_28dda3b7` |
| A4, "The disk key is sealed to the measured boot, and no one at Odile holds it." | the disk key is the build key (§0.1) | `t_28dda3b7` |
| A8, "Not the disk key" | the disk key is recoverable from the published image (§0.1) | `t_28dda3b7` |
| §3 disk row, "its key is sealed to the measured boot" | a keyslot key is sealed, not the disk key (§0.1) | `t_28dda3b7` |
| §4.3, "No human keyslot; the seal." / "Odile never had a key or a passphrase." | Odile built the keyslot passphrase and published it on the image; the master key is the build key (§0.1) | `t_28dda3b7` |
| §4.8, "The host sees ciphertext" | the disk half: the host holds the bytes and the published image's key opens them (§0.1) | `t_28dda3b7` |
| gap 3, "A changed image or firmware breaks the seal; the machine is rebuilt." | a changed image carrying the recovered key reads the disk without the seal (§0.1) | `t_28dda3b7` |
| "Can Odile log in to my machine?", "We can stop or delete the machine, but we cannot read it." | the disk half (§0.1) | `t_28dda3b7` |
| "Can Servury read my disk?" | the disk half (§0.1) | `t_28dda3b7` |
| "What do you store?", "Not the disk key" | the disk half (§0.1) | `t_28dda3b7` |
| "We can stop it. We cannot read it." | the disk half (§0.1) | `t_28dda3b7` |
| "On a tenant machine, Odile cannot read the disk, the prompts or the files; a support grant can." | the disk half (§0.1) | `t_28dda3b7` |
| "Memory is encrypted by the CPU. The hosting provider sees ciphertext.", the second sentence | the disk half (§0.1) | `t_28dda3b7` |
| "Can you reset my sign-in or unlock my disk?", the "unlock my disk" half | the disk key is the build key (§0.1); the restore half landed with ODL-474 (v3.10) | `t_28dda3b7` |
| "The machine is in Montreal, Canada. Backups are off-site.", "Backups are off-site." | "The machine is in Montreal, Canada." is on `web/faq.html` and `web/privacy.html`. "Backups are off-site" is not true at this head: ADR-004 §1 (merged `9140dd6`) measures the bytes on HQ's own disk — the same Servury provider and the same city as the machine — and §9 makes the sentence true only when the R2 provisioning card (§8) and its drill land | `t_66fcaa02` |
| §1 Money and place, the whole allowance row, "The $12 inference allowance is included and hard-capped; at the cap the agent stops and tells you."; and §5 "What does inference cost?", the whole answer, which carries it verbatim | the price and the allowance are Savio's and he is re-cutting the machine-and-price details; the web pages that carried it are gone (`4b09cac` on 14 November, `c8fe39b` on 17 November), and its proof never ran — ODL-242 (`t_8ec06788`) is still open, parked for A2's window. It was live on the installed help (`skills/odile-help/SKILL.md`, embedded in `installer/odile_install.py` and the signed release copy `release/odile_install.py` — a re-cut there is a re-embed and a re-sign) until `t_080f8a83` re-cut it (`a20566d`, merged at `6f4936b`, release `2026.11.20-1`); after that cut the held sentence rides on no live surface in the repo, which is why the row stays held, not live. The support template's money copy left with `t_02310cbf` (`ddcde16`). | The allowance's return to `web/terms.html` and `web/faq.html` — **no card yet**; it waits on Savio's re-cut of the machine-and-price details, and the row returns verbatim or re-cut. The price homes are re-cut — the ready email with ODL-581 (`238c22b`), `web/claim.html` and `templates/emails/support.md` with `t_02310cbf` (`ddcde16`) — and the installed help's re-cut landed as `t_080f8a83` (`a20566d`, merged at `6f4936b`); the served installer (`odile.fyi/odile_install.py`, still `1d946c44…`, 1,503,889 B) reaches it when `t_a5cd44ba` deploys from a clean `main`. |
One decision in v3.9 is **not** a hold, and it is recorded here so the table
stays a list of sentences a card will publish:

- **"Claiming takes 3 minutes" is dropped, not held.** The claim page's timing
  was never clocked and BRAND forbids an unmeasured number (ODL-466 §2). The row
  and the page now say "Claiming is a form and a confirmation."

The two status rows were re-cut, not held: ODL-467 removed owner-waiting from
the uptime sentence and from the status-page sentence, and the page's uptime
math no longer carries an owner-wait bucket. `templates/status.html` and
`web/terms.html` carry the decided text above; `status/status.py` counts
`ready` time only, and `app.py` serves the page at `/status`.

---

## 2. Forbidden

<!-- claims-lint:off -->

| Never say | Why | Say instead |
|---|---|---|
| "unhackable" | Nothing is | "Hardened; here is what it does and does not protect" |
| "zero-knowledge" | The host runs the console; Odile controls DNS and can stop the machine | "Operator-blind; host-blind at rest" |
| "anonymous" | Email + payment | "No name, no phone, no KYC" (only if true of the flow) |
| "trustless" | You trust AMD, NEAR, Servury and Odile's provisioning | "Verifiable" |
| "military-grade encryption" | Meaningless | "AES-256-XTS for disk, SEV-SNP for memory" |
| "provably private" | Only parts are proven | "Measured boot; live attestation; published evidence" |
| "we hold your keys" | False in alpha.0 | "There is no user key; the only sign-in is yours." |
| "we can log in" | False by default | "No one at Odile can log in" |
| "operator access" as a feature | It is the thing we removed | "User-only access" |
| "no one can access your machine" | The host console exists | "No one at Odile can log in" |
| "we can't see your data" | We see metadata and can stop the machine | "We cannot read the disk or the prompts" |
| "your data never leaves the machine" | It goes to inference | "Prompts go only to the inference enclave" |
| "fully audited" | One external audit, scoped | "Audited for X; findings published" |

Lint list: `unhackable`, `zero-knowledge`, `zero knowledge`, `trustless`, `military-grade`, `provably private`, `we hold your keys`, `we can log in`, `operator access`, `no one can access`, `can't see your data`, `cannot see your data`, `never leaves`.

Retired from this list on 2026-11-14: `full privacy` — it was banned because no
row carried it. Savio made it his own landing sentence (v3.9d), so the row now
approves it verbatim and a generic ban would only flag the page the deck allows.

<!-- claims-lint:on -->

---

## 3. The three gaps we state before anyone asks

Put these in the FAQ and on `/verify`, in this order:

1. **"The app layer is not hardware-measured yet."** Measured boot covers firmware, kernel, initramfs and command line. Hermes, the installer and the skills are pinned by a signed manifest and a published hash, but the CPU does not measure them. The installer is signed, inspectable and audited at handoff for keys, passwords and keyslots; only a measured image can prove there is no hidden backdoor. The manifest records the machine as provisioned; anything you install afterwards is yours.
2. **"The console passes through the host."** Serial and VNC can be watched. Never type a secret there; the sign-in goes over TLS to the guest, after the report checks out.
3. **"Odile keeps three powers."** We can stop or delete the machine (availability, not confidentiality); we control DNS for your slug (pin the certificate fingerprint in your recovery file — the one your machine first served; a renewal changes it) and we can set a different boot image from the panel (detectable via the measurement — compare it to the one in your ready email before you set your sign-in). A changed image or firmware breaks the seal; the machine is rebuilt.

> **15 November 2026 — the boot-image mechanism is broader than the panel; no
> approved sentence changes.** The third power is unchanged: whoever holds the
> reseller key for the account chooses the image the node starts, the node
> checks the declared SHA-256 at every start, and the user's check — compare the
> measurement to the value in the ready email before the sign-in — is what makes
> a swap detectable. What is broader is the route: the image is also set by
> `POST ?action=boot-image&id=…` with `{"url","sha256"}` (exercised
> 2026-11-12T20:08:03Z; `docs/architecture/ADR-image-vs-installer.md` §15.1),
> and that path appends nothing to any Odile log. The panel stays the route for
> an account with no reseller key, and the **disk** image is still panel-only
> (`?action=disk-image` → 404). The sentence is not re-cut here: it is true in
> substance, and its copies on `/verify` and in the installed help travel inside
> the signed installer (`installer/embed_verify.py`), so changing two words is a
> re-embed, a re-sign and a release cut. The next signed-installer release cut
> adds "or the API" to this row and every surface in one commit. Ruling by the
> Product Lead, card `t_bf99b807`; verifier review.

---

## 4. Evidence, with commands

Each claim has a check the user runs **on their own machine**, as root.

The access rows' evidence verdict is `docs/security/CLAIMS-VERDICT-strict.md`
(ODL-466), read against the first tenant (ODL-332, `docs/evidence/m1-tenant.md`).
A row it marks BLOCK is listed in §1a with the card that will prove it.

### 4.1 Zero usable user keys.

```sh
cat /root/.ssh/authorized_keys      # after boot
ssh-keygen -lf /root/.ssh/authorized_keys
```

`authorized_keys` shows the throwaway public key from the measured command line
(`/proc/cmdline`), and `ssh-keygen -lf` shows its fingerprint. Its private half
was never written, so no one can use it. There is no user key, no operator key,
no shared account.

### 4.2 No root password, no operator account

```sh
passwd -S root                     # L or NP, not P
sshd -T | grep -i '^passwordauthentication'   # no
getent passwd | grep -i odile      # only the agent's unprivileged user, if installed
```

Servury's panel has no guest agent and cannot reset root. There is no
operator account; the only sign-in is yours, unless you grant support (§4.7).

### 4.3 No human keyslot; the seal.

```sh
lsblk -f                                  # find the LUKS device
cryptsetup luksDump /dev/<luks-device> | sed -n '/Keyslots:/,/Tokens:/p'
```

`cryptsetup luksDump` shows no passphrase keyslot; the volume key is sealed to
the measured boot and unsealed by the initramfs hook on every boot. Odile never
had a key or a passphrase.

### 4.4 The boot was measured, and you checked it before you signed in.

The `/verify` page shows, per boot: the measurement, the seal policy, zero usable
user keys, the sign-in hash and no operator key, read from your own machine.
The raw report at `/attestation` carries the release id and the
command line that was measured. Compare the measurement to the one in your ready
email. The certificate fingerprint is in your recovery file (A9), not on
`/verify`. The optional deeper check is the verifier bundle, from your laptop:

```sh
curl -fsSLO https://odile.fyi/odile-verify.tar.gz
echo '99395155ae1d08e67f81e3a64096c918300992700d8fe8ccd3f8b16762073bde  odile-verify.tar.gz' | sha256sum -c -
tar xzf odile-verify.tar.gz
cd odile-verify && ./odile-verify --expect <measurement from your ready email> https://<slug>.odile.fyi
```

It prints PASS or FAIL, and the hash line proves you got the tarball this
command names. On macOS, `shasum -a 256 -c -` reads the same hash line. A
tenant's report names another chip than the demo's, so the verifier fetches
that report's own VCEK from AMD's KDS and checks it against the pinned ARK; the
machine's release selects its entry in the pin. On PASS, open the URL and set
your sign-in. The independent rebuild is §4.5 (`sev-snp-measure` + `snpguest`).

### 4.5 This is a genuine SEV-SNP guest

```sh
curl -s https://<slug>.odile.fyi/attestation | jq '{measurement, report_version, fetched_at}'
```

Rebuild the expected digest from the published release. The tenant boots a UKI —
one signed file holding the kernel, the startup system and the command line — so
the measurement is over the UKI, not over a loose kernel and initrd:

```sh
curl -fsSLO https://odile.fyi/releases/m1/odile-tenant.efi
sha256sum odile-tenant.efi        # 5cb31808… — the UKI the ready email's measurement was made from
curl -fsSLO https://servury.com/downloads/blackbox/2026.11.1/OVMF.amdsev.fd
sha256sum OVMF.amdsev.fd          # 8731846c… — Servury's published 2026.11.1 firmware
sev-snp-measure --mode snp --ovmf OVMF.amdsev.fd \
  --kernel odile-tenant.efi --vcpus 4 --vcpu-type EPYC-v4 --guest-features 0x1
# 9f01725a… — the measurement in your ready email
```

It must equal `measurement` in the report. Verify the report signature with
`snpguest` against the AMD VCEK chain.

### 4.6 The installer and app manifest

```sh
curl -s https://<slug>.odile.fyi/manifest | jq
sha256sum /usr/local/bin/odile-install.sh   # compare with manifest.installer_sha256
```

The first boot places the exact release installer at
`/usr/local/bin/odile-install.sh` (0755, root) and the signed manifest at
`/etc/odile/manifest.json` with its `.sig`; `/manifest` serves the signed bytes,
so `sha256sum` equals `manifest.installer_sha256`.

The manifest lists the Hermes commit, package versions, skill hashes, installer hash and model pin. It is signed with Odile's Ed25519 key; the public key is in your recovery kit and at `https://odile.fyi/odile-signing-key.pub`. This is an operator signature, not a hardware measurement — see gap 1.

### 4.7 Support access is off

```sh
odile-support status               # "no grant"
grep -c odile-support /root/.ssh/authorized_keys   # 0
```

When you grant it:

```sh
odile-support grant --hours 24     # prints the expiry and the revoke command
odile-support revoke
```

The grant is logged in `/var/log/odile-support.log` and shown on `/verify`.

### 4.8 The host sees ciphertext

```sh
dmesg | grep -i "Memory Encryption"
# Memory Encryption Features active: AMD SEV SEV-ES SEV-SNP
```

### 4.9 No trackers

```sh
curl -sI https://odile.fyi/ | grep -i content-security-policy
# default-src 'none'; ...
```

### 4.10 The brief is signed and sent by Odile's own mail server

Open the brief in your inbox and choose **Show original**. The headers show the
message was signed by us and handed straight to your provider's MX:

```
Authentication-Results: mx.google.com;
  dkim=pass header.d=mail.odile.fyi; spf=pass smtp.mailfrom=mail.odile.fyi;
  dmarc=pass header.from=mail.odile.fyi
DKIM-Signature: v=1; a=rsa-sha256; ... d=mail.odile.fyi; s=odile; ...
Received: from mail.odile.fyi (mail.odile.fyi [203.0.113.139]) by mx.google.com ...
```

Check the signing key and the server:

```sh
dig +short TXT odile._domainkey.mail.odile.fyi
dig +short MX mail.odile.fyi
```

The `Received` line goes from `mail.odile.fyi` to the recipient's MX; there is
no third-party relay between them. Our mail server is the only hop it adds, and
it carries the message, not the disk.

### 4.11 The sign-in is yours; HQ holds no password

```sh
curl -s -o /dev/null -w '%{http_code}\n' https://<slug>.odile.fyi/api/sessions   # 401
grep -o 'HERMES_DASHBOARD_BASIC_AUTH[A-Z_]*' "$HERMES_HOME/.env"
# HERMES_DASHBOARD_BASIC_AUTH_USERNAME, ..._PASSWORD_HASH, ..._SECRET
```

The sessions endpoint answers `401` until you sign in. The `.env` holds a
username, a scrypt hash and a session secret — never the password. HQ stores the
throwaway public key, a hash of your setup token and the row that is your
machine; there is no private half and no passphrase, and the sign-in hash is on
the machine.

---

## 5. Support answers

**"Can Odile log in to my machine?"** No. There is no user key and no private key for us to hold, no root password, no disk passphrase. We can stop or delete the machine, but we cannot read it.

**"What is the boot key?"** A public key in the measured command line whose private half was thrown away. It is plumbing, not a credential.

**"What is the Odile sign-in?"** The username and password that open your workspace. You set it on your machine at first visit, and it is hashed there; we never see it. It is the only app-layer gate: no 2FA in alpha.0. A lost sign-in is reset with your recovery code.

**"Do I need the boot key?"** No. You never see it. The boot is sealed; your sign-in is the only thing you set.

**"Can you reset my sign-in or unlock my disk?"** No. There is no escrow and no passphrase. A lost sign-in is reset on the machine with your recovery code; if the disk will not open, the machine is rebuilt and you restore from your backup.

**"Can Servury read my disk?"** After your first boot, no. Memory is encrypted by the CPU and the disk is LUKS2; they see ciphertext. They run the console, so never type a secret there.

**"What happens after a reboot?"** It starts itself. The disk key is sealed to the measured boot, so no one unlocks it; a repeating job that was due runs once when it comes back up.

**"Can you fix something for me?"** Ask the agent to run `odile-diag` and email the bundle to help@odile.fyi. If that is not enough, ask it to run `odile-support grant --hours 24`; it prints the expiry and the revoke command, and every session is logged. A grant is a handover: it can read the mounted disk, every file, the agent's memory and past prompts, the sign-in hash, and the backup password if the recovery file is still on the machine. Expiry and revoke block new logins; an open session keeps running until it exits. A root shell can add a keyslot, copy the volume key, or leave a service behind, so ask the agent to check `authorized_keys`, `cryptsetup luksDump` and `crontab -l` after revoke. The grant log is written on the machine; a root shell can edit it.

**"What happens if the machine dies?"** We rebuild it; you ask the agent to restore last night's backup with your recovery file — the one you downloaded on your first visit. If the machine is still running and you lost your copy, ask the agent to show you the file. Target under an hour; your last day may be lost. The drill is practiced before you get a machine.

**"Can I install anything?"** Yes. It is a Debian machine and you are root.

**"Where does the model run?"** In NEAR AI's confidential cloud, in a hardware enclave. Your prompts go from your machine to the enclave. They do not pass through Odile's servers.

**"What does inference cost?"** The $12 inference allowance is included and hard-capped; at the cap the agent stops and tells you. *(Held 17 November 2026, card `t_ddcb00fc`: this answer is the §1 Money and place allowance row, held in §1a; the installed help's moment 4 is re-cut to alpha.0's no-price answer — card `t_080f8a83` (`a20566d`, merged at `6f4936b`, a re-embed and a re-sign) — and the support template's copy left with `t_02310cbf` (`ddcde16`); the answer returns with the row when Savio re-cuts the machine-and-price details.)*

**"Where is the machine?"** The machine is in Montreal, Canada. Backups are off-site. The encrypted backup is kept 30 days after cancellation, then deleted. Odile can delete it, not read it. *(Held 19 November 2026, card `t_66fcaa02`: "Backups are off-site" is not true at this head — ADR-004 measures the bytes on HQ's disk, the same provider and the same city as the machine. Give the two live sentences and leave the middle one out until the R2 migration and its drill land.)*

**"What do you store?"** We store your email address, your payment record at Stripe, and the metadata needed to run your machine. At HQ we hold the boot key with no private half, a hash of your setup token, and the record of your machine. Not the disk key, not your sign-in.

**"Do you track me?"** No cookies, no trackers, no third-party requests.

**"Do you train on my data?"** No — per our inference provider's terms. (Do not publish until the written confirmation is on file.)

---

## 6. Change log

| Date | Change | By |
|---|---|---|
| 2026-11-05 | First version; claims aligned to V0.1 scope | Savio |
| 2026-11-05 | v2: user-only access; no operator key or escrow; support by grant; reboot waits for owner; three gaps replace two | Savio |
| 2026-11-05 | v3: M-1 review; support is a root-shell handover; grant cannot reach the initramfs after reboot; recovery file copied off and shredded; jobs run on unlock; claim fingerprint and second-copy gate; mail relay and agent privilege stated | Savio |
| 2026-11-05 | v3.1: add the Webapp-password gate and `odile-diag` bundle rows (ODL-094 R1-S6 decision) | Savio |
| 2026-11-06 | v3.2: add the /verify row-provenance row (ODL-095 R2-S1; owner direction) | Savio |
| 2026-11-06 | v3.3: self-hosted mail; the brief goes out through our own mail server; the relay row is replaced, and §4.10 carries the header/DNS check (D-002) | Product lead |
| 2026-11-07 | v3.4: Model A access re-cut — the **boot key** and **your Odile sign-in** enter the deck as named rows (§1 Access, §1 First run, §4.11), the Webapp-password row is re-cut to the sign-in, HQ's custody is stated, and the FAQ rows (alpha, cost, storage, tracking, the three boot/sign-in questions) are added. Sources: ODL-309 answer A (2026-11-06/07), `docs/product/RECUT-2026-11-07-model-a.md` §1–§2, `docs/MVP-V0.1.md` §0/§7. By ODL-237. | Product lead |
| 2026-11-07 | v3.4a: the grant row's "Webapp password hash" is aligned to "sign-in hash", and the FAQ's short version and meta description use the scoped no-login row (the read-through's accepted fix, applied to the page). ODL-237 review fix. | Product lead |
| 2026-11-08 | v3.5: demo-scope rows for the MH judge path (ODL-329): the demo machine, sharing and reset, the public password, the HQ inference path, the readable demo key, and the reboot/refresh. The tenant access row is scoped to "a tenant machine" so the demo cannot make it false. Wording is from `docs/product/REVIEW-UX-2026-11-07.md` §5 and §9. Approved on card t_cf01ae14 by the Product lead (2026-11-08) under `docs/AGENT-ORG.md` §3; a rejected row is deleted, never softened. Numbered v3.5 at the ODL-237 merge: two entries had claimed v3.4. | Product lead |
| 2026-11-08 | v3.6: the data inventory for the M4 legal pages (ODL-243) — machine record, waitlist, tokens-as-hashes, heartbeat, meter, grants, Stripe, retention and the two log rows — plus the 24-hour abuse-answer target. Every row restates the existing "what we store" rows; the access model is unchanged. `tests/test_legal.py` binds the list to the 13 tables, and `tests/test_claims.py` binds `web/privacy.html` to this section. Deck edit by the Product lead under `docs/AGENT-ORG.md` §3 (claims deck: Product lead + verifier review). | Product lead |
| 2026-11-08 | v3.6a: ODL-243 review fixes, wording only — the signup rate-limit datum is named as the **IP address** the code hashes (not "the address", which reads as the email elsewhere on the page), and the tenant no-read row is scoped "On a tenant machine" so the demo, whose key Odile holds, cannot make it false. No new claim; the access model and the demo rows are unchanged. Product lead under `docs/AGENT-ORG.md` §3, verifier review. | Product lead |
| 2026-11-09 | v3.6b: §4.4's command block only — the `curl` now names the unversioned verifier path Servury documents (the per-release path 404s for 2026.11.1), fails on a non-200 instead of saving the error page, and prints the audited sha256 `8c937f02a94e83e7618a98b16a961a0f09b309cefa4100abc1f05e994df30f97` for `sha256sum blackbox-verify` to compare; the fetched file is byte-identical to `blackbox-images/tools/blackbox-verify` in the 2026.11.1 source tarball (checked 9 November 2026). No approved sentence changed; if Servury republishes the verifier, this line moves with the hash in the same commit. ODL-402. | Product lead |
| 2026-11-10 | v3.7: the demo reset row is narrowed to the mechanism — the workspace returns to its starting files and the sessions are cleared — after ODL-391 measured that the 00:00 UTC reset never touches `cron/jobs.json`, `memories/` or `SOUL.md` while the demo's own seeded welcome already promised only the workspace (`docs/product/DECISION-demo-reset-scope.md`, card t_c1a51444). By the Product lead under `docs/AGENT-ORG.md` §3; verifier review. | Product lead |
| 2026-11-10 | v3.8: the demo inference row loses "it can read the demo's NEAR key and" — ODL-391 moved the key into `odile-demo-meter.service` (`/etc/odile-demo/meter.env`, 0600 root), so the demo shell can still spend against Odile's shared balance but a judge can no longer read the key (`/api/env/reveal` 404, `cat` Permission denied). The rest of the row is unchanged (no new sentence, no access-model change). §4.4's command block gains the macOS `shasum -a 256` substitution the recovery kit already carries (`templates/recovery-kit.md:46`), from the ODL-402 review note; no approved sentence changed by it. Card t_350b1643; by the Product lead under `docs/AGENT-ORG.md` §3; verifier review. | Product lead |
| 2026-11-13 | v3.8b: the demo moved to the tenant (ODL-503) — the demo-machine rows now name **Odile's tenant machine** (boot key held by Odile, no nightly reset) and the inference row no longer says the prompts pass through HQ; the judge page and README carry the same sentences and link the tenant's own /verify. Drafted by the Builder on card t_1840dfc2 from Savio's 2026-11-13 instruction ("this is Odile's tenant machine and Odile holds its boot key"); **approved verbatim by the Product lead on 2026-11-14 on card t_6b1ad2e7** under `docs/AGENT-ORG.md` §3. Measured at approval (2026-11-14 02:33–02:47Z): `demo.odile.fyi` resolves to the tenant `203.0.113.87`; `/verify` is `/attestation` at `9f01725a…f137` (SEV-SNP, report v5, 1184 B); the judge signs in with the published password, `/api/sessions` lists the shared sessions (64 at measurement, the seed among them) and the seed answers; the model route is Near AI direct (`https://cloud-api.near.ai/v1`, the one `HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY`, no meter token); `check_demo_public.py --password-file` passed 41/41 at approval (the check at `37372d4`). The HQ `web/verify.html` `<!--demo-->` lede still says "It is not a tenant machine" and is re-cut to this row on t_781fc5cb; the same page's "resets daily at 00:00 UTC" note is t_9c2d31f1. | Product lead |
| 2026-11-10 | v3.8a: the machine-record inventory row names **the release the machine was ordered to boot** — ODL-398 added `tenants.release` and `tenant_state.release`, and `web/privacy.html` already discloses the pinned release in the approved machine-record row; this closes the one-item gap the Designer's brand review found in §1's "exact item list the privacy page carries". No page change, no access-model change, no new sentence outside the row. Numbered v3.8a, not v3.9: v3.9 is the decided strict-bar access re-cut (`docs/product/CLAIMS-RECUT-STRICT.md`, ODL-461) that ODL-462 lands, and this deck's follow-up rows take the next sub-letter (v3.4a, v3.6a, v3.6b). ODL-398 follow-up, card t_01974e10; by the Product lead under `docs/AGENT-ORG.md` §3; verifier review. | Product lead |
| 2026-11-13 | v3.9: strict-bar access re-cut — no user SSH key, no passphrase, sealed boot on the custom measured image, sign-in at first visit, recovery code + recovery file. A1–A11, the audience rows and the remaining key/passphrase/waiting/installer rows in §1, §4 and §5 re-cut or deleted: A6 and "One machine, one key, one owner" and "We never see the private half of your key" are cut; the boot key is plumbing with no private half (A2); the sign-in is set at first visit and reset by the recovery code (A7); the recovery file is delivered in the browser (A9); the grant is a time-boxed door closed by expiry or revoke (A10); a repeating job catches up once after a reboot (A11). The installed help surface is ODL-470; the recovery-file delivery is ODL-471; the installer and manifest placement is ODL-472; the tenant's own `/verify` access report is ODL-473; the claim and ceremony notes are ODL-459's (`8f41da0`) and this landing re-checks them verbatim. **Verdict:** `docs/security/CLAIMS-VERDICT-strict.md` (ODL-466, evidence base `main` `8c2ad99`, tenant `odl-fake01`, live checks 2026-11-12) — A2/A5/A11, §4.1/§4.4(row)/§4.5/§4.6/§4.7/§4.9/§4.10, gap 1/2, the audience rows, the claim/first-run rows, the recovery-file inventory row and most §5 answers PASS; the disk-custody cluster (A3/A4/A8's disk clause, §4.3, §4.8, gap 3, "we cannot read it", "Not the disk key") is BLOCKED on `t_28dda3b7` (the first-boot re-key); the sign-in cluster (A1's "only sign-in is yours", A7's "only gate", §4.2) is BLOCKED on `t_adcdec31` (the image's public bench password); A10 and the grant answer land with ODL-217; the backup sentences await ODL-215/ODL-474; §4.11's first clause awaits `t_22951d32`. Every blocked row is approved here and held from the public pages in §1a. §4.4's optional laptop verifier command is cut (it prints FAIL on a tenant; `t_8a0ef011`); §4.5's command block is the UKI form that reproduces `9f01725a…`; "Claiming takes 3 minutes" is dropped as unmeasured and the row now says "Claiming is a form and a confirmation"; the recovery kit's slug-continuity sentence (t_81ddb0f7) gains a row. Because `/verify` travels inside the signed installer (`installer/embed_verify.py`), the landing also re-embeds the page, re-signs the pair and cuts release `2026.11.12-5` (installer `42ca1846…`, 603,046 B; manifest `6e6a6df3…`; `docs/runbooks/install.md` §12.6), so a tenant's own page stops teaching the v3.7 gap 2. Sources: Savio 2026-11-10; docs/product/DECISION-strict-no-cli.md; docs/product/REESTIMATE-2026-11-10.md; ODL-310. The decided text is `docs/product/CLAIMS-RECUT-STRICT.md` (ODL-461); access rows decided by the Product Lead under D-002 (Savio direction 2026-11-10: no human card); verifier review; security claims verdict ODL-466 on the evidence; landed by ODL-462 after ODL-332. | Product lead |
| 2026-11-13 | v3.9a: the two status rows re-cut for the sealed boot (ODL-467) — the reliability row drops "with owner-waiting counted separately" and the status-page row becomes "The status page counts uptime and names no tenant." The uptime math changes with them: `status/status.py` counts `ready` time only, a legacy `waiting_for_owner` segment is neither up nor down and gets no bucket, and the incidents list is `down` only. `templates/status.html` and `web/terms.html` carry the decided rows verbatim; `app.py` serves the page at `/status`; `odilectl/health.py` raises `down` for a stopped heartbeat and sends no unlock email. Evidence: `tests/test_status.py`, `tests/test_heartbeat.py`, `tests/test_flow.py`, `make check`. Decided under the ODL-467 card (the re-cut table in `docs/product/CLAIMS-RECUT-STRICT.md` §2 assigns both rows to it); verifier review. | Product lead |
| 2026-11-13 | v3.9b: the demo sign-in page's copy is Savio's boundary sentence, verbatim — "This is an Odile demo instance. Despite that it runs on confidential compute and inference, access is open to public. Everyone could see everything on this machine. Refrain from uploading personal and sensitive details." The page loses the demo-machine, reset and public-password lines and the Hermes version credit; one **Sign in** button posts the published `judge` credential. The three older demo rows stay in the deck because `web/judge.html` and `README.md` still carry them. Savio's instruction of 2026-11-13, card t_3d717ffc: a record of his sentence, not a new claim (Product lead approval requested on that card; verifier review). | Savio via t_3d717ffc |
| 2026-11-14 | v3.9c: **the meter is retired from the product record** (ODL-205; card t_7f339817, from Savio's 2026-11-13 instruction "Remove everything meter in our project codebase… simply deploy a new Near AI API key for each new individual user, even in production"). The data-inventory meter row is replaced by the machine's own NEAR AI key — one per user, written at install, read by the agent, the only inference credential; §1 Money and place records the path and the cost model: no meter, no proxy, no per-request ledger, the key's prepaid credit is the allowance, Odile tops up at the billing anchor, and at zero the provider refuses and the agent reports it. The public "hard-capped" money row is unchanged — the key's balance is what makes it true — and ODL-242 proves the zero-balance message. History is not rewritten: the ODL-391/ODL-501/ODL-503 meter-and-cap records stay in `docs/backlog.yaml` with a retirement note, and `docs/security/CLAIMS-VERDICT-strict.md` carries one dated note at its meter rows. Code: `f884184` (merged `0a4b726`). By the Product lead under `docs/AGENT-ORG.md` §3; verifier review. | Product lead |
| 2026-11-14 | v3.9d: the landing description is Savio's own sentence, verbatim — "Full agent with full privacy. Always-on agents running on confidential computers and powered by confidential models." It replaces the v3.8 landing sentence on `web/index.html` (the visible lede, `<meta name=description>` and `og:description`), and the machine section gains it as one row: `always-on` and `confidential models` are approved as written, the second being the plain form of §1 *Inference*'s NEAR AI enclave row. `full privacy` leaves §2's lint list for the same reason it was on it — no row carried it, and now one does. Savio's instruction, 2026-11-14 16:05Z. | Savio |
| 2026-11-14 | v3.9e: the public site drops V0.1, version labels and the ten-person count — Savio's instruction of 2026-11-14 ("remove all mentioning of V0.1 and/or versioning, ten testers, etc. We'll define such details later"). §1's ten-people row becomes "The waitlist hears first."; the Reliability row becomes "Runs around the clock. Uptime is published on the status page."; §5 loses the "Why only ten people?" answer. `web/index.html`, `web/faq.html`, `web/terms.html` (which also loses its whole "The machine and the price" section under Savio's separate decision), `web/claim.html` and `templates/status.html` carry no V0.1/alpha/ten-people wording; live-verified, `make check` OK. Commits `b0b7186`, `4b09cac`. | Chief of Staff |
| 2026-11-14 | v3.9f: the last public copy that contradicted the demo rows. `web/verify.html`'s `<!--demo-->` lede is re-cut to the v3.8b tenant row **verbatim** — "This is Odile's tenant machine: one shared workspace on our measured AMD SEV-SNP machine. Odile holds this machine's boot key and can log in." — and its two `/judge` links (header and foot) go to `https://demo.odile.fyi/login`, which Savio made the judge's entry surface on 2026-11-14; `/judge` retires to a 301 (ODL-901). The note under §1 now names the surfaces that carry the demo rows after `web/judge.html`: the tenant's own `/verify` and the `web/verify.html` demo block, with `README.md` simplified to a login link (`9083922`, 2026-11-14T15:24Z) and the login page carrying only the v3.9b boundary row. **No approved sentence changed.** The page is byte-embedded in the signed installer, so this cut re-embeds and re-signs it as `2026.11.14-6` (installer `49f90a3c…`, 616,403 B; manifest `f6dea119…`; `docs/runbooks/install.md` §12.6). Measured 2026-11-14T16:40Z: `https://odile.fyi/verify` answered 200 with the old sentence (live is still `-4`: installer `6b42f8ef…`, 616,370 bytes, manifest `65ac178c…`, the 15:16Z deploy), and `https://demo.odile.fyi/login` answered 200 with one **Sign in** button; the next deploy publishes `-6`. Card t_781fc5cb; by the Product lead under `docs/AGENT-ORG.md` §3; verifier review. | Product lead |
| 2026-11-14 | v3.10: decided 13 November 2026; landed 14 November 2026. The five M2 sentences land — A9's "the file opens your backup" clause, the §5 machine-dies answer, the §5 reset/unlock answer's restore half, "Backed up nightly. Rebuildable in under an hour — without a key.", and A10's §5 support-grant answer — verbatim, no new sentence. Surfaces: `web/faq.html` (the reset/unlock answer, the machine-dies answer and "Can you fix something for me?"), `web/terms.html` (A9 and the reliability row), `web/claim.html` (A10 back in "Who holds what"), `docs/FAQ.md` (moments 2, 3 and 5), `skills/odile-help/SKILL.md` (the machine-dies moment) and `templates/emails/support.md` (bodies 3 and 5); `web/first-visit.html`'s two done pages already carried A9's full row and were re-embedded byte-identically. The installer now places `odile-support` and `odile-diag` (ODL-217's deferred step), so the A10 answer's two commands exist on a fresh tenant; the skill is re-embedded, the installer re-signed, and the release cut from the converged tree is named in `docs/runbooks/install.md` §12.6. **Evidence:** ODL-215 (`9f43829`: the nightly backup and the restore drill, RTO 38m03s, the restore into a rebuilt guest) and ODL-217 (`ebaf266`: the grant expiry and revoke drill); `tests/test_claims.py`, `tests/test_help_answers.py` and `tests/test_first_visit.py`; claims lint and `make check`. The two backup rows §1a still holds are `t_5f10dd14`: the 30-day retention sentence belongs on `web/privacy.html` (outside this card's `touches`) and "Backups are off-site" on `web/faq.html` (outside the five passages this card's DoD names). By the Product lead under `docs/AGENT-ORG.md` §3 (D-002); verifier review. | Product lead |
| 2026-11-15 | v3.10a: **the README opening enters the deck.** Savio rewrote `README.md` on the public tree while `t_781fc5cb` ran (`2d8999d`, `a5c3428`); its headline, its lede and one closing clause were privacy sentences no row carried, and the lint sees a forbidden phrase, not an unapproved sentence. Two rows are recorded **verbatim** and are his — "A full agent, privately." and "Odile is an AI agent that gets things done with privacy." — both the short form of the v3.9d landing row, naming no mechanism. The third is his sentence with its last clause re-cut: "protecting your agent from outsider access at the hardware level" restates §2's "The host console exists" row and is false on three measured counts (the console passes through the host, §3 gap 2; Odile keeps stop, DNS and the boot-image choice, gap 3; the served image's disk key is still the published build key, `t_51ebf972`). The re-cut states two rows the deck already proves — "Memory is encrypted by the CPU." and "Inference runs on NEAR AI's confidential cloud, inside a hardware enclave." — and claims no access property. `README.md` carries it; `tests/test_claims.py::ReadmeTests` now maps the README's opening block to this deck, as the FAQ and the privacy page already are, so a hand rewrite fails the gate instead of a read-through. No new claim, no access-model change. Card `t_0d4ebfd8`; by the Product lead under `docs/AGENT-ORG.md` §3; verifier review. **Numbering:** v3.10a — ODL-474's v3.10 merges first, so this row takes the a-suffix and the header stays v3.10; the rows' content does not change. | Product lead |
| 2026-11-15 | v3.10b: **§4.4 carries the optional laptop verifier command.** The paragraph that deferred it until `t_8a0ef011` proved it is replaced by the decided text's §4.4 body, verbatim from `docs/product/CLAIMS-RECUT-STRICT.md`: the four-line block (`curl`, `sha256sum -c` over `d200329260d66ce4ec545b8d34e05b262c06111ee506449c6c44958937250b53`, `tar`, `--expect <measurement from your ready email>`), the KDS sentence and the §4.5 pointer. **Proof:** `t_8a0ef011`, merged at `37372d4` — the published bundle is 20,610 B and byte-identical to `app.verifier_bundle()` at `main` `0d62fae`; `./odile-verify --expect 9f01725a…f137 https://odl-fake01.odile.fyi` printed every row PASS and exited 0 (vcek from AMD's KDS, chain to the pinned ARK, policy 0x30000, committed SNP 28 ≥ 27, release 2026.11.2); the demo prints PASS with no argument; a byte changed at `0x90` fails the signature and exits 1. Re-run on card `t_91b95124` at 2026-11-15T09:51Z: the download hashes to `d2003292…0b53` (`sha256sum -c` OK) and the tenant run is PASS, exit 0. The v3.9 row's cut stays as history; this is the additive re-cut. **No public page sentence changed** — `web/verify.html`'s "Check it yourself" block is the demo-only `<!--demo-->` block (`./odile-verify https://odile.fyi`, no `--expect`) and stays as it is. §1a's §4.4 row is ODL-520's held table (`t_9ada4233`), whose decision 2 owns how the row reads now; this landing does not edit it and claims no page. Card `t_91b95124`; by the Product lead under `docs/AGENT-ORG.md` §3; verifier review. | Product lead |
| 2026-11-15 | v3.10c: **no approved sentence changed; the boot-image power has a second route.** The image is also set by `POST ?action=boot-image` (exercised 2026-11-12T20:08:03Z; `docs/architecture/ADR-image-vs-installer.md` §15.1), and that path appends nothing to any Odile log — on 12 November a production tenant carried a test UKI for about 45 minutes (19:19:08Z–20:04:49Z) and no record names the writer. The row keeps "from the panel": the power and the user's check are unchanged, and the sentence's copies ride inside the signed installer, so two words cost a re-embed, a re-sign and a release cut. §3 carries the dated note; the next such cut adds "or the API" to the row and every surface in one commit. Card `t_bf99b807`; by the Product Lead under `docs/AGENT-ORG.md` §3 (D-002); verifier review. | Product lead |
| 2026-11-15 | v3.10d: **§4.4's verifier block gains the macOS hash line.** Stock macOS has no `sha256sum`, so the sentence after each block now says `shasum -a 256 -c -` reads the same hash line. The four command lines are unchanged byte-for-byte in all three homes — `docs/CLAIMS.md` §4.4, `docs/product/CLAIMS-RECUT-STRICT.md` §4.4 (the decided text) and `docs/runbooks/verify.md` §"A tenant run, from a laptop" — and the deck and the decided text still agree word for word through the §4.5 pointer. **No approved sentence changed**: the added line is a platform note, not a claim, and asserts no privacy, security or reliability property; no public page changes (`web/verify.html`'s block is the demo-only `<!--demo-->` form and stays). The v3.8 row recorded the same substitution once before, and the v3.10b landing took the decided text verbatim, which carries the GNU form only. Numbered v3.10d because v3.10c landed first (`t_bf99b807`). Card `t_496b4812`; by the Product lead under `docs/AGENT-ORG.md` §3; verifier review. | Product lead |
| 2026-11-16 | v3.10e: **odile.fyi/verify is the demo tenant's page, by redirect.** The HQ app (`ODILE_ROLE=demo`) answers **302** to `https://demo.odile.fyi/verify` for any Host other than `demo.odile.fyi`; the demo host still renders the page (the DNS-fallback shape), and `ODILE_ROLE=tenant` still renders the machine's own page on every Host. No approved sentence changed: the note under §The demo machine moves the demo rows' public home to `demo.odile.fyi/verify`, names `web/verify.html`'s demo block as the HQ app's render only, and points HQ's own report at `https://odile.fyi/attestation`. `web/verify.html`, the embedded installer page and `odile-verify.pin` are unchanged, so no re-embed or re-sign; `/attestation` keeps serving HQ's own JSON and the pin's source. **Measured after the deploy:** `curl -s -o /dev/null -w '%{http_code} %{redirect_url}' https://odile.fyi/verify` → `302 https://demo.odile.fyi/verify` (before: `200` with HQ's `e99728ef…fbc` under the v3.8b tenant row); `curl -sL https://odile.fyi/verify` → 200, 43,159 B, sha256 `3cdd7427…`, measurement `9f01725a…f137`, access report headed `read from your machine`, and the `./odile-verify https://demo.odile.fyi --expect 9f01725a…f137` command; `https://odile.fyi/attestation` still 200 with `available: true`, `e99728ef…fbc`, `fetched_at` `2026-11-16T00:55:21Z` and advancing. Numbered v3.10e because v3.10d landed first (`t_496b4812`). Card `t_627e01cd`; by the Product Lead under `docs/AGENT-ORG.md` §3; verifier review. | Product lead |
| 2026-11-17 | v3.10f: **the money row is held, not retired.** `web/terms.html` lost "The machine and the price" on 14 November (`4b09cac`) and `web/faq.html` lost the inference-cost answer on 17 November (`c8fe39b`, Savio: "Delete that as well"), so no web page carries §1 Money and place's allowance row. The row stays approved and moves to §1a, and §5's "What does inference cost?" answer, which carries it verbatim, gets the same dated note: the price and the allowance are Savio's and he is re-cutting the machine-and-price details, and the row's proof never ran — ODL-242 (`t_8ec06788`) is still open, parked for A2's window. Held, not retired, because the sentence is his number and returns verbatim or re-cut; no replacement copy is invented. It was not fully off the public surface on that date: it was live on the installed help (`skills/odile-help/SKILL.md`, embedded in `installer/odile_install.py` and the signed release copy `release/odile_install.py`) and on the support template (`templates/emails/support.md`), which `t_02310cbf` was to re-cut (done at `ddcde16`); the embedded copies make the re-cut a re-embed and a re-sign — recorded as a trigger, not re-signed now (the v3.10c pattern). The price was not a deck row: it was stated on the invite page (`web/claim.html`) and in the ready email (`templates/emails/ready.txt`), both `t_02310cbf`'s to re-cut then; the ready email's left with ODL-581 (`238c22b`) and the invite page's with `t_02310cbf` (`ddcde16`); `docs/MVP-V0.1.md` §0 records alpha.0's no-price decision and keeps the v5 $49/month in §8 as not current. `docs/FAQ.md` moment 4 quoted Stripe custody and the pre-alpha.0 price line and named the allowance row as held. **No web page changed and no new claim:** the one copy change is the support reply in `docs/FAQ.md` moment 4, which stops quoting the held row and is re-cut when it returns. Card `t_ddcb00fc`; by the Product Lead under `docs/AGENT-ORG.md` §3 (D-002); verifier review. | Product lead |
| 2026-11-19 | v3.10g: **the README opening follows Savio's 19 November rewrite.** He rewrote `README.md` on the public tree (`2876eda`, "Clean up") and replaced the 14 November headline, lede and mechanism sentence. The new headline is the v3.9d landing row verbatim; the new lede restates the 14 November lede with "privacy-friendly" for "with privacy" and "for you" added (a weaker adjective, naming no mechanism); the new mechanism sentence is his, verbatim, and restates §1 *Confidential computing*, §1 *Inference* and the landing row's "full privacy" without claiming an access property. Two rows replace the three in §The README opening. The same rewrite removes the demo `judge` credentials table and the "How NEAR fits in" section: the credentials are carried by the sign-in form (`deploy/demo-host/login.html`, as the runbook says), so no password is added anywhere new, and both demo NEAR rows stay approved (the tenant's seeded session still states the inference row verbatim and the prompt path); only their README home is gone. `tests/test_demo_verify_command.py::CopyTests` re-pins the README's NEAR sentence to the new row; `tests/test_claims.py::ReadmeTests` maps the opening as before. No access-model change, no new mechanism. Card `t_2b482267`; by the Product Lead under `docs/AGENT-ORG.md` §3; verifier review. | Product lead |
| 2026-11-19 | v3.10h: **the 30-day retention row lands; the place row's off-site half stays held.** `web/privacy.html`'s retention list gains "The encrypted backup is kept 30 days after cancellation, then deleted. Odile can delete it, not read it.", and `web/faq.html`'s "Where is the machine?" answer carries it beside "The machine is in Montreal, Canada.". The privacy page's backup destination cell stops saying "an off-site SFTP storage box" and says "an SFTP storage box at HQ": ADR-004 §1 (merged `9140dd6`, 19 November 2026) measures the nightly repository on HQ's own disk, the same Servury provider and the same city as the machine, and the old cell was wrong twice — the bytes are there and the off-site box was never bought. **The place row's second sentence did not land:** "Backups are off-site" is not true at this head and waits on ADR-004 §8's R2 provisioning card and its destroy→rebuild→paste→restore drill, card `t_66fcaa02`, held in §1a. No approved sentence changed: the retention and Montreal sentences are §1 rows, and the corrected destination cell is the inventory's facts, not a new claim. **The deck's call on ADR-004 §9's rebuild-path caveat** — the row is "true on the storage path; conditional during a rebuild" (a rebuild reads the user's recovery file on HQ through `--backup-password-file`, and the interim R2 cutover retires that path) — **is to publish it at this head**: the row's proof (ODL-215) ran, the caveat is a path-scope note and not a missing proof, and §1a holds only "Backups are off-site", which is false in fact. The caveat stays in this row and in ADR-004 §9 until the cutover makes the row true on both paths. `tests/test_claims.py` drops the retention clause from its held tuple, makes the off-site check case-insensitive, and pins the landed row on both pages. Card `t_5f10dd14`, landed by Growth executing v3.10's own delegation of these two rows (that row names this card as their landing); the deck decision is the Product lead's under `docs/AGENT-ORG.md` §3; verifier review. | Product lead |
| 2026-11-19 | v3.10i: **the deck's own bookkeeping about where the money copy lives is brought up to date; no approved sentence changed.** ODL-581 (`238c22b`) removed the price footer from the ready email (`templates/emails/ready.txt`), and `t_02310cbf` (`ddcde16`) re-cut the invite page (`web/claim.html`), the support template (`templates/emails/support.md`) and `docs/FAQ.md` moment 4 to the no-price answer. The §1 held note, the §1a intro, the §1a allowance row's reason and lands-with cells and the §5 dated note now say so, and the v3.10f row's present-tense clauses are re-cut to that row's date. The allowance row and its hold are unchanged: it stays in §1a until ODL-242 (`t_8ec06788`) proves the zero-balance message and Savio re-cuts the machine-and-price details. At this row's date the one repo-side surface still carrying the v5 price and the held allowance was the installed help (`skills/odile-help/SKILL.md`, embedded in `installer/odile_install.py` and `release/odile_install.py`); its re-cut was `t_080f8a83` — a re-embed and a re-sign, landed at `a20566d` (merged at `6f4936b`) and recorded in v3.10m. **No public sentence changed and no new claim.** Card `t_d0ae3149`; by the Product Lead under `docs/AGENT-ORG.md` §3 (D-002); verifier review. | Product lead |
| 2026-11-19 | v3.10j: **§1a's §4.4 row is deleted; the sentence is not held.** v3.10b (15 November) replaced the paragraph that deferred §4.4's optional laptop verifier with the decided text's §4.4 body — the four-line block, the KDS sentence and the §4.5 pointer — so §1a's row, which still named `t_8a0ef011` as the card that would land it, described a hold that no longer exists. Its reason also named the demo's vendored certificate, which `t_3cd62ef9` deleted on 19 November because it published the demo chip's silicon id; the row was stale on both counts. **No public sentence changed:** the §4.4 body is unchanged and `web/verify.html`'s demo-only "Check it yourself" block is untouched. Card `t_47291445`; by the Product Lead under `docs/AGENT-ORG.md` §3 (D-002); verifier review. | Product lead |
| 2026-11-19 | v3.10k: **G13 — the deck's five live version tags read alpha.0.** G13's rule (`docs/product/ROADMAP-ALPHA0.md` §4) and `docs/MVP-V0.1.md`'s "Names" paragraph make V0.0.1-alpha.0 the release the deck is current against, so five live statements are renamed and nothing else moves — no price, hold, landing card or verdict changes: §1 Access's A7 row, §5 "What is the Odile sign-in?", §1a's held A7 row, the §1 Money and place inference-path note's "manual Stripe link", and §2's forbidden-terms table, where the keys row's Why cell now reads "False in alpha.0" and its Say-instead stays "There is no user key; the only sign-in is yours.". The kit the tester receives (`templates/recovery-kit.md`) carries the A7 sentence verbatim with the same tag, and `tests/test_claims.py` and `tests/test_recovery_kit.py` pin the new text. **No re-embed and no re-sign:** `scripts/recovery_kit.py` renders the template on HQ, and neither `installer/odile_install.py` nor `release/odile_install.py` carries the sentence (measured 2026-11-19: `grep -c 'no 2FA in V0.1'` is 0 for both). **Held, not re-cut by this row:** this card's own change is the five tag replacements above and this row — `git diff 25202a6 2d68ffa -- docs/CLAIMS.md` is five tag-only replacements and this row (6 insertions, 5 deletions), no other hunk; every other move in this file since `25202a6`, including the `t_ddcb00fc` hold paragraph (moved by main's v3.10i, `t_d0ae3149`) and §1a's non-A7 moves (v3.10h, v3.10i and main's v3.10j, which deletes the §4.4 row), is a main row's, not this card's, and the only §1a line this card moves is the A7 held row above, tag only (this card's own tree at `2d68ffa`: 59 of 60 §1a lines identical to `25202a6`) — and `docs/FAQ.md`'s v3.9 A7 quote with `tests/test_help_answers.py`'s pin keeps V0.1 as marked history. **Numbering:** v3.10k — main's v3.10j (`t_47291445`) landed first, so this row takes the next free label. Card `t_317f6b87` (ODL-920); by the Product Lead under `docs/AGENT-ORG.md` §3; verifier review. | Product lead |
| 2026-11-20 | v3.10l: **the allowance's mechanism note names the provider's per-key `spendLimit`; no approved sentence changed.** ODL-567's funded re-run (`docs/product/SPIKE-near-key-api.md` §7, 19 November 2026) measured the cap: a key minted with `{"spendLimit":{"amount":50000000,"currency":"USD"}}` answered 28 billed requests, then `402 api_key_limit_exceeded` with `Spent: $0.05000218, Limit: $0.05`; the request admitted while usage was under the limit was billed in full, so usage ended `2,180` nano-USD over — the check is post-charge, and the last admitted request can cross the limit by up to its own cost. §1's mechanism note now says the cap is that per-key `spendLimit`, set at mint, and names `402 no_limit_configured` at zero credit; the old balance-only model is dropped. The approved allowance row is unchanged and stays held (§1a, v3.10f). **Measured at `1736352`** for the three files' pre-cut sentences and at the commit that carries this row for the post-cut grep (`exit 1`), both checked against §7; this row takes the next free letter at its merges with `main`; it stands at v3.10l after main's v3.10j (`t_47291445`) and v3.10k (G13, `t_317f6b87`). No web page, no installer copy and no new public sentence. Card `t_a933243a`; by the Product lead under `docs/AGENT-ORG.md` §3 (D-002); verifier review. | Product lead |
| 2026-11-20 | v3.10m: **the deck's bookkeeping about the installed help follows `t_080f8a83`'s landing; no approved sentence changed.** `a20566d` (merged at `6f4936b`) re-cut the installed help's bill answer to alpha.0's no-price line and re-embedded and re-signed the installer, cutting release `2026.11.20-1` (installer `d48f73aa…`, 1,503,831 B). The installed help — the last tester-facing surface in the repo that carried the v5 price — now states none, and the §1 held note, the §1a intro, the §1a allowance row's reason and lands-with cells, the §5 dated note and `docs/FAQ.md` moment 4's note now name that cut; the v3.10i row's present-tense clause is re-cut to its own date. The allowance row and its hold are unchanged: it stays in §1a until ODL-242 (`t_8ec06788`) proves the zero-balance message and Savio re-cuts the machine-and-price details, and after `a20566d` the held sentence rides on no live surface in the repo, which is why the row stays held, not live. The **served** installer at `odile.fyi/odile_install.py` is still the pre-re-cut build (`1d946c44…`, 1,503,889 B; `/opt/odile` on release `2026.11.19-5`, `release.json` synced 06:46:41Z, before `a20566d` at 08:13:16Z); `t_a5cd44ba` deploys from a clean `main`, which carries the re-cut. **No public sentence changed and no new claim.** Card `t_439f8488`; by the Product Lead under `docs/AGENT-ORG.md` §3 (D-002); verifier review. | Product lead |
| 2026-11-21 | v3.10n: **§4.4's `sha256sum -c` line names the bundle this tree builds; no approved sentence changed.** The pin is one of the five reviewed files in the archive, so `t_a37c001d`'s 2-vCPU plan (release `2026.11.2`, `vcpu_counts [2, 4, 6]`) moved the tarball from 20,560 B to 20,562 B, and §4.4's command block now checks `99395155ae1d08e67f81e3a64096c918300992700d8fe8ccd3f8b16762073bde`; the block is byte-for-byte `docs/product/CLAIMS-RECUT-STRICT.md` §4.4 and its other three lines are unchanged. **Proof:** at `main` `5d6eb1d` and again at this card's base `da5dce1`, `app.verifier_bundle()` is 20,562 B and hashes to that value; `curl -fsSL https://odile.fyi/odile-verify.tar.gz` returns the same 20,562 B and hash; the demo run prints `release PASS 2026.11.2 is pinned (3 artifacts, vcpu_counts [2, 4, 6])` and exits 0 (`t_a37c001d`'s evidence, 2026-11-20T07:40Z, re-run by its verifier); and `tests/test_verify.py::BundleTests.test_the_published_hash_is_the_bundle_in_this_tree` binds that same hash into `docs/runbooks/verify.md`, `docs/product/CLAIMS-RECUT-STRICT.md` and `docs/security/CLAIMS-VERDICT-strict.md`. The 19 November value it replaces is the one in that verdict's 19 November bundle paragraph; it is not re-quoted here. No public page, installer copy or release changed, so no re-embed and no re-sign. Card `t_b0cc9015`; by the Product Lead under `docs/AGENT-ORG.md` §3 (D-002); verifier review. | Product lead |
