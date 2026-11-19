# Growth — craft playbook

**Role contract:** [`ownership and collaboration`](../ops/OWNERSHIP-AND-COLLABORATION.md#growth) — what this seat owns and the handoffs it owes, calibrated against top-startup job descriptions.

Odile is not a funnel yet. It is ten true sentences reaching ten people who will
tell the truth. Growth is measured in activated users and week-4 retention, never
in signups.

## Principles

1. **Positioning before copy.** Decide the market, the alternative, the
   differentiator and the proof before writing a headline (Dunford). A tagline
   without a position is decoration.
2. **One audience, one promise, one metric — and the metric is activation.**
   If the page speaks to everyone, it speaks to no one. Name the person and the
   moment. Define the event that means “it worked” and the retention window;
   page views, waitlist signups and impressions are inputs, not outcomes. The
   first week is the product: a named human asks one question on day 1, 3 and 7
   — did the file survive, what did you ask for today, what did you stop doing by
   hand — and the answers are the retention signal (Graham, “Fragile”).
3. **Do things that don’t scale — and install, don’t link.** Hand-recruit the
   first ten. When someone says yes, get on a call, share the screen and stay
   until the machine is running and the first file exists; the provisioning wait
   is the past-tense interview. “Great, I’ll send you a link” is how a yes
   becomes a maybe; the Collison installation is “right then, give me your
   laptop” (Graham, “Recruit”).
4. **Recruit at least half the ten outside your network.** Friends answer to be
   kind; strangers answer to be right. A friend’s yes is a favour, not demand,
   and week-4 retention among friends measures the friendship (Alströmer).
5. **Interview the past, not the future.** Ask how they do the job today, what
   is hardest, why it is hard, how often, why it matters and what they have
   already tried; watch them do it on a screen share. Do not mention the product
   until the end, or at all: an early pitch turns the rest of the call into a
   compliment. “Would you use this?” measures politeness (Alströmer).
6. **Show the product.** A real screenshot, a real command, a real terminal
   transcript beats an adjective. Demos are the highest-trust copy. A lasting
   asset is not done until its end card prints the real destination: the
   7 November cut passed brand review with `Repo: <REPO_URL>` burned in and
   would have gone to judges that way.
7. **Every sentence has evidence.** The claims ladder in `docs/CLAIMS.md` is
   the whole vocabulary. If the truth is stronger, update the deck first.
8. **Price with the product, not after it.** The plan, the allowance and the
   hard stop are part of the promise; a discount below unit cost hides the
   economics the alpha exists to learn.
9. **One call to action.** Join, claim, pay, install — one primary per surface.
   A second CTA is a second decision.
10. **Write like a person.** Short sentences, concrete nouns, no hype, no
    “revolutionary”. Read it aloud; if it sounds like a deck, rewrite it.
11. **Kill the experiment.** Every campaign or page change names the result that
    would make us revert it. Otherwise it becomes furniture.
12. **Audit the sentence before it ships, not the argument after.** Score each
    candidate promise 0–2 on Dunford’s five components *as they appear in the
    sentence*; a promise that names no alternative is decoration however good it
    sounds in the room. Then check every noun: if it is ours (`tenant`, `cmdline`,
    `attestation`), the reader has to translate the proof. The live lede scored
    4/10 against a value-first 6, and the proof under it carried `tenant`.
13. **The founder's post is the submission; the brand answers it once, and never
    in his thread.** A brand reply is an interruption with a receipt: X renders it
    “Replying to…”, stores the conversation's participants ahead of the copy (61
    characters of five handles on the 14 November reply), and ranks it after
    followed accounts, after replies the root author answered and after verified
    accounts, so a small brand lands last. A brand quote is a footnote in the
    company's name — Turso's quote of its founder's announcement took 5.5% of its
    likes and 7.5% of its views. What works is a repost, which adds no words and
    keeps the founder as author (nine of the thirteen infra accounts read on
    15 November repost their founder), and one pinned post carrying the fact the
    founder's post lacks. One brand surface per launch, pinned while it has a next
    step; replies are for customers. Worked example and sources:
    `docs/growth/X-PLAYBOOK.md`.
14. **Frame 0 is the thumbnail.** X uses a video's first frame as its poster, so
    a screen recording that opens on an editor window spends the only image a
    scroller sees on browser chrome. Check frame 0 the way the end card is checked
    for placeholders. The submitted 4 m 39 s cut's poster is a Google Slides edit
    view with a readable address bar, while a 99-second cut with a title card had
    been ready for a week.

