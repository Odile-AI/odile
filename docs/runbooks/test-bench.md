# The test bench — test.odile.fyi

**Status:** v0, built by ODL-339 (A1) · **Deployer:** `deploy/bench/deploy_bench.py`
**Host:** the HQ box, beside production · **Address:** `test.odile.fyi`
**Serves:** the same account site as `odile.fyi` and a second seed service

The bench is a second environment on the same machine: the same `app.py`, the
same `seed/seed.py` and the same Caddy, with their own values, ports, database,
seed config and release tree. A development deploy goes to the bench first, so
a card never lands on `odile.fyi` to find out whether it works.

It is **not** a tenant machine and **not** a fork. There is one code tree; the
bench is a second set of values, two units and one Caddy block. Ports 8082
(site) and 8083 (seed) are the bench's; production keeps 8080/8081.

## 1. What exists

| Piece | Bench | Production |
|---|---|---|
| Release tree | `/opt/odile-test/releases/<id>`, `current` and `previous` symlinks | `/opt/odile` |
| Database | `/var/lib/odile-test/waitlist.db` (empty at creation; `init_db()` writes the schema) | `/var/lib/odile/waitlist.db` (real addresses — never copied) |
| Attestation report | `/var/lib/odile-test/attestation.json` | `/var/lib/odile/attestation.json` |
| Roadmap cache | `/var/lib/odile-test/roadmap.html` | `/var/lib/odile/roadmap.html` |
| Runtime values | `/etc/odile-bench/bench.env` (0644) | inside `odile.service` |
| Seed config | `/root/.odile/seed-config.test.json` (0600) | `/root/.odile/seed-config.json` (0600) |
| Seed key | `/root/.odile/seed-signing.pem` via `LoadCredential` (same key, never copied) | same file |
| Units | `odile-bench.service` (site), `odile-bench-seed.service` (seed) | `odile.service`, `odile-seed.service` |
| Caddy | a `# BEGIN odile-bench` … `# END odile-bench` block | the `odile.fyi` site |
| DNS | `test.odile.fyi` A, DNS-only | `odile.fyi` A, DNS-only |

The bench's seed config is **derived** from the production one (the box holds
one inference key and one relay secret) with two changes: the backup repository
is `/tenants/test-{slug}` and an `environment: bench` marker. It is written
once, at 0600, and never committed; delete it to have the deployer rebuild it.

The roadmap cache is generated once by the first apply, when
`/var/lib/odile-test/roadmap.html` is absent; the bench has no roadmap timer,
so the page stays at that generation until you delete the file and re-run the
deploy. A missing file is the app's own one-line 503 (`roadmap not generated
yet`), not an error page.

`test.odile.fyi` is a name inside the `odile.fyi` zone, not a new hostname and
not a new box. The card and `docs/backlog.yaml` print `203.0.113.139` as the
documentation stand-in for the HQ address; the deployer does not contain the
real address — it discovers the machine's own IPv4 (`ip -4 route get
203.0.113.1`) and publishes that. `--dns-ip <address>` overrides it.

## 2. Before the first apply

The deployer needs root (units, `/etc/caddy`, `/var/lib/odile-test`), the
`odile` service account (created by `installer/odile_install.py`), and these
three files:

```sh
ls -l /root/.odile/seed-config.json          # production seed config, 0600
ls -l /root/.odile/seed-signing.pem          # the seed key, 0600
ls -l /root/.odile/cloudflare_dns_token      # Zone:DNS:Edit on odile.fyi only, 0600
```

The Cloudflare token is read at run time and never printed. If it cannot write
the `test` label, the deploy stops with the API's own error — comment on the
card, do not invent a hostname.

## 3. Deploy

```sh
./deploy/bench/deploy_bench.py --dry-run        # names every write, writes nothing
./deploy/bench/deploy_bench.py                  # apply this checkout
./deploy/bench/deploy_bench.py                  # a second time: `bench: nothing to do`
./deploy/bench/deploy_bench.py --source <tree>  # apply another checkout (rollback test)
```

The dry run prints one line per step: `would create` for a directory, database
or config; `would write` for `bench.env`, a unit, the Caddyfile and the DNS
record; `would copy` for a release; `would point` for the `current` symlink;
`would generate` for the roadmap cache; and `would probe` for the three
checks. It writes no file and runs no mutating command.

An apply is idempotent. A converged host prints `same` for every step and ends
`bench: nothing to do`, with no `systemctl` restart and no Caddy write. The
release is named by the sha256 of the source tree's files, so applying the
same checkout twice reuses one release directory.

Both units run the release through `/opt/odile-test/current`, so a rollback is
a symlink flip plus two restarts — the env file, the seed config and the Caddy
block do not change.

## 4. Verify

```sh
dig +short test.odile.fyi
curl -sS -o /dev/null -w '%{http_code}\n' https://test.odile.fyi/
curl -sS -o /dev/null -w '%{http_code}\n' https://test.odile.fyi/seed   # 405, POST only

