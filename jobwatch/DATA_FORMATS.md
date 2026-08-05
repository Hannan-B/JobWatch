# JobWatch — Data Formats (locked in Phase 0.4)

Every file JobWatch saves uses one of the shapes below. These are fixed now
because every later phase reads and writes them. All files are plain JSON,
stored in your external data folder (`~/JobWatchData`), never in git.

---

## 1. Job (the shared shape every connector returns)

Not stored on its own — it's the building block inside snapshots.

```json
{
  "id": "4673971005",
  "title": "Social Strategy Director",
  "location": "London, United Kingdom",
  "department": "PR, Influence and Social",
  "url": "https://job-boards.greenhouse.io/ogilvyuk/jobs/4673971005"
}
```
- `id` — stable unique string for the role. Drives new/removed detection.
- `department` — blank string `""` when the board doesn't provide it.

---

## 2. Company  (stored in `companies.json`, a list of these)

```json
{
  "key": "ogilvy-uk",
  "display_name": "Ogilvy UK",
  "connector": "greenhouse",
  "config": { "board": "ogilvyuk" },
  "buckets": ["Advertising", "London targets"],
  "sub_buckets": { "Advertising": "Creative" },
  "tier": 1,
  "added_on": "2026-06-15"
}
```
- `key` — internal unique id (lowercase, no spaces).
- `connector` — which connector reads it (`greenhouse`, `eightfold`, `apple`, ...).
- `config` — whatever that connector needs (e.g. a board token). Auto-filled by the add-company flow.
- `tier` — 1 auto-detected · 2 preset · 3 not-yet-supported (recorded but not runnable).
- `sub_buckets` *(optional, added Phase I)* — a map `{ bucket_name: sub_bucket_name }` placing this company in **one** sub-bucket per bucket it belongs to. A bucket absent from the map (or absent `sub_buckets` entirely) means the company is **unplaced** in that bucket → treated as `Other/Misc` at read time. Only buckets the company actually belongs to should appear here. See §7.

---

## 2b. Sub-bucket name roster  (stored in `sub_buckets.json`, added Phase I)

A small sibling file holding the **named** sub-buckets per bucket, so a sub-bucket that's been named but has no members yet still survives.

```json
{
  "Advertising": ["Creative", "Strategy"],
  "London targets": ["Priority"]
}
```
- Map of `bucket_name → [sub_bucket_name, ...]`.
- The implicit **`Other/Misc`** catch-all is NEVER stored here — it always exists and is appended last by the engine. Don't write it into this file.
- The full sub-bucket list for a bucket = union of (this roster) ∪ (names actually used by members in `sub_buckets`) ∪ {`Other/Misc`}.
- Forgiving, non-critical (like `trends.json`): a missing or corrupt file just yields `{}`.
- Deleting or renaming a bucket cascades here (the entry is dropped or moved).

---

## 3. Phase  (stored in `phases.json`, a list of these)

```json
{
  "id": "phase-2026-04-active",
  "name": "Active hunt – April 2026",
  "type": "active",
  "cadence_days": 3,
  "started_on": "2026-04-01",
  "ended_on": null
}
```
- `type` — `"active"` or `"casual"`.
- `cadence_days` — expected gap between checks (active ~3, casual ~10).
- `ended_on` — `null` means this is the current phase. Only one phase is current at a time.

---

## 4. Snapshot  (stored in `snapshots/<company-key>/<timestamp>.json`)

One file per check of one company. The atom of the system.

```json
{
  "company_key": "ogilvy-uk",
  "phase_id": "phase-2026-04-active",
  "taken_at": "2026-06-15T09:30:00",
  "jobs": [ { "id": "...", "title": "...", "location": "...", "department": "...", "url": "..." } ]
}
```
- Comparison only ever happens between two snapshots **with the same `phase_id`**.

---

## 5. Interests  (stored in `interests.json`)

