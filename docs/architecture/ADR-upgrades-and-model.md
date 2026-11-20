# ADR-005 — Upgrade inventory and the default-model switch

**Status:** Proposed — the inventory and the trigger default wait on Savio's
confirmation, which A4's exit criterion 2 names · 17 November 2026 · Architect
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
place; a rebuild is scheduled by HQ, announced to the user, and finished by the
user with their own backup password.
**What Savio is asked to confirm:** (1) the layer table in §3, row by row;
(2) the model route in §4 — a signed document, no proxy in front of NEAR;
(3) the trigger in §5 — the machine updates itself, HQ never connects, and a
boot-chain or OS change is a rebuild the user is told about and completes.
**Related:** `docs/MVP-V0.1.md` §3–§7, §12 (D7, D14, D25, D27);
`docs/CLAIMS.md` §1 *Inference* and *Money and place*, §3 gaps 1–3, §4.6;
`docs/runbooks/update.md` §2, §8, §10; `docs/runbooks/install.md` §4, §6.1;
`docs/runbooks/demo-public.md` §6; `docs/architecture/ADR-image-vs-installer.md`
§3 axis 6, §15.2; `docs/architecture/ADR-backup.md` (the ADR-004 number);
`docs/architecture/PIN-POLICY.md` §6; cards ODL-219, ODL-332, ODL-205, ODL-503,
t_d95b8c13, A4.
**Decision owner:** Architect for the mechanism, the document schema and the
applier's interface. **Savio confirms the inventory and the trigger** because the
card says so and because the trigger changes what a tenant does without its owner
present. No new provider, no new network path, no standing credential on the
tenant, no change to the access model and no sentence in `docs/CLAIMS.md` moves —
so this is not an escalation under `docs/AGENT-ORG.md` §3; it is a confirmation.

---

## 1. The invariant at risk

Four sentences the product is built on, and what this change does to each:

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
  records the model from the **seed cache** (`scripts/manifest.py:502`), and
  `odile_update` never reads the model at all (zero occurrences at this head).
  A switch without a record fix would make the machine's own manifest lie.
- **I4 — a changed boot image or firmware breaks the seal, and the machine is
  rebuilt.** `docs/CLAIMS.md` §3 gap 3; `docs/MVP-V0.1.md` §5's 15 November
  note. This is what turns three of the table's rows into rebuild-and-restore.

## 2. What exists today, at this head

The inventory in §3 is not a wish list; it is a reading of the tree at the
merge commit `9a29fdf` — this branch's prior head `9bcc54c` merged with
`main`'s `c97c4bc` — and every later commit on the branch changes this ADR only
(§11). Eight facts shape it, and each is cited because a later card will
change one of them.

**2.1 The app layer already has a signed, transactional mover.**
`update/odile_update.py` (sha256 `dd70d0de…`, 2074 lines) fetches
`https://odile.fyi/release.json`, verifies one Ed25519 signature over the exact
bytes, verifies the manifest it names, applies only the app layer, snapshots
once, checks the result, and rolls back on failure. Its refusals are precise:
a release for another kernel (`exit 5`), a downgrade (`exit 5`), a moved
environment (`exit 5`), a bad signature (`exit 3`), an inconsistent release
(`exit 4`). Its own layer table is `docs/runbooks/update.md` §2, and it is the
parent of the table in §3. **`--self-test` prints `self-test: 37/37 passed` at
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
(`installer/odile_install.py:11905`, `model = seed.get("model")`) into **two**
keys — `model.default` (`:8510`) and `custom_providers[0].model` (`:8521`) —
and the template fixes a third setting beside them,
`agent.reasoning_effort: "high"` (`:8515`, because this model rejects Hermes's
default effort); the **manifest** records the model from the seed cache. The
seed cache at `/etc/odile/seed.json` is verified forever and never re-fetched
while it verifies — `cached_seed` reads it (`:10500`) and `load_or_fetch_seed`
only fetches without one (`:10889`) —
and the token that fetched it is spent on use: `_end_live_tokens` ends every
live token for a tenant when the next is minted (`seed/seed.py:823`), and the
measured command line carries a token valid once for 24 hours
(`deploy/image/odile-firstboot.py:12`–`15`). So the seed is not a channel: a
tenant cannot get a new seed without a new token, and the seed also carries the
NEAR key and the mail secret.

