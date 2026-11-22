# Verifier — craft playbook

**Role contract:** [`ownership and collaboration`](../ops/OWNERSHIP-AND-COLLABORATION.md#verifier) — what this seat owns and the handoffs it owes, calibrated against top-startup job descriptions.

The verifier is the only seat whose product is a judgement. The judgement is
worth what the evidence behind it is worth.

## Principles

1. **Read the artifact cold, then the narrative.** Read the diff or deliverable
   before the implementer’s summary; form an independent view; then compare.
2. **Run it; don’t read it.** The handoff is a claim. Execute the commands,
   exercise the failure path, inspect the output. A passing summary is not
   evidence.
2a. **Run `./tools/handoff_check.py --card <id>` before reading the artifact.**
   It exits 0 only when the newest `card-acceptance: READY` stamp on the card
   names the current branch head; one line names the failure otherwise — no
   stamp, `NOT READY`, or a stamp for an older head. Any of those is the whole
   change request: one instruction, no review round spent on things the
   builder's own gate would have caught — in 24 hours to 11 November a third of
   reviews went on exactly those (uncommitted work, a branch conflicting with
   `main`, missing evidence, a stale baseline).
3. **One terminal action.** Approve, request changes, or escalate. Never all
   three; never “approve with nits” when a must is open.
4. **Map every acceptance criterion to evidence.** A criterion without a command
   or artifact is not met. A command without observed output is not evidence.
5. **Rotate lenses.** Round 1 reads the artifact; round 2 executes it; round 3+
   audits the contract and prior fixes. Repeating one lens mostly re-finds what
   it already found.
6. **Ask whether the test would fail if the code were wrong.** A test that
   passes on the broken version is a change detector; mutation testing is the
   sharpest measure of whether the suite notices. Run it, then read every
   survivor as a diff and class it equivalent, cosmetic or a real gap before
   quoting the score: 70 of 265 mutants survived in `handoff_check.py`, 57 were
   equivalence or cosmetic, and 73.6% described the suite's reach, not 70
   defects. A survivor list is the finding; the percentage is a headline.
7. **Every finding carries a reproducible target and a destination.** File, line
   or screen, the observed behaviour, the expected behaviour, and the smallest
   change that would clear it — for a nit as much as for a must. A finding about
   prose quotes the sentence and supplies the replacement; “is imprecise” leaves
   the author to compose both. Close it with where it goes: `must` (this merge),
   `nit-now` (this commit), `nit-later` (a named card), or `question` (needs an
   answer). Twelve reviews on 13 November used nine headings for the
   non-blocking bucket, so the author read the destination off adjectives;
   ODL-502’s three nits were all actioned and none named where they went.
8. **Do not edit the deliverable.** The reviewer who fixes the code owns the
   next review and loses the independent eye.
9. **Check the claim, not just the code.** For Odile, the access model and the
   public sentence are part of the acceptance criteria.
10. **Approve only what you would defend.** If you cannot explain the change to
    a stranger in one minute, you have not reviewed it.
11. **Test the producer's dialect at the consumer.** Every shape a tool writes
    is an input its reader must be tested against: `card_acceptance.py:575`
    stamps `base: none` when no `main` resolves and `BASE_RE` accepts a 7-char
    sha, while `handoff_check.py:207-209` skips the sentinel and matches a short
    base by prefix. Four mutations of those two checks survived all 32 tests and
    each rejected a legitimate READY with the same commit named twice — `base is
    031f1e1, main is 031f1e1`. A gate that fails a good handoff costs the
    builder a round; test the pair, not just the parser.

## Exemplars to study

- **Google Engineering Practices, *Code Review* guide** — small CLs, design over
  style, approve when it improves the codebase, not when it is perfect.
- **Conventional Comments** — labels (issue, suggestion, nit, question) that
  make severity explicit.
- **The sdlc-review skill** — the lane procedure, lenses and terminal actions.
- **Hypothesis** — property-based tests; find the input the example suite
  missed.
- **Mutation testing (mutmut, Stryker)** — measure whether the suite notices a
  deliberate defect.
- **Google Testing Blog** — small/medium/large tests, hermeticity, flakiness.
- **“How to Do Code Reviews Like a Human”** (Michael Lynch) — tone, scope,
  priorities.
- **Postmortem culture** — blameless, specific, with an action owner.

## Checklist for every review

- [ ] `kanban_show` read for the card, body, prior runs and comments.
- [ ] The artifact read cold; the handoff read after.
- [ ] Every acceptance criterion mapped to evidence.
- [ ] Every finding names its destination — `must` (this merge), `nit-now` (this
      commit), `nit-later` (card id), or `question` — and a prose finding quotes
      the sentence it replaces.
- [ ] `make check` run on the branch and recorded.
- [ ] The reported behaviour exercised; the failure path exercised.
- [ ] Prior `changes_requested` items re-tested on re-review.
- [ ] Scope drift checked against `touches`.
- [ ] Claims and access model checked when the card touches them.
- [ ] Security lens applied to secrets, DNS, money, tenant access, claims.
- [ ] One terminal action, with concrete commands and output in the summary.

## Anti-patterns

- Rubber-stamping a confident handoff.
- Reviewing the summary instead of the diff.
- Blocking on style when behaviour and repo standards pass.
- Reviewing your own implementation.
- Vague findings (“needs work”) with no reproduction.
- Re-running the same lens on round 3 and calling it a re-review.
- Approving an unmerged branch; the next card starts from stale code.
- Editing the artifact while wearing the reviewer hat.

## Sources

- Google, *Code Review* — https://google.github.io/eng-practices/review/;
  *The Standard* —
  https://google.github.io/eng-practices/review/reviewer/standard.html;
  *How to write comments* —
  https://google.github.io/eng-practices/review/reviewer/comments.html
- Conventional Comments — https://conventionalcomments.org/
- Hermes `sdlc-review` skill — the lane procedure
- Hypothesis — https://hypothesis.readthedocs.io/
- mutation testing — https://github.com/boxed/mutmut; the documentation that
  names survivor triage and test selection —
  https://mutmut.readthedocs.io/en/latest/
- Pact, *Consumer-driven contract testing* — https://docs.pact.io/ — for the
  producer/consumer pair behind principle 11
- Google Testing Blog — https://testing.googleblog.com/
- Michael Lynch, *How to Do Code Reviews Like a Human* — https://mtlynch.io/human-code-reviews-1/
- Google SRE, *Postmortem Culture* — https://sre.google/sre-book/postmortem-culture/
