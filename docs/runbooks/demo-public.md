# The public demo — demo.odile.fyi on Odile's tenant machine

A judge opens <https://demo.odile.fyi>, meets Odile's sign-in page, signs in as
`judge`, and lands in the Webapp of Odile's own tenant machine: the custom
measured image (`odl-7971a8`, `203.0.113.87`, measurement `9f01725a…`), one
shared workspace, no nightly reset and no meter. Card: ODL-503.

`https://odile.fyi/judge` was retired on 2026-11-14: ODL-901 answers it with a
301 to <https://demo.odile.fyi/login>, the address the posted cut prints. The
judge path is login → the seeded session → `/verify`; `README.md` names the
login entry and the page prints the verifier line (a tenant run needs
`--expect`).

This is **Odile's tenant machine**: Odile holds the boot key and can log in,
the judge password is published on purpose, and the machine's `/verify` is the
page a judge should be sent to. It does not demonstrate a user-held key.

| Piece | Value |
|---|---|
| DNS | `demo.odile.fyi` A -> `203.0.113.87`, DNS-only (Cloudflare, `proxied=false`) |
| TLS + proxy | Caddy on the tenant, block between `# BEGIN odile-demo-public` and `# END odile-demo-public` in `/etc/caddy/Caddyfile`; `admin off`, so a change needs `systemctl restart caddy` |
| Webapp | `127.0.0.1:9130`, the `tenant` gateway; `webapp.public_url` is `https://demo.odile.fyi` |
| Host check | Hermes refuses a Host header that is not the bound host or `webapp.public_url` (`400 Invalid Host header`); the demo host *is* the public_url, so the machine's own name `odl-7971a8.odile.fyi` still answers `/verify`, `/first-visit` and `/manifest`, but its Webapp path is refused |
| Gate | Hermes dashboard basic-auth provider (`HERMES_DASHBOARD_BASIC_AUTH_*` in `/home/tenant/.hermes/.env`, mode 0600) |
| Account | `judge`; the password is carried by the sign-in page's form (`deploy/demo-host/login.html`), never here |
| Sign-in page | `/srv/odile-demo/login.html`, Caddy's `handle /login*`. Committed and live are **one file**: sha256 `94a22b03748f40ae…` (167,426 B), t_9896a577's artwork page ("The demo machine" + the inlined Ruysch band; cards t_3d717ffc, t_330cea5c, t_9896a577), installed from `deploy/demo-host/login.html`. `--no-login` remains for a deploy that must keep the machine's current page |
| Caddy block | `deploy/caddy/demo-tenant-site.caddy`, sha256 `64fd9d64e9e3a1309b0c214512b104b3e15c30824edc85d5cac049869f99d1f4`, 3,213 B; no mutation matcher and no self-update matcher — every write reaches Hermes (§4) |
| Seeded session | `Start here — Odile demo`, pinned, two messages, id `local-931d9e32…` (the constant `/judge` deep-links); written **once**, when the row is absent, and never rewritten |
| Sessions | every session a judge opens persists across deploys and reboots; a deploy writes no session and deletes none |
| Meter | none in the path: model route is Near AI direct, `.env` carries one key; on this machine the meter is **removed** (unit not installed, no key file, user gone — t_e237e261), so nothing masks or starts it |
| HQ copy | **retired 2026-11-13; code deleted 2026-11-14** (t_9d7ee536): no account, home, unit or Caddy block on HQ, and this tree carries no deployer for one; the record still switches with one command, but HQ no longer serves the Host without a restore |

## 1. Committed files

| File | Role |
|---|---|
| `deploy/demo-host/deploy_demo_public.py` | one command: DNS, the tenant files over its own Webapp socket, the apply, the restart, the HTTPS verify; `--dry-run`, `--self-test` |
| `deploy/demo-host/apply_demo_public.py` | runs as root **on the tenant**: sign-in page, Caddy block, Hermes settings, judge account, seeded session (written once), and the removal of the old boot unit; `--dry-run`, `--self-test`, `--report`, `--report --session-id` |
| `deploy/demo-host/tenant_console.py` | the transport: login, WS ticket, one `shell.exec` per command (a tenant has no SSH key) |
| `deploy/demo-host/check_demo_public.py` | the card's DoD over HTTPS plus the tenant's own report; `--persistence`, `--sessions` |
| `deploy/demo-host/create_dns.py` | the one Cloudflare record; idempotent; `--target 203.0.113.139` moves it to HQ's address (no demo there since 2026-11-13) |
| `deploy/demo-host/login.html` | Odile's sign-in page |
| `deploy/demo-host/verify-lede.html` | the demo machine's `/verify` lede, one paragraph; the apply injects it into the release template's tenant block on this machine only (finding 1) |
| `deploy/demo-host/seed/session-receipt.json` | the reviewed receipt template the seeded session's row is minted from (cwd, route and caps rewritten for this machine) |
| `deploy/demo-host/seed/start-here-tenant.md` | the seeded session's answer |
| `deploy/caddy/demo-tenant-site.caddy` | the tenant's site block, committed source of truth |

