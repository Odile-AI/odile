# Deploying Odile to HQ

The live service runs from `/opt/odile` as the `odile` user; the database lives
outside the repo at `/var/lib/odile/waitlist.db`. `./scripts/deploy.sh` rsyncs the
checkout to `/opt/odile` with `--delete`, installs the tree itself as
`root:odile 0750`, installs `odile.service` plus the attest-refresh and
claim-worker unit pairs, and restarts the service. On start, `app.py` calls
`init_db()`, so the schema migration runs in place.

**Production is main-only.** Before the first write, `./scripts/deploy.sh` proves the
checkout's HEAD contains `main` (`git merge-base --is-ancestor main HEAD`) and
prints the verdict — `→ guard   HEAD <sha> contains main <sha>`. A checkout
that does not contain main exits 1 with one line naming the missing commits and
writes nothing. The guard exists because on 2026-11-11 two deploys from
pre-merge branch trees replaced `/opt/odile` with a tree that had no `/roadmap`
route, and the page answered 404 until a main deploy. A branch that contains
main's tip — a release cut, or a card branch merged up to date — still deploys;
the one deliberate exception is a rollback (§6), which passes `--allow-behind`.
Deploy with a clean tree: the sha256 of `/opt/odile/app.py` must equal
`git show <deployed-commit>:app.py` (§4.1).

## 1. Back up the live database

The database is in WAL mode and the service is running, so use the SQLite backup
API, not `cp`.

```sh
TS=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p /var/backups/odile && chmod 700 /var/backups/odile
BK=/var/backups/odile/waitlist-$TS.db
/usr/bin/python3 - "$BK" <<'EOF'
import sqlite3, sys
src = sqlite3.connect('file:/var/lib/odile/waitlist.db?mode=ro', uri=True)
out = sqlite3.connect(sys.argv[1])
with out:
    src.backup(out)
out.execute('PRAGMA journal_mode=DELETE')
out.commit()
print('backup:', sys.argv[1])
print('integrity:', out.execute('PRAGMA integrity_check').fetchone()[0])
print('signups:', out.execute('select count(*) from waitlist').fetchone()[0])
EOF
chmod 600 "$BK"
echo "$BK"
```

Record the path and the pre-deploy rows (`id`, `email`, `created_at`, `token`,
`source`, `unsubscribed`) so step 4 can compare them.

The deploy on 2026-11-06 wrote and used
`/var/backups/odile/waitlist-20261106T123433Z.db` (sha256
`c65e01a8bd8edf663d908ad17d8c61482916b50b2b522d241c7840a782971d79`, integrity
`ok`, 2 signups, `user_version 0`). That path is the restore source in §6.

## 2. Deploy

```sh
cd <checkout>              # main checkout, clean tree
git rev-parse HEAD          # record the deployed commit; §4.1 checks against it
./scripts/deploy.sh -n              # dry run: guard verdict + the plan; writes nothing
./scripts/deploy.sh                 # apply; ends with `active` and `page 200`
```

The script resolves the repository root from its own path, so the first output
line is the checkout — not `scripts/` — whichever directory you run it from.
Measured on 2026-11-14 at `83eefd2`:

```text
→ source  <checkout>
→ runtime /opt/odile
→ guard   HEAD 83eefd2 contains main 83eefd2
```

A copy of the script outside `scripts/` — where `..` is not the checkout —
refuses before the guard or any write, naming the directory it resolved:

```text
deploy: refused: <dir> is not the repository root (no app.py; run scripts/deploy.sh from the checkout)
```

A refused deploy prints one stderr line, exits 1 and writes nothing (measured
on 2026-11-11 from a card worktree three commits behind `main`):

```text
deploy: refused: HEAD 15f0793 does not contain main 0df4706; 3 commit(s) missing: 0df4706 Merge odl/testing-host-check-retire, 8cd8efc ops: ODL-492 — a deploy from a behind-main branch must not roll production back, 94bfbd2 ODL-306: the testing-host check asserts the retirement; deploy from main or a checkout that contains it, or pass --allow-behind for a deliberate rollback (docs/runbooks/deploy.md §6)
```