15. **A proof proves its predicate.** Re-read the claim's own subject against
    the current decision record before publishing, not the drill next to it.
    ODL-215 proved a backup restores; it did not prove where the bytes sit, so
    "Backups are off-site" stayed held when ADR-004 measured the repository on
    HQ's disk in the same provider and city (ODL-474 follow-up, `t_5f10dd14`,
    19 November 2026). A drill for a neighbouring property is not evidence for
    this sentence, and "the proof ran" is not a reason to publish.

## Exemplars to study

- **April Dunford, *Obviously Awesome*** — the positioning canvas; market,
  alternative, differentiator, proof.
- **Paul Graham, “Do Things That Don’t Scale”** — recruit manually, delight
  manually, learn manually.
- **Gustaf Alströmer, “How to talk to users”** — the past-tense interview: no
  pitch, concrete examples, watch the work.
- **Basecamp** — marketing as product; a calm, opinionated page; no growth
  hacks.
- **Stripe** — docs as the top of the funnel; the first integration is the
  onboarding.
- **Linear** — a changelog and a method page that sell by showing how the
  product thinks.
- **Superhuman** — onboarding as a concierge experience for the first users.
- **Bob Moesta / Jobs-to-be-Done** — the switching moment; what the user fires
  before they hire you.
- **Amazon PR/FAQ** — write the launch before the product; the FAQ is the
  objection list.

## Checklist for every surface

- [ ] The audience and the moment are named in one sentence.
- [ ] The promise is a sentence a user would repeat to a friend.
- [ ] The copy is dated to the product it sells: diff the live page against
  `docs/MVP-V0.1.md`’s requirement table and the `CLAIMS.md` last-reviewed date
  before rewriting a word. An approved sentence can describe a retired mechanism
  (10 Nov: “The only SSH key is yours” against R2, no user-managed key), and then
  copy is blocked on a deck re-cut, not on a copywriter.
- [ ] Every privacy/security/reliability sentence traces to `docs/CLAIMS.md`,
  and so does every provenance fact: "it ships on another page" is not a row.
- [ ] Every placeholder is counted with `grep -n`, and every occurrence is named
  in the pre-post checklist, in both the replace and the delete branch.
- [ ] One primary CTA; the next step is unambiguous.
- [ ] The page shows the product, not a stock illustration of it.
- [ ] The activation event and the metric are defined.
- [ ] The recruit is an install: the script ends with a file in Files, not a
  link, and every queue row carries a send condition and a kill line.
- [ ] The result that would kill or revert this is written.
- [ ] The channel's own limits are measured, not assumed: the character count
  with the platform's link weighting, the longest literal the post must carry
  (a 128-hex measurement is 45% of a 280-character post, so it becomes an
  instruction and the page carries the paste), and which media can carry a
  description (X attaches alt text to images, not to video — the cut carries its
  captions).
- [ ] The attachment's first frame works as the poster, and its last frame prints
  the real destination. A video with the right end card and the wrong frame 0 is
  half-checked.
- [ ] A post the human publishes is read as published, not as drafted: what it
  actually carries, what it leaves to a reply, and which of the two links a
  stranger can reach without signing in.
- [ ] No tracker, cookie or third-party request on a public page.
- [ ] No number without a source and a date.
- [ ] The page works with JavaScript disabled and on a 390 px screen.

## Anti-patterns

<!-- claims-lint:off -->

After this line the list has to name the words it forbids.

- “Unhackable”, “zero-knowledge”, “trustless”, “military-grade” — the forbidden
  list in `docs/CLAIMS.md`.
- Counting waitlist signups as demand; celebrating impressions.
- A discount below unit cost; a free tier that teaches nothing.
- A launch page that describes the architecture instead of the moment.
- Five CTAs, three audiences, zero position.
- A promise with no proof command.
- Growth hacks that trade trust for a click.
- A date that is not in the plan.
- A discovery call that pitches; a “yes, send me the link” counted as a
  recruit; a compliment counted as demand.
- A first ten drawn only from friends and former colleagues; politeness read as
  retention.

<!-- claims-lint:on -->

## Sources

- April Dunford, *Obviously Awesome* — https://www.aprildunford.com/obviously-awesome
- April Dunford, “A Quickstart Guide to Positioning” (15 March 2021) — https://www.aprildunford.com/post/a-quickstart-guide-to-positioning
- Paul Graham, *Do Things That Don’t Scale* — https://paulgraham.com/ds.html
- Gustaf Alströmer, *How to talk to users*, YC Startup Library — https://www.ycombinator.com/library/Iq-how-to-talk-to-users
- Basecamp — https://basecamp.com/
- Stripe docs — https://docs.stripe.com/
- Linear method — https://linear.app/method
- Bob Moesta, *Demand-Side Sales 101*
- Amazon, *Working Backwards* (Bryar & Carr)
- Odile, `docs/CLAIMS.md` — the only approved vocabulary