sqlite3 /var/lib/odile-test/waitlist.db 'select count(*) from tenants'
stat -c '%a %U:%G %n' /var/lib/odile-test/waitlist.db /root/.odile/seed-config.test.json

systemctl is-active odile-bench odile-bench-seed
readlink -f /opt/odile-test/current /opt/odile-test/previous
grep -n 'BEGIN odile-bench' /etc/caddy/Caddyfile
```

The site is the account page, not a redirect: `curl -sS https://test.odile.fyi/`
contains the same page `/` renders on production, with `Server: odile-bench`
and `X-Robots-Tag: noindex, nofollow` from the bench block.

The deployer also measures production around every apply and rollback: the
sha256 of `https://odile.fyi/`, `/roadmap` and `/attestation`, and
`systemctl show -p NRestarts odile.service`. It prints each hash and fails the
run if any of them moved.

## 5. Certificate and renewal

Caddy obtains the certificate for `test.odile.fyi` itself on the first
`systemctl restart caddy` that loads the bench block; there is no certificate
file and no cron job to manage. Automatic renewal is Caddy's own: it renews
each certificate roughly a third of its lifetime before expiry, without a
restart and without a unit of ours.

```sh
journalctl -u caddy --since '10 minutes ago' | grep 'test.odile.fyi'
echo | openssl s_client -connect test.odile.fyi:443 -servername test.odile.fyi 2>/dev/null \
  | openssl x509 -noout -subject -dates
```

The journal line is `certificate obtained successfully` with
`identifier=test.odile.fyi`. DNS is DNS-only (not proxied), so the certificate
is the box's own and the ACME challenge reaches it directly. If the name does
not resolve, Caddy retries with backoff and the deploy's probe fails after
`CERT_SECONDS` — fix the record, do not hand-copy a certificate.

## 6. Rollback

```sh
./deploy/bench/deploy_bench.py --rollback --dry-run   # names the flip
./deploy/bench/deploy_bench.py --rollback             # previous release back
systemctl is-active odile-bench
```

A rollback returns the release that was live before the last apply and swaps
`previous` to the release it just left, so it is itself reversible. It restarts
both units because the bytes they run moved, then probes the bench and
re-measures production. A host that has never had two releases has no
`previous`, and the command exits 2 with that sentence rather than guessing.

There is one deploy and one rollback in A1. The release tree keeps every
release directory it has copied; remove old ones only after checking
`readlink current` and `readlink previous`.

## 7. Destroy the bench

The card asks for a deploy and a rollback, not a destroy, so this is a printed
path rather than an exercised one. Run it in this order:

```sh
systemctl disable --now odile-bench.service odile-bench-seed.service
rm -f /etc/systemd/system/odile-bench.service /etc/systemd/system/odile-bench-seed.service
systemctl daemon-reload
# Remove the marked block from /etc/caddy/Caddyfile (keep every other byte),
# or restore the newest /etc/caddy/Caddyfile.bak-bench-* copy, then:
caddy validate --config /etc/caddy/Caddyfile && systemctl restart caddy
rm -rf /opt/odile-test /var/lib/odile-test /etc/odile-bench
rm -f /root/.odile/seed-config.test.json
# Delete the test.odile.fyi A record through the same Cloudflare token.
```

