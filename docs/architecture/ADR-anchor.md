# ADR-002 — Provenance: the pin in the public repository, no chain

**Status:** Accepted · 9 November 2026 · Architect
**Answers:** ODL-420 — "is the NEAR testnet anchor part of the product's
provenance story, and should a chain, a transparency log or mainnet take its
place?"
**Short answer:** no. The provenance story is the **signed pin in the public
repository** — "signed" in the sense §5 defines and no other. The testnet anchor
is **removed** by ODL-421, not demoted, and nothing replaces it. No chain, no
transparency log, no mainnet until after V1.0 — and only if a customer, an
auditor or the community asks for provenance that does not trust our GitHub.
**Related:** `docs/MVP-V0.1.md` §2, §6.1, §7 and §12 (D14–D17, D25);
`docs/CLAIMS.md` §3 gap 1 and §4.4–§4.6; `docs/architecture/PIN-POLICY.md`
§3.1, §5, §8; `docs/architecture/ADR-image-vs-installer.md` §4 and §14.2;
`docs/runbooks/verify.md`; cards ODL-323, ODL-420, ODL-421.
**Decision owner:** Architect. This ADR removes a mechanism and adopts none. A
chain, a transparency log or a new provider added later is a new trust boundary
and goes to Savio (`docs/AGENT-ORG.md` §3). It does not strengthen a public
claim: no approved sentence in `docs/CLAIMS.md` names the anchor, so the deck
does not move.

---

## 1. The invariant at risk

**The claim.** A tenant checks the boot, not a boot we say we checked. The user
runs the verifier on their own laptop, against a pin they fetched themselves,
before typing the passphrase (PIN-POLICY §1, P1–P4).

**The risk.** Provenance — *where did the expected number come from, and when?*
The pin is a file we author. If we can write it after the fact, we can make a
boot that failed look like one that passed. What this ADR settles is what,
outside our own repository, stops that. The answer is: nothing, deliberately.

## 2. What the anchor actually was

ODL-323 wrote one record to a NEAR **testnet** contract on 7 November 2026. Read
as code, not as a marketing line, it was this:

