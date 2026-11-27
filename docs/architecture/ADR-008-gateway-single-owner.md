# ADR-008 — One gateway, and the systemd user unit is its only supervisor

**Status:** Accepted · 27 November 2026 · Architect · card `t_1d677e3b` (ODL-355)
**Answers:** ODL-355 — after a boot or `systemctl --user restart hermes-gateway`,
which process serves the host gateway; what a `hermes` CLI call does when the unit
is stopped; and what `ExecStart` must do when the host lock is held by another pid.
**Short answer:** the unit owns the gateway, and only the unit starts one. A
`hermes` CLI that needs a gateway starts `hermes-gateway.service` and waits for it;
it never spawns a detached gateway while a unit is installed. A supervised
`gateway run` — the process systemd started, not a CLI that inherited its
environment — **takes the host over** when a foreign pid holds the lock (drain
first, then claim) instead of attaching and exiting. `Restart=on-failure` plus the
base unit's `RestartForceExitStatus=75` and `RestartPreventExitStatus=78` is the
exit-code contract; the takeover removes the exit that made `Restart=always` write
135 `Scheduled restart job` rows. PIDFile adoption loses because the adopted pid
lives outside the unit's cgroup, where the unit's stop cannot reach it.
**Related:** `docs/ops/OPERATING.md` § “One gateway — the unit owns it”;
`docs/ops/evidence/2026-11-25-gateway-unit/README.md`; Hermes checkout
`edc8a222a8` (`hermes_cli/gateway.py`, `hermes_cli/gateway_runtime.py`,
`hermes_cli/gateway_runtime_service_identity.py`, `gateway/host_attach.py`,
`gateway/run_bootstrap.py`, `gateway/run_shutdown.py`, `gateway/session_kanban.py`,
`gateway/lifecycle_ledger.py`); cards ODL-355, `t_542ba140`, `t_436574a6`,
`t_e45873ef`, `t_8b331a57`.
**Decision owner:** Architect. This ADR adds no provider, network path, trust
boundary or standing credential, and it strengthens no sentence in
`docs/CLAIMS.md`: it makes an operating rule true on one host. Savio is not
escalated.

---

## 1. The invariant at risk

**The rule.** The board, every managed worker, every cron session and the
five-minute watchdog are clients of **one** gateway process on this host. That
process is a child of `hermes-gateway.service`. If the unit does not own it, then
`systemctl --user restart` stops nothing, the 420 s stop envelope and the 300 s
drain apply to nothing, and a gateway that dies leaves no supervisor to bring it
back. ODL-355 measured both halves of the breach: a gateway outside the unit
(PID 2259, launched from a console TTY at 03:11:22Z) and a unit that attached to
it, exited clean, and was restarted 135 times in 21 minutes.

The invariant, as sentences that can be falsified:

- **I1 — one owner.** At most one `gateway run` process serves the host, and it
  is the pid `systemctl --user show hermes-gateway -p MainPID` names.
- **I2 — every start ends owned.** After a boot, `systemctl --user start`, or
  `systemctl --user restart`, either I1 holds or the unit is parked (MainPID 0,
  a named reason in the journal) — never a restart loop.
- **I3 — the CLI asks, it does not spawn.** A `hermes` CLI call that needs a
  gateway and finds none starts the unit and waits; with a unit installed it
  never leaves a gateway outside it.
- **I4 — no storm.** A steady host writes 0 `Scheduled restart job` rows in any
  ten-minute window.

I2 is the strong form: it includes the state the previous design left open — the
unit stopped while a foreign pid serves (drill 2 C3). I3 is the form the parent
card's second Definition-of-Done bullet tests.

## 2. What was measured

All commands ran on this host on 2026-11-27; the Hermes checkout is
`edc8a222a8`. The drill rows are from
`docs/ops/evidence/2026-11-25-gateway-unit/README.md` and
`/root/.odile/evidence/ODL-355/drill-2.log`, whose hashes the parent card
verified.

