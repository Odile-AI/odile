# The image bench — build the tenant image, boot it under QEMU, run the smoke tests

This box has no `/dev/kvm` (no `vmx`/`svm` flag) and no Servury API call is
made. The bench therefore proves what a machine without AMD SEV can prove: the
tenant stack installs from one definition, the image boots, the auth gate
answers, a shell runs as the unprivileged tenant, a file survives a service
restart, a cron job fires, and a UKI built from that image produces a stable
launch measurement against Servury's published OVMF.

Card: ODL-380. Nothing here is published and nothing here touches the live
demo host or the HQ services.

## 1. Three artefacts, one definition

| Artefact | Built by | What it is |
|---|---|---|
| `localhost/odile-tenant:v0.23.0` | `deploy/image/Containerfile` | Debian 13 + the tenant stack, systemd as PID 1 |
| `deploy/image/out/odile-tenant.raw` (+ `.qcow2`) | `deploy/image/mkdisk.sh` | a bootable BIOS/GPT disk: debootstrap Debian 13, the same `install-stack.sh` under chroot |
| `deploy/image/out/odile-tenant.efi` | `deploy/image/uki.sh` | the image's kernel + initramfs + cmdline + os-release as a UKI, plus its `sev-snp-measure` launch measurement |

The install is one script, `deploy/image/install-stack.sh`, run twice:

- in the container build, as root, against the image root;
- in `mkdisk.sh`, under `chroot` into the debootstrapped root.

A second run of `install-stack.sh` prints `same` for every piece and changes
nothing; `mkdisk.sh` runs it twice on purpose and the second run is the
idempotency evidence.

| File | Role |
|---|---|
| `deploy/image/install-stack.sh` | the only installer: user, Hermes v0.23.0, config, `.env`, cron job, unit |
| `deploy/image/config.yaml` | the seven managed blocks the installer's tenant template carries, including `auxiliary.vision.model` (the vision lane's pin, ODL-341-F1) and `onboarding.run: false` (the tenant first-run pin); `__WEBAPP_HOST__`/`__WEBAPP_PORT__` are substituted |
| `deploy/image/tenant.service` | the tenant's systemd unit (the demo host's unit with two names substituted) |
| `deploy/image/bench-heartbeat.sh` | the body of the one Hermes cron job, `bench-heartbeat`, every minute, no agent |
| `deploy/image/bench.py` | the five checks, standard library only; `local`, `container`, `image` modes |
| `deploy/image/smoke.sh` | the one command; wraps `bench.py` |
| `deploy/image/mkdisk.sh` | debootstrap + chroot + GRUB + extracts kernel/initramfs/cmdline/os-release |
| `deploy/image/uki.sh` | `ukify` build + two `sev-snp-measure` runs against Servury's OVMF |
| `deploy/image/odile-bench-smoke.service` | guest oneshot: runs `smoke.sh local` at boot, writes to the bench serial channel |
| `deploy/image/odile-bench-proxy.{socket,service}` | the bench's stand-in for Caddy: forwards the host-forwarded port to the loopback Webapp |

## 2. What the five checks are

One line per check, `ODILE-BENCH <name> <PASS|FAIL> <detail>`, then exactly one
`image-bench: PASS (5/5)` or `image-bench: FAIL (…)` line.

| Check | Assertion |
|---|---|
| `gate-anonymous` | anonymous `GET /` → **302** with `Location: /login…`; anonymous `GET /api/sessions` → **401**; `GET /login` → **200**; a wrong password → **401** |
| `gate-session` | a correct password → **200** and a session cookie; `GET /` with the cookie → **200** with the Webapp shell (`<title>Hermes</title>`, `id="root"`); `POST /api/auth/ws-ticket` → **200**; `GET /api/ws?ticket=…` → **101**, and the same path without a ticket is **not 101** (measured: 403) |
| `shell` | `runuser -u tenant -- sh -lc '…; hermes --version'` exits 0 and prints `Hermes Agent v0.23.0` |
| `persistence` | the tenant writes `~/bench/restart-marker`; `systemctl restart odile-tenant`; the marker is byte-identical afterwards, the unit is `active`, and its MainPID still belongs to `tenant` |
| `cron` | the `bench-heartbeat` Hermes cron job fires once and appends its UTC line to `~/bench/heartbeat.log` |