`-n` runs the same guard and refuses in exactly the same words; the flag only
changes what happens after the guard has passed. The list stops at ten commits
and adds `(+N more)`; `git log HEAD..main` shows the rest. The guard reads the
local `refs/heads/main`, which every worktree of this repository shares.

`rsync --delete` removes files under `/opt/odile` that are not in the checkout,
except the host-only paths `scripts/deploy.sh` excludes:

- `docs/growth/` — the growth seat's workspace (`queue.md`, `log.md`,
  `research-log.md`, `teammate-sync.md`, `x-launch.md`, `research/`, `assets/`).
  It is not in git. Three enabled cron jobs in the `growth` profile use
  `workdir=/opt/odile` and read or write it: post cycle
  (`a24afb53eb72`, Tue/Thu/Sat 15:00Z), weekly learning pass
  (`48077358dd2f`), teammate sync (`28aa6a309d34`). `--exclude 'docs/growth'`
  keeps a deploy from deleting it. If it is ever lost, restore it from
  `/var/backups/odile/pre-ODL-305-opt-odile-extras-20261106T123512Z.tar.gz`:

  ```sh
  tar xzpf /var/backups/odile/pre-ODL-305-opt-odile-extras-20261106T123512Z.tar.gz \
    -C /opt/odile docs/growth
  ```

- `.worktrees/` — transient git worktree copies. Excluded so a deploy does not
  copy ~250 MB into the runtime, nor delete the runtime copy when deploying
  from a task worktree.

- `scripts/deploy.sh` (tracked) and, on this host, the root-level copy of it
  listed in `.git/info/exclude` — the entry point itself. Both are excluded
  by root-anchored paths, so neither is copied into `/opt/odile`, and
  `--delete` cannot remove the one that is not the rsync source. Before
  2026-11-14 the tracked script resolved its source as `scripts/` and a run
  from there planned 1305 file deletions, including `app.py` and every page
  under `web/` (see §2).

`tools/daily_visual.py` was also host-only. It has no consumer in the repo and
no cron job references it, so it was retired, not restored; its only copy is in
the same archive.

### Which copy of a host-local tool the deployed tree runs

`/opt/odile` runs **the HQ checkout's copy of every HQ-only path a deployed unit
executes or reads** — the files `.git/info/exclude` hides, present in HQ's
working tree and absent from its index. Today that is every private file under
`tools/` and `docs/backlog.yaml`:

- `tools/roadmap_page.py` is the generator `odile-roadmap.service` executes;
  the rest of the private machinery under `tools/` is what the board and the
  merge gate run, and a deploy already had to keep it.
- `docs/backlog.yaml` is that unit's `--backlog /opt/odile/docs/backlog.yaml`.
  The plan is HQ-only, so a worktree cannot carry it.

A card worktree can carry its own copy of a hidden tool — the same exclude file
hides it there too, so `git status` stays clean — and a plain `rsync --delete`
installed that copy over HQ's. Measured 2026-11-23: ODL-949's deploy from
`.worktrees/t_a0e1fdb5` restored the pre-card generator (`a2957af5…`) over the
fixed bytes (`5f84c3e0…`), and the 17:00Z tick regenerated the live page from
it, undoing ODL-347-F1. The same deploy shape broke the other way: this card's
deploy from `.worktrees/t_d0d4b865` deleted `/opt/odile/docs/backlog.yaml` and
`odile-roadmap.service` exited 2 (`roadmap: [Errno 2] No such file or
directory: /opt/odile/docs/backlog.yaml`) until the file was restored.

One rule now covers every such path, implemented in `scripts/hq_tools.sh` and
called by `scripts/deploy.sh`:

- the deploy refuses before any write when the checkout does not carry the
  helper — a deploy without the rule would silently ship a worktree's copy;
- the HQ checkout is the parent of the shared `git rev-parse
  --git-common-dir`, which every worktree reports;
- each HQ-only path is excluded from the rsync by its root-anchored path, so a
  worktree's copy never lands and `--delete` cannot remove the deployed one;
- the exclusion list is `scripts/hq_tools.sh exclude`, not `list`: it also
  names every unit input `/opt/odile` already holds. A checkout that hides
  nothing (a standalone clone, or a `list` that fails) therefore still excludes
  the deployed copies, and `rsync --delete` cannot remove the only one — the
  deploy fails closed, because the assignment aborts the run when the helper
  fails and an empty list never means "exclude nothing";
