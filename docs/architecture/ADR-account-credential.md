# ADR-004 — The account password is not the instance sign-in

**Status:** Accepted · 17 November 2026 · Architect · revised 18 November 2026
after review round 1 (host naming per environment, counts, annex output)
**Answers:** ODL-569 · G3 of `docs/product/ROADMAP-ALPHA0.md` §4 — “is the
account password the instance sign-in, or are they two credentials?”
**Short answer:** no. **Two credentials, one link.** A2's registration collects an
email and a password that open the **account page and nothing else**; the
machine's sign-in is still set on the guest at first visit and hashed there, and
HQ holds nothing that opens it. The account is new, so one CLAIMS sentence is
falsified (“There is no password and no account at HQ”) and one inventory list
gains a row (“At HQ we hold …”, adding the account and a hash of its password);
A2 forces both whichever option wins. Every other access sentence in the deck
stays literal.
**Related:** `docs/MVP-V0.1.md` §2 (the access principle), §4 R1/R3/R6, §6.1
Invariants 2 and 4, §7 gap 3, §12 D14/D20/D23/D26; `docs/CLAIMS.md` §1 Access,
§1 First run, §1 Data handling, §1a, §4.11, §5; `docs/product/ROADMAP-ALPHA0.md`
§4 G3; `docs/architecture/DESIGN-claim-ceremony.md`; `docs/architecture/
ADR-image-vs-installer.md` §7; cards ODL-521, ODL-522, ODL-523, ODL-569,
ODL-572, `t_adcdec31`.
**Decision owner:** Architect for the recommendation as written: it keeps the
access model, adds one account verifier at HQ, and changes no public sentence
that A2 does not already change. **Option B needs Savio** — it makes HQ the
authority for a tenant's sign-in for the life of the machine. **Option C needs
Savio** and puts Odile in every request path.

---

## 1. The invariant at risk

The product's defining property is that the operator is not a credential holder
(`docs/MVP-V0.1.md` §2; D14 “standing; changing it is a new product”). Written
so it can be falsified:

- **I1 — no operator path opens a tenant machine between the first visit and a
  rebuild.** Not a key, not a passphrase, not a password, and not a value HQ can
  present to the machine's gate. §6 names the two windows that exist (the setup
  token before the first visit, and the rebuild's sealed backup password).
- **I2 — the guest is the only writer of the sign-in.** The scrypt hash in the
  tenant `.env` is produced on the machine, from a password typed into the
  machine's own page.
- **I3 — HQ is not in a request path** (§6.1 Invariant 4). The browser talks TLS
  to the guest; the guest talks TLS to NEAR. This is what makes “your prompts do
  not pass through Odile's servers” true.
- **I4 — the proof is checkable from the machine.** `/verify` shows the sign-in
  hash read from the guest, so the claim is not a promise about our code
  (R6).

The question this ADR settles is which of the three credential models keeps all
four, and what the answer costs. It does **not** decide the reset UX — that is
ODL-572 — but it binds it, and §5 states the constraint.

## 2. What exists today, and what it already promises

The pieces A2 must fit, with the lines that matter:

| Piece | Today | Where |
|---|---|---|
| Account | **Does not exist.** The waitlist, the claim spine and the tenant row do, keyed by `tenants.email UNIQUE` | `app.py:153-176` |
| The schema's own promise | “No passwords, passphrases, private keys or IP history are stored.” | `app.py:135-137` |
| The sign-in writer | The guest's first-visit service: single-use token → `scrypt$…` (n=2^14, r=8, p=1) → the tenant `.env` (0600), session secret kept byte for byte | `installer/odile_install.py:3229`, `:3604`, `:4222` |
| The recovery code | scrypt hash on the guest, generated in the same response, rotated on a reset; **not** single-use and **in a URL** | `:3303`, `:3556`, `:4303` (written, never read) |
| The token's route to the owner | The ready email: `/first-visit?t=<64 hex>`, composed and queued on HQ, delivered by `mail.odile.fyi`; the command line is also written to a 0644 log on the guest | `odilectl/claim.py:1185`, `templates/emails/ready.txt:4`, `deploy/image/odile-firstboot.py:1137-1138` |
| The seed contract | `SEED_KEYS` allow eight fields; `SEED_FORBIDDEN` is `password, passphrase, private_key, ssh_private_key, luks` — **the seed may not carry a password**; a rebuild carries a ciphertext sealed under the setup token | `installer/odile_install.py:184-188`, `seed/seed.py:308-334` |
| The instance username | fixed `odile`; the account's email is **not** the machine's username | `installer/odile_install.py:3187-3189` |
| The image's pre-first-visit hole | closed: no usable hash ships, the session secret is per machine, no SSH host key ships | ODL-521/522/523, all done |