**2.5 The config is a managed-block file, and the installer wins a tie.**
`MANAGED_KEYS = ("model", "agent", "custom_providers", "dashboard", "webapp",
"onboarding")` (`installer/odile_install.py:223`; the template's own header
calls it "Six managed blocks", `:8492`). `merge_block` drops "a current line
whose key the template does name … even if its value differs" (`:9733`), and
`ensure_config` renders the model from the seed on every run. **A model switch
applied by any new mechanism would be reverted by the next installer re-run**
unless the precedence rule in §4.6 exists. This is the failure this ADR exists to
prevent, and it is invisible to a test that only switches a model.

`dashboard` is the sixth key, and the one a line-level rewrite is most likely to
drop: it was added after the first tenant (t_d95b8c13), the template carries
`dashboard.public_url` at `:8532` beside `webapp.public_url` at `:8538`, and a
config written before it has the earlier four (`PRE_ONBOARDING_KEYS`, `:228`).
Both hosts name the same machine, and neither is a fallback for the other.

**2.6 What HQ already knows, and what it does not.** HQ's heartbeat contract
accepts an optional `versions` map of names to strings (`odilectl/health.py:964`),
and the approved privacy row already names "versions" among what a heartbeat
carries. But the tenant's own sender is the installer's embedded
`backup.py heartbeat`, and it sends only the backup facts and the slug
(`installer/odile_install.py:1247`–`1259`): no app release, no model, and none of
the other facts the row lists. `scripts/heartbeat.py` is the richer sender, with
a `versions` map at `:263`, and the tenant does not run it. HQ's one drift
detector, `release_report` (`odilectl/health.py:1020`), compares the **boot**
release it intended with the one the guest reports — there is no app-layer or
model analogue. §8 lists the signal; today it does not exist.