- after the rsync, `scripts/hq_tools.sh install` copies HQ's files into
  `/opt/odile`. It is idempotent: a converged file prints `same`, a changed one
  prints `installed` with its sha256.

Adding a hidden input to a deployed unit means adding it to `HQ_ONLY_UNIT_INPUTS`
(its name, so a clone cannot delete it) and, when it lives under a new
directory, to `HQ_ONLY_PATHS` in `scripts/hq_tools.sh`;
`tests/test_deploy_hq_tools.py` pins both lists.

A standalone clone carries no hidden files, so there is nothing to install, but
its deploy keeps what `/opt/odile` already has: the clone's `exclude` still
names both unit inputs because the deployed tree holds them, and the rsync
leaves them alone. Deploy from a worktree of the HQ checkout, as §2 and §6 do,
when the deployed tree should receive HQ's *current* bytes.

The dry run lists the rule's whole plan, and the apply prints the same lines
with `same`/`installed`:

```sh
./scripts/deploy.sh -n
# → would keep /opt/odile's copies of the HQ-only paths and install HQ's from <HQ checkout> (tools/ and docs/backlog.yaml); a worktree's copy never lands:
#   hq-tools: same docs/backlog.yaml
#   hq-tools: same tools/roadmap_page.py
#   hq-tools: would install tools/<name>.py (<sha256>)
#   hq-tools: N file(s) would change, M already HQ's bytes
```

The live check a verifier runs after the deploy is in §4.

### The tree's mode

`./scripts/deploy.sh` follows the rsync with `install -d -o root -g odile -m 0750
/opt/odile` and then asserts `stat -c '%a %U:%G'`. The order matters: `rsync
-a` rewrites the destination root's mode from the checkout, so a bare rsync
puts a hand-set 0750 back to 0755 — measured on 2026-11-10, after which the
unprivileged uid could list the tree again and the next `./scripts/deploy.sh` restored 0750.
`odile.service` runs as `odile:odile` and reads through the group, so 0700
would take odile.fyi down; the deploy fails loudly rather than leave either
mode. Check both:

```sh
stat -c '%a %U:%G' /opt/odile                  # 750 root:odile
sudo -u nobody ls /opt/odile                   # Permission denied
sudo -u odile test -r /opt/odile/app.py && echo readable
```

`./scripts/deploy.sh` asserts the mode on every run (a mismatch is `deploy: /opt/odile is
…` and exit 1); run the two read tests after it.

### Where a branch is previewed

Production is main-only, so a branch is previewed from its own worktree, on
loopback — never by pointing `./scripts/deploy.sh` at `/opt/odile`:

```sh
cd <checkout>/.worktrees/<card>
ODILE_BIND=127.0.0.1 ODILE_PORT=8099 \
ODILE_DB=/var/tmp/odile-preview-<card>.db \
ODILE_ATTEST=/var/lib/odile/attestation.json \
ODILE_ROADMAP=/var/lib/odile/roadmap.html \
  /usr/bin/python3 app.py
```

Then, in another shell:

```sh
for p in / /roadmap /verify; do
  curl -s -o /dev/null -w "$p %{http_code}\n" "http://127.0.0.1:8099$p"
done
```

`ODILE_DB` is a throwaway file, not `/var/lib/odile/waitlist.db`: `app.py`
runs `init_db()` at start, and a preview must not touch the live queue.
`ODILE_BIND=127.0.0.1` keeps the preview off the network. Ctrl-C stops it;
delete the database afterwards. Measured on 2026-11-11 from the ODL-492 branch
worktree — which was three commits behind `main` at the time — `/`, `/roadmap`
and `/verify` all answered 200 and the live site was untouched.

`make demo` previews the workspace surface instead, and
[`demo-public.md`](demo-public.md) is the hosted demo; neither replaces a
deploy. A branch that has to be on `odile.fyi` itself must contain `main`'s tip
(merge `main` into it), which is what the guard checks. A release cut must merge
`main` before it publishes: [`update.md`](update.md) §5 publishes a release by
deploying `release/`, and a deploy is main-only.

