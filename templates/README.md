# `templates/` — everything a machine renders from

| file | what it is | written to |
|---|---|---|
| [`verify.html`](verify.html) | the claim table `verify/verify.py` renders (ODL-236): every verification claim, with where it was checked | via `verify.py --out` / `--serve`; the guest's `/verify` is `web/verify.html`, rendered by `installer/first_visit.py` |
| [`recovery-kit.md`](recovery-kit.md) | the operator's recovery kit, rendered by `scripts/recovery_kit.py` at install time | the kit the operator hands over |
| [`tenant-agents.md`](tenant-agents.md) | the tenant's standing context, embedded in the installer | `$HOME/AGENTS.md` |
| [`config.yaml`](config.yaml) | the Hermes tenant profile, seven managed blocks merged in place | `$HERMES_HOME/config.yaml` |
| [`skin.yaml`](skin.yaml) | the Webapp's Odile skin, merged with `display.skin: odile` | `$HERMES_HOME/skins/odile.yaml` |
| [`status.html`](status.html) | the public status page: uptime counts, no tenant named | `/var/lib/odile/status.html`, served at `/status` |
| [`webapp-brand.js`](webapp-brand.js) | the Webapp's brand re-assertion, after every Hermes build | `odile-brand.js` in the Webapp bundle, beside the `index.html` it links |
| [`cron/morning-brief.json`](cron/morning-brief.json) | the morning-brief job template | a cron job on the machine |
| [`cron/watchdog.json`](cron/watchdog.json) | the silent-watchdog job template | a cron job on the machine |
| [`cron/watch.py`](cron/watch.py) | the watchdog script: no-agent, silent unless the value changes | `$HERMES_HOME/scripts/watch-<slug>.py` |
| [`emails/ready.txt`](emails/ready.txt) | the ready email: URL, measurement, first-visit link | the invite mail |
| [`emails/support.md`](emails/support.md) | the support reply bodies and structure rules | a reply from `help@` |

## The verify page

`verify.html` is the template behind the claim table `verify/verify.py` renders
(see [`verify/README.md`](../verify/README.md)). The page a guest serves at
`/verify` today is the installer's embedded copy of `web/verify.html`, rendered
by `installer/first_visit.py` around `/var/lib/odile/access-report.txt`; this
template is the fuller one.

```sh
sudo /usr/bin/python3 verify/verify.py             # the page on stdout (no flag)
sudo /usr/bin/python3 verify/verify.py --json      # the same rows
```

The page exists so a sceptical stranger can read the machine's records for
ninety seconds: the live report, the measurement, the boot key's fingerprint,
every `authorized_keys` line, root lock, password authentication, the accounts,
the LUKS keyslots, the boot/unlock state, the support-grant state and the signed
manifest. Every row is tagged `checked from your laptop` or `read from the
machine`; a root shell can change the machine rows, the CPU's signature is the
half it cannot fake. The three deeper checks a laptop can run are in
[`verify/README.md`](../verify/README.md) §1–§2.

## The recovery kit, and the recovery file

Two different files, and the difference matters:

| | the kit (`recovery-kit.md`) | the recovery file (`/etc/odile/recovery.txt`) |
|---|---|---|
| when | before the install, by the operator | at the first visit, on the machine |
| rendered by | `scripts/recovery_kit.py render` | `installer/first_visit.py` |
| carries | the setup token, the machine id, the measurement, the signing key | the backup password and the certificate SHA-256 |
| where it lives | in the operator's hands | `0600 root` on the machine, downloaded from the first-visit page behind the recovery code |

The file is the only copy of the backup password. One recovery code resets
both: your account password at the account page and your machine's sign-in on
the machine. There is no emailed reset. While the machine is still running, the
agent can replace the code and show you the new one. While the machine is
running the agent can show you the file:

```sh
sudo cat /etc/odile/recovery.txt
```

`backup.py verify` reads that file and proves the password opens the backup
repository — see [`scripts/README.md`](../scripts/README.md). If the machine is
gone, the copy the user kept is the one that opens the backup.

The kit's own sentences and the first-visit page are `docs/CLAIMS.md` rows; do
not strengthen them here.
