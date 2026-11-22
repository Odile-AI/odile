# The four recipes

**Status:** A2 · **Card:** ODL-234 (the recipes), ODL-235 (the sender), ODL-469 (the tenant
wiring) · **Tested:** a fresh scratch tenant, 22 November 2026 (`docs/evidence/odl-469/`,
`docs/evidence/odl-234/`).

Four things Odile does on day one without an API key, an OAuth app or an
integration. Each one is a skill recipe with a tested prompt; two have a cron
template. The agent does the work on the machine and leaves a file the user can
open in the file browser.

| Recipe | They ask | It lands in | Runs |
|---|---|---|---|
| Morning brief | "run my brief", "set up my brief" | `~/briefs/YYYY-MM-DD.md`, and the user's inbox when an address is on file | On demand, and daily at the time they chose |
| Silent watchdog | "watch this page", "tell me when it changes" | `~/.hermes/watch/<slug>.json`, `~/.hermes/scripts/watch-<slug>.py`, `~/workspace/watchdog/<slug>.md` | A no-agent cron job; silent until the value changes |
| Research memo | "research X", "look into X" | `~/workspace/memos/YYYY-MM-DD-<slug>.md` | On demand |
| CSV chart | "analyse this CSV", "chart this file" | `~/workspace/analysis/<stem>-report.md` and `<stem>-chart.svg` | On demand |

The skill is `skills/odile-recipes/SKILL.md`. The installer carries it in the
signed tenant bundle, so a fresh machine has it before the first chat; the
standing context in `templates/tenant-agents.md` points at it.

## Morning brief

- **Ask:** "run my brief", "what's my day", "set up my brief", "make my brief
  08:30", "pause my brief".
- **What it does:** reads the name, time zone, city and three things from
  memory, writes today's date in the user's zone, one line of weather from
  `wttr.in`, one short linked item per thing via web search, and one sentence
  on what it will watch tomorrow. Under 200 words.
- **File:** `~/briefs/YYYY-MM-DD.md`. The first-run ritual writes the first
  one; this recipe regenerates it or repairs the schedule.
- **Email:** the job's last step is the installed sender —
  `sudo -n /usr/bin/python3 ~/.hermes/scripts/send_brief.py` — which composes
  the message from `~/.hermes/brief.txt`, sends it through Odile's relay as
  `brief-<slug>@mail.odile.fyi`, and records the outcome. The address lives in
  `~/.hermes/brief.json` (`{"to": "…"}`, 0600, written at first run or when
  the user turns email on later); with no file the send exits 2 and the brief
  stays in `~/briefs/`. The prompt tells the agent to report the sender's one
  line, never to write its own mail code.
- **Schedule:** one job named `Morning brief`, created from
  `templates/cron/morning-brief.json` with the user's IANA zone, the local
  time, `deliver: local` and the toolsets `web` and `terminal`. The prompt must
  carry the name, zone, city and three things because the job runs in a fresh
  session.
- **One job, not two:** the recipe edits the existing `Morning brief` job; it
  never creates a second one.
- **Failure:** a failed lookup becomes one honest line in the brief; the file
  is still written. A failed cron create is reported as "the file is there, the
  schedule needs one more try", never as a schedule that does not exist.
- **Not email:** the brief lands in `~/briefs/` even when the send fails; the
  inbox is the sender's half, and a failed send is recorded where the heartbeat
  reads it rather than retried silently.

## Silent watchdog

- **Ask:** "watch https://example.com/pricing and tell me when the price
  changes", "check this page every hour", "pause my watchdog", "stop watching
  it".
- **Shape:** Hermes's no-agent cron pattern. The script is the whole job: empty
  stdout sends nothing, non-empty stdout is delivered verbatim, and a broken
  script raises the scheduler's error alert. No model runs on a tick, so a
  watchdog costs no inference.
- **Files:** the generic script is `templates/cron/watch.py`, copied to
  `~/.hermes/scripts/watch-<slug>.py`. One config per watch at
  `~/.hermes/watch/<slug>.json`; the state file sits beside it at
  `~/.hermes/watch/<slug>.state.json`; each alert is appended to
  `~/workspace/watchdog/<slug>.md`.
- **State:** the baseline is the first successful read. A read identical to the
  stored value prints nothing. A changed read prints one alert and stores the
  new value, so one change is one alert. A version change in the state file is
  a fresh baseline, not a burst of alerts.
