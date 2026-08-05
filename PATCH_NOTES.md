# JobWatch — Patch Notes

Newest first. Each entry records what changed, why, and anything the owner needs
to do by hand after updating.

---

## v1.2.0 — 2026-08-05

A broken connector diagnosed and fixed, the auto-ghost rule loosened and made
adjustable, and a new engine feature: every role now carries the date JobWatch
first saw it.

**Test suite: 20 files, ~565 assertions, all green.**

### Fixed: a Workday board had gone unreachable for a week

A tracked company stopped returning roles. The careers page loaded fine in a
browser, which is what made it confusing — and is exactly the trap. Workday
serves the human page at `<tenant>.<pod>.myworkdayjobs.com` but the job list
comes from a JSON endpoint under `/wday/cxs/`, and JobWatch calls that endpoint
directly using the host stored when the company was added. **Workday had
migrated the tenant to a different pod.** The browser followed the redirect; a
stored hostname doesn't get that courtesy.

Nothing was corrupted, because soft-fail did its job: a company that can't be
fetched writes no snapshot and is flagged, so the week of silence never became a
false "everything removed".

The fix was one field in `companies.json` — the connector, the tenant and the
site name were all still correct. **Worth doing in place rather than by deleting
and re-adding the company:** snapshots, trend rows and application records are
all keyed on `company_key`, so a re-add that derives a different key orphans the
history and makes the next check read as a fresh baseline.

Two process points, both now in the playbook's spirit: **a working careers page
is not a working connector**, and per the S32 lesson, most "connector" bugs turn
out to be in the stored config rather than the connector.

### Auto-ghost: 14 days → 21, and now a setting

Two weeks of silence was closing applications that were still alive. The
threshold moved to 21 days and, more usefully, stopped being a constant:
`settings.ghost_after_days` is adjustable from the Settings screen, sitting
below the dormancy threshold.

**The two thresholds are independent and share nothing but a default of 21.**
One is a whole phase going unchecked; the other is one employer going quiet.
They now sit inches apart in the settings store and on the Settings screen,
which makes them easy to conflate, so the separation is stated in `settings.py`,
`applications.py`, `app.js` and `DATA_FORMATS.md`, and `test_auto_ghost_threshold`
proves that moving one does not move the other.

**Raising the threshold does NOT un-ghost rows already flipped.** The auto-ghost
overwrites `status` and keeps no record of what it replaced, so a row that was
at `screening` when it ghosted is afterwards indistinguishable from one that was
at `applied`. Correcting the affected rows was a one-off hand-written script, and
the old statuses had to be reconstructed by hand — one of them could not be
recovered from the data at all. The limitation is now documented at the point of
the flip rather than being rediscovered.

A related trap, recorded because it is easy to trip: **a correction cannot go
through `update_status`.** `ghosted` is terminal and `_is_legal_transition`
rightly refuses terminal → live. Any un-ghosting is a direct write to the file;
the ladder must not be weakened to make a backfill convenient.

The Ghost rate card now reads its number from the server rather than a literal,
so the label follows the setting. The **"Last 14 days"** card is a different
fourteen — applications *submitted* in the last fortnight — and deliberately
stays at 14; it is commented as such, because it looks like an oversight.

### New: every role carries the date JobWatch first saw it

Under the location line on every role, in the Jobs tab, Saved jobs and the
Application Tracker: the exact date, with the relative age quietly beneath it.

**What the date means, precisely.** It is the start of the role's current
unbroken run of sightings in *your* checks. It is **not** the employer's posting
date, and it differs from one in three ways that matter:

- Snapshots store the **shown** (location-filtered) list, not the raw fetch, so
  a role that existed outside your filter has no record until the day it matched
  — or the day the filter widened.
- **Adding a company backdates nothing.** Every role already live when a company
  was added reads as first seen on that first check.
- **The clock restarts on re-appearance.** If a job id leaves the snapshots and
  later returns, the date moves to the return. The common causes of a gap are a
  filter change or the pre-fix Avature page drift, not a genuine re-listing.

Rather than caveat every affected row, the Jobs two-pane header shows the
company's **added-on** date once, which tells you at a glance which company's
dates are floors rather than facts. Where no date exists at all — a manually
added tracker row never came through a check — it reads **"not recorded"**, the
same honest-fallback contract as *location unclear*.

**Implemented as an index, not a scan.** The answer is derivable from the
snapshot tree, but that tree grows with every check and the screens re-render in
place on every filter chip, sort and drag, so a scan behind each click would get
slower every week. `first_seen.json` holds `{since, last_seen, bounded}` per
role; the orchestrator folds each check in at the same point it records trends,
with the same non-critical swallow. Snapshots remain the source of truth and
`first_seen.rebuild_from_snapshots()` regenerates the index from them at any
time.

