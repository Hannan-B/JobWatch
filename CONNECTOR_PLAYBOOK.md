# JobWatch Connector Playbook

_The single reference for adding a new company to JobWatch. Hand this file to an
agent (plus the files listed in §10) and it has everything needed to add a
company end-to-end. Updated **2026-07-25** after the Cursor / Bloomberg (Avature) /
Penguin Random House (SuccessFactors) / Cleo (Revolut People) builds — which also
replaced the capture method in §4 with something far less painful, added
completeness guards, and cost us three near-misses now written up in §6._

---

## 0. What JobWatch is, in one paragraph

JobWatch monitors companies' careers pages and tells you what roles are new or
removed since the last check. Adding a company means giving JobWatch a way to
read that company's job list. There are two ways: (a) the company runs on a
**standard platform** JobWatch already supports (Greenhouse, Lever, Ashby,
Workday, etc.) — then it's **paste-the-URL, no code**; or (b) it runs a **custom
board** — then someone writes a small **connector** function. Most of this
playbook is about (b), but **(a) is always the first thing to check**, because
most companies turn out to be on a standard platform even when their careers page
looks bespoke.

---

## 1. The golden rule: rule out the easy path first

**Before writing a single line of code, check whether the company is on a
standard platform.** This has paid off repeatedly — Notion, Financial Times,
Navan and Figma all *looked* like custom boards but were actually Greenhouse
underneath. The check costs two minutes and saves a whole build.

How to check:
1. Open the company's careers page, click into a job, and look at the URL bar and
   the page's network requests. Tell-tale hosts/paths:
   - `boards.greenhouse.io` / `boards-api.greenhouse.io` / `job-boards.greenhouse.io` → **Greenhouse**
   - `jobs.lever.co` / `api.lever.co` → **Lever**
   - `jobs.ashbyhq.com` / `api.ashbyhq.com` → **Ashby**
   - `*.myworkdayjobs.com` → **Workday**
   - `*.eightfold.ai` / `careers.*` powered by Eightfold → **Eightfold**
   - `smartrecruiters.com` → **SmartRecruiters**
   - `apply.workable.com` → **Workable**
   - `*.pinpointhq.com` → **Pinpoint**
   - `*.teamtailor.com` (or a known custom domain) → **Teamtailor**
   - `*.avature.net` → **Avature**
   - `rmkcdn.successfactors.com` assets / a `careerSiteCompanyId` cookie /
     `/<SITE_ID>/search/` paths → **SAP SuccessFactors**
   - `revolutpeople.com/<tenant>/public/careers` → **Revolut People**
2. Often the company hides the platform behind their own domain. Look for the
   **API call** the page makes (DevTools → Network → Fetch/XHR). If it calls one
   of the hosts above, it's that platform — even if the front-end is custom. (FT
   and Notion were exactly this: pretty front-end, Greenhouse API underneath.
   Canva was the 2026-07-25 example: `lifeatcanva.com` is an Umbraco skin over
   SmartRecruiters, and needed no code at all.)
3. **Search, don't just look.** `"<company> careers"` plus an ATS name, or the
   job-ID format, often answers it in one query — faster than reading markup, and
   it works even when the careers page is challenge-walled.
4. Remember the platform list now includes THREE hosted-ATS platform connectors
   added 2026-07-25 (avature, successfactors, revolutpeople) plus teamtailor,
   ciphr and webitrent. A company on any of them is **paste-and-go** — no build.
3. The platform's **board token** is usually in that API URL. For Greenhouse,
   `boards-api.greenhouse.io/v1/boards/<TOKEN>/jobs` — the `<TOKEN>` is what you
   need. Watch for renamed tokens: Navan's is still `tripactions` (the old name).

If it's a standard platform: **add it via paste-URL in the app, done.** No
connector, no code, no test file. Only proceed to a custom build if the company
genuinely runs its own board.

**Currently supported standard platforms** (paste-and-go):
greenhouse, lever, ashby, workday, eightfold, smartrecruiters, workable, pinpoint.

**Custom connectors already built** (these specific companies/platforms):
uber, spotify, playstation, deliveroo, va (Victoria & Albert Museum),
webitrent (MHR iTrent platform), ciphr (CIPHR iRecruit platform), sohohouse.