| Piece | What it was |
|---|---|
| Contract | `anchor.wasm`, 288 bytes, account `odile-measurement-1.testnet`, storage key `anchor.v1` |
| Methods | `set` (store the caller's raw input bytes) and `get` (return them). **No owner, no access check, no upgrade path, no payable method.** |
| Payload | 371-byte canonical JSON: `measurement`, `chip_id` (report bytes `[0x1A0,0x1E0)`), the report's own `fetched_at`, `/etc/machine-id`, a schema tag. sha256 `2b046e88…c6378`. |
| Reader's path | `/verify` linked `explorer.testnet.near.org/transactions/97BPLSG3…Vvs`; `anchor.py verify` reads `https://rpc.testnet.near.org` (default, overridable). |
| Coverage | **One machine** — the demo host, whose key Odile holds. No tenant machine was ever anchored. |

Two consequences follow from that table and neither was in the card that built
it:

1. **The contract state was not append-only.** `set` is permissionless, so any
   testnet account could overwrite `anchor.v1` at any time. The append-only
   object was NEAR's **transaction history**, not the value `get` returns. The
   `/verify` page printed a tuple fixed at build time, so the page could keep
   showing what Odile wrote while `get` returned whatever was written last.
   `anchor.py verify` re-read the chain and would have reported the divergence
   (`:502–514`), but nothing ran it on a schedule and nothing could prevent the
   overwrite: the detection existed, the enforcement did not. That is the failure
   the builder did not test.
2. **It was adjacent to the verifier, never part of it.** `odile-verify` has
   never read the anchor: it loads the pin from disk and fetches exactly one
   URL, the machine under test (`fetch()` at `odile-verify:96`, no redirects).
   So removing the anchor changes no verifier behaviour at all — which is why
   the removal is cheap and why keeping it bought nothing on the verify path
   except a second service a sceptic could trip over.

## 3. The one property a chain buys that a repository does not

Stated precisely, because it is the whole argument:

> **An append-only, third-party timestamp: proof that these exact bytes existed
> at or before a moment, checkable without trusting the repository that holds
> them.**

That is it. A chain does **not** prove the measurement is the right one, that
the pinned release is honest, or that the author meant well. It fixes *when* a
tuple existed, in front of witnesses (NEAR's validator set), and nothing else.
The measurement's correctness comes from the derivation — published artifacts
plus the machine's own command line (PIN-POLICY §2, §5.1) — and that is a local
computation the user repeats themselves.

## 4. Decision

**The pin is the provenance story; the repository is its trust root.**

- `odile-verify.pin` is committed to the public repository. The user fetches it
  at a commit they choose, or receives it beside the verifier, and compares it
  with the derivation. That is the check.
- **No chain, no transparency log, no mainnet until after V1.0.**
- **ODL-421 removes the anchor**: the contract and its CLI and wasm, the public
  record, `tests/test_anchor.py`, `docs/runbooks/near-anchor.md`, the `/verify`
  section, the `tests/test_flow.py` assertion that the explorer link is present,
  and — last, once the page no longer links the account — the disposable testnet
  key at `/root/.odile/near-anchor-testnet.pem`, which is outside git and worth
  nothing. It is removed, not demoted: a disabled anchor is a config flag, a key
  and a runbook that outlive their reason.
- ODL-421 also corrects the two places where `docs/runbooks/verify.md` still
  presents the anchor as the external record (§5 of that runbook and the
  residual-risk row). Those are traces of this decision, not of the code.

## 5. The sentence this ADR corrects — "signed"

The cards and Savio's note say "the **signed** pin in the public repository".
That phrase is the right shorthand and it must not be read as something we have
not built. Precisely:

- **What is true.** The pin is a file committed to the public repository, so the
  bytes a reader checks are the bytes at a commit, and every change to it is a
  dated, diffable line of public history. That is a real trust root: it is what
  makes a silent pin swap a public event rather than an invisible one. Odile's
  Ed25519 key (`installer/odile-signing-key.pub`) signs the app-layer manifest
  and release index — the installer, the meter and the skills — and covers
  neither the boot pin nor the release recipe inside it.
- **What is not true.** No signature covers `odile-verify.pin` itself — there is
  no `odile-verify.pin.sig`, and `odile-verify` verifies none. Nor does anything
  timestamp it.

**We do not add a signature over the pin at V0.1**, and the reason is the
property in §3:

1. **A detached signature is not a timestamp.** It proves *who* wrote a file,
   not *when* — and "when" is the one thing the anchor bought. Without a
   timestamped, append-only log, a signature over the pin is strictly less than
   the property §7 gives up, at the cost of a per-pin step.
2. **It separates GitHub from Odile, not Odile from herself.** The signature
   would be made by the same operator who pushes the pin, verified with a key
   that also lives in the repository; a reader who trusts neither gets nothing
   new. A reader who already has the key fingerprint from the recovery kit
   (ODL-400) can check it — and that reader can equally check the pin's bytes
   against their own copy.
3. **It adds a silent failure mode.** A signature that nothing verifies goes
   stale on the first `--print-pin` refresh: the pin moves, the `.sig` does not,
   and no test fails because verification would be new code. That is a pinned
   lie with a green gate, which is worse than an unsigned file.

If the trigger in §8 fires, the answer is a transparency log, not a bare
signature — a Merkle log with a signed tree head gives author *and* time in one
object, and can be checked offline against a tree head the user already holds.

## 6. The rule this decision protects: the verifier fetches one URL

**`odile-verify` must never depend on a network fetch beyond the machine it
checks.** Concretely, at the head this ADR was written against:

- one `urllib` fetch per run, to the target's `/attestation`, with redirects
  refused (`_OneURL`) and a 1 MiB cap;
- the pin is a **local** file (`--pin`, default `odile-verify.pin`) — never
  fetched;
- the AMD chain fixtures are local files; `openssl` is a local binary;
- standard library plus `openssl`, nothing else.

A PASS is a function of *the signed bytes the machine served* and *the bytes on
the reader's disk*. It is never a function of a third service being up, of our
account having funds, or of a log answering. The anchor put a second service in
the reader's path — an explorer link and an RPC node — so a testnet outage or a
drained account could fail a check of a perfectly good machine for reasons that
had nothing to do with its bytes. That is a reliability defect wearing a
provenance costume, and it is the reason this rule is written down here rather
than left implicit in PIN-POLICY §3.

## 7. What we give up, and why it is acceptable now

**Given up:** a reader can no longer prove that a pin *existed before a
particular boot* without trusting our GitHub. If we wanted to back-date a pin to
cover a bad boot, the only obstacle is the visibility of public history and the
copy of the bytes the reader kept.

Accepted, for four reasons:

1. **The derivation does the work, not the pin's age.** A back-dated pin cannot
   make a wrong boot pass: the user recomputes the measurement from Servury's
   published artifacts and the command line the machine itself reports. A wrong
   image fails arithmetic, not provenance.
2. **Back-dating requires rewriting public history.** A reader who pinned the
   bytes at a commit holds them; a force-push is visible against that copy.
   "Trust GitHub" is a real trust assumption, stated as one, not a hidden one.
3. **The anchor never covered the product.** One machine, the demo, whose key we
   hold. The number is per machine (PIN-POLICY §2), so covering ten tenants needs
   one of three things, each worse than the thing it replaces: a funded
   publishing key on HQ writing a record per boot (so the record is still ours —
   "the proof does not live on our server" becomes half true); a funded key on
   every tenant machine (a standing credential); or a permissionless `set`, which
   is what the demo had and which authenticates nothing.
4. **It costs less than it looks.** The anchor's price at ten hand-sold tenants:
   a funded account, a publishing key on HQ, an RPC in the reader's path, a
   release step, a rotation story, a contract to review, a test file, a runbook
   and a page section — for a property no tenant asked for.

## 8. The trigger that reopens this

Reopen when **any** of these is true, and not before:

- a **customer or auditor asks, in writing**, for provenance that does not trust
  our GitHub;
- **after V1.0**, the community asks for it — an issue or thread naming the
  property, not a wish for more ceremony;
- Odile must prove a pin's **age** to a third party — a dispute, a refund or an
  incident postmortem — where git history is not accepted as evidence.

The test to apply: *can a reader who trusts neither GitHub nor odile.fyi
determine that this pin existed before that boot?* If the answer has to become
yes, reopen this ADR. The first answer to reach for is a transparency log; a
chain is the most expensive way to buy the same property, and mainnet adds a
rotation and funding story for no extra guarantee.

## 9. The simpler default

When in doubt about provenance: **do nothing**. The default is the pin file in
the public repository, read from disk, checked by derivation. A new mechanism has
to beat that, not merely sound stronger. This ADR exists because the last
mechanism did not.

## 10. Reversibility

**Two-way, and cheaply.** No tenant state, no code path, no credential on any
machine, no published claim. Re-creating an anchor is an account, a deploy and a
`set` — a day, and the wasm is committed in history. It stops being cheap at the
first moment a customer contract, an auditor's report or a `/verify` page cites a
chain record as evidence: then removing it is a change to an external statement,
not to a demo.

## 11. Evidence

- The contract, read as code: `contracts/measurement/anchor.wat` (no owner, no
  access check) and `contracts/measurement/anchor.py:37` (`rpc.testnet.near.org`
  default), `:502–514` (`verify` re-reads the chain over RPC).
- The tuple and transactions: `contracts/measurement/anchor.json`,
  `docs/runbooks/near-anchor.md` §"the tuple is exactly the four values". Both
  hashes in §2 were recomputed at this head from the committed bytes:
  `json.dumps(payload, sort_keys=True, separators=(",", ":"))` → 371 bytes,
  sha256 `2b046e88…c6378`; `anchor.wasm` → 288 bytes, sha256
  `178f6bf9…76fd`. Each equals the `payload_sha256` and `wasm_sha256` the
  record carries.
- The verifier's one-URL property, at this head: `odile-verify:89` `_OneURL`
  (redirects refused), `:96` `fetch()`, `:382` `--pin` defaulting to a local
  path; `env -i PATH=/usr/bin:/bin ./odile-verify https://odile.fyi` exits 0
  (recorded in `docs/runbooks/verify.md`).
- The two runbook sentences ODL-421 corrects: `docs/runbooks/verify.md:118–121`
  and the residual-risk row `:439`.

## 12. What this ADR does not do

- **No code.** ODL-421 removes; nothing here adds.
- **No `docs/CLAIMS.md` change.** No approved sentence names the anchor. The
  `/verify` section ODL-421 deletes was page copy, not a deck row.
- **No `docs/MVP-V0.1.md` §12 row.** A D26 ("provenance is the pin in the
  repository; no chain before V1.0") would be the right one-line record; it is
  outside this card's `touches` and is left to the next `docs/MVP-V0.1.md` edit
  rather than smuggled in here.
- **No case against chains in general.** For a product whose customers are
  strangers and whose releases are audited by third parties, a log is the right
  answer. V0.1 has ten hand-sold tenants and one operator; the cost is real and
  the property is not yet worth it.