## 3. The seed service and its route

`./scripts/deploy.sh` carries the seed service (ODL-204) as well as the site. A HQ
that skipped it had no `/seed`, so every tenant install failed at the seed
exchange, before any user-visible progress. The deployer is
`deploy/seed/deploy_seed.py`; it is idempotent — a converged host prints
`same` for the unit, the boot link and the route, and writes nothing.

### 3.1 Prerequisites — two root-only files

The deploy does not create the seed config, and creates the seed key only when
it is absent. Prepare the config once per host, per [`seed.md`](seed.md) §3
steps 1–2, at mode 0600 (the service refuses a group- or world-readable file):

- `/root/.odile/seed-config.json` — the inference key, the model pin, the
  support public key, the backup repo URL and the mail-relay block, with
  `{slug}` substituted per tenant;
- `/root/.odile/seed-signing.pem` — the **seed** key, which signs every tenant
  seed and nothing else (ODL-514). `deploy/seed/deploy_seed.py` creates it 0600
  root when it is missing and writes its public half to
  `deploy/seed/seed-key.pub`; the release key, which signs the installer and
  the certificate over this key, is CI-held and is not on this box.

The deploy also checks the chain: `deploy/seed/seed-key.crt` must be the
release key's signature over `deploy/seed/seed-key.pub`, or the run stops with
the `make seed-cert` ceremony step in the message. A missing or wrongly-moded
file stops `./scripts/deploy.sh` before it installs the seed unit or route, naming the
file:

```
error: missing /root/.odile/seed-config.json — create it per docs/runbooks/seed.md §3, then re-run
```

The unit reads both through `LoadCredential=`; `/root` is 0700, so the
unprivileged `odile` user never sees the paths.

### 3.2 What the deploy changes

1. installs `deploy/seed/odile-seed.service` at
   `/etc/systemd/system/odile-seed.service` (a replaced unit is kept as
   `odile-seed.service.bak-<UTC>`), then `daemon-reload` and `enable --now`;
2. splices `deploy/seed/odile-seed.caddy` into `/etc/caddy/Caddyfile` between
   `# BEGIN odile-seed` and `# END odile-seed`, keeping a
   `Caddyfile.bak-<UTC>` copy, then `caddy validate` — a file Caddy rejects is
   restored and the deploy fails — then `systemctl restart caddy`;
3. restarts `odile-seed` only when the module it runs changed (the deploy
   passes the pre-deploy sha256 of `/opt/odile/seed/seed.py`).

The committed fragment is the source of truth for the live block: it routes
`/seed` and only `/seed` to `127.0.0.1:8081`, and everything else to the app on
`127.0.0.1:8080`. Edit `deploy/seed/odile-seed.caddy`, never the live file.
The splice accepts every shape a host can be in — a fresh bare
`reverse_proxy 127.0.0.1:8080`, an explicit `handle { ... }` catch-all, or the
unmarked block applied by hand on 9 November — and converges them to the same
bytes; anything it does not recognise is refused rather than guessed.

### 3.3 Probes

`./scripts/deploy.sh` ends with these, and each is the evidence line for a fresh host:

```sh
systemctl is-enabled odile-seed                    # enabled: it survives reboot
systemctl is-active odile-seed                     # active
curl -fsS http://127.0.0.1:8081/health             # ok
curl -s -o /dev/null -w '%{http_code}\n' https://odile.fyi/seed   # 405
curl -s -o /dev/null -w '%{http_code}\n' https://odile.fyi/        # 200
```

`GET /seed` is 405 by design: the exchange is a POST, so no token ever lands in
a URL. A 404 means the route is missing (`systemctl status caddy`, then re-run
the deploy); a 502 means the route exists and the service does not
(`systemctl status odile-seed`, `journalctl -u odile-seed`).

### 3.4 Rollback

The service is additive: no schema is dropped, no tenant row is touched. To
take the endpoint down, follow [`seed.md`](seed.md) §8: `systemctl disable --now
odile-seed`, then delete the `@seed path /seed` matcher and its `handle @seed`
block from the `# BEGIN odile-seed` … `# END odile-seed` region — keeping the
app's `handle { reverse_proxy 127.0.0.1:8080 }`, which lives inside it — and
`systemctl restart caddy`. `/seed` then falls through to `app.py`, which
answers 404, and the live tokens stay inert.

