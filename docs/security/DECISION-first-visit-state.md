# DECISION — the first-visit state: `signin_set_at` is a fact about the `.env`, not about the page

**Status:** decision record · 22 November 2026 · Security seat · card `t_10bd80a6`
(from `t_1e7bb06b`, the runbook card that measured the trap).
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

The defect, on fixtures built from this tree (the probe is attached to the card
as `probe_marker.py` / `probe_marker.out`):

| # | Fixture | Result |
|---|---|---|
| A | `.env` with a scrypt hash of a known password, a session secret; state with a 64-hex token hash, `used_at` and `signin_set_at` **null** | `ensure_env(first_visit=True)` prints `rewrite … (the sign-in hash is a placeholder until first visit)`; the stored hash is replaced; the known password **no longer verifies**; the state is unchanged |
| B | the same `.env` and state, with `signin_set_at` set | `changed=False`; the hash is kept byte for byte |
| C | a marked image `.env` (`# odile-signin-placeholder: 1`), then the page's own `write_env_hash` | the marker **survives** the write: `render_env` replaces the hash line in place and preserves comments (`installer/first_visit.py:643-670`) |
| D | state with `signin_set_at` set, `used_at` null | `check_token` answers `ok` — the setup-token page is still open (`installer/first_visit.py:1118-1124`) |

Measured on the live demo machine, 2026-11-22:

- `GET https://demo.odile.fyi/attestation` → `cmdline` carries a 64-hex
  `odile.token=` (value elided here; the Caddy site template calls `/attestation`
  "public", `installer/odile_install.py:10962`).
- On the machine's own name, `GET /first-visit?t=<that token>` → **410** (the
  token was spent by the 2026-11-22 recovery POST), an unknown 64-hex token →
  **404**, no token → **404**; on `demo.odile.fyi` `/first-visit` → **302**
  into the Webapp's Host gate. A 200 form is the open state.

The card's second measurement — the 19:59Z forced run kept the hash when the
state carried `signin_set_at` — is the same mechanism as fixture B, measured on
the machine.

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
`installer/odile_install.py:13834-13838`); option 3 makes that promise false for
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
`tests/test_first_visit.py::FirstVisitEnvTests.test_the_images_bench_hash_is_replaced_before_the_visit`
and its two siblings (every-run rewrite, session secret kept), and the live
evidence is the image's own `.env` (`docs/runbooks/image.md` §13).

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

1. `test_a_deploy_set_signin_survives_the_first_visit_run` — fixture A, run the
   deploy's stamp, then `installer.ensure_env(first_visit=True)`: `changed` is
   False, the hash is byte-identical, the published password still verifies.
   **This is the defect; it fails before the fix.**
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
410 after a deploy-set sign-in; `check_demo_public.py --password-file …` is
73/0; the judge sign-in over loopback is 200.

## 7. Files the builder card touches

- `deploy/demo-host/apply_demo_public.py` — `ensure_env` plus one
  `stamp_first_visit_state()` helper (lock, validate, two keys, atomic write,
  `say` row); docstring step 4 says what it now records.
- `tests/test_demo_public.py` — the class in §6.
- `docs/runbooks/demo-public.md` §6 — re-cut the "forced run shuts the sign-in"
  paragraph: the trap is the **pre-fix** record (measured 2026-11-21); after
  this card a forced run keeps the deploy-set sign-in, and the page recovery
  steps remain only for a machine whose state is missing. Keep the
  `webapp.public_url` paragraph unchanged — this card does not touch it.
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
- The state file is a 0600 root file on the guest; the deploy, the page and the
  installer all run as root there, so the lock is against lost updates, not
  against a local privilege boundary. No non-root writer exists.