Two consequences the options must live with:

1. **The setup token is an operator-obtainable path before the first visit.**
   HQ mints it, writes it into the ready email, and holds the mail queue and the
   relay (`mail.odile.fyi` is ours). It is also copied into
   `/var/log/odile-firstboot.log` at the image's default umask. So *before* the
   owner sets their sign-in, an operator can set one and log in. The promise is
   true **from the first visit onwards**; §4 states it that way, and the second
   copy of the token is one of the fixes §5 item 4 names. `docs/CLAIMS.md` §1a's
   A1 row still gives the image's bench password as its reason and names
   `t_adcdec31` as the card that lands it; that reason is cleared
   (ODL-521/522/523, all done) and the card is SUPERSEDED on the board, while
   the setup-token reason above survives. The row's why and landing line are the Product Lead's
   re-cut, outside this card's `touches`; this ADR names them so the deck is not
   read as cleared while the token window is open.
2. **A2 must put a session somewhere.** The public pages carry “No cookies, no
   trackers, no third-party requests” and a CSP of `default-src 'none'`
   (`docs/CLAIMS.md:154`, `app.py:742-745`). A registration page with a session
   cookie and a form becomes `default-src 'self'` and one first-party cookie.
   That is an A2 cost under every option, and it is the Product Lead's re-cut.

## 3. The three options

| | What it buys | What it costs | Who pays |
|---|---|---|---|
| **A — two credentials, one link** (recommended) | The four invariants, unchanged; the guest's first-visit flow, already built and proven, is reused as-is; the account and the machine can be reset independently | One more password to remember and one more form, reached from the ready email the user already opens; the machine's reset does not follow the account; a real copy problem (“which password?”) | **The user** (a password), **Product Lead** (copy and one criterion re-cut), **support** (the lost-recovery-code path) |
| **B — one credential, set at registration** | One password, one reset story, the account is the authority; the pre-first-visit token retires; “the instance follows the account” is true by construction | HQ sees the password once (B1) or holds a verifier (B2); HQ can set any tenant's sign-in at any time, so the machine's gate is inside the operator's power; eight approved CLAIMS sentences — seven of them unique to B — and two embedded surfaces move; a new HQ→guest write path; a migration across every machine already seeded | **The product** (the promise and `/verify`'s proof), **Builder** (seed contract, installer, A5 push path), **security** (an offline-crackable copy of the machine credential at HQ) |
| **C — an auth proxy in front of the instance** | SSO; resets trivial; one place to gate every workspace; can hide the instance's own page entirely | Odile is in the login path **and in every request**: Invariant 4 and “your prompts do not pass through Odile's servers” both die; HQ holds an assertion key that mints a session for any tenant; HQ's availability becomes every tenant's sign-in; a new service, cookie domain, TLS and CSP surface | **The product** (the access model), **Builder** (a new service and path), **Savio** (access change) |

### 3.1 A — two credentials, one link

Registration takes an email and a password. HQ stores `accounts(email UNIQUE,
password_hash)` in the installer's own scrypt format (`scrypt$n$r$p$salt$dk`,
`installer/odile_install.py:3229`) and a session; the account page shows the
tenant's state, the URL and the one action that matters (“open your machine”).
The machine is untouched: the same first-visit service, the same token in the
ready email, the same scrypt hash on the guest, the same recovery code. **The
link between the two credentials is the tenant row** (`tenants.account_id`), not
a secret — and the rule that keeps it a link is one line: *nothing in `accounts`
may open a machine.* The account is the person; the tenant row is the machine; a
rebuild replaces the machine under the same account.

What it buys is exactly the four invariants plus one thing no other option has:
the user's password is chosen **after** the machine's report is checked
(“you set your sign-in only after PASS”, `docs/CLAIMS.md:143`), so a password is
never typed into a surface the user has not verified. And the split is the right
way round for a user's memory: the password typed **daily** is the machine's,
and the one typed twice a year is the account's, which is also the one that is
cheap to reset.