## 4. Verify

Run all four; each is also the evidence line for the card.

1. Code identity — the deployed app is the build recorded in §2:

   ```sh
   sha256sum /opt/odile/app.py
   git show <deployed-commit>:app.py | sha256sum
   ```

   The two hashes must match. Pinning the commit keeps the check true after
   `main` moves; comparing against `main` is only correct while `main` has not
   moved since the deploy. On 2026-11-06 both were
   `bcd11660dec1fb5b9d8c74b57fac03939bf045cb5b3c705496cc5204aaccb0db` at
   `a75043b` (blob `455f93a`, unchanged through `5805e0c`).

   The private paths are checked the same way, in one command (§2, "Which copy
   of a host-local tool the deployed tree runs"). It compares every HQ-only path
   the units run with — `tools/roadmap_page.py` and `docs/backlog.yaml` against
   the HQ checkout's copies — and proves the tick that wrote
   `/var/lib/odile/roadmap.html` ran those bytes: the page's own footer instant
   must follow the installed tool, the footer's source sha must be
   `/var/lib/odile/roadmap-source`, and the deployed generator must render the
   same footer in a `--stdout` dry run.

   ```sh
   ./scripts/check_hq_tools.sh
   ```

   It exits 1 with a `DIFF`/`MISSING` line for any file the deployed tree does
   not carry at HQ's bytes, and with `the page was generated … before … was
   installed` when the tick has not run since the deploy
   (`systemctl start odile-roadmap.service`). `OK` is the pass. The page is not
   compared byte for byte: the board moves between ticks.

2. Service and public page:

   ```sh
   systemctl is-active odile
   curl -s -o /dev/null -w '%{http_code}\n' https://odile.fyi/
   ```

   Expect `active` and `200` (the deploy script prints both). Also check
   `journalctl -u odile --since '-5 min'` for a traceback.

3. Live schema and rows:

   ```sh
   /usr/bin/python3 - <<'EOF'
   import sqlite3
   c = sqlite3.connect('file:/var/lib/odile/waitlist.db?mode=ro', uri=True)
   print('tables:', [r[0] for r in c.execute(
       "select name from sqlite_master where type='table' order by name")])
   print('user_version:', c.execute('pragma user_version').fetchone()[0])
   print('waitlist:', c.execute('select count(*) from waitlist').fetchone()[0])
   EOF
   ```

   Expect the eleven tables `app.init_db()` creates — `accounts, admin_log,
   claims, events, grants, seed_tokens, tenant_keys, tenant_state,
   tenant_state_events, tenants, waitlist` — and `user_version: 4`.
   `admin_log` is the append-only record of the console's re-point action
   (ODL-582: who moved an account's address, from which address to which, and
   when; the previous address stays the account's name for `/forgot`).
   `tenant_keys` is the inference key's
   record (ODL-242: the label, the first characters and the state; never the
   key), created by `odilectl/state.py`'s additive schema. Schema v2 was the
   same list without the three claim-spine tables, at `user_version: 2`; v3
   adds the `tenants.release` column and no table; v4 adds the `accounts` table
   and no column (ODL-341: one row per registered tester — the scrypt verifier,
   the confirmation token's hash, the trial window written once at registration,
   the session epoch, and the provision queue: the state, the provider machine
   id, the IPv4 and the inference key's label and first characters). The live
   database also holds
   `health_alerts`, `heartbeat_keys` and `heartbeats` once the health ingest
   has run (`odilectl/health.py`); they are not part of `init_db()`, so compare
   the eleven and the version, not equality of the whole list. Compare the signup count and rows with §1. On 2026-11-06
   both rows were unchanged (same `id`, `email`, `created_at`, `token`,
   `source`, `unsubscribed`) and `migrate` backfilled exactly one `signup`
   event per row.

4. Funnel and count — the M0 exit criterion:

   ```sh
   /usr/bin/python3 /opt/odile/manage.py funnel
   /usr/bin/python3 /opt/odile/manage.py count
   ```

   The funnel prints six stages; its `signups` line must equal the count. On
   2026-11-06 it printed `signups 2`, `invited 0`, `claimed 0`, `installed 0`,
   `activated 0`, `active in last 7 days 0`, and `count` printed
   `on the list: 2`. Running `/usr/bin/python3 /opt/odile/manage.py migrate`
   again is idempotent: every table `already present`, 0 backfilled.

## 5. The claim worker (`odile-claim.timer`)

ODL-220's claim spine records a claim in the web service; the paid work — adopt
or order the machine, write the A record, mint the setup token — is this root
command run from `/opt/odile`:

```sh
/usr/bin/python3 -m odilectl.claim run --apply --json
```

The ready email is the second half of the same timer (ODL-947): the order
records the machine and stages the mail's facts, and a later run asks the
machine's own `/attestation` and `/verify` at `https://<slug>.<tenant domain>`.
Only a machine that answers gets the one ready email, rendered from the staged
facts with the machine's own launch measurement (the number its `/verify` page
prints). Until then `claim status` says `waiting_for_guest`, and the row stays
`provisioned`.