```json
{
  "keywords_ranked": ["strategy", "data", "director", "PR"],
  "keywords_mode": "rank",
  "locations_allowed": ["London", "Remote UK"],
  "departments_allowed": ["Finance", "Strategy"],
  "experience_years_max": 8
}
```
- `keywords_ranked` — ORDERED by preference. Earlier = higher priority in flagging.
- `keywords_mode` *(added Phase M)* — `"rank"` (default) or `"filter"`. `"rank"` keeps the v1 behaviour (matching roles sort to the top, nothing hidden). `"filter"` shows ONLY roles that match at least one keyword. With no keywords set, both behave as "no keyword gate" (everything kept).
- `locations_allowed` — the ONLY hard filter. Roles elsewhere are excluded. **Capped at 5** (Phase M); the save path rejects more with a plain message.
- `departments_allowed` *(added Phase M)* — multi-value department filter, **contains-match** and case-insensitive ("Finance" catches "Global Finance"). When set, roles with NO department go to a **"department unclear"** group (flagged, never hidden), mirroring the location "ambiguous" bucket. Empty list = no department gate. No cap.
- `experience_years_max` — roles asking for more raise an amber "stretch" flag (never hidden).
- **Filters live on the Jobs → Check tab (Phase M):** this record is the SAVED DEFAULTS. The Jobs filter panel pre-fills from it, lets the user tweak per run (the tweak is passed to the run only, NOT written back), and "Save as my defaults" writes the panel back here. Older files missing `keywords_mode` / `departments_allowed` load forward-compatibly (defaults `"rank"` / `[]`).
- **Shown-job flags (computed, not stored):** roles surfaced through `filters.apply_all` carry `interest_rank` / `interest_hits` / `experience_required` / `experience_stretch` / `location_unclear` / `department_unclear` *(the last added Phase M)*. Saved and tracked roles are RE-FLAGGED on read (Phase M.5) so these show consistently with the run views; the flags are never persisted to `saved_jobs.json` / `applications.json`.

---

## 6. Trend log  (stored in `trends.json`)

Tiny counts kept ~1 year, tagged by phase, so charts can band by phase.

```json
{
  "entries": [
    {
      "date": "2026-06-15",
      "company_key": "apple",
      "phase_id": "phase-2026-04-active",
      "department": "Finance",
      "location_bucket": "London",
      "open_count": 9,
      "added_count": 2,
      "removed_count": 1
    }
  ]
}
```
- One small entry per company/department/location/date. Cheap to keep long-term.
- `open_count` — roles open in that (department, location) cell on that date. This is what powers "Apple London finance roles went 2 → 5 → 9 across the phase."
- `added_count` / `removed_count` (**Phase N**) — how many roles were ADDED and REMOVED in that same cell since the previous check, recorded in lockstep with `open_count` from the run's compare verdict. A baseline check records everything as `added` with `removed` 0. The three numbers share one row so a chart reads all of them without a join.
- **Backward-compatible:** rows written before Phase N have no `added_count` / `removed_count`; readers MUST treat a missing delta field as 0. (The engine only stamps the delta fields when the caller passes the run's new/removed lists, so pre-N behaviour is byte-identical when they're absent.)
- **Union-of-cells rule:** a check records a row for every (department, location) cell seen across the open list AND the added/removed lists. So a team that emptied out this check — 0 open, but 3 removed — still gets a row with `open_count` 0 and `removed_count` 3, rather than silently vanishing.
- **Recording point (Phase N):** trends are written by the orchestrator right after each company's compare (`_record_trends`), the one spot with company + phase + current list + verdict together. Before Phase N nothing called the recorder, so the chart had no real data — see the v2 handover's Phase N notes. Recording is non-critical: any failure is swallowed so a trend write can never break a real check.

---

## 6b. Saved (favourite) roles  (stored in `saved_jobs.json`, added Phase K)

The roles you've STARRED while reading a run. A flat list of these records.