The gate is asserted as measured, not as the card body once said: anonymous
HTML is a 302 to `/login`, not a 401. A smoke that expected 401 on `/` would
pass the wrong thing (`tests/test_image_bench.py` pins both shapes).

The guest has no KVM, and its Webapp can stall for tens of seconds while the
gateway warms up. `wait_gate` therefore requires **two consecutive** answers
two seconds apart before the checks start, and each gate request is retried
three times with a 60 s timeout. A gate that never answers still fails; a stall
does not (`test_a_stalled_request_is_retried`, `test_a_gate_that_never_answers_fails`).

## 3. Prerequisites on the builder

Measured on this box: Ubuntu 26.04.1 LTS, kernel 6.12.115, 4 vCPU, 7.3 GiB RAM,
98 GB root, **no** `vmx`/`svm` flag. Packages: `podman` (5.7), `qemu-system-x86`
(10.2), `debootstrap`, `systemd-boot-efi` (for
`/usr/lib/systemd/boot/efi/linuxx64.efi.stub`), `sev-snp-measure` (0.0.13),
`shellcheck`. Add swap if the host is small:

```sh
swapon --show                     # this box had none and 7.3 GiB of RAM
fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
```

The Webapp bundle build (`vite`) reached 2.3 GiB RSS; without swap the kernel
killed it (`SIGKILL`) and the first container build failed. The bench was run
with 4 GiB of swap.

`ukify` on this host is `/usr/lib/systemd/ukify` and must be run with
`/usr/bin/python3`; the bare `ukify` shim picks the wrong interpreter and dies
on import.

## 4. The container

```sh
podman build -t localhost/odile-tenant:v0.23.0 -f deploy/image/Containerfile deploy/image
deploy/image/smoke.sh container --image localhost/odile-tenant:v0.23.0 --cron-wait 300
```

`smoke.sh container` runs the same five checks inside the container and adds
the two gate checks from outside, through `127.0.0.1:19131` published to the
container's port 9130.

Two container-only details, both stated in the files:

- the Webapp binds `0.0.0.0` **inside the container's network namespace**
  (podman publishes it on `127.0.0.1:19131` only). A container-side
  `127.0.0.1` bind is unreachable through podman's published port — measured
  with the first build: `curl` through the published port timed out. A real
  tenant binds loopback behind Caddy, and the QEMU image keeps that.
- the container runs `--privileged`. systemd inside a rootful container must
  keep `CAP_SYS_ADMIN` across the `User=` switch to set up the unit's
  mount-namespace sandbox (`ProtectSystem=strict`, `PrivateTmp=yes`); podman's
  default bounding set lacks it and the unit fails with `217/USER`. The tenant
  process is still uid 1000, and the `persistence` check asserts the MainPID's
  owner.

The tenant config also carries `onboarding.run: false` and
`onboarding.profile_build: "off"`, the same `onboarding` block the installer's
tenant template and `templates/config.yaml` carry (ODL-387), so the bench serves
the tenant's first screen. Measured 2026-11-10: the first run of the updated
`install-stack.sh`
printed `write /home/tenant/.hermes/config.yaml`, the second printed `same` for
every piece, and the restarted tenant's first signed-in Webapp load showed
`Sessions / No sessions yet` with no onboarding chat (`state.db`: 0 sessions);
`smoke.sh local` then passed 5/5.

## 5. The disk image

`mkdisk.sh` is destructive only inside its output directory, and it refuses to
overwrite an existing raw image without `--force`.

```sh
deploy/image/mkdisk.sh --dry-run
sudo deploy/image/mkdisk.sh --out deploy/image/out --size 8G
deploy/image/smoke.sh image --disk deploy/image/out/odile-tenant.raw
```

`mkdisk.sh` writes:

