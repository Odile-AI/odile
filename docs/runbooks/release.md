# The release directory — publishing an image and keeping three

**What this covers.** `/srv/odile-releases` is the one directory Caddy serves at
`https://odile.fyi/releases/` (`deploy/caddy/releases.caddy`), and the Servury
panel downloads the tenant's boot image and disk image from it. This page is the
retention rule for that directory and the two commands that apply it: one that
publishes a build, one that prunes. The rule is **the current release and its
two predecessors** — three disk images and three UKIs — and it is measured, not
assumed: the 15:22:14Z capture, the last look before the first prune, found
**15 disk images** — 12 pruned + 3 kept — at 85.5 GB on disk in `disks/`, with
`df -P /` 81 % used and 37.4 GB free. (The card body's "ten" divided 85.5 GB by
the 8 GiB apparent size; the directory held fifteen half-sparse files, each
`ls -l` 8 GiB and 4.5–8 GB on disk — `20-first-prune-df.txt`,
`22-first-prune-ls.txt`.) The first prune ran at 15:27–15:28Z and left the
three pairs §2 names. Measured again at 16:13–16:25Z: `disks/` holds
**3 images, 20.6 GiB on disk**, `uki/` **3 (345 MiB)**, and `df -P /` reads
**49 % used, 95.9 GiB free**; re-checked at 20:12Z, still three images and
20.6 GiB. A kept image is 8 GiB apparent and ~6.9 GiB on disk, so a build takes
~7 GiB: eleven more builds from 95.9 GiB free leave ~20 GiB free, and the
twelfth — which the floor still allows — ends at ~13.6 GiB free, 93 % used,
inside the sweeper's ≥ 90 % band (18.73 GiB free on this 187.3 GiB volume). The
16 GB publish floor is one build plus headroom, **not** a fence outside that
band — §3 states exactly what it guarantees.

**Card:** ODL-356. **Code:** `deploy/caddy/deploy_releases.py`
(`--publish-disk`, `--retention`, `--pins-out`, `--self-test`). **Evidence:**
`docs/ops/evidence/2026-11-25-release-disks/`.

## 1. What the directory is

| Path | Served at | Content |
|---|---|---|
| `/srv/odile-releases/disks/` | `/releases/disks/<digest[:16]>.raw` | tenant disk images, content-addressed |
| `/srv/odile-releases/uki/` | `/releases/uki/<digest[:16]>.efi` | signed UKIs, content-addressed |
| `/srv/odile-releases/m1/` | `/releases/m1/odile-tenant-sealed.raw`, `odile-tenant.efi` | the M1 release under its **fixed** name (ODL-332) |
| `/srv/odile-releases/webapp/` | `/releases/webapp/` | the Webapp bundle |

`disks/` and `uki/` are content-addressed: the file name is the first 16 hex of
its own SHA-256 and a `.sha256` sidecar names the file and the full digest
(`<digest>  <name>`). That is what makes a rebuild safe to publish — the old URL
keeps answering the old bytes, and the panel's pinned hash still matches. It is
also why nothing ever overwrote anything, and why nothing was ever removed.

`m1/` and `webapp/` are **outside** the retention rule: their names are fixed,
their runbooks (`docs/evidence/m1-image.md`, the Webapp build) replace them in
place, and a fixed name is one image, not a museum.

## 2. The rule

**Keep the disk image and the UKI that each live machine's panel fields name.
Delete every other image in `disks/` and `uki/`. Nothing else survives.**

The panel's Boot image and Disk image fields *are* the live release manifest:
each holds an https URL and the SHA-256 the node checks at every start, so a
file those fields name must keep answering or that machine cannot be
reinstalled. Read from the panel on 2026-11-25T16:14Z
(`docs/ops/evidence/2026-11-25-release-disks/04-panel-pins.tsv`, written by
`--pins-out`):

