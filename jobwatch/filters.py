"""
filters.py  (Phase C.3 + C.4 + C.5)
===================================
Once we have a clean job list from a connector, three things happen to it:

  C.3  LOCATION FILTER  - the ONLY hard gate. Roles clearly elsewhere are
       removed; roles in your locations are kept; roles the board tagged only
       at country level (ambiguous) are KEPT SEPARATELY so you can eyeball them.

  C.4  INTEREST FLAGGING - your ranked keywords surface matching roles in
       preference order. This NEVER hides anything; it sorts/labels.

  C.5  EXPERIENCE FLAG - if a listing's text asks for more years than you set,
       it gets an amber "stretch" flag. Again, never hidden - you can still apply.

The guiding rule from the handover: LOCATION HIDES, EVERYTHING ELSE FLAGS.
And location filtering is forgiving by design, because board location data is
messy and we must never silently drop a role that might be relevant.
"""

import re

from . import geo


# ---- C.3: Location filter ("contains + ambiguous bucket") ----------------

# Words that mean "no specific city given" - a role tagged only like this is
# ambiguous, not clearly elsewhere, so it goes in the review bucket not the bin.
# Expanded this session beyond the UK so country-only roles from US/EU boards
# (Google reports country-level; some Apple roles too) are recognised as
# country-only rather than mistaken for "elsewhere".
_COUNTRY_ONLY = {
    # UK
    "united kingdom", "uk", "england", "scotland", "wales", "great britain", "gb",
    # US
    "united states", "usa", "us", "u.s.", "u.s.a.", "america",
    # Other common single-country / remote tags
    "ireland", "eire", "germany", "deutschland", "france", "netherlands",
    "holland", "singapore", "canada", "australia", "spain", "italy",
    "remote", "remote uk", "remote - uk", "remote us", "anywhere",
}

# Genuinely location-LESS tags: these could be anywhere (including your city), so
# a role tagged ONLY like this goes to review rather than being excluded. A
# concrete foreign country ("Australia") is NOT vague and must be excluded when
# you didn't filter for it — that distinction fixes foreign-role leakage.
_VAGUE_ANYWHERE = {
    "remote", "anywhere", "remote uk", "remote - uk", "remote us",
    "worldwide", "global", "flexible", "various", "multiple locations",
}

# Some employers tag a role with a VENUE / BUILDING / CAMPUS name instead of a
# city — e.g. the National Gallery's board reports every role's location as
# "The National Gallery", never "London". Pure city-text matching then drops all
# of them (they contain no city name and no country term). This map resolves such
# known site names to their real city so the location gate can match them. It is
# deliberately CONSERVATIVE: only listed venues resolve; an unknown site name
# stays strict (unchanged behaviour). Matching-only — the displayed location text
# is left untouched (kept truthful), and a matched-via-site role is tagged so the
# UI can show "London — site not a city" the same way country-only roles are.
# Keys are _norm()'d site strings; values are the canonical city (lowercase).
_SITE_CITY = {
    "the national gallery": "london",
    "national gallery": "london",
}


def _norm(text: str) -> str:
    """Lowercase and collapse odd spacing/punctuation for forgiving matching.
    Handles the real-world mess like 'London,United Kingdom' (missing space)."""
    t = (text or "").lower()
    t = t.replace(",", ", ")          # fix missing space after commas
    t = re.sub(r"\s+", " ", t).strip()
    return t