| Output | What it is |
|---|---|
| `out/odile-tenant.raw` | the bootable disk (GPT: 1 MiB BIOS-boot partition + ext4 root) |
| `out/odile-tenant.qcow2` | the same disk, `qemu-img convert -c` |
| `out/boot/vmlinuz`, `out/boot/initrd.img`, `out/boot/os-release`, `out/boot/cmdline`, `out/boot/uname` | the exact boot files the image uses, for `uki.sh` |
| `out/boot.log` | the QEMU console (boot, getty), written by `smoke.sh image` |
| `out/bench.log` | the bench serial channel: the guest's five checks, written by `smoke.sh image` |
| `/var/tmp/odile-image-rootfs` | the chroot build tree (outside the repo: `make check` lints every `.sh` and compiles every `.py` under the tree); removed on exit |

`mkdisk.sh` refuses to overwrite a raw image or a non-empty build tree without
`--force`. Its `EXIT` trap marks the `/dev` and `/sys` rbind copies **slave**
(`mount --make-rslave`) before anything else can unmount them, then unmounts in
order, detaches the loop device, and only then removes the tree. That order is
not decoration: a shared rbind plus `umount -R` propagated back to the host and
unmounted `/sys/fs/cgroup`, which stopped every systemd unit on the box
(2026-11-08). `cleanup` also asserts that `cgroup2 /sys/fs/cgroup` and
`/dev/null`, `/dev/tty` survived, and exits **3** with `HOST DAMAGE` if not.

The image boots under QEMU TCG with `-accel tcg,thread=multi` (no KVM). QEMU is
given **two serial channels**: `ttyS0` is the console (boot, getty), `ttyS1`
is the bench channel. The guest's `odile-bench-smoke.service` writes its
result to `ttyS1`; the host reads `out/bench.log` for the `ODILE-BENCH` lines
and keeps `out/boot.log` as the boot log. A unit writing to `ttyS0` printed
one line and then nothing once agetty took that console (measured 2026-11-08);
a getty-less channel has no such race. `smoke.sh image` stops QEMU when it is
done, whatever the verdict.

The guest keeps the Webapp on `127.0.0.1:9130`, like a tenant behind Caddy.
QEMU's host-forward reaches the guest's slirp address (`10.0.2.15`), not
loopback, so the bench-only `odile-bench-proxy.socket` accepts on
`10.0.2.15:9130` and `systemd-socket-proxyd` pipes it to loopback. That is the
one place the bench stands in for Caddy. `odile-bench-proxy.service` carries
`StartLimitIntervalSec=0`/`StartLimitBurst=0`: socket-proxyd serves one
connection and exits, so systemd starts it per connection, and the default
five-starts-in-ten-seconds limit left the socket accepting connections that
nothing served (measured 2026-11-08: a healthy Webapp, a 15 s timeout).

## 6. The UKI and the measurement

```sh
deploy/image/uki.sh --in deploy/image/out
```

Inputs are the kernel, initramfs, command line and `os-release` `mkdisk.sh`
recorded from the image. `uki.sh`:

1. downloads Servury's published `SHA256SUMS` and `OVMF.amdsev.fd` from
   `https://servury.com/downloads/blackbox/2026.11.1/`, and refuses to proceed
   unless both name `8731846cdbcbb6aa5c624238b839544b1553f4c5af874450f043321b3ef8958b`;
2. builds the UKI with `ukify`, passing `--os-release @boot/os-release` so the
   image is described, not the machine that built it (without it, a Debian 13
   image built on this Ubuntu host produced a UKI whose `.osrel` said
   `Ubuntu 26.04.1 LTS`), and records its sections in `out/uki-inspect.txt`;
3. runs `sev-snp-measure` twice for the UKI-as-kernel form and twice for the
   loose kernel+initrd+cmdline form, and fails if either pair differs.

The UKI is unsigned and its stub is the builder's
(`/usr/lib/systemd/boot/efi/linuxx64.efi.stub`, systemd 259.9-0ubuntu0.1). A
released UKI must pin its stub; see §7.

## 7. What the bench proves, and what only a Blackbox can prove

**The bench proves** (with the logs in §8): the stack installs idempotently
from one definition into a container and a disk; the image boots under BIOS +
GRUB on QEMU TCG; the gate answers 302/401/200/101 exactly; the tenant is an
unprivileged uid; a file survives `systemctl restart`; the Hermes cron job
fires; and the kernel/initramfs/cmdline produce a stable measurement against
Servury's published firmware hash.

