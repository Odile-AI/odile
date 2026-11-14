# Odile — the positioning canvas

**Status:** cycle 1, unit G1 · 10 November 2026 · **Owner:** Growth
**Source:** April Dunford, *Obviously Awesome* and “A Quickstart Guide to Positioning” (15 March 2021) — <https://www.aprildunford.com/post/a-quickstart-guide-to-positioning>
**Product:** `docs/MVP-V0.1.md` **v5, 10 November 2026** — the owner’s requirement, R1–R6. **Public vocabulary:** `docs/CLAIMS.md` v3.6b (9 November), which is one generation behind on the access rows and is the reason no new access copy ships yet.
**Live artifact:** `https://odile.fyi/`, fetched 10 November 2026; the sentences it serves are in `docs/ops/evidence/2026-11-10-growth/live-lede.txt`.

Dunford’s order is the whole method: competitive alternatives, unique
attributes, value, the customers who care, and only then the market category.
Each component is defined by the one before it, so the canvas is filled top to
bottom or not at all. The category is a conclusion, not a choice made first.

## 1. Competitive alternatives — what the ten would do if Odile did not exist

| # | Alternative | What it gives them | What it does not | Evidence |
|---|---|---|---|---|
| 1 | Keep the chat subscription (ChatGPT / Claude) and work around the ceiling | The best models, a polished window, a price they already pay | No computer, no cron, no filesystem they own, shared tenancy | `MVP-V0.1.md` §0: the buyer “already pay[s] for an AI subscription and [has] hit its ceiling” |
| 2 | Self-host Hermes / OpenClaw on a VPS | A machine that persists, if they are the sysadmin | They build it, patch it, back it up, and trust themselves | `MVP-V0.1.md` §2 wedge table |
| 3 | A generic VPS plus an agent | A computer | A sysadmin project; the provider has root; no privacy proof | `MVP-V0.1.md` §2 |
| 4 | Do nothing — keep pasting into chat | Nothing changes | Nothing | Dunford: “we typically lose 25% of deals to ‘no decision’”; the status quo is the alternative to beat |

**Phantom competitors, deliberately excluded.** Enterprise confidential-computing
platforms, code-sandbox APIs, managed Kubernetes, “AI PCs”. Not one of the ten
would name one if Odile vanished, and none is ever seen in a deal at this size.
Dunford’s first trap: naming a competitor the customer never considers waters
down the position.

**What v5 changes here.** Alternatives 2 and 3 used to be the *buyer’s own
profile* — “technical enough to generate one SSH key, not willing to run a
VPS”. R1 and R2 remove the buyer from that list entirely: non-technical, never
opens a terminal. So the VPS stops being a near-miss and becomes the thing the
buyer is escaping, which sharpens the position and raises the bar on the proof —
there is no longer a technical reader to fill in what the page leaves out.

**The finding this forces.** Alternative 1 is the one every one of the ten
already holds, and the live lede never names it. The promise on the page is a
category statement with a proof attached; the reader has to supply the
comparison. That is a positioning gap, not a copy problem.

## 2. Unique attributes — what we have that those four do not

Each row is a requirement in v5, with the test the requirement carries.

| Attribute | Why the alternatives lack it | Requirement / proof |
|---|---|---|
| No terminal in the user’s path | A VPS *is* a terminal; self-hosting is a weekend of commands | R1: claim → sign-in → workspace, zero commands, phone and laptop |
| No user-managed SSH key, and no usable user key on the machine | Every VPS is created with a key; a managed host keeps one | R2, R4: zero usable user keys; `/verify` shows it |
| Keyless sealed boot: the disk key is derived from the SNP key and sealed to the measured image | A VPS disk is either unencrypted or unlocked by a secret a human holds | R5: fails closed on a changed image or command line; auto-unlocks on reboot |
| No operator key, no escrow, root locked | Every managed host holds a way in “just in case” | R4; `CLAIMS.md` §1 Access |
| The proof is checkable: attestation, measurement, seal policy, zero keys, sign-in hash, root lock | “Trusted” hosting is a policy, not a measurement | R6; `CLAIMS.md` §4.4–4.5 |
| Inference in a TEE that does not pass through Odile’s servers | A chat product sees every prompt | `CLAIMS.md` §1 Inference |
| One machine per person, not shared tenancy | A chat plan is one account among millions on shared compute | `CLAIMS.md` §1 “One machine per person. Nothing is shared.” |
| The marketing page itself runs on the stack it sells | No chat product can make this claim at all | `MVP-V0.1.md` §1 |

**One attribute has retired since the live page shipped.** “The only SSH key is
yours” is v4. R2 says the tenant has no usable user key, and R5 replaces the
user’s passphrase with a sealed key. It is still an approved sentence in
`CLAIMS.md` §1, so the page is not in violation — it is one generation behind,
and the deck re-cut is a card after ODL-310 (`MVP-V0.1.md` §7). Growth does not
edit the deck and does not ship around it; it states the block.

## 3. Value — so what, for the buyer