| Machine (`server`) | Disk image | UKI | Role |
|---|---|---|---|
| the cold-walk tenant (`ce31aee1`) | `4b01dcf21fc1ae06.raw` | `63a3bf152772e578.efi` | **current release** (newest pair) |
| the bench (`<server:bench>`) | `1f0ec18099aaa464.raw` | `7571cb22a591a5d4.efi` | predecessor 1 |
| the demo tenant (`<server:demo>`) | `5ab78a3a61393cd3.raw` | `47be81ca41c24a9f.efi` | predecessor 2 |

In this table and in §4.1's transcript, `<server:bench>` and `<server:demo>`
stand for the panel's instance ids of the bench machine and the demo tenant —
the same first column the `--pins`/`--pins-out` file carries in §4.3 — while the
cold-walk tenant's id is named in full because it is the machine this release
serves.

Every machine on the account that sets neither image field names nothing and
keeps nothing.

- **Current release** = the newest of the *kept* pairs by mtime — normally the
  one the most recent publish wrote, once its URL and hash are pasted into the
  panel. A freshly published, not-yet-pinned image is not kept: the next
  `--retention` deletes it (§3 step 7). **Predecessor** = an older pair a live
  machine still names. Three is the depth because three is what the live
  machines name today: the cold-walk tenant, the bench and the demo tenant. A
  fourth live pair raises the kept count, and the run prints it; the prune never
  leaves a machine pointing at a file it deleted.
- **The prune refuses rather than guesses.** Before deleting anything it reads
  every named image, checks that the file exists and that its SHA-256 equals the
  panel's, and stops with exit 2 and `refused` if any of that is false. A pin
  whose name and hash disagree is a broken pin, not a licence to delete. An
  empty keep set is a refusal too: `--pins` with every image field `-` does not
  empty the directory.
- **A machine on another Servury account** is invisible to this read. Capture
  its pair with `--pins-out` from that account, or write the TSV by hand
  (§4.3) and run the prune with `--pins`.
- **A machine pinned to `m1/`** (the fixed-name release) is out of the rule:
  its value is printed under `outside` and `m1/` is left alone. It names
  nothing in `disks/` or `uki/`, so if it is the *only* pin the keep set is
  empty and the run refuses (`the keep set is empty — nothing deleted`) rather
  than printing `outside` and then deleting every image.

## 3. Publish — one image, one refusal

```sh
# the built image, from wherever the build wrote it
./deploy/caddy/deploy_releases.py --publish-disk /var/tmp/odile-xxx/out/odile-tenant-sealed.raw
```

It measures free space on the volume **before the first write** — including the
`mkdir` — and refuses, printing the measured number, when free space is below
the floor:

```console
$ ./deploy/caddy/deploy_releases.py --publish-disk out/odile-tenant-sealed.raw --min-free-gb 100
free      95.8 GB on /srv/odile-releases (publish floor 100 GB)
refused   95.8 GB free < 100 GB floor — nothing written
$ echo $?
2
```

(measured 2026-11-25T16:19Z; the disk-image count was 3 before and 3 after —
`docs/ops/evidence/2026-11-25-release-disks/07-publish-refusal.txt`).

The floor is **16 GB** by default (`FREE_FLOOR_GB`). What it guarantees is
narrow, and worth stating exactly, because the sweeper reads `df`'s percentage
(`deploy/disk-care/hermes-disk-care.sh`: `STALE_HOURS=3` from 90 % used; no
quarantine from 95 % — `docs/ops/DISK-CARE.md`). On the volume measured here
(187.3 GiB effective: 95,741,212 KiB used + 100,641,096 KiB available),
90 % used is **18.73 GiB free** and 95 % is **9.36 GiB free**. So:

- 16 GB free is **92 % used** — already inside the ≥ 90 % band, and only
  6.6 GiB above the ≥ 95 % line, so a build that writes more than 6.6 GiB on
  disk ends inside the no-quarantine band.
- A kept image is 8 GiB apparent and ~6.9 GiB on disk. A publish that starts
  at the 16 GB floor ends at **~9.1 GiB free (96 %)** — inside the ≥ 95 % band
  where the sweeper stops quarantining and deletes directly. The floor keeps
  one copy from failing; it does **not** keep the volume out of either band.
