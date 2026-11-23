# The history gate's 81 findings — Security's verdict

**Card:** ODL-945 (`t_64327634`) · **Date:** 2026-11-23 · **Seat:** Security
**Subject:** `history-secrets` is red on every push to `main`: 81 `forbidden-value`
findings in four commits that carried the operating record before `7ce0f2b`
untracked it (`66aace0`, `54ff33b`, `c57b286`, `8d8f33e`).
**Decision:** option (a) of the card — waive the 81 as debt **passes**, with
**14 path-pinned rows** in `leak-allow.tsv`, not the card's 11 directory globs.
The reason for the narrower shape is §"Why 14 path rows" below.

The question is not "does the gate pass?" It is "does the waiver prove what it
says, and does a new leak still red the job?". The first answer is yes, on the
evidence below; the second is yes, measured with three planted literals.

## Verdict, per claim

| # | Claim | Verdict | The command and its output |
|---|---|---|---|
| H1 | The 81 findings are all inside the two dead directories; no live path carries one | **pass** | baseline scan, §1: every one of the 81 begins `docs/ops/evidence/2026-11-19-growth/` or `docs/ops/evidence/2026-11-20-finance/`; 14 distinct `(path, value-digest)` pairs, 8 distinct digests |
| H2 | Those directories are dead on the pushed head, and the four commits are already public | **pass** | `git ls-tree -r origin/main -- docs/ops` → 0 lines; same at `main` and `HEAD`; `git merge-base --is-ancestor` on each of the four → yes |
| H3 | The waiver is value- and path-pinned: another value at a pinned path, and the same value at another path, both still fail | **pass** | probe repo, §4: 3 planted literals → 3 findings, none waived |
| H4 | `publish-lint` and the tree scan are unchanged | **pass** | tree scan before and after the rows: same 407 files, same 456 allowlisted, PASS; only the allowlist file's own bytes differ (§3) |
| H5 | A planted forbidden literal in a live path still reds the tree job | **pass** | `web/leak-probe.txt` with the box id → `FAIL: 1 finding(s)`; removed → PASS (§3) |
| H6 | Every new row is `debt`, cites `t_3436baff`, carries an expiry, and no row is stale | **pass** | scan header: `170 rows, 4 credentials, 14 debt`; no `stale-allow`; the 14 rows in §2 |
| H7 | With the rows, the history job is green on the line the push lands | **pass, locally** | §1: 850 commits / 2,191 unique blobs → `PASS`; the `--no-ff` merge into the 859-commit `main` (861 commits / 2,210 blobs) → `PASS`; the push run id is produced by the merge that lands this card (Security does not push) |
| H8 | The rows put no secret and no deny-list literal into the tree | **pass** | pre-push patch scan over the diff → `PASS`; `credential=no` on every row; reasons name the labels, never the values (§2) |

## 1. The baseline, and the green line

A single-branch clone of the card's head `8c2e912` (the `origin/main` the card
names, 850 commits) — the shape CI checks out — with the HQ deny list supplied
at its documented path:

```
$ git rev-list --count --all
850
$ python3 tools/leak_scan.py --history \
    --forbid-file docs/ops/leak-forbid.txt --forbid-only
allowlist:       156 rows, 4 credentials, 0 debt
leak scan: history
  commits scanned: 850 (git rev-list --all)
  unique blobs:    2,191
  bytes scanned:  172,976,386
  allowlisted:    3754
...
FAIL: 81 finding(s) in the history
```

All 81 findings, grouped (count, deny-list label, value digest, path):

| n | label | digest | path |
|---|---|---|---|
| 10 | servury-box-id | `sha256:3e5275784e44` | `docs/ops/evidence/2026-11-19-growth/make-check.txt` |
| 3 | servury-dev-account | `sha256:4014bb9b4e56` | `docs/ops/evidence/2026-11-19-growth/make-check.txt` |
| 5 | tenant-instance-id | `sha256:eaf6312fa1f2` | `docs/ops/evidence/2026-11-19-growth/make-check.txt` |
| 24 | servury-box-id | `sha256:3e5275784e44` | `docs/ops/evidence/2026-11-20-finance/make-check.txt` |
| 6 | servury-dev-account | `sha256:4014bb9b4e56` | `docs/ops/evidence/2026-11-20-finance/make-check.txt` |
| 10 | tenant-instance-id | `sha256:eaf6312fa1f2` | `docs/ops/evidence/2026-11-20-finance/make-check.txt` |
| 6 | servury-account | `sha256:716084d53ac1` | `docs/ops/evidence/2026-11-20-finance/README.md` |
| 1 | servury-account | `sha256:716084d53ac1` | `docs/ops/evidence/2026-11-20-finance/reconcile-week.txt` |
| 2 | servury-account | `sha256:716084d53ac1` | `docs/ops/evidence/2026-11-20-finance/reconcile_week.py` |
| 1 | checkout-path | `sha256:18b74e4c8911` | `docs/ops/evidence/2026-11-20-finance/reconcile_week.py` |
| 8 | servury-account | `sha256:716084d53ac1` | `docs/ops/evidence/2026-11-20-finance/servury-read-2026-11-20.json` |
| 2 | host-box-id | `sha256:421fe1d07ee8` | `docs/ops/evidence/2026-11-20-finance/servury-read-2026-11-20.json` |
| 2 | provider-ipv6 | `sha256:59994270da6b` | `docs/ops/evidence/2026-11-20-finance/servury-read-2026-11-20.json` |
| 1 | hq-address | `sha256:9876939e2536` | `docs/ops/evidence/2026-11-20-finance/servury-read-2026-11-20.json` |

