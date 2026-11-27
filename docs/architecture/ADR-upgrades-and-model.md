# ADR-005 — Upgrade inventory and the default-model switch

**Status:** Proposed, with the trigger for rows 1–5 confirmed by Savio on 24
November 2026 (`t_ce72ed5b`): a rebuild is a prompted, clicked, windowed event,
and §5.3–§5.4 are its flow. The inventory (§3) and the app-layer timer default
still wait on A4's exit criterion 2. Savio's 27 November 2026 rulings
(`t_7f88e278`): the heartbeat's one new fact and its one-sentence privacy row
are approved (the row lands as ODL-955, `t_62dd6f8a`), and the booking email is
a notification only — no consent, no reject, no reply-to-decline; booking,
change and cancel are in-app (`t_08885b98`). One thing is still Savio's: the
windowed executor that drives the Servury API (§5.4) · 17 November 2026 · Architect
**Revision 8** — 27 November 2026, §5.3 is re-cut to Savio's 27 November ruling
(`t_7f88e278`, `t_08885b98`): the booking email is a notification, never a
consent, reject or prompt path; every decision — book, change, cancel — is an
in-app act; and the in-app change/cancel that does not exist yet is named as
required work and handed to ODL-961, the one card that owns the HQ door, the
guest verbs, the notice and the copy. The status line records the approval of the
heartbeat row (ODL-955). No other decision changes: the flow's mechanism and
the account-site residual are untouched, and the fact gains only the optional
`action` key the in-app change/cancel needs (§5.3).
**Revision 7** — 25 November 2026, §5's rebuild half is re-cut to the flow Savio
approved on 24 November (`t_ce72ed5b`): a rebuild is prompted in the app, the
click books the tenant's next window, the machine takes a T-0 snapshot and HQ
verifies it before the panel write, the progress page is served by HQ, and the
first visit restores before the sign-in. §5.2 keeps the public-host rule and its
old four-step block moves to the new §5.3 (the card calls that block "§5.2 step
1"; the steps and their numbers are kept, the subsection is not); §5.3 carries
the progress-page stage statement, §5.4 the executor and its gate. §1 gains I5
and I6, §2 gains 2.9 (the facts the flow rests on at this head), §6 gains nine
rows, §8 four items and §9 two paragraphs. The model route is untouched: §3, §4
and the A/B on `t_ce72ed5b` do not move. Every in-text pointer is re-resolved at
`main`'s `592f352` (25 November 2026) and §11 is re-cut there with its commands
re-run, because five of the files §11 reads moved after `8ad9145` — ODL-926 moved
`installer/odile_install.py` by 1,373 lines, `scripts/heartbeat.py` by 289,
`app.py` by 34 and `release/release.json` by 12, and ODL-947 the first-boot
program by 37 — so the installer's line numbers all shift, and Revision 6's "no
merge of `main` after `8ad9145` has moved one of them" is retired rather than
repeated.
**Revision 6** — 24 November 2026, §2 and §11 are re-cut at `main`'s
`8ad9145`, and §2.8's boot-script sentence is corrected: the script has two
mechanisms, not one — an operator's `ODILE_RELEASE_PUBLIC_URL`, then the
measured `odile.public_url=` token — and the sentence now names their
precedence. `main`'s own first-parent history took twenty-eight merges in
`8eb1ceb..8ad9145` (forty-six merge commits are reachable; the others came in
with those twenty-eight), eight of which moved a file whose lines §11 reads —
`1f16e5f` and `74ca6d6` the installer and the release, `991c5fb`, `c75afd8`
and `9585090` the app, `92acb25` the installer, the boot script, the first-boot
token prose, the update runbook, the release and the app, `48000ee` the
first-boot program again, and `8ad9145` the update runbook, the installer and
the release again — and `dcf2a94` re-cut this ADR and nothing else. So every
in-text pointer is re-resolved at `8ad9145`, §2.1's size and self-test count
re-read, and §11's commands re-run with their real output pasted. No other
statement in §2.8 changes, and no decision changes.
**Revision 5** — 22–23 November 2026, §2 and §11 are re-cut at `main`'s
`8eb1ceb`; no merge of `main` since it has moved a file §11 reads:
ODL-341-F1 added `auxiliary` as the seventh managed block, so §2.5's tuple,
its block count and the `dashboard` sentence are corrected and every in-text
pointer is regenerated; §11's commands are re-run and their real output pasted;
§2.1's hash, size and self-test count are re-read at the tree. Five merges of
`main` since the first cut at `ae5a305` moved a file whose lines §11 reads —
`a93520e` cut the `2026.11.22-7` release and moved the installer and the
update runbook, `cbdaa83` moved the boot script, `8c2e912` cut `2026.11.23-1`
and moved the installer and the update runbook again, `db97100` moved the
public app, and `8eb1ceb` moved it again for the account reset — so it is
repeated here. No decision changes.
**Revision 4** — 18 November 2026, review round 3 folded in: `main` merged at
`9a29fdf` (the ODL-910 rewrite is in `main`'s history, so the round-3 baseline
`a6b6bed` no longer resolves); §2 and §11 re-cut at that tree and the two
transcript commands re-run there; two pointers corrected —
`PRE_ONBOARDING_KEYS` is `:228` and the heartbeat body is `:1247`–`:1259`, the
slug at `:1259`. No decision changes.
**Revision 3** — 18 November 2026, review round 2 folded in and `main` merged:
renumbered to **ADR-005** (main's ADR-004 is the backup decision,
`docs/architecture/ADR-backup.md`); §11 and every in-text pointer re-cut at the
merge head; the `dashboard` block named in the applier and in the precedence
rule (§2.5, §4.5, §4.6); the installer re-run's public host stated and fixed
(§4.6, §5.2, §6); and the re-run's snapshot widened from four placement files
to **every file the installer writes**, taken by the installer itself (§5.1).
Revision 2 folded in review round 1: the placements row, the applier's state
machine and record pair, the effort key and the real `OnCalendar`. The status
and the three questions for Savio are unchanged.
**Answers:** ODL-571 — "which layers of a tenant can be updated, and how does the
team switch a tenant's default model without seeing prompts?"
**Short answer:** every layer that is not the boot chain or the OS moves in place
from a signed artefact the guest pulls; firmware, the UKI, the disk image, the OS
packages and the environment are rebuild-and-restore; the machine's NEAR key and
the signing-key anchor are not updatable at all. The default model is a **signed
per-tenant document the guest pulls** — a model name, not a prompt — so the guest
keeps calling `cloud-api.near.ai` with its own key and no Odile service ever sees
a prompt. The machine updates itself on a timer for the layers that can move in
place; a rebuild is **prompted in the app, booked by one click into the tenant's
next window, prepared by the machine itself (a T-0 snapshot HQ verifies before it
writes), and finished by the user** with their own backup password on the first
visit — before the sign-in, so the reborn agent is never a stranger the user can
talk to.
**What Savio is asked to confirm:** (1) the layer table in §3, row by row;
(2) the model route in §4 — a signed document, no proxy in front of NEAR;
(3) the trigger in §5 — **confirmed 24 November 2026** for rows 1–5: the machine
updates itself, HQ never connects, and a rebuild is a prompted, clicked, windowed
event (§5.3–§5.4); (4) the heartbeat's one new fact, `facts.upgrade`, and the
one-sentence privacy row it needs (§5.3) — **approved 27 November 2026**
(`t_7f88e278`; the row lands as ODL-955, `t_62dd6f8a`), with the email ruled a
notification only. One ask remains: (5) the windowed executor that drives the
Servury API and the panel (§5.4), which the authority matrix already puts behind
him.
**Related:** `docs/MVP-V0.1.md` §3–§7, §12 (D7, D14, D19, D25, D27);
`docs/CLAIMS.md` §1 *Inference*, *Money and place* and *Data handling*, §3 gaps
1–3, §4.6, §5 (the machine-dies answer); `docs/runbooks/update.md` §2, §8, §10;
`docs/runbooks/install.md` §4, §6.1; `docs/runbooks/backup.md` §5, §8;
`docs/runbooks/demo-public.md` §6; `docs/architecture/ADR-image-vs-installer.md`
§3 axis 6, §15.2; `docs/architecture/ADR-backup.md` (the ADR-004 number) §6,
§7.1–§7.4; `docs/architecture/PIN-POLICY.md` §6; `docs/product/SPIKE-servury-reinstall.md`
§2, §6, §7 (local-only, like `docs/craft/`); cards ODL-219, ODL-332, ODL-205,
ODL-503, ODL-568, ODL-924, ODL-933, ODL-937, ODL-938, t_d95b8c13, A4.
**Decision owner:** Architect for the mechanism, the document schema and the
applier's interface, and for the rebuild flow's states, the fact's schema and the
booking row. **Savio confirms the inventory and the trigger** because the card
says so and because the trigger changes what a tenant does without its owner
present. No new provider, no new network path, no standing credential on the
tenant, no inbound route to a machine and no change to the access model — the
consent fact rides the route the machine already posts to, and the executor uses
the Servury credential that already exists. The **one** thing that touches a
published sentence is the heartbeat row's enumeration (§5.3), and that is a
Product-lead edit with Savio's approval, not an architecture decision.

---

## 1. The invariant at risk

Six sentences the product is built on, and what this change does to each:

- **I1 — no Odile service sits between the agent and the inference endpoint.**
  `docs/MVP-V0.1.md` §6.1 invariant 4 ("Odile is never in the data path") and
  `docs/CLAIMS.md` §1: "Your prompts go from your machine to the enclave. They do
  not pass through Odile's servers." **This is the invariant the rejected option
  breaks**, and it is why the choice in §4 is not a matter of taste.
- **I2 — one operator identity signs what the machine runs, and the machine
  trusts exactly that key.** `docs/runbooks/update.md` §1: the same Ed25519 key
  signs the installer, the seed responses and the manifest inside every release.
  The model document joins that scope; it does not add a key.
- **I3 — the machine and the record agree.** A4's exit criterion: "The machine
  and the record agree on the installed version, and a stale instance is
  visible." Today the record is *not* ready for a model switch: `manifest.py`
  records the model from the **seed cache** (`scripts/manifest.py:537`), and
  `odile_update` never reads the model at all (zero occurrences at this head).
  A switch without a record fix would make the machine's own manifest lie.
- **I4 — a changed boot image or firmware breaks the seal, and the machine is
  rebuilt.** `docs/CLAIMS.md` §3 gap 3; `docs/MVP-V0.1.md` §5's 15 November
  note. This is what turns three of the table's rows into rebuild-and-restore.
- **I5 — the user is the only keyholder, across a rebuild too.** "No operator SSH
  key, no root password, no escrow" (`AGENTS.md` §"The defining property";
  `docs/CLAIMS.md` §1 *Access*, D-3) is a property of the machine *between*
  rebuilds as well as at handoff. The panel's reinstall sets the rebuilt
  machine's **whole** `authorized_keys` to the key the form is posted
  (`docs/product/SPIKE-servury-reinstall.md` §3), so the window's write is the one
  place an operator key could quietly enter a tenant. The flow posts the tenant
  row's own public key and nothing else; a rebuilt machine's `authorized_keys` is
  one line, the user's.
- **I6 — the data that comes back is the user's, as of the window.** The rebuild
  destroys the disk, so the only copy of the home is the repository. §5.3's T-0
  snapshot is taken after the machine is quiesced and before the write, and HQ
  refuses to write until that snapshot is on the record. The property is
  falsifiable in one line: the snapshot id the machine restores is the id the T-0
  run wrote (§5.3), and the loss window is the minutes between them — not the day
  since the last backup.

## 2. What exists today, at this head

The inventory in §3 is not a wish list; it is a reading of the tree at
`main`'s `592f352` (25 November 2026), the cut §11 is taken at, and every commit
this branch adds after it changes the two ADRs and nothing else (§11). Nine facts
shape it, and each is cited because a later card will change one of them.

**2.1 The app layer already has a signed, transactional mover.**
`update/odile_update.py` (sha256 `2c795cad…`, 2206 lines) fetches
`https://odile.fyi/release.json`, verifies one Ed25519 signature over the exact
bytes, verifies the manifest it names, applies only the app layer, snapshots
once, checks the result, and rolls back on failure. Its refusals are precise:
a release for another kernel (`exit 5`), a downgrade (`exit 5`), a moved
environment (`exit 5`), a bad signature (`exit 3`), an inconsistent release
(`exit 4`). Its own layer table is `docs/runbooks/update.md` §2, and it is the
parent of the table in §3. **`--self-test` prints `self-test: 42/42 passed` at
this head** (§11).

**2.2 The guest half of it is not installed yet.** `grep -c odile-update
installer/odile_install.py` is **0**: the installer places the installer, the
skills, the Webapp, Caddy, the backup unit and the boot record, and it does not
place the update command, its key or a timer; the first tenant's record does not
name one either. `docs/runbooks/update.md` §9 lists
that wiring as the tenant half; the machine-free half was ODL-219, and the first
tenant (ODL-332) did not carry it. So "in place" in §3 means *the mechanism
exists and is proved on scratch roots*, not *it runs on a tenant tonight*. The
build card closes that gap; this ADR does not.

**2.3 The boot chain cannot move in place, by construction.**
`docs/architecture/ADR-image-vs-installer.md` §15.2: the tenant boots Odile's
signed UKI **plus a pre-sealed disk image written by the Servury panel**. The
keyslot passphrase is derived from `SNP_GET_DERIVED_KEY` and sealed to the
measurement, so a new UKI derives a different passphrase and the old disk will
not open. The swap itself is non-destructive (`?action=boot-image`), which is
exactly the trap: the machine starts, fails to unlock, and is a rebuild. The LUKS
**master key** is still the build-time key until `t_28dda3b7` lands and
`t_51ebf972` publishes the re-keyed image (`docs/MVP-V0.1.md` §5 note).

**2.4 The model has three homes today, and none of them is a switch.**
The **seed** carries `model`; the installer renders it into the config
(`installer/odile_install.py:15745`, `model = seed.get("model")`) into **two**
keys — `model.default` (`:12114`) and `custom_providers[0].model` (`:12125`) —
and the template fixes a third setting beside them,
`agent.reasoning_effort: "high"` (`:12119`, because this model rejects Hermes's
default effort); the **manifest** records the model from the seed cache. The
seed cache at `/etc/odile/seed.json` is verified forever and never re-fetched
while it verifies — `cached_seed` reads it (`:14278`) and `load_or_fetch_seed`
only fetches without one (`:14682`) —
and the token that fetched it is spent on use: `_end_live_tokens` ends every
live token for a tenant when the next is minted (`seed/seed.py:1141`), and the
measured command line carries a token valid once for 24 hours
(`deploy/image/odile-firstboot.py:18`–`:21`). So the seed is not a channel: a
tenant cannot get a new seed without a new token, and the seed also carries the
NEAR key and the mail secret.

**2.5 The config is a managed-block file, and the installer wins a tie.**
`MANAGED_KEYS = ("model", "agent", "custom_providers", "dashboard", "webapp",
"onboarding", "auxiliary")` (`installer/odile_install.py:301`; the template's
own header calls it "Seven managed blocks", `:12086`, and lists `auxiliary` at
`:12098`). `merge_block` drops "a current line whose key path the template does
name … even if its value differs" (`:13452`), and `ensure_config` renders the
model from the seed on every run. **A model switch applied by any new mechanism
would be reverted by the next installer re-run** unless the precedence rule in
§4.6 exists. This is the failure this ADR exists to prevent, and it is invisible
to a test that only switches a model.