Note `webitrent` and `ciphr` are **platform** connectors, not single companies —
any employer on `*.webitrent.com` or `*.ciphr-irecruit.com` works by pasting
their URL. (National Gallery and British Museum are instances of these.)

---

## 2. Where everything lives (file paths)

Real project root (your Mac):
```
~/Documents/Personal Projects/Job Search Tracker/
├── jobwatch/                  ← the package (all code lives here)
│   ├── connectors.py          ← WRITE: connector fn + CONNECTORS registry + shared helpers
│   ├── detect.py              ← WRITE: paste-URL auto-detect (_detect_<name> + host-match)
│   ├── market_scope.py        ← WRITE (only if global board): SCOPABLE + scoped_config
│   ├── geo.py                 ← READ-only: city → country/coords (region_for_city, COUNTRY_ISO)
│   ├── filters.py             ← READ-only: the location-filter contract the connector must satisfy
│   └── DATA_FORMATS.md        ← READ-only: the locked job/company shapes
├── test_<name>.py             ← WRITE: one mocked test file per custom connector (at ROOT, not in jobwatch/)
└── CONNECTOR_PLAYBOOK.md       ← this file
```

Tests run **from the project root** so `from jobwatch import connectors` resolves.
Downloads from the agent land in `~/Downloads`; you `mv` them into place (commands
are always provided at the end of a build).

---

## 3. The architecture in five minutes

### 3.1 What a connector is
A function `name(config)` that returns a **list of job dicts**. Nothing more.
It's registered in the `CONNECTORS` dict and called via `fetch_jobs(provider, config)`.

### 3.2 The locked job shape (DATA_FORMATS.md §1) — never deviate
Every job dict has exactly these keys, built with the shared `_job()` helper:
```python
_job(id_, title, location, department, url)
# → {"id": str, "title": str, "location": str, "department": str, "url": str}
```
- **id** — a STABLE, UNIQUE string. This drives new/removed detection, so it must
  not change between checks. A requisition number or slug is ideal; never a
  row-index or anything positional. `_job` coerces it to `str` (so integer `0` and
  `"0"` survive — see §6 falsy-id lesson).
- **title** — the role title. Unescape HTML entities (`&amp;` → `&`).
- **location** — prefer a CITY (DATA_FORMATS §E-2). Country/site is an allowed
  fallback only when the source genuinely exposes no city. The app's location
  filter matches against this string.
- **department** — `""` when the source has none. Never invent one.
- **url** — the role's apply/detail page, made absolute.

### 3.3 Shared helpers in connectors.py — REUSE, never reinvent
- `_job(id_, title, location, department, url)` — builds the locked dict; coerces
  id to str, defaults blanks to `""`.
- `_merge_by_id(jobs)` — collapses rows sharing an id, MERGING distinct locations
  ("Cambridge; London; Saint Albans"). Use it as the last step of every connector.
  Prevents double-counting that would destabilise new/removed detection.
- `http_get(url, headers=None)` — GET with sane defaults.
- `http_post_json(url, payload, headers=None)` — POST JSON.
- `BROWSER_HEADERS` — a realistic Chrome UA + headers. Use for sites that reject
  bare requests.
- `_polite_pause()` — small randomised sleep between paged requests.
- `ConnectorError` — raise this (with a clear message) when something the user can
  act on goes wrong (e.g. "site returned no rows; re-capture").
- `TIMEOUT_SECONDS` — the standard timeout.

Top-of-file imports already available: `json, time, random, re,
urllib.request/parse/error`. For HTML connectors use stdlib `re` + `html`
(`import html` inside the function) — **no bs4**; the project has no HTML-parser
dependency and won't get one.

### 3.4 detect.py — paste-URL auto-detect
Two pieces make a pasted URL auto-recognise:
1. A `_detect_<name>(url, host, path, query)` recogniser (for platform-style
   matches), OR a host-match block inside `detect()` (for single companies).
2. The provider being present in `CONNECTORS` — that's what flips it from
   "recognised but not runnable" to a **Tier-1 runnable** result.
