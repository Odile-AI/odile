# The demo's egress sentence — Security's verdict

**Card:** t_d969339e (follow-up from ODL-501) · **Date:** 2026-11-15 · **Seat:** Security
**Claim under audit:** "the demo profile cannot reach the internet except the meter
and DNS" — ODL-501's context (the private `docs/backlog.yaml`), written against
the HQ demo on 2026-11-13.
**Status:** the sentence is **false as written** and is **retired**. The reword
the card recommends ("cannot reach any other service on this box") is also false
for one class of listener. The true property, and the only one the shipped rule
bought, is *the demo profile cannot reach any other loopback listener on the
box*. The demo today — Odile's tenant machine — has no packet filter at all.

## Verdict, per claim

| # | Claim | Verdict | Evidence | Smallest change that clears it |
|---|---|---|---|---|
| E1 | The demo profile cannot reach the internet except the meter and DNS | **block** | `sudo -u odiledemo curl … https://odile.fyi/` → 200 (2026-11-13, ODL-501); the filter file's own header: *"Internet traffic is untouched"* | Retire the sentence; do not re-cut it as an egress claim |
| E2 | Reword: "cannot reach any other service on this box" | **block** | the rule matches only `ip daddr 127.0.0.0/8` and `ip6 daddr ::1`; a `0.0.0.0` listener stays reachable through the host's own address — measured here: `203.0.113.139:2222` open from the filtered uid | Say **loopback**: "cannot reach any other loopback listener on the box" |
| E3 | Today's demo machine is egress-restricted | **block** | measured on `demo.odile.fyi` as `tenant`: `command -v nft` → absent, `https://odile.fyi/` → 200, `https://example.com/` → 200 | Record the position; the runbook paragraph and the check's egress block do |
| E4 | Option 2 — close the internet on the demo | **rejected** | it is now a *tenant* machine: NEAR AI, backups and updates need egress; it changes the access surface, so it needs Savio's line; and an egress rule still says nothing about loopback | Do not do it |

## 1. What the rule did

`deploy/demo-host/odile-demo.nft` (deleted with the HQ demo at `1257777`) opened
with its own description:

> The demo runs on the same box as HQ. Unix permissions keep /root and
> /var/lib/odile out of its reach, but every process on the box shares
> 127.0.0.1: before this filter the demo shell could reach the default Hermes
> gateway's unauthenticated /api/status, every `python3 -m http.server` a seat
> leaves running, and any future loopback service (the HQ meter included).
> … Everything else to 127.0.0.0/8 and ::1 is rejected. Internet traffic is
> untouched.

That is a **loopback** filter. It accepted the meter (`127.0.0.1:8011`), DNS
(`127.0.0.53/54:53`, both directions) and replies to the demo's own Webapp
(`sport 9130`), then rejected the rest of `127.0.0.0/8` and `::1`. Nothing
matched a destination outside loopback, so the internet was open by
construction. ODL-501 measured exactly that (`https://odile.fyi/` → 200) and
recorded it in the runbook; the sentence in the card was never re-cut.

## 2. The measurement that settles the reword

The recommended wording says "any other service on this box". The rule's reach
is narrower than that, and the difference is measurable. On 2026-11-15 I loaded
the retired file's rules into a throwaway table, templated to an unprivileged
uid (`meta skuid nobody`), and probed as that uid:

```sh
$ nft -f /var/tmp/odile-t_d969339e-probe/probe.nft && nft list table inet odile_demo_probe
table inet odile_demo_probe {
	chain output {
		type filter hook output priority filter; policy accept;
		meta skuid 65534 ip daddr 127.0.0.1 tcp sport 9130 accept
		meta skuid 65534 ip daddr 127.0.0.1 tcp dport 8011 accept
		…
		meta skuid 65534 ip daddr 127.0.0.0/8 reject with icmp port-unreachable
		meta skuid 65534 ip6 daddr ::1 reject with icmpv6 port-unreachable
	}
}
$ sudo -u nobody /usr/bin/python3 /var/tmp/odile-t_d969339e-probe/connect.py
ConnectionRefusedError loopback:8080 (the waitlist, not allowed): [Errno 111] Connection refused
OPEN      stub DNS 127.0.0.53:53 (allowed)
OPEN      host public addr :2222 (sshd on 0.0.0.0)
ConnectionRefusedError host public addr :8080 (loopback-only svc): [Errno 111] Connection refused
OPEN      public internet 1.1.1.1:443
$ nft delete table inet odile_demo_probe && nft list ruleset | grep -c odile_demo_probe
0
```

