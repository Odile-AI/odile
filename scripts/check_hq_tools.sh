#!/bin/sh
# The live proof a deploy left HQ's private tools running (ODL-347-F1-F1).
#
# A verifier runs this after a deploy. It answers two questions:
#
#   1. Does /opt/odile carry HQ's private tools, byte for byte? Every file
#      `.git/info/exclude` hides at HQ is compared (scripts/hq_tools.sh check),
#      which includes tools/roadmap_page.py against the HQ checkout's copy.
#   2. Did the tick that wrote /var/lib/odile/roadmap.html run those bytes?
#      The page's own footer names the instant it was generated; that instant
#      must not predate the installed tool, the footer's source sha must be the
#      one the deploy recorded in /var/lib/odile/roadmap-source, and the
#      deployed generator must render that same footer in a --stdout dry run.
#      The page is not compared byte for byte: the board moves between ticks.
#
# usage: scripts/check_hq_tools.sh [DEST] [HQ_ROOT] [PAGE] [SOURCE_FILE]
#   defaults: /opt/odile  <parent of the shared .git>  /var/lib/odile/roadmap.html
#             /var/lib/odile/roadmap-source
set -eu

script_dir=$(unset CDPATH; cd -- "$(dirname -- "$0")" && pwd)
DEST=${1:-/opt/odile}
HQ=${2:-}
PAGE=${3:-/var/lib/odile/roadmap.html}
SOURCE=${4:-/var/lib/odile/roadmap-source}

fail() {
	echo "check-hq-tools: $*" >&2
	exit 1
}

if [ -z "$HQ" ]; then
	common=$(git -C "$script_dir/.." rev-parse --path-format=absolute --git-common-dir 2>/dev/null) \
		|| fail "cannot resolve the HQ checkout from $script_dir; pass HQ_ROOT as the second argument"
	HQ=$(unset CDPATH; cd -- "$common/.." && pwd)
fi

"$script_dir/hq_tools.sh" check "$DEST" "$HQ" || fail "$DEST does not carry HQ's private tools (see the DIFF lines above)"

[ -f "$PAGE" ] || fail "$PAGE is missing; the tick has not written a page (systemctl start odile-roadmap.service)"
tool=$DEST/tools/roadmap_page.py
[ -f "$tool" ] || fail "$tool is missing"

# The footer the tick itself wrote: `generated <ISO-8601 UTC> · source <sha> · odile.fyi`.
foot=$(grep -o '<p class="foot">generated [^<]*</p>' "$PAGE" | tail -n 1)
generated=$(printf '%s\n' "$foot" | sed -n 's/.*generated \([^ ]*\) ·.*/\1/p')
source_sha=$(printf '%s\n' "$foot" | sed -n 's/.*source \([^ ]*\) ·.*/\1/p')
[ -n "$generated" ] && [ -n "$source_sha" ] || fail "$PAGE carries no parseable footer"

recorded=$(cat "$SOURCE" 2>/dev/null || true)
[ "$source_sha" = "$recorded" ] || fail "the page names source '$source_sha', $SOURCE says '$recorded' (a stale page, or a deploy that did not write it)"

page_epoch=$(date -u -d "$generated" +%s 2>/dev/null) \
	|| fail "the page's generated instant '$generated' is not a date"
tool_mtime=$(stat -c %Y "$tool")
tool_installed=$(date -u -d "@$tool_mtime" +%FT%TZ)
if [ "$page_epoch" -lt "$tool_mtime" ]; then
	fail "the page was generated $generated, before $tool was installed at $tool_installed; the tick has not run since (systemctl start odile-roadmap.service)"
fi
echo "check-hq-tools: page generated $generated, after $tool was installed at $tool_installed"

# Prove those bytes can write that page: a --stdout dry run at the page's own
# instant must reproduce the footer. Everything else is read from the deployed
# tree, exactly as odile-roadmap.service runs it.
rendered=$(mktemp)
trap 'rm -f "$rendered"' EXIT
/usr/bin/python3 "$tool" \
	--backlog "$DEST/docs/backlog.yaml" \
	--template "$DEST/web/roadmap.html" \
	--adr "$DEST/docs/architecture/ADR-upgrades-and-model.md" \
	--now "$generated" \
	--source-sha "$source_sha" \
	--stdout > "$rendered" \
	|| fail "the deployed generator failed to render (see the error above)"
grep -qF "generated $generated · source $source_sha · odile.fyi" "$rendered" \
	|| fail "the deployed generator does not reproduce the page's footer"
echo "check-hq-tools: the deployed generator renders the same footer at $generated"
echo "check-hq-tools: OK"