| Fact | Measured | Source |
|---|---|---|
| The base unit ships `Restart=always`, `RestartSec=5`, `RestartForceExitStatus=75`, `SuccessExitStatus=75`, `RestartPreventExitStatus=78`, `KillMode=mixed`, `TimeoutStopSec=330`; the installed drop-ins add `TimeoutStopSec=420`, `OOMPolicy=continue`, `Restart=on-failure`. `MainPID=3047799`, `NRestarts=0`, one `gateway run` pid. | `systemctl --user show` and `pgrep -af '[g]ateway run'` | this host |
| Under the unit's environment (`INVOCATION_ID` set), a second `gateway run` attaches and exits **75**: `✓ The host gateway already serves profile 'default' — nothing to start.` | `hermes -p default gateway run --quiet` → rc 75 | this host |
| From a plain shell (no `INVOCATION_ID`), the same attach exits **0**. | `env -u INVOCATION_ID … gateway run --quiet` → rc 0 | this host |
| `Restart=on-failure` does **not** stop an exit-75 loop: `RestartForceExitStatus=75` forces the restart. | throwaway unit, exit 75 every start → `NRestarts=5` in 6 s | this host |
| `RestartPreventExitStatus=75` **does** stop it, and parks every exit 75 — including the drain-aware relaunch. | same unit with both lists → `ActiveState=failed`, `NRestarts=0` | this host |
| The stray state still loops with `restart.conf` installed: unit start attaches, exits 75, restarts every ~9 s; `MainPID=0`, state `activating`. | drill 2 C3: **11** restart rows in 10 min | drill-2.log:100–109 |
| With the unit stopped and no owner, a CLI call starts the unit and the unit owns the result: `ensure` → `state: ready`, then `MainPID=2874849` serving. | drill 2 A2 | drill-2.log:78–85 |
| The CLI's start path is already the unit: discovery returns `ExistingService(backend='systemd', start_argv=('systemctl','--user','--no-ask-password','start','hermes-gateway.service'))`; `next_start_action(absent, installed_service=True)` is `start-service`, and `spawn-unmanaged` only when no service is installed. | read-only probe against the checkout | this host |
| `hermes gateway ensure` refuses a unit whose `ExecStart` is not `gateway run` plus only `--quiet` or `--external-supervisor`, and refuses any `ExecStartPre`: `service_identity_unverified`, exit 7, whenever the gateway is not already answering. | drill A (2026-11-25) and `verify_gateway_argv` / `verify_systemd` | drill-2 evidence; identity module |
| Linger is on (`Linger=yes`), the unit is `enabled`, `WantedBy=default.target`; there is one `hermes-gateway.service` and no profile unit. | `loginctl show-user root -p Linger`; `systemctl --user list-unit-files` | this host |
| The 03:41:00–04:02:13 storm: 135 `Scheduled restart job` rows, each naming PID 2259; the pulse run was recorded `interrupted` at 04:08:18. | parent evidence dossier | `docs/ops/evidence/2026-11-25-gateway-unit/README.md` |

Two consequences drive the decision. First, **exit 75 is the loop**: the
supervised attach path returns 75, and the unit's own `RestartForceExitStatus=75`
turns that into a restart every `RestartSec`, whatever `Restart=` says. Second,
**the unit's command may not change**: every wrapper, `--replace`, or
`ExecStartPre` that would let the unit adopt or retire a stray makes the CLI's
own identity check refuse the unit, so the CLI cannot start it at all.

## 3. The decision

### 3.1 The owner is the unit's own process

`systemctl --user start` and `systemctl --user restart` are the only ways a
gateway starts on this host. `hermes` CLI calls and client entrypoints start the
unit; a gateway process is never spawned beside it while a unit is installed.

The unit keeps `Type=simple` and `ExecStart=… hermes gateway run` exactly as
Hermes generates it. The new behaviour is in the start path, gated on the caller
being the process systemd started: `systemctl --user show hermes-gateway -p
MainPID` names this pid (a fallback of `INVOCATION_ID` plus a parent whose comm is
`systemd` covers the first millisecond). A worker or cron session that inherited
the unit's environment is not the MainPID, so a stray `gateway run` typed in a
session keeps the old attach semantics.

### 3.2 The CLI asks systemd (option a); it does not leave a pid to adopt (option b)

When a `hermes` CLI call needs a gateway and none serves, the CLI runs the
installed unit's start command and waits for readiness — `start-service`, not
`spawn-unmanaged`. This is already the upstream path
(`gateway_runtime.ensure_gateway_runtime` → `start_existing_gateway_service`),
measured in drill 2 A2, and this ADR pins it: with a unit installed, there is no
fallback to an unmanaged spawn. The unmanaged spawn survives only for hosts with
no service manager (Termux, a bare checkout), which is not this host.

