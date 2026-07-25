# JobWatch — Patch Notes

Newest first. Each entry records what changed, why, and anything the owner needs
to do by hand after updating.

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
