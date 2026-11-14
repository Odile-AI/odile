# The pull-based app-layer update

ODL-219. `update/odile_update.py` is `odile-update`: the guest's update command.
It fetches a signed release document from HQ, verifies its Ed25519 signature and
the app-layer manifest the release names, and applies **only** the app layer —
the installer, the skills, the Webapp bundle and, when a release
moves it, the Hermes commit. The guest initiates; nothing on HQ connects to the
machine.

The boot chain is pinned. Firmware, kernel, initramfs and the command line are
not app layer: a release built for another kernel release is refused, because
those change by rebuild-and-restore. The OS packages and the Python/npm
environment are pinned the same way: a release that needs different pins is a
reinstall, not an in-place update.

This card proves the machine-free half on HQ: a release is cut and signed, it
applies to a scratch root, a tampered release or manifest is refused, the
applied tree rebuilds the release's manifest byte for byte, and rollback
restores the previous version byte for byte. Installing the command, the key and
the HTTPS route on a real tenant, and the live update there, are ODL-332.

## 1. Committed files

| File | Role |
|---|---|
| `update/odile_update.py` | fetch, verify, plan, snapshot, apply, rollback; cut a release on HQ; `self-test` |
| `docs/runbooks/update.md` | this page |
| `release/` | the signed release served from HQ: the document, its signatures, the manifest and the artifacts (not secret; replaced per release) |
| `/root/.odile/installer-signing.pem` | the HQ private key, mode 0600, never in git |
| `installer/odile-signing-key.pub` | the published public key; the recovery kit carries the same bytes |

The trust anchor is the public key the recovery kit carries, installed at
`/etc/odile/odile-signing-key.pub`. One key for one operator identity: the same
key signs the installer, the seed responses and, through `scripts/manifest.py`,
the manifest inside every release.

## 2. What an update is, and what it is not

| Layer | Changed in place? | How |
|---|---|---|
| installer, skills, Webapp bundle | yes | signed artifacts, hashed against the signed manifest |
| Hermes commit | yes, when the checkout already carries the commit and the environment does not move | `git checkout --detach`; lockfiles and the installed Python pins must equal the release's |
| firmware, kernel, initramfs, command line | no | rebuild-and-restore; a release for another kernel is refused |
| OS packages, Python venv, npm tree | no | recorded by the manifest but never written; a Hermes-commit change whose lockfiles or Python pins move is refused as a reinstall |

The chain is: one Ed25519 signature over the exact bytes of `release.json`;
`release.json` names the sha256 and URLs of `manifest.json`; the manifest has
its own signature with the same key; every artifact is hashed against the
release document, and the installer hash must also equal the manifest's
`installer_sha256`. Skill hashes are compared per skill after rendering, in the
canonical form `scripts/manifest.py` defines, so the same release verifies on
machines with different users and homes.

Nothing in the release is a secret. The seed's model name is recorded; its
NEAR key is not.

## 3. The release document

`release.json` is JSON, sorted keys, one signature beside it as
`release.json.sig`:

```json
{
  "schema": 1,
  "product": "odile",
  "release": "2026.11.08-2",
  "generated_at": "2026-11-08T15:10:00Z",
  "boot": {"kernel": "6.12.115+deb13.1-amd64"},
  "manifest": {
    "url": "manifest.json",
    "sha256": "44f3a943…",
    "signature_url": "manifest.json.sig"
  },
  "artifacts": {
    "installer": {"url": "odile_install.py", "sha256": "a666f7c8…", "size": 153069},
    "skills":    {"url": "skills.tar.gz",    "sha256": "193cc19d…", "size": 839373}
  },
  "notes": "one line of human text, optional"
}
```

| Field | Meaning |
|---|---|
| `release` | an id the operator chooses; `generated_at` orders releases |
| `boot.kernel` | the kernel release the release was built for; `uname -r` on the image |
| `manifest` | URL, sha256 and signature URL of the app-layer manifest |
| `artifacts` | one URL and sha256 per artifact; relative URLs resolve against the release URL |
| `notes` | one line a user can read before applying |

Artifact names are fixed and map to fixed destinations; a release cannot name
an arbitrary path. `webapp` is an optional third artifact (needed when the
Hermes commit or the bundle moves). Unknown artifact names are refused.

**The bundle beside the document.** The release directory is what a stranger
downloads, and it is self-contained: the installer, its detached signature and
the published key travel with the release document, so `--verify-signature`
passes from the bundle alone (ODL-332 follow-up, 2026-11-09):

| File | Role |
|---|---|
| `release.json`, `release.json.sig` | the release document and its Ed25519 signature |
| `manifest.json`, `manifest.json.sig` | the app-layer manifest and its signature |
| `odile_install.py` | the exact installer this release names (`artifacts.installer.sha256`) |
| `odile_install.py.sig` | that installer's detached signature, byte-identical to `installer/odile_install.py.sig` |
| `odile-signing-key.pub` | the published key, byte-identical to `installer/odile-signing-key.pub`; the same bytes the `/odile-signing-key.pub` route serves |
| `skills.tar.gz` | the other artifact |
| `webapp.tar.gz` | optional; present only in a release that moves the bundle |

The first three names are the three files of the tenant ritual
(`docs/runbooks/install.md` §3). `app.py` serves the eight fixed names, so the
signature is a URL next to the installer it signs, not a repo-only file.

## 4. The commands

