#!/usr/bin/python3
"""The machine-side half of ODL-505's demo update — run on the demo tenant.

`deploy/demo-host/deploy_demo_icons.py` uploads the new page server, the two
pages, the five icons, the re-assert helper, the sign-in page and the Caddy
block, then runs this program as root. It converges the three files that belong
to `apply_demo_public.py`'s surface and nothing else:

  * the demo lede on `/usr/local/lib/odile/verify.html` (the page was replaced
    with the release's own copy, which carries the icon links);
  * the sign-in page at `/srv/odile-demo/login.html`, and the three tab files
    beside it (`ensure_icons`, t_6b25f6e5);
  * the `demo.odile.fyi` Caddy block, with the three static tab routes and the
    two page-server routes.

It does **not** touch the model, provider, judge account, sessions or `.env`:
those are `apply_demo_public.py`'s `--write` run, and this card does not need
it. The program lives beside `apply_demo_public.py` so it imports the same
functions, not a second copy.

    sudo /usr/bin/python3 /usr/local/lib/odile-demo-public/apply_demo_icons.py --restart
    sudo /usr/bin/python3 /usr/local/lib/odile-demo-public/apply_demo_icons.py --dry-run

Exit codes: 0 converged, 1 a refused splice (apply_demo_public raises), 2 bad
input.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

sys.path.insert(0, "/usr/local/lib/odile-demo-public")

import apply_demo_public as demo  # noqa: E402

FIRST_VISIT = "odile-first-visit"
BRAND_HELPER = "/usr/local/lib/odile/odile-webapp-brand.py"


def restart(unit: str) -> None:
    """Restart one unit and wait for it; the console cannot send this command.

    The Webapp's command guard reads `systemctl restart` as destructive, so
    the restart lives here, on the machine, beside the splice that needs it.
    """
    subprocess.run(["systemctl", "restart", unit], check=True)
    for _ in range(20):
        state = subprocess.run(["systemctl", "is-active", unit],
                               capture_output=True, text=True).stdout.strip()
        if state == "active":
            print(f"restart      {unit}: active", flush=True)
            return
        time.sleep(0.5)
    raise SystemExit(f"error: {unit} did not become active")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Converge the demo's icon surface.")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--restart", action="store_true",
                        help="restart odile-first-visit (the page server changed)")
    args = parser.parse_args(argv)
    dry = args.dry_run
    changed = demo.ensure_verify_lede(dry)
    changed = demo.ensure_login_page(dry) or changed
    changed = demo.ensure_icons(dry) or changed
    changed = demo.ensure_caddy_block(dry) or changed
    print(("would write" if dry else "write") if changed else "same",
          "demo icon surface (/verify lede, /login page, tab icons, Caddy block)",
          flush=True)
    if dry:
        return 0
    if args.restart:
        restart(FIRST_VISIT)
    for attempt in ("first", "second"):
        result = subprocess.run([BRAND_HELPER, "--home", "/home/tenant"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(f"error: {BRAND_HELPER} -> {result.returncode}: "
                             f"{result.stderr.strip()}")
        print(result.stdout.rstrip(), flush=True)
        print(f"helper       {attempt}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
