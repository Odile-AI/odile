# DECISION — the first-visit state: `signin_set_at` is a fact about the `.env`, not about the page

**Status:** decision record · 22 November 2026 · Security seat · card `t_10bd80a6`
(from `t_1e7bb06b`, the runbook card that measured the trap). Round 2 names the
fixture's key set and the deploy's own four-key `.env` (§2, §4, §6–§8).
**Changes:** none to an approved sentence. This is a mechanism decision: one
demo-host program, its tests, and the runbook paragraph that describes the trap.
`docs/CLAIMS.md` is untouched; the claims and brand lints stay green.
**Implements:** a builder card follows (created by this card). The installer is
not edited here; its docstring's "the first-visit page has written" wording is
re-cut in ODL-925's release cut, which already re-signs the installer.

---

## 1. Decision

**Option 1, narrowed: the writer of a usable sign-in records the visit, in the
state the first-visit service owns.**

`deploy/demo-host/apply_demo_public.py` stamps `/etc/odile/first-visit.json`
when it installs the judge sign-in. The file stays the first-visit service's:
`installer/first_visit.py` owns its schema, its version, the setup-token hash
and every recovery field. What changes is the meaning of one key:

- `signin_set_at` does **not** mean "the page wrote this". It means **"the
  `.env` holds a usable sign-in the owner can use"** — the page's POST wrote it,
  the demo deploy wrote it, or (from ODL-925) the seeded first boot wrote the
  seed's verifier.
- The deploy stamps `used_at` and `signin_set_at` together, one UTC timestamp,
  and writes **no other key**. It writes no `recovery_hash`: a recovery code the
  deploy never shows would be a credential nobody holds, and the demo's recovery
  is the deploy itself.
- It writes the `.env` first and the state second, so the state never says
  "owner set" while the `.env` still holds a placeholder. If the state is
  missing or malformed it prints a `warn` row and does not invent the file (the
  schema needs the 64-hex token hash the deploy does not own; the installer's
  `ensure_first_visit` exits on a state without one, so a deploy-invented file
  would break the next boot).

This is not a new writer class: `installer/odile_install.py` already writes the
state once when it is absent (`ensure_first_visit`, `installer/odile_install.py`
:13337-13356), and ODL-925's seeded first boot is already decided to stamp it.
The page's "**One writer**" note (`installer/first_visit.py:79-81`) is about two
concurrent POSTs, not about a single-writer invariant. The rule that file needs
is narrower and testable: **every writer updates under the lock, and no writer
creates the file unless it owns the token hash.**

## 2. What was measured

The defect, on fixtures built from this tree (the probes are attached to the
card as `probe_marker.py` / `probe_marker.out` for A–D and
`probe_marker_round2.py` / `probe_marker_round2.out` for B′):

| # | Fixture | Result |
|---|---|---|
| A | `.env` with **exactly the three allowlisted keys** — a scrypt hash of a known password, a session secret — and a state with a 64-hex token hash, `used_at` and `signin_set_at` **null** | `ensure_env(first_visit=True)` prints `rewrite … (the sign-in hash is a placeholder until first visit)`; the stored hash is replaced; the known password **no longer verifies**; the state is unchanged |
| B | the same `.env` (three allowlisted keys) and state, with `signin_set_at` set | `changed=False` (`same … 3 allowlisted keys`); the hash is kept byte for byte |
| B′ | the deploy's own `.env` — B plus the fourth key `HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY`, which `deploy/demo-host/apply_demo_public.py:1071-1076` writes — and the same stamped state | `changed=True` (`rewrite … outside the allowlist: 1; seat secrets: 0`); the hash is **still** kept byte for byte and the published password verifies; the fourth key is gone (the installer writes its three-key template) |
| C | a marked image `.env` (`# odile-signin-placeholder: 1`), then the page's own `write_env_hash` | the marker **survives** the write: `render_env` replaces the hash line in place and preserves comments (`installer/first_visit.py:643-670`) |
| D | state with `signin_set_at` set, `used_at` null | `check_token` answers `ok` — the setup-token page is still open (`installer/first_visit.py:1118-1124`) |

Measured on the live demo machine, 2026-11-22:

- `GET https://demo.odile.fyi/attestation` → `cmdline` carries a 64-hex
  `odile.token=` (value elided here; the Caddy site template's comment calls `/attestation`
  "public", `CADDY_SITE_TEMPLATE`). Its SHA-256 is exactly the
  state's `token_sha256` (`20fd4bf9…`), so the link the 410 below closes is the
  published token's.
- On the machine's own name, `GET /first-visit?t=<that token>` → **410** (the
  token was spent by the 2026-11-21T16:57:50Z recovery POST — the state's
  `used_at` and `signin_set_at`), an unknown 64-hex token → **404**, no token →
  **404**; on `demo.odile.fyi` `/first-visit` → **302** into the Webapp's Host
  gate. A 200 form is the open state.
- The state itself (`sudo cat /etc/odile/first-visit.json`, over the tenant
  console, 2026-11-22): `used_at` and `signin_set_at` are both
  `2026-11-21T16:57:50Z`, with `recovery_hash`/`recovery_issued_at` from the
  same second and the `download_*` fields still the page's (a 17:07:50Z link,
  unused). That is the page's recovery POST, not the deploy's: the deploy's own
  hash write (before that POST) left no trace in this file — the defect this
  card closes.
- `/etc/odile/key.env` is present on the demo: 0600 `root:root`, 76 bytes, one
  35-byte `HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY`; the `.env` carries the
  same 35-byte key, so today both the authoritative `.env` copy and the
  deploy's fallback exist.

The card's second measurement — the 19:59Z forced run kept the hash when the
state carried `signin_set_at` — is fixture B's **decision** (the installer reads
the state, not the file) measured on the machine, but not B's `same` path: after
the 2026-11-21 recovery the deploy had put the fourth key back, so that run took
B′'s rewrite branch. What the report's sign-in-hash row measured is the hash
column — PASS before and after — not `changed=False`; the same run also took the
Near AI key out of the `.env` (§4, §8).

## 3. Why not the other two options

**Option 2, the `.env` placeholder marker, is fail-open twice over.**

1. The marker is an image-build artefact (`deploy/image/install-stack.sh:368,377`,
   ODL-521). An image built **before** ODL-521 — the live tenant still boots
   `2026.11.11-8`, an 11 November image (`docs/runbooks/install.md` §12.6), and
   ODL-521 cut the marker on 14 November (`cf05e31`) — ships a usable bench hash
   and no marker. "Keep any hash that lacks the marker" keeps it, which is the
   exact hole ODL-521 closed.
2. On an image that **does** carry the marker, the page's in-place write keeps
   the marker (fixture C). So the owner's own hash would be replaced on the next
   boot unless every writer also strips the marker — i.e. the marker contract
   needs the same shared-writer discipline as the state, but defaults to
   **keep** when absent. Absence must mean "no owner yet", never "owner": that is
   fail-closed, and the marker cannot express it without a second signal. The
   state already is that signal.

**Option 3, keep today's behaviour and let the runbook be the contract,** keeps a
documented footgun on the documented retry path. `--force` is how a failed first
run is retried (`docs/runbooks/demo-public.md` §6), and on a machine whose hash
the deploy wrote it silently shuts the gate and locks the HQ-driven deploy out,
because that deploy opens its console with the same judge account (measured
2026-11-21). A runbook paragraph that says "the recovery is three steps" is not
idempotence. The installer's own docstring already promises the stronger rule
("After it, the hash is the owner's and a boot must keep it",
`first_visit_signin_set`'s docstring); option 3 makes that promise false for
one legitimate writer.

**Why not read `used_at` in `first_visit_signin_set()`:** the demo state had
`used_at` null too, so it would not have saved the machine. The two keys answer
different questions: `used_at` is the setup token's state (the page's link is
spent); `signin_set_at` is the `.env`'s state (a usable sign-in is in it). The
installer keeps reading `signin_set_at` alone.

## 4. The rule that must keep holding

**ODL-521, unchanged and fenced:** under `--first-visit`, any `.env` hash whose
state has no `signin_set_at` is a placeholder and is replaced on every run —
**including a hash that verifies a known password**. Absent or malformed state is
fail-closed: replace, never keep. The fence is
`tests/test_first_visit.py::FirstVisitEnvTests` — five tests: the image bench
hash is replaced, every boot before the visit rewrites the placeholder, the
session secret is kept, the owner's hash survives the next boot, and a dry run
changes nothing — and the live evidence is the image's own `.env`
(`docs/runbooks/image.md` §13).

