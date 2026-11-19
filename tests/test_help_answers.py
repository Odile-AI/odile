"""ODL-238 / ODL-470 / ODL-474 — the five support answers stay inside the deck.

`docs/FAQ.md` is a public-facing support document (the claims lint already
scans it). Every sentence it marks as a deck row must appear verbatim in
`docs/CLAIMS.md`, or in a decided v3.9 row of
`docs/product/CLAIMS-RECUT-STRICT.md` that the deck has not yet absorbed. The
`odile-help` skill must name the three real exits: the FAQ link, `odile-diag`,
and the support grant.

ODL-470 re-cut the answers from the v3.4 ceremony (a boot SSH key, an unlock
prompt, a passphrase typed over SSH) to the sealed boot. ODL-474 landed the
sentences the M2 drills held back: the restore and the reliability row after
ODL-215, the support-grant answer after ODL-217. The last two tests are the
mechanical half: the old ceremony is gone from all three surfaces, and the
machine-dies moment promises last night's backup — on the help surfaces, not on
`web/faq.html`.

These are the mechanical half of "the five answers exist and match the deck";
the round trip and the transcript are in `docs/evidence/odl-238/`.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAQ = ROOT / "docs" / "FAQ.md"
DECK = ROOT / "docs" / "CLAIMS.md"
DECIDED = ROOT / "docs" / "product" / "CLAIMS-RECUT-STRICT.md"
SKILL = ROOT / "skills" / "odile-help" / "SKILL.md"
EMAIL = ROOT / "templates" / "emails" / "support.md"
# The public FAQ the installed skill points at. Its `<code>` markup is the
# only difference from the deck's plain text.
PAGE = ROOT / "web" / "faq.html"
# The file the installer writes as the tenant's $HOME/AGENTS.md. It is the
# surface the agent answers from, so the cut ceremony must be gone from it too
# (ODL-496: it taught the v3.7 reboot row until the embed and the cut moved).
AGENTS = ROOT / "templates" / "tenant-agents.md"

CITED = re.compile(r'"([^"]+)"')
CLOCK = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b")
DECK_BLOCK = re.compile(r"^\*\*Deck(?: \(v3\.9\))?:\*\*")

# The three v3.10 passages, exactly as `docs/CLAIMS.md` §5 carries them. The
# verbatim test below is the sharper guard: a paraphrase ("ask me" for "ask the
# agent") passed the restore-presence check the first writer shipped.
MACHINE_DIES = (
    "We rebuild it; you ask the agent to restore last night's backup with your "
    "recovery file — the one you downloaded on your first visit. If the machine "
    "is still running and you lost your copy, ask the agent to show you the "
    "file. Target under an hour; your last day may be lost. The drill is "
    "practiced before you get a machine."
)
RESET_RESTORE = (
    "No. There is no escrow and no passphrase. A lost sign-in is reset on the "
    "machine with your recovery code; if the disk will not open, the machine is "
    "rebuilt and you restore from your backup."
)
SUPPORT_GRANT = (
    "Ask the agent to run `odile-diag` and email the bundle to help@odile.fyi. "
    "If that is not enough, ask it to run `odile-support grant --hours 24`; it "
    "prints the expiry and the revoke command, and every session is logged. A "
    "grant is a handover: it can read the mounted disk, every file, the "
    "agent's memory and past prompts, the sign-in hash, and the backup password "
    "if the recovery file is still on the machine. Expiry and revoke block new "
    "logins; an open session keeps running until it exits. A root shell can add "
    "a keyslot, copy the volume key, or leave a service behind, so ask the "
    "agent to check `authorized_keys`, `cryptsetup luksDump` and `crontab -l` "
    "after revoke. The grant log is written on the machine; a root shell can "
    "edit it."
)

# The decided v3.9 rows the five answers quote while `docs/CLAIMS.md` is still
# v3.8. ODL-462 lands them in the deck; until then this tuple is what keeps a
# paraphrase out of the answers: each sentence is asserted against the decided
# draft, and every **Deck:** citation must be one of these or a deck row.
DECIDED_ROWS = (
    "No one at Odile can log in to a tenant machine. The only sign-in is yours.",
    "The disk unlocks only on the boot you verify. Change the image, and it "
    "stays locked.",
    "There is no passphrase to type. The disk key is sealed to the measured "
    "boot, and no one at Odile holds it.",
    "After a reboot, it starts itself. The seal is the key.",
    "A repeating job — the morning brief, a watchdog — runs when the machine "
    "comes back up if it was due while it was down.",
    "Your Odile sign-in is set on your machine at first visit and hashed "
    "there. We never see it. It is the only app-layer gate: no 2FA in V0.1.",
    "A lost Odile sign-in is reset on the machine with your recovery code. "
    "There is no HQ reset.",
    RESET_RESTORE,
    "At HQ we hold your email, the record of your machine, and a boot key "
    "with no private half. Not the disk key, not your sign-in.",
    "A recovery code and a recovery file are the way back: the code resets "
    "your sign-in, the file opens your backup. Your first visit gives you "
    "both; keep them off the machine. While the machine is still running, the "
    "agent can show it again.",
    "A support grant is a time-boxed door into the running machine. It ends "
    "for new logins when it expires or you revoke it.",
    "A root shell, for 24 hours — only when you grant it.",
    "It can add a LUKS keyslot or copy the volume key, so it can keep disk "
    "access after the grant.",
    MACHINE_DIES,
    SUPPORT_GRANT,
    "Backed up nightly. Rebuildable in under an hour — without a key.",
)

# The ceremony v3.4 taught and the sealed boot removed: the card's DoD grep,
# plus the two phrases that would still send a user to a terminal.
OLD_CEREMONY = re.compile(
    r"unlock prompt|ssh in with your key|run .unlock|your passphrase unlocks|"
    r"the boot ssh key|add a second key now|disk passphrase|initramfs",
    re.IGNORECASE,
)


def flat(text: str) -> str:
    """One line, one space: the deck wraps sentences across lines."""
    return " ".join(text.split())


def citations(faq: str) -> list[tuple[int, str]]:
    """(line number, sentence) for every quoted span on a **Deck:** block."""
    found: list[tuple[int, str]] = []
    block: list[str] | None = None
    start = 0
    for number, line in enumerate(faq.splitlines(), 1):
        if DECK_BLOCK.match(line):
            block, start = [line.replace("\u201c", '"').replace("\u201d", '"')], number
            continue
        if block is None:
            continue
        if not line.strip():
            found.extend((start, flat(match)) for match in CITED.findall(" ".join(block)))
            block = None
            continue
        block.append(line.replace("\u201c", '"').replace("\u201d", '"'))
    if block is not None:
        found.extend((start, flat(match)) for match in CITED.findall(" ".join(block)))
    return found


def section(text: str, number: int) -> str:
    """The body of answer `number`, between its heading and the next one."""
    start = text.index(f"\n## {number}. ")
    try:
        end = text.index(f"\n## {number + 1}. ", start)
    except ValueError:
        end = len(text)
    return text[start:end]


def quoted(text: str) -> str:
    """The reply a user would read: every blockquote line, joined."""
    return " ".join(line.lstrip("> ").strip() for line in text.splitlines() if line.startswith(">"))


def plain(text: str) -> str:
    """The reader's text: blockquote markers, `<code>` markup and backticks gone."""
    text = text.replace("<code>", "").replace("</code>", "").replace("`", "")
    text = "\n".join(line.lstrip("> ").strip() for line in text.splitlines())
    return flat(re.sub(r"<[^>]+>", " ", text))


