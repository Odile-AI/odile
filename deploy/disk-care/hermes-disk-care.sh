#!/usr/bin/env bash
# hermes-disk-care.sh — bounded, safe reclaim of the space Hermes seats leave behind.
# Runs from hermes-disk-care.timer (every 6 h). Safe to run by hand: --dry-run prints only.
#
# The source of truth is deploy/disk-care/hermes-disk-care.sh in the Odile repo.
# The host copy at /usr/local/sbin is `install -m 0755` from there and the two
# units beside it are installed the same way; docs/ops/DISK-CARE.md carries the
# step. Edit the repo file, install it, then `cmp` the two — a rebuilt host must
# not depend on a remembered paste.
#
# --tmp-only runs just section 2b, the /tmp rule: no lock, no board, no other
# section. tests/test_disk_care.py drives it over a fixture directory through
# TMP_ROOT, TMP_MIN_BYTES and TMP_STALE_HOURS; TMP_FD_CHECK=proc forces the
# /proc scan for hosts without lsof.
#
# Rules encoded here, learned the hard way on this host:
#  * A loop device backed by a "(deleted)" file still pins its blocks until detached.
#  * /var/tmp holds multi-GB one-shot test roots; only trees with no live reference and
#    no recent write are removed, and the allow-list below is never touched.
#  * /tmp is a 10 GB tmpfs — RAM, not disk. A 1 GB file there costs 1 GB of host
#    memory and `df /` never shows it; stale multi-GB files are removed outright.
#  * Profile scratch and merged, clean git worktrees of finished cards are disposable.
#  * Caches (journal, apt, npm, pip, uv, podman layers) are rebuilt on demand.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
DRY=0
ONLY=all
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --tmp-only) ONLY=tmp ;;
    *) echo "hermes-disk-care.sh: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

LOG=${LOG:-/var/log/hermes-disk-care.log}
LOCK=${LOCK:-/run/hermes-disk-care.lock}
TMP_ROOT=${TMP_ROOT:-/tmp}
# --tmp-only is a test/debug selector, so it takes no lock: the timer may hold
# one while a fixture run needs the script itself.
if [ "$ONLY" = all ]; then
  exec 9>"$LOCK" || exit 0
  flock -n 9 || { echo "$(date -u +%FT%TZ) another run holds the lock" >>"$LOG"; exit 0; }
fi

STALE_HOURS=${STALE_HOURS:-}
# under real pressure, shrink the staleness window instead of waiting a full day
used_pct=$(df -P / | awk 'NR==2{gsub("%","",$5); print $5}')
if [ -z "$STALE_HOURS" ]; then
  if [ "${used_pct:-0}" -ge 90 ]; then STALE_HOURS=3; else STALE_HOURS=12; fi
fi
KEEP_TREES="/var/tmp/bb"          # Servury rebuild evidence; never auto-removed
now=$(date +%s)
PROFILES_DIR=/root/.hermes/profiles
# $HOME is unset in a system service with no User=, so name the fallback only.
REPO=${REPO:-${HOME:-/root}/odile}
BOARD_DB=/root/.hermes/kanban/boards/odile/kanban.db
log() { echo "$(date -u +%FT%TZ) $*" >>"$LOG"; }
run() { if [ "$DRY" = 1 ]; then echo "DRY: $*"; else "$@"; fi; }
before=$(df -Pm / | awk 'NR==2{print $4}')

log "=== start (dry=$DRY, stale=${STALE_HOURS}h, free=${before}MB) ==="

# --tmp-only jumps straight to section 2b: sections 1-2 touch loops, /dev and
# the board, and a fixture run must touch none of them.
if [ "$ONLY" != tmp ]; then

# ---------------------------------------------------------------- 1. loop devices
# detach loops whose backing file is already gone and which nothing mounts
losetup -a 2>/dev/null | grep -F '(deleted)' | while IFS=: read -r dev _ rest; do
  dev=$(echo "$dev" | tr -d ' ')
  [ -b "$dev" ] || continue
  mm=$(lsblk -rno MAJ:MIN "$dev" 2>/dev/null | head -1)
  if [ -n "$mm" ] && grep -q " $mm " /proc/self/mountinfo; then continue; fi
  log "detach orphan loop $dev ($rest)"
  run losetup -d "$dev" 2>/dev/null || true