What it costs is honest: a second password, a second reset flow, and a first
visit that is not just “sign in”.

### 3.2 B — one credential, set at registration

The card's option: the password chosen at registration becomes the machine's
sign-in, hashed on the guest. Two shapes, and they are not the same decision:

- **B1 — HQ hashes it.** The plaintext passes through HQ once; HQ writes a
  `sign_in_hash` into the seed; the installer writes it into the tenant `.env`
  before the first visit. Simple, and it kills “We never see it” outright.
- **B2 — the browser hashes it.** The onboarding page computes the verifier and
  posts only that; HQ never holds the password, only the same verifier the guest
  holds. It keeps “We never see it” literally true, and it costs a browser-side
  scrypt (WebCrypto has PBKDF2, not scrypt), a JS dependency on a page the house
  rule keeps script-free, and a verifier at HQ that is offline-crackable.

Both shapes share the parts that are expensive, and they are the reason this is
not merely a seed field:

1. **The seed contract opens.** `SEED_FORBIDDEN` lists `password` today, and
   `validate_seed` enforces it (`installer/odile_install.py:184-188`,
   `:10832-10857`). A verifier is not a password, so the field can be
   `sign_in_hash` — but the seed now carries the machine's gate, and
   `docs/runbooks/seed.md:52` says it carries no login credential.
2. **The guest's only writer changes.** ODL-521's whole point is “the first-visit
   page is the only writer of a usable sign-in”. B makes the installer a second
   writer, so that invariant, its test and the `/verify` line that describes it
   all move.
3. **A5 needs a new network path.** “The instance follows the account” means HQ
   must write a verifier to a machine it cannot log into. Either a signed,
   single-use reset push from HQ to the guest (new endpoint, replay protection,
   an expiry) or a boot-time pull (the guest asks HQ for its verifier). Both make
   HQ the authority: whoever holds that key can set any tenant's sign-in.
4. **The promise narrows for the life of the machine.** Under A the operator's
   only door closes at the first visit and stays closed. Under B it is open
   forever, by design, and `docs/CLAIMS.md` §1a's “the only sign-in is yours”
   cannot be published without the qualifier “unless you ask us to reset it”.
5. **The mail queue becomes a path to every tenant.** A reset email read at the
   relay completes an account reset, which — under B — sets the machine's
   sign-in. Under A the same email only opens the account page.

The one thing B buys that A cannot: a password reset that works when the machine
is down and the user has lost the recovery code. §5 prices that.

### 3.3 C — an auth proxy in front of the instance

The proxy authenticates the user at HQ and asserts the identity to the guest
(a signed header or a short-lived token). One credential, resets at HQ, and the
guest's app-layer gate becomes redundant. It is the cleanest *product* answer and
the worst *promise* answer: the machine's gate is now an HQ-issued assertion, so
an operator holding the assertion key can mint a session for any tenant, which is
the definition of the thing the product removed. If the proxy also fronts the
app, Invariant 4 dies, and with it `docs/CLAIMS.md`'s “They do not pass through
Odile's servers.” It also makes HQ availability every tenant's sign-in — a
robot that cannot reach HQ is a machine nobody can open.

Rejected. It is the right answer only if Savio decides Odile is a hosted SaaS
with an operator admin console, not an operator-blind machine.

## 4. Recommendation — A, and the sentence it touches

**Choose A.** It is the only option that keeps I1–I4 without a new mechanism, a
new network path or a new residual power. It also changes **no line of the
guest's contract**: `SEED_KEYS`, `SEED_FORBIDDEN`, the installer, the first-visit
page, the `/verify` report and every test that binds them are untouched — the
whole of A2's build is on HQ. (A5's session rotation, §5 item 3, is a separate,
bounded guest change that both options need.) That is what makes it a two-way
door: A→B is a seed field, an installer write and a push path, and it can be done
per-tenant; B→A cannot restore the claim for machines already seeded under B,
because the verifier sat at HQ and the operator could set the sign-in at will.
That asymmetry, not convenience, is the deciding argument: **A is a two-way door,
B is a one-way door on the promise**, and the promise is the product.