**2.7 The installer's placements have no mover, only a repair.** The installer
writes more than its own bytes: `/etc/systemd/system/odile-agent.service`
(`installer/odile_install.py:12283`), the `# BEGIN odile-<slug>` block in
`/etc/caddy/Caddyfile` (`ensure_caddy_site`, `:11119`–`:11170`, kept only if
`caddy validate` passes), the two backup units (`ensure_backup_units`, `:12233`,
write at `:12239`), the `/etc/odile` files the machine reads (`support-key.pub`
`:12135`, `backup-repo.txt` `:12138`, `apt-pins.list` `:11348`), and the
tenant's own export command (`/usr/local/bin/odile-export`,
`ensure_export_program` `:12186`, write `:12205`). `odile_update` writes the
installer's bytes and never runs it — its own docstring says so
(`update/odile_update.py:976`, "a file the next install reads, not a running
process") — and the image's converge script refuses a second run by design
(`deploy/image/odile-release-setup.sh:93`, gated on the marker at `:75`; only
`--force` ignores it, `:85`). So today a changed unit, Caddy block or
`/etc/odile` file reaches a tenant only on a manual repair re-run
(`docs/runbooks/demo-public.md` §6). That is a layer without a row, and §3's
row 15 is it.

**2.8 The same re-run can revert the owner's public host.** The tenant layout
takes its public name from the flag and the slug alone — `args.public_url or
f"https://{slug}.odile.fyi"` (`installer/odile_install.py:14136`, re-derived
after the seed at `:14324`) — and `render_tenant_config` writes that value into
both `dashboard.public_url` (`:8532`) and `webapp.public_url` (`:8538`). Because
`merge_block` drops the live line the template names (`:9733`), a re-run without
`--public-url` rewrites both keys to the slug URL on a tenant whose owner moved
the host (ODL-503); the Webapp then answers `400 Invalid Host header` for every
request to the owner's name — the t_d95b8c13 outage. The boot script's comment
claims the opposite ("keeps it when this script is re-run by hand",
`deploy/image/odile-release-setup.sh:125`–`:129`), but its only mechanism is
`ODILE_RELEASE_PUBLIC_URL` (`:130`), which defaults to the slug when the
operator sets nothing. A trigger that re-runs the installer without fixing this
would create the outage it is meant to prevent; §5.2 fixes it.

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
| 15 | Installer placements — the agent unit and the two backup units, `/etc/caddy/Caddyfile`'s `# BEGIN odile-<slug>` block, `/etc/odile/{support-key.pub,backup-repo.txt,apt-pins.list}`, `/usr/local/bin/odile-export`, and every other file a re-run writes (`config.yaml`, `.env`, the skin, the first-visit page) | written by the first installer run; the converge marker stops every later run | **in place** | The signed installer is the applier, and it is idempotent by construction — "every step compares and reports `same`" (`installer/odile_install.py:72`–`73`). A changed placement is one re-run, then `daemon-reload`/restart if a file moved (`:12576`–`:12578`); §5.1 gives the timer that trigger, and today only a manual `--force` repair does (`docs/runbooks/demo-public.md` §6). The snapshot is the installer's own and covers every file it writes (§5.1, §6). |

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
(`installer/odile_install.py:8515`), because a model change and the effort its
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
| `model.default` | double-quoted, the template's own shape (`installer/odile_install.py:8510`) | reads back equal |
| `custom_providers[0].model` | unquoted, the template's shape (`:8521`) | reads back equal |
| `agent.reasoning_effort` | double-quoted (`:8515`) — **only when the document carries one**; absent leaves the live value | reads back equal when written |

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
  records for exactly that reason (`update/odile_update.py:945`, restored at
  `:1034`), and the model snapshot would otherwise undo the config and leave the
  record pointing at the document it just failed on — a fail/restart loop every
  fifteen minutes.
  1. snapshot `~/.hermes/config.yaml`, `/etc/odile/model.json` and
     `/etc/odile/model.json.sig` into
     `/var/lib/odile/odile-update/model-snapshot/`, with the same
     `{sha256, mode, uid, gid}` record and the same `null`-means-absent
     convention the app layer uses (`update/odile_update.py:930`–`:959`);
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
value (`installer/odile_install.py:11905`, `:9733`). Left alone, a repair
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
default (`:14324`); `scripts/manifest.py` records the **live** model (with the
seed as fallback) so the machine's own manifest agrees with the machine; and
`installer --manifest` prints the same value. The release manifest's `model`
field then means "the model the release candidate was installed with", which is
provenance, and it may legitimately differ from a tenant's live model after a
switch. That is written down in `docs/runbooks/manifest.md` in the same card.
`dashboard.public_url` takes the same host as `webapp.public_url` in the
template (`:8532`, `:8538`), and the precedence rule keeps both: a line-level
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
rebuild that HQ schedules, tells the user about, and the user finishes.**

| Layer group | Who triggers | When | What the user sees |
|---|---|---|---|
| App layer (rows 6–9) | the machine — `odile-update.timer`, installed **on** by default in alpha.0; the user may also run it | daily, 04:00 ± 1 h — `OnCalendar=*-*-* 04:00:00`, `RandomizedDelaySec=1h`, `Persistent=true`, after the 03:00 backup; and after boot | A journal line and a record; the app release readable at `/manifest` and recorded in `/etc/odile/release.json` (the `/verify` row checks the **boot** release); a failed post-check rolls back by itself and the release is still the old one |
| Installer placements (row 15) | the machine — the same timer, one installer re-run after the bytes are written | only when the release's installer artifact changed (a handful of times a year), immediately after the write | nothing visible: the unit or Caddy block moves, `odile-agent` restarts only if a file the gateway reads moved, and the release's journal line says the re-run's exit code |
| Default model (row 10) | the machine — `odile-update model apply --yes` on its own timer, which is a no-op when current; the user may also run it | on boot, every 15 min ± 3 min, and on demand; a 404 costs one request; a held digest is not retried | The About panel's model label; the one-line `notes` in `/etc/odile/model.json`; the next turn on the new model |
| Boot chain, OS, environment (rows 1–5) | **HQ schedules; the user consents and finishes** | per change, announced in advance | An email with the date and the one restore step; a rebuilt machine at the same slug; the data back from the last backup once the user's recovery file opens it |

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
`OnCalendar=daily` + `RandomizedDelaySec=1h` (`:1191`–`:1192`), which is
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
(`installer/odile_install.py:72`–`73`), the seed comes from the verified cache
(`cached_seed`, `:10500`), the measurement from the boot record (a re-run
without `--expect` reads that record), the recovery-file prompt is
`--i-have-copied-it`, the first-visit state is left exactly as it is
(`ensure_first_visit`, `:11496`–`:11507`), and the paid smoke turn is skipped
while `smoke.ok` exists unless `--force-smoke` is passed. So a converged re-run
changes nothing and spends nothing — but only if it is called correctly, and
today two calls are missing:

- **The signature is not beside the bytes.** `odile_update` writes
  `/usr/local/bin/odile-install.sh` and nothing else
  (`update/odile_update.py:1076`); the installer's signature defaults to the
  installer's own name plus `.sig` (`installer/odile_install.py:14268`–`:14270`),
  and an update writes no `.sig`, so `verify_or_die` would refuse the re-run with
  `exit 3`. The app layer must write the release's `odile_install.py.sig` beside
  the installed copy and snapshot the pair.
- **The token is demanded before the cache is read.** `load_or_fetch_seed`
  refuses without a 64-hex setup token (`:10895`) even when `cached_seed` would
  return a verified cache; a live tenant's token is spent (`seed/seed.py:823`).
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
`ensure_file` (`installer/odile_install.py:9514`) and `write_atomic` (`:9449`) —
once per path, before its first write: `{sha256, mode, uid, gid}` with the old
bytes under `DIR/payload/`, or `null` when the path did not exist, the same
convention the app layer uses (`update/odile_update.py:945`) and the model
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
   `login_table` makes (`installer/odile_install.py:12636`–`:12644`), without a
   password and without a paid turn;
5. clear `pending`; the snapshot is the one rollback step.

Any failure, or a leftover `pending` on the next tick, restores every recorded
path (deleting the `null` ones), runs `systemctl daemon-reload`
(`:12576`–`:12578`), restarts what moved, writes the release id into `held` with
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
f"https://{slug}.odile.fyi"` at `installer/odile_install.py:14324` — where
`live_public_url` reads `webapp.public_url` out of the live managed block with
the reader `split_blocks` already provides (`:9682`). That makes the boot
script's comment true (`deploy/image/odile-release-setup.sh:125`–`:130`), keeps
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

**The rebuild, and the owner who is asleep.** A rebuild can be *started* without
the owner and cannot be *finished* without them: the backup password is theirs
(D19) and the restore runs with their recovery file (`docs/CLAIMS.md` §5). So the
honest sequence for a boot-chain or OS change is:

1. HQ picks the window and emails the user the date, the reason and the one
   restore step. Consent is the reply; a tenant that does not reply waits.
2. The new machine is built from the new UKI and disk image at the same slug,
   with its seed cut to the current default model; the DNS record moves; the
   installer runs; the Webapp and the agent answer, with no memory of the user
   yet. A model document for the slug, if one exists, applies within 15 minutes.
3. The user restores last night's backup with their recovery file, or asks the
   agent to. The agent's memory and the workspace return together.
4. The old machine is destroyed, and the event is written to the machine record.

What this costs, stated: the rebuild loses the day since the last backup, and
until the restore the agent is a stranger to the user. That is why the trigger is
a scheduled, announced event rather than a nightly automatic one — the machine
cannot re-key itself without a passphrase nobody holds.

## 6. Failure modes, and the control for each

| Failure | What the user gets | The control |
|---|---|---|
| A validly signed **old** document is served (replay, stale CDN, a rollback commit) | the tenant pinned to an old model | `generated_at` must be newer than the recorded one (`exit 5`); compared to the record, not the clock |
| A document for **another tenant** is served | the wrong model | `slug` must equal the machine's own (`exit 5`) |
| A half-written document during a deploy | nothing | the signature covers the exact bytes (`exit 3`); no write; the next tick retries |
| The apply fails after the record was written | the old model, and no loop | the snapshot holds the record pair as well as the config (`update/odile_update.py:945`'s convention, restored at `:970`); the failed digest goes into `held`, so the 15-minute tick does not retry it (`exit 6`) |
| The machine dies between the config write and the restart | the old model, or the new one — never a moving target | the `pending` entry in `model-state.json` re-enters the transaction on the next tick; two re-entries roll back and hold the digest |
| A locally rolled-back document is re-applied | the model the user just left | `rollback` holds the rolled-back-from digest; only a newer document or `--retry` moves it |
| A changed unit, Caddy block or `/etc/odile` file never reaches a tenant | the old placement, indefinitely | row 15 and §5.1's re-run: `odile-update` re-runs the installer when the release's installer artifact changed. Until that lands, only a manual `--force` repair |
| The installer re-run has no signature to verify | the placement stays old; `exit 3` | the app layer writes the release's `odile_install.py.sig` beside the installed installer and snapshots the pair (§5.1); the installer verifies before its first write |
| The installer re-run fails halfway | the old bytes, every one of them | `--snapshot-dir` records every file the installer writes before it writes it; restore deletes the `null` entries, restores the rest, `daemon-reload`, restart, `exit 6` (§5.1) |
| The machine dies during a re-run | the old placements on the next tick | `placement-state.json` carries `pending`; the next app-layer run restores and holds the release before it plans anything |
| The re-run rewrites the owner's public host to the slug | every request to the owner's name answers `400 Invalid Host header` | §5.2: the live `webapp.public_url` beats the slug default and `dashboard.public_url` follows; the post-check reads both back and probes the Host with the pre-run value |
| The installer is re-run after a switch | the seed's model returns, silently | §4.6's precedence rule, plus the byte-compatibility test and `manifest.py` reading the live model |
| `model.default` and `custom_providers[0].model` disagree | the About panel and the turns disagree | both written; the post-check reads both; refuse if either block is missing |
| A model id with a YAML metacharacter | a broken config, a dead gateway | the id pattern excludes `:`, quotes, backslash and newline; each key keeps the template's own quoting (`installer/odile_install.py:8510` quoted, `:8521` unquoted), and the byte test holds it |
| A restart lands mid-turn | that turn is lost | the timer restarts only on a real change; the user re-asks; the record says when |
| The document names a model NEAR does not serve, retires one, or one the account is not entitled to | the switch applies and the first turn fails | try it on the demo tenant (Odile's own machine) first, with the same key type; publish a new document naming a working model — every tenant picks it up within 15 minutes |
| The timer dies, or the machine is off for a week | the old model keeps working | `Persistent=true`; a journal line per run; once §8 item 3 lands, `versions.model` against HQ's intended model makes a stale instance visible |
| The signing key rotates | every document fails to verify | row 12: today a rebuild. A signed rotation document is a later ADR |
| The applied config is valid YAML but the gateway will not start | the workspace is down | post-check `systemctl is-active`, then restore the snapshot and restart (exit 6) |

Blast radius: one tenant. The document is per-tenant, the `slug` check is
server-agnostic, the model applier touches three lines of one file, one record
pair and one state file, and a failed apply restores the previous bytes. The
installer re-run touches every file the installer writes, all restored from the
snapshot the installer took, and it runs only inside a release the machine has
already accepted. No tenant's document can reach another machine.

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

**Follow-on cards this ADR names but does not do:**

1. **The guest wiring of `odile-update`** — command, key, URL, timer, the model
   timer, and the installer re-run. Still open from `update.md` §9; it is the A4
   build card. It carries the four edits §5.1 and §5.2 name: the signature
   written beside the installed installer, the token relaxed when the cache
   verifies, the live public host, and `--snapshot-dir`. The placement snapshot,
   the `pending` state file and the re-run's post-checks are one card with the
   wiring, because a re-run without them is the half-write §6 names.
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

**What does not change.** No access-model change: Odile still cannot log in, and
the new document is a file, not a door. No new provider and no new network path:
the same host, the same TLS, the same direction as `release.json` and the seed.
No standing credential on a tenant: the signing key is already there. No CLAIMS
sentence moves, and the *Inference* row stays true verbatim. The model was
already Odile's choice at install (D7; MVP §5's "one model, chosen and pinned by
us"), so a movable default is the same power at a later time — not a fourth
power in gap 3's list, though the deck may want to say it in one sentence (§8,
item 5).

## 9. Reversibility

**The model mechanism is two-way until the first tenant applies a document.** Up
to that point it is a route, a tool and a subcommand, and deleting them leaves a
tree that installs and updates exactly as today. After the first apply, the unit,
the record and the timer are shipped surfaces: removing them is a release plus a
`systemctl disable` on every machine — still two-way, at the cost of a release.
Putting a tenant back on any model is one document, any time.

**The trigger default is two-way** — a unit that is enabled or not, one
installer line, one skill sentence and, once §8 item 5 lands, one deck row.

**The installer re-run is two-way too.** It is a step in the app apply, not a new
daemon: deleting the step leaves the installer bytes being written exactly as
today, and a placement that needs to move is a manual `--force` repair again. The
snapshot entries it adds are inert when no re-run happens, and the
`--snapshot-dir` flag writes nothing unless it is passed.

**The public-host preference is two-way and smaller than the bug it fixes.** One
line at `installer/odile_install.py:14324`; deleting it restores today's slug
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
`cut_model.py`; per-response inference attestation; the model catalogue or a
picker (V0.2); signing-key rotation; whether alpha.0 ever takes money. Each is
its own card, and two of them (A3's provider, a per-response proxy) are Savio's.

## 11. Evidence

Read and run at the merge commit `9a29fdf`
(`9a29fdf5ad17b943e98fb2220e069dd38b473617`), whose parents are this branch's
prior head `9bcc54c` and `main`'s `c97c4bc` (18 November 2026). `main`'s
history was rewritten earlier that day (ODL-910), so the round-3 baseline
`a6b6bed` and its parents `24f06f2`/`9d9149c` no longer resolve; this is the
current tree. Every commit after the merge changes this ADR and nothing else, so
the code readings below are the tree the ADR lands on:

```sh
$ git diff --name-only 9a29fdf..HEAD
docs/architecture/ADR-upgrades-and-model.md
```

Read-only: no file outside this ADR is touched.

```sh
$ git rev-parse 9a29fdf
9a29fdf5ad17b943e98fb2220e069dd38b473617
$ sha256sum update/odile_update.py installer/odile_install.py app.py \
    scripts/manifest.py scripts/heartbeat.py
dd70d0de863c6ce16e1bc34cc220605bde5d1643b9296d90a3f3615690508e72  update/odile_update.py
37860cc9f65faefd2e5ac5b5fce8556b6feab7f1d24d96d19c13a68dc5c37529  installer/odile_install.py
6d256539709e194a1f6cd85e6b9a4c29d0920fa28e8a35287594741256a89a1b  app.py
bdca28acb6782a0658f6ef14f13e6c3cd6bfd7f123af9be1f8d119a80730b345  scripts/manifest.py
2130870684e5ba2975517b805abc2aa0b4731478b277cbb74ef2e4a0d2922f32  scripts/heartbeat.py

$ ./update/odile_update.py self-test | tail -1
self-test: 37/37 passed
$ ./installer/odile_install.py --self-test | tail -1
self-test: PASS

$ grep -c odile-update installer/odile_install.py     # the guest half is unbuilt
0
$ grep -c model update/odile_update.py                 # the updater never reads a model
0
$ grep -c /model app.py                                # no model route exists
0
$ grep -n '^MANAGED_KEYS' installer/odile_install.py
223:MANAGED_KEYS = ("model", "agent", "custom_providers", "dashboard", "webapp", "onboarding")
$ sed -n '8492p' installer/odile_install.py
# Six managed blocks. Everything else in the written file belongs to Hermes
$ sed -n '9730,9738p' installer/odile_install.py
def merge_block(desired: list[str], current: list[str]) -> list[str]:
    """`desired`'s settings, then every current line the template does not name.

    A current line whose key the template does name is dropped even if its
    value differs, so a drifted value cannot survive as a duplicate key. Lines
    Hermes owns (`models:`, `models_discovered:`, …) are kept; they are
    indented, so appending them at the end of the block leaves them inside the
    same mapping.
    """
$ sed -n '11904,11909p' installer/odile_install.py
def render_tenant_config(layout: Layout, seed: dict) -> str:
    model = seed.get("model") or "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    body = TENANT_CONFIG_TEMPLATE.format(
        model=model, webapp_port=layout.webapp_port,
        public_url=layout.webapp_public_url,
    )