done

# deferred-remove dm devices whose backing loop file is already deleted: the last
# opener closing removes them (a plain `remove` fails busy while a service keeps a
# private mount namespace alive)
if command -v dmsetup >/dev/null 2>&1; then
  dmsetup ls --noheadings 2>/dev/null | awk '{print $1}' | while read -r name; do
    [ -n "$name" ] && [ "$name" != "root_crypt" ] || continue
    dev=$(dmsetup table "$name" 2>/dev/null | head -1 | grep -Eo '[0-9]+:[0-9]+' | tail -1)
    [ -n "$dev" ] || continue
    backing=$(cat "/sys/dev/block/$dev/loop/backing_file" 2>/dev/null || true)
    case "$backing" in
      *"(deleted)"*)
        log "defer-remove dm $name (backing $dev deleted)"
        run dmsetup remove --deferred "$name" 2>/dev/null || true
        ;;
    esac
  done
fi

# this host's /dev is a minimal devtmpfs; recreate core nodes if something ate them
for spec in "null c 1 3" "zero c 1 5" "full c 1 7" "random c 1 8" "urandom c 1 9" "tty c 5 0"; do
  read -r node typ maj min <<< "$spec"
  [ -e "/dev/$node" ] || run mknod "/dev/$node" "$typ" "$maj" "$min" 2>/dev/null
done
udevadm trigger --action=add >/dev/null 2>&1 || true

# ------------------------------------------------- 2. stale one-shot roots in /var/tmp
# A tree is removed only when ALL of these hold:
#   * name matches an ephemeral prefix (odile-*, odlNNN-*, verify-*, bb-*, dx*, repro-*)
#   * no process has it as cwd/root, no fd points inside, no mount references it
#   * no LIVE board card (todo/ready/running/review/blocked/scheduled) names its id or ODL number
#   * no board text from the last 48 h references the path (comments, results)
#   * it is older than STALE_HOURS — or older than 1 h and owned by a DONE card
# Trees >=100 MB are moved to /var/tmp/.disk-care-trash first (48 h undo) unless the
# volume is already >=95 % used, where space must be freed immediately.
python3 - "$DRY" "$STALE_HOURS" "$BOARD_DB" "$KEEP_TREES" "$(df -P / | awk 'NR==2{gsub("%","",$5); print $5}')" <<'PY' >>"$LOG" 2>&1
import os, re, sys, time, shutil, sqlite3, subprocess

dry = sys.argv[1] == "1"
stale_h = int(sys.argv[2])
board = sys.argv[3]
keep = {p.rstrip("/") for p in sys.argv[4].split()}
used_pct = int(sys.argv[5] or 0)
now = time.time()
TRASH = "/var/tmp/.disk-care-trash"
MANIFEST = "/var/lib/hermes-disk-care/manifest.log"
os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)

def note(line):
    print(line)
    try:
        with open(MANIFEST, "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {line}\n")
    except OSError:
        pass