### 4.1 The CLAIMS sentence it touches

`docs/CLAIMS.md` §1 First run:

> “Your Odile sign-in is the username and password that open the workspace; only
> its hash is stored, on the machine. **There is no password and no account at
> HQ.**”

The clause in bold is falsified, and **A2 falsifies it, not this ADR**:
registration creates an account, and the account has a password. The re-cut is
the Product Lead's, and its honest form is one sentence, not a retreat:

> “Your Odile account holds your email and a password that opens your account
> page — never your machine. The sign-in on the machine is set there and hashed
> there; Odile never sees it.”

Everything else in the access deck stays literal, except `:156`'s inventory
list, which gains the account and its password hash in the same re-cut (the
`:156` row below). Read against the options:

| Approved sentence (`docs/CLAIMS.md`) | A | B |
|---|---|---|
| §1 “No one at Odile can log in to a tenant machine. The only sign-in is yours.” (:51) | survives **between the first visit and a rebuild** (§2 item 1, §6) | dies — HQ can set a sign-in at any time |
| §1 “Your Odile sign-in is set on your machine at first visit and hashed there. We never see it.” (:57) | survives verbatim | dies in both shapes (B1 sees it; B2 hashes it elsewhere) |
| §1 “only its hash is stored, on the machine. There is no password and no account at HQ.” (:106) | last clause moves, **forced by A2** | dies: the verifier is at HQ |
| §1 “A lost Odile sign-in is reset on the machine with your recovery code. There is no HQ reset.” (:107) | survives | dies — the reset is at HQ |
| §1 “At HQ we hold your email, the record of your machine, and a boot key with no private half. Not the disk key, not your sign-in.” (:156) | survives, with “and your account and a hash of its password” added | dies: “not your sign-in” |
| §1 “you set your sign-in only after PASS” (:143) | survives — the password is typed on a verified machine | dies — the password is chosen before the machine exists |
| §4.11 “The sign-in is yours; HQ holds no password” (:449) | survives (the account hash is named) | dies |
| §5 “Can you reset my sign-in or unlock my disk?” “No. There is no escrow…” (:475) | survives | dies |

B also costs two embedded surfaces a re-embed and a re-sign: the first-visit page
(`web/first-visit.html`, `installer/odile_install.py:4534`) and the installed
help skill (`skills/odile-help/SKILL.md` in the installer), plus the generated
copies in `release/`. A costs one deck clause and one inventory addition, the
account's rows in the privacy inventory (`tests/test_legal.py` binds them), and
the copy that explains which password is which.

### 4.2 What A does not buy

- **One password.** The user holds two. Expect most users to reuse the same one;
  that is their choice, and it means HQ sees the machine's password *in practice*
  while the mechanism still never sends it. Say so in the copy; do not pretend
  the habit away.
- **“The instance follows the account.”** It cannot, without B's write path. §5
  states what A5 must say instead.
- **A reset from the registered email for the machine's sign-in.** It resets the
  account only.

### 4.3 If Savio wants one credential

B is bounded and this ADR is the design note for it: `accounts` keeps the
verifier in the guest's own scrypt format, so switching is a seed field
(`sign_in_hash`), an installer write before the first visit, a first-visit page
that becomes a login, and A5's signed reset push. The claim re-cut in §4.1 and
Savio's acceptance of the fourth residual power (“Odile can set your machine's
sign-in”) are the price, and they must be decided **before A2 ships** — after
that, the change is a migration with two credential models in the field.

## 5. The reset constraint for A5 (ODL-572)

Under A there are two resets, and the card must say which is which on every
screen. The constraint is not “pick one”; it is **two flows with two authorities,
and the machine's flow needs the machine**.

