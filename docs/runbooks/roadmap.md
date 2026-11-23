# Roadmap page — the plan and the board on one unlisted URL

**Status:** v1, built by ODL-490 and re-cut by ODL-494 · **URL:** `https://odile.fyi/roadmap` · **Generator:** `tools/roadmap_page.py`
**Contract:** `docs/product/ROADMAP-PAGE-CEO.md` (the CEO overview; read it before changing the page)
**Sources:** `docs/backlog.yaml` (milestones and cards) and the `odile` board database (execution state)

One screen Savio can open from anywhere: where the project is, whether the
gate in question is held, what comes next and what needs him — then every gate
one click away. It is generated, not written, so it cannot drift from either
source. It is **unlisted**: `noindex`, no link from any other page, no sitemap
entry, no promotion. It is not a public commitment page.

```
docs/backlog.yaml ─┐
                   ├─▶ tools/roadmap_page.py ─▶ /var/lib/odile/roadmap.html ─▶ GET /roadmap
board kanban.db ───┘        (every 15 min)          (atomic write)            (no-store, 503 if absent)
```

## 1. What the page shows

The order is the contract: status sentence, **Now**, **What needs you**,
**Gates**, the note, one `<details>` per gate (the focus gate is open), the
**ADR view** A4 links to, and **How this page is made** (closed).

- **The status sentence** — one computed sentence naming the open gate(s), the
  days to the next close, the next gate's start date, and the plan-wide count
  of cards in flight and blocked. Four facts, one statement of position.
- **Now** — the focus gate: its `{id} · {name}`, a `held` tag when an agent
  card is blocked, its when phrase (`Closes …` / `Opens …` / `Closed …`) with
  `{done} of {total} card(s) done · {f} in flight · {b} blocked`, and the M1
  go/no-go line while that date is still ahead. The focus gate is defined in
  every state: the open gate with the most unfinished cards, the next gate in a
  gap, the last gate after the final window.
- **What needs you** — `Nothing.`, or up to three human cards whose
  dependencies are done. A plan card is named by its backlog id; a card that
  exists only on the board is named by its task id. The rule is
  `tools/board_sync.py:is_human_card` plus the plan's `lane: human`, so the
  page and the dispatcher agree on whose card it is.
- **Gates** — seven rows in window order: id, name, dates, `done/total`, a
  CSS-only bar, the `now` marker on the open window(s), and `held` where an
  agent card is blocked. The bar and the count carry the same fact twice, so
  the visual survives a grayscale screenshot.
- **The note** — `Counts are counts, not verdicts: a criterion is judged met in
  the Monday gate packet, not here.`
- **One `<details>` per gate** — id, name, dates and `done/total` in the
  `<summary>`; objective, gate question, demo and every exit criterion inside,
  verbatim from `docs/backlog.yaml`, then the gate's `open` questions as a
  final list when the milestone defines any. Exactly one is open: the focus
  gate's.
- **The ADR view** — A4's block carries one in-page link whose label names
  ADR-005, and the linked section renders that ADR's §3 layer table (all 15
  rows, each with its label and the computed `7 in place · 5
  rebuild-and-restore · 3 not updatable` line), §4's chosen model route (the
  §4.1 prompt-path row and §4.3's prose) and §5's trigger (the one-line answer
  and the four row groups). The text is read from
  `docs/architecture/ADR-upgrades-and-model.md` on every render, so a changed
  ADR changes the view; a lost row, an unknown label or a moved heading stops
  the generator instead of shortening the page. The inventory is a real table
  in the DOM; below 760 px each row stacks (label, today, reason) and nothing
  scrolls sideways. No second route, no hand-written copy: the ADR is the one
  source, and `--adr` names it.
- **How this page is made** — the counting and redaction rules, in six
  sentences, behind a closed disclosure.
- **The footer** — `generated <ISO-8601 UTC> · source <short sha> · odile.fyi`.