The alternative — the CLI starts a gateway and the unit adopts the already-serving
pid through `PIDFile=` — loses for five mechanical reasons:

1. **The adopted pid is outside the unit's cgroup.** `KillMode=mixed` sends
   SIGTERM to the main process but SIGKILL to the control group
   (`systemd.kill(5)`), so a gateway that ignores SIGTERM survives its own unit
   stop and the next start makes two.
2. **The unit loses accounting and OOM policy for the one process that matters.**
   `Memory=`, `Tasks=`, `CPU=` and `OOMPolicy=continue` (ODL-335) all describe a
   cgroup the adopted pid is not in.
3. **`systemctl status` and `is-active` would claim a supervisor that does not
   exist.** Journal attribution goes too: the adopted process keeps the stdout it
   was started with, not the unit's `StandardOutput=journal`.
4. **It changes the normal start path.** `Type=forking` requires `gateway run` to
   daemonize; the no-owner start forks, the stray-owner start does not, and the
   process is two different shapes depending on the state.
5. **It is invisible to the CLI's own check.** Hermes' identity verifier reads
   `ExecStart`, `Environment`, `ExecStartPre` and friends; it does not read
   `Type=` or `PIDFile=`. The CLI would report a supervised unit whose supervisor
   is a pointer.

### 3.3 When the lock is held: a supervised start takes over, it does not attach

A supervised `gateway run` that finds a live host gateway serving this profile
does what `--replace` does — but **without the flag**, so the unit's command
stays inside the identity allowlist:

1. write the takeover marker for the foreign pid (so it exits 0, not 1);
2. SIGTERM it and let its own drain run — up to `agent.restart_drain_timeout`
   plus the 30 s teardown envelope (330 s), clamped below `TimeoutStopSec=420`;
3. if it is still alive, SIGKILL, then reap its children and remove its pid file;
4. claim the host lock and serve.

The new process is the unit's MainPID from the moment systemd forks it; it
becomes the serving pid when the handover completes. Clients in the window see
the old owner `ready` (it still serves in-flight turns) and the new one
`starting`; `ensure` returns ready once either serves. A handover that cannot
complete — the target cannot be proven ours, or survives SIGKILL — exits **78**
with a `gateway_takeover_refused` log line, so the unit parks (MainPID 0, one
gateway still serving) instead of looping.

This is one layer of two. The other is the exit code: a supervised **ATTACH**
(no takeover applied, e.g. an older checkout) exits **0** and parks the unit
under `Restart=on-failure`, not 75. Only a transient refusal (an owner that has
not answered yet) and a lost host-lock race keep 75. A partial or stale patch
therefore still cannot produce the 135-row storm: it parks instead.

### 3.4 The exit-code contract and the unit directives

| Exit | Meaning | Unit reaction |
|---|---|---|
| 0 | clean: operator stop, idle exit, nothing to do (attach) | parks under `Restart=on-failure` |
| 1 | crash or startup failure | relaunch after `RestartSec=5` |
| 75 | transient: drain-aware relaunch, lost host-lock race, liveness watchdog | `RestartForceExitStatus=75` relaunches after 5 s |
| 78 | permanent: config refusal, takeover refused | `RestartPreventExitStatus=78` parks the unit |

The exact unit directives to change — one, and nothing else:

| Directive | Base unit | Decision | Where |
|---|---|---|---|
| `Restart` | `always` | **`on-failure`** | drop-in `deploy/hermes-gateway/restart.conf` |
| `ExecStart` | `…/hermes gateway run` | unchanged (identity allowlist) | base unit, Hermes-generated |
| `RestartSec` | `5` | unchanged | base unit |
| `RestartForceExitStatus` | `75` | unchanged | base unit |
| `SuccessExitStatus` | `75` | unchanged | base unit |
| `RestartPreventExitStatus` | `78` | unchanged; the takeover refusal uses it | base unit |
| `StartLimitIntervalSec` | `0` | unchanged — the no-storm guarantee is the takeover and the park, not rate limiting | base unit |
| `Type` | `simple` | unchanged — no `PIDFile`, no `forking` | base unit |
| `KillMode` / `KillSignal` | `mixed` / `SIGTERM` | unchanged — SIGTERM reaches the MainPID, the drain runs | base unit |
| `TimeoutStopSec` | `330` → `420` | unchanged | `drain.conf` |
| `OOMPolicy` | `continue` | unchanged | `oom.conf` |
| `ExecStop` / `ExecStopPost` | stop mark / cgroup cleanup | unchanged | base unit |