**Name the host per environment.** A2 and A5 are judged on the bench: A1 puts
the account site at `test.odile.fyi` and a test tenant at
`<slug>.test.odile.fyi` (ODL-339/ODL-340; A1's exit criteria 1 and 2), A2's demo
registers a new account “on the bench”, and A5's demo changes the email and the
password “on the bench”. A link minted on the bench must name the bench host: a
tester who clicks `https://odile.fyi/reset?t=…` lands on the production app,
which has no such account, and nothing is reset.

**Account password (HQ authority).**

- A single-use emailed link to the account host — on the bench
  `https://test.odile.fyi/reset?t=<token>`, in production
  `https://odile.fyi/reset?t=<token>` — valid 30 minutes, the token stored as a
  SHA-256 hash, invalidated by a new request or an email change. It works with
  the instance down, which is its whole point. The link is a bearer in a URL,
  the same shape as the recovery code below; it is safe only because it is
  single-use, short-lived and stored as a hash, and A5's link must keep all
  three or it becomes defect 2.
- It ends the account's other sessions. It does **not** touch the machine.
- A signed-in user changes the email at the account: confirm at both addresses
  (`old` and `new`), then invalidate live links sent to the old one. The machine
  is untouched, because its username is the fixed `odile`
  (`installer/odile_install.py:3189`).

**Machine sign-in (the guest's authority).**

- The machine's own first-visit reset, `https://<slug>.test.odile.fyi/first-visit/reset`
  on the bench and `https://<slug>.odile.fyi/first-visit/reset` in production —
  a recovery code, a new password. The page is served by the guest, so **the
  machine must be up**. When it is down, say exactly that: the sign-in reset
  waits for the machine; nothing else the user does is lost, because they could
  not use the machine anyway.
- The code rotates on success, and the response must show the new one once, like
  the first visit does.
- If the user has lost the code: there is no operator reset (D20). The path is
  the recovery file's restore into a rebuilt machine (A3/A6). The account page
  must name that path before the user asks support.
- If the account email is dead: the account is the identity, so this is an A6
  admin action (delete and recreate), and the machine is the anchor that survives
  it — its sign-in and its recovery code are on the machine. A5 promises email
  change only with two working inboxes, and must not imply otherwise.

**Four defects in today's sign-in paths that A5 inherits and must fix.** They are
small, and they matter because A makes the machine's reset the only
post-first-visit path:

1. **The recovery code is not single-use.** `recovery_used_at` is written
   (`installer/odile_install.py:4303`, dumped at `:4371`) and never read, so the
   code re-downloads the recovery file (which holds the backup password)
   indefinitely. Enforce one use, or give the code an expiry.
2. **The code travels in a URL.** `/first-visit/recovery?c=<code>`
   (`:3556`) puts a permanent bearer in browser history and sync. Either accept
   the code as a bearer with an expiry, or post it.
3. **A sign-in change keeps every session.** `write_env_hash` keeps
   `HERMES_DASHBOARD_BASIC_AUTH_SECRET` byte for byte, deliberately
   (`:3604-3628`), so a reset does not end other machine sessions. A5's “a
   password change ends the other sessions” means rotating that secret in the
   same write.
4. **The setup token has a permanent copy.** `odile-firstboot.py:1137-1138`
   appends `--first-visit-token <64 hex>` to `/var/log/odile-firstboot.log` at
   the unit's default umask (0644: no `UMask=` is set in
   `deploy/image/odile-firstboot.service`). Fix the mode to 0600 and redact the
   argument; A5's reset link must not repeat the shape.

**The A5 criteria to re-cut**, verbatim, with the reading A requires:

- A2 “The registered email and password reach that instance, and a chat turn and
  a file write succeed.” → “The registered email opens the account page, which
  reaches that instance; the sign-in is set on arrival; a chat turn and a file
  write succeed.”
- A5 “A password change ends the other sessions, and the instance sign-in follows
  the account.” → “An account password change ends the account's other sessions.
  The machine's sign-in changes only on the machine, and a change there ends the
  machine's other sessions.”
- A5 “A forgotten password is reset from the registered email.” → “A forgotten
  account password is reset from the registered email. A forgotten machine
  sign-in is reset on the machine with the recovery code.”

If Savio picks B, these three read as written and A5 becomes one flow. That is
the trade the gate is buying. The Product Lead owns the two cards' criteria; the
edit lands on A2's gate and on ODL-572 **before** either gate is judged, not as
a post-hoc reading of a gate that already ran.

## 6. The trust boundary, written down

The account is a new store at HQ. What it does and does not do:

| Party | Can | Cannot |
|---|---|---|
| **The account service (HQ)** | Read the email, the scrypt verifier and the account's sessions; reset the **account** password; read the tenant record; create, stop, delete the machine (provider); read serial boot logs and the mail queue | Read or set the machine's sign-in; read the disk, memory, prompts or files; present anything to the guest's gate |
| **The guest's first-visit service** | Write the sign-in hash and rotate it with the recovery code | Be reached by HQ except with the token (before the first visit) or the code |
| **The operator (a person at Odile)** | Everything the HQ service can; read the setup token in the mail queue or the boot log **before the first visit** | Open a machine **after** the first visit, without a support grant |
| **Servury** | Console, ciphertext, stop, delete | Log in; read the disk |
| **NEAR AI** | Prompts inside the TEE | The machine |

Two windows exist and are stated, not hidden: the pre-first-visit token (§2
item 1), and a **rebuild**, where `odilectl/provision.py` mints a new setup
token and seals the old backup password under it, so the operator who mints it
can unseal it. The rebuild is a handover by design (the machine is replaced and
the user restores); it is not the credential this ADR decides, and a later ADR
may retire the sealed copy.

**The account record** (the interface this ADR owns):

```
accounts(id, email UNIQUE, password_hash, created_at, email_verified_at,
         invite_code_id NULL, state)
tenants.account_id → accounts.id        -- the link; no secret crosses it
```

`password_hash` is the installer's own string, `scrypt$n$r$p$salt$dk`
(`installer/odile_install.py:3229`, `:9541-9545`), so the same verifier could be
handed to a guest if B is ever adopted — one format, one implementation to audit.
The guard rail, stated once and testable: **no column in `accounts` is a value
that can be presented to a tenant's gate.** A test that asserts exactly that is
worth more than a paragraph in this ADR.

## 7. Failure modes the builder must not miss

- **One account per email, one machine per account.** `accounts.email UNIQUE`
  and `tenants.account_id`; a retried registration reuses the row; a retried
  provision replays the claim worker's existing idempotency (“a second accept
  replays idempotently”, `odilectl/claim.py`). A retry must never order a second
  machine (`docs/backlog.yaml` A2: “a failed provision leaves no billed machine”).
