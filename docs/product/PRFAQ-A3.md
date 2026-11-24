# PR/FAQ — A3: a backup the tester can hold

**Status:** draft 1 · 24 November 2026 · Product lead · upskilling unit P2.
**Read with:** A3 in `docs/backlog.yaml`:168–182 (its criteria are provisional
until exit 1 replaces them); `docs/architecture/ADR-backup.md` (ADR-004 v2.2 —
v3 is pending ODL-351); `docs/runbooks/backup.md`; `docs/MVP-V0.1.md` §6.7;
`docs/CLAIMS.md` §1 Money and place, §1a, §1 Reliability.
**What this is:** the Working Backwards artifact for the next gate, A3. It is not
a public page. Every sentence in the press release is a `docs/CLAIMS.md` row or
a subset of one, or is marked **pending** the card named beside it.
**The one metric:** a file the tester asked for, on a machine that was destroyed
and rebuilt, restored and checked against its hash with the recovery file as the
only secret used — no operator, no secret of Odile’s.

---

## 1. The press release

### Your files come back, even when the machine does not

**For the invited testers of the private alpha:** people who keep real work on
the machine — client files, a weekly brief, a page they watch — and will never
open a terminal.

**The problem.** The machine is one copy. A disk fails, a build goes wrong, a
machine is destroyed, and the work you have been keeping is gone. Every hosted
product says “backed up”; almost none of them lets you hold the backup, and the
ones that do hand you a repository and a command line.

**The solution.** Your machine backs itself up every night at 03:00 local, with
a password generated on the machine and written into the recovery file you
download on your first visit. The copy is encrypted before it leaves the machine;
you hold the only key that opens it. Odile can delete the copy and cannot read
it. [CLAIMS §1 Money and place; §1 Reliability “Backed up nightly. Rebuildable
in under an hour — without a key.”]

If a file goes, ask the agent and it comes back in about a second (0.931 s,
measured). If the whole machine goes, a new one is built and your home comes
back from the copy; the measured rebuild-and-restore is 38m03s. [ADR-004 §2;
`docs/runbooks/backup.md` §4–§5] **Off-site** is the promise this gate exists to
make true: today the bytes sit on HQ’s own disk, the same provider and the same
city as the machine, and `docs/CLAIMS.md` §1a holds the sentence until A3’s
destroy → rebuild → paste → restore drill lands (`t_66fcaa02`). **Pending**
Hetzner BX11 and the drill.

Nothing to install. No client, no command, no key to make. The recovery file is
the only thing to keep — and keep it off the machine.

**A quote we expect to hear (draft — not collected).** “I deleted the folder by
accident, asked the agent for it back, and it was there. Then I asked what
happens if the machine dies, and the answer was a file I had already downloaded.”

---

## 2. The tester’s moments, and the surface each needs

| # | Moment | What the tester does | What must exist | Built? |
|---|---|---|---|---|
| 1 | First visit | Downloads the recovery file | `web/first-visit.html:218`; `installer/first_visit.py` | Yes |
| 2 | Every night | Nothing | `scripts/backup.py run` + timer, 03:00 local | Yes, to the v5 SFTP stand-in at HQ; the interim destination is not shipped (`docs/runbooks/backup.md` “Vintage”, ODL-933) |
| 3 | One file back | Asks the agent; checks the file | `backup.py restore latest --include <path>` | Yes — Drill 1 |
| 4 | The machine dies | Replies to the ready email; a new machine is built; **pastes the recovery file into one page**; signs in | A restore page on the new machine | **No** — ODL-937 |
| 5 | The trial ends | — | The copy’s fate after day 14 | **No answer** — see §5 below |

---

## 3. External FAQ — the tester asks

**Where is my backup?** On a storage box at a third provider, paid by Odile,
neither your machine nor HQ. **Pending** ADR-004 v3 and ODL-351: the documents
name four destinations today (see §5).

**How do I get a file back?** Ask the agent. It restores from last night’s
snapshot and tells you where the file is. The file is checked against its
checksum in the drill.

**How do I get everything back if the machine is gone?** A new machine is built,
you paste your recovery file into its restore page, and your home comes back.
**Pending** ODL-937 — the page is designed, not built.

**Can Odile read my backup?** No. The repository is encrypted with a 32-character
password generated on your machine; HQ keeps the repository URL, the size, the
last status and the latest snapshot id. [CLAIMS §1 Money and place: “Odile can
delete it, not read it.”] The caveat is the rebuild path: today the operator
holds your recovery file for that one command. The interim design retires that
flag (`ADR-004`:635) by making the rebuild a paste on the machine — the paste
page (ODL-937) is not built.

**What is in it?** Your home: files, the agent’s skills and state, its cron jobs.
Not the agent’s program, its toolchains or its caches — those come back from the
signed installer, and `scripts/backup.py:76` names every excluded tree.

**How long is it kept?** 30 days after cancellation, then deleted. [CLAIMS §1
Money and place] **What “cancellation” means for a 14-day trial is unanswered**
(§5, question 3).

---

## 4. Internal FAQ — the dragons