Linger (`loginctl enable-linger root`, measured `Linger=yes`) is the boot half:
the user manager, and so the unit, exists before any CLI can start a gateway. It
is a host setting, not a directive, and the boot check in `OPERATING.md` proves
it.

### 3.5 The drain and takeover protocol

A gateway passes through four observable states: `absent`, `starting`, `ready`,
`draining`, then gone (`gateway/runtime_contract.py`). A restart's after-turn
wait is `draining` with `drain_reason=restart_wait` and remains attachable; a
stop drain is `draining` with no reason and is not.

- **On SIGTERM or takeover**, the gateway stops accepting new turns
  (`runtime_draining`), keeps in-flight turns running, and drains for up to
  `agent.restart_drain_timeout` (300 s, `config.yaml`). New work is refused, not
  queued.
- **A client** that races the drain gets `Gateway draining: not_ready`
  (`hermes_cli/gateway_client.py`), attaches again when the next owner is ready,
  or waits out `restart_wait` to answer a parked approval.
- **An in-flight card run** that cannot finish is closed by its owner with an
  explicit `worker_lost` event (`gateway/session_kanban.py::record_worker_loss`),
  not left owned. The board sweep books the run (`stale_lock=… [managed worker
  lost: …]`), returns the card to ready — or to review when the run carried
  `retry_status: review` — and resets `consecutive_failures`. No human step.
- **A cron session** inside the envelope completes. One cut off by the drain
  timeout is marked `interrupted` by the escalation, so the envelope must cover
  the longest scheduled job; the check is the before/after `hermes cron history`
  line.
- **The lifecycle ledger** is the forensic proof: the single graceful-exit funnel
  calls `mark_exited` (`gateway/run_shutdown.py` →
  `gateway/lifecycle_ledger.py`), so `state/gateway.lifecycle.json` reads
  `phase=exited` and the next boot writes no `gateway.previous_unclean_exit`
  line for that pid. A takeover writes the same clean exit on the old owner.
- **The ledger line and the run outcome are the contract**: a planned restart
  leaves a clean previous life and a reclaim, never `exited UNCLEANLY (no exit
  path ran)`.

## 4. Behaviour matrix

`R` = unit active and owns; `S` = unit stopped, no serving gateway; `F` = unit
stopped, a foreign pid holds the host lock and serves. “CLI” is any `hermes`
client entrypoint that needs a gateway (chat, TUI, cron, `gateway ensure`).

| State | CLI call | `systemctl --user start` | `systemctl --user restart` |
|---|---|---|---|
| **R** | discovery is `ready`; attaches; no start; exit 0 | no-op (already active); MainPID unchanged | planned-stop marker, drain ≤ 300 s inside `TimeoutStopSec=420`, new ExecStart owns; 0 restart rows |
| **S** | `start-service` → `systemctl … start`; waits; `ready`; MainPID owns; 0 rows | starts; claims lock; MainPID owns; 0 rows | stop is a no-op, then start; MainPID owns; 0 rows |
| **F** | discovery is `ready` (the foreign owner serves); attaches; **creates no second gateway**; the unit stays inactive until an explicit start | takeover: marker, SIGTERM, drain ≤ 330 s, SIGKILL if needed, claim; MainPID owns after the handover; 0 rows | same as start (inactive stop is a no-op); takeover; MainPID owns; 0 rows |

Two notes the matrix does not show. In `F`, an ordinary client call does **not**
restart a unit an operator stopped — the repair is an explicit start (or the boot
check). In `F`, a takeover that cannot complete exits 78: the unit reads `failed`,
MainPID is 0, the foreign gateway still serves, and the journal names
`gateway_takeover_refused`; no loop.

## 5. Alternatives rejected