- **The address is verified before the machine exists.** A typo'd email is a
  billed machine no one can claim, and it is the cheapest way to burn A2's
  budget. Verify the address (`email_verified_at`) before the provision worker
  runs, and never mint a reset link for an unverified address.
- **Reset links are tokens.** Store the hash, not the link; single-use; a TTL;
  rate-limit the request; invalidate on use and on an email change. A link
  printed in a mail log must be useless after its first use.
- **The first visit is a one-shot POST.** `check_token` returns `used` after a
  success and the POST answers **410** (`installer/odile_install.py:4018`,
  `:4232`). A browser retry after a timeout is therefore *not* a failure, and the
  page must render it as “already set”, not as an error. Do not “fix” it by
  allowing a second write.
- **No password in the seed.** `SEED_FORBIDDEN` stays as it is under A; the seed
  carries no login credential (`docs/runbooks/seed.md:52`).
- **No secret in an argv.** `installer/odile_install.py:14040` already has a
  `--signin-password` flag on the classic-install path; the account flow must not
  add one, and A5's reset must not pass a password on a command line.
- **The privacy page is a test, not a document.** `tests/test_legal.py:124-137`
  fails until every `accounts.*` column is in `web/privacy.html`, and
  `tests/test_claims.py` fails until every page sentence is in the deck. The
  account rows land in the same commit as the schema, or `make check` is red.
  The schema comment at `app.py:135-137` (“No passwords … are stored”) is the
  same edit, in code: no **machine** password is stored; the account's scrypt
  verifier is.
- **A lost ready email is a token rotation, not a resend.** HQ keeps only the
  token's hash, so the first-visit link cannot be shown again; the account page's
  “send it again” mints a new token with the existing rotation
  (`odilectl/rotate_token.py` → `seed/seed.py token rotate`) and ends the old
  one. Re-showing a stored plaintext would put the machine's gate back at HQ.
- **Registration is a public, unauthenticated POST that costs money.** Rate
  limit it, and note that A7's invitation gate is what stops it being an open
  machine factory (`docs/backlog.yaml` A7).

## 8. What would change the answer

- **Savio wants one credential**, or wants “the instance follows the account” as
  written: adopt B (§4.3), and accept the fourth residual power and the claim
  re-cut first.