**1. The destination moved three times in eight days.** ADR-004 v1 named B2; v2
made R2 the interim and B2 the target; Savio’s 24 November decision replaces
both with a Hetzner Storage Box BX11, carried as a decision only in ODL-351’s context
(`docs/backlog.yaml:9886`). The ADR’s change log stops at v2.2. Six A3 cards
still describe R2 buckets, and the runbook still says “Cloudflare R2, not the
SFTP box described below”. A PR/FAQ written before ODL-351 lands must mark the
destination pending, not pick one.

**2. “Only the user can read it” is true on the storage path and false on the
rebuild path.** ADR-004 §1.3: a rebuild runs
`odilectl provision <slug> --backup-password-file <the user’s recovery file>`,
which reads the password on HQ and seals it under the new setup token. The
recovery file therefore sits on HQ’s disk during a rebuild, and the “HQ cannot
read it” grep covers three trees that file is not under. The ADR’s own interim
cut retires the flag (`ADR-004`:635) because the rebuild becomes a paste on the
machine — but the paste page is ODL-937 and the provisioning change is ODL-924,
neither shipped at this head. Until both land, the external FAQ must say which
path it is describing.

**3. “Under an hour” was measured against a repository on the same host.**
38m03s (the timed rebuild-and-restore) and 84m13s (end-to-end, including the
harness’s 46m10s gap) were measured with the v5 SFTP stand-in at HQ. ADR-004 §9
marks the sentence **at risk** against the real destination and lets the A3
drill decide whether it stays. The page quotes the sentence because it is
published; the drill may move it.

**4. The copy has a size but no quota.** BX11 is 1 TB and 100 sub-accounts
(ODL-351). The v2 R2 design carried a 2 GB per-tenant cap and a 9 GB account
cap; the BX11 cut names no per-tenant cap. One tester who fills the box is a
support incident, not a billing event. The cap belongs in ODL-924’s DoD.

**5. “Download a copy in one action” has no surface.** See §5, question 2.

---

## 5. The three answers this spec cannot give

These are the exercise’s finding. Each is a question the press release raises
and A3’s text does not answer; each has an owner and the artifact that would
close it. They are recorded here rather than invented.

1. **“Where exactly does my backup live?”** Four current documents give four
   answers: `docs/MVP-V0.1.md:432` — “an off-site SFTP box (v5 record)”;
   `docs/runbooks/backup.md:32` — “Cloudflare R2 … until Savio’s Backblaze B2
   account is restored”; `docs/architecture/ADR-backup.md` — B2 as the target;
   and `docs/backlog.yaml:9886` — Hetzner BX11, Savio’s decision of 24 November
   2026. A tester cannot be told a destination the cards do not use, and the
   page cannot say “off-site” while the bytes are on HQ’s disk. **Close:**
   ADR-004 v3, then ODL-351’s six-card re-cut; Architect then Product lead.

2. **“How do I download my copy?”** A3 exit criterion 2
   (`docs/backlog.yaml:180`) says the user “downloads a backup copy from their
   own instance in one action”. The only download surface built is the recovery
   file’s (`web/first-visit.html:218`), and `web/privacy.html:153` describes it
   as the backup password and certificate fingerprint. The design’s read-only
   sub-account key (ADR-004 §4) is a restic credential, and restic is a CLI —
   the tester this page names will never open a terminal. There is no URL behind
   “download my copy”. **Close:** a product decision inside A3 — a page that
   streams an archive, an agent-mediated export, or both — then one card.

3. **“What happens to my copy on day 15?”** The trial is 14 days
   (`docs/product/ROADMAP-ALPHA0.md` §5 D2; `docs/backlog.yaml`:164), and the backup is kept “30 days
   after cancellation” (`docs/CLAIMS.md:128`;
   `docs/runbooks/offboard.md:14`). Trial end is not cancellation, and A3’s
   exit criteria do not name it. A tester whose trial ends on day 14 cannot be
   told whether their files survive to day 44 or disappear the next morning.
   **Close:** A8/A9’s decision on what the trial’s end does, plus one row in
   A3’s retention table; Savio (scope/money) then Product lead (copy).

---

## 6. What this page changes, and the kill criterion

| Change | Where | Owner | State |
|---|---|---|---|
| A3’s PR/FAQ exists before its cards are re-cut, so the destination and the download surface are decided against a tester’s sentence | this page | Product lead | Draft 1 |
| The three unanswered questions above, each with an owner | this page §5 | Product lead | Open |

**The kill criterion.** If a cold reader — one who did not write this page and
has not read ADR-004 — cannot answer “where is my copy, how do I get it, and who
can read it?” in one pass, A3 is a mechanism without a product: stop and rewrite
the flow, not the card. This draft fails its own test on questions 1 and 2, by
design: it records the missing answers instead of inventing them.

**Sources read:** Working Backwards PR/FAQ instructions —
<https://www.workingbackwards.com/resources/working-backwards-pr-faq> (the FAQ
as “the map to that destination and a detailed description of the dragons”);
A3 and ODL-351 in `docs/backlog.yaml`; ADR-004 (§1, §2, §4, §9); the backup
runbook (§1, §3.1, §4–§5); `scripts/backup.py`; `installer/first_visit.py`;
`docs/CLAIMS.md` §1, §1a, §1 Reliability; `docs/MVP-V0.1.md` §6.7.