# --- board: live ids/numbers keep a tree even if nothing is running in it yet
live_ids, live_nums, live_text = set(), set(), []
done = {}          # number -> completed_at
done_ids = set()   # t_<8hex> of finished cards
recent_text = []   # anything a human/agent looked at in the last 48 h
board_ok = os.path.exists(board)
if board_ok:
    try:
        con = sqlite3.connect(f"file:{board}?mode=ro", uri=True, timeout=20)
        cur = con.cursor()
        for tid, title, body, result in cur.execute(
                "select id,title,coalesce(body,''),coalesce(result,'') from tasks "
                "where status not in ('done','archived')"):
            live_ids.add(tid)
            live_text.append(title + "\n" + body + "\n" + result)
            live_nums |= set(re.findall(r"(?:odile|odl)[^0-9]{0,20}(\d{2,4})(?![0-9a-f]{2})", title, re.I))
        # every comment on a live card protects its path, however old the card is
        for (body,) in cur.execute(
                "select c.body from task_comments c join tasks t on t.id=c.task_id "
                "where t.status not in ('done','archived')"):
            live_text.append(body or "")
        cutoff = now - 48 * 3600
        for tid, title, body, result, comp in cur.execute(
                "select id,title,coalesce(body,''),coalesce(result,''),completed_at from tasks "
                "where status in ('done','archived')"):
            if comp and comp > cutoff:
                recent_text.append(body + "\n" + result)
            for m in re.findall(r"(?:odile|odl)[^0-9]{0,20}(\d{2,4})(?![0-9a-f]{2})", title, re.I):
                done[m] = comp or 0
            for h in re.findall(r"(?<![0-9a-f])([0-9a-f]{8})(?![0-9a-f])", title, re.I):
                done_ids.add(f"t_{h}")
        for (body,) in cur.execute("select body from task_comments where created_at > ?", (cutoff,)):
            recent_text.append(body or "")
        con.close()
    except Exception as exc:  # fail closed: without the board, delete nothing
        board_ok = False
        note(f"board read failed ({exc}); no /var/tmp or scratch deletion this run")
live_text = "\n".join(live_text)
recent_text = "\n".join(recent_text)

# --- process references, collected once
refs = set()
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    base = f"/proc/{pid}"
    for name in ("cwd", "root"):
        try:
            refs.add(os.readlink(f"{base}/{name}"))
        except OSError:
            pass
    try:
        for fd in os.listdir(f"{base}/fd"):
            try:
                refs.add(os.readlink(f"{base}/fd/{fd}"))
            except OSError:
                pass
    except OSError:
        pass
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        with open(f"/proc/{pid}/mountinfo") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) > 4:
                    refs.add(parts[4])
    except OSError:
        pass

def referenced(path):
    for r in refs:
        if r == path or r.startswith(path + "/"):
            return True
    return False

def nums(s):
    out = set()
    out |= set(re.findall(r"(?:odile|odl)[^0-9]{0,20}(\d{2,4})(?![0-9a-f]{2})", s, re.I))
    return out

def hex_ids(s):
    """t_<8hex> task ids, with or without the t_ prefix in the directory name."""
    return {f"t_{h}" for h in re.findall(r"(?<![0-9a-f])([0-9a-f]{8})(?![0-9a-f])", s, re.I)}