| Command | What it does | Writes? |
|---|---|---|
| `odile-update status` | the recorded release, the installed app layer, the key, the snapshot | no |
| `odile-update check` | fetch, verify, compare, print the plan; exit 10 when already current | no |
| `odile-update apply` | the plan, a snapshot, the writes, the post-check | yes; asks first |
| `odile-update apply --yes` | the same, no question (the timer's form) | yes |
| `odile-update apply --dry-run` | the plan only; refuses an older release exactly as `check` does | no |
| `odile-update rollback --yes` | restore the snapshot and verify the restore | yes |
| `odile-update release …` | cut and sign a release from a candidate root (HQ only) | yes |
| `odile-update self-test` | the pure functions and one whole release, in a temporary directory | no |

Exit codes: `0` success; `2` a bad input or prerequisite; `3` a signature does
not verify; `4` the release is internally inconsistent; `5` refused for this
machine (boot chain, downgrade, missing commit, moved environment); `6` the
post-update check failed and the snapshot was restored; `7` nothing to roll back
to. `check` exits `10` when the machine is already at the release.

`apply` without `--yes` on a terminal asks `Apply release <id>? [y/N]`; without
a terminal it refuses (`exit 2`) rather than guess. That is the manual default
for V0.1.

## 5. Cutting a release on HQ

A release is cut from a **release-candidate root**: an installed root that
carries the app layer the release should install. `manifest.py build` records
it, then `release` verifies the manifest against that same root, exports the
skills in canonical form, hashes every artifact, writes the release document
and signs it with the HQ key. A manifest from a different tree is refused.

This is the transcript from the card's evidence run. The machine is a copy of
the scratch install ODL-214 made (root B), moved to `/var/tmp/odl219-a`; the
copy rebuilds root B's recorded manifest `de184be3…` at the same timestamp, so
its app layer is the one ODL-214 reviewed.

```sh
$ cp -a /var/tmp/odl214-b /var/tmp/odl219-a                      # 1m12s
$ sed -i 's#/var/tmp/odl214-b#/var/tmp/odl219-a#g' \
    /var/tmp/odl219-a/home/odl214b/.hermes/skills/odile-home/SKILL.md \
    /var/tmp/odl219-a/etc/systemd/system/odile-meter.service \
    /var/tmp/odl219-a/etc/systemd/system/odile-agent.service

$ ./scripts/manifest.py build --root /var/tmp/odl219-a \
    --generated-at 2026-11-08T14:00:01Z --out copy-vs-root-b.json
manifest     copy-vs-root-b.json (sha256 de184be3e0fd4e07dceaee9421a7a7583e8ad8535251d0ee626b5a9d0d543dc8, 45 skills, 9 packages, audit null)

$ ./scripts/manifest.py build --root /var/tmp/odl219-a \
    --generated-at 2026-11-08T15:00:00Z --out v1.json
manifest     v1.json (sha256 34ee40a73970ed2491da60bd43dab53d69973b7f70e511fe8bfd9c43c7ab67cf, 45 skills, 9 packages, audit null)
```

The first build is byte-identical to the manifest ODL-214 recorded for root B
(`de184be3…`), which is the point: the copy is that install's app layer, at a
new path, with the three files that named the old root naming the right one
(the `odile-home` skill and the two unit files). The second build is the same
tree at the card's timestamp; that is the version the update starts from.

The candidate gains a new meter and one changed skill, and the v2 manifest
names exactly those two app-layer rows:

```sh
$ sed -i 's#odile-meter/0\.1#odile-meter/0.1.1#g' /var/tmp/odl219-a/usr/local/lib/odile/meter.py
$ printf '\n- Updated in release 2026.11.08-2: the release document is signed and the update verifies it.\n' >> /var/tmp/odl219-a/home/odl214b/.hermes/skills/note-taking/obsidian/SKILL.md
$ ./scripts/manifest.py build --root /var/tmp/odl219-a \
    --generated-at 2026-11-08T15:10:00Z --out v2.json
manifest     v2.json (sha256 44f3a943c82c85127a29314d188684b9d613b3b557cb6188dad83948348d8109, 45 skills, 9 packages, audit null)

$ ./scripts/manifest.py diff v1.json v2.json
-  "meter_sha256": "3db4effbfe8c31b5528d5d012ac2c0186c7d8b94c58dff51e5651b2f6e2f78cb",
+  "meter_sha256": "dd3fbfdce4eb8ba517252b7d9a5dd1050162f51a3443daa6c391cfcafa32f3f2",
-    "note-taking/obsidian": "b94764d4470516f8a3a33d48485426a8866008949cf915f8198224b3885ad103",
+    "note-taking/obsidian": "efaa9e96c4b02304c7c3abebca403d9e3c47f8053909afc7aa9f81a017352f93",
```

Then one command cuts and signs the release:

```sh
$ ./update/odile_update.py release --root /var/tmp/odl219-a --user odl214b \
    --manifest v2.json --release 2026.11.08-2 --boot-kernel "$(uname -r)" \
    --generated-at 2026-11-08T15:10:00Z --out-dir serve/good \
    --key /root/.odile/installer-signing.pem \
    --notes "meter 0.1.1; the obsidian skill names the signed release"
artifact     meter     meter.py (33911 bytes, sha256 dd3fbfdce4eb…)
artifact     installer odile_install.py (153069 bytes, sha256 a666f7c85631…)
artifact     skills    skills.tar.gz (839373 bytes, sha256 193cc19dff27…)
release      serve/good/release.json (release 2026.11.08-2, manifest sha256 44f3a943c82c…)
sign         serve/good/manifest.json.sig (key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
sign         serve/good/release.json.sig (key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
```

The cut is reproducible: the skills tarball is written with sorted entries,
`mtime 0`, uid/gid 0 and canonical bytes, so a second cut of the same tree is
the same bytes, and Ed25519 signatures are deterministic too:

```sh
$ cmp serve/good/skills.tar.gz serve/good2/skills.tar.gz && echo identical
identical
$ cmp serve/good/release.json serve/good2/release.json && echo identical
identical
$ ./scripts/manifest.py verify --manifest serve/good/manifest.json \
    --signature serve/good/manifest.json.sig --pubkey installer/odile-signing-key.pub
signature: OK (sha256 44f3a943c82c85127a29314d188684b9d613b3b557cb6188dad83948348d8109, key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
manifest     schema 1 · 45 skills · hermes 2bd378ed1b8d · deepseek-ai/DeepSeek-V4.1-Flash-1017-abliterated
```

**The cut writes the signed pair too.** Beside the seven names the schema
defines, `release` copies the installer's detached signature and the published
key into `--out-dir` and prints one line each, so the directory the command
leaves behind is the whole bundle of §3:

```sh
$ ./update/odile_update.py release … --out-dir release --key /root/.odile/installer-signing.pem
…
copy         release/odile_install.py.sig (installer/odile_install.py.sig → 90 bytes, sha256 a4b9319b6471…)
copy         release/odile-signing-key.pub (installer/odile-signing-key.pub → 113 bytes, sha256 13e8c2aee826…)
```

The signature it copies is the one beside the installer the cut exports
(`layout.installer` + `.sig` on a tenant root, `installer/odile_install.py.sig`
for a scratch root) and the key is `installer/odile-signing-key.pub`. Before
writing anything the cut verifies that signature over the exported installer
bytes: a stale pair makes it exit `3` with no bundle, rather than shipping a
download that fails at the user's first command. A rebuild from a signed
installer therefore needs no `install` and no re-sign. (The transcript above is
the ODL-219 cut, from before the pair was part of the command; its manual
`install` step is gone.)

`tests/test_update_release.py` cuts into a temp directory and asserts the nine
names — the seven `CORE_RELEASE_FILES` requires, `webapp.tar.gz` when the cut
carries it, and the key — that the two copies are byte-identical to the
committed pair, and that a bad signature refuses the cut. `tests/test_flow.py`
still fails when a committed `release/` lacks `odile_install.py.sig`
(`CORE_RELEASE_FILES`) or when the key copy differs from
`installer/odile-signing-key.pub`, so a bundle built by hand still has to pass
the same gate.

The files are served at `https://odile.fyi/release.json` and
`https://odile.fyi/release.json.sig`, with the manifest and artifacts beside
them. The directory is `release/` in the deployed tree: `app.py` maps the eight
fixed names in §3 to files in it, reads the file on every request and sends
`Cache-Control: no-store`, so a release is published by replacing the directory
and deploying — no service restart, and no stale bytes. A file with any other
name is not a URL. The guest verifies both signatures and every hash, so a
half-copied directory is refused on the machine, not trusted. The ODL-219
evidence run served a loopback copy with `python3 -m http.server`, which
exercises the same HTTP client path; the live route is measured in §12.

### The re-cut that carries `odile-recipes` (ODL-234's follow-up)

ODL-234 embedded `odile-recipes` in the installer; `2026.11.09-1` predates
that embed, so a tenant installing from `https://odile.fyi` got
`odile-first-run` and `odile-help` but not the recipes skill. The first cut
(`2026.11.10-1`, installer `d8535f2b…`) was overtaken while it sat in review:
`main` changed the installer twice (ODL-471's recovery-file download and
`6dfb682`'s tenant template), so the release was re-cut on 2026-11-11 from a
scratch root the merged installer made, converged on the second run, with the
manifest rebuilt from that same root:

```sh
$ ./installer/sim_tenant.py --root /var/tmp/odile-093a49b5-cand --user odiletest093c \
    --port 9290 --meter-port 9291 --seed-port 9292 --reset --apply
…
run          1: exit 0 in 3m 40s; log /var/tmp/odile-093a49b5-cand/evidence/run1.log
ok   the first run exits 0
ok   the first run provisioned
…
run          2: exit 0 in 0m 1s; log /var/tmp/odile-093a49b5-cand/evidence/run2.log
ok   the second run exits 0
ok   the second run prints `tenant: same`
ok   the second run changes nothing: 0 write line(s)

simulation: PASS (22 passed, 0 failed)
```

This run was made in a private mount namespace with a card-private seed ledger
(`unshare -m`, then a bind mount over `/root/.odile/installer-sim`): the
ledger path is hardcoded and a concurrent card's `sim_tenant.py --reset`
deletes it, which turns a clean run into `FAIL a replayed token is
refused: 404 unknown_token`.

```sh
$ ./scripts/manifest.py build --root /var/tmp/odile-093a49b5-cand --user odiletest093c \
    --installer /var/tmp/odile-093a49b5-cand/bootstrap/odile_install.py \
    --generated-at 2026-11-11T07:05:00Z --out …/release-manifest.json
manifest     … (sha256 0b846c021874…, 48 skills, 9 packages, audit null)

$ ./update/odile_update.py release --root /var/tmp/odile-093a49b5-cand --user odiletest093c \
    --manifest …/release-manifest.json --release 2026.11.11-1 --boot-kernel "$(uname -r)" \
    --generated-at 2026-11-11T07:05:00Z --out-dir release \
    --key /root/.odile/installer-signing.pem \
    --notes "the signed installer (78fc0a31) and meter (a4aab3cf) the tenant runs; the bundle carries the installer's signature, the published key and odile-recipes"
artifact     meter     meter.py (44642 bytes, sha256 a4aab3cf198e…)
artifact     installer odile_install.py (415378 bytes, sha256 78fc0a319546…)
artifact     skills    skills.tar.gz (851822 bytes, sha256 30e7c1d315b7…)
copy         release/odile_install.py.sig (installer/odile_install.py.sig → 90 bytes, sha256 8e61f6034469…)
copy         release/odile-signing-key.pub (installer/odile-signing-key.pub → 113 bytes, sha256 13e8c2aee826…)
release      release/release.json (release 2026.11.11-1, manifest sha256 0b846c021874…)
sign         release/manifest.json.sig (key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
sign         release/release.json.sig (key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
```

The bundle has 48 skills and the row this cut exists for is present:

```sh
$ tar -tzf release/skills.tar.gz | grep odile-recipes
odile-recipes/SKILL.md
```

No installer was re-signed: the artifact is the committed pair, and the cut
verified `installer/odile_install.py.sig` over the exported bytes before it
wrote anything. The cut writes every name `0644`; the committed copy of the
installer is `0755` (`chmod 0755 release/odile_install.py` after a cut) so the
directory can be run directly, as §12's `release/odile_install.py
--verify-signature` does. HTTP carries no mode: the ritual runs `python3
odile_install.py …` (`install.md` §12.2) and installs the copy `0755` on the
machine. The published bytes were then installed into a **second** fresh
scratch root — `sim_tenant.py` driven with the release copies of the installer,
its signature and the key — to prove the download a tenant gets is the one that
writes `$HOME/AGENTS.md` and the tenant skills:

```sh
$ /usr/bin/python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, "installer")
import sim_tenant as sim
release = Path("release").resolve()
sim.INSTALLER = release / "odile_install.py"
sim.INSTALLER_SIG = release / "odile_install.py.sig"
sim.PUBKEY = release / "odile-signing-key.pub"
raise SystemExit(sim.main([
    "--root", "/var/tmp/odile-093a49b5-rel", "--user", "odiletest093d",
    "--port", "9300", "--meter-port", "9301", "--seed-port", "9302",
    "--reset", "--apply",
]))
PY
…
run          1: exit 0 in 3m 39s; log /var/tmp/odile-093a49b5-rel/evidence/run1.log
ok   the second run prints `tenant: same`
simulation: PASS (22 passed, 0 failed)

$ R=/var/tmp/odile-093a49b5-rel
$ sha256sum $R/bootstrap/odile_install.py release/odile_install.py
78fc0a319546e83492d15d4fd3785dd9630ce43e68392958a6fcdceb418145af  $R/bootstrap/odile_install.py
78fc0a319546e83492d15d4fd3785dd9630ce43e68392958a6fcdceb418145af  release/odile_install.py
$ ls -l $R/home/odiletest093d/AGENTS.md
-rw-r--r-- 1 odiletest093d odiletest093d 1876 … $R/home/odiletest093d/AGENTS.md
$ sha256sum $R/home/odiletest093d/AGENTS.md
0df62439ec050ed39525eab345252fedb97aac5401dd14ddd4701ff484eb76ea  $R/home/odiletest093d/AGENTS.md
$ for s in odile-first-run odile-help odile-recipes; do
    sha256sum $R/home/odiletest093d/.hermes/skills/$s/SKILL.md; done
0ca5d154b315…  …/odile-first-run/SKILL.md
fd1a4910a375…  …/odile-help/SKILL.md
e7e6cf85139a…  …/odile-recipes/SKILL.md
```

The three skill hashes are the checkout's `skills/*/SKILL.md` hashes, so the
download installs the same bytes this tree signs.

### The cut that carries the self-copy installer (2026-11-11)

ODL-332's follow-up made the installer write its own bytes to
`/usr/local/bin/odile-install.sh` before the boot record, so the installer
moved after `2026.11.11-1` and that release became stale by the rule above.
The cut for the merged head (`04b3993b…`, 418,745 bytes) is **`2026.11.11-2`**,
from a fresh scratch root `sim_tenant.py --reset --apply` converged — run 1
installs, run 2 prints `same`:

That cut was itself overtaken: ODL-473 embedded the `/verify` page, the boot
report wrapper and the strict-bar reporter in the installer, moving the bytes to
`671293ed…` (550,034 bytes). **`2026.11.11-3`** is ODL-473's cut that names
that pair; it is superseded by the merged cut below.

```sh
$ unshare -m bash -c 'mount --bind /var/tmp/odile-332-seed/ledger /root/.odile/installer-sim && exec /usr/bin/python3 installer/sim_tenant.py --root /var/tmp/odile-332-cand --user odiletest332 --port 9330 --meter-port 9331 --seed-port 9332 --reset --apply'
reset        root /var/tmp/odile-332-cand and user odiletest332
…
run          1: exit 0 in 3m 17s; log /var/tmp/odile-332-cand/evidence/run1.log
ok   the first run exits 0
ok   the first run provisioned
…
run          2: exit 0 in 0m 1s; log /var/tmp/odile-332-cand/evidence/run2.log
ok   the second run exits 0
ok   the second run prints `tenant: same`
ok   the second run changes nothing: 0 write line(s)

simulation: PASS (22 passed, 0 failed)
```

The private mount over the seed ledger is the harness note the earlier re-cut
carries: the ledger path is hardcoded and a concurrent card's `--reset` deletes
it mid-run.

```sh
$ ./scripts/manifest.py build --root /var/tmp/odile-332-cand --user odiletest332 \
    --installer /var/tmp/odile-332-cand/bootstrap/odile_install.py \
    --generated-at 2026-11-11T09:39:35Z --out /var/tmp/odile-332-publish/cand-manifest.json
manifest     /var/tmp/odile-332-publish/cand-manifest.json (sha256 b2f4fb884cc79e2391f466690f1095de5a8db9b8ce1a7ab84107cbf7a705465f, 48 skills, 9 packages, audit null)

$ ./update/odile_update.py release --root /var/tmp/odile-332-cand --user odiletest332 \
    --manifest /var/tmp/odile-332-publish/cand-manifest.json \
    --release 2026.11.11-2 --boot-kernel "$(uname -r)" \
    --generated-at 2026-11-11T09:55:00Z --out-dir /var/tmp/odile-332-publish/recut-fresh \
    --key /root/.odile/installer-signing.pem \
    --notes "the self-copy installer (04b3993b) and meter (a4aab3cf) the tenant runs; /usr/local/bin/odile-install.sh is written by the install itself, so CLAIMS 4.6 hashes a file that exists; the bundle carries the installer's signature, the published key and odile-recipes"
artifact     meter     meter.py (44642 bytes, sha256 a4aab3cf198e…)
artifact     installer odile_install.py (418745 bytes, sha256 04b3993b211d…)
artifact     skills    skills.tar.gz (851822 bytes, sha256 30e7c1d315b7…)
copy         /var/tmp/odile-332-publish/recut-fresh/odile_install.py.sig (/root/.hermes/kanban/boards/odile/workspaces/t_a6726b86/installer/odile_install.py.sig → 90 bytes, sha256 d627772a40ac…)
copy         /var/tmp/odile-332-publish/recut-fresh/odile-signing-key.pub (/root/.hermes/kanban/boards/odile/workspaces/t_a6726b86/installer/odile-signing-key.pub → 113 bytes, sha256 13e8c2aee826…)
release      /var/tmp/odile-332-publish/recut-fresh/release.json (release 2026.11.11-2, manifest sha256 b2f4fb884cc7…)
sign         /var/tmp/odile-332-publish/recut-fresh/manifest.json.sig (key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
sign         /var/tmp/odile-332-publish/recut-fresh/release.json.sig (key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
```

The two `--generated-at` values are the release's own (`release.json`
`2026-11-11T09:55:00Z`, `manifest.json` `2026-11-11T09:39:35Z`), so the cut
reproduces: the manifest from that root is `b2f4fb88…` and every one of the
nine names is byte-identical to the committed `release/` directory:

```sh
$ for f in release.json release.json.sig manifest.json manifest.json.sig \
    odile_install.py odile_install.py.sig odile-signing-key.pub meter.py skills.tar.gz; do
    cmp "/var/tmp/odile-332-publish/recut-fresh/$f" "release/$f" && echo "identical $f"
  done
identical release.json
identical release.json.sig
identical manifest.json
identical manifest.json.sig
identical odile_install.py
identical odile_install.py.sig
identical odile-signing-key.pub
identical meter.py
identical skills.tar.gz
```

No installer was re-signed: the artifact is the committed pair, and the cut
verified `installer/odile_install.py.sig` over the exported bytes before it
wrote anything. §12 measures the published result.

### The cut that carries the manifest placement (2026-11-11)

ODL-472 added the `/manifest` and `/manifest.sig` routes to the installer's
Caddy template and made the first boot place the release installer and the
signed pair, so the installer moved again after `2026.11.11-2` — ODL-473's
`/verify` surface moved it once more and cut `2026.11.11-3`. The cut for the
merged head (`ad04d570…`, 552,849 bytes) is **`2026.11.11-4`**, from a fresh
scratch root `sim_tenant.py --reset --apply` converged — run 1 installs, run 2
prints `tenant: same` with no writes:

```sh
$ mkdir -p /root/.odile/installer-sim /var/tmp/odile-472-sim-merged-ledger
$ unshare -m bash -c 'mount --bind /var/tmp/odile-472-sim-merged-ledger /root/.odile/installer-sim && exec /usr/bin/python3 installer/sim_tenant.py --root /var/tmp/odile-472-sim-merged --user odile472m --port 9340 --meter-port 9341 --seed-port 9342 --reset --apply'
run          1: exit 0 in 3m 17s; log /var/tmp/odile-472-sim-merged/evidence/run1.log
run          2: exit 0 in 0m 1s; log /var/tmp/odile-472-sim-merged/evidence/run2.log
ok   the second run prints `tenant: same`
ok   the second run changes nothing: 0 write line(s)
simulation: PASS (22 passed, 0 failed)
```

The private mount over the seed ledger is the harness note the earlier re-cut
carries: the ledger path is hardcoded and a concurrent card's `--reset` deletes
it mid-run. The `--installer` below names the copy the install writes for
itself at `/usr/local/bin/odile-install.sh`; the manifest's
`installer_sha256` is that file's hash, and the copy is byte-identical to
`release/odile_install.py`:

```sh
$ ./scripts/manifest.py build --root /var/tmp/odile-472-sim-merged --user odile472m \
    --installer /var/tmp/odile-472-sim-merged/usr/local/bin/odile-install.sh \
    --generated-at 2026-11-11T16:45:00Z --out /var/tmp/odile-472-cut4b/cand-manifest.json
manifest     /var/tmp/odile-472-cut4b/cand-manifest.json (sha256 568a0a40d79350ffc2eec898509b8baafd397c41107df1b486396479e2ef63cd, 48 skills, 9 packages, audit null)

$ ./update/odile_update.py release --root /var/tmp/odile-472-sim-merged --user odile472m \
    --manifest /var/tmp/odile-472-cut4b/cand-manifest.json \
    --release 2026.11.11-4 --boot-kernel 6.12.115+deb13.1-amd64 \
    --generated-at 2026-11-11T16:45:00Z --out-dir /var/tmp/odile-472-cut4b/fresh \
    --key /root/.odile/installer-signing.pem \
    --notes "the installer names the release it runs (self-copy), the Caddy block proxies /manifest when the signed pair is present, and the image places both (ODL-472)"
artifact     meter     meter.py (44642 bytes, sha256 a4aab3cf198e…)
artifact     installer odile_install.py (552849 bytes, sha256 ad04d570217f…)
artifact     skills    skills.tar.gz (851822 bytes, sha256 30e7c1d315b7…)
release      /var/tmp/odile-472-cut4b/fresh/release.json (release 2026.11.11-4, manifest sha256 568a0a40d793…)
```

The cut reproduces: the manifest from that root is `568a0a40d793…` and every one
of the nine names is byte-identical to the committed `release/` directory:

```sh
$ for f in release.json release.json.sig manifest.json manifest.json.sig \
    odile_install.py odile_install.py.sig odile-signing-key.pub meter.py skills.tar.gz; do
    cmp "/var/tmp/odile-472-cut4b/fresh/$f" "release/$f" && echo "identical $f"
  done
identical release.json
identical release.json.sig
identical manifest.json
identical manifest.json.sig
identical odile_install.py
identical odile_install.py.sig
identical odile-signing-key.pub
identical meter.py
identical skills.tar.gz
```

No installer was re-signed for the cut: the artifact is the committed pair,
and the cut verified `installer/odile_install.py.sig` over the exported bytes
before it wrote anything. §12 measures the published result.

### The cut that re-pins the vendor install script (2026-11-13)

The vendor replaced `install.sh` after the pin was taken, so the installer that
named `abf33860…` could no longer install on a fresh machine: a 2026-11-13
`sim_tenant.py --reset --apply` stopped before `bash` with

```
error: the fetched Hermes install script does not match its pinned hash
       fetched  50348fcc95e934d2f92b493b40fa2a4f22d58237184ef88ed69ad395531f1ad4 (67104 bytes)
       pinned   abf338605dae6dd28d101673ac6aed7995c955745db46d47676bc591a7957209
       refusing to execute it; nothing was installed
```

ODL-497 read the new bytes and diffed them against the 66,864-byte script the
ODL-454 bench kept (the vendor host only serves the current file). The whole
diff is one flag and its comment: the pinned uv archive is now extracted with
`tar -o -xzf` (`--no-same-owner`) instead of `tar -xzf`, so the tarball's
builder uid 1001 cannot own the `uv` the install runs. No new command, URL,
`eval` or privilege — it narrows what the extractor does as root. The installer
moved to `HERMES_INSTALL_SHA256 = 50348fcc…` and was re-signed (a pin bump
edits the signed file, §5 above). `main` moved while the first cut was in
review: ODL-462's v3.9 claims landing re-cut the embedded `/verify` page and
published `2026.11.12-5`, so this branch merged `main` and re-cut on the merged
head. The merged installer carries both changes and is `afdd50b6…` (603,253
bytes); the cut is **`2026.11.13-2`** from a fresh scratch root that installer
made, converged:

```sh
$ unshare -m bash -c 'mount --bind /var/tmp/odile-497b-seed/ledger /root/.odile/installer-sim && exec /usr/bin/python3 installer/sim_tenant.py --root /var/tmp/odile-497b-rc --user odl497b --reset --apply'
reset        root /var/tmp/odile-497b-rc and user odl497b
create       user odl497b (home /var/tmp/odile-497b-rc/home/odl497b, no sudo)
seed         /root/.odile/installer-sim/seed.db · tenant odl-sim-203 · instance srv-odl-sim-203 · token 9922f7bc…
listening    seed 127.0.0.1:8082 (pid 290969)
run          1: exit 0 in 4m 30s; log /var/tmp/odile-497b-rc/evidence/run1.log
ok   the first run exits 0
ok   the first run provisioned
…
run          2: exit 0 in 0m 1s; log /var/tmp/odile-497b-rc/evidence/run2.log
ok   the second run exits 0
ok   the second run prints `tenant: same`
ok   the second run changes nothing: 0 write line(s)

simulation: PASS (22 passed, 0 failed)
```

The private mount over the seed ledger is the harness note the earlier re-cuts
carry: the ledger path is hardcoded and a concurrent card's `--reset` deletes
it mid-run. Run 1's log names the script it checked:
`install.sh 67104 bytes, sha256 50348fcc95e9… matches the pin`.

```sh
$ sudo -u odl497b -H env HOME=/var/tmp/odile-497b-rc/home/odl497b /usr/bin/python3 - <<'PY'
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("odile_install", "/var/tmp/odile-497b-rc/bootstrap/odile_install.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["odile_install"] = mod
spec.loader.exec_module(mod)
layout = mod.Layout.scratch(root=Path("/var/tmp/odile-497b-rc"), user="odl497b",
                            webapp_port=9140, meter_port=9141,
                            public_url="http://odile-sim.test:9140")
print("changed:", mod.ensure_verify_surface(layout, False))
PY
write        /var/tmp/odile-497b-rc/usr/local/lib/odile/verify.html (the /verify page) sha256 8814faead212…
write        /var/tmp/odile-497b-rc/usr/local/lib/odile/access_report.sh (the boot report wrapper) sha256 c85b4c370b4e…
write        /var/tmp/odile-497b-rc/usr/local/lib/odile/verify_and_unlock.sh (the access reporter) sha256 3a5bd57ca1fe…
write        /var/tmp/odile-497b-rc/usr/local/bin/odile-audit (the handoff audit) sha256 539e205f7d26…
changed: True

$ ./scripts/manifest.py build --root /var/tmp/odile-497b-rc --user odl497b \
    --installer /var/tmp/odile-497b-rc/usr/local/bin/odile-install.sh \
    --generated-at 2026-11-13T05:42:00Z --out /var/tmp/odile-497b-cut/cand-manifest.json
manifest     /var/tmp/odile-497b-cut/cand-manifest.json (sha256 b48549e85318ac14ed3bcde32881a7893b92f13ce47f16c055905ad24fd3cd78, 48 skills, 9 packages, audit 539e205f7d26e1c0757a4cdabbcae444c98df10f6e68eff92b4c147604cf4e4e)

$ ./update/odile_update.py release --root /var/tmp/odile-497b-rc --user odl497b \
    --manifest /var/tmp/odile-497b-cut/cand-manifest.json \
    --release 2026.11.13-2 --boot-kernel 6.12.115+deb13.1-amd64 \
    --generated-at 2026-11-13T05:42:00Z --out-dir release \
    --key /root/.odile/installer-signing.pem \
    --notes "the Hermes install-script pin moves to the 2026-11-13 vendor script (67,104 bytes, sha256 50348fcc; the only change is tar -o --no-same-owner for the pinned uv archive) on the merged head that also carries ODL-462's v3.9 claims; installer afdd50b6, manifest built from a fresh scratch root (ODL-497)"
artifact     meter     meter.py (44642 bytes, sha256 a4aab3cf198e…)
artifact     installer odile_install.py (603253 bytes, sha256 afdd50b6b49a…)
artifact     skills    skills.tar.gz (851865 bytes, sha256 3badf0370fa7…)
copy         release/odile_install.py.sig (installer/odile_install.py.sig → 90 bytes, sha256 22ff70f90a27…)
copy         release/odile-signing-key.pub (installer/odile-signing-key.pub → 113 bytes, sha256 13e8c2aee826…)
release      release/release.json (release 2026.11.13-2, manifest sha256 b48549e85318…)
sign         release/manifest.json.sig (key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
sign         release/release.json.sig (key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
```

The plain simulation writes no first-visit state, so it never reaches
`ensure_verify_surface`; a tenant's first boot does — it writes
`/etc/odile/first-visit.json` before the installer runs — so the audit script
and the `/verify` surface are part of the app layer the release installs.
Calling the installer's own function on the same root records them; the build
against the plain root differs by exactly that one line (`audit_sha256`: null
vs `539e205f…`), so the cut names the authority a sceptic runs.

The manifest records the pinned commit unchanged beside the new pin
(`hermes.commit 2bd378ed1b8d`, `hermes.install_script_sha256 50348fcc…`) and
the installer the root runs. The cut reproduces: a second cut into a scratch
directory is byte-identical on all nine names, and `release.json` names
installer `afdd50b6…` (603,253 bytes) and manifest `b48549e8…`:

```sh
$ for f in release.json release.json.sig manifest.json manifest.json.sig \
    odile_install.py odile_install.py.sig odile-signing-key.pub meter.py skills.tar.gz; do
    cmp "/var/tmp/odile-497b-cut/fresh/$f" "release/$f" && echo "identical $f"
  done
identical release.json
identical release.json.sig
identical manifest.json
identical manifest.json.sig
identical odile_install.py
identical odile_install.py.sig
identical odile-signing-key.pub
identical meter.py
identical skills.tar.gz

$ /usr/bin/python3 -c 'import json;d=json.load(open("release/release.json"));print(d["release"],d["generated_at"],d["artifacts"]["installer"]["sha256"],d["artifacts"]["installer"]["size"],d["manifest"]["sha256"])'
2026.11.13-2 2026-11-13T05:42:00Z afdd50b6b49a1c23683dc8e7bccbf5714b0af57a959c0e1c1491b2042d45e1a5 603253 b48549e85318ac14ed3bcde32881a7893b92f13ce47f16c055905ad24fd3cd78
```

The cut is committed, not deployed. Measured 2026-11-13T05:48Z, the URL serves
`2026.11.12-4` (installer `2405fd58…`, 602,968 bytes; manifest `70887d8a…`),
the deployed pair, one cut behind `main`'s `2026.11.12-5` (installer
`42ca1846…`, 603,046 bytes; manifest `6e6a6df3…`); the next `./scripts/deploy.sh` from
a `main` that carries that merge published `2026.11.13-2`. ODL-496 took
`2026.11.13-3`; the ODL-215 follow-up cut `2026.11.13-4` at its merged head,
which `main` re-signed in place when the live addresses left the tree, and
ODL-514 took and deployed `2026.11.13-5` from its own branch. ODL-504 re-cut
`2026.11.13-6` on top of the re-signed pair, and ODL-514 merged that head and
cut and deployed `2026.11.13-7` (`install.md` §12.6). The release id must not be
reused: `-8` and `-9` are carried by open branches and `2026.11.14-2` was
taken and deployed by t_9c2d31f1, so the next cut in this tree takes
`2026.11.14-3` (below).

```sh
$ date -u +%Y-%m-%dT%H:%M:%SZ
2026-11-13T05:48:32Z
$ curl -fsSL https://odile.fyi/release.json | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["release"],d["generated_at"],d["manifest"]["sha256"],d["artifacts"]["installer"]["sha256"],d["artifacts"]["installer"]["size"])'
2026.11.12-4 2026-11-12T23:43:26Z 70887d8a3716e5229e1ac552a0aadfa602f0f232325a1f91e9d5ada2ec4ab04a 2405fd5830f4adb2a5af3de5a9a4577c6c66c00c002766f3b67f000e33826123 602968
$ curl -fsSL https://odile.fyi/odile_install.py | sha256sum
2405fd5830f4adb2a5af3de5a9a4577c6c66c00c002766f3b67f000e33826123  -
$ curl -fsSL https://odile.fyi/manifest.json | sha256sum
70887d8a3716e5229e1ac552a0aadfa602f0f232325a1f91e9d5ada2ec4ab04a  -
```

A first boot from this cut is proved by running the **release copies** through
the same harness on a second fresh root (`sim_tenant.INSTALLER/.INSTALLER_SIG/
.PUBKEY` pointed at `release/`), which is what a download a stranger gets would
install: run 1 fetched the new script and matched the pin, run 2 read `same`;
the root's `bootstrap/` copy and its `/usr/local/bin/odile-install.sh` are the
release's bytes and the Hermes stamp names the pinned commit:

```sh
$ sha256sum /var/tmp/odile-497b-rel/bootstrap/odile_install.py release/odile_install.py
afdd50b6b49a1c23683dc8e7bccbf5714b0af57a959c0e1c1491b2042d45e1a5  /var/tmp/odile-497b-rel/bootstrap/odile_install.py
afdd50b6b49a1c23683dc8e7bccbf5714b0af57a959c0e1c1491b2042d45e1a5  release/odile_install.py
$ grep -n 'install.sh 67104\|pinned 2bd378ed' /var/tmp/odile-497b-rel/evidence/run1.log
24:install      Hermes as odl497brel into /var/tmp/odile-497b-rel/home/odl497brel/.hermes (pinned 2bd378ed1b8d, browser/cua skipped)
25:             install.sh 67104 bytes, sha256 50348fcc95e9… matches the pin
$ grep -c 'does not match its pinned hash' /var/tmp/odile-497b-rel/evidence/run1.log
0
$ /usr/bin/python3 -c 'import json;print(json.load(open("/var/tmp/odile-497b-rel/home/odl497brel/.hermes/gateway_state.json"))["code_sha"])'
2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19
```

### The second re-pin of the vendor install script (2026-11-14)

The vendor moved `install.sh` again after ODL-497's pin, so a fresh install
stopped before `bash` with

```
error: the fetched Hermes install script does not match its pinned hash
       fetched  c73dad4dcf7c9ecadb98578e28cc763d23d7be3fa1b513545319038795493214 (68015 bytes)
       pinned   50348fcc95e934d2f92b493b40fa2a4f22d58237184ef88ed69ad395531f1ad4
       refusing to execute it; nothing was installed
```

Read against the 67,104-byte script ODL-497 pinned (kept at
`/var/tmp/install-now.sh`), the whole diff is three hunks of git-location
hygiene: a new `drop_git_location_env` unsets an inherited `GIT_DIR` family
before `stage_repository` and `stage_complete` touch the checkout, and the
broken-`.git` probe is fenced with `GIT_CEILING_DIRECTORIES` so a `.git` git
cannot read cannot walk up into a git-managed `$HOME` (#146032). No new
command, URL, `eval` or privilege.

The baseline moved while the card was in flight: `main` became t_e237e261's
meter-free installer (`2026.11.14-1`, `f211738c…`, 616,222 bytes), and an
earlier `2026.11.14-2` (t_9c2d31f1's HQ `/verify` cut) was cut and deployed
with the old pin still in place. This branch merged `0a4b726`, re-applied the
pin to the meter-free installer and re-signed it in the same commit: the file
moves to `HERMES_INSTALL_SHA256 = c73dad4d…`, sha256 `fd405391…` (616,445
bytes), `--self-test` PASS and `--verify-signature` `OK`.

The fresh root was made by that merged installer, with the seed ledger on a
private mount so a concurrent card's `--reset` cannot delete it mid-run:

```sh
$ unshare -m bash -c 'mount --bind /var/tmp/odile-t_19642a84-seed/ledger /root/.odile/installer-sim && exec /usr/bin/python3 installer/sim_tenant.py --root /var/tmp/odile-t_19642a84-rc2 --user odl19642b --reset --apply'
reset        root /var/tmp/odile-t_19642a84-rc2 and user odl19642b
create       user odl19642b (home /var/tmp/odile-t_19642a84-rc2/home/odl19642b, no sudo)
seed         /root/.odile/installer-sim/seed.db · tenant odl-sim-203 · instance srv-odl-sim-203 · token c74804b5…
listening    seed 127.0.0.1:8082 (pid 3987161)
run          1: exit 0 in 4m 24s; log /var/tmp/odile-t_19642a84-rc2/evidence/run1.log
ok   the first run exits 0
ok   the first run provisioned
…
run          2: exit 0 in 0m 2s; log /var/tmp/odile-t_19642a84-rc2/evidence/run2.log
ok   the second run exits 0
ok   the second run prints `tenant: same`
ok   the second run changes nothing: 0 write line(s)

simulation: PASS (24 passed, 0 failed)
```

The meter-free harness has 24 assertions: the old 22's one meter row became
three per-user-key checks (`/etc/odile/key.env` 0600, the unit's
`EnvironmentFile` in the gateway environment, no inference key in the profile
`.env`). Run 1 is the one that proves the URL — it fetched it over HTTPS and
its log names the bytes it checked before `bash`, with no pin refusal anywhere
in the log:

```sh
$ grep -n 'install.sh\|does not match' /var/tmp/odile-t_19642a84-rc2/evidence/run1.log
26:             install.sh 68015 bytes, sha256 c73dad4dcf7c… matches the pin
$ grep -c 'does not match its pinned hash' /var/tmp/odile-t_19642a84-rc2/evidence/run1.log
0
```

The tenant's first boot also writes the `/verify` surface before the release
installer runs, so the same root gained the audit script and the two reporter
programs (`ensure_verify_surface` through the root's own bootstrap copy), and
the manifest build from that root records them:

```sh
$ ./scripts/manifest.py build --root /var/tmp/odile-t_19642a84-rc2 --user odl19642b \
    --installer /var/tmp/odile-t_19642a84-rc2/usr/local/bin/odile-install.sh \
    --generated-at 2026-11-14T07:57:20Z --out /var/tmp/odile-t_19642a84-cut3/manifest.json
manifest     /var/tmp/odile-t_19642a84-cut3/manifest.json (sha256 d0f35d7bbc824b08cb885d35f39e69fb38f3634457f6617786d31a7bb7f02b3f, 48 skills, 9 packages, audit 539e205f7d26e1c0757a4cdabbcae444c98df10f6e68eff92b4c147604cf4e4e)

$ ./scripts/manifest.py diff release/manifest.json /var/tmp/odile-t_19642a84-cut3/manifest.json
--- manifest-a
+++ manifest-b
@@ -3,7 +3,7 @@
   "hermes": {
     "commit": "2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19",
     "commit_source": "git",
-    "install_script_sha256": "50348fcc95e934d2f92b493b40fa2a4f22d58237184ef88ed69ad395531f1ad4",
+    "install_script_sha256": "c73dad4dcf7c9ecadb98578e28cc763d23d7be3fa1b513545319038795493214",
     "lockfiles": {
@@ -1338,7 +1338,7 @@
-  "installer_sha256": "f211738c5012b6350f1dccde95167fde756d5c003a4816ad175dc3792b1c583a",
+  "installer_sha256": "fd40539149616676b0024bdefbc30a22add9678a616fd3743a13229e9fcedb56",
```

The cut (the meter-free release has eight names, not nine):

```sh
$ ./update/odile_update.py release --root /var/tmp/odile-t_19642a84-rc2 --user odl19642b \
    --manifest /var/tmp/odile-t_19642a84-cut3/manifest.json \
    --release 2026.11.14-3 --boot-kernel 6.12.115+deb13.1-amd64 \
    --generated-at 2026-11-14T07:57:20Z --out-dir release \
    --key /root/.odile/installer-signing.pem \
    --notes "ODL-497 follow-up on the meter-free main (0a4b726): … installer fd405391 built from the merged head, manifest from a fresh scratch root whose own run fetched the vendor URL and matched the pin (24/24, run 2 tenant: same). The live 2026.11.14-2 (t_9c2d31f1) still pins 50348fcc."
artifact     installer odile_install.py (616445 bytes, sha256 fd4053914961…)
artifact     skills    skills.tar.gz (851865 bytes, sha256 09be3cd1bd05…)
copy         release/odile_install.py.sig (installer/odile_install.py.sig → 90 bytes, sha256 67adb75dfa02…)
copy         release/odile-signing-key.pub (installer/odile-signing-key.pub → 113 bytes, sha256 13e8c2aee826…)
release      release/release.json (release 2026.11.14-3, manifest sha256 d0f35d7bbc82…)
sign         release/manifest.json.sig (key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
sign         release/release.json.sig (key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
```

A second cut into a scratch directory is byte-identical on all eight names,
both JSON signatures verify against `installer/odile-signing-key.pub`, and
`./update/odile_update.py self-test` printed `self-test: 37/37 passed`:

```sh
$ for f in release.json release.json.sig manifest.json manifest.json.sig \
    odile_install.py odile_install.py.sig odile-signing-key.pub skills.tar.gz; do
    cmp "release/$f" "/var/tmp/odile-t_19642a84-cut5/$f" && echo "identical $f"
  done
identical release.json
identical release.json.sig
identical manifest.json
identical manifest.json.sig
identical odile_install.py
identical odile_install.py.sig
identical odile-signing-key.pub
identical skills.tar.gz
```

`release.json` names `2026.11.14-3` at `2026-11-14T07:57:20Z`, installer
`fd405391…` (616,445 bytes) and manifest `d0f35d7b…` (49,957 bytes). It is
committed, not deployed: measured 2026-11-14T07:58Z, the URL serves
`2026.11.14-2` (installer `bac5e058…`, 616,145 bytes; manifest `0bcc735d…`),
which still pins the old script, so the next `./scripts/deploy.sh` from a `main` that
carries this merge publishes the new cut.

## 6. The guest: check, apply, rollback

The machine's v1 tree is its identity: `manifest.py build` is
`34ee40a7…` at `15:00:00Z`. The commands below set `R` and `U` once; the
guest fetches the release over HTTP, verifies both signatures and every
artifact, and prints the plan; nothing is written:

```sh
$ R=/var/tmp/odl219-a
$ U=http://127.0.0.1:9180
$ ./update/odile_update.py check --root "$R" --user odl214b \
    --release-url "$U/good/release.json"
release      2026.11.08-2 (generated 2026-11-08T15:10:00Z)
notes        meter 0.1.1; the obsidian skill names the signed release
signature    OK (release.json and manifest.json; key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
boot         kernel 6.12.115+deb13.1-amd64 matches this machine
manifest     schema 1 · 45 skills · hermes 2bd378ed1b8d · meter dd3fbfdce4eb · installer a666f7c85631
plan         meter     replace   3db4effbfe8c → dd3fbfdce4eb
plan         skills    replace   1 changed, 0 added, 0 left in place
plan         records   write     manifest.json and release.json
available    unrecorded → 2026.11.08-2
```

Four refusals, each with its exit code, leave the tree untouched:

```sh
$ ./update/odile_update.py check --root "$R" --user odl214b \
    --release-url "$U/bad-signature/release.json"
error: the signature on http://127.0.0.1:9180/bad-signature/release.json is not valid for this key
exit=3

$ ./update/odile_update.py check --root "$R" --user odl214b \
    --release-url "$U/bad-manifest/release.json"
error: the signature on http://127.0.0.1:9180/bad-manifest/manifest.json is not valid for this key
exit=3

$ ./update/odile_update.py check --root "$R" --user odl214b \
    --release-url "$U/bad-artifact/release.json"
error: the meter artifact does not match its signed sha256
exit=4

$ ./update/odile_update.py check --root "$R" --user odl214b \
    --release-url "$U/wrong-kernel/release.json"
error: release 2026.11.08-2 was built for kernel 0.0.0-not-this-kernel, this machine runs 6.12.115+deb13.1-amd64: a boot-chain change is rebuild-and-restore
exit=5

$ ./scripts/manifest.py build --root "$R" \
    --generated-at 2026-11-08T15:00:00Z --out v1-after-refusals.json
manifest     v1-after-refusals.json (sha256 34ee40a73970ed2491da60bd43dab53d69973b7f70e511fe8bfd9c43c7ab67cf, …)
$ cmp v1.json v1-after-refusals.json && echo identical
identical
```

`bad-manifest` is the hard case: the manifest bytes are changed **and** the
release is re-signed to name them. The release signature verifies; the
manifest's own signature does not, and the update stops at exit 3.

The apply writes the two changed components and the release records, then
checks the whole app layer against the manifest before it reports success:

```sh
$ ./update/odile_update.py apply --root "$R" --user odl214b --yes \
    --release-url "$U/good/release.json"
release      2026.11.08-2 (generated 2026-11-08T15:10:00Z)
notes        meter 0.1.1; the obsidian skill names the signed release
signature    OK (release.json and manifest.json; key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
boot         kernel 6.12.115+deb13.1-amd64 matches this machine
manifest     schema 1 · 45 skills · hermes 2bd378ed1b8d · meter dd3fbfdce4eb · installer a666f7c85631
plan         meter     replace   3db4effbfe8c → dd3fbfdce4eb
plan         skills    replace   1 changed, 0 added, 0 left in place
plan         records   write     manifest.json and release.json
snapshot     /var/tmp/odl219-a/var/lib/odile/odile-update/snapshot (release none, hermes 2bd378ed1b8d)
write        /var/tmp/odl219-a/usr/local/lib/odile/meter.py
write        /var/tmp/odl219-a/home/odl214b/.hermes/skills/note-taking/obsidian
write        /var/tmp/odl219-a/home/odl214b/.hermes/skills/.bundled_manifest
record       /var/tmp/odl219-a/etc/odile/manifest.json and release.json
scratch      no systemd units started; restart the simulation by hand if it is running
applied      release 2026.11.08-2
rollback     odile-update rollback --yes

$ ./scripts/manifest.py build --root "$R" \
    --generated-at 2026-11-08T15:10:00Z --out after-apply.json
manifest     after-apply.json (sha256 44f3a943c82c85127a29314d188684b9d613b3b557cb6188dad83948348d8109, …)
$ cmp v2.json after-apply.json && echo identical
identical
```

The applied machine builds the release's manifest byte for byte — including the
timestamp, because the release manifest records the candidate's `generated_at`.
That is a stronger statement than "the two files changed": the whole app layer,
45 skill hashes and 104 Python pins included, is the signed release.

A second run is a no-op, and an older release is refused:

```sh
$ ./update/odile_update.py check --root "$R" --user odl214b \
    --release-url "$U/good/release.json"
current      already at release 2026.11.08-2          # exit 10
$ ./update/odile_update.py apply --root "$R" --user odl214b --yes \
    --release-url "$U/good/release.json"
already at release 2026.11.08-2                       # exit 0
$ ./update/odile_update.py check --root "$R" --user odl214b \
    --release-url "$U/old-release/release.json"
error: release 2026.11.08-1 (2026-11-08T14:00:00Z) is older than the installed 2026.11.08-2 (2026-11-08T15:10:00Z); pass --allow-downgrade to apply
                                                      # exit 5
```

Rollback restores the snapshot taken before the first write, and the restore is
checked by rebuilding the old manifest:

```sh
$ ./update/odile_update.py rollback --root "$R" --user odl214b --yes
scratch      no systemd units started; restart the simulation by hand if it is running
rolled back  /var/tmp/odl219-a/var/lib/odile/odile-update/snapshot

$ ./scripts/manifest.py build --root "$R" \
    --generated-at 2026-11-08T15:00:00Z --out after-rollback.json
manifest     after-rollback.json (sha256 34ee40a73970ed2491da60bd43dab53d69973b7f70e511fe8bfd9c43c7ab67cf, …)
$ cmp v1.json after-rollback.json && echo identical
identical
$ ./update/odile_update.py rollback --root "$R" --user odl214b --yes
same         the tree already matches /var/tmp/odl219-a/var/lib/odile/odile-update/snapshot
```

The guest initiates. The release server's log for the run ends with the
requests the update made; nothing on HQ opened a connection to the machine:

```sh
$ grep ' /good/' http.log | tail -8
127.0.0.1 - - [08/Nov/2026 15:58:40] "GET /good/skills.tar.gz HTTP/1.1" 200 -
127.0.0.1 - - [08/Nov/2026 15:58:40] "GET /good/release.json HTTP/1.1" 200 -
127.0.0.1 - - [08/Nov/2026 15:58:40] "GET /good/release.json.sig HTTP/1.1" 200 -
127.0.0.1 - - [08/Nov/2026 15:58:40] "GET /good/manifest.json HTTP/1.1" 200 -
127.0.0.1 - - [08/Nov/2026 15:58:40] "GET /good/manifest.json.sig HTTP/1.1" 200 -
127.0.0.1 - - [08/Nov/2026 15:58:40] "GET /good/odile_install.py HTTP/1.1" 200 -
127.0.0.1 - - [08/Nov/2026 15:58:40] "GET /good/meter.py HTTP/1.1" 200 -
127.0.0.1 - - [08/Nov/2026 15:58:40] "GET /good/skills.tar.gz HTTP/1.1" 200 -
```

## 7. What the update writes

| Path | Mode | Owner | Content |
|---|---|---|---|
| `/usr/local/bin/odile-install.sh` (tenant) / `<root>/bootstrap/odile_install.py` (scratch) | unchanged | unchanged | the release's installer |
| `~/.hermes/skills/<name>/…` | from the artifact | the skills directory's owner | rendered skills, only those that changed |
| `~/.hermes/skills/.bundled_manifest` | 0644 | the skills directory's owner | the release's bundle manifest |
| `~/.hermes/hermes-agent/apps/desktop/dist-webapp/` | from the artifact | unchanged | the Webapp bundle, only when it moves |
| `/etc/odile/manifest.json`, `.sig` | 0644 | `/etc/odile`'s owner | the verified release manifest, exactly as signed |
| `/etc/odile/release.json`, `.sig` | 0644 | `/etc/odile`'s owner | the verified release document, exactly as signed |
| `/var/lib/odile/odile-update/snapshot/` | 0755 (directory; files 0644) | root | `snapshot.json` and the previous bytes |
| `/var/lib/odile/odile-update.log` | 0644 | root | one line per apply, failure, rollback; no secret |

A first update on a fresh machine has no `/etc/odile/release.json`; the
snapshot records `previous_release: none` and rollback removes the records it
wrote. One snapshot is kept: each apply replaces it, so `rollback` is one step,
not a history.

On a tenant, only a changed Hermes commit or Webapp bundle restarts
`odile-agent`; the installer and skill writes restart nothing. A restart failure
fails the update, which restores the snapshot and restarts every unit whose bytes
moved — `odile-agent` for the Hermes checkout or the Webapp bundle — once each,
so a rollback never leaves a unit running the version it rolled away from. In a
scratch root no systemd unit is touched; the simulation's processes are started
by its own harness.

## 8. The daily timer (opt-in)

The default in V0.1 is manual: the user runs `sudo odile-update`. A user who
consents to a daily check gets a timer; the installer asks, and ODL-332 installs
it. The unit is deliberately a thin wrapper around `apply --yes`, whose plan is
printed to the journal:

```ini
# /etc/systemd/system/odile-update.service
[Unit]
Description=Odile app-layer update (signed release, one rollback step)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/odile-update apply --yes
```

```ini
# /etc/systemd/system/odile-update.timer
[Unit]
Description=Odile daily app-layer update check

[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
```

`systemctl enable --now odile-update.timer` turns it on;
`systemctl disable --now odile-update.timer` turns it off. A release is
refused for this machine (boot chain, moved environment, downgrade) before
anything is written, so a timer that runs on a machine the release was not cut
for is a journal line, not a broken machine.

## 9. The tenant wiring (ODL-332)

ODL-332 owns the machine half. It needs to:

- install this file at `/usr/local/bin/odile-update` (mode 0755) and put the
  published key at `/etc/odile/odile-signing-key.pub` (0644);
- fetch `https://odile.fyi/release.json`; HQ already serves the directory from
  the deployed tree (§5), so this is a key and a URL, not a new route;
- cut the release for the **tenant image's** kernel, not HQ's; `--boot-kernel`
  is required for that reason;
- install the timer only if the user consents, and record the answer in the
  install transcript;
- after the first update, run `manifest.py build --root / --user odile` and
  keep the result as the machine's own record (the release manifest is the
  release's record; §10).

## 10. Boundaries and known gates

- **The boot chain is pinned.** A release names one kernel release. Kernel,
  initramfs, cmdline and OS-release changes are rebuild-and-restore; the update
  refuses them with exit 5 rather than half-applying.
- **A Hermes commit change is narrow by design.** The checkout must already
  carry the commit, be clean, and the release's `uv.lock`, `package-lock.json`
  and installed Python pins must equal the machine's. A release that moves the
  environment is a reinstall. A commit change also needs a `webapp` artifact;
  the update does not build on the tenant (a failed build mid-update would serve
  neither version).
- **The release manifest is cut on HQ** (`mode: scratch`, `packages.source:
  probe`). Those two fields, and `packages.os`, are the release's provenance,
  not the tenant's dpkg. The machine's own record is `manifest.py build` on the
  tenant; ODL-214 §8 defines which fields to compare.
- **A skill the release drops is left in place.** The update replaces the
  skills the release names and reports how many installed skills it did not
  touch. Pruning is a later decision, not a surprise.
- **One snapshot.** Rollback is one step. Two failed applies in a row keep the
  latest snapshot, which is the state before the latest apply.
- **`audit_sha256` is null** until ODL-208 installs the handoff audit; a
  release does not carry that artifact yet, and an unknown artifact name is
  refused.
- **The public key is served** at `https://odile.fyi/odile-signing-key.pub`
  (measured 200 from HQ); the key the update *uses* is the recovery kit's copy,
  installed locally at `/etc/odile/odile-signing-key.pub`.
- **The installed installer's name differs by mode.** A tenant resolves
  `/usr/local/bin/odile-install.sh` first (CLAIMS.md §4.6); a scratch root
  resolves `bootstrap/odile_install.py`. The release names the artifact, not
  the destination.

## 11. The self-test

`./update/odile_update.py self-test` builds a machine, a candidate root, a
signed release and a rollback in one temporary directory. Thirty-seven checks:
the canonical round trip and a cross-check against `scripts/manifest.py`'s
canonicaliser and hasher; a release that plans, applies, verifies and rolls
back; a second apply that writes nothing; a rollback that moved the Hermes
commit or the Webapp bundle restarts `odile-agent`, a rollback that moved only
the installer or the skills restarts nothing, and a no-op rollback restarts
nothing; a dry run of an older release is refused with exit 5 and
`--allow-downgrade` still prints the plan; two post-check tampering cases; a
changed release, a changed manifest under a re-signed release, a changed
artifact, another kernel, a commit the checkout lacks, and two schema refusals.
It prints `self-test: 37/37 passed` and exits 0. `make check` compiles the file
but does not run this self-test; the installer's `--self-test` is a gate step
([`install.md`](install.md) §7), this one is not.

## 12. Evidence

The commands and output above were run in the card's worktree,
`<checkout>/.worktrees/t_2c30a476`, on 2026-11-08. The raw transcript is
`/root/.hermes/profiles/builder/cache/scratch/odl219-evidence/transcript.log`
(the script that writes it is `run_evidence.sh` beside it); the release
directory, the five tampered variants, the manifests and the served copies are
in the same directory. The machine is `/var/tmp/odl219-a`; the release was
served by `python3 -m http.server 9180` from that directory.

| DoD line | Observed |
|---|---|
| a signed update applies in a scratch root | `apply --yes` exit 0; the machine rebuilds the release manifest `44f3a943…` byte for byte (`cmp v2.json after-apply.json` → identical) |
| a tampered manifest is refused | manifest bytes changed and the release re-signed to name them → `signature … is not valid for this key`, exit 3; the tree still rebuilds `34ee40a7…` |
| rollback restores the previous version | `rollback --yes` exit 0; the machine rebuilds `34ee40a7…` byte for byte (`cmp v1.json after-rollback.json` → identical); a second rollback says `same` |
| no operator SSH; the guest initiates | the guest ran every command; the release server's log shows the guest's GETs; no inbound connection to the machine |

The transcript was made at commit `761e25e` (the branch's first commit); later
commits on the branch only change this page. `make check` on the branch: 522
tests OK (skipped=1), meter 12 OK, claims lint 14 files/14 phrases, brand lint
11 surfaces/17 colours; `self-test: 32/32 passed`.

### The live route

The follow-up card (`t_7963fa74`) put release `2026.11.08-2` in the deployed
tree at commit `657ef3f`, byte for byte against this card's evidence directory,
and measured the route over TLS:

```sh
$ curl -sS -D- https://odile.fyi/release.json
HTTP/2 200
cache-control: no-store
content-type: application/json
content-length: 904

$ for f in release.json release.json.sig manifest.json manifest.json.sig \
    meter.py odile_install.py skills.tar.gz; do
    curl -sS -o "/tmp/served/$f" "https://odile.fyi/$f"
    cmp "/tmp/served/$f" "release/$f" && echo "identical $f"
  done
identical release.json
identical release.json.sig
identical manifest.json
identical manifest.json.sig
identical meter.py
identical odile_install.py
identical skills.tar.gz

$ ./update/odile_update.py check --root /var/tmp/odl219-a --user odl214b \
    --release-url https://odile.fyi/release.json
release      2026.11.08-2 (generated 2026-11-08T15:10:00Z)
notes        meter 0.1.1; the obsidian skill names the signed release
signature    OK (release.json and manifest.json; key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
boot         kernel 6.12.115+deb13.1-amd64 matches this machine
manifest     schema 1 · 45 skills · hermes 2bd378ed1b8d · meter dd3fbfdce4eb · installer a666f7c85631
plan         meter     replace   3db4effbfe8c → dd3fbfdce4eb
plan         skills    replace   1 changed, 0 added, 0 left in place
plan         records   write     manifest.json and release.json
available    unrecorded → 2026.11.08-2
                                                      # exit 0
```

The deploy left the waitlist surfaces untouched (`/`, `/faq`, `/verify`,
`/attestation`, `/odile-signing-key.pub` all 200); `/release`, `/release/`,
`/release/meter.py`, `/app.py` and `/release.json/` are 404. The response body
is the file itself, and `make check` stayed green: 556 tests OK (skipped=1),
meter 12 OK, claims 14 files/14 phrases, brand 11 surfaces/17 colours.

### The follow-up: a rollback restarts the agent; a dry run refuses an older release

Two tenant-side gaps the scratch run could not see were closed by the follow-up
card on branch `odl/ODL-219-followup`. A rollback after an apply that moved the
Hermes commit or the Webapp bundle restored the files but restarted only
`odile-meter`, leaving `odile-agent` running the release it had just rolled away
from; and `apply --dry-run` printed a plan for an older release and exited 0,
while `check` and `apply` refused it with exit 5.

The evidence was run on 2026-11-09 against `/var/tmp/odl219-verify`, a copy of
`/var/tmp/odl219-a` that rebuilds `34ee40a7…`, with the evidence release
directory served on loopback: `R=/var/tmp/odl219-verify`,
`U=http://127.0.0.1:9319` from `python3 -m http.server 9319` in
`/root/.hermes/profiles/builder/cache/scratch/odl219-evidence`. The probe
`/root/.hermes/profiles/builder/cache/scratch/odl219fu-evidence/restart_probe.py`
replaces `restart_units` with a recorder, because a scratch root only prints
the scratch line; everything else is the real module.

```sh
$ ./update/odile_update.py apply --yes --root "$R" --user odl214b \
    --release-url "$U/good/release.json"          # exit 0; machine at 2026.11.08-2

$ ./update/odile_update.py apply --dry-run --root "$R" --user odl214b \
    --release-url "$U/old-release/release.json"
error: release 2026.11.08-1 (2026-11-08T14:00:00Z) is older than the installed 2026.11.08-2 (2026-11-08T15:10:00Z); pass --allow-downgrade to apply
                                                      # exit 5
$ ./scripts/manifest.py build --root "$R" \
    --generated-at 2026-11-08T15:10:00Z --out after-dryrun.json
manifest     after-dryrun.json (sha256 44f3a943…, …)  # the dry run wrote nothing

$ git -C "$R/home/odl214b/.hermes/hermes-agent" checkout --detach 9d1f128176
    # stand in for the release that moved Hermes: the snapshot records 2bd378ed
$ /usr/bin/python3 restart_probe.py
restart_units  ['odile-meter', 'odile-agent']
changed        True
$ /usr/bin/python3 restart_probe.py                # a second, no-op rollback
changed        False
units          []
$ ./scripts/manifest.py build --root "$R" \
    --generated-at 2026-11-08T15:00:00Z --out after-rollback.json
manifest     after-rollback.json (sha256 34ee40a7…, …)
$ cmp v1.json after-rollback.json && echo identical
identical
```

The probe records the units instead of starting them; on a tenant the same list
reaches `systemctl restart`, so a failed restart fails the update and the
rollback restarts the restored units rather than leaving one on the new bytes.
The self-test covers the Webapp-bundle half of the same rule: it snapshots the
bundle, replaces `hermes-build.json`, and asserts that the restore asks for
`odile-agent` and puts the bytes back. `make check` on the branch: 604 tests OK
(skipped=1), meter 20 OK, claims lint 15 files/14 phrases, brand lint 11
surfaces/17 colours; `self-test: 38/38 passed`.

### The rebuilt release: the download is the signed installer

ODL-332's run (`docs/runbooks/install.md` §12.4) found the published bundle
stale: the release served for `odile_install.py` was `a666f7c8…`, the ODL-203
head, while `installer/odile_install.py.sig` verifies only `8ca566d1…`, and
the bundle carried no `.sig` beside the installer. On 2026-11-09 the bundle was
rebuilt as **`2026.11.09-1`** from a root the current installer made and the
release was cut from it. **No installer was re-signed**: the artifact is the
committed pair, the signature travelled beside it (§3), and the manifest was
rebuilt against the same root.

The root is `/var/tmp/odile-sim-399`, an existing scratch install converged by
the signed installer, then re-run to prove the idempotent path:

```sh
$ sudo -u odiletest399 -H env HOME=/var/tmp/odile-sim-399/home/odiletest399 \
    /usr/bin/python3 /var/tmp/odile-sim-399/bootstrap/odile_install.py \
    --root /var/tmp/odile-sim-399 --user odiletest399 --port 9260 --meter-port 9261 \
    --public-url http://odile-sim.test:9260 --recovery-kit …/odl332-kit.json \
    --signin-password … --backup-password … --i-have-copied-it --no-tmux
odile installer 0.1.0 — tenant plan (scratch) · sha256 8ca566d16ddfa0ca…
same         meter.py … same Hermes install … same Webapp bundle (42 MB)
same         config.yaml … same skins/odile.yaml … same .env
same         odile-home/SKILL.md … odile-first-run … odile-help … AGENTS.md
ok   the gate is 302/401/200 (deploy_demo_site.gate_ok shape): 302/401/200
elapsed      0m 7s
tenant: provisioned                       # run 1: services started
                                          # run 2: the same command —
tenant: same                              #   elapsed 0m 0s, no write line
```

```sh
$ ./scripts/manifest.py build --root /var/tmp/odile-sim-399 --user odiletest399 \
    --installer /var/tmp/odile-sim-399/bootstrap/odile_install.py \
    --generated-at 2026-11-09T21:50:00Z --out /root/.hermes/profiles/builder/cache/scratch/odl332-manifest.json
manifest     … (sha256 c3bb40b1a750…, 47 skills, 9 packages, audit null)
$ ./update/odile_update.py release --root /var/tmp/odile-sim-399 --user odiletest399 \
    --manifest …/odl332-manifest.json --release 2026.11.09-1 --boot-kernel "$(uname -r)" \
    --generated-at 2026-11-09T21:50:00Z --out-dir release \
    --key /root/.odile/installer-signing.pem --notes "the signed installer (8ca566d1) and meter (a4aab3cf) the tenant runs; the bundle carries the installer's signature and the published key"
artifact     meter     meter.py (44642 bytes, sha256 a4aab3cf198e…)
artifact     installer odile_install.py (285071 bytes, sha256 8ca566d16ddf…)
artifact     skills    skills.tar.gz (844098 bytes, sha256 1aa08b45fb4d…)
release      release/release.json (release 2026.11.09-1, manifest sha256 c3bb40b1a750…)
sign         release/manifest.json.sig (key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
sign         release/release.json.sig (key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
$ install -m 644 installer/odile_install.py.sig release/odile_install.py.sig
$ install -m 644 installer/odile-signing-key.pub release/odile-signing-key.pub
```

Those two `install` lines were the manual step §5 used to describe. Since the
cutter writes the pair itself, re-cutting this root at the same
`--generated-at` reproduces the published directory — both copies included —
with no manual step; §5 carries the command and the two `copy` lines.

Published by `./scripts/deploy.sh` from the branch, then measured over TLS:

```sh
$ curl -s https://odile.fyi/odile_install.py | sha256sum
8ca566d16ddfa0ca7d8f08c516cd955d8db02b10a32245702757fb1be4337bbe  -
$ sha256sum installer/odile_install.py
8ca566d16ddfa0ca7d8f08c516cd955d8db02b10a32245702757fb1be4337bbe  installer/odile_install.py

$ for f in release.json release.json.sig manifest.json manifest.json.sig \
    odile_install.py odile_install.py.sig odile-signing-key.pub meter.py skills.tar.gz; do
    curl -sS -o "/tmp/served/$f" "https://odile.fyi/$f"
    cmp "/tmp/served/$f" "release/$f" && echo "identical $f"
  done
identical release.json
identical release.json.sig
identical manifest.json
identical manifest.json.sig
identical odile_install.py
identical odile_install.py.sig
identical odile-signing-key.pub
identical meter.py
identical skills.tar.gz

$ cd /tmp/served && python3 odile_install.py --verify-signature --pubkey odile-signing-key.pub
signing key  sha256 13e8c2aee8263f550033448c8193a82b89498f533f52943e09c14d22e5b036e2 · SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8
signature: OK (sha256 8ca566d16ddfa0ca7d8f08c516cd955d8db02b10a32245702757fb1be4337bbe)
                                                      # exit 0 — the ritual's step 1, from the bundle alone

$ ./scripts/manifest.py verify --manifest /tmp/served/release.json \
    --signature /tmp/served/release.json.sig --pubkey /tmp/served/odile-signing-key.pub
signature: OK (sha256 22e1c6ade897…, key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
$ ./scripts/manifest.py verify --manifest /tmp/served/manifest.json \
    --signature /tmp/served/manifest.json.sig --pubkey /tmp/served/odile-signing-key.pub
signature: OK (sha256 c3bb40b1a750ef63a4d290c0756a2231329e6b150cc99c1d0e1d707f96559a8f, key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
manifest     schema 1 · 47 skills · hermes 2bd378ed1b8d · deepseek-ai/DeepSeek-V4.1-Flash-1017-abliterated
```

The guest's own client agrees: `check` against the live URL verifies both
signatures, finds `boot.kernel 6.12.115+deb13.1-amd64` matching, and plans
only the records write on the root the release was cut from (`unrecorded →
2026.11.09-1`, exit 0). `/release`, `/release/`, `/release/odile_install.py.sig`
and `/app.py` stay 404: the nine names are a map, not a directory. The
waitlist surfaces were unchanged (`/`, `/faq`, `/verify`, `/attestation`,
`/odile-signing-key.pub`, `/health` all 200) and `make check` is green from a
clean clone.

### The re-cut that carries `odile-recipes` (2026-11-11)

`2026.11.09-1` was cut from a root made before ODL-234 embedded
`odile-recipes`, so the published tarball had no such skill. §5 records the
re-cut; this is the served result of **`2026.11.11-1`**, published by
`./scripts/deploy.sh` from the branch and measured over TLS immediately after the
deploy:

```sh
$ curl -sS -D- https://odile.fyi/release.json
HTTP/2 200
cache-control: no-store
content-type: application/json
content-length: 999

$ for f in release.json release.json.sig manifest.json manifest.json.sig \
    odile_install.py odile_install.py.sig odile-signing-key.pub meter.py skills.tar.gz; do
    curl -sS -o "/tmp/served/$f" "https://odile.fyi/$f"
    cmp "/tmp/served/$f" "release/$f" && echo "identical $f"
  done
identical release.json
identical release.json.sig
identical manifest.json
identical manifest.json.sig
identical odile_install.py
identical odile_install.py.sig
identical odile-signing-key.pub
identical meter.py
identical skills.tar.gz

$ curl -s https://odile.fyi/odile_install.py | sha256sum
78fc0a319546e83492d15d4fd3785dd9630ce43e68392958a6fcdceb418145af  -
$ curl -s https://odile.fyi/manifest.json | sha256sum
0b846c021874fc9974d15e040f228907dd9e03aaeed9d4dc7cd3e1630f282e6f  -
$ curl -s https://odile.fyi/release.json | sha256sum
649e7d4494de8416e3779b016c6e3af058ccb4fff1a06aa6b48953c23598e0ac  -
$ curl -s https://odile.fyi/skills.tar.gz | sha256sum
30e7c1d315b7b89855f21a82b5836fb8cdee8b674394e6283bcdb9877a1e0e48  -
$ curl -s https://odile.fyi/skills.tar.gz | tar -tzf - | grep odile-recipes
odile-recipes/SKILL.md

$ cd /tmp/served && python3 odile_install.py --verify-signature --pubkey odile-signing-key.pub
signing key  sha256 13e8c2aee8263f550033448c8193a82b89498f533f52943e09c14d22e5b036e2 · SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8
signature: OK (sha256 78fc0a319546e83492d15d4fd3785dd9630ce43e68392958a6fcdceb418145af)

$ ./scripts/manifest.py verify --manifest /tmp/served/release.json \
    --signature /tmp/served/release.json.sig --pubkey /tmp/served/odile-signing-key.pub
signature: OK (sha256 649e7d4494de…, key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
$ ./scripts/manifest.py verify --manifest /tmp/served/manifest.json \
    --signature /tmp/served/manifest.json.sig --pubkey /tmp/served/odile-signing-key.pub
signature: OK (sha256 0b846c021874fc9974d15e040f228907dd9e03aaeed9d4dc7cd3e1630f282e6f, key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
manifest     schema 1 · 48 skills · hermes 2bd378ed1b8d · deepseek-ai/DeepSeek-V4.1-Flash-1017-abliterated

$ ./update/odile_update.py check --root /var/tmp/odile-093a49b5-cand --user odiletest093c \
    --release-url https://odile.fyi/release.json --pubkey release/odile-signing-key.pub
release      2026.11.11-1 (generated 2026-11-11T07:05:00Z)
notes        the signed installer (78fc0a31) and meter (a4aab3cf) the tenant runs; the bundle carries the installer's signature, the published key and odile-recipes
signature    OK (release.json and manifest.json; key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
boot         kernel 6.12.115+deb13.1-amd64 matches this machine
manifest     schema 1 · 48 skills · hermes 2bd378ed1b8d · meter a4aab3cf198e · installer 78fc0a319546
plan         records   write     manifest.json and release.json
available    unrecorded → 2026.11.11-1
                                                      # exit 0
```

`/release`, `/release/`, `/release/meter.py`, `/release.json/` and `/app.py` are
404; `/`, `/faq`, `/verify`, `/attestation`, `/odile-signing-key.pub` and
`/health` are 200. The deploy also carried `main`'s 2026-11-11 tree into
`/opt/odile` (`web/first-visit.html`, the `/verify` re-cut, the tenant
template), which is what `./scripts/deploy.sh` from a branch off `main` publishes. A
later deploy from `main` replaces `/opt/odile/release` with `main`'s older
release directory, so re-measure this block before trusting it.

### The self-copy installer, published (2026-11-11)

The `2026.11.11-2` cut in §5 was published by `./scripts/deploy.sh` from the branch
that carries it (`main` after the ODL-332 follow-up merge carries the same
`release/`), and measured over TLS immediately after the deploy:

```sh
$ curl -sS -D- https://odile.fyi/release.json
HTTP/2 200 
alt-svc: h3=":443"; ma=2592000
cache-control: no-store
content-security-policy: default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; font-src data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'
content-type: application/json
date: Wed, 11 Nov 2026 10:52:13 GMT
permissions-policy: camera=(), microphone=(), geolocation=(), interest-cohort=()
referrer-policy: no-referrer
server: odile
strict-transport-security: max-age=31536000; includeSubDomains
x-content-type-options: nosniff
x-frame-options: DENY
content-length: 1109
```

```sh
$ for f in release.json release.json.sig manifest.json manifest.json.sig \
    odile_install.py odile_install.py.sig odile-signing-key.pub meter.py skills.tar.gz; do
    curl -sS -o "/var/tmp/odile-332-publish/served/$f" "https://odile.fyi/$f"
    cmp "/var/tmp/odile-332-publish/served/$f" "release/$f" && echo "identical $f"
  done
identical release.json
identical release.json.sig
identical manifest.json
identical manifest.json.sig
identical odile_install.py
identical odile_install.py.sig
identical odile-signing-key.pub
identical meter.py
identical skills.tar.gz
```

```sh
$ curl -s https://odile.fyi/odile_install.py | sha256sum
04b3993b211dfe0d2a7c40fd50206f5032e8afd148022604abf934237b891277  -
$ curl -s https://odile.fyi/manifest.json | sha256sum
b2f4fb884cc79e2391f466690f1095de5a8db9b8ce1a7ab84107cbf7a705465f  -
$ curl -s https://odile.fyi/release.json | sha256sum
b60d36e7ff030ae896f1ef31d210597ef0d8276e6a421f5f43588970d50c316d  -
$ curl -s https://odile.fyi/skills.tar.gz | sha256sum
30e7c1d315b7b89855f21a82b5836fb8cdee8b674394e6283bcdb9877a1e0e48  -
$ curl -s https://odile.fyi/skills.tar.gz | tar -tzf - | grep odile-recipes
odile-recipes/SKILL.md

$ cd /var/tmp/odile-332-publish/served && python3 odile_install.py --verify-signature --pubkey odile-signing-key.pub
signing key  sha256 13e8c2aee8263f550033448c8193a82b89498f533f52943e09c14d22e5b036e2 · SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8
signature: OK (sha256 04b3993b211dfe0d2a7c40fd50206f5032e8afd148022604abf934237b891277)

$ ./scripts/manifest.py verify --manifest /var/tmp/odile-332-publish/served/release.json \
    --signature /var/tmp/odile-332-publish/served/release.json.sig \
    --pubkey /var/tmp/odile-332-publish/served/odile-signing-key.pub
signature: OK (sha256 b60d36e7ff030ae896f1ef31d210597ef0d8276e6a421f5f43588970d50c316d, key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
$ ./scripts/manifest.py verify --manifest /var/tmp/odile-332-publish/served/manifest.json \
    --signature /var/tmp/odile-332-publish/served/manifest.json.sig \
    --pubkey /var/tmp/odile-332-publish/served/odile-signing-key.pub
signature: OK (sha256 b2f4fb884cc79e2391f466690f1095de5a8db9b8ce1a7ab84107cbf7a705465f, key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
manifest     schema 1 · 48 skills · hermes 2bd378ed1b8d · deepseek-ai/DeepSeek-V4.1-Flash-1017-abliterated

$ ./update/odile_update.py check --root /var/tmp/odile-332-cand --user odiletest332 \
    --release-url https://odile.fyi/release.json --pubkey release/odile-signing-key.pub
release      2026.11.11-2 (generated 2026-11-11T09:55:00Z)
notes        the self-copy installer (04b3993b) and meter (a4aab3cf) the tenant runs; /usr/local/bin/odile-install.sh is written by the install itself, so CLAIMS 4.6 hashes a file that exists; the bundle carries the installer's signature, the published key and odile-recipes
signature    OK (release.json and manifest.json; key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
boot         kernel 6.12.115+deb13.1-amd64 matches this machine
manifest     schema 1 · 48 skills · hermes 2bd378ed1b8d · meter a4aab3cf198e · installer 04b3993b211d
plan         records   write     manifest.json and release.json
available    unrecorded → 2026.11.11-2
                                                      # exit 0
```

The guest client accepts the live release and plans only the records write on
the root the release was cut from. `/release`, `/release/`,
`/release/meter.py`, `/release.json/` and `/app.py` are 404; `/`, `/faq`,
`/verify`, `/attestation`, `/odile-signing-key.pub` and `/health` are 200. The
installer the tenant copies is the published one: on that root
`sha256sum /var/tmp/odile-332-cand/usr/local/bin/odile-install.sh` is
`04b3993b…`, the manifest's `installer_sha256` (`install.md` §12.4). Because
`main` already carries `2026.11.11-2`, a later `./scripts/deploy.sh` from `main`
re-publishes the same bytes rather than reverting them; a new installer edit
still makes this block stale and needs the §5 cut again.

### The manifest placement, published (2026-11-11)

The `2026.11.11-4` cut in §5 was published by `./scripts/deploy.sh` from the branch
that carries it, and measured over TLS immediately after the deploy:

```sh
$ curl -s https://odile.fyi/release.json | python3 -c 'import json,sys; d=json.load(sys.stdin); print("release", d["release"]); print("installer", d["artifacts"]["installer"])'
release 2026.11.11-4
installer {'sha256': 'ad04d570217f1c7e59870b6d533403fab1ee24335e8eca04c27f32952742b05b', 'size': 552849, 'url': 'odile_install.py'}

$ for f in release.json release.json.sig manifest.json manifest.json.sig \
    odile_install.py odile_install.py.sig odile-signing-key.pub meter.py skills.tar.gz; do
    curl -fsS -o "/var/tmp/odile-472-served/$f" "https://odile.fyi/$f"
    cmp "/var/tmp/odile-472-served/$f" "release/$f" && echo "identical $f"
  done
identical release.json
identical release.json.sig
identical manifest.json
identical manifest.json.sig
identical odile_install.py
identical odile_install.py.sig
identical odile-signing-key.pub
identical meter.py
identical skills.tar.gz

$ curl -s https://odile.fyi/odile_install.py | sha256sum
ad04d570217f1c7e59870b6d533403fab1ee24335e8eca04c27f32952742b05b  -
$ curl -s https://odile.fyi/manifest.json | sha256sum
568a0a40d79350ffc2eec898509b8baafd397c41107df1b486396479e2ef63cd  -
$ ./scripts/manifest.py verify --manifest /var/tmp/odile-472-served/release.json \
    --signature /var/tmp/odile-472-served/release.json.sig \
    --pubkey /var/tmp/odile-472-served/odile-signing-key.pub
signature: OK (sha256 f791ecc77a6c4194358c2b99e45ac2eb2f323d00be3ac6c604658005b7caf3c1, key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
$ ./scripts/manifest.py verify --manifest /var/tmp/odile-472-served/manifest.json \
    --signature /var/tmp/odile-472-served/manifest.json.sig \
    --pubkey /var/tmp/odile-472-served/odile-signing-key.pub
signature: OK (sha256 568a0a40d79350ffc2eec898509b8baafd397c41107df1b486396479e2ef63cd, key SHA256:ZyFEbkw2oetDml63XaHTix0kUrR2kAVGZaaeDAHvXu8)
manifest     schema 1 · 48 skills · hermes 2bd378ed1b8d · deepseek-ai/DeepSeek-V4.1-Flash-1017-abliterated
$ cd /var/tmp/odile-472-served && python3 odile_install.py --verify-signature --pubkey odile-signing-key.pub
signature: OK (sha256 ad04d570217f1c7e59870b6d533403fab1ee24335e8eca04c27f32952742b05b)
```

The tenant half — `/usr/local/bin/odile-install.sh` byte-identical to the
bundle, the signed pair at `/etc/odile/manifest.json`, and `/manifest`
answering those bytes while a tampered pair answers nothing — is proved on the
bench image (`deploy/image/FIRSTBOOT.md` §5: `installed`, `same`, and
`fail-closed: manifest signature does not verify` with the port closed) and on
a real tenant by ODL-332. `main` carries `2026.11.11-4` after this branch
merges, so a later `./scripts/deploy.sh` from `main` re-publishes the same bytes; a new
installer edit still makes this block stale and needs the §5 cut again.

The deploy above was re-run twice: a concurrent branch's `./scripts/deploy.sh` from
`main` (which still carried `2026.11.11-2`) reverted `/opt/odile/release` to
the older directory minutes after the first publish, and again after the
roadmap merge. The numbers here are the fresh download after the final deploy
from this branch rebased on the roadmap head (`db104cf`, 2026-11-11T13:06:30Z),
which carries both `/roadmap` and this release. Until this branch merges, a
deploy from a `main` that predates it reverts the bundle again; measure after
the merge before trusting the URL.
