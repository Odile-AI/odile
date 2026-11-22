# Survivor triage — `tools/handoff_check.py`, 2026-11-22

`mutmut 3` in a scratch tree (module at the root so the test's plain
`import handoff_check` and mutmut's key agree), 265 mutants, four children.
Config and counts: `mutation-summary-before.txt`, `mutation-summary-after.txt`.

**Score before the new test: 195/265 killed, 70 survived (73.6%).**
**After `BaseSentinelTests`: 199/265 killed, 66 survived (75.1%).**

A score is not a defect count. Every survivor was read as a diff
(`survivors-before.txt`, `survivor-diffs-before.txt`) and sorted:

| Class | Count | What the diff changes | Example |
|---|---|---|---|
| Equivalent / unreachable | 24 | `uri=True` removed (Debian's libsqlite3 honours `file:` URIs anyway), `--verify` dropped (exit is non-zero either way), `timeout=5.0`→`6.0`, `or True`, a `"XXXX"` in an unreachable default, a SQL keyword's case | `x_card_branch__mutmut_9`, `x_main_head__mutmut_15` |
| Cosmetic | 33 | argparse `description`/`help`, `print(None)` where the line is not asserted, message case, `[:7]`→`[:8]` | `x_main__mutmut_87`, `x_main__mutmut_124` |
| Real gap | 9 | behaviour differs on a path no test takes: `--card` no longer required (3), a board error returns 3 instead of the documented 2 (2), a READY stamp with no head returns 2 instead of 1 (1), `stamp or stamp["head"]` raises when `stamp is None` (1), `branch_head` crashes in a tree with no `.git` (1), the `Unresolved` message is `None` (1) | `x_main__mutmut_91`, `x_main__mutmut_57` |

The nine real gaps are the exit-code ladder (usage/board = 2, verdict = 1)
and the error-path sentences: the docstring promises "one line names which",
and no test drives `main()` with a malformed board, a missing `--card`, or a
READY stamp whose `head:` line is empty. That is the next test to add; it was
not this session's, because the base-line gap was both reachable from a stamp
`card_acceptance.py` writes today and a false change request.

## The four killed by the added test

`BASE_RE` accepts `none` and a 7-char sha, and `card_acceptance.py:575`
writes `base: none` whenever no `main` resolves. `handoff_check.py:207` skips
that sentinel and `:209` matches a short base by prefix. No test in
`tests/test_card_acceptance.py` carried either shape, so these mutants all
survived and each turns a legitimate READY into "main moved since the stamp":

| Mutant | Change |
|---|---|
| `x_main__mutmut_109` | `if base and base != "none"` → `if base or base != "none"` |
| `x_main__mutmut_111` | `"none"` → `"XXnoneXX"` |
| `x_main__mutmut_112` | `"none"` → `"NONE"` |
| `x_main__mutmut_116` | `startswith(base) or startswith(current)` → `and` |

`mutant-112.txt` and `mutant-116.txt` show the 32-test suite green on the
mutant and `BaseSentinelTests` red, with the message naming the same commit
twice: `base is 031f1e1, main is 031f1e1`.

## The proposed test

`proposed-test.patch` appends one class, two methods, to
`tests/test_card_acceptance.py`:

- `test_a_stamp_written_without_a_base_still_passes` — `base: none`, as the
  gate itself writes it, must PASS.
- `test_a_short_base_naming_main_still_passes` — a 7-char base of `main`
  must PASS.

Both were run against the pristine module (`pristine-suite.txt`: 34 passed).