`scripts/deploy.sh` installs `odile-claim.service` (oneshot) and `odile-claim.timer`
(every five minutes, `WantedBy=timers.target`) and enables the timer, so no
human runs the queue. A converged run exits 0 with `"count": 0`; the timer's
journal is the record of every run.

**Config prerequisite.** The worker exits 5 until `/root/.odile/config.yaml`
exists at mode 0600 with at least:

| key | value |
|---|---|
| `reseller_key` | the Servury reseller key |
| `default_plan_id` | the numeric plan a machine is ordered on. **138** — Blackbox Nano, Canada (1 vCPU / 2 GB / 25 GB). 139 is the Netherlands Nano and 141 the US Nano; 108/127 are the larger Core and are never the default (Servury cannot downgrade a machine) |
| `default_days` | the term: **365**, the longest term and the largest discount ($47.44 a year at the partner price, measured 2026-11-24). A shorter term names its measured reason on the card; Nano's own minimum is 31 days, and 7 would be refused |
| `default_vcpus` | **1** — the Nano's size; the plan id already names the tier, this keeps the order's own record honest |
| `dns_zone_id` | the Cloudflare zone id for `odile.fyi` |
| `outbox` | `/var/lib/odile/outbox` |
| `alert_email` | where a failed provision is reported |

The default new machine is a Blackbox Nano at the 365-day term (Savio, 2026-11-24;
`docs/ops/evidence/2026-11-24-servury-nano/README.md`). The test bench carries the
same plan id but a 31-day term (`/root/.odile/config.test.yaml`): Nano's minimum
term is 31 days and a bench machine is disposable, so a year would cost ten times
a two-week trial. `tools/servury_panel.py --order` defaults to the same
138 / 365, and `docs/evidence/nano-baseline.md` records the bench measurement of
the Nano envelope.

The provider key and the Cloudflare token also exist as their own 0600 files
(`/root/.odile/servury_reseller_key`, `/root/.odile/cloudflare_dns_token`). HQ's
config was written for the first-tenant run (ODL-332); it is not in git.

Because the worker runs as root, a message it queues is `root:root` 0600 inside
the `odile`-owned outbox. The mailer that sends it (ODL-232) has to run as root
too, or take ownership of the queued files when it picks them up.

**The queue rule.** A row in `tenants` is work when all of these hold: `plan`
is non-empty, `ssh_public_key` is set, `claimed_at` is set, `state` is
`claimed` or `provisioned`, and there is no `provisioned` event whose
`detail.step` is `ready`. The event, not the queued file, is the done marker:
the mailer may send the message and remove the `.eml`, and the tenant still
must not be provisioned twice.

**A failure after adoption terminates the machine it adopted.** Provisioning
looks at the provider's machine list first and adopts an existing machine with
the tenant's hostname instead of ordering a second one. If any later step fails
— poll, DNS, token, kit or email — the worker terminates that machine, adopted
or newly created, removes a DNS record it wrote, restores the tenant row's
fields to what they were before the attempt (a first claim goes back to
`claimed`) and writes a `provision_failed` alert to the outbox. A retry therefore
costs nothing the failed attempt did not already clean up.

