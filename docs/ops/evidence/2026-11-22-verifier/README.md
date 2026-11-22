# V2 — does the suite notice a defect in the verifier's first step?

**Date:** 2026-11-22 · **Seat:** verifier · **Unit:** V2 (primary source —
mutation testing)
**Source:** mutmut documentation and README —
<https://mutmut.readthedocs.io/en/latest/> and
<https://github.com/boxed/mutmut> ("mutmut is a mutation testing system for
Python … remembers work … knows which tests to execute").
**Module:** `tools/handoff_check.py` (223 lines) — the tool the verifier's
playbook runs before reading any artifact (principle 2a). Its suite is
`tests/test_card_acceptance.py` (32 tests at `b000999`).

## What was run

The module under mutation must be importable as `handoff_check`, so the
scratch tree puts it at the root and keeps `card_acceptance.py` and
`board_sync.py` in `tools/` where the test's `sys.path.insert` looks. No file
in the repository was changed by the run.

```
# /root/.hermes/cache/scratch/mut-v2 — scratch copy, module at the root
mutmut 3.8.1, Python 3.14.4 (venv), 4 children
setup.cfg:
  [mutmut]
  source_paths=handoff_check.py
  pytest_add_cli_args_test_selection=tests/test_card_acceptance.py
  also_copy = conftest.py, tools/
mutmut run --max-children 4
```

Observed: **265 mutants — 195 killed, 70 survived, 0 no-tests, 0 timeouts**
(5.85 mutations/second, 46 s). A mutmut run against the repository's own
`/usr/bin/python3` was not used: mutmut needs `pytest`, which the host's
system interpreter does not carry, and a venv keeps the install off the
system Python that `make check` pins.

## Result

| | Killed | Survived | Score |
|---|---|---|---|
| `b000999`, 32 tests | 195 | 70 | 73.6% |
| + `BaseSentinelTests` (2 tests) | 199 | 66 | 75.1% |

The 70 survivors, read one by one (`survivors-triage.md`): 24 equivalent or
unreachable, 33 cosmetic (help text, message case, `[:7]`→`[:8]`), **9 real
gaps** — the exit-code ladder in `main()` (usage/board = 2, verdict = 1) and
the error-path sentences, none of which a test drives.

The one test to add in this session was the producer/consumer pair the gate
itself creates: `card_acceptance.py:575` writes `base: none` when no `main`
resolves, `BASE_RE` accepts a 7-char sha, and `handoff_check.py:207-209`
skips the sentinel and matches a short base by prefix. No test carried either
shape, so four mutants that turn a legitimate READY into "main moved" survived
(`x_main__mutmut_109/111/112/116`). `mutant-112.txt` and `mutant-116.txt`
show the existing 32 tests green on the mutant, the added test red, and the
message naming the same commit twice: `base is 031f1e1, main is 031f1e1`.

The proposed test is `proposed-test.patch`; it is evidence, not a committed
test, because this session commits only the playbook, the track table, the log
and this directory. Applying it is a card-sized change for the owner of
`tests/test_card_acceptance.py`.

## Files

- `mutation-summary-before.txt`, `mutation-summary-after.txt` — the runs
- `survivors-before.txt`, `survivors-after.txt` — mutant ids by name
- `survivor-diffs-before.txt` — `mutmut show` for all 70, the triage input
- `survivors-triage.md` — classes, counts, and the four killed mutants
- `mutant-112.txt`, `mutant-116.txt` — existing suite green, new test red
- `proposed-test.patch` — the test class, against `b000999`
- `pristine-suite.txt` — 34 tests pass with the class added to the real module