14 pairs, 8 digests, one directory tree each. The same clone with the 14 rows:

```
allowlist:       170 rows, 4 credentials, 14 debt
leak scan: history
  commits scanned: 850 (git rev-list --all)
  unique blobs:    2,191
  bytes scanned:  172,976,386
  allowlisted:    3835
PASS: nothing to leak in 850 commits (2,191 unique blobs), 172,976,386 bytes scanned
```

`3754 + 81 = 3835`: the rows waive exactly the findings, nothing else. The
heads moved while this card ran: the local `main` at handoff is `258438c` (859
commits, 2,208 unique blobs), the same scan with the rows is **PASS**
(`allowlisted: 3847`), and a `--no-ff` merge of this card into that main — 861
commits, 2,210 blobs — is **PASS** as well. The failing runs the card names
reproduce: run `45095519414` (job `137434343999`, step "Gate every blob with
the tree's own deny list") is red at `8c2e912`, and the same job is red at the
later `origin/main` `db97100` (run `45100645588`, job `137450465739`).

The directories are dead on every live ref (`git ls-tree -r <ref> -- docs/ops` → 0
lines at `origin/main`, `main` and `HEAD`), and all four carrier commits are
ancestors of `origin/main`, so the disclosure predates the waiver: these are
already-public values, not a new publication.

## 2. The rows

The 14 rows are the same 14 `(path, digest)` pairs that `7ce0f2b` deleted when
the operating record left the tree, restored with `class=debt`. Each is one value
at one exact path; the path and the digest are the pin, so another value there,
or this value at another path, is a finding.

| path | label | digest |
|---|---|---|
| `docs/ops/evidence/2026-11-19-growth/make-check.txt` | servury-box-id | `sha256:3e5275784e44` |
| `docs/ops/evidence/2026-11-19-growth/make-check.txt` | servury-dev-account | `sha256:4014bb9b4e56` |
| `docs/ops/evidence/2026-11-19-growth/make-check.txt` | tenant-instance-id | `sha256:eaf6312fa1f2` |
| `docs/ops/evidence/2026-11-20-finance/README.md` | servury-account | `sha256:716084d53ac1` |
| `docs/ops/evidence/2026-11-20-finance/make-check.txt` | servury-box-id | `sha256:3e5275784e44` |
| `docs/ops/evidence/2026-11-20-finance/make-check.txt` | servury-dev-account | `sha256:4014bb9b4e56` |
| `docs/ops/evidence/2026-11-20-finance/make-check.txt` | tenant-instance-id | `sha256:eaf6312fa1f2` |
| `docs/ops/evidence/2026-11-20-finance/reconcile-week.txt` | servury-account | `sha256:716084d53ac1` |
| `docs/ops/evidence/2026-11-20-finance/reconcile_week.py` | checkout-path | `sha256:18b74e4c8911` |
| `docs/ops/evidence/2026-11-20-finance/reconcile_week.py` | servury-account | `sha256:716084d53ac1` |
| `docs/ops/evidence/2026-11-20-finance/servury-read-2026-11-20.json` | host-box-id | `sha256:421fe1d07ee8` |
| `docs/ops/evidence/2026-11-20-finance/servury-read-2026-11-20.json` | provider-ipv6 | `sha256:59994270da6b` |
| `docs/ops/evidence/2026-11-20-finance/servury-read-2026-11-20.json` | servury-account | `sha256:716084d53ac1` |
| `docs/ops/evidence/2026-11-20-finance/servury-read-2026-11-20.json` | hq-address | `sha256:9876939e2536` |

Every row carries `debt`, `credential=no`, a reason that names the deny-list
label and cites `t_3436baff` (purge) and `t_5b4fe62c` (the rewrite hold), and
`expires 2026-12-31`. No reason quotes the value it waives, so no row is
self-satisfying. The rows add no literal: the diff passes the scanner's own
pre-push shape

```
$ git diff main...HEAD | python3 tools/leak_scan.py \
    --stdin-patch --label ODL-945 --forbid-file docs/ops/leak-forbid.txt
PASS: nothing forbidden in ODL-945
```

## 3. The live-tree gates are unchanged

The tree scan with the rows, on the same CI-shaped clone: same 407 files and
same 456 allowed values as before — PASS. The only byte difference is the
allowlist's own growth (`14,132,864` → `14,139,451` bytes scanned).

A plant in a live path, in this card's worktree:

```
$ printf 'planted box id: <the box id>\n' > web/leak-probe.txt
$ python3 tools/leak_scan.py --tree --forbid-file docs/ops/leak-forbid.txt
FINDING forbidden-value        web/leak-probe.txt:1 sha256:3e5275784e44 len=8  (servury-box-id)
FAIL: 1 finding(s) in the tree
$ rm web/leak-probe.txt && python3 tools/leak_scan.py --tree ...
PASS: nothing to leak in 409 files, 14,251,069 bytes scanned
```

The plant was never committed or pushed. `leak-allow.tsv` is the only modified
path in the worktree.

## 4. The waiver is narrower than a glob — three planted negatives

In a throwaway two-commit repo carrying this card's allowlist, three literals
were planted in one commit:

```
$ python3 tools/leak_scan.py --history --forbid-only
FINDING forbidden-value  commit <head> docs/ops/evidence/2026-11-19-growth/make-check.txt:1 sha256:0cca50f512f3 len=8  (tenant-instance-id)
FINDING forbidden-value  commit <head> docs/ops/evidence/2026-11-19-growth/notes.txt:1        sha256:3e5275784e44 len=8  (servury-box-id)
FINDING forbidden-value  commit <head> web/leak-probe.txt:1                                  sha256:3e5275784e44 len=8  (servury-box-id)
FAIL: 3 finding(s) in the history
```

- a **different** tenant-instance-id at a pinned dead path → a finding (the
  digest pins the value);
- the **same** box id in a **new file** inside the dead directory → a finding
  (the path pins the file);
- the **same** box id in a live path → a finding.

## Why 14 path rows, not the card's 11 directory globs

The card offers 11 rows, one per `(directory glob, value digest, label)` pair.
This lands 14, one per `(exact path, value digest, label)` pair, for three
reasons:

1. `leak-allow.tsv`'s own rule, stated in its header and in the ODL-911 section,
   is **one value at one path, never a glob** — "Do not widen a row to a
   directory glob."
2. These are the same `(path, digest)` pairs `7ce0f2b` deleted; restoring them
   is the smallest diff that clears the gate, and it keeps the history rows
   shaped like every other published-history row in the file.
3. A directory glob waives the same value in a *new* file under the dead
   directory. The probe above shows the path rows do not: a new
   `notes.txt` carrying the box id fails. Narrower is strictly stronger, and
   the acceptance criteria ask for a gate that still catches a new literal.

The card's 11 rows and these 14 waive the same 81 findings and the same eight
digests; no finding needs a glob.

## The expiry, and why 2026-12-31

`t_3436baff` is event-gated, not dated: `t_5b4fe62c` lifts only when NEAR
announces the hackathon results **and** Savio confirms the judges have finished;
judging opened 2026-11-16 and Savio reports NEAR said it takes weeks, finish
unknown. A debt with no date is a permanent exception, so Security sets the
ceiling rather than invent a schedule: **2026-12-31**, about six weeks after
judging opened and the longest "weeks" window stated. If the purge has not run by
then, the first push after the date reds `history-secrets` with a `stale-allow`
finding — that is the alarm, not a new leak. Extending it is a new row with a
dated reason; a silent date bump is the anti-pattern.

## Residual risk — named, bounded, already Savio's

- **Already public, unchanged.** The 81 values are in the published history of
  `Odile-AI/odile` and answer by SHA until `t_3436baff` purges them. This card
  changes the gate's colour, not the disclosure: it publishes nothing new and
  revokes nothing.
- **The blind spot is exactly 14 `(path, value)` pairs — 8 values over 6
  paths.** A future re-add of one of those values at one of those paths is
  waived by design; anything else reds.
- **The hold is Savio's.** The blobs stay fetchable because of his 2026-11-22
  direction (`t_5b4fe62c`), not because of this waiver, so this card accepts no
  new residual risk.
- **The run id.** Security does not merge or push; the green `history-secrets`
  run is produced by the merge that lands this card. The pre-merge proof is the
  CI-shaped clone above: same scanner, same allowlist, same deny list, 850
  commits, 2,191 blobs.

## Evidence commands

```sh
# the CI shape
git init <clone> && git -C <clone> remote add origin <checkout>
git -C <clone> fetch origin origin/main:refs/heads/main
python3 tools/leak_scan.py --history --forbid-file docs/ops/leak-forbid.txt \
  --forbid-only --root <clone>

# the waiver's shape
grep -c '^forbidden-value' leak-allow.tsv          # 108 (94 + 14)
python3 tools/leak_scan.py --tree --forbid-file docs/ops/leak-forbid.txt
```

**Tests:** `python3 -m unittest tests.test_leak_scan tests.test_forbid_gate
tests.test_leak_classes tests.test_public_tree_shape` → `Ran 167 tests ...
OK (skipped=1)`; `tools/claims_lint.py` → OK (20 files); `tools/brand_lint.py`
→ OK (23 surfaces).
