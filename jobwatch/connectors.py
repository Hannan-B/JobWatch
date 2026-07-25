"""
connectors.py  (Phase A)
========================
A "connector" knows how to read the list of open jobs from one TYPE of careers
board (Greenhouse, Eightfold, Apple, Google, ...). Every connector returns the
SAME clean shape, so the rest of the app never cares which company it came from.

The shared job shape (locked in DATA_FORMATS.md):
    {
        "id":         stable unique string for the role,
        "title":      job title,
        "location":   human-readable location string,
        "department": team/department, or "" if the board doesn't provide it,
        "url":        link to view/apply,
    }

To support a new board type later: write one function that returns that shape,
then register it in CONNECTORS at the bottom. Nothing else changes.
"""

import json
import time
import random
import re
import unicodedata
import urllib.request
import urllib.parse
import urllib.error


# --- A.2: the safe HTTP helper -------------------------------------------
# "Slow and safe" posture lives here. We identify as a normal browser, set a
# sensible timeout, and (optionally) pause politely before a request.

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT_SECONDS = 30


class ConnectorError(Exception):
    """Raised when a connector cannot read jobs (bad config, blocked, etc.).
    Carries a plain-language message safe to show the user."""


def _polite_pause():
    """A short, randomized human-paced wait. Used by the orchestrator later;
    harmless to call here. Keeps us from hammering a site."""
    time.sleep(random.uniform(1.0, 2.5))


