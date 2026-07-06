# JobWatch

A small, local tool that watches companies' careers pages and tells you what roles are
**new** and **removed** since you last checked — organised into buckets you define,
filtered by location, with longer-term hiring trends and a tracker for the jobs you've
applied to.

It runs entirely on your own Mac. No cloud, no account, no subscription, no cost, and it
is **not AI-powered** — it's plain, predictable code that reads job boards and compares
what it finds.

---

## What it does

- **Tracks companies' careers pages.** Add a company once; JobWatch reads its live job
  list each time you run a check.
- **Tells you what changed.** Every check is compared against the previous one, so you
  see exactly which roles are **new** and which have been **removed** — never guessed,
  always computed from a stable job ID.
- **Organises companies into buckets.** Group the companies you care about however you
  like (by industry, priority, location — your call), with optional sub-groups inside a
  bucket. Run a whole bucket, a sub-group, or a hand-picked selection.
- **Filters by location.** Set the cities you care about and JobWatch narrows every run
  to them, using a built-in offline city database so it works without touching the web
  for geography. Roles tagged only at country level are surfaced honestly rather than
  dropped.
- **Flags what matters to you.** Set ranked keywords, departments, and an experience
  ceiling; matching roles are sorted to the top (or, if you prefer, everything else is
  hidden). Nothing you'd want to see is ever silently removed.
- **Shows hiring trends.** A Trends screen charts how open roles rise and fall over time
  — per company or across a bucket, by team or by location — with your job hunt split
  into phases so each stretch reads clearly.
- **Tracks your applications.** Star roles as you read a run, mark them applied, and
  follow each one through a simple pipeline (applied → screening → interview → offer, plus
  ghosted / rejected / withdrawn), with a weekly view of how many you've sent.
- **Adapts to how active you are.** The app reskins and reframes itself depending on
  whether you're in an active hunt, a casual watch, or a dormant stretch.

Everything is a local web page that opens in your browser when you launch the app — but
it's all running on your machine.

---

## Mac only

JobWatch is built and tested for **macOS**. You launch it by double-clicking
`JobWatch.command`, which is a Mac shell script. It relies on Python 3, which ships with
macOS. It has **not** been built or tested for Windows or Linux, and the double-click
launcher won't work there as-is.

You do **not** need to install anything else. There are no third-party packages to
download — JobWatch uses only what comes with Python. (The one exception is a developer
tool used to rebuild the bundled city database, which you never need to run for normal
use.)

---

## Your data is private

Everything personal — which companies you track, your check history, your saved and
applied jobs, your interests and settings — is stored in a folder called `~/JobWatchData`,
**outside this code project**. That folder never enters git, and the app creates and
manages it for you. You never have to touch it.

The code project (this folder) contains no personal data. That separation is deliberate
and enforced by the app.

---

## Getting started

1. Make sure you're on a Mac with Python 3 (macOS includes it).
2. Double-click **`JobWatch.command`**. The first launch creates your private data
   folder and opens JobWatch in your browser.
3. Add a company (paste its careers-page URL), put it in a bucket, set your locations and
   interests, and run your first check.

That's it — no setup, no accounts, no keys.

---

## Which job boards work automatically

Most companies run their careers page on a small number of standard recruiting platforms.
If a company is on one of these, adding it is just **paste the URL** — JobWatch recognises
the platform, sets itself up, and shows you real job titles as proof before you save:

- **Greenhouse**
- **Lever**
- **Ashby**
- **SmartRecruiters**
- **Workable**
- **Workday**
- **Eightfold**
- **Pinpoint**

A handful of large companies that run their own custom careers sites are also supported
out of the box, because the work to read them was done in advance — for example **Apple**
and **Google**. (These two run big, heavily-defended sites, so JobWatch reads them slowly
and politely; a run may occasionally need a retry.)

Beyond those, a number of other custom sites have already been added — including Uber,
Spotify, Deliveroo, Soho House, the V&A, and the National Gallery, among others.

---

## Adding a company on a custom careers site

Some companies don't use a standard platform and aren't already built in — they run their
own bespoke careers site. JobWatch can't read one of these on its own the first time: a
small piece of code (a "connector") has to be written so the app knows how to read that
particular site's job list.

**This step needs an AI coding agent.** JobWatch can't add this capability to itself —
writing a new connector means new code and a restart. But you don't need to write it
yourself: the app prepares almost everything, and an AI coding agent does the actual
coding from what it prepares.

Here's the recommended way to do it:

**1. Let the app gather the details (the Guide).**
Open the **Guide** screen in JobWatch. It walks you step by step through inspecting the
company's careers page in your browser and capturing a few details about how the site
serves its job list. When you're done, the Guide produces a ready-to-hand-over **brief**
(a markdown file) describing exactly what was captured.

**2. Hand the brief and the reference files to an AI coding agent.**
Give the agent the brief from step 1, plus these files from this project:

- **`CONNECTOR_PLAYBOOK.md`** — the complete instructions for writing a JobWatch
  connector. This is the main reference; it tells the agent everything it needs.
- **`jobwatch/connectors.py`** — where the connector code goes (so it reuses the existing
  helpers and matches the style).
- **`jobwatch/detect.py`** — so a pasted URL for the new site is recognised automatically.
- **`jobwatch/DATA_FORMATS.md`** — the fixed shape every connector must return.
- **`jobwatch/geo.py`** and **`jobwatch/filters.py`** — reference only, for how locations
  are handled.
- **`jobwatch/market_scope.py`** — only if it's a large global site that should be
  narrowed to your chosen cities at the source.
- One existing test file (e.g. **`tests/test_uber.py`**) as a pattern to copy.

The playbook lists this same set, so the agent can confirm it has what it needs.

**3. Add the connector to the project.**
Put the code the agent writes into `jobwatch/`, and put the test file the agent writes
into `tests/`.

**4. Check it works — first in the terminal, then in the app.**
Before trusting it, confirm the new connector actually reads the site. The agent's tests
prove it *parses* the data correctly, but only a live run proves it can *reach and fetch*
the real site, so do both:

- **In the terminal**, from the project folder, run the new connector against the real
  careers page and eyeball the count and a few titles against what the site shows. The
  `CONNECTOR_PLAYBOOK.md` gives the exact one-line command for this (and for running the
  tests). If it returns a sensible number of roles that match the live page, the fetch
  works.
- **In the app**, add the company (paste its URL) and run a check. If its jobs come
  through like any other company's, you're done.

If the terminal run errors or returns zero while the site clearly has jobs, the problem is
in reaching the site (host, headers, or pagination), not in reading it — hand that back to
the agent with what you saw.

Once it's confirmed, adding *other* companies on that same site afterwards is instant, no
agent required.

`jobwatch/DATA_FORMATS.md` (referenced above) documents the exact shape of every data
file the app reads and writes. You don't need it for normal use — it's there for anyone,
human or AI agent, working on the code.