`dashboard` is the fourth key in `MANAGED_KEYS` and the sixth the template
gained: the four in `PRE_ONBOARDING_KEYS` (`:309`), then `onboarding`, then
`dashboard` after the first tenant (t_d95b8c13), then `auxiliary` (ODL-341-F1)
as the seventh. It is the one a line-level rewrite is most likely to drop: the
template carries `dashboard.public_url` at `:12136` beside `webapp.public_url`
at `:12142`, and a config written before it has the earlier four
(`PRE_ONBOARDING_KEYS`, `:309`). Both hosts name the same machine, and neither
is a fallback for the other.

**2.6 What HQ already knows, and what it does not.** HQ's heartbeat contract
accepts an optional `versions` map of names to strings (`odilectl/health.py:964`),
and the approved privacy row already names "versions" among what a heartbeat
carries. But the tenant's own sender is the installer's embedded
`backup.py heartbeat`, and it sends only the backup facts and the slug
(`installer/odile_install.py:2220`–`:2232`): no app release, no model, and none of
the other facts the row lists. `scripts/heartbeat.py` is the richer sender, with
a `versions` map at `:279`, and the tenant does not run it. HQ's one drift
detector, `release_report` (`odilectl/health.py:1020`), compares the **boot**
release it intended with the one the guest reports — there is no app-layer or
model analogue. §8 lists the signal; today it does not exist.

**2.7 The installer's placements have no mover, only a repair.** The installer
writes more than its own bytes: `/etc/systemd/system/odile-agent.service`
(`installer/odile_install.py:16195`), the `# BEGIN odile-<slug>` block in
`/etc/caddy/Caddyfile` (`ensure_caddy_site`, `:14926`–`:14977`, kept only if
`caddy validate` passes), the two backup units (`ensure_backup_units`, `:16145`,
write at `:16151`), the `/etc/odile` files the machine reads (`support-key.pub`
`:16028`, `backup-repo.txt` `:16031`, `apt-pins.list` `:15155`), and the
tenant's own export command (`/usr/local/bin/odile-export`,
`ensure_export_program` `:16098`, write `:16117`). `odile_update` writes the
installer's bytes and never runs it — its own docstring says so
(`update/odile_update.py:996`, "a file the next install reads, not a running
process") — and the image's converge script refuses a second run by design
(`deploy/image/odile-release-setup.sh:106`, gated on the marker at `:86`; only
`--force` ignores it, `:98`). So today a changed unit, Caddy block or
`/etc/odile` file reaches a tenant only on a manual repair re-run
(`docs/runbooks/demo-public.md` §6). That is a layer without a row, and §3's
row 15 is it.

**2.8 The same re-run can revert the owner's public host.** The tenant layout
takes its public name from the flag or the slug's default host —
`args.public_url or public_url_for(name, args.seed_url or DEFAULT_SEED_URL)`
(`installer/odile_install.py:18474`, re-derived after the seed at `:18681`) —
and `render_tenant_config` writes that value into both `dashboard.public_url`
(`:12136`) and `webapp.public_url` (`:12142`). Because `merge_block` drops the
live line the template names (`:13452`), a re-run without `--public-url`
rewrites both keys to the slug URL on a tenant whose owner moved the host
(ODL-503); the Webapp then answers `400 Invalid Host header` for every request
to the owner's name — the t_d95b8c13 outage. The boot script's comment claims
the opposite ("keeps it when this script is re-run by hand",
`deploy/image/odile-release-setup.sh:140`–`:144`), but it has two mechanisms and
neither reads the host the machine already serves: `ODILE_RELEASE_PUBLIC_URL`
(`:156`), which an operator sets on a re-run and which ranks first, and the
measured `odile.public_url=` token it parses from `/proc/cmdline` (`:129`, the
ODL-340 follow-up), which ranks between the operator's setting and production's
`<slug>.odile.fyi` default (`:146`–`:149`) and reaches the installer as
`--public-url` (`:206`). A machine with neither — production, or a re-run that
sets nothing — gets the slug URL. A trigger that re-runs the installer without
fixing this would create the outage it is meant to prevent; §5.2 fixes it.

**2.9 The rebuild flow can stand on three existing facts, and it walks into
three existing traps.** The facts: the nightly unit's `ExecStartPost` posts the
tenant's status as a signed heartbeat (`scripts/backup.py:797`–`:808`), and that
body already carries `facts.backup.snapshot` and its `at`
(`installer/odile_install.py:2220`–`:2232`), so the T-0 snapshot id needs **no new
fact** to reach HQ; the heartbeat answer is already read by the guest — it is
`{ok, slug, at, received_at}` (`scripts/heartbeat.py:406`–`:409`) and
`heartbeat.py post` prints it (`:522`), so a new answer key is additive; and
`_validated_facts` validates the facts it knows and **ignores the rest**
(`odilectl/health.py:962`), while `heartbeats.body` stores the whole object — an
HQ that does not know a fact accepts and keeps it, which is what lets the HQ half
ship before any tenant can send it.

The traps. **First, a rebuilt machine cannot post.** `record()` refuses a
public key that differs from the one registered for the slug, `409`
(`odilectl/health.py:1179`–`:1182`), and requires `at` to move forward
(`:1191`–`:1196`); `heartbeat_keys` holds one key per slug
(`odilectl/health.py:833`) and nothing in the tree clears it on a rebuild — the
runbook's revoke is a manual `DELETE` (`docs/runbooks/alerting.md` §10
*Rollback*). A rebuilt machine mints a new key at install
(`scripts/backup.py:764` sends the public half on every beat), so without a clear
its first beat, and the post-restore beat the progress page reads, are `409`.
**Second, the sign-in hash collides with the restore.** The first-visit service
verifies against the tenant `.env` (`installer/first_visit.py:118`) and
`/etc/odile` must not be restored (`:76`–`:81`); the home's `.env` **is** in the
backup (ADR-backup §6), so the restore replaces the seed's verifier with the
backup night's copy. **Third, the T-0 id has no carrier into the new machine.**
`SEED_KEYS` is the set of **required** keys (`installer/odile_install.py:253`)
and the only refusal is a missing one (`:14628`), so a new optional seed key is
ignored by an older installer — but nothing writes one today, and
`backup.py restore` takes a snapshot id positionally with `latest` as the default
(`scripts/backup.py:930`), so "restore the T-0" is an argument the caller must
supply, not a behaviour the program has. §5.3 and §5.4 close all three by name.

Two more facts bound the flow. The agent unit stops with `TimeoutStopSec=90` and
`KillMode=mixed` (`installer/odile_install.py:12756`–`:12757`) — that is the "time
it out" bound in the quiesce, not a number this ADR invents. And the window's
upper edge does not exist yet: the tenant backup timer is 03:00
(`installer/odile_install.py:12721`) but the app-layer timer is not installed
(`grep -c odile-update installer/odile_install.py` is 0, §2.2) and the runbook's
unit is still `daily` + 1 h (`docs/runbooks/update.md:1676`), so "before the
04:00 app timer" is a property of §8 item 1, and the quiesce stops the timer
anyway.

## 3. The layer inventory

Three labels, and only three: **in place** (a signed artefact is written and a
process restarted, no rebuild), **rebuild-and-restore** (a new machine is built
and the user's data comes back from the nightly backup), **not updatable** (no
mechanism exists, and by design or by an unbuilt dependency).

| # | Layer | Today | Label | Why, and the path |
|---|---|---|---|---|
| 1 | Firmware — `OVMF.amdsev.fd`, Servury's edk2 build | part of Servury's release; in the measurement | rebuild-and-restore | Not Odile's artefact. A firmware change re-measures every unchanged UKI and breaks the seal; the machine is rebuilt onto the new firmware. ADR-001 §3 axis 8. |
| 2 | Boot image — Odile's signed UKI (kernel, initramfs, command line, seal hook) | `tenant-image.pin`, `releases/m1/odile-tenant.efi`; set by panel or `?action=boot-image` | rebuild-and-restore | Measured, and the disk keyslot is sealed to the measurement. The swap is non-destructive; the disk will not open under it. New UKI + new disk image + restore. |
| 3 | Disk image — Debian rootfs, pre-sealed keyslot, `/etc/odile/apt-pins.list` | written by the Servury panel (no API verb) | rebuild-and-restore | The panel's write is destructive, and the LUKS master key is the build key until `t_28dda3b7`/`t_51ebf972`. A rebuild is the only honest route. |
| 4 | OS packages — the pinned trixie set from `snapshot.debian.org` | recorded in the manifest; installed from the pin | rebuild-and-restore | A release whose pins move is refused as a reinstall (`update.md` §10). `apt` in place works on the machine but leaves the signed record stale; the supported route is reinstall plus restore. |
| 5 | Python venv, npm tree, Hermes toolchain | recorded in the manifest (`python_packages`, `node_packages`, `toolchain`) | rebuild-and-restore | A Hermes commit whose lockfiles or Python pins move is a reinstall, refused before a write. In place only when the environment does not move. |
| 6 | Hermes commit — the agent source | a git checkout under `~/.hermes/hermes-agent` | in place | `git checkout --detach` to a commit the checkout already carries; the environment must not move, and a `webapp` artifact must ride with it. `update.md` §10. |
| 7 | Installer — `/usr/local/bin/odile-install.sh` | its own bytes, 0755 root | in place | A signed artifact hashed against the release and the manifest; restarts nothing. |
| 8 | Skills — `~/.hermes/skills` | signed tarball, canonical hashes | in place | Only skills that changed are written; a skill the release drops is left in place (`update.md` §10). |
| 9 | Webapp bundle — `apps/desktop/dist-webapp` | signed artifact, `hermes-build.json` | in place | Written only when it moves; restarts `odile-agent`. |
| 10 | Default model — `~/.hermes/config.yaml`, `model.default` and `custom_providers[0].model` | rendered from the seed at install | **in place** | A text setting and a restart. **Today there is no route**: the seed is cached and single-use, and the installer reads the cache. §4 adds the signed document. |
| 11 | The machine's NEAR key — `/etc/odile/key.env` | written once from the signed seed, 0600 root | not updatable | One key per user, read by the agent through `EnvironmentFile=`. A new key arrives with a rebuild (A2); there is no rotation path, and rotating in place would be a new credential path with its own decision. |
| 12 | The signing-key anchor — `/etc/odile/odile-signing-key.pub` | the recovery kit's copy, installed at first boot | not updatable | It verifies every release and (from §4) every model document. Rotating it needs a document signed by the **old** key, which is not built. Today a key rotation means a rebuild of every tenant, or a user-initiated support grant on each. |
| 13 | What the CPU measures — firmware, UKI, command line | the live report at `/attestation` | not updatable | No update can make the running app layer measured; that is a new image with dm-verity or a measured disk (`docs/MVP-V0.1.md` §13 item 3). Rows 6–10 and 15 are **signed and hashed, not measured** — gap 1 stands unchanged. |
| 14 | The user's files, memory and sessions — `/home/odile` | the user's workspace | in place | The user's own, by definition. It is what rows 1–5 restore, and the reason a rebuild is a user-facing event rather than a silent one. |
| 15 | Installer placements — the agent unit and the two backup units, `/etc/caddy/Caddyfile`'s `# BEGIN odile-<slug>` block, `/etc/odile/{support-key.pub,backup-repo.txt,apt-pins.list}`, `/usr/local/bin/odile-export`, and every other file a re-run writes (`config.yaml`, `.env`, the skin, the first-visit page) | written by the first installer run; the converge marker stops every later run | **in place** | The signed installer is the applier, and it is idempotent by construction — "every step compares and reports `same`" (`installer/odile_install.py:78`–`:79`). A changed placement is one re-run, then `daemon-reload`/restart if a file moved (`:16652`–`:16654`); §5.1 gives the timer that trigger, and today only a manual `--force` repair does (`docs/runbooks/demo-public.md` §6). The snapshot is the installer's own and covers every file it writes (§5.1, §6). |

**The rule behind the labels.** "In place" requires three things: a signed
artefact, an applier that can fail, and a post-check that can roll back. Row 10
has the first and third and gets its applier from §4; row 15 has all three — the
installer is both artefact and applier — and gets its trigger from §5; rows 6–9
already run on the app timer. Row 14 is the
exception that proves the label: the user's own data, written by the user's own
agent, with the nightly backup as its rollback, so the "signed artefact" test
does not apply to it. Rows 4 and 5 fail the first test (they are pinned but
never written), rows 1–3 fail the second (the destination is a disk, not a
file), and row 12 fails the third in a way worth stating: the verifier cannot
verify its own replacement.

**What this table does not say.** "In place" is not "unmeasured". Rows 6–10 and
15 are covered by one Ed25519 signature and a manifest of hashes; the CPU does not vouch
for them. That is gap 1, it is already stated, and this ADR does not narrow it.

## 4. The default-model switch

### 4.1 The two options, and the one consequence that decides it

| | A signed document the guest pulls | An Odile API in front of NEAR resolving an alias |
|---|---|---|
| Where the model is resolved | on the guest, from a signed file | in Odile's service |
| Who sees a prompt | NEAR's enclave only | **Odile's service, in plaintext, on every turn** |
| Latency of a switch | the guest's next check (§5) | immediate |
| What a prompt's path becomes | guest → `cloud-api.near.ai` | guest → Odile → NEAR |
| `docs/CLAIMS.md` §1 *Inference* | true, verbatim | **false** |
| The privacy inventory | unchanged | gains prompt content at a third party |
| Availability | one document fetch | every tenant's inference depends on one service |
| The retired meter/proxy (ODL-205) | stays retired | re-created with a central key |
| A4's exit criterion | satisfied | **fails**: "no prompt passes through an Odile service" |

### 4.2 Why the proxy is rejected

1. **It makes an approved sentence false.** `docs/CLAIMS.md` §1 *Inference*:
   "Your prompts go from your machine to the enclave. They do not pass through
   Odile's servers." A proxy that resolves an alias sees the request body. The
   row would have to be re-cut, the privacy inventory would gain a new store, and
   the change would need Savio — which is exactly the escalation this ADR is
   designed not to require.
2. **It fails the gate it is meant to serve.** A4's exit criterion names "no
   prompt passes through an Odile service". An option that fails its own
   milestone's criterion is not an option.
3. **It re-creates what ODL-205 retired.** The meter was removed for one NEAR key
   per user, deployed at install and spent directly; the allowance's cap is the
   provider's per-key `spendLimit`, set at mint (ODL-567 §7: a minted key refuses
   at its limit with `402 api_key_limit_exceeded`, and the check is post-charge).
   A proxy holds the account key and meters per request — the central key, the
   per-request ledger and a proxy-side cap knob all come back.
4. **It moves the attestation story.** Today the guest calls the enclave and the
   model attestation binds to that endpoint. Through a proxy, the attested call
   is the proxy's, and the user's evidence no longer covers the hop that holds
   their text.
5. **It is a new single point of failure and a new trust boundary** in the
   prompt path, with no verdict behind it. The one thing it buys — immediacy — is
   bought for a 15-minute poll in §5.