**Only a Blackbox can prove:**

| Thing | Why the bench cannot |
|---|---|
| The SNP report | no `/dev/kvm`, no AMD PSP, no SEV-SNP. `sev-snp-measure` computes what the digest *should* be; only the PSP produces a signed report, and only the guest can serve it (the `/attestation` page). |
| The measured boot of the real chain | the bench never boots under Servury's OVMF with SEV enabled. The report's `measurement` equalling the recomputed digest — firmware, kernel, initramfs and cmdline as actually launched — is a Blackbox fact. |
| The initramfs unlock | the real chain drops to an initramfs prompt for the LUKS passphrase and unlocks with the user's key. The bench disk has no LUKS and boots unattended. |
| LUKS | the bench root is plain ext4 on a raw image, by design: a passphrase prompt cannot be answered headless. Keyslot layout, cryptsetup, and the user-held passphrase are Blackbox-only. |
| The shipped pin | the signed manifest — Hermes commit, installer hash, model id, skill hashes — is produced by the tenant installer and `odile-signing-key.pub` at handoff. The bench records the commit it installed (`2bd378ed…`, stable v0.23.0) but ships no manifest. |

Also out of scope for the bench: Caddy and TLS, the tenant's DNS name, the
Servury panel's vCPU count (the measurement depends on it), the SNP guest
policy, and the **systemd stub**. This UKI was built with the bench host's
Ubuntu systemd 259.9 stub, so its `.text`/`.sbat` sections are the builder's.
A released UKI must pin the stub — build it on the release image or record the
stub's sha256 in the manifest — before its measurement means anything as a
shipped pin.

## 8. What was recorded on 2026-11-08

Host: Ubuntu 26.04.1 LTS, kernel 6.12.115, 4 vCPU, 7.3 GiB RAM plus 4 GiB swap,
98 GB root, no `/dev/kvm`. QEMU 10.2.1, podman 5.7.0, debootstrap
1.0.142ubuntu2, sev-snp-measure 0.0.13, systemd 259.9.

### 8.1 The container

The first build of the day (10:35:13 → 10:47:39) died in the Webapp bundle
build: `node` reached 2.35 GiB RSS and the kernel OOM-killed it at 10:47:36
(`dmesg`: `Out of memory: Killed process … (node-MainThread) anon-rss:2348444kB`).
4 GiB of swap was added, and the same build then completed in 10 min 37 s
(image `15650bd0c372`, 3,889,010,666 bytes).

The build recorded here is the one from the final definition, with the earlier
layers cached:

```
$ podman build -t localhost/odile-tenant:v0.23.0 -f deploy/image/Containerfile deploy/image
...
installed    Hermes Agent v0.23.0 (2026.9.24) · stable · local 2bd378ed1b8d
...
COMMIT localhost/odile-tenant:v0.23.0
--> b27b2ac53418
Successfully tagged localhost/odile-tenant:v0.23.0
```

- wall clock 13:40:03 → 13:45:08 = **5 min 05 s**
- image `b27b2ac53418`, **3.88 GB**

```
$ deploy/image/smoke.sh container --image localhost/odile-tenant:v0.23.0 --cron-wait 300
ODILE-BENCH podman run localhost/odile-tenant:v0.23.0 (systemd as PID 1)
ODILE-BENCH container port mapped after 5s wall
ODILE-BENCH environment user=tenant home=/home/tenant port=9130 cron-wait=300.0s
ODILE-BENCH webapp listening after 0s
ODILE-BENCH gate-anonymous PASS GET / -> 302 /login?next=%2F; GET /api/sessions -> 401; GET /login -> 200; wrong password -> 401
ODILE-BENCH gate-session PASS POST /auth/password-login -> 200 (cookie: yes); GET / with cookie -> 200 (Webapp shell: yes); POST /api/auth/ws-ticket -> 200; GET /api/ws -> 101 (anonymous -> 403)
ODILE-BENCH shell PASS runuser -u tenant -- sh -lc ... -> Hermes Agent v0.23.0 (2026.9.24) · stable · local 2bd378ed1b8d
ODILE-BENCH persistence PASS marker survived systemctl restart odile-tenant; unit active; MainPID owner tenant
ODILE-BENCH cron PASS bench-heartbeat fired at 2026-11-08T13:45:26Z after 0s
ODILE-BENCH elapsed 22s
ODILE-BENCH final PASS image-bench: PASS (5/5)
image-bench: PASS (5/5)
ODILE-BENCH boot=5s total=31s
```