**What a forced run does to a deploy-written `.env`.** The installer's file is
exactly the three allowlisted keys (`TENANT_ENV_KEYS`,
template `TENANT_ENV_TEMPLATE`). The deploy's
`.env` carries a fourth key, `HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY`
(`apply_demo_public.py:1071-1076`), so on the demo (and any machine whose
`.env` the deploy wrote) **every** forced run takes the rewrite branch: the
hash is kept once the state is stamped (B/B′), but the fourth key leaves the
file. The next deploy re-adds it only from `/etc/odile/key.env`
(`inference_key()`, `apply_demo_public.py:1027-1045`); when that store is
missing, `inference_key()` raises `error: … has no
HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY and /etc/odile/key.env is missing`
(measured: `SystemExit` code 1) from `ensure_env`, and the deploy stops at its
tenant **dry run** with `error: the tenant dry run failed` before the apply's
write path starts. On the demo the store is present (§2), so the deploy is still
the recovery — the recovery depends on the store, not on the `.env`.

**The writer rules this decision adds:**

1. A writer stamps `signin_set_at` only after it has established the `.env`
   holds a usable sign-in (it verified the stored hash, or it just wrote one).
2. It holds the page's lock (`installer/first_visit.py:389-399`,
   `<state>.lock`), preserves every other key byte for byte, and writes
   atomically at 0600 root.
3. It never moves a timestamp a visit already recorded: if `signin_set_at` is
   set, leave `used_at`/`signin_set_at` alone (idempotent).
4. Whenever a sign-in is installed outside the page, `used_at` is stamped with
   it: the setup-token link must not remain an alternate reset path on a machine
   whose sign-in is already set.
5. It never creates the state file and never writes `recovery_*`.

The demo deploy's shape: load the state, validate it (version, token hash),
`state["used_at"] = state["used_at"] or now`, `state["signin_set_at"] =
state["signin_set_at"] or state["used_at"]`, save. A dry run prints the would-be
write and changes nothing. The report gains one row (`first-visit state:
the deploy-set sign-in is recorded at …`), so the live check can see it.

## 5. The adjacent gap this closes on the demo — and does not close for a tenant

`/attestation` is public by design and publishes the measured command line, and
the command line carries `odile.token=`. The setup-token page accepts exactly
that value until `used_at` is set (`check_token`, `installer/first_visit.py`
:1118-1124), and the page has no expiry — the "24-hour" lifetime is the seed
ledger's, not the page's. So on a classic machine whose sign-in was installed
outside the page, the setup token is a **public reset credential**: anyone who
reads `/attestation` and knows the machine's name can replace the sign-in. On the
demo, stamping `used_at` closes it.

For a tenant, the A-era first visit is the same page, and the ADR names the
setup token as an *operator* window before the first visit
(`docs/architecture/ADR-account-credential.md` §2 item 1 and §6);
`/attestation` widens "operator" to "anyone with the URL", and the URL is in DNS
and, with ACME issuance, in Certificate Transparency. **B-once (ODL-925) removes
the `?t=` page and makes the first visit a login**, which closes the window for
new tenants; until ODL-925 lands, a freshly booted classic tenant is
front-runnable between boot and the owner's first visit. This card does not change
that: it names it, and asks ODL-925's builder to fence it (the tokenless page
must answer 410 to `?t=`, and a fresh machine's page must not accept a token).
Closing it sooner than A2 is Savio's call.

## 6. Test shape

`tests/test_demo_public.py`, a new `FirstVisitStateTests` that loads
`apply_demo_public.py` and `installer/odile_install.py` (the module's own
fixture style), with a temp `.env` + state:

1. `test_a_deploy_set_signin_survives_the_first_visit_run` — the fixture `.env`
   carries the deploy's own shape: **four keys**, the three allowlisted plus
   `HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY` (`ensure_env`,
   `apply_demo_public.py:1071-1076`; point `apply.ENV_FILE`/`apply.KEY_STORE`
   at the temp files, as `TenantProgramTests` points its paths); the state is
   stamped by the deploy. Run `installer.ensure_env(first_visit=True)`:
   `changed` is **True** (`rewrite … outside the allowlist: 1` — the fourth key
   is outside the installer's three-key template), the hash is
   **byte-identical**, the published password still verifies, and the Near AI key
   is no longer in the `.env`. On a copy cut down to the three allowlisted keys
   the same run is `changed=False`; assert both, so the test says which shape
   produces which `changed`. **This is the defect; the hash assertion fails
   before the fix.**
2. `test_without_the_stamp_the_image_hash_still_dies` — fixture A without the
   deploy: the installer replaces the hash and the image password stops
   verifying. **The ODL-521 fence; it must stay green.**
3. `test_the_stamp_is_idempotent_and_preserves_the_page_state` — run it twice:
   one timestamp, `token_sha256`/`created_at`/`recovery_*`/`download_*` byte for
   byte, mode 0600, the second run says `same`.
4. `test_the_stamp_does_not_move_a_visit_the_page_recorded` — state with the
   page's `used_at`/`signin_set_at`: both unchanged.
5. `test_a_missing_state_is_reported_not_invented` — no state: the `.env` is
   written, a `warn` row names the missing state, no file is created, and the
   installer then replaces the hash (fail-closed).
6. `test_the_stamp_spends_the_setup_token` — after the stamp,
   `first_visit.check_token(state, token) == "used"`; the page's 410 is the
   same verdict.

The builder's live checks on the demo: the forced run's report row for the
sign-in hash reads PASS before and after; `GET /first-visit?t=<token>` answers
410 after a deploy-set sign-in; the forced run's `.env` line reads
`rewrite … (outside the allowlist: 1)` and the file then has three keys, so
`check_demo_public.py`'s "the .env carries the one Near AI key" row (`:649`) is
red until the deploy re-adds it from `/etc/odile/key.env`; the deploy's dry run
prints the first-visit state's `same`/`would write` row and the apply writes it;
`check_demo_public.py --password-file …` is 73/0; the judge sign-in over
loopback is 200.

## 7. Files the builder card touches

- `deploy/demo-host/apply_demo_public.py` — `ensure_env` plus one
  `stamp_first_visit_state()` helper (lock, validate, two keys, atomic write,
  `say` row); docstring step 4 says what it now records.
- `tests/test_demo_public.py` — the class in §6.
- `docs/runbooks/demo-public.md` §6 — re-cut the "forced run shuts the sign-in"
  paragraph: the trap is the **pre-fix** record (measured 2026-11-21); after
  this card a forced run keeps the deploy-set sign-in hash, and the page
  recovery steps remain only for a machine whose state is missing. It must also
  say that the run still rewrites the `.env` to the installer's three keys
  (`rewrite … outside the allowlist: 1`): the Near AI key leaves the file, and
  the deploy re-adds it from `/etc/odile/key.env` (present on the demo,
  measured 2026-11-22; the deploy stops at its tenant dry run when the store is
  missing), so §6's "the deploy is the recovery either way" depends on that
  store. Keep the `webapp.public_url` paragraph unchanged — this card does not
  touch it.
- Not `installer/odile_install.py` in this cut: the docstring's "the page has
  written" becomes "a writer of the owner's sign-in has recorded it" in ODL-925's
  re-sign, where the installer is re-embedded anyway.
- Not `docs/CLAIMS.md`: no approved sentence moves.

## 8. Residuals, named

- **A deploy-set sign-in has no page reset path once stamped.** The page's
  `/first-visit/reset` needs a `recovery_hash` the deploy does not write, and the
  token is spent. That is intended: the deploy is the demo's recovery, and the
  page reopens only when the state says no visit was recorded (the state-loss
  case). A machine whose state is stamped but whose `.env` is then hand-broken
  needs the serial console to delete the state or the deploy. The demo is
  Odile's own machine, and the serial console is its documented last resort
  (`deploy/demo-host/apply_demo_public.py` docstring).
- **The tenant token window** of §5 remains until ODL-925; it is a schedule
  decision (A2), not this card's to accept.
- **A forced run takes the Near AI key out of a deploy-written `.env`.** The
  installer rewrites the file to its three-key template, so the fourth key
  leaves; the next deploy re-adds it from `/etc/odile/key.env` (measured
  present on the demo, §2) and stops at its tenant dry run when that store is
  missing. This is true before and after this card; the stamp changes only
  whether the hash survives.
- The state file is a 0600 root file on the guest; the deploy, the page and the
  installer all run as root there, so the lock is against lost updates, not
  against a local privilege boundary. No non-root writer exists.