The recogniser returns a dict with `provider`, `config` (the keys the connector
needs, e.g. `{"url": url}`), `tier: 1`, `confident: True`, and a `message`.

### 3.5 market_scope.py — only for GLOBAL boards
If a board returns the whole world and supports server-side location filtering,
add it to `SCOPABLE` and map the user's chosen cities to the board's filter.
Two patterns exist:
- **Country-code scoping** (Apple, Google): city → ISO code via `geo.region_for_city`
  + `COUNTRY_ISO`, passed as the board's country param.
- **City-name passthrough** (Uber, PlayStation, Spotify): the chosen city names are
  passed straight through as `location_list` and the connector turns them into the
  board's city slugs/filters.
If a board has **no** server-side filter (static file, single HTML page), do NOT
add it to SCOPABLE — fetch everything and let the app's filter narrow. (Deliveroo,
Soho House work this way.)

---

## 4. The build process, step by step

### Step 1 — Capture the real data (the user does this on their Mac)
The sandbox the agent runs in **cannot reach job boards**. So the user captures
the real request/response and uploads it. **Build against the real captured body,
never a guessed shape** (DATA_FORMATS §A-3) — guessing the endpoint/shape is what
wasted rounds before captures existed.

#### 1a. DEFAULT: the agent writes a `curl` command, the user runs it and uploads the file
This is the method to reach for first. It is far less work for the owner than
DevTools, and it produces exactly what the agent needs. The AGENT writes the
command — including the URL, the browser User-Agent, and a size check — and the
owner pastes it into Terminal and uploads the resulting file.

```bash
cd ~/Downloads
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
curl -sL -A "$UA" -w "\nHTTP %{http_code} — %{size_download} bytes\n" \
  "<THE CAREERS URL>" -o capture.html
```

Rules that make this work:
- **Always print the status and size** (`-w`). The size is the tell: a few KB
  means you got a JS shell or a challenge page, not the jobs.
- **Always name the file** and tell the owner to upload it. Terminal output
  pasted into chat is not the same as the file — a size alone is not a capture.
- **Grab TWO pages when the board paginates** (`?page=2`, `&startrow=25`,
  `&jobOffset=12`). One page proves the parse; two prove the pagination, and
  pagination is where the expensive bugs live (§6.14).
- **Strip tracking/filter params** from the URL before capturing — `pid=`,
  `af_sub4=`, location facets. Capture what the connector will actually fetch.
- **Sanity-grep in the same command** where it's cheap, e.g.
  `grep -c "JobDetail" capture.html`, or `grep -o 'of [0-9]*' capture.html`.
  A single command that captures AND diagnoses saves a whole round trip.
- **Write the command so it can be pasted verbatim.** No `<PLACEHOLDERS>` the
  owner has to fill in — if you don't know the URL yet, ask for it as its own
  question first. (A placeholder left in a command wasted a round on PRH.)

The agent can often do the reconnaissance itself before asking for anything: an
agent with web access can fetch many careers pages directly to learn the platform,
the pagination scheme and the row markup, then ask for a much more precise
capture. **Do this first — it has repeatedly turned a "build" into "no build
needed"** (§1, and Canva in §6.13).

#### 1b. FALLBACK: DevTools Network capture — for anything trickier
Use this when 1a comes back small, empty, or challenge-walled — i.e. the jobs are
loaded by JavaScript rather than served in the HTML.

DevTools → **Network** → **Fetch/XHR** → reload the page, then:
- Find the request that returned the jobs (its Response tab shows job titles).
- Right-click → **Copy → Copy as cURL** (URL, method, headers, cookies).
- Right-click → **Copy → Copy response** (the JSON body — the source of truth
  for field names).
- For a POST, also copy the **Payload** (page size and filters live there).
- If nothing shows under Fetch/XHR, switch to **All**; some boards fetch on
  scroll rather than on load.

**Then re-test cookieless.** A DevTools cURL carries the browser's cookies; the
connector will have none. Re-run the endpoint with a plain `curl` (no `-H
'Cookie: …'`) before designing anything — Cleo's API turned out to need no auth
at all, which made the connector trivial.

