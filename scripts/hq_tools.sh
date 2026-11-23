#!/bin/sh
# HQ-only tools: one rule for every private file under tools/.
#
# `tools/` is mixed. The tracked files ship with the tree; every file
# `.git/info/exclude` hides is private machinery that exists only in the HQ
# checkout — `tools/roadmap_page.py` is the generator odile-roadmap.service
# executes, `tools/board_sync.py` is what the board is cut with. A card
# worktree can carry its own (stale) copy of a hidden file, because the same
# exclude file hides it there too, so `git status` stays clean and nothing
# stops it from reaching /opt/odile. Measured 2026-11-23 (ODL-347-F1-F1): a
# deploy from .worktrees/t_a0e1fdb5 installed its pre-card generator over the
# fixed bytes, and the next tick served the reverted page.
#
# The rule: the deployed tree runs the HQ checkout's copy. scripts/deploy.sh
# excludes every HQ-only file from the rsync and runs `install` after it.
#
#   scripts/hq_tools.sh list [HQ_ROOT]                  the HQ-only paths
#   scripts/hq_tools.sh install [--dry-run] HQ_ROOT DEST
#   scripts/hq_tools.sh check DEST HQ_ROOT              sha256 per path; 1 on DIFF
#
# HQ_ROOT is the checkout that owns the shared .git: the parent of
# `git rev-parse --git-common-dir`, which every worktree of the repository
# reports. `list` derives it when it is not given.
set -eu

usage() {
	cat <<'EOF'
usage: scripts/hq_tools.sh list [HQ_ROOT]
       scripts/hq_tools.sh install [--dry-run] HQ_ROOT DEST
       scripts/hq_tools.sh check DEST HQ_ROOT

list     print one HQ-only path (relative to HQ_ROOT) per line
install  copy every HQ-only path from HQ_ROOT into DEST; idempotent
check    compare every HQ-only path in DEST with HQ_ROOT; exit 1 on a mismatch
EOF
}

resolve_hq() {
	if [ "$#" -ge 1 ] && [ -n "$1" ]; then
		printf '%s\n' "$1"
		return 0
	fi
	script_dir=$(unset CDPATH; cd -- "$(dirname -- "$0")" && pwd)
	repo=$(unset CDPATH; cd -- "$script_dir/.." && pwd)
	common=$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) \
		|| { echo "hq-tools: refused: $repo is not a git checkout; pass HQ_ROOT" >&2; return 1; }
	(unset CDPATH; cd -- "$common/.." && pwd)
}

# The files git ignores at HQ: present in its working tree, absent from its
# index. Sorted by ls-files; caches are not tools.
hq_only_files() {
	hq=$1
	[ -d "$hq/tools" ] || return 0
	git -C "$hq" ls-files --others --ignored --exclude-standard -- tools 2>/dev/null \
		| grep -v -e '__pycache__' -e '\.pyc$' || true
}

cmd_list() {
	[ "$#" -le 1 ] || { usage >&2; exit 2; }
	hq=$(resolve_hq "${1:-}")
	hq_only_files "$hq"
}

cmd_install() {
	dry=0
	if [ "${1:-}" = "--dry-run" ]; then
		dry=1
		shift
	fi
	[ "$#" -eq 2 ] || { usage >&2; exit 2; }
	hq=$1
	dest=$2
	[ -d "$hq" ] || { echo "hq-tools: refused: $hq is not a directory" >&2; exit 1; }
	files=$(hq_only_files "$hq")
	if [ -z "$files" ]; then
		echo "hq-tools: no HQ-only files under $hq/tools; DEST keeps the copies it has (deploy from a worktree of the HQ checkout, not a standalone clone — docs/runbooks/deploy.md)"
		return 0
	fi
	changed=0
	same=0
	for path in $files; do
		src=$hq/$path
		dst=$dest/$path
		sum=$(sha256sum "$src" | cut -d' ' -f1)
		if [ -f "$dst" ] && [ "$(sha256sum "$dst" | cut -d' ' -f1)" = "$sum" ]; then
			echo "hq-tools: same $path"
			same=$((same + 1))
			continue
		fi
		if [ "$dry" -eq 1 ]; then
			echo "hq-tools: would install $path ($sum)"
		else
			install -D -m "$(stat -c '%a' "$src")" "$src" "$dst"
			echo "hq-tools: installed $path ($sum)"
		fi
		changed=$((changed + 1))
	done
	if [ "$dry" -eq 1 ]; then
		echo "hq-tools: $changed file(s) would change, $same already HQ's bytes"
	else
		echo "hq-tools: $changed file(s) changed, $same already HQ's bytes"
	fi
}

cmd_check() {
	[ "$#" -eq 2 ] || { usage >&2; exit 2; }
	dest=$1
	hq=$2
	files=$(hq_only_files "$hq")
	if [ -z "$files" ]; then
		echo "hq-tools: refused: $hq/tools has no HQ-only files to compare" >&2
		exit 1
	fi
	bad=0
	for path in $files; do
		src=$hq/$path
		dst=$dest/$path
		sum=$(sha256sum "$src" | cut -d' ' -f1)
		if [ ! -f "$dst" ]; then
			echo "hq-tools: MISSING $dst (HQ has $sum)"
			bad=1
			continue
		fi
		found=$(sha256sum "$dst" | cut -d' ' -f1)
		if [ "$found" = "$sum" ]; then
			echo "hq-tools: same $path $sum"
		else
			echo "hq-tools: DIFF $path expected $sum found $found"
			bad=1
		fi
	done
	exit "$bad"
}

verb=${1:-}
if [ "$#" -gt 0 ]; then
	shift
fi
case "$verb" in
	list) cmd_list "$@" ;;
	install) cmd_install "$@" ;;
	check) cmd_check "$@" ;;
	-h|--help) usage ;;
	*) usage >&2; exit 2 ;;
esac