| Alternative | Why it loses |
|---|---|
| `Restart=always` kept, attach path unchanged | the measured 135-row storm; `RestartForceExitStatus=75` restarts even under `on-failure` |
| `RestartPreventExitStatus=75` added | parks the drain-aware relaunch too (`NRestarts=0`, `failed`), so a self-restart leaves the host without a gateway |
| `--replace` in `ExecStart` (the first ODL-355 attempt) | `service_identity_unverified`, exit 7, whenever the gateway is not answering; the CLI then cannot start the unit (drill A). Upstream retired its own `20-replace.conf` for the same reason |
| `ExecStartPre=hermes gateway stop` | the identity verifier rejects any `ExecStartPre`; same exit 7 |
| `PIDFile=` / `Type=forking` adoption | §3.2: outside the cgroup, no accounting, SIGKILL misses it, invisible to the CLI check |
| A blocking `ExecStart` that waits for the lock | MainPID would name a waiter, not the serving gateway; `pgrep -af 'gateway run'` would show two; `is-active` would claim a service the host does not have |
| A timer that starts the unit every N minutes | a second supervisor, and it papers over ownership instead of deciding it |
| Park-only (ATTACH exits 0, no takeover) | stops the storm but leaves I2 false in state `F`; kept as the fallback layer inside §3.3, not as the decision |

## 6. Deployment, reversibility, and the post-update check

The behaviour change is a patch to the Hermes checkout, following the
`t_0284f0cf` practice: edit the checkout, save the diff as a tracked patch
artifact, and re-apply it after an update if the autostash conflicts.

| Artifact | Owner card | Touches |
|---|---|---|
| `deploy/hermes-gateway/restart.conf` | `t_542ba140` | the one unit directive |
| `deploy/hermes-gateway/patches/0001-supervised-takeover.patch` | `t_542ba140` | `hermes_cli/gateway.py` (takeover hook, ATTACH exit 0), `gateway/run_bootstrap.py` (330 s takeover wait) |
| `deploy/hermes-gateway/patches/0002-drain-and-reclaim.patch` | `t_436574a6` | `gateway/run_shutdown.py`, `gateway/session_managed_worker.py`, `gateway/session_kanban.py` — the reclaim and cron contract of §3.5 |
| `deploy/hermes-gateway/apply-patches.sh` | `t_542ba140` | applies `patches/*.patch` in name order after `git apply --check`; `--reverse` reverts; idempotent |
| `deploy/hermes-gateway/check-takeover.py` | `t_542ba140` | exit 1 when the hook or the exit contract is absent from the checkout |

The two patches touch disjoint files, so the cards run in parallel. Apply order
is name order; `0001` is safe alone (the takeover drains through the handler the
gateway already has), and `0002` makes the in-flight run and cron outcomes of
§3.5 explicit, so it can land before or after it. `hermes update` autostashes and
restores local changes; after every update, run `check-takeover.py` and
`systemctl --user show hermes-gateway -p ExecStart` (still `… gateway run` with
no flag), then `hermes gateway ensure --json` (`state: ready`).

**Reversibility.** Two-way. Remove `restart.conf` and `systemctl --user
daemon-reload` restores `Restart=always`; `apply-patches.sh --reverse` plus a
unit restart restores the attach behaviour. The unit file itself is never edited
(Hermes rewrites it on update; drop-ins survive, the same rule as `drain.conf`
and `oom.conf`). The decision stops being cheap only if a future Hermes release
changes `_attach_to_host_gateway_or_guard` or the identity allowlist; the patch
artifact and `check-takeover.py` name exactly what to re-verify.

## 7. Definition of Done mapped to runnable checks