def filter_by_location(jobs: list, locations_allowed: list) -> dict:
    """
    C.3 (+ this session's city-first country fallback) - split jobs by location.

    locations_allowed - e.g. ["London", "New York"]. The locked rule is CITY-level
    matching: a role whose location text contains an allowed CITY is a confirmed
    match. BUT some boards only tag at country level (Google reports country only;
    some Apple roles say just "United Kingdom"). With pure city text-matching those
    roles never match a city filter and get lost. So:

      - CONFIRMED city match  -> "matched"          (contains the city text)
      - COUNTRY-ONLY role in the filtered city's COUNTRY (or, for US cities, its
        STATE / state-code) -> "matched" too, but each such job is flagged
        job["location_country_only"] = True so the UI can tag it
        ("UK - city not specified"). Pulled in, never disguised.
      - clearly elsewhere     -> "elsewhere"        (filtered out)
      - no location text / country-only in a country we're NOT filtering ->
        "ambiguous" (the old review bucket; never silently dropped)

    The country fallback only fires for cities we KNOW (geo.py). An unknown city
    stays strict city-only — the safe default, so we never loosen a filter we
    can't reason about.

    Returns {"matched", "ambiguous", "elsewhere"}. As before, matched + ambiguous
    are shown; elsewhere is the hard gate. Empty locations -> filtering OFF.

    NOTE on the flag: jobs going into "matched" via the country fallback carry
    location_country_only=True; confirmed city matches carry it False. apply_all
    surfaces this alongside location_unclear so the UI can label them.
    """
    allowed_raw = [loc for loc in (locations_allowed or []) if loc and loc.strip()]
    allowed_norm = [_norm(loc) for loc in allowed_raw]

    # No locations configured -> don't filter by location at all (show everything).
    if not allowed_norm:
        return {"matched": list(jobs), "ambiguous": [], "elsewhere": []}

    # Build, per filtered city, the set of country/state-level terms that should
    # count as a (tagged) match for that city. Cities we don't know contribute
    # nothing here, so they stay strict city-only.
    geo_map = geo.load_geo()
    country_terms = set()
    for loc in allowed_raw:
        for term in geo.country_terms_for_city(loc, geo_map):
            country_terms.add(term)

    matched, ambiguous, elsewhere = [], [], []

    for job in jobs:
        loc = _norm(job.get("location", ""))
        if not loc:
            ambiguous.append(job)            # no location at all -> review
            continue

        # 1) Confirmed city match (the locked city-level rule).
        if any(a in loc for a in allowed_norm):
            j = dict(job)
            j["location_country_only"] = False
            matched.append(j)
            continue

        # 1b) Known VENUE/SITE name that maps to a city (e.g. "The National
        #     Gallery" -> London). Employers who tag by building instead of city
        #     would otherwise be dropped entirely. Conservative: only listed sites
        #     resolve; the displayed location text is left as-is (truthful), and
        #     the role is tagged location_via_site so the UI can note the city was
        #     inferred from the site, mirroring the country-only tag.
        site_city = _SITE_CITY.get(loc)
        if site_city and any(_norm(a) == site_city or site_city in _norm(a)
                             for a in allowed_raw):
            j = dict(job)
            j["location_country_only"] = False
            j["location_via_site"] = True
            matched.append(j)
            continue

        # Tokens of the role's location (comma-separated parts).
        tokens = {p.strip() for p in loc.split(",") if p.strip()}

        # Is the role tagged only at country/remote level (no specific city)?
        # A role is "country-only" if every one of its location tokens is itself
        # a country/region term — either in the global set OR (crucially) in the
        # country terms for one of our filtered cities. This is what lets a
        # "United Arab Emirates" role count as country-only for a "Dubai" filter
        # even though UAE isn't in the static set.
        country_word_set = _COUNTRY_ONLY | country_terms
        is_country_only = (bool(tokens) and tokens.issubset(country_word_set)) \
            or loc in country_word_set

        # 2) Country fallback: a country-only role whose country/state matches one
        #    of our filtered cities' countries -> pull in, but TAG it.
        if country_terms and any(term in loc for term in country_terms):
            # Only treat as a city-country match when the role itself is
            # country/region-level (no competing city). A role naming a DIFFERENT
            # city in the same country shouldn't be claimed as ours.
            if is_country_only:
                j = dict(job)
                j["location_country_only"] = True
                matched.append(j)
                continue
            # Names a specific (other) city in the right country -> elsewhere,
            # unless that city is itself one we filter (already caught in step 1).
            elsewhere.append(job)
            continue

        # 3) No city match, no country fallback (role isn't in a filtered city's
        #    country). The old "never drop a country-only role" rule was meant for
        #    genuinely VAGUE locations ("Remote", "Anywhere") that could include
        #    your city. A role tagged with a SPECIFIC foreign country you didn't
        #    filter for (e.g. "Australia" when you filtered "London") is NOT
        #    relevant and must be excluded — otherwise foreign roles leak into the
        #    results. So: only truly-vague tags go to review; concrete non-matching
        #    locations (foreign country or foreign city) go to elsewhere.
        only_vague = bool(tokens) and tokens.issubset(_VAGUE_ANYWHERE)
        if only_vague or not loc:
            ambiguous.append(job)            # "remote"/"anywhere"/blank -> review
        else:
            elsewhere.append(job)            # a concrete place we didn't ask for

    return {"matched": matched, "ambiguous": ambiguous, "elsewhere": elsewhere}


# ---- M.4 + this session: Department rank-or-filter ----------------------