- **Failures:** a failed fetch is not a change. Failures are counted; the third
  consecutive failure prints one alert and the script stays silent until the
  source recovers. Recovery resets the counter.
- **Schedule:** `every 6h` unless the user asks for a frequency. The job is
  `Watch: <label>`, `no_agent: true`, `script: watch-<slug>.py`, `deliver:
  local`.
- **Alerts:** each alert is appended to `~/workspace/watchdog/<slug>.md` and
  kept in the job's run output; both are visible in the file browser and the
  cron panel. A watchdog has no email of its own; the brief's sender is the one
  thing that writes to an inbox. `deliver: origin` is deliberately not
  used: the Webapp/API session is stateless, and a delivery that cannot resolve
  marks a correct alert as a failed run.
- **Test before scheduling:** `--print-value` shows the compared value;
  `--debug` twice shows `baseline` then `unchanged`; `--test-change` shows the
  alert shape and writes nothing.
- **Job template:** `templates/cron/watchdog.json`.

## Research memo

- **Ask:** "research the EU AI Act's GPAI obligations", "look into X", "write
  me a memo on Y".
- **What it does:** two or three searches, extraction of the two or three best
  sources, a memo in markdown with the question, linked findings, what could
  not be verified, and the source list. Under 600 words.
- **Rule:** every factual bullet carries a link; a number appears only if a
  source states it; two disagreeing sources are both named.
- **File:** `~/workspace/memos/YYYY-MM-DD-<slug>.md`. The chat reply is a
  three-sentence summary plus the path.
- **On demand only:** scheduled research is not in V0.1.

## CSV chart

- **Ask:** "analyse ~/workspace/sales.csv", "chart this file", "what's in this
  spreadsheet".
- **What it does:** reads the CSV with the Python standard library on the
  machine, reports rows, columns, types, missing values and three to five
  findings, then draws one SVG chart in the brand palette.
- **Files:** `~/workspace/analysis/<stem>-report.md` and
  `<stem>-chart.svg`. The file browser previews the SVG as an image.
- **Chart choice:** a text column and a number column become the top eight
  categories by that number; a text column alone becomes value counts; numbers
  alone become an eight-bin histogram. One chart, sorted, labelled, titled with
  both columns.
- **Limits:** uploads are capped at 16 MiB by the Webapp; the script counts in
  a stream rather than loading a large file whole. Missing values are stated,
  not filled in. The recipe never invents a row or a number.
- **On demand only:** scheduled reports are not in V0.1.

## What the recipes do not do

| Not in V0.1 | Why | Earliest |
|---|---|---|
| HTML email or attachments | The brief is plain text: the relay sees the message, and nothing renders on the far side | V0.2 |
| Integrations (Gmail, Calendar, Drive) | The computer is the integration; OAuth is V0.2 | V0.2 |
| A model picker or BYO key | One curated model, straight to NEAR AI | V0.2 |
| Scheduled research or scheduled charts | On-demand is the job; a schedule is a feature the user has to want | V0.3 |
| A watchdog that watches a logged-in page | No stored browser session; a public URL or a script the user owns | V0.2 |
| Multiple charts per CSV | One clear chart beats four crowded ones | V0.3 |

## Supporting a recipe

- **The brief did not arrive.** Check the job in `cronjob_manage` (enabled,
  timezone, next run), then `ls -l ~/briefs/`. If the machine was locked at the
  scheduled time, the job runs on unlock; a missed brief is the
  "waiting for owner" signal. When the file is there but the inbox is empty,
  run the sender by hand (`sudo -n /usr/bin/python3
  ~/.hermes/scripts/send_brief.py`) and read its one line; the record in
  `/var/lib/odile/brief.json` carries the last error and travels in the signed
  heartbeat.
- **The watchdog is silent.** That is correct when nothing changed. Run
  `python3 ~/.hermes/scripts/watch-<slug>.py --debug`; the state file shows the
  last value, the failure count and the last check. If `--print-value` fails,
  the page changed shape and the match needs repair.
- **The chart is empty.** Check the path the user named, the header row, and
  the row count in the report. A CSV with one column still charts as value
  counts; a file with no rows writes a report and says so.
- **The memo is thin.** The "What I could not verify" section is the honest
  part; do not pad it with unlinked claims.

## Evidence

`docs/evidence/odl-234/` holds the four run transcripts, the watchdog tick
transcripts and a README with the exact commands. `tests/test_recipes.py` pins
the skill structure, both cron templates, the watchdog state machine and the
installer's embedded bundle; `make check` runs it.