- **A6's console must “log in as” a tenant**: that is C, and it breaks I1 and I3.
  A6 should only delete and inspect the record; if it needs to see a workspace,
  it uses a support grant like anyone else.
- **A passkey later.** R3's “a passkey is a later upgrade” and D26's “password
  now, passkey later” both name the **machine's** Odile sign-in, so under A a
  passkey is a guest change to the first-visit credential and leaves the
  account's password alone. Reading it as the account credential is a re-cut of
  R3/D26, not their text; and R3's “no HQ account or password” and D26's “only
  credential” are changed by A2's account under every option, so the MVP re-cut
  (`docs/MVP-V0.1.md`, ODL-576) must name both rows.

## 9. Alternatives rejected

- **B, now.** Rejected because it makes HQ the authority for a tenant's sign-in
  for the life of the machine, and because eight approved public sentences (seven
  of them unique to B), two embedded surfaces and `/verify`'s proof move with it.
  Adoptable later; §4.3 is its design note.
- **C, at all.** Rejected because an operator-held assertion key is a credential
  that opens every machine, and because a proxy in the request path ends
  Invariant 4 and the prompts sentence together. It is a different product.
- **A plus an emailed reset for the machine.** Rejected as an oxymoron: an emailed
  link that resets the machine's sign-in must be redeemable by HQ's code, which
  is B. The email resets the account; the machine resets on the machine.

## 10. Migration and rollback

alpha.0 has no accounts, so A2 creates the table and there is nothing to
migrate. The rollback for A is to stop using the account (the tenant row and the
machine are unchanged); the rollback for B does not exist for machines already
seeded, which is why B must be decided before A2 ships.

## Annex — the load-bearing lines, reproducible

At the card's base, `main` `d3c372b` (17 November 2026), in a clean checkout:

```sh
$ grep -n 'SEED_FORBIDDEN = ' installer/odile_install.py
188:SEED_FORBIDDEN = ("password", "passphrase", "private_key", "ssh_private_key", "luks")

$ grep -n 'first-visit-token' deploy/image/odile-firstboot.py
54:       install-stack.sh --seed /etc/odile/seed.json --first-visit-token <token>
1133:    command = [installer, "--seed", seed_path, "--first-visit-token", token]

$ grep -n 'open(log_path' deploy/image/odile-firstboot.py
1033:    with open(log_path, "a", encoding="utf-8") as log:
1137:    with open(log_path, "ab") as log:
# 54 is the module docstring; 1033 is the recovery-file writer's log (no token);
# 1137 appends the 1133 argv, token included, to log_path.

$ grep -n 'recovery_used_at' installer/odile_install.py
3303:        "recovery_used_at": None,
3316:    for key in ("used_at", "signin_set_at", "recovery_issued_at", "recovery_used_at"):
4303:                state["recovery_used_at"] = state["recovery_issued_at"]
4371:        "recovery_used_at": state["recovery_used_at"],
11414:        "recovery_used_at": None,
# 11414 is the fresh state dict; no line reads the field as a condition —
# the code is written, never enforced.

$ sed -n '3604,3611p' installer/odile_install.py
def write_env_hash(env_path: Path, password_hash: str) -> str:
    """Write the hash into the tenant .env; return the username it names.

    Only the hash line moves: the session-signing secret the first boot
    generated for this machine (``HERMES_DASHBOARD_BASIC_AUTH_SECRET``) is kept
    byte for byte, or every session cookie the owner had would be invalidated
    by setting a password. A file without a secret is refused rather than
    written: the tenant would come back with no way to sign a cookie.

$ grep -n 'first-visit?t=' odilectl/claim.py
1185:        first_visit_url = f"{url}/first-visit?t={token}"
```

The CLAIMS clause this ADR falsifies has no test and no lint phrase binding it:

$ git grep -n 'no account at HQ' d3c372b
d3c372b:docs/CLAIMS.md:106:- "Your Odile sign-in is the username and password that open the workspace; only its hash is stored, on the machine. There is no password and no account at HQ."

The tracked tree at the base has exactly one hit, and nothing under `tests/` or
`web/`. The two other places that discuss the idea are HQ-only files no checkout
carries — `docs/product/RECUT-2026-11-07-model-a.md`'s “Odile account at HQ”
row (the idea, not the phrase) and the untracked 2026-11-08 evidence walkthrough
that quotes it.