class SupportAnswers(unittest.TestCase):
    def test_every_deck_citation_is_an_approved_sentence(self):
        deck = flat(DECK.read_text(encoding="utf-8"))
        decided = [flat(row) for row in DECIDED_ROWS]
        quoted = citations(FAQ.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(quoted), 15, "the FAQ cites too few deck rows")
        missing = [
            f"docs/FAQ.md:{number}: {fragment!r}"
            for number, fragment in quoted
            if fragment not in deck and not any(fragment in row for row in decided)
        ]
        self.assertEqual(
            missing, [],
            "a support answer cites a sentence neither the deck nor a decided "
            "v3.9 row carries",
        )

    def test_the_decided_rows_are_verbatim_in_the_recut_draft(self):
        # The tuple above is a copy of the decided text, not a paraphrase of
        # it: a reworded row in the draft fails here before the answers drift.
        draft = flat(DECIDED.read_text(encoding="utf-8"))
        missing = [row for row in DECIDED_ROWS if flat(row) not in draft]
        self.assertEqual(missing, [],
                         "a row quoted by the answers is not in the decided draft")

    def test_the_recut_rows_are_cited(self):
        # The five moments name the rows they come from; the bill stays on the
        # deck's unchanged money rows.
        faq = FAQ.read_text(encoding="utf-8")
        self.assertIn("v3.9", faq)
        for row in ("A1", "A3", "A4", "A5", "A7", "A8", "A9", "A10", "A11"):
            self.assertRegex(faq, rf"\b{row}\b", f"the FAQ does not cite {row}")

    def test_no_unlock_ssh_or_passphrase_ceremony(self):
        # ODL-470's first DoD. The sealed boot has no prompt to wait at, no SSH
        # step in the user's path, no boot SSH key and no passphrase to type.
        # ODL-496 adds the tenant's own standing rules: the agent answers from
        # $HOME/AGENTS.md, so a stale row there reaches the user in chat.
        for path in (FAQ, SKILL, EMAIL, AGENTS):
            text = path.read_text(encoding="utf-8")
            found = sorted({match.group(0).lower() for match in OLD_CEREMONY.finditer(text)})
            self.assertEqual(found, [], f"{path.relative_to(ROOT)} still teaches {found}")

    def test_the_machine_dies_answer_promises_the_restore(self):
        # ODL-474 flips the M1 guard after ODL-215's drill: the quoted reply a
        # user reads promises last night's backup and the measured target, the
        # help surfaces carry the reliability row, and `web/faq.html` keeps it
        # off (ODL-474's DoD: the FAQ does not gain the reliability row).
        faq = FAQ.read_text(encoding="utf-8")
        self.assertIn("restore", quoted(section(faq, 3)).lower())
        for path in (FAQ, SKILL, EMAIL):
            text = path.read_text(encoding="utf-8")
            self.assertIn("restore", text.lower(), f"{path.relative_to(ROOT)} lost the restore")
            self.assertIn("backed up nightly", text.lower(),
                          f"{path.relative_to(ROOT)} lost the reliability row")
        page = (ROOT / "web" / "faq.html").read_text(encoding="utf-8")
        self.assertNotIn("backed up nightly", page.lower())

    def test_the_landed_answers_are_verbatim(self):
        # ODL-474's DoD says "verbatim", not "the same fact": the first writer
        # shipped "ask me" where the deck says "ask the agent". Each passage is
        # checked against the surface the DoD names, with markup removed.
        for label, answer, surfaces in (
            ("the machine-dies answer", MACHINE_DIES, (FAQ, SKILL, EMAIL)),
            ("the reset/unlock answer", RESET_RESTORE, (FAQ, PAGE)),
            ("the support-grant answer", SUPPORT_GRANT, (PAGE, EMAIL)),
        ):
            needle = flat(answer).replace("`", "")
            for path in surfaces:
                self.assertIn(needle, plain(path.read_text(encoding="utf-8")),
                              f"{path.relative_to(ROOT)} does not carry {label} verbatim")

    def test_no_reliability_fact_escapes_the_deck(self):
        # A fact stated in the guidance prose is not covered by a **Deck:**
        # quote: "Backups run nightly at 03:00 local." slipped past the first
        # version of this file. Guard the class, not just the one sentence.
        faq = flat(FAQ.read_text(encoding="utf-8"))
        deck = flat(DECK.read_text(encoding="utf-8"))
        escaped_times = sorted({span for span in CLOCK.findall(faq) if span not in deck})
        self.assertEqual(escaped_times, [], "docs/FAQ.md states a clock time the deck does not carry")
        self.assertNotIn(
            "nightly at",
            faq.lower(),
            "docs/FAQ.md qualifies the backup cadence; the deck carries no time of day",
        )

    def test_the_bill_answer_states_no_price_and_still_answers(self):
        # alpha.0 has no price (`docs/MVP-V0.1.md` §0); the v5 $49 line and the
        # held allowance sentence are not repeated while the alpha is free. The
        # answer still says who holds the billing record and how a person reads
        # the question, so a "why was I charged?" mail is answered, not bounced.
        faq = FAQ.read_text(encoding="utf-8")
        reply = quoted(section(faq, 4))
        email = EMAIL.read_text(encoding="utf-8")
        self.assertIn("has no price", reply)
        self.assertIn("has no price", email)
        for text, label in ((reply, "docs/FAQ.md moment 4"),
                            (email, "templates/emails/support.md")):
            for gone in ("$49", "$12", "founding price", "month-to-month"):
                self.assertNotIn(gone, text, f"{label} still quotes {gone!r}")
            self.assertIn("Stripe holds the card", text, label)
            self.assertIn("within 24 hours", text, label)

    def test_the_five_moments_are_all_answered(self):
        text = FAQ.read_text(encoding="utf-8")
        for number in range(1, 6):
            self.assertIn(f"\n## {number}. ", text, f"answer {number} is missing")
        self.assertIn("https://odile.fyi/faq", text)
        self.assertIn("odile-diag", text)

    def test_the_skill_names_the_three_exits(self):
        skill = SKILL.read_text(encoding="utf-8")
        self.assertIn("name: odile-help", skill)
        for needle in (
            "https://odile.fyi/faq",
            "odile-diag",
            "odile-support grant --hours 24",
            "help@odile.fyi",
        ):
            self.assertIn(needle, skill, f"the skill does not name {needle!r}")

    def test_the_skill_is_not_named_help(self):
        # Hermes reserves /help for its own command list; a skill whose name
        # collides with a core command is skipped by agent/skill_commands.py.
        skill = SKILL.read_text(encoding="utf-8")
        self.assertNotIn("name: help\n", skill)


if __name__ == "__main__":
    unittest.main()