def http_get(url, headers=None):
    """GET a URL and return the text body, or raise a friendly ConnectorError."""
    req = urllib.request.Request(url, headers=headers or BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise ConnectorError(
                "This site refused the request (403). It may be blocking "
                "automated access right now. Try again later."
            ) from e
        if e.code == 404:
            raise ConnectorError(
                "Nothing found at that address (404). The board token may be wrong."
            ) from e
        raise ConnectorError(f"The site returned an error ({e.code}).") from e
    except urllib.error.URLError as e:
        raise ConnectorError(f"Couldn't reach the site: {e.reason}") from e
    except Exception as e:
        raise ConnectorError(f"Unexpected problem fetching jobs: {e}") from e


def http_post_json(url, payload, headers=None):
    """POST JSON and return the text body, or raise a friendly ConnectorError."""
    body = json.dumps(payload).encode("utf-8")
    h = dict(headers or BROWSER_HEADERS)
    h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise ConnectorError(f"The site returned an error ({e.code}).") from e
    except urllib.error.URLError as e:
        raise ConnectorError(f"Couldn't reach the site: {e.reason}") from e
    except Exception as e:
        raise ConnectorError(f"Unexpected problem fetching jobs: {e}") from e


def _job(id_, title, location, department, url):
    """Build a job in the locked shared shape, with safe defaults."""
    return {
        "id": str(id_) if id_ is not None else "",
        "title": (title or "").strip(),
        "location": (location or "").strip(),
        "department": (department or "").strip(),
        "url": (url or "").strip(),
    }


def _merge_by_id(jobs):
    """Collapse jobs that share the same id, MERGING their distinct locations.

    Some boards (Apple, confirmed) list a single multi-site role once PER location
    — same id, same title, different `location`. Keeping every row would inflate
    counts and destabilise new/removed detection (a role's row-count could shift
    between checks). We keep ONE record per id, preserving the first occurrence's
    fields but joining the distinct location strings in first-seen order, so a role
    open in Cambridge + London + Saint Albans becomes one job tagged
    "Cambridge; London; Saint Albans". The app's city filter still matches any of
    them (each city appears in the merged string).

    Roles with a blank id are passed through unchanged (no safe key to merge on).
    Order is preserved: each id keeps the position of its FIRST occurrence.
    """
    order = []                 # ids in first-seen order
    by_id = {}                 # id -> merged job dict
    locs_seen = {}             # id -> list of distinct location parts (ordered)
    passthrough = []           # (index, job) for blank-id roles, kept as-is
    for idx, j in enumerate(jobs):
        jid = j.get("id") or ""
        if not jid:
            passthrough.append(j)
            continue
        loc = (j.get("location") or "").strip()
        if jid not in by_id:
            by_id[jid] = dict(j)          # copy the first occurrence
            order.append(jid)
            locs_seen[jid] = []
        # Collect distinct, non-empty location parts (a row's location may itself
        # already be a multi-part "A; B" string — split and add each part).
        for part in [p.strip() for p in loc.split(";") if p.strip()]:
            if part not in locs_seen[jid]:
                locs_seen[jid].append(part)
    # Rebuild merged jobs with joined locations.
    merged = []
    for jid in order:
        job = by_id[jid]
        if locs_seen[jid]:
            job["location"] = "; ".join(locs_seen[jid])
        merged.append(job)
    # Blank-id roles keep their original relative position at the end is acceptable
    # (they have no identity to interleave on); append them after the merged set.
    merged.extend(passthrough)
    return merged


# --- A.3: Greenhouse (Ogilvy uses this — our proven connector) -----------

def greenhouse(config):
    """
    config = {"board": "<token>"}   e.g. Ogilvy UK -> "ogilvyuk"
    Public JSON feed, no key needed. Includes department info.
    """
    token = config["board"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    data = json.loads(http_get(url))
    jobs = []
    for j in data.get("jobs", []):
        # Greenhouse exposes department(s) under "departments" when content=true.
        dept = ""
        depts = j.get("departments") or []
        if depts:
            dept = depts[0].get("name", "")
        jobs.append(_job(
            id_=j.get("id"),
            title=j.get("title"),
            location=(j.get("location") or {}).get("name", ""),
            department=dept,
            url=j.get("absolute_url"),
        ))
    return jobs


# --- A.4: Eightfold (Netflix uses this) ----------------------------------

def eightfold(config):
    """
    Eightfold-powered careers boards (Netflix and many large employers).

    config needs:
        "host":   the careers host, e.g. "explore.jobs.netflix.net"
        "domain": the company web domain Eightfold keys on, e.g. "netflix.com"

    Why two values? Eightfold serves the public job list from the careers HOST,
    but filters by a DOMAIN parameter that is usually the company's main website.
    We pull both from the careers URL during the add-company flow.

    Tries the common "SmartApply" endpoint first, then the "PCSX" one as a
    fallback, since different Eightfold tenants use different patterns.
    Paginates 100 at a time. No login needed for the public list.
    """
    host = config["host"].replace("https://", "").replace("http://", "").strip("/")
    domain = config.get("domain", "").strip() or host

    def parse_positions(positions):
        out = []
        for j in positions:
            url = j.get("canonicalPositionUrl")
            if not url:
                pid = j.get("id") or j.get("ats_job_id")
                url = f"https://{host}/careers/job/{pid}" if pid else ""
            out.append(_job(
                id_=j.get("ats_job_id") or j.get("id"),
                title=j.get("name"),
                location=j.get("location"),
                department=j.get("department"),
                url=url,
            ))
        return out

    # --- Attempt 1: SmartApply ( /api/apply/v2/jobs ) ---
    # This is the public search endpoint. It paginates with start/num and reports
    # the GRAND TOTAL in "count" (older tenants used "totalJobs"; we read either).
    # Confirmed shape (Netflix, live-captured): the list endpoint returns
    #   { "positions": [...], "count": <grand total>, "facets": {...}, ... }
    # and honours ?start=&num=&sort_by=relevance. num=100 fetches a small market
    # (e.g. London ~10) in a single request; large boards page in 100s.
    jobs = []
    start = 0
    page = 100
    total = None
    smartapply_worked = False
    while True:
        url = (f"https://{host}/api/apply/v2/jobs"
               f"?domain={domain}&start={start}&num={page}&sort_by=relevance")
        try:
            data = json.loads(http_get(url))
        except ConnectorError:
            break
        positions = data.get("positions", [])
        if positions:
            smartapply_worked = True
        jobs.extend(parse_positions(positions))
        # Grand total lives in "count" on the current tenant; "totalJobs" is the
        # legacy name. A short page (fewer than we asked for) also means we're done.
        if total is None:
            total = data.get("count")
            if total is None:
                total = data.get("totalJobs")
        got = len(positions)
        start += page
        if not positions or got < page:
            break
        if total is not None and start >= total:
            break

    if smartapply_worked:
        return jobs

    # --- Attempt 2: PCSX ( /api/pcsx/search ) ---
    jobs = []
    start = 0
    while True:
        url = (f"https://{host}/api/pcsx/search"
               f"?domain={domain}&start={start}&num={page}")
        data = json.loads(http_get(url))  # let errors surface if this also fails
        positions = (data.get("positions")
                     or data.get("data", {}).get("positions", [])
                     or [])
        jobs.extend(parse_positions(positions))
        total = data.get("totalJobs") or data.get("count") or 0
        start += page
        if start >= total or not positions:
            break
    return jobs


# --- A.5: Apple (best-effort preset) -------------------------------------

def apple(config):
    """
    Apple Jobs.  Real endpoint discovered via browser inspection:
        POST https://jobs.apple.com/api/v1/search

    Apple protects this endpoint with a CSRF token + session cookie that are
    handed out when you load the careers page. So this connector does a
    two-step dance, exactly like a browser:
        1) GET the careers page -> harvest the CSRF token (from a <meta> tag)
           and the session cookie.
        2) POST the search with that token + cookie attached.

    config (all optional):
        "location": Apple location code, e.g. "postLocation-GBR" for the UK.
                    Leave blank for all locations.
        "query":    keyword search, e.g. "engineer". Blank = everything.
        "locale":   defaults to "en-gb".

    BEST-EFFORT: Apple may change the token mechanism; if so this fails softly
    with a clear message and we re-inspect. Works from a normal machine.
    """
    import http.cookiejar

    locale = config.get("locale", "en-gb")
    page_url = f"https://jobs.apple.com/{locale}/search"
    token_url = "https://jobs.apple.com/api/v1/CSRFToken"
    api_url = "https://jobs.apple.com/api/v1/search"

    # A cookie jar + opener so the session cookie is carried across all steps.
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    # --- Step 1: load the careers page to establish a session cookie ---
    page_headers = dict(BROWSER_HEADERS)
    page_headers["Accept"] = "text/html,application/xhtml+xml"
    try:
        req = urllib.request.Request(page_url, headers=page_headers)
        with opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
            resp.read()  # we only need the cookie it sets
    except Exception as e:
        raise ConnectorError(
            f"Couldn't load Apple's careers page to start the search: {e}"
        ) from e

    # --- Step 2: get a fresh CSRF token from the dedicated endpoint ---
    # Apple returns the token in the RESPONSE HEADER 'x-apple-csrf-token',
    # not in the body.
    token_headers = dict(BROWSER_HEADERS)
    token_headers["Referer"] = page_url
    try:
        req = urllib.request.Request(token_url, headers=token_headers)
        with opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
            csrf_token = (resp.headers.get("x-apple-csrf-token")
                          or resp.headers.get("X-Apple-CSRF-Token"))
    except Exception as e:
        raise ConnectorError(f"Couldn't get Apple's security token: {e}") from e

    if not csrf_token:
        raise ConnectorError(
            "Called Apple's token endpoint but it didn't return a token header. "
            "Apple may have changed their site; we'll need to re-inspect."
        )

    # --- Step 2: POST the search, paging through ALL results --------------
    # Phase O FIX: the old loop stopped at `page > 100` (= 2000 roles at 20/page),
    # which truncated Apple's true global total and could hide late-sorted London
    # roles. Now that we scope by country (UK ≈ 118), the total is small and we page
    # to Apple's own `totalRecords`. The high safety ceiling only guards against a
    # runaway when UNscoped; a failed page is RETRIED once before we stop, instead
    # of silently returning short.
    jobs = []
    page = 1
    total = None
    PAGE_SIZE = 20            # Apple's page size
    MAX_PAGES = 1000          # safety ceiling FAR above any real country total
    while True:
        body = {
            "query": config.get("query", ""),
            "filters": {},
            "page": page,
            "locale": locale,
            "sort": "",
            "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"},
        }
        # Phase O — scope the fetch by COUNTRY at the source. The market scoper
        # (market_scope.scoped_config) sets config["location_list"] to one or
        # more "postLocation-<ISO3>" codes (e.g. ["postLocation-GBR"], or
        # ["postLocation-GBR","postLocation-USA"] for a London+New York filter).
        # Apple's filters.locations is a LIST, so the union is one request — no
        # extra fetches for multiple countries. Falls back to the single
        # config["location"] for backward-compatibility / a hand-set preset code.
        loc_list = config.get("location_list")
        if isinstance(loc_list, list) and loc_list:
            body["filters"]["locations"] = [str(c) for c in loc_list if str(c).strip()]
        else:
            loc = config.get("location", "")
            if loc:
                body["filters"]["locations"] = [loc]

        data_bytes = json.dumps(body).encode("utf-8")
        post_headers = dict(BROWSER_HEADERS)
        post_headers["Content-Type"] = "application/json"
        post_headers["X-Apple-CSRF-Token"] = csrf_token
        post_headers["Origin"] = "https://jobs.apple.com"
        post_headers["Referer"] = page_url

        # Fetch this page, with ONE retry on a transient failure before giving up
        # (Phase O: a single failed page must not silently truncate the result).
        payload = None
        for attempt in (1, 2):
            try:
                req = urllib.request.Request(api_url, data=data_bytes,
                                             headers=post_headers, method="POST")
                with opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                break
            except urllib.error.HTTPError as e:
                if attempt == 2:
                    raise ConnectorError(
                        f"Apple's search refused the request ({e.code}). "
                        "The security token may have expired; try again."
                    ) from e
                _polite_pause()
            except Exception as e:
                if attempt == 2:
                    raise ConnectorError(f"Problem searching Apple jobs: {e}") from e
                _polite_pause()

        # Apple returns results under res.searchResults (confirmed live).
        res = payload.get("res") or {}
        roles = res.get("searchResults") or []
        if total is None:
            total = res.get("totalRecords", 0) or 0
        if not roles:
            break

        for j in roles:
            locs = j.get("locations", [])
            loc_str = ", ".join(x.get("name", "") for x in locs if isinstance(x, dict)) \
                if isinstance(locs, list) else str(locs)
            team = j.get("team") or {}
            dept = team.get("teamName", "") if isinstance(team, dict) else ""
            pid = j.get("positionId") or j.get("id")
            slug = j.get("transformedPostingTitle") or ""
            title = j.get("postingTitle") or j.get("title") or ""
            # Apple detail URLs are /<locale>/details/<positionId>/<slug>
            detail = f"https://jobs.apple.com/{locale}/details/{pid}"
            if slug:
                detail += f"/{slug}"
            jobs.append(_job(
                id_=pid,
                title=title,
                location=loc_str,
                department=dept,
                url=detail,
            ))

        # Stop when we've collected the reported total, or a page came back short
        # (fewer than a full page = the last page). Page to the REAL total now,
        # not an arbitrary 100-page cap. NOTE: `total` (totalRecords) counts ROWS,
        # and Apple lists a multi-site role once PER location — so 118 rows can be
        # only ~100 distinct roles. We collect all rows here, then merge by id below.
        if (total and len(jobs) >= total) or len(roles) < PAGE_SIZE:
            break
        page += 1
        if page > MAX_PAGES:   # safety ceiling (only ever hit on a huge unscoped run)
            break
        _polite_pause()

    # Phase O — MERGE multi-site duplicates. Apple returns the same role (same id)
    # once per location it's open in (e.g. a GPU role listed under Cambridge AND
    # London AND Saint Albans = 3 rows, 1 real job). Keeping all rows would inflate
    # the count and, worse, destabilise new/removed detection (a role's row-count
    # could change between checks). We collapse by id, MERGING the distinct
    # locations into one "Cambridge; London; Saint Albans" string so the multi-site
    # info is preserved and the app's city filter still matches any of them. This
    # mirrors Google's connector, which already yields multi-location strings.
    jobs = _merge_by_id(jobs)
    return jobs


# --- A.6: Google (best-effort preset) ------------------------------------

def google(config):
    """
    Google Careers. Real endpoint discovered via browser inspection:
        POST .../HiringCportalFrontendUi/data/batchexecute?rpcids=r06xKb

    Google returns data in its internal "batchexecute" format: a security
    prefix ()]}') then a length, then nested arrays containing a JSON STRING
    that itself must be parsed (double-encoded). No login needed for the
    public job list.

    config (all optional):
        "location": a location search string, e.g. "London, UK". Blank = all.

    BEST-EFFORT and the most fragile connector — Google reshapes this format
    without notice. Fails softly if the structure changes.
    """
    base = ("https://www.google.com/about/careers/applications/_/"
            "HiringCportalFrontendUi/data/batchexecute")
    params = {
        "rpcids": "r06xKb",
        "source-path": "/about/careers/applications/jobs/results",
        "bl": "boq_corp-hiring-boq-cportal-frontend_20260610.00_p0",
        "hl": "en",
        "soc-app": "1", "soc-platform": "1", "soc-device": "1",
        "_reqid": "100000", "rt": "c",
    }
    url = base + "?" + urllib.parse.urlencode(params)
    location = config.get("location", "")

    headers = dict(BROWSER_HEADERS)
    headers["Content-Type"] = "application/x-www-form-urlencoded;charset=utf-8"
    headers["Origin"] = "https://www.google.com"
    headers["Referer"] = "https://www.google.com/about/careers/applications/jobs/results"
    headers["X-Same-Domain"] = "1"

    # Phase O — Google's batchexecute 2-letter codes, kept ONLY as a fallback for
    # the rare role missing the structured location at entry[9]. The primary path
    # now reads entry[9], which carries the real city (probe-confirmed).
    COUNTRY = {
        "GB": "United Kingdom", "US": "United States", "IE": "Ireland",
        "DE": "Germany", "FR": "France", "NL": "Netherlands", "CH": "Switzerland",
        "IN": "India", "SG": "Singapore", "JP": "Japan", "CA": "Canada",
        "AU": "Australia", "ES": "Spain", "IT": "Italy", "PL": "Poland",
        "TW": "Taiwan", "IL": "Israel", "BR": "Brazil", "CN": "China",
        "VN": "Vietnam", "KR": "South Korea", "MX": "Mexico", "BE": "Belgium",
        "MY": "Malaysia", "TH": "Thailand", "AR": "Argentina", "FI": "Finland",
        "SE": "Sweden", "AE": "United Arab Emirates", "AT": "Austria",
        "CL": "Chile", "CZ": "Czechia", "GR": "Greece", "HK": "Hong Kong",
        "ID": "Indonesia", "PH": "Philippines", "QA": "Qatar", "SV": "El Salvador",
    }

    def _location_from_entry9(entry):
        """Phase O — read the REAL location from entry[9].

        Probe-confirmed shape: entry[9] is a LIST of location objects, each:
            ['New York, NY, USA', [address...], 'New York', null, 'NY', 'US']
             [0]=display string     [1]=address  [2]=city    [3]   [4]=region [5]=ISO2
        A role can have several (multi-location). We build a readable, city-first
        string joining each location's display ([0]), which contains the city —
        so the app's city filter matches it directly (the whole Phase-O point).
        Returns "" if entry[9] is absent/empty (caller falls back to the loc= code).
        """
        if len(entry) <= 9 or not isinstance(entry[9], list):
            return ""
        parts = []
        for locobj in entry[9]:
            if not isinstance(locobj, list) or not locobj:
                continue
            display = locobj[0] if isinstance(locobj[0], str) else ""
            if not display:
                # Reconstruct from city + region + country if no display string.
                city = locobj[2] if len(locobj) > 2 and isinstance(locobj[2], str) else ""
                region = locobj[4] if len(locobj) > 4 and isinstance(locobj[4], str) else ""
                display = ", ".join(p for p in (city, region) if p)
            if display and display not in parts:
                parts.append(display)
        return "; ".join(parts)

    def parse_jobs(data):
        out = []
        if not data or not isinstance(data, list):
            return out, 0
        job_list = data[0] if isinstance(data[0], list) else []
        total = data[2] if len(data) > 2 and isinstance(data[2], int) else len(job_list)
        for entry in job_list:
            if not isinstance(entry, list) or len(entry) < 2:
                continue
            jid = entry[0]
            title = entry[1] if isinstance(entry[1], str) else ""
            url_field = entry[2] if len(entry) > 2 and isinstance(entry[2], str) else ""
            # PRIMARY: the real city/location from entry[9].
            loc_str = _location_from_entry9(entry)
            # FALLBACK: the old country-code scrape, only if entry[9] gave nothing.
            if not loc_str:
                loc_code = ""
                if "loc=" in url_field:
                    q = urllib.parse.parse_qs(
                        urllib.parse.urlparse(url_field.replace("\\u0026", "&")).query)
                    loc_code = q.get("loc", [""])[0]
                loc_str = COUNTRY.get(loc_code, loc_code)
            public_url = (f"https://www.google.com/about/careers/applications/"
                          f"jobs/results/{jid}" if jid else url_field)
            if title:
                out.append(_job(
                    id_=jid, title=title, location=loc_str,
                    department="", url=public_url,
                ))
        return out, total

    def peel(raw):
        cleaned = raw[4:] if raw.startswith(")]}'") else raw
        cleaned = cleaned.lstrip()
        dec = json.JSONDecoder()
        idx, n = 0, len(cleaned)
        while idx < n:
            while idx < n and (cleaned[idx].isspace() or cleaned[idx].isdigit()):
                idx += 1
            if idx >= n:
                break
            try:
                value, end = dec.raw_decode(cleaned, idx)
            except Exception:
                break
            idx = end
            if isinstance(value, list):
                for env in value:
                    if isinstance(env, list) and len(env) >= 3 and env[0] == "wrb.fr":
                        return json.loads(env[2])
        return None

    def fetch_page(page_num, loc_value):
        """Fetch one page. loc_value is a single Google location code (e.g. "GB")
        or "" for all locations. The trailing number in the payload is the page."""
        if loc_value:
            inner = (f'[[null,null,null,null,"en",null,'
                     f'[["{loc_value}"]],{page_num}]]')
        else:
            inner = f'[[null,null,null,null,"en",null,null,{page_num}]]'
        freq = json.dumps([[["r06xKb", inner, None, "3"]]])
        body = urllib.parse.urlencode({"f.req": freq}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return raw

    def _peel_with_retry(page_num, loc_value):
        """Fetch+peel one page, retrying ONCE on a transient failure before giving
        up (Phase O: a single failed page must not silently truncate results)."""
        for attempt in (1, 2):
            try:
                return peel(fetch_page(page_num, loc_value))
            except Exception:
                if attempt == 2:
                    return None
                _polite_pause()
        return None

    def _fetch_one_location(loc_value):
        """Page through ALL results for a single Google location code (or "" for
        all). Returns the list of jobs. Hardened: retries a failed page, pages to
        the reported total, and has a safety ceiling."""
        collected = []
        first = _peel_with_retry(1, loc_value)
        if first is None:
            # The very first page failing for a location is a real problem only if
            # it happens for ALL of them; the driver decides. Return empty here.
            return collected, None
        page_jobs, total = parse_jobs(first)
        collected.extend(page_jobs)
        page = 2
        while len(collected) < total and page_jobs:
            _polite_pause()
            more = _peel_with_retry(page, loc_value)
            if more is None:
                break  # one page failed even after a retry — stop politely
            page_jobs, _ = parse_jobs(more)
            if not page_jobs:
                break
            collected.extend(page_jobs)
            page += 1
            if page > 200:  # safety ceiling (far above any country total)
                break
        return collected, total

    # --- Phase O — multi-country driver ----------------------------------
    # The market scoper sets config["location_list"] to one or more ISO-2 codes
    # (e.g. ["GB"] or ["GB","US","IE"] for a London+New York+Dublin filter). We
    # page each country and union the results, de-duped by role id. Falls back to
    # the single config["location"], or "" (all locations) when nothing is set.
    loc_list = config.get("location_list")
    if isinstance(loc_list, list) and loc_list:
        targets = [str(c) for c in loc_list if str(c).strip()]
    elif location:
        targets = [location]
    else:
        targets = [""]   # no scope → fetch everything (today's behaviour)

    jobs = []
    any_ok = False
    first_error = None
    for t in targets:
        try:
            part, total = _fetch_one_location(t)
        except urllib.error.HTTPError as e:
            first_error = first_error or e
            continue
        except Exception as e:
            first_error = first_error or e
            continue
        if total is not None:
            any_ok = True
        jobs.extend(part)

    # Phase O — dedupe by id, merging locations (same helper Apple uses). Google's
    # entry[9] already lists all of a role's locations in one row, so within a
    # single country query there are no dupes; but a role returned under TWO country
    # queries collapses here, and if the two rows happened to carry different
    # location strings they MERGE (rather than one silently winning). Idempotent for
    # the common single-country case.
    jobs = _merge_by_id(jobs)

    # Only raise if EVERY target failed to produce a usable response — a single
    # country failing shouldn't sink a multi-country run.
    if not any_ok and not jobs:
        if first_error is not None:
            raise ConnectorError(
                f"Problem fetching Google jobs: {first_error}") from first_error
        raise ConnectorError(
            "Got a response from Google but couldn't find the job data inside it. "
            "Google may have changed their format; we'll need to re-inspect."
        )

    return jobs


# --- Bonus standard providers (A.8) --------------------------------------

def lever(config):
    """config = {"board": "<token>"}  e.g. jobs.lever.co/<token>"""
    token = config["board"]
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    data = json.loads(http_get(url))
    jobs = []
    for j in data:
        cats = j.get("categories", {}) or {}
        jobs.append(_job(
            id_=j.get("id"),
            title=j.get("text"),
            location=cats.get("location"),
            department=cats.get("team") or cats.get("department"),
            url=j.get("hostedUrl"),
        ))
    return jobs


def ashby(config):
    """config = {"board": "<token>"}

    Ashby lists a role's extra cities in `secondaryLocations` (each entry is
    {"location": "<city>", "address": {...}}), separate from the primary
    `location`. The old connector read only the primary, so a role open in
    several cities matched ONLY its primary city — e.g. a "San Francisco +
    Boston + New York" role vanished when the user filtered to Boston. That is
    the Lovable undercount: 72 roles in the feed, 21 of them multi-location.

    Fix: fold the primary + all secondary cities into one "; "-joined location
    string (the same multi-site convention used by the Uber connector and
    _merge_by_id), trimming stray whitespace (the feed has e.g. "San Francisco ").
    The app's city filter then matches ANY listed city. Counting is unchanged:
    still one job per posting id — we are not inflating the role count, only
    making each role's full set of cities visible to the filter.
    """
    token = config["board"]
    url = "https://api.ashbyhq.com/posting-api/job-board/" + urllib.parse.quote(token)
    data = json.loads(http_get(url))

    def _locations(j):
        """Primary + secondary cities, de-duplicated, order-preserving, trimmed."""
        parts = []
        primary = (j.get("location") or "").strip()
        if primary:
            parts.append(primary)
        for sec in (j.get("secondaryLocations") or []):
            if not isinstance(sec, dict):
                continue
            city = (sec.get("location") or "").strip()
            if city and city not in parts:
                parts.append(city)
        return "; ".join(parts)

    jobs = []
    for j in data.get("jobs", []):
        jobs.append(_job(
            id_=j.get("id"),
            title=j.get("title"),
            location=_locations(j),
            department=j.get("department") or j.get("team"),
            url=j.get("jobUrl") or j.get("applyUrl"),
        ))
    return jobs


def uber(config):
    """Uber Careers (jobs.uber.com) — a single-company custom board.

    Phase O Part 2 build. Uber's search API matches jobs by GEOGRAPHIC
    COORDINATES (lat/lng) + radius, NOT by free-text city — typing "London"
    returns nothing; you must pass London's lat/lng (confirmed live: the working
    request carried lat=51.5072178&lng=-0.1275862). So this connector behaves like
    Apple/Google's scoping but with COORDINATES: it reads the run's chosen cities
    (config["location_list"], a list of city names) and fetches each city's coords
    from CITY_COORDS below. A city not in the table is skipped with a note; if no
    chosen city resolves (or none given) it falls back to a broad fetch.

    Response shape (confirmed live): {"jobs":[ { Id, Title, Teams[], Locations[]
    {Address, City, Region, Country}, Urls[]{Url,IsDefault} } ]}. No total count
    field, so we page until a short page (fewer than pagesize = the last page).
    The app's location filter still runs on top as the final authority.
    """
    # City -> (lat, lng). Seeded with London (the confirmed working coords).
    # Extend this one-liner per city you want Uber roles for. Keys are lowercase.
    CITY_COORDS = {
        "london": (51.5072178, -0.1275862),
        # "new york": (40.7127753, -74.0059728),
        # "dublin":   (53.3498053, -6.2603097),
        # "paris":    (48.8566969, 2.3514616),
        # "amsterdam":(52.3675734, 4.9041389),
    }
    RADIUS = 100      # miles — Uber's own default radius for a city search
    PAGE_SIZE = 100   # ask for 100/page; page until a short page comes back
    MAX_PAGES = 200   # safety ceiling

    base = "https://jobs.uber.com/api/jobs/search/"  # trailing slash avoids a 308

    def _parse_location(loc_objs):
        """Build the best location string from a role's Locations[] list.
        Prefer the ready-made Address; else assemble City/Region/Country; join
        multiple locations with '; ' so a multi-site role keeps them all."""
        parts = []
        for lo in (loc_objs or []):
            if not isinstance(lo, dict):
                continue
            s = (lo.get("Address") or "").strip()
            if not s:
                bits = [(lo.get("City") or "").strip(),
                        (lo.get("Region") or "").strip(),
                        (lo.get("Country") or "").strip()]
                s = ", ".join(b for b in bits if b)
            if s and s not in parts:
                parts.append(s)
        return "; ".join(parts)

    def _parse_url(url_objs):
        """Pick the default (or first) URL and make it absolute."""
        chosen = ""
        for u in (url_objs or []):
            if not isinstance(u, dict):
                continue
            if u.get("IsDefault") and u.get("Url"):
                chosen = u["Url"]; break
            if not chosen and u.get("Url"):
                chosen = u["Url"]
        if chosen and chosen.startswith("/"):
            return "https://jobs.uber.com" + chosen
        return chosen

    def _fetch_at(lat, lng, location_name):
        """Page through all roles for one coordinate, returning a job list."""
        out = []
        page = 1
        while True:
            params = {"locale": "en", "page": page, "pagesize": PAGE_SIZE}
            if lat is not None and lng is not None:
                params.update({"location": location_name or "",
                               "lat": lat, "lng": lng, "radius": RADIUS})
            qs = urllib.parse.urlencode(params)
            try:
                raw = http_get(base + "?" + qs)
            except Exception:
                # one retry before giving up on this page (don't truncate silently)
                _polite_pause()
                try:
                    raw = http_get(base + "?" + qs)
                except Exception:
                    break
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                break
            roles = data.get("jobs", []) if isinstance(data, dict) else []
            if not roles:
                break
            for j in roles:
                if not isinstance(j, dict):
                    continue
                jid = j.get("Id") or j.get("Reference")
                title = j.get("Title") or ""
                if not (jid and title):
                    continue
                teams = j.get("Teams") or []
                dept = teams[0] if isinstance(teams, list) and teams else ""
                out.append(_job(
                    id_=jid,
                    title=title,
                    location=_parse_location(j.get("Locations")),
                    department=dept,
                    url=_parse_url(j.get("Urls")),
                ))
            if len(roles) < PAGE_SIZE:
                break          # short page = last page (Uber reports no total)
            page += 1
            if page > MAX_PAGES:
                break
            _polite_pause()
        return out

    # Decide what coordinates to fetch, from the run's chosen cities.
    loc_list = config.get("location_list")
    if not (isinstance(loc_list, list) and loc_list):
        single = config.get("location")
        loc_list = [single] if single else []

    targets = []          # (lat, lng, name)
    unresolved = []
    for city in loc_list:
        coords = CITY_COORDS.get((city or "").strip().lower())
        if coords:
            targets.append((coords[0], coords[1], city))
        else:
            unresolved.append(city)

    jobs = []
    if targets:
        for lat, lng, name in targets:
            jobs.extend(_fetch_at(lat, lng, name))
    else:
        # No resolvable city (or none chosen) -> broad fetch; the app filter
        # narrows afterwards. (Uber may default to a location-less result set.)
        jobs.extend(_fetch_at(None, None, None))

    # Collapse any role returned under more than one city, merging locations.
    return _merge_by_id(jobs)


def spotify(config):
    """Spotify Careers (lifeatspotify.com) — a single-company custom board.

    Phase O Part 2 build. Spotify is a Next.js site whose job list is loaded
    client-side; the `_next/data/<buildId>/jobs.json` file holds ONLY the filter
    sidebar, and the page calls a separate endpoint for the rows. The exact live
    endpoint resisted capture (the careers site was intermittently showing
    "0 jobs" during inspection), so this connector is built to SELF-DIAGNOSE on
    the user's Mac (which can reach Spotify, unlike the sandbox):

      1. Fetch the /jobs page and read the CURRENT buildId from __NEXT_DATA__
         (so we never hardcode the hash, which changes on every redeploy — the
         fragility the brief warns about).
      2. Try a small ordered list of candidate endpoints, using the FIRST that
         returns real job rows. Each candidate is tried GET then POST.
      3. Parse the confirmed job shape (from the live index.json):
            { id, text (title), category{name}, locations[]{location} }
         building the apply URL as lifeatspotify.com/jobs/<id>.

    If none return rows, raise a clear ConnectorError naming what was tried, so
    the run result tells the user "Spotify served no jobs" rather than failing
    opaquely. The app's location filter narrows to the chosen city afterwards.
    """
    import html as _html

    BASE = "https://www.lifeatspotify.com"
    API = "https://api.lifeatspotify.com"
    JOBS_PAGE = BASE + "/jobs"

    # --- 1. read the live buildId from the /jobs page --------------------
    build_id = None
    try:
        page = http_get(JOBS_PAGE)
        m = re.search(r'"buildId"\s*:\s*"([^"]+)"', page)
        if m:
            build_id = m.group(1)
    except Exception:
        build_id = None   # carry on; some candidates don't need the buildId

    # The chosen-city slugs (Spotify uses slugs like "london", "new-york").
    loc_list = config.get("location_list") or []
    if not loc_list:
        single = config.get("location")
        loc_list = [single] if single else []
    slugs = [str(c).strip().lower().replace(" ", "-") for c in loc_list if str(c).strip()]

    def _slug_variants(slug):
        # Try the city slug and an unscoped fetch.
        return [slug] if slug else [""]

    # --- 2. candidate endpoints, tried in order -------------------------
    # Each is a (method, url, payload-or-None) builder for a given location slug.
    def _candidates(slug):
        cands = []
        loc_q = f"?l={slug}" if slug else ""
        # 0) CONFIRMED LIVE (capture 2026-06-30): the careers site is WordPress;
        #    the real jobs feed is on the api. host, theme namespace "animal".
        #    Plain GET, no token, returns {"result":[...], "main_categories":[...]}.
        #    Tried FIRST.
        cands.append(("GET", f"{API}/wp-json/animal/v1/job/search{loc_q}", None))
        # a) Next.js data file WITH the location query (returns filters today,
        #    but may return rows on a redeploy — cheap to try first).
        if build_id:
            cands.append(("GET", f"{BASE}/_next/data/{build_id}/jobs.json{loc_q}", None))
        # b) The /api/jobs route the page's own perf log referenced.
        cands.append(("GET",  f"{BASE}/api/jobs{loc_q}", None))
        cands.append(("POST", f"{BASE}/api/jobs", {"location": slug} if slug else {}))
        # c) A search-style route (common Next.js careers pattern).
        cands.append(("GET",  f"{BASE}/api/search{loc_q}", None))
        cands.append(("POST", f"{BASE}/api/search", {"location": slug} if slug else {}))
        return cands

    def _extract_jobs(obj):
        """Find a list of job-shaped dicts anywhere in a parsed JSON object.
        A job-shaped dict has an id and a title-ish field (text/title/name) and
        a locations list. Searches recursively so we don't depend on the exact
        envelope (pageProps.jobs vs results vs positions, etc.)."""
        found = []

        def looks_like_job(d):
            if not isinstance(d, dict):
                return False
            has_id = any(k in d for k in ("id", "Id", "jobId"))
            has_title = any(k in d for k in ("text", "title", "Title", "name"))
            return has_id and has_title

        def walk(node):
            if isinstance(node, list):
                # A list where most items look like jobs = the jobs array.
                jobby = [x for x in node if looks_like_job(x)]
                if jobby and len(jobby) >= max(1, len(node) // 2):
                    found.extend(jobby)
                else:
                    for x in node:
                        walk(x)
            elif isinstance(node, dict):
                for v in node.values():
                    walk(v)

        walk(obj)
        # De-dupe by id while preserving order.
        seen, uniq = set(), []
        for j in found:
            jid = str(j.get("id") or j.get("Id") or j.get("jobId") or "")
            if jid and jid not in seen:
                seen.add(jid)
                uniq.append(j)
        return uniq

    def _try(method, url, payload):
        try:
            if method == "GET":
                raw = http_get(url)
            else:
                raw = http_post_json(url, payload or {})
            data = json.loads(raw)
        except Exception:
            return None
        jobs = _extract_jobs(data)
        return jobs or None

    # --- 3. fetch per location, first working candidate wins ------------
    all_raw = []
    tried = []
    working = None      # remember the endpoint that worked, reuse for other slugs
    for slug in (slugs or [""]):
        got = None
        if working:
            # reuse the known-good endpoint shape for subsequent cities
            method, url_tmpl, payload_tmpl = working
            url = url_tmpl.replace("__SLUG__", slug)
            payload = ({"location": slug} if payload_tmpl is not None and slug else payload_tmpl)
            got = _try(method, url, payload)
        if not got:
            for method, url, payload in _candidates(slug):
                tried.append(f"{method} {url}")
                got = _try(method, url, payload)
                if got:
                    working = (method, url.replace(f"l={slug}", "l=__SLUG__")
                                          .replace(f"/{slug}", "/__SLUG__"),
                               payload)
                    break
        if got:
            all_raw.extend(got)

    if not all_raw:
        raise ConnectorError(
            "Spotify returned no job rows from any known endpoint. Their careers "
            "site loads jobs via a client-side call that may have changed or been "
            "temporarily empty. Tried: " + "; ".join(tried[:6]) +
            ". Re-capture the live jobs request from lifeatspotify.com and update "
            "the connector's candidate list."
        )

    # --- 4. parse the confirmed job shape ------------------------------
    jobs = []
    for j in all_raw:
        jid = str(j.get("id") or j.get("Id") or j.get("jobId") or "")
        title = j.get("text") or j.get("title") or j.get("Title") or j.get("name") or ""
        title = _html.unescape(title).strip()
        if not (jid and title):
            continue
        cat = j.get("main_category") or j.get("category") or {}
        dept = cat.get("name", "") if isinstance(cat, dict) else (cat or "")
        locs = j.get("locations") or []
        loc_names = []
        if isinstance(locs, list):
            for lo in locs:
                if isinstance(lo, dict):
                    nm = lo.get("location") or lo.get("name") or lo.get("label") or ""
                else:
                    nm = str(lo)
                if nm and nm not in loc_names:
                    loc_names.append(nm)
        location = "; ".join(loc_names)
        jobs.append(_job(
            id_=jid,
            title=title,
            location=location,
            department=dept,
            url=f"{BASE}/jobs/{jid}",
        ))

    return _merge_by_id(jobs)


def playstation(config):
    """PlayStation Careers (careers.playstation.com) — a single-company custom board.

    PlayStation's careers site runs on Paradox (Olivia). Its get-jobs endpoint
    aggregates across SEVERAL Greenhouse boards (Sony Interactive's master board
    plus per-studio boards like Media Molecule), so no single Greenhouse token
    covers the whole company — we read the unifying Paradox API.

    REQUEST (confirmed live via Copy-as-cURL, 2026-06-29):
        POST https://careers.playstation.com/api/get-jobs?radius=15&page_number=<n>&enable_kilometers=false
        (city scope is a query param: &filter[city][0]=London, url-encoded)
        Content-Type: application/json
        Body: {"disable_switch_search_mode": false,
               "site_available_languages": ["fr-ca","ja","en","en-us"]}
        NB: it is a POST, not a GET — a GET 403s.

    ANONYMOUS SESSION GATE (the bit that bit us): the API rejects (403 "Access
    Denied") any call that doesn't present the `ct` token the site mints for a
    visitor. That token is issued ANONYMOUSLY on first page load — no login. So,
    exactly like Apple's connector, we GET the careers page once on a cookie jar
    to pick up the anonymous `ct`, then POST the API on the SAME opener so the
    cookie rides along. NO personal/login cookies are read or stored — only the
    fresh anonymous token the public site hands every visitor (the playbook's
    explicitly-allowed "fresh anonymous cookie" path).

    RESPONSE shape (confirmed live):
        {"jobs":[ {requisitionID, title, locations[]{city,state,country,...},
                   customFields[]{cfKey,value}, applyURL} ],
         "totalJob": <int>}

    config (all optional):
        "location_list": chosen city NAMES (set by market_scope, like
                         Uber/Spotify). Each becomes a filter[city][i] param so
                         PlayStation scopes the fetch at the source. With none,
                         we fetch the global list and the app filter narrows it.

    - CITY: locations[].city (+ state/country), per §E-2 — never country alone.
    - DEPARTMENT: the customField with cfKey "cf_job_family_group".
    - PAGINATION: page_number, paging to the reported "totalJob"; a failed page
      is retried once; we also stop on a short/empty page. (§E-3)
    - DEDUP: merge by id (requisitionID). (§E-4)
    """
    import http.cookiejar

    BASE = "https://careers.playstation.com/api/get-jobs"
    HOME = "https://careers.playstation.com/"
    PAGE_LIMIT = 200            # safety ceiling; real totals are small once scoped
    # The static body the site sends; languages list is fixed site config.
    POST_BODY = {
        "disable_switch_search_mode": False,
        "site_available_languages": ["fr-ca", "ja", "en", "en-us"],
    }
    POST_HEADERS = dict(BROWSER_HEADERS)
    POST_HEADERS.update({
        "Content-Type": "application/json",
        "Origin": "https://careers.playstation.com",
        "Referer": HOME,
    })

    def _location_str(loc_objs):
        """City-first readable string from a role's locations[]; joins multiple."""
        parts = []
        for lo in (loc_objs or []):
            if not isinstance(lo, dict):
                continue
            city = (lo.get("city") or "").strip()
            state = (lo.get("state") or "").strip()
            country = (lo.get("country") or "").strip()
            bits = [b for b in (city, state, country) if b]
            s = ", ".join(bits) if bits else (lo.get("locationText") or "").strip()
            if s and s not in parts:
                parts.append(s)
        return "; ".join(parts)

    def _department(custom_fields):
        for cf in (custom_fields or []):
            if isinstance(cf, dict) and cf.get("cfKey") == "cf_job_family_group":
                return (cf.get("value") or "").strip()
        return ""

    def _parse(data):
        roles = data.get("jobs", []) if isinstance(data, dict) else []
        total = data.get("totalJob", 0) or 0 if isinstance(data, dict) else 0
        out = []
        for j in roles:
            if not isinstance(j, dict):
                continue
            jid = j.get("requisitionID") or j.get("reference") or j.get("uniqueID")
            title = j.get("title") or ""
            if jid is None or jid == "" or not title:
                continue
            out.append(_job(
                id_=jid,
                title=title,
                location=_location_str(j.get("locations")),
                department=_department(j.get("customFields")),
                url=j.get("applyURL") or "",
            ))
        return out, total

    def _open_session():
        """Build a cookie-jar opener and warm it up so the anonymous `ct` token
        is set. Returns the opener, or raises ConnectorError on a hard failure."""
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        warm = urllib.request.Request(
            HOME, headers={"User-Agent": BROWSER_HEADERS["User-Agent"],
                           "Accept": "text/html,application/xhtml+xml"})
        try:
            with opener.open(warm, timeout=TIMEOUT_SECONDS) as r:
                r.read()
        except Exception as e:
            raise ConnectorError(
                "Couldn't load PlayStation's careers page to start a session: "
                f"{e}") from e
        return opener

    def _fetch_page(opener, page_num, city_params):
        """POST one page on the warmed opener; retry once on failure."""
        params = [("radius", "15"), ("page_number", str(page_num)),
                  ("enable_kilometers", "false")]
        for i, city in enumerate(city_params):
            params.append((f"filter[city][{i}]", city))
        url = BASE + "?" + urllib.parse.urlencode(params)
        data = json.dumps(POST_BODY).encode("utf-8")
        for attempt in (1, 2):
            try:
                req = urllib.request.Request(url, data=data,
                                             headers=POST_HEADERS, method="POST")
                with opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
                    return json.loads(resp.read().decode("utf-8", errors="replace"))
            except Exception:
                if attempt == 2:
                    return None
                _polite_pause()
        return None

    # Chosen cities → source-side city filter (city-name passthrough scoping).
    loc_list = config.get("location_list")
    if not (isinstance(loc_list, list) and loc_list):
        single = config.get("location")
        loc_list = [single] if single else []
    city_params = [str(c).strip() for c in loc_list if str(c).strip()]

    opener = _open_session()

    jobs = []
    page = 1
    total = None
    while True:
        data = _fetch_page(opener, page, city_params)
        if data is None:
            break   # page failed even after a retry — stop rather than truncate silently
        page_jobs, page_total = _parse(data)
        if total is None:
            total = page_total
        jobs.extend(page_jobs)
        if not page_jobs:
            break
        if total and len(jobs) >= total:
            break
        page += 1
        if page > PAGE_LIMIT:
            break
        _polite_pause()

    return _merge_by_id(jobs)


def deliveroo(config):
    """Deliveroo Careers (careers.deliveroo.co.uk) — a single-company custom board.

    Deliveroo runs a WordPress site; its roles live in the WP REST API:
        GET https://careers.deliveroo.co.uk/wp-json/wp/v2/roles?per_page=100&page=<n>
    Each role is a WP post. Its `locations` and `teams` fields are TAXONOMY IDs
    (numbers), not names — so we request `_embed=wp:term`, which inlines the
    resolved term objects under `_embedded["wp:term"]`. That gives us location
    and team NAMES without a second lookup call (§E-2: read the real city, not an
    opaque id). Title is HTML-entity encoded, so we unescape it.

    Response shape (confirmed live, capture 2026-06-29): a JSON LIST of posts:
        [ {id, title{rendered}, link, locations:[<termId>...], teams:[<termId>...],
           _embedded{"wp:term":[[{taxonomy,name,...}], ...]}} ]
    WP has no total field in the body (it's in the X-WP-Total header, which our
    http_get doesn't expose), so we page until a short/empty page. (§E-3)

    SOURCE-SIDE SCOPING (Phase O pattern). The WP feed DOES take a location
    filter — `?locations_slug=<slug>` (confirmed live 2026-07-03) — but Deliveroo's
    location taxonomy is COUNTRY-level only. The live slugs are:
        france, ireland, italy, kuwait, non-office-locations,
        united-arab-emirates, united-kingdom
    There is NO city slug (no 'london'); every role's location term is just the
    country ("United Kingdom", "France", ...). So we CANNOT filter to a city at
    source — the finest Deliveroo offers is the country. What we DO is scope the
    fetch to the chosen city's COUNTRY (e.g. London -> united-kingdom), which
    drops all the other countries, then let the app's own location filter run on
    top. Because each role's location text is the country name, the app filter
    treats these as country-tagged matches (see geo.country_terms_for_city) rather
    than dropping them — the truthful best-available behaviour for this board.

    If no chosen city resolves to a known Deliveroo country slug (or none is
    given), we fall back to a broad all-country fetch, exactly like Uber/Spotify.

    config:
        "location_list": chosen city NAMES (set by market_scope), or
        "location":      a single city name, or nothing (broad fetch).
    No login or cookies needed (public list); reuse BROWSER_HEADERS/http_get.
    """
    import html
    try:
        from . import geo
    except Exception:            # pragma: no cover - allow flat-module import in tests
        import geo

    BASE = "https://careers.deliveroo.co.uk/wp-json/wp/v2/roles"
    PAGE_SIZE = 100
    MAX_PAGES = 200

    # The board's real, live country slugs (2026-07-03). A chosen city resolves to
    # one of these via its country name/aliases from geo; anything else is ignored
    # (and, if nothing resolves, we do a broad fetch).
    KNOWN_SLUGS = {
        "france", "ireland", "italy", "kuwait",
        "non-office-locations", "united-arab-emirates", "united-kingdom",
    }

    def _slugs_for_cities(city_names):
        """Map chosen city names -> Deliveroo country slugs, de-duplicated and
        order-preserving. A city whose country isn't one of Deliveroo's slugs is
        skipped. Returns [] when nothing resolves (caller then fetches broad)."""
        g = geo.load_geo()
        slugs = []
        for city in city_names:
            for term in geo.country_terms_for_city(city, g):
                # geo yields country name + aliases (e.g. "united kingdom","uk",
                # "england","gb"); turn each into a WP slug shape and keep known ones.
                cand = term.strip().lower().replace(" ", "-")
                if cand in KNOWN_SLUGS and cand not in slugs:
                    slugs.append(cand)
        return slugs

    def _terms_by_taxonomy(post):
        """From _embedded['wp:term'] build {taxonomy_name: [term_name, ...]}.
        WP embeds wp:term as a list of lists (one inner list per taxonomy)."""
        out = {}
        emb = (post.get("_embedded") or {}).get("wp:term") or []
        for group in emb:
            if not isinstance(group, list):
                continue
            for term in group:
                if not isinstance(term, dict):
                    continue
                tax = term.get("taxonomy") or ""
                name = (term.get("name") or "").strip()
                if name:
                    out.setdefault(tax, []).append(name)
        return out

    def _fetch_page(page_num, location_slug):
        params = {"per_page": PAGE_SIZE, "page": page_num, "_embed": "wp:term",
                  "order": "asc", "orderby": "title"}
        if location_slug:
            params["locations_slug"] = location_slug
        url = BASE + "?" + urllib.parse.urlencode(params)
        for attempt in (1, 2):
            try:
                return json.loads(http_get(url))
            except Exception:
                if attempt == 2:
                    return None
                _polite_pause()
        return None

    def _fetch_scope(location_slug):
        """Page through every role for one location slug ('' = all countries)."""
        out = []
        page = 1
        while True:
            data = _fetch_page(page, location_slug)
            if not isinstance(data, list) or not data:
                break   # empty/short page (or a failed page after retry) = stop
            for post in data:
                if not isinstance(post, dict):
                    continue
                jid = post.get("id")
                title_obj = post.get("title") or {}
                title = html.unescape((title_obj.get("rendered") or "").strip()) \
                    if isinstance(title_obj, dict) else ""
                if jid is None or jid == "" or not title:
                    continue
                terms = _terms_by_taxonomy(post)
                # Location taxonomy is "location" (singular term name); join multiples.
                loc_names = terms.get("location") or terms.get("locations") or []
                location = "; ".join(loc_names)
                # Team taxonomy is "team"; first is the department.
                team_names = terms.get("team") or terms.get("teams") or []
                dept = team_names[0] if team_names else ""
                out.append(_job(
                    id_=jid,
                    title=title,
                    location=location,
                    department=dept,
                    url=post.get("link") or "",
                ))
            if len(data) < PAGE_SIZE:
                break   # short page = last page (WP total only in a header)
            page += 1
            if page > MAX_PAGES:
                break
            _polite_pause()
        return out

    # Resolve the run's chosen cities to Deliveroo country slugs.
    loc_list = config.get("location_list")
    if not (isinstance(loc_list, list) and loc_list):
        single = config.get("location")
        loc_list = [single] if single else []

    slugs = _slugs_for_cities(loc_list) if loc_list else []

    jobs = []
    if slugs:
        for slug in slugs:
            jobs.extend(_fetch_scope(slug))
    else:
        # Nothing resolved (or nothing chosen) -> broad all-country fetch; the
        # app's location filter narrows afterwards.
        jobs.extend(_fetch_scope(""))

    return _merge_by_id(jobs)


def va(config):
    """Victoria and Albert Museum (vam.ac.uk/vacancies) — a single-company board.

    The V&A's vacancies list is SERVER-RENDERED HTML on its own site (no JSON
    feed; the page ships the jobs in the markup). The underlying ATS is Hireserve
    "current-vacancies.com" — each role's apply link is a Jobs/FeedLink/<id> URL,
    and that <id> is the stable unique id. We fetch the page and parse the role
    blocks. (Confirmed live via DOM inspection 2026-06-29.)

    Source (confirmed):
        GET https://www.vam.ac.uk/vacancies
    Markup (confirmed): inside <div class="vacancies__all"> each role is
        <div class="vacancy js-vacancy" data-group="<Department>">
          <div class="vacancy__main">
            <div class="vacancy__intro">
              <span class="vacancy__department ...">Department</span>
              <span class="separator ..."></span>
              <text node: the LOCATION, e.g. "Cromwell Road, London">
            </div>
            <h2 class="vacancy__title">Title</h2>
            <a class="u-link--arrowed" href=".../Jobs/FeedLink/<id>?...">
          </div>
          <div class="vacancy__meta">deadline / salary / type</div>
        </div>

    - ID: the FeedLink/<id> number (stable, unique). (§E-1)
    - DEPARTMENT: the data-group attribute (free, no parsing). (DATA_FORMATS §1)
    - LOCATION: the text node in vacancy__intro after the department/separator
      spans — a real place string ("Cromwell Road, London"). The V&A is London-
      only, so every role resolves to London; we still report the source text so
      the app filter behaves normally. (§E-2)
    - URL: the full current-vacancies FeedLink (where "Full information" goes).
    - PAGINATION: none — the page renders every role at once (confirmed).
    - DEDUP: merge by id for safety, though the page lists each role once. (§E-4)

    HTML connector (the project's first). Parsed with the stdlib `re`/`html`
    modules only — NO new dependency (no bs4), matching the rest of the project.
    Custom HTML boards are FRAGILE (a site redesign breaks the parse); when that
    happens, re-capture the markup and adjust the patterns (§ rules).
    No login or cookies needed (public list); reuse BROWSER_HEADERS/http_get.
    """
    import html as _html

    URL = "https://www.vam.ac.uk/vacancies"

    def _parse(page):
        out = []
        # Split at each vacancy block start; each segment runs to the next start.
        starts = [m.start() for m in
                  re.finditer(r'<div class="vacancy js-vacancy"', page)]
        if not starts:
            return out
        bounds = starts + [len(page)]
        for i in range(len(starts)):
            seg = page[bounds[i]:bounds[i + 1]]
            dm = re.search(r'data-group="([^"]*)"', seg)
            dept = _html.unescape(dm.group(1)).strip() if dm else ""
            mt = re.search(r'<h2 class="vacancy__title">(.*?)</h2>', seg, re.S)
            title = _html.unescape(re.sub(r"<[^>]+>", "", mt.group(1))).strip() if mt else ""
            ma = re.search(
                r'<a[^>]*class="[^"]*u-link--arrowed[^"]*"[^>]*href="([^"]+)"', seg)
            url = _html.unescape(ma.group(1)) if ma else ""
            mid = re.search(r"/FeedLink/(\d+)", url)
            jid = mid.group(1) if mid else ""
            # Location: the intro text minus the department + separator spans.
            mi = re.search(r'<div class="vacancy__intro">(.*?)</div>', seg, re.S)
            location = ""
            if mi:
                intro = mi.group(1)
                intro = re.sub(
                    r'<span class="vacancy__department[^"]*">.*?</span>', "",
                    intro, flags=re.S)
                intro = re.sub(
                    r'<span class="separator[^"]*">.*?</span>', "",
                    intro, flags=re.S)
                intro = re.sub(r"<[^>]+>", "", intro)
                location = _html.unescape(intro).strip().strip('"').strip()
            if not (jid and title):
                continue
            out.append(_job(id_=jid, title=title, location=location,
                            department=dept, url=url))
        return out

    page = http_get(URL)
    return _merge_by_id(_parse(page))


def webitrent(config):
    """MHR iTrent web recruitment (*.webitrent.com) — used by UK public-sector /
    cultural employers (e.g. the National Gallery). A recognised platform: each
    employer is one tenant host + a WVID (web-view / employer id).

    REQUEST FLOW (confirmed live, capture 2026-06-29, National Gallery):
      1. The job list is a JSON endpoint:
           GET <base>/etrec106gf.json?WVID=<wvid>&USESSION=<token>&LANG=USA
         where <base> is e.g.
           https://ce0838li.webitrent.com/ce0838li_webrecruitment/wrd/run
      2. USESSION is a PER-SESSION token minted when the search page loads. So,
         like PlayStation/V&A, we first GET the launch page
           <base>/ETREC179GF.open?WVID=<wvid>
         on a cookie jar to obtain a fresh USESSION, then call the JSON list.
         The session is ANONYMOUS (no login); cookies are a load-balancer +
         session cookie, not personal — the allowed "fresh anonymous" path.

    RESPONSE shape (confirmed live):
        {"search": {"total_rec": N, "results_pp": "10", "rec_to": ...},
         "results": [ {vacancy_id, job_title, location_id, salary,
                       vacancy_ref, basis_id, app_close_d, ...} ]}

    config:
        "url"   - the full launch URL the user pasted (REQUIRED). We parse the
                  tenant host, the path prefix, and the WVID out of it.
        (optional explicit overrides: "host", "prefix", "wvid".)

    - ID: vacancy_id (stable, unique). (§E-1)
    - LOCATION: location_id — a SITE name ("The National Gallery"), not a city.
      These employers are single-site (London); we report the board's text and
      let the app filter's country/site fallback handle it. (§E-2)
    - DEPARTMENT: the feed has none, so "" (DATA_FORMATS §1 allows blank).
    - URL: the public advert page, built from vacancy_id + WVID.
    - PAGINATION: page via rec_from/results_pp until total_rec is reached;
      a failed page is retried once. (§E-3)
    - DEDUP: merge by id. (§E-4)
    """
    import http.cookiejar
    import html as _html

    launch_url = (config.get("url") or "").strip()
    host = config.get("host") or ""
    prefix = config.get("prefix") or ""
    wvid = config.get("wvid") or ""

    # Derive host / path-prefix / wvid from the launch URL when not given.
    if launch_url:
        p = urllib.parse.urlparse(launch_url)
        host = host or p.netloc
        # prefix is everything up to and including ".../wrd/run"
        m = re.match(r"(.*/wrd/run)/", p.path)
        if m and not prefix:
            prefix = m.group(1)
        if not wvid:
            q = urllib.parse.parse_qs(p.query)
            wvid = (q.get("WVID", [""])[0])
    if not (host and prefix and wvid):
        raise ConnectorError(
            "The webitrent connector needs the full launch URL (it carries the "
            "tenant and WVID). Paste the careers URL that opens the vacancy list.")

    base = f"https://{host}{prefix}"
    launch = f"{base}/ETREC179GF.open?WVID={urllib.parse.quote(wvid)}"

    def _open_session():
        """Warm up an anonymous session and return (opener, USESSION).

        The launch page (ETREC179GF.open) both SETS the session cookies (an F5
        'BIGipServer...' stickiness cookie + a 'TS...' session cookie — captured
        automatically by the shared cookie jar) and exposes the USESSION token.
        The token can surface in three places depending on the tenant's flow:
          1. the FINAL URL after any redirects (…?USESSION=…),
          2. a redirect hop's Location header along the way, or
          3. the page BODY's own links/scripts (…USESSION=… / subscribe_url).
        We harvest from all three and keep the LAST, longest hex-ish match, which
        is the live token (early matches can be template placeholders). Using the
        SAME opener for the subsequent JSON call is essential: it carries the
        stickiness cookie so the feed request lands on the backend that minted the
        session (a mismatch is what makes the board fall back to HTML → a 0)."""
        cj = http.cookiejar.CookieJar()

        # Record redirect target URLs so a token in a Location header isn't lost.
        seen_urls = []

        class _RedirectTracker(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                seen_urls.append(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj), _RedirectTracker())
        req = urllib.request.Request(
            launch, headers={"User-Agent": BROWSER_HEADERS["User-Agent"],
                             "Accept": "text/html,application/xhtml+xml"})
        try:
            with opener.open(req, timeout=TIMEOUT_SECONDS) as r:
                page = r.read().decode("utf-8", errors="replace")
                final_url = r.geturl()
        except Exception as e:
            raise ConnectorError(
                f"Couldn't open the webitrent launch page to start a session: {e}"
            ) from e

        # Gather every USESSION candidate across final URL, redirect hops, body.
        haystacks = [final_url] + seen_urls + [page]
        candidates = []
        for h in haystacks:
            candidates.extend(re.findall(r"USESSION=([A-Za-z0-9]+)", h or ""))
        # Keep the longest token (placeholders tend to be short/empty); on a tie
        # keep the last seen (later in the flow = the live one).
        usession = ""
        for tok in candidates:
            if len(tok) >= len(usession):
                usession = tok
        return opener, usession

    # Outcome sentinels so the caller can tell WHY a page was empty:
    #   ("ok",   data) - real JSON parsed
    #   ("html", None) - the board served its HTML web page instead of the feed
    #                    (session/token not accepted) — a real fault, not "0 jobs"
    #   ("err",  None) - network/transport failure after retries
    def _fetch_list(opener, usession, rec_from):
        # webitrent returns an HTML fallback page UNLESS the request carries the
        # 'mhrParams' header with the full (mostly-empty) search param set — this
        # is what the browser's XHR sends (confirmed live). It's a static request
        # header, no personal data.
        mhr_parts = [
            f"WVID={wvid}", f"USESSION={usession}", "LANG=USA",
            "JOB_TITLE=", "KEYWORDS=", "LOCATION_ID=", "VAC_TYPES=",
            "SALARY_BAND=", "ORDER_BY=VACANCY_D",
        ]
        if rec_from > 1:
            mhr_parts.append(f"REC_FROM={rec_from}")
        mhr_params = "&".join(mhr_parts)

        params = {"WVID": wvid, "USESSION": usession, "LANG": "USA"}
        if rec_from > 1:
            params["REC_FROM"] = str(rec_from)
        url = f"{base}/etrec106gf.json?" + urllib.parse.urlencode(params)
        headers = {
            "User-Agent": BROWSER_HEADERS["User-Agent"],
            "Accept": "*/*",
            "mhrParams": mhr_params,
            "Referer": launch,
            "X-Requested-With": "XMLHttpRequest",
        }
        for attempt in (1, 2):
            try:
                req = urllib.request.Request(url, headers=headers)
                with opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
            except Exception:
                if attempt == 2:
                    return ("err", None)
                _polite_pause()
                continue
            # The feed is JSON. If we got HTML back, the session/token wasn't
            # accepted and the board fell back to its web page — distinct from a
            # genuinely empty result set, and must NOT look like "0 jobs".
            stripped = raw.lstrip()
            if stripped[:1] in ("{", "["):
                try:
                    return ("ok", json.loads(raw))
                except ValueError:
                    return ("html", None)  # JSON-ish but unparseable → treat as fault
            return ("html", None)
        return ("err", None)

    def _advert_url(vac_id):
        return (f"{base}/ETREC179GF.open?WVID={urllib.parse.quote(wvid)}"
                f"&VACANCY_ID={urllib.parse.quote(vac_id)}")

    def _parse(data):
        results = data.get("results", []) if isinstance(data, dict) else []
        search = data.get("search", {}) if isinstance(data, dict) else {}
        total = 0
        try:
            total = int(search.get("total_rec") or 0)
        except (TypeError, ValueError):
            total = 0
        out = []
        for r in results:
            if not isinstance(r, dict):
                continue
            vac_id = r.get("vacancy_id")
            title = _html.unescape((r.get("job_title") or "").strip())
            if not vac_id or not title:
                continue
            out.append(_job(
                id_=vac_id,
                title=title,
                location=_html.unescape((r.get("location_id") or "").strip()),
                department="",   # webitrent's list feed has no department field
                url=_advert_url(vac_id),
            ))
        return out, total

    opener, usession = _open_session()
    if not usession:
        raise ConnectorError(
            "Couldn't obtain a session token from the webitrent launch page; "
            "the site may have changed. Re-capture and check the launch URL.")

    jobs = []
    rec_from = 1
    total = None
    PAGE_LIMIT = 100
    pages = 0
    while True:
        status, data = _fetch_list(opener, usession, rec_from)
        if status == "html":
            # The board served its web page instead of the job feed. This means
            # the anonymous session/token wasn't accepted — a real fault. Fail
            # LOUDLY rather than reporting an empty board (which would silently
            # look like "no jobs" and could wipe the snapshot on compare).
            raise ConnectorError(
                "The National Gallery / webitrent board returned its web page "
                "instead of the job list — the anonymous session wasn't accepted. "
                "This usually means the launch-page step needs re-checking "
                "(re-capture the careers URL). Not treating this as zero jobs.")
        if status == "err":
            # Network/transport failure. On the FIRST page this is fatal (we have
            # nothing); on a later page, keep what we already have.
            if rec_from == 1:
                raise ConnectorError(
                    "Couldn't reach the webitrent job feed (network error). "
                    "Try again shortly.")
            break
        page_jobs, page_total = _parse(data)
        if total is None:
            total = page_total
        jobs.extend(page_jobs)
        if not page_jobs:
            break
        if total and len(jobs) >= total:
            break
        rec_from = len(jobs) + 1
        pages += 1
        if pages > PAGE_LIMIT:
            break
        _polite_pause()

    return _merge_by_id(jobs)


def ciphr(config):
    """CIPHR iRecruit (*.ciphr-irecruit.com) — UK public-sector / cultural
    employers (e.g. the British Museum). A recognised platform: each employer is
    one tenant subdomain; the vacancy list is SERVER-RENDERED HTML (a Bootstrap
    table on /Applicants/vacancy — no JSON feed, no XHR; confirmed live 2026-06-29).

    Source (confirmed):
        GET https://<tenant>.ciphr-irecruit.com/applicants/vacancy
    Markup (confirmed): <table class="table table-striped ...">
        <thead> ... <th title="LocationColumnHeaderTooltip">Location</th> ... </thead>
        <tbody>
          <tr>
            <td><a href="/Applicants/vacancy/<id>/<slug>">Title</a></td>  <- title+id+url
            <td>Application Deadline</td>
            <td>Location</td>                                             <- city/site
            <td>...</td>
          </tr>
        </tbody>

    config:
        "url"  - the full vacancy-list URL the user pasted (REQUIRED). We parse
                 the tenant host out of it and fetch its /applicants/vacancy.
        (optional: "host" to override the tenant host directly.)

    - ID: the /vacancy/<id> number in the title link (stable, unique). (§E-1)
    - TITLE: the title link's text.
    - LOCATION: the cell under the "Location" header — found by the header's
      stable `LocationColumnHeaderTooltip` title (robust to column reordering on
      other CIPHR tenants), falling back to a "Location" header text match. A
      site/city string ("London, Great Russell Street"). (§E-2)
    - DEPARTMENT: the list table has none, so "" (DATA_FORMATS §1 allows blank).
    - URL: the advert page (title link, made absolute).
    - PAGINATION: none observed (small boards render every role at once); if a
      tenant ever paginates, this fetches page 1 only — re-capture if so.
    - DEDUP: merge by id. (§E-4)

    HTML connector (stdlib `re`/`html`, like the V&A one — NO new dependency).
    No login or cookies needed (public list); reuse BROWSER_HEADERS/http_get.
    """
    import html as _html

    launch_url = (config.get("url") or "").strip()
    host = config.get("host") or ""
    if launch_url and not host:
        host = urllib.parse.urlparse(launch_url).netloc
    if not host:
        raise ConnectorError(
            "The CIPHR connector needs the vacancy-list URL (it carries the "
            "tenant). Paste the careers URL that opens the vacancy table.")
    base = f"https://{host}"
    list_url = f"{base}/applicants/vacancy"

    def _location_col(page):
        mth = re.search(r"<thead[^>]*>(.*?)</thead>", page, re.S)
        if not mth:
            return None
        ths = re.findall(r"<th[^>]*>.*?</th>", mth.group(1), re.S)
        for i, th in enumerate(ths):
            if re.search(r"LocationColumnHeaderTooltip", th) or \
               re.search(r">\s*Location\s*<", th, re.I):
                return i
        return None

    def _parse(page):
        out = []
        loc_col = _location_col(page)
        mtb = re.search(r"<tbody[^>]*>(.*?)</tbody>", page, re.S)
        if not mtb:
            return out
        for rowm in re.finditer(r"<tr[^>]*>(.*?)</tr>", mtb.group(1), re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", rowm.group(1), re.S)
            if not tds:
                continue
            am = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', tds[0], re.S)
            if not am:
                continue
            href = _html.unescape(am.group(1))
            title = _html.unescape(re.sub(r"<[^>]+>", "", am.group(2))).strip()
            mid = re.search(r"/vacancy/(\d+)", href)
            jid = mid.group(1) if mid else ""
            url = href if href.startswith("http") else base + href
            location = ""
            if loc_col is not None and loc_col < len(tds):
                location = _html.unescape(
                    re.sub(r"<[^>]+>", "", tds[loc_col])).strip()
            if not (jid and title):
                continue
            out.append(_job(id_=jid, title=title, location=location,
                            department="", url=url))
        return out

    page = http_get(list_url)
    return _merge_by_id(_parse(page))


def sohohouse(config):
    """Soho House Careers (careers.sohohouse.com) — a single-company custom board.

    The careers site is a Next.js app. Its FULL vacancy list ships as one static
    JSON data file (every role in one shot — the "Load more" button just reveals
    more of an already-loaded array; no real pagination, no token, no POST).
    Confirmed live 2026-06-30.

    THE WRINKLE: the data file's URL carries the Next.js BUILD ID, which changes
    on every redeploy:
        https://careers.sohohouse.com/_next/data/<buildId>/careers.json
    So we can't hardcode it. We fetch the careers HTML page, read the current
    buildId from its embedded __NEXT_DATA__ script, then request the data file.
    Next.js also embeds the page's props inline in __NEXT_DATA__, so as a first
    (and most robust) try we read the vacancies straight from the HTML — only
    falling back to the data-file fetch if the inline copy isn't present.

    RESPONSE shape (confirmed live): pageProps.vacancies[] with:
        {id, job_title, job_location (the CITY), department, venue, region,
         department_area}

    - ID: the numeric id (stable, unique). (§E-1)
    - LOCATION: job_location — a real CITY ("London", "New York", "São Paulo"),
      exactly what filters want. (§E-2)
    - DEPARTMENT: the department field ("" when absent). (DATA_FORMATS §1)
    - URL: built as /careers/<id> (confirmed role-link pattern).
    - GLOBAL BOARD: returns ~all roles worldwide; NOT source-side scopable (the
      static file has no filter param), so we fetch all and the app filter
      narrows — same model as Deliveroo.
    - DEDUP: merge by id. (§E-4)
    No login or cookies needed (public); reuse BROWSER_HEADERS/http_get.
    """
    import html as _html

    BASE = "https://careers.sohohouse.com"
    PAGE_URL = f"{BASE}/careers"

    def _jobs_from_vacancies(vacancies):
        out = []
        for v in (vacancies or []):
            if not isinstance(v, dict):
                continue
            jid = v.get("id")
            title = _html.unescape((v.get("job_title") or "").strip())
            if jid is None or jid == "" or not title:
                continue
            out.append(_job(
                id_=jid,
                title=title,
                location=_html.unescape((v.get("job_location") or "").strip()),
                department=_html.unescape((v.get("department") or "").strip()),
                url=f"{BASE}/careers/{jid}",
            ))
        return out

    def _vacancies_from_props(obj):
        """Pull pageProps.vacancies from a parsed __NEXT_DATA__ / data-file dict."""
        if not isinstance(obj, dict):
            return None
        props = obj.get("pageProps")
        if isinstance(props, dict) and isinstance(props.get("vacancies"), list):
            return props["vacancies"]
        # __NEXT_DATA__ nests pageProps under props.pageProps
        p = obj.get("props")
        if isinstance(p, dict):
            pp = p.get("pageProps")
            if isinstance(pp, dict) and isinstance(pp.get("vacancies"), list):
                return pp["vacancies"]
        return None

    # Step 1: load the careers page.
    page = http_get(PAGE_URL)

    # Step 1a (preferred): read vacancies inline from __NEXT_DATA__.
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    build_id = ""
    if m:
        try:
            nd = json.loads(m.group(1))
            build_id = nd.get("buildId", "") if isinstance(nd, dict) else ""
            vac = _vacancies_from_props(nd)
            if vac:
                return _merge_by_id(_jobs_from_vacancies(vac))
        except (ValueError, TypeError):
            pass

    # Step 1b: find the buildId if we didn't get it above.
    if not build_id:
        mb = re.search(r'"buildId"\s*:\s*"([^"]+)"', page)
        if mb:
            build_id = mb.group(1)
    if not build_id:
        raise ConnectorError(
            "Couldn't find Soho House's build id on the careers page; the site "
            "may have changed. Re-capture and check the careers page markup.")

    # Step 2: fetch the static data file for that build.
    data_url = f"{BASE}/_next/data/{build_id}/careers.json"
    raw = http_get(data_url)
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ConnectorError(
            f"Soho House's data file wasn't valid JSON: {e}") from e
    vac = _vacancies_from_props(data)
    if vac is None:
        raise ConnectorError(
            "Soho House's data file had no vacancies list where expected; "
            "re-capture the response shape.")
    return _merge_by_id(_jobs_from_vacancies(vac))


def teamtailor(config):
    """Teamtailor career sites — a recognised PLATFORM, not a single company.

    Teamtailor (teamtailor.com) hosts thousands of employers' career sites. Each
    tenant lives at either <subdomain>.teamtailor.com/jobs OR a CUSTOM DOMAIN the
    employer maps to it (e.g. Yoto's careers.yotoplay.com/jobs). So, like the
    webitrent/ciphr platform connectors, we key off the pasted URL's HOST rather
    than assuming a teamtailor.com host, and any Teamtailor employer works by
    pasting their careers URL.

    WHY HTML, not the API: Teamtailor's public JSON:API (api.teamtailor.com/v1/jobs)
    requires a private per-tenant token (Authorization: Token token=...), which we
    don't have and won't use. The jobs are, however, fully SERVER-RENDERED into the
    /jobs page, so we parse that markup with stdlib re/html — the same approach as
    the V&A and CIPHR connectors (NO bs4 dependency). Confirmed live 2026-07-06
    against careers.yotoplay.com.

    Markup (confirmed): a single list container
        <ul id="jobs_list_container" class="company-links">
          <li ...>
            <a href="https://<host>/jobs/<id>-<slug>"> <span.../> Title </a>
            <div class="mt-1 text-md">
              [<span>Department</span> · ]         <- PRESENT ONLY when the role
              <span>Location</span> ·                  has a department
              <span class="inline-flex ...">Remote-status <svg/></span>
            </div>
          </li>
          ...
        </ul>

    - ID: the digits before the first '-' in /jobs/<id>-<slug> (stable, unique). (§E-1)
    - TITLE: the <a> text (entities unescaped; the absolute-inset <span> stripped). (§1)
    - DEPARTMENT: the FIRST meta <span> — but it's absent on roles with no
      department, so we read the meta STRUCTURALLY, not positionally (the §6.10
      lesson): drop the remote-status span (it carries the <svg>) and the "·"
      divider spans, then of the remaining spans the LAST is the location and any
      span before it is the department. Blank department is allowed (§1).
    - LOCATION: the site/city text ("Yoto HQ - UK"); reported as-is so the app's
      location filter narrows it. (§E-2)
    - URL: the full absolute href on the tenant's host.
    - PAGINATION: Teamtailor pages the list with ?page=N; we page until a page has
      no job rows (a short/empty page = the end). (§E-3)
    - DEDUP: merge by id. (§E-4)

    config:
        "url"  - the careers URL the user pasted (REQUIRED). We take its host and
                 fetch <host>/jobs. (optional explicit override: "host".)

    Custom HTML boards are fragile by nature (a Teamtailor theme change can move
    these classes); if the parse ever returns 0 while the site clearly has roles,
    re-capture one <li> block and adjust the patterns.
    No login or cookies needed (public list); reuse BROWSER_HEADERS/http_get.
    """
    import html as _html

    pasted = (config.get("url") or "").strip()
    host = (config.get("host") or "").strip()
    if pasted and not host:
        host = urllib.parse.urlparse(
            pasted if "://" in pasted else "https://" + pasted).netloc
    if not host:
        raise ConnectorError(
            "The Teamtailor connector needs the careers URL (it carries the "
            "tenant's host). Paste the careers page that lists the jobs.")
    base = f"https://{host}"
    list_url = f"{base}/jobs"

    PAGE_LIMIT = 100      # safety ceiling; real tenants are far smaller

    def _meta_fields(li):
        """Department + location from a card's meta div, read structurally.
        Returns (department, location); department is '' when the role has none."""
        mdiv = re.search(r'<div[^>]*class="mt-1 text-md"[^>]*>(.*)', li, re.S)
        if not mdiv:
            return "", ""
        meta = mdiv.group(1)
        # Drop the remote-status span (the one that contains an <svg>/wifi icon).
        meta = re.sub(r'<span[^>]*class="inline-flex[^"]*"[^>]*>.*?</span>', "",
                      meta, flags=re.S)
        # Drop the "·" divider spans.
        meta = re.sub(r'<span[^>]*class="mx-\[2px\][^"]*"[^>]*>.*?</span>', "",
                      meta, flags=re.S)
        # Remaining spans, skipping layout-only ones (absolute-inset / border).
        spans = re.findall(
            r'<span(?![^>]*class="(?:block|absolute)[^"]*")[^>]*>(.*?)</span>',
            meta, re.S)
        vals = [_html.unescape(re.sub(r"<[^>]+>", "", s)).strip() for s in spans]
        vals = [v for v in vals if v]
        if len(vals) >= 2:
            return vals[0], vals[1]      # department, location
        if len(vals) == 1:
            return "", vals[0]           # location only (no department)
        return "", ""

    def _parse(page):
        out = []
        mul = re.search(
            r'<ul[^>]*id="jobs_list_container"[^>]*>(.*?)</ul>', page, re.S)
        scope = mul.group(1) if mul else page
        for lim in re.finditer(r"<li[^>]*>(.*?)</li>", scope, re.S):
            li = lim.group(1)
            am = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', li, re.S)
            if not am:
                continue
            href = _html.unescape(am.group(1))
            title = _html.unescape(re.sub(r"<[^>]+>", "", am.group(2))).strip()
            mid = re.search(r"/jobs/(\d+)", href)
            jid = mid.group(1) if mid else ""
            if not (jid and title):
                continue
            dept, location = _meta_fields(li)
            url = href if href.startswith("http") else base + href
            out.append(_job(id_=jid, title=title, location=location,
                            department=dept, url=url))
        return out

    jobs = []
    page_num = 1
    while True:
        url = list_url if page_num == 1 else f"{list_url}?page={page_num}"
        for attempt in (1, 2):
            try:
                page = http_get(url)
                break
            except Exception:
                if attempt == 2:
                    page = ""
                _polite_pause()
        page_jobs = _parse(page) if page else []
        if not page_jobs:
            break               # empty/short page (or a failed page) = the end
        jobs.extend(page_jobs)
        page_num += 1
        if page_num > PAGE_LIMIT:
            break
        _polite_pause()

    return _merge_by_id(jobs)


def cursor(config):
    """Cursor / Anysphere (cursor.com/careers) — a single-company custom board.

    HISTORY (matters, 2026-07-24): Cursor ran a hosted ASHBY board at
    jobs.ashbyhq.com/cursor. That board is now switched off (404) and the roles
    are served from cursor.com itself, so the standard-platform path is closed
    and a pasted Ashby URL is dead. Watch for the §6.4 trap while re-checking:
    jobs.ashbyhq.com returns HTTP 200 with a bare "enable JavaScript" SPA shell
    for ANY path, so a 200 there proves nothing — check the body.

    The list is fully SERVER-RENDERED into the careers page (Next.js App Router,
    but the markup ships in the HTML — no flight-data or build-hash parsing
    needed, unlike Soho House). Parsed with stdlib re/html, no bs4. Confirmed
    against a live capture off the owner's Mac, 2026-07-24 (120 roles).

    Source (confirmed):
        GET https://cursor.com/careers
    Markup (confirmed): each role is one <article>
        <article class="flex grow-1 flex-col">
          <a class="card card--text grow-1" href="/careers/<slug>">
            <p class="type-base text-theme-text text-pretty">Title</p>
            <div class="text-theme-text-sec flex shrink-0 items-center">
              <span>Department</span><span class="mx-1">·</span>
              <span>Full-time</span><span class="mx-1">·</span>
              <span>San Francisco; New York</span>
            </div>
            <span class="btn-tertiary">Apply →</span>
          </a>
        </article>

    - ID: the <slug> from /careers/<slug> — a stable, non-positional string
      (DATA_FORMATS §"ids are opaque strings"). Cursor exposes no numeric req id.
    - TITLE: the type-base <p> (entities unescaped — R&D-style "&amp;" is live
      in 14 places on the board). (§6.9)
    - DEPARTMENT / LOCATION: read STRUCTURALLY, never positionally (§6.10). The
      meta div holds "Department · Employment-type · Location", but a role
      missing a department would shift every field left. So we drop the "·"
      divider spans, drop the EMPLOYMENT-TYPE span BY VALUE (_WORK_TYPES), then
      take the LAST remaining span as the location and any earlier one as the
      department. Blank department is allowed (§1); the location is the field
      the hard filter gates on, so it is the one we protect.
    - LOCATION FORMAT: multi-site roles arrive already ";"-joined
      ("San Francisco; New York") — the same convention _merge_by_id emits, and
      what filters.filter_by_location's semicolon handling expects. Reported
      as-is; the app narrows it. (§E-2)
    - URL: absolute on cursor.com.
    - PAGINATION: none — the whole list ships in one page (confirmed: no paging
      control, 120 unique roles). No page loop to get wrong.
    - DEDUP: the page renders the SAME list TWICE (responsive desktop/mobile
      markup), so a raw parse yields 240 rows for 120 roles. _merge_by_id
      collapses them; both copies were byte-identical in the capture, so the
      merge is lossless. This is exactly the double-counting §6.8 warns about.
    - NOT SCOPABLE: the board has no server-side location query, so it stays out
      of market_scope.SCOPABLE — broad fetch, then filters.py narrows. (§3.5)

    config: none required (single company, fixed URL).

    Custom boards are fragile by nature (§6.12): if this ever returns 0 while the
    site clearly has roles, re-capture cursor.com/careers and adjust the patterns
    — the connector raises a clear error saying so rather than reporting an empty
    list, which would otherwise read as "every role removed".
    """
    import html as _html

    URL = "https://cursor.com/careers"
    BASE = "https://cursor.com"

    # Employment-type tokens, matched BY VALUE so the meta is read structurally
    # rather than by span position (§6.10).
    _WORK_TYPES = {
        "full-time", "part-time", "contract", "contractor", "freelance",
        "internship", "intern", "temporary", "fixed-term", "full time",
        "part time",
    }
    _DIVIDERS = {"·", "•", "|", "-", "–", "—"}

    def _text(fragment):
        return _html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()

    def _meta_fields(article):
        """(department, location) from a card's meta div, read structurally.
        Department is '' when the role has none."""
        m = re.search(
            r'<div[^>]*class="[^"]*text-theme-text-sec[^"]*"[^>]*>(.*?)</div>',
            article, re.S)
        if not m:
            return "", ""
        vals = []
        for sm in re.finditer(r"<span([^>]*)>(.*?)</span>", m.group(1), re.S):
            attrs, txt = sm.group(1), _text(sm.group(2))
            if "mx-1" in attrs:          # the "·" divider spans
                continue
            if not txt or txt in _DIVIDERS:
                continue
            vals.append(txt)
        # Drop the employment type wherever it sits in the run.
        vals = [v for v in vals if v.lower() not in _WORK_TYPES]
        if not vals:
            return "", ""
        location = vals[-1]
        department = vals[0] if len(vals) >= 2 else ""
        return department, location

    def _parse(page):
        out = []
        for am in re.finditer(r"<article\b[^>]*>(.*?)</article>", page, re.S):
            art = am.group(1)
            hm = re.search(r'href="(/careers/[^"#?]+)"', art)
            if not hm:
                continue                 # not a role card (or markup changed)
            path = _html.unescape(hm.group(1))
            slug = path.rstrip("/").rsplit("/", 1)[-1]
            tm = re.search(
                r'<p[^>]*class="[^"]*type-base[^"]*"[^>]*>(.*?)</p>', art, re.S)
            title = _text(tm.group(1)) if tm else ""
            if not (slug and title):
                continue
            dept, location = _meta_fields(art)
            out.append(_job(id_=slug, title=title, location=location,
                            department=dept, url=BASE + path))
        return out

    headers = dict(BROWSER_HEADERS)
    headers["Accept"] = "text/html,application/xhtml+xml"

    page = ""
    for attempt in (1, 2):               # retry a failed fetch once (§6.7)
        try:
            page = http_get(URL, headers=headers)
            break
        except ConnectorError:
            if attempt == 2:
                raise
            _polite_pause()

    jobs = _parse(page)
    if not jobs:
        raise ConnectorError(
            "Cursor's careers page returned no roles. The page markup has "
            "probably changed — re-capture https://cursor.com/careers and "
            "update the connector's patterns."
        )
    return _merge_by_id(jobs)


def revolutpeople(config):
    """Revolut People (revolutpeople.com) — a PLATFORM connector.

    Revolut spun its internal hiring product out as a third-party ATS in 2024.
    Tenants get revolutpeople.com/<tenant>/public/careers, and a clean public
    JSON API behind it. One connector serves every tenant: the tenant slug comes
    from the pasted URL. Confirmed live 2026-07-25 against Cleo (37 roles).

    Source (confirmed, GET, JSON, NO auth and NO cookie needed — a plain
    cookieless request returns 200 even though the site sits behind Cloudflare):
        GET https://revolutpeople.com/api/<tenant>/external/v3/postings?page=<n>

    Response shape (confirmed):
        {"pages": {"next": null, "previous": null, "total": 1, "page_size": 100},
         "count": 37,
         "results": [{"id": "<uuid>", "title": "...",
                      "locations": [{"name": "UK - Remote", "type": "remote",
                                     "country": {"name": "United Kingdom"}}, ...],
                      "function": {"name": "Engineering"},   # or NULL
                      "is_featured": false}, ...]}

    - ID: a UUID string. Opaque and stable (DATA_FORMATS: ids are strings).
    - DEPARTMENT: function.name — but `function` is NULL on real roles (Product
      Design Manager, Director of InfoSec), so it must be read defensively or a
      naive .get("name") raises. Blank department is allowed (§1).
    - LOCATION: `locations` is a LIST; roles routinely carry several ("Spain",
      "UK - Remote", "United Kingdom"). Joined with "; " — the convention
      _merge_by_id emits and filters.filter_by_location already splits on. Names
      are de-duplicated, order preserved.
      NOTE for expectations: this board tags by COUNTRY, not city — "United
      Kingdom" and "UK - Remote", never "London". A London filter still matches
      them via geo.py's country-level rule, but they arrive flagged
      location_unclear ("city not specified") rather than as confirmed London
      hits. That is the designed behaviour, not a bug.
    - PAGINATION: ?page=N, bounded by pages.total. Requesting a page past the
      end returns {"detail": "Invalid page."} (an HTTP error), so we never probe
      past pages.total; a failure on a later page is caught and ends the crawl
      rather than killing the run, with the completeness guard below as the net.
    - COMPLETENESS GUARD: the payload states `count`. We compare it against what
      we collected and raise rather than return a short list, which would read
      as roles being removed. Same reasoning as the avature/successfactors
      guards.
    - NOT SCOPABLE at the source by city: stays out of market_scope.SCOPABLE.

    ROBOTS: revolutpeople.com's robots.txt asks automated clients away. The
    owner reviewed it and chose to proceed for personal, low-frequency use of a
    public careers page. Recorded here so the decision is visible rather than
    implicit (§11).

    config = {"url": "<the tenant's public careers URL>"}
    """
    url_in = (config or {}).get("url", "")
    if not str(url_in).strip():
        raise ConnectorError(
            "The Revolut People connector needs the careers URL, e.g. "
            "https://revolutpeople.com/cleo/public/careers")

    parsed = urllib.parse.urlparse(str(url_in).strip())
    host = parsed.netloc or "revolutpeople.com"
    scheme = parsed.scheme or "https"
    segs = [s for s in parsed.path.split("/") if s]
    tenant = segs[0] if segs and segs[0].lower() not in ("api", "public") else ""
    if not tenant:
        raise ConnectorError(
            f"Couldn't read the company from the Revolut People URL {url_in!r}. "
            f"It should look like https://{host}/<company>/public/careers")

    origin = f"{scheme}://{host}"
    api = f"{origin}/api/{tenant}/external/v3/postings"

    def _slugify(text):
        """Title -> the URL slug Revolut People uses. Confirmed against a live
        link: 'Senior / Lead Data Scientist, Product Analytics' ->
        'senior-lead-data-scientist-product-analytics'. Any run of non
        alphanumerics collapses to a single hyphen, so '/', ',', '|', '&' and
        brackets all fold away."""
        t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")

    def _job_url(jid, title):
        """The public posting URL. The API returns NO link, so it is rebuilt as
            /<tenant>/public/careers/position/<title-slug>-<id>
        confirmed live 2026-07-25. If Revolut ever changes the slug rule, this
        is the single place to fix — the id is the trailing UUID either way."""
        slug = _slugify(title)
        tail = f"{slug}-{jid}" if slug else jid
        return f"{origin}/{tenant}/public/careers/position/{tail}"

    def _fetch(page):
        return json.loads(http_get(f"{api}?page={page}", headers=BROWSER_HEADERS))

    def _parse(payload):
        out = []
        for r in (payload.get("results") or []):
            jid = str(r.get("id") or "").strip()
            title = str(r.get("title") or "").strip()
            if not (jid and title):
                continue
            # `function` is null on real roles — never chain .get() blindly.
            fn = r.get("function")
            dept = str((fn or {}).get("name") or "").strip() if isinstance(fn, dict) else ""
            names, seen = [], set()
            for loc in (r.get("locations") or []):
                if not isinstance(loc, dict):
                    continue
                nm = str(loc.get("name") or "").strip()
                if not nm:
                    country = loc.get("country")
                    nm = str((country or {}).get("name") or "").strip() \
                        if isinstance(country, dict) else ""
                if nm and nm not in seen:
                    seen.add(nm)
                    names.append(nm)
            out.append(_job(id_=jid, title=title, location="; ".join(names),
                            department=dept, url=_job_url(jid, title)))
        return out

    first = _fetch(1)
    jobs = _parse(first)
    pages = first.get("pages") or {}
    total_pages = int(pages.get("total") or 1)
    count = int(first.get("count") or 0)

    page = 2
    while page <= total_pages and page <= 200:
        _polite_pause()
        try:
            jobs.extend(_parse(_fetch(page)))
        except ConnectorError:
            # A page past the end answers {"detail": "Invalid page."}; treat any
            # later-page failure as the end of the list and let the guard judge.
            break
        page += 1

    jobs = _merge_by_id(jobs)

    if not jobs:
        raise ConnectorError(
            f"Revolut People returned no roles for '{tenant}'. Check the company "
            f"slug in the URL, or the board may genuinely be empty.")

    if count:
        shortfall = count - len(jobs)
        allowed = max(3, count // 100)
        if shortfall > allowed:
            raise ConnectorError(
                f"Revolut People fetch looks incomplete: the API reports {count} "
                f"roles but only {len(jobs)} were collected ({shortfall} missing, "
                f"more than the {allowed} tolerated). Reporting this rather than "
                f"a short list, which would read as roles being removed.")
    return jobs


def successfactors(config):
    """SAP SuccessFactors career sites (Recruiting Marketing / Career Site
    Builder) — a PLATFORM connector.

    Used across UK publishing, retail, public sector and enterprise. One
    connector serves every tenant: the host and the SITE ID both come from the
    pasted URL, never hard-coded. Confirmed live 2026-07-25 against Penguin
    Random House UK (PRH_UK, 19 roles, all London).

    Source (confirmed):
        GET https://<host>/<site>/search/?q=&sortColumn=referencedate
            &sortDirection=desc&startrow=<n>
    Server-rendered HTML table — no JS, no session token, no anti-bot wall.
    Each role is one <tr class="data-row">:
        <tr class="data-row">
          <td class="colTitle">
            <span class="jobTitle hidden-phone">
              <a href="/PRH_UK/job/<slug>/<id>/" class="jobTitle-link">Title</a></span>
            <div class="jobdetail-phone visible-phone">      <-- SAME role again
              <a class="jobTitle-link" href="...">Title</a>
              <span class="jobLocation">London, United Kingdom, SW11 7BW</span>
              <span class="jobDate">24 Jul 2026</span></div></td>
          <td class="colLocation hidden-phone">
            <span class="jobLocation">London, United Kingdom, SW11 7BW</span></td>
        </tr>

    - SITE SCOPING MATTERS MORE HERE THAN ANYWHERE ELSE. Many SuccessFactors
      installs are shared by a whole GROUP of companies, one site id each. The
      bare /search/ on Penguin's host is Bertelsmann's: 826 roles across RTL,
      Arvato, Riverty, Territory, Sonopress and others, with PRH one tenant
      among many. The site id lives in the path (/PRH_UK/, /DK_UK/, /ARVATO/),
      so the connector ALWAYS scopes to the site id from the pasted URL. Point
      it at a group-wide URL and you will track the group. (Dorling Kindersley
      is DK_UK — a separate board from PRH_UK despite the same London address.)
    - ID: the numeric id from /job/<slug>/<id>/ — stable and unique.
    - TITLE: the jobTitle-link text (entities unescaped; "&" is common here).
    - LOCATION: the first <span class="jobLocation"> in the row.
    - DOUBLE MARKUP: each row renders the title link TWICE (a desktop
      .hidden-phone copy and a .visible-phone copy) and the location twice too.
      We parse per-ROW and take the first of each, so a row yields one job;
      _merge_by_id is the backstop. (§6.8 — the double-count trap, same shape as
      the Cursor board.)
    - DEPARTMENT is always "" — the results table carries title, location and
      date only. Reading departments would need one detail fetch per role.
      Blank department is allowed (DATA_FORMATS §1).
    - PAGINATION: ?startrow=N, page size read from page 1 (SuccessFactors
      serves 25 by default). We PIN THE SORT (referencedate desc) rather than
      relying on the site's default order — an unpinned sort is what made
      Avature's paging fail to tile. If the completeness check still comes up
      short (ties on the sort key can shuffle), the crawl is retried once with
      HALF-STEPS so consecutive windows overlap and absorb the drift.
    - COMPLETENESS GUARD: the page prints its own total —
        <span class="paginationLabel">Results <b>1 – 19</b> of <b>19</b></span>
      We compare it against what we collected and raise rather than return a
      short list, which would read as roles being removed.
    - NOT SCOPABLE at the source by city: stays out of market_scope.SCOPABLE.

    config = {"url": "<any URL on the tenant's SuccessFactors site>"}
    """
    import html as _html

    url_in = (config or {}).get("url", "")
    if not str(url_in).strip():
        raise ConnectorError(
            "The SuccessFactors connector needs the careers URL, e.g. "
            "https://jobsearch.createyourowncareer.com/PRH_UK/search/")

    parsed = urllib.parse.urlparse(str(url_in).strip())
    host = parsed.netloc
    if not host:
        raise ConnectorError(f"Couldn't read a host from the URL: {url_in!r}")

    # First path segment is the SITE ID unless it's one of the site's own pages.
    _PAGES = {"search", "job", "content", "go", "viewalljobs", "talentcommunity",
              "jobs", "login", "profile"}
    segs = [s for s in parsed.path.split("/") if s]
    site = segs[0] if segs and segs[0].lower() not in _PAGES else ""
    scheme = parsed.scheme or "https"
    base = f"{scheme}://{host}/{site}/search/" if site else f"{scheme}://{host}/search/"
    origin = f"{scheme}://{host}"

    ROW = re.compile(r'(?s)<tr[^>]*class="[^"]*\bdata-row\b[^"]*"[^>]*>(.*?)</tr>')
    LINK = re.compile(r'<a[^>]*?href="([^"]+)"[^>]*?class="[^"]*jobTitle-link[^"]*"[^>]*>(.*?)</a>'
                      r'|<a[^>]*?class="[^"]*jobTitle-link[^"]*"[^>]*?href="([^"]+)"[^>]*>(.*?)</a>',
                      re.S)
    LOC = re.compile(r'(?s)<span[^>]*class="[^"]*\bjobLocation\b[^"]*"[^>]*>(.*?)</span>')
    TOTAL = re.compile(r'(?is)paginationLabel.*?\bof\b\s*<b>\s*([\d,]+)\s*</b>')

    def _text(fragment):
        return _html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()

    def _parse_page(page):
        out = []
        for row in ROW.findall(page):
            m = LINK.search(row)                 # first link only: the row
            if not m:                            # renders the same role twice
                continue
            href = _html.unescape(m.group(1) or m.group(3) or "")
            title = _text(m.group(2) or m.group(4) or "")
            jid = href.rstrip("/").rsplit("/", 1)[-1]
            if not (jid and title):
                continue
            lm = LOC.search(row)                 # first location only, same reason
            out.append(_job(
                id_=jid,
                title=title,
                location=_text(lm.group(1)) if lm else "",
                department="",                   # not in the results table
                url=href if href.startswith("http") else origin + href,
            ))
        return out

    def _fetch(startrow):
        q = "q=&sortColumn=referencedate&sortDirection=desc"
        u = f"{base}?{q}" if startrow <= 0 else f"{base}?{q}&startrow={startrow}"
        for attempt in (1, 2):                   # retry a failed page once
            try:
                return http_get(u, headers=BROWSER_HEADERS)
            except ConnectorError:
                if attempt == 2:
                    raise
                _polite_pause()

    first = _fetch(0)
    page1 = _parse_page(first)
    if not page1:
        raise ConnectorError(
            f"No roles found on {base}. Either this site id has no open jobs, "
            f"or the SuccessFactors markup has changed — re-capture the search "
            f"page and check for <tr class=\"data-row\">.")

    tm = TOTAL.search(first)
    total = int(tm.group(1).replace(",", "")) if tm else 0
    page_size = len(page1)
    MAX_REQUESTS = 400

    def _crawl(step):
        got_all, start, reqs, empties = list(page1), step, 1, 0
        while reqs < MAX_REQUESTS:
            if total and len(_merge_by_id(got_all)) >= total:
                break
            if total and start >= total:
                break
            if not total and empties >= 2:
                break
            _polite_pause()
            rows = _parse_page(_fetch(start))
            reqs += 1
            empties = empties + 1 if not rows else 0
            if not rows and (not total or start >= total - page_size):
                break
            got_all.extend(rows)
            start += step
        return _merge_by_id(got_all), reqs

    jobs, requests = _crawl(page_size)

    # Completeness guard. A pinned sort should tile cleanly, but ties on the
    # sort key can still shuffle rows between requests, so a short crawl is
    # retried with overlapping windows before we give up (the Avature fix).
    if total:
        allowed = max(3, total // 100)
        if total - len(jobs) > allowed and page_size > 1:
            jobs, extra = _crawl(max(1, page_size // 2))
            requests += extra
        shortfall = total - len(jobs)
        if shortfall > allowed:
            raise ConnectorError(
                f"SuccessFactors fetch looks incomplete: the site reports "
                f"{total} roles but only {len(jobs)} were collected across "
                f"{requests} pages ({shortfall} missing, more than the "
                f"{allowed} tolerated), even after retrying with overlapping "
                f"pages. Reporting this rather than a short list, which would "
                f"read as roles being removed.")
    return jobs


def avature(config):
    """Avature career portals (*.avature.net) — a PLATFORM connector.

    Avature hosts careers portals for large enterprise employers (Bloomberg,
    and many others). One connector serves every tenant: the subdomain and the
    portal path are read from the pasted URL, never hard-coded.

    Source (confirmed live 2026-07-25 on bloomberg.avature.net, 426 roles):
        GET https://<tenant>.avature.net/<portal>/SearchJobs?jobOffset=<n>
    Server-rendered HTML — no JS, no session token, no anti-bot wall.
    Each role is one <article class="article article--result">:
        <h3 class="article__header__text__title ...">
          <a href="https://<host>/<portal>/JobDetail/<slug>/<id>">Title</a></h3>
        <span class="list-item-location">London, United Kingdom</span>

    THE HARD PART — Avature's paging DOES NOT TILE (measured, not assumed):
      * jobRecordsPerPage is IGNORED. Ask for 100, get 12; Avature even rewrites
        its own pagination links back to 12. The page size is fixed.
      * sortBy is IGNORED too. Passing a sort field returned byte-identical
        results, so the order cannot be pinned.
      * The result order DRIFTS between requests. Adjacent pages re-serve roles
        (offsets 60 and 72 shared 3; 168 and 180 shared 3), and pages are not
        ordered by id — offset 132 spanned 20363-20529 while offset 144 spanned
        20446-20511, nested INSIDE it. A straight 12-step crawl of Bloomberg
        collected 406 of 426: every role re-served on a later page is one that
        drifted out of an earlier window and was never returned. The missing set
        varies per run, which for JobWatch is worse than missing data — compare
        would report those roles REMOVED, then ADDED again next check, spraying
        phantom churn into the run verdict and the trend deltas.

    THE FIX, in two parts:
      1. HALF-STEP PAGING. Step by half a page (6) instead of a whole one, so
         consecutive windows overlap by 6 positions and absorb a role drifting
         up to 6 places. Observed drift is 1-3, so that is roughly double
         cover. The duplicates this creates cost nothing — _merge_by_id already
         collapses them. Price: ~71 requests for Bloomberg instead of 36.
      2. A COMPLETENESS GUARD. Avature prints its own total:
             <div class="list-controls__text__legend" aria-label="426 results">
         We parse it, compare against what we actually collected, and raise a
         clear ConnectorError when materially short. Half-stepping is a
         MITIGATION, not a proof — if drift ever exceeds the overlap, this is
         what catches it. A quietly incomplete fetch would present itself as the
         whole board and read as mass removals; failing loudly is the honest
         option (same reasoning as the empty-parse guards elsewhere here).
      A small shortfall is tolerated because the board legitimately changes
      mid-crawl — Bloomberg's own total moved 436 -> 426 during testing.

    QUERY STRING IS DROPPED, deliberately. A pasted portal URL often carries a
    location facet ("1845=%5B162558%5D" = London for Bloomberg's tenant). Those
    field/option ids are opaque and tenant-specific, so nothing can map a chosen
    city onto them. Honouring one would pin the fetch to a single city, and a
    later change to locations_allowed would silently drop roles rather than
    widen — the incompleteness failure market_scope.py exists to prevent. So we
    fetch the whole board and let filters.py narrow, exactly as the teamtailor
    connector already does with a pasted ?location_id=. Not scopable; stays out
    of market_scope.SCOPABLE.

    DEPARTMENT is always "" — the Avature list view carries title and location
    only. Reading departments would mean one detail fetch per role (400+), which
    is not a reasonable cost. Blank department is allowed (DATA_FORMATS §1);
    Avature companies simply won't contribute to department trends.

    config = {"url": "<any URL on the tenant's Avature portal>"}
    """
    import html as _html

    url_in = (config or {}).get("url", "")
    if not str(url_in).strip():
        raise ConnectorError(
            "The Avature connector needs the careers URL, e.g. "
            "https://bloomberg.avature.net/careers/SearchJobs")

    parsed = urllib.parse.urlparse(str(url_in).strip())
    host = parsed.netloc
    if not host:
        raise ConnectorError(f"Couldn't read a host from the Avature URL: {url_in!r}")
    segs = [s for s in parsed.path.split("/") if s]
    portal = segs[0] if segs and segs[0].lower() != "searchjobs" else "careers"
    base = f"{parsed.scheme or 'https'}://{host}/{portal}/SearchJobs"

    ART = re.compile(r'(?s)<article[^>]*class="[^"]*article--result[^"]*"[^>]*>(.*?)</article>')
    TITLE = re.compile(r'(?s)<h3[^>]*>\s*<a[^>]*href="([^"]*?/JobDetail/[^"]*?)"[^>]*>(.*?)</a>')
    LOC = re.compile(r'(?s)<span[^>]*class="[^"]*list-item-location[^"]*"[^>]*>(.*?)</span>')
    TOTAL = re.compile(r'aria-label="\s*([\d,]+)\s*results?\s*"')

    def _text(fragment):
        return _html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()

    def _parse_page(page):
        out = []
        for art in ART.findall(page):
            m = TITLE.search(art)
            if not m:
                continue
            href, title = _html.unescape(m.group(1)), _text(m.group(2))
            jid = href.rstrip("/").rsplit("/", 1)[-1]
            if not (jid and title):
                continue
            locs = [_text(x) for x in LOC.findall(art)]
            out.append(_job(
                id_=jid,
                title=title,
                location="; ".join([l for l in locs if l]),
                department="",                     # not in the list view
                url=href,
            ))
        return out

    def _fetch(offset):
        u = base if offset <= 0 else f"{base}?jobOffset={offset}"
        for attempt in (1, 2):                     # retry a failed page once
            try:
                return http_get(u, headers=BROWSER_HEADERS)
            except ConnectorError:
                if attempt == 2:
                    raise
                _polite_pause()

    first = _fetch(0)
    rows = _parse_page(first)
    if not rows:
        raise ConnectorError(
            f"No roles found on {base}. The Avature portal markup may have "
            f"changed, or this portal has no open jobs — re-capture the page "
            f"and check for <article class=\"article article--result\">.")

    tm = TOTAL.search(first)
    total = int(tm.group(1).replace(",", "")) if tm else 0

    page_size = len(rows)                          # Avature fixes this (12)
    step = max(1, page_size // 2)                  # half-step: windows overlap
    MAX_REQUESTS = 400                             # hard stop, never hammer

    collected = list(rows)
    offset, requests, empty_runs = step, 1, 0
    while requests < MAX_REQUESTS:
        if total and offset >= total:
            break
        if not total and empty_runs >= 2:
            break
        _polite_pause()
        page = _fetch(offset)
        requests += 1
        got = _parse_page(page)
        empty_runs = empty_runs + 1 if not got else 0
        if not got and total and offset >= total - page_size:
            break
        collected.extend(got)
        offset += step

    jobs = _merge_by_id(collected)

    # Completeness guard — see the docstring. Tolerate a small, honest drift.
    if total:
        shortfall = total - len(jobs)
        allowed = max(3, total // 100)
        if shortfall > allowed:
            raise ConnectorError(
                f"Avature fetch looks incomplete: the board reports {total} "
                f"roles but only {len(jobs)} were collected across {requests} "
                f"pages ({shortfall} missing, more than the {allowed} tolerated). "
                f"Avature reshuffles results between requests, so a role can "
                f"drift out of every page window. Reporting this rather than a "
                f"short list, which would read as roles being removed.")
    return jobs


def smartrecruiters(config):
    """config = {"company": "<company-id>"}  paginates 100 at a time."""
    company = config["company"]
    jobs = []
    offset = 0
    while True:
        url = (f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
               f"?limit=100&offset={offset}")
        data = json.loads(http_get(url))
        content = data.get("content", [])
        for j in content:
            loc = j.get("location", {}) or {}
            loc_str = ", ".join(x for x in [loc.get("city"), loc.get("country")] if x)
            dept = (j.get("department") or {}).get("label", "") if isinstance(j.get("department"), dict) else ""
            jobs.append(_job(
                id_=j.get("id"),
                title=j.get("name"),
                location=loc_str,
                department=dept,
                url=f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}",
            ))
        offset += 100
        if offset >= data.get("totalFound", 0) or not content:
            break
    return jobs


def workable(config):
    """config = {"account": "<subdomain>"}"""
    account = config["account"]
    url = f"https://apply.workable.com/api/v1/widget/accounts/{account}?details=true"
    data = json.loads(http_get(url))
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(_job(
            id_=j.get("shortcode") or j.get("id"),
            title=j.get("title"),
            location=(j.get("location", {}) or {}).get("location_str", ""),
            department=j.get("department", ""),
            url=j.get("url") or j.get("application_url"),
        ))
    return jobs


# --- Workday (Phase C - new connector) -----------------------------------

def workday(config):
    """
    Workday-powered careers sites (used by many large enterprises and the
    Wellcome Trust). Workday exposes a public JSON feed - no login needed.

    config needs (all auto-extracted from the careers URL by detect.py):
        "host":   the careers host exactly as seen, e.g.
                  "wellcome.wd3.myworkdayjobs.com"  (note the wd3 - the data
                  centre number varies per company, so we never assume it)
        "tenant": the company tenant, e.g. "wellcome"
        "site":   the careers site, e.g. "Wellcome"

    The feed lives at:
        https://<host>/wday/cxs/<tenant>/<site>/jobs
    called with POST and a small JSON body. Response has "total" (count) and
    "jobPostings" (the list). We paginate by offset until we've read them all.

    Honest caveat: Workday sits behind Akamai bot management. Checking once
    every few days from a home Mac is fine for normal personal use, but - like
    Apple and Google - it can't be guaranteed never to block. Slow-and-safe.
    Location data is whatever Workday's "locationsText" gives (often a city,
    sometimes a country); our location filter handles the messiness.
    """
    host = config["host"].replace("https://", "").replace("http://", "").strip("/")
    tenant = config["tenant"]
    site = config["site"]

    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    headers = dict(BROWSER_HEADERS)
    headers["Accept"] = "application/json"
    headers["Referer"] = f"https://{host}/en-US/{site}"

    jobs = []
    offset = 0
    page = 20  # Workday's default page size
    while True:
        payload = {"appliedFacets": {}, "limit": page, "offset": offset,
                   "searchText": ""}
        data = json.loads(http_post_json(api, payload, headers=headers))
        postings = data.get("jobPostings", []) or []
        for j in postings:
            ext = j.get("externalPath", "") or ""
            full_url = f"https://{host}/en-US/{site}{ext}" if ext else ""
            jobs.append(_job(
                id_=j.get("bulletFields", [None])[0] or ext or j.get("title"),
                title=j.get("title"),
                location=j.get("locationsText", ""),
                department="",  # Workday listings don't expose a clean dept field
                url=full_url,
            ))
        total = data.get("total", 0) or 0
        offset += page
        if offset >= total or not postings:
            break
        _polite_pause()
    return jobs


# --- Pinpoint (Phase C - new connector) ----------------------------------

def pinpoint(config):
    """
    Pinpoint-powered careers sites (e.g. HarperCollins UK). Pinpoint exposes a
    clean public JSON feed with NO authentication needed - the nicest kind.

    config needs (auto-extracted from the careers URL by detect.py):
        "subdomain": the company subdomain, e.g. "harpercollins" from
                     "harpercollins.pinpointhq.com"

    The feed lives at:
        https://<subdomain>.pinpointhq.com/postings.json
    (We use postings.json, not the deprecated jobs.json.) The response has a
    "data" list of postings. Returns everything in one call - no pagination.

    Field notes from Pinpoint's docs: department moved under a nested "job"
    object in the postings format, but older feeds keep it top-level, so we
    check both. Location is an object {id, name}; the name is sometimes a city,
    sometimes just a country ("United Kingdom") - our location filter copes.
    """
    sub = config["subdomain"]
    url = f"https://{sub}.pinpointhq.com/postings.json"
    headers = dict(BROWSER_HEADERS)
    headers["Accept"] = "application/json"
    headers["X-Requested-With"] = "XMLHttpRequest"

    data = json.loads(http_get(url, headers=headers))
    jobs = []
    for j in data.get("data", []):
        # Department may be nested under "job" (new) or top-level (old).
        dept = ""
        nested_job = j.get("job") or {}
        dept_obj = nested_job.get("department") or j.get("department") or {}
        if isinstance(dept_obj, dict):
            dept = dept_obj.get("name", "")
        loc_obj = j.get("location") or {}
        location = loc_obj.get("name", "") if isinstance(loc_obj, dict) else ""
        jobs.append(_job(
            id_=j.get("id"),
            title=j.get("title"),
            location=location,
            department=dept,
            url=j.get("url") or "",
        ))
    return jobs


# --- A.7: the registry + single entry point ------------------------------
# name -> (function, [required config fields], human description)

CONNECTORS = {
    "greenhouse":      (greenhouse,      ["board"],   "Greenhouse board token (e.g. 'ogilvyuk')"),
    "eightfold":       (eightfold,       ["host"],    "Eightfold careers host + web domain (e.g. host='explore.jobs.netflix.net', domain='netflix.com')"),
    "apple":           (apple,           [],          "Apple Jobs (optional 'location' code like 'postLocation-GBR', 'query', 'locale')"),
    "google":          (google,          [],          "Google Careers (optional 'location' like 'London, UK')"),
    "lever":           (lever,           ["board"],   "Lever board token"),
    "ashby":           (ashby,           ["board"],   "Ashby board token"),
    "smartrecruiters": (smartrecruiters, ["company"], "SmartRecruiters company id"),
    "workable":        (workable,        ["account"], "Workable account subdomain"),
    "workday":         (workday,         ["host", "tenant", "site"], "Workday careers host + tenant + site (auto-extracted from a *.myworkdayjobs.com URL)"),
    "pinpoint":        (pinpoint,        ["subdomain"], "Pinpoint subdomain (auto-extracted from a *.pinpointhq.com URL)"),
    "uber":            (uber,            [],          "Uber Careers (jobs.uber.com) — coordinate-scoped; reads chosen cities via CITY_COORDS"),
    "spotify":         (spotify,         [],          "Spotify Careers (lifeatspotify.com) — self-diagnosing endpoint probe; reads chosen city slugs"),
    "playstation":     (playstation,     [],          "PlayStation Careers (careers.playstation.com) — custom API across studio Greenhouse boards; city-name scoped via filter[city]"),
    "deliveroo":       (deliveroo,       [],          "Deliveroo Careers (careers.deliveroo.co.uk) — WordPress REST feed; reads embedded location/team terms"),
    "va":              (va,              [],          "Victoria and Albert Museum (vam.ac.uk/vacancies) — server-rendered HTML list; Hireserve FeedLink ids"),
    "webitrent":       (webitrent,       ["url"],     "MHR iTrent web recruitment (*.webitrent.com) — anonymous-session JSON list; UK public-sector/cultural employers"),
    "ciphr":           (ciphr,           ["url"],     "CIPHR iRecruit (*.ciphr-irecruit.com) — server-rendered HTML vacancy table; UK public-sector/cultural employers"),
    "sohohouse":       (sohohouse,       [],          "Soho House Careers (careers.sohohouse.com) — Next.js static vacancy JSON; reads build id then the data file"),
    "teamtailor":      (teamtailor,      ["url"],     "Teamtailor career sites (*.teamtailor.com or a custom domain) — server-rendered HTML job list; UK/EU employers. Needs the careers URL."),
    "cursor":          (cursor,          [],          "Cursor / Anysphere (cursor.com/careers) — server-rendered HTML list; slug ids, list rendered twice so merged by id"),
    "revolutpeople":   (revolutpeople,   ["url"],     "Revolut People (revolutpeople.com/<company>/public/careers) — public JSON API, no auth. Needs the careers URL."),
    "successfactors":  (successfactors,  ["url"],     "SAP SuccessFactors career sites (Recruiting Marketing / Career Site Builder). Server-rendered results table; scopes to the SITE ID in the pasted URL, since many installs are shared by a whole group. Needs the careers URL."),
    "avature":         (avature,         ["url"],     "Avature career portals (*.avature.net) — enterprise employers. Server-rendered HTML; pages by half-steps because Avature's result order drifts, with a completeness guard. Needs the careers URL."),
}


def fetch_jobs(provider, config):
    """
    The single entry point the rest of the app uses.
    Looks up the connector by name, checks required config is present,
    runs it, and returns the clean job list. Raises ConnectorError on problems.
    """
    if provider not in CONNECTORS:
        raise ConnectorError(f"Don't recognise the provider '{provider}'.")
    func, required, _ = CONNECTORS[provider]
    for field in required:
        if not str(config.get(field, "")).strip():
            raise ConnectorError(
                f"The '{provider}' connector needs a '{field}' value, which is missing."
            )
    return func(config)


# Allow a quick manual test:  python3 jobwatch/connectors.py
if __name__ == "__main__":
    import sys
    prov = sys.argv[1] if len(sys.argv) > 1 else "greenhouse"
    cfg = {"board": "ogilvyuk"} if prov == "greenhouse" else {}
    if prov == "eightfold":
        cfg = {"host": "explore.jobs.netflix.net", "domain": "netflix.com"}
    try:
        result = fetch_jobs(prov, cfg)
        print(f"{prov}: fetched {len(result)} jobs. First 10:\n")
        for jb in result[:10]:
            print(f"  - {jb['title']}  [{jb['location']}]  dept={jb['department'] or '-'}")
    except ConnectorError as err:
        print(f"{prov}: {err}")