def filter_by_department(jobs: list, departments_allowed: list,
                         mode: str = "filter") -> dict:
    """
    Split/flag jobs by department. Mirrors the keyword rank-or-filter choice.

    departments_allowed - e.g. ["Finance", "Strategy"]. Contains-match, case-
    insensitive (so "Finance" catches "Global Finance"), because departments are
    named inconsistently across boards.

    mode:
      "filter" (default, the original M.4 gate): only roles whose department
               MATCHES are shown, PLUS roles with no department at all (kept in
               "unclear" for review, never silently dropped). Non-matching
               departments go to "elsewhere".
      "rank"   (new, mirrors keywords): NOTHING is hidden. Every role comes back
               in "matched" (so apply_all shows them all), but each is tagged
               department_match True/False so the UI can flag + sort the matches
               to the top. "unclear"/"elsewhere" are empty in rank mode.

    Returns {"matched", "unclear", "elsewhere"} where every returned job is a
    COPY carrying:
        job["department_match"] : True if its department matched an allowed term

    Empty departments_allowed -> filtering OFF: everything in "matched",
    department_match False (no department lens active).
    """
    allowed_norm = [_norm(d) for d in (departments_allowed or []) if d.strip()]
    mode = (mode or "filter").strip().lower()
    if mode not in ("rank", "filter"):
        mode = "filter"

    # No departments configured -> don't filter/flag by department at all.
    if not allowed_norm:
        out = []
        for job in jobs:
            j = dict(job)
            j["department_match"] = False
            out.append(j)
        return {"matched": out, "unclear": [], "elsewhere": []}

    matched, unclear, elsewhere = [], [], []
    for job in jobs:
        dept = _norm(job.get("department", ""))
        hit = bool(dept) and any(a in dept for a in allowed_norm)
        j = dict(job)
        j["department_match"] = hit

        if mode == "rank":
            # Nothing hidden: everything is "matched"; the flag drives sort/label.
            matched.append(j)
            continue

        # filter mode (gate):
        if not dept:
            unclear.append(j)                # no department -> review, never drop
        elif hit:
            matched.append(j)
        else:
            elsewhere.append(j)

    return {"matched": matched, "unclear": unclear, "elsewhere": elsewhere}


def department_values(jobs: list) -> list:
    """The DISTINCT department values present across a set of jobs, with a count
    each, sorted by count (desc) then name. Powers the "show actual department
    values" discovery view, so the user picks real names instead of guessing
    ("is Corporate hiring? is Finance hiring?"). Blank departments are skipped.

    Returns: [{"name": "Retail", "count": 12}, {"name": "Finance", "count": 9}, ...]
    """
    counts = {}
    for job in jobs:
        name = (job.get("department") or "").strip()
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
    rows = [{"name": n, "count": c} for n, c in counts.items()]
    rows.sort(key=lambda r: (-r["count"], r["name"].lower()))
    return rows


# ---- C.4: Ranked interest flagging ---------------------------------------

def flag_interests(jobs: list, keywords_ranked: list,
                   mode: str = "rank") -> list:
    """
    C.4 - Attach interest info to each job. Two modes (M.3):

      "rank"   (default, the locked v1 behaviour, UNCHANGED): nothing is hidden.
               Matching roles sort to the top by best rank; unmatched roles stay
               in the list, after the matched ones.
      "filter" (M.3, the new gate behaviour the user chose): only roles matching
               at least one keyword are returned. Non-matches are excluded. Used
               when the user flips the Jobs filter panel's keyword toggle to
               "Filter" instead of "Rank".

    In BOTH modes each returned job still carries the flag fields, so the UI tags
    render the same way:
        "interest_rank":  the best (lowest) rank index matched, or None
        "interest_hits":  the list of keywords that matched (in rank order)

    With no keywords set, "filter" mode would hide everything; instead, an empty
    keyword list is treated as "no keyword gate" and EVERY role is kept (in
    original order, unranked) - the same honest empty-default as the location and
    department filters. The gate only bites once you've actually entered keywords.
    """
    ranked = [k.strip() for k in (keywords_ranked or []) if k.strip()]
    ranked_lower = [k.lower() for k in ranked]

    out = []
    for idx, job in enumerate(jobs):
        haystack = f"{job.get('title','')} {job.get('department','')}".lower()
        hits = [ranked[i] for i, kw in enumerate(ranked_lower) if kw in haystack]
        best = None
        for i, kw in enumerate(ranked_lower):
            if kw in haystack:
                best = i
                break
        j = dict(job)
        j["interest_rank"] = best
        j["interest_hits"] = hits
        out.append((idx, j))

    # Filter mode: keep only matched roles - but ONLY when keywords are actually
    # set. No keywords => no gate => keep everything (honest empty-default).
    if mode == "filter" and ranked:
        out = [(idx, j) for idx, j in out if j["interest_rank"] is not None]

    # Sort: matched first (by best rank asc), then unmatched; stable by original index.
    def sort_key(pair):
        orig_idx, j = pair
        rank = j["interest_rank"]
        return (0, rank, orig_idx) if rank is not None else (1, 0, orig_idx)

    out.sort(key=sort_key)
    return [j for _, j in out]