$ sed -n '14136p;14322,14324p' installer/odile_install.py
    public_url = args.public_url or f"https://{slug}.odile.fyi"
    if layout.webapp_public_url.endswith("<slug>.odile.fyi") or layout.mode == "tenant":
        layout = dataclasses.replace(layout, webapp_public_url=(
            args.public_url or f"https://{slug}.odile.fyi"))
$ sed -n '8510p;8515p;8521p;8532p;8538p' installer/odile_install.py
  default: "{model}"
  reasoning_effort: "high"
    model: {model}
  public_url: "{public_url}"
  public_url: "{public_url}"
$ sed -n '502p' scripts/manifest.py
        model_name = seed.get("model")
$ sed -n '1247,1259p' installer/odile_install.py   # the tenant's own heartbeat body
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
$ sed -n '964,965p' odilectl/health.py                # HQ accepts a versions map, optional
    versions = facts.get("versions")
    if versions is not None:
$ python3 -c 'import json;d=json.load(open("release/release.json"));\
print(d["release"],d["generated_at"],sorted(d["artifacts"]))'
2026.11.16-1 2026-11-16T01:40:00Z ['installer', 'skills']
```

Revision 3's citations, the same tree, 18 November 2026:

```sh
$ sed -n '823p' seed/seed.py
def _end_live_tokens(con: sqlite3.Connection, tenant_id: int, stamp: str) -> int:
$ sed -n '12,15p' deploy/image/odile-firstboot.py
The token is the badge, not a standing key: it is minted at provision bound to
`bb.iid`, valid once and for 24 hours. The command line is public, so the
token is public while it lives; single use and the instance binding are what
keep it from seeding another machine. (The residual: a host that reads the
$ sed -n '93p;130p' deploy/image/odile-release-setup.sh
if [ -s "$DONE" ] && [ -z "$FORCE" ]; then
PUBLIC_URL=${ODILE_RELEASE_PUBLIC_URL:-https://$HOST.odile.fyi}
$ sed -n '945p;976p;1073,1078p' update/odile_update.py
    for name in ("manifest.json", "manifest.json.sig", "release.json", "release.json.sig"):
    rollback. The installer is a file the next install reads, not a running
    if change.name == "installer":
        uid, gid = (owner_of(layout.installer) if layout.installer.exists()
                    else owner_of(layout.installer.parent))
        write_atomic(layout.installer, release.artifacts["installer"],
                     mode_of(layout.installer, 0o755), uid, gid)
        print(f"write        {layout.installer}")
$ sed -n '12283p;12576,12578p' installer/odile_install.py
    changed = ensure_file(layout.unit_target, agent_unit.encode("utf-8"), 0o644,
        print(f"{'would reread' if dry else 'daemon-reload':<12} systemd units")
        if not dry:
            run(["systemctl", "daemon-reload"], check=True)
$ sed -n '12233,12241p' installer/odile_install.py
def ensure_backup_units(layout: Layout, slug: str, dry: bool,
                        heartbeat_url: str = HEARTBEAT_URL) -> bool:
    """Write the nightly backup unit and its timer, root-owned 0644."""
    changed = False
    for path, text in backup_unit_files(layout, slug, heartbeat_url).items():
        detail = "(nightly restic, 03:00)" if path == layout.backup_service else "(daily 03:00)"
        changed |= ensure_file(path, text.encode("utf-8"), 0o644, *current_ids(layout),
                               dry, detail=detail)
    return changed
$ sed -n '11348p;12135p;12138p;12205p' installer/odile_install.py
    ensure_file(apt_pins_file(layout), apt_sources().encode("utf-8"), 0o644, 0, 0, dry,
    changed = ensure_file(layout.etc / "support-key.pub", support.encode("utf-8"), 0o644,
    changed |= ensure_file(layout.etc / "backup-repo.txt", backup.encode("utf-8"), 0o644,
    changed |= ensure_file(layout.export_program, data, 0o755, *current_ids(layout), dry,
$ sed -n '9449p;9514p' installer/odile_install.py     # the re-run's snapshot choke points
def write_atomic(path: Path, data: bytes, mode: int, uid: int | None = None,
def ensure_file(target: Path, desired: bytes, mode: int, uid: int | None, gid: int | None,
$ sed -n '10889,10897p' installer/odile_install.py    # the token is checked before the cache
def load_or_fetch_seed(layout: Layout, kit: dict, args: argparse.Namespace,
                       pubkey: Path) -> dict:
    """Cache first, exchange second, replay-with-a-valid-cache allowed."""
    token = kit.get("setup_token", "")
    instance_id = kit.get("instance_id", "")
    slug = kit.get("slug")
    if not TOKEN_RE.match(token):
        raise SystemExit("error: the recovery kit has no 64-hex setup token")
    if not INSTANCE_RE.match(instance_id):
$ sed -n '14268,14271p' installer/odile_install.py    # the signature defaults beside the bytes
def verify_or_die(args: argparse.Namespace, kit: dict) -> Path:
    signature = Path(args.signature) if args.signature else INSTALLER_PATH.with_name(
        INSTALLER_PATH.name + ".sig")
    pubkey = kit_pubkey(kit)
$ grep -n 'OnCalendar\|RandomizedDelaySec' docs/runbooks/update.md
1191:OnCalendar=daily
1192:RandomizedDelaySec=1h
```

`docs/MVP-V0.1.md`, `docs/AGENT-ORG.md` and `docs/craft/*` are untracked
(`.git/info/exclude`), so a citation to them reads only in a working tree that
carries them. The invariant this ADR turns on is also in a tracked file,
verbatim: `docs/CLAIMS.md` §1 *Inference*.

| Claim in this ADR | Where it comes from |
|---|---|
| the app layer moves in place, one snapshot, refusals with exit codes | `update/odile_update.py` docstring; `docs/runbooks/update.md` §2, §4, §10; self-test 37/37 |
| the guest half of the updater is not installed | `grep -c odile-update installer/odile_install.py` → 0 |
| the boot chain is rebuild-and-restore because the seal is bound to the measurement | `docs/CLAIMS.md` §3 gap 3; `docs/MVP-V0.1.md` §5 note; ADR-001 §15.2 |
| the OS and the environment are pinned and refused, not written | `docs/runbooks/update.md` §10; `docs/runbooks/install.md` §6.1 (`apt-pins.list`) |
| the model is rendered from the seed into two keys, and a third setting sits beside them | `installer/odile_install.py:11905`, `:8510`, `:8515`, `:8521`, `TENANT_CONFIG_TEMPLATE` |
| a re-run would revert a switch | `installer/odile_install.py:9733`, `ensure_config` |
| a re-run would also revert an owner-moved public host | `installer/odile_install.py:14136`, `:14324`, `:8532`, `:8538`; `merge_block` `:9733`; `deploy/image/odile-release-setup.sh:125`–`:130` |
| the seed cannot be refreshed | `installer/odile_install.py:10500`, `:10889`; `SEED_KEYS` |
| the manifest records the seed's model, not the live one | `scripts/manifest.py:502` |
| the tenant's heartbeat sends no versions at all; the contract accepts one | `installer/odile_install.py:1247`; `odilectl/health.py:964`; `scripts/heartbeat.py:263` |
| HQ's only drift detector is the boot release | `odilectl/health.py:1020` (`release_report`) |
| no Odile service is in the prompt path today | `docs/MVP-V0.1.md` §6.1 invariant 4; `docs/CLAIMS.md` §1 *Inference*; `docs/runbooks/install.md` §4 |
| the proxy re-creates the retired meter and central key | `docs/CLAIMS.md` §1 *Money and place* (v3.9c, ODL-205) |
| the installer's placements have no mover; a re-run needs `--force` | `installer/odile_install.py:12283`, `:11119`–`:11170`, `:12233`–`:12241`, `:11348`, `:12135`, `:12138`, `:12205`; `update/odile_update.py:976`; `deploy/image/odile-release-setup.sh:93` |
| a re-run is designed to converge: `same`, cached seed, boot record, `smoke.ok` | `installer/odile_install.py:72`–`73`, `:10500`/`:10889` (`load_or_fetch_seed`), `:11496`–`:11507` (`ensure_first_visit`), `--i-have-copied-it`/`--force-smoke` in `build_parser` |
| the re-run has no signature beside the installed installer | `update/odile_update.py:1073`–`:1078`; `installer/odile_install.py:14268`–`:14270` |
| the re-run's token is demanded before the cache is read | `installer/odile_install.py:10895`; `seed/seed.py:823` |
| the snapshot choke points every tenant write already goes through | `installer/odile_install.py:9449`, `:9514` |
| the token is spent at the service, not only at the client | `seed/seed.py:823` (`_end_live_tokens`); `deploy/image/odile-firstboot.py:12`–`15` |
| the model snapshot copies the app layer's record convention | `update/odile_update.py:945` (the four records), `:970` (restore) |
| the template's keys and their quoting | `installer/odile_install.py:8510`, `:8515`, `:8521` (`TENANT_CONFIG_TEMPLATE`) |
| the template's two public hosts | `installer/odile_install.py:8532`, `:8538` |
| the app timer today is 00:00–01:00, not 04:00 | `docs/runbooks/update.md`:1191–1192 |