`time` reported 0m31.144s wall.

### 8.2 The disk image

```
$ sudo deploy/image/mkdisk.sh --force --out deploy/image/out --size 8G
start 2026-11-08T13:56:59Z
create       deploy/image/out/odile-tenant.raw (8G)
loop         /dev/loop1
mount        /dev/loop1p2 -> /var/tmp/odile-image-rootfs
debootstrap  trixie (ca-certificates,curl,dbus,git,grub-pc,initramfs-tools,iproute2,libatomic1,libstdc++6,linux-image-amd64,passwd,procps,python3,systemd-sysv,util-linux,xz-utils)
chroot       install-stack.sh (with guest smoke)
...
initramfs    update-initramfs -u -k all
grub         grub-install --target=i386-pc /dev/loop1
boot         kernel vmlinuz-6.12.107+deb13-amd64, initramfs 11M
qcow2        deploy/image/out/odile-tenant.qcow2
sizes        raw 8192 MiB, qcow2 1920 MiB
exit=0 end=2026-11-08T14:04:15Z
```

- wall clock 13:56:59 → 14:04:15 = **7 min 16 s**
- `odile-tenant.raw` 8,589,934,592 bytes apparent (4.8 GiB allocated);
  `odile-tenant.qcow2` 2,013,712,384 bytes (1.9 GiB on disk)

The second `install-stack.sh` run — the idempotency evidence — printed `same`
for every piece:

```
same         tenant exists (uid 1000, home /home/tenant)
same         /home/tenant/.hermes/hermes-agent
same         Hermes Agent v0.23.0 (2026.9.24) · stable · local 2bd378ed1b8d
same         Webapp bundle /home/tenant/.hermes/hermes-agent/apps/desktop/dist-webapp/index.html
same         /home/tenant/.hermes/config.yaml
same         /home/tenant/.hermes/.env (bench password unchanged)
same         cron job bench-heartbeat (every 1m, no-agent)
same         /etc/systemd/system/odile-tenant.service
same         odile-tenant.service enabled at boot
same         /etc/systemd/system/odile-bench-smoke.service
same         odile-bench-smoke.service enabled at boot
same         /etc/systemd/system/odile-bench-proxy.socket
same         /etc/systemd/system/odile-bench-proxy.service
same         odile-bench-proxy.socket enabled at boot
```

Cleanup left the host intact: the `HOST DAMAGE` assertion did not fire,
`cgroup2 /sys/fs/cgroup` was still mounted, and `/dev/null` and `/dev/tty`
were still present afterwards.

### 8.3 The boot under QEMU TCG

```
$ deploy/image/smoke.sh image --disk deploy/image/out/odile-tenant.raw --guest-timeout 2700
start 2026-11-08T14:04:23Z
ODILE-BENCH boot <checkout>/.worktrees/t_7ea69828/deploy/image/out/odile-tenant.raw under QEMU TCG (no KVM), serial .../out/boot.log, bench channel .../out/bench.log
ODILE-BENCH guest TCP port forwarded after 1s wall
ODILE-BENCH gate-anonymous PASS GET / -> 302 /login?next=%2F; GET /api/sessions -> 401; GET /login -> 200; wrong password -> 401
ODILE-BENCH gate-session PASS POST /auth/password-login -> 200 (cookie: yes); GET / with cookie -> 200 (Webapp shell: yes); POST /api/auth/ws-ticket -> 200; GET /api/ws -> 101 (anonymous -> 403)
ODILE-BENCH shell PASS runuser -u tenant -- sh -lc ... -> Hermes Agent v0.23.0 (2026.9.24) · stable · local 2bd378ed1b8d
ODILE-BENCH persistence PASS marker survived systemctl restart odile-tenant; unit active; MainPID owner tenant
ODILE-BENCH cron PASS bench-heartbeat fired at 2026-11-08T14:07:26Z after 0s
ODILE-BENCH boot=1s total=270s serial=.../out/boot.log bench=.../out/bench.log
ODILE-BENCH sizes raw=8589934592 bytes
ODILE-BENCH final PASS image-bench: PASS (5/5)
image-bench: PASS (5/5)
exit=0 end=2026-11-08T14:08:53Z
```