One decision worth knowing: **a dormant lapse does not restart a role's clock.**
A clean baseline reports everything as new, but the role never went anywhere —
the hunt stopped looking. The live recorder carries entries through. The
historical backfill cannot make that distinction, because after the fact a lapse
is indistinguishable from any other gap, so roles that spanned a lapsed phase
have their clock restarted in the backfilled data only.

**The board's own posting date — the honest answer to "how long has this been on
the market" — is deferred to its own build.** It will arrive as a separate
`posted_on` field, which is why this one is called `first_seen` and not
`date_added`.

### Saved jobs: removed roles are struck through

A saved role that has since come off the board now renders exactly like a
removed role in a run report — strikethrough, `--removed` colour, and an
explicit "no longer listed" label. The label matters: a strikethrough alone
reads as *done* rather than *gone*, and it is the only cue a screen reader
would otherwise get.

The cross-reference the tracker has used since L.5 was factored into one shared
helper so both screens compute "gone" identically. It stays deliberately
conservative: the flag is only asserted when there **is** a snapshot to check
against and the id is genuinely absent, so a company that couldn't be reached
can never make every saved role look dead.

### New: download the tracker as markdown

A **Download as markdown** button in the Application Tracker header, for handing
the record to an AI agent to review.

The file leads with a summary — counts by status, the funnel, the engagement
rate — then a section per role with dates, stage, rounds, whether it is still
listed, the link, and your notes. Notes are stored as a restricted HTML subset
and are converted properly: bold and italic survive, bullets become markdown
lists, and underline is dropped rather than faked with underscores, which
markdown would render as italics and quietly change the meaning.

The document **explains its own terms**, which is the point of it: what
"employer engaged" counts, that ghosting is automatic silence rather than an
explicit rejection, and specifically that first-seen is not a posting date. An
agent handed a bare date will assume the role went up that day and reason
wrongly about how fast you applied.

Every number comes from the same helpers the tracker screen uses, so the export
cannot disagree with what is on screen. Served with `Content-Disposition:
attachment` from a plain `<a download>`, so the formatting lives in one place
rather than being duplicated in JavaScript.

### Test suite

Two new files: `test_auto_ghost_threshold` (the threshold, its settings wiring,
its independence from dormancy, and the destructive-flip property pinned as a
known limitation) and `test_first_seen` (continuity vs re-appearance, the
dormant carry-through, bounded dates, the unclear fallback, and the rebuild with
the filesystem seams stubbed).

**Neither writes anything.** Both use injectable parameters or stubbed
dependencies throughout — calling the real `settings.set_ghost_after_days()` or
`rebuild_from_snapshots()` in a test would edit live data, which is the trap
that made `test_trends_london_boroughs` fail on its second-ever run.

`test_first_seen` caught a real bug while being written: a corrupt index
carrying `"companies": null` crashed `for_company` instead of degrading to "not
recorded" — `data.get("companies", {})` doesn't help when the key exists with a
null value.

### An observation worth recording

The first-seen backfill surfaced two companies returning **zero roles across
every check ever run** — 30 checks and 10 checks respectively. Investigated and
confirmed genuine: those boards really have had nothing open in the tracked
locations. Worth noting because a permanently empty board and a silently broken
connector look identical from the outside, and the count alone doesn't tell you
which you have.

It also showed four companies with **zero bounded roles**, meaning their
first-ever snapshot held no jobs at all. Those are the boards affected by the
2026-07-07 location-classifier bug, which dropped country-remote, site-labelled
and semicolon multi-location roles to `elsewhere`. Their first-seen dates are
therefore the date the filter fix landed, not the date the roles went up.

---

## Actions after updating — v1.2.0

1. **Hard-refresh the browser** (⌘⇧R) — `app.js` and `app.css` both changed.
2. **Build the first-seen index**, once:
   ```bash
   python3 -c "from jobwatch import first_seen; d = first_seen.rebuild_from_snapshots(); print(sum(len(v) for v in d['companies'].values()), 'roles indexed')"
   ```
   Until this runs, every role reads *"First seen: not recorded"*. The same
   command is the repair path if the index ever drifts — it reads only
   snapshots, so it is always safe to re-run.
3. **Check the Settings screen** for the new *Auto-ghost threshold* control
   below Dormancy, and confirm it reads 21.
4. **Expect a few applications to re-ghost within the week.** Rows corrected
   from the old 14-day rule kept their real `last_progress_at` — the clock was
   not reset, because that would invent progress that never happened. They go
   quiet again as they cross 21 days.

---

## v1.1.0 — 2026-07-25