# ---- C.5: Experience "stretch" flag --------------------------------------

# Patterns like "5+ years", "5-7 years", "minimum 5 years", "at least 5 years".
_YEARS_PATTERNS = [
    re.compile(r"(\d+)\s*\+\s*years"),
    re.compile(r"(\d+)\s*-\s*\d+\s*years"),
    re.compile(r"(?:minimum|min\.?|at least)\s*(\d+)\s*years"),
    re.compile(r"(\d+)\s*years?"),
]


def _years_required(text: str) -> int | None:
    """Pull the smallest 'years required' number from listing text, if any.
    Returns None if no year requirement is detectable."""
    if not text:
        return None
    t = text.lower()
    found = []
    for pat in _YEARS_PATTERNS:
        for m in pat.finditer(t):
            try:
                found.append(int(m.group(1)))
            except (ValueError, IndexError):
                pass
    # The "minimum" we'd be held to is the smallest stated requirement.
    return min(found) if found else None


def flag_experience(jobs: list, experience_years_max: int | None,
                    text_field: str = "title") -> list:
    """
    C.5 - Add an amber "stretch" flag to roles asking for more years than your
    max. NEVER hides anything.

    experience_years_max - your comfortable ceiling (e.g. 8). None disables the flag.
    text_field           - which job field to scan for a years requirement.
                           Defaults to "title"; pass "description" if connectors
                           ever carry full text (they don't today, so title is a
                           best-effort scan - honestly limited, see note).

    Returns a NEW list of jobs (copies) each with added fields:
        "experience_required": int years detected, or None
        "experience_stretch":  True if that exceeds your max

    NOTE (honesty): our connectors return title/location/dept/url, not full
    descriptions, so today this can only catch a years figure that appears in
    the TITLE (rare). It's wired correctly so that if richer text becomes
    available later, the flag just starts working better - no rebuild needed.
    """
    out = []
    for job in jobs:
        text = job.get(text_field, "") or ""
        req = _years_required(text)
        stretch = (experience_years_max is not None
                   and req is not None
                   and req > experience_years_max)
        j = dict(job)
        j["experience_required"] = req
        j["experience_stretch"] = bool(stretch)
        out.append(j)
    return out


# ---- Convenience: run the whole C.3->C.4->C.5 pipeline -------------------