- **Prune between builds.** Eleven builds from 95.9 GiB free leave ~20 GiB
  free, still above the floor; the twelfth is allowed and ends at ~13.6 GiB
  free, 93 % used.

A floor that keeps the volume outside the ≥ 90 % band would be one build plus
18.7 GiB ≈ **26 GiB**. 16 GB is the card's stated number, so it stands here; a
raised floor is an owner decision, not a silent edit. `--min-free-gb` exists to
exercise the refusal without filling the volume; do not lower it in a real
publish.

After the guard the verb:

1. hashes the source (SHA-256, streamed — an 8 GiB file is never held in
   memory);
2. `cp --sparse=always` into `<digest[:16]>.raw`, keeping the image half-sparse
   instead of doubling it to 8 GiB;
3. re-hashes the copy and refuses if it differs from the build;
4. writes the `.sha256` sidecar (mode 0644);
5. reads `https://odile.fyi/releases/disks/<name>` back with `HEAD` and checks
   the served size (`--no-verify` skips this when the host is offline);
6. prints the panel pair and the free space after:

```console
URL      https://odile.fyi/releases/disks/<digest[:16]>.raw
SHA-256  <the 64-hex digest>
```

7. Those two lines go straight into **Manage → Disk image**, and the machine
   is recycled (`docs/runbooks/recycle.md`) — **before any prune**. The
   retention rule keeps what the panel names, so an image published and not yet
   pasted into the panel is exactly what the next `--retention` deletes. This is
   the verb's own `next:` line, made a step; §6 repeats it in order. The UKI is
   the same step one field over: `tools/publish_uki.py` prints its URL and
   SHA-256 for **Manage → Boot image**, and an unpinned new `.efi` is pruned
   exactly as an unpinned `.raw` is.

Publishing the same bytes twice is `same` and rewrites only the sidecar, so the
command is safe to rerun. The UKI is published by `tools/publish_uki.py`, which
names it the same way in `uki/`; the retention rule covers both directories.

## 4. Prune — dry run first

### 4.1 The plan

```sh
./deploy/caddy/deploy_releases.py --retention
```

It reads the panel (or `--pins FILE`, §4.3), verifies every named file against
the panel's SHA-256, and prints the kept pairs and every file it would delete.
Nothing is deleted without `--apply`. Measured on 2026-11-25T16:20Z, after a
1 GiB drill image had been published into the live directory (file 08) so the
run had something to prune:

```console
$ ./deploy/caddy/deploy_releases.py --retention
keep      3 image(s) in disks/ of 4
keep      3 image(s) in uki/ of 3
pair      current         4b01dcf21fc1ae06.raw + 63a3bf152772e578.efi (live on ce31aee1)
pair      predecessor 1   1f0ec18099aaa464.raw + 7571cb22a591a5d4.efi (live on <server:bench>)
pair      predecessor 2   5ab78a3a61393cd3.raw + 47be81ca41c24a9f.efi (live on <server:demo>)
prune     1 disk image(s) and 1 sidecar(s), 1.0 GB
prune     0 UKI image(s) and 0 sidecar(s), 0.0 GB
would     rm /srv/odile-releases/disks/ab645071ad9513ed.raw (1.0 GB)
would     rm /srv/odile-releases/disks/ab645071ad9513ed.raw.sha256 (0.0 GB)
dry-run   1.0 GB would be freed; nothing deleted (run again with --apply)
```

### 4.2 Apply, and the proof

```sh
./deploy/caddy/deploy_releases.py --retention --apply
```

The real run deletes exactly the listed files, then re-hashes every kept file
and prints it — the before/after equality is the run's own check, not a claim:

```console
rm        /srv/odile-releases/disks/ab645071ad9513ed.raw (1.0 GB)
rm        /srv/odile-releases/disks/ab645071ad9513ed.raw.sha256 (0.0 GB)
kept      1f0ec18099aaa464.raw sha256 1f0ec18099aaa4645ade7949d8ea9699f5795c74a572bca5a45c00dc4351b87f (byte-identical to the panel)
kept      4b01dcf21fc1ae06.raw sha256 4b01dcf21fc1ae0698c3cfe99b28fc12fbad2f454a5b36ef6e901add2fd86e35 (byte-identical to the panel)
kept      5ab78a3a61393cd3.raw sha256 5ab78a3a61393cd30e1415c7873587baafb1fdae5a79df5f594e1a9811671a92 (byte-identical to the panel)
kept      47be81ca41c24a9f.efi sha256 47be81ca41c24a9f49a6f8e462fd0add6383f956d37dc1add2c114478d235717 (byte-identical to the panel)
kept      63a3bf152772e578.efi sha256 63a3bf152772e57848403b3b0aeed609c1e86521f792cf03f38858a30bfada85 (byte-identical to the panel)
kept      7571cb22a591a5d4.efi sha256 7571cb22a591a5d4089c2904cf38befd650257e094ad7c64b9ec16a2f7a1c213 (byte-identical to the panel)
freed     1.0 GB; images 21.9 GB -> 20.9 GB
free      92.7 GB -> 96.0 GB on /srv/odile-releases
```

`images` counts `disks/` and `uki/` by bytes on disk and divides by 1024³
while labelling the result GB. The current image's SHA-256 is the same 64 hex
before the drill (`06-current-image-before.txt`), in the kept line above and
after it (`14-current-image-after.txt`), and its `.sha256` sidecar carries that
same digest (`18-current-image-and-sidecar.txt`). The verb's own `free` line
moved by more than the 1 GiB it deleted because other seats were writing to the
same volume during the run; the volume's numbers in the evidence are the two
`df -P /` reads (files 01 and 11), and the prune's own measurement is `du` on
the two directories it owns.

The first live prune ran at **15:27–15:28Z**. The 15:22:14Z capture — the last
look before it — found **15 disk images** (12 pruned + 3 kept) at 85.5 GB on
disk in `disks/`, `df -P /` 81 % used, 37.4 GB free (`20-first-prune-df.txt`,
`21-first-prune-du.txt`, `22-first-prune-ls.txt`). The dry run at 15:25 planned
`12 disk image(s) and 3 sidecar(s), 60.9 GB` and `13 UKI image(s) … 1.5 GB`,
and ended `dry-run 62.4 GB would be freed`
(`24-first-prune-retention-dry-run.txt`); the apply at 15:27 printed
`freed 62.4 GB; images 83.3 GB -> 20.9 GB` and a
`kept … byte-identical to the panel` line for all six files
(`25-first-prune-retention-apply.txt`). The directory now holds three,
`disks/` reads 20.6 GiB and `df -P /` 49 % used — re-checked at 20:12Z with
the same three images and the same 20.6 GiB; the volume's own percentage moved
to 60 % in between because other seats write to it. A second run is
`same nothing to prune` (file 15).

The exact transcripts, `df -P /` and `du -x -d2 /srv` before and after are in
`docs/ops/evidence/2026-11-25-release-disks/`.

### 4.3 When the panel cannot be read

`--pins FILE` reads the same facts from a TSV — one machine per line, `-` for a
field the machine does not set:

```
# server	disk	disk_sha256	uki	uki_sha256
<server:bench>	https://odile.fyi/releases/disks/1f0ec18099aaa464.raw	1f0ec18099aaa4645ade7949d8ea9699f5795c74a572bca5a45c00dc4351b87f	https://odile.fyi/releases/uki/7571cb22a591a5d4.efi	7571cb22a591a5d4089c2904cf38befd650257e094ad7c64b9ec16a2f7a1c213
```

A line starting with `#` is a comment; every other line needs exactly five
whitespace-separated columns, and a file with no machine is an error. That
header line is what `--pins-out` writes, so do not copy the header without
its `#` — the parser would read it as a machine. The first column is the panel's
instance id (`<server:bench>` in this example; §2 names the current machine's
id in full).

`--pins-out FILE` writes that file from the panel, so the snapshot a prune ran
against is the one in the evidence directory. The panel session is the pasted
browser cookie in `/root/.odile/servury_panel.cookie` (mode 0600; the panel is
captcha-gated, so a re-login is a human step — `tools/servury_panel.py
--login`). An expired session serves the login page, which names no image; the
verb then exits 2 with `pass --pins` instead of pruning against an empty keep
set.