| DoD bullet | Check | Pass |
|---|---|---|
| One gateway, unit owns it, no storm after boot or restart | `systemctl --user restart hermes-gateway`; then `mp=$(systemctl --user show hermes-gateway -p MainPID --value)`; `pgrep -af '[g]ateway run'`; `journalctl --user -u hermes-gateway --since '-10 min'` | `mp` is non-zero, exactly one `gateway run` line and its pid equals `mp`, 0 `Scheduled restart job` rows, `NRestarts=0` |
| A CLI call while the unit is stopped leaves the unit owning | `systemctl --user stop hermes-gateway`; `hermes gateway ensure --json`; re-read `MainPID` and `pgrep -af '[g]ateway run'` | `state: ready`, `endpoint.supervisor: systemd`, one line, `MainPID` equals its pid |
| The foreign-pid state is repaired by a start | stop the unit; start a console `gateway run` in a scope outside the unit; `systemctl --user start hermes-gateway`; re-read `MainPID` and `pgrep -af '[g]ateway run'` | one line, `MainPID` equals its pid, the foreign pid gone, 0 rows; or exit 78 with `gateway_takeover_refused` and one gateway still serving |
| Restart with a card running is graceful | run a drill card whose worker sleeps; `systemctl --user restart hermes-gateway`; `hermes kanban show <task>`; before/after `hermes cron history <pulse>`; `state/gateway.lifecycle.json` | the run ends `worker_lost` and the card is re-claimed, the pulse line reads `completed` (not `interrupted`), `phase=exited`, no `gateway.previous_unclean_exit` for that pid |
| The unit is the command the CLI can verify | `systemctl --user show hermes-gateway -p Restart -p RestartForceExitStatus -p RestartPreventExitStatus -p ExecStart -p TimeoutStopUSec -p OOMPolicy -p KillMode`; `python3 deploy/hermes-gateway/check-takeover.py` | `Restart=on-failure`, `75`, `78`, `ExecStart=… gateway run`, `7min`, `continue`, `mixed`; the checker exits 0 |
| OPERATING.md records the rule | `grep -n 'systemctl --user show hermes-gateway -p MainPID' docs/ops/OPERATING.md`; `grep -n 'systemctl --user restart hermes-gateway' docs/ops/OPERATING.md`; `grep -n 'hermes gateway ensure' docs/ops/OPERATING.md` | the three commands are present with their expected output, and the page names the takeover and the park fallback |

The drills restart the gateway, so the driver runs from a transient
`systemd-run --user --scope` outside the unit's cgroup (the ODL-355 drill's
shape: the driver must survive the restart it performs), through
`tools/restart_window.py` or inside the 03:00–03:30Z window; the evidence quotes
the gate's verdict.

## 8. What the implementation cards may not re-decide

- **One unit, one gateway, all profiles.** `hermes-gateway.service` is the only
  gateway unit; every profile is served by its multiplexer. A second profile unit
  is out of scope. So is an owner that answers `multiplex: False`: that is the
  pre-migration per-profile fleet, where Hermes starts beside it by design; fold
  it in once with `hermes gateway migrate --multiplex`. I1 is stated for the
  multiplex host this ADR governs.
- **The unit's command does not change.** `ExecStart` stays `… gateway run`; no
  `--replace`, no wrapper, no `ExecStartPre`, no `PIDFile`, no `Type=forking`.
- **The takeover is in-process, not in the unit.** A supervised start takes over;
  the unit file names none of it.
- **Exit codes keep their meaning.** 0 clean, 1 crash, 75 transient, 78
  permanent. No `RestartPreventExitStatus=75`, no `Restart=no`, no start limit.
- **The two patches are disjoint.** `0001` owns `hermes_cli/gateway.py` and
  `gateway/run_bootstrap.py`; `0002` owns the drain and reclaim modules. Neither
  edits the other's files.

## 9. Consequences and risks

- A unit start is now authoritative: `systemctl start` on a host with a console
  gateway drains it. That is the repair the foreign-pid state needs; the drain
  envelope bounds it.
- The takeover can outlive a client's 60 s `ensure` deadline while the old owner
  finishes a long turn. The client retries and joins the same start; systemd
  serialises the unit and the host lock arbitrates the race, so no second
  gateway is spawned.
- The patch is a local Hermes modification. It is tracked, checkable and
  reversible, and `hermes update` autostashes it; a conflicting update is
  caught by `check-takeover.py`, not by a silent revert to the storm.
- The foreign-pid state is still reachable by a deliberate console `gateway run`.
  Under this ADR it is self-healing on the next start, and the boot check names
  the state (`MainPID=0`) if nobody starts the unit.
- No tenant claim moves: every tenant already runs one gateway under one unit;
  this makes the same rule true on HQ.

## 10. What pins it

- `tests/test_gateway_unit_dropins.py` — `restart.conf` sets `Restart=on-failure`;
  no drop-in sets `ExecStart`, `ExecStartPre`, `PIDFile`, `Type` or the identity
  directives; the patch files exist and touch only §6's paths.
- `deploy/hermes-gateway/check-takeover.py` — the hook and the exit contract are
  present in the checkout (skipped when the checkout is absent, so a public clone
  stays green).
- The drill transcripts in `docs/ops/evidence/2026-11-25-gateway-unit/` — the
  three states of §4, with `systemctl show`, `pgrep`, the journal rows, the
  ledger line and the cron history.
- `docs/ops/OPERATING.md` § “One gateway — the unit owns it” — the operator
  commands and the expected output of §7.