- wall clock 14:04:23 → 14:08:53 = **4 min 30 s** for boot + all five checks
- the guest's own clock: `webapp listening after 59s`, `elapsed 209s`
- the guest's five check lines, from `out/bench.log`:

```
ODILE-BENCH environment user=tenant home=/home/tenant port=9130 cron-wait=420.0s
ODILE-BENCH webapp listening after 59s
ODILE-BENCH gate-anonymous PASS GET / -> 302 /login?next=%2F; GET /api/sessions -> 401; GET /login -> 200; wrong password -> 401
ODILE-BENCH gate-session PASS POST /auth/password-login -> 200 (cookie: yes); GET / with cookie -> 200 (Webapp shell: yes); POST /api/auth/ws-ticket -> 200; GET /api/ws -> 101 (anonymous -> 403)
ODILE-BENCH shell PASS runuser -u tenant -- sh -lc ... -> Hermes Agent v0.23.0 (2026.9.24) · stable · local 2bd378ed1b8d
ODILE-BENCH persistence PASS marker survived systemctl restart odile-tenant; unit active; MainPID owner tenant
ODILE-BENCH cron PASS bench-heartbeat fired at 2026-11-08T14:07:26Z after 0s
ODILE-BENCH elapsed 209s
ODILE-BENCH final PASS image-bench: PASS (5/5)
image-bench: PASS (5/5)
```

The console log (`out/boot.log`, 594 bytes) shows the kernel and the autologin:

```
Debian GNU/Linux 13 odile-tenant ttyS0
odile-tenant login: tenant (automatic login)
Linux odile-tenant 6.12.107+deb13-amd64 #1 SMP PREEMPT_DYNAMIC Debian 6.12.107-1 (2026-08-29) x86_64
...
tenant@odile-tenant:~$
```

### 8.4 The UKI and the measurements

```
$ deploy/image/uki.sh --in deploy/image/out
fetch        https://servury.com/downloads/blackbox/2026.11.1/OVMF.amdsev.fd
same         .../out/ovmf/OVMF.amdsev.fd (matches the published hash)
build        .../out/odile-tenant.efi
built        .../out/odile-tenant.efi (23785472 bytes)
measure      UKI as the kernel blob (run 1)
measure      UKI as the kernel blob (run 2)
measure      loose kernel+initrd+cmdline (run 1)
measure      loose kernel+initrd+cmdline (run 2)

UKI measurement (identical twice): 1454b09b9efcc5132d14cce555b4066be9621fdb2d4394fc51a52469649cd824ef99cfd2d0bf035767822eb894badec0
loose measurement (identical twice): cdb4b28de5c74c688c09cded22349134b439c37905899cdd26abc250e816da146da0e2126e741507c18db77ad52d3141
```