## 2. Apply

```sh
cd <checkout>
./deploy/demo-host/deploy_demo_public.py --dry-run
./deploy/demo-host/deploy_demo_public.py
# only for a tree older than the live page — keep `/srv/odile-demo/login.html`:
./deploy/demo-host/deploy_demo_public.py --no-login
```

`--no-login` keeps `/srv/odile-demo/login.html` as the machine has it and does
not upload the tree's copy. Since t_9896a577's artwork page merged (main
`87a0f75`), the committed page *is* the artwork page (`94a22b03…`), so a
deploy from `main` installs it and the flag is not needed. The deploy asserts
the live page's markers either way, so a stale page still fails the gate check.

A converged machine prints `same` for every piece and still runs the restart
and the verify. The apply restarts Caddy (which drops the socket it is talking
over) and then `odile-agent`; the gateway takes ~35 s to answer again, and its
graceful shutdown can take up to `TimeoutStopSec=90`, so a restart is a
one-to-two-minute outage of the demo.

### The `/verify` lede is demo-only — the choice

The page a judge lands on must not open with the customer row ("No one at
Odile can log in to a tenant machine. The only sign-in is yours."): on this
machine Odile holds the boot key and the sign-in is the shared `judge`
account. `demo.odile.fyi/verify` therefore opens with

> This is Odile's tenant machine: one shared workspace on our measured AMD
> SEV-SNP machine. Odile holds this machine's boot key and can log in. The demo
> password is public by design. Every judge who signs in lands in the same
> workspace.

**The route (ODL-503 finding 1): a demo-only page, not a release re-cut.** The
signed installer's page already carries both paragraphs — the demo one in a
`<!--demo-->` block, the customer one in the `<!--tenant-->` block — but the
release first-visit server keeps only tenant/machine blocks, so the demo
paragraph never reaches a browser. Re-cutting the release to change that would
change what every customer machine verifies. Instead:

- `deploy/demo-host/verify-lede.html` is the demo's one paragraph, assembled
  from `docs/CLAIMS.md` §"The demo machine" rows; `web/judge.html`, the
  second source until ODL-901 retired the page, is gone;
- `deploy/demo-host/verify-command.html` is the demo's one-line command
  section: the terminal block's last line is
  `cd odile-verify && ./odile-verify https://demo.odile.fyi --expect …`, and
  the first-visit renderer fills `{{MEASUREMENT}}` from the report it serves.
  A tenant URL is not the pin's `source`, so the verifier's own rule needs
  `--expect`; the page is where a judge copies the number. `README.md` names
  the same form and sends the reader to the page for the live value;
  `check_demo_public.py` extracts that line and runs it out of the published
  tarball, so the page and the verifier cannot drift;
- `apply_demo_public.py` replaces the tenant block's one lede in
  `/usr/local/lib/odile/verify.html` — the template the first-visit service
  reads per request — adds the command section inside the same block, and keeps
  the release page at
  `/usr/local/lib/odile-demo-public/verify.html.release` for the rollback;
- the deploy's `apply --write` is the only writer. `odile-release-setup.service`
  converges **once** (t_ee5aac66 writes a done marker under `/var/lib/odile`),
  so after the first boot the release installer no longer rewrites the
  template; there is no boot unit and no `--tune` re-assert;
- a customer machine never runs `apply_demo_public.py`, so its `/verify` keeps
  the general row. `tests/test_demo_public.py` renders both pages through the
  release renderer and pins the sentence each one shows. The same test runs
  the anchor against `installer/odile_install.py` and `release/odile_install.py`
  in `make check`: a re-cut that moves the tenant block or adds a second lede
  turns the gate red, and the apply then exits non-zero rather than serving a
  half page.

Rollback, as root on the machine:

```sh
sudo install -m 0644 /usr/local/lib/odile-demo-public/verify.html.release \
    /usr/local/lib/odile/verify.html
```

The template is read per request, so no restart is needed. `--report` prints a
`verify` object with the template's hash, `demo_lede`, `demo_command`,
`customer_lede` and whether the rollback copy is present.

### The meter is gone

The demo used to stop and mask the meter here. It no longer does (t_ee5aac66:
the machine's meter state is its own), and the meter is **removed on this
machine**: on `odl-7971a8` the unit is not installed
(`systemctl is-enabled odile-meter` → `not-found`), `/etc/odile/meter.key` is
gone and the `odile-meter` user is deleted (t_e237e261, merged into `main` at
`0a4b726`; the installer no longer installs the meter product-wide). The
`.env` carries the machine's one Near AI key, and `apply_demo_public.py` reads
it from there (falling back to `/etc/odile/meter.key` only on a machine that
still has the meter). `apply_demo_public.py --report` prints the unit's state;
the check no longer asserts on it.

## 3. The spend risk, stated plainly

**There is no meter by decision.** The tenant's Hermes calls NEAR AI directly
with the single key in its `.env`; nothing counts or caps tokens. A stranger
who has the published password can spend the trial credit up to the provider's
own balance. That is the same exposure a real tenant has on the same route.

**There are no run caps either** (t_ee5aac66, Savio 2026-11-13: *"they are free
to make changes, can see past sessions, no unexpected reverts / locks /
deletes"*). A real tenant's config sets neither a turn limit nor a session
limit, so the demo sets neither and writes none: the turn and session limits in
`config.yaml` are the owner's, and a judge who sets one in Settings keeps it
across the next deploy. The old three-session / twenty-five-turn pair is
deleted. A fourth concurrent session runs — `check_demo_public.py --sessions`
measures exactly that.

The provider fallback / spare key is Savio's to set up in Hermes settings; the
tenant `.env` carries exactly one inference key. There is no daily spend check
and no daily reset and no prune: sessions persist across deploys and reboots,
and the seeded session is written once.

The provider row's **label** is owned here too: the release installer's one
run writes `name: Odile Meter`, so `apply_demo_public.py` renames the row to
`Near AI` and re-points it at `https://cloud-api.near.ai/v1` in both shapes
Hermes accepts (the installer's `custom_providers:` list and the normalised
`custom_providers: []` + `providers:` map). It validates the YAML before every
write; `--report` prints the row the machine actually uses, and
`check_demo_public.py` fails on any other label or route.

### What a judge can reach on this machine

The published password signs in as `judge`, and the transport it lands on runs
one command per `shell.exec` as `tenant` — an account with passwordless sudo
(`sudo -n id -un` → `root`). That is the demo's design: one shared machine,
Odile holds the boot key, and the public copy says so. These are the commands
that measure it, run through the tenant's own Webapp transport
(`tenant_console.py`); this is the 2026-11-14 01:39–01:46Z transcript:

```sh
$ id -un; sudo -n id -un
tenant
root
$ ls -l /home/tenant/.hermes/.env
-rw------- 1 tenant tenant 348 Nov 13 10:41 /home/tenant/.hermes/.env
$ grep -c '^HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY=' ~/.hermes/.env
1
$ systemctl is-active odile-meter; systemctl is-enabled odile-meter
inactive
masked
$ ls -ld /var/lib/odile
drwxr-xr-x 2 odile-meter odile-meter 4096 Nov 13 10:40 /var/lib/odile
$ sudo -n nft list ruleset
sudo: nft: command not found
```

Measured 2026-11-14 01:39–01:46Z and re-checked 15:05–15:35Z; re-measured
17:54:57Z. Two readings are not the same at every probe, and both are kept:

- `systemctl is-enabled odile-meter` → `masked` at 01:4xZ (the unit was still
  installed), `not-found` from 15:05Z on (the unit is gone — t_e237e261, §2);
- `ls -ld /var/lib/odile` → `odile-meter odile-meter`, mtime 2026-11-13 10:40
  at 01:4xZ; `root root`, mtime 2026-11-14 04:51 at 17:54Z. The directory
  remains; its owner moved.

The other four readings are identical at every probe: the shell is `tenant`,
`sudo -n` is root, the tenant's one Near AI key is readable from it
(`grep -c` → 1 in a `0600` `.env` owned by `tenant`, 348 B), and there is no
`nft` on the machine. So the key the agent spends against is the key the
published password can read; the only cap is the provider's own balance (§3),
because there is no meter to mask or start.

## 4. Verify

```sh
./deploy/demo-host/check_demo_public.py --password-file /path/to/judge-password
./deploy/demo-host/check_demo_public.py --password-file … --persistence
./deploy/demo-host/check_demo_public.py --password-file … --sessions
```

`--persistence` writes a file in `/home/tenant/workspace` through the Webapp's
own shell, restarts `odile-agent`, reconnects and reads the file back (measured
2026-11-13: it survives). `--sessions` measures that a fourth concurrent
session runs.

Every run with a password also measures the relay's core sentence: it opens a
session, gives it one turn, samples the sessions already on the machine (the
seed and up to four others with messages), runs the demo's own apply with
`--no-restart`, and compares. **A deploy changes no session** — the row, its
messages and its title/pinned/hidden/archived flags are byte-identical across
the apply, and no sampled session disappears (the old prune is gone). Every
session the check opens is deleted at the end — the four concurrent ones, the
one it deletes, and the console's own `shell.exec` session, which Hermes keeps
as a row too. Existence is the machine's own `--report --session-id`, never the
list endpoint (that window is 20 rows); the check fails unless every id it
opened is gone and the tenant's session count is where it found it.

It also asserts there is **no `odile-demo-*` unit file and no `odile-demo-*`
timer** on the machine: `systemctl list-unit-files` and `systemctl list-timers`
both come back empty for that glob.

The block carries **no mutation matcher and no self-update matcher**
(t_69f1a5f3; Savio, 2026-11-13: "Remove everything about freezing in our
project codebase (self-update block included)"). There is no deny-by-default at
the edge, so every write a judge makes reaches Hermes. The check probes the open
shape: `PUT /api/config` and `POST /api/cron/jobs` with the judge session must
**reach Hermes** (422 for an empty body), `PUT /api/config` and
`POST /api/hermes/update` unauthenticated must be 401, a session the judge opens
must delete (`DELETE /api/sessions/<id>` 200, then 404), and `GET /api/sessions`
200 is the control. **A 403 from the edge is the regression**, not the
expectation; measured on this deploy 422 / 422 / 401 / 401 / 200 / 200 / 404.

The delete path is Hermes's, and it needs the session's receipt. A row whose
`gateway.local_policy.v1:<id>` receipt is absent from `state_meta` is refused
`503 storage_unavailable` by `session_local_recovery`, so a judge's Delete on
such a row does nothing (measured 2026-11-14 on two rows; reproduced after a
restart). The demo's own sessions all carry a receipt — the seed's is written
by the apply, and a Webapp-created session's by the gateway. For a receipt-less
row the owner path is:

```sh
sudo -u tenant env HOME=/home/tenant HERMES_HOME=/home/tenant/.hermes \
    hermes sessions delete --yes <id>
```

`--report` lists the live block's matcher names (`caddy.matchers`); the only one
left is `@apidocs`. A block that names a `mutation` or `update` matcher fails
both the deploy and the check.

The check also asserts the two Caddy copies by hash: the installed
`/usr/local/lib/odile-demo-public/demo-tenant-site.caddy` and the live block in
`/etc/caddy/Caddyfile` must both equal `deploy/caddy/demo-tenant-site.caddy`,
with one marker pair and no console banner (`ready: gateway.ready …`) after
`# END`. On 2026-11-13 a concurrent writer left the installed copy as the whole
6,772-byte Caddyfile and appended the banner to both files; the apply now
re-uploads the committed block and strips the banner, and the deploy fails if
either returns.

The tenant's own reading, as root on the machine or over the console:

```sh
sudo /usr/bin/python3 /usr/local/lib/odile-demo-public/apply_demo_public.py --report
```

It prints the config flags (`near_ai_direct`, `public_url`), the provider row
it actually uses, the `.env` key *names*, the meter's state, the `verify` object
(template hash, `demo_lede`, `customer_lede`, rollback copy), the Caddy block's
hashes and residue count, the seeded session's row and the last runs' turn
counts. The check also reads the served `/verify` and fails if its first
paragraph is not `deploy/demo-host/verify-lede.html` or if the customer row
still appears. `--report --session-id <id>` prints one session's row and a
digest of its messages, which is what the deploy-changes-no-session check
compares. What the same console reaches when it runs a shell — `tenant`,
passwordless root, the tenant's Near AI key — is the six-command block in §3,
with its outputs and dates.

## 5. The DNS record

`demo.odile.fyi` is one DNS-only A record, and `deploy/demo-host/create_dns.py`
is its only writer:

```sh
./deploy/demo-host/create_dns.py --target 203.0.113.87    # the tenant (idempotent)
./deploy/demo-host/create_dns.py --target 203.0.113.139   # back to HQ's address
```

The second line moves the record, not a demo: the HQ copy was retired on
2026-11-13 (account, home, units, Caddy block) and its code deleted on
2026-11-14 (t_9d7ee536), so a
working page behind that address needs an HQ demo rebuilt from git history. The
supported way back to a demo is to re-run the deploy against this tenant. Nothing
on the tenant changes either way: Caddy keeps answering `/verify` on the
machine's own name, and the record propagates in a minute.

## 6. Recovery

- **The Webapp is down but Caddy answers**: use the machine's serial console and
  `sudo systemctl restart odile-agent`, or re-run the apply with
  `--write --password-file /run/odile-demo-secret`.
- **Caddy is broken**: `caddy validate --config /etc/caddy/Caddyfile`; the
  apply backs the file up (`/etc/caddy/Caddyfile.odile-demo-public.bak`) and
  rolls back on failure.
- **The account is locked** (too many wrong passwords): the tenant's own
  recovery path is `/first-visit/reset` with the recovery code on the machine;
  then re-run the deploy to re-assert the judge hash.
- **A config key went back to the installer's value**: `odile-release-setup.service`
  converges **once** now (t_ee5aac66): its first run writes the signed
  installer's blocks and a done marker at `/var/lib/odile/release-setup.done`,
  and every later boot prints `already converged` and changes nothing. If the
  marker is missing on a machine that has run the installer, the script
  backfills it from its own log rather than run the single-use first-visit
  token again. To retry a failed first run on purpose:

  ```sh
  sudo tail -n 40 /var/log/odile-release-setup.log   # what the first run did
  sudo rm -f /var/lib/odile/release-setup.done
  sudo sh /usr/local/lib/odile-image/odile-release-setup.sh --force
  ```

  `--force` is the explicit retry: removing the marker alone is **not** enough,
  because the script finds its own log's earlier `=== odile-release-setup` line
  and backfills the marker instead of running the installer (measured
  2026-11-14). A forced run appends a new run block to the log and writes the
  marker with its exit code, so the next boot reads `already converged` again.

  The demo's old boot unit (`odile-demo-public`, which re-ran `apply --tune`
  after every boot) is removed by the apply and does not exist on the machine.
  Nothing re-asserts the demo config or the `/verify` lede at boot; the deploy
  is the only writer.

## 7. The seed's first turn (the part that is not obvious)

A session-scoped RPC only reaches a session the gateway holds **in memory**, so
the Webapp resumes the stored session when the judge opens it and only then
submits the prompt. Measured 2026-11-13: `session.resume` of the seeded id loads
its two messages and a `prompt.submit` after it runs a normal turn; a
`prompt.submit` *before* the resume answers `4001 permission_denied` ("session
not in memory") — that is what an operator scripting the console sees, and it is
not the judge's path.

Two state rows make the resume work, both written by the apply:

- `gateway.local_policy.v1:<id>` — the receipt. The tenant's
  `session_local_recovery.owned_receipt` refuses another home's profile
  (`profile_mismatch`), so the receipt carries this machine's `profile_id` and a
  `legacy_session_id` binding the HQ demo's constant id, plus the
  `gateway.local_legacy.v1:<id>` lineage row `legacy_lineage_root` requires.
- the receipt's frozen `config_json`. A receipt stores the whole config it was
  minted under; the HQ template's copy routed inference at
  `http://127.0.0.1:8011/v1` (the HQ meter), so a resumed turn on this machine
  failed with `APIConnectionError … base_url=http://127.0.0.1:8011/v1`. The
  apply re-points that snapshot at Near AI and renames its provider row to
  `Near AI`; it carries no turn or session limit (the demo sets none).
  `apply_demo_public.py --self-test` asserts no `8011` survives, that the
  row's name is the demo's, and that the snapshot carries no cap.

`check_demo_public.py` always proves the judge's first action: it resumes the
seed as `judge`, submits a prompt and reads the marker back out of the
transcript.

## 8. Evidence for this card (2026-11-13)

| What | Where |
|---|---|
| DNS record | `create_dns.py` output: `same A demo.odile.fyi -> 203.0.113.87 (proxied=False)` |
| The Caddy block | `deploy/caddy/demo-tenant-site.caddy` sha256 `64fd9d64…` (3,213 B, no mutation or self-update matcher); `deploy/demo-host/evidence/odl-503-tenant-report-2026-11-13T1710.json` shows the installed copy and the live block both hash to the block as it stood then (`18b98297…`), one marker pair, `residue_lines: 0`, caddy `active` |
| Signed-in screenshots | `deploy/demo-host/evidence/odl-503-login-1440.png`, `odl-503-login-390.png` (the committed and live page, t_9896a577's artwork build, 94a22b03…), `odl-503-signed-in-1440.png`, `odl-503-signed-in-390.png` (the pinned "Start here — Odile demo" transcript with the check's turn); the judge page: `odl-503-judge-1440.png`, `odl-503-judge-390.png` |
| The gate, measured | `odl-503-check-2026-11-13T2238.txt`: PASS 43/43 at the rebased head (base `87a0f75`) — the 302, the judge sign-in, the seed's reply, the write probes (422 / 422 / 401, control 200), the two Caddy hashes, `/verify` = `/attestation`; earlier `odl-503-check-2026-11-13T1709.txt` was PASS 43/43; `odl-503-deploy-2026-11-13T1706.txt`: PASS, installed == live == committed |
| The tenant's own report | `https://demo.odile.fyi/verify`, measurement `9f01725afd71968f74e6dec5…`, equal to `/attestation`'s |
| The provider row | `odl-503-tenant-report-2026-11-13T1710.json`: `Near AI` at `https://cloud-api.near.ai/v1`, key env `HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY` |
| Fallback | `./deploy/demo-host/create_dns.py --target 203.0.113.139` (see §5: the HQ copy is retired) |

### Evidence for finding 1 (2026-11-14)

| What | Where |
|---|---|
| The served page | `curl -s https://demo.odile.fyi/verify` sha256 `c08c590e…`, 42,077 B; its first paragraph is `deploy/demo-host/verify-lede.html`; measurement `9f01725a…f137` equals `/attestation` (fetched `2026-11-13T10:28:34Z`) |
| The customer row is gone | `grep -c 'No one at Odile can log in'` on the served page = 0 |
| The customer page | `web/verify.html` unchanged, sha256 `0d16c494…`; the release renderer test shows its `/verify` still opens with the general row |
| The machine's template | `/usr/local/lib/odile/verify.html` sha256 `aa0bf368…`; `apply_demo_public.py --report` → `"demo_lede":true,"customer_lede":false,"rollback":true`; the release page kept at `…/verify.html.release`, sha256 `9622b4e7…` (the page the release installer wrote) |
| The boot unit | `systemctl start odile-demo-public` → `Result=success` at 02:31Z; journal `same /usr/local/lib/odile/verify.html (the demo lede)` … `demo converged`; the tree's `ExecStart=… --tune --keep-hash --write`. A concurrent deploy at 03:29Z wrote the pre-merge template back (`--tune --keep-hash`, no `--write`); t_ee5aac66 removes the unit and makes `odile-release-setup` converge once, after which a boot no longer rewrites the template and the deploy's `--write` is the only writer |
| The gate, before the merge | `deploy/demo-host/evidence/odl-503-followup-check-2026-11-14.txt`: PASS 44/44 |
| The gate, after merging main | `deploy/demo-host/evidence/odl-503-followup-check-2026-11-14T0405.txt`: 46 passed, 1 failed — the failure is the 25-turn cap t_ee5aac66 removes; every `/verify` line and the measurement pass |
| Screenshots | `odl-503-followup-verify-1440.png`, `-390.png` (viewport) and `-1440-full.png`, `-390-full.png`; at both widths `scrollWidth == clientWidth`, 0 `<img>`, body `rgb(234,223,196)`, lede `rgb(74,65,54)` |

### Evidence for the verifier command (2026-11-14)

| What | Where |
|---|---|
| The page's line | `https://demo.odile.fyi/verify` 200, sha256 `6c6fe7aa…`, 43,082 B at 20:15Z; its `<!--demo-check-->` section is `deploy/demo-host/verify-command.html` (sha256 `46c95b39…`) with the measurement it serves, `9f01725a…f137` = `/attestation` |
| The line in a judge's shell | `check_demo_public.py` extracts it and runs it from the published bundle: `expected PASS matches --expect …` / `PASS — the CPU signed this report and the launch measurement matches the pin.`, exit 0 |
| The plain form is the defect | `./odile-verify https://demo.odile.fyi` prints `a tenant run needs --expect <measurement>`, exit 2 — the pin's `source` is the HQ record |
| The pages that hand it over | `README.md` (a `/verify` pointer, no bare command) and the demo page; `/judge` is a 301 since ODL-901, so `web/judge.html` no longer ships |

### The published password's reach (2026-11-14)

| What | Where |
|---|---|
| The shell, the root and the key | The six-command block in §3, with its outputs: `id -un` → `tenant`, `sudo -n id -un` → `root`, the `0600` 348-byte `.env` owned by `tenant` with one `HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY` (`grep -c` → 1), the meter (`masked` at 01:4xZ, `not-found` after t_e237e261), `/var/lib/odile` (`odile-meter` at 01:4xZ, `root` at 17:54Z) and no `nft`; measured 2026-11-14 01:39–01:46Z, re-checked 15:05–15:35Z and re-measured 17:54:57Z through `tenant_console.py` with the published password. The threat note that carried these facts as prose is deleted history (t_9d7ee536, commit `1257777`); this row and the §3 block are their surviving record. |

**The freeze removal (t_69f1a5f3, 2026-11-14).** The committed block is
`deploy/caddy/demo-tenant-site.caddy` sha256 `64fd9d64…` (3,213 B, matchers
`['apidocs']`). Rendered into the machine's own 6,962-byte Caddyfile it comes
out at 6,228 B with the same block bytes, and `caddy validate` prints
`Valid configuration`. The check's write probes pass on the live deploy —
`PUT /api/config` 422, `POST /api/cron/jobs` 422, unauthenticated `PUT` and
`POST /api/hermes/update` 401, control read 200, and a judge's
`DELETE /api/sessions/<id>` 200 followed by 404. Until the next deploy the same
run still fails its three byte assertions, because the machine serves the
pre-freeze-removal block (`18b98297…`) and the installed `apply_demo_public.py`
predates `caddy.matchers`: `41 passed, 3 failed`, and the three are named in the
`== the Caddy block` section.

**The judge's data (t_ee5aac66, 2026-11-14).** A deploy now changes no
session and deletes none: a session created before the deploy (2 messages) and
a past session (31 messages) kept their row, flags and message digest
byte-for-byte; the seed was absent before the deploy and was written once
(`local-931d9e32…`, pinned, two messages) — and a re-created seed lifts the
delete's `gateway.retired_session.v1.<id>` tombstone, or its first turn
answers `4001 not_found` (measured). The config and `.env` were byte-unchanged
across the deploy. The old boot unit is removed (`systemctl status
odile-demo-public` → `Unit … could not be found`, no `odile-demo-*` unit or
timer). `odile-release-setup.service` converges once: two `systemctl start`s
after the new script was installed printed `already ran … marker backfilled`
and `already converged`, and `webapp.public_url`, the provider block, `.env` and the
setup log stayed byte-identical. A judge renamed, archived and deleted sessions
over the same socket the Webapp uses (all three on a Webapp-created session,
which carries a receipt; a receipt-less row answers 503 — §4). `check_demo_public.py --password-file …
--sessions` is PASS 53/53. Evidence: `docs/ops/evidence/t_ee5aac66/`.

Round 2 (2026-11-14, the check's session cleanup and the release-setup
retry): `docs/ops/evidence/t_ee5aac66/session-cleanup-and-retry.md` — the
check is PASS 59/59 with `17 before, 17 after, 8 session(s) opened`, every id
gone, the deploy deletes its three console sessions (`18` before / `18`
after), and the forced retry runs the installer.

Round 3 (2026-11-14, the merged head `main 0a4b726`):
`docs/ops/evidence/t_ee5aac66/round3-merged-head.md`. The branch's resolved
tree is the verifier's proven `daf788d9…` tree; the gate is green; a subject
created before the deploy kept its row, 2 messages and digest `cbd5b17d…`
across the deploy and a `systemctl start odile-release-setup.service` (whose
journal says `already converged … nothing to do`, `Result=success`); the
config, `.env`, `/verify` template and setup log were byte-unchanged; the unit
is not found and there is no demo timer; the check is PASS 57/57 with `76
before, 76 after, 8 opened all gone` (the two meter rows main removed are no
longer counted); rename/delete/archive were re-measured.

Round 3b (2026-11-14, after t_9d7ee536 and Round 9 merged — `main 8d395d7`):
`docs/ops/evidence/t_ee5aac66/round3b-merged-head.md`. The rebase took main's
HQ retirement and the seed path move (`deploy/demo-host/seed/`) and kept this
card's shape; the gate is `Ran 1595 tests — OK` (the HQ tests are gone) with
the linters green; a new subject kept its row, 2 messages and digest
`aeb3d3c1…` across the deploy (reconnect 36s) and a `systemctl start
odile-release-setup.service` (`already converged … nothing to do`,
`Result=success`), with the config, `.env`, `/verify` template and setup log
byte-unchanged; the unit is not found and there is no demo timer; the check is
PASS 57/57 with `86 before, 86 after, 8 opened all gone` (a first run failed
only on the dashboard sign-in rate limit, 400, and passed 57/57 after the
documented wait); rename/delete/archive were re-measured.

## 9. The boot image is a footgun — read before any power action

A Servury **power action boots whatever custom image is staged** in the
boot-image slot (`next_start: custom`), not necessarily the release the machine
ran before. On 2026-11-13 this machine had `f7bc0ceb…` (an odlukitest UKI, built by card
t_c1e0f336) staged; a restart for an unrelated Caddy outage booted it, the guest configured
odlukitest's address, and — because the seal no longer matched — the initramfs
re-keyed the LUKS volume and shredded the first-boot bootstrap key. The tenant's
own UKI then asked for the disk passphrase, which Odile does not hold.

Check before every restart:

```sh
curl -sS -H "X-Reseller-Key: $(...) " \
  'https://servury.com/api/reseller.php?action=boot-image&id=31a63bbb'
```

`next_start` must be `custom` with
`image.url=https://odile.fyi/releases/m1/odile-tenant.efi` and
`sha256=5cb318083d2ce0…`, or `release` for the vendor image. To put the
tenant's own image back (the route measured on 2026-11-13, download ready in
10 s):

```sh
POST ?action=boot-image&id=31a63bbb
{"url": "https://odile.fyi/releases/m1/odile-tenant.efi",
 "sha256": "5cb318083d2ce0555132a008b0d5dfb7613e6bd0a3fb19d0ae29adac8b1b6a5e"}
```

then `POST ?action=power {"action":"restart"}` and read `?action=serial` until
`bb-sealed-unlock: root_crypt opened with the SNP-derived key` and the
`odile-release-setup … · odl-7971a8 · instance 31a63bbb` header. The guest's
identity comes from the UKI's own command line; the wrong UKI shows
`odlukitest`/`.117` there.

If the seal is broken, the only two openings are the passphrase (the user's, in
the panel console — Odile holds no copy) or a disk rebuilt from
`/srv/odile-releases/m1/odile-tenant-sealed.raw`.

## 10. After a reinstall or a full first boot

`odile-release-setup.service` converges once (t_ee5aac66), so a boot no longer
re-runs the installer. A boot that ends `rc≠0` (for example after a restore)
leaves the machine with the **tenant's own Caddy block only** —
`demo.odile.fyi` then has no site, Caddy's TLS handshake for it fails with
`tlsv1 alert internal error`, and the Webapp is reachable on
`https://odl-7971a8.odile.fyi` instead. There is no boot re-assert to repair
it; run the deploy against the machine's own name:

```sh
./deploy/demo-host/deploy_demo_public.py --base https://odl-7971a8.odile.fyi \
  --access-user tenant --access-password-file <published> \
  --judge-password-file <published>
```

A machine that has just finished its first boot is behind the **first-visit**
gate (`/login` 401 for every account). Its token is the one in the boot UKI's
command line — recover it with
`objcopy -O binary --only-section=.cmdline <uki>.efi /tmp/c; tr -d '\0' </tmp/c`
and open `/first-visit?t=<token>`; setting the sign-in with the published
password lets the deploy in as `tenant`. The deploy then writes the `judge`
account, the demo block and the seed (when the row is absent), and the demo is
back without a reboot (measured 2026-11-13: deploy PASS in 48 s).
