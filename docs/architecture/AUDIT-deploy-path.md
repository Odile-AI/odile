# Audit — the HQ deploy path, factors III, IV, VI and IX

**Date:** 2026-11-16 · **Seat:** architect · **Unit:** A2 (primary source: *The
Twelve-Factor App*, https://12factor.net/)
**Path audited:** `./deploy.sh` → `rsync -a --delete` → `/opt/odile` →
`odile.service` plus the four timers the deploy installs (attest-refresh, claim,
roadmap, seed). **Tree:** `d1a0bdd`, deployed tree from `8caee57`
(`/var/lib/odile/roadmap-source`).

The method is one command per factor against the live host, then one finding
narrow enough to fix in a line and prove with a rerun.

## 1. The four factors

| Factor | What it asks | Command | Observed |
|---|---|---|---|
| III. Config | Everything that varies between deploys lives in the environment | `systemctl show odile -p Environment` | Four knobs set (`ODILE_PORT`, `ODILE_BIND`, `ODILE_DB`, `ODILE_ATTEST`); `app.py` reads nine (`:51`–`:85`, `:495`, `:549`), the other five default to the production value. No credential is in the tree — the litmus test passes. |
| IV. Backing services | Every service consumed is an attached resource, named in config | `systemctl show odile-roadmap.service -p Environment`; `grep -n ".hermes" tools/roadmap_page.py` | The waitlist DB is attached (`ODILE_DB`). The board database the roadmap generator consumes is **not**: `tools/roadmap_page.py:433` builds `~/.hermes/kanban/boards/odile/kanban.db` from `Path.home()`, and the unit carries no `Environment=`. |
| VI. Processes | Processes are stateless; persistent data is in a backing service | `grep -n "open(.*'w'" app.py`; `ls -l /var/lib/odile` | Stateless: the only write handle in `app.py` is an in-memory `tarfile` (`:689`); all state is under `/var/lib/odile`. One piece of state is not in a service at all: the deployed tree has no `.git`, so its identity is written into `/var/lib/odile/roadmap-source` by `deploy.sh:175`. |
| IX. Disposability | Fast start, graceful stop on SIGTERM | `sh docs/ops/evidence/2026-11-16-architect/disposability.txt` | Start 19 ms, SIGTERM exit 8 ms, rc 143. `app.py` registers no signal handler and sets `daemon_threads = True` (`:1155`), so a POST in flight is cut rather than drained. Acceptable for a waitlist; it is not written down anywhere. |

## 2. Finding — a backing service the sandbox forbids, failing twelve times in a row

`odile-roadmap.service` has failed every run since 03:45Z:

```
Nov 16 03:45:01 python3[2546763]: roadmap: board db /root/.hermes/kanban/boards/odile/kanban.db: unable to open database file
Nov 16 03:45:01 systemd[1]: odile-roadmap.service: Failed with result 'exit-code'.
```

Twelve consecutive failures by 06:30Z and a thirteenth at 06:45Z
(`journalctl -u odile-roadmap --since '2026-11-16 03:30' | grep -c "unable to
open database file"` → 13). The page at `/var/lib/odile/roadmap.html` still has
mtime **03:30**, so it has been serving a snapshot more than three hours old
with a `generated …` footer nobody reads.

The cause is not the path and not the SQLite flags on their own; it is the pair.
The board database is in WAL mode (`PRAGMA journal_mode` → `wal`), and a
`mode=ro` handle on a WAL database still creates `<db>-shm` and `<db>-wal`
beside the file. The unit runs with `ProtectHome=read-only`, so it can read
`/root/.hermes/...` but cannot create those two files. SQLite reports that as
`SQLITE_CANTOPEN` — "unable to open database file".

Four cases on a copy of the live board (`probe_wal_ro.sh`, transcript in
`wal-readonly.txt`), each run inside the unit's own sandbox properties:

| Case | Directory | URI | Result |
|---|---|---|---|
| A | read-only, no side files | `mode=ro` | **FAIL** `unable to open database file` — the unit's error |
| B | read-only, no side files | `mode=ro&immutable=1` | ok, `journal_mode=delete`, 388 rows |
| C | writable (`ReadWritePaths`), no side files | `mode=ro` | ok, `journal_mode=wal`, **2 side files created** |
| D | read-only, side files already present | `mode=ro` | ok — the failure is masked |

Case C is the sentence the playbook needs: a read-only handle wrote two files.
Case D is why the defect hid: any live Hermes writer, or any other reader that
touched the board first, leaves the side files behind and the timer passes. The
03:30 run wrote the page and the 03:45 run failed, with nothing changed on the
path but the side files; a read connection of my own created them at 06:37 and
they were gone by 06:41, which is the same window the unit hit at 06:45.

**Fix, one line, not applied here:** add
`ReadWritePaths=/root/.hermes/kanban/boards/odile` to
`deploy/roadmap/odile-roadmap.service`. `immutable=1` also works (case B) but
tells SQLite the file cannot change, so a page generated while the board is
being written would ignore committed WAL frames; it is the wrong trade for a
page whose only job is to be current.

## 3. Blast radius

`/roadmap` is unlisted and generated; nothing reads it. No tenant, claim, price
or DNS path is involved. The failure mode is stale bytes plus a red unit, and
the red unit is only visible in `systemctl --failed` and the journal — which is
how it went unnoticed for three hours.

## 4. Also observed

- **The release is not the codebase.** `deploy.sh` ships `docs/ops/**`,
  `docs/evidence/**`, `docs/brand/**` and `docs/craft/**` into `/opt/odile`
  although `.git/info/exclude` calls them host-only: the dry-run would write ten
  such files now, including `docs/ops/finance/WEEKLY-2026-11-16.md`
  (`deploy-ships-ignored.txt`). Contained today — `/opt/odile` is `0750
  root:odile` and group `odile` has one member — but a second runtime root
  would carry the ledger.
- **Three per-deploy hostnames are constants.** `app.py:93,100,101`
  (`DEMO_LOGIN_URL`, `DEMO_HOST`, `DEMO_VERIFY_URL`) cannot vary by deploy
  without a code change. Fine while HQ serves one demo; it is the exception that
  the unit's `Environment=` does not cover.
- **A second backing service with no precondition.** `odile-claim.service`
  reads `/root/.odile/config.yaml` (`odilectl/config.py:38`) and its only
  `ConditionPathExists=` is the queue file. On a rebuilt HQ the timer is green
  and the worker fails each tick, at the exec, with nothing in the unit naming
  the missing resource.

## What I did not change

`deploy.sh`, `odile.service`, `odile-roadmap.service` and `app.py` are
untouched: the fix belongs to a card, not to an audit. The proposal is the
one-line `ReadWritePaths` above, with a rerun of the unit as its evidence.

## Evidence

- `docs/ops/evidence/2026-11-16-architect/probe_wal_ro.sh` — the four cases, reproducible from the live board.
- `docs/ops/evidence/2026-11-16-architect/wal-readonly.txt` — its transcript.
- `docs/ops/evidence/2026-11-16-architect/disposability.txt` — start/stop timings.
- `docs/ops/evidence/2026-11-16-architect/deploy-ships-ignored.txt` — the rsync itemize for the host-only paths.