So the rule bought *loopback isolation*, not a service boundary: sshd on
`0.0.0.0:2222` — and the other ports on the public-listener baseline ODL-501
recorded (22 25 80 443 587 2222 8099) — stayed reachable through the host's own
address. The honest sentence is the loopback one. The table is deleted; the
probe directory is removed with this card.

## 3. Today's demo machine

The demo is Odile's tenant machine (`odl-7971a8`, `demo.odile.fyi`), not the HQ
profile the sentence describes. Measured 2026-11-15 through `tenant_console.py`
(sign-in as `judge`, `shell.exec` as `tenant`):

```sh
$ id -un; sudo -n id -un
tenant
root
$ command -v nft || echo nft-absent
nft-absent
$ sudo -n nft list ruleset
sudo: nft: command not found
$ curl -s -o /dev/null -w '%{http_code}\n' https://odile.fyi/
200
$ curl -s -o /dev/null -w '%{http_code}\n' https://example.com/
200
$ head -2 /etc/resolv.conf
nameserver 1.1.1.1
nameserver 9.9.9.9
```

`iptables` is absent too. The same `/etc/odile-demo` directory, `odiledemo`
uid, `odile-demo*` units and `inet odile_demo` table are gone from HQ — checked
on 2026-11-15 (`getent passwd odiledemo` → nothing; `nft list ruleset | grep -c
odile_demo` → 0; `systemctl list-unit-files 'odile-demo*'` → 0 units).

The check's new egress block ran in place later the same day
(`check_demo_host.sh --tenant`): `== egress` → *no nft on the demo machine (no
packet filter)* and *the demo shell reaches the public internet: 200*. The run
finished **98 passed, 1 failed**; the failure is the unrelated `odile timers on
this machine` line, which ODL-561 is restoring on this tenant as this card is
written.

## 4. What changed

| File | Change |
|---|---|
| `docs/runbooks/demo-public.md` §3 | new *Egress: no packet filter* paragraph: today's transcript, the retired filter's own header, the exact loopback wording, and the `0.0.0.0` blind spot |
| `deploy/demo-host/check_demo_host.sh` §3b | the egress block: `nft` absent, and `sudo -u tenant curl https://odile.fyi/` → 200 — a filter appearing fails the check, which forces the paragraph's re-cut |
| `tests/test_no_retired_demo_code.py` | `DemoEgressTests` pins the pair (the check's two facts, the runbook's three lines) so one cannot be dropped without the other |
| `docs/backlog.yaml` (private writeback) | ODL-501's sentence re-cut to the loopback property and its `retired:` note extended with this card's measurement and the HQ-demo deletion |

No public page sentence changed and no `CLAIMS.md` row was added: the paragraph
is a dated measurement, and the approved demo rows already say the judge's shell
runs as the machine's owner (`docs/CLAIMS.md` § *The demo machine*), which
implies the network. A deck row naming egress would be a Product lead judgment,
not a security one.

## 5. Residual

- The check looks for `nft`. A filter that is not `nft` — an eBPF program, a
  container network, `iptables` (also absent today) — would pass it. Adding one
  is a card, and the runbook paragraph would be re-cut with it.
- Full egress is the *tenant* design, not a demo defect: a tenant machine must
  reach NEAR AI, the backup target and the release channel. The demo's exposure
  is the published password and the root shell behind it, which the deck states.
- The 2026-11-13 line itself cannot be re-measured: the profile, the filter and
  the meter are deleted. Its record is the ODL-501 transcript and the filter
  file's own header in git history (`1257777^`), both quoted above.