**What the rejection costs, stated honestly.** MVP §13 item 4 wants per-response
attestation, and "a thin proxy would capture NEAR's response signature". This ADR
closes the proxy as a *model-resolution* mechanism; a later per-response
attestation proxy is a separate decision with its own CLAIMS row. It is not
banned by this ADR.

### 4.3 The chosen route: a signed per-tenant document, pulled by the guest

```
        odile.fyi (HQ)                                NEAR AI (TEE)
        │  GET /model/<slug>.json + .sig               ▲
        │  one signed JSON: a model name               │ prompts, direct, with
        ▼                                               │ the machine's own key
   ┌────────────────────────────┐                       │
   │ tenant VM                  │───────────────────────┘
   │  odile-update model apply  │
   │    verify → compare → write│         (no Odile service in this line)
   │    → restart odile-agent   │
   └────────────────────────────┘
```

The guest initiates; HQ publishes. Nothing on HQ connects to a tenant, and no
process of Odile's is on the prompt path. "Push" in the card's wording means
"HQ makes it available"; the connection is the guest's, because that is the
existing direction (`release.json`, the seed, the heartbeat) and the one that
needs no listener on a machine whose owner holds the only key.

### 4.4 The document — one interface, written down

```
GET https://odile.fyi/model/<slug>.json        Cache-Control: no-store
GET https://odile.fyi/model/<slug>.json.sig    detached Ed25519, base64 + "\n"
```

```json
{
  "schema": 1,
  "slug": "odl-7f3a2c",
  "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017-abliterated",
  "reasoning_effort": "high",
  "generated_at": "2026-11-17T18:00:00Z",
  "notes": "one line a user can read"
}
```

| Field | Rule | On failure |
|---|---|---|
| signature | Ed25519 over the exact bytes, verified with `/etc/odile/odile-signing-key.pub`; raw 64 bytes or base64 accepted, the same reader the release uses | `exit 3`, no write |
| `schema` | exactly `1`; unknown top-level keys are refused, so a future schema is a new number | `exit 4` |
| `slug` | equals this machine's slug, read from `/etc/odile/seed.json` | `exit 5` — a mis-served document cannot switch another tenant's model |
| `model` | `^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$`; no colon, quote, backslash or newline, because the value is written into YAML and must not be able to add a key | `exit 4` |
| `reasoning_effort` | optional; one of `low`, `medium`, `high`, `max` — the levels the pinned model's provider accepts. A model that needs another level is schema 2 | `exit 4` |
| `generated_at` | `YYYY-MM-DDTHH:MM:SSZ`; must be **newer** than the recorded one. Equal means already current; older is refused unless `--allow-downgrade` | `exit 10` / `exit 5` |
| `notes` | optional, one line, ≤ 200 characters; printed and stored, never parsed | `exit 4` |
| the whole document | a `404` means **no document**: the seed's model stands. It is a state, not an error | `exit 0`, `no-document` |

The ordering on `generated_at` is the replay control: a validly signed old
document cannot pin a tenant to an old model, and the comparison is against the
**recorded** document, not against the guest's clock, so a machine with a wrong
clock is still correct. Rolling back to an earlier model is therefore a **new**
document with a newer timestamp, not a downgrade flag — the same rule
`odile_update` already uses for releases. The one exception is the local
`rollback` (§4.5): it restores the previous state and **holds** the digest it
rolled away from, so the next tick does not re-apply it; only a newer document
(or `--retry`) moves that tenant again.

`--allow-downgrade` is the escape hatch for one case: a document that is worse
than the one before it and cannot be replaced yet. It is a human on the machine
or the agent, never the timer — the unit runs `apply --yes`, which passes no such
flag — and it still needs a valid signature and a matching `slug`. A fleet-wide
move backwards is a new document with a newer `generated_at`, the same rule as
forward.

`reasoning_effort` is the document's second setting, not a second mechanism:
it lands in `agent.reasoning_effort`, the same key the installer's template sets
(`installer/odile_install.py:12119`), because a model change and the effort its
provider accepts move together. A document without it leaves the live value
alone.

### 4.5 The applier

`odile-update model {check,apply,rollback,status}` — a subcommand of the same
program, not a second program. One file on the machine, one key, one signature
reader, one log (`/var/lib/odile/odile-update.log`), one snapshot directory. The
subcommand skips the app layer's refusals, which do not apply to a text setting.

**What a document may change.** Three keys, and nothing else:

| Key | Written as | Post-check |
|---|---|---|
| `model.default` | double-quoted, the template's own shape (`installer/odile_install.py:12114`) | reads back equal |
| `custom_providers[0].model` | unquoted, the template's shape (`:12125`) | reads back equal |
| `agent.reasoning_effort` | double-quoted (`:12119`) — **only when the document carries one**; absent leaves the live value | reads back equal when written |

The quoting is the template's, key by key, because the byte-compatibility test
compares the applied file to `render_tenant_config(seed)` — which also means
the test's seed carries the document's effort, or its `agent.reasoning_effort`
line is excluded from the comparison. A document with `low` cannot byte-equal a
template that hard-codes `"high"` until §4.6 reads the effort from the record
too.

- **`check`** fetches, verifies, prints the plan, writes nothing, and exits 10
  when the machine is already at the document. A `404` is `no-document`, exit 0.
  A digest in the state file's `held` entry is `held`, exit 0 (§4.4). `check` is
  the read-only verb the user and the agent use; it never repairs drift.