Counts are read from `tasks` in the board database. A card belongs to a
backlog entry only when the **first line** of its body is a bare
`Backlog-ID: ODL-nnn` marker (the marker `tools/board_sync.py` writes) and that
id is a task in `docs/backlog.yaml`. A follow-up or a human escalation that
quotes the same ID with a suffix is not that entry's card, and an archived card
is off the plan. `running` and `review` are both in flight. `blocked` counts
**agent cards only**: a card in the human lane is never counted as in flight or
blocked, because a card waiting for a person is not a stall; it appears under
**What needs you** when its dependencies are done. The page counts cards, not
backlog entries, and it prints one state word: `held`.

## 2. The price rule

The landing page states no price, so this page must not be the first public
price sentence. M4's exit criterion in `docs/backlog.yaml` reads "Ten
subscriptions active at $49." and the page renders **"Ten subscriptions active
at the founding price."**

The map is `REDACTIONS` in `tools/roadmap_page.py`, keyed by the exact
sentence, and generation **fails** if any `$` followed by a digit survives
into the output. A new price in the backlog is therefore a loud error, not a
silent leak: add the sentence to `REDACTIONS`, or the timer stops and the page
goes stale. `tests/test_roadmap_page.py` pins the rendered page against
`\$[0-9]`.

A board card's `title` is a sentence on the page too, under **What needs you**
(§1). Those titles are redacted one by one before the line is joined, so a
title's key is the exact board `title` — not the joined needs line, which
changes whenever a card enters or leaves the three-item queue. A priced card
that reached the page without an entry (measured 2026-11-13: `$10.18` in a
human card's title) stops the timer exactly as a backlog price does; the error
names the `$`-and-digit fragment, so grep the board's titles, not only
`docs/backlog.yaml`.

## 3. Regenerate by hand

```sh
cd /opt/odile
sudo /usr/bin/python3 tools/roadmap_page.py --stdout --now "$(date -u +%FT%TZ)" | head -40
sudo /usr/bin/python3 tools/roadmap_page.py            # write /var/lib/odile/roadmap.html
sudo systemctl status odile-roadmap.service --no-pager # the last timer run
```

- `--backlog` — the milestone file (default `docs/backlog.yaml` beside the tool).
- `--board-db` — the board's SQLite file. Default: `HERMES_KANBAN_DB`, else
  `~/.hermes/kanban/boards/odile/kanban.db`.
- `--out` — where to write (default `/var/lib/odile/roadmap.html`).
- `--stdout` — print, write nothing (the dry run).
- `--now` — an ISO-8601 instant; the Now block is then deterministic.
- `--adr` — the document the A4 view renders (default:
  `docs/architecture/ADR-upgrades-and-model.md` beside the tool). The section
  is read on every run; `--adr` exists so a test can hand it a changed copy.
- `--source-sha` / `--source-file` — what the footer prints; the default file
  is `/var/lib/odile/roadmap-source`, written by `./scripts/deploy.sh` from the
  checkout it deployed (`/opt/odile` has no `.git`).

The write is atomic: a temporary file in the same directory, `fsync`, then
`os.replace`. A failed run leaves the previous page in place, and the timer
reports the unit as failed.

## 4. Serve and deploy

`app.py` owns the route: `GET /roadmap` reads `/var/lib/odile/roadmap.html` on
every request and sends it `Content-Type: text/html; charset=utf-8` with
`Cache-Control: no-store`. If the file is absent it answers `503` with the one
line `roadmap not generated yet` as `text/plain`. No Caddy change is involved:
the request falls through to the app's existing catch-all.

```sh
cd <checkout>
./scripts/deploy.sh -n    # prints the roadmap would-lines, changes nothing
./scripts/deploy.sh       # installs the units, records the source sha, runs the generator once
```

`/opt/odile/tools/roadmap_page.py` is an HQ-only file (`.git/info/exclude`), so
the deployed tree runs the **HQ checkout's** copy: the rsync excludes it and
`scripts/deploy.sh` installs HQ's bytes afterwards. A card worktree's stale copy
never lands — the rule is in [`deploy.md`](deploy.md) §2, "Which copy of a
host-local tool the deployed tree runs". `./scripts/check_hq_tools.sh` is the
live proof (deploy.md §4): the installed generator is HQ's byte for byte, and
the page's footer instant follows its install.

The units are committed under `deploy/roadmap/`:

- `odile-roadmap.service` — oneshot, root, `ProtectSystem=strict`,
  `ProtectHome=read-only`, `ReadWritePaths=/var/lib/odile` and the board's
  directory (`/root/.hermes/kanban/boards/odile`), because a `mode=ro` open of
  a WAL database still writes the `-shm` index beside it.
- `odile-roadmap.timer` — `OnCalendar=*:0/15`, `Persistent=true`.

## 5. Check it

```sh
curl -sS -D - -o /tmp/roadmap.html https://odile.fyi/roadmap | head -12
grep -c '<li class="gate' /tmp/roadmap.html             # one per milestone (7 today)
grep -c '<details' /tmp/roadmap.html                    # 7 gates + the provenance note (8)
grep -o '<details[^>]* open' /tmp/roadmap.html | wc -l  # exactly 1: the focus gate
grep -c 'class="criteria"' /tmp/roadmap.html            # one list per gate (7)
grep -F 'generated ' /tmp/roadmap.html                  # the footer line
sha256sum /tmp/roadmap.html
systemctl status odile-roadmap.timer --no-pager
systemctl status odile-roadmap.service --no-pager | head -12
```

The page must carry `noindex`, no `<script>`, no cookie, no third-party
request, and no link to itself. It must also carry one ADR view: A4's in-page
link, 15 row labels, and the three §3/§4/§5 headings.

```sh
grep -c '<section class="adr"' /tmp/roadmap.html        # 1: the view, not a second page
grep -c 'class="adr-label ' /tmp/roadmap.html           # 15 row labels
grep -F '7 in place · 5 rebuild-and-restore · 3 not updatable' /tmp/roadmap.html
grep -o '<h3 id="adr-[a-z]*-title">[^<]*</h3>' /tmp/roadmap.html  # the three headings
grep -o '<h4 class="adr-group">[^<]*' /tmp/roadmap.html            # the four §5 groups
grep -F 'href="#adr-upgrades-and-model"' /tmp/roadmap.html        # A4's link
```

`make check` runs `tests/test_roadmap_page.py`, which pins the disclosure
structure, the single open gate, the first-screen blocks, all three invariants
and the ADR view (the 15 row labels, the 7 / 5 / 3 counts, the three headings,
the A4 link, and a lost row stopping the render). The visual review is at
1440×900 and 390×844, plus a grayscale screenshot for the `held` tag. In the
ADR inventory the five cells are one row at every width: from 760 px up the
four short cells sit on one line and "Why, and the path" takes the full table
width beneath them, with a 164 px label column that holds the
`rebuild-and-restore` pill on one line; below 760 px the same five cells stack
with a `data-label` line each. Neither the page nor the table scrolls
horizontally at 320–1440.

## 6. When something is wrong

| Symptom | Cause | Fix |
|---|---|---|
| `503 roadmap not generated yet` | the state file is missing | `sudo systemctl start odile-roadmap.service`; read its log |
| The page is stale | the timer did not run or the unit failed | `systemctl status odile-roadmap.timer`; `journalctl -u odile-roadmap -n 40` |
| `roadmap: board db …: no such file` | the board moved or `HERMES_KANBAN_DB` is unset in the unit's environment | name the file with `--board-db` in the unit |
| `roadmap: a price reached the page` | a `$`-and-digit sentence in the backlog or in a board card `title` is not in `REDACTIONS` | add it to the map — a title is keyed by its exact board `title`, redacted before the needs line is joined; the page stays at the previous generation until then |
| `roadmap: milestones[3]: unknown key 'owner'` | a field was added to `docs/backlog.yaml` that the tool does not render | add it to `MILESTONE_KEYS` and to the template |

## 7. Rollback

```sh
sudo systemctl disable --now odile-roadmap.timer
sudo rm -f /var/lib/odile/roadmap.html      # /roadmap now answers one line of 503
```

The route stays: it is a few lines in `app.py`, it reads no database and it
spends nothing. To remove the page itself, revert the card and redeploy.

## 8. Why it is unlisted, not private

Anyone with the URL can read it. The `noindex` meta and the absence of any
link keep it out of a search index and out of every visitor's path; the URL is
the only way in, and it is not a secret. Do not link it from `web/`, `app.py`,
`README.md`, or any other document — `tests/test_roadmap_page.py` fails when a
new link appears. This runbook is the one place it is named on purpose.