**Checks.**

```sh
systemctl is-enabled odile-claim.timer      # enabled
systemctl list-timers 'odile-claim.timer'   # next elapse within five minutes
systemctl start odile-claim.service; echo $?
journalctl -u odile-claim.service -o cat -n 20
```

A failed provision exits 1, leaves the tenant row as it was before the attempt
plus a `provision_failed` event (so `claim status` prints `provision: failed`),
and names the alert path in the journal; the next tick retries it.

**Rollback** — stop the queue without touching the site, then let `scripts/deploy.sh`
re-enable it on the next deploy:

```sh
systemctl disable --now odile-claim.timer
```

## 6. Rollback

The pre-deploy code is a git commit, so rollback is a deploy of that commit
plus the database backup from §1.

```sh
# 1. Stop the service so nothing writes while the database is replaced, and
#    stop the units the newer tree installed: their scripts live under
#    /opt/odile, and the rollback's rsync deletes them.
systemctl stop odile
systemctl disable --now odile-attest-refresh.timer odile-claim.timer \
  odile-roadmap.timer odile-seed

# 2. Restore the pre-deploy database (path recorded in §1)
install -o odile -g odile -m 600 \
  /var/backups/odile/waitlist-20261106T123433Z.db /var/lib/odile/waitlist.db
rm -f /var/lib/odile/waitlist.db-wal /var/lib/odile/waitlist.db-shm

# 3. Deploy the previous build with that build's own entry point: 6c0535f is
#    the 5 November build, and its entry point is ./deploy.sh at the tree root
#    (it predates the scripts/ layout). Its script predates the main guard, so
#    it needs no flag and installs exactly the units that tree carries.
git worktree add /tmp/odile-rollback 6c0535f962ce4534accc6ec259ae6687b14360b4
cd /tmp/odile-rollback && ./deploy.sh
cd <checkout> && git worktree remove /tmp/odile-rollback

# 4. Verify the old build serves
systemctl is-active odile
curl -s -o /dev/null -w '%{http_code}\n' https://odile.fyi/
```

The 5 November build has no `migrate`; it ignores the v2 tables. Restoring the
database backup also returns `user_version` to 0 and leaves only `waitlist`.
A rollback loses every signup written after the backup, so record the backup
timestamp in the incident note.

Run the rollback tree's **own** entry point — for this build, `./deploy.sh` at
the tree root; a target that carries the `scripts/` layout runs
`scripts/deploy.sh` there instead. The current script expects the current tree:
it installs `deploy/attest-refresh`, `deploy/seed`, `deploy/claim-worker` and
`deploy/roadmap`, and none of the four exist at `6c0535f`, so copying it in
aborts with
`install: cannot stat 'deploy/attest-refresh/odile-service-atomic.conf'` after
the rsync and before the restart (measured 2026-11-11 in a private mount
namespace).

A rollback target that carries the guard — any commit after ODL-492 — runs its
own entry point with `--allow-behind`; that is the flag's one use. Check that
entry point before running it: until this fix, the entry point in `scripts/`
resolves its rsync source as `scripts/`, so `./scripts/deploy.sh -n` from the
worktree root prints `→ source  <tree>/scripts` and plans the live site's
deletion. Fix the target's own copy first — replace `cd "$(dirname "$0")"` with
`cd "$(dirname "$0")/.."` in its `scripts/deploy.sh`, then re-run `-n` until
the first line is `→ source  <tree>` — and only then deploy. A tree at or after
this fix resolves the checkout root itself and needs no edit. The four units
stopped in step 1 are the ones the newer tree added; the next main deploy
re-enables all of them.

The 5 November `deploy.sh` predates the `docs/growth` and `.worktrees` excludes,
so a rollback run as written deletes both from `/opt/odile`. Restore the growth
workspace from the archive with the command in §2 after step 4; the
`.worktrees` copy is transient and needs no restore. It also predates the tree
mode: its rsync leaves `/opt/odile` at 0755, so run
`install -d -o root -g odile -m 0750 /opt/odile` after a rollback.