## 5. What is never pruned

- The three kept pairs, and any file a live machine's panel fields name, even
  when it is older than the depth.
- Anything in `m1/` and `webapp/` — fixed-name releases with their own
  runbooks.
- Files in `disks/` or `uki/` that are neither an image nor its sidecar: left
  alone, and the run does not name them.
- `outside` is only for a panel field that names a file in another directory
  (`m1/`, a second account's host): it is printed and left untouched.

## 6. Runbook checklist

- [ ] `df -P /` first. Below 16 GB free, publish nothing; prune instead. (The
      floor keeps one copy from failing — it does not keep the volume out of
      the sweeper's bands; §3.)
- [ ] `--publish-disk` (one image) and `tools/publish_uki.py` (one UKI).
- [ ] Paste the printed URL + SHA-256 into **Manage → Disk image** — and the
      UKI's into **Manage → Boot image** (`tools/publish_uki.py` prints it
      there) — and recycle (`docs/runbooks/recycle.md`), **before the prune**.
      Until the panel names the new pair, `--retention` deletes it.
- [ ] `--retention` dry run; read the `pair` lines against the machine the
      release is for — `current` must be the pair just pasted.
- [ ] `--retention --apply`; the `kept … byte-identical` lines are the proof.
- [ ] `df -P /` and `du -x -d2 /srv` again; paste into the evidence directory.
- [ ] A second `--retention` prints `nothing to prune`.

## 7. After a factory restore — republish the release images

`/srv/odile-releases` is **not** in the HQ backup, by decision
(`docs/architecture/ADR-factory-backup.md` §3.1, 2026-11-26): measured
34,843,229,977 bytes in 15 files — 32.4 GiB apparent, 30 GiB on disk, four
fifths of it three 8 GiB disk images — against a backup set of 7.5 GiB. The
two artefacts the panel pins are build output, and the restored checkpoint
rebuilds them. So a restored HQ serves **404** on every `/releases/*` URL
until one republish; `deploy/recovery/recover.sh` prints these commands during
the recovery (before the DNS update and `RECOVERY COMPLETE`, not after it), and
this is the page they name.

On the recovered box (the site answers while these are missing — only the
panel's download links are 404):

```sh
cd ~/odile
sudo ./deploy/image/sealed-image.sh --out deploy/image/out-sealed
./deploy/image/uki.sh --in deploy/image/out-sealed --sign --pin deploy/image/tenant-image.pin
./deploy/caddy/deploy_releases.py --publish-disk deploy/image/out-sealed/odile-tenant-sealed.raw
./tools/publish_uki.py --uki deploy/image/out-sealed/odile-tenant.efi
```

Each publish prints the `URL` / `SHA-256` pair; paste both into the panel
(§3 step 7) before any `--retention` run — an unpinned new pair is exactly
what the next prune deletes (§4). The build needs the image bench's packages
and ~20 GB free (`docs/runbooks/image.md`); on the 40 GB recovery box, build,
publish and prune before the next build (`docs/ops/DISK-CARE.md`).

Three facts a recovered box should not be surprised by:

- **The digests change.** A rebuild is not the old build: the throwaway boot
  key and the LUKS passphrase are new, so the disk and UKI hash differently.
  The old URLs keep their old names but no longer answer; every machine's panel
  fields must be given the new pair before it can be reinstalled. A machine
  that is merely *running* is unaffected — it re-hashes the bytes on its own
  disk at boot, not the URL.
- **`m1/` does not come back.** It held the fixed-name ODL-332 pair; the panel
  now names content-addressed pairs in `disks/`/`uki/` (§2), and nothing
  rewrites `m1/`.
- **`webapp/` is the one file with no build command here.** The 1.5 MB
  `web_dist-<id>.tgz` bundle has no tool in this repository; copy the tarball
  the Webapp build produces:
  `install -d -m 755 /srv/odile-releases/webapp && cp <bundle>.tgz /srv/odile-releases/webapp/`.