```json
{
  "id": "4673971005",
  "company_key": "ogilvy-uk",
  "company_name": "Ogilvy UK",
  "title": "Social Strategy Director",
  "location": "London, United Kingdom",
  "department": "PR, Influence and Social",
  "url": "https://job-boards.greenhouse.io/ogilvyuk/jobs/4673971005",
  "phase_id": "phase-2026-04-active",
  "saved_on": "2026-06-21T14:05:00",
  "applied": false,
  "applied_on": null
}
```
- Identity is the pair **(`company_key`, `id`)** — starring the same role twice is a no-op.
- `saved_on` drives the "by date added" sort; `company_name` the "by company" sort.
- `applied` is set true by the Saved tab's "Applied" button; the role then leaves the active Saved view, on its way to the Application Tracker (Phase L). `applied_on` records when.
- **Dormancy reset (locked):** when the current hunt lapses into dormancy (or there's no current phase), the whole list is wiped — enforced lazily on read (`list_saved(is_dormant=True)` clears the file and returns `[]`). Applied-but-unadopted roles are cleared too; once Phase L exists, applied roles already live in the tracker, so nothing is lost then.
- Forgiving, non-critical (like `trends.json`): a missing or corrupt file just yields `[]`.

---

## 6c. Applications  (stored in `applications.json`, added Phase L)

The durable record of roles you've APPLIED to. A flat list of these records.
Unlike saved jobs, applications **survive dormancy** — this is the real record
of a job hunt, tied to the phase it was created in.

```json
{
  "id": "4673971005",
  "title": "Social Strategy Director",
  "company_key": "ogilvy-uk",
  "company_name": "Ogilvy UK",
  "url": "https://job-boards.greenhouse.io/ogilvyuk/jobs/4673971005",
  "phase_id": "phase-2026-04-active",
  "applied_on": "2026-06-21",
  "status": "applied",
  "screening_interview": false,
  "interview_rounds": 0,
  "notes": "",
  "last_progress_at": "2026-06-21"
}
```
- Identity is the pair **(`company_key`, `id`)**; adding a role already tracked **in the same phase** is a no-op. Manual adds with no engine id get a synthesised `manual-<timestamp>` id and dedupe on URL within the phase.
- `applied_on` — date the row was created (AUTO). Drives the Date-Applied column + filter. Date only.
- `status` — one of **8**: `applied` (default) · `screening` · `interview` · `ghosted` · `offer` · `rejected_before_interview` · `rejected_after_interview` · `withdrawn`. Grouped To-do / In progress / Complete. The live statuses (`applied`/`screening`/`interview`) form a **one-way ladder**; the five terminal statuses are set directly. Backward/sideways live moves are rejected.
- **Rejections carry their STAGE** *(added 2026-07-25)*. `rejected_before_interview` means it never reached a conversation; `rejected_after_interview` means it did. The distinction is the difference between "the market isn't biting" and "I get in the room but don't close", and it is what makes `response_rate` meaningful.
- **Backward-compatible:** rows written before the split carry a bare **`rejected`**, which stays valid and readable for ever and is NEVER migrated (same rule as pre-Phase-N trend rows without delta fields). Readers treat it as "stage unknown" and fall back to the engagement markers below. The UI still renders it, and allows correcting it to a staged value, but never offers it as a new choice — so it can only shrink over time.
- **What counts as a RESPONSE** *(server.`_employer_engaged`)*: did the EMPLOYER engage? `rejected_before_interview` is an explicit no and overrides everything; `screening`/`interview`/`offer`/`rejected_after_interview` are an explicit yes; every other status — `applied`, `ghosted`, `withdrawn`, legacy `rejected` — is judged by the markers `screening_interview` or `interview_rounds > 0`. So withdrawing after two rounds counts while withdrawing before anyone called doesn't, and being ghosted after a screening counts while silence doesn't.
- `screening_interview` — the Yes/No column; default `false`.
- `interview_rounds` — an editable count; default `0`. Independent of status.
- `notes` — free text; the user's OWN thinking. **NOT a job description** (Description is deliberately not stored — Phase L locked decision; engage with descriptions live on the board).
- `last_progress_at` — date of the last **forward signal**; starts equal to `applied_on`. Bumped ONLY by a forward status move OR the screening-interview toggle going No→Yes. Notes/rounds edits never bump it.
- **Auto-ghost (locked):** a row still in a live status whose `last_progress_at` is ≥ the auto-ghost threshold auto-flips to `ghosted`. Evaluated **lazily on read** (no background timer); the flip is persisted on that read.
- **The threshold is a SETTING, not a constant** *(2026-08-05)*. It lives in `settings.json` as `ghost_after_days` (default **21**, raised from a hardcoded 14) and is adjustable from the Settings screen. It is **independent of `dormancy_days`** despite sharing a default: one measures a phase going unchecked, the other one employer going quiet. Readers must never serve one from the other.
- **The flip is DESTRUCTIVE and is not reversible by raising the threshold.** `apply_auto_ghost` overwrites `status` and records nothing about the previous value, so a row that was at `screening` when it ghosted is afterwards indistinguishable from one that was at `applied`. Correcting past flips means a deliberate one-off write, reconstructing the old status by hand or inferring it from `screening_interview` / `interview_rounds`. Such a write must go **direct to the file**: `ghosted` is terminal and the ladder legitimately refuses terminal → live.
- **The "Applied" hand-off (L.6):** roles the user pressed "Applied" on in the Jobs tab are adopted here from `saved_jobs.json` (then removed from it, so an applied role lives only in the tracker).
- Forgiving, non-critical (like `trends.json`): a missing or corrupt file just yields `[]`.

---

## 7. Sub-bucket semantics (Phase I)

- A **bucket** is a flat label a company carries; a company carries **many** (unchanged from §2).
- A **sub-bucket** is a named subdivision **inside one bucket**. A company sits in **at most one** sub-bucket *per bucket* (single-occupancy), recorded in its `sub_buckets` map (§2).
- **`Other/Misc`** is implicit and always present when a bucket has sub-buckets: any member not explicitly placed resolves to it. It is never stored (not on the company, not in the roster) — it's computed.
- Sub-buckets are **opt-in per bucket**: a bucket with no roster entry and no placed members is simply *flat*.
- Cascades: renaming a bucket moves its `sub_buckets` keys and its roster entry; deleting a bucket drops both.

---

## Rules that never change
- All personal data lives in `~/JobWatchData`, never in git.
- `id` fields are treated as opaque strings (don't assume they're numbers).
- Missing optional text fields are `""`, missing dates are `null`.
- Comparison is always within a single phase.