Four connectors built (three of them PLATFORM connectors covering any employer on
that ATS), two data bugs fixed, and the trends chart rebuilt. Two regression
guards that had been silently dead were repaired.

**Test suite: 17 files, ~500 assertions, all green.**

> **Note on scope.** Companies are not part of this repo — they live in
> `companies.json` in the external data folder, per the two-folder rule. This
> release adds no companies. It adds connector support for the platforms below,
> which is what makes boards on those platforms addable.

### Platform coverage added

| Platform | Connector | Kind |
|---|---|---|
| Avature (`*.avature.net`) | `avature` | new — platform |
| SAP SuccessFactors (Recruiting Marketing / Career Site Builder) | `successfactors` | new — platform |
| Revolut People (`revolutpeople.com`) | `revolutpeople` | new — platform |
| — (a custom, single-employer board) | `cursor` | new — single board |

Greenhouse, Ashby and SmartRecruiters were already supported and needed no work.
One board investigated this cycle looked bespoke — a custom front end sitting
behind a Cloudflare challenge — but turned out to be a CMS skin over
SmartRecruiters, so it needed no connector at all. Worth remembering as a
process point: **identify the ATS before reading anyone's markup.** Building
against that front end would have produced a connector that passed every test and
then failed live for ever, because the challenge page blocks the runtime fetch,
not just the capture.

### New connectors

**`cursor`** — a single-employer custom board, server-rendered. Notable because
this employer previously ran a hosted Ashby board and switched it off, which is a
reminder that a board disappearing may mean the employer moved rather than that a
slug is wrong. The page ships the same list **twice** (desktop + mobile markup),
so a raw parse returns double the real count; `_merge_by_id` collapses it. Department and
location are read structurally — the employment-type token is dropped **by
value**, so a role with no department can't shift the location into the wrong
field.

**`avature`** — any `*.avature.net` portal. Server-rendered and easy to parse,
but Avature ignores both `jobRecordsPerPage` and `sortBy`, and **its result order
drifts between requests**: adjacent pages re-serve roles. On a ~430-role board a
straight full-page crawl collected about 95% of them, with a different ~5%
missing each run. That is worse than missing data — compare would report those
roles removed, then added again next check. Fixed by paging in **half-steps** so
consecutive windows overlap and absorb the drift, plus a completeness guard.
Costs roughly double the requests, so a large portal takes a couple of minutes
per check.

**`successfactors`** — any SAP Recruiting Marketing / Career Site Builder site.
Server-rendered results table, `?startrow=N`, sort pinned to `referencedate desc`.
**Scopes to the site id in the pasted URL**, which matters more than it sounds:
many installs are shared by an entire corporate group, one site id per member
company. An unscoped URL returns the whole group's roles and silently tracks the
parent rather than the employer you meant. Completeness guard with a half-step
retry before it gives up.

**`revolutpeople`** — any `revolutpeople.com/<tenant>/public/careers` board.
Public JSON API, no auth or cookie required. The `function` field is `null` on
real roles;
the API returns no job link, so the URL is rebuilt as
`/position/<title-slug>-<uuid>` from a slug rule pinned against a confirmed live
link.

### Fixed: trend charts were double-counting

`trends.record_snapshot_trends` appends a fresh row set on every run and never
upserts, so checking one company twice in a day wrote two rows for the same
`(company, phase, department, location_bucket, date)`. Because `open_count` is an
absolute count, the read side — which sums a date's rows, correctly, for one
series spanning several location buckets — doubled it.

Measured against a real trend log before the fix: roughly **a fifth of all rows
were duplicates, inflating 6 of 19 recorded dates, two of them tripled.** Not a
uniform doubling that could be mentally divided out, but phantom hiring spikes on
exactly the days a check happened to be re-run.

Fixed on the **read** side (`server._collapse_duplicate_trend_rows`), so the
engine is untouched and rows already on disk are corrected as they're read — no
migration. `open_count` takes the last row for a cell (absolute: the latest write
is current truth); `added_count` / `removed_count` are summed (deltas: two checks
in a day genuinely added both).

Also removed a dead duplicate `_trends_state` definition that was shadowed by the
real one — a trap for anyone editing the wrong copy.

### Fixed: the response rate was wrong

It counted **every** rejection as a response, including auto-rejections that never
reached a human, and counted `withdrawn` too — which is the applicant's own
action, not a reply.

It now asks one question: did the **employer** engage?
`rejected_before_interview` is an explicit no and overrides everything;
`screening` / `interview` / `offer` / `rejected_after_interview` are an explicit
yes; everything else — `applied`, `ghosted`, `withdrawn`, and legacy `rejected` —
is judged by the engagement markers (`screening_interview`, `interview_rounds`).
So withdrawing after two interview rounds counts while withdrawing before anyone
called doesn't, and being ghosted after a screening counts while silence doesn't.

