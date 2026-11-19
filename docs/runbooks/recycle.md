# Recycle a freed machine — the three writes

**What this covers.** A machine that comes back from a previous owner (or a
bench box that has to be rebuilt to a published pair). The measured cost is
**three writes, not one call**: a reinstall, a boot image, then a disk image —
and the first boot is the machine's own step. The reinstall has an API verb and
no image parameters; the disk image has **no** API verb at all.

Measured on `<server:ukitest>` (`168020b4`, Blackbox Core, account
`lkbxu6i9`) on 2026-11-19 for this card, on 2026-11-17 for the spike. The
spike is `docs/product/SPIKE-servury-reinstall.md`; this runbook is the
procedure that came out of it and the one open shape it left (§3).

**Never here:** HQ (`<server:hq>`) and any tenant. The destructive verb reads
`/root/.odile/servury_uki_server_id` strictly — it must name **exactly one**
id, and the tool refuses a list of two rather than guess. That was measured on
this host: the file named the bench and the live tenant, and the reinstall
refused until the file named one id (`--reinstall --server 168020b4` → "the
allow-list … names 2 ids … fenced to exactly one").

## 1. The fence

`tools/servury_panel.py --reinstall` (and `--recycle`, which uses it) is
fenced harder than the image writes, because it is the one write that replaces
a tenant's disk and its measured boot image without the user:

1. **One id, exact match.** The allow-list must name exactly one id and
   `--server` must equal it. No prefix, no case folding, and `--allow-server`
   is refused for this verb.
2. **`ERASE` is a constant.** The tool types it; it is not an argument. The
   tool also refuses to POST unless the page still carries the button labelled
   **"Erase and reinstall"**, so a vendor rename fails loudly instead of
   erasing the wrong form.
3. **The field, not the path, is the fence.** The form posts to
   `/manage/<id>/danger/`, whose path names no forbidden verb; the destroy form
   sits in the same block with `action=delete` and the literal `DELETE`.
   `post()` therefore refuses `action=reinstall` everywhere except the verb that
   owns it, and never permits `action=delete`.
4. **No defaults on the build-time fields.** `--os`, `--want-snp`,
   `--want-fde` and `--want-measured` are required. The vendor's rule is "what
   you do not send is off", so an omitted `--want-measured` would silently
   build an unmeasured machine.
5. **The key comes from a file.** `--ssh-key-file`, never argv (argv is
   world-readable). Whatever is in the box becomes the whole `authorized_keys`
   of the new install, so a machine recycled for tenant B is given tenant B's
   key here.
6. **The line comes first.** An audit line is written before the POST with the
   pre-write `os`, the three flags, the key's comment and the boot/disk pair
   the write is about to lose. Both image fields live on the machine's own
   storage and the reinstall clears them.
7. **Two flags.** `--apply` alone is a dry run; `--confirm` says the disk's
   contents are gone.
8. **The fence holds before the composite's first write.** `--recycle` runs
   rule 1 at the top, before it arms the console log — arming the console is a
   POST too, and a `--recycle` naming a widened allow-list must post nothing
   at all. (Measured 2026-11-19 with the file naming both the bench and the
   tenant: `--recycle --server 168020b4 --apply --confirm` → exit 2, no POST,
   no audit line — and the same for the tenant's own id, which a mistyped
   `--server` would have aimed at.)

## 2. The sequence

### 2.0 Build and publish the one UKI artefact

The target shape is **one UKI per release with an empty `.cmdline`**; the
per-machine line travels in the panel's command-line field (ADR-001 §4). The
published `f7bc0ceb…`/`90e97a0f…` UKIs carry a **baked** line, which is a
different — and older — shape.

```sh
# from the shipped bytes: same kernel, same initramfs, same .uname/.osrel,
# only the .cmdline is absent
objcopy -O binary --only-section=.linux  <published>.efi linux.bin
objcopy -O binary --only-section=.initrd <published>.efi initrd.bin
objcopy -O binary --only-section=.uname  <published>.efi uname.bin
objcopy -O binary --only-section=.osrel  <published>.efi osrel.bin
/usr/bin/python3 /usr/lib/systemd/ukify build \
    --linux linux.bin --initrd initrd.bin --cmdline '' \
    --uname "$(cat uname.bin)" --os-release @osrel.bin \
    --secureboot-private-key /root/.odile/image-signing.key \
    --secureboot-certificate deploy/image/odile-image-signing.crt \
    --output odile-tenant-empty-cmdline.efi
sbverify --cert deploy/image/odile-image-signing.crt odile-tenant-empty-cmdline.efi
./tools/publish_uki.py --uki odile-tenant-empty-cmdline.efi
```

Two **unsigned** builds from the same inputs are byte-identical (`cmp` them if
you need the reproducibility check); the signature is the only thing that moves
the bytes, because sbsign stamps a signing time into the PKCS#7. The empty
`.cmdline` is absent from `ukify inspect` entirely, which is the check to look
for.

The per-machine line is the boot line the disk image expects — `root=`,
`console=`, `ip=`, `bb.host`, `bb.iid`, `bb.ip4`, `bb.gw4`, `bb.ip6`,
`bb.gw6`, `bb.dns`, `bb.key`, `seal_id`, `odile.token`, `odile.seed_url`,
`odile.public_url` — with `bb.key` the base64 of the key's
`ssh-ed25519 AAAA… comment` form and `odile.token` a fresh per-machine token
(§2.1). Keep it in a file and pass `--cmdline-file`.

### 2.1 Arm the seed token for the first boot

The reinstall wipes the disk, so the first boot re-presents the token from
the line. A baked line re-presents the **same** token, and if the ledger has
no row for it the boot stops `fail-closed: seed token unknown` (the
`404 unknown_token` row of `docs/runbooks/seed.md`; §6a's `UPDATE` is a no-op
on an absent row). The shape that scales is a **fresh token per machine**:

```sh
ODILE_DB=/var/lib/odile-test/waitlist.db /usr/bin/python3 seed/seed.py \
    token issue --tenant <slug> --instance <server-id> --ttl-hours 24
# add `--instance <id>` when the tenant row's server_id is not the machine
```

The plaintext is printed once and goes into the line; only its SHA-256 is
stored. Arm it **before** the disk write. Measured 2026-11-19: the row's
`used_at` was stamped by the first boot at 16:50:47Z, 24 seconds after the
disk write started.

### 2.2 The three writes, as one command

```sh
./tools/servury_panel.py --recycle --server <id> \
    --os debian13 --want-snp --want-fde --want-measured \
    --ssh-key-file /root/.odile/keys/odile-uki-test.pub \
    --boot-url https://odile.fyi/releases/uki/<digest>.efi --boot-sha256 <digest> \
    --cmdline-file line.txt \
    --disk-url https://odile.fyi/releases/disks/<digest>.raw --disk-sha256 <digest> \
    --format raw --apply --confirm --wait 420 --interval 10
```

It arms the console log first (the first boot's transcript cannot be recovered
after the fact), **after** the one-id fence (§1 rule 8), then reinstall → boot
image → disk image, in the vendor's order. The disk write **starts the machine
itself**; the command ends at that submission. Without `--apply --confirm` the
same command is a dry run and prints every field it would post, including the
pair it would lose.

If the wait for `Running` ends the command (the machine is still `reinstalling`
at `--wait`), the erase has already happened and the boot/disk fields are
cleared: re-run the same command. The second run's rule-(e) line legitimately
reads `boot=none disk=none` — the first run's line is the one that carries the
pair. Measured 2026-11-19: the next command came 4 m 08 s (248 s) after the
first attempt's erase, shorter than the 300 s default, so that run **did not
end by its own timeout** — it was interrupted after the erase had been
submitted. Its stdout was not kept, so its `--wait` is not on record; the
re-run carried `--wait 420` and reached `running` in 63 s.

Measured 2026-11-19 on `<server:ukitest>`: reinstall POST → `Running` 63 s;
boot-image attach (115 MB download + hash) 10 s; disk write submitted, the
machine stops, writes and starts. The spike measured 1 m 32 s and 6 s and
77 s for the same three steps on the same class of box; treat the ones digit
as "a minute or so", the order as the contract.

### 2.3 The first boot — the machine's own evidence

Poll the console until the boot prints its own measurement, then read the
machine:

```sh
./tools/servury_panel.py --console --server <id> --action read > console.txt
ssh -i /root/.odile/keys/odile-uki-test root@<address> \
    'cat /run/blackbox/attest.json; cat /etc/odile/boot-record.json; cat /proc/cmdline'
```

Three things must be true, and they are the evidence — not the panel's
"submitted":

* `/proc/cmdline` is **byte-identical** to the line you passed
  (`sha256(cmdline.strip())` equals the boot record's `cmdline_sha256`);
* the boot record's `measurement` equals `sev-snp-measure` over the **served**
  UKI bytes with the same line (§3);
* the seal was re-provisioned to the new chip: the console carries
  `bb-sealed-unlock: … the first-boot bootstrap key (the blob is not sealed to
  this chip yet)` and `bb-sealed-provision: sealed … ; shredded the
  first-boot bootstrap key`.

Note that root SSH on this image works only after
`odile-release-setup.service` has written the verify surface (the measured boot
key line in `/root/.ssh/authorized_keys`); a `Permission denied (publickey)`
a minute earlier is that step not having run yet, not a bad key.

## 3. The empty-`.cmdline` shape, measured

The spike left this shape unmeasured (§10). Measured 2026-11-19 on
`<server:ukitest>` with a UKI built as above from the published
`90e97a0f…` bytes (`06488116b8602db1.efi`, served), and the panel line:

```
$ sev-snp-measure --mode snp --ovmf OVMF.amdsev.fd \
    --kernel served-uki.efi --append "<the panel line>" \
    --vcpus 2 --vcpu-type EPYC-v4 --guest-features 0x1
4bd9312f1e377a52f567357c077e8926c380bbc9b73a3859585ceaf80c99df9150a05afb292821b78c259a83a898d787

machine /etc/odile/boot-record.json:
  "measurement": "4bd9312f…73d0a6c9…", "expected": "4bd9312f…",
  "cmdline_sha256": "068e5a1a…", "source": "/run/blackbox/attest.json", "vcpus": 2
```

What the numbers mean — and what they do **not** mean:

* The SEV-SNP launch measurement covers the **kernel blob as QEMU receives
  it** (here, the whole UKI PE), the **initrd field** (empty for a
  `-kernel <UKI>` launch; the UKI carries its own) and the **QEMU append** —
  not the UKI's internal `.cmdline`. A baked-line UKI measures with an empty
  append; the empty-`.cmdline` UKI measures with the panel line as the append.
  Both are verifiable; they are different numbers for the same kernel.
* `--append` is load-bearing on the measurement command. Dropping it yields a
  different value that is not what the machine booted.
* Do **not** run `sev-snp-measure` against the extracted `vmlinuz`/`initrd`
  for a `-kernel <UKI>` launch: that hashes a different kernel blob and gives a
  third number.
* `odile-release-setup` compares the image's pin with the boot's own
  measurement and, when they differ, prints `installing with the boot's own
  measurement` and continues with the boot's value. A recycled machine on
  another line therefore installs, but the pin in the image is not the record
  of that boot — the boot record is.
* A per-machine line that changes `bb.key` also changes the **measured boot
  key**, and the disk's baked `/etc/odile/boot-key.pub` still names the old
  one: `odile-firstboot` prints `record skipped (the measured boot key and
  /etc/odile/boot-key.pub disagree)` and skips the boot-record **POST** to HQ.
  The local `/etc/odile/boot-record.json` is still written and `/attestation`
  still reports the same measurement. Re-cutting the disk with the new
  `boot-key.pub` is the fix; the skip is not a failed boot.

## 4. Trim the fence, then restore it

The destructive verb needs the allow-list to name one id; the non-destructive
writes on this account legitimately reach two (the bench and the live tenant's
box — the second id is the tenant's, which is why this file is **copied** back
and never retyped: retyping it is how a live id reaches a published tree).

Before the run, save the resting file and trim it to the machine that may be
erased:

```sh
cp -p /root/.odile/servury_uki_server_id allow-list.before
printf '%s\n' 168020b4 > /root/.odile/servury_uki_server_id
cat /root/.odile/servury_uki_server_id
```

After the run, restore it byte-for-byte from that copy and prove it — the
digest must equal the one you took before the trim:

```sh
sha256sum /root/.odile/servury_uki_server_id   # take this before the trim
cp -p allow-list.before /root/.odile/servury_uki_server_id
sha256sum /root/.odile/servury_uki_server_id   # same value, two lines back
stat -c '%a %U:%G' /root/.odile/servury_uki_server_id   # 600 root:root
```

## 5. What is not automated

* Reading the first boot's evidence (§2.3) — one console read and one SSH
  command; the machine has no ttyS0 autologin since ODL-914.
* The ACME step: on 2026-11-19 the recycled box's
  `odile-release-setup` sat at `acme attempt 10: not issued yet
  ([SSL: TLSV1_ALERT_INTERNAL_ERROR])` for `odl-340a1e.test.odile.fyi`, and
  `odile-access-report.service` failed to start. Neither is the recycle: the
  machine booted, sealed, seeded and reported its own measurement. Both are
  named here so the next run does not chase them.