- **Nothing to configure.** Not a shorter ceremony — no ceremony. Claim, set a
  sign-in, work (v5 §0; R1–R3).
- **A computer that keeps working when the tab closes.** Files that persist,
  packages it can install, a job that fires at 7 a.m. and a brief waiting in the
  inbox (`MVP-V0.1.md` §3 day 1–5).
- **A place for the work you would only put on your own laptop.** The privacy is
  not a badge; it is the reason the machine can be trusted with the client file,
  the inbox export, the half-finished thing. “No one at Odile can log in” is what
  makes that true rather than a promise.
- **No sysadmin.** Provisioned in minutes, backed up nightly, rebuildable in under
  an hour without Odile holding a credential (`CLAIMS.md` §1 Reliability).

The value is *possession plus trust*, not *better answers*. Any sentence that
sells answer quality is selling alternative 1.

## 4. The customers who care — and the ones who do not

**Best fit (v5):** ten people who already pay for an AI subscription, have hit
its ceiling, and will never open a terminal — they never generate a key and never
run a command (`MVP-V0.1.md` §0, R1). They have usually tried to fix the ceiling
already; the try is the evidence.

**Poor fit, for the alpha:** anyone who wants a better chat window (they compare
on model quality and price per token, where Odile loses); anyone who wants root
and a shell (they will ask for SSH, and for them a VPS is the right answer);
teams, where one machine per person breaks the shape.

Dunford’s test is not “who could use this” but “who cares a lot”, so the segment
is defined by the alternative they are escaping, not by job title.

## 5. Market category — the frame, decided last

**Chosen:** *your own computer, with an agent inside it* — a personal machine,
sold managed. It borrows two frames the buyer already owns (“computer”, “agent”)
and invents nothing.

**Rejected:** a new category (“agentic workspace”, “AI operating system”,
“personal cloud”). Dunford’s second trap: a new category must be taught before it
can frame anything, and she cites that 90% of tech companies that went public in
the five years to 2021 positioned in existing markets rather than creating one. A
ten-person alpha has no education budget.

**Cost of the frame, sharpened by v5:** “computer” sets VPS assumptions — a box
you administer — and the v5 buyer is exactly the person who will not administer
one. The frame only works if the sentence after it removes the labour (“nothing
to install”). Otherwise the category does the opposite of its job.

## 6. Three candidate promises, scored

Each sentence is scored 0–2 on whether the component is present *in the sentence
itself* (0 absent, 1 implied, 2 explicit), in Dunford’s order. All three are
approved `CLAIMS.md` §1 rows and carry no house noun — the choice is a
positioning choice, not a claims one (`docs/ops/evidence/2026-11-10-growth/claims-trace.txt`).

| # | Promise | Alt | Attribute | Value | Customer | Category | Total |
|---|---|---|---|---|---|---|---|
| A | “Your own computer, with an agent inside it.” *(live today)* | 0 | 1 | 1 | 0 | 2 | **4** |
| B | “A real Linux machine, not a chat window.” | 2 | 1 | 0 | 0 | 1 | **4** |
| D | “It keeps working when you close the tab.” | 1 | 1 | 2 | 1 | 1 | **6** |

- **A** has the best category and no alternative: “computer” is a frame the buyer
  owns, but the sentence never says what it replaces, and “own … inside it” would
  also describe a VPS with an agent on it.
- **B** is the only candidate that names the alternative — but under v5 “Linux”
  selects the sysadmin, the buyer we just removed from the segment. The sentence
  names the right alternative and the wrong reader.
- **D** is the only candidate that carries the value as a consequence rather than
  an adjective, and it needs no technical noun: the reader has closed a tab and
  knows what it costs. Its weakness is the opposite of B’s: “it” names nothing, so
  the category has to be set by the sentence before it.

**Dropped before scoring:** “One machine, one key, one owner.” Under v5 the user
holds no key (R2), so the sentence now describes the machine’s key rather than
the owner’s, and it never named a category.

## 7. The pick, and the evidence that would change it

**Pick D: “It keeps working when you close the tab.”** Highest score, approved
verbatim, and the only candidate that carries the value to a non-technical
reader. It is the lead of a pair, not a standalone: the first screen reads
D → A → the access proof, so the category arrives in the second sentence and the
differentiator in the third.

**Why not A, the live sentence:** A is the better category line and the weaker
promise. Keeping it as the lead is what the page does today, and it is why the
page reads as a category statement with a proof bolted on.

**Evidence that would change it:** the first five discovery calls. If fewer than
three describe the ceiling as losing work or re-explaining context — if they name
answer quality or price instead — then persistence is the wrong ceiling and D is
selling the wrong value. Then A returns as the lead, B’s alternative (“not a chat
window”) becomes the subhead, and the proof follows.

**What it changes on the next card:** the landing-copy card starts from the pair
D → A, not from the live lede, and the canvas is not re-litigated per card.
Re-score only when the alternative set changes — a new alternative, or a phantom
that starts appearing in calls. The access proof in position three waits for the
`CLAIMS.md` re-cut after ODL-310; until then the third sentence stays the v4 row.