- **`apply`** is one transaction over **three files and one state file**, and
  the record pair is part of it because a rollback that leaves the record naming
  the failed document is not a rollback: the app layer snapshots its four
  records for exactly that reason (`update/odile_update.py:965`, restored at
  `:1054`), and the model snapshot would otherwise undo the config and leave the
  record pointing at the document it just failed on — a fail/restart loop every
  fifteen minutes.
  1. snapshot `~/.hermes/config.yaml`, `/etc/odile/model.json` and
     `/etc/odile/model.json.sig` into
     `/var/lib/odile/odile-update/model-snapshot/`, with the same
     `{sha256, mode, uid, gid}` record and the same `null`-means-absent
     convention the app layer uses (`update/odile_update.py:950`–`:979`);
  2. write `pending` (the document's digest, `generated_at`, phase) to
     `/var/lib/odile/odile-update/model-state.json`;
  3. write the config atomically (temp file, `fsync`, `rename`, 0644, the
     home's uid/gid), then the record pair — `/etc/odile/model.json` and its
     `.sig`, byte-for-byte the served bytes, 0644 root;
  4. phase `restarting`; restart `odile-agent`;
  5. post-check: the unit is active, and every key the document names reads
     back as the document says;
  6. clear `pending`, and keep the snapshot as the one rollback step.
  On any failure it restores the snapshot — config **and** record pair —
  restarts, writes the document's digest into `held` with `reason: failed`, and
  exits 6. A failed document is attempted once, not every tick: a newer
  `generated_at` replaces the held entry, and `--retry` forces the held one.
- **The crash window is named and closed.** A machine that dies between step 2
  and step 6 leaves `pending` behind. The next tick — the timer runs `apply
  --yes`, not `check` — sees `pending` before it sees "already at the
  document", re-enters the transaction for that digest, and either completes the
  restart and post-check or fails and rolls back; the writes are idempotent, so
  re-entry is safe. A second re-entry for the same digest (`attempts: 2`) rolls
  back and holds it, so a document that kills the gateway cannot loop.
- **`rollback`** restores that one snapshot — config **and** record pair —
  restarts, and checks. It is one step, like the app layer's: a second rollback
  says `same`, and applying a second document replaces the snapshot. It writes
  the rolled-back-from digest into `held` (`reason: rolled-back`), because §4.4
  makes a durable move backwards a **new document with a newer timestamp**; a
  local rollback is an override the next tick must not undo. What clears it: a
  newer document, or `--retry`.
- **Two homes, always both.** `model.default` and
  `custom_providers[0].model` are written together, because the About panel and
  the provider route read different keys. The applier refuses if the `model`
  block or a single `custom_providers` entry is missing or ambiguous: it is a
  setter, not a config generator.
- **Everything else survives.** `_config_version`, `display.skin`, the `webapp`,
  `dashboard` and `onboarding` blocks (both `public_url` values included), and
  every key Hermes added inside `custom_providers` (`models:`,
  `models_discovered:`) are preserved on its own line-level rewrite. The model
  applier never writes a host; only the installer re-run can move one, and §5.2
  makes that keep the live value.
- **No new key.** The machine's NEAR key is not scoped to a model (A2: keys
  carry their own limits and draw on the account's shared credit pool), so a
  switch needs no key change. The operator must confirm the account serves the
  new model before the document is cut — the demo tenant is the canary (§4.8).
- **Idempotent.** A second apply of the same document writes nothing, restarts
  nothing, and says `same` — unless a `pending` entry survives, in which case it
  finishes the transaction first.
- **The managed block is the operator's.** If the live config disagrees with the
  record, the next `apply` repairs it and logs `drift` — the template already
  says "never hand-edited" (`installer/odile_install.py` `TENANT_CONFIG_TEMPLATE`).

### 4.6 The precedence rule — the failure this ADR exists for

`ensure_config` renders the model from the seed and `merge_block` drops a drifted
value (`installer/odile_install.py:15745`, `:13452`). Left alone, a repair
re-run of the installer would put a switched tenant back on the seed's model,
silently, days later. The rule:

> **The seed and the flags are the install-time defaults. A live value on the
> machine is the operator's later word and wins. On every installer run, the
> model and the reasoning effort are read from `/etc/odile/model.json` when it
> exists and verifies against the `/etc/odile/model.json.sig` beside it, and the
> public host from the live `webapp.public_url`; the seed and `--public-url` are
> the defaults; an explicit `--public-url` still wins for a deliberate move.**

Four edits follow, and they belong in the build card, not in a later one:
`render_tenant_config` prefers the record (both keys and the effort); the
tenant layout's public URL prefers the live `webapp.public_url` over the slug
default (`:18681`); `scripts/manifest.py` records the **live** model (with the
seed as fallback) so the machine's own manifest agrees with the machine; and
`installer --manifest` prints the same value. The release manifest's `model`
field then means "the model the release candidate was installed with", which is
provenance, and it may legitimately differ from a tenant's live model after a
switch. That is written down in `docs/runbooks/manifest.md` in the same card.
`dashboard.public_url` takes the same host as `webapp.public_url` in the
template (`:12136`, `:12142`), and the precedence rule keeps both: a line-level
rewrite that keeps one and drops the other is the t_d95b8c13 fault in a new
place.

This rule stops being theoretical the moment row 15 gets its trigger (§5.1): the
timer re-runs the installer when the release's installer bytes changed, and that
re-run is precisely the moment a stale render would flip a switched tenant back
to the seed's model and effort.

### 4.7 What the user sees

Nothing interrupts, and nothing is hidden:

- **At the switch:** no email, no banner, no prompt. The About panel in the
  Webapp already names the model and the effort; on the next page load it names
  the new one.
- **The next turn** runs on the new model, because `odile-agent` was restarted.
  A turn in flight when the timer fires is lost; the model timer only restarts
  when a document actually changes (a handful of times a year), and the user
  simply re-asks.
- **The record:** `/etc/odile/model.json` is the signed document exactly as
  served, with its `generated_at` and its one-line `notes`, and
  `/etc/odile/model.json.sig` is the signature over those bytes;
  `odile-update model status` and the agent can both read the pair, and one
  line lands in `/var/lib/odile/odile-update.log`. A held digest
  (`/var/lib/odile/odile-update/model-state.json`) is what `status` shows as a
  failed or rolled-back switch, and it names the document so the user can quote
  it to support.
- **The machine's page:** `/verify` gains one row naming the model and the app
  release (§8, a release change). Until that lands, the About panel and the
  record are the two places.

### 4.8 The HQ action

`tools/cut_model.py --slug <slug> --model <id> [--effort high] [--notes "…"]`
writes `model/<slug>.json`, signs it with the HQ key, copies the `.sig`, and
verifies the pair with the published key before it prints the digests — the shape
`tools/recut_release.py` already has. `./scripts/deploy.sh` publishes it. One
tenant is one run of the tool; the fleet is a loop over slugs. `app.py` serves
`/model/<slug>.json` and `.sig` from `model/` with a slug pattern and an
existence check — no path is named by a request, the same rule `RELEASE_FILES`
follows today. The files are not secret (a model id and a slug are already
public) and no page links them.

The intended model therefore has **one** home on HQ: the signed file. No database
column, no second copy — the same discipline that keeps the release in `release/`.

## 5. The trigger

**The answer, in one line: the user never presses update. The machine updates
itself for every layer that can move in place; the layers that cannot are a
rebuild the machine **books with one click** into its own next window, prepares
with a T-0 snapshot, and finishes on the first visit — HQ writes the disk in
between.**

| Layer group | Who triggers | When | What the user sees |
|---|---|---|---|
| App layer (rows 6–9) | the machine — `odile-update.timer`, installed **on** by default in alpha.0; the user may also run it | daily, 04:00 ± 1 h — `OnCalendar=*-*-* 04:00:00`, `RandomizedDelaySec=1h`, `Persistent=true`, after the 03:00 backup; and after boot | A journal line and a record; the app release readable at `/manifest` and recorded in `/etc/odile/release.json` (the `/verify` row checks the **boot** release); a failed post-check rolls back by itself and the release is still the old one |
| Installer placements (row 15) | the machine — the same timer, one installer re-run after the bytes are written | only when the release's installer artifact changed (a handful of times a year), immediately after the write | nothing visible: the unit or Caddy block moves, `odile-agent` restarts only if a file the gateway reads moved, and the release's journal line says the re-run's exit code |
| Default model (row 10) | the machine — `odile-update model apply --yes` on its own timer, which is a no-op when current; the user may also run it | on boot, every 15 min ± 3 min, and on demand; a 404 costs one request; a held digest is not retried | The About panel's model label; the one-line `notes` in `/etc/odile/model.json`; the next turn on the new model |
| Boot chain, OS, environment (rows 1–5) | **the user clicks Upgrade; the machine books its own window; HQ writes the disk; the user restores** | the tenant's next 03:10 at least 15 minutes after the click — or the first window after a security deadline, with no click (§5.3) | A corner notice with two buttons, and a booked notice with **Change** and **Cancel**; the booking email is a notification only; "Upgrading your machine" on the account page while it is rebuilt; then the machine's own page — paste the recovery file, "Restoring your data…", the snapshot id and time, sign in (§5.3) |

**Why the app layer's timer is installed on by default.** V0.1's design was a
manual default with an opt-in timer (`update.md` §8), for paying strangers with a
ceremony. A4's objective is
"a security and a feature update without an operator", alpha.0 is
invitation-only with the team watching, and a signed, hash-checked,
snapshot-protected release that refuses a machine it was not cut for is a smaller
risk than a tenant six weeks behind a security fix. The user can turn it off
(`systemctl disable --now odile-update.timer`) — and because row 15 rides the
same timer, that also freezes the placements; the model timer is separate. The
first-run skill will say the machine keeps itself updated. The record is
`/etc/odile/release.json`. The unit in `docs/runbooks/update.md` §8 reads
`OnCalendar=daily` + `RandomizedDelaySec=1h` (`:1676`–`:1677`), which is
00:00–01:00; the build card moves it to `*-*-* 04:00:00` so the day's data is
backed up (03:00) before the app layer moves.

**Why the model check is 15 minutes and the app layer is daily.** The model
document is a few hundred bytes and a switch is a handful of times a year; the
freshness that matters is "before the next real conversation", not "within the
day". The app layer is a multi-hundred-kilobyte fetch and a restart, so daily is
right. Both are `Persistent=true`, so a machine that was off catches up once when
it returns — the same rule as the backup and brief timers. The model timer runs
`apply --yes`, not `check`: `check` writes nothing by definition, so a drift
repair, a `pending` re-entry and a held-digest refusal are all `apply`'s work.

### 5.1 The installer re-run, stated as a transaction

The re-run is the one program on the machine written to converge rather than to
install again: every step compares and reports `same`
(`installer/odile_install.py:78`–`:79`), the seed comes from the verified cache
(`cached_seed`, `:14278`), the measurement from the boot record (a re-run
without `--expect` reads that record), the recovery-file prompt is
`--i-have-copied-it`, the first-visit state is left exactly as it is
(`ensure_first_visit`, `:15310`–`:15321`), and the paid smoke turn is skipped
while `smoke.ok` exists unless `--force-smoke` is passed. So a converged re-run
changes nothing and spends nothing — but only if it is called correctly, and
today two calls are missing:

- **The signature is not beside the bytes.** `odile_update` writes
  `/usr/local/bin/odile-install.sh` and nothing else
  (`update/odile_update.py:1096`); the installer's signature defaults to the
  installer's own name plus `.sig` (`installer/odile_install.py:18621`–`:18623`),
  and an update writes no `.sig`, so `verify_or_die` would refuse the re-run with
  `exit 3`. The app layer must write the release's `odile_install.py.sig` beside
  the installed copy and snapshot the pair.
- **The token is demanded before the cache is read.** `load_or_fetch_seed`
  refuses without a 64-hex setup token (`:14688`) even when `cached_seed` would
  return a verified cache; a live tenant's token is spent (`seed/seed.py:1141`).
  The build card makes the token required only for a fetch.

The rest of the call is read from the machine, not remembered by a timer:
`--slug` and `--instance-id` from `/etc/odile/seed.json` (the signed cache
carries both), `--pubkey /etc/odile/odile-signing-key.pub` (the image places it,
`deploy/image/sealed-image.sh:506`), `--signature
/usr/local/bin/odile-install.sh.sig`, `--i-have-copied-it`, `--snapshot-dir`,
`--json` — and no `--expect` (the boot record) and no `--first-visit` (the
state file exists).

**The snapshot is the installer's, not a list in this ADR.** A re-run writes far
more than the four placement files: `config.yaml`, `.env`, the skin, the
first-visit page and its icons, `odile-export`, `odile-backup`, `odile-support`
and its `odile-diag` link, `attest.py`, the verify page, the installer copy, the
Webapp bundle, the pin files, `recovery.txt`, `apt-pins.list`, the sudoers and
sshd drop-ins, the skills and `AGENTS.md`. A hand list would be stale by the
next commit, so `--snapshot-dir DIR` makes the installer record the pre-image of
every path at the two choke points every tenant write already goes through —
`ensure_file` (`installer/odile_install.py:13209`) and `write_atomic` (`:13144`) —
once per path, before its first write: `{sha256, mode, uid, gid}` with the old
bytes under `DIR/payload/`, or `null` when the path did not exist, the same
convention the app layer uses (`update/odile_update.py:965`) and the model
applier uses (§4.5). A symlink is recorded by its target. Three writes in the
tenant path skip those two functions and are named instead of covered:
`/var/lib/odile/agent.pid`,
`/var/lib/odile/first-visit.pid` and `/etc/odile/smoke.ok`; the pids are
re-created by the restart, and `smoke.ok` is written only **after** a passing
smoke, so a failure cannot leave a half-written one. Directories the installer
creates are left behind on a restore; nothing the gateway reads at start lives
only in a directory entry.

**The transaction.** `odile_update` owns it, because it outlives the installer:

1. write the installer bytes and their `.sig` (the app layer's existing snapshot
   already holds the old installer, and gains the `.sig`);
2. write `pending` (the release id, the installer digest) to
   `/var/lib/odile/odile-update/placement-state.json`;
3. run the installer with the arguments above and
   `--snapshot-dir /var/lib/odile/odile-update/placement-snapshot`;
4. post-check: `systemctl is-active odile-agent`; `caddy validate`; both
   `public_url` keys read back equal to the values they held before the re-run;
   and `GET /` plus `GET /api/sessions` on `127.0.0.1:<port>` with
   `Host: <live host>` answer `302`/`401`, never `400` — the two probes
   `login_table` makes (`installer/odile_install.py:16730`–`:16738`), without a
   password and without a paid turn;
5. clear `pending`; the snapshot is the one rollback step.

Any failure, or a leftover `pending` on the next tick, restores every recorded
path (deleting the `null` ones), runs `systemctl daemon-reload`
(`:16652`–`:16654`), restarts what moved, writes the release id into `held` with
`reason: failed`, and exits 6. A failed re-run is attempted once per release;
a newer release replaces the held entry. The crash window is the same shape as
§4.5's: the state file is written before the first write, and the next tick
re-enters rather than assuming converged.

**What a re-run cannot reach.** `apt`, `useradd` and `systemctl enable` are not
file writes. They are covered by the precondition, not by the snapshot: the app
layer refuses a release whose kernel, environment or pins move (`exit 5`,
`docs/runbooks/update.md` §10) **before** it writes the installer, so a re-run
only runs inside the release the machine has just accepted, whose pinned set is
the installed one. A package change is a reinstall (row 4), and a re-run never
causes one.

### 5.2 The public host the re-run must keep

§2.8's bug is one line deep. The tenant's live value is read before the slug
default is substituted — `args.public_url or live_public_url(layout) or
f"https://{slug}.odile.fyi"` at `installer/odile_install.py:18681` — where
`live_public_url` reads `webapp.public_url` out of the live managed block with
the reader `split_blocks` already provides (`:13377`). That makes the boot
script's comment true (`deploy/image/odile-release-setup.sh:140`–`:156`), keeps
an owner's ODL-503 name across every repair and every timer re-run, and leaves a
deliberate move as the one case that needs a flag: an explicit `--public-url`
still wins, and the first install has no live value at all.
`dashboard.public_url` follows the same value, because the template writes both
from one argument.

The post-check in §5.1 reads both keys back and makes the anonymous Host probe
with the **pre-run** host; a re-run that rewrote either one to the slug fails
before the transaction is cleared.

**HQ never connects.** There is no inbound route to a tenant, no queue, no
daemon. The heartbeat already goes guest → HQ; the two fetches go guest → HQ.
A "push" that opened a connection from HQ would be the first inbound path since
the console, and it would need a listener and a credential on the machine.

**The rebuild.** §5.2 keeps only the host rule. The four-step block that used to
sit here — HQ emails, the machine is built, the user restores last night's
backup, the old machine is destroyed — is re-cut in the next two subsections as
the flow Savio approved on 24 November: a prompt, a click, a booked window, a
T-0 snapshot HQ verifies before the write, a progress page at HQ, and a restore
before the sign-in. The card calls that block "§5.2 step 1"; the steps and their
numbers are kept, the subsection is not, so that every existing pointer to §5.2
still means the host rule.

### 5.3 The rebuild flow — prompt, click, window, quiesce, restore

**The answer, in one line: the tenant is told in the app, one click books the
machine's next window, the machine quiesces and takes its own T-0 snapshot, HQ
writes the disk only after that snapshot is on the record with its id, and the
first visit restores that id before it offers a sign-in.**

A rebuild is a *replacement*, not an installation: nothing is installed on the
old machine, the same server gets a new disk and boot image, and the user's data
comes back from the repository. Nine stages (0–8), and who owns each:

0. **the offer** — the machine; the user sees nothing yet. It reads
   `odile-update`'s refusal of the newest signed release (`exit 5`, §2.9) and
   writes `rebuild.json`.
1. **the prompt** — the machine; a corner notice (and the same fact in chat).
   It reads `rebuild.json` and `snooze.json`.
2. **the click** — the machine → HQ; "Booked for 03:10". It reads the signed
   `facts.upgrade` beat and the booking row.
3. **the window** — the machine; the date, in the notice. It reads the machine's
   own clock.
4. **quiesce + T-0** — the machine; nothing (≈03:10). It reads the backup unit's
   status record and its beat.
5. **the write** — HQ; "Upgrading your machine". It reads the executor's row
   (§5.4).
6. **boot** — HQ, then the machine; "Booting", then "Waiting for your file". It
   reads the panel's `Running` and the seed token's `used_at`.
7. **restore** — the machine; "Restoring your data…", then the snapshot id and
   time. It reads `odile-restore.service`'s status.
8. **sign in** — the user; the app. The check is the verifier the seed carried.

**The prompt.** A corner notice, not a modal: *"An upgrade is available — about
20 minutes. [Not now] [Upgrade]"* — that line and two buttons, nothing else.
Every decision is in-app: **Not now** declines here, **Upgrade** books here, and
changing or cancelling a booked window happens on the booked notice (below). The
**email is a notification, not a decision path**: at booking it says what was
booked and when, and carries one link into the app; it never carries consent, a
reply-to-decline, or a prompt that only email can answer (Savio, 27 November
2026; D8 is email out only, so an email reply had no route into HQ at all). The
chat surface may state the same three facts — what happens, how long, the one
thing the user must do afterwards — and the security deadline when the release
carries one, all read from `--status`; it books with the same verb, because the
agent is a surface: the notice is a fact the agent may repeat, not a second
mechanism. A tenant that presses nothing and has no security deadline is never
upgraded.

**Change and cancel — required work, not a shipped mechanism.** The
`/upgrade/decide` contract (ODL-951) defines two actions, `upgrade` and
`not_now`, and both are decided *before* a booking; the booked state ODL-952
specifies reads only *"Booked for {{WINDOW}}."* and offers nothing else. So a
human who books and then needs the window moved has no in-app way to move it,
and the email is not one. **ODL-961 owns the whole of the missing mechanism on
one card, and this ADR pins its contract** — it depends on ODL-951 (the
endpoint), ODL-952 (the offer state it extends) and ODL-960 (the FAQ answer):
the same `facts.upgrade` object gains one optional key, `action` — `book` when
absent (ODL-950's shape and its probe stay valid), `change`, `cancel`; one
enum, not a new datum class, so the approved privacy row still covers it.
**A change** moves the one open row's `window` and `updated_at`, keeps the
same `request_id` (the row is the same booking, moved), and sends no second
email; the machine computes the next 03:10 after the booked window from its
own timer (DST-aware), and HQ checks it is later than the booked window and no
more than 26 hours after it — the booking horizon is not re-applied from the
change's clock. **A cancel** closes the open row as `cancelled`, with no
email; it names the window it cancels, and a cancel for a window the row no
longer names is a `409` (the notice re-fetches); `cancelled` enters
`UPGRADE_STATES` and the table's `CHECK` through the
same transactional table rebuild `_MIGRATE_ALERT_KINDS` uses, preserving every
id and row — a `CREATE TABLE IF NOT EXISTS` cannot widen a `CHECK`. **Any
other `action` is a `400`** (a closed enum, playbook rule 13), and the row's
state decides a replay: a `cancel` against a row already `cancelled` replays a
cancelled answer, and a `change` against a row already at the target window
replays the booking — neither re-books. **The surface**: the booked notice
keeps one line and two buttons — `Booked for {{WINDOW}}.` with **Change** and
**Cancel**; a change re-renders the new
window, and a cancel says `Booking cancelled.` for three seconds and returns
the box to the offer. That is an **amendment to ODL-952's verbatim-copy
contract**, whose "one line and two buttons in every state … Nothing else" was
written for the offer state; this ADR scopes it there, and ODL-961 carries the
booked state's line and two controls into the brand script. The machine's
`heartbeat.py upgrade --change|--cancel` posts the actions, the first-visit
handler answers them beside `upgrade`/`not_now`, and the first-visit source
(`FIRST_VISIT_SHA256`) and the heartbeat script (`HEARTBEAT_SHA256`) are
re-embedded in one release. Two constraints are this ADR's: **a cancel clears
the machine's booking only after HQ confirms the row closed** — the order is HQ
first, so a lost cancel leaves both sides agreed on the booking, never a
machine that skips a window while HQ writes the disk; and **both are
`booked`-state acts** — at `writing` or later the row refuses them with a
`409`, because the executor's state is the lock (§6), and the notice
re-fetches. **A `deadline` booking is the one whose cancel is not final** —
the machine's next deadline tick re-books it at the next window, because the
deadline is the release's promise and not the user's choice; ODL-961's FAQ
says the update is applied even if the booking is cancelled, and the cancel is
a decline of that window, not of the update (the re-book is a new booking and
sends its own notification). **The deploy order is HQ first**: an HQ that
predates `action` ignores the key and reads a change as a booking, so the door
learns the enum before any machine sends it (playbook rule 12). The email copy's
own offer to reply — the template's *"If that window is wrong…"* sentence,
`BRAND.md`'s
restatements of it and the mailer's comment that names a reply as the way to
change a booking (`odilectl/health.py:743`–`:744`) — is withdrawn by the same
ruling; ODL-961 owns the template, the brand sheet, the comment and the test
that pins the body (`tests/test_upgrade_booking.py`); the reply line goes and
the **one link into the app** the notification carries is the line that
replaces it, with the template's other lines byte-identical. Until it lands,
those are the tree's remaining exceptions, and no page may quote them.
Until this ships, a booking is one-way in the app, and no public sentence may
say otherwise.

**The offer, and where the machine learns it.** Not from a new HQ field. The
app layer already fetches the signed release document, and `load_release` refuses
a release whose `boot.kernel` differs with `exit 5` and one sentence — "a
boot-chain change is rebuild-and-restore" (`update/odile_update.py:574`–`:578`).
That refusal is the offer, and the gate the notice reads is a small record the
app layer writes when it makes it: `/var/lib/odile/odile-update/rebuild.json`
`{release, reason, generated_at}`, world-readable, no secret. Rejected: a
`rebuild` field in the heartbeat answer, because HQ would have to decide "this is
a rebuild **for you**" without knowing the machine's kernel — it holds the
release's target kernel, not the guest's — and it would be a second statement of
what a release needs, next to the signed one. **The notice is blank on a tenant
until §8 item 1 lands**, because the guest half of the updater is not installed
(§2.2); in that interval no prompt exists, so nothing books and no booking email
is sent — the email never becomes the prompt in the notice's place.

**Consent — the fact, written down.** The click posts one signed heartbeat on
the route the machine already uses. No new route, no new key, no new credential:
the same `/heartbeat`, the same Ed25519 key the machine registered at first boot.

```
POST https://odile.fyi/heartbeat      Content-Type: application/json
X-Odile-Signature: <base64 Ed25519 over the exact body>
```

```json
{
  "slug": "odl-7f3a2c",
  "at": 1795611600,
  "facts": {
    "last_backup_at": 1795582800,
    "backup": {"repo": "sftp:…", "snapshot": "…", "status": "ok",
               "bytes": 0, "repo_bytes": 0, "at": 1795582800},
    "upgrade": {
      "release": "2026.12.1",
      "window": "2026-12-01T03:10:00Z",
      "origin": "click",
      "request_id": "3f0c8ad2f0e14a8b9c6d5e4f3a2b1c0d"
    }
  }
}
```

- **`signature`** — the key `heartbeat_keys` holds for the slug
  (`odilectl/health.py:1179`–`:1182`); a different key is `409`, retried, and
  §5.4's clear is what lets a rebuilt machine register again.
- **`at`** — a Unix second **newer** than the last accepted beat
  (`odilectl/health.py:1191`–`:1196`); a `409`, and the retry re-signs with
  `at = now`.
- **`release`** — `RELEASE_ID_RE`; at booking time it must be the release HQ
  intends for this tenant. A `400` on shape; the answer carries `stale: true` on
  policy, and the notice refreshes.
- **`window`** — `YYYY-MM-DDTHH:MM:SSZ`. For `book` it is ≥ 15 minutes and
  ≤ 26 hours after receipt; a `change` names the next window (§5.3: later than
  the booked one, ≤ 26 hours after it); a `cancel` names the window it cancels.
  A `400` on shape, and the guest recomputes.
- **`origin`** — `click` or `deadline`. **Any other value is refused**, because
  its presence selects a write: a `400`, because an action enum is closed even
  when the key around it is open (playbook rule 13).
- **`request_id`** — 32 hex, minted once per decision and stable across retries;
  the booking's idempotency key, so a duplicate is the same booking.
- **`action`** — absent means `book`; `change` and `cancel` are §5.3's two
  in-app acts. **Any other value is refused**: it selects a write, so the enum
  is closed even when the key around it is open (playbook rule 13).
- **no `upgrade` key** — an older machine, or one that never decided; no
  booking, and not an error.

`facts.last_backup_at` and `facts.backup` ride along because `record()` keeps the
latest beat, and the freshness watchdog, the drift report and the account page
read the latest beat's facts (`odilectl/health.py:1983`). A consent beat without
them would make the newest beat thinner than the nightly one; both are already in
the approved privacy row.

The answer gains one key and keeps the four it has (`scripts/heartbeat.py:406`–`:409`):

```json
{"ok": true, "slug": "odl-7f3a2c", "at": 1795611600,
 "received_at": "2026-11-25T09:00:01Z",
 "upgrade": {"booked": true, "release": "2026.12.1", "window": "2026-12-01T03:10:00Z"}}
```

Additive for the guest: `heartbeat.py post` already prints the answer (`:522`).
A new `heartbeat.py upgrade` verb collects the same body, adds `facts.upgrade`,
posts, and records the answer in `/var/lib/odile/upgrade/state.json`; a
five-minute timer (`odile-upgrade.timer`) re-posts while the state says
`pending`, the same shape as ODL-926's recovery push. **Ordering matters and it
is the reverse of the usual rule:** the writer is frozen at install, the reader is
deployed at will, so HQ must accept the fact *before* any tenant can send it —
the right direction for an action (playbook rule 12). An unknown fact is stored
and ignored by an older HQ (`_validated_facts`, §2.9); an unknown `origin` is
refused, because it would select a write.

**Why not the account site.** The alternative was an authenticated click at HQ —
the user signs in at `/login` (`app.py:1956`, `:2184`) and presses Upgrade on
`/account/<slug>` (`:1969`, `:2522`), and no new fact exists at all. Rejected on
two counts: it is a hop out of the app the user is already in, and — decisive —
**the machine is what has to quiesce**, so a booking made only at HQ needs a
second exchange to reach the machine (a query at the window, or a push), while
the click on the machine leaves the booking in the machine's own state file. **The
residual, stated:** the consenting party is the *machine*, not the human. The user
and the agent share one uid, so a prompt-injected agent can press the same
button. What bounds that is not the identity of the clicker but the window, the
T-0 snapshot, the restore and — once ODL-961 lands it (§5.3) — the human's own
**Cancel** on the booked notice, which ends an unintended booking before
the window begins. The **booking email is not the objection path**: it is a
notification, and a reply to it changes nothing (Savio, 27 November 2026). If the
product wants a human-presence proof, the account site is the place to get one,
and that is a new decision.

**The window.** The machine computes it; HQ validates it. The window is the
machine's own 03:10 — ten minutes after its backup timer
(`installer/odile_install.py:12721`, `OnCalendar=*-*-* 03:00:00`) and, once §8
item 1 lands, before the app timer at 04:00. Time is the machine's local time, so
a tenant in another timezone gets a window at its own night; the fact carries the
instant in UTC. The booking rule: **the next 03:10 at least 15 minutes out, and
no more than 26 hours out.** A click at 03:05 books tomorrow, not tonight:
the machine needs the 15 minutes to quiesce and HQ needs the gate. **There is no
"upgrade now"**, and that is not a UI choice: the disk write is panel-only
(ODL-568 §2 — `reinstall` carries no image parameter at all), so the window is
the one place a machine can be replaced without a vendor dependency created by
the user's click.

**Quiesce and T-0.** At the booked window the machine runs one oneshot
(`odile-rebuild-prepare.service`). It has no timer of its own: the same
five-minute `odile-upgrade.timer` that re-posts while the state says `pending`
starts it once `/var/lib/odile/upgrade/state.json` holds the answer's booking
and the clock has reached that booking's `window`. The oneshot writes the window
it is running for into the same state file before its first step and exits at
once if the file already names that window, so a later tick cannot start a
second quiesce for the same booking — the trigger is periodic, the work is once
per window. Every step then has a bound:

1. `systemctl stop odile-agent`. The unit's own `TimeoutStopSec=90` and
   `KillMode=mixed` (`installer/odile_install.py:12756`–`:12757`) end the only
   writer of `state.db` — a turn that will not finish is cut at 90 s — so restic
   reads the file with any `-wal` beside it, not a file being written. That is
   ADR-backup §7.4's torn-database failure, on the backup side this time.
2. `systemctl stop odile-update.timer`, so a 04:00 tick cannot move the app
   layer between the snapshot and the write.
3. `systemctl start odile-backup-tenant.service` — the *same* unit the night runs
   (`installer/odile_install.py:16145`), so the snapshot uses the same
   repository, key and excludes; its `ExecStartPost` posts the beat with
   `facts.backup` (`scripts/backup.py:797`–`:808`). Measured: **27.3 s for
   3.14 GB** (ADR-backup §2).
4. **Verify the snapshot**: read the status record — `status: ok`, a non-empty
   `snapshot`, `last_success_at` at or after the window start. A failed run is
   not a T-0: the machine restarts the agent and the timer and reports nothing.
5. `systemctl start odile-agent` and the timer. The machine is live again, with
   a fresh snapshot behind it — so a window that is skipped (HQ down, the panel
   refusing) leaves a working tenant, not a quiesced one.

The T-0 is the beat step 3 posts. It is not a new fact, and it is not `latest`:
it is the newest `facts.backup.snapshot` whose `at` falls inside the window, and
that id is what the rebuild seed later names (§5.4).

**What this costs, stated.** The loss window is the gap between the T-0
(≈03:10:30) and the write (≈03:15), not the day since the last backup: the agent
is stopped for the snapshot, and everything it wrote before 03:10 is in it. What
is still lost is (a) a turn or a file written during the 90-second stop, and
(b) anything written after the agent restarts but before the disk is replaced —
minutes, on a machine that is about to be replaced. A machine whose T-0 failed is
not written at all, so it loses nothing. Before this cut the same event lost the
day; the flow's own sentence is "minutes", and I6 is the property.

**The progress page, and where it is served.** **By HQ, not by the machine**: the
old machine goes down at the panel write and the new one boots empty, so a page
on the machine dies in the middle of the flow. The account the user already has
serves it — a block on `/account/<slug>` (`app.py:1969`), behind the session
cookie (`:2522`) — and the booking email may repeat the same stages as a
notification for a user who is not signed in, with one link back to the page; it
carries no control. The block is shown for an **open** row and not for a
`cancelled` one (ODL-953's row filter, §5.3's change/cancel): a cancel removes
the booking, so the page must not keep saying `Queued`. **Stages, not a
percentage**, because the variable is the panel write:

```
queued → backed up ✓ → upgrading your machine → booting → waiting for your file
       → restoring your data → done
```

- **queued** — the booking row: the release and the window the answer named.
- **backed up ✓** — the T-0 beat's `facts.backup.snapshot` and `.at`, shown
  with the id and the time.
- **upgrading your machine** — the executor's own row, written before the
  reinstall call (§5.4).
- **booting** — the panel answers `Running`; the seed token's `used_at` marks
  the installer.
- **waiting for your file** — the machine is up and the restore has not finished
  — no post-restore beat yet.
- **restoring your data** — the `backup.py run` that `odile-restore.service`
  makes after the restore (ADR-backup §7.2 step 5) posts the beat that flips
  this stage.
- **done** — that beat, plus the page's own link to the machine.

Every stage is an existing record; the page adds no fact. **Range, not a number.**
The measured components are build 7m48s, boot to first-visit 6m29s and a 0.861 s
restore — but that restore was a **3.630 MiB** home, not a 3.14 GB one, and the
repository is now a Hetzner box in the EU read from Montréal (ADR-backup §7.2's
transfer term). The copy says "about 20 minutes", inside the 15–25 minute
estimate, and **the number that replaces the estimate is the drill's** — the
destroy → rebuild → paste → restore run against the real box (ADR-backup §8's
first-visit row; `t_12b3720a` superseded ODL-938 and measured only the fixed
costs); until it lands the copy carries "about", the same rule ADR-backup §7.2
applies to "under an hour". **The one comparison worth showing:**
the HQ page shows the T-0 id, and the machine's page shows the id it restored;
the user's check is that they are the same id, which is why showing an id that
opens nothing without their password is safe — HQ still cannot read the
repository (D19). **Wording:** *"Upgrading your machine"* for the build and boot,
*"Restoring your data…"* for the restore, and **never "Installing upgrades…"**
for rows 1–5: nothing is installed on the old machine, it is replaced.

**The first visit: restore before the sign-in.** The rebuild seed decides it
(ADR-backup §6, §7.2–§7.3). The seed carries `sign_in_hash` (the account's own
scrypt verifier — the string `accounts.password_hash` holds; the installer reads
it from the seed at `installer/odile_install.py:18708` and writes it through
`write_env_hash`, `:4715`) and **two keys new in this cut**, both written by the
executor's arming (§5.4 step 3) through `seed/seed.py`'s `build_seed` and read
by the first visit. **`previous_instance`** is the old machine's id, and the
rebuild marker the first visit reads; it is new because the marker it replaces
does not survive the BX11 re-cut — at this head a rebuild is a seed's sealed
`rebuild` object, which `odile-firstboot.py:1559` unseals for the old repository
password, and the provisioning card retires that password (ADR-backup §8) — while
`build_seed` writes only the *new* `instance_id` today. **`backup_snapshot`** is
the T-0 id, armed by the same step after the gate.

The page asks for the recovery file first; `odile-restore.service` restores
**that id**, not `latest` (`backup.py restore <id>` takes it positionally,
`scripts/backup.py:930`; `latest` stays the fallback for a seed that names none,
which is what an older seed means); then the page shows the id and its time; only
then the sign-in.

Three rules make the order load-bearing:

- **`/etc/odile` is not restored** — the A3 rule (`installer/first_visit.py:76`–`:81`)
  keeps the machine's own state, including the recovery-code hash.
- **The restored `.env` is the backup's copy of the sign-in**
  (`installer/first_visit.py:118` verifies against `~/.hermes/.env`, and
  ADR-backup §6 lists `.hermes/.env` in the repository). So the restore *replaces*
  the verifier the seed wrote at first boot with the one from the backup's night.
  If the user changed their password after that night, the restored verifier
  **accepts the retired password and refuses the current one** — the opposite of
  the pre-B-once failure, and worse, because it is silent. So the first-visit
  service re-writes the seed's `sign_in_hash` after the restore and before it
  offers the sign-in, through the same `write_env_hash` the installer uses.
- **No sign-in before the restore.** The reborn agent's home is empty; letting
  the user in is the panic this flow exists to prevent — the machine would answer
  as a stranger, and the user's first message would go to an agent with no memory.

A password changed *during* the rebuild window is the one case the seed cannot
carry: A5's signed reset push (ODL-578) is the mechanism that closes it, and at
this head a login failure on the machine is the signal.

**"Not now", and the security deadline.** "Not now" is **local and per
release**: the notice writes `snooze_until = now + 7 days` into
`/var/lib/odile/upgrade/snooze.json` and hides until then; a new release
re-prompts regardless; and HQ learns nothing — snooze is not a fact. **The
deadline** is a property of the release, not of the tenant: a release may carry
`upgrade: {class: "rebuild", security_deadline: "YYYY-MM-DDTHH:MM:SSZ"}` in its
signed document (an optional key; an older reader ignores it, and a document
without it is a release with no deadline). At the first window at or after that
instant the machine queues **itself** — it posts the same fact with
`origin: "deadline"` and a fresh `request_id`, then quiesces and snapshots as
usual — so a tenant who never clicks still gets a T-0 restore rather than a day's
loss. The cut sets the date; the default for a security release is
`generated_at + 14 days`. Both numbers (7 and 14) are defaults the Product lead
may re-cut in copy; the mechanism is this ADR's.

### 5.4 The window's executor, and what it must not do

The disk write is **three calls, not one**, and the order is the spike's
(`docs/product/SPIKE-servury-reinstall.md` §6): reinstall (`os=debian13`, the
three `want_*` flags, and the **tenant's** public key) → boot image (URL +
SHA-256) → disk image (URL + SHA-256, panel only). `tools/servury_panel.py
--recycle` is that composite behind the fence the spike asked for: one server id
from the allow-list, `ERASE` typed by the tool and not passed as an argument
(`REINSTALL_CONTROL`, `tools/servury_panel.py:162`), the build-time fields
explicit — "what you do not send is off", so an omitted `want_measured` is an
unmeasured machine — a log line before the POST, and `--apply --confirm` for the
destructive write (`:1318`–`:1336`). The destroy form next door is kept out by
the `action` field and `FORBIDDEN_PATH` (`:137`, `:159`).

**The account boundary, stated rather than hidden.** The tool runs on the host
account's Servury key today, and that key **also reaches HQ** (spike §7: the
same `?action=servers` lists HQ and the tenant, and a reinstall is scoped to the
key's account, not to a plan). The runbook's fence is what keeps HQ out — *"Never
here: HQ (`<server:hq>`) and any tenant"* (`docs/runbooks/recycle.md` §1),
enforced by rule 1: the allow-list names **exactly one id**, `--server` must equal
it, and `--allow-server` is refused for this verb. So the executor writes **one**
tenant's id into the allow-list, runs, and writes the next; turning the allow-list
into a *set* is the change that would put HQ one typo from a wipe, and it is not
this design. The key comes from a file, never argv (rule 5). A tenant rebuild on
a dedicated account is A2's recycling follow-up; until it exists, the one-id
allow-list is the whole control.

**One window at a time, because the allow-list is one file.** `SERVER_ID_FILE`
is a single id shared by every destructive and non-destructive panel write
(`tools/servury_panel.py:136`), so two window runs in the same five minutes would
fight over it — and the dangerous shape is subtle: the tool refuses a `--server`
that is not the allow-list's id, so a racing pair is *refused* if each passes its
own id, but a pair that read the id back from the file could both pass the
*wrong* tenant's id and both be accepted. So the executor takes the host lock
(the shape `tools/check_lock.py` takes for `make check`), writes the allow-list
from its own booking row, passes `--server` **from that row and never from the
file**, runs, and releases. A second due window waits; the gate's 20-minute
window has room.

**The gate, before the first call.** The executor writes nothing until the T-0 is
on the record: the latest `facts.backup` has a non-empty `snapshot`, `status: ok`,
and an `at` inside `[window, window + 20 min]`. No T-0, no write: the machine
stays live (the quiesce restarted the agent), the booking moves to the next
window, and after three windows the row is `failed` and the user is notified by
email. This is the data-loss control: **the write follows the snapshot, never
the clock.**

**What the executor does, in order — each step idempotent.**

1. gate (§ above), then write `writing` into the booking row;
2. **clear `heartbeat_keys` for the slug.** The rebuilt machine mints a new key
   at install, and `record()` refuses a different key on a registered slug
   (`odilectl/health.py:1179`–`:1182`). Without this step the post-restore beat
   is `409`, the progress page never reaches "restored", and the freshness
   watchdog opens a false alert three days later. A duplicate clear is a no-op.
   **The clear comes after the gate, and that ordering is the control:** between
   the clear and the new machine's first beat the slug's key is unregistered, and
   `record()` accepts the first public key it sees for a slug
   (`odilectl/health.py:1171`–`:1177`) — so a stranger who posts first can own
   the slug's signature until the runbook's `DELETE` clears it again. What that
   buys them arrives *after* the T-0 has been verified and the disk is already
   being replaced: forged facts, not a wipe. Reversed (clear, then gate), a
   forged `facts.backup` could open the gate for a machine that never snapshotted.
   The residual is a denial of the "restored" signal, not data.
3. **arm the rebuild seed**: `previous_instance` and `backup_snapshot` (both
   new in this cut — the old machine's id, the marker the first visit reads;
   and the T-0 id), `sign_in_hash` (the account's verifier), and a **fresh
   per-machine seed token bound to the instance id the command line bakes** —
   the spike's §5 failure, a recycled machine that boots a stock-looking box
   that never installs, is exactly the row this step inserts. Arming twice is
   the same row (the seed ledger's `UNIQUE` shape).
4. `--recycle --apply --confirm` with the UKI and disk image
   `deploy/image/tenant-image.pin` names — the same pair the release's boot chain
   is pinned to (§2.3), and the order the spike measured: reinstall, boot image,
   disk image;
5. poll `?action=server` to `active`, then the seed ledger's `used_at` — the
   installer is running; then `booted` in the row;
6. leave the rest to the machine: the first visit, the restore, and the
   post-restore beat that flips the page to "done".

The reinstall keeps the IPv4, the rDNS and the term (spike §4), so the **slug,
the URL and the DNS record do not move**; ADR-007's `destroyed → reopened` edge is
not on this path, and the tenant row gains the new instance id in the same call.

**Who runs it, and the one thing it cannot promise.** A timer at HQ
(`odilectl upgrade window`, every five minutes) reads the due booking rows and
runs the six steps; the same command run by hand inside the window changes
nothing about the flow, because the user's click booked it hours earlier and no
one is waiting at the moment of the click. **Rolling the executor out is Savio's**
— it drives the Servury API and the panel, which the authority matrix puts behind
him — and until the tool's allow-list admits tenant machines, the alpha's window
is a scheduled operator run. The promise the flow does make is the narrow one:
*the click never waits on a vendor or a person.* The promise it does not make is
a wall-clock SLO for the write; the panel is the variable, and the page shows
stages rather than a percentage for that reason.

## 6. Failure modes, and the control for each

| Failure | What the user gets | The control |
|---|---|---|
| A validly signed **old** document is served (replay, stale CDN, a rollback commit) | the tenant pinned to an old model | `generated_at` must be newer than the recorded one (`exit 5`); compared to the record, not the clock |
| A document for **another tenant** is served | the wrong model | `slug` must equal the machine's own (`exit 5`) |
| A half-written document during a deploy | nothing | the signature covers the exact bytes (`exit 3`); no write; the next tick retries |
| The apply fails after the record was written | the old model, and no loop | the snapshot holds the record pair as well as the config (`update/odile_update.py:965`'s convention, restored at `:990`); the failed digest goes into `held`, so the 15-minute tick does not retry it (`exit 6`) |
| The machine dies between the config write and the restart | the old model, or the new one — never a moving target | the `pending` entry in `model-state.json` re-enters the transaction on the next tick; two re-entries roll back and hold the digest |
| A locally rolled-back document is re-applied | the model the user just left | `rollback` holds the rolled-back-from digest; only a newer document or `--retry` moves it |
| A changed unit, Caddy block or `/etc/odile` file never reaches a tenant | the old placement, indefinitely | row 15 and §5.1's re-run: `odile-update` re-runs the installer when the release's installer artifact changed. Until that lands, only a manual `--force` repair |
| The installer re-run has no signature to verify | the placement stays old; `exit 3` | the app layer writes the release's `odile_install.py.sig` beside the installed installer and snapshots the pair (§5.1); the installer verifies before its first write |
| The installer re-run fails halfway | the old bytes, every one of them | `--snapshot-dir` records every file the installer writes before it writes it; restore deletes the `null` entries, restores the rest, `daemon-reload`, restart, `exit 6` (§5.1) |
| The machine dies during a re-run | the old placements on the next tick | `placement-state.json` carries `pending`; the next app-layer run restores and holds the release before it plans anything |
| The re-run rewrites the owner's public host to the slug | every request to the owner's name answers `400 Invalid Host header` | §5.2: the live `webapp.public_url` beats the slug default and `dashboard.public_url` follows; the post-check reads both back and probes the Host with the pre-run value |
| The installer is re-run after a switch | the seed's model returns, silently | §4.6's precedence rule, plus the byte-compatibility test and `manifest.py` reading the live model |
| `model.default` and `custom_providers[0].model` disagree | the About panel and the turns disagree | both written; the post-check reads both; refuse if either block is missing |
| A model id with a YAML metacharacter | a broken config, a dead gateway | the id pattern excludes `:`, quotes, backslash and newline; each key keeps the template's own quoting (`installer/odile_install.py:12114` quoted, `:12125` unquoted), and the byte test holds it |
| A restart lands mid-turn | that turn is lost | the timer restarts only on a real change; the user re-asks; the record says when |
| The document names a model NEAR does not serve, retires one, or one the account is not entitled to | the switch applies and the first turn fails | try it on the demo tenant (Odile's own machine) first, with the same key type; publish a new document naming a working model — every tenant picks it up within 15 minutes |
| The timer dies, or the machine is off for a week | the old model keeps working | `Persistent=true`; a journal line per run; once §8 item 3 lands, `versions.model` against HQ's intended model makes a stale instance visible |
| The signing key rotates | every document fails to verify | row 12: today a rebuild. A signed rotation document is a later ADR |
| The applied config is valid YAML but the gateway will not start | the workspace is down | post-check `systemctl is-active`, then restore the snapshot and restart (exit 6) |
| A click lands after the window it computed (§5.3) | nothing is written; the notice says the next date | the window is `now + 15 min` at the earliest; a booking whose window has passed is advanced to the next one, never executed late |
| The consent is posted twice, or the retry re-signs | one booking, one window | `request_id` is the booking's idempotency key (`UNIQUE (slug, request_id)`) and the partial index `upgrade_windows_open` holds one open row per slug; the second answer repeats the first booking |
| A **Change** or **Cancel** lands as the window opens | the machine has quiesced and HQ has moved the row to `writing`; closing or moving the row would leave the executor mid-write | both are `booked`-state acts (§5.3): at `writing` the row refuses them and the notice says the window is running — the executor's state is the lock |
| The T-0 beat never arrives (the backup failed, the machine was off) | the machine keeps running; nothing is wiped | §5.4's gate: the write follows the snapshot, never the clock. The booking moves to the next window; after three the row is `failed` and the user is notified by email |
| The panel write fails halfway (reinstall ok, boot or disk refused) | stock Debian on the tenant's address, no data loss — the home is in the repository | `--recycle` orders the three calls and its log line names the pair it is about to lose; a retry re-runs boot image and disk (the reinstall is idempotent); `booting` never flips, so the page shows the truth |
| The rebuilt machine's first heartbeat is `409` | no post-restore beat, so the page never says "restored"; the freshness watchdog opens a false alert three days later | §5.4 step 2 clears `heartbeat_keys` for the slug at the `writing` transition; `record()` then registers the new public key on first sight (`odilectl/health.py:1171`–`:1177`) |
| Two windows fall due at once, and the allow-list is one file | at best one run is refused; at worst both runs carry the **wrong** tenant's id and one disk is erased | §5.4: one window at a time under the host lock, `--server` from the booking row and never read back from the allow-list, the allow-list written and used inside the lock |
| The restore puts back a **retired** password | the old password works and the current one does not, silently | the first-visit service re-writes the seed's `sign_in_hash` after the restore and before it offers the sign-in (§5.3); `/etc/odile` is not restored, so the recovery-code hash survives |
| The user never pastes | the machine waits at the first-visit page with an empty home; the data is safe in the repository | the page re-offers the paste, the 36-hour freshness watchdog reports the silence, and no second write happens — the booking is `done` only after the post-restore beat |
| A prompt-injected agent presses Upgrade | a rebuild the human did not press | the consenting party is the machine (§5.3, stated); the window, the T-0, the restore and, once ODL-961 lands it, the in-app **Cancel** bound what it can cost; the booking email is the human's **record**, not their consent. This is the residual of choosing the click on the machine over the account site |

Blast radius: one tenant. The document is per-tenant, the `slug` check is
server-agnostic, the model applier touches three lines of one file, one record
pair and one state file, and a failed apply restores the previous bytes. The
installer re-run touches every file the installer writes, all restored from the
snapshot the installer took, and it runs only inside a release the machine has
already accepted. No tenant's document can reach another machine.

**The rebuild's blast radius is one disk — as long as the fence holds.** The
executor's three writes are scoped to one server id, the disk they replace is
that tenant's, and the data comes back from that tenant's own repository under
that tenant's own password. The one shared thing is the Servury account: the host
key reaches HQ as well as the tenant (spike §7), so "one server id" is the control
that matters and the allow-list is what names it — not the tenant boundary, which
the write does not cross.

## 7. Alternatives rejected

- **A — an Odile API in front of NEAR, resolving a default alias.** Rejected in
  §4.2: it puts Odile in the prompt path, falsifies an approved sentence, fails
  A4's own exit criterion, and re-creates the retired central key and meter.
- **B — add the model to the existing release document.** The smallest number of
  new parts, and the coupling is worse than the extra part: a model switch would
  inherit the app layer's refusals (another kernel, a moved environment, a
  downgrade) so a tenant on an older boot chain could not take a model change at
  all; and a one-line change would need a new manifest, installer and skills cut.
  The model's lifecycle is the operator's, not the release's.
- **C — a new seed, or a re-run of the installer with a new seed.** Rejected by
  §2.4: the seed is cached and verified forever, its token is single-use, and it
  carries the NEAR key and the mail secret — a "refresh" would re-deliver
  secrets to a machine that already has them. The installer is also a repair, not
  a switch: it runs the whole install and its smoke tests. §5.1 does give it one
  automation — a re-run when the release's installer bytes changed — but that is
  the *placement* route (row 15), never the model switch, which needs no seed,
  and it reads the seed from the cache, not from a new exchange.
- **D — an HQ push: a listener, a queue, or a connection from HQ.** Rejected: it
  is a new inbound path to a machine whose owner holds the only key, it needs a
  listener and a credential, and it inverts the direction every other control
  message already uses. The guest pulls.
- **E — the user switches the model.** Rejected as the mechanism, kept as an
  option: the user cannot know a model was retired, alpha.0's testers are not
  shell users, and A4 says the **team** moves the tenant. The user can still run
  `sudo odile-update model apply` (or ask the agent to) and can turn the timer
  off.
- **F — a second signing key for configuration.** Rejected: two trust anchors on
  the machine, two rotations, no benefit. Whoever holds the key that signs the
  installer can already ship code; a document it also signs adds no power.
- **G — the click at the account site (the card's option b).** Rejected in
  §5.3: no new fact, and a genuine human-presence proof, but the hop is out of
  the app the user is in, and the machine — the party that has to quiesce — would
  need a second exchange to learn its booking. It stays the option to reach for
  if a human-presence proof is ever required; that would be a new decision, and
  the fact in §5.3 is what it would replace.
- **H — "Upgrade now".** Rejected: the disk write is panel-only (ODL-568 §2), so
  a click that promised "now" would put a vendor and a scheduling step on the
  critical path of a button. The window is what makes the click cost nothing.
- **I — the consent as the next scheduled beat's fact.** The smallest possible
  change — the webapp writes a file, the existing nightly collector reads it —
  and rejected because the confirmation arrives a day late, and a click at 03:05
  would race the window it was booking. A five-minute retry over the same route
  is one timer more and an answer in the same interaction.

## 8. Consequences

**New parts, counted.** One route family on HQ (`/model/<slug>.json` + `.sig`),
one cutter (`tools/cut_model.py`), one subcommand family in `odile_update.py`,
one file per switched tenant in `model/`, one record pair on the machine
(`/etc/odile/model.json` + `.sig`), one state file
(`/var/lib/odile/odile-update/model-state.json`), one snapshot slot, two timers
(the app layer's and the model's), one installer re-run step in the app apply,
one placement snapshot directory and its `placement-state.json`, one
`--snapshot-dir` flag in the installer, one signature written beside the
installed installer, one live-host read in the tenant layout, one line in the
installer's placement list, and one release cut. Of the fifteen rows, seven are
in place (6–10, 14, 15), five are rebuild-and-restore (1–5) and three are not
updatable (11–13).

**The rebuild flow's parts, counted the same way.** One fact (`facts.upgrade`),
one guest verb (`heartbeat.py upgrade`), one guest unit and its timer
(`odile-upgrade.timer`, re-posting while unacknowledged and starting the oneshot
at the window), one guest oneshot (`odile-rebuild-prepare.service`), two guest
state files (`upgrade/state.json`, `upgrade/snooze.json`), one guest record
(`rebuild.json`), two optional seed keys (`previous_instance`,
`backup_snapshot`), one optional release-document key
(`upgrade.security_deadline`), one HQ table (`upgrade_windows`) with a partial
unique index for the one open booking per machine, one HQ booker in the
heartbeat path, one HQ executor (`odilectl upgrade window`) and its timer, one
optional `action` key on the same fact (`book` default, `change`, `cancel`), one
`cancelled` state and the table rebuild that adds it, two actions on the
machine's `/upgrade/decide` handler and the two buttons that send them (§5.3,
ODL-961), one block on `/account/<slug>`, and one re-cut of ODL-937's
restore step. Nothing on the machine is a listener, and nothing new is reachable
from the internet.

**Follow-on cards this ADR names but does not do:**

1. **The guest wiring of `odile-update`** — command, key, URL, timer, the model
   timer, and the installer re-run. Still open from `update.md` §9; it is the A4
   build card. It carries the four edits §5.1 and §5.2 name: the signature
   written beside the installed installer, the token relaxed when the cache
   verifies, the live public host, and `--snapshot-dir`. The placement snapshot,
   the `pending` state file and the re-run's post-checks are one card with the
   wiring, because a re-run without them is the half-write §6 names. It also
   writes `rebuild.json` on the kernel/env refusal — **the notice has no source
   before it** (§5.3).
2. **The precedence rule** — §4.6's four edits, with the byte-compatibility
   test and the manifest runbook note.
3. **The staleness signal** — `odile-update` maintains
   `/var/lib/odile/odile-versions.json` (`odile`, `hermes`, `model`); the
   installer's `backup.py heartbeat` adds those keys to `facts.versions`, which
   HQ's contract already accepts and the privacy row already names; and
   `odilectl health` compares the intended model (the document) and the intended
   app release with the beat. This is A4's "a stale instance is visible", and it
   adds no new datum class — but it does add a fact the tenant's heartbeat does
   not send today (§2.6).
4. **A `/verify` row** naming the model and the app release (the page is
   embedded in the installer, so this rides a release cut).
5. **A CLAIMS row for self-updating** — "your machine keeps itself updated from
   signed releases" is a reliability sentence no row carries. It is the Product
   lead's and Savio's, before any page says it. This ADR adds no sentence.
6. **Signing-key rotation** — a document signed by the old key that installs the
   new one; not built, and row 12 says so.
7. **The machine half of the rebuild flow** — `facts.upgrade`'s validation at
   HQ's door (`_validated_facts`), the booker and the `upgrade_windows` table,
   the `heartbeat.py upgrade` verb, `odile-upgrade.timer` (which also starts the
   prepare oneshot at the window), the notice's `rebuild.json` and
   `snooze.json`, and `odile-rebuild-prepare.service`. **Order:
   the HQ half first** — the reader before any tenant can write (§5.3).
8. **The windowed executor** — `odilectl upgrade window` and its timer, the
   gate, the `heartbeat_keys` clear, the seed arming with `previous_instance`
   and `backup_snapshot`, and the account page's stage block. **Savio's**,
   because it drives the Servury API and the panel (authority matrix: Servury
   API); the allow-list must admit
   the tenant's server id before it runs unattended.
9. **ODL-937's re-cut** — the first visit reads `previous_instance` as the
   rebuild marker, restores the seed's `backup_snapshot`, not `latest`, and
   re-writes `sign_in_hash` after the restore and before the sign-in page
   (§5.3). The card is `todo`; its DoD names `restore latest`, so
   the change lands there, not in a new card.
10. **The heartbeat privacy sentence** — the approved row enumerates what a
    heartbeat carries, and `facts.upgrade` is one more. Savio approved the
    sentence on 27 November 2026 (`t_7f88e278`); it lands as ODL-955
    (`t_62dd6f8a`), which edits `docs/CLAIMS.md` §1 *Data handling* and
    `web/privacy.html`. Until it lands the fact ships only on the demo tenant.
11. **In-app change and cancel of a booked window** — **ODL-961**, one card,
    owns the whole: the HQ booker's optional `action` key (`book` default,
    `change`, `cancel`), the `cancelled` state and the table rebuild that
    widens the `CHECK`s, the machine's `heartbeat.py upgrade --change|--cancel`
    and the first-visit handler's two further actions, the re-embeds
    (`FIRST_VISIT_SHA256`, `HEARTBEAT_SHA256`), the booked notice's **Change**
    and **Cancel**, the amendment to ODL-952's copy contract that those two
    controls require, the template/BRAND copy and the FAQ/CLAIMS answer
    (Savio's approval on that card before the page ships; §5.3), and the
    account page's row filter that drops a `cancelled` row (ODL-953). ODL-952
    ships the offer state; ODL-961 extends it. The HQ door
    lands before any machine sends the enum. Until it lands a booking is
    one-way in the app, and the booking email is a notification that cannot
    move it.

**What does not change.** No access-model change: Odile still cannot log in, and
the new document is a file, not a door. No new provider and no new network path:
the same host, the same TLS, the same direction as `release.json` and the seed —
the consent fact rides the `/heartbeat` route that already exists, and the
progress page is a block on a page that already exists. No standing credential on
a tenant: the signing key and the heartbeat key are already there, and the
rebuilt machine gets the **tenant's** key, not an operator's (I5). The *Inference*
row stays true verbatim, and no public sentence becomes stronger — one becomes
more complete (item 10, approved on 27 November and landing as ODL-955), and
ODL-961's FAQ answer is a new deck row, not a stronger sentence. The
model was already Odile's choice at install (D7; MVP §5's "one model, chosen and
pinned by us"), so a movable default is the same power at a later time — not a
fourth power in gap 3's list, though the deck may want to say it in one sentence
(§8, item 5).

## 9. Reversibility

**The model mechanism is two-way until the first tenant applies a document.** Up
to that point it is a route, a tool and a subcommand, and deleting them leaves a
tree that installs and updates exactly as today. After the first apply, the unit,
the record and the timer are shipped surfaces: removing them is a release plus a
`systemctl disable` on every machine — still two-way, at the cost of a release.
Putting a tenant back on any model is one document, any time.

**The trigger default is two-way** — a unit that is enabled or not, one
installer line, one skill sentence and, once §8 item 5 lands, one deck row.

**The notification-only email is two-way, and it removes a promise that had no
mechanism.** D8 is email out only, so the reply-to-decline line had no inbound
route to HQ; the ruling deletes that line and puts change and cancel where the
booking is made. Re-cutting it back would need an inbound mail path — a new
network path and a standing mailbox — which is the expensive direction; this ADR
chooses the cheap one. The in-app control is additive: two actions on an
endpoint and two buttons on a notice; deleting them leaves the booking flow as
ODL-950 shipped it, and a row already cancelled is terminal.

**The installer re-run is two-way too.** It is a step in the app apply, not a new
daemon: deleting the step leaves the installer bytes being written exactly as
today, and a placement that needs to move is a manual `--force` repair again. The
snapshot entries it adds are inert when no re-run happens, and the
`--snapshot-dir` flag writes nothing unless it is passed.

**The build flow is two-way until the first tenant books a window.** Up to that
point it is one fact, one verb, two units, three state files, one HQ table and
one block on a page: deleting them leaves a tree that installs, updates and
rebuilds exactly as today, because nothing in the release path, the installer or
the agent's start depends on any of it. After the first booked window the states
are shipped surfaces — a machine that has posted `facts.upgrade` holds a booking
in its own state file and expects to be quiesced at 03:10 — so removing the flow
is a release plus a `systemctl disable` on every machine, the same cost as the
model mechanism's.
**The seed keys and the fact are the reversible-by-construction pieces.** Both
keys are optional: an older installer ignores them, and a newer installer
reading an older seed falls back to `latest` — and a seed with no
`previous_instance` is a first install, exactly as today; `facts.upgrade` is a
fact, so an older HQ stores it and does not book, and a newer HQ reading an older
machine sees no key and books nothing. **One piece is one-way once used:** the
`heartbeat_keys` clear is irreversible by design — the old machine's key is gone
and the rebuilt machine's key is registered on first sight (trust on first use).
That is the correct direction for a replacement, and it is why the clear is the
executor's step and not a background job.

**The public-host preference is two-way and smaller than the bug it fixes.** One
line at `installer/odile_install.py:18681`; deleting it restores today's slug
default, and it only ever copies a value the machine already carries, so it
cannot invent a host. It applies to every installer run, not only the timer's,
which is the point: a manual repair is today's only route and has the same bug.

**The proxy option would have been one-way.** It changes an approved claim, the
privacy inventory and the key model at once; un-cutting it means re-cutting all
three and telling every user. That asymmetry is the reason to decide it here
rather than after the first tenant.

## 10. What this ADR does not decide

The A2 credential question (account password versus instance sign-in); A3's
backup provider; A6's admin console, which is the natural future UI for
`cut_model.py` and for the per-slug booking rows; per-response inference
attestation; the model catalogue or a picker (V0.2); signing-key rotation;
whether alpha.0 ever takes money. Each is its own card, and two of them (A3's
provider, a per-response proxy) are Savio's. **The windowed executor's automation
is a card and Savio's** (§5.4): the alpha can run the same command by hand inside
the window, and the flow's promise — the click never waits on a person — holds
either way. **The duration in copy is not decided here**: "about 20 minutes" is
Savio's approved wording, and the destroy → rebuild → paste → restore drill
against the real box (ADR-backup §8) owns the number that either keeps it or
re-cuts it.

## 11. Evidence

Read and run at `main`'s `592f352`
(`592f352c1d8fd23658348d2f2236fac325c9476c`, 25 November 2026), the cut these
readings and every pointer in §2–§8 are taken at. Every commit this branch adds
after it changes the two ADRs and nothing else (the first command). `main` moved
**five** of these files between Revision 6's cut and this one — ODL-926 in
`installer/odile_install.py` (+1,373 lines), `scripts/heartbeat.py` (+289),
`app.py` (+34) and `release/release.json` (+12), and ODL-947 in
`deploy/image/odile-firstboot.py` (+37) — so every installer pointer in this ADR
was re-resolved through that shift rather than patched by hand, and §11's
commands were re-run at the new head (the second and third commands are the shift
and the check that nothing else on the list moved; `update/odile_update.py`,
`scripts/manifest.py`, `odilectl/health.py`, `seed/seed.py`,
`deploy/image/odile-release-setup.sh` and `docs/runbooks/update.md` did not):

```sh
$ git diff --name-only $(git merge-base main HEAD)..HEAD
docs/architecture/ADR-backup.md
docs/architecture/ADR-upgrades-and-model.md
$ git diff --stat 8ad9145 592f352 -- installer/odile_install.py \
    update/odile_update.py app.py scripts/manifest.py scripts/heartbeat.py \
    odilectl/health.py seed/seed.py deploy/image/odile-firstboot.py \
    deploy/image/odile-release-setup.sh docs/runbooks/update.md \
    release/release.json
 app.py                          |   34 +-
 deploy/image/odile-firstboot.py |   37 +-
 installer/odile_install.py      | 1373 +++++++++++++++++++++++++++++++++++++--
 release/release.json            |   12 +-
 scripts/heartbeat.py            |  289 +++++++-
 5 files changed, 1634 insertions(+), 111 deletions(-)
$ git diff --name-only 8ad9145 592f352 -- installer/odile_install.py \
    update/odile_update.py app.py scripts/manifest.py scripts/heartbeat.py \
    odilectl/health.py seed/seed.py deploy/image/odile-firstboot.py \
    deploy/image/odile-release-setup.sh docs/runbooks/update.md \
    release/release.json
app.py
deploy/image/odile-firstboot.py
installer/odile_install.py
release/release.json
scripts/heartbeat.py
```

Read-only: no file outside the two ADRs is touched. `main`'s first-parent
history from `8ad9145` to `592f352` is where the five moved files moved; the
other six files §11 reads did not.

```sh
$ git rev-parse 592f352
592f352c1d8fd23658348d2f2236fac325c9476c
$ sha256sum update/odile_update.py installer/odile_install.py app.py \
    scripts/manifest.py scripts/heartbeat.py scripts/backup.py \
    odilectl/health.py tools/servury_panel.py
2c795cad3625c870051797881ae95576fab6537ab394586c475b48b8b5c97415  update/odile_update.py
ba8268d242c282609f4ccc04793ed8cc3156a2aad4ab48c923cfa9d2dc5e0688  installer/odile_install.py
25c5431e086ee51a497bed444b103b73812e14d5b77a80548ba260a1274b4caf  app.py
7c0b045f8b602e43f1857d8b2813e55c1f82d88d7af717ab23734c8591cdc0a7  scripts/manifest.py
2459ca264d7c079b0843d65bb3263cb2d4249d23ed7c0fe78a369aa9d4748322  scripts/heartbeat.py
d9c4278a0660650e7edc6b50847d96cdbb71ca7709c52402257e94a291a98955  scripts/backup.py
d1c79961121722da7f0b5e11f2a3048c579ee713aab1aaabfbf293093d9f0eee  odilectl/health.py
275020c6c7229883f6ab2b3f40f1c574bebfe033944d99335af4f29bfa383bfe  tools/servury_panel.py
$ ./update/odile_update.py self-test | tail -1
self-test: 42/42 passed
$ ./installer/odile_install.py --self-test | tail -1
self-test: PASS
$ grep -c odile-update installer/odile_install.py     # the guest half is unbuilt
0
$ grep -c model update/odile_update.py                 # the updater never reads a model
0
$ grep -c /model app.py                                # no model route exists
0
$ grep -n '^MANAGED_KEYS' installer/odile_install.py
301:MANAGED_KEYS = ("model", "agent", "custom_providers", "dashboard", "webapp", "onboarding",
$ sed -n '302p' installer/odile_install.py
                "auxiliary")
$ sed -n '12086p;12098p' installer/odile_install.py
# Seven managed blocks. Everything else in the written file belongs to Hermes
#   * auxiliary        — an image is described on the auxiliary lane by a
$ sed -n '13449,13458p' installer/odile_install.py     # merge_block
def merge_block(desired: list[str], current: list[str]) -> list[str]:
    """`desired`'s settings, then every current line the template does not name.

    A current line whose key path the template does name is dropped even if its
    value differs, so a drifted value cannot survive as a duplicate key. Lines
    Hermes owns (`models:`, `models_discovered:`, `auxiliary.compression:`, …)
    are kept; they are indented, so appending them at the end of the block
    leaves them inside the same mapping.
    """
    desired_paths = {path for path in line_paths(desired) if path is not None}
$ sed -n '15745,15750p' installer/odile_install.py   # render_tenant_config
    model = seed.get("model") or "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    body = TENANT_CONFIG_TEMPLATE.format(
        model=model, webapp_port=layout.webapp_port,
        public_url=layout.webapp_public_url, vision_model=VISION_MODEL,
    )
    version = config_version([
$ sed -n '18474p;18679,18681p' installer/odile_install.py
    public_url = args.public_url or public_url_for(name, args.seed_url or DEFAULT_SEED_URL)
    if layout.mode == "tenant" or layout.webapp_public_url.endswith("<slug>.odile.fyi"):
        layout = dataclasses.replace(layout, webapp_public_url=(
            args.public_url or public_url_for(slug, seed_url)))
$ sed -n '12114p;12119p;12125p;12136p;12142p' installer/odile_install.py
  default: "{model}"
  reasoning_effort: "high"
    model: {model}
  public_url: "{public_url}"
  public_url: "{public_url}"
$ sed -n '2220,2232p' installer/odile_install.py   # the tenant's own heartbeat body
    facts = {
        "last_backup_at": status.get("last_success_at"),
        "backup": {
            "repo": status.get("repo"),
            "snapshot": status.get("snapshot"),
            "status": status.get("status"),
            "bytes": status.get("bytes_processed"),
            "repo_bytes": status.get("repo_bytes"),
            "at": status.get("last_success_at"),
        },
    }
    if args.slug:
        facts["slug"] = args.slug
$ sed -n '537p' scripts/manifest.py
        model_name = seed.get("model")
$ sed -n '964,965p' odilectl/health.py                # HQ accepts a versions map, optional
    versions = facts.get("versions")
    if versions is not None:
$ python3 -c 'import json;d=json.load(open("release/release.json"));\
print(d["release"],d["generated_at"],sorted(d["artifacts"]))'
2026.11.23-8 2026-11-24T06:12:52Z ['installer', 'skills']
```

The rebuild flow's readings, the same head:

```sh
$ sed -n '574,578p' update/odile_update.py          # the refusal the notice reads
    machine_kernel = kernel if kernel is not None else os.uname().release
    if document["boot"]["kernel"] != machine_kernel:
        raise UpdateError(
            f"release {document['release']} was built for kernel {document['boot']['kernel']}, "
            f"this machine runs {machine_kernel}: a boot-chain change is rebuild-and-restore",
$ sed -n '406,409p' scripts/heartbeat.py            # the answer JSON
        self._json(
            200,
            {"ok": True, "slug": beat.slug, "at": beat.at, "received_at": beat.received_at},
        )
$ sed -n '1179,1182p;1191,1196p' odilectl/health.py   # a different key is 409; at must move
            if isinstance(offered, str) and offered.strip() != registered.strip():
                raise HeartbeatRejected(
                    "heartbeat carries a public key different from the registered key", 409
                )
        latest = self.latest(slug)
        if latest is not None and at <= latest.at:
            raise HeartbeatRejected(
                f"heartbeat at {at} is not newer than the last accepted one ({latest.at})",
                409,
            )
$ sed -n '758,766p' scripts/backup.py               # the beat carries the public half
            ["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
            capture_output=True, text=True,
        )
        if done.returncode != 0:
            raise BackupError(f"openssl pkey failed: {done.stderr.strip()}")
        print(f"created      {private} (the private half never leaves this machine)")
    body["public_key"] = public.read_text(encoding="utf-8")
    payload = json.dumps(body, sort_keys=True).encode("utf-8")
    signed = subprocess.run(
$ sed -n '930p' scripts/backup.py                  # restore takes the id positionally
    restore.add_argument("snapshot", nargs="?", default="latest")
$ sed -n '12721p;12755,12757p' installer/odile_install.py   # the window's edge, the stop bound
OnCalendar=*-*-* 03:00:00
RestartSec=3
TimeoutStopSec=90
KillMode=mixed
$ sed -n '4715,4717p' installer/odile_install.py       # write_env_hash
def write_env_hash(env_path: Path, password_hash: str, *,
                   rotate_secret: bool = False) -> str:
    """Write the hash into the tenant .env; return the username it names.
$ sed -n '18708p' installer/odile_install.py         # the seed's verifier, on a first visit
    seeded_signin = seed.get("sign_in_hash") if args.first_visit else None
$ sed -n '253,261p;14628p' installer/odile_install.py    # SEED_KEYS is the required set
SEED_KEYS = (
    "slug", "instance_id", "near_api_key", "model", "support_public_key",
    "backup_repo_url", "mail_relay", "hermes_version",
    # B-once (ODL-925): the account's own scrypt verifier, written by the seed
    # writer (ODL-341) and installed into the tenant .env before the Webapp's
    # first boot. The first visit is then a login, not a setup.
    "sign_in_hash",
)
SEED_FORBIDDEN = ("password", "passphrase", "private_key", "ssh_private_key", "luks")
    missing = [key for key in SEED_KEYS if key not in body]
$ sed -n '1316,1320p;137p;159p;162p' tools/servury_panel.py
FORBIDDEN_PATH = re.compile(r"(delete|terminate|rebuild|reinstall|power|destroy|cancel|upgrade|resize)", re.I)
REINSTALL_ACTION = "reinstall"
REINSTALL_CONTROL = "Erase and reinstall"
    parser.add_argument("--reinstall", action="store_true",
                        help="the Danger Zone reinstall (ERASE), fenced to the allow-list's one id")
    parser.add_argument("--recycle", action="store_true",
                        help="console log, reinstall, boot image, disk image — the order a freed "
                             "machine needs; the disk write starts the machine itself")
$ sed -n '76,79p;117,119p' installer/first_visit.py
* **A whole-machine restore must not restore ``/etc/odile`` (A3's rule).** The
  state file holds this machine's sign-in hash and recovery-code hash; a restore
  that brought back the backup's copy would revoke the code the user was shown
  after the rebuild. A3's restore excludes it (or rotates the code afterwards);
        --state /etc/odile/first-visit.json \
        --env-file /home/odile/.hermes/.env \
        --page /usr/local/lib/odile/first-visit.html
$ sed -n '1956p;1969p;2184p;2549,2551p' app.py
        if path in ("/login", "/signin"):
        if path.startswith("/account/"):
        if path in ("/login", "/signin"):
        row = self._session_account()
        if row is None:
            return self._send(303, "", head=head, location="/login",
$ sed -n '1676p' docs/runbooks/update.md
OnCalendar=daily
```

Revision 3's citations, re-cut at this head: the statements are unchanged and
every number is now the one that statement resolves to today, so each `sed`
above reproduces. (Revision 3 cut them at the 23 November 2026 tree; the
installer has moved twice since, and a block that only reproduced on a tree
nobody has is not evidence.)

```sh
$ sed -n '1141p' seed/seed.py
def _end_live_tokens(con: sqlite3.Connection, tenant_id: int, stamp: str) -> int:
$ sed -n '18,21p' deploy/image/odile-firstboot.py
The token is the badge, not a standing key: it is minted at provision bound to
`bb.iid`, valid once and for 24 hours. The command line is public, so the
token is public while it lives; single use and the instance binding are what
keep it from seeding another machine. (The residual: a host that reads the
$ sed -n '106p;156p' deploy/image/odile-release-setup.sh
if [ -s "$DONE" ] && [ -z "$FORCE" ]; then
PUBLIC_URL=${ODILE_RELEASE_PUBLIC_URL:-${PUBLIC_TOKEN:-https://$HOST.odile.fyi}}
$ sed -n '965p;996p;1093,1098p' update/odile_update.py
    for name in ("manifest.json", "manifest.json.sig", "release.json", "release.json.sig"):
    rollback. The installer is a file the next install reads, not a running
    if change.name == "installer":
        uid, gid = (owner_of(layout.installer) if layout.installer.exists()
                    else owner_of(layout.installer.parent))
        write_atomic(layout.installer, release.artifacts["installer"],
                     mode_of(layout.installer, 0o755), uid, gid)
        print(f"write        {layout.installer}")
$ sed -n '16195p;16652,16654p' installer/odile_install.py
    changed = ensure_file(layout.unit_target, agent_unit.encode("utf-8"), 0o644,
        print(f"{'would reread' if dry else 'daemon-reload':<12} systemd units")
        if not dry:
            run(["systemctl", "daemon-reload"], check=True)
$ sed -n '16145,16153p' installer/odile_install.py
def ensure_backup_units(layout: Layout, slug: str, dry: bool,
                        heartbeat_url: str = HEARTBEAT_URL) -> bool:
    """Write the nightly backup unit and its timer, root-owned 0644."""
    changed = False
    for path, text in backup_unit_files(layout, slug, heartbeat_url).items():
        detail = "(nightly restic, 03:00)" if path == layout.backup_service else "(daily 03:00)"
        changed |= ensure_file(path, text.encode("utf-8"), 0o644, *current_ids(layout),
                               dry, detail=detail)
    return changed
$ sed -n '15155p;16028p;16031p;16117p' installer/odile_install.py
    ensure_file(apt_pins_file(layout), apt_sources().encode("utf-8"), 0o644, 0, 0, dry,
    changed = ensure_file(layout.etc / "support-key.pub", support.encode("utf-8"), 0o644,
    changed |= ensure_file(layout.etc / "backup-repo.txt", backup.encode("utf-8"), 0o644,
    changed |= ensure_file(layout.export_program, data, 0o755, *current_ids(layout), dry,
$ sed -n '13144p;13209p' installer/odile_install.py     # the re-run's snapshot choke points
def write_atomic(path: Path, data: bytes, mode: int, uid: int | None = None,
def ensure_file(target: Path, desired: bytes, mode: int, uid: int | None, gid: int | None,
$ sed -n '14682,14690p' installer/odile_install.py    # the token is checked before the cache
def load_or_fetch_seed(layout: Layout, kit: dict, args: argparse.Namespace,
                       pubkey: Path) -> dict:
    """Cache first, exchange second, replay-with-a-valid-cache allowed."""
    token = kit.get("setup_token", "")
    instance_id = kit.get("instance_id", "")
    slug = kit.get("slug")
    if not TOKEN_RE.match(token):
        raise SystemExit("error: the recovery kit has no 64-hex setup token")
    if not INSTANCE_RE.match(instance_id):
$ sed -n '18621,18624p' installer/odile_install.py    # the signature defaults beside the bytes
def verify_or_die(args: argparse.Namespace, kit: dict) -> Path:
    signature = Path(args.signature) if args.signature else INSTALLER_PATH.with_name(
        INSTALLER_PATH.name + ".sig")
    pubkey = kit_pubkey(kit)
$ grep -n 'OnCalendar\|RandomizedDelaySec' docs/runbooks/update.md
1676:OnCalendar=daily
1677:RandomizedDelaySec=1h
```

`docs/MVP-V0.1.md`, `docs/AGENT-ORG.md`, `docs/craft/*` and
`docs/product/SPIKE-servury-reinstall.md` are untracked (`.git/info/exclude`),
so a citation to them reads only in a working tree that carries them. The
invariant this ADR turns on is also in a tracked file, verbatim:
`docs/CLAIMS.md` §1 *Inference*.

| Claim in this ADR | Where it comes from |
|---|---|
| the app layer moves in place, one snapshot, refusals with exit codes | `update/odile_update.py` docstring; `docs/runbooks/update.md` §2, §4, §10; self-test 42/42 |
| the guest half of the updater is not installed | `grep -c odile-update installer/odile_install.py` → 0 |
| the boot chain is rebuild-and-restore because the seal is bound to the measurement | `docs/CLAIMS.md` §3 gap 3; `docs/MVP-V0.1.md` §5 note; ADR-001 §15.2 |
| the OS and the environment are pinned and refused, not written | `docs/runbooks/update.md` §10; `docs/runbooks/install.md` §6.1 (`apt-pins.list`) |
| the model is rendered from the seed into two keys, and a third setting sits beside them | `installer/odile_install.py:15745`, `:12114`, `:12119`, `:12125`, `TENANT_CONFIG_TEMPLATE` |
| a re-run would revert a switch | `installer/odile_install.py:13452`, `ensure_config` |
| a re-run would also revert an owner-moved public host | `installer/odile_install.py:18474`, `:18681`, `:12136`, `:12142`; `merge_block` `:13452`; `deploy/image/odile-release-setup.sh:140`–`:156` |
| the seed cannot be refreshed | `installer/odile_install.py:14278`, `:14682`; `SEED_KEYS` |
| the manifest records the seed's model, not the live one | `scripts/manifest.py:537` |
| the tenant's heartbeat sends no versions at all; the contract accepts one | `installer/odile_install.py:2220`; `odilectl/health.py:964`; `scripts/heartbeat.py:279` |
| HQ's only drift detector is the boot release | `odilectl/health.py:1020` (`release_report`) |
| no Odile service is in the prompt path today | `docs/MVP-V0.1.md` §6.1 invariant 4; `docs/CLAIMS.md` §1 *Inference*; `docs/runbooks/install.md` §4 |
| the proxy re-creates the retired meter and central key | `docs/CLAIMS.md` §1 *Money and place* (v3.9c, ODL-205) |
| the installer's placements have no mover; a re-run needs `--force` | `installer/odile_install.py:16195`, `:14926`–`:14977`, `:16145`–`:16153`, `:15155`, `:16028`, `:16031`, `:16117`; `update/odile_update.py:996`; `deploy/image/odile-release-setup.sh:106` |
| a re-run is designed to converge: `same`, cached seed, boot record, `smoke.ok` | `installer/odile_install.py:78`–`:79`, `:14278`/`:14682` (`load_or_fetch_seed`), `:15310`–`:15321` (`ensure_first_visit`), `--i-have-copied-it`/`--force-smoke` in `build_parser` |
| the re-run has no signature beside the installed installer | `update/odile_update.py:1093`–`:1098`; `installer/odile_install.py:18621`–`:18623` |
| the re-run's token is demanded before the cache is read | `installer/odile_install.py:14688`; `seed/seed.py:1141` |
| the snapshot choke points every tenant write already goes through | `installer/odile_install.py:13144`, `:13209` |
| the token is spent at the service, not only at the client | `seed/seed.py:1141` (`_end_live_tokens`); `deploy/image/odile-firstboot.py:18`–`:21` |
| the model snapshot copies the app layer's record convention | `update/odile_update.py:965` (the four records), `:990` (restore) |
| the template's keys and their quoting | `installer/odile_install.py:12114`, `:12119`, `:12125` (`TENANT_CONFIG_TEMPLATE`) |
| the template's two public hosts | `installer/odile_install.py:12136`, `:12142` |
| the app timer today is 00:00–01:00, not 04:00 | `docs/runbooks/update.md`:1676–1677 |
| the offer is the app layer's own refusal, with one sentence | `update/odile_update.py:574`–`:578` (`EXIT_REFUSED`) |
| the heartbeat answer is read by the guest, and a new key is additive | `scripts/heartbeat.py:406`–`:409` (the four keys), `:522` (`post` prints it) |
| an unknown fact is stored and ignored; an unknown key in the body is refused | `odilectl/health.py:962` (`_validated_facts`), `:1179`–`:1182` (a different key is `409`), `:1191`–`:1196` (`at` must advance) |
| a rebuilt machine could not post until the key row is cleared | `odilectl/health.py:1171`–`:1177` (first sight registers), `scripts/backup.py:764` (the public half rides every beat); `docs/runbooks/alerting.md` §10 |
| the T-0 id already reaches HQ every night, as an existing fact | `installer/odile_install.py:2220`–`:2232`; `scripts/backup.py:797`–`:808` (the unit's `ExecStartPost`) |
| the quiesce's stop bound is the unit's, not this ADR's | `installer/odile_install.py:12756`–`:12757` (`TimeoutStopSec=90`, `KillMode=mixed`) |
| the window is the machine's own 03:10 | `installer/odile_install.py:12721` (`OnCalendar=*-*-* 03:00:00`); `docs/runbooks/update.md`:1676 (the app timer is still `daily`) |
| the seed carries the account's verifier, and optional keys are tolerated | `installer/odile_install.py:253`–`:261` (`SEED_KEYS`), `:14628` (only a missing key is refused), `:18708` and `:4715` (`write_env_hash`) |
| the restore can name a snapshot | `scripts/backup.py:930` (`restore <snapshot>`, default `latest`) |
| `/etc/odile` survives a restore; the login reads the home's `.env` | `installer/first_visit.py:76`–`:81`, `:117`–`:119` (`--env-file`) |
| the window's three writes are fenced, and the destructive one has two flags | `tools/servury_panel.py:1316`–`:1320` (`--reinstall`/`--recycle`), `:137` (`FORBIDDEN_PATH`), `:159`, `:162` (`REINSTALL_CONTROL`) |
| the progress page sits behind the account session | `app.py:1969` (`/account/<slug>`), `:2184` (`/login`), `:2549`–`:2551` (a signed-out request is a `303` to `/login`) |