`/root/.odile/seed-signing.pem` is **not** removed: production's seed service
uses it too.

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `bench: deployed` but `https://test.odile.fyi/` answers 000/503 | certificate not issued yet, or DNS not resolving | `journalctl -u caddy`, `dig +short test.odile.fyi`; re-run the deploy once the record resolves |
| `odile-bench` inactive, `journalctl -u odile-bench` shows `address already in use` | 8082 is taken by something else | `./scripts/check_bench.sh --probe` names the holder; the deployer refuses it before writing. Kill the holder, or let the deployer reap it when its `.worktrees/<card>` directory is gone |
| `odile-bench-seed` fails `seed config … missing` | `/root/.odile/seed-config.test.json` was deleted while the unit was running | re-run the deploy (it recreates the file) and `systemctl restart odile-bench-seed` |
| `caddy validate rejected …; restored it` | the block was edited into invalid Caddy syntax | the live file is unchanged; fix `deploy/bench/bench.caddy` and re-run |
| `error: production moved during the bench run` | a production deploy or the roadmap timer ran mid-apply | re-run the bench deploy when production is quiet; the bench itself is fine |
| `error: the Cloudflare token cannot see the odile.fyi zone` | wrong or rotated token | replace `/root/.odile/cloudflare_dns_token`; do not edit DNS by hand |

## 9. The liveness probe, and the five-minute timer (ODL-936)

On 2026-11-21 `test.odile.fyi` answered `404` for half a day while `/health`
answered `200`: `odile-bench.service` had restarted 17,060 times because an
orphaned `seed.py serve` from a deleted card worktree held `127.0.0.1:8082`.
`check_bench.sh` is manual and `/health` is not the bench's page, so nothing
noticed. Two things now do.

**The probe, in one line.** It needs no record, no tenant and no database:

```sh
./scripts/check_bench.sh --probe
# bench probe: OK https://test.odile.fyi/ -> 200 server: odile-bench (the account site); 127.0.0.1:8082 pid 2109529 /usr/bin/python3 /opt/odile-test/current/app.py
```

Green requires three things at once: `200`, `server: odile-bench`, and the
account page's own join form in the body. The header alone is Caddy's — the
bench block sets it — so it proves the name reached the bench, not which
process answered; a foreign server on 8082 answers `200` through the same
Caddy. A red probe prints exactly one line and exits 1, naming the pid and
command that hold the port (or `nobody listening`). `BENCH_URL` and
`BENCH_PORT` override the target for a drill.

**The timer.** `deploy/bench/deploy_bench.py` installs and enables
`odile-bench-probe.service` and `odile-bench-probe.timer`; the timer runs the
probe every five minutes (`OnCalendar=*:0/5`, `Persistent=true`). A red tick
raises one alert in the existing HQ path — the `odilectl.health` ledger, the
outbox and the configured transports, `slug bench`, `kind down` — with the
probe's line as the symptom; the same alert stays open (no repeat) until a
green tick resolves it. A stopped unit and a foreign listener each produce a
red line and one ledger row.

```sh
systemctl list-timers odile-bench-probe.timer
systemctl start odile-bench-probe.service      # one tick by hand
journalctl -u odile-bench-probe.service -n 20
/usr/bin/python3 -m odilectl health            # the open/resolved bench alert
```

**The port guard.** Before its first write, every apply and rollback inspects
8082 and 8083. A holder inside the bench unit is the healthy case. A holder
whose working directory is a `.worktrees/<card>` that no longer exists is the
2026-11-21 orphan: it is named and killed (SIGTERM, then SIGKILL). Any other
holder stops the deploy with exit 2, naming the pid, the command and its cwd —
production is not measured and nothing is written.

## 10. An image re-cut re-records the baseline in the same card

`docs/evidence/a1-bench-baseline.json` records the tenant's expected launch
measurement. A tenant image re-cut changes that number, and from that moment
`check_bench.sh` is red on two checks until the record moves with it. A re-cut
card is therefore not done until it has re-recorded the baseline and the full
check is 16/16, in that card:

```sh
# the pin's 96 hex is the tenant's own /verify value, after the re-cut boot
./scripts/check_bench.sh --record --expect <the pin's 96 hex>
./scripts/check_bench.sh                        # must end: check_bench: PASS (16 checks)
```

The record is a capture: it also holds the three production pages as served and
`NRestarts`. Never edit it by hand; `--record` writes it atomically. The current
value at 2026-11-22 is `2caf4a5b…` (the 2026.11.21 image), up from `674cc40d…`;
`docs/evidence/a1-bench.md` §12 carries the transcript.