| Input | sha256 |
|---|---|
| `OVMF.amdsev.fd` (Servury 2026.11.1) | `8731846cdbcbb6aa5c624238b839544b1553f4c5af874450f043321b3ef8958b` (matches the published `SHA256SUMS`) |
| `odile-tenant.efi` | `444df5db6d77db155a000d863d98728278236619e30604a68d73d9db054d217a` |
| `boot/vmlinuz` (6.12.107+deb13-amd64) | `2b2358b37674d2505350528875bb17afae2a36522a9e8a9417eaca65a7da0e08` |
| `boot/initrd.img` | `d9df2c136eedd36cb5a18593cef56ab3d045cbb673726f7365272875380d08ac` |
| `boot/os-release` (the image's, Debian 13) | `e249e69c32d81350fdd6b05ac5575255f14e380ba14b8454acea14e49f088bfa` |
| cmdline | `root=PARTUUID=e18a5309-6c37-41f1-a67f-ba3d547a8da7 ro console=ttyS0,115200 console=tty0 quiet systemd.show_status=1` |

Commands, verbatim from `out/measurements.txt`:

```
sev-snp-measure --mode snp --ovmf out/ovmf/OVMF.amdsev.fd \
  --kernel out/odile-tenant.efi --vcpus 2 --vcpu-type EPYC-v4 --guest-features 0x1
sev-snp-measure --mode snp --ovmf out/ovmf/OVMF.amdsev.fd \
  --kernel out/boot/vmlinuz --initrd out/boot/initrd.img \
  --append '<the cmdline above>' --vcpus 2 --vcpu-type EPYC-v4 --guest-features 0x1
```

Both pairs are identical: the tool is deterministic and the inputs are pinned
by sha256. The number depends on the launch parameters: **2 vCPUs, EPYC-v4,
guest-features 0x1**. Servury's panel assigns the vCPU count, so a tenant
launched with a different count gets a different measurement; recompute with
`--vcpus <N>` before comparing with `/verify`.

`--snp-ovmf-hash` was not used: with `--mode snp` it trips an assertion in
sev-snp-measure 0.0.13 (`gctx.update_zero_pages`, measured 2026-11-08), and
`--mode snp:ovmf-hash` produced a different digest from hashing the same
bytes. The OVMF file itself is what is measured, and its sha256 is checked
against Servury's published manifest in `uki.sh` before the measurement runs.

### 8.5 What the boot found in the bench itself

The first two boot attempts failed, and each failure was a bench bug, not a
tenant bug. They are recorded here because the next person will hit them again:

1. **`local` inherited the container's port.** `--port` lived on a shared
   argparse parent; `set_defaults(port=19131)` on the container subparser
   rewrote the default on the shared action, so the guest smoke waited 15
   minutes on `127.0.0.1:19131` while the Webapp listened on 9130. Fixed by
   giving every mode its own `--port` action
   (`test_each_mode_has_its_own_port_default`).
2. **The guest's output died with the console.** A unit writing to `ttyS0`
   printed its first line and nothing after agetty took the console; the unit
   exited 1 and the host saw one line of five. Fixed with a dedicated `ttyS1`
   bench channel and by installing the unit only through `install-stack.sh`
   (`test_the_guest_smoke_runs_on_the_webapp_port_and_its_own_serial`,
   `test_every_template_placeholder_has_a_substitution`).
3. **The proxy was rate-limited.** `systemd-socket-proxyd` serves one
   connection and exits, so the unit starts per connection; the default
   five-starts-in-ten-seconds limit left the socket accepting connections that
   nothing served, and a healthy Webapp looked like a 15 s timeout. Fixed with
   `StartLimitIntervalSec=0` (`test_the_proxy_is_not_rate_limited`).
4. **The `umount -R` propagated to the host.** `mount --rbind /sys` keeps the
   host's shared propagation; `umount -R` then unmounted the host's
   `/sys/fs/cgroup`, which stopped every systemd unit on the box
   (11:08 → 12:07, repaired by hand). Fixed with `--make-rslave` on both
   rbind copies and the `HOST DAMAGE` assertion in `cleanup`
   (`test_mkdisk_never_propagates_an_unmount_to_the_host`).

## 9. Cleanup and rollback

Everything the bench writes lives under `deploy/image/out/` (gitignored), in
`/var/tmp/odile-image-rootfs`, or in `/var/tmp`. The container and its image
can be removed:

```sh
podman rm -f odile-bench-<pid>                 # smoke.sh removes its own container
podman rmi localhost/odile-tenant:v0.23.0
rm -rf deploy/image/out
```

The swap file is a builder convenience, not part of the bench:

```sh
swapoff /swapfile && rm /swapfile
```

`mkdisk.sh` mounts its root under `out/rootfs` and detaches the loop device on
exit, including on failure; a killed build is cleaned up by the next run. If a
run ever prints `mkdisk: HOST DAMAGE`, stop and check
`grep cgroup2 /proc/mounts` and `/dev/null` before doing anything else.