#### 1c. What to note either way
Where the **city** lives, how **pagination** works (and any reported TOTAL —
you'll want it for the completeness guard, §6.14), whether roles **repeat** per
location, whether the board is **global**, and whether the page renders the list
**more than once** (§6.8).

### Step 2 — Agent writes the connector (against the captured body)
- Read `connectors.py` first to reuse helpers and match house style.
- Parse only fields that exist in the capture. Map them to the locked shape.
- Page to the board's own reported TOTAL; never an arbitrary cap. Retry a failed
  page once before stopping.
- **Add a COMPLETENESS GUARD whenever the board reports its own total.** Collect,
  compare, and raise a clear `ConnectorError` naming BOTH numbers rather than
  return a short list. A quietly incomplete fetch is worse than an error: every
  missing role reads as REMOVED, and next check as ADDED again. Tolerate a small
  shortfall (`max(3, total//100)`) because boards genuinely change mid-crawl —
  Bloomberg's own total moved 436 → 426 while we were testing.
- Same reasoning for an empty parse: raise, don't return `[]`.
- End with `_merge_by_id(jobs)`.

### Step 3 — Agent wires up detect (+ market_scope if global)
- Add the recogniser/host-match in `detect.py` and confirm the provider is in
  `CONNECTORS` (that's what makes it runnable).
- If global + server-filterable, wire `market_scope.py`.

### Step 4 — Agent writes a mocked test file
One `tests/test_<name>.py` (NOT the project root — they moved during cleanup;
run as `python3 -m tests.test_<name>`), mirroring the canonical pattern (§7).
It mocks `http_get`/`http_post_json` (or the cookiejar opener) to serve the
captured body, and asserts the parse, pagination, dedupe, detect wiring, and any
quirk specific to this board.

### Step 5 — Agent verifies in-sandbox, then the user verifies live
- In-sandbox: run the new test + ALL existing tests (regressions). Everything must
  pass. The agent also validates the parser against the real captured body
  directly (not just the synthetic mock).
- Live (the real checkpoint): the user `mv`s the files into place and runs the
  connector on their Mac, eyeballing the count against the careers page. **That
  live count is the only true checkpoint** (DATA_FORMATS §E-8) — green tests prove
  the parse, but only a live run proves the fetch.

### Step 6 — Add in the app
Paste the company's careers URL into Add-a-Company. detect recognises it; the
connector does the rest. **Tell the owner the EXACT string to paste**, and flag
any part of it that is load-bearing — `/PRH_UK/` scopes SuccessFactors to Penguin
rather than the whole Bertelsmann group, and pasting a marketing page instead of
the board dead-ends at Tier 3.

**When you hand files over, list EVERY file that needs installing, every time.**
Don't write "unchanged since last time, skip it" — that instruction sent the
owner into a red test suite because `detect.py` had in fact changed. Cheap to
re-copy, expensive to debug.

---

## 5. The four connector patterns seen so far

Pick the one that matches the capture. Each has a worked example in `connectors.py`.

### Pattern A — JSON API, plain GET/POST (simplest)
The page calls an endpoint that returns JSON. Parse the array, map fields, page if
needed. **Examples:** Deliveroo (WordPress REST, paged), Spotify
(`api.lifeatspotify.com/wp-json/animal/v1/job/search`, GET).
- Watch the **host**: the jobs API may be on a different subdomain than the page
  (Spotify's feed is on `api.` not `www.` — that one detail kept it parked until
  captured).

### Pattern B — JSON API needing an anonymous warm-up token
The endpoint needs a session token first minted by visiting the page anonymously.
Use a `cookiejar` + `build_opener`: GET the launch page to mint the token/cookie,
then call the endpoint on the **same opener**. **NO personal login cookies** —
only anonymous, freshly-minted ones.
- **PlayStation:** GET careers page → mint `ct` token → POST to get-jobs on the
  same opener.
- **National Gallery (webitrent):** GET launch page → scrape `USESSION` from HTML
  → call the JSON endpoint. **Critical gotcha:** webitrent returns an HTML
  fallback page unless the list request carries the **`mhrParams` header** with the
  full (mostly-empty) search param set. A 200 that's HTML instead of JSON = a
  missing required header; compare your request to the captured cURL header-by-header.

### Pattern C — Server-rendered HTML (no JSON at all)
The jobs are baked into the page HTML; nothing in Network/Fetch. Parse with stdlib
`re` + `html`. **Examples:** V&A (`<div class="vacancy">` blocks), British Museum
(`<table>` rows).
- Find columns/fields **robustly**, not by fixed position: the British Museum
  connector locates the Location column by its header's `LocationColumnHeaderTooltip`
  attribute (falling back to an index), so it survives column reordering on other
  CIPHR tenants.
- Unescape entities; strip inner tags from cell text.

### Pattern D — Next.js / static data file (with a rotating build hash)
The site is a Next.js app; the full job list ships as a static JSON data file at
`/_next/data/<buildId>/<page>.json`. The `<buildId>` **changes on every redeploy**,
so you can't hardcode it. **Soho House** is the worked example:
1. Fetch the careers HTML page.
2. Read the current `buildId` from the embedded `__NEXT_DATA__` script.
3. Next.js usually embeds the page's data **inline** in `__NEXT_DATA__` too — read
   the jobs straight from there (most robust). Only if that's absent, fetch
   `/_next/data/<buildId>/<page>.json` as a fallback.
This inline-first approach means routine redeploys (which only change the hash)
fix themselves with no code change.

---

## 6. Lessons learned (the bugs we already paid for — don't repeat them)

1. **Always rule out standard platforms first** (§1). Notion, FT, Navan, Figma all
   looked custom, were Greenhouse. Two-minute check, whole-build saving.

2. **Build against the real captured body, never a guess** (DATA_FORMATS §A-3).
   Every parked/painful build traced back to working from an assumed shape. The
   capture is reality.

3. **The jobs API can be on a different host than the page.** Spotify's feed is
   `api.lifeatspotify.com`, not `www.`. When probing candidates, include the `api.`
   subdomain. A connector that only tries the page's host will silently find nothing.

4. **A 200 isn't success — check the body type.** webitrent returned HTTP 200 with
   an *HTML* fallback when the `mhrParams` header was missing. If JSON parsing fails
   on a 200, you're probably missing a required header; diff against the captured
   cURL.

5. **Falsy-id bug.** Guard with `if jid is None or jid == ""`, never `if not jid` —
   an integer `0` or string `"0"` is a valid id and must survive. `_job` coerces ids
   to `str`; respect that.

6. **Capture the CITY, not just the country** (the Google lesson, DATA_FORMATS §E-2).
   If a city exists anywhere in the response, read it. Settling for a country code
   when a city was available is a real bug we fixed. Country-only is allowed *only*
   when the source truly exposes no city.

7. **Page to the reported TOTAL; retry a failed page once.** A single failed page
   must never silently truncate the list (the Apple lesson). If you stop short,
   say "fetched N of M".

8. **Merge multi-location / duplicate rows** with `_merge_by_id`. Boards that list a
   role once per office (or per location in a `locations[]` array) will otherwise
   inflate counts and destabilise new/removed detection.

9. **Unescape HTML entities everywhere** — titles and locations. The feeds are full
   of `&amp;` (R&D, "Portfolio & Monetization"). Applies to JSON feeds too, not just
   HTML (Spotify's JSON carried `&amp;` in titles).

10. **Parse HTML structurally, not positionally.** Locate columns by header
    identity (British Museum's `LocationColumnHeaderTooltip`), not a hard index, so
    the connector survives reordering and works across a platform's tenants.

11. **Don't defeat anti-bot walls; park instead.** Bloomsbury's job list sat behind
    a Cloudflare JS challenge with no clean entry point — we parked it rather than
    hardcode ids or defeat the challenge (both against the rules). Parking a hard
    company is a legitimate, honest outcome.

12. **Custom boards are fragile by nature.** When a bespoke site reshapes its
    internal endpoints, the connector breaks — re-capture and rebuild. Standard
    platforms rarely break; prefer them. (Penguin Books was deferred precisely
    because they're mid-rebuild — don't build against a moving target.)

13. **No personal cookies, ever.** The public job list works logged-out. Anonymous
    warm-up cookies (Pattern B) are fine; a user's session/login cookies are never
    read, stored, or hardcoded. If a board truly requires login to see jobs, stop
    and flag it.

14. **Self-diagnosing connectors help when capture is hard.** Spotify was built to
    probe an ordered list of candidate endpoints and raise a clear "tried: …" error
    naming each — so when it parked, the error itself told us what to capture next.
    Worth doing for boards that are intermittently empty or hard to inspect.

---

### 6.13 The ATS may be hiding behind a CMS skin — chase the ATS, not the markup
`lifeatcanva.com` looked like a bespoke server-rendered board and was behind a
Cloudflare challenge. It is an Umbraco skin over **SmartRecruiters**, which
JobWatch already supported: Canva needed ZERO code, just
`careers.smartrecruiters.com/canva`. The tell was in the data all along — the job
IDs on the careers page (`6000000001258599`) matched SmartRecruiters' format.
**Before reading anyone's markup, find out what ATS they run.** Search
"<company> careers <ATS name>", open a role and look at where **Apply** goes, and
check the ID format. §1 exists for exactly this and it paid twice in one session.

### 6.14 Offset pagination does not always TILE — prove it before you trust it
Avature ignores `jobRecordsPerPage` AND `sortBy`, and its result order DRIFTS
between requests: adjacent pages re-served roles, and a straight 12-step crawl of
Bloomberg collected **406 of 426** — with a different ~20 missing each run. That
is worse than missing data: compare reports those roles REMOVED, then ADDED next
check, spraying phantom churn into the run verdict and the trend deltas.
**The test:** crawl every page and check unique-collected against the board's
reported total. If they don't match, the pages aren't tiling.
**The fix:** step by HALF a page so consecutive windows overlap and absorb drift
(`_merge_by_id` collapses the duplicates for free), and add the §Step-2
completeness guard so a crawl that still comes up short FAILS LOUDLY.
SuccessFactors goes further: it pins the sort, and only retries with half-steps
if the guard would have fired.

### 6.15 A shared ATS install may serve a whole GROUP — scope to the site id
Penguin Random House UK sits on **Bertelsmann's** SuccessFactors install. The
bare `/search/` returns 826 roles across RTL, Arvato, Riverty, Sonopress,
Territory… PRH is one tenant among many, identified by the `/PRH_UK/` path
segment. An unscoped URL silently tracks the parent conglomerate — no error, just
the wrong company. (Dorling Kindersley is `DK_UK`, a separate board again, despite
the same London address.) **Always read the tenant/site id from the pasted URL and
scope to it**, and pin it with a test.

### 6.16 A 200 is not a live board — and a hosted board can be switched OFF
Cursor ran a hosted Ashby board; it now 404s and the roles live on `cursor.com`
itself. A stale `jobs.ashbyhq.com/cursor` job URL still returns **HTTP 200** —
with Ashby's bare "You need to enable JavaScript" SPA shell, which it serves for
ANY path and 404s client-side. Check the BODY, not the status. And when a board
that used to work stops, check whether the employer moved rather than assuming a
wrong slug.

### 6.17 An anti-bot wall blocks the CONNECTOR, not just the capture
A `curl` of Canva's careers page returned a Cloudflare managed-challenge page. The
important part isn't that the capture failed — it's that `http_get` uses urllib
and would hit the same wall on **every check, forever**. A connector built on that
would pass every mocked test and fail live, permanently. Treat a challenge page as
a park signal (rule 11) unless there's another route to the same data — there was
here (§6.13).

### 6.18 JSON fields you can see are not the fields you can rely on
Cleo's API returns `"function": null` on real roles, so a blind
`r["function"]["name"]` raises. `locations` is a LIST and roles routinely carry
several. And the payload contains **no job URL at all** — it had to be rebuilt as
`/position/<title-slug>-<uuid>`, with the slug rule derived from one confirmed
live link the owner supplied and pinned in a test. **Read the whole captured
payload for nulls and arrays before writing the parser**, and if you have to
CONSTRUCT a URL, get one real example and pin it.

### 6.19 Watch for lists rendered TWICE
Cursor's page ships the same job list twice (desktop + mobile markup): 240
`<article>` blocks for 120 roles. SuccessFactors does it per row (a
`.hidden-phone` title link and a `.visible-phone` copy). `_merge_by_id` saves you
where ids match, but only if you notice — otherwise the count is double and every
"total vs collected" check is nonsense. Count unique ids in the capture before
believing the row count.

### 6.20 Don't overwrite your own work with a stale upload
Mid-session an agent re-copied `server.py` from the owner's ORIGINAL upload to
read it — silently reverting a fix made earlier in the same session, then building
new work on top of the reverted file. **`test_trends_dedupe` caught it.** If you
must re-read a file you've already edited, read YOUR copy; if you genuinely need
the owner's, diff it against yours first. And run the FULL suite before handing
anything over, not just the tests for what you touched.

---

## 7. Anatomy of a test file (the canonical pattern)

Every `test_<name>.py` mirrors `test_uber.py`. Structure:

```python
"""One-paragraph docstring: what this verifies. Run: python3 -m tests.test_<name>"""
import json
from jobwatch import connectors           # + detect, market_scope if wired

_passed = _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")

# A small factory that builds a fake row in the REAL captured shape.
def _role(...): ...

# Mock the network: replace connectors.http_get (and/or http_post_json, or the
# cookiejar opener for Pattern B) to serve canned bodies in the captured shape.
# Also stub connectors._polite_pause = lambda: None so tests run instantly.
def _install(...): ...

# One test_* function per behaviour: parse, pagination, dedupe/merge, location
# fallback, entity unescaping, the board's specific quirk, and detect wiring.
def test_parses_real_shape(): ...
def test_pagination(): ...
def test_dedupe(): ...
def test_detect_recognises(): ...

def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns: fn()
    print(f"\n{_passed} passed, {_failed} failed ({len(fns)} tests)")
    return _failed == 0

if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
```

Key points:
- Tests are **self-contained** (no pytest dependency) — just `python3 -m tests.test_x`.
- They mock the network and serve the **real captured shape**, so they prove the
  parser against reality, not a toy.
- Mock the right seam: `connectors.http_get` for Pattern A/C/D; the
  `urllib.request.build_opener` for Pattern B (cookiejar) connectors.
- Always include a `test_detect_*` so the paste-URL wiring can't silently regress.
- Assert the board's **specific gotcha** (e.g. webitrent asserts the `mhrParams`
  header is sent; British Museum asserts location-by-header survives reordering).

---

## 8. Running tests in the terminal

From the project root (always — so `from jobwatch import …` resolves):

```bash
cd "$HOME/Documents/Personal Projects/Job Search Tracker"

# 1. Sanity: does the package import?
python3 -c "from jobwatch import connectors, detect; print('import OK')"

# 2. Run the new connector's tests
python3 -m tests.test_<name>

# 3. Run ALL tests (regression check — nothing else should break)
for t in test_*.py; do echo "-- $t --"; python3 "$t"; done

# 4. LIVE check (the real checkpoint) — run the actual connector against the
#    real site and eyeball the count vs the careers page. Example shape:
python3 -c "from jobwatch import connectors; \
jobs=connectors.fetch_jobs('<name>', {'location_list':['London']}); \
print(f'<Company>: {len(jobs)} roles'); \
[print(' ', j['title'][:45], '|', j['location']) for j in jobs[:10]]"
```

Expected output of a test file: `N passed, 0 failed (M tests)`. Any FAIL line
names the exact assertion that broke. If the live run errors or returns 0 while
the site clearly has jobs, the break is in the **fetch** (host, header, token, or
pagination), not the parse — probe the live request directly before touching the
parser.

---

## 9. Which test files to keep vs remove

**Principle:** a test file is worth keeping if (a) it guards a connector you still
use against regressions, or (b) it's a useful **template** for the next build.
Test files are tiny and self-contained, so the bias is toward keeping them — but
here's the explicit guidance.

### KEEP — regression guards (one per live connector)
These protect connectors that are in `CONNECTORS` and that you rely on. Keep each
as long as its connector exists; they're what catch a shared-helper change or a
refactor breaking an existing board:
- `test_uber.py` — Pattern A + market_scope (city passthrough). **Also the
  canonical template** — give this one to an agent as the reference for any new build.
- `test_spotify.py` — Pattern A, different-host API, candidate-probe + clear-error.
- `test_playstation.py` — Pattern B (anonymous warm-up token) + POST + city scoping.
- `test_deliveroo.py` — Pattern A, paged WordPress REST, country-only locations.
- `test_va.py` — Pattern C (server-rendered HTML, div blocks).
- `test_webitrent.py` — Pattern B (session token) + the `mhrParams` header gotcha.
- `test_ciphr.py` — Pattern C (HTML table) + location-by-header robustness.
- `test_sohohouse.py` — Pattern D (Next.js, rotating build hash, inline-first).

### GIVE TO AN AGENT when adding a new company (templates by pattern)
You don't need to hand over all of them — pick the 1–2 closest to the new board's
likely pattern, plus the canonical one:
- **Always include `test_uber.py`** — the canonical structure.
- New board looks like a **JSON API** → also `test_spotify.py` or `test_deliveroo.py`.
- Needs a **token/warm-up** → also `test_playstation.py` or `test_webitrent.py`.
- **Server-rendered HTML** → also `test_va.py` (div blocks) or `test_ciphr.py` (table).
- **Next.js / static data file** → also `test_sohohouse.py`.

### REMOVE — only in these cases
- A test file whose **connector you've deleted** from `CONNECTORS` (dead code →
  dead test). None currently apply — every test above maps to a live connector.
- A **duplicate/scratch** test from an abandoned build attempt (e.g. a
  `test_<name>_old.py` or a probe script left in root). None currently in the
  sandbox, but check your real root for any one-off `*_resp.json` capture dumps or
  scratch scripts — those `*_resp.json` files (e.g. `deliveroo_resp.json`,
  `ps_resp.json`, `ng_resp.json`) are **capture artifacts, not tests**; they can be
  deleted once the connector is live and its test passes, since the test now
  carries a representative shape inline.

### Housekeeping note on capture dumps
Large raw `*_resp.json` files in the project root are leftovers from building
against the real body. They're safe to delete after the connector ships and its
test is green — the test embeds the shape it needs. Keep them only if you want a
re-capture reference handy.

---

## 10. What to hand an agent to add a new company

Give the agent:
1. **This file** (`CONNECTOR_PLAYBOOK.md`).
2. **`jobwatch/connectors.py`** — to reuse helpers and match style (WRITE target).
3. **`jobwatch/detect.py`** — paste-URL wiring (WRITE target).
4. **`jobwatch/market_scope.py`** — only if the board is global (WRITE target).
5. **`jobwatch/geo.py`** — READ-only reference for scoping.
6. **`jobwatch/filters.py`** — READ-only, the location-filter contract.
7. **`jobwatch/DATA_FORMATS.md`** — READ-only, the locked shapes.
8. **The canonical test** `test_uber.py`, plus the 1–2 pattern-matched test
   templates from §9.
9. **The capture** for the new company: the request (cURL) + response body, or for
   HTML boards one job row's outerHTML + the header structure, plus notes on city
   location / pagination / global-or-not.

That's a complete, self-contained brief — no other project context required.

---

## 11. Quick decision flow

```
New company to add
  │
  ├─ Is it on a standard platform? (check the careers page's network calls, §1)
  │     YES → paste the URL in the app. DONE. No code.
  │     NO  ↓
  │
  ├─ Capture the real jobs request + response (user, in browser).
  │
  ├─ Which pattern? (§5)
  │     JSON GET/POST ............... Pattern A
  │     JSON needing warm-up token .. Pattern B
  │     Server-rendered HTML ........ Pattern C
  │     Next.js static data file .... Pattern D
  │
  ├─ Write connector (against the capture) → returns _job() dicts → _merge_by_id.
  ├─ Wire detect.py (+ market_scope.py if global & server-filterable).
  ├─ Write test_<name>.py (mock the network, serve the captured shape).
  ├─ Run new test + all regressions in-sandbox; validate parser vs real body.
  ├─ User mv's files, runs LIVE, eyeballs count vs careers page. ← real checkpoint
  └─ Paste URL in app. DONE.

Can't get a clean entry point (Cloudflare wall, login required, site mid-rebuild)?
  → PARK it. Honest non-build beats a fragile hack. (Bloomsbury, Penguin.)
```