def fresh_refs(path):
    """Re-scan /proc for this path right before deleting it (the snapshot may be stale)."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        base = f"/proc/{pid}"
        for name in ("cwd", "root"):
            try:
                v = os.readlink(f"{base}/{name}")
                if v == path or v.startswith(path + "/"):
                    return True
            except OSError:
                pass
        try:
            for fd in os.listdir(f"{base}/fd"):
                try:
                    v = os.readlink(f"{base}/fd/{fd}")
                    if v == path or v.startswith(path + "/"):
                        return True
                except OSError:
                    pass
        except OSError:
            pass
        try:
            with open(f"{base}/mountinfo") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) > 4 and (parts[4] == path or parts[4].startswith(path + "/")):
                        return True
        except OSError:
            pass
    return False

def fresh_live(path, base):
    """Ask the board again: is this tree named by a live card or its comments?"""
    try:
        con = sqlite3.connect(f"file:{board}?mode=ro", uri=True, timeout=10)
        cur = con.cursor()
        like = f"%{path}%"
        if cur.execute("select count(*) from tasks where status not in ('done','archived') "
                       "and (title like ? or body like ? or result like ?)",
                       (like, like, like)).fetchone()[0]:
            return True
        ids = hex_ids(base)
        nset = nums(base)
        for tid, title in cur.execute("select id,title from tasks where status not in ('done','archived')"):
            if tid in ids:
                return True
            if nset and nset & nums(title):
                return True
        if cur.execute("select count(*) from task_comments c join tasks t on t.id=c.task_id "
                       "where t.status not in ('done','archived') and c.body like ?",
                       (like,)).fetchone()[0]:
            return True
        con.close()
        return False
    except Exception:
        return True  # fail closed

# --- purge the quarantine: 48 h undo window, or immediately under pressure
if os.path.isdir(TRASH):
    for e in os.listdir(TRASH):
        p = os.path.join(TRASH, e)
        m = re.search(r"\.(\d{10})$", e)
        if m:
            old = (now - int(m.group(1))) > 48 * 3600
        else:
            try:
                old = (now - os.stat(p).st_mtime) > 48 * 3600
            except OSError:
                old = True
        if old or used_pct >= 95:
            if dry:
                print(f"DRY purge trash {p}")
            else:
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
                note(f"purge-trash {p}")

prefix = re.compile(r"^(odile-|odl\d|verify-|bb-|dx$|dx-|repro-)")
now_ts = int(now)
if not board_ok:
    note("board unavailable — /var/tmp trees kept this run")
for base in sorted(os.listdir("/var/tmp")):
    if not board_ok:
        break
    p = os.path.join("/var/tmp", base)
    if base.startswith(".") or p in keep or not os.path.isdir(p) or not prefix.match(base):
        continue
    try:
        age = now - os.stat(p).st_mtime
    except OSError:
        continue
    numset = nums(base)
    owner_id = hex_ids(base)
    if owner_id & live_ids or numset & live_nums:
        note(f"keep live-card {p} ({age/3600:.0f}h)")
        continue
    if p in live_text or p in recent_text:
        note(f"keep board-referenced {p} ({age/3600:.0f}h)")
        continue
    if referenced(p):
        note(f"keep process-referenced {p} ({age/3600:.0f}h)")
        continue
    owned_by_done = bool(numset & set(done)) or bool(owner_id & done_ids)
    min_age = 3600 if owned_by_done else stale_h * 3600
    if age < min_age:
        continue
    # last look: re-ask the board and /proc immediately before the irreversible step
    if fresh_live(p, base) or fresh_refs(p):
        note(f"keep late-live {p} ({age/3600:.0f}h)")
        continue
    try:
        mb = int(subprocess.run(["du", "-sm", p], capture_output=True, text=True).stdout.split("\t")[0])
    except Exception:
        mb = 0
    reason = "done-card" if owned_by_done else "stale"
    if mb >= 100 and used_pct < 95:
        dest = os.path.join(TRASH, f"{base}.{now_ts}")
        note(f"quarantine {p} ({age/3600:.0f}h, {mb}MB, {reason}) -> {dest}")
        if not dry:
            os.makedirs(TRASH, exist_ok=True)
            shutil.move(p, dest)
    else:
        note(f"rm {p} ({age/3600:.0f}h, {mb}MB, {reason})")
        if not dry:
            subprocess.run(["rm", "-rf", "--one-file-system", p])

# --- cap the quarantine: keep at most 8 GB; oldest entries go first
CAP_MB = 24576  # 24 GB undo window; the volume is 200 GB at the block layer
if os.path.isdir(TRASH):
    entries, total = [], 0
    for e in os.listdir(TRASH):
        p = os.path.join(TRASH, e)
        try:
            mb = int(subprocess.run(["du", "-sm", p], capture_output=True, text=True).stdout.split("\t")[0])
        except Exception:
            mb = 0
        m = re.search(r"\.(\d{10})$", e)
        ts = int(m.group(1)) if m else int(os.stat(p).st_mtime)
        entries.append((ts, p, mb))
        total += mb
    for ts, p, mb in sorted(entries):
        if total <= CAP_MB:
            break
        note(f"cap-purge {p} ({mb}MB, trash {total}MB)")
        if not dry:
            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
        total -= mb
PY

fi

# ------------------------------------- 2b. multi-GB files in /tmp (tmpfs is RAM)
# /tmp is a 10 GB tmpfs: a 1 GB file there costs 1 GB of host memory, `df /`
# never shows it, and a 7.58 GB raw image left there plus an 8 GB QEMU guest
# drove the 14 GB host into global OOM three times on 2026-11-12 (19:30:58,
# 20:10:40, 20:47:30) — each OOM killed the guest, systemd stopped the gateway
# and every managed worker died with it. A regular file >= 1 GB that is older
# than 2 h and has no open fd is removed outright; there is no quarantine,
# because a copy on the same tmpfs frees nothing. A file younger than 2 h, or
# one a process holds open (a running QEMU's disk), is kept.
TMP_STALE_HOURS=${TMP_STALE_HOURS:-2}
TMP_MIN_BYTES=${TMP_MIN_BYTES:-1073741824}
# open_fd uses lsof when it exists; TMP_FD_CHECK=proc forces the /proc scan,
# which is also what a host without lsof gets.
TMP_FD_CHECK=${TMP_FD_CHECK:-auto}
tmp_before=$(df -P "$TMP_ROOT" 2>/dev/null | awk 'NR==2{print $3}')
open_fd() {  # 0 when some process holds $1 open
  if [ "$TMP_FD_CHECK" != proc ] && command -v lsof >/dev/null 2>&1; then
    lsof -t -- "$1" >/dev/null 2>&1
    return $?
  fi
  for fd in /proc/[0-9]*/fd/*; do
    [ "$(readlink "$fd" 2>/dev/null)" = "$1" ] && return 0
  done
  return 1
}
find "$TMP_ROOT" -xdev -type f -size +$((TMP_MIN_BYTES - 1))c -mmin +$((TMP_STALE_HOURS * 60)) -print0 2>/dev/null |
while IFS= read -r -d '' f; do
  size=$(stat -c %s "$f" 2>/dev/null || echo 0)
  mtime=$(stat -c %y "$f" 2>/dev/null | cut -d. -f1)
  if open_fd "$f"; then
    log "keep $TMP_ROOT file (held open) $f (${size} bytes, mtime ${mtime})"
    continue
  fi
  if [ "$DRY" = 1 ]; then
    log "would remove $TMP_ROOT file $f (${size} bytes, mtime ${mtime})"
    echo "DRY: would remove $TMP_ROOT file $f (${size} bytes, mtime ${mtime})"
  else
    log "rm $TMP_ROOT file $f (${size} bytes, mtime ${mtime})"
    rm -f -- "$f" || log "rm failed $TMP_ROOT file $f"
  fi
done
tmp_after=$(df -P "$TMP_ROOT" 2>/dev/null | awk 'NR==2{print $3}')
log "tmpfs $TMP_ROOT used ${tmp_before:-?}KB -> ${tmp_after:-?}KB (files >= $((TMP_MIN_BYTES / 1073741824))GB, older than ${TMP_STALE_HOURS}h, no open fd)"

# --tmp-only ends here: the rest is board work, scratch, worktrees and caches.
[ "$ONLY" = tmp ] && exit 0

# ------------------------------------------------------ 3. profile scratch (>24 h idle)
python3 - "$PROFILES_DIR" "$BOARD_DB" "$DRY" <<'PY' >>"$LOG" 2>&1
import os, sys, time, subprocess, re, sqlite3
profiles, board, dry = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
live, live_nums, live_text = set(), set(), []
board_ok = os.path.exists(board)
if board_ok:
    try:
        con = sqlite3.connect(f"file:{board}?mode=ro", uri=True, timeout=20)
        cur = con.cursor()
        for tid, title, body, result in cur.execute(
                "select id,title,coalesce(body,''),coalesce(result,'') from tasks "
                "where status not in ('done','archived')"):
            live.add(tid)
            live_text.append(title + "\n" + body + "\n" + result)
            live_nums |= set(re.findall(r"(?:odile|odl)[^0-9]{0,20}(\d{2,4})(?![0-9a-f]{2})", title, re.I))
        for (body,) in cur.execute(
                "select c.body from task_comments c join tasks t on t.id=c.task_id "
                "where t.status not in ('done','archived')"):
            live_text.append(body or "")
        con.close()
    except Exception as exc:  # fail closed
        board_ok = False
        print(f"board read failed ({exc}); no scratch deletion this run")
live_text = "\n".join(live_text)

refs = set()
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    basep = f"/proc/{pid}"
    for name in ("cwd", "root"):
        try:
            refs.add(os.readlink(f"{basep}/{name}"))
        except OSError:
            pass
    try:
        for fd in os.listdir(f"{basep}/fd"):
            try:
                refs.add(os.readlink(f"{basep}/fd/{fd}"))
            except OSError:
                pass
    except OSError:
        pass

def referenced(p):
    for r in refs:
        if r == p or r.startswith(p + "/"):
            return True
    return False

cut = time.time() - 24 * 3600
freed = 0
def entry_nums(s):
    out = set()
    out |= set(re.findall(r"(?:odile|odl)[^0-9]{0,20}(\d{2,4})(?![0-9a-f]{2})", s, re.I))
    return out
def entry_ids(s):
    return {f"t_{h}" for h in re.findall(r"(?<![0-9a-f])([0-9a-f]{8})(?![0-9a-f])", s, re.I)}
for prof in os.listdir(profiles):
    s = os.path.join(profiles, prof, "cache", "scratch")
    if not os.path.isdir(s):
        continue
    for e in os.listdir(s):
        if not board_ok:
            break
        p = os.path.join(s, e)
        if entry_ids(e) & live or entry_nums(e) & live_nums:
            continue
        if p in live_text or referenced(p):
            continue
        newest = 0
        if os.path.islink(p) or os.path.isfile(p):
            newest = os.lstat(p).st_mtime
        else:
            for root, dirs, files in os.walk(p):
                for n in files:
                    try: newest = max(newest, os.lstat(os.path.join(root, n)).st_mtime)
                    except OSError: pass
                if newest >= cut: break
        if newest < cut:
            try:
                mb = int(subprocess.run(["du", "-sm", p], capture_output=True, text=True).stdout.split("\t")[0])
            except Exception:
                mb = 0
            print(f"{'DRY rm' if dry else 'rm'} scratch {p} ({mb}MB)")
            if not dry:
                subprocess.run(["rm", "-rf", p])
            freed += mb
print(f"scratch reclaimed ~{freed}MB")
PY

# ------------------------------------------------- 4. merged, clean worktrees of done cards
if [ -d "$REPO/.git" ]; then
  python3 - "$REPO" "$BOARD_DB" "$DRY" <<'PY' >>"$LOG" 2>&1
import os, sys, subprocess, re, sqlite3
repo, board, dry = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
live, live_nums = set(), set()
board_ok = os.path.exists(board)
if board_ok:
    try:
        con = sqlite3.connect(f"file:{board}?mode=ro", uri=True, timeout=20)
        cur = con.cursor()
        for tid, title in cur.execute(
                "select id,title from tasks where status not in ('done','archived')"):
            live.add(tid)
            live_nums |= set(re.findall(r"(?:odile|odl)[^0-9]{0,20}(\d{2,4})(?![0-9a-f]{2})", title, re.I))
        con.close()
    except Exception as exc:  # fail closed
        board_ok = False
        print(f"board read failed ({exc}); no worktree removal this run")
def ref_nums(s):
    out = set()
    out |= set(re.findall(r"(?:odile|odl)[^0-9]{0,20}(\d{2,4})(?![0-9a-f]{2})", s, re.I))
    return out
def ref_ids(s):
    return {f"t_{h}" for h in re.findall(r"(?<![0-9a-f])([0-9a-f]{8})(?![0-9a-f])", s, re.I)}
def git(*a, cwd=repo):
    return subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True).stdout
porcelain = git("worktree", "list", "--porcelain")
blocks, cur = [], {}
for line in porcelain.splitlines():
    if line.startswith("worktree "):
        if cur: blocks.append(cur)
        cur = {"path": line[9:]}
    elif line.startswith("branch "):
        cur["branch"] = line[7:]
if cur: blocks.append(cur)
removed = 0
for b in blocks:
    if not board_ok:
        break
    p, br = b["path"], b.get("branch", "")
    if p == repo or not br:
        continue
    if os.path.basename(p) == "gc-hermes-fork":
        continue
    if ref_ids(p) & live:
        continue
    short = br.replace("refs/heads/", "")
    if ref_nums(short) & live_nums:
        continue
    if subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", short, "main"]).returncode != 0:
        continue
    if subprocess.run(["git", "-C", p, "status", "--porcelain"], capture_output=True, text=True).stdout.strip():
        continue
    print(f"{'DRY remove' if dry else 'remove'} worktree {p} [{short}]")
    if not dry:
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", p], capture_output=True)
    removed += 1
if not dry and removed:
    subprocess.run(["git", "-C", repo, "worktree", "prune"], capture_output=True)
print(f"merged worktrees removed: {removed}")
PY
fi

# ------------------------------------------- 4b. weekly session-store prune (30 days)
MARK=/var/lib/hermes-disk-care/last-session-prune
mkdir -p "$(dirname "$MARK")" 2>/dev/null || true
if [ ! -f "$MARK" ] || [ $(( (now - $(stat -c %Y "$MARK" 2>/dev/null || echo 0)) / 86400 )) -ge 7 ]; then
  for p in default $(ls "$PROFILES_DIR" 2>/dev/null); do
    log "session prune: $p (retain 30d)"
    run hermes --profile "$p" sessions prune --older-than 30 --yes >/dev/null 2>&1 || true
  done
  run hermes --board odile kanban gc --event-retention-days 30 --log-retention-days 14 >/dev/null 2>&1 || true
  run touch "$MARK"
fi

# ---------------------------------------------------------------- 5. rebuildable caches
journalctl --vacuum-size=100M >/dev/null 2>&1 || true
apt-get clean >/dev/null 2>&1 || true
for d in /root/.npm /root/.hermes/cache/uv /root/.cache/pip; do
  [ -d "$d" ] || continue
  mb=$(du -sm "$d" 2>/dev/null | cut -f1)
  [ "${mb:-0}" -ge 200 ] || continue
  case "$d" in
    /root/.npm) run npm cache clean --force ;;
    *cache/uv) UVBIN=/root/.hermes/tools/uv-0.13.5-linux-x64/uv; [ -x "$UVBIN" ] && run env UV_CACHE_DIR="$d" "$UVBIN" cache prune ;;
    *pip) run /usr/bin/python3 -m pip cache purge ;;
  esac
done
run podman image prune -f >/dev/null 2>&1 || true

# ---------------------------------------------------------------- 6. old text logs
find /root/.hermes/logs "$PROFILES_DIR"/*/logs -type f -mtime +14 -delete 2>/dev/null || true

after=$(df -Pm / | awk 'NR==2{print $4}')
used_after=$(df -P / | awk 'NR==2{gsub("%","",$5); print $5}')
log "=== done: free ${before}MB -> ${after}MB (reclaimed $((after-before))MB, used ${used_after}%) ==="

# one alert per 12 h when the sweep still leaves the volume above 90 %.
# The target is one Matrix room id in host state, never a literal in the repo:
# alerting is host config, and a room id is high-entropy enough for the leak
# scanner to treat it as a token. A rebuilt host restores the file with the rest
# of /root/.odile/, or runs without the alert and logs that it did.
ALERT_MARK=/var/lib/hermes-disk-care/last-alert
ALERT_ROOM_FILE=${ALERT_ROOM_FILE:-/root/.odile/disk-care-alert-room}
if [ "$used_after" -ge 90 ] && [ "$DRY" = 0 ]; then
  if [ ! -f "$ALERT_MARK" ] || [ $(( (now - $(stat -c %Y "$ALERT_MARK" 2>/dev/null || echo 0)) / 3600 )) -ge 12 ]; then
    top=$(du -xhd1 /var/tmp 2>/dev/null | sort -hr | sed -n '2p' | tr -s ' \t' ' ')
    msg="Disk: ${used_after}% used, $((after/1024))G free after disk care. Largest ${top}. Live test roots are held by their cards; see docs/ops/DISK-CARE.md."
    room=$(tr -d '[:space:]' <"$ALERT_ROOM_FILE" 2>/dev/null)
    if [ -n "$room" ]; then
      hermes --profile chief-of-staff send -t "matrix:$room" "$msg" >>"$LOG" 2>&1 || log "alert send failed"
      touch "$ALERT_MARK"
    else
      log "alert target $ALERT_ROOM_FILE is missing or empty; no Matrix alert sent"
    fi
  fi
fi