def apply_all(jobs: list, interests: dict) -> dict:
    """
    Run the full filter+flag pipeline using an interests record (the locked
    interests.json shape, extended in Phase M):
        {
            "keywords_ranked":     [...],
            "keywords_mode":       "rank" | "filter",   # M.3; default "rank"
            "locations_allowed":   [...],
            "departments_allowed": [...],               # M.4; default []
            "experience_years_max": int | null
        }

    Pipeline order (M): LOCATION gate -> DEPARTMENT gate -> KEYWORD rank/filter
    -> EXPERIENCE flag. Location is always the hard gate; department gates only
    when departments are set (blank-department roles go to a "department unclear"
    group, never dropped); keywords either rank (default) or filter, per
    keywords_mode. Backward-compatible: a record missing the new fields behaves
    exactly as before (mode "rank", no department gate).

    Returns:
        {
            "shown":     [job, ...],   # what to show you: location+department
                                       # survivors, keyword-ranked-or-filtered,
                                       # experience-flagged. Each carries
                                       # location_unclear + department_unclear.
            "matched":   [...],        # subset: clear location match
            "ambiguous": [...],        # subset: location unclear (review)
            "elsewhere": [...],        # filtered out by location
            "dept_matched":  [...],    # subset: clear department match
            "dept_unclear":  [...],    # subset: no department (review)
            "dept_elsewhere":[...],    # filtered out by department
        }
    """
    # 1. Location gate (the only always-on hard filter). matched + ambiguous pass.
    loc = filter_by_location(jobs, interests.get("locations_allowed", []))
    after_location = loc["matched"] + loc["ambiguous"]
    ambiguous_ids = {j.get("id") for j in loc["ambiguous"]}
    # Country-only matches (pulled in by the city->country fallback) carry a flag
    # set in filter_by_location; remember their ids so we can re-tag after the
    # later stages make their own copies.
    country_only_ids = {j.get("id") for j in loc["matched"]
                        if j.get("location_country_only")}

    # 2. Department rank-or-filter (mirrors keywords). In "filter" mode it gates
    #    (matched + no-department-unclear shown); in "rank" mode nothing is hidden
    #    and department matches are flagged/sorted. Default "filter" preserves the
    #    original M.4 behaviour for existing saved records.
    dept_mode = (interests.get("departments_mode") or "filter").strip().lower()
    if dept_mode not in ("rank", "filter"):
        dept_mode = "filter"
    dept = filter_by_department(after_location,
                                interests.get("departments_allowed", []),
                                mode=dept_mode)
    after_department = dept["matched"] + dept["unclear"]
    dept_unclear_ids = {j.get("id") for j in dept["unclear"]}
    dept_match_ids = {j.get("id") for j in (dept["matched"] + dept["unclear"])
                      if j.get("department_match")}

    # 3. Keyword rank-or-filter (M.3). Default "rank" = nothing hidden.
    mode = (interests.get("keywords_mode") or "rank").strip().lower()
    if mode not in ("rank", "filter"):
        mode = "rank"
    flagged = flag_interests(after_department,
                             interests.get("keywords_ranked", []), mode=mode)

    # 4. Experience stretch flag (never hides).
    flagged = flag_experience(flagged, interests.get("experience_years_max"))

    # Tag the unclear groups so the UI can mark/group them. Both are independent:
    # a role can be location-unclear, department-unclear, neither, or both.
    for j in flagged:
        jid = j.get("id")
        j["location_unclear"] = jid in ambiguous_ids
        j["department_unclear"] = jid in dept_unclear_ids
        # City filter pulled this in via its country (e.g. a Google "United
        # Kingdom" role matched a "London" filter): show it, but tag it.
        j["location_country_only"] = jid in country_only_ids
        # Department match (for rank-mode flagging + filter-mode confirmation).
        j["department_match"] = jid in dept_match_ids

    # If department ranking is on, sort matches up while preserving the existing
    # interest order within each group (stable). Keyword rank stays primary when
    # both are active; department is a gentle secondary lift.
    if dept_mode == "rank" and interests.get("departments_allowed"):
        flagged.sort(key=lambda j: 0 if j.get("department_match") else 1)

    return {
        "shown": flagged,
        "matched": loc["matched"],
        "ambiguous": loc["ambiguous"],
        "elsewhere": loc["elsewhere"],
        "dept_matched": dept["matched"],
        "dept_unclear": dept["unclear"],
        "dept_elsewhere": dept["elsewhere"],
        # The distinct department values present in what's shown — for the UI's
        # "actual departments" discovery list (Corporate vs Finance hiring?).
        "department_values": department_values(flagged),
    }


# Quick manual test:  python3 -m jobwatch.filters
if __name__ == "__main__":
    sample = [
        {"id": "1", "title": "Data Strategy Director", "location": "London, United Kingdom", "department": "Strategy", "url": ""},
        {"id": "2", "title": "Engineer (5+ years)", "location": "London,United Kingdom", "department": "Eng", "url": ""},
        {"id": "3", "title": "Analyst", "location": "United Kingdom", "department": "Data", "url": ""},
        {"id": "4", "title": "Manager", "location": "Paris, France", "department": "Ops", "url": ""},
        {"id": "5", "title": "Coordinator", "location": "Remote UK", "department": "", "url": ""},
    ]
    interests = {
        "keywords_ranked": ["strategy", "data", "director"],
        "locations_allowed": ["London", "Remote UK"],
        "experience_years_max": 3,
    }
    res = apply_all(sample, interests)
    print(f"shown={len(res['shown'])} matched={len(res['matched'])} "
          f"ambiguous={len(res['ambiguous'])} elsewhere={len(res['elsewhere'])}")
    for j in res["shown"]:
        tags = []
        if j["interest_rank"] is not None:
            tags.append(f"interest#{j['interest_rank']+1}({','.join(j['interest_hits'])})")
        if j["location_unclear"]:
            tags.append("location-unclear")
        if j["experience_stretch"]:
            tags.append(f"STRETCH({j['experience_required']}y)")
        print(f"  [{j['id']}] {j['title']}  <{j['location']}>  {' '.join(tags)}")
    print("  filtered out (elsewhere):", [j["id"] for j in res["elsewhere"]])