On ten applications where eight were auto-rejected, one ghosted in silence and one
rejected after interviewing, the old formula reported ~90%. It now reports 10%.

### Application tracker: rejections carry their stage

`rejected` is split into **`rejected_before_interview`** and
**`rejected_after_interview`**, because "the market isn't biting" and "I get in
the room but don't close" are different problems and the old data couldn't tell
them apart. Two new rates are exposed alongside the existing ones.

**Backward compatible — nothing is migrated.** Rows written before the split keep
their bare `rejected`, which stays valid for ever, reads as "stage unknown", and
renders as *"Rejected (stage not recorded)"*. It can be corrected to either staged
value but is never offered as a new choice, so it can only shrink over time.

### Trends chart rebuilt

Four things were wrong, all now fixed:

- **It didn't fill its panel.** The SVG sized itself to its *data* and pinned
  left, so five check-dates drew a ~560px chart in a 2000px box. It now measures
  its container and lays out across it, with a `ResizeObserver` redrawing on
  resize.
- **Dots stacked vertically.** The x axis was categorical, so in weekly mode every
  check in a week was drawn at that week's single x — several readings piled onto
  one column, reading as conflicting values for the same week. The axis is now
  time-proportional, which makes stacking structurally impossible. Verified: 11
  checks across 3 weeks now produce 11 dots at 11 distinct positions.
- **Weekly was a guess.** The chart silently switched to weeks at the 13th check
  date, then capped at the most recent 12 weeks, quietly hiding older history.
  Both are gone, replaced by an explicit **Day / Week** toggle. Day joins every
  check; Week keeps the same per-day dots and draws a trend line through one
  anchor per week — that week's last check, so the line ends on the most recent
  reading.
- **No way to focus a period.** Added a **From / To** date window, bounded to
  phase start → today, with a reset.

### Test suite

Five new files: `test_cursor`, `test_avature`, `test_successfactors`,
`test_revolutpeople`, `test_trends_dedupe`, `test_applications_rejection`.

Two existing guards were found **silently dead** and repaired:

- `test_webitrent` — its fake response object lacked `geturl()` after the
  connector started calling it, so the whole suite errored out rather than
  running. Broken for weeks without anyone noticing.
- `test_trends_london_boroughs` — used a hardcoded phase id and wrote into the
  real `trends.json`, so it failed the second time it was ever run. It now uses a
  unique company and phase id per run and deletes its own rows on the way out.

A green suite only means something if you notice which suites *aren't* running.

### Documentation

`CONNECTOR_PLAYBOOK.md` — §4 Step 1 rewritten around the capture method that
actually works: the agent writes a `curl` command, the owner runs it and uploads
the file, with DevTools kept as the fallback for JS-loaded boards. Eight new
lessons (§6.13–6.20). Stale references to running tests from the project root
corrected.

`FILE-MAP.md` (local-only, not in this repo) bumped to v1.1.0 with every touched
file updated.

---

## Actions after updating

These act on the **external data folder** (`~/JobWatchData/`), not the repo.

1. **Hard-refresh the browser** (⌘⇧R) — `app.js` and `app.css` both changed and a
   plain reload serves the cached copies.
2. **Reclassify existing rejections** in the tracker. They all currently read
   *"Rejected (stage not recorded)"*; the picker offers both staged values.
3. **Clear the stray demo trend rows**, which came from running
   `python3 -m jobwatch.trends` — its `__main__` block writes to live data:
   ```bash
   python3 -c "
   from jobwatch import trends
   d = trends._load(); n = len(d['entries'])
   d['entries'] = [e for e in d['entries'] if e.get('phase_id') != 'phase-demo' and e.get('company_key') != 'demo']
   trends._save(d); print(f'removed {n - len(d[\"entries\"])} demo rows')"
   ```
4. **Check the Trends chart.** Any dates where a check was run more than once
   previously showed an inflated spike; those should now read true. The most
   telling ones are dates where only SOME companies were re-checked, since those
   were distorted in shape rather than uniformly doubled. To find them:
   ```bash
   python3 -c "
   import json, pathlib, collections
   p = pathlib.Path.home()/'JobWatchData'/'trends.json'
   rows = json.loads(p.read_text()).get('entries', [])
   k = lambda e: (e.get('company_key'), e.get('phase_id'), e.get('department'), e.get('location_bucket'), e.get('date'))
   c = collections.Counter(k(e) for e in rows)
   d = collections.Counter(a[4] for a, n in c.items() if n > 1)
   print('dates that were inflated:', sorted(d) or 'none')"
   ```
